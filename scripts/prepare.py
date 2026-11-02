"""Build an offline ARM64 payload without modifying pi-pocket source (Python 3.11+)."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / '.cache'
ASSETS = ROOT / 'app/src/main/assets'
REPO = 'https://packages-cf.termux.dev/apt/termux-main'
BOOTSTRAP = {
    'url': 'https://github.com/termux/termux-packages/releases/download/bootstrap-2026.02.12-r1%2Bapt.android-7/bootstrap-aarch64.zip',
    'sha256': 'ea2aeba8819e517db711f8c32369e89e7c52cee73e07930ff91185e1ab93f4f3',
}
ARCHITECTURES = {'aarch64': 'arm64-v8a', 'x86_64': 'x86_64'}
BOOTSTRAP_SHA256 = {
    'aarch64': BOOTSTRAP['sha256'],
    'x86_64': 'b7fd0f2e3a4de534be3144f9f91acc768630fc463eaf134ab2e64c545e834f7a',
}
PREFIX_PARTS = ('data', 'data', 'com.termux', 'files', 'usr')
PROOT_BOOTSTRAP_PACKAGES = {'proot', 'libtalloc', 'libandroid-shmem'}
PACKAGES = ['nodejs-lts', 'npm', 'git', 'bash', 'coreutils', 'findutils', 'grep', 'sed',
            'tar', 'ripgrep', 'fd', 'termux-exec', 'ca-certificates', 'curl', 'diffutils', 'which', 'util-linux', 'proot']

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()

def download_urls(url: str, locked: bool) -> list[str]:
    """Only checksum-locked Termux binaries may use the equivalent official mirror."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https':
        raise ValueError('Build downloads require HTTPS')
    candidates = [url]
    mirrors = ('packages-cf.termux.dev', 'packages.termux.dev')
    if locked and parsed.netloc in mirrors and parsed.path.startswith('/apt/termux-main/'):
        candidates.extend(parsed._replace(netloc=mirror).geturl()
                          for mirror in mirrors if mirror != parsed.netloc)
    return candidates

def fetch_download(url: str, target: Path) -> None:
    if os.name == 'nt':
        subprocess.run(['powershell', '-NoProfile', '-File', str(ROOT / 'scripts/download.ps1'),
                        '-Url', url, '-Destination', str(target)], check=True, timeout=120)
    elif curl := shutil.which('curl'):
        # curl uses the runner's CA store and handles GitHub asset redirects. Do not disable TLS.
        subprocess.run([curl, '--fail', '--location', '--silent', '--show-error',
                        '--proto', '=https', '--proto-redir', '=https',
                        '--connect-timeout', '15', '--max-time', '90',
                        '--output', str(target), url], check=True, timeout=100)
    else:
        request = urllib.request.Request(url, headers={'User-Agent': 'PiPocketAndroidBuild/0.1'})
        with urllib.request.urlopen(request, timeout=30) as response, target.open('wb') as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)

def download(url: str, target: Path, sha256: str | None = None) -> Path:
    if target.exists() and sha256 and file_digest(target) == sha256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + '.part')
    last_error = None
    for candidate in download_urls(url, locked=sha256 is not None):
        for attempt in range(2):
            print('Download', candidate, f'(attempt {attempt + 1})', flush=True)
            try:
                fetch_download(candidate, temp)
                if sha256 and file_digest(temp) != sha256:
                    raise RuntimeError(f'Checksum mismatch: {candidate}')
                temp.replace(target)
                return target
            except (OSError, subprocess.SubprocessError) as error:
                last_error = error
                print(f'Download failed: {type(error).__name__}; retrying or using official mirror', flush=True)
            finally:
                temp.unlink(missing_ok=True)
            if attempt == 0:
                time.sleep(2)
    raise RuntimeError(f'Could not download locked build input: {url}') from last_error

def parse_index(text: str) -> dict[str, dict[str, str]]:
    result = {}
    for paragraph in text.strip().split('\n\n'):
        fields = {}
        last = ''
        for line in paragraph.splitlines():
            if line.startswith(' ') and last:
                fields[last] += '\n' + line
            elif ': ' in line:
                last, value = line.split(': ', 1)
                fields[last] = value
        if 'Package' in fields:
            result[fields['Package']] = fields
    return result

def dependency_names(expression: str) -> list[list[str]]:
    return [[re.split(r'[ (:\[]', item.strip())[0] for item in group.split('|')]
            for group in expression.split(',') if group.strip()]

