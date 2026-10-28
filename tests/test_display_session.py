from __future__ import annotations

import unittest
import threading

from ai_debug_display.models import AdapterFeatures, Output, QaTarget, SessionRequest, Topology, WindowObservation
from ai_debug_display.policy import PolicyDenied, validate_session
from ai_debug_display.service import DisplaySessionService


class FakeAdapter:
    features = AdapterFeatures(True, True, True, True, True, True, True, True, True, True)

    def __init__(self):
        self.begins = []
        self.ends = []
        self.routes = []
        self.route_result = True
        self.begin_entered = threading.Event()
        self.begin_release = threading.Event()
        self.block_begin = False
        self.window_callback = None
        self.fail_begin = False
        self.fail_route = False
        self.fail_end = False
        self.block_end = False
        self.end_entered = threading.Event()
        self.end_release = threading.Event()

    def begin(self, request, target, topology, on_window_event):
        self.window_callback = on_window_event
        self.begin_entered.set()
        if self.block_begin:
            if not self.begin_release.wait(2):
                raise TimeoutError("fake begin timeout")
        if self.fail_begin:
            raise OSError("synthetic setup failure")
        self.begins.append(request.run_id)

    def emit_window(self, observation):
        return self.window_callback(observation)

    def route_foreign_window(self, hwnd, pid, output_id, topology_fingerprint):
        self.routes.append((hwnd, pid, output_id, topology_fingerprint))
        if self.fail_route:
            raise OSError("synthetic route failure")
        return self.route_result

    def end(self, run_id):
        self.end_entered.set()
        if self.block_end and not self.end_release.wait(2):
            raise TimeoutError("synthetic cleanup timeout")
        if self.fail_end:
            raise OSError("synthetic cleanup failure")
        self.ends.append(run_id)


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def fixture(*, surface="host", outputs=None, hwnd_pids=(501,)):
    target = QaTarget("qa-17", "sample-app", 501, (9001,), hwnd_pids, surface, True)
    topology = Topology(outputs or (
        Output("VDD-A", "virtual", True, True, True, False, ((1280, 720), (1920, 1080))),
        Output("MON-A", "physical", True, True, True, True, ((2560, 1440),)),
    ), "topology-abc", 44)
    request = SessionRequest("qa-17", "sample-app", "VDD-A", "MON-A", (1280, 720), 125)
    return target, topology, request


def service(target, topology, adapter=None, clock=None):
    return DisplaySessionService(target_lookup=lambda run_id: target if run_id == target.run_id else None,
                                 topology_lookup=lambda: topology, adapter=adapter,
                                 clock=clock or FakeClock())


