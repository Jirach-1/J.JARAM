from __future__ import annotations

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import requests
from PIL import Image, ImageGrab, ImageOps
from PyQt6.QtCore import QThread, pyqtSignal

import difflib
import psutil
import win32gui
import win32process
import win32ui
from ctypes import windll
from dataclasses import dataclass

from multiscope import APP_FOOTER

# Optional Torch import for GPU-accelerated preprocessing
try:  # pragma: no cover - environment dependent
    import torch as _torch  # type: ignore
except Exception:  # pragma: no cover
    _torch = None  # type: ignore
# Optional Kornia import for GPU-native image ops
try:  # pragma: no cover - environment dependent
    import kornia as _kornia  # type: ignore
except Exception:  # pragma: no cover
    _kornia = None  # type: ignore
# EasyOCR import with fallback search paths (helps when the frozen EXE missed the package)
easyocr = None  # type: ignore
_EASYOCR_IMPORT_ERROR = None


def get_ocr_device_summary() -> str:
    """
    Return a short human-readable summary of the device used for OCR
    preprocessing (GPU-only). This is used by the GUI to show users
    whether Torch/Kornia will run on CUDA; CPU fallback is not used.
    """
    if _torch is None:
        return "Unavailable (Torch not installed; GPU required)"
    if _kornia is None:
        return "Unavailable (Kornia not installed; GPU required)"

    try:
        if _torch.cuda.is_available():
            try:
                name = _torch.cuda.get_device_name(0)
                return f"GPU (CUDA: {name})"
            except Exception:
                return "GPU (CUDA available)"
        return "Unavailable (CUDA not available; GPU required)"
    except Exception:
        return "Unavailable (Torch CUDA check failed; GPU required)"


def _try_import_easyocr():
    """Try to import easyocr from standard site-packages locations only."""
    import sys
    import sysconfig
    import importlib
    import traceback
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

    try:
        return importlib.import_module("easyocr")  # type: ignore
    except Exception as e:
        msg = (
            f"[EasyOCR] import failed: {e.__class__.__name__}: {e}\n"
            f"search paths tried (prepended to sys.path): {candidates}\n"
            f"sys.path (truncated): {sys.path[:10]}"
        )
        try:
            with open("easyocr_import_debug.log", "a", encoding="utf-8") as f:
                f.write(msg + "\n" + traceback.format_exc() + "\n")
        except Exception:
            pass
        return None

try:
    import easyocr  # type: ignore
except Exception as _easy_err:  # pragma: no cover - environment dependent
    easyocr = _try_import_easyocr()  # type: ignore
    _EASYOCR_IMPORT_ERROR = _easy_err if easyocr is None else None
