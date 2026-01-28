from __future__ import annotations

import io
import json
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from multiprocessing import get_context

import numpy as np
import requests
from PIL import Image, ImageGrab, ImageOps
from PySide6.QtCore import QThread, Signal

import difflib
import psutil
import win32gui
import win32process
import win32ui
import ctypes

import win32api

from ctypes import windll, wintypes
from dataclasses import dataclass

from multiscope import APP_FOOTER

# -----------------------------
# Frame similarity helpers
# -----------------------------

_FRAME_HASH_SIZE = 16  # 16x16 = 256-bit perceptual hash (fast, robust to minor noise)


def compute_frame_hash(image: Image.Image, hash_size: int = _FRAME_HASH_SIZE) -> int:
    """
    Compute a compact perceptual hash (average-hash) for quickly comparing frames.

    Returns a Python int whose bits represent the hash (hash_size*hash_size bits).
    """
    if hash_size <= 0:
        raise ValueError("hash_size must be > 0")

    gray = image if image.mode == "L" else image.convert("L")
    small = gray.resize((hash_size, hash_size), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32)
    avg = float(arr.mean()) if arr.size else 0.0
    bits = (arr > avg).astype(np.uint8).reshape(-1)

    packed = np.packbits(bits)
    return int.from_bytes(packed.tobytes(), byteorder="big", signed=False)


def frame_hash_diff_percent(hash_a: int, hash_b: int, hash_size: int = _FRAME_HASH_SIZE) -> float:
    """Return the percent of bits that differ between two frame hashes (0..100)."""
    bits = int(hash_size) * int(hash_size)
    if bits <= 0:
        return 100.0
    dist = (int(hash_a) ^ int(hash_b)).bit_count()
    return (float(dist) / float(bits)) * 100.0

# RapidOCR (ONNXRuntime) import with fallback search paths (helps when the frozen EXE missed the package)
RapidOCR = None  # type: ignore
ort = None  # type: ignore
_RAPIDOCR_IMPORT_ERROR = None
_ORT_IMPORT_ERROR = None
_OCR_DEVICE_SUMMARY: Optional[str] = None
_OCR_ENGINE_REF: Any = None
_OCR_DEVICE_ID: Optional[int] = None
_RAPIDOCR_ORIG_GET_EP_LIST = None
_RAPIDOCR_PATCHED_DEVICE_ID: Optional[int] = None


def _hresult_code(value: int) -> int:
    return value & 0xFFFFFFFF


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wintypes.UINT),
    ]


_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 0x2
_IID_IDXGIFactory1 = _GUID(
    0x770AAE78, 0xF26F, 0x4DBA, (0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
)


def _com_method(instance: ctypes.c_void_p, index: int, restype, argtypes):
    vtbl = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    func_type = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return func_type(vtbl[index])


def _release_com(instance: ctypes.c_void_p) -> None:
    if not instance:
        return
    try:
        release = _com_method(instance, 2, wintypes.ULONG, [])
        release(instance)
    except Exception:
        pass


def _dxgi_enum_adapters() -> List[Dict[str, Any]]:
    adapters: List[Dict[str, Any]] = []
    try:
        dxgi = ctypes.windll.dxgi
    except Exception:
        return adapters

    factory = ctypes.c_void_p()
    try:
        create_factory = dxgi.CreateDXGIFactory1
        create_factory.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        create_factory.restype = wintypes.HRESULT
        hr = create_factory(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory))
    except Exception:
        return adapters
    if hr != 0 or not factory:
        return adapters

    try:
        enum_adapters1 = _com_method(
            factory,
            12,
            wintypes.HRESULT,
            [wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)],
        )
        idx = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = enum_adapters1(factory, idx, ctypes.byref(adapter))
            if _hresult_code(hr) == _DXGI_ERROR_NOT_FOUND:
                break
            if hr != 0 or not adapter:
                break
            try:
                get_desc1 = _com_method(
                    adapter, 10, wintypes.HRESULT, [ctypes.POINTER(_DXGI_ADAPTER_DESC1)]
                )
                desc = _DXGI_ADAPTER_DESC1()
                if get_desc1(adapter, ctypes.byref(desc)) == 0:
                    name = str(desc.Description).rstrip("\x00").strip()
                    if name and not (int(desc.Flags) & _DXGI_ADAPTER_FLAG_SOFTWARE):
                        adapters.append({"id": idx, "name": name})
            finally:
                _release_com(adapter)
            idx += 1
    finally:
        _release_com(factory)
    return adapters


def _enumerate_display_adapters_fallback() -> List[Dict[str, Any]]:
    if win32api is None:
        return []
    adapters: List[Dict[str, Any]] = []
    idx = 0
    while True:
        try:
            dev = win32api.EnumDisplayDevices(None, idx)
        except Exception:
            break
        name = (getattr(dev, "DeviceString", "") or "").strip()
        if name:
            adapters.append({"id": idx, "name": name})
        idx += 1
    return adapters


