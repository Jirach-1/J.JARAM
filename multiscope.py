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
    from biomes import load_biomes_catalog, biome_meta, biome_duration, biome_names
    load_biomes_catalog()
except Exception:
    from typing import Tuple, Optional
    def biome_meta(name: str) -> Tuple[int, str]:
        return int(0x3BA55D), ""     # default color, empty thumbnail
    def biome_duration(name: str) -> Optional[int]:
        return None
    def biome_names() -> list[str]:
        return ["NORMAL"]
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


APP_FOOTER = "J.JARAM JX 2x51"
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
    texts: List[str] = []

    # Bloxstrap schemas can differ; try both "state" and "details".
    for k in ("state", "details"):
        try:
            v = data.get(k)
        except Exception:
            continue
        if isinstance(v, str) and v.strip():
            texts.append(v.strip())
            continue
        if isinstance(v, dict):
            for kk in ("text", "value", "name", "label", "title"):
                try:
                    vv = v.get(kk)
                except Exception:
                    continue
                if isinstance(vv, str) and vv.strip():
                    texts.append(vv.strip())
                    break

    if not texts:
        return None

    s = " ".join(texts).strip().lower()
    if not s:
        return None

    # Treat common variants as "in menu".
    if ("in main menu" in s) or ("main menu" in s) or ("in menu" in s) or (s in {"menu", "mainmenu"}):
        return True
    return False



# Merchant detection modes.
MERCHANT_MODE_ASSET_ID = "asset_id"
MERCHANT_MODE_LEGACY_CHAT = "legacy_chat"

MERCHANT_ASSET_IDS = {
    "18247420806": "Jester",
    "18247165978": "Mari",
    "97148159887178": "Rin",
}

# Legacy merchant chat lines - tolerant to optional colon after [Merchant]
# and variable ms precision.
MERCHANT_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{0,6})?Z),"  # allow 0-6 ms digits
    r"[^\n]*?\[(?:Merchant|Merchants)\]:?\s*"                               # optional colon after [Merchant]
    r"(?P<merchant_name>Jester|Mari|Rin)\b"
    r"[^\n]*?\b(arrived|spawn(?:ed|ing)?|appeared)\b"
    r"[^\n]*"
    r")$",
    re.IGNORECASE | re.MULTILINE
)

# Merchant asset-id lines - only the animation asset ID matters; the
# Workspace.Map.* segment is intentionally ignored because it is random.
MERCHANT_ASSET_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{0,6})?Z),"
    r"[^\n]*?rbxassetid://(?P<asset_id>97148159887178|18247420806|18247165978)"
    r"[^\n]*"
    r")$",
    re.IGNORECASE | re.MULTILINE
)

MERCHANT_ASSET_PREFILTERS = tuple(f"rbxassetid://{asset_id}".lower() for asset_id in MERCHANT_ASSET_IDS)
MERCHANT_LEGACY_PREFILTERS = ("[merchant]", "[merchants]")


def _normalize_merchant_detection_mode(mode: object) -> str:
    raw = str(mode or "").strip().lower()
    if raw in {"legacy", "chat", "merchant", "merchant_chat", MERCHANT_MODE_LEGACY_CHAT}:
        return MERCHANT_MODE_LEGACY_CHAT
    return MERCHANT_MODE_ASSET_ID


def _merchant_prefilters_for_mode(mode: object) -> tuple[str, ...]:
    if _normalize_merchant_detection_mode(mode) == MERCHANT_MODE_LEGACY_CHAT:
        return MERCHANT_LEGACY_PREFILTERS
    return MERCHANT_ASSET_PREFILTERS


