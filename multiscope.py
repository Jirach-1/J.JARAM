# multiscope.py — MultiScopeEngine with strict switching + live cache refresh
# • STRICT: only switch to logs that contain the username marker (no guessing)
# • Watchdog observer refreshes the username→log cache immediately on changes
# • Immediate strict re-resolve for affected users (no 60s TTL wait)
# • 1s jittered fallback refresher (active users every tick; idle round-robin)
# • Anti-flap guard: only switch if candidate log is strictly newer by mtime
# • Biome detection from [BloxstrapRPC] JSON (largeImage.hoverText)
# • Merchant detection independent of biomes
# • Embeds: 4 rows (Account / Detected by / Time / Private Server)
# • Biome Started includes PS link; Biome Ended shows PS label only
# • Handoff: previous biome “Ended” carried donor → spare

from __future__ import annotations

import os, re, json, time, threading, requests
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

# IMPORTANT: we keep strictness; cache is refreshed on demand
from log_utils import find_log_for_username, refresh_username_log_map

# Optional biomes metadata (color, thumbnail). Fallbacks if missing.
# Optional biomes metadata (color, thumbnail). Fallbacks if missing.
try:
    from biomes import load_biomes_catalog, biome_meta, biome_duration
    load_biomes_catalog()
except Exception:
    def biome_meta(name: str):
        return (0x3BA55D, None)
    def biome_duration(name: str):
        return None
# ── optional watcher deps ─────────────────────────────────────────────────────
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except Exception:
    Observer = None
    class FileSystemEventHandler:  # type: ignore
        pass

APP_FOOTER = "J1's JARAM v1.2"
# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _post_webhook(url: str, payload: dict) -> None:
    if not url:
        return
    try:
        import requests
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

# Parse [BloxstrapRPC] JSON blobs strictly
_R_RPC_MARK = "[BloxstrapRPC]"
_R_JSON_START = re.compile(r"\{")

def _extract_rpc_jsons_from_text(text: str) -> List[dict]:
    out: List[dict] = []
    start = 0
    while True:
        i = text.find(_R_RPC_MARK, start)
        if i == -1:
            break
        m = _R_JSON_START.search(text, i)
        if not m:
            start = i + len(_R_RPC_MARK)
            continue
        j = m.start()

        depth = 0
        end = None
        for k in range(j, len(text)):
            ch = text[k]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end is None:
            # fall back line end
            end = text.find("\n", j)
            if end == -1:
                end = len(text)

        blob = text[j:end]
        try:
            out.append(json.loads(blob))
        except Exception:
            try:
                out.append(json.loads(blob.replace("'", '"')))
            except Exception:
                pass
        start = i + len(_R_RPC_MARK)
    return out

def _extract_biome_from_rpc(rpc: dict) -> Optional[str]:
    """STRICT: use data.largeImage.hoverText only."""
    if not isinstance(rpc, dict):
        return None
    data = rpc.get("data")
    if not isinstance(data, dict):
        return None
    li = data.get("largeImage")
    if not isinstance(li, dict):
        return None
    biome = li.get("hoverText")
    if isinstance(biome, str):
        biome = biome.strip()
    return biome or None



# Merchant lines — flexible but precise; timestamp anchored
# Merchant lines — tolerant to optional colon after [Merchant] and variable ms precision
MERCHANT_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{0,6})?Z),"  # allow 0–6 ms digits
    r"[^\n]*?\[(?:Merchant|Merchants)\]:?\s*"                               # optional colon after [Merchant]
    r"(?P<merchant_name>Jester|Mari)\b"
    r"[^\n]*?\b(arrived|spawn(?:ed|ing)?|appeared)\b"
    r"[^\n]*"
    r")$",
    re.IGNORECASE | re.MULTILINE
)

# Biome RPC lines — anchor timestamp exactly like merchants
BIOME_RPC_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z)"
    r".*?\[BloxstrapRPC\]\s*(?P<json>\{.*?\})"
    r")$",
    re.IGNORECASE | re.MULTILINE
)


# ──────────────────────────────────────────────────────────────────────────────
# Blocker
# ──────────────────────────────────────────────────────────────────────────────