def _unique_by_name(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def get_ocr_available_devices() -> List[Tuple[int, str]]:
    adapters = _dxgi_enum_adapters()
    if not adapters:
        adapters = _unique_by_name(_enumerate_display_adapters_fallback())
    return [(int(d["id"]), str(d["name"])) for d in adapters]


def _get_gpu_name_for_device(device_id: int) -> Optional[str]:
    adapters = _dxgi_enum_adapters()
    if not adapters:
        adapters = _enumerate_display_adapters_fallback()
    for item in adapters:
        if int(item.get("id", -1)) == int(device_id):
            return str(item.get("name", "")).strip() or None
    return None


def _format_provider_summary(provider: str, device_id: Optional[int]) -> str:
    if provider == "DmlExecutionProvider" or "CUDAExecutionProvider":
        base = "GPU"
    elif provider == "CPUExecutionProvider":
        return "CPU"
    else:
        return provider

    if device_id is None:
        device_id = 0
    name = _get_gpu_name_for_device(device_id)
    if name:
        return f"{name} ({base} {device_id})"
    return base


def _collect_provider_lists(obj: Any, max_depth: int = 3) -> List[List[str]]:
    providers_list: List[List[str]] = []
    seen: set[int] = set()

    def _walk(value: Any, depth: int) -> None:
        if value is None:
            return
        obj_id = id(value)
        if obj_id in seen:
            return
        seen.add(obj_id)

        get_providers = getattr(value, "get_providers", None)
        if callable(get_providers):
            try:
                providers = list(get_providers())
            except Exception:
                providers = []
            if providers:
                providers_list.append(providers)

        if depth >= max_depth:
            return

        if isinstance(value, dict):
            for v in value.values():
                _walk(v, depth + 1)
            return
        if isinstance(value, (list, tuple, set)):
            for v in value:
                _walk(v, depth + 1)
            return

        try:
            obj_dict = vars(value)
        except Exception:
            slots = getattr(value, "__slots__", None)
            if slots:
                for slot in slots:
                    try:
                        _walk(getattr(value, slot), depth + 1)
                    except Exception:
                        continue
            return
        for v in obj_dict.values():
            _walk(v, depth + 1)

    _walk(obj, 0)
    return providers_list


def _summarize_device_from_engine(
    engine: Any, fallback_providers: List[str], device_id: Optional[int]
) -> str:
    provider_lists = _collect_provider_lists(engine)
    if provider_lists:
        primaries: List[str] = []
        for providers in provider_lists:
            if providers:
                primaries.append(providers[0])
        if primaries:
            unique: List[str] = []
            for provider in primaries:
                if provider not in unique:
                    unique.append(provider)
            if len(unique) == 1:
                return _format_provider_summary(unique[0], device_id)
            readable = ", ".join(_format_provider_summary(p, device_id) for p in unique)
            return f"Mixed ({readable})"
    if fallback_providers:
        return f"Unknown (sessions not inspected; available: {', '.join(fallback_providers)})"
    return "Unknown (OCR not initialized)"


def get_ocr_device_summary() -> str:
    """
    Return a short human-readable summary of the device used for OCR.
    """
    if RapidOCR is None or ort is None:
        return "Unavailable (RapidOCR/ONNX Runtime not installed)"
    global _OCR_DEVICE_SUMMARY
    if _OCR_ENGINE_REF is not None:
        if not _OCR_DEVICE_SUMMARY or _OCR_DEVICE_SUMMARY.startswith("Unknown"):
            _OCR_DEVICE_SUMMARY = _summarize_device_from_engine(
                _OCR_ENGINE_REF, _get_ort_providers(), _OCR_DEVICE_ID
            )
        if _OCR_DEVICE_SUMMARY:
            return _OCR_DEVICE_SUMMARY
    if _OCR_DEVICE_SUMMARY:
        return _OCR_DEVICE_SUMMARY
    return "Unknown (OCR engine not initialized)"


def _add_site_packages_paths() -> None:
    """Add common site-packages locations to sys.path (best-effort)."""
    import sys
    import sysconfig
    from pathlib import Path

    candidates = []

    # Explicit site-packages locations (purelib/platlib) and base_prefix/Lib/site-packages
    for key in ("purelib", "platlib"):
        try:
            p = sysconfig.get_path(key)
            if p:
                candidates.append(Path(p))
        except Exception:
            pass
    try:
        candidates.append(Path(sys.base_prefix) / "Lib" / "site-packages")
    except Exception:
        pass
    # Explicit Windows install path under LOCALAPPDATA\Programs\Python\PythonXY\Lib\site-packages
    try:
        import os
        la = os.environ.get("LOCALAPPDATA", "")
        if la:
            pyver = f"Python{sys.version_info.major}{sys.version_info.minor}"
            candidates.append(Path(la) / "Programs" / "Python" / pyver / "Lib" / "site-packages")
    except Exception:
        pass

    seen = set()
    for base in candidates:
        if not base:
            continue
        if base in seen:
            continue
        seen.add(base)
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))


def _try_import_rapidocr():
    """Try to import rapidocr_onnxruntime from standard site-packages locations only."""
    import importlib

    _add_site_packages_paths()
    try:
        module = importlib.import_module("rapidocr_onnxruntime")  # type: ignore
    except Exception:
        return None
    return getattr(module, "RapidOCR", None)


def _try_import_onnxruntime():
    """Try to import onnxruntime from standard site-packages locations only."""
    import importlib

    _add_site_packages_paths()
    try:
        return importlib.import_module("onnxruntime")  # type: ignore
    except Exception:
        return None


try:
    from rapidocr_onnxruntime import RapidOCR  # type: ignore
except Exception as _rapid_err:  # pragma: no cover - environment dependent
    RapidOCR = _try_import_rapidocr()  # type: ignore
    _RAPIDOCR_IMPORT_ERROR = _rapid_err if RapidOCR is None else None
else:
    _RAPIDOCR_IMPORT_ERROR = None

try:
    import onnxruntime as ort  # type: ignore
except Exception as _ort_err:  # pragma: no cover - environment dependent
    ort = _try_import_onnxruntime()  # type: ignore
    _ORT_IMPORT_ERROR = _ort_err if ort is None else None
else:
    _ORT_IMPORT_ERROR = None


def _get_ort_providers() -> List[str]:
    if ort is None:
        return []
    try:
        return ort.get_available_providers()  # type: ignore[attr-defined]
    except Exception:
        return []


