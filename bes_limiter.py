"""
bes_limiter.py

Thin wrapper that prefers the native pybind11 extension (`bes_limiter_native`) when
available, and falls back to the pure-Python implementation (`bes_limiter_py`).
"""

from __future__ import annotations

import sys
from pathlib import Path

_native_dir = Path(__file__).resolve().parent / "native"
if _native_dir.is_dir():
    native_s = str(_native_dir)
    if native_s not in sys.path:
        sys.path.insert(0, native_s)

try:
    from bes_limiter_native import (  # type: ignore
        BESLimiterWorker,
        BESMultiProcessController,
        list_thread_ids,
        list_thread_ids_for_pids,
    )

    _USING_NATIVE = True
except Exception:  # pragma: no cover
    _legacy_dir = Path(__file__).resolve().parent / "Legacy"
    if _legacy_dir.is_dir():
        legacy_s = str(_legacy_dir)
        if legacy_s not in sys.path:
            sys.path.insert(0, legacy_s)
    from bes_limiter_py import (  # type: ignore
        BESLimiterWorker,
        BESMultiProcessController,
        list_thread_ids,
        list_thread_ids_for_pids,
    )

    _USING_NATIVE = False

__all__ = [
    "BESLimiterWorker",
    "BESMultiProcessController",
    "list_thread_ids",
    "list_thread_ids_for_pids",
    "_USING_NATIVE",
]
