from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from ai_debug_display.evolution import EvolutionError, EvolutionModuleHost
from ai_debug_display.models import (
    AdapterFeatures, Output, QaTarget, SessionRequest, Topology, WindowObservation,
)
from ai_debug_display.service import DisplaySessionService


PROJECT = Path(__file__).resolve().parents[1]
REAL_POLICY = PROJECT / "ai_debug_display" / "modules" / "display_policy.py"
MANIFEST_TEMPLATE = {
    "api_version": 1,
    "watch_allowlist": [],
    "modules": [{
        "id": "display-policy",
        "path": "modules/display_policy.py",
        "version": "1.0.0",
        "api_version": 1,
        "dependencies": [],
        "reloadable": True,
        "restart_required": False,
        "auto_watch": False,
        "lifecycle": {
            "initialize": "initialize",
            "dispose": "dispose",
            "snapshot_state": "snapshot_state",
            "restore_state": "restore_state",
        },
    }],
}

STATEFUL_POLICY = '''MODULE_ID = "display-policy"
VERSION = "1.0.0"
API_VERSION = 1
def initialize(host, api):
    if api != API_VERSION:
        raise RuntimeError("api")
    return {}
def dispose(host, handle):
    return None
def snapshot_state(host, handle):
    return {"value": getattr(host, "test_value", None)}
def restore_state(host, handle, state):
    host.test_value = state["value"]
def validate_session(*args, **kwargs):
    return (), None, None
def route_window(*args, **kwargs):
    return {"version": VERSION}
'''


class FakeAdapter:
    features = AdapterFeatures(True, True, True, True, True, True, True, True, True, True)

    def __init__(self):
        self.callback = None
        self.ended = []

    def begin(self, request, target, topology, on_window_event):
        self.callback = on_window_event

    def route_foreign_window(self, hwnd, pid, output_id, topology_fingerprint):
        return True

    def end(self, run_id):
        self.ended.append(run_id)


class EvolutionModuleHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "modules").mkdir()
        shutil.copyfile(PROJECT / "module-manifest.json", self.root / "module-manifest.json")
        shutil.copyfile(REAL_POLICY, self.root / "modules" / "display_policy.py")

    def tearDown(self):
        self.temp.cleanup()

    def make_host(self, blockers=None):
        return EvolutionModuleHost(self.root, blockers=blockers)

    def edit_candidate(self, source_text=None, *, version="1.0.1", from_version="1.0.0"):
        module_path = self.root / "modules" / "display_policy.py"
        original = module_path.read_text(encoding="utf-8")
        if source_text is None:
            source_text = original.replace(
                f'VERSION = "{from_version}"', f'VERSION = "{version}"', 1)
        elif f'VERSION = "{version}"' not in source_text:
            source_text = source_text.replace(
                f'VERSION = "{from_version}"', f'VERSION = "{version}"', 1)
        module_path.write_text(source_text, encoding="utf-8")
        manifest_path = self.root / "module-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["modules"][0]["version"] = version
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return source_text

    def test_real_policy_module_reloads_through_session_service_only_when_stopped(self):
        service_ref = [None]
        host = self.make_host(
            blockers=lambda module_id: service_ref[0].module_reload_blockers(module_id)
            if service_ref[0] is not None else ())
        target = QaTarget(
            "qa-17", "sample-app", 501, (9001,), (501,), "host", True)
        topology = Topology((
            Output("VDD-A", "virtual", True, True, True, False,
                   ((1280, 720), (1920, 1080))),
            Output("MON-A", "physical", True, True, True, True, ((2560, 1440),)),
        ), "topology-abc", 44)
        request = SessionRequest("qa-17", "sample-app", "VDD-A", "MON-A",
                                 (1280, 720), 125)
        adapter = FakeAdapter()
        service = DisplaySessionService(
            target_lookup=lambda run_id: target if run_id == target.run_id else None,
            topology_lookup=lambda: topology, adapter=adapter, policy=host)
        service_ref[0] = service

        service.start(request, explicitly_confirmed=True)
        observation = WindowObservation(9001, 501, "VDD-A", "qa-17", "sample-app")
        self.assertEqual(service.guard(observation), "keep_on_virtual")

        changed = (self.root / "modules" / "display_policy.py").read_text(encoding="utf-8")
        changed = changed.replace(
            'RouteDecision("keep_on_virtual", "exact_qa_window", observation.hwnd, observation.pid)',
            'RouteDecision("block_and_alert", "synthetic_hot_reload", observation.hwnd, observation.pid)',
            1)
        self.edit_candidate(changed)
        candidate = host.stage()
        with self.assertRaisesRegex(EvolutionError, "active_work_blocked"):
            host.apply(candidate)
        self.assertEqual(host.status().version, "1.0.0")
        self.assertEqual(host.status().generation, 1)

        self.assertTrue(service.stop("qa-17"))
        host.apply(candidate)
        self.assertEqual(host.status().version, "1.0.1")
        self.assertEqual(host.status().generation, 2)
        service.start(request, explicitly_confirmed=True)
        self.assertEqual(service.guard(observation), "block_and_alert")
        self.assertEqual(service.status().state, "stopped")
        host.close()

    def test_state_snapshot_and_restore_survive_successful_reload(self):
        (self.root / "modules" / "display_policy.py").write_text(
            STATEFUL_POLICY, encoding="utf-8")
        manifest = json.loads((self.root / "module-manifest.json").read_text(encoding="utf-8"))
        (self.root / "module-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        host = self.make_host()
        host.test_value = {"draft": "kept"}
        candidate_source = STATEFUL_POLICY.replace(
            'VERSION = "1.0.0"', 'VERSION = "1.0.1"', 1)
        self.edit_candidate(candidate_source)
        host.apply(host.stage())
        self.assertEqual(host.test_value, {"draft": "kept"})
        self.assertEqual(host.status().version, "1.0.1")
        host.close()

    def test_failed_candidate_init_restores_old_source_module_and_manifest(self):
        host = self.make_host()
        original_source = (self.root / "modules" / "display_policy.py").read_bytes()
        original_manifest = (self.root / "module-manifest.json").read_bytes()
        before_hash = host.status().source_sha256
        changed = self.edit_candidate()
        changed = changed.replace(
            'def initialize(host, api):',
            'def initialize(host, api):\n    raise RuntimeError("synthetic init failure")',
            1)
        (self.root / "modules" / "display_policy.py").write_text(changed, encoding="utf-8")
        candidate = host.stage()
        with self.assertRaisesRegex(EvolutionError, "candidate_init_or_restore_failed"):
            host.apply(candidate)
        self.assertEqual((self.root / "modules" / "display_policy.py").read_bytes(), original_source)
        self.assertEqual((self.root / "module-manifest.json").read_bytes(), original_manifest)
        self.assertEqual(host.status().source_sha256, before_hash)
        self.assertEqual(host.status().version, "1.0.0")
        self.assertEqual(host.status().state, "ready")
        host.close()

    def test_previous_verified_version_can_be_rolled_back(self):
        host = self.make_host()
        original_hash = host.status().source_sha256
        self.edit_candidate()
        host.apply(host.stage())
        self.assertEqual(host.status().version, "1.0.1")
        status = host.rollback(original_hash)
        self.assertEqual(status.version, "1.0.0")
        self.assertEqual(status.generation, 3)
        self.assertEqual(status.source_sha256, original_hash)
        host.close()

    def test_candidate_stale_after_manifest_changes(self):
        host = self.make_host()
        self.edit_candidate()
        candidate = host.stage()
        self.edit_candidate(version="1.0.2", from_version="1.0.1")
        with self.assertRaisesRegex(EvolutionError, "candidate_generation_stale|candidate_manifest_stale"):
            host.apply(candidate)
        self.assertEqual(host.status().version, "1.0.0")
        self.assertEqual(host.status().state, "ready")
        host.close()

    def test_failed_version_and_active_blockers_do_not_apply(self):
        blocker_list = []
        host = self.make_host(blockers=lambda _module_id: tuple(blocker_list))
        changed = self.edit_candidate()
        candidate = host.stage()
        blocker_list.append("provider_request_active")
        with self.assertRaisesRegex(EvolutionError, "active_work_blocked"):
            host.apply(candidate)
        self.assertEqual(host.status().version, "1.0.0")
        blocker_list.clear()
        host.apply(candidate)
        host.close()

    def test_stale_module_generation_is_rejected(self):
        host = self.make_host()
        self.edit_candidate()
        first = host.stage()
        host.apply(first)
        with self.assertRaisesRegex(EvolutionError, "candidate_generation_stale"):
            host.apply(first)
        host.close()

    def test_lifecycle_operation_fences_concurrent_apply(self):
        host = self.make_host()
        self.edit_candidate()
        candidate = host.stage()
        with host.lifecycle_operation():
            with self.assertRaisesRegex(EvolutionError, "policy_calls_did_not_drain"):
                host.apply(candidate, blocker_timeout_seconds=0.01)
        self.assertEqual(host.status().version, "1.0.0")
        host.close()

    def test_watchers_are_disabled_and_duplicate_manifest_keys_rejected(self):
        host = self.make_host()
        self.assertFalse(host.status().auto_watch)
        self.assertEqual(json.loads((self.root / "module-manifest.json").read_text())["watch_allowlist"], [])
        host.close()
        raw = (self.root / "module-manifest.json").read_text(encoding="utf-8")
        (self.root / "module-manifest.json").write_text(
            raw.replace('"api_version": 1,', '"api_version": 1,\n  "api_version": 1,', 1),
            encoding="utf-8")
        with self.assertRaisesRegex(EvolutionError, "duplicate_manifest_key"):
            self.make_host()

    def test_manifest_path_traversal_is_rejected(self):
        manifest_path = self.root / "module-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["modules"][0]["path"] = "../outside.py"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(EvolutionError, "module_path_not_allowlisted"):
            self.make_host()


if __name__ == "__main__":
    unittest.main()