class DisplaySessionTests(unittest.TestCase):
    def test_default_adapter_is_inert_and_denies_start(self):
        target, topology, request = fixture()
        subject = service(target, topology)
        with self.assertRaisesRegex(PolicyDenied, "필수 창 단위"):
            subject.start(request, explicitly_confirmed=True)
        self.assertEqual(subject.status().state, "stopped")

    def test_explicit_confirmation_required(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        with self.assertRaises(PolicyDenied) as error:
            service(target, topology, adapter).start(request, explicitly_confirmed=False)
        self.assertEqual(error.exception.code, "confirmation_required")
        self.assertEqual(adapter.begins, [])

    def test_private_desktop_is_not_a_host_virtual_display(self):
        target, topology, request = fixture(surface="private-desktop")
        with self.assertRaises(PolicyDenied) as error:
            service(target, topology, FakeAdapter()).start(request, explicitly_confirmed=True)
        self.assertEqual(error.exception.code, "surface_incompatible")

    def test_session_uses_only_exact_process_owned_windows_and_records_no_content(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        subject = service(target, topology, adapter)
        result = subject.start(request, explicitly_confirmed=True)
        self.assertEqual(result["allowed_hwnds"], [9001])
        self.assertEqual(subject.status().state, "running")
        self.assertTrue(subject.stop("qa-17"))
        self.assertEqual(adapter.ends, ["qa-17"])
        self.assertNotIn("title", subject.events()[0].__dict__)

    def test_foreign_window_is_returned_and_qa_window_is_kept(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        subject = service(target, topology, adapter)
        subject.start(request, explicitly_confirmed=True)
        self.assertEqual(subject.guard(WindowObservation(9001, 501, "VDD-A", "qa-17", "sample-app")), "keep_on_virtual")
        self.assertEqual(subject.guard(WindowObservation(7002, 811, "VDD-A", None, "photoshop")), "returned")
        self.assertEqual(adapter.routes, [(7002, 811, "MON-A", "topology-abc")])

    def test_window_created_event_keeps_photoshop_on_physical_output(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        subject = service(target, topology, adapter)
        subject.start(request, explicitly_confirmed=True)
        # Simulate a Photoshop top-level window appearing on the VDD while QA is active.
        outcome = adapter.emit_window(WindowObservation(8120, 830, "VDD-A", None, "photoshop"))
        self.assertEqual(outcome, "returned")
        self.assertEqual(adapter.routes, [(8120, 830, "MON-A", "topology-abc")])

    def test_non_target_app_is_returned_even_when_pid_and_hwnd_match(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        subject = service(target, topology, adapter)
        subject.start(request, explicitly_confirmed=True)
        result = subject.guard(WindowObservation(9001, 501, "VDD-A", "qa-17", "photoshop"))
        self.assertEqual(result, "returned")
        self.assertEqual(adapter.routes, [(9001, 501, "MON-A", "topology-abc")])

    def test_topology_change_fails_closed_without_moving_foreign_window(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        current = [topology]
        subject = DisplaySessionService(target_lookup=lambda _: target,
                                        topology_lookup=lambda: current[0], adapter=adapter)
        subject.start(request, explicitly_confirmed=True)
        current[0] = Topology(topology.outputs, "topology-changed", topology.foreground_hwnd)
        self.assertEqual(subject.guard(WindowObservation(7002, 811, "VDD-A", None, "photoshop")), "block_and_alert")
        self.assertEqual(adapter.routes, [])
        self.assertEqual(subject.status().state, "stopped")

    def test_unqualified_or_primary_virtual_output_is_denied(self):
        target, topology, request = fixture()
        unqualified = AdapterFeatures(installed=True)
        with self.assertRaises(PolicyDenied) as error:
            validate_session(request, target, topology, unqualified, explicitly_confirmed=True)
        self.assertEqual(error.exception.code, "adapter_unqualified")
        primary = Topology((Output("VDD-A", "virtual", True, True, True, True, ((1280, 720),)), topology.outputs[1]), "topology-abc", 44)
        with self.assertRaises(PolicyDenied) as error2:
            validate_session(request, target, primary, FakeAdapter.features, explicitly_confirmed=True)
        self.assertEqual(error2.exception.code, "virtual_output_primary")

    def test_only_advertised_mode_and_bounded_scale_are_accepted(self):
        target, topology, request = fixture()
        bad_mode = SessionRequest("qa-17", "sample-app", "VDD-A", "MON-A", (800, 600), 125)
        with self.assertRaises(PolicyDenied) as error:
            validate_session(bad_mode, target, topology, FakeAdapter.features, explicitly_confirmed=True)
        self.assertEqual(error.exception.code, "mode_unsupported")
        bad_scale = SessionRequest("qa-17", "sample-app", "VDD-A", "MON-A", (1280, 720), 250)
        with self.assertRaises(PolicyDenied) as error2:
            validate_session(bad_scale, target, topology, FakeAdapter.features, explicitly_confirmed=True)
        self.assertEqual(error2.exception.code, "scale_out_of_range")

    def test_idle_timeout_waits_for_active_operation_then_stops_after_five_minutes(self):
        target, topology, request = fixture()
        adapter, clock = FakeAdapter(), FakeClock()
        subject = service(target, topology, adapter, clock)
        subject.start(request, explicitly_confirmed=True)
        with subject.operation("qa-17") as allowed:
            self.assertTrue(allowed)
            clock.advance(600)
            self.assertEqual(subject.sweep_idle(), [])
        clock.advance(299)
        self.assertEqual(subject.sweep_idle(), [])
        clock.advance(1)
        self.assertEqual(subject.sweep_idle(), ["qa-17"])
        self.assertEqual(adapter.ends, ["qa-17"])

    def test_stop_during_start_cancels_lease_and_cleans_adapter_once(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        adapter.block_begin = True
        subject = service(target, topology, adapter)
        errors = []

        def begin():
            try:
                subject.start(request, explicitly_confirmed=True)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=begin)
        worker.start()
        self.assertTrue(adapter.begin_entered.wait(2))
        self.assertFalse(subject.stop("qa-17", wait_seconds=0))
        adapter.begin_release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)
        self.assertEqual(subject.status().state, "stopped")
        self.assertEqual(adapter.ends, ["qa-17"])

    def test_new_session_can_start_after_previous_session_stops(self):
        target, topology, request = fixture()
        second_target = QaTarget("qa-18", "sample-app", 502, (9002,), (502,), "host", True)
        second_request = SessionRequest("qa-18", "sample-app", "VDD-A", "MON-A", (1920, 1080), 100)
        targets = {target.run_id: target, second_target.run_id: second_target}
        adapter = FakeAdapter()
        subject = DisplaySessionService(target_lookup=targets.get,
                                        topology_lookup=lambda: topology, adapter=adapter)
        subject.start(request, explicitly_confirmed=True)
        self.assertTrue(subject.stop("qa-17"))
        self.assertEqual(subject.start(second_request, explicitly_confirmed=True)["run_id"], "qa-18")
        self.assertEqual(adapter.begins, ["qa-17", "qa-18"])

    def test_start_failure_cleans_partial_adapter_once_and_leaves_no_running_session(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        adapter.fail_begin = True
        subject = service(target, topology, adapter)
        with self.assertRaises(PolicyDenied) as error:
            subject.start(request, explicitly_confirmed=True)
        self.assertEqual(error.exception.code, "adapter_start_failed")
        self.assertEqual(adapter.ends, ["qa-17"])
        self.assertEqual(subject.status().state, "stopped")

    def test_failed_foreign_window_route_stops_session_without_escaping_callback(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        adapter.fail_route = True
        subject = service(target, topology, adapter)
        subject.start(request, explicitly_confirmed=True)
        self.assertEqual(adapter.emit_window(WindowObservation(8120, 830, "VDD-A", None, "photoshop")), "blocked")
        self.assertEqual(subject.status().state, "stopped")
        self.assertEqual(adapter.ends, ["qa-17"])

    def test_cleanup_failure_is_visible_and_blocks_a_new_session(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        adapter.fail_end = True
        subject = service(target, topology, adapter)
        subject.start(request, explicitly_confirmed=True)
        self.assertFalse(subject.stop("qa-17"))
        self.assertEqual(subject.status().state, "failed")
        with self.assertRaises(PolicyDenied) as error:
            subject.start(request, explicitly_confirmed=True)
        self.assertEqual(error.exception.code, "session_busy")

    def test_hung_cleanup_is_bounded_and_keeps_session_fenced_until_it_finishes(self):
        target, topology, request = fixture()
        adapter = FakeAdapter()
        adapter.block_end = True
        subject = DisplaySessionService(target_lookup=lambda _: target,
                                        topology_lookup=lambda: topology,
                                        adapter=adapter, cleanup_timeout_seconds=0.05)
        subject.start(request, explicitly_confirmed=True)
        self.assertFalse(subject.stop("qa-17"))
        self.assertTrue(adapter.end_entered.is_set())
        self.assertEqual(subject.status().state, "stopping")
        adapter.end_release.set()
        self.assertTrue(subject.stop("qa-17"))
        self.assertEqual(subject.status().state, "stopped")
        self.assertEqual(adapter.ends, ["qa-17"])


if __name__ == "__main__":
    unittest.main()
