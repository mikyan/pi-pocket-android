import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
        Path(__file__).parents[1] / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = script("plan-build")
collect = script("collect-release")


class ReleasePlanning(unittest.TestCase):
    def test_schedule_no_change_skips_only_when_published(self):
        self.assertEqual(plan.make_plan("schedule", "refs/heads/main", False, True, False, True)["build"], "false")
        repair = plan.make_plan("schedule", "refs/heads/main", False, True, False, False)
        self.assertEqual((repair["build"], repair["publish"]), ("true", "true"))

    def test_new_stable_builds_and_manual_can_disable_release(self):
        self.assertEqual(plan.make_plan("schedule", "refs/heads/main", False, True, True, True)["publish"], "true")
        result = plan.make_plan("workflow_dispatch", "refs/heads/main", True, False, True, False)
        self.assertEqual((result["build"], result["publish"]), ("true", "false"))

    def test_pull_request_and_other_branches_never_publish(self):
        for event, ref in (("pull_request", "refs/heads/main"), ("workflow_dispatch", "refs/heads/other")):
            self.assertEqual(plan.make_plan(event, ref, True, True, True, False)["publish"], "false")

    def test_draft_and_mismatched_commit_do_not_suppress_retry(self):
        marker = {"schemaVersion": 1, "upstreamCommit": "a" * 40, "upstreamLockSha256": "b" * 64}
        release = {"draft": False, "prerelease": False, "body": "<!-- pi-pocket-android " + json.dumps(marker) + " -->"}
        lock = {"commit": "a" * 40}
        self.assertTrue(plan.already_published([release], lock, "b" * 64))
        for field in ("draft", "prerelease"):
            self.assertFalse(plan.already_published([{**release, field: True}], lock, "b" * 64))
        self.assertFalse(plan.already_published([release], lock, "c" * 64))
        self.assertFalse(plan.already_published([release], {"commit": "c" * 40}, "b" * 64))

    def test_node_requirement_cannot_outrun_either_android_runtime(self):
        runtime = {"packages": [{"Package": "nodejs-lts", "Version": "24.22.2-1"}]}
        plan.validate_runtime({"minimumNode": "22.19.0"}, runtime)
        with self.assertRaises(ValueError):
            plan.validate_runtime({"minimumNode": "26.0.0"}, runtime)


class ReleaseCollection(unittest.TestCase):
    def test_metadata_and_hashes_bind_every_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, content in {
                "app/build/outputs/apk/debug/app-debug.apk": b"fixture APK",
                "app/src/main/assets/payload.json": b"{}",
                "upstream.lock.json": json.dumps({"version": "0.27.0", "commit": "c" * 40}).encode(),
                "runtime.lock.json": b"{}",
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            env = {"GITHUB_RUN_NUMBER": "3", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                   "GITHUB_SHA": "a" * 40, "GITHUB_REPOSITORY": "example/repo",
                   "POCKET_VERSION_CODE": "1003", "POCKET_VERSION_NAME": "0.2.0-ci.3"}
            metadata = collect.collect(root, env)
            self.assertEqual(metadata["versionCode"], 1003)
            self.assertEqual(metadata["baseSha"], "a" * 40)
            rows = (root / "dist/SHA256SUMS.txt").read_text().splitlines()
            self.assertEqual(len(rows), 6)
            for row in rows:
                sha, name = row.split("  ", 1)
                self.assertEqual(sha, collect.digest(root / "dist" / name))
            with self.assertRaises(ValueError):
                collect.collect(root, env)


if __name__ == "__main__":
    unittest.main()
