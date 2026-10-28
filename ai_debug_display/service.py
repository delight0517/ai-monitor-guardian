"""Independent session coordinator; platform effects require an injected adapter."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager, nullcontext
from typing import Callable, Iterator, Protocol

from .models import (AdapterFeatures, Event, QaTarget, SessionRequest,
                     SessionStatus, Topology, WindowObservation)
from . import policy as default_policy
from .policy import PolicyDenied


class DisplayAdapter(Protocol):
    @property
    def features(self) -> AdapterFeatures: ...

    def begin(self, request: SessionRequest, target: QaTarget,
              topology: Topology,
              on_window_event: Callable[[WindowObservation], str]) -> None: ...

    def route_foreign_window(self, hwnd: int, pid: int, output_id: str,
                             topology_fingerprint: str) -> bool: ...

    def end(self, run_id: str) -> None: ...


class SessionPolicy(Protocol):
    def validate_session(self, request: SessionRequest, target: QaTarget | None,
                         topology: Topology | None, features: AdapterFeatures,
                         *, explicitly_confirmed: bool) -> tuple[tuple[int, ...], object, object]: ...

    def route_window(self, observation: WindowObservation, *, allowed_hwnds: tuple[int, ...],
                     target_pid: int, target_app_id: str, virtual_output_id: str,
                     physical_output_id: str, session_id: str,
                     topology_unchanged: bool, guard_enabled: bool) -> object: ...


class NoDisplayAdapter:
    """Default adapter: inert by design; this package never edits host displays alone."""

    features = AdapterFeatures()

    def begin(self, request: SessionRequest, target: QaTarget,
              topology: Topology,
              on_window_event: Callable[[WindowObservation], str]) -> None:
        raise PolicyDenied("adapter_unavailable", "검증된 Windows 디스플레이 어댑터가 없습니다.")

    def route_foreign_window(self, hwnd: int, pid: int, output_id: str,
                             topology_fingerprint: str) -> bool:
        return False

    def end(self, run_id: str) -> None:
        return None


def _adapter_ready(adapter: DisplayAdapter) -> bool:
    f = adapter.features
    return all((f.installed, f.exact_window_scope, f.non_activating,
                f.foreground_invariant, f.no_global_input, f.no_topology_mutation,
                f.output_mode_scoped, f.verify_identity_before_move,
                f.verify_after_move, f.continuous_window_guard))


class DisplaySessionService:
    """One explicit QA lease, exact HWND allowlist, bounded idle lifetime."""

    def __init__(self, *, target_lookup: Callable[[str], QaTarget | None],
                 topology_lookup: Callable[[], Topology | None],
                 adapter: DisplayAdapter | None = None,
                 policy: SessionPolicy | None = None,
                 idle_timeout_seconds: int = 300,
                 cleanup_timeout_seconds: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        if idle_timeout_seconds < 1 or cleanup_timeout_seconds <= 0:
            raise ValueError("idle and cleanup timeouts must be positive")
        self._target_lookup = target_lookup
        self._topology_lookup = topology_lookup
        self._adapter = adapter or NoDisplayAdapter()
        self._policy = policy or default_policy
        self._idle_timeout = idle_timeout_seconds
        self._cleanup_timeout = float(cleanup_timeout_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._session: dict[str, object] | None = None
        self._events: list[Event] = []

    def _event(self, session_id: str, action: str, result: str,
               *, hwnd: int | None = None, pid: int | None = None,
               code: str = "") -> None:
        # Deliberately omit window titles, screen text, prompts, and screenshots.
        self._events.append(Event(self._clock(), session_id, action, result, hwnd, pid, code))

    def events(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    def start(self, request: SessionRequest, *, explicitly_confirmed: bool) -> dict[str, object]:
        operation = getattr(self._policy, "lifecycle_operation", None)
        scope = operation() if callable(operation) else nullcontext()
        with scope:
            return self._start(request, explicitly_confirmed=explicitly_confirmed)

    def _start(self, request: SessionRequest, *, explicitly_confirmed: bool) -> dict[str, object]:
        target = self._target_lookup(request.run_id)
        topology = self._topology_lookup()
        hwnds, _, _ = self._policy.validate_session(
            request, target, topology, self._adapter.features,
            explicitly_confirmed=explicitly_confirmed)
        with self._lock:
            if (self._session is not None and self._session["state"] == "stopped"
                    and self._session["ended"]):
                self._session = None
            if self._session is not None:
                raise PolicyDenied("session_busy", "다른 디버그 화면 세션이 이미 사용 중입니다.")
            session = {
                "request": request, "target": target,
                "fingerprint": topology.fingerprint,
                "allowed_hwnds": hwnds, "state": "starting",
                "last_activity": self._clock(), "active": 0,
                "starting": True, "stopping": False,
                "cleanup_started": False, "ended": False,
            }
            self._session = session
        try:
            self._adapter.begin(request, target, topology, self.guard)
        except Exception as exc:
            with self._lock:
                if self._session is session:
                    session["starting"] = False
                    session["state"] = "stopping"
                    session["stopping"] = True
                    self._event(request.run_id, "start", "failed", code=type(exc).__name__)
                    self._condition.notify_all()
            self._stop_exact(session)
            raise PolicyDenied("adapter_start_failed", "가상 화면 세션 시작에 실패했습니다.") from exc
        with self._lock:
            session["starting"] = False
            if session["stopping"]:
                cancelled = True
                self._condition.notify_all()
            else:
                cancelled = False
                session["state"] = "running"
                session["last_activity"] = self._clock()
                self._event(request.run_id, "start", "running")
                self._condition.notify_all()
        if cancelled:
            self._stop_exact(session)
            raise PolicyDenied("start_cancelled", "시작 중 종료 요청이 들어와 세션을 정리했습니다.")
        return {"state": "running", "run_id": request.run_id,
                "allowed_hwnds": list(hwnds), "idle_timeout_seconds": self._idle_timeout}

    def note_activity(self, run_id: str) -> bool:
        with self._lock:
            s = self._session
            if not s or s["request"].run_id != run_id or s["state"] != "running":
                return False
            s["last_activity"] = self._clock()
            return True

    @contextmanager
    def operation(self, run_id: str) -> Iterator[bool]:
        with self._lock:
            s = self._session
            allowed = bool(s and s["request"].run_id == run_id and s["state"] == "running")
            if allowed:
                s["active"] += 1
                s["last_activity"] = self._clock()
        try:
            yield allowed
        finally:
            cleanup = False
            if allowed:
                with self._lock:
                    if self._session is s:
                        s["active"] -= 1
                        s["last_activity"] = self._clock()
                        cleanup = s["stopping"] and s["active"] == 0 and not s["starting"]
                        self._condition.notify_all()
                if cleanup:
                    self._stop_exact(s)

    def guard(self, window: WindowObservation) -> str:
        with self._lock:
            s = self._session
            if not s or s["state"] not in {"starting", "running"}:
                return "no_action"
            s["active"] += 1
            request = s["request"]
            target = s["target"]
            topology_fingerprint = s["fingerprint"]
            hwnds = s["allowed_hwnds"]
        result = "blocked"
        must_stop = False
        try:
            current = self._topology_lookup()
            unchanged = bool(current and current.fingerprint == topology_fingerprint)
            decision = self._policy.route_window(
                window, allowed_hwnds=hwnds, target_pid=target.pid,
                target_app_id=target.app_id,
                virtual_output_id=request.virtual_output_id,
                physical_output_id=request.physical_return_output_id,
                session_id=request.run_id, topology_unchanged=unchanged,
                guard_enabled=True)
            with self._lock:
                if s["state"] not in {"starting", "running"}:
                    return "blocked"
            if decision.action == "return_to_physical":
                try:
                    moved = self._adapter.route_foreign_window(
                        decision.hwnd, decision.pid, request.physical_return_output_id,
                        topology_fingerprint)
                except Exception as exc:
                    moved = False
                    error_code = type(exc).__name__
                else:
                    error_code = decision.reason
                with self._lock:
                    self._event(request.run_id, "foreign_window_return",
                                "moved" if moved else "adapter_refused",
                                hwnd=decision.hwnd, pid=decision.pid,
                                code=error_code)
                result = "returned" if moved else "blocked"
                must_stop = not moved
            elif decision.action == "block_and_alert":
                with self._lock:
                    self._event(request.run_id, "guard", "blocked", hwnd=window.hwnd,
                                pid=window.pid, code=decision.reason)
                result = decision.action
                must_stop = True
            else:
                result = decision.action
            return result
        except Exception as exc:
            with self._lock:
                self._event(request.run_id, "guard", "error", hwnd=window.hwnd,
                            pid=window.pid, code=type(exc).__name__)
            must_stop = True
            return "blocked"
        finally:
            cleanup = False
            with self._lock:
                if must_stop and s["state"] in {"starting", "running"}:
                    s["state"] = "stopping"
                    s["stopping"] = True
                s["active"] = max(0, s["active"] - 1)
                cleanup = s["stopping"] and s["active"] == 0 and not s["starting"]
                self._condition.notify_all()
            if cleanup:
                self._stop_exact(s)

    def stop(self, run_id: str, *, wait_seconds: float = 5.0) -> bool:
        deadline = self._clock() + max(0.0, wait_seconds)
        with self._lock:
            s = self._session
            if not s or s["request"].run_id != run_id:
                return False
            if s["state"] == "stopped":
                return True
            s["state"] = "stopping"
            s["stopping"] = True
            self._condition.notify_all()
            while s["starting"] or s["active"]:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return self._stop_exact(s)

    def _stop_exact(self, s: dict[str, object]) -> bool:
        with self._lock:
            if s["ended"]:
                return s["state"] == "stopped"
            if s["cleanup_started"]:
                self._condition.wait_for(lambda: s["ended"], timeout=self._cleanup_timeout)
                if not s["ended"]:
                    return False
                return s["state"] == "stopped"
            s["cleanup_started"] = True

        def cleanup() -> None:
            error: Exception | None = None
            try:
                self._adapter.end(s["request"].run_id)
            except Exception as exc:
                error = exc
            with self._lock:
                s["ended"] = True
                s["state"] = "failed" if error else "stopped"
                self._event(s["request"].run_id, "stop",
                            "failed" if error else "stopped",
                            code=type(error).__name__ if error else "")
                self._condition.notify_all()

        worker = threading.Thread(target=cleanup, name="debug-display-cleanup", daemon=True)
        worker.start()
        worker.join(timeout=self._cleanup_timeout)
        with self._lock:
            return bool(s["ended"] and s["state"] == "stopped")

    def sweep_idle(self, *, now: float | None = None) -> list[str]:
        current = self._clock() if now is None else now
        with self._lock:
            s = self._session
            if (not s or s["state"] != "running" or s["active"]
                    or current - s["last_activity"] < self._idle_timeout):
                return []
            run_id = s["request"].run_id
        return [run_id] if self.stop(run_id) else []

    def status(self) -> SessionStatus:
        with self._lock:
            s = self._session
            if not s:
                return SessionStatus("stopped", None, None, None, (), None, None,
                                     None, None, 0, self._idle_timeout, 0,
                                     _adapter_ready(self._adapter))
            req, target = s["request"], s["target"]
            return SessionStatus(s["state"], req.run_id, req.app_id, target.pid,
                                 tuple(s["allowed_hwnds"]), req.virtual_output_id,
                                 req.physical_return_output_id, req.mode,
                                 req.scale_percent,
                                 max(0, int(self._clock() - s["last_activity"])),
                                 self._idle_timeout, int(s["active"]),
                                 _adapter_ready(self._adapter))

    def module_reload_blockers(self, module_id: str) -> tuple[str, ...]:
        if module_id != "display-policy":
            return ("unknown_module",)
        with self._lock:
            s = self._session
            if not s:
                return ()
            if (s["state"] == "stopped" and s["ended"] and not s["active"]
                    and not s["starting"]):
                return ()
            return (f"display_session_{s['state']}",)
