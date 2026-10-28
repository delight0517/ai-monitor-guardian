"""Exact-window reconnect recovery decisions; no native or topology side effects."""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import math
import ntpath
from pathlib import PureWindowsPath
import threading
from types import MappingProxyType
from typing import Literal

Rect = tuple[int, int, int, int]

def _text(value, *, allow_empty=False):
    if not isinstance(value,str) or (not value and not allow_empty) or len(value)>32767 or any(ord(c)<32 for c in value):
        raise ValueError('invalid_text_identity')
    return value

def _rect(value):
    if not isinstance(value,(list,tuple)) or len(value)!=4 or any(type(v) is not int or abs(v)>2**31-1 for v in value):
        raise ValueError('invalid_integer_rectangle')
    if value[2]<=value[0] or value[3]<=value[1]:raise ValueError('empty_or_reversed_rectangle')
    return tuple(value)

def _locked(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self._lock:return method(self,*args,**kwargs)
    return call

def _invalidates_bad_snapshot(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        try:return method(self,*args,**kwargs)
        except (ValueError,TypeError):
            self._generation+=1;self._snapshot=None;self._since=None;self._available=False
            raise
    return call


@dataclass(frozen=True)
class RecoveryOutput:
    stable_id: str
    kind: Literal['physical', 'virtual']
    work_rect: Rect
    attached: bool = True
    verified: bool = True
    primary: bool = False
    display_name: str = ''  # DISPLAYn is diagnostic only, never an identity.

    def __post_init__(self):
        _text(self.stable_id);_text(self.display_name,allow_empty=True)
        if self.kind not in ('physical','virtual'):raise ValueError('invalid_output_kind')
        if any(type(v) is not bool for v in (self.attached,self.verified,self.primary)):raise ValueError('invalid_output_flags')
        object.__setattr__(self,'work_rect',_rect(self.work_rect))


@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    process_started: str
    executable_path: str

    def __post_init__(self):
        if type(self.hwnd) is not int or type(self.pid) is not int or not 0<self.hwnd<2**64 or not 0<self.pid<2**32:raise ValueError('invalid_window_pid_or_handle')
        _text(self.process_started);_text(self.executable_path)
        try:
            started=datetime.fromisoformat(self.process_started.replace('Z','+00:00'))
            if started.tzinfo is None:raise ValueError('timestamp_requires_timezone')
        except (ValueError,TypeError) as exc:raise ValueError('invalid_process_start_timestamp') from exc
        if not PureWindowsPath(self.executable_path).is_absolute():raise ValueError('absolute_executable_required')
        object.__setattr__(self,'process_started',started.astimezone(timezone.utc).isoformat())
        object.__setattr__(self,'executable_path',ntpath.normcase(ntpath.normpath(self.executable_path)))


@dataclass(frozen=True)
class RecoveryWindow:
    identity: WindowIdentity
    rect: Rect
    minimized: bool = False
    assigned_output_id: str | None = None
    titlebar_rect: Rect | None = None

    def __post_init__(self):
        if not isinstance(self.identity,WindowIdentity) or type(self.minimized) is not bool:raise ValueError('invalid_window_snapshot')
        object.__setattr__(self,'rect',_rect(self.rect))
        if self.assigned_output_id is not None:_text(self.assigned_output_id)
        if self.titlebar_rect is not None:
            title=_rect(self.titlebar_rect)
            if not (self.rect[0]<=title[0]<title[2]<=self.rect[2] and self.rect[1]<=title[1]<title[3]<=self.rect[3]):
                raise ValueError('titlebar_outside_window')
            object.__setattr__(self,'titlebar_rect',title)


@dataclass(frozen=True)
class SavedPlacement:
    identity: WindowIdentity
    physical_output_id: str
    rect: Rect

    def __post_init__(self):
        if not isinstance(self.identity,WindowIdentity):raise ValueError('invalid_saved_identity')
        _text(self.physical_output_id);object.__setattr__(self,'rect',_rect(self.rect))


@dataclass(frozen=True)
class RecoveryPlan:
    identity: WindowIdentity
    expected_rect: Rect
    destination_output_id: str
    destination_rect: Rect
    source_snapshot: tuple
    generation: int


def _overlap(a: Rect, b: Rect) -> int:
    return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))


def _physical(outputs):
    return tuple(o for o in outputs if o.kind=='physical' and o.attached and o.verified)


def _fingerprint(outputs):
    return tuple(sorted((o.stable_id,o.kind,o.work_rect,o.attached,o.verified,o.primary) for o in outputs))

def _outputs(outputs):
    if not isinstance(outputs,(list,tuple)) or any(not isinstance(o,RecoveryOutput) for o in outputs):raise ValueError('invalid_output_snapshot')
    if len({o.stable_id for o in outputs})!=len(outputs):raise ValueError('duplicate_output_identity')
    return tuple(outputs)