def _apply_rapidocr_device_id(device_id: Optional[int]) -> None:
    global _RAPIDOCR_ORIG_GET_EP_LIST, _RAPIDOCR_PATCHED_DEVICE_ID
    try:
        from rapidocr_onnxruntime.utils import infer_engine as _infer_engine  # type: ignore
    except Exception:
        return

    if _RAPIDOCR_ORIG_GET_EP_LIST is None:
        _RAPIDOCR_ORIG_GET_EP_LIST = _infer_engine.OrtInferSession._get_ep_list

    if device_id is None:
        _infer_engine.OrtInferSession._get_ep_list = _RAPIDOCR_ORIG_GET_EP_LIST
        _RAPIDOCR_PATCHED_DEVICE_ID = None
        return

    if _RAPIDOCR_PATCHED_DEVICE_ID == device_id:
        return

    orig_get_ep_list = _RAPIDOCR_ORIG_GET_EP_LIST

    def _get_ep_list(self):  # type: ignore[no-untyped-def]
        ep_list = orig_get_ep_list(self)
        updated = []
        for provider, opts in ep_list:
            if provider in ("DmlExecutionProvider", "CUDAExecutionProvider"):
                new_opts = dict(opts or {})
                new_opts["device_id"] = int(device_id)
                updated.append((provider, new_opts))
            else:
                updated.append((provider, opts))
        return updated

    _infer_engine.OrtInferSession._get_ep_list = _get_ep_list
    _RAPIDOCR_PATCHED_DEVICE_ID = device_id


def _init_rapidocr_engine(
    require_dml: bool = True,
    device_id: Optional[int] = None,
    force_cpu: bool = False,
):
    global _OCR_DEVICE_SUMMARY, _OCR_ENGINE_REF, _OCR_DEVICE_ID
    if RapidOCR is None:
        raise RuntimeError("rapidocr_onnxruntime is not available.")
    if ort is None:
        raise RuntimeError("onnxruntime is not available.")
    if force_cpu:
        require_dml = False
    _OCR_DEVICE_ID = device_id
    if force_cpu:
        _apply_rapidocr_device_id(None)
    else:
        _apply_rapidocr_device_id(device_id)
    providers = _get_ort_providers()
    use_dml = "DmlExecutionProvider" in providers
    if require_dml and not use_dml:
        _OCR_DEVICE_SUMMARY = "Unavailable (DirectML provider not available)"
        raise RuntimeError("DirectML provider is not available (install onnxruntime-directml).")
    if force_cpu:
        try:
            engine = RapidOCR(
                det_use_dml=False,
                cls_use_dml=False,
                rec_use_dml=False,
                det_use_cuda=False,
                cls_use_cuda=False,
                rec_use_cuda=False,
            )
        except TypeError:
            engine = RapidOCR()
        _OCR_DEVICE_SUMMARY = "CPU"
    else:
        if use_dml:
            try:
                engine = RapidOCR(det_use_dml=True, cls_use_dml=True, rec_use_dml=True)
            except TypeError:
                engine = RapidOCR()
        else:
            engine = RapidOCR()
        _OCR_DEVICE_SUMMARY = _summarize_device_from_engine(engine, providers, device_id)
    _OCR_ENGINE_REF = engine
    return engine


def _rapidocr_text_only(engine, img_np: np.ndarray) -> str:
    """
    Run RapidOCR and return text only (one line per detection).
    rapidocr_onnxruntime returns (ocr_result, elapse).
    """
    ocr_result, _elapse = engine(img_np)
    if not ocr_result:
        return ""

    lines: List[str] = []
    for item in ocr_result:
        try:
            rec_text = item[1]
        except Exception:
            continue
        if rec_text:
            lines.append(str(rec_text))
    return "\n".join(lines)


_POOL_ENGINE = None


def _init_pool_reader(device_id: Optional[int] = None, force_cpu: bool = False) -> None:
    """Initializer for OCR process workers so RapidOCR is created once per process."""
    global _POOL_ENGINE
    if _POOL_ENGINE is None:
        _POOL_ENGINE = _init_rapidocr_engine(
            require_dml=not force_cpu,
            device_id=device_id,
            force_cpu=force_cpu,
        )


def _pool_read_text(img_bytes: bytes) -> str:
    """Read text from a preprocessed image inside a process worker."""
    global _POOL_ENGINE
    if _POOL_ENGINE is None:
        _init_pool_reader()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    return _rapidocr_text_only(_POOL_ENGINE, np.array(img))


def _ocr_pool_task(
    preprocessed_bytes: bytes,
    raw_bytes: bytes,
    filters: List["ColorFilter"],
    use_preprocess: bool,
) -> Dict[str, Any]:
    """
    Run OCR inside a process worker and return the detected merchant (if any).
    """
    try:
        text = _pool_read_text(preprocessed_bytes)
        merchant_type = detect_merchant_type(text)

        if merchant_type == "jester":
            # Secondary confirmation using only purple filters to reduce false positives.
            purple_filters: List[ColorFilter] = []
            saw_purple = False
            for cf in filters:
                name = (cf.name or "").strip().lower()
                enabled = name in ("jester", "purple_text")
                if enabled:
                    saw_purple = True
                purple_filters.append(ColorFilter(cf.name, cf.r, cf.g, cf.b, cf.tol, enabled))

            if saw_purple:
                raw_img = Image.open(io.BytesIO(raw_bytes))
                purple_img = preprocess_for_ocr(raw_img, purple_filters)
                purple_text = _pool_read_text(_image_bytes(purple_img))
                if not contains_jester_message(purple_text):
                    merchant_type = None

        return {"merchant": merchant_type, "text": text}
    except Exception as e:
        return {"merchant": None, "error": f"{e.__class__.__name__}: {e}"}


# -----------------------------
# Data structures & helpers
# -----------------------------


@dataclass
class RobloxWindow:
    hwnd: int
    pid: int
    title: str


@dataclass
class ColorFilter:
    name: str
    r: int
    g: int
    b: int
    tol: int
    enabled: bool = True


MERCHANT_LINES = {
    "jester": "[Merchant]: Jester has arrived on the island!!",
    "mari": "[Merchant]: Mari has arrived on the island...",
}


