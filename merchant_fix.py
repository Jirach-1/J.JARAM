"""Merchant Fix backend.

The normal J.JARAM process stays unelevated.  Privileged work is performed by a
small copy of this module launched through UAC and controlled over an
authenticated localhost connection.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Any, Callable, Optional

import psutil


DOMAIN = "assetdelivery.roblox.com"
SMART_LOG_MARKER = (
    "[FLog::CreatorOutput] Info: Script "
    "'ReplicatedFirst.ClientHandlers.BossRaidUI', Line 114"
)
SMART_LOG_PATTERN = re.compile(re.escape(SMART_LOG_MARKER))
SMART_BLOCK_TRIGGER_BOSS_RAID_UI = "boss_raid_ui"
SMART_BLOCK_TRIGGER_MENU_EXIT = "menu_exit"
DEFAULT_SMART_BLOCK_TRIGGER = SMART_BLOCK_TRIGGER_BOSS_RAID_UI
DEFAULT_POST_MARKER_DELAY_SECONDS = 0.0
DEFAULT_MENU_EXIT_DELAY_SECONDS = 0.0
MAX_POST_MARKER_DELAY_SECONDS = 60.0
SMART_DATABASE_LOCK_SECONDS = 10.0
SMART_ACTIVITY_LOG_INTERVAL_SECONDS = 30.0
SMART_ACTIVITY_LOG_PID_SAMPLE_SIZE = 12
# The fallback PID-to-log matcher is only used when psutil cannot report the
# log opened by the Roblox process.  A shutdown can update an old log after a
# replacement process has started, so modification time alone is not enough
# to prove that a log belongs to the replacement.
SMART_LOG_CREATION_EARLY_TOLERANCE_SECONDS = 2.0
SMART_USER_SCOPE_ALL = "all"
SMART_USER_SCOPE_SELECTED = "selected"
SMART_USER_SCOPE_WHITELIST = "whitelist"
SMART_USER_SCOPE_BLACKLIST = "blacklist"
HOSTS_START = "# JARAM MERCHANT FIX START"
HOSTS_END = "# JARAM MERCHANT FIX END"
ROBLOX_PROCESS_NAME = "robloxplayerbeta.exe"
STORAGE_DATABASE_NAMES = (
    "rbx-storage.db",
    "rbx-storage.db-wal",
    "rbx-storage.db-shm",
)
MANAGER_RESUME_STATE_KEY = "merchant_fix_smart_state"
_HANDOFF_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STORAGE_HOLDING_NAME_PATTERN = re.compile(
    r"^rbx-storage\.jaram-held-[0-9a-f]{32}$"
)
_STORAGE_PLACEHOLDER_NAME_PATTERN = re.compile(
    r"^rbx-storage\.jaram-placeholder-[0-9a-f]{32}$"
)

_LOG_LOCK = threading.Lock()
_LOG_PATH_OVERRIDE: Optional[Path] = None


class _HelperTransportError(RuntimeError):
    """The helper IPC channel failed before a valid response was received."""


def normalize_post_marker_delay(value: object) -> float:
    """Return a safe Smart-mode delay in seconds for saved or UI-provided values."""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return DEFAULT_POST_MARKER_DELAY_SECONDS
    if not (delay >= 0.0):  # Also rejects NaN.
        return DEFAULT_POST_MARKER_DELAY_SECONDS
    return min(delay, MAX_POST_MARKER_DELAY_SECONDS)


def normalize_smart_block_trigger(value: object) -> str:
    """Return a supported event that starts the Smart asset-block delay."""
    normalized = str(value or "").strip().lower()
    if normalized in {
        SMART_BLOCK_TRIGGER_MENU_EXIT,
        "exit_main_menu",
        "main_menu_exit",
    }:
        return SMART_BLOCK_TRIGGER_MENU_EXIT
    return SMART_BLOCK_TRIGGER_BOSS_RAID_UI


def _smart_menu_states_from_log_text(text: str) -> tuple[bool, ...]:
    """Use MultiScope's established BloxstrapRPC menu-state interpretation."""
    # Keep this import lazy: the elevated helper never needs MultiScope and
    # should not load its networking and detection dependencies at startup.
    from multiscope import _extract_in_menu_from_rpc, _extract_rpc_entries_from_text

    states: list[bool] = []
    for rpc, _timestamp in _extract_rpc_entries_from_text(
        text, extract_timestamp=False
    ):
        state = _extract_in_menu_from_rpc(rpc)
        if state is not None:
            states.append(bool(state))
    return tuple(states)


def normalize_smart_user_scope(value: object) -> str:
    """Return the supported Smart user scope, preserving legacy all-user behavior."""
    normalized = str(value or "").strip().lower()
    if normalized in {SMART_USER_SCOPE_SELECTED, SMART_USER_SCOPE_WHITELIST}:
        return SMART_USER_SCOPE_WHITELIST
    if normalized in {SMART_USER_SCOPE_BLACKLIST, "denylist"}:
        return SMART_USER_SCOPE_BLACKLIST
    # Legacy "all" is represented by an empty blacklist in the new picker.
    return SMART_USER_SCOPE_BLACKLIST


def normalize_smart_user_ids(value: object) -> tuple[str, ...]:
    """Normalize persisted manager user IDs without treating a string as a list."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(
        sorted(
            {
                str(user_id).strip()
                for user_id in value
                if user_id is not None and str(user_id).strip()
            }
        )
    )


def get_jram_logs_dir() -> Path:
    root = Path(os.environ.get("APPDATA") or Path.home())
    path = root / "Jaram" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_merchant_fix_log_path() -> Path:
    if _LOG_PATH_OVERRIDE is not None:
        _LOG_PATH_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
        return _LOG_PATH_OVERRIDE
    return get_jram_logs_dir() / "MerchantFix.log"


def get_acl_recovery_path() -> Path:
    return get_merchant_fix_log_path().parent.parent / "merchant_fix_acl_recovery.json"


def get_helper_handoff_path() -> Path:
    return get_merchant_fix_log_path().parent.parent / "merchant_fix_helper_handoff.json"


def _write_log(message: str, *, level: str = "INFO") -> None:
    line = (
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"[{level}] pid={os.getpid()} {str(message).rstrip()}\n"
    )
    try:
        with _LOG_LOCK:
            with get_merchant_fix_log_path().open("a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        pass


def get_roblox_storage_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        raise RuntimeError("LOCALAPPDATA is unavailable; the Roblox folder could not be located.")
    return Path(local_appdata) / "Roblox" / "rbx-storage"


def get_current_user_sid() -> str:
    if os.name != "nt":
        raise RuntimeError("Windows user SIDs are only available on Windows.")
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    return str(win32security.ConvertSidToStringSid(sid))


def _validate_storage_dir(path: Path) -> Path:
    expected = get_roblox_storage_dir().resolve(strict=False)
    resolved = Path(path).resolve(strict=False)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(expected)):
        raise ValueError(f"Refusing to modify an unexpected storage path: {resolved}")
    return resolved


def _validate_helper_storage_dir(path: Path) -> Path:
    """Validate the unelevated parent's storage path without changing user profiles."""
    raw = Path(path)
    if not raw.is_absolute():
        raise ValueError("The Roblox storage path must be absolute.")
    resolved = raw.resolve(strict=False)
    if resolved.name.lower() != "rbx-storage" or resolved.parent.name.lower() != "roblox":
        raise ValueError(f"Refusing to modify an unexpected storage path: {resolved}")
    return resolved


def close_roblox_processes(timeout: float = 8.0) -> int:
    """Terminate every Roblox Player process and wait until each one exits."""
    processes: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if str(process.info.get("name") or "").lower() == ROBLOX_PROCESS_NAME:
                processes.append(process)
        except (psutil.Error, OSError):
            continue

    for process in processes:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass

    _, alive = psutil.wait_procs(processes, timeout=max(0.1, float(timeout) * 0.65))
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    if alive:
        _, alive = psutil.wait_procs(alive, timeout=max(0.1, float(timeout) * 0.35))
    if alive:
        raise RuntimeError(f"{len(alive)} Roblox process(es) could not be closed.")

    _write_log(f"Closed {len(processes)} Roblox process(es).")
    return len(processes)


def clear_roblox_storage() -> int:
    """Remove rbx-storage contents and its database files from LocalAppData Roblox."""
    storage = _validate_storage_dir(get_roblox_storage_dir())
    if storage.exists() and not storage.is_dir():
        raise RuntimeError(f"Roblox storage path is not a directory: {storage}")

    removed = 0
    targets = list(storage.iterdir()) if storage.is_dir() else []
    targets.extend(
        storage.parent / name
        for name in STORAGE_DATABASE_NAMES
    )
    for child in targets:
        # Only storage children and three exact sibling database paths are allowed.
        try:
            if not child.exists() and not child.is_symlink():
                continue
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.parent == storage:
                shutil.rmtree(child)
            else:
                raise RuntimeError(f"Expected a storage database file, but found a directory: {child}")
            removed += 1
        except FileNotFoundError:
            continue
        except PermissionError:
            try:
                os.chmod(child, 0o700)
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except Exception as exc:
                raise RuntimeError(f"Could not delete {child.name}: {exc}") from exc

    _write_log(
        f"Cleared {removed} rbx-storage item(s), including database files, from {storage.parent}."
    )
    return removed


def is_classic_block_active() -> bool:
    if os.name != "nt":
        return False
    try:
        text = _hosts_path().read_text(encoding="utf-8", errors="replace")
        return HOSTS_START in text and HOSTS_END in text
    except Exception:
        return False


def _hosts_path() -> Path:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(system_root) / "System32" / "drivers" / "etc" / "hosts"


def _set_classic_hosts_block(enabled: bool) -> None:
    path = _hosts_path()
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(HOSTS_START)}\s*$.*?^\s*{re.escape(HOSTS_END)}\s*$\r?\n?"
    )
    updated = pattern.sub("", text).rstrip("\r\n")
    if enabled:
        block = (
            f"{HOSTS_START}\n"
            f"0.0.0.0 {DOMAIN}\n"
            f":: {DOMAIN}\n"
            f"{HOSTS_END}"
        )
        updated = f"{updated}\n\n{block}" if updated else block
    path.write_text(updated + "\n", encoding="utf-8", newline="")
    try:
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.run(
            ["ipconfig.exe", "/flushdns"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception:
        pass
    _write_log(f"Classic hosts block {'enabled' if enabled else 'disabled'} in {path}.")


def _resolve_domain() -> set[str]:
    addresses: set[str] = set()
    for family, _, _, _, sockaddr in socket.getaddrinfo(DOMAIN, None, socket.AF_UNSPEC):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        addresses.add(str(ipaddress.ip_address(sockaddr[0].split("%", 1)[0])))
    if not addresses:
        raise RuntimeError(f"No addresses were returned for {DOMAIN}.")
    return addresses


def _socket_filter(pid: int, addresses: set[str]) -> str:
    terms = " or ".join(
        f"remoteAddr == {address}"
        for address in sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))
    )
    if not terms:
        raise ValueError("No destination addresses were supplied.")
    return f"event == CONNECT and processId == {int(pid)} and ({terms})"


def _matching_connections_by_pid(
    pids: set[int],
    addresses: set[str],
    *,
    connections: Optional[list[Any]] = None,
) -> dict[int, list[dict[str, Any]]]:
    """Return matching live flows after one machine-wide connection snapshot."""
    target_pids: set[int] = set()
    for raw_pid in pids:
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            target_pids.add(pid)
    matches: dict[int, dict[tuple[Any, ...], dict[str, Any]]] = {
        pid: {} for pid in target_pids
    }
    snapshot = psutil.net_connections(kind="inet") if connections is None else connections
    for connection in snapshot:
        try:
            pid = int(connection.pid or 0)
        except (TypeError, ValueError):
            continue
        if pid not in target_pids or not connection.raddr:
            continue
        try:
            remote_ip = str(ipaddress.ip_address(connection.raddr.ip))
            local_ip = str(ipaddress.ip_address(connection.laddr.ip))
        except Exception:
            continue
        if remote_ip not in addresses:
            continue
        if connection.type == socket.SOCK_STREAM:
            protocol = "tcp"
        elif connection.type == socket.SOCK_DGRAM:
            protocol = "udp"
        else:
            continue
        item = {
            "protocol": protocol,
            "local_ip": local_ip,
            "local_port": int(connection.laddr.port),
            "remote_ip": remote_ip,
            "remote_port": int(connection.raddr.port),
        }
        key = tuple(item.values())
        matches[pid][key] = item
    return {pid: list(items.values()) for pid, items in matches.items()}


def _matching_connections(pid: int, addresses: set[str]) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that only need one PID."""
    pid_i = int(pid)
    return _matching_connections_by_pid({pid_i}, addresses).get(pid_i, [])


def _flow_filter(item: dict[str, Any]) -> str:
    proto = str(item["protocol"])
    local_ip = str(item["local_ip"])
    remote_ip = str(item["remote_ip"])
    local_port = int(item["local_port"])
    remote_port = int(item["remote_port"])
    if ipaddress.ip_address(local_ip).version == 4:
        family = "ip"
        src = "ip.SrcAddr"
        dst = "ip.DstAddr"
    else:
        family = "ipv6"
        src = "ipv6.SrcAddr"
        dst = "ipv6.DstAddr"
    outgoing = (
        f"(outbound and {family} and {proto} and {src} == {local_ip} and "
        f"{dst} == {remote_ip} and {proto}.SrcPort == {local_port} and "
        f"{proto}.DstPort == {remote_port})"
    )
    incoming = (
        f"(inbound and {family} and {proto} and {src} == {remote_ip} and "
        f"{dst} == {local_ip} and {proto}.SrcPort == {remote_port} and "
        f"{proto}.DstPort == {local_port})"
    )
    return f"({outgoing} or {incoming})"


class _SmartActivityLogger:
    """Combine routine Smart blocker events without hiding errors or state changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, dict[str, Any]] = {}
        self._logged_once = False
        self._last_flush_at = 0.0

    def _take_locked(self) -> list[tuple[str, int, tuple[int, ...], bool]]:
        snapshot = [
            (
                label,
                int(event["count"]),
                tuple(sorted(event["pids"])),
                bool(event["pids_truncated"]),
            )
            for label, event in self._events.items()
        ]
        self._events.clear()
        return snapshot

    @staticmethod
    def _format(snapshot: list[tuple[str, int, tuple[int, ...], bool]]) -> str:
        parts = []
        for label, count, pids, pids_truncated in snapshot:
            detail = f"{count} {label}"
            if pids:
                samples = ", ".join(str(pid) for pid in pids)
                if pids_truncated:
                    samples += ", ..."
                detail += f" (PID sample: {samples})"
            parts.append(detail)
        return "Smart activity: " + "; ".join(parts) + "."

    def record(self, label: str, *, amount: int = 1, pid: object = None) -> None:
        try:
            normalized_amount = max(1, int(amount))
        except (TypeError, ValueError):
            normalized_amount = 1
        snapshot: list[tuple[str, int, tuple[int, ...], bool]] = []
        with self._lock:
            event = self._events.setdefault(
                str(label),
                {"count": 0, "pids": set(), "pids_truncated": False},
            )
            event["count"] += normalized_amount
            try:
                normalized_pid = int(pid)
            except (TypeError, ValueError):
                normalized_pid = 0
            if normalized_pid > 0 and normalized_pid not in event["pids"]:
                if len(event["pids"]) < SMART_ACTIVITY_LOG_PID_SAMPLE_SIZE:
                    event["pids"].add(normalized_pid)
                else:
                    event["pids_truncated"] = True

            now = time.monotonic()
            if not self._logged_once:
                self._logged_once = True
                self._last_flush_at = now
                snapshot = self._take_locked()
            elif now - self._last_flush_at >= SMART_ACTIVITY_LOG_INTERVAL_SECONDS:
                self._last_flush_at = now
                snapshot = self._take_locked()
        if snapshot:
            _write_log(self._format(snapshot))

    def flush(self) -> None:
        with self._lock:
            snapshot = self._take_locked()
            if snapshot:
                self._last_flush_at = time.monotonic()
        if snapshot:
            _write_log(self._format(snapshot))