def _window(window):
    if not isinstance(window,RecoveryWindow):raise ValueError('invalid_window_snapshot')
    return window

def _usable(window,output):
    left,top,right,bottom=window.rect
    title=window.titlebar_rect or (left,top,right,min(bottom,top+32))
    work=output.work_rect
    tw=max(0,min(title[2],work[2])-max(title[0],work[0]))
    th=max(0,min(title[3],work[3])-max(title[1],work[1]))
    area=(right-left)*(bottom-top)
    return tw>=min(120,title[2]-title[0]) and th>=min(20,title[3]-title[1]) and _overlap(window.rect,work)>=area*.2

def _owner(window,outputs):
    attached=[o for o in outputs if o.attached and o.verified]
    if window.assigned_output_id is not None:
        found=[o for o in attached if o.stable_id==window.assigned_output_id]
        return found[0] if len(found)==1 else 'ambiguous'
    areas=[(_overlap(window.rect,o.work_rect),o) for o in attached]
    best=max((a for a,_ in areas),default=0)
    if best==0:return None
    matches=[o for a,o in areas if a==best]
    return matches[0] if len(matches)==1 else 'ambiguous'


def _fit(rect, work):
    width=min(rect[2]-rect[0],work[2]-work[0]);height=min(rect[3]-rect[1],work[3]-work[1])
    left=max(work[0],min(rect[0],work[2]-width));top=max(work[1],min(rect[1],work[3]-height))
    return left,top,left+width,top+height


class ReconnectRecovery:
    """Plans only. Exact native readers and user scope remain consumer-owned."""
    def __init__(self,stable_seconds=2.0,*,journal=None):
        if isinstance(stable_seconds,bool) or not isinstance(stable_seconds,(int,float)) or not math.isfinite(stable_seconds) or stable_seconds<0:
            raise ValueError('invalid_stability_period')
        self.stable_seconds=stable_seconds
        self._saved={};self._snapshot=None;self._since=None;self._last_now=None
        self._generation=0;self._available=False;self._inflight=None
        self._lock=threading.RLock()
        self._journal=journal
        if journal is not None and journal.path.exists():self.reload(journal)

    @property
    def saved(self):
        with self._lock:return MappingProxyType(dict(self._saved))

    @_locked
    def export_records(self):
        return tuple(self._saved[k] for k in sorted(self._saved))

    @_locked
    def reload(self,journal):
        records=journal.load()  # validate whole journal before replacing active state
        replacement={}
        for record in records:
            if not isinstance(record,SavedPlacement) or record.identity.hwnd in replacement:
                raise ValueError('invalid_or_duplicate_saved_record')
            replacement[record.identity.hwnd]=record
        if self._inflight is not None:raise RuntimeError('recovery_inflight')
        if replacement!=self._saved:
            self._saved=replacement;self._generation+=1;self._snapshot=None;self._since=None
        self._journal=journal
        return len(records)

    @_locked
    def remember(self,window,outputs,*,user_confirmed):
        if user_confirmed is not True:raise ValueError('exact_window_confirmation_required')
        window=_window(window);outputs=_outputs(outputs)
        if self._inflight is not None:raise RuntimeError('recovery_inflight')
        if window.minimized:raise ValueError('cannot_infer_restore_placement_from_minimized_window')
        physical=_physical(outputs)
        matches=[o for o in physical if _overlap(window.rect,o.work_rect)>0]
        if not matches:raise ValueError('no_verified_physical_placement_to_remember')
        best=max(_overlap(window.rect,o.work_rect) for o in matches)
        matches=[o for o in matches if _overlap(window.rect,o.work_rect)==best]
        if len(matches)!=1:raise ValueError('ambiguous_original_physical_output')
        destination=matches[0]
        owner=_owner(window,outputs)
        if owner=='ambiguous' or (owner is not None and owner.stable_id!=destination.stable_id) or not _usable(window,destination):
            raise ValueError('original_physical_window_not_usable_or_ambiguous')
        record=SavedPlacement(window.identity,destination.stable_id,window.rect)
        if self._saved.get(window.identity.hwnd)!=record:
            replacement=dict(self._saved);replacement[window.identity.hwnd]=record
            if self._journal is not None:self._journal.save(tuple(replacement.values()))
            self._saved=replacement;self._generation+=1;self._snapshot=None;self._since=None
        return record

    @_locked
    @_invalidates_bad_snapshot
    def poll(self,outputs,windows,*,now,physical_usable=True):
        outputs=_outputs(outputs)
        if not isinstance(windows,(list,tuple)):raise ValueError('invalid_windows_snapshot')
        windows=tuple(_window(w) for w in windows)
        if len({w.identity.hwnd for w in windows})!=len(windows):raise ValueError('duplicate_window_identity')
        if isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now) or now<0:raise ValueError('invalid_monotonic_time')
        if type(physical_usable) is not bool:raise ValueError('invalid_physical_availability')
        physical=_physical(outputs);available=physical_usable and bool(physical)
        signature=(_fingerprint(outputs),available)
        backwards=self._last_now is not None and now<self._last_now
        self._last_now=now
        if signature!=self._snapshot or backwards:
            self._generation+=1;self._snapshot=signature;self._since=now if available else None
        self._available=available
        base={'ai_backend':'private_desktop','preview_allowed':False,'plans':[],'generation':self._generation}
        if not available:return dict(base,state='physical_unavailable')
        if self._inflight is not None:return dict(base,state='recovery_inflight')
        if self._since is None:self._since=now
        if now-self._since<self.stable_seconds:return dict(base,state='waiting_for_stable_physical')
        plans=[]
        for window in windows:
            saved=self._saved.get(window.identity.hwnd)
            if not saved or saved.identity!=window.identity or window.minimized:continue
            destinations=[o for o in physical if o.stable_id==saved.physical_output_id]
            if len(destinations)!=1:continue
            owner=_owner(window,outputs)
            if owner=='ambiguous':continue
            # Ownership and actual titlebar accessibility are separate checks.
            if any(_usable(window,o) for o in physical):continue
            destination=destinations[0]
            plans.append(RecoveryPlan(window.identity,window.rect,destination.stable_id,
                                      _fit(saved.rect,destination.work_rect),_fingerprint(outputs),self._generation))
        preview=not any(o.kind=='virtual' and o.attached and o.primary for o in outputs)
        return dict(base,state='ready',preview_allowed=preview,plans=plans)

    @_locked
    def revalidate(self,plan,outputs,window):
        outputs=_outputs(outputs)
        if window is None:return False
        window=_window(window)
        if not isinstance(plan,RecoveryPlan) or type(plan.generation) is not int:return False
        if plan.generation!=self._generation or not self._available or self._inflight is not None:return False
        if _fingerprint(outputs)!=plan.source_snapshot:return False
        if window.identity!=plan.identity or window.rect!=plan.expected_rect or window.minimized:return False
        saved=self._saved.get(plan.identity.hwnd)
        if not saved or saved.identity!=plan.identity or saved.physical_output_id!=plan.destination_output_id:return False
        matches=[o for o in _physical(outputs) if o.stable_id==plan.destination_output_id]
        return len(matches)==1 and _fit(saved.rect,matches[0].work_rect)==plan.destination_rect

    @_locked
    def consume(self,plan):
        if self._inflight is not None or plan.generation!=self._generation or not self._available:return False
        self._inflight=(plan.generation,plan.identity.hwnd)
        self._generation+=1
        return True

    @_locked
    def finish(self,plan):
        if self._inflight==(plan.generation,plan.identity.hwnd):
            self._inflight=None;self._since=self._last_now


