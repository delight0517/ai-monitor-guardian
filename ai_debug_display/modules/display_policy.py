"""Reloadable, stateless display admission and foreign-window routing policy."""

from __future__ import annotations

from ..models import AdapterFeatures, Output, QaTarget, SessionRequest, Topology, WindowObservation
from ..policy_contracts import PolicyDenied, RouteDecision


MODULE_ID = "display-policy"
VERSION = "1.0.0"
API_VERSION = 1


def initialize(host, api):
    if api != API_VERSION:
        raise RuntimeError("policy_api_version_mismatch")
    return None


def dispose(host, handle):
    return None


def snapshot_state(host, handle):
    return {}


def restore_state(host, handle, snapshot):
    if snapshot != {}:
        raise RuntimeError("stateless_policy_snapshot_invalid")


def require_adapter(features: AdapterFeatures) -> None:
    required = (
        features.installed,
        features.exact_window_scope,
        features.non_activating,
        features.foreground_invariant,
        features.no_global_input,
        features.no_topology_mutation,
        features.output_mode_scoped,
        features.verify_identity_before_move,
        features.verify_after_move,
        features.continuous_window_guard,
    )
    if not all(required):
        raise PolicyDenied("adapter_unqualified", "필수 창 단위 비활성 제어·검증 기능이 없는 실행기입니다.")


def validate_session(request: SessionRequest, target: QaTarget | None,
                     topology: Topology | None, features: AdapterFeatures,
                     *, explicitly_confirmed: bool) -> tuple[tuple[int, ...], Output, Output]:
    if not explicitly_confirmed:
        raise PolicyDenied("confirmation_required", "사용자가 이 QA 세션을 직접 확인해야 합니다.")
    require_adapter(features)
    if target is None or not target.running or target.run_id != request.run_id:
        raise PolicyDenied("run_unavailable", "요청한 QA 실행 세션이 활성 상태가 아닙니다.")
    if target.app_id != request.app_id or target.pid <= 0:
        raise PolicyDenied("target_mismatch", "실행 앱 식별자 또는 PID가 일치하지 않습니다.")
    if target.launch_surface != "host":
        raise PolicyDenied("surface_incompatible", "현재 QA 실행 위치는 호스트 가상 화면에 연결할 수 없습니다.")
    if not target.hwnds or len(set(target.hwnds)) != len(target.hwnds):
        raise PolicyDenied("window_identity_unknown", "QA 앱의 고유한 창 목록을 확인할 수 없습니다.")
    if len(target.hwnds) != len(target.hwnd_pids) or any(hwnd <= 0 for hwnd in target.hwnds):
        raise PolicyDenied("window_identity_unknown", "QA 창과 프로세스 소유 정보가 불완전합니다.")
    if any(pid != target.pid for pid in target.hwnd_pids):
        raise PolicyDenied("foreign_window_in_target", "QA 앱 프로세스가 소유하지 않은 창이 포함되어 있습니다.")
    if topology is None or not topology.fingerprint:
        raise PolicyDenied("topology_unknown", "기준 화면 구성을 확인할 수 없습니다.")
    virtual = [o for o in topology.outputs if o.device_id == request.virtual_output_id]
    physical = [o for o in topology.outputs if o.device_id == request.physical_return_output_id]
    if len(virtual) != 1 or virtual[0].kind != "virtual" or not all((virtual[0].attached, virtual[0].verified, virtual[0].unique)):
        raise PolicyDenied("virtual_output_unknown", "선택한 가상 출력을 고유하게 확인할 수 없습니다.")
    if virtual[0].primary:
        raise PolicyDenied("virtual_output_primary", "가상 출력이 기본 화면으로 설정되어 있어 시작하지 않습니다.")
    if len(physical) != 1 or physical[0].kind != "physical" or not all((physical[0].attached, physical[0].verified, physical[0].unique)):
        raise PolicyDenied("return_output_unknown", "복귀용 실제 화면을 고유하게 확인할 수 없습니다.")
    if virtual[0].device_id == physical[0].device_id:
        raise PolicyDenied("output_alias", "가상 출력과 복귀 화면의 식별자가 같습니다.")
    if request.mode not in virtual[0].modes:
        raise PolicyDenied("mode_unsupported", "선택한 모드는 가상 출력이 광고한 지원 목록에 없습니다.")
    if not 50 <= request.scale_percent <= 200:
        raise PolicyDenied("scale_out_of_range", "UI 배율은 50%~200% 범위여야 합니다.")
    return target.hwnds, virtual[0], physical[0]


def route_window(observation: WindowObservation, *, allowed_hwnds: tuple[int, ...],
                 target_pid: int, target_app_id: str, virtual_output_id: str,
                 physical_output_id: str, session_id: str,
                 topology_unchanged: bool, guard_enabled: bool) -> RouteDecision:
    """Decide only. A separately qualified adapter performs and verifies the move."""
    if not guard_enabled or observation.output_id != virtual_output_id:
        return RouteDecision("no_action", "guard_inactive_or_not_on_virtual", observation.hwnd, observation.pid)
    if not topology_unchanged:
        return RouteDecision("block_and_alert", "topology_changed", observation.hwnd, observation.pid)
    if observation.hwnd <= 0 or observation.pid <= 0:
        return RouteDecision("block_and_alert", "window_identity_invalid", observation.hwnd, observation.pid)
    owned = (observation.hwnd in allowed_hwnds and observation.pid == target_pid
             and observation.app_id == target_app_id)
    if owned and observation.session_run_id == session_id:
        return RouteDecision("keep_on_virtual", "exact_qa_window", observation.hwnd, observation.pid)
    return RouteDecision("return_to_physical", "foreign_or_unowned_window", observation.hwnd, observation.pid)
