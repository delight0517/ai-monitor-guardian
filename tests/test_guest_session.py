from __future__ import annotations

import threading
import unittest

from ai_debug_display.guest_session import (
    APPROVED_VM_NAME, GuestSessionController, GuestSessionError, GuestSnapshot,
)


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeGuestBackend:
    def __init__(self):
        self.state = "Off"
        self.run_id = None
        self.network_adapter_count = 0
        self.guest_service_interface_enabled = False
        self.calls = []
        self.fail = set()
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.block_start = False
        self.ready_cancel_wait = threading.Event()

    def inspect(self, vm_name):
        self.calls.append(("inspect", vm_name))
        return GuestSnapshot(vm_name, self.state, self.state == "Running",
                             self.network_adapter_count,
                             self.guest_service_interface_enabled,
                             True, True, True, self.run_id)

    def start_guest(self, vm_name, run_id):
        self.calls.append(("start", vm_name, run_id))
        self.start_entered.set()
        if self.block_start:
            self.start_release.wait(2)
        if "start" in self.fail:
            raise OSError("synthetic start failure")
        self.state, self.run_id = "Running", run_id

    def wait_guest_ready(self, vm_name, run_id, timeout, cancelled):
        self.calls.append(("ready", vm_name, run_id))
        if "ready_block" in self.fail:
            self.ready_cancel_wait.wait(timeout)
            return not cancelled.is_set()
        return "ready" not in self.fail and not cancelled.is_set()

    def enable_display(self, vm_name, run_id):
        self.calls.append(("display_on", vm_name, run_id))
        if "display_on" in self.fail:
            raise OSError("synthetic output failure")

    def disable_display(self, vm_name, run_id):
        self.calls.append(("display_off", vm_name, run_id))
        if "display_off" in self.fail:
            raise OSError("synthetic output teardown failure")

    def request_guest_shutdown(self, vm_name, run_id):
        self.calls.append(("shutdown", vm_name, run_id))
        if "shutdown" in self.fail:
            raise OSError("synthetic graceful shutdown failure")

    def wait_guest_off(self, vm_name, timeout):
        self.calls.append(("wait_off", vm_name))
        if "off" in self.fail:
            return False
        self.state, self.run_id = "Off", None
        return True


def make_controller(backend=None, clock=None):
    backend = backend or FakeGuestBackend()
    clock = clock or FakeClock()
    return GuestSessionController(backend, idle_timeout_seconds=300,
                                  startup_timeout_seconds=0.1,
                                  shutdown_timeout_seconds=0.1,
                                  operation_timeout_seconds=0.1,
                                  clock=clock), backend, clock


