## Build native modules (pybind11)

Prereqs (Windows):
- Python (same interpreter you run `gui.py` with)
- Visual Studio Build Tools (MSVC + Windows SDK)

From the repo root:
1. Install build deps: `python -m pip install -U pip pybind11 setuptools`
2. Build in-place (Move `bes_limiter_native*.pyd` and `antiafk_native*.pyd` next to `bes_limiter.py` and
   `antiafk.py`):
   - `python setup.py build_ext --inplace`

If you prefer installing into the interpreter instead:
- `python -m pip install .`
