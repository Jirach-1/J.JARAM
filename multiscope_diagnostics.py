"""Account membership reports for comparing the manager, reader, and table."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def row_user_ids(rows):
    return {str(uid) for row in rows or [] if isinstance(row, dict)
            for uid in row.get("users") or []}


def eligible_user_ids(users):
    return {str(uid) for uid, info in users.items() if isinstance(info, dict)
            and not any(info.get(flag) for flag in ("disabled", "bad", "cap"))}


def runtime_report(*, saved_users, manager_users, user_states, active_pids, status,
                   requested_users, rows, reader):
    stages = {
        "enabled_in_config": eligible_user_ids(saved_users),
        "enabled_in_manager": eligible_user_ids(manager_users),
        "enabled_in_user_states": eligible_user_ids({
            uid: state.get("user_info", {}) for uid, state in user_states.items()
            if isinstance(state, dict)
        }),
        "live_process_users": {str(uid) for uid, pids in active_pids.items() if pids},
        "live_status_users": {str(uid) for uid, row in status.items()
                              if isinstance(row, dict) and row.get("pids")},
        "requested_users": {str(uid) for uid in requested_users},
        "snapshot_users": row_user_ids(rows),
    }
    # Missing diagnostics are unknown, rather than evidence of zero readers.
    for key, source in (("engine_tracked_users", "tracked_user_ids"),
                        ("attached_log_users", "attached_user_ids")):
        if isinstance(reader.get(source), (list, tuple, set)):
            stages[key] = {str(uid) for uid in reader[source]}
    live_enabled = stages["enabled_in_config"] & stages["live_process_users"]
    differences = {
        "live_missing_from_user_states": live_enabled - stages["enabled_in_user_states"],
        "live_missing_from_request": live_enabled - stages["requested_users"],
        "live_missing_from_snapshot": live_enabled - stages["snapshot_users"],
    }
    if "engine_tracked_users" in stages:
        differences["requested_missing_from_engine"] = stages["requested_users"] - stages["engine_tracked_users"]
        differences["engine_not_requested"] = stages["engine_tracked_users"] - stages["requested_users"]
    if "attached_log_users" in stages and "engine_tracked_users" in stages:
        differences["tracked_without_log"] = stages["engine_tracked_users"] - stages["attached_log_users"]
    usernames = {str(uid): str(info.get("username") or uid)
                 for source in (manager_users, saved_users) for uid, info in source.items()
                 if isinstance(info, dict)}
    index = reader.get("index") or {}
    process = reader.get("process") or {}
    # Keep reports useful for blocked/dead readers without exporting paths,
    # credentials, private links, or arbitrary payloads from future diagnostics.
    index_keys = ("health", "refreshes", "refresh_failures", "last_refresh_seconds",
                  "max_refresh_seconds", "last_refresh_error_type", "indexed_files",
                  "pending_files", "unreadable_files", "watcher_active",
                  "seconds_since_success", "consecutive_failures")
    process_keys = ("pid", "alive", "exit_code", "seconds_since_message",
                    "last_message_type",
                    "seconds_since_diagnostics", "tick_pending", "tick_wait_seconds",
                    "latest_tick_waiting")
    return {
        "counts": {**{name: len(ids) for name, ids in stages.items()}, "server_rows": len(rows or [])},
        "stages": {name: sorted(ids) for name, ids in stages.items()},
        "differences": {name: [{"user_id": uid, "username": usernames.get(uid, uid)}
                                for uid in sorted(ids)] for name, ids in differences.items()},
        "usernames": usernames,
        "reader_health": "unhealthy" if process.get("alive") is False else str(index.get("health") or "unknown"),
        "reader_index": {key: index[key] for key in index_keys if key in index},
        "reader_process": {key: process[key] for key in process_keys if key in process},
    }


def write_report(config_dir, name, report):
    """Publish one complete report without credentials or private server links."""
    directory = Path(config_dir) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    payload = {"captured_at_unix": time.time(), "application_pid": os.getpid(), **report}
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
