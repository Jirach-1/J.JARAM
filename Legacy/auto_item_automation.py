"""
Auto Item automation (multi-user) for JARAM.

This module is intentionally based on the click/type flow from `lib/macro_logic.py::use_item`,
but it does NOT import or call that function.
"""

from __future__ import annotations

import ctypes
import atexit
import json
import os
import random
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from ctypes import wintypes

try:
    import autoit  # type: ignore
except Exception:  # pragma: no cover
    autoit = None  # type: ignore

import win32api
import win32con
import win32gui
import win32process

try:
    from PIL import ImageGrab
except Exception:  # pragma: no cover
    ImageGrab = None  # type: ignore


# ---------------------------
# Types / helpers
# ---------------------------


@dataclass(frozen=True)
class RelPoint:
    """Point stored as percentage of the Roblox window client area."""

    x: float  # 0..1
    y: float  # 0..1


@dataclass(frozen=True)
class ConditionalClick:
    enabled: bool
    point: Optional[RelPoint]
    color_hex: str
    tolerance: int = 0


@dataclass(frozen=True)
class ItemRule:
    name: str
    amount: int
    cooldown_s: float
    allowed_biomes: Tuple[str, ...]  # uppercase names; empty => any
    enabled: bool = True
    alert_enabled: bool = False
    alert_lead_s: float = 15.0
    alert_webhook: str = ""
    alert_message: str = ""


APP_FOOTER = "J.JARAM JX 2x27"


_AUTO_ITEM_ALERT_UNLOCKED = False
_ACCESS_ENV_NAME = "".join(chr(c) for c in (74, 65, 82, 65, 77, 95, 85, 78, 76, 79, 67, 75))
_ACCESS_MARKER_NAME = "".join(chr(c) for c in (74, 65, 82, 65, 77, 46, 98, 105, 117))


def _auto_item_alerts_unlocked() -> bool:
    """Gate Auto-Item webhook alerts behind the shared access check."""
    global _AUTO_ITEM_ALERT_UNLOCKED
    if _AUTO_ITEM_ALERT_UNLOCKED:
        return True

    try:
        if os.environ.get(_ACCESS_ENV_NAME, "").strip() == "1":
            _AUTO_ITEM_ALERT_UNLOCKED = True
            return True
    except Exception:
        pass

    try:
        candidates = [Path(_ACCESS_MARKER_NAME)]
        try:
            candidates.append(Path(__file__).resolve().with_name(_ACCESS_MARKER_NAME))
        except Exception:
            pass
        try:
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / _ACCESS_MARKER_NAME)
        except Exception:
            pass

        for p in candidates:
            try:
                if p.exists():
                    _AUTO_ITEM_ALERT_UNLOCKED = True
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _post_webhook(url: str, payload: dict) -> None:
    if not url:
        return
    try:
        import requests

        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def _build_item_alert_embed(
    *,
    item_name: str,
    amount: int,
    username: str,
    server_label: str,
    ps_link: str,
    use_at_epoch: float,
) -> dict:
    import datetime as _dt

    unix = int(use_at_epoch)
    iso = _dt.datetime.fromtimestamp(unix, tz=_dt.timezone.utc).isoformat()

    ts_full = f"<t:{unix}:D>  \u2022  <t:{unix}:T>"
    ts_rel = f"<t:{unix}:R>"

    server_label = str(server_label or "").strip() or "N/A"
    ps_link = str(ps_link or "").strip()
    if ps_link:
        ps_line = f"**Private Server:** [Private Server Link]({ps_link})"
    else:
        ps_line = f"**Private Server:** `{server_label}`"

    uname = str(username or "").strip() or "Unknown"
    item_disp = str(item_name or "").strip() or "Unknown Item"
    amt = max(1, int(amount or 1))

    description = (
        f"**Account:** `{uname}`\n"
        f"**Item:** `{item_disp} x{amt}`\n"
        f"**Time:** {ts_full} ({ts_rel})\n"
        f"{ps_line}"
    )

    return {
        "title": "Auto-Item Alert",
        "description": description,
        "color": 0xF59E0B,
        "timestamp": iso,
        "footer": {"text": f"{APP_FOOTER}  \u2022  {server_label}"},
    }


def _normalize_user_id_list(raw: object) -> Optional[List[str]]:
    """
    Normalize a per-item users filter to a list of string UIDs.

    Returns:
      - None: no filter (all users)
      - []  : explicit empty selection (no users)
      - [uids...]: explicit allowlist
    """
    if raw is None:
        return None

    seq: Iterable
    if isinstance(raw, (list, tuple, set)):
        seq = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        # Support configs that persisted the list as a JSON string or as "a,b,c".
        try:
            parsed = json.loads(s)
        except Exception:
            parts = [p.strip() for p in s.replace("\r", "\n").replace("\n", ",").split(",")]
            seq = [p for p in parts if p]
        else:
            if isinstance(parsed, (list, tuple, set)):
                seq = parsed
            else:
                seq = [parsed]
    else:
        # Be tolerant of Qt container/variant types.
        try:
            if isinstance(raw, dict):
                return None
            seq = list(raw)  # type: ignore[arg-type]
        except Exception:
            return None

    cleaned = [str(u).strip() for u in seq if str(u).strip()]
    return cleaned


def _clamp01(v: float) -> float:
    try:
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return float(v)
    except Exception:
        return 0.0


def _hex_to_rgb(color_hex: str) -> Tuple[int, int, int]:
    s = (color_hex or "").strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        return (0, 0, 0)
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (r, g, b)
    except Exception:
        return (0, 0, 0)


def _color_close(a: Tuple[int, int, int], b: Tuple[int, int, int], tol: int) -> bool:
    tol = int(tol or 0)
    return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))


def _client_origin_and_size(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    try:
        left, top = win32gui.ClientToScreen(hwnd, (0, 0))
        _l, _t, right, bottom = win32gui.GetClientRect(hwnd)
        width = int(right - _l)
        height = int(bottom - _t)
        if width <= 0 or height <= 0:
            return None
        return int(left), int(top), width, height
    except Exception:
        return None


def _abs_from_rel(hwnd: int, p: RelPoint) -> Optional[Tuple[int, int]]:
    base = _client_origin_and_size(hwnd)
    if not base:
        return None
    left, top, width, height = base
    x = left + int(_clamp01(p.x) * width)
    y = top + int(_clamp01(p.y) * height)
    return x, y


def _mouse_move_instant(x: int, y: int) -> None:
    x = int(x)
    y = int(y)

    if autoit is None:
        return

    try:
        _note_program_mouse_target(int(x), int(y))
        autoit.mouse_move(int(x), int(y), speed=0)
    except Exception:
        pass


def _mouse_move_natural(x: int, y: int) -> None:
    """
    Legacy name used by earlier versions. Auto-Item now uses instant movement + a tiny pre-click wiggle.
    """
    _mouse_move_instant(x, y)


# ---------------------------
# User mouse-move blocker
# ---------------------------

_BLOCK_USER_MOUSE_MOVE_ENABLED: bool = False
_ALLOWED_MOVE_X: int = 0
_ALLOWED_MOVE_Y: int = 0
_ALLOWED_MOVE_UNTIL: float = 0.0
_ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)
_LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
_WPARAM = getattr(wintypes, "WPARAM", ctypes.c_size_t)
_LPARAM = getattr(wintypes, "LPARAM", ctypes.c_ssize_t)


