from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

ROBLOSECURITY_COOKIE_NAME = ".ROBLOSECURITY"
# Historical note: Roblox previously used a warning-prefixed cookie value,
# but the internal format is opaque and may change at any time.

_logger = logging.getLogger("jaram.cookie")


def _cookie_fp(value: str) -> str:
    v = normalize_roblosecurity_cookie_value(value)
    if not v:
        return "empty"
    try:
        digest = hashlib.sha256(v.encode("utf-8", "ignore")).hexdigest()[:12]
        return f"sha256:{digest}"
    except Exception:
        return "sha256:err"


def normalize_roblosecurity_cookie_value(value: str) -> str:
    """
    Normalize a `.ROBLOSECURITY` cookie string to the raw cookie value.

    Accepts any of these common forms:
      - "<cookie_value>"
      - ".ROBLOSECURITY=<cookie_value>; Path=/; Domain=.roblox.com; ..."
      - "Cookie: RBXEventTrackerV2=...; .ROBLOSECURITY=<cookie_value>; ..."
      - "Set-Cookie: .ROBLOSECURITY=<cookie_value>; ..."

    Returns "" if no value is found.
    """
    s = str(value or "").strip()
    if not s:
        return ""

    lower = s.lower()
    needles = (f"{ROBLOSECURITY_COOKIE_NAME}=".lower(), "roblosecurity=")
    for needle in needles:
        idx = lower.find(needle)
        if idx != -1:
            s = s[idx + len(needle) :]
            break

    if ";" in s:
        s = s.split(";", 1)[0]

    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return str(s).strip()


def is_probably_roblosecurity(value: str) -> bool:
    value = normalize_roblosecurity_cookie_value(value)
    if not value:
        return False

    # Don't accept our own at-rest encrypted cookie strings.
    if value.startswith(("enc_v1:", "enc_v2:")):
        return False

    # Cookie values should not contain whitespace/control characters.
    if any(ch.isspace() for ch in value):
        return False

    # Common "deleted"/placeholder values.
    if value.lower() in ("deleted", "null", "none", "0"):
        return False

    # Obvious Set-Cookie attribute tokens (when user pastes the wrong part).
    lv = value.lower()
    if lv in ("secure", "httponly"):
        return False
    if lv.startswith(("path=", "domain=", "expires=", "max-age=", "samesite=", "priority=")):
        return False

    # Treat it as an opaque auth token (format/length may change).
    return True


def extract_roblosecurity_from_requests_response(response: Any, *, session: Any = None) -> Optional[str]:
    """
    Best-effort extraction of `.ROBLOSECURITY` from a `requests.Response`.

    Prefers parsed cookie jars (`response.cookies`, then `session.cookies`) rather than
    parsing raw Set-Cookie headers.
    """
    for jar in (getattr(response, "cookies", None), getattr(session, "cookies", None)):
        if jar is None:
            continue
        try:
            val = jar.get(ROBLOSECURITY_COOKIE_NAME)
        except Exception:
            val = None
        if is_probably_roblosecurity(val):
            return normalize_roblosecurity_cookie_value(val)
    return None


def extract_roblosecurity_from_selenium_driver(driver: Any) -> Optional[str]:
    try:
        cookie_obj = driver.get_cookie(ROBLOSECURITY_COOKIE_NAME) if driver else None
    except Exception:
        cookie_obj = None
    if isinstance(cookie_obj, dict):
        val = cookie_obj.get("value")
        if is_probably_roblosecurity(val):
            return normalize_roblosecurity_cookie_value(val)
    return None


