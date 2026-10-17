"""Lazy WGC service. Native COM/D3D resources stay on a dedicated MTA thread."""
from __future__ import annotations

import atexit
from concurrent.futures import Future
import math
from pathlib import Path
import queue
import sys
import threading

from PIL import Image

_lock = threading.Lock()
_requests: queue.Queue | None = None
_thread: threading.Thread | None = None
_policy = (0, 1.0)  # (active windows, expected seconds between visits)


def configure_wgc_captures(active_windows: int, batch_size: int, batch_delay: float) -> None:
    """Set the active working set and expected revisit time for session retention."""
    global _policy
    count = max(0, min(252, int(active_windows)))
    batch = max(1, int(batch_size))
    try:
        delay = float(batch_delay)
    except (TypeError, ValueError):
        delay = 1.0
    if not math.isfinite(delay):
        delay = 1.0
    revisit = math.ceil(count / batch) * max(0.5, delay)
    with _lock:
        _policy = (count, revisit)


def _load_native():
    # Windows keeps a loaded .pyd locked. Prefer the staged runtime build so a
    # new JARAM process can use updated native code without stopping the old one.
    native_dir = Path(__file__).resolve().parent / "native"
    runtime_dir = native_dir / "runtime"
    for directory in (native_dir, runtime_dir):
        if str(directory) in sys.path:
            sys.path.remove(str(directory))
        if directory.is_dir():
            sys.path.insert(0, str(directory))
    try:
        import wgc_capture_native
    except ImportError as exc:
        raise RuntimeError(
            "Windows Graphics Capture requires wgc_capture_native for this Python version. "
            "Build JARAM/native with 'python setup.py build_ext --inplace', "
            "or use a JARAM build that includes WGC."
        ) from exc
    return wgc_capture_native


def _serve(requests: queue.Queue) -> None:
    engine = None
    applied_policy = None
    while True:
        with _lock:
            active, revisit = _policy
        idle_timeout = max(15.0, min(3600.0, 2 * revisit + 5))
        try:
            request = requests.get(timeout=idle_timeout)
        except queue.Empty:
            # Keep sessions through the configured revisit period, then release.
            engine = None
            applied_policy = None
            continue
        if request is None:
            engine = None
            return
        hwnd, future = request
        if not future.set_running_or_notify_cancel():
            continue
        if hwnd is None:
            engine = None
            applied_policy = None
            future.set_result(None)
            continue
        try:
            if engine is None:
                engine = _load_native().Capturer()
            with _lock:
                policy = _policy
            if policy != applied_policy:
                engine.configure(*policy)
                applied_policy = policy
            future.set_result(engine.grab(hwnd, 750))
        except Exception as exc:
            if "WGC device lost:" in str(exc):
                engine = None
                applied_policy = None
            future.set_exception(RuntimeError(f"Windows Graphics Capture: {exc}"))


def capture_window_wgc(hwnd: int) -> Image.Image:
    global _requests, _thread
    future = Future()
    with _lock:
        if _thread is None or not _thread.is_alive():
            _requests = queue.Queue()
            _thread = threading.Thread(target=_serve, args=(_requests,), name="wgc-capture", daemon=True)
            _thread.start()
        _requests.put((int(hwnd), future))
    width, height, data, x, y, outer_width, outer_height = future.result()
    image = Image.frombytes("RGB", (width, height), data, "raw", "BGRX")
    if (width, height) != (outer_width, outer_height) or x or y:
        aligned = Image.new("RGB", (outer_width, outer_height))
        aligned.paste(image, (x, y))
        image.close()
        image = aligned
    return image


def release_wgc_captures() -> None:
    """Queue cleanup on the owning thread; safe during mode changes/worker stop."""
    with _lock:
        if _requests is not None and _thread is not None and _thread.is_alive():
            _requests.put((None, Future()))


def _shutdown() -> None:
    with _lock:
        thread = _thread
        if _requests is not None and thread is not None and thread.is_alive():
            _requests.put(None)
    if thread is not None:
        thread.join(timeout=2.0)


atexit.register(_shutdown)