def _note_program_mouse_target(x: int, y: int, *, hold_s: float = 0.25) -> None:
    """
    Tell the low-level mouse hook which cursor positions are expected from Auto-Item.

    This makes the "block user mouse movement" feature compatible with input methods that do not
    mark injected mouse events with LLMHF_INJECTED.
    """
    global _ALLOWED_MOVE_X, _ALLOWED_MOVE_Y, _ALLOWED_MOVE_UNTIL
    try:
        _ALLOWED_MOVE_X = int(x)
        _ALLOWED_MOVE_Y = int(y)
        _ALLOWED_MOVE_UNTIL = float(time.monotonic()) + float(max(0.0, hold_s))
    except Exception:
        pass


class _LL_POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _LL_POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _UserMouseMoveBlocker:
    """
    Low-level mouse hook (WH_MOUSE_LL) that blocks physical mouse movement while Auto-Item runs.

    - Blocks only movement (WM_MOUSEMOVE / WM_NCMOUSEMOVE).
    - Allows injected movement (LLMHF_INJECTED) and program-expected coordinates (see _note_program_mouse_target).
    """

    WH_MOUSE_LL = 14
    WM_MOUSEMOVE = 0x0200
    WM_NCMOUSEMOVE = 0x00A0
    WM_QUIT = 0x0012
    LLMHF_INJECTED = 0x00000001
    LLMHF_LOWER_IL_INJECTED = 0x00000002

    LowLevelMouseProc = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, _WPARAM, _LPARAM)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refcount = 0
        self._warned = False
        self._failed = False
        self._last_error: int = 0

        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._hook = None
        self._ready = threading.Event()

        self._user32 = None
        self._kernel32 = None

        self._proc = self.LowLevelMouseProc(self._hook_proc)

    def acquire(self, *, log_fn: Optional[Callable[[str], None]] = None) -> bool:
        global _BLOCK_USER_MOUSE_MOVE_ENABLED
        with self._lock:
            if self._failed:
                if log_fn and not self._warned:
                    self._warned = True
                    msg = "[Auto-Item] Mouse-move block is unavailable on this system/build."
                    if int(self._last_error or 0) != 0:
                        msg += f" (winerr={int(self._last_error)})"
                    log_fn(msg)
                return False

            ok = self._ensure_hook_installed()
            if not ok:
                self._failed = True
                if log_fn and not self._warned:
                    self._warned = True
                    msg = "[Auto-Item] Failed to enable mouse-move block (hook install failed)."
                    if int(self._last_error or 0) != 0:
                        msg += f" (winerr={int(self._last_error)})"
                    log_fn(msg)
                return False

            self._refcount += 1
            _BLOCK_USER_MOUSE_MOVE_ENABLED = True
            return True

    def release(self) -> None:
        global _BLOCK_USER_MOUSE_MOVE_ENABLED
        should_shutdown = False
        with self._lock:
            if self._refcount <= 0:
                _BLOCK_USER_MOUSE_MOVE_ENABLED = False
                return
            self._refcount -= 1
            if self._refcount <= 0:
                _BLOCK_USER_MOUSE_MOVE_ENABLED = False
                should_shutdown = True
        if should_shutdown:
            self.shutdown(timeout_s=1.0)

    def shutdown(self, timeout_s: float = 2.0) -> None:
        global _BLOCK_USER_MOUSE_MOVE_ENABLED
        thread: Optional[threading.Thread] = None
        with self._lock:
            self._refcount = 0
            _BLOCK_USER_MOUSE_MOVE_ENABLED = False
            thread = self._thread
            thread_id = int(self._thread_id or 0)
            user32 = self._user32
            if user32 is not None and thread_id:
                try:
                    user32.PostThreadMessageW.argtypes = [
                        wintypes.DWORD,
                        wintypes.UINT,
                        _WPARAM,
                        _LPARAM,
                    ]
                    user32.PostThreadMessageW.restype = wintypes.BOOL
                except Exception:
                    pass
                try:
                    user32.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0)
                except Exception:
                    pass

        if thread and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=max(0.0, float(timeout_s)))
            except Exception:
                pass

        with self._lock:
            if thread and thread.is_alive() and self._hook and self._user32 is not None:
                try:
                    self._user32.UnhookWindowsHookEx(self._hook)
                except Exception:
                    pass
                self._hook = None
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _ensure_hook_installed(self) -> bool:
        if self._hook:
            return True

        if self._thread and self._thread.is_alive():
            # Existing thread is alive but hook isn't ready yet.
            self._ready.wait(timeout=1.0)
            return bool(self._hook)

        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="AutoItemMouseMoveBlocker", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)
        return bool(self._hook)

    def _thread_main(self) -> None:
        hook = None
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            # Prototypes (avoid 32-bit truncation of handles on 64-bit Python).
            try:
                self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
                self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
                self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE

                self._user32.SetWindowsHookExW.argtypes = [ctypes.c_int, self.LowLevelMouseProc, wintypes.HINSTANCE, wintypes.DWORD]
                self._user32.SetWindowsHookExW.restype = ctypes.c_void_p
                self._user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
                self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
                self._user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, _WPARAM, _LPARAM]
                self._user32.CallNextHookEx.restype = _LRESULT

                self._user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
                self._user32.PeekMessageW.restype = wintypes.BOOL
                self._user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
                self._user32.GetMessageW.restype = ctypes.c_int
                self._user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
                self._user32.TranslateMessage.restype = wintypes.BOOL
                self._user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
                self._user32.DispatchMessageW.restype = _LRESULT
            except Exception:
                pass

            try:
                self._thread_id = int(self._kernel32.GetCurrentThreadId())
            except Exception:
                self._thread_id = 0

            # Ensure the thread has a message queue before installing the hook.
            try:
                msg = wintypes.MSG()
                self._user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 0)
            except Exception:
                pass

            try:
                hmod = self._kernel32.GetModuleHandleW(None)
            except Exception:
                hmod = 0

            try:
                ctypes.set_last_error(0)
            except Exception:
                pass
            hook = self._user32.SetWindowsHookExW(int(self.WH_MOUSE_LL), self._proc, hmod, 0)
            self._hook = hook
            if not hook:
                try:
                    self._last_error = int(ctypes.get_last_error() or 0)
                except Exception:
                    self._last_error = 0
            else:
                self._last_error = 0
        except Exception:
            hook = None
            self._hook = None
            self._thread_id = 0
        finally:
            self._ready.set()

        if not hook:
            return

        # Basic message loop keeps the hook alive.
        try:
            msg = wintypes.MSG()
            while True:
                res = self._user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if res == 0 or res == -1:
                    break
                try:
                    self._user32.TranslateMessage(ctypes.byref(msg))
                    self._user32.DispatchMessageW(ctypes.byref(msg))
                except Exception:
                    pass
        finally:
            try:
                if self._hook:
                    self._user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None

    def _hook_proc(self, nCode: int, wParam: int, lParam: int):
        user32 = self._user32
        if user32 is None:
            try:
                user32 = ctypes.windll.user32
            except Exception:
                user32 = None

        if nCode < 0 or not _BLOCK_USER_MOUSE_MOVE_ENABLED:
            if user32 is not None:
                try:
                    return user32.CallNextHookEx(0, nCode, wParam, lParam)
                except Exception:
                    return 0
            return 0

        msg = int(wParam)
        if msg not in (self.WM_MOUSEMOVE, self.WM_NCMOUSEMOVE):
            if user32 is not None:
                try:
                    return user32.CallNextHookEx(0, nCode, wParam, lParam)
                except Exception:
                    return 0
            return 0

        try:
            info = ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            flags = int(info.flags)
            if flags & (self.LLMHF_INJECTED | self.LLMHF_LOWER_IL_INJECTED):
                # Injected movement (Auto-Item) -> allow.
                if user32 is not None:
                    return user32.CallNextHookEx(0, nCode, wParam, lParam)
                return 0

            # If not marked as injected, still allow movement that matches the program's expected targets.
            now = float(time.monotonic())
            if now <= float(_ALLOWED_MOVE_UNTIL):
                dx = abs(int(info.pt.x) - int(_ALLOWED_MOVE_X))
                dy = abs(int(info.pt.y) - int(_ALLOWED_MOVE_Y))
                if dx <= 3 and dy <= 3:
                    if user32 is not None:
                        return user32.CallNextHookEx(0, nCode, wParam, lParam)
                    return 0
        except Exception:
            # If hook parsing fails, fail-open (don't brick the mouse).
            if user32 is not None:
                try:
                    return user32.CallNextHookEx(0, nCode, wParam, lParam)
                except Exception:
                    return 0
            return 0

        # Block physical mouse movement.
        return 1


