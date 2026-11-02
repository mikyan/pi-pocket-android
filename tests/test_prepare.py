import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).parents[1] / 'scripts/prepare.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

class PackagingBoundaries(unittest.TestCase):
    def test_reject_archive_traversal(self):
        for name in ('../home/auth.json', '/data/data/other', 'x/../../escape', 'x\\escape'):
            with self.assertRaises(ValueError):
                prepare.safe_name(name)

    def test_npm_does_not_pull_conflicting_node(self):
        def p(name, depends=''):
            return {'Package': name, 'Depends': depends, 'Version': '1', 'Architecture': 'aarch64', 'Filename': name, 'SHA256': '0' * 64}
        index = {'npm': p('npm', 'nodejs | nodejs-lts'), 'nodejs': p('nodejs'), 'nodejs-lts': p('nodejs-lts')}
        result = prepare.resolve(index, ['npm', 'nodejs-lts'])
        self.assertEqual({x['Package'] for x in result}, {'npm', 'nodejs-lts'})

    def test_unknown_dependency_fails_closed(self):
        with self.assertRaises(RuntimeError):
            prepare.resolve({'nodejs-lts': {'Depends': 'missing'}}, ['nodejs-lts'])

    def test_mirror_fallback_only_for_locked_official_termux_paths(self):
        url = 'https://packages-cf.termux.dev/apt/termux-main/pool/main/n/node.deb'
        self.assertEqual(prepare.download_urls(url, True), [url, url.replace('packages-cf.', 'packages.')])
        self.assertEqual(prepare.download_urls(url, False), [url])
        other = 'https://packages-cf.termux.dev.example.org/apt/termux-main/package.deb'
        self.assertEqual(prepare.download_urls(other, True), [other])
        with self.assertRaises(ValueError):
            prepare.download_urls('http://packages.termux.dev/apt/termux-main/package.deb', True)

    def test_download_fallback_verifies_bytes_and_renames_complete_file(self):
        payload = b'locked Android package bytes'
        url = 'https://packages-cf.termux.dev/apt/termux-main/pool/main/n/node.deb'
        attempts = []
        def fetch(candidate, output):
            attempts.append(candidate)
            if 'packages-cf.' in candidate:
                output.write_bytes(b'incomplete response')
                raise OSError('mirror unavailable')
            output.write_bytes(payload)
        with tempfile.TemporaryDirectory() as directory, patch.object(prepare, 'fetch_download', fetch), patch.object(prepare.time, 'sleep'):
            target = Path(directory) / 'node.deb'
            prepare.download(url, target, prepare.digest(payload))
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(len(attempts), 3)
            self.assertFalse(target.with_suffix('.deb.part').exists())

    def test_checksum_failure_never_replaces_existing_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'node.deb'
            target.write_bytes(b'previous cached bytes')
            def fetch(url, output):
                output.write_bytes(b'wrong bytes')
            with patch.object(prepare, 'fetch_download', fetch):
                with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                    prepare.download('https://example.org/node.deb', target, prepare.digest(b'expected bytes'))
            self.assertEqual(target.read_bytes(), b'previous cached bytes')
            self.assertFalse(target.with_suffix('.deb.part').exists())

    def write_deb(self, path, members):
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode='w:xz') as archive:
            for name, data, link in members:
                item = tarfile.TarInfo(name)
                item.mode = 0o755
                if link is not None:
                    item.type = tarfile.SYMTYPE
                    item.linkname = link
                    archive.addfile(item)
                else:
                    item.size = len(data)
                    archive.addfile(item, io.BytesIO(data))
        body = content.getvalue()
        header = f'{"data.tar.xz/":<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(body):<10}`\n'.encode()
        path.write_bytes(b'!<arch>\n' + header + body + (b'\n' if len(body) % 2 else b''))

    def test_preinstall_proot_files_permissions_and_library_links(self):
        with tempfile.TemporaryDirectory() as directory:
            deb = Path(directory) / 'proot.deb'
            self.write_deb(deb, [
                ('./data/data/com.termux/files/usr/bin/proot', b'ELF-test', None),
                ('./data/data/com.termux/files/usr/lib/libtalloc.so.2.5.0', b'library', None),
                ('./data/data/com.termux/files/usr/lib/libtalloc.so.2', b'', 'libtalloc.so.2.5.0'),
            ])
            files, modes, links = {}, {}, {}
            prepare.preinstall_proot_package(deb, files, modes, links)
            self.assertEqual(files['bin/proot'], b'ELF-test')
            self.assertEqual(modes['bin/proot'], 0o755)
            self.assertEqual(links['lib/libtalloc.so.2'], 'libtalloc.so.2.5.0')

    def test_preinstall_refuses_outside_prefix_and_escape_links(self):
        invalid = [
            ('../../outside', b'bad', None),
            ('./etc/passwd', b'bad', None),
            ('./data/data/com.termux/files/usr/bin/proot', b'', '../../../outside'),
            ('./data/data/com.termux/files/usr/lib/bad', b'', '/etc/passwd'),
            ('./data/data/com.termux/files/usr/var/lib/dpkg/status', b'bad', None),
        ]
        with tempfile.TemporaryDirectory() as directory:
            deb = Path(directory) / 'bad.deb'
            for member in invalid:
                with self.subTest(member=member):
                    self.write_deb(deb, [member])
                    with self.assertRaises(ValueError):
                        prepare.preinstall_proot_package(deb, {}, {}, {})

if __name__ == '__main__':
    unittest.main()
