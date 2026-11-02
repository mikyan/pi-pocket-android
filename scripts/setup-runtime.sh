#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
# Serialize setup even if Android restarts the Java host while its child survives.
exec 9>"$PREFIX/share/pocket-host/setup.lock"
"$PREFIX/bin/flock" --exclusive --nonblock 9 || exit 75
if [ -f "$PREFIX/share/pocket-host/configured" ]; then
    echo PI_POCKET_RUNTIME_READY
    exit 0
fi
export DEBIAN_FRONTEND=noninteractive
second_stage="$PREFIX/etc/termux/termux-bootstrap/second-stage/termux-bootstrap-second-stage.sh"
bootstrap_done="$PREFIX/share/pocket-host/bootstrap-configured"
if [ -f "$second_stage" ] && [ ! -f "$bootstrap_done" ]; then
    # Upstream creates this lock before postinst and leaves it behind on failure.
    # Our setup lock proves no concurrent installer is using it; replay until success.
    rm -f -- "$second_stage.lock"
    "$PREFIX/bin/bash" "$second_stage"
    touch "$bootstrap_done"
fi
"$PREFIX/bin/dpkg" --force-confnew --install "$PREFIX"/share/runtime-packages/*.deb
"$PREFIX/bin/dpkg" --configure -a
audit="$("$PREFIX/bin/dpkg" --audit)"
if [ -n "$audit" ]; then
    printf '%s\n' "$audit"
    exit 1
fi
"$PREFIX/bin/node" --input-type=module -e 'import {DatabaseSync} from "node:sqlite"; import {execFileSync} from "node:child_process"; const d=new DatabaseSync(":memory:"); d.exec("select 1"); execFileSync("bash",["-c","git --version && rg --version && fd --version && npm --version"],{stdio:"inherit"});'
touch "$PREFIX/share/pocket-host/configured"
echo PI_POCKET_RUNTIME_READY