else:
    _EASYOCR_IMPORT_ERROR = None


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
    for line in lines:
        l = line.lower()
        if "merchant" not in l:
            continue
        if "jester" in l and "arrived" in l and "island" in l:
            return "jester"
        if "mari" in l and "arrived" in l and "island" in l:
            return "mari"
        if _fuzzy_match(line, MERCHANT_LINES["jester"]):
            return "jester"
        if _fuzzy_match(line, MERCHANT_LINES["mari"]):
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

    win32gui.EnumWindows(callback, None)
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

    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bitmap)
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0x00000002)
        if result != 1:
            win32gui.DeleteObject(save_bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
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
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
    finally:
        win32gui.ReleaseDC(hwnd, hwnd_dc)
    return img


def capture_window_fallback(hwnd: int) -> Optional[Image.Image]:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    try:
        return ImageGrab.grab(bbox=(left, top, right, bottom))
    except OSError:
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
    if not color_filters:
        gray = image.convert("L")
        gray = ImageOps.autocontrast(gray)
        w0, h0 = gray.size
        scale = 3 if w0 < 400 else 2
        return gray.resize((w0 * scale, h0 * scale), Image.LANCZOS)

    if _torch is None or _kornia is None:
        raise RuntimeError("Torch + Kornia are required for OCR color-filter preprocessing.")

    rgb = image.convert("RGB")
    arr = np.asarray(rgb, dtype=np.uint8)

    h, w = arr.shape[0], arr.shape[1]
    if h == 0 or w == 0:
        gray = rgb.convert("L")
        gray = ImageOps.autocontrast(gray)
        w0, h0 = gray.size
        scale = 3 if w0 < 400 else 2
        return gray.resize((w0 * scale, h0 * scale), Image.LANCZOS)

    # Strict GPU-only: do not silently fall back to CPU
    if not _torch.cuda.is_available():
        raise RuntimeError("CUDA is required for OCR color-filter preprocessing (torch.cuda.is_available() is False).")
    device = "cuda"
    with _torch.no_grad():
        # Kornia expects BCHW; image_to_tensor gives CxHxW
        t = _kornia.image_to_tensor(arr, keepdim=False).to(device=device, dtype=_torch.float16) / 255.0  # CxHxW
        t = t.unsqueeze(0)  # 1xCxHxW

        # Build filter tensors: Fx3x1x1 and Fx1x1x1
        filt_rgb = []
        filt_tol = []
        for cf in color_filters:
            if not cf.enabled:
                continue
            filt_rgb.append([float(cf.r) / 255.0, float(cf.g) / 255.0, float(cf.b) / 255.0])
            filt_tol.append(float(cf.tol) / 255.0)
        if not filt_rgb:
            gray = rgb.convert("L")
            gray = ImageOps.autocontrast(gray)
            w0, h0 = gray.size
            scale = 3 if w0 < 400 else 2
            return gray.resize((w0 * scale, h0 * scale), Image.LANCZOS)

        filt_rgb_t = _torch.tensor(filt_rgb, device=device, dtype=_torch.float16).view(-1, 3, 1, 1)
        filt_tol_t = _torch.tensor(filt_tol, device=device, dtype=_torch.float16).view(-1, 1, 1, 1)

        # Compute mask: FxHxW
        diff = (t - filt_rgb_t).abs()  # B(1) x F x C x H x W broadcast
        keep = (diff <= filt_tol_t).all(dim=2).any(dim=1)  # B x H x W
        keep_any = keep[0]  # HxW

        mask = keep_any.to(dtype=_torch.float16).unsqueeze(0).repeat(3, 1, 1)  # 3xHxW
        # Convert to PIL via CPU
        mask_img = _kornia.tensor_to_image(mask.cpu())  # HxWx3 float16 0..1
        mask_img = np.clip(mask_img * 255.0, 0, 255).astype(np.uint8)
        text_only = Image.fromarray(mask_img, mode="RGB")

        gray = ImageOps.autocontrast(text_only.convert("L"))
        w0, h0 = gray.size
        scale = 3 if w0 < 400 else 2
        return gray.resize((w0 * scale, h0 * scale), Image.LANCZOS)



def _filters_from_cfg(raw_filters: List[Dict[str, Any]]) -> List[ColorFilter]:
    filters: List[ColorFilter] = []
    for f in raw_filters or []:
        try:
            filters.append(
                ColorFilter(
                    str(f.get("name", "")),
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
            ColorFilter("white_text", 255, 255, 255, 40, True),
            ColorFilter("purple_text", 145, 67, 255, 40, True),
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


class OCRWorker(QThread):
    """
    Background OCR loop that mirrors roblox_multi_ocr.py but integrates with
    the existing MultiScope merchant webhook and JARAM process metadata.
    """

    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)

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

        self._send_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ocr-send")

        self._apply_cfg(self._ocr_cfg, self._ms_cfg)

    def stop(self) -> None:
        self._stop_event.set()

    def update_settings(self, ocr_settings: Dict[str, Any], ms_settings: Optional[Dict[str, Any]] = None) -> None:
        self._apply_cfg(ocr_settings or {}, ms_settings or self._ms_cfg)

    # -------------------------- core loop --------------------------
    def run(self) -> None:
        if easyocr is None:
            self._log(f"[OCR] easyocr is not available: {_EASYOCR_IMPORT_ERROR}")
            return

        if not self._roi:
            self._log("OCR worker did not start: calibrate the chat area first.")
            return

        self._stop_event.clear()
        loop_idx = 0
        try:
            self._reader = self._init_reader()
        except Exception as e:  # pragma: no cover - GPU/driver specific
            self._log(f"[OCR] Failed to initialize EasyOCR: {e}")
            return

        self.status_signal.emit("running")
        self._log("OCR worker started.")

        while not self._stop_event.is_set():
            loop_idx += 1
            start = time.time()
            windows = enum_roblox_windows()
            self._log(f"[Loop {loop_idx}] Enumerated {len(windows)} Roblox window(s).")
            if not windows:
                self._log("No Roblox windows found.")
                self._log(f"[Loop {loop_idx}] Sleeping 1.00s (no windows).")
                time.sleep(1.0)
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
                    preprocessed = self._preprocess_batch(imgs_only)
                self._log(f"[Loop {loop_idx}] Preprocessed {len(preprocessed)} image(s) (valid captures: {len(valid_pairs)}).")

                # Dispatch OCR work (still parallelized) using the precomputed images
                with ThreadPoolExecutor(max_workers=min(self._workers, len(valid_pairs))) as pool:
                    futures = []
                    for (win, raw_img), prep_img in zip(valid_pairs, preprocessed):
                        futures.append(pool.submit(self._process_window_preprocessed, win, raw_img, prep_img))
                    for fut in as_completed(futures):
                        try:
                            fut.result()
                        except Exception as e:
                            self._log(f"[OCR] Worker error: {e}")
                self._log(f"[Loop {loop_idx}] Completed OCR for {len(valid_pairs)} image(s).")
            else:
                self._log(f"[Loop {loop_idx}] No windows eligible for capture this cycle.")

            elapsed = time.time() - start
            if elapsed < 1.0:
                sleep_for = max(0.0, 1.0 - elapsed)
                self._log(f"[Loop {loop_idx}] Sleeping {sleep_for:.2f}s to throttle loop.")
                time.sleep(sleep_for)

        self._log("OCR worker stopped.")
        self.status_signal.emit("stopped")

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
            text_lines = self._reader.readtext(np.array(img_for_ocr), detail=0, paragraph=True)
            text = "\n".join(text_lines) if text_lines else ""
        except Exception as e:
            self._log(f"[OCR error pid {win.pid}] {e}")
            return

        merchant_type = detect_merchant_type(text)
        if merchant_type == "jester" and not self._confirm_jester_with_purple(raw_img):
            merchant_type = None

        if merchant_type in ("jester", "mari"):
            self._handle_detection(merchant_type, win.pid, raw_img)
            self._set_pid_cooldown(win.pid)

    def _preprocess_batch(self, images: List[Image.Image]) -> List[Image.Image]:
        """Batch preprocess using Torch when available to cut per-image overhead."""
        if not images:
            return []

        # No preprocessing requested
        if not self._use_preprocess:
            return images

        # If no filters are enabled, fall back to simple grayscale path.
        # This is an explicit user choice and does not require GPU.
        if not self._filters:
            return [preprocess_for_ocr(img, []) for img in images]

        # From here on, GPU is mandatory for preprocessing (no CPU fallback).
        if _torch is None or _kornia is None:
            raise RuntimeError("Torch + Kornia are required for OCR GPU preprocessing (no CPU fallback).")
        if not _torch.cuda.is_available():
            raise RuntimeError("CUDA is required for OCR GPU preprocessing (no CPU fallback).")

        device = "cuda"
        try:
            try:
                self._log(f"[OCR] Preprocess batch device={device}.")
            except Exception:
                pass

            # Optionally downscale before masking to reduce GPU work
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
            arrs = [np.asarray(img, dtype=np.uint8) for img in downs]

            with _torch.no_grad():
                # Stack into a single tensor: B x 3 x H x W (pad to max size)
                max_h = max(a.shape[0] for a in arrs)
                max_w = max(a.shape[1] for a in arrs)
                batch = _torch.zeros((len(arrs), 3, max_h, max_w), dtype=_torch.float16, device=device)
                masks_valid = []
                for i, a in enumerate(arrs):
                    h, w, _ = a.shape
                    t = _kornia.image_to_tensor(a, keepdim=False).to(device=device, dtype=_torch.float16) / 255.0  # 3xHxW
                    batch[i, :, :h, :w] = t
                    masks_valid.append((h, w))

                # Build filter tensors (broadcasted)
                filt_rgb = []
                filt_tol = []
                for cf in self._filters:
                    if not cf.enabled:
                        continue
                    filt_rgb.append([float(cf.r) / 255.0, float(cf.g) / 255.0, float(cf.b) / 255.0])
                    filt_tol.append(float(cf.tol) / 255.0)
                if not filt_rgb:
                    # No enabled filters; fall back to simple grayscale/autocontrast/resize
                    return [preprocess_for_ocr(img, []) for img in images]

                filt_rgb_t = _torch.tensor(filt_rgb, device=device, dtype=_torch.float16).view(-1, 3, 1, 1)
                filt_tol_t = _torch.tensor(filt_tol, device=device, dtype=_torch.float16).view(-1, 1, 1, 1)

                # Compute mask in one broadcasted pass: B x F x H x W
                diff = (batch.unsqueeze(1) - filt_rgb_t).abs()  # B x F x 3 x H x W
                keep = (diff <= filt_tol_t).all(dim=2)  # B x F x H x W
                keep_any = keep.any(dim=1)  # B x H x W

                # Build masked images per batch element
                out_images: List[Image.Image] = []
                keep_any = keep_any.to(dtype=_torch.float16)
                keep_any_cpu = keep_any.cpu().numpy()
                for i, (h, w) in enumerate(masks_valid):
                    m = keep_any_cpu[i, :h, :w]
                    mask_arr = (np.clip(m, 0, 1) * 255.0).astype(np.uint8)
                    mask_rgb = np.repeat(mask_arr[:, :, None], 3, axis=2)
                    text_only = Image.fromarray(mask_rgb, mode="RGB")
                    gray = ImageOps.autocontrast(text_only.convert("L"))
                    w0, h0 = gray.size
                    scale = 3 if w0 < 400 else 2
                    out_images.append(gray.resize((w0 * scale, h0 * scale), Image.LANCZOS))

                # Proactively free GPU cache to avoid creeping allocations
                try:
                    _torch.cuda.empty_cache()
                except Exception:
                    pass

                return out_images
        except Exception as e:
            # Surface the error so the worker stops instead of silently using CPU.
            raise RuntimeError(f"OCR GPU preprocessing failed: {e}") from e

    def _handle_detection(self, merchant: str, pid: int, raw_img: Image.Image) -> None:
        ctx = self._context_provider(pid) if self._context_provider else {}
        username = ctx.get("username") or f"PID {pid}"
        owner = ctx.get("owner") or username
        server_label = ctx.get("server_label") or "Unknown"
        ps_link = ctx.get("ps_link") or ""

        self._log(f"[DETECT] {merchant.upper()} detected in PID {pid} ({username}).")
        self._send_webhook(merchant, pid, username, owner, server_label, ps_link, raw_img)

    def _send_webhook(
        self,
        merchant: str,
        pid: int,
        username: str,
        owner: str,
        server_label: str,
        ps_link: str,
        raw_img: Image.Image,
    ) -> None:
        url = (self._ms_cfg or {}).get("merchant_webhook", "").strip()
        if not url:
            self._log("[Webhook] Merchant webhook is not configured.")
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
            "fields": [
                #{"name": "PID", "value": f"`{pid}`", "inline": True},
                #{"name": "Server", "value": f"`{server_label}`", "inline": True},
            ],
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
            enabled = (cf.name or "").lower() == "purple_text"
            if enabled:
                saw_purple = True
            purple_filters.append(ColorFilter(cf.name, cf.r, cf.g, cf.b, cf.tol, enabled))

        if not saw_purple:
            return True

        img_purple = preprocess_for_ocr(raw_img, purple_filters)
        try:
            text_lines = self._reader.readtext(np.array(img_purple), detail=0, paragraph=True)
            text = "\n".join(text_lines) if text_lines else ""
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

    # ------------------------ configuration -----------------------
    def _apply_cfg(self, ocr_settings: Dict[str, Any], ms_settings: Dict[str, Any]) -> None:
        self._ocr_cfg = ocr_settings or {}
        self._ms_cfg = ms_settings or {}

        self._filters = _filters_from_cfg(self._ocr_cfg.get("color_filters") or [])
        self._roi = _roi_from_cfg(self._ocr_cfg.get("roi") or {})
        self._workers = max(1, int(self._ocr_cfg.get("workers", 2) or 1))
        self._max_captures_per_second = max(1, int(self._ocr_cfg.get("max_captures_per_second", 20) or 1))
        self._cooldown_seconds = float(self._ocr_cfg.get("cooldown_seconds", 600) or 600)
        self._use_preprocess = bool(self._ocr_cfg.get("use_preprocess", True))

    def _init_reader(self):
        try:
            return easyocr.Reader(["en"], gpu=True)
        except Exception as e:
            raise RuntimeError(f"EasyOCR GPU init failed (GPU required): {e}")

    # ---------------------------- misc ----------------------------
    def _log(self, msg: str) -> None:
        clean = msg.strip()
        if not clean:
            return
        if clean == self._last_log:
            return
        self._last_log = clean
        self.log_signal.emit(clean)

    def __del__(self) -> None:  # pragma: no cover - destructor safety
        try:
            self._send_pool.shutdown(wait=False)
        except Exception:
            pass
