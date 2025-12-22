# multiscope.py - MultiScopeEngine with strict switching + live cache refresh
# STRICT: only switch to logs that contain the username marker (no guessing)
# Watchdog observer refreshes the username-log cache immediately on changes
# Immediate strict re-resolve for affected users (no 60s TTL wait)
# 1s jittered fallback refresher (active users every tick; idle round-robin)
# Anti-flap guard: only switch if candidate log is strictly newer by mtime
# Biome detection from [BloxstrapRPC] JSON (largeImage.hoverText)
# Merchant detection independent of biomes
# Embeds: 4 rows (Account / Detected by / Time / Private Server)
# Biome Started includes PS link; Biome Ended shows PS label only
# Handoff: previous biome Ended carried donor -> spare

from __future__ import annotations
import time as _t
import os, re, json, time, threading, requests
import requests.exceptions as _rq_exc
from typing import Optional, Dict
from selenium.common.exceptions import WebDriverException, UnexpectedAlertPresentException, NoAlertPresentException
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple, Any
from concurrent.futures import ThreadPoolExecutor
from log_utils import (
    find_log_for_username,
    refresh_username_log_map,
    R_DISC_REASON, R_DISC_NOTIFY, R_DISC_SENDING, R_CONN_LOST,  
)

# IMPORTANT: we keep strictness; cache is refreshed on demand
from log_utils import find_log_for_username, refresh_username_log_map

# Optional biomes metadata (color, thumbnail). Fallbacks if missing.
try:
    from biomes import load_biomes_catalog, biome_meta, biome_duration
    load_biomes_catalog()
except Exception:
    from typing import Tuple, Optional
    def biome_meta(name: str) -> Tuple[int, str]:
        return int(0x3BA55D), ""     # default color, empty thumbnail
    def biome_duration(name: str) -> Optional[int]:
        return None
# -- optional watcher deps -------------------------------------------------
try:
    from watchdog.observers import Observer as WDObserver
    from watchdog.events import FileSystemEventHandler as WDFileSystemEventHandler
except Exception:
    WDObserver = None  # type: ignore[assignment]

    # Use a different class name to avoid colliding with the alias
    class _FallbackFileSystemEventHandler:
        pass

    # Alias the fallback to the expected name for the rest of the file
    WDFileSystemEventHandler = _FallbackFileSystemEventHandler  # type: ignore[assignment]


APP_FOOTER = "J.JARAM JX 2x10"
_LOOKUP_SAVE_LOCK = threading.Lock()
# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

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


def _extract_in_menu_from_rpc(rpc: dict) -> Optional[bool]:
    """
    Return True if RPC state mentions the main menu, False if state exists and
    is not the main menu, None if state missing.
    """
    if not isinstance(rpc, dict):
        return None
    data = rpc.get("data")
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    if not isinstance(state, str):
        return None
    s = state.strip().lower()
    if not s:
        return None
    return "in main menu" in s



# Merchant lines - flexible but precise; timestamp anchored
# Merchant lines - tolerant to optional colon after [Merchant] and variable ms precision
MERCHANT_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{0,6})?Z),"  # allow 0-6 ms digits
    r"[^\n]*?\[(?:Merchant|Merchants)\]:?\s*"                               # optional colon after [Merchant]
    r"(?P<merchant_name>Jester|Mari)\b"
    r"[^\n]*?\b(arrived|spawn(?:ed|ing)?|appeared)\b"
    r"[^\n]*"
    r")$",
    re.IGNORECASE | re.MULTILINE
)

# Biome RPC lines - anchor timestamp exactly like merchants
BIOME_RPC_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z)"
    r".*?\[BloxstrapRPC\]\s*(?P<json>\{.*?\})"
    r")$",
    re.IGNORECASE | re.MULTILINE
)


# ------------------------------------------------------------------------------
# Blocker
# ------------------------------------------------------------------------------

