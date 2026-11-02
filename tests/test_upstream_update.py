import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("check_upstream", Path(__file__).parents[1] / "scripts/check-upstream.py")
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)

CURRENT = "a" * 40
NEXT = "b" * 40
ANNOTATED = "c" * 40


def lock(version="0.21.0", commit=CURRENT):
    return {"schemaVersion": 1, "repository": upstream.REPOSITORY,
            "version": version, "commit": commit, "minimumNode": "22.19.0"}


class FakeApi:
    def __init__(self, version="0.27.0", commit=NEXT, tag=None, annotated=False):
        tag = tag or "v" + version
        self.calls = []
        self.responses = {
            f"{upstream.API_ROOT}/releases/latest": {"tag_name": tag, "draft": False, "prerelease": False},
            f"{upstream.API_ROOT}/git/ref/tags/{tag}": {
                "ref": f"refs/tags/{tag}",
                "object": {"type": "tag" if annotated else "commit", "sha": ANNOTATED if annotated else commit}},
            f"{upstream.API_ROOT}/git/tags/{ANNOTATED}": {
                "sha": ANNOTATED, "object": {"type": "commit", "sha": commit}},
        }
        self.commit = commit
        self.package = {"name": "pi-pocket", "version": version,
                        "engines": {"node": ">=22.19"}, "bin": {"pi-pocket": "bin/pi-pocket.js"}}
        self.package_lock = {"name": "pi-pocket", "version": version,
                             "packages": {"": {"name": "pi-pocket", "version": version}}}
        self.files = {"bin/pi-pocket.js": b"// fixture only; never executed"}

    def get(self, path):
        self.calls.append(path)
        if path in self.responses:
            return copy.deepcopy(self.responses[path])
        prefix = upstream.API_ROOT + "/contents/"
        if path.startswith(prefix) and path.endswith("?ref=" + self.commit):
            name = path[len(prefix):].split("?", 1)[0]
            data = {"package.json": json.dumps(self.package).encode(),
                    "package-lock.json": json.dumps(self.package_lock).encode(), **self.files}[name]
            return {"type": "file", "path": name, "encoding": "base64", "content": base64.b64encode(data).decode()}
        raise AssertionError("Unexpected API request: " + path)


