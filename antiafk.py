"""
antiafk.py

Thin wrapper that prefers the native pybind11 extension (`antiafk_native`) when
available, and falls back to the pure-Python implementation (`antiafk_py`).
"""

from __future__ import annotations

try:
    from antiafk_native import AntiAFK  # type: ignore

    _USING_NATIVE = True
except Exception:  # pragma: no cover
    from antiafk_py import AntiAFK  # type: ignore

    _USING_NATIVE = False

__all__ = [
    "AntiAFK",
    "_USING_NATIVE",
]