class _TempBlockSession(threading.Thread):
    """
    3-minute temp blocker for one 'finder' account.
    - Preloads a Selenium driver for the finder (cookie)
    - Tails their log for 'Player added: <name> <id>'
    - Blocks only names present in Blank 
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
        self._pending: list[tuple[str, str]] = []  # (username, roblox_id) carried across ticks

    # ---------- Jaram files ----------
    @staticmethod
    def _jaram_dir() -> Path:
        base = os.environ.get("APPDATA") or ""
        p = Path(base) / "Jaram" if base else Path.cwd() / "Jaram"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def _lookup_path(cls) -> Path:
        return cls._jaram_dir() / "lookup.json"

    @classmethod
    def _cred_path(cls) -> Path:
        return cls._jaram_dir() / "credentials.json"

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
        # Use a unique tmp name per thread to avoid collisions, still guarded by a lock
        tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with _LOOKUP_SAVE_LOCK:
            tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
            # Atomic replace on Windows 10+ and modern Python
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
          - {"discordIDs":["..."]}    a plural form (pick first)
        Returns Discord ID string or None. Logs status so you can verify it ran.
        """
        key = self._bloxlink_key()
        if not key:
            self._log("[TempBlock] Bloxlink key missing; reverse lookup skipped")
            return None

        guild_id = self.GUILD_ID
        url = f"https://api.blox.link/v4/public/guilds/{guild_id}/roblox-to-discord/{roblox_id}"
        headers = {"Authorization": key}

        # 3 tries total, short timeouts, quick backoff on transient failures
        timeouts = (5, 5, 5)  # seconds per attempt
        backoffs = (0.5, 1) # between attempts
        for i, to in enumerate(timeouts):
            try:
                r = requests.get(url, headers=headers, timeout=to)
                status = r.status_code
                try:
                    data = r.json()
                except Exception:
                    data = None

                self._log(f"[TempBlock] Bloxlink - {status} for {roblox_id} (guild {guild_id})")

                if isinstance(data, dict) and data.get("error"):
                    self._log(f"[TempBlock] Bloxlink error: {data.get('error')}")

                if status == 200 and isinstance(data, dict):
                    # 1) {"user":{"id":"..."}}
                    if isinstance(data.get("user"), dict) and data["user"].get("id"):
                        return str(data["user"]["id"])
                    # 2) {"discordID":"..."} / {"discordId":"..."}
                    if data.get("discordID"):
                        return str(data["discordID"])
                    if data.get("discordId"):
                        return str(data["discordId"])
                    # 3) {"discordIDs":["..."]}
                    if isinstance(data.get("discordIDs"), list) and data["discordIDs"]:
                        return str(data["discordIDs"][0])
                    # 200 but unknown shape - treat as no mapping
                    return None

                # 204/404/400/etc - no mapping; don't retry
                if status not in (500, 502, 503, 504):   # only retry server errors
                    return None

            except (_rq_exc.Timeout, _rq_exc.ConnectionError) as e:
                self._log(f"[TempBlock] Bloxlink timeout/network error (try {i+1}/3): {e}")
            except Exception as e:
                self._log(f"[TempBlock] Bloxlink exception (try {i+1}/3): {e}")

            # retry if we have remaining attempts
            if i < len(backoffs):
                time.sleep(backoffs[i])

        # fell through without success
        self._log("[TempBlock] Bloxlink: giving up after retries")
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

    # ---------- Tail the finder's log ----------
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

        # Load lookup once; we'll persist changes as they occur
        lookup = self._load_lookup()

        deadline = time.time() + self.WINDOW_SEC
        self._log(f"[TempBlock] {self.uid}: window OPEN ({self.WINDOW_SEC}s)")

        try:
            while time.time() < deadline and not self._stop:
                # --- PRE-SWEEP: clear any surprise alerts before the heartbeat ---
                try:
                    a = driver.switch_to.alert
                    _ = a.text  # poke to ensure it's real
                    a.dismiss()
                    self._log(f"[TempBlock] {self.uid}: dismissed unexpected alert during heartbeat (pre-sweep)")
                except NoAlertPresentException:
                    pass

                # --- HEARTBEAT: fail fast if the driver is dead ---
                try:
                    driver.execute_script("return 1")
                except UnexpectedAlertPresentException:
                    # Alert popped between pre-sweep and heartbeat; dismiss and continue
                    try:
                        driver.switch_to.alert.dismiss()
                        self._log(f"[TempBlock] {self.uid}: dismissed alert after heartbeat exception")
                    except WebDriverException as e:
                        self._log(f"[TempBlock] {self.uid}: alert dismiss failed - {e}")
                        self._driver_dead = True
                        break
                except WebDriverException as e:
                    self._log(f"[TempBlock] {self.uid}: browser/WebDriver not reachable - {e.__class__.__name__}: {e}")
                    self._driver_dead = True
                    break

                # --- SURGE-AWARE PER-TICK LOGIC ---
                tick_deadline = time.time() + 0.10  # ~100ms budget per tick

                # merge any leftover work from the previous tick with fresh arrivals
                new_players = self._tail_new_players(f)
                batch = (self._pending + new_players)
                self._pending = []

                surge = len(batch) >= 20  # tune threshold as you like
                if surge:
                    self._log(f"[TempBlock] {self.uid}: SURGE mode ({len(batch)} new) - skipping Bloxlink")

                for uname, rid in batch:
                    # budget guard FIRST: if we're out of time, queue this work for next tick
                    if time.time() > tick_deadline:
                        self._pending.append((uname, rid))
                        continue

                    if rid in self._seen_ids:
                        continue

                    # only mark "seen" once we *know* we'll process this entry now
                    # (prevents losing users when we run out of budget)
                    # --- Known bad - block immediately ---
                    if self._in_blocklist(uname, lookup):
                        res = self._block_id(driver, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] blocked @{uname} ({rid}) on {self.uid} - {res} [blocklist]")
                        else:
                            self._log(f"[TempBlock] failed blocking @{uname} ({rid}) on {self.uid} [blocklist]")
                        self._seen_ids.add(rid)
                        continue

                    # --- Already mapped - skip ---
                    if self._in_lookups(uname, lookup):
                        self._log(f"[TempBlock] @{uname} already mapped in lookups - skip")
                        self._seen_ids.add(rid)
                        continue

                    if surge:
                        # SURGE path: skip Bloxlink; block now
                        self._append_blocklist(lookup, uname)
                        self._save_lookup(lookup)
                        self._log(f"[TempBlock] (SURGE) @{uname} - added to blocklist & blocking now")
                        res = self._block_id(driver, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] (SURGE) blocked @{uname} ({rid}) on {self.uid} - {res}")
                        else:
                            self._log(f"[TempBlock] (SURGE) failed blocking @{uname} ({rid}) on {self.uid}")
                        self._seen_ids.add(rid)
                        continue

                    # Normal path: Bloxlink quick try; else block+record
                    self._log(f"[TempBlock] Bloxlink reverse lookup for @{uname} ({rid})")
                    d_id = self._bloxlink_reverse(rid)

                    if d_id:
                        self._add_to_lookups(lookup, d_id, uname)
                        self._save_lookup(lookup)
                        self._log(f"[TempBlock] @{uname} - Discord {d_id} (added to lookups)")
                        # no block
                    else:
                        self._append_blocklist(lookup, uname)
                        self._save_lookup(lookup)
                        self._log(f"[TempBlock] @{uname} - no Bloxlink match; added to blocklist and blocking now")
                        res = self._block_id(driver, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] blocked @{uname} ({rid}) on {self.uid} - {res} [unknown-blocklist]")
                        else:
                            self._log(f"[TempBlock] failed blocking @{uname} ({rid}) on {self.uid} [unknown-blocklist]")

                    # we actually processed this entry this tick - safe to mark seen
                    self._seen_ids.add(rid)
                    
                time.sleep(0.25)

        except WebDriverException as e:
            # Catch any webdriver issues that bubble out of the loop
            self._log(f"[TempBlock] {self.uid}: browser crashed - {e.__class__.__name__}: {e}")
            self._driver_dead = True
        except Exception as e:
            self._log(f"[TempBlock] {self.uid}: loop crashed - {e!r}")
        finally:
            # Always close the log file
            try:
                f.close()
            except Exception:
                pass

        # Unblock everyone we blocked during this window
        if self._blocked_ids:
            self._log(f"[TempBlock] {self.uid}: window CLOSED - unblocking {len(self._blocked_ids)} id(s)")
            for rid in list(self._blocked_ids):
                if self._unblock_id(driver, rid):
                    self._log(f"[TempBlock] unblocked {rid}")
                else:
                    self._log(f"[TempBlock] unblock failed {rid}")
        else:
            self._log(f"[TempBlock] {self.uid}: window CLOSED - nothing to unblock")

        try: driver.quit()
        except Exception: pass

