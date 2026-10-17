## Build native modules (pybind11)

Prereqs (Windows):
- Python (the exact interpreter you use to run `gui.py`)
- Visual Studio Build Tools (MSVC + Windows SDK)
- WGC uses C++20 and C++/WinRT headers from a recent Windows SDK (tested with 10.0.26100.0).

Native extensions are tied to a CPython ABI. For example, Python 3.14 loads a
file ending in `.cp314-win_amd64.pyd`; a `.cp312-win_amd64.pyd` build cannot be
reused or safely renamed.

From `JARAM/native`:
1. Confirm the interpreter: `python -VV`
2. Install build deps: `python -m pip install -U pip "pybind11>=3.0" "setuptools>=77"`
3. Build in-place (the wrappers load native modules from this `native/` directory):
   - `python setup.py build_ext --inplace`
4. Verify the resulting names match the interpreter, then test the imports:
   - `python -c "import antiafk_native, bes_limiter_native, ram_limiter_native, wgc_capture_native; print('Native modules OK')"`

If you prefer installing into the interpreter instead:
- `python -m pip install .`

The OCR tab's Windows Graphics Capture mode requires Windows 10 1903 or newer.
The capture service loads `wgc_capture_native` lazily; PrintWindow mode still
works if that extension is absent. The Nuitka build script builds and includes
the extension automatically. WGC keeps an active working set of up to 256
window sessions within an estimated 1 GiB capture-memory budget, and releases
idle resources according to the expected batch revisit time. It does not fall
back to PrintWindow when capture fails.

When JARAM is running, Windows locks its loaded `.pyd`. To stage a new WGC build
without stopping that process, use `python setup.py build_ext --build-lib runtime`
from `JARAM/native`. New source launches prefer `native/runtime` over the older
in-place binary. The Nuitka build script uses the staged runtime build.
