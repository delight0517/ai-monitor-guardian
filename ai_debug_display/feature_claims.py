"""Cross-process feature edit claims for cooperating project writers.

Claims serialize writers of one feature while permitting disjoint feature
claims in parallel. The reserved integration claim can be acquired only when
all feature claims have drained, and excludes new feature claims until release.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import threading
import time
from typing import Iterator


REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class FeatureClaimError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FeatureClaimInfo:
    feature_id: str
    owner: str
    pid: int
    started_ns: int


@dataclass
class _HeldLock:
    path: Path
    stream: object

    def release(self) -> None:
        stream = self.stream
        if stream is None:
            return
        self.stream = None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class FeatureClaimCoordinator:
    def __init__(self, canonical_root: str | Path,
                 ownership_manifest: str = "feature-ownership.json"):
        self.root = Path(canonical_root).resolve(strict=True)
        self.manifest_path = self.root / ownership_manifest
        self.features, self.integration_id = self._load_ownership()
        self._thread_gate = threading.RLock()

    def feature_files(self, feature_id: str) -> tuple[str, ...]:
        if feature_id not in self.features:
            raise FeatureClaimError("feature_not_registered")
        return self.features[feature_id]

    @contextmanager
    def claim(self, feature_id: str, *, owner: str,
              timeout_seconds: float = 0.0) -> Iterator[FeatureClaimInfo]:
        if feature_id not in self.features:
            raise FeatureClaimError("feature_not_registered")
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 128 or any(ord(c) < 32 for c in owner):
            raise FeatureClaimError("claim_owner_invalid")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds < 0 or timeout_seconds > 3600):
            raise ValueError("claim_timeout_out_of_range")
        deadline = time.monotonic() + timeout_seconds
        held: _HeldLock | None = None
        while held is None:
            with self._thread_gate:
                gate = self._open_lock(".gate")
                if not self._try_lock(gate):
                    gate.stream.close()
                    gate.stream = None
                else:
                    try:
                        held = self._try_acquire_feature(feature_id)
                    finally:
                        gate.release()
            if held is None:
                if time.monotonic() >= deadline:
                    raise FeatureClaimError("feature_claim_busy_or_integration_active")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

        info = FeatureClaimInfo(feature_id, owner.strip(), os.getpid(), time.time_ns())
        try:
            self._write_info(held, info)
            yield info
        finally:
            try:
                self._clear_info(held)
            finally:
                held.release()

    def active_claims(self) -> tuple[FeatureClaimInfo, ...]:
        found: list[FeatureClaimInfo] = []
        with self._thread_gate:
            gate = self._open_lock(".gate")
            self._lock_wait(gate, 5.0)
            try:
                for feature_id in self.features:
                    lock = self._open_lock(feature_id)
                    if self._try_lock(lock):
                        lock.release()
                        continue
                    info = self._read_info(lock)
                    if info is not None:
                        found.append(info)
                    lock.stream.close()
                    lock.stream = None
            finally:
                gate.release()
        return tuple(found)

    def _try_acquire_feature(self, feature_id: str) -> _HeldLock | None:
        if feature_id == self.integration_id:
            locks: list[_HeldLock] = []
            try:
                integration = self._open_lock(feature_id)
                if not self._try_lock(integration):
                    integration.stream.close()
                    integration.stream = None
                    return None
                locks.append(integration)
                for other_id in self.features:
                    if other_id == feature_id:
                        continue
                    other = self._open_lock(other_id)
                    if not self._try_lock(other):
                        other.stream.close()
                        other.stream = None
                        for lock in reversed(locks):
                            lock.release()
                        return None
                    locks.append(other)
                # Integration retains only its own lock. Other feature locks are probes.
                for lock in locks[1:]:
                    lock.release()
                return integration
            except Exception:
                for lock in reversed(locks):
                    lock.release()
                raise

        target = self._open_lock(feature_id)
        if not self._try_lock(target):
            target.stream.close()
            target.stream = None
            return None
        try:
            integration = self._open_lock(self.integration_id)
        except Exception:
            target.release()
            raise
        if not self._try_lock(integration):
            integration.stream.close()
            integration.stream = None
            target.release()
            return None
        try:
            integration.release()
        except Exception:
            target.release()
            raise
        return target

    def _load_ownership(self) -> tuple[dict[str, tuple[str, ...]], str]:
        try:
            meta = self.manifest_path.lstat()
            if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & REPARSE_POINT:
                raise FeatureClaimError("ownership_manifest_reparse_point")
            raw = self.manifest_path.read_bytes()
            if not raw or len(raw) > 256 * 1024:
                raise FeatureClaimError("ownership_manifest_size_invalid")
            doc = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=self._unique_pairs)
        except FeatureClaimError:
            raise
        except Exception:
            raise FeatureClaimError("ownership_manifest_invalid") from None
        if (not isinstance(doc, dict) or set(doc) != {"api_version", "integration_scope", "features"}
                or type(doc["api_version"]) is not int or doc["api_version"] != 1):
            raise FeatureClaimError("ownership_manifest_schema_invalid")
        integration_id = doc["integration_scope"]
        if not isinstance(integration_id, str) or not _ID_RE.fullmatch(integration_id):
            raise FeatureClaimError("integration_scope_invalid")
        items = doc["features"]
        if not isinstance(items, list) or not items:
            raise FeatureClaimError("ownership_features_invalid")
        result: dict[str, tuple[str, ...]] = {}
        owners: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict) or set(item) != {"id", "files"}:
                raise FeatureClaimError("ownership_entry_invalid")
            feature_id, paths = item["id"], item["files"]
            if not isinstance(feature_id, str) or not _ID_RE.fullmatch(feature_id) or feature_id in result:
                raise FeatureClaimError("ownership_feature_id_invalid")
            if not isinstance(paths, list) or not paths:
                raise FeatureClaimError("ownership_paths_invalid")
            normalized: list[str] = []
            for value in paths:
                if (not isinstance(value, str) or "\\" in value or ":" in value
                        or value.startswith("/") or ".." in PurePosixPath(value).parts):
                    raise FeatureClaimError("ownership_path_invalid")
                if "*" in value and (not value.endswith("/**") or value.count("*") != 2):
                    raise FeatureClaimError("ownership_glob_invalid")
                for existing, existing_owner in owners.items():
                    if existing_owner != feature_id and self._paths_overlap(value, existing):
                        raise FeatureClaimError("ownership_path_overlap")
                owners[value] = feature_id
                normalized.append(value)
            result[feature_id] = tuple(normalized)
        if integration_id not in result:
            raise FeatureClaimError("integration_scope_not_registered")
        return result, integration_id

    @staticmethod
    def _paths_overlap(first: str, second: str) -> bool:
        def prefix(pattern: str) -> str | None:
            return pattern[:-3].rstrip("/") if pattern.endswith("/**") else None
        first_prefix, second_prefix = prefix(first), prefix(second)
        if first == second:
            return True
        if first_prefix is not None and second_prefix is not None:
            return (first_prefix == second_prefix
                    or first_prefix.startswith(second_prefix + "/")
                    or second_prefix.startswith(first_prefix + "/"))
        if first_prefix is not None:
            return second == first_prefix or second.startswith(first_prefix + "/")
        if second_prefix is not None:
            return first == second_prefix or first.startswith(second_prefix + "/")
        return False

    @staticmethod
    def _unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise FeatureClaimError("ownership_duplicate_key")
            result[key] = value
        return result

    def _open_lock(self, name: str) -> _HeldLock:
        directory = self.root / ".evolution" / "claims"
        current = self.root
        for part in (".evolution", "claims"):
            current = current / part
            try:
                current.mkdir(exist_ok=True)
                meta = current.lstat()
            except OSError:
                raise FeatureClaimError("claim_lock_directory_unavailable") from None
            if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & REPARSE_POINT:
                raise FeatureClaimError("claim_lock_directory_reparse_point")
        path = directory / f"{name}.lock"
        try:
            if path.exists():
                meta = path.lstat()
                if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & REPARSE_POINT:
                    raise FeatureClaimError("claim_lock_file_reparse_point")
            stream = path.open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            return _HeldLock(path, stream)
        except FeatureClaimError:
            raise
        except OSError:
            raise FeatureClaimError("claim_lock_file_unavailable") from None

    @staticmethod
    def _try_lock(held: _HeldLock) -> bool:
        held.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(held.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(held.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            return False

    def _lock_wait(self, held: _HeldLock, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self._try_lock(held):
            if time.monotonic() >= deadline:
                held.stream.close()
                held.stream = None
                raise FeatureClaimError("claim_registry_busy")
            time.sleep(0.025)

    @staticmethod
    def _write_info(held: _HeldLock, info: FeatureClaimInfo) -> None:
        data = json.dumps(info.__dict__, separators=(",", ":")).encode("utf-8")
        held.stream.seek(1)
        held.stream.truncate()
        held.stream.write(data)
        held.stream.flush()
        os.fsync(held.stream.fileno())

    @staticmethod
    def _clear_info(held: _HeldLock) -> None:
        if held.stream is None:
            return
        held.stream.seek(1)
        held.stream.truncate()
        held.stream.flush()

    @staticmethod
    def _read_info(held: _HeldLock) -> FeatureClaimInfo | None:
        try:
            held.stream.seek(1)
            raw = held.stream.read(4096)
            value = json.loads(raw.decode("utf-8"))
            return FeatureClaimInfo(str(value["feature_id"]), str(value["owner"]),
                                    int(value["pid"]), int(value["started_ns"]))
        except Exception:
            return None
