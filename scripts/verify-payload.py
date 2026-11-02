"""Verify portable Termux payload safety, dependency links, and unchanged upstream bytes."""
import hashlib
import json
from pathlib import Path
import tarfile

from prepare import HOST_SCRIPTS, excluded_dependency_files, file_digest, portable_entries, runtime_version, safe_name, validate_links, validate_source

ROOT = Path(__file__).resolve().parent.parent


def verify(root=ROOT):
    assets = root / 'app/src/main/assets'
    payload = json.loads((assets / 'payload.json').read_text(encoding='utf-8'))
    lock = json.loads((root / 'upstream.lock.json').read_text(encoding='utf-8'))
    assert payload['schemaVersion'] == 2 and payload['runtime'] == 'termux'
    assert payload['archive'] == 'pocket.tgz' and 'architecture' not in payload
    assert payload['piPocketCommit'] == lock['commit'] and payload['piPocketVersion'] == lock['version']
    assert payload['minimumNode'] == lock['minimumNode']
    archive_path = assets / payload['archive']
    assert file_digest(archive_path) == payload['sha256']
    assert not (assets / 'runtime.zip').exists(), 'Obsolete embedded runtime must not ship'
    source = root / '.cache/upstream'
    checked = validate_source(source, root / '.cache' / f'upstream-{lock["commit"]}.tar', lock)
    files, modes, expected_links = portable_entries(source)
    assert payload['excludedDependencyFiles'] == excluded_dependency_files(source)
    links = {}
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        names = [safe_name(member.name) for member in members]
        assert len(names) == len(set(names)), 'Duplicate payload entries'
        assert set(names) == set(files) | set(expected_links), 'Unexpected or omitted portable files'
        for member in members:
            assert not member.mode & 0o7000, 'Special permission bits are forbidden'
            if member.issym():
                links[member.name] = member.linkname
            else:
                assert member.isfile(), 'Only regular files and symlinks are supported'
                assert member.mode == modes[member.name]
                with archive.extractfile(member) as stream:
                    assert hashlib.file_digest(stream, 'sha256').hexdigest() == file_digest(files[member.name]), member.name
        assert links == expected_links
        validate_links(set(files), links)
    for name in HOST_SCRIPTS:
        expected = (root / 'scripts' / name).read_bytes().replace(b'\r\n', b'\n')
        assert (assets / name).read_bytes() == expected, name
        assert payload['hostScripts'][name] == hashlib.sha256(expected).hexdigest()
    assert payload['runtimeVersion'] == runtime_version(lock['commit'], payload['sha256'], payload['hostScripts']), 'Payload identity must bind archive and host scripts'
    print(f'PASS: {len(files)} portable files, {checked} unchanged upstream files, {len(links)} safe POSIX links; installed Termux required')


if __name__ == '__main__':
    verify()
