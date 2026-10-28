"""Stable import facade for display-policy module API v1."""

from .modules.display_policy import require_adapter, route_window, validate_session
from .policy_contracts import PolicyDenied, RouteDecision

__all__ = [
    "PolicyDenied",
    "RouteDecision",
    "require_adapter",
    "route_window",
    "validate_session",
]

