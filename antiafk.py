"""
antiafk.py

Multiprocessing proxy for the native pybind11 extension (`antiafk_native`).

The native engine runs inside a dedicated worker process so the GUI and other
Python threads stay responsive (and so native input automation is isolated).
"""

from __future__ import annotations

import ctypes
import multiprocessing as mp
import queue
import threading
import time
import traceback
from typing import Any, Optional

from ctypes import wintypes

_USING_NATIVE = True


def _antiafk_worker_main(cmd_conn, cb_conn, event_queue, initial_config: Optional[dict]) -> None:
    """
    Worker process entrypoint.

    This is defined at module top-level so it is picklable under Windows' spawn
    start method.
    """
    native = None

    def _emit_status(message: Any) -> None:
        try:
            event_queue.put({"type": "status", "message": str(message)})
        except Exception:
            pass

    def _emit_state(running: Any) -> None:
        try:
            event_queue.put({"type": "state", "running": bool(running)})
        except Exception:
            pass

    # Python-side Anti-AFK loop (lets us integrate with BES without rebuilding the native extension).
    _loop_lock = threading.Lock()
    _loop_stop = threading.Event()
    _loop_pause = threading.Event()
    _loop_paused = threading.Event()
    _loop_thread: Optional[threading.Thread] = None
    _shutdown_flag = threading.Event()

    def _loop_is_running() -> bool:
        t = _loop_thread
        return bool(t is not None and t.is_alive())

    def _start_loop() -> None:
        _emit_status("Anti-AFK native engine is not available")

    def _stop_loop() -> None:
        return

    def _pause_loop(wait: bool) -> bool:
        return False

    def _resume_loop() -> bool:
        return False
    try:
        from antiafk_native import AntiAFK as NativeAntiAFK  # type: ignore

        cfg = dict(initial_config or {})
        native = NativeAntiAFK(parent=None, config=cfg)

        native.status_callback = _emit_status
        native.button_state_callback = _emit_state

        cb_lock = threading.Lock()
        cb_req_id = 0
        cb_unmatched: dict[int, Any] = {}

        def _cb_rpc(request: dict, *, timeout_s: float, default: Any) -> Any:
            nonlocal cb_req_id
            try:
                cb_req_id += 1
                req_id = int(cb_req_id)
            except Exception:
                req_id = int(time.monotonic() * 1000) & 0x7FFFFFFF

            msg = dict(request or {})
            msg["req_id"] = req_id

            with cb_lock:
                try:
                    cb_conn.send(msg)
                except Exception:
                    return default

                end = time.monotonic() + float(max(0.0, timeout_s))
                while True:
                    try:
                        if req_id in cb_unmatched:
                            resp = cb_unmatched.pop(req_id)
                        else:
                            remaining = end - time.monotonic()
                            if remaining <= 0.0:
                                return default
                            if not cb_conn.poll(min(0.2, remaining)):
                                continue
                            resp = cb_conn.recv()
                    except Exception:
                        return default

                    if not isinstance(resp, dict):
                        continue

                    rid = resp.get("req_id", None)
                    if rid is None:
                        # Backwards-compat (shouldn't happen once both sides are updated).
                        return resp.get("result", default)
                    try:
                        rid_i = int(rid)
                    except Exception:
                        continue
                    if rid_i != req_id:
                        cb_unmatched[rid_i] = resp
                        if len(cb_unmatched) > 256:
                            cb_unmatched.clear()
                        continue
                    return resp.get("result", default)

        def _is_pid_in_menu(pid: Any):
            try:
                pid_int = int(pid)
            except Exception:
                return None
            return _cb_rpc({"type": "is_pid_in_menu", "pid": pid_int}, timeout_s=1.0, default=None)

        native.is_pid_in_menu_callback = _is_pid_in_menu

        def _bes_hold_unthrottled(pids: Any, seconds: Any) -> bool:
            try:
                pid_list = [int(p) for p in (pids or []) if int(p) > 0]
            except Exception:
                pid_list = []
            if not pid_list:
                return False
            try:
                hold_s = float(seconds)
            except Exception:
                hold_s = 0.0
            res = _cb_rpc(
                {"type": "bes_hold_unthrottled", "pids": pid_list, "seconds": float(max(0.0, hold_s))},
                timeout_s=5.0,
                default=False,
            )
            return bool(res)

        def _bes_release_hold(pids: Any) -> bool:
            try:
                pid_list = [int(p) for p in (pids or []) if int(p) > 0]
            except Exception:
                pid_list = []
            if not pid_list:
                return False
            res = _cb_rpc({"type": "bes_release_hold", "pids": pid_list}, timeout_s=5.0, default=False)
            return bool(res)

        # Win32 helpers for BES integration / foreground restore.
        _GetWindowThreadProcessId = None
        _GetForegroundWindow = None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            _GetWindowThreadProcessId = user32.GetWindowThreadProcessId
            _GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            _GetWindowThreadProcessId.restype = wintypes.DWORD
            _GetForegroundWindow = user32.GetForegroundWindow
            _GetForegroundWindow.argtypes = []
            _GetForegroundWindow.restype = wintypes.HWND
        except Exception:
            _GetWindowThreadProcessId = None
            _GetForegroundWindow = None

        def _hwnd_to_pid(hwnd_int: int) -> Optional[int]:
            if _GetWindowThreadProcessId is None:
                return None
            try:
                pid = wintypes.DWORD(0)
                tid = _GetWindowThreadProcessId(wintypes.HWND(int(hwnd_int)), ctypes.byref(pid))
                if int(tid) == 0:
                    return None
                return int(pid.value) if int(pid.value) > 0 else None
            except Exception:
                return None

        def _get_foreground_hwnd() -> int:
            if _GetForegroundWindow is None:
                return 0
            try:
                hwnd = _GetForegroundWindow()
                return int(getattr(hwnd, "value", hwnd) or 0)
            except Exception:
                return 0

        def _wait_interruptible(seconds: float) -> bool:
            """
            Wait up to `seconds`, returning False if stop/pause/shutdown is requested.
            """
            try:
                total = float(seconds)
            except Exception:
                total = 0.0
            if total <= 0.0:
                return True
            end = time.monotonic() + total
            while time.monotonic() < end:
                if _shutdown_flag.is_set() or _loop_stop.is_set() or _loop_pause.is_set():
                    return False
                time.sleep(min(0.1, max(0.0, end - time.monotonic())))
            return True

        def _antiafk_python_loop() -> None:
            crash = None
            try:
                _emit_status("Anti-AFK loop started")

                cfg0 = getattr(native, "config", {}) or {}
                try:
                    interval = int(cfg0.get("antiafk_interval", 120))
                except Exception:
                    interval = 120
                try:
                    action_type = str(cfg0.get("antiafk_action", "space"))
                except Exception:
                    action_type = "space"
                try:
                    alt_delay_ms = int(cfg0.get("antiafk_alt_delay_ms", 400))
                except Exception:
                    alt_delay_ms = 400
                try:
                    menu_autoreconnect = bool(cfg0.get("antiafk_menu_autoreconnect", False))
                except Exception:
                    menu_autoreconnect = False

                _emit_status(
                    "Settings: Interval="
                    + str(interval)
                    + "s, Action="
                    + str(action_type)
                    + ", AltDelayMs="
                    + str(alt_delay_ms)
                    + ", MenuAutoReconnect="
                    + ("True" if menu_autoreconnect else "False")
                )

                last_action_time = time.monotonic() - max(0.0, float(interval) - 10.0)
                last_no_windows_status = time.monotonic() - 11.0
                pause_started: Optional[float] = None
                any_pacify_fail = False
                last_pacify_fail_log = 0.0

                while not _shutdown_flag.is_set() and not _loop_stop.is_set():
                    if _loop_pause.is_set():
                        if pause_started is None:
                            pause_started = time.monotonic()
                            _loop_paused.set()
                            _emit_status("Anti-AFK paused")

                        while _loop_pause.is_set() and not _shutdown_flag.is_set() and not _loop_stop.is_set():
                            time.sleep(0.1)

                        if _shutdown_flag.is_set() or _loop_stop.is_set():
                            break

                        paused_for = time.monotonic() - float(pause_started)
                        last_action_time += paused_for
                        last_no_windows_status += paused_for
                        pause_started = None
                        _loop_paused.clear()
                        _emit_status("Anti-AFK resumed")

                    cfg = getattr(native, "config", {}) or {}
                    try:
                        interval = int(cfg.get("antiafk_interval", interval))
                    except Exception:
                        pass
                    try:
                        action_type = str(cfg.get("antiafk_action", action_type))
                    except Exception:
                        pass
                    try:
                        menu_autoreconnect = bool(cfg.get("antiafk_menu_autoreconnect", menu_autoreconnect))
                    except Exception:
                        pass

                    now = time.monotonic()
                    if (now - last_action_time) < float(max(1, interval)):
                        _wait_interruptible(1.0)
                        continue

                    try:
                        windows = list(native.find_roblox_windows(True))
                    except Exception:
                        windows = []

                    if not windows:
                        if (now - last_no_windows_status) >= 10.0:
                            _emit_status("No Roblox windows found, waiting...")
                            last_no_windows_status = now
                        _wait_interruptible(5.0)
                        continue

                    _emit_status(f"Performing Anti-AFK action on {len(windows)} Roblox window(s)")
                    action_success = True
                    old_hwnd = _get_foreground_hwnd()

                    # BES unthrottle integration (optional).
                    try:
                        unthrottle_enabled = bool(cfg.get("antiafk_unthrottle_enabled", False))
                    except Exception:
                        unthrottle_enabled = False
                    try:
                        batch_size = int(cfg.get("antiafk_unthrottle_batch_size", 5) or 5)
                    except Exception:
                        batch_size = 5
                    try:
                        lead_s = float(cfg.get("antiafk_unthrottle_lead_s", 0.0) or 0.0)
                    except Exception:
                        lead_s = 0.0
                    batch_size = max(1, batch_size)
                    lead_s = max(0.0, lead_s)

                    for i in range(0, len(windows), batch_size):
                        if _shutdown_flag.is_set() or _loop_stop.is_set() or _loop_pause.is_set():
                            break
                        batch = windows[i : i + batch_size]

                        held_pids = []
                        try:
                            if unthrottle_enabled:
                                seen = set()
                                for hwnd_int in batch:
                                    pid = _hwnd_to_pid(int(hwnd_int))
                                    if pid is None or pid in seen:
                                        continue
                                    seen.add(pid)
                                    held_pids.append(pid)

                                if held_pids:
                                    hold_s = lead_s + 120.0
                                    hold_ok = False
                                    try:
                                        hold_ok = bool(_bes_hold_unthrottled(held_pids, hold_s))
                                    except Exception:
                                        hold_ok = False
                                    if not hold_ok:
                                        any_pacify_fail = True
                                        if (time.monotonic() - float(last_pacify_fail_log)) >= 30.0:
                                            last_pacify_fail_log = time.monotonic()
                                            _emit_status(
                                                "BES pacify: failed to request unthrottle for this batch; inputs may be throttled."
                                            )

                                    # Always yield a tiny amount after a successful hold so the BES scheduler
                                    # can process the wake (even when lead time is 0).
                                    if hold_ok:
                                        apply_wait_s = max(float(lead_s), 0.25)
                                        if apply_wait_s > 0.0:
                                            if not _wait_interruptible(apply_wait_s):
                                                break

                            for hwnd_int in batch:
                                if _shutdown_flag.is_set() or _loop_stop.is_set() or _loop_pause.is_set():
                                    break
                                try:
                                    try:
                                        ok = bool(native.perform_antiafk_action(int(hwnd_int), action_type, False))
                                    except TypeError:
                                        ok = bool(native.perform_antiafk_action(int(hwnd_int), action_type))
                                except Exception:
                                    ok = False
                                if not ok:
                                    action_success = False
                        finally:
                            if held_pids:
                                try:
                                    _bes_release_hold(held_pids)
                                except Exception:
                                    pass

                    if not (_shutdown_flag.is_set() or _loop_stop.is_set() or _loop_pause.is_set()):
                        try:
                            if old_hwnd:
                                native.restore_foreground_window(int(old_hwnd))
                        except Exception:
                            pass

                    if _loop_pause.is_set():
                        continue

                    if action_success:
                        last_action_time = time.monotonic()
                        _emit_status("Anti-AFK action completed successfully")
                        _wait_interruptible(0.5)
                    else:
                        _emit_status("Anti-AFK action failed, will retry on next cycle")
            except Exception:
                crash = traceback.format_exc()

            if crash:
                _emit_status("Anti-AFK loop crashed:\n" + crash)
            else:
                _emit_status("Anti-AFK loop ended")

            try:
                _loop_paused.clear()
            except Exception:
                pass

            try:
                _emit_state(False)
            except Exception:
                pass
            if any_pacify_fail:
                try:
                    _emit_status("Note: Some BES pacify requests failed or timed out; some windows may stay throttled.")
                except Exception:
                    pass

        def _start_loop() -> None:
            nonlocal _loop_thread
            if _shutdown_flag.is_set():
                _emit_status("Anti-AFK is shut down")
                return
            # Ensure the native loop isn't also running.
            try:
                if native is not None and bool(getattr(native, "antiafk_running", False)):
                    native.stop_antiafk()
            except Exception:
                pass
            with _loop_lock:
                if _loop_is_running():
                    _emit_status("Anti-AFK is already running")
                    return
                _loop_stop.clear()
                _loop_pause.clear()
                _loop_paused.clear()
                _loop_thread = threading.Thread(target=_antiafk_python_loop, name="AntiAFKPythonLoop", daemon=True)
                _loop_thread.start()
            _emit_status("Anti-AFK started")
            _emit_state(True)

        def _stop_loop() -> None:
            nonlocal _loop_thread
            with _loop_lock:
                running = _loop_is_running()
                if running:
                    _loop_stop.set()
                    _loop_pause.clear()
                    _loop_paused.clear()
                t = _loop_thread
            native_was_running = False
            try:
                native_was_running = bool(native is not None and bool(getattr(native, "antiafk_running", False)))
            except Exception:
                native_was_running = False
            try:
                if running and t is not None:
                    t.join(timeout=10.0)
            except Exception:
                pass
            with _loop_lock:
                _loop_thread = None
            # Make sure any native loop is also stopped (defensive).
            try:
                if native_was_running and native is not None:
                    native.stop_antiafk()
            except Exception:
                pass
            if running or native_was_running:
                _emit_status("Anti-AFK stopped")
                _emit_state(False)
            else:
                _emit_status("Anti-AFK is not running")
                _emit_state(False)

        def _pause_loop(wait: bool) -> bool:
            if _loop_is_running():
                _loop_pause.set()
                if not wait:
                    return True
                try:
                    return bool(_loop_paused.wait(timeout=30.0))
                except Exception:
                    return False
            try:
                if native is not None:
                    return bool(native.pause_antiafk(bool(wait)))
            except Exception:
                return False
            return False

        def _resume_loop() -> bool:
            if _loop_is_running():
                _loop_pause.clear()
                return True
            try:
                if native is not None:
                    return bool(native.resume_antiafk())
            except Exception:
                return False
            return False

        try:
            native.update_button_states()
        except Exception:
            pass
    except Exception as e:
        # Keep the worker alive so the host can receive structured errors.
        try:
            event_queue.put({"type": "status", "message": f"Failed to start AntiAFK native engine: {e}"})
        except Exception:
            pass

    try:
        while True:
            try:
                msg = cmd_conn.recv()
            except EOFError:
                break

            if not isinstance(msg, dict):
                continue

            req_id = msg.get("id")
            cmd = msg.get("cmd")

            def _reply(ok: bool, result: Any = None, error: Optional[str] = None) -> None:
                try:
                    cmd_conn.send({"id": req_id, "ok": ok, "result": result, "error": error})
                except Exception:
                    pass

            try:
                if cmd == "get_config":
                    if native is None:
                        _reply(True, dict(initial_config or {}))
                    else:
                        _reply(True, dict(getattr(native, "config", {}) or {}))
                    continue

                if cmd == "get_running":
                    running = bool(_loop_is_running())
                    if not running and native is not None:
                        try:
                            running = bool(getattr(native, "antiafk_running", False))
                        except Exception:
                            running = False
                    _reply(True, bool(running))
                    continue

                if cmd == "update_config":
                    if native is None:
                        raise RuntimeError("antiafk_native is not available in the worker process")
                    updates = msg.get("updates") or {}
                    if isinstance(updates, dict):
                        try:
                            for k, v in dict(updates).items():
                                try:
                                    native.config[str(k)] = v
                                except Exception:
                                    continue
                        except Exception:
                            pass
                    _reply(True, True)
                    continue

                if cmd == "shutdown":
                    try:
                        _shutdown_flag.set()
                    except Exception:
                        pass
                    try:
                        _stop_loop()
                    except Exception:
                        pass
                    if native is not None:
                        try:
                            native.shutdown()
                        except Exception:
                            pass
                    _reply(True, True)
                    break

                if cmd == "call":
                    if native is None:
                        raise RuntimeError("antiafk_native is not available in the worker process")
                    name = msg.get("name")
                    args = msg.get("args") or []
                    kwargs = msg.get("kwargs") or {}
                    name_s = str(name)

                    # Only use the Python loop when BES unthrottle integration is enabled (or already running).
                    use_py_loop = bool(_loop_is_running())
                    if not use_py_loop:
                        try:
                            cfg0 = getattr(native, "config", {}) or {}
                            use_py_loop = bool(cfg0.get("antiafk_unthrottle_enabled", False))
                        except Exception:
                            use_py_loop = False

                    if name_s == "start_antiafk" and use_py_loop:
                        _start_loop()
                        _reply(True, None)
                        continue
                    if name_s == "stop_antiafk" and use_py_loop:
                        _stop_loop()
                        _reply(True, None)
                        continue
                    if name_s == "toggle_antiafk" and use_py_loop:
                        if not args:
                            raise ValueError("toggle_antiafk requires an explicit enable state")
                        enable = bool(args[0])
                        try:
                            native.config["antiafk_enabled"] = bool(enable)
                        except Exception:
                            pass
                        if enable:
                            _start_loop()
                        else:
                            _stop_loop()
                        _reply(True, None)
                        continue
                    if name_s == "pause_antiafk" and use_py_loop:
                        wait = True
                        if args:
                            wait = bool(args[0])
                        elif isinstance(kwargs, dict) and "wait" in kwargs:
                            wait = bool(kwargs.get("wait", True))
                        _reply(True, bool(_pause_loop(bool(wait))))
                        continue
                    if name_s == "resume_antiafk" and use_py_loop:
                        _reply(True, bool(_resume_loop()))
                        continue

                    fn = getattr(native, name_s)
                    result = fn(*args, **kwargs)
                    _reply(True, result)
                    continue

                raise RuntimeError(f"Unknown command: {cmd}")
            except Exception:
                tb = traceback.format_exc()
                try:
                    event_queue.put({"type": "status", "message": "AntiAFK worker error:\n" + tb})
                except Exception:
                    pass
                _reply(False, None, tb)
    finally:
        if native is not None:
            try:
                native.shutdown()
            except Exception:
                pass
        try:
            cmd_conn.close()
        except Exception:
            pass
        try:
            cb_conn.close()
        except Exception:
            pass