# ------------------------------------------------------------------------------
# Data
# ------------------------------------------------------------------------------


@dataclass
class ServerScope:
    key: str
    users: Set[str] = field(default_factory=set)
    last_biome: Optional[str] = None
    last_biome_ts: float = 0.0
    last_merchant: Optional[str] = None
    last_merchant_ts: float = 0.0
    in_menu: Optional[bool] = True  # default assume main menu until proven otherwise
    last_menu_ts: float = 0.0
    events: int = 0

    # NEW: scheduling state
    next_tail_at: float = 0.0     # epoch seconds when this scope should be polled again
    poll_rot: int = 0             # round-robin index across users in this scope

@dataclass
class Cursor:
    path: Optional[str] = None
    pos: int = 0
    carry: str = ""          


    path: Optional[str] = None
    pos: int = 0
    carry: str = ""          

# ------------------------------------------------------------------------------
# Watch handler
# ------------------------------------------------------------------------------

class _LogDirHandler(WDFileSystemEventHandler):
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


# ------------------------------------------------------------------------------
# Engine
# ------------------------------------------------------------------------------

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
        get_cookie_for_user,            # NEW
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self._get_username = get_username
        self._get_server_label = get_server_label
        self._get_ps_link = get_ps_link_for_user or (lambda uid: "")
        self._get_owner = get_server_owner_for_user or (lambda uid: "")
        self._get_cookie_for_user = get_cookie_for_user   # NEW
        self._log = log_fn or (lambda _msg: None)

        self._cur: Dict[str, Cursor] = {}
        self._scopes: Dict[str, ServerScope] = {}

        # Handoff: donor_uid - spare_uid
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
                
        # Merchant last-post timestamp per scope - merchant - epoch seconds
        self._last_merchant_ts_by_scope: Dict[str, Dict[str, float]] = {}

        # Fallback refresher (Option B++)
        self._next_log_refresh = 0.0
        self._refresh_cursor = 0

        # Watchdog
        self._observer: Optional[Any] = None
        self._watched_dirs: Set[str] = set()

        # Webhooks
        self._biome_webhooks: List[dict] = []

        self._lock = threading.Lock()
        # Events (thread-safe): GUI will drain these and act (e.g., recycle on disconnect)
        self._event_lock = threading.Lock()
        self._events: list[tuple[str, str, str]] = []   # (kind, uid, payload)

        # -- Tailer pool / concurrency -----------------------------------------
        self._max_workers = 32                      # tune: 24-32 recommended
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._inflight = set()                      # uids currently being tailed
        self._inflight_lock = threading.Lock()
        
        # NEW: dedicated webhook sender pool (keeps tailers non-blocking)
        self._send_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ms-send")

        # per-read cap to micro-batch bytes instead of reading all deltas at once
        self._per_read_cap = 256 * 1024    # 256 KiB per dequeue
        
        # Debounce settings for watchdog events
        self._watch_cooldown_by_dir = {}   # dir -> last-hit-ts
        self._watch_cooldown_sec = 2.0     # ignore hits closer than this
        
        self._normpath_by_uid: Dict[str, str] = {}
        self._menu_unknown_log_by_uid: Dict[str, str] = {}
        
        # NEW: per-biome notifier modes (biome -> "None" | "Message" | "Everyone")
        self._biome_modes: Dict[str, str] = {}
        # Raw user-provided biome modes (without enforced overrides).
        self._biome_modes_user: Dict[str, str] = {}
        # Latch relaxed state once found; avoid flapping back to locked.
        self._bm_relaxed: bool = False
        # Require confirmation before forcing locked mode.
        self._bm_lock_confirmed: bool = False
        # Track which biomes were forced to Everyone due to lock enforcement.
        self._lock_forced_biomes: Set[str] = set()

        self._temp_block_sessions = {}  # uid -> expiry epoch (simple gate)
        self._temp_block_disabled: bool = False

        # init watcher if available
        if WDObserver is not None:
            try:
                obs = WDObserver()
                obs.daemon = True
                obs.start()
                self._observer = obs
                self._log("[MultiScope] Watcher enabled.")
            except Exception:
                self._observer = None
                self._log("[MultiScope] Watcher failed to start; using timer refresh.")
        else:
            self._log("[MultiScope] watchdog not installed; using timer refresh only.")
    # -- Config ----------------------------------------------------------------

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
        lock_enforced = self._is_bm_lock_enforced()
        lock_disabled = not lock_enforced
        base_modes_raw: Dict[str, str] = {str(k).upper(): str(v) for k, v in (biome_modes or {}).items()}
        base_modes: Dict[str, str] = dict(base_modes_raw)
        forced_biomes: Set[str] = set()
        if lock_enforced:
            for hard in ("GLITCHED", "DREAMSPACE", "CYBERSPACE"):
                base_modes[hard] = "Everyone"
                forced_biomes.add(hard)

        normalized_hooks: List[dict] = []
        for wh in (biome_webhooks or []):
            if not isinstance(wh, dict):
                continue
            url = (wh.get("url") or "").strip()
            if not url:
                continue
            allowed_biomes = [
                str(b).upper() for b in (wh.get("biomes") or []) if str(b).strip()
            ]
            modes = {str(k).upper(): str(v) for k, v in (wh.get("biome_modes") or {}).items()}
            if not allowed_biomes and modes:
                allowed_biomes = [k for k, v in modes.items() if str(v).lower() in ("message", "everyone")]
            if not lock_disabled:
                for hard in ("GLITCHED", "DREAMSPACE", "CYBERSPACE"):
                    if modes.get(hard) != "Everyone":
                        modes[hard] = "Everyone"
            # NEW: user routing
            raw_users = wh.get("users", None)
            users_explicit = bool(wh.get("users_explicit", False))
            users: Optional[List[str]]
            if users_explicit and isinstance(raw_users, (list, tuple, set)):
                users = [str(u).strip() for u in raw_users if str(u).strip()]
            else:
                # None -> no user filter (all users allowed)
                users = None

            nh = {
                "url": url,
                "name": wh.get("name", ""),
                "biomes": allowed_biomes,
                "biome_modes": modes,
                "users": users,
            }
            if users is not None:
                nh["_user_lower"] = {u.lower() for u in users}
            normalized_hooks.append(nh)

        self._biome_webhooks = normalized_hooks
        self._merchant_hook = (merchant_hook or "").strip()
        self._merchant_filters = {"Jester": bool(enable_jester), "Mari": bool(enable_mari)}
        self._ping_map = {"Jester": jester_ping or "", "Mari": mari_ping or ""}
        # --- ignore merchant_rate_limit entirely (no cooldown)
        self._biome_min_interval = float(biome_min_interval or 2.0)
        self._biome_modes = base_modes
        self._biome_modes_user = base_modes_raw
        self._bm_relaxed = lock_disabled
        self._bm_lock_confirmed = not lock_disabled
        self._lock_forced_biomes = forced_biomes


    # -- Watcher plumbing ------------------------------------------------------

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
        """Called by watcher thread - do a light pass and nudge the fast fallback."""
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
                    msg=f"[MultiScope] Watch hit - refreshed {count} user(s) for {dirpath}",
                    every=10.0,   # at most once every 10s per directory
                )

    def _submit_tail(self, uid: str) -> bool:
        """Enqueue one tail read for `uid` if not already in flight."""
        with self._inflight_lock:
            if uid in self._inflight:
                return False
            self._inflight.add(uid)

        def _run():
            try:
                self._tail_one(uid)
            finally:
                with self._inflight_lock:
                    self._inflight.discard(uid)

        try:
            self._executor.submit(_run)
            return True
        except Exception:
            # Fall back to inline if executor is saturated/closing
            try:
                self._tail_one(uid)
            finally:
                with self._inflight_lock:
                    self._inflight.discard(uid)
            return True

    # -- User mapping / logs ---------------------------------------------------

    def update_users(self, user_ids: List[str]) -> None:
        try:
            refresh_username_log_map()  # build the strict map right now
        except Exception:
            pass
        with self._lock:
            # remove stale only
            for uid in list(self._cur.keys()):
                if uid not in user_ids:
                    self._cur.pop(uid, None)

        # Do resolves + watcher setup without holding the engine lock
        for uid in user_ids:
            self._resolve_current_log(uid, force=True)
            cur = self._cur.get(uid)
            if cur and cur.path:
                try:
                    self._seed_last_rpc_state(uid, cur.path)
                except Exception:
                    pass
                self._watch_dir_for_path(cur.path)

        # Do the I/O-heavy warmstarts last, also outside the lock
        for uid in user_ids:
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

        # Immediately seed menu/biome from existing content of the new log
        try:
            self._seed_last_rpc_state(uid, cur.path)
        except Exception:
            pass

        self._cur[uid] = cur
        # remember last switch time
        if not hasattr(self, "_last_switch_ts"):
            self._last_switch_ts = {}
        self._last_switch_ts[uid] = now_t

        # ensure watcher on the new directory
        self._watch_dir_for_path(cur.path)
        
        self._normpath_by_uid[uid] = new_np

        self._log(f"[MultiScope] switched log for {uname} - {os.path.basename(path)}")

    def _maybe_refresh_paths(self, status_by_uid: Dict[str, dict]) -> None:
        """
        Option B++ fallback: 1s cadence + jitter.
        - Rebuild username-log cache (cheap) so strict lookups see the latest file set
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
                window = 8 * 1024 * 1024  # read up to last 8 MiB for seeds
                if size_now > window:
                    f.seek(size_now - window)
                chunk = f.read()
        except Exception:
            return

        # merchant seed (no notify) +' seed *scope* timestamps to avoid retro spam across users
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

        # mark this user as warmstarted so its first live read doesn?t post old lines
        self._first_merchant_scan_done.add(uid)

        # seed last biome for scope (no notify) + do NOT set scope.last_biome,
        # so the first live RPC will emit a START normally.
        rpcs = _extract_rpc_jsons_from_text(chunk)
        if rpcs:
            last = _extract_biome_from_rpc(rpcs[-1])
            if last:
                key = self._server_key_for(uid)
                self._scope(key).users.add(uid)
            latest_state = None
            for rpc in rpcs:
                st = _extract_in_menu_from_rpc(rpc)
                if st is not None:
                    latest_state = st
            if latest_state is not None:
                key = self._server_key_for(uid)
                scope = self._scope(key)
                scope.in_menu = latest_state
                scope.last_menu_ts = time.time()
        else:
            # as a fallback, scan the tail of the file for the latest RPC to seed menu state
            self._seed_last_rpc_state(uid, cur.path)

    def _seed_last_rpc_state(self, uid: str, path: str) -> None:
        """Scan the tail of a log for the most recent BloxstrapRPC to seed menu/biome."""
        if not path or not os.path.isfile(path):
            return
        try:
            size_now = os.path.getsize(path)
            window = 16 * 1024 * 1024  # 16 MiB tail
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                if size_now > window:
                    f.seek(size_now - window)
                chunk = f.read()
        except Exception:
            return

        rpcs = _extract_rpc_jsons_from_text(chunk)
        if not rpcs and size_now <= window:
            # if small file and still none, try full file just in case
            try:
                chunk = Path(path).read_text(encoding="utf-8", errors="ignore")
                rpcs = _extract_rpc_jsons_from_text(chunk)
            except Exception:
                return
        if not rpcs:
            return

        latest_state: Optional[bool] = None
        latest_biome: Optional[str] = None
        for rpc in rpcs:
            st = _extract_in_menu_from_rpc(rpc)
            if st is not None:
                latest_state = st
            b = _extract_biome_from_rpc(rpc)
            if b:
                latest_biome = str(b).upper()

        key = self._server_key_for(uid)
        scope = self._scope(key)
        now_t = time.time()
        if latest_state is not None:
            scope.in_menu = latest_state
            scope.last_menu_ts = now_t
            self._clear_menu_unknown(uid)
        else:
            scope.in_menu = scope.in_menu if scope.in_menu is not None else True
        if latest_biome:
            scope.last_biome = latest_biome
            scope.last_biome_ts = now_t
        scope.users.add(uid)

    def _emit_event(self, kind: str, uid: str, payload: str = "") -> None:
        with self._event_lock:
            self._events.append((kind, uid, payload))

    def drain_events(self):
        with self._event_lock:
            ev = self._events[:]
            self._events.clear()
        return ev

    def _mark_menu_unknown(self, uid: str) -> None:
        try:
            key = self._server_key_for(uid)
            scope = self._scope(key)
            scope.in_menu = None
            scope.users.add(uid)
        except Exception:
            pass

        try:
            cur = self._cur.get(uid)
            if cur and cur.path:
                self._menu_unknown_log_by_uid[str(uid)] = os.path.abspath(cur.path)
        except Exception:
            pass

    def _clear_menu_unknown(self, uid: str) -> None:
        try:
            self._menu_unknown_log_by_uid.pop(str(uid), None)
        except Exception:
            pass

    def _scan_disconnect_in_chunk(self, uid: str, chunk: str) -> bool:
        """Check a just-read log chunk for Roblox disconnect signals."""
        if not chunk:
            return False
        if (R_DISC_REASON.search(chunk) or
            R_DISC_NOTIFY.search(chunk) or
            R_DISC_SENDING.search(chunk) or
            R_CONN_LOST.search(chunk)):
            self._emit_event("disconnect", uid, "detected in log")
            self._mark_menu_unknown(uid)
            return True
        return False

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

    # -- Scope/owner helpers ---------------------------------------------------

    def _server_key_for(self, uid: str) -> str:
        return self._get_server_label(uid) or "Unknown"

    def _scope(self, key: str) -> ServerScope:
        return self._scopes.setdefault(key, ServerScope(key))

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

    # -- Embeds ----------------------------------------------------------------

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
            f"**Time:** {ts_full}({ts_rel})\n"  # seconds included
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

        # Copy merchant's footer style, include server label
        embed["footer"] = {"text": f"{APP_FOOTER}  •  {server_label}"}
        return embed

    def _is_bm_relaxed(self) -> bool:
        try:
            if getattr(self, "_bm_relaxed", False):
                return True
            if os.environ.get("JARAM_UNLOCK", "").strip() == "1":
                return True

            candidates = [Path("JARAM.biu")]
            try:
                candidates.append(Path(__file__).resolve().with_name("JARAM.biu"))
            except Exception:
                pass
            try:
                import sys as _sys
                meipass = getattr(_sys, "_MEIPASS", None)
                if meipass:
                    candidates.append(Path(meipass) / "JARAM.biu")
            except Exception:
                pass

            for p in candidates:
                try:
                    if p.exists():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _is_bm_lock_enforced(self) -> bool:
        """
        Returns True when the biome lock should be enforced (force Everyone on hard biomes).
        We double-check before locking to avoid accidental flips when the sentinel exists.
        """
        try:
            if self._is_bm_relaxed():
                # If we were previously locked, clear the confirmation so we can relax.
                if getattr(self, "_bm_lock_confirmed", False):
                    self._bm_lock_confirmed = False
                return False

            if getattr(self, "_bm_lock_confirmed", False):
                return True

            # Second pass before enforcing lock to avoid flapping on transient misses.
            if self._is_bm_relaxed():
                self._bm_lock_confirmed = False
                return False

            self._bm_lock_confirmed = True
            return True
        except Exception:
            # Fail closed if anything unexpected happens.
            return True


    def _emit_biome_event(self, uid: str, server_key: str, biome: str, *, event_type: str, ts_epoch: Optional[float] = None) -> None:
        detector     = self._get_username(uid) or uid
        server_label = server_key
        owner        = self._resolve_owner(uid, server_label)
        ps_link      = self._get_ps_link(uid) or ""
        scope        = self._scope(server_key)

        b = (biome or "").upper()
        base_modes = getattr(self, "_biome_modes", {}) or {}
        base_modes_user = getattr(self, "_biome_modes_user", {}) or {}
        lock_disabled = not self._is_bm_lock_enforced()
        forced_biomes = getattr(self, "_lock_forced_biomes", set())

        def _mode_for_hook(hook_modes: Optional[Dict[str, str]]) -> str:
            mode = None
            if isinstance(hook_modes, dict):
                mode = hook_modes.get(b)
            if mode is None:
                mode = base_modes.get(b)
            # If we previously forced Everyone due to lock, drop it once relaxed.
            if lock_disabled and b in forced_biomes:
                if mode is None or str(mode).lower() == "everyone":
                    mode = base_modes_user.get(b)
            if not lock_disabled and b in ("GLITCHED", "DREAMSPACE", "CYBERSPACE"):
                return "Everyone"
            if mode is None:
                if lock_disabled and b in ("GLITCHED", "DREAMSPACE", "CYBERSPACE"):
                    return "None"
                if b == "NORMAL":
                    return "None"
                return "Message"
            return str(mode).capitalize()

        embed = None

        # Build a set of all known users on this server so that
        # "Assign Users" works per-server, not just for the one
        # account whose log produced this event.
        try:
            server_users = {str(u) for u in getattr(scope, "users", set())}
        except Exception:
            server_users = set()
        server_users.add(str(uid))

        posted_any = False
        for wh in self._biome_webhooks:
            mode = _mode_for_hook(wh.get("biome_modes"))
            if mode == "None":
                continue
            url = (wh.get("url") or "").strip()
            if not url:
                continue
            allowed = wh.get("biomes") or []
            if allowed and b not in allowed:
                continue
            allowed_users = wh.get("users", None)
            # None => no user filter (all users on the server allowed).
            # []   => explicit "no users" for this webhook.
            if allowed_users is not None:
                if not allowed_users:
                    # Explicitly disabled for all users.
                    continue
                allowed_set = {str(u) for u in allowed_users}
                lower_users = wh.get("_user_lower") or {u.lower() for u in allowed_set}
                uid_str     = str(uid)
                detector_s  = str(detector)
                detector_l  = detector_s.lower()

                # 1) Direct matches: current uid or detector name
                direct_match = (
                    uid_str in allowed_set
                    or detector_s in allowed_set
                    or detector_l in lower_users
                )

                # 2) Server-level matches: any uid on this server
                server_match = any(su in allowed_set for su in server_users)

                if not (direct_match or server_match):
                    continue
            if embed is None:
                embed = self._build_biome_embed(
                    event_type=event_type,
                    biome=b,
                    owner_name=owner,
                    detected_by=detector,
                    server_label=server_label,
                    ps_link=ps_link,
                    include_ps_link=(event_type == "start"),
                    ts_epoch=ts_epoch,  # anchor to log line's timestamp
                )
            content = "@everyone" if (mode == "Everyone" and event_type == "start") else ""
            payload = {"content": content, "embeds": [embed]}
            try:
                self._send_executor.submit(_post_webhook, url, payload)
            except Exception:
                pass
            posted_any = True

        if posted_any:
            self._log(
                f"[MultiScope] BIOME {event_type.upper()} posted | biome={b} | server={server_key} | "
                f"by={detector} | ts={int(ts_epoch) if ts_epoch else '-'}"
            )
        if posted_any:
            scope.events += 1


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

        try:
            self._send_executor.submit(_post_webhook, self._merchant_hook, payload)
        except Exception:
            pass
        # START TEMP BLOCK WINDOW for Jester
        if who == "Jester":
            self._maybe_start_temp_block(uid, "Jester")

        scope = self._scopes.setdefault(server_key, ServerScope(server_key))
        scope.last_merchant = who
        scope.last_merchant_ts = time.time()
        scope.users.add(uid)
        scope.events += 1

    # ---- Cadence model -------------------------------------------------

    _MERCHANT_MIN_GAP = 18 * 60  # 18 minutes

    def _choose_uid_for_scope(self, key: str) -> Optional[str]:
        scope = self._scope(key)
        members = sorted(scope.users)
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
            base = 1.20 + 3.80 * rem_ratio  # 1.2 - 5.0

        # Merchant window: if we're past the minimum spawn gap, tighten polling
        m_age = (now - scope.last_merchant_ts) if scope.last_merchant_ts else 1e9
        if m_age >= self._MERCHANT_MIN_GAP:
            base *= 0.50   # tighten (more often)
        else:
            base *= 1.50   # relax (less often) while we're still within the 18-min quiet

        # Clamp
        if base < MIN_IVL: base = MIN_IVL
        if base > MAX_IVL: base = MAX_IVL
        return float(base)

    # -- Tail one user ---------------------------------------------------------

    def _tail_one(self, uid: str) -> None:
        cur = self._cur.get(uid)
        if not cur or not cur.path or not os.path.isfile(cur.path):
            self._resolve_current_log(uid, force=True)
            cur = self._cur.get(uid)
            if not cur or not cur.path or not os.path.isfile(cur.path):
                return

        disconnect_hit = False
        try:
            read_start = _t.perf_counter()
            chunks = []
            bytes_read = 0

            while True:
                size_now = os.path.getsize(cur.path)
                if size_now <= cur.pos:
                    break

                to_read = min(self._per_read_cap, size_now - cur.pos)
                with open(cur.path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(cur.pos)
                    chunks.append(f.read(to_read))
                cur.pos += to_read
                bytes_read += to_read
                # burst limits: ~30ms or up to ~1 MiB total this dequeue
                if (_t.perf_counter() - read_start) >= 0.030:
                    break
                if bytes_read >= (self._per_read_cap * 4):
                    break

            if not chunks:
                return

            chunk = "".join(chunks)
            disconnect_hit = self._scan_disconnect_in_chunk(uid, chunk)

        except Exception:
            return

        # Stitch with any carried partial line from last time, and only parse full lines
        text = (cur.carry or "") + (chunk or "")
        nl = text.rfind("\n")
        if nl == -1:
            # still no complete line; carry everything
            cur.carry = text[-4096:]  # keep small tail
            return
        parse_text = text[:nl + 1]
        cur.carry = text[nl + 1:]

        # -- Cheap token prefilters before heavy regex -------------------------
        has_merchant = ("[Merchant]" in parse_text) or ("[Merchants]" in parse_text)
        has_rpc      = ("[BloxstrapRPC]" in parse_text)

        # Merchants (scope-level dedupe by timestamp window)
        if has_merchant:
            matches = list(MERCHANT_RE.finditer(parse_text))
            if matches:
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
                    self._last_merchant_ts_by_scope.setdefault(scope_key, {})
                    for k, v in latest.items():
                        self._last_merchant_ts_by_scope[scope_key][k] = v["ts"].timestamp()
                    self._first_merchant_scan_done.add(uid)
                else:
                    for k, v in latest.items():
                        ts_epoch = v["ts"].timestamp()
                        if not self._scope_dedupe_merchant_ts(scope_key, k, ts_epoch, window=10.0):
                            self._emit_merchant(uid, k, v["ts"], v["line"])

        # Biomes (anchor to log line's ISO timestamp like merchants)
        if has_rpc:
            matches = list(BIOME_RPC_RE.finditer(parse_text))
            # Always keep explicit Optional types so Pylance knows the shape.
            latest_ts: Optional[float] = None
            latest_biome: Optional[str] = None
            latest_menu_ts: Optional[float] = None
            latest_menu_flag: Optional[bool] = None

            server_key = self._server_key_for(uid)
            scope = self._scopes.setdefault(server_key, ServerScope(server_key))

            if matches:
                for m in matches:
                    ts_text = m.group("timestamp")
                    try:
                        dt = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
                    except Exception:
                        continue

                    try:
                        rpc = json.loads(m.group("json"))
                    except Exception:
                        continue

                    menu_state = _extract_in_menu_from_rpc(rpc)
                    if menu_state is not None:
                        ts_epoch = float(dt.timestamp())
                        if (latest_menu_ts is None) or (ts_epoch >= latest_menu_ts):
                            latest_menu_ts = ts_epoch
                            latest_menu_flag = menu_state

                    try:
                        data = rpc.get("data") or {}
                        li = data.get("largeImage") or {}
                        biome_name = (li.get("hoverText") or "").strip()
                    except Exception:
                        continue

                    if not biome_name:
                        continue

                    latest_ts = float(dt.timestamp())
                    latest_biome = str(biome_name).upper()

            # Fallback: parse any RPC blobs even if the regex missed them (no timestamp)
            if latest_menu_ts is None or latest_ts is None or not latest_biome:
                try:
                    fallback_rpcs = _extract_rpc_jsons_from_text(parse_text)
                except Exception:
                    fallback_rpcs = []
                if fallback_rpcs:
                    now_f = time.time()
                    for rpc in fallback_rpcs:
                        if latest_menu_ts is None:
                            ms = _extract_in_menu_from_rpc(rpc)
                            if ms is not None:
                                latest_menu_ts = now_f
                                latest_menu_flag = ms
                        if latest_ts is None or not latest_biome:
                            b = _extract_biome_from_rpc(rpc)
                            if b:
                                latest_ts = now_f
                                latest_biome = str(b).upper()

            if not disconnect_hit:
                if latest_menu_ts is not None:
                    if latest_menu_ts >= getattr(scope, "last_menu_ts", 0.0):
                        scope.in_menu = latest_menu_flag
                        scope.last_menu_ts = latest_menu_ts
                    scope.users.add(uid)
                    self._clear_menu_unknown(uid)
                else:
                    # keep default True when no RPC state parsed yet
                    scope.in_menu = scope.in_menu if scope.in_menu is not None else True
                    scope.users.add(uid)
            else:
                scope.users.add(uid)

            if (latest_ts is not None) and latest_biome:
                event_ts: float = latest_ts
                biome: str = latest_biome

                scope.users.add(uid)

                # carry donor's last biome into spare (handoff) just once
                prev = scope.last_biome
                if not prev and uid in self._handoff_prev_biome_for_spare:
                    prev = self._handoff_prev_biome_for_spare.pop(uid, None)

                # NEW: if the biome didn't change, do nothing.
                # Don't emit End/Start and don't reset the start timestamp.
                if prev and biome == prev:
                    pass
                else:
                    # Keep the existing min-interval gating and first-post allowance.
                    last_post: float = float(self._last_biome_post_by_scope.get(server_key, 0.0) or 0.0)
                    allow_first: bool = (last_post == 0.0 and not prev)  # first biome for this scope

                    if allow_first or (event_ts - last_post) >= self._biome_min_interval:
                        if prev:
                            self._emit_biome_event(uid, server_key, prev, event_type="end", ts_epoch=event_ts)

                        # Only set the start timestamp when the biome actually CHANGES.
                        scope.last_biome = biome
                        scope.last_biome_ts = event_ts
                        self._last_biome_post_by_scope[server_key] = event_ts

                        if biome != "NORMAL":
                            self._emit_biome_event(uid, server_key, biome, event_type="start", ts_epoch=event_ts)
                            if biome in ("DREAMSPACE", "GLITCHED", "CYBERSPACE"):
                                self._maybe_start_temp_block(uid, f"Biome:{biome}")
                        else:
                            self._log(f"[MultiScope] + NORMAL (start suppressed) | user={self._get_username(uid)} | server={server_key}")
        # keep scope membership fresh
        key = self._server_key_for(uid)
        self._scope(key).users.add(uid)
        # If backlog remains, pull this scope forward slightly
        try:
            if os.path.getsize(cur.path) > cur.pos:
                key = self._server_key_for(uid)
                scope = self._scope(key)
                scope.next_tail_at = min(getattr(scope, "next_tail_at", 0.0), _t.time() + 0.05)
        except Exception:
            pass



    # -- Public loop hooks -----------------------------------------------------

    def tick(self, status_by_uid: Dict[str, dict]) -> None:
        with self._lock:
            # ensure scopes contain their current members
            for uid in list(self._cur.keys()):
                key = self._server_key_for(uid)
                if key:
                    self._scope(key).users.add(uid)

            # fallback refresher (1s jitter; includes cache refresh)
            self._maybe_refresh_paths(status_by_uid)

            # Seed menu state on-demand if still unknown for a scope
            for key, scope in list(self._scopes.items()):
                if scope.in_menu is None:
                    # try any member we have a log for
                    for uid in list(scope.users):
                        cur = self._cur.get(uid)
                        if not cur or not cur.path:
                            continue
                        blocked = self._menu_unknown_log_by_uid.get(str(uid))
                        if blocked:
                            try:
                                if os.path.abspath(blocked) == os.path.abspath(cur.path):
                                    continue
                            except Exception:
                                if blocked == cur.path:
                                    continue
                        try:
                            self._seed_last_rpc_state(uid, cur.path)
                        except Exception:
                            pass
                        break

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

                # Tail once (now concurrent, bounded by the pool)
                self._submit_tail(uid)

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
                "in_menu": s.in_menu,
                "last_biome": s.last_biome or "",
                "biome_age": int(now_t - s.last_biome_ts) if s.last_biome_ts else None,
                "last_merchant": s.last_merchant or "",
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
    
    # in multiscope.py (inside class MultiScopeEngine)
    def shutdown(self):
        obs = getattr(self, "_observer", None)
        self._observer = None
        try:
            if obs:
                obs.stop()
                obs.join(timeout=3)
        except Exception:
            pass
        exe = getattr(self, "_executor", None)
        try:
            if exe:
                exe.shutdown(wait=False)
        except Exception:
            pass
        snd = getattr(self, "_send_executor", None)
        try:
            if snd:
                snd.shutdown(wait=False)
        except Exception:
            pass
        # optional: clear bookkeeping
        self._watched_dirs.clear()
        # temp-block sessions currently auto-expire; no explicit cancel hook yet








