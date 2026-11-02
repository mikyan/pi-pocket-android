#!/usr/bin/env python3
"""Select one immutable upstream candidate for every job in this workflow run."""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import hashlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_upstream", ROOT / "scripts/check-upstream.py")
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)

RELEASE_MARKER = re.compile(r"<!-- pi-pocket-android (\{[^\r\n]*\}) -->")


def validate_runtime(candidate, runtime):
    versions = [package["Version"] for package in runtime["packages"]
                if package.get("Package") == "nodejs-lts"]
    if len(versions) != 1:
        raise ValueError("Runtime must contain exactly one pinned nodejs-lts package")
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-\d+)?", versions[0])
    if not match or upstream.version_tuple(match[1]) < upstream.version_tuple(candidate["minimumNode"]):
        raise ValueError("Upstream requires a newer Node.js runtime; update and test the Android runtime locks first")


def already_published(releases, candidate, lock_sha256):
    if not isinstance(releases, list):
        raise ValueError("Expected a GitHub release list")
    for release in releases:
        if release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        for match in RELEASE_MARKER.finditer(release.get("body") or ""):
            try:
                record = json.loads(match[1])
            except ValueError:
                continue
            if (isinstance(record, dict) and record.get("schemaVersion") == 1
                    and record.get("upstreamCommit") == candidate["commit"]
                    and record.get("upstreamLockSha256") == lock_sha256):
                return True
    return False


def make_plan(event, ref, check_requested, publish_requested, changed, published):
    main = ref == "refs/heads/main" and event != "pull_request"
    scheduled = event == "schedule"
    # A scheduled no-op uses no emulator/build minutes. If publication failed
    # after the lock commit, no matching Release exists and the next check heals it.
    build = not scheduled or changed or not published
    publish = main and (event != "workflow_dispatch" or publish_requested)
    return {"build": str(build).lower(), "publish": str(build and publish).lower(),
            "changed": str(changed).lower()}


def main():
    env = os.environ
    event = env["GITHUB_EVENT_NAME"]
    ref = env["GITHUB_REF"]
    repository = env["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    lock_path = ROOT / "upstream.lock.json"
    current = json.loads(lock_path.read_text(encoding="utf-8"))
    candidate_path = ROOT / ".cache/upstream-plan/upstream.lock.json"
    check_requested = env.get("CHECK_UPSTREAM", "false") == "true"
    checking = event == "schedule" or (event == "workflow_dispatch" and check_requested)
    api = upstream.GitHubApi()
    if checking:
        candidate, checked = upstream.check_update(current, api)
        changed = checked["changed"] == "true"
    else:
        candidate, changed = current, False
    for name in ("runtime.lock.json", "runtime-x86_64.lock.json"):
        validate_runtime(candidate, json.loads((ROOT / name).read_text(encoding="utf-8")))
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    if changed:
        upstream.write_candidate(candidate_path, candidate)
    else:
        # Preserve the exact original bytes for publication/retry identity.
        shutil.copyfile(lock_path, candidate_path)
    lock_sha = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    published = False
    if event == "schedule" and not changed:
        releases = api.get(f"repos/{repository}/releases?per_page=100")
        published = already_published(releases, candidate, lock_sha)
    result = make_plan(event, ref, check_requested,
                       env.get("PUBLISH_RELEASE", "true") != "false", changed, published)
    result.update(version=candidate["version"], commit=candidate["commit"], lock_sha256=lock_sha)
    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"Selected pi-pocket **{candidate['version']}** (`{candidate['commit']}`).\n\n")
            handle.write("No upstream update; a matching Release already exists. Build skipped.\n"
                         if result["build"] == "false" else
                         "The selected lock is shared by the device gate and ARM64 build. Publication follows successful checks.\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        print(f"Build planning failed: {exc}", file=sys.stderr)
        sys.exit(1)
