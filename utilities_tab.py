# utilities_tab.py
# ──────────────────────────────────────────────────────────────────────────────
# JARAM Utilities Tab (Blocking / Unblocking / PSL Grabber)
# Self-contained: paths, JSON I/O, migration, Selenium helpers, QThreads, a
# progress dialog, and a single entrypoint: setup_UTILITIES_tab(self)
# Expects on MainWindow:
#   - self.append_log(str) method
#   - self.tab_widget (QTabWidget) to add the tab
# ──────────────────────────────────────────────────────────────────────────────

import os, json, time, urllib.parse as _urlparse, random, threading
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional, Set
from collections import defaultdict

import requests
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout, QLineEdit,
    QPushButton, QPlainTextEdit, QCheckBox, QLabel, QScrollArea,
    QDialog, QProgressBar
)

try:
    from roblox_cookie_utils import (
        extract_roblosecurity_from_requests_response,
        extract_roblosecurity_from_selenium_driver,
        normalize_roblosecurity_cookie_value,
        update_cookie_in_users_dict,
    )
except Exception:
    extract_roblosecurity_from_requests_response = None
    extract_roblosecurity_from_selenium_driver = None
    normalize_roblosecurity_cookie_value = None
    update_cookie_in_users_dict = None

# ---------------- Paths ----------------
def _appdata_dir() -> str:
    base = os.environ.get("APPDATA")
    if base:
        p = os.path.join(base, "JARAM")
        os.makedirs(p, exist_ok=True)
        return p
    p = os.path.join(os.getcwd(), "JARAM")
    os.makedirs(p, exist_ok=True)
    return p

def _users_json_path() -> str:
    return os.path.join(_appdata_dir(), "users.json")

def _block_log_path() -> str:
    return os.path.join(_appdata_dir(), "block_log.json")

def _persistent_blocklist_path() -> str:
    return os.path.join(_appdata_dir(), "users_to_block.txt")

# ---------------- Files ----------------
def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default

def _save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

_CONFIG_MANAGER = None

def _set_config_manager(config_manager) -> None:
    global _CONFIG_MANAGER
    _CONFIG_MANAGER = config_manager

def _load_users() -> Dict[str, Any]:
    if _CONFIG_MANAGER is not None:
        try:
            return _CONFIG_MANAGER.load_users() or {}
        except Exception:
            return {}
    return _load_json(_users_json_path(), {})

def _save_users(users: Dict[str, Any]) -> bool:
    if _CONFIG_MANAGER is not None:
        try:
            return bool(_CONFIG_MANAGER.save_users(users))
        except Exception:
            return False
    _save_json(_users_json_path(), users)
    return True

# ---------------- Block log schema (new) ----------------
# {
#   "by_user": { "<JARAM uid>": { "blocked": ["<target_id>", ...] } },
#   "username_cache": { "<inputUsername>": "12345" }
# }
def _load_blocklog_migrated(users: Dict[str, Any]) -> Dict[str, Any]:
    path = _block_log_path()
    old = _load_json(path, {})
    if "by_user" in old:
        return old

    by_user: Dict[str, Dict[str, List[str]]] = {}
    username_cache = old.get("username_cache", {})

    # Build reverse map cookie->uid from users.json
    cookie_to_uid: Dict[str, str] = {}
    for uid, data in (users or {}).items():
        c = str(data.get("cookie") or "")
        if c:
            cookie_to_uid[c] = str(uid)

    # Old format migration (best effort)
    for acc in old.get("valid", []):
        cookie = acc.get("cookie")
        blocked = [str(x) for x in acc.get("blocked", [])]
        uid = cookie_to_uid.get(cookie)
        if not uid:
            continue
        by_user.setdefault(uid, {}).setdefault("blocked", [])
        for t in blocked:
            if t not in by_user[uid]["blocked"]:
                by_user[uid]["blocked"].append(t)

    new_obj = {"by_user": by_user, "username_cache": username_cache}
    _save_json(path, new_obj)
    return new_obj

# ---------------- Roblox username resolution (original semantics) ----------------
def _resolve_username_single(username: str, cache: Dict[str, str]) -> Optional[str]:
    """
    Mirror original Blocker.py semantics:
    - If the exact input username is in cache, use it.
    - Otherwise resolve THIS username only, and store exactly under the input key.
    No lowercase duplicates, no canonical-name rewrites.
    """
    if username in cache:
        return str(cache[username])
    try:
        r = requests.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
            timeout=10,
        )
        data = (r.json() or {}).get("data", [])
        if data:
            uid = str(data[0].get("id"))
            if uid:
                cache[username] = uid  # store exactly as typed
                return uid
    except Exception:
        pass
    return None

def _coerce_targets_to_ids_and_names(lines: List[str], username_cache: Dict[str, str]):
    """
    Convert each input line to a numeric id.
    Returns:
      - ids: sorted list of unique target IDs
      - id_to_raw: mapping id -> set of raw inputs that produced it
      - unresolved: list of raw inputs we could not resolve
    """
    raw = [ln.strip().lstrip("@") for ln in lines if ln.strip()]
    ids: Set[str] = set()
    id_to_raw = defaultdict(set)
    unresolved: List[str] = []

    for entry in raw:
        if entry.isdigit():
            ids.add(entry)
            id_to_raw[entry].add(entry)
        else:
            uid = _resolve_username_single(entry, username_cache)
            if uid:
                ids.add(uid)
                id_to_raw[uid].add(entry)
            else:
                unresolved.append(entry)

    return sorted(ids), id_to_raw, unresolved

# ---------------- Selenium session ----------------
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def _make_driver(cookie: str, headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--start-maximized")
    d = webdriver.Chrome(options=opts)
    d.get("https://www.roblox.com/")
    d.add_cookie({
        "name": ".ROBLOSECURITY",
        "value": cookie,
        "domain": ".roblox.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    })
    d.get("https://www.roblox.com/home")
    try:
        WebDriverWait(d, 7).until(EC.invisibility_of_element_located((By.ID, "sign-up-button")))
    except Exception:
        d.quit()
        raise RuntimeError("Login failed (guest UI still present).")
    return d

# ---------------- Blocking flow ----------------
def _more_button(driver):
    locators = [
        (By.XPATH, "//button[@aria-label='See More']"),
        (By.XPATH, "//button[contains(@class,'icon-more')]"),
        (By.XPATH, "//button[.//span[contains(@class,'icon-more')]]"),
        (By.XPATH, "//button[.//span[normalize-space()='More']]"),
    ]
    for by, sel in locators:
        try:
            return WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, sel)))
        except Exception:
            continue
    return None

def _confirm_btn(driver, label: str, timeout: float = 3.0):
    try:
        return WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, f"//button[.//span[normalize-space()='{label}']]"))
        )
    except Exception:
        return None