_USER_MOUSE_BLOCKER: Optional[_UserMouseMoveBlocker] = None
_USER_MOUSE_BLOCKER_LOCK = threading.Lock()


def shutdown_user_mouse_blocker(timeout_s: float = 2.0) -> None:
    blocker: Optional[_UserMouseMoveBlocker]
    with _USER_MOUSE_BLOCKER_LOCK:
        blocker = _USER_MOUSE_BLOCKER
    if blocker is not None:
        try:
            blocker.shutdown(timeout_s=timeout_s)
        except Exception:
            pass


atexit.register(shutdown_user_mouse_blocker)


@contextmanager
def _block_user_mouse_movement_during_actions(
    enabled: bool,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
    notify_fn: Optional[Callable[[bool], None]] = None,
):
    """
    Context manager: when enabled, blocks physical mouse movement while inside the block.
    """
    if not bool(enabled):
        yield
        return

    global _USER_MOUSE_BLOCKER
    with _USER_MOUSE_BLOCKER_LOCK:
        if _USER_MOUSE_BLOCKER is None:
            _USER_MOUSE_BLOCKER = _UserMouseMoveBlocker()
        blocker = _USER_MOUSE_BLOCKER

    acquired = False
    try:
        acquired = bool(blocker.acquire(log_fn=log_fn))
        if acquired:
            if notify_fn is not None:
                try:
                    notify_fn(True)
                except Exception:
                    pass
            else:
                _auto_item_mouse_block_tooltip(True)
        yield
    finally:
        if acquired:
            if notify_fn is not None:
                try:
                    notify_fn(False)
                except Exception:
                    pass
            else:
                _auto_item_mouse_block_tooltip(False)
        if acquired:
            try:
                blocker.release()
            except Exception:
                pass


def _mouse_focus_wiggle(hwnd: int, x: int, y: int) -> None:
    """
    Tiny pre-click wiggle to help the game/window "pick up" the cursor and focus reliably.
    Always ends at (x, y) so the click position is not misaligned.
    """
    if autoit is None:
        return

    x = int(x)
    y = int(y)

    # Keep wiggle inside the client area when possible.
    bounds = _client_origin_and_size(hwnd) if hwnd else None
    if bounds:
        left, top, width, height = bounds
        min_x = int(left)
        min_y = int(top)
        max_x = int(left + max(1, int(width)) - 1)
        max_y = int(top + max(1, int(height)) - 1)

        def _clamp(px: int, py: int) -> Tuple[int, int]:
            return (int(max(min_x, min(max_x, int(px)))), int(max(min_y, min(max_y, int(py)))))

        x, y = _clamp(x, y)
    else:

        def _clamp(px: int, py: int) -> Tuple[int, int]:
            return int(px), int(py)

    # Tiny + brief: 2-step wiggle around the target, then snap back to target.
    dx, dy = 2, 1
    seq = [(x + dx, y + dy), (x - dx, y - dy), (x, y)]
    try:
        for i, (px, py) in enumerate(seq):
            cx, cy = _clamp(px, py)
            _note_program_mouse_target(int(cx), int(cy))
            autoit.mouse_move(int(cx), int(cy), speed=0)
            # Yield without adding real delay.
            if i < len(seq) - 1:
                time.sleep(0)
    except Exception:
        # Ensure we still end at the requested position below.
        pass

    try:
        _note_program_mouse_target(int(x), int(y))
        autoit.mouse_move(int(x), int(y), speed=0)
    except Exception:
        pass


def _mouse_left_click(hwnd: int, x: int, y: int) -> None:
    x = int(x)
    y = int(y)

    if autoit is None:
        return

    try:
        if hwnd and win32gui.IsWindow(hwnd) and win32gui.GetForegroundWindow() != hwnd:
            _bring_window_foreground(hwnd)
            time.sleep(0.01)
    except Exception:
        # Best-effort; clicks can still work even if focus is blocked by OS rules.
        pass

    def _force_left_up() -> None:
        try:
            # Fail-safe: if AutoIt errors between mouse_down/mouse_up, never leave LMB logically held.
            ctypes.windll.user32.mouse_event(int(win32con.MOUSEEVENTF_LEFTUP), 0, 0, 0, 0)
        except Exception:
            pass

    # Move like a person and add small dwell time before pressing.
    _mouse_move_instant(x, y)
    _mouse_focus_wiggle(hwnd, x, y)

    try:
        autoit.mouse_down("left")
        # Keep a tiny hold so games/UI consistently register the click.
        time.sleep(0.01)
    except Exception:
        # Ensure we still attempt to release in the finally block.
        pass
    finally:
        try:
            autoit.mouse_up("left")
        except Exception:
            pass
        _force_left_up()


