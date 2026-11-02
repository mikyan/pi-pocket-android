#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
umask 077
BASE="${1:?managed data root is required}"
VERSION="${2:?runtime version is required}"
[[ "$BASE" == "$HOME/.local/share/pi-pocket-android" ]] || exit 64
[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ ]] || exit 64
mkdir -p "$BASE/data" "$BASE/workspace" "$BASE/logs"
if [[ "${POCKET_SERVER_LOCK_INHERITED:-}" != 1 ]]; then exec 8> "$BASE/data/server.lock"; fi
flock --exclusive --nonblock --conflict-exit-code 73 8
printf '%s\n' "$$" > "$BASE/data/server.pid"
printf '%s\n' "$VERSION" > "$BASE/data/running-version"
export PI_POCKET_GUARD=off
unset NODE_OPTIONS NODE_PATH
# Preserve the real Termux HOME: Pi credentials and extensions stay in ~/.pi/agent.
cd "$BASE/workspace"
exec node "$BASE/versions/$VERSION/bin/pi-pocket.js" \
    --access local --host 127.0.0.1 --port 18787 \
    --data "$BASE/data" --cwd "$BASE/workspace" -y