def apply_one_recovery(plan, *, policy, read_outputs, read_window, read_context,
                       move_nonactivating, user_confirmed):
    """No second move. Post-invocation failures retain a truthful return path."""
    if user_confirmed is not True:
        return {'attempted':False,'verified':False,'reason':'confirmation_required'}
    try:
        context=read_context();outputs=read_outputs();current=read_window(plan.identity)
        if not policy.revalidate(plan,outputs,current):
            return {'attempted':False,'verified':False,'reason':'fresh_identity_or_output_mismatch'}
        original=current.rect
        if read_context()!=context or not policy.revalidate(plan,read_outputs(),read_window(plan.identity)):
            return {'attempted':False,'verified':False,'reason':'pre_move_state_changed'}
        if not policy.consume(plan):
            return {'attempted':False,'verified':False,'reason':'stale_or_consumed_generation'}
    except Exception as exc:
        return {'attempted':False,'verified':False,'reason':'preflight_failed','error_type':type(exc).__name__}
    result={'attempted':True,'verified':False,'original_rect':original,'destination_rect':plan.destination_rect}
    try:
        try:
            move_nonactivating(plan.identity,plan.destination_rect)
        except Exception as exc:
            return dict(result,reason='move_failed_or_partial',mutation_uncertain=True,error_type=type(exc).__name__)
        try:
            after=read_window(plan.identity)
            after_outputs=_outputs(read_outputs())
            after_context=read_context()
            if after is not None:_window(after)
        except Exception as exc:
            return dict(result,reason='verification_incomplete',mutation_uncertain=True,error_type=type(exc).__name__)
        target_ok=bool(after and after.identity==plan.identity and after.rect==plan.destination_rect)
        verified=target_ok and _fingerprint(after_outputs)==plan.source_snapshot and after_context==context
        return dict(result,verified=verified,reason='recovered' if verified else 'post_move_verification_failed',mutation_uncertain=not target_ok)
    finally:
        policy.finish(plan)
