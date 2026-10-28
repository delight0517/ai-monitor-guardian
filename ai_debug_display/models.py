"""Versioned, content-free contracts for AI debug display sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


API_VERSION = 1


@dataclass(frozen=True)
class Output:
    device_id: str
    kind: Literal["virtual", "physical"]
    attached: bool
    verified: bool
    unique: bool
    primary: bool
    modes: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Topology:
    outputs: tuple[Output, ...]
    fingerprint: str
    foreground_hwnd: int | None


@dataclass(frozen=True)
class QaTarget:
    run_id: str
    app_id: str
    pid: int
    hwnds: tuple[int, ...]
    hwnd_pids: tuple[int, ...]
    launch_surface: Literal["host", "private-desktop", "vm"]
    running: bool


@dataclass(frozen=True)
class SessionRequest:
    run_id: str
    app_id: str
    virtual_output_id: str
    physical_return_output_id: str
    mode: tuple[int, int]
    scale_percent: int = 100


@dataclass(frozen=True)
class WindowObservation:
    hwnd: int
    pid: int
    output_id: str
    session_run_id: str | None
    app_id: str = ""


@dataclass(frozen=True)
class AdapterFeatures:
    installed: bool = False
    exact_window_scope: bool = False
    non_activating: bool = False
    foreground_invariant: bool = False
    no_global_input: bool = False
    no_topology_mutation: bool = False
    output_mode_scoped: bool = False
    verify_identity_before_move: bool = False
    verify_after_move: bool = False
    continuous_window_guard: bool = False


@dataclass(frozen=True)
class Event:
    at_monotonic: float
    session_id: str
    action: str
    result: str
    hwnd: int | None = None
    pid: int | None = None
    detail_code: str = ""


@dataclass(frozen=True)
class SessionStatus:
    state: str
    session_id: str | None
    app_id: str | None
    pid: int | None
    allowed_hwnds: tuple[int, ...]
    virtual_output_id: str | None
    physical_output_id: str | None
    mode: tuple[int, int] | None
    scale_percent: int | None
    idle_seconds: int
    idle_timeout_seconds: int
    active_operations: int
    adapter_ready: bool
    security_boundary: str = "presentation-only"
