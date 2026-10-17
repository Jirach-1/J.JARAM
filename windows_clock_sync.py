"""Request a one-time sync from the configured Windows Time service."""

import os
import subprocess


MISC_SYNC_WINDOWS_CLOCK_ON_STARTUP_KEY = "sync_windows_clock_on_startup"


def should_sync_windows_clock_on_startup(settings: object) -> bool:
    if not isinstance(settings, dict):
        return False
    misc = settings.get("misc")
    return isinstance(misc, dict) and bool(
        misc.get(MISC_SYNC_WINDOWS_CLOCK_ON_STARTUP_KEY, False)
    )


def sync_windows_clock() -> tuple[bool, str]:
    """Ask Windows Time to resync; leave its service configuration untouched."""
    if os.name != "nt":
        return False, "Windows clock sync is available only on Windows."

    try:
        result = subprocess.run(
            ["w32tm", "/resync"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "Windows clock sync timed out."
    except OSError as exc:
        return False, f"Windows clock sync could not start: {exc}"

    output = (result.stderr or result.stdout or "").strip()
    if result.returncode == 0:
        return True, "Windows clock sync requested successfully."
    detail = output or f"w32tm exited with code {result.returncode}"
    return False, f"Windows clock sync failed: {detail}"