def update_cookie_in_users_dict(
    users: Any,
    *,
    user_id: Optional[str] = None,
    old_cookie: Optional[str] = None,
    new_cookie: str,
) -> bool:
    """
    Mutates `users` in-place.

    Supports both formats:
      - {"uid": {"cookie": "...", ...}}
      - {"uid": "<cookie_string>"}  (legacy)
    """
    if not isinstance(users, dict):
        _logger.debug("[Cookie] update skipped: users not dict")
        return False
    new_cookie = normalize_roblosecurity_cookie_value(new_cookie)
    if not is_probably_roblosecurity(new_cookie):
        _logger.debug("[Cookie] update skipped: new_cookie invalid fp=%s", _cookie_fp(new_cookie))
        return False

    updated_any = False

    if user_id is not None:
        uid = str(user_id)
        info = users.get(uid)
        if isinstance(info, dict):
            existing_raw = str(info.get("cookie") or "")
            existing = normalize_roblosecurity_cookie_value(existing_raw)
            if existing != new_cookie:
                info["cookie"] = new_cookie
                users[uid] = info
                updated_any = True
                _logger.info("[Cookie] uid=%s updated %s -> %s", uid, _cookie_fp(existing), _cookie_fp(new_cookie))
        elif isinstance(info, str):
            existing_raw = str(info or "")
            existing = normalize_roblosecurity_cookie_value(existing_raw)
            if existing != new_cookie:
                users[uid] = new_cookie
                updated_any = True
                _logger.info("[Cookie] uid=%s updated %s -> %s", uid, _cookie_fp(existing), _cookie_fp(new_cookie))
        else:
            _logger.debug("[Cookie] update skipped: uid=%s missing/invalid user record", uid)
            return False
    else:
        old_cookie = normalize_roblosecurity_cookie_value(old_cookie)
        if not old_cookie:
            _logger.debug("[Cookie] update skipped: old_cookie empty")
            return False
        for uid, info in list(users.items()):
            if isinstance(info, dict):
                existing_raw = str(info.get("cookie") or "")
                existing = normalize_roblosecurity_cookie_value(existing_raw)
                if existing == old_cookie and existing != new_cookie:
                    info["cookie"] = new_cookie
                    users[str(uid)] = info
                    updated_any = True
                    _logger.info("[Cookie] uid=%s updated %s -> %s", uid, _cookie_fp(existing), _cookie_fp(new_cookie))
            elif isinstance(info, str):
                existing_raw = str(info or "")
                existing = normalize_roblosecurity_cookie_value(existing_raw)
                if existing == old_cookie and existing != new_cookie:
                    users[str(uid)] = new_cookie
                    updated_any = True
                    _logger.info("[Cookie] uid=%s updated %s -> %s", uid, _cookie_fp(existing), _cookie_fp(new_cookie))

    return updated_any


def persist_updated_cookie(
    config_manager: Any,
    *,
    user_id: Optional[str] = None,
    old_cookie: Optional[str] = None,
    new_cookie: str,
) -> bool:
    """
    Load users.json via `config_manager`, apply the cookie update, then save.

    `config_manager` must expose `load_users()` and `save_users(users)`.
    """
    if config_manager is None:
        _logger.debug("[Cookie] persist skipped: config_manager is None")
        return False
    try:
        users = config_manager.load_users() or {}
    except Exception:
        _logger.debug("[Cookie] persist skipped: load_users failed", exc_info=True)
        return False

    if not update_cookie_in_users_dict(users, user_id=user_id, old_cookie=old_cookie, new_cookie=new_cookie):
        _logger.debug(
            "[Cookie] persist skipped: no matching user update (uid=%s old=%s new=%s)",
            user_id,
            _cookie_fp(old_cookie),
            _cookie_fp(new_cookie),
        )
        return False

    try:
        ok = bool(config_manager.save_users(users))
        if ok:
            _logger.info(
                "[Cookie] persisted update (uid=%s old=%s new=%s)",
                user_id,
                _cookie_fp(old_cookie),
                _cookie_fp(new_cookie),
            )
        else:
            err = ""
            try:
                get_err = getattr(config_manager, "get_cookie_error", None)
                if callable(get_err):
                    err = str(get_err() or "")
            except Exception:
                err = ""

            if err:
                _logger.warning("[Cookie] persist failed (uid=%s): %s", user_id, err)
            else:
                _logger.warning("[Cookie] persist failed (uid=%s)", user_id)
        return ok
    except Exception:
        _logger.warning("[Cookie] persist failed (uid=%s): save_users raised", user_id, exc_info=True)
        return False
