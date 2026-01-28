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

    def _emit_touch(pid: Any) -> None:
        try:
            event_queue.put({"type": "touch", "pid": int(pid)})
        except Exception:
            pass

    def _emit_pre_action(seconds_until_action: Any) -> None:
        try:
            event_queue.put({"type": "pre_action", "seconds": float(seconds_until_action)})
        except Exception:
            pass

    # Anti-AFK scheduling (including BES "Throttle Pacify") runs in the native engine (C++).
    try:
        # Prefer the in-repo native build output (./native) when available.
        try:
            import sys
            from pathlib import Path

            native_dir = Path(__file__).resolve().parent / "native"
            if native_dir.is_dir():
                sys.path.insert(0, str(native_dir))
        except Exception:
            pass

        from antiafk_native import AntiAFK as NativeAntiAFK  # type: ignore

        cfg = dict(initial_config or {})
        native = NativeAntiAFK(parent=None, config=cfg)

        native.status_callback = _emit_status
        native.button_state_callback = _emit_state
        try:
            native.touch_callback = _emit_touch
        except Exception:
            pass
        try:
            native.pre_action_callback = _emit_pre_action
        except Exception:
            pass

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

        try:
            native.bes_hold_unthrottled_callback = _bes_hold_unthrottled
            native.bes_release_hold_callback = _bes_release_hold
        except Exception:
            pass

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
                    running = False
                    if native is not None:
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
      - touch_callback (callable | None)
      - pre_action_callback (callable | None)
      - is_pid_in_menu_callback (callable | None)
      - bes_hold_unthrottled_callback (callable | None)
      - bes_release_hold_callback (callable | None)
      - antiafk_running (bool property)
    """

    def __init__(self, parent: Any = None, config: Optional[dict] = None) -> None:
        self._parent = parent  # kept for signature compatibility; not sent to worker

        self.status_callback = None
        self.button_state_callback = None
        self.touch_callback = None
        self.pre_action_callback = None
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
            elif etype == "touch":
                pid = evt.get("pid", None)
                cb = getattr(self, "touch_callback", None)
                if callable(cb):
                    try:
                        cb(pid)
                    except Exception:
                        pass
            elif etype == "pre_action":
                seconds = evt.get("seconds", 0.0)
                cb = getattr(self, "pre_action_callback", None)
                if callable(cb):
                    try:
                        cb(seconds)
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
        alert_sound_enabled: Optional[bool] = None,
        alert_tooltip_enabled: Optional[bool] = None,
        alert_lead_s: Optional[float] = None,
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
        if alert_sound_enabled is not None:
            v = bool(alert_sound_enabled)
            extra_updates["antiafk_alert_sound"] = v
            self.config["antiafk_alert_sound"] = v
        if alert_tooltip_enabled is not None:
            v = bool(alert_tooltip_enabled)
            extra_updates["antiafk_alert_tooltip"] = v
            self.config["antiafk_alert_tooltip"] = v
        if alert_lead_s is not None:
            try:
                v = max(0.0, float(alert_lead_s))
            except Exception:
                v = 0.0
            extra_updates["antiafk_alert_lead_s"] = v
            self.config["antiafk_alert_lead_s"] = v
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

    def touch_pids(self, pids) -> None:
        try:
            pid_list = [int(p) for p in (pids or []) if int(p) > 0]
        except Exception:
            pid_list = []
        if not pid_list:
            return
        self._call("touch_pids", pid_list)

    def set_pids_disconnected(self, pids, disconnected: bool = True) -> None:
        try:
            pid_list = [int(p) for p in (pids or []) if int(p) > 0]
        except Exception:
            pid_list = []
        if not pid_list:
            return
        self._call("set_pids_disconnected", pid_list, bool(disconnected))

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
