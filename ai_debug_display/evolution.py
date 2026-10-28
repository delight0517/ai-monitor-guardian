"""Canonical-source module host with stale-candidate fencing and rollback.

This executes trusted project code in-process. It is neither a sandbox nor a
production broker. The initial vertical slice reloads one stateless display
policy module; native driver, process/desktop, guest and consumer layers retain
their explicit build/restart/idle boundaries.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import threading
import time
from types import ModuleType
from typing import Callable, Iterator, Mapping


MANIFEST_NAME = "module-manifest.json"
MANIFEST_API_VERSION = 1
MAX_MANIFEST_BYTES = 256 * 1024
MAX_MODULE_BYTES = 2 * 1024 * 1024
MAX_STATE_BYTES = 256 * 1024
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class EvolutionError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ModuleCandidate:
    module_id: str
    canonical_root: str
    relative_path: str
    version: str
    api_version: int
    source_sha256: str
    manifest_sha256: str
    loaded_source_sha256: str
    loaded_manifest_sha256: str
    generation: int


@dataclass(frozen=True)
class ModuleStatus:
    module_id: str
    version: str
    api_version: int
    source_sha256: str
    generation: int
    state: str
    auto_watch: bool
    last_error: str | None


@dataclass
class _Loaded:
    entry: dict[str, object]
    module: ModuleType
    handle: object
    source_bytes: bytes
    manifest_bytes: bytes
    source_sha256: str
    manifest_sha256: str
    generation: int
    module_name: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvolutionError("duplicate_manifest_key")
        result[key] = value
    return result


def _version_tuple(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise EvolutionError("module_version_invalid")
    return tuple(int(part) for part in value.split("."))


class EvolutionModuleHost:
    """One-module composition root with explicit manual reload only."""

    def __init__(self, canonical_root: str | Path, *,
                 module_id: str = "display-policy",
                 blockers: Callable[[str], tuple[str, ...] | list[str]] | None = None,
                 state_limit_bytes: int = MAX_STATE_BYTES):
        self._root = Path(canonical_root).resolve(strict=True)
        if not self._root.is_dir():
            raise EvolutionError("canonical_root_not_directory")
        self._manifest_path = self._root / MANIFEST_NAME
        self._module_id = module_id
        self._blockers = blockers or (lambda _module_id: ())
        if not 0 < state_limit_bytes <= MAX_STATE_BYTES:
            raise ValueError("state_limit_out_of_range")
        self._state_limit = state_limit_bytes
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._generation = 0
        self._policy_calls = 0
        self._applying = False
        self._state = "loading"
        self._last_error: str | None = None
        self._loaded: _Loaded | None = None

        manifest_bytes, manifest = self._read_manifest()
        entry = self._entry(manifest, module_id)
        source_bytes, source_path = self._read_module(entry)
        loaded = self._load(entry, source_bytes, manifest_bytes, source_path, 1)
        self._loaded = loaded
        self._generation = 1
        self._state = "ready"

    @property
    def current_policy(self):
        return self

    def status(self) -> ModuleStatus:
        with self._lock:
            loaded = self._loaded
            if loaded is None:
                return ModuleStatus(self._module_id, "", 0, "", self._generation,
                                    self._state, False, self._last_error)
            return ModuleStatus(
                self._module_id, str(loaded.entry["version"]),
                int(loaded.entry["api_version"]), loaded.source_sha256,
                loaded.generation, self._state,
                bool(loaded.entry.get("auto_watch", False)), self._last_error)

    def reload_blockers(self) -> tuple[str, ...]:
        with self._lock:
            if self._state == "closed":
                return ("module_host_closed",)
            if self._policy_calls:
                return ("policy_call_active",)
        try:
            blockers = tuple(self._blockers(self._module_id))
        except Exception as exc:
            return (f"blocker_check_failed_{type(exc).__name__}",)
        return tuple(code for code in blockers if isinstance(code, str) and code)

    @contextmanager
    def lifecycle_operation(self) -> Iterator[None]:
        with self._condition:
            if self._state != "ready" or self._applying:
                raise EvolutionError("module_reload_in_progress")
            self._policy_calls += 1
        try:
            yield
        finally:
            with self._condition:
                self._policy_calls -= 1
                self._condition.notify_all()

    def validate_session(self, *args, **kwargs):
        with self.lifecycle_operation():
            with self._lock:
                loaded = self._loaded
            if loaded is None:
                raise EvolutionError("module_host_not_ready")
            return loaded.module.validate_session(*args, **kwargs)

    def route_window(self, *args, **kwargs):
        with self.lifecycle_operation():
            with self._lock:
                loaded = self._loaded
            if loaded is None:
                raise EvolutionError("module_host_not_ready")
            return loaded.module.route_window(*args, **kwargs)

    def stage(self, module_id: str | None = None) -> ModuleCandidate:
        module_id = module_id or self._module_id
        if module_id != self._module_id:
            raise EvolutionError("module_not_registered")
        manifest_bytes, manifest = self._read_manifest()
        entry = self._entry(manifest, module_id)
        source_bytes, _ = self._read_module(entry)
        try:
            compile(source_bytes.decode("utf-8-sig"), str(self._module_path(entry)), "exec")
        except (UnicodeError, SyntaxError):
            raise EvolutionError("candidate_syntax_invalid") from None
        with self._lock:
            loaded = self._loaded
            if self._state != "ready" or loaded is None:
                raise EvolutionError("module_host_not_ready")
            if int(entry["api_version"]) != int(loaded.entry["api_version"]):
                raise EvolutionError("module_api_change_requires_restart")
            if str(entry["path"]) != str(loaded.entry["path"]):
                raise EvolutionError("module_path_change_requires_restart")
            if _version_tuple(entry["version"]) <= _version_tuple(loaded.entry["version"]):
                raise EvolutionError("candidate_version_not_newer")
            if _sha(source_bytes) == loaded.source_sha256:
                raise EvolutionError("candidate_source_unchanged")
            return ModuleCandidate(
                module_id=module_id,
                canonical_root=str(self._root),
                relative_path=str(entry["path"]),
                version=str(entry["version"]),
                api_version=int(entry["api_version"]),
                source_sha256=_sha(source_bytes),
                manifest_sha256=_sha(manifest_bytes),
                loaded_source_sha256=loaded.source_sha256,
                loaded_manifest_sha256=loaded.manifest_sha256,
                generation=loaded.generation)

    def apply(self, candidate: ModuleCandidate, *,
              blocker_timeout_seconds: float = 5.0) -> ModuleStatus:
        if blocker_timeout_seconds < 0:
            raise ValueError("blocker_timeout_must_be_nonnegative")
        with self._cross_process_lock():
            with self._condition:
                loaded = self._loaded
                if candidate.module_id != self._module_id:
                    raise EvolutionError("candidate_module_mismatch")
                if candidate.canonical_root != str(self._root):
                    raise EvolutionError("candidate_root_mismatch")
                if loaded is None or self._state != "ready":
                    raise EvolutionError("module_host_not_ready")
                if candidate.generation != loaded.generation:
                    raise EvolutionError("candidate_generation_stale")
                if candidate.loaded_source_sha256 != loaded.source_sha256:
                    raise EvolutionError("candidate_base_source_stale")
                if candidate.loaded_manifest_sha256 != loaded.manifest_sha256:
                    raise EvolutionError("candidate_base_manifest_stale")
                self._applying = True
                self._state = "applying"
                deadline = time.monotonic() + blocker_timeout_seconds
                while self._policy_calls:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._applying = False
                        self._state = "ready"
                        self._condition.notify_all()
                        raise EvolutionError("policy_calls_did_not_drain")
                    self._condition.wait(remaining)

            try:
                blocker_codes = tuple(self._blockers(self._module_id))
            except Exception as exc:
                self._finish_apply_failure(f"blocker_check_failed_{type(exc).__name__}")
                raise EvolutionError(f"blocker_check_failed_{type(exc).__name__}") from None
            if blocker_codes:
                self._finish_apply_failure("active_work_blocked")
                raise EvolutionError("active_work_blocked:" + ",".join(blocker_codes))

            try:
                current_manifest_bytes, manifest = self._read_manifest()
                entry = self._entry(manifest, self._module_id)
                source_bytes, source_path = self._read_module(entry)
            except EvolutionError as exc:
                self._finish_apply_failure(exc.code)
                raise
            if _sha(current_manifest_bytes) != candidate.manifest_sha256:
                self._finish_apply_failure("candidate_manifest_stale")
                raise EvolutionError("candidate_manifest_stale")
            if _sha(source_bytes) != candidate.source_sha256:
                self._finish_apply_failure("candidate_source_stale")
                raise EvolutionError("candidate_source_stale")
            if str(entry["path"]) != candidate.relative_path or str(entry["version"]) != candidate.version:
                self._finish_apply_failure("candidate_identity_stale")
                raise EvolutionError("candidate_identity_stale")
            if int(entry["api_version"]) != candidate.api_version:
                self._finish_apply_failure("module_api_change_requires_restart")
                raise EvolutionError("module_api_change_requires_restart")
            if _version_tuple(entry["version"]) <= _version_tuple(loaded.entry["version"]):
                self._finish_apply_failure("candidate_version_not_newer")
                raise EvolutionError("candidate_version_not_newer")
            try:
                snapshot = self._snapshot(loaded)
            except Exception as exc:
                self._finish_apply_failure(f"snapshot_failed_{type(exc).__name__}")
                raise EvolutionError("snapshot_failed") from None

            dispose_attempted = False
            next_loaded = None
            try:
                self._archive_loaded(loaded)
                dispose_attempted = True
                loaded.module.dispose(self, loaded.handle)
                next_loaded = self._load(
                    entry, source_bytes, current_manifest_bytes, source_path,
                    loaded.generation + 1)
                next_loaded.module.restore_state(self, next_loaded.handle, snapshot)
            except Exception as exc:
                candidate_cleanup_ok = True
                if next_loaded is not None:
                    try:
                        next_loaded.module.dispose(self, next_loaded.handle)
                    except Exception:
                        candidate_cleanup_ok = False
                    sys.modules.pop(next_loaded.module_name, None)
                try:
                    self._atomic_write(self._module_path(loaded.entry), loaded.source_bytes)
                    self._atomic_write(self._manifest_path, loaded.manifest_bytes)
                    rollback_ok = (self._reinitialize(loaded, snapshot)
                                   if dispose_attempted else True)
                except Exception:
                    rollback_ok = False
                if not rollback_ok or not candidate_cleanup_ok:
                    with self._condition:
                        self._state = "failed"
                        self._last_error = "reload_failed_and_rollback_failed"
                        self._applying = False
                        self._condition.notify_all()
                    raise EvolutionError("reload_failed_and_rollback_failed") from None
                with self._condition:
                    self._loaded = loaded
                    self._generation = loaded.generation
                    self._state = "ready"
                    self._last_error = f"candidate_rejected_{type(exc).__name__}"
                    self._applying = False
                    self._condition.notify_all()
                raise EvolutionError("candidate_init_or_restore_failed") from None

            with self._condition:
                self._loaded = next_loaded
                self._generation = next_loaded.generation
                self._state = "ready"
                self._last_error = None
                self._applying = False
                self._condition.notify_all()
            if loaded.module_name in sys.modules:
                del sys.modules[loaded.module_name]
            return self.status()

    def rollback(self, source_sha256: str) -> ModuleStatus:
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise EvolutionError("rollback_hash_invalid")
        with self._cross_process_lock():
            with self._condition:
                active = self._loaded
                if active is None or self._state != "ready":
                    raise EvolutionError("module_host_not_ready")
                if self._policy_calls:
                    raise EvolutionError("policy_call_active")
                self._applying = True
                self._state = "applying"
            try:
                blockers = tuple(self._blockers(self._module_id))
            except Exception:
                self._finish_apply_failure("blocker_check_failed")
                raise EvolutionError("blocker_check_failed") from None
            if blockers:
                self._finish_apply_failure("active_work_blocked")
                raise EvolutionError("active_work_blocked:" + ",".join(blockers))
            manifest_now, manifest_obj = self._read_manifest()
            source_now, _ = self._read_module(self._entry(manifest_obj, self._module_id))
            if _sha(manifest_now) != active.manifest_sha256 or _sha(source_now) != active.source_sha256:
                self._finish_apply_failure("unapplied_source_candidate_exists")
                raise EvolutionError("unapplied_source_candidate_exists")
            backup = self._read_backup(source_sha256)
            if backup["module_id"] != self._module_id:
                self._finish_apply_failure("rollback_module_mismatch")
                raise EvolutionError("rollback_module_mismatch")
            old_source = base64.b64decode(backup["source_b64"], validate=True)
            old_manifest = base64.b64decode(backup["manifest_b64"], validate=True)
            if _sha(old_source) != source_sha256 or _sha(old_manifest) != backup["manifest_sha256"]:
                self._finish_apply_failure("rollback_backup_hash_mismatch")
                raise EvolutionError("rollback_backup_hash_mismatch")
            try:
                manifest_target = json.loads(old_manifest.decode("utf-8-sig"),
                                            object_pairs_hook=_pairs_no_duplicates)
                entry = self._entry(manifest_target, self._module_id)
                if str(entry["path"]) != str(active.entry["path"]):
                    raise EvolutionError("rollback_path_mismatch")
                if _sha(old_source) != backup["source_sha256"]:
                    raise EvolutionError("rollback_source_hash_mismatch")
                snapshot = self._snapshot(active)
                self._archive_loaded(active)
            except Exception as exc:
                self._finish_apply_failure(f"rollback_preflight_failed_{type(exc).__name__}")
                raise EvolutionError("rollback_preflight_failed") from None

            dispose_attempted = False
            loaded = None
            try:
                dispose_attempted = True
                active.module.dispose(self, active.handle)
                self._atomic_write(self._module_path(entry), old_source)
                self._atomic_write(self._manifest_path, old_manifest)
                loaded = self._load(entry, old_source, old_manifest,
                                    self._module_path(entry), active.generation + 1)
                loaded.module.restore_state(self, loaded.handle, snapshot)
            except Exception as exc:
                candidate_cleanup_ok = True
                if loaded is not None:
                    try:
                        loaded.module.dispose(self, loaded.handle)
                    except Exception:
                        candidate_cleanup_ok = False
                    sys.modules.pop(loaded.module_name, None)
                try:
                    self._atomic_write(self._module_path(active.entry), active.source_bytes)
                    self._atomic_write(self._manifest_path, active.manifest_bytes)
                    rollback_ok = (self._reinitialize(active, snapshot)
                                   if dispose_attempted else True)
                except Exception:
                    rollback_ok = False
                if not rollback_ok or not candidate_cleanup_ok:
                    with self._condition:
                        self._state = "failed"
                        self._last_error = "rollback_failed"
                        self._applying = False
                        self._condition.notify_all()
                    raise EvolutionError("rollback_failed") from None
                with self._condition:
                    self._loaded = active
                    self._state = "ready"
                    self._generation = active.generation
                    self._last_error = f"rollback_rejected_{type(exc).__name__}"
                    self._applying = False
                    self._condition.notify_all()
                raise EvolutionError("rollback_apply_failed") from None

            with self._condition:
                self._loaded = loaded
                self._generation = loaded.generation
                self._state = "ready"
                self._last_error = None
                self._applying = False
                self._condition.notify_all()
            if active.module_name in sys.modules:
                del sys.modules[active.module_name]
            return self.status()

    def close(self) -> None:
        with self._cross_process_lock():
            with self._condition:
                if self._state == "closed":
                    return
                if self._policy_calls or self._applying:
                    raise EvolutionError("module_host_busy")
                self._state = "closing"
                loaded = self._loaded
            if loaded is not None:
                blockers = tuple(self._blockers(self._module_id))
                if blockers:
                    with self._condition:
                        self._state = "ready"
                    raise EvolutionError("active_work_blocked:" + ",".join(blockers))
                try:
                    snapshot = self._snapshot(loaded)
                    loaded.module.dispose(self, loaded.handle)
                except Exception as exc:
                    with self._condition:
                        self._state = "failed"
                        self._last_error = f"dispose_failed_{type(exc).__name__}"
                    raise EvolutionError("module_dispose_failed") from None
                if loaded.module_name in sys.modules:
                    del sys.modules[loaded.module_name]
            with self._condition:
                self._loaded = None
                self._state = "closed"
                self._condition.notify_all()

    def _finish_apply_failure(self, code: str) -> None:
        with self._condition:
            self._state = "ready"
            self._last_error = code
            self._applying = False
            self._condition.notify_all()

    def _snapshot(self, loaded: _Loaded):
        value = loaded.module.snapshot_state(self, loaded.handle)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > self._state_limit:
            raise EvolutionError("module_state_snapshot_too_large")
        return json.loads(raw.decode("utf-8"))

    def _load(self, entry: dict[str, object], source: bytes, manifest: bytes,
              path: Path, generation: int) -> _Loaded:
        compile(source.decode("utf-8-sig"), str(path), "exec")
        digest = _sha(source)
        module_name = (f"ai_debug_display.modules._evolution_"
                       f"{self._module_id.replace('-', '_')}_g{generation}_{digest[:12]}")
        module = ModuleType(module_name)
        module.__file__ = str(path)
        module.__package__ = "ai_debug_display.modules"
        sys.modules[module_name] = module
        try:
            exec(compile(source.decode("utf-8-sig"), str(path), "exec"), module.__dict__)
            if getattr(module, "MODULE_ID", None) != self._module_id:
                raise EvolutionError("module_id_mismatch")
            if getattr(module, "VERSION", None) != entry["version"]:
                raise EvolutionError("module_version_manifest_mismatch")
            if type(getattr(module, "API_VERSION", None)) is not int or module.API_VERSION != entry["api_version"]:
                raise EvolutionError("module_api_version_mismatch")
            lifecycle = entry["lifecycle"]
            functions = (lifecycle["initialize"], lifecycle["dispose"],
                         lifecycle["snapshot_state"], lifecycle["restore_state"])
            if any(not callable(getattr(module, name, None)) for name in functions):
                raise EvolutionError("module_lifecycle_contract_incomplete")
            handle = getattr(module, lifecycle["initialize"])(self, int(entry["api_version"]))
            return _Loaded(entry, module, handle, source, manifest, digest,
                           _sha(manifest), generation, module_name)
        except Exception:
            sys.modules.pop(module_name, None)
            raise

    def _reinitialize(self, loaded: _Loaded, snapshot) -> bool:
        try:
            lifecycle = loaded.entry["lifecycle"]
            handle = getattr(loaded.module, lifecycle["initialize"])(self, int(loaded.entry["api_version"]))
            getattr(loaded.module, lifecycle["restore_state"])(self, handle, snapshot)
            loaded.handle = handle
            return True
        except Exception:
            return False

    def _entry(self, manifest: dict[str, object], module_id: str) -> dict[str, object]:
        if set(manifest) != {"api_version", "watch_allowlist", "modules"}:
            raise EvolutionError("manifest_fields_invalid")
        if type(manifest.get("api_version")) is not int or manifest["api_version"] != MANIFEST_API_VERSION:
            raise EvolutionError("manifest_api_version_unsupported")
        if manifest.get("watch_allowlist") != []:
            raise EvolutionError("automatic_watcher_not_supported")
        modules = manifest.get("modules")
        if not isinstance(modules, list) or len(modules) != 1 or not isinstance(modules[0], dict):
            raise EvolutionError("runtime_supports_one_explicit_module")
        entry = modules[0]
        required = {"id", "path", "version", "api_version", "dependencies",
                    "reloadable", "restart_required", "auto_watch", "lifecycle"}
        if set(entry) != required or entry["id"] != module_id:
            raise EvolutionError("manifest_module_identity_invalid")
        if entry["reloadable"] is not True or entry["restart_required"] is not False or entry["auto_watch"] is not False:
            raise EvolutionError("manifest_module_reload_policy_invalid")
        if entry["dependencies"] != []:
            raise EvolutionError("module_dependencies_not_supported_in_first_slice")
        if type(entry["api_version"]) is not int or entry["api_version"] < 1:
            raise EvolutionError("module_api_version_invalid")
        _version_tuple(entry["version"])
        lifecycle = entry["lifecycle"]
        expected = {"initialize", "dispose", "snapshot_state", "restore_state"}
        if not isinstance(lifecycle, dict) or set(lifecycle) != expected or any(
                not isinstance(lifecycle[key], str) or not lifecycle[key] for key in expected):
            raise EvolutionError("module_lifecycle_invalid")
        self._module_path(entry)
        return dict(entry)

    def _module_path(self, entry: Mapping[str, object]) -> Path:
        value = entry.get("path")
        if not isinstance(value, str) or "\\" in value or ":" in value:
            raise EvolutionError("module_path_invalid")
        posix = PurePosixPath(value)
        if posix.is_absolute() or ".." in posix.parts or len(posix.parts) < 2 or posix.parts[0] != "modules" or posix.suffix != ".py":
            raise EvolutionError("module_path_not_allowlisted")
        candidate = self._root.joinpath(*posix.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise EvolutionError("module_source_missing") from None
        if self._root not in resolved.parents:
            raise EvolutionError("module_path_escapes_root")
        current = self._root
        for part in posix.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError:
                raise EvolutionError("module_source_missing") from None
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT:
                raise EvolutionError("module_reparse_point_rejected")
        if not resolved.is_file():
            raise EvolutionError("module_source_not_file")
        return resolved

    def _read_module(self, entry: Mapping[str, object]) -> tuple[bytes, Path]:
        path = self._module_path(entry)
        try:
            data = path.read_bytes()
        except OSError:
            raise EvolutionError("module_read_failed") from None
        if not data or len(data) > MAX_MODULE_BYTES:
            raise EvolutionError("module_size_invalid")
        return data, path

    def _read_manifest(self) -> tuple[bytes, dict[str, object]]:
        try:
            meta = self._manifest_path.lstat()
            if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & REPARSE_POINT:
                raise EvolutionError("manifest_reparse_point_rejected")
            raw = self._manifest_path.read_bytes()
        except OSError:
            raise EvolutionError("manifest_read_failed") from None
        if not raw or len(raw) > MAX_MANIFEST_BYTES:
            raise EvolutionError("manifest_size_invalid")
        try:
            obj = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs_no_duplicates)
        except EvolutionError:
            raise
        except (UnicodeError, json.JSONDecodeError):
            raise EvolutionError("manifest_invalid_json") from None
        if not isinstance(obj, dict):
            raise EvolutionError("manifest_must_be_object")
        return raw, obj

    def _backup_dir(self) -> Path:
        path = self._root / ".evolution" / "rollback" / self._module_id
        current = self._root
        for part in (".evolution", "rollback", self._module_id):
            current = current / part
            try:
                current.mkdir(exist_ok=True)
                metadata = current.lstat()
            except OSError:
                raise EvolutionError("rollback_directory_unavailable") from None
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT:
                raise EvolutionError("rollback_reparse_point_rejected")
        if self._root not in path.resolve(strict=True).parents:
            raise EvolutionError("rollback_directory_escapes_root")
        return path

    def _archive_loaded(self, loaded: _Loaded) -> None:
        digest = loaded.source_sha256
        directory = self._backup_dir()
        path = directory / f"{digest}.json"
        record = {
            "module_id": self._module_id,
            "relative_path": str(loaded.entry["path"]),
            "version": str(loaded.entry["version"]),
            "api_version": int(loaded.entry["api_version"]),
            "source_sha256": digest,
            "manifest_sha256": loaded.manifest_sha256,
            "source_b64": base64.b64encode(loaded.source_bytes).decode("ascii"),
            "manifest_b64": base64.b64encode(loaded.manifest_bytes).decode("ascii"),
            "created_ns": time.time_ns(),
        }
        raw = json.dumps(record, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if path.exists():
            existing = self._read_backup(digest)
            if existing != record and (existing["source_sha256"] != digest or
                                       existing["manifest_sha256"] != loaded.manifest_sha256):
                raise EvolutionError("rollback_backup_collision")
        else:
            self._atomic_write(path, raw)
        self._prune_backups(directory)

    def _read_backup(self, source_sha256: str) -> dict[str, object]:
        path = self._backup_dir() / f"{source_sha256}.json"
        try:
            meta = path.lstat()
            if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & REPARSE_POINT:
                raise EvolutionError("rollback_backup_reparse_point_rejected")
            raw = path.read_bytes()
            if len(raw) > (MAX_MANIFEST_BYTES + MAX_MODULE_BYTES * 2):
                raise EvolutionError("rollback_backup_too_large")
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
            if not isinstance(record, dict):
                raise EvolutionError("rollback_backup_invalid")
            source = base64.b64decode(record["source_b64"], validate=True)
            manifest = base64.b64decode(record["manifest_b64"], validate=True)
        except EvolutionError:
            raise
        except Exception:
            raise EvolutionError("rollback_backup_invalid") from None
        if (not isinstance(record, dict) or _sha(source) != record.get("source_sha256")
                or _sha(manifest) != record.get("manifest_sha256")
                or record.get("source_sha256") != source_sha256):
            raise EvolutionError("rollback_backup_hash_mismatch")
        return record

    def _prune_backups(self, directory: Path, keep: int = 2) -> None:
        records = []
        for path in directory.glob("*.json"):
            record = self._read_backup(path.stem)
            records.append((int(record["created_ns"]), path))
        records.sort(reverse=True)
        for _, path in records[keep:]:
            path.unlink()

    def _atomic_write(self, target: Path, data: bytes) -> None:
        target = target.resolve(strict=False)
        if self._root not in target.parents:
            raise EvolutionError("write_path_escapes_root")
        fd, temp_name = tempfile.mkstemp(prefix=".evolution-tmp-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    @contextmanager
    def _cross_process_lock(self):
        path = self._root / ".evolution" / "locks" / f"{self._module_id}.lock"
        current = self._root
        for part in (".evolution", "locks"):
            current = current / part
            current.mkdir(exist_ok=True)
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT:
                raise EvolutionError("lock_directory_reparse_point_rejected")
        if path.exists():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT:
                raise EvolutionError("lock_file_reparse_point_rejected")
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

