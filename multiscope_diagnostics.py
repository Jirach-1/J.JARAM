"""Account membership reports for comparing the manager, reader, and table."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
import math
from multiscope_metrics import TIMING_LABELS, TIMING_FIELDS
from webhook_delivery import safe_rate_limit_event


def _numeric_fields(source, names):
    return {name: source[name] for name in names if name in source
            and isinstance(source[name], (int, float)) and math.isfinite(source[name])}


def is_multiscope_attachment_log(message):
    """Discovery/attachment progress stays in the main Logs tab."""
    text = str(message).lower()
    return (text.startswith('[multiscope] switched log for ')
            or text.startswith('[multiscope] saved log attachments unavailable:')
            or (text.startswith('[multiscope] restored ') and ' saved log attachments' in text))


def is_multiscope_log(message):
    text = str(message).lower()
    return not is_multiscope_attachment_log(text) and text.startswith(('[multiscope]', '[multiscopeprocess]', '[webhook] biome ',
                            '[webhook] merchant mari:', '[webhook] merchant rin:',
                            '[webhook] merchant jester:', '[webhook] event '))


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
    delivery = reader.get('delivery') or {}
    timings = {**(reader.get('timings') or {}), **(delivery.get('timings') or {})}
    stats = reader.get('statistics') or {}
    return {
        'captured_at_unix': time.time(),
        "counts": {**{name: len(ids) for name, ids in stages.items()}, "server_rows": len(rows or [])},
        "stages": {name: sorted(ids) for name, ids in stages.items()},
        "differences": {name: [{"user_id": uid, "username": usernames.get(uid, uid)}
                                for uid in sorted(ids)] for name, ids in differences.items()},
        "usernames": usernames,
        "reader_health": "unhealthy" if process.get("alive") is False else str(index.get("health") or "unknown"),
        "reader_index": {key: index[key] for key in index_keys if key in index},
        "reader_process": {key: process[key] for key in process_keys if key in process},
        "reader_resume": _numeric_fields(reader.get('resume') or {},
            ('restored', 'discovery', 'missing_cursor', 'username_changed_or_missing', 'validation_failed')),
        'reader_handles': _numeric_fields(reader.get('handles') or {},
            ('capacity', 'open_handles', 'reserved_handles', 'peak_open_handles', 'hits', 'misses',
             'opens', 'evictions', 'invalidations', 'close_failures')),
        'reader_counters': _numeric_fields(reader, ('bytes_read', 'lines_read', 'read_failures',
                                                   'decode_errors', 'oversized_lines', 'backlog_users',
                                                   'tail_workers', 'last_cycle_tailed_users', 'tail_cycles')),
        'delivery': {**_numeric_fields(delivery, ('accepted', 'delivered', 'retried', 'failed', 'unconfirmed',
                                              'expired', 'rejected', 'cancelled', 'http_attempts', 'rate_limited',
                                              'server_errors', 'pending', 'queued', 'workers', 'capacity',
                                              'oldest_pending_seconds', 'global_cooldown_seconds',
                                              'destination_cooldown_seconds', 'last_http_status', 'active_destinations')),
                     **{key: [safe_rate_limit_event(event) for event in delivery.get(key, [])[-32:]
                              if isinstance(event, dict)]
                        for key in ('recent_rate_limits', 'recent_success_cooldowns')
                        if isinstance(delivery.get(key), list)}},
        'timings': {stage: _numeric_fields(values, TIMING_FIELDS) for stage, values in timings.items()
                    if stage in TIMING_LABELS and isinstance(values, dict)},
        'statistics': {**_numeric_fields(stats, ('dirty', 'save_failures', 'writer_alive', 'seconds_since_save')),
                       'last_error_type': str(stats.get('last_error_type') or '')[:80]
                       if str(stats.get('last_error_type') or '').isidentifier() else ''},
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
