## Build native modules (pybind11)

Prereqs (Windows):
- Python (the exact interpreter you use to run `gui.py`)
- Visual Studio Build Tools (MSVC + Windows SDK)

Native extensions are tied to a CPython ABI. For example, Python 3.14 loads a
file ending in `.cp314-win_amd64.pyd`; a `.cp312-win_amd64.pyd` build cannot be
reused or safely renamed.

From `JARAM/native`:
1. Confirm the interpreter: `python -VV`
2. Install build deps: `python -m pip install -U pip "pybind11>=3.0" "setuptools>=77"`
3. Build in-place (the wrappers load native modules from this `native/` directory):
   - `python setup.py build_ext --inplace`
4. Verify the resulting names match the interpreter, then test the imports:
   - `python -c "import antiafk_native, bes_limiter_native, ram_limiter_native; print('Native modules OK')"`

If you prefer installing into the interpreter instead:
- `python -m pip install .`
