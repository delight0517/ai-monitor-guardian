"""Run-scoped lifecycle for the dedicated, isolated Hyper-V QA guest.

This is a policy/controller module. A platform backend must be supplied by the
host integration; importing this module never starts, stops, or inspects a VM.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Protocol


APPROVED_VM_NAME = "AiDebugDisplayGuestQA"
MAX_IDLE_SECONDS = 300


@dataclass(frozen=True)
class GuestSnapshot:
    vm_name: str
    state: str
    guest_ready: bool
    network_adapter_count: int
    guest_service_interface_enabled: bool
    secure_boot_enabled: bool
    tpm_enabled: bool
    shutdown_integration_enabled: bool
    active_run_id: str | None = None


@dataclass(frozen=True)
class GuestSessionStatus:
    state: str
    run_id: str | None
    vm_name: str
    idle_seconds: int
    idle_timeout_seconds: int
    display_enabled: bool
    last_error: str | None
    shutdown_reason: str | None


class GuestBackend(Protocol):
    """Narrow host-to-guest adapter; every method is bound to the fixed VM."""

    def inspect(self, vm_name: str) -> GuestSnapshot: ...
    def start_guest(self, vm_name: str, run_id: str) -> None: ...
    def wait_guest_ready(self, vm_name: str, run_id: str, timeout: float,
                         cancelled: threading.Event) -> bool: ...
    def enable_display(self, vm_name: str, run_id: str) -> None: ...
    def disable_display(self, vm_name: str, run_id: str) -> None: ...
    def request_guest_shutdown(self, vm_name: str, run_id: str) -> None: ...
    def wait_guest_off(self, vm_name: str, timeout: float) -> bool: ...


class GuestSessionError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GuestSessionController:
    """Explicit guest lease with idle expiry and graceful, bounded teardown.

    The caller must run this controller in a broker process that remains alive
    for the lease and must call ``close`` on normal application/session exit.
    A process crash recovery service is deliberately outside this module.
    """

    def __init__(self, backend: GuestBackend, *, idle_timeout_seconds: int = 300,
                 startup_timeout_seconds: float = 120,
                 shutdown_timeout_seconds: float = 45,
                 operation_timeout_seconds: float = 10,
                 clock=time.monotonic):
        if not 0 < idle_timeout_seconds <= MAX_IDLE_SECONDS:
            raise ValueError("idle_timeout_out_of_range")
        if min(startup_timeout_seconds, shutdown_timeout_seconds,
               operation_timeout_seconds) <= 0:
            raise ValueError("timeout_must_be_positive")
        self._backend = backend
        self._idle_timeout = idle_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        self._operation_timeout = operation_timeout_seconds
        self._clock = clock
        self._cv = threading.Condition(threading.RLock())
        self._state = "stopped"
        self._run_id: str | None = None
        self._last_activity = 0.0
        self._display_enabled = False
        self._started_guest = False
        self._cancel = threading.Event()
        self._last_error: str | None = None
        self._shutdown_reason: str | None = None
        self._cleanup_active = False
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    def start(self, run_id: str, *, explicitly_confirmed: bool) -> GuestSessionStatus:
        if not explicitly_confirmed:
            raise GuestSessionError("confirmation_required")
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 128:
            raise GuestSessionError("invalid_run_id")
        with self._cv:
            if self._state != "stopped":
                raise GuestSessionError("guest_session_busy")
            self._state = "starting"
            self._run_id = run_id
            self._cancel = threading.Event()
            self._last_error = None
            self._shutdown_reason = None
            self._display_enabled = False
            self._started_guest = False

        error: str | None = None
        try:
            snapshot = self._backend.inspect(APPROVED_VM_NAME)
            self._validate_snapshot(snapshot, expected_state="Off")
            if snapshot.active_run_id:
                raise GuestSessionError("stale_guest_lease")
            if self._cancel.is_set():
                raise GuestSessionError("start_cancelled")
            self._backend.start_guest(APPROVED_VM_NAME, run_id)
            self._started_guest = True
            if self._cancel.is_set():
                raise GuestSessionError("start_cancelled")
            ready = self._backend.wait_guest_ready(
                APPROVED_VM_NAME, run_id, self._startup_timeout, self._cancel)
            if self._cancel.is_set():
                raise GuestSessionError("start_cancelled")
            if not ready:
                raise GuestSessionError("guest_ready_timeout")
            ready_snapshot = self._backend.inspect(APPROVED_VM_NAME)
            self._validate_snapshot(ready_snapshot, expected_state="Running")
            if not ready_snapshot.guest_ready or ready_snapshot.active_run_id != run_id:
                raise GuestSessionError("guest_identity_or_health_mismatch")
            if self._cancel.is_set():
                raise GuestSessionError("start_cancelled")
            self._display_enabled = True
            self._backend.enable_display(APPROVED_VM_NAME, run_id)
            with self._cv:
                if self._cancel.is_set():
                    raise GuestSessionError("start_cancelled")
                self._last_activity = self._clock()
                self._state = "running"
                self._cv.notify_all()
                return self.status()
        except Exception as exc:
            error = exc.code if isinstance(exc, GuestSessionError) else type(exc).__name__
            with self._cv:
                cancelled = self._cancel.is_set()
                self._state = "stopping" if cancelled or self._started_guest else "failed"
                self._last_error = error
                self._cv.notify_all()
            if self._started_guest:
                # Also clean up if stop()'s bounded wait expired before start
                # returned; cancellation must never strand the guest.
                self._cleanup("start_cancelled" if cancelled else "start_failed")
            else:
                with self._cv:
                    self._state = "stopped"
                    self._run_id = None
                    self._cv.notify_all()
            if isinstance(exc, GuestSessionError):
                raise
            raise GuestSessionError("guest_start_failed") from exc

    def heartbeat(self, run_id: str) -> bool:
        with self._cv:
            if self._state != "running" or run_id != self._run_id:
                return False
            now = self._clock()
            if now - self._last_activity >= self._idle_timeout:
                self._cancel.set()
                return False
            self._last_activity = now
            return True

    def stop(self, run_id: str, *, reason: str = "explicit_stop",
             wait_seconds: float | None = None) -> bool:
        deadline = self._clock() + (self._operation_timeout if wait_seconds is None
                                    else max(0.0, wait_seconds))
        with self._cv:
            if run_id != self._run_id or self._state == "stopped":
                return False
            self._shutdown_reason = reason
            self._cancel.set()
            while self._state == "starting":
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
            if self._state == "stopped":
                return True
            self._state = "stopping"
        return self._cleanup(reason)

    def _cleanup(self, reason: str) -> bool:
        with self._cv:
            if self._cleanup_active:
                self._cv.wait_for(lambda: not self._cleanup_active,
                                  timeout=self._operation_timeout)
                return self._state == "stopped"
            self._cleanup_active = True
        errors: list[str] = []
        display_error: str | None = None
        run_id = self._run_id
        if not run_id:
            errors.append("missing_run_id")
        else:
            if self._display_enabled:
                try:
                    self._backend.disable_display(APPROVED_VM_NAME, run_id)
                    self._display_enabled = False
                except Exception as exc:
                    display_error = f"display_stop:{type(exc).__name__}"
            if self._started_guest:
                shutdown_error: str | None = None
                try:
                    self._backend.request_guest_shutdown(APPROVED_VM_NAME, run_id)
                except Exception as exc:
                    shutdown_error = f"guest_shutdown:{type(exc).__name__}"
                try:
                    guest_is_off = self._backend.wait_guest_off(
                        APPROVED_VM_NAME, self._shutdown_timeout)
                except Exception as exc:
                    guest_is_off = False
                    errors.append(f"guest_state:{type(exc).__name__}")
                if guest_is_off:
                    # Confirmed VM Off is the final isolation boundary even if
                    # the guest's in-VM display teardown endpoint was unresponsive.
                    self._started_guest = False
                    self._display_enabled = False
                else:
                    if shutdown_error:
                        errors.append(shutdown_error)
                    errors.append("graceful_shutdown_timeout")
            elif display_error:
                errors.append(display_error)
        with self._cv:
            self._cleanup_active = False
            if errors:
                self._state = "failed"
                self._last_error = ",".join(errors)
            else:
                self._state = "stopped"
                self._run_id = None
                self._display_enabled = False
                self._started_guest = False
                self._last_error = ("display_teardown_recovered_by_guest_shutdown"
                                    if display_error else None)
            self._shutdown_reason = reason
            self._cv.notify_all()
            return not errors

    def sweep_idle(self) -> bool:
        with self._cv:
            run_id = self._run_id
            idle = (self._state == "running" and run_id is not None
                    and self._clock() - self._last_activity >= self._idle_timeout)
        return self.stop(run_id, reason="idle_timeout") if idle and run_id else False

    def start_watchdog(self, *, poll_seconds: float = 1.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_interval_must_be_positive")
        with self._cv:
            if self._watchdog and self._watchdog.is_alive():
                return
            self._watchdog_stop.clear()
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, args=(poll_seconds,),
                name="ai-debug-guest-idle-watchdog", daemon=True)
            self._watchdog.start()

    def _watchdog_loop(self, poll_seconds: float) -> None:
        while not self._watchdog_stop.wait(poll_seconds):
            self.sweep_idle()

    def close(self) -> bool:
        """Stop the watchdog and gracefully close this exact active run."""
        self._watchdog_stop.set()
        with self._cv:
            run_id = self._run_id
            watchdog = self._watchdog
        if watchdog and watchdog is not threading.current_thread():
            watchdog.join(timeout=self._operation_timeout)
        if run_id and self.status().state != "stopped":
            return self.stop(run_id, reason="broker_close")
        return self.status().state == "stopped"

    def status(self) -> GuestSessionStatus:
        with self._cv:
            age = 0 if self._state != "running" else max(
                0, int(self._clock() - self._last_activity))
            return GuestSessionStatus(self._state, self._run_id, APPROVED_VM_NAME,
                                      age, self._idle_timeout,
                                      self._display_enabled, self._last_error,
                                      self._shutdown_reason)

    @staticmethod
    def _validate_snapshot(snapshot: GuestSnapshot, *, expected_state: str) -> None:
        if snapshot.vm_name != APPROVED_VM_NAME:
            raise GuestSessionError("unapproved_vm_identity")
        if snapshot.state != expected_state:
            raise GuestSessionError("unexpected_vm_state")
        if snapshot.network_adapter_count != 0:
            raise GuestSessionError("guest_network_not_isolated")
        if snapshot.guest_service_interface_enabled:
            raise GuestSessionError("guest_file_integration_enabled")
        if not snapshot.secure_boot_enabled:
            raise GuestSessionError("guest_secure_boot_disabled")
        if not snapshot.tpm_enabled:
            raise GuestSessionError("guest_tpm_disabled")
        if not snapshot.shutdown_integration_enabled:
            raise GuestSessionError("guest_shutdown_unavailable")