def _send_ctrl_a() -> None:
    if autoit is not None:
        try:
            autoit.send("^a")
            return
        except Exception:
            pass


def _send_typed_text(text: str) -> None:
    """
    Type text directly via AutoIt (no clipboard).

    Used for numeric fields (like item amount) so we don't depend on clipboard timing.
    """
    if autoit is None:
        return
    s = str(text or "")
    try:
        autoit.send(s, mode=1)
        return
    except Exception:
        pass
    try:
        autoit.send(s)
    except Exception:
        pass


def _auto_item_mouse_block_tooltip(show: bool) -> None:
    """
    On-screen tooltip while physical mouse movement is blocked.
    """
    if autoit is None:
        return
    try:
        if not show:
            autoit.tooltip("")
            return

        try:
            cx, cy = win32api.GetCursorPos()
            x = int(cx) + 16
            y = int(cy) + 16
        except Exception:
            x = 10
            y = 10
        autoit.tooltip("User mouse movement is disabled during Auto-Item.", int(x), int(y))
    except Exception:
        pass


def _send_unicode_text(text: str) -> None:
    """
    Paste text via AutoIt (clipboard + Ctrl+V).
    """
    if autoit is None:
        return

    # Global clipboard guard: Auto-Item uses the system clipboard to paste into Roblox.
    # If we restore the clipboard too quickly, Roblox may read the old value and paste it.
    # This lock ensures only one clipboard paste is in-flight at a time and we hold the value
    # long enough for the target to consume it.

    def _clip_get_safe() -> Optional[str]:
        try:
            return autoit.clip_get()
        except Exception:
            return None

    def _clip_put_wait(value: str, *, timeout_s: float = 0.25) -> bool:
        try:
            autoit.clip_put(value)
        except Exception:
            return False

        deadline = time.time() + float(timeout_s or 0.0)
        while time.time() < deadline:
            cur = _clip_get_safe()
            if cur == value:
                return True
            time.sleep(0.01)
        # One last check
        return _clip_get_safe() == value

    s = str(text or "")

    with _AUTO_ITEM_CLIPBOARD_LOCK:
        prev = _clip_get_safe()
        pasted = False
        try:
            # Set clipboard and wait for the value to actually stick.
            if not _clip_put_wait(s, timeout_s=0.35):
                raise RuntimeError("clipboard put did not stick")

            # Small yield so the clipboard update lands before Ctrl+V.
            time.sleep(0.02)
            autoit.send("^v")
            pasted = True

            # Hold the clipboard long enough for Roblox to read it under load.
            time.sleep(0.12)
        except Exception:
            try:
                # Fallback: type raw text if clipboard is unavailable/unreliable.
                autoit.send(s, mode=1)
            except Exception:
                pass
        finally:
            # Only restore clipboard if we had something to restore to.
            if prev is not None:
                # If we didn't paste via clipboard, don't thrash the clipboard unnecessarily.
                if pasted or prev != s:
                    try:
                        autoit.clip_put(prev)
                    except Exception:
                        pass


# Global clipboard lock shared by all Auto-Item paste calls in this process.
_AUTO_ITEM_CLIPBOARD_LOCK = threading.Lock()


def _bring_window_foreground(hwnd: int) -> bool:
    """
    Best-effort foreground activation for the given window.
    """
    try:
        if not win32gui.IsWindow(hwnd):
            return False

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        def _toggle_topmost() -> None:
            try:
                flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                was_topmost = _is_window_topmost(hwnd)
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
                if not was_topmost:
                    try:
                        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                    except Exception:
                        pass
            except Exception:
                pass

        current_thread_id: Optional[int] = None
        window_thread_id: Optional[int] = None
        try:
            current_thread_id = win32api.GetCurrentThreadId()
            window_thread_id = win32process.GetWindowThreadProcessId(hwnd)[0]
            ctypes.windll.user32.AttachThreadInput(current_thread_id, window_thread_id, True)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
        finally:
            if current_thread_id is not None and window_thread_id is not None:
                try:
                    ctypes.windll.user32.AttachThreadInput(current_thread_id, window_thread_id, False)
                except Exception:
                    pass

        # If focus didn't stick, toggle TOPMOST as a fallback and try again.
        try:
            if win32gui.GetForegroundWindow() != hwnd:
                _toggle_topmost()
                try:
                    current_thread_id = win32api.GetCurrentThreadId()
                    window_thread_id = win32process.GetWindowThreadProcessId(hwnd)[0]
                    ctypes.windll.user32.AttachThreadInput(current_thread_id, window_thread_id, True)
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                finally:
                    try:
                        if current_thread_id is not None and window_thread_id is not None:
                            ctypes.windll.user32.AttachThreadInput(current_thread_id, window_thread_id, False)
                    except Exception:
                        pass
        except Exception:
            pass

        return True
    except Exception:
        return False


def _is_window_topmost(hwnd: int) -> bool:
    try:
        exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        return bool(int(exstyle) & int(win32con.WS_EX_TOPMOST))
    except Exception:
        return False


def _set_window_topmost(hwnd: int, topmost: bool) -> None:
    try:
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
        insert_after = win32con.HWND_TOPMOST if bool(topmost) else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags)
    except Exception:
        pass


@contextmanager
def _window_topmost_during(hwnd: int):
    original = _is_window_topmost(hwnd)
    _set_window_topmost(hwnd, True)
    try:
        yield
    finally:
        _set_window_topmost(hwnd, original)


def _screen_pixel_rgb(x: int, y: int) -> Optional[Tuple[int, int, int]]:
    if ImageGrab is None:
        return None
    try:
        img = ImageGrab.grab(bbox=(int(x), int(y), int(x) + 1, int(y) + 1))
        return tuple(img.getpixel((0, 0))[:3])  # type: ignore[return-value]
    except Exception:
        return None


# ---------------------------
# Public engine
# ---------------------------