def resolve(index: dict[str, dict[str, str]], roots: list[str]) -> list[dict[str, str]]:
    # npm's nodejs alternative must resolve to the explicitly selected LTS package.
    aliases = {'nodejs': 'nodejs-lts'}
    selected = {}
    def visit(name):
        name = aliases.get(name, name)
        if name in selected:
            return
        if name not in index:
            raise RuntimeError(f'Unresolved package {name}; review runtime dependencies')
        package = index[name]
        selected[name] = package
        for options in dependency_names(package.get('Pre-Depends', '') + ',' + package.get('Depends', '')):
            choice = next((aliases.get(option, option) for option in options if aliases.get(option, option) in index), None)
            if choice is None:
                raise RuntimeError(f'Unresolved dependency of {name}: {options}')
            visit(choice)
    for name in roots:
        visit(name)
    return [{k: p[k] for k in ('Package', 'Version', 'Architecture', 'Filename', 'SHA256')}
            for p in sorted(selected.values(), key=lambda p: p['Package'])]

def runtime_lock_filename(architecture: str) -> str:
    return 'runtime.lock.json' if architecture == 'aarch64' else f'runtime-{architecture}.lock.json'

def runtime_lock(update: bool, architecture: str = 'aarch64') -> dict:
    if architecture not in ARCHITECTURES:
        raise ValueError(f'Unsupported architecture: {architecture}')
    path = ROOT / runtime_lock_filename(architecture)
    if update:
        raw = download(REPO + f'/dists/stable/main/binary-{architecture}/Packages', CACHE / f'Packages-{architecture}').read_bytes()
        packages = resolve(parse_index(raw.decode()), PACKAGES)
        bootstrap = {'url': BOOTSTRAP['url'].replace('bootstrap-aarch64.zip', f'bootstrap-{architecture}.zip'),
                     'sha256': BOOTSTRAP_SHA256[architecture]}
        lock = {'schemaVersion': 1, 'architecture': architecture, 'repository': REPO,
                'indexSha256': digest(raw), 'bootstrap': bootstrap, 'roots': PACKAGES, 'packages': packages}
        path.write_text(json.dumps(lock, indent=2) + '\n', encoding='utf-8')
    elif not path.exists():
        raise RuntimeError('Missing runtime.lock.json; first run requires --update-runtime-lock')
    lock = json.loads(path.read_text(encoding='utf-8'))
    if lock.get('architecture') != architecture:
        raise RuntimeError('Runtime lock architecture does not match --arch')
    return lock

def run(args, cwd=None):
    print('+', ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)

def prepare_upstream(source: Path | None, lock: dict) -> Path:
    target = CACHE / 'upstream'
    stamp = target / '.pocket-source-commit'
    if stamp.exists() and stamp.read_text().strip() == lock['commit'] and (target / 'node_modules').exists():
        return target
    if target.exists():
        # Only delete our exact staging directory, never the user-provided repository.
        if target.resolve() != (ROOT / '.cache/upstream').resolve():
            raise RuntimeError('Unexpected staging path')
        shutil.rmtree(target)
    target.mkdir(parents=True)
    archive = CACHE / 'upstream.tar'
    if source:
        run(['git', '-C', source, 'archive', '--format=tar', '-o', archive, lock['commit']])
    else:
        repo = CACHE / 'source.git'
        if not repo.exists():
            run(['git', 'clone', '--bare', '--filter=blob:none', lock['repository'], repo])
        run(['git', '-C', repo, 'fetch', 'origin', lock['commit']])
        run(['git', '-C', repo, 'archive', '--format=tar', '-o', archive, lock['commit']])
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter='data')
    package = json.loads((target / 'package.json').read_text(encoding='utf-8'))
    if package['version'] != lock['version']:
        raise RuntimeError('upstream.lock version does not match pinned source')
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not npm:
        raise RuntimeError('Node.js >=22.19 and npm are required')
    run([npm, 'ci', '--omit=dev', '--omit=optional', '--ignore-scripts', '--no-audit', '--no-fund'], target)
    stamp.write_text(lock['commit'], encoding='utf-8')
    return target

def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError(f'Unsafe archive path {name}')
    return str(path)

def deb_data_archive(data: bytes) -> bytes:
    """Read the data tar from a Debian ar container without extracting host paths."""
    if not data.startswith(b'!<arch>\n'):
        raise ValueError('Invalid Debian ar archive')
    position = 8
    result = None
    while position < len(data):
        header = data[position:position + 60]
        if len(header) != 60 or header[58:60] != b'`\n':
            raise ValueError('Invalid ar member header')
        length = int(header[48:58].strip())
        if length < 0 or position + 60 + length > len(data):
            raise ValueError('Invalid ar member size')
        name = header[:16].decode('ascii').strip().rstrip('/')
        body = data[position + 60:position + 60 + length]
        position += 60 + length + length % 2
        if not name.startswith('data.tar'):
            continue
        if result is not None:
            raise ValueError('Duplicate Debian data archives')
        if name.endswith(('.zst', '.zstd')):
            try:
                from compression import zstd
                body = zstd.decompress(body)
            except ImportError:
                executable = shutil.which('zstd')
                if not executable:
                    raise RuntimeError('This .deb requires Python 3.14+ or the zstd executable')
                body = subprocess.run([executable, '--decompress', '--stdout'], input=body,
                                      stdout=subprocess.PIPE, check=True, timeout=60).stdout
        result = body
    if result is None:
        raise ValueError('Debian package has no data archive')
    return result

