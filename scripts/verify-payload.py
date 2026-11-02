"""Check offline payload integrity and byte-for-byte upstream source preservation."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'app/src/main/assets'
payload = json.loads((ASSETS / 'payload.json').read_text())
lock_name = payload.get('runtimeLock', 'runtime.lock.json')
assert lock_name in ('runtime.lock.json', 'runtime-x86_64.lock.json')
runtime = json.loads((ROOT / lock_name).read_text())
upstream = json.loads((ROOT / 'upstream.lock.json').read_text())
assert payload['piPocketCommit'] == upstream['commit']
assert payload['piPocketVersion'] == upstream['version']
archive_path = ASSETS / 'runtime.zip'
assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == payload['sha256']
with zipfile.ZipFile(archive_path) as archive:
    names = archive.namelist()
    assert len(names) == len(set(names)), 'Duplicate payload entries'
    for name in names:
        path = PurePosixPath(name)
        assert not path.is_absolute() and '..' not in path.parts and '\\' not in name, name
    permissions = json.loads(archive.read('permissions.json'))
    for name, mode in permissions.items():
        assert name in names and isinstance(mode, int) and 0 <= mode <= 0o777
    links = {}
    for line in archive.read('SYMLINKS.txt').decode().splitlines():
        target, link = line.split('←')
        assert link not in names and link not in links, link
        assert '..' not in PurePosixPath(link).parts and not link.startswith('/')
        links[link] = target
    for package in runtime['packages']:
        filename = PurePosixPath(package['Filename']).name.replace(':', '_')
        data = archive.read('share/runtime-packages/' + filename)
        assert hashlib.sha256(data).hexdigest() == package['SHA256'], filename
    assert 'nodejs-lts' in {p['Package'] for p in runtime['packages']}
    assert 'nodejs' not in {p['Package'] for p in runtime['packages']}
    assert {'proot', 'libtalloc', 'libandroid-shmem'}.issubset({p['Package'] for p in runtime['packages']})
    checked = 0
    source = ROOT / '.cache/upstream'
    for directory in ('src', 'web', 'bin'):
        for path in (source / directory).rglob('*'):
            if path.is_file():
                relative = path.relative_to(source).as_posix()
                assert archive.read('share/pi-pocket/' + relative) == path.read_bytes(), relative
                checked += 1
    for filename in ('package.json', 'package-lock.json', 'LICENSE'):
        assert archive.read('share/pi-pocket/' + filename) == (source / filename).read_bytes()
    for executable in ('bin/bash', 'bin/dpkg', 'bin/proot', 'libexec/proot/loader', 'share/pocket-host/setup-runtime.sh', 'share/pocket-host/launch-pocket.sh'):
        assert permissions[executable] & 0o100, executable
print(f'PASS: {len(names)} archive files, {len(runtime["packages"])} verified Android packages, '
      f'{checked} unchanged upstream source/frontend/entry files, {len(links)} safe link paths')
