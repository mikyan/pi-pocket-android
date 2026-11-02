"""Package unchanged pi-pocket source and portable JS dependencies for installed Termux."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / '.cache'
ASSETS = ROOT / 'app/src/main/assets'
HOST_SCRIPTS = ('setup-runtime.sh', 'launch-pocket.sh')
REPOSITORY = 'https://github.com/TannerMidd/pi-pocket.git'
DESKTOP_TUI_HELPER = re.compile(r'(?:^|/)node_modules/@earendil-works/pi-tui/native/(?:darwin|win32|linux)/prebuilds/[^/]+/[^/]+\.node$')


def file_digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def runtime_version(commit: str, archive_sha256: str, script_hashes: dict[str, str]) -> str:
    """Canonical identity includes host scripts so packaging-only fixes reinstall."""
    identity = json.dumps({'archiveSha256': archive_sha256, 'hostScripts': script_hashes},
                          sort_keys=True, separators=(',', ':')).encode('utf-8')
    return commit[:12] + '-' + hashlib.sha256(identity).hexdigest()[:12]


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or str(path) == '.':
        raise ValueError(f'Unsafe archive path {name}')
    return str(path)


def safe_link(name: str, target: str) -> str:
    safe_name(name)
    if not target or target.startswith('/') or '\\' in target or ':' in target:
        raise ValueError(f'Unsafe symbolic link: {name}')
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    safe_name(resolved)
    return resolved


def validate_links(names: set[str], links: dict[str, str]):
    for name, target in links.items():
        resolved = safe_link(name, target)
        seen = {name}
        while resolved in links:
            if resolved in seen:
                raise ValueError(f'Symbolic link cycle: {name}')
            seen.add(resolved)
            resolved = safe_link(resolved, links[resolved])
        if resolved not in names and not any(item.startswith(resolved + '/') for item in names):
            raise ValueError(f'Missing symbolic link target: {name}')
    for name in names | set(links):
        if any(str(parent) in links for parent in PurePosixPath(name).parents):
            raise ValueError(f'Archive entry is nested below a symbolic link: {name}')


def run(args, cwd=None):
    print('+', ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)


def validate_lock(lock):
    if lock.get('schemaVersion') != 1 or lock.get('repository') != REPOSITORY:
        raise ValueError('Unsupported upstream lock')
    if not re.fullmatch(r'[0-9a-f]{40}', lock.get('commit', '')):
        raise ValueError('Invalid upstream commit')
    for field in ('version', 'minimumNode'):
        if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', lock.get(field, '')):
            raise ValueError('Invalid upstream ' + field)


def validate_source(upstream: Path, archive_path: Path, lock: dict) -> int:
    """Check staging against the immutable git archive, including all source bytes."""
    count = 0
    with tarfile.open(archive_path) as archive:
        for member in archive:
            name = safe_name(member.name)
            target = upstream / name
            if member.isdir():
                continue
            if member.issym():
                safe_link(name, member.linkname)
                if not target.is_symlink() or os.readlink(target).replace('\\', '/') != member.linkname:
                    raise ValueError('Upstream symbolic link changed: ' + name)
            elif member.isfile():
                with archive.extractfile(member) as expected, target.open('rb') as actual:
                    if hashlib.file_digest(expected, 'sha256').digest() != hashlib.file_digest(actual, 'sha256').digest():
                        raise ValueError('Upstream source changed: ' + name)
            else:
                raise ValueError('Unsupported upstream archive entry: ' + name)
            count += 1
    package = json.loads((upstream / 'package.json').read_text(encoding='utf-8'))
    package_lock = json.loads((upstream / 'package-lock.json').read_text(encoding='utf-8'))
    if any(item.get('version') != lock['version'] for item in (package, package_lock, package_lock.get('packages', {}).get('', {}))):
        raise ValueError('Pinned source package versions disagree with upstream.lock.json')
    if package.get('bin', {}).get('pi-pocket') != 'bin/pi-pocket.js':
        raise ValueError('Upstream launcher contract changed')
    return count


def prepare_upstream(source: Path | None, lock: dict) -> Path:
    validate_lock(lock)
    target = CACHE / 'upstream'
    if source is not None and source.resolve().is_relative_to(target.resolve()):
        raise ValueError('--source must not point inside the disposable upstream staging directory')
    archive = CACHE / f'upstream-{lock["commit"]}.tar'
    stamp = target / '.pocket-source-commit'
    if stamp.exists() and stamp.read_text().strip() == lock['commit'] and (target / 'node_modules').exists() and archive.exists():
        validate_source(target, archive, lock)
        return target
    if target.exists():
        if target.is_symlink() or target.resolve() != (ROOT / '.cache/upstream').resolve():
            raise RuntimeError('Unexpected staging path')
        shutil.rmtree(target)
    target.mkdir(parents=True)
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
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not npm:
        raise RuntimeError('Node.js and npm are required on the build host')
    run([npm, 'ci', '--omit=dev', '--omit=optional', '--ignore-scripts', '--no-audit', '--no-fund'], target)
    validate_source(target, archive, lock)
    stamp.write_text(lock['commit'], encoding='utf-8')
    return target


def excluded_dependency_files(upstream: Path):
    return [{'path': path.relative_to(upstream).as_posix(), 'sha256': file_digest(path),
             'reason': 'desktop-only-pi-tui-helper'}
            for path in sorted((upstream / 'node_modules').rglob('*.node'))
            if DESKTOP_TUI_HELPER.search(path.relative_to(upstream).as_posix())]


def portable_entries(upstream: Path):
    upstream = upstream.resolve()
    files, modes, links = {}, {}, {}
    for directory, dirs, names in os.walk(upstream):
        # Discard npm's host-specific .bin shims on every OS, then reconstruct.
        dirs[:] = [name for name in dirs if name not in ('.git', '.bin')]
        for name in list(dirs):
            source = Path(directory) / name
            if source.is_symlink():
                relative = safe_name(source.relative_to(upstream).as_posix())
                links[relative] = os.readlink(source).replace('\\', '/')
                dirs.remove(name)
        for name in names:
            if name == '.pocket-source-commit':
                continue
            source = Path(directory) / name
            relative = safe_name(source.relative_to(upstream).as_posix())
            if source.is_symlink():
                links[relative] = os.readlink(source).replace('\\', '/')
                continue
            if not source.is_file():
                raise ValueError('Unsupported source file: ' + relative)
            if relative.startswith('node_modules/'):
                # Pi's optional terminal/clipboard helpers only load on desktop
                # darwin/win32/linux, never Android. Keep JS unmodified and omit
                # those bundled desktop prebuilds from the universal package.
                if DESKTOP_TUI_HELPER.search(relative):
                    continue
                with source.open('rb') as stream:
                    magic = stream.read(4)
                # quickjs-wasi calls its portable WASM modules *.so.
                if magic != b'\0asm' and (source.suffix.lower() in ('.node', '.exe', '.dll', '.so', '.dylib') or magic in (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf') or magic[:2] == b'MZ'):
                    raise ValueError('Architecture-specific dependency cannot enter universal payload: ' + relative)
            files[relative] = source
            modes[relative] = 0o755 if source.stat().st_mode & 0o111 else 0o644
    for package_json in sorted((upstream / 'node_modules').rglob('package.json')):
        parent = package_json.parent
        module_parent = parent.parent.parent if parent.parent.name.startswith('@') else parent.parent
        if module_parent.name != 'node_modules':
            continue
        package = json.loads(package_json.read_text(encoding='utf-8'))
        bins = package.get('bin', {})
        if isinstance(bins, str):
            bins = {package['name'].split('/')[-1]: bins}
        if not isinstance(bins, dict):
            raise ValueError('Invalid package bin metadata')
        for command, relative in sorted(bins.items()):
            if not isinstance(command, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', command):
                raise ValueError('Unsafe npm bin command')
            if not isinstance(relative, str):
                raise ValueError('Unsafe npm bin target')
            safe_name(relative)
            executable = (parent / relative).resolve()
            if not executable.is_relative_to(parent.resolve()) or not executable.is_file():
                raise ValueError('npm bin points outside its package or is missing')
            destination = executable.relative_to(upstream).as_posix()
            if destination not in files:
                raise ValueError('npm bin is absent from portable payload')
            modes[destination] = 0o755
            link = (module_parent / '.bin' / command).relative_to(upstream).as_posix()
            target = os.path.relpath(executable, module_parent / '.bin').replace('\\', '/')
            if link in links and links[link] != target:
                raise ValueError('Conflicting npm bin commands: ' + link)
            links[link] = target
    validate_links(set(files), links)
    modes['bin/pi-pocket.js'] = 0o755
    return files, modes, links


def build_payload(upstream: Path, lock: dict):
    validate_lock(lock)
    files, modes, links = portable_entries(upstream)
    ASSETS.mkdir(parents=True, exist_ok=True)
    # AAPT treats *.gz as a pre-compressed input and strips that suffix. *.tgz
    # retains the exact gzip bytes and filename inside the APK.
    output = ASSETS / 'pocket.tgz'
    temporary = output.with_name(output.name + '.part')
    with temporary.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
            for name in sorted(set(files) | set(links)):
                entry = tarfile.TarInfo(name)
                entry.mtime, entry.uid, entry.gid = 0, 0, 0
                entry.uname = entry.gname = ''
                if name in links:
                    entry.type, entry.linkname, entry.mode = tarfile.SYMTYPE, links[name], 0o777
                    archive.addfile(entry)
                else:
                    entry.size, entry.mode = files[name].stat().st_size, modes[name]
                    with files[name].open('rb') as content:
                        archive.addfile(entry, content)
    temporary.replace(output)
    manifest = write_payload_metadata(upstream, lock, output)
    print(f'Portable Termux payload: {output.stat().st_size / 1024 / 1024:.1f} MiB; {len(files)} files, {len(links)} symlinks', flush=True)
    return manifest


def write_payload_metadata(upstream: Path, lock: dict, output: Path):
    """Bind the generated upstream archive and this host's launch scripts."""
    payload_hash = file_digest(output)
    script_hashes = {}
    for name in HOST_SCRIPTS:
        content = (ROOT / 'scripts' / name).read_bytes().replace(b'\r\n', b'\n')
        (ASSETS / name).write_bytes(content)
        script_hashes[name] = hashlib.sha256(content).hexdigest()
    manifest = {'schemaVersion': 2, 'runtime': 'termux', 'archive': output.name,
                'runtimeVersion': runtime_version(lock['commit'], payload_hash, script_hashes),
                'piPocketVersion': lock['version'], 'piPocketCommit': lock['commit'],
                'minimumNode': lock['minimumNode'], 'sha256': payload_hash,
                'hostScripts': script_hashes,
                'excludedDependencyFiles': excluded_dependency_files(upstream)}
    (ASSETS / 'payload.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8', newline='\n')
    for obsolete in ('runtime.zip', 'pocket.tar.gz', 'permissions.json', 'SYMLINKS.txt'):
        (ASSETS / obsolete).unlink(missing_ok=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Read committed source from a local pi-pocket checkout')
    args = parser.parse_args()
    CACHE.mkdir(exist_ok=True)
    lock = json.loads((ROOT / 'upstream.lock.json').read_text(encoding='utf-8'))
    validate_lock(lock)
    node = subprocess.check_output(['node', '--version'], text=True).strip().removeprefix('v')
    if tuple(map(int, node.split('.'))) < tuple(map(int, lock['minimumNode'].split('.'))):
        raise RuntimeError('Build Node.js is older than upstream minimumNode')
    upstream = prepare_upstream(args.source, lock)
    build_payload(upstream, lock)


if __name__ == '__main__':
    main()