class _TempBlockSession(threading.Thread):
    """
    3-minute temp blocker for one 'finder' account.
    - Preloads a Selenium driver for the finder (cookie)
    - Tails their log for 'Player added: <name> <id>'
    - Blocks only names present in UltimaEditing\\lookup.json -> blocklist
    - Grows lookups/blocklist via Bloxlink reverse search
    - Unblocks any IDs we blocked when the window ends
    """
    GUILD_ID = "1371698242886307921"   # your server (can be moved to credentials if you prefer)
    WINDOW_SEC = 180
    HEADLESS = True

    def __init__(self, log_fn, uid: str, username: str, cookie: str):
        super().__init__(daemon=True)
        self._log = log_fn
        self.uid = str(uid)
        self.username = str(username or "").strip()
        self.cookie = cookie
        self._stop = False
        self._blocked_ids = set()
        self._seen_ids = set()

    # ---------- UltimaEditing files ----------
    @staticmethod
    def _ultima_dir() -> Path:
        base = os.environ.get("APPDATA") or ""
        p = Path(base) / "UltimaEditing" if base else Path.cwd() / "UltimaEditing"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def _lookup_path(cls) -> Path:
        return cls._ultima_dir() / "lookup.json"

    @classmethod
    def _cred_path(cls) -> Path:
        return cls._ultima_dir() / "credentials.json"

    @classmethod
    def _load_lookup(cls) -> dict:
        p = cls._lookup_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        return {"lookups": {}, "blocklist": []}

    @classmethod
    def _save_lookup(cls, obj: dict) -> None:
        p = cls._lookup_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    @classmethod
    def _bloxlink_key(cls) -> str:
        p = cls._cred_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
            return str(data.get("bloxlink_api_key") or "")
        except Exception:
            return ""

    # ---------- Decisions ----------
    @staticmethod
    def _in_blocklist(name: str, lk: dict) -> bool:
        bl = [str(x).lower() for x in (lk.get("blocklist") or [])]
        return name.lower() in bl

    @staticmethod
    def _in_lookups(name: str, lk: dict) -> bool:
        nm = name.lower()
        for arr in (lk.get("lookups") or {}).values():
            for item in arr or []:
                if str(item).lower() == nm:
                    return True
        return False

    @classmethod
    def _add_to_lookups(cls, lk: dict, discord_id: str, username: str) -> None:
        lookups = lk.setdefault("lookups", {})
        arr = lookups.setdefault(str(discord_id), [])
        if username not in arr and username.lower() not in [a.lower() for a in arr]:
            arr.append(username)

    @classmethod
    def _append_blocklist(cls, lk: dict, username: str) -> None:
        bl = lk.setdefault("blocklist", [])
        if username not in bl and username.lower() not in [b.lower() for b in bl]:
            bl.append(username)

    # ---------- Bloxlink reverse ----------
    def _bloxlink_reverse(self, roblox_id: str) -> str | None:
        """
        Map Roblox userId -> Discord ID via Bloxlink using *plain* Authorization header.
        Supports response shapes:
          - {"user":{"id":"..."}}
          - {"discordID":"..."} or {"discordId":"..."}
          - {"discordIDs":["..."]}    ← plural form (pick first)
        Returns Discord ID string or None. Logs status so you can verify it ran.
        """
        key = self._bloxlink_key()
        if not key:
            self._log("[TempBlock] Bloxlink key missing; reverse lookup skipped")
            return None

        guild_id = self.GUILD_ID
        url = f"https://api.blox.link/v4/public/guilds/{guild_id}/roblox-to-discord/{roblox_id}"
        headers = {"Authorization": key}

        try:
            r = requests.get(url, headers=headers, timeout=10)
            try:
                data = r.json()
            except Exception:
                data = None

            self._log(f"[TempBlock] Bloxlink → {r.status_code} for {roblox_id} (guild {guild_id})")
            if isinstance(data, dict) and data.get("error"):
                self._log(f"[TempBlock] Bloxlink error: {data.get('error')}")

            if r.status_code == 200 and isinstance(data, dict):
                # 1) {"user":{"id":"..."}}
                if isinstance(data.get("user"), dict) and data["user"].get("id"):
                    return str(data["user"]["id"])

                # 2) {"discordID":"..."} / {"discordId":"..."}
                if data.get("discordID"):
                    return str(data["discordID"])
                if data.get("discordId"):
                    return str(data["discordId"])

                # 3) {"discordIDs":["..."]}  ← plural form
                if isinstance(data.get("discordIDs"), list) and data["discordIDs"]:
                    return str(data["discordIDs"][0])

            # 204/404/400/etc → treat as no mapping
            return None
        except Exception as e:
            self._log(f"[TempBlock] Bloxlink exception: {e}")
            return None

    # ---------- Roblox API (id -> name) ----------
    @staticmethod
    def _roblox_name_for(user_id: str) -> str | None:
        try:
            r = requests.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=8)
            if r.status_code == 200:
                nm = (r.json() or {}).get("name")
                return str(nm) if nm else None
        except Exception:
            pass
        return None

    # ---------- Selenium via your utilities ----------
    def _make_driver(self):
        from utilities_tab import _make_driver   # lazy-import to avoid init cycles
        return _make_driver(self.cookie, headless=self.HEADLESS)

    def _block_id(self, driver, user_id: str) -> str:
        from utilities_tab import _block_user
        return _block_user(driver, user_id)

    def _unblock_id(self, driver, user_id: str) -> bool:
        # we unblock via BlockedUsers page using username-based DOM, same as your Unblocker
        from utilities_tab import (
            _BLOCKED_URL,
            _find_blocked_node_by_name,
            _fully_load_then_find_by_name,
            _unblock_display_node,
        )
        name = self._roblox_name_for(user_id)
        if not name:
            return False
        try:
            driver.get(_BLOCKED_URL)
            time.sleep(0.3)
            node = (_find_blocked_node_by_name(driver, name.lower(), quick_only=True)
                    or _fully_load_then_find_by_name(driver, name.lower()))
            if not node:
                return True  # already unblocked
            return bool(_unblock_display_node(driver, node))
        except Exception:
            return False

    # ---------- Tail the finder’s log ----------
    def _tail_new_players(self, f) -> list[tuple[str, str]]:
        """Read any new lines and return [(username, id), ...] that match."""
        found = []
        while True:
            at = f.tell()
            line = f.readline()
            if not line:
                f.seek(at)
                break
            if "Player added:" not in line:
                continue
            m = re.search(r"Player added:\s+([A-Za-z0-9_]+)\s+(\d+)", line)
            if m:
                uname = m.group(1)
                rid = m.group(2)
                found.append((uname, rid))
        return found

    # ---------- Main ----------
    def run(self):
        if not self.cookie or not self.username:
            self._log(f"[TempBlock] {self.uid}: missing cookie/username")
            return

        # Open driver up front (preload)
        try:
            driver = self._make_driver()
        except Exception as e:
            self._log(f"[TempBlock] {self.uid}: failed to start browser ({e})")
            return

        # Prepare log tail
        log_path = find_log_for_username(self.username.lower(), allow_fallback=False)
        if not log_path or not os.path.isfile(log_path):
            self._log(f"[TempBlock] {self.uid}: no log for '{self.username}'")
            try: driver.quit()
            except Exception: pass
            return

        try:
            f = open(log_path, "r", encoding="utf-8", errors="ignore")
        except Exception as e:
            self._log(f"[TempBlock] {self.uid}: cannot open log ({e})")
            try: driver.quit()
            except Exception: pass
            return

        # Seek to end so we only see new players after the spawn
        try:
            f.seek(os.path.getsize(log_path))
        except Exception:
            f.seek(0, os.SEEK_END)

        # Load lookup once; we’ll persist changes as they occur
        lookup = self._load_lookup()

        deadline = time.time() + self.WINDOW_SEC
        self._log(f"[TempBlock] {self.uid}: window OPEN ({self.WINDOW_SEC}s)")

        try:
            while time.time() < deadline and not self._stop:
                for uname, rid in self._tail_new_players(f):
                    if rid in self._seen_ids:
                        continue
                    self._seen_ids.add(rid)

                    # Decide action
                    if self._in_blocklist(uname, lookup):
                        res = self._block_id(driver, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] blocked @{uname} ({rid}) on {self.uid} → {res} [blocklist]")
                        else:
                            self._log(f"[TempBlock] failed blocking @{uname} ({rid}) on {self.uid} [blocklist]")
                        continue

                    if self._in_lookups(uname, lookup):
                        self._log(f"[TempBlock] @{uname} already mapped in lookups → skip")
                        continue

                    # Try to resolve via Bloxlink (log shows whether it actually ran)
                    self._log(f"[TempBlock] Bloxlink reverse lookup for @{uname} ({rid}) …")
                    d_id = self._bloxlink_reverse(rid)

                    if d_id:
                        self._add_to_lookups(lookup, d_id, uname)
                        self._save_lookup(lookup)
                        self._log(f"[TempBlock] @{uname} → Discord {d_id} (added to lookups)")
                        # No block if resolvable
                    else:
                        # New behavior: block NOW + remember in blocklist
                        self._append_blocklist(lookup, uname)
                        self._save_lookup(lookup)
                        self._log(f"[TempBlock] @{uname} → no Bloxlink match; added to blocklist and blocking now")
                        res = self._block_id(driver, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] blocked @{uname} ({rid}) on {self.uid} → {res} [unknown→blocklist]")
                        else:
                            self._log(f"[TempBlock] failed blocking @{uname} ({rid}) on {self.uid} [unknown→blocklist]")
                time.sleep(0.25)
        finally:
            # Close the log file regardless
            try: f.close()
            except Exception: pass

        # Unblock everyone we blocked during this window
        if self._blocked_ids:
            self._log(f"[TempBlock] {self.uid}: window CLOSED – unblocking {len(self._blocked_ids)} id(s)")
            for rid in list(self._blocked_ids):
                if self._unblock_id(driver, rid):
                    self._log(f"[TempBlock] unblocked {rid}")
                else:
                    self._log(f"[TempBlock] unblock failed {rid}")
        else:
            self._log(f"[TempBlock] {self.uid}: window CLOSED – nothing to unblock")

        try: driver.quit()
        except Exception: pass