class AutoItemEngine:
    """
    Background worker that applies item usage to multiple user windows.

    The host is expected to:
      - Call `update_config()` whenever UI settings change.
      - Provide `pid_provider(uid)->Optional[int]` to locate the window per user.
      - Provide `biome_provider(uid)->str` (may be empty/unknown).
      - Provide `in_menu_provider(uid)->Optional[bool]` (True in menu, False in-game, None unknown).
      - Provide `hwnd_provider(pid)->Optional[int]` to resolve PID -> HWND.
      - Optionally provide `username_provider/server_label_provider/ps_link_provider` for webhook embeds.
    """

    def __init__(
        self,
        *,
        pid_provider: Callable[[str], Optional[int]],
        hwnd_provider: Callable[[int], Optional[int]],
        biome_provider: Callable[[str], str],
        in_menu_provider: Optional[Callable[[str], Optional[bool]]] = None,
        username_provider: Optional[Callable[[str], str]] = None,
        server_label_provider: Optional[Callable[[str], str]] = None,
        ps_link_provider: Optional[Callable[[str], str]] = None,
        log: Callable[[str], None],
        mouse_block_notify: Optional[Callable[[bool], None]] = None,
        pause_antiafk: Optional[Callable[[], None]] = None,
        resume_antiafk: Optional[Callable[[], None]] = None,
        antiafk_overdue_within_provider: Optional[Callable[[float], bool]] = None,
        pre_action_hook: Optional[Callable[[str, int], float]] = None,
        post_action_hook: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._pid_provider = pid_provider
        self._hwnd_provider = hwnd_provider
        self._biome_provider = biome_provider
        self._in_menu_provider = in_menu_provider
        self._username_provider = username_provider
        self._server_label_provider = server_label_provider
        self._ps_link_provider = ps_link_provider
        self._log = log
        self._mouse_block_notify = mouse_block_notify
        self._pause_antiafk = pause_antiafk
        self._resume_antiafk = resume_antiafk
        self._antiafk_overdue_within_provider = antiafk_overdue_within_provider
        self._pre_action_hook = pre_action_hook
        self._post_action_hook = post_action_hook

        self._cfg_lock = threading.Lock()
        self._cfg: Dict = {"enabled": False}

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Prevent concurrent in-window automation (engine loop vs manual test).
        self._action_lock = threading.Lock()

        # per-user per-item-index cooldown expiry
        self._next_ready: Dict[str, Dict[int, float]] = {}
        # per-user per-item-index scheduled use time (for pre-use alerts)
        self._pending_use_at: Dict[str, Dict[int, float]] = {}
        self._pending_item_name: Dict[str, Dict[int, str]] = {}
        self._not_in_menu_since: Dict[str, float] = {}
        self._state_lock = threading.Lock()

        # Throttle "alert suppressed" logs when Anti-AFK is overdue.
        self._last_antiafk_overdue_alert_log_ts: float = 0.0

    def _username(self, uid: str) -> str:
        fn = self._username_provider
        if fn is None:
            return str(uid)
        try:
            return str(fn(str(uid)) or "").strip() or str(uid)
        except Exception:
            return str(uid)

    def _server_label(self, uid: str) -> str:
        fn = self._server_label_provider
        if fn is None:
            return ""
        try:
            return str(fn(str(uid)) or "").strip()
        except Exception:
            return ""

    def _ps_link(self, uid: str) -> str:
        fn = self._ps_link_provider
        if fn is None:
            return ""
        try:
            return str(fn(str(uid)) or "").strip()
        except Exception:
            return ""

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="AutoItemEngine", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=float(timeout_s))
        shutdown_user_mouse_blocker(timeout_s=1.0)

    def update_config(self, cfg: Dict) -> None:
        with self._cfg_lock:
            # Keep a shallow copy; values are primitives/lists/dicts.
            self._cfg = dict(cfg or {})

    def _cfg_snapshot(self) -> Dict:
        with self._cfg_lock:
            return dict(self._cfg or {})

    def _rules_from_cfg(self, cfg: Dict, uid: Optional[str] = None) -> List[Tuple[int, ItemRule]]:
        out: List[Tuple[int, ItemRule]] = []
        raw_items = cfg.get("items") or []
        if not isinstance(raw_items, list):
            raw_items = []

        uid_str = str(uid).strip() if uid is not None else None

        for idx, it in enumerate(raw_items):
            try:
                if not isinstance(it, dict):
                    continue

                # Optional per-item user filter.
                # Backward compatible: empty list means "all users" unless users_explicit=True.
                if uid_str is not None:
                    raw_users = it.get("users", None)
                    users_explicit = bool(it.get("users_explicit", False))
                    allowed_users = _normalize_user_id_list(raw_users)

                    if isinstance(allowed_users, list):
                        if allowed_users:
                            if uid_str not in allowed_users:
                                continue
                        else:
                            # Backward compatible: empty list means "all users" unless users_explicit=True.
                            if users_explicit:
                                continue

                name = str(it.get("name") or "").strip()
                if not name:
                    continue
                enabled = bool(it.get("enabled", True))
                amount = int(it.get("amount", 1))
                cooldown_s = float(it.get("cooldown", it.get("cooldown_s", 0)))
                biomes = it.get("biomes", it.get("allowed_biomes", [])) or []
                allowed = tuple(str(b).strip().upper() for b in biomes if str(b).strip())
                alert_enabled = bool(it.get("alert_enabled", False))
                alert_webhook = str(it.get("alert_webhook") or it.get("alert_webhook_url") or "").strip()
                alert_message = str(it.get("alert_message") or "")
                try:
                    alert_lead_s = float(it.get("alert_lead_s", 15.0) or 15.0)
                except Exception:
                    alert_lead_s = 15.0
                out.append(
                    (
                        int(idx),
                        ItemRule(
                            name=name,
                            amount=max(1, amount),
                            cooldown_s=max(0.0, cooldown_s),
                            allowed_biomes=allowed,
                            enabled=enabled,
                            alert_enabled=alert_enabled,
                            alert_lead_s=max(0.0, float(alert_lead_s)),
                            alert_webhook=alert_webhook,
                            alert_message=alert_message,
                        ),
                    )
                )
            except Exception:
                continue
        return out

    def _coords_from_cfg(self, cfg: Dict) -> Optional[Dict[str, RelPoint]]:
        coords = cfg.get("coords") or {}

        def _pt(key: str) -> Optional[RelPoint]:
            raw = coords.get(key) or {}
            if not isinstance(raw, dict):
                return None
            try:
                return RelPoint(float(raw.get("x", 0.0)), float(raw.get("y", 0.0)))
            except Exception:
                return None

        required = {
            "inv_button": _pt("inv_button"),
            "items_tab": _pt("items_tab"),
            "search_box": _pt("search_box"),
            "query_pos": _pt("query_pos"),
            "amount_box": _pt("amount_box"),
            "use_button": _pt("use_button"),
            "close_button": _pt("close_button"),
        }
        if any(v is None for v in required.values()):
            return None
        return required  # type: ignore[return-value]

    def _conditional_from_cfg(self, cfg: Dict) -> ConditionalClick:
        c = (cfg.get("coords") or {}).get("conditional") or {}
        enabled = bool(c.get("enabled", False))
        pt_raw = c.get("point") or {}
        point = None
        try:
            if isinstance(pt_raw, dict):
                point = RelPoint(float(pt_raw.get("x", 0.0)), float(pt_raw.get("y", 0.0)))
        except Exception:
            point = None
        color_hex = str(c.get("color", c.get("color_hex", "#FFFFFF")) or "#FFFFFF").strip()
        tol = int(c.get("tolerance", 0) or 0)
        return ConditionalClick(enabled=enabled, point=point, color_hex=color_hex, tolerance=tol)

    def _use_items_on_window(
        self,
        hwnd: int,
        coords: Dict[str, RelPoint],
        rules_to_use: Sequence[ItemRule],
        *,
        click_delay: float,
        conditional: ConditionalClick,
        block_user_mouse_move: bool = False,
    ) -> None:
        with _window_topmost_during(hwnd), _block_user_mouse_movement_during_actions(
            bool(block_user_mouse_move),
            log_fn=self._log,
            notify_fn=getattr(self, "_mouse_block_notify", None),
        ):
            _bring_window_foreground(hwnd)
            _set_window_topmost(hwnd, True)
            time.sleep(max(0.02, float(click_delay) * 0.5))

            # Optional conditional click (color gate) - always the first in-window action.
            # At the end, click it again if the gate is no longer active (toggle-back behavior).
            cond_abs_xy: Optional[Tuple[int, int]] = None
            cond_clicked = False
            if conditional.enabled and conditional.point:
                cond_abs_xy = _abs_from_rel(hwnd, conditional.point)
                if cond_abs_xy:
                    px = _screen_pixel_rgb(*cond_abs_xy)
                    if px is not None and _color_close(px, _hex_to_rgb(conditional.color_hex), conditional.tolerance):
                        _mouse_left_click(hwnd, *cond_abs_xy)
                        cond_clicked = True
                        time.sleep(max(0.02, float(click_delay)))

            def _click(name: str):
                p = coords[name]
                abs_xy = _abs_from_rel(hwnd, p)
                if abs_xy:
                    _mouse_left_click(hwnd, *abs_xy)
                time.sleep(max(0.01, float(click_delay)))

            # Open inventory -> items
            _click("inv_button")
            _click("items_tab")

            for rule in rules_to_use:
                # Search box -> type item name
                _click("search_box")
                _send_ctrl_a()
                time.sleep(0.03)
                _send_unicode_text(rule.name)
                time.sleep(max(0.05, float(click_delay)))

                # Select first result / query
                _click("query_pos")

                # Amount box
                _click("amount_box")
                _send_ctrl_a()
                time.sleep(0.02)
                _send_typed_text(str(max(1, int(rule.amount))))
                time.sleep(max(0.05, float(click_delay)))

                # Use
                _click("use_button")
                time.sleep(max(0.05, float(click_delay)))

            # Close menu (double click like original use_item)
            _click("close_button")
            _click("close_button")

            # Click the conditional button again if the gate condition is now false.
            if cond_abs_xy:
                px_end = _screen_pixel_rgb(*cond_abs_xy)
                if px_end is not None:
                    if not _color_close(px_end, _hex_to_rgb(conditional.color_hex), conditional.tolerance):
                        _mouse_left_click(hwnd, *cond_abs_xy)
                        time.sleep(max(0.02, float(click_delay)))
                elif cond_clicked:
                    _mouse_left_click(hwnd, *cond_abs_xy)
                    time.sleep(max(0.02, float(click_delay)))

    def _eligible_in_biome(self, biome: str, rule: ItemRule) -> bool:
        allowed = rule.allowed_biomes
        if not allowed:
            return True
        b = (biome or "").strip().upper()
        if not b:
            return False
        return b in allowed

    def _menu_gate_allows(self, uid: str, min_not_in_menu_s: float) -> bool:
        """
        Return True only when:
          - in_menu_provider reports False (not in main menu), AND
          - it has been continuously False for at least min_not_in_menu_s seconds.

        Unknown (None) is treated as not allowed.
        """
        if self._in_menu_provider is None:
            return False

        try:
            in_menu = self._in_menu_provider(str(uid))
        except Exception:
            in_menu = None

        now = time.time()
        with self._state_lock:
            if in_menu is None or bool(in_menu):
                # Not allowed (unknown or in menu) -> reset timer
                self._not_in_menu_since.pop(str(uid), None)
                return False

            # Not in menu
            if float(min_not_in_menu_s) <= 0.0:
                self._not_in_menu_since.setdefault(str(uid), now)
                return True

            start = self._not_in_menu_since.get(str(uid))
            if start is None:
                self._not_in_menu_since[str(uid)] = now
                return False

            return (now - float(start)) >= float(min_not_in_menu_s)

    def _due_rules_for_user(
        self, uid: str, biome: str, rules: Sequence[Tuple[int, ItemRule]]
    ) -> List[Tuple[int, ItemRule]]:
        now = time.time()
        with self._state_lock:
            per = self._next_ready.setdefault(uid, {})
            pending = self._pending_use_at.setdefault(uid, {})
            pending_names = self._pending_item_name.setdefault(uid, {})

            # Prune pending entries that no longer exist in the current ruleset for this user
            # (e.g., item removed, user filter changed, etc.).
            try:
                valid = {int(i) for i, _r in (rules or [])}
            except Exception:
                valid = set()
            if valid:
                for pidx in list(pending.keys()):
                    if int(pidx) not in valid:
                        pending.pop(int(pidx), None)
                        pending_names.pop(int(pidx), None)
            else:
                pending.clear()
                pending_names.clear()

            due: List[Tuple[int, ItemRule]] = []
            for idx, r in rules:
                next_ok = float(per.get(idx, 0.0))
                pending_at = pending.get(idx)

                if not r.enabled:
                    # Cancel any in-flight alert schedule when the rule is disabled.
                    if pending_at is not None:
                        pending.pop(idx, None)
                        pending_names.pop(idx, None)
                    continue

                # If a pending schedule exists but the cooldown isn't ready anymore (e.g., config changed),
                # cancel and re-schedule later so the alert stays aligned with actual use.
                if pending_at is not None and now < next_ok:
                    pending.pop(idx, None)
                    pending_names.pop(idx, None)
                    continue

                # If the item at this index changed, don't trust the pending schedule.
                try:
                    pn = pending_names.get(idx)
                except Exception:
                    pn = None
                if pending_at is not None and pn and str(pn) != str(r.name):
                    pending.pop(idx, None)
                    pending_names.pop(idx, None)
                    continue

                if pending_at is not None and now < float(pending_at):
                    # Alert already sent; wait until the scheduled use time.
                    continue

                # If the alert's scheduled use time has passed but we cannot use right now (biome gate),
                # cancel so we send a fresh alert ~lead seconds before the *actual* use.
                if pending_at is not None and now >= float(pending_at) and not self._eligible_in_biome(biome, r):
                    pending.pop(idx, None)
                    pending_names.pop(idx, None)
                    continue

                if now < next_ok:
                    continue
                if not self._eligible_in_biome(biome, r):
                    continue
                if now >= next_ok:
                    due.append((idx, r))
            return due

    def _mark_used(self, uid: str, used: Sequence[Tuple[int, ItemRule]]) -> None:
        now = time.time()
        with self._state_lock:
            per = self._next_ready.setdefault(uid, {})
            pending = self._pending_use_at.setdefault(uid, {})
            pending_names = self._pending_item_name.setdefault(uid, {})
            for idx, r in used:
                per[idx] = now + max(0.0, float(r.cooldown_s))
                pending.pop(idx, None)
                pending_names.pop(idx, None)

    def _cancel_overdue_pending(self, uid: str, *, now: Optional[float] = None, reason: str = "") -> None:
        """
        Cancel only overdue pending schedules (use time already reached).

        This prevents using immediately after a long delay (menu/biome/window),
        so we can re-alert ~lead seconds before the actual use.
        """
        try:
            now_ts = float(now if now is not None else time.time())
        except Exception:
            now_ts = time.time()

        canceled: List[int] = []
        with self._state_lock:
            pending = self._pending_use_at.get(str(uid))
            if not pending:
                return
            names = self._pending_item_name.get(str(uid)) or {}
            for idx, use_at in list(pending.items()):
                try:
                    if now_ts >= float(use_at):
                        pending.pop(int(idx), None)
                        names.pop(int(idx), None)
                        canceled.append(int(idx))
                except Exception:
                    continue
            if not pending:
                self._pending_use_at.pop(str(uid), None)
            if names:
                self._pending_item_name[str(uid)] = names
            else:
                self._pending_item_name.pop(str(uid), None)

        if canceled:
            try:
                why = f" ({reason})" if reason else ""
                self._log(f"[Auto-Item] {uid}: canceled overdue alert schedule{why}")
            except Exception:
                pass

    def _schedule_item_alert(self, uid: str, pid: int, idx: int, r: ItemRule) -> bool:
        """
        Schedule a pre-use alert and delay for a rule.

        Returns True if a new alert was scheduled (and should not be used immediately).
        """
        if not (r.alert_enabled and r.alert_webhook and float(r.alert_lead_s) > 0.0):
            return False
        if not _auto_item_alerts_unlocked():
            return False

        # Suppress alerts when Anti-AFK is (or is about to be) overdue.
        try:
            fn = getattr(self, "_antiafk_overdue_within_provider", None)
            if fn is not None:
                # Overdue now => don't send alerts and don't attempt to use items (Auto-Item will yield to Anti-AFK).
                if bool(fn(0.0)):
                    try:
                        now_ts = time.time()
                        if (now_ts - float(self._last_antiafk_overdue_alert_log_ts)) >= 30.0:
                            self._last_antiafk_overdue_alert_log_ts = float(now_ts)
                            self._log("[Auto-Item] Anti-AFK overdue; suppressing Auto-Item alerts until it catches up.")
                    except Exception:
                        pass
                    return True

                # Stop alerts early when we are too close to becoming overdue for the alert's lead time.
                try:
                    lead_s = max(0.0, float(r.alert_lead_s))
                except Exception:
                    lead_s = 0.0
                window_s = lead_s + 15.0
                if window_s > 0.0 and bool(fn(float(window_s))):
                    try:
                        now_ts = time.time()
                        if (now_ts - float(self._last_antiafk_overdue_alert_log_ts)) >= 30.0:
                            self._last_antiafk_overdue_alert_log_ts = float(now_ts)
                            self._log(
                                f"[Auto-Item] Anti-AFK nearing overdue (<= {window_s:.0f}s); suppressing Auto-Item alerts for now."
                            )
                    except Exception:
                        pass
                    return False
        except Exception:
            pass

        now = time.time()
        use_at = now + float(r.alert_lead_s)
        with self._state_lock:
            per = self._pending_use_at.setdefault(str(uid), {})
            names = self._pending_item_name.setdefault(str(uid), {})
            if int(idx) in per:
                return False
            per[int(idx)] = float(use_at)
            names[int(idx)] = str(r.name)

        embed = _build_item_alert_embed(
            item_name=r.name,
            amount=r.amount,
            username=self._username(uid),
            server_label=self._server_label(uid),
            ps_link=self._ps_link(uid),
            use_at_epoch=use_at,
        )
        payload = {"content": str(r.alert_message or ""), "embeds": [embed]}

        try:
            threading.Thread(
                target=_post_webhook,
                args=(str(r.alert_webhook), payload),
                daemon=True,
                name="AutoItemWebhook",
            ).start()
        except Exception:
            pass

        try:
            self._log(f"[Auto-Item] {uid}: alert scheduled for '{r.name}' in {float(r.alert_lead_s):.1f}s")
        except Exception:
            pass
        return True

    def _run(self) -> None:
        self._log("[Auto-Item] Engine started.")
        if autoit is None:
            self._log("[Auto-Item] AutoIt is required for input. Auto-Item disabled.")
            return
        while not self._stop.is_set():
            cfg = self._cfg_snapshot()
            enabled = bool(cfg.get("enabled", False))
            interval = float(cfg.get("tick_interval", 1.0) or 1.0)

            # If alerts are locked, ensure they never delay actions via stale schedules.
            if not _auto_item_alerts_unlocked():
                with self._state_lock:
                    self._pending_use_at.clear()
                    self._pending_item_name.clear()

            if not enabled:
                # Avoid resuming delayed actions after a manual disable/enable flip.
                with self._state_lock:
                    self._pending_use_at.clear()
                    self._pending_item_name.clear()
                self._stop.wait(timeout=max(0.2, interval))
                continue

            users = [str(u).strip() for u in (cfg.get("users") or []) if str(u).strip()]
            coords = self._coords_from_cfg(cfg)
            conditional = self._conditional_from_cfg(cfg)
            click_delay = float(cfg.get("click_delay", 0.2) or 0.2)
            block_user_mouse_move = bool(cfg.get("disable_mouse_move", False))
            min_not_in_menu_s = 10.0

            if not users or not coords:
                # Can't act -> don't keep stale alert schedules.
                with self._state_lock:
                    self._pending_use_at.clear()
                    self._pending_item_name.clear()
                self._stop.wait(timeout=max(0.5, interval))
                continue

            # If a user was de-selected, don't keep pending alert schedules for them.
            try:
                active = {str(u) for u in users}
            except Exception:
                active = set()
            if active:
                with self._state_lock:
                    for puid in list(self._pending_use_at.keys()):
                        if str(puid) not in active:
                            self._pending_use_at.pop(str(puid), None)
                            self._pending_item_name.pop(str(puid), None)

            did_any = False
            paused = False
            pause_denied = False

            try:
                for uid in users:
                    if self._stop.is_set():
                        break

                    # If Auto-Item is disabled mid-cycle, finish the current user then stop.
                    try:
                        if not bool(self._cfg_snapshot().get("enabled", False)):
                            break
                    except Exception:
                        pass

                    now_ts = time.time()

                    pid = None
                    try:
                        pid = self._pid_provider(uid)
                    except Exception:
                        pid = None
                    if not pid:
                        self._cancel_overdue_pending(uid, now=now_ts, reason="window missing")
                        continue

                    hwnd = None
                    try:
                        hwnd = self._hwnd_provider(int(pid))
                    except Exception:
                        hwnd = None
                    if not hwnd:
                        self._cancel_overdue_pending(uid, now=now_ts, reason="window missing")
                        continue

                    biome = ""
                    try:
                        biome = self._biome_provider(uid) or ""
                    except Exception:
                        biome = ""

                    # Only operate when the user is in-game (not in the main menu) for a minimum duration.
                    if not self._menu_gate_allows(uid, min_not_in_menu_s):
                        self._cancel_overdue_pending(uid, now=now_ts, reason="menu gate")
                        continue

                    rules = self._rules_from_cfg(cfg, uid=uid)
                    if not rules:
                        # Item list changed/empty -> clear any pending schedules.
                        with self._state_lock:
                            self._pending_use_at.pop(str(uid), None)
                            self._pending_item_name.pop(str(uid), None)
                        continue

                    due = self._due_rules_for_user(uid, biome, rules)
                    if not due:
                        continue

                    # If any due rules have alerts configured, schedule them and skip immediate use.
                    to_use: List[Tuple[int, ItemRule]] = []
                    for idx, r in due:
                        try:
                            if self._schedule_item_alert(uid, int(pid), int(idx), r):
                                continue
                        except Exception:
                            pass
                        to_use.append((idx, r))

                    if not to_use:
                        continue

                    # Pause Anti-AFK before any BES/prep lead so it can't release holds mid-prep.
                    if pause_denied:
                        break
                    if not paused and self._pause_antiafk:
                        try:
                            res = self._pause_antiafk()
                            if res is False:
                                pause_denied = True
                                break
                            paused = True
                        except Exception:
                            paused = False

                    # Allow host to prepare (e.g., temporarily disable throttling) a bit before acting.
                    lead_s = 0.0
                    if self._pre_action_hook is not None:
                        try:
                            lead_s = float(self._pre_action_hook(str(uid), int(pid)) or 0.0)
                        except Exception:
                            lead_s = 0.0
                    if lead_s > 0.0:
                        self._stop.wait(timeout=max(0.0, float(lead_s)))
                        if self._stop.is_set():
                            break

                    used_rules = [r for _i, r in to_use]
                    try:
                        with self._action_lock:
                            self._use_items_on_window(
                                int(hwnd),
                                coords,
                                used_rules,
                                click_delay=click_delay,
                                conditional=conditional,
                                block_user_mouse_move=block_user_mouse_move,
                            )
                        self._mark_used(uid, to_use)
                        did_any = True
                        self._log(
                            f"[Auto-Item] {uid}: used "
                            + ", ".join(f"{r.name}x{r.amount}" for r in used_rules)
                            + (f" (biome={biome})" if biome else "")
                        )
                    except Exception as e:
                        self._log(f"[Auto-Item] {uid}: error during item use: {e}")
                    finally:
                        if self._post_action_hook is not None:
                            try:
                                self._post_action_hook(str(uid), int(pid))
                            except Exception:
                                pass

                    time.sleep(max(0.05, click_delay))
            finally:
                if paused and self._resume_antiafk:
                    try:
                        self._resume_antiafk()
                    except Exception:
                        pass

            # If we were disabled mid-cycle, loop back so the disabled branch clears pending schedules quickly.
            try:
                if not bool(self._cfg_snapshot().get("enabled", False)):
                    continue
            except Exception:
                pass

            # If we didn't do anything this cycle, sleep a bit longer to reduce churn.
            sleep_for = max(0.2, interval if did_any else min(2.0, interval))
            self._stop.wait(timeout=sleep_for)

        self._log("[Auto-Item] Engine stopped.")

    def test_once(self, uid: str) -> bool:
        """
        Run the configured automation once for a single user.
        - Uses enabled items in table order
        - Ignores cooldown timers
        - Respects biome restrictions when biome is known
        """
        if autoit is None:
            self._log("[Auto-Item] Test: AutoIt is required for input.")
            return False

        cfg = self._cfg_snapshot()
        coords = self._coords_from_cfg(cfg)
        rules = self._rules_from_cfg(cfg, uid=uid)
        conditional = self._conditional_from_cfg(cfg)
        click_delay = float(cfg.get("click_delay", 0.2) or 0.2)
        block_user_mouse_move = bool(cfg.get("disable_mouse_move", False))
        min_not_in_menu_s = 10.0

        if not coords:
            self._log("[Auto-Item] Test: missing coordinates. Capture coords first.")
            return False
        if not rules:
            self._log("[Auto-Item] Test: no items configured. Add at least one item.")
            return False

        pid = None
        try:
            pid = self._pid_provider(str(uid))
        except Exception:
            pid = None
        if not pid:
            self._log("[Auto-Item] Test: could not resolve PID for selected user (is the manager running?).")
            return False

        hwnd = None
        try:
            hwnd = self._hwnd_provider(int(pid))
        except Exception:
            hwnd = None
        if not hwnd:
            self._log("[Auto-Item] Test: could not resolve Roblox window handle for selected user.")
            return False

        biome = ""
        try:
            biome = self._biome_provider(str(uid)) or ""
        except Exception:
            biome = ""

        # Manual tests should not run in the main menu (inventory UI may not be ready).
        if self._in_menu_provider is not None:
            try:
                in_menu = self._in_menu_provider(str(uid))
            except Exception:
                in_menu = None
            if in_menu is None or bool(in_menu):
                self._log("[Auto-Item] Test: user appears to be in the main menu (or status unknown); skipping.")
                return False

            # Don't block the test run on the full timer, but note when it would have.
            try:
                if float(min_not_in_menu_s) > 0.0 and not self._menu_gate_allows(uid, min_not_in_menu_s):
                    self._log(f"[Auto-Item] Test: note: menu gate requires {min_not_in_menu_s:.0f}s out of menu.")
            except Exception:
                pass

        to_use: List[ItemRule] = []
        skipped: List[str] = []
        for _idx, r in rules:
            if not r.enabled:
                continue
            if not self._eligible_in_biome(biome, r):
                skipped.append(r.name)
                continue
            to_use.append(r)

        if not to_use:
            if skipped:
                msg = f"[Auto-Item] Test: no items allowed in current biome{f' ({biome})' if biome else ''}."
                self._log(msg + " Adjust item biome filters or move to an allowed biome.")
            else:
                self._log("[Auto-Item] Test: no enabled items to run.")
            return False

        paused = False
        try:
            if self._pause_antiafk:
                try:
                    res = self._pause_antiafk()
                    if res is False:
                        paused = False
                    else:
                        paused = True
                except Exception:
                    paused = False

            self._log(f"[Auto-Item] Test: running once for {uid} ({len(to_use)} item(s))...")
            with self._action_lock:
                self._use_items_on_window(
                    int(hwnd),
                    coords,
                    to_use,
                    click_delay=click_delay,
                    conditional=conditional,
                    block_user_mouse_move=block_user_mouse_move,
                )
            if skipped:
                self._log(
                    "[Auto-Item] Test: skipped due to biome filter: " + ", ".join(str(n) for n in skipped if str(n).strip())
                )
            self._log("[Auto-Item] Test: complete.")
            return True
        except Exception as e:
            self._log(f"[Auto-Item] Test: error: {e}")
            return False
        finally:
            if paused and self._resume_antiafk:
                try:
                    self._resume_antiafk()
                except Exception:
                    pass