class _PidDomainBlocker:
    """WinDivert SOCKET block plus NETWORK drops for already-open flows."""

    def __init__(
        self,
        pid: int,
        creation_time: float,
        addresses: set[str],
        *,
        activity_logger: Optional[_SmartActivityLogger] = None,
    ) -> None:
        self.pid = int(pid)
        self.creation_time = float(creation_time)
        self.addresses = set(addresses)
        self.socket_handle = None
        self.network_handles: list[Any] = []
        self.stop_event = threading.Event()
        self._receive_thread: Optional[threading.Thread] = None
        self._lifecycle_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._blocked_connections = 0
        self._first_hit_logged = False
        self._activity_logger = activity_logger
        self._stopped = False
        self._flow_keys: set[tuple[Any, ...]] = set()

    @property
    def blocked_connection_count(self) -> int:
        with self._stats_lock:
            return int(self._blocked_connections)

    def inherit_blocked_connections(self, count: object) -> None:
        """Carry hit totals across an address-refresh blocker replacement."""
        try:
            inherited = max(0, int(count))
        except (TypeError, ValueError):
            inherited = 0
        if not inherited:
            return
        with self._stats_lock:
            self._blocked_connections += inherited
            self._first_hit_logged = True

    def _record_blocked_connection(self, remote_port: int) -> None:
        first_hit = False
        with self._stats_lock:
            if self._stopped:
                return
            self._blocked_connections += 1
            if not self._first_hit_logged:
                self._first_hit_logged = True
                first_hit = True
        if self._activity_logger is not None:
            self._activity_logger.record(
                f"{DOMAIN} connection attempts blocked",
                pid=self.pid,
            )
        elif first_hit:
            _write_log(
                f"Smart block for PID {self.pid} intercepted its first {DOMAIN} "
                f"connection (remote port {int(remote_port)}); further hits will be "
                "counted silently."
            )

    @staticmethod
    def _flow_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(item.get("protocol") or ""),
            str(item.get("local_ip") or ""),
            int(item.get("local_port") or 0),
            str(item.get("remote_ip") or ""),
            int(item.get("remote_port") or 0),
        )

    @classmethod
    def _unique_flows(cls, flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in flows:
            try:
                unique[cls._flow_key(item)] = item
            except Exception:
                continue
        return list(unique.values())

    def _open_network_handles(
        self,
        flows: list[dict[str, Any]],
    ) -> tuple[list[Any], set[tuple[Any, ...]]]:
        from pydivert import windivert_dll
        from pydivert.consts import Flag, Layer

        new_handles: list[Any] = []
        opened_keys: set[tuple[Any, ...]] = set()
        pending = [
            item
            for item in self._unique_flows(flows)
            if self._flow_key(item) not in self._flow_keys
        ]
        try:
            for offset in range(0, len(pending), 8):
                chunk = pending[offset : offset + 8]
                text = " or ".join(_flow_filter(item) for item in chunk)
                handle = windivert_dll.WinDivertOpen(
                    text.encode("ascii"), int(Layer.NETWORK), 900, int(Flag.DROP)
                )
                new_handles.append(handle)
                opened_keys.update(self._flow_key(item) for item in chunk)
        except Exception:
            for handle in new_handles:
                try:
                    windivert_dll.WinDivertClose(handle)
                except Exception:
                    pass
            raise
        return new_handles, opened_keys

    def add_network_flows(self, flows: list[dict[str, Any]]) -> int:
        """Add DROP handles for flows that appeared during a blocker refresh."""
        with self._lifecycle_lock:
            if self._stopped:
                return 0
            handles, keys = self._open_network_handles(flows)
            self.network_handles.extend(handles)
            self._flow_keys.update(keys)
            return len(keys)

    def start(self, *, existing_flows: Optional[list[dict[str, Any]]] = None) -> None:
        from pydivert import windivert_dll
        from pydivert.consts import Flag, Layer

        socket_filter = _socket_filter(self.pid, self.addresses)
        flows = (
            _matching_connections(self.pid, self.addresses)
            if existing_flows is None
            else self._unique_flows(existing_flows)
        )
        socket_handle = None
        network_handles: list[Any] = []
        flow_keys: set[tuple[Any, ...]] = set()
        try:
            socket_handle = windivert_dll.WinDivertOpen(
                socket_filter.encode("ascii"), int(Layer.SOCKET), 1000, int(Flag.RECV_ONLY)
            )
            network_handles, flow_keys = self._open_network_handles(flows)
        except Exception:
            if socket_handle is not None:
                try:
                    windivert_dll.WinDivertClose(socket_handle)
                except Exception:
                    pass
            for handle in network_handles:
                try:
                    windivert_dll.WinDivertClose(handle)
                except Exception:
                    pass
            raise

        receive_thread = threading.Thread(
            target=self._receive_loop,
            name=f"MerchantFix-PID-{self.pid}",
            daemon=True,
        )
        with self._lifecycle_lock:
            self.socket_handle = socket_handle
            self.network_handles = network_handles
            self._flow_keys = flow_keys
            self._receive_thread = receive_thread
            self.stop_event.clear()
        try:
            receive_thread.start()
        except Exception:
            with self._lifecycle_lock:
                self.socket_handle = None
                self.network_handles = []
                self._flow_keys.clear()
                self._receive_thread = None
            try:
                windivert_dll.WinDivertClose(socket_handle)
            except Exception:
                pass
            for network_handle in network_handles:
                try:
                    windivert_dll.WinDivertClose(network_handle)
                except Exception:
                    pass
            raise
        if self._activity_logger is not None:
            self._activity_logger.record("PID domain blocks activated", pid=self.pid)
        else:
            _write_log(
                f"Smart domain block active for PID {self.pid}; "
                f"{len(self.addresses)} resolved address(es), "
                f"{len(flows)} existing flow(s)."
            )

    def _receive_loop(self) -> None:
        from pydivert import windivert_dll

        handle = self.socket_handle
        while handle is not None and not self.stop_event.is_set():
            address = windivert_dll.WinDivertAddress()
            try:
                windivert_dll.WinDivertRecv(handle, None, 0, None, ctypes.byref(address))
            except Exception as exc:
                if not self.stop_event.is_set():
                    _write_log(f"Smart blocker receive failed for PID {self.pid}: {exc}", level="ERROR")
                return
            try:
                socket_event = address.Socket
                self._record_blocked_connection(int(socket_event.RemotePort))
            except Exception:
                pass

    def stop(self, *, report_hits: bool = True) -> None:
        with self._lifecycle_lock:
            with self._stats_lock:
                if self._stopped:
                    return
                self._stopped = True
            self.stop_event.set()
            handle = self.socket_handle
            receive_thread = self._receive_thread
            network_handles = list(self.network_handles)
            self.socket_handle = None
            self.network_handles = []
            self._flow_keys.clear()

        # An unstarted blocker owns no native resources, so stopping it should
        # not require pydivert to be importable (useful for partial startup and
        # for reporting-only cleanup paths).
        windivert_dll = None
        if handle is not None or network_handles:
            from pydivert import windivert_dll

        if handle is not None and hasattr(windivert_dll, "WinDivertShutdown"):
            try:
                # WINDIVERT_SHUTDOWN_RECV = 0x1. This releases a synchronous
                # WinDivertRecv before its handle is closed.
                windivert_dll.WinDivertShutdown(handle, 0x1)
            except Exception:
                pass

        if (
            receive_thread is not None
            and receive_thread is not threading.current_thread()
        ):
            receive_thread.join(timeout=2.0)

        receiver_alive = bool(receive_thread is not None and receive_thread.is_alive())
        if receiver_alive:
            _write_log(
                f"Smart blocker receiver for PID {self.pid} did not stop within 2 seconds; "
                "moving handle cleanup off the helper control loop.",
                level="WARNING",
            )

            def _reap_receiver() -> None:
                try:
                    receive_thread.join(timeout=5.0)
                except Exception:
                    pass
                if handle is not None:
                    try:
                        windivert_dll.WinDivertClose(handle)
                    except Exception:
                        pass

            threading.Thread(
                target=_reap_receiver,
                name=f"MerchantFix-Reaper-{self.pid}",
                daemon=True,
            ).start()
        elif handle is not None:
            try:
                windivert_dll.WinDivertClose(handle)
            except Exception:
                pass
            self._receive_thread = None

        for network_handle in network_handles:
            try:
                windivert_dll.WinDivertClose(network_handle)
            except Exception:
                pass
        if report_hits:
            blocked = self.blocked_connection_count
            if self._activity_logger is not None:
                self._activity_logger.record("PID domain blocks stopped", pid=self.pid)
            else:
                _write_log(
                    f"Smart domain block stopped for PID {self.pid} after blocking "
                    f"{blocked} connection attempt{'s' if blocked != 1 else ''}."
                )


class _StorageAclGuard:
    def __init__(
        self,
        recovery_path: Path,
        *,
        activity_logger: Optional[_SmartActivityLogger] = None,
    ) -> None:
        self.path: Optional[Path] = None
        self.original_sddl: Optional[str] = None
        # Keep the storage tree and its sibling SQLite files separate. A file
        # beneath rbx-storage can legitimately have the same name as one of
        # the sibling databases, so a single recovery mapping is ambiguous.
        self.original_tree_sddl: dict[str, str] = {}
        self.original_database_sddl: dict[str, str] = {}
        self.created_placeholders: dict[str, tuple[int, int, int]] = {}
        self.blocked_user_sid: Optional[str] = None
        self.holding_name: Optional[str] = None
        self.placeholder_name: Optional[str] = None
        self.original_storage_identity: Optional[tuple[int, int, int]] = None
        self.storage_placeholder_identity: Optional[tuple[int, int, int]] = None
        self.storage_placeholder_sddl: Optional[str] = None
        self.launch_deadlines: dict[str, Optional[float]] = {}
        self.release_at: Optional[float] = None
        self.recovery_path = Path(recovery_path)
        self._activity_logger = activity_logger

    @property
    def active(self) -> bool:
        return self.path is not None and bool(self.original_database_sddl)

    @staticmethod
    def _normalize_launch_id(raw_launch_id: object) -> str:
        launch_id = str(raw_launch_id or "").strip()
        if not launch_id or len(launch_id) > 256:
            raise ValueError("The Smart pre-launch identifier is invalid.")
        return launch_id

    def _recalculate_release_at(self) -> None:
        if not self.launch_deadlines or any(
            deadline is None for deadline in self.launch_deadlines.values()
        ):
            self.release_at = None
            return
        self.release_at = max(
            float(deadline)
            for deadline in self.launch_deadlines.values()
            if deadline is not None
        )

    def _save_database_recovery_record(
        self,
        path: Path,
        database_sddl: dict[str, str],
        created_placeholders: dict[str, tuple[int, int, int]],
    ) -> None:
        self.recovery_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.recovery_path.with_name(
            f"{self.recovery_path.name}.{os.getpid()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": 8,
                        "mode": "database_files_only",
                        "path": str(path),
                        "entries": database_sddl,
                        "created_placeholders": {
                            name: self._identity_to_record(identity)
                            for name, identity in sorted(created_placeholders.items())
                        },
                        "owner_pid": os.getpid(),
                        "owner_creation_time": self._current_process_creation_time(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self.recovery_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass

    def _clear_recovery_record(self) -> None:
        try:
            self.recovery_path.unlink(missing_ok=True)
        except Exception as exc:
            raise RuntimeError(
                f"Could not remove the ACL recovery record {self.recovery_path}: {exc}"
            ) from exc

    @staticmethod
    def _restore_sddl(path: Path, sddl: str) -> None:
        import win32security

        security = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            sddl, win32security.SDDL_REVISION_1
        )
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, security
        )

    @staticmethod
    def _iter_acl_paths(root: Path):
        """Yield the root and existing descendants without following links."""
        yield root
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as scanner:
                    entries = list(scanner)
            except FileNotFoundError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    child = Path(entry.path)
                    yield child
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
                except FileNotFoundError:
                    continue

    @classmethod
    def _capture_acl_tree(cls, root: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for candidate in cls._iter_acl_paths(root):
            relative = "." if candidate == root else str(candidate.relative_to(root))
            result[relative] = cls._capture_acl(candidate)
        return result

    @staticmethod
    def _capture_acl(path: Path) -> str:
        import win32security

        security = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        return str(
            win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                security,
                win32security.SDDL_REVISION_1,
                win32security.DACL_SECURITY_INFORMATION,
            )
        )

    @staticmethod
    def _deny_permissions(
        path: Path,
        sid: Any,
        permissions: int,
        *,
        inherit: bool,
        base_sddl: Optional[str] = None,
    ) -> None:
        """Apply a canonical deny ACE, optionally from a captured clean DACL."""
        import win32security

        if base_sddl is None:
            security = win32security.GetFileSecurity(
                str(path), win32security.DACL_SECURITY_INFORMATION
            )
        else:
            security = (
                win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                    str(base_sddl), win32security.SDDL_REVISION_1
                )
            )
        dacl = security.GetSecurityDescriptorDacl()
        entries = [
            {
                "Trustee": {
                    "MultipleTrustee": None,
                    "MultipleTrusteeOperation": 0,
                    "TrusteeForm": win32security.TRUSTEE_IS_SID,
                    "TrusteeType": win32security.TRUSTEE_IS_USER,
                    "Identifier": sid,
                },
                "Inheritance": (
                    win32security.SUB_CONTAINERS_AND_OBJECTS_INHERIT
                    if inherit
                    else win32security.NO_INHERITANCE
                ),
                "AccessMode": win32security.DENY_ACCESS,
                "AccessPermissions": int(permissions),
            }
        ]
        if dacl is None:
            # A null DACL grants everyone full access. Preserve that behavior
            # for rights outside this targeted read/write deny.
            dacl = win32security.ACL()
            entries.append(
                {
                    "Trustee": {
                        "MultipleTrustee": None,
                        "MultipleTrusteeOperation": 0,
                        "TrusteeForm": win32security.TRUSTEE_IS_SID,
                        "TrusteeType": win32security.TRUSTEE_IS_WELL_KNOWN_GROUP,
                        "Identifier": win32security.CreateWellKnownSid(
                            win32security.WinWorldSid, None
                        ),
                    },
                    "Inheritance": (
                        win32security.SUB_CONTAINERS_AND_OBJECTS_INHERIT
                        if inherit
                        else win32security.NO_INHERITANCE
                    ),
                    "AccessMode": win32security.GRANT_ACCESS,
                    "AccessPermissions": ntsecuritycon.FILE_ALL_ACCESS,
                }
            )
        dacl.SetEntriesInAcl(entries)
        if base_sddl is not None:
            # The active read deny prevents SetNamedSecurityInfo from reopening
            # the object for READ_CONTROL. SetFileSecurity can replace the DACL
            # directly from our captured clean descriptor without that read.
            security.SetSecurityDescriptorDacl(1, dacl, 0)
            win32security.SetFileSecurity(
                str(path), win32security.DACL_SECURITY_INFORMATION, security
            )
            return
        # SetNamedSecurityInfo, unlike SetFileSecurity, propagates a new
        # inheritable ACE to existing descendants of a directory.
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )

    @classmethod
    def _deny_read_write(cls, path: Path, sid: Any, *, inherit: bool) -> None:
        """Add a canonical R/W deny ACE without starting icacls.exe."""
        import ntsecuritycon

        cls._deny_permissions(
            path,
            sid,
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE,
            inherit=inherit,
        )

    @classmethod
    def _prepare_placeholder_for_rename(
        cls, path: Path, sid: Any, original_sddl: str
    ) -> None:
        """Keep new entries blocked while allowing the placeholder rename."""
        import ntsecuritycon

        cls._deny_permissions(
            path,
            sid,
            ntsecuritycon.FILE_ADD_FILE | ntsecuritycon.FILE_ADD_SUBDIRECTORY,
            inherit=False,
            base_sddl=original_sddl,
        )

    @classmethod
    def _restore_acl_tree(cls, root: Path, entries: dict[str, str]) -> int:
        validated: list[tuple[str, str]] = []
        for relative, sddl in entries.items():
            relative_text = str(relative)
            relative_path = Path(relative_text)
            if (
                relative_text != "."
                and (relative_path.is_absolute() or ".." in relative_path.parts)
            ):
                raise ValueError(f"Unsafe ACL recovery path: {relative}")
            if not str(sddl):
                raise ValueError(f"ACL recovery path has no descriptor: {relative}")
            validated.append((relative_text, str(sddl)))
        restored = 0
        ordered = sorted(
            validated,
            key=lambda item: (0 if item[0] == "." else len(Path(item[0]).parts), item[0]),
        )
        for relative, sddl in ordered:
            relative_path = Path(str(relative))
            candidate = root if str(relative) == "." else root / relative_path
            if not candidate.exists() and not candidate.is_symlink():
                continue
            try:
                cls._restore_sddl(candidate, str(sddl))
            except Exception as exc:
                # A system process can evict a cache entry during stale
                # recovery. A vanished path no longer has an ACL to repair.
                if cls._is_missing_path_error(exc):
                    continue
                raise
            restored += 1
        return restored

    @staticmethod
    def _database_targets(storage: Path) -> dict[str, Path]:
        return {name: storage.parent / name for name in STORAGE_DATABASE_NAMES}

    @staticmethod
    def _current_process_creation_time() -> float:
        try:
            return float(psutil.Process(os.getpid()).create_time())
        except Exception:
            return 0.0

    @staticmethod
    def _recovery_owner_is_live(record: dict[str, Any]) -> bool:
        try:
            owner_pid = int(record.get("owner_pid") or 0)
            expected_creation = float(record.get("owner_creation_time") or 0.0)
            if owner_pid <= 0 or expected_creation <= 0.0:
                return False
        except (TypeError, ValueError):
            return False
        try:
            actual_creation = float(psutil.Process(owner_pid).create_time())
            return abs(actual_creation - expected_creation) < 0.01
        except psutil.NoSuchProcess:
            return False
        except Exception:
            # Failing closed here is safer than restoring ACLs that may still
            # be owned by a live elevated helper. A later retry can establish
            # conclusively that the owner exited.
            return True

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int]:
        if os.name == "nt":
            try:
                import win32con
                import win32file

                shares = (
                    int(win32con.FILE_SHARE_READ)
                    | int(win32con.FILE_SHARE_WRITE)
                    | int(win32con.FILE_SHARE_DELETE)
                )
                flags = int(win32con.FILE_ATTRIBUTE_NORMAL) | int(
                    getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0)
                )
                flags |= int(getattr(win32con, "FILE_FLAG_BACKUP_SEMANTICS", 0))
                handle = win32file.CreateFile(
                    str(path),
                    0,
                    shares,
                    None,
                    int(win32con.OPEN_EXISTING),
                    flags,
                    None,
                )
                try:
                    information = win32file.GetFileInformationByHandle(handle)
                finally:
                    handle.Close()
                volume = int(information[4])
                file_index = (int(information[8]) << 32) | int(information[9])
                if volume != 0 or file_index != 0:
                    return (volume, file_index, 0)
            except Exception:
                pass
        stat = path.stat()
        identity = (
            int(stat.st_dev),
            int(stat.st_ino),
            int(getattr(stat, "st_birthtime_ns", 0)),
        )
        if identity[0] == 0 and identity[1] == 0:
            raise RuntimeError(f"Windows did not provide a stable file identity for {path}.")
        return identity

    @staticmethod
    def _identity_to_record(identity: tuple[int, int, int]) -> dict[str, int]:
        return {
            "device": int(identity[0]),
            "inode": int(identity[1]),
            "birth_ns": int(identity[2]),
        }

    @staticmethod
    def _identity_from_record(
        value: object, *, description: str
    ) -> tuple[int, int, int]:
        if not isinstance(value, dict):
            raise ValueError(f"The {description} identity is missing.")
        try:
            identity = (
                int(value.get("device")),
                int(value.get("inode")),
                int(value.get("birth_ns")),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"The {description} identity is invalid.") from exc
        if identity[0] == 0 and identity[1] == 0:
            raise ValueError(f"The {description} identity is invalid.")
        return identity

    @staticmethod
    def _validate_swap_name(name: object, pattern: re.Pattern[str]) -> str:
        normalized = str(name or "")
        if pattern.fullmatch(normalized) is None or Path(normalized).name != normalized:
            raise ValueError("The storage swap recovery name is invalid.")
        return normalized

    @classmethod
    def _remove_storage_placeholder(
        cls,
        path: Path,
        expected_identity: tuple[int, int, int],
        original_sddl: str,
        *,
        remove_populated: bool = False,
        preserve_replaced: bool = False,
    ) -> int:
        if not path.exists() and not path.is_symlink():
            return 0
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"Expected {path} to remain a placeholder directory.")

        # A transitioned placeholder permits identity reads but still blocks
        # new entries. Check before changing its DACL whenever possible so a
        # replacement is never modified or removed.
        try:
            current_identity = cls._file_identity(path)
        except Exception:
            cls._restore_sddl(path, original_sddl)
            current_identity = cls._file_identity(path)
        if current_identity != expected_identity:
            if preserve_replaced:
                _write_log(
                    "Preserved a replacement found at the Smart storage placeholder "
                    f"path after restoring the original rbx-storage directory: {path}",
                    level="WARNING",
                )
                return 0
            raise RuntimeError(f"Refusing to remove a replaced storage placeholder: {path}")
        cls._restore_sddl(path, original_sddl)
        try:
            with os.scandir(path) as scanner:
                if next(scanner, None) is not None:
                    if remove_populated:
                        _write_log(
                            "Removing a populated Smart storage placeholder after "
                            f"restoring the original rbx-storage directory: {path}",
                            level="WARNING",
                        )
                        shutil.rmtree(path)
                        return 1
                    raise RuntimeError(
                        f"Refusing to remove a non-empty storage placeholder: {path}"
                    )
        except FileNotFoundError:
            return 0
        path.rmdir()
        return 1

    @classmethod
    def _restore_storage_swap(
        cls,
        storage: Path,
        holding_name: object,
        placeholder_name: object,
        original_identity: tuple[int, int, int],
        placeholder_identity: tuple[int, int, int],
        placeholder_sddl: str,
        user_sid: str,
    ) -> tuple[int, int]:
        import win32security

        holding_name = cls._validate_swap_name(
            holding_name, _STORAGE_HOLDING_NAME_PATTERN
        )
        placeholder_name = cls._validate_swap_name(
            placeholder_name, _STORAGE_PLACEHOLDER_NAME_PATTERN
        )
        holding = storage.parent / holding_name
        staged_placeholder = storage.parent / placeholder_name
        holding_exists = holding.exists() or holding.is_symlink()
        storage_exists = storage.exists() or storage.is_symlink()
        staged_exists = staged_placeholder.exists() or staged_placeholder.is_symlink()
        removed = 0

        if holding_exists:
            if holding.is_symlink() or not holding.is_dir():
                raise RuntimeError(f"Expected {holding} to remain the held storage directory.")
            if cls._file_identity(holding) != original_identity:
                raise RuntimeError(f"The held storage directory was replaced: {holding}")
            if storage_exists and staged_exists:
                if storage.is_symlink() or not storage.is_dir():
                    raise RuntimeError(
                        f"Expected {storage} to remain a storage directory."
                    )
                if staged_placeholder.is_symlink() or not staged_placeholder.is_dir():
                    raise RuntimeError(
                        f"Expected {staged_placeholder} to remain a storage directory."
                    )
                try:
                    visible_identity = cls._file_identity(storage)
                except Exception:
                    visible_identity = None
                try:
                    staged_identity = cls._file_identity(staged_placeholder)
                except Exception:
                    staged_identity = None
                if staged_identity == placeholder_identity:
                    # A previous recovery moved the recorded placeholder aside,
                    # then Roblox recreated the canonical storage directory before
                    # the held original could be restored. Remove only the exact
                    # recorded placeholder. The replacement at the canonical path
                    # is moved aside and preserved by the normal flow below.
                    removed += cls._remove_storage_placeholder(
                        staged_placeholder,
                        placeholder_identity,
                        placeholder_sddl,
                        remove_populated=True,
                    )
                    staged_exists = False
                elif visible_identity == placeholder_identity:
                    # The canonical path is still the exact recorded placeholder;
                    # the occupied staging path is a replacement and must survive.
                    removed += cls._remove_storage_placeholder(
                        storage,
                        placeholder_identity,
                        placeholder_sddl,
                        remove_populated=True,
                    )
                    storage_exists = False
                else:
                    raise RuntimeError(
                        "Both storage recovery locations contain unrecognized "
                        "directories; refusing recovery."
                    )
            if storage_exists:
                if storage.is_symlink() or not storage.is_dir():
                    raise RuntimeError(
                        f"Expected {storage} to remain a placeholder directory."
                    )
                try:
                    visible_identity = cls._file_identity(storage)
                except Exception:
                    visible_identity = None
                if visible_identity is None or visible_identity == placeholder_identity:
                    sid = win32security.ConvertStringSidToSid(str(user_sid or ""))
                    # Replace the R/W deny with a narrow create-entry deny. It
                    # remains impossible for Roblox to add files, while Windows
                    # can rename the directory out of the canonical path.
                    cls._prepare_placeholder_for_rename(
                        storage, sid, placeholder_sddl
                    )
                storage.rename(staged_placeholder)
            holding.rename(storage)
            if staged_placeholder.exists() or staged_placeholder.is_symlink():
                removed += cls._remove_storage_placeholder(
                    staged_placeholder,
                    placeholder_identity,
                    placeholder_sddl,
                    remove_populated=True,
                    preserve_replaced=True,
                )
            return 1, removed

        if not storage_exists:
            raise RuntimeError("The original and held storage directories are both missing.")
        if storage.is_symlink() or not storage.is_dir():
            raise RuntimeError(f"Expected {storage} to remain a storage directory.")
        try:
            storage_identity = cls._file_identity(storage)
        except Exception:
            # A denied placeholder cannot be identity-checked normally. Make
            # it inspectable without deleting it; the identity check below
            # still prevents a replacement from being removed.
            cls._restore_sddl(storage, placeholder_sddl)
            storage_identity = cls._file_identity(storage)
        if storage_identity != original_identity:
            if storage_identity == placeholder_identity:
                raise RuntimeError(
                    "The storage placeholder remains, but the held original is missing."
                )
            raise RuntimeError(f"The storage directory was unexpectedly replaced: {storage}")
        if staged_exists:
            removed += cls._remove_storage_placeholder(
                staged_placeholder,
                placeholder_identity,
                placeholder_sddl,
                remove_populated=True,
                preserve_replaced=True,
            )
        return 0, removed

    @classmethod
    def _prepare_database_targets(
        cls, storage: Path
    ) -> tuple[dict[str, str], dict[str, tuple[int, int, int]]]:
        entries: dict[str, str] = {}
        created: dict[str, tuple[int, int, int]] = {}
        try:
            for name, target in cls._database_targets(storage).items():
                for attempt in range(3):
                    if target.is_symlink() or (
                        target.exists() and not target.is_file()
                    ):
                        raise RuntimeError(
                            f"Expected {target} to be a regular database file."
                        )
                    try:
                        target.touch(exist_ok=False)
                        created[name] = cls._file_identity(target)
                    except FileExistsError:
                        created.pop(name, None)
                    try:
                        entries[name] = cls._capture_acl(target)
                        break
                    except Exception as exc:
                        if attempt >= 2 or not cls._is_missing_path_error(exc):
                            raise
                        # A concurrently running Roblox process can remove a
                        # WAL/SHM file between creation and ACL capture.
                        created.pop(name, None)
                else:
                    raise RuntimeError(f"Could not stabilize database file {target}.")
            return entries, created
        except Exception:
            cls._remove_empty_placeholders(storage, created)
            raise

    @staticmethod
    def _remove_empty_placeholders(
        storage: Path,
        identities: dict[str, Optional[tuple[int, int, int]]],
    ) -> int:
        removed = 0
        for name, expected_identity in sorted(identities.items()):
            if name not in STORAGE_DATABASE_NAMES:
                continue
            target = storage.parent / name
            try:
                if target.is_symlink() or not target.is_file():
                    continue
                current_identity = _StorageAclGuard._file_identity(target)
                if expected_identity is None or current_identity != expected_identity:
                    _write_log(
                        f"Kept replaced Smart-mode database placeholder {target}.",
                        level="WARNING",
                    )
                    continue
                if target.stat().st_size != 0:
                    _write_log(
                        f"Kept non-empty Smart-mode database placeholder {target}.",
                        level="WARNING",
                    )
                    continue
                target.unlink()
                removed += 1
            except FileNotFoundError:
                continue
        return removed

    @staticmethod
    def _created_placeholders_from_record(
        record: dict[str, Any],
    ) -> dict[str, Optional[tuple[int, int, int]]]:
        raw_created = record.get("created_placeholders")
        created: dict[str, Optional[tuple[int, int, int]]] = {}
        if isinstance(raw_created, dict):
            for raw_name, raw_identity in raw_created.items():
                name = str(raw_name)
                if isinstance(raw_identity, dict):
                    try:
                        identity = (
                            int(raw_identity.get("device")),
                            int(raw_identity.get("inode")),
                            int(raw_identity.get("birth_ns")),
                        )
                    except (TypeError, ValueError):
                        identity = None
                else:
                    identity = None
                created[name] = identity
        elif isinstance(raw_created, list):
            # Version 4 had names but no stable identities. Restore their ACLs,
            # but keep the files rather than risk deleting replacements.
            created = {str(name): None for name in raw_created}
        return created

    @classmethod
    def _restore_database_targets(
        cls,
        storage: Path,
        entries: dict[str, str],
        created_placeholders: dict[str, Optional[tuple[int, int, int]]],
    ) -> tuple[int, int]:
        unexpected = set(entries).difference(STORAGE_DATABASE_NAMES)
        unexpected.update(created_placeholders.keys() - set(STORAGE_DATABASE_NAMES))
        if unexpected:
            raise ValueError(
                f"Unsafe database ACL recovery target(s): {', '.join(sorted(unexpected))}"
            )
        missing = set(STORAGE_DATABASE_NAMES).difference(entries)
        if missing:
            raise ValueError(
                f"Database ACL recovery is missing target(s): {', '.join(sorted(missing))}"
            )
        restored = 0
        for name in STORAGE_DATABASE_NAMES:
            sddl = str(entries.get(name) or "")
            target = storage.parent / name
            if not sddl or (not target.exists() and not target.is_symlink()):
                continue
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(
                    f"Expected {target} to remain a regular database file."
                )
            # The active deny prevents even a metadata-only identity query for
            # the blocked user. Restore access first, then use the recorded
            # identity to decide whether an empty placeholder is safe to delete.
            cls._restore_sddl(target, sddl)
            restored += 1
        removed = cls._remove_empty_placeholders(storage, created_placeholders)
        return restored, removed

    @classmethod
    def _restore_swap_targets(
        cls,
        storage: Path,
        database_entries: dict[str, str],
        created_placeholders: dict[str, Optional[tuple[int, int, int]]],
        holding_name: object,
        placeholder_name: object,
        original_identity: tuple[int, int, int],
        placeholder_identity: tuple[int, int, int],
        placeholder_sddl: str,
        user_sid: str,
    ) -> tuple[int, int, int]:
        unexpected = set(database_entries).difference(STORAGE_DATABASE_NAMES)
        unexpected.update(created_placeholders.keys() - set(STORAGE_DATABASE_NAMES))
        if unexpected:
            raise ValueError(
                f"Unsafe database ACL recovery target(s): {', '.join(sorted(unexpected))}"
            )
        missing = set(STORAGE_DATABASE_NAMES).difference(database_entries)
        if missing:
            raise ValueError(
                f"Database ACL recovery is missing target(s): {', '.join(sorted(missing))}"
            )
        if not str(placeholder_sddl):
            raise ValueError("The storage placeholder ACL is missing.")
        if not str(user_sid):
            raise ValueError("The storage swap recovery record has no user SID.")

        storage_restored, storage_removed = cls._restore_storage_swap(
            storage,
            holding_name,
            placeholder_name,
            original_identity,
            placeholder_identity,
            str(placeholder_sddl),
            str(user_sid),
        )
        database_restored, database_removed = cls._restore_database_targets(
            storage, database_entries, created_placeholders
        )
        return (
            storage_restored + database_restored,
            storage_removed,
            database_removed,
        )

    @classmethod
    def _restore_combined_targets(
        cls,
        storage: Path,
        storage_entries: dict[str, str],
        database_entries: dict[str, str],
        created_placeholders: dict[str, Optional[tuple[int, int, int]]],
        user_sid: str,
    ) -> tuple[int, int, int]:
        root_sddl = str(storage_entries.get(".") or "")
        if not root_sddl:
            raise ValueError(
                "The storage ACL recovery record is missing the root descriptor."
            )
        unexpected = set(database_entries).difference(STORAGE_DATABASE_NAMES)
        unexpected.update(created_placeholders.keys() - set(STORAGE_DATABASE_NAMES))
        if unexpected:
            raise ValueError(
                f"Unsafe database ACL recovery target(s): {', '.join(sorted(unexpected))}"
            )
        missing = set(STORAGE_DATABASE_NAMES).difference(database_entries)
        if missing:
            raise ValueError(
                f"Database ACL recovery is missing target(s): {', '.join(sorted(missing))}"
            )
        import win32security

        sid = win32security.ConvertStringSidToSid(str(user_sid or ""))
        sid_text = str(win32security.ConvertSidToStringSid(sid))

        # Restore the parent first. Removing its inheritable deny makes every
        # child reachable again; then each captured descendant gets its exact
        # pre-block ACL back. This ordering is what prevents inherited deny
        # entries from being stranded on child files.
        restored = cls._restore_acl_tree(storage, storage_entries)

        # Files created by a service or another security principal while the
        # block was active are not in the snapshot. Normally Windows removes
        # their inherited deny when the parent is restored, but explicitly
        # strip only Merchant Fix's inherited R/W deny as a recovery safeguard.
        repaired = cls.repair_orphaned_denies(storage, sid_text)
        database_restored, removed = cls._restore_database_targets(
            storage, database_entries, created_placeholders
        )
        return restored + database_restored, repaired, removed

    @staticmethod
    def _strip_legacy_deny_sddl(sddl: str, sid_text: str) -> tuple[str, int]:
        legacy_pattern = re.compile(
            rf"\(D;(?=[^;]*ID)[^;]*;"
            rf"(?:0x12019f|GRGW);;;{re.escape(sid_text)}\)",
            re.IGNORECASE,
        )
        return legacy_pattern.subn("", str(sddl))

    @staticmethod
    def _read_sddl_for_repair(path: Path) -> str:
        import win32security

        try:
            security = win32security.GetFileSecurity(
                str(path), win32security.DACL_SECURITY_INFORMATION
            )
        except Exception as original_error:
            # The old inherited deny also blocks ordinary READ_CONTROL. An
            # elevated helper can use backup semantics to inspect that ACL
            # without taking ownership or resetting unrelated permissions.
            try:
                import ntsecuritycon
                import win32api
                import win32con
                import win32file

                token = win32security.OpenProcessToken(
                    win32api.GetCurrentProcess(),
                    win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY,
                )
                privileges = [
                    (
                        win32security.LookupPrivilegeValue(None, privilege),
                        win32security.SE_PRIVILEGE_ENABLED,
                    )
                    for privilege in ("SeBackupPrivilege", "SeRestorePrivilege")
                ]
                win32security.AdjustTokenPrivileges(token, False, privileges)
                flags = int(win32con.FILE_FLAG_BACKUP_SEMANTICS)
                flags |= int(getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0))
                shares = (
                    int(win32con.FILE_SHARE_READ)
                    | int(win32con.FILE_SHARE_WRITE)
                    | int(win32con.FILE_SHARE_DELETE)
                )
                handle = win32file.CreateFile(
                    str(path),
                    int(ntsecuritycon.READ_CONTROL),
                    shares,
                    None,
                    int(win32con.OPEN_EXISTING),
                    flags,
                    None,
                )
                try:
                    security = win32security.GetSecurityInfo(
                        handle,
                        win32security.SE_FILE_OBJECT,
                        win32security.DACL_SECURITY_INFORMATION,
                    )
                finally:
                    handle.Close()
            except Exception as backup_error:
                raise RuntimeError(
                    f"Could not inspect stale ACL {path}: {backup_error}"
                ) from original_error
        return str(
            win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                security,
                win32security.SDDL_REVISION_1,
                win32security.DACL_SECURITY_INFORMATION,
            )
        )

    @staticmethod
    def _is_missing_path_error(error: BaseException) -> bool:
        current: Optional[BaseException] = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, FileNotFoundError):
                return True
            winerror = getattr(current, "winerror", None)
            if winerror in {2, 3}:
                return True
            current = current.__cause__ or current.__context__
        return False

    @classmethod
    def repair_orphaned_denies(cls, root: Path, user_sid: str) -> int:
        """Remove only the inherited deny ACE produced by older Smart builds."""
        if not root.is_dir():
            return 0
        import win32security

        sid = win32security.ConvertStringSidToSid(str(user_sid or ""))
        sid_text = str(win32security.ConvertSidToStringSid(sid))
        repaired = 0
        for candidate in cls._iter_acl_paths(root):
            if candidate == root:
                continue
            try:
                sddl = cls._read_sddl_for_repair(candidate)
            except Exception as exc:
                # Roblox may evict storage files between scandir and the ACL
                # read. A vanished candidate needs no legacy-deny repair.
                if cls._is_missing_path_error(exc):
                    continue
                raise
            updated, removed = cls._strip_legacy_deny_sddl(sddl, sid_text)
            if not removed:
                continue
            cls._restore_sddl(candidate, updated)
            repaired += 1
        if repaired:
            _write_log(
                f"Repaired {repaired} orphaned Smart-mode deny ACL(s) beneath {root}."
            )
        return repaired

    def recover_stale_acl(self) -> None:
        if not self.recovery_path.is_file():
            return
        try:
            record = json.loads(self.recovery_path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("The ACL recovery record is not a JSON object.")
            path = _validate_helper_storage_dir(Path(str(record.get("path") or "")))
            if self._recovery_owner_is_live(record):
                _write_log(
                    "Left the Smart storage ACL recovery record with its live helper "
                    f"owner (PID {int(record.get('owner_pid') or 0)})."
                )
                return
            raw_entries = record.get("entries")
            mode = str(record.get("mode") or "")
            if mode == "storage_directory_swap_and_database_files":
                raw_database_entries = record.get("database_entries")
                if not isinstance(raw_database_entries, dict) or not raw_database_entries:
                    raise ValueError(
                        "The database ACL recovery record has no security descriptors."
                    )
                database_entries = {
                    str(name): str(entry_sddl)
                    for name, entry_sddl in raw_database_entries.items()
                    if str(entry_sddl)
                }
                created = self._created_placeholders_from_record(record)
                original_identity = self._identity_from_record(
                    record.get("original_storage_identity"),
                    description="original storage",
                )
                placeholder_identity = self._identity_from_record(
                    record.get("storage_placeholder_identity"),
                    description="storage placeholder",
                )
                restored, storage_removed, database_removed = self._restore_swap_targets(
                    path,
                    database_entries,
                    created,
                    record.get("holding_name"),
                    record.get("placeholder_name"),
                    original_identity,
                    placeholder_identity,
                    str(record.get("storage_placeholder_sddl") or ""),
                    str(record.get("user_sid") or ""),
                )
                detail = (
                    f"Recovered {restored} stale Smart storage/database ACL(s), "
                    f"removed {storage_removed} empty storage placeholder(s), and "
                    f"removed {database_removed} empty database placeholder(s) for "
                    f"{path.parent}."
                )
            elif mode == "storage_tree_and_database_files":
                raw_storage_entries = record.get("storage_entries")
                raw_database_entries = record.get("database_entries")
                if not isinstance(raw_storage_entries, dict) or not raw_storage_entries:
                    raise ValueError(
                        "The storage ACL recovery record has no security descriptors."
                    )
                if not isinstance(raw_database_entries, dict) or not raw_database_entries:
                    raise ValueError(
                        "The database ACL recovery record has no security descriptors."
                    )
                storage_entries = {
                    str(relative): str(entry_sddl)
                    for relative, entry_sddl in raw_storage_entries.items()
                    if str(entry_sddl)
                }
                database_entries = {
                    str(name): str(entry_sddl)
                    for name, entry_sddl in raw_database_entries.items()
                    if str(entry_sddl)
                }
                created = self._created_placeholders_from_record(record)
                user_sid = str(record.get("user_sid") or "")
                if not user_sid:
                    raise ValueError("The storage ACL recovery record has no user SID.")
                restored, repaired, removed = self._restore_combined_targets(
                    path,
                    storage_entries,
                    database_entries,
                    created,
                    user_sid,
                )
                detail = (
                    f"Recovered {restored} stale Smart storage/database ACL(s), "
                    f"repaired {repaired} inherited deny ACL(s), and removed "
                    f"{removed} empty placeholder(s) for {path.parent}."
                )
            elif mode == "database_files_only":
                if not isinstance(raw_entries, dict) or not raw_entries:
                    raise ValueError(
                        "The database ACL recovery record has no security descriptors."
                    )
                entries = {
                    str(name): str(entry_sddl)
                    for name, entry_sddl in raw_entries.items()
                    if str(entry_sddl)
                }
                created = self._created_placeholders_from_record(record)
                restored, removed = self._restore_database_targets(
                    path, entries, created
                )
                detail = (
                    f"Recovered {restored} stale Smart database ACL(s) and removed "
                    f"{removed} empty placeholder(s) for {path.parent}."
                )
            else:
                # Retain recovery support for directory ACL records written by
                # versions 1-3 of Merchant Fix.
                sddl = str(record.get("sddl") or "")
                if not sddl:
                    raise ValueError(
                        "The legacy ACL recovery record has no security descriptor."
                    )
                if isinstance(raw_entries, dict) and raw_entries:
                    entries = {
                        str(relative): str(entry_sddl)
                        for relative, entry_sddl in raw_entries.items()
                        if str(entry_sddl)
                    }
                else:
                    entries = {".": sddl}
                restored = self._restore_acl_tree(path, entries)
                detail = f"Recovered {restored} stale rbx-storage ACL(s) for {path}."
            self._clear_recovery_record()
            _write_log(detail)
        except Exception as exc:
            _write_log(f"Could not recover the stale rbx-storage ACL: {exc}", level="ERROR")
            raise RuntimeError(
                "A previous Smart-mode storage ACL could not be restored. "
                f"Recovery file: {self.recovery_path}. Error: {exc}"
            ) from exc

    def apply(self, raw_path: str, user_sid: str, launch_id: object = "legacy") -> None:
        if os.name != "nt":
            raise RuntimeError("Storage ACL blocking is only available on Windows.")
        launch_key = self._normalize_launch_id(launch_id)
        path = _validate_helper_storage_dir(Path(raw_path))
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"Roblox storage path is not a regular directory: {path}")

        if not self.active and self.recovery_path.is_file():
            record = json.loads(self.recovery_path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise RuntimeError("The existing ACL recovery record is invalid.")
            if self._recovery_owner_is_live(record):
                raise RuntimeError(
                    "Another live Merchant Fix helper currently owns the Smart "
                    "database-file guard; retry this launch after its pre-launch guard ends."
                )
            self.recover_stale_acl()

        if self.active:
            if os.path.normcase(str(self.path)) != os.path.normcase(str(path)):
                self.restore()
            else:
                self.launch_deadlines[launch_key] = None
                self._recalculate_release_at()
                if self._activity_logger is not None:
                    self._activity_logger.record(
                        "database-file launch guards extended"
                    )
                else:
                    _write_log(
                        "Extended the existing rbx-storage database-file block for "
                        f"launch {launch_key}."
                    )
                return

        import win32security

        # Resolve the SID supplied by the unelevated parent. This matters when
        # a standard user approves UAC with another administrator account.
        sid = win32security.ConvertStringSidToSid(str(user_sid or ""))
        sid_text = str(win32security.ConvertSidToStringSid(sid))
        original_database_sddl, created_placeholders = self._prepare_database_targets(
            path
        )
        try:
            self._save_database_recovery_record(
                path,
                original_database_sddl,
                created_placeholders,
            )
        except Exception:
            self._remove_empty_placeholders(path, created_placeholders)
            raise
        self.path = path
        self.original_sddl = None
        self.original_tree_sddl = {}
        self.original_database_sddl = original_database_sddl
        self.created_placeholders = created_placeholders
        self.blocked_user_sid = sid_text
        self.holding_name = None
        self.placeholder_name = None
        self.original_storage_identity = None
        self.storage_placeholder_identity = None
        self.storage_placeholder_sddl = None
        self.launch_deadlines = {launch_key: None}
        self.release_at = None
        try:
            for target in self._database_targets(path).values():
                self._deny_read_write(target, sid, inherit=False)
        except Exception:
            # An ACL update can fail after earlier targets changed, so restore
            # every captured database descriptor before returning the error.
            self.restore()
            raise
        _write_log(
            "Denied local user read/write access to rbx-storage.db, "
            "rbx-storage.db-wal, and rbx-storage.db-shm before Roblox launch; "
            "the rbx-storage directory was left unchanged."
        )

    def release_after(self, delay: float, launch_id: object = "legacy") -> None:
        if not self.active:
            return
        launch_key = self._normalize_launch_id(launch_id)
        if launch_key not in self.launch_deadlines:
            _write_log(
                f"Ignored an unknown Smart database-file release for launch {launch_key}.",
                level="WARNING",
            )
            return
        normalized_delay = max(0.0, float(delay))
        self.launch_deadlines[launch_key] = time.monotonic() + normalized_delay
        self._recalculate_release_at()
        if self._activity_logger is not None:
            self._activity_logger.record(
                "database-file launch releases scheduled"
            )
        elif self.release_at is None:
            waiting = sum(
                deadline is None for deadline in self.launch_deadlines.values()
            )
            _write_log(
                f"Scheduled database-file release for launch {launch_key}; waiting for "
                f"{waiting} other prepared launch(es)."
            )
        else:
            _write_log(
                "Scheduled rbx-storage database-file restoration after all launches "
                f"complete (no sooner than {normalized_delay:.2f}s)."
            )

    def cancel_launch(self, launch_id: object) -> None:
        if not self.active:
            return
        launch_key = self._normalize_launch_id(launch_id)
        if launch_key not in self.launch_deadlines:
            return
        self.launch_deadlines.pop(launch_key, None)
        if not self.launch_deadlines:
            self.restore()
            return
        self._recalculate_release_at()
        _write_log(
            f"Cancelled Smart database-file preparation for launch {launch_key}; "
            f"{len(self.launch_deadlines)} launch(es) remain protected."
        )

    def abandon_pending_launches(self) -> None:
        if not self.active:
            return
        abandoned = [
            launch_id
            for launch_id, deadline in self.launch_deadlines.items()
            if deadline is None
        ]
        for launch_id in abandoned:
            self.launch_deadlines.pop(launch_id, None)
        if not self.launch_deadlines:
            self.restore()
            return
        self._recalculate_release_at()
        if abandoned:
            _write_log(
                f"Abandoned {len(abandoned)} pending Smart database-file launch(es) "
                "while preserving already-started launch protection."
            )

    def tick(self) -> None:
        if self.active and self.release_at is not None and time.monotonic() >= self.release_at:
            try:
                self.restore()
            except Exception:
                # A short-lived file lock must not kill the elevated helper or
                # allow a later apply to overwrite the recovery information.
                self.release_at = time.monotonic() + 1.0
                _write_log(
                    "Will retry rbx-storage database-file restoration in 1 second.",
                    level="WARNING",
                )

    def restore(self) -> None:
        path = self.path
        sddl = self.original_sddl
        database_sddl = self.original_database_sddl
        created_placeholders = self.created_placeholders
        holding_name = self.holding_name
        placeholder_name = self.placeholder_name
        original_storage_identity = self.original_storage_identity
        storage_placeholder_identity = self.storage_placeholder_identity
        storage_placeholder_sddl = self.storage_placeholder_sddl
        user_sid = self.blocked_user_sid
        if path is None or not database_sddl:
            return
        try:
            combined_guard = any(
                value is not None
                for value in (
                    sddl,
                    holding_name,
                    placeholder_name,
                    original_storage_identity,
                    storage_placeholder_identity,
                    storage_placeholder_sddl,
                )
            )
            if combined_guard:
                if original_storage_identity is None or storage_placeholder_identity is None:
                    raise RuntimeError("The active storage swap has no stable identities.")
                if not user_sid:
                    raise RuntimeError("The active storage swap has no user SID.")
                restored, storage_removed, database_removed = self._restore_swap_targets(
                    path,
                    database_sddl,
                    created_placeholders,
                    holding_name,
                    placeholder_name,
                    original_storage_identity,
                    storage_placeholder_identity,
                    str(storage_placeholder_sddl or ""),
                    user_sid,
                )
            else:
                restored, database_removed = self._restore_database_targets(
                    path, database_sddl, created_placeholders
                )
                storage_removed = 0
            self._clear_recovery_record()
            if combined_guard:
                _write_log(
                    f"Restored {restored} legacy rbx-storage directory/database target(s), "
                    f"removed {storage_removed} empty storage placeholder(s), and "
                    f"removed {database_removed} empty database placeholder(s) for "
                    f"{path.parent}."
                )
            else:
                _write_log(
                    f"Restored {restored} rbx-storage database ACL(s) and removed "
                    f"{database_removed} empty database placeholder(s) for {path.parent}."
                )
            self.path = None
            self.original_sddl = None
            self.original_tree_sddl = {}
            self.original_database_sddl = {}
            self.created_placeholders = {}
            self.blocked_user_sid = None
            self.holding_name = None
            self.placeholder_name = None
            self.original_storage_identity = None
            self.storage_placeholder_identity = None
            self.storage_placeholder_sddl = None
            self.launch_deadlines = {}
            self.release_at = None
        except Exception as exc:
            _write_log(
                f"Could not restore the rbx-storage database ACLs for "
                f"{path.parent}: {exc}",
                level="ERROR",
            )
            raise


class _ElevatedHelper:
    def __init__(self, connection: Connection, parent_pid: int, recovery_path: Path) -> None:
        self.connection: Optional[Connection] = connection
        self.parent_pid = int(parent_pid)
        self._smart_activity = _SmartActivityLogger()
        self.acl = _StorageAclGuard(
            recovery_path,
            activity_logger=self._smart_activity,
        )
        self.handoff_path = Path(recovery_path).with_name("merchant_fix_helper_handoff.json")
        self.blockers: dict[int, _PidDomainBlocker] = {}
        self.running = True
        self.last_resolve_at = 0.0
        self.addresses: set[str] = set()
        self._pending_refresh_addresses: Optional[set[str]] = None
        self._pending_refresh_pids: deque[int] = deque()
        self._pending_refresh_flows_by_pid: dict[int, list[dict[str, Any]]] = {}
        self._detached_handoff_id: Optional[str] = None
        self._prepared_handoff_id: Optional[str] = None
        self._handoff_acl_wait_logged = False
        self._handoff_adopted = False

    def _parent_alive(self) -> bool:
        try:
            return psutil.pid_exists(self.parent_pid)
        except Exception:
            # The authenticated pipe will still close when the parent exits.
            # Do not tear down every live PID block on a transient process-query
            # failure while that stronger liveness signal remains available.
            return True

    @staticmethod
    def _validate_handoff_id(raw_handoff_id: object) -> str:
        handoff_id = str(raw_handoff_id or "").strip().lower()
        if _HANDOFF_ID_PATTERN.fullmatch(handoff_id) is None:
            raise ValueError("Invalid Merchant Fix manager handoff identifier.")
        return handoff_id

    def _write_handoff_record(self, handoff_id: str, status: str) -> None:
        handoff_id = self._validate_handoff_id(handoff_id)
        status = str(status or "").strip().lower()
        if status not in {"ready", "waiting", "adopted"}:
            raise ValueError("Invalid Merchant Fix manager handoff status.")
        self.handoff_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.handoff_path.with_name(
            f"{self.handoff_path.name}.{os.getpid()}.tmp"
        )
        try:
            helper_creation_time = float(psutil.Process(os.getpid()).create_time())
        except Exception:
            helper_creation_time = 0.0
        record = {
            "version": 1,
            "handoff_id": handoff_id,
            "status": status,
            "helper_pid": os.getpid(),
            "helper_creation_time": helper_creation_time,
            "updated_at": time.time(),
            "blocked_pids": [
                {"pid": int(pid), "creation_time": float(blocker.creation_time)}
                for pid, blocker in self.blockers.items()
            ],
        }
        try:
            temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
            os.replace(temporary, self.handoff_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass

    def _handoff_was_adopted(self) -> bool:
        if self._handoff_adopted:
            return True
        handoff_id = self._detached_handoff_id
        if handoff_id is None:
            return False
        try:
            record = json.loads(self.handoff_path.read_text(encoding="utf-8"))
            record_id = self._validate_handoff_id(record.get("handoff_id"))
            status = str(record.get("status") or "").strip().lower()
        except Exception:
            return False
        # If READY overwrote this helper's WAITING record, the replacement has
        # already opened every expected PID block. Promote that state to
        # ADOPTED so both sides converge regardless of write ordering.
        if record_id == handoff_id and status == "ready":
            try:
                self._write_handoff_record(handoff_id, "adopted")
            except Exception:
                return False
            self._handoff_adopted = True
            return True
        # A different waiting helper can only publish its record after opening
        # replacement blocks, so it is also a completed takeover for this one.
        adopted = (record_id == handoff_id and status == "adopted") or (
            record_id != handoff_id and status == "waiting"
        )
        if adopted:
            self._handoff_adopted = True
        return adopted

    def _clear_own_handoff_record(self) -> None:
        handoff_id = self._detached_handoff_id or self._prepared_handoff_id
        if handoff_id is None:
            return
        try:
            record = json.loads(self.handoff_path.read_text(encoding="utf-8"))
            record_id = str(record.get("handoff_id") or "").strip().lower()
            record_helper_pid = int(record.get("helper_pid") or 0)
            record_status = str(record.get("status") or "").strip().lower()
            preserve_adoption = (
                self._handoff_adopted
                and self._prepared_handoff_id is None
                and record_status == "adopted"
            )
            if (
                record_id == handoff_id
                and record_helper_pid == os.getpid()
                and not preserve_adoption
            ):
                self.handoff_path.unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _parse_expected_handoff_blocks(raw_records: object) -> dict[int, float]:
        if not isinstance(raw_records, list):
            return {}
        expected: dict[int, float] = {}
        for raw in raw_records[:1000]:
            if not isinstance(raw, dict):
                continue
            try:
                pid = int(raw.get("pid") or 0)
                creation_time = float(raw.get("creation_time") or 0.0)
            except (TypeError, ValueError):
                continue
            if pid > 0 and creation_time > 0.0:
                expected[pid] = creation_time
        return expected

    def _prepare_handoff(self, raw_handoff_id: object, raw_records: object) -> bool:
        handoff_id = self._validate_handoff_id(raw_handoff_id)
        expected = self._parse_expected_handoff_blocks(raw_records)
        if not expected:
            raise RuntimeError("No live Smart PID blocks were provided for handoff preparation.")
        missing = []
        for pid, creation_time in expected.items():
            blocker = self.blockers.get(pid)
            if blocker is None or abs(float(blocker.creation_time) - creation_time) >= 0.01:
                missing.append(pid)
        if missing:
            raise RuntimeError(
                "Replacement helper is missing Smart PID block(s): "
                + ", ".join(str(pid) for pid in missing)
            )
        self._prepared_handoff_id = handoff_id
        self._write_handoff_record(handoff_id, "ready")
        _write_log(
            f"Replacement helper prepared {len(expected)} Smart PID block(s) "
            "for a late manager handoff."
        )
        return True

    def _detach_for_resume(self, raw_handoff_id: object) -> bool:
        handoff_id = self._validate_handoff_id(raw_handoff_id)
        self.acl.abandon_pending_launches()
        self._detached_handoff_id = handoff_id
        self._write_handoff_record(handoff_id, "waiting")
        _write_log(
            f"Merchant Fix helper detached for manager resume with "
            f"{len(self.blockers)} Smart PID block(s) still active."
        )
        return True

    def _complete_handoff(self, raw_handoff_id: object) -> bool:
        handoff_id = self._validate_handoff_id(raw_handoff_id)
        self._prepared_handoff_id = handoff_id
        self._write_handoff_record(handoff_id, "adopted")
        _write_log("Replacement helper completed the paused-manager Smart block handoff.")
        return True

    def _smart_ready(self) -> dict[str, Any]:
        from pydivert import windivert_dll
        from pydivert.consts import Flag, Layer

        # Opening a no-match handle validates the packaged WinDivert DLL,
        # driver loading, architecture and elevation before any Roblox launch.
        probe = windivert_dll.WinDivertOpen(
            b"false", int(Layer.NETWORK), 0, int(Flag.DEFAULT)
        )
        windivert_dll.WinDivertClose(probe)

        self.addresses = _resolve_domain()
        self.last_resolve_at = time.monotonic()
        return {"addresses": sorted(self.addresses)}

    def _block_pid(self, pid: int, creation_time: float) -> dict[str, Any]:
        pid = int(pid)
        process = psutil.Process(pid)
        actual_creation = float(process.create_time())
        if str(process.name() or "").lower() != ROBLOX_PROCESS_NAME:
            raise RuntimeError(f"PID {pid} is not RobloxPlayerBeta.exe.")
        if abs(actual_creation - float(creation_time)) >= 0.01:
            raise RuntimeError(f"PID {pid} was reused before the block could be applied.")
        old = self.blockers.pop(pid, None)
        if old is not None:
            old.stop()
        if self._pending_refresh_addresses is not None:
            addresses = set(self._pending_refresh_addresses)
        elif self.addresses and time.monotonic() - self.last_resolve_at < 60.0:
            addresses = set(self.addresses)
        else:
            addresses = _resolve_domain()
        self.addresses = set(addresses)
        self.last_resolve_at = time.monotonic()
        activity_logger = getattr(self, "_smart_activity", None)
        if activity_logger is None:
            blocker = _PidDomainBlocker(pid, actual_creation, addresses)
        else:
            blocker = _PidDomainBlocker(
                pid,
                actual_creation,
                addresses,
                activity_logger=activity_logger,
            )
        blocker.start()
        self.blockers[pid] = blocker
        return {"pid": pid, "addresses": sorted(addresses)}

    def _refresh_domain_addresses(self) -> None:
        if self._pending_refresh_addresses is None:
            if not self.blockers or time.monotonic() - self.last_resolve_at < 60.0:
                return
            self.last_resolve_at = time.monotonic()
            try:
                addresses = _resolve_domain()
            except Exception as exc:
                _write_log(f"Could not refresh {DOMAIN} addresses: {exc}", level="ERROR")
                return
            pending = deque(
                pid
                for pid, blocker in self.blockers.items()
                if blocker.addresses != addresses
            )
            if not pending:
                self.addresses = set(addresses)
                return
            try:
                flows_by_pid = _matching_connections_by_pid(set(pending), addresses)
            except Exception as exc:
                flows_by_pid = {pid: [] for pid in pending}
                _write_log(
                    f"Could not snapshot existing {DOMAIN} flows for Smart refresh: {exc}",
                    level="WARNING",
                )
            self._pending_refresh_addresses = set(addresses)
            self._pending_refresh_pids = pending
            self._pending_refresh_flows_by_pid = flows_by_pid
            _write_log(
                f"{DOMAIN} address set changed; refreshing {len(pending)} Smart PID "
                "block(s) incrementally."
            )

        addresses = self._pending_refresh_addresses
        if addresses is None:
            return
        while self._pending_refresh_pids:
            pid = self._pending_refresh_pids.popleft()
            old = self.blockers.get(pid)
            if old is None or old.addresses == addresses:
                continue
            try:
                try:
                    previous_hits = int(old.blocked_connection_count)
                except (TypeError, ValueError):
                    previous_hits = 0
                activity_logger = getattr(self, "_smart_activity", None)
                if activity_logger is None:
                    replacement = _PidDomainBlocker(
                        pid, old.creation_time, addresses
                    )
                else:
                    replacement = _PidDomainBlocker(
                        pid,
                        old.creation_time,
                        addresses,
                        activity_logger=activity_logger,
                    )
                replacement.inherit_blocked_connections(previous_hits)
                replacement.start(
                    existing_flows=(
                        getattr(self, "_pending_refresh_flows_by_pid", {}) or {}
                    ).pop(pid, [])
                )
                self.blockers[pid] = replacement
                old.stop(report_hits=False)
                try:
                    final_previous_hits = int(old.blocked_connection_count)
                except (TypeError, ValueError):
                    final_previous_hits = previous_hits
                replacement.inherit_blocked_connections(
                    max(0, final_previous_hits - previous_hits)
                )
            except Exception as exc:
                _write_log(f"Could not refresh Smart block for PID {pid}: {exc}", level="ERROR")
            break

        if not self._pending_refresh_pids:
            # One final machine-wide snapshot covers flows opened while the
            # SOCKET filters were being replaced. This keeps the expensive
            # scan count constant (two per refresh), rather than one per PID.
            refreshed = {
                pid: blocker
                for pid, blocker in self.blockers.items()
                if blocker.addresses == addresses
            }
            if refreshed:
                try:
                    final_flows = _matching_connections_by_pid(set(refreshed), addresses)
                    for pid, blocker in refreshed.items():
                        blocker.add_network_flows(final_flows.get(pid, []))
                except Exception as exc:
                    _write_log(
                        f"Could not reconcile existing {DOMAIN} flows after Smart refresh: {exc}",
                        level="WARNING",
                    )
            self.addresses = set(addresses)
            self._pending_refresh_addresses = None
            self._pending_refresh_flows_by_pid = {}
            _write_log(f"Completed incremental {DOMAIN} Smart PID block refresh.")

    def _cancel_domain_refresh(self) -> None:
        self._pending_refresh_addresses = None
        self._pending_refresh_pids.clear()
        self._pending_refresh_flows_by_pid = {}

    def _remove_dead_blockers(self) -> None:
        for pid, blocker in list(self.blockers.items()):
            try:
                process = psutil.Process(pid)
                alive = (
                    str(process.name() or "").lower() == ROBLOX_PROCESS_NAME
                    and abs(float(process.create_time()) - blocker.creation_time) < 0.01
                )
            except psutil.NoSuchProcess:
                alive = False
            except Exception:
                # AccessDenied and other transient inspection failures are not
                # proof that the process exited. Keep protection and retry on
                # the helper's next tick.
                continue
            if not alive:
                self.blockers.pop(pid, None)
                blocker.stop()

    def _dispatch(self, request: dict[str, Any]) -> Any:
        operation = str(request.get("operation") or "")
        if operation == "ping":
            try:
                creation_time = float(psutil.Process(os.getpid()).create_time())
            except Exception:
                creation_time = 0.0
            return {
                "admin": bool(ctypes.windll.shell32.IsUserAnAdmin()),
                "pid": os.getpid(),
                "creation_time": creation_time,
            }
        if operation == "smart_ready":
            return self._smart_ready()
        if operation == "database_prepare":
            self.acl.apply(
                str(request.get("path") or ""),
                str(request.get("user_sid") or ""),
                request.get("launch_id") or "legacy",
            )
            return True
        if operation == "database_release_after":
            self.acl.release_after(
                float(request.get("delay") or 0.0),
                request.get("launch_id") or "legacy",
            )
            return True
        if operation == "database_cancel":
            self.acl.cancel_launch(request.get("launch_id") or "legacy")
            return True
        if operation == "block_pid":
            return self._block_pid(int(request["pid"]), float(request["creation_time"]))
        if operation == "unblock_all":
            self._cancel_domain_refresh()
            for blocker in list(self.blockers.values()):
                blocker.stop()
            self.blockers.clear()
            self.acl.restore()
            self._smart_activity.flush()
            return True
        if operation == "classic_enable":
            _set_classic_hosts_block(True)
            return True
        if operation == "classic_disable":
            _set_classic_hosts_block(False)
            return True
        if operation == "detach_for_resume":
            return self._detach_for_resume(request.get("handoff_id"))
        if operation == "handoff_ready":
            return self._prepare_handoff(
                request.get("handoff_id"), request.get("blocked_pids")
            )
        if operation == "handoff_takeover":
            return self._complete_handoff(request.get("handoff_id"))
        if operation == "shutdown":
            self.running = False
            return True
        raise ValueError(f"Unknown helper operation: {operation}")

    def run(self) -> int:
        _write_log("Elevated Merchant Fix helper connected.")
        try:
            self.acl.recover_stale_acl()
            while self.running:
                if self._detached_handoff_id is not None:
                    if self._handoff_was_adopted():
                        if self.acl.active:
                            if not self._handoff_acl_wait_logged:
                                _write_log(
                                    "Paused-manager Smart block handoff was adopted; "
                                    "keeping the old helper until its database ACL "
                                    "deadline completes."
                                )
                                self._handoff_acl_wait_logged = True
                        else:
                            _write_log(
                                "Paused-manager Smart block handoff was adopted; "
                                "stopping old helper."
                            )
                            break
                    if not self.blockers:
                        _write_log(
                            "Detached Merchant Fix helper has no live PID blocks remaining."
                        )
                        break
                elif not self._parent_alive():
                    break
                self.acl.tick()
                self._remove_dead_blockers()
                connection = self.connection
                if connection is None:
                    self._refresh_domain_addresses()
                    time.sleep(0.25)
                    continue
                try:
                    ready = connection.poll(0)
                    if not ready:
                        self._refresh_domain_addresses()
                        ready = connection.poll(0.25)
                except Exception:
                    if self._detached_handoff_id is None:
                        raise
                    try:
                        connection.close()
                    except Exception:
                        pass
                    self.connection = None
                    continue
                if not ready:
                    continue
                try:
                    request = connection.recv()
                except EOFError:
                    if self._detached_handoff_id is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass
                        self.connection = None
                        continue
                    break
                try:
                    result = self._dispatch(request if isinstance(request, dict) else {})
                    response = {"ok": True, "result": result}
                except Exception as exc:
                    operation = (
                        str(request.get("operation") or "unknown")
                        if isinstance(request, dict)
                        else "invalid-request"
                    )
                    _write_log(
                        f"Helper operation {operation} failed: {exc}\n{traceback.format_exc().rstrip()}",
                        level="ERROR",
                    )
                    response = {"ok": False, "error": str(exc)}
                try:
                    connection.send(response)
                except Exception:
                    if self._detached_handoff_id is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass
                        self.connection = None
                        continue
                    break
        finally:
            try:
                self.acl.restore()
            except Exception:
                pass
            for blocker in list(self.blockers.values()):
                try:
                    blocker.stop()
                except Exception:
                    pass
            self.blockers.clear()
            self._smart_activity.flush()
            connection = self.connection
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._clear_own_handoff_record()
            _write_log("Elevated Merchant Fix helper stopped.")
        return 0


class MerchantFixController:
    def __init__(self) -> None:
        self._connection: Optional[Connection] = None
        self._listener: Optional[Listener] = None
        self._connected = threading.Event()
        self._start_lock = threading.Lock()
        self._rpc_lock = threading.Lock()
        self._state_lock = threading.RLock()
        # Serializes helper mutations whose ordering must agree with the local
        # Smart enabled state. In particular, a watcher must not apply a PID
        # block after deactivate_smart() has already completed unblock_all.
        self._smart_operation_lock = threading.RLock()
        self._smart_activity = _SmartActivityLogger()
        self._smart_enabled = False
        self._smart_block_trigger = DEFAULT_SMART_BLOCK_TRIGGER
        self._post_marker_delay_seconds = DEFAULT_POST_MARKER_DELAY_SECONDS
        self._menu_exit_delay_seconds = DEFAULT_MENU_EXIT_DELAY_SECONDS
        self._smart_user_scope = SMART_USER_SCOPE_BLACKLIST
        self._smart_user_ids: frozenset[str] = frozenset()
        self._prepared_launch_ids: set[str] = set()
        self._watch_generation = 0
        self._watchers: dict[int, threading.Event] = {}
        self._claimed_logs: dict[Path, int] = {}
        self._blocked_pids: dict[int, float] = {}
        self._helper_pid: Optional[int] = None
        self._helper_creation_time = 0.0
        self._resume_handoff_id: Optional[str] = None
        self._resume_handoff_detached = False
        self._resume_source_manager_pid: Optional[int] = None
        self._resume_source_manager_creation_time = 0.0
        self._late_handoff_stop: Optional[threading.Event] = None
        self._late_handoff_callback: Optional[Callable[[int], None]] = None
        self._handoff_adopted_by_replacement = False

    @property
    def smart_enabled(self) -> bool:
        with self._state_lock:
            return bool(self._smart_enabled)

    @property
    def post_marker_delay_seconds(self) -> float:
        with self._state_lock:
            return float(self._post_marker_delay_seconds)

    @property
    def menu_exit_delay_seconds(self) -> float:
        with self._state_lock:
            return float(self._menu_exit_delay_seconds)

    def smart_block_delay_seconds(self, trigger: object = None) -> float:
        with self._state_lock:
            normalized_trigger = normalize_smart_block_trigger(
                self._smart_block_trigger if trigger is None else trigger
            )
            if normalized_trigger == SMART_BLOCK_TRIGGER_MENU_EXIT:
                return float(self._menu_exit_delay_seconds)
            return float(self._post_marker_delay_seconds)

    @property
    def smart_block_trigger(self) -> str:
        with self._state_lock:
            return self._smart_block_trigger

    @property
    def smart_user_scope(self) -> str:
        with self._state_lock:
            return self._smart_user_scope

    @property
    def smart_user_ids(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(sorted(self._smart_user_ids))

    def _smart_applies_to_user_unlocked(self, user_id: object) -> bool:
        normalized_user_id = str(user_id).strip()
        if self._smart_user_scope == SMART_USER_SCOPE_WHITELIST:
            return normalized_user_id in self._smart_user_ids
        if self._smart_user_scope == SMART_USER_SCOPE_BLACKLIST:
            return normalized_user_id not in self._smart_user_ids
        return True

    def smart_applies_to_user(self, user_id: object) -> bool:
        with self._state_lock:
            return self._smart_applies_to_user_unlocked(user_id)

    @property
    def handoff_active(self) -> bool:
        with self._state_lock:
            return bool(self._resume_handoff_detached)

    @property
    def handoff_adopted_by_replacement(self) -> bool:
        with self._state_lock:
            return bool(self._handoff_adopted_by_replacement)

    def set_late_handoff_callback(
        self, callback: Optional[Callable[[int], None]]
    ) -> None:
        with self._state_lock:
            self._late_handoff_callback = callback

    @property
    def helper_connected(self) -> bool:
        return self._helper_connection_alive()

    def _helper_connection_alive(self) -> bool:
        connection = self._connection
        if connection is None or bool(getattr(connection, "closed", False)):
            return False
        helper_pid = self._helper_pid
        if helper_pid is None:
            return True
        try:
            process = psutil.Process(helper_pid)
            if self._helper_creation_time > 0.0:
                return abs(float(process.create_time()) - self._helper_creation_time) < 0.01
            return process.is_running()
        except psutil.NoSuchProcess:
            return False
        except Exception:
            # Access to an elevated process can be restricted when UAC was
            # approved with another administrator account. The authenticated
            # connection remains the best available signal in that case.
            return True

    def _discard_helper_connection(self, expected: Optional[Connection] = None) -> None:
        connection = self._connection
        if expected is not None and connection is not expected:
            return
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        self._connection = None
        self._helper_pid = None
        self._helper_creation_time = 0.0

    def _record_helper_identity(self, result: object) -> None:
        if not isinstance(result, dict):
            self._helper_pid = None
            self._helper_creation_time = 0.0
            return
        try:
            pid = int(result.get("pid") or 0)
            creation_time = float(result.get("creation_time") or 0.0)
        except (TypeError, ValueError):
            pid = 0
            creation_time = 0.0
        self._helper_pid = pid if pid > 0 else None
        self._helper_creation_time = max(0.0, creation_time)

    @staticmethod
    def _validated_handoff_id(raw_handoff_id: object) -> Optional[str]:
        handoff_id = str(raw_handoff_id or "").strip().lower()
        return handoff_id if _HANDOFF_ID_PATTERN.fullmatch(handoff_id) else None

    @staticmethod
    def _parse_resume_pid_records(raw_records: object) -> dict[int, float]:
        if not isinstance(raw_records, list):
            return {}
        records: dict[int, float] = {}
        for raw in raw_records[:1000]:
            if not isinstance(raw, dict):
                continue
            try:
                pid = int(raw.get("pid") or 0)
                creation_time = float(raw.get("creation_time") or 0.0)
            except (TypeError, ValueError):
                continue
            if pid > 0 and creation_time > 0.0:
                records[pid] = creation_time
        return records

    def _load_waiting_handoff_record(self) -> dict[str, Any]:
        try:
            record = json.loads(get_helper_handoff_path().read_text(encoding="utf-8"))
            handoff_id = self._validated_handoff_id(record.get("handoff_id"))
            if handoff_id is None or str(record.get("status") or "").lower() != "waiting":
                return {}
            return {
                "version": 1,
                "smart_enabled": True,
                "handoff_id": handoff_id,
                "detached": True,
                "blocked_pids": list(record.get("blocked_pids") or []),
            }
        except Exception:
            return {}

    def _handoff_record(self) -> dict[str, Any]:
        try:
            record = json.loads(get_helper_handoff_path().read_text(encoding="utf-8"))
            handoff_id = self._validated_handoff_id(record.get("handoff_id"))
            if handoff_id is None:
                return {}
            return dict(record)
        except Exception:
            return {}

    @staticmethod
    def _manager_identity() -> tuple[int, float]:
        pid = os.getpid()
        try:
            creation_time = float(psutil.Process(pid).create_time())
        except Exception:
            creation_time = 0.0
        return pid, creation_time

    def _resume_source_is_foreign(self) -> bool:
        with self._state_lock:
            source_pid = self._resume_source_manager_pid
            source_creation_time = self._resume_source_manager_creation_time
        if source_pid is None or source_pid <= 0:
            return False
        current_pid, current_creation_time = self._manager_identity()
        if source_pid != current_pid:
            return True
        if source_creation_time <= 0.0 or current_creation_time <= 0.0:
            return False
        return abs(source_creation_time - current_creation_time) >= 0.01

    def _replacement_handoff_ready(
        self, handoff_id: str, expected_blocks: list[tuple[int, float]]
    ) -> bool:
        record = self._handoff_record()
        if self._validated_handoff_id(record.get("handoff_id")) != handoff_id:
            return False
        if str(record.get("status") or "").strip().lower() != "ready":
            return False
        ready_blocks = self._parse_resume_pid_records(record.get("blocked_pids"))
        if any(
            pid not in ready_blocks
            or abs(float(ready_blocks[pid]) - creation_time) >= 0.01
            for pid, creation_time in expected_blocks
        ):
            return False
        try:
            helper_pid = int(record.get("helper_pid") or 0)
            helper_creation_time = float(record.get("helper_creation_time") or 0.0)
            updated_at = float(record.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            return False
        if helper_pid <= 0 or helper_pid == self._helper_pid:
            return False
        if updated_at <= 0.0 or time.time() - updated_at > 120.0:
            return False
        try:
            process = psutil.Process(helper_pid)
            if helper_creation_time > 0.0:
                return abs(float(process.create_time()) - helper_creation_time) < 0.01
            return process.is_running()
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            # UAC may have been approved with another administrator account.
            # The fresh, matching cryptographic handoff record is the strongest
            # signal available to the unelevated original manager in that case.
            return True
        except Exception:
            return False

    def _wait_for_handoff_adoption(self, handoff_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            record = self._handoff_record()
            if (
                self._validated_handoff_id(record.get("handoff_id")) == handoff_id
                and str(record.get("status") or "").strip().lower() == "adopted"
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _adopt_waiting_handoff_once(
        self, handoff_id: str, generation: int
    ) -> bool:
        record = self._handoff_record()
        if self._validated_handoff_id(record.get("handoff_id")) != handoff_id:
            return False
        status = str(record.get("status") or "").strip().lower()
        if status not in {"waiting", "adopted"}:
            return False
        with self._state_lock:
            if (
                not self._smart_enabled
                or generation != self._watch_generation
                or self._resume_handoff_id != handoff_id
            ):
                return False
        live = self._live_blocked_pids()
        self._request_with_reconnect(
            "handoff_takeover", handoff_id=handoff_id, timeout=10.0
        )
        with self._state_lock:
            if self._resume_handoff_id == handoff_id:
                self._resume_handoff_detached = False
                self._resume_handoff_id = None
            callback = self._late_handoff_callback
        _write_log(
            f"Completed late paused-manager handoff for {len(live)} live Smart PID block(s)."
        )
        if callback is not None:
            try:
                callback(len(live))
            except Exception as exc:
                _write_log(f"Late handoff callback failed: {exc}", level="ERROR")
        return True

    def _start_late_handoff_watch(self, handoff_id: str, generation: int) -> None:
        with self._state_lock:
            previous = self._late_handoff_stop
            if previous is not None:
                previous.set()
            stop_event = threading.Event()
            self._late_handoff_stop = stop_event

        def _watch() -> None:
            try:
                while not stop_event.wait(0.1):
                    if self._adopt_waiting_handoff_once(handoff_id, generation):
                        return
                    with self._state_lock:
                        if (
                            not self._smart_enabled
                            or generation != self._watch_generation
                            or self._resume_handoff_id != handoff_id
                        ):
                            return
            except Exception as exc:
                _write_log(f"Late Smart handoff watcher failed: {exc}", level="ERROR")
            finally:
                with self._state_lock:
                    if self._late_handoff_stop is stop_event:
                        self._late_handoff_stop = None

        threading.Thread(
            target=_watch,
            name="MerchantFix-LateHandoff",
            daemon=True,
        ).start()

    def _release_detached_handoff(self) -> None:
        handoff_id = self._validated_handoff_id(self._resume_handoff_id)
        if not self._resume_handoff_detached or handoff_id is None:
            return
        path = get_helper_handoff_path()
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "handoff_id": handoff_id,
                        "status": "adopted",
                        "helper_pid": 0,
                        "updated_at": time.time(),
                        "blocked_pids": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
        self._resume_handoff_detached = False
        _write_log("Released detached paused-manager Smart block handoff.")

    def import_manager_resume_state(self, resume_state: object) -> int:
        state = (
            resume_state.get(MANAGER_RESUME_STATE_KEY)
            if isinstance(resume_state, dict)
            else None
        )
        state = dict(state) if isinstance(state, dict) else {}
        if bool(state.get("detached")):
            waiting = self._load_waiting_handoff_record()
            if waiting:
                state = waiting
        handoff_id = self._validated_handoff_id(state.get("handoff_id"))
        blocked = self._parse_resume_pid_records(state.get("blocked_pids"))
        if not state or handoff_id is None:
            return 0
        try:
            source_manager_pid = int(state.get("owner_manager_pid") or 0)
            source_creation_time = float(
                state.get("owner_manager_creation_time") or 0.0
            )
        except (TypeError, ValueError):
            source_manager_pid = 0
            source_creation_time = 0.0
        with self._state_lock:
            if bool(state.get("smart_enabled")):
                self._smart_enabled = True
            self._blocked_pids.update(blocked)
            self._resume_handoff_id = handoff_id
            self._resume_handoff_detached = bool(state.get("detached"))
            self._resume_source_manager_pid = (
                source_manager_pid if source_manager_pid > 0 else None
            )
            self._resume_source_manager_creation_time = max(
                0.0, source_creation_time
            )
        return len(self._live_blocked_pids())

    def attach_manager_resume_state(
        self,
        resume_state: object,
        *,
        detached: Optional[bool] = None,
        force_new_handoff: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(resume_state, dict):
            return {}
        live = self._live_blocked_pids() if self.smart_enabled else []
        if not live:
            resume_state.pop(MANAGER_RESUME_STATE_KEY, None)
            return {}
        handoff_id = self._resume_handoff_id
        if force_new_handoff or self._validated_handoff_id(handoff_id) is None:
            handoff_id = secrets.token_hex(32)
        is_detached = (
            self._resume_handoff_detached if detached is None else bool(detached)
        )
        manager_pid, manager_creation_time = self._manager_identity()
        state = {
            "version": 1,
            "smart_enabled": True,
            "handoff_id": handoff_id,
            "detached": is_detached,
            "owner_manager_pid": manager_pid,
            "owner_manager_creation_time": manager_creation_time,
            "blocked_pids": [
                {"pid": pid, "creation_time": creation_time}
                for pid, creation_time in live
            ],
        }
        resume_state[MANAGER_RESUME_STATE_KEY] = state
        self._resume_handoff_id = handoff_id
        self._resume_handoff_detached = is_detached
        self._resume_source_manager_pid = manager_pid
        self._resume_source_manager_creation_time = manager_creation_time
        return state

    def adopt_manager_resume_state(self, resume_state: object) -> int:
        restored = self.import_manager_resume_state(resume_state)
        if not self._resume_handoff_detached:
            handoff_id = self._validated_handoff_id(self._resume_handoff_id)
            if restored and handoff_id is not None and self._resume_source_is_foreign():
                # The replacement manager resumed while the original manager is
                # still open. Open duplicate blocks first, advertise readiness,
                # then wait for the original helper's eventual detach.
                self.ensure_helper()
                live = self._live_blocked_pids()
                self._request_with_reconnect(
                    "handoff_ready",
                    handoff_id=handoff_id,
                    blocked_pids=[
                        {"pid": pid, "creation_time": creation_time}
                        for pid, creation_time in live
                    ],
                    timeout=10.0,
                )
                with self._state_lock:
                    generation = self._watch_generation
                self._start_late_handoff_watch(handoff_id, generation)
                _write_log(
                    f"Replacement manager preloaded {len(live)} Smart PID block(s); "
                    "waiting for the original manager to close."
                )
            return restored
        handoff_id = self._validated_handoff_id(self._resume_handoff_id)
        if handoff_id is None:
            raise RuntimeError("The paused-manager Merchant Fix handoff is invalid.")
        live = self._live_blocked_pids()
        if not live:
            self._resume_handoff_detached = False
            return 0
        self.ensure_helper()
        self._request_with_reconnect(
            "handoff_takeover", handoff_id=handoff_id, timeout=10.0
        )
        self._resume_handoff_detached = False
        self._resume_handoff_id = None
        if isinstance(resume_state, dict):
            state = resume_state.get(MANAGER_RESUME_STATE_KEY)
            if isinstance(state, dict):
                state["detached"] = False
        _write_log(
            f"Adopted paused-manager Smart state with {len(live)} live PID block(s)."
        )
        return len(live)

    def detach_for_manager_resume(self, resume_state: object) -> bool:
        self.import_manager_resume_state(resume_state)
        if self._resume_handoff_detached:
            self.adopt_manager_resume_state(resume_state)
        live = self._live_blocked_pids() if self.smart_enabled else []
        if not live:
            return False
        self.ensure_helper()
        state = self.attach_manager_resume_state(
            resume_state, detached=True, force_new_handoff=False
        )
        handoff_id = self._validated_handoff_id(state.get("handoff_id"))
        if handoff_id is None:
            raise RuntimeError("Could not create a paused-manager Merchant Fix handoff.")
        replacement_ready = self._replacement_handoff_ready(handoff_id, live)
        self._request_with_reconnect(
            "detach_for_resume", handoff_id=handoff_id, timeout=20.0
        )
        connection = self._connection
        self._discard_helper_connection(connection)
        adopted = (
            self._wait_for_handoff_adoption(handoff_id, 2.0)
            if replacement_ready
            else False
        )
        with self._state_lock:
            self._handoff_adopted_by_replacement = adopted
        _write_log(
            f"Detached Smart helper with {len(live)} PID block(s) for manager resume"
            f"; replacement {'adopted the blocks' if adopted else 'will adopt later'}."
        )
        return True

    def load_settings(self, settings: object) -> None:
        cfg = settings.get("merchant_fix", {}) if isinstance(settings, dict) else {}
        with self._state_lock:
            self._smart_enabled = (
                bool(cfg.get("smart_enabled", False))
                if isinstance(cfg, dict)
                else False
            )
            raw_trigger = (
                cfg.get("smart_block_trigger", DEFAULT_SMART_BLOCK_TRIGGER)
                if isinstance(cfg, dict)
                else DEFAULT_SMART_BLOCK_TRIGGER
            )
            self._smart_block_trigger = normalize_smart_block_trigger(raw_trigger)
            raw_delay = (
                cfg.get("post_marker_delay_seconds", DEFAULT_POST_MARKER_DELAY_SECONDS)
                if isinstance(cfg, dict)
                else DEFAULT_POST_MARKER_DELAY_SECONDS
            )
            self._post_marker_delay_seconds = normalize_post_marker_delay(raw_delay)
            raw_menu_exit_delay = (
                cfg.get(
                    "menu_exit_delay_seconds",
                    raw_delay
                    if self._smart_block_trigger == SMART_BLOCK_TRIGGER_MENU_EXIT
                    else DEFAULT_MENU_EXIT_DELAY_SECONDS,
                )
                if isinstance(cfg, dict)
                else DEFAULT_MENU_EXIT_DELAY_SECONDS
            )
            self._menu_exit_delay_seconds = normalize_post_marker_delay(
                raw_menu_exit_delay
            )
            raw_scope = (
                cfg.get("smart_user_scope", SMART_USER_SCOPE_ALL)
                if isinstance(cfg, dict)
                else SMART_USER_SCOPE_ALL
            )
            raw_user_ids = (
                cfg.get("smart_user_ids", ()) if isinstance(cfg, dict) else ()
            )
            self._smart_user_scope = normalize_smart_user_scope(raw_scope)
            normalized_ids = normalize_smart_user_ids(raw_user_ids)
            if str(raw_scope or "").strip().lower() == SMART_USER_SCOPE_ALL:
                normalized_ids = ()
            self._smart_user_ids = frozenset(normalized_ids)

    def set_smart_block_trigger(self, value: object) -> str:
        trigger = normalize_smart_block_trigger(value)
        with self._state_lock:
            self._smart_block_trigger = trigger
        label = (
            "main-menu exit"
            if trigger == SMART_BLOCK_TRIGGER_MENU_EXIT
            else "BossRaidUI marker"
        )
        _write_log(f"Smart asset-block delay trigger changed to {label}.")
        return trigger

    def set_post_marker_delay_seconds(self, value: object) -> float:
        return self.set_smart_block_delay_seconds(
            value, trigger=SMART_BLOCK_TRIGGER_BOSS_RAID_UI
        )

    def set_smart_block_delay_seconds(
        self, value: object, *, trigger: object = None
    ) -> float:
        delay = normalize_post_marker_delay(value)
        with self._state_lock:
            normalized_trigger = normalize_smart_block_trigger(
                self._smart_block_trigger if trigger is None else trigger
            )
            if normalized_trigger == SMART_BLOCK_TRIGGER_MENU_EXIT:
                self._menu_exit_delay_seconds = delay
                label = "main-menu exit"
            else:
                self._post_marker_delay_seconds = delay
                label = "BossRaidUI marker"
        _write_log(f"Smart {label} asset-block delay changed to {delay:.2f}s.")
        return delay

    def set_smart_user_selection(
        self, scope: object, user_ids: object
    ) -> tuple[str, tuple[str, ...]]:
        normalized_scope = normalize_smart_user_scope(scope)
        normalized_ids = normalize_smart_user_ids(user_ids)
        if str(scope or "").strip().lower() == SMART_USER_SCOPE_ALL:
            normalized_ids = ()
        with self._state_lock:
            self._smart_user_scope = normalized_scope
            self._smart_user_ids = frozenset(normalized_ids)
        description = (
            f"{normalized_scope} with {len(normalized_ids)} selected user(s)"
        )
        _write_log(f"Smart user selection changed to {description}.")
        return normalized_scope, normalized_ids

    def set_smart_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._smart_operation_lock:
            with self._state_lock:
                self._smart_enabled = enabled
                self._watch_generation += 1
                if not enabled:
                    late_handoff_stop = self._late_handoff_stop
                    if late_handoff_stop is not None:
                        late_handoff_stop.set()
                    self._late_handoff_stop = None
                    for stop_event in self._watchers.values():
                        stop_event.set()
                    self._watchers.clear()
                    self._claimed_logs.clear()
                    self._blocked_pids.clear()
                    self._prepared_launch_ids.clear()
                    self._resume_handoff_id = None
                    self._resume_handoff_detached = False
                    self._resume_source_manager_pid = None
                    self._resume_source_manager_creation_time = 0.0
                    self._handoff_adopted_by_replacement = False
        if not enabled:
            self._smart_activity.flush()
        _write_log(f"Smart mode setting changed to {'enabled' if enabled else 'disabled'}.")

    def _accept_helper(self, listener: Listener) -> None:
        try:
            connection = listener.accept()
            self._connection = connection
            self._connected.set()
        except Exception as exc:
            _write_log(f"Elevated helper connection failed: {exc}", level="ERROR")
        finally:
            try:
                listener.close()
            except Exception:
                pass
            if self._listener is listener:
                self._listener = None

    def _launch_elevated(self, port: int, auth_hex: str) -> None:
        if os.name != "nt":
            raise RuntimeError("Merchant Fix is only available on Windows.")
        if getattr(sys, "frozen", False) or "__compiled__" in globals():
            executable = sys.executable
            arguments = [
                "--merchant-fix-helper",
                str(port),
                auth_hex,
                str(os.getpid()),
                str(get_merchant_fix_log_path()),
                str(get_acl_recovery_path()),
            ]
            working_directory = str(Path(sys.executable).resolve().parent)
        else:
            executable = sys.executable
            arguments = [
                str(Path(__file__).resolve()),
                "--helper",
                str(port),
                auth_hex,
                str(os.getpid()),
                str(get_merchant_fix_log_path()),
                str(get_acl_recovery_path()),
            ]
            working_directory = str(Path(__file__).resolve().parent)
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            executable,
            subprocess.list2cmdline(arguments),
            working_directory,
            0,
        )
        if int(result) <= 32:
            raise RuntimeError("Administrator permission was not granted.")

    def ensure_helper(self, timeout: float = 60.0) -> None:
        with self._start_lock:
            if self._helper_connection_alive():
                return
            self._discard_helper_connection()

            auth_key = secrets.token_bytes(32)
            listener = Listener(("127.0.0.1", 0), family="AF_INET", authkey=auth_key)
            self._listener = listener
            self._connected.clear()
            port = int(listener.address[1])
            threading.Thread(
                target=self._accept_helper,
                args=(listener,),
                name="MerchantFix-HelperAccept",
                daemon=True,
            ).start()
            try:
                self._launch_elevated(port, auth_key.hex())
            except Exception:
                try:
                    listener.close()
                except Exception:
                    pass
                self._listener = None
                raise
            if not self._connected.wait(max(1.0, float(timeout))):
                try:
                    listener.close()
                except Exception:
                    pass
                raise RuntimeError("Timed out waiting for the elevated Merchant Fix helper.")
            try:
                handshake = self._request("ping", timeout=5.0)
                self._record_helper_identity(handshake)
                self._rehydrate_blocked_pids()
            except Exception:
                self._discard_helper_connection()
                raise

    def _request(self, operation: str, *, timeout: float = 20.0, **payload: Any) -> Any:
        connection = self._connection
        if connection is None:
            raise RuntimeError("The elevated Merchant Fix helper is not connected.")
        request = {"operation": operation, **payload}
        with self._rpc_lock:
            try:
                connection.send(request)
                if not connection.poll(max(0.1, float(timeout))):
                    raise RuntimeError(f"Merchant Fix helper timed out during {operation}.")
                response = connection.recv()
            except Exception as exc:
                if operation != "ping":
                    _write_log(f"Helper request {operation} transport failed: {exc}", level="ERROR")
                raise _HelperTransportError(
                    f"Merchant Fix helper connection failed during {operation}: {exc}"
                ) from exc
        if not isinstance(response, dict) or not response.get("ok"):
            error = response.get("error") if isinstance(response, dict) else "Invalid helper response."
            raise RuntimeError(str(error or f"Merchant Fix helper failed during {operation}."))
        return response.get("result")

    def _request_with_reconnect(
        self, operation: str, *, timeout: float = 20.0, **payload: Any
    ) -> Any:
        for attempt in range(2):
            self.ensure_helper()
            connection = self._connection
            try:
                return self._request(operation, timeout=timeout, **payload)
            except _HelperTransportError:
                self._discard_helper_connection(connection)
                if attempt:
                    raise
                _write_log(
                    f"Merchant Fix helper disconnected during {operation}; reconnecting once.",
                    level="ERROR",
                )
        raise RuntimeError(f"Merchant Fix helper could not complete {operation}.")

    def _live_blocked_pids(self) -> list[tuple[int, float]]:
        with self._state_lock:
            candidates = list(self._blocked_pids.items())
        live: list[tuple[int, float]] = []
        stale: list[int] = []
        for pid, creation_time in candidates:
            try:
                process = psutil.Process(pid)
                valid = (
                    str(process.name() or "").lower() == ROBLOX_PROCESS_NAME
                    and abs(float(process.create_time()) - creation_time) < 0.01
                )
            except psutil.NoSuchProcess:
                valid = False
            except Exception:
                # A transient permission/query failure is inconclusive. Keep
                # the block tracked so it can be displayed and rehydrated.
                live.append((pid, creation_time))
                continue
            if valid:
                live.append((pid, creation_time))
            else:
                stale.append(pid)
        if stale:
            with self._state_lock:
                for pid in stale:
                    self._blocked_pids.pop(pid, None)
        return live

    def current_blocks(self) -> list[dict[str, Any]]:
        """Return live Smart blocks for display without contacting the helper."""
        connected = self.helper_connected
        with self._state_lock:
            detached = bool(self._resume_handoff_detached)
        if connected:
            state = "Active"
        elif detached:
            state = "Handoff active"
        else:
            state = "Helper disconnected"
        return [
            {
                "scope": "Smart",
                "pid": pid,
                "creation_time": creation_time,
                "domain": DOMAIN,
                "state": state,
            }
            for pid, creation_time in self._live_blocked_pids()
        ]

    def _rehydrate_blocked_pids(self) -> None:
        if not self.smart_enabled:
            return
        live = self._live_blocked_pids()
        if not live:
            return
        _write_log(
            f"Reapplying {len(live)} Smart PID block(s) after helper reconnection."
        )
        restored = 0
        failed: list[int] = []
        for pid, creation_time in live:
            try:
                self._request(
                    "block_pid", pid=pid, creation_time=creation_time, timeout=30.0
                )
                restored += 1
            except _HelperTransportError:
                raise
            except Exception as exc:
                failed.append(pid)
                _write_log(
                    f"Could not reapply Smart block for PID {pid}: {exc}", level="ERROR"
                )
        if failed:
            still_live = {pid for pid, _creation_time in self._live_blocked_pids()}
            failed_live = [pid for pid in failed if pid in still_live]
            if failed_live:
                raise RuntimeError(
                    "Could not restore Smart protection for live PID(s): "
                    + ", ".join(str(pid) for pid in failed_live)
                )
        _write_log(f"Reapplied {restored} of {len(live)} Smart PID block(s).")

    def activate_smart(self) -> None:
        with self._smart_operation_lock:
            if is_classic_block_active():
                raise RuntimeError("Disable the global Classic block before enabling Smart mode.")
            with self._state_lock:
                if (
                    self._smart_user_scope == SMART_USER_SCOPE_WHITELIST
                    and not self._smart_user_ids
                ):
                    raise RuntimeError("Select at least one user before enabling Smart mode.")
            result = self._request_with_reconnect(
                "smart_ready",
                timeout=30.0,
            )
            addresses = result.get("addresses", []) if isinstance(result, dict) else []
            _write_log(
                f"Smart mode privilege check passed; resolved {len(addresses)} domain address(es)."
            )
            self.set_smart_enabled(True)

    def recover_stale_acl_if_needed(self) -> None:
        if get_acl_recovery_path().is_file():
            self.ensure_helper()

    def deactivate_smart(self) -> None:
        with self._smart_operation_lock:
            self._release_detached_handoff()
            self.set_smart_enabled(False)
            if self._connection is not None:
                self._request_with_reconnect("unblock_all", timeout=20.0)

    def before_roblox_launch(self, user_id: object) -> bool:
        with self._smart_operation_lock:
            with self._state_lock:
                if not self._smart_enabled:
                    return False
                applies = self._smart_applies_to_user_unlocked(user_id)
            if not applies:
                self._smart_activity.record(
                    "managed launches skipped by Smart user selection"
                )
                return False
            self._request_with_reconnect(
                "database_prepare",
                path=str(get_roblox_storage_dir()),
                user_sid=get_current_user_sid(),
                launch_id=str(user_id),
                timeout=20.0,
            )
            with self._state_lock:
                self._prepared_launch_ids.add(str(user_id))
        self._smart_activity.record("managed launches admitted by Smart mode")
        return True

    def cancel_prelaunch(self, user_id: object, reason: str) -> None:
        with self._state_lock:
            admitted = str(user_id) in self._prepared_launch_ids
            self._prepared_launch_ids.discard(str(user_id))
        if admitted:
            try:
                self._request_with_reconnect(
                    "database_cancel", launch_id=str(user_id), timeout=20.0
                )
                _write_log(
                    f"Cancelled Smart database-file guard for user {user_id}: {reason}."
                )
            except Exception as exc:
                _write_log(
                    f"Could not cancel Smart database-file guard: {exc}",
                    level="ERROR",
                )

    def on_roblox_process_created(
        self, pid: int, user_id: object, process_created_at: Optional[float] = None
    ) -> None:
        launch_id = str(user_id)
        with self._smart_operation_lock:
            with self._state_lock:
                prepared = launch_id in self._prepared_launch_ids
                self._prepared_launch_ids.discard(launch_id)
                if not self._smart_enabled or not prepared:
                    return
            process = psutil.Process(int(pid))
            creation_time = float(process_created_at or process.create_time())
            remaining = max(
                0.0,
                (creation_time + SMART_DATABASE_LOCK_SECONDS) - time.time(),
            )
            self._request_with_reconnect(
                "database_release_after",
                delay=remaining,
                launch_id=launch_id,
                timeout=10.0,
            )
            with self._state_lock:
                generation = self._watch_generation
                old = self._watchers.pop(int(pid), None)
                if old is not None:
                    old.set()
                stop_event = threading.Event()
                self._watchers[int(pid)] = stop_event
            threading.Thread(
                target=self._watch_for_user_key,
                args=(int(pid), str(user_id), creation_time, generation, stop_event),
                name=f"MerchantFix-Log-{int(pid)}",
                daemon=True,
            ).start()

    def _candidate_log_for_pid(self, pid: int, creation_time: float) -> Optional[Path]:
        try:
            process = psutil.Process(pid)
            for opened in process.open_files():
                path = Path(opened.path)
                if path.suffix.lower() == ".log" and "roblox" in str(path).lower():
                    return path
        except Exception:
            pass

        local_appdata = os.environ.get("LOCALAPPDATA")
        if not local_appdata:
            return None
        logs_dir = Path(local_appdata) / "Roblox" / "logs"
        if not logs_dir.is_dir():
            return None
        candidates: list[tuple[float, Path]] = []
        try:
            for path in logs_dir.glob("*.log"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < creation_time - 3.0:
                    continue
                # On Windows st_ctime is the file creation time.  Do not bind a
                # new PID to a prior session's log merely because that old log
                # received a final write while the account was relaunching.
                if (
                    stat.st_ctime
                    < creation_time - SMART_LOG_CREATION_EARLY_TOLERANCE_SECONDS
                ):
                    continue
                with self._state_lock:
                    owner = self._claimed_logs.get(path)
                if owner not in (None, pid):
                    continue
                candidates.append((abs(stat.st_ctime - creation_time), path))
        except OSError:
            return None
        return min(candidates, default=(0.0, None), key=lambda item: item[0])[1]

    def _watch_for_user_key(
        self,
        pid: int,
        user_id: str,
        creation_time: float,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        log_path: Optional[Path] = None
        offset = 0
        carry = ""
        trigger = self.smart_block_trigger
        saw_main_menu = False
        pending_menu_delay: Optional[float] = None
        self._smart_activity.record("Roblox log watchers started", pid=pid)
        try:
            while True:
                poll_seconds = 0.2
                if pending_menu_delay is not None:
                    poll_seconds = min(poll_seconds, max(0.0, pending_menu_delay))
                if poll_seconds > 0.0 and stop_event.wait(poll_seconds):
                    return
                if pending_menu_delay is not None:
                    pending_menu_delay = max(
                        0.0, pending_menu_delay - poll_seconds
                    )
                with self._state_lock:
                    if (
                        not self._smart_enabled
                        or generation != self._watch_generation
                    ):
                        return
                try:
                    process = psutil.Process(pid)
                    if (
                        str(process.name() or "").lower() != ROBLOX_PROCESS_NAME
                        or abs(float(process.create_time()) - creation_time) >= 0.01
                    ):
                        return
                except psutil.NoSuchProcess:
                    return
                except Exception:
                    # A transient process query failure must not permanently
                    # abandon protection for an otherwise live managed PID.
                    continue

                if log_path is None or not log_path.exists():
                    log_path = self._candidate_log_for_pid(pid, creation_time)
                    if log_path is None:
                        continue
                    with self._state_lock:
                        self._claimed_logs[log_path] = pid
                    offset = 0
                    carry = ""
                    self._smart_activity.record(
                        "Roblox logs associated with managed PIDs",
                        pid=pid,
                    )

                try:
                    size = log_path.stat().st_size
                    if size < offset:
                        offset = 0
                        carry = ""
                    text = ""
                    if size != offset:
                        with log_path.open("rb") as handle:
                            handle.seek(offset)
                            raw = handle.read()
                            offset = handle.tell()
                        decoded = raw.decode("utf-8", errors="replace")
                        text = carry + decoded
                        if trigger == SMART_BLOCK_TRIGGER_MENU_EXIT:
                            # Only parse complete log lines. This preserves a split
                            # RPC JSON entry without replaying completed states on
                            # every later write. A syntactically complete trailing
                            # RPC entry is safe to consume even without a newline;
                            # Roblox can leave its final flushed record that way.
                            line_end = max(text.rfind("\n"), text.rfind("\r"))
                            if line_end < 0:
                                carry = text[-8192:]
                                if _smart_menu_states_from_log_text(carry):
                                    text = carry
                                    carry = ""
                                else:
                                    text = ""
                            else:
                                carry = text[line_end + 1 :][-8192:]
                                text = text[: line_end + 1]
                                if carry and _smart_menu_states_from_log_text(carry):
                                    text += carry
                                    carry = ""
                        else:
                            carry = text[-8192:]
                except (OSError, PermissionError):
                    continue

                if trigger == SMART_BLOCK_TRIGGER_MENU_EXIT:
                    apply_reason = ""
                    for in_menu in _smart_menu_states_from_log_text(text):
                        if in_menu:
                            saw_main_menu = True
                            if pending_menu_delay is not None:
                                _write_log(
                                    f"PID {pid} returned to the Roblox main menu; "
                                    "cancelled the pending per-PID block delay."
                                )
                            pending_menu_delay = None
                        elif saw_main_menu:
                            if pending_menu_delay is not None:
                                continue
                            delay = self.smart_block_delay_seconds(trigger)
                            if delay > 0.0:
                                pending_menu_delay = delay
                                self._smart_activity.record(
                                    "main-menu exit triggers awaiting block delay",
                                    pid=pid,
                                )
                            else:
                                apply_reason = "immediate"
                                break
                    if (
                        not apply_reason
                        and pending_menu_delay is not None
                        and pending_menu_delay <= 0.0
                    ):
                        apply_reason = "elapsed"
                    if not apply_reason:
                        continue
                    if apply_reason == "elapsed":
                        self._smart_activity.record(
                            "PID block requests issued after delay",
                            pid=pid,
                        )
                    else:
                        self._smart_activity.record(
                            "PID block requests issued immediately",
                            pid=pid,
                        )
                else:
                    if SMART_LOG_PATTERN.search(text) is None:
                        continue
                    delay = self.smart_block_delay_seconds(trigger)
                    if delay > 0.0:
                        self._smart_activity.record(
                            "BossRaidUI triggers awaiting block delay",
                            pid=pid,
                        )
                        if stop_event.wait(delay):
                            return
                        with self._state_lock:
                            if not self._smart_enabled or generation != self._watch_generation:
                                return
                        try:
                            process = psutil.Process(pid)
                            if (
                                str(process.name() or "").lower() != ROBLOX_PROCESS_NAME
                                or abs(float(process.create_time()) - creation_time) >= 0.01
                            ):
                                return
                        except Exception:
                            return
                        self._smart_activity.record(
                            "PID block requests issued after delay",
                            pid=pid,
                        )
                    else:
                        self._smart_activity.record(
                            "PID block requests issued immediately",
                            pid=pid,
                        )
                with self._smart_operation_lock:
                    with self._state_lock:
                        if (
                            not self._smart_enabled
                            or generation != self._watch_generation
                            or stop_event.is_set() is True
                        ):
                            return
                    self._request_with_reconnect(
                        "block_pid", pid=pid, creation_time=creation_time, timeout=30.0
                    )
                    with self._state_lock:
                        if self._smart_enabled and generation == self._watch_generation:
                            self._blocked_pids[pid] = creation_time
                return
        except Exception as exc:
            _write_log(f"Smart log watcher failed for PID {pid}: {exc}", level="ERROR")
        finally:
            with self._state_lock:
                if self._watchers.get(pid) is stop_event:
                    self._watchers.pop(pid, None)
                if log_path is not None and self._claimed_logs.get(log_path) == pid:
                    self._claimed_logs.pop(log_path, None)

    def enable_classic_block(self) -> None:
        if self.smart_enabled:
            raise RuntimeError("Disable Smart mode before enabling the global Classic block.")
        self._request_with_reconnect("classic_enable", timeout=20.0)

    def disable_classic_block(self) -> None:
        self._request_with_reconnect("classic_disable", timeout=20.0)

    def shutdown(self) -> None:
        self._release_detached_handoff()
        self.set_smart_enabled(False)
        connection = self._connection
        if connection is not None:
            try:
                self._request("shutdown", timeout=3.0)
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass
        self._connection = None


_CONTROLLER = MerchantFixController()


def get_merchant_fix_controller() -> MerchantFixController:
    return _CONTROLLER


def run_elevated_helper(
    port: int,
    auth_hex: str,
    parent_pid: int,
    log_path: Optional[str] = None,
    recovery_path: Optional[str] = None,
) -> int:
    global _LOG_PATH_OVERRIDE
    if log_path:
        candidate = Path(log_path).resolve(strict=False)
        if (
            candidate.name.lower() == "merchantfix.log"
            and candidate.parent.name.lower() == "logs"
            and candidate.parent.parent.name.lower() == "jaram"
        ):
            _LOG_PATH_OVERRIDE = candidate
    if os.name != "nt" or not bool(ctypes.windll.shell32.IsUserAnAdmin()):
        _write_log("Merchant Fix helper was started without administrator rights.", level="ERROR")
        return 2
    try:
        connection = Client(("127.0.0.1", int(port)), family="AF_INET", authkey=bytes.fromhex(auth_hex))
        recovery = Path(recovery_path).resolve(strict=False) if recovery_path else get_acl_recovery_path()
        if (
            recovery.name.lower() != "merchant_fix_acl_recovery.json"
            or recovery.parent.name.lower() != "jaram"
        ):
            raise ValueError("Invalid Merchant Fix ACL recovery path.")
        return _ElevatedHelper(connection, int(parent_pid), recovery).run()
    except Exception as exc:
        _write_log(
            f"Elevated Merchant Fix helper exited unexpectedly: {exc}\n"
            f"{traceback.format_exc().rstrip()}",
            level="ERROR",
        )
        return 3


def run_helper_from_argv(arguments: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args and args[0] in {"--helper", "--merchant-fix-helper"}:
        args = args[1:]
    if len(args) not in (3, 4, 5):
        return 2
    return run_elevated_helper(
        int(args[0]),
        args[1],
        int(args[2]),
        args[3] if len(args) >= 4 else None,
        args[4] if len(args) >= 5 else None,
    )


if __name__ == "__main__":
    raise SystemExit(run_helper_from_argv())
