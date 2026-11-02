import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('prepare', Path(__file__).parents[1] / 'scripts/prepare.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
verify_spec = importlib.util.spec_from_file_location('verify_payload', Path(__file__).parents[1] / 'scripts/verify-payload.py')
verify = importlib.util.module_from_spec(verify_spec)
with patch.dict(sys.modules, {'prepare': prepare}):
    verify_spec.loader.exec_module(verify)


class PortablePackaging(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / '.cache/upstream'
        self.assets = self.root / 'app/src/main/assets'
        self.lock = {'schemaVersion': 1, 'repository': prepare.REPOSITORY, 'commit': 'a' * 40,
                     'version': '0.27.0', 'minimumNode': '22.19.0'}
        self.write(self.root / 'upstream.lock.json', json.dumps(self.lock).encode())
        package = {'name': 'pi-pocket', 'version': '0.27.0', 'bin': {'pi-pocket': 'bin/pi-pocket.js'}}
        self.write(self.source / 'package.json', json.dumps(package).encode())
        self.write(self.source / 'package-lock.json', json.dumps({'version': '0.27.0', 'packages': {'': package}}).encode())
        self.write(self.source / 'bin/pi-pocket.js', b'#!/usr/bin/env node\n// original launcher\n')
        self.write(self.source / 'src/server.ts', b'// exact upstream bytes\r\n')
        self.write(self.source / 'web/main.js', b'export const upstream = true;\n')
        self.write(self.source / 'LICENSE', b'license text\n')
        self.archive = self.root / '.cache' / ('upstream-' + self.lock['commit'] + '.tar')
        with tarfile.open(self.archive, 'w') as archive:
            for path in sorted(self.source.rglob('*')):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(self.source).as_posix(), recursive=False)
        for name in prepare.HOST_SCRIPTS:
            self.write(self.root / 'scripts' / name, b'#!/bin/bash\r\necho fixture\r\n')
        self.write(self.source / 'node_modules/example/package.json', b'{"name":"example","bin":{"example":"cli.js"}}')
        self.write(self.source / 'node_modules/example/cli.js', b'#!/usr/bin/env node\n')
        for shim in ('example', 'example.cmd', 'example.ps1'):
            self.write(self.source / 'node_modules/.bin' / shim, b'host-specific npm shim')

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def build(self):
        with patch.object(prepare, 'ROOT', self.root), patch.object(prepare, 'ASSETS', self.assets):
            return prepare.build_payload(self.source, self.lock)

    def test_archive_rebuilds_posix_bins_and_preserves_upstream_bytes(self):
        payload = self.build()
        self.assertEqual(payload['schemaVersion'], 2)
        self.assertEqual(payload['runtime'], 'termux')
        self.assertEqual(payload['minimumNode'], self.lock['minimumNode'])
        self.assertNotIn('architecture', payload)
        with tarfile.open(self.assets / 'pocket.tgz') as archive:
            self.assertEqual(archive.extractfile('src/server.ts').read(), b'// exact upstream bytes\r\n')
            entry = archive.getmember('node_modules/.bin/example')
            self.assertTrue(entry.issym())
            self.assertEqual(entry.linkname, '../example/cli.js')
            self.assertEqual(archive.getmember('node_modules/example/cli.js').mode, 0o755)
            self.assertFalse(any(name.endswith(('.cmd', '.ps1')) for name in archive.getnames()))
            self.assertFalse(any(name.startswith('share/runtime-packages/') for name in archive.getnames()))
        verify.verify(self.root)

    def test_payload_is_repeatable_and_scripts_are_separate_assets(self):
        first = self.build()
        second = self.build()
        self.assertEqual(first, second)
        self.assertEqual((self.assets / 'setup-runtime.sh').read_bytes(), b'#!/bin/bash\necho fixture\n')
        self.assertEqual(first['hostScripts']['setup-runtime.sh'], prepare.file_digest(self.assets / 'setup-runtime.sh'))

    def test_host_only_fix_changes_runtime_identity_without_changing_upstream_archive(self):
        original = self.build()
        self.write(self.root / 'scripts/launch-pocket.sh', b'#!/bin/bash\necho corrected-launcher\n')
        fixed = self.build()
        self.assertEqual(original['sha256'], fixed['sha256'])
        self.assertNotEqual(original['runtimeVersion'], fixed['runtimeVersion'])
        self.assertEqual(fixed['runtimeVersion'], prepare.runtime_version(self.lock['commit'], fixed['sha256'], fixed['hostScripts']))
        verify.verify(self.root)

    def test_runtime_identity_is_independent_of_script_dictionary_order(self):
        scripts = {'setup-runtime.sh': 'b' * 64, 'launch-pocket.sh': 'c' * 64}
        reversed_scripts = dict(reversed(list(scripts.items())))
        self.assertEqual(prepare.runtime_version('a' * 40, 'd' * 64, scripts),
                         prepare.runtime_version('a' * 40, 'd' * 64, reversed_scripts))

    def test_source_tampering_is_detected_against_git_archive(self):
        prepare.validate_source(self.source, self.archive, self.lock)
        (self.source / 'src/server.ts').write_bytes(b'modified')
        with self.assertRaisesRegex(ValueError, 'Upstream source changed'):
            prepare.validate_source(self.source, self.archive, self.lock)

    def test_modified_tar_fails_integrity_check(self):
        self.build()
        with (self.assets / 'pocket.tgz').open('ab') as output:
            output.write(b'unexpected')
        with self.assertRaises(AssertionError):
            verify.verify(self.root)

    def test_native_dependencies_are_rejected(self):
        for name, content in (('binary.node', b'fake'), ('tool', b'\x7fELFfake'), ('tool.exe', b'MZfake')):
            with self.subTest(name=name):
                path = self.source / 'node_modules/example' / name
                path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, 'Architecture-specific'):
                    prepare.portable_entries(self.source)
                path.unlink()

    def test_wasm_shared_modules_remain_and_optional_desktop_helpers_are_omitted(self):
        self.write(self.source / 'node_modules/quickjs-wasi/extensions/url/url.so', b'\0asmfixture')
        desktop = self.source / 'node_modules/@earendil-works/pi-tui/native/linux/prebuilds/linux-x64/linux-platform-x11.node'
        self.write(desktop, b'\x7fELFdesktop-only')
        files, _, _ = prepare.portable_entries(self.source)
        self.assertIn('node_modules/quickjs-wasi/extensions/url/url.so', files)
        self.assertNotIn(desktop.relative_to(self.source).as_posix(), files)
        self.assertTrue(desktop.exists(), 'Packaging must not rewrite the dependency checkout')
        payload = self.build()
        self.assertEqual(payload['excludedDependencyFiles'], [{
            'path': desktop.relative_to(self.source).as_posix(), 'sha256': prepare.file_digest(desktop),
            'reason': 'desktop-only-pi-tui-helper'}])

    def test_npm_bin_metadata_cannot_escape_package_or_bin_directory(self):
        manifest = self.source / 'node_modules/example/package.json'
        for bins in ({'../escape': 'cli.js'}, {'tool': '../outside.js'}, {'tool': '/etc/passwd'}, {'tool': 'missing.js'}):
            with self.subTest(bins=bins):
                manifest.write_text(json.dumps({'name': 'example', 'bin': bins}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    prepare.portable_entries(self.source)

    def test_safe_links_allow_bin_relative_target_but_reject_traversal_and_cycles(self):
        prepare.validate_links({'node_modules/example/cli.js'}, {'node_modules/.bin/example': '../example/cli.js'})
        for links in ({'bin/x': '../../outside'}, {'bin/x': '/etc/passwd'}, {'bin/x': 'missing'},
                      {'x': 'y', 'y': 'x'}, {'nested': 'real'}):
            with self.subTest(links=links), self.assertRaises(ValueError):
                prepare.validate_links({'real/file', 'nested/file'}, links)

    def test_reject_unsafe_archive_names(self):
        for name in ('../home/auth.json', '/data/data/other', 'x/../../escape', 'x\\escape', 'C:/file', '.', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                prepare.safe_name(name)

    def test_old_embedded_payload_is_removed(self):
        self.write(self.assets / 'runtime.zip', b'obsolete')
        self.write(self.assets / 'pocket.tar.gz', b'obsolete gzip filename')
        self.build()
        self.assertFalse((self.assets / 'runtime.zip').exists())
        self.assertFalse((self.assets / 'pocket.tar.gz').exists())


if __name__ == '__main__':
    unittest.main()
