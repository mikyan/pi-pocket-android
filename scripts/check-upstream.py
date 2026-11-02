#!/usr/bin/env python3
"""Validate the latest stable pi-pocket release without executing upstream code.

By default this reports a candidate only. --write-lock [PATH] explicitly writes
the validated candidate atomically (PATH defaults to upstream.lock.json).
Authentication is delegated to gh, using GH_TOKEN/GITHUB_TOKEN in Actions.
"""

import argparse
import base64
import binascii
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/TannerMidd/pi-pocket.git"
API_ROOT = "repos/TannerMidd/pi-pocket"
RELEASE_ROOT = "https://github.com/TannerMidd/pi-pocket/releases/tag/"
NUMBER = r"(?:0|[1-9][0-9]{0,8})"
VERSION = re.compile(rf"({NUMBER})\.({NUMBER})\.({NUMBER})\Z")
TAG = re.compile(rf"v?({NUMBER})\.({NUMBER})\.({NUMBER})\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
NODE_MINIMUM = re.compile(rf">=\s*({NUMBER})(?:\.({NUMBER}))?(?:\.({NUMBER}))?\Z")


class UpdateError(ValueError):
    """Metadata is unsafe or incompatible with automatic updates."""


def version_tuple(value, *, tag=False):
    match = (TAG if tag else VERSION).fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise UpdateError("Expected a stable numeric major.minor.patch version")
    return tuple(int(part) for part in match.groups())


def commit_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise UpdateError("Expected a lowercase 40-character commit SHA")
    return value


def minimum_node(value):
    match = NODE_MINIMUM.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise UpdateError("Unsupported engines.node: only a single >= minimum is accepted")
    return ".".join(str(int(part or 0)) for part in match.groups())


def object_value(value, description):
    if not isinstance(value, dict):
        raise UpdateError(f"Expected an object for {description}")
    return value


class GitHubApi:
    def get(self, path):
        # All callers construct paths from constants and validated tags/SHAs.
        try:
            result = subprocess.run(
                ["gh", "api", "--method", "GET", path],
                capture_output=True, text=True, encoding="utf-8", timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("Unable to query GitHub: install/authenticate gh or set GH_TOKEN") from exc
        if result.returncode:
            # Do not copy arbitrary API errors or environment credentials into logs.
            raise UpdateError("GitHub API request failed; check authentication, API limits, and upstream metadata")
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError) as exc:
            raise UpdateError("GitHub API did not return valid JSON") from exc


def resolve_tag(api, tag):
    version_tuple(tag, tag=True)
    ref = object_value(api.get(f"{API_ROOT}/git/ref/tags/{tag}"), "tag reference")
    if ref.get("ref") != f"refs/tags/{tag}":
        raise UpdateError("GitHub returned a different tag reference")
    obj = object_value(ref.get("object"), "tag target")
    seen = set()
    for _ in range(8):
        sha = commit_sha(obj.get("sha"))
        if obj.get("type") == "commit":
            return sha
        if obj.get("type") != "tag" or sha in seen:
            raise UpdateError("Release tag does not resolve to a commit")
        seen.add(sha)
        annotated = object_value(api.get(f"{API_ROOT}/git/tags/{sha}"), "annotated tag")
        if annotated.get("sha") != sha:
            raise UpdateError("Annotated tag SHA does not match requested object")
        obj = object_value(annotated.get("object"), "annotated tag target")
    raise UpdateError("Release tag nesting exceeds the supported limit")


def read_file(api, commit, path):
    entry = object_value(api.get(f"{API_ROOT}/contents/{path}?ref={commit}"), path)
    if entry.get("type") != "file" or entry.get("path") != path:
        raise UpdateError(f"Upstream {path} must be a regular file")
    if entry.get("encoding") != "base64" or not isinstance(entry.get("content"), str):
        raise UpdateError(f"Upstream {path} is missing readable base64 content")
    try:
        content = base64.b64decode("".join(entry["content"].split()), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise UpdateError(f"Upstream {path} has invalid content encoding") from exc
    if not content or len(content) > 2 * 1024 * 1024:
        raise UpdateError(f"Upstream {path} is empty or too large")
    return content


def read_json_file(api, commit, path):
    try:
        return object_value(json.loads(read_file(api, commit, path)), path)
    except (ValueError, UnicodeError) as exc:
        raise UpdateError(f"Upstream {path} is not a valid JSON object") from exc


def validate_source(api, commit, version):
    package = read_json_file(api, commit, "package.json")
    lock = read_json_file(api, commit, "package-lock.json")
    root_package = object_value(object_value(lock.get("packages"), "lock packages").get(""), "lock root package")
    for metadata in (package, lock, root_package):
        if metadata.get("version") != version:
            raise UpdateError("Release tag, package.json, and package-lock.json versions must match")
        if metadata.get("name") != "pi-pocket":
            raise UpdateError("Release package must be named pi-pocket")
    bins = object_value(package.get("bin"), "package bin")
    if bins.get("pi-pocket") != "bin/pi-pocket.js":
        raise UpdateError("Upstream launcher contract has changed")
    read_file(api, commit, "bin/pi-pocket.js")
    engines = object_value(package.get("engines"), "package engines")
    return minimum_node(engines.get("node"))


def check_update(lock, api):
    object_value(lock, "upstream lock")
    if lock.get("schemaVersion") != 1 or lock.get("repository") != REPOSITORY:
        raise UpdateError("Unsupported lock schema or upstream repository")
    current_version = version_tuple(lock.get("version"))
    current_sha = commit_sha(lock.get("commit"))
    version_tuple(lock.get("minimumNode"))
    # GitHub's latest endpoint excludes draft releases and prereleases. Verify
    # these flags ourselves too; a tag that looks like a beta is never accepted.
    release = object_value(api.get(f"{API_ROOT}/releases/latest"), "latest release")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise UpdateError("Automatic updates require a published stable release")
    tag = release.get("tag_name")
    latest_version = version_tuple(tag, tag=True)
    if latest_version < current_version:
        raise UpdateError("Latest stable release is older than the lock; refusing a downgrade")
    version = ".".join(map(str, latest_version))
    commit = resolve_tag(api, tag)
    if latest_version == current_version and commit != current_sha:
        raise UpdateError("The locked release tag moved to another commit; manual review is required")
    node = validate_source(api, commit, version)
    changed = latest_version > current_version
    if not changed and node != lock["minimumNode"]:
        raise UpdateError("Locked minimumNode differs from the immutable upstream source")
    candidate = dict(lock)
    if changed:
        candidate.update(version=version, commit=commit, minimumNode=node,
                         releaseTag=tag, releaseUrl=RELEASE_ROOT + tag)
    outputs = {
        "changed": "true" if changed else "false",
        "current_version": lock["version"],
        "version": version,
        "commit": commit,
        "minimum_node": node,
        "release_tag": tag,
    }
    return candidate, outputs


def safe_candidate_path(value, root=ROOT):
    root = root.resolve()
    requested = Path(value)
    candidate = (requested if requested.is_absolute() else root / requested).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UpdateError("Candidate lock path must stay inside the packaging repository") from exc
    if not relative.parts or any(part.lower() == ".git" for part in relative.parts):
        raise UpdateError("Candidate lock cannot target the repository root or .git")
    if candidate.suffix != ".json":
        raise UpdateError("Candidate lock filename must end in .json")
    if candidate.exists():
        # Explicit overwrite is permitted for a real existing lock, never for
        # arbitrary JSON configuration or source files.
        try:
            existing = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UpdateError("Refusing to overwrite a file that is not an upstream lock") from exc
        if not isinstance(existing, dict) or existing.get("schemaVersion") != 1 or existing.get("repository") != REPOSITORY:
            raise UpdateError("Refusing to overwrite a file that is not an upstream lock")
    return candidate


def write_candidate(path, candidate):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=".upstream-", suffix=".json",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(candidate, handle, indent=2)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "upstream.lock.json", help="Existing lock to compare")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report only; this is the default")
    mode.add_argument("--write-lock", nargs="?", const="upstream.lock.json", metavar="PATH",
                      help="Write validated candidate inside this repository; default: upstream.lock.json")
    parser.add_argument("--github-output", type=Path, help="Append validated scalar outputs to this Actions output file")
    args = parser.parse_args(argv)
    try:
        destination = safe_candidate_path(args.write_lock) if args.write_lock is not None else None
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
        candidate, outputs = check_update(lock, GitHubApi())
        if destination is not None:
            write_candidate(destination, candidate)
        if args.github_output is not None:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as handle:
                for key, value in outputs.items():
                    handle.write(f"{key}={value}\n")
        print(json.dumps(outputs, indent=2))
        return 0
    except (UpdateError, OSError, ValueError) as exc:
        # Metadata validation errors contain fixed descriptions only. Raw API
        # content, release descriptions, tokens, and subprocess stderr are not logged.
        message = str(exc) if isinstance(exc, UpdateError) else "Unable to read or write the requested JSON/output file"
        print(f"Upstream check failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
