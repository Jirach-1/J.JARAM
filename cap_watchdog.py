from __future__ import annotations

from typing import MutableMapping, Tuple


DEFAULT_CAP_WATCHDOG_SETTINGS = {
    "missing_username_increments_cap": True,
    "missing_username_timeout_seconds": 360,
    "in_menu_none_increments_cap": True,
    "in_menu_none_timeout_seconds": 120,
    "cap_counter_limit": 3,
}


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def normalize_cap_watchdog_settings(raw: object) -> dict:
    """Return a complete, bounded CAP watchdog configuration."""
    source = raw if isinstance(raw, dict) else {}
    defaults = DEFAULT_CAP_WATCHDOG_SETTINGS
    return {
        "missing_username_increments_cap": bool(
            source.get(
                "missing_username_increments_cap",
                defaults["missing_username_increments_cap"],
            )
        ),
        "missing_username_timeout_seconds": _bounded_int(
            source.get("missing_username_timeout_seconds"),
            defaults["missing_username_timeout_seconds"],
            1,
            86_400,
        ),
        "in_menu_none_increments_cap": bool(
            source.get(
                "in_menu_none_increments_cap",
                defaults["in_menu_none_increments_cap"],
            )
        ),
        "in_menu_none_timeout_seconds": _bounded_int(
            source.get("in_menu_none_timeout_seconds"),
            defaults["in_menu_none_timeout_seconds"],
            1,
            86_400,
        ),
        "cap_counter_limit": _bounded_int(
            source.get("cap_counter_limit"),
            defaults["cap_counter_limit"],
            1,
            100,
        ),
    }


def increment_cap_counter(
    state: MutableMapping[str, object],
    *,
    enabled: bool,
    limit: int,
    key: str = "log_miss_streak",
) -> Tuple[int, bool, bool]:
    """
    Increment a user's shared CAP counter when enabled.

    Returns ``(counter, counted, reached_limit)``. When disabled, the existing
    value is preserved so callers can still perform their normal process kill
    and recycle behavior without changing CAP state.
    """
    try:
        current = max(0, int(state.get(key, 0) or 0))
    except (TypeError, ValueError, OverflowError):
        current = 0
    threshold = _bounded_int(limit, DEFAULT_CAP_WATCHDOG_SETTINGS["cap_counter_limit"], 1, 100)
    if not bool(enabled):
        return current, False, False
    current += 1
    state[key] = current
    return current, True, current >= threshold
