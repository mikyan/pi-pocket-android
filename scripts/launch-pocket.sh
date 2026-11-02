#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
mkdir -p "$HOME/.pi-pocket"
# One original upstream supervisor per persistent database. Lock survives native host death.
exec "$PREFIX/bin/flock" --exclusive --nonblock --conflict-exit-code 73 --no-fork \
    "$HOME/.pi-pocket/android-host.lock" \
    "$PREFIX/bin/node" "$PREFIX/share/pi-pocket/bin/pi-pocket.js" "$@"