def preinstall_proot_package(path: Path, files: dict, modes: dict, symlinks: dict) -> None:
    """Stage only PRoot's early loader dependencies; dpkg still installs each full package later."""
    with tarfile.open(fileobj=io.BytesIO(deb_data_archive(path.read_bytes())), mode='r:*') as archive:
        hardlinks = []
        for member in archive:
            parts = PurePosixPath(safe_name(member.name)).parts
            if member.isdir() and (parts == ('.',) or PREFIX_PARTS[:len(parts)] == parts):
                continue
            if parts[:len(PREFIX_PARTS)] != PREFIX_PARTS:
                raise ValueError(f'PRoot package writes outside the runtime prefix: {member.name}')
            relative = PurePosixPath(*parts[len(PREFIX_PARTS):]).as_posix()
            if relative.startswith(('var/lib/dpkg/', 'etc/apt/')):
                raise ValueError(f'PRoot preinstall must not replace package-manager state: {relative}')
            if member.isdir():
                continue
            if member.issym():
                target = member.linkname
                if target.startswith('/'):
                    if not target.startswith('/' + '/'.join(PREFIX_PARTS) + '/') or '..' in PurePosixPath(target).parts:
                        raise ValueError(f'Unexpected absolute symlink: {target}')
                else:
                    # Reject relative escapes while retaining legitimate ../library links.
                    depth = len(PurePosixPath(relative).parent.parts)
                    for part in PurePosixPath(target).parts:
                        depth += -1 if part == '..' else 0 if part == '.' else 1
                        if depth < 0:
                            raise ValueError(f'PRoot symlink escapes runtime prefix: {target}')
                files.pop(relative, None)
                modes.pop(relative, None)
                symlinks[relative] = target
            elif member.islnk():
                target_parts = PurePosixPath(safe_name(member.linkname)).parts
                if target_parts[:len(PREFIX_PARTS)] != PREFIX_PARTS:
                    raise ValueError('PRoot hardlink escapes runtime prefix')
                hardlinks.append((relative, PurePosixPath(*target_parts[len(PREFIX_PARTS):]).as_posix(), member.mode))
            elif member.isfile():
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f'Missing file data: {member.name}')
                files[relative] = source.read()
                modes[relative] = member.mode & 0o777
                symlinks.pop(relative, None)
            else:
                raise ValueError(f'Unsupported PRoot archive entry: {member.name}')
        for relative, target, mode in hardlinks:
            if target not in files:
                raise ValueError(f'Unresolved PRoot hardlink: {target}')
            files[relative] = files[target]
            modes[relative] = mode & 0o777
            symlinks.pop(relative, None)

