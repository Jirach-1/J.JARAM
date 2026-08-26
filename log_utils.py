from __future__ import annotations

import atexit
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Optional


ROBLOX_LOGS_DIR = os.path.join(
    os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"),
    "Roblox",
    "logs",
)

_USERNAME_MARKER = re.compile(rb"Players\.([^.\r\n]+)\.")
_VALID_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_SESSION_IN_NAME = re.compile(r"_(\d{8}T\d{6}Z)_", re.IGNORECASE)

# Disconnect strings used by MultiScope's line dispatcher.
R_DISC_REASON = re.compile(r"\[FLog::Network\]\s+Disconnect reason received:\s*(\d+)", re.I)
R_DISC_NOTIFY = re.compile(r"\[FLog::Network\]\s+Disconnection Notification\.\s*Reason:\s*(\d+)", re.I)
R_DISC_SENDING = re.compile(r"\[FLog::Network\]\s+Sending disconnect with reason:\s*(\d+)", re.I)
R_CONN_LOST = re.compile(r"\[FLog::Network\]\s+Connection lost", re.I)

_DEFAULT_DISCOVERY_SECONDS = 30 * 60
_POLL_INTERVAL_SECONDS = 1.0
_UNHEALTHY_AFTER_SECONDS = 10.0
_UNHEALTHY_AFTER_FAILURES = 3
_SCAN_CHUNK_BYTES = 256 * 1024
_SCAN_BUDGET_BYTES = 4 * 1024 * 1024
_MARKER_CARRY_BYTES = 128

_DEBUG = str(os.environ.get("JARAM_LOG_READER_DEBUG", "")).strip().lower() in {
    "1", "true", "yes", "on",
}
_LOGGER = logging.getLogger("jaram.log_reader")


def _debug(message: str, *args: object) -> None:
    if _DEBUG:
        _LOGGER.debug(message, *args)


class ReaderHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class LogMatch:
    username: str
    path: str
    generation_id: str
    session_started_at: float
    marker_offset: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class LogLookupResult:
    status: str
    match: Optional[LogMatch] = None
    health: ReaderHealth = ReaderHealth.HEALTHY

    @property
    def is_match(self) -> bool:
        return self.status == "matched" and self.match is not None

    @property
    def is_conclusive(self) -> bool:
        return self.status in {"matched", "conclusively_missing"}


@dataclass
class _IndexedFile:
    path: str
    norm_path: str
    identity: tuple[int, int, int]
    generation_id: str
    session_started_at: float
    size: int
    mtime_ns: int
    scan_pos: int = 0
    marker_carry: bytes = b""
    markers: Dict[str, int] = field(default_factory=dict)
    classified: bool = False
    read_pending: bool = True
    read_error: bool = False
    revision: int = 0


class _IndexWatchHandler:
    """Small adapter so importing this module does not require watchdog."""

    def __init__(self, index: "RobloxLogIndex") -> None:
        self._index = index

    def dispatch(self, event) -> None:
        try:
            self._index.mark_dirty(getattr(event, "src_path", ""))
            self._index.mark_dirty(getattr(event, "dest_path", ""))
        except Exception:
            pass


