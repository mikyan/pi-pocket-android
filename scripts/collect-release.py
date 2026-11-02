#!/usr/bin/env python3
"""Collect the verified universal Termux-hosted APK and bind it to this run."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def collect(root, env):
    run_number = int(env["GITHUB_RUN_NUMBER"])
    version_code = 1000 + run_number
    version_name = f"0.3.0-ci.{run_number}"
    if int(env["POCKET_VERSION_CODE"]) != version_code or env["POCKET_VERSION_NAME"] != version_name:
        raise ValueError("Built APK version differs from this workflow's version")
    if not re.fullmatch(r"[0-9a-f]{40}", env["GITHUB_SHA"]):
        raise ValueError("Invalid host commit")
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    if list(dist.iterdir()):
        raise ValueError("Release staging directory must be empty")
    apk_name = f"pi-pocket-android-{version_name}-universal-debug.apk"
    shutil.copyfile(root / "app/build/outputs/apk/debug/app-debug.apk", dist / apk_name)
    for source in ("app/src/main/assets/payload.json", "upstream.lock.json", "termux-requirements.json"):
        shutil.copyfile(root / source, dist / Path(source).name)
    lock = json.loads((dist / "upstream.lock.json").read_text(encoding="utf-8"))
    build = {
        "schemaVersion": 1, "runtime": "termux", "baseSha": env["GITHUB_SHA"],
        "repository": env["GITHUB_REPOSITORY"], "runId": env["GITHUB_RUN_ID"],
        "runNumber": run_number, "runAttempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "versionCode": version_code, "versionName": version_name,
        "upstreamCommit": lock["commit"], "upstreamVersion": lock["version"],
        "upstreamLockSha256": digest(dist / "upstream.lock.json"),
        "apk": {"name": apk_name, "sha256": digest(dist / apk_name)},
    }
    (dist / "BUILD.json").write_text(json.dumps(build, indent=2) + "\n", encoding="utf-8", newline="\n")
    run_url = f"https://github.com/{build['repository']}/actions/runs/{build['runId']}"
    (dist / "BUILD.txt").write_text(
        f"Host commit: {build['baseSha']}\nWorkflow run: {run_url}\n"
        f"pi-pocket: {lock['version']} ({lock['commit']})\n"
        f"Android version: {version_name} / {version_code}\n"
        "Requires an installed and configured official Termux; no embedded Linux runtime.\n"
        "Device gate: same candidate passed installed-Termux startup, rendered frontend, and host-app force-stop/reopen checks.\n"
        "Universal APK: built, linted, signed with the repository key, and signature verified.\n"
        "Physical hardware and in-flight model-task recovery: not tested.\n",
        encoding="utf-8", newline="\n")
    files = sorted(path for path in dist.iterdir() if path.is_file())
    (dist / "SHA256SUMS.txt").write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in files), encoding="utf-8", newline="\n")
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"APK: `{apk_name}`\n\nVersion code: `{version_code}`\n\nWorkflow: {run_url}\n")
    return build


if __name__ == "__main__":
    print(json.dumps(collect(ROOT, os.environ), indent=2))
