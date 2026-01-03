# utilities_tab.py
# ──────────────────────────────────────────────────────────────────────────────
# JARAM Utilities Tab (Blocking / Unblocking / PSL Grabber)
# Self-contained: paths, JSON I/O, migration, Selenium helpers, QThreads, a
# progress dialog, and a single entrypoint: setup_UTILITIES_tab(self)
# Expects on MainWindow:
#   - self.append_log(str) method
#   - self.tab_widget (QTabWidget) to add the tab
# ──────────────────────────────────────────────────────────────────────────────

import os, json, time, urllib.parse as _urlparse
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional, Set
from collections import defaultdict

import requests
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout, QLineEdit,
    QPushButton, QPlainTextEdit, QCheckBox, QLabel, QScrollArea,
    QDialog, QProgressBar
)

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

def _resolve_share_code(share_code: str, cookie: str) -> Tuple[Optional[str], Optional[str]]:
    if not share_code or not cookie:
        return None, None
    url = "https://apis.roblox.com/sharelinks/v1/resolve-link"
    payload = {"linkId": share_code, "linkType": "Server"}
    s = requests.Session()
    s.cookies[".ROBLOSECURITY"] = cookie
    s.headers.update({"Content-Type": "application/json", "User-Agent": "Mozilla/5.0", "Referer": "https://www.roblox.com/"})
    r = s.post(url, json=payload, timeout=12)
    if r.status_code == 403:
        csrf = r.headers.get("X-CSRF-TOKEN")
        if csrf:
            s.headers["X-CSRF-TOKEN"] = csrf
            r = s.post(url, json=payload, timeout=12)
    if r.status_code == 200:
        data = r.json() or {}
        invite = data.get("privateServerInviteData") or {}
        place = str(invite.get("placeId") or "")
        link_code = invite.get("linkCode")
        if link_code:
            return place, link_code
    return None, None

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
    progress = pyqtSignal(str)
    tick = pyqtSignal(int, int)
    done = pyqtSignal()

    def __init__(self, headless: bool, targets_text: str):
        super().__init__()
        self.headless = headless
        self.targets_text = targets_text
        self._cancel = False

    def cancel(self): self._cancel = True
    def _emit(self, s: str): self.progress.emit(s)

    def run(self):
        users = _load_json(_users_json_path(), {})
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
        total = 0
        for uid, data in users.items():
            cookie = str(data.get("cookie") or "")
            bad = bool(data.get("bad", False))
            if not cookie or bad:
                continue
            already = set(map(str, blocklog.get("by_user", {}).get(str(uid), {}).get("blocked", [])))
            remaining = [t for t in targets if t not in already]
            if remaining:
                per_uid_targets[str(uid)] = remaining
                total += len(remaining)

        if total == 0:
            self._emit("[Block] Everyone already blocked; nothing to do.")
            self.done.emit(); return

        for u, rem in per_uid_targets.items():
            self._emit(f"[Block] {u}: will attempt {len(rem)} target(s).")

        cur = 0
        self.tick.emit(cur, total)

        for uid, to_do in per_uid_targets.items():
            if self._cancel: break
            data = users.get(uid, {})
            cookie = str(data.get("cookie") or "")
            self._emit(f"[Block] {uid}: starting session")

            try:
                driver = _make_driver(cookie, headless=self.headless)
            except Exception as e:
                self._emit(f"[Block] {uid}: cookie failed auth ({e})")
                continue

            blocklog.setdefault("by_user", {}).setdefault(uid, {}).setdefault("blocked", [])
            blocked = set(map(str, blocklog["by_user"][uid]["blocked"]))

            try:
                for t in to_do:
                    if self._cancel: break
                    if t in blocked:
                        cur += 1; self.tick.emit(cur, total)
                        continue
                    res = _block_user(driver, t)
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
                        self._emit(f"[Block] {uid}: {t} → failed")
                    cur += 1; self.tick.emit(cur, total)
            finally:
                try: driver.quit()
                except Exception: pass

        blocklog["username_cache"] = username_cache
        _save_json(_block_log_path(), blocklog)

        self._emit("[Block] " + ("Cancelled." if self._cancel else "Done."))
        self.done.emit()