class RobloxLogIndex:
    """Incremental, thread-safe index of Roblox usernames to log generations."""

    def __init__(
        self,
        logs_dir: str | os.PathLike[str] = ROBLOX_LOGS_DIR,
        *,
        discovery_seconds: float = _DEFAULT_DISCOVERY_SECONDS,
        enable_watcher: bool = True,
    ) -> None:
        self.logs_dir = os.path.abspath(os.fspath(logs_dir))
        self.discovery_seconds = max(1.0, float(discovery_seconds))
        self.enable_watcher = bool(enable_watcher)
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._files: Dict[str, _IndexedFile] = {}
        self._pinned_generations: set[str] = set()
        self._unreadable_candidates: set[str] = set()
        self._dirty_paths: set[str] = set()
        self._next_poll_at = 0.0
        self._created_mono = time.monotonic()
        self._last_success_mono = 0.0
        self._consecutive_failures = 0
        self._observer = None
        self._closed = False
        self._stats = {
            "refreshes": 0,
            "refresh_failures": 0,
            "files_enumerated": 0,
            "files_scanned": 0,
            "bytes_scanned": 0,
            "read_failures": 0,
            "truncations": 0,
            "replacements": 0,
        }

    @staticmethod
    def _normalize(path: str | os.PathLike[str]) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))

    @staticmethod
    def _session_started_at(path: str, st: os.stat_result) -> float:
        match = _SESSION_IN_NAME.search(os.path.basename(path))
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except Exception:
                pass
        try:
            return float(st.st_ctime)
        except Exception:
            return float(st.st_mtime)

    @staticmethod
    def _identity(st: os.stat_result) -> tuple[int, int, int]:
        # st_ctime is a change timestamp on Unix and would manufacture a new
        # generation on every append. Prefer a true birth time when exposed.
        birth_ns = getattr(st, "st_birthtime_ns", None)
        if birth_ns is None:
            birth = getattr(st, "st_birthtime", None)
            birth_ns = int(float(birth) * 1_000_000_000) if birth is not None else 0
        return (
            int(getattr(st, "st_dev", 0) or 0),
            int(getattr(st, "st_ino", 0) or 0),
            int(birth_ns or 0),
        )

    @classmethod
    def _generation_id(
        cls,
        norm_path: str,
        identity: tuple[int, int, int],
        revision: int = 0,
    ) -> str:
        return f"{norm_path}|{identity[0]}:{identity[1]}:{identity[2]}:{int(revision)}"

    def mark_dirty(self, path: str | os.PathLike[str] | None = None) -> None:
        with self._lock:
            if path:
                try:
                    self._dirty_paths.add(self._normalize(path))
                except Exception:
                    self._dirty_paths.add("")
            else:
                self._dirty_paths.add("")
            self._next_poll_at = 0.0

    def pin(self, match: LogMatch | str) -> None:
        generation_id = match.generation_id if isinstance(match, LogMatch) else str(match or "")
        if generation_id:
            with self._lock:
                self._pinned_generations.add(generation_id)

    def unpin(self, match: LogMatch | str) -> None:
        generation_id = match.generation_id if isinstance(match, LogMatch) else str(match or "")
        if generation_id:
            with self._lock:
                self._pinned_generations.discard(generation_id)

    def invalidate_generation(self, path: str | os.PathLike[str]) -> None:
        """Force a same-path file to be rediscovered as a new generation."""
        norm_path = self._normalize(path)
        with self._lock:
            state = self._files.get(norm_path)
            if state is None:
                self.mark_dirty(path)
                return
            self._pinned_generations.discard(state.generation_id)
            state.revision += 1
            state.generation_id = self._generation_id(norm_path, state.identity, state.revision)
            state.scan_pos = 0
            state.marker_carry = b""
            state.markers.clear()
            state.classified = False
            state.read_pending = True
            state.read_error = False
            self._stats["truncations"] += 1
            self._dirty_paths.add(norm_path)
            self._next_poll_at = 0.0

    def has_generation(self, generation_id: str) -> bool:
        generation = str(generation_id or "")
        with self._lock:
            return any(state.generation_id == generation for state in self._files.values())

    def generation_sizes(self) -> Dict[str, int]:
        with self._lock:
            return {state.generation_id: int(state.size) for state in self._files.values()}

    def _ensure_observer(self) -> None:
        with self._lock:
            if self._closed or self._observer is not None or not self.enable_watcher:
                return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            adapter = _IndexWatchHandler(self)

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event) -> None:
                    adapter.dispatch(event)

            observer = Observer()
            observer.daemon = True
            observer.schedule(Handler(), self.logs_dir, recursive=False)
            observer.start()
        except Exception as exc:
            _debug("log-index watcher unavailable for %s: %r", self.logs_dir, exc)
            return
        with self._lock:
            if self._closed:
                try:
                    observer.stop()
                    observer.join(timeout=1.0)
                except Exception:
                    pass
            else:
                self._observer = observer

    def _record_refresh_failure(self, exc: BaseException) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._stats["refresh_failures"] += 1
            self._next_poll_at = time.monotonic() + _POLL_INTERVAL_SECONDS
        _debug("log-index refresh failed for %s: %r", self.logs_dir, exc)

    def refresh(self, *, force: bool = False) -> ReaderHealth:
        now_mono = time.monotonic()
        with self._lock:
            if self._closed:
                return ReaderHealth.UNHEALTHY
            if not force and not self._dirty_paths and now_mono < self._next_poll_at:
                return self.health()

        entries: list[tuple[str, str, os.stat_result]] = []
        unreadable_candidates: set[str] = set()
        try:
            with os.scandir(self.logs_dir) as it:
                for entry in it:
                    if not entry.name.lower().endswith(".log"):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        path = os.path.abspath(entry.path)
                        entries.append((path, self._normalize(path), st))
                    except (FileNotFoundError, PermissionError, OSError):
                        try:
                            unreadable_candidates.add(self._normalize(entry.path))
                        except Exception:
                            pass
                        continue
        except (FileNotFoundError, PermissionError, OSError) as exc:
            self._record_refresh_failure(exc)
            return self.health()

        seen: set[str] = set()
        now_wall = time.time()
        with self._lock:
            self._stats["refreshes"] += 1
            self._stats["files_enumerated"] += len(entries)
            self._last_success_mono = now_mono
            self._consecutive_failures = 0
            self._next_poll_at = now_mono + _POLL_INTERVAL_SECONDS
            self._dirty_paths.clear()
            self._unreadable_candidates = unreadable_candidates

            # A candidate which was indexed successfully on an earlier pass is
            # still the same candidate when a later stat/read is denied.  Keep
            # that state around and make only usernames already associated with
            # it inconclusive.  A brand-new unreadable filename cannot safely be
            # attributed to every account; doing so suspended every preconnect
            # watchdog indefinitely because of one unrelated bad file.
            seen.update(unreadable_candidates)
            for norm_path in unreadable_candidates:
                unreadable = self._files.get(norm_path)
                if unreadable is not None:
                    unreadable.read_error = True
                    unreadable.read_pending = True

            for path, norm_path, st in entries:
                seen.add(norm_path)
                identity = self._identity(st)
                size = int(st.st_size)
                mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
                current = self._files.get(norm_path)
                if current is None or current.identity != identity:
                    if current is not None:
                        self._stats["replacements"] += 1
                        self._pinned_generations.discard(current.generation_id)
                    session_ts = self._session_started_at(path, st)
                    self._files[norm_path] = _IndexedFile(
                        path=path,
                        norm_path=norm_path,
                        identity=identity,
                        generation_id=self._generation_id(norm_path, identity),
                        session_started_at=session_ts,
                        size=size,
                        mtime_ns=mtime_ns,
                        read_pending=(max(session_ts, float(st.st_mtime)) >= now_wall - self.discovery_seconds),
                    )
                    continue

                # Metadata access recovered.  If content is still unreadable,
                # the incremental read below will set this flag again.
                current.read_error = False
                if size < current.size:
                    self._pinned_generations.discard(current.generation_id)
                    current.revision += 1
                    current.generation_id = self._generation_id(norm_path, identity, current.revision)
                    current.scan_pos = 0
                    current.marker_carry = b""
                    current.markers.clear()
                    current.classified = False
                    current.read_pending = True
                    current.read_error = False
                    self._stats["truncations"] += 1
                elif size > current.scan_pos and not current.classified:
                    current.read_pending = True
                current.path = path
                current.size = size
                current.mtime_ns = mtime_ns

            for norm_path in tuple(self._files):
                if norm_path not in seen:
                    removed = self._files.pop(norm_path, None)
                    if removed is not None:
                        self._pinned_generations.discard(removed.generation_id)

        self._ensure_observer()
        return self.health()

    def _states_for_scan(self, not_before: Optional[float] = None) -> list[_IndexedFile]:
        cutoff = time.time() - self.discovery_seconds if not_before is None else float(not_before)
        with self._lock:
            states = []
            for state in self._files.values():
                if state.classified:
                    continue
                if state.session_started_at < cutoff and (state.mtime_ns / 1_000_000_000) < cutoff:
                    continue
                if state.scan_pos < state.size or state.read_error:
                    state.read_pending = True
                    states.append(state)
            states.sort(key=lambda s: (s.session_started_at, s.mtime_ns), reverse=True)
            return states

    def _scan_pending(
        self,
        *,
        not_before: Optional[float] = None,
        max_bytes: int = _SCAN_BUDGET_BYTES,
    ) -> int:
        with self._scan_lock:
            return self._scan_pending_serial(not_before=not_before, max_bytes=max_bytes)

    def _scan_pending_serial(
        self,
        *,
        not_before: Optional[float] = None,
        max_bytes: int = _SCAN_BUDGET_BYTES,
    ) -> int:
        remaining = max(0, int(max_bytes))
        scanned = 0
        for state in self._states_for_scan(not_before):
            if remaining <= 0:
                break
            with self._lock:
                if self._files.get(state.norm_path) is not state:
                    continue
                start = state.scan_pos
                size = state.size
                path = state.path
                carry = state.marker_carry
                generation_id = state.generation_id
            want = min(_SCAN_CHUNK_BYTES, remaining, max(0, size - start))
            if want <= 0:
                with self._lock:
                    state.read_pending = False
                    state.read_error = False
                continue
            try:
                with open(path, "rb") as handle:
                    handle.seek(start)
                    raw = handle.read(want)
            except (FileNotFoundError, PermissionError, OSError) as exc:
                with self._lock:
                    state.read_error = True
                    state.read_pending = True
                    self._stats["read_failures"] += 1
                _debug("log-index read failed for %s: %r", state.path, exc)
                continue
            if not raw:
                with self._lock:
                    state.read_pending = False
                    state.read_error = False
                continue

            combined = carry + raw
            combined_base = max(0, start - len(carry))
            found: Dict[str, int] = {}
            for marker in _USERNAME_MARKER.finditer(combined):
                try:
                    username = marker.group(1).decode("ascii").strip()
                except Exception:
                    continue
                if not _VALID_USERNAME.fullmatch(username):
                    continue
                key = username.lower()
                found[key] = max(found.get(key, -1), combined_base + marker.start())

            with self._lock:
                if (
                    self._files.get(state.norm_path) is not state
                    or state.generation_id != generation_id
                    or state.scan_pos != start
                ):
                    continue
                state.scan_pos += len(raw)
                state.marker_carry = combined[-_MARKER_CARRY_BYTES:]
                state.read_error = False
                state.markers.update(found)
                # Username identity is established by the first marker-bearing chunk.
                # Stop reading this active log so discovery does not duplicate tail I/O.
                if state.markers:
                    state.classified = True
                    state.read_pending = False
                    state.marker_carry = b""
                else:
                    state.read_pending = state.scan_pos < state.size
                self._stats["files_scanned"] += 1
                self._stats["bytes_scanned"] += len(raw)
            scanned += len(raw)
            remaining -= len(raw)
        return scanned

    def poll(
        self,
        *,
        force: bool = False,
        not_before: Optional[float] = None,
    ) -> ReaderHealth:
        health = self.refresh(force=force)
        if health is not ReaderHealth.UNHEALTHY:
            self._scan_pending(
                not_before=not_before,
                max_bytes=_SCAN_BUDGET_BYTES,
            )
        return self.health()

    def health(self) -> ReaderHealth:
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return ReaderHealth.UNHEALTHY
            since_success = (
                now - self._last_success_mono
                if self._last_success_mono > 0
                else now - self._created_mono
            )
            if self._consecutive_failures >= _UNHEALTHY_AFTER_FAILURES or since_success >= _UNHEALTHY_AFTER_SECONDS:
                return ReaderHealth.UNHEALTHY
            if self._last_success_mono <= 0:
                return ReaderHealth.DEGRADED
            if self._consecutive_failures:
                return ReaderHealth.DEGRADED
            return ReaderHealth.HEALTHY

    def lookup(
        self,
        username: str,
        *,
        not_before: Optional[float] = None,
        refresh_index: bool = True,
    ) -> LogLookupResult:
        key = str(username or "").strip().lower()
        if not key:
            return LogLookupResult("conclusively_missing", health=self.health())

        # Callers resolving many users in one heartbeat can advance the index
        # once and then perform read-only lookups.  Previously every username
        # forced another directory refresh and up to two 4 MiB scan passes,
        # making resume latency scale linearly with the account count.
        if refresh_index:
            self.poll(not_before=not_before)

        health = self.health()
        with self._lock:
            cutoff = time.time() - self.discovery_seconds if not_before is None else float(not_before)
            candidates = [
                state
                for state in self._files.values()
                if key in state.markers
                and (
                    (not_before is None and state.generation_id in self._pinned_generations)
                    or state.session_started_at >= cutoff
                )
            ]
            if candidates:
                state = max(candidates, key=lambda s: (s.session_started_at, s.mtime_ns, s.norm_path))
                return LogLookupResult(
                    "matched",
                    LogMatch(
                        username=key,
                        path=state.path,
                        generation_id=state.generation_id,
                        session_started_at=state.session_started_at,
                        marker_offset=int(state.markers[key]),
                        size=state.size,
                        mtime_ns=state.mtime_ns,
                    ),
                    health,
                )

            relevant = [
                state for state in self._files.values()
                if state.session_started_at >= cutoff or (state.mtime_ns / 1_000_000_000) >= cutoff
            ]
            pending = any(
                (
                    state.read_error
                    and (key in state.markers or not state.markers)
                )
                or (not state.classified and state.scan_pos < state.size)
                for state in relevant
            )

        if health is not ReaderHealth.HEALTHY:
            return LogLookupResult("unhealthy", health=health)
        if pending:
            return LogLookupResult("pending", health=health)
        return LogLookupResult("conclusively_missing", health=health)

    def newest_log(self) -> Optional[LogMatch]:
        self.poll()
        with self._lock:
            states = list(self._files.values())
            if not states:
                return None
            state = max(states, key=lambda s: (s.session_started_at, s.mtime_ns, s.norm_path))
            username = next(iter(state.markers), "")
            return LogMatch(
                username=username,
                path=state.path,
                generation_id=state.generation_id,
                session_started_at=state.session_started_at,
                marker_offset=int(state.markers.get(username, -1)),
                size=state.size,
                mtime_ns=state.mtime_ns,
            )

    def diagnostics_snapshot(self) -> dict:
        with self._lock:
            out = dict(self._stats)
            out.update({
                "health": self.health().value,
                "indexed_files": len(self._files),
                "classified_files": sum(bool(s.classified) for s in self._files.values()),
                "pending_files": sum(bool(s.read_pending) for s in self._files.values()),
                "unreadable_files": (
                    sum(bool(s.read_error) for s in self._files.values())
                    + len(self._unreadable_candidates)
                ),
                "pinned_generations": len(self._pinned_generations),
                "dirty_paths": len(self._dirty_paths),
                "watcher_active": self._observer is not None,
                "seconds_since_success": (
                    max(0.0, time.monotonic() - self._last_success_mono)
                    if self._last_success_mono > 0
                    else None
                ),
                "consecutive_failures": self._consecutive_failures,
            })
            return out

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            observer = self._observer
            self._observer = None
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception:
                pass


