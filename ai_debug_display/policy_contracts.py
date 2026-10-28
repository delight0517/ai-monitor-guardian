"""Stable policy-module API and errors; this file itself is restart-bound."""

from __future__ import annotations

from dataclasses import dataclass


class PolicyDenied(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RouteDecision:
    action: str
    reason: str
    hwnd: int
    pid: int

