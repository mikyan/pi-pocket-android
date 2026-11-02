import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('publish_release', Path(__file__).parents[1] / 'scripts/publish-release.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeGit:
    def __init__(self, base, target):
        self.head, self.base, self.target = base, base, target
        self.tag = None
        self.writes = []
        self.fail_push = False

    def validate(self, dist):
        pass

    def candidate_commit(self, dist, build):
        return self.target

    def main_head(self):
        return self.head

    def tag_target(self, tag):
        return self.tag

    def push_tag(self, tag, target):
        self.writes.append(('tag', tag, target))
        self.tag = target

    def advance_main(self, target):
        if self.fail_push:
            raise RuntimeError('non-fast-forward')
        self.writes.append(('main', target))
        self.head = target


class FakeAPI:
    def __init__(self):
        self.items = []
        self.files = {}
        self.writes = []
        self.fail_publish = False
        self.after_upload = None

    def releases(self):
        return copy.deepcopy(self.items)

    def assets(self, release):
        return [{'id': index, 'name': name, 'size': len(data), 'state': 'uploaded', 'digest': sha(data)}
                for index, (name, data) in enumerate(sorted(self.files.items()))]

    def asset_digest(self, asset):
        return asset['digest']

    def create_draft(self, tag, target, build):
        release = {'id': 1, 'tag_name': tag, 'target_commitish': target, 'draft': True,
                   'prerelease': False, 'body': publisher.release_body(build)}
        self.items.append(release)
        self.writes.append(('draft', tag))
        return copy.deepcopy(release)

    def upload(self, tag, paths):
        for path in paths:
            if path.name in self.files:
                raise AssertionError('attempted asset replacement')
            self.files[path.name] = path.read_bytes()
        self.writes.append(('upload', tuple(path.name for path in paths)))
        if self.after_upload:
            self.after_upload()

    def publish(self, release):
        if self.fail_publish:
            raise RuntimeError('simulated GitHub outage')
        self.items[0]['draft'] = False
        self.writes.append(('publish', release['id']))
        return copy.deepcopy(self.items[0])


class ReleaseSafety(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dist = Path(self.directory.name)
        self.environment = {'GITHUB_REPOSITORY': publisher.REPOSITORY, 'GITHUB_SHA': 'a' * 40,
                            'GITHUB_RUN_ID': '1234567', 'GITHUB_RUN_NUMBER': '3', 'GITHUB_RUN_ATTEMPT': '2'}
        self.lock = {'schemaVersion': 1, 'repository': publisher.UPSTREAM, 'commit': 'b' * 40,
                     'version': '0.27.0', 'minimumNode': '22.19.0'}
        self.write_json('upstream.lock.json', self.lock)
        self.write_json('termux-requirements.json', {'schemaVersion': 1, 'runtime': 'termux', 'packageName': 'com.termux'})
        runtime = b'the exact prepared Android runtime archive'
        self.payload = {'schemaVersion': 2, 'runtime': 'termux', 'archive': 'pocket.tgz', 'minimumNode': '22.19.0',
                        'piPocketCommit': self.lock['commit'], 'piPocketVersion': self.lock['version'],
                        'sha256': sha(runtime), 'hostScripts': {name: sha(b'fixture script') for name in publisher.HOST_SCRIPTS}}
        self.payload['runtimeVersion'] = publisher.runtime_version(self.lock['commit'], self.payload['sha256'], self.payload['hostScripts'])
        self.write_json('payload.json', self.payload)
        self.apk_name = 'pi-pocket-android-0.3.0-ci.3-universal-debug.apk'
        with zipfile.ZipFile(self.dist / self.apk_name, 'w') as archive:
            archive.writestr('assets/payload.json', (self.dist / 'payload.json').read_bytes())
            archive.writestr('assets/pocket.tgz', runtime)
            for name in publisher.HOST_SCRIPTS:
                archive.writestr('assets/' + name, b'fixture script')
        self.build = {'schemaVersion': 1, 'runtime': 'termux', 'baseSha': 'a' * 40, 'repository': publisher.REPOSITORY,
                      'runId': '1234567', 'runNumber': 3, 'runAttempt': 1, 'versionCode': 1003,
                      'versionName': '0.3.0-ci.3', 'upstreamCommit': self.lock['commit'],
                      'upstreamVersion': self.lock['version'],
                      'upstreamLockSha256': publisher.digest(self.dist / 'upstream.lock.json'),
                      'apk': {'name': self.apk_name, 'sha256': publisher.digest(self.dist / self.apk_name)}}
        self.environment['EXPECTED_UPSTREAM_LOCK_SHA256'] = self.build['upstreamLockSha256']
        self.write_json('BUILD.json', self.build)
        (self.dist / 'BUILD.txt').write_text('Build summary\n', encoding='utf-8')
        self.hashes = self.refresh_checksums()
        self.git = FakeGit(self.build['baseSha'], 'c' * 40)
        self.api = FakeAPI()

    def write_json(self, name, value):
        (self.dist / name).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')

    def refresh_checksums(self):
        names = publisher.FIXED_FILES | {self.apk_name}
        hashes = {name: publisher.digest(self.dist / name) for name in names}
        (self.dist / 'SHA256SUMS.txt').write_text(''.join(f'{hashes[name]}  {name}\n' for name in sorted(names)), encoding='utf-8')
        hashes['SHA256SUMS.txt'] = publisher.digest(self.dist / 'SHA256SUMS.txt')
        return hashes

    def validate(self):
        return publisher.validate_dist(self.dist, self.environment)

    def publish(self):
        build, hashes = self.validate()
        return publisher.publish(self.dist, build, hashes, self.git, self.api)

    def test_failed_job_rerun_accepts_the_original_build_attempt(self):
        build, hashes = self.validate()
        self.assertEqual(build['runAttempt'], 1)
        self.assertEqual(set(hashes), publisher.FIXED_FILES | {self.apk_name, 'SHA256SUMS.txt'})

    def test_candidate_must_match_the_independent_plan_output(self):
        self.environment['EXPECTED_UPSTREAM_LOCK_SHA256'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'plan output'):
            self.validate()

    def test_reject_artifact_from_another_run_even_if_checksums_match(self):
        self.build['runId'] = '999'
        self.write_json('BUILD.json', self.build)
        self.refresh_checksums()
        with self.assertRaisesRegex(RuntimeError, 'another run'):
            self.validate()

    def test_reject_payload_substitution_even_if_outer_checksums_match(self):
        self.payload['piPocketCommit'] = 'd' * 40
        self.write_json('payload.json', self.payload)
        self.refresh_checksums()
        with self.assertRaisesRegex(RuntimeError, 'payload upstream mismatch'):
            self.validate()

    def test_reject_changed_apk_even_if_build_and_outer_checksums_are_updated(self):
        with zipfile.ZipFile(self.dist / self.apk_name, 'w') as archive:
            archive.writestr('assets/payload.json', (self.dist / 'payload.json').read_bytes())
            archive.writestr('assets/pocket.tgz', b'wrong payload')
            for name in publisher.HOST_SCRIPTS:
                archive.writestr('assets/' + name, b'fixture script')
        self.build['apk']['sha256'] = publisher.digest(self.dist / self.apk_name)
        self.write_json('BUILD.json', self.build)
        self.refresh_checksums()
        with self.assertRaisesRegex(RuntimeError, 'another payload'):
            self.validate()

    def test_reject_partial_checksum_coverage_and_unsafe_names(self):
        checksums = self.dist / 'SHA256SUMS.txt'
        original = checksums.read_text()
        checksums.write_text(original.splitlines()[0] + '\n')
        with self.assertRaisesRegex(RuntimeError, 'cover every'):
            self.validate()
        checksums.write_text('0' * 64 + '  ../outside\n')
        with self.assertRaisesRegex(RuntimeError, 'Invalid SHA256SUMS'):
            self.validate()

    def test_embedded_native_runtime_is_rejected_even_with_valid_outer_hashes(self):
        with zipfile.ZipFile(self.dist / self.apk_name, 'a') as archive:
            archive.writestr('assets/runtime.zip', b'obsolete embedded runtime')
        self.build['apk']['sha256'] = publisher.digest(self.dist / self.apk_name)
        self.write_json('BUILD.json', self.build)
        self.refresh_checksums()
        with self.assertRaisesRegex(RuntimeError, 'must not embed'):
            self.validate()

    def test_payload_identity_cannot_ignore_host_script_fixes(self):
        self.payload['runtimeVersion'] = self.lock['commit'][:12] + '-' + self.payload['sha256'][:12]
        self.write_json('payload.json', self.payload)
        with zipfile.ZipFile(self.dist / self.apk_name, 'w') as archive:
            archive.writestr('assets/payload.json', (self.dist / 'payload.json').read_bytes())
            archive.writestr('assets/pocket.tgz', b'the exact prepared Android runtime archive')
            for name in publisher.HOST_SCRIPTS:
                archive.writestr('assets/' + name, b'fixture script')
        self.build['apk']['sha256'] = publisher.digest(self.dist / self.apk_name)
        self.write_json('BUILD.json', self.build)
        self.refresh_checksums()
        with self.assertRaisesRegex(RuntimeError, 'identity must bind'):
            self.validate()

    def test_host_script_and_node_requirements_are_bound_to_payload(self):
        self.payload['hostScripts']['setup-runtime.sh'] = '0' * 64
        self.write_json('payload.json', self.payload)
        with zipfile.ZipFile(self.dist / self.apk_name, 'w') as archive:
            archive.writestr('assets/payload.json', (self.dist / 'payload.json').read_bytes())
            archive.writestr('assets/pocket.tgz', b'the exact prepared Android runtime archive')
            for name in publisher.HOST_SCRIPTS:
                archive.writestr('assets/' + name, b'fixture script')
        self.build['apk']['sha256'] = publisher.digest(self.dist / self.apk_name)
        self.write_json('BUILD.json', self.build)
        self.refresh_checksums()
        with self.assertRaisesRegex(RuntimeError, 'host script mismatch'):
            self.validate()
        self.payload['minimumNode'] = '26.0.0'
        self.write_json('payload.json', self.payload)
        self.refresh_checksums()
        with self.assertRaisesRegex(RuntimeError, 'Node.js requirement mismatch'):
            self.validate()

    def test_draft_assets_are_verified_before_main_and_publication(self):
        url = self.publish()
        self.assertIn('android-1003-pi-0.27.0', url)
        self.assertEqual(self.git.head, self.git.target)
        self.assertFalse(self.api.items[0]['draft'])
        self.assertEqual(self.api.files, {path.name: path.read_bytes() for path in self.dist.iterdir()})
        self.assertEqual(publisher.parse_marker(self.api.items[0]), publisher.release_marker(self.build))

    def test_old_main_is_retained_when_source_advances_during_upload(self):
        self.api.after_upload = lambda: setattr(self.git, 'head', 'd' * 40)
        with self.assertRaisesRegex(RuntimeError, 'advanced while'):
            self.publish()
        self.assertEqual(self.git.head, 'd' * 40)
        self.assertTrue(self.api.items[0]['draft'])
        self.assertFalse(any(write[0] == 'main' for write in self.git.writes))

    def test_non_fast_forward_rejection_never_publishes(self):
        self.git.fail_push = True
        with self.assertRaisesRegex(RuntimeError, 'non-fast-forward'):
            self.publish()
        self.assertEqual(self.git.head, self.git.base)
        self.assertTrue(self.api.items[0]['draft'])

    def test_resume_after_main_advanced_but_publication_failed(self):
        self.api.fail_publish = True
        with self.assertRaisesRegex(RuntimeError, 'outage'):
            self.publish()
        self.assertEqual(self.git.head, self.git.target)
        original_uploads = len([write for write in self.api.writes if write[0] == 'upload'])
        self.api.fail_publish = False
        self.publish()
        self.assertFalse(self.api.items[0]['draft'])
        self.assertEqual(len([write for write in self.api.writes if write[0] == 'upload']), original_uploads)

    def test_published_release_rerun_is_read_only_even_after_main_advances(self):
        self.publish()
        before = copy.deepcopy((self.git.writes, self.api.writes))
        self.git.head = 'd' * 40
        self.publish()
        self.assertEqual((self.git.writes, self.api.writes), before)

    def test_published_assets_are_never_overwritten_on_digest_mismatch(self):
        self.publish()
        self.api.files['BUILD.txt'] = b'bad release data'
        before = copy.deepcopy((self.git.writes, self.api.writes))
        with self.assertRaisesRegex(RuntimeError, 'asset (size|checksum) mismatch'):
            self.publish()
        self.assertEqual((self.git.writes, self.api.writes), before)

    def test_older_version_cannot_replace_latest(self):
        self.api.items.append({'id': 5, 'tag_name': 'android-1004-pi-0.27.0', 'draft': False})
        with self.assertRaisesRegex(RuntimeError, 'newer version code'):
            self.publish()
        self.assertEqual(self.git.writes, [])
        self.assertEqual(self.api.writes, [])

    def test_tag_pointing_to_other_source_is_never_moved(self):
        self.git.tag = 'd' * 40
        with self.assertRaisesRegex(RuntimeError, 'different source tree'):
            self.publish()
        self.assertEqual(self.git.writes, [])


class CandidateCommitSafety(unittest.TestCase):
    def test_commit_changes_only_candidate_lock_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
            git('init', '--quiet')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.org')
            git('config', 'core.autocrlf', 'false')
            (root / 'upstream.lock.json').write_text('old lock\n', encoding='utf-8')
            (root / 'host.txt').write_text('host code\n', encoding='utf-8')
            git('add', '.')
            git('commit', '--quiet', '-m', 'base')
            base = git('rev-parse', 'HEAD')
            dist = root / 'dist'
            dist.mkdir()
            (dist / 'upstream.lock.json').write_text('candidate lock\n', encoding='utf-8')
            build = {'upstreamVersion': '0.27.0', 'runId': '123',
                     'upstreamLockSha256': publisher.digest(dist / 'upstream.lock.json')}
            repository = publisher.GitRepository(root, base)
            first = repository.candidate_commit(dist, build)
            second = repository.candidate_commit(dist, build)
            self.assertEqual(first, second)
            self.assertEqual(git('rev-parse', 'HEAD'), base)
            self.assertEqual(git('diff-tree', '--no-commit-id', '--name-only', '-r', first), 'upstream.lock.json')
            self.assertEqual(git('show', f'{first}:upstream.lock.json'), 'candidate lock')
            self.assertEqual((root / 'upstream.lock.json').read_text(), 'old lock\n')
            self.assertEqual(git('diff', '--cached', '--name-only'), '')


if __name__ == '__main__':
    unittest.main()