def build_payload(upstream: Path, lock: dict, runtime: dict):
    bootstrap = runtime['bootstrap']
    architecture = runtime['architecture']
    base = download(bootstrap['url'], CACHE / f'bootstrap-{architecture}.zip', bootstrap['sha256'])
    packages = []
    early_packages = []
    for p in runtime['packages']:
        path = download(runtime['repository'] + '/' + p['Filename'], CACHE / 'debs' / Path(p['Filename']).name.replace(':', '_'), p['SHA256'])
        packages.append(path)
        if p['Package'] in PROOT_BOOTSTRAP_PACKAGES:
            early_packages.append(path)
    files = {}
    modes = {}
    symlinks = {}
    with zipfile.ZipFile(base) as archive:
        for entry in archive.infolist():
            name = safe_name(entry.filename)
            if entry.is_dir():
                continue
            data = archive.read(entry)
            if name == 'SYMLINKS.txt':
                for line in data.decode().splitlines():
                    target, link = line.split('←', 1)
                    symlinks[safe_name(link)] = target
                continue
            files[name] = data
            mode = (entry.external_attr >> 16) & 0o777
            modes[name] = mode or (0o755 if name.startswith(('bin/', 'libexec/', 'lib/apt/')) else 0o644)
    if len(early_packages) != len(PROOT_BOOTSTRAP_PACKAGES):
        raise RuntimeError('Runtime lock must include proot, libtalloc, and libandroid-shmem')
    for path in early_packages:
        preinstall_proot_package(path, files, modes, symlinks)
    # Copy original source bytes. No rewriting/transpilation/patching is permitted here.
    excluded = {'.git', 'node_modules', 'docs-site', 'docs', 'test', '.github'}
    for basepath, dirs, names in os.walk(upstream):
        rel = Path(basepath).relative_to(upstream)
        if rel == Path('.'):
            dirs[:] = [d for d in dirs if d not in excluded or d == 'node_modules']
        # npm.cmd/.bin generated on Windows contain nonportable paths. Link them from package metadata below.
        dirs[:] = [d for d in dirs if d not in ('.git', '.bin')]
        # os.walk does not traverse directory symlinks; preserve them explicitly on Unix hosts.
        for name in list(dirs):
            src = Path(basepath) / name
            if src.is_symlink():
                dest = 'share/pi-pocket/' + src.relative_to(upstream).as_posix()
                symlinks[dest] = os.readlink(src).replace('\\', '/')
                dirs.remove(name)
        for name in names:
            if name == '.pocket-source-commit':
                continue
            src = Path(basepath) / name
            dest = 'share/pi-pocket/' + src.relative_to(upstream).as_posix()
            if src.is_symlink():
                symlinks[dest] = os.readlink(src).replace('\\', '/')
                continue
            files[dest] = src.read_bytes()
            modes[dest] = 0o644
    for package_json in (upstream / 'node_modules').rglob('package.json'):
        try:
            package = json.loads(package_json.read_text(encoding='utf-8'))
        except (ValueError, UnicodeDecodeError):
            continue
        bins = package.get('bin', {})
        if isinstance(bins, str):
            bins = {package['name'].split('/')[-1]: bins}
        parent = package_json.parent
        module_parent = parent.parent.parent if parent.parent.name.startswith('@') else parent.parent
        if module_parent.name != 'node_modules':
            continue
        for command, relative in bins.items():
            executable = (parent / relative).resolve()
            if not executable.is_relative_to(upstream.resolve()) or not executable.exists():
                continue
            dst = 'share/pi-pocket/' + executable.relative_to(upstream.resolve()).as_posix()
            modes[dst] = 0o755
            link = 'share/pi-pocket/' + (module_parent / '.bin' / command).relative_to(upstream).as_posix()
            symlinks[link] = os.path.relpath(executable, module_parent / '.bin').replace('\\', '/')
    for path in packages:
        files['share/runtime-packages/' + path.name] = path.read_bytes()
        modes['share/runtime-packages/' + path.name] = 0o644
    for name in ('setup-runtime.sh', 'launch-pocket.sh'):
        src = ROOT / 'scripts' / name
        if src.exists():
            dest = 'share/pocket-host/' + name
            files[dest] = src.read_bytes().replace(b'\r\n', b'\n')
            modes[dest] = 0o755
    lock_filename = runtime_lock_filename(architecture)
    files['share/pocket-host/runtime.lock.json'] = (ROOT / lock_filename).read_bytes()
    files['share/pocket-host/upstream.lock.json'] = (ROOT / 'upstream.lock.json').read_bytes()
    files['SYMLINKS.txt'] = ''.join(f'{target}←{link}\n' for link, target in sorted(symlinks.items())).encode()
    files['permissions.json'] = json.dumps(modes, sort_keys=True).encode()
    ASSETS.mkdir(parents=True, exist_ok=True)
    output = ASSETS / 'runtime.zip'
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(safe_name(name), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100000 | modes.get(name, 0o644)) << 16
            archive.writestr(info, data)
    payload_hash = digest(output.read_bytes())
    manifest = {'schemaVersion': 1, 'architecture': ARCHITECTURES[architecture], 'runtimeLock': lock_filename,
                'runtimeVersion': lock['commit'][:12] + '-' + payload_hash[:12],
                'piPocketVersion': lock['version'], 'piPocketCommit': lock['commit'], 'sha256': payload_hash}
    (ASSETS / 'payload.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'Offline payload ready: {output.stat().st_size / 1024 / 1024:.1f} MiB; {len(files)} files', flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Read committed source from a local pi-pocket checkout')
    parser.add_argument('--update-runtime-lock', action='store_true', help='Explicitly refresh Android package versions')
    parser.add_argument('--arch', choices=ARCHITECTURES, default='aarch64', help='Android runtime architecture')
    args = parser.parse_args()
    CACHE.mkdir(exist_ok=True)
    lock = json.loads((ROOT / 'upstream.lock.json').read_text())
    runtime = runtime_lock(args.update_runtime_lock, args.arch)
    upstream = prepare_upstream(args.source, lock)
    build_payload(upstream, lock, runtime)

if __name__ == '__main__':
    main()