class UpstreamUpdates(unittest.TestCase):
    def test_no_update_keeps_entire_lock(self):
        current = lock()
        current["customMetadata"] = "preserve"
        candidate, outputs = upstream.check_update(current, FakeApi("0.21.0", CURRENT))
        self.assertEqual(candidate, current)
        self.assertEqual(outputs["changed"], "false")

    def test_higher_stable_release_resolves_annotated_tag_and_preserves_fields(self):
        current = lock()
        current["customMetadata"] = {"keep": True}
        api = FakeApi(annotated=True)
        api.responses[f"{upstream.API_ROOT}/releases/latest"]["html_url"] = "https://untrusted.example/\nchanged=false"
        candidate, outputs = upstream.check_update(current, api)
        self.assertEqual(outputs["changed"], "true")
        self.assertEqual(candidate["version"], "0.27.0")
        self.assertEqual(candidate["commit"], NEXT)
        self.assertEqual(candidate["minimumNode"], "22.19.0")
        self.assertEqual(candidate["customMetadata"], {"keep": True})
        self.assertEqual(candidate["releaseUrl"], upstream.RELEASE_ROOT + "v0.27.0")
        self.assertEqual(current["version"], "0.21.0")

    def test_semver_comparison_is_numeric(self):
        candidate, outputs = upstream.check_update(lock("0.9.0"), FakeApi("0.10.0"))
        self.assertEqual(outputs["changed"], "true")
        self.assertEqual(candidate["version"], "0.10.0")

    def test_draft_or_prerelease_is_rejected(self):
        for flag in ("draft", "prerelease"):
            with self.subTest(flag=flag):
                api = FakeApi()
                api.responses[f"{upstream.API_ROOT}/releases/latest"][flag] = True
                with self.assertRaises(upstream.UpdateError):
                    upstream.check_update(lock(), api)

    def test_beta_and_unsafe_or_ambiguous_tags_are_rejected_before_resolving(self):
        for tag in ("v0.28.0-beta.5", "v1.0.0+build", "v01.0.0", "V1.0.0", "v1.0", "v1.0.0\nchanged=true", "--help", "../v1.0.0", "v1.0.0 ", 123):
            with self.subTest(tag=tag):
                api = FakeApi()
                api.responses[f"{upstream.API_ROOT}/releases/latest"]["tag_name"] = tag
                with self.assertRaises(upstream.UpdateError):
                    upstream.check_update(lock(), api)
                self.assertEqual(len(api.calls), 1)

    def test_downgrade_fails(self):
        with self.assertRaisesRegex(upstream.UpdateError, "downgrade"):
            upstream.check_update(lock("0.30.0"), FakeApi())

    def test_same_version_retag_fails(self):
        with self.assertRaisesRegex(upstream.UpdateError, "tag moved"):
            upstream.check_update(lock(), FakeApi("0.21.0", NEXT))

    def test_package_versions_must_all_match(self):
        for location in ("package", "lock", "root"):
            with self.subTest(location=location):
                api = FakeApi()
                target = {"package": api.package, "lock": api.package_lock,
                          "root": api.package_lock["packages"][""]}[location]
                target["version"] = "0.28.0"
                with self.assertRaisesRegex(upstream.UpdateError, "versions must match"):
                    upstream.check_update(lock(), api)

    def test_node_minimum_is_updated_and_complex_constraints_fail_closed(self):
        api = FakeApi()
        api.package["engines"]["node"] = ">=24.1.2"
        candidate, outputs = upstream.check_update(lock(), api)
        self.assertEqual(candidate["minimumNode"], "24.1.2")
        self.assertEqual(outputs["minimum_node"], "24.1.2")
        for value in ("^22.19", ">=22 <25", ">=22 || >=24", "*", ">22.19", ">=22.19.0-beta", ">=22.19\nkey=bad", None):
            with self.subTest(value=value), self.assertRaises(upstream.UpdateError):
                upstream.minimum_node(value)

    def test_launcher_contract_is_checked(self):
        api = FakeApi()
        api.package["bin"]["pi-pocket"] = "bin/changed.js"
        with self.assertRaisesRegex(upstream.UpdateError, "launcher contract"):
            upstream.check_update(lock(), api)
        api = FakeApi()
        api.files["bin/pi-pocket.js"] = b""
        with self.assertRaises(upstream.UpdateError):
            upstream.check_update(lock(), api)

    def test_unreadable_or_wrong_file_type_fails(self):
        for entry in ({"type": "dir", "path": "bin/pi-pocket.js"},
                      {"type": "file", "path": "another.js", "encoding": "base64", "content": "eA=="},
                      {"type": "file", "path": "bin/pi-pocket.js", "encoding": "none", "content": ""},
                      {"type": "file", "path": "bin/pi-pocket.js", "encoding": "base64", "content": "!not-base64"}):
            with self.subTest(entry=entry):
                api = FakeApi()
                api.responses[f"{upstream.API_ROOT}/contents/bin/pi-pocket.js?ref={NEXT}"] = entry
                with self.assertRaises(upstream.UpdateError):
                    upstream.check_update(lock(), api)

    def test_bad_commit_and_noncommit_tag_target_fail(self):
        for target in ({"type": "commit", "sha": "b" * 39},
                       {"type": "tree", "sha": NEXT}, {"type": "commit", "sha": NEXT + "\noutput=x"}):
            with self.subTest(target=target):
                api = FakeApi()
                api.responses[f"{upstream.API_ROOT}/git/ref/tags/v0.27.0"]["object"] = target
                with self.assertRaises(upstream.UpdateError):
                    upstream.check_update(lock(), api)

    def test_annotated_tag_cycle_fails(self):
        api = FakeApi(annotated=True)
        api.responses[f"{upstream.API_ROOT}/git/tags/{ANNOTATED}"]["object"] = {"type": "tag", "sha": ANNOTATED}
        with self.assertRaisesRegex(upstream.UpdateError, "resolve to a commit"):
            upstream.check_update(lock(), api)

    def test_existing_lock_fields_are_validated(self):
        for key, value in (("repository", "https://evil.example/repo.git"), ("schemaVersion", 2),
                           ("version", "0.21.0\nchanged=true"), ("minimumNode", "22.19")):
            with self.subTest(key=key):
                current = lock()
                current[key] = value
                with self.assertRaises(upstream.UpdateError):
                    upstream.check_update(current, FakeApi())

    def test_safe_output_stays_in_repository_and_preserves_unrelated_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in ("../outside.json", ".git/config.json", ".", "script.py"):
                with self.subTest(value=value), self.assertRaises(upstream.UpdateError):
                    upstream.safe_candidate_path(value, root)
            (root / "package.json").write_text('{"name":"other"}', encoding="utf-8")
            with self.assertRaises(upstream.UpdateError):
                upstream.safe_candidate_path("package.json", root)
            destination = upstream.safe_candidate_path(".cache/candidate.json", root)
            upstream.write_candidate(destination, lock())
            self.assertEqual(json.loads(destination.read_text()), lock())
            self.assertEqual(upstream.safe_candidate_path(".cache/candidate.json", root), destination)

    def test_default_check_is_read_only_and_actions_outputs_are_single_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            current_path = Path(directory) / "upstream.lock.json"
            current_path.write_text(json.dumps(lock()), encoding="utf-8")
            original = current_path.read_bytes()
            output = Path(directory) / "github-output"
            with patch.object(upstream, "GitHubApi", return_value=FakeApi()), patch("sys.stdout", new=io.StringIO()):
                result = upstream.main(["--lock", str(current_path), "--github-output", str(output)])
            self.assertEqual(result, 0)
            self.assertEqual(current_path.read_bytes(), original)
            self.assertEqual(output.read_text().splitlines(), ["changed=true", "current_version=0.21.0",
                "version=0.27.0", "commit=" + NEXT, "minimum_node=22.19.0", "release_tag=v0.27.0"])

    def test_failed_validation_does_not_write_outputs_or_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            current_path = Path(directory) / "upstream.lock.json"
            current_path.write_text(json.dumps(lock()), encoding="utf-8")
            output = Path(directory) / "github-output"
            candidate = Path(directory) / "candidate.json"
            with patch.object(upstream, "safe_candidate_path", return_value=candidate), \
                 patch.object(upstream, "GitHubApi", return_value=FakeApi("0.21.0", NEXT)), \
                 patch("sys.stderr", new=io.StringIO()):
                result = upstream.main(["--lock", str(current_path), "--write-lock", str(candidate), "--github-output", str(output)])
            self.assertEqual(result, 1)
            self.assertFalse(output.exists())
            self.assertFalse(candidate.exists())

    def test_explicit_write_saves_candidate_without_changing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            current_path = Path(directory) / "upstream.lock.json"
            current_path.write_text(json.dumps(lock()), encoding="utf-8")
            original = current_path.read_bytes()
            candidate = Path(directory) / "candidate.json"
            with patch.object(upstream, "safe_candidate_path", return_value=candidate), \
                 patch.object(upstream, "GitHubApi", return_value=FakeApi()), \
                 patch("sys.stdout", new=io.StringIO()):
                result = upstream.main(["--lock", str(current_path), "--write-lock", str(candidate)])
            self.assertEqual(result, 0)
            self.assertEqual(current_path.read_bytes(), original)
            self.assertEqual(json.loads(candidate.read_text())["version"], "0.27.0")


if __name__ == "__main__":
    unittest.main()