def _block_user(driver, user_id: str) -> str:
    driver.get(f"https://www.roblox.com/users/{user_id}/profile")
    try:
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception:
        return "failed"
    if "/login" in driver.current_url.lower():
        return "bad_cookie"

    more = _more_button(driver)
    if not more:
        return "failed"
    driver.execute_script("arguments[0].click();", more)
    try:
        li = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, "block-button")))
        driver.execute_script("arguments[0].click();", li)
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", more)
            li = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.ID, "block-button")))
            driver.execute_script("arguments[0].click();", li)
        except Exception:
            return "failed"

    if _confirm_btn(driver, "Unblock", timeout=2.0):
        return "already_blocked"

    ok = _confirm_btn(driver, "Block", timeout=3.0)
    if ok:
        driver.execute_script("arguments[0].click();", ok)
        time.sleep(0.15)
        return "blocked"

    mb = _more_button(driver)
    if mb:
        driver.execute_script("arguments[0].click();", mb)
        if _confirm_btn(driver, "Unblock", timeout=1.5):
            return "already_blocked"
    return "failed"

# ---------------- Unblocking helpers (username-based DOM targeting) ----------------
_BLOCKED_URL = "https://www.roblox.com/my/account#!/privacy/BlockedUsers"

def _blocked_username_xpath(uname_lower: str) -> str:
    return (
        "//li[contains(@class,'blocked-users-item')]"
        "[.//div[contains(@class,'text-secondary') and "
        "translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')="
        f"'@{uname_lower}']]"
    )

def _find_blocked_node_by_name(driver, uname_lower: str, quick_only: bool = True):
    x_expr = _blocked_username_xpath(uname_lower)
    try:
        return WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.XPATH, x_expr))
        )
    except Exception:
        if quick_only:
            return None
        last_h = 0
        for _ in range(6):
            driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight*0.9));")
            time.sleep(0.15)
            try:
                return driver.find_element(By.XPATH, x_expr)
            except Exception:
                h = driver.execute_script("return document.body.scrollHeight;")
                if h == last_h:
                    break
                last_h = h
        return None

def _fully_load_then_find_by_name(driver, uname_lower: str):
    x_expr = _blocked_username_xpath(uname_lower)
    last_h = -1
    stable = 0
    for _ in range(12):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.2)
        h = driver.execute_script("return document.body.scrollHeight;")
        if h == last_h:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_h = h
    try:
        return driver.find_element(By.XPATH, x_expr)
    except Exception:
        return None

def _unblock_display_node(driver, node) -> bool:
    try:
        btn = node.find_element(By.CSS_SELECTOR, "button.user-blocking-btn")
        driver.execute_script("arguments[0].click();", btn)
        try:
            confirm = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'btn-primary') and normalize-space()='Unblock']"))
            )
            driver.execute_script("arguments[0].click();", confirm)
            try:
                WebDriverWait(driver, 6).until(EC.staleness_of(node))
                return True
            except Exception:
                pass
        except Exception:
            pass

        time.sleep(0.25)
        for _ in range(3):
            try:
                WebDriverWait(driver, 1.5).until(EC.staleness_of(node))
                return True
            except Exception:
                pass
            driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight*0.5));")
            time.sleep(0.15)

        try:
            uname_lower = node.find_element(By.CSS_SELECTOR, ".blocked-user-name .text-secondary").text.lstrip("@").lower()
            driver.find_element(By.XPATH, _blocked_username_xpath(uname_lower))
            return False
        except Exception:
            return True
    except Exception:
        return False

# ---------------- Roblox API (Blocking/Unblocking) ----------------
_BLOCKING_API_BASE = "https://apis.roblox.com/user-blocking-api/v1/users"
_CSRF_CACHE: Dict[str, Dict[str, Any]] = {}
_BROWSER_ID_CACHE: Dict[str, str] = {}
_API_CACHE_LOCK = threading.Lock()
_BLOCKING_CALL_DELAY_SEC = 1.0
_USER_SWAP_DELAY_SEC = 5.0


def _transfer_cookie_caches(old_cookie: str, new_cookie: str) -> None:
    old_cookie = str(old_cookie or "")
    new_cookie = str(new_cookie or "")
    if not old_cookie or not new_cookie or old_cookie == new_cookie:
        return
    with _API_CACHE_LOCK:
        try:
            if old_cookie in _BROWSER_ID_CACHE and new_cookie not in _BROWSER_ID_CACHE:
                _BROWSER_ID_CACHE[new_cookie] = _BROWSER_ID_CACHE[old_cookie]
        except Exception:
            pass
        try:
            if old_cookie in _CSRF_CACHE and new_cookie not in _CSRF_CACHE:
                _CSRF_CACHE[new_cookie] = dict(_CSRF_CACHE[old_cookie])
        except Exception:
            pass


def _maybe_take_updated_cookie(resp: Optional[requests.Response], cookie: str, *, session: Optional[requests.Session] = None) -> str:
    cookie = str(cookie or "")
    if not resp or extract_roblosecurity_from_requests_response is None:
        return cookie
    try:
        updated = extract_roblosecurity_from_requests_response(resp, session=session)
    except Exception:
        updated = None
    if updated and updated != cookie:
        _transfer_cookie_caches(cookie, updated)
        return str(updated)
    return cookie


def _apply_session_cookies(session: requests.Session, cookie: str, browser_id: Optional[str] = None) -> None:
    cookie = str(cookie or "")
    if normalize_roblosecurity_cookie_value is not None:
        try:
            cookie = normalize_roblosecurity_cookie_value(cookie)
        except Exception:
            cookie = str(cookie or "")
    if not session or not cookie:
        return
    try:
        session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
    except Exception:
        try:
            session.cookies[".ROBLOSECURITY"] = cookie
        except Exception:
            pass
    if browser_id:
        try:
            session.cookies.set("RBXEventTrackerV2", f"browserid={browser_id}", domain=".roblox.com", path="/")
        except Exception:
            pass

def _generate_browser_id() -> str:
    # Mirrors `browser_id = f"{rand}{rand}"` in main.py
    return f"{random.randint(100000,130000)}{random.randint(100000,900000)}"

def _get_or_create_browser_id(cookie: str) -> str:
    if not cookie:
        return _generate_browser_id()
    with _API_CACHE_LOCK:
        bid = _BROWSER_ID_CACHE.get(cookie)
        if bid:
            return bid
        bid = _generate_browser_id()
        _BROWSER_ID_CACHE[cookie] = bid
        return bid

def _get_cached_csrf(cookie: str) -> Optional[str]:
    if not cookie:
        return None
    with _API_CACHE_LOCK:
        entry = _CSRF_CACHE.get(cookie) or {}
        if float(entry.get("expires") or 0) > time.time():
            tok = str(entry.get("token") or "").strip()
            return tok or None
    return None

