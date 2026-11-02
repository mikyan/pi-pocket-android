#!/data/data/com.termux/files/usr/bin/bash
# Invoked over the official RUN_COMMAND stdin API. Never modifies upstream source.
set -Eeuo pipefail
umask 077
unset NODE_OPTIONS NODE_PATH
: "${HOME:?Termux HOME is required}" "${PREFIX:?Termux PREFIX is required}"
[[ "$HOME" == /* && "$HOME" != / ]] || exit 64
[[ "$POCKET_RUNTIME_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ ]] || exit 64
[[ "$POCKET_ARCHIVE_SHA256" =~ ^[a-f0-9]{64}$ ]] || exit 64
[[ "$POCKET_MINIMUM_NODE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 64
[[ "$POCKET_REQUEST_NONCE" =~ ^[a-f0-9-]{32,40}$ ]] || exit 64
[[ "$POCKET_OPERATION" == start || "$POCKET_OPERATION" == restart || "$POCKET_OPERATION" == probe ]] || exit 64
BASE="$HOME/.local/share/pi-pocket-android"
mkdir -p "$BASE/data" "$BASE/workspace" "$BASE/versions" "$BASE/logs"
CONNECTION="$BASE/data/connect-$POCKET_REQUEST_NONCE"
STAGE='initializing Termux integration'
exec 3>&1 4>&2

failure() {
    local code="$1"
    trap - ERR
    : > "$CONNECTION.error"
    if command -v node >/dev/null 2>&1; then
        node - "$BASE" "$STAGE" "$code" >&3 2>/dev/null <<'JS' || true
const fs = require('node:fs');
const [base, stage, code] = process.argv.slice(2);
let token = '';
try { token = JSON.parse(fs.readFileSync(base + '/data/config.json', 'utf8')).ownerToken || ''; } catch {}
let diagnostic = '';
for (const name of ['setup.log', 'server.log']) {
  try { const f = fs.openSync(base + '/logs/' + name, 'r'); const size = fs.fstatSync(f).size;
    const b = Buffer.alloc(Math.min(size, 5000)); fs.readSync(f, b, 0, b.length, size - b.length); fs.closeSync(f);
    diagnostic += '\n' + name + ':\n' + b.toString('utf8'); } catch {}
}
if (token) diagnostic = diagnostic.split(token).join('[hidden]');
diagnostic = diagnostic.replace(/http:\/\/127\.0\.0\.1:\d+\/[^\s"']+/g, '[local capability hidden]')
  .replace(/([?&](?:token|key|code)=)[^&\s]+/gi, '$1[hidden]')
  .replace(/(authorization[:=]\s*(?:bearer\s+)?)[^\s]+/gi, '$1[hidden]')
  .replace(/("(?:ownerToken|token|apiKey)"\s*:\s*")[^"]+/gi, '$1[hidden]');
process.stdout.write('POCKET_RESULT:' + JSON.stringify({status:'error', message:stage + ' failed (exit ' + code + '). See ~/.local/share/pi-pocket-android/logs/.', diagnostic}) + '\n');
JS
    else
        printf 'POCKET_RESULT:{"status":"error","message":"Installing Termux dependencies failed. Check network and ~/.local/share/pi-pocket-android/logs/setup.log."}\n' >&3
        tail -c 5000 "$BASE/logs/setup.log" >&4 2>/dev/null || true
    fi
    exit "$code"
}
trap 'failure $?' ERR

if [[ "$POCKET_OPERATION" == probe ]]; then
    # The separate short command carries credentials only in its PendingIntent result.
    # A per-request marker is written by setup only after an old instance is handled.
    probe_deadline=$((SECONDS + 19 * 60))
    while ((SECONDS < probe_deadline)); do
        if [[ -f "$CONNECTION.error" ]]; then STAGE='Termux setup'; failure 1; fi
        if [[ -f "$CONNECTION" ]] && command -v node >/dev/null 2>&1; then
            if node - "$BASE" "$CONNECTION" "$POCKET_RUNTIME_VERSION" 2>/dev/null <<'JS'
const fs = require('node:fs'); const http = require('node:http');
const [base, marker, wanted] = process.argv.slice(2);
try {
  const expected = fs.readFileSync(marker, 'utf8').trim();
  const version = fs.readFileSync(base + '/data/running-version', 'utf8').trim();
  const pid = Number(fs.readFileSync(base + '/data/server.pid', 'utf8').trim());
  if (expected !== version || !Number.isSafeInteger(pid) || pid <= 1) process.exit(1);
  process.kill(pid, 0);
  const args = fs.readFileSync('/proc/' + pid + '/cmdline', 'utf8').split('\0');
  if (!args.includes(base + '/versions/' + version + '/bin/pi-pocket.js') || !args.includes(base + '/data')) process.exit(1);
  const token = JSON.parse(fs.readFileSync(base + '/data/config.json', 'utf8')).ownerToken;
  if (typeof token !== 'string' || !token) process.exit(1);
  const request = http.get({hostname:'127.0.0.1', port:18787, path:'/api/me', headers:{Authorization:'Bearer ' + token}}, response => {
    response.resume();
    if (response.statusCode !== 200) process.exit(1);
    try { fs.unlinkSync(marker); } catch {}
    process.stdout.write('POCKET_RESULT:' + JSON.stringify({status:version === wanted ? 'ready' : 'deferred', token, runtimeVersion:version}) + '\n', () => process.exit(0));
  });
  request.on('error', () => process.exit(1));
  request.setTimeout(1500, () => { request.destroy(); process.exit(1); });
} catch { process.exit(1); }
JS
            then exit 0; fi
        fi
        sleep 1
    done
    STAGE='Waiting for authenticated Pi Pocket startup'; failure 1
fi

exec >> "$BASE/logs/setup.log" 2>&1
printf '\nPi Pocket Termux setup: %s (%s)\n' "$POCKET_RUNTIME_VERSION" "$POCKET_OPERATION"
die() { printf '%s\n' "$*" >&2; return 1; }

# Bootstrap only the lock tool before taking the lasting deployment lock. A mkdir
# lock covers the very first util-linux install without relying on that tool.
BOOTSTRAP="$BASE/bootstrap.lock"
BOOTSTRAP_OWNED=0
release_bootstrap() {
    if [[ "$BOOTSTRAP_OWNED" == 1 && -f "$BOOTSTRAP/pid" && "$(cat "$BOOTSTRAP/pid")" == "$$" ]]; then
        rm -f -- "$BOOTSTRAP/pid"
        rmdir "$BOOTSTRAP" 2>/dev/null || true
        BOOTSTRAP_OWNED=0
    fi
}
trap release_bootstrap EXIT
STAGE='Installing Termux locking tools'
if ! command -v flock >/dev/null 2>&1; then
    empty_count=0
    for ((attempt=0; attempt<120; attempt++)); do
        if mkdir "$BOOTSTRAP" 2>/dev/null; then
            printf '%s\n' "$$" > "$BOOTSTRAP/pid"
            BOOTSTRAP_OWNED=1
            break
        fi
        owner=''
        [[ ! -f "$BOOTSTRAP/pid" ]] || read -r owner < "$BOOTSTRAP/pid" || true
        if [[ "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" 2>/dev/null; then
            rm -f -- "$BOOTSTRAP/pid"
            rmdir "$BOOTSTRAP" 2>/dev/null || true
        elif [[ ! -s "$BOOTSTRAP/pid" ]]; then
            empty_count=$((empty_count + 1))
            # Recover SIGKILL between mkdir and pid creation; rmdir only removes an empty directory.
            if [[ "$empty_count" -ge 3 && ! -s "$BOOTSTRAP/pid" ]]; then
                rm -f -- "$BOOTSTRAP/pid"
                rmdir "$BOOTSTRAP" 2>/dev/null || true
            fi
        else
            empty_count=0
        fi
        sleep 1
    done
    [[ "$BOOTSTRAP_OWNED" == 1 ]] || die 'Another Termux dependency installer is busy; retry later.'
    pkg install -y util-linux
fi
exec 9> "$BASE/setup.lock"
flock --exclusive --wait 120 9
release_bootstrap

managed_pid() {
    ACTIVE=''; PID=''
    [[ -f "$BASE/data/running-version" && -f "$BASE/data/server.pid" ]] || return 1
    read -r ACTIVE < "$BASE/data/running-version"
    read -r PID < "$BASE/data/server.pid"
    [[ "$ACTIVE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ && "$PID" =~ ^[0-9]+$ && "$PID" -gt 1 ]] || return 1
    kill -0 "$PID" 2>/dev/null || return 1
    local script_found=0 data_found=0 arg
    while IFS= read -r -d '' arg; do
        [[ "$arg" != "$BASE/versions/$ACTIVE/bin/pi-pocket.js" ]] || script_found=1
        [[ "$arg" != "$BASE/data" ]] || data_found=1
    done < "/proc/$PID/cmdline"
    [[ "$script_found" == 1 && "$data_found" == 1 ]]
}

STAGE='Checking the existing Pi Pocket process'
if ! flock --exclusive --nonblock "$BASE/data/server.lock" true; then
    managed_pid || die 'A process holds the Pi Pocket lock but its identity cannot be confirmed. Nothing was stopped.'
    if [[ "$POCKET_OPERATION" != restart ]]; then
        printf '%s\n' "$ACTIVE" > "$CONNECTION"
        printf 'Existing Pi Pocket preserved; requested update is deferred until an explicit restart.\n'
        exit 0
    fi
    STAGE='Stopping Pi Pocket after the explicit restart request'
    kill -TERM "$PID"
    stopped=0
    for ((attempt=0; attempt<60; attempt++)); do
        if flock --exclusive --nonblock "$BASE/data/server.lock" true; then stopped=1; break; fi
        sleep 1
    done
    [[ "$stopped" == 1 ]] || die 'Pi Pocket has not stopped after SIGTERM. No force-kill or upgrade was performed.'
fi

STAGE='Checking Node.js compatibility'
missing=()
if command -v node >/dev/null 2>&1; then
    node - "$POCKET_MINIMUM_NODE" <<'JS' || die 'The existing Node.js is too old or lacks node:sqlite. Upgrade your chosen nodejs/nodejs-lts package in Termux, then retry; Pi Pocket will not replace it automatically.'
const actual = process.versions.node.split('.').map(Number), minimum = process.argv[2].split('.').map(Number);
let newer = false;
for (let i=0; i<3; i++) { if (actual[i] > minimum[i]) { newer = true; break; } if (actual[i] < minimum[i]) process.exit(1); }
const {DatabaseSync} = require('node:sqlite'); const db = new DatabaseSync(':memory:'); db.exec('select 1'); db.close();
JS
else
    missing+=(nodejs-lts npm)
fi
for pair in 'git:git' 'rg:ripgrep' 'fd:fd' 'curl:curl' 'tar:tar' 'sha256sum:coreutils' 'base64:coreutils' 'npm:npm'; do
    command_name="${pair%%:*}"; package="${pair#*:}"
    if ! command -v "$command_name" >/dev/null 2>&1 && [[ " ${missing[*]} " != *" $package "* ]]; then missing+=("$package"); fi
done
STAGE='Installing only missing Termux dependencies'
if [[ "${#missing[@]}" -gt 0 ]]; then pkg install -y "${missing[@]}"; fi
STAGE='Validating Node.js and command tools'
node - "$POCKET_MINIMUM_NODE" <<'JS'
const actual = process.versions.node.split('.').map(Number), minimum = process.argv[2].split('.').map(Number);
for (let i=0; i<3; i++) { if (actual[i] > minimum[i]) break; if (actual[i] < minimum[i]) throw Error('Node.js below required version'); }
const {DatabaseSync} = require('node:sqlite'); const db = new DatabaseSync(':memory:'); db.exec('select 1'); db.close();
const {execFileSync} = require('node:child_process');
for (const command of ['git', 'rg', 'fd', 'curl', 'tar', 'npm', 'flock']) execFileSync(command, ['--version'], {stdio:'ignore'});
console.log('PI_POCKET_TERMUX_SELF_TEST_OK');
JS

DESTINATION="$BASE/versions/$POCKET_RUNTIME_VERSION"
if [[ -d "$DESTINATION" ]]; then
    [[ -f "$DESTINATION/.android/archive.sha256" && "$(cat "$DESTINATION/.android/archive.sha256")" == "$POCKET_ARCHIVE_SHA256" ]] \
        || die 'The existing version directory does not match this APK. It was not overwritten.'
else
    STAGE='Transferring the verified Pi Pocket payload'
    ARCHIVE="$BASE/versions/.download-$POCKET_REQUEST_NONCE.tar.gz"
    curl --fail --silent --show-error --noproxy '*' --proto '=http' --connect-timeout 10 --max-time 180 \
        --output "$ARCHIVE" "$POCKET_DOWNLOAD_URL"
    printf '%s  %s\n' "$POCKET_ARCHIVE_SHA256" "$ARCHIVE" | sha256sum --check --status
    STAGE='Extracting the original upstream and JavaScript dependencies'
    STAGING="$BASE/versions/.install-$POCKET_REQUEST_NONCE"
    [[ ! -e "$STAGING" ]] || die 'An incomplete staging directory exists; retry with a new request.'
    mkdir "$STAGING"
    tar --extract --gzip --file "$ARCHIVE" --directory "$STAGING" --no-same-owner
    [[ -f "$STAGING/bin/pi-pocket.js" && -f "$STAGING/package.json" && -d "$STAGING/node_modules" ]] || die 'Incomplete Pi Pocket payload.'
    [[ ! -e "$STAGING/.android" && ! -L "$STAGING/.android" ]] || die 'The upstream payload uses the reserved .android directory.'
    mkdir "$STAGING/.android"
    printf '%s\n' "$POCKET_ARCHIVE_SHA256" > "$STAGING/.android/archive.sha256"
    mv -- "$STAGING" "$DESTINATION"
    rm -f -- "$ARCHIVE"
fi
STAGE='Checking the loopback port'
node - <<'JS' || die 'Local port 18787 is occupied by another process. Nothing was stopped.'
const net = require('node:net'); const server = net.createServer();
server.once('error', () => process.exit(1)); server.listen(18787, '127.0.0.1', () => server.close());
JS

# Reserve the server lock before releasing the setup lock, then inherit it across
# exec. The long RUN_COMMAND itself becomes Node, so TermuxService tracks it.
exec 8> "$BASE/data/server.lock"
flock --exclusive --nonblock 8
previous=''
[[ ! -f "$BASE/data/running-version" ]] || read -r previous < "$BASE/data/running-version" || true
if [[ -n "$previous" && "$previous" != "$POCKET_RUNTIME_VERSION" && -f "$BASE/data/config.json" ]]; then
    STAGE='Backing up stopped Pi Pocket data before the version change'
    mkdir -p "$BASE/backups"
    backup="$BASE/backups/data-before-$POCKET_RUNTIME_VERSION-$POCKET_REQUEST_NONCE.tar.gz"
    tar --create --gzip --file "$backup.partial" --directory "$BASE" \
        --exclude='data/server.lock' --exclude='data/connect-*' data
    mv -- "$backup.partial" "$backup"
fi
STAGE='Installing the Android host launcher after the previous server stopped'
[[ -d "$DESTINATION/.android" && ! -L "$DESTINATION/.android" ]] || die 'Invalid Android host metadata directory.'
printf '%s' "$POCKET_LAUNCH_BASE64" | base64 --decode > "$DESTINATION/.android/launch-$POCKET_REQUEST_NONCE.tmp"
printf '%s' "$POCKET_MANIFEST_BASE64" | base64 --decode > "$DESTINATION/.android/payload-$POCKET_REQUEST_NONCE.tmp"
chmod 700 "$DESTINATION/.android/launch-$POCKET_REQUEST_NONCE.tmp"
mv -- "$DESTINATION/.android/launch-$POCKET_REQUEST_NONCE.tmp" "$DESTINATION/.android/launch-pocket.sh"
mv -- "$DESTINATION/.android/payload-$POCKET_REQUEST_NONCE.tmp" "$DESTINATION/.android/payload.json"
printf '%s\n' "$POCKET_RUNTIME_VERSION" > "$CONNECTION"
flock --unlock 9
exec 9>&-
trap - EXIT
export POCKET_SERVER_LOCK_INHERITED=1
export PI_POCKET_GUARD=off
exec "$PREFIX/bin/bash" "$DESTINATION/.android/launch-pocket.sh" "$BASE" "$POCKET_RUNTIME_VERSION" \
    >> "$BASE/logs/server.log" 2>&1 3>&- 4>&-
