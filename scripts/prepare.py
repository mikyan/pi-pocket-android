"""Build an offline ARM64 payload without modifying pi-pocket source (Python 3.11+)."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
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
PACKAGES = ['nodejs-lts', 'npm', 'git', 'bash', 'coreutils', 'findutils', 'grep', 'sed',
            'tar', 'ripgrep', 'fd', 'termux-exec', 'ca-certificates', 'curl', 'diffutils', 'which', 'util-linux']

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def download(url: str, target: Path, sha256: str | None = None) -> Path:
    if target.exists() and sha256 and digest(target.read_bytes()) == sha256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    print('Download', url, flush=True)
    temp = target.with_suffix(target.suffix + '.part')
    if os.name == 'nt':
        subprocess.run(['powershell', '-NoProfile', '-File', str(ROOT / 'scripts/download.ps1'),
                        '-Url', url, '-Destination', str(temp)], check=True)
        data = temp.read_bytes()
    else:
        request = urllib.request.Request(url, headers={'User-Agent': 'PiPocketAndroidBuild/0.1'})
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read()
    if sha256 and digest(data) != sha256:
        raise RuntimeError(f'Checksum mismatch: {url}')
    temp.write_bytes(data)
    temp.replace(target)
    return target

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

def runtime_lock(update: bool) -> dict:
    path = ROOT / 'runtime.lock.json'
    if update:
        raw = download(REPO + '/dists/stable/main/binary-aarch64/Packages', CACHE / 'Packages').read_bytes()
        packages = resolve(parse_index(raw.decode()), PACKAGES)
        lock = {'schemaVersion': 1, 'architecture': 'aarch64', 'repository': REPO,
                'indexSha256': digest(raw), 'bootstrap': BOOTSTRAP, 'roots': PACKAGES, 'packages': packages}
        path.write_text(json.dumps(lock, indent=2) + '\n', encoding='utf-8')
    elif not path.exists():
        raise RuntimeError('Missing runtime.lock.json; first run requires --update-runtime-lock')
    return json.loads(path.read_text(encoding='utf-8'))

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
    package = json.loads((target / 'package.json').read_text())
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

def build_payload(upstream: Path, lock: dict, runtime: dict):
    bootstrap = runtime['bootstrap']
    base = download(bootstrap['url'], CACHE / 'bootstrap.zip', bootstrap['sha256'])
    packages = []
    for p in runtime['packages']:
        path = download(runtime['repository'] + '/' + p['Filename'], CACHE / 'debs' / Path(p['Filename']).name.replace(':', '_'), p['SHA256'])
        packages.append(path)
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
    # Copy original source bytes. No rewriting/transpilation/patching is permitted here.
    excluded = {'.git', 'node_modules', 'docs-site', 'docs', 'test', '.github'}
    for basepath, dirs, names in os.walk(upstream):
        rel = Path(basepath).relative_to(upstream)
        if rel == Path('.'):
            dirs[:] = [d for d in dirs if d not in excluded or d == 'node_modules']
        # npm.cmd/.bin generated on Windows contain nonportable paths. Link them from package metadata below.
        dirs[:] = [d for d in dirs if d not in ('.git', '.bin')]
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
    files['share/pocket-host/runtime.lock.json'] = (ROOT / 'runtime.lock.json').read_bytes()
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
    manifest = {'schemaVersion': 1, 'architecture': 'arm64-v8a',
                'runtimeVersion': lock['commit'][:12] + '-' + payload_hash[:12],
                'piPocketVersion': lock['version'], 'piPocketCommit': lock['commit'], 'sha256': payload_hash}
    (ASSETS / 'payload.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'Offline payload ready: {output.stat().st_size / 1024 / 1024:.1f} MiB; {len(files)} files', flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Read committed source from a local pi-pocket checkout')
    parser.add_argument('--update-runtime-lock', action='store_true', help='Explicitly refresh Android package versions')
    args = parser.parse_args()
    CACHE.mkdir(exist_ok=True)
    lock = json.loads((ROOT / 'upstream.lock.json').read_text())
    runtime = runtime_lock(args.update_runtime_lock)
    upstream = prepare_upstream(args.source, lock)
    build_payload(upstream, lock, runtime)

if __name__ == '__main__':
    main()
