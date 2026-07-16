DEFAULT_LAUNCH_PRIORITY = 0
MIN_LAUNCH_PRIORITY = -3
MAX_LAUNCH_PRIORITY = 3


def coerce_launch_priority(value: object) -> int:
    try:
        if value is None:
            return DEFAULT_LAUNCH_PRIORITY
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            priority = int(value)
        else:
            text = str(value or "").strip()
            if not text:
                return DEFAULT_LAUNCH_PRIORITY
            priority = int(float(text))
    except Exception:
        priority = DEFAULT_LAUNCH_PRIORITY
    return max(MIN_LAUNCH_PRIORITY, min(MAX_LAUNCH_PRIORITY, priority))


def launch_priority_value(user_info: object) -> int:
    if isinstance(user_info, dict):
        return coerce_launch_priority(user_info.get("launch_priority", DEFAULT_LAUNCH_PRIORITY))
    return DEFAULT_LAUNCH_PRIORITY


def launch_priority_sort_key(uid: object, user_info: object, fallback_index: int = 0) -> tuple:
    return (-launch_priority_value(user_info), int(fallback_index))


def sort_user_items_by_launch_priority(items) -> list:
    indexed = list(enumerate(list(items or [])))
    indexed.sort(
        key=lambda pair: launch_priority_sort_key(
            pair[1][0],
            pair[1][1] if len(pair[1]) > 1 else {},
            pair[0],
        )
    )
    return [item for _idx, item in indexed]


def sort_user_ids_by_launch_priority(user_ids, info_lookup) -> list:
    indexed = []
    for idx, uid in enumerate(list(user_ids or [])):
        info = {}
        try:
            info = info_lookup(uid)
        except Exception:
            info = {}
        indexed.append((idx, str(uid), info if isinstance(info, dict) else {}))
    indexed.sort(key=lambda item: launch_priority_sort_key(item[1], item[2], item[0]))
    return [uid for _idx, uid, _info in indexed]