@dataclass
class _PreconnectState:
    launch_token: float
    healthy_missing_seconds: float = 0.0
    last_check_mono: float = 0.0
    last_was_conclusive_missing: bool = False
    confirmed: bool = False
    timed_out: bool = False


class PreconnectTracker:
    """Pure healthy-time gate shared by GUI and headless watchdogs."""

    def __init__(self, grace_seconds: float = 360.0) -> None:
        self.grace_seconds = max(0.0, float(grace_seconds))
        self._states: Dict[str, _PreconnectState] = {}

    def reset(self, uid: str) -> None:
        self._states.pop(str(uid), None)

    def observe(
        self,
        uid: str,
        *,
        launch_token: float,
        live: bool,
        lookup: LogLookupResult,
        now_mono: Optional[float] = None,
    ) -> str:
        key = str(uid)
        if not live:
            self.reset(key)
            return "inactive"
        now = time.monotonic() if now_mono is None else float(now_mono)
        token = float(launch_token or 0.0)
        state = self._states.get(key)
        if state is None or abs(state.launch_token - token) > 0.001:
            state = _PreconnectState(launch_token=token, last_check_mono=now)
            self._states[key] = state
        if state.confirmed:
            return "confirmed"
        if state.timed_out:
            return "timed_out"
        elapsed = max(0.0, now - state.last_check_mono)
        if state.last_was_conclusive_missing:
            state.healthy_missing_seconds += elapsed
        healthy_missing = (
            lookup.status == "conclusively_missing"
            and lookup.health is ReaderHealth.HEALTHY
        )
        state.last_was_conclusive_missing = healthy_missing
        if lookup.is_match:
            state.confirmed = True
            state.last_check_mono = now
            return "confirmed"
        state.last_check_mono = now
        if not healthy_missing:
            return "suspended"
        if state.healthy_missing_seconds >= self.grace_seconds:
            state.timed_out = True
            return "timed_out"
        return "waiting"

    def healthy_missing_seconds(self, uid: str) -> float:
        state = self._states.get(str(uid))
        return float(state.healthy_missing_seconds if state else 0.0)


