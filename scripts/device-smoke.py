"""Exercise installed-Termux deployment and process recovery on a disposable emulator.

No model prompts are sent. Owner credentials are kept in memory and redacted from
the small, app-scoped diagnostic artifacts. Never point this test at a personal phone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

APP = 'io.github.mikyan.pipocket'
TERMUX = 'com.termux'
ACTIVITY = APP + '/app.pipocket.android.MainActivity'
TERMUX_ACTIVITY = TERMUX + '/com.termux.app.TermuxActivity'
RUN_PERMISSION = 'com.termux.permission.RUN_COMMAND'
TERMUX_ROOT = 'files/home/.local/share/pi-pocket-android'
PORT = 18787
TERMUX_FILENAME = 'termux-app_v0.118.3+github-debug_x86_64.apk'
TERMUX_RELEASE = 'https://github.com/termux/termux-app/releases/download/v0.118.3/'
TERMUX_APK_URL = TERMUX_RELEASE + TERMUX_FILENAME.replace('+', '%2B')
TERMUX_SUM_URL = TERMUX_RELEASE + 'termux-app_v0.118.3%2Bgithub-debug_sha256sums'
SENTINEL = b'pi-pocket-coexistence-fixture\n'


class DeviceSmoke:
    def __init__(self, apk: Path, report: Path):
        self.apk = apk.resolve()
        self.report = report.resolve()
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.tokens: set[str] = set()
        self.pids: set[str] = set()
        self.records: list[str] = []
        self.last_ui_xml: bytes | None = None
        self.started = time.monotonic()
        self.adb = os.environ.get('ADB', 'adb')
        self.serial = os.environ.get('ANDROID_SERIAL', '')
        self.emulator_verified = False
        self.runtime_version: str | None = None
        self.launch_previous_nonce: str | None = None
        # Do not let a shell/runner HTTP proxy receive local owner credentials.
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def redact(self, text: str) -> str:
        for token in sorted(self.tokens, key=len, reverse=True):
            text = text.replace(token, '[REDACTED]')
        text = re.sub(r'(?i)([?&](?:token|key|code)=)[^&\s]+', r'\1[REDACTED]', text)
        text = re.sub(r'(https?://(?:127\.0\.0\.1|localhost):\d+)/[^\s"\x27<>]*',
                      r'\1/[REDACTED]', text)
        text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s]+', r'\1[REDACTED]', text)
        return re.sub(r'(?i)(["\x27]?(?:ownerToken|token|access_token|refresh_token)["\x27]?\s*[:=]\s*["\x27]?)[^"\x27\s,}]+',
                      r'\1[REDACTED]', text)

    def record(self, text: str):
        line = f'[{time.monotonic() - self.started:.1f}s] {self.redact(text)}'
        self.records.append(line)
        print(line, flush=True)
        self.report.write_text('\n'.join(self.records) + '\n', encoding='utf-8')

    def command(self, *args: str, timeout=30, check=True, input_data=None):
        command = [self.adb]
        if self.serial:
            command += ['-s', self.serial]
        command += list(args)
        result = subprocess.run(command, input=input_data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
        if check and result.returncode:
            # Do not include command output: a run-as read might contain a token.
            raise RuntimeError(f'adb {args[0]} failed (exit {result.returncode})')
        return result

    def read_private(self, relative: str, package=APP) -> bytes | None:
        result = self.command('exec-out', 'run-as', package, 'cat', relative, check=False, timeout=10)
        return result.stdout if result.returncode == 0 else None

    def token(self) -> str | None:
        data = self.read_private(TERMUX_ROOT + '/data/config.json', TERMUX)
        if data is None:
            return None
        try:
            value = json.loads(data).get('ownerToken')
            if isinstance(value, str) and value:
                self.tokens.add(value)
                return value
        except (ValueError, UnicodeDecodeError):
            pass
        return None

    def select_emulator(self):
        if not self.serial:
            result = self.command('devices')
            emulators = [line.split()[0] for line in result.stdout.decode().splitlines()
                         if re.fullmatch(r'emulator-\d+\s+device', line.strip())]
            if len(emulators) != 1:
                raise RuntimeError('Set ANDROID_SERIAL to one disposable emulator; no unique emulator was found')
            self.serial = emulators[0]
        self.command('wait-for-device', timeout=60)
        hardware = self.command('shell', 'getprop', 'ro.kernel.qemu').stdout.decode().strip()
        if hardware != '1':
            raise RuntimeError('This test only runs on a disposable Android emulator')
        self.emulator_verified = True

    def start_termux(self):
        self.command('shell', 'am', 'start', '-W', '-n', TERMUX_ACTIVITY, timeout=30)

    def initialize_termux(self):
        self.start_termux()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            ready = self.command('shell', 'run-as', TERMUX, '/system/bin/sh', '-c',
                                 "'test -x files/usr/bin/bash && test -x files/usr/bin/apt && test -d files/home'",
                                 check=False, timeout=10)
            if ready.returncode == 0:
                self.record('PASS: official Termux initialized its own bootstrap and HOME')
                return
            time.sleep(2)
        raise RuntimeError('Official Termux did not finish first-launch initialization within 180s')

    def select_default_mirror_fixture(self):
        if not self.emulator_verified:
            raise RuntimeError('Mirror fixtures require a verified disposable emulator')
        default = 'files/usr/etc/termux/mirrors/default'
        contents = self.read_private(default, TERMUX)
        if not contents or b'MAIN="https://packages-cf.termux.dev/apt/termux-main"' not in contents.splitlines():
            raise RuntimeError('Official Termux default mirror configuration is unavailable')
        target = '/data/data/' + TERMUX + '/' + default
        selected = 'files/usr/etc/termux/chosen_mirrors'
        self.command('shell', 'run-as', TERMUX, '/system/bin/ln', '-sfn', target, selected)
        link = self.command('exec-out', 'run-as', TERMUX, '/system/bin/readlink', selected).stdout.decode().strip()
        if link != target:
            raise RuntimeError('Disposable Termux fixture did not select the official default mirror')
        self.record('PASS: disposable Termux fixture selected official packages-cf default mirror; runtime packages remain uninstalled')

    def set_external_commands(self, allowed: bool):
        # This test only writes a disposable emulator's freshly installed Termux.
        self.command('shell', 'run-as', TERMUX, 'mkdir', '-p', 'files/home/.termux')
        self.command('shell', 'run-as', TERMUX, '/system/bin/tee',
                     'files/home/.termux/termux.properties',
                     input_data=f'allow-external-apps={str(allowed).lower()}\n'.encode())
        self.command('shell', 'am', 'force-stop', TERMUX)
        self.start_termux()

    def read_ui(self, timeout=20) -> bytes | None:
        ui_path = '/data/local/tmp/pocket-ui.xml'
        self.command('shell', 'rm', '-f', ui_path, check=False, timeout=5)
        result = self.command('shell', 'uiautomator', 'dump', '--compressed', ui_path,
                              check=False, timeout=timeout)
        if result.returncode != 0:
            return None
        result = self.command('exec-out', 'cat', ui_path, check=False, timeout=5)
        if result.returncode == 0:
            self.last_ui_xml = result.stdout
            return result.stdout
        return None

    def wait_startup_blocked(self, stage: str, markers: tuple[str, ...], seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.token() is not None:
                raise RuntimeError(f'{stage} created owner data despite a blocked Termux command')
            try:
                xml = self.read_ui(timeout=min(15, max(1, deadline - time.monotonic())))
                if xml:
                    tree = ET.fromstring(xml)
                    labels = ' '.join(node.get('text', '') + ' ' + node.get('content-desc', '')
                                      for node in tree.iter('node')).lower()
                    if any(marker.lower() in labels for marker in markers):
                        (self.report.parent / f'ui-{stage}.xml.txt').write_text(
                            self.redact(xml.decode('utf-8', errors='replace')), encoding='utf-8')
                        self.record(f'PASS: {stage} showed an actionable Termux prerequisite error without starting pi-pocket')
                        return
            except (OSError, ET.ParseError, subprocess.TimeoutExpired):
                pass
            time.sleep(2)
        raise RuntimeError(f'{stage} did not show the expected prerequisite error within {seconds}s')

    def tap_button(self, text: str, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                xml = self.read_ui(timeout=min(15, max(1, deadline - time.monotonic())))
                if xml:
                    for node in ET.fromstring(xml).iter('node'):
                        if node.get('package') != APP or node.get('text', '').casefold() != text.casefold():
                            continue
                        if node.get('clickable') != 'true' or node.get('enabled') == 'false':
                            continue
                        bounds = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
                        if bounds:
                            left, top, right, bottom = map(int, bounds.groups())
                            if right > left and bottom > top:
                                self.command('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))
                                return
            except (OSError, ET.ParseError, subprocess.TimeoutExpired):
                pass
            time.sleep(1)
        raise RuntimeError('The expected host setup button was not available')

    def verify_termux_server(self):
        marker = self.read_private(TERMUX_ROOT + '/data/running-version', TERMUX)
        if not marker or marker.decode().strip() != self.runtime_version:
            raise RuntimeError('Termux server is not running this APK payload version')
        pid_file = self.read_private(TERMUX_ROOT + '/data/server.pid', TERMUX)
        pid = pid_file.decode().strip() if pid_file else ''
        if not pid.isdigit():
            raise RuntimeError('Termux server process marker is missing')
        termux_uid = self.command('exec-out', 'run-as', TERMUX, '/system/bin/id', '-u').stdout.decode().strip()
        host_uid = self.command('exec-out', 'run-as', APP, '/system/bin/id', '-u').stdout.decode().strip()
        status = self.read_private(f'/proc/{pid}/status', TERMUX)
        command_line = self.read_private(f'/proc/{pid}/cmdline', TERMUX)
        base = '/data/data/' + TERMUX + '/' + TERMUX_ROOT
        self.validate_server_process(status, command_line, termux_uid, host_uid,
                                     base + '/versions/' + self.runtime_version + '/bin/pi-pocket.js',
                                     base + '/data')

    @staticmethod
    def validate_server_process(status: bytes | None, command_line: bytes | None,
                                termux_uid: str, host_uid: str, entry: str, data: str):
        # Android's runas_app SELinux domain may read the process but cannot use
        # kill(pid, 0) on the app domain, even with the same Linux UID.
        if not termux_uid.isdigit() or not host_uid.isdigit() or termux_uid == host_uid:
            raise RuntimeError('Termux and the Android host must have distinct Linux UIDs')
        if status is None or command_line is None:
            raise RuntimeError('Managed Termux process metadata is unavailable')
        fields = status.decode('utf-8', errors='replace')
        uids = re.search(r'^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$', fields, re.MULTILINE)
        if not uids or any(uid != termux_uid for uid in uids.groups()):
            actual = ','.join(uids.groups()) if uids else 'missing'
            raise RuntimeError(f'Managed server UID mismatch: expected Termux UID {termux_uid}; actual UIDs {actual}')
        state = re.search(r'^State:\s+(\S+)', fields, re.MULTILINE)
        if not state or state.group(1).upper() in {'Z', 'X'}:
            raise RuntimeError('Managed Termux server is no longer a live process')
        arguments = command_line.decode('utf-8', errors='replace').split('\0')
        if entry not in arguments or data not in arguments:
            raise RuntimeError('Managed Termux PID does not identify the selected payload and data directory')

    def assert_service_stopped(self, token: str, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.request('/api/me', token)
            except (OSError, ValueError):
                return
            time.sleep(1)
        raise RuntimeError('Force-stopping Termux left its local service reachable')

    def request(self, path: str, token: str):
        request = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}',
                                         headers={'Authorization': 'Bearer ' + token})
        with self.http.open(request, timeout=3) as response:
            return response.status, response.read(), response.headers.get('Content-Type', '')

    @staticmethod
    def parse_connection_state(xml: bytes | None) -> dict[str, str]:
        # Never expose or retain the complete preferences, which also contain ownerToken.
        if xml is None:
            return {}
        try:
            root = ET.fromstring(xml)
        except (ET.ParseError, UnicodeDecodeError):
            return {}
        fields = {'requestNonce', 'resultNonce', 'resultError'}
        return {node.get('name'): node.text or '' for node in root.findall('string')
                if node.get('name') in fields}

    def connection_state(self) -> dict[str, str]:
        return self.parse_connection_state(self.read_private('shared_prefs/termux-connection.xml'))

    @staticmethod
    def fresh_connection_error(state: dict[str, str], previous_nonce: str | None) -> str | None:
        request = state.get('requestNonce', '')
        if request and request != previous_nonce and state.get('resultNonce') == request:
            return state.get('resultError') or None
        return None

    def start(self):
        self.launch_previous_nonce = None
        self.launch_previous_nonce = self.connection_state().get('requestNonce')
        self.command('shell', 'am', 'start', '-W', '-n', ACTIVITY, timeout=30)
        result = self.command('shell', 'pidof', APP, check=False)
        self.pids.update(pid for pid in result.stdout.decode().split() if pid.isdigit())

    def record_wait_progress(self, state: dict[str, str]):
        detail = 'waiting for Termux callback'
        for source, package, path in (
                ('Termux setup', TERMUX, TERMUX_ROOT + '/logs/setup.log'),
                ('host log', APP, 'files/termux-host.log')):
            try:
                result = self.command('exec-out', 'run-as', package, '/system/bin/tail', '-n', '1',
                                      path, check=False, timeout=5)
                if result.returncode == 0:
                    line = self.redact(result.stdout.decode('utf-8', errors='replace'))
                    line = re.sub(r'[\x00-\x1f\x7f]', ' ', line).strip()
                    if line:
                        detail = source + ': ' + line[:200]
                        break
            except (OSError, subprocess.TimeoutExpired):
                continue
        request = state.get('requestNonce', '')
        received = bool(request and request != self.launch_previous_nonce
                        and state.get('resultNonce') == request)
        self.record(f'WAIT: {detail}; current callback received={str(received).lower()}')

    def wait_ready(self, seconds: int):
        deadline = time.monotonic() + seconds
        next_progress = time.monotonic() + 30
        while time.monotonic() < deadline:
            state: dict[str, str] = {}
            try:
                token = self.token()
                state = self.connection_state()
                failure = self.fresh_connection_error(state, self.launch_previous_nonce)
                if failure:
                    raise RuntimeError('Termux startup reported an error: ' + self.redact(failure))
                if token:
                    status, body, _ = self.request('/api/me', token)
                    if status == 200:
                        return token, json.loads(body)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            if time.monotonic() >= next_progress:
                self.record_wait_progress(state)
                next_progress = time.monotonic() + 30
            time.sleep(2)
        raise RuntimeError(f'Local authenticated service did not become ready within {seconds}s')

    @staticmethod
    def rendered_webview(xml: bytes) -> dict[str, int] | None:
        """Require actual upstream controls inside a visible WebView, not the host splash/title.

        The stable labels come from pinned upstream web/sessions.js and web/home.js.
        An empty WebView, a browser error page and the native startup layout all fail.
        """
        tree = ET.fromstring(xml)
        stable_labels = ('search sessions', 'open the launcher', 'new session',
                         'no sessions yet. start one with new.')

        def visible(node):
            if node.get('visible-to-user') == 'false' or node.get('enabled') == 'false':
                return False
            bounds = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', node.get('bounds', ''))
            if not bounds:
                return False
            left, top, right, bottom = map(int, bounds.groups())
            return right > left and bottom > top

        for webview in tree.iter('node'):
            if webview.get('class') != 'android.webkit.WebView' or not visible(webview):
                continue
            if webview.get('package', APP) != APP:
                continue
            labels = set()
            actionable = 0
            for child in webview.iter('node'):
                if child is webview or not visible(child):
                    continue
                label = ' '.join((child.get('text', '') + ' ' + child.get('content-desc', '')).split()).lower()
                if label and label != 'pi pocket':
                    labels.add(label)
                if any(child.get(attribute) == 'true' for attribute in ('clickable', 'focusable', 'long-clickable')):
                    actionable += 1
            stable = sum(any(label.startswith(expected) for label in labels) for expected in stable_labels)
            if len(labels) >= 2 and actionable >= 1 and stable >= 1:
                return {'visible_labels': len(labels), 'actionable_nodes': actionable, 'upstream_markers': stable}
        return None

    def wait_webview_rendered(self, stage: str, seconds=60):
        deadline = time.monotonic() + seconds
        ui_path = '/data/local/tmp/pocket-ui.xml'
        self.last_ui_xml = None

        def budget(limit):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('WebView rendering deadline reached')
            return min(limit, remaining)

        while time.monotonic() < deadline:
            try:
                # Remove the previous dump so a failed refresh cannot reuse a pre-restart UI.
                self.command('shell', 'rm', '-f', ui_path, check=False, timeout=budget(5))
                dumped = self.command('shell', 'uiautomator', 'dump', '--compressed', ui_path,
                                      check=False, timeout=budget(15))
                if dumped.returncode == 0:
                    snapshot = self.command('exec-out', 'cat', ui_path, check=False, timeout=budget(5))
                    if snapshot.returncode == 0:
                        self.last_ui_xml = snapshot.stdout
                        details = self.rendered_webview(snapshot.stdout)
                        if details:
                            # .xml.txt stays within the existing scoped text-artifact upload.
                            (self.report.parent / f'ui-{stage}.xml.txt').write_text(
                                self.redact(snapshot.stdout.decode('utf-8', errors='replace')), encoding='utf-8')
                            self.record(f'PASS: {stage} WebView rendered upstream controls '
                                        f'({details["visible_labels"]} visible labels, '
                                        f'{details["actionable_nodes"]} actionable nodes)')
                            return
            except (OSError, ET.ParseError, subprocess.TimeoutExpired):
                pass
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise RuntimeError(f'{stage} WebView did not render visible upstream controls within {seconds}s')

    def packages(self):
        output = self.command('shell', 'pm', 'list', 'packages').stdout.decode()
        installed = set(output.splitlines())
        if f'package:{APP}' not in installed or f'package:{TERMUX}' not in installed:
            raise RuntimeError('Both independent Android packages must remain installed')

    def verify_termux_untouched(self):
        actual = self.read_private('files/.pipocket-coexist-sentinel', TERMUX)
        if actual != SENTINEL:
            raise RuntimeError('Official Termux private-data sentinel changed')

    def download_termux(self) -> Path:
        cache = self.report.parent / 'downloads'
        cache.mkdir(exist_ok=True)
        apk = cache / TERMUX_FILENAME
        sums = cache / 'termux-sha256sums.txt'
        for url, destination in ((TERMUX_APK_URL, apk), (TERMUX_SUM_URL, sums)):
            subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                            '--retry', '3', '--connect-timeout', '20', '--max-time', '180',
                            '--output', str(destination), url], check=True, timeout=600)
        checksum = None
        for line in sums.read_text(encoding='utf-8').splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].lstrip('*').removeprefix('./') == TERMUX_FILENAME:
                checksum = parts[0].lower()
        if not checksum or not re.fullmatch('[0-9a-f]{64}', checksum):
            raise RuntimeError('Official checksum list does not contain the pinned Termux APK')
        if hashlib.sha256(apk.read_bytes()).hexdigest() != checksum:
            raise RuntimeError('Official Termux APK checksum mismatch')
        self.record('PASS: official Termux 0.118.3 x86_64 APK matches release SHA-256')
        return apk

    def diagnostics(self):
        if not self.emulator_verified:
            return
        # Retry reading the token before logs so even early-startup failures redact it.
        try:
            self.token()
        except Exception:
            pass
        if self.last_ui_xml is not None:
            (self.report.parent / 'ui-failure.xml.txt').write_text(
                self.redact(self.last_ui_xml.decode('utf-8', errors='replace')), encoding='utf-8')
        for package, relative, name in (
                (APP, 'files/termux-host.log', 'host.log.txt'),
                (TERMUX, TERMUX_ROOT + '/logs/setup.log', 'termux-setup.log.txt'),
                (TERMUX, TERMUX_ROOT + '/logs/server.log', 'termux-server.log.txt')):
            try:
                result = self.command('exec-out', 'run-as', package, '/system/bin/tail', '-n', '300',
                                      relative, check=False, timeout=10)
                if result.returncode == 0:
                    (self.report.parent / name).write_text(
                        self.redact(result.stdout.decode('utf-8', errors='replace')), encoding='utf-8')
            except Exception:
                pass
        try:
            current = self.command('shell', 'pidof', APP, check=False, timeout=10)
            self.pids.update(pid for pid in current.stdout.decode().split() if pid.isdigit())
            for pid in sorted(self.pids):
                result = self.command('logcat', '-d', '-v', 'threadtime', '--pid=' + pid,
                                      '-t', '300', check=False, timeout=15)
                (self.report.parent / f'logcat-app-{pid}.txt').write_text(
                    self.redact(result.stdout.decode('utf-8', errors='replace')), encoding='utf-8')
        except Exception:
            pass

    def run(self):
        if not self.apk.is_file():
            raise RuntimeError('Test APK does not exist')
        with zipfile.ZipFile(self.apk) as archive:
            payload = json.loads(archive.read('assets/payload.json'))
            if payload.get('schemaVersion') != 2 or payload.get('runtime') != 'termux':
                raise RuntimeError('Device test requires the installed-Termux payload schema')
            self.runtime_version = payload.get('runtimeVersion')
            if not isinstance(self.runtime_version, str) or not self.runtime_version:
                raise RuntimeError('APK payload version is missing')
            if 'assets/pocket.tgz' not in archive.namelist():
                raise RuntimeError('Universal APK lacks the original upstream payload')
            if any(name.startswith('lib/') or name == 'assets/runtime.zip' for name in archive.namelist()):
                raise RuntimeError('Universal APK unexpectedly contains an embedded native runtime')
        self.select_emulator()
        self.record('Device smoke scope: installed-Termux integration on x86_64 emulator; no model requests or in-flight agent tasks')
        termux_apk = self.download_termux()
        self.command('install', '-r', str(termux_apk), timeout=180)
        self.initialize_termux()
        self.select_default_mirror_fixture()
        self.command('shell', 'run-as', TERMUX, 'mkdir', '-p', 'files')
        self.command('shell', 'run-as', TERMUX, '/system/bin/tee', 'files/.pipocket-coexist-sentinel',
                     input_data=SENTINEL)
        self.command('install', '-r', str(self.apk), timeout=180)
        self.command('shell', 'pm', 'grant', APP, 'android.permission.POST_NOTIFICATIONS', check=False)
        self.packages()
        self.record('PASS: official Termux and Pi Pocket have distinct installed package identities')
        self.command('forward', f'tcp:{PORT}', f'tcp:{PORT}')

        self.command('shell', 'pm', 'revoke', APP, RUN_PERMISSION)
        self.command('shell', 'pm', 'set-permission-flags', APP, RUN_PERMISSION, 'user-set', 'user-fixed')
        self.start()
        self.tap_button('连接 Termux 并启动')
        self.wait_startup_blocked('permission-denied', ('RUN_COMMAND', '需要授权', 'Termux 权限'))
        self.command('shell', 'am', 'force-stop', APP)
        self.command('shell', 'pm', 'clear-permission-flags', APP, RUN_PERMISSION, 'user-set', 'user-fixed')
        self.command('shell', 'pm', 'grant', APP, RUN_PERMISSION)
        self.set_external_commands(False)
        self.start()
        self.wait_startup_blocked('external-commands-disabled', ('allow-external-apps',))
        self.command('shell', 'am', 'force-stop', APP)
        self.set_external_commands(True)
        self.start()
        # This includes the real first-run pkg installation; CI must not preload the runtime.
        token, identity = self.wait_ready(900)
        first_started_at = identity.get('server', {}).get('startedAt')
        if not isinstance(first_started_at, (int, float)):
            raise RuntimeError('The local service did not expose a process start timestamp')
        self.verify_termux_server()
        setup_log = self.read_private(TERMUX_ROOT + '/logs/setup.log', TERMUX) or b''
        if b'PI_POCKET_TERMUX_SELF_TEST_OK' not in setup_log:
            raise RuntimeError('Termux deployment did not record its Node.js, SQLite and tool self-test marker')
        self.record('PASS: Termux Node.js/SQLite and git/rg/fd/curl/tar/npm/flock self-tests completed')
        self.record('PASS: first-run deployment started the selected upstream payload under the Termux UID')
        status, body, content_type = self.request('/', token)
        if status != 200 or 'html' not in content_type.lower() or b'<html' not in body.lower():
            raise RuntimeError('The original frontend did not return HTML')
        self.verify_termux_untouched()
        self.record('PASS: authenticated /api/me and original frontend HTML are available')
        self.wait_webview_rendered('initial')
        self.command('shell', 'am', 'force-stop', APP)
        for _ in range(5):
            status, body, _ = self.request('/api/me', token)
            if status != 200 or json.loads(body).get('server', {}).get('startedAt') != first_started_at:
                raise RuntimeError('Stopping the Android host interrupted or replaced the Termux service')
            time.sleep(1)
        self.verify_termux_server()
        self.record('PASS: force-stopping only Pi Pocket leaves the same Termux server running')
        self.start()
        reopened_token, reopened_identity = self.wait_ready(180)
        if reopened_token != token or reopened_identity.get('server', {}).get('startedAt') != first_started_at:
            raise RuntimeError('Reopening the host failed to reconnect to the existing owner and server')
        self.wait_webview_rendered('host-reopened')

        # Stop the host first so its foreground retry loop cannot race this assertion.
        self.command('shell', 'am', 'force-stop', APP)
        self.command('shell', 'am', 'force-stop', TERMUX)
        self.assert_service_stopped(token)
        self.record('PASS: force-stopping Termux terminates the server it owns')
        self.start()
        recovered_token, recovered_identity = self.wait_ready(180)
        if recovered_token != token:
            raise RuntimeError('Owner identity was regenerated after force-stop')
        recovered_started_at = recovered_identity.get('server', {}).get('startedAt', 0)
        if not isinstance(recovered_started_at, (int, float)) or recovered_started_at <= first_started_at:
            raise RuntimeError('Force-stop recovery did not start a new server process')
        before = identity.get('user', {}).get('id')
        after = recovered_identity.get('user', {}).get('id')
        if before is not None and before != after:
            raise RuntimeError('Authenticated owner changed after force-stop')
        self.packages()
        self.verify_termux_untouched()
        self.verify_termux_server()
        self.record('PASS: reopening Pi Pocket restarts its Termux server with the same owner; pre-existing Termux sentinel stays intact')
        self.wait_webview_rendered('termux-restarted')
        # Accessibility confirms the DOM is visible; allow the compositor one final frame.
        time.sleep(1)
        screenshot = self.command('exec-out', 'screencap', '-p', check=False).stdout
        if screenshot.startswith(b'\x89PNG\r\n\x1a\n'):
            (self.report.parent / 'frontend.png').write_bytes(screenshot)
        self.record('RESULT: PASS (Termux permissions/deployment/render/host reconnect/Termux restart; physical devices and active-task recovery remain separate tests)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', type=Path, required=True)
    parser.add_argument('--report', type=Path, default=Path('.cache/device-test/report.txt'))
    args = parser.parse_args()
    smoke = DeviceSmoke(args.apk, args.report)
    try:
        smoke.run()
    except Exception as error:
        smoke.record('RESULT: FAIL - ' + str(error))
        smoke.diagnostics()
        return 1
    finally:
        if smoke.emulator_verified:
            try:
                smoke.command('forward', '--remove', f'tcp:{PORT}', check=False, timeout=10)
            except Exception:
                pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
