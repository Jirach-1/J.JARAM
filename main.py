import psutil
import os
import time
import win32gui
import win32process
import random
import requests
import json
import shutil
from log_utils import find_log_for_username, refresh_username_log_map
from pathlib import Path
from collections import defaultdict

try:
    from gui import ConfigManager
except ImportError:


    
    def limit_strap_helpers(threshold: int = 50, *, kill_all: bool = False) -> None:
        """
        Trim *-strap.exe* helpers.

        • kill_all = False  ➜ keep the **oldest** helper and terminate any
        extras once the running count reaches or exceeds *threshold*.
        • kill_all = True   ➜ terminate **every** helper.

        Pass threshold=1 to “kill all but oldest” unconditionally.
        """
        helpers = [
            p for p in psutil.process_iter(['name', 'create_time'])
            if (n := p.info['name']) and n.lower().endswith('strap.exe')
        ]
        if not helpers:
            return

        if kill_all:
            for p in helpers:
                try:
                    p.kill()
                except Exception:
                    pass
            return

        if len(helpers) < threshold:
            return                                    # nothing to trim

        helpers.sort(key=lambda p: p.info['create_time'])  # oldest first
        for p in helpers[1:]:                         # keep index-0
            try:
                p.kill()
            except Exception:
                pass


    class ConfigManager:
        def __init__(self):
            self.app_name = "JARAM"
            self.config_dir = self._get_config_directory()
            self.users_file = self.config_dir / "users.json"
            self._ensure_directories()

        def _get_config_directory(self):
            if os.name == 'nt':
                appdata = os.environ.get('APPDATA')
                if appdata:
                    return Path(appdata) / self.app_name
            return Path.home() / f".{self.app_name.lower()}"

        def _ensure_directories(self):
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                pass
        # ── new ─────────────────────────────────────────────
        def _deep_update(self, base: dict, updates: dict):
            """Recursive dict.update so nested keys survive partial files."""
            for k, v in updates.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k] = self._deep_update(base[k], v)
                else:
                    base[k] = v
            return base

        def load_settings(self):
            try:
                if self.settings_file.exists():
                    with open(self.settings_file, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)

                    # start from defaults, then deep-merge file content
                    settings = json.loads(json.dumps(self.default_settings))  # deep copy
                    settings = self._deep_update(settings, loaded)
                    return settings
                else:
                    return json.loads(json.dumps(self.default_settings))
            except Exception:
                return json.loads(json.dumps(self.default_settings))

# ──────────────────────────────────────────────────────────────
# 1-A. RobloxManager – strip presence monitor & shorter loop
# ──────────────────────────────────────────────────────────────
class RobloxManager:
    def __init__(self, config_manager: "ConfigManager" = None):
        # use the GUI’s instance if one is provided
        self.config_manager = config_manager or ConfigManager()
        self.settings        = self._load_settings()
        self.process_tracker = ProcessTracker()
        self.auth_handler    = AuthenticationHandler()

        # ⬇ delete: self.presence_monitor = PresenceMonitor()

        app_settings = self._load_app_settings()
        self.target_place = "15532962292"
        self.window_limit = app_settings.get("window_limit", 1)

        # presence key removed, default tick every 2 s
        self.check_intervals = {
            'window'   : 3,
            'cleanup'  : 30,
            'main_tick': 2
        }

        timeouts = app_settings.get("timeouts", {})

        self.timeouts = {
            "relaunch"     : 20,
            "launch_delay" : timeouts.get("launch_delay", 4),
            "offline"      : timeouts.get("offline",      35),
            "initial_delay": timeouts.get("initial_delay",4)
        }

        self.excluded_pid = 0
        from timeout_monitor import TimeoutMonitor   # top-level import

        tm_cfg = app_settings.get("timeout_monitor", {})

        self.timeout_monitor = TimeoutMonitor(
            kill_timeout  = tm_cfg.get("kill_timeout", 1740),
            poll_interval = tm_cfg.get("poll_interval", 10),
            webhook_url   = tm_cfg.get("webhook_url", ""),
            ping_message  = tm_cfg.get("ping_message", "<@YourPing> This message is sent whenever your active processes drop to 1 or 0, for debugging, leave webhook empty if not interested"),
            kill_enabled  = bool(tm_cfg.get("kill_enabled", True))
        )


    def _load_settings(self):
        try:

            if hasattr(self.config_manager, 'get_users_for_manager'):
                return self.config_manager.get_users_for_manager()   # keep ALL users

            else:
                users = self.config_manager.load_users()

            if not users:
                return {}
            return users
        except Exception as error:
            return {}

    def _load_app_settings(self):
        try:
            if hasattr(self.config_manager, 'load_settings'):
                return self.config_manager.load_settings()
            else:

                return {
                    "window_limit": 1,
                    "timeouts": {
                        "offline": 35,
                        "launch_delay": 4
                    }
                }
        except Exception as error:
            return {
                "window_limit": 1,
                "timeouts": {
                    "offline": 35,
                    "launch_delay": 4
                }
            }