def _set_cached_csrf(cookie: str, token: str) -> None:
    if not cookie or not token:
        return
    with _API_CACHE_LOCK:
        _CSRF_CACHE[cookie] = {"token": token, "expires": time.time() + 1800}

def _retrieve_csrf_token(cookie: str) -> Tuple[Optional[str], str]:
    cookie = str(cookie or "")
    if normalize_roblosecurity_cookie_value is not None:
        try:
            cookie = normalize_roblosecurity_cookie_value(cookie)
        except Exception:
            cookie = str(cookie or "")
    cached = _get_cached_csrf(cookie)
    if cached:
        return cached, cookie

    # Roblox returns the CSRF token in a 403 response header.
    browser_id = _get_or_create_browser_id(cookie)
    s = requests.Session()
    s.headers.update(
        {
            "Referer": "https://www.roblox.com/",
            "Origin": "https://www.roblox.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        }
    )
    _apply_session_cookies(s, cookie, browser_id)

    try:
        r = s.post("https://auth.roblox.com/v1/authentication-ticket", timeout=6)
        cookie = _maybe_take_updated_cookie(r, cookie, session=s)
        _apply_session_cookies(s, cookie, browser_id)
        if r.status_code == 403:
            token = r.headers.get("x-csrf-token") or r.headers.get("X-CSRF-TOKEN")
            if token:
                _set_cached_csrf(cookie, token)
                return token, cookie
    except Exception:
        pass
    return None, cookie

def _make_blocking_api_session(cookie: str) -> Tuple[requests.Session, str]:
    """
    Create a requests Session for the Roblox user-blocking API.
    Sends:
      - Cookie: RBXEventTrackerV2=browserid=<browser_id>; .ROBLOSECURITY=<cookie>
      - x-csrf-token: <token>
    """
    cookie = str(cookie or "")
    if normalize_roblosecurity_cookie_value is not None:
        try:
            cookie = normalize_roblosecurity_cookie_value(cookie)
        except Exception:
            cookie = str(cookie or "")
    browser_id = _get_or_create_browser_id(cookie)
    s = requests.Session()
    s.headers.update(
        {
            "Referer": "https://www.roblox.com/",
            "Origin": "https://www.roblox.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json",
        }
    )
    _apply_session_cookies(s, cookie, browser_id)
    token, cookie = _retrieve_csrf_token(cookie)
    _apply_session_cookies(s, cookie, browser_id)
    if token:
        s.headers["x-csrf-token"] = token
    return s, cookie

def _roblox_error_hint(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            errs = data.get("errors")
            if isinstance(errs, list) and errs:
                e0 = errs[0] if isinstance(errs[0], dict) else {}
                msg = (e0.get("message") or e0.get("userFacingMessage") or "").strip()
                code = e0.get("code")
                if msg and code is not None:
                    return f"{code}: {msg}"
                if msg:
                    return msg
    except Exception:
        pass
    txt = (resp.text or "").strip().replace("\n", " ")
    return txt[:160]

def _roblox_error_code(resp: requests.Response) -> Optional[int]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            errs = data.get("errors")
            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                code = errs[0].get("code")
                if code is not None:
                    try:
                        return int(code)
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        txt = (resp.text or "").strip()
        if txt.isdigit():
            return int(txt)
    except Exception:
        pass
    return None

def _roblox_post(session: requests.Session, cookie: str, url: str, timeout: float = 12.0) -> Tuple[Optional[requests.Response], str]:
    cookie = str(cookie or "")
    last: Optional[requests.Response] = None
    for attempt in range(3):
        if attempt:
            time.sleep(min(0.5 * (2 ** (attempt - 1)), 3.0))
        try:
            r = session.post(url, timeout=timeout)
            last = r
        except Exception:
            continue

        cookie = _maybe_take_updated_cookie(r, cookie, session=session)
        _apply_session_cookies(session, cookie)

        # CSRF refresh pattern: 403 + x-csrf-token header
        if r.status_code == 403:
            tok = r.headers.get("x-csrf-token") or r.headers.get("X-CSRF-TOKEN")
            if tok:
                session.headers["x-csrf-token"] = tok
                _set_cached_csrf(cookie, tok)
                try:
                    r = session.post(url, timeout=timeout)
                    last = r
                except Exception:
                    continue
                cookie = _maybe_take_updated_cookie(r, cookie, session=session)
                _apply_session_cookies(session, cookie)

        # Rate-limit: retry a couple times (short backoff)
        if r.status_code == 429 and attempt < 2:
            ra = r.headers.get("Retry-After")
            if ra:
                try:
                    time.sleep(min(float(ra), 5.0))
                except Exception:
                    pass
            continue

        return r, cookie

    return last, cookie

def _api_block_user(session: requests.Session, cookie: str, target_user_id: str) -> Tuple[str, str]:
    url = f"{_BLOCKING_API_BASE}/{target_user_id}/block-user"
    r, cookie = _roblox_post(session, cookie, url)
    if r is None:
        return "failed", cookie

    if r.status_code in (200, 204):
        return "blocked", cookie

    if r.status_code in (401, 403):
        return "bad_cookie", cookie

    if r.status_code in (409,):
        return "already_blocked", cookie

    hint = _roblox_error_hint(r)
    if r.status_code == 400:
        if _roblox_error_code(r) == 1:
            return "already_blocked", cookie
        lhint = hint.lower()
        if "already" in lhint and "block" in lhint:
            return "already_blocked", cookie

    return f"failed ({r.status_code})" + (f" {hint}" if hint else ""), cookie

def _api_unblock_user(session: requests.Session, cookie: str, target_user_id: str) -> Tuple[str, str]:
    url = f"{_BLOCKING_API_BASE}/{target_user_id}/unblock-user"
    r, cookie = _roblox_post(session, cookie, url)
    if r is None:
        return "failed", cookie

    if r.status_code in (200, 204):
        return "unblocked", cookie

    if r.status_code in (401, 403):
        return "bad_cookie", cookie

    if r.status_code in (404, 409):
        # best-effort: treat as already unblocked / not present
        return "already_unblocked", cookie

    hint = _roblox_error_hint(r)
    if r.status_code == 400:
        if _roblox_error_code(r) == 4:
            return "already_unblocked", cookie
        lhint = hint.lower()
        if ("not" in lhint and "block" in lhint) or ("already" in lhint and "unblock" in lhint):
            return "already_unblocked", cookie

    return f"failed ({r.status_code})" + (f" {hint}" if hint else ""), cookie

# ---------------- PSL helpers ----------------
def _game_instances_url(place_id: str, slug: str) -> str:
    slug = slug or "Example"
    return f"https://www.roblox.com/games/{place_id}/{slug}#!/game-instances"

def _extract_share_code(share_url: str) -> Optional[str]:
    try:
        parsed = _urlparse.urlparse(share_url)
        qs = _urlparse.parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        if code:
            return code
        parts = [p for p in parsed.path.split("/") if p]
        return parts[-1] if parts else None
    except Exception:
        return None

def _resolve_share_code(share_code: str, cookie: str) -> Tuple[Optional[str], Optional[str], str]:
    cookie = str(cookie or "")
    if not share_code or not cookie:
        return None, None, cookie
    url = "https://apis.roblox.com/sharelinks/v1/resolve-link"
    payload = {"linkId": share_code, "linkType": "Server"}
    s = requests.Session()
    _apply_session_cookies(s, cookie)
    s.headers.update({"Content-Type": "application/json", "User-Agent": "Mozilla/5.0", "Referer": "https://www.roblox.com/"})
    try:
        r = s.post(url, json=payload, timeout=12)
    except Exception:
        return None, None, cookie

    cookie = _maybe_take_updated_cookie(r, cookie, session=s)
    _apply_session_cookies(s, cookie)

    if r.status_code == 403:
        csrf = r.headers.get("X-CSRF-TOKEN")
        if csrf:
            s.headers["X-CSRF-TOKEN"] = csrf
            try:
                r = s.post(url, json=payload, timeout=12)
            except Exception:
                return None, None, cookie
            cookie = _maybe_take_updated_cookie(r, cookie, session=s)
            _apply_session_cookies(s, cookie)

    if r.status_code == 200:
        data = r.json() or {}
        invite = data.get("privateServerInviteData") or {}
        place = str(invite.get("placeId") or "")
        link_code = invite.get("linkCode")
        if link_code:
            return place, link_code, cookie
    return None, None, cookie

def _compose_ps_link(place_id: str, link_code: str, slug: str) -> str:
    slug = slug or "-"
    return f"https://www.roblox.com/games/{place_id}/{slug}?privateServerLinkCode={link_code}"

# ---------------- Progress dialog ----------------
class _UtilitiesRunDialog(QDialog):
    def __init__(self, parent=None, title="Working..."):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)

        v = QVBoxLayout(self)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # indeterminate until totals known
        v.addWidget(self.bar)

        self.status = QLabel("Starting...")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        row = QHBoxLayout()
        row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        row.addWidget(self.cancel_btn)
        v.addLayout(row)

    def set_total(self, total: int):
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(0)

    def set_value(self, value: int):
        self.bar.setValue(max(0, value))

    def set_status(self, s: str):
        self.status.setText(s)

# ---------------- Workers ----------------
class _BlockWorker(QThread):
    progress = Signal(str)
    tick = Signal(int, int)
    done = Signal()

    def __init__(self, headless: bool, targets_text: str, allowed_uids=None, force: bool = False):
        super().__init__()
        self.headless = headless
        self.targets_text = targets_text
        if allowed_uids is None:
            self.allowed_uids = None
        else:
            self.allowed_uids = {str(x) for x in allowed_uids}
        self.force = bool(force)
        self._cancel = False

    def cancel(self): self._cancel = True
    def _emit(self, s: str): self.progress.emit(s)

    def run(self):
        users = _load_users()
        blocklog = _load_blocklog_migrated(users)
        username_cache: Dict[str, str] = blocklog.get("username_cache", {})

        targets, id_to_raw, unresolved = _coerce_targets_to_ids_and_names(
            self.targets_text.splitlines(), username_cache
        )

        if unresolved:
            self._emit("[Block] Unresolved usernames: " + ", ".join(unresolved))

        if not targets:
            self._emit("[Block] No targets.")
            self.done.emit(); return

        per_uid_targets: Dict[str, List[str]] = {}
        eligible_users = 0
        total = 0
        for uid, data in users.items():
            uid = str(uid)
            if self.allowed_uids is not None and uid not in self.allowed_uids:
                continue
            cookie = str(data.get("cookie") or "")
            bad = bool(data.get("bad", False))
            if not cookie or bad:
                continue
            eligible_users += 1
            already = set(map(str, blocklog.get("by_user", {}).get(uid, {}).get("blocked", [])))
            remaining = list(targets) if self.force else [t for t in targets if t not in already]
            if remaining:
                per_uid_targets[uid] = remaining
                total += len(remaining)

        if eligible_users == 0:
            self._emit("[Block] No eligible users selected.")
            self.done.emit(); return

        if total == 0:
            self._emit("[Block] All selected users already have these targets blocked; nothing to do.")
            self.done.emit(); return

        for u, rem in per_uid_targets.items():
            self._emit(f"[Block] {u}: will attempt {len(rem)} target(s).")

        cur = 0
        self.tick.emit(cur, total)

        work_items = list(per_uid_targets.items())
        users_dirty = False
        for user_idx, (uid, to_do) in enumerate(work_items):
            if self._cancel: break
            data = users.get(uid, {})
            cookie = str(data.get("cookie") or "")
            self._emit(f"[Block] {uid}: starting session")

            try:
                old_cookie = cookie
                session, cookie = _make_blocking_api_session(cookie)
                if cookie and old_cookie and cookie != old_cookie:
                    if update_cookie_in_users_dict is not None:
                        users_dirty |= bool(update_cookie_in_users_dict(users, user_id=uid, new_cookie=cookie))
                    else:
                        try:
                            if isinstance(data, dict):
                                data["cookie"] = cookie
                                users[uid] = data
                                users_dirty = True
                        except Exception:
                            pass
            except Exception as e:
                self._emit(f"[Block] {uid}: failed to start API session ({e})")
                if (not self._cancel) and user_idx < (len(work_items) - 1):
                    time.sleep(_USER_SWAP_DELAY_SEC)
                continue

            blocklog.setdefault("by_user", {}).setdefault(uid, {}).setdefault("blocked", [])
            blocked = set(map(str, blocklog["by_user"][uid]["blocked"]))

            try:
                did_call = False
                for t in to_do:
                    if self._cancel: break
                    if (not self.force) and t in blocked:
                        cur += 1; self.tick.emit(cur, total)
                        continue
                    if did_call:
                        time.sleep(_BLOCKING_CALL_DELAY_SEC)
                    res, new_cookie = _api_block_user(session, cookie, t)
                    did_call = True
                    if new_cookie and cookie and new_cookie != cookie:
                        cookie = new_cookie
                        if update_cookie_in_users_dict is not None:
                            users_dirty |= bool(update_cookie_in_users_dict(users, user_id=uid, new_cookie=cookie))
                        else:
                            try:
                                if isinstance(data, dict):
                                    data["cookie"] = cookie
                                    users[uid] = data
                                    users_dirty = True
                            except Exception:
                                pass
                    if res == "bad_cookie":
                        self._emit(f"[Block] {uid}: cookie became invalid mid-run")
                        break
                    if res in ("blocked", "already_blocked"):
                        blocked.add(t)
                        blocklog["by_user"][uid]["blocked"] = sorted(blocked)
                        blocklog["username_cache"] = username_cache
                        _save_json(_block_log_path(), blocklog)
                        self._emit(f"[Block] {uid}: {t} → {res}")
                    else:
                        self._emit(f"[Block] {uid}: {t} → {res}")
                    cur += 1; self.tick.emit(cur, total)
            finally:
                try: session.close()
                except Exception: pass

            if self._cancel:
                break
            if user_idx < (len(work_items) - 1):
                time.sleep(_USER_SWAP_DELAY_SEC)

        blocklog["username_cache"] = username_cache
        _save_json(_block_log_path(), blocklog)

        if users_dirty:
            if not _save_users(users):
                self._emit("[Block] Failed to save users.json (cookie updates).")

        self._emit("[Block] " + ("Cancelled." if self._cancel else "Done."))
        self.done.emit()

class _UnblockWorker(QThread):
    progress = Signal(str)
    tick = Signal(int, int)
    scrub_from_persistent = Signal(list)
    done = Signal()

    def __init__(self, headless: bool, unblock_text: str, allowed_uids=None, force: bool = False):
        super().__init__()
        self.headless = headless
        self.unblock_text = unblock_text
        if allowed_uids is None:
            self.allowed_uids = None
        else:
            self.allowed_uids = {str(x) for x in allowed_uids}
        self.force = bool(force)
        self._cancel = False

    def cancel(self): self._cancel = True
    def _emit(self, s: str): self.progress.emit(s)

    def run(self):
        users = _load_users()
        blocklog = _load_blocklog_migrated(users)
        username_cache: Dict[str, str] = blocklog.get("username_cache", {}) or {}

        raw_targets = [ln.strip() for ln in self.unblock_text.splitlines() if ln.strip()]
        targets, id_to_raw, unresolved = _coerce_targets_to_ids_and_names(raw_targets, username_cache)
        if unresolved:
            self._emit("[Unblock] Unresolved usernames: " + ", ".join(unresolved))

        if not targets:
            self._emit("[Unblock] No targets.")
            self.done.emit(); return

        # Build per-user work map from new schema
        work_map: Dict[str, List[str]] = {}
        eligible_users = 0
        total = 0
        for uid, data in users.items():
            uid = str(uid)
            if self.allowed_uids is not None and uid not in self.allowed_uids:
                continue
            cookie = str(data.get("cookie") or "")
            bad = bool(data.get("bad", False))
            if not cookie or bad:
                continue
            eligible_users += 1
            blocked_ids = set(map(str, blocklog.get("by_user", {}).get(uid, {}).get("blocked", [])))
            hits = list(targets) if self.force else [t for t in targets if t in blocked_ids]
            if hits:
                work_map[uid] = hits
                total += len(hits)

        if eligible_users == 0:
            self._emit("[Unblock] No eligible users selected.")
            self.done.emit(); return

        if total == 0:
            self._emit("[Unblock] None of the targets are currently blocked on the selected users; nothing to do.")
            self.done.emit(); return

        cur = 0
        self.tick.emit(cur, total)

        removed_ids_global: Set[str] = set()
        removed_names_global: Set[str] = set()

        work_items = list(work_map.items())
        users_dirty = False
        for user_idx, (uid, hits_for_uid) in enumerate(work_items):
            if self._cancel: break

            data = users.get(uid, {})
            cookie = str(data.get("cookie") or "")
            blocked_ids = set(map(str, blocklog.get("by_user", {}).get(uid, {}).get("blocked", [])))

            try:
                old_cookie = cookie
                session, cookie = _make_blocking_api_session(cookie)
                if cookie and old_cookie and cookie != old_cookie:
                    if update_cookie_in_users_dict is not None:
                        users_dirty |= bool(update_cookie_in_users_dict(users, user_id=uid, new_cookie=cookie))
                    else:
                        try:
                            if isinstance(data, dict):
                                data["cookie"] = cookie
                                users[uid] = data
                                users_dirty = True
                        except Exception:
                            pass
            except Exception as e:
                self._emit(f"[Unblock] {uid}: failed to start API session ({e})")
                cur += len(hits_for_uid); self.tick.emit(cur, total)
                if (not self._cancel) and user_idx < (len(work_items) - 1):
                    time.sleep(_USER_SWAP_DELAY_SEC)
                continue

            try:
                did_call = False
                for t in hits_for_uid:
                    if self._cancel: break

                    raws = list(id_to_raw.get(t, []))
                    raw_name = next((x for x in raws if not str(x).isdigit()), None)
                    label = f"@{str(raw_name).lower()}" if raw_name else t

                    if did_call:
                        time.sleep(_BLOCKING_CALL_DELAY_SEC)
                    res, new_cookie = _api_unblock_user(session, cookie, t)
                    did_call = True
                    if new_cookie and cookie and new_cookie != cookie:
                        cookie = new_cookie
                        if update_cookie_in_users_dict is not None:
                            users_dirty |= bool(update_cookie_in_users_dict(users, user_id=uid, new_cookie=cookie))
                        else:
                            try:
                                if isinstance(data, dict):
                                    data["cookie"] = cookie
                                    users[uid] = data
                                    users_dirty = True
                            except Exception:
                                pass
                    if res == "bad_cookie":
                        self._emit(f"[Unblock] {uid}: cookie became invalid mid-run")
                        break

                    if res in ("unblocked", "already_unblocked"):
                        blocked = [x for x in blocked_ids if x != t]
                        blocklog.setdefault("by_user", {}).setdefault(uid, {})["blocked"] = sorted(blocked)
                        _save_json(_block_log_path(), blocklog)
                        blocked_ids = set(blocked)  # <-- keep our working set in sync for subsequent removals

                        removed_ids_global.add(t)
                        for rawname in id_to_raw.get(t, []):
                            removed_names_global.add(rawname)

                    self._emit(f"[Unblock] {uid}: {label} ({t}) → {res}")
                    cur += 1; self.tick.emit(cur, total)

            finally:
                try: session.close()
                except Exception: pass

            if self._cancel:
                break
            if user_idx < (len(work_items) - 1):
                time.sleep(_USER_SWAP_DELAY_SEC)

        # Scrub persistent users_to_block.txt and notify GUI to scrub the box
        scrub_names = list(removed_ids_global) + list(removed_names_global)
        if scrub_names:
            self.scrub_from_persistent.emit(scrub_names)

        _save_json(_block_log_path(), blocklog)

        if users_dirty:
            if not _save_users(users):
                self._emit("[Unblock] Failed to save users.json (cookie updates).")

        self._emit("[Unblock] " + ("Cancelled." if self._cancel else "Done."))
        self.done.emit()

@dataclass
class _PSLOpts:
    place_id: str
    game_slug: str
    name_template: str
    headless: bool
    only_missing: bool

class _PSLWorker(QThread):
    progress = Signal(str)
    tick = Signal(int, int)
    done = Signal()

    def __init__(self, opts: _PSLOpts):
        super().__init__()
        self.opts = opts
        self._cancel = False

    def cancel(self): self._cancel = True
    def _emit(self, s: str): self.progress.emit(s)

    def run(self):
        users = _load_users()
        work = [(uid, d) for uid, d in users.items()
                if str(d.get("cookie") or "") and not bool(d.get("bad", False))]
        # NEW: filter to users that don't already have a private server link
        if self.opts.only_missing:
            work = [
                (uid, d) for uid, d in work
                if not (str(d.get("private_server_link") or "").strip())
            ]
        total = len(work)
        if total == 0:
            self._emit("[PSL] No eligible users.")
            self.done.emit(); return
        cur = 0; self.tick.emit(cur, total)

        any_update = False
        for uid, data in work:
            if self._cancel: break
            cookie = str(data.get("cookie") or "")
            username = str(data.get("username") or "")
            self._emit(f"[PSL] {uid}: session")
            try:
                d = _make_driver(cookie, headless=self.opts.headless)
            except Exception as e:
                self._emit(f"[PSL] {uid}: cookie failed auth ({e})")
                cur += 1; self.tick.emit(cur, total)
                continue

            try:
                d.get(_game_instances_url(self.opts.place_id, self.opts.game_slug))
                time.sleep(0.4)

                def _first_present(selectors: List[str], timeout=12, click=False, scroll=True):
                    last_err = None
                    for sel in selectors:
                        locator = (By.XPATH, sel) if sel.startswith("//") else (By.CSS_SELECTOR, sel)
                        try:
                            el = WebDriverWait(d, timeout).until(EC.presence_of_element_located(locator))
                            WebDriverWait(d, timeout).until(EC.element_to_be_clickable(locator))
                            if scroll:
                                d.execute_script("arguments[0].scrollIntoView({block:'center'});", el); time.sleep(0.15)
                            if click:
                                try: d.execute_script("arguments[0].click();", el)
                                except Exception: el.click()
                            return el
                        except Exception as e:
                            last_err = e
                    if last_err: raise last_err
                    raise RuntimeError("Element not found")

                def _click_first_now(selectors: List[str]) -> bool:
                    for sel in selectors:
                        by, val = ((By.XPATH, sel) if sel.startswith("//") else (By.CSS_SELECTOR, sel))
                        try:
                            elements = d.find_elements(by, val)
                        except Exception:
                            continue
                        for el in elements:
                            try:
                                if not el.is_displayed() or not el.is_enabled():
                                    continue
                            except Exception:
                                pass
                            try:
                                d.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                            except Exception:
                                pass
                            try:
                                d.execute_script("arguments[0].click();", el)
                            except Exception:
                                try:
                                    el.click()
                                except Exception:
                                    continue
                            return True
                    return False

                SELECTORS = {
                    "create_free": [
                        "//button[.//span[normalize-space()='Create one for free']]",
                        "//button[normalize-space(.)='Create one for free']",
                    ],
                    "configure_link": [
                        "a[aria-label='Configure'][href*='/private-server/configure']",
                        "//a[@aria-label='Configure' and contains(@href, '/private-server/configure')]",
                    ],
                    "generate_btn": [
                        "#generate-link-button",
                        "//button[@id='generate-link-button']",
                    ],
                    "share_input": [
                        "#join-link",
                        "//input[@id='join-link']",
                    ],
                }

                _click_first_now(SELECTORS["create_free"])
                _first_present(SELECTORS["configure_link"], click=True)

                share_el = _first_present(SELECTORS["share_input"], click=False)
                share_url = (share_el.get_attribute("value") or share_el.get_property("value") or "").strip()
                if not share_url:
                    _first_present(SELECTORS["generate_btn"], click=True)
                    end = time.time() + 15
                    while time.time() < end:
                        share_url = (share_el.get_attribute("value") or share_el.get_property("value") or "").strip()
                        if share_url:
                            break
                        time.sleep(0.25)
                    if not share_url:
                        raise TimeoutError("Share link did not populate in time.")

                code = _extract_share_code(share_url)
                if not code:
                    raise RuntimeError(f"Could not extract share code from: {share_url}")

                if extract_roblosecurity_from_selenium_driver is not None:
                    try:
                        browser_cookie = extract_roblosecurity_from_selenium_driver(d)
                    except Exception:
                        browser_cookie = None
                    if browser_cookie and browser_cookie != cookie:
                        cookie = browser_cookie
                        data["cookie"] = cookie
                        users[uid] = data
                        any_update = True

                place_from_api, link_code, new_cookie = _resolve_share_code(code, cookie)
                if new_cookie and new_cookie != cookie:
                    cookie = new_cookie
                    data["cookie"] = cookie
                    users[uid] = data
                    any_update = True
                if not link_code:
                    raise RuntimeError("Failed to resolve share code to privateServerLinkCode.")

                final_place = place_from_api or self.opts.place_id
                ps_link = _compose_ps_link(final_place, link_code, self.opts.game_slug)

                data["private_server_link"] = ps_link
                data["place"] = str(final_place)
                users[uid] = data
                any_update = True
                self._emit(f"[PSL] {uid}: OK → {ps_link}")

            except Exception as e:
                self._emit(f"[PSL] {uid}: error → {e}")
            finally:
                if extract_roblosecurity_from_selenium_driver is not None:
                    try:
                        browser_cookie = extract_roblosecurity_from_selenium_driver(d)
                    except Exception:
                        browser_cookie = None
                    if browser_cookie and browser_cookie != cookie:
                        cookie = browser_cookie
                        try:
                            data["cookie"] = cookie
                            users[uid] = data
                            any_update = True
                        except Exception:
                            pass
                try: d.quit()
                except Exception: pass

            cur += 1; self.tick.emit(cur, total)

        if any_update:
            if not _save_users(users):
                self._emit("[PSL] Failed to save users.json")
        self._emit("[PSL] " + ("Cancelled." if self._cancel else "Done."))
        self.done.emit()

# ---------------- GUI ----------------
def build_utilities_widget(self) -> QWidget:
    _set_config_manager(getattr(self, "config_manager", None))
    tab = QWidget()
    v = QVBoxLayout(tab)

    # Global toggles + account selector (single row)
    self.utilities_headless_chk = QCheckBox("Run non-headless (debug)")
    self.utilities_headless_chk.setChecked(False)

    self.utilities_force_chk = QCheckBox("Force block/unblock")
    self.utilities_force_chk.setChecked(False)

    # Accounts selector (applies to Blocking / Unblocking)
    if not hasattr(self, "_utilities_selected_uids"):
        self._utilities_selected_uids = None  # None => all eligible users

    self.utilities_accounts_btn = QPushButton("Accounts…")
    self.utilities_accounts_label = QLabel("Accounts: All")
    top_row = QHBoxLayout()
    top_row.addWidget(self.utilities_headless_chk)
    top_row.addWidget(self.utilities_force_chk)
    top_row.addStretch()
    top_row.addWidget(self.utilities_accounts_label)
    top_row.addWidget(self.utilities_accounts_btn)
    v.addLayout(top_row)

    # Scroll container
    scroll = QScrollArea(); scroll.setWidgetResizable(True)
    content = QWidget(); content_v = QVBoxLayout(content)
    scroll.setWidget(content)
    v.addWidget(scroll)

    # Blocking group
    block_grp = QGroupBox("Blocking")
    block_form = QVBoxLayout(block_grp)

    block_hint = QLabel("Block list:")
    block_form.addWidget(block_hint)

    self.block_persistent_box = QPlainTextEdit()
    self.block_persistent_box.setPlaceholderText("e.g.\nSomeUser\n12345678\nAnotherUser")
    self.block_persistent_box.setFixedHeight(160)
    block_form.addWidget(self.block_persistent_box)

    row = QHBoxLayout()
    save_block_btn = QPushButton("Save Block List")
    run_block_btn = QPushButton("Start Blocking")
    row.addWidget(save_block_btn); row.addStretch(); row.addWidget(run_block_btn)
    block_form.addLayout(row)

    # Unblocking group
    un_grp = QGroupBox("Unblocking")
    un_form = QVBoxLayout(un_grp)

    un_form.addWidget(QLabel("Unblock list:"))
    self.unblock_box = QPlainTextEdit()
    self.unblock_box.setPlaceholderText("e.g.\nSomeUser\n12345678")
    self.unblock_box.setFixedHeight(140)
    un_form.addWidget(self.unblock_box)

    run_unblock_btn = QPushButton("Start Unblocking")
    un_form.addWidget(run_unblock_btn, alignment=Qt.AlignmentFlag.AlignRight)

    # PSL Grabber group
    psl_grp = QGroupBox("Private Server Link Grabber")
    psl_form = QFormLayout(psl_grp)
    self.psl_place_id = QLineEdit()
    self.psl_game_slug = QLineEdit()
    self.psl_name_tmpl = QLineEdit("JARAM-{username}-{ts}")
    psl_form.addRow("Place ID:", self.psl_place_id)
    psl_form.addRow("Game Slug:", self.psl_game_slug)
    psl_form.addRow("Server Name:", self.psl_name_tmpl)
    self.psl_only_missing_chk = QCheckBox("Only users without a private server link")
    self.psl_only_missing_chk.setChecked(True)  # default ON
    psl_form.addRow("", self.psl_only_missing_chk)
    run_psl_btn = QPushButton("Run PSL Grabber")
    psl_form.addRow("", run_psl_btn)
    content_v.addWidget(psl_grp)
    content_v.addWidget(block_grp)
    content_v.addWidget(un_grp)

    content_v.addStretch()

    # Load persistent block list on open
    try:
        p = _persistent_blocklist_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                self.block_persistent_box.setPlainText(f.read())
    except Exception:
        pass

    # ---- one-job-at-a-time guard ----
    self._util_buttons = []
    def _register_btn(b: QPushButton):
        self._util_buttons.append(b)
        return b
    _register_btn(save_block_btn)
    _register_btn(run_block_btn)
    _register_btn(run_unblock_btn)
    _register_btn(run_psl_btn)
    _register_btn(self.utilities_accounts_btn)

    self._util_busy = False
    def _set_util_busy(on: bool):
        self._util_busy = on
        for b in self._util_buttons:
            b.setEnabled(not on)
        try: self.utilities_force_chk.setEnabled(not on)
        except Exception: pass
        try: self.utilities_headless_chk.setEnabled(not on)
        except Exception: pass

    # ---- helpers ----
    def _is_eligible_user(uid: str, data: dict) -> bool:
        cookie = str((data or {}).get("cookie") or "")
        bad = bool((data or {}).get("bad", False))
        return bool(cookie) and not bad

    def _update_accounts_label():
        try:
            users_now = _load_users() or {}
        except Exception:
            users_now = {}

        eligible = [
            str(uid) for uid, data in users_now.items()
            if _is_eligible_user(str(uid), data)
        ]

        if not eligible:
            self.utilities_accounts_label.setText("Accounts: None")
            return

        eligible_set = set(eligible)
        sel = getattr(self, "_utilities_selected_uids", None)
        if sel is None:
            self.utilities_accounts_label.setText(f"Accounts: All ({len(eligible)})")
            return

        sel_set = {str(x) for x in sel} & eligible_set
        if sel_set == eligible_set:
            self._utilities_selected_uids = None
            self.utilities_accounts_label.setText(f"Accounts: All ({len(eligible)})")
            return

        self._utilities_selected_uids = sel_set
        self.utilities_accounts_label.setText(f"Accounts: {len(sel_set)}/{len(eligible)}")

    def _open_accounts_dialog():
        if getattr(self, "_util_busy", False):
            self.append_log("[Utilities] A job is already running.")
            return

        users_now = _load_users() or {}
        dlg = QDialog(self)
        dlg.setWindowTitle("Select Accounts (Blocking/Unblocking)")
        dlg.setModal(True)
        dlg.setMinimumWidth(520)

        vbox = QVBoxLayout(dlg)
        vbox.addWidget(QLabel("Choose which accounts will run Blocking/Unblocking.\n(Only accounts with a valid cookie are selectable.)"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_v = QVBoxLayout(inner)
        scroll.setWidget(inner)
        vbox.addWidget(scroll)

        eligible_uids: List[str] = []
        boxes: Dict[str, QCheckBox] = {}

        current_sel = getattr(self, "_utilities_selected_uids", None)

        for uid, data in users_now.items():
            uid_str = str(uid)
            username = str((data or {}).get("username") or "").strip()
            cookie = str((data or {}).get("cookie") or "")
            bad = bool((data or {}).get("bad", False))

            label = uid_str
            if username:
                label += f"  ({username})"
            if bad:
                label += "  [BAD]"
            if not cookie:
                label += "  [NO COOKIE]"

            cb = QCheckBox(label)
            eligible = _is_eligible_user(uid_str, data)
            cb.setEnabled(bool(eligible))

            if eligible:
                eligible_uids.append(uid_str)
                if current_sel is None:
                    cb.setChecked(True)
                else:
                    cb.setChecked(uid_str in set(map(str, current_sel)))
            else:
                cb.setChecked(False)

            boxes[uid_str] = cb
            inner_v.addWidget(cb)

        inner_v.addStretch()

        btn_row = QHBoxLayout()
        sel_all_btn = QPushButton("Select All")
        sel_none_btn = QPushButton("Select None")
        btn_row.addWidget(sel_all_btn)
        btn_row.addWidget(sel_none_btn)
        btn_row.addStretch()
        vbox.addLayout(btn_row)

        action_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        ok_btn = QPushButton("OK")
        action_row.addStretch()
        action_row.addWidget(cancel_btn)
        action_row.addWidget(ok_btn)
        vbox.addLayout(action_row)

        def _set_all(val: bool):
            for uid_str in eligible_uids:
                boxes[uid_str].setChecked(val)

        sel_all_btn.clicked.connect(lambda: _set_all(True))
        sel_none_btn.clicked.connect(lambda: _set_all(False))
        cancel_btn.clicked.connect(dlg.reject)
        ok_btn.clicked.connect(dlg.accept)

        if dlg.exec() != 1:
            return

        eligible_set = set(eligible_uids)
        chosen = {uid for uid in eligible_uids if boxes[uid].isChecked()}
        self._utilities_selected_uids = None if chosen == eligible_set else chosen
        _update_accounts_label()

    self.utilities_accounts_btn.clicked.connect(_open_accounts_dialog)
    _update_accounts_label()

    def _save_blocklist():
        with open(_persistent_blocklist_path(), "w", encoding="utf-8") as f:
            f.write(self.block_persistent_box.toPlainText())
        self.append_log("[Utilities] Saved persistent block list.")

    def _scrub(names):
        if not names:
            return
        # Scrub file
        path = _persistent_blocklist_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f]
            rem_ids = {n for n in names if n.isdigit()}
            rem_names = {n.lower() for n in names if not n.isdigit()}

            def _keep(ln: str) -> bool:
                norm = ln.strip().lstrip("@")
                if not norm:
                    return False
                if norm.isdigit():
                    return norm not in rem_ids
                return norm.lower() not in rem_names

            new_lines = [ln for ln in lines if _keep(ln)]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + ("\n" if new_lines else ""))

        # Scrub the UI textbox too
        cur = self.block_persistent_box.toPlainText().splitlines()
        rem_ids = {n for n in names if n.isdigit()}
        rem_names = {n.lower() for n in names if not n.isdigit()}
        kept = []
        for ln in cur:
            norm = ln.strip().lstrip("@")
            if not norm:
                continue
            if norm.isdigit():
                if norm in rem_ids:
                    continue
            else:
                if norm.lower() in rem_names:
                    continue
            kept.append(ln)
        self.block_persistent_box.setPlainText("\n".join(kept))

    # ---- start actions with dialog wiring ----
    def _start_block():
        if self._util_busy:
            self.append_log("[Utilities] A job is already running.")
            return
        _save_blocklist()
        headless = not self.utilities_headless_chk.isChecked()
        targets_text = self.block_persistent_box.toPlainText()

        w = _BlockWorker(
            headless=headless,
            targets_text=targets_text,
            allowed_uids=getattr(self, "_utilities_selected_uids", None),
            force=bool(self.utilities_force_chk.isChecked()),
        )
        dlg = _UtilitiesRunDialog(self, "Blocking…")
        _set_util_busy(True)

        w.progress.connect(lambda s: (self.append_log(s), dlg.set_status(s)))
        w.tick.connect(lambda cur, tot: (dlg.set_total(tot), dlg.set_value(cur)))
        w.done.connect(lambda: (self.append_log("[Utilities] Blocking complete."),
                                dlg.accept(), _set_util_busy(False)))
        dlg.cancel_btn.clicked.connect(w.cancel)

        self._active_worker = w
        w.start()
        dlg.exec()

    def _start_unblock():
        if self._util_busy:
            self.append_log("[Utilities] A job is already running.")
            return
        headless = not self.utilities_headless_chk.isChecked()
        w = _UnblockWorker(
            headless=headless,
            unblock_text=self.unblock_box.toPlainText(),
            allowed_uids=getattr(self, "_utilities_selected_uids", None),
            force=bool(self.utilities_force_chk.isChecked()),
        )

        dlg = _UtilitiesRunDialog(self, "Unblocking…")
        _set_util_busy(True)

        w.progress.connect(lambda s: (self.append_log(s), dlg.set_status(s)))
        w.tick.connect(lambda cur, tot: (dlg.set_total(tot), dlg.set_value(cur)))
        w.scrub_from_persistent.connect(_scrub)
        w.done.connect(lambda: (self.append_log("[Utilities] Unblocking complete."),
                                dlg.accept(), _set_util_busy(False)))
        dlg.cancel_btn.clicked.connect(w.cancel)

        self._active_worker = w
        w.start()
        dlg.exec()

    def _start_psl():
        if self._util_busy:
            self.append_log("[Utilities] A job is already running.")
            return
        headless = not self.utilities_headless_chk.isChecked()
        opts = _PSLOpts(
            place_id=self.psl_place_id.text().strip(),
            game_slug=self.psl_game_slug.text().strip(),
            name_template=self.psl_name_tmpl.text().strip() or "JARAM-{username}-{ts}",
            headless=headless,
            only_missing=self.psl_only_missing_chk.isChecked(),
        )
        if not opts.place_id:
            self.append_log("[PSL] PLACE_ID is required.")
            return

        w = _PSLWorker(opts)
        dlg = _UtilitiesRunDialog(self, "PSL Grabber…")
        _set_util_busy(True)

        w.progress.connect(lambda s: (self.append_log(s), dlg.set_status(s)))
        w.tick.connect(lambda cur, tot: (dlg.set_total(tot), dlg.set_value(cur)))
        w.done.connect(lambda: (self.append_log("[Utilities] PSL run complete."),
                                dlg.accept(), _set_util_busy(False)))
        dlg.cancel_btn.clicked.connect(w.cancel)

        self._active_worker = w
        w.start()
        dlg.exec()

    # Wire UI
    save_block_btn.clicked.connect(_save_blocklist)
    run_block_btn.clicked.connect(_start_block)
    run_unblock_btn.clicked.connect(_start_unblock)
    run_psl_btn.clicked.connect(_start_psl)
    return tab


def setup_UTILITIES_tab(self):
    tab = build_utilities_widget(self)
    self.tab_widget.addTab(tab, "Utilities")
