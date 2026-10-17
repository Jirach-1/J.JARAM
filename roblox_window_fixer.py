"""Reliable launch-time sizing for Roblox windows.

Roblox can replace or reconfigure its top-level window several seconds after the
process is detected.  A successful ``SetWindowPos`` call therefore does not mean
the requested geometry actually stuck.  This module keeps the retry/verification
state independent from the launcher so it can be exercised without real windows.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import psutil
import win32con
import win32gui
import win32process


Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class WindowGeometry:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_settings(cls, settings: dict) -> "WindowGeometry":
        return cls(
            x=int(settings.get("x", 0) or 0),
            y=int(settings.get("y", 0) or 0),
            width=int(settings.get("w", 0) or 0),
            height=int(settings.get("h", 0) or 0),
        )

    @property
    def is_valid(self) -> bool:
        # SetWindowPos uses signed 32-bit coordinates and dimensions.  Reject
        # damaged/manual settings rather than overflowing in the native call.
        limit = (1 << 31) - 1
        return (
            0 < self.width <= limit
            and 0 < self.height <= limit
            and -limit <= self.x <= limit
            and -limit <= self.y <= limit
        )

    @property
    def rect(self) -> Rect:
        return (
            self.x,
            self.y,
            self.x + self.width,
            self.y + self.height,
        )

    def matches(self, rect: Rect, tolerance: int = 2) -> bool:
        left, top, right, bottom = (int(value) for value in rect)
        tolerance = max(0, int(tolerance))
        return (
            abs(left - self.x) <= tolerance
            and abs(top - self.y) <= tolerance
            and abs((right - left) - self.width) <= tolerance
            and abs((bottom - top) - self.height) <= tolerance
        )


@dataclass(frozen=True)
class WindowFixResult:
    success: bool
    reason: str
    attempts: int
    hwnd: Optional[int] = None
    actual_rect: Optional[Rect] = None


class WindowBackend(Protocol):
    """Native operations used by :class:`RobloxWindowGeometryFixer`."""

    def discover_related_pids(self, root_pid: int, known_pids: set[int]) -> set[int]: ...

    def find_best_window(
        self,
        target_pids: set[int],
        *,
        preferred_hwnd: Optional[int],
        include_hidden: bool,
    ) -> Optional[int]: ...

    def is_target_window(self, hwnd: int, target_pids: set[int]) -> bool: ...

    def get_rect(self, hwnd: int) -> Rect: ...

    def prepare_window(self, hwnd: int) -> bool: ...

    def set_geometry(self, hwnd: int, geometry: WindowGeometry) -> None: ...

    def move_geometry(self, hwnd: int, geometry: WindowGeometry) -> None: ...


class Win32WindowBackend:
    """pywin32 implementation of the native window operations."""

    def __init__(self) -> None:
        # Pair PIDs with their creation time so an unusually fast PID reuse can
        # never redirect an in-flight fixer to an unrelated application.
        self._pid_create_times: dict[int, float] = {}

    def discover_related_pids(self, root_pid: int, known_pids: set[int]) -> set[int]:
        candidates = {int(root_pid), *(int(pid) for pid in known_pids)}
        discovered: set[int] = set()

        # Keep previously discovered children in the search set.  Roblox may
        # detach a child from a short-lived bootstrap process during startup.
        for pid in tuple(candidates):
            try:
                process = psutil.Process(pid)
                created_at = float(process.create_time())
                expected_created_at = self._pid_create_times.get(pid)
                if (
                    expected_created_at is not None
                    and abs(created_at - expected_created_at) > 0.001
                ):
                    continue
                self._pid_create_times[pid] = created_at
                discovered.add(pid)
                for child in process.children(recursive=True):
                    child_pid = int(child.pid)
                    if child_pid <= 0:
                        continue
                    try:
                        child_created_at = float(child.create_time())
                    except (psutil.AccessDenied, OSError, ValueError):
                        discovered.add(child_pid)
                        continue
                    expected_child_created_at = self._pid_create_times.get(child_pid)
                    if (
                        expected_child_created_at is not None
                        and abs(child_created_at - expected_child_created_at) > 0.001
                    ):
                        continue
                    self._pid_create_times[child_pid] = child_created_at
                    discovered.add(child_pid)
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError, ValueError):
                # An access failure is not evidence that an already discovered
                # process disappeared.
                discovered.add(pid)
        return {pid for pid in discovered if pid > 0}

    @staticmethod
    def _window_score(
        hwnd: int,
        *,
        visible: bool,
        iconic: bool,
        area: int,
        preferred_hwnd: Optional[int],
    ) -> tuple[int, ...]:
        try:
            title = str(win32gui.GetWindowText(hwnd) or "").casefold()
        except Exception:
            title = ""
        try:
            class_name = str(win32gui.GetClassName(hwnd) or "").casefold()
        except Exception:
            class_name = ""
        try:
            owner = int(win32gui.GetWindow(hwnd, win32con.GW_OWNER) or 0)
        except Exception:
            owner = 0
        try:
            ex_style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE))
        except Exception:
            ex_style = 0

        looks_like_roblox = (
            "roblox" in title
            or "roblox" in class_name
            or class_name == "windowsclient"
        )
        is_tool_window = bool(ex_style & int(win32con.WS_EX_TOOLWINDOW))
        return (
            int(visible),
            int(looks_like_roblox),
            int(owner == 0),
            int(not is_tool_window),
            int(not iconic),
            int(area >= 160 * 120),
            int(area),
            int(hwnd == preferred_hwnd),
        )

    def find_best_window(
        self,
        target_pids: set[int],
        *,
        preferred_hwnd: Optional[int],
        include_hidden: bool,
    ) -> Optional[int]:
        targets = {int(pid) for pid in target_pids if int(pid) > 0}
        if not targets:
            return None

        candidates: list[tuple[tuple[int, ...], int]] = []

        def callback(hwnd: int, _extra: object) -> None:
            try:
                if not win32gui.IsWindow(hwnd):
                    return
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if int(pid) not in targets:
                    return
                visible = bool(win32gui.IsWindowVisible(hwnd))
                if not visible and not include_hidden:
                    return
                iconic = bool(win32gui.IsIconic(hwnd))
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width = max(0, int(right) - int(left))
                height = max(0, int(bottom) - int(top))
                area = width * height
                if area <= 0:
                    return
                score = self._window_score(
                    int(hwnd),
                    visible=visible,
                    iconic=iconic,
                    area=area,
                    preferred_hwnd=preferred_hwnd,
                )
                candidates.append((score, int(hwnd)))
            except Exception:
                return

        try:
            win32gui.EnumWindows(callback, None)
        except Exception:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate[0])[1]

    def is_target_window(self, hwnd: int, target_pids: set[int]) -> bool:
        try:
            if not win32gui.IsWindow(int(hwnd)):
                return False
            _, pid = win32process.GetWindowThreadProcessId(int(hwnd))
            return int(pid) in target_pids
        except Exception:
            return False

    def get_rect(self, hwnd: int) -> Rect:
        return tuple(int(value) for value in win32gui.GetWindowRect(int(hwnd)))  # type: ignore[return-value]

    def prepare_window(self, hwnd: int) -> bool:
        """Return True when a state transition was requested."""

        hwnd = int(hwnd)
        changed = False
        try:
            if win32gui.IsIconic(hwnd) or win32gui.IsZoomed(hwnd):
                win32gui.ShowWindow(hwnd, int(win32con.SW_RESTORE))
                changed = True
            elif not win32gui.IsWindowVisible(hwnd):
                show_without_activation = int(
                    getattr(win32con, "SW_SHOWNOACTIVATE", win32con.SW_SHOW)
                )
                win32gui.ShowWindow(hwnd, show_without_activation)
                changed = True
        except Exception:
            # Geometry calls may still work, so a failed state query/restore is
            # not by itself terminal.
            return changed
        return changed

    @staticmethod
    def _is_hung(hwnd: int) -> bool:
        try:
            return bool(ctypes.windll.user32.IsHungAppWindow(int(hwnd)))
        except Exception:
            return False

    def set_geometry(self, hwnd: int, geometry: WindowGeometry) -> None:
        flags = int(win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        # Avoid permanently wedging the worker if the game is not pumping its
        # message queue.  The state machine will verify this asynchronous call.
        if self._is_hung(hwnd):
            flags |= int(getattr(win32con, "SWP_ASYNCWINDOWPOS", 0))
        win32gui.SetWindowPos(
            int(hwnd),
            0,
            int(geometry.x),
            int(geometry.y),
            int(geometry.width),
            int(geometry.height),
            flags,
        )

    def move_geometry(self, hwnd: int, geometry: WindowGeometry) -> None:
        # MoveWindow exercises a different User32 path and is important when
        # SetWindowPos returns successfully but the target silently ignores it.
        if self._is_hung(hwnd):
            return
        win32gui.MoveWindow(
            int(hwnd),
            int(geometry.x),
            int(geometry.y),
            int(geometry.width),
            int(geometry.height),
            True,
        )


class RobloxWindowGeometryFixer:
    """Apply and verify a geometry until the Roblox window has settled."""

    def __init__(
        self,
        backend: Optional[WindowBackend] = None,
        *,
        timeout_seconds: float = 35.0,
        hidden_window_delay_seconds: float = 8.0,
        stable_seconds: float = 0.75,
        guard_seconds: float = 8.0,
        poll_seconds: float = 0.15,
        apply_settle_seconds: float = 0.10,
        tolerance: int = 2,
        max_attempts: int = 30,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.backend = backend or Win32WindowBackend()
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.hidden_window_delay_seconds = max(0.0, float(hidden_window_delay_seconds))
        self.stable_seconds = max(0.0, float(stable_seconds))
        self.guard_seconds = max(self.stable_seconds, float(guard_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.apply_settle_seconds = max(0.0, float(apply_settle_seconds))
        self.tolerance = max(0, int(tolerance))
        self.max_attempts = max(1, int(max_attempts))
        self.clock = clock
        self.sleep = sleep

    def _pause(self, cancel_event: object, duration: float) -> bool:
        """Pause, returning False if cancellation was requested."""

        wait = getattr(cancel_event, "wait", None)
        if callable(wait):
            return not bool(wait(max(0.0, duration)))
        self.sleep(max(0.0, duration))
        is_set = getattr(cancel_event, "is_set", None)
        return not (callable(is_set) and bool(is_set()))

    @staticmethod
    def _cancelled(cancel_event: object) -> bool:
        is_set = getattr(cancel_event, "is_set", None)
        return bool(callable(is_set) and is_set())

    def fix(
        self,
        root_pid: int,
        geometry: WindowGeometry,
        *,
        cancel_event: object = None,
    ) -> WindowFixResult:
        if not geometry.is_valid:
            return WindowFixResult(False, "invalid_geometry", 0)

        start = self.clock()
        deadline = start + self.timeout_seconds
        known_pids = {int(root_pid)}
        active_hwnd: Optional[int] = None
        actual_rect: Optional[Rect] = None
        attempts = 0
        saw_window = False
        correct_since: Optional[float] = None
        guarded_since: Optional[float] = None
        next_apply_at = start
        backoff = (0.10, 0.15, 0.25, 0.40, 0.65, 1.0, 1.5)

        while self.clock() < deadline:
            if self._cancelled(cancel_event):
                return WindowFixResult(False, "cancelled", attempts, active_hwnd, actual_rect)

            now = self.clock()
            try:
                known_pids = self.backend.discover_related_pids(int(root_pid), known_pids)
            except Exception:
                # Retain the known PID set through a transient process-query error.
                pass

            try:
                candidate = self.backend.find_best_window(
                    known_pids,
                    preferred_hwnd=active_hwnd,
                    include_hidden=(now - start) >= self.hidden_window_delay_seconds,
                )
            except Exception:
                candidate = None

            if candidate is None:
                active_hwnd = None
                correct_since = None
                guarded_since = None
                if not self._pause(cancel_event, self.poll_seconds):
                    return WindowFixResult(False, "cancelled", attempts, None, actual_rect)
                continue

            candidate = int(candidate)
            saw_window = True
            if candidate != active_hwnd:
                active_hwnd = candidate
                correct_since = None
                guarded_since = None
                next_apply_at = now

            try:
                is_target = self.backend.is_target_window(active_hwnd, known_pids)
            except Exception:
                is_target = False
            if not is_target:
                active_hwnd = None
                correct_since = None
                guarded_since = None
                if not self._pause(cancel_event, self.poll_seconds):
                    return WindowFixResult(False, "cancelled", attempts, None, actual_rect)
                continue

            try:
                state_changed = bool(self.backend.prepare_window(active_hwnd))
            except Exception:
                state_changed = False
            if state_changed:
                correct_since = None
                guarded_since = None
                if not self._pause(cancel_event, self.poll_seconds):
                    return WindowFixResult(False, "cancelled", attempts, active_hwnd, actual_rect)
                continue

            try:
                actual_rect = self.backend.get_rect(active_hwnd)
            except Exception:
                active_hwnd = None
                correct_since = None
                guarded_since = None
                if not self._pause(cancel_event, self.poll_seconds):
                    return WindowFixResult(False, "cancelled", attempts, None, actual_rect)
                continue

            if geometry.matches(actual_rect, self.tolerance):
                if correct_since is None:
                    correct_since = now
                if guarded_since is None:
                    guarded_since = now
                if (
                    now - correct_since >= self.stable_seconds
                    and now - guarded_since >= self.guard_seconds
                ):
                    return WindowFixResult(True, "stable", attempts, active_hwnd, actual_rect)
                if not self._pause(cancel_event, self.poll_seconds):
                    return WindowFixResult(False, "cancelled", attempts, active_hwnd, actual_rect)
                continue

            # Any drift restarts the guard period.  This catches Roblox applying
            # its own saved placement a few seconds after our first successful set.
            correct_since = None
            guarded_since = None

            if attempts >= self.max_attempts:
                return WindowFixResult(
                    False,
                    "attempts_exhausted",
                    attempts,
                    active_hwnd,
                    actual_rect,
                )
            if now < next_apply_at:
                if not self._pause(
                    cancel_event,
                    min(self.poll_seconds, max(0.01, next_apply_at - now)),
                ):
                    return WindowFixResult(False, "cancelled", attempts, active_hwnd, actual_rect)
                continue

            attempts += 1
            primary_error: Optional[Exception] = None
            try:
                self.backend.set_geometry(active_hwnd, geometry)
            except Exception as exc:
                primary_error = exc

            if not self._pause(cancel_event, self.apply_settle_seconds):
                return WindowFixResult(False, "cancelled", attempts, active_hwnd, actual_rect)

            try:
                still_target = self.backend.is_target_window(active_hwnd, known_pids)
                actual_rect = self.backend.get_rect(active_hwnd) if still_target else None
            except Exception:
                still_target = False
                actual_rect = None

            # Crucially, use the fallback on an unchanged rectangle as well as on
            # an exception.  User32 APIs can report success while startup code
            # immediately rejects or overwrites the requested placement.
            if (
                still_target
                and (primary_error is not None or actual_rect is None or not geometry.matches(actual_rect, self.tolerance))
            ):
                try:
                    self.backend.move_geometry(active_hwnd, geometry)
                except Exception:
                    pass
                if not self._pause(cancel_event, self.apply_settle_seconds):
                    return WindowFixResult(False, "cancelled", attempts, active_hwnd, actual_rect)

            delay_index = min(attempts - 1, len(backoff) - 1)
            next_apply_at = self.clock() + backoff[delay_index]

        reason = "timeout" if saw_window else "no_window"
        return WindowFixResult(False, reason, attempts, active_hwnd, actual_rect)