# ──────────────────────────────────────────────────────────────────────────────
# Data
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ServerScope:
    key: str
    users: Set[str] = field(default_factory=set)
    last_biome: Optional[str] = None
    last_biome_ts: float = 0.0
    last_merchant: Optional[str] = None
    last_merchant_ts: float = 0.0
    events: int = 0

    # NEW: scheduling state
    next_tail_at: float = 0.0     # epoch seconds when this scope should be polled again
    poll_rot: int = 0             # round-robin index across users in this scope

@dataclass
class Cursor:
    path: Optional[str] = None
    pos: int = 0

# ──────────────────────────────────────────────────────────────────────────────
# Watch handler
# ──────────────────────────────────────────────────────────────────────────────

class _LogDirHandler(FileSystemEventHandler):
    """Notifies engine when files change in a watched directory (created/moved/modified)."""
    def __init__(self, engine: "MultiScopeEngine", dirpath: str):
        super().__init__()
        self.engine = engine
        self.dirpath = os.path.abspath(dirpath)

    def on_created(self, event):  self._hit(event)
    def on_moved(self, event):    self._hit(event)
    def on_modified(self, event): self._hit(event)

    def _hit(self, *_args, **_kwargs):
        import time, os
        now = time.time()
        last = self.engine._watch_cooldown_by_dir.get(self.dirpath, 0.0)
        if (now - last) < self.engine._watch_cooldown_sec:
            return  # drop noisy burst
        self.engine._watch_cooldown_by_dir[self.dirpath] = now

        # do the actual refresh
        self.engine._refresh_users_in_dir_immediate(self.dirpath)


# ──────────────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────────────

