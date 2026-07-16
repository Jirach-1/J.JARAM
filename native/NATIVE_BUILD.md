## Build native modules (pybind11)

Prereqs (Windows):
- Python (same interpreter you run `gui.py` with)
- Visual Studio Build Tools (MSVC + Windows SDK)

From `JARAM-1.3-dev-DML/native`:
1. Install build deps: `python -m pip install -U pip pybind11 setuptools`
2. Build in-place (the wrappers load native modules from this `native/` directory):
   - `python setup.py build_ext --inplace`

If you prefer installing into the interpreter instead:
- `python -m pip install .`
