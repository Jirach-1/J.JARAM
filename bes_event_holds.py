"""Event-duration BES leases for biome and merchant finder accounts."""
from __future__ import annotations

import math
import threading
import time
import uuid

from biomes import biome_duration, biome_names

MERCHANTS = ("Jester", "Mari", "Rin")

_BRIDGE_LOCK = threading.Lock()


def trigger_catalog():
    return [(f"Biome:{name}", name, biome_duration(name)) for name in biome_names()] + [
        (f"Merchant:{name}", name, 180) for name in MERCHANTS
    ]


def normalize_event_holds(config):
    config = config if isinstance(config, dict) else {}
    events = config.get("events")
    events = events if isinstance(events, dict) else {}
    return {"enabled": bool(config.get("enabled", False)),
            "events": {str(key): bool(value) for key, value in events.items()}}


def migrate_event_holds(settings):
    bes = settings.setdefault("bes", {})
    legacy = settings.get("temp_block") or {}
    if not isinstance(bes, dict):
        bes = settings["bes"] = {}
    if not isinstance(legacy, dict):
        legacy = {}
    rules = legacy.get("rules") or {}
    if not isinstance(rules, dict):
        rules = {}
    if "event_holds" not in bes:
        events = {}
        for trigger, _name, duration in trigger_catalog():
            rule = rules.get(trigger)
            rule = rule if isinstance(rule, dict) else {}
            events[trigger] = bool(rule.get("hold_bes", legacy.get("hold_bes", False))) and bool(duration)
        was_active = any(selected and isinstance(rules.get(trigger), dict) and rules[trigger].get("enabled")
                         for trigger, selected in events.items())
        bes["event_holds"] = {"enabled": bool(legacy.get("enabled")) and was_active, "events": events}
    legacy.pop("hold_bes", None)
    for rule in rules.values():
        if isinstance(rule, dict):
            rule.pop("hold_bes", None)


class BESEventHoldController:
    """Own event windows independently of credentials and player access checks."""
    def __init__(self, engine, notify):
        self.engine = engine
        self.notify = notify
        self.config = normalize_event_holds({})
        self._lock = threading.RLock()
        self._sessions = {}

    def _context(self, uid):
        cursor = self.engine._cur.get(uid)
        return (str(getattr(cursor, "generation_id", "")), self.engine._server_key_for(uid),
                self.engine._process_generation_by_uid.get(uid))

    @staticmethod
    def _same_context(previous, current):
        return previous[:2] == current[:2] and (
            previous[2] is None or current[2] is None or previous[2] == current[2])

    def configure(self, config):
        config = normalize_event_holds(config)
        with self._lock:
            if config != self.config:
                self.shutdown()
                self.config = config

    def trigger(self, uid, trigger, event_ts):
        with self._lock:
            if not self.notify or not self.config["enabled"] or not self.config["events"].get(trigger):
                return
            category, _, name = trigger.partition(":")
            duration = (180 if name in MERCHANTS else None) if category == "Merchant" else (
                biome_duration(name) if category == "Biome" else None)
            if not duration or duration <= 0 or not math.isfinite(event_ts) or event_ts > time.time():
                return
            until = event_ts + duration
            if until <= time.time():
                return
            uid = str(uid)
            context = self._context(uid)
            session = self._sessions.get(uid)
            if session and not self._same_context(session["context"], context):
                self.release(uid)
                session = None
            if session is None:
                session = {"context": context, "client": BESHoldClient(uid, self.notify), "until": until}
                self._sessions[uid] = session
            session["context"] = context
            session["until"] = max(session["until"], until)
            session["client"].refresh(session["until"])

    def release(self, uid):
        with self._lock:
            session = self._sessions.pop(str(uid), None)
            if session:
                session["client"].release()

    def tick(self):
        with self._lock:
            for uid, session in list(self._sessions.items()):
                context = self._context(uid)
                if (session["until"] <= time.time() or uid not in self.engine._tracked_uids
                        or uid in self.engine._log_resolution_suspended_uids
                        or not self._same_context(session["context"], context)):
                    self.release(uid)
                else:
                    session["context"] = context
                    session["client"].refresh(session["until"])

    def shutdown(self):
        with self._lock:
            for uid in list(self._sessions):
                self.release(uid)