class MultiScopeEngine:
    def _normalized_server_key(self, uid: str) -> str:
        label = (self._get_server_label(uid) or "").strip()
        u = label.upper()
        if u.startswith("DISCONNECTED") or u.startswith("OFFLINE"):
            return "Disconnected"  # single, friendly pool name
        return label or "Unknown"

    def __init__(
        self,
        *,
        get_username: Callable[[str], str],
        get_server_label: Callable[[str], str],
        get_ps_link_for_user: Optional[Callable[[str], str]] = None,
        get_server_owner_for_user: Optional[Callable[[str], str]] = None,  # supplied by GUI
        get_cookie_for_user,            # ← NEW
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self._get_username = get_username
        self._get_server_label = get_server_label
        self._get_ps_link = get_ps_link_for_user or (lambda uid: "")
        self._get_owner = get_server_owner_for_user or (lambda uid: "")
        self._get_cookie_for_user = get_cookie_for_user   # ← NEW
        self._log = log_fn or (lambda _msg: None)

        self._cur: Dict[str, Cursor] = {}
        self._scopes: Dict[str, ServerScope] = {}

        # Handoff: donor_uid → spare_uid
        self._handoffs: Dict[str, str] = {}
        # Carry donor's last biome into spare to emit Ended
        self._handoff_prev_biome_for_spare: Dict[str, str] = {}

        # Per-user: skip first biome event after attaching a log (avoid stale spam)
        self._skip_first_event_by_uid: Set[str] = set()

        # Biome cadence per server
        self._biome_min_interval = 2.0
        self._last_biome_post_by_scope: Dict[str, float] = {}

        # Merchant cadence
        self._merchant_rate_limit = 15.0
        self._last_merchant_post = 0.0
        self._merchant_hook: str = ""
        self._merchant_filters = {"Jester": True, "Mari": True}
        self._ping_map = {"Jester": "", "Mari": ""}

        # Merchant dedupe per uid -> merchant -> last full line  (legacy; no longer used)
        self._first_merchant_scan_done: Set[str] = set()
        self._last_merchant_line_by_user: Dict[str, Dict[str, str]] = {}
                
        # Merchant last-post timestamp per scope → merchant → epoch seconds
        self._last_merchant_ts_by_scope: Dict[str, Dict[str, float]] = {}

        # Fallback refresher (Option B++)
        self._next_log_refresh = 0.0
        self._refresh_cursor = 0

        # Watchdog
        self._observer: Optional[Observer] = None
        self._watched_dirs: Set[str] = set()

        # Webhooks
        self._biome_webhooks: List[dict] = []

        self._lock = threading.Lock()
        
        # Debounce settings for watchdog events
        self._watch_cooldown_by_dir = {}   # dir -> last-hit-ts
        self._watch_cooldown_sec = 2.0     # ignore hits closer than this
        
        self._normpath_by_uid: Dict[str, str] = {}
        
        # NEW: per-biome notifier modes (biome -> "None" | "Message" | "Everyone")
        self._biome_modes: Dict[str, str] = {}

        self._temp_block_sessions = {}  # uid -> expiry epoch (simple gate)

        # init watcher if available
        if Observer is not None:
            try:
                self._observer = Observer()
                self._observer.daemon = True
                self._observer.start()
                self._log("[MultiScope] Watcher enabled.")
            except Exception:
                self._observer = None
                self._log("[MultiScope] Watcher failed to start; using timer refresh.")
        else:
            self._log("[MultiScope] watchdog not installed; using timer refresh only.")

    # ── Config ────────────────────────────────────────────────────────────────

    def configure_webhooks(
        self,
        biome_webhooks: List[dict],
        merchant_hook: str = "",
        enable_jester: bool = True,
        enable_mari: bool = True,
        jester_ping: str = "",
        mari_ping: str = "",
        merchant_rate_limit: float = 15.0,   # kept for backward-compat; ignored
        biome_min_interval: float = 2.0,
        # NEW:
        biome_modes: Optional[Dict[str, str]] = None,
    ) -> None:
        self._biome_webhooks = biome_webhooks or []
        self._merchant_hook = (merchant_hook or "").strip()
        self._merchant_filters = {"Jester": bool(enable_jester), "Mari": bool(enable_mari)}
        self._ping_map = {"Jester": jester_ping or "", "Mari": mari_ping or ""}
        # ↓↓↓ ignore merchant_rate_limit entirely (no cooldown)
        self._biome_min_interval = float(biome_min_interval or 2.0)
        # NEW: store modes (uppercased keys)
        self._biome_modes = {str(k).upper(): str(v) for k, v in (biome_modes or {}).items()}


    # ── Watcher plumbing ──────────────────────────────────────────────────────

    def _watch_dir_for_path(self, path: str) -> None:
        if not (self._observer and path):
            return
        try:
            dirpath = os.path.abspath(os.path.dirname(path))
            if dirpath and dirpath not in self._watched_dirs:
                handler = _LogDirHandler(self, dirpath)
                self._observer.schedule(handler, dirpath, recursive=False)
                self._watched_dirs.add(dirpath)
                self._log(f"[MultiScope] Watching dir: {dirpath}")
        except Exception:
            pass

    def _refresh_users_in_dir_immediate(self, dirpath: str) -> None:
        """Called by watcher thread – do a light pass and nudge the fast fallback."""
        import os
        with self._lock:
            # 1) DO NOT rebuild the cache here. Let the 1s fallback tick refresh it.
            #    (It already calls refresh_username_log_map() once per tick.)
            #    This avoids redundant scans & log spam.  # see _maybe_refresh_paths()

            # 2) Re-resolve users whose current log lives in this directory
            count = 0
            absdir = os.path.abspath(dirpath)
            for uid, cur in list(self._cur.items()):
                p = cur.path or ""
                if p and os.path.abspath(os.path.dirname(p)) == absdir:
                    self._resolve_current_log(uid, force=False)
                    count += 1

            # 3) Nudge the timer-based refresher to run ASAP (harmless, cheap)
            self._next_log_refresh = 0.0

            # 4) Only log when something changed, and throttle per-dir
            if count:
                self._throttled_log(
                    key=f"watch-refresh:{absdir}",
                    msg=f"[MultiScope] Watch hit → refreshed {count} user(s) for {dirpath}",
                    every=10.0,   # at most once every 10s per directory
                )



    # ── User mapping / logs ───────────────────────────────────────────────────

    def update_users(self, user_ids: List[str]) -> None:
        with self._lock:
            # remove stale
            for uid in list(self._cur.keys()):
                if uid not in user_ids:
                    self._cur.pop(uid, None)

            # ensure cursors exist and warm-scan
            for uid in user_ids:
                self._resolve_current_log(uid, force=True)
                cur = self._cur.get(uid)
                if cur and cur.path:
                    self._watch_dir_for_path(cur.path)
                self._warmstart_user_tail(uid)


    def _resolve_current_log(self, uid: str, *, force: bool = False) -> None:
        uname = (self._get_username(uid) or "").lower()
        if not uname:
            return

        # STRICT: rely on log_utils mapping (which we refresh on watcher hits)
        try:
            path = find_log_for_username(uname, allow_fallback=False)
        except Exception:
            return
        if not path:
            return

        cur = self._cur.get(uid) or Cursor()
        # NEW: canonicalize both sides and skip if unchanged
        new_np = os.path.normcase(os.path.abspath(path))
        cur_np = self._normpath_by_uid.get(uid)
        if not force and cur_np and new_np == cur_np:
            if cur.path:
                self._watch_dir_for_path(cur.path)
            return


        # Optional per-uid minimum interval between switches (prevents rapid flaps)
        last_sw = getattr(self, "_last_switch_ts", {}).get(uid, 0.0)
        now_t = time.time()
        min_switch_interval = 1.0
        if not force and (now_t - last_sw < min_switch_interval) and cur.path:
            if path == cur.path:
                return

        # Anti-flap guard: only switch if candidate is STRICTLY newer
        if cur.path and os.path.isfile(cur.path) and not force:
            if path == cur.path:
                # ensure watcher is set
                self._watch_dir_for_path(cur.path)
                return
            try:
                old_mtime = os.path.getmtime(cur.path)
            except Exception:
                old_mtime = 0.0
            try:
                new_mtime = os.path.getmtime(path)
            except Exception:
                new_mtime = 0.0
            if new_mtime <= old_mtime:
                return

        # Switch
        cur.path = path
        try:
            # seek to EOF so we only read NEW data from the new file
            cur.pos = os.path.getsize(path) if os.path.isfile(path) else 0
        except Exception:
            cur.pos = 0

        self._cur[uid] = cur
        # remember last switch time
        if not hasattr(self, "_last_switch_ts"):
            self._last_switch_ts = {}
        self._last_switch_ts[uid] = now_t

        # ensure watcher on the new directory
        self._watch_dir_for_path(cur.path)
        
        self._normpath_by_uid[uid] = new_np

        self._log(f"[MultiScope] switched log for {uname} → {os.path.basename(path)}")

    def _maybe_refresh_paths(self, status_by_uid: Dict[str, dict]) -> None:
        """
        Option B++ fallback: 1s cadence + jitter.
        - Rebuild username→log cache (cheap) so strict lookups see the latest file set
        - Refresh ALL 'active' users every tick
        - Round-robin idle users to sweep quickly without spikes
        - If status_by_uid is empty, operate on self._cur keys
        """
        now = time.time()
        if now < getattr(self, "_next_log_refresh", 0.0):
            return

        import random
        self._next_log_refresh = now + 1.0 + random.uniform(0.0, 0.25)

        # Rebuild the strict cache once per fallback tick
        try:
            refresh_username_log_map()
        except Exception:
            pass

        uids_all = list(status_by_uid.keys()) if status_by_uid else list(self._cur.keys())
        if not uids_all:
            return

        active_uids = [uid for uid in uids_all
                       if status_by_uid and ((status_by_uid.get(uid, {}).get("status") == "Active")
                                             or bool(status_by_uid.get(uid, {}).get("pids")))]
        idle_uids = [uid for uid in uids_all if uid not in active_uids]

        # refresh all actives immediately
        for uid in active_uids:
            self._resolve_current_log(uid)

        # round-robin idles
        if idle_uids:
            sweep_seconds = 3.0
            per_call = max(1, int(len(idle_uids) / sweep_seconds))
            cursor = getattr(self, "_refresh_cursor", 0)
            end = cursor + per_call
            for idx in range(cursor, end):
                uid = idle_uids[idx % len(idle_uids)]
                self._resolve_current_log(uid)
            self._refresh_cursor = (end % len(idle_uids))

    def _warmstart_user_tail(self, uid: str) -> None:
        cur = self._cur.get(uid)
        if not cur or not cur.path or not os.path.isfile(cur.path):
            return
        try:
            size_now = os.path.getsize(cur.path)
            with open(cur.path, "r", encoding="utf-8", errors="ignore") as f:
                if size_now > 2 * 1024 * 1024:
                    f.seek(size_now - 2 * 1024 * 1024)
                chunk = f.read()
        except Exception:
            return

        # merchant seed (no notify) → seed *scope* timestamps to avoid retro spam across users
        matches = list(MERCHANT_RE.finditer(chunk))
        if matches:
            scope_key = self._server_key_for(uid)
            self._last_merchant_ts_by_scope.setdefault(scope_key, {})
            for m in matches:
                try:
                    ts = datetime.fromisoformat(m.group("timestamp").replace("Z", "+00:00"))
                except Exception:
                    continue
                name = m.group("merchant_name").title()
                self._last_merchant_ts_by_scope[scope_key][name] = ts.timestamp()

        # mark this user as warmstarted so its first live read doesn’t post old lines
        self._first_merchant_scan_done.add(uid)

        # seed last biome for scope (no notify)
        rpcs = _extract_rpc_jsons_from_text(chunk)
        if rpcs:
            last = _extract_biome_from_rpc(rpcs[-1])
            if last:
                b = str(last).upper()
                key = self._server_key_for(uid)
                scope = self._scopes.setdefault(key, ServerScope(key))
                scope.users.add(uid)
                scope.last_biome = b
                scope.last_biome_ts = time.time()

    # ── Handoff ───────────────────────────────────────────────────────────────

    def begin_handoff(self, donor_uid: str, spare_uid: str) -> None:
        with self._lock:
            self._handoffs[donor_uid] = spare_uid
            donor_key = self._server_key_for(donor_uid)
            prev = (self._scopes.get(donor_key) or ServerScope(donor_key)).last_biome
            if prev:
                self._handoff_prev_biome_for_spare[spare_uid] = prev
            self._scope(donor_key).users.update({donor_uid, spare_uid})

    def complete_handoff(self, donor_uid: str) -> None:
        with self._lock:
            self._handoffs.pop(donor_uid, None)

    # ── Scope/owner helpers ───────────────────────────────────────────────────

    def _server_key_for(self, uid: str) -> str:
        return self._get_server_label(uid) or "Unknown"

    def _scope(self, key: str) -> ServerScope:
        s = self._scopes.get(key)
        if not s:
            s = ServerScope(key=key)
            self._scopes[key] = s
        return s

    def _resolve_owner(self, uid: str, server_label: str) -> str:
        """
        Prefer explicit owner callback; otherwise fall back to detector username.
        Kept simple so we don't disturb existing logic elsewhere.
        """
        owner = (self._get_owner(uid) or "").strip()
        if owner:
            return owner
        # fallback: current detecting user
        return (self._get_username(uid) or "Unknown").strip()

    def _maybe_start_temp_block(self, uid: str, reason: str):
        now = time.time()
        exp = self._temp_block_sessions.get(uid, 0)
        if exp > now:
            return  # already running recently

        username = (self._get_username(uid) or "").strip()
        cookie = (self._get_cookie_for_user(uid) or "").strip()
        if not username or not cookie:
            return

        self._log(f"[TempBlock] starting for {uid} ({username}) due to {reason}")
        t = _TempBlockSession(self._log, uid, username, cookie)
        t.start()
        # Gate re-entrancy slightly beyond the window (3min + 30s pad)
        self._temp_block_sessions[uid] = now + (_TempBlockSession.WINDOW_SEC + 30)

    # ── Embeds ────────────────────────────────────────────────────────────────

    def _build_biome_embed(
        self,
        *,
        event_type: str,      # "start" | "end"
        biome: str,
        owner_name: str,      # PS owner
        detected_by: str,
        server_label: str,
        ps_link: str,
        include_ps_link: bool,
        ts_epoch: Optional[float] = None,   # NEW: anchor to log time
    ) -> dict:
        title = f"🌍 {biome} Biome Started" if event_type == "start" else f"🌍 {biome} Biome Ended"
        color_int, thumb = biome_meta(biome)

        import time as _time
        import datetime as _dt
        unix = int((ts_epoch if ts_epoch is not None else _time.time()))
        iso  = _dt.datetime.fromtimestamp(unix, tz=_dt.timezone.utc).isoformat()

        if include_ps_link and ps_link:
            ps_line = f"**Private Server:** [Private Server Link]({ps_link})"
        else:
            ps_line = f"**Private Server:** `{server_label}`"

        # Long date + exact time WITH seconds
        ts_full = f"<t:{unix}:D> • <t:{unix}:T>"
        ts_rel = f"<t:{unix}:R>"

        description = (
            f"**Owner:** `{owner_name}`\n"
            f"**Detected by:** `{detected_by}`\n"
            f"**Time:** {ts_full}({ts_rel})\n"  # ← seconds included
            f"{ps_line}"
        )

        embed = {
            "title": title,
            "description": description,
            "color": color_int,
            "timestamp": iso,
        }
        if thumb:
            embed["thumbnail"] = {"url": thumb}

        # Copy merchant’s footer style, include server label
        embed["footer"] = {"text": f"{APP_FOOTER}  •  {server_label}"}
        return embed


    def _emit_biome_event(self, uid: str, server_key: str, biome: str, *, event_type: str, ts_epoch: Optional[float] = None) -> None:
        detector     = self._get_username(uid) or uid
        server_label = server_key
        owner        = self._resolve_owner(uid, server_label)
        ps_link      = self._get_ps_link(uid) or ""

        b = (biome or "").upper()
        mode = getattr(self, "_biome_modes", {}).get(b)
        if mode is None:
            if b in ("GLITCHED", "DREAMSPACE"):
                mode = "Everyone"
            elif b == "NORMAL":
                mode = "None"
            else:
                mode = "Message"
        if mode == "None":
            return

        embed = self._build_biome_embed(
            event_type=event_type,
            biome=b,
            owner_name=owner,
            detected_by=detector,
            server_label=server_label,
            ps_link=ps_link,
            include_ps_link=(event_type == "start"),
            ts_epoch=ts_epoch,  # ← anchor to log line’s timestamp
        )

        content = "@everyone" if (mode == "Everyone" and event_type == "start") else ""
        payload = {"content": content, "embeds": [embed]}

        posted_any = False
        for wh in self._biome_webhooks:
            url = (wh.get("url") or "").strip()
            if not url:
                continue
            allowed = wh.get("biomes") or []
            if allowed and b not in allowed:
                continue
            _post_webhook(url, payload)
            posted_any = True

        if posted_any:
            self._log(
                f"[MultiScope] BIOME {event_type.upper()} posted | biome={b} | server={server_key} | "
                f"by={detector} | ts={int(ts_epoch) if ts_epoch else '—'}"
            )

    def _emit_merchant(self, uid: str, who: str, event_time_utc: datetime, full_line: str) -> None:
        if not self._merchant_hook:
            return
        if not self._merchant_filters.get(who, True):
            return

        server_key   = self._server_key_for(uid)
        server_label = server_key
        detector     = self._get_username(uid) or uid
        owner        = self._resolve_owner(uid, server_label)
        ps_link      = self._get_ps_link(uid) or ""

        emojis = {"Jester": "🃏", "Mari": "🛍️"}
        colors = {"Jester": 0xA352FF, "Mari": 0xFF82AB}
        title  = f"{emojis.get(who,'📣')} {who} Has Arrived!"
        ts     = int(event_time_utc.timestamp())
        ts_full = f"<t:{ts}:D> • <t:{ts}:T>"
        ts_rel  = f"<t:{ts}:R>"

        desc = (
            f"**Owner:** `{owner}`\n"
            f"**Detected by:** `{detector}`\n"
            f"**Detected At:** {ts_full} ({ts_rel})\n"
            f"**Private Server:** " + (f"[Private Server Link]({ps_link})" if ps_link else "`N/A`")
        )

        payload = {"content": (self._ping_map.get(who, "") or ""), "embeds": [{
            "title": title,
            "description": desc,
            "color": colors.get(who, 0x7289DA),
            "timestamp": event_time_utc.isoformat(),
            "footer": {"text": f"{APP_FOOTER}  •  {server_label}"}
        }]}

        _post_webhook(self._merchant_hook, payload)
         # START TEMP BLOCK WINDOW for Jester
        if who == "Jester":
            self._maybe_start_temp_block(uid, "Jester")

        scope = self._scopes.setdefault(server_key, ServerScope(server_key))
        scope.last_merchant = who
        scope.last_merchant_ts = time.time()
        scope.users.add(uid)

    # ---- Cadence model -------------------------------------------------

    _MERCHANT_MIN_GAP = 18 * 60  # 18 minutes

    def _scope(self, key: str) -> ServerScope:
        return self._scopes.setdefault(key, ServerScope(key))

    def _choose_uid_for_scope(self, key: str) -> Optional[str]:
        scope = self._scope(key)
        members = sorted(u for u in scope.users if self._normalized_server_key(u) != "Disconnected")
        if not members:
            return None
        # Round-robin across users in the scope so we don't starve anyone
        idx = scope.poll_rot % len(members)
        scope.poll_rot = (scope.poll_rot + 1) % len(members)
        return members[idx]

    def _compute_scope_interval(self, scope: ServerScope) -> float:
        """
        Returns seconds until the next poll for this scope, based on:
        - Biome (NORMAL = hot, active biome = cooler; scale by remaining time if known)
        - Merchant window (tighten after >= 18 minutes since last merchant)
        """
        import time
        now = time.time()

        # Default bounds (clamped)
        MIN_IVL = 0.25   # never less than 250ms
        MAX_IVL = 6.00   # never more than 6s

        # Disconnected/Offline scopes: deprioritize hard
        if scope.key == "Disconnected":
            return 5.0

        biome = (scope.last_biome or "NORMAL").upper()
        color, _thumb = biome_meta(biome)  # keep warm (already imported)
        dur = biome_duration(biome) or 600  # default 10 min if unknown
        age = (now - scope.last_biome_ts) if scope.last_biome_ts else 0.0
        remaining = max(0.0, float(dur) - age)

        # Biome-driven base interval
        if biome == "NORMAL":
            base = 0.40    # poll very frequently when NORMAL to catch new spawns fast
        else:
            # Scale 1.2s..5.0s depending on how much time remains in the active biome
            # (when far from ending, poll less often; tighten again as it nears the end)
            rem_ratio = 0.0 if dur <= 0 else max(0.0, min(1.0, remaining / float(dur)))
            base = 1.20 + 3.80 * rem_ratio  # 1.2 → 5.0

        # Merchant window: if we’re past the minimum spawn gap, tighten polling
        m_age = (now - scope.last_merchant_ts) if scope.last_merchant_ts else 1e9
        if m_age >= self._MERCHANT_MIN_GAP:
            base *= 0.50   # tighten (more often)
        else:
            base *= 1.50   # relax (less often) while we’re still within the 18-min quiet

        # Clamp
        if base < MIN_IVL: base = MIN_IVL
        if base > MAX_IVL: base = MAX_IVL
        return float(base)

    # ── Tail one user ─────────────────────────────────────────────────────────

    def _tail_one(self, uid: str) -> None:
        if self._normalized_server_key(uid) == "Disconnected":
            return
        cur = self._cur.get(uid)
        if not cur or not cur.path or not os.path.isfile(cur.path):
            self._resolve_current_log(uid, force=True)
            cur = self._cur.get(uid)
            if not cur or not cur.path or not os.path.isfile(cur.path):
                return

        try:
            size_now = os.path.getsize(cur.path)
            if size_now < cur.pos:
                cur.pos = 0
            if size_now == cur.pos:
                return
            with open(cur.path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(cur.pos)
                chunk = f.read()
            cur.pos = size_now
        except Exception:
            return

        # Merchants (scope-level dedupe by timestamp window)
        matches = list(MERCHANT_RE.finditer(chunk))
        if matches:
            # newest instance per merchant in this chunk
            latest: Dict[str, dict] = {}
            for m in matches:
                try:
                    ts = datetime.fromisoformat(m.group("timestamp").replace("Z", "+00:00"))
                except Exception:
                    continue
                name = m.group("merchant_name").title()
                latest[name] = {"ts": ts, "line": m.group("full_line")}

            scope_key = self._server_key_for(uid)
            self._scopes.setdefault(scope_key, ServerScope(scope_key)).users.add(uid)

            if uid not in self._first_merchant_scan_done:
                # seed scope timestamps; skip emit once
                self._last_merchant_ts_by_scope.setdefault(scope_key, {})
                for k, v in latest.items():
                    self._last_merchant_ts_by_scope[scope_key][k] = v["ts"].timestamp()
                self._first_merchant_scan_done.add(uid)
            else:
                # emit only if not seen within the recent window (default 10s)
                for k, v in latest.items():
                    ts_epoch = v["ts"].timestamp()
                    if not self._scope_dedupe_merchant_ts(scope_key, k, ts_epoch, window=10.0):
                        self._emit_merchant(uid, k, v["ts"], v["line"])

        # Biomes (anchor to log line's ISO timestamp like merchants)
        matches = list(BIOME_RPC_RE.finditer(chunk))
        if matches:
            # take the latest biome event found in this chunk (by log timestamp)
            latest = None  # (ts_epoch, biome_name)
            for m in matches:
                # parse ISO timestamp from the line
                try:
                    ts = datetime.fromisoformat(m.group("timestamp").replace("Z", "+00:00"))
                except Exception:
                    continue

                # parse RPC JSON and pull biome name from largeImage.hoverText
                try:
                    rpc = json.loads(m.group("json"))
                    data = rpc.get("data") or {}
                    li = data.get("largeImage") or {}
                    biome_name = (li.get("hoverText") or "").strip()
                except Exception:
                    continue

                if not biome_name:
                    continue

                latest = (ts.timestamp(), biome_name)

            if latest:
                event_ts, biome = latest
                biome = str(biome).upper()

                server_key = self._server_key_for(uid)
                scope = self._scopes.setdefault(server_key, ServerScope(server_key))
                scope.users.add(uid)

                # carry donor's last biome into spare (handoff) just once
                prev = scope.last_biome
                if not prev and uid in self._handoff_prev_biome_for_spare:
                    prev = self._handoff_prev_biome_for_spare.pop(uid, None)

                if prev != biome:
                    last_post = self._last_biome_post_by_scope.get(server_key, 0.0)
                    if (event_ts - last_post) >= self._biome_min_interval:
                        # first, ENDED previous biome if it existed
                        if prev:
                            self._emit_biome_event(uid, server_key, prev, event_type="end", ts_epoch=event_ts)

                        # update scope to the new biome anchored to log time
                        scope.last_biome = biome
                        scope.last_biome_ts = event_ts
                        self._last_biome_post_by_scope[server_key] = event_ts

                        # STARTED new biome (suppress NORMAL)
                        if biome != "NORMAL":
                            self._emit_biome_event(uid, server_key, biome, event_type="start", ts_epoch=event_ts)
                            
                            # START TEMP BLOCK WINDOW for Dreamspace / Glitched
                            if biome in ("DREAMSPACE", "GLITCHED"):
                                self._maybe_start_temp_block(uid, f"Biome:{biome}")
                                
                        else:
                            self._log(f"[MultiScope] → NORMAL (start suppressed) | user={self._get_username(uid)} | server={server_key}")

        # keep scope membership fresh
        key = self._server_key_for(uid)
        self._scope(key).users.add(uid)


    # ── Public loop hooks ─────────────────────────────────────────────────────

    def tick(self, status_by_uid: Dict[str, dict]) -> None:
        with self._lock:
            # ensure scopes contain their current members
            for uid in list(self._cur.keys()):
                key = self._server_key_for(uid)
                if key:
                    self._scope(key).users.add(uid)

            # fallback refresher (1s jitter; includes cache refresh)
            self._maybe_refresh_paths(status_by_uid)

            # ---- SCHEDULER: poll by scope, not by user -----------------------
            import time
            now_t = time.time()

            # Compute a stable list of active scopes for this pass
            active_keys = sorted(self._scopes.keys())

            for key in active_keys:
                scope = self._scope(key)

                # Skip until due
                due = getattr(scope, "next_tail_at", 0.0)
                if now_t < due:
                    continue

                # Choose which user to read for this scope (round-robin)
                uid = self._choose_uid_for_scope(key)
                if not uid:
                    continue

                # Tail once
                self._tail_one(uid)

                # Schedule next poll for this scope
                interval = self._compute_scope_interval(scope)
                scope.next_tail_at = time.time() + interval

            # prune quiet, empty scopes (unchanged)
            now_t = time.time()
            for key, scope in list(self._scopes.items()):
                scope.users = {u for u in scope.users if self._server_key_for(u) == key}
                quiet = (now_t - max(scope.last_biome_ts, scope.last_merchant_ts, 0)) > 600
                if not scope.users and quiet:
                    self._scopes.pop(key, None)

    def snapshot(self) -> List[dict]:
        out: List[dict] = []
        now_t = time.time()
        for key, s in sorted(self._scopes.items(), key=lambda kv: kv[0]):
            out.append({
                "server": key,
                "users": sorted(list(s.users)),
                "last_biome": s.last_biome or "—",
                "biome_age": int(now_t - s.last_biome_ts) if s.last_biome_ts else None,
                "last_merchant": s.last_merchant or "—",
                "merchant_age": int(now_t - s.last_merchant_ts) if s.last_merchant_ts else None,
                "events": s.events,
            })
        return out
    
    def _throttled_log(self, key: str, msg: str, every: float = 10.0) -> None:
        import time
        if not hasattr(self, "_last_log_by_key"):
            self._last_log_by_key = {}
        now = time.time()
        last = self._last_log_by_key.get(key, 0.0)
        if (now - last) >= every:
            self._last_log_by_key[key] = now
            self._log(msg)
    
    def _scope_dedupe_merchant_ts(self, scope_key: str, merchant: str, ts_epoch: float, window: float = 2.0) -> bool:
        """
        Return True if this merchant was posted for this scope within `window` seconds of ts_epoch.
        Otherwise record the ts and return False.
        """
        d = self._last_merchant_ts_by_scope.setdefault(scope_key, {})
        last = d.get(merchant)
        if last is not None and abs(ts_epoch - last) <= window:
            return True
        d[merchant] = ts_epoch
        return False



