from __future__ import annotations

import logging
import os
import queue
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

import multiprocessing as mp


_READER_DEBUG = str(os.environ.get("JARAM_LOG_READER_DEBUG", "")).strip().lower() in {
    "1", "true", "yes", "on",
}
_READER_LOGGER = logging.getLogger("jaram.log_reader.process")


def _multiscope_worker_main(cmd_q: mp.Queue, out_q: mp.Queue) -> None:
    usernames_by_uid: Dict[str, str] = {}
    cookies_by_uid: Dict[str, str] = {}
    server_label_by_uid: Dict[str, str] = {}
    ps_link_by_uid: Dict[str, str] = {}
    owner_by_uid: Dict[str, str] = {}

    engine = None
    last_reader_health = ""
    next_reader_diagnostics_at = 0.0
    last_snapshot_signature = None
    last_statistics_signature = None
    pending_update_users: Optional[dict] = None
    tracked_user_ids: set[str] = set()
    pending_configure_webhooks: Optional[dict] = None
    pending_development_config: dict = {}
    pending_bes_event_holds: dict = {}
    service_waiters: dict = {}
    service_lock = threading.Lock()
    pending_record_ocr_merchants: List[tuple[str, str]] = []

    def _send(msg: dict) -> bool:
        try:
            out_q.put(msg)
            return True
        except Exception:
            return False

    def _service_notify(payload):
        return _send({"type": "service_request", "id": uuid.uuid4().hex,
                      "payload": payload, "notify_only": True})

    def _log(msg: object) -> None:
        try:
            _send({"type": "log", "message": str(msg)})
        except Exception:
            pass

    def _member_lookup(guild_id, user_id):
        request_id = uuid.uuid4().hex
        event = threading.Event()
        result = {}
        with service_lock:
            service_waiters[request_id] = (event, result)
        try:
            _send({"type": "service_request", "id": request_id,
                   "payload": {"method": "discord_member", "guild_id": guild_id, "user_id": user_id}})
            if not event.wait(10):
                return {"ok": False, "message": "Discord service unavailable."}
            return result
        finally:
            with service_lock:
                service_waiters.pop(request_id, None)

    try:
        from multiscope import MultiScopeEngine
    except Exception as e:
        _send({"type": "error", "message": f"Failed to import multiscope engine: {e!r}"})
        return

    def _get_username(uid: str) -> str:
        try:
            return str(usernames_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _get_cookie(uid: str) -> str:
        try:
            return str(cookies_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _get_server_label(uid: str) -> str:
        try:
            return str(server_label_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _get_ps_link(uid: str) -> str:
        try:
            return str(ps_link_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _get_owner(uid: str) -> str:
        try:
            return str(owner_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _update_users(cmd: dict, *, refresh_user_ids=()) -> None:
        nonlocal tracked_user_ids
        user_ids = [str(uid) for uid in (cmd.get("user_ids") or [])]
        user_ids_set = set(user_ids)
        refresh = set(refresh_user_ids)
        incoming_usernames = cmd.get("usernames")
        if isinstance(incoming_usernames, dict):
            normalized = {
                str(uid): str(name or "") for uid, name in incoming_usernames.items()
                if str(uid) in user_ids_set
            }
            refresh.update(
                uid for uid, name in normalized.items()
                if usernames_by_uid.get(uid) != name
            )
            usernames_by_uid.clear()
            usernames_by_uid.update(normalized)
        incoming_cookies = cmd.get("cookies")
        if isinstance(incoming_cookies, dict):
            cookies_by_uid.clear()
            cookies_by_uid.update({
                str(uid): str(cookie or "") for uid, cookie in incoming_cookies.items()
                if str(uid) in user_ids_set
            })
        for mapping in (usernames_by_uid, cookies_by_uid, server_label_by_uid,
                        ps_link_by_uid, owner_by_uid):
            for uid in list(mapping):
                if uid not in user_ids_set:
                    mapping.pop(uid, None)
        resume_state = cmd.get('resume_state')
        if isinstance(resume_state, dict) and resume_state:
            engine.update_users(user_ids, refresh_user_ids=refresh, resume_state=resume_state)
        else:
            engine.update_users(user_ids, refresh_user_ids=refresh)
        tracked_user_ids = user_ids_set

    def _snapshot_signature(rows: List[dict]) -> tuple:
        signature = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            menu_by_uid = row.get("in_menu_by_uid") or {}
            menu_ts_by_uid = row.get("last_menu_ts_by_uid") or {}
            signature.append(
                (
                    str(row.get("server_key") or row.get("server") or ""),
                    tuple(str(uid) for uid in (row.get("users") or [])),
                    row.get("in_menu"),
                    float(row.get("last_menu_ts", 0.0) or 0.0),
                    tuple(sorted((str(uid), value) for uid, value in menu_by_uid.items())),
                    tuple(sorted((str(uid), float(value or 0.0)) for uid, value in menu_ts_by_uid.items())),
                    str(row.get("last_biome") or row.get("biome") or ""),
                    str(row.get("last_merchant") or row.get("merchant") or ""),
                    int(row.get("events", 0) or 0),
                )
            )
        return tuple(signature)

    def _publish_snapshot(*, force: bool = False) -> List[dict]:
        nonlocal last_snapshot_signature
        if engine is None:
            return []
        rows = engine.snapshot()
        signature = _snapshot_signature(rows)
        if force or signature != last_snapshot_signature:
            last_snapshot_signature = signature
            _send({"type": "snapshot", "rows": rows})
        return rows

    def _publish_found_statistics(*, force: bool = False) -> None:
        nonlocal last_statistics_signature
        get_totals = getattr(engine, "get_found_stats_snapshot", None)
        if not callable(get_totals):
            return
        totals = get_totals()
        if not isinstance(totals, dict):
            return
        signature = (
            tuple(sorted((totals.get("biomes_total") or {}).items())),
            tuple(sorted((totals.get("merchants_total") or {}).items())),
        )
        if force or signature != last_statistics_signature:
            if _send({"type": "found_statistics", "snapshot": totals}):
                last_statistics_signature = signature

    def _poll_engine() -> bool:
        nonlocal last_reader_health, next_reader_diagnostics_at
        if engine is None or reader_paused:
            return False
        try:
            diagnostics = engine.poll_logs()
            _publish_found_statistics()
            events = engine.drain_events()
            if events:
                _send({"type": "events", "events": events})
            health = str(((diagnostics or {}).get("index") or {}).get("health") or "")
            now = time.monotonic()
            if health and (health != last_reader_health or now >= next_reader_diagnostics_at):
                last_reader_health = health
                next_reader_diagnostics_at = now + 5.0
                _send({"type": "reader_health", "health": health, "diagnostics": diagnostics})
                if _READER_DEBUG:
                    _READER_LOGGER.debug("reader health=%s diagnostics=%r", health, diagnostics)
            _publish_snapshot()
            return bool(int((diagnostics or {}).get("backlog_users", 0) or 0))
        except Exception as exc:
            if _READER_DEBUG:
                _READER_LOGGER.debug("autonomous log poll failed", exc_info=exc)
            return False

    reader_paused = False
    poll_due = True
    commands_since_poll = 0
    messages_since_poll = 0
    command_wait = 0.1
    next_queue_error_at = 0.0
    while True:
        if poll_due:
            backlog_pending = _poll_engine()
            command_wait = 0.01 if backlog_pending else 0.1
            commands_since_poll = 0
            messages_since_poll = 0
            poll_due = False
        try:
            # Give queued commands an actual batch, even when scheduling or
            # processing one command takes longer than the reader's 10ms gap.
            # A deadline alone could spend that gap on a single expired RPC and
            # repeatedly return to expensive reads before reaching account updates.
            cmd = cmd_q.get(timeout=command_wait if messages_since_poll == 0 else 0)
        except queue.Empty:
            poll_due = True
            continue
        except Exception as exc:
            now = time.monotonic()
            if now >= next_queue_error_at:
                next_queue_error_at = now + 30.0
                _send({"type": "error", "command_type": "command_queue",
                       "message": f"Could not read worker command: {type(exc).__name__}"})
            poll_due = True
            continue

        messages_since_poll += 1
        poll_due = messages_since_poll >= 256

        if not isinstance(cmd, dict):
            continue

        ctype = cmd.get("type")
        # A pause snapshot is the final reader state, rather than a point from
        # which more notifications can escape before the GUI shuts us down.
        if reader_paused and ctype not in {'rpc', 'shutdown', 'service_reply'}:
            continue
        try:
            if ctype == "rpc":
                deadline = cmd.get("deadline_mono")
                if deadline is not None and time.monotonic() >= float(deadline):
                    # Discarding expired work does not consume an engine-command
                    # slot. Bound raw messages as well so reads still advance.
                    continue
            commands_since_poll += 1
            poll_due = poll_due or commands_since_poll >= 32
            if ctype == "init":
                stats_path = cmd.get("stats_path")
                usernames_by_uid = {
                    str(k): str(v or "") for k, v in (cmd.get("usernames") or {}).items()
                }
                cookies_by_uid = {
                    str(k): str(v or "") for k, v in (cmd.get("cookies") or {}).items()
                }
                server_label_by_uid.clear()
                ps_link_by_uid.clear()
                owner_by_uid.clear()

                engine = MultiScopeEngine(
                    get_username=_get_username,
                    get_server_label=_get_server_label,
                    get_ps_link_for_user=_get_ps_link,
                    get_server_owner_for_user=_get_owner,
                    get_cookie_for_user=_get_cookie,
                    stats_path=str(stats_path) if stats_path else None,
                    log_fn=_log,
                    member_lookup=_member_lookup,
                    service_notify=_service_notify,
                )
                engine.configure_development(pending_development_config)
                engine.configure_bes_event_holds(pending_bes_event_holds)
                _publish_found_statistics(force=True)
                _send({"type": "ready"})

                # Apply any commands that arrived before init completed.
                try:
                    if pending_configure_webhooks:
                        engine.configure_webhooks(
                            biome_webhooks=pending_configure_webhooks.get("biome_webhooks") or [],
                            event_webhooks=pending_configure_webhooks.get("event_webhooks") or [],
                            disabled_log_events=pending_configure_webhooks.get("disabled_log_events") or [],
                            merchant_hook=str(pending_configure_webhooks.get("merchant_hook") or ""),
                            enable_jester=bool(pending_configure_webhooks.get("enable_jester", True)),
                            enable_mari=bool(pending_configure_webhooks.get("enable_mari", True)),
                            enable_rin=bool(pending_configure_webhooks.get("enable_rin", True)),
                            jester_ping=str(pending_configure_webhooks.get("jester_ping") or ""),
                            mari_ping=str(pending_configure_webhooks.get("mari_ping") or ""),
                            rin_ping=str(pending_configure_webhooks.get("rin_ping") or ""),
                            merchant_detection_mode=str(
                                pending_configure_webhooks.get("merchant_detection_mode") or "asset_id"
                            ),
                            disable_log_based_merchant_detection=bool(
                                pending_configure_webhooks.get("disable_log_based_merchant_detection", False)
                            ),
                            merchant_rate_limit=float(pending_configure_webhooks.get("merchant_rate_limit", 15.0) or 15.0),
                            biome_min_interval=float(pending_configure_webhooks.get("biome_min_interval", 2.0) or 2.0),
                            biome_modes=pending_configure_webhooks.get("biome_modes"),
                            skip_webhook_unknown_context=bool(
                                pending_configure_webhooks.get("skip_webhook_unknown_context", False)
                            ),
                            in_menu_none_timeout_seconds=float(
                                pending_configure_webhooks.get("in_menu_none_timeout_seconds", 120.0)
                                or 120.0
                            ),
                        )
                except Exception:
                    pass

                try:
                    if pending_update_users is not None:
                        _update_users(pending_update_users)
                        pending_update_users = None
                        _publish_snapshot(force=True)
                except Exception:
                    pass
                try:
                    if pending_record_ocr_merchants:
                        for uid, merchant in pending_record_ocr_merchants:
                            engine.record_ocr_merchant(str(uid or ""), str(merchant or ""))
                        pending_record_ocr_merchants.clear()
                except Exception:
                    pass

            elif ctype == "update_users":
                if engine is None:
                    pending_update_users = dict(cmd)
                    continue
                _update_users(cmd)
                _publish_snapshot(force=True)

            elif ctype == "service_reply":
                with service_lock:
                    waiter = service_waiters.get(cmd.get("id"))
                    if waiter:
                        event, result = waiter
                        result.update(cmd.get("result") or {"ok": False})
                        event.set()

            elif ctype == "configure_bes_event_holds":
                pending_bes_event_holds = dict(cmd.get("config") or {})
                if engine is not None:
                    engine.configure_bes_event_holds(pending_bes_event_holds)

            elif ctype == "configure_development":
                pending_development_config = dict(cmd.get("config") or {})
                if engine is not None:
                    engine.configure_development(pending_development_config)

            elif ctype == "configure_webhooks":
                if engine is None:
                    pending_configure_webhooks = dict(cmd)
                    continue
                engine.configure_webhooks(
                    biome_webhooks=cmd.get("biome_webhooks") or [],
                    event_webhooks=cmd.get("event_webhooks") or [],
                    disabled_log_events=cmd.get("disabled_log_events") or [],
                    merchant_hook=str(cmd.get("merchant_hook") or ""),
                    enable_jester=bool(cmd.get("enable_jester", True)),
                    enable_mari=bool(cmd.get("enable_mari", True)),
                    enable_rin=bool(cmd.get("enable_rin", True)),
                    jester_ping=str(cmd.get("jester_ping") or ""),
                    mari_ping=str(cmd.get("mari_ping") or ""),
                    rin_ping=str(cmd.get("rin_ping") or ""),
                    merchant_detection_mode=str(cmd.get("merchant_detection_mode") or "asset_id"),
                    disable_log_based_merchant_detection=bool(
                        cmd.get("disable_log_based_merchant_detection", False)
                    ),
                    merchant_rate_limit=float(cmd.get("merchant_rate_limit", 15.0) or 15.0),
                    biome_min_interval=float(cmd.get("biome_min_interval", 2.0) or 2.0),
                    biome_modes=cmd.get("biome_modes"),
                    skip_webhook_unknown_context=bool(cmd.get("skip_webhook_unknown_context", False)),
                    in_menu_none_timeout_seconds=float(
                        cmd.get("in_menu_none_timeout_seconds", 120.0) or 120.0
                    ),
                )

            elif ctype == "recover_user_log_tracking":
                if engine is None:
                    continue
                recovered = engine.recover_user_log_tracking(
                    str(cmd.get("uid") or ""),
                    process_created_at=cmd.get("process_created_at"),
                )
                if recovered:
                    _publish_snapshot(force=True)

            elif ctype == "import_state":
                if engine is None:
                    continue
                state = cmd.get("state") or {}
                if isinstance(state, dict) and engine.import_state(state):
                    _publish_snapshot(force=True)

            elif ctype == "begin_handoff":
                if engine is None:
                    continue
                engine.begin_handoff(str(cmd.get("donor_uid") or ""), str(cmd.get("spare_uid") or ""))

            elif ctype == "complete_handoff":
                if engine is None:
                    continue
                engine.complete_handoff(str(cmd.get("donor_uid") or ""))

            elif ctype == "record_ocr_merchant":
                if engine is None:
                    pending_record_ocr_merchants.append(
                        (str(cmd.get("uid") or ""), str(cmd.get("merchant") or ""))
                    )
                    continue
                engine.record_ocr_merchant(
                    str(cmd.get("uid") or ""),
                    str(cmd.get("merchant") or ""),
                )

            elif ctype == "tick":
                if engine is None:
                    continue
                status_by_uid = cmd.get("status") or {}
                refresh_user_ids = set()
                if isinstance(status_by_uid, dict):
                    for uid, st in status_by_uid.items():
                        u = str(uid)
                        if not isinstance(st, dict):
                            continue
                        server_label_by_uid[u] = str(st.get("server") or "")
                        ps_link_by_uid[u] = str(st.get("ps_link") or "")
                        owner_by_uid[u] = str(st.get("server_owner") or "")
                        if "username" in st:
                            username = str(st.get("username") or "")
                            if usernames_by_uid.get(u) != username:
                                refresh_user_ids.add(u)
                            usernames_by_uid[u] = username
                        if "cookie" in st:
                            cookies_by_uid[u] = str(st.get("cookie") or cookies_by_uid.get(u, ""))

                engine.tick(status_by_uid)
                # Heartbeats carry the complete live/grace roster and current
                # names. Reconcile here as well so a missed account-update
                # command cannot leave a running user permanently unmonitored.
                current_user_ids = set(getattr(engine, "_tracked_uids", tracked_user_ids))
                if set(status_by_uid) != current_user_ids or refresh_user_ids:
                    _update_users(
                        {"user_ids": list(status_by_uid)}, refresh_user_ids=refresh_user_ids
                    )
                rows = engine.snapshot()
                last_snapshot_signature = _snapshot_signature(rows)
                # A tick reply is also the command acknowledgement used by the
                # proxy to release its in-flight guard, so it must always send.
                _send({"type": "tick", "rows": rows, "events": engine.drain_events(),
                       "diagnostics": engine.reader_diagnostics()})

            elif ctype == "rpc":
                rid = cmd.get("id")
                method = str(cmd.get("method") or "")
                args = cmd.get("args") or []
                kwargs = cmd.get("kwargs") or {}
                ok = True
                result: Any = None
                err = ""
                try:
                    if engine is None:
                        raise RuntimeError("MultiScope process not initialized")
                    fn = getattr(engine, method, None)
                    if fn is None:
                        raise AttributeError(f"MultiScopeEngine has no method '{method}'")
                    if not isinstance(args, list):
                        args = list(args)
                    if not isinstance(kwargs, dict):
                        kwargs = {}
                    pause_reader = method == 'export_state' and bool(kwargs.pop('_pause_reader', False))
                    result = fn(*args, **kwargs)
                    if pause_reader:
                        reader_paused = True
                except Exception as e:
                    ok = False
                    err = str(e)
                _send({"type": "rpc", "id": rid, "ok": ok, "result": result, "error": err})

            elif ctype == "shutdown":
                try:
                    if engine is not None:
                        engine.shutdown()
                except Exception:
                    pass
                return

        except Exception as e:
            _send({"type": "error", "command_type": ctype,
                   "message": f"{e!r}", "traceback": traceback.format_exc()})


class MultiScopeProcessProxy:
    """
    Runs MultiScopeEngine in a dedicated child process to avoid slowing the main worker loop.
    Public API mirrors the in-process engine methods used by gui.py / found_stats.py.
    """

    def __init__(
        self,
        *,
        usernames_by_uid: Dict[str, str],
        cookies_by_uid: Dict[str, str],
        stats_path: str,
        log_fn: Optional[Callable[[str], None]] = None,
        service_handler: Optional[Callable[[dict], dict]] = None,
        resume_state: Optional[dict] = None,
    ):
        self._log = log_fn or (lambda _msg: None)
        self._service_handler = service_handler
        self._service_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ms-service")
        self._ctx = mp.get_context("spawn")
        self._cmd_q: mp.Queue = self._ctx.Queue()
        self._out_q: mp.Queue = self._ctx.Queue()
        self._proc = self._ctx.Process(target=_multiscope_worker_main, args=(self._cmd_q, self._out_q), daemon=True)

        self._state_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._ready = False
        self._initial_resume_state = resume_state if isinstance(resume_state, dict) and resume_state else None
        self._last_snapshot: List[dict] = []
        self._found_statistics: Optional[dict] = None
        self._found_statistics_mono = 0.0
        self._events: List[tuple[str, str, str]] = []
        self._reader_health: str = "degraded"
        self._reader_diagnostics: dict = {}
        self._last_message_mono = 0.0
        self._last_message_type = ""
        self._reader_diagnostics_mono = 0.0
        self._rpc_replies: Dict[int, dict] = {}
        self._pending_rpc_ids: set[int] = set()
        self._next_rpc_id = 1
        self._tick_pending = False
        self._tick_sent_ts = 0.0
        self._next_tick_status: Optional[Dict[str, dict]] = None

        self._proc.start()
        self._send_cmd(
            {
                "type": "init",
                "stats_path": stats_path,
                "usernames": dict(usernames_by_uid or {}),
                "cookies": dict(cookies_by_uid or {}),
            }
        )

    def _send_cmd(self, msg: dict) -> bool:
        if getattr(self, "_closed", False) or (
            getattr(self, "_closing", False)
            and msg.get("type") not in {"shutdown", "service_reply"}
        ):
            return False
        try:
            if self._proc.is_alive():
                self._cmd_q.put(msg)
                return True
        except Exception:
            pass
        return False

    def _drain_out_queue(self, *, max_items: int = 200) -> None:
        logs: List[str] = []
        for _ in range(max_items):
            try:
                msg = self._out_q.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break

            if not isinstance(msg, dict):
                continue

            with self._state_lock:
                self._last_message_mono = time.monotonic()
                self._last_message_type = str(msg.get("type") or "")
            mtype = msg.get("type")
            if mtype == "service_request":
                self._service_executor.submit(self._answer_service_request, msg)
            elif mtype == "ready":
                with self._state_lock:
                    self._ready = True
            elif mtype == "log":
                try:
                    logs.append(str(msg.get("message") or ""))
                except Exception:
                    continue
            elif mtype == "tick":
                rows = msg.get("rows") or []
                evs = msg.get("events") or []
                with self._state_lock:
                    self._tick_pending = False
                    if isinstance(msg.get("diagnostics"), dict):
                        self._reader_diagnostics = dict(msg["diagnostics"])
                        self._reader_diagnostics_mono = time.monotonic()
                    if isinstance(rows, list):
                        self._last_snapshot = rows
                    if isinstance(evs, list):
                        for ev in evs:
                            try:
                                kind, uid, payload = ev
                                self._events.append((str(kind), str(uid), str(payload)))
                            except Exception:
                                continue
            elif mtype == "snapshot":
                rows = msg.get("rows") or []
                if isinstance(rows, list):
                    with self._state_lock:
                        self._last_snapshot = rows
            elif mtype == "found_statistics":
                totals = msg.get("snapshot")
                if isinstance(totals, dict):
                    with self._state_lock:
                        self._found_statistics = {
                            key: dict(value) if isinstance(value, dict) else value
                            for key, value in totals.items()
                        }
                        self._found_statistics_mono = time.monotonic()
            elif mtype == "events":
                evs = msg.get("events") or []
                if isinstance(evs, list):
                    with self._state_lock:
                        for ev in evs:
                            try:
                                kind, uid, payload = ev
                                self._events.append((str(kind), str(uid), str(payload)))
                            except Exception:
                                continue
            elif mtype == "reader_health":
                with self._state_lock:
                    self._reader_health = str(msg.get("health") or "degraded")
                    diag = msg.get("diagnostics")
                    self._reader_diagnostics = dict(diag) if isinstance(diag, dict) else {}
                    self._reader_diagnostics_mono = time.monotonic()
            elif mtype == "rpc":
                rid = msg.get("id")
                try:
                    rid_i = int(rid)
                except Exception:
                    continue
                with self._state_lock:
                    # Timed-out callers no longer own a reply slot. Discard
                    # late replies, which can include large exported states.
                    if rid_i in self._pending_rpc_ids:
                        self._rpc_replies[rid_i] = msg
            elif mtype == "error":
                if msg.get("command_type") == "tick":
                    with self._state_lock:
                        self._tick_pending = False
                try:
                    logs.append(f"[MultiscopeProcess] {msg.get('message')}")
                    tb = msg.get("traceback")
                    if tb:
                        logs.append(str(tb))
                except Exception:
                    continue

        for line in logs:
            if line:
                try:
                    self._log(line)
                except Exception:
                    pass
        self._flush_tick()

    def _flush_tick(self) -> None:
        with self._state_lock:
            status = getattr(self, "_next_tick_status", None)
            if getattr(self, "_closing", False) or status is None or not self._ready or self._tick_pending:
                return
            self._tick_pending = True
            self._tick_sent_ts = time.monotonic()
            if self._send_cmd({"type": "tick", "status": status}):
                self._next_tick_status = None
            else:
                self._tick_pending = False

    def _answer_service_request(self, msg: dict) -> None:
        try:
            result = self._service_handler(msg.get("payload") or {}) if self._service_handler else {"ok": False}
        except Exception:
            result = {"ok": False, "message": "Service request failed."}
        if not msg.get("notify_only"):
            self._send_cmd({"type": "service_reply", "id": msg.get("id"), "result": result})

    def configure_development(self, config: dict) -> None:
        self._send_cmd({"type": "configure_development", "config": config})

    def configure_bes_event_holds(self, config: dict) -> None:
        self._send_cmd({"type": "configure_bes_event_holds", "config": config})

    def _ensure_ready(self) -> bool:
        self._drain_out_queue()
        with self._state_lock:
            return bool(self._ready and self._proc.is_alive())

    def wait_ready(self, timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + float(timeout_s or 0.0)
        while time.monotonic() < deadline:
            if self._ensure_ready():
                return True
            time.sleep(0.01)
        return self._ensure_ready()

    def update_users(
        self,
        user_ids: List[str],
        usernames_by_uid: Optional[Dict[str, str]] = None,
        cookies_by_uid: Optional[Dict[str, str]] = None,
    ) -> bool:
        self._drain_out_queue()
        msg = {"type": "update_users", "user_ids": list(user_ids or [])}
        if usernames_by_uid is not None:
            msg["usernames"] = dict(usernames_by_uid or {})
        if cookies_by_uid is not None:
            msg["cookies"] = dict(cookies_by_uid or {})
        initial_state = getattr(self, '_initial_resume_state', None)
        if initial_state:
            msg['resume_state'] = initial_state
        accepted = self._send_cmd(msg)
        if accepted and initial_state:
            self._initial_resume_state = None
        return accepted

    def configure_webhooks(
        self,
        *,
        biome_webhooks: List[dict],
        merchant_hook: str = "",
        enable_jester: bool = True,
        enable_mari: bool = True,
        enable_rin: bool = True,
        jester_ping: str = "",
        mari_ping: str = "",
        rin_ping: str = "",
        merchant_detection_mode: str = "asset_id",
        disable_log_based_merchant_detection: bool = False,
        merchant_rate_limit: float = 15.0,
        biome_min_interval: float = 2.0,
        biome_modes: Optional[Dict[str, str]] = None,
        skip_webhook_unknown_context: bool = False,
        in_menu_none_timeout_seconds: float = 120.0,
        event_webhooks: Optional[List[dict]] = None,
        disabled_log_events: Optional[List[str]] = None,
    ) -> None:
        self._drain_out_queue()
        self._send_cmd(
            {
                "type": "configure_webhooks",
                "biome_webhooks": biome_webhooks or [],
                "event_webhooks": event_webhooks or [],
                "disabled_log_events": disabled_log_events or [],
                "merchant_hook": merchant_hook or "",
                "enable_jester": bool(enable_jester),
                "enable_mari": bool(enable_mari),
                "enable_rin": bool(enable_rin),
                "jester_ping": jester_ping or "",
                "mari_ping": mari_ping or "",
                "rin_ping": rin_ping or "",
                "merchant_detection_mode": str(merchant_detection_mode or "asset_id"),
                "disable_log_based_merchant_detection": bool(disable_log_based_merchant_detection),
                "merchant_rate_limit": float(merchant_rate_limit or 0.0),
                "biome_min_interval": float(biome_min_interval or 0.0),
                "biome_modes": biome_modes,
                "skip_webhook_unknown_context": bool(skip_webhook_unknown_context),
                "in_menu_none_timeout_seconds": float(in_menu_none_timeout_seconds),
            }
        )

    def begin_handoff(self, donor_uid: str, spare_uid: str) -> None:
        self._drain_out_queue()
        self._send_cmd({"type": "begin_handoff", "donor_uid": donor_uid, "spare_uid": spare_uid})

    def complete_handoff(self, donor_uid: str) -> None:
        self._drain_out_queue()
        self._send_cmd({"type": "complete_handoff", "donor_uid": donor_uid})

    def record_ocr_merchant(self, uid: str, merchant: str) -> None:
        self._drain_out_queue()
        uid_s = str(uid or "").strip()
        merchant_s = str(merchant or "").strip()
        if not uid_s or not merchant_s:
            return
        self._send_cmd({"type": "record_ocr_merchant", "uid": uid_s, "merchant": merchant_s})

    def tick(self, status_by_uid: Dict[str, dict]) -> None:
        # Keep one heartbeat in flight and one latest desired state. A delayed
        # reader must not accumulate old rosters every two seconds. Its reply
        # releases the next heartbeat even if the manager has not ticked again.
        with self._state_lock:
            self._next_tick_status = {
                str(uid): dict(row) for uid, row in (status_by_uid or {}).items()
            }
        self._drain_out_queue()

    def snapshot(self) -> List[dict]:
        self._drain_out_queue()
        with self._state_lock:
            return list(self._last_snapshot or [])

    def drain_events(self) -> List[tuple[str, str, str]]:
        self._drain_out_queue()
        with self._state_lock:
            ev = list(self._events)
            self._events.clear()
        return ev

    def reader_health(self) -> str:
        self._drain_out_queue()
        with self._state_lock:
            return str(self._reader_health or "degraded")

    def reader_diagnostics(self) -> dict:
        self._drain_out_queue()
        with self._state_lock:
            result = dict(self._reader_diagnostics or {})
            now = time.monotonic()
            result["process"] = {
                "pid": self._proc.pid,
                "alive": self._proc.is_alive(),
                "exit_code": self._proc.exitcode,
                "seconds_since_message": (max(0.0, now - self._last_message_mono)
                                          if self._last_message_mono else None),
                "last_message_type": self._last_message_type,
                "seconds_since_diagnostics": (max(0.0, now - self._reader_diagnostics_mono)
                                              if self._reader_diagnostics_mono else None),
                "tick_pending": self._tick_pending,
                "tick_wait_seconds": (max(0.0, now - self._tick_sent_ts) if self._tick_pending else 0.0),
                "latest_tick_waiting": self._next_tick_status is not None,
            }
            return result

    def diagnostics_snapshot(self) -> dict:
        return self.reader_diagnostics()

    def _rpc(self, method: str, *args: Any, timeout_s: float = 0.25, **kwargs: Any) -> Any:
        self._drain_out_queue()
        if not self._ensure_ready():
            raise RuntimeError("MultiScope process not ready")

        with self._state_lock:
            rid = int(self._next_rpc_id)
            self._next_rpc_id += 1
            self._pending_rpc_ids.add(rid)

        try:
            deadline = time.monotonic() + float(timeout_s or 0.0)
            if not self._send_cmd({"type": "rpc", "id": rid, "method": method,
                                   "args": list(args), "kwargs": dict(kwargs),
                                   "deadline_mono": deadline}):
                raise RuntimeError("MultiScope process unavailable")
            while time.monotonic() < deadline:
                self._drain_out_queue()
                with self._state_lock:
                    reply = self._rpc_replies.pop(rid, None)
                if reply is not None:
                    if bool(reply.get("ok", False)):
                        return reply.get("result")
                    raise RuntimeError(str(reply.get("error") or "RPC failed"))
                if not self._proc.is_alive():
                    raise RuntimeError('MultiScope process exited during state request')
                time.sleep(0.01)
            raise TimeoutError(f"MultiScope RPC timeout: {method}")
        finally:
            with self._state_lock:
                self._pending_rpc_ids.discard(rid)
                self._rpc_replies.pop(rid, None)

    def get_found_stats_snapshot(self) -> dict:
        # A busy log batch can exceed the RPC deadline. Totals are small and
        # published after each completed poll, so UI queries use the latest
        # published snapshot without waiting for another reader cycle.
        self._drain_out_queue()
        with self._state_lock:
            totals = getattr(self, "_found_statistics", None)
            if totals is not None:
                return {key: dict(value) if isinstance(value, dict) else value
                        for key, value in totals.items()}
        out = self._rpc("get_found_stats_snapshot", timeout_s=0.25)
        return out if isinstance(out, dict) else {}

    def get_biomes_found_counts(self, window_seconds: float) -> dict:
        out = self._rpc("get_biomes_found_counts", window_seconds, timeout_s=0.25)
        return out if isinstance(out, dict) else {"counts": {}, "total": 0, "window_seconds": window_seconds}

    def get_merchants_found_counts(self, window_seconds: float) -> dict:
        out = self._rpc("get_merchants_found_counts", window_seconds, timeout_s=0.25)
        return out if isinstance(out, dict) else {"counts": {}, "total": 0, "window_seconds": window_seconds}

    def recover_user_log_tracking(
        self,
        uid: str,
        process_created_at: Optional[float] = None,
    ) -> bool:
        uid_s = str(uid or "").strip()
        if not uid_s:
            return False
        try:
            out = self._rpc(
                "recover_user_log_tracking",
                uid_s,
                process_created_at=process_created_at,
                timeout_s=1.0,
            )
            return bool(out)
        except Exception:
            return False

    def recover_user_log_tracking_async(
        self,
        uid: str,
        process_created_at: Optional[float] = None,
    ) -> bool:
        """Queue recovery without stalling the manager heartbeat on an RPC."""
        uid_s = str(uid or "").strip()
        if not uid_s:
            return False
        self._drain_out_queue()
        try:
            if not self._proc.is_alive():
                return False
        except Exception:
            return False
        self._send_cmd(
            {
                "type": "recover_user_log_tracking",
                "uid": uid_s,
                "process_created_at": process_created_at,
            }
        )
        return True

    def export_state(self) -> dict:
        return self._export_state(pause_reader=False)

    def pause_and_export_state(self) -> dict:
        """Finish the active batch and freeze reading at the exported cursor."""
        return self._export_state(pause_reader=True)

    def _export_state(self, *, pause_reader: bool) -> dict:
        # Recent real reader batches exceed 90 seconds. Wait for the owner
        # thread to finish safely; a stale cached cursor could replay alerts.
        if not self.wait_ready(timeout_s=10.0):
            self._log('[MultiScope] Saved log attachments unavailable: reader not ready; '
                      'the next resume will require discovery.')
            return {}
        try:
            out = self._rpc("export_state", timeout_s=180.0, _pause_reader=pause_reader)
            return out if isinstance(out, dict) else {}
        except Exception as exc:
            self._log(f'[MultiScope] Saved log attachments unavailable: {type(exc).__name__}; '
                      'the next resume will require discovery.')
            return {}

    def import_state(self, state: dict) -> bool:
        # Same rationale as export_state: tolerate slower process startup and busy ticks.
        if not self.wait_ready(timeout_s=10.0):
            return False
        try:
            out = self._rpc("import_state", state, timeout_s=10.0)
            return bool(out)
        except Exception:
            return False

    def import_state_async(self, state: dict) -> bool:
        """Queue a resume import so manager readiness does not wait on log I/O."""
        if not isinstance(state, dict) or not state:
            return False
        self._drain_out_queue()
        try:
            if not self._proc.is_alive():
                return False
        except Exception:
            return False
        self._send_cmd({"type": "import_state", "state": state})
        return True

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._closed:
                return
            self._closing = True
            try:
                self._drain_out_queue()
                self._send_cmd({"type": "shutdown"})
                deadline = time.monotonic() + 30.0
                while self._proc.is_alive() and time.monotonic() < deadline:
                    self._drain_out_queue()
                    self._proc.join(timeout=0.05)
                self._drain_out_queue()
            except Exception:
                # A broken output pipe must not skip process/queue cleanup.
                pass
            finally:
                try:
                    if self._proc.is_alive():
                        self._proc.terminate()
                    self._proc.join(timeout=2.0)
                except Exception:
                    pass
                self._closed = True
                # The worker no longer reads commands. Joining a feeder with
                # buffered commands here (or at Python exit) can wait forever.
                for channel in (self._cmd_q, self._out_q):
                    try:
                        channel.cancel_join_thread()
                    finally:
                        channel.close()
                self._service_executor.shutdown(wait=True, cancel_futures=True)