class BESHoldClient:
    """Queue leases without blocking MultiScope's log reader on a GUI reply."""
    def __init__(self, uid, notify):
        self.uid = str(uid)
        self.owner = f"bes_event:{self.uid}:{uuid.uuid4().hex}"
        self.notify = notify
        self._lock = threading.Lock()
        self._sequence = 0
        self._until = 0.0
        self._next_refresh = 0.0
        self._released = False

    @property
    def released(self):
        with self._lock:
            return self._released

    def refresh(self, until):
        with self._lock:
            if self._released or until <= time.time():
                return
            if until == self._until and time.monotonic() < self._next_refresh:
                return
            self._until = max(self._until, float(until))
            self._next_refresh = time.monotonic() + 2.0
            self._sequence += 1
            self.notify({"method": "bes_event_hold", "uid": self.uid, "owner": self.owner,
                         "sequence": self._sequence, "until": self._until})

    def release(self):
        with self._lock:
            if self._released:
                return
            self._released = True
            self._sequence += 1
            self.notify({"method": "bes_event_release", "uid": self.uid, "owner": self.owner,
                         "sequence": self._sequence, "until": self._until})


class BESHoldBridge:
    def __init__(self, gui):
        self.gui = gui
        self._lock = threading.RLock()
        self._owners = {}

    def _pids(self, uid):
        # Use the same account ownership information as Jaram's BES targeting.
        worker = getattr(self.gui, "worker_thread", None)
        manager = getattr(worker, "manager", None)
        tracker = getattr(manager, "process_tracker", None)
        tracked = getattr(tracker, "user_processes", None)
        if isinstance(tracked, dict):
            values = tracked.get(uid, []) or []
        else:
            values = ((getattr(self.gui, "user_data", {}) or {}).get(uid) or {}).get("pids", []) or []
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        pids = set()
        for value in values:
            try:
                pid = int(value)
            except (ValueError, TypeError):
                continue
            if pid > 0:
                pids.add(pid)
        return pids

    def _log(self, state, message):
        if state.get("message") != message:
            signal = getattr(self.gui, "bes_log_signal", None)
            if signal is not None:
                signal.emit(f"[BES Events] {message}")
            state["message"] = message

    @staticmethod
    def _release(ctl, pids, owner):
        if ctl is not None and pids:
            ctl.release_hold_many(sorted(pids), owner=owner)

    def handle(self, payload):
        uid, owner = str(payload.get("uid") or ""), str(payload.get("owner") or "")
        sequence = int(payload.get("sequence") or 0)
        until = float(payload.get("until") or 0.0)
        if not uid or not owner.startswith(f"bes_event:{uid}:") or sequence <= 0:
            return {"ok": False, "message": "Invalid BES hold request."}
        with self._lock:
            now = time.time()
            for token, old in list(self._owners.items()):
                if old["until"] + 60 < now:
                    self._owners.pop(token, None)
            state = self._owners.setdefault(owner, {"uid": uid, "sequence": 0, "until": until,
                                                    "pids": set(), "closed": False})
            if sequence <= state["sequence"] or (state["closed"] and payload["method"] == "bes_event_hold"):
                return {"ok": True, "ignored": True}
            state["sequence"] = sequence
            state["until"] = max(until, state["until"])
            ctl = getattr(self.gui, "bes_controller", None)
            if payload["method"] == "bes_event_release" or until <= now:
                state["closed"] = True
                self._release(ctl, state["pids"], owner)
                if state["pids"]:
                    self._log(state, f"BES hold released for {uid}")
                state["pids"] = set()
                return {"ok": True}
            if ctl is None or not bool((ctl.snapshot() or {}).get("enabled")):
                self._log(state, f"BES hold unavailable for {uid}: BES is not enabled")
                return {"ok": False}
            pids = self._pids(uid)
            self._release(ctl, state["pids"] - pids, owner)
            state["pids"] &= pids
            if not pids:
                self._log(state, f"BES hold waiting for the finder process for {uid}")
                return {"ok": False}
            # Absolute expiry prevents delayed IPC requests extending an event.
            remaining = max(0.0, until - time.time())
            if remaining <= 0:
                return {"ok": False}
            if ctl.hold_unthrottled_many(sorted(pids), remaining, owner=owner) is False:
                self._log(state, f"BES hold failed for {uid}")
                return {"ok": False}
            state["pids"] = pids
            state["applied_until"] = until
            self._log(state, f"BES hold active for {uid} until the event window ends")
            return {"ok": True}


def handle_service_request(gui, payload):
    with _BRIDGE_LOCK:
        bridge = getattr(gui, "_bes_event_bridge", None)
        if bridge is None:
            bridge = BESHoldBridge(gui)
            gui._bes_event_bridge = bridge
    return bridge.handle(payload)
