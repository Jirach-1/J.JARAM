from __future__ import annotations

from pathlib import Path
from setuptools import setup

try:
    from pybind11.setup_helpers import Pybind11Extension, build_ext
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "pybind11 is required to build the native BES limiter. Install it first:\n"
        "  python -m pip install pybind11\n"
        f"Original import error: {e}"
    )


BASE_DIR = Path(__file__).resolve().parent

ext_modules = [
    Pybind11Extension(
        "bes_limiter_native",
        [str(BASE_DIR / "bes_limiter_native.cpp")],
        cxx_std=17,
        libraries=["winmm"],
    ),
    Pybind11Extension(
        "ram_limiter_native",
        [str(BASE_DIR / "ram_limiter_native.cpp")],
        cxx_std=17,
        libraries=["psapi"],
    ),
    Pybind11Extension(
        "antiafk_native",
        [str(BASE_DIR / "antiafk_native.cpp")],
        cxx_std=17,
        libraries=["user32"],
    ),
]


setup(
    name="bes_limiter_native",
    version="0.0.0",
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
    zip_safe=False,
)