class ProcessTracker:
    def __init__(self):
        from collections import defaultdict
        self.user_processes = defaultdict(list)   # user_id -> [pids]
        self.process_owners = {}                  # pid -> user_id
        self.creation_timestamps = {}             # pid -> create_time
        self.user_server = {}                     # user_id -> human label of server joined
        self.protection_period = 60               # seconds to protect very new PIDs from aggression
        self.initialization_mode = False
    
        # NEW: per-user resolved private server OWNER username
        self.server_owner = {}                    # user_id -> owner username
        
        # NEW: cache the exact PS link code and place used at launch
        self.user_ps_code  = {}   # user_id -> full linkCode string
        self.user_ps_place = {}   # user_id -> placeId string
        # Tracks short-lived reservations so normal launches avoid in-flight handoffs
        self.reserved_servers = {}   # label -> {"by": uid, "type": "handoff"|"normal", "exp": epoch}
        # throttle normal-launch retries when a target server is occupied (per-user TTL)
        self.skip_until_by_user = {}   # uid -> epoch seconds
        # map share-code -> {"place": "...", "link": "..."} once any user resolves it
        self.share_to_link = {}   # e.g. {"A1B2C3D4": {"place": "15532962292", "link": "0669103657"}}




class AuthenticationHandler:
    def __init__(self):
        self.token_cache = {}

    def retrieve_csrf_token(self, cookie):
        if cookie in self.token_cache and self.token_cache[cookie]["expires"] > time.time():
            return self.token_cache[cookie]["token"]

        session = requests.Session()
        session.cookies[".ROBLOSECURITY"] = cookie
        session.headers.update({
            "Referer": "https://www.roblox.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        })

        try:
            response = session.post("https://auth.roblox.com/v1/authentication-ticket", timeout=5)
            if response.status_code == 403 and "x-csrf-token" in response.headers:
                token = response.headers["x-csrf-token"]
                self.token_cache[cookie] = {
                    "token": token,
                    "expires": time.time() + 1800
                }
                return token
        except Exception as error:
            pass
        return None

    def obtain_auth_ticket(self, cookie):
        session = requests.Session()
        session.headers.update({
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Referer": "https://www.roblox.com/",
            "User-Agent": "Roblox/WinInet"
        })

        try:
            response = session.post("https://auth.roblox.com/v1/authentication-ticket", timeout=5)
            if response.status_code == 403 and "x-csrf-token" in response.headers:
                csrf_token = response.headers["x-csrf-token"]
                session.headers.update({
                    "X-CSRF-TOKEN": csrf_token,
                    "Content-Type": "application/json"
                })
                second_response = session.post("https://auth.roblox.com/v1/authentication-ticket", timeout=5)
                ticket = second_response.headers.get("rbx-authentication-ticket")
                if ticket:
                    return ticket
        except Exception as error:
            pass
        return None

# ──────────────────────────────────────────────────────────────
# 1-B. presence monitor class – delete the whole class
#     (PresenceMonitor … end)
# ──────────────────────────────────────────────────────────────

class ProcessManager:
    def __init__(self, excluded_pid=0):
        self.excluded_pid = excluded_pid
        self.process_name = "RobloxPlayerBeta.exe"

    def is_game_active(self):
        for process in psutil.process_iter(['name', 'pid']):
            if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                return True
        return False

    def terminate_process(self, pid=None, tracker=None):
        if pid:
            try:
                process = psutil.Process(pid)
                # (optional) protect the launcher itself
                if pid == self.excluded_pid:
                    return False
                # primary method
                rc = os.system(f"taskkill /F /PID {pid}")
                if rc != 0:              # taskkill failed – try psutil
                    process.kill()
                # … tracker-cleanup exactly as before …
                    if tracker and pid in tracker.process_owners:
                        user_id = tracker.process_owners[pid]
                        if pid in tracker.user_processes[user_id]:
                            tracker.user_processes[user_id].remove(pid)
                        del tracker.process_owners[pid]
                    if tracker and pid in tracker.creation_timestamps:
                        del tracker.creation_timestamps[pid]
                    return True
            except psutil.NoSuchProcess:
                if tracker and pid in tracker.process_owners:
                    user_id = tracker.process_owners[pid]
                    if pid in tracker.user_processes[user_id]:
                        tracker.user_processes[user_id].remove(pid)
                    del tracker.process_owners[pid]
                if tracker and pid in tracker.creation_timestamps:
                    del tracker.creation_timestamps[pid]
            return False
        else:
            terminated = False
            for process in psutil.process_iter(['pid', 'name']):
                if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                    pid = process.info['pid']
                    os.system(f"taskkill /F /PID {pid}")

                    if tracker and pid in tracker.process_owners:
                        user_id = tracker.process_owners[pid]
                        if pid in tracker.user_processes[user_id]:
                            tracker.user_processes[user_id].remove(pid)
                        del tracker.process_owners[pid]
                    if tracker and pid in tracker.creation_timestamps:
                        del tracker.creation_timestamps[pid]
                    terminated = True
            return terminated

    def count_windows_by_process(self):
        active_pids = []
        for process in psutil.process_iter(['pid', 'name']):
            if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                active_pids.append(process.info['pid'])

        window_counts = defaultdict(int)

        def window_callback(hwnd, extra):
            if win32gui.IsWindowVisible(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in active_pids:
                    window_counts[pid] += 1

        win32gui.EnumWindows(window_callback, None)
        return window_counts

    def verify_process_active(self, pid):
        try:
            process = psutil.Process(pid)
            return process.name() == self.process_name and pid != self.excluded_pid
        except psutil.NoSuchProcess:
            return False

    def await_new_process(self, user_id, launch_timestamp, timeout, tracker):
        start_time = time.time()

        while time.time() - start_time < timeout:
            for process in psutil.process_iter(['pid', 'name', 'create_time']):
                if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                    pid = process.info['pid']
                    create_time = process.info['create_time']

                    if create_time > launch_timestamp and pid not in tracker.process_owners:
                        tracker.process_owners[pid] = user_id
                        tracker.user_processes[user_id].append(pid)
                        tracker.creation_timestamps[pid] = create_time
                        return pid

            time.sleep(0.5)

        return None

    def cleanup_dead_processes(self, tracker):
        active_pids = set()
        for process in psutil.process_iter(['pid', 'name']):
            if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                active_pids.add(process.info['pid'])

        dead_pids = set(tracker.process_owners.keys()) - active_pids

        for pid in dead_pids:
            user_id = tracker.process_owners[pid]
            if pid in tracker.user_processes.get(user_id, []):
                tracker.user_processes[user_id].remove(pid)
            del tracker.process_owners[pid]
            if pid in tracker.creation_timestamps:
                del tracker.creation_timestamps[pid]
        
        # NEW: move users with no live processes into the disconnected pool
        for uid, lst in list(tracker.user_processes.items()):
            if not lst:
                tracker.user_server[uid] = "DISCONNECTED"

    def eliminate_orphaned_processes(self, tracker, valid_users):
        eliminated = False
        current_time = time.time()

        if tracker.initialization_mode:
            return False

        for process in psutil.process_iter(['pid', 'name', 'create_time']):
            if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                pid = process.info['pid']
                process_create_time = process.info['create_time']

                if current_time - process_create_time < tracker.protection_period:
                    continue

                if pid not in tracker.process_owners:
                    self.terminate_process(pid, tracker)
                    eliminated = True
                elif tracker.process_owners[pid] not in valid_users:
                    self.terminate_process(pid, tracker)
                    eliminated = True

        return eliminated

class GameLauncher:
    def __init__(self,
                 target_place,
                 process_mgr,
                 auth_handler,
                 process_tracker,
                 config_mgr,
                 launch_delay=4,
                 initial_delay=4,
                 log_fn=None):
        self.target_place     = target_place
        self.process_manager  = process_mgr
        self.auth_handler     = auth_handler
        self.tracker          = process_tracker
        self.cfg              = config_mgr

        self.launch_delay  = launch_delay
        self.initial_delay = initial_delay
        self.process_timeout = 20
        self.process_timeout = 20
        self.log = log_fn or print
        self._skip_log_until = {}  # (uid, label) -> epoch seconds


    def _extract_private_server_info(self, private_server_link, cookie=None):
        import re
        if not private_server_link:
            return None, None, "direct"

        pattern1 = r'roblox\.com/games/(\d+)/[^?]*\?privateServerLinkCode=([A-Za-z0-9_-]+)'
        m1 = re.search(pattern1, private_server_link)
        if m1:
            return m1.group(1), m1.group(2), "direct"

        pattern2 = r'roblox\.com/share\?code=([A-Za-z0-9_-]+)&type=Server'
        m2 = re.search(pattern2, private_server_link)
        if m2:
            share_code = m2.group(1)
            if cookie:
                p, code = self._convert_share_link(share_code, cookie)
                if p and code:
                    return p, code, "resolved"
                return None, share_code, "share"
            return None, share_code, "share"
        return None, None, "invalid"
    
    # main.py — inside class GameLauncher
    def log_skip(self, user_id: str, server_label: str, reason: str, throttle: float = 8.0) -> None:
        """
        Consistent, throttled [LAUNCH SKIP] logging usable from preflight checks.
        Keyed by (uid, label, reason) so you still see different reasons.
        """
        now = time.time()
        key = (user_id, server_label, reason)
        if self._skip_log_until.get(key, 0.0) <= now:
            self._skip_log_until[key] = now + float(throttle or 0)
            self.log(f"[LAUNCH SKIP] {user_id} -> {server_label} {reason}")


    def _convert_share_link(self, share_code, cookie):
        import requests, json
        if not share_code or not cookie:
            return None, None
        url = "https://apis.roblox.com/sharelinks/v1/resolve-link"
        payload = {"linkId": share_code, "linkType": "Server"}
        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
        s.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.roblox.com/"
        })
        try:
            r = s.post(url, json=payload, timeout=10)
            if r.status_code == 403:
                csrf = r.headers.get("X-CSRF-TOKEN")
                if csrf:
                    s.headers["X-CSRF-TOKEN"] = csrf
                    r = s.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                data = r.json()
                invite = data.get("privateServerInviteData") or {}
                place = str(invite.get("placeId") or "")
                link  = invite.get("linkCode")
                # NEW: remember this mapping globally so other users can compare labels
                try:
                    if link:
                        self.tracker.share_to_link[share_code] = {"place": place, "link": link}
                except Exception:
                    pass
                return place, link
        except Exception:
            pass
        return None, None
    
    def compute_server_label(self, user_info: dict, cookie: str) -> str:
        """
        Return the exact server label we use at launch time:
        - Private server => first 10 chars of *linkCode* (not share code)
        - Public => 'Public:<placeId>'
        """
        psl = (user_info.get("private_server_link") or "").strip() if isinstance(user_info, dict) else ""
        place_cfg = user_info.get("place") if isinstance(user_info, dict) else None

        # Parse quickly
        p, code, ltype = self._extract_private_server_info(psl, cookie)

        # If it's a SHARE link, prefer any previously learned mapping → linkCode
        if ltype == "share" and code:
            try:
                m = self.tracker.share_to_link.get(code)
            except Exception:
                m = None
            if m and m.get("link"):
                # adopt mapped values (now equivalent to a resolved direct link)
                p, code, ltype = (m.get("place") or ""), (m.get("link") or ""), "resolved"
            else:
                # try to resolve with the current cookie (may fail — that's fine)
                rp, rc = self._convert_share_link(code, cookie)
                if rp and rc:
                    p, code, ltype = rp, rc, "resolved"

        target_place = str(p or place_cfg or self.target_place)
        return (code[:10] if code else f"Public:{target_place}")


    def start_game_session(self, user_id, cookie, user_info=None, skip_cleanup=False):
        import os, time, random

        launch_ts = time.time()

        # pull original config
        psl = ""
        if user_info and isinstance(user_info, dict):
            psl = user_info.get("private_server_link", "")

        # parse target place / link-code (resolve share links early)
        place_id, private_code, link_type = self._extract_private_server_info(psl, cookie)
        if link_type == "share" and private_code:
            rp, rc = self._convert_share_link(private_code, cookie)
            if rp and rc:
                place_id, private_code, link_type = rp, rc, "resolved"

        user_place_cfg = user_info.get("place") if isinstance(user_info, dict) else None
        target_place = place_id or user_place_cfg or self.target_place

        # server label (keep short-code for PS; public = place)
        server_label = (f"{(private_code or '')[:10]}" if private_code else f"Public:{target_place}")

        # ---- Reservation guard (prevents races vs handoff pre-joins) --------------
        allow_shared = bool((user_info or {}).get("allow_shared_server"))
        r = None  # IMPORTANT: always initialize so allow_shared=True doesn't break
        if not allow_shared:
            rs = getattr(self.tracker, "reserved_servers", {})
            r = rs.get(server_label)
        if r is not None and r.get("by") != user_id and r.get("exp", 0) > time.time():
            # throttle logging & backoff
            now = time.time()
            key = (user_id, server_label)
            if self._skip_log_until.get(key, 0) <= now:
                self._skip_log_until[key] = now + 8
                self.log(f"[LAUNCH SKIP] {user_id} -> {server_label} reserved by {r.get('by')} ({r.get('type')})")
            try:
                self.tracker.skip_until_by_user[user_id] = now + 10
            except Exception:
                pass
            return False

        # ---- One-per-server guard (live occupants) --------------------------------
        if not allow_shared:
            for other_uid, other_label in (self.tracker.user_server or {}).items():
                if other_uid != user_id and other_label == server_label:
                    now = time.time()
                    key = (user_id, server_label)
                    if self._skip_log_until.get(key, 0) <= now:
                        self._skip_log_until[key] = now + 8
                        self.log(f"[LAUNCH SKIP] {user_id} -> {server_label} already occupied by {other_uid}")
                    try:
                        self.tracker.skip_until_by_user[user_id] = now + 30
                    except Exception:
                        pass
                    return False
        # ---- Build auth + URL ------------------------------------------------------
        auth_ticket = self.auth_handler.obtain_auth_ticket(cookie)
        if not auth_ticket:
            self.cfg.mark_bad_cookie(user_id, True)
            if user_info is not None:
                user_info["bad"] = True
                user_info["inactive_since"] = time.time()
            return False

        browser_id = f"{random.randint(100000,130000)}{random.randint(100000,900000)}"
        if private_code:
            launcher_url = (
                "https://assetgame.roblox.com/game/PlaceLauncher.ashx"
                f"?request=RequestPrivateGame&placeId={target_place}&linkCode={private_code}"
            )
        else:
            launcher_url = (
                "https://assetgame.roblox.com/game/PlaceLauncher.ashx"
                f"?request=RequestGame&placeId={target_place}"
            )

        game_url = (
            "roblox-player://1/1+launchmode:play"
            f"+gameinfo:{auth_ticket}"
            f"+launchtime:{int(launch_ts * 1000)}"
            f"+browsertrackerid:{browser_id}"
            f"+placelauncherurl:{launcher_url}"
            "+robloxLocale:en_us+gameLocale:en_us"
        )

        try:
            if not skip_cleanup:
                for pid in self.tracker.user_processes.get(user_id, []).copy():
                    if pid != self.process_manager.excluded_pid:
                        self.process_manager.terminate_process(pid, self.tracker)

            os.startfile(game_url)
            new_pid = self.process_manager.await_new_process(user_id, launch_ts, self.process_timeout, self.tracker)
            if new_pid:
                # clear bad flag if we just launched fine
                if user_info and user_info.get("bad", False):
                    self.cfg.mark_bad_cookie(user_id, False)
                    user_info["bad"] = False

                # record the live server label/code/place
                self.tracker.user_server[user_id]    = server_label
                self.tracker.user_ps_place[user_id]  = str(target_place)
                self.tracker.user_ps_code[user_id]   = private_code or ""

                # cache resolved owner
                owner_username = self._find_ps_owner_username(psl, private_code)
                if not owner_username and isinstance(user_info, dict):
                    owner_username = (user_info.get("username") or "").strip()
                self.tracker.server_owner[user_id] = owner_username or ""

                # release any reservation held by this uid (if present)
                try:
                    rs = getattr(self.tracker, "reserved_servers", {})
                    for lbl, meta in list(rs.items()):
                        if meta.get("by") == user_id:
                            rs.pop(lbl, None)
                except Exception:
                    pass

                return True

            # failed to see a process — leave cleanup to caller/TTL
            return False

        except Exception:
            # on exception, do nothing; TTL will prune any stale reservations
            return False


    def initialize_all_sessions(self, user_configs: dict):
        import time
        self.tracker.initialization_mode = True
        try:
            for idx, (user_id, user_info) in enumerate(user_configs.items()):
                if user_info.get("bad", False):
                    continue
                cookie = user_info.get("cookie", "") if isinstance(user_info, dict) else user_info
                for pid in self.tracker.user_processes.get(user_id, []).copy():
                    if self.process_manager.verify_process_active(pid):
                        self.process_manager.terminate_process(pid, self.tracker)
                self.start_game_session(user_id, cookie, user_info, skip_cleanup=True)
                if idx < len(user_configs) - 1:
                    time.sleep(self.initial_delay)
        finally:
            self.tracker.initialization_mode = False
        # --- PS owner resolution helpers ----------------------------------------

    def _extract_code_quick(self, link: str) -> str:
        """Best-effort parse of a private server code from a link (no network)."""
        if not link:
            return ""
        import re
        # Direct link: ...?privateServerLinkCode=XXXXXXXX
        m = re.search(r'privateServerLinkCode=([A-Za-z0-9_-]+)', link)
        if m:
            return m.group(1)
        # Share link: .../share?code=XXXXXXXX&type=Server
        m = re.search(r'/share\?code=([A-Za-z0-9_-]+)&type=Server', link)
        if m:
            return m.group(1)
        return ""

    def _find_ps_owner_username(self, psl: str, private_code: str = "") -> str:
        """
        Determine the PS owner by comparing the current link/code to the users.json entries.
        Rule: the user whose configured private_server_link matches (by exact link OR by code)
              is considered the owner.
        """
        try:
            users = self.cfg.load_users() or {}
        except Exception:
            users = {}

        # Normalize target
        target_code = (private_code or self._extract_code_quick(psl) or "").strip()
        target_link = (psl or "").strip()

        # 1) Exact link match
        if target_link:
            for _, info in users.items():
                if isinstance(info, dict) and (info.get("private_server_link") or "").strip() == target_link:
                    return (info.get("username") or "").strip()

        # 2) Code match
        if target_code:
            for _, info in users.items():
                link = (info.get("private_server_link") or "").strip()
                if not link:
                    continue
                code = self._extract_code_quick(link)
                if code and code == target_code:
                    return (info.get("username") or "").strip()

        return ""



# ──────────────────────────────────────────────────────────────
# 1-C. execute_main_loop – new “process-only” heartbeat
# ──────────────────────────────────────────────────────────────
def execute_main_loop():
    manager      = RobloxManager()
    process_mgr  = ProcessManager(manager.excluded_pid)
    launcher = GameLauncher(
        manager.target_place,
        process_mgr,
        manager.auth_handler,
        manager.process_tracker,
        manager.config_manager,
        launch_delay=manager.timeouts["launch_delay"],
        initial_delay=manager.timeouts["initial_delay"]
)


    # track the last launch so we honour launch_delay
    user_state = {
        uid: {"last_launch": 0,
              "user_info" : info}
        for uid, info in manager.settings.items()
    }

    # fire everything once on boot
    launcher.initialize_all_sessions(manager.settings)
    for uid in user_state:
        user_state[uid]["last_launch"] = time.time()

    # ───── main loop ─────
    tickers = {'window': 0, 'cleanup': 0}
    while True:
        now = time.time()

        # housekeeping
        if now - tickers['cleanup'] >= manager.check_intervals['cleanup']:
            process_mgr.cleanup_dead_processes(manager.process_tracker)
            process_mgr.eliminate_orphaned_processes(
                manager.process_tracker, set(manager.settings.keys())
            )
            tickers['cleanup'] = now

        if now - tickers['window'] >= manager.check_intervals['window']:
            for pid, nwin in process_mgr.count_windows_by_process().items():
                if nwin > manager.window_limit and pid != manager.excluded_pid:
                    process_mgr.terminate_process(pid, manager.process_tracker)
            tickers['window'] = now
            
        # --- NEW: pre-connect watchdog (headless) -----------------------------
        try:
            refresh_username_log_map()  # make sure strict map is fresh
        except Exception:
            pass

        now = time.time()
        PRECONNECT_GRACE = 120  # seconds

        for uid, pids in list(manager.process_tracker.user_processes.items()):
            live_pids = [pid for pid in pids if process_mgr.verify_process_active(pid)]
            if not live_pids:
                continue

            info = manager.settings.get(uid, {}) or {}
            uname = str(info.get("username", "")).lower()
            if not uname:
                continue  # nothing to check

            log_path = find_log_for_username(uname, allow_fallback=False)
            if not log_path:
                oldest_ct = min(manager.process_tracker.creation_timestamps.get(pid, now) for pid in live_pids)
                waited = now - oldest_ct
                if waited >= PRECONNECT_GRACE:
                    # failed to ever attach to a log with the username — recycle it
                    for pid in live_pids:
                        process_mgr.terminate_process(pid, manager.process_tracker)
                    manager.process_tracker.user_server[uid] = "DISCONNECTED"
        # --- END new watchdog --------------------------------------------------

        # --- build eligible candidates for this tick ---------------------------------
        eligible = []   # list of tuples (uid, st, cookie, info, server_label)

        # snapshot once for speed/readability
        servers_live = dict(manager.process_tracker.user_server or {})
        skip_ttl     = dict(manager.process_tracker.skip_until_by_user or {})
        reserved     = dict(manager.process_tracker.reserved_servers or {})

        for uid, st in user_state.items():
            # 1) live process? then skip (but keep 'Disconnected' marker fresh)
            live_pids = [pid for pid in manager.process_tracker.user_processes.get(uid, [])
                        if process_mgr.verify_process_active(pid)]
            if not live_pids:
                manager.process_tracker.user_server[uid] = "DISCONNECTED"
            if live_pids:
                continue

            # 2) honor the global launch_delay
            if (now - st["last_launch"]) < manager.timeouts['launch_delay']:
                continue

            # 3) per-user backoff after a skip
            if now < skip_ttl.get(uid, 0):
                continue

            # 4) compute target label exactly as launcher will
            info   = st["user_info"] if isinstance(st["user_info"], dict) else {}
            cookie = info.get("cookie", "") if isinstance(info, dict) else info
            server_label = launcher.compute_server_label(info, cookie)

            # 5) preflight checks with LOGGING (do NOT bump last_launch on skips)
            #    (a) already occupied by someone else?
            occupied_by = next(
                (other_uid for other_uid, other_label in servers_live.items()
                if other_uid != uid and other_label == server_label),
                None
            )
            if occupied_by:
                manager.process_tracker.skip_until_by_user[uid] = now + 30
                launcher.log_skip(uid, server_label, f"already occupied by {occupied_by}")
                continue

            #    (b) reserved by an in-flight handoff/normal?
            r = reserved.get(server_label)
            if r and r.get("by") != uid and r.get("exp", 0) > now:
                manager.process_tracker.skip_until_by_user[uid] = now + 10
                launcher.log_skip(uid, server_label, f"reserved by {r.get('by')} ({r.get('type')})")
                continue

            #    (c) same-owner guard (avoid launching into a PS whose owner is already active)
            pslink = (info.get("private_server_link") or "").strip()
            owner  = launcher._find_ps_owner_username(
                pslink, "" if server_label.startswith("Public:") else server_label
            )
            if owner:
                owner_lc = owner.strip().lower()
                same_owner_live = any(
                    (other_uid != uid) and
                    ((manager.settings.get(other_uid, {}).get("username") or "").strip().lower() == owner_lc)
                    for other_uid in servers_live
                )
                if same_owner_live:
                    manager.process_tracker.skip_until_by_user[uid] = now + 30
                    launcher.log_skip(uid, server_label, "same owner already active")
                    continue

            # if we got here, this uid is a valid candidate for launching this tick
            eligible.append((uid, st, cookie, info, server_label))

        # --- deterministic: preserve dict insertion order; try up to N immediately ---
        MAX_TRIES = 3
        tries = 0
        for uid, st, cookie, info, server_label in eligible:
            if tries >= MAX_TRIES:
                break
            tries += 1
            ok = launcher.start_game_session(uid, cookie, info)
            st["last_launch"] = now           # bump ONLY on an actual attempt
            if ok:
                break                         # launched one → stop this tick

        time.sleep(manager.check_intervals['main_tick'])

if __name__ == "__main__":
    execute_main_loop()