"""
bes_limiter.py

Thin wrapper that prefers the native pybind11 extension (`bes_limiter_native`) when
available, and falls back to the pure-Python implementation (`bes_limiter_py`).
"""

from __future__ import annotations

try:
    from bes_limiter_native import (  # type: ignore
        BESLimiterWorker,
        BESMultiProcessController,
        list_thread_ids,
        list_thread_ids_for_pids,
    )

    _USING_NATIVE = True
except Exception:  # pragma: no cover
    print("error")
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