_INDEX_LOCK = threading.Lock()
_INDEX: Optional[RobloxLogIndex] = None

# Compatibility view retained for older introspection/tests.
_CACHE: dict[str, object] = {"map": {}, "expire_at": 0.0}


def get_roblox_log_index() -> RobloxLogIndex:
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = RobloxLogIndex()
        return _INDEX


def find_log_match(
    username: str,
    *,
    not_before: Optional[float] = None,
    refresh_index: bool = True,
) -> LogLookupResult:
    return get_roblox_log_index().lookup(
        username,
        not_before=not_before,
        refresh_index=refresh_index,
    )


def find_log_for_username(
    username: str,
    allow_fallback: bool = False,
    *,
    not_before: Optional[float] = None,
) -> Optional[str]:
    result = find_log_match(username, not_before=not_before)
    if result.match is not None:
        return result.match.path
    if allow_fallback:
        newest = get_roblox_log_index().newest_log()
        return newest.path if newest else None
    return None


def find_newest_log() -> Optional[str]:
    match = get_roblox_log_index().newest_log()
    return match.path if match else None


def refresh_username_log_map() -> None:
    index = get_roblox_log_index()
    index.poll(force=True)
    mapping: dict[str, str] = {}
    with index._lock:
        for state in index._files.values():
            for username in state.markers:
                previous = mapping.get(username)
                if previous is None:
                    mapping[username] = state.path
                    continue
                prev_state = index._files.get(index._normalize(previous))
                if prev_state is None or (state.session_started_at, state.mtime_ns) > (
                    prev_state.session_started_at,
                    prev_state.mtime_ns,
                ):
                    mapping[username] = state.path
    _CACHE["map"] = mapping
    _CACHE["expire_at"] = time.time() + _POLL_INTERVAL_SECONDS


def shutdown_log_index() -> None:
    global _INDEX
    with _INDEX_LOCK:
        index = _INDEX
        _INDEX = None
    if index is not None:
        index.shutdown()


atexit.register(shutdown_log_index)
