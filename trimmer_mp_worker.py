from __future__ import annotations

import queue
import time
import traceback
from typing import Any, Optional


TARGET_PROCESS_NAME = "RobloxPlayerBeta.exe"


def _safe_put(log_queue: Any, item: Any) -> None:
    try:
        log_queue.put(item)
    except Exception:
        pass


def run_trimmer_worker(config_queue: Any, log_queue: Any, stop_event: Any) -> None:
    enabled: bool = False
    interval_s: float = 15.0
    threshold_mb: Optional[float] = None

    def drain_config_queue() -> None:
        nonlocal enabled, interval_s, threshold_mb
        try:
            while True:
                msg = config_queue.get_nowait()
                if msg is None:
                    try:
                        stop_event.set()
                    except Exception:
                        pass
                    return
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") != "config":
                    continue

                enabled = bool(msg.get("enabled", enabled))

                try:
                    interval_s = float(msg.get("interval_s", interval_s))
                except Exception:
                    pass

                raw_threshold = msg.get("threshold_mb", threshold_mb)
                if raw_threshold is None:
                    threshold_mb = None
                else:
                    try:
                        threshold_mb = float(raw_threshold)
                    except Exception:
                        pass
        except queue.Empty:
            return
        except Exception:
            return

    try:
        from ram_limiter_native import trim_targets as native_trim_targets  # type: ignore
    except Exception:
        _safe_put(
            log_queue,
            "[ERROR] Native RAM trimmer module `ram_limiter_native` is not available. "
            "Build it from `native/` and place the `.pyd` next to `trimmer.py`.",
        )
        _safe_put(log_queue, None)
        return

    _safe_put(log_queue, "[INFO] Trimmer worker process started.")
    try:
        while True:
            if stop_event.is_set():
                break

            drain_config_queue()
            if stop_event.is_set():
                break

            if not enabled:
                try:
                    stop_event.wait(0.2)
                except Exception:
                    time.sleep(0.2)
                continue

            try:
                for line in native_trim_targets(TARGET_PROCESS_NAME, threshold_mb):
                    _safe_put(log_queue, str(line))
            except Exception as e:
                _safe_put(log_queue, f"[ERROR] Native RAM trimmer failed: {e!r}")

            sleep_s = max(0.1, float(interval_s))
            end_time = time.time() + sleep_s
            while time.time() < end_time and not stop_event.is_set():
                drain_config_queue()
                if stop_event.is_set() or not enabled:
                    break
                try:
                    remaining = max(0.0, end_time - time.time())
                    stop_event.wait(min(0.2, remaining))
                except Exception:
                    time.sleep(min(0.2, max(0.0, end_time - time.time())))
    except Exception:
        _safe_put(log_queue, "[ERROR] Trimmer worker process crashed:")
        _safe_put(log_queue, traceback.format_exc())
    finally:
        _safe_put(log_queue, "[INFO] Trimmer worker process stopped.")
        _safe_put(log_queue, None)
