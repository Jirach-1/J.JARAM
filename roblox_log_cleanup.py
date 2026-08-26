from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


SETTINGS_KEY = "roblox_log_cleanup"
DEFAULT_CONFIG = {
    "enabled": False,
    "inactive_age": 24,
    "inactive_unit": "hours",
}

_UNIT_SECONDS = {
    "minutes": 60,
    "hours": 60 * 60,
    "days": 24 * 60 * 60,
}


@dataclass(frozen=True)
class CleanupResult:
    logs_dir: str
    folder_exists: bool
    scanned: int = 0
    deleted: int = 0
    bytes_deleted: int = 0
    skipped_recent: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()


def get_roblox_logs_dir() -> Path:
    """Return the standard per-user Roblox Player log directory."""
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data)
    else:
        root = Path.home() / "AppData" / "Local"
    return root / "Roblox" / "logs"


def normalize_cleanup_config(config: object) -> dict:
    raw: Mapping = config if isinstance(config, Mapping) else {}

    try:
        inactive_age = int(raw.get("inactive_age", DEFAULT_CONFIG["inactive_age"]))
    except (TypeError, ValueError):
        inactive_age = int(DEFAULT_CONFIG["inactive_age"])
    inactive_age = max(1, min(9999, inactive_age))

    inactive_unit = str(raw.get("inactive_unit", DEFAULT_CONFIG["inactive_unit"]) or "").lower()
    if inactive_unit not in _UNIT_SECONDS:
        inactive_unit = str(DEFAULT_CONFIG["inactive_unit"])

    return {
        "enabled": bool(raw.get("enabled", DEFAULT_CONFIG["enabled"])),
        "inactive_age": inactive_age,
        "inactive_unit": inactive_unit,
    }


def inactive_seconds(config: object) -> int:
    normalized = normalize_cleanup_config(config)
    return int(normalized["inactive_age"]) * _UNIT_SECONDS[str(normalized["inactive_unit"])]


def cleanup_inactive_log_files(
    logs_dir: Optional[str | os.PathLike[str]] = None,
    *,
    max_inactive_seconds: float,
    now: Optional[float] = None,
) -> CleanupResult:
    """
    Delete stale, top-level ``.log`` files from the Roblox log directory.

    Symlinks, directories, other file types, and nested files are deliberately
    ignored. Each candidate is stat'ed again immediately before deletion so a
    file modified during the scan is kept.
    """
    root = Path(logs_dir) if logs_dir is not None else get_roblox_logs_dir()
    root_text = os.path.abspath(os.fspath(root))

    try:
        threshold = float(max_inactive_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_inactive_seconds must be a positive number") from exc
    if threshold <= 0:
        raise ValueError("max_inactive_seconds must be greater than zero")

    reference_time = float(time.time() if now is None else now)
    cutoff = reference_time - threshold

    if not root.is_dir():
        return CleanupResult(logs_dir=root_text, folder_exists=False)

    scanned = 0
    deleted = 0
    bytes_deleted = 0
    skipped_recent = 0
    failed = 0
    errors: list[str] = []

    try:
        entries = os.scandir(root)
    except OSError as exc:
        return CleanupResult(
            logs_dir=root_text,
            folder_exists=True,
            failed=1,
            errors=(str(exc),),
        )

    with entries:
        for entry in entries:
            if not entry.name.lower().endswith(".log"):
                continue
            try:
                file_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                scanned += 1
                if float(file_stat.st_mtime) > cutoff:
                    skipped_recent += 1
                    continue

                latest_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(latest_stat.st_mode) or float(latest_stat.st_mtime) > cutoff:
                    skipped_recent += 1
                    continue

                os.unlink(entry.path)
                deleted += 1
                bytes_deleted += max(0, int(latest_stat.st_size))
            except FileNotFoundError:
                # Another process already removed the candidate; there is
                # nothing left for this cleanup pass to do.
                continue
            except OSError as exc:
                failed += 1
                if len(errors) < 10:
                    errors.append(f"{entry.name}: {exc}")

    return CleanupResult(
        logs_dir=root_text,
        folder_exists=True,
        scanned=scanned,
        deleted=deleted,
        bytes_deleted=bytes_deleted,
        skipped_recent=skipped_recent,
        failed=failed,
        errors=tuple(errors),
    )
