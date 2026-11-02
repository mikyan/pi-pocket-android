"""Publish only the validated artifact from this run; never replace release assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = 'mikyan/pi-pocket-android'
UPSTREAM = 'https://github.com/TannerMidd/pi-pocket.git'
SHA = re.compile(r'[0-9a-f]{40}')
SHA256 = re.compile(r'[0-9a-f]{64}')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
TAG = re.compile(r'android-([0-9]+)-pi-(.+)')
MARKER = re.compile(r'^<!-- pi-pocket-android (\{[^\r\n]+\}) -->$', re.MULTILINE)
FIXED_FILES = {'upstream.lock.json', 'termux-requirements.json', 'payload.json', 'BUILD.json', 'BUILD.txt'}
HOST_SCRIPTS = ('setup-runtime.sh', 'launch-pocket.sh')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def runtime_version(commit, archive_sha256, script_hashes):
    identity = json.dumps({'archiveSha256': archive_sha256, 'hostScripts': script_hashes},
                          sort_keys=True, separators=(',', ':')).encode('utf-8')
    return commit[:12] + '-' + hashlib.sha256(identity).hexdigest()[:12]


def run(command, *, cwd=ROOT, input=None, env=None):
    result = subprocess.run(command, cwd=cwd, input=input, text=True, capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError(f'{command[0]} {command[1]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    require(isinstance(value, dict), f'{Path(path).name} must contain an object')
    return value


def validate_dist(dist, environment):
    """Fail before any GitHub writes when provenance, bytes, or payload disagree."""
    dist = Path(dist).resolve()
    build = read_json(dist / 'BUILD.json')
    require(build.get('schemaVersion') == 1, 'Unsupported BUILD schema')
    require(build.get('runtime') == 'termux', 'Build must use installed Termux')
    require(environment.get('GITHUB_REPOSITORY') == REPOSITORY, 'Unexpected repository')
    require(build.get('repository') == REPOSITORY, 'Artifact belongs to another repository')
    require(SHA.fullmatch(environment.get('GITHUB_SHA', '')) is not None, 'Invalid base SHA')
    require(build.get('baseSha') == environment['GITHUB_SHA'], 'Artifact base SHA differs from this run')
    require(re.fullmatch(r'[1-9][0-9]*', environment.get('GITHUB_RUN_ID', '')) is not None, 'Invalid run ID')
    require(build.get('runId') == environment['GITHUB_RUN_ID'], 'Artifact belongs to another run')
    require(re.fullmatch(r'[1-9][0-9]*', environment.get('GITHUB_RUN_NUMBER', '')) is not None, 'Invalid run number')
    number = int(environment['GITHUB_RUN_NUMBER'])
    require(type(build.get('runNumber')) is int and build['runNumber'] == number, 'Run number mismatch')
    require(type(build.get('runAttempt')) is int and build['runAttempt'] > 0, 'Invalid build attempt')
    # A failed-jobs rerun deliberately uses the original successful build attempt.
    require(type(build.get('versionCode')) is int and build['versionCode'] == 1000 + number,
            'Version code does not belong to this workflow run')
    require(build['versionCode'] <= 2100000000, 'Android version code limit exceeded')
    require(build.get('versionName') == f'0.3.0-ci.{number}', 'Version name mismatch')
    require(isinstance(build.get('upstreamVersion'), str) and VERSION.fullmatch(build['upstreamVersion']),
            'Only stable semantic upstream versions may be published')
    require(isinstance(build.get('upstreamCommit'), str) and SHA.fullmatch(build['upstreamCommit']),
            'Invalid upstream commit')
    require(isinstance(build.get('upstreamLockSha256'), str) and SHA256.fullmatch(build['upstreamLockSha256']),
            'Invalid upstream lock digest')
    apk = build.get('apk', {})
    expected_name = f'pi-pocket-android-{build["versionName"]}-universal-debug.apk'
    require(isinstance(apk, dict) and apk.get('name') == expected_name, 'Unexpected APK filename')
    require(isinstance(apk.get('sha256'), str) and SHA256.fullmatch(apk['sha256']), 'Invalid APK digest')
    names = FIXED_FILES | {expected_name, 'SHA256SUMS.txt'}
    require({item.name for item in dist.iterdir()} == names, 'Unexpected or missing release files')
    require(all((dist / name).is_file() and not (dist / name).is_symlink() for name in names),
            'Release assets must be regular files')
    hashes = {name: digest(dist / name) for name in names}
    checksums = {}
    for line in (dist / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64}) [ *]([^/\\\r\n]+)', line)
        require(match is not None, 'Invalid SHA256SUMS entry')
        checksum, name = match.groups()
        require(name not in checksums, 'Duplicate SHA256SUMS entry')
        checksums[name] = checksum
    require(set(checksums) == names - {'SHA256SUMS.txt'}, 'SHA256SUMS must cover every release asset')
    require(all(hashes[name] == checksum for name, checksum in checksums.items()), 'Release checksum mismatch')
    require(hashes[expected_name] == apk['sha256'], 'APK differs from build manifest')
    require(hashes['upstream.lock.json'] == build['upstreamLockSha256'], 'Candidate lock digest mismatch')
    require(hashes['upstream.lock.json'] == environment.get('EXPECTED_UPSTREAM_LOCK_SHA256'),
            'Artifact candidate differs from the plan output')
    lock = read_json(dist / 'upstream.lock.json')
    require(lock.get('schemaVersion') == 1 and lock.get('repository') == UPSTREAM, 'Unexpected upstream lock')
    require(lock.get('commit') == build['upstreamCommit'] and lock.get('version') == build['upstreamVersion'],
            'Candidate lock differs from tested upstream')
    requirements = read_json(dist / 'termux-requirements.json')
    require(requirements.get('schemaVersion') == 1 and requirements.get('runtime') == 'termux'
            and requirements.get('packageName') == 'com.termux', 'Expected installed-Termux requirements')
    payload = read_json(dist / 'payload.json')
    require(payload.get('schemaVersion') == 2 and payload.get('runtime') == 'termux'
            and payload.get('archive') == 'pocket.tar.gz' and 'architecture' not in payload,
            'Expected universal installed-Termux payload')
    require(payload.get('piPocketCommit') == build['upstreamCommit']
            and payload.get('piPocketVersion') == build['upstreamVersion'], 'APK payload upstream mismatch')
    require(isinstance(lock.get('minimumNode'), str) and VERSION.fullmatch(lock['minimumNode'])
            and payload.get('minimumNode') == lock['minimumNode'], 'Minimum Node.js requirement mismatch')
    require(isinstance(payload.get('sha256'), str) and SHA256.fullmatch(payload['sha256']), 'Invalid payload digest')
    with zipfile.ZipFile(dist / expected_name) as archive:
        for name in ('assets/payload.json', 'assets/pocket.tar.gz', *('assets/' + name for name in HOST_SCRIPTS)):
            require(archive.namelist().count(name) == 1, f'APK must contain exactly one {name}')
        require('assets/runtime.zip' not in archive.namelist()
                and not any(name.startswith('lib/') for name in archive.namelist()), 'APK must not embed a native runtime')
        require(archive.read('assets/payload.json') == (dist / 'payload.json').read_bytes(), 'APK embeds another manifest')
        with archive.open('assets/pocket.tar.gz') as handle:
            require(hashlib.file_digest(handle, 'sha256').hexdigest() == payload['sha256'], 'APK embeds another payload')
        scripts = payload.get('hostScripts', {})
        require(isinstance(scripts, dict) and set(scripts) == set(HOST_SCRIPTS), 'Missing host script integrity metadata')
        for name in HOST_SCRIPTS:
            require(hashlib.sha256(archive.read('assets/' + name)).hexdigest() == scripts[name], 'APK host script mismatch')
        require(payload.get('runtimeVersion') == runtime_version(lock['commit'], payload['sha256'], scripts),
                'Payload identity must bind archive and host scripts')
    return build, hashes


def release_marker(build):
    return {key: build[key] for key in ('schemaVersion', 'runtime', 'upstreamCommit', 'upstreamLockSha256',
                                       'versionCode', 'baseSha', 'runId')}


def parse_marker(release):
    matches = MARKER.findall(release.get('body') or '')
    require(len(matches) == 1, 'Release lacks a unique build identity marker')
    value = json.loads(matches[0])
    require(isinstance(value, dict), 'Invalid release identity marker')
    return value


def release_body(build):
    marker = json.dumps(release_marker(build), separators=(',', ':'))
    return (f'Pi Pocket {build["upstreamVersion"]}, Android version code {build["versionCode"]}.\n\n'
            f'Built from host `{build["baseSha"]}` and upstream `{build["upstreamCommit"]}`.\n\n'
            'Requires installed official Termux, configured external-app access, and its RUN_COMMAND permission. '
            'The same upstream candidate passed the emulator gate for Termux startup, rendered frontend '
            'and host-app force-stop/reopen. The signed universal APK '
            'passed packaging, upstream smoke and lint checks. Physical hardware and in-flight '
            'model-task recovery have not been tested.\n\n'
            f'[Build and test run](https://github.com/{REPOSITORY}/actions/runs/{build["runId"]})\n\n'
            f'<!-- pi-pocket-android {marker} -->\n')


class GitRepository:
    def __init__(self, root, base):
        self.root, self.base = Path(root), base

    def git(self, *args, **kwargs):
        return run(['git', *args], cwd=self.root, **kwargs)

    def validate(self, dist):
        require(self.git('rev-parse', 'HEAD') == self.base, 'Publisher must check out the tested base SHA')
        origin = self.git('remote', 'get-url', 'origin')
        require(origin in (f'https://github.com/{REPOSITORY}', f'https://github.com/{REPOSITORY}.git',
                           f'git@github.com:{REPOSITORY}.git'), 'Unexpected Git origin')
        expected = subprocess.check_output(['git', 'show', f'{self.base}:termux-requirements.json'], cwd=self.root)
        require(expected == (dist / 'termux-requirements.json').read_bytes(), 'Termux requirements differ from tested host source')
        payload = read_json(dist / 'payload.json')
        for name in HOST_SCRIPTS:
            expected_script = subprocess.check_output(['git', 'show', f'{self.base}:scripts/{name}'], cwd=self.root).replace(b'\r\n', b'\n')
            require(hashlib.sha256(expected_script).hexdigest() == payload['hostScripts'][name], 'Host script differs from tested source')

    def candidate_commit(self, dist, build):
        candidate = (dist / 'upstream.lock.json').read_bytes()
        original = subprocess.check_output(['git', 'show', f'{self.base}:upstream.lock.json'], cwd=self.root)
        if candidate == original:
            return self.base
        # A private index creates exactly one file change without altering the checkout.
        # Fixed identity/date/message make reruns recover the very same commit object.
        blob = self.git('hash-object', '-w', '--stdin', input=candidate.decode('utf-8'))
        with tempfile.TemporaryDirectory(prefix='pocket-release-index-') as directory:
            environment = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
            self.git('read-tree', self.base, env=environment)
            self.git('update-index', '--add', '--cacheinfo', f'100644,{blob},upstream.lock.json', env=environment)
            tree = self.git('write-tree', env=environment)
        date = self.git('show', '-s', '--format=%cI', self.base)
        environment = dict(os.environ, GIT_AUTHOR_NAME='github-actions[bot]',
                           GIT_COMMITTER_NAME='github-actions[bot]',
                           GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
                           GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
                           GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        message = (f'Update pi-pocket to {build["upstreamVersion"]}\n\n'
                   f'Pi-Pocket-Android-Run: {build["runId"]}\n'
                   f'Pi-Pocket-Android-Lock-SHA256: {build["upstreamLockSha256"]}\n')
        return self.git('commit-tree', tree, '-p', self.base, input=message, env=environment)

    def main_head(self):
        output = self.git('ls-remote', '--heads', 'origin', 'refs/heads/main').splitlines()
        require(len(output) == 1, 'Cannot identify remote main')
        return output[0].split()[0]

    def tag_target(self, tag):
        lines = self.git('ls-remote', '--tags', 'origin', f'refs/tags/{tag}', f'refs/tags/{tag}^{{}}').splitlines()
        refs = {line.split()[1]: line.split()[0] for line in lines}
        return refs.get(f'refs/tags/{tag}^{{}}', refs.get(f'refs/tags/{tag}'))

    def push_tag(self, tag, target):
        self.git('push', 'origin', f'{target}:refs/tags/{tag}')

    def advance_main(self, target):
        self.git('push', 'origin', f'{target}:refs/heads/main')


class GitHub:
    def api(self, method, endpoint, data=None):
        command = ['gh', 'api', '--method', method, endpoint, '-H', 'Accept: application/vnd.github+json']
        if data is not None:
            command += ['--input', '-']
        output = run(command, input=json.dumps(data) if data is not None else None)
        return json.loads(output) if output else None

    def pages(self, endpoint):
        result = []
        for page in range(1, 10001):
            values = self.api('GET', f'{endpoint}?per_page=100&page={page}')
            require(isinstance(values, list), 'Unexpected GitHub pagination response')
            result.extend(values)
            if len(values) < 100:
                return result
        raise RuntimeError('GitHub pagination exceeded safe limit')

    def releases(self):
        return self.pages(f'repos/{REPOSITORY}/releases')

    def assets(self, release):
        return self.pages(f'repos/{REPOSITORY}/releases/{release["id"]}/assets')

    def create_draft(self, tag, target, build):
        return self.api('POST', f'repos/{REPOSITORY}/releases', {
            'tag_name': tag, 'target_commitish': target, 'name': f'Pi Pocket {build["upstreamVersion"]} · Android {build["versionCode"]}',
            'body': release_body(build), 'draft': True, 'prerelease': False, 'make_latest': 'false'})

    def upload(self, tag, paths):
        # No --clobber: an existing name must always be verified, never replaced.
        run(['gh', 'release', 'upload', tag, *[str(path) for path in paths], '--repo', REPOSITORY])

    def asset_digest(self, asset):
        server_digest = asset.get('digest') or ''
        if re.fullmatch(r'sha256:[0-9a-f]{64}', server_digest):
            return server_digest[7:]
        # Older API responses may omit digest; hash downloaded bytes instead.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(['gh', 'api', f'repos/{REPOSITORY}/releases/assets/{asset["id"]}',
                                     '-H', 'Accept: application/octet-stream'], stdout=output, stderr=subprocess.PIPE)
            require(result.returncode == 0, 'Cannot download release asset for verification')
            output.seek(0)
            return hashlib.file_digest(output, 'sha256').hexdigest()

    def publish(self, release):
        return self.api('PATCH', f'repos/{REPOSITORY}/releases/{release["id"]}',
                        {'draft': False, 'make_latest': 'true'})


def verify_assets(api, release, dist, hashes, *, allow_missing=False):
    assets = api.assets(release)
    names = [asset['name'] for asset in assets]
    require(len(set(names)) == len(names), 'Duplicate release asset names')
    require(set(names).issubset(hashes), 'Release contains unexpected assets')
    for asset in assets:
        name = asset['name']
        require(asset.get('state') == 'uploaded', f'Incomplete release asset: {name}')
        require(asset.get('size') == (dist / name).stat().st_size, f'Release asset size mismatch: {name}')
        require(api.asset_digest(asset) == hashes[name], f'Release asset checksum mismatch: {name}')
    missing = sorted(set(hashes) - set(names))
    require(allow_missing or not missing, 'Release assets are incomplete')
    return missing


def require_new_version(releases, build):
    for release in releases:
        if release.get('draft'):
            continue
        match = TAG.fullmatch(release.get('tag_name', ''))
        if match:
            require(int(match[1]) < build['versionCode'],
                    'A published release already has this or a newer version code; refusing to replace latest')


def publish(dist, build, hashes, git, api):
    dist = Path(dist)
    git.validate(dist)
    target = git.candidate_commit(dist, build)
    tag = f'android-{build["versionCode"]}-pi-{build["upstreamVersion"]}'
    releases = api.releases()
    existing = [release for release in releases if release['tag_name'] == tag]
    require(len(existing) <= 1, 'Duplicate releases for this tag')
    release = existing[0] if existing else None
    tag_target = git.tag_target(tag)
    require(tag_target in (None, target), 'Existing tag refers to a different source tree')
    if release:
        require(tag_target == target, 'Release tag is missing or mismatched')
        require(parse_marker(release) == release_marker(build), 'Existing release belongs to another build')
        require(not release.get('prerelease'), 'Existing release has unexpected prerelease status')
        if not release['draft']:
            verify_assets(api, release, dist, hashes)
            return f'https://github.com/{REPOSITORY}/releases/tag/{tag}'
    require_new_version(releases, build)
    require(git.main_head() in (build['baseSha'], target), 'Remote main advanced; rebuild from the current main')
    if tag_target is None:
        git.push_tag(tag, target)
    if release is None:
        release = api.create_draft(tag, target, build)
    missing = verify_assets(api, release, dist, hashes, allow_missing=True)
    if missing:
        api.upload(tag, [dist / name for name in missing])
    verify_assets(api, release, dist, hashes)
    # Check again after uploading; manual source pushes need not share our concurrency group.
    require_new_version(api.releases(), build)
    head = git.main_head()
    require(head in (build['baseSha'], target), 'Remote main advanced while preparing the draft')
    if head != target:
        git.advance_main(target)  # Fast-forward only; never rebase, merge, force or roll back.
    require(git.main_head() == target, 'Remote main changed before publication')
    result = api.publish(release)
    require(result.get('draft') is False, 'Draft publication was not confirmed; rerun the failed job')
    return f'https://github.com/{REPOSITORY}/releases/tag/{tag}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, required=True)
    args = parser.parse_args()
    dist = args.dist.resolve()
    build, hashes = validate_dist(dist, os.environ)
    url = publish(dist, build, hashes, GitRepository(ROOT, build['baseSha']), GitHub())
    print(url)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as handle:
            handle.write(f'\nPublished/verified [Android {build["versionCode"]} · Pi Pocket {build["upstreamVersion"]}]({url}).\n')


if __name__ == '__main__':
    main()
