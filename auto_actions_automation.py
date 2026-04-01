"""
Auto Actions automation engine for JARAM.

This keeps the existing low-level click/paste helpers from the legacy
auto-item module, but switches the rule model over to named action strings
triggered by OCR filters.
"""

from __future__ import annotations

import copy
import ctypes
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import win32con

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


APP_FOOTER = "J.JARAM JX 2x27"


@dataclass(frozen=True)
class ActionStep:
    name: str
    kind: str
    point: Optional[RelPoint] = None
    text: str = ""
    color_hex: str = "#FFFFFF"
    tolerance: int = 0
    select_all: bool = True
    scroll_direction: str = "down"


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


def _normalize_rel_point(raw: Any) -> Optional[RelPoint]:
    if not isinstance(raw, dict):
        return None
    try:
        return RelPoint(float(raw.get("x", 0.0)), float(raw.get("y", 0.0)))
    except Exception:
        return None


def _normalize_action_step(raw: Any, *, fallback_name: str = "") -> Optional[ActionStep]:
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("type") or raw.get("kind") or raw.get("action_type") or "").strip().lower()
    if kind == "conditional":
        kind = "conditional_click"
    if kind not in ("click", "conditional_click", "paste", "scroll"):
        return None

    name = str(raw.get("name") or fallback_name or kind.replace("_", " ").title()).strip()
    point = _normalize_rel_point(raw.get("point"))

    try:
        tolerance = max(0, int(raw.get("tolerance", 0) or 0))
    except Exception:
        tolerance = 0

    try:
        select_all = bool(raw.get("select_all", True))
    except Exception:
        select_all = True

    scroll_direction = "up" if str(raw.get("scroll_direction") or raw.get("direction") or "").strip().lower() == "up" else "down"

    return ActionStep(
        name=name or kind.replace("_", " ").title(),
        kind=kind,
        point=point,
        text=str(raw.get("text") or ""),
        color_hex=str(raw.get("color") or raw.get("color_hex") or "#FFFFFF").strip() or "#FFFFFF",
        tolerance=tolerance,
        select_all=select_all,
        scroll_direction=scroll_direction,
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

    def _run_step(self, hwnd: int, step: ActionStep, *, click_delay: float) -> None:
        if step.kind == "paste":
            if bool(step.select_all):
                _send_ctrl_a()
                time.sleep(0.03)
            _send_unicode_text(str(step.text or ""))
            time.sleep(max(0.03, float(click_delay)))
            return

        if step.point is None:
            time.sleep(max(0.01, float(click_delay)))
            return

        abs_xy = _abs_from_rel(hwnd, step.point)
        if not abs_xy:
            time.sleep(max(0.01, float(click_delay)))
            return

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
            return

        if step.kind == "conditional_click":
            px = _screen_pixel_rgb(*abs_xy)
            if px is None:
                time.sleep(max(0.01, float(click_delay)))
                return
            if not _color_close(px, _hex_to_rgb(step.color_hex), int(step.tolerance or 0)):
                time.sleep(max(0.01, float(click_delay)))
                return

        _mouse_left_click(hwnd, *abs_xy)
        time.sleep(max(0.01, float(click_delay)))

    def _run_rule_on_window(
        self,
        hwnd: int,
        rule: ActionRule,
        *,
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
                for step in rule.actions:
                    self._run_step(hwnd, step, click_delay=click_delay)

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
