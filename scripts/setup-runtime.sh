#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
if [ -f "$PREFIX/share/pocket-host/configured" ]; then
    echo PI_POCKET_RUNTIME_READY
    exit 0
fi
export DEBIAN_FRONTEND=noninteractive
second_stage="$PREFIX/etc/termux/termux-bootstrap/second-stage/termux-bootstrap-second-stage.sh"
if [ -f "$second_stage" ]; then
    "$PREFIX/bin/bash" "$second_stage"
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