class AntiAFK:
    """
    Host-side proxy that forwards calls to a worker process.

    Public attributes (for compatibility with the native class):
      - config (dict)
      - status_callback (callable | None)
      - button_state_callback (callable | None)
      - is_pid_in_menu_callback (callable | None)
      - bes_hold_unthrottled_callback (callable | None)
      - bes_release_hold_callback (callable | None)
      - antiafk_running (bool property)
    """

    def __init__(self, parent: Any = None, config: Optional[dict] = None) -> None:
        self._parent = parent  # kept for signature compatibility; not sent to worker

        self.status_callback = None
        self.button_state_callback = None
        self.is_pid_in_menu_callback = None
        self.bes_hold_unthrottled_callback = None
        self.bes_release_hold_callback = None

        self.config: dict = dict(config or {})
        self._running: bool = False

        self._ctx = mp.get_context("spawn")
        self._cmd_conn, cmd_child = self._ctx.Pipe(duplex=True)
        self._cb_conn, cb_child = self._ctx.Pipe(duplex=True)
        self._event_queue = self._ctx.Queue()

        self._cmd_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._proc = self._ctx.Process(
            target=_antiafk_worker_main,
            args=(cmd_child, cb_child, self._event_queue, dict(self.config)),
            name="AntiAFKWorker",
            daemon=True,
        )
        self._proc.start()

        self._cb_thread = threading.Thread(target=self._cb_loop, name="AntiAFKCallbackLoop", daemon=True)
        self._cb_thread.start()

        self._event_thread = threading.Thread(target=self._event_loop, name="AntiAFKEventLoop", daemon=True)
        self._event_thread.start()

        # Sync effective config + initial running state from the worker.
        try:
            cfg = self._rpc("get_config")
            if isinstance(cfg, dict):
                self.config = cfg
        except Exception:
            pass
        try:
            self._running = bool(self._rpc("get_running"))
        except Exception:
            self._running = False

    @property
    def antiafk_running(self) -> bool:
        return bool(self._running and self._proc is not None and self._proc.is_alive())

    def _rpc(self, cmd: str, **payload: Any) -> Any:
        if self._shutdown_event.is_set():
            raise RuntimeError("AntiAFK is shut down")
        if not self._proc.is_alive():
            raise RuntimeError("AntiAFK worker process is not running")

        with self._cmd_lock:
            self._cmd_conn.send({"id": 0, "cmd": cmd, **payload})
            resp = self._cmd_conn.recv()

        if not isinstance(resp, dict) or not resp.get("ok", False):
            err = None
            if isinstance(resp, dict):
                err = resp.get("error")
            raise RuntimeError(err or "AntiAFK worker call failed")

        return resp.get("result")

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self._rpc("call", name=name, args=list(args), kwargs=kwargs)

    def _cb_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                if not self._cb_conn.poll(0.2):
                    continue
                msg = self._cb_conn.recv()
            except (EOFError, OSError):
                break
            except Exception:
                continue

            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")
            result = None

            if mtype == "is_pid_in_menu":
                pid = msg.get("pid")
                cb = getattr(self, "is_pid_in_menu_callback", None)
                if callable(cb):
                    try:
                        result = cb(pid)
                    except Exception:
                        result = None
            elif mtype == "bes_hold_unthrottled":
                pids = msg.get("pids") or []
                seconds = msg.get("seconds", 0.0)
                cb = getattr(self, "bes_hold_unthrottled_callback", None)
                if callable(cb):
                    try:
                        result = cb(pids, seconds)
                    except Exception:
                        result = None
            elif mtype == "bes_release_hold":
                pids = msg.get("pids") or []
                cb = getattr(self, "bes_release_hold_callback", None)
                if callable(cb):
                    try:
                        result = cb(pids)
                    except Exception:
                        result = None
            else:
                continue

            try:
                resp = {"result": result}
                if "req_id" in msg:
                    resp["req_id"] = msg.get("req_id")
                self._cb_conn.send(resp)
            except Exception:
                pass

    def _event_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                evt = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            except Exception:
                continue

            if not isinstance(evt, dict):
                continue

            etype = evt.get("type")
            if etype == "status":
                msg = str(evt.get("message", ""))
                cb = getattr(self, "status_callback", None)
                if callable(cb):
                    try:
                        cb(msg)
                    except Exception:
                        pass
            elif etype == "state":
                running = bool(evt.get("running", False))
                self._running = running
                cb = getattr(self, "button_state_callback", None)
                if callable(cb):
                    try:
                        cb(running)
                    except Exception:
                        pass

    # ---- Native-compatible API (subset + commonly used helpers) ----

    def apply_host_config(
        self,
        *,
        multi_instance_enabled: Optional[bool] = None,
        interval: Optional[int] = None,
        action: Optional[str] = None,
        alt_delay_ms: Optional[int] = None,
        menu_autoreconnect: Optional[bool] = None,
        unthrottle_enabled: Optional[bool] = None,
        unthrottle_batch_size: Optional[int] = None,
        unthrottle_lead_s: Optional[float] = None,
    ) -> None:
        kwargs = {}
        extra_updates = {}
        if multi_instance_enabled is not None:
            kwargs["multi_instance_enabled"] = bool(multi_instance_enabled)
            self.config["multi_instance_enabled"] = bool(multi_instance_enabled)
        if interval is not None:
            kwargs["interval"] = int(interval)
            self.config["antiafk_interval"] = int(interval)
        if action is not None:
            kwargs["action"] = str(action)
            self.config["antiafk_action"] = str(action)
        if alt_delay_ms is not None:
            kwargs["alt_delay_ms"] = int(alt_delay_ms)
            self.config["antiafk_alt_delay_ms"] = int(alt_delay_ms)
        if menu_autoreconnect is not None:
            kwargs["menu_autoreconnect"] = bool(menu_autoreconnect)
            self.config["antiafk_menu_autoreconnect"] = bool(menu_autoreconnect)
        if unthrottle_enabled is not None:
            v = bool(unthrottle_enabled)
            extra_updates["antiafk_unthrottle_enabled"] = v
            self.config["antiafk_unthrottle_enabled"] = v
        if unthrottle_batch_size is not None:
            try:
                v = max(1, int(unthrottle_batch_size))
            except Exception:
                v = 5
            extra_updates["antiafk_unthrottle_batch_size"] = v
            self.config["antiafk_unthrottle_batch_size"] = v
        if unthrottle_lead_s is not None:
            try:
                v = max(0.0, float(unthrottle_lead_s))
            except Exception:
                v = 0.0
            extra_updates["antiafk_unthrottle_lead_s"] = v
            self.config["antiafk_unthrottle_lead_s"] = v

        # Feature removals: keep host config clean.
        self.config.pop("antiafk_user_safe", None)
        self.config.pop("antiafk_sequential_mode", None)
        self.config.pop("antiafk_sequential_delay", None)

        self._call("apply_host_config", **kwargs)
        if extra_updates:
            try:
                self._rpc("update_config", updates=extra_updates)
            except Exception:
                pass

    def toggle_antiafk(self, enable: Any = None) -> None:
        if enable is None:
            raise ValueError("toggle_antiafk requires an explicit enable state")
        self.config["antiafk_enabled"] = bool(enable)
        self._call("toggle_antiafk", bool(enable))
        try:
            self._running = bool(self._rpc("get_running"))
        except Exception:
            self._running = False

    def start_antiafk(self) -> None:
        self._call("start_antiafk")
        try:
            self._running = bool(self._rpc("get_running"))
        except Exception:
            self._running = False

    def stop_antiafk(self) -> None:
        self._call("stop_antiafk")
        try:
            self._running = bool(self._rpc("get_running"))
        except Exception:
            self._running = False

    def pause_antiafk(self, wait: bool = True) -> bool:
        return bool(self._call("pause_antiafk", bool(wait)))

    def resume_antiafk(self) -> bool:
        return bool(self._call("resume_antiafk"))

    def shutdown(self) -> None:
        if self._shutdown_event.is_set():
            return
        try:
            if self._proc.is_alive():
                try:
                    self._rpc("shutdown")
                except Exception:
                    pass
        finally:
            self._shutdown_event.set()
            try:
                if self._proc.is_alive():
                    self._proc.join(timeout=2.0)
            except Exception:
                pass
            try:
                if self._proc.is_alive():
                    self._proc.terminate()
            except Exception:
                pass
            try:
                self._cmd_conn.close()
            except Exception:
                pass
            try:
                self._cb_conn.close()
            except Exception:
                pass

    def find_roblox_windows(self, include_hidden: bool = True):
        return self._call("find_roblox_windows", bool(include_hidden))

    def show_roblox_windows(self) -> None:
        self._call("show_roblox_windows")

    def hide_roblox_windows(self) -> None:
        self._call("hide_roblox_windows")

    def perform_antiafk_action(self, hwnd: int, action_type: Optional[str] = None) -> bool:
        return bool(self._call("perform_antiafk_action", int(hwnd), action_type))

    def test_action(self) -> None:
        self._call("test_action")

    def test_action_with_delay(self) -> None:
        self._call("test_action_with_delay")

    def is_window_fullscreen(self, hwnd: int) -> bool:
        return bool(self._call("is_window_fullscreen", int(hwnd)))

    def restore_foreground_window(self, hwnd: int) -> None:
        self._call("restore_foreground_window", int(hwnd))

    def enable_multi_instance(self) -> bool:
        return bool(self._call("enable_multi_instance"))

    def disable_multi_instance(self) -> bool:
        return bool(self._call("disable_multi_instance"))

    def toggle_multi_instance(self) -> None:
        self._call("toggle_multi_instance")

    def update_status(self, message: str) -> None:
        self._call("update_status", str(message))

    def update_button_states(self) -> None:
        self._call("update_button_states")

    def log_error(self, exception: Any, message: Any = None) -> None:
        # Mirror the native signature where message is optional.
        if message is None:
            self._call("log_error", exception)
        else:
            self._call("log_error", exception, message)

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.shutdown()
        except Exception:
            pass


__all__ = [
    "AntiAFK",
    "_USING_NATIVE",
]