def _iter_merchant_matches(text: str, mode: object) -> List[dict]:
    normalized_mode = _normalize_merchant_detection_mode(mode)
    if normalized_mode == MERCHANT_MODE_LEGACY_CHAT:
        return [
            {
                "full_line": m.group("full_line"),
                "timestamp": m.group("timestamp"),
                "merchant_name": m.group("merchant_name").title(),
            }
            for m in MERCHANT_RE.finditer(text)
        ]

    out: List[dict] = []
    for m in MERCHANT_ASSET_RE.finditer(text):
        asset_id = str(m.group("asset_id") or "").strip()
        who = MERCHANT_ASSET_IDS.get(asset_id)
        if not who:
            continue
        out.append(
            {
                "full_line": m.group("full_line"),
                "timestamp": m.group("timestamp"),
                "merchant_name": who,
            }
        )
    return out

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
    - Uses Roblox user-blocking API (no browser)
    - Tails their log for 'Player added: <name> <id>'
    - Blocks only names present in Blank 
    - Grows lookups/blocklist via Bloxlink reverse search
    - Unblocks any IDs we blocked when the window ends
    """
    GUILD_ID = "1371698242886307921"   # your server (can be moved to credentials if you prefer)
    WINDOW_SEC = 180

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

    # ---------- Roblox user-blocking API ----------
    def _make_session(self):
        from utilities_tab import _make_blocking_api_session  # lazy-import to avoid init cycles
        return _make_blocking_api_session(self.cookie)

    def _block_id(self, session, user_id: str) -> str:
        from utilities_tab import _api_block_user
        return _api_block_user(session, self.cookie, user_id)

    def _unblock_id(self, session, user_id: str) -> str:
        from utilities_tab import _api_unblock_user
        return _api_unblock_user(session, self.cookie, user_id)

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

        # Create API session up front (CSRF + browserid)
        try:
            session = self._make_session()
        except Exception as e:
            self._log(f"[TempBlock] {self.uid}: failed to start API session ({e})")
            return

        # Prepare log tail
        log_path = find_log_for_username(self.username.lower(), allow_fallback=False)
        if not log_path or not os.path.isfile(log_path):
            self._log(f"[TempBlock] {self.uid}: no log for '{self.username}'")
            try: session.close()
            except Exception: pass
            return

        try:
            f = open(log_path, "r", encoding="utf-8", errors="ignore")
        except Exception as e:
            self._log(f"[TempBlock] {self.uid}: cannot open log ({e})")
            try: session.close()
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
                        res = self._block_id(session, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] blocked @{uname} ({rid}) on {self.uid} - {res} [blocklist]")
                        else:
                            self._log(f"[TempBlock] failed blocking @{uname} ({rid}) on {self.uid} - {res} [blocklist]")
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
                        res = self._block_id(session, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] (SURGE) blocked @{uname} ({rid}) on {self.uid} - {res}")
                        else:
                            self._log(f"[TempBlock] (SURGE) failed blocking @{uname} ({rid}) on {self.uid} - {res}")
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
                        res = self._block_id(session, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] blocked @{uname} ({rid}) on {self.uid} - {res} [unknown-blocklist]")
                        else:
                            self._log(f"[TempBlock] failed blocking @{uname} ({rid}) on {self.uid} - {res} [unknown-blocklist]")

                    # we actually processed this entry this tick - safe to mark seen
                    self._seen_ids.add(rid)
                    
                time.sleep(0.25)

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
                res = self._unblock_id(session, rid)
                if res in ("unblocked", "already_unblocked"):
                    self._log(f"[TempBlock] unblocked {rid} - {res}")
                else:
                    self._log(f"[TempBlock] unblock failed {rid} - {res}")
        else:
            self._log(f"[TempBlock] {self.uid}: window CLOSED - nothing to unblock")

        try: session.close()
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
    in_menu: Optional[bool] = None  # unknown until proven otherwise
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
        stats_path: Optional[str] = None,
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
        self._merchant_filters = {"Jester": True, "Mari": True, "Rin": True}
        self._ping_map = {"Jester": "", "Mari": "", "Rin": ""}
        self._merchant_detection_mode = MERCHANT_MODE_ASSET_ID
        self._disable_log_based_merchant_detection = False

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
        self._skip_webhook_unknown_context = False

        self._lock = threading.Lock()
        # Events (thread-safe): GUI will drain these and act (e.g., recycle on disconnect)
        self._event_lock = threading.Lock()
        self._events: list[tuple[str, str, str]] = []   # (kind, uid, payload)

        # Disconnect dedupe: uid -> (normpath, absolute_end_offset)
        self._last_disconnect_sig_by_uid: Dict[str, Tuple[str, int]] = {}

        # Status snapshot for lookback gates (set in tick()).
        self._status_snapshot: Dict[str, dict] = {}
        self._status_snapshot_ts: float = 0.0

        # Persistent "found" counters (biomes + merchants)
        self._stats_path = str(stats_path or "").strip()
        self._stats_lock = threading.Lock()
        self._found_stats: dict = self._default_found_stats()
        self._load_found_stats()
        self._ensure_found_stats_catalog()

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
        # Per-user log paths that were active when a disconnect fired.
        # These are ignored on future resolves so we do not re-attach stale logs.
        self._ignored_logs_by_uid: Dict[str, Set[str]] = {}
        self._menu_unknown_log_by_uid: Dict[str, str] = {}
        # Disconnect fallback: if in_menu stays unknown too long, recycle that user.
        self._menu_none_since_by_uid: Dict[str, float] = {}
        self._menu_none_disconnect_fired_by_uid: Set[str] = set()
        
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
        enable_rin: bool = True,
        jester_ping: str = "",
        mari_ping: str = "",
        rin_ping: str = "",
        merchant_detection_mode: str = MERCHANT_MODE_ASSET_ID,
        disable_log_based_merchant_detection: bool = False,
        merchant_rate_limit: float = 15.0,   # kept for backward-compat; ignored
        biome_min_interval: float = 2.0,
        # NEW:
        biome_modes: Optional[Dict[str, str]] = None,
        skip_webhook_unknown_context: bool = False,
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
        self._merchant_filters = {
            "Jester": bool(enable_jester),
            "Mari": bool(enable_mari),
            "Rin": bool(enable_rin),
        }
        self._ping_map = {"Jester": jester_ping or "", "Mari": mari_ping or "", "Rin": rin_ping or ""}
        self._merchant_detection_mode = _normalize_merchant_detection_mode(merchant_detection_mode)
        self._disable_log_based_merchant_detection = bool(disable_log_based_merchant_detection)
        # --- ignore merchant_rate_limit entirely (no cooldown)
        self._biome_min_interval = float(biome_min_interval or 2.0)
        self._biome_modes = base_modes
        self._biome_modes_user = base_modes_raw
        self._bm_relaxed = lock_disabled
        self._bm_lock_confirmed = not lock_disabled
        self._lock_forced_biomes = forced_biomes
        self._skip_webhook_unknown_context = bool(skip_webhook_unknown_context)


    # -- Persistent "found" counters -------------------------------------------

    def _default_found_stats(self) -> dict:
        return {
            "schema": 2,
            "biomes_total": {},       # biome -> count (ALL TIME)
            "merchants_total": {},    # merchant -> count (ALL TIME)
            "biome_events": [],       # [{ts: float, biome: str}] (rolling, for 24h/week/month)
            "merchant_events": [],    # [{ts: float, merchant: str}] (rolling, for 24h/week/month)
        }

    def _ensure_found_stats_catalog(self) -> None:
        """
        Ensure the persisted stats file always contains:
        - Every biome from biomes.json (excluding NORMAL) with at least a 0 count
        - The canonical merchants (Jester/Mari/Rin) with at least a 0 count
        """
        path = getattr(self, "_stats_path", "") or ""
        if not path:
            return

        try:
            file_exists = os.path.isfile(path)
        except Exception:
            file_exists = False

        try:
            biomes = [b for b in biome_names() if str(b).strip().upper() != "NORMAL"]
        except Exception:
            biomes = []
        biomes = [str(b).strip().upper() for b in biomes if str(b).strip()]

        with self._stats_lock:
            changed = False

            if self._found_stats.get("schema") != 2:
                self._found_stats["schema"] = 2
                changed = True

            bt = self._found_stats.setdefault("biomes_total", {})
            if not isinstance(bt, dict):
                bt = {}
                self._found_stats["biomes_total"] = bt
                changed = True
            if "NORMAL" in bt:
                try:
                    bt.pop("NORMAL", None)
                    changed = True
                except Exception:
                    pass
            for b in biomes:
                if b not in bt:
                    bt[b] = 0
                    changed = True

            mt = self._found_stats.setdefault("merchants_total", {})
            if not isinstance(mt, dict):
                mt = {}
                self._found_stats["merchants_total"] = mt
                changed = True
            for merch in ("Jester", "Mari", "Rin"):
                if merch not in mt:
                    mt[merch] = 0
                    changed = True

            if not isinstance(self._found_stats.get("biome_events"), list):
                self._found_stats["biome_events"] = []
                changed = True
            if not isinstance(self._found_stats.get("merchant_events"), list):
                self._found_stats["merchant_events"] = []
                changed = True

            self._prune_found_events_locked(now_ts=time.time())

            if changed or not file_exists:
                self._save_found_stats_locked()

    def _prune_found_events_locked(self, *, now_ts: Optional[float] = None) -> None:
        try:
            events = self._found_stats.get("biome_events")
            if not isinstance(events, list):
                self._found_stats["biome_events"] = []
                events = []

            now_v = float(now_ts if now_ts is not None else time.time())
            # Keep ~31 days so "month" (30d) always has coverage.
            cutoff = now_v - (31 * 24 * 3600)

            kept = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ts_raw = ev.get("ts")
                biome_raw = ev.get("biome")
                try:
                    ts_f = float(ts_raw)
                except Exception:
                    continue
                if ts_f < cutoff:
                    continue
                if not isinstance(biome_raw, str):
                    continue
                b = biome_raw.strip().upper()
                if not b or b == "NORMAL":
                    continue
                kept.append({"ts": ts_f, "biome": b})

            kept.sort(key=lambda d: d.get("ts", 0.0))
            MAX_EVENTS = 20_000
            if len(kept) > MAX_EVENTS:
                kept = kept[-MAX_EVENTS:]

            self._found_stats["biome_events"] = kept
        except Exception:
            self._found_stats["biome_events"] = []

        try:
            events = self._found_stats.get("merchant_events")
            if not isinstance(events, list):
                self._found_stats["merchant_events"] = []
                events = []

            now_v = float(now_ts if now_ts is not None else time.time())
            cutoff = now_v - (31 * 24 * 3600)

            kept = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ts_raw = ev.get("ts")
                merch_raw = ev.get("merchant")
                try:
                    ts_f = float(ts_raw)
                except Exception:
                    continue
                if ts_f < cutoff:
                    continue
                if not isinstance(merch_raw, str):
                    continue
                m = merch_raw.strip().title()
                if not m:
                    continue
                kept.append({"ts": ts_f, "merchant": m})

            kept.sort(key=lambda d: d.get("ts", 0.0))
            MAX_EVENTS = 20_000
            if len(kept) > MAX_EVENTS:
                kept = kept[-MAX_EVENTS:]

            self._found_stats["merchant_events"] = kept
        except Exception:
            self._found_stats["merchant_events"] = []

    def _load_found_stats(self) -> None:
        path = getattr(self, "_stats_path", "") or ""
        if not path:
            return
        try:
            if not os.path.isfile(path):
                return
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                raw = json.load(f)
        except Exception:
            return
        if not isinstance(raw, dict):
            return

        stats = self._default_found_stats()

        bt = raw.get("biomes_total")
        if isinstance(bt, dict):
            for k, v in bt.items():
                if not isinstance(k, str):
                    continue
                b = k.strip().upper()
                if not b:
                    continue
                try:
                    stats["biomes_total"][b] = int(v)
                except Exception:
                    continue

        mt = raw.get("merchants_total")
        if isinstance(mt, dict):
            for k, v in mt.items():
                if not isinstance(k, str):
                    continue
                m = k.strip().title()
                if not m:
                    continue
                try:
                    stats["merchants_total"][m] = int(v)
                except Exception:
                    continue

        evs = raw.get("biome_events")
        if isinstance(evs, list):
            stats["biome_events"] = evs
        mevs = raw.get("merchant_events")
        if isinstance(mevs, list):
            stats["merchant_events"] = mevs

        with self._stats_lock:
            self._found_stats = stats
            self._prune_found_events_locked(now_ts=time.time())

    def _save_found_stats_locked(self) -> None:
        path = getattr(self, "_stats_path", "") or ""
        if not path:
            return
        try:
            dirpath = os.path.dirname(path)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
        except Exception:
            pass

        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._found_stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _record_found_biome(self, biome: str, *, ts_epoch: Optional[float] = None) -> None:
        b = str(biome or "").strip().upper()
        if not b or b == "NORMAL":
            return
        try:
            ts = float(ts_epoch if ts_epoch is not None else time.time())
        except Exception:
            ts = time.time()

        with self._stats_lock:
            bt = self._found_stats.setdefault("biomes_total", {})
            try:
                bt[b] = int(bt.get(b, 0)) + 1
            except Exception:
                bt[b] = 1

            self._found_stats.setdefault("biome_events", []).append({"ts": ts, "biome": b})
            self._prune_found_events_locked(now_ts=ts)
            self._save_found_stats_locked()

    def _record_found_merchant(self, merchant: str, *, ts_epoch: Optional[float] = None) -> None:
        m = str(merchant or "").strip().title()
        if not m:
            return
        try:
            ts = float(ts_epoch if ts_epoch is not None else time.time())
        except Exception:
            ts = time.time()
        with self._stats_lock:
            mt = self._found_stats.setdefault("merchants_total", {})
            try:
                mt[m] = int(mt.get(m, 0)) + 1
            except Exception:
                mt[m] = 1
            self._found_stats.setdefault("merchant_events", []).append({"ts": ts, "merchant": m})
            self._prune_found_events_locked(now_ts=ts)
            self._save_found_stats_locked()

    def get_found_stats_snapshot(self) -> dict:
        with self._stats_lock:
            snap = json.loads(json.dumps(self._found_stats))

        bt = snap.get("biomes_total") if isinstance(snap.get("biomes_total"), dict) else {}
        mt = snap.get("merchants_total") if isinstance(snap.get("merchants_total"), dict) else {}
        try:
            biome_total = sum(int(v) for v in bt.values())
        except Exception:
            biome_total = 0
        try:
            merchant_total = sum(int(v) for v in mt.values())
        except Exception:
            merchant_total = 0

        return {
            "biomes_total": bt,
            "merchants_total": mt,
            "biomes_total_count": biome_total,
            "merchants_total_count": merchant_total,
        }

    def get_biomes_found_counts(self, window_seconds: float) -> dict:
        try:
            window = float(window_seconds)
        except Exception:
            window = 0.0
        if window <= 0:
            return {"counts": {}, "total": 0, "window_seconds": window_seconds}

        now_ts = time.time()
        cutoff = now_ts - window
        counts: Dict[str, int] = {}

        with self._stats_lock:
            events = list(self._found_stats.get("biome_events") or [])

        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                ts = float(ev.get("ts", 0))
            except Exception:
                continue
            if ts < cutoff:
                continue
            biome = ev.get("biome")
            if not isinstance(biome, str):
                continue
            b = biome.strip().upper()
            if not b or b == "NORMAL":
                continue
            counts[b] = counts.get(b, 0) + 1

        total = sum(counts.values())
        return {"counts": counts, "total": total, "window_seconds": window_seconds}

    def get_merchants_found_counts(self, window_seconds: float) -> dict:
        try:
            window = float(window_seconds)
        except Exception:
            window = 0.0
        if window <= 0:
            return {"counts": {}, "total": 0, "window_seconds": window_seconds}

        now_ts = time.time()
        cutoff = now_ts - window
        counts: Dict[str, int] = {}

        with self._stats_lock:
            events = list(self._found_stats.get("merchant_events") or [])

        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                ts = float(ev.get("ts", 0))
            except Exception:
                continue
            if ts < cutoff:
                continue
            merch = ev.get("merchant")
            if not isinstance(merch, str):
                continue
            m = merch.strip().title()
            if not m:
                continue
            counts[m] = counts.get(m, 0) + 1

        total = sum(counts.values())
        return {"counts": counts, "total": total, "window_seconds": window_seconds}


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
        user_ids_set = {str(uid) for uid in (user_ids or [])}
        with self._lock:
            # remove stale only
            stale_uids = {
                str(uid)
                for uid in (
                    set(self._cur.keys())
                    | set(self._normpath_by_uid.keys())
                    | set(self._ignored_logs_by_uid.keys())
                    | set(self._menu_unknown_log_by_uid.keys())
                    | set(self._last_disconnect_sig_by_uid.keys())
                )
                if str(uid) not in user_ids_set
            }
            for uid in stale_uids:
                self._cur.pop(uid, None)
                self._normpath_by_uid.pop(uid, None)
                self._ignored_logs_by_uid.pop(uid, None)
                self._menu_unknown_log_by_uid.pop(uid, None)
                self._last_disconnect_sig_by_uid.pop(uid, None)
                try:
                    if hasattr(self, "_last_switch_ts"):
                        self._last_switch_ts.pop(uid, None)
                except Exception:
                    pass

        # Do resolves + watcher setup without holding the engine lock
        for uid in user_ids_set:
            self._resolve_current_log(uid, force=True)
            cur = self._cur.get(uid)
            if cur and cur.path:
                self._watch_dir_for_path(cur.path)

        # Do the I/O-heavy warmstarts last, also outside the lock
        for uid in user_ids_set:
            self._warmstart_user_tail(uid)

    @staticmethod
    def _normalize_log_path(path: Optional[str]) -> str:
        if not path:
            return ""
        try:
            return os.path.normcase(os.path.abspath(str(path)))
        except Exception:
            return str(path)

    def _remember_ignored_log(self, uid: str, path: Optional[str]) -> None:
        np = self._normalize_log_path(path)
        if not np:
            return
        uid_s = str(uid)
        ignored = self._ignored_logs_by_uid.setdefault(uid_s, set())
        ignored.add(np)

    def _drop_user_log_tracking(self, uid: str, *, ignore_current: bool = True) -> None:
        uid_s = str(uid)
        cur = self._cur.pop(uid_s, None)

        if ignore_current:
            try:
                if cur and cur.path:
                    self._remember_ignored_log(uid_s, cur.path)
            except Exception:
                pass

        prev_np = self._normpath_by_uid.pop(uid_s, None)
        if ignore_current and prev_np:
            self._remember_ignored_log(uid_s, prev_np)

        try:
            if hasattr(self, "_last_switch_ts"):
                self._last_switch_ts.pop(uid_s, None)
        except Exception:
            pass

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
        prev_np = self._normpath_by_uid.get(uid)
        if not prev_np and cur.path:
            try:
                prev_np = os.path.normcase(os.path.abspath(cur.path))
            except Exception:
                prev_np = os.path.normcase(str(cur.path))
        # NEW: canonicalize both sides and skip if unchanged
        new_np = os.path.normcase(os.path.abspath(path))
        ignored_logs = self._ignored_logs_by_uid.get(str(uid), set())
        if new_np in ignored_logs:
            return
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
        if prev_np and new_np != prev_np:
            # Prevent stitching a partial line from the previous file into the new one.
            cur.carry = ""
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

        self._log(f"[MultiScope] switched log for {uname} - {os.path.basename(path)}")
        # Warmstart on log switches so we still seed in_menu/merchant state from the new file.
        if prev_np and new_np != prev_np:
            try:
                self._executor.submit(self._warmstart_user_tail, uid)
            except Exception:
                try:
                    self._warmstart_user_tail(uid)
                except Exception:
                    pass

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
                base_offset = 0
                if size_now > window:
                    base_offset = int(size_now - window)
                    f.seek(base_offset)
                chunk = f.read()
        except Exception:
            return

        # Disconnect lookback: only when the manager considers this uid active/recent.
        # This avoids firing disconnect events for idle users when MultiScope starts.
        disconnect_hit = False
        try:
            if self._should_disconnect_lookback(uid):
                disconnect_hit = self._scan_disconnect_in_text(uid, chunk, path=cur.path, base_offset=base_offset)
        except Exception:
            disconnect_hit = False

        # merchant seed (no notify) +' seed *scope* timestamps to avoid retro spam across users
        if not self._disable_log_based_merchant_detection:
            matches = _iter_merchant_matches(chunk, self._merchant_detection_mode)
            if matches:
                scope_key = self._server_key_for(uid)
                self._last_merchant_ts_by_scope.setdefault(scope_key, {})
                for m in matches:
                    try:
                        ts = datetime.fromisoformat(str(m.get("timestamp") or "").replace("Z", "+00:00"))
                    except Exception:
                        continue
                    name = str(m.get("merchant_name") or "").title()
                    if not name:
                        continue
                    self._last_merchant_ts_by_scope[scope_key][name] = ts.timestamp()

        # mark this user as warmstarted so its first live read doesn't post old lines
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
            if (not disconnect_hit) and latest_state is not None:
                key = self._server_key_for(uid)
                scope = self._scope(key)
                scope.in_menu = latest_state
                scope.last_menu_ts = time.time()
                try:
                    self._log(f"[SCAN-TRACE] {uid}: warmstart in_menu={latest_state} server={key}")
                except Exception:
                    pass
            else:
                try:
                    self._log(f"[SCAN-TRACE] {uid}: warmstart no in_menu found rpc={len(rpcs)}")
                except Exception:
                    pass

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

    def _scan_disconnect_in_text(
        self,
        uid: str,
        text: str,
        *,
        path: Optional[str] = None,
        base_offset: int = 0,
    ) -> bool:
        """Scan `text` for disconnect signals; emits at most once per log position."""
        if not text:
            return False

        # Fast negative check before doing full finditers (keeps behavior close to the old scanner).
        try:
            if not (R_DISC_REASON.search(text) or
                    R_DISC_NOTIFY.search(text) or
                    R_DISC_SENDING.search(text) or
                    R_CONN_LOST.search(text)):
                return False
        except Exception:
            pass

        last_end: Optional[int] = None
        last_payload = "detected in log"

        def _consider(match, payload: str) -> None:
            nonlocal last_end, last_payload
            try:
                end = int(match.end())
            except Exception:
                return
            if last_end is None or end >= last_end:
                last_end = end
                last_payload = payload

        try:
            for m in R_DISC_REASON.finditer(text):
                _consider(m, f"reason={m.group(1)}")
        except Exception:
            pass
        try:
            for m in R_DISC_NOTIFY.finditer(text):
                _consider(m, f"reason={m.group(1)}")
        except Exception:
            pass
        try:
            for m in R_DISC_SENDING.finditer(text):
                _consider(m, f"reason={m.group(1)}")
        except Exception:
            pass
        try:
            for m in R_CONN_LOST.finditer(text):
                _consider(m, "connection lost")
        except Exception:
            pass

        if last_end is None:
            return False

        norm_path = ""
        if path:
            try:
                norm_path = os.path.normcase(os.path.abspath(path))
            except Exception:
                norm_path = str(path)

        try:
            abs_end = max(0, int(base_offset)) + int(last_end)
        except Exception:
            abs_end = int(last_end)

        prev = self._last_disconnect_sig_by_uid.get(str(uid))
        if prev and prev[0] == norm_path and abs_end <= int(prev[1]):
            return False

        self._last_disconnect_sig_by_uid[str(uid)] = (norm_path, abs_end)
        self._mark_menu_unknown(uid)
        self._drop_user_log_tracking(uid, ignore_current=True)
        self._emit_event("disconnect", uid, last_payload)
        return True

    def _scan_disconnect_in_chunk(self, uid: str, chunk: str) -> bool:
        """Check a just-read log chunk for Roblox disconnect signals."""
        return self._scan_disconnect_in_text(uid, chunk)

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
        label = (self._get_server_label(uid) or "").strip()
        if not label:
            return "Unknown"
        upper = label.upper()
        if upper.startswith("DISCONNECTED") or upper.startswith("OFFLINE"):
            return "Disconnected"
        if upper.startswith("PUBLIC:"):
            return f"{label} #{uid}"
        return label

    def _display_server_label(self, server_key: str) -> str:
        if not server_key:
            return "Unknown"
        upper = server_key.upper()
        if upper.startswith("PUBLIC:") and " #" in server_key:
            return server_key.split(" #", 1)[0]
        return server_key

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

    def _should_skip_webhook(self, owner_raw: str, server_label: str, ps_link: str) -> bool:
        if not self._skip_webhook_unknown_context:
            return False
        server_unknown = (not server_label) or server_label.strip().lower() == "unknown"
        owner_unknown = (not owner_raw) or owner_raw.strip().lower() == "unknown"
        ps_unknown = not bool(ps_link)
        if server_unknown or owner_unknown or ps_unknown:
            self._log("[MultiScope] Skipping webhook; owner or private server unknown.")
            return True
        return False

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
        server_label = self._display_server_label(server_key)
        owner_raw    = (self._get_owner(uid) or "").strip()
        owner        = owner_raw or (self._get_username(uid) or "Unknown").strip()
        ps_link      = self._get_ps_link(uid) or ""
        scope        = self._scope(server_key)
        if self._should_skip_webhook(owner_raw, server_label, ps_link):
            return

        b = (biome or "").upper()
        if str(event_type).lower() == "start":
            try:
                self._record_found_biome(b, ts_epoch=ts_epoch)
            except Exception:
                pass
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
        if self._disable_log_based_merchant_detection:
            return
        try:
            self._record_found_merchant(who, ts_epoch=event_time_utc.timestamp())
        except Exception:
            pass

        server_key   = self._server_key_for(uid)
        scope = self._scopes.setdefault(server_key, ServerScope(server_key))
        scope.last_merchant = who
        try:
            scope.last_merchant_ts = float(event_time_utc.timestamp())
        except Exception:
            scope.last_merchant_ts = time.time()
        scope.users.add(uid)

        if not self._merchant_hook:
            return
        if not self._merchant_filters.get(who, True):
            return
        server_label = self._display_server_label(server_key)
        detector     = self._get_username(uid) or uid
        owner_raw    = (self._get_owner(uid) or "").strip()
        owner        = owner_raw or (self._get_username(uid) or "Unknown").strip()
        ps_link      = self._get_ps_link(uid) or ""
        if self._should_skip_webhook(owner_raw, server_label, ps_link):
            return

        emojis = {"Jester": "🃏", "Mari": "🛍️", "Rin": "🦊"}
        colors = {"Jester": 0xA352FF, "Mari": 0xFF82AB, "Rin": 0xFF9F1C}
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

        scope.events += 1

    def record_ocr_merchant(self, uid: str, merchant: str) -> None:
        """
        Update the per-scope "last merchant" tracker from an OCR detection.

        This does not emit/ping webhooks (OCR already does that); it only updates
        MultiScope's last_merchant/age and scope-level dedupe timestamp so the
        MultiScope table + scheduler reflect OCR-found merchants.
        """
        uid = str(uid or "").strip()
        if not uid:
            return

        m = str(merchant or "").strip().lower()
        if m not in ("jester", "mari", "rin"):
            return
        who = m.title()
  
        now_ts = time.time()
        with self._lock:
            scope_key = self._server_key_for(uid)
            scope = self._scope(scope_key)
            try:
                if self._scope_dedupe_merchant_ts(scope_key, who, float(now_ts), window=10.0):
                    return
            except Exception:
                pass
            try:
                self._record_found_merchant(who, ts_epoch=now_ts)
            except Exception:
                pass
            scope.last_merchant = who
            scope.last_merchant_ts = now_ts
            scope.users.add(uid)
            try:
                self._last_merchant_ts_by_scope.setdefault(scope_key, {})[who] = float(now_ts)
            except Exception:
                pass

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
            start_pos = int(cur.pos or 0)
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

        except Exception:
            return

        # Stitch with any carried partial line from last time, and only parse full lines
        text = (cur.carry or "") + (chunk or "")

        # Disconnect look-back: include the carried partial line so we don't miss split lines.
        try:
            base_offset = max(0, start_pos - len(cur.carry or ""))
        except Exception:
            base_offset = 0
        disconnect_hit = self._scan_disconnect_in_text(uid, text, path=cur.path, base_offset=base_offset)
        nl = text.rfind("\n")
        if nl == -1:
            # still no complete line; carry everything
            cur.carry = text[-4096:]  # keep small tail
            return
        parse_text = text[:nl + 1]
        cur.carry = text[nl + 1:]

        # -- Cheap token prefilters before heavy regex -------------------------
        parse_text_lower = parse_text.lower()
        has_merchant = (
            (not self._disable_log_based_merchant_detection)
            and any(token in parse_text_lower for token in _merchant_prefilters_for_mode(self._merchant_detection_mode))
        )
        has_rpc      = ("[BloxstrapRPC]" in parse_text)

        # Merchants (scope-level dedupe by timestamp window)
        if has_merchant:
            matches = _iter_merchant_matches(parse_text, self._merchant_detection_mode)
            if matches:
                latest: Dict[str, dict] = {}
                for m in matches:
                    try:
                        ts = datetime.fromisoformat(str(m.get("timestamp") or "").replace("Z", "+00:00"))
                    except Exception:
                        continue
                    name = str(m.get("merchant_name") or "").title()
                    if not name:
                        continue
                    latest[name] = {"ts": ts, "line": str(m.get("full_line") or "")}

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
            fallback_count: int = 0
            fallback_sample: Optional[dict] = None

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
                    try:
                        fallback_count = int(len(fallback_rpcs))
                        if isinstance(fallback_rpcs[-1], dict):
                            fallback_sample = fallback_rpcs[-1]
                    except Exception:
                        fallback_count = 0
                        fallback_sample = None
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
                        prev_menu = getattr(scope, "in_menu", None)
                        scope.in_menu = latest_menu_flag
                        scope.last_menu_ts = latest_menu_ts
                        if prev_menu != scope.in_menu:
                            try:
                                self._log(f"[SCAN-TRACE] {uid}: in_menu={scope.in_menu} server={server_key}")
                            except Exception:
                                pass
                    scope.users.add(uid)
                    self._clear_menu_unknown(uid)
                else:
                    scope.users.add(uid)
                    # Debug: RPC lines present but no menu state extracted.
                    try:
                        diag = ""
                        if isinstance(fallback_sample, dict):
                            data = fallback_sample.get("data")
                            if isinstance(data, dict):
                                sk = list(data.keys())[:8]
                                st = data.get("state")
                                dtl = data.get("details")
                                diag = f" data_keys={sk} state={st!r} details={dtl!r}"
                        self._throttled_log(
                            key=f"menu-miss:{uid}",
                            msg=f"[SCAN-TRACE] {uid}: no in_menu parsed from RPC matches={len(matches)} fallback={fallback_count} server={server_key}{diag}",
                            every=30.0,
                        )
                    except Exception:
                        pass
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
                            self._log(f"[MultiScope] BIOME START suppressed | biome=NORMAL | user={self._get_username(uid)} | server={server_key}")
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
            # Keep a snapshot for lookback gates (used by warmstart on log switches).
            try:
                import time
                self._status_snapshot = status_by_uid or {}
                self._status_snapshot_ts = float(time.time())
            except Exception:
                self._status_snapshot = status_by_uid or {}
                self._status_snapshot_ts = 0.0

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

            # Disconnect condition: in-menu remains unknown for too long.
            # Skip users already in the disconnected pool.
            try:
                for uid, st in (status_by_uid or {}).items():
                    try:
                        key = self._server_key_for(uid)
                    except Exception:
                        key = "Unknown"

                    if key == "Disconnected":
                        self._mark_menu_unknown(uid)
                        self._drop_user_log_tracking(uid, ignore_current=True)
                        self._menu_none_since_by_uid.pop(uid, None)
                        self._menu_none_disconnect_fired_by_uid.discard(uid)
                        continue

                    pids = []
                    try:
                        if isinstance(st, dict):
                            pids = st.get("pids") or []
                    except Exception:
                        pids = []

                    # Only apply while the user is actually running.
                    if not pids:
                        self._menu_none_since_by_uid.pop(uid, None)
                        self._menu_none_disconnect_fired_by_uid.discard(uid)
                        continue

                    scope = self._scope(key)
                    scope.users.add(uid)

                    if scope.in_menu is None:
                        # Only start the in_menu-none timeout after we have a strict
                        # per-user log attached (username marker found in logs).
                        has_user_log = False
                        try:
                            cur = self._cur.get(uid)
                            has_user_log = bool(cur and cur.path and os.path.isfile(cur.path))
                        except Exception:
                            has_user_log = False

                        if not has_user_log:
                            self._menu_none_since_by_uid.pop(uid, None)
                            self._menu_none_disconnect_fired_by_uid.discard(uid)
                        else:
                            since = self._menu_none_since_by_uid.get(uid)
                            if since is None:
                                self._menu_none_since_by_uid[uid] = now_t
                            elif (now_t - since) >= 120.0 and uid not in self._menu_none_disconnect_fired_by_uid:
                                self._mark_menu_unknown(uid)
                                self._drop_user_log_tracking(uid, ignore_current=True)
                                self._emit_event("disconnect", uid, "in_menu_none_timeout=120")
                                self._menu_none_disconnect_fired_by_uid.add(uid)
                    else:
                        self._menu_none_since_by_uid.pop(uid, None)
                        self._menu_none_disconnect_fired_by_uid.discard(uid)
            except Exception:
                pass

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

    def export_state(self) -> dict:
        """
        Export a JSON-serializable snapshot of MultiScope runtime state.
        Used by GUI Pause/Resume so in-menu + last biome/merchant state isn't reset.
        """
        out: dict = {"version": 1, "ts": time.time()}
        with self._lock:
            try:
                known_uids = set(self._cur.keys())
            except Exception:
                known_uids = set()

            scopes: dict = {}
            for key, s in (self._scopes or {}).items():
                try:
                    k = str(key)
                except Exception:
                    continue

                try:
                    users = [str(u) for u in (s.users or set()) if not known_uids or str(u) in known_uids]
                except Exception:
                    users = []

                try:
                    scopes[k] = {
                        "key": k,
                        "users": users,
                        "last_biome": (str(s.last_biome) if s.last_biome else ""),
                        "last_biome_ts": float(getattr(s, "last_biome_ts", 0.0) or 0.0),
                        "last_merchant": (str(s.last_merchant) if s.last_merchant else ""),
                        "last_merchant_ts": float(getattr(s, "last_merchant_ts", 0.0) or 0.0),
                        "in_menu": (None if getattr(s, "in_menu", None) is None else bool(getattr(s, "in_menu", None))),
                        "last_menu_ts": float(getattr(s, "last_menu_ts", 0.0) or 0.0),
                        "events": int(getattr(s, "events", 0) or 0),
                        "next_tail_at": float(getattr(s, "next_tail_at", 0.0) or 0.0),
                        "poll_rot": int(getattr(s, "poll_rot", 0) or 0),
                    }
                except Exception:
                    continue

            cursors: dict = {}
            for uid, cur in (self._cur or {}).items():
                try:
                    u = str(uid)
                except Exception:
                    continue
                if known_uids and u not in known_uids:
                    continue
                try:
                    cursors[u] = {
                        "path": (str(cur.path) if getattr(cur, "path", None) else None),
                        "pos": int(getattr(cur, "pos", 0) or 0),
                        "carry": str(getattr(cur, "carry", "") or ""),
                    }
                except Exception:
                    continue

            out["scopes"] = scopes
            out["cursors"] = cursors

            try:
                out["handoffs"] = {str(k): str(v) for k, v in (self._handoffs or {}).items()}
            except Exception:
                out["handoffs"] = {}
            try:
                out["handoff_prev_biome_for_spare"] = {
                    str(k): str(v) for k, v in (self._handoff_prev_biome_for_spare or {}).items()
                }
            except Exception:
                out["handoff_prev_biome_for_spare"] = {}

            # Best-effort dedupe/throttle caches (safe to omit if they fail)
            try:
                out["last_biome_post_by_scope"] = {
                    str(k): float(v) for k, v in (self._last_biome_post_by_scope or {}).items()
                }
            except Exception:
                out["last_biome_post_by_scope"] = {}
            try:
                out["last_merchant_ts_by_scope"] = {
                    str(scope): {str(m): float(ts) for m, ts in (mm or {}).items()}
                    for scope, mm in (self._last_merchant_ts_by_scope or {}).items()
                }
            except Exception:
                out["last_merchant_ts_by_scope"] = {}
            try:
                out["first_merchant_scan_done"] = [
                    str(u)
                    for u in (self._first_merchant_scan_done or set())
                    if not known_uids or str(u) in known_uids
                ]
            except Exception:
                out["first_merchant_scan_done"] = []
            try:
                out["last_disconnect_sig_by_uid"] = {
                    str(uid): [str(sig[0]), int(sig[1])]
                    for uid, sig in (self._last_disconnect_sig_by_uid or {}).items()
                    if not known_uids or str(uid) in known_uids
                }
            except Exception:
                out["last_disconnect_sig_by_uid"] = {}
            try:
                out["ignored_logs_by_uid"] = {
                    str(uid): sorted(list(paths or []))
                    for uid, paths in (self._ignored_logs_by_uid or {}).items()
                }
            except Exception:
                out["ignored_logs_by_uid"] = {}

        return out

    def import_state(self, state: dict) -> bool:
        """
        Restore a previously-exported runtime snapshot.
        Returns True if anything was applied.
        """
        if not isinstance(state, dict) or not state:
            return False
        try:
            ver = int(state.get("version", 0) or 0)
        except Exception:
            ver = 0
        if ver != 1:
            return False

        applied = False
        with self._lock:
            try:
                known_uids = set(self._cur.keys())
            except Exception:
                known_uids = set()

            # -- Scopes -------------------------------------------------------
            scopes_in = state.get("scopes") or {}
            if isinstance(scopes_in, dict) and scopes_in:
                for key, raw in scopes_in.items():
                    if not isinstance(raw, dict):
                        continue
                    k = str(key)
                    scope = self._scopes.get(k) or ServerScope(k)
                    try:
                        users_raw = raw.get("users") or []
                        if isinstance(users_raw, (list, tuple, set)):
                            scope.users = {str(u) for u in users_raw if not known_uids or str(u) in known_uids}
                    except Exception:
                        pass
                    try:
                        b = str(raw.get("last_biome") or "").strip().upper()
                        scope.last_biome = b or None
                    except Exception:
                        pass
                    try:
                        scope.last_biome_ts = float(raw.get("last_biome_ts", scope.last_biome_ts) or 0.0)
                    except Exception:
                        pass
                    try:
                        m = str(raw.get("last_merchant") or "").strip().title()
                        scope.last_merchant = m or None
                    except Exception:
                        pass
                    try:
                        scope.last_merchant_ts = float(raw.get("last_merchant_ts", scope.last_merchant_ts) or 0.0)
                    except Exception:
                        pass
                    try:
                        val = raw.get("in_menu", None)
                        scope.in_menu = None if val is None else bool(val)
                    except Exception:
                        pass
                    try:
                        scope.last_menu_ts = float(raw.get("last_menu_ts", scope.last_menu_ts) or 0.0)
                    except Exception:
                        pass
                    try:
                        scope.events = int(raw.get("events", scope.events) or 0)
                    except Exception:
                        pass
                    try:
                        scope.next_tail_at = float(raw.get("next_tail_at", scope.next_tail_at) or 0.0)
                    except Exception:
                        pass
                    try:
                        scope.poll_rot = int(raw.get("poll_rot", scope.poll_rot) or 0)
                    except Exception:
                        pass

                    self._scopes[k] = scope
                    applied = True

            # -- Cursors (pos/carry only when path matches current) ----------
            cursors_in = state.get("cursors") or {}
            if isinstance(cursors_in, dict) and cursors_in:
                import os
                for uid, raw in cursors_in.items():
                    u = str(uid)
                    if known_uids and u not in known_uids:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    cur = self._cur.get(u)
                    if not cur:
                        continue
                    try:
                        snap_path = raw.get("path")
                        cur_path = getattr(cur, "path", None)
                        if snap_path and cur_path:
                            sp = os.path.normcase(os.path.abspath(str(snap_path)))
                            cp = os.path.normcase(os.path.abspath(str(cur_path)))
                            if sp != cp:
                                continue
                        elif snap_path or cur_path:
                            continue
                    except Exception:
                        continue

                    try:
                        cur.pos = int(raw.get("pos", getattr(cur, "pos", 0)) or 0)
                    except Exception:
                        pass
                    try:
                        cur.carry = str(raw.get("carry", "") or "")
                    except Exception:
                        pass
                    applied = True

            # -- Handoffs -----------------------------------------------------
            try:
                h = state.get("handoffs") or {}
                if isinstance(h, dict):
                    self._handoffs = {
                        str(k): str(v)
                        for k, v in h.items()
                        if not known_uids or (str(k) in known_uids and str(v) in known_uids)
                    }
                    applied = True
            except Exception:
                pass
            try:
                hb = state.get("handoff_prev_biome_for_spare") or {}
                if isinstance(hb, dict):
                    self._handoff_prev_biome_for_spare = {
                        str(k): str(v) for k, v in hb.items() if not known_uids or str(k) in known_uids
                    }
                    applied = True
            except Exception:
                pass

            # -- Dedupe/throttle caches --------------------------------------
            try:
                lbp = state.get("last_biome_post_by_scope") or {}
                if isinstance(lbp, dict):
                    self._last_biome_post_by_scope = {str(k): float(v) for k, v in lbp.items()}
                    applied = True
            except Exception:
                pass
            try:
                lmt = state.get("last_merchant_ts_by_scope") or {}
                if isinstance(lmt, dict):
                    merged: dict = {}
                    for scope, mm in lmt.items():
                        if not isinstance(mm, dict):
                            continue
                        merged[str(scope)] = {str(m): float(ts) for m, ts in mm.items()}
                    self._last_merchant_ts_by_scope = merged
                    applied = True
            except Exception:
                pass
            try:
                fms = state.get("first_merchant_scan_done") or []
                if isinstance(fms, (list, tuple, set)):
                    self._first_merchant_scan_done = {
                        str(u) for u in fms if not known_uids or str(u) in known_uids
                    }
                    applied = True
            except Exception:
                pass
            try:
                lds = state.get("last_disconnect_sig_by_uid") or {}
                if isinstance(lds, dict):
                    out = {}
                    for uid, sig in lds.items():
                        u = str(uid)
                        if known_uids and u not in known_uids:
                            continue
                        if isinstance(sig, (list, tuple)) and len(sig) == 2:
                            out[u] = (str(sig[0]), int(sig[1]))
                    self._last_disconnect_sig_by_uid = out
                    applied = True
            except Exception:
                pass
            try:
                ilb = state.get("ignored_logs_by_uid") or {}
                if isinstance(ilb, dict):
                    out: Dict[str, Set[str]] = {}
                    for uid, paths in ilb.items():
                        u = str(uid)
                        vals: Set[str] = set()
                        if isinstance(paths, (list, tuple, set)):
                            for p in paths:
                                np = self._normalize_log_path(str(p))
                                if np:
                                    vals.add(np)
                        elif isinstance(paths, str):
                            np = self._normalize_log_path(paths)
                            if np:
                                vals.add(np)
                        if vals:
                            out[u] = vals
                    self._ignored_logs_by_uid = out
                    applied = True
            except Exception:
                pass

        return applied

    def snapshot(self) -> List[dict]:
        out: List[dict] = []
        now_t = time.time()
        for key, s in sorted(self._scopes.items(), key=lambda kv: kv[0]):
            server_label = self._display_server_label(key)
            out.append({
                "server": server_label,
                "server_key": key,
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

    def _should_disconnect_lookback(self, uid: str) -> bool:
        """
        Guard for disconnect lookback scans:
        - Avoid triggering restarts for idle users when MultiScope starts.
        - Allow for users that are active OR recently active (PID died but log flushed a disconnect).
        """
        try:
            key = self._server_key_for(uid)
            if key == "Disconnected":
                return False
        except Exception:
            pass

        try:
            st = (self._status_snapshot or {}).get(uid)
        except Exception:
            st = None
        if not isinstance(st, dict):
            return False

        try:
            pids = st.get("pids") or []
        except Exception:
            pids = []
        if pids:
            return True

        try:
            last_active = float(st.get("last_active", 0) or 0)
        except Exception:
            last_active = 0.0

        try:
            now_t = float(getattr(self, "_status_snapshot_ts", 0.0) or 0.0) or time.time()
        except Exception:
            now_t = time.time()

        # If the user was active recently, still allow the lookback (disconnect may have been written
        # before we switched to the newest log file, or after the PID already died).
        return bool(last_active and (now_t - last_active) <= 180.0)
    
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
        try:
            with self._stats_lock:
                self._prune_found_events_locked(now_ts=time.time())
                self._save_found_stats_locked()
        except Exception:
            pass








