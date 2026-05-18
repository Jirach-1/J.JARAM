"""
Auto Actions automation engine for JARAM.

This keeps the existing low-level click/paste helpers from the legacy
auto-item module, but switches the rule model over to named action strings
triggered by OCR filters.
"""

from __future__ import annotations

import copy
import ctypes
import datetime as _dt
import difflib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import win32con
import win32gui

from auto_item_automation import (
    RelPoint,
    _abs_from_rel,
    _auto_item_alerts_unlocked,
    _block_user_mouse_movement_during_actions,
    _bring_window_foreground,
    _color_close,
    _hex_to_rgb,
    _mouse_left_click,
    _mouse_move_instant,
    _normalize_user_id_list,
    _post_webhook,
    _screen_pixel_rgb,
    _send_ctrl_a,
    _send_unicode_text,
    _set_window_topmost,
    _window_topmost_during,
    autoit,
)

try:
    from ocr_worker import capture_window_image as _capture_window_image
except Exception:  # pragma: no cover
    _capture_window_image = None  # type: ignore[assignment]


APP_FOOTER = "J.JARAM JX 2x27"


@dataclass(frozen=True)
class RelRect:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class ActionCondition:
    enabled: bool = False
    kind: str = "color"
    point: Optional[RelPoint] = None
    roi: Optional[RelRect] = None
    color_hex: str = "#FFFFFF"
    tolerance: int = 0
    target_text: str = ""
    match_mode: str = "contains"
    case_sensitive: bool = False
    filter_ids: Tuple[str, ...] = ()
    color_filters: Tuple[Tuple[int, int, int, int], ...] = ()


@dataclass(frozen=True)
class ActionStep:
    name: str
    kind: str
    point: Optional[RelPoint] = None
    end_point: Optional[RelPoint] = None
    text: str = ""
    paste_per_user: bool = False
    paste_user_values: Tuple[Tuple[str, str], ...] = ()
    webhook_url: str = ""
    webhook_message: str = ""
    key: str = ""
    keys: Tuple[str, ...] = ()
    key_hold_s: float = 0.0
    click_button: str = "left"
    click_count: int = 1
    loop_count: int = 1
    wait_s: float = 0.0
    drag_duration_s: float = 0.5
    color_hex: str = "#FFFFFF"
    tolerance: int = 0
    select_all: bool = True
    scroll_direction: str = "down"
    condition: ActionCondition = field(default_factory=ActionCondition)


@dataclass(frozen=True)
class ActionRule:
    name: str
    actions: Tuple[ActionStep, ...]
    cooldown_s: float
    allowed_biomes: Tuple[str, ...]
    trigger_type: str
    trigger_filter_ids: Tuple[str, ...]
    repeat_mode: str
    repeat_count: int
    enabled: bool = True
    alert_enabled: bool = False
    alert_lead_s: float = 15.0
    alert_webhook: str = ""
    alert_message: str = ""


def _unique_strings(values: Iterable[Any], *, upper: bool = False) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if upper:
            value = value.upper()
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _normalize_filter_ids(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple, set)):
        try:
            raw = list(raw)  # type: ignore[arg-type]
        except Exception:
            return ()
    return tuple(_unique_strings(raw))


