"""Exercise coexistence, local startup and force-stop recovery on a disposable emulator.

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

APP = 'io.github.mikyan.pipocket'
TERMUX = 'com.termux'
ACTIVITY = APP + '/app.pipocket.android.MainActivity'
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
        self.started = time.monotonic()
        self.adb = os.environ.get('ADB', 'adb')
        self.serial = os.environ.get('ANDROID_SERIAL', '')
        # Do not let a shell/runner HTTP proxy receive local owner credentials.
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def redact(self, text: str) -> str:
        for token in sorted(self.tokens, key=len, reverse=True):
            text = text.replace(token, '[REDACTED]')
        text = re.sub(r'(?i)([?&](?:token|key|code)=)[^&\s]+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s]+', r'\1[REDACTED]', text)
        return re.sub(r'(?i)(["\x27]?(?:ownerToken|access_token|refresh_token)["\x27]?\s*[:=]\s*["\x27]?)[^"\x27\s,}]+',
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
        data = self.read_private('files/home/.pi-pocket/config.json')
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

    def request(self, path: str, token: str):
        request = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}',
                                         headers={'Authorization': 'Bearer ' + token})
        with self.http.open(request, timeout=3) as response:
            return response.status, response.read(), response.headers.get('Content-Type', '')

    def start(self):
        self.command('shell', 'am', 'start', '-W', '-n', ACTIVITY, timeout=30)
        result = self.command('shell', 'pidof', APP, check=False)
        self.pids.update(pid for pid in result.stdout.decode().split() if pid.isdigit())

    def wait_ready(self, seconds: int):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                token = self.token()
                if token:
                    status, body, _ = self.request('/api/me', token)
                    if status == 200:
                        return token, json.loads(body)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            time.sleep(2)
        raise RuntimeError(f'Local authenticated service did not become ready within {seconds}s')

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
        # Retry reading the token before logs so even early-startup failures redact it.
        try:
            self.token()
        except Exception:
            pass
        try:
            result = self.command('exec-out', 'run-as', APP, '/system/bin/tail', '-n', '300',
                                  'files/home/pocket.log', check=False, timeout=10)
            if result.returncode == 0:
                (self.report.parent / 'pocket.log.txt').write_text(
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
        self.command('wait-for-device', timeout=60)
        hardware = self.command('shell', 'getprop', 'ro.kernel.qemu').stdout.decode().strip()
        if hardware != '1':
            raise RuntimeError('This test only runs on a disposable Android emulator')
        self.record('Device smoke scope: x86_64 emulator; no model requests or in-flight agent tasks')
        termux_apk = self.download_termux()
        self.command('install', '-r', str(termux_apk), timeout=180)
        self.command('shell', 'run-as', TERMUX, 'mkdir', '-p', 'files')
        self.command('shell', 'run-as', TERMUX, '/system/bin/tee', 'files/.pipocket-coexist-sentinel',
                     input_data=SENTINEL)
        self.command('install', '-r', str(self.apk), timeout=180)
        self.packages()
        self.record('PASS: official Termux and Pi Pocket have distinct installed package identities')
        self.command('forward', f'tcp:{PORT}', f'tcp:{PORT}')
        self.start()
        token, identity = self.wait_ready(300)
        first_started_at = identity.get('server', {}).get('startedAt')
        if not isinstance(first_started_at, (int, float)):
            raise RuntimeError('The local service did not expose a process start timestamp')
        configured = self.command('shell', 'run-as', APP, 'test', '-f',
                                  'files/usr/share/pocket-host/configured', check=False)
        if configured.returncode != 0:
            raise RuntimeError('Runtime setup success marker is missing')
        log = self.read_private('files/home/pocket.log') or b''
        if b'PI_POCKET_RUNTIME_READY' not in log:
            raise RuntimeError('Runtime setup did not record its self-test completion marker')
        self.record('PASS: runtime configured; SQLite and child-process/tool self-tests completed')
        status, body, content_type = self.request('/', token)
        if status != 200 or 'html' not in content_type.lower() or b'<html' not in body.lower():
            raise RuntimeError('The original frontend did not return HTML')
        self.verify_termux_untouched()
        self.record('PASS: authenticated /api/me and original frontend HTML are available')
        self.command('shell', 'am', 'force-stop', APP)
        stopped = False
        for _ in range(15):
            try:
                self.request('/api/me', token)
            except (OSError, ValueError):
                stopped = True
                break
            time.sleep(1)
        if not stopped:
            raise RuntimeError('Force-stop left the original local service reachable')
        self.record('PASS: force-stop terminated the local service')
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
        self.record('PASS: same owner and local service recovered; official Termux data stayed intact')
        screenshot = self.command('exec-out', 'screencap', '-p', check=False).stdout
        if screenshot.startswith(b'\x89PNG\r\n\x1a\n'):
            (self.report.parent / 'frontend.png').write_bytes(screenshot)
        self.record('RESULT: PASS (emulator startup/restart/coexistence; ARM64 hardware and active-task recovery remain separate tests)')


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
        try:
            smoke.command('forward', '--remove', f'tcp:{PORT}', check=False, timeout=10)
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