class GuestSessionTests(unittest.TestCase):
    def test_requires_explicit_user_confirmation(self):
        subject, backend, _ = make_controller()
        with self.assertRaisesRegex(GuestSessionError, "confirmation_required"):
            subject.start("run-a", explicitly_confirmed=False)
        self.assertEqual(backend.calls, [])

    def test_start_is_scoped_and_stop_requests_graceful_guest_shutdown(self):
        subject, backend, _ = make_controller()
        status = subject.start("run-a", explicitly_confirmed=True)
        self.assertEqual(status.state, "running")
        self.assertTrue(status.display_enabled)
        self.assertTrue(subject.stop("run-a"))
        self.assertEqual(subject.status().state, "stopped")
        self.assertIn(("shutdown", APPROVED_VM_NAME, "run-a"), backend.calls)
        self.assertLess(backend.calls.index(("display_off", APPROVED_VM_NAME, "run-a")),
                        backend.calls.index(("shutdown", APPROVED_VM_NAME, "run-a")))
        self.assertEqual(backend.state, "Off")

    def test_wrong_vm_identity_network_or_guest_sharing_fail_closed(self):
        for mutation, code in (
            (lambda s: GuestSnapshot("OtherVM", "Off", False, 0, False, True, True, True), "unapproved_vm_identity"),
            (lambda s: GuestSnapshot(s.vm_name, "Off", False, 1, False, True, True, True), "guest_network_not_isolated"),
            (lambda s: GuestSnapshot(s.vm_name, "Off", False, 0, True, True, True, True), "guest_file_integration_enabled"),
            (lambda s: GuestSnapshot(s.vm_name, "Off", False, 0, False, False, True, True), "guest_secure_boot_disabled"),
            (lambda s: GuestSnapshot(s.vm_name, "Off", False, 0, False, True, False, True), "guest_tpm_disabled"),
            (lambda s: GuestSnapshot(s.vm_name, "Off", False, 0, False, True, True, False), "guest_shutdown_unavailable"),
        ):
            subject, backend, _ = make_controller()
            original = backend.inspect
            backend.inspect = lambda name, m=mutation, o=original: m(o(name))
            with self.assertRaisesRegex(GuestSessionError, code):
                subject.start("run-a", explicitly_confirmed=True)
            self.assertFalse(any(c[0] == "start" for c in backend.calls))

    def test_unknown_running_vm_and_stale_run_are_not_reused(self):
        subject, backend, _ = make_controller()
        backend.state = "Running"
        with self.assertRaisesRegex(GuestSessionError, "unexpected_vm_state"):
            subject.start("run-a", explicitly_confirmed=True)
        backend.state, backend.run_id = "Off", "old-run"
        with self.assertRaisesRegex(GuestSessionError, "stale_guest_lease"):
            subject.start("run-a", explicitly_confirmed=True)

    def test_wrong_heartbeat_cannot_refresh_lease_and_idle_sweep_stops_it(self):
        subject, backend, clock = make_controller()
        subject.start("run-a", explicitly_confirmed=True)
        clock.advance(299)
        self.assertFalse(subject.heartbeat("other-run"))
        self.assertEqual(subject.status().idle_seconds, 299)
        clock.advance(1)
        self.assertTrue(subject.sweep_idle())
        self.assertEqual(subject.status().shutdown_reason, "idle_timeout")
        self.assertEqual(backend.state, "Off")

    def test_heartbeat_at_deadline_does_not_revive_expired_lease(self):
        subject, _, clock = make_controller()
        subject.start("run-a", explicitly_confirmed=True)
        clock.advance(300)
        self.assertFalse(subject.heartbeat("run-a"))
        self.assertTrue(subject.sweep_idle())
        self.assertEqual(subject.status().state, "stopped")

    def test_stop_wins_while_guest_start_is_in_flight(self):
        subject, backend, _ = make_controller()
        backend.block_start = True
        errors = []
        worker = threading.Thread(target=lambda: self._try_start(subject, errors))
        worker.start()
        self.assertTrue(backend.start_entered.wait(1))
        stopped = threading.Thread(target=lambda: subject.stop("run-a", wait_seconds=1))
        stopped.start()
        backend.start_release.set()
        worker.join(2)
        stopped.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(stopped.is_alive())
        self.assertTrue(errors)
        self.assertEqual(subject.status().state, "stopped")
        self.assertEqual([c[0] for c in backend.calls].count("display_on"), 0)

    def test_start_finishes_cleanup_even_if_stop_wait_budget_expires(self):
        subject, backend, _ = make_controller()
        backend.block_start = True
        errors = []
        worker = threading.Thread(target=lambda: self._try_start(subject, errors))
        worker.start()
        self.assertTrue(backend.start_entered.wait(1))
        self.assertFalse(subject.stop("run-a", wait_seconds=0))
        backend.start_release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)
        self.assertEqual(subject.status().state, "stopped")
        self.assertEqual(backend.state, "Off")

    @staticmethod
    def _try_start(subject, errors):
        try:
            subject.start("run-a", explicitly_confirmed=True)
        except Exception as exc:
            errors.append(exc)

    def test_failed_shutdown_is_visible_and_never_reports_stopped(self):
        subject, backend, _ = make_controller()
        subject.start("run-a", explicitly_confirmed=True)
        backend.fail.add("off")
        self.assertFalse(subject.stop("run-a"))
        self.assertEqual(subject.status().state, "failed")
        self.assertIn("graceful_shutdown_timeout", subject.status().last_error)
        self.assertEqual(backend.state, "Running")

    def test_close_performs_graceful_cleanup_of_active_session(self):
        subject, backend, _ = make_controller()
        subject.start("run-a", explicitly_confirmed=True)
        self.assertTrue(subject.close())
        self.assertEqual(subject.status().state, "stopped")
        self.assertEqual(backend.state, "Off")

    def test_watchdog_expires_idle_guest_without_a_manual_sweep(self):
        backend = FakeGuestBackend()
        subject = GuestSessionController(backend, idle_timeout_seconds=1,
                                         startup_timeout_seconds=0.1,
                                         shutdown_timeout_seconds=0.1,
                                         operation_timeout_seconds=0.5)
        subject.start("run-a", explicitly_confirmed=True)
        subject.start_watchdog(poll_seconds=0.01)
        try:
            deadline = threading.Event()
            for _ in range(100):
                if subject.status().state == "stopped":
                    deadline.set()
                    break
                threading.Event().wait(0.02)
            self.assertTrue(deadline.is_set())
            self.assertEqual(subject.status().shutdown_reason, "idle_timeout")
            self.assertEqual(backend.state, "Off")
        finally:
            subject.close()

    def test_partial_display_start_is_torn_down_before_guest_shutdown(self):
        subject, backend, _ = make_controller()
        backend.fail.add("display_on")
        with self.assertRaisesRegex(GuestSessionError, "guest_start_failed"):
            subject.start("run-a", explicitly_confirmed=True)
        names = [call[0] for call in backend.calls]
        self.assertLess(names.index("display_on"), names.index("display_off"))
        self.assertLess(names.index("display_off"), names.index("shutdown"))
        self.assertEqual(subject.status().state, "stopped")
        self.assertEqual(backend.state, "Off")


if __name__ == "__main__":
    unittest.main()