def _normalize_user_text_map(raw: Any) -> Tuple[Tuple[str, str], ...]:
    pairs: List[Tuple[Any, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                uid = item.get("uid", item.get("user_id", item.get("id", item.get("user"))))
                if "text" in item:
                    text = item.get("text")
                elif "value" in item:
                    text = item.get("value")
                else:
                    text = item.get("paste", item.get("content", ""))
                pairs.append((uid, text))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((item[0], item[1]))

    out: Dict[str, str] = {}
    for raw_uid, raw_text in pairs:
        uid = str(raw_uid or "").strip()
        if not uid:
            continue
        out[uid] = str(raw_text if raw_text is not None else "")
    return tuple((uid, out[uid]) for uid in sorted(out.keys()))


def _normalize_condition_color_filters(raw: Any) -> Tuple[Tuple[int, int, int, int], ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: List[Tuple[int, int, int, int]] = []
    seen: set[Tuple[int, int, int, int]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled", True)):
            continue
        try:
            spec = (
                max(0, min(255, int(item.get("r", 255) or 0))),
                max(0, min(255, int(item.get("g", 255) or 0))),
                max(0, min(255, int(item.get("b", 255) or 0))),
                max(0, min(255, int(item.get("tol", item.get("tolerance", 40)) or 0))),
            )
        except Exception:
            continue
        if spec in seen:
            continue
        seen.add(spec)
        out.append(spec)
    return tuple(out)


def _normalize_rel_point(raw: Any) -> Optional[RelPoint]:
    if not isinstance(raw, dict):
        return None
    try:
        return RelPoint(float(raw.get("x", 0.0)), float(raw.get("y", 0.0)))
    except Exception:
        return None


def _normalize_rel_rect(raw: Any) -> Optional[RelRect]:
    if not isinstance(raw, dict):
        return None
    try:
        rect = RelRect(
            float(raw.get("x", 0.0)),
            float(raw.get("y", 0.0)),
            float(raw.get("w", raw.get("width", 0.0))),
            float(raw.get("h", raw.get("height", 0.0))),
        )
    except Exception:
        return None
    if rect.w <= 0.0 or rect.h <= 0.0:
        return None
    return rect


def _normalize_condition(
    raw: Any,
    *,
    default_enabled: bool = False,
    legacy_point: Optional[RelPoint] = None,
    legacy_color: str = "#FFFFFF",
    legacy_tolerance: int = 0,
) -> ActionCondition:
    base = raw if isinstance(raw, dict) else {}

    kind = str(base.get("type") or base.get("kind") or base.get("condition_type") or "color").strip().lower()
    if kind in ("colour", "pixel", "pixel_color", "color_conditional", "conditional_click"):
        kind = "color"
    elif kind in ("ocr", "ocr_text", "ocr_conditional"):
        kind = "ocr"
    else:
        kind = "color"

    enabled_default = bool(default_enabled)
    enabled = bool(base.get("enabled", enabled_default))

    try:
        tolerance = max(0, int(base.get("tolerance", base.get("tol", legacy_tolerance)) or 0))
    except Exception:
        tolerance = max(0, int(legacy_tolerance or 0))

    point = _normalize_rel_point(base.get("point") or base.get("condition_point") or base.get("conditional_point"))
    if point is None:
        point = legacy_point

    roi = _normalize_rel_rect(base.get("roi") or base.get("area") or base.get("ocr_roi"))

    match_mode = str(base.get("match_mode") or base.get("ocr_match_mode") or "contains").strip().lower()
    if match_mode not in ("contains", "equals", "regex", "fuzzy"):
        match_mode = "contains"

    return ActionCondition(
        enabled=enabled,
        kind=kind,
        point=point,
        roi=roi,
        color_hex=str(base.get("color") or base.get("color_hex") or legacy_color or "#FFFFFF").strip() or "#FFFFFF",
        tolerance=tolerance,
        target_text=str(base.get("target_text") or base.get("ocr_text") or base.get("text") or "").strip(),
        match_mode=match_mode,
        case_sensitive=bool(base.get("case_sensitive", False)),
        filter_ids=_normalize_filter_ids(base.get("filter_ids", base.get("ocr_filter_ids", [])) or []),
        color_filters=_normalize_condition_color_filters(
            base.get("color_filters", base.get("ocr_color_filters", [])) or []
        ),
    )


def _normalize_action_step(raw: Any, *, fallback_name: str = "") -> Optional[ActionStep]:
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("type") or raw.get("kind") or raw.get("action_type") or "").strip().lower()
    legacy_conditional_click = kind in ("conditional", "conditional_click")
    if legacy_conditional_click:
        kind = "click"
    elif kind in ("keyboard", "keyboard_click", "key_click", "keypress", "key_press"):
        kind = "key"
    elif kind in ("end", "end_if", "endif", "end_loop", "end_block"):
        kind = "end"
    elif kind in ("sleep", "wait_sleep", "delay"):
        kind = "wait"
    elif kind in ("mouse_drag", "drag_click", "click_drag"):
        kind = "drag"
    elif kind in ("discord_webhook", "send_webhook", "webhook_send"):
        kind = "webhook"
    if kind not in ("click", "key", "paste", "scroll", "drag", "wait", "webhook", "if", "else", "end", "break", "loop"):
        return None

    name = str(raw.get("name") or fallback_name or kind.replace("_", " ").title()).strip()
    point = _normalize_rel_point(raw.get("point") or raw.get("start_point") or raw.get("from_point"))
    end_point = _normalize_rel_point(raw.get("end_point") or raw.get("to_point") or raw.get("target_point"))

    try:
        tolerance = max(0, int(raw.get("tolerance", 0) or 0))
    except Exception:
        tolerance = 0

    try:
        select_all = bool(raw.get("select_all", True))
    except Exception:
        select_all = True

    paste_per_user = bool(
        raw.get(
            "paste_per_user",
            raw.get("per_user_paste", raw.get("user_specific_paste", False)),
        )
    )
    paste_user_values = _normalize_user_text_map(
        raw.get(
            "paste_user_values",
            raw.get(
                "per_user_paste_values",
                raw.get("user_texts", raw.get("per_user_text", raw.get("user_values", {}))),
            ),
        )
    )

    scroll_direction = "up" if str(raw.get("scroll_direction") or raw.get("direction") or "").strip().lower() == "up" else "down"

    click_button = str(raw.get("click_button") or raw.get("button") or "left").strip().lower()
    if click_button not in ("left", "right"):
        click_button = "left"

    try:
        click_count = max(1, min(2, int(raw.get("click_count", 2 if bool(raw.get("double_click", False)) else 1) or 1)))
    except Exception:
        click_count = 1

    try:
        key_hold_s = max(0.0, float(raw.get("key_hold_s", raw.get("hold_s", raw.get("hold_seconds", 0.0))) or 0.0))
    except Exception:
        key_hold_s = 0.0
    key_values = _unique_strings(raw.get("keys") or [])
    legacy_key = str(raw.get("key") or raw.get("key_name") or "").strip()
    if legacy_key and legacy_key not in key_values:
        key_values.insert(0, legacy_key)

    try:
        loop_count = max(1, int(raw.get("loop_count", raw.get("count", raw.get("times", 1))) or 1))
    except Exception:
        loop_count = 1

    try:
        wait_s = max(0.0, float(raw.get("wait_s", raw.get("sleep_s", raw.get("seconds", raw.get("duration", 0.0)))) or 0.0))
    except Exception:
        wait_s = 0.0

    try:
        drag_duration_s = max(0.0, float(raw.get("drag_duration_s", raw.get("drag_seconds", raw.get("move_duration_s", 0.5))) or 0.0))
    except Exception:
        drag_duration_s = 0.5

    condition_raw = raw.get("condition") if isinstance(raw.get("condition"), dict) else raw.get("conditional")
    if legacy_conditional_click:
        condition = _normalize_condition(
            condition_raw,
            default_enabled=True,
            legacy_point=point,
            legacy_color=str(raw.get("color") or raw.get("color_hex") or "#FFFFFF").strip() or "#FFFFFF",
            legacy_tolerance=tolerance,
        )
    else:
        top_level_condition_keys = (
            "condition_enabled",
            "conditional_enabled",
            "condition_type",
            "condition_point",
            "conditional_point",
            "ocr_text",
            "target_text",
            "ocr_roi",
        )
        if not isinstance(condition_raw, dict) and any(k in raw for k in top_level_condition_keys):
            condition_raw = {
                "enabled": raw.get("condition_enabled", raw.get("conditional_enabled", False)),
                "type": raw.get("condition_type", raw.get("conditional_type", "color")),
                "point": raw.get("condition_point") or raw.get("conditional_point"),
                "roi": raw.get("ocr_roi") or raw.get("roi"),
                "color": raw.get("condition_color", raw.get("color", "#FFFFFF")),
                "tolerance": raw.get("condition_tolerance", raw.get("tolerance", 0)),
                "target_text": raw.get("target_text", raw.get("ocr_text", "")),
                "match_mode": raw.get("match_mode", raw.get("ocr_match_mode", "contains")),
                "case_sensitive": raw.get("case_sensitive", False),
            }
        condition = _normalize_condition(condition_raw, default_enabled=(kind == "if"))

    return ActionStep(
        name=name or kind.replace("_", " ").title(),
        kind=kind,
        point=point,
        end_point=end_point,
        text=str(raw.get("text") or ""),
        paste_per_user=paste_per_user,
        paste_user_values=paste_user_values,
        webhook_url=str(raw.get("webhook_url") or raw.get("url") or "").strip(),
        webhook_message=str(
            raw.get("webhook_message")
            or raw.get("message")
            or raw.get("content")
            or (raw.get("text") if kind == "webhook" else "")
            or ""
        ),
        key=key_values[0] if key_values else "",
        keys=tuple(key_values),
        key_hold_s=key_hold_s,
        click_button=click_button,
        click_count=click_count,
        loop_count=loop_count,
        wait_s=wait_s,
        drag_duration_s=drag_duration_s,
        color_hex=str(raw.get("color") or raw.get("color_hex") or "#FFFFFF").strip() or "#FFFFFF",
        tolerance=tolerance,
        select_all=select_all,
        scroll_direction=scroll_direction,
        condition=condition,
    )


def _normalize_behavior(raw: Any, legacy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = raw if isinstance(raw, dict) else {}
    legacy = legacy or {}

    trigger = base.get("trigger") if isinstance(base.get("trigger"), dict) else {}
    filter_ids = _normalize_filter_ids(
        trigger.get("filter_ids", base.get("filter_ids", legacy.get("filter_ids", []))) or []
    )
    raw_trigger_type = str(trigger.get("type") or base.get("trigger_type") or "").strip().lower()
    if raw_trigger_type in ("normal", "none"):
        trigger_type = "normal"
    elif raw_trigger_type == "ocr_filter":
        trigger_type = "ocr_filter" if filter_ids else "normal"
    else:
        trigger_type = "ocr_filter" if filter_ids else "normal"
    if trigger_type != "ocr_filter":
        filter_ids = ()

    repeat_mode = str(base.get("repeat_mode") or legacy.get("repeat_mode") or "repeat").strip().lower()
    if repeat_mode not in ("repeat", "count", "once_per_pid"):
        repeat_mode = "repeat"

    try:
        repeat_count = max(1, int(base.get("repeat_count", legacy.get("repeat_count", 1)) or 1))
    except Exception:
        repeat_count = 1

    try:
        cooldown = max(
            0.0,
            float(
                base.get(
                    "cooldown",
                    legacy.get("cooldown", legacy.get("cooldown_s", 0.0)),
                )
                or 0.0
            ),
        )
    except Exception:
        cooldown = 0.0

    biomes = tuple(
        _unique_strings(
            base.get("biomes", legacy.get("biomes", legacy.get("allowed_biomes", []))) or [],
            upper=True,
        )
    )

    return {
        "cooldown": cooldown,
        "biomes": biomes,
        "repeat_mode": repeat_mode,
        "repeat_count": repeat_count,
        "trigger_type": trigger_type,
        "filter_ids": filter_ids,
    }


def _build_action_alert_embed(
    *,
    action_name: str,
    username: str,
    server_label: str,
    ps_link: str,
    use_at_epoch: float,
) -> dict:
    import datetime as _dt

    unix = int(use_at_epoch)
    iso = _dt.datetime.fromtimestamp(unix, tz=_dt.timezone.utc).isoformat()

    ts_full = f"<t:{unix}:D>  -  <t:{unix}:T>"
    ts_rel = f"<t:{unix}:R>"

    server = str(server_label or "").strip() or "N/A"
    ps = str(ps_link or "").strip()
    if ps:
        ps_line = f"**Private Server:** [Private Server Link]({ps})"
    else:
        ps_line = f"**Private Server:** `{server}`"

    uname = str(username or "").strip() or "Unknown"
    action_disp = str(action_name or "").strip() or "Unnamed Action"

    description = (
        f"**Account:** `{uname}`\n"
        f"**Action:** `{action_disp}`\n"
        f"**Time:** {ts_full} ({ts_rel})\n"
        f"{ps_line}"
    )

    return {
        "title": "Auto-Actions Alert",
        "description": description,
        "color": 0xF59E0B,
        "timestamp": iso,
        "footer": {"text": f"{APP_FOOTER}  -  {server}"},
    }


_AUTOIT_KEY_ALIASES = {
    "space": "{SPACE}",
    "enter": "{ENTER}",
    "return": "{ENTER}",
    "tab": "{TAB}",
    "esc": "{ESC}",
    "escape": "{ESC}",
    "backspace": "{BACKSPACE}",
    "delete": "{DELETE}",
    "del": "{DELETE}",
    "insert": "{INSERT}",
    "ins": "{INSERT}",
    "home": "{HOME}",
    "end": "{END}",
    "pageup": "{PGUP}",
    "pgup": "{PGUP}",
    "pagedown": "{PGDN}",
    "pgdn": "{PGDN}",
    "up": "{UP}",
    "down": "{DOWN}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
    "shift": "{SHIFT}",
    "ctrl": "{CTRL}",
    "control": "{CTRL}",
    "alt": "{ALT}",
}


def _autoit_key_token(key: str) -> str:
    raw = str(key or "").strip()
    if not raw:
        return ""
    if raw.startswith("{") and raw.endswith("}"):
        return raw
    low = raw.lower()
    if low in _AUTOIT_KEY_ALIASES:
        return _AUTOIT_KEY_ALIASES[low]
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", low):
        return "{" + low.upper() + "}"
    if len(raw) == 1:
        return raw
    return "{" + raw.upper() + "}"


def _autoit_key_hold_tokens(key: str) -> Tuple[str, str]:
    token = _autoit_key_token(key)
    if not token:
        return "", ""
    if token.startswith("{") and token.endswith("}"):
        name = token[1:-1].strip()
    else:
        name = token.strip()
    if not name:
        return "", ""
    return "{" + name + " down}", "{" + name + " up}"


def _send_single_key(key: str, hold_s: float = 0.0) -> None:
    _send_keys([key], hold_s)


def _send_keys(keys: Sequence[str], hold_s: float = 0.0) -> None:
    if autoit is None:
        return
    clean_keys = _unique_strings(keys or [])
    if not clean_keys:
        return
    try:
        hold = max(0.0, float(hold_s or 0.0))
    except Exception:
        hold = 0.0

    if len(clean_keys) > 1 or hold > 0.0:
        down_up = [_autoit_key_hold_tokens(key) for key in clean_keys]
        down_up = [(down, up) for down, up in down_up if down and up]
        if down_up:
            try:
                for down, _up in down_up:
                    autoit.send(down)
                    time.sleep(0.01)
                time.sleep(max(0.02, hold))
            finally:
                for _down, up in reversed(down_up):
                    try:
                        autoit.send(up)
                        time.sleep(0.005)
                    except Exception:
                        pass
            return

    token = _autoit_key_token(clean_keys[0])
    if not token:
        return
    try:
        if len(token) == 1:
            autoit.send(token, mode=1)
        else:
            autoit.send(token)
    except Exception:
        pass


def _mouse_button_click(hwnd: int, x: int, y: int, *, button: str = "left", count: int = 1) -> None:
    button_s = "right" if str(button or "").strip().lower() == "right" else "left"
    try:
        count_i = max(1, min(2, int(count or 1)))
    except Exception:
        count_i = 1

    for idx in range(count_i):
        if button_s == "left":
            _mouse_left_click(hwnd, int(x), int(y))
        else:
            try:
                if hwnd and _bring_window_foreground(hwnd):
                    time.sleep(0.01)
            except Exception:
                pass
            _mouse_move_instant(int(x), int(y))
            try:
                autoit.mouse_down("right")
                time.sleep(0.01)
            except Exception:
                pass
            finally:
                try:
                    autoit.mouse_up("right")
                except Exception:
                    pass
                try:
                    ctypes.windll.user32.mouse_event(int(win32con.MOUSEEVENTF_RIGHTUP), 0, 0, 0, 0)
                except Exception:
                    pass
        if idx + 1 < count_i:
            time.sleep(0.06)


def _mouse_button_drag(
    hwnd: int,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    *,
    button: str = "left",
    duration_s: float = 0.5,
) -> None:
    if autoit is None:
        return
    button_s = "right" if str(button or "").strip().lower() == "right" else "left"
    try:
        duration = max(0.0, float(duration_s or 0.0))
    except Exception:
        duration = 0.5
    try:
        if hwnd:
            _bring_window_foreground(hwnd)
            time.sleep(0.01)
    except Exception:
        pass

    _mouse_move_instant(int(start_x), int(start_y))
    time.sleep(0.02)

    up_flag = win32con.MOUSEEVENTF_RIGHTUP if button_s == "right" else win32con.MOUSEEVENTF_LEFTUP
    try:
        autoit.mouse_down(button_s)
        if duration <= 0.0:
            _mouse_move_instant(int(end_x), int(end_y))
        else:
            steps = max(2, min(240, int(duration * 60)))
            sleep_s = duration / float(steps)
            sx = float(start_x)
            sy = float(start_y)
            ex = float(end_x)
            ey = float(end_y)
            for step_idx in range(1, steps + 1):
                t = float(step_idx) / float(steps)
                x = int(round(sx + ((ex - sx) * t)))
                y = int(round(sy + ((ey - sy) * t)))
                _mouse_move_instant(x, y)
                time.sleep(max(0.001, sleep_s))
    except Exception:
        pass
    finally:
        try:
            autoit.mouse_up(button_s)
        except Exception:
            pass
        try:
            ctypes.windll.user32.mouse_event(int(up_flag), 0, 0, 0, 0)
        except Exception:
            pass


def _window_rel_pixel_rgb(hwnd: int, point: RelPoint) -> Optional[Tuple[int, int, int]]:
    if _capture_window_image is None:
        return None
    try:
        full = _capture_window_image(int(hwnd))
    except Exception:
        full = None
    if full is None:
        return None
    try:
        try:
            _lo, hi = full.convert("L").getextrema()
            if int(hi) <= 5:
                return None
        except Exception:
            pass
        _cl, _ct, cr, cb = win32gui.GetClientRect(int(hwnd))
        client_w = max(1, int(cr - _cl))
        client_h = max(1, int(cb - _ct))
        wl, wt, wr, wb = win32gui.GetWindowRect(int(hwnd))
        win_w = max(1, int(wr - wl))
        win_h = max(1, int(wb - wt))
        scale_x = float(full.width) / float(win_w)
        scale_y = float(full.height) / float(win_h)
        client_left, client_top = win32gui.ClientToScreen(int(hwnd), (0, 0))
        crop_left = int((client_left - wl) * scale_x)
        crop_top = int((client_top - wt) * scale_y)
        crop_w = max(1, int(client_w * scale_x))
        crop_h = max(1, int(client_h * scale_y))
        rel_x = max(0.0, min(1.0, float(point.x)))
        rel_y = max(0.0, min(1.0, float(point.y)))
        px = max(0, min(full.width - 1, crop_left + int(rel_x * crop_w)))
        py = max(0, min(full.height - 1, crop_top + int(rel_y * crop_h)))
        return tuple(full.convert("RGB").getpixel((px, py))[:3])  # type: ignore[return-value]
    except Exception:
        return None


def _condition_payload(condition: ActionCondition) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "enabled": bool(condition.enabled),
        "type": str(condition.kind or "color"),
        "color": str(condition.color_hex or "#FFFFFF"),
        "tolerance": int(condition.tolerance or 0),
        "target_text": str(condition.target_text or ""),
        "match_mode": str(condition.match_mode or "contains"),
        "case_sensitive": bool(condition.case_sensitive),
        "filter_ids": list(condition.filter_ids or ()),
        "color_filters": [
            {"r": int(r), "g": int(g), "b": int(b), "tol": int(tol), "enabled": True}
            for r, g, b, tol in (condition.color_filters or ())
        ],
    }
    if condition.point is not None:
        payload["point"] = {"x": float(condition.point.x), "y": float(condition.point.y)}
    if condition.roi is not None:
        payload["roi"] = {
            "x": float(condition.roi.x),
            "y": float(condition.roi.y),
            "w": float(condition.roi.w),
            "h": float(condition.roi.h),
        }
    return payload


def _ocr_text_matches(text: str, condition: ActionCondition) -> bool:
    raw_text = str(text or "")
    target = str(condition.target_text or "").strip()
    if not target:
        return bool(raw_text.strip())

    mode = str(condition.match_mode or "contains")
    flags = 0 if bool(condition.case_sensitive) else re.IGNORECASE
    if mode == "regex":
        try:
            return re.search(target, raw_text, flags=flags) is not None
        except re.error:
            return False

    if bool(condition.case_sensitive):
        haystack = raw_text
        needle = target
    else:
        haystack = raw_text.lower()
        needle = target.lower()

    if str(condition.match_mode or "contains") == "equals":
        lines = [line.strip() for line in haystack.splitlines() if line.strip()]
        return haystack.strip() == needle.strip() or needle.strip() in lines

    if mode == "fuzzy":
        def _normalize(value: str) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()

        hay_norm = _normalize(haystack)
        needle_norm = _normalize(needle)
        if not needle_norm:
            return bool(hay_norm)
        if needle_norm in hay_norm:
            return True

        threshold = 0.9 if len(needle_norm) < 16 else 0.82
        candidates = [hay_norm]
        candidates.extend(_normalize(line) for line in haystack.splitlines() if _normalize(line))

        words = hay_norm.split()
        target_words = needle_norm.split()
        if words and target_words:
            target_count = len(target_words)
            for size in range(max(1, target_count - 2), min(len(words), target_count + 2) + 1):
                for start in range(0, max(0, len(words) - size) + 1):
                    candidates.append(" ".join(words[start : start + size]))

        seen: set[str] = set()
        for candidate in candidates:
            candidate = _normalize(candidate)
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if difflib.SequenceMatcher(None, candidate, needle_norm).ratio() >= threshold:
                return True
        return False

    return needle in haystack


class AutoActionEngine:
    """
    Background worker that plays configured action strings for selected users.

    The host is expected to:
      - Call `update_config()` whenever UI settings change.
      - Provide `record_ocr_filter_trigger()` events from the OCR worker.
      - Provide pid/biome/menu/window lookup callbacks for each user.
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
        discord_ping_provider: Optional[Callable[[str], str]] = None,
        log: Callable[[str], None],
        mouse_block_notify: Optional[Callable[[bool], None]] = None,
        pause_antiafk: Optional[Callable[[], None]] = None,
        resume_antiafk: Optional[Callable[[], None]] = None,
        antiafk_overdue_within_provider: Optional[Callable[[float], bool]] = None,
        pre_action_hook: Optional[Callable[[str, int], float]] = None,
        post_action_hook: Optional[Callable[[str, int], None]] = None,
        ocr_text_provider: Optional[Callable[[int, Dict[str, Any]], str]] = None,
    ) -> None:
        self._pid_provider = pid_provider
        self._hwnd_provider = hwnd_provider
        self._biome_provider = biome_provider
        self._in_menu_provider = in_menu_provider
        self._username_provider = username_provider
        self._server_label_provider = server_label_provider
        self._ps_link_provider = ps_link_provider
        self._discord_ping_provider = discord_ping_provider
        self._log = log
        self._mouse_block_notify = mouse_block_notify
        self._pause_antiafk = pause_antiafk
        self._resume_antiafk = resume_antiafk
        self._antiafk_overdue_within_provider = antiafk_overdue_within_provider
        self._pre_action_hook = pre_action_hook
        self._post_action_hook = post_action_hook
        self._ocr_text_provider = ocr_text_provider

        self._cfg_lock = threading.Lock()
        self._cfg: Dict[str, Any] = {"enabled": False}

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._action_lock = threading.Lock()

        self._state_lock = threading.Lock()
        self._next_ready: Dict[str, Dict[int, float]] = {}
        self._pending_use_at: Dict[str, Dict[int, float]] = {}
        self._last_trigger_seq: Dict[str, Dict[int, int]] = {}
        self._completed_pids: Dict[str, Dict[int, set[int]]] = {}
        self._rule_signature: Dict[str, Dict[int, int]] = {}
        self._not_in_menu_since: Dict[str, float] = {}
        self._trigger_events: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._trigger_seq: int = 0

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="AutoActionEngine", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_s)))

    def update_config(self, cfg: Dict[str, Any]) -> None:
        with self._cfg_lock:
            self._cfg = copy.deepcopy(cfg or {})

    def record_ocr_filter_trigger(self, uid: str, pid: int, filter_id: str, filter_name: str = "") -> None:
        uid_s = str(uid or "").strip()
        filter_id_s = str(filter_id or "").strip()
        if not uid_s or not filter_id_s:
            return
        with self._state_lock:
            self._trigger_seq += 1
            self._trigger_events.setdefault(uid_s, {})[filter_id_s] = {
                "seq": int(self._trigger_seq),
                "ts": float(time.time()),
                "pid": int(pid or 0),
                "name": str(filter_name or filter_id_s),
            }

    def _cfg_snapshot(self) -> Dict[str, Any]:
        with self._cfg_lock:
            return copy.deepcopy(self._cfg or {})

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

    def _discord_ping(self, uid: str) -> str:
        fn = self._discord_ping_provider
        if fn is None:
            return ""
        try:
            return str(fn(str(uid)) or "").strip()
        except Exception:
            return ""

    def _rules_from_cfg(self, cfg: Dict[str, Any], *, uid: Optional[str] = None) -> List[Tuple[int, ActionRule]]:
        out: List[Tuple[int, ActionRule]] = []
        raw_items = cfg.get("items") or []
        if not isinstance(raw_items, list):
            raw_items = []

        uid_s = str(uid).strip() if uid is not None else None

        for idx, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue

            if uid_s is not None:
                raw_users = raw.get("users", None)
                users_explicit = bool(raw.get("users_explicit", False))
                allowed_users = _normalize_user_id_list(raw_users)
                if isinstance(allowed_users, list):
                    if allowed_users:
                        if uid_s not in allowed_users:
                            continue
                    elif users_explicit:
                        continue

            actions_raw = raw.get("actions") or []
            if not isinstance(actions_raw, list):
                actions_raw = []
            actions: List[ActionStep] = []
            for action_idx, action_raw in enumerate(actions_raw):
                step = _normalize_action_step(action_raw, fallback_name=f"Action {action_idx + 1}")
                if step is not None:
                    actions.append(step)
            if not actions:
                continue

            behavior = _normalize_behavior(raw.get("behavior"), legacy=raw)
            name = str(raw.get("name") or f"Action {idx + 1}").strip() or f"Action {idx + 1}"

            try:
                alert_lead_s = max(0.0, float(raw.get("alert_lead_s", 15.0) or 15.0))
            except Exception:
                alert_lead_s = 15.0

            out.append(
                (
                    int(idx),
                    ActionRule(
                        name=name,
                        actions=tuple(actions),
                        cooldown_s=float(behavior.get("cooldown", 0.0) or 0.0),
                        allowed_biomes=tuple(behavior.get("biomes") or ()),
                        trigger_type=str(behavior.get("trigger_type") or "ocr_filter"),
                        trigger_filter_ids=tuple(behavior.get("filter_ids") or ()),
                        repeat_mode=str(behavior.get("repeat_mode") or "repeat"),
                        repeat_count=max(1, int(behavior.get("repeat_count", 1) or 1)),
                        enabled=bool(raw.get("enabled", True)),
                        alert_enabled=bool(raw.get("alert_enabled", False)),
                        alert_lead_s=alert_lead_s,
                        alert_webhook=str(raw.get("alert_webhook") or raw.get("alert_webhook_url") or "").strip(),
                        alert_message=str(raw.get("alert_message") or ""),
                    ),
                )
            )

        return out

    @staticmethod
    def _eligible_in_biome(biome: str, rule: ActionRule) -> bool:
        allowed = tuple(str(b).strip().upper() for b in (rule.allowed_biomes or ()) if str(b).strip())
        if not allowed:
            return True
        biome_s = str(biome or "").strip().upper()
        if not biome_s:
            return False
        return biome_s in allowed

    def _menu_gate_allows(self, uid: str, min_not_in_menu_s: float) -> bool:
        if self._in_menu_provider is None:
            return False

        try:
            in_menu = self._in_menu_provider(str(uid))
        except Exception:
            in_menu = None

        now = time.time()
        with self._state_lock:
            if in_menu is None or bool(in_menu):
                self._not_in_menu_since.pop(str(uid), None)
                return False

            if float(min_not_in_menu_s) <= 0.0:
                self._not_in_menu_since.setdefault(str(uid), now)
                return True

            started = self._not_in_menu_since.get(str(uid))
            if started is None:
                self._not_in_menu_since[str(uid)] = now
                return False
            return (now - float(started)) >= float(min_not_in_menu_s)

    def _latest_trigger_event(self, uid: str, rule: ActionRule) -> Optional[Dict[str, Any]]:
        if rule.trigger_type != "ocr_filter" or not rule.trigger_filter_ids:
            return None
        events = self._trigger_events.get(str(uid), {})
        latest: Optional[Dict[str, Any]] = None
        for filter_id in rule.trigger_filter_ids:
            event = events.get(str(filter_id))
            if not event:
                continue
            if latest is None or int(event.get("seq", 0) or 0) > int(latest.get("seq", 0) or 0):
                latest = event
        return latest

    def _schedule_alert(self, uid: str, rule: ActionRule) -> None:
        if not (rule.alert_enabled and rule.alert_webhook and float(rule.alert_lead_s) > 0.0):
            return
        if not _auto_item_alerts_unlocked():
            return

        use_at = time.time() + float(rule.alert_lead_s)
        payload = {
            "content": str(rule.alert_message or ""),
            "embeds": [
                _build_action_alert_embed(
                    action_name=rule.name,
                    username=self._username(uid),
                    server_label=self._server_label(uid),
                    ps_link=self._ps_link(uid),
                    use_at_epoch=use_at,
                )
            ],
        }

        try:
            threading.Thread(
                target=_post_webhook,
                args=(str(rule.alert_webhook), payload),
                daemon=True,
                name="AutoActionWebhook",
            ).start()
        except Exception:
            pass

        try:
            self._log(f"[Auto-Actions] {uid}: alert scheduled for '{rule.name}' in {float(rule.alert_lead_s):.1f}s")
        except Exception:
            pass

    def _mark_used(self, uid: str, pid: int, used: Sequence[Tuple[int, ActionRule]]) -> None:
        now = time.time()
        with self._state_lock:
            next_ready = self._next_ready.setdefault(str(uid), {})
            completed = self._completed_pids.setdefault(str(uid), {})
            pending = self._pending_use_at.setdefault(str(uid), {})
            for idx, rule in used:
                next_ready[int(idx)] = now + max(0.0, float(rule.cooldown_s))
                pending.pop(int(idx), None)
                if str(rule.repeat_mode) == "once_per_pid":
                    completed.setdefault(int(idx), set()).add(int(pid or 0))

    def _condition_matches(self, hwnd: int, condition: ActionCondition, *, click_delay: float) -> bool:
        if not bool(condition.enabled):
            return True

        if condition.kind == "ocr":
            provider = self._ocr_text_provider
            if provider is None:
                try:
                    self._log("[Auto-Actions] OCR conditional skipped; OCR reader is not available.")
                except Exception:
                    pass
                time.sleep(max(0.01, float(click_delay)))
                return False
            try:
                text = str(provider(int(hwnd), _condition_payload(condition)) or "")
            except Exception as e:
                try:
                    self._log(f"[Auto-Actions] OCR conditional failed: {e}")
                except Exception:
                    pass
                time.sleep(max(0.01, float(click_delay)))
                return False
            return _ocr_text_matches(text, condition)

        if condition.point is None:
            time.sleep(max(0.01, float(click_delay)))
            return False

        abs_xy = _abs_from_rel(hwnd, condition.point)
        if not abs_xy:
            time.sleep(max(0.01, float(click_delay)))
            return False

        expected = _hex_to_rgb(condition.color_hex)
        tolerance = int(condition.tolerance or 0)
        sampled = [
            px
            for px in (
                _window_rel_pixel_rgb(hwnd, condition.point),
                _screen_pixel_rgb(*abs_xy),
            )
            if px is not None
        ]
        if not sampled:
            time.sleep(max(0.01, float(click_delay)))
            return False
        return any(_color_close(px, expected, tolerance) for px in sampled)

    @staticmethod
    def _find_if_bounds(actions: Sequence[ActionStep], start_idx: int, end_idx: Optional[int] = None) -> Tuple[Optional[int], int]:
        limit = len(actions) if end_idx is None else max(0, min(len(actions), int(end_idx)))
        depth = 0
        for idx in range(int(start_idx) + 1, limit):
            kind = str(actions[idx].kind or "")
            if kind in ("if", "loop"):
                depth += 1
            elif kind in ("end", "endif", "end_if", "end_loop", "end_block"):
                if depth == 0:
                    return None, idx
                depth -= 1
            elif kind == "else" and depth == 0:
                else_idx = idx
                for end_scan in range(idx + 1, limit):
                    scan_kind = str(actions[end_scan].kind or "")
                    if scan_kind in ("if", "loop"):
                        depth += 1
                    elif scan_kind in ("end", "endif", "end_if", "end_loop", "end_block"):
                        if depth == 0:
                            return else_idx, end_scan
                        depth -= 1
                return else_idx, limit
        return None, limit

    @staticmethod
    def _find_block_end_index(actions: Sequence[ActionStep], start_idx: int, end_idx: Optional[int] = None) -> int:
        limit = len(actions) if end_idx is None else max(0, min(len(actions), int(end_idx)))
        depth = 0
        for idx in range(int(start_idx) + 1, limit):
            kind = str(actions[idx].kind or "")
            if kind in ("if", "loop"):
                depth += 1
            elif kind in ("end", "endif", "end_if", "end_loop", "end_block"):
                if depth == 0:
                    return idx
                depth -= 1
        return limit

    @staticmethod
    def _paste_text_for_user(step: ActionStep, uid: str) -> str:
        if bool(step.paste_per_user):
            uid_s = str(uid or "").strip()
            if uid_s:
                for value_uid, value_text in tuple(step.paste_user_values or ()):
                    if str(value_uid) == uid_s:
                        return str(value_text if value_text is not None else "")
        return str(step.text or "")

    def _webhook_message_for_step(self, step: ActionStep, uid: str, rule_name: str) -> str:
        template = str(step.webhook_message or "").strip()
        if not template:
            template = "Auto-Actions webhook step reached for {username}."

        uid_s = str(uid or "").strip()
        username = self._username(uid_s) if uid_s else ""
        server_label = self._server_label(uid_s) if uid_s else ""
        ps_link = self._ps_link(uid_s) if uid_s else ""
        discord_ping = self._discord_ping(uid_s) if uid_s else ""
        try:
            biome = str(self._biome_provider(uid_s) or "").strip() if uid_s else ""
        except Exception:
            biome = ""
        now = _dt.datetime.now().astimezone()
        replacements = {
            "user_id": uid_s,
            "uid": uid_s,
            "username": username or uid_s or "Unknown",
            "server": server_label,
            "server_label": server_label,
            "private_server": ps_link or server_label,
            "ps_link": ps_link,
            "discord_ping": discord_ping,
            "biome": biome,
            "action": str(rule_name or "").strip(),
            "row": str(rule_name or "").strip(),
            "step": str(step.name or "").strip(),
            "time": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "timestamp": now.isoformat(timespec="seconds"),
        }
        message = template
        for key, value in replacements.items():
            message = message.replace("{" + key + "}", str(value or ""))
        message = message[:2000].strip()
        return message or "Auto-Actions webhook step reached."

    def _send_webhook_step(self, step: ActionStep, uid: str, rule_name: str) -> None:
        url = str(step.webhook_url or "").strip()
        if not url:
            try:
                self._log(f"[Auto-Actions] {uid}: webhook step '{step.name}' skipped (missing URL).")
            except Exception:
                pass
            return

        payload = {"content": self._webhook_message_for_step(step, uid, rule_name)}
        try:
            threading.Thread(
                target=_post_webhook,
                args=(url, payload),
                daemon=True,
                name="AutoActionStepWebhook",
            ).start()
            try:
                self._log(f"[Auto-Actions] {uid}: webhook step '{step.name}' sent.")
            except Exception:
                pass
        except Exception:
            pass

    def _run_action_step(
        self,
        hwnd: int,
        step: ActionStep,
        *,
        click_delay: float,
        uid: str = "",
        rule_name: str = "",
    ) -> bool:
        if not self._condition_matches(hwnd, step.condition, click_delay=click_delay):
            time.sleep(max(0.01, float(click_delay)))
            return False

        if step.kind == "break":
            return True

        if step.kind == "webhook":
            self._send_webhook_step(step, uid, rule_name)
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "paste":
            if bool(step.select_all):
                _send_ctrl_a()
                time.sleep(0.03)
            _send_unicode_text(self._paste_text_for_user(step, uid))
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "key":
            _send_keys(step.keys or ([step.key] if step.key else []), step.key_hold_s)
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "wait":
            time.sleep(max(0.0, float(step.wait_s or 0.0)))
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.point is None:
            time.sleep(max(0.01, float(click_delay)))
            return False

        abs_xy = _abs_from_rel(hwnd, step.point)
        if not abs_xy:
            time.sleep(max(0.01, float(click_delay)))
            return False

        if step.kind == "scroll":
            direction = "up" if str(step.scroll_direction or "down").strip().lower() == "up" else "down"
            delta = 120 if direction == "up" else -120
            try:
                _mouse_move_instant(*abs_xy)
                time.sleep(0.02)
                ctypes.windll.user32.mouse_event(int(win32con.MOUSEEVENTF_WHEEL), 0, 0, int(delta), 0)
            except Exception:
                try:
                    if autoit is not None:
                        autoit.mouse_wheel(direction, 1)
                except Exception:
                    pass
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "drag":
            if step.end_point is None:
                time.sleep(max(0.01, float(click_delay)))
                return False
            end_xy = _abs_from_rel(hwnd, step.end_point)
            if not end_xy:
                time.sleep(max(0.01, float(click_delay)))
                return False
            _mouse_button_drag(
                hwnd,
                int(abs_xy[0]),
                int(abs_xy[1]),
                int(end_xy[0]),
                int(end_xy[1]),
                button=str(step.click_button or "left"),
                duration_s=float(step.drag_duration_s or 0.0),
            )
            time.sleep(max(0.03, float(click_delay)))
            return False

        _mouse_button_click(
            hwnd,
            *abs_xy,
            button=str(step.click_button or "left"),
            count=int(step.click_count or 1),
        )
        time.sleep(max(0.01, float(click_delay)))
        return False

    def _run_steps_once(
        self,
        hwnd: int,
        actions: Sequence[ActionStep],
        *,
        click_delay: float,
        uid: str = "",
        rule_name: str = "",
    ) -> bool:
        return self._run_steps_range(
            hwnd,
            actions,
            0,
            len(actions),
            click_delay=click_delay,
            uid=uid,
            rule_name=rule_name,
        )

    def _run_steps_range(
        self,
        hwnd: int,
        actions: Sequence[ActionStep],
        start_idx: int,
        end_idx: int,
        *,
        click_delay: float,
        uid: str = "",
        rule_name: str = "",
    ) -> bool:
        pc = 0
        pc = max(0, int(start_idx))
        end = max(0, min(len(actions), int(end_idx)))
        while pc < end:
            step = actions[pc]
            kind = str(step.kind or "")
            if kind == "if":
                else_idx, block_end = self._find_if_bounds(actions, pc, end)
                if self._condition_matches(hwnd, step.condition, click_delay=click_delay):
                    true_end = else_idx if else_idx is not None else block_end
                    if self._run_steps_range(
                        hwnd,
                        actions,
                        pc + 1,
                        true_end,
                        click_delay=click_delay,
                        uid=uid,
                        rule_name=rule_name,
                    ):
                        return True
                else:
                    if else_idx is not None:
                        if self._run_steps_range(
                            hwnd,
                            actions,
                            else_idx + 1,
                            block_end,
                            click_delay=click_delay,
                            uid=uid,
                            rule_name=rule_name,
                        ):
                            return True
                pc = block_end + 1
                continue
            if kind == "loop":
                block_end = self._find_block_end_index(actions, pc, end)
                if self._condition_matches(hwnd, step.condition, click_delay=click_delay):
                    for _ in range(max(1, int(step.loop_count or 1))):
                        if self._run_steps_range(
                            hwnd,
                            actions,
                            pc + 1,
                            block_end,
                            click_delay=click_delay,
                            uid=uid,
                            rule_name=rule_name,
                        ):
                            return True
                pc = block_end + 1
                continue
            if kind in ("else", "end", "endif", "end_if", "end_loop", "end_block"):
                pc += 1
                continue

            if self._run_action_step(hwnd, step, click_delay=click_delay, uid=uid, rule_name=rule_name):
                return True
            pc += 1
        return False

    def _run_rule_on_window(
        self,
        hwnd: int,
        rule: ActionRule,
        *,
        uid: str = "",
        click_delay: float,
        times: int,
        block_user_mouse_move: bool = False,
    ) -> None:
        with _window_topmost_during(hwnd), _block_user_mouse_movement_during_actions(
            bool(block_user_mouse_move),
            log_fn=self._log,
            notify_fn=self._mouse_block_notify,
        ):
            _bring_window_foreground(hwnd)
            _set_window_topmost(hwnd, True)
            time.sleep(max(0.02, float(click_delay) * 0.5))

            repeats = max(1, int(times or 1))
            for _ in range(repeats):
                if self._run_steps_once(
                    hwnd,
                    rule.actions,
                    click_delay=click_delay,
                    uid=uid,
                    rule_name=rule.name,
                ):
                    break

    def _execution_count(self, rule: ActionRule) -> int:
        if rule.repeat_mode == "count":
            return max(1, int(rule.repeat_count or 1))
        return 1

    def _prune_state_for_user(self, uid: str, valid_indices: set[int]) -> None:
        if not valid_indices:
            self._next_ready.pop(uid, None)
            self._pending_use_at.pop(uid, None)
            self._last_trigger_seq.pop(uid, None)
            self._completed_pids.pop(uid, None)
            self._rule_signature.pop(uid, None)
            return

        for mapping in (
            self._next_ready.get(uid),
            self._pending_use_at.get(uid),
            self._last_trigger_seq.get(uid),
            self._completed_pids.get(uid),
            self._rule_signature.get(uid),
        ):
            if not isinstance(mapping, dict):
                continue
            for idx in list(mapping.keys()):
                if int(idx) not in valid_indices:
                    mapping.pop(int(idx), None)

    def _evaluate_rules_for_user(
        self,
        uid: str,
        pid: int,
        biome: str,
        rules: Sequence[Tuple[int, ActionRule]],
    ) -> List[Tuple[int, ActionRule]]:
        now = time.time()
        due: List[Tuple[int, ActionRule]] = []
        alerts: List[ActionRule] = []

        with self._state_lock:
            valid = {int(idx) for idx, _rule in (rules or [])}
            self._prune_state_for_user(str(uid), valid)

            next_ready = self._next_ready.setdefault(str(uid), {})
            pending = self._pending_use_at.setdefault(str(uid), {})
            consumed = self._last_trigger_seq.setdefault(str(uid), {})
            completed = self._completed_pids.setdefault(str(uid), {})
            signatures = self._rule_signature.setdefault(str(uid), {})

            for idx, rule in rules:
                idx_i = int(idx)
                sig = hash(rule)
                if signatures.get(idx_i) != sig:
                    signatures[idx_i] = sig
                    next_ready.pop(idx_i, None)
                    pending.pop(idx_i, None)
                    consumed.pop(idx_i, None)
                    completed.pop(idx_i, None)

                if not rule.enabled or not rule.actions:
                    pending.pop(idx_i, None)
                    continue

                if idx_i in pending:
                    if now < float(pending.get(idx_i, 0.0)):
                        continue
                    pending.pop(idx_i, None)
                    if now < float(next_ready.get(idx_i, 0.0)):
                        continue
                    if not self._eligible_in_biome(biome, rule):
                        continue
                    if rule.repeat_mode == "once_per_pid" and int(pid or 0) in completed.get(idx_i, set()):
                        continue
                    due.append((idx_i, rule))
                    continue

                if rule.trigger_type != "ocr_filter" or not rule.trigger_filter_ids:
                    if now < float(next_ready.get(idx_i, 0.0)):
                        continue
                    if not self._eligible_in_biome(biome, rule):
                        continue
                    if rule.repeat_mode == "once_per_pid" and int(pid or 0) in completed.get(idx_i, set()):
                        continue

                    if rule.alert_enabled and rule.alert_webhook and float(rule.alert_lead_s) > 0.0:
                        pending[idx_i] = now + max(0.0, float(rule.alert_lead_s))
                        alerts.append(rule)
                        continue

                    due.append((idx_i, rule))
                    continue

                latest = self._latest_trigger_event(str(uid), rule)
                if latest is None:
                    continue

                seq = int(latest.get("seq", 0) or 0)
                if seq <= int(consumed.get(idx_i, 0) or 0):
                    continue

                consumed[idx_i] = seq

                if now < float(next_ready.get(idx_i, 0.0)):
                    continue
                if not self._eligible_in_biome(biome, rule):
                    continue
                if rule.repeat_mode == "once_per_pid" and int(pid or 0) in completed.get(idx_i, set()):
                    continue

                if rule.alert_enabled and rule.alert_webhook and float(rule.alert_lead_s) > 0.0:
                    pending[idx_i] = now + max(0.0, float(rule.alert_lead_s))
                    alerts.append(rule)
                    continue

                due.append((idx_i, rule))

        for rule in alerts:
            self._schedule_alert(str(uid), rule)
        return due

    def _run(self) -> None:
        self._log("[Auto-Actions] Engine started.")
        if autoit is None:
            self._log("[Auto-Actions] AutoIt is required for input. Auto-Actions disabled.")
            return

        while not self._stop.is_set():
            cfg = self._cfg_snapshot()
            enabled = bool(cfg.get("enabled", False))
            try:
                interval = max(0.2, float(cfg.get("tick_interval", 1.0) or 1.0))
            except Exception:
                interval = 1.0

            if not enabled:
                with self._state_lock:
                    self._pending_use_at.clear()
                self._stop.wait(timeout=interval)
                continue

            users = [str(uid).strip() for uid in (cfg.get("users") or []) if str(uid).strip()]
            click_delay = float(cfg.get("click_delay", 0.2) or 0.2)
            block_user_mouse_move = bool(cfg.get("disable_mouse_move", False))
            min_not_in_menu_s = 10.0

            if not users:
                with self._state_lock:
                    self._pending_use_at.clear()
                self._stop.wait(timeout=max(0.5, interval))
                continue

            did_any = False
            paused = False
            pause_denied = False

            try:
                for uid in users:
                    if self._stop.is_set():
                        break

                    try:
                        if not bool(self._cfg_snapshot().get("enabled", False)):
                            break
                    except Exception:
                        pass

                    try:
                        pid = self._pid_provider(uid)
                    except Exception:
                        pid = None
                    if not pid:
                        continue

                    try:
                        hwnd = self._hwnd_provider(int(pid))
                    except Exception:
                        hwnd = None
                    if not hwnd:
                        continue

                    try:
                        biome = self._biome_provider(uid) or ""
                    except Exception:
                        biome = ""

                    if not self._menu_gate_allows(uid, min_not_in_menu_s):
                        continue

                    rules = self._rules_from_cfg(cfg, uid=uid)
                    if not rules:
                        continue

                    due = self._evaluate_rules_for_user(uid, int(pid), biome, rules)
                    if not due:
                        continue

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

                    try:
                        used: List[Tuple[int, ActionRule]] = []
                        with self._action_lock:
                            for idx, rule in due:
                                self._run_rule_on_window(
                                    int(hwnd),
                                    rule,
                                    uid=str(uid),
                                    click_delay=click_delay,
                                    times=self._execution_count(rule),
                                    block_user_mouse_move=block_user_mouse_move,
                                )
                                used.append((idx, rule))
                        if used:
                            self._mark_used(uid, int(pid), used)
                            did_any = True
                            self._log(
                                f"[Auto-Actions] {uid}: played "
                                + ", ".join(str(rule.name) for _idx, rule in used)
                                + (f" (biome={biome})" if biome else "")
                            )
                    except Exception as e:
                        self._log(f"[Auto-Actions] {uid}: error during action playback: {e}")
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

            try:
                if not bool(self._cfg_snapshot().get("enabled", False)):
                    continue
            except Exception:
                pass

            sleep_for = max(0.2, interval if did_any else min(2.0, interval))
            self._stop.wait(timeout=sleep_for)

        self._log("[Auto-Actions] Engine stopped.")

    def test_once(self, uid: str, row_indices: Optional[Sequence[int]] = None) -> bool:
        if autoit is None:
            self._log("[Auto-Actions] Test: AutoIt is required for input.")
            return False

        cfg = self._cfg_snapshot()
        rules = self._rules_from_cfg(cfg, uid=uid)
        if row_indices is not None:
            wanted = {int(idx) for idx in row_indices}
            rules = [(idx, rule) for idx, rule in rules if int(idx) in wanted]
        if not rules:
            self._log("[Auto-Actions] Test: no actions configured. Add or select at least one action row.")
            return False

        try:
            pid = self._pid_provider(str(uid))
        except Exception:
            pid = None
        if not pid:
            self._log("[Auto-Actions] Test: could not resolve PID for selected user.")
            return False

        try:
            hwnd = self._hwnd_provider(int(pid))
        except Exception:
            hwnd = None
        if not hwnd:
            self._log("[Auto-Actions] Test: could not resolve Roblox window handle for selected user.")
            return False

        if self._in_menu_provider is not None:
            try:
                in_menu = self._in_menu_provider(str(uid))
            except Exception:
                in_menu = None
            if in_menu is None or bool(in_menu):
                self._log("[Auto-Actions] Test: user appears to be in the main menu (or status unknown); skipping.")
                return False

        click_delay = float(cfg.get("click_delay", 0.2) or 0.2)
        block_user_mouse_move = bool(cfg.get("disable_mouse_move", False))

        paused = False
        try:
            if self._pause_antiafk:
                try:
                    paused = bool(self._pause_antiafk() is not False)
                except Exception:
                    paused = False

            self._log(f"[Auto-Actions] Test: running {len(rules)} selected action row(s) once for {uid}...")
            with self._action_lock:
                for _idx, rule in rules:
                    self._run_rule_on_window(
                        int(hwnd),
                        rule,
                        uid=str(uid),
                        click_delay=click_delay,
                        times=1,
                        block_user_mouse_move=block_user_mouse_move,
                    )
            self._log("[Auto-Actions] Test: complete.")
            return True
        except Exception as e:
            self._log(f"[Auto-Actions] Test: error: {e}")
            return False
        finally:
            if paused and self._resume_antiafk:
                try:
                    self._resume_antiafk()
                except Exception:
                    pass