class _UnblockWorker(QThread):
    progress = pyqtSignal(str)
    tick = pyqtSignal(int, int)
    scrub_from_persistent = pyqtSignal(list)
    done = pyqtSignal()

    def __init__(self, headless: bool, unblock_text: str):
        super().__init__()
        self.headless = headless
        self.unblock_text = unblock_text
        self._cancel = False

    def cancel(self): self._cancel = True
    def _emit(self, s: str): self.progress.emit(s)

    def run(self):
        users = _load_json(_users_json_path(), {})
        blocklog = _load_blocklog_migrated(users)
        username_cache: Dict[str, str] = blocklog.get("username_cache", {}) or {}

        raw_targets = [ln.strip() for ln in self.unblock_text.splitlines() if ln.strip()]
        targets, id_to_raw, unresolved = _coerce_targets_to_ids_and_names(raw_targets, username_cache)
        target_ids = set(targets)
        if unresolved:
            self._emit("[Unblock] Unresolved usernames: " + ", ".join(unresolved))

        if not target_ids:
            self._emit("[Unblock] No targets.")
            self.done.emit(); return

        # Build per-user work map from new schema
        work_map: Dict[str, List[str]] = {}
        total = 0
        for uid, data in users.items():
            cookie = str(data.get("cookie") or "")
            bad = bool(data.get("bad", False))
            if not cookie or bad:
                continue
            blocked_ids = set(map(str, blocklog.get("by_user", {}).get(str(uid), {}).get("blocked", [])))
            hits = [t for t in target_ids if t in blocked_ids]
            if hits:
                work_map[str(uid)] = hits
                total += len(hits)

        if total == 0:
            self._emit("[Unblock] None of the targets are currently blocked; nothing to do.")
            self.done.emit(); return

        cur = 0
        self.tick.emit(cur, total)

        removed_ids_global: Set[str] = set()
        removed_names_global: Set[str] = set()

        # Reverse map id->name (best effort; don't write new cache entries here)
        id_to_name_lower: Dict[str, str] = {}
        for nm, rid in username_cache.items():
            id_to_name_lower[str(rid)] = str(nm).lower()

        for uid, hits_for_uid in work_map.items():
            if self._cancel: break

            data = users.get(uid, {})
            cookie = str(data.get("cookie") or "")
            blocked_ids = set(map(str, blocklog.get("by_user", {}).get(uid, {}).get("blocked", [])))

            driver = None
            try:
                driver = _make_driver(cookie, headless=self.headless)
                driver.get(_BLOCKED_URL)
                time.sleep(0.4)

                for t in list(hits_for_uid):
                    if self._cancel: break

                    uname_lower = id_to_name_lower.get(t)
                    if not uname_lower:
                        # resolve name on the fly (don't persist to cache to avoid duplicates)
                        try:
                            r = requests.get(f"https://users.roblox.com/v1/users/{t}", timeout=8)
                            if r.status_code == 200 and r.json().get("name"):
                                uname_lower = r.json()["name"].lower()
                                id_to_name_lower[t] = uname_lower
                        except Exception:
                            pass

                    if not uname_lower:
                        self._emit(f"[Unblock] {uid}: id {t} has no resolvable name for DOM; skipping.")
                        cur += 1; self.tick.emit(cur, total)
                        continue

                    node = (_find_blocked_node_by_name(driver, uname_lower, quick_only=True)
                            or _fully_load_then_find_by_name(driver, uname_lower))

                    if not node:
                        # Already unblocked → sync
                        blocked = [x for x in blocked_ids if x != t]
                        blocklog.setdefault("by_user", {}).setdefault(uid, {})["blocked"] = sorted(blocked)
                        _save_json(_block_log_path(), blocklog)
                        blocked_ids = set(blocked)  # <-- keep our working set in sync for subsequent removals


                        removed_ids_global.add(t)
                        for rawname in id_to_raw.get(t, []):
                            removed_names_global.add(rawname)
                        self._emit(f"[Unblock] {uid}: @{uname_lower} ({t}) not present → synced")
                        cur += 1; self.tick.emit(cur, total)
                        continue

                    ok = _unblock_display_node(driver, node)
                    if ok:
                        blocked = [x for x in blocked_ids if x != t]
                        blocklog.setdefault("by_user", {}).setdefault(uid, {})["blocked"] = sorted(blocked)
                        _save_json(_block_log_path(), blocklog)
                        blocked_ids = set(blocked)  # <-- keep our working set in sync for subsequent removals

                        removed_ids_global.add(t)
                        for rawname in id_to_raw.get(t, []):
                            removed_names_global.add(rawname)
                        self._emit(f"[Unblock] {uid}: @{uname_lower} ({t}) unblocked")
                    else:
                        self._emit(f"[Unblock] {uid}: @{uname_lower} ({t}) still present")

                    cur += 1; self.tick.emit(cur, total)

            finally:
                try: driver.quit()
                except Exception: pass

        # Scrub persistent users_to_block.txt and notify GUI to scrub the box
        scrub_names = list(removed_ids_global) + list(removed_names_global)
        if scrub_names:
            self.scrub_from_persistent.emit(scrub_names)

        _save_json(_block_log_path(), blocklog)
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
    progress = pyqtSignal(str)
    tick = pyqtSignal(int, int)
    done = pyqtSignal()

    def __init__(self, opts: _PSLOpts):
        super().__init__()
        self.opts = opts
        self._cancel = False

    def cancel(self): self._cancel = True
    def _emit(self, s: str): self.progress.emit(s)

    def run(self):
        users = _load_json(_users_json_path(), {})
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

                def _any_present_now(selectors: List[str]) -> bool:
                    for sel in selectors:
                        by, val = ((By.XPATH, sel) if sel.startswith("//") else (By.CSS_SELECTOR, sel))
                        if d.find_elements(by, val):
                            return True
                    return False

                SELECTORS = {
                    "create_ps": [
                        "button.rbx-private-server-create-button",
                        "//button[normalize-space()='Create Private Server']",
                    ],
                    "max_free": [
                        "span.rbx-private-server-create-disabled-text",
                        "//span[contains(@class,'rbx-private-server-create-disabled-text')]",
                    ],
                    "more_menu": [
                        "div.link-menu.rbx-private-game-server-menu button.btn-generic-more-sm",
                        "//div[contains(@class,'rbx-private-game-server-menu')]//button[contains(@class,'btn-generic-more-sm')]",
                    ],
                    "configure_link": [
                        "a.rbx-private-server-configure",
                        "//a[contains(@class,'rbx-private-server-configure')]",
                    ],
                    "server_name_input": [
                        "#private-server-name-text-box",
                        "//input[@id='private-server-name-text-box']",
                    ],
                    "buy_now": [
                        "//button[normalize-space()='Buy Now']",
                        "button.modal-button.btn-primary-md.btn-min-width",
                    ],
                    "customize": [
                        "//button[normalize-space()='Customize']",
                        "//a[contains(. 'Customize') or contains(. 'Configure')]",
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

                if _any_present_now(SELECTORS["max_free"]):
                    _first_present(SELECTORS["more_menu"], click=True)
                    _first_present(SELECTORS["configure_link"], click=True)
                else:
                    if _any_present_now(SELECTORS["create_ps"]):
                        _first_present(SELECTORS["create_ps"], click=True)
                        ts = int(time.time())
                        server_name = (self.opts.name_template or "JARAM-{username}-{ts}").format(
                            username=(username or uid), uid=uid, ts=ts
                        )
                        try:
                            box = _first_present(SELECTORS["server_name_input"])
                            box.clear(); box.send_keys(server_name); time.sleep(0.2)
                        except Exception:
                            pass
                        _first_present(SELECTORS["buy_now"], click=True)
                        _first_present(SELECTORS["customize"], click=True)
                    else:
                        _first_present(SELECTORS["customize"], click=True)

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

                place_from_api, link_code = _resolve_share_code(code, cookie)
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
                try: d.quit()
                except Exception: pass

            cur += 1; self.tick.emit(cur, total)

        if any_update:
            _save_json(_users_json_path(), users)
        self._emit("[PSL] " + ("Cancelled." if self._cancel else "Done."))
        self.done.emit()

# ---------------- GUI ----------------
def build_utilities_widget(self) -> QWidget:
    tab = QWidget()
    v = QVBoxLayout(tab)

    # Global toggle
    self.utilities_headless_chk = QCheckBox("Run non-headless (debug)")
    self.utilities_headless_chk.setChecked(False)
    v.addWidget(self.utilities_headless_chk)

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

    self._util_busy = False
    def _set_util_busy(on: bool):
        self._util_busy = on
        for b in self._util_buttons:
            b.setEnabled(not on)

    # ---- helpers ----
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

        w = _BlockWorker(headless=headless, targets_text=targets_text)
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
        w = _UnblockWorker(headless=headless, unblock_text=self.unblock_box.toPlainText())

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