def _normalize_text(s: str) -> str:
    return " ".join(s.lower().split())


def _fuzzy_match(line: str, target: str, threshold: float = 0.7) -> bool:
    l = _normalize_text(line)
    t = _normalize_text(target)
    if not l or not t:
        return False
    return difflib.SequenceMatcher(None, l, t).ratio() >= threshold


def detect_merchant_type(text: str) -> Optional[str]:
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    max_lookahead = 3
    for i in range(len(lines)):
        # Merchant lines may wrap across chat lines, so check a window of nearby lines.
        for lookahead in range(1, min(max_lookahead, len(lines) - i) + 1):
            block = " ".join(lines[i : i + lookahead])
            block_lower = block.lower()
            if "merchant" not in block_lower:
                continue
            if "jester" in block_lower and "arrived" in block_lower and "island" in block_lower:
                return "jester"
            if "mari" in block_lower and "arrived" in block_lower and "island" in block_lower:
                return "mari"
            if _fuzzy_match(block, MERCHANT_LINES["jester"]):
                return "jester"
            if _fuzzy_match(block, MERCHANT_LINES["mari"]):
                return "mari"
    return None


def contains_jester_message(text: str) -> bool:
    return detect_merchant_type(text) == "jester"


def enum_roblox_windows(process_names: Optional[List[str]] = None) -> List[RobloxWindow]:
    if process_names is None:
        process_names = ["RobloxPlayerBeta.exe"]

    wanted = set(n.lower() for n in process_names)
    pid_to_name: Dict[int, str] = {}

    def get_proc_name(pid: int) -> Optional[str]:
        if pid in pid_to_name:
            return pid_to_name[pid]
        try:
            p = psutil.Process(pid)
            name = p.name().lower()
            pid_to_name[pid] = name
            return name
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    windows: List[RobloxWindow] = []

    def callback(hwnd, _extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            name = get_proc_name(pid)
            if name in wanted:
                windows.append(RobloxWindow(hwnd=hwnd, pid=pid, title=title))
        except Exception:
            return

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return windows
    return windows


def crop_image_with_roi(image: Image.Image, roi: Tuple[float, float, float, float]) -> Image.Image:
    iw, ih = image.size
    rx, ry, rw, rh = roi
    x0 = max(0, min(iw, int(rx * iw)))
    y0 = max(0, min(ih, int(ry * ih)))
    x1 = max(0, min(iw, int((rx + rw) * iw)))
    y1 = max(0, min(ih, int((ry + rh) * ih)))
    if x1 <= x0 or y1 <= y0:
        return image.copy()
    return image.crop((x0, y0, x1, y1))


def capture_window_printwindow(hwnd: int) -> Optional[Image.Image]:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None

    mfc_dc = None
    save_dc = None
    save_bitmap = None
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bitmap)
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0x00000002)
        if result != 1:
            return None
        bmpinfo = save_bitmap.GetInfo()
        bmpstr = save_bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr,
            "raw",
            "BGRX",
            0,
            1,
        )
        return img
    except Exception:
        return None
    finally:
        try:
            if save_bitmap is not None:
                win32gui.DeleteObject(save_bitmap.GetHandle())
        except Exception:
            pass
        try:
            if save_dc is not None:
                save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def capture_window_fallback(hwnd: int) -> Optional[Image.Image]:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    try:
        return ImageGrab.grab(bbox=(left, top, right, bottom))
    except Exception:
        return None


def capture_window_image(hwnd: int, roi: Optional[Tuple[float, float, float, float]] = None) -> Optional[Image.Image]:
    img = capture_window_printwindow(hwnd)
    if img is None:
        img = capture_window_fallback(hwnd)
    if img is None:
        return None
    if roi is not None:
        return crop_image_with_roi(img, roi)
    return img


def preprocess_for_ocr(image: Image.Image, color_filters: List[ColorFilter]) -> Image.Image:
    def _finalize_gray(gray_img: Image.Image) -> Image.Image:
        """Apply contrast, scale, and invert to prepare for OCR."""
        if gray_img.mode != "L":
            gray_img = gray_img.convert("L")
        gray_img = ImageOps.autocontrast(gray_img)
        w0, h0 = gray_img.size
        if w0 == 0 or h0 == 0:
            return ImageOps.invert(gray_img)
        scale = 3 if w0 < 400 else 2
        resized = gray_img.resize((w0 * scale, h0 * scale), Image.LANCZOS)
        return ImageOps.invert(resized)

    if not color_filters:
        return _finalize_gray(image.convert("L"))

    rgb = image.convert("RGB")
    arr = np.asarray(rgb, dtype=np.uint8)

    h, w = arr.shape[0], arr.shape[1]
    if h == 0 or w == 0:
        return _finalize_gray(rgb.convert("L"))

    enabled_filters = [cf for cf in color_filters if cf.enabled]
    if not enabled_filters:
        return _finalize_gray(rgb.convert("L"))

    keep_mask = np.zeros((h, w), dtype=bool)
    r_channel = arr[:, :, 0].astype(np.int16)
    g_channel = arr[:, :, 1].astype(np.int16)
    b_channel = arr[:, :, 2].astype(np.int16)

    for cf in enabled_filters:
        r0 = max(0, min(255, int(cf.r)))
        g0 = max(0, min(255, int(cf.g)))
        b0 = max(0, min(255, int(cf.b)))
        tol = max(0, int(cf.tol))

        dr = np.abs(r_channel - r0)
        dg = np.abs(g_channel - g0)
        db = np.abs(b_channel - b0)

        keep_mask |= (dr <= tol) & (dg <= tol) & (db <= tol)

    mask_arr = np.zeros_like(arr, dtype=np.uint8)
    mask_arr[keep_mask] = [255, 255, 255]
    text_only = Image.fromarray(mask_arr, mode="RGB")
    gray = text_only.convert("L")
    return _finalize_gray(gray)



