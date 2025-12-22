"""
Auto Item automation (multi-user) for JARAM.

This module is intentionally based on the click/type flow from `lib/macro_logic.py::use_item`,
but it does NOT import or call that function.
"""

from __future__ import annotations

import ctypes
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

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


def _mouse_move_natural(x: int, y: int) -> None:
    x = int(x)
    y = int(y)

    if autoit is None:
        return

    try:
        cur_x, cur_y = win32api.GetCursorPos()
    except Exception:
        cur_x, cur_y = x, y

    dx = float(x - int(cur_x))
    dy = float(y - int(cur_y))
    dist = (dx * dx + dy * dy) ** 0.5

    try:
        # AutoIt speed: 0=fastest (instant), 1..100 slower.
        # Keep motion "human-ish" but prioritize speed.
        if dist >= 250:
            speed = 3
        elif dist >= 120:
            speed = 4
        else:
            speed = 5

        jx = random.randint(-1, 1)
        jy = random.randint(-1, 1)
        autoit.mouse_move(int(x) + jx, int(y) + jy, speed=speed)
        if jx or jy:
            autoit.mouse_move(int(x), int(y), speed=0)
    except Exception:
        pass


def _mouse_left_click(x: int, y: int) -> None:
    x = int(x)
    y = int(y)

    if autoit is None:
        return

    # Move like a person and add small dwell time before pressing.
    _mouse_move_natural(x, y)
    time.sleep(random.uniform(0.0, 0.01))

    try:
        autoit.mouse_down("left")
        time.sleep(random.uniform(0.015, 0.03))
        autoit.mouse_up("left")
    except Exception:
        pass


def _send_ctrl_a() -> None:
    if autoit is not None:
        try:
            autoit.send("^a")
            return
        except Exception:
            pass


def _send_unicode_text(text: str) -> None:
    """
    Type text via AutoIt.
    """
    if autoit is None:
        return
    try:
        # mode=1 => raw text (do not treat +, !, ^, # as special keys)
        autoit.send(str(text or ""), mode=1)
    except Exception:
        pass


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
    """

    def __init__(
        self,
        *,
        pid_provider: Callable[[str], Optional[int]],
        hwnd_provider: Callable[[int], Optional[int]],
        biome_provider: Callable[[str], str],
        in_menu_provider: Optional[Callable[[str], Optional[bool]]] = None,
        log: Callable[[str], None],
        pause_antiafk: Optional[Callable[[], None]] = None,
        resume_antiafk: Optional[Callable[[], None]] = None,
        pre_action_hook: Optional[Callable[[str, int], float]] = None,
        post_action_hook: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._pid_provider = pid_provider
        self._hwnd_provider = hwnd_provider
        self._biome_provider = biome_provider
        self._in_menu_provider = in_menu_provider
        self._log = log
        self._pause_antiafk = pause_antiafk
        self._resume_antiafk = resume_antiafk
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
        self._not_in_menu_since: Dict[str, float] = {}
        self._state_lock = threading.Lock()

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

        uid_str = str(uid) if uid is not None else None

        for idx, it in enumerate(raw_items):
            try:
                if not isinstance(it, dict):
                    continue

                # Optional per-item user filter.
                # Backward compatible: empty list means "all users" unless users_explicit=True.
                if uid_str is not None:
                    raw_users = it.get("users", None)
                    users_explicit = bool(it.get("users_explicit", False))

                    if raw_users is None:
                        allowed_users = None
                    elif isinstance(raw_users, (list, tuple, set)):
                        allowed_users = [str(u).strip() for u in raw_users if str(u).strip()]
                    else:
                        allowed_users = None

                    if isinstance(allowed_users, list):
                        if allowed_users:
                            if uid_str not in allowed_users:
                                continue
                        else:
                            # Empty list => none (only when explicitly requested)
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
                out.append(
                    (
                        int(idx),
                        ItemRule(
                            name=name,
                            amount=max(1, amount),
                            cooldown_s=max(0.0, cooldown_s),
                            allowed_biomes=allowed,
                            enabled=enabled,
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
    ) -> None:
        with _window_topmost_during(hwnd):
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
                        _mouse_left_click(*cond_abs_xy)
                        cond_clicked = True
                        time.sleep(max(0.02, float(click_delay)))

            def _click(name: str):
                p = coords[name]
                abs_xy = _abs_from_rel(hwnd, p)
                if abs_xy:
                    _mouse_left_click(*abs_xy)
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
                _send_unicode_text(str(max(1, int(rule.amount))))
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
                        _mouse_left_click(*cond_abs_xy)
                        time.sleep(max(0.02, float(click_delay)))
                elif cond_clicked:
                    _mouse_left_click(*cond_abs_xy)
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
            due: List[Tuple[int, ItemRule]] = []
            for idx, r in rules:
                if not r.enabled:
                    continue
                if not self._eligible_in_biome(biome, r):
                    continue
                next_ok = float(per.get(idx, 0.0))
                if now >= next_ok:
                    due.append((idx, r))
            return due

    def _mark_used(self, uid: str, used: Sequence[Tuple[int, ItemRule]]) -> None:
        now = time.time()
        with self._state_lock:
            per = self._next_ready.setdefault(uid, {})
            for idx, r in used:
                per[idx] = now + max(0.0, float(r.cooldown_s))

    def _run(self) -> None:
        self._log("[Auto-Item] Engine started.")
        if autoit is None:
            self._log("[Auto-Item] AutoIt is required for input. Auto-Item disabled.")
            return
        while not self._stop.is_set():
            cfg = self._cfg_snapshot()
            enabled = bool(cfg.get("enabled", False))
            interval = float(cfg.get("tick_interval", 1.0) or 1.0)

            if not enabled:
                self._stop.wait(timeout=max(0.2, interval))
                continue

            users = [str(u) for u in (cfg.get("users") or []) if str(u).strip()]
            coords = self._coords_from_cfg(cfg)
            conditional = self._conditional_from_cfg(cfg)
            click_delay = float(cfg.get("click_delay", 0.2) or 0.2)
            min_not_in_menu_s = 10.0

            if not users or not coords:
                self._stop.wait(timeout=max(0.5, interval))
                continue

            did_any = False
            paused = False

            try:
                for uid in users:
                    if self._stop.is_set():
                        break

                    pid = None
                    try:
                        pid = self._pid_provider(uid)
                    except Exception:
                        pid = None
                    if not pid:
                        continue

                    hwnd = None
                    try:
                        hwnd = self._hwnd_provider(int(pid))
                    except Exception:
                        hwnd = None
                    if not hwnd:
                        continue

                    biome = ""
                    try:
                        biome = self._biome_provider(uid) or ""
                    except Exception:
                        biome = ""

                    # Only operate when the user is in-game (not in the main menu) for a minimum duration.
                    if not self._menu_gate_allows(uid, min_not_in_menu_s):
                        continue

                    rules = self._rules_from_cfg(cfg, uid=uid)
                    if not rules:
                        continue

                    due = self._due_rules_for_user(uid, biome, rules)
                    if not due:
                        continue

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

                    # Pause Anti-AFK only when we are about to interact.
                    if not paused and self._pause_antiafk:
                        try:
                            self._pause_antiafk()
                            paused = True
                        except Exception:
                            paused = False

                    used_rules = [r for _i, r in due]
                    try:
                        with self._action_lock:
                            self._use_items_on_window(
                                int(hwnd),
                                coords,
                                used_rules,
                                click_delay=click_delay,
                                conditional=conditional,
                            )
                        self._mark_used(uid, due)
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
                    self._pause_antiafk()
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
