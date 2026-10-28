from __future__ import annotations

import json
from fnmatch import fnmatchcase
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from ai_debug_display.feature_claims import FeatureClaimCoordinator, FeatureClaimError


PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "scripts" / "hold_feature_claim.py"


class FeatureClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copyfile(PROJECT / "feature-ownership.json", self.root / "feature-ownership.json")
        self.coordinator = FeatureClaimCoordinator(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_distinct_features_can_be_claimed_at_the_same_time(self):
        with self.coordinator.claim("display-policy", owner="agent-a"):
            with self.coordinator.claim("private-desktop", owner="agent-b"):
                active = {claim.feature_id for claim in self.coordinator.active_claims()}
                self.assertEqual(active, {"display-policy", "private-desktop"})

    def test_integration_waits_for_feature_claims_and_excludes_new_feature_claims(self):
        with self.coordinator.claim("display-policy", owner="agent-a"):
            with self.assertRaisesRegex(FeatureClaimError, "feature_claim_busy"):
                with self.coordinator.claim("integration", owner="integrator"):
                    self.fail("integration must not overlap a feature edit")
        with self.coordinator.claim("integration", owner="integrator"):
            with self.assertRaisesRegex(FeatureClaimError, "feature_claim_busy"):
                with self.coordinator.claim("broker-protocol", owner="agent-b"):
                    self.fail("feature work must wait for integration")

    def test_cross_process_same_feature_is_exclusive_but_other_feature_is_available(self):
        command = [sys.executable, str(CLI), "--root", str(self.root),
                   "--feature", "display-policy", "--owner", "child", "--hold"]
        held = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            line = held.stdout.readline().strip()
            self.assertEqual(json.loads(line)["state"], "claimed")
            conflict = subprocess.run(
                [sys.executable, str(CLI), "--root", str(self.root), "--feature",
                 "display-policy", "--owner", "second", "--wait", "0"],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(conflict.returncode, 3, conflict.stdout + conflict.stderr)
            self.assertEqual(json.loads(conflict.stdout)["code"],
                             "feature_claim_busy_or_integration_active")
            independent = subprocess.run(
                [sys.executable, str(CLI), "--root", str(self.root), "--feature",
                 "private-desktop", "--owner", "parallel", "--wait", "0"],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(independent.returncode, 0, independent.stdout + independent.stderr)
        finally:
            if held.poll() is None:
                held.stdin.write("\n")
                held.stdin.flush()
                held.wait(timeout=10)
            held.stdout.close()
            held.stderr.close()
            held.stdin.close()

    def test_claim_reports_scoped_files_and_releases_after_exception(self):
        with self.assertRaisesRegex(ValueError, "sentinel"):
            with self.coordinator.claim("guest-lifecycle", owner="codex-task") as claim:
                self.assertEqual(claim.feature_id, "guest-lifecycle")
                self.assertEqual(self.coordinator.feature_files("guest-lifecycle"),
                                 ("ai_debug_display/guest_session.py", "tests/test_guest_session.py"))
                raise ValueError("sentinel")
        self.assertEqual(self.coordinator.active_claims(), ())

    def test_unknown_feature_and_invalid_owner_are_rejected(self):
        for feature, owner, expected in (("not-registered", "agent", "feature_not_registered"),
                                          ("display-policy", "bad\nowner", "claim_owner_invalid")):
            with self.assertRaises(FeatureClaimError) as caught:
                with self.coordinator.claim(feature, owner=owner):
                    pass
            self.assertEqual(caught.exception.code, expected)

    def test_duplicate_file_ownership_is_rejected(self):
        manifest = json.loads((self.root / "feature-ownership.json").read_text(encoding="utf-8"))
        manifest["features"][1]["files"].append(manifest["features"][0]["files"][0])
        (self.root / "feature-ownership.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(FeatureClaimError, "ownership_path_overlap"):
            FeatureClaimCoordinator(self.root)

    def test_overlapping_directory_and_file_claim_patterns_are_rejected(self):
        manifest = json.loads((self.root / "feature-ownership.json").read_text(encoding="utf-8"))
        manifest["features"][0]["files"] = ["drivers/**"]
        (self.root / "feature-ownership.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(FeatureClaimError, "ownership_path_overlap"):
            FeatureClaimCoordinator(self.root)

    def test_code_test_and_script_files_have_a_declared_owner(self):
        manifest = json.loads((PROJECT / "feature-ownership.json").read_text(encoding="utf-8"))
        patterns = [pattern for feature in manifest["features"] for pattern in feature["files"]]
        missing = []
        for base in (PROJECT / "ai_debug_display", PROJECT / "tests", PROJECT / "scripts", PROJECT / "drivers"):
            for path in base.rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                relative = path.relative_to(PROJECT).as_posix()
                if not any((pattern.endswith("/**") and
                            (relative == pattern[:-3].rstrip("/") or
                             relative.startswith(pattern[:-3].rstrip("/") + "/")))
                           or fnmatchcase(relative, pattern) for pattern in patterns):
                    missing.append(relative)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
