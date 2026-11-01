"""Shared event definitions and merchant-style webhook formatting."""
from __future__ import annotations

import re
from datetime import datetime


EVENTS = {
    "sinister_s": {
        "name": "Sinister S",
        "asset_id": "136698315787897",
        "filter_id": "event_sinister_s",
        "target_text": "Sinister S is patrolling the graveyard...",
        "color": 0x9598F9,
        "emoji": "🎃",
        "cooldown_seconds": 200.0,
    },
}
EVENT_FILTER_IDS = {event["filter_id"]: key for key, event in EVENTS.items()}
EVENT_ASSET_IDS = {event["asset_id"]: key for key, event in EVENTS.items()}
EVENT_ASSET_PREFILTERS = tuple(f"rbxassetid://{asset_id}" for asset_id in EVENT_ASSET_IDS)
EVENT_ASSET_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{0,6})?Z),"
    r"[^\n]*?rbxassetid://(?P<asset_id>" + "|".join(EVENT_ASSET_IDS) + r")(?!\d)[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)


def get_event_ocr_preset(event_id: str) -> dict:
    event = EVENTS[event_id]
    return {
        "id": event["filter_id"], "name": event["name"],
        "r": 149, "g": 152, "b": 249, "tol": 60,
        "enabled": True, "solo_ocr": False,
        "target_text": event["target_text"],
        "cooldown_seconds": event["cooldown_seconds"],
        "use_shared_area": True, "shared_area_id": "chat",
        "roi": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
        "webhook_url": "", "webhook_message": "",
        "send_screenshot": True, "repeat_alert_sound": False,
        "user_ids": None, "behavior": "event", "event_id": event_id,
        "cooldown_group": event["filter_id"],
    }


def event_id_for_filter(spec: dict) -> str:
    event_id = str(spec.get("event_id") or "")
    if event_id in EVENTS:
        return event_id
    event_id = EVENT_FILTER_IDS.get(str(spec.get("id") or ""), "")
    if event_id:
        return event_id
    name = str(spec.get("name") or "").strip().lower()
    return next((key for key, event in EVENTS.items() if event["name"].lower() == name), "")


def iter_event_matches(text: str) -> list[dict]:
    return [
        {"event_id": EVENT_ASSET_IDS[match["asset_id"]],
         "timestamp": match["timestamp"], "full_line": match["full_line"]}
        for match in EVENT_ASSET_RE.finditer(text)
    ]


def normalize_event_webhooks(webhooks: list) -> list[dict]:
    """Keep one configuration per known event, including unconfigured events.

    For older rows with multiple destinations, prefer the first enabled URL,
    then the first configured URL. Event names always come from the catalog.
    """
    hooks = [hook for hook in (webhooks or []) if isinstance(hook, dict)]
    normalized = []
    for event_id, event in EVENTS.items():
        candidates = []
        for hook in hooks:
            configured_event = str(hook.get("event_id") or "")
            if configured_event and configured_event != event_id:
                continue
            modes = hook.get("event_modes")
            modes = modes if isinstance(modes, dict) else {}
            if configured_event != event_id and event_id not in modes:
                continue
            mode = str(modes.get(event_id, "None")).strip().capitalize()
            if mode not in ("None", "Message", "Everyone"):
                mode = "None"
            ping_type = str(hook.get("ping_type") or "None").strip()
            if ping_type not in ("None", "User ID", "Role ID"):
                ping_type = "None"
            candidates.append({
                "event_id": event_id, "name": event["name"],
                "url": str(hook.get("url") or "").strip(),
                "event_modes": {event_id: mode},
                "ping_type": ping_type, "ping_id": str(hook.get("ping_id") or "").strip(),
            })
        configured = [hook for hook in candidates if hook["url"]]
        enabled = [hook for hook in configured if hook["event_modes"][event_id] != "None"]
        normalized.append(next(iter(enabled or configured or candidates), {
            "event_id": event_id, "name": event["name"], "url": "",
            "event_modes": {event_id: "None"},
            "ping_type": "None", "ping_id": "",
        }))
    return normalized


def event_webhook_targets(webhooks: list, event_id: str):
    for hook in normalize_event_webhooks(webhooks):
        if hook["event_id"] != event_id:
            continue
        url = hook["url"]
        mode = hook["event_modes"][event_id]
        if url and mode in ("Message", "Everyone"):
            content = "@everyone" if mode == "Everyone" else ""
            if mode == "Message" and hook["ping_id"]:
                if hook["ping_type"] == "Role ID":
                    content = f"<@&{hook['ping_id']}>"
                elif hook["ping_type"] == "User ID":
                    content = f"<@{hook['ping_id']}>"
            yield url, content


def build_event_payload(event_id: str, *, owner: str, detector: str,
                        server_label: str, ps_link: str, detected_at: datetime,
                        footer: str, content: str = "", screenshot: bool = False) -> dict:
    event = EVENTS[event_id]
    ts = int(detected_at.timestamp())
    embed = {
        "title": f"{event['emoji']} {event['name']} Has Arrived!",
        "description": (
            f"**Owner:** `{owner}`\n"
            f"**Detected by:** `{detector}`\n"
            f"**Detected At:** <t:{ts}:D> • <t:{ts}:T> (<t:{ts}:R>)\n"
            "**Private Server:** " + (f"[Private Server Link]({ps_link})" if ps_link else "`N/A`")
        ),
        "color": event["color"], "timestamp": detected_at.isoformat(),
        "footer": {"text": f"{footer}  •  {server_label}"},
    }
    if screenshot:
        embed["image"] = {"url": "attachment://chat.png"}
    return {"content": content, "embeds": [embed]}