def _filters_from_cfg(raw_filters: List[Dict[str, Any]]) -> List[ColorFilter]:
    filters: List[ColorFilter] = []
    for f in raw_filters or []:
        try:
            name = str(f.get("name", "")).strip()
            lower = name.lower()
            if lower == "white_text":
                name = "Mari"
            elif lower == "purple_text":
                name = "Jester"
            filters.append(
                ColorFilter(
                    name,
                    int(f.get("r", 0)),
                    int(f.get("g", 0)),
                    int(f.get("b", 0)),
                    int(f.get("tol", 0)),
                    bool(f.get("enabled", True)),
                )
            )
        except Exception:
            continue
    if not filters:
        filters = [
            ColorFilter("Mari", 255, 255, 255, 40, True),
            ColorFilter("Jester", 145, 67, 255, 40, True),
        ]
    return filters


def _roi_from_cfg(roi_cfg: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    try:
        rx = float(roi_cfg.get("x", 0.0))
        ry = float(roi_cfg.get("y", 0.0))
        rw = float(roi_cfg.get("w", 0.0))
        rh = float(roi_cfg.get("h", 0.0))
    except Exception:
        return None
    if rw <= 0 or rh <= 0:
        return None
    return (rx, ry, rw, rh)


def _image_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _parse_device_id(raw: Any) -> Tuple[Optional[int], bool]:
    if raw is None:
        return None, False
    if isinstance(raw, str):
        val = raw.strip().lower()
        if val in ("", "auto", "none"):
            return None, False
        if val in ("cpu", "force_cpu"):
            return None, True
    try:
        device_id = int(raw)
    except Exception:
        return None, False
    if device_id < 0:
        return None, True
    return device_id, False


class OCRWorker(QThread):
    """
    Background OCR loop that mirrors roblox_multi_ocr.py but integrates with
    the existing MultiScope merchant webhook and JARAM process metadata.
    """

    log_signal = Signal(str)
    status_signal = Signal(str)
    merchant_signal = Signal(str, str)  # (user_id, merchant)

    def __init__(
        self,
        *,
        ocr_settings: Optional[Dict[str, Any]],
        ms_settings: Optional[Dict[str, Any]],
        context_provider: Optional[Callable[[int], Dict[str, Any]]] = None,
    ):
        super().__init__()
        self._ocr_cfg = ocr_settings or {}
        self._ms_cfg = ms_settings or {}
        self._context_provider = context_provider

        self._cooldowns: Dict[int, float] = {}
        self._capture_rr_index = 0
        self._stop_event = threading.Event()
        self._last_log: Optional[str] = None
        self._reader = None
        self._ocr_pool: Optional[ProcessPoolExecutor] = None
        self._mp_ctx = get_context("spawn")
        self._frame_hash_size = _FRAME_HASH_SIZE
        self._frame_diff_tolerance = 0.0
        self._last_frame_hash_by_pid: Dict[int, int] = {}

        self._send_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ocr-send")

        self._apply_cfg(self._ocr_cfg, self._ms_cfg)

    def stop(self) -> None:
        self._stop_event.set()

    def update_settings(self, ocr_settings: Dict[str, Any], ms_settings: Optional[Dict[str, Any]] = None) -> None:
        self._apply_cfg(ocr_settings or {}, ms_settings or self._ms_cfg)

    # -------------------------- core loop --------------------------
    def run(self) -> None:
        if RapidOCR is None or ort is None:
            missing: List[str] = []
            if RapidOCR is None:
                missing.append(f"rapidocr_onnxruntime ({_RAPIDOCR_IMPORT_ERROR})")
            if ort is None:
                missing.append(f"onnxruntime ({_ORT_IMPORT_ERROR})")
            self._log(f"[OCR] RapidOCR/ONNX Runtime not available: {', '.join(missing)}")
            return

        if not self._roi:
            self._log("OCR worker did not start: calibrate the chat area first.")
            return

        self._stop_event.clear()
        loop_idx = 0
        try:
            self._reader = self._init_reader()
        except Exception as e:  # pragma: no cover - GPU/driver specific
            self._log(f"[OCR] Failed to initialize RapidOCR (DirectML): {e}")
            return
        try:
            self._ocr_pool = ProcessPoolExecutor(
                max_workers=max(1, self._workers),
                mp_context=self._mp_ctx,
                initializer=_init_pool_reader,
                initargs=(self._device_id, self._force_cpu),
            )
        except Exception as e:
            self._log(f"[OCR] Failed to start OCR process pool: {e}")
            return

        try:
            self.status_signal.emit("running")
            self._log("OCR worker started.")

            while not self._stop_event.is_set():
                loop_idx += 1
                start = time.time()
                try:
                    windows = enum_roblox_windows()
                    self._log(f"[Loop {loop_idx}] Enumerated {len(windows)} Roblox window(s).")
                    if not windows:
                        self._log("No Roblox windows found.")
                        sleep_for = max(0.0, getattr(self, "_batch_delay_seconds", 1.0))
                        self._log(f"[Loop {loop_idx}] Sleeping {sleep_for:.2f}s (no windows).")
                        time.sleep(sleep_for)
                        continue

                    work_list = self._select_windows(windows)
                    self._log(f"[Loop {loop_idx}] Selected {len(work_list)} window(s) for capture.")
                    if work_list:
                        # Capture raw images first so we can preprocess in batch
                        captured: List[Tuple[Any, Optional[Image.Image]]] = []
                        for win in work_list:
                            if self._stop_event.is_set():
                                break
                            raw_img = capture_window_image(win.hwnd, self._roi)
                            captured.append((win, raw_img))
                        self._log(f"[Loop {loop_idx}] Captured {len(captured)} window(s).")

                        # Preprocess in batch for windows that captured successfully
                        valid_pairs = [(w, img) for w, img in captured if img is not None]
                        preprocessed: List[Image.Image] = []
                        if valid_pairs:
                            imgs_only = [img for _, img in valid_pairs]
                            try:
                                preprocessed = self._preprocess_batch(imgs_only)
                            except Exception as e:
                                self._log(f"[OCR] Preprocess batch failed: {e}")
                                self._log(traceback.format_exc())
                                preprocessed = imgs_only
                        self._log(
                            f"[Loop {loop_idx}] Preprocessed {len(preprocessed)} image(s) (valid captures: {len(valid_pairs)})."
                        )

                        processed_count = 0
                        skipped_similar = 0
                        if valid_pairs and preprocessed:
                            if len(preprocessed) != len(valid_pairs):
                                self._log(f"[Loop {loop_idx}] Preprocess/result length mismatch; trimming to smallest set.")
                                valid_pairs = valid_pairs[: len(preprocessed)]

                            to_ocr: List[Tuple[Any, Image.Image, Image.Image]] = []
                            for (win, raw_img), prep_img in zip(valid_pairs, preprocessed):
                                skip, _diff_pct = self._skip_ocr_for_similar_frame(win.pid, prep_img)
                                if skip:
                                    skipped_similar += 1
                                    continue
                                to_ocr.append((win, raw_img, prep_img))

                            if self._ocr_pool:
                                future_map = {}
                                for win, raw_img, prep_img in to_ocr:
                                    try:
                                        fut = self._ocr_pool.submit(
                                            _ocr_pool_task,
                                            _image_bytes(prep_img),
                                            _image_bytes(raw_img),
                                            self._filters,
                                            self._use_preprocess,
                                        )
                                        future_map[fut] = (win, raw_img)
                                    except Exception as e:
                                        self._log(f"[OCR] Failed to dispatch process task for PID {win.pid}: {e}")

                                # IMPORTANT: `as_completed()` without a timeout can block forever if the
                                # pool workers are busy/hung, which prevents a clean shutdown and can
                                # leave background processes after closing JARAM. Use a short wait loop
                                # so `stop()` is responsive.
                                pending = set(future_map.keys())
                                while pending:
                                    if self._stop_event.is_set():
                                        for fut in list(pending):
                                            try:
                                                fut.cancel()
                                            except Exception:
                                                pass
                                        break

                                    done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                                    if not done:
                                        continue

                                    for fut in done:
                                        win, raw_img = future_map.get(fut, (None, None))
                                        if win is None:
                                            continue
                                        if self._stop_event.is_set():
                                            break
                                        try:
                                            result = fut.result()
                                        except Exception as e:
                                            self._log(f"[OCR] Worker process error for PID {win.pid}: {e}")
                                            continue

                                    if not isinstance(result, dict):
                                        continue
                                    if result.get("error"):
                                        self._log(f"[OCR] Worker reported error for PID {win.pid}: {result['error']}")
                                        continue

                                    if getattr(self, "_log_ocr_text", False):
                                        text = str(result.get("text") or "").strip()
                                        if text:
                                            self._log(f"[OCR TEXT] PID {win.pid}:\n{text}")

                                    merchant_type = result.get("merchant")
                                    if merchant_type in ("jester", "mari"):
                                        self._handle_detection(merchant_type, win.pid, raw_img)
                                        self._set_pid_cooldown(win.pid)
                                    processed_count += 1
                            else:
                                # Fallback to in-process OCR if the pool is unavailable.
                                for win, raw_img, prep_img in to_ocr:
                                    try:
                                        self._process_window_preprocessed(win, raw_img, prep_img)
                                        processed_count += 1
                                    except Exception as e:
                                        self._log(f"[OCR] Worker error: {e}")

                        self._log(
                            f"[Loop {loop_idx}] Completed OCR for {processed_count} image(s) (skipped {skipped_similar} similar)."
                        )
                    else:
                        self._log(f"[Loop {loop_idx}] No windows eligible for capture this cycle.")

                    elapsed = time.time() - start
                    target_delay = max(0.0, getattr(self, "_batch_delay_seconds", 1.0))
                    if elapsed < target_delay:
                        sleep_for = max(0.0, target_delay - elapsed)
                        self._log(f"[Loop {loop_idx}] Sleeping {sleep_for:.2f}s to throttle loop.")
                        time.sleep(sleep_for)
                except Exception as e:
                    self._log(f"[OCR] Loop error: {e}")
                    self._log(traceback.format_exc())
                    time.sleep(1.0)

            self._log("OCR worker stopped.")
            self.status_signal.emit("stopped")
        finally:
            self._shutdown_ocr_pool()
            self._shutdown_send_pool()

    # ---------------------- detection helpers ---------------------
    def _process_window(self, win) -> None:
        if self._stop_event.is_set():
            return

        raw_img = capture_window_image(win.hwnd, self._roi)
        if raw_img is None:
            return

        img_for_ocr = preprocess_for_ocr(raw_img, self._filters) if self._use_preprocess else raw_img

        self._process_window_preprocessed(win, raw_img, img_for_ocr)

    def _process_window_preprocessed(self, win, raw_img: Image.Image, img_for_ocr: Image.Image) -> None:
        """Process a window when you already have raw + preprocessed images."""
        try:
            text = _rapidocr_text_only(self._reader, np.array(img_for_ocr))
        except Exception as e:
            self._log(f"[OCR error pid {win.pid}] {e}")
            return

        if getattr(self, "_log_ocr_text", False):
            clean_text = str(text or "").strip()
            if clean_text:
                self._log(f"[OCR TEXT] PID {win.pid}:\n{clean_text}")

        merchant_type = detect_merchant_type(text)
        if merchant_type == "jester" and not self._confirm_jester_with_purple(raw_img):
            merchant_type = None

        if merchant_type in ("jester", "mari"):
            self._handle_detection(merchant_type, win.pid, raw_img)
            self._set_pid_cooldown(win.pid)

    def _preprocess_batch(self, images: List[Image.Image]) -> List[Image.Image]:
        """Batch preprocess using PIL to cut per-image overhead."""
        if not images:
            return []

        # No preprocessing requested
        if not self._use_preprocess:
            return images

        # If no filters are enabled, fall back to simple grayscale path.
        # This is an explicit user choice and does not require GPU.
        if not self._filters:
            return [preprocess_for_ocr(img, []) for img in images]

        try:
            # Optionally downscale before masking to reduce work
            def _maybe_downscale(img: Image.Image) -> Image.Image:
                w, h = img.size
                if max(w, h) <= 800:
                    return img
                # scale down to ~800px max dimension
                scale = 800.0 / float(max(w, h))
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                return img.resize((new_w, new_h), Image.BILINEAR)

            downs = [_maybe_downscale(img.convert("RGB")) for img in images]
            return [preprocess_for_ocr(img, self._filters) for img in downs]
        except Exception as e:
            raise RuntimeError(f"OCR preprocessing failed: {e}") from e

    def _handle_detection(self, merchant: str, pid: int, raw_img: Image.Image) -> None:
        ctx = self._context_provider(pid) if self._context_provider else {}
        try:
            uid = str(ctx.get("user_id") or "").strip()
            if uid and merchant:
                self.merchant_signal.emit(uid, str(merchant))
        except Exception:
            pass
        username = ctx.get("username") or f"PID {pid}"
        owner_raw = str(ctx.get("owner") or "").strip()
        owner_known = bool(owner_raw)
        owner = owner_raw or username
        server_label = str(ctx.get("server_label") or "").strip() or "Unknown"
        ps_link = str(ctx.get("ps_link") or "").strip()

        self._log(f"[DETECT] {merchant.upper()} detected in PID {pid} ({username}).")
        self._send_webhook(merchant, pid, username, owner, owner_known, server_label, ps_link, raw_img)

    def _send_webhook(
        self,
        merchant: str,
        pid: int,
        username: str,
        owner: str,
        owner_known: bool,
        server_label: str,
        ps_link: str,
        raw_img: Image.Image,
    ) -> None:
        url = (self._ms_cfg or {}).get("merchant_webhook", "").strip()
        if not url:
            self._log("[Webhook] Merchant webhook is not configured.")
            return
        if bool(getattr(self, "_skip_webhook_unknown_context", False)):
            server_unknown = (not server_label) or server_label.strip().lower() == "unknown"
            owner_unknown = (not owner_known) or (str(owner).strip().lower() == "unknown")
            ps_unknown = not bool(ps_link)
            if server_unknown or owner_unknown or ps_unknown:
                self._log("[Webhook] Skipping OCR webhook; owner or private server unknown.")
                return

        ping_map = {
            "jester": (self._ms_cfg or {}).get("jester_ping", ""),
            "mari": (self._ms_cfg or {}).get("mari_ping", ""),
        }

        ts = datetime.now(timezone.utc)
        ts_epoch = int(ts.timestamp())
        ts_full = f"<t:{ts_epoch}:D> - <t:{ts_epoch}:T>"
        ts_rel = f"<t:{ts_epoch}:R>"

        emojis = {"jester": "\U0001f0cf", "mari": "\U0001f6cd"}
        colors = {"jester": 0xA352FF, "mari": 0xFF82AB}
        title = f"{emojis.get(merchant, '\U0001f4e3')} {merchant.title()} Has Arrived!"

        desc = (
            f"**Owner:** `{owner}`\n"
            f"**Detected by:** `{username}`\n"
            f"**Detected At:** {ts_full} ({ts_rel})\n"
            f"**Private Server:** " + (f"[Private Server Link]({ps_link})" if ps_link else "`N/A`")
        )

        embed = {
            "title": title,
            "description": desc,
            "color": colors.get(merchant, 0x7289DA),
            "timestamp": ts.isoformat(),
            "footer": {"text": f"{APP_FOOTER} - {server_label}"},
            "image": {"url": "attachment://chat.png"},
        }

        payload = {
            "content": ping_map.get(merchant, ""),
            "embeds": [embed],
        }

        files = {
            "chat.png": ("chat.png", _image_bytes(raw_img), "image/png"),
        }

        def _send() -> None:
            try:
                requests.post(url, data={"payload_json": json.dumps(payload)}, files=files, timeout=10)
            except Exception as e:
                self._log(f"[Webhook] Error posting detection: {e}")

        self._send_pool.submit(_send)

    def _confirm_jester_with_purple(self, raw_img: Image.Image) -> bool:
        purple_filters: List[ColorFilter] = []
        saw_purple = False
        for cf in self._filters:
            name = (cf.name or "").strip().lower()
            enabled = name in ("jester", "purple_text")
            if enabled:
                saw_purple = True
            purple_filters.append(ColorFilter(cf.name, cf.r, cf.g, cf.b, cf.tol, enabled))

        if not saw_purple:
            return True

        img_purple = preprocess_for_ocr(raw_img, purple_filters)
        try:
            text = _rapidocr_text_only(self._reader, np.array(img_purple))
        except Exception as e:
            self._log(f"[Jester verify] OCR error: {e}")
            return False

        if contains_jester_message(text):
            return True

        self._log("[Jester verify] Candidate rejected by purple-only check.")
        return False

    # ------------------------- scheduling -------------------------
    def _select_windows(self, windows) -> List[Any]:
        total = len(windows)
        if total == 0:
            return []

        limit = min(self._max_captures_per_second, total)
        start_idx = self._capture_rr_index % total
        idx = start_idx
        seen = 0
        work_list = []

        while seen < total and len(work_list) < limit:
            win = windows[idx]
            if not self._pid_on_cooldown(win.pid):
                work_list.append(win)
            idx = (idx + 1) % total
            seen += 1

        self._capture_rr_index = idx
        return work_list

    def _pid_on_cooldown(self, pid: int) -> bool:
        next_allowed = self._cooldowns.get(pid)
        return bool(next_allowed and time.time() < next_allowed)

    def _set_pid_cooldown(self, pid: int) -> None:
        self._cooldowns[pid] = time.time() + self._cooldown_seconds

    def _skip_ocr_for_similar_frame(self, pid: int, img_for_ocr: Image.Image) -> Tuple[bool, Optional[float]]:
        """
        Compare the current OCR frame to the last frame for this PID.
        Returns (skip, diff_percent). The internal last-frame cache is updated
        regardless so consecutive frames are compared.
        """
        tol = getattr(self, "_frame_diff_tolerance", None)
        if tol is None or float(tol) < 0:
            return False, None

        try:
            current_hash = compute_frame_hash(img_for_ocr, hash_size=self._frame_hash_size)
        except Exception:
            return False, None

        last_hash = self._last_frame_hash_by_pid.get(int(pid))
        self._last_frame_hash_by_pid[int(pid)] = current_hash
        if last_hash is None:
            return False, None

        diff_pct = frame_hash_diff_percent(last_hash, current_hash, hash_size=self._frame_hash_size)
        return (diff_pct <= float(tol)), diff_pct

    # ------------------------ configuration -----------------------
    def _apply_cfg(self, ocr_settings: Dict[str, Any], ms_settings: Dict[str, Any]) -> None:
        self._ocr_cfg = ocr_settings or {}
        self._ms_cfg = ms_settings or {}

        prev_filters = getattr(self, "_filters", None)
        prev_roi = getattr(self, "_roi", None)
        prev_use_preprocess = getattr(self, "_use_preprocess", None)
        prev_device_id = getattr(self, "_device_id", None)
        prev_force_cpu = getattr(self, "_force_cpu", None)

        self._filters = _filters_from_cfg(self._ocr_cfg.get("color_filters") or [])
        self._roi = _roi_from_cfg(self._ocr_cfg.get("roi") or {})
        self._workers = max(1, int(self._ocr_cfg.get("workers", 1) or 1))
        self._max_captures_per_second = max(1, int(self._ocr_cfg.get("max_captures_per_second", 20) or 1))
        try:
            self._batch_delay_seconds = max(0.0, float(self._ocr_cfg.get("batch_delay_seconds", 1.0)))
        except Exception:
            self._batch_delay_seconds = 1.0
        self._cooldown_seconds = float(self._ocr_cfg.get("cooldown_seconds", 600) or 600)
        self._use_preprocess = bool(self._ocr_cfg.get("use_preprocess", True))
        self._device_id, self._force_cpu = _parse_device_id(self._ocr_cfg.get("device_id"))
        self._log_ocr_text = bool(self._ocr_cfg.get("log_ocr_text", False))
        self._log_loop = bool(self._ocr_cfg.get("log_loop", True))
        skip_flag = None
        if isinstance(self._ms_cfg, dict) and "skip_webhook_unknown_context" in self._ms_cfg:
            skip_flag = bool(self._ms_cfg.get("skip_webhook_unknown_context", False))
        if skip_flag is None:
            skip_flag = bool(self._ocr_cfg.get("skip_webhook_unknown_context", False))
        self._skip_webhook_unknown_context = bool(skip_flag)

        try:
            tol = float(self._ocr_cfg.get("frame_diff_tolerance", 2.0))
        except Exception:
            tol = 2.0
        self._frame_diff_tolerance = max(0.0, min(100.0, tol))

        if prev_filters != self._filters or prev_roi != self._roi or prev_use_preprocess != self._use_preprocess:
            try:
                self._last_frame_hash_by_pid.clear()
            except Exception:
                pass
        if (
            (prev_device_id != self._device_id or prev_force_cpu != self._force_cpu)
            and getattr(self, "_reader", None) is not None
        ):
            try:
                self._log("[OCR] Device change detected; restart OCR to apply.")
            except Exception:
                pass

    def _init_reader(self):
        try:
            providers = _get_ort_providers()
            if providers:
                self._log(f"[ORT] Available providers: {providers}")
            else:
                self._log("[ORT] No ONNX Runtime providers detected.")

            engine = _init_rapidocr_engine(
                require_dml=not self._force_cpu,
                device_id=self._device_id,
                force_cpu=self._force_cpu,
            )
            if self._force_cpu:
                self._log("[ORT] OCR initialized on CPU.")
            elif "DmlExecutionProvider" in providers:
                self._log("[ORT] DirectML detected. RapidOCR initialized with DirectML.")
            return engine
        except Exception as e:
            raise RuntimeError(f"RapidOCR init failed: {e}")

    def _shutdown_ocr_pool(self) -> None:
        if self._ocr_pool:
            try:
                self._ocr_pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._ocr_pool.shutdown(wait=False)
            except Exception:
                pass
            self._ocr_pool = None

    def _shutdown_send_pool(self) -> None:
        pool = getattr(self, "_send_pool", None)
        if pool is None:
            return
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass
        except Exception:
            pass
        try:
            self._send_pool = None
        except Exception:
            pass

    # ---------------------------- misc ----------------------------
    def _log(self, msg: str) -> None:
        clean = msg.strip()
        if not clean:
            return
        if not getattr(self, "_log_loop", True):
            if clean.startswith("[Loop "):
                return
            if clean == "No Roblox windows found.":
                return
        if clean == self._last_log:
            return
        self._last_log = clean
        self.log_signal.emit(clean)

    def __del__(self) -> None:  # pragma: no cover - destructor safety
        try:
            self._shutdown_send_pool()
        except Exception:
            pass
        try:
            self._shutdown_ocr_pool()
        except Exception:
            pass
