import sys
import copy
import base64
import hashlib
import json
import time
import os
import shutil
import uuid
import requests
import re
import threading
from collections import deque
from typing import Any, Dict, Set, List, Tuple, Optional
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from PIL import Image
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                            QHBoxLayout, QGridLayout, QTabWidget, QTableWidget,
                            QTableWidgetItem, QPushButton, QLabel, QLineEdit,
                            QSpinBox, QDoubleSpinBox, QSlider, QTextEdit, QGroupBox,
                            QComboBox, QCheckBox, QSplitter,
                            QAbstractSpinBox, QStyle, QStyleOptionSpinBox,
                            QHeaderView, QMessageBox, QDialog, QDialogButtonBox,
                            QFormLayout, QScrollArea, QSizePolicy, QFileDialog,
                            QAbstractScrollArea,
                            QAbstractItemView, QHeaderView, QScrollArea, QRubberBand,
                            QRadioButton, QListWidget, QListWidgetItem, QKeySequenceEdit)
from PySide6.QtCore import (
    QTimer,
    QThread,
    QRunnable,
    QThreadPool,
    Signal,
    QEvent,
    Qt,
    QSize,
    QBuffer,
    QByteArray,
    QIODevice,
    QPointF,
    QRect,
    QPoint,
    QAbstractNativeEventFilter,
    QEventLoop,
)
from PySide6.QtGui import QFont, QIcon, QColor, QPixmap, QMovie, QRegion, QPainter, QPainterPath, QImage, QTextCursor, QKeySequence, QBrush

try:
    from roblox_cookie_utils import (
        extract_roblosecurity_from_requests_response,
        extract_roblosecurity_from_selenium_driver,
        is_probably_roblosecurity,
        normalize_roblosecurity_cookie_value,
        persist_updated_cookie,
    )
except Exception:
    extract_roblosecurity_from_requests_response = None
    extract_roblosecurity_from_selenium_driver = None
    is_probably_roblosecurity = None
    normalize_roblosecurity_cookie_value = None
    persist_updated_cookie = None

# Windows system sounds
try:
    import winsound  # type: ignore
except Exception:
    winsound = None

# Cookie encryption (Windows AES-GCM via CNG/bcrypt.dll)
try:
    import ctypes
    from ctypes import wintypes
except Exception:
    ctypes = None
    wintypes = None

_BCRYPT_AVAILABLE = False
_bcrypt = None
_bcrypt_initialized = False

if os.name == "nt" and ctypes is not None:
    try:
        _bcrypt = ctypes.WinDLL("bcrypt")
        _BCRYPT_AVAILABLE = True
    except Exception:
        _bcrypt = None
        _BCRYPT_AVAILABLE = False

def _bcrypt_init() -> None:
    global _bcrypt_initialized
    if not _BCRYPT_AVAILABLE or _bcrypt is None:
        return
    if _bcrypt_initialized:
        return
    _bcrypt_initialized = True

    NTSTATUS = wintypes.LONG
    ULONG = wintypes.ULONG

    _bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ULONG,
    ]
    _bcrypt.BCryptOpenAlgorithmProvider.restype = NTSTATUS

    _bcrypt.BCryptCloseAlgorithmProvider.argtypes = [ctypes.c_void_p, ULONG]
    _bcrypt.BCryptCloseAlgorithmProvider.restype = NTSTATUS

    _bcrypt.BCryptSetProperty.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ULONG,
        ULONG,
    ]
    _bcrypt.BCryptSetProperty.restype = NTSTATUS

    _bcrypt.BCryptGetProperty.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ULONG,
        ctypes.POINTER(ULONG),
        ULONG,
    ]
    _bcrypt.BCryptGetProperty.restype = NTSTATUS

    _bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ULONG,
        ctypes.c_void_p,
        ULONG,
        ULONG,
    ]
    _bcrypt.BCryptGenerateSymmetricKey.restype = NTSTATUS

    _bcrypt.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
    _bcrypt.BCryptDestroyKey.restype = NTSTATUS

    _bcrypt.BCryptEncrypt.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ULONG,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ULONG,
        ctypes.c_void_p,
        ULONG,
        ctypes.POINTER(ULONG),
        ULONG,
    ]
    _bcrypt.BCryptEncrypt.restype = NTSTATUS

    _bcrypt.BCryptDecrypt.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ULONG,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ULONG,
        ctypes.c_void_p,
        ULONG,
        ctypes.POINTER(ULONG),
        ULONG,
    ]
    _bcrypt.BCryptDecrypt.restype = NTSTATUS

if ctypes is not None:
    class _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.ULONG),
            ("dwInfoVersion", wintypes.ULONG),
            ("pbNonce", ctypes.c_void_p),
            ("cbNonce", wintypes.ULONG),
            ("pbAuthData", ctypes.c_void_p),
            ("cbAuthData", wintypes.ULONG),
            ("pbTag", ctypes.c_void_p),
            ("cbTag", wintypes.ULONG),
            ("pbMacContext", ctypes.c_void_p),
            ("cbMacContext", wintypes.ULONG),
            ("cbAAD", wintypes.ULONG),
            ("cbData", ctypes.c_ulonglong),
            ("dwFlags", wintypes.ULONG),
        ]

def _bcrypt_check(status: int, context: str) -> None:
    if int(status) == 0:
        return
    raise OSError(int(status), f"{context} (NTSTATUS=0x{int(status) & 0xFFFFFFFF:08X})")

def _cng_aes_gcm_encrypt(*, key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> Tuple[bytes, bytes]:
    if not _BCRYPT_AVAILABLE or _bcrypt is None:
        raise RuntimeError("AES-GCM backend unavailable.")
    _bcrypt_init()
    if len(key) not in (16, 24, 32):
        raise ValueError("Invalid AES key length.")

    h_alg = ctypes.c_void_p()
    _bcrypt_check(_bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "BCryptOpenAlgorithmProvider")
    h_key = ctypes.c_void_p()
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        _bcrypt_check(
            _bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, ctypes.sizeof(mode), 0),
            "BCryptSetProperty(ChainingMode)",
        )

        obj_len = wintypes.ULONG()
        cb_result = wintypes.ULONG()
        _bcrypt_check(
            _bcrypt.BCryptGetProperty(
                h_alg, "ObjectLength", ctypes.byref(obj_len), ctypes.sizeof(obj_len), ctypes.byref(cb_result), 0
            ),
            "BCryptGetProperty(ObjectLength)",
        )
        key_object = ctypes.create_string_buffer(int(obj_len.value))
        key_buf = ctypes.create_string_buffer(key, len(key))
        _bcrypt_check(
            _bcrypt.BCryptGenerateSymmetricKey(
                h_alg, ctypes.byref(h_key), key_object, int(obj_len.value), key_buf, len(key), 0
            ),
            "BCryptGenerateSymmetricKey",
        )

        nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
        aad_buf = ctypes.create_string_buffer(aad, len(aad)) if aad else None
        tag_buf = ctypes.create_string_buffer(16)

        auth = _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
        auth.cbSize = ctypes.sizeof(_BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO)
        auth.dwInfoVersion = 1
        auth.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
        auth.cbNonce = len(nonce)
        if aad_buf is not None:
            auth.pbAuthData = ctypes.cast(aad_buf, ctypes.c_void_p)
            auth.cbAuthData = len(aad)
        else:
            auth.pbAuthData = None
            auth.cbAuthData = 0
        auth.pbTag = ctypes.cast(tag_buf, ctypes.c_void_p)
        auth.cbTag = 16
        auth.pbMacContext = None
        auth.cbMacContext = 0
        auth.cbAAD = 0
        auth.cbData = 0
        auth.dwFlags = 0

        in_buf = ctypes.create_string_buffer(plaintext, len(plaintext))
        out_buf = ctypes.create_string_buffer(len(plaintext))
        out_len = wintypes.ULONG()
        _bcrypt_check(
            _bcrypt.BCryptEncrypt(
                h_key,
                in_buf,
                len(plaintext),
                ctypes.byref(auth),
                None,
                0,
                out_buf,
                len(out_buf),
                ctypes.byref(out_len),
                0,
            ),
            "BCryptEncrypt",
        )
        return out_buf.raw[: int(out_len.value)], tag_buf.raw[:16]
    finally:
        try:
            if h_key:
                _bcrypt.BCryptDestroyKey(h_key)
        except Exception:
            pass
        try:
            if h_alg:
                _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)
        except Exception:
            pass

def _cng_aes_gcm_decrypt(*, key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes) -> bytes:
    if not _BCRYPT_AVAILABLE or _bcrypt is None:
        raise RuntimeError("AES-GCM backend unavailable.")
    _bcrypt_init()
    if len(key) not in (16, 24, 32):
        raise ValueError("Invalid AES key length.")

    h_alg = ctypes.c_void_p()
    _bcrypt_check(_bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "BCryptOpenAlgorithmProvider")
    h_key = ctypes.c_void_p()
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        _bcrypt_check(
            _bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, ctypes.sizeof(mode), 0),
            "BCryptSetProperty(ChainingMode)",
        )

        obj_len = wintypes.ULONG()
        cb_result = wintypes.ULONG()
        _bcrypt_check(
            _bcrypt.BCryptGetProperty(
                h_alg, "ObjectLength", ctypes.byref(obj_len), ctypes.sizeof(obj_len), ctypes.byref(cb_result), 0
            ),
            "BCryptGetProperty(ObjectLength)",
        )
        key_object = ctypes.create_string_buffer(int(obj_len.value))
        key_buf = ctypes.create_string_buffer(key, len(key))
        _bcrypt_check(
            _bcrypt.BCryptGenerateSymmetricKey(
                h_alg, ctypes.byref(h_key), key_object, int(obj_len.value), key_buf, len(key), 0
            ),
            "BCryptGenerateSymmetricKey",
        )

        nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
        aad_buf = ctypes.create_string_buffer(aad, len(aad)) if aad else None
        tag_buf = ctypes.create_string_buffer(tag, len(tag))

        auth = _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
        auth.cbSize = ctypes.sizeof(_BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO)
        auth.dwInfoVersion = 1
        auth.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
        auth.cbNonce = len(nonce)
        if aad_buf is not None:
            auth.pbAuthData = ctypes.cast(aad_buf, ctypes.c_void_p)
            auth.cbAuthData = len(aad)
        else:
            auth.pbAuthData = None
            auth.cbAuthData = 0
        auth.pbTag = ctypes.cast(tag_buf, ctypes.c_void_p)
        auth.cbTag = len(tag)
        auth.pbMacContext = None
        auth.cbMacContext = 0
        auth.cbAAD = 0
        auth.cbData = 0
        auth.dwFlags = 0

        in_buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        out_buf = ctypes.create_string_buffer(len(ciphertext))
        out_len = wintypes.ULONG()
        _bcrypt_check(
            _bcrypt.BCryptDecrypt(
                h_key,
                in_buf,
                len(ciphertext),
                ctypes.byref(auth),
                None,
                0,
                out_buf,
                len(out_buf),
                ctypes.byref(out_len),
                0,
            ),
            "BCryptDecrypt",
        )
        return out_buf.raw[: int(out_len.value)]
    finally:
        try:
            if h_key:
                _bcrypt.BCryptDestroyKey(h_key)
        except Exception:
            pass
        try:
            if h_alg:
                _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)
        except Exception:
            pass
# ---------- Qt6 enum shims (keep PyQt5-style constants working) ----------
# Paste directly below your current Qt imports.

# Alignment flags
if hasattr(Qt, "AlignmentFlag"):
    Qt.AlignLeft    = Qt.AlignmentFlag.AlignLeft
    Qt.AlignRight   = Qt.AlignmentFlag.AlignRight
    Qt.AlignHCenter = Qt.AlignmentFlag.AlignHCenter
    Qt.AlignVCenter = Qt.AlignmentFlag.AlignVCenter
    Qt.AlignCenter  = Qt.AlignmentFlag.AlignCenter
    Qt.NoFocus      = Qt.FocusPolicy.NoFocus

# Scrollbar policies
if hasattr(Qt, "ScrollBarPolicy"):
    Qt.ScrollBarAlwaysOff = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    Qt.ScrollBarAsNeeded  = Qt.ScrollBarPolicy.ScrollBarAsNeeded

# Table/list selection behavior/mode
if not hasattr(QAbstractItemView, "SelectRows"):
    QAbstractItemView.SelectRows      = QAbstractItemView.SelectionBehavior.SelectRows
if not hasattr(QAbstractItemView, "SingleSelection"):
    QAbstractItemView.SingleSelection = QAbstractItemView.SelectionMode.SingleSelection

# Header resize modes
if not hasattr(QHeaderView, "Stretch"):
    QHeaderView.Stretch          = QHeaderView.ResizeMode.Stretch
    QHeaderView.ResizeToContents = QHeaderView.ResizeMode.ResizeToContents
    QHeaderView.Interactive      = QHeaderView.ResizeMode.Interactive
    

class _WinHotkeyFilter(QAbstractNativeEventFilter):
    """
    Minimal WM_HOTKEY bridge for Windows (RegisterHotKey -> Qt callback).
    """

    WM_HOTKEY = 0x0312

    def __init__(self, on_hotkey):
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, eventType, message):
        try:
            et = eventType
            try:
                # Some Qt bindings can pass a QByteArray-like here.
                if isinstance(et, (bytes, bytearray)):
                    et_str = bytes(et).decode(errors="ignore")
                else:
                    et_str = str(et)
            except Exception:
                et_str = ""

            if "windows" not in et_str.lower():
                return False, 0

            import ctypes
            from ctypes import wintypes

            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if int(msg.message) == self.WM_HOTKEY:
                try:
                    self._on_hotkey(int(msg.wParam))
                except Exception:
                    pass
                return True, 0
        except Exception:
            pass
        return False, 0


class _FunctionRunnable(QRunnable):
    """Tiny QRunnable wrapper to execute a callable off the GUI thread."""

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            self._func(*self._args, **self._kwargs)
        except Exception:
            pass


from main import RobloxManager, ProcessManager, GameLauncher
from cookie_extractor import CookieExtractor
from RAM_export import transform         # re-use your parsing helper
from main import (
    limit_strap_helpers,
    limit_roblox_crash_handlers,
    limit_msedgewebview2_processes,
)
from log_utils import find_log_for_username, refresh_username_log_map
from biomes import biome_names, biome_meta, biome_duration
from utilities_tab import build_utilities_widget
from trimmer import setup_TRIMMER_tab
from found_stats import FoundStatsMixin
# Exclude NORMAL from the Settings table (still exists internally, we just don't offer it as a toggle)
GUI_BIOME_NAMES = [b for b in biome_names() if str(b).upper() != "NORMAL"]
from multiscope_process import MultiScopeProcessProxy
from antiafk import AntiAFK
from ocr_worker import (
    OCRWorker,
    enum_roblox_windows,
    capture_window_image,
    preprocess_for_ocr,
    ColorFilter,
    get_default_ocr_filters,
    get_ocr_device_summary,
    get_ocr_available_devices,
    compute_frame_hash,
    frame_hash_diff_percent,
)

OCR_MERCHANT_FILTER_IDS = {"merchant_jester", "merchant_mari", "merchant_rin"}
OCR_MERCHANT_FILTER_NAMES = {"jester", "mari", "rin", "white_text", "purple_text", "orange_text"}
MISC_DISABLE_LOG_MERCHANTS_WHEN_OCR_ACTIVE_KEY = (
    "disable_log_based_merchant_detection_when_ocr_merchants_enabled"
)


def _candidate_ocr_filters_from_cfg(ocr_cfg: object) -> List[dict]:
    if not isinstance(ocr_cfg, dict):
        return []

    raw_filters = ocr_cfg.get("filters")
    if isinstance(raw_filters, list) and raw_filters:
        return [item for item in raw_filters if isinstance(item, dict)]

    legacy_filters = ocr_cfg.get("color_filters")
    if isinstance(legacy_filters, list) and legacy_filters:
        return [item for item in legacy_filters if isinstance(item, dict)]

    try:
        defaults = get_default_ocr_filters()
    except Exception:
        defaults = []
    return [item for item in defaults if isinstance(item, dict)]


def _ocr_merchant_filters_enabled_in_cfg(ocr_cfg: object) -> bool:
    if not isinstance(ocr_cfg, dict):
        return False
    if not bool(ocr_cfg.get("enabled", False)):
        return False

    for spec in _candidate_ocr_filters_from_cfg(ocr_cfg):
        if not bool(spec.get("enabled", True)):
            continue
        filter_id = str(spec.get("id") or spec.get("filter_id") or "").strip()
        behavior = str(spec.get("behavior") or "").strip().lower()
        name = str(spec.get("name") or "").strip().lower()
        if (
            filter_id in OCR_MERCHANT_FILTER_IDS
            or behavior == "merchant"
            or name in OCR_MERCHANT_FILTER_NAMES
        ):
            return True
    return False


def _should_disable_log_based_merchant_detection(settings_cfg: object) -> bool:
    if not isinstance(settings_cfg, dict):
        return False
    misc_cfg = settings_cfg.get("misc")
    if not isinstance(misc_cfg, dict):
        misc_cfg = {}
    if not bool(misc_cfg.get(MISC_DISABLE_LOG_MERCHANTS_WHEN_OCR_ACTIVE_KEY, True)):
        return False
    return _ocr_merchant_filters_enabled_in_cfg(settings_cfg.get("ocr"))

# --- Auto Item engine (local module) ---
try:
    from auto_actions_automation import AutoActionEngine as AutoItemEngine  # type: ignore
except Exception:  # pragma: no cover
    AutoItemEngine = None  # type: ignore

# --- BES limiter (local module) ---
try:
    from bes_limiter import BESMultiProcessController  # type: ignore
except Exception:  # pragma: no cover
    BESMultiProcessController = None  # type: ignore

# --- minimal crash logging (EXE-safe, AV-friendly) ---
import os, sys, atexit, logging, logging.handlers, threading, warnings
from pathlib import Path

# Put logs in Jaram folder
_log_dir = Path(os.getenv("APPDATA", Path.home())) / "Jaram" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
LOGFILE = _log_dir / "JARAM.log"

_logger = logging.getLogger("jaram")
_logger.setLevel(logging.INFO)

# Rotate at ~1MB, keep 3 backups
_handler = logging.handlers.RotatingFileHandler(
    LOGFILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_handler.setFormatter(_formatter)
_logger.addHandler(_handler)

# Also capture warnings->logging (no consoles needed)
warnings.simplefilter("default")
logging.captureWarnings(True)

def _log_unhandled(exc_type, exc_value, exc_traceback):
    try:
        _logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    except Exception:
        pass

# Main-thread uncaught exceptions
sys.excepthook = _log_unhandled

# Python 3.8+ thread exceptions
def _thread_excepthook(args: threading.ExceptHookArgs):
    _log_unhandled(args.exc_type, args.exc_value, args.exc_traceback)
threading.excepthook = _thread_excepthook

# Python "unraisable" (e.g., __del__ errors)
def _unraisable_hook(unraisable):
    try:
        _logger.error("Unraisable exception: %r", unraisable.err_msg, exc_info=(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback))
    except Exception:
        pass
setattr(sys, "unraisablehook", _unraisable_hook)

# Clean shutdown of handlers
def _close_handlers():
    for h in list(_logger.handlers):
        try:
            h.flush()
            h.close()
        except Exception:
            pass
        _logger.removeHandler(h)
atexit.register(_close_handlers)
# --- end minimal crash logging ---

def _get_icon_path():

    icon_path = "JARAM.ico"
    if os.path.exists(icon_path):
        return icon_path

    if hasattr(sys, '_MEIPASS'):
        icon_path = os.path.join(sys._MEIPASS, "JARAM.ico")
        if os.path.exists(icon_path):
            return icon_path

    script_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(script_dir, "JARAM.ico")
    if os.path.exists(icon_path):
        return icon_path

    return None

# Lockout
_BM_RELAXED = False          # latched once sentinel/env found
_BM_LOCK_CONFIRMED = False   # latched once lock is enforced

def _bm_relaxed() -> bool:
    global _BM_RELAXED
    if _BM_RELAXED:
        return True
    try:
        if os.environ.get("JARAM_UNLOCK", "").strip() == "1":
            _BM_RELAXED = True
            return True
    except Exception:
        pass
    try:
        # Check for the sentinel in common launch locations:
        # - current working directory (when run from source)
        # - next to this file (when launched from another cwd)
        # - PyInstaller's temp extraction dir (onefile)
        candidates = [Path("JARAM.biu")]
        try:
            candidates.append(Path(__file__).resolve().with_name("JARAM.biu"))
        except Exception:
            pass
        try:
            import sys as _sys
            if getattr(_sys, "_MEIPASS", None):
                candidates.append(Path(getattr(_sys, "_MEIPASS")) / "JARAM.biu")
        except Exception:
            pass
        for sentinel in candidates:
            try:
                if sentinel.exists():
                    _BM_RELAXED = True
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _bm_lock_enforced() -> bool:
    """
    True when biome lock should be enforced (force Everyone on hard biomes).
    Uses a double-check to avoid accidental flips when the sentinel exists.
    """
    global _BM_LOCK_CONFIRMED
    try:
        if _bm_relaxed():
            if _BM_LOCK_CONFIRMED:
                _BM_LOCK_CONFIRMED = False
            return False

        if _BM_LOCK_CONFIRMED:
            return True

        # Second pass before enforcing lock to avoid flapping on transient misses.
        if _bm_relaxed():
            _BM_LOCK_CONFIRMED = False
            return False

        _BM_LOCK_CONFIRMED = True
        return True
    except Exception:
        # Fail closed if anything unexpected happens.
        return True

_COOKIE_ENC_PREFIX_V1 = "enc_v1:"  # legacy (unsupported; reset required)
_COOKIE_ENC_PREFIX_V2 = "enc_v2:"  # AES-GCM (password based)
_COOKIE_ENC_SENTINEL = "JARAM_COOKIE_VERIFIER_v1"
_COOKIE_KDF_ITERS_DEFAULT = 200000
_COOKIE_KDF_SALT_LEN = 16
_COOKIE_GCM_NONCE_LEN = 12
_COOKIE_GCM_TAG_LEN = 16
_COOKIE_GCM_AAD = b"JARAM_COOKIE_v2"
_COOKIE_ENC_META_KEY = "__cookie_encryption__"

class ConfigManager:
    _cookie_entropy: Optional[bytes] = None
    _cookie_entropy_lock = threading.Lock()
    _cookie_unlock_token = 0

    def __init__(self):
        self.app_name = "JARAM"  
        self.config_dir = self._get_config_directory()
        self.users_file = self.config_dir / "users.json"
        self.settings_file = self.config_dir / "settings.json"
        self.backup_dir = self.config_dir / "backups"
        

        self._ensure_directories()

        self.default_settings = {
            "window_limit": 1,
            "spares_mode": False,            # ← NEW: launch ~half of good accounts
            "spares_fraction": "1/2",
            "timeouts": {
                "strap_threshold": 10,
                "offline": 25,
                "launch_delay": 10,
                "initial_delay": 10,
                "kill_timeout": 1740,
                "poll_interval": 10,
                "handoff_lead": 25,          # ← NEW: seconds before kill to pre-join spare
                "early_join_window": 90,     # ← NEW: lookahead to avoid backlog spikes
                "webhook_url": "",
                "ping_message": "",
            },
            "alerts": {
                "webhook_url": "",
                "blackout_ping": "",
                "cap_message": "",
                "bad_message": "",
                "hourly_users_report_enabled": False,
                "hourly_users_report_interval_hours": 1,
            },
            "multiscope": {
                "webhooks": [],   # ← NEW
                "enable_jester": True,
                "enable_mari": True,
                "enable_rin": True,
                "merchant_detection_mode": "asset_id",
                "jester_ping": "",
                "mari_ping": "",
                "rin_ping": "",
                "merchant_rate_limit": 15,   # seconds (global cooldown for merchant alerts)
                "biome_min_interval": 2,     # seconds per server (dampen bursts)
            },
            "ocr": {
                "enabled": False,             # current desired state
                "only_mapped_pids": False,
                "workers": 1,
                "max_captures_per_second": 20,
                "batch_delay_seconds": 1.0,
                "use_preprocess": True,
                "frame_diff_tolerance": 2,    # percent (skip OCR if frame changes <= this)
                "log_ocr_text": False,        # debug: include OCR text in OCR log
                "log_loop": True,             # include per-loop "[Loop N]" logs in OCR log
                "device_id": None,            # None => auto/default
                "roi": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
                "shared_areas": [],
                "filters": get_default_ocr_filters(),
            },
            "misc": {
                "skip_webhook_unknown_context": True,
                "disable_log_based_merchant_detection_when_ocr_merchants_enabled": True,
                "log_confirmed_launch_mode": False,
                "disable_manager_bad_marking": False,
                "msedgewebview2_limiter_enabled": True,
            },
            "ui": {
                "show_tutorial_menu": False,
                "webhooks_hidden_biomes": [],
                "show_selected_sets_bes_exempt_slot1": False,
            },
            "roblox_window_geometry": {
                # When enabled, each newly launched Roblox window gets a one-time size/position check.
                # If it differs from the recorded geometry, we move/resize it to match.
                "enforce_on_launch": False,
                "x": 0,
                "y": 0,
                "w": 0,
                "h": 0,
            },
            "cookie_encryption": {
                "enabled": False,
                "prompted": False,
                "kdf_salt": "",
                "kdf_iters": _COOKIE_KDF_ITERS_DEFAULT,
                "verifier": "",
                "version": 2,
            },

            "antiafk": {
                "antiafk_enabled": False,
                "multi_instance_enabled": False,
                "antiafk_interval": 120,
                "antiafk_action": "space",
                "antiafk_alt_delay_ms": 400,
                "antiafk_dev_mode": False,
                "antiafk_menu_autoreconnect": False,
                # Alerts shortly before Anti-AFK runs.
                "antiafk_alert_sound": False,
                "antiafk_alert_tooltip": False,
                "antiafk_alert_lead_s": 3.0,
                # BES integration: optionally unthrottle targets shortly before sending inputs.
                "antiafk_unthrottle_enabled": False,
                "antiafk_unthrottle_batch_size": 5,
                "antiafk_unthrottle_lead_s": 3.0,
            },

              "auto_item": {
                   "enabled": False,
                   "tick_interval": 1.0,
                   "click_delay": 0.2,
                   "disable_mouse_move": False,
                   "toggle_hotkey": "Ctrl+Alt+Space",
                   "users": [],
                   "presets": [],
                   "items": [],
              },

            "bes": {
                "enabled": False,
                "cycle_ms": 50,
                "menu_throttle_percent": 85,
                "game_throttle_percent": 50,
                # Exactly 3 slots (strings). Empty/None => unused.
                "exempt_users": ["", "", ""],
                # Auto Item integration: unthrottle lead seconds before actions.
                "auto_item_lead_s": 3.0,
                # Keep unthrottled briefly after actions (small grace).
                "auto_item_grace_s": 1.0,
            },

            "trimmer": {
                "enabled": False,
                "interval_s": 15,
                "use_threshold": True,
                "threshold_mb": 1024.0,
            },

        }


        self.default_user_structure = {
            "username": "",
            "cookie": "",
            "private_server_link": "",
            "place": "",
            "bad": False,
            "cap": False,
            "disabled": False,
            "alternate_launch": False,
            "skip_reconnect_on_log_disconnect": False,
        }

        # Re-entrant because some cache update paths call helper methods that
        # also consult cached settings/users.
        self._cache_lock = threading.RLock()
        self._users_cache_mtime: float = -1.0
        self._users_cache: dict = {}
        self._users_cache_unlock_token: int = -1
        self._raw_cookie_cache: dict = {}
        self._settings_cache_mtime: float = -1.0
        self._settings_cache: dict = {}
        self._cookie_last_error: str = ""
        self._users_file_cookie_meta: dict = {}
        self._users_file_has_encrypted_cookies: bool = False
        self._prime_users_file_cookie_encryption_meta()
        

    def _get_config_directory(self):
        if os.name == 'nt':  
            appdata = os.environ.get('APPDATA')
            if appdata:
                return Path(appdata) / self.app_name

        return Path.home() / f".{self.app_name.lower()}"

    def _ensure_directories(self):
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            pass

    def _create_backup(self, file_path):
        if not file_path.exists():
            return

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{file_path.stem}_{timestamp}.json"
            backup_path = self.backup_dir / backup_name

            shutil.copy2(file_path, backup_path)

            self._cleanup_old_backups(file_path.stem)
        except Exception as e:
            pass

    def _cleanup_old_backups(self, file_stem):
        try:
            pattern = f"{file_stem}_*.json"
            backups = sorted(self.backup_dir.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)

            for backup in backups[10:]:
                backup.unlink()
        except Exception as e:
            pass

    def _safe_write_json(self, file_path, data):
        temp_path = file_path.with_suffix('.tmp')

        try:

            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            if os.name == 'nt':  
                if file_path.exists():
                    file_path.unlink()
                temp_path.rename(file_path)
            else:
                temp_path.rename(file_path)

            return True
        except Exception as e:

            if temp_path.exists():
                temp_path.unlink()
            raise e
    # ADD this helper anywhere inside the class
    def _deep_update(self, base: dict, updates: dict):
        """Recursive dict.update so nested keys survive partial files."""
        for k, v in updates.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                base[k] = self._deep_update(base[k], v)
            else:
                base[k] = v
        return base

    @classmethod
    def _get_cookie_unlock_token(cls) -> int:
        with cls._cookie_entropy_lock:
            return cls._cookie_unlock_token

    @classmethod
    def _set_cookie_entropy(cls, entropy: Optional[bytes]) -> None:
        with cls._cookie_entropy_lock:
            cls._cookie_entropy = entropy
            cls._cookie_unlock_token += 1

    @classmethod
    def _get_cookie_entropy(cls) -> Optional[bytes]:
        with cls._cookie_entropy_lock:
            return cls._cookie_entropy

    @classmethod
    def is_cookie_unlocked(cls) -> bool:
        return cls._get_cookie_entropy() is not None

    def _set_cookie_error(self, message: str) -> None:
        self._cookie_last_error = str(message or "")

    def get_cookie_error(self) -> str:
        return str(self._cookie_last_error or "")

    def cookie_encryption_available(self) -> bool:
        return bool(_BCRYPT_AVAILABLE)

    def _split_users_payload(self, payload: object) -> Tuple[dict, dict]:
        meta: dict = {}
        users: dict = {}
        if isinstance(payload, dict):
            users = dict(payload)
            meta_val = users.pop(_COOKIE_ENC_META_KEY, None)
            if isinstance(meta_val, dict):
                meta = dict(meta_val)
        return meta, users

    def _merge_users_payload(self, users_data: dict, meta: Optional[dict]) -> dict:
        payload = dict(users_data or {})
        if isinstance(meta, dict) and meta:
            payload[_COOKIE_ENC_META_KEY] = dict(meta)
        return payload

    def _prime_users_file_cookie_encryption_meta(self) -> None:
        try:
            self._users_file_cookie_meta = {}
            self._users_file_has_encrypted_cookies = False
            if not getattr(self, "users_file", None) or not self.users_file.exists():
                return
            with open(self.users_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            meta, users = self._split_users_payload(loaded)
            self._users_file_cookie_meta = dict(meta) if isinstance(meta, dict) else {}
            formatted = self._ensure_new_format(users if isinstance(users, dict) else {})
            self._users_file_has_encrypted_cookies = self._users_data_has_encrypted_cookies(formatted)
        except Exception:
            self._users_file_cookie_meta = {}
            self._users_file_has_encrypted_cookies = False

    def _build_cookie_file_meta(self, cfg: Optional[dict] = None) -> dict:
        src = cfg if isinstance(cfg, dict) else self._get_cookie_encryption_settings()
        existing = self._users_file_cookie_meta if isinstance(self._users_file_cookie_meta, dict) else {}
        meta: dict = {}
        if bool(src.get("enabled", False)):
            meta["enabled"] = True
        try:
            meta["version"] = int(src.get("version") or existing.get("version") or 2)
        except Exception:
            meta["version"] = int(existing.get("version") or 2)
        salt = str(src.get("kdf_salt") or existing.get("kdf_salt") or "")
        if salt:
            meta["kdf_salt"] = salt
        try:
            iters_val = src.get("kdf_iters", existing.get("kdf_iters"))
            if iters_val is not None and str(iters_val).strip() != "":
                meta["kdf_iters"] = int(iters_val)
        except Exception:
            pass
        verifier = str(src.get("verifier") or existing.get("verifier") or "")
        if verifier:
            meta["verifier"] = verifier
        return meta

    def _get_cookie_encryption_settings(self, settings: Optional[dict] = None) -> dict:
        base = settings if isinstance(settings, dict) else (self.peek_settings() or {})
        defaults = self.default_settings.get("cookie_encryption", {})
        cfg = base.get("cookie_encryption", {})
        out = dict(defaults) if isinstance(defaults, dict) else {}
        if isinstance(cfg, dict):
            out.update(cfg)

        meta = self._users_file_cookie_meta if isinstance(self._users_file_cookie_meta, dict) else {}
        if isinstance(meta, dict) and meta:
            if meta.get("enabled") is True:
                out["enabled"] = True
            if meta.get("kdf_salt"):
                out["kdf_salt"] = str(meta.get("kdf_salt") or "")
            if meta.get("kdf_iters") is not None and str(meta.get("kdf_iters")).strip() != "":
                try:
                    out["kdf_iters"] = int(meta.get("kdf_iters"))
                except Exception:
                    pass
            if meta.get("verifier"):
                out["verifier"] = str(meta.get("verifier") or "")
            if meta.get("version") is not None and str(meta.get("version")).strip() != "":
                try:
                    out["version"] = int(meta.get("version"))
                except Exception:
                    pass

        if not bool(out.get("enabled", False)) and bool(getattr(self, "_users_file_has_encrypted_cookies", False)):
            out["enabled"] = True
        return out

    def cookie_encryption_enabled(self) -> bool:
        return bool(self._get_cookie_encryption_settings().get("enabled", False))

    def cookie_encryption_prompted(self) -> bool:
        cfg = self._get_cookie_encryption_settings()
        if bool(cfg.get("enabled", False)):
            return True
        return bool(cfg.get("prompted", False))

    def set_cookie_encryption_prompted(self, prompted: bool) -> bool:
        settings = self.load_settings()
        cfg = self._get_cookie_encryption_settings(settings)
        cfg["prompted"] = bool(prompted)
        settings["cookie_encryption"] = cfg
        return bool(self.save_settings(settings))

    def update_cookie_encryption_settings(self, **updates) -> bool:
        settings = self.load_settings()
        cfg = self._get_cookie_encryption_settings(settings)
        cfg.update(updates)
        settings["cookie_encryption"] = cfg
        return bool(self.save_settings(settings))

    def _derive_cookie_entropy(self, password: str, cfg: dict) -> Optional[bytes]:
        if not password:
            self._set_cookie_error("Password is required.")
            return None
        salt_b64 = str(cfg.get("kdf_salt") or "")
        if not salt_b64:
            self._set_cookie_error("Missing encryption salt.")
            return None
        try:
            salt = base64.b64decode(salt_b64)
        except Exception:
            self._set_cookie_error("Invalid encryption salt.")
            return None
        try:
            iters = int(cfg.get("kdf_iters") or _COOKIE_KDF_ITERS_DEFAULT)
        except Exception:
            iters = _COOKIE_KDF_ITERS_DEFAULT
        try:
            return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=32)
        except Exception as e:
            self._set_cookie_error(f"Failed to derive key: {e}")
            return None

    def _encrypt_bytes_v2(self, data: bytes, entropy: bytes) -> Optional[bytes]:
        if not _BCRYPT_AVAILABLE:
            self._set_cookie_error("Cookie encryption is not available on this system.")
            return None
        try:
            nonce = os.urandom(_COOKIE_GCM_NONCE_LEN)
            ciphertext, tag = _cng_aes_gcm_encrypt(key=entropy, nonce=nonce, plaintext=data, aad=_COOKIE_GCM_AAD)
            return nonce + tag + ciphertext
        except Exception as e:
            self._set_cookie_error(f"Cookie encryption failed: {e}")
            return None

    def _decrypt_bytes_v2(self, data: bytes, entropy: bytes) -> Optional[bytes]:
        if not _BCRYPT_AVAILABLE:
            self._set_cookie_error("Cookie decryption is not available on this system.")
            return None
        blob = bytes(data or b"")
        if len(blob) < (_COOKIE_GCM_NONCE_LEN + _COOKIE_GCM_TAG_LEN):
            self._set_cookie_error("Invalid encrypted cookie data.")
            return None
        nonce = blob[:_COOKIE_GCM_NONCE_LEN]
        tag = blob[_COOKIE_GCM_NONCE_LEN:_COOKIE_GCM_NONCE_LEN + _COOKIE_GCM_TAG_LEN]
        ciphertext = blob[_COOKIE_GCM_NONCE_LEN + _COOKIE_GCM_TAG_LEN:]
        try:
            return _cng_aes_gcm_decrypt(key=entropy, nonce=nonce, ciphertext=ciphertext, tag=tag, aad=_COOKIE_GCM_AAD)
        except Exception:
            self._set_cookie_error("Incorrect password or corrupted encrypted data.")
            return None

    def _make_cookie_verifier(self, entropy: bytes, cfg: dict) -> Optional[str]:
        blob = self._encrypt_bytes_v2(_COOKIE_ENC_SENTINEL.encode("utf-8"), entropy)
        if blob is None:
            return None
        return base64.b64encode(blob).decode("ascii")

    def _verify_cookie_verifier(self, entropy: bytes, cfg: dict) -> bool:
        verifier_b64 = str(cfg.get("verifier") or "")
        if not verifier_b64:
            self._set_cookie_error("Missing cookie verifier.")
            return False
        try:
            blob = base64.b64decode(verifier_b64)
        except Exception:
            self._set_cookie_error("Invalid cookie verifier.")
            return False
        plain = self._decrypt_bytes_v2(blob, entropy)
        if plain is None:
            return False
        try:
            return plain.decode("utf-8") == _COOKIE_ENC_SENTINEL
        except Exception:
            return False

    def _is_cookie_encrypted(self, value: str) -> bool:
        raw = str(value or "")
        return raw.startswith(_COOKIE_ENC_PREFIX_V1) or raw.startswith(_COOKIE_ENC_PREFIX_V2)

    def _encrypt_cookie_value(self, value: str, entropy: bytes) -> Optional[str]:
        if not value:
            return ""
        if self._is_cookie_encrypted(value):
            return value
        blob = self._encrypt_bytes_v2(value.encode("utf-8"), entropy)
        if blob is None:
            return None
        return _COOKIE_ENC_PREFIX_V2 + base64.b64encode(blob).decode("ascii")

    def _decrypt_cookie_value(self, value: str, entropy: bytes) -> Optional[str]:
        if not value:
            return ""
        raw = str(value or "")
        if raw.startswith(_COOKIE_ENC_PREFIX_V2):
            b64 = raw[len(_COOKIE_ENC_PREFIX_V2):]
            try:
                blob = base64.b64decode(b64)
            except Exception:
                self._set_cookie_error("Invalid encrypted cookie data.")
                return None
            plain = self._decrypt_bytes_v2(blob, entropy)
            if plain is None:
                return None
            try:
                return plain.decode("utf-8")
            except Exception:
                return plain.decode("utf-8", errors="replace")

        if raw.startswith(_COOKIE_ENC_PREFIX_V1):
            self._set_cookie_error(
                "Legacy cookie encryption is no longer supported. Use Cookie Encryption -> Reset (Clear Cookies)."
            )
            return None

        return value

    def _users_data_has_encrypted_cookies(self, users_data: dict) -> bool:
        for info in (users_data or {}).values():
            if not isinstance(info, dict):
                continue
            if self._is_cookie_encrypted(str(info.get("cookie") or "")):
                return True
        return False

    def _encrypt_users_cookies(self, users_data: dict, entropy: bytes) -> Optional[dict]:
        data = copy.deepcopy(users_data or {})
        for uid, info in data.items():
            if not isinstance(info, dict):
                continue
            cookie = str(info.get("cookie") or "")
            if not cookie:
                continue
            encrypted = self._encrypt_cookie_value(cookie, entropy)
            if encrypted is None:
                return None
            info["cookie"] = encrypted
        return data

    def _decrypt_users_cookies(self, users_data: dict, entropy: bytes, *, blank_on_fail: bool) -> Optional[dict]:
        data = copy.deepcopy(users_data or {})
        for uid, info in data.items():
            if not isinstance(info, dict):
                continue
            cookie = str(info.get("cookie") or "")
            if not cookie:
                continue
            decrypted = self._decrypt_cookie_value(cookie, entropy)
            if decrypted is None:
                if blank_on_fail:
                    info["cookie"] = ""
                    continue
                return None
            info["cookie"] = decrypted
        return data

    def _load_users_raw_from_disk(self) -> dict:
        try:
            if self.users_file.exists():
                with open(self.users_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                meta, users = self._split_users_payload(loaded)
                self._users_file_cookie_meta = dict(meta) if isinstance(meta, dict) else {}
                formatted = self._ensure_new_format(users if isinstance(users, dict) else {})
                self._users_file_has_encrypted_cookies = self._users_data_has_encrypted_cookies(formatted)
                return formatted
        except Exception:
            pass
        self._users_file_cookie_meta = {}
        self._users_file_has_encrypted_cookies = False
        return {}

    def _extract_raw_cookie_map(self, users_data: dict) -> dict:
        out: dict = {}
        for uid, info in (users_data or {}).items():
            if not isinstance(info, dict):
                continue
            out[str(uid)] = str(info.get("cookie") or "")
        return out

    def _update_raw_cookie_cache(self, users_data: dict) -> None:
        try:
            self._raw_cookie_cache = self._extract_raw_cookie_map(users_data)
        except Exception:
            self._raw_cookie_cache = {}

    def _apply_cookie_encryption_on_load(self, users_data: dict) -> dict:
        data = copy.deepcopy(users_data or {})
        self._update_raw_cookie_cache(data)
        cfg = self._get_cookie_encryption_settings()
        if not cfg.get("enabled", False):
            return data
        entropy = self._get_cookie_entropy()
        for uid, info in data.items():
            if not isinstance(info, dict):
                continue
            cookie = str(info.get("cookie") or "")
            if not cookie:
                continue
            if self._is_cookie_encrypted(cookie):
                if entropy is None:
                    info["cookie"] = ""
                    continue
                decrypted = self._decrypt_cookie_value(cookie, entropy)
                info["cookie"] = decrypted if decrypted is not None else ""
        return data

    def _write_users_backup_from_data(self, data: dict) -> None:
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{self.users_file.stem}_{timestamp}.json"
            backup_path = self.backup_dir / backup_name
            payload = data
            if self.cookie_encryption_enabled():
                meta = self._build_cookie_file_meta()
                payload = self._merge_users_payload(data, meta)
            self._safe_write_json(backup_path, payload)
            self._cleanup_old_backups(self.users_file.stem)
        except Exception:
            pass

    def _iter_users_backup_files(self) -> List[Path]:
        try:
            pattern = f"{self.users_file.stem}_*.json"
            return sorted(self.backup_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
        except Exception:
            return []

    def encrypt_existing_users_backups(self, entropy: Optional[bytes] = None) -> Optional[Tuple[int, int, int]]:
        """
        Encrypt plaintext cookie values inside existing users_*.json backups in the backups folder.

        Returns (scanned_files, updated_files, failed_files) on success, or None on hard failure.
        """
        self._set_cookie_error("")
        if entropy is None:
            entropy = self._get_cookie_entropy()
        if entropy is None:
            self._set_cookie_error("Cookies are locked. Unlock first.")
            return None

        scanned = 0
        updated = 0
        failed = 0

        for backup_path in self._iter_users_backup_files():
            scanned += 1
            try:
                with open(backup_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception:
                failed += 1
                continue
            if not isinstance(loaded, dict):
                continue

            file_meta, users = self._split_users_payload(loaded)
            data = self._ensure_new_format(users if isinstance(users, dict) else {})

            sample_encrypted = None
            for info in (data or {}).values():
                if not isinstance(info, dict):
                    continue
                cookie = str(info.get("cookie") or "")
                if cookie and self._is_cookie_encrypted(cookie):
                    sample_encrypted = cookie
                    break

            # Avoid mixing keys: if the backup already contains encrypted cookies,
            # only modify it if those cookies are decryptable with the current key.
            if sample_encrypted is not None and self._decrypt_cookie_value(sample_encrypted, entropy) is None:
                failed += 1
                continue

            modified = False
            file_failed = False
            for uid, info in (data or {}).items():
                if not isinstance(info, dict):
                    continue
                cookie = str(info.get("cookie") or "")
                if not cookie or self._is_cookie_encrypted(cookie):
                    continue
                encrypted = self._encrypt_cookie_value(cookie, entropy)
                if encrypted is None:
                    file_failed = True
                    break
                info["cookie"] = encrypted
                modified = True

            if file_failed:
                failed += 1
                continue

            needs_meta = False
            if modified:
                needs_meta = True
            elif sample_encrypted is not None:
                if not (isinstance(file_meta, dict) and file_meta.get("kdf_salt") and file_meta.get("verifier")):
                    needs_meta = True

            if not modified and not needs_meta:
                continue
            try:
                payload = data
                if modified or needs_meta:
                    meta = self._build_cookie_file_meta()
                    payload = self._merge_users_payload(data, meta)
                self._safe_write_json(backup_path, payload)
                updated += 1
            except Exception as e:
                self._set_cookie_error(f"Failed to write backup {backup_path.name}: {e}")
                failed += 1

        return scanned, updated, failed

    def decrypt_existing_users_backups(self, entropy: Optional[bytes] = None) -> Optional[Tuple[int, int, int, int]]:
        """
        Decrypt encrypted cookie values inside existing users_*.json backups in the backups folder.

        Returns (scanned_files, updated_files, skipped_files, failed_files) on success, or None on hard failure.
        """
        self._set_cookie_error("")
        if entropy is None:
            entropy = self._get_cookie_entropy()
        if entropy is None:
            self._set_cookie_error("Cookies are locked. Unlock first.")
            return None

        scanned = 0
        updated = 0
        skipped = 0
        failed = 0

        for backup_path in self._iter_users_backup_files():
            scanned += 1
            try:
                with open(backup_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception:
                failed += 1
                continue
            if not isinstance(loaded, dict):
                continue

            file_meta, users = self._split_users_payload(loaded)
            data = self._ensure_new_format(users if isinstance(users, dict) else {})
            meta_present = bool(file_meta)
            modified = False
            file_skipped = False
            for uid, info in (data or {}).items():
                if not isinstance(info, dict):
                    continue
                cookie = str(info.get("cookie") or "")
                if not cookie or not self._is_cookie_encrypted(cookie):
                    continue
                decrypted = self._decrypt_cookie_value(cookie, entropy)
                if decrypted is None:
                    file_skipped = True
                    break
                info["cookie"] = decrypted
                modified = True

            if file_skipped:
                skipped += 1
                continue
            if not modified and not meta_present:
                continue
            try:
                self._safe_write_json(backup_path, data)
                updated += 1
            except Exception as e:
                self._set_cookie_error(f"Failed to write backup {backup_path.name}: {e}")
                failed += 1

        return scanned, updated, skipped, failed

    def _create_users_backup(self) -> None:
        if not self.users_file.exists():
            return
        cfg = self._get_cookie_encryption_settings()
        if not cfg.get("enabled", False):
            self._create_backup(self.users_file)
            return
        raw = self._load_users_raw_from_disk()
        if not raw:
            self._create_backup(self.users_file)
            return
        raw = self._ensure_new_format(raw)
        has_encrypted = self._users_data_has_encrypted_cookies(raw)
        entropy = self._get_cookie_entropy()
        # When cookie encryption is enabled, backups are always stored encrypted.
        # If users.json is already encrypted (common case), a straight copy is enough.
        if has_encrypted or entropy is None:
            self._create_backup(self.users_file)
            return
        encrypted = self._encrypt_users_cookies(raw, entropy)
        if encrypted is None:
            self._create_backup(self.users_file)
            return
        self._write_users_backup_from_data(encrypted)

    def unlock_cookie_encryption(self, password: str) -> bool:
        self._set_cookie_error("")
        if not self.cookie_encryption_enabled():
            self._set_cookie_error("Cookie encryption is not enabled.")
            return False
        cfg = self._get_cookie_encryption_settings()
        version = 2
        try:
            version = int(cfg.get("version") or 2)
        except Exception:
            version = 2
        if version < 2:
            self._set_cookie_error(
                "Legacy cookie encryption is no longer supported. Use Cookie Encryption -> Reset (Clear Cookies)."
            )
            return False
        if not _BCRYPT_AVAILABLE:
            self._set_cookie_error("Cookie encryption is not available on this system.")
            return False
        entropy = self._derive_cookie_entropy(password, cfg)
        if entropy is None:
            return False
        if not self._verify_cookie_verifier(entropy, cfg):
            if not self.get_cookie_error():
                self._set_cookie_error("Incorrect password.")
            return False
        self._set_cookie_entropy(entropy)
        try:
            current_meta = self._users_file_cookie_meta if isinstance(self._users_file_cookie_meta, dict) else {}
            if not (current_meta.get("kdf_salt") and current_meta.get("verifier")) and self.users_file.exists():
                with open(self.users_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    _, users_payload = self._split_users_payload(loaded)
                    new_meta = self._build_cookie_file_meta(cfg)
                    self._users_file_cookie_meta = new_meta
                    self._users_file_has_encrypted_cookies = True
                    payload = self._merge_users_payload(users_payload if isinstance(users_payload, dict) else {}, new_meta)
                    self._safe_write_json(self.users_file, payload)
        except Exception:
            pass
        return True

    def lock_cookie_encryption(self) -> None:
        self._set_cookie_entropy(None)

    def enable_cookie_encryption(self, password: str) -> bool:
        self._set_cookie_error("")
        if not _BCRYPT_AVAILABLE:
            self._set_cookie_error("Cookie encryption is not available on this system.")
            return False
        if self.cookie_encryption_enabled():
            self._set_cookie_error("Cookie encryption is already enabled.")
            return False
        salt = os.urandom(_COOKIE_KDF_SALT_LEN)
        cfg = self._get_cookie_encryption_settings()
        cfg.update({
            "enabled": True,
            "prompted": True,
            "kdf_salt": base64.b64encode(salt).decode("ascii"),
            "kdf_iters": _COOKIE_KDF_ITERS_DEFAULT,
            "version": 2,
        })
        entropy = self._derive_cookie_entropy(password, cfg)
        if entropy is None:
            return False
        verifier = self._make_cookie_verifier(entropy, cfg)
        if verifier is None:
            return False
        cfg["verifier"] = verifier

        settings = self.load_settings()
        settings["cookie_encryption"] = cfg
        if not self.save_settings(settings):
            self._set_cookie_error("Failed to save encryption settings.")
            return False

        self._set_cookie_entropy(entropy)
        raw = self._load_users_raw_from_disk()
        raw = self._ensure_new_format(raw)
        encrypted = self._encrypt_users_cookies(raw, entropy)
        if encrypted is None:
            return False
        meta = self._build_cookie_file_meta(cfg)
        self._users_file_cookie_meta = meta
        self._users_file_has_encrypted_cookies = self._users_data_has_encrypted_cookies(encrypted)
        if raw:
            self._write_users_backup_from_data(encrypted)
        try:
            payload = self._merge_users_payload(encrypted, meta)
            self._safe_write_json(self.users_file, payload)
        except Exception as e:
            self._set_cookie_error(f"Failed to write users.json: {e}")
            return False
        try:
            with self._cache_lock:
                self._update_raw_cookie_cache(encrypted)
                self._users_cache = self._apply_cookie_encryption_on_load(copy.deepcopy(encrypted))
                self._users_cache_mtime = self._file_mtime(self.users_file)
                self._users_cache_unlock_token = self._get_cookie_unlock_token()
        except Exception:
            pass
        return True

    def disable_cookie_encryption(self) -> bool:
        self._set_cookie_error("")
        if not self.cookie_encryption_enabled():
            self._set_cookie_error("Cookie encryption is not enabled.")
            return False
        entropy = self._get_cookie_entropy()
        if entropy is None:
            self._set_cookie_error("Cookies are locked. Unlock first.")
            return False
        raw = self._load_users_raw_from_disk()
        raw = self._ensure_new_format(raw)
        decrypted = self._decrypt_users_cookies(raw, entropy, blank_on_fail=False)
        if decrypted is None:
            return False

        if raw:
            self._create_users_backup()

        try:
            self._users_file_cookie_meta = {}
            self._users_file_has_encrypted_cookies = False
            self._safe_write_json(self.users_file, decrypted)
        except Exception as e:
            self._set_cookie_error(f"Failed to write users.json: {e}")
            return False
        settings = self.load_settings()
        cfg = self._get_cookie_encryption_settings(settings)
        cfg.update({
            "enabled": False,
            "kdf_salt": "",
            "kdf_iters": _COOKIE_KDF_ITERS_DEFAULT,
            "verifier": "",
            "version": 2,
            "prompted": True,
        })
        settings["cookie_encryption"] = cfg
        if not self.save_settings(settings):
            self._set_cookie_error("Failed to save encryption settings.")
            return False

        self._set_cookie_entropy(None)
        try:
            with self._cache_lock:
                self._update_raw_cookie_cache(decrypted)
                self._users_cache = dict(decrypted or {})
                self._users_cache_mtime = self._file_mtime(self.users_file)
                self._users_cache_unlock_token = self._get_cookie_unlock_token()
        except Exception:
            pass
        return True

    def reset_cookie_encryption(self) -> bool:
        """
        Emergency recovery: disables cookie encryption without the password.

        Because the cookies can't be decrypted without the password, any encrypted
        cookie values are cleared.
        """
        self._set_cookie_error("")
        if not self.cookie_encryption_enabled():
            self._set_cookie_error("Cookie encryption is not enabled.")
            return False

        raw = self._load_users_raw_from_disk()
        raw = self._ensure_new_format(raw)
        cleaned = copy.deepcopy(raw or {})
        for uid, info in (cleaned or {}).items():
            if not isinstance(info, dict):
                continue
            cookie = str(info.get("cookie") or "")
            if self._is_cookie_encrypted(cookie):
                info["cookie"] = ""

        try:
            # Preserve the on-disk encrypted file for potential later recovery.
            self._create_backup(self.users_file)
        except Exception:
            pass

        try:
            self._users_file_cookie_meta = {}
            self._users_file_has_encrypted_cookies = False
            self._safe_write_json(self.users_file, cleaned)
        except Exception as e:
            self._set_cookie_error(f"Failed to write users.json: {e}")
            return False

        settings = self.load_settings()
        cfg = self._get_cookie_encryption_settings(settings)
        cfg.update({
            "enabled": False,
            "kdf_salt": "",
            "kdf_iters": _COOKIE_KDF_ITERS_DEFAULT,
            "verifier": "",
            "version": 2,
            "prompted": True,
        })
        settings["cookie_encryption"] = cfg
        if not self.save_settings(settings):
            self._set_cookie_error("Failed to save encryption settings.")
            return False

        self._set_cookie_entropy(None)
        try:
            with self._cache_lock:
                self._update_raw_cookie_cache(cleaned)
                self._users_cache = dict(cleaned or {})
                self._users_cache_mtime = self._file_mtime(self.users_file)
                self._users_cache_unlock_token = self._get_cookie_unlock_token()
        except Exception:
            pass
        return True

    def change_cookie_encryption_password(self, new_password: str) -> bool:
        self._set_cookie_error("")
        if not self.cookie_encryption_enabled():
            self._set_cookie_error("Cookie encryption is not enabled.")
            return False
        entropy = self._get_cookie_entropy()
        if entropy is None:
            self._set_cookie_error("Cookies are locked. Unlock first.")
            return False
        raw = self._load_users_raw_from_disk()
        raw = self._ensure_new_format(raw)
        decrypted = self._decrypt_users_cookies(raw, entropy, blank_on_fail=False)
        if decrypted is None:
            return False

        salt = os.urandom(_COOKIE_KDF_SALT_LEN)
        cfg = self._get_cookie_encryption_settings()
        try:
            if int(cfg.get("version") or 2) < 2:
                self._set_cookie_error(
                    "Legacy cookie encryption is no longer supported. Use Cookie Encryption -> Reset (Clear Cookies)."
                )
                return False
        except Exception:
            pass
        if not _BCRYPT_AVAILABLE:
            self._set_cookie_error("Cookie encryption is not available on this system.")
            return False
        cfg.update({
            "enabled": True,
            "kdf_salt": base64.b64encode(salt).decode("ascii"),
            "kdf_iters": _COOKIE_KDF_ITERS_DEFAULT,
            "version": 2,
        })
        new_entropy = self._derive_cookie_entropy(new_password, cfg)
        if new_entropy is None:
            return False
        verifier = self._make_cookie_verifier(new_entropy, cfg)
        if verifier is None:
            return False
        cfg["verifier"] = verifier

        encrypted = self._encrypt_users_cookies(decrypted, new_entropy)
        if encrypted is None:
            return False
        meta = self._build_cookie_file_meta(cfg)
        self._users_file_cookie_meta = meta
        self._users_file_has_encrypted_cookies = self._users_data_has_encrypted_cookies(encrypted)

        if raw:
            self._create_users_backup()
        try:
            payload = self._merge_users_payload(encrypted, meta)
            self._safe_write_json(self.users_file, payload)
        except Exception as e:
            self._set_cookie_error(f"Failed to write users.json: {e}")
            return False

        settings = self.load_settings()
        current = self._get_cookie_encryption_settings(settings)
        cfg["prompted"] = True
        settings["cookie_encryption"] = cfg
        if not self.save_settings(settings):
            self._set_cookie_error("Failed to save encryption settings.")
            return False

        self._set_cookie_entropy(new_entropy)
        try:
            with self._cache_lock:
                self._update_raw_cookie_cache(encrypted)
                self._users_cache = self._apply_cookie_encryption_on_load(copy.deepcopy(encrypted))
                self._users_cache_mtime = self._file_mtime(self.users_file)
                self._users_cache_unlock_token = self._get_cookie_unlock_token()
        except Exception:
            pass
        return True

    def _file_mtime(self, file_path: Path) -> float:
        try:
            return float(file_path.stat().st_mtime)
        except Exception:
            return 0.0

    def get_users_mtime(self) -> float:
        return self._file_mtime(self.users_file)

    def get_settings_mtime(self) -> float:
        return self._file_mtime(self.settings_file)

    def _load_users_uncached(self) -> dict:
        try:
            raw = self._load_users_raw_from_disk()
            if raw:
                return self._apply_cookie_encryption_on_load(raw)
            return self._migrate_old_config()
        except Exception:
            return {}

    def peek_users(self) -> dict:
        """
        Fast path: return the cached users.json mapping (read-only by convention).
        Use load_users() if you intend to mutate the returned dict.
        """
        try:
            mtime = self._file_mtime(self.users_file)
            unlock_token = self._get_cookie_unlock_token()
            with self._cache_lock:
                if (
                    mtime == self._users_cache_mtime
                    and isinstance(self._users_cache, dict)
                    and self._users_cache_unlock_token == unlock_token
                ):
                    return self._users_cache

            data = self._load_users_uncached()
            # migrations may create users.json; re-sample mtime after load
            mtime2 = self._file_mtime(self.users_file)
            with self._cache_lock:
                self._users_cache_mtime = mtime2
                self._users_cache = data if isinstance(data, dict) else {}
                self._users_cache_unlock_token = unlock_token
            return self._users_cache
        except Exception:
            return {}

    def load_users(self):
        # Keep legacy semantics: callers may freely mutate the returned object.
        return copy.deepcopy(self.peek_users())

    def save_users(self, users_data):
        try:
            self._set_cookie_error("")
            formatted_data = self._ensure_new_format(users_data)
            cfg = self._get_cookie_encryption_settings()
            if cfg.get("enabled", False):
                entropy = self._get_cookie_entropy()
                if entropy is None:
                    raw_map = self._raw_cookie_cache or self._extract_raw_cookie_map(self._load_users_raw_from_disk())
                    for uid, info in (formatted_data or {}).items():
                        if not isinstance(info, dict):
                            continue
                        new_cookie = str(info.get("cookie") or "")
                        raw_cookie = raw_map.get(str(uid))
                        if new_cookie and (raw_cookie is None or new_cookie != raw_cookie):
                            self._set_cookie_error("Cookies are locked. Unlock to update cookies.")
                            return False
                    for uid, info in (formatted_data or {}).items():
                        if not isinstance(info, dict):
                            continue
                        raw_cookie = raw_map.get(str(uid))
                        if raw_cookie is not None:
                            info["cookie"] = raw_cookie
                else:
                    encrypted = self._encrypt_users_cookies(formatted_data, entropy)
                    if encrypted is None:
                        return False
                    formatted_data = encrypted

            self._create_users_backup()
            payload = formatted_data
            if cfg.get("enabled", False):
                meta = self._build_cookie_file_meta(cfg)
                self._users_file_cookie_meta = meta
                payload = self._merge_users_payload(formatted_data, meta)
            else:
                self._users_file_cookie_meta = {}
            self._users_file_has_encrypted_cookies = self._users_data_has_encrypted_cookies(formatted_data)
            self._safe_write_json(self.users_file, payload)
            try:
                with self._cache_lock:
                    self._update_raw_cookie_cache(formatted_data)
                    self._users_cache = self._apply_cookie_encryption_on_load(copy.deepcopy(formatted_data))
                    self._users_cache_mtime = self._file_mtime(self.users_file)
                    self._users_cache_unlock_token = self._get_cookie_unlock_token()
            except Exception:
                pass
            return True
        except Exception as e:
            return False

    def _load_settings_uncached(self) -> dict:
        try:
            if self.settings_file.exists():
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)

                loaded_has_alerts = isinstance(loaded.get("alerts"), dict)
                settings = copy.deepcopy(self.default_settings)
                if isinstance(loaded, dict):
                    misc = loaded.get("misc")
                    misc_dict = misc if isinstance(misc, dict) else {}
                    if "skip_webhook_unknown_context" not in misc_dict:
                        ocr = loaded.get("ocr")
                        if isinstance(ocr, dict) and "skip_webhook_unknown_context" in ocr:
                            loaded = dict(loaded)
                            misc_dict = dict(misc_dict)
                            misc_dict["skip_webhook_unknown_context"] = bool(
                                ocr.get("skip_webhook_unknown_context", False)
                            )
                            loaded["misc"] = misc_dict
                    settings = self._deep_update(settings, loaded)
                    try:
                        alerts = settings.get("alerts", {}) or {}
                        if not isinstance(alerts, dict):
                            alerts = {}

                        if "blackout_ping" not in alerts and "ping_message" in alerts:
                            alerts["blackout_ping"] = str(alerts.get("ping_message") or "")

                        # Migration: older configs stored these under timeout_monitor or timeouts.
                        if not loaded_has_alerts:
                            tm_cfg = settings.get("timeout_monitor", {}) or {}
                            if not isinstance(tm_cfg, dict):
                                tm_cfg = {}
                            t_cfg = settings.get("timeouts", {}) or {}
                            if not isinstance(t_cfg, dict):
                                t_cfg = {}

                            if not str(alerts.get("webhook_url") or "").strip():
                                legacy_url = str(
                                    tm_cfg.get("webhook_url") or t_cfg.get("webhook_url") or ""
                                ).strip()
                                if legacy_url:
                                    alerts["webhook_url"] = legacy_url

                            if not str(alerts.get("blackout_ping") or "").strip():
                                legacy_ping = str(
                                    tm_cfg.get("ping_message") or t_cfg.get("ping_message") or ""
                                )
                                if legacy_ping:
                                    alerts["blackout_ping"] = legacy_ping

                        try:
                            interval_h = int(alerts.get("hourly_users_report_interval_hours", 1) or 1)
                        except Exception:
                            interval_h = 1
                        alerts["hourly_users_report_interval_hours"] = max(1, min(168, interval_h))
                        alerts["hourly_users_report_enabled"] = bool(
                            alerts.get("hourly_users_report_enabled", False)
                        )

                        settings["alerts"] = alerts
                    except Exception:
                        pass
                return settings
            return copy.deepcopy(self.default_settings)
        except Exception:
            return copy.deepcopy(self.default_settings)

    def peek_settings(self) -> dict:
        """
        Fast path: return cached settings.json (read-only by convention).
        Use load_settings() if you intend to mutate the returned dict.
        """
        try:
            mtime = self._file_mtime(self.settings_file)
            with self._cache_lock:
                if mtime == self._settings_cache_mtime and isinstance(self._settings_cache, dict):
                    return self._settings_cache

            data = self._load_settings_uncached()
            mtime2 = self._file_mtime(self.settings_file)
            with self._cache_lock:
                self._settings_cache_mtime = mtime2
                self._settings_cache = data if isinstance(data, dict) else {}
            return self._settings_cache
        except Exception:
            return copy.deepcopy(self.default_settings)

    def load_settings(self):
        # Keep legacy semantics: callers may freely mutate the returned object.
        return copy.deepcopy(self.peek_settings())

    def save_settings(self, settings_data):
        try:

            self._create_backup(self.settings_file)

            self._safe_write_json(self.settings_file, settings_data)
            try:
                with self._cache_lock:
                    self._settings_cache = dict(settings_data or {})
                    self._settings_cache_mtime = self._file_mtime(self.settings_file)
            except Exception:
                pass
            return True
        except Exception as e:
            return False

    def _migrate_old_config(self):
        old_config_path = Path("config.json")
        if old_config_path.exists():
            try:
                with open(old_config_path, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)

                new_data = self._convert_to_new_format(old_data)

                if self.save_users(new_data):
                    return new_data
            except Exception as e:
                pass

        return {}

    def _convert_to_new_format(self, old_data):
        new_data = {}
        for user_id, cookie in old_data.items():
            if isinstance(cookie, str):

                new_data[user_id] = {
                    "username": f"User_{user_id}",  
                    "cookie": cookie,
                    "private_server_link": "",
                    "place": "",
                    "bad": False,
                    "cap": False,
                    "alternate_launch": False,
                    "skip_reconnect_on_log_disconnect": False,
                }
            else:

                new_data[user_id] = cookie
        return new_data

    def _ensure_new_format(self, users_data):
        if not users_data:
            return {}

        def _is_alternate_launch(user_info: dict) -> bool:
            if not isinstance(user_info, dict):
                return False
            mode = str(user_info.get("launch_mode", "") or "").strip().lower()
            if mode == "alternate":
                return True
            return bool(user_info.get("alternate_launch", user_info.get("alternate", False)))

        def _skip_reconnect_on_log_disconnect(user_info: dict) -> bool:
            if not isinstance(user_info, dict):
                return False
            return bool(
                user_info.get(
                    "skip_reconnect_on_log_disconnect",
                    user_info.get("dont_reconnect_on_log_disconnect", False),
                )
            )

        def _uid_sort_key(uid: str):
            uid_s = str(uid)
            if uid_s.isdigit():
                try:
                    return (0, int(uid_s))
                except Exception:
                    return (0, uid_s)
            return (1, uid_s)

        new_data = {}
        for user_id, user_info in users_data.items():
            if isinstance(user_info, str):

                new_data[user_id] = {
                    "username": f"User_{user_id}",
                    "cookie": user_info,
                    "private_server_link": "",
                    "place": "",
                    "server_type": "private",
                    "bad": False,
                    "cap": False,
                    "disabled": False,
                    "description": "",
                    "alternate_launch": False,
                    "skip_reconnect_on_log_disconnect": False,
                }
            elif isinstance(user_info, dict):
                private_link = user_info.get("private_server_link", "")
                place = user_info.get("place", "") or user_info.get("place_id", "")
                server_type = str(user_info.get("server_type", "") or "").strip().lower()
                if server_type not in ("private", "public"):
                    if str(private_link).strip():
                        server_type = "private"
                    elif str(place).strip():
                        server_type = "public"
                    else:
                        server_type = "private"
                new_data[user_id] = {
                    "username": user_info.get("username", f"User_{user_id}"),
                    "cookie": user_info.get("cookie", ""),
                    "private_server_link": private_link,
                    "place": place,
                    "server_type": server_type,
                    "bad":  user_info.get("bad", False),
                    "cap":  user_info.get("cap", False),
                    "disabled": user_info.get("disabled", False),
                    "description": str(user_info.get("description", "") or ""),
                    "alternate_launch": _is_alternate_launch(user_info),
                    "skip_reconnect_on_log_disconnect": _skip_reconnect_on_log_disconnect(user_info),
                }
            else:

                new_data[user_id] = {
                    "username": f"User_{user_id}",
                    "cookie": "",
                    "private_server_link": "",
                    "place": "",
                    "server_type": "private",
                    "bad":  False,
                    "cap": False,
                    "disabled": False,
                    "description": "",
                    "alternate_launch": False,
                    "skip_reconnect_on_log_disconnect": False,
                }

        # Enforce: at most one account may have alternate launch enabled.
        try:
            alternates = [
                str(uid) for uid, info in new_data.items()
                if isinstance(info, dict) and bool(info.get("alternate_launch", False))
            ]
            if len(alternates) > 1:
                keep_uid = sorted(alternates, key=_uid_sort_key)[0]
                for uid in alternates:
                    if uid == keep_uid:
                        continue
                    try:
                        new_data[uid]["alternate_launch"] = False
                    except Exception:
                        pass
        except Exception:
            pass

        return new_data

    def auto_bad_marking_enabled(self) -> bool:
        try:
            settings = self.peek_settings()
            misc = settings.get("misc", {}) or {}
            if isinstance(misc, dict):
                return not bool(misc.get("disable_manager_bad_marking", False))
        except Exception:
            pass
        return True

    def mark_bad_cookie(self, user_id: str, state: bool) -> None:
        if state and not self.auto_bad_marking_enabled():
            return
        users = self.load_users()
        if user_id in users and users[user_id].get("bad", False) != state:
            username = ""
            try:
                info = users.get(user_id)
                if isinstance(info, dict):
                    username = str(info.get("username") or "")
            except Exception:
                username = ""
            users[user_id]["bad"] = state
            if self.save_users(users) and state:
                self._send_bad_alert_webhook(user_id, username)

    def _resolve_alert_webhook_url(self, settings: dict) -> str:
        webhook_url = ""
        try:
            alerts = settings.get("alerts", {}) or {}
            if isinstance(alerts, dict):
                webhook_url = str(alerts.get("webhook_url") or "").strip()
            if not webhook_url:
                tm = settings.get("timeout_monitor", {}) or {}
                if isinstance(tm, dict):
                    webhook_url = str(tm.get("webhook_url") or "").strip()
        except Exception:
            webhook_url = ""
        return webhook_url

    def _dispatch_alert_message(self, webhook_url: str, message: str) -> bool:
        webhook_url = str(webhook_url or "").strip()
        msg = str(message or "").strip()
        if not webhook_url or not msg:
            return False

        def _post():
            try:
                requests.post(webhook_url, json={"content": msg}, timeout=8)
            except Exception:
                pass

        try:
            threading.Thread(target=_post, daemon=True).start()
        except Exception:
            _post()
        return True

    def _build_user_alert_message(self, template: str, user_id: str, username: str = "") -> str:
        uid = str(user_id)
        uname = str(username or "").strip() or uid
        return (
            str(template or "")
            .replace("{username}", uname)
            .replace("{uid}", uid)
            .replace("{user_id}", uid)
            .strip()
        )

    def _send_cap_alert_webhook(self, user_id: str, username: str = "") -> None:
        """
        Fire-and-forget Discord webhook alert when a user is marked CAP.
        Uses settings.json -> alerts.{webhook_url, cap_message}.
        """
        try:
            settings = self.peek_settings() or {}
        except Exception:
            settings = {}

        template = ""
        try:
            alerts = settings.get("alerts", {}) or {}
            if isinstance(alerts, dict):
                template = str(alerts.get("cap_message") or "").strip()
        except Exception:
            return

        if not template:
            return

        webhook_url = self._resolve_alert_webhook_url(settings)
        msg = self._build_user_alert_message(template, str(user_id), username)
        self._dispatch_alert_message(webhook_url, msg)

    def _send_bad_alert_webhook(self, user_id: str, username: str = "") -> None:
        """
        Fire-and-forget Discord webhook alert when a user is marked BAD.
        Uses settings.json -> alerts.{webhook_url, bad_message}.
        """
        try:
            settings = self.peek_settings() or {}
        except Exception:
            settings = {}

        template = ""
        try:
            alerts = settings.get("alerts", {}) or {}
            if isinstance(alerts, dict):
                template = str(alerts.get("bad_message") or "").strip()
        except Exception:
            return

        if not template:
            return

        webhook_url = self._resolve_alert_webhook_url(settings)
        msg = self._build_user_alert_message(template, str(user_id), username)
        self._dispatch_alert_message(webhook_url, msg)

    def send_hourly_users_report_webhook(self, total_users: int, active_users: int) -> bool:
        try:
            settings = self.peek_settings() or {}
        except Exception:
            settings = {}
        webhook_url = self._resolve_alert_webhook_url(settings)
        msg = f"Hourly user report: Total Users = {int(total_users)}, Active Users = {int(active_users)}."
        return self._dispatch_alert_message(webhook_url, msg)

    def mark_cap_flag(self, user_id: str, state: bool) -> None:
        users = self.load_users()
        if user_id in users and users[user_id].get("cap", False) != state:
            username = ""
            try:
                info = users.get(user_id)
                if isinstance(info, dict):
                    username = str(info.get("username") or "")
            except Exception:
                username = ""
            users[user_id]["cap"] = state
            if self.save_users(users) and state:
                self._send_cap_alert_webhook(user_id, username)

    def clear_all_bad_flags(self):
        users = self.load_users()
        for info in users.values():
            info["bad"] = False
            info["cap"] = False
        self.save_users(users)

    def get_users_for_manager(self):
        users = self.peek_users()
        manager_format = {}
        for user_id, user_info in users.items():
            if isinstance(user_info, dict):

                manager_format[user_id] = dict(user_info)
            else:

                manager_format[user_id] = {
                    "username": f"User_{user_id}",
                    "cookie": user_info,
                    "private_server_link": "",
                    "place": "",
                    "bad": False,
                    "cap": False,
                    "disabled": False,
                    "alternate_launch": False,
                    "skip_reconnect_on_log_disconnect": False,
                }
        return manager_format

    def get_config_info(self):
        return {
            "config_dir": str(self.config_dir),
            "users_file": str(self.users_file),
            "settings_file": str(self.settings_file),
            "backup_dir": str(self.backup_dir)
        }
        
    def mark_user_bad_cookie(self, user_id):
        users = self.load_users()
        if user_id in users:
            users[user_id]["bad_cookie"] = True
            self.save_users(users)

    def clear_all_bad_cookies(self):
        users = self.load_users()
        for user in users.values():
            user["bad_cookie"] = False
        self.save_users(users)


class ModernStyle:
    BACKGROUND = "#1e1e1e"
    SURFACE = "#2d2d2d"
    SURFACE_VARIANT = "#3d3d3d"
    PRIMARY = "#6366f1"
    PRIMARY_VARIANT = "#4f46e5"
    SECONDARY = "#10b981"
    ERROR = "#ef4444"
    WARNING = "#f59e0b"
    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "#a1a1aa"
    BORDER = "#404040"

    @staticmethod
    def get_stylesheet():
        return f"""
        QMainWindow {{
            background-color: {ModernStyle.BACKGROUND};
            color: {ModernStyle.TEXT_PRIMARY};
        }}

        QWidget {{
            background-color: {ModernStyle.BACKGROUND};
            color: {ModernStyle.TEXT_PRIMARY};
            font-family: 'Segoe UI', Arial, sans-serif;
        }}

        QTabWidget::pane {{
            border: 1px solid {ModernStyle.BORDER};
            background-color: {ModernStyle.SURFACE};
            border-radius: 8px;
        }}

        QTabBar::tab {{
            background-color: {ModernStyle.SURFACE_VARIANT};
            color: {ModernStyle.TEXT_SECONDARY};
            padding: 12px 20px;
            margin-right: 2px;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
        }}

        QTabBar::tab:selected {{
            background-color: {ModernStyle.PRIMARY};
            color: {ModernStyle.TEXT_PRIMARY};
        }}

        QTableWidget {{
            background-color: {ModernStyle.SURFACE};
            border: 1px solid {ModernStyle.BORDER};
            border-radius: 8px;
            gridline-color: {ModernStyle.BORDER};
            selection-background-color: {ModernStyle.PRIMARY_VARIANT};
        }}

        QTableWidget::item {{
            padding: 8px;
            border-bottom: 1px solid {ModernStyle.BORDER};
        }}

        QHeaderView::section {{
            background-color: {ModernStyle.SURFACE_VARIANT};
            color: {ModernStyle.TEXT_PRIMARY};
            padding: 10px;
            border: none;
            font-weight: bold;
        }}

        QPushButton {{
            background-color: {ModernStyle.PRIMARY};
            color: {ModernStyle.TEXT_PRIMARY};
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: 500;
            min-width: 80px;
            min-height: 28px;
            font-size: 13px;
        }}

        QPushButton:hover {{
            background-color: {ModernStyle.PRIMARY_VARIANT};
        }}

        QPushButton:pressed {{
            background-color: 
        }}

        QPushButton:disabled {{
            background-color: {ModernStyle.SURFACE_VARIANT};
            color: {ModernStyle.TEXT_SECONDARY};
        }}

        QPushButton.success {{
            background-color: {ModernStyle.SECONDARY};
        }}

        QPushButton.success:hover {{
            background-color: 
        }}

        QPushButton.danger {{
            background-color: {ModernStyle.ERROR};
        }}

        QPushButton.danger:hover {{
            background-color: 
        }}

        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
            background-color: {ModernStyle.SURFACE};
            border: 2px solid {ModernStyle.BORDER};
            border-radius: 6px;
            padding: 8px 12px;
            color: {ModernStyle.TEXT_PRIMARY};
        }}

        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
            border-color: {ModernStyle.PRIMARY};
        }}

        QTextEdit {{
            background-color: {ModernStyle.SURFACE};
            border: 2px solid {ModernStyle.BORDER};
            border-radius: 6px;
            padding: 8px;
            color: {ModernStyle.TEXT_PRIMARY};
        }}

        QGroupBox {{
            font-weight: bold;
            border: 2px solid {ModernStyle.BORDER};
            border-radius: 8px;
            margin-top: 10px;
            padding-top: 10px;
        }}

        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 8px 0 8px;
            color: {ModernStyle.TEXT_PRIMARY};
        }}

        QLabel {{
            color: {ModernStyle.TEXT_PRIMARY};
        }}

        QProgressBar {{
            border: 2px solid {ModernStyle.BORDER};
            border-radius: 6px;
            text-align: center;
            background-color: {ModernStyle.SURFACE};
        }}

        QProgressBar::chunk {{
            background-color: {ModernStyle.PRIMARY};
            border-radius: 4px;
        }}

        QCheckBox {{
            color: {ModernStyle.TEXT_PRIMARY};
            spacing: 8px;
        }}

        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
            border: 2px solid {ModernStyle.BORDER};
            border-radius: 4px;
            background-color: {ModernStyle.SURFACE};
        }}

        QCheckBox::indicator:checked {{
            background-color: {ModernStyle.PRIMARY};
            border-color: {ModernStyle.PRIMARY};
        }}
        
        QCheckBox::indicator:disabled {{
            background-color: {ModernStyle.SURFACE};
            border-color: {ModernStyle.SURFACE};
        }}

        QScrollBar:vertical {{
            background-color: {ModernStyle.SURFACE};
            width: 12px;
            border-radius: 6px;
        }}

        QScrollBar::handle:vertical {{
            background-color: {ModernStyle.SURFACE_VARIANT};
            border-radius: 6px;
            min-height: 20px;
        }}

        QScrollBar::handle:vertical:hover {{
            background-color: {ModernStyle.BORDER};
        }}
        """


class _AutoItemSpinBox(QSpinBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        try:
            opt = QStyleOptionSpinBox()
            self.initStyleOption(opt)

            up_rect = self.style().subControlRect(
                QStyle.ComplexControl.CC_SpinBox,
                opt,
                QStyle.SubControl.SC_SpinBoxUp,
                self,
            )
            down_rect = self.style().subControlRect(
                QStyle.ComplexControl.CC_SpinBox,
                opt,
                QStyle.SubControl.SC_SpinBoxDown,
                self,
            )

            enabled = self.isEnabled()
            try:
                step_enabled = self.stepEnabled()
            except Exception:
                step_enabled = QAbstractSpinBox.StepEnabledFlag.StepUpEnabled | QAbstractSpinBox.StepEnabledFlag.StepDownEnabled

            up_enabled = enabled and bool(step_enabled & QAbstractSpinBox.StepEnabledFlag.StepUpEnabled)
            down_enabled = enabled and bool(step_enabled & QAbstractSpinBox.StepEnabledFlag.StepDownEnabled)

            def _draw_triangle(rect: QRect, direction: str):
                r = rect.adjusted(7, 5, -7, -5)
                if r.width() <= 1 or r.height() <= 1:
                    r = rect.adjusted(4, 4, -4, -4)
                if r.width() <= 1 or r.height() <= 1:
                    return

                cx = float(r.center().x())
                top = float(r.top())
                bottom = float(r.bottom())
                left = float(r.left())
                right = float(r.right())

                path = QPainterPath()
                if direction == "up":
                    path.moveTo(cx, top)
                    path.lineTo(right, bottom)
                    path.lineTo(left, bottom)
                else:
                    path.moveTo(left, top)
                    path.lineTo(right, top)
                    path.lineTo(cx, bottom)
                path.closeSubpath()
                painter.drawPath(path)

            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)

            painter.setBrush(QColor(ModernStyle.TEXT_PRIMARY if up_enabled else ModernStyle.TEXT_SECONDARY))
            _draw_triangle(up_rect, "up")

            painter.setBrush(QColor(ModernStyle.TEXT_PRIMARY if down_enabled else ModernStyle.TEXT_SECONDARY))
            _draw_triangle(down_rect, "down")

            painter.end()
        except Exception:
            return None

class WorkerThread(QThread):
    log_signal     = Signal(str)
    status_signal  = Signal(object)
    process_signal = Signal(object)
    multiscope_signal = Signal(object)   # drives the Multiscope tab

    def __init__(self, cfg_manager, resume_state: Optional[dict] = None):
        super().__init__()
        self.cfg_manager      = cfg_manager
        self._resume_state    = resume_state
        self.running          = False
        self.manager          = None
        self.process_mgr      = None
        self.launcher         = None
        self._ramp_thread     = None


        self.user_states      = {}
        self.log_pointers     = {}
        self.timing_trackers  = {}
        
        # cooldown map: uid -> expiry epoch
        self._recent_handoffs = {}
        # seconds to protect the spare after we swap pools
        self.handoff_grace = 20

        self.restart_threshold = 0
        self.strap_threshold   = 50
        self.initial_delay     = 0
        self.launch_wait_for_log_mode = False
        self.msedgewebview2_limiter_enabled = True
        self._launch_tracking_lock = threading.Lock()
        self._latest_launch_pending_uid: Optional[str] = None
        self._launch_gate_uid: Optional[str] = None
        self._launch_gate_started_at: float = 0.0
        self._launch_gate_min_delay_s: float = 0.0
        self._launch_gate_log_confirmed_uid: Optional[str] = None
        self._last_gate_log_refresh_at: float = 0.0
        self._launch_gate_debug_last_key: str = ""
        self._launch_gate_debug_last_ts: float = 0.0
        self._msedge_timed_loop_active: bool = False
        self._msedge_timed_loop_next_at: float = 0.0
        self._msedge_timed_loop_interval_s: float = 2.0

        # Spare / handoff controls
        self.spares_mode       = False
        self.handoff_lead      = 25
        self.early_join_window = 90
        
        self._spares_num = 1    # numerator for fraction like 1/2, 3/4, 4/5
        self._spares_den = 2

        self.active_pool = set()      # type: Set[str]
        self.spare_pool  = set()      # type: Set[str]
        self.handoff_for = {}         # type: Dict[str, str]     # donor_uid -> spare_uid
        self.last_launch = {}         # type: Dict[str, float]   # uid -> epoch of last launch
        self._skip_until_by_user = {}   # uid -> epoch (per-user backoff after a conflict)
        self._restart_cursor = 0        # round-robin pointer into the restartables ring


        self._last_proc_count = 0
        self._last_growth_ts  = time.time()
        self.log_inactivity_timeout = 120

        # Multiscope
        self.ms = None  # ← NEW: MultiScopeEngine instance
        
        self._last_good_set = set()  # tracks which users are currently 'good' (flagged/disabled == False)
        self._reservations_ttl = 60  # seconds a server is "held" by a handoff pre-join
        self.preconnect_grace = 360  # seconds to wait for username to show in logs on first connect
        self._waiting_usernames_since = {}  # uid -> epoch; cleaned up automatically
        self._boot_phase = True  # use initial_delay for any launches during ramp

        self._bootstrap_multiscope_rows: Optional[list] = None
        self._bootstrap_multiscope_deadline: float = 0.0
        try:
            if isinstance(resume_state, dict):
                rows = resume_state.get("multiscope_rows")
                if isinstance(rows, list) and rows:
                    self._bootstrap_multiscope_rows = rows
                    self._bootstrap_multiscope_deadline = time.time() + 12.0
        except Exception:
            self._bootstrap_multiscope_rows = None
            self._bootstrap_multiscope_deadline = 0.0

    def _current_launch_gate_delay(self) -> float:
        try:
            if bool(getattr(self, "_boot_phase", False)):
                delay = float(getattr(self, "initial_delay", 0) or 0)
            else:
                timeouts = (getattr(self.manager, "timeouts", {}) or {})
                delay = float(timeouts.get("launch_delay", 0) or 0)
        except Exception:
            delay = 0.0

        if delay <= 0:
            try:
                delay = float(getattr(getattr(self, "launcher", None), "launch_delay", 0) or 0)
            except Exception:
                delay = 0.0
        return max(0.0, delay)

    def _clear_launch_gate(self, uid: Optional[str] = None) -> None:
        try:
            with self._launch_tracking_lock:
                if uid is not None and self._launch_gate_uid != str(uid):
                    return
                self._launch_gate_uid = None
                self._launch_gate_started_at = 0.0
                self._launch_gate_min_delay_s = 0.0
                self._launch_gate_log_confirmed_uid = None
                self._last_gate_log_refresh_at = 0.0
        except Exception:
            pass

    def _register_successful_launch(self, uid: str, launched_at: Optional[float] = None) -> None:
        uid_s = str(uid)
        try:
            ts = time.time() if launched_at is None else float(launched_at)
        except Exception:
            ts = time.time()
        min_delay = self._current_launch_gate_delay()
        try:
            with self._launch_tracking_lock:
                self._latest_launch_pending_uid = uid_s
                if self.launch_wait_for_log_mode:
                    self._launch_gate_uid = uid_s
                    self._launch_gate_started_at = ts
                    self._launch_gate_min_delay_s = max(0.0, float(min_delay))
                    self._launch_gate_log_confirmed_uid = None
                else:
                    self._launch_gate_uid = None
                    self._launch_gate_started_at = 0.0
                    self._launch_gate_min_delay_s = 0.0
                    self._launch_gate_log_confirmed_uid = None
                self._last_gate_log_refresh_at = 0.0
        except Exception:
            pass
        if self.launch_wait_for_log_mode:
            self._log_launch_gate_debug(
                f"set:{uid_s}",
                f"set gate uid={uid_s} min_delay={max(0.0, float(min_delay)):.1f}s",
                min_interval_s=0.0,
            )

    def _mark_user_log_confirmed(self, uid: str) -> None:
        uid_s = str(uid)
        marked = False
        try:
            with self._launch_tracking_lock:
                if self._launch_gate_uid == uid_s:
                    if self._launch_gate_log_confirmed_uid != uid_s:
                        self._launch_gate_log_confirmed_uid = uid_s
                        marked = True
        except Exception:
            pass
        if marked:
            self._log_launch_gate_debug(
                f"confirm:{uid_s}",
                f"uid={uid_s} strict log confirmed",
                min_interval_s=0.0,
            )

    def _log_launch_gate_debug(self, key: str, message: str, *, min_interval_s: float = 1.0) -> None:
        key_s = str(key or "")
        try:
            now = float(time.time())
        except Exception:
            now = 0.0

        try:
            last_key = str(getattr(self, "_launch_gate_debug_last_key", "") or "")
            last_ts = float(getattr(self, "_launch_gate_debug_last_ts", 0.0) or 0.0)
        except Exception:
            last_key = ""
            last_ts = 0.0

        try:
            gate_s = max(0.0, float(min_interval_s))
        except Exception:
            gate_s = 0.0

        if key_s and last_key == key_s and gate_s > 0 and (now - last_ts) < gate_s:
            return

        try:
            self._launch_gate_debug_last_key = key_s
            self._launch_gate_debug_last_ts = now
        except Exception:
            pass

        try:
            self._log(f"[LaunchGate] {message}")
        except Exception:
            pass

    def _user_has_strict_log_match(self, uid: str) -> bool:
        try:
            st = self.user_states.get(str(uid), {}) or {}
            info = st.get("user_info", {}) if isinstance(st, dict) else {}
            if not isinstance(info, dict):
                info = {}
            uname = str(info.get("username", "") or "").strip().lower()
            if not uname:
                return False
            return bool(find_log_for_username(uname, allow_fallback=False))
        except Exception:
            return False

    def _is_user_live(self, uid: str) -> bool:
        try:
            pids = list(self.manager.process_tracker.user_processes.get(str(uid), []) or [])
        except Exception:
            pids = []
        for pid in pids:
            try:
                if self.process_mgr.verify_process_active(pid):
                    return True
            except Exception:
                continue
        return False

    def _is_launch_gate_ready(
        self,
        *,
        now: Optional[float] = None,
        strict_log_matches: Optional[dict] = None,
        live_by_uid: Optional[dict] = None,
    ) -> bool:
        if not self.launch_wait_for_log_mode:
            return True

        try:
            with self._launch_tracking_lock:
                gate_uid = self._launch_gate_uid
                gate_started = float(self._launch_gate_started_at or 0.0)
                gate_min = float(self._launch_gate_min_delay_s or 0.0)
                gate_confirmed = (self._launch_gate_log_confirmed_uid == self._launch_gate_uid)
        except Exception:
            gate_uid = None
            gate_started = 0.0
            gate_min = 0.0
            gate_confirmed = False

        if not gate_uid:
            return True

        ts = time.time() if now is None else float(now)
        elapsed = max(0.0, ts - gate_started)

        st = self.user_states.get(gate_uid, {}) if isinstance(self.user_states, dict) else {}
        info = st.get("user_info", {}) if isinstance(st, dict) else {}
        if not isinstance(info, dict):
            info = {}
        if (not st) or bool(info.get("bad", False) or info.get("cap", False) or info.get("disabled", False)):
            self._log_launch_gate_debug(
                f"open:invalid:{gate_uid}",
                f"open uid={gate_uid} reason=state_invalid_or_flagged",
                min_interval_s=0.0,
            )
            self._clear_launch_gate(gate_uid)
            return True

        live = None
        if isinstance(live_by_uid, dict):
            try:
                live = bool(live_by_uid.get(gate_uid, False))
            except Exception:
                live = None
        if live is None:
            live = self._is_user_live(gate_uid)
        if not live:
            # Don't deadlock launches if the "latest launched" process died before log confirmation.
            self._log_launch_gate_debug(
                f"open:not_live:{gate_uid}",
                f"open uid={gate_uid} reason=not_live",
                min_interval_s=0.0,
            )
            self._clear_launch_gate(gate_uid)
            return True

        if elapsed < max(0.0, gate_min):
            remain = max(0.0, gate_min - elapsed)
            self._log_launch_gate_debug(
                f"wait:min_delay:{gate_uid}:{int(remain)}",
                f"wait uid={gate_uid} reason=min_delay elapsed={elapsed:.1f}s remain={remain:.1f}s",
                min_interval_s=1.0,
            )
            return False

        if gate_confirmed:
            self._log_launch_gate_debug(
                f"open:confirmed:{gate_uid}",
                f"open uid={gate_uid} reason=confirmed",
                min_interval_s=0.0,
            )
            self._clear_launch_gate(gate_uid)
            return True

        # Hard failsafe: never let the gate block beyond preconnect_grace.
        try:
            grace_s = float(getattr(self, "preconnect_grace", 0) or 0)
        except Exception:
            grace_s = 0.0
        if grace_s > 0 and elapsed >= grace_s:
            self._log_launch_gate_debug(
                f"open:failsafe:{gate_uid}",
                f"open uid={gate_uid} reason=preconnect_grace elapsed={elapsed:.1f}s grace={grace_s:.1f}s",
                min_interval_s=0.0,
            )
            self._clear_launch_gate(gate_uid)
            return True

        seen = None
        if isinstance(strict_log_matches, dict):
            seen = strict_log_matches.get(gate_uid, None)
            if seen is not None:
                seen = bool(seen)
        if not bool(seen):
            # Refresh the username->log map opportunistically while waiting so
            # the gate can react quickly to newly written logs.
            try:
                last_refresh = float(getattr(self, "_last_gate_log_refresh_at", 0.0) or 0.0)
            except Exception:
                last_refresh = 0.0
            if (ts - last_refresh) >= 1.0:
                try:
                    refresh_username_log_map()
                except Exception:
                    pass
                try:
                    self._last_gate_log_refresh_at = ts
                except Exception:
                    pass
            seen = bool(seen) or self._user_has_strict_log_match(gate_uid)

        if bool(seen):
            self._log_launch_gate_debug(
                f"open:strict_seen:{gate_uid}",
                f"open uid={gate_uid} reason=strict_log_match elapsed={elapsed:.1f}s",
                min_interval_s=0.0,
            )
            self._clear_launch_gate(gate_uid)
            return True

        grace_text = f"{grace_s:.1f}s" if grace_s > 0 else "off"
        self._log_launch_gate_debug(
            f"wait:strict_missing:{gate_uid}:{int(elapsed)}",
            f"wait uid={gate_uid} reason=strict_log_missing elapsed={elapsed:.1f}s grace={grace_text}",
            min_interval_s=1.0,
        )
        return False

    def _wait_for_launch_gate_ready(self) -> bool:
        if not self.launch_wait_for_log_mode:
            return True
        if self._is_launch_gate_ready():
            return True
        wait_started = time.time()
        self._log_launch_gate_debug(
            "wait_loop:start",
            "blocking launch until latest launched user is confirmed in logs",
            min_interval_s=0.0,
        )
        while self.running:
            if self._is_launch_gate_ready():
                waited = max(0.0, time.time() - wait_started)
                self._log_launch_gate_debug(
                    f"wait_loop:release:{int(waited)}",
                    f"release after {waited:.1f}s",
                    min_interval_s=0.0,
                )
                return True
            time.sleep(0.1)
        self._log_launch_gate_debug(
            "wait_loop:stop",
            "aborted while waiting because worker stopped",
            min_interval_s=0.0,
        )
        return False

    def _maybe_trigger_msedge_kill_for_user(self, uid: str, *, strict_log_seen: bool) -> None:
        if not strict_log_seen:
            return
        if not bool(getattr(self, "msedgewebview2_limiter_enabled", True)):
            return
        should_kill = False
        uid_s = str(uid)
        try:
            with self._launch_tracking_lock:
                if self._latest_launch_pending_uid == uid_s:
                    self._latest_launch_pending_uid = None
                    should_kill = True
        except Exception:
            should_kill = False

        if not should_kill:
            return
        try:
            limit_msedgewebview2_processes(threshold=1, kill_all=True)
        except Exception:
            pass

    def _all_live_users_in_menu_false(
        self,
        *,
        live_by_uid: Optional[dict],
        multiscope_rows: Optional[list],
        strict_log_matches: Optional[dict] = None,
    ) -> bool:
        if not isinstance(live_by_uid, dict):
            return False
        live_uids = {str(uid) for uid, is_live in live_by_uid.items() if bool(is_live)}
        if not live_uids:
            return False
        if not isinstance(multiscope_rows, list) or not multiscope_rows:
            return False

        in_menu_by_uid: Dict[str, Optional[bool]] = {}
        for row in multiscope_rows:
            if not isinstance(row, dict):
                continue
            users = row.get("users", [])
            if not isinstance(users, (list, tuple, set)):
                continue
            val = row.get("in_menu", None)
            in_menu_val = None if val is None else bool(val)
            for uid in users:
                uid_s = str(uid or "").strip()
                if uid_s:
                    in_menu_by_uid[uid_s] = in_menu_val

        strict_by_uid = None
        if isinstance(strict_log_matches, dict):
            strict_by_uid = {}
            for uid, seen in strict_log_matches.items():
                uid_s = str(uid or "").strip()
                if uid_s:
                    strict_by_uid[uid_s] = bool(seen)

        for uid_s in live_uids:
            # A reconnecting session is still "unknown" until its username is
            # seen in the current Roblox logs again.
            if strict_by_uid is not None and strict_by_uid.get(uid_s, False) is not True:
                return False
            if in_menu_by_uid.get(uid_s, None) is not False:
                return False
        return True

    def _drive_msedge_kill_timed_loop(
        self,
        *,
        live_by_uid: Optional[dict],
        multiscope_rows: Optional[list],
        strict_log_matches: Optional[dict] = None,
        now: Optional[float] = None,
    ) -> None:
        if not bool(getattr(self, "msedgewebview2_limiter_enabled", True)):
            self._msedge_timed_loop_active = False
            self._msedge_timed_loop_next_at = 0.0
            return

        try:
            ts = time.time() if now is None else float(now)
        except Exception:
            ts = time.time()

        launched_all_configured = False
        eligible_uids: List[str] = []
        if isinstance(self.user_states, dict) and self.user_states:
            for uid, st in self.user_states.items():
                _st = st if isinstance(st, dict) else {}
                info = _st.get("user_info", {}) if isinstance(_st, dict) else {}
                if not isinstance(info, dict):
                    info = {}
                if bool(info.get("disabled", False) or info.get("bad", False) or info.get("cap", False)):
                    continue
                eligible_uids.append(str(uid))

        if eligible_uids:
            launched_all_configured = True
            live_map = live_by_uid if isinstance(live_by_uid, dict) else {}
            for uid_s in eligible_uids:
                if not bool(live_map.get(uid_s, False)):
                    launched_all_configured = False
                    break

        all_in_menu_false = self._all_live_users_in_menu_false(
            live_by_uid=live_by_uid,
            multiscope_rows=multiscope_rows,
            strict_log_matches=strict_log_matches,
        )

        should_run = bool(launched_all_configured and all_in_menu_false)
        if not should_run:
            self._msedge_timed_loop_active = False
            self._msedge_timed_loop_next_at = 0.0
            return

        if not bool(self._msedge_timed_loop_active):
            self._msedge_timed_loop_active = True
            self._msedge_timed_loop_next_at = 0.0

        try:
            next_at = float(getattr(self, "_msedge_timed_loop_next_at", 0.0) or 0.0)
        except Exception:
            next_at = 0.0
        if ts < next_at:
            return

        try:
            limit_msedgewebview2_processes(threshold=1, kill_all=True)
        except Exception:
            pass

        try:
            interval = max(0.25, float(getattr(self, "_msedge_timed_loop_interval_s", 2.0) or 2.0))
        except Exception:
            interval = 2.0
        self._msedge_timed_loop_next_at = ts + interval

    def _is_log_disconnect_payload(self, payload: str) -> bool:
        p = str(payload or "").strip().lower()
        if not p:
            return False
        if p.startswith("reason="):
            return True
        return p in ("connection lost", "detected in log")

    def _is_in_menu_none_disconnect_payload(self, payload: str) -> bool:
        p = str(payload or "").strip().lower()
        if not p:
            return False
        return p.startswith("in_menu_none_timeout")

    def export_state(self) -> dict:
        """
        Snapshot enough in-memory state to allow a later Pause -> Resume without
        losing PID<->user mappings, pool/handoff state, and log scan offsets.
        """
        state: dict = {"version": 1, "ts": time.time()}

        try:
            state["user_states"] = copy.deepcopy(getattr(self, "user_states", {}) or {})
        except Exception:
            state["user_states"] = {}

        try:
            state["log_pointers"] = dict(getattr(self, "log_pointers", {}) or {})
        except Exception:
            state["log_pointers"] = {}

        try:
            state["timing_trackers"] = dict(getattr(self, "timing_trackers", {}) or {})
        except Exception:
            state["timing_trackers"] = {}

        try:
            state["active_pool"] = list(getattr(self, "active_pool", set()) or [])
        except Exception:
            state["active_pool"] = []

        try:
            state["spare_pool"] = list(getattr(self, "spare_pool", set()) or [])
        except Exception:
            state["spare_pool"] = []

        try:
            state["handoff_for"] = dict(getattr(self, "handoff_for", {}) or {})
        except Exception:
            state["handoff_for"] = {}

        try:
            state["recent_handoffs"] = dict(getattr(self, "_recent_handoffs", {}) or {})
        except Exception:
            state["recent_handoffs"] = {}

        try:
            state["skip_until_by_user"] = dict(getattr(self, "_skip_until_by_user", {}) or {})
        except Exception:
            state["skip_until_by_user"] = {}

        try:
            state["restart_cursor"] = int(getattr(self, "_restart_cursor", 0) or 0)
        except Exception:
            state["restart_cursor"] = 0

        try:
            state["last_launch"] = dict(getattr(self, "last_launch", {}) or {})
        except Exception:
            state["last_launch"] = {}

        try:
            state["spares_mode"] = bool(getattr(self, "spares_mode", False))
        except Exception:
            state["spares_mode"] = False

        try:
            state["spares_fraction"] = f"{int(getattr(self, '_spares_num', 1))}/{int(getattr(self, '_spares_den', 2))}"
        except Exception:
            state["spares_fraction"] = "1/2"

        try:
            tracker = getattr(getattr(self, "manager", None), "process_tracker", None)
            if tracker is not None:
                state["process_tracker"] = {
                    "user_processes": {
                        str(uid): list(pids)
                        for uid, pids in dict(getattr(tracker, "user_processes", {}) or {}).items()
                    },
                    "process_owners": dict(getattr(tracker, "process_owners", {}) or {}),
                    "creation_timestamps": dict(getattr(tracker, "creation_timestamps", {}) or {}),
                    "user_server": dict(getattr(tracker, "user_server", {}) or {}),
                    "pid_grace_until": dict(getattr(tracker, "pid_grace_until", {}) or {}),
                    "protection_period": int(getattr(tracker, "protection_period", 60) or 60),
                    "server_owner": dict(getattr(tracker, "server_owner", {}) or {}),
                    "user_ps_code": dict(getattr(tracker, "user_ps_code", {}) or {}),
                    "user_ps_place": dict(getattr(tracker, "user_ps_place", {}) or {}),
                    "reserved_servers": dict(getattr(tracker, "reserved_servers", {}) or {}),
                    "skip_until_by_user": dict(getattr(tracker, "skip_until_by_user", {}) or {}),
                    "share_to_link": dict(getattr(tracker, "share_to_link", {}) or {}),
                }
            else:
                state["process_tracker"] = {}
        except Exception:
            state["process_tracker"] = {}

        try:
            ms = getattr(self, "ms", None)
            if ms:
                try:
                    state["multiscope_rows"] = ms.snapshot()
                except Exception:
                    state["multiscope_rows"] = []
                try:
                    state["multiscope_state"] = ms.export_state()
                except Exception:
                    state["multiscope_state"] = {}
            else:
                state["multiscope_rows"] = []
                state["multiscope_state"] = {}
        except Exception:
            state["multiscope_rows"] = []
            state["multiscope_state"] = {}

        # If the RPC export failed (e.g. multiscope process was busy), synthesize a minimal
        # import_state payload from the last snapshot so UI values don't reset on resume.
        try:
            ms_state = state.get("multiscope_state") or {}
            rows = state.get("multiscope_rows") or []
            if (not ms_state) and isinstance(rows, list) and rows:
                now = time.time()
                scopes: dict = {}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    key = str(row.get("server_key") or row.get("server") or "").strip()
                    if not key:
                        continue
                    users_raw = row.get("users") or []
                    if not isinstance(users_raw, (list, tuple, set)):
                        users_raw = []
                    biome = str(row.get("last_biome", row.get("biome", "")) or "").strip()
                    merchant = str(row.get("last_merchant", row.get("merchant", "")) or "").strip()
                    in_menu = row.get("in_menu", None)

                    # Try to preserve ages if present (snapshot provides seconds-since).
                    try:
                        b_age = row.get("biome_age", None)
                        b_age = None if b_age is None else int(b_age)
                    except Exception:
                        b_age = None
                    try:
                        m_age = row.get("merchant_age", None)
                        m_age = None if m_age is None else int(m_age)
                    except Exception:
                        m_age = None

                    try:
                        events = int(row.get("events", 0) or 0)
                    except Exception:
                        events = 0

                    scopes[key] = {
                        "key": key,
                        "users": [str(u) for u in users_raw if str(u).strip()],
                        "last_biome": biome,
                        "last_biome_ts": (now - b_age) if b_age is not None else 0.0,
                        "last_merchant": merchant,
                        "last_merchant_ts": (now - m_age) if m_age is not None else 0.0,
                        "in_menu": in_menu,
                        "last_menu_ts": now if in_menu is not None else 0.0,
                        "events": events,
                        "next_tail_at": 0.0,
                        "poll_rot": 0,
                    }

                if scopes:
                    state["multiscope_state"] = {"version": 1, "ts": now, "scopes": scopes, "cursors": {}}
        except Exception:
            pass

        return state

    def _restore_process_tracker_snapshot(self, snapshot: dict) -> None:
        if not self.manager or not hasattr(self.manager, "process_tracker"):
            return
        if not isinstance(snapshot, dict) or not snapshot:
            return

        try:
            from collections import defaultdict

            t = self.manager.process_tracker

            user_processes = defaultdict(list)
            for uid, pids in (snapshot.get("user_processes") or {}).items():
                if uid is None:
                    continue
                out: list[int] = []
                for pid in (pids or []):
                    try:
                        out.append(int(pid))
                    except Exception:
                        continue
                user_processes[str(uid)] = out
            t.user_processes = user_processes

            proc_owners: dict[int, str] = {}
            for pid, uid in (snapshot.get("process_owners") or {}).items():
                try:
                    proc_owners[int(pid)] = str(uid)
                except Exception:
                    continue
            t.process_owners = proc_owners

            cts: dict[int, float] = {}
            for pid, ts in (snapshot.get("creation_timestamps") or {}).items():
                try:
                    cts[int(pid)] = float(ts)
                except Exception:
                    continue
            t.creation_timestamps = cts

            t.user_server = {str(uid): str(lbl) for uid, lbl in (snapshot.get("user_server") or {}).items() if uid is not None}
            t.pid_grace_until = {str(uid): float(ts) for uid, ts in (snapshot.get("pid_grace_until") or {}).items() if uid is not None}

            try:
                t.protection_period = int(snapshot.get("protection_period", getattr(t, "protection_period", 60)))
            except Exception:
                pass

            t.server_owner = {str(uid): str(owner) for uid, owner in (snapshot.get("server_owner") or {}).items() if uid is not None}
            t.user_ps_code = {str(uid): str(code) for uid, code in (snapshot.get("user_ps_code") or {}).items() if uid is not None}
            t.user_ps_place = {str(uid): str(place) for uid, place in (snapshot.get("user_ps_place") or {}).items() if uid is not None}

            t.reserved_servers = dict(snapshot.get("reserved_servers") or {})
            t.skip_until_by_user = {str(uid): float(ts) for uid, ts in (snapshot.get("skip_until_by_user") or {}).items() if uid is not None}
            t.share_to_link = dict(snapshot.get("share_to_link") or {})

            try:
                t.initialization_mode = False
            except Exception:
                pass
        except Exception:
            return

    def _apply_resume_state(self, resume_state: dict) -> None:
        if not isinstance(resume_state, dict) or not resume_state:
            return
        if not self.manager:
            return

        valid_uids = set(getattr(self.manager, "settings", {}) or {})
        now = time.time()

        snap_states = resume_state.get("user_states") or {}
        if isinstance(snap_states, dict) and getattr(self, "user_states", None):
            for uid, snap in snap_states.items():
                uid_s = str(uid)
                if uid_s not in self.user_states or not isinstance(snap, dict):
                    continue
                st = self.user_states[uid_s]
                for k in ("last_active", "inactive_since", "requires_restart", "status", "last_launch"):
                    if k in snap:
                        st[k] = snap.get(k)
                try:
                    info = self.manager.settings.get(uid_s, {})
                    st["user_info"] = info if isinstance(info, dict) else {}
                except Exception:
                    pass

        snap_lp = resume_state.get("log_pointers") or {}
        if isinstance(snap_lp, dict):
            self.log_pointers = {str(uid): int(pos) for uid, pos in snap_lp.items() if str(uid) in valid_uids}
            for uid in valid_uids:
                if uid in self.log_pointers:
                    continue
                info = self.manager.settings.get(uid, {}) or {}
                username = info.get("username") if isinstance(info, dict) else None
                log_path = find_log_for_username(username, allow_fallback=False)
                if log_path and os.path.isfile(log_path):
                    self.log_pointers[uid] = os.path.getsize(log_path)
                else:
                    self.log_pointers[uid] = 0

        snap_tt = resume_state.get("timing_trackers") or {}
        if isinstance(snap_tt, dict):
            if not getattr(self, "timing_trackers", None):
                self.timing_trackers = {}
            for k, v in snap_tt.items():
                try:
                    self.timing_trackers[str(k)] = float(v)
                except Exception:
                    continue

        snap_skip = resume_state.get("skip_until_by_user") or {}
        if isinstance(snap_skip, dict):
            self._skip_until_by_user = {
                str(uid): float(ts) for uid, ts in snap_skip.items() if str(uid) in valid_uids
            }

        snap_recent = resume_state.get("recent_handoffs") or {}
        if isinstance(snap_recent, dict):
            self._recent_handoffs = {
                str(uid): float(exp)
                for uid, exp in snap_recent.items()
                if str(uid) in valid_uids and float(exp) > now
            }

        try:
            self._restart_cursor = int(resume_state.get("restart_cursor", self._restart_cursor))
        except Exception:
            pass

        snap_ll = resume_state.get("last_launch") or {}
        if isinstance(snap_ll, dict):
            self.last_launch = {str(uid): float(ts) for uid, ts in snap_ll.items() if str(uid) in valid_uids}

        snap_active = resume_state.get("active_pool")
        snap_spare = resume_state.get("spare_pool")
        if isinstance(snap_active, (list, tuple, set)):
            self.active_pool = {str(uid) for uid in snap_active if str(uid) in valid_uids}
        if isinstance(snap_spare, (list, tuple, set)):
            self.spare_pool = {str(uid) for uid in snap_spare if str(uid) in valid_uids}

        snap_handoff = resume_state.get("handoff_for") or {}
        if isinstance(snap_handoff, dict):
            self.handoff_for = {
                str(d): str(s)
                for d, s in snap_handoff.items()
                if str(d) in valid_uids and str(s) in valid_uids
            }

        try:
            overlap = set(self.active_pool) & set(self.spare_pool)
            if overlap:
                self.spare_pool -= overlap
        except Exception:
            pass

        self._boot_phase = False

    # -------------- utilities -----------------
    def _log(self, msg: str):
        self.log_signal.emit(msg)

    def _trace(self, uid: str, msg: str, *, every: float = 30.0) -> None:
        now = time.time()
        key = (uid, msg.split()[0])
        if not hasattr(self, "_trace_ts"):
            self._trace_ts = {}
        last = self._trace_ts.get(key, 0.0)
        if now - last >= every:
            self.log_signal.emit(f"[SCAN-TRACE] {uid}: {msg}")
            self._trace_ts[key] = now

    # -------------- pools ---------------------
    def _recompute_pools(self):
        # Prefer live user_states (reflects in-memory flag/disabled flips immediately)
        if getattr(self, "user_states", None):
            source_items = [
                (uid, st.get("user_info", {}))
                for uid, st in self.user_states.items()
            ]
        else:
            # Fallback during very early init
            source_items = list(self.manager.settings.items())

        total_users = len(source_items)
        flagged_count = sum(1 for _uid, info in source_items if info.get("bad", False) or info.get("cap", False))
        disabled_count = sum(1 for _uid, info in source_items if info.get("disabled", False))

        good_sorted = sorted(
            uid for uid, info in source_items
            if not (info.get("bad", False) or info.get("cap", False)) and not info.get("disabled", False)
        )

        if self.spares_mode:
            n = len(good_sorted)
            # ceil(n * num/den) without importing math
            target_active = max(1, (n * self._spares_num + self._spares_den - 1) // self._spares_den)
        else:
            target_active = len(good_sorted)

        self.active_pool = set(good_sorted[:target_active])
        self.spare_pool  = set(good_sorted[target_active:])

        self._log(
            f"Pools set - spares_mode={self.spares_mode} active={len(self.active_pool)} "
            f"spare={len(self.spare_pool)} total={total_users} flagged={flagged_count} disabled={disabled_count}"
        )


    def _eligible_spares(self):
        now = time.time()
        for uid in sorted(self.spare_pool):
            st = self.user_states.get(uid)
            # Skip if flagged in live state OR in settings (covers recent flips + reloads)
            is_bad_live = bool(st and st.get("user_info", {}).get("bad", False))
            is_cap_live = bool(st and st.get("user_info", {}).get("cap", False))
            is_bad_cfg  = bool(self.manager.settings.get(uid, {}).get("bad", False))
            is_cap_cfg  = bool(self.manager.settings.get(uid, {}).get("cap", False))
            is_disabled_live = bool(st and st.get("user_info", {}).get("disabled", False))
            is_disabled_cfg  = bool(self.manager.settings.get(uid, {}).get("disabled", False))
            if is_bad_live or is_cap_live or is_bad_cfg or is_cap_cfg or is_disabled_live or is_disabled_cfg:
                continue

            live = [
                pid for pid in self.manager.process_tracker.user_processes.get(uid, [])
                if self.process_mgr.verify_process_active(pid)
            ]
            if live:
                continue

            if now - st.get("last_launch", 0) < self.manager.timeouts["launch_delay"]:
                continue

            yield uid
    
    def _pick_spare_for(self, target_owner: str, target_code: str) -> str | None:
        """
        Return a spare uid, *preferring* the one that OWNS the target server.
        Ownership is determined by users.json private_server_linkCode -> uid.
        Falls back to any eligible spare if the owner is unavailable.
        """
        target_owner = (target_owner or "").strip().lower()
        target_code  = (target_code  or "").strip()

        elig = list(self._eligible_spares())
        if not elig:
            return None

        # Load users.json once
        try:
            users_cfg = self.cfg_manager.peek_users() or {}
        except Exception:
            users_cfg = {}

        # Quick helpers
        import re
        def _code_from_link(link: str) -> str:
            if not link:
                return ""
            m = re.search(r'privateServerLinkCode=([A-Za-z0-9_-]+)', link)
            if m:
                return m.group(1)
            m = re.search(r'/share\?code=([A-Za-z0-9_-]+)&type=Server', link)
            return m.group(1) if m else ""

        # Resolve owner UID by code first, then by username
        owner_uid = None
        if target_code:
            for _uid, info in users_cfg.items():
                if _code_from_link((info.get("private_server_link") or "").strip()) == target_code:
                    owner_uid = _uid
                    break
        if not owner_uid and target_owner:
            for _uid, info in users_cfg.items():
                if (info.get("username","").strip().lower() == target_owner):
                    owner_uid = _uid
                    break

        owners, others = [], []
        for uid in elig:
            info  = users_cfg.get(uid, {}) if isinstance(users_cfg, dict) else {}
            uname = (info.get("username") or "").strip().lower()
            code  = _code_from_link((info.get("private_server_link") or "").strip())

            if (owner_uid and uid == owner_uid) or \
            (target_owner and uname == target_owner) or \
            (target_code and code == target_code):
                owners.append(uid)
            else:
                others.append(uid)

        return (owners[0] if owners else (others[0] if others else None))

    def _compute_server_label_gui(self, info: dict) -> str:
        """
        Compute the exact server label the launcher will use:
        - Private server => first 10 chars of resolved linkCode
        - Public => 'Public:<placeId>'
        Prefer the launcher's helper if it exists; else resolve here.
        """
        cookie = (info or {}).get("cookie", "")
        # If the launcher exposes a canonical helper, use it (best case)
        if hasattr(self.launcher, "compute_server_label"):
            try:
                return self.launcher.compute_server_label(info or {}, cookie)
            except Exception:
                pass

        # Fallback: local resolve, matching launcher's behavior
        ps_link = (info or {}).get("private_server_link", "")
        place   = (info or {}).get("place", "") or str(self.manager.target_place)

        place_id, code, link_type = self.launcher._extract_private_server_info(ps_link, cookie=None)
        if link_type == "share" and code:
            rp, rc = self.launcher._convert_share_link(code, cookie)
            if rp and rc:
                place_id, code, link_type = rp, rc, "resolved"

        target_place = str(place_id or place or self.manager.target_place)
        return (f"{(code or '')[:10]}" if code else f"Public:{target_place}")

    def _normal_launch_conflict(self, uid: str, info: dict) -> tuple[bool, str, str]:
        """
        Preflight: would a *normal* launch for this user collide with a server already active
        or currently reserved by someone else (e.g., a handoff pre-join)?
        Returns (True/False, server_label, conflicting_uid_or_empty).
        """
        server_label = self._compute_server_label_gui(info)
        is_public = server_label.startswith("Public:")

        # 1) Live occupant check
        if not is_public:
            for other_uid, other_label in (self.manager.process_tracker.user_server or {}).items():
                if other_uid == uid or not other_label:
                    continue
                if other_label == server_label:
                    return True, server_label, other_uid

        # 2) Reservation check (blocks races during handoffs)
        rs = getattr(self.manager.process_tracker, "reserved_servers", {})
        r  = rs.get(server_label)
        if r and r.get("by") != uid and r.get("exp", 0) > time.time():
            return True, server_label, (r.get("by") or "")

        # 3) Owner-based fallback (same PS under a different label variant)
        ps_link = (info or {}).get("private_server_link", "")
        # When label is public we pass empty code; when private we pass the short code (label itself).
        owner = self.launcher._find_ps_owner_username(ps_link, "" if server_label.startswith("Public:") else server_label)
        if owner:
            owner_lc = owner.strip().lower()
            for other_uid, _ in (self.manager.process_tracker.user_server or {}).items():
                if other_uid == uid:
                    continue
                o_name = (self.manager.settings.get(other_uid, {}).get("username") or "").strip().lower()
                if o_name and o_name == owner_lc:
                    return True, server_label, other_uid

        return False, server_label, ""

    # -------------- handoff -------------------
    def _launch_spare_into_donors_server(self, donor_uid: str, donor_info: dict) -> bool:
        """
        Launch a spare to pre-join donor's exact server.
        Preference: pick a spare that owns that server.
        Also reserves the server so normal launches won't collide during the race.
        """
        # Determine donor's live place+code (authoritative if captured at launch)
        donor_live_code  = self.manager.process_tracker.user_ps_code.get(donor_uid, "")
        donor_live_place = self.manager.process_tracker.user_ps_place.get(donor_uid, "") \
                        or donor_info.get("place", "") \
                        or str(self.manager.target_place)

        # Owner (from tracker, already resolved by launcher)
        donor_owner = (self.manager.process_tracker.server_owner.get(donor_uid, "") or "").strip()

        # Pick a spare (prefer real owner, else any eligible)
        spare_uid = self._pick_spare_for(donor_owner, donor_live_code)
        if not spare_uid:
            return False

        # Build override to force joining donor's exact server
        spare_info = self.user_states[spare_uid]["user_info"]
        override   = dict(spare_info)
        override["allow_shared_server"] = True  # handoff exception: joining donor’s live server

        # Double-check disk flags
        users_cfg = self.cfg_manager.load_users() or {}
        if users_cfg.get(spare_uid, {}).get("bad", False) or users_cfg.get(spare_uid, {}).get("cap", False):
            self._log(f"Skip spare {spare_uid}: marked flagged in users.json")
            return False

        if donor_live_code:
            override["private_server_link"] = (
                f"https://www.roblox.com/games/{donor_live_place}/_/join"
                f"?privateServerLinkCode={donor_live_code}"
            )
            override["place"] = str(donor_live_place)
            server_label = donor_live_code[:10]
        else:
            override["private_server_link"] = ""
            override["place"] = str(donor_live_place)
            server_label = f"Public:{donor_live_place}"

        if not self._wait_for_launch_gate_ready():
            return False

        # Reserve this server while the spare is spinning up
        self._reserve_server(server_label, spare_uid, "handoff")

        cookie = override.get("cookie", "")
        ok = self.launcher.start_game_session(spare_uid, cookie, override)
        if ok:
            self.handoff_for[donor_uid] = spare_uid
            now2 = time.time()
            self.user_states[spare_uid]["last_launch"] = now2
            self._register_successful_launch(spare_uid, now2)
            self._log(
                f"Spare {spare_uid} pre-joined {donor_uid}'s server"
                + (f" (owner-pref: {donor_owner})" if donor_owner else "")
            )
            if self.ms:
                self.ms.begin_handoff(donor_uid, spare_uid)
            return True

        # If failed, drop the reservation immediately
        try:
            rs = getattr(self.manager.process_tracker, "reserved_servers", {})
            meta = rs.get(server_label)
            if meta and meta.get("by") == spare_uid:
                rs.pop(server_label, None)
        except Exception:
            pass
        return False


    def _complete_handoff(self, donor_uid: str, spare_uid: str):
        """Swap pools once donor is down and spare is up."""
        if donor_uid in self.active_pool:
            self.active_pool.remove(donor_uid)
            self.spare_pool.add(donor_uid)
        if spare_uid in self.spare_pool:
            self.spare_pool.remove(spare_uid)
            self.active_pool.add(spare_uid)
        self.handoff_for.pop(donor_uid, None)
        st = self.user_states.get(donor_uid, {})
        if st:
            st["requires_restart"] = False
            st["inactive_since"] = None
            st["status"] = "Standby"
        self._log(f"Completed handoff: {spare_uid} replaced {donor_uid}")

        # Multiscope: tell engine donor is done
        if self.ms:
            self.ms.complete_handoff(donor_uid)
        # NEW: protect the new active user for a short window so dedupe never snipes it
        self._recent_handoffs[spare_uid] = time.time() + self.handoff_grace
        


    # -------------- manager init --------------
    def _apply_multiscope_webhook_settings(self, cfg: dict) -> None:
        if not self.ms or not isinstance(cfg, dict):
            return
        ms_cfg = cfg.get("multiscope") or {}
        if not isinstance(ms_cfg, dict):
            ms_cfg = {}
        misc_cfg = cfg.get("misc") or {}
        if not isinstance(misc_cfg, dict):
            misc_cfg = {}
        ocr_cfg = cfg.get("ocr") or {}
        if not isinstance(ocr_cfg, dict):
            ocr_cfg = {}
        self.ms.configure_webhooks(
            biome_webhooks=cfg.get("webhooks", []),
            merchant_hook=ms_cfg.get("merchant_webhook", ""),
            enable_jester=ms_cfg.get("enable_jester", True),
            enable_mari=ms_cfg.get("enable_mari", True),
            enable_rin=ms_cfg.get("enable_rin", True),
            jester_ping=ms_cfg.get("jester_ping", ""),
            mari_ping=ms_cfg.get("mari_ping", ""),
            rin_ping=ms_cfg.get("rin_ping", ""),
            merchant_detection_mode=ms_cfg.get("merchant_detection_mode", "asset_id"),
            disable_log_based_merchant_detection=_should_disable_log_based_merchant_detection(cfg),
            merchant_rate_limit=float(ms_cfg.get("merchant_rate_limit", 15)),
            biome_min_interval=float(ms_cfg.get("biome_min_interval", 2)),
            skip_webhook_unknown_context=bool(
                misc_cfg.get(
                    "skip_webhook_unknown_context",
                    ocr_cfg.get("skip_webhook_unknown_context", False),
                )
            ),
        )

    def refresh_multiscope_settings(self, cfg: dict) -> None:
        try:
            self._apply_multiscope_webhook_settings(cfg)
        except Exception:
            pass

    def initialize_manager(self) -> bool:
        try:
            resume_state = getattr(self, "_resume_state", None) or None

            self.manager = RobloxManager(config_manager=self.cfg_manager)
            if resume_state:
                try:
                    self._restore_process_tracker_snapshot(resume_state.get("process_tracker") or {})
                except Exception:
                    pass
            self.manager.timeout_monitor.start()

            self.restart_threshold = self.manager.timeouts["offline"]
            self.initial_delay     = self.manager.timeouts["initial_delay"]

            self.process_mgr = ProcessManager(self.manager.excluded_pid)
            self.launcher = GameLauncher(
                self.manager.target_place,
                self.process_mgr,
                self.manager.auth_handler,
                self.manager.process_tracker,
                self.manager.config_manager,
                launch_delay=self.manager.timeouts["launch_delay"],
                initial_delay=self.manager.timeouts["initial_delay"],
                log_fn=self._log,   # <-- add this
            )
            


            now = time.time()
            while not self.manager.timeout_monitor.msg_q.empty():
                self.log_signal.emit(self.manager.timeout_monitor.msg_q.get_nowait())

            self.user_states = {
                uid: {
                    "last_active"    : now,
                    "inactive_since" : None,
                    "user_info"      : info,
                    "requires_restart": False,
                    "status"         : "Initializing"
                } for uid, info in self.manager.settings.items()
            }
            for uid, info in self.manager.settings.items():
                username = info.get("username") if isinstance(info, dict) else None
                log_path = find_log_for_username(username, allow_fallback=False)
                if log_path and os.path.isfile(log_path):
                    self.log_pointers[uid] = os.path.getsize(log_path)
                else:
                    self.log_pointers[uid] = 0

            self.timing_trackers = {'window': 0, 'cleanup': 0, 'relaunch': 0}

            # ───────────── Multiscope: run out-of-process ─────────────
            from multiscope_process import MultiScopeProcessProxy

            def _get_username(uid: str) -> str:
                info = self.manager.settings.get(uid, {})
                return str(info.get("username", ""))

            def _get_ps_link(uid: str) -> str:
                # Hide link if this account is currently flagged
                st = self.user_states.get(uid, {}) if hasattr(self, "user_states") else {}
                is_flagged = bool(
                    st.get("user_info", {}).get("bad", False)
                    or st.get("user_info", {}).get("cap", False)
                    or self.manager.settings.get(uid, {}).get("bad", False)
                    or self.manager.settings.get(uid, {}).get("cap", False)
                )
                if is_flagged:
                    return ""

                # 1) Prefer the live code/place captured at launch for THIS uid
                code  = (self.manager.process_tracker.user_ps_code or {}).get(uid, "")  # e.g., ABCDEFGH
                place = (self.manager.process_tracker.user_ps_place or {}).get(uid, "") # e.g., "15532962292"

                # 2) If missing, try to borrow from any teammate currently on the same server label
                if not code:
                    my_label = self.manager.process_tracker.user_server.get(uid, "")
                    if my_label:
                        for other_uid, other_label in (self.manager.process_tracker.user_server or {}).items():
                            if other_uid == uid:
                                continue
                            if other_label == my_label:
                                oc = (self.manager.process_tracker.user_ps_code or {}).get(other_uid, "")
                                op = (self.manager.process_tracker.user_ps_place or {}).get(other_uid, "")
                                if oc:
                                    code, place = oc, op
                                    break

                # 3) If we still have no code, there’s no private link to show
                if not code:
                    return ""

                # 4) Build a canonical PS join link
                place = str(place or self.manager.target_place or "15532962292").strip()
                return f"https://www.roblox.com/games/{place}/join?privateServerLinkCode={code}"

            # Prefer owner from main.py tracker; fallback to users.json by matching PS code
            def _get_owner_for_ms(uid: str) -> str:
                # 1) main.py authoritative owner (separate path)
                owner = self.manager.process_tracker.server_owner.get(uid, "")
                if owner:
                    return owner.strip()

                # 2) fallback: derive from users.json via current PS label code
                import re
                server_label = self.manager.process_tracker.user_server.get(uid, "")
                m = re.search(r"PS[:\s•-]*([A-Za-z0-9_-]{5,})", server_label, re.I)
                label_code = m.group(1) if m else ""

                try:
                    users_cfg = self.cfg_manager.load_users() or {}
                except Exception:
                    users_cfg = {}

                def _code_from_link(link: str) -> str:
                    if not link:
                        return ""
                    m1 = re.search(r'privateServerLinkCode=([A-Za-z0-9_-]+)', link)
                    if m1:
                        return m1.group(1)
                    m2 = re.search(r'/share\?code=([A-Za-z0-9_-]+)&type=Server', link)
                    if m2:
                        return m2.group(1)
                    return ""

                if label_code:
                    for _uid, info in users_cfg.items():
                        link = (info.get("private_server_link") or "").strip()
                        if _code_from_link(link) == label_code:
                            return (info.get("username") or "").strip()

                # 3) last resort
                info = self.manager.settings.get(uid, {})
                return str(info.get("username", "")).strip()
            
            def _get_cookie(uid: str) -> str:
                info = self.manager.settings.get(uid, {}) or {}
                return str(info.get("cookie") or "")

            # Expose helpers for other workers (OCR, etc.)
            self.get_ps_link_for_user = _get_ps_link
            self.get_owner_for_user = _get_owner_for_ms
            self.get_username_for_user = _get_username

            usernames_by_uid = {str(uid): _get_username(str(uid)) for uid in self.user_states.keys()}
            cookies_by_uid = {str(uid): _get_cookie(str(uid)) for uid in self.user_states.keys()}

            self.ms = MultiScopeProcessProxy(
                usernames_by_uid=usernames_by_uid,
                cookies_by_uid=cookies_by_uid,
                stats_path=str(self.cfg_manager.config_dir / "found_stats.json"),
                log_fn=self._log,
            )



            # Provide full user list AFTER user_states are built
            self.ms.update_users(list(self.user_states.keys()))

            # Load and push webhook config
            cfg = self.cfg_manager.load_settings() or {}
            self._apply_multiscope_webhook_settings(cfg)

            # ↓↓↓ ensure spares_mode, delays, and pools are live before first launch
            self.apply_new_settings(cfg)

            if resume_state:
                try:
                    ms_state = resume_state.get("multiscope_state") or {}
                    if self.ms and isinstance(ms_state, dict) and ms_state:
                        applied = bool(self.ms.import_state(ms_state))
                        if not applied:
                            try:
                                self._log("[Multiscope] import_state returned False; using cached rows until signals")
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    self._apply_resume_state(resume_state)
                except Exception:
                    pass
                self._resume_state = None

            return True
        except Exception as e:
            self.log_signal.emit(f"Manager init failed: {e}")
            return False


    def apply_new_settings(self, cfg: dict):
        if not cfg:
            return
        # base timings
        self.manager.window_limit               = cfg.get("window_limit", 1)
        t = cfg.get("timeouts", {})
        self.manager.timeouts["launch_delay"]  = t.get("launch_delay", 4)
        self.manager.timeouts["offline"]       = t.get("offline", 35)
        self.manager.timeouts["initial_delay"] = t.get("initial_delay", 4)
        self.strap_threshold                    = t.get("strap_threshold", 50)
        misc_cfg = cfg.get("misc", {}) or {}
        if not isinstance(misc_cfg, dict):
            misc_cfg = {}
        self.launch_wait_for_log_mode = bool(misc_cfg.get("log_confirmed_launch_mode", False))
        self.msedgewebview2_limiter_enabled = bool(
            misc_cfg.get("msedgewebview2_limiter_enabled", True)
        )
        if not self.launch_wait_for_log_mode:
            self._clear_launch_gate()
        if not self.msedgewebview2_limiter_enabled:
            self._msedge_timed_loop_active = False
            self._msedge_timed_loop_next_at = 0.0

        tm = cfg.get("timeout_monitor", {})
        self.manager.timeout_monitor.kill_enabled  = bool(tm.get("kill_enabled", True))
        self.manager.timeout_monitor.kill_timeout  = tm.get("kill_timeout", 1740)
        self.manager.timeout_monitor.poll_interval = tm.get("poll_interval", 10)
        alerts = cfg.get("alerts", {}) or {}
        if not isinstance(alerts, dict):
            alerts = {}
        self.manager.timeout_monitor.webhook_url = str(
            alerts.get("webhook_url") or tm.get("webhook_url", "") or ""
        ).strip()
        self.manager.timeout_monitor.ping_message = str(
            alerts.get("blackout_ping") or alerts.get("ping_message") or tm.get("ping_message", "") or ""
        ).strip()


        # spare / handoff
        self.spares_mode       = cfg.get("spares_mode", False)
        self.handoff_lead      = t.get("handoff_lead", 25)
        self.early_join_window = t.get("early_join_window", 90)
        
        # NEW: parse "spares_fraction" like "3/4" into ints (fallback to 1/2)
        frac = str(cfg.get("spares_fraction", "1/2")).strip()
        try:
            num_s, den_s = frac.split("/", 1)
            num, den = int(num_s), int(den_s)
            if 1 <= num < den <= 10:   # simple sanity guard
                self._spares_num, self._spares_den = num, den
            else:
                self._spares_num, self._spares_den = 1, 2
        except Exception:
            self._spares_num, self._spares_den = 1, 2

        self.restart_threshold = self.manager.timeouts["offline"]
        self.initial_delay     = self.manager.timeouts["initial_delay"]
        if self.launcher:
            self.launcher.launch_delay  = self.manager.timeouts["launch_delay"]
            self.launcher.initial_delay = self.manager.timeouts["initial_delay"]

        # ── Multiscope webhooks (biomes + merchants) ──
        try:
            if self.ms:
                self._apply_multiscope_webhook_settings(cfg)
        except Exception:
            pass

        self._recompute_pools()

    def _ramp_up_launches(self):
        """Launch initial sessions in the background so the main loop (and ms.tick) stays active."""
        try:
            # mirror old 'initialization' behavior for any consumers
            try:
                self.manager.process_tracker.initialization_mode = True
            except Exception:
                pass

            launch_list = sorted(self.active_pool) if self.spares_mode else [uid for uid, _ in self.manager.settings.items()]
            total = len(launch_list)

            for i, uid in enumerate(launch_list):
                if not self.running:
                    break

                st = self.user_states.get(uid, {})
                info = st.get("user_info", {})
                if info.get("disabled", False):
                    continue
                if info.get("bad", False) or info.get("cap", False):
                    continue

                # If this user already has a live process (e.g. after Pause->Resume),
                # don't launch another instance.
                try:
                    live = [
                        pid for pid in self.manager.process_tracker.user_processes.get(uid, [])
                        if self.process_mgr.verify_process_active(pid)
                    ]
                except Exception:
                    live = []
                if live:
                    continue
                if not self._wait_for_launch_gate_ready():
                    break

                cookie = info.get("cookie", "")
                attempted = False
                try:
                    attempted = True
                    ok = bool(self.launcher.start_game_session(uid, cookie, info, skip_cleanup=True))
                    now2 = time.time()
                    try:
                        self.user_states[uid]["last_launch"] = now2
                        if ok:
                            self.user_states[uid]["inactive_since"] = None
                            self.user_states[uid]["requires_restart"] = False
                            self.user_states[uid]["status"] = "Restarting"
                            self._register_successful_launch(uid, now2)
                        else:
                            self.user_states[uid]["requires_restart"] = True
                            if self.user_states[uid].get("status") not in ("Bad", "CAP", "Disabled"):
                                self.user_states[uid]["status"] = "Offline"
                    except Exception:
                        pass
                except Exception:
                    attempted = True
                    try:
                        now2 = time.time()
                        self.user_states[uid]["last_launch"] = now2
                        self.user_states[uid]["requires_restart"] = True
                        if self.user_states[uid].get("status") not in ("Bad", "CAP", "Disabled"):
                            self.user_states[uid]["status"] = "Offline"
                    except Exception:
                        pass

                # optional: a quick warm tick; main loop is already ticking continuously
                try:
                    self._ms_prelaunch_tick()
                except Exception:
                    pass

                # Apply initial_delay only BETWEEN attempted launches, so Pause->Resume doesn't
                # hold initialization_mode for ages when all users are already running.
                if (not self.launch_wait_for_log_mode) and attempted and i < total - 1:
                    ticks = max(0, int(self.initial_delay * 10))  # 0.1s steps for responsive stop()
                    for _ in range(ticks):
                        if not self.running:
                            break
                        time.sleep(0.1)
                    if not self.running:
                        break
        except Exception as e:
            self._log(f"[RampUp] error: {e}")
        finally:
            # end boot phase → subsequent launches use launch_delay
            self._boot_phase = False
            try:
                self.manager.process_tracker.initialization_mode = False
            except Exception:
                pass

    # -------------- actions -------------------
    def restart_user_session(self, user_id):
        if not self.manager or user_id not in self.user_states:
            return False
        try:
            # cancel in-flight mapping on manual restart
            self.handoff_for.pop(user_id, None)

            for pid in self.manager.process_tracker.user_processes.get(user_id, []):
                if self.process_mgr.verify_process_active(pid):
                    self.process_mgr.terminate_process(pid, self.manager.process_tracker)
            info = self.user_states[user_id]["user_info"]
            cookie = info.get("cookie", "") if isinstance(info, dict) else info
            ok = self.launcher.start_game_session(user_id, cookie, info)
            if ok:
                now2 = time.time()
                self.user_states[user_id]["inactive_since"] = None
                self.user_states[user_id]["requires_restart"] = False
                self.user_states[user_id]["skip_reconnect_on_disconnect"] = False
                self.user_states[user_id]["status"] = "Restarting"
                self.user_states[user_id]["last_launch"] = now2
                self._register_successful_launch(user_id, now2)
            return ok
        except Exception:
            return False

    def launch_user_session_custom(self, user_id: str, user_info: dict, *, skip_cleanup: bool = False) -> bool:
        """
        Launch a user using an explicit user_info dict (supports per-click overrides like a shared PS link).
        This does not persist changes back to users.json; it only affects this launch call.
        """
        if not self.manager or user_id not in self.user_states:
            return False
        try:
            # cancel in-flight mapping on manual launch
            self.handoff_for.pop(user_id, None)

            # kill any existing instances for this user first
            for pid in self.manager.process_tracker.user_processes.get(user_id, []):
                if self.process_mgr.verify_process_active(pid):
                    self.process_mgr.terminate_process(pid, self.manager.process_tracker)

            info = user_info if isinstance(user_info, dict) else {}
            cookie = str(info.get("cookie", "") or "")
            ok = bool(self.launcher.start_game_session(user_id, cookie, info, skip_cleanup=bool(skip_cleanup)))
            if ok:
                now2 = time.time()
                self.user_states[user_id]["inactive_since"] = None
                self.user_states[user_id]["requires_restart"] = False
                self.user_states[user_id]["skip_reconnect_on_disconnect"] = False
                self.user_states[user_id]["status"] = "Restarting"
                self.user_states[user_id]["last_launch"] = now2
                self._register_successful_launch(user_id, now2)
            return ok
        except Exception:
            return False

    def kill_user_processes(self, user_id: str) -> bool:
        try:
            for pid in self.manager.process_tracker.user_processes.get(user_id, []).copy():
                self.process_mgr.terminate_process(pid, self.manager.process_tracker)
            return True
        except Exception:
            return False

    def kill_all_processes(self):
        try:
            return self.process_mgr.terminate_process(None, self.manager.process_tracker)
        except Exception:
            return False

    def cleanup_dead_processes(self):
        try:
            self.process_mgr.cleanup_dead_processes(self.manager.process_tracker)
            return True
        except Exception:
            return False

    # -------------- thread loop ---------------
    def run(self):
        if not self.initialize_manager():
            return
        self.running = True
        # Safety: if spare mode is on but pools haven’t been computed yet, do it now.
        if self.spares_mode and not self.active_pool:
            self._recompute_pools()

        # Start launching in the background so ms.tick is fully active now
        self._ramp_thread = threading.Thread(target=self._ramp_up_launches, name="JARAM-RampUp", daemon=True)
        self._ramp_thread.start()

        while self.running:
            try:
                now = time.time()
                
                # ---- Hot reload users.json (propagate flag flips, etc.) ----
                fresh_map = None
                try:
                    users_mtime = float(self.cfg_manager.get_users_mtime())
                except Exception:
                    users_mtime = 0.0
                if getattr(self, "_users_mtime_seen", None) != users_mtime:
                    self._users_mtime_seen = users_mtime
                    try:
                        fresh_map = self.cfg_manager.get_users_for_manager() or {}
                    except Exception:
                        fresh_map = {}

                # Only do work if something actually changed
                if fresh_map and fresh_map != self.manager.settings:
                    old_ids = set(self.manager.settings.keys())
                    new_ids = set(fresh_map.keys())

                    # Snapshot old flags so we can apply transition behavior (disable/enable, flag flips).
                    old_flags: dict[str, dict] = {}
                    try:
                        for uid in (old_ids & new_ids):
                            st = self.user_states.get(uid, {})
                            info0 = st.get("user_info", {}) if isinstance(st, dict) else {}
                            if isinstance(info0, dict):
                                old_flags[str(uid)] = {
                                    "disabled": bool(info0.get("disabled", False)),
                                    "bad": bool(info0.get("bad", False)),
                                    "cap": bool(info0.get("cap", False)),
                                }
                            else:
                                old_flags[str(uid)] = {"disabled": False, "bad": False, "cap": False}
                    except Exception:
                        old_flags = {}

                    # 1) Remove users that no longer exist
                    for uid in (old_ids - new_ids):
                        # Optional: terminate any lingering processes
                        for pid in self.manager.process_tracker.user_processes.get(uid, []).copy():
                            if self.process_mgr.verify_process_active(pid):
                                self.process_mgr.terminate_process(pid, self.manager.process_tracker)
                        self.user_states.pop(uid, None)
                        self.handoff_for.pop(uid, None)
                        self.active_pool.discard(uid)
                        self.spare_pool.discard(uid)

                    # 2) Replace settings (source of truth for user_info)
                    self.manager.settings = fresh_map

                    # 3) Update existing users in-place so flags/cookies/links apply immediately
                    now2 = time.time()
                    for uid in (old_ids & new_ids):
                        st = self.user_states.get(uid)
                        if not isinstance(st, dict):
                            continue

                        new_info = self.manager.settings.get(uid, {})
                        if not isinstance(new_info, dict):
                            new_info = {}
                        st["user_info"] = new_info  # keep it in sync with manager.settings

                        prev = old_flags.get(str(uid), {}) if isinstance(old_flags, dict) else {}
                        old_disabled = bool(prev.get("disabled", False))
                        old_bad = bool(prev.get("bad", False))
                        old_cap = bool(prev.get("cap", False))
                        old_flagged = old_bad or old_cap

                        new_disabled = bool(new_info.get("disabled", False))
                        new_bad = bool(new_info.get("bad", False))
                        new_cap = bool(new_info.get("cap", False))
                        new_flagged = new_bad or new_cap

                        # If newly disabled, terminate any live processes now so it stays off.
                        if new_disabled and not old_disabled:
                            try:
                                self.kill_user_processes(str(uid))
                            except Exception:
                                pass

                        # Cancel any in-flight handoff roles for accounts that are now excluded.
                        if new_disabled or new_flagged:
                            try:
                                self.handoff_for.pop(str(uid), None)
                                donors = [d for d, s in list(self.handoff_for.items()) if s == str(uid)]
                                for d in donors:
                                    self.handoff_for.pop(d, None)
                            except Exception:
                                pass

                            try:
                                st["requires_restart"] = False
                                st["inactive_since"] = None
                                st["status"] = "Disabled" if new_disabled else ("CAP" if new_cap else "Bad")
                            except Exception:
                                pass
                        else:
                            # If the account just became eligible again, relaunch it if it's currently offline.
                            if (old_disabled or old_flagged) and not (new_disabled or new_flagged):
                                live = []
                                try:
                                    live = [
                                        pid for pid in (self.manager.process_tracker.user_processes.get(str(uid), []) or [])
                                        if self.process_mgr.verify_process_active(pid)
                                    ]
                                except Exception:
                                    live = []
                                if not live:
                                    try:
                                        st["requires_restart"] = True
                                        st["inactive_since"] = None
                                        st["status"] = "Offline"
                                        st["last_active"] = now2
                                    except Exception:
                                        pass

                    # 4) Add new users (if any)
                    for uid in (new_ids - old_ids):
                        info = self.manager.settings.get(uid, {})
                        if not isinstance(info, dict):
                            info = {}
                        is_bad = bool(info.get("bad", False))
                        is_cap = bool(info.get("cap", False))
                        is_disabled = bool(info.get("disabled", False))
                        self.user_states[uid] = {
                            "last_active": now2,
                            "inactive_since": None,
                            # New accounts should be eligible to launch promptly if they aren't excluded.
                            "requires_restart": bool((not is_bad) and (not is_cap) and (not is_disabled)),
                            "user_info": info,
                            "status": ("Disabled" if is_disabled else ("CAP" if is_cap else ("Bad" if is_bad else "Offline"))),
                        }

                    # 5) Re-compute pools (uses user_states user_info flags)
                    try:
                        self._recompute_pools()
                    except Exception as _e:
                        self._log(f"[Pools] recompute error: {_e}")

                # housekeeping: clean dead processes
                if 'cleanup' not in self.timing_trackers:
                    self.timing_trackers['cleanup'] = 0
                if 'window' not in self.timing_trackers:
                    self.timing_trackers['window'] = 0
                if 'relaunch' not in self.timing_trackers:
                    self.timing_trackers['relaunch'] = 0

                if now - self.timing_trackers['cleanup'] >= self.manager.check_intervals['cleanup']:
                    try:
                        self.process_mgr.cleanup_dead_processes(self.manager.process_tracker)
                    except Exception as e:
                        self._log(f"[Cleanup] error: {e!r}")
                    try:
                        limit_roblox_crash_handlers(threshold=2, kill_all=False)
                    except Exception:
                        pass
                    self.timing_trackers['cleanup'] = now

                if now - self.timing_trackers['window'] >= self.manager.check_intervals['window']:
                    try:
                        # enforce window cap: kill helpers with too many windows
                        win_counts = self.process_mgr.count_windows_by_process()
                        for pid, nwin in win_counts.items():
                            if nwin > self.manager.window_limit and pid != self.manager.excluded_pid:
                                uid = None
                                try:
                                    uid = self.manager.process_tracker.process_owners.get(pid)
                                except Exception:
                                    uid = None
                                self._log(f"[WindowCheck] killing pid={pid} uid={uid} nwin={nwin} limit={self.manager.window_limit}")
                                self.process_mgr.terminate_process(pid, self.manager.process_tracker)
                    except Exception as e:
                        self._log(f"[WindowCheck] error: {e!r}")
                    self.timing_trackers['window'] = now
                
                # After housekeeping, before relaunch logic
                self._enforce_one_per_server()
                self._prune_reservations()

                # --- sync flags + evict from pools immediately ---
                try:
                    changed = False
                    for uid, cfg_info in list(self.manager.settings.items()):
                        st = self.user_states.get(uid)
                        if not st:
                            continue
                        bad_disk = bool(cfg_info.get("bad", False))
                        cap_disk = bool(cfg_info.get("cap", False))
                        if st["user_info"].get("bad", False) != bad_disk:
                            st["user_info"]["bad"] = bad_disk
                            changed = True
                        if st["user_info"].get("cap", False) != cap_disk:
                            st["user_info"]["cap"] = cap_disk
                            changed = True

                        # If flagged, evict from both pools and cancel any in-flight handoff roles
                        if bad_disk or cap_disk:
                            if uid in self.active_pool or uid in self.spare_pool:
                                self.active_pool.discard(uid)
                                self.spare_pool.discard(uid)
                                changed = True
                        # If this flagged user is being used as a spare for a donor, cancel it
                            donors = [d for d, s in list(self.handoff_for.items()) if s == uid]
                            for d in donors:
                                self.handoff_for.pop(d, None)
                    if changed and self.spares_mode:
                        self._recompute_pools()
                except Exception as _e:
                    self._log(f"[Sync] flag sync error: {_e}")

                # Count live processes and guard against stalls
                active_processes = sum(
                    len([pid for pid in self.manager.process_tracker.user_processes.get(uid, [])
                        if self.process_mgr.verify_process_active(pid)])
                    for uid in list(self.manager.settings.keys())
                )
                total_users = len(self.manager.settings)
                STUCK_TIMEOUT = self.manager.check_intervals.get('stuck_guard', 90)

                if active_processes > self._last_proc_count:
                    self._last_proc_count = active_processes
                    self._last_growth_ts = now
                else:
                    self._last_proc_count = active_processes
                if active_processes < total_users and (now - self._last_growth_ts) >= STUCK_TIMEOUT:
                    limit_strap_helpers(threshold=1, kill_all=False)
                    self._last_growth_ts = now

                # per-user heartbeat
                status = {}
                kill_t = self.manager.timeout_monitor.kill_timeout
                strict_log_matches = {}
                live_by_uid = {}

                for uid, st in list(self.user_states.items()):
                    info = st["user_info"]
                    # flagged users
                    is_bad = bool(info.get("bad", False))
                    is_cap = bool(info.get("cap", False))
                    if is_bad or is_cap:
                        status[uid] = {
                            "status": "CAP" if is_cap else "Bad",
                            "pids": [],
                            "needs_restart": False,
                            "last_active": st.get("last_active", 0),
                            "inactive_since": st.get("inactive_since"),
                            "ttl": [],
                            "server": self.manager.process_tracker.user_server.get(uid, ""),
                            "ps_link": "",
                            "server_owner": "",
                        }
                        continue
                    # disabled users: do not launch/restart
                    if info.get("disabled", False):
                        status[uid] = {
                            "status": "Disabled",
                            "pids": [],
                            "needs_restart": False,
                            "last_active": st.get("last_active", 0),
                            "inactive_since": st.get("inactive_since"),
                            "ttl": [],
                            "server": self.manager.process_tracker.user_server.get(uid, ""),
                            "ps_link": "",
                            "server_owner": "",
                        }
                        st["requires_restart"] = False
                        continue
                    
                    ps_link = ""
                    try:
                        fn = getattr(self, "get_ps_link_for_user", None)
                        if callable(fn):
                            ps_link = str(fn(uid) or "")
                    except Exception:
                        ps_link = ""
                    server_owner = ""
                    try:
                        fn = getattr(self, "get_owner_for_user", None)
                        if callable(fn):
                            server_owner = str(fn(uid) or "")
                    except Exception:
                        server_owner = ""

                    live = [pid for pid in self.manager.process_tracker.user_processes.get(uid, [])
                            if self.process_mgr.verify_process_active(pid)]
                    live_by_uid[uid] = bool(live)
                    
                    # --- NEW: pre-connect watchdog -------------------------------------------
                    # If this account has live processes but we still don't have a strict log
                    # match for its username within 2 minutes of launch, assume it failed to
                    # connect and recycle it.
                    now = time.time()
                    uname = str(info.get("username", "")).lower()

                    if live:
                        # Strict lookup: only returns a path once the username actually appears in logs
                        log_path = find_log_for_username(uname, allow_fallback=False)
                        strict_log_matches[uid] = bool(log_path)
                        if log_path:
                            st["log_miss_streak"] = 0
                            self._mark_user_log_confirmed(uid)
                            self._maybe_trigger_msedge_kill_for_user(uid, strict_log_seen=True)

                        if not log_path:
                            # oldest process start for this user. If per-PID create times are
                            # missing, fall back to this user's last launch timestamp so the
                            # preconnect watchdog still advances.
                            ct_candidates = []
                            for pid in live:
                                try:
                                    ct = float(self.manager.process_tracker.creation_timestamps.get(pid, 0) or 0)
                                except Exception:
                                    ct = 0.0
                                if ct > 0:
                                    ct_candidates.append(ct)
                            if not ct_candidates:
                                try:
                                    fallback_ct = float(st.get("last_launch", 0) or 0)
                                except Exception:
                                    fallback_ct = 0.0
                                if fallback_ct <= 0:
                                    fallback_ct = now
                                ct_candidates.append(fallback_ct)
                            oldest_ct = min(ct_candidates)
                            waited = now - oldest_ct

                            if waited >= self.preconnect_grace:
                                self._log(f"⚠️  {uname} did not appear in logs within {self.preconnect_grace}s — terminating")
                                if not bool(info.get("cap", False)):
                                    streak = int(st.get("log_miss_streak", 0) or 0) + 1
                                    st["log_miss_streak"] = streak
                                    if streak >= 3:
                                        self._log(f"[CAP] {uname} missing in logs {streak}/3; marking CAP.")
                                        try:
                                            self.cfg_manager.mark_cap_flag(uid, True)
                                        except Exception:
                                            pass
                                        try:
                                            info["cap"] = True
                                            st["user_info"]["cap"] = True
                                        except Exception:
                                            pass
                                        try:
                                            self.active_pool.discard(uid)
                                            self.spare_pool.discard(uid)
                                        except Exception:
                                            pass
                                    else:
                                        self._log(f"[CAP] {uname} missing in logs {streak}/3; retrying.")
                                self.kill_user_processes(uid)
                                st["requires_restart"] = not bool(info.get("bad", False) or info.get("cap", False))
                                # clean up a bit so next launch is fresh
                                try:
                                    self.manager.process_tracker.user_server.pop(uid, None)
                                except Exception:
                                    pass
                                live = []
                                live_by_uid[uid] = False
                    else:
                        strict_log_matches[uid] = False

                    # --- end pre-connect watchdog ---

                    # status shape
                    ttl_list = []
                    min_ttl = None
                    for pid in live:
                        ct  = self.manager.process_tracker.creation_timestamps.get(pid, now)
                        ttl = max(0, int(kill_t - (now - ct)))
                        ttl_list.append(ttl)
                        if min_ttl is None or ttl < min_ttl:
                            min_ttl = ttl

                    # donor standby
                    if self.spares_mode and uid in self.handoff_for:
                        spare_uid = self.handoff_for[uid]
                        spare_live = any(
                            self.process_mgr.verify_process_active(pid)
                            for pid in self.manager.process_tracker.user_processes.get(spare_uid, [])
                        )
                        if spare_live:
                            st["status"] = "Standby (handoff)"
                            st["requires_restart"] = False
                            st["inactive_since"] = None
                            if not live:
                                self._complete_handoff(uid, spare_uid)

                            status[uid] = {
                                "status": st["status"],
                                "pids": live,
                                "needs_restart": False,
                                "last_active": st.get("last_active", 0),
                                "inactive_since": st.get("inactive_since"),
                                "ttl": ttl_list,
                                "server": self.manager.process_tracker.user_server.get(uid, ""),
                                "ps_link": ps_link,
                                "server_owner": server_owner,
                            }
                            continue

                    # active
                    if live:
                        st["status"] = "Active"
                        st["requires_restart"] = False
                        st["inactive_since"] = None
                        st["last_active"] = now
                        st["skip_reconnect_on_disconnect"] = False
                    else:
                        if st.get("inactive_since") is None:
                            st["inactive_since"] = now
                        skip_reconnect = bool(st.get("skip_reconnect_on_disconnect", False))
                        skip_cfg = bool(info.get("skip_reconnect_on_log_disconnect", False)) if isinstance(info, dict) else False
                        if skip_reconnect and (not skip_cfg):
                            st["skip_reconnect_on_disconnect"] = False
                            skip_reconnect = False
                        if (now - st.get("last_active", 0)) > self.restart_threshold:
                            if skip_reconnect:
                                st["requires_restart"] = False
                            else:
                                st["requires_restart"] = True
                        if st.get("status") != "Offline":
                            st["status"] = "Offline"

                    status[uid] = {
                        "status": st["status"],
                        "pids": live,
                        "needs_restart": st.get("requires_restart", False),
                        "last_active": st.get("last_active", 0),
                        "inactive_since": st.get("inactive_since"),
                        "ttl": ttl_list,
                        "server": self.manager.process_tracker.user_server.get(uid, ""),
                        "ps_link": ps_link,
                        "server_owner": server_owner,
                    }

                try:
                    self._is_launch_gate_ready(
                        now=time.time(),
                        strict_log_matches=strict_log_matches,
                        live_by_uid=live_by_uid,
                    )
                except Exception:
                    pass

                # After building status, keep pools synced with 'good' users immediately
                try:
                    current_good = {
                        u for u, s in self.user_states.items()
                        if not (s.get("user_info", {}).get("bad", False) or s.get("user_info", {}).get("cap", False))
                        and not s.get("user_info", {}).get("disabled", False)
                    }
                    if current_good != getattr(self, "_last_good_set", set()):
                        self._last_good_set = current_good
                        self._recompute_pools()
                except Exception as _e:
                    self._log(f"[Pools] good-set recompute error: {_e}")

                self.status_signal.emit(status)
                ms_rows_for_limiter = None
                
                # Multiscope: update detection and push snapshot to the tab
                try:
                    if hasattr(self, "ms") and self.ms:
                        self.ms.tick(status)                 # feed current user/server state
                        # Handle MultiScope signals (disconnects, etc.)
                        for kind, uid, payload in self.ms.drain_events():
                            if kind == "disconnect":
                                payload_text = str(payload or "")
                                uname = (self.manager.settings.get(uid, {}) or {}).get("username", uid)
                                info = {}
                                try:
                                    info = self.manager.settings.get(uid, {}) or {}
                                except Exception:
                                    info = {}
                                is_alt = bool(info.get("alternate_launch", False))
                                cap_triggered = False
                                if self._is_in_menu_none_disconnect_payload(payload_text):
                                    try:
                                        already_cap = bool(info.get("cap", False))
                                    except Exception:
                                        already_cap = False

                                    if not already_cap:
                                        st0 = self.user_states.get(uid, {})
                                        streak = int(st0.get("log_miss_streak", 0) or 0) + 1
                                        st0["log_miss_streak"] = streak
                                        if streak >= 3:
                                            cap_triggered = True
                                            self._log(f"[CAP] {uname} in_menu_none {streak}/3; marking CAP.")
                                            try:
                                                self.cfg_manager.mark_cap_flag(uid, True)
                                            except Exception:
                                                pass
                                            try:
                                                info["cap"] = True
                                                st0["user_info"]["cap"] = True
                                            except Exception:
                                                pass
                                            try:
                                                self.active_pool.discard(uid)
                                                self.spare_pool.discard(uid)
                                            except Exception:
                                                pass
                                        else:
                                            self._log(f"[CAP] {uname} in_menu_none {streak}/3; retrying.")
                                if bool(info.get("skip_reconnect_on_log_disconnect", False)) and self._is_log_disconnect_payload(payload_text):
                                    self._log(
                                        f"[Disconnect] {uname} - {payload_text}; auto-reconnect disabled for this account."
                                    )
                                    try:
                                        self.kill_user_processes(uid)
                                    except Exception:
                                        pass
                                    st = self.user_states.get(uid, {})
                                    st["skip_reconnect_on_disconnect"] = True
                                    st["requires_restart"] = False
                                    st["inactive_since"]   = None
                                    st["status"]           = "Offline"
                                    continue
                                if cap_triggered:
                                    self._log(f"[Disconnect] {uname} - {payload_text}; CAP streak hit (3/3).")
                                else:
                                    self._log(f"[Disconnect] {uname} - {payload_text}; restarting now.")
                                try:
                                    self.kill_user_processes(uid)
                                except Exception:
                                    pass
                                st = self.user_states.get(uid, {})
                                st["skip_reconnect_on_disconnect"] = False
                                st["requires_restart"] = not cap_triggered
                                st["inactive_since"]   = None
                                st["status"]           = "Offline"
                        rows = self.ms.snapshot()            # [{server, users, biome/merchant…}]
                        has_signal = False
                        try:
                            for r in (rows or []):
                                if not isinstance(r, dict):
                                    continue
                                if r.get("in_menu", None) is not None:
                                    has_signal = True
                                    break
                                b = r.get("last_biome", r.get("biome", ""))
                                if str(b or "").strip():
                                    has_signal = True
                                    break
                                m = r.get("last_merchant", r.get("merchant", ""))
                                if str(m or "").strip():
                                    has_signal = True
                                    break
                        except Exception:
                            has_signal = False

                        if self._bootstrap_multiscope_rows and time.time() < self._bootstrap_multiscope_deadline:
                            if (not rows) or (not has_signal):
                                rows = self._bootstrap_multiscope_rows
                            else:
                                self._bootstrap_multiscope_rows = None
                                self._bootstrap_multiscope_deadline = 0.0
                        elif rows and self._bootstrap_multiscope_rows:
                            self._bootstrap_multiscope_rows = None
                            self._bootstrap_multiscope_deadline = 0.0
                        ms_rows_for_limiter = rows if isinstance(rows, list) else None
                        self.multiscope_signal.emit(rows)    # GUI will render it
                except Exception as _e:
                    self._log(f"[Multiscope] tick error: {_e}")

                try:
                    self._drive_msedge_kill_timed_loop(
                        live_by_uid=live_by_uid,
                        multiscope_rows=ms_rows_for_limiter,
                        strict_log_matches=strict_log_matches,
                        now=time.time(),
                    )
                except Exception:
                    pass

                # process table signal
                proc_info = {}
                for uid, pids in list(self.manager.process_tracker.user_processes.items()):
                    for pid in list(pids):  # snapshot the list in case it changes
                        if not self.process_mgr.verify_process_active(pid):
                            continue
                        created = datetime.fromtimestamp(
                            self.manager.process_tracker.creation_timestamps.get(pid, time.time())
                        ).strftime("%H:%M:%S")
                        proc_info[pid] = {"user_id": uid, "created": created}
                self.process_signal.emit(proc_info)


                # auto-restart queue (skip donors in handoff)
                try:
                    restartables = [
                        u for u, s in list(self.user_states.items())
                        if s.get("requires_restart")
                        and not (s["user_info"].get("bad", False) or s["user_info"].get("cap", False))
                        and not s["user_info"].get("disabled", False)
                        and u not in self.handoff_for
                    ]
                    # Per-user gating: honor backoff and per-user launch_delay up front,
                    # so we don't keep picking the same blocked uid over and over.
                    now = time.time()
                    launch_gate_open = self._is_launch_gate_ready(
                        now=now,
                        strict_log_matches=strict_log_matches,
                        live_by_uid=live_by_uid,
                    )
                    def _eligible_now(u: str) -> bool:
                        st = self.user_states.get(u, {})
                        info = st.get("user_info", {})
                        if now < self._skip_until_by_user.get(u, 0):
                            return False
                        # per-user launch delay (boot uses initial_delay)
                        gate = self.initial_delay if self._boot_phase else self.manager.timeouts["launch_delay"]
                        if now - st.get("last_launch", 0) < gate:
                            return False
                        return True 

                    restartables = [u for u in restartables if _eligible_now(u)]

                    # Prefer accounts expected in active_pool, then apply round-robin starting point
                    restartables.sort(key=lambda u: (u not in self.active_pool, u))
                    if restartables:
                        # advance cursor into the sorted list (wrap around)
                        self._restart_cursor = self._restart_cursor % len(restartables)
                        ordered = restartables[self._restart_cursor:] + restartables[:self._restart_cursor]
                    else:
                        ordered = []

                    # Only attempt one launch per window; disable relaunches during boot
                    _global_gate = self.initial_delay if self._boot_phase else self.manager.timeouts["launch_delay"]

                    # block the relaunch queue entirely until ramp finishes
                    if self._boot_phase:
                        ordered = []

                    if ordered and launch_gate_open and (now - self.timing_trackers['relaunch']) >= _global_gate:
                        launched = False

                        for uid in ordered:
                            st    = self.user_states.get(uid, {})
                            info  = st.get("user_info", {})
                            cookie = info.get("cookie", "")

                            # If spares_mode and this is a spare, only launch to meet target count
                            if self.spares_mode and uid in self.spare_pool:
                                target_live = len(self.active_pool)
                                live_now = sum(1 for row in status.values() if row["status"] == "Active")
                                if live_now >= target_live:
                                    continue  # capacity satisfied; don't attempt, don't log

                            # Final preflight: only when we're about to actually attempt a launch
                            conflict, label, by_uid = self._normal_launch_conflict(uid, info)
                            if conflict:
                                # Back off this uid for a bit so it doesn't starve others
                                self._skip_until_by_user[uid] = now + 20
                                # Move the round-robin cursor past this uid for next time
                                self._restart_cursor = (self._restart_cursor + 1) % max(1, len(ordered))
                                # Try the next candidate
                                continue

                            # Safe to try launching
                            if self.launcher.start_game_session(uid, cookie, info):
                                now2 = time.time()
                                self.user_states[uid]["inactive_since"]   = None
                                self.user_states[uid]["requires_restart"] = False
                                self.user_states[uid]["status"]           = "Restarting"
                                self.user_states[uid]["last_launch"]      = now2
                                self.timing_trackers['relaunch']          = now2
                                self._register_successful_launch(uid, now2)
                                # Move round-robin forward from the *next* uid
                                self._restart_cursor = (self._restart_cursor + 1) % max(1, len(ordered))
                                launched = True
                                break
                            else:
                                # Launch failed for some other reason — short backoff on this uid
                                self._skip_until_by_user[uid] = now + 10
                                self._restart_cursor = (self._restart_cursor + 1) % max(1, len(ordered))
                                # keep trying next candidate in this tick

                        # If none launched, do not reset relaunch timer (we’ll retry next tick with new order)

                    if not ordered:
                        limit_strap_helpers(threshold=self.strap_threshold)
                except Exception as e:
                    self._log(f"[Relaunch] scheduler error: {e!r}")

                time.sleep(self.manager.check_intervals['main_tick'])
            except Exception as e:
                try:
                    self._log(f"[WorkerLoop] crash prevented: {e!r}")
                    import traceback
                    self._log(traceback.format_exc())
                except Exception:
                    pass
                time.sleep(0.2)
                continue
    
    def _enforce_one_per_server(self):
        p = self.manager.process_tracker

        # Prune expired cooldowns
        now = time.time()
        rh = getattr(self, "_recent_handoffs", {})
        for u, exp in list(rh.items()):
            if exp <= now:
                rh.pop(u, None)

        # Group by real labels only (skip synthetic pools)
        by_label = {}
        for uid, raw in (p.user_server or {}).items():
            if not raw:
                continue
            lbl = str(raw).strip()
            up = lbl.upper()
            if up.startswith("DISCONNECT") or up.startswith("UNKNOWN") or up.startswith("PUBLIC:"):
                continue  # never dedupe these pools
            by_label.setdefault(lbl, []).append(uid)

        # Build protection set:
        #  - always protect donor+spare while mapping exists
        #  - protect any uid within the recent-handoff cooldown window
        protected = set()
        for donor, spare in (self.handoff_for or {}).items():
            protected.update([donor, spare])
        protected.update([u for u, exp in rh.items() if exp > now])

        # Helper: earliest PID start time for tie-break
        def oldest_start(uid: str) -> float:
            times = [p.creation_timestamps.get(pid, float('inf'))
                    for pid in p.user_processes.get(uid, [])]
            return min(times) if times else float('inf')

        # Choose keeper per label by priority, then terminate extras
        for label, uids in by_label.items():
            if len(uids) <= 1:
                continue

            def prio(u: str):
                return (
                    0 if u in protected else 1,          # keep protected if present
                    0 if u in self.active_pool else 1,   # else prefer active-pool user
                    oldest_start(u)                      # else oldest start wins
                )

            keep = sorted(uids, key=prio)[0]

            for uid in uids:
                if uid == keep or uid in protected:
                    continue
                for pid in p.user_processes.get(uid, []):
                    if self.process_mgr.verify_process_active(pid):
                        self.process_mgr.terminate_process(pid, p)
                        self._log(f"[DEDUP] Killed extra instance pid={pid} uid={uid} on {label}")

    def _reserve_server(self, label: str, uid: str, purpose: str = "handoff"):
        """Mark a server label as reserved for a short window (prevents normal launches)."""
        if not label or not hasattr(self.manager, "process_tracker"):
            return
        now = time.time()
        rs = getattr(self.manager.process_tracker, "reserved_servers", None)
        if rs is None:
            self.manager.process_tracker.reserved_servers = rs = {}
        rs[label] = {"by": uid, "type": purpose, "exp": now + self._reservations_ttl}

    def _prune_reservations(self):
        """Expire old reservations (called each loop)."""
        if not hasattr(self.manager, "process_tracker"):
            return
        rs = getattr(self.manager.process_tracker, "reserved_servers", {})
        if not rs:
            return
        now = time.time()
        for lbl, meta in list(rs.items()):
            if meta.get("exp", 0) <= now:
                rs.pop(lbl, None)

    def stop(self, *, shutdown_ms: bool = True):
        self.running = False  # make the loop exit ASAP
        if shutdown_ms and getattr(self, "ms", None) and hasattr(self.ms, "shutdown"):
            try:
                self.ms.shutdown()
            except Exception:
                pass
        if self.manager and self.manager.timeout_monitor:
            try:
                self.manager.timeout_monitor.stop()
            except Exception:
                pass
        
        # don’t block long on ramp thread
        try:
            t = getattr(self, "_ramp_thread", None)
            if t and t.is_alive():
                t.join(timeout=0.5)
        except Exception:
            pass

class UserManagementDialogLegacy(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("User Account Management")
        self.setModal(True)

        self.resize(1400, 850)
        self.setMinimumSize(1200, 700)
        self.config_manager = ConfigManager()
        self.cookie_extractor = CookieExtractor(self)
        self.skip_account_private_link_warning = False
        self.skip_account_public_place_warning = False
        self._settings_baseline: Optional[dict] = None
        self._settings_prompt_ready = False
        self._tab_change_guard = False
        self._last_tab_index: Optional[int] = None
        self._settings_label_map = {
            "window_limit": "Window Limit",
            "spares_mode": "Spares Mode",
            "spares_fraction": "Spare Mode Split",
            "roblox_window_geometry.enforce_on_launch": "Auto-fix Roblox Window Geometry",
            "roblox_window_geometry.x": "Roblox Window X",
            "roblox_window_geometry.y": "Roblox Window Y",
            "roblox_window_geometry.w": "Roblox Window Width",
            "roblox_window_geometry.h": "Roblox Window Height",
            "timeouts.offline": "Restart Inactive After",
            "timeouts.initial_delay": "Initial Launch Delay",
            "timeouts.launch_delay": "Launch Delay",
            "timeouts.strap_threshold": "Strap Limit",
            "timeouts.handoff_lead": "Handoff Lead",
            "timeouts.early_join_window": "Early-Join Window",
            "timeout_monitor.kill_enabled": "Kill After Enabled",
            "timeout_monitor.kill_timeout": "Kill After",
            "timeout_monitor.poll_interval": "Poll Interval",
            "alerts.webhook_url": "Webhook URL",
            "alerts.blackout_ping": "Blackout Ping",
            "alerts.cap_message": "CAP Message",
            "alerts.bad_message": "BAD Message",
            "alerts.hourly_users_report_enabled": "Hourly Users Report",
            "alerts.hourly_users_report_interval_hours": "Hourly Users Report Interval",
            "webhooks": "Webhooks",
            "ui.webhooks_hidden_biomes": "Hidden Webhook Biome Columns",
            "ui.show_tutorial_menu": "Show Tutorial Menu Item",
            "misc.skip_webhook_unknown_context": "Skip Unknown-Context Webhooks",
            "misc.log_confirmed_launch_mode": "Launch Next After Log Confirm",
            "misc.disable_manager_bad_marking": "Disable Manager BAD Marking",
            "misc.msedgewebview2_limiter_enabled": "Enable msedgewebview2 Limiter",
            "multiscope.merchant_webhook": "Merchant Webhook URL",
            "multiscope.merchant_detection_mode": "Merchant Detection Mode",
            "multiscope.enable_jester": "Enable Jester Pings",
            "multiscope.enable_mari": "Enable Mari Pings",
            "multiscope.enable_rin": "Enable Rin Pings",
            "multiscope.jester_ping_type": "Jester Ping Type",
            "multiscope.jester_ping_id": "Jester Ping ID",
            "multiscope.mari_ping_type": "Mari Ping Type",
            "multiscope.mari_ping_id": "Mari Ping ID",
            "multiscope.rin_ping_type": "Rin Ping Type",
            "multiscope.rin_ping_id": "Rin Ping ID",
            "misc.disable_log_based_merchant_detection_when_ocr_merchants_enabled": "Disable Log Merchant Detection While OCR Merchants Active",
        }
        self.selected_user_id = None
        self.setup_ui()
        self.load_users()
        self.skip_private_server_warning = False      # session-only


    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setChildrenCollapsible(False)

        left_panel = self._create_user_list_panel()
        main_splitter.addWidget(left_panel)

        right_panel = self._create_user_form_panel()
        main_splitter.addWidget(right_panel)

        main_splitter.setSizes([980, 420])
        main_layout.addWidget(main_splitter)

        controls_layout = self._create_controls_layout()
        main_layout.addLayout(controls_layout)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.save_and_close)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

    def _create_user_list_panel(self):
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 10, 0)

        header_label = QLabel("User Accounts")
        header_label.setStyleSheet(f"""
            QLabel {{
                font-size: 16px;
                font-weight: bold;
                color: {ModernStyle.TEXT_PRIMARY};
                padding: 10px 0;
                border-bottom: 2px solid {ModernStyle.PRIMARY};
            }}
        """)
        panel_layout.addWidget(header_label)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setStyleSheet(f"""
            QScrollArea {{
                border: 1px solid {ModernStyle.BORDER};
                border-radius: 8px;
                background-color: {ModernStyle.SURFACE};
            }}
        """)

        self.user_list_widget = QWidget()
        self.user_list_layout = QVBoxLayout(self.user_list_widget)
        self.user_list_layout.setSpacing(8)
        self.user_list_layout.setContentsMargins(15, 15, 15, 15)

        scroll_area.setWidget(self.user_list_widget)
        panel_layout.addWidget(scroll_area)

        return panel

    def _create_user_form_panel(self):
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 0, 0, 0)

        self.form_header = QLabel("Add New User")
        self.form_header.setStyleSheet(f"""
            QLabel {{
                font-size: 16px;
                font-weight: bold;
                color: {ModernStyle.TEXT_PRIMARY};
                padding: 10px 0;
                border-bottom: 2px solid {ModernStyle.SECONDARY};
            }}
        """)
        panel_layout.addWidget(self.form_header)

        form_container = QWidget()
        form_container.setStyleSheet(f"""
            QWidget {{
                background-color: {ModernStyle.BACKGROUND};
                border: 1px solid {ModernStyle.SURFACE};
                border-radius: 8px;
                padding: 15px;
            }}
        """)
        form_layout = QVBoxLayout(form_container)
        form_layout.setSpacing(12)

        self.user_id_input = QLineEdit()
        self.user_id_input.setPlaceholderText("Enter user ID (e.g., 123456789)")
        self.user_id_input.setStyleSheet(self._get_input_style())
        form_layout.addWidget(QLabel("User ID:"))
        form_layout.addWidget(self.user_id_input)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Enter username (e.g., PlayerName)")
        self.username_input.setStyleSheet(self._get_input_style())
        form_layout.addWidget(QLabel("Username:"))
        form_layout.addWidget(self.username_input)

        self.private_server_input = QLineEdit()
        self.private_server_input.setPlaceholderText("Enter private server link (recommended)")
        self.private_server_input.setStyleSheet(self._get_input_style())
        form_layout.addWidget(QLabel("Private Server Link:"))
        form_layout.addWidget(self.private_server_input)

        self.place_input = QLineEdit()
        self.place_input.setPlaceholderText("Enter place ID")
        self.place_input.setStyleSheet(self._get_input_style())
        form_layout.addWidget(QLabel("Place:"))
        form_layout.addWidget(self.place_input)

        form_layout.addWidget(QLabel("Cookie:"))
        cookie_layout = QHBoxLayout()
        self.cookie_input = QLineEdit()
        self.cookie_input.setPlaceholderText("Enter .ROBLOSECURITY cookie")
        self.cookie_input.setStyleSheet(self._get_input_style())
        cookie_layout.addWidget(self.cookie_input)

        self.browser_login_btn = QPushButton("Login with Browser")
        self.browser_login_btn.setStyleSheet(self._get_secondary_button_style())
        self.browser_login_btn.setToolTip("Open browser to login and automatically extract cookie")
        self.browser_login_btn.clicked.connect(self.extract_cookie_from_browser)
        cookie_layout.addWidget(self.browser_login_btn)
        form_layout.addLayout(cookie_layout)

        button_layout = QHBoxLayout()
        self.add_btn = QPushButton("Add User")
        self.add_btn.setStyleSheet(self._get_primary_button_style())
        self.add_btn.clicked.connect(self.add_user)
        button_layout.addWidget(self.add_btn)

        self.update_btn = QPushButton("Update User")
        self.update_btn.setStyleSheet(self._get_primary_button_style())
        self.update_btn.clicked.connect(self.update_user)
        self.update_btn.hide()  
        button_layout.addWidget(self.update_btn)

        self.cancel_edit_btn = QPushButton("Cancel Edit")
        self.cancel_edit_btn.setStyleSheet(self._get_secondary_button_style())
        self.cancel_edit_btn.clicked.connect(self.cancel_edit)
        self.cancel_edit_btn.hide()  
        button_layout.addWidget(self.cancel_edit_btn)

        form_layout.addLayout(button_layout)
        panel_layout.addWidget(form_container)
        panel_layout.addStretch()

        return panel

    def _create_controls_layout(self):
        controls_layout = QHBoxLayout()

        refresh_btn = QPushButton("Refresh List")
        refresh_btn.setStyleSheet(self._get_secondary_button_style())
        refresh_btn.clicked.connect(self.refresh_user_list)
        controls_layout.addWidget(refresh_btn)

        controls_layout.addStretch()

        return controls_layout

    def _get_input_style(self):
        return f"""
            QLineEdit {{
                background-color: {ModernStyle.SURFACE};
                border: 2px solid {ModernStyle.BORDER};
                border-radius: 6px;
                padding: 10px 12px;
                color: {ModernStyle.TEXT_PRIMARY};
                font-size: 13px;
                min-height: 20px;
            }}
            QLineEdit:focus {{
                border-color: {ModernStyle.PRIMARY};
            }}
        """

    def _get_primary_button_style(self):
        return f"""
            QPushButton {{
                background-color: {ModernStyle.PRIMARY};
                color: {ModernStyle.TEXT_PRIMARY};
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 13px;
                min-height: 20px;
                min-width: 100px;
            }}
            QPushButton:hover {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}
            QPushButton:pressed {{
                background-color: 
            }}
        """

    def _get_secondary_button_style(self):
        return f"""
            QPushButton {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_PRIMARY};
                border: 1px solid {ModernStyle.BORDER};
                padding: 10px 20px;
                border-radius: 6px;
                font-weight: 500;
                font-size: 13px;
                min-height: 20px;
                min-width: 100px;
            }}
            QPushButton:hover {{
                background-color: {ModernStyle.BORDER};
            }}
        """

    def _get_action_button_style(self, color_type="primary"):
        if color_type == "danger":
            bg_color = ModernStyle.ERROR
            hover_color = "#dc2626"
            pressed_color = "#b91c1c"
        else:
            bg_color = ModernStyle.PRIMARY
            hover_color = ModernStyle.PRIMARY_VARIANT
            pressed_color = "#3730a3"

        return f"""
            QPushButton {{
                background-color: {bg_color};
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 12px;
                min-width: 70px;
                max-width: 80px;
                min-height: 30px;
                max-height: 32px;
            }}
            QPushButton:hover {{
                background-color: {hover_color};
            }}
            QPushButton:pressed {{
                background-color: {pressed_color};
            }}
        """

    def load_users(self):
        try:
            self.original_config = self.config_manager.load_users()
            self.refresh_user_list()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load users: {e}")
            self.original_config = {}
            self.refresh_user_list()

    def refresh_user_list(self):

        for i in reversed(range(self.user_list_layout.count())):
            child = self.user_list_layout.itemAt(i).widget()
            if child:
                child.setParent(None)

        for user_id, user_info in self.original_config.items():
            user_card = self._create_user_card(user_id, user_info)
            self.user_list_layout.addWidget(user_card)

        self.user_list_layout.addStretch()

    def _create_user_card(self, user_id, user_info):
        card = QWidget()
        card.setStyleSheet(f"""
            QWidget {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                border: 1px solid {ModernStyle.BORDER};
                border-radius: 8px;
                padding: 12px;
                margin: 2px;
            }}
            QWidget:hover {{
                border-color: {ModernStyle.PRIMARY};
                background-color: {ModernStyle.SURFACE};
            }}
        """)

        card.setMinimumHeight(100)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout(card)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        if isinstance(user_info, dict):
            username = user_info.get("username", f"User_{user_id}")
            private_server_link = user_info.get("private_server_link", "")
            place = user_info.get("place", "")
            cookie = user_info.get("cookie", "")
        else:
            username = f"User_{user_id}"
            private_server_link = ""
            place = ""
            cookie = user_info

        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)

        user_id_label = QLabel(f"ID: {user_id}")
        user_id_label.setStyleSheet(f"""
            QLabel {{
                font-weight: bold;
                font-size: 14px;
                color: {ModernStyle.PRIMARY};
                margin: 0px;
                padding: 0px;
            }}
        """)
        user_id_label.setWordWrap(True)
        header_layout.addWidget(user_id_label)

        username_label = QLabel(f"User: {username}")
        username_label.setStyleSheet(f"""
            QLabel {{
                font-size: 12px;
                color: {ModernStyle.TEXT_PRIMARY};
                font-weight: 500;
                margin: 0px;
                padding: 0px;
            }}
        """)
        username_label.setWordWrap(True)
        header_layout.addWidget(username_label)

        layout.addLayout(header_layout)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        if place:
            place_label = QLabel(f"Place: {place}")
            place_label.setStyleSheet(f"""
                QLabel {{
                    color: {ModernStyle.TEXT_SECONDARY};
                    font-size: 11px;
                    margin: 0px;
                    padding: 0px;
                }}
            """)
            place_label.setWordWrap(True)
            info_layout.addWidget(place_label)

        if private_server_link:

            if "roblox.com/share" in private_server_link:
                server_text = "Share Link: " + private_server_link.split("?")[1][:25] + "..."
            elif len(private_server_link) > 35:
                server_text = "Server: " + private_server_link[:35] + "..."
            else:
                server_text = f"Server: {private_server_link}"

            server_label = QLabel(server_text)
            server_label.setStyleSheet(f"""
                QLabel {{
                    color: {ModernStyle.TEXT_SECONDARY};
                    font-size: 11px;
                    margin: 0px;
                    padding: 0px;
                }}
            """)
            server_label.setWordWrap(True)
            server_label.setToolTip(private_server_link)  
            info_layout.addWidget(server_label)

        layout.addLayout(info_layout)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(6)
        button_layout.setContentsMargins(0, 4, 0, 0)

        edit_btn = QPushButton("Edit")
        edit_btn.setStyleSheet(self._get_action_button_style("primary"))
        edit_btn.clicked.connect(lambda: self.edit_user_card(user_id))
        button_layout.addWidget(edit_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet(self._get_action_button_style("danger"))
        delete_btn.clicked.connect(lambda: self.delete_user_by_id(user_id))
        button_layout.addWidget(delete_btn)

        button_layout.addStretch()
        layout.addLayout(button_layout)

        return card

    def edit_user_card(self, user_id):
        if user_id not in self.original_config:
            QMessageBox.warning(self, "Error", f"User {user_id} not found!")
            return

        self.selected_user_id = user_id
        self.form_header.setText(f"Edit User {user_id}")

        user_info = self.original_config[user_id]
        if isinstance(user_info, dict):
            self.user_id_input.setText(user_id)
            self.user_id_input.setEnabled(False)  
            self.username_input.setText(user_info.get("username", f"User_{user_id}"))
            self.private_server_input.setText(user_info.get("private_server_link", ""))
            self.place_input.setText(user_info.get("place", ""))
            self.cookie_input.setText(user_info.get("cookie", ""))
        else:
            self.user_id_input.setText(user_id)
            self.user_id_input.setEnabled(False)
            self.username_input.setText(f"User_{user_id}")
            self.private_server_input.setText("")
            self.place_input.setText("")
            self.cookie_input.setText(user_info)

        self.add_btn.hide()
        self.update_btn.show()
        self.cancel_edit_btn.show()

    def cancel_edit(self):
        self.selected_user_id = None
        self.form_header.setText("Add New User")

        self.user_id_input.clear()
        self.user_id_input.setEnabled(True)
        self.username_input.clear()
        self.private_server_input.clear()
        self.place_input.clear()
        self.cookie_input.clear()

        self.add_btn.show()
        self.update_btn.hide()
        self.cancel_edit_btn.hide()

    def update_user(self):
        if not self.selected_user_id:
            return

        user_id = self.selected_user_id
        username = self.username_input.text().strip()
        private_server_link = self.private_server_input.text().strip()
        place = self.place_input.text().strip()
        cookie = self.cookie_input.text().strip()

        cookie_raw = cookie
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
            if cookie and cookie != cookie_raw:
                try:
                    self.cookie_input.setText(cookie)
                except Exception:
                    pass

        if not username:
            username = f"User_{user_id}"

        if not private_server_link:
            if not self._confirm_missing_ps_link():
                self.private_server_input.setFocus()
                return


        if not cookie:
            QMessageBox.warning(self, "Error", "Cookie cannot be empty!")
            self.cookie_input.setFocus()
            return

        self.original_config[user_id] = {
            "username": username,
            "private_server_link": private_server_link,
            "place": place,
            "cookie": cookie,
            "bad": False,
            "cap": False,
        }

        self.refresh_user_list()
        self.cancel_edit()
        QMessageBox.information(self, "Success", f"User {user_id} ({username}) updated successfully!")

    def _confirm_missing_ps_link(self) -> bool:
        """Return True to proceed with save, False to cancel."""
        if self.skip_private_server_warning:
            return True

        box = QMessageBox(self)
        box.setWindowTitle("No Private Server Link")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            "You didn’t enter a Private Server Link.\n\n"
            "If you continue, the account will launch into a public server "
            "using ‘Place:’ (or Sols RNG public lobby if that's missing as well)."
        )
        box.setInformativeText("Save anyway?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)

        chk = QCheckBox("Don’t warn me again")
        box.setCheckBox(chk)

        decision = box.exec() == QMessageBox.StandardButton.Yes
        if decision and chk.isChecked():
            self.skip_private_server_warning = True   # remember only for this run
        return decision

    def add_user(self):
        user_id = self.user_id_input.text().strip()
        username = self.username_input.text().strip()
        private_server_link = self.private_server_input.text().strip()
        place = self.place_input.text().strip()
        cookie = self.cookie_input.text().strip()

        if not user_id:
            QMessageBox.warning(self, "Error", "Please enter a User ID")
            self.user_id_input.setFocus()
            return

        if not user_id.isdigit():
            QMessageBox.warning(self, "Error", "User ID should be numeric (e.g., 123456789)")
            self.user_id_input.setFocus()
            return

        if user_id in self.original_config:
            QMessageBox.warning(self, "Error", f"User ID {user_id} already exists. Use Edit to modify existing users.")
            self.user_id_input.setFocus()
            return

        if not private_server_link:
            if not self._confirm_missing_ps_link():
                self.private_server_input.setFocus()
                return

        if not cookie:
            QMessageBox.warning(self, "Error", "Please enter a Cookie")
            self.cookie_input.setFocus()
            return

        cookie_raw = cookie
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
            if cookie and cookie != cookie_raw:
                try:
                    self.cookie_input.setText(cookie)
                except Exception:
                    pass

        if not username:
            username = f"User_{user_id}"

        import re
        pattern1 = r'roblox\.com/games/\d+/[^?]*\?privateServerLinkCode=[A-Za-z0-9_-]+'
        pattern2 = r'roblox\.com/share\?code=[A-Za-z0-9_-]+&type=Server'

        if not (re.search(pattern1, private_server_link) or re.search(pattern2, private_server_link)):
            reply = QMessageBox.question(self, "Private Server Link Warning",
                                       "The private server link doesn't appear to be in the expected format.\n\n"
                                       "Supported formats:\n"
                                       "• Direct Link: https://www.roblox.com/games/[ID]/[NAME]?privateServerLinkCode=[CODE]\n"
                                       "• Share Link: https://www.roblox.com/share?code=[CODE]&type=Server\n\n"
                                       "Continue anyway?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                self.private_server_input.setFocus()
                return

        should_warn_cookie = False
        try:
            if is_probably_roblosecurity is not None:
                should_warn_cookie = not bool(is_probably_roblosecurity(cookie))
            else:
                should_warn_cookie = not bool(str(cookie or "").strip())
        except Exception:
            should_warn_cookie = True

        if should_warn_cookie:
            reply = QMessageBox.question(self, "Cookie Warning",
                                       "The cookie doesn't appear to be a valid .ROBLOSECURITY value. Continue anyway?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                self.cookie_input.setFocus()
                return

        try:

            self.original_config[user_id] = {
                "username": username,
                "private_server_link": private_server_link,
                "place": place,
                "cookie": cookie,
                "bad": False,
                "cap": False,
            }

            self.user_id_input.clear()
            self.username_input.clear()
            self.private_server_input.clear()
            self.place_input.clear()
            self.cookie_input.clear()

            self.refresh_user_list()

            QMessageBox.information(self, "Success", f"User {user_id} ({username}) added successfully!")

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add user: {e}")

    def extract_cookie_from_browser(self):
        try:
            self.browser_login_btn.setEnabled(False)
            self.browser_login_btn.setText("Extracting...")

            self.cookie_extractor.extract_cookie_async(
                callback=self._on_cookie_extraction_complete,
                parent_widget=self
            )

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start cookie extraction: {str(e)}")
            self._reset_browser_button()

    def _fetch_authenticated_user(self, cookie: str) -> Tuple[Optional[dict], str]:
        cookie = str(cookie or "").strip()
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
        if not cookie:
            return None, cookie
        try:
            session = requests.Session()
            try:
                session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
            except Exception:
                session.cookies.set(".ROBLOSECURITY", cookie)

            response = session.get("https://users.roblox.com/v1/users/authenticated", timeout=8)
            if extract_roblosecurity_from_requests_response is not None:
                try:
                    updated = extract_roblosecurity_from_requests_response(response, session=session)
                except Exception:
                    updated = None
                if updated and updated != cookie:
                    cookie = updated

            if response.status_code != 200:
                return None, cookie
            data = response.json()
            if not isinstance(data, dict):
                return None, cookie
            if "id" not in data or "name" not in data:
                return None, cookie
            return data, cookie
        except Exception:
            return None, cookie

    def _on_cookie_extraction_complete(self, cookie: str):
        try:
            if cookie:
                self.cookie_input.setText(cookie)
                user_info, updated_cookie = self._fetch_authenticated_user(cookie)
                if updated_cookie and updated_cookie != cookie:
                    cookie = updated_cookie
                    self.cookie_input.setText(cookie)
                extra = ""
                if user_info:
                    self.user_id_input.setText(str(user_info.get("id", "")))
                    self.username_input.setText(str(user_info.get("name", "")))
                    extra = f"\n\nUser info filled: {user_info.get('name', '')} ({user_info.get('id', '')})"
                else:
                    extra = "\n\nCould not fetch user info; you can enter it manually."
                QMessageBox.information(self, "Success",
                                      "Cookie extracted successfully!\n\n"
                                      "The cookie has been automatically filled in the input field."
                                      f"{extra}")
            else:
                QMessageBox.information(self, "Extraction Cancelled",
                                      "Cookie extraction was cancelled or failed.\n\n"
                                      "You can try again or enter the cookie manually.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error handling extracted cookie: {str(e)}")
        finally:
            self._reset_browser_button()

    def _reset_browser_button(self):
        self.browser_login_btn.setEnabled(True)
        self.browser_login_btn.setText("Login with Browser")

    def delete_user_by_id(self, user_id):
        user_info = self.original_config.get(user_id, {})
        if isinstance(user_info, dict):
            username = user_info.get("username", f"User_{user_id}")
        else:
            username = f"User_{user_id}"

        reply = QMessageBox.question(self, "Confirm Delete",
                                   f"Are you sure you want to delete user {user_id} ({username})?\n\n"
                                   f"This action cannot be undone.",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            if user_id in self.original_config:

                if self.selected_user_id == user_id:
                    self.cancel_edit()

                del self.original_config[user_id]
                self.refresh_user_list()
                QMessageBox.information(self, "Success", f"User {user_id} ({username}) deleted successfully!")
            else:
                QMessageBox.warning(self, "Error", f"User {user_id} not found in configuration!")

    def save_and_close(self):
        if self.config_manager.save_users(self.original_config):
            config_info = self.config_manager.get_config_info()
            QMessageBox.information(self, "Success",
                                  f"User configuration saved successfully!\n\n"
                                  f"Location: {config_info['users_file']}\n"
                                  f"Backup created in: {config_info['backup_dir']}")
            self.accept()
        else:
            QMessageBox.critical(self, "Error",
                               "Failed to save user configuration. Please check the logs for details.")


class UserManagementDialog(QDialog):
    """
    Launcher-focused Manage Users dialog.

    - Multi-select users and launch Roblox (PS/share link, else public lobby).
    - Open a logged-in browser by injecting the user's .ROBLOSECURITY cookie.
    - Basic add/edit/delete still available.
    """

    # Keep Selenium drivers alive so Chrome windows don't auto-close.
    _browser_driver_keepalive: list = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Users")
        self.setModal(True)

        self.resize(1400, 850)
        self.setMinimumSize(1200, 700)

        self.config_manager = ConfigManager()
        self.cookie_extractor = CookieExtractor(self)

        self.original_config: dict[str, dict] = {}
        self.selected_user_id: Optional[str] = None

        # For manager-off launches
        self._manual_manager: Optional[RobloxManager] = None
        self._manual_process_mgr: Optional[ProcessManager] = None
        self._manual_launcher: Optional[GameLauncher] = None

        self._build_ui()
        self.load_users()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(10, 10, 10, 10)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by user ID / username...")
        self.search_input.textChanged.connect(self.refresh_user_list)
        search_row.addWidget(self.search_input, 1)

        self.selected_count_label = QLabel("Selected: 0")
        self.selected_count_label.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        search_row.addWidget(self.selected_count_label)

        refresh_btn = QPushButton("Reload")
        refresh_btn.clicked.connect(self.load_users)
        search_row.addWidget(refresh_btn)
        main_layout.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Left: table
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)

        self.users_table = QTableWidget()
        self.users_table.setColumnCount(5)
        self.users_table.setHorizontalHeaderLabels([
            "User ID",
            "Username",
            "Mode",
            "Link / Place",
            "Description",
        ])
        try:
            self.users_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.users_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        except Exception:
            pass
        self.users_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.users_table.verticalHeader().setVisible(False)
        self.users_table.setAlternatingRowColors(True)
        self.users_table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.users_table.cellDoubleClicked.connect(self._on_table_double_clicked)
        self.users_table.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {ModernStyle.SURFACE};
                alternate-background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_PRIMARY};
                gridline-color: {ModernStyle.BORDER};
                border: 1px solid {ModernStyle.BORDER};
            }}
            QTableWidget::item {{
                padding: 6px;
            }}
            QHeaderView::section {{
                background-color: {ModernStyle.BACKGROUND};
                color: {ModernStyle.TEXT_PRIMARY};
                border: 1px solid {ModernStyle.BORDER};
                padding: 6px;
            }}
            QTableWidget::item:selected {{
                background-color: {ModernStyle.PRIMARY};
                color: {ModernStyle.TEXT_PRIMARY};
            }}
            """
        )

        header_obj = self.users_table.horizontalHeader()
        header_obj.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_obj.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_obj.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_obj.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header_obj.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

        left_layout.addWidget(self.users_table)
        splitter.addWidget(left)

        # Right: tabs (Launcher + Edit)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)

        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(self._build_launch_tab(), "Launcher")
        self.right_tabs.addTab(self._build_edit_tab(), "Edit")
        right_layout.addWidget(self.right_tabs)
        splitter.addWidget(right)

        splitter.setSizes([900, 500])
        main_layout.addWidget(splitter)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save And Close")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setText("Cancel")
        cancel_btn.setProperty("class", "danger")
        buttons.accepted.connect(self.save_and_close)
        buttons.rejected.connect(self._confirm_close_without_saving)
        main_layout.addWidget(buttons)

        self._set_edit_mode(None)
        self._update_launch_actions()

    def _build_launch_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        self.launch_summary = QLabel("Select one or more users from the table.")
        self.launch_summary.setWordWrap(True)
        self.launch_summary.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(self.launch_summary)

        target_group = QGroupBox("Target")
        target_form = QFormLayout(target_group)

        self.launch_server_link_input = QLineEdit()
        self.launch_server_link_input.setPlaceholderText("Private server / share link (leave empty to use Place ID)")
        try:
            self.launch_server_link_input.textChanged.connect(self._update_launch_actions)
        except Exception:
            pass
        target_form.addRow("Server Link:", self.launch_server_link_input)

        self.launch_place_id_input = QLineEdit()
        self.launch_place_id_input.setPlaceholderText("Place ID (used when Server Link is empty)")
        try:
            self.launch_place_id_input.setText(self._default_place_id())
        except Exception:
            pass
        try:
            self.launch_place_id_input.textChanged.connect(self._update_launch_actions)
        except Exception:
            pass
        target_form.addRow("Place ID:", self.launch_place_id_input)

        self.launch_roblox_btn = QPushButton("Launch Roblox")
        self.launch_roblox_btn.clicked.connect(self.launch_roblox_for_selected)
        target_form.addRow(self.launch_roblox_btn)

        hint = QLabel("If Server Link is empty, Place ID is used (public lobby).")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        target_form.addRow(hint)

        layout.addWidget(target_group)

        description_group = QGroupBox("Description")
        dg = QVBoxLayout(description_group)

        self.launch_description_input = QTextEdit()
        self.launch_description_input.setPlaceholderText("Optional description (saved per user)")
        try:
            self.launch_description_input.setAcceptRichText(False)
        except Exception:
            pass
        try:
            fm = self.launch_description_input.fontMetrics()
            row_h = fm.lineSpacing() if fm else 16
            self.launch_description_input.setFixedHeight(int(row_h * 5 + 12))
        except Exception:
            pass
        dg.addWidget(self.launch_description_input)

        desc_btn_row = QHBoxLayout()
        desc_btn_row.addStretch(1)
        self.apply_description_btn = QPushButton("Apply")
        self.apply_description_btn.setToolTip("Apply this description to the selected user(s)")
        self.apply_description_btn.clicked.connect(self._apply_description_to_selected)
        desc_btn_row.addWidget(self.apply_description_btn)
        dg.addLayout(desc_btn_row)

        layout.addWidget(description_group)

        browser_group = QGroupBox("Browser (cookie-loaded)")
        bg = QVBoxLayout(browser_group)

        top_row = QHBoxLayout()
        self.open_browser_home_btn = QPushButton("Open Roblox Home")
        self.open_browser_home_btn.clicked.connect(lambda: self.open_browser_for_selected("home"))
        top_row.addWidget(self.open_browser_home_btn)
        top_row.addStretch(1)
        bg.addLayout(top_row)

        link_row = QHBoxLayout()
        self.browser_any_link_input = QLineEdit()
        self.browser_any_link_input.setPlaceholderText("Any link to open (optional)")
        try:
            self.browser_any_link_input.textChanged.connect(self._update_launch_actions)
        except Exception:
            pass
        link_row.addWidget(self.browser_any_link_input, 1)

        self.open_browser_link_btn = QPushButton("Open Link")
        self.open_browser_link_btn.clicked.connect(lambda: self.open_browser_for_selected("link"))
        link_row.addWidget(self.open_browser_link_btn)
        bg.addLayout(link_row)

        layout.addWidget(browser_group)

        layout.addStretch(1)
        return w

    def _build_edit_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self.edit_hint = QLabel("Select a single user to edit, or clear selection to add a new user.")
        self.edit_hint.setWordWrap(True)
        self.edit_hint.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(self.edit_hint)

        self.edit_group = QGroupBox("User Details")
        form = QFormLayout(self.edit_group)

        self.user_id_input = QLineEdit()
        self.user_id_input.setPlaceholderText("User ID (e.g., 123456789)")
        form.addRow("User ID:", self.user_id_input)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username (e.g., PlayerName)")
        form.addRow("Username:", self.username_input)

        self.private_server_input = QLineEdit()
        self.private_server_input.setPlaceholderText("Private server link or share link (optional)")
        form.addRow("Server Link:", self.private_server_input)

        self.place_input = QLineEdit()
        self.place_input.setPlaceholderText("Place ID (optional)")
        form.addRow("Place:", self.place_input)

        cookie_row = QWidget()
        cookie_row_layout = QHBoxLayout(cookie_row)
        cookie_row_layout.setContentsMargins(0, 0, 0, 0)

        self.cookie_input = QLineEdit()
        self.cookie_input.setPlaceholderText(".ROBLOSECURITY cookie")
        cookie_row_layout.addWidget(self.cookie_input)

        self.browser_login_btn = QPushButton("Login (Extract Cookie)")
        self.browser_login_btn.setToolTip("Open browser to login and automatically extract cookie")
        self.browser_login_btn.clicked.connect(self.extract_cookie_from_browser)
        cookie_row_layout.addWidget(self.browser_login_btn)

        form.addRow("Cookie:", cookie_row)

        flags_row = QWidget()
        flags_layout = QVBoxLayout(flags_row)
        flags_layout.setContentsMargins(0, 0, 0, 0)
        flags_layout.setSpacing(4)

        flags_top = QHBoxLayout()
        flags_top.setContentsMargins(0, 0, 0, 0)
        self.disabled_chk = QCheckBox("Disabled")
        self.bad_chk = QCheckBox("Bad Cookie")
        self.cap_chk = QCheckBox("Captcha Lock")
        self.alternate_chk = QCheckBox("Alternate")
        self.alternate_chk.setToolTip(
            "Alternate launch mode (no cookies).\n"
            "Launches via roblox://placeId=<place> (public) or roblox://placeId=<place>&linkCode=<code> (private)."
        )
        self.skip_reconnect_on_log_disconnect_chk = QCheckBox("Disable log reconnects")
        self.skip_reconnect_on_log_disconnect_chk.setToolTip(
            "When enabled, this account will not auto-reconnect after log disconnects."
        )
        try:
            self.alternate_chk.toggled.connect(self._on_edit_alternate_toggled)
        except Exception:
            pass
        flags_top.addWidget(self.disabled_chk)
        flags_top.addWidget(self.bad_chk)
        flags_top.addWidget(self.cap_chk)
        flags_top.addWidget(self.alternate_chk)
        flags_top.addStretch(1)
        flags_layout.addLayout(flags_top)

        flags_bottom = QHBoxLayout()
        flags_bottom.setContentsMargins(0, 0, 0, 0)
        flags_bottom.addWidget(self.skip_reconnect_on_log_disconnect_chk)
        flags_bottom.addStretch(1)
        flags_layout.addLayout(flags_bottom)
        form.addRow("Flags:", flags_row)

        layout.addWidget(self.edit_group)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("Add User")
        self.add_btn.clicked.connect(self.add_user)
        btn_row.addWidget(self.add_btn)

        self.update_btn = QPushButton("Update User")
        self.update_btn.clicked.connect(self.update_user)
        btn_row.addWidget(self.update_btn)

        self.delete_btn = QPushButton("Delete User")
        self.delete_btn.setProperty("class", "danger")
        self.delete_btn.clicked.connect(self.delete_selected_user)
        btn_row.addWidget(self.delete_btn)

        self.cancel_edit_btn = QPushButton("Cancel")
        self.cancel_edit_btn.clicked.connect(self.cancel_edit)
        btn_row.addWidget(self.cancel_edit_btn)

        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        return w

    def load_users(self) -> None:
        try:
            self.original_config = self.config_manager.load_users() or {}
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load users: {e}")
            self.original_config = {}
        self.refresh_user_list()

    def refresh_user_list(self) -> None:
        query = self.search_input.text().strip().lower() if hasattr(self, "search_input") else ""

        rows: list[tuple[str, dict]] = []
        for uid, info in (self.original_config or {}).items():
            uid = str(uid)
            if not isinstance(info, dict):
                continue
            username = str(info.get("username", ""))
            if query and (query not in uid.lower()) and (query not in username.lower()):
                continue
            rows.append((uid, info))

        def _sort_key(item):
            uid0 = item[0]
            if uid0.isdigit():
                try:
                    return (0, int(uid0))
                except Exception:
                    return (0, uid0)
            return (1, uid0)

        rows.sort(key=_sort_key)

        self.users_table.setRowCount(len(rows))
        for r, (uid, info) in enumerate(rows):
            username = str(info.get("username", f"User_{uid}"))
            psl = str(info.get("private_server_link", "") or "").strip()
            place = str(info.get("place", "") or "").strip()
            description = str(info.get("description", "") or "").strip()

            if bool(info.get("alternate_launch", False)):
                mode = "Alternate"
                link_preview = psl if psl else (place or "Default")
            elif psl:
                mode = "Share" if "roblox.com/share" in psl else "Private"
                link_preview = psl
            else:
                mode = "Public"
                link_preview = place or "Default"

            if "roblox.com/share" in link_preview and "code=" in link_preview:
                try:
                    code = link_preview.split("code=", 1)[1].split("&", 1)[0]
                    link_preview = f"Share:{code}"
                except Exception:
                    pass
            if len(link_preview) > 64:
                link_preview = link_preview[:61] + "..."
            desc_preview = description
            if len(desc_preview) > 64:
                desc_preview = desc_preview[:61] + "..."

            uid_item = QTableWidgetItem(uid)
            uid_item.setData(Qt.ItemDataRole.UserRole, uid)
            self.users_table.setItem(r, 0, uid_item)
            self.users_table.setItem(r, 1, QTableWidgetItem(username))
            self.users_table.setItem(r, 2, QTableWidgetItem(mode))

            link_item = QTableWidgetItem(link_preview)
            if psl:
                link_item.setToolTip(psl)
            self.users_table.setItem(r, 3, link_item)
            desc_item = QTableWidgetItem(desc_preview)
            if description:
                desc_item.setToolTip(description)
            self.users_table.setItem(r, 4, desc_item)

        try:
            self.users_table.resizeRowsToContents()
        except Exception:
            pass

        self._update_launch_actions()
        self._sync_edit_with_selection()

    def save_and_close(self) -> None:
        if self.config_manager.save_users(self.original_config):
            self.accept()
        else:
            err = self.config_manager.get_cookie_error()
            msg = "Failed to save users.json."
            if err:
                msg = msg + "\n\n" + err
            QMessageBox.critical(self, "Error", msg)

    def _confirm_close_without_saving(self) -> None:
        reply = QMessageBox.warning(
            self,
            "Discard Changes?",
            "Changes made will not save.\n\nAre you sure you want to close?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.reject()

    def open_browser_for_selected(self, mode: str) -> None:
        uids = self._get_selected_user_ids()
        if not uids:
            return

        mode = str(mode or "").strip().lower()
        if mode not in ("home", "link"):
            mode = "home"

        any_link = ""
        try:
            any_link = self.browser_any_link_input.text().strip()
        except Exception:
            any_link = ""

        if mode == "link" and any_link:
            try:
                if any_link.startswith("//"):
                    any_link = "https:" + any_link
                elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", any_link):
                    any_link = "https://" + any_link
                try:
                    self.browser_any_link_input.setText(any_link)
                except Exception:
                    pass
            except Exception:
                pass

        if mode == "link" and not any_link:
            QMessageBox.information(self, "No Link", "Enter a link to open first.")
            return

        missing = []
        skip_alternate = []
        for uid in uids:
            info = self.original_config.get(uid, {}) if isinstance(self.original_config, dict) else {}
            if not isinstance(info, dict):
                continue
            if bool(info.get("alternate_launch", False)):
                skip_alternate.append(uid)
                continue
            cookie = str(info.get("cookie", "") or "").strip()
            if not cookie:
                missing.append(uid)
                continue

            url = "https://www.roblox.com/home" if mode == "home" else any_link

            try:
                self._open_logged_in_browser(uid, cookie, url)
            except Exception as e:
                self._ui_log(f"[Browser] uid={uid} failed: {e}")

        if skip_alternate or missing:
            parts = []
            if skip_alternate:
                parts.append(
                    f"Skipped {len(skip_alternate)} alternate-launch user(s) (does not use cookies):\n"
                    + ", ".join(skip_alternate[:12])
                    + (" ..." if len(skip_alternate) > 12 else "")
                )
            if missing:
                parts.append(
                    f"Skipped {len(missing)} user(s) with no cookie:\n"
                    + ", ".join(missing[:12])
                    + (" ..." if len(missing) > 12 else "")
                )
            QMessageBox.information(
                self,
                "Skipped Users",
                "\n\n".join(parts).strip(),
            )

    def launch_roblox_for_selected(self) -> None:
        uids = self._get_selected_user_ids()
        if not uids:
            return

        try:
            server_link = self.launch_server_link_input.text().strip()
        except Exception:
            server_link = ""

        try:
            place_id = self.launch_place_id_input.text().strip()
        except Exception:
            place_id = ""

        if not place_id:
            place_id = self._default_place_id()

        skip_disabled = []
        skip_flagged = []
        skip_no_cookie = []
        launch_list: list[str] = []

        for uid in uids:
            info = self.original_config.get(uid, {}) if isinstance(self.original_config, dict) else {}
            if not isinstance(info, dict):
                continue
            if bool(info.get("disabled", False)):
                skip_disabled.append(uid)
                continue
            if bool(info.get("bad", False)) or bool(info.get("cap", False)):
                skip_flagged.append(uid)
                continue
            is_alt = bool(info.get("alternate_launch", False))
            cookie = str(info.get("cookie", "") or "").strip()
            if (not cookie) and (not is_alt):
                skip_no_cookie.append(uid)
                continue
            launch_list.append(uid)

        if not launch_list:
            QMessageBox.information(self, "Nothing To Launch", "No selected users are launchable (disabled/flagged/missing cookie for cookie-mode users).")
            return

        if len(launch_list) > 1:
            reply = QMessageBox.question(
                self,
                "Confirm Launch",
                f"Launch Roblox for {len(launch_list)} users?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        if self._manager_running():
            parent = self.parent()
            wt = getattr(parent, "worker_thread", None)
            online_skipped = 0
            for uid in launch_list:
                if self._is_uid_online_in_manager(uid):
                    online_skipped += 1
                    continue
                try:
                    info0 = self.original_config.get(uid, {}) if isinstance(self.original_config, dict) else {}
                    launch_info = dict(info0) if isinstance(info0, dict) else {}
                    if server_link:
                        launch_info["private_server_link"] = server_link
                        launch_info["server_type"] = "private"
                        # Fallback place for share-link resolve failures, etc.
                        if place_id:
                            launch_info["place"] = place_id
                    else:
                        launch_info["private_server_link"] = ""
                        launch_info["server_type"] = "public"
                        launch_info["place"] = place_id
                    wt.launch_user_session_custom(uid, launch_info, skip_cleanup=False)
                except Exception as e:
                    self._ui_log(f"[ManualLaunch] uid={uid} failed: {e}")
            if online_skipped:
                self._ui_log(f"[ManualLaunch] Skipped {online_skipped} already-online user(s).")
            return

        # Manager is off: manual launching on a background thread.
        try:
            self.launch_roblox_btn.setEnabled(False)
        except Exception:
            pass

        def _run():
            try:
                launcher = self._get_manual_launcher()
                for uid in launch_list:
                    info0 = self.original_config.get(uid, {}) if isinstance(self.original_config, dict) else {}
                    launch_info = dict(info0) if isinstance(info0, dict) else {}
                    if server_link:
                        launch_info["private_server_link"] = server_link
                        launch_info["server_type"] = "private"
                        if place_id:
                            launch_info["place"] = place_id
                    else:
                        launch_info["private_server_link"] = ""
                        launch_info["server_type"] = "public"
                        launch_info["place"] = place_id

                    cookie = str((launch_info or {}).get("cookie", "") or "").strip()
                    self._ui_log(f"[ManualLaunch] launching uid={uid}")
                    try:
                        launcher.start_game_session(uid, cookie, launch_info, skip_cleanup=False)
                    except Exception as e:
                        self._ui_log(f"[ManualLaunch] uid={uid} error: {e}")
                    time.sleep(0.8)
                self._ui_log(f"[ManualLaunch] done ({len(launch_list)} users)")
            finally:
                QTimer.singleShot(0, lambda: self.launch_roblox_btn.setEnabled(True))

        threading.Thread(target=_run, daemon=True).start()

        if skip_disabled or skip_flagged or skip_no_cookie:
            parts = []
            if skip_disabled:
                parts.append(f"disabled={len(skip_disabled)}")
            if skip_flagged:
                parts.append(f"flagged={len(skip_flagged)}")
            if skip_no_cookie:
                parts.append(f"no_cookie={len(skip_no_cookie)}")
            self._ui_log("[ManualLaunch] skipped: " + ", ".join(parts))

    # ---------------- Selection & editing ----------------
    def _get_selected_user_ids(self) -> list[str]:
        selected_rows = set()
        try:
            for idx in self.users_table.selectedIndexes():
                selected_rows.add(idx.row())
        except Exception:
            return []

        uids: list[str] = []
        for r in sorted(selected_rows):
            it = self.users_table.item(r, 0)
            if it is None:
                continue
            uid = it.data(Qt.ItemDataRole.UserRole) or it.text()
            if uid:
                uids.append(str(uid))
        return uids

    def _on_table_double_clicked(self, row: int, col: int) -> None:
        try:
            self.right_tabs.setCurrentIndex(1)
        except Exception:
            pass

    def _on_table_selection_changed(self) -> None:
        self._update_launch_actions()
        self._sync_launch_target_with_selection()
        self._sync_edit_with_selection()

    def _update_launch_actions(self) -> None:
        uids = self._get_selected_user_ids()
        if not uids:
            self.launch_summary.setText("Select one or more users from the table.")
        elif len(uids) == 1:
            self.launch_summary.setText(f"Selected: {uids[0]}")
        else:
            preview = ", ".join(uids[:4]) + (" ..." if len(uids) > 4 else "")
            self.launch_summary.setText(f"Selected: {len(uids)} users ({preview})")

        try:
            self.selected_count_label.setText(f"Selected: {len(uids)}")
        except Exception:
            pass

        has_selection = bool(uids)

        any_link = ""
        try:
            any_link = self.browser_any_link_input.text().strip()
        except Exception:
            any_link = ""

        cookie_capable = False
        try:
            for uid in uids:
                info = self.original_config.get(str(uid), {}) if isinstance(self.original_config, dict) else {}
                if not isinstance(info, dict):
                    continue
                if not bool(info.get("alternate_launch", False)):
                    cookie_capable = True
                    break
        except Exception:
            cookie_capable = False

        open_home_enabled = has_selection and cookie_capable
        open_link_enabled = has_selection and bool(any_link) and cookie_capable
        launch_enabled = has_selection

        try:
            self.open_browser_home_btn.setEnabled(open_home_enabled)
        except Exception:
            pass
        try:
            self.open_browser_link_btn.setEnabled(open_link_enabled)
        except Exception:
            pass
        try:
            self.launch_roblox_btn.setEnabled(launch_enabled)
        except Exception:
            pass
        try:
            self.apply_description_btn.setEnabled(has_selection)
        except Exception:
            pass

    def _sync_launch_target_with_selection(self) -> None:
        """
        When exactly one user is selected, prefill the launcher inputs from that user.
        """
        uids = self._get_selected_user_ids()
        if len(uids) != 1:
            return

        uid = str(uids[0])
        info = self.original_config.get(uid, {}) if isinstance(self.original_config, dict) else {}
        if not isinstance(info, dict):
            return

        link = str(info.get("private_server_link", "") or "").strip()
        place = str(info.get("place", "") or "").strip() or self._default_place_id()
        desc = str(info.get("description", "") or "").strip()

        try:
            self.launch_server_link_input.setText(link)
        except Exception:
            pass
        try:
            self.launch_place_id_input.setText(place)
        except Exception:
            pass
        try:
            self.launch_description_input.setPlainText(desc)
        except Exception:
            pass

    def _apply_description_to_selected(self) -> None:
        uids = self._get_selected_user_ids()
        if not uids:
            return

        try:
            desc = str(self.launch_description_input.toPlainText() or "").strip()
        except Exception:
            desc = ""

        uids_set = set(str(u) for u in uids)
        changed = 0
        for uid in uids_set:
            info = self.original_config.get(uid, {}) if isinstance(self.original_config, dict) else {}
            if not isinstance(info, dict):
                continue
            if str(info.get("description", "") or "") != desc:
                info["description"] = desc
                self.original_config[uid] = info
                changed += 1

        if changed <= 0:
            return

        # Update table cells in-place to preserve selection.
        try:
            preview = desc
            if len(preview) > 64:
                preview = preview[:61] + "..."

            for row in range(self.users_table.rowCount()):
                it = self.users_table.item(row, 0)
                if it is None:
                    continue
                row_uid = it.data(Qt.ItemDataRole.UserRole) or it.text()
                if str(row_uid) not in uids_set:
                    continue

                desc_item = self.users_table.item(row, 4)
                if desc_item is None:
                    desc_item = QTableWidgetItem()
                    self.users_table.setItem(row, 4, desc_item)
                desc_item.setText(preview)
                desc_item.setToolTip(desc if desc else "")
        except Exception:
            # Fallback: refresh if anything goes wrong.
            try:
                self.refresh_user_list()
            except Exception:
                pass

        self._ui_log(f"[UI] Updated description for {changed} user(s).")

    def _set_edit_mode(self, user_id: Optional[str]) -> None:
        self.selected_user_id = user_id
        is_edit = bool(user_id)
        try:
            self.user_id_input.setEnabled(not is_edit)
        except Exception:
            pass
        try:
            self.add_btn.setVisible(not is_edit)
            self.update_btn.setVisible(is_edit)
            self.delete_btn.setVisible(is_edit)
        except Exception:
            pass

    def _sync_edit_with_selection(self) -> None:
        uids = self._get_selected_user_ids()
        if len(uids) == 1:
            uid = uids[0]
            self._load_user_into_form(uid)
            self.edit_hint.setText(f"Editing user {uid}.")
            try:
                self.edit_group.setEnabled(True)
            except Exception:
                pass
            self._set_edit_mode(uid)
        elif len(uids) == 0:
            self.edit_hint.setText("Add a new user (or select a single user to edit).")
            try:
                self.edit_group.setEnabled(True)
            except Exception:
                pass
            self._clear_form()
            self._set_edit_mode(None)
        else:
            self.edit_hint.setText("Multiple users selected. Editing is disabled.")
            try:
                self.edit_group.setEnabled(False)
            except Exception:
                pass
            self._set_edit_mode(None)

    def _clear_form(self) -> None:
        try:
            self.user_id_input.clear()
            self.username_input.clear()
            self.private_server_input.clear()
            self.place_input.clear()
            self.cookie_input.clear()
            self.disabled_chk.setChecked(False)
            self.bad_chk.setChecked(False)
            if hasattr(self, "cap_chk"):
                self.cap_chk.setChecked(False)
            if hasattr(self, "alternate_chk"):
                self.alternate_chk.setChecked(False)
            if hasattr(self, "skip_reconnect_on_log_disconnect_chk"):
                self.skip_reconnect_on_log_disconnect_chk.setChecked(False)
        except Exception:
            pass
        try:
            self._on_edit_alternate_toggled(False)
        except Exception:
            pass

    def _load_user_into_form(self, user_id: str) -> None:
        info = (self.original_config or {}).get(str(user_id), {})
        if not isinstance(info, dict):
            info = {}
        try:
            self.user_id_input.setText(str(user_id))
            self.username_input.setText(str(info.get("username", f"User_{user_id}")))
            self.private_server_input.setText(str(info.get("private_server_link", "") or ""))
            self.place_input.setText(str(info.get("place", "") or ""))
            self.cookie_input.setText(str(info.get("cookie", "") or ""))
            self.disabled_chk.setChecked(bool(info.get("disabled", False)))
            self.bad_chk.setChecked(bool(info.get("bad", False)))
            if hasattr(self, "cap_chk"):
                self.cap_chk.setChecked(bool(info.get("cap", False)))
            if hasattr(self, "alternate_chk"):
                self.alternate_chk.setChecked(bool(info.get("alternate_launch", False)))
            if hasattr(self, "skip_reconnect_on_log_disconnect_chk"):
                self.skip_reconnect_on_log_disconnect_chk.setChecked(
                    bool(info.get("skip_reconnect_on_log_disconnect", False))
                )
        except Exception:
            pass
        try:
            self._on_edit_alternate_toggled(bool(getattr(self, "alternate_chk", None) and self.alternate_chk.isChecked()))
        except Exception:
            pass

    def _on_edit_alternate_toggled(self, checked: bool) -> None:
        checked = bool(checked)
        try:
            self.cookie_input.setEnabled(not checked)
            if checked:
                self.cookie_input.setToolTip("Disabled: Alternate launch mode does not use cookies.")
                self.cookie_input.setPlaceholderText("Not used in Alternate mode")
            else:
                self.cookie_input.setToolTip("")
                self.cookie_input.setPlaceholderText(".ROBLOSECURITY cookie")
        except Exception:
            pass
        try:
            btn = getattr(self, "browser_login_btn", None)
            if btn is not None:
                btn.setEnabled(not checked)
                btn.setToolTip(
                    "Disabled: Alternate launch mode does not use cookies."
                    if checked
                    else "Open browser to login and automatically extract cookie"
                )
        except Exception:
            pass

    def cancel_edit(self) -> None:
        try:
            self.users_table.clearSelection()
        except Exception:
            pass
        self._clear_form()
        self._set_edit_mode(None)
        self._update_launch_actions()

    def _confirm_missing_ps_link(self) -> bool:
        return (
            QMessageBox.question(
                self,
                "No Server Link",
                "No private server/share link is set.\n\nThis account will launch into the public lobby.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _derive_server_type(self, private_server_link: str, place: str) -> str:
        if str(private_server_link or "").strip():
            return "private"
        if str(place or "").strip():
            return "public"
        return "private"

    def add_user(self) -> None:
        user_id = self.user_id_input.text().strip()
        username = self.username_input.text().strip() or f"User_{user_id}"
        private_server_link = self.private_server_input.text().strip()
        place = self.place_input.text().strip()
        cookie = self.cookie_input.text().strip()
        disabled = bool(self.disabled_chk.isChecked())
        bad = bool(self.bad_chk.isChecked())
        cap = bool(getattr(self, "cap_chk", None) and self.cap_chk.isChecked())
        alternate = bool(getattr(self, "alternate_chk", None) and self.alternate_chk.isChecked())
        skip_reconnect_on_log_disconnect = bool(
            getattr(self, "skip_reconnect_on_log_disconnect_chk", None)
            and self.skip_reconnect_on_log_disconnect_chk.isChecked()
        )

        cookie_raw = cookie
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
            if cookie and cookie != cookie_raw:
                try:
                    self.cookie_input.setText(cookie)
                except Exception:
                    pass

        if not user_id:
            QMessageBox.warning(self, "Error", "User ID cannot be empty!")
            self.user_id_input.setFocus()
            return
        if user_id in (self.original_config or {}):
            QMessageBox.warning(self, "Error", f"User {user_id} already exists! Select it to edit.")
            return

        if not private_server_link:
            if not self._confirm_missing_ps_link():
                self.private_server_input.setFocus()
                return

        if (not alternate) and (not cookie):
            QMessageBox.warning(self, "Error", "Cookie cannot be empty!")
            self.cookie_input.setFocus()
            return

        if alternate:
            for uid, info in (self.original_config or {}).items():
                if not isinstance(info, dict):
                    continue
                info["alternate_launch"] = False
                self.original_config[str(uid)] = info

        self.original_config[user_id] = {
            "username": username,
            "private_server_link": private_server_link,
            "place": place,
            "cookie": cookie,
            "server_type": self._derive_server_type(private_server_link, place),
            "bad": bad,
            "cap": cap,
            "disabled": disabled,
            "alternate_launch": alternate,
            "skip_reconnect_on_log_disconnect": skip_reconnect_on_log_disconnect,
        }

        self.refresh_user_list()
        self.cancel_edit()
        QMessageBox.information(self, "Success", f"User {user_id} ({username}) added.")

    def update_user(self) -> None:
        if not self.selected_user_id:
            return

        user_id = str(self.selected_user_id)
        username = self.username_input.text().strip() or f"User_{user_id}"
        private_server_link = self.private_server_input.text().strip()
        place = self.place_input.text().strip()
        cookie = self.cookie_input.text().strip()
        disabled = bool(self.disabled_chk.isChecked())
        bad = bool(self.bad_chk.isChecked())
        cap = bool(getattr(self, "cap_chk", None) and self.cap_chk.isChecked())
        alternate = bool(getattr(self, "alternate_chk", None) and self.alternate_chk.isChecked())
        skip_reconnect_on_log_disconnect = bool(
            getattr(self, "skip_reconnect_on_log_disconnect_chk", None)
            and self.skip_reconnect_on_log_disconnect_chk.isChecked()
        )

        cookie_raw = cookie
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
            if cookie and cookie != cookie_raw:
                try:
                    self.cookie_input.setText(cookie)
                except Exception:
                    pass

        if not private_server_link:
            if not self._confirm_missing_ps_link():
                self.private_server_input.setFocus()
                return

        if (not alternate) and (not cookie):
            QMessageBox.warning(self, "Error", "Cookie cannot be empty!")
            self.cookie_input.setFocus()
            return

        if alternate:
            for uid, info in (self.original_config or {}).items():
                if str(uid) == str(user_id):
                    continue
                if not isinstance(info, dict):
                    continue
                info["alternate_launch"] = False
                self.original_config[str(uid)] = info

        existing = self.original_config.get(user_id)
        if not isinstance(existing, dict):
            existing = {}

        existing_cookie = str(existing.get("cookie", "") or "")
        cookie_changed = str(cookie or "") != existing_cookie
        if cookie_changed and bool(existing.get("bad", False)):
            bad = False
            try:
                self.bad_chk.setChecked(False)
            except Exception:
                pass

        updated = dict(existing)
        updated.update(
            {
                "username": username,
                "private_server_link": private_server_link,
                "place": place,
                "cookie": cookie,
                "server_type": self._derive_server_type(private_server_link, place),
                "bad": bad,
                "cap": cap,
                "disabled": disabled,
                "alternate_launch": alternate,
                "skip_reconnect_on_log_disconnect": skip_reconnect_on_log_disconnect,
            }
        )
        self.original_config[user_id] = updated

        self.refresh_user_list()
        QMessageBox.information(self, "Success", f"User {user_id} updated.")

    def delete_selected_user(self) -> None:
        if not self.selected_user_id:
            return
        user_id = str(self.selected_user_id)
        info = self.original_config.get(user_id, {}) if isinstance(self.original_config, dict) else {}
        username = str(info.get("username", f"User_{user_id}")) if isinstance(info, dict) else f"User_{user_id}"

        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete user {user_id} ({username})?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.original_config.pop(user_id, None)
        except Exception:
            pass
        self.refresh_user_list()
        self.cancel_edit()

    # ---------------- Cookie extraction ----------------
    def extract_cookie_from_browser(self) -> None:
        try:
            if bool(getattr(self, "alternate_chk", None) and self.alternate_chk.isChecked()):
                QMessageBox.information(
                    self,
                    "Alternate Launch",
                    "This account is set to Alternate launch mode.\n\nIt does not use cookies, so cookie extraction is disabled.",
                )
                return
            self.browser_login_btn.setEnabled(False)
            self.browser_login_btn.setText("Extracting...")
            self.cookie_extractor.extract_cookie_async(
                callback=self._on_cookie_extraction_complete,
                parent_widget=self,
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start cookie extraction: {e}")
            self._reset_browser_button()

    def _on_cookie_extraction_complete(self, cookie: str):
        try:
            if cookie:
                self.cookie_input.setText(cookie)
                QMessageBox.information(self, "Success", "Cookie extracted and filled.")
            else:
                QMessageBox.information(self, "Cancelled", "Cookie extraction was cancelled or failed.")
        finally:
            self._reset_browser_button()

    def _reset_browser_button(self):
        try:
            self.browser_login_btn.setEnabled(True)
            self.browser_login_btn.setText("Login (Extract Cookie)")
        except Exception:
            pass

    # ---------------- Helpers ----------------
    def _ui_log(self, message: str) -> None:
        msg = str(message or "")
        parent = self.parent()
        add_log = getattr(parent, "add_log", None)
        if callable(add_log):
            QTimer.singleShot(0, lambda m=msg: add_log(m))
        else:
            try:
                print(msg)
            except Exception:
                pass

    def _default_place_id(self) -> str:
        return "15532962292"

    def _find_chrome_executable(self) -> Optional[str]:
        try:
            env_path = str(os.environ.get("CHROME_PATH", "") or "").strip()
            if env_path and os.path.isfile(env_path):
                return env_path
        except Exception:
            pass

        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        try:
            local = os.environ.get("LOCALAPPDATA") or ""
            if local:
                candidates.append(os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"))
        except Exception:
            pass

        for p in candidates:
            try:
                if p and os.path.isfile(p):
                    return p
            except Exception:
                continue

        try:
            import shutil

            for name in ("chrome.exe", "chrome", "google-chrome"):
                p = shutil.which(name)
                if p and os.path.isfile(p):
                    return p
        except Exception:
            pass

        return None

    def _pick_free_port(self) -> int:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _wait_for_chrome_debug(self, port: int, timeout_s: float = 10.0) -> bool:
        deadline = time.time() + float(timeout_s or 0)
        url = f"http://127.0.0.1:{int(port)}/json/version"
        while time.time() < deadline:
            try:
                with urlopen(url, timeout=0.5) as resp:
                    if int(getattr(resp, "status", 200) or 200) == 200:
                        return True
            except Exception:
                time.sleep(0.2)
        return False

    def _open_logged_in_browser(self, uid: str, cookie: str, url: str, profile_dir: Optional[str] = None) -> None:
        """
        Launch a real Chrome process (remote debugging) and inject .ROBLOSECURITY into it.
        This does NOT use CookieExtractor (separate workflow from cookie extraction).
        """
        uid = str(uid or "")
        cookie = str(cookie or "").strip()
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
        url = str(url or "")

        def _worker():
            try:
                chrome_exe = self._find_chrome_executable()
                if not chrome_exe:
                    self._ui_log("[Browser] Chrome not found. Set CHROME_PATH or install Google Chrome.")
                    return

                # Always use a temporary profile so browser data is not persisted.
                try:
                    import tempfile

                    profile_dir_local = tempfile.mkdtemp(prefix=f"jaram_chrome_{uid}_")
                except Exception:
                    self._ui_log(f"[Browser] uid={uid} failed: could not create temp profile.")
                    return

                def _schedule_profile_cleanup(proc, profile_path: Optional[str]) -> None:
                    if proc is None or not profile_path:
                        return

                    def _cleanup():
                        try:
                            proc.wait()
                        except Exception:
                            pass
                        try:
                            shutil.rmtree(profile_path, ignore_errors=True)
                        except Exception:
                            pass

                    threading.Thread(target=_cleanup, daemon=True).start()

                def _launch_with_profile(_profile_dir: Optional[str]) -> tuple[int, Optional[str], Optional[object]]:
                    import subprocess

                    port = self._pick_free_port()
                    args = [
                        chrome_exe,
                        f"--remote-debugging-port={port}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--new-window",
                        "https://www.roblox.com/",
                    ]
                    if _profile_dir:
                        args.insert(2, f"--user-data-dir={_profile_dir}")
                    try:
                        proc = subprocess.Popen(
                            args,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except Exception:
                        proc = subprocess.Popen(args)
                    return port, _profile_dir, proc

                port, used_profile, proc = _launch_with_profile(profile_dir_local)
                _schedule_profile_cleanup(proc, used_profile)
                if not self._wait_for_chrome_debug(port, timeout_s=8.0):
                    try:
                        tmp_profile = tempfile.mkdtemp(prefix=f"jaram_chrome_{uid}_")
                    except Exception:
                        tmp_profile = None

                    if not tmp_profile:
                        self._ui_log(f"[Browser] uid={uid} failed: could not create a temp Chrome profile.")
                        return

                    port, used_profile, proc = _launch_with_profile(tmp_profile)
                    _schedule_profile_cleanup(proc, used_profile)
                    if not self._wait_for_chrome_debug(port, timeout_s=8.0):
                        self._ui_log(f"[Browser] uid={uid} failed: Chrome remote debug not reachable (port={port}).")
                        return

                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options

                opts = Options()
                opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{int(port)}")

                driver = webdriver.Chrome(options=opts)
                try:
                    driver.get("https://www.roblox.com/")
                except Exception:
                    pass

                cookie_obj = {
                    "name": ".ROBLOSECURITY",
                    "value": cookie,
                    "domain": ".roblox.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
                try:
                    try:
                        driver.delete_cookie(".ROBLOSECURITY")
                    except Exception:
                        pass
                    driver.add_cookie(cookie_obj)
                except Exception:
                    cookie_obj["domain"] = "roblox.com"
                    driver.add_cookie(cookie_obj)

                try:
                    driver.get("https://www.roblox.com/home")
                except Exception:
                    pass
                if url:
                    try:
                        driver.get(url)
                    except Exception:
                        pass

                # Keep driver alive to avoid Chrome closing when the session ends/GC occurs.
                try:
                    UserManagementDialog._browser_driver_keepalive.append(driver)
                except Exception:
                    pass

                last_cookie = cookie
                if extract_roblosecurity_from_selenium_driver is not None:
                    try:
                        current_cookie = extract_roblosecurity_from_selenium_driver(driver)
                    except Exception:
                        current_cookie = None
                    if current_cookie and current_cookie != last_cookie:
                        last_cookie = current_cookie
                        if persist_updated_cookie is not None:
                            try:
                                if persist_updated_cookie(self.config_manager, user_id=uid, new_cookie=last_cookie):
                                    self._ui_log(f"[Cookie] uid={uid} updated from browser.")
                                else:
                                    self._ui_log(f"[Cookie] uid={uid} updated in browser, but failed to save (cookies locked?).")
                            except Exception:
                                pass

                def _cookie_watch() -> None:
                    nonlocal last_cookie
                    while True:
                        time.sleep(5.0)
                        try:
                            if extract_roblosecurity_from_selenium_driver is not None:
                                cur = extract_roblosecurity_from_selenium_driver(driver)
                            else:
                                obj = driver.get_cookie(".ROBLOSECURITY")
                                cur = str(obj.get("value") or "") if isinstance(obj, dict) else ""
                        except Exception:
                            break
                        if not cur or cur == last_cookie:
                            continue
                        last_cookie = cur
                        if persist_updated_cookie is None:
                            continue
                        try:
                            if persist_updated_cookie(self.config_manager, user_id=uid, new_cookie=cur):
                                self._ui_log(f"[Cookie] uid={uid} updated from browser.")
                            else:
                                self._ui_log(f"[Cookie] uid={uid} updated in browser, but failed to save (cookies locked?).")
                        except Exception:
                            pass

                try:
                    threading.Thread(target=_cookie_watch, daemon=True).start()
                except Exception:
                    pass

                profile_label = "temp" if used_profile else "default"
                self._ui_log(f"[Browser] uid={uid} opened (profile={profile_label})")

            except Exception as e:
                self._ui_log(f"[Browser] uid={uid} failed: {type(e).__name__}: {e}")

        threading.Thread(target=_worker, daemon=True).start()

    def _compute_browser_url(self, user_info: dict, mode: str) -> str:
        if mode == "home":
            return "https://www.roblox.com/home"

        psl = str(user_info.get("private_server_link", "") or "").strip()
        if psl:
            return psl

        place = str(user_info.get("place", "") or "").strip() or self._default_place_id()
        if place.isdigit():
            return f"https://www.roblox.com/games/{place}"
        return place or "https://www.roblox.com/home"

    def _manager_running(self) -> bool:
        parent = self.parent()
        wt = getattr(parent, "worker_thread", None)
        try:
            return bool(wt and wt.isRunning())
        except Exception:
            return False

    def _is_uid_online_in_manager(self, uid: str) -> bool:
        parent = self.parent()
        wt = getattr(parent, "worker_thread", None)
        if not wt or not getattr(wt, "manager", None) or not getattr(wt, "process_mgr", None):
            return False
        tracker = getattr(wt.manager, "process_tracker", None)
        if not tracker:
            return False
        pids = (tracker.user_processes or {}).get(uid, []) or []
        try:
            return any(wt.process_mgr.verify_process_active(pid) for pid in pids)
        except Exception:
            return False

    def _get_manual_launcher(self) -> GameLauncher:
        if self._manual_launcher is not None:
            return self._manual_launcher

        self._manual_manager = RobloxManager(config_manager=self.config_manager)
        self._manual_process_mgr = ProcessManager(getattr(self._manual_manager, "excluded_pid", 0) or 0)

        timeouts = getattr(self._manual_manager, "timeouts", {}) or {}
        try:
            launch_delay = int(timeouts.get("launch_delay", 4) or 4)
        except Exception:
            launch_delay = 4
        try:
            initial_delay = int(timeouts.get("initial_delay", 4) or 4)
        except Exception:
            initial_delay = 4

        self._manual_launcher = GameLauncher(
            getattr(self._manual_manager, "target_place", self._default_place_id()),
            self._manual_process_mgr,
            getattr(self._manual_manager, "auth_handler", None),
            getattr(self._manual_manager, "process_tracker", None),
            getattr(self._manual_manager, "config_manager", self.config_manager),
            launch_delay=launch_delay,
            initial_delay=initial_delay,
            log_fn=self._ui_log,
        )
        return self._manual_launcher


class BorderRing(QWidget):
    """Transparent widget that draws a circular ring and ignores mouse events."""
    def __init__(self, diameter: int, border_px: int, colour: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)

        # NEW — tell Qt to honour the stylesheet even with a transparent bg
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.setStyleSheet(
            f"border:{border_px}px solid {colour};"
            f"border-radius:{diameter//2}px;"
            "background:transparent;"
        )


class RoundMovieLabel(QLabel):
    def __init__(self, diameter: int, border_px: int, border_color: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"border:{border_px}px solid {border_color};"
            f"border-radius:{diameter//2}px;"
            f"background-color:{ModernStyle.SURFACE};"
        )

        # ── build a mask that ends halfway through the ring ──────────────
        half = diameter / 2
        r    = half - border_px / 1          # 60 − 1.5 = 58.5  (includes border)
        path = QPainterPath()
        path.addEllipse(QPointF(half, half), r, r)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

        # inner square you can show safely (used for movie scaling)
        self._inner_side = int(round(r * 2))   # 117 px


def pil_to_pixmap(img: Image.Image) -> QPixmap:
    """Convert a PIL Image to a detached QPixmap."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


class _PointPickLabel(QLabel):
    """QLabel that emits a point when clicked (normalized to pixmap size)."""
    point_selected = Signal(tuple)

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())
        self.setCursor(Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, event):
        pm = self.pixmap()
        if not pm:
            return
        w = pm.width()
        h = pm.height()
        if w <= 0 or h <= 0:
            return

        pt = event.position().toPoint()
        x = max(0, min(w - 1, pt.x()))
        y = max(0, min(h - 1, pt.y()))
        self.point_selected.emit((x / w, y / h, x, y))


class PointPickDialog(QDialog):
    """Modal dialog that lets the user pick a single point on a screenshot."""

    def __init__(self, pixmap: QPixmap, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._point: Optional[Tuple[float, float, int, int]] = None

        layout = QVBoxLayout(self)

        hint = QLabel("Click on the screenshot to select the coordinate.")
        hint.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(hint)

        self._label = _PointPickLabel(pixmap, self)
        self._label.point_selected.connect(self._on_point_selected)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._label)
        layout.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_point_selected(self, point: tuple):
        try:
            xf, yf, px, py = point
            self._point = (float(xf), float(yf), int(px), int(py))
        except Exception:
            self._point = None
        if self._point is not None:
            self.accept()

    def selected_point(self) -> Optional[Tuple[float, float, int, int]]:
        return self._point


class _SelectableLabel(QLabel):
    """QLabel that exposes a drag-to-select ROI signal."""
    roi_selected = Signal(tuple)

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self._origin: Optional[QPoint] = None

    def mousePressEvent(self, event):
        self._origin = event.position().toPoint()
        self._rubber.setGeometry(QRect(self._origin, QSize()))
        self._rubber.show()

    def mouseMoveEvent(self, event):
        if self._origin is None:
            return
        current = event.position().toPoint()
        rect = QRect(self._origin, current).normalized()
        self._rubber.setGeometry(rect)

    def mouseReleaseEvent(self, event):
        if self._origin is None:
            return
        current = event.position().toPoint()
        rect = QRect(self._origin, current).normalized()
        self._rubber.hide()
        self._rubber.setGeometry(QRect())

        origin = self._origin
        self._origin = None
        if rect.width() < 2 or rect.height() < 2:
            return

        pm = self.pixmap()
        if not pm:
            return
        w = pm.width()
        h = pm.height()
        if w <= 0 or h <= 0:
            return

        roi = (rect.x() / w, rect.y() / h, rect.width() / w, rect.height() / h)
        self.roi_selected.emit(roi)


class ROICropDialog(QDialog):
    """Modal dialog that lets the user pick a chat ROI."""
    def __init__(self, pixmap: QPixmap, parent=None, *, title: str = "Select Chat Area", hint: str = "Drag to draw the chat box. Release to save."):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._roi: Optional[Tuple[float, float, float, float]] = None

        layout = QVBoxLayout(self)
        hint_lbl = QLabel(hint)
        hint_lbl.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(hint_lbl)

        label = _SelectableLabel(pixmap, self)
        label.roi_selected.connect(self._on_roi_selected)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(label)
        layout.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        try:
            self.resize(min(max(pixmap.width() + 40, 520), 980), min(max(pixmap.height() + 120, 360), 760))
        except Exception:
            pass

    def _on_roi_selected(self, roi: Tuple[float, float, float, float]):
        self._roi = roi
        self.accept()

    def selected_roi(self) -> Optional[Tuple[float, float, float, float]]:
        return self._roi


class OCRAlertStopDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_offset: Optional[QPoint] = None
        self.setWindowTitle("OCR Alert")
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("ocrAlertStopWindow")
        self.setMinimumWidth(220)
        self.setStyleSheet(
            f"""
            QDialog#ocrAlertStopWindow {{
                background-color: {ModernStyle.SURFACE};
                border: 1px solid {ModernStyle.BORDER};
                border-radius: 10px;
            }}
            QLabel#ocrAlertTitle {{
                color: {ModernStyle.TEXT_PRIMARY};
                background-color: {ModernStyle.SURFACE};
                font-weight: 600;
            }}
            QPushButton#ocrAlertStopButton {{
                background-color: {ModernStyle.PRIMARY};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 4px 12px;
                font-weight: 600;
            }}
            QPushButton#ocrAlertStopButton:hover {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        self.title_label = QLabel("Stop OCR Alert")
        self.title_label.setObjectName("ocrAlertTitle")
        self.title_label.setCursor(Qt.CursorShape.SizeAllCursor)
        layout.addWidget(self.title_label, 1)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("ocrAlertStopButton")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.stop_btn)

    def mousePressEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        except Exception:
            self._drag_offset = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            try:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
            except Exception:
                pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class RobloxManagerGUI(QMainWindow, FoundStatsMixin):
    # Bridge between AntiAFK worker threads and the Qt UI
    antiafk_log_signal = Signal(str)
    antiafk_state_signal = Signal(bool)
    antiafk_touch_signal = Signal(int)
    antiafk_pre_action_signal = Signal(float)
    autoitem_log_signal = Signal(str)
    autoitem_mouse_block_signal = Signal(bool)
    bes_log_signal = Signal(str)
    ocr_filter_alert_ui_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self._manager_paused: bool = False
        self._paused_worker_state: Optional[dict] = None
        self._paused_at: Optional[float] = None
        self.ocr_worker: Optional[OCRWorker] = None
        self.ocr_roi: Optional[Tuple[float, float, float, float]] = None
        self.ocr_shared_areas: List[dict] = []
        self.ocr_only_mapped_pids: bool = False
        self.ocr_verification_roi: Optional[Tuple[float, float, float, float]] = None
        self._ocr_filter_alert_active: bool = False
        self._ocr_filter_alert_loop_timer: Optional[QTimer] = None
        self._ocr_filter_alert_stop_window: Optional[QWidget] = None
        self._ocr_filter_alert_stop_label: Optional[QLabel] = None
        self._last_ocr_log: Optional[str] = None
        self._ocr_test_last_hash: Optional[int] = None
        self.log_autoscroll: bool = True
        self.ocr_log_autoscroll: bool = True
        self._last_ocr_device_id = None
        self._loading_ocr_settings = False
        self._loading_antiafk_settings = False
        self._loading_autoitem_settings = False
        self.settings_tab_index: Optional[int] = None
        self.dashboard_tab_index: Optional[int] = None
        self.users_tab_index: Optional[int] = None
        self.multiscope_tab_index: Optional[int] = None
        self._ram_export_dialog: Optional[QDialog] = None
        self._utilities_dialog: Optional[QDialog] = None
        self._ram_export_widget: Optional[QWidget] = None
        self._utilities_widget: Optional[QWidget] = None
        self.process_data = {}
        self.user_data = {}
        self._hourly_users_report_last_sent_at: float = 0.0
        self._hourly_users_report_interval_hours: int = 1

        self._users_table_order: List[str] = []
        self._users_table_row_by_uid: Dict[str, int] = {}
        self._users_table_dirty: bool = False
        self._users_table_refresh_pending: bool = False
        self._users_table_force_full: bool = False

        self._multiscope_table_dirty: bool = False
        self._multiscope_table_refresh_pending: bool = False
        self._multiscope_table_latest_rows: Optional[list] = None

        self._log_queue = deque()
        self._activity_recent = deque(maxlen=10)
        self._log_flush_timer: Optional[QTimer] = None
        self._ocr_log_queue = deque()
        self._antiafk_log_queue = deque()
        self._autoitem_log_queue = deque()
        self._bes_log_queue = deque()
        self.config_manager = ConfigManager()
        self.cookie_extractor = CookieExtractor(self)
        self.skip_account_private_link_warning = False
        self.skip_account_public_place_warning = False
        self._settings_baseline: Optional[dict] = None
        self._settings_prompt_ready = False
        self._tab_change_guard = False
        self._last_tab_index: Optional[int] = None
        self._settings_label_map = {
            "window_limit": "Window Limit",
            "spares_mode": "Spares Mode",
            "spares_fraction": "Spare Mode Split",
            "roblox_window_geometry.enforce_on_launch": "Auto-fix Roblox Window Geometry",
            "roblox_window_geometry.x": "Roblox Window X",
            "roblox_window_geometry.y": "Roblox Window Y",
            "roblox_window_geometry.w": "Roblox Window Width",
            "roblox_window_geometry.h": "Roblox Window Height",
            "timeouts.offline": "Restart Inactive After",
            "timeouts.initial_delay": "Initial Launch Delay",
            "timeouts.launch_delay": "Launch Delay",
            "timeouts.strap_threshold": "Strap Limit",
            "timeouts.handoff_lead": "Handoff Lead",
            "timeouts.early_join_window": "Early-Join Window",
            "timeout_monitor.kill_enabled": "Kill After Enabled",
            "timeout_monitor.kill_timeout": "Kill After",
            "timeout_monitor.poll_interval": "Poll Interval",
            "alerts.webhook_url": "Webhook URL",
            "alerts.blackout_ping": "Blackout Ping",
            "alerts.cap_message": "CAP Message",
            "alerts.bad_message": "BAD Message",
            "alerts.hourly_users_report_enabled": "Hourly Users Report",
            "alerts.hourly_users_report_interval_hours": "Hourly Users Report Interval",
            "webhooks": "Webhooks",
            "ui.webhooks_hidden_biomes": "Hidden Webhook Biome Columns",
            "ui.show_tutorial_menu": "Show Tutorial Menu Item",
            "misc.skip_webhook_unknown_context": "Skip Unknown-Context Webhooks",
            "misc.log_confirmed_launch_mode": "Launch Next After Log Confirm",
            "misc.disable_manager_bad_marking": "Disable Manager BAD Marking",
            "misc.msedgewebview2_limiter_enabled": "Enable msedgewebview2 Limiter",
            "multiscope.merchant_webhook": "Merchant Webhook URL",
            "multiscope.merchant_detection_mode": "Merchant Detection Mode",
            "multiscope.enable_jester": "Enable Jester Pings",
            "multiscope.enable_mari": "Enable Mari Pings",
            "multiscope.enable_rin": "Enable Rin Pings",
            "multiscope.jester_ping_type": "Jester Ping Type",
            "multiscope.jester_ping_id": "Jester Ping ID",
            "multiscope.mari_ping_type": "Mari Ping Type",
            "multiscope.mari_ping_id": "Mari Ping ID",
            "multiscope.rin_ping_type": "Rin Ping Type",
            "multiscope.rin_ping_id": "Rin Ping ID",
            "misc.disable_log_based_merchant_detection_when_ocr_merchants_enabled": "Disable Log Merchant Detection While OCR Merchants Active",
        }

        # Anti-AFK engine instance (configured in setup_antiafk_tab)
        self.antiafk: Optional[AntiAFK] = None
        self.antiafk_status_box: Optional[QTextEdit] = None
        self._antiafk_thread_pool = QThreadPool.globalInstance()
        self._antiafk_save_timer = QTimer(self)
        self._antiafk_save_timer.setSingleShot(True)
        self._antiafk_save_timer.setInterval(300)
        self._antiafk_save_timer.timeout.connect(self._save_antiafk_settings)

        # Auto-Item engine + log view (configured in setup_auto_item_tab)
        self.auto_item_engine = None
        self.autoitem_status_box: Optional[QTextEdit] = None
        self._auto_item_hwnd_cache: Dict[int, int] = {}
        self._auto_item_hwnd_cache_ts: float = 0.0
        self._auto_item_hwnd_cache_lock = threading.Lock()
        self._ms_biome_by_server: Dict[str, str] = {}
        self._ms_in_menu_by_server: Dict[str, Optional[bool]] = {}
        self._ms_biome_by_uid: Dict[str, str] = {}
        self._ms_in_menu_by_uid: Dict[str, Optional[bool]] = {}
        self._ms_biome_lock = threading.Lock()
        self._last_multiscope_rows: Optional[list] = None
        self._ms_resume_grace_until: float = 0.0
        self._auto_item_antiafk_was_running: bool = False

        # Anti-AFK "last touch" bookkeeping (touch = Anti-AFK action OR Auto-Item action).
        self._antiafk_touch_lock = threading.Lock()
        self._antiafk_last_touch_by_uid: Dict[str, float] = {}
        self._antiafk_pid_to_uid: Dict[int, str] = {}
        self._antiafk_disconnected_pids: Set[int] = set()

        # BES limiter controller (optional; Windows-only)
        self._loading_bes_settings = False
        self.bes_controller = (
            BESMultiProcessController(log=self.bes_log_signal.emit, max_cycle_ms=1000)
            if BESMultiProcessController is not None
            else None
        )
        self.bes_log_box: Optional[QTextEdit] = None
        self._bes_cfg_lock = threading.Lock()
        self._bes_cfg_cache: Dict = dict(self.config_manager.default_settings.get("bes", {}) or {})
        try:
            _settings = self.config_manager.load_settings() or {}
            _bes_cfg = _settings.get("bes", None)
            if isinstance(_bes_cfg, dict):
                self._bes_cfg_cache.update(dict(_bes_cfg))
        except Exception:
            pass
        self._bes_save_timer: Optional[QTimer] = None
        self._bes_tick_timer: Optional[QTimer] = None

        # Global hotkeys (Windows RegisterHotKey -> WM_HOTKEY)
        self._win_hotkey_filter: Optional[_WinHotkeyFilter] = None
        self._auto_item_hotkey_id: int = 0xA117
        self._auto_item_hotkey_hwnd: int = 0
        self._auto_item_hotkey_registered: bool = False

        # Connect Anti-AFK cross-thread signals
        self.antiafk_log_signal.connect(self._on_antiafk_status)
        self.antiafk_state_signal.connect(self._on_antiafk_state_changed)
        self.antiafk_touch_signal.connect(self._on_antiafk_touch)
        self.antiafk_pre_action_signal.connect(self._on_antiafk_pre_action)
        self.autoitem_log_signal.connect(self._on_autoitem_status)
        self.autoitem_mouse_block_signal.connect(self._on_autoitem_mouse_block)
        self.bes_log_signal.connect(self._on_bes_log)
        self.ocr_filter_alert_ui_signal.connect(
            self._handle_ocr_filter_alert_ui,
            Qt.ConnectionType.QueuedConnection,
        )

        self.setup_ui()
        try:
            self._log_flush_timer = QTimer(self)
            self._log_flush_timer.setInterval(100)
            self._log_flush_timer.timeout.connect(self._flush_log_queue)
            self._log_flush_timer.start()
        except Exception:
            self._log_flush_timer = None
        try:
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
        except Exception:
            pass
        # NEW: add the Multiscope tab
        self.setup_multiscope_tab()
        self.setup_timers()
        self._last_tab_index = self.tab_widget.currentIndex()
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        self._settings_prompt_ready = True
        QTimer.singleShot(0, self._maybe_prompt_cookie_encryption)


    def eventFilter(self, obj, event):
        """
        Prevent mouse-wheel from changing inputs unless they were clicked first.
        This avoids accidental changes while scrolling.
        """
        try:
            if event is not None:
                et = event.type()

                def _wheel_target(w):
                    if isinstance(w, (QAbstractSpinBox, QComboBox, QSlider)):
                        return w
                    if isinstance(w, QLineEdit):
                        p = w.parent()
                        if isinstance(p, (QAbstractSpinBox, QComboBox)):
                            return p
                    return None

                if et == QEvent.Type.MouseButtonPress:
                    target = _wheel_target(obj)
                    if target is not None:
                        target.setProperty("_wheel_armed", True)

                if et == QEvent.Type.FocusOut:
                    target = _wheel_target(obj)
                    if target is not None:
                        target.setProperty("_wheel_armed", False)

                if et == QEvent.Type.Wheel:
                    target = _wheel_target(obj)
                    if target is not None and not bool(target.property("_wheel_armed")):
                        event.ignore()
                        parent = target.parent()
                        while parent is not None and not isinstance(parent, QAbstractScrollArea):
                            parent = parent.parent()
                        if isinstance(parent, QAbstractScrollArea):
                            try:
                                QApplication.sendEvent(parent.viewport(), event)
                            except Exception:
                                try:
                                    QApplication.sendEvent(parent, event)
                                except Exception:
                                    pass
                        return True
        except Exception:
            pass
        return super().eventFilter(obj, event)


    def setup_ui(self):
        self.setWindowTitle("J.JARAM - Jirach1's Just Another Roblox Account Manager")
        self.setGeometry(100, 100, 1100, 720)

        icon_path = _get_icon_path()
        if icon_path and os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        header_layout = QHBoxLayout()

        title_label = QLabel("J.JARAM - Jirach1's Just Another Roblox Account Manager")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title_label.setFont(title_font)
        header_layout.addWidget(title_label)

        header_layout.addStretch()

        self.start_btn = QPushButton("Start Manager")
        self.start_btn.setProperty("class", "success")
        self.start_btn.clicked.connect(self.start_manager)
        header_layout.addWidget(self.start_btn)

        self.pause_btn = QPushButton("Pause Manager")
        self.pause_btn.setProperty("class", "warning")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.toggle_pause_manager)
        header_layout.addWidget(self.pause_btn)

        self.stop_btn = QPushButton("Stop Manager")
        self.stop_btn.setProperty("class", "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_manager)
        header_layout.addWidget(self.stop_btn)

        main_layout.addLayout(header_layout)

        status_layout = QHBoxLayout()
        self.status_label = QLabel("Status: Stopped")
        self.status_label.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY}; font-weight: bold;")
        status_layout.addWidget(self.status_label)

        status_layout.addStretch()

        self.uptime_label = QLabel("Uptime: 00:00:00")
        status_layout.addWidget(self.uptime_label)

        main_layout.addLayout(status_layout)

        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        self.setup_dashboard_tab()
        self.setup_users_tab()
        self.setup_accounts_tab()
        self.setup_logs_tab()
        self.setup_ocr_tab()
        self.setup_antiafk_tab()
        self.setup_auto_item_tab()
        self.setup_bes_tab()
        setup_TRIMMER_tab(self)
        self.setup_settings_tab()
        self.setup_credits_tab()

        self.setup_menu_bar()

        self.setStyleSheet(ModernStyle.get_stylesheet())

        self.start_time = None
        self.user_data = {}

    def setup_menu_bar(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")

        manage_users_action = file_menu.addAction("Manage Users")
        manage_users_action.triggered.connect(self.open_user_management)

        config_location_action = file_menu.addAction("Show Config Location")
        config_location_action.triggered.connect(self.show_config_location)

        cookie_encryption_action = file_menu.addAction("Cookie Encryption...")
        cookie_encryption_action.triggered.connect(self.show_cookie_encryption_dialog)

        file_menu.addSeparator()

        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        extras_menu = menubar.addMenu("Extras")

        # Live-updated counters (persisted by MultiScope)
        self._extras_found_counter_action = extras_menu.addAction("All-time found: Biomes 0 | Merchants 0")
        self._extras_found_counter_action.setEnabled(False)
        try:
            extras_menu.aboutToShow.connect(self._refresh_extras_found_counters)
        except Exception:
            pass

        found_stats_action = extras_menu.addAction("Found Stats…")
        found_stats_action.triggered.connect(self.show_found_stats_window)

        extras_menu.addSeparator()

        ram_export_action = extras_menu.addAction("RAM Export")
        ram_export_action.triggered.connect(self.show_ram_export_window)

        utilities_action = extras_menu.addAction("Utilities")
        utilities_action.triggered.connect(self.show_utilities_window)

        help_menu = menubar.addMenu("Help")

        self._tutorial_menu_action = help_menu.addAction("Tutorial")
        self._tutorial_menu_action.triggered.connect(self.open_help_link)
        self._tutorial_menu_sep = help_menu.addSeparator()

        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

        show_tutorial = False
        try:
            settings = self.config_manager.peek_settings() or {}
            ui = settings.get("ui", {}) or {}
            if isinstance(ui, dict):
                show_tutorial = bool(ui.get("show_tutorial_menu", False))
        except Exception:
            show_tutorial = False
        self._apply_tutorial_menu_visibility(show_tutorial)

    def _apply_tutorial_menu_visibility(self, show: bool) -> None:
        try:
            a = getattr(self, "_tutorial_menu_action", None)
            if a is not None:
                a.setVisible(bool(show))
        except Exception:
            pass
        try:
            sep = getattr(self, "_tutorial_menu_sep", None)
            if sep is not None:
                sep.setVisible(bool(show))
        except Exception:
            pass

    def _prompt_cookie_unlock_password(self, title: str, message: str) -> Optional[str]:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(message))
        pwd = QLineEdit()
        pwd.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(pwd)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)

        def _accept():
            if not pwd.text().strip():
                QMessageBox.warning(dlg, "Password Required", "Please enter a password.")
                return
            dlg.accept()

        buttons.accepted.connect(_accept)
        buttons.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return pwd.text()

    def _prompt_cookie_password_setup(
        self,
        title: str,
        *,
        include_backup_option: bool,
        default_encrypt_existing_backups: bool,
    ) -> Optional[Tuple[str, bool]]:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        layout = QVBoxLayout(dlg)
        form = QFormLayout()
        pwd1 = QLineEdit()
        pwd1.setEchoMode(QLineEdit.EchoMode.Password)
        pwd2 = QLineEdit()
        pwd2.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", pwd1)
        form.addRow("Confirm:", pwd2)
        layout.addLayout(form)

        backups_chk = None
        if include_backup_option:
            backups_chk = QCheckBox("Encrypt cookies in existing backup files")
            backups_chk.setToolTip("One-time action: encrypt cookies inside existing users_*.json backups.")
            backups_chk.setChecked(bool(default_encrypt_existing_backups))
            layout.addWidget(backups_chk)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)

        def _accept():
            p1 = pwd1.text()
            p2 = pwd2.text()
            if not p1:
                QMessageBox.warning(dlg, "Password Required", "Please enter a password.")
                return
            if p1 != p2:
                QMessageBox.warning(dlg, "Password Mismatch", "Passwords do not match.")
                return
            dlg.accept()

        buttons.accepted.connect(_accept)
        buttons.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        password = pwd1.text()
        return password, (backups_chk.isChecked() if backups_chk is not None else False)

    def _maybe_prompt_cookie_encryption(self) -> None:
        try:
            # Ensure prompts aren't hidden behind splash / startup work.
            try:
                if not self.isVisible():
                    QTimer.singleShot(250, self._maybe_prompt_cookie_encryption)
                    return
            except Exception:
                pass

            if self.config_manager.cookie_encryption_prompted():
                return
            if not self.config_manager.cookie_encryption_available():
                self.config_manager.set_cookie_encryption_prompted(True)
                return

            reply = QMessageBox.question(
                self,
                "Cookie Encryption",
                "Would you like to encrypt cookies in users.json?\n\n"
                "Encrypted cookies require a password to unlock.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.config_manager.set_cookie_encryption_prompted(True)
                return

            result = self._prompt_cookie_password_setup(
                "Set Cookie Password",
                include_backup_option=True,
                default_encrypt_existing_backups=True,
            )
            if not result:
                # User canceled; allow prompting again next launch.
                return
            password, encrypt_existing_backups = result
            if self.config_manager.enable_cookie_encryption(password):
                extra = ""
                if encrypt_existing_backups:
                    backup_result = self.config_manager.encrypt_existing_users_backups()
                    if backup_result is None:
                        err = self.config_manager.get_cookie_error() or "Failed to encrypt existing backups."
                        extra = "\n\n" + err
                    else:
                        scanned, updated, failed = backup_result
                        unchanged = max(0, scanned - updated - failed)
                        extra = (
                            "\n\nExisting backups:"
                            f"\n- Scanned: {scanned}"
                            f"\n- Updated: {updated}"
                            f"\n- Unchanged: {unchanged}"
                        )
                        if failed:
                            extra += f"\n- Failed: {failed}"
                QMessageBox.information(self, "Cookie Encryption", "Cookie encryption is enabled." + extra)
                self.config_manager.set_cookie_encryption_prompted(True)
            else:
                err = self.config_manager.get_cookie_error()
                msg = "Failed to enable cookie encryption."
                if err:
                    msg = msg + "\n\n" + err
                QMessageBox.warning(self, "Cookie Encryption", msg)
                # Leave prompted=False so the user gets asked again next launch.
        except Exception as e:
            QMessageBox.critical(self, "Cookie Encryption", f"Unexpected error: {e}")

    def show_cookie_encryption_dialog(self) -> None:
        if not self.config_manager.cookie_encryption_available():
            QMessageBox.warning(
                self,
                "Cookie Encryption",
                "Cookie encryption is not available on this system.",
            )
            return

        cfg = self.config_manager._get_cookie_encryption_settings()
        enabled = bool(cfg.get("enabled", False))
        unlocked = self.config_manager.is_cookie_unlocked()
        version = 2
        try:
            version = int(cfg.get("version") or 2)
        except Exception:
            version = 2

        dlg = QDialog(self)
        dlg.setWindowTitle("Cookie Encryption")
        layout = QVBoxLayout(dlg)

        status = "Enabled" if enabled else "Disabled"
        if enabled:
            status += " (Unlocked)" if unlocked else " (Locked)"
        status_label = QLabel(f"Status: {status}")
        layout.addWidget(status_label)

        backups_chk = QCheckBox("Encrypt cookies in existing backup files")
        backups_chk.setToolTip("One-time action: encrypt cookies inside existing users_*.json backups.")
        backups_chk.setEnabled(bool(enabled))
        layout.addWidget(backups_chk)

        btn_row = QHBoxLayout()

        def _refresh_after_change():
            try:
                self._schedule_users_table_refresh(force_full=True)
            except Exception:
                pass

        def _ensure_unlocked() -> bool:
            if not self.config_manager.cookie_encryption_enabled():
                QMessageBox.warning(dlg, "Cookie Encryption", "Cookie encryption is not enabled.")
                return False
            if self.config_manager.is_cookie_unlocked():
                return True
            pwd = self._prompt_cookie_unlock_password(
                "Unlock Cookies",
                "Enter your cookie password to continue.",
            )
            if not pwd:
                return False
            if not self.config_manager.unlock_cookie_encryption(pwd):
                err = self.config_manager.get_cookie_error() or "Incorrect password."
                QMessageBox.warning(dlg, "Cookie Encryption", err)
                return False
            return True

        def _run_encrypt_existing_backups() -> None:
            try:
                if not self.config_manager.cookie_encryption_enabled():
                    QMessageBox.information(dlg, "Cookie Encryption", "Enable cookie encryption first.")
                    return
                if not _ensure_unlocked():
                    return
                confirm = QMessageBox.question(
                    dlg,
                    "Encrypt Existing Backups",
                    "Encrypt cookies inside existing users.json files in the backup folder?\n\n"
                    "This will overwrite those backup files.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if confirm != QMessageBox.StandardButton.Yes:
                    return
                result = self.config_manager.encrypt_existing_users_backups()
                if result is None:
                    err = self.config_manager.get_cookie_error() or "Failed to encrypt existing backups."
                    QMessageBox.warning(dlg, "Cookie Encryption", err)
                    return
                scanned, updated, failed = result
                unchanged = max(0, scanned - updated - failed)
                msg = (
                    "Existing backups:"
                    f"\n- Scanned: {scanned}"
                    f"\n- Updated: {updated}"
                    f"\n- Unchanged: {unchanged}"
                )
                if failed:
                    msg += f"\n- Failed: {failed}"
                QMessageBox.information(dlg, "Cookie Encryption", msg)
            except Exception as e:
                QMessageBox.critical(dlg, "Cookie Encryption", f"Unexpected error: {e}")

        def _on_backups_toggle(checked: bool) -> None:
            if not checked:
                return
            try:
                _run_encrypt_existing_backups()
            finally:
                try:
                    backups_chk.blockSignals(True)
                    backups_chk.setChecked(False)
                finally:
                    backups_chk.blockSignals(False)

        backups_chk.toggled.connect(_on_backups_toggle)

        if enabled:
            if not unlocked:
                unlock_btn = QPushButton("Unlock Cookies")
                btn_row.addWidget(unlock_btn)
                reset_btn = QPushButton("Reset")
                reset_btn.setToolTip("Deletes ALL cookies and disables cookie encryption (Unrecoverable).")
                btn_row.addWidget(reset_btn)

                def _do_unlock():
                    try:
                        pwd = self._prompt_cookie_unlock_password(
                            "Unlock Cookies",
                            "Enter your cookie password to unlock.",
                        )
                        if not pwd:
                            return
                        if self.config_manager.unlock_cookie_encryption(pwd):
                            QMessageBox.information(dlg, "Cookie Encryption", "Cookies unlocked.")
                            _refresh_after_change()
                            dlg.accept()
                        else:
                            err = self.config_manager.get_cookie_error() or "Incorrect password."
                            QMessageBox.warning(dlg, "Cookie Encryption", err)
                    except Exception as e:
                        QMessageBox.critical(dlg, "Cookie Encryption", f"Unexpected error: {e}")

                unlock_btn.clicked.connect(_do_unlock)

                def _do_reset():
                    try:
                        confirm = QMessageBox.question(
                            dlg,
                            "Reset Cookie Encryption",
                            "Disable cookie encryption and clear encrypted cookie values?\n\n"
                            "You will need to re-enter cookies.",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        )
                        if confirm != QMessageBox.StandardButton.Yes:
                            return
                        if self.config_manager.reset_cookie_encryption():
                            QMessageBox.information(
                                dlg,
                                "Cookie Encryption",
                                "Cookie encryption disabled and encrypted cookies cleared.",
                            )
                            _refresh_after_change()
                            dlg.accept()
                        else:
                            err = self.config_manager.get_cookie_error() or "Reset failed."
                            QMessageBox.warning(dlg, "Cookie Encryption", err)
                    except Exception as e:
                        QMessageBox.critical(dlg, "Cookie Encryption", f"Unexpected error: {e}")

                reset_btn.clicked.connect(_do_reset)

            change_btn = QPushButton("Change Password")
            decrypt_btn = QPushButton("Decrypt Cookies")
            btn_row.addWidget(change_btn)
            btn_row.addWidget(decrypt_btn)

            def _do_change_password():
                try:
                    if not _ensure_unlocked():
                        return
                    result = self._prompt_cookie_password_setup(
                        "Change Cookie Password",
                        include_backup_option=False,
                        default_encrypt_existing_backups=True,
                    )
                    if not result:
                        return
                    new_password, _ = result
                    if self.config_manager.change_cookie_encryption_password(new_password):
                        QMessageBox.information(dlg, "Cookie Encryption", "Password updated.")
                        _refresh_after_change()
                        dlg.accept()
                    else:
                        err = self.config_manager.get_cookie_error() or "Failed to update password."
                        QMessageBox.warning(dlg, "Cookie Encryption", err)
                except Exception as e:
                    QMessageBox.critical(dlg, "Cookie Encryption", f"Unexpected error: {e}")

            def _do_decrypt():
                try:
                    if not _ensure_unlocked():
                        return
                    box = QMessageBox(dlg)
                    box.setIcon(QMessageBox.Icon.Question)
                    box.setWindowTitle("Decrypt Cookies")
                    box.setText(
                        "Decrypt cookies and disable encryption?\n\n"
                        "This removes the password requirement."
                    )
                    decrypt_backups_chk = QCheckBox("Also decrypt cookies in existing backup files")
                    decrypt_backups_chk.setToolTip("Removes the password requirement for existing users_*.json backups.")
                    box.setCheckBox(decrypt_backups_chk)
                    yes_btn = box.addButton(QMessageBox.StandardButton.Yes)
                    box.addButton(QMessageBox.StandardButton.No)
                    box.setDefaultButton(yes_btn)
                    box.exec()
                    if box.clickedButton() != yes_btn:
                        return

                    entropy = self.config_manager._get_cookie_entropy()
                    if entropy is None:
                        QMessageBox.warning(dlg, "Cookie Encryption", "Cookies are locked. Unlock first.")
                        return

                    if not self.config_manager.disable_cookie_encryption():
                        err = self.config_manager.get_cookie_error() or "Failed to decrypt cookies."
                        QMessageBox.warning(dlg, "Cookie Encryption", err)
                        return

                    if decrypt_backups_chk.isChecked():
                        backup_result = self.config_manager.decrypt_existing_users_backups(entropy)
                        if backup_result is None:
                            err = self.config_manager.get_cookie_error() or "Failed to decrypt backups."
                            QMessageBox.warning(dlg, "Cookie Encryption", err)
                        else:
                            scanned, updated, skipped, failed = backup_result
                            unchanged = max(0, scanned - updated - skipped - failed)
                            msg = (
                                "Backup files:"
                                f"\n- Scanned: {scanned}"
                                f"\n- Decrypted: {updated}"
                                f"\n- Unchanged: {unchanged}"
                            )
                            if skipped:
                                msg += f"\n- Skipped: {skipped}"
                            if failed:
                                msg += f"\n- Failed: {failed}"
                            if skipped:
                                msg += "\n\nSkipped backups usually mean a different password was used or the file is corrupted."
                            QMessageBox.information(dlg, "Cookie Encryption", msg)

                    QMessageBox.information(dlg, "Cookie Encryption", "Cookie encryption disabled.")
                    _refresh_after_change()
                    dlg.accept()
                except Exception as e:
                    QMessageBox.critical(dlg, "Cookie Encryption", f"Unexpected error: {e}")

            change_btn.clicked.connect(_do_change_password)
            decrypt_btn.clicked.connect(_do_decrypt)
        else:
            enable_btn = QPushButton("Enable Encryption")
            btn_row.addWidget(enable_btn)

            def _do_enable():
                try:
                    result = self._prompt_cookie_password_setup(
                        "Set Cookie Password",
                        include_backup_option=True,
                        default_encrypt_existing_backups=True,
                    )
                    if not result:
                        return
                    password, encrypt_existing_backups = result
                    if self.config_manager.enable_cookie_encryption(password):
                        extra = ""
                        if encrypt_existing_backups:
                            backup_result = self.config_manager.encrypt_existing_users_backups()
                            if backup_result is None:
                                err = self.config_manager.get_cookie_error() or "Failed to encrypt existing backups."
                                extra = "\n\n" + err
                            else:
                                scanned, updated, failed = backup_result
                                unchanged = max(0, scanned - updated - failed)
                                extra = (
                                    "\n\nExisting backups:"
                                    f"\n- Scanned: {scanned}"
                                    f"\n- Updated: {updated}"
                                    f"\n- Unchanged: {unchanged}"
                                )
                                if failed:
                                    extra += f"\n- Failed: {failed}"
                        QMessageBox.information(dlg, "Cookie Encryption", "Cookie encryption is enabled." + extra)
                        _refresh_after_change()
                        dlg.accept()
                    else:
                        err = self.config_manager.get_cookie_error() or "Failed to enable encryption."
                        QMessageBox.warning(dlg, "Cookie Encryption", err)
                except Exception as e:
                    QMessageBox.critical(dlg, "Cookie Encryption", f"Unexpected error: {e}")

            enable_btn.clicked.connect(_do_enable)

        btn_row.addStretch()
        close_btn = QPushButton("Close")
        btn_row.addWidget(close_btn)
        close_btn.clicked.connect(dlg.accept)
        layout.addLayout(btn_row)

        dlg.exec()

    def show_ram_export_window(self):
        if self._ram_export_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("RAM Export")
            dlg.resize(700, 520)

            layout = QVBoxLayout(dlg)
            if self._ram_export_widget is None:
                self._ram_export_widget = self.setup_RAMEXPORT_tab()
            layout.addWidget(self._ram_export_widget)

            self._ram_export_dialog = dlg

        self._ram_export_dialog.show()
        self._ram_export_dialog.raise_()
        self._ram_export_dialog.activateWindow()

    def show_utilities_window(self):
        if self._utilities_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Utilities")
            dlg.resize(950, 720)

            layout = QVBoxLayout(dlg)
            if self._utilities_widget is None:
                self._utilities_widget = build_utilities_widget(self)
            layout.addWidget(self._utilities_widget)

            self._utilities_dialog = dlg

        self._utilities_dialog.show()
        self._utilities_dialog.raise_()
        self._utilities_dialog.activateWindow()

    def setup_dashboard_tab(self):
        dashboard_widget = QWidget()
        layout = QVBoxLayout(dashboard_widget)

        stats_group = QGroupBox("System Statistics")
        stats_layout = QGridLayout(stats_group)

        self.total_users_label = QLabel("0")
        self.total_users_label.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {ModernStyle.PRIMARY};")
        stats_layout.addWidget(QLabel("Total Users:"), 0, 0)
        stats_layout.addWidget(self.total_users_label, 0, 1)

        self.active_users_label = QLabel("0")
        self.active_users_label.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {ModernStyle.SECONDARY};")
        stats_layout.addWidget(QLabel("Active Users:"), 0, 2)
        stats_layout.addWidget(self.active_users_label, 0, 3)

        self.total_processes_label = QLabel("0")
        self.total_processes_label.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {ModernStyle.WARNING};")
        stats_layout.addWidget(QLabel("Total Processes:"), 1, 0)
        stats_layout.addWidget(self.total_processes_label, 1, 1)

        self.pending_restarts_label = QLabel("0")
        self.pending_restarts_label.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {ModernStyle.ERROR};")
        stats_layout.addWidget(QLabel("Pending Restarts:"), 1, 2)
        stats_layout.addWidget(self.pending_restarts_label, 1, 3)

        layout.addWidget(stats_group)

        actions_group = QGroupBox("Quick Actions")
        actions_layout = QHBoxLayout(actions_group)

        restart_all_btn = QPushButton("Restart All Sessions")
        restart_all_btn.clicked.connect(self.restart_all_sessions)
        actions_layout.addWidget(restart_all_btn)

        kill_all_btn = QPushButton("Kill All Processes")
        kill_all_btn.setProperty("class", "danger")
        kill_all_btn.clicked.connect(self.kill_all_processes)
        actions_layout.addWidget(kill_all_btn)

        actions_layout.addStretch()

        layout.addWidget(actions_group)

        activity_group = QGroupBox("Recent Activity")
        activity_layout = QVBoxLayout(activity_group)

        self.activity_list = QTextEdit()
        self.activity_list.setMaximumHeight(200)
        self.activity_list.setReadOnly(True)
        activity_layout.addWidget(self.activity_list)

        layout.addWidget(activity_group)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(dashboard_widget)
        self.dashboard_tab_index = self.tab_widget.addTab(scroll, "Dashboard")

    def setup_users_tab(self):
        users_widget = QWidget()
        layout = QVBoxLayout(users_widget)

        self.users_table = QTableWidget()
        self.users_table.setColumnCount(13)
        self.users_table.setHorizontalHeaderLabels([
            "User ID","Username","Private Server","Place",
            "Server",               # ← NEW
            "Status","PIDs","TTL(s)","Created","Last Active",
            "Inactive For", "Anti-AFK Age", "Actions"
        ])

        header = self.users_table.horizontalHeader()
        # NOTE: ResizeToContents becomes extremely expensive with many rows + frequent updates.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)       # Server
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Interactive)       # Created
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)           # Last Active
        header.setSectionResizeMode(10, QHeaderView.ResizeMode.Fixed)            # Inactive For
        header.setSectionResizeMode(11, QHeaderView.ResizeMode.Interactive)      # Anti-AFK Age
        header.setSectionResizeMode(12, QHeaderView.ResizeMode.Fixed)            # Actions

        self.users_table.setColumnWidth(0, 100)
        self.users_table.setColumnWidth(1, 160)
        self.users_table.setColumnWidth(2, 200)
        self.users_table.setColumnWidth(3, 100)
        self.users_table.setColumnWidth(4, 120)
        self.users_table.setColumnWidth(5, 110)
        self.users_table.setColumnWidth(6, 160)
        self.users_table.setColumnWidth(7, 100)   # TTL(s)
        self.users_table.setColumnWidth(8, 100)   # Created
        self.users_table.setColumnWidth(10, 160)  # Inactive For
        self.users_table.setColumnWidth(11, 120)  # Anti-AFK Age
        self.users_table.setColumnWidth(12, 260)  # Actions
        self.users_table.verticalHeader().setDefaultSectionSize(60)
        self.users_table.setWordWrap(False)
        try:
            self.users_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        except Exception:
            pass

        try:
            self.users_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.users_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        except Exception:
            pass
        self.users_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        layout.addWidget(self.users_table)

        controls_layout = QHBoxLayout()
        refresh_users_btn = QPushButton("Refresh")
        refresh_users_btn.clicked.connect(self.refresh_users)
        controls_layout.addWidget(refresh_users_btn)

        add_user_btn = QPushButton("Modify Users")
        add_user_btn.clicked.connect(self.open_user_management)
        controls_layout.addWidget(add_user_btn)

        show_selected_btn = QPushButton("Show Selected")
        show_selected_btn.clicked.connect(self.show_selected_user_window)
        controls_layout.addWidget(show_selected_btn)

        kill_selected_btn = QPushButton("Kill Selected")
        kill_selected_btn.setProperty("class", "danger")
        kill_selected_btn.clicked.connect(self.kill_selected_user)
        controls_layout.addWidget(kill_selected_btn)

        controls_layout.addStretch()
        layout.addLayout(controls_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(users_widget)
        self.users_tab_index = self.tab_widget.addTab(scroll, "Users")

    def setup_accounts_tab(self):
        """Account management tab."""
        accounts_widget = QWidget()
        layout = QVBoxLayout(accounts_widget)

        header_label = QLabel("Account Management")
        header_label.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {ModernStyle.TEXT_PRIMARY}; padding: 10px 0;")
        layout.addWidget(header_label)

        main_layout = QHBoxLayout()

        # Left form
        form_widget = QWidget()
        form_widget.setMaximumWidth(400)
        form_layout = QVBoxLayout(form_widget)

        form_title = QLabel("Add New Account")
        form_title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {ModernStyle.TEXT_PRIMARY}; padding: 5px 0;")
        form_layout.addWidget(form_title)

        self.account_user_id = QLineEdit()
        self.account_user_id.setPlaceholderText("User ID (e.g., 123456789)")
        form_layout.addWidget(QLabel("User ID:"))
        form_layout.addWidget(self.account_user_id)

        self.account_username = QLineEdit()
        self.account_username.setPlaceholderText("Username (e.g., PlayerName)")
        form_layout.addWidget(QLabel("Username:"))
        form_layout.addWidget(self.account_username)

        form_layout.addWidget(QLabel("Server Type:"))
        server_layout = QHBoxLayout()
        self.account_private_radio = QRadioButton("Private Server")
        self.account_public_radio = QRadioButton("Public Server")
        self.account_private_radio.setChecked(True)
        server_layout.addWidget(self.account_private_radio)
        server_layout.addWidget(self.account_public_radio)
        form_layout.addLayout(server_layout)

        self.account_private_link = QLineEdit()
        self.account_private_link.setPlaceholderText("Private server link")
        self.account_private_link_label = QLabel("Private Server Link:")
        form_layout.addWidget(self.account_private_link_label)
        form_layout.addWidget(self.account_private_link)

        self.account_place_id = QLineEdit()
        self.account_place_id.setPlaceholderText("Place ID")
        self.account_place_id_label = QLabel("Place ID:")
        form_layout.addWidget(self.account_place_id_label)
        form_layout.addWidget(self.account_place_id)
        self.account_place_id_label.hide()
        self.account_place_id.hide()

        self.account_cookie = QLineEdit()
        self.account_cookie.setPlaceholderText("ROBLOSECURITY cookie")
        form_layout.addWidget(QLabel("Cookie:"))
        cookie_layout = QHBoxLayout()
        cookie_layout.addWidget(self.account_cookie)

        self.account_browser_login_btn = QPushButton("Login with Browser")
        self.account_browser_login_btn.setToolTip("Open browser to login and automatically extract cookie")
        self.account_browser_login_btn.clicked.connect(self.extract_account_cookie_from_browser)
        self.account_browser_login_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_PRIMARY};
                border: 1px solid {ModernStyle.BORDER};
                padding: 8px 16px;
                border-radius: 6px;
                font-weight: 500;
                font-size: 13px;
                min-height: 28px;
                min-width: 160px;
            }}
            QPushButton:hover {{
                background-color: {ModernStyle.BORDER};
            }}
            """
        )
        cookie_layout.addWidget(self.account_browser_login_btn)
        form_layout.addLayout(cookie_layout)

        flags_row = QWidget()
        flags_layout = QVBoxLayout(flags_row)
        flags_layout.setContentsMargins(0, 0, 0, 0)
        flags_layout.setSpacing(4)

        flags_top = QHBoxLayout()
        flags_top.setContentsMargins(0, 0, 0, 0)

        self.account_disabled = QCheckBox("Disable this account")
        flags_top.addWidget(self.account_disabled)

        flags_top.addSpacing(52)

        self.account_alternate_launch = QCheckBox("Alternate")
        self.account_alternate_launch.setToolTip(
            "Alternate launch mode (no cookies required).\n"
            "Make sure any window open with the account is closed before launching with JARAM"
        )
        try:
            self.account_alternate_launch.toggled.connect(self._on_account_alternate_launch_toggled)
        except Exception:
            pass
        flags_top.addWidget(self.account_alternate_launch)

        flags_top.addStretch(1)
        flags_layout.addLayout(flags_top)

        self.account_skip_reconnect_on_log_disconnect = QCheckBox("Disable log reconnects")
        self.account_skip_reconnect_on_log_disconnect.setToolTip(
            "When enabled, this account will not auto-reconnect after log disconnects."
        )
        flags_bottom = QHBoxLayout()
        flags_bottom.setContentsMargins(0, 0, 0, 0)
        flags_bottom.addWidget(self.account_skip_reconnect_on_log_disconnect)
        flags_bottom.addStretch(1)
        flags_layout.addLayout(flags_bottom)
        form_layout.addWidget(flags_row)

        button_layout = QHBoxLayout()
        self.add_account_btn = QPushButton("Add Account")
        self.add_account_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {ModernStyle.PRIMARY};
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}
            QPushButton:pressed {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}
            """
        )
        self.add_account_btn.clicked.connect(self.add_account)
        button_layout.addWidget(self.add_account_btn)

        self.clear_form_btn = QPushButton("Clear")
        self.clear_form_btn.clicked.connect(self.clear_account_form)
        button_layout.addWidget(self.clear_form_btn)

        form_layout.addLayout(button_layout)
        form_layout.addStretch()

        self.account_private_radio.toggled.connect(self.on_account_server_type_changed)

        main_layout.addWidget(form_widget)

        # Right list
        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)

        list_title = QLabel("Existing Accounts")
        list_title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {ModernStyle.TEXT_PRIMARY}; padding: 5px 0;")
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(list_title, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addStretch()
        disable_selected_btn = QPushButton("Disable Select")
        disable_selected_btn.clicked.connect(lambda: self.disable_selected_users(self.accounts_list))
        toggle_selected_btn = QPushButton("Toggle Status")
        toggle_selected_btn.clicked.connect(lambda: self.toggle_selected_users_status(self.accounts_list))
        clear_bad_btn = QPushButton("Clear All Flags")
        clear_bad_btn.clicked.connect(self._clear_bad_flags)
        clear_sel_bad_btn = QPushButton("Clear Select Flag")
        clear_sel_bad_btn.clicked.connect(self._clear_selected_bad_flags)
        title_row.addWidget(disable_selected_btn, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(toggle_selected_btn, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(clear_bad_btn, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(clear_sel_bad_btn, 0, Qt.AlignmentFlag.AlignTop)
        list_layout.addLayout(title_row)

        self.accounts_list = QTableWidget()
        self.accounts_list.setColumnCount(6)
        self.accounts_list.setHorizontalHeaderLabels(["User ID", "Username", "Server Type", "Status", "Actions", "Delete"])
        header = self.accounts_list.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.accounts_list.setColumnWidth(1, 120)
        self.accounts_list.setColumnWidth(4, 90)
        self.accounts_list.setColumnWidth(5, 80)
        self.accounts_list.verticalHeader().setDefaultSectionSize(35)
        try:
            self.accounts_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.accounts_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        except Exception:
            pass
        self.accounts_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        list_layout.addWidget(self.accounts_list)

        main_layout.addWidget(list_widget)
        layout.addLayout(main_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(accounts_widget)
        self.accounts_tab_index = self.tab_widget.addTab(scroll, "Accounts")
        self.refresh_accounts_list()

    def setup_logs_tab(self):
        logs_widget = QWidget()
        layout = QVBoxLayout(logs_widget)

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFont(QFont("Consolas", 10))
        try:
            # Prevent long-run UI slowdowns from unbounded QTextDocument growth.
            self.log_display.document().setMaximumBlockCount(5000)
        except Exception:
            pass
        layout.addWidget(self.log_display)

        controls_layout = QHBoxLayout()

        clear_logs_btn = QPushButton("Clear Logs")
        clear_logs_btn.clicked.connect(self.clear_logs)
        controls_layout.addWidget(clear_logs_btn)

        save_logs_btn = QPushButton("Save Logs")
        save_logs_btn.clicked.connect(self.save_logs)
        controls_layout.addWidget(save_logs_btn)

        controls_layout.addStretch()
        
        self.watch_hit_chk = QCheckBox("WATCH-HIT/SCAN-TRACE logs", self)
        self.watch_hit_chk.setChecked(False)  # hidden by default
        controls_layout.addWidget(self.watch_hit_chk)

        self.scan_trace_chk = self.watch_hit_chk

        self.multiscope_biome_chk = QCheckBox("MultiScope BIOME logs", self)
        self.multiscope_biome_chk.setChecked(False)
        controls_layout.addWidget(self.multiscope_biome_chk)

        self.launch_debug_chk = QCheckBox("Launch Debug", self)
        self.launch_debug_chk.setChecked(False)  # hidden by default
        controls_layout.addWidget(self.launch_debug_chk)

        self.launch_gate_debug_chk = QCheckBox("Launch Gate Debug", self)
        self.launch_gate_debug_chk.setChecked(False)
        controls_layout.addWidget(self.launch_gate_debug_chk)
         
        self.auto_scroll_checkbox = QCheckBox("Auto-scroll")
        self.auto_scroll_checkbox.setChecked(True)
        self.log_autoscroll = bool(self.auto_scroll_checkbox.isChecked())
        self.auto_scroll_checkbox.toggled.connect(lambda checked: setattr(self, "log_autoscroll", bool(checked)))
        controls_layout.addWidget(self.auto_scroll_checkbox)

        layout.addLayout(controls_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(logs_widget)
        self.tab_widget.addTab(scroll, "Logs")

    def setup_ocr_tab(self):
        ocr_widget = QWidget()
        layout = QVBoxLayout(ocr_widget)

        # Show the OCR runtime device
        self.ocr_device_label = QLabel()
        self.ocr_device_label.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY}; font-weight: bold;")
        layout.addWidget(self.ocr_device_label)
        self._update_ocr_device_label()

        toggles_row = QHBoxLayout()
        self.ocr_enable_chk = QCheckBox("Enabled")
        self.ocr_enable_chk.toggled.connect(self._on_ocr_enabled_toggled)
        toggles_row.addWidget(self.ocr_enable_chk)
        toggles_row.addStretch()
        layout.addLayout(toggles_row)

        controls_group = QGroupBox("Capture | OCR")
        controls_form = QFormLayout(controls_group)

        self.ocr_workers_spin = QSpinBox(); self.ocr_workers_spin.setRange(1, 16)
        self.ocr_workers_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_max_caps_spin = QSpinBox(); self.ocr_max_caps_spin.setRange(1, 60)
        self.ocr_max_caps_spin.setToolTip("Maximum number of window captures per OCR batch.")
        self.ocr_max_caps_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_batch_delay_spin = QDoubleSpinBox(); self.ocr_batch_delay_spin.setRange(0.0, 60.0); self.ocr_batch_delay_spin.setDecimals(2); self.ocr_batch_delay_spin.setSingleStep(0.1); self.ocr_batch_delay_spin.setSuffix(" s")
        self.ocr_batch_delay_spin.setToolTip("Minimum delay between OCR capture batches (lower = more frequent batches).")
        self.ocr_batch_delay_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_preprocess_chk = QCheckBox("Use preprocessing")
        self.ocr_preprocess_chk.toggled.connect(self._on_ocr_settings_changed)
        self.ocr_frame_diff_tol_spin = QSpinBox(); self.ocr_frame_diff_tol_spin.setRange(0, 100); self.ocr_frame_diff_tol_spin.setSuffix(" %")
        self.ocr_frame_diff_tol_spin.setToolTip("Skip OCR when the chat frame changes by at most this amount compared to the previous frame.")
        self.ocr_frame_diff_tol_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_device_combo = QComboBox()
        self.ocr_device_combo.currentIndexChanged.connect(self._on_ocr_settings_changed)
        self._load_ocr_device_choices()

        controls_form.addRow("OCR workers:", self.ocr_workers_spin)
        controls_form.addRow("Max captures / batch:", self.ocr_max_caps_spin)
        controls_form.addRow("Batch delay:", self.ocr_batch_delay_spin)
        controls_form.addRow("Preprocess chat image:", self.ocr_preprocess_chk)
        controls_form.addRow("Processor:", self.ocr_device_combo)
        controls_form.addRow("Skip OCR if frame change ≤:", self.ocr_frame_diff_tol_spin)

        layout.addWidget(controls_group)

        btn_row = QHBoxLayout()
        calibrate_btn = QPushButton("Calibrate chat area")
        calibrate_btn.clicked.connect(self.calibrate_ocr_roi)
        calibrate_btn.setToolTip("Select 6 lines of roblox chat from the bottom")
        preview_btn = QPushButton("Preview chat")
        preview_btn.clicked.connect(self.show_ocr_preview)
        preview_btn.setToolTip("Preview OCR output using the calibrated chat area.")
        compare_btn = QPushButton("Chat frame compare")
        compare_btn.clicked.connect(self.test_ocr_frame_compare)
        compare_btn.setToolTip("Compare frame similarity using the calibrated chat area.")
        btn_row.addWidget(calibrate_btn)
        btn_row.addWidget(preview_btn)
        btn_row.addWidget(compare_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.ocr_roi_label = QLabel("Chat ROI: not calibrated")
        layout.addWidget(self.ocr_roi_label)

        filters_group = QGroupBox("Filters")
        filters_layout = QVBoxLayout(filters_group)
        self.ocr_filter_table = QTableWidget()
        self.ocr_filter_table.setColumnCount(7)
        self.ocr_filter_table.setHorizontalHeaderLabels(["Enabled", "Name", "R", "G", "B", "Tol", "Settings"])
        self.ocr_filter_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.ocr_filter_table.setMinimumHeight(380)
        self.ocr_filter_table.setShowGrid(False)
        self.ocr_filter_table.setAlternatingRowColors(False)
        header = self.ocr_filter_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.ocr_filter_table.setColumnWidth(0, 90)
        for col in range(2, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.ocr_filter_table.setColumnWidth(col, 95)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.ocr_filter_table.setColumnWidth(6, 120)
        vh = self.ocr_filter_table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(62)
        vh.setMinimumSectionSize(62)
        # Match the Auto Item "Items" table styling for a consistent look.
        self.ocr_filter_table.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {ModernStyle.SURFACE};
                gridline-color: transparent;
            }}

            QTableWidget::item {{
                border: none;
                padding: 0px;
            }}

            QTableWidget QLineEdit {{
                background-color: {ModernStyle.SURFACE};
                border: 2px solid {ModernStyle.BORDER};
                border-radius: 6px;
                padding: 6px 10px;
                min-height: 36px;
                color: {ModernStyle.TEXT_PRIMARY};
            }}

            QTableWidget QSpinBox {{
                background-color: {ModernStyle.SURFACE};
                border: 2px solid {ModernStyle.BORDER};
                border-radius: 6px;
                padding: 6px 30px 6px 10px; /* reserve space for arrows */
                min-height: 36px;
                color: {ModernStyle.TEXT_PRIMARY};
            }}

            QTableWidget QSpinBox::up-button, QTableWidget QSpinBox::down-button {{
                subcontrol-origin: border;
                width: 22px;
                border-left: 1px solid {ModernStyle.BORDER};
                background-color: {ModernStyle.SURFACE_VARIANT};
            }}
            QTableWidget QSpinBox::up-button {{
                subcontrol-position: top right;
                border-top-right-radius: 6px;
            }}
            QTableWidget QSpinBox::down-button {{
                subcontrol-position: bottom right;
                border-bottom-right-radius: 6px;
            }}
            QTableWidget QSpinBox::up-button:hover, QTableWidget QSpinBox::down-button:hover {{
                background-color: {ModernStyle.BORDER};
            }}

            QTableWidget QCheckBox {{
                padding: 0px;
                margin: 0px;
                background: transparent;
            }}

            QTableWidget QPushButton {{
                background-color: {ModernStyle.PRIMARY};
                color: {ModernStyle.TEXT_PRIMARY};
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                font-weight: 500;
                min-width: 80px;
                min-height: 28px;
            }}

            QTableWidget QPushButton:hover {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}

            QTableWidget QPushButton:disabled {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_SECONDARY};
            }}
            """
        )
        filters_layout.addWidget(self.ocr_filter_table)
        # Changes are wired from per-cell widgets (see _add_filter_row).

        filter_btns = QHBoxLayout()
        add_filter_btn = QPushButton("Add Filter")
        add_filter_btn.clicked.connect(lambda: (self._add_filter_row(), self._on_ocr_settings_changed()))
        remove_filter_btn = QPushButton("Remove Selected")
        remove_filter_btn.clicked.connect(lambda: (self._remove_selected_filter_rows(), self._on_ocr_settings_changed()))
        shared_areas_btn = QPushButton("Shared Areas")
        shared_areas_btn.clicked.connect(self._open_ocr_shared_areas_dialog)
        filter_users_btn = QPushButton("Filter Users")
        filter_users_btn.clicked.connect(self._open_ocr_filter_user_assignments_dialog)
        filter_presets_btn = QPushButton("Preset Filters")
        filter_presets_btn.clicked.connect(self._open_ocr_filter_presets_dialog)
        filter_btns.addWidget(add_filter_btn)
        filter_btns.addWidget(remove_filter_btn)
        filter_btns.addWidget(shared_areas_btn)
        filter_btns.addWidget(filter_users_btn)
        filter_btns.addWidget(filter_presets_btn)
        filter_btns.addStretch()
        filters_layout.addLayout(filter_btns)
        layout.addWidget(filters_group)

        self.ocr_status_label = QLabel("Status: Stopped")
        self.ocr_status_label.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY}; font-weight: bold;")
        layout.addWidget(self.ocr_status_label)

        self.ocr_log_text_chk = QCheckBox("OCR text (debug)")
        self.ocr_log_text_chk.setToolTip("Logs the raw OCR output text to the OCR log (can be spammy).")
        self.ocr_log_text_chk.toggled.connect(self._on_ocr_settings_changed)

        self.ocr_loop_logs_chk = QCheckBox("Show loop logs")
        self.ocr_loop_logs_chk.setToolTip("Show per-loop capture stats (messages like [Loop N] ...).")
        self.ocr_loop_logs_chk.toggled.connect(self._on_ocr_settings_changed)

        log_group = QGroupBox("OCR Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setSpacing(4)
        self.ocr_log_box = QTextEdit()
        self.ocr_log_box.setReadOnly(True)
        self.ocr_log_box.setFont(QFont("Consolas", 10))
        self.ocr_log_box.setMinimumHeight(260)
        try:
            self.ocr_log_box.document().setMaximumBlockCount(5000)
        except Exception:
            pass
        self.ocr_auto_scroll_chk = QCheckBox("Auto-scroll")
        self.ocr_auto_scroll_chk.setChecked(True)
        self.ocr_auto_scroll_chk.toggled.connect(lambda checked: setattr(self, "ocr_log_autoscroll", bool(checked)))
        clear_log_btn = QPushButton("Clear OCR Log")
        clear_log_btn.clicked.connect(self.clear_ocr_log)
        log_layout.addWidget(self.ocr_log_box)
        log_options_row = QHBoxLayout()
        log_options_row.setSpacing(8)
        log_options_row.addWidget(self.ocr_log_text_chk)
        log_options_row.addWidget(self.ocr_loop_logs_chk)
        log_options_row.addWidget(self.ocr_auto_scroll_chk)
        log_options_row.addStretch()
        log_options_row.addWidget(clear_log_btn)
        log_layout.addLayout(log_options_row)
        layout.addWidget(log_group)

        # Footer: reset OCR settings to defaults
        footer = QHBoxLayout()
        footer.addStretch()
        reset_ocr_btn = QPushButton("Restore OCR Defaults")
        reset_ocr_btn.clicked.connect(self._reset_ocr_to_defaults)
        footer.addWidget(reset_ocr_btn)
        layout.addLayout(footer)

        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(ocr_widget)
        self.tab_widget.addTab(scroll, "OCR")

    def setup_antiafk_tab(self):
        antiafk_widget = QWidget()
        layout = QVBoxLayout(antiafk_widget)

        info_group = QGroupBox("Roblox Anti-AFK")
        info_layout = QVBoxLayout(info_group)
        desc = QLabel(
            "Anti-AFK keeps your Roblox sessions active by periodically sending key presses.\n"
            "This works on all Roblox windows and may require administrator privileges."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        info_layout.addWidget(desc)
        layout.addWidget(info_group)

        settings_group = QGroupBox("Settings")
        settings_layout = QGridLayout(settings_group)

        # Interval + quick presets
        settings_layout.addWidget(QLabel("Action Interval (seconds):"), 0, 0)
        interval_row_widget = QWidget()
        interval_row = QHBoxLayout(interval_row_widget)
        interval_row.setContentsMargins(0, 0, 0, 0)
        self.antiafk_interval_spin = QSpinBox()
        self.antiafk_interval_spin.setRange(5, 3600)
        self.antiafk_interval_spin.setValue(120)
        interval_row.addWidget(self.antiafk_interval_spin)
        for seconds, label in ((120, "2m"), (300, "5m"), (600, "10m")):
            btn = QPushButton(label)
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _, s=seconds: self.antiafk_interval_spin.setValue(s))
            interval_row.addWidget(btn)
        interval_row.addStretch()
        settings_layout.addWidget(interval_row_widget, 0, 1)

        # Action type
        settings_layout.addWidget(QLabel("Action Type:"), 1, 0)
        self.antiafk_action_combo = QComboBox()
        self.antiafk_action_combo.addItems(["space", "ws", "zoom", "AutoReconnect"])
        settings_layout.addWidget(self.antiafk_action_combo, 1, 1)

        # Key delay (ms) for key holds / multi-key actions
        settings_layout.addWidget(QLabel("Key Delay (ms):"), 2, 0)
        self.antiafk_alt_delay_spin = QSpinBox()
        self.antiafk_alt_delay_spin.setRange(10, 5000)
        self.antiafk_alt_delay_spin.setSingleStep(10)
        self.antiafk_alt_delay_spin.setValue(400)
        settings_layout.addWidget(self.antiafk_alt_delay_spin, 2, 1)

        # Use AutoReconnect in main menu
        self.antiafk_menu_autoreconnect_chk = QCheckBox("Use AutoReconnect when in main menu")
        settings_layout.addWidget(self.antiafk_menu_autoreconnect_chk, 3, 0, 1, 2)

        layout.addWidget(settings_group)

        alerts_group = QGroupBox("Alerts")
        alerts_layout = QGridLayout(alerts_group)
        try:
            alerts_layout.setContentsMargins(10, 10, 10, 10)
            alerts_layout.setHorizontalSpacing(12)
            alerts_layout.setVerticalSpacing(6)
        except Exception:
            pass

        self.antiafk_alert_sound_chk = QCheckBox("Sound Alert")
        self.antiafk_alert_sound_chk.setToolTip("Play a sound before Anti-AFK actions.")
        alerts_layout.addWidget(self.antiafk_alert_sound_chk, 0, 0)

        lead_label = QLabel("Lead time (seconds):")
        lead_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        alerts_layout.addWidget(lead_label, 0, 1)

        self.antiafk_alert_tooltip_chk = QCheckBox("Message Alert")
        self.antiafk_alert_tooltip_chk.setToolTip("Show a tooltip in the center of the monitor before Anti-AFK actions.")
        alerts_layout.addWidget(self.antiafk_alert_tooltip_chk, 1, 0)

        self.antiafk_alert_lead_spin = QDoubleSpinBox()
        self.antiafk_alert_lead_spin.setRange(0.0, 60.0)
        self.antiafk_alert_lead_spin.setSingleStep(0.5)
        self.antiafk_alert_lead_spin.setDecimals(1)
        self.antiafk_alert_lead_spin.setValue(3.0)
        self.antiafk_alert_lead_spin.setToolTip("How many seconds before Anti-AFK to trigger the alert.")
        alerts_layout.addWidget(self.antiafk_alert_lead_spin, 1, 1)

        layout.addWidget(alerts_group)

        # BES integration: unthrottle targets shortly before sending Anti-AFK input.
        pacify_group = QGroupBox("Throttle Pacify (BES)")
        pacify_layout = QGridLayout(pacify_group)

        self.antiafk_unthrottle_chk = QCheckBox("Temporarily unthrottle before Anti-AFK actions")
        pacify_layout.addWidget(self.antiafk_unthrottle_chk, 0, 0, 1, 2)

        pacify_layout.addWidget(QLabel("Unthrottle lead time (seconds):"), 1, 0)
        self.antiafk_unthrottle_lead_spin = QDoubleSpinBox()
        self.antiafk_unthrottle_lead_spin.setRange(0.0, 15.0)
        self.antiafk_unthrottle_lead_spin.setSingleStep(0.5)
        self.antiafk_unthrottle_lead_spin.setDecimals(1)
        self.antiafk_unthrottle_lead_spin.setValue(3.0)
        pacify_layout.addWidget(self.antiafk_unthrottle_lead_spin, 1, 1)
        try:
            w = int(
                max(
                    self.antiafk_unthrottle_lead_spin.sizeHint().width(),
                    self.antiafk_unthrottle_lead_spin.minimumSizeHint().width(),
                )
            )
            if hasattr(self, "antiafk_alert_lead_spin") and self.antiafk_alert_lead_spin:
                self.antiafk_alert_lead_spin.setMinimumWidth(max(0, w))
        except Exception:
            pass

        pacify_layout.addWidget(QLabel("Unthrottle batch size (windows):"), 2, 0)
        self.antiafk_unthrottle_batch_spin = QSpinBox()
        self.antiafk_unthrottle_batch_spin.setRange(1, 50)
        self.antiafk_unthrottle_batch_spin.setValue(5)
        pacify_layout.addWidget(self.antiafk_unthrottle_batch_spin, 2, 1)

        hint = QLabel("When BES throttling is enabled, Anti-AFK will unthrottle targets in batches before acting.")
        hint.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        pacify_layout.addWidget(hint, 3, 0, 1, 2)

        layout.addWidget(pacify_group)

        # Status/log view specific to Anti-AFK
        status_group = QGroupBox("Anti-AFK Log")
        status_layout = QVBoxLayout(status_group)
        self.antiafk_status_box = QTextEdit()
        self.antiafk_status_box.setReadOnly(True)
        self.antiafk_status_box.setFont(QFont("Consolas", 10))
        try:
            self.antiafk_status_box.document().setMaximumBlockCount(5000)
        except Exception:
            pass
        status_layout.addWidget(self.antiafk_status_box)
        layout.addWidget(status_group)

        # Control row: enable toggle + actions + defaults
        btn_row = QHBoxLayout()
        self.antiafk_enable_chk = QCheckBox("Enable Anti-AFK (while manager is running)")
        btn_row.addWidget(self.antiafk_enable_chk)
        btn_row.addStretch()
        self.antiafk_test_btn = QPushButton("Test Action")
        self.antiafk_test_btn.clicked.connect(self._on_antiafk_test)
        self.antiafk_show_btn = QPushButton("Show Roblox")
        self.antiafk_show_btn.clicked.connect(self._on_antiafk_show)
        self.antiafk_hide_btn = QPushButton("Hide Roblox")
        self.antiafk_hide_btn.clicked.connect(self._on_antiafk_hide)
        reset_btn = QPushButton("Restore Anti-AFK Defaults")
        reset_btn.clicked.connect(self._reset_antiafk_to_defaults)
        btn_row.addWidget(self.antiafk_test_btn)
        btn_row.addWidget(self.antiafk_show_btn)
        btn_row.addWidget(self.antiafk_hide_btn)
        btn_row.addWidget(reset_btn)
        layout.addLayout(btn_row)

        layout.addStretch()

        # Wire up engine + config
        settings_data = self.config_manager.load_settings() or {}
        antiafk_cfg = settings_data.get("antiafk", {}) or {}
        self.antiafk = AntiAFK(parent=self, config=antiafk_cfg)

        # Connect callbacks so worker threads report via Qt signals
        self.antiafk.status_callback = self._emit_antiafk_status
        self.antiafk.button_state_callback = self._emit_antiafk_state
        self.antiafk.touch_callback = self._emit_antiafk_touch
        try:
            self.antiafk.pre_action_callback = self._emit_antiafk_pre_action
        except Exception:
            pass
        self.antiafk.is_pid_in_menu_callback = self._is_pid_in_menu
        self.antiafk.bes_hold_unthrottled_callback = self._antiafk_bes_hold_unthrottled
        self.antiafk.bes_release_hold_callback = self._antiafk_bes_release_hold

        # Apply config to UI without triggering change handlers
        self._loading_antiafk_settings = True
        try:
            cfg = self.antiafk.config or {}
            self.antiafk_interval_spin.setValue(int(cfg.get("antiafk_interval", 120)))
            self.antiafk_action_combo.setCurrentText(cfg.get("antiafk_action", "space"))
            self.antiafk_alt_delay_spin.setValue(int(cfg.get("antiafk_alt_delay_ms", 400)))
            self.antiafk_menu_autoreconnect_chk.setChecked(bool(cfg.get("antiafk_menu_autoreconnect", False)))
            self.antiafk_enable_chk.setChecked(bool(cfg.get("antiafk_enabled", False)))
            self.antiafk_alert_sound_chk.setChecked(bool(cfg.get("antiafk_alert_sound", False)))
            self.antiafk_alert_tooltip_chk.setChecked(bool(cfg.get("antiafk_alert_tooltip", False)))
            try:
                self.antiafk_alert_lead_spin.setValue(float(cfg.get("antiafk_alert_lead_s", 3.0) or 0.0))
            except Exception:
                self.antiafk_alert_lead_spin.setValue(3.0)
            self.antiafk_unthrottle_chk.setChecked(bool(cfg.get("antiafk_unthrottle_enabled", False)))
            try:
                self.antiafk_unthrottle_batch_spin.setValue(int(cfg.get("antiafk_unthrottle_batch_size", 5) or 5))
            except Exception:
                self.antiafk_unthrottle_batch_spin.setValue(5)
            try:
                self.antiafk_unthrottle_lead_spin.setValue(float(cfg.get("antiafk_unthrottle_lead_s", 3.0) or 0.0))
            except Exception:
                self.antiafk_unthrottle_lead_spin.setValue(3.0)
        finally:
            self._loading_antiafk_settings = False

        # React to UI changes
        self.antiafk_interval_spin.valueChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_action_combo.currentTextChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_alt_delay_spin.valueChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_menu_autoreconnect_chk.toggled.connect(self._on_antiafk_ui_changed)
        self.antiafk_alert_sound_chk.toggled.connect(self._on_antiafk_ui_changed)
        self.antiafk_alert_tooltip_chk.toggled.connect(self._on_antiafk_ui_changed)
        self.antiafk_alert_lead_spin.valueChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_unthrottle_chk.toggled.connect(self._on_antiafk_ui_changed)
        self.antiafk_unthrottle_batch_spin.valueChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_unthrottle_lead_spin.valueChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_enable_chk.toggled.connect(self._on_antiafk_ui_changed)

        # Ensure initial engine config (including multi-instance) is applied
        self._on_antiafk_ui_changed()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(antiafk_widget)
        self.tab_widget.addTab(scroll, "Anti AFK")

    def _emit_antiafk_status(self, message: str):
        """Called from AntiAFK worker threads to forward status messages."""
        self.antiafk_log_signal.emit(message)

    def _emit_antiafk_state(self, running: bool):
        """Called from AntiAFK worker threads to mirror running state."""
        self.antiafk_state_signal.emit(bool(running))

    def _emit_antiafk_touch(self, pid: int):
        """Called from AntiAFK worker threads when a PID is successfully touched."""
        try:
            self.antiafk_touch_signal.emit(int(pid))
        except Exception:
            pass

    def _emit_antiafk_pre_action(self, seconds_until_action: float) -> None:
        """Called from AntiAFK worker threads shortly before an action cycle starts."""
        try:
            self.antiafk_pre_action_signal.emit(float(seconds_until_action))
        except Exception:
            pass

    def _hide_antiafk_center_tooltip(self) -> None:
        w = getattr(self, "_antiafk_center_tooltip", None)
        if w is None:
            return
        try:
            w.hide()
        except Exception:
            pass

    def _show_antiafk_center_tooltip(self, message: str, duration_ms: int) -> None:
        try:
            duration_ms = int(duration_ms)
        except Exception:
            duration_ms = 1500
        duration_ms = max(250, min(duration_ms, 60_000))

        tip = getattr(self, "_antiafk_center_tooltip", None)
        if tip is None:
            tip = QLabel("", None)
            flags = (
                Qt.WindowType.ToolTip
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            transparent_for_input = getattr(Qt.WindowType, "WindowTransparentForInput", None)
            if transparent_for_input is not None:
                flags |= transparent_for_input
            tip.setWindowFlags(flags)
            tip.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            tip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            tip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            tip.setAutoFillBackground(True)
            try:
                tip.setWindowOpacity(0.60)
            except Exception:
                pass
            tip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tip.setMargin(12)
            tip.setStyleSheet(
                f"background-color:{ModernStyle.SURFACE_VARIANT};"
                f"color:{ModernStyle.TEXT_PRIMARY};"
                f"border:1px solid {ModernStyle.BORDER};"
                "border-radius:10px;"
                "font-weight:600;"
            )
            self._antiafk_center_tooltip = tip

        try:
            tip.setText(str(message))
            tip.adjustSize()
        except Exception:
            return

        screen = None
        try:
            screen = self.screen()
        except Exception:
            screen = None
        if screen is None:
            try:
                screen = QApplication.primaryScreen()
            except Exception:
                screen = None
        if screen is None:
            return

        try:
            geom = screen.availableGeometry()
            x = int(geom.x() + (geom.width() - tip.width()) / 2)
            y = int(geom.y() + (geom.height() - tip.height()) / 2)
            tip.move(x, y)
        except Exception:
            pass

        try:
            tip.show()
            tip.raise_()
        except Exception:
            pass

        t = getattr(self, "_antiafk_center_tooltip_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._hide_antiafk_center_tooltip)
            self._antiafk_center_tooltip_timer = t
        try:
            t.start(duration_ms)
        except Exception:
            pass

    def _hide_autoitem_center_tooltip(self) -> None:
        w = getattr(self, "_autoitem_center_tooltip", None)
        if w is None:
            return
        try:
            w.hide()
        except Exception:
            pass

        t = getattr(self, "_autoitem_center_tooltip_timer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass

    def _show_autoitem_center_tooltip(self, message: str, duration_ms: int) -> None:
        try:
            duration_ms = int(duration_ms)
        except Exception:
            duration_ms = 1500
        duration_ms = max(250, min(duration_ms, 600_000))

        tip = getattr(self, "_autoitem_center_tooltip", None)
        if tip is None:
            tip = QLabel("", None)
            flags = (
                Qt.WindowType.ToolTip
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            transparent_for_input = getattr(Qt.WindowType, "WindowTransparentForInput", None)
            if transparent_for_input is not None:
                flags |= transparent_for_input
            tip.setWindowFlags(flags)
            tip.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            tip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            tip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            tip.setAutoFillBackground(True)
            try:
                tip.setWindowOpacity(0.60)
            except Exception:
                pass
            tip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tip.setMargin(12)
            tip.setStyleSheet(
                f"background-color:{ModernStyle.SURFACE_VARIANT};"
                f"color:{ModernStyle.TEXT_PRIMARY};"
                f"border:1px solid {ModernStyle.BORDER};"
                "border-radius:10px;"
                "font-weight:600;"
            )
            self._autoitem_center_tooltip = tip

        try:
            tip.setText(str(message))
            tip.adjustSize()
        except Exception:
            return

        screen = None
        try:
            screen = self.screen()
        except Exception:
            screen = None
        if screen is None:
            try:
                screen = QApplication.primaryScreen()
            except Exception:
                screen = None
        if screen is None:
            return

        try:
            geom = screen.availableGeometry()
            x = int(geom.x() + (geom.width() - tip.width()) / 2)
            y = int(geom.y() + (geom.height() - tip.height()) / 2)
            tip.move(x, y)
        except Exception:
            pass

        try:
            tip.show()
            tip.raise_()
        except Exception:
            pass

        t = getattr(self, "_autoitem_center_tooltip_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._hide_autoitem_center_tooltip)
            self._autoitem_center_tooltip_timer = t
        try:
            t.start(duration_ms)
        except Exception:
            pass

    def _play_antiafk_alert_sound(self) -> None:
        alias = "Notification.Looping.Call"
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            try:
                winsound.PlaySound(str(alias), winsound.SND_ALIAS | winsound.SND_ASYNC)
                return
            except Exception:
                try:
                    winsound.MessageBeep(winsound.MB_OK)
                    return
                except Exception:
                    pass
        try:
            QApplication.beep()
        except Exception:
            pass

    def _replay_ocr_filter_alert_sound(self) -> None:
        if not bool(getattr(self, "_ocr_filter_alert_active", False)):
            return
        self._play_antiafk_alert_sound()

    def _start_ocr_filter_alert_sound(self) -> None:
        timer = getattr(self, "_ocr_filter_alert_loop_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(3000)
            timer.timeout.connect(self._replay_ocr_filter_alert_sound)
            self._ocr_filter_alert_loop_timer = timer

        try:
            timer.stop()
        except Exception:
            pass

        alias = "Notification.Looping.Call"
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            try:
                winsound.PlaySound(str(alias), winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_LOOP)
                return
            except Exception:
                pass

        self._replay_ocr_filter_alert_sound()
        try:
            timer.start()
        except Exception:
            pass

    def _stop_ocr_filter_alert_sound(self) -> None:
        timer = getattr(self, "_ocr_filter_alert_loop_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def _ensure_ocr_filter_alert_stop_window(self) -> QWidget:
        window = getattr(self, "_ocr_filter_alert_stop_window", None)
        if window is not None:
            return window

        window = OCRAlertStopDialog(self)
        window.stop_btn.clicked.connect(self._stop_ocr_filter_alert)
        self._ocr_filter_alert_stop_window = window
        self._ocr_filter_alert_stop_label = window.title_label
        return window

    def _show_ocr_filter_alert_stop_window(self, filter_name: str) -> None:
        window = self._ensure_ocr_filter_alert_stop_window()
        label = getattr(self, "_ocr_filter_alert_stop_label", None)
        filter_name_s = str(filter_name or "").strip() or "Filter"
        if label is not None:
            label.setText("Stop OCR Alert")
            label.setToolTip(f"Latest match: {filter_name_s}")
        try:
            window.setToolTip(f"Latest match: {filter_name_s}")
        except Exception:
            pass
        try:
            window.adjustSize()
        except Exception:
            pass

        try:
            anchor = self.mapToGlobal(QPoint(max(0, self.width() - window.width() - 24), 56))
            x = int(anchor.x())
            y = int(anchor.y())
            screen = None
            try:
                screen = QApplication.screenAt(anchor)
            except Exception:
                screen = None
            if screen is None:
                try:
                    handle = self.windowHandle()
                    if handle is not None:
                        screen = handle.screen()
                except Exception:
                    screen = None
            if screen is None:
                try:
                    screen = QApplication.primaryScreen()
                except Exception:
                    screen = None
            if screen is not None:
                available = screen.availableGeometry()
                x = max(available.left() + 8, min(x, available.right() - window.width() - 8))
                y = max(available.top() + 8, min(y, available.bottom() - window.height() - 8))
            window.move(x, y)
        except Exception:
            pass

        if window.isVisible():
            try:
                window.raise_()
                window.activateWindow()
            except Exception:
                pass

    def _open_ocr_filter_alert_stop_window(self, filter_name: str) -> None:
        if not bool(getattr(self, "_ocr_filter_alert_active", False)):
            return
        window = self._ensure_ocr_filter_alert_stop_window()
        self._show_ocr_filter_alert_stop_window(filter_name)
        try:
            window.show()
            window.raise_()
            window.activateWindow()
        except Exception:
            pass

    def _stop_ocr_filter_alert(self) -> None:
        self._ocr_filter_alert_active = False
        self._stop_ocr_filter_alert_sound()
        window = getattr(self, "_ocr_filter_alert_stop_window", None)
        self._ocr_filter_alert_stop_window = None
        self._ocr_filter_alert_stop_label = None
        if window is not None:
            try:
                window.done(0)
            except Exception:
                pass
            try:
                window.deleteLater()
            except Exception:
                pass

    def _start_ocr_filter_alert(self, filter_name: str) -> None:
        filter_name_s = str(filter_name or "").strip() or "Filter"
        already_active = bool(getattr(self, "_ocr_filter_alert_active", False))
        self._ocr_filter_alert_active = True
        if not already_active:
            self._open_ocr_filter_alert_stop_window(filter_name_s)
            self._start_ocr_filter_alert_sound()
            return
        self._show_ocr_filter_alert_stop_window(filter_name_s)

    def _handle_ocr_filter_alert(self, filter_name: str) -> None:
        self.ocr_filter_alert_ui_signal.emit(str(filter_name or "").strip() or "Filter")

    def _handle_ocr_filter_alert_ui(self, filter_name: str) -> None:
        self._start_ocr_filter_alert(filter_name)

    def _handle_ocr_filter_match_for_auto_actions(self, pid: int, filter_id: str, filter_name: str) -> None:
        engine = getattr(self, "auto_item_engine", None)
        if engine is None or not hasattr(engine, "record_ocr_filter_trigger"):
            return
        try:
            ctx = self._resolve_pid_context(int(pid))
        except Exception:
            ctx = {}
        uid = str((ctx or {}).get("user_id") or "").strip()
        if not uid:
            return
        try:
            engine.record_ocr_filter_trigger(uid, int(pid), str(filter_id or "").strip(), str(filter_name or "").strip())
        except Exception:
            pass

    def _on_antiafk_status(self, message: str):
        """Qt slot: update Anti-AFK tab and main log safely on the GUI thread."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"

        try:
            self._antiafk_log_queue.append(line)
        except Exception:
            box = getattr(self, "antiafk_status_box", None)
            if box is not None:
                try:
                    box.append(line)
                except Exception:
                    pass

        try:
            if not self._log_flush_timer:
                self._flush_antiafk_log_queue()
        except Exception:
            pass

    def _on_antiafk_state_changed(self, enabled: bool):
        """Qt slot: keep Anti-AFK buttons and inputs in sync with worker state."""
        enabled = bool(enabled)
        for w in (
            getattr(self, "antiafk_action_combo", None),
            getattr(self, "antiafk_alt_delay_spin", None),
            getattr(self, "antiafk_alert_sound_chk", None),
            getattr(self, "antiafk_alert_tooltip_chk", None),
            getattr(self, "antiafk_alert_lead_spin", None),
            getattr(self, "antiafk_unthrottle_chk", None),
            getattr(self, "antiafk_unthrottle_batch_spin", None),
            getattr(self, "antiafk_unthrottle_lead_spin", None),
        ):
            if w is not None:
                try:
                    w.setEnabled(not enabled)
                except Exception:
                    pass

    def _on_antiafk_touch(self, pid: int) -> None:
        """Qt slot: update per-user last-touch timestamps from Anti-AFK hits."""
        try:
            pid_i = int(pid)
        except Exception:
            return

        with self._antiafk_touch_lock:
            uid = self._antiafk_pid_to_uid.get(pid_i)
        if not uid:
            return

        runtime = (self.user_data or {}).get(str(uid), {}) or {}
        server = str(runtime.get("server", "") or "")
        if self._is_disconnected_server_label(server):
            return

        now_ts = time.time()
        with self._antiafk_touch_lock:
            self._antiafk_last_touch_by_uid[str(uid)] = float(now_ts)

    def _on_antiafk_pre_action(self, seconds_until_action: float) -> None:
        try:
            seconds = float(seconds_until_action or 0.0)
        except Exception:
            seconds = 0.0
        seconds = max(0.0, seconds)

        play_sound = bool(
            getattr(self, "antiafk_alert_sound_chk", None) and self.antiafk_alert_sound_chk.isChecked()
        )
        show_tooltip = bool(
            getattr(self, "antiafk_alert_tooltip_chk", None) and self.antiafk_alert_tooltip_chk.isChecked()
        )

        if play_sound:
            try:
                self._play_antiafk_alert_sound()
            except Exception:
                pass

        if show_tooltip:
            text = "Anti-AFK action now" if seconds <= 0.0 else f"Anti-AFK action in {seconds:.1f}s"
            self._show_antiafk_center_tooltip(text, int(max(1000.0, seconds * 1000.0)))

    def _run_antiafk_async(self, func_name: str, *args, **kwargs):
        antiafk = getattr(self, "antiafk", None)
        if not antiafk:
            return

        def _call():
            try:
                fn = getattr(antiafk, func_name, None)
                if callable(fn):
                    fn(*args, **kwargs)
            except Exception:
                pass

        try:
            runnable = _FunctionRunnable(_call)
            self._antiafk_thread_pool.start(runnable)
        except Exception:
            threading.Thread(target=_call, daemon=True).start()

    def _schedule_antiafk_save(self):
        try:
            self._antiafk_save_timer.start()
        except Exception:
            try:
                self._save_antiafk_settings()
            except Exception:
                pass

    def _on_antiafk_ui_changed(self):
        """Apply current Anti-AFK UI values to the engine and persist them."""
        if self._loading_antiafk_settings or not self.antiafk:
            return

        try:
            interval = int(self.antiafk_interval_spin.value())
            action = self.antiafk_action_combo.currentText()
            alt_delay_ms = int(self.antiafk_alt_delay_spin.value())
            menu_autoreconnect = bool(self.antiafk_menu_autoreconnect_chk.isChecked())
            unthrottle_enabled = bool(getattr(self, "antiafk_unthrottle_chk", None) and self.antiafk_unthrottle_chk.isChecked())
            unthrottle_batch_size = int(getattr(self, "antiafk_unthrottle_batch_spin", None) and self.antiafk_unthrottle_batch_spin.value() or 5)
            unthrottle_lead_s = float(getattr(self, "antiafk_unthrottle_lead_spin", None) and self.antiafk_unthrottle_lead_spin.value() or 0.0)
            alert_sound_enabled = bool(getattr(self, "antiafk_alert_sound_chk", None) and self.antiafk_alert_sound_chk.isChecked())
            alert_tooltip_enabled = bool(getattr(self, "antiafk_alert_tooltip_chk", None) and self.antiafk_alert_tooltip_chk.isChecked())
            alert_lead_s = float(getattr(self, "antiafk_alert_lead_spin", None) and self.antiafk_alert_lead_spin.value() or 0.0)
            enabled_flag = bool(self.antiafk_enable_chk.isChecked())

            # Push new settings into the AntiAFK engine so behavior changes immediately.
            self.antiafk.apply_host_config(
                interval=interval,
                action=action,
                alt_delay_ms=alt_delay_ms,
                menu_autoreconnect=menu_autoreconnect,
                alert_sound_enabled=alert_sound_enabled,
                alert_tooltip_enabled=alert_tooltip_enabled,
                alert_lead_s=alert_lead_s,
                unthrottle_enabled=unthrottle_enabled,
                unthrottle_batch_size=unthrottle_batch_size,
                unthrottle_lead_s=unthrottle_lead_s,
            )

            # If the manager is running, keep Anti-AFK running state in sync with the toggle.
            if self._is_manager_running():
                self._run_antiafk_async("toggle_antiafk", enabled_flag)

            # Persist Anti-AFK settings to disk so they survive relaunch.
            self._schedule_antiafk_save()
        except Exception:
            # AntiAFK will log detailed errors via its own log_error method.
            pass

    def _save_antiafk_settings(self):
        """Persist Anti-AFK-related settings into settings.json."""
        try:
            settings = self.config_manager.load_settings()
        except Exception:
            settings = self.config_manager.default_settings.copy()

        antiafk_cfg = settings.get("antiafk", {}) or {}
        try:
            antiafk_cfg["antiafk_interval"] = int(self.antiafk_interval_spin.value())
            antiafk_cfg["antiafk_action"] = self.antiafk_action_combo.currentText()
            antiafk_cfg["antiafk_alt_delay_ms"] = int(self.antiafk_alt_delay_spin.value())
            antiafk_cfg["antiafk_menu_autoreconnect"] = bool(self.antiafk_menu_autoreconnect_chk.isChecked())
            antiafk_cfg["antiafk_enabled"] = bool(self.antiafk_enable_chk.isChecked())
            antiafk_cfg["antiafk_alert_sound"] = bool(
                getattr(self, "antiafk_alert_sound_chk", None) and self.antiafk_alert_sound_chk.isChecked()
            )
            antiafk_cfg["antiafk_alert_tooltip"] = bool(
                getattr(self, "antiafk_alert_tooltip_chk", None) and self.antiafk_alert_tooltip_chk.isChecked()
            )
            antiafk_cfg["antiafk_alert_lead_s"] = float(
                getattr(self, "antiafk_alert_lead_spin", None) and self.antiafk_alert_lead_spin.value() or 0.0
            )
            antiafk_cfg["antiafk_unthrottle_enabled"] = bool(
                getattr(self, "antiafk_unthrottle_chk", None) and self.antiafk_unthrottle_chk.isChecked()
            )
            antiafk_cfg["antiafk_unthrottle_batch_size"] = int(
                getattr(self, "antiafk_unthrottle_batch_spin", None) and self.antiafk_unthrottle_batch_spin.value() or 5
            )
            antiafk_cfg["antiafk_unthrottle_lead_s"] = float(
                getattr(self, "antiafk_unthrottle_lead_spin", None) and self.antiafk_unthrottle_lead_spin.value() or 0.0
            )
            antiafk_cfg.pop("antiafk_user_safe", None)
            antiafk_cfg.pop("antiafk_sequential_mode", None)
            antiafk_cfg.pop("antiafk_sequential_delay", None)
        except Exception:
            # If widgets are missing or invalid, keep whatever was already stored.
            pass

        settings["antiafk"] = antiafk_cfg
        try:
            self.config_manager.save_settings(settings)
        except Exception:
            pass

    def _reset_antiafk_to_defaults(self):
        """Reset only the Anti-AFK tab to its default config and live-apply."""
        defaults = self.config_manager.default_settings.get("antiafk", {}) or {}
        self._loading_antiafk_settings = True
        try:
            self.antiafk_interval_spin.setValue(int(defaults.get("antiafk_interval", 120)))
            self.antiafk_action_combo.setCurrentText(defaults.get("antiafk_action", "space"))
            self.antiafk_alt_delay_spin.setValue(int(defaults.get("antiafk_alt_delay_ms", 400)))
            self.antiafk_menu_autoreconnect_chk.setChecked(bool(defaults.get("antiafk_menu_autoreconnect", False)))
            self.antiafk_enable_chk.setChecked(bool(defaults.get("antiafk_enabled", False)))
            self.antiafk_alert_sound_chk.setChecked(bool(defaults.get("antiafk_alert_sound", False)))
            self.antiafk_alert_tooltip_chk.setChecked(bool(defaults.get("antiafk_alert_tooltip", False)))
            try:
                self.antiafk_alert_lead_spin.setValue(float(defaults.get("antiafk_alert_lead_s", 3.0) or 0.0))
            except Exception:
                self.antiafk_alert_lead_spin.setValue(3.0)
            self.antiafk_unthrottle_chk.setChecked(bool(defaults.get("antiafk_unthrottle_enabled", False)))
            try:
                self.antiafk_unthrottle_batch_spin.setValue(int(defaults.get("antiafk_unthrottle_batch_size", 5) or 5))
            except Exception:
                self.antiafk_unthrottle_batch_spin.setValue(5)
            try:
                self.antiafk_unthrottle_lead_spin.setValue(float(defaults.get("antiafk_unthrottle_lead_s", 3.0) or 0.0))
            except Exception:
                self.antiafk_unthrottle_lead_spin.setValue(3.0)
        finally:
            self._loading_antiafk_settings = False

        # Apply + persist + sync running state
        self._on_antiafk_ui_changed()

    def _on_antiafk_start(self):
        """Start the Anti-AFK loop with current settings."""
        if not self.antiafk:
            return
        # Ensure engine has latest values and they are saved
        self._on_antiafk_ui_changed()
        self._run_antiafk_async("toggle_antiafk", True)

    def _on_antiafk_stop(self):
        """Stop the Anti-AFK loop."""
        if not self.antiafk:
            return
        self._run_antiafk_async("toggle_antiafk", False)
        self._schedule_antiafk_save()

    def _is_pid_in_menu(self, pid: int):
        """
        Best-effort check: return True if the Roblox PID appears to be in the main menu
        according to MultiScope's last known state, False if explicitly not, or None
        if unknown.
        """
        try:
            pid = int(pid)
        except Exception:
            return None

        # Need a running worker + multiscope engine
        wt = getattr(self, "worker_thread", None)
        if not wt or not wt.isRunning():
            return None

        try:
            manager = wt.manager
            tracker = manager.process_tracker
        except Exception:
            return None

        uid = tracker.process_owners.get(pid)
        if not uid:
            return None

        server_label = tracker.user_server.get(uid, "")
        if not server_label:
            return None

        ms = getattr(wt, "ms", None)
        if not ms:
            return None

        try:
            rows = ms.snapshot()
        except Exception:
            return None

        for row in rows:
            if row.get("server", "") == server_label:
                val = row.get("in_menu")
                # Treat unknown as in-menu to avoid misclassification.
                if val is None:
                    return True
                return bool(val)

        return None

    def _antiafk_bes_hold_unthrottled(self, pids, seconds) -> bool:
        ctl = getattr(self, "bes_controller", None)
        if ctl is None:
            return False

        try:
            with self._bes_cfg_lock:
                cfg = dict(self._bes_cfg_cache or {})
        except Exception:
            cfg = {}

        if not bool(cfg.get("enabled", False)):
            return False

        try:
            hold_s = max(0.0, float(seconds or 0.0))
        except Exception:
            hold_s = 0.0

        ok_any = False
        for pid in (pids or []):
            try:
                pid_i = int(pid)
            except Exception:
                continue
            if pid_i <= 0:
                continue
            try:
                ctl.hold_unthrottled(pid_i, float(hold_s))
                ok_any = True
            except Exception:
                continue

        return ok_any

    def _antiafk_bes_release_hold(self, pids) -> bool:
        ctl = getattr(self, "bes_controller", None)
        if ctl is None:
            return False

        try:
            with self._bes_cfg_lock:
                cfg = dict(self._bes_cfg_cache or {})
        except Exception:
            cfg = {}

        if not bool(cfg.get("enabled", False)):
            return False

        ok_any = False
        for pid in (pids or []):
            try:
                pid_i = int(pid)
            except Exception:
                continue
            if pid_i <= 0:
                continue
            try:
                ctl.release_hold(pid_i)
                ok_any = True
            except Exception:
                continue

        return ok_any

    def _on_antiafk_show(self):
        """Show all Roblox windows detected by the Anti-AFK engine."""
        if self.antiafk:
            self.antiafk.show_roblox_windows()

    def _on_antiafk_hide(self):
        """Hide all visible Roblox windows detected by the Anti-AFK engine."""
        if self.antiafk:
            self.antiafk.hide_roblox_windows()

    def _on_antiafk_test(self):
        """Run the built-in AntiAFK test_action_with_delay helper."""
        if not self.antiafk:
            return
        # Delegate completely to AntiAFK's own test helper.
        self.antiafk.test_action_with_delay()

    # ------------------------
    # Auto Item tab + engine
    # ------------------------

    def setup_auto_item_tab(self):
        auto_widget = QWidget()
        layout = QVBoxLayout(auto_widget)

        info_group = QGroupBox("Auto Item")
        info_layout = QVBoxLayout(info_group)
        desc = QLabel(
            "Automatically uses configured items for selected users.\n"
            "Coordinates are stored relative to each Roblox window (client area)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        info_layout.addWidget(desc)
        layout.addWidget(info_group)

        settings_group = QGroupBox("Settings")
        settings_layout = QGridLayout(settings_group)

        self.auto_item_enable_chk = QCheckBox("Enable Auto-Item (while manager is running)")
        settings_layout.addWidget(self.auto_item_enable_chk, 0, 0, 1, 2)

        settings_layout.addWidget(QLabel("Tick interval (seconds):"), 1, 0)
        self.auto_item_tick_spin = QDoubleSpinBox()
        self.auto_item_tick_spin.setRange(0.2, 60.0)
        self.auto_item_tick_spin.setDecimals(2)
        self.auto_item_tick_spin.setSingleStep(0.1)
        self.auto_item_tick_spin.setValue(1.0)
        settings_layout.addWidget(self.auto_item_tick_spin, 1, 1)

        settings_layout.addWidget(QLabel("Click/paste delay (seconds):"), 2, 0)
        self.auto_item_delay_spin = QDoubleSpinBox()
        self.auto_item_delay_spin.setRange(0.01, 2.0)
        self.auto_item_delay_spin.setDecimals(2)
        self.auto_item_delay_spin.setSingleStep(0.05)
        self.auto_item_delay_spin.setValue(0.2)
        settings_layout.addWidget(self.auto_item_delay_spin, 2, 1)

        self.auto_item_disable_mouse_chk = QCheckBox("Disable user mouse movement during Auto-Item")
        settings_layout.addWidget(self.auto_item_disable_mouse_chk, 3, 0, 1, 2)
        try:
            self._update_auto_item_disable_mouse_tooltip()
        except Exception:
            pass

        settings_layout.addWidget(QLabel("Toggle hotkey:"), 4, 0)
        self.auto_item_hotkey_edit = QKeySequenceEdit()
        self.auto_item_hotkey_edit.setToolTip("Global hotkey to toggle Auto-Item enable/disable (default: Ctrl+Alt+Space).")
        try:
            self.auto_item_hotkey_edit.setKeySequence(QKeySequence("Ctrl+Alt+Space"))
        except Exception:
            pass
        settings_layout.addWidget(self.auto_item_hotkey_edit, 4, 1)

        self.auto_item_test_btn = QPushButton("Test Auto-Item (first selected user)")
        self.auto_item_test_btn.setToolTip("Runs the configured automation once on the first selected user window.")
        self.auto_item_test_btn.clicked.connect(self._auto_item_test_once)
        settings_layout.addWidget(self.auto_item_test_btn, 5, 0, 1, 2)

        layout.addWidget(settings_group)

        coords_group = QGroupBox("Coordinates")
        coords_layout = QGridLayout(coords_group)

        self._auto_item_coord_edits = {}
        self._auto_item_coords = {}

        def _add_coord_row(row: int, key: str, label: str):
            coords_layout.addWidget(QLabel(label + ":"), row, 0)
            le = QLineEdit()
            le.setReadOnly(True)
            le.setPlaceholderText("not set")
            coords_layout.addWidget(le, row, 1)
            btn = QPushButton("Capture")
            btn.clicked.connect(lambda _, k=key: self._auto_item_capture_coord(k))
            coords_layout.addWidget(btn, row, 2)
            self._auto_item_coord_edits[key] = le

        _add_coord_row(0, "inv_button", "Inventory button")
        _add_coord_row(1, "items_tab", "Items tab")
        _add_coord_row(2, "search_box", "Search box")
        _add_coord_row(3, "query_pos", "Query/result click")
        _add_coord_row(4, "amount_box", "Amount box")
        _add_coord_row(5, "use_button", "Use button")
        _add_coord_row(6, "close_button", "Close button")

        # Conditional click
        self.auto_item_cond_enable_chk = QCheckBox("Enable conditional click (pixel color match)")
        coords_layout.addWidget(self.auto_item_cond_enable_chk, 7, 0, 1, 3)

        coords_layout.addWidget(QLabel("Conditional point:"), 8, 0)
        self.auto_item_cond_point_le = QLineEdit()
        self.auto_item_cond_point_le.setReadOnly(True)
        self.auto_item_cond_point_le.setPlaceholderText("not set")
        coords_layout.addWidget(self.auto_item_cond_point_le, 8, 1)
        self.auto_item_cond_capture_btn = QPushButton("Capture + Sample Color")
        self.auto_item_cond_capture_btn.clicked.connect(lambda: self._auto_item_capture_coord("conditional_point", sample_color=True))
        coords_layout.addWidget(self.auto_item_cond_capture_btn, 8, 2)

        coords_layout.addWidget(QLabel("Expected color (#RRGGBB):"), 9, 0)
        self.auto_item_cond_color_le = QLineEdit("#FFFFFF")
        coords_layout.addWidget(self.auto_item_cond_color_le, 9, 1)
        coords_layout.addWidget(QLabel("Tolerance:"), 9, 2)
        self.auto_item_cond_tol_spin = QSpinBox()
        self.auto_item_cond_tol_spin.setRange(0, 255)
        self.auto_item_cond_tol_spin.setValue(10)
        coords_layout.addWidget(self.auto_item_cond_tol_spin, 9, 3)

        hint = QLabel("Capture takes a screenshot of a Roblox window; click the screenshot to set the coordinate.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        coords_layout.addWidget(hint, 10, 0, 1, 4)

        layout.addWidget(coords_group)

        items_group = QGroupBox("Items (order matters)")
        items_layout = QVBoxLayout(items_group)

        self.auto_item_table = QTableWidget()
        self.auto_item_table.setColumnCount(7)
        self.auto_item_table.setHorizontalHeaderLabels(["Enabled", "Item", "Amount", "Cooldown (s)", "Biomes", "Users", "Alert"])
        self.auto_item_table.setShowGrid(False)
        self.auto_item_table.setAlternatingRowColors(False)
        header = self.auto_item_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        vh = self.auto_item_table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(62)
        self.auto_item_table.setColumnWidth(0, 80)
        self.auto_item_table.setColumnWidth(2, 95)
        self.auto_item_table.setColumnWidth(3, 120)
        self.auto_item_table.setColumnWidth(4, 120)
        self.auto_item_table.setColumnWidth(5, 140)
        self.auto_item_table.setColumnWidth(6, 140)
        # Hide per-item alert controls unless unlocked (JARAM.biu / JARAM_UNLOCK).
        try:
            self.auto_item_table.setColumnHidden(6, not _bm_relaxed())
        except Exception:
            pass
        # Compact + consistent controls inside the table (avoid clipped borders inside cells).
        self.auto_item_table.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {ModernStyle.SURFACE};
                gridline-color: transparent;
            }}

            QTableWidget::item {{
                border: none;
                padding: 0px;
            }}

            QTableWidget QLineEdit {{
                background-color: {ModernStyle.SURFACE};
                border: 2px solid {ModernStyle.BORDER};
                border-radius: 6px;
                padding: 6px 10px;
                min-height: 36px;
                color: {ModernStyle.TEXT_PRIMARY};
            }}

            QTableWidget QSpinBox {{
                background-color: {ModernStyle.SURFACE};
                border: 2px solid {ModernStyle.BORDER};
                border-radius: 6px;
                padding: 6px 30px 6px 10px; /* reserve space for arrows */
                min-height: 36px;
                color: {ModernStyle.TEXT_PRIMARY};
            }}

            QTableWidget QSpinBox::up-button, QTableWidget QSpinBox::down-button {{
                subcontrol-origin: border;
                width: 22px;
                border-left: 1px solid {ModernStyle.BORDER};
                background-color: {ModernStyle.SURFACE_VARIANT};
            }}
            QTableWidget QSpinBox::up-button {{
                subcontrol-position: top right;
                border-top-right-radius: 6px;
            }}
            QTableWidget QSpinBox::down-button {{
                subcontrol-position: bottom right;
                border-bottom-right-radius: 6px;
            }}
            QTableWidget QSpinBox::up-button:hover, QTableWidget QSpinBox::down-button:hover {{
                background-color: {ModernStyle.BORDER};
            }}

            QTableWidget QPushButton {{
                background-color: {ModernStyle.PRIMARY};
                color: {ModernStyle.TEXT_PRIMARY};
                border: 1px solid {ModernStyle.PRIMARY_VARIANT};
                border-bottom: 3px solid {ModernStyle.PRIMARY_VARIANT};
                padding: 6px 12px;
                border-radius: 6px;
                min-height: 36px;
                min-width: 0px;
                font-weight: 600;
                text-align: left;
            }}

            QTableWidget QPushButton:hover {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}

            QTableWidget QPushButton:pressed {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
                border-bottom: 1px solid {ModernStyle.PRIMARY_VARIANT};
                padding-top: 7px;
                padding-bottom: 5px;
            }}

            QTableWidget QCheckBox {{
                padding: 0px;
                margin: 0px;
                background: transparent;
            }}
            """
        )
        items_layout.addWidget(self.auto_item_table)

        items_btn_row = QHBoxLayout()
        add_item_btn = QPushButton("Add Item")
        add_item_btn.clicked.connect(self._auto_item_add_item)
        remove_item_btn = QPushButton("Remove Selected")
        remove_item_btn.clicked.connect(self._auto_item_remove_selected_items)
        up_btn = QPushButton("Move Up")
        up_btn.clicked.connect(lambda: self._auto_item_move_selected(-1))
        down_btn = QPushButton("Move Down")
        down_btn.clicked.connect(lambda: self._auto_item_move_selected(1))
        items_btn_row.addWidget(add_item_btn)
        items_btn_row.addWidget(remove_item_btn)
        items_btn_row.addWidget(up_btn)
        items_btn_row.addWidget(down_btn)
        items_btn_row.addStretch()
        items_layout.addLayout(items_btn_row)

        layout.addWidget(items_group)

        users_group = QGroupBox("Users")
        users_layout = QVBoxLayout(users_group)

        users_btn_row = QHBoxLayout()
        refresh_users_btn = QPushButton("Refresh List")
        refresh_users_btn.clicked.connect(self._auto_item_refresh_users)
        sel_all_btn = QPushButton("Select All")
        sel_all_btn.clicked.connect(lambda: self._auto_item_set_all_users(True))
        sel_none_btn = QPushButton("Select None")
        sel_none_btn.clicked.connect(lambda: self._auto_item_set_all_users(False))
        users_btn_row.addWidget(refresh_users_btn)
        users_btn_row.addWidget(sel_all_btn)
        users_btn_row.addWidget(sel_none_btn)
        users_btn_row.addStretch()
        users_layout.addLayout(users_btn_row)

        self.auto_item_users_container = QWidget()
        self.auto_item_users_vbox = QVBoxLayout(self.auto_item_users_container)
        self.auto_item_users_vbox.setContentsMargins(0, 0, 0, 0)
        self.auto_item_users_vbox.setSpacing(4)
        self.auto_item_user_checks = {}

        users_scroll = QScrollArea()
        users_scroll.setWidgetResizable(True)
        users_scroll.setWidget(self.auto_item_users_container)
        users_scroll.setMinimumHeight(180)
        users_layout.addWidget(users_scroll)

        layout.addWidget(users_group)

        log_group = QGroupBox("Auto-Item Log")
        log_layout = QVBoxLayout(log_group)
        self.autoitem_status_box = QTextEdit()
        self.autoitem_status_box.setReadOnly(True)
        self.autoitem_status_box.setFont(QFont("Consolas", 10))
        try:
            self.autoitem_status_box.document().setMaximumBlockCount(5000)
        except Exception:
            pass
        log_layout.addWidget(self.autoitem_status_box)
        layout.addWidget(log_group)

        footer = QHBoxLayout()
        footer.addStretch()
        reset_btn = QPushButton("Restore Auto-Item Defaults")
        reset_btn.clicked.connect(self._reset_auto_item_to_defaults)
        footer.addWidget(reset_btn)
        layout.addLayout(footer)

        layout.addStretch()

        # Debounced persistence (avoid writing settings.json every keystroke)
        self._auto_item_save_timer = QTimer(self)
        self._auto_item_save_timer.setSingleShot(True)
        self._auto_item_save_timer.timeout.connect(self._save_auto_item_settings)

        # Wire change events
        self.auto_item_enable_chk.toggled.connect(self._on_auto_item_enabled_toggled)
        self.auto_item_tick_spin.valueChanged.connect(self._on_auto_item_ui_changed)
        self.auto_item_delay_spin.valueChanged.connect(self._on_auto_item_ui_changed)
        self.auto_item_disable_mouse_chk.toggled.connect(self._on_auto_item_mouse_toggle_changed)
        self.auto_item_hotkey_edit.keySequenceChanged.connect(self._on_auto_item_hotkey_changed)
        self.auto_item_cond_enable_chk.toggled.connect(self._on_auto_item_ui_changed)
        self.auto_item_cond_color_le.textChanged.connect(self._on_auto_item_ui_changed)
        self.auto_item_cond_tol_spin.valueChanged.connect(self._on_auto_item_ui_changed)

        # Load persisted config + populate user list and table
        self._auto_item_refresh_users()
        self._load_auto_item_settings()

        # Ensure engine exists (but it will idle until enabled + fully configured)
        self._ensure_auto_item_engine()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(auto_widget)
        self.tab_widget.addTab(scroll, "Auto Item")

    def _on_autoitem_status(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"

        try:
            self._autoitem_log_queue.append(line)
        except Exception:
            box = getattr(self, "autoitem_status_box", None)
            if box is not None:
                try:
                    box.append(line)
                except Exception:
                    pass

        try:
            if not self._log_flush_timer:
                self._flush_autoitem_log_queue()
        except Exception:
            pass

    def _on_autoitem_mouse_block(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled:
            self._show_autoitem_center_tooltip("User mouse movement is disabled during Auto-Actions.", 600_000)
        else:
            self._hide_autoitem_center_tooltip()

    def _ensure_auto_item_engine(self):
        if self.auto_item_engine is not None or AutoItemEngine is None:
            return

        def _pid_provider(uid: str) -> Optional[int]:
            try:
                if not self._is_manager_running():
                    return None
                runtime = (self.user_data or {}).get(uid, {}) or {}
                pids = runtime.get("pids", []) or []
                if not isinstance(pids, (list, tuple)):
                    pids = [pids]
                if not pids:
                    return None
                return int(pids[0])
            except Exception:
                return None

        def _hwnd_provider(pid: int) -> Optional[int]:
            try:
                now = time.time()
                with self._auto_item_hwnd_cache_lock:
                    if (now - float(self._auto_item_hwnd_cache_ts)) > 1.0 or not self._auto_item_hwnd_cache:
                        self._auto_item_hwnd_cache = {int(w.pid): int(w.hwnd) for w in enum_roblox_windows()}
                        self._auto_item_hwnd_cache_ts = now
                    return self._auto_item_hwnd_cache.get(int(pid))
            except Exception:
                return None

        def _biome_provider(uid: str) -> str:
            try:
                runtime = (self.user_data or {}).get(uid, {}) or {}
                server = str(runtime.get("server", "") or "").strip()
                if not server:
                    return ""
                with self._ms_biome_lock:
                    uid_key = str(uid).strip()
                    if uid_key and uid_key in (self._ms_biome_by_uid or {}):
                        return str(self._ms_biome_by_uid.get(uid_key, "") or "")
                    return str(self._ms_biome_by_server.get(server, "") or "")
            except Exception:
                return ""

        def _in_menu_provider(uid: str) -> Optional[bool]:
            try:
                runtime = (self.user_data or {}).get(uid, {}) or {}
                server = str(runtime.get("server", "") or "").strip()
                if not server:
                    return None
                with self._ms_biome_lock:
                    uid_key = str(uid).strip()
                    if uid_key and uid_key in (self._ms_in_menu_by_uid or {}):
                        val = self._ms_in_menu_by_uid.get(uid_key, None)
                        if val is None:
                            return None
                        return bool(val)
                    if server not in (self._ms_in_menu_by_server or {}):
                        return None
                    val = self._ms_in_menu_by_server.get(server, None)
                    if val is None:
                        return None
                    return bool(val)
            except Exception:
                return None

        def _username_provider(uid: str) -> str:
            try:
                fn = getattr(self, "get_username_for_user", None)
                if callable(fn):
                    return str(fn(str(uid)) or "").strip() or str(uid)
            except Exception:
                pass
            try:
                users = self.config_manager.load_users() or {}
                info = users.get(str(uid), {}) or {}
                return str(info.get("username") or uid)
            except Exception:
                return str(uid)

        def _server_label_provider(uid: str) -> str:
            try:
                runtime = (self.user_data or {}).get(uid, {}) or {}
                return str(runtime.get("server", "") or "").strip()
            except Exception:
                return ""

        def _ps_link_provider(uid: str) -> str:
            try:
                fn = getattr(self, "get_ps_link_for_user", None)
                if callable(fn):
                    return str(fn(str(uid)) or "").strip()
            except Exception:
                pass
            try:
                users = self.config_manager.peek_users() or {}
                info = users.get(str(uid), {}) or {}
                return str(info.get("private_server_link") or "").strip()
            except Exception:
                return ""

        self.auto_item_engine = AutoItemEngine(
            pid_provider=_pid_provider,
            hwnd_provider=_hwnd_provider,
            biome_provider=_biome_provider,
            in_menu_provider=_in_menu_provider,
            username_provider=_username_provider,
            server_label_provider=_server_label_provider,
            ps_link_provider=_ps_link_provider,
            log=self.autoitem_log_signal.emit,
            mouse_block_notify=self.autoitem_mouse_block_signal.emit,
            pause_antiafk=self._auto_item_pause_antiafk,
            resume_antiafk=self._auto_item_resume_antiafk,
            antiafk_overdue_within_provider=self._auto_item_is_antiafk_overdue_within,
            pre_action_hook=self._auto_item_pre_action_hook,
            post_action_hook=self._auto_item_post_action_hook,
        )
        try:
            self.auto_item_engine.start()
        except Exception:
            pass

        # Push initial config snapshot
        try:
            self.auto_item_engine.update_config(self._get_auto_item_settings_from_ui())
        except Exception:
            pass

    def _auto_item_is_antiafk_overdue_within(self, within_s: float) -> bool:
        """
        True when at least one connected user is overdue (or will be overdue soon) for an Anti-AFK touch.

        within_s=0  => already overdue (>=10m since last touch)
        within_s=45 => within 45s of becoming overdue (>= 9m15s since last touch)
        """
        antiafk = getattr(self, "antiafk", None)
        if not antiafk:
            return False

        # Only consider "overdue" when Anti-AFK is actually running/enabled.
        try:
            if not (
                self._is_manager_running()
                and bool(getattr(self, "antiafk_enable_chk", None) and self.antiafk_enable_chk.isChecked())
                and bool(getattr(antiafk, "antiafk_running", False))
            ):
                return False
        except Exception:
            return False

        try:
            window = max(0.0, float(within_s or 0.0))
        except Exception:
            window = 0.0

        overdue_age_s = 600.0
        cutoff_age = overdue_age_s - window
        if cutoff_age < 0.0:
            cutoff_age = 0.0

        try:
            now_ts = time.time()
            for uid, runtime in (self.user_data or {}).items():
                runtime = runtime or {}
                server = str(runtime.get("server", "") or "")
                if self._is_disconnected_server_label(server):
                    continue
                pids = runtime.get("pids", []) or []
                if not isinstance(pids, (list, tuple, set)):
                    pids = [pids]
                has_pid = False
                for p in (pids or []):
                    try:
                        if int(p) > 0:
                            has_pid = True
                            break
                    except Exception:
                        continue
                if not has_pid:
                    continue

                with self._antiafk_touch_lock:
                    last_ts = self._antiafk_last_touch_by_uid.get(str(uid))
                if last_ts is None:
                    continue
                if (now_ts - float(last_ts)) >= float(cutoff_age):
                    return True
        except Exception:
            pass

        return False

    def _auto_item_is_antiafk_overdue(self) -> bool:
        return self._auto_item_is_antiafk_overdue_within(0.0)

    def _auto_item_pause_antiafk(self):
        """
        Pause Anti-AFK while Auto-Item is interacting with Roblox.

        Note: we avoid relying solely on the cached `antiafk_running` state (it can be stale if the
        worker process is lagging). Prefer asking the engine to pause and use its return value.
        """
        self._auto_item_antiafk_was_running = False

        antiafk = getattr(self, "antiafk", None)
        if not antiafk:
            return True

        # If Anti-AFK isn't running/enabled, there is nothing to pause (and nothing to protect).
        try:
            if not (
                self._is_manager_running()
                and bool(getattr(self, "antiafk_enable_chk", None) and self.antiafk_enable_chk.isChecked())
                and bool(getattr(antiafk, "antiafk_running", False))
            ):
                return True
        except Exception:
            return True

        # If any connected user is overdue for an Anti-AFK touch, don't pause for this cycle.
        if self._auto_item_is_antiafk_overdue():
            try:
                self.autoitem_log_signal.emit(
                    "[Auto-Actions] Anti-AFK overdue (>=10m on at least one connected user); skipping pause this cycle."
                )
            except Exception:
                pass
            return False

        # Preferred: native pause (works for both worker-proxy and in-process engines).
        if hasattr(antiafk, "pause_antiafk"):
            try:
                paused_ok = bool(antiafk.pause_antiafk(wait=True))
            except Exception:
                paused_ok = False
            if not paused_ok:
                try:
                    if hasattr(antiafk, "resume_antiafk"):
                        antiafk.resume_antiafk()
                except Exception:
                    pass

                try:
                    self.autoitem_log_signal.emit(
                        "[Auto-Actions] Failed to pause Anti-AFK (timeout); skipping this cycle to keep Anti-AFK running."
                    )
                except Exception:
                    pass

                self._auto_item_antiafk_was_running = False
                return False

            self._auto_item_antiafk_was_running = True
            return True

        # Legacy fallback: fully stop before Auto-Item interactions (only if it looks like it's running).
        try:
            if not bool(getattr(antiafk, "antiafk_running", False)):
                return True
        except Exception:
            return True

        try:
            self._auto_item_antiafk_was_running = True
            t = getattr(antiafk, "antiafk_thread", None)
            antiafk.stop_antiafk()
            try:
                if t is not None and getattr(t, "is_alive", None) and t.is_alive():
                    t.join()
            except Exception:
                pass
        except Exception:
            self._auto_item_antiafk_was_running = False
            return False

        return True

    def _auto_item_resume_antiafk(self):
        try:
            if (
                self._auto_item_antiafk_was_running
                and getattr(self, "antiafk", None)
                and self._is_manager_running()
                and bool(getattr(self, "antiafk_enable_chk", None) and self.antiafk_enable_chk.isChecked())
            ):
                # Prefer resume when supported, without relying on cached `antiafk_running`.
                antiafk = getattr(self, "antiafk", None)
                if antiafk is None:
                    return

                if hasattr(antiafk, "resume_antiafk"):
                    resumed = False
                    try:
                        resumed = bool(antiafk.resume_antiafk())
                    except Exception:
                        resumed = False
                    if not resumed:
                        antiafk.start_antiafk()
                else:
                    antiafk.start_antiafk()
        except Exception:
            pass
        finally:
            self._auto_item_antiafk_was_running = False

    def _auto_item_pre_action_hook(self, uid: str, pid: int) -> float:
        """
        Auto Item hook: temporarily disable BES throttling a few seconds before actions.
        Returns the requested lead time (seconds) so the engine can wait before clicking.
        """
        ctl = getattr(self, "bes_controller", None)
        if ctl is None:
            return 0.0

        try:
            with self._bes_cfg_lock:
                cfg = dict(self._bes_cfg_cache or {})
        except Exception:
            cfg = {}

        if not bool(cfg.get("enabled", False)):
            return 0.0

        try:
            lead_s = float(cfg.get("auto_item_lead_s", 0.0) or 0.0)
        except Exception:
            lead_s = 0.0

        # Hold long enough to cover the full click/type sequence; post-action hook will release early.
        try:
            ctl.hold_unthrottled(int(pid), max(0.0, lead_s) + 120.0)
        except Exception:
            pass

        return max(0.0, float(lead_s))

    def _auto_item_post_action_hook(self, uid: str, pid: int) -> None:
        """Auto Item hook: release BES unthrottle hold after actions (with small grace)."""
        ctl = getattr(self, "bes_controller", None)

        try:
            with self._bes_cfg_lock:
                cfg = dict(self._bes_cfg_cache or {})
        except Exception:
            cfg = {}

        if ctl is not None and bool(cfg.get("enabled", False)):
            try:
                grace_s = float(cfg.get("auto_item_grace_s", 0.0) or 0.0)
            except Exception:
                grace_s = 0.0

            try:
                ctl.release_hold(int(pid))
            except Exception:
                pass

            if grace_s > 0.0:
                try:
                    ctl.hold_unthrottled(int(pid), float(grace_s))
                except Exception:
                    pass

        # Treat Auto-Item as an Anti-AFK "touch" so Anti-AFK ordering and timers stay in sync.
        try:
            uid_s = str(uid)
            runtime = (self.user_data or {}).get(uid_s, {}) or {}
            server = str(runtime.get("server", "") or "")
            if self._is_disconnected_server_label(server):
                return
            now_ts = float(time.time())
            with self._antiafk_touch_lock:
                self._antiafk_last_touch_by_uid[uid_s] = now_ts
            antiafk = getattr(self, "antiafk", None)
            if antiafk is not None:
                try:
                    antiafk.touch_pids([int(pid)])
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------
    # Auto Item global hotkey
    # ------------------------

    def _ensure_win_hotkey_filter(self) -> None:
        if os.name != "nt":
            return
        if getattr(self, "_win_hotkey_filter", None) is not None:
            return
        try:
            app = QApplication.instance()
            if app is None:
                return
            self._win_hotkey_filter = _WinHotkeyFilter(self._on_win_hotkey)
            app.installNativeEventFilter(self._win_hotkey_filter)
        except Exception:
            self._win_hotkey_filter = None

    def _parse_hotkey_to_win32(self, seq: str) -> Optional[Tuple[int, int]]:
        """
        Convert a single hotkey combo like "Ctrl+Alt+Space" -> (MOD_*, VK_*).
        Returns None if unsupported/empty.
        """
        s = str(seq or "").strip()
        if not s:
            return None

        MOD_ALT = 0x0001
        MOD_CONTROL = 0x0002
        MOD_SHIFT = 0x0004
        MOD_WIN = 0x0008

        def _vk_from_token(token: str) -> Optional[int]:
            t = str(token or "").strip()
            if not t:
                return None
            up = t.upper()
            if len(up) == 1 and (("A" <= up <= "Z") or ("0" <= up <= "9")):
                return ord(up)

            mapping = {
                "SPACE": 0x20,
                "TAB": 0x09,
                "ENTER": 0x0D,
                "RETURN": 0x0D,
                "ESC": 0x1B,
                "ESCAPE": 0x1B,
                "UP": 0x26,
                "DOWN": 0x28,
                "LEFT": 0x25,
                "RIGHT": 0x27,
                "PGUP": 0x21,
                "PAGEUP": 0x21,
                "PGDN": 0x22,
                "PAGEDOWN": 0x22,
                "HOME": 0x24,
                "END": 0x23,
                "INSERT": 0x2D,
                "DEL": 0x2E,
                "DELETE": 0x2E,
                "BACKSPACE": 0x08,
                "BKSP": 0x08,
            }
            if up in mapping:
                return int(mapping[up])

            if up.startswith("F") and up[1:].isdigit():
                n = int(up[1:])
                if 1 <= n <= 24:
                    return 0x70 + (n - 1)

            return None

        parts = [p.strip() for p in s.replace("-", "+").split("+") if p.strip()]
        mods = 0
        vk: Optional[int] = None
        for p in parts:
            up = p.upper()
            if up in ("CTRL", "CONTROL"):
                mods |= MOD_CONTROL
            elif up == "ALT":
                mods |= MOD_ALT
            elif up == "SHIFT":
                mods |= MOD_SHIFT
            elif up in ("WIN", "META", "CMD", "COMMAND"):
                mods |= MOD_WIN
            else:
                vk = _vk_from_token(p)

        if vk is None:
            return None
        return int(mods), int(vk)

    def _unregister_auto_item_hotkey(self) -> None:
        if os.name != "nt":
            return
        if not getattr(self, "_auto_item_hotkey_registered", False):
            return
        try:
            import ctypes

            ctypes.windll.user32.UnregisterHotKey(int(self._auto_item_hotkey_hwnd), int(self._auto_item_hotkey_id))
        except Exception:
            pass
        self._auto_item_hotkey_registered = False
        self._auto_item_hotkey_hwnd = 0

    def _apply_auto_item_hotkey(self, seq: str, *, quiet: bool = False) -> None:
        if os.name != "nt":
            return
        self._ensure_win_hotkey_filter()

        s = str(seq or "").strip()
        if not s:
            self._unregister_auto_item_hotkey()
            if not quiet:
                self.autoitem_log_signal.emit("[Auto-Actions] Toggle hotkey cleared (disabled).")
            return

        parsed = self._parse_hotkey_to_win32(s)
        if not parsed:
            if not quiet:
                QMessageBox.warning(
                    self,
                    "Auto-Actions Hotkey",
                    "Unsupported hotkey.\n\nUse a single combo like: Ctrl+Alt+Space",
                )
            return
        mods, vk = parsed

        self._unregister_auto_item_hotkey()

        try:
            import ctypes

            hwnd = 0
            try:
                hwnd = int(self.winId())
            except Exception:
                hwnd = 0

            ok = int(ctypes.windll.user32.RegisterHotKey(int(hwnd), int(self._auto_item_hotkey_id), int(mods), int(vk)))
            if not ok:
                ok = int(ctypes.windll.user32.RegisterHotKey(0, int(self._auto_item_hotkey_id), int(mods), int(vk)))
                if ok:
                    hwnd = 0

            if ok:
                self._auto_item_hotkey_registered = True
                self._auto_item_hotkey_hwnd = int(hwnd)
                if not quiet:
                    self.autoitem_log_signal.emit(f"[Auto-Actions] Toggle hotkey registered: {s}")
            else:
                self._auto_item_hotkey_registered = False
                self._auto_item_hotkey_hwnd = 0
                msg = f"Could not register hotkey '{s}'. It may be in use by another app."
                self.autoitem_log_signal.emit(f"[Auto-Actions] {msg}")
                if not quiet:
                    QMessageBox.warning(self, "Auto-Actions Hotkey", msg)
        except Exception as e:
            self._auto_item_hotkey_registered = False
            self._auto_item_hotkey_hwnd = 0
            if not quiet:
                QMessageBox.warning(self, "Auto-Actions Hotkey", f"Failed to register hotkey:\n{e}")

    def _on_win_hotkey(self, hotkey_id: int) -> None:
        try:
            if int(hotkey_id) == int(self._auto_item_hotkey_id):
                self._toggle_auto_item_enabled()
        except Exception:
            pass

    def _toggle_auto_item_enabled(self) -> None:
        try:
            chk = getattr(self, "auto_item_enable_chk", None)
            if chk is None:
                return
            chk.setChecked(not bool(chk.isChecked()))
        except Exception:
            pass

    def _on_auto_item_hotkey_changed(self, *_):
        if self._loading_autoitem_settings:
            return
        try:
            seq = self.auto_item_hotkey_edit.keySequence().toString().strip()
        except Exception:
            seq = ""
        self._apply_auto_item_hotkey(seq, quiet=False)
        self._on_auto_item_ui_changed()

    def _auto_item_refresh_users(self):
        try:
            users = self.config_manager.load_users() or {}
        except Exception:
            users = {}

        # Preserve current in-UI selection when possible (fallback to disk on first load)
        selected = [uid for uid, cb in (getattr(self, "auto_item_user_checks", {}) or {}).items() if cb.isChecked()]
        if not selected:
            try:
                selected = list(self._get_auto_item_cfg_from_disk().get("users", []) or [])
            except Exception:
                selected = []

        # Clear existing
        try:
            while self.auto_item_users_vbox.count():
                item = self.auto_item_users_vbox.takeAt(0)
                w = item.widget()
                if w:
                    w.setParent(None)
        except Exception:
            pass

        self.auto_item_user_checks = {}
        for uid in sorted(users.keys(), key=lambda u: (users.get(u, {}) or {}).get("username", str(u))):
            info = users.get(uid, {}) or {}
            uname = info.get("username", uid)
            cb = QCheckBox(f"{uname} ({uid})")
            cb.toggled.connect(self._on_auto_item_ui_changed)
            self.auto_item_users_vbox.addWidget(cb)
            self.auto_item_user_checks[str(uid)] = cb

        self.auto_item_users_vbox.addStretch()

        # Apply selection without triggering persistence churn
        prev = bool(getattr(self, "_loading_autoitem_settings", False))
        self._loading_autoitem_settings = True
        try:
            self._apply_auto_item_users_to_ui(selected)
        finally:
            self._loading_autoitem_settings = prev

    def _auto_item_set_all_users(self, enabled: bool):
        for cb in (self.auto_item_user_checks or {}).values():
            try:
                cb.setChecked(bool(enabled))
            except Exception:
                pass
        self._on_auto_item_ui_changed()

    def _auto_item_add_item(self):
        items = self._current_auto_item_items()
        items.append(
            {
                "enabled": True,
                "name": "",
                "amount": 1,
                "cooldown": 0,
                "biomes": [],
                "alert_enabled": False,
                "alert_webhook": "",
                "alert_message": "",
                "alert_lead_s": 15.0,
            }
        )
        self._load_auto_item_items_table(items)
        self._on_auto_item_ui_changed()

    def _auto_item_remove_selected_items(self):
        rows = sorted({idx.row() for idx in self.auto_item_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        items = self._current_auto_item_items()
        for r in rows:
            if 0 <= r < len(items):
                items.pop(r)
        self._load_auto_item_items_table(items)
        self._on_auto_item_ui_changed()

    def _auto_item_move_selected(self, delta: int):
        row = self.auto_item_table.currentRow()
        if row < 0:
            return
        items = self._current_auto_item_items()
        new_row = row + int(delta)
        if new_row < 0 or new_row >= len(items):
            return
        items[row], items[new_row] = items[new_row], items[row]
        self._load_auto_item_items_table(items)
        try:
            self.auto_item_table.setCurrentCell(new_row, 1)
        except Exception:
            pass
        self._on_auto_item_ui_changed()

    def _auto_item_test_once(self):
        self._ensure_auto_item_engine()
        if not getattr(self, "auto_item_engine", None):
            QMessageBox.warning(self, "Auto-Item", "Auto-Item engine is not available.")
            return

        if not self._is_manager_running():
            QMessageBox.information(self, "Auto-Item Test", "Start the manager first so a user window can be resolved.")
            return

        uid = None
        for k, cb in (getattr(self, "auto_item_user_checks", {}) or {}).items():
            try:
                if cb.isChecked():
                    uid = str(k)
                    break
            except Exception:
                continue

        if not uid:
            QMessageBox.information(self, "Auto-Item Test", "Select at least one user in the Users list first.")
            return

        uname = uid
        try:
            users = self.config_manager.load_users() or {}
            info = users.get(uid, {}) or {}
            uname = info.get("username") or uid
        except Exception:
            uname = uid

        confirm = QMessageBox.question(
            self,
            "Auto-Item Test",
            f"Run Auto-Item once on:\n{uname} ({uid})\n\nThis will interact with Roblox and may use items.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Ensure the engine uses the latest UI config (without waiting for debounce).
        try:
            self.auto_item_engine.update_config(self._get_auto_item_settings_from_ui())
        except Exception:
            pass

        try:
            ok = bool(self.auto_item_engine.test_once(uid))
        except Exception as e:
            self.autoitem_log_signal.emit(f"[Auto-Item] Test: error: {e}")
            ok = False

        if ok:
            QMessageBox.information(self, "Auto-Item Test", "Test run complete. Check the Auto-Item log for details.")
        else:
            QMessageBox.warning(self, "Auto-Item Test", "Test run did not complete. Check the Auto-Item log for details.")

    def _update_biomes_btn_text(self, btn: QPushButton):
        biomes = btn.property("biomes") or []
        biomes = [str(b).strip().upper() for b in (biomes or []) if str(b).strip()]
        btn.setProperty("biomes", biomes)
        label = "Any" if not biomes else f"{len(biomes)} selected"
        btn.setText(f"{label} v")

    def _update_users_btn_text(self, btn: QPushButton):
        raw = btn.property("users")
        # Semantics:
        # - None / missing => All users
        # - []            => No users
        # - [uids...]     => Only those users
        if raw is None:
            btn.setProperty("users", None)
            btn.setText("All users")
            btn.setToolTip("Targets every checked user in the Users panel.")
            return
        if not isinstance(raw, (list, tuple, set)):
            # Be tolerant of Qt container/variant types (e.g., QStringList).
            try:
                if isinstance(raw, (str, dict)):
                    raise TypeError
                raw = list(raw)  # type: ignore[arg-type]
            except Exception:
                btn.setProperty("users", None)
                btn.setText("All users")
                btn.setToolTip("Targets every checked user in the Users panel.")
                return

        users = [str(u).strip() for u in (raw or []) if str(u).strip()]
        btn.setProperty("users", users)
        if not users:
            btn.setText("No users")
            btn.setToolTip("This row will not run for any user until at least one user is assigned.")
            return

        user_names: List[str] = []
        try:
            user_cfg = self.config_manager.peek_users() or {}
        except Exception:
            user_cfg = {}
        for uid in users:
            try:
                user_names.append(str((user_cfg.get(uid, {}) or {}).get("username") or uid))
            except Exception:
                user_names.append(str(uid))
        btn.setText("1 user" if len(users) == 1 else f"{len(users)} users")
        btn.setToolTip("Assigned users: " + ", ".join(user_names))

    def _update_alert_btn_text(self, btn: QPushButton):
        try:
            enabled = bool(btn.property("alert_enabled"))
        except Exception:
            enabled = False
        try:
            hook = str(btn.property("alert_webhook") or "").strip()
        except Exception:
            hook = ""

        btn.setProperty("alert_enabled", bool(enabled))
        btn.setProperty("alert_webhook", hook)

        if not enabled:
            btn.setText("Alert off")
            btn.setToolTip("No pre-run webhook alert will be sent.")
        elif not hook:
            btn.setText("Need webhook")
            btn.setToolTip("Alerts are enabled, but no webhook URL is configured.")
        else:
            try:
                lead_s = float(btn.property("alert_lead_s") or 15.0)
            except Exception:
                lead_s = 15.0
            btn.setText("Alert on")
            btn.setToolTip(f"Webhook alert will be sent {lead_s:.1f}s before the action row runs.")

    def _edit_item_biomes(self, btn: QPushButton):
        try:
            current = btn.property("biomes") or []
            current = [str(b).strip().upper() for b in (current or []) if str(b).strip()]
        except Exception:
            current = []

        # Dialog
        dlg = QDialog(self)
        dlg.setWindowTitle("Select Allowed Biomes")
        v = QVBoxLayout(dlg)

        btn_row = QHBoxLayout()
        sel_all_btn = QPushButton("Select All")
        sel_none_btn = QPushButton("Select None")
        sel_all_btn.setStyleSheet(self._get_secondary_button_style())
        sel_none_btn.setStyleSheet(self._get_secondary_button_style())
        btn_row.addWidget(sel_all_btn)
        btn_row.addWidget(sel_none_btn)
        btn_row.addStretch()
        v.addLayout(btn_row)

        lst = QListWidget()
        lst.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        lst.setMinimumHeight(340)
        # Make selected highlight consistent regardless of focus (Select All button vs manual clicks)
        lst.setStyleSheet(
            """
            QListWidget::item:selected {
                background-color: palette(highlight);
                color: palette(highlighted-text);
            }
            QListWidget::item:selected:!active {
                background-color: palette(highlight);
                color: palette(highlighted-text);
            }
            """
        )
        try:
            all_biomes = list(biome_names())
        except Exception:
            all_biomes = []
        sel = set(current)
        for b in all_biomes:
            it = QListWidgetItem(str(b).upper())
            lst.addItem(it)
            if it.text() in sel:
                it.setSelected(True)

        sel_all_btn.clicked.connect(lambda: (lst.selectAll(), lst.setFocus()))
        sel_none_btn.clicked.connect(lambda: (lst.clearSelection(), lst.setFocus()))
        v.addWidget(lst)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            chosen = [i.text() for i in lst.selectedItems()]
            btn.setProperty("biomes", chosen)
            self._update_biomes_btn_text(btn)
            self._on_auto_item_ui_changed()

    def _edit_item_users(self, btn: QPushButton):
        try:
            raw_current = btn.property("users")
        except Exception:
            raw_current = None
        current: Optional[List[str]] = None
        if raw_current is None:
            current = None  # All
        elif isinstance(raw_current, (list, tuple)):
            current = [str(u).strip() for u in raw_current if str(u).strip()]
        else:
            current = None  # All

        try:
            users = self.config_manager.load_users() or {}
        except Exception:
            users = {}

        all_uids: List[str] = [str(uid) for uid in sorted(users.keys())]

        dlg = QDialog(self)
        dlg.setWindowTitle("Row Users")
        dlg.resize(440, 520)
        self._auto_item_apply_dialog_style(dlg)
        v = QVBoxLayout(dlg)

        hint = QLabel("Choose exactly which users this action row is allowed to run against.")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        btn_row = QHBoxLayout()
        sel_all_btn = QPushButton("Select All")
        sel_none_btn = QPushButton("Select None")
        btn_row.addWidget(sel_all_btn)
        btn_row.addWidget(sel_none_btn)
        btn_row.addStretch()
        v.addLayout(btn_row)

        lst = QListWidget()
        lst.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        # Make selected highlight consistent regardless of focus (Select All button vs manual clicks)
        lst.setStyleSheet(
            """
            QListWidget::item:selected {
                background-color: palette(highlight);
                color: palette(highlighted-text);
            }
            QListWidget::item:selected:!active {
                background-color: palette(highlight);
                color: palette(highlighted-text);
            }
            """
        )

        # Semantics:
        # - current is None => All users selected in UI
        # - current is []   => None selected in UI
        # - current list    => that subset selected
        sel = set(all_uids) if current is None else set(current or [])

        for uid in sorted(users.keys(), key=lambda u: (users.get(u, {}) or {}).get("username", str(u))):
            info = users.get(uid, {}) or {}
            uname = info.get("username", uid)
            it = QListWidgetItem(f"{uname} ({uid})")
            it.setData(Qt.ItemDataRole.UserRole, str(uid))
            lst.addItem(it)
            if str(uid) in sel:
                it.setSelected(True)

        sel_all_btn.clicked.connect(lambda: (lst.selectAll(), lst.setFocus()))
        sel_none_btn.clicked.connect(lambda: (lst.clearSelection(), lst.setFocus()))

        v.addWidget(lst)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            chosen: List[str] = []
            for i in lst.selectedItems():
                try:
                    uid = str(i.data(Qt.ItemDataRole.UserRole) or "").strip()
                except Exception:
                    uid = ""
                if uid:
                    chosen.append(uid)

            chosen_set = set(chosen)
            if all_uids and len(chosen_set) >= len(set(all_uids)):
                # All selected => store None (meaning all users)
                btn.setProperty("users", None)
            else:
                # None selected => store [] (meaning no users)
                btn.setProperty("users", chosen)
            self._update_users_btn_text(btn)
            self._on_auto_item_ui_changed()

    def _edit_item_alert(self, btn: QPushButton):
        try:
            enabled = bool(btn.property("alert_enabled"))
        except Exception:
            enabled = False
        try:
            hook = str(btn.property("alert_webhook") or "").strip()
        except Exception:
            hook = ""
        try:
            msg = str(btn.property("alert_message") or "")
        except Exception:
            msg = ""
        try:
            lead_s = float(btn.property("alert_lead_s") or 15.0)
        except Exception:
            lead_s = 15.0

        dlg = QDialog(self)
        dlg.setWindowTitle("Row Alert")
        dlg.resize(520, 320)
        self._auto_item_apply_dialog_style(dlg)
        v = QVBoxLayout(dlg)

        intro = QLabel("Configure an optional webhook alert before this row executes.")
        intro.setProperty("role", "hint")
        intro.setWordWrap(True)
        v.addWidget(intro)

        form = QFormLayout()

        enable_chk = QCheckBox("Enable alert before this row runs")
        enable_chk.setChecked(bool(enabled))
        form.addRow(enable_chk)

        lead_spin = QDoubleSpinBox()
        lead_spin.setRange(0.0, 600.0)
        lead_spin.setDecimals(1)
        lead_spin.setSingleStep(0.5)
        lead_spin.setValue(max(0.0, float(lead_s)))
        form.addRow("Lead seconds:", lead_spin)

        hook_le = QLineEdit(hook)
        hook_le.setPlaceholderText("https://discord.com/api/webhooks/...")
        try:
            hook_le.setClearButtonEnabled(True)
        except Exception:
            pass
        form.addRow("Webhook URL:", hook_le)

        msg_le = QLineEdit(msg)
        msg_le.setPlaceholderText("(optional) message to send alongside the alert embed")
        try:
            msg_le.setClearButtonEnabled(True)
        except Exception:
            pass
        form.addRow("Message:", msg_le)

        v.addLayout(form)

        hint = QLabel(
            "The alert includes the row name, target account, and private server details when available."
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        v.addWidget(hint)

        def _sync_enabled_state() -> None:
            active = bool(enable_chk.isChecked())
            lead_spin.setEnabled(active)
            hook_le.setEnabled(active)
            msg_le.setEnabled(active)

        enable_chk.toggled.connect(_sync_enabled_state)
        _sync_enabled_state()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            btn.setProperty("alert_enabled", bool(enable_chk.isChecked()))
            btn.setProperty("alert_webhook", hook_le.text().strip())
            btn.setProperty("alert_message", msg_le.text())
            btn.setProperty("alert_lead_s", float(lead_spin.value()))
            self._update_alert_btn_text(btn)
            self._on_auto_item_ui_changed()

    def _load_auto_item_items_table(self, items: List[dict]):
        self.auto_item_table.setRowCount(0)

        def _wrap_cell(w: QWidget, *, center: bool = False, margins: Tuple[int, int, int, int] = (0, 6, 0, 6)) -> QWidget:
            holder = QWidget()
            holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            holder.setStyleSheet("background: transparent;")
            lay = QHBoxLayout(holder)
            lay.setContentsMargins(*margins)
            lay.setSpacing(0)
            if center:
                lay.addStretch(1)
                lay.addWidget(w, 0, Qt.AlignmentFlag.AlignCenter)
                lay.addStretch(1)
            else:
                lay.addWidget(w, 1, Qt.AlignmentFlag.AlignVCenter)
            try:
                w.raise_()
            except Exception:
                pass
            try:
                holder.raise_()
            except Exception:
                pass
            return holder

        for it in (items or []):
            row = self.auto_item_table.rowCount()
            self.auto_item_table.insertRow(row)
            try:
                self.auto_item_table.setRowHeight(row, 62)
            except Exception:
                pass

            en = QCheckBox()
            en.setChecked(bool(it.get("enabled", True)))
            en.setStyleSheet("background: transparent;")
            en.toggled.connect(self._on_auto_item_ui_changed)
            self.auto_item_table.setCellWidget(row, 0, _wrap_cell(en, center=True))

            name = QLineEdit(str(it.get("name", "") or ""))
            name.setPlaceholderText("Item name")
            name.textChanged.connect(self._on_auto_item_ui_changed)
            name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.auto_item_table.setCellWidget(row, 1, _wrap_cell(name, center=False, margins=(6, 6, 6, 6)))

            amt = _AutoItemSpinBox()
            amt.setRange(1, 999)
            amt.setValue(max(1, int(it.get("amount", 1) or 1)))
            amt.setAlignment(Qt.AlignmentFlag.AlignCenter)
            try:
                amt.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
            except Exception:
                pass
            amt.setMinimumWidth(80)
            amt.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            amt.valueChanged.connect(self._on_auto_item_ui_changed)
            self.auto_item_table.setCellWidget(row, 2, _wrap_cell(amt, center=True, margins=(0, 6, 0, 6)))

            cd = _AutoItemSpinBox()
            cd.setRange(0, 86400)
            cd.setValue(max(0, int(it.get("cooldown", 0) or 0)))
            cd.setAlignment(Qt.AlignmentFlag.AlignCenter)
            try:
                cd.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
            except Exception:
                pass
            cd.setMinimumWidth(95)
            cd.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            cd.valueChanged.connect(self._on_auto_item_ui_changed)
            self.auto_item_table.setCellWidget(row, 3, _wrap_cell(cd, center=True, margins=(0, 6, 0, 6)))

            bbtn = QPushButton()
            bbtn.setProperty("biomes", it.get("biomes", []) or [])
            self._update_biomes_btn_text(bbtn)
            try:
                bbtn.setCursor(Qt.CursorShape.PointingHandCursor)
            except Exception:
                pass
            bbtn.clicked.connect(lambda _, b=bbtn: self._edit_item_biomes(b))
            bbtn.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
            self.auto_item_table.setCellWidget(row, 4, _wrap_cell(bbtn, center=False, margins=(6, 6, 6, 6)))

            ubtn = QPushButton()
            raw_users = it.get("users", None)
            users_explicit = bool(it.get("users_explicit", False))
            users_prop = None
            if raw_users is None:
                users_prop = None
            elif isinstance(raw_users, (list, tuple, set)):
                users_list = [str(u).strip() for u in raw_users if str(u).strip()]
                if not users_list:
                    users_prop = [] if users_explicit else None
                else:
                    users_prop = users_list
            ubtn.setProperty("users", users_prop)
            self._update_users_btn_text(ubtn)
            try:
                ubtn.setCursor(Qt.CursorShape.PointingHandCursor)
            except Exception:
                pass
            ubtn.clicked.connect(lambda _, b=ubtn: self._edit_item_users(b))
            ubtn.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
            self.auto_item_table.setCellWidget(row, 5, _wrap_cell(ubtn, center=False, margins=(6, 6, 6, 6)))

            abtn = QPushButton()
            abtn.setProperty("alert_enabled", bool(it.get("alert_enabled", False)))
            abtn.setProperty(
                "alert_webhook",
                str(it.get("alert_webhook") or it.get("alert_webhook_url") or "").strip(),
            )
            abtn.setProperty("alert_message", str(it.get("alert_message") or ""))
            try:
                abtn.setProperty("alert_lead_s", float(it.get("alert_lead_s", 15.0) or 15.0))
            except Exception:
                abtn.setProperty("alert_lead_s", 15.0)
            self._update_alert_btn_text(abtn)
            try:
                abtn.setCursor(Qt.CursorShape.PointingHandCursor)
            except Exception:
                pass
            abtn.clicked.connect(lambda _, b=abtn: self._edit_item_alert(b))
            abtn.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
            self.auto_item_table.setCellWidget(row, 6, _wrap_cell(abtn, center=False, margins=(6, 6, 6, 6)))

    def _current_auto_item_items(self) -> List[dict]:
        items: List[dict] = []
        rows = self.auto_item_table.rowCount()

        def _unwrap(col_widget: QWidget, typ):
            if col_widget is None:
                return None
            if isinstance(col_widget, typ):
                return col_widget
            try:
                return col_widget.findChild(typ)
            except Exception:
                return None

        for r in range(rows):
            try:
                en = _unwrap(self.auto_item_table.cellWidget(r, 0), QCheckBox)
                name = _unwrap(self.auto_item_table.cellWidget(r, 1), QLineEdit)
                amt = _unwrap(self.auto_item_table.cellWidget(r, 2), QSpinBox)
                cd = _unwrap(self.auto_item_table.cellWidget(r, 3), QSpinBox)
                bbtn = _unwrap(self.auto_item_table.cellWidget(r, 4), QPushButton)
                ubtn = _unwrap(self.auto_item_table.cellWidget(r, 5), QPushButton)
                abtn = _unwrap(self.auto_item_table.cellWidget(r, 6), QPushButton)

                item = {
                    "enabled": bool(en.isChecked()) if isinstance(en, QCheckBox) else True,
                    "name": name.text().strip() if isinstance(name, QLineEdit) else "",
                    "amount": int(amt.value()) if isinstance(amt, QSpinBox) else 1,
                    "cooldown": int(cd.value()) if isinstance(cd, QSpinBox) else 0,
                    "biomes": (bbtn.property("biomes") or []) if isinstance(bbtn, QPushButton) else [],
                }
                if isinstance(ubtn, QPushButton):
                    raw_users = ubtn.property("users")
                    users_seq = None
                    if raw_users is None:
                        users_seq = None
                    elif isinstance(raw_users, (list, tuple, set)):
                        users_seq = raw_users
                    else:
                        try:
                            if isinstance(raw_users, (str, dict)):
                                users_seq = None
                            else:
                                users_seq = list(raw_users)  # type: ignore[arg-type]
                        except Exception:
                            users_seq = None

                    if users_seq is not None:
                        users_list = [str(u).strip() for u in (users_seq or []) if str(u).strip()]
                        item["users"] = users_list
                        item["users_explicit"] = True

                if isinstance(abtn, QPushButton):
                    try:
                        item["alert_enabled"] = bool(abtn.property("alert_enabled"))
                    except Exception:
                        item["alert_enabled"] = False
                    try:
                        item["alert_webhook"] = str(abtn.property("alert_webhook") or "").strip()
                    except Exception:
                        item["alert_webhook"] = ""
                    try:
                        item["alert_message"] = str(abtn.property("alert_message") or "")
                    except Exception:
                        item["alert_message"] = ""
                    try:
                        item["alert_lead_s"] = float(abtn.property("alert_lead_s") or 15.0)
                    except Exception:
                        item["alert_lead_s"] = 15.0
                items.append(item)
            except Exception:
                continue
        return items

    def _auto_item_capture_coord(self, key: str, sample_color: bool = False):
        """
        Capture a coordinate by screenshotting a Roblox window and letting the user click the point.
        """
        try:
            import ctypes
            import psutil
            import win32api as _wapi
            import win32con as _wcon
            import win32gui as _wgui
            import win32process as _wproc
        except Exception as e:
            self.autoitem_log_signal.emit(f"[Auto-Item] Missing dependencies for capture: {e}")
            return

        def _is_roblox_hwnd(hwnd: int) -> bool:
            try:
                if not hwnd or not _wgui.IsWindow(hwnd):
                    return False
                _, pid = _wproc.GetWindowThreadProcessId(hwnd)
                if not pid:
                    return False
                return str(psutil.Process(int(pid)).name()).lower() == "robloxplayerbeta.exe"
            except Exception:
                return False

        def _pick_hwnd() -> Optional[int]:
            try:
                fg = _wgui.GetForegroundWindow()
                if _is_roblox_hwnd(fg):
                    return int(fg)
            except Exception:
                pass
            try:
                wins = enum_roblox_windows()
                if wins:
                    return int(wins[0].hwnd)
            except Exception:
                pass
            return None

        def _bring_foreground(hwnd: int) -> None:
            try:
                if _wgui.IsIconic(hwnd):
                    _wgui.ShowWindow(hwnd, _wcon.SW_RESTORE)
                try:
                    cur_tid = _wapi.GetCurrentThreadId()
                    win_tid = _wproc.GetWindowThreadProcessId(hwnd)[0]
                    ctypes.windll.user32.AttachThreadInput(cur_tid, win_tid, True)
                    _wgui.BringWindowToTop(hwnd)
                    _wgui.SetForegroundWindow(hwnd)
                finally:
                    try:
                        ctypes.windll.user32.AttachThreadInput(cur_tid, win_tid, False)
                    except Exception:
                        pass
            except Exception:
                try:
                    _wgui.SetForegroundWindow(hwnd)
                except Exception:
                    pass

        try:
            hwnd = _pick_hwnd()
            if not hwnd:
                self.autoitem_log_signal.emit("[Auto-Item] No Roblox window found to capture from.")
                return

            _bring_foreground(hwnd)
            time.sleep(0.12)

            # Capture client area only (so ratios match the engine's client-relative math)
            _l, _t, cr, cb = _wgui.GetClientRect(hwnd)
            client_w = int(cr - _l)
            client_h = int(cb - _t)
            if client_w <= 0 or client_h <= 0:
                raise RuntimeError("Invalid Roblox client size.")

            full = capture_window_image(hwnd)
            if full is None:
                raise RuntimeError("Failed to capture Roblox window.")

            def _is_blackish(im: Image.Image) -> bool:
                try:
                    _lo, _hi = im.convert("L").getextrema()
                    return int(_hi) <= 5
                except Exception:
                    return False

            # If the primary capture is black (PrintWindow can "succeed" but return black),
            # try a direct screen grab as a fallback.
            if _is_blackish(full):
                try:
                    from PIL import ImageGrab as _ig

                    wl, wt, wr, wb = _wgui.GetWindowRect(hwnd)
                    alt = _ig.grab(bbox=(wl, wt, wr, wb))
                    if alt is not None and not _is_blackish(alt):
                        full = alt
                except Exception:
                    pass

            # If still black, try alternate PrintWindow flags (some windows only render with certain flags).
            if _is_blackish(full):
                try:
                    import win32ui as _wui
                    from ctypes import windll as _windll

                    def _try_printwindow(flag: int) -> Optional[Image.Image]:
                        try:
                            left, top, right, bottom = _wgui.GetWindowRect(hwnd)
                            width = int(right - left)
                            height = int(bottom - top)
                            if width <= 0 or height <= 0:
                                return None

                            hwnd_dc = _wgui.GetWindowDC(hwnd)
                            if not hwnd_dc:
                                return None
                            try:
                                mfc_dc = _wui.CreateDCFromHandle(hwnd_dc)
                                save_dc = mfc_dc.CreateCompatibleDC()
                                bmp = _wui.CreateBitmap()
                                bmp.CreateCompatibleBitmap(mfc_dc, width, height)
                                save_dc.SelectObject(bmp)

                                ok = _windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), int(flag))
                                if int(ok) != 1:
                                    try:
                                        _wgui.DeleteObject(bmp.GetHandle())
                                    except Exception:
                                        pass
                                    try:
                                        save_dc.DeleteDC()
                                    except Exception:
                                        pass
                                    try:
                                        mfc_dc.DeleteDC()
                                    except Exception:
                                        pass
                                    return None

                                bmpinfo = bmp.GetInfo()
                                bmpstr = bmp.GetBitmapBits(True)
                                img2 = Image.frombuffer(
                                    "RGB",
                                    (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
                                    bmpstr,
                                    "raw",
                                    "BGRX",
                                    0,
                                    1,
                                )
                                try:
                                    _wgui.DeleteObject(bmp.GetHandle())
                                except Exception:
                                    pass
                                try:
                                    save_dc.DeleteDC()
                                except Exception:
                                    pass
                                try:
                                    mfc_dc.DeleteDC()
                                except Exception:
                                    pass
                                return img2
                            finally:
                                try:
                                    _wgui.ReleaseDC(hwnd, hwnd_dc)
                                except Exception:
                                    pass
                        except Exception:
                            return None

                    for fl in (0x00000000, 0x00000001, 0x00000003):
                        alt2 = _try_printwindow(fl)
                        if alt2 is not None and not _is_blackish(alt2):
                            full = alt2
                            break
                except Exception:
                    pass

            # Compute client crop rect in the captured image coordinate space.
            wl, wt, wr, wb = _wgui.GetWindowRect(hwnd)
            win_w = max(1, int(wr - wl))
            win_h = max(1, int(wb - wt))
            scale_x = float(full.width) / float(win_w) if win_w else 1.0
            scale_y = float(full.height) / float(win_h) if win_h else 1.0

            client_left, client_top = _wgui.ClientToScreen(hwnd, (0, 0))
            crop_left = int((client_left - wl) * scale_x)
            crop_top = int((client_top - wt) * scale_y)
            crop_right = int(crop_left + (client_w * scale_x))
            crop_bottom = int(crop_top + (client_h * scale_y))

            crop_left = max(0, min(full.width - 1, crop_left))
            crop_top = max(0, min(full.height - 1, crop_top))
            crop_right = max(crop_left + 1, min(full.width, crop_right))
            crop_bottom = max(crop_top + 1, min(full.height, crop_bottom))

            client_crop_w = max(1, crop_right - crop_left)
            client_crop_h = max(1, crop_bottom - crop_top)

            client_img = full.crop((crop_left, crop_top, crop_right, crop_bottom))

            # Prefer showing the client crop; if it's black but the full window isn't, show full
            # and convert clicks back into client-relative coordinates.
            show_img = client_img
            offset_x = 0
            offset_y = 0
            if _is_blackish(client_img) and not _is_blackish(full):
                show_img = full
                offset_x = crop_left
                offset_y = crop_top

            # If the screenshot is still black, give a clearer hint (common with fullscreen / protected surfaces)
            if _is_blackish(show_img):
                # Final fallback: try Qt's grabWindow which can behave differently than PIL/PrintWindow.
                try:
                    screen = QApplication.primaryScreen()
                    pm_full = screen.grabWindow(hwnd) if screen else QPixmap()

                    def _pm_blackish(pm: QPixmap) -> bool:
                        try:
                            if pm.isNull():
                                return True
                            qimg = pm.toImage()
                            w = qimg.width()
                            h = qimg.height()
                            if w <= 0 or h <= 0:
                                return True
                            pts = [
                                (w // 2, h // 2),
                                (w // 4, h // 4),
                                (3 * w // 4, h // 4),
                                (w // 4, 3 * h // 4),
                                (3 * w // 4, 3 * h // 4),
                            ]
                            mx = 0
                            for x, y in pts:
                                c = qimg.pixelColor(int(x), int(y))
                                mx = max(mx, int(c.red()), int(c.green()), int(c.blue()))
                            return mx <= 5
                        except Exception:
                            return True

                    if not _pm_blackish(pm_full):
                        # Crop client area from the Qt pixmap using the same scale approach.
                        sx = float(pm_full.width()) / float(win_w) if win_w else 1.0
                        sy = float(pm_full.height()) / float(win_h) if win_h else 1.0
                        q_crop_left = int((client_left - wl) * sx)
                        q_crop_top = int((client_top - wt) * sy)
                        q_crop_right = int(q_crop_left + (client_w * sx))
                        q_crop_bottom = int(q_crop_top + (client_h * sy))
                        q_crop_left = max(0, min(pm_full.width() - 1, q_crop_left))
                        q_crop_top = max(0, min(pm_full.height() - 1, q_crop_top))
                        q_crop_right = max(q_crop_left + 1, min(pm_full.width(), q_crop_right))
                        q_crop_bottom = max(q_crop_top + 1, min(pm_full.height(), q_crop_bottom))

                        pm_client = pm_full.copy(QRect(q_crop_left, q_crop_top, q_crop_right - q_crop_left, q_crop_bottom - q_crop_top))
                        pm_show = pm_client if not _pm_blackish(pm_client) else pm_full

                        # Convert to PIL for reuse of the existing click->relative math + color sampling.
                        try:
                            qimg = pm_show.toImage().convertToFormat(QImage.Format.Format_RGB888)
                            ptr = qimg.bits()
                            ptr.setsize(qimg.width() * qimg.height() * 3)
                            show_img = Image.frombytes("RGB", (qimg.width(), qimg.height()), bytes(ptr))

                            if pm_show.cacheKey() == pm_full.cacheKey():
                                offset_x = q_crop_left
                                offset_y = q_crop_top
                                client_crop_w = max(1, q_crop_right - q_crop_left)
                                client_crop_h = max(1, q_crop_bottom - q_crop_top)
                            else:
                                offset_x = 0
                                offset_y = 0
                                client_crop_w = max(1, pm_show.width())
                                client_crop_h = max(1, pm_show.height())
                        except Exception:
                            pass
                except Exception:
                    pass

            if _is_blackish(show_img):
                self.autoitem_log_signal.emit(
                    "[Auto-Item] Screenshot is still black. Roblox may be blocking capture (common in exclusive fullscreen / MS Store builds). "
                    "Try windowed/borderless and re-capture."
                )
                resp = QMessageBox.question(
                    self,
                    "Capture Failed",
                    "Roblox screenshot capture returned a black image.\n\n"
                    "Try:\n"
                    "- Windowed/borderless mode (avoid exclusive fullscreen)\n"
                    "- Ensure the window is not minimized\n"
                    "- Try running J.JARAM as Administrator\n\n"
                    "Use hover-based capture instead?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if resp == QMessageBox.StandardButton.Yes:
                    self.autoitem_log_signal.emit(f"[Auto-Item] Hover capture for '{key}' in 3 seconds...")

                    def _hover_capture():
                        try:
                            cx, cy = _wapi.GetCursorPos()
                            ox, oy = _wgui.ClientToScreen(hwnd, (0, 0))
                            _cl, _ct, _cr, _cb = _wgui.GetClientRect(hwnd)
                            ww = int(_cr - _cl)
                            hh = int(_cb - _ct)
                            if ww <= 0 or hh <= 0:
                                raise RuntimeError("Invalid Roblox client size.")

                            rx2 = max(0.0, min(1.0, (cx - ox) / float(ww)))
                            ry2 = max(0.0, min(1.0, (cy - oy) / float(hh)))

                            if key == "conditional_point":
                                self._auto_item_coords["conditional_point"] = {"x": rx2, "y": ry2}
                                self.auto_item_cond_point_le.setText(f"{rx2:.4f}, {ry2:.4f}")
                                if sample_color:
                                    try:
                                        from PIL import ImageGrab as _ig2

                                        px_img = _ig2.grab(bbox=(int(cx), int(cy), int(cx) + 1, int(cy) + 1))
                                        r, g, b = tuple(px_img.getpixel((0, 0))[:3])
                                        self.auto_item_cond_color_le.setText(f"#{int(r):02X}{int(g):02X}{int(b):02X}")
                                    except Exception:
                                        pass
                            else:
                                self._auto_item_coords[key] = {"x": rx2, "y": ry2}
                                le2 = self._auto_item_coord_edits.get(key)
                                if le2:
                                    le2.setText(f"{rx2:.4f}, {ry2:.4f}")

                            self.autoitem_log_signal.emit(f"[Auto-Item] Set '{key}' to ({rx2:.4f}, {ry2:.4f}) via hover.")
                            self._on_auto_item_ui_changed()
                        except Exception as e:
                            self.autoitem_log_signal.emit(f"[Auto-Item] Hover capture failed for '{key}': {e}")

                    QTimer.singleShot(3000, _hover_capture)
                return

            title_map = {
                "inv_button": "Inventory Button",
                "items_tab": "Items Tab",
                "search_box": "Search Box",
                "query_pos": "Query / Result Click",
                "amount_box": "Amount Box",
                "use_button": "Use Button",
                "close_button": "Close Button",
                "conditional_point": "Conditional Point",
            }
            title = f"Select {title_map.get(key, key)}"

            pm = pil_to_pixmap(show_img)
            dlg = PointPickDialog(pm, title, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return

            picked = dlg.selected_point()
            if not picked:
                return
            _xf, _yf, px, py = picked

            # Convert the click back into client-relative coordinates (0..1)
            rx = (float(px) - float(offset_x)) / float(client_crop_w)
            ry = (float(py) - float(offset_y)) / float(client_crop_h)
            if rx < 0.0 or rx > 1.0 or ry < 0.0 or ry > 1.0:
                QMessageBox.warning(self, "Invalid Selection", "Please click inside the Roblox client area.")
                return

            if key == "conditional_point":
                self._auto_item_coords["conditional_point"] = {"x": float(rx), "y": float(ry)}
                self.auto_item_cond_point_le.setText(f"{float(rx):.4f}, {float(ry):.4f}")

                if sample_color:
                    try:
                        rgb_img = show_img.convert("RGB")
                        r, g, b = rgb_img.getpixel((int(px), int(py)))[:3]
                        self.auto_item_cond_color_le.setText(f"#{int(r):02X}{int(g):02X}{int(b):02X}")
                    except Exception:
                        pass
            else:
                self._auto_item_coords[key] = {"x": float(rx), "y": float(ry)}
                le = self._auto_item_coord_edits.get(key)
                if le:
                    le.setText(f"{float(rx):.4f}, {float(ry):.4f}")

            self.autoitem_log_signal.emit(f"[Auto-Item] Set '{key}' to ({float(rx):.4f}, {float(ry):.4f}).")
            self._on_auto_item_ui_changed()
        except Exception as e:
            self.autoitem_log_signal.emit(f"[Auto-Item] Capture failed for '{key}': {e}")

    def _update_auto_item_disable_mouse_tooltip(self) -> None:
        chk = getattr(self, "auto_item_disable_mouse_chk", None)
        if chk is None:
            return
        try:
            if bool(chk.isChecked()):
                chk.setToolTip("User mouse movement is disabled during Auto-Actions.")
            else:
                chk.setToolTip("When enabled, physical mouse movement is blocked during Auto-Actions to prevent misclicks.")
        except Exception:
            pass

    def _on_auto_item_mouse_toggle_changed(self, *_args) -> None:
        self._update_auto_item_disable_mouse_tooltip()
        self._on_auto_item_ui_changed()

    def _on_auto_item_enabled_toggled(self, *_args) -> None:
        if self._loading_autoitem_settings:
            return

        # Live-apply immediately so disabling doesn't continue through other users.
        try:
            if self.auto_item_engine is not None:
                self.auto_item_engine.update_config(self._get_auto_item_settings_from_ui())
        except Exception:
            pass

        self._on_auto_item_ui_changed()

    def _get_auto_item_settings_from_ui(self) -> dict:
        # Selected users
        users = [str(uid).strip() for uid, cb in (self.auto_item_user_checks or {}).items() if cb.isChecked() and str(uid).strip()]

        coords = {}
        for k in ("inv_button", "items_tab", "search_box", "query_pos", "amount_box", "use_button", "close_button"):
            if k in (self._auto_item_coords or {}):
                coords[k] = dict(self._auto_item_coords[k])

        # Conditional click lives under coords.conditional
        cond_pt = (self._auto_item_coords or {}).get("conditional_point")
        coords["conditional"] = {
            "enabled": bool(self.auto_item_cond_enable_chk.isChecked()),
            "point": dict(cond_pt) if isinstance(cond_pt, dict) else {"x": 0.0, "y": 0.0},
            "color": self.auto_item_cond_color_le.text().strip() or "#FFFFFF",
            "tolerance": int(self.auto_item_cond_tol_spin.value()),
        }

        return {
            "enabled": bool(self.auto_item_enable_chk.isChecked()),
            "tick_interval": float(self.auto_item_tick_spin.value()),
            "click_delay": float(self.auto_item_delay_spin.value()),
            "disable_mouse_move": bool(getattr(self, "auto_item_disable_mouse_chk", None) and self.auto_item_disable_mouse_chk.isChecked()),
            "toggle_hotkey": (self.auto_item_hotkey_edit.keySequence().toString().strip() if getattr(self, "auto_item_hotkey_edit", None) else ""),
            "users": users,
            "coords": coords,
            "items": self._current_auto_item_items(),
        }

    def _get_auto_item_cfg_from_disk(self) -> dict:
        try:
            settings = self.config_manager.load_settings() or {}
            return settings.get("auto_item", {}) or {}
        except Exception:
            return {}

    def _apply_auto_item_users_to_ui(self, users: List[str]):
        selected = {str(u) for u in (users or []) if str(u).strip()}
        for uid, cb in (self.auto_item_user_checks or {}).items():
            try:
                cb.setChecked(uid in selected)
            except Exception:
                pass

    def _load_auto_item_settings(self):
        cfg = self._get_auto_item_cfg_from_disk()
        defaults = self.config_manager.default_settings.get("auto_item", {}) or {}
        cfg = {**defaults, **(cfg or {})}
        hk = str(cfg.get("toggle_hotkey", "Ctrl+Alt+Space") or "Ctrl+Alt+Space").strip()

        self._loading_autoitem_settings = True
        try:
            self.auto_item_enable_chk.setChecked(bool(cfg.get("enabled", False)))
            self.auto_item_tick_spin.setValue(float(cfg.get("tick_interval", 1.0) or 1.0))
            self.auto_item_delay_spin.setValue(float(cfg.get("click_delay", 0.2) or 0.2))
            try:
                self.auto_item_disable_mouse_chk.setChecked(bool(cfg.get("disable_mouse_move", False)))
            except Exception:
                self.auto_item_disable_mouse_chk.setChecked(False)
            self._update_auto_item_disable_mouse_tooltip()
            try:
                self.auto_item_hotkey_edit.setKeySequence(QKeySequence(hk))
            except Exception:
                pass

            # Coords
            self._auto_item_coords = {}
            coords = cfg.get("coords", {}) or {}
            for k in ("inv_button", "items_tab", "search_box", "query_pos", "amount_box", "use_button", "close_button"):
                if isinstance(coords.get(k), dict):
                    self._auto_item_coords[k] = {"x": float(coords[k].get("x", 0.0)), "y": float(coords[k].get("y", 0.0))}
                    le = self._auto_item_coord_edits.get(k)
                    if le:
                        le.setText(f"{self._auto_item_coords[k]['x']:.4f}, {self._auto_item_coords[k]['y']:.4f}")
                else:
                    le = self._auto_item_coord_edits.get(k)
                    if le:
                        le.clear()

            cond = coords.get("conditional", {}) or {}
            self.auto_item_cond_enable_chk.setChecked(bool(cond.get("enabled", False)))
            self.auto_item_cond_color_le.setText(str(cond.get("color", "#FFFFFF") or "#FFFFFF"))
            self.auto_item_cond_tol_spin.setValue(int(cond.get("tolerance", 10) or 10))
            pt = cond.get("point") or {}
            if isinstance(pt, dict):
                self._auto_item_coords["conditional_point"] = {"x": float(pt.get("x", 0.0)), "y": float(pt.get("y", 0.0))}
                self.auto_item_cond_point_le.setText(f"{self._auto_item_coords['conditional_point']['x']:.4f}, {self._auto_item_coords['conditional_point']['y']:.4f}")
            else:
                self.auto_item_cond_point_le.clear()

            # Items + users
            self._load_auto_item_items_table(cfg.get("items", []) or [])
            self._apply_auto_item_users_to_ui(cfg.get("users", []) or [])
        finally:
            self._loading_autoitem_settings = False

        # Register hotkey (quiet on load)
        try:
            self._apply_auto_item_hotkey(hk, quiet=True)
        except Exception:
            pass

        # Push config into engine after load
        try:
            if self.auto_item_engine is not None:
                self.auto_item_engine.update_config(self._get_auto_item_settings_from_ui())
        except Exception:
            pass

    def _save_auto_item_settings(self):
        if self._loading_autoitem_settings:
            return
        try:
            settings = self.config_manager.load_settings() or {}
        except Exception:
            settings = self.config_manager.default_settings.copy()

        cfg = self._get_auto_item_settings_from_ui()
        settings["auto_item"] = cfg
        try:
            self.config_manager.save_settings(settings)
        except Exception:
            pass

        # Live-apply to engine
        try:
            if self.auto_item_engine is not None:
                self.auto_item_engine.update_config(cfg)
        except Exception:
            pass

    def _on_auto_item_ui_changed(self):
        if self._loading_autoitem_settings:
            return
        # Debounce writes (and engine updates)
        try:
            self._auto_item_save_timer.start(300)
        except Exception:
            self._save_auto_item_settings()

    def _reset_auto_item_to_defaults(self):
        defaults = self.config_manager.default_settings.get("auto_item", {}) or {}
        hk = str(defaults.get("toggle_hotkey", "Ctrl+Alt+Space") or "Ctrl+Alt+Space").strip()
        self._loading_autoitem_settings = True
        try:
            self.auto_item_enable_chk.setChecked(bool(defaults.get("enabled", False)))
            self.auto_item_tick_spin.setValue(float(defaults.get("tick_interval", 1.0) or 1.0))
            self.auto_item_delay_spin.setValue(float(defaults.get("click_delay", 0.2) or 0.2))
            try:
                self.auto_item_disable_mouse_chk.setChecked(bool(defaults.get("disable_mouse_move", False)))
            except Exception:
                self.auto_item_disable_mouse_chk.setChecked(False)
            self._update_auto_item_disable_mouse_tooltip()
            try:
                self.auto_item_hotkey_edit.setKeySequence(QKeySequence(hk))
            except Exception:
                pass
            self.auto_item_cond_enable_chk.setChecked(False)
            self.auto_item_cond_color_le.setText("#FFFFFF")
            self.auto_item_cond_tol_spin.setValue(10)
            self.auto_item_cond_point_le.clear()
            self._auto_item_coords = {}
            for le in (self._auto_item_coord_edits or {}).values():
                try:
                    le.clear()
                except Exception:
                    pass
            self._load_auto_item_items_table([])
            self._auto_item_set_all_users(False)
        finally:
            self._loading_autoitem_settings = False

        try:
            self._apply_auto_item_hotkey(hk, quiet=True)
        except Exception:
            pass

        self._on_auto_item_ui_changed()

    def _default_auto_item_behavior(self) -> dict:
        return {
            "cooldown": 0,
            "biomes": [],
            "repeat_mode": "repeat",
            "repeat_count": 1,
            "trigger": {"type": "normal", "filter_ids": []},
        }

    def _make_auto_item_preset_actions(
        self,
        legacy_coords: Optional[dict] = None,
        *,
        search_text: str = "ITEM NAME",
        amount_text: str = "1",
    ) -> List[dict]:
        coords = legacy_coords if isinstance(legacy_coords, dict) else {}

        def _point(key: str) -> Optional[dict]:
            raw = coords.get(key)
            if not isinstance(raw, dict):
                return None
            try:
                return {"x": float(raw.get("x", 0.0)), "y": float(raw.get("y", 0.0))}
            except Exception:
                return None

        actions: List[dict] = []
        conditional = coords.get("conditional") if isinstance(coords.get("conditional"), dict) else {}
        cond_enabled = bool((conditional or {}).get("enabled", False))
        cond_point = _point("conditional_point")
        if cond_enabled and cond_point is not None:
            actions.append(
                {
                    "name": "Conditional gate",
                    "type": "conditional_click",
                    "point": cond_point,
                    "color": str((conditional or {}).get("color", "#FFFFFF") or "#FFFFFF"),
                    "tolerance": int((conditional or {}).get("tolerance", 10) or 10),
                }
            )

        ordered = [
            ("Inventory button", "click", _point("inv_button")),
            ("Items tab", "click", _point("items_tab")),
            ("Search box", "click", _point("search_box")),
            ("Paste item", "paste", None),
            ("Query result", "click", _point("query_pos")),
            ("Amount box", "click", _point("amount_box")),
            ("Paste amount", "paste", None),
            ("Use button", "click", _point("use_button")),
            ("Close button", "click", _point("close_button")),
            ("Close button", "click", _point("close_button")),
        ]

        for name, kind, point in ordered:
            if kind == "paste":
                actions.append(
                    {
                        "name": name,
                        "type": "paste",
                        "text": search_text if "item" in name.lower() else amount_text,
                        "select_all": True,
                    }
                )
                continue
            actions.append({"name": name, "type": kind, "point": copy.deepcopy(point)})

        return actions

    def _normalize_auto_item_action(self, raw: Any, *, fallback_name: str = "") -> Optional[dict]:
        if not isinstance(raw, dict):
            return None

        action_type = str(raw.get("type") or raw.get("kind") or raw.get("action_type") or "").strip().lower()
        if action_type == "conditional":
            action_type = "conditional_click"
        if action_type not in ("click", "conditional_click", "paste", "scroll"):
            return None

        point = None
        if isinstance(raw.get("point"), dict):
            try:
                point = {
                    "x": float(raw["point"].get("x", 0.0)),
                    "y": float(raw["point"].get("y", 0.0)),
                }
            except Exception:
                point = None

        try:
            tolerance = max(0, int(raw.get("tolerance", 0) or 0))
        except Exception:
            tolerance = 0

        return {
            "name": str(raw.get("name") or fallback_name or action_type.replace("_", " ").title()).strip()
            or action_type.replace("_", " ").title(),
            "type": action_type,
            "point": point,
            "text": str(raw.get("text") or ""),
            "color": str(raw.get("color") or raw.get("color_hex") or "#FFFFFF").strip() or "#FFFFFF",
            "tolerance": tolerance,
            "select_all": bool(raw.get("select_all", True)),
            "scroll_direction": (
                "up"
                if str(raw.get("scroll_direction") or raw.get("direction") or "").strip().lower() == "up"
                else "down"
            ),
        }

    def _normalize_auto_item_behavior(self, raw: Any, legacy: Optional[dict] = None) -> dict:
        base = raw if isinstance(raw, dict) else {}
        legacy = legacy if isinstance(legacy, dict) else {}
        trigger = base.get("trigger") if isinstance(base.get("trigger"), dict) else {}
        filter_ids = []
        for filter_id in trigger.get("filter_ids", base.get("filter_ids", legacy.get("filter_ids", []))) or []:
            value = str(filter_id or "").strip()
            if value and value not in filter_ids:
                filter_ids.append(value)

        raw_trigger_type = str(trigger.get("type") or base.get("trigger_type") or "").strip().lower()
        if raw_trigger_type in ("normal", "none"):
            trigger_type = "normal"
        elif raw_trigger_type == "ocr_filter":
            trigger_type = "ocr_filter" if filter_ids else "normal"
        else:
            trigger_type = "ocr_filter" if filter_ids else "normal"
        if trigger_type != "ocr_filter":
            filter_ids = []

        repeat_mode = str(base.get("repeat_mode") or legacy.get("repeat_mode") or "repeat").strip().lower()
        if repeat_mode not in ("repeat", "count", "once_per_pid"):
            repeat_mode = "repeat"

        try:
            repeat_count = max(1, int(base.get("repeat_count", legacy.get("repeat_count", 1)) or 1))
        except Exception:
            repeat_count = 1

        try:
            cooldown = max(
                0,
                int(
                    float(
                        base.get(
                            "cooldown",
                            legacy.get("cooldown", legacy.get("cooldown_s", 0)),
                        )
                        or 0
                    )
                ),
            )
        except Exception:
            cooldown = 0

        biomes = []
        for biome in base.get("biomes", legacy.get("biomes", legacy.get("allowed_biomes", []))) or []:
            value = str(biome or "").strip().upper()
            if value and value not in biomes:
                biomes.append(value)

        return {
            "cooldown": cooldown,
            "biomes": biomes,
            "repeat_mode": repeat_mode,
            "repeat_count": repeat_count,
            "trigger": {"type": trigger_type, "filter_ids": filter_ids},
        }

    def _normalize_auto_item_preset(self, raw: Any, *, fallback_index: int = 0) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None

        actions = []
        for idx, action_raw in enumerate(raw.get("actions") or []):
            action = self._normalize_auto_item_action(action_raw, fallback_name=f"Action {idx + 1}")
            if action is not None:
                actions.append(action)

        preset_id = str(raw.get("id") or "").strip() or f"preset_{fallback_index}_{uuid.uuid4().hex[:8]}"
        name = str(raw.get("name") or f"Preset {fallback_index + 1}").strip() or f"Preset {fallback_index + 1}"

        return {
            "id": preset_id,
            "name": name,
            "builtin": bool(raw.get("builtin", preset_id == "item")),
            "actions": actions,
        }

    def _normalize_auto_item_row(self, raw: Any, *, fallback_index: int = 0, legacy_coords: Optional[dict] = None) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None

        actions = []
        for idx, action_raw in enumerate(raw.get("actions") or []):
            action = self._normalize_auto_item_action(action_raw, fallback_name=f"Action {idx + 1}")
            if action is not None:
                actions.append(action)

        if not actions and any(k in raw for k in ("name", "amount", "cooldown", "biomes")):
            actions = self._make_auto_item_preset_actions(
                legacy_coords,
                search_text=str(raw.get("name") or ""),
                amount_text=str(raw.get("amount", 1) or 1),
            )

        if not actions:
            actions = []

        return {
            "enabled": bool(raw.get("enabled", True)),
            "name": str(raw.get("name") or f"Action {fallback_index + 1}").strip() or f"Action {fallback_index + 1}",
            "actions": actions,
            "behavior": self._normalize_auto_item_behavior(raw.get("behavior"), legacy=raw),
            "users": copy.deepcopy(raw.get("users")) if "users" in raw else None,
            "users_explicit": bool(raw.get("users_explicit", False)),
            "alert_enabled": bool(raw.get("alert_enabled", False)),
            "alert_webhook": str(raw.get("alert_webhook") or raw.get("alert_webhook_url") or "").strip(),
            "alert_message": str(raw.get("alert_message") or ""),
            "alert_lead_s": float(raw.get("alert_lead_s", 15.0) or 15.0),
        }

    def _default_auto_item_presets(self, legacy_coords: Optional[dict] = None) -> List[dict]:
        return [
            {
                "id": "item",
                "name": "Item",
                "builtin": True,
                "actions": self._make_auto_item_preset_actions(legacy_coords),
            }
        ]

    def _normalize_auto_item_cfg(self, cfg: Optional[dict]) -> dict:
        base = copy.deepcopy(cfg or {})
        legacy_coords = base.get("coords") if isinstance(base.get("coords"), dict) else {}

        preset_list: List[dict] = []
        seen_preset_ids: set[str] = set()
        for idx, raw in enumerate(base.get("presets") or []):
            preset = self._normalize_auto_item_preset(raw, fallback_index=idx)
            if preset is None:
                continue
            preset_id = str(preset.get("id") or "").strip()
            if not preset_id or preset_id in seen_preset_ids:
                continue
            seen_preset_ids.add(preset_id)
            preset_list.append(preset)

        if "item" not in seen_preset_ids:
            preset_list = self._default_auto_item_presets(legacy_coords) + preset_list

        items: List[dict] = []
        for idx, raw in enumerate(base.get("items") or []):
            row = self._normalize_auto_item_row(raw, fallback_index=idx, legacy_coords=legacy_coords)
            if row is not None:
                items.append(row)

        return {
            "enabled": bool(base.get("enabled", False)),
            "tick_interval": float(base.get("tick_interval", 1.0) or 1.0),
            "click_delay": float(base.get("click_delay", 0.2) or 0.2),
            "disable_mouse_move": bool(base.get("disable_mouse_move", False)),
            "toggle_hotkey": str(base.get("toggle_hotkey", "Ctrl+Alt+Space") or "Ctrl+Alt+Space"),
            "users": [str(uid).strip() for uid in (base.get("users") or []) if str(uid).strip()],
            "presets": preset_list,
            "items": items,
        }

    def _auto_item_filter_catalog(self) -> List[dict]:
        try:
            cfg = self._get_ocr_settings_from_ui() or {}
        except Exception:
            cfg = {}
        out: List[dict] = []
        seen: set[str] = set()
        for idx, spec in enumerate(cfg.get("filters", []) or []):
            if not isinstance(spec, dict):
                continue
            filter_id = str(spec.get("id") or spec.get("filter_id") or "").strip() or f"filter_{idx + 1}"
            if filter_id in seen:
                continue
            seen.add(filter_id)
            out.append(
                {
                    "id": filter_id,
                    "name": str(spec.get("name") or filter_id).strip() or filter_id,
                    "enabled": bool(spec.get("enabled", True)),
                }
            )
        return out

    def _auto_item_filter_name_map(self) -> Dict[str, str]:
        return {str(entry.get("id") or ""): str(entry.get("name") or entry.get("id") or "") for entry in self._auto_item_filter_catalog()}

    def _auto_item_action_summary_text(self, actions: List[dict]) -> str:
        actions = list(actions or [])
        if not actions:
            return "No steps"
        count = len(actions)
        return "1 step" if count == 1 else f"{count} steps"

    def _auto_item_behavior_summary_text(self, behavior: dict) -> str:
        cfg = self._normalize_auto_item_behavior(behavior)
        mode = str(cfg.get("repeat_mode") or "repeat")
        cooldown = int(cfg.get("cooldown", 0) or 0)
        trigger = cfg.get("trigger") or {}
        trigger_type = str(trigger.get("type") or "normal")
        filters = list(trigger.get("filter_ids", [])) if trigger_type == "ocr_filter" else []
        if mode == "count":
            mode_text = f"x{max(1, int(cfg.get('repeat_count', 1) or 1))}"
        elif mode == "once_per_pid":
            mode_text = "1 per PID"
        else:
            mode_text = "Loop"

        if filters:
            filter_text = "1 filter" if len(filters) == 1 else f"{len(filters)} filters"
        else:
            filter_text = "No trigger"

        if cooldown > 0:
            return f"{filter_text} | {mode_text} | {cooldown}s"
        return f"{filter_text} | {mode_text}"

    def _auto_item_actions_tooltip(self, actions: List[dict]) -> str:
        actions = [a for a in (actions or []) if isinstance(a, dict)]
        if not actions:
            return "No steps configured.\nClick to build the action string."
        lines = ["Ordered steps:"]
        for idx, action in enumerate(actions[:8], 1):
            name = str(action.get("name") or action.get("type") or f"Step {idx}").strip()
            kind = str(action.get("type") or "").replace("_", " ").title()
            lines.append(f"{idx}. {name} [{kind}]")
        if len(actions) > 8:
            lines.append(f"... +{len(actions) - 8} more")
        lines.append("")
        lines.append("Click to edit the sequence.")
        return "\n".join(lines)

    def _auto_item_behavior_tooltip(self, behavior: dict) -> str:
        cfg = self._normalize_auto_item_behavior(behavior)
        filter_names = self._auto_item_filter_name_map()
        trigger = cfg.get("trigger") or {}
        trigger_type = str(trigger.get("type") or "normal")
        filters = [str(fid).strip() for fid in (trigger.get("filter_ids", []) or []) if str(fid).strip()]
        if trigger_type != "ocr_filter":
            filters = []
        biomes = [str(b).strip().upper() for b in (cfg.get("biomes") or []) if str(b).strip()]
        mode = str(cfg.get("repeat_mode") or "repeat")
        if mode == "count":
            mode_text = f"Play count: {max(1, int(cfg.get('repeat_count', 1) or 1))}"
        elif mode == "once_per_pid":
            mode_text = "Play mode: Once per PID"
        else:
            mode_text = "Play mode: Repeatedly"
        lines = [
            mode_text,
            f"Cooldown: {int(cfg.get('cooldown', 0) or 0)}s",
            "Trigger: OCR filter" if trigger_type == "ocr_filter" else "Trigger: Normal",
        ]
        if filters:
            lines.append("Bound filters: " + ", ".join(str(filter_names.get(fid, fid) or fid) for fid in filters))
        else:
            lines.append("Bound filters: none")
        lines.append("Biomes: " + ("Any biome" if not biomes else ", ".join(biomes)))
        lines.append("")
        lines.append("Click to edit behavior.")
        return "\n".join(lines)

    def _auto_item_apply_cell_button_style(self, btn: QPushButton, *, accent: bool = False) -> None:
        try:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        btn.setMinimumHeight(42)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        border_color = ModernStyle.PRIMARY if accent else ModernStyle.BORDER
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_PRIMARY};
                border: 1px solid {border_color};
                border-radius: 9px;
                padding: 8px 12px;
                font-weight: 600;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: {ModernStyle.SURFACE};
                border-color: {ModernStyle.PRIMARY};
            }}
            QPushButton:pressed {{
                background-color: {ModernStyle.SURFACE};
            }}
            QPushButton:disabled {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_SECONDARY};
                border-color: {ModernStyle.BORDER};
            }}
            """
        )

    def _get_primary_button_style(self) -> str:
        return f"""
            QPushButton {{
                background-color: {ModernStyle.PRIMARY};
                color: {ModernStyle.TEXT_PRIMARY};
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                font-weight: 500;
                font-size: 13px;
                min-width: 80px;
                min-height: 28px;
            }}
            QPushButton:hover {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}
            QPushButton:pressed {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}
            QPushButton:disabled {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_SECONDARY};
            }}
        """

    def _get_secondary_button_style(self) -> str:
        return f"""
            QPushButton {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_PRIMARY};
                border: 2px solid {ModernStyle.BORDER};
                padding: 8px 16px;
                border-radius: 6px;
                font-weight: 500;
                font-size: 13px;
                min-width: 80px;
                min-height: 28px;
            }}
            QPushButton:hover {{
                background-color: {ModernStyle.SURFACE};
                border-color: {ModernStyle.PRIMARY};
            }}
            QPushButton:pressed {{
                background-color: {ModernStyle.SURFACE};
            }}
            QPushButton:disabled {{
                color: {ModernStyle.TEXT_SECONDARY};
                border-color: {ModernStyle.BORDER};
            }}
        """

    def _auto_item_apply_dialog_style(self, dlg: QWidget) -> None:
        dlg.setStyleSheet(
            f"""
            QDialog {{
                background-color: {ModernStyle.BACKGROUND};
                color: {ModernStyle.TEXT_PRIMARY};
            }}
            QListWidget {{
                background-color: {ModernStyle.SURFACE};
                border: 2px solid {ModernStyle.BORDER};
                border-radius: 6px;
                color: {ModernStyle.TEXT_PRIMARY};
            }}
            QListWidget::item {{
                padding: 6px 8px;
            }}
            QListWidget::item:hover {{
                background-color: {ModernStyle.SURFACE_VARIANT};
            }}
            QListWidget::item:selected {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
                color: {ModernStyle.TEXT_PRIMARY};
            }}
            QListWidget::item:selected:!active {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
                color: {ModernStyle.TEXT_PRIMARY};
            }}
            QLabel[role="hint"] {{
                color: {ModernStyle.TEXT_SECONDARY};
                background: transparent;
            }}
            QLabel[role="section"] {{
                color: {ModernStyle.TEXT_PRIMARY};
                font-weight: 700;
                background: transparent;
            }}
            """
        )

    def _auto_item_apply_main_table_style(self, table: QTableWidget) -> None:
        table.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {ModernStyle.SURFACE};
                border: 1px solid {ModernStyle.BORDER};
                border-radius: 12px;
                gridline-color: transparent;
                selection-background-color: {ModernStyle.PRIMARY_VARIANT};
            }}
            QTableWidget::item {{
                border: none;
                padding: 0px;
            }}
            QHeaderView::section {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_PRIMARY};
                padding: 12px 10px;
                border: none;
                font-weight: 700;
            }}
            QTableWidget QLineEdit {{
                background-color: {ModernStyle.BACKGROUND};
                border: 1px solid {ModernStyle.BORDER};
                border-radius: 9px;
                padding: 8px 12px;
                min-height: 24px;
                color: {ModernStyle.TEXT_PRIMARY};
            }}
            QTableWidget QLineEdit:focus {{
                border-color: {ModernStyle.PRIMARY};
            }}
            QTableWidget QCheckBox {{
                background: transparent;
                margin: 0px;
                padding: 0px;
            }}
            """
        )

    def _update_actions_btn_text(self, btn: QPushButton) -> None:
        actions = btn.property("actions") or []
        if not isinstance(actions, list):
            actions = []
        btn.setProperty("actions", copy.deepcopy(actions))
        btn.setText(self._auto_item_action_summary_text(actions))
        btn.setToolTip(self._auto_item_actions_tooltip(actions))

    def _update_behavior_btn_text(self, btn: QPushButton) -> None:
        behavior = self._normalize_auto_item_behavior(btn.property("behavior"))
        btn.setProperty("behavior", copy.deepcopy(behavior))
        btn.setText(self._auto_item_behavior_summary_text(behavior))
        btn.setToolTip(self._auto_item_behavior_tooltip(behavior))

    def _capture_auto_item_point(self, *, sample_color: bool = False) -> Optional[dict]:
        try:
            import ctypes
            import psutil
            import win32api as _wapi
            import win32con as _wcon
            import win32gui as _wgui
            import win32process as _wproc
        except Exception as e:
            QMessageBox.warning(self, "Capture Failed", f"Missing dependencies for capture:\n{e}")
            return None

        def _is_roblox_hwnd(hwnd: int) -> bool:
            try:
                if not hwnd or not _wgui.IsWindow(hwnd):
                    return False
                _, pid = _wproc.GetWindowThreadProcessId(hwnd)
                if not pid:
                    return False
                return str(psutil.Process(int(pid)).name()).lower() == "robloxplayerbeta.exe"
            except Exception:
                return False

        def _pick_hwnd() -> Optional[int]:
            try:
                fg = _wgui.GetForegroundWindow()
                if _is_roblox_hwnd(fg):
                    return int(fg)
            except Exception:
                pass
            try:
                wins = enum_roblox_windows()
                if wins:
                    return int(wins[0].hwnd)
            except Exception:
                pass
            return None

        def _bring_foreground(hwnd: int) -> None:
            try:
                if _wgui.IsIconic(hwnd):
                    _wgui.ShowWindow(hwnd, _wcon.SW_RESTORE)
                cur_tid = _wapi.GetCurrentThreadId()
                win_tid = _wproc.GetWindowThreadProcessId(hwnd)[0]
                ctypes.windll.user32.AttachThreadInput(cur_tid, win_tid, True)
                try:
                    _wgui.BringWindowToTop(hwnd)
                    _wgui.SetForegroundWindow(hwnd)
                finally:
                    ctypes.windll.user32.AttachThreadInput(cur_tid, win_tid, False)
            except Exception:
                try:
                    _wgui.SetForegroundWindow(hwnd)
                except Exception:
                    pass

        def _is_blackish(im: Image.Image) -> bool:
            try:
                _lo, _hi = im.convert("L").getextrema()
                return int(_hi) <= 5
            except Exception:
                return False

        hwnd = _pick_hwnd()
        if not hwnd:
            QMessageBox.warning(self, "Capture Failed", "No Roblox window was found.")
            return None

        try:
            _bring_foreground(hwnd)
            time.sleep(0.12)

            _l, _t, cr, cb = _wgui.GetClientRect(hwnd)
            client_w = int(cr - _l)
            client_h = int(cb - _t)
            if client_w <= 0 or client_h <= 0:
                raise RuntimeError("Invalid Roblox client size.")

            full = capture_window_image(hwnd)
            if full is None:
                raise RuntimeError("Failed to capture Roblox window.")

            if _is_blackish(full):
                try:
                    from PIL import ImageGrab as _ig

                    wl, wt, wr, wb = _wgui.GetWindowRect(hwnd)
                    alt = _ig.grab(bbox=(wl, wt, wr, wb))
                    if alt is not None and not _is_blackish(alt):
                        full = alt
                except Exception:
                    pass

            wl, wt, wr, wb = _wgui.GetWindowRect(hwnd)
            win_w = max(1, int(wr - wl))
            win_h = max(1, int(wb - wt))
            scale_x = float(full.width) / float(win_w) if win_w else 1.0
            scale_y = float(full.height) / float(win_h) if win_h else 1.0

            client_left, client_top = _wgui.ClientToScreen(hwnd, (0, 0))
            crop_left = int((client_left - wl) * scale_x)
            crop_top = int((client_top - wt) * scale_y)
            crop_right = int(crop_left + (client_w * scale_x))
            crop_bottom = int(crop_top + (client_h * scale_y))

            crop_left = max(0, min(full.width - 1, crop_left))
            crop_top = max(0, min(full.height - 1, crop_top))
            crop_right = max(crop_left + 1, min(full.width, crop_right))
            crop_bottom = max(crop_top + 1, min(full.height, crop_bottom))

            client_crop_w = max(1, crop_right - crop_left)
            client_crop_h = max(1, crop_bottom - crop_top)
            client_img = full.crop((crop_left, crop_top, crop_right, crop_bottom))

            show_img = client_img
            offset_x = 0
            offset_y = 0
            if _is_blackish(client_img) and not _is_blackish(full):
                show_img = full
                offset_x = crop_left
                offset_y = crop_top

            if _is_blackish(show_img):
                QMessageBox.warning(
                    self,
                    "Capture Failed",
                    "Roblox screenshot capture returned a black image.\n\nTry windowed/borderless mode and capture again.",
                )
                return None

            pm = pil_to_pixmap(show_img)
            dlg = PointPickDialog(pm, "Capture Point", parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return None

            picked = dlg.selected_point()
            if not picked:
                return None
            _xf, _yf, px, py = picked

            rx = (float(px) - float(offset_x)) / float(client_crop_w)
            ry = (float(py) - float(offset_y)) / float(client_crop_h)
            if rx < 0.0 or rx > 1.0 or ry < 0.0 or ry > 1.0:
                QMessageBox.warning(self, "Invalid Selection", "Please click inside the Roblox client area.")
                return None

            color = None
            if sample_color:
                try:
                    rgb_img = show_img.convert("RGB")
                    r, g, b = rgb_img.getpixel((int(px), int(py)))[:3]
                    color = f"#{int(r):02X}{int(g):02X}{int(b):02X}"
                except Exception:
                    color = None

            return {
                "point": {"x": float(rx), "y": float(ry)},
                "color": color,
            }
        except Exception as e:
            QMessageBox.warning(self, "Capture Failed", str(e))
            return None

    def _edit_auto_item_action_step_dialog(self, action_type: str, current: Optional[dict] = None) -> Optional[dict]:
        action = self._normalize_auto_item_action(current or {"type": action_type}, fallback_name=action_type.title())
        if action is None:
            action = {
                "name": action_type.replace("_", " ").title(),
                "type": action_type,
                "point": None,
                "text": "",
                "color": "#FFFFFF",
                "tolerance": 10,
                "select_all": True,
                "scroll_direction": "down",
            }

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit {action_type.replace('_', ' ').title()} Action")
        dlg.resize(620, 420 if action_type in ("paste", "scroll") else 360)
        self._auto_item_apply_dialog_style(dlg)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)

        intro_map = {
            "click": "A click action taps a captured point whenever the row executes.",
            "conditional_click": "A conditional click only fires when the sampled color matches the captured point.",
            "paste": "A paste action sends text into the active field as part of the row sequence.",
            "scroll": "A scroll action moves the mouse to a captured point and scrolls either up or down there.",
        }
        intro = QLabel(str(intro_map.get(action_type, "Configure the action step.")))
        intro.setProperty("role", "hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        basics_group = QGroupBox("Action Identity")
        basics_form = QFormLayout(basics_group)
        name_le = QLineEdit(str(action.get("name") or ""))
        name_le.setPlaceholderText(action_type.replace("_", " ").title())
        try:
            name_le.setClearButtonEnabled(True)
        except Exception:
            pass
        basics_form.addRow("Action name:", name_le)
        layout.addWidget(basics_group)

        if action_type in ("click", "conditional_click", "scroll"):
            target_group = QGroupBox("Target")
            target_layout = QVBoxLayout(target_group)
            target_hint = QLabel("Capture the exact on-screen point this action should use.")
            target_hint.setProperty("role", "hint")
            target_hint.setWordWrap(True)
            target_layout.addWidget(target_hint)

            point_le = QLineEdit()
            point_le.setReadOnly(True)
            point_le.setPlaceholderText("No point captured yet")
            point = action.get("point") if isinstance(action.get("point"), dict) else None
            if isinstance(point, dict):
                point_le.setText(f"{float(point.get('x', 0.0)):.4f}, {float(point.get('y', 0.0)):.4f}")
            capture_btn = QPushButton("Capture Point")
            capture_btn.setToolTip("Minimize the app overlay and click inside the game window to sample a point.")
            capture_btn.setStyleSheet(self._get_primary_button_style())

            def _capture() -> None:
                result = self._capture_auto_item_point(sample_color=(action_type == "conditional_click"))
                if not result:
                    return
                point_val = result.get("point")
                if isinstance(point_val, dict):
                    action["point"] = {
                        "x": float(point_val.get("x", 0.0)),
                        "y": float(point_val.get("y", 0.0)),
                    }
                    point_le.setText(f"{action['point']['x']:.4f}, {action['point']['y']:.4f}")
                if action_type == "conditional_click" and result.get("color"):
                    color_le.setText(str(result.get("color") or "#FFFFFF"))

            capture_btn.clicked.connect(_capture)
            row = QHBoxLayout()
            row.addWidget(point_le, 1)
            row.addWidget(capture_btn)
            holder = QWidget()
            holder.setLayout(row)
            target_layout.addWidget(holder)
            layout.addWidget(target_group)

        if action_type == "conditional_click":
            cond_group = QGroupBox("Condition")
            cond_layout = QVBoxLayout(cond_group)
            cond_hint = QLabel("The click only runs when the sampled pixel color matches within the chosen tolerance.")
            cond_hint.setProperty("role", "hint")
            cond_hint.setWordWrap(True)
            cond_layout.addWidget(cond_hint)
            cond_form = QFormLayout()
            color_le = QLineEdit(str(action.get("color") or "#FFFFFF"))
            color_le.setPlaceholderText("#FFFFFF")
            cond_form.addRow("Expected color:", color_le)
            tolerance_spin = QSpinBox()
            tolerance_spin.setRange(0, 255)
            tolerance_spin.setValue(int(action.get("tolerance", 10) or 10))
            cond_form.addRow("Tolerance:", tolerance_spin)
            cond_layout.addLayout(cond_form)
            layout.addWidget(cond_group)

        if action_type == "scroll":
            scroll_group = QGroupBox("Scroll")
            scroll_layout = QVBoxLayout(scroll_group)
            scroll_hint = QLabel("Choose whether this step scrolls upward or downward at the captured point.")
            scroll_hint.setProperty("role", "hint")
            scroll_hint.setWordWrap(True)
            scroll_layout.addWidget(scroll_hint)
            scroll_form = QFormLayout()
            scroll_direction_combo = QComboBox()
            scroll_direction_combo.addItem("Up", "up")
            scroll_direction_combo.addItem("Down", "down")
            scroll_direction_combo.setCurrentIndex(
                max(0, scroll_direction_combo.findData(str(action.get("scroll_direction") or "down")))
            )
            scroll_form.addRow("Direction:", scroll_direction_combo)
            scroll_layout.addLayout(scroll_form)
            layout.addWidget(scroll_group)

        if action_type == "paste":
            paste_group = QGroupBox("Paste Content")
            paste_layout = QVBoxLayout(paste_group)
            paste_hint = QLabel("Enter the exact text this step should send. Multi-line text is supported.")
            paste_hint.setProperty("role", "hint")
            paste_hint.setWordWrap(True)
            paste_layout.addWidget(paste_hint)
            text_le = QTextEdit()
            text_le.setAcceptRichText(False)
            text_le.setPlaceholderText("Text to paste when this step runs")
            text_le.setPlainText(str(action.get("text") or ""))
            text_le.setMinimumHeight(120)
            text_le.setMaximumHeight(180)
            paste_layout.addWidget(text_le)
            select_all_chk = QCheckBox("Select all before paste")
            select_all_chk.setChecked(bool(action.get("select_all", True)))
            paste_layout.addWidget(select_all_chk)
            layout.addWidget(paste_group)

        layout.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        out = {
            "name": name_le.text().strip() or action_type.replace("_", " ").title(),
            "type": action_type,
            "point": copy.deepcopy(action.get("point")),
            "text": "",
            "color": "#FFFFFF",
            "tolerance": 0,
            "select_all": True,
            "scroll_direction": "down",
        }
        if action_type == "conditional_click":
            out["color"] = color_le.text().strip() or "#FFFFFF"
            out["tolerance"] = int(tolerance_spin.value())
        if action_type == "scroll":
            out["scroll_direction"] = str(scroll_direction_combo.currentData() or "down")
        if action_type == "paste":
            out["text"] = text_le.toPlainText()
            out["select_all"] = bool(select_all_chk.isChecked())

        return out

    def _edit_auto_item_actions_dialog(self, actions: List[dict], *, title: str = "Actions") -> Optional[List[dict]]:
        working = [copy.deepcopy(a) for a in (actions or []) if isinstance(a, dict)]

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(920, 560)
        self._auto_item_apply_dialog_style(dlg)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)

        intro = QLabel("Build the ordered action string for this row. Use short names so the sequence stays easy to scan.")
        intro.setProperty("role", "hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Name", "Type", "Details"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.verticalHeader().setVisible(False)
        table.setMinimumHeight(300)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._auto_item_apply_main_table_style(table)
        layout.addWidget(table)

        def _details(action: dict) -> str:
            kind = str(action.get("type") or "")
            if kind == "paste":
                text = str(action.get("text") or "")
                text = text.replace("\r", " ").replace("\n", " ")
                if len(text) > 48:
                    text = text[:45] + "..."
                return f"Paste '{text}'"
            point = action.get("point") if isinstance(action.get("point"), dict) else None
            if kind == "scroll":
                direction = "Up" if str(action.get("scroll_direction") or "down").strip().lower() == "up" else "Down"
                if isinstance(point, dict):
                    return f"Scroll {direction} at {float(point.get('x', 0.0)):.4f}, {float(point.get('y', 0.0)):.4f}"
                return f"Scroll {direction} (no point)"
            if kind == "conditional_click":
                if isinstance(point, dict):
                    return (
                        f"{float(point.get('x', 0.0)):.4f}, {float(point.get('y', 0.0)):.4f} "
                        f"{str(action.get('color') or '#FFFFFF')}"
                    )
                return "No point"
            if isinstance(point, dict):
                return f"{float(point.get('x', 0.0)):.4f}, {float(point.get('y', 0.0)):.4f}"
            return "No point"

        def _refresh() -> None:
            table.setRowCount(len(working))
            for row, action in enumerate(working):
                try:
                    table.setRowHeight(row, 46)
                except Exception:
                    pass
                name_item = QTableWidgetItem(str(action.get("name") or ""))
                type_item = QTableWidgetItem(str(action.get("type") or "").replace("_", " ").title())
                detail_text = _details(action)
                detail_item = QTableWidgetItem(detail_text)
                for item in (name_item, type_item, detail_item):
                    item.setToolTip(detail_text if item is detail_item else str(item.text() or ""))
                table.setItem(row, 0, name_item)
                table.setItem(row, 1, type_item)
                table.setItem(row, 2, detail_item)

        def _current_row() -> int:
            row = table.currentRow()
            if row < 0 and working:
                row = 0
            return row

        add_row = QHBoxLayout()
        add_click_btn = QPushButton("Add Click")
        add_cond_btn = QPushButton("Add Conditional")
        add_paste_btn = QPushButton("Add Paste")
        add_scroll_btn = QPushButton("Add Scroll")
        edit_btn = QPushButton("Edit")
        remove_btn = QPushButton("Remove")
        up_btn = QPushButton("Move Up")
        down_btn = QPushButton("Move Down")
        add_click_btn.setStyleSheet(self._get_primary_button_style())
        for btn in (add_cond_btn, add_paste_btn, add_scroll_btn, edit_btn, remove_btn, up_btn, down_btn):
            btn.setStyleSheet(self._get_secondary_button_style())
        for btn in (add_click_btn, add_cond_btn, add_paste_btn, add_scroll_btn):
            add_row.addWidget(btn)
        add_row.addStretch()
        layout.addLayout(add_row)

        manage_row = QHBoxLayout()
        for btn in (edit_btn, remove_btn, up_btn, down_btn):
            manage_row.addWidget(btn)
        manage_row.addStretch()
        layout.addLayout(manage_row)

        helper = QLabel("Double-click any step to edit it. Reorder with Move Up and Move Down.")
        helper.setProperty("role", "hint")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        def _add(kind: str) -> None:
            result = self._edit_auto_item_action_step_dialog(kind)
            if result is None:
                return
            working.append(result)
            _refresh()
            table.setCurrentCell(max(0, len(working) - 1), 0)

        def _edit_selected() -> None:
            row = _current_row()
            if row < 0 or row >= len(working):
                return
            current = working[row]
            result = self._edit_auto_item_action_step_dialog(str(current.get("type") or ""), current)
            if result is None:
                return
            working[row] = result
            _refresh()
            table.setCurrentCell(row, 0)

        def _move(delta: int) -> None:
            row = _current_row()
            new_row = row + int(delta)
            if row < 0 or new_row < 0 or new_row >= len(working):
                return
            working[row], working[new_row] = working[new_row], working[row]
            _refresh()
            table.setCurrentCell(new_row, 0)

        def _remove_selected() -> None:
            row = _current_row()
            if row < 0 or row >= len(working):
                return
            working.pop(row)
            _refresh()
            if working:
                table.setCurrentCell(max(0, min(row, len(working) - 1)), 0)

        add_click_btn.clicked.connect(lambda: _add("click"))
        add_cond_btn.clicked.connect(lambda: _add("conditional_click"))
        add_paste_btn.clicked.connect(lambda: _add("paste"))
        add_scroll_btn.clicked.connect(lambda: _add("scroll"))
        edit_btn.clicked.connect(_edit_selected)
        remove_btn.clicked.connect(_remove_selected)
        up_btn.clicked.connect(lambda: _move(-1))
        down_btn.clicked.connect(lambda: _move(1))
        table.doubleClicked.connect(lambda *_args: _edit_selected())

        _refresh()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return [copy.deepcopy(a) for a in working]

    def _edit_item_actions(self, btn: QPushButton) -> None:
        current = btn.property("actions") or []
        if not isinstance(current, list):
            current = []
        updated = self._edit_auto_item_actions_dialog(current, title="Edit Actions")
        if updated is None:
            return
        btn.setProperty("actions", copy.deepcopy(updated))
        self._update_actions_btn_text(btn)
        self._on_auto_item_ui_changed()

    def _edit_item_behavior(self, btn: QPushButton) -> None:
        current = self._normalize_auto_item_behavior(btn.property("behavior"))
        filters = self._auto_item_filter_catalog()

        dlg = QDialog(self)
        dlg.setWindowTitle("Action Behavior")
        dlg.resize(920, 620)
        self._auto_item_apply_dialog_style(dlg)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)

        intro = QLabel(
            "Define when this row is allowed to run, how often it repeats, and which OCR filters can trigger it."
        )
        intro.setProperty("role", "hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        layout.addLayout(top_row)

        playback_group = QGroupBox("Playback")
        playback_layout = QVBoxLayout(playback_group)
        form = QFormLayout()
        cooldown_spin = _AutoItemSpinBox()
        cooldown_spin.setRange(0, 86400)
        cooldown_spin.setValue(int(current.get("cooldown", 0) or 0))
        form.addRow("Cooldown (s):", cooldown_spin)

        repeat_combo = QComboBox()
        repeat_combo.addItem("Repeatedly", "repeat")
        repeat_combo.addItem("Set amount of times", "count")
        repeat_combo.addItem("Once per PID", "once_per_pid")
        idx = max(0, repeat_combo.findData(str(current.get("repeat_mode") or "repeat")))
        repeat_combo.setCurrentIndex(idx)
        form.addRow("Play mode:", repeat_combo)

        repeat_count_holder = QWidget()
        repeat_count_holder_layout = QHBoxLayout(repeat_count_holder)
        repeat_count_holder_layout.setContentsMargins(0, 0, 0, 0)
        repeat_count_holder_layout.setSpacing(0)
        repeat_count_spin = _AutoItemSpinBox()
        repeat_count_spin.setRange(1, 999)
        repeat_count_spin.setValue(int(current.get("repeat_count", 1) or 1))
        repeat_count_blank = QLineEdit()
        repeat_count_blank.setReadOnly(True)
        repeat_count_blank.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        repeat_count_blank.setText("")
        repeat_count_holder_layout.addWidget(repeat_count_spin)
        repeat_count_holder_layout.addWidget(repeat_count_blank)
        form.addRow("Play count:", repeat_count_holder)
        playback_layout.addLayout(form)
        mode_hint = QLabel()
        mode_hint.setProperty("role", "hint")
        mode_hint.setWordWrap(True)
        playback_layout.addWidget(mode_hint)
        top_row.addWidget(playback_group, 1)

        trigger_group = QGroupBox("Trigger")
        trigger_layout = QVBoxLayout(trigger_group)
        trigger_combo = QComboBox()
        trigger_combo.addItem("Normal", "normal")
        trigger_combo.addItem("OCR filter triggered", "ocr_filter")
        trigger_idx = max(0, trigger_combo.findData(str((current.get("trigger") or {}).get("type") or "normal")))
        trigger_combo.setCurrentIndex(trigger_idx)
        trigger_form = QFormLayout()
        trigger_form.addRow("Trigger type:", trigger_combo)
        trigger_layout.addLayout(trigger_form)
        trigger_hint = QLabel()
        trigger_hint.setProperty("role", "hint")
        trigger_hint.setWordWrap(True)
        trigger_layout.addWidget(trigger_hint)
        trigger_layout.addStretch(1)
        top_row.addWidget(trigger_group, 1)

        biome_group = QGroupBox("Allowed Biomes")
        biome_layout = QVBoxLayout(biome_group)
        biome_btn_row = QHBoxLayout()
        biome_all_btn = QPushButton("Select All")
        biome_none_btn = QPushButton("Select None")
        biome_all_btn.setStyleSheet(self._get_secondary_button_style())
        biome_none_btn.setStyleSheet(self._get_secondary_button_style())
        biome_btn_row.addWidget(biome_all_btn)
        biome_btn_row.addWidget(biome_none_btn)
        biome_btn_row.addStretch()
        biome_layout.addLayout(biome_btn_row)
        biome_list = QListWidget()
        biome_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        biome_list.setMinimumWidth(240)
        biome_list.setMinimumHeight(280)
        current_biomes = {str(b).strip().upper() for b in (current.get("biomes") or []) if str(b).strip()}
        try:
            all_biomes = list(biome_names())
        except Exception:
            all_biomes = []
        for biome in all_biomes:
            item = QListWidgetItem(str(biome).upper())
            biome_list.addItem(item)
            if item.text() in current_biomes:
                item.setSelected(True)
        biome_all_btn.clicked.connect(lambda: biome_list.selectAll())
        biome_none_btn.clicked.connect(lambda: biome_list.clearSelection())
        biome_layout.addWidget(biome_list)

        filter_group = QGroupBox("OCR Filter Bindings")
        filter_layout = QVBoxLayout(filter_group)
        filter_hint = QLabel("Choose which OCR filters will trigger this action row.")
        filter_hint.setWordWrap(True)
        filter_hint.setProperty("role", "hint")
        filter_layout.addWidget(filter_hint)
        filter_btn_row = QHBoxLayout()
        filter_btn_row.setSpacing(8)
        filter_all_btn = QPushButton("Select All")
        filter_none_btn = QPushButton("Select None")
        filter_refresh_btn = QPushButton("Refresh")
        filter_all_btn.setStyleSheet(self._get_secondary_button_style())
        filter_none_btn.setStyleSheet(self._get_secondary_button_style())
        filter_refresh_btn.setStyleSheet(self._get_secondary_button_style())
        filter_btn_row.addWidget(filter_all_btn)
        filter_btn_row.addWidget(filter_none_btn)
        filter_btn_row.addWidget(filter_refresh_btn)
        filter_btn_row.addStretch()
        filter_layout.addLayout(filter_btn_row)
        filter_list = QListWidget()
        filter_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        filter_list.setMinimumWidth(320)
        filter_list.setMinimumHeight(280)
        current_filters = {
            str(fid).strip()
            for fid in ((current.get("trigger") or {}).get("filter_ids", []) or [])
            if str(fid).strip()
        }
        filter_refresh_initialized = False
        filter_all_btn.clicked.connect(lambda: filter_list.selectAll())
        filter_none_btn.clicked.connect(lambda: filter_list.clearSelection())
        filter_layout.addWidget(filter_list)

        lists_row = QHBoxLayout()
        lists_row.setSpacing(12)
        lists_row.addWidget(biome_group, 1)
        lists_row.addWidget(filter_group, 1)
        layout.addLayout(lists_row)

        def _sync_repeat_count_state() -> None:
            mode = str(repeat_combo.currentData() or "repeat")
            is_count = mode == "count"
            repeat_count_spin.setVisible(is_count)
            repeat_count_blank.setVisible(not is_count)
            if mode == "count":
                mode_hint.setText("This row will run its sequence exactly the number of times shown above for each trigger.")
            elif mode == "once_per_pid":
                mode_hint.setText("This row will only run once for each detected Roblox PID until the process changes.")
            else:
                mode_hint.setText("This row can keep firing whenever its trigger conditions are met and cooldown permits it.")

        def _sync_trigger_state() -> None:
            is_ocr = str(trigger_combo.currentData() or "normal") == "ocr_filter"
            has_filters = bool(filters)
            filter_list.setEnabled(is_ocr and has_filters)
            filter_all_btn.setEnabled(is_ocr and has_filters)
            filter_none_btn.setEnabled(is_ocr and has_filters)
            if not has_filters:
                filter_hint.setText("No OCR filters are available yet. Configure OCR filters first, then bind them here.")
            elif is_ocr:
                filter_hint.setText("Choose which OCR filters will trigger this action row.")
            else:
                filter_hint.setText("Normal mode ignores OCR filters and runs when cooldown and biome checks allow it.")
            trigger_hint.setText(
                "Normal mode runs on its own cooldown. OCR filter mode waits for any bound OCR filter to fire."
            )

        def _refresh_filter_bindings() -> None:
            nonlocal filters, filter_refresh_initialized
            selected_ids = {
                str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
                for item in filter_list.selectedItems()
                if str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            }
            if not selected_ids and not filter_refresh_initialized:
                selected_ids = set(current_filters)

            filters = self._auto_item_filter_catalog()
            filter_list.clear()
            if not filters:
                empty_item = QListWidgetItem("No OCR filters configured yet")
                empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
                filter_list.addItem(empty_item)
            else:
                for spec in filters:
                    filter_id = str(spec.get("id") or "").strip()
                    label = str(spec.get("name") or filter_id or "").strip()
                    if not bool(spec.get("enabled", True)):
                        label += " (disabled)"
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, filter_id)
                    item.setToolTip(f"{label}\nID: {filter_id}")
                    filter_list.addItem(item)
                    if filter_id in selected_ids:
                        item.setSelected(True)
            filter_refresh_initialized = True
            _sync_trigger_state()

        repeat_combo.currentIndexChanged.connect(_sync_repeat_count_state)
        trigger_combo.currentIndexChanged.connect(_sync_trigger_state)
        filter_refresh_btn.clicked.connect(_refresh_filter_bindings)
        _refresh_filter_bindings()
        _sync_repeat_count_state()
        _sync_trigger_state()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        chosen_filters = []
        for item in filter_list.selectedItems():
            value = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if value and value not in chosen_filters:
                chosen_filters.append(value)

        chosen_biomes = []
        for item in biome_list.selectedItems():
            value = str(item.text() or "").strip().upper()
            if value and value not in chosen_biomes:
                chosen_biomes.append(value)

        btn.setProperty(
            "behavior",
            {
                "cooldown": int(cooldown_spin.value()),
                "biomes": chosen_biomes,
                "repeat_mode": str(repeat_combo.currentData() or "repeat"),
                "repeat_count": int(repeat_count_spin.value()) if str(repeat_combo.currentData() or "repeat") == "count" else 1,
                "trigger": {
                    "type": str(trigger_combo.currentData() or "normal"),
                    "filter_ids": chosen_filters if str(trigger_combo.currentData() or "normal") == "ocr_filter" else [],
                },
            },
        )
        self._update_behavior_btn_text(btn)
        self._on_auto_item_ui_changed()

    def _edit_auto_item_preset_dialog(self, preset: Optional[dict] = None) -> Optional[dict]:
        current = self._normalize_auto_item_preset(preset or {}, fallback_index=0) or {
            "id": f"preset_{uuid.uuid4().hex[:8]}",
            "name": "New Preset",
            "builtin": False,
            "actions": [],
        }
        actions = [copy.deepcopy(a) for a in (current.get("actions") or []) if isinstance(a, dict)]

        dlg = QDialog(self)
        dlg.setWindowTitle("Preset")
        dlg.resize(560, 300)
        self._auto_item_apply_dialog_style(dlg)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)

        intro = QLabel("Save a reusable action string that can be inserted into the table later.")
        intro.setProperty("role", "hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        identity_group = QGroupBox("Preset Identity")
        form = QFormLayout(identity_group)
        name_le = QLineEdit(str(current.get("name") or ""))
        name_le.setPlaceholderText("Preset name")
        form.addRow("Preset name:", name_le)
        layout.addWidget(identity_group)

        sequence_group = QGroupBox("Sequence")
        sequence_layout = QVBoxLayout(sequence_group)
        summary_lbl = QLabel(self._auto_item_action_summary_text(actions))
        summary_lbl.setProperty("role", "section")
        summary_lbl.setToolTip(self._auto_item_actions_tooltip(actions))
        sequence_layout.addWidget(summary_lbl)
        sequence_hint = QLabel("Edit the underlying steps without leaving this preset dialog.")
        sequence_hint.setProperty("role", "hint")
        sequence_hint.setWordWrap(True)
        sequence_layout.addWidget(sequence_hint)

        edit_actions_btn = QPushButton("Edit Actions")
        edit_actions_btn.setStyleSheet(self._get_primary_button_style())

        def _edit_actions() -> None:
            nonlocal actions
            updated = self._edit_auto_item_actions_dialog(actions, title=f"Preset Actions - {name_le.text().strip() or 'Preset'}")
            if updated is None:
                return
            actions = updated
            summary_lbl.setText(self._auto_item_action_summary_text(actions))
            summary_lbl.setToolTip(self._auto_item_actions_tooltip(actions))

        edit_actions_btn.clicked.connect(_edit_actions)
        sequence_layout.addWidget(edit_actions_btn)
        layout.addWidget(sequence_group)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        return {
            "id": str(current.get("id") or f"preset_{uuid.uuid4().hex[:8]}"),
            "name": name_le.text().strip() or "Preset",
            "builtin": bool(current.get("builtin", False)),
            "actions": [copy.deepcopy(a) for a in actions],
        }

    def _open_auto_item_presets_dialog(self) -> None:
        presets = [copy.deepcopy(p) for p in (getattr(self, "_auto_item_presets", []) or []) if isinstance(p, dict)]

        dlg = QDialog(self)
        dlg.setWindowTitle("Action Presets")
        dlg.resize(980, 580)
        self._auto_item_apply_dialog_style(dlg)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)

        intro = QLabel(
            "Use presets to keep common action strings reusable, exportable, and easy to drop into new rows."
            " Save table rows from the main Auto Actions tab with the Save Selected Rows button."
        )
        intro.setProperty("role", "hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        content_row = QHBoxLayout()
        content_row.setSpacing(12)
        layout.addLayout(content_row, 1)

        left_group = QGroupBox("Saved Presets")
        left = QVBoxLayout(left_group)
        left.setSpacing(10)
        right_group = QGroupBox("Preview")
        right = QVBoxLayout(right_group)
        right.setSpacing(10)
        content_row.addWidget(left_group, 1)
        content_row.addWidget(right_group, 2)

        preset_list = QListWidget()
        preset_list.setMinimumWidth(280)
        left.addWidget(preset_list, 1)

        preview_hint = QLabel("Select a preset to inspect every step before adding it to the table.")
        preview_hint.setProperty("role", "hint")
        preview_hint.setWordWrap(True)
        right.addWidget(preview_hint)

        preview = QTextEdit()
        preview.setReadOnly(True)
        preview.setFont(QFont("Consolas", 10))
        right.addWidget(preview, 1)

        def _refresh_list(select_id: Optional[str] = None) -> None:
            preset_list.clear()
            target_row = 0
            for row, preset in enumerate(presets):
                label = str(preset.get("name") or "Preset")
                if bool(preset.get("builtin", False)):
                    label += " [Built-in]"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, str(preset.get("id") or ""))
                item.setToolTip(
                    f"{label}\n{self._auto_item_action_summary_text(preset.get('actions') or [])}"
                )
                preset_list.addItem(item)
                if select_id and str(preset.get("id") or "") == select_id:
                    target_row = row
            if preset_list.count():
                preset_list.setCurrentRow(max(0, min(target_row, preset_list.count() - 1)))

        def _selected_index() -> int:
            row = preset_list.currentRow()
            return row if 0 <= row < len(presets) else -1

        def _refresh_preview() -> None:
            row = _selected_index()
            if row < 0:
                preview.setPlainText("Select a preset to preview its action string.")
                return
            preset = presets[row]
            preset_actions = [a for a in (preset.get("actions") or []) if isinstance(a, dict)]
            lines = [
                str(preset.get("name") or "Preset"),
                "=" * max(6, len(str(preset.get("name") or "Preset"))),
                f"Type: {'Built-in' if bool(preset.get('builtin', False)) else 'Custom'}",
                f"Steps: {len(preset_actions)}",
                "",
            ]
            if not preset_actions:
                lines.append("No steps configured yet.")
            for idx, action in enumerate(preset_actions):
                kind = str(action.get("type") or "").replace("_", " ").title()
                name = str(action.get("name") or action.get("type") or "Action")
                lines.append(f"{idx + 1}. {name} [{kind}]")
                if str(action.get("type") or "") == "paste":
                    text = str(action.get("text") or "").replace("\r", " ").replace("\n", " ").strip()
                    if len(text) > 70:
                        text = text[:67] + "..."
                    lines.append(f"    Paste: {text or '(empty)'}")
                else:
                    point = action.get("point") if isinstance(action.get("point"), dict) else None
                    if isinstance(point, dict):
                        lines.append(
                            f"    Point: {float(point.get('x', 0.0)):.4f}, {float(point.get('y', 0.0)):.4f}"
                        )
                    else:
                        lines.append("    Point: not set")
                    if str(action.get("type") or "") == "scroll":
                        direction = "Up" if str(action.get("scroll_direction") or "down").strip().lower() == "up" else "Down"
                        lines.append(f"    Scroll: {direction}")
                    if str(action.get("type") or "") == "conditional_click":
                        lines.append(
                            f"    Match: {str(action.get('color') or '#FFFFFF')} +/- {int(action.get('tolerance', 10) or 10)}"
                        )
            preview.setPlainText("\n".join(lines))

        def _sync_presets() -> None:
            self._auto_item_presets = [copy.deepcopy(p) for p in presets]
            self._on_auto_item_ui_changed()

        add_to_table_btn = QPushButton("Add To Table")
        new_btn = QPushButton("New")
        edit_btn = QPushButton("Edit")
        duplicate_btn = QPushButton("Duplicate")
        remove_btn = QPushButton("Remove")
        import_btn = QPushButton("Import")
        export_btn = QPushButton("Export")
        add_to_table_btn.setStyleSheet(self._get_primary_button_style())
        for btn in (new_btn, edit_btn, duplicate_btn, remove_btn, import_btn, export_btn):
            btn.setStyleSheet(self._get_secondary_button_style())

        primary_row = QHBoxLayout()
        primary_row.setContentsMargins(0, 2, 0, 0)
        primary_row.setSpacing(8)
        primary_row.addStretch()
        primary_row.addWidget(add_to_table_btn)
        primary_row.addWidget(new_btn)
        primary_row.addWidget(edit_btn)
        primary_row.addStretch()
        left.addLayout(primary_row)

        manage_row = QHBoxLayout()
        manage_row.setContentsMargins(0, 0, 0, 0)
        manage_row.setSpacing(8)
        for btn in (duplicate_btn, remove_btn, import_btn, export_btn):
            manage_row.addWidget(btn)
        manage_row.addStretch()
        left.addLayout(manage_row)

        preset_list.currentRowChanged.connect(lambda *_args: _refresh_preview())

        def _insert_preset_as_row() -> None:
            row = _selected_index()
            if row < 0:
                return
            items = self._current_auto_item_items()
            preset = presets[row]
            items.append(
                {
                    "enabled": True,
                    "name": str(preset.get("name") or "Preset"),
                    "actions": [copy.deepcopy(a) for a in (preset.get("actions") or [])],
                    "behavior": self._default_auto_item_behavior(),
                    "alert_enabled": False,
                    "alert_webhook": "",
                    "alert_message": "",
                    "alert_lead_s": 15.0,
                }
            )
            self._load_auto_item_items_table(items)
            self._on_auto_item_ui_changed()

        def _new_preset() -> None:
            preset = self._edit_auto_item_preset_dialog()
            if preset is None:
                return
            presets.append(preset)
            _refresh_list(select_id=str(preset.get("id") or ""))
            _sync_presets()

        def _edit_preset() -> None:
            row = _selected_index()
            if row < 0:
                return
            updated = self._edit_auto_item_preset_dialog(presets[row])
            if updated is None:
                return
            presets[row] = updated
            _refresh_list(select_id=str(updated.get("id") or ""))
            _sync_presets()

        def _duplicate_preset() -> None:
            row = _selected_index()
            if row < 0:
                return
            dup = copy.deepcopy(presets[row])
            dup["id"] = f"preset_{uuid.uuid4().hex[:8]}"
            dup["name"] = f"{str(dup.get('name') or 'Preset')} Copy"
            dup["builtin"] = False
            presets.append(dup)
            _refresh_list(select_id=str(dup.get("id") or ""))
            _sync_presets()

        def _remove_preset() -> None:
            row = _selected_index()
            if row < 0:
                return
            if bool(presets[row].get("builtin", False)) or str(presets[row].get("id") or "") == "item":
                QMessageBox.information(self, "Action Presets", "The built-in Item preset cannot be removed.")
                return
            presets.pop(row)
            _refresh_list()
            _sync_presets()

        def _import_presets() -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Import Action Presets", "", "JSON Files (*.json)")
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception as e:
                QMessageBox.warning(self, "Import Failed", str(e))
                return
            raw_list = payload if isinstance(payload, list) else [payload]
            added_id = None
            for idx, raw in enumerate(raw_list):
                preset = self._normalize_auto_item_preset(raw, fallback_index=idx)
                if preset is None:
                    continue
                if str(preset.get("id") or "") == "item":
                    preset["builtin"] = True
                presets.append(preset)
                added_id = str(preset.get("id") or "")
            _refresh_list(select_id=added_id)
            _sync_presets()

        def _export_preset() -> None:
            row = _selected_index()
            if row < 0:
                return
            preset = presets[row]
            suggested = re.sub(r"[^A-Za-z0-9._-]+", "_", str(preset.get("name") or "preset")).strip("_") or "preset"
            path, _ = QFileDialog.getSaveFileName(self, "Export Action Preset", f"{suggested}.json", "JSON Files (*.json)")
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(preset, fh, indent=2)
            except Exception as e:
                QMessageBox.warning(self, "Export Failed", str(e))

        add_to_table_btn.clicked.connect(_insert_preset_as_row)
        new_btn.clicked.connect(_new_preset)
        edit_btn.clicked.connect(_edit_preset)
        duplicate_btn.clicked.connect(_duplicate_preset)
        remove_btn.clicked.connect(_remove_preset)
        import_btn.clicked.connect(_import_presets)
        export_btn.clicked.connect(_export_preset)

        _refresh_list()
        _refresh_preview()
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(self._get_secondary_button_style())
        close_btn.clicked.connect(dlg.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)
        dlg.exec()

    def setup_auto_item_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        auto_widget = QWidget()
        scroll.setWidget(auto_widget)
        layout = QVBoxLayout(auto_widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        info_group = QGroupBox("Auto Actions")
        info_layout = QVBoxLayout(info_group)
        info_layout.setSpacing(6)
        desc = QLabel(
            "Chain clicks, conditional clicks, and pastes into reusable rows."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        info_layout.addWidget(desc)
        sub_desc = QLabel(
            "Each row carries its own action string, OCR trigger bindings, cooldown/biome behavior, "
            "optional user targeting, and pre-run alert settings."
        )
        sub_desc.setWordWrap(True)
        sub_desc.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        info_layout.addWidget(sub_desc)
        layout.addWidget(info_group)

        settings_group = QGroupBox("Settings")
        settings_layout = QGridLayout(settings_group)
        settings_layout.setHorizontalSpacing(16)
        settings_layout.setVerticalSpacing(12)
        settings_layout.setColumnStretch(0, 0)
        settings_layout.setColumnStretch(1, 1)

        self.auto_item_enable_chk = QCheckBox("Enable Auto-Actions (while manager is running)")
        settings_layout.addWidget(self.auto_item_enable_chk, 0, 0, 1, 2)

        settings_layout.addWidget(QLabel("Tick interval (seconds):"), 1, 0)
        self.auto_item_tick_spin = QDoubleSpinBox()
        self.auto_item_tick_spin.setRange(0.2, 60.0)
        self.auto_item_tick_spin.setDecimals(2)
        self.auto_item_tick_spin.setSingleStep(0.1)
        self.auto_item_tick_spin.setValue(1.0)
        settings_layout.addWidget(self.auto_item_tick_spin, 1, 1)

        settings_layout.addWidget(QLabel("Click/paste delay (seconds):"), 2, 0)
        self.auto_item_delay_spin = QDoubleSpinBox()
        self.auto_item_delay_spin.setRange(0.01, 2.0)
        self.auto_item_delay_spin.setDecimals(2)
        self.auto_item_delay_spin.setSingleStep(0.05)
        self.auto_item_delay_spin.setValue(0.2)
        settings_layout.addWidget(self.auto_item_delay_spin, 2, 1)

        self.auto_item_disable_mouse_chk = QCheckBox("Disable user mouse movement during Auto-Actions")
        settings_layout.addWidget(self.auto_item_disable_mouse_chk, 3, 0, 1, 2)
        try:
            self._update_auto_item_disable_mouse_tooltip()
        except Exception:
            pass

        settings_layout.addWidget(QLabel("Toggle hotkey:"), 4, 0)
        self.auto_item_hotkey_edit = QKeySequenceEdit()
        self.auto_item_hotkey_edit.setToolTip("Global hotkey to toggle Auto-Actions enable/disable (default: Ctrl+Alt+Space).")
        try:
            self.auto_item_hotkey_edit.setKeySequence(QKeySequence("Ctrl+Alt+Space"))
        except Exception:
            pass
        self.auto_item_hotkey_edit.setMinimumWidth(220)
        settings_layout.addWidget(self.auto_item_hotkey_edit, 4, 1)

        layout.addWidget(settings_group)

        items_group = QGroupBox("Action Rows")
        items_layout = QVBoxLayout(items_group)
        items_layout.setSpacing(10)

        self.auto_item_table = QTableWidget()
        self.auto_item_table.setColumnCount(6)
        self.auto_item_table.setHorizontalHeaderLabels(["Enabled", "Name", "Actions", "Behavior", "Users", "Alert"])
        self.auto_item_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.auto_item_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.auto_item_table.setShowGrid(False)
        self.auto_item_table.setAlternatingRowColors(False)
        self.auto_item_table.setWordWrap(False)
        self.auto_item_table.verticalHeader().setVisible(False)
        self.auto_item_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        header = self.auto_item_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(72)
        self.auto_item_table.setColumnWidth(0, 72)
        self.auto_item_table.setMinimumHeight(280)
        try:
            self.auto_item_table.setColumnHidden(5, not _bm_relaxed())
        except Exception:
            pass
        self._auto_item_apply_main_table_style(self.auto_item_table)
        items_layout.addWidget(self.auto_item_table)

        rows_hint = QLabel(
            "Use presets to drop in reusable sequences."
        )
        rows_hint.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        rows_hint.setWordWrap(True)
        items_layout.addWidget(rows_hint)

        items_btn_row = QHBoxLayout()
        items_btn_row.setSpacing(8)
        add_item_btn = QPushButton("Add Action")
        add_item_btn.clicked.connect(self._auto_item_add_item)
        remove_item_btn = QPushButton("Remove Selected")
        remove_item_btn.clicked.connect(self._auto_item_remove_selected_items)
        save_rows_btn = QPushButton("Save Selected")
        save_rows_btn.clicked.connect(self._auto_item_save_selected_rows_as_presets)
        test_btn = QPushButton("Test Selected")
        test_btn.clicked.connect(self._auto_item_test_once)
        self.auto_item_test_btn = test_btn
        up_btn = QPushButton("Move Up")
        up_btn.clicked.connect(lambda: self._auto_item_move_selected(-1))
        down_btn = QPushButton("Move Down")
        down_btn.clicked.connect(lambda: self._auto_item_move_selected(1))
        presets_btn = QPushButton("Presets")
        presets_btn.clicked.connect(self._open_auto_item_presets_dialog)
        for btn in (add_item_btn, remove_item_btn, save_rows_btn, test_btn, up_btn, down_btn, presets_btn):
            items_btn_row.addWidget(btn)
        items_btn_row.addStretch()
        items_layout.addLayout(items_btn_row)

        layout.addWidget(items_group)

        users_group = QGroupBox("Users")
        users_layout = QVBoxLayout(users_group)

        users_btn_row = QHBoxLayout()
        refresh_users_btn = QPushButton("Refresh List")
        refresh_users_btn.clicked.connect(self._auto_item_refresh_users)
        sel_all_btn = QPushButton("Select All")
        sel_all_btn.clicked.connect(lambda: self._auto_item_set_all_users(True))
        sel_none_btn = QPushButton("Select None")
        sel_none_btn.clicked.connect(lambda: self._auto_item_set_all_users(False))
        users_btn_row.addWidget(refresh_users_btn)
        users_btn_row.addWidget(sel_all_btn)
        users_btn_row.addWidget(sel_none_btn)
        users_btn_row.addStretch()
        users_layout.addLayout(users_btn_row)

        users_hint = QLabel("These checkboxes define the available targets for rows set to run on all users.")
        users_hint.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        users_hint.setWordWrap(True)
        users_layout.addWidget(users_hint)

        self.auto_item_users_container = QWidget()
        self.auto_item_users_vbox = QVBoxLayout(self.auto_item_users_container)
        self.auto_item_users_vbox.setContentsMargins(0, 0, 0, 0)
        self.auto_item_users_vbox.setSpacing(4)
        self.auto_item_user_checks = {}

        users_scroll = QScrollArea()
        users_scroll.setWidgetResizable(True)
        users_scroll.setWidget(self.auto_item_users_container)
        users_scroll.setMinimumHeight(180)
        users_scroll.setMinimumWidth(280)
        users_layout.addWidget(users_scroll)

        log_group = QGroupBox("Auto-Actions Log")
        log_layout = QVBoxLayout(log_group)
        log_hint = QLabel("Runtime messages, OCR-trigger activity, and manual tests are written here.")
        log_hint.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        log_hint.setWordWrap(True)
        log_layout.addWidget(log_hint)
        self.autoitem_status_box = QTextEdit()
        self.autoitem_status_box.setReadOnly(True)
        self.autoitem_status_box.setFont(QFont("Consolas", 10))
        self.autoitem_status_box.setMinimumHeight(180)
        try:
            self.autoitem_status_box.document().setMaximumBlockCount(5000)
        except Exception:
            pass
        log_layout.addWidget(self.autoitem_status_box)

        lower_row = QHBoxLayout()
        lower_row.setSpacing(14)
        lower_row.addWidget(users_group, 1)
        lower_row.addWidget(log_group, 2)
        layout.addLayout(lower_row)

        footer = QHBoxLayout()
        footer.addStretch()
        reset_btn = QPushButton("Restore Auto-Actions Defaults")
        reset_btn.clicked.connect(self._reset_auto_item_to_defaults)
        footer.addWidget(reset_btn)
        layout.addLayout(footer)
        layout.addStretch()

        self._auto_item_presets = []
        self._auto_item_save_timer = QTimer(self)
        self._auto_item_save_timer.setSingleShot(True)
        self._auto_item_save_timer.timeout.connect(self._save_auto_item_settings)

        self.auto_item_enable_chk.toggled.connect(self._on_auto_item_enabled_toggled)
        self.auto_item_tick_spin.valueChanged.connect(self._on_auto_item_ui_changed)
        self.auto_item_delay_spin.valueChanged.connect(self._on_auto_item_ui_changed)
        self.auto_item_disable_mouse_chk.toggled.connect(self._on_auto_item_mouse_toggle_changed)
        self.auto_item_hotkey_edit.keySequenceChanged.connect(self._on_auto_item_hotkey_changed)

        self._auto_item_refresh_users()
        self._load_auto_item_settings()
        self._ensure_auto_item_engine()

        self.tab_widget.addTab(scroll, "Auto Actions")

    def _auto_item_add_item(self):
        items = self._current_auto_item_items()
        items.append(
            {
                "enabled": True,
                "name": "",
                "actions": [],
                "behavior": self._default_auto_item_behavior(),
                "alert_enabled": False,
                "alert_webhook": "",
                "alert_message": "",
                "alert_lead_s": 15.0,
            }
        )
        self._load_auto_item_items_table(items)
        self._on_auto_item_ui_changed()

    def _auto_item_save_selected_rows_as_presets(self) -> None:
        rows = sorted({idx.row() for idx in self.auto_item_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "Action Presets", "Select at least one row in the Auto Actions table first.")
            return

        items = self._current_auto_item_items()
        presets = [copy.deepcopy(p) for p in (getattr(self, "_auto_item_presets", []) or []) if isinstance(p, dict)]

        if len(rows) == 1:
            row_idx = rows[0]
            if row_idx < 0 or row_idx >= len(items):
                return
            item = items[row_idx]
            preset = self._edit_auto_item_preset_dialog(
                {
                    "name": str(item.get("name") or "Preset"),
                    "actions": [copy.deepcopy(a) for a in (item.get("actions") or [])],
                    "builtin": False,
                }
            )
            if preset is None:
                return
            presets.append(preset)
            self._auto_item_presets = presets
            self._on_auto_item_ui_changed()
            return

        added = 0
        for row_idx in rows:
            if row_idx < 0 or row_idx >= len(items):
                continue
            item = items[row_idx]
            preset = self._normalize_auto_item_preset(
                {
                    "id": f"preset_{uuid.uuid4().hex[:8]}",
                    "name": str(item.get("name") or f"Preset {row_idx + 1}").strip() or f"Preset {row_idx + 1}",
                    "builtin": False,
                    "actions": [copy.deepcopy(a) for a in (item.get("actions") or [])],
                },
                fallback_index=len(presets) + added,
            )
            if preset is None:
                continue
            presets.append(preset)
            added += 1

        if added <= 0:
            QMessageBox.information(self, "Action Presets", "No presets were created from the selected rows.")
            return

        self._auto_item_presets = presets
        self._on_auto_item_ui_changed()
        QMessageBox.information(self, "Action Presets", f"Saved {added} presets from the selected rows.")

    def _auto_item_test_once(self):
        self._ensure_auto_item_engine()
        if not getattr(self, "auto_item_engine", None):
            QMessageBox.warning(self, "Auto Actions", "Auto-Actions engine is not available.")
            return
        if not self._is_manager_running():
            QMessageBox.information(self, "Auto-Actions Test", "Start the manager first so a user window can be resolved.")
            return

        uid = None
        for key, cb in (getattr(self, "auto_item_user_checks", {}) or {}).items():
            try:
                if cb.isChecked():
                    uid = str(key)
                    break
            except Exception:
                continue
        if not uid:
            QMessageBox.information(self, "Auto-Actions Test", "Select at least one user first.")
            return

        rows = sorted({idx.row() for idx in self.auto_item_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "Auto-Actions Test", "Select at least one action row in the table first.")
            return

        try:
            self.auto_item_engine.update_config(self._get_auto_item_settings_from_ui())
        except Exception:
            pass

        try:
            ok = bool(self.auto_item_engine.test_once(uid, row_indices=rows))
        except Exception as e:
            self.autoitem_log_signal.emit(f"[Auto-Actions] Test: error: {e}")
            ok = False

        if ok:
            QMessageBox.information(self, "Auto-Actions Test", "Test run complete. Check the Auto-Actions log for details.")
        else:
            QMessageBox.warning(self, "Auto-Actions Test", "Test run did not complete. Check the Auto-Actions log for details.")

    def _load_auto_item_items_table(self, items: List[dict]):
        self.auto_item_table.setRowCount(0)

        def _wrap_cell(w: QWidget, *, center: bool = False, margins: Tuple[int, int, int, int] = (0, 6, 0, 6)) -> QWidget:
            holder = QWidget()
            holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            holder.setStyleSheet("background: transparent;")
            lay = QHBoxLayout(holder)
            lay.setContentsMargins(*margins)
            lay.setSpacing(0)
            if center:
                lay.addStretch(1)
                lay.addWidget(w, 0, Qt.AlignmentFlag.AlignCenter)
                lay.addStretch(1)
            else:
                lay.addWidget(w, 1, Qt.AlignmentFlag.AlignVCenter)
            return holder

        for item in (items or []):
            row = self.auto_item_table.rowCount()
            self.auto_item_table.insertRow(row)
            try:
                self.auto_item_table.setRowHeight(row, 80)
            except Exception:
                pass

            enabled = QCheckBox()
            enabled.setChecked(bool(item.get("enabled", True)))
            enabled.toggled.connect(self._on_auto_item_ui_changed)
            self.auto_item_table.setCellWidget(row, 0, _wrap_cell(enabled, center=True))

            name = QLineEdit(str(item.get("name") or ""))
            name.setPlaceholderText("Name this action row")
            name.setMinimumWidth(180)
            try:
                name.setClearButtonEnabled(True)
            except Exception:
                pass
            name.setToolTip("This label is used in presets, alerts, and manual tests.")
            name.textChanged.connect(self._on_auto_item_ui_changed)
            self.auto_item_table.setCellWidget(row, 1, _wrap_cell(name, margins=(6, 6, 6, 6)))

            actions_btn = QPushButton()
            actions_btn.setProperty("actions", [copy.deepcopy(a) for a in (item.get("actions") or []) if isinstance(a, dict)])
            self._auto_item_apply_cell_button_style(actions_btn, accent=True)
            self._update_actions_btn_text(actions_btn)
            actions_btn.clicked.connect(lambda _, b=actions_btn: self._edit_item_actions(b))
            self.auto_item_table.setCellWidget(row, 2, _wrap_cell(actions_btn, margins=(6, 6, 6, 6)))

            behavior_btn = QPushButton()
            behavior_btn.setProperty("behavior", self._normalize_auto_item_behavior(item.get("behavior"), legacy=item))
            self._auto_item_apply_cell_button_style(behavior_btn)
            self._update_behavior_btn_text(behavior_btn)
            behavior_btn.clicked.connect(lambda _, b=behavior_btn: self._edit_item_behavior(b))
            self.auto_item_table.setCellWidget(row, 3, _wrap_cell(behavior_btn, margins=(6, 6, 6, 6)))

            users_btn = QPushButton()
            raw_users = item.get("users", None)
            users_prop = None
            if raw_users is None:
                users_prop = None
            elif isinstance(raw_users, (list, tuple, set)):
                users_list = [str(u).strip() for u in raw_users if str(u).strip()]
                users_prop = users_list if users_list or bool(item.get("users_explicit", False)) else None
            users_btn.setProperty("users", users_prop)
            self._auto_item_apply_cell_button_style(users_btn)
            self._update_users_btn_text(users_btn)
            users_btn.clicked.connect(lambda _, b=users_btn: self._edit_item_users(b))
            self.auto_item_table.setCellWidget(row, 4, _wrap_cell(users_btn, margins=(6, 6, 6, 6)))

            alert_btn = QPushButton()
            alert_btn.setProperty("alert_enabled", bool(item.get("alert_enabled", False)))
            alert_btn.setProperty("alert_webhook", str(item.get("alert_webhook") or "").strip())
            alert_btn.setProperty("alert_message", str(item.get("alert_message") or ""))
            alert_btn.setProperty("alert_lead_s", float(item.get("alert_lead_s", 15.0) or 15.0))
            self._auto_item_apply_cell_button_style(alert_btn)
            self._update_alert_btn_text(alert_btn)
            alert_btn.clicked.connect(lambda _, b=alert_btn: self._edit_item_alert(b))
            self.auto_item_table.setCellWidget(row, 5, _wrap_cell(alert_btn, margins=(6, 6, 6, 6)))

    def _current_auto_item_items(self) -> List[dict]:
        items: List[dict] = []

        def _unwrap(col_widget: QWidget, typ):
            if col_widget is None:
                return None
            if isinstance(col_widget, typ):
                return col_widget
            try:
                return col_widget.findChild(typ)
            except Exception:
                return None

        for row in range(self.auto_item_table.rowCount()):
            enabled = _unwrap(self.auto_item_table.cellWidget(row, 0), QCheckBox)
            name = _unwrap(self.auto_item_table.cellWidget(row, 1), QLineEdit)
            actions_btn = _unwrap(self.auto_item_table.cellWidget(row, 2), QPushButton)
            behavior_btn = _unwrap(self.auto_item_table.cellWidget(row, 3), QPushButton)
            users_btn = _unwrap(self.auto_item_table.cellWidget(row, 4), QPushButton)
            alert_btn = _unwrap(self.auto_item_table.cellWidget(row, 5), QPushButton)

            item = {
                "enabled": bool(enabled.isChecked()) if isinstance(enabled, QCheckBox) else True,
                "name": name.text().strip() if isinstance(name, QLineEdit) else "",
                "actions": [copy.deepcopy(a) for a in ((actions_btn.property("actions") if isinstance(actions_btn, QPushButton) else []) or []) if isinstance(a, dict)],
                "behavior": self._normalize_auto_item_behavior(behavior_btn.property("behavior") if isinstance(behavior_btn, QPushButton) else {}),
            }

            if isinstance(users_btn, QPushButton):
                raw_users = users_btn.property("users")
                if raw_users is None:
                    item["users"] = None
                    item["users_explicit"] = False
                elif isinstance(raw_users, (list, tuple, set)):
                    item["users"] = [str(u).strip() for u in raw_users if str(u).strip()]
                    item["users_explicit"] = True

            if isinstance(alert_btn, QPushButton):
                item["alert_enabled"] = bool(alert_btn.property("alert_enabled"))
                item["alert_webhook"] = str(alert_btn.property("alert_webhook") or "").strip()
                item["alert_message"] = str(alert_btn.property("alert_message") or "")
                item["alert_lead_s"] = float(alert_btn.property("alert_lead_s") or 15.0)

            items.append(item)

        return items

    def _get_auto_item_settings_from_ui(self) -> dict:
        users = [str(uid).strip() for uid, cb in (self.auto_item_user_checks or {}).items() if cb.isChecked() and str(uid).strip()]
        return {
            "enabled": bool(self.auto_item_enable_chk.isChecked()),
            "tick_interval": float(self.auto_item_tick_spin.value()),
            "click_delay": float(self.auto_item_delay_spin.value()),
            "disable_mouse_move": bool(getattr(self, "auto_item_disable_mouse_chk", None) and self.auto_item_disable_mouse_chk.isChecked()),
            "toggle_hotkey": (self.auto_item_hotkey_edit.keySequence().toString().strip() if getattr(self, "auto_item_hotkey_edit", None) else ""),
            "users": users,
            "presets": [copy.deepcopy(p) for p in (getattr(self, "_auto_item_presets", []) or []) if isinstance(p, dict)],
            "items": self._current_auto_item_items(),
        }

    def _get_auto_item_cfg_from_disk(self) -> dict:
        try:
            settings = self.config_manager.load_settings() or {}
        except Exception:
            settings = {}
        return self._normalize_auto_item_cfg(settings.get("auto_item", {}) or {})

    def _load_auto_item_settings(self):
        cfg = self._get_auto_item_cfg_from_disk()
        defaults = self._normalize_auto_item_cfg(self.config_manager.default_settings.get("auto_item", {}) or {})
        merged = {**defaults, **cfg}
        hk = str(merged.get("toggle_hotkey", "Ctrl+Alt+Space") or "Ctrl+Alt+Space").strip()

        self._loading_autoitem_settings = True
        try:
            self.auto_item_enable_chk.setChecked(bool(merged.get("enabled", False)))
            self.auto_item_tick_spin.setValue(float(merged.get("tick_interval", 1.0) or 1.0))
            self.auto_item_delay_spin.setValue(float(merged.get("click_delay", 0.2) or 0.2))
            self.auto_item_disable_mouse_chk.setChecked(bool(merged.get("disable_mouse_move", False)))
            self._update_auto_item_disable_mouse_tooltip()
            try:
                self.auto_item_hotkey_edit.setKeySequence(QKeySequence(hk))
            except Exception:
                pass
            self._auto_item_presets = [copy.deepcopy(p) for p in (merged.get("presets") or []) if isinstance(p, dict)]
            self._load_auto_item_items_table(merged.get("items", []) or [])
            self._apply_auto_item_users_to_ui(merged.get("users", []) or [])
        finally:
            self._loading_autoitem_settings = False

        try:
            self._apply_auto_item_hotkey(hk, quiet=True)
        except Exception:
            pass

        try:
            if self.auto_item_engine is not None:
                self.auto_item_engine.update_config(self._get_auto_item_settings_from_ui())
        except Exception:
            pass

    def _reset_auto_item_to_defaults(self):
        defaults = self._normalize_auto_item_cfg(self.config_manager.default_settings.get("auto_item", {}) or {})
        hk = str(defaults.get("toggle_hotkey", "Ctrl+Alt+Space") or "Ctrl+Alt+Space").strip()
        self._loading_autoitem_settings = True
        try:
            self.auto_item_enable_chk.setChecked(bool(defaults.get("enabled", False)))
            self.auto_item_tick_spin.setValue(float(defaults.get("tick_interval", 1.0) or 1.0))
            self.auto_item_delay_spin.setValue(float(defaults.get("click_delay", 0.2) or 0.2))
            self.auto_item_disable_mouse_chk.setChecked(bool(defaults.get("disable_mouse_move", False)))
            self._update_auto_item_disable_mouse_tooltip()
            try:
                self.auto_item_hotkey_edit.setKeySequence(QKeySequence(hk))
            except Exception:
                pass
            self._auto_item_presets = [copy.deepcopy(p) for p in (defaults.get("presets") or []) if isinstance(p, dict)]
            self._load_auto_item_items_table(defaults.get("items", []) or [])
            self._auto_item_set_all_users(False)
        finally:
            self._loading_autoitem_settings = False

        try:
            self._apply_auto_item_hotkey(hk, quiet=True)
        except Exception:
            pass

        self._on_auto_item_ui_changed()

    # ------------------------
    # BES tab + controller
    # ------------------------

    def setup_bes_tab(self):
        bes_widget = QWidget()
        layout = QVBoxLayout(bes_widget)

        if self.bes_controller is None:
            msg = QLabel("BES throttling is unavailable on this system/build.")
            msg.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
            layout.addWidget(msg)
            layout.addStretch(1)
            self.tab_widget.addTab(bes_widget, "BES")
            return

        top_group = QGroupBox("BES - Battle Encoder Shirasé")
        top_layout = QGridLayout(top_group)

        self.bes_enable_chk = QCheckBox("Enable throttling for all Roblox processes")
        top_layout.addWidget(self.bes_enable_chk, 0, 0, 1, 3)

        top_layout.addWidget(QLabel("Cycle (ms):"), 1, 0)
        self.bes_cycle_spin = QSpinBox()
        self.bes_cycle_spin.setRange(10, 1000)
        self.bes_cycle_spin.setSingleStep(5)
        self.bes_cycle_spin.setValue(50)
        top_layout.addWidget(self.bes_cycle_spin, 1, 1)

        self.bes_status_label = QLabel("Status: Disabled")
        self.bes_status_label.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        top_layout.addWidget(self.bes_status_label, 1, 2)

        layout.addWidget(top_group)

        levels_group = QGroupBox("Throttle Levels")
        levels_layout = QGridLayout(levels_group)

        levels_layout.addWidget(QLabel("Main menu:"), 0, 0)
        self.bes_menu_slider = QSlider(Qt.Orientation.Horizontal)
        self.bes_menu_slider.setRange(0, 99)
        self.bes_menu_slider.setValue(85)
        self.bes_menu_val = QLabel("85%")
        levels_layout.addWidget(self.bes_menu_slider, 0, 1)
        levels_layout.addWidget(self.bes_menu_val, 0, 2)

        levels_layout.addWidget(QLabel("Outside main menu:"), 1, 0)
        self.bes_game_slider = QSlider(Qt.Orientation.Horizontal)
        self.bes_game_slider.setRange(0, 99)
        self.bes_game_slider.setValue(50)
        self.bes_game_val = QLabel("50%")
        levels_layout.addWidget(self.bes_game_slider, 1, 1)
        levels_layout.addWidget(self.bes_game_val, 1, 2)

        layout.addWidget(levels_group)

        exempt_group = QGroupBox("Exempt Users")
        exempt_layout = QGridLayout(exempt_group)

        self.bes_exempt_combos: List[QComboBox] = []
        for i in range(3):
            exempt_layout.addWidget(QLabel(f"Slot {i + 1}:"), i, 0)
            combo = QComboBox()
            combo.setMinimumWidth(280)
            self.bes_exempt_combos.append(combo)
            exempt_layout.addWidget(combo, i, 1, 1, 2)

        refresh_btn = QPushButton("Refresh User List")
        refresh_btn.clicked.connect(self._bes_refresh_user_list)
        exempt_layout.addWidget(refresh_btn, 3, 0, 1, 3)

        layout.addWidget(exempt_group)

        auto_group = QGroupBox("Auto Item Pacify")
        auto_layout = QGridLayout(auto_group)

        auto_layout.addWidget(QLabel("Unthrottle lead time (seconds):"), 0, 0)
        self.bes_auto_lead_spin = QDoubleSpinBox()
        self.bes_auto_lead_spin.setRange(0.0, 15.0)
        self.bes_auto_lead_spin.setSingleStep(0.5)
        self.bes_auto_lead_spin.setDecimals(1)
        self.bes_auto_lead_spin.setValue(3.0)
        auto_layout.addWidget(self.bes_auto_lead_spin, 0, 1)

        auto_layout.addWidget(QLabel("Post-action grace (seconds):"), 1, 0)
        self.bes_auto_grace_spin = QDoubleSpinBox()
        self.bes_auto_grace_spin.setRange(0.0, 10.0)
        self.bes_auto_grace_spin.setSingleStep(0.25)
        self.bes_auto_grace_spin.setDecimals(2)
        self.bes_auto_grace_spin.setValue(1.0)
        auto_layout.addWidget(self.bes_auto_grace_spin, 1, 1)

        hint = QLabel("Auto Item temporarily disables throttling before it starts clicking/typing.")
        hint.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        auto_layout.addWidget(hint, 2, 0, 1, 2)

        layout.addWidget(auto_group)

        log_group = QGroupBox("BES Log")
        log_layout = QVBoxLayout(log_group)
        self.bes_log_box = QTextEdit()
        self.bes_log_box.setReadOnly(True)
        self.bes_log_box.setFont(QFont("Consolas", 10))
        self.bes_log_box.setMinimumHeight(180)
        try:
            self.bes_log_box.document().setMaximumBlockCount(5000)
        except Exception:
            pass
        log_layout.addWidget(self.bes_log_box)
        layout.addWidget(log_group)

        footer = QHBoxLayout()
        footer.addStretch()
        reset_btn = QPushButton("Restore BES Defaults")
        reset_btn.clicked.connect(self._reset_bes_to_defaults)
        footer.addWidget(reset_btn)
        layout.addLayout(footer)

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(bes_widget)
        self.tab_widget.addTab(scroll, "BES")

        # Debounced persistence (avoid writing settings.json on slider drag)
        self._bes_save_timer = QTimer(self)
        self._bes_save_timer.setSingleShot(True)
        self._bes_save_timer.timeout.connect(self._save_bes_settings)

        # Periodic enforcement loop
        self._bes_tick_timer = QTimer(self)
        self._bes_tick_timer.timeout.connect(self._bes_tick)
        self._bes_tick_timer.setInterval(1000)

        # Wire change events
        self.bes_enable_chk.toggled.connect(self._on_bes_enabled_toggled)
        self.bes_cycle_spin.valueChanged.connect(self._on_bes_ui_changed)
        self.bes_menu_slider.valueChanged.connect(self._on_bes_slider_changed)
        self.bes_game_slider.valueChanged.connect(self._on_bes_slider_changed)
        self.bes_auto_lead_spin.valueChanged.connect(self._on_bes_ui_changed)
        self.bes_auto_grace_spin.valueChanged.connect(self._on_bes_ui_changed)
        for combo in self.bes_exempt_combos:
            combo.currentIndexChanged.connect(self._on_bes_ui_changed)

        # Populate + load persisted settings
        self._bes_refresh_user_list()
        self._load_bes_settings()

    def _on_bes_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {str(message)}"
        try:
            self._bes_log_queue.append(line)
        except Exception:
            box = getattr(self, "bes_log_box", None)
            if box is not None:
                try:
                    box.append(line)
                except Exception:
                    pass

        try:
            if not self._log_flush_timer:
                self._flush_bes_log_queue()
        except Exception:
            pass

    def _bes_refresh_user_list(self) -> None:
        combos = getattr(self, "bes_exempt_combos", None) or []
        try:
            users_cfg = self.config_manager.peek_users() or {}
        except Exception:
            users_cfg = {}

        # Preserve current selections (userData values)
        prev: List[str] = []
        for c in combos:
            try:
                prev.append(str(c.currentData() or ""))
            except Exception:
                prev.append("")

        items: List[tuple[str, str]] = []
        for uid, info in (users_cfg or {}).items():
            try:
                uid_s = str(uid).strip()
                if not uid_s:
                    continue
                name = str((info or {}).get("username", "") or "").strip() or uid_s
                items.append((uid_s, name))
            except Exception:
                continue
        items.sort(key=lambda t: (t[1].lower(), t[0]))

        for idx, combo in enumerate(combos):
            try:
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("None", "")
                for uid_s, name in items:
                    combo.addItem(f"{name} ({uid_s})", uid_s)

                sel = prev[idx] if idx < len(prev) else ""
                if sel and combo.findData(sel) < 0:
                    combo.insertItem(1, f"Unknown ({sel})", sel)
                if sel:
                    combo.setCurrentIndex(max(0, combo.findData(sel)))
                else:
                    combo.setCurrentIndex(0)
            except Exception:
                pass
            finally:
                try:
                    combo.blockSignals(False)
                except Exception:
                    pass

    def _bes_update_status(self, text: str, *, warning: bool = False) -> None:
        lbl = getattr(self, "bes_status_label", None)
        if lbl is None:
            return
        try:
            lbl.setText(str(text))
            if warning:
                lbl.setStyleSheet(f"color: {ModernStyle.WARNING};")
            else:
                lbl.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        except Exception:
            pass

    def _on_bes_slider_changed(self, _v: int) -> None:
        try:
            self.bes_menu_val.setText(f"{int(self.bes_menu_slider.value())}%")
            self.bes_game_val.setText(f"{int(self.bes_game_slider.value())}%")
        except Exception:
            pass
        self._on_bes_ui_changed()

    def _on_bes_enabled_toggled(self, enabled: bool) -> None:
        if self.bes_controller is None:
            self._bes_update_status("Status: Unsupported", warning=True)
            return

        enabled = bool(enabled)
        try:
            self.bes_controller.set_enabled(enabled)
        except Exception:
            pass

        if enabled:
            try:
                if self._bes_tick_timer is not None:
                    self._bes_tick_timer.start()
            except Exception:
                pass
            self._bes_tick()
        else:
            try:
                if self._bes_tick_timer is not None:
                    self._bes_tick_timer.stop()
            except Exception:
                pass
            self._bes_update_status("Status: Disabled")

        try:
            with self._bes_cfg_lock:
                self._bes_cfg_cache = self._get_bes_settings_from_ui()
        except Exception:
            pass

        self._on_bes_ui_changed()

    def _get_bes_settings_from_ui(self) -> Dict:
        exempt: List[str] = []
        for c in getattr(self, "bes_exempt_combos", None) or []:
            try:
                exempt.append(str(c.currentData() or "").strip())
            except Exception:
                exempt.append("")
        while len(exempt) < 3:
            exempt.append("")
        exempt = exempt[:3]

        return {
            "enabled": bool(getattr(self, "bes_enable_chk", None) and self.bes_enable_chk.isChecked()),
            "cycle_ms": int(getattr(self, "bes_cycle_spin", None) and self.bes_cycle_spin.value() or 50),
            "menu_throttle_percent": int(getattr(self, "bes_menu_slider", None) and self.bes_menu_slider.value() or 0),
            "game_throttle_percent": int(getattr(self, "bes_game_slider", None) and self.bes_game_slider.value() or 0),
            "exempt_users": exempt,
            "auto_item_lead_s": float(getattr(self, "bes_auto_lead_spin", None) and self.bes_auto_lead_spin.value() or 0.0),
            "auto_item_grace_s": float(getattr(self, "bes_auto_grace_spin", None) and self.bes_auto_grace_spin.value() or 0.0),
        }

    def _load_bes_settings(self) -> None:
        try:
            settings = self.config_manager.load_settings() or {}
        except Exception:
            settings = dict(self.config_manager.default_settings or {})

        cfg = settings.get("bes", {}) or {}
        defaults = self.config_manager.default_settings.get("bes", {}) or {}

        self._loading_bes_settings = True
        try:
            self.bes_enable_chk.setChecked(bool(cfg.get("enabled", defaults.get("enabled", False))))
            self.bes_cycle_spin.setValue(int(cfg.get("cycle_ms", defaults.get("cycle_ms", 50))))

            menu_pct = int(cfg.get("menu_throttle_percent", defaults.get("menu_throttle_percent", 85)))
            game_pct = int(cfg.get("game_throttle_percent", defaults.get("game_throttle_percent", 50)))
            self.bes_menu_slider.setValue(max(0, min(99, menu_pct)))
            self.bes_game_slider.setValue(max(0, min(99, game_pct)))
            self.bes_menu_val.setText(f"{int(self.bes_menu_slider.value())}%")
            self.bes_game_val.setText(f"{int(self.bes_game_slider.value())}%")

            self.bes_auto_lead_spin.setValue(float(cfg.get("auto_item_lead_s", defaults.get("auto_item_lead_s", 3.0))))
            self.bes_auto_grace_spin.setValue(float(cfg.get("auto_item_grace_s", defaults.get("auto_item_grace_s", 1.0))))

            exempt = cfg.get("exempt_users", defaults.get("exempt_users", ["", "", ""])) or ["", "", ""]
            if not isinstance(exempt, list):
                exempt = ["", "", ""]
            while len(exempt) < 3:
                exempt.append("")
            for i, combo in enumerate(getattr(self, "bes_exempt_combos", None) or []):
                uid = str(exempt[i] or "").strip()
                if uid and combo.findData(uid) < 0:
                    combo.insertItem(1, f"Unknown ({uid})", uid)
                combo.setCurrentIndex(max(0, combo.findData(uid) if uid else 0))
        finally:
            self._loading_bes_settings = False

        # Cache snapshot for cross-thread consumers (Auto Item hook)
        try:
            with self._bes_cfg_lock:
                self._bes_cfg_cache = self._get_bes_settings_from_ui()
        except Exception:
            pass

        # Apply enabled state on load (starts/stops controller/timer)
        try:
            self._on_bes_enabled_toggled(bool(self.bes_enable_chk.isChecked()))
        except Exception:
            pass

    def _save_bes_settings(self) -> None:
        if self._loading_bes_settings:
            return

        try:
            settings = self.config_manager.load_settings() or {}
        except Exception:
            settings = dict(self.config_manager.default_settings or {})

        cfg = self._get_bes_settings_from_ui()
        settings["bes"] = cfg
        try:
            self.config_manager.save_settings(settings)
        except Exception:
            pass

        try:
            with self._bes_cfg_lock:
                self._bes_cfg_cache = dict(cfg or {})
        except Exception:
            pass

        # Live-apply cycle changes
        if self.bes_controller is not None:
            try:
                self.bes_controller.set_cycle_ms(int(cfg.get("cycle_ms", 50)))
            except Exception:
                pass

        if bool(cfg.get("enabled", False)):
            self._bes_tick()

    def _on_bes_ui_changed(self) -> None:
        if self._loading_bes_settings:
            return
        try:
            with self._bes_cfg_lock:
                self._bes_cfg_cache = self._get_bes_settings_from_ui()
        except Exception:
            pass
        try:
            if self._bes_save_timer is not None:
                self._bes_save_timer.start(300)
        except Exception:
            self._save_bes_settings()

    def _reset_bes_to_defaults(self) -> None:
        defaults = self.config_manager.default_settings.get("bes", {}) or {}
        self._loading_bes_settings = True
        try:
            self.bes_enable_chk.setChecked(bool(defaults.get("enabled", False)))
            self.bes_cycle_spin.setValue(int(defaults.get("cycle_ms", 50)))
            self.bes_menu_slider.setValue(int(defaults.get("menu_throttle_percent", 85)))
            self.bes_game_slider.setValue(int(defaults.get("game_throttle_percent", 50)))
            self.bes_menu_val.setText(f"{int(self.bes_menu_slider.value())}%")
            self.bes_game_val.setText(f"{int(self.bes_game_slider.value())}%")
            self.bes_auto_lead_spin.setValue(float(defaults.get("auto_item_lead_s", 3.0)))
            self.bes_auto_grace_spin.setValue(float(defaults.get("auto_item_grace_s", 1.0)))
            for combo in getattr(self, "bes_exempt_combos", None) or []:
                try:
                    combo.setCurrentIndex(0)
                except Exception:
                    pass
        finally:
            self._loading_bes_settings = False
        self._on_bes_enabled_toggled(bool(self.bes_enable_chk.isChecked()))
        self._on_bes_ui_changed()

    def _bes_tick(self) -> None:
        """
        Enforce per-process throttling:
          - Only Roblox processes tracked per-user by the manager get a limiter worker.
          - Exempt users get 0% (no throttling).
          - In-menu vs outside-menu uses separate slider values.
          - Active Auto Item holds force 0% temporarily (handled inside controller).
        """
        ctl = self.bes_controller
        if ctl is None:
            return
        if not (getattr(self, "bes_enable_chk", None) and self.bes_enable_chk.isChecked()):
            return

        try:
            import psutil  # local import to keep GUI import surface small
        except Exception:
            self._bes_update_status("Status: Missing psutil", warning=True)
            return

        cfg = self._get_bes_settings_from_ui()
        cycle_ms = int(cfg.get("cycle_ms", 50) or 50)
        menu_pct = int(cfg.get("menu_throttle_percent", 0) or 0)
        game_pct = int(cfg.get("game_throttle_percent", 0) or 0)
        exempt = {str(u).strip() for u in (cfg.get("exempt_users") or []) if str(u).strip()}

        try:
            ctl.set_cycle_ms(cycle_ms)
        except Exception:
            pass

        # Map pid -> uid and uid -> server from last UI snapshot.
        # Untracked Roblox processes are intentionally untouched.
        pid_to_uid: Dict[int, str] = {}
        uid_to_server: Dict[str, str] = {}
        try:
            for uid, runtime in (self.user_data or {}).items():
                uid_s = str(uid)
                uid_to_server[uid_s] = str((runtime or {}).get("server", "") or "").strip()
                for pid in (runtime or {}).get("pids", []) or []:
                    try:
                        pid_i = int(pid)
                        if pid_i > 0:
                            pid_to_uid[pid_i] = uid_s
                    except Exception:
                        continue
        except Exception:
            pass

        tracked_pids = sorted(pid_to_uid.keys())

        try:
            with self._ms_biome_lock:
                in_menu_by_server = dict(self._ms_in_menu_by_server or {})
        except Exception:
            in_menu_by_server = {}

        targets: Dict[int, int] = {}
        names: Dict[int, str] = {}
        throttled = 0

        for pid in tracked_pids:
            uid = pid_to_uid.get(int(pid), "")
            # Best-effort: skip dead PIDs, but do not scan unrelated processes.
            try:
                proc = psutil.Process(int(pid))
            except Exception:
                continue
            try:
                if proc.name() != "RobloxPlayerBeta.exe":
                    continue
            except Exception:
                continue
            if uid and uid in exempt:
                pct = 0
            else:
                server = uid_to_server.get(uid, "") if uid else ""
                in_menu = True
                if server and server in in_menu_by_server:
                    val = in_menu_by_server.get(server, None)
                    in_menu = True if val is None else bool(val)
                pct = int(menu_pct if in_menu else game_pct)
                if pct > 0:
                    throttled += 1

            targets[int(pid)] = max(0, min(99, pct))
            names[int(pid)] = f"{uid} (PID {pid})" if uid else f"PID {pid}"

        try:
            ctl.apply(targets, names=names)
            snap = ctl.snapshot()
            self._bes_update_status(
                f"Status: Running • Tracked={len(tracked_pids)} • Live={len(targets)} • Throttled={throttled} • Holds={int(snap.get('holds', 0))}"
            )
        except Exception:
            self._bes_update_status("Status: Error", warning=True)

    def _ocr_empty_roi_cfg(self) -> dict:
        return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}

    def _ocr_default_shared_areas(self) -> List[dict]:
        defaults = (self.config_manager.default_settings.get("ocr", {}) or {}).get("shared_areas", [])
        if isinstance(defaults, list):
            return self._merge_ocr_shared_areas(defaults)
        return []

    def _ocr_filter_presets_catalog(self) -> Dict[str, List[dict]]:
        return copy.deepcopy(
            {
                "Egg Hunt": [
                    {
                        "name": "Sky Festival",
                        "r": 190,
                        "g": 134,
                        "b": 223,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: 'Wait. am I still dreaming?'",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                    {
                        "name": "Y.O.L.K.E.G.G",
                        "r": 173,
                        "g": 100,
                        "b": 232,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: Preparing Protocol. 'Do you want to be my friend?'",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                    {
                        "name": "Eggis",
                        "r": 160,
                        "g": 245,
                        "b": 154,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: Scanning. Egg cannon charging 2000%.",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                    {
                        "name": "Eostre",
                        "r": 222,
                        "g": 225,
                        "b": 187,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: 'Let's have an egg hunt here!'",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                    {
                        "name": "Eggore",
                        "r": 232,
                        "g": 33,
                        "b": 121,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: Don't forget to water the 'small plant'.",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                    {
                        "name": "REVIVE",
                        "r": 225,
                        "g": 151,
                        "b": 110,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: 'Holy Eggsus'",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                    {
                        "name": "Eggsistance",
                        "r": 255,
                        "g": 255,
                        "b": 255,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: 'Am I in spaaaace right now?!'",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                    {
                        "name": "Emperor",
                        "r": 228,
                        "g": 107,
                        "b": 84,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: A special egg has spawned.",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                    {
                        "name": "Hatchwarden",
                        "r": 58,
                        "g": 221,
                        "b": 98,
                        "tol": 40,
                        "enabled": True,
                        "target_text": "[Egg Spawned]: A special egg has spawned.",
                        "cooldown_seconds": 600.0,
                        "use_shared_area": True,
                        "shared_area_id": "chat",
                        "roi": self._ocr_empty_roi_cfg(),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": False,
                        "user_ids": None,
                        "behavior": "webhook",
                    },
                ]
            }
        )

    def _ocr_default_filters(self) -> List[dict]:
        defaults = (self.config_manager.default_settings.get("ocr", {}) or {}).get("filters", [])
        if isinstance(defaults, list) and defaults:
            return copy.deepcopy(defaults)
        return copy.deepcopy(get_default_ocr_filters())

    def _ocr_default_filter_map(self) -> Dict[str, dict]:
        return {str(item.get("id") or ""): item for item in self._ocr_default_filters()}

    def _ocr_default_filter_ids(self) -> Set[str]:
        return set(self._ocr_default_filter_map().keys())

    def _ocr_merchant_filter_ids(self) -> Set[str]:
        return {"merchant_jester", "merchant_mari", "merchant_rin"}

    def _ocr_normalize_filter_name(self, raw: str) -> str:
        name = str(raw or "").strip()
        lower = name.lower()
        if lower == "white_text":
            return "Mari"
        if lower == "purple_text":
            return "Jester"
        if lower == "orange_text":
            return "Rin"
        if lower in ("verification", "verification_check", "verification check"):
            return "Verification Check"
        return name

    def _ocr_known_filter_id(self, name: str) -> str:
        lower = self._ocr_normalize_filter_name(name).lower()
        if lower == "jester":
            return "merchant_jester"
        if lower == "mari":
            return "merchant_mari"
        if lower == "rin":
            return "merchant_rin"
        if lower == "verification check":
            return "verification_check"
        return ""

    def _ocr_filter_behavior(self, filter_id: str, name: str, raw_behavior: str = "") -> str:
        behavior = str(raw_behavior or "").strip().lower()
        if behavior in ("merchant", "verification_cap", "webhook"):
            return behavior
        if filter_id in self._ocr_merchant_filter_ids():
            return "merchant"
        if filter_id == "verification_check" or self._ocr_normalize_filter_name(name).lower() == "verification check":
            return "verification_cap"
        return "webhook"

    def _ocr_slugify_filter_id(self, raw: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
        return slug or "filter"

    def _generate_unique_ocr_filter_id(
        self,
        name: str,
        *,
        preferred_id: str = "",
        existing_ids: Optional[Set[str]] = None,
    ) -> str:
        used_ids = {str(v).strip() for v in (existing_ids or set()) if str(v).strip()}
        candidate = str(preferred_id or "").strip()
        if candidate and candidate not in used_ids:
            return candidate

        reserved_ids = set(self._ocr_default_filter_ids()) | used_ids
        slug = self._ocr_slugify_filter_id(name or "filter")
        base = f"custom_{slug}"
        candidate = base
        suffix = 1
        while candidate in reserved_ids:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    def _dedupe_ocr_filter_spec_id(self, spec: Optional[dict], existing_ids: Optional[Set[str]] = None) -> dict:
        out = copy.deepcopy(spec or {})
        current_id = str(out.get("id") or out.get("filter_id") or "").strip()
        unique_id = self._generate_unique_ocr_filter_id(
            str(out.get("name") or "").strip(),
            preferred_id=current_id,
            existing_ids=existing_ids,
        )
        if unique_id != current_id:
            out["id"] = unique_id
            cooldown_group = str(out.get("cooldown_group", "") or "").strip()
            behavior = str(out.get("behavior", "") or "").strip().lower()
            if not cooldown_group or cooldown_group == current_id:
                out["cooldown_group"] = "merchant_filters" if behavior == "merchant" else unique_id
        return out

    def _ocr_filter_ids_in_table(self, *, exclude_row: Optional[int] = None) -> Set[str]:
        ids: Set[str] = set()
        table = getattr(self, "ocr_filter_table", None)
        if table is None:
            return ids
        try:
            row_count = int(table.rowCount())
        except Exception:
            row_count = 0
        for row in range(max(0, row_count)):
            if exclude_row is not None and row == int(exclude_row):
                continue
            btn = self._ocr_unwrap_table_widget(table.cellWidget(row, 6), QPushButton)
            if not isinstance(btn, QPushButton):
                continue
            spec = self._get_ocr_filter_button_meta(btn)
            filter_id = str(spec.get("id") or spec.get("filter_id") or "").strip()
            if filter_id:
                ids.add(filter_id)
        return ids

    def _ocr_slugify_shared_area_id(self, raw: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
        return slug or "area"

    def _normalize_ocr_filter_user_ids(self, raw: Any) -> Optional[List[str]]:
        if raw is None:
            return None
        if not isinstance(raw, (list, tuple, set)):
            return None
        cleaned: List[str] = []
        seen: Set[str] = set()
        for uid in raw:
            uid_s = str(uid or "").strip()
            if not uid_s or uid_s in seen:
                continue
            seen.add(uid_s)
            cleaned.append(uid_s)
        return cleaned

    def _normalize_ocr_shared_area_spec(self, raw: Optional[dict], *, fallback_index: int = 0) -> dict:
        base = raw or {}
        name = str(base.get("name", "") or "").strip() or f"Shared Area {fallback_index + 1}"
        area_id = str(base.get("id") or base.get("area_id") or "").strip()
        if not area_id or area_id.lower() == "chat":
            slug = self._ocr_slugify_shared_area_id(name or uuid.uuid4().hex[:8])
            area_id = f"shared_{fallback_index}_{slug}"
        roi_cfg = base.get("roi") if isinstance(base.get("roi"), dict) else {}
        return {
            "id": area_id,
            "name": name,
            "roi": {
                "x": float((roi_cfg or {}).get("x", 0.0) or 0.0),
                "y": float((roi_cfg or {}).get("y", 0.0) or 0.0),
                "w": float((roi_cfg or {}).get("w", 0.0) or 0.0),
                "h": float((roi_cfg or {}).get("h", 0.0) or 0.0),
            },
        }

    def _merge_ocr_shared_areas(self, areas: List[dict]) -> List[dict]:
        normalized: List[dict] = []
        seen_ids: Set[str] = set()
        for idx, raw in enumerate(areas or []):
            if not isinstance(raw, dict):
                continue
            spec = self._normalize_ocr_shared_area_spec(raw, fallback_index=idx)
            area_id = str(spec.get("id") or "").strip()
            if not area_id or area_id in seen_ids or area_id.lower() == "chat":
                continue
            seen_ids.add(area_id)
            normalized.append(spec)
        return normalized

    def _ocr_shared_areas_from_cfg(self, cfg: Optional[dict]) -> List[dict]:
        raw_areas = (cfg or {}).get("shared_areas")
        if isinstance(raw_areas, list):
            return self._merge_ocr_shared_areas(raw_areas)
        return self._ocr_default_shared_areas()

    def _ocr_chat_area_as_spec(
        self,
        *,
        chat_roi: Optional[Tuple[float, float, float, float]] = None,
    ) -> dict:
        roi = chat_roi if chat_roi is not None else self.ocr_roi
        if roi is None:
            roi_cfg = self._ocr_empty_roi_cfg()
        else:
            roi_cfg = {"x": float(roi[0]), "y": float(roi[1]), "w": float(roi[2]), "h": float(roi[3])}
        return {"id": "chat", "name": "Chat Area", "roi": roi_cfg}

    def _ocr_shared_area_choices(
        self,
        *,
        shared_areas: Optional[List[dict]] = None,
        chat_roi: Optional[Tuple[float, float, float, float]] = None,
    ) -> List[dict]:
        out = [self._ocr_chat_area_as_spec(chat_roi=chat_roi)]
        areas = self.ocr_shared_areas if shared_areas is None else shared_areas
        out.extend(self._merge_ocr_shared_areas(areas or []))
        return out

    def _ocr_shared_area_by_id(
        self,
        area_id: str,
        *,
        shared_areas: Optional[List[dict]] = None,
        chat_roi: Optional[Tuple[float, float, float, float]] = None,
    ) -> Optional[dict]:
        wanted = str(area_id or "").strip()
        for spec in self._ocr_shared_area_choices(shared_areas=shared_areas, chat_roi=chat_roi):
            if str(spec.get("id") or "").strip() == wanted:
                return copy.deepcopy(spec)
        return None

    def _ocr_filter_shared_area_id(self, filter_cfg: Optional[dict]) -> str:
        spec = filter_cfg or {}
        use_shared_area = bool(spec.get("use_shared_area", spec.get("use_chat_area", False)))
        if not use_shared_area:
            return ""
        area_id = str(spec.get("shared_area_id") or "").strip()
        if not area_id and bool(spec.get("use_chat_area", False)):
            area_id = "chat"
        return area_id or "chat"

    def _ocr_filter_uses_chat_area(self, filter_cfg: Optional[dict]) -> bool:
        return self._ocr_filter_shared_area_id(filter_cfg) == "chat"

    def _ocr_shared_area_roi(
        self,
        area_id: str,
        *,
        shared_areas: Optional[List[dict]] = None,
        chat_roi: Optional[Tuple[float, float, float, float]] = None,
    ) -> Optional[Tuple[float, float, float, float]]:
        area = self._ocr_shared_area_by_id(area_id, shared_areas=shared_areas, chat_roi=chat_roi)
        if not isinstance(area, dict):
            return None
        roi_cfg = area.get("roi") if isinstance(area.get("roi"), dict) else {}
        try:
            rx = float(roi_cfg.get("x", 0.0) or 0.0)
            ry = float(roi_cfg.get("y", 0.0) or 0.0)
            rw = float(roi_cfg.get("w", 0.0) or 0.0)
            rh = float(roi_cfg.get("h", 0.0) or 0.0)
        except Exception:
            return None
        if rw <= 0 or rh <= 0:
            return None
        return (rx, ry, rw, rh)

    def _ocr_shared_area_summary_text(self, area_cfg: Optional[dict]) -> str:
        spec = self._normalize_ocr_shared_area_spec(area_cfg or {})
        roi = self._ocr_shared_area_roi(str(spec.get("id") or ""))
        if roi:
            x, y, w, h = roi
            return f"{spec.get('name', 'Shared Area')}: x={x:.3f}, y={y:.3f}, w={w:.3f}, h={h:.3f}"
        return f"{spec.get('name', 'Shared Area')}: not calibrated"

    def _normalize_ocr_filter_spec(self, raw: dict, *, fallback_index: int = 0) -> dict:
        base = raw or {}
        default_map = self._ocr_default_filter_map()
        name = self._ocr_normalize_filter_name(str(base.get("name", "")).strip())
        filter_id = str(base.get("id") or base.get("filter_id") or "").strip()
        if not filter_id:
            filter_id = self._ocr_known_filter_id(name)
        if not filter_id:
            filter_id = self._generate_unique_ocr_filter_id(
                name or f"Filter {fallback_index + 1}",
                existing_ids=set(),
            )
        default_spec = default_map.get(filter_id, {})
        behavior = self._ocr_filter_behavior(filter_id, name, str(base.get("behavior", default_spec.get("behavior", "")) or ""))
        use_shared_area = bool(
            base.get(
                "use_shared_area",
                base.get(
                    "use_chat_area",
                    default_spec.get("use_shared_area", default_spec.get("use_chat_area", behavior == "merchant")),
                ),
            )
        )
        shared_area_id = str(
            base.get(
                "shared_area_id",
                default_spec.get("shared_area_id", "chat" if use_shared_area else ""),
            )
            or ""
        ).strip()
        if not shared_area_id and bool(base.get("use_chat_area", False)):
            shared_area_id = "chat"
        if behavior == "merchant":
            use_shared_area = True
            shared_area_id = "chat"
        if not use_shared_area:
            shared_area_id = ""
        roi_cfg = base.get("roi") if isinstance(base.get("roi"), dict) else default_spec.get("roi", self._ocr_empty_roi_cfg())
        cooldown_group = str(base.get("cooldown_group", default_spec.get("cooldown_group", "")) or "").strip()
        if not cooldown_group:
            cooldown_group = "merchant_filters" if behavior == "merchant" else filter_id
        return {
            "id": filter_id,
            "name": name or str(default_spec.get("name") or filter_id),
            "r": int(base.get("r", default_spec.get("r", 255)) or 0),
            "g": int(base.get("g", default_spec.get("g", 255)) or 0),
            "b": int(base.get("b", default_spec.get("b", 255)) or 0),
            "tol": int(base.get("tol", default_spec.get("tol", 60)) or 0),
            "enabled": bool(base.get("enabled", default_spec.get("enabled", True))),
            "target_text": str(base.get("target_text", default_spec.get("target_text", name)) or "").strip(),
            "cooldown_seconds": float(base.get("cooldown_seconds", default_spec.get("cooldown_seconds", 600)) or 0.0),
            "use_shared_area": bool(use_shared_area),
            "shared_area_id": shared_area_id,
            "roi": {
                "x": float((roi_cfg or {}).get("x", 0.0) or 0.0),
                "y": float((roi_cfg or {}).get("y", 0.0) or 0.0),
                "w": float((roi_cfg or {}).get("w", 0.0) or 0.0),
                "h": float((roi_cfg or {}).get("h", 0.0) or 0.0),
            },
            "webhook_url": str(base.get("webhook_url", "") or "").strip(),
            "webhook_message": str(base.get("webhook_message", "") or ""),
            "send_screenshot": bool(base.get("send_screenshot", default_spec.get("send_screenshot", behavior == "merchant"))),
            "repeat_alert_sound": bool(base.get("repeat_alert_sound", default_spec.get("repeat_alert_sound", False))),
            "user_ids": self._normalize_ocr_filter_user_ids(base.get("user_ids", default_spec.get("user_ids"))),
            "behavior": behavior,
            "cooldown_group": cooldown_group,
            "locked": bool(filter_id in default_map),
        }

    def _merge_ocr_filters_with_defaults(self, filters: List[dict]) -> List[dict]:
        normalized: List[dict] = []
        seen_ids: Set[str] = set()
        for idx, raw in enumerate(filters or []):
            if not isinstance(raw, dict):
                continue
            spec = self._normalize_ocr_filter_spec(raw, fallback_index=idx)
            spec = self._dedupe_ocr_filter_spec_id(spec, seen_ids)
            filter_id = str(spec.get("id") or "").strip()
            if not filter_id:
                continue
            seen_ids.add(filter_id)
            normalized.append(spec)

        by_id = {str(spec.get("id") or ""): spec for spec in normalized}
        out: List[dict] = []
        for default_spec in self._ocr_default_filters():
            fid = str(default_spec.get("id") or "").strip()
            out.append(by_id.pop(fid, self._normalize_ocr_filter_spec(default_spec)))
        out.extend(spec for spec in normalized if str(spec.get("id") or "").strip() in by_id)
        return out

    def _ocr_filters_from_cfg(self, cfg: dict) -> List[dict]:
        raw_filters = cfg.get("filters")
        if isinstance(raw_filters, list) and raw_filters:
            return self._merge_ocr_filters_with_defaults(raw_filters)

        legacy_filters = cfg.get("color_filters")
        if isinstance(legacy_filters, list) and legacy_filters:
            try:
                legacy_cooldown = float(cfg.get("cooldown_seconds", 600) or 600)
            except Exception:
                legacy_cooldown = 600.0
            verification_roi = cfg.get("verification_roi") if isinstance(cfg.get("verification_roi"), dict) else self._ocr_empty_roi_cfg()
            migrated: List[dict] = []
            for idx, raw in enumerate(legacy_filters):
                if not isinstance(raw, dict):
                    continue
                name = self._ocr_normalize_filter_name(str(raw.get("name", "")).strip())
                filter_id = self._ocr_known_filter_id(name)
                default_spec = self._ocr_default_filter_map().get(filter_id, {})
                behavior = self._ocr_filter_behavior(filter_id, name)
                migrated.append(
                    {
                        "id": filter_id,
                        "name": name,
                        "r": int(raw.get("r", default_spec.get("r", 255)) or 0),
                        "g": int(raw.get("g", default_spec.get("g", 255)) or 0),
                        "b": int(raw.get("b", default_spec.get("b", 255)) or 0),
                        "tol": int(raw.get("tol", default_spec.get("tol", 60)) or 0),
                        "enabled": bool(raw.get("enabled", True)),
                        "target_text": str(default_spec.get("target_text") or name or "").strip(),
                        "cooldown_seconds": float(legacy_cooldown),
                        "use_shared_area": bool(behavior == "merchant"),
                        "shared_area_id": "chat" if behavior == "merchant" else "",
                        "roi": copy.deepcopy(verification_roi if filter_id == "verification_check" else self._ocr_empty_roi_cfg()),
                        "webhook_url": "",
                        "webhook_message": "",
                        "send_screenshot": bool(behavior == "merchant"),
                        "repeat_alert_sound": False,
                        "user_ids": None,
                        "behavior": behavior,
                        "cooldown_group": "merchant_filters" if behavior == "merchant" else (filter_id or f"legacy_{idx}"),
                    }
                )
            return self._merge_ocr_filters_with_defaults(migrated)

        return self._ocr_default_filters()

    def _ocr_unwrap_table_widget(self, col_widget: QWidget, typ):
        if col_widget is None:
            return None
        if isinstance(col_widget, typ):
            return col_widget
        try:
            return col_widget.findChild(typ)
        except Exception:
            return None

    def _ocr_wrap_table_cell(
        self,
        widget: QWidget,
        *,
        center: bool = False,
        margins: Tuple[int, int, int, int] = (0, 6, 0, 6),
    ) -> QWidget:
        holder = QWidget()
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        holder.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(*margins)
        lay.setSpacing(0)
        if center:
            lay.addStretch(1)
            lay.addWidget(widget, 0, Qt.AlignmentFlag.AlignCenter)
            lay.addStretch(1)
        else:
            lay.addWidget(widget, 1, Qt.AlignmentFlag.AlignVCenter)
        return holder

    def _ocr_make_rgb_spin(self, value: int, *, maximum: int = 255) -> QSpinBox:
        sb = _AutoItemSpinBox()
        sb.setRange(0, int(maximum))
        sb.setValue(max(0, min(int(maximum), int(value or 0))))
        sb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        try:
            sb.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        except Exception:
            pass
        sb.setMinimumWidth(90)
        sb.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        sb.valueChanged.connect(self._on_ocr_settings_changed)
        return sb

    def _set_ocr_filter_button_meta(self, btn: QPushButton, meta: dict) -> None:
        setattr(btn, "_ocr_filter_meta", copy.deepcopy(meta or {}))

    def _get_ocr_filter_button_meta(self, btn: QPushButton) -> dict:
        try:
            meta = getattr(btn, "_ocr_filter_meta", {}) or {}
        except Exception:
            meta = {}
        return copy.deepcopy(meta)

    def _add_filter_row(self, filter_cfg: Optional[dict] = None):
        base_cfg = filter_cfg or {
            "name": "Blank",
            "r": 255,
            "g": 255,
            "b": 255,
            "tol": 40,
            "enabled": True,
            "target_text": "",
            "cooldown_seconds": 600,
            "use_shared_area": False,
            "shared_area_id": "",
            "roi": self._ocr_empty_roi_cfg(),
            "webhook_url": "",
            "webhook_message": "",
            "repeat_alert_sound": False,
            "user_ids": None,
            "behavior": "webhook",
        }
        spec = self._normalize_ocr_filter_spec(base_cfg, fallback_index=self.ocr_filter_table.rowCount())
        spec = self._dedupe_ocr_filter_spec_id(spec, self._ocr_filter_ids_in_table())
        row = self.ocr_filter_table.rowCount()
        self.ocr_filter_table.insertRow(row)
        try:
            self.ocr_filter_table.setRowHeight(row, 62)
        except Exception:
            pass

        en = QCheckBox()
        en.setChecked(bool(spec.get("enabled", True)))
        en.setStyleSheet("background: transparent;")
        en.toggled.connect(self._on_ocr_settings_changed)
        self.ocr_filter_table.setCellWidget(row, 0, self._ocr_wrap_table_cell(en, center=True))

        name_le = QLineEdit(str(spec.get("name", "") or ""))
        name_le.setPlaceholderText("Filter name")
        name_le.textChanged.connect(self._on_ocr_settings_changed)
        name_le.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if bool(spec.get("locked", False)):
            name_le.setReadOnly(True)
            name_le.setProperty("ocr_default_filter", True)
        self.ocr_filter_table.setCellWidget(row, 1, self._ocr_wrap_table_cell(name_le, center=False, margins=(6, 6, 6, 6)))

        r_sb = self._ocr_make_rgb_spin(int(spec.get("r", 255) or 0), maximum=255)
        g_sb = self._ocr_make_rgb_spin(int(spec.get("g", 255) or 0), maximum=255)
        b_sb = self._ocr_make_rgb_spin(int(spec.get("b", 255) or 0), maximum=255)
        tol_sb = self._ocr_make_rgb_spin(int(spec.get("tol", 60) or 0), maximum=255)

        self.ocr_filter_table.setCellWidget(row, 2, self._ocr_wrap_table_cell(r_sb, center=True))
        self.ocr_filter_table.setCellWidget(row, 3, self._ocr_wrap_table_cell(g_sb, center=True))
        self.ocr_filter_table.setCellWidget(row, 4, self._ocr_wrap_table_cell(b_sb, center=True))
        self.ocr_filter_table.setCellWidget(row, 5, self._ocr_wrap_table_cell(tol_sb, center=True))

        settings_btn = QPushButton("Edit")
        settings_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {ModernStyle.PRIMARY};
                color: {ModernStyle.TEXT_PRIMARY};
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                font-weight: 500;
                min-width: 80px;
                min-height: 28px;
            }}

            QPushButton:hover {{
                background-color: {ModernStyle.PRIMARY_VARIANT};
            }}

            QPushButton:disabled {{
                background-color: {ModernStyle.SURFACE_VARIANT};
                color: {ModernStyle.TEXT_SECONDARY};
            }}
            """
        )
        settings_btn.clicked.connect(lambda _checked=False, btn=settings_btn: self._open_ocr_filter_settings_dialog(btn))
        self._set_ocr_filter_button_meta(settings_btn, spec)
        settings_btn.setToolTip(self._ocr_filter_assignment_summary(spec))
        self.ocr_filter_table.setCellWidget(row, 6, self._ocr_wrap_table_cell(settings_btn, center=True))

    def _remove_selected_filter_rows(self):
        rows = sorted({idx.row() for idx in self.ocr_filter_table.selectedIndexes()}, reverse=True)
        for r in rows:
            spec = self._ocr_filter_row_data(r) or {}
            if bool(spec.get("locked", False)):
                continue
            self.ocr_filter_table.removeRow(r)

    def _load_color_filters_table(self, filters: List[dict]):
        self.ocr_filter_table.setRowCount(0)
        merged = self._ocr_filters_from_cfg({"filters": filters or []} if filters else {})
        for spec in merged:
            self._add_filter_row(spec)

    def _row_for_filter_settings_button(self, btn: QPushButton) -> int:
        rows = self.ocr_filter_table.rowCount()
        for row in range(rows):
            holder = self.ocr_filter_table.cellWidget(row, 6)
            found = self._ocr_unwrap_table_widget(holder, QPushButton)
            if found is btn:
                return row
        return -1

    def _ocr_filter_row_data(self, row: int) -> Optional[dict]:
        if row < 0 or row >= self.ocr_filter_table.rowCount():
            return None
        en = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 0), QCheckBox)
        name_w = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 1), QLineEdit)
        r_w = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 2), QSpinBox)
        g_w = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 3), QSpinBox)
        b_w = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 4), QSpinBox)
        tol_w = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 5), QSpinBox)
        btn = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 6), QPushButton)
        spec = self._get_ocr_filter_button_meta(btn) if isinstance(btn, QPushButton) else {}
        spec["enabled"] = bool(en.isChecked()) if isinstance(en, QCheckBox) else True
        spec["name"] = name_w.text().strip() if isinstance(name_w, QLineEdit) else str(spec.get("name", "") or "")
        spec["r"] = int(r_w.value()) if isinstance(r_w, QSpinBox) else int(spec.get("r", 0) or 0)
        spec["g"] = int(g_w.value()) if isinstance(g_w, QSpinBox) else int(spec.get("g", 0) or 0)
        spec["b"] = int(b_w.value()) if isinstance(b_w, QSpinBox) else int(spec.get("b", 0) or 0)
        spec["tol"] = int(tol_w.value()) if isinstance(tol_w, QSpinBox) else int(spec.get("tol", 0) or 0)
        return self._normalize_ocr_filter_spec(spec, fallback_index=row)

    def _current_color_filters(self, as_dataclass: bool = False, *, chat_only: bool = False):
        filters = []
        seen_ids: Set[str] = set()
        for r in range(self.ocr_filter_table.rowCount()):
            spec = self._ocr_filter_row_data(r)
            if not isinstance(spec, dict):
                continue
            unique_spec = self._dedupe_ocr_filter_spec_id(spec, seen_ids)
            if str(unique_spec.get("id") or "").strip() != str(spec.get("id") or "").strip():
                btn = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(r, 6), QPushButton)
                if isinstance(btn, QPushButton):
                    self._set_ocr_filter_button_meta(btn, unique_spec)
                    try:
                        btn.setToolTip(self._ocr_filter_assignment_summary(unique_spec))
                    except Exception:
                        pass
                spec = unique_spec
            filter_id = str(spec.get("id") or "").strip()
            if not filter_id or filter_id in seen_ids:
                continue
            seen_ids.add(filter_id)
            if chat_only and not self._ocr_filter_uses_chat_area(spec):
                continue
            if as_dataclass:
                filters.append(
                    ColorFilter(
                        str(spec.get("name", "") or "").strip(),
                        int(spec.get("r", 0) or 0),
                        int(spec.get("g", 0) or 0),
                        int(spec.get("b", 0) or 0),
                        int(spec.get("tol", 0) or 0),
                        bool(spec.get("enabled", True)),
                    )
                )
            else:
                filters.append(spec)
        return filters

    def _capture_ocr_reference_image(self) -> Optional[Tuple[Any, Image.Image]]:
        windows = enum_roblox_windows()
        if not windows:
            QMessageBox.warning(self, "No Roblox windows", "No visible Roblox windows were found.")
            return None
        win = windows[0]
        img = capture_window_image(win.hwnd)
        if img is None:
            QMessageBox.warning(self, "Capture failed", f"Could not capture window '{win.title}'.")
            return None
        return win, img

    def _show_pil_preview_dialog(self, title: str, img: Image.Image) -> None:
        pm = pil_to_pixmap(img)
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        v = QVBoxLayout(dlg)
        lbl = QLabel()
        lbl.setPixmap(pm)
        v.addWidget(lbl)
        dlg.resize(pm.width(), pm.height())
        dlg.exec()

    def _ocr_effective_filter_roi(self, filter_cfg: dict) -> Optional[Tuple[float, float, float, float]]:
        area_id = self._ocr_filter_shared_area_id(filter_cfg)
        if area_id:
            return self._ocr_shared_area_roi(area_id)
        roi_cfg = filter_cfg.get("roi") if isinstance(filter_cfg.get("roi"), dict) else {}
        try:
            rx = float(roi_cfg.get("x", 0.0) or 0.0)
            ry = float(roi_cfg.get("y", 0.0) or 0.0)
            rw = float(roi_cfg.get("w", 0.0) or 0.0)
            rh = float(roi_cfg.get("h", 0.0) or 0.0)
        except Exception:
            return None
        if rw <= 0 or rh <= 0:
            return None
        return (rx, ry, rw, rh)

    def _ocr_filter_roi_summary(self, filter_cfg: dict) -> str:
        area_id = self._ocr_filter_shared_area_id(filter_cfg)
        if area_id:
            area = self._ocr_shared_area_by_id(area_id)
            area_name = str((area or {}).get("name") or "Shared Area").strip() or "Shared Area"
            roi = self._ocr_shared_area_roi(area_id)
            if roi:
                x, y, w, h = roi
                return f"Using shared area '{area_name}': x={x:.3f}, y={y:.3f}, w={w:.3f}, h={h:.3f}"
            return f"Using shared area '{area_name}': not calibrated"
        roi = self._ocr_effective_filter_roi(filter_cfg)
        if roi:
            x, y, w, h = roi
            return f"Custom ROI: x={x:.3f}, y={y:.3f}, w={w:.3f}, h={h:.3f}"
        return "Custom ROI: not calibrated"

    def _preview_ocr_filter_area(self, filter_cfg: dict) -> None:
        roi = self._ocr_effective_filter_roi(filter_cfg)
        if roi is None:
            QMessageBox.warning(self, "Calibrate OCR", "Please calibrate the selected area first.")
            return
        windows = enum_roblox_windows()
        if not windows:
            QMessageBox.warning(self, "No Roblox windows", "No visible Roblox windows were found.")
            return
        win = windows[0]
        img = capture_window_image(win.hwnd, roi)
        if img is None:
            QMessageBox.warning(self, "Capture failed", f"Could not capture window '{win.title}'.")
            return
        if self.ocr_preprocess_chk.isChecked():
            img_to_show = preprocess_for_ocr(
                img,
                [
                    ColorFilter(
                        str(filter_cfg.get("name", "") or ""),
                        int(filter_cfg.get("r", 0) or 0),
                        int(filter_cfg.get("g", 0) or 0),
                        int(filter_cfg.get("b", 0) or 0),
                        int(filter_cfg.get("tol", 0) or 0),
                        True,
                    )
                ],
            )
        else:
            img_to_show = img
        self._show_pil_preview_dialog(f"{str(filter_cfg.get('name') or 'Filter')} Preview", img_to_show)

    def _pick_ocr_filter_color(self) -> Optional[Tuple[int, int, int]]:
        captured = self._capture_ocr_reference_image()
        if not captured:
            return None
        _win, img = captured
        dlg = PointPickDialog(pil_to_pixmap(img), "Pick OCR Color", parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        picked = dlg.selected_point()
        if not picked:
            return None
        _xf, _yf, px, py = picked
        rgb = img.convert("RGB")
        try:
            r, g, b = rgb.getpixel((int(px), int(py)))[:3]
        except Exception:
            return None
        return int(r), int(g), int(b)

    def _pick_ocr_filter_roi(self, title: str) -> Optional[dict]:
        captured = self._capture_ocr_reference_image()
        if not captured:
            return None
        _win, img = captured
        dlg = ROICropDialog(pil_to_pixmap(img), self, title=title, hint="Drag to draw the OCR area. Release to save.")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        roi = dlg.selected_roi()
        if not roi:
            return None
        return {"x": float(roi[0]), "y": float(roi[1]), "w": float(roi[2]), "h": float(roi[3])}

    def _preview_ocr_shared_area(self, area_cfg: Optional[dict]) -> None:
        spec = self._normalize_ocr_shared_area_spec(area_cfg or {})
        roi = self._ocr_shared_area_roi(str(spec.get("id") or ""), shared_areas=[spec])
        if roi is None:
            QMessageBox.warning(self, "Shared Area", "Please calibrate the shared area first.")
            return
        windows = enum_roblox_windows()
        if not windows:
            QMessageBox.warning(self, "No Roblox windows", "No visible Roblox windows were found.")
            return
        win = windows[0]
        img = capture_window_image(win.hwnd, roi)
        if img is None:
            QMessageBox.warning(self, "Capture failed", f"Could not capture window '{win.title}'.")
            return
        self._show_pil_preview_dialog(f"{str(spec.get('name') or 'Shared Area')} Preview", img)

    def _open_ocr_shared_area_editor_dialog(self, area_cfg: Optional[dict] = None, *, fallback_index: int = 0) -> Optional[dict]:
        base = self._normalize_ocr_shared_area_spec(area_cfg or {}, fallback_index=fallback_index)
        current_roi = copy.deepcopy(base.get("roi") or self._ocr_empty_roi_cfg())

        dlg = QDialog(self)
        dlg.setWindowTitle("Shared Area")
        dlg.resize(520, 220)
        layout = QVBoxLayout(dlg)

        form = QFormLayout()
        name_le = QLineEdit(str(base.get("name", "") or ""))
        name_le.setPlaceholderText("Area name")
        form.addRow("Name:", name_le)

        area_row = QWidget()
        area_layout = QHBoxLayout(area_row)
        area_layout.setContentsMargins(0, 0, 0, 0)
        area_layout.setSpacing(8)
        area_btn = QPushButton("Calibrate Area")
        preview_btn = QPushButton("Preview Area")
        area_layout.addWidget(area_btn)
        area_layout.addWidget(preview_btn)
        area_layout.addStretch()
        form.addRow("Area:", area_row)

        area_label = QLabel()
        area_label.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        form.addRow("", area_label)
        layout.addLayout(form)

        def _current_area_spec() -> dict:
            popup_spec = copy.deepcopy(base)
            popup_spec["name"] = name_le.text().strip()
            popup_spec["roi"] = copy.deepcopy(current_roi)
            return self._normalize_ocr_shared_area_spec(popup_spec, fallback_index=fallback_index)

        def _refresh_area_state() -> None:
            popup_spec = _current_area_spec()
            area_label.setText(self._ocr_shared_area_summary_text(popup_spec))
            preview_btn.setEnabled(self._ocr_shared_area_roi(str(popup_spec.get("id") or ""), shared_areas=[popup_spec]) is not None)

        def _pick_area() -> None:
            nonlocal current_roi
            roi_cfg = self._pick_ocr_filter_roi(f"Select {str(name_le.text().strip() or base.get('name') or 'Shared Area')} Area")
            if roi_cfg:
                current_roi = roi_cfg
                _refresh_area_state()

        def _preview_area() -> None:
            self._preview_ocr_shared_area(_current_area_spec())

        name_le.textChanged.connect(_refresh_area_state)
        area_btn.clicked.connect(_pick_area)
        preview_btn.clicked.connect(_preview_area)
        _refresh_area_state()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        saved = _current_area_spec()
        if not str(saved.get("name", "") or "").strip():
            QMessageBox.warning(self, "Shared Area", "Please enter a name for the shared area.")
            return None
        if self._ocr_shared_area_roi(str(saved.get("id") or ""), shared_areas=[saved]) is None:
            QMessageBox.warning(self, "Shared Area", "Please calibrate the shared area before saving.")
            return None
        return saved

    def _open_ocr_shared_areas_dialog(self) -> None:
        areas = self._merge_ocr_shared_areas(self.ocr_shared_areas)

        dlg = QDialog(self)
        dlg.setWindowTitle("Shared OCR Areas")
        dlg.resize(620, 420)
        layout = QVBoxLayout(dlg)

        info = QLabel("Create reusable OCR areas here. Filters can then use a shared area instead of storing their own ROI.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(info)

        area_list = QListWidget()
        detail_label = QLabel("Select a shared area to view its bounds.")
        detail_label.setWordWrap(True)
        detail_label.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(area_list)
        layout.addWidget(detail_label)

        def _refresh_list(selected_id: str = "") -> None:
            area_list.clear()
            chosen_row = -1
            for idx, area in enumerate(areas):
                item = QListWidgetItem(str(area.get("name", "Shared Area") or "Shared Area"))
                item.setData(Qt.ItemDataRole.UserRole, str(area.get("id") or "").strip())
                item.setToolTip(self._ocr_shared_area_summary_text(area))
                area_list.addItem(item)
                if selected_id and str(area.get("id") or "").strip() == selected_id:
                    chosen_row = idx
            if chosen_row >= 0:
                area_list.setCurrentRow(chosen_row)
            elif area_list.count() > 0:
                area_list.setCurrentRow(0)
            else:
                detail_label.setText("Select a shared area to view its bounds.")

        def _selected_area_index() -> int:
            row = area_list.currentRow()
            if row < 0 or row >= len(areas):
                return -1
            return row

        def _update_detail() -> None:
            idx = _selected_area_index()
            if idx < 0:
                detail_label.setText("Select a shared area to view its bounds.")
                return
            detail_label.setText(self._ocr_shared_area_summary_text(areas[idx]))

        def _add_area() -> None:
            saved = self._open_ocr_shared_area_editor_dialog(None, fallback_index=len(areas))
            if not saved:
                return
            areas.append(saved)
            selected_id = str(saved.get("id") or "").strip()
            _refresh_list(selected_id)
            _update_detail()

        def _edit_area() -> None:
            idx = _selected_area_index()
            if idx < 0:
                QMessageBox.information(dlg, "Shared Areas", "Select a shared area to edit.")
                return
            saved = self._open_ocr_shared_area_editor_dialog(areas[idx], fallback_index=idx)
            if not saved:
                return
            areas[idx] = saved
            _refresh_list(str(saved.get("id") or "").strip())
            _update_detail()

        def _remove_area() -> None:
            idx = _selected_area_index()
            if idx < 0:
                QMessageBox.information(dlg, "Shared Areas", "Select a shared area to remove.")
                return
            name = str(areas[idx].get("name", "Shared Area") or "Shared Area")
            answer = QMessageBox.question(
                dlg,
                "Remove Shared Area",
                f"Remove shared area '{name}'?\nFilters using it will need to be updated manually.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            del areas[idx]
            _refresh_list()
            _update_detail()

        def _preview_selected() -> None:
            idx = _selected_area_index()
            if idx < 0:
                QMessageBox.information(dlg, "Shared Areas", "Select a shared area to preview.")
                return
            self._preview_ocr_shared_area(areas[idx])

        area_list.currentRowChanged.connect(lambda _row: _update_detail())

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Area")
        edit_btn = QPushButton("Edit Area")
        remove_btn = QPushButton("Remove Area")
        preview_btn = QPushButton("Preview Area")
        add_btn.clicked.connect(_add_area)
        edit_btn.clicked.connect(_edit_area)
        remove_btn.clicked.connect(_remove_area)
        preview_btn.clicked.connect(_preview_selected)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(preview_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        _refresh_list()
        _update_detail()

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self.ocr_shared_areas = self._merge_ocr_shared_areas(areas)
        self._on_ocr_settings_changed()

    def _ocr_filter_assignment_summary(self, spec: Optional[dict], user_map: Optional[dict] = None) -> str:
        clean_ids = self._normalize_ocr_filter_user_ids((spec or {}).get("user_ids", None))
        if clean_ids is None:
            return "All users."
        if not clean_ids:
            return "No users selected."
        if user_map is None:
            try:
                user_map = self.config_manager.load_users() or {}
            except Exception:
                user_map = {}
        names: List[str] = []
        for uid in clean_ids:
            info = user_map.get(uid, {}) if isinstance(user_map, dict) else {}
            names.append(str(info.get("username") or uid))
        preview = ", ".join(names[:4])
        suffix = "..." if len(names) > 4 else ""
        return f"Users: {preview}{suffix}"

    def _apply_ocr_filter_user_ids_to_row(self, row: int, user_ids: Optional[List[str]]) -> None:
        btn = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 6), QPushButton)
        if not isinstance(btn, QPushButton):
            return
        spec = self._get_ocr_filter_button_meta(btn)
        spec["user_ids"] = self._normalize_ocr_filter_user_ids(user_ids)
        self._set_ocr_filter_button_meta(btn, spec)
        tip = self._ocr_filter_assignment_summary(spec)
        try:
            btn.setToolTip(tip)
        except Exception:
            pass

    def _open_ocr_filter_user_assignments_dialog(self) -> None:
        rows = self.ocr_filter_table.rowCount()
        if rows <= 0:
            QMessageBox.information(self, "OCR Filter Users", "Add a filter before assigning users.")
            return

        try:
            users_cfg = self.config_manager.load_users() or {}
        except Exception:
            users_cfg = {}

        user_choices = [
            {
                "id": str(uid).strip(),
                "username": str(info.get("username", uid)).strip(),
            }
            for uid, info in (users_cfg.items() if isinstance(users_cfg, dict) else [])
            if str(uid).strip()
        ]
        user_choices.sort(key=lambda item: item["username"].lower())
        choice_ids = [u["id"] for u in user_choices]
        choice_set = set(choice_ids)

        entries: List[dict] = []
        for row in range(rows):
            spec = self._ocr_filter_row_data(row) or {}
            filter_id = str(spec.get("id") or "").strip()
            filter_name = str(spec.get("name") or filter_id or f"Filter {row + 1}").strip()
            raw_selected = self._normalize_ocr_filter_user_ids(spec.get("user_ids", None))
            if raw_selected is None:
                selected = set(choice_ids)
                explicit = False
            else:
                selected = set(raw_selected)
                explicit = True
            entries.append(
                {
                    "row": row,
                    "id": filter_id,
                    "name": filter_name,
                    "selected": selected,
                    "explicit": explicit,
                }
            )

        dlg = QDialog(self)
        dlg.setWindowTitle("OCR Filter Users")
        dlg.resize(760, 540)

        outer = QVBoxLayout(dlg)
        only_mapped_chk = QCheckBox("Only search mapped PIDs")
        only_mapped_chk.setChecked(bool(getattr(self, "ocr_only_mapped_pids", False)))
        only_mapped_chk.setToolTip("When enabled, OCR skips Roblox processes that are not mapped to a known user.")
        outer.addWidget(only_mapped_chk)

        h = QHBoxLayout()
        outer.addLayout(h)

        left_col = QVBoxLayout()
        filter_list = QListWidget()
        filter_list.setMinimumWidth(260)
        for entry in entries:
            item = QListWidgetItem(entry["name"])
            item.setToolTip(self._ocr_filter_assignment_summary({"user_ids": None if not entry["explicit"] else sorted(entry["selected"])}, users_cfg))
            filter_list.addItem(item)
        left_col.addWidget(filter_list)
        left_col.addStretch()
        h.addLayout(left_col)

        right = QVBoxLayout()
        h.addLayout(right)
        right.addWidget(QLabel("Choose which users each OCR filter applies to:"))

        btn_row = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        deselect_all_btn = QPushButton("Deselect All")
        right.addLayout(btn_row)
        btn_row.addWidget(select_all_btn)
        btn_row.addWidget(deselect_all_btn)
        btn_row.addStretch()

        user_scroll = QScrollArea()
        user_scroll.setWidgetResizable(True)
        user_container = QWidget()
        user_layout = QVBoxLayout(user_container)
        user_layout.setContentsMargins(6, 6, 6, 6)
        user_scroll.setWidget(user_container)
        right.addWidget(user_scroll)

        helper_label = QLabel()
        helper_label.setWordWrap(True)
        helper_label.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        right.addWidget(helper_label)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        right.addWidget(btn_box)

        current_checks: List[QCheckBox] = []

        def _clear_user_layout() -> None:
            nonlocal current_checks
            current_checks = []
            while user_layout.count():
                item = user_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

        def _entry_summary(entry: dict) -> str:
            if not entry.get("explicit", False):
                return "All users."
            selected = sorted({str(uid).strip() for uid in (entry.get("selected") or set()) if str(uid).strip()})
            return self._ocr_filter_assignment_summary({"user_ids": selected}, users_cfg)

        def _refresh_filter_list_labels() -> None:
            for idx, entry in enumerate(entries):
                item = filter_list.item(idx)
                if item is None:
                    continue
                item.setToolTip(_entry_summary(entry))

        def _current_entry() -> Optional[dict]:
            idx = filter_list.currentRow()
            if idx < 0 or idx >= len(entries):
                return None
            return entries[idx]

        def _recompute_entry_from_checks() -> None:
            entry = _current_entry()
            if entry is None:
                return
            selected = {str(cb.property("user_id") or "").strip() for cb in current_checks if cb.isChecked() and str(cb.property("user_id") or "").strip()}
            entry["selected"] = selected
            entry["explicit"] = selected != choice_set
            _refresh_filter_list_labels()
            helper_label.setText(_entry_summary(entry))

        def _build_user_checks(_idx: int) -> None:
            _clear_user_layout()
            entry = _current_entry()
            if entry is None:
                helper_label.setText("Select a filter to edit.")
                return

            selected = set(entry.get("selected") or set())
            if not user_choices:
                user_layout.addWidget(QLabel("No users are available in users.json."))
                helper_label.setText("No users available.")
                return

            extra_ids = sorted(selected - choice_set)
            full_choices = list(user_choices)
            for uid in extra_ids:
                full_choices.append({"id": uid, "username": f"{uid} (not in users.json)"})

            for user in full_choices:
                uid = str(user["id"]).strip()
                cb = QCheckBox(f"{user['username']} [{uid}]")
                cb.setChecked(uid in selected)
                cb.setProperty("user_id", uid)
                cb.toggled.connect(lambda _checked=False: _recompute_entry_from_checks())
                current_checks.append(cb)
                user_layout.addWidget(cb)
            user_layout.addStretch()
            helper_label.setText(_entry_summary(entry))

        def _set_all_checks(state: bool) -> None:
            for cb in current_checks:
                cb.setChecked(bool(state))
            _recompute_entry_from_checks()

        filter_list.currentRowChanged.connect(_build_user_checks)
        select_all_btn.clicked.connect(lambda: _set_all_checks(True))
        deselect_all_btn.clicked.connect(lambda: _set_all_checks(False))
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        filter_list.setCurrentRow(0 if entries else -1)
        _refresh_filter_list_labels()

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self.ocr_only_mapped_pids = bool(only_mapped_chk.isChecked())
        for entry in entries:
            normalized = None if not entry.get("explicit", False) else sorted(
                {str(uid).strip() for uid in (entry.get("selected") or set()) if str(uid).strip()}
            )
            self._apply_ocr_filter_user_ids_to_row(int(entry.get("row", -1)), normalized)
        self._on_ocr_settings_changed()

    def _open_ocr_filter_presets_dialog(self) -> None:
        catalog = self._ocr_filter_presets_catalog()
        if not catalog:
            QMessageBox.information(self, "OCR Filter Presets", "No OCR filter presets are available.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("OCR Filter Presets")
        dlg.resize(780, 575)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        info = QLabel(
            "Choose a preset category, then select one or more presets to add to the OCR filters table. "
            "The webhook settings below will be copied into each added preset."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        info.setContentsMargins(0, 0, 0, 2)
        layout.addWidget(info)

        category_row = QHBoxLayout()
        category_row.setContentsMargins(0, 0, 0, 0)
        category_row.setSpacing(6)
        category_row.addWidget(QLabel("Category:"))
        category_combo = QComboBox()
        for category_name in catalog.keys():
            category_combo.addItem(category_name)
        category_row.addWidget(category_combo, 1)
        layout.addLayout(category_row)

        preset_list = QListWidget()
        preset_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(preset_list, 1)

        detail_label = QLabel("Select a preset to preview its text and color settings.")
        detail_label.setWordWrap(True)
        detail_label.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        detail_label.setMaximumHeight(52)
        detail_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(detail_label)

        webhook_group = QGroupBox("Webhook For Added Presets")
        webhook_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        webhook_form = QFormLayout(webhook_group)
        webhook_form.setContentsMargins(8, 8, 8, 8)
        webhook_form.setSpacing(6)

        preset_hook_le = QLineEdit()
        preset_hook_le.setPlaceholderText("https://discord.com/api/webhooks/…")
        webhook_form.addRow("Webhook URL:", preset_hook_le)

        preset_msg_le = QLineEdit()
        preset_msg_le.setPlaceholderText("{filter} detected in {username} (PID {pid})")
        preset_msg_le.setToolTip(
            "Available placeholders: {filter}, {username}, {owner}, "
            "{server_label}, {pid}, {ps_link}, {user_id}"
        )
        webhook_form.addRow("Message:", preset_msg_le)

        layout.addWidget(webhook_group)

        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(6)
        add_selected_btn = QPushButton("Add Selected")
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.reject)
        footer_row.addWidget(add_selected_btn)
        footer_row.addStretch()
        footer_row.addWidget(close_btn)
        layout.addLayout(footer_row)

        def _current_presets() -> List[dict]:
            return list(catalog.get(str(category_combo.currentText() or "").strip(), []) or [])

        def _refresh_preset_list() -> None:
            preset_list.clear()
            for idx, preset in enumerate(_current_presets()):
                item = QListWidgetItem(str(preset.get("name", f"Preset {idx + 1}") or f"Preset {idx + 1}"))
                item.setData(Qt.ItemDataRole.UserRole, idx)
                item.setToolTip(str(preset.get("target_text", "") or ""))
                preset_list.addItem(item)
            if preset_list.count() > 0:
                preset_list.setCurrentRow(0)
            else:
                detail_label.setText("No presets are available in this category.")

        def _update_detail() -> None:
            selected_items = preset_list.selectedItems()
            presets = _current_presets()
            if len(selected_items) != 1:
                if selected_items:
                    detail_label.setText(f"{len(selected_items)} presets selected.")
                else:
                    detail_label.setText("Select a preset to preview its text and color settings.")
                return
            try:
                idx = int(selected_items[0].data(Qt.ItemDataRole.UserRole))
                preset = presets[idx]
            except Exception:
                detail_label.setText("Select a preset to preview its text and color settings.")
                return
            detail_label.setText(
                f"Name: {str(preset.get('name') or '').strip()}\n"
                f"Text: {str(preset.get('target_text') or '').strip()}\n"
                f"Color: R={int(preset.get('r', 0) or 0)}, G={int(preset.get('g', 0) or 0)}, "
                f"B={int(preset.get('b', 0) or 0)}, Tol={int(preset.get('tol', 0) or 0)}"
            )

        def _add_selected_presets() -> None:
            selected_items = preset_list.selectedItems()
            if not selected_items:
                QMessageBox.information(dlg, "OCR Filter Presets", "Select at least one preset to add.")
                return
            presets = _current_presets()
            webhook_url = preset_hook_le.text().strip()
            webhook_message = preset_msg_le.text()
            for item in selected_items:
                try:
                    idx = int(item.data(Qt.ItemDataRole.UserRole))
                    preset_cfg = copy.deepcopy(presets[idx])
                except Exception:
                    continue
                preset_cfg["webhook_url"] = webhook_url
                preset_cfg["webhook_message"] = webhook_message
                self._add_filter_row(preset_cfg)
            self._on_ocr_settings_changed()
            dlg.accept()

        category_combo.currentIndexChanged.connect(lambda _idx: _refresh_preset_list())
        preset_list.itemSelectionChanged.connect(_update_detail)
        add_selected_btn.clicked.connect(_add_selected_presets)
        _refresh_preset_list()
        _update_detail()
        dlg.exec()

    def _open_ocr_filter_settings_dialog(self, btn: QPushButton) -> None:
        row = self._row_for_filter_settings_button(btn)
        spec = self._ocr_filter_row_data(row)
        if not isinstance(spec, dict):
            return

        is_locked = bool(spec.get("locked", False))
        is_merchant = str(spec.get("id") or "") in self._ocr_merchant_filter_ids() or str(spec.get("behavior") or "") == "merchant"
        current_roi = copy.deepcopy(spec.get("roi") or self._ocr_empty_roi_cfg())
        current_shared_area_id = self._ocr_filter_shared_area_id(spec) or "chat"

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Filter Settings - {spec.get('name', 'Filter')}")
        dlg.resize(560, 520)
        layout = QVBoxLayout(dlg)

        form = QFormLayout()

        cooldown_spin = _AutoItemSpinBox()
        cooldown_spin.setRange(0, 86400)
        cooldown_spin.setSuffix(" s")
        cooldown_spin.setValue(max(0, int(float(spec.get("cooldown_seconds", 600) or 0))))
        form.addRow("Cooldown:", cooldown_spin)

        target_text = QTextEdit()
        target_text.setPlainText(str(spec.get("target_text", "") or ""))
        target_text.setMinimumHeight(72)
        target_text.setMaximumHeight(110)
        form.addRow("Text to detect:", target_text)

        hook_le = QLineEdit(str(spec.get("webhook_url", "") or ""))
        hook_le.setPlaceholderText("https://discord.com/api/webhooks/…")
        form.addRow("Webhook URL:", hook_le)

        msg_te = QTextEdit()
        msg_te.setPlainText(str(spec.get("webhook_message", "") or ""))
        msg_te.setMinimumHeight(72)
        msg_te.setMaximumHeight(120)
        msg_te.setToolTip("Available placeholders: {filter}, {username}, {owner}, {server_label}, {pid}, {ps_link}, {user_id}")
        form.addRow("Message:", msg_te)

        send_screenshot_chk = QCheckBox("Attach screenshot to webhook")
        send_screenshot_chk.setChecked(bool(spec.get("send_screenshot", False)))
        if is_merchant:
            send_screenshot_chk.setEnabled(False)
            send_screenshot_chk.setToolTip("Merchant filters already attach the screenshot through the built-in merchant webhook system.")
        else:
            form.addRow("", send_screenshot_chk)

        repeat_alert_chk = QCheckBox("Repeat alert sound on match")
        repeat_alert_chk.setChecked(bool(spec.get("repeat_alert_sound", False)))
        repeat_alert_chk.setToolTip("Plays the same repeating alert sound used by Anti-AFK until stopped.")
        form.addRow("", repeat_alert_chk)

        color_row = QWidget()
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(8)
        r_sb = self._ocr_make_rgb_spin(int(spec.get("r", 0) or 0), maximum=255)
        g_sb = self._ocr_make_rgb_spin(int(spec.get("g", 0) or 0), maximum=255)
        b_sb = self._ocr_make_rgb_spin(int(spec.get("b", 0) or 0), maximum=255)
        tol_sb = self._ocr_make_rgb_spin(int(spec.get("tol", 0) or 0), maximum=255)
        pick_color_btn = QPushButton("Pick Color")
        color_layout.addWidget(QLabel("R"))
        color_layout.addWidget(r_sb)
        color_layout.addWidget(QLabel("G"))
        color_layout.addWidget(g_sb)
        color_layout.addWidget(QLabel("B"))
        color_layout.addWidget(b_sb)
        color_layout.addWidget(QLabel("Tol"))
        color_layout.addWidget(tol_sb)
        color_layout.addWidget(pick_color_btn)
        form.addRow("Color:", color_row)

        use_shared_chk = QCheckBox("Use shared area")
        use_shared_chk.setChecked(bool(spec.get("use_shared_area", spec.get("use_chat_area", False))))
        form.addRow("", use_shared_chk)

        shared_area_combo = QComboBox()
        last_shared_area_id = current_shared_area_id
        form.addRow("Shared area:", shared_area_combo)

        area_row = QWidget()
        area_layout = QHBoxLayout(area_row)
        area_layout.setContentsMargins(0, 0, 0, 0)
        area_layout.setSpacing(8)
        area_btn = QPushButton("Calibrate Area")
        preview_btn = QPushButton("Preview Area")
        area_layout.addWidget(area_btn)
        area_layout.addWidget(preview_btn)
        area_layout.addStretch()
        form.addRow("Area:", area_row)

        area_label = QLabel()
        area_label.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        form.addRow("", area_label)

        layout.addLayout(form)

        if is_merchant:
            hook_le.setEnabled(False)
            msg_te.setEnabled(False)
            use_shared_chk.setChecked(True)
            use_shared_chk.setEnabled(False)
            hook_le.setToolTip("Merchant filters continue using the built-in merchant webhook system.")
            msg_te.setToolTip("Merchant filters continue using the built-in merchant webhook system.")

        def _populate_shared_area_combo(selected_id: str = "") -> None:
            target_id = str(selected_id or "").strip() or "chat"
            try:
                shared_area_combo.blockSignals(True)
                shared_area_combo.clear()
                if not use_shared_chk.isChecked() and not is_merchant:
                    return
                for area in self._ocr_shared_area_choices():
                    shared_area_combo.addItem(str(area.get("name", "Shared Area") or "Shared Area"), str(area.get("id") or ""))
                combo_idx = shared_area_combo.findData(target_id)
                if combo_idx < 0:
                    combo_idx = shared_area_combo.findData("chat")
                if combo_idx >= 0:
                    shared_area_combo.setCurrentIndex(combo_idx)
            finally:
                shared_area_combo.blockSignals(False)

        _populate_shared_area_combo(current_shared_area_id)

        def _current_popup_spec() -> dict:
            popup_spec = copy.deepcopy(spec)
            popup_spec["cooldown_seconds"] = float(cooldown_spin.value())
            popup_spec["target_text"] = target_text.toPlainText().strip()
            popup_spec["webhook_url"] = hook_le.text().strip()
            popup_spec["webhook_message"] = msg_te.toPlainText()
            popup_spec["send_screenshot"] = bool(send_screenshot_chk.isChecked()) if not is_merchant else bool(spec.get("send_screenshot", True))
            popup_spec["repeat_alert_sound"] = bool(repeat_alert_chk.isChecked())
            popup_spec["r"] = int(r_sb.value())
            popup_spec["g"] = int(g_sb.value())
            popup_spec["b"] = int(b_sb.value())
            popup_spec["tol"] = int(tol_sb.value())
            popup_spec["use_shared_area"] = bool(use_shared_chk.isChecked()) if not is_merchant else True
            popup_spec["shared_area_id"] = str(shared_area_combo.currentData() or "").strip() if popup_spec["use_shared_area"] else ""
            popup_spec["roi"] = copy.deepcopy(current_roi)
            return popup_spec

        def _refresh_area_controls() -> None:
            nonlocal last_shared_area_id
            current_data = str(shared_area_combo.currentData() or "").strip()
            if current_data:
                last_shared_area_id = current_data
            _populate_shared_area_combo(last_shared_area_id)
            popup_spec = _current_popup_spec()
            area_btn.setEnabled(not popup_spec.get("use_shared_area", False))
            shared_area_combo.setEnabled(bool(popup_spec.get("use_shared_area", False)) and not is_merchant)
            preview_btn.setEnabled(self._ocr_effective_filter_roi(popup_spec) is not None)
            area_label.setText(self._ocr_filter_roi_summary(popup_spec))

        def _shared_area_changed(_idx: int) -> None:
            nonlocal last_shared_area_id
            current_data = str(shared_area_combo.currentData() or "").strip()
            if current_data:
                last_shared_area_id = current_data
            _refresh_area_controls()

        use_shared_chk.toggled.connect(_refresh_area_controls)
        shared_area_combo.currentIndexChanged.connect(_shared_area_changed)

        def _pick_color() -> None:
            picked = self._pick_ocr_filter_color()
            if not picked:
                return
            rv, gv, bv = picked
            r_sb.setValue(rv)
            g_sb.setValue(gv)
            b_sb.setValue(bv)

        def _pick_area() -> None:
            nonlocal current_roi
            roi_cfg = self._pick_ocr_filter_roi(f"Select {str(spec.get('name') or 'Filter')} Area")
            if roi_cfg:
                current_roi = roi_cfg
                _refresh_area_controls()

        def _preview_area() -> None:
            self._preview_ocr_filter_area(_current_popup_spec())

        pick_color_btn.clicked.connect(_pick_color)
        area_btn.clicked.connect(_pick_area)
        preview_btn.clicked.connect(_preview_area)
        _refresh_area_controls()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        saved = self._normalize_ocr_filter_spec(_current_popup_spec(), fallback_index=row)
        name_w = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, 1), QLineEdit)
        if isinstance(name_w, QLineEdit):
            name_w.setText(str(saved.get("name", "") or ""))
        for col, key in ((2, "r"), (3, "g"), (4, "b"), (5, "tol")):
            sb = self._ocr_unwrap_table_widget(self.ocr_filter_table.cellWidget(row, col), QSpinBox)
            if isinstance(sb, QSpinBox):
                sb.setValue(int(saved.get(key, 0) or 0))
        self._set_ocr_filter_button_meta(btn, saved)
        self._on_ocr_settings_changed()

    def _get_ocr_settings_from_ui(self) -> dict:
        roi = self.ocr_roi or (0.0, 0.0, 0.0, 0.0)
        filters = []
        for spec in self._current_color_filters(as_dataclass=False):
            clean = dict(spec or {})
            clean.pop("locked", None)
            filters.append(clean)
        return {
            "enabled": bool(self.ocr_enable_chk.isChecked()),
            "only_mapped_pids": bool(getattr(self, "ocr_only_mapped_pids", False)),
            "workers": self.ocr_workers_spin.value(),
            "max_captures_per_second": self.ocr_max_caps_spin.value(),
            "batch_delay_seconds": float(self.ocr_batch_delay_spin.value()),
            "use_preprocess": bool(self.ocr_preprocess_chk.isChecked()),
            "frame_diff_tolerance": int(self.ocr_frame_diff_tol_spin.value()),
            "log_ocr_text": bool(getattr(self, "ocr_log_text_chk", None) and self.ocr_log_text_chk.isChecked()),
            "log_loop": bool(getattr(self, "ocr_loop_logs_chk", None) and self.ocr_loop_logs_chk.isChecked()),
            "device_id": self.ocr_device_combo.currentData() if hasattr(self, "ocr_device_combo") else None,
            "roi": {"x": roi[0], "y": roi[1], "w": roi[2], "h": roi[3]},
            "shared_areas": self._merge_ocr_shared_areas(self.ocr_shared_areas),
            "filters": filters,
        }

    def _apply_ocr_settings_to_ui(self, cfg: dict):
        defaults = self.config_manager.default_settings.get("ocr", {}) or {}
        cfg = cfg or defaults
        self._loading_ocr_settings = True
        try:
            target_enabled = bool(cfg.get("enabled", False))
            self.ocr_enable_chk.setChecked(target_enabled)
            self.ocr_only_mapped_pids = bool(cfg.get("only_mapped_pids", defaults.get("only_mapped_pids", False)))

            self.ocr_workers_spin.setValue(int(cfg.get("workers", defaults.get("workers", 1))))
            self.ocr_max_caps_spin.setValue(int(cfg.get("max_captures_per_second", defaults.get("max_captures_per_second", 20))))
            self.ocr_batch_delay_spin.setValue(float(cfg.get("batch_delay_seconds", defaults.get("batch_delay_seconds", 1.0))))
            self.ocr_preprocess_chk.setChecked(bool(cfg.get("use_preprocess", defaults.get("use_preprocess", True))))
            self.ocr_frame_diff_tol_spin.setValue(int(cfg.get("frame_diff_tolerance", defaults.get("frame_diff_tolerance", 2))))
            if hasattr(self, "ocr_log_text_chk"):
                self.ocr_log_text_chk.setChecked(bool(cfg.get("log_ocr_text", defaults.get("log_ocr_text", False))))
            if hasattr(self, "ocr_loop_logs_chk"):
                self.ocr_loop_logs_chk.setChecked(bool(cfg.get("log_loop", defaults.get("log_loop", True))))
            self._load_ocr_device_choices()
            self._select_ocr_device(cfg.get("device_id", defaults.get("device_id", None)))
            self._last_ocr_device_id = self.ocr_device_combo.currentData()

            roi_cfg = cfg.get("roi") or {}
            rx, ry, rw, rh = roi_cfg.get("x", 0.0), roi_cfg.get("y", 0.0), roi_cfg.get("w", 0.0), roi_cfg.get("h", 0.0)
            try:
                fx, fy, fw, fh = float(rx), float(ry), float(rw), float(rh)
            except Exception:
                fx = fy = fw = fh = 0.0
            self.ocr_roi = (fx, fy, fw, fh) if fw > 0 and fh > 0 else None
            self.ocr_shared_areas = self._ocr_shared_areas_from_cfg(cfg)
            self._update_ocr_roi_label()
            self._load_color_filters_table(self._ocr_filters_from_cfg(cfg))
        finally:
            self._loading_ocr_settings = False

        # Reflect current OCR device in the OCR tab
        self._update_ocr_device_label()

        if self._is_manager_running() and self.ocr_enable_chk.isChecked():
            self._start_ocr_worker()

    def _update_ocr_roi_label(self):
        if self.ocr_roi:
            x, y, w, h = self.ocr_roi
            self.ocr_roi_label.setText(f"Chat ROI: x={x:.3f}, y={y:.3f}, w={w:.3f}, h={h:.3f}")
        else:
            self.ocr_roi_label.setText("Chat ROI: not calibrated")

    def _update_ocr_device_label(self):
        """Update the OCR device label with current OCR runtime status."""
        try:
            summary = get_ocr_device_summary()
        except Exception:
            summary = "Unknown (error checking OCR device)"
        self.ocr_device_label.setText(f"OCR Device: {summary}")

    def _load_ocr_device_choices(self) -> None:
        if not hasattr(self, "ocr_device_combo"):
            return
        self.ocr_device_combo.blockSignals(True)
        self.ocr_device_combo.clear()
        self.ocr_device_combo.addItem("Auto (default)", None)
        self.ocr_device_combo.addItem("CPU", "cpu")
        try:
            devices = get_ocr_available_devices()
        except Exception:
            devices = []
        for device_id, name in devices:
            label = f"GPU {device_id}: {name}"
            self.ocr_device_combo.addItem(label, int(device_id))
        self.ocr_device_combo.blockSignals(False)

    def _select_ocr_device(self, device_id) -> None:
        if not hasattr(self, "ocr_device_combo"):
            return
        target = None
        if isinstance(device_id, str):
            if device_id.strip().lower() in ("cpu", "force_cpu"):
                target = "cpu"
            else:
                try:
                    target = int(device_id)
                except Exception:
                    target = None
        elif device_id is not None:
            try:
                target = int(device_id)
            except Exception:
                target = None
        for idx in range(self.ocr_device_combo.count()):
            if self.ocr_device_combo.itemData(idx) == target:
                self.ocr_device_combo.setCurrentIndex(idx)
                return
        self.ocr_device_combo.setCurrentIndex(0)

    def _refresh_ocr_device_label(self, attempts: int = 8, delay_ms: int = 500) -> None:
        """Retry a few times so the label updates after OCR initialization."""
        def _tick(remaining: int) -> None:
            self._update_ocr_device_label()
            try:
                summary = get_ocr_device_summary()
            except Exception:
                summary = "Unknown"
            if remaining > 0 and str(summary).startswith("Unknown"):
                QTimer.singleShot(delay_ms, lambda: _tick(remaining - 1))

        _tick(attempts)

    def _on_ocr_enabled_toggled(self, checked: bool):
        if self._loading_ocr_settings:
            return
        if checked:
            if not self._is_manager_running():
                QMessageBox.information(self, "Manager not running", "OCR will start once the manager is running.")
                return
            self._start_ocr_worker()
        else:
            self._stop_ocr_worker()

        # Persist the new enabled state and settings immediately
        try:
            settings = self.config_manager.load_settings()
        except Exception:
            settings = self.config_manager.default_settings.copy()
        settings["ocr"] = self._get_ocr_settings_from_ui()
        try:
            self.config_manager.save_settings(settings)
        except Exception:
            pass
        try:
            if self.worker_thread and self.worker_thread.isRunning():
                self.worker_thread.refresh_multiscope_settings(settings)
        except Exception:
            pass

    def _sync_ocr_worker_settings(self):
        if self.ocr_worker and self.ocr_worker.isRunning():
            self.ocr_worker.update_settings(self._get_ocr_settings_from_ui(), self._ms_settings_from_ui())

    def _on_ocr_settings_changed(self):
        """Apply OCR tab settings immediately and persist them."""
        if self._loading_ocr_settings:
            return
        ocr_settings = self._get_ocr_settings_from_ui()
        device_id = ocr_settings.get("device_id")
        device_changed = device_id != self._last_ocr_device_id
        # Save settings
        try:
            settings = self.config_manager.load_settings()
        except Exception:
            settings = self.config_manager.default_settings.copy()
        settings["ocr"] = ocr_settings
        try:
            self.config_manager.save_settings(settings)
        except Exception:
            pass
        try:
            if self.worker_thread and self.worker_thread.isRunning():
                self.worker_thread.refresh_multiscope_settings(settings)
        except Exception:
            pass
        # Live-apply to worker if running (restart to apply device changes)
        if device_changed and self.ocr_worker and self.ocr_worker.isRunning():
            self._stop_ocr_worker()
            if self.ocr_enable_chk.isChecked():
                self._start_ocr_worker()
        else:
            self._sync_ocr_worker_settings()
        self._last_ocr_device_id = device_id

    def _ocr_has_usable_filters(self, ocr_cfg: Optional[dict] = None) -> bool:
        cfg = ocr_cfg or self._get_ocr_settings_from_ui()
        roi_cfg = cfg.get("roi") if isinstance(cfg.get("roi"), dict) else {}
        try:
            chat_roi = (
                float(roi_cfg.get("x", 0.0) or 0.0),
                float(roi_cfg.get("y", 0.0) or 0.0),
                float(roi_cfg.get("w", 0.0) or 0.0),
                float(roi_cfg.get("h", 0.0) or 0.0),
            )
        except Exception:
            chat_roi = (0.0, 0.0, 0.0, 0.0)
        usable_chat_roi = chat_roi if chat_roi[2] > 0 and chat_roi[3] > 0 else None
        shared_areas = self._ocr_shared_areas_from_cfg(cfg)

        for spec in self._ocr_filters_from_cfg(cfg):
            if not bool(spec.get("enabled", True)):
                continue
            if self._ocr_filter_shared_area_id(spec):
                roi = self._ocr_shared_area_roi(
                    self._ocr_filter_shared_area_id(spec),
                    shared_areas=shared_areas,
                    chat_roi=usable_chat_roi,
                )
            else:
                roi = self._ocr_effective_filter_roi(spec)
            if roi is not None:
                return True
        return False

    def _reset_ocr_to_defaults(self):
        """Reset only the OCR tab to its default config and live-apply."""
        defaults = self.config_manager.default_settings.get("ocr", {}) or {}
        self._loading_ocr_settings = True
        try:
            self.ocr_enable_chk.setChecked(bool(defaults.get("enabled", False)))
            self.ocr_only_mapped_pids = bool(defaults.get("only_mapped_pids", False))
            self.ocr_workers_spin.setValue(int(defaults.get("workers", 1)))
            self.ocr_max_caps_spin.setValue(int(defaults.get("max_captures_per_second", 20)))
            self.ocr_batch_delay_spin.setValue(float(defaults.get("batch_delay_seconds", 1.0)))
            self.ocr_preprocess_chk.setChecked(bool(defaults.get("use_preprocess", True)))
            self.ocr_frame_diff_tol_spin.setValue(int(defaults.get("frame_diff_tolerance", 2)))
            if hasattr(self, "ocr_log_text_chk"):
                self.ocr_log_text_chk.setChecked(bool(defaults.get("log_ocr_text", False)))
            if hasattr(self, "ocr_loop_logs_chk"):
                self.ocr_loop_logs_chk.setChecked(bool(defaults.get("log_loop", True)))
            self._load_ocr_device_choices()
            self._select_ocr_device(defaults.get("device_id", None))
            self._last_ocr_device_id = self.ocr_device_combo.currentData()
            roi_cfg = defaults.get("roi") or {}
            rx, ry, rw, rh = roi_cfg.get("x", 0.0), roi_cfg.get("y", 0.0), roi_cfg.get("w", 0.0), roi_cfg.get("h", 0.0)
            try:
                fx, fy, fw, fh = float(rx), float(ry), float(rw), float(rh)
            except Exception:
                fx = fy = fw = fh = 0.0
            self.ocr_roi = (fx, fy, fw, fh) if fw > 0 and fh > 0 else None
            self.ocr_shared_areas = self._ocr_shared_areas_from_cfg(defaults)
            self._update_ocr_roi_label()
            self._load_color_filters_table(defaults.get("filters", []))
        finally:
            self._loading_ocr_settings = False

        # Persist + apply to worker
        self._on_ocr_settings_changed()
        # Device availability may have changed between runs (e.g., driver update)
        self._update_ocr_device_label()

    def _start_ocr_worker(self):
        if not self._is_manager_running():
            return
        if self.ocr_worker and self.ocr_worker.isRunning():
            return
        ocr_cfg = self._get_ocr_settings_from_ui()
        if not self._ocr_has_usable_filters(ocr_cfg):
            QMessageBox.warning(self, "Calibrate OCR", "Please calibrate the chat area or at least one enabled filter area before enabling OCR.")
            self.ocr_enable_chk.setChecked(False)
            return

        ms_cfg = self._ms_settings_from_ui()

        self.ocr_worker = OCRWorker(
            ocr_settings=ocr_cfg,
            ms_settings=ms_cfg,
            context_provider=self._resolve_pid_context,
        )
        self.ocr_worker.log_signal.connect(self._handle_ocr_log)
        self.ocr_worker.status_signal.connect(self._handle_ocr_status)
        self.ocr_worker.merchant_signal.connect(self._handle_ocr_merchant)
        self.ocr_worker.verification_cap_signal.connect(self._handle_ocr_verification_cap)
        self.ocr_worker.filter_alert_signal.connect(
            self._handle_ocr_filter_alert,
            Qt.ConnectionType.QueuedConnection,
        )
        self.ocr_worker.filter_match_signal.connect(
            self._handle_ocr_filter_match_for_auto_actions,
            Qt.ConnectionType.QueuedConnection,
        )
        self.ocr_worker.start()
        self._handle_ocr_status("running")

    def _stop_ocr_worker(self):
        self._stop_ocr_worker_with_timeout(timeout_ms=3000)
        self._handle_ocr_status("stopped")

    def _stop_ocr_worker_with_timeout(self, *, timeout_ms: int = 3000) -> None:
        worker = getattr(self, "ocr_worker", None)
        self._stop_ocr_filter_alert()
        if not worker:
            return

        try:
            worker.stop()
        except Exception:
            pass

        try:
            timeout_i = int(timeout_ms)
        except Exception:
            timeout_i = 3000

        # Wait for a clean stop; if it hangs, force-terminate but also try to tear down any pools
        # so we don't leave orphaned processes/threads after closing JARAM.
        stopped = False
        try:
            stopped = bool(worker.wait(max(0, timeout_i)))
        except Exception:
            stopped = False

        if not stopped:
            try:
                self.add_log("[OCR] stop timed out; forcing terminate()")
            except Exception:
                pass
            try:
                worker.terminate()
            except Exception:
                pass
            try:
                worker.wait(1500)
            except Exception:
                pass

        # Best-effort: ensure any internal executors are shut down even if the thread was terminated.
        for name in ("_shutdown_ocr_pool", "_shutdown_send_pool", "_shutdown_send_executor"):
            try:
                fn = getattr(worker, name, None)
                if callable(fn):
                    fn()
            except Exception:
                pass

        try:
            self.ocr_worker = None
        except Exception:
            pass

    def _terminate_multiprocessing_children(self, *, timeout_s: float = 2.0) -> None:
        # Last-resort cleanup for any leftover multiprocessing children (OCR pool, multiscope, etc.).
        try:
            import multiprocessing as mp
            import time as _time
        except Exception:
            return

        try:
            deadline = _time.time() + float(max(0.0, timeout_s))
        except Exception:
            deadline = _time.time() + 2.0

        try:
            children = list(mp.active_children() or [])
        except Exception:
            children = []

        for p in children:
            try:
                if p.is_alive():
                    p.terminate()
            except Exception:
                pass

        for p in children:
            try:
                remaining = max(0.0, deadline - _time.time())
                if remaining <= 0.0:
                    break
                p.join(timeout=remaining)
            except Exception:
                pass

        # Second pass: ensure nothing new appeared (or survived).
        try:
            children2 = list(mp.active_children() or [])
        except Exception:
            children2 = []
        for p in children2:
            try:
                if p.is_alive():
                    p.terminate()
            except Exception:
                pass

    def _is_manager_running(self) -> bool:
        return bool(self.worker_thread and self.worker_thread.isRunning())

    def _ms_settings_from_ui(self) -> dict:
        ms = {}
        settings = {}
        try:
            settings = self.config_manager.load_settings() or {}
            ms = settings.get("multiscope", {}) or {}
        except Exception:
            ms = {}

        if hasattr(self, "ms_merchant_webhook_input"):
            ms["merchant_webhook"] = self.ms_merchant_webhook_input.text().strip()
        if hasattr(self, "ms_enable_jester"):
            ms["enable_jester"] = bool(self.ms_enable_jester.isChecked())
        if hasattr(self, "ms_enable_mari"):
            ms["enable_mari"] = bool(self.ms_enable_mari.isChecked())
        if hasattr(self, "ms_enable_rin"):
            ms["enable_rin"] = bool(self.ms_enable_rin.isChecked())
        if hasattr(self, "ms_merchant_detection_mode"):
            ms["merchant_detection_mode"] = str(self.ms_merchant_detection_mode.currentData() or "asset_id")
        if hasattr(self, "ms_jester_type") and hasattr(self, "ms_jester_id"):
            ms["jester_ping_type"] = self.ms_jester_type.currentText()
            ms["jester_ping_id"] = self.ms_jester_id.text().strip()
        if hasattr(self, "ms_mari_type") and hasattr(self, "ms_mari_id"):
            ms["mari_ping_type"] = self.ms_mari_type.currentText()
            ms["mari_ping_id"] = self.ms_mari_id.text().strip()
        if hasattr(self, "ms_rin_type") and hasattr(self, "ms_rin_id"):
            ms["rin_ping_type"] = self.ms_rin_type.currentText()
            ms["rin_ping_id"] = self.ms_rin_id.text().strip()

        misc = {}
        try:
            misc = settings.get("misc", {}) or {}
        except Exception:
            misc = {}
        if hasattr(self, "misc_skip_unknown_webhook_chk"):
            ms["skip_webhook_unknown_context"] = bool(self.misc_skip_unknown_webhook_chk.isChecked())
        elif isinstance(misc, dict) and "skip_webhook_unknown_context" in misc:
            ms["skip_webhook_unknown_context"] = bool(misc.get("skip_webhook_unknown_context", False))

        def _mk_ping(typ: str, ident: str) -> str:
            ident = (ident or "").strip()
            if not ident:
                return ""
            if typ == "User ID":
                return f"<@{ident}>"
            if typ == "Role ID":
                return f"<@&{ident}>"
            return ident

        ms["jester_ping"] = _mk_ping(ms.get("jester_ping_type", "None"), ms.get("jester_ping_id", ""))
        ms["mari_ping"] = _mk_ping(ms.get("mari_ping_type", "None"), ms.get("mari_ping_id", ""))
        ms["rin_ping"] = _mk_ping(ms.get("rin_ping_type", "None"), ms.get("rin_ping_id", ""))
        return ms

    def calibrate_ocr_roi(self):
        windows = enum_roblox_windows()
        if not windows:
            QMessageBox.warning(self, "No Roblox windows", "No visible Roblox windows were found.")
            return
        ref_win = windows[0]
        img = capture_window_image(ref_win.hwnd)
        if img is None:
            QMessageBox.warning(self, "Capture failed", f"Could not capture window '{ref_win.title}'.")
            return

        pixmap = pil_to_pixmap(img)
        dlg = ROICropDialog(pixmap, self)
        if dlg.exec():
            roi = dlg.selected_roi()
            if roi:
                self.ocr_roi = roi
                self._update_ocr_roi_label()
                # Persist new ROI + live-apply
                self._on_ocr_settings_changed()

    def calibrate_ocr_verification_roi(self):
        QMessageBox.information(self, "OCR Filters", "Verification areas are now configured from each filter's Edit popup.")

    def show_ocr_preview(self):
        """
        Capture the calibrated chat area from a Roblox window and show what the
        OCR engine would see. Any unexpected errors are logged to the OCR log
        panel so users can debug missing dependencies or GPU issues.
        """
        if not self.ocr_roi:
            QMessageBox.warning(self, "Calibrate OCR", "Please calibrate the chat area first (Test preview uses chat ROI only).")
            return
        windows = enum_roblox_windows()
        if not windows:
            QMessageBox.warning(self, "No Roblox windows", "No visible Roblox windows were found.")
            return

        try:
            win = windows[0]
            img = capture_window_image(win.hwnd, self.ocr_roi)
            if img is None:
                QMessageBox.warning(self, "Capture failed", f"Could not capture window '{win.title}'.")
                return

            if self.ocr_preprocess_chk.isChecked():
                img_to_show = preprocess_for_ocr(
                    img,
                    self._current_color_filters(as_dataclass=True, chat_only=True),
                )
            else:
                img_to_show = img

            pm = pil_to_pixmap(img_to_show)

            dlg = QDialog(self)
            dlg.setWindowTitle("OCR Preview")
            v = QVBoxLayout(dlg)
            lbl = QLabel()
            lbl.setPixmap(pm)
            v.addWidget(lbl)
            dlg.resize(pm.width(), pm.height())
            dlg.exec()
        except Exception as e:
            # Mirror OCR worker logging so users see why preview failed
            msg = f"[OCR Preview] Error: {e}"
            try:
                self._handle_ocr_log(msg)
            except Exception:
                # Fallback in case the log widget is not available yet
                try:
                    print(msg)
                except Exception:
                    pass
            QMessageBox.critical(
                self,
                "OCR Preview Error",
                f"An unexpected error occurred while generating the OCR preview:\n{e}",
            )

    def show_ocr_verification_preview(self):
        QMessageBox.information(self, "OCR Filters", "Verification previews are now available from each filter's Edit popup.")

    def test_ocr_frame_compare(self):
        """
        Capture the OCR frame twice (click multiple times) and report how similar
        it is to the previous capture using the current tolerance setting.
        """
        if not self.ocr_roi:
            QMessageBox.warning(self, "Calibrate OCR", "Please calibrate the chat area first (frame compare uses chat ROI only).")
            return

        windows = enum_roblox_windows()
        if not windows:
            QMessageBox.warning(self, "No Roblox windows", "No visible Roblox windows were found.")
            return

        win = windows[0]
        img = capture_window_image(win.hwnd, self.ocr_roi)
        if img is None:
            QMessageBox.warning(self, "Capture failed", f"Could not capture window '{win.title}'.")
            return

        try:
            if self.ocr_preprocess_chk.isChecked():
                img_for_compare = preprocess_for_ocr(img, self._current_color_filters(as_dataclass=True, chat_only=True))
            else:
                img_for_compare = img

            current_hash = compute_frame_hash(img_for_compare)
        except Exception as e:
            msg = f"[Frame Compare] Error: {e}"
            try:
                self._handle_ocr_log(msg)
            except Exception:
                pass
            QMessageBox.critical(self, "Frame Compare Error", f"An unexpected error occurred:\n{e}")
            return

        tol = float(self.ocr_frame_diff_tol_spin.value()) if hasattr(self, "ocr_frame_diff_tol_spin") else 0.0

        if self._ocr_test_last_hash is None:
            self._ocr_test_last_hash = current_hash
            msg = f"[Frame Compare] Baseline captured for PID {win.pid}. Click again to compare."
            try:
                self._handle_ocr_log(msg)
            except Exception:
                pass
            QMessageBox.information(self, "Frame Compare", msg)
            return

        diff_pct = frame_hash_diff_percent(self._ocr_test_last_hash, current_hash)
        skip = diff_pct <= tol
        self._ocr_test_last_hash = current_hash

        verdict = "SKIP OCR" if skip else "RUN OCR"
        msg = f"[Frame Compare] PID {win.pid}: diff={diff_pct:.2f}% (tol={tol:.0f}%) -> {verdict}"
        try:
            self._handle_ocr_log(msg)
        except Exception:
            pass

        QMessageBox.information(
            self,
            "Frame Compare",
            f"Window: {win.title}\nPID: {win.pid}\nDiff: {diff_pct:.2f}%\nTolerance: {tol:.0f}%\nResult: {verdict}",
        )

    def _resolve_pid_context(self, pid: int) -> Dict[str, Any]:
        try:
            pid_i = int(pid)
        except Exception:
            pid_i = 0
        ctx: Dict[str, Any] = {
            "user_id": "",
            "username": "",
            "server_label": "",
            "ps_link": "",
            "owner": "",
            "has_user_log": False,
            "is_cap": False,
        }
        wt = self.worker_thread
        if wt and wt.manager:
            tracker = wt.manager.process_tracker
            uid = tracker.process_owners.get(pid_i) or tracker.process_owners.get(str(pid_i))
            if not uid:
                try:
                    for cand_uid, pids in (tracker.user_processes or {}).items():
                        if not isinstance(pids, (list, tuple, set)):
                            pids = [pids]
                        found = False
                        for p in pids:
                            try:
                                if int(p) == pid_i:
                                    uid = str(cand_uid)
                                    found = True
                                    break
                            except Exception:
                                continue
                        if found:
                            break
                except Exception:
                    pass
            if uid:
                ctx["user_id"] = uid
                info = wt.manager.settings.get(uid, {}) or {}
                ctx["username"] = str(info.get("username", uid) or uid)
                ctx["server_label"] = str(tracker.user_server.get(uid, "") or "")
                ctx["is_cap"] = bool(info.get("cap", False))
                if hasattr(wt, "get_ps_link_for_user"):
                    try:
                        ctx["ps_link"] = wt.get_ps_link_for_user(uid) or ""
                    except Exception:
                        ctx["ps_link"] = ""
                if hasattr(wt, "get_owner_for_user"):
                    try:
                        ctx["owner"] = wt.get_owner_for_user(uid) or ctx["username"]
                    except Exception:
                        ctx["owner"] = ctx["username"]

        pdata = None
        try:
            pdata = self.process_data.get(pid_i, None)
            if pdata is None:
                pdata = self.process_data.get(str(pid_i), None)
        except Exception:
            pdata = None

        if (not ctx.get("username")) and isinstance(pdata, dict):
            data = pdata
            uid = data.get("user_id", "")
            ctx["user_id"] = uid
            users_cfg = self.config_manager.peek_users()
            info = users_cfg.get(uid, {}) if isinstance(users_cfg, dict) else {}
            ctx["username"] = str(info.get("username", uid) or uid)
            ctx["ps_link"] = str(info.get("private_server_link", "") or "")
            ctx["is_cap"] = bool(info.get("cap", False))

        uname_key = str(ctx.get("username") or "").strip().lower()
        if uname_key:
            try:
                ctx["has_user_log"] = bool(find_log_for_username(uname_key, allow_fallback=False))
            except Exception:
                ctx["has_user_log"] = False

        return ctx

    def _handle_ocr_log(self, msg: str):
        if msg == self._last_ocr_log:
            return
        self._last_ocr_log = msg
        try:
            self._ocr_log_queue.append(str(msg))
        except Exception:
            try:
                self.ocr_log_box.append(str(msg))
                if self.ocr_log_autoscroll:
                    self.ocr_log_box.moveCursor(QTextCursor.MoveOperation.End)
            except Exception:
                pass
        try:
            if not self._log_flush_timer:
                self._flush_ocr_log_queue()
        except Exception:
            pass

    def _handle_ocr_merchant(self, uid: str, merchant: str) -> None:
        uid_s = str(uid or "").strip()
        merch_s = str(merchant or "").strip()
        if not uid_s or not merch_s:
            try:
                self.add_log(
                    f"[OCR->FoundStats] Skipped merchant update (uid='{uid_s or '-'}', merchant='{merch_s or '-'}')."
                )
            except Exception:
                pass
            return
        try:
            wt = self.worker_thread
            ms = getattr(wt, "ms", None) if wt else None
            if not ms:
                try:
                    self.add_log("[OCR->FoundStats] MultiScope is unavailable; merchant not recorded.")
                except Exception:
                    pass
                return
            fn = getattr(ms, "record_ocr_merchant", None)
            if not callable(fn):
                try:
                    self.add_log("[OCR->FoundStats] MultiScope proxy lacks record_ocr_merchant; merchant not recorded.")
                except Exception:
                    pass
                return
            fn(uid_s, merch_s)
        except Exception as e:
            try:
                self.add_log(f"[OCR->FoundStats] Failed to record OCR merchant: {e}")
            except Exception:
                pass

    def _handle_ocr_verification_cap(self, uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid:
            return

        wt = self.worker_thread
        info: Dict[str, Any] = {}
        try:
            if wt and wt.manager:
                info = wt.manager.settings.get(uid, {}) or {}
        except Exception:
            info = {}
        if not info:
            try:
                users_cfg = self.config_manager.peek_users()
                if isinstance(users_cfg, dict):
                    info = users_cfg.get(uid, {}) or {}
            except Exception:
                info = {}

        username = str(info.get("username") or uid)
        if bool(info.get("cap", False)):
            return

        self.add_log(f"[CAP] {username} flagged by verification check (Start Puzzle).")
        try:
            self.config_manager.mark_cap_flag(uid, True)
        except Exception:
            pass

        try:
            if wt and wt.manager:
                live_info = wt.manager.settings.get(uid, {}) or {}
                live_info["cap"] = True
                st = wt.user_states.get(uid, {})
                if isinstance(st, dict):
                    st_info = st.get("user_info")
                    if isinstance(st_info, dict):
                        st_info["cap"] = True
                    st["requires_restart"] = False
                    st["status"] = "CAP"
                wt.active_pool.discard(uid)
                wt.spare_pool.discard(uid)
                wt.kill_user_processes(uid)
        except Exception:
            pass

    def _handle_ocr_status(self, status: str):
        status_upper = (status or "").strip().lower()
        if status_upper == "running":
            self.ocr_status_label.setText("Status: Running")
            self.ocr_status_label.setStyleSheet(f"color:{ModernStyle.SECONDARY}; font-weight: bold;")
            self._refresh_ocr_device_label()
        else:
            self._stop_ocr_filter_alert()
            self.ocr_status_label.setText("Status: Stopped")
            self.ocr_status_label.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY}; font-weight: bold;")
    def setup_settings_tab(self):
        settings_widget = QWidget()
        layout = QVBoxLayout(settings_widget)

        scroll_area = QScrollArea(); scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content_widget = QWidget(); content_layout = QVBoxLayout(content_widget)

        # ── Basic ────────────────────────────────────────────────────────────────
        basic_group = QGroupBox("Basic Settings"); basic_layout = QFormLayout(basic_group)
        self.settings_window_limit_input = QSpinBox(); self.settings_window_limit_input.setRange(1, 999)
        self.settings_window_limit_input.setToolTip("Maximum windows per Roblox process")
        basic_layout.addRow("Window Limit:", self.settings_window_limit_input)

        self.spares_mode_chk = QCheckBox("Use Spare Accounts (launch ~half)")
        self.spares_mode_chk.setToolTip("When enabled, only ~half of good accounts are launched initially. Others act as spares for pre-join handoffs.")
        basic_layout.addRow("Spares Mode:", self.spares_mode_chk)
        
        # NEW:
        self.spares_split_cmb = QComboBox()
        self.spares_split_cmb.addItems(["1/2", "3/4", "4/5"])
        self.spares_split_cmb.setToolTip("When spare mode is ON: what fraction of good accounts to launch as active.")
        self.spares_split_cmb.setEnabled(self.spares_mode_chk.isChecked())
        self.spares_mode_chk.toggled.connect(self.spares_split_cmb.setEnabled)
        basic_layout.addRow("Spare Mode Split:", self.spares_split_cmb)
        content_layout.addWidget(basic_group)

        # ── Timing ───────────────────────────────────────────────────────────────
        # Roblox Window Geometry
        win_geom_group = QGroupBox("Set Roblox Window Geometry")
        win_geom_layout = QVBoxLayout(win_geom_group)

        self.rbwin_geom_enforce_chk = QCheckBox("Auto-fix Roblox window size/position on launch")
        self.rbwin_geom_enforce_chk.setToolTip(
            "When enabled, each Roblox window launched by J.JARAM is checked once.\n"
            "If its size/position differs from the recorded geometry, it will be moved/resized to match."
        )
        win_geom_layout.addWidget(self.rbwin_geom_enforce_chk)

        record_row = QHBoxLayout()
        self.rbwin_geom_record_btn = QPushButton("Record an open Roblox window")
        self.rbwin_geom_record_btn.setToolTip(
            "Records the size and position of an open Roblox window.\n"
            "Tip: focus the Roblox window you want to record, then click this button."
        )
        self.rbwin_geom_record_btn.clicked.connect(self._record_roblox_window_geometry)
        record_row.addWidget(self.rbwin_geom_record_btn)
        record_row.addStretch()
        win_geom_layout.addLayout(record_row)

        self.rbwin_geom_status_lbl = QLabel("Recorded: none")
        self.rbwin_geom_status_lbl.setWordWrap(True)
        self.rbwin_geom_status_lbl.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        win_geom_layout.addWidget(self.rbwin_geom_status_lbl)

        content_layout.addWidget(win_geom_group)

        timing_group = QGroupBox("Timing Settings"); timing_layout = QFormLayout(timing_group)
        self.settings_offline_threshold_input = QSpinBox(); self.settings_offline_threshold_input.setRange(10, 120); self.settings_offline_threshold_input.setSuffix(" s")
        timing_layout.addRow("Restart Inactive After:", self.settings_offline_threshold_input)

        self.settings_initial_delay_input = QSpinBox(); self.settings_initial_delay_input.setRange(5, 999999); self.settings_initial_delay_input.setSuffix(" s")
        timing_layout.addRow("Initial Launch Delay:", self.settings_initial_delay_input)

        self.settings_launch_delay_input = QSpinBox(); self.settings_launch_delay_input.setRange(1, 999999); self.settings_launch_delay_input.setSuffix(" s")
        timing_layout.addRow("Launch Delay:", self.settings_launch_delay_input)

        self.handoff_lead_input = QSpinBox(); self.handoff_lead_input.setRange(5, 300); self.handoff_lead_input.setSuffix(" s")
        timing_layout.addRow("Handoff Lead (pre-join before kill):", self.handoff_lead_input)

        self.early_join_window_input = QSpinBox(); self.early_join_window_input.setRange(10, 600); self.early_join_window_input.setSuffix(" s")
        self.early_join_window_input.setToolTip("Assign spares early for sessions expiring within this window to avoid backlog spikes.")
        timing_layout.addRow("Early-Join Window:", self.early_join_window_input)
        content_layout.addWidget(timing_group)

        # ── Shutdown / Timeout ───────────────────────────────────────────────────
        timeout_group = QGroupBox("Shutdown Settings"); timeout_layout = QFormLayout(timeout_group)
        self.settings_strap_threshold_input = QSpinBox(); self.settings_strap_threshold_input.setRange(1, 200)
        self.settings_strap_threshold_input.setToolTip("Max number of strap.exe helpers before trimming")
        timeout_layout.addRow("-Strap Limit:", self.settings_strap_threshold_input)
        
        self.kill_after_enable_chk = QCheckBox("Enable Kill After (auto-close)")
        self.kill_after_enable_chk.setChecked(False)
        timeout_layout.addRow("Enable:", self.kill_after_enable_chk)

        # Optional: gray out the timeout field when disabled
        def _toggle_kill_inputs(checked: bool):
            self.kill_timeout_input.setEnabled(checked)
        self.kill_after_enable_chk.toggled.connect(_toggle_kill_inputs)

        self.kill_timeout_input = QSpinBox(); self.kill_timeout_input.setRange(60, 7200); self.kill_timeout_input.setSuffix(" s")
        self.kill_timeout_input.setToolTip("Time until window auto-closes")
        timeout_layout.addRow("Kill After:", self.kill_timeout_input)

        self.poll_interval_input = QSpinBox(); self.poll_interval_input.setRange(1, 120); self.poll_interval_input.setSuffix(" s")
        self.poll_interval_input.setToolTip("How often the window timer for kill after is checked")
        timeout_layout.addRow("Poll Interval:", self.poll_interval_input)
        content_layout.addWidget(timeout_group)

        # ── Alerts ────────────────────────────────────────────────────────────
        alerts_group = QGroupBox("Alerts"); alerts_layout = QFormLayout(alerts_group)

        self.webhook_input = QLineEdit(); self.webhook_input.setPlaceholderText("Discord webhook alert URL")
        self.webhook_input.setToolTip("Discord webhook URL used for blackout, CAP, BAD, and hourly user-report alerts.")
        alerts_layout.addRow("Webhook URL:", self.webhook_input)

        self.blackout_ping_input = QLineEdit()
        self.blackout_ping_input.setPlaceholderText("This message is sent whenever your active processes drop to 1 or less. Leave this empty if not interested")
        self.blackout_ping_input.setToolTip("Sent when active Roblox processes drop to 1 or 0.")
        alerts_layout.addRow("Blackout Ping:", self.blackout_ping_input)

        self.cap_msg_input = QLineEdit()
        self.cap_msg_input.setPlaceholderText("This message is sent whenever a user is marked for captcha. Leave this empty if not interested. — Supports {username} and {uid}")
        self.cap_msg_input.setToolTip("Sent when a user is marked CAP. Example Message: User {username} has disconnected, User ID = {uid}.")
        alerts_layout.addRow("CAP Ping:", self.cap_msg_input)

        self.bad_msg_input = QLineEdit()
        self.bad_msg_input.setPlaceholderText("This message is sent whenever a user is marked BAD. Leave this empty if not interested. - Supports {username} and {uid}")
        self.bad_msg_input.setToolTip("Sent when a user is marked BAD. Example Message: User {username} has a bad cookie, User ID = {uid}.")
        alerts_layout.addRow("BAD Ping:", self.bad_msg_input)

        self.hourly_users_report_chk = QCheckBox("")
        self.hourly_users_report_chk.setToolTip(
            "Send periodic webhook messages with total users and active users."
        )
        self.hourly_users_report_interval_spin = QSpinBox()
        self.hourly_users_report_interval_spin.setRange(1, 168)
        self.hourly_users_report_interval_spin.setSuffix(" h")
        self.hourly_users_report_interval_spin.setToolTip("Hours between user-report messages.")
        self.hourly_users_report_interval_spin.setValue(1)
        self.hourly_users_report_interval_spin.setMinimumWidth(90)
        self.hourly_users_report_interval_spin.setEnabled(False)

        hourly_row_widget = QWidget()
        hourly_row_layout = QHBoxLayout(hourly_row_widget)
        hourly_row_layout.setContentsMargins(0, 0, 0, 0)
        hourly_row_layout.setSpacing(8)
        hourly_row_layout.addWidget(self.hourly_users_report_chk)
        hourly_row_layout.addWidget(QLabel("Send users report every"))
        hourly_row_layout.addWidget(self.hourly_users_report_interval_spin)
        hourly_row_layout.addStretch()
        alerts_layout.addRow("Users Report:", hourly_row_widget)
        self.hourly_users_report_chk.toggled.connect(self.hourly_users_report_interval_spin.setEnabled)

        content_layout.addWidget(alerts_group)

        # --- Misc ---
        misc_box = QGroupBox("Misc")
        misc_layout = QFormLayout(misc_box)
        self.ui_show_tutorial_menu_chk = QCheckBox("Show Tutorial in Help menu")
        self.ui_show_tutorial_menu_chk.setToolTip("Enable to show Help → Tutorial in the menu bar.")
        misc_layout.addRow(self.ui_show_tutorial_menu_chk)
        self.ui_show_selected_sets_bes_exempt_slot1_chk = QCheckBox("Show Selected sets BES Exempt Slot 1")
        self.ui_show_selected_sets_bes_exempt_slot1_chk.setToolTip(
            "When enabled, Users → Show Selected also assigns that user to BES → Exempt Users → Slot 1.\n"
            "Tip: Click “Save Settings” to persist this toggle."
        )
        misc_layout.addRow(self.ui_show_selected_sets_bes_exempt_slot1_chk)
        self.misc_skip_unknown_webhook_chk = QCheckBox("Skip webhooks if owner/PS unknown")
        self.misc_skip_unknown_webhook_chk.setToolTip(
            "When enabled, webhook messages are suppressed if the owner or private server is unknown."
        )
        misc_layout.addRow(self.misc_skip_unknown_webhook_chk)
        self.misc_disable_log_merchants_when_ocr_active_chk = QCheckBox(
            "Disable log merchant detection while OCR merchant filters are active"
        )
        self.misc_disable_log_merchants_when_ocr_active_chk.setToolTip(
            "When enabled, MultiScope ignores merchant log lines whenever OCR is on and at least one OCR merchant filter is enabled."
        )
        misc_layout.addRow(self.misc_disable_log_merchants_when_ocr_active_chk)
        self.misc_log_confirmed_launch_mode_chk = QCheckBox("Launch only after previous fully launched")
        self.misc_log_confirmed_launch_mode_chk.setToolTip(
            "When enabled, launch delays are treated as minimums and the next account will launch only after "
            "the latest launched account's username is found in logs (Finished Verification)."
        )
        misc_layout.addRow(self.misc_log_confirmed_launch_mode_chk)
        self.misc_disable_manager_bad_marking_chk = QCheckBox("Disable manager auto-BAD marking")
        self.misc_disable_manager_bad_marking_chk.setToolTip(
            "When enabled, launch/auth failures will no longer automatically mark accounts as BAD.\n"
            "Warning: invalid cookies may keep retrying Roblox auth requests and can hit rate limits."
        )
        self.misc_disable_manager_bad_marking_chk.toggled.connect(self._sync_misc_disable_bad_marking_warning)
        self.misc_disable_manager_bad_marking_chk.clicked.connect(self._warn_misc_disable_bad_marking_enabled)
        misc_layout.addRow(self.misc_disable_manager_bad_marking_chk)
        self.misc_disable_manager_bad_marking_warn_lbl = QLabel(
            "Warning: invalid cookies may keep retrying Roblox auth requests and can trigger rate limiting."
        )
        self.misc_disable_manager_bad_marking_warn_lbl.setWordWrap(True)
        self.misc_disable_manager_bad_marking_warn_lbl.setStyleSheet(f"color: {ModernStyle.WARNING};")
        self.misc_disable_manager_bad_marking_warn_lbl.setVisible(False)
        misc_layout.addRow(self.misc_disable_manager_bad_marking_warn_lbl)
        self.misc_msedgewebview2_limiter_enabled_chk = QCheckBox("Enable msedgewebview2 Limiter")
        self.misc_msedgewebview2_limiter_enabled_chk.setToolTip(
            "When enabled, msedgewebview2.exe processes are killed after launch log confirmation. (May interfere with Edge Browser)"
        )
        self.misc_msedgewebview2_limiter_enabled_chk.setChecked(True)
        misc_layout.addRow(self.misc_msedgewebview2_limiter_enabled_chk)
        content_layout.addWidget(misc_box)

        # ── Webhooks (Per-webhook biome filters) ─────────────────────────────────
        webhooks_group = QGroupBox("Biome Webhooks")
        webhooks_v = QVBoxLayout(webhooks_group)

        info_lbl = QLabel("Each row is a webhook. Choose how each biome should notify.")
        info_lbl.setWordWrap(True)
        webhooks_v.addWidget(info_lbl)

        self.webhooks_table = QTableWidget()
        self.webhooks_table.setColumnCount(2 + len(GUI_BIOME_NAMES))  # Name + URL + biomes...
        headers = ["Name", "Webhook URL"] + GUI_BIOME_NAMES
        self.webhooks_table.setHorizontalHeaderLabels(headers)
        self.webhooks_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.webhooks_table.setShowGrid(False)
        self.webhooks_table.setAlternatingRowColors(False)

        header = self.webhooks_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # Make biome columns wide enough so the combobox text is visible when closed
        header.setMinimumSectionSize(150)
        vh = self.webhooks_table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(62)   # match Auto Item table sizing
        vh.setMinimumSectionSize(62)   # prevents squeeze below readable height
        visible_webhook_rows = 4
        table_frame = self.webhooks_table.frameWidth() * 2
        header_height = self.webhooks_table.horizontalHeader().sizeHint().height()
        row_height = vh.defaultSectionSize() * visible_webhook_rows
        scrollbar_height = self.webhooks_table.horizontalScrollBar().sizeHint().height()
        self.webhooks_table.setMinimumHeight(table_frame + header_height + row_height + scrollbar_height)
        # Keep the existing dropdown behavior; just remove gridlines/fit row height above.

        for c in range(2, 2 + len(GUI_BIOME_NAMES)):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.webhooks_table.setColumnWidth(c, 150)
        webhooks_v.addWidget(self.webhooks_table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Webhook")
        rem_btn = QPushButton("Remove Selected")
        route_btn = QPushButton("Assign Users...")
        cols_btn = QPushButton("Biome Columns...")
        test_webhook_btn = QPushButton("Test Webhook")
        test_webhook_btn.clicked.connect(self.test_selected_webhook)
        btn_row.addWidget(add_btn); btn_row.addWidget(rem_btn); btn_row.addWidget(route_btn); btn_row.addWidget(cols_btn); btn_row.addWidget(test_webhook_btn); btn_row.addStretch()
        webhooks_v.addLayout(btn_row)

        MODE_ITEMS = ("None", "Message", "Everyone")  # tri-mode per biome cell

        def _apply_webhook_biome_column_visibility(hidden_biomes=None):
            """Hide biome columns visually; values still load/save normally."""
            hidden_set = set()
            if isinstance(hidden_biomes, (list, tuple, set)):
                hidden_set = {str(b).strip().upper() for b in hidden_biomes if str(b).strip()}

            self._webhooks_hidden_biomes = set(hidden_set)

            for idx, biome in enumerate(GUI_BIOME_NAMES):
                col = 2 + idx
                self.webhooks_table.setColumnHidden(col, str(biome).strip().upper() in hidden_set)

        def _open_webhook_columns_dialog():
            current_hidden = getattr(self, "_webhooks_hidden_biomes", set()) or set()
            if not isinstance(current_hidden, (list, tuple, set)):
                current_hidden = set()
            current_hidden = {str(b).strip().upper() for b in current_hidden if str(b).strip()}

            dlg = QDialog(self)
            dlg.setWindowTitle("Webhook Biome Columns")
            dlg.resize(460, 520)

            v = QVBoxLayout(dlg)
            hint = QLabel("Uncheck biomes to hide their columns in the table.\nHidden columns are still active.")
            hint.setWordWrap(True)
            v.addWidget(hint)

            lw = QListWidget()
            for biome in GUI_BIOME_NAMES:
                key = str(biome).strip().upper()
                item = QListWidgetItem(str(biome))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked if key in current_hidden else Qt.CheckState.Checked)
                item.setData(Qt.ItemDataRole.UserRole, key)
                lw.addItem(item)
            v.addWidget(lw)

            quick_row = QHBoxLayout()
            show_all_btn = QPushButton("Show All")
            hide_all_btn = QPushButton("Hide All")
            quick_row.addWidget(show_all_btn)
            quick_row.addWidget(hide_all_btn)
            quick_row.addStretch()
            v.addLayout(quick_row)

            btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            v.addWidget(btn_box)

            def _set_all(state: Qt.CheckState):
                for i in range(lw.count()):
                    it = lw.item(i)
                    if it is not None:
                        it.setCheckState(state)

            show_all_btn.clicked.connect(lambda: _set_all(Qt.CheckState.Checked))
            hide_all_btn.clicked.connect(lambda: _set_all(Qt.CheckState.Unchecked))

            def _apply_and_close():
                hidden = set()
                for i in range(lw.count()):
                    it = lw.item(i)
                    if it is None:
                        continue
                    key = it.data(Qt.ItemDataRole.UserRole)
                    if it.checkState() != Qt.CheckState.Checked:
                        hidden.add(str(key).strip().upper())
                _apply_webhook_biome_column_visibility(hidden)
                dlg.accept()

            btn_box.accepted.connect(_apply_and_close)
            btn_box.rejected.connect(dlg.reject)
            dlg.exec()

        def _apply_user_filter_to_row(row: int, user_ids=None, user_map: Optional[dict] = None, all_user_ids=None):
            """Persist the selected users and surface a quick tooltip."""
            name_item = self.webhooks_table.item(row, 0)
            url_item  = self.webhooks_table.item(row, 1)
            target_item = name_item or url_item
            if not target_item:
                return

            tip = "Sends events for all users."
            cleaned = None
            if user_ids is None:
                target_item.setData(Qt.ItemDataRole.UserRole, None)
            else:
                cleaned = [str(u).strip() for u in (user_ids or []) if str(u).strip()]
                # If caller passes the full universe, treat it as no filter for clarity.
                if all_user_ids and set(cleaned) == set(all_user_ids):
                    cleaned = None
                    target_item.setData(Qt.ItemDataRole.UserRole, None)
                else:
                    target_item.setData(Qt.ItemDataRole.UserRole, cleaned)

            # Keep the tooltip useful (preview a handful of names)
            if cleaned is not None:
                if user_map is None:
                    try:
                        user_map = self.config_manager.load_users() or {}
                    except Exception:
                        user_map = {}
                if cleaned:
                    names = []
                    for uid in cleaned:
                        info = user_map.get(uid, {}) if isinstance(user_map, dict) else {}
                        names.append(info.get("username") or uid)
                    preview = ", ".join(names[:4])
                    suffix = "..." if len(names) > 4 else ""
                    tip = f"Users: {preview}{suffix}"
                else:
                    tip = "No users selected. This webhook will be skipped until users are assigned."

            if name_item:
                name_item.setToolTip(tip)
            if url_item:
                url_item.setToolTip(tip)

        def _mk_mode_combo(default_text: str = "None") -> QComboBox:
            cmb = QComboBox()
            cmb.addItems(("None", "Message", "Everyone"))
            if default_text in ("None", "Message", "Everyone"):
                cmb.setCurrentText(default_text)
            
            # Make the CLOSED state readable and consistent
            cmb.setMinimumContentsLength(9)
            cmb.setMinimumWidth(120)

            # Match Auto Item control sizing
            cmb.setMinimumHeight(30)
            cmb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            cmb.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

            # Tighten vertical padding a bit; keep arrow space (legacy behavior).
            cmb.setStyleSheet(
                "QComboBox{min-height:10px; padding:2px 8px;}"
                "QComboBox::drop-down{width:22px;}"
            )
            return cmb

        def _place_centered(table: QTableWidget, row: int, col: int, w: QWidget):
            """
            Wrap `w` in a container and center it both horizontally & vertically.
            Also give the holder enough minimum height so the child never clips.
            """
            holder = QWidget()
            v = QVBoxLayout(holder)
            holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            holder.setStyleSheet("background: transparent;")
            v.setContentsMargins(0, 6, 0, 6)   # breathing room to prevent border clipping
            v.setSpacing(0)
            v.addWidget(w, 1, Qt.AlignCenter)  # true H+V centering
            # ensure the row will be at least the child height + a couple px
            holder.setMinimumHeight(max(30, w.sizeHint().height() + 4))
            table.setCellWidget(row, col, holder)
            return holder


        def add_webhook_row(name: str = "", url: str = "", allowed_biomes=None, biome_modes=None, user_ids=None):
            """
            allowed_biomes: Optional[List[str]]  -> kept for backward compatibility
            biome_modes:    Optional[Dict[str, str]] per-biome: "None"|"Message"|"Everyone"
                            If provided, it overrides the mode derived from allowed_biomes.
            """
            row = self.webhooks_table.rowCount()
            self.webhooks_table.insertRow(row)

            name_item = QTableWidgetItem(name or "")
            url_item = QTableWidgetItem(url)
            self.webhooks_table.setItem(row, 0, name_item)
            self.webhooks_table.setItem(row, 1, url_item)
            _apply_user_filter_to_row(row, user_ids)

            allowed_set = {str(b).upper() for b in (allowed_biomes or [])}
            biome_modes = biome_modes or {}

            for idx, biome in enumerate(GUI_BIOME_NAMES):
                bkey = str(biome).upper()
                default_mode = "Message" if bkey in allowed_set else "None"
                mode = biome_modes.get(bkey, default_mode)
                combo = _mk_mode_combo(mode)
                if bkey in ("GLITCHED", "DREAMSPACE", "CYBERSPACE") and _bm_lock_enforced():
                    combo.setCurrentText("Everyone")
                    combo.setEnabled(False)
                _place_centered(self.webhooks_table, row, 2 + idx, combo)

            # Center + size the row based on the first combo’s height
            first_holder = self.webhooks_table.cellWidget(row, 2)  # wrapper at first biome col
            first_combo  = None
            if first_holder:
                first_combo = first_holder.findChild(QComboBox)

            if first_combo:
                need = first_combo.sizeHint().height() + 6  # 2–3 px above/below
            else:
                need = self.webhooks_table.verticalHeader().defaultSectionSize()

            need = max(need, 34)  # don’t go below a comfortable default
            self.webhooks_table.setRowHeight(row, need)


        def remove_selected_rows():
            rows = sorted({i.row() for i in self.webhooks_table.selectedIndexes()}, reverse=True)
            for r in rows:
                self.webhooks_table.removeRow(r)

        def _open_webhook_user_dialog():
            rows = self.webhooks_table.rowCount()
            if rows == 0:
                QMessageBox.information(self, "No webhooks", "Add a webhook before assigning users.")
                return

            try:
                users_cfg = self.config_manager.load_users() or {}
            except Exception:
                users_cfg = {}

            user_choices = [
                {
                    "id": str(uid).strip(),
                    "username": str(info.get("username", uid)).strip(),
                }
                for uid, info in (users_cfg.items() if isinstance(users_cfg, dict) else [])
            ]
            user_choices.sort(key=lambda u: u["username"].lower())
            # Deduplicate by canonicalized id (strip spaces) in case upstream data has duplicates/whitespace
            uniq = []
            seen_ids = set()
            for u in user_choices:
                cid = u["id"].strip()
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                uniq.append({"id": cid, "username": u["username"]})
            user_choices = uniq
            choice_ids = [u["id"] for u in user_choices]
            choice_set = set(choice_ids)
            name_to_id = {u["username"].strip().lower(): u["id"] for u in user_choices if u["username"]}

            entries = []
            for r in range(rows):
                name_item = self.webhooks_table.item(r, 0)
                url_item  = self.webhooks_table.item(r, 1)
                data_item = name_item or url_item
                selected_raw = data_item.data(Qt.ItemDataRole.UserRole) if data_item else None
                # None  -> no explicit filter yet (default: all users)
                # []    -> explicit "no users"
                # [ids] -> explicit subset
                selected_set = set()
                if isinstance(selected_raw, (list, tuple, set)):
                    selected_set = {str(u).strip() for u in selected_raw if str(u).strip()}
                if selected_raw is None and choice_ids:
                    selected_set = set(choice_ids)  # default: all users enabled
                name_txt = (name_item.text().strip() if name_item else "")
                url_txt = (url_item.text().strip() if url_item else "")
                display = name_txt or url_txt or f"Webhook {r + 1}"
                entries.append({"row": r, "name": name_txt, "url": url_txt, "display": display, "selected": selected_set})

            dlg = QDialog(self)
            dlg.setWindowTitle("Webhook User Routing")
            dlg.resize(720, 520)

            h = QHBoxLayout(dlg)
            left_col = QVBoxLayout()
            webhook_list = QListWidget()
            webhook_list.setMinimumWidth(240)
            for entry in entries:
                webhook_list.addItem(QListWidgetItem(entry["display"]))
            left_col.addWidget(webhook_list)

            distribute_btn = QPushButton("Distribute Evenly")
            left_col.addWidget(distribute_btn)
            left_col.addStretch()
            h.addLayout(left_col)

            right = QVBoxLayout()
            h.addLayout(right)

            right.addWidget(QLabel("Choose which users can send events to the selected webhook:"))

            btn_row = QHBoxLayout()
            select_all_btn = QPushButton("Select All")
            deselect_all_btn = QPushButton("Deselect All")
            stagger_btn = QPushButton("Stagger")
            invert_btn = QPushButton("Invert")
            btn_row.addWidget(select_all_btn)
            btn_row.addWidget(deselect_all_btn)
            btn_row.addWidget(stagger_btn)
            btn_row.addWidget(invert_btn)
            btn_row.addStretch()
            right.addLayout(btn_row)

            user_scroll = QScrollArea()
            user_scroll.setWidgetResizable(True)
            user_container = QWidget()
            user_layout = QVBoxLayout(user_container)
            user_layout.setContentsMargins(6, 6, 6, 6)
            user_scroll.setWidget(user_container)
            right.addWidget(user_scroll)

            btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            right.addWidget(btn_box)

            current_checks: list[QCheckBox] = []

            def _clear_user_layout():
                while user_layout.count():
                    item = user_layout.takeAt(0)
                    if item.widget():
                        w = item.widget()
                        w.setParent(None)
                        w.deleteLater()
                current_checks.clear()

            def _build_user_checks(idx: int):
                _clear_user_layout()
                if idx < 0 or idx >= len(entries):
                    user_layout.addWidget(QLabel("Select a webhook to edit."))
                    user_layout.addStretch()
                    return

                entry = entries[idx]
                # Normalize any legacy selections (usernames, mixed casing) to IDs where possible
                def _resolve_to_id(val: str) -> str:
                    val_clean = (val or "").strip()
                    if not val_clean:
                        return ""
                    if val_clean in choice_set:
                        return val_clean
                    m = re.search(r"(\\d+)", val_clean)
                    if m:
                        cand = m.group(1)
                        if cand in choice_set:
                            return cand
                    mapped = name_to_id.get(val_clean.lower())
                    if mapped:
                        return mapped
                    return ""

                normalized = set()
                missing_raw = set()
                for val in entry["selected"]:
                    uid_resolved = _resolve_to_id(str(val))
                    if uid_resolved:
                        normalized.add(uid_resolved)
                    else:
                        missing_raw.add(str(val).strip())
                entry["selected"] = normalized

                if not user_choices and not entry["selected"]:
                    user_layout.addWidget(QLabel("No users found. Add accounts first."))
                    user_layout.addStretch()
                    return

                for u in user_choices:
                    uid = u["id"]
                    label = f"{u['username']} ({uid})" if u["username"] else uid
                    cb = QCheckBox(label)
                    cb.setChecked(uid in entry["selected"])
                    cb.toggled.connect(lambda state, uid=uid, entry=entry:
                                       entry["selected"].add(uid) if state else entry["selected"].discard(uid))
                    user_layout.addWidget(cb)
                    current_checks.append(cb)

                extras = sorted(
                    v for v in missing_raw
                    if not _resolve_to_id(v)  # skip anything that actually matches a known user
                )
                if extras:
                    user_layout.addWidget(QLabel("Missing / legacy users:"))
                    for uid in extras:
                        cb = QCheckBox(f"{uid} (not in users.json)")
                        cb.setChecked(True)
                        cb.toggled.connect(lambda state, uid=uid, entry=entry:
                                           entry["selected"].add(uid) if state else entry["selected"].discard(uid))
                        user_layout.addWidget(cb)
                        current_checks.append(cb)

                user_layout.addStretch()
                user_scroll.verticalScrollBar().setValue(0)

            def _set_checks(val: bool):
                for cb in current_checks:
                    cb.setChecked(val)

            def _stagger_checks():
                for idx, cb in enumerate(current_checks):
                    cb.setChecked(idx % 2 == 0)

            def _invert_checks():
                for cb in current_checks:
                    cb.setChecked(not cb.isChecked())

            def _distribute_evenly():
                if not entries or not choice_ids:
                    return
                extras_map = {e["row"]: {uid for uid in e["selected"] if uid not in choice_set} for e in entries}
                for e in entries:
                    e["selected"] = set(extras_map.get(e["row"], set()))
                for i, uid in enumerate(choice_ids):
                    entries[i % len(entries)]["selected"].add(uid)
                _build_user_checks(webhook_list.currentRow())

            select_all_btn.clicked.connect(lambda: _set_checks(True))
            deselect_all_btn.clicked.connect(lambda: _set_checks(False))
            stagger_btn.clicked.connect(_stagger_checks)
            invert_btn.clicked.connect(_invert_checks)
            distribute_btn.clicked.connect(_distribute_evenly)

            webhook_list.currentRowChanged.connect(_build_user_checks)
            webhook_list.setCurrentRow(0 if entries else -1)

            btn_box.rejected.connect(dlg.reject)

            def _persist_webhook_users_only():
                settings = self.config_manager.load_settings()
                existing = settings.get("webhooks", []) or []
                if not isinstance(existing, list):
                    existing = []

                # Only persist per-webhook user routing; do NOT save name/url/biomes/modes here.
                updates: dict[str, tuple[bool, list[str]]] = {}
                for entry in entries:
                    row_idx = entry["row"]
                    name_item = self.webhooks_table.item(row_idx, 0)
                    url_item = self.webhooks_table.item(row_idx, 1)
                    url = (url_item.text().strip() if url_item else "")
                    if not url:
                        continue

                    data_item = name_item or url_item
                    user_filter = data_item.data(Qt.ItemDataRole.UserRole) if data_item else None
                    selected_users: list[str] = []
                    explicit_users = False
                    if isinstance(user_filter, (list, tuple, set)):
                        explicit_users = True
                        selected_users = [str(u).strip() for u in user_filter if str(u).strip()]
                        selected_users = sorted({u for u in selected_users})
                        # If the explicit selection equals "all users", treat it as no filter.
                        if choice_ids and set(selected_users) == set(choice_ids):
                            explicit_users = False
                            selected_users = []

                    updates[url] = (explicit_users, selected_users)

                if not updates:
                    return

                updated_any = False
                for wh in existing:
                    if not isinstance(wh, dict):
                        continue
                    url = str(wh.get("url", "") or "").strip()
                    if url not in updates:
                        continue
                    explicit_users, selected_users = updates[url]
                    if explicit_users:
                        wh["users"] = selected_users
                        wh["users_explicit"] = True
                    else:
                        wh.pop("users", None)
                        wh.pop("users_explicit", None)
                    updated_any = True

                if not updated_any:
                    return

                settings["webhooks"] = existing
                if self.config_manager.save_settings(settings):
                    # Update baseline routing only so other unsaved changes remain "dirty".
                    baseline = getattr(self, "_settings_baseline", None)
                    if isinstance(baseline, dict):
                        base_hooks = baseline.get("webhooks", None)
                        if isinstance(base_hooks, list):
                            for bh in base_hooks:
                                if not isinstance(bh, dict):
                                    continue
                                burl = str(bh.get("url", "") or "").strip()
                                if burl not in updates:
                                    continue
                                explicit_users, selected_users = updates[burl]
                                if explicit_users:
                                    bh["users"] = selected_users
                                    bh["users_explicit"] = True
                                else:
                                    bh.pop("users", None)
                                    bh.pop("users_explicit", None)

                    try:
                        if self.worker_thread and self.worker_thread.isRunning():
                            self.worker_thread.apply_new_settings(settings)
                    except Exception:
                        pass

            def _apply_selections_and_close():
                for entry in entries:
                    _apply_user_filter_to_row(entry["row"], sorted(entry["selected"]), user_map=users_cfg, all_user_ids=choice_ids)
                _persist_webhook_users_only()
                dlg.accept()

            btn_box.accepted.connect(_apply_selections_and_close)

            _build_user_checks(0 if entries else -1)
            dlg.exec()

        add_btn.clicked.connect(lambda: add_webhook_row("", ""))
        rem_btn.clicked.connect(remove_selected_rows)
        route_btn.clicked.connect(_open_webhook_user_dialog)
        cols_btn.clicked.connect(_open_webhook_columns_dialog)

        # expose helpers for load/save
        self._add_webhook_row = add_webhook_row
        self._clear_webhook_rows = lambda: self.webhooks_table.setRowCount(0)
        self._apply_webhook_biome_column_visibility = _apply_webhook_biome_column_visibility

        content_layout.addWidget(webhooks_group)

        # --- Multiscope — Merchant & Pings (simple, no custom helpers) ---
        ms_box = QGroupBox("Merchant Webhook & Pings")
        ms_form = QFormLayout(ms_box)

        self.ms_merchant_webhook_input = QLineEdit()
        self.ms_merchant_webhook_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.ms_enable_jester = QCheckBox("Enable Jester pings")
        self.ms_enable_mari   = QCheckBox("Enable Mari pings")
        self.ms_enable_rin    = QCheckBox("Enable Rin pings")
        self.ms_merchant_detection_mode = QComboBox()
        self.ms_merchant_detection_mode.addItem("Asset ID (Default)", "asset_id")
        self.ms_merchant_detection_mode.addItem("Legacy Chat Line", "legacy_chat")

        type_opts = ["None", "User ID", "Role ID"]

        self.ms_jester_type = QComboBox(); self.ms_jester_type.addItems(type_opts)
        self.ms_jester_id   = QLineEdit();  self.ms_jester_id.setPlaceholderText("numeric ID or @everyone")
        self.ms_mari_type   = QComboBox();  self.ms_mari_type.addItems(type_opts)
        self.ms_mari_id     = QLineEdit();  self.ms_mari_id.setPlaceholderText("numeric ID or @everyone")
        self.ms_rin_type    = QComboBox();  self.ms_rin_type.addItems(type_opts)
        self.ms_rin_id      = QLineEdit();  self.ms_rin_id.setPlaceholderText("numeric ID or @everyone")

        # tidy two-control rows without custom helpers:
        jester_row = QWidget(); jester_h = QHBoxLayout(jester_row); jester_h.setContentsMargins(0,0,0,0)
        jester_h.addWidget(self.ms_jester_type); jester_h.addWidget(self.ms_jester_id)

        mari_row = QWidget(); mari_h = QHBoxLayout(mari_row); mari_h.setContentsMargins(0,0,0,0)
        mari_h.addWidget(self.ms_mari_type); mari_h.addWidget(self.ms_mari_id)

        rin_row = QWidget(); rin_h = QHBoxLayout(rin_row); rin_h.setContentsMargins(0,0,0,0)
        rin_h.addWidget(self.ms_rin_type); rin_h.addWidget(self.ms_rin_id)

        ms_form.addRow("Merchant Webhook URL", self.ms_merchant_webhook_input)
        ms_form.addRow("Merchant Detection Mode", self.ms_merchant_detection_mode)
        ms_form.addRow(self.ms_enable_jester)
        ms_form.addRow("Jester ping type / ID", jester_row)
        ms_form.addRow(self.ms_enable_mari)
        ms_form.addRow("Mari ping type / ID", mari_row)
        ms_form.addRow(self.ms_enable_rin)
        ms_form.addRow("Rin ping type / ID", rin_row)

        ms_test_row = QHBoxLayout()
        ms_test_btn = QPushButton("Test Webhook")
        ms_test_btn.clicked.connect(self.test_merchant_pings)
        ms_test_row.addWidget(ms_test_btn)
        ms_test_row.addStretch()
        ms_form.addRow(ms_test_row)

        content_layout.addWidget(ms_box)

        # ── Save / Reset ─────────────────────────────────────────────────────────
        buttons_layout = QHBoxLayout()
        save_settings_btn = QPushButton("Save Settings"); save_settings_btn.setProperty("class", "success"); save_settings_btn.clicked.connect(lambda: self.save_settings())
        reset_settings_btn = QPushButton("Reset to Defaults"); reset_settings_btn.clicked.connect(self.reset_settings)
        buttons_layout.addWidget(save_settings_btn); buttons_layout.addWidget(reset_settings_btn); buttons_layout.addStretch()
        content_layout.addLayout(buttons_layout); content_layout.addStretch()

        scroll_area.setWidget(content_widget); layout.addWidget(scroll_area)
        self.settings_tab_index = self.tab_widget.addTab(settings_widget, "Settings")
        self.load_settings_tab()

    def append_log(self, s: str):
        self.add_log(s)

    def setup_RAMEXPORT_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # ── API parameters ─────────────────────────────────────────
        form = QFormLayout()

        self.ram_port_input  = QLineEdit("7963")
        form.addRow("RAM Port:", self.ram_port_input)

        self.ram_group_input = QLineEdit()
        form.addRow("Group (Blank = All):", self.ram_group_input)

        self.ram_pwd_input   = QLineEdit()
        self.ram_pwd_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self.ram_pwd_input)

        layout.addLayout(form)

        # ── merge / replace toggles ───────────────────────────────
        self.merge_chk = QCheckBox("Merge with existing users.json (otherwise replace)")
        self.merge_chk.setChecked(True)
        layout.addWidget(self.merge_chk)

        self.replace_cookie_chk = QCheckBox("Overwrite existing cookies")
        self.replace_ps_chk     = QCheckBox("Overwrite existing private-servers")

        def _merge_toggled(checked: bool):          # checked is True / False
            self.replace_cookie_chk.setEnabled(checked)
            self.replace_ps_chk.setEnabled(checked)

        self.merge_chk.toggled.connect(_merge_toggled)   # use toggled(bool)
        _merge_toggled(self.merge_chk.isChecked())       # set initial state

        layout.addWidget(self.replace_cookie_chk)
        layout.addWidget(self.replace_ps_chk)

        # ── run button ─────────────────────────────────────────────
        run_btn = QPushButton("Fetch && Apply Accounts")
        run_btn.setProperty("class", "success")
        run_btn.clicked.connect(self.execute_ram_import)
        layout.addWidget(run_btn)

        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(tab)
        return scroll
        
    @staticmethod
    def _make_dev_card(name: str,
                    movie_bytes: bytes,
                    fallback: str = "GIF\nError") -> QWidget:
        card   = QWidget()
        layout = QVBoxLayout(card)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(15)

        ring_px = 6                         # ← any thickness you want

        outer = QWidget()
        outer.setFixedSize(120, 120)

        # -------- coloured ring (layer 1) --------
        ring = BorderRing(120, ring_px, ModernStyle.PRIMARY, parent=outer)
        ring.move(0, 0)

        # -------- masked GIF holder (layer 0) ----
        inner_d = 120 - ring_px * 2         # 120 − 6*2 = 108 px
        holder  = RoundMovieLabel(inner_d, 0, "transparent", parent=outer)
        holder.setFixedSize(inner_d, inner_d)
        holder.move(ring_px, ring_px)       # gap = ring thickness
        
        def _show_fallback() -> None:
            holder.setText(fallback)
            holder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder.setStyleSheet(
                f"color:{ModernStyle.TEXT_SECONDARY};"
                f"border:1px dashed {ModernStyle.PRIMARY};"
            )

        if not movie_bytes:
            _show_fallback()
        else:
            try:
                buf = QBuffer()
                buf.setData(QByteArray(movie_bytes))
                buf.open(QIODevice.OpenModeFlag.ReadOnly)

                mv = QMovie()
                mv.setDevice(buf)
                mv.setCacheMode(QMovie.CacheMode.CacheAll)
                mv.setScaledSize(QSize(150, False))      # == holder size

                buf.setParent(mv)
                mv.setParent(holder)
                holder.setMovie(mv)
                mv.start()

            except Exception:
                _show_fallback()

        layout.addWidget(outer)

        # name label
        lbl = QLabel(name)
        f   = QFont(); f.setPointSize(16); f.setBold(True)
        lbl.setFont(f)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color:{ModernStyle.SECONDARY}")
        layout.addWidget(lbl)

        return card
    

    def setup_credits_tab(self):
        credits_widget = QWidget()
        layout = QVBoxLayout(credits_widget)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(20)

        title_label = QLabel("JARAM Credits")
        title_font = QFont()
        title_font.setPointSize(24)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet(f"color: {ModernStyle.PRIMARY}; margin: 20px 0;")
        content_layout.addWidget(title_label)

        developer_group = QGroupBox("Developer")

        # ── Two dev cards, side-by-side ──────────────────────────────
        developer_layout = QHBoxLayout(developer_group)
        developer_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        developer_layout.setSpacing(40)

        # — Jirach1 —
        bytes_j = b""
        try:
            bytes_j = Path(__file__).with_name("jirachi.gif").read_bytes()
        except Exception:
            try:
                bytes_j = urlopen("https://media.tenor.com/7c8Mqt2ciZgAAAAi/jirachi.gif").read()
            except Exception:
                bytes_j = b""

        developer_layout.addWidget(self._make_dev_card("Jirach1", bytes_j))

        # — cresqnt —
        bytes_c = b""
        try:
            bytes_c = Path(__file__).with_name("cresqnt.gif").read_bytes()
        except Exception:
            try:
                bytes_c = urlopen("https://media1.tenor.com/m/CNBGgG2DU10AAAAd/nyan-cat-poptart.gif").read()
            except Exception:
                bytes_c = b""

        developer_layout.addWidget(self._make_dev_card("cresqnt",  bytes_c))

        content_layout.addWidget(developer_group)

        support_group = QGroupBox("Support")
        support_layout = QVBoxLayout(support_group)

        support_label = QLabel("Discord Support Server:")
        support_label.setStyleSheet(f"color: {ModernStyle.TEXT_PRIMARY}; font-weight: bold; margin-bottom: 5px;")
        support_layout.addWidget(support_label)

        discord_btn = QPushButton("https://discord.gg/6cuCu6ymkX")
        discord_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: 
                color: white;
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: 
            }}
        """)
        discord_btn.clicked.connect(lambda: self.open_url("https://discord.gg/6cuCu6ymkX"))
        support_layout.addWidget(discord_btn)

        content_layout.addWidget(support_group) 
#---------------------------------------------------------------------------------------------------
        support_group2 = QGroupBox("Additional") #lazy copy and paste...
        support_layout2 = QVBoxLayout(support_group2)

        support_label2 = QLabel("The Best Glitch Hunt Server:")
        support_label2.setStyleSheet(f"color: {ModernStyle.TEXT_PRIMARY}; font-weight: bold; margin-bottom: 5px;")
        support_layout2.addWidget(support_label2)

        discord_btn2 = QPushButton("https://discord.gg/TheGlitchCore")
        discord_btn2.setStyleSheet(f"""
            QPushButton {{
                background-color: 
                color: white;
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: 
            }}
        """)
        discord_btn2.clicked.connect(lambda: self.open_url("https://discord.gg/TheGlitchCore"))
        support_layout2.addWidget(discord_btn2)

        bes_label = QLabel("BES (Battle Encoder Shirasé):")
        bes_label.setStyleSheet(f"color: {ModernStyle.TEXT_PRIMARY}; font-weight: bold; margin-top: 10px; margin-bottom: 5px;")
        support_layout2.addWidget(bes_label)

        bes_btn = QPushButton("https://mion.yosei.fi/BES/")
        bes_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: 
                color: white;
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: 
            }}
        """)
        bes_btn.clicked.connect(lambda: self.open_url("https://mion.yosei.fi/BES/"))
        support_layout2.addWidget(bes_btn)

        placeholder_label = QLabel("RAM-Limiter (Trimmer Inspiration):")
        placeholder_label.setStyleSheet(f"color: {ModernStyle.TEXT_PRIMARY}; font-weight: bold; margin-top: 10px; margin-bottom: 5px;")
        support_layout2.addWidget(placeholder_label)

        placeholder_btn = QPushButton("https://github.com/0vm/RAM-Limiter")
        placeholder_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: 
                color: white;
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: 
            }}
        """)
        placeholder_btn.clicked.connect(lambda: self.open_url("https://github.com/0vm/RAM-Limiter"))
        support_layout2.addWidget(placeholder_btn)

        multiscope_fix_label = QLabel("MultiScope Merchant Fix")
        multiscope_fix_label.setStyleSheet(f"color: {ModernStyle.TEXT_PRIMARY}; font-weight: bold; margin-top: 10px; margin-bottom: 5px;")
        support_layout2.addWidget(multiscope_fix_label)

        multiscope_fix_btn = QPushButton("https://github.com/ManasAarohi1/MultiScope-Fix")
        multiscope_fix_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: 
                color: white;
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: 
            }}
        """)
        multiscope_fix_btn.clicked.connect(lambda: self.open_url("https://github.com/ManasAarohi1/MultiScope-Fix"))
        support_layout2.addWidget(multiscope_fix_btn)

        content_layout.addWidget(support_group2)
        
        license_group = QGroupBox("License | Legal")
        license_layout = QVBoxLayout(license_group)

        copyright_label = QLabel("© 2025 cresqnt")
        copyright_label.setStyleSheet(f"color: {ModernStyle.TEXT_PRIMARY}; font-weight: bold; margin-bottom: 10px;")
        license_layout.addWidget(copyright_label)

        license_label = QLabel("Licensed under AGPL-3.0")
        license_label.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY}; margin-bottom: 10px;")
        license_layout.addWidget(license_label)

        content_layout.addWidget(license_group)

        content_layout.addStretch()

        scroll_area.setWidget(content_widget)
        layout.addWidget(scroll_area)

        self.tab_widget.addTab(credits_widget, "Credits")
        
    def execute_ram_import(self):
        base_url = f"http://127.0.0.1:{self.ram_port_input.text().strip() or '7963'}"
        params   = {
            "Password"      : self.ram_pwd_input.text().strip(),
            "IncludeCookies": "true"
        }
        group_val = self.ram_group_input.text().strip()
        if group_val:
            params["Group"] = group_val

        try:
            r = requests.get(f"{base_url}/GetAccountsJson", params=params, timeout=15)
            if r.status_code == 200:
                accounts_raw = r.json()
            elif r.status_code == 400:
                raise RuntimeError("400 Bad Request – “Allow external connections” is OFF in Roblox Account Manager.")
            elif r.status_code == 401:
                raise RuntimeError("401 Unauthorized – Wrong Password")
            elif r.status_code == 404:
                raise RuntimeError("404 Not Found – RAM endpoint missing on this port.")#
            elif r.status_code == 500:
                raise RuntimeError("500 Server Error – RAM threw an internal error.")
            else:
                raise RuntimeError(f"{r.status_code} {r.reason} – RAM API request failed.")

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as net_err:
            QMessageBox.critical(
                self,
                "Port / Connection Error",
                f"Could not reach Roblox Account Manager at port {self.ram_port_input.text()}\n"
                "• Is Roblox Account Manager open?\n"
                "• Is the port correct?\n"
                "*NOTE: Roblox Account Manager must be restarted whenever you change the port.\n\n"
            )
            return

        except Exception as e:
            QMessageBox.critical(self, "Import Error", str(e))
            return


        new_users = transform(accounts_raw)        # -> JARAM user-dict
        if not new_users:
            QMessageBox.warning(self, "No Accounts", "RAM returned 0 usable accounts.")
            return

        # --- backup BEFORE touching users.json --------------------
        self.config_manager._create_users_backup()

        if not self.merge_chk.isChecked():
            merged = new_users                       # full replace
        else:
            merged = self.config_manager.load_users()
            for uid, info in new_users.items():
                if uid not in merged:
                    merged[uid] = info
                else:                                # existing user
                    if self.replace_cookie_chk.isChecked():
                        merged[uid]["cookie"] = info.get("cookie", "")
                        merged[uid]["bad"] = False
                        merged[uid]["cap"] = False
                    if self.replace_ps_chk.isChecked():
                        merged[uid]["private_server_link"] = info.get("private_server_link", "")
                        merged[uid]["place"]               = info.get("place", "")

        if self.config_manager.save_users(merged):
            QMessageBox.information(self, "Success",
                f"Imported {len(new_users)} accounts.\n"
                f"Total users.json entries: {len(merged)}")
            self.add_log("RAM import complete — users.json updated.")
        else:
            err = self.config_manager.get_cookie_error()
            msg = "Failed to write users.json!"
            if err:
                msg = msg + "\n\n" + err
            QMessageBox.critical(self, "Save Error", msg)


    def open_url(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to open URL: {e}")

    def confirm_and_open_url(self, url, title="Open Link", prompt=None):
        if not url:
            QMessageBox.information(self, "No Link", "No link is configured.")
            return

        if prompt is None:
            prompt = f"Open this link in your browser?\n\n{url}"

        reply = QMessageBox.question(
            self,
            title,
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.open_url(url)

    def open_help_link(self):
        placeholder_url = "https://youtube.com"
        self.confirm_and_open_url(
            placeholder_url,
            title="Tutorial",
            prompt=f"This will open your browser to:\n\n{placeholder_url}\n\nContinue?",
        )

    def setup_timers(self):

        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.update_ui)
        self.ui_timer.start(1000)  

        self.uptime_timer = QTimer()
        self.uptime_timer.timeout.connect(self.update_uptime)
        self.uptime_timer.start(1000)

    def start_manager(self):
        if self._manager_paused and self._paused_worker_state and not (self.worker_thread and self.worker_thread.isRunning()):
            self.resume_manager()
            return
        if self.worker_thread and self.worker_thread.isRunning():
            return
        if self._settings_prompt_ready and self.settings_tab_index is not None:
            if self.tab_widget.currentIndex() == self.settings_tab_index:
                changes = self._get_settings_changes()
                if changes:
                    result = self._prompt_settings_save(
                        title="Unsaved Settings",
                        message="You have unsaved settings changes. Save before starting the manager?",
                        allow_cancel=True,
                        changes=changes,
                        no_text="Revert",
                    )
                    if result == QMessageBox.StandardButton.Yes:
                        if not self.save_settings(confirm=False):
                            return
                    elif result == QMessageBox.StandardButton.Cancel:
                        return
                    elif result == QMessageBox.StandardButton.No:
                        self.load_settings_tab()

        try:
            config = self.config_manager.get_users_for_manager()
            if not config:
                QMessageBox.warning(self, "No Users",
                                    "No users found in configuration. Please add users first using File → Manage Users.")
                return
        except Exception as e:
            QMessageBox.critical(self, "Config Error", f"Error reading user configuration: {e}")
            return

        self.worker_thread = WorkerThread(self.config_manager)
        self.worker_thread.log_signal.connect(self.add_log)
        self.worker_thread.status_signal.connect(self.update_user_status)
        self.worker_thread.process_signal.connect(self.update_process_data)
        # NEW: Multiscope snapshot updates the tab
        self.worker_thread.multiscope_signal.connect(self.update_multiscope)

        self.worker_thread.start()

        if self.ocr_enable_chk.isChecked():
            self._start_ocr_worker()

        # Auto-start Anti-AFK if enabled in the UI
        if getattr(self, "antiafk", None) and bool(self.antiafk_enable_chk.isChecked()):
            self._run_antiafk_async("toggle_antiafk", True)

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Pause Manager")
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Status: Running")
        self.status_label.setStyleSheet(f"color: {ModernStyle.SECONDARY}; font-weight: bold;")
        self.start_time = time.time()
        self._paused_at = None
        self._manager_paused = False


    def stop_manager(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.stop()
            # wait a few seconds for a clean shutdown; force-stop if hung
            if not self.worker_thread.wait(5000):
                self.add_log("[UI] Worker stop timed out; forcing terminate()")
                try:
                    self.worker_thread.terminate()
                except Exception:
                    pass
                self.worker_thread.wait(1000)
        self._stop_ocr_worker()

        # Always stop Anti-AFK when the manager stops
        if getattr(self, "antiafk", None):
            self._run_antiafk_async("toggle_antiafk", False)

        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Pause Manager")
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Status: Stopped")
        self.status_label.setStyleSheet(f"color: {ModernStyle.ERROR}; font-weight: bold;")
        self.start_time = None
        self._paused_at = None
        self._paused_worker_state = None
        self._manager_paused = False

    def update_uptime(self):
        if self.start_time:
            now = self._paused_at if self._paused_at else time.time()
            uptime = now - self.start_time
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            seconds = int(uptime % 60)
            self.uptime_label.setText(f"Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")
        else:
            self.uptime_label.setText("Uptime: 00:00:00")

    def toggle_pause_manager(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.pause_manager()
            return
        if self._manager_paused and self._paused_worker_state:
            self.resume_manager()

    def pause_manager(self):
        if not (self.worker_thread and self.worker_thread.isRunning()):
            return

        # Stop worker thread (like Stop Manager), but capture state so we can resume.
        wt = self.worker_thread
        try:
            wt.stop(shutdown_ms=False)
            if not wt.wait(5000):
                self.add_log("[UI] Worker pause timed out; forcing terminate()")
                try:
                    self._paused_worker_state = wt.export_state()
                except Exception:
                    self._paused_worker_state = None
                try:
                    wt.terminate()
                except Exception:
                    pass
                wt.wait(1000)
            else:
                try:
                    self._paused_worker_state = wt.export_state()
                except Exception:
                    self._paused_worker_state = None
        except Exception:
            self._paused_worker_state = None

        # Fallback: keep the last rendered MultiScope rows if the worker snapshot couldn't grab them.
        try:
            if isinstance(self._paused_worker_state, dict):
                if (not self._paused_worker_state.get("multiscope_rows")) and self._last_multiscope_rows:
                    self._paused_worker_state["multiscope_rows"] = list(self._last_multiscope_rows)
        except Exception:
            pass

        # Ensure MultiScope is fully stopped while paused, but only after we snapshot it.
        try:
            ms = getattr(wt, "ms", None)
            if ms and hasattr(ms, "shutdown"):
                ms.shutdown()
        except Exception:
            pass

        self._stop_ocr_worker()

        # Always stop Anti-AFK when pausing
        if getattr(self, "antiafk", None):
            self._run_antiafk_async("toggle_antiafk", False)

        self._manager_paused = True
        self._paused_at = time.time()

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Resume Manager")
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Status: Paused")
        self.status_label.setStyleSheet(f"color: {ModernStyle.WARNING}; font-weight: bold;")

    def resume_manager(self):
        if self.worker_thread and self.worker_thread.isRunning():
            return
        if not (self._manager_paused and self._paused_worker_state):
            return

        resume_state = self._paused_worker_state

        # Keep the last known MultiScope values visible immediately on resume.
        try:
            cached_rows = resume_state.get("multiscope_rows") if isinstance(resume_state, dict) else None
            if isinstance(cached_rows, list) and cached_rows:
                self._ms_resume_grace_until = time.time() + 12.0
                self.update_multiscope(cached_rows)
        except Exception:
            pass

        self.worker_thread = WorkerThread(self.config_manager, resume_state=resume_state)
        self.worker_thread.log_signal.connect(self.add_log)
        self.worker_thread.status_signal.connect(self.update_user_status)
        self.worker_thread.process_signal.connect(self.update_process_data)
        self.worker_thread.multiscope_signal.connect(self.update_multiscope)

        self.worker_thread.start()

        if self.ocr_enable_chk.isChecked():
            self._start_ocr_worker()

        if getattr(self, "antiafk", None) and bool(self.antiafk_enable_chk.isChecked()):
            self._run_antiafk_async("toggle_antiafk", True)

        # Keep uptime continuous across pauses
        if self.start_time and self._paused_at:
            try:
                self.start_time += (time.time() - self._paused_at)
            except Exception:
                self.start_time = time.time()
        elif not self.start_time:
            self.start_time = time.time()
        self._paused_at = None

        self._manager_paused = False
        self._paused_worker_state = None

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Pause Manager")
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Status: Running")
        self.status_label.setStyleSheet(f"color: {ModernStyle.SECONDARY}; font-weight: bold;")

    def update_ui(self):
        active_users = sum(1 for data in self.user_data.values() if data.get('status') == 'Active')
        total_processes = sum(len(data.get('pids', [])) for data in self.user_data.values())
        pending_restarts = sum(1 for data in self.user_data.values() if data.get('needs_restart', False))
        users_cfg = self.config_manager.peek_users()
        try:
            good_count = sum(1 for i in users_cfg.values() if not (i.get("bad") or i.get("cap")) and not i.get("disabled"))
        except Exception:
            good_count = 0

        self.total_users_label.setText(str(good_count))
        self.active_users_label.setText(str(active_users))
        self.total_processes_label.setText(str(total_processes))
        self.pending_restarts_label.setText(str(pending_restarts))
        self._maybe_send_hourly_users_report(good_count, active_users)
        try:
            if self.users_tab_index is not None and self.tab_widget.currentIndex() == self.users_tab_index:
                wt = getattr(self, "worker_thread", None)
                if wt is not None and wt.isRunning():
                    self._refresh_users_antiafk_age_column()
        except Exception:
            pass

    def _maybe_send_hourly_users_report(self, total_users: int, active_users: int) -> None:
        try:
            settings = self.config_manager.peek_settings() or {}
        except Exception:
            return

        alerts = settings.get("alerts", {}) or {}
        if not isinstance(alerts, dict):
            return

        enabled = bool(alerts.get("hourly_users_report_enabled", False))
        try:
            interval_h = int(alerts.get("hourly_users_report_interval_hours", 1) or 1)
        except Exception:
            interval_h = 1
        interval_h = max(1, min(168, interval_h))
        now_ts = float(time.time())

        if not enabled:
            self._hourly_users_report_last_sent_at = 0.0
            self._hourly_users_report_interval_hours = interval_h
            return

        wt = getattr(self, "worker_thread", None)
        if wt is None or not wt.isRunning():
            self._hourly_users_report_last_sent_at = 0.0
            self._hourly_users_report_interval_hours = interval_h
            return

        prev_interval_h = int(getattr(self, "_hourly_users_report_interval_hours", interval_h) or interval_h)
        if prev_interval_h != interval_h:
            self._hourly_users_report_interval_hours = interval_h
            self._hourly_users_report_last_sent_at = now_ts
            return

        last_sent = float(getattr(self, "_hourly_users_report_last_sent_at", 0.0) or 0.0)
        if last_sent <= 0:
            self._hourly_users_report_last_sent_at = now_ts
            self._hourly_users_report_interval_hours = interval_h
            return

        interval_s = float(interval_h * 3600)
        if (now_ts - last_sent) < interval_s:
            return

        self._hourly_users_report_last_sent_at = now_ts
        self._hourly_users_report_interval_hours = interval_h
        try:
            self.config_manager.send_hourly_users_report_webhook(total_users, active_users)
        except Exception:
            pass

    def update_user_status(self, status_data):
        self.user_data = status_data
        try:
            self._sync_antiafk_touch_state_from_user_data()
        except Exception:
            pass
        self._schedule_users_table_refresh()

    def update_process_data(self, process_data):
        self.process_data = process_data
        try:
            if self.users_tab_index is not None and self.tab_widget.currentIndex() == self.users_tab_index:
                self._refresh_users_created_column()
        except Exception:
            pass

    @staticmethod
    def _is_disconnected_server_label(label: str) -> bool:
        u = str(label or "").strip().upper()
        return u.startswith("DISCONNECTED") or u.startswith("OFFLINE")

    @staticmethod
    def _format_antiafk_age(age_s: int) -> str:
        try:
            s = max(0, int(age_s))
        except Exception:
            s = 0
        if s < 60:
            return f"{s}s"
        if s < 3600:
            m, r = divmod(s, 60)
            return f"{m}m {r}s"
        h, r = divmod(s, 3600)
        m = r // 60
        return f"{h}h {m}m"

    def _sync_antiafk_touch_state_from_user_data(self) -> None:
        now_ts = float(time.time())

        new_pid_to_uid: Dict[int, str] = {}
        pids_by_uid: Dict[str, List[int]] = {}
        disconnected_uids: Set[str] = set()
        disconnected_pids: Set[int] = set()

        for uid, runtime in (self.user_data or {}).items():
            uid_s = str(uid)
            runtime = runtime or {}
            server = str(runtime.get("server", "") or "")
            is_disconnected = self._is_disconnected_server_label(server)
            if is_disconnected:
                disconnected_uids.add(uid_s)

            pids = runtime.get("pids", []) or []
            if not isinstance(pids, (list, tuple, set)):
                pids = [pids]

            pid_list: List[int] = []
            for p in pids:
                try:
                    pid_i = int(p)
                except Exception:
                    continue
                if pid_i <= 0:
                    continue
                new_pid_to_uid[pid_i] = uid_s
                pid_list.append(pid_i)

            if pid_list:
                pids_by_uid[uid_s] = pid_list
                if is_disconnected:
                    disconnected_pids.update(pid_list)

        pids_to_touch: List[int] = []
        with self._antiafk_touch_lock:
            self._antiafk_pid_to_uid = new_pid_to_uid

            for uid_s in disconnected_uids:
                self._antiafk_last_touch_by_uid.pop(uid_s, None)

            for uid_s, pid_list in pids_by_uid.items():
                if uid_s in disconnected_uids:
                    continue
                if uid_s not in self._antiafk_last_touch_by_uid:
                    self._antiafk_last_touch_by_uid[uid_s] = now_ts
                    pids_to_touch.extend(pid_list)

            old_disconnected = set(self._antiafk_disconnected_pids)
            self._antiafk_disconnected_pids = set(disconnected_pids)

        antiafk = getattr(self, "antiafk", None)
        if antiafk is not None:
            try:
                newly_disconnected = list(disconnected_pids - old_disconnected)
                newly_connected = list(old_disconnected - disconnected_pids)
                if newly_disconnected:
                    antiafk.set_pids_disconnected(newly_disconnected, True)
                if newly_connected:
                    antiafk.set_pids_disconnected(newly_connected, False)
            except Exception:
                pass
            if pids_to_touch:
                try:
                    antiafk.touch_pids(pids_to_touch)
                except Exception:
                    pass

    def _users_table_refresh_delay_ms(self) -> int:
        try:
            n = len(self.user_data or {})
        except Exception:
            n = 0
        if n <= 30:
            return 75
        if n <= 80:
            return 125
        if n <= 150:
            return 200
        return 300

    def _schedule_users_table_refresh(self, *, force_full: bool = False) -> None:
        self._users_table_dirty = True
        if force_full:
            self._users_table_force_full = True
        if self._users_table_refresh_pending:
            return
        try:
            if self.users_tab_index is not None and self.tab_widget.currentIndex() != self.users_tab_index:
                return
        except Exception:
            pass
        self._users_table_refresh_pending = True
        QTimer.singleShot(self._users_table_refresh_delay_ms(), self._flush_users_table_refresh)

    def _flush_users_table_refresh(self) -> None:
        self._users_table_refresh_pending = False
        if not self._users_table_dirty:
            return
        try:
            if self.users_tab_index is not None and self.tab_widget.currentIndex() != self.users_tab_index:
                return
        except Exception:
            return

        force_full = bool(getattr(self, "_users_table_force_full", False))
        self._users_table_force_full = False
        try:
            self.refresh_users(force_full=force_full)
            self._users_table_dirty = False
        except Exception:
            pass

    def _set_users_table_item(self, row: int, col: int, text: str, *, fg: Optional[QColor] = None) -> None:
        table = getattr(self, "users_table", None)
        if table is None:
            return
        try:
            item = table.item(row, col)
            if item is None:
                item = QTableWidgetItem(str(text))
                table.setItem(row, col, item)
            else:
                s = str(text)
                if item.text() != s:
                    item.setText(s)
            if fg is not None:
                item.setForeground(fg)
        except Exception:
            return

    def refresh_users(self, *_args, force_full: bool = False):
        table = getattr(self, "users_table", None)
        if table is None:
            return

        users_cfg = self.config_manager.peek_users() or {}
        if not isinstance(users_cfg, dict):
            users_cfg = {}

        try:
            def _uid_sort_key(value: object) -> tuple:
                s = str(value)
                try:
                    return (0, int(s), s)
                except Exception:
                    return (1, 0, s)

            ordered = sorted(
                (self.user_data or {}).items(),
                key=lambda kv: (
                    bool(
                        (users_cfg.get(str(kv[0]), {}) or {}).get("bad", False)
                        or (users_cfg.get(str(kv[0]), {}) or {}).get("cap", False)
                    ),
                    bool((users_cfg.get(str(kv[0]), {}) or {}).get("disabled", False)),
                    _uid_sort_key(kv[0]),
                ),
            )
        except Exception:
            ordered = list((self.user_data or {}).items())

        ordered_uids = [str(uid) for uid, _runtime in ordered]
        try:
            row_count_changed = int(table.rowCount()) != len(ordered_uids)
        except Exception:
            row_count_changed = True

        order_changed = ordered_uids != (self._users_table_order or [])
        rebuild = bool(force_full) or bool(order_changed) or bool(row_count_changed)
        rebuild_widgets = bool(order_changed) or bool(row_count_changed)

        try:
            table.setUpdatesEnabled(False)
        except Exception:
            pass

        try:
            if rebuild:
                if row_count_changed:
                    try:
                        table.setRowCount(len(ordered_uids))
                    except Exception:
                        pass
                if order_changed or row_count_changed:
                    self._users_table_order = list(ordered_uids)
                    self._users_table_row_by_uid = {uid: i for i, uid in enumerate(ordered_uids)}

            now_ts = float(time.time())
            for row, (user_id, runtime) in enumerate(ordered):
                uid = str(user_id)
                runtime = runtime or {}

                u_conf = users_cfg.get(uid, {}) or {}
                if not isinstance(u_conf, dict):
                    u_conf = {}
                bad_flag = bool(u_conf.get("bad", False))
                cap_flag = bool(u_conf.get("cap", False))
                disabled_flag = bool(u_conf.get("disabled", False))

                server = str(runtime.get("server", "") or "")

                if rebuild:
                    username = u_conf.get("username", f"User_{uid}")
                    ps_link = str(u_conf.get("private_server_link", "") or "")
                    place = str(u_conf.get("place", "") or "")
                    trimmed_link = ps_link[:25] + "..." if len(ps_link) > 25 else ps_link
                    self._set_users_table_item(row, 0, uid)
                    self._set_users_table_item(row, 1, str(username))
                    self._set_users_table_item(row, 2, trimmed_link)
                    self._set_users_table_item(row, 3, place)
                self._set_users_table_item(row, 4, server)

                if cap_flag:
                    status_text, colour = "CAP", QColor(ModernStyle.ERROR)
                elif bad_flag:
                    status_text, colour = "Bad", QColor(ModernStyle.ERROR)
                elif disabled_flag:
                    status_text, colour = "Disabled", QColor(ModernStyle.TEXT_SECONDARY)
                else:
                    raw = str(runtime.get("status", "Unknown") or "Unknown")
                    if "Active" in raw:
                        colour = QColor(ModernStyle.SECONDARY)
                    elif "Inactive" in raw:
                        colour = QColor(ModernStyle.WARNING)
                    elif "Restarting" in raw:
                        colour = QColor(ModernStyle.PRIMARY)
                    else:
                        colour = QColor(ModernStyle.ERROR)
                    status_text = raw
                self._set_users_table_item(row, 5, status_text, fg=colour)

                pids = runtime.get('pids', []) or []
                if not isinstance(pids, (list, tuple)):
                    pids = [pids]
                self._set_users_table_item(row, 6, ', '.join(map(str, pids)) or 'None')

                ttl_list = runtime.get('ttl', []) or []
                if not isinstance(ttl_list, (list, tuple)):
                    ttl_list = [ttl_list]
                self._set_users_table_item(row, 7, ', '.join(f"{t}s" for t in ttl_list) or 'N/A')

                if rebuild:
                    created_vals: List[str] = []
                    for pid in (pids or []):
                        try:
                            pid_i = int(pid)
                        except Exception:
                            continue
                        pdata = (self.process_data or {}).get(pid_i) or (self.process_data or {}).get(str(pid_i)) or {}
                        c = str(pdata.get("created", "") or "").strip()
                        if c:
                            created_vals.append(c)
                    created_str = ", ".join(created_vals) if created_vals else "N/A"
                    self._set_users_table_item(row, 8, created_str)

                last_active_str = "Never"
                try:
                    last_active_ts = float(runtime.get('last_active', 0) or 0)
                    if last_active_ts > 0:
                        last_active_str = datetime.fromtimestamp(last_active_ts).strftime("%H:%M:%S")
                except Exception:
                    last_active_str = "Never"
                self._set_users_table_item(row, 9, last_active_str)

                dur = None
                try:
                    inactive_since = float(runtime.get('inactive_since') or 0)
                    if inactive_since > 0:
                        dur = int(now_ts - inactive_since)
                except Exception:
                    dur = None
                self._set_users_table_item(row, 10, f"{dur}s" if dur else "N/A")

                if rebuild:
                    age_text = "N/A"
                    try:
                        if not self._is_disconnected_server_label(server):
                            with self._antiafk_touch_lock:
                                last_ts = self._antiafk_last_touch_by_uid.get(uid)
                            if last_ts is None:
                                age_text = "0s"
                            else:
                                age_text = self._format_antiafk_age(int(now_ts - float(last_ts)))
                    except Exception:
                        age_text = "N/A"
                    self._set_users_table_item(row, 11, age_text)

                try:
                    if rebuild_widgets or table.cellWidget(row, 12) is None:
                        actions_widget = QWidget()
                        actions_layout = QHBoxLayout(actions_widget)
                        actions_layout.setContentsMargins(6, 4, 6, 4)
                        actions_layout.setSpacing(6)
                        actions_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

                        restart_btn = QPushButton("Restart")
                        restart_btn.clicked.connect(lambda _, uid=uid: self.restart_user_session(uid))
                        actions_layout.addWidget(restart_btn)

                        kill_btn = QPushButton("Kill")
                        try:
                            kill_btn.setProperty("class", "danger")
                        except Exception:
                            pass
                        kill_btn.clicked.connect(lambda _, uid=uid: self.kill_user_processes(uid))
                        actions_layout.addWidget(kill_btn)

                        table.setCellWidget(row, 12, actions_widget)
                except Exception:
                    pass

        finally:
            try:
                table.setUpdatesEnabled(True)
            except Exception:
                pass

    def _refresh_users_antiafk_age_column(self) -> None:
        """
        Update only the Users tab "Anti-AFK Age" column from the local last-touch state.
        """
        table = getattr(self, "users_table", None)
        if table is None:
            return

        try:
            row_count = int(table.rowCount())
        except Exception:
            return

        now_ts = time.time()
        try:
            table.setUpdatesEnabled(False)
        except Exception:
            pass

        try:
            for row in range(row_count):
                try:
                    uid_item = table.item(row, 0)
                    if uid_item is None:
                        continue
                    uid = str(uid_item.text() or "").strip()
                    if not uid:
                        continue

                    runtime = (self.user_data or {}).get(uid, {}) or {}
                    server = str(runtime.get("server", "") or "")
                    if self._is_disconnected_server_label(server):
                        text = "N/A"
                    else:
                        with self._antiafk_touch_lock:
                            last_ts = self._antiafk_last_touch_by_uid.get(uid)
                        if last_ts is None:
                            text = "0s"
                        else:
                            text = self._format_antiafk_age(int(now_ts - float(last_ts)))

                    item = table.item(row, 11)
                    if item is None:
                        table.setItem(row, 11, QTableWidgetItem(text))
                    else:
                        if item.text() != text:
                            item.setText(text)
                except Exception:
                    continue
        finally:
            try:
                table.setUpdatesEnabled(True)
            except Exception:
                pass

    def _refresh_users_created_column(self) -> None:
        """
        Update only the Users tab "Created" column from the latest per-PID process info.
        This avoids rebuilding the entire table on every process-signal tick.
        """
        table = getattr(self, "users_table", None)
        if table is None:
            return

        try:
            row_count = int(table.rowCount())
        except Exception:
            return

        try:
            table.setUpdatesEnabled(False)
        except Exception:
            pass

        try:
            for row in range(row_count):
                try:
                    uid_item = table.item(row, 0)
                    if uid_item is None:
                        continue
                    uid = str(uid_item.text() or "").strip()
                    if not uid:
                        continue

                    runtime = (self.user_data or {}).get(uid, {}) or {}
                    pids = runtime.get("pids", []) or []
                    if not isinstance(pids, (list, tuple)):
                        pids = [pids]

                    created_vals: List[str] = []
                    for pid in (pids or []):
                        try:
                            pid_i = int(pid)
                        except Exception:
                            continue
                        pdata = (self.process_data or {}).get(pid_i) or (self.process_data or {}).get(str(pid_i)) or {}
                        c = str(pdata.get("created", "") or "").strip()
                        if c:
                            created_vals.append(c)
                    created_str = ", ".join(created_vals) if created_vals else "N/A"
                    self._set_users_table_item(row, 8, created_str)
                except Exception:
                    continue
        finally:
            try:
                table.setUpdatesEnabled(True)
            except Exception:
                pass

    def append_log(self, message: str):
        """Compatibility wrapper so helpers can call parent.append_log()."""
        self.add_log(message)

    def _flush_log_queue(self) -> None:
        try:
            q = getattr(self, "_log_queue", None)
            if not q:
                return

            log_display = getattr(self, "log_display", None)
            activity_list = getattr(self, "activity_list", None)
            if log_display is None or activity_list is None:
                return

            batch: List[str] = []
            try:
                while q and len(batch) < 250:
                    batch.append(q.popleft())
            except Exception:
                return

            if not batch:
                return

            try:
                log_display.moveCursor(QTextCursor.MoveOperation.End)
                log_display.insertPlainText("\n".join(batch) + "\n")
            except Exception:
                pass

            try:
                self._activity_recent.extend(batch)
                activity_list.setPlainText("\n".join(self._activity_recent))
            except Exception:
                pass

            autoscroll = bool(getattr(self, "log_autoscroll", True))
            try:
                chk = getattr(self, "auto_scroll_checkbox", None)
                if chk is not None:
                    autoscroll = bool(chk.isChecked())
            except Exception:
                pass
            if autoscroll:
                try:
                    scrollbar = log_display.verticalScrollBar()
                    scrollbar.setValue(scrollbar.maximum())
                except Exception:
                    pass
        finally:
            try:
                self._flush_ocr_log_queue()
            except Exception:
                pass
            try:
                self._flush_antiafk_log_queue()
            except Exception:
                pass
            try:
                self._flush_autoitem_log_queue()
            except Exception:
                pass
            try:
                self._flush_bes_log_queue()
            except Exception:
                pass

    def _flush_ocr_log_queue(self) -> None:
        q = getattr(self, "_ocr_log_queue", None)
        if not q:
            return

        box = getattr(self, "ocr_log_box", None)
        if box is None:
            return

        batch: List[str] = []
        try:
            while q and len(batch) < 250:
                batch.append(q.popleft())
        except Exception:
            return

        if not batch:
            return

        autoscroll = bool(getattr(self, "ocr_log_autoscroll", True))
        scrollbar = None
        prev_scroll = 0
        try:
            scrollbar = box.verticalScrollBar()
            if scrollbar is not None:
                prev_scroll = int(scrollbar.value())
        except Exception:
            scrollbar = None

        try:
            cursor = QTextCursor(box.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText("\n".join(batch) + "\n")
        except Exception:
            try:
                for line in batch:
                    box.append(str(line))
            except Exception:
                pass

        try:
            if scrollbar is not None:
                if autoscroll:
                    scrollbar.setValue(scrollbar.maximum())
                else:
                    scrollbar.setValue(min(prev_scroll, int(scrollbar.maximum())))
            elif autoscroll:
                box.moveCursor(QTextCursor.MoveOperation.End)
        except Exception:
            pass

    def _flush_antiafk_log_queue(self) -> None:
        q = getattr(self, "_antiafk_log_queue", None)
        if not q:
            return

        box = getattr(self, "antiafk_status_box", None)
        if box is None:
            return

        batch: List[str] = []
        try:
            while q and len(batch) < 250:
                batch.append(q.popleft())
        except Exception:
            return

        if not batch:
            return

        try:
            box.moveCursor(QTextCursor.MoveOperation.End)
            box.insertPlainText("\n".join(batch) + "\n")
        except Exception:
            try:
                for line in batch:
                    box.append(str(line))
            except Exception:
                pass

        try:
            box.moveCursor(QTextCursor.MoveOperation.End)
        except Exception:
            pass

    def _flush_autoitem_log_queue(self) -> None:
        q = getattr(self, "_autoitem_log_queue", None)
        if not q:
            return

        box = getattr(self, "autoitem_status_box", None)
        if box is None:
            return

        batch: List[str] = []
        try:
            while q and len(batch) < 250:
                batch.append(q.popleft())
        except Exception:
            return

        if not batch:
            return

        try:
            box.moveCursor(QTextCursor.MoveOperation.End)
            box.insertPlainText("\n".join(batch) + "\n")
        except Exception:
            try:
                for line in batch:
                    box.append(str(line))
            except Exception:
                pass

        try:
            box.moveCursor(QTextCursor.MoveOperation.End)
        except Exception:
            pass

    def _flush_bes_log_queue(self) -> None:
        q = getattr(self, "_bes_log_queue", None)
        if not q:
            return

        box = getattr(self, "bes_log_box", None)
        if box is None:
            return

        batch: List[str] = []
        try:
            while q and len(batch) < 250:
                batch.append(q.popleft())
        except Exception:
            return

        if not batch:
            return

        try:
            box.moveCursor(QTextCursor.MoveOperation.End)
            box.insertPlainText("\n".join(batch) + "\n")
        except Exception:
            try:
                for line in batch:
                    box.append(str(line))
            except Exception:
                pass

        try:
            box.moveCursor(QTextCursor.MoveOperation.End)
        except Exception:
            pass

    def add_log(self, message):
        message = str(message or "")
        if message.startswith("[MultiScope] Watch hit"):
            chk = getattr(self, "watch_hit_chk", None)
            if chk is not None and not chk.isChecked():
                return
        if message.startswith("[SCAN-TRACE]"):
            chk = getattr(self, "scan_trace_chk", None)
            if chk is not None and not chk.isChecked():
                return
        if (
            message.startswith("[MultiScope] BIOME ")
            or message.startswith("[MultiScope] Skipping webhook")
        ):
            chk = getattr(self, "multiscope_biome_chk", None)
            if not (chk and chk.isChecked()):
                return
        if message.startswith("[LaunchGate]"):
            chk = getattr(self, "launch_gate_debug_chk", None)
            if chk is not None and not chk.isChecked():
                return
        if (
            not bool(getattr(self, "launch_debug_chk", None) and self.launch_debug_chk.isChecked())
            and (
                message.startswith("[LAUNCH")
                or message.startswith("[PIDWAIT")
                or message.startswith("[PID DEAD]")
                or message.startswith("[WindowCheck] killing")
                or message.startswith("[DEDUP]")
            )
        ):
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"

        try:
            self._log_queue.append(formatted_message)
        except Exception:
            try:
                self.log_display.append(formatted_message)
            except Exception:
                pass
        try:
            if not self._log_flush_timer:
                self._flush_log_queue()
        except Exception:
            pass

    def clear_logs(self):
        try:
            self._log_queue.clear()
            self._activity_recent.clear()
        except Exception:
            pass
        self.log_display.clear()
        try:
            self.activity_list.clear()
        except Exception:
            pass

    def clear_ocr_log(self) -> None:
        try:
            self._ocr_log_queue.clear()
        except Exception:
            pass
        try:
            self._last_ocr_log = None
        except Exception:
            pass
        try:
            self.ocr_log_box.clear()
        except Exception:
            pass

    def save_logs(self):
        try:
            try:
                self._flush_log_queue()
            except Exception:
                pass
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            base_dir = None
            try:
                base_dir = Path(self.config_manager.config_dir)
            except Exception:
                base_dir = Path.cwd()

            logs_dir = base_dir / "logs"
            try:
                logs_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            file_path = logs_dir / f"jaram_logs_{timestamp}.txt"

            def _box_text(box: Optional[QTextEdit]) -> str:
                if box is None:
                    return ""
                try:
                    return str(box.toPlainText() or "")
                except Exception:
                    return ""

            sections = [
                ("Main Logs", _box_text(getattr(self, "log_display", None))),
                ("OCR Log", _box_text(getattr(self, "ocr_log_box", None))),
                ("Anti-AFK Log", _box_text(getattr(self, "antiafk_status_box", None))),
                ("Auto-Item Log", _box_text(getattr(self, "autoitem_status_box", None))),
                ("BES Log", _box_text(getattr(self, "bes_log_box", None))),
            ]

            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("JARAM Logs Export\n")
                f.write(f"Generated: {timestamp}\n\n")
                for title, body in sections:
                    f.write(f"===== {title} =====\n")
                    if body.strip():
                        f.write(body.rstrip() + "\n")
                    f.write("\n")

            QMessageBox.information(self, "Success", f"Logs saved to {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save logs: {e}")

    def open_user_management(self):
        dialog = UserManagementDialog(self)
        dialog.exec()

    def open_settings(self):
        idx = self.settings_tab_index if self.settings_tab_index is not None else 5
        self.tab_widget.setCurrentIndex(idx)

    def _trim_settings_text(self, text: str, limit: int = 80) -> str:
        text = str(text or "").replace("\r", "").replace("\n", "\\n")
        if len(text) > limit:
            return text[: max(0, limit - 3)] + "..."
        return text

    def _summarize_settings_list(self, value: list) -> str:
        if not value:
            return "[]"
        if all(isinstance(v, dict) for v in value):
            names = []
            for v in value:
                if not isinstance(v, dict):
                    continue
                name = v.get("name") or v.get("url") or v.get("id")
                if name:
                    names.append(str(name))
            if names:
                preview = ", ".join(self._trim_settings_text(n, 24) for n in names[:3])
                suffix = "" if len(names) <= 3 else f", +{len(names) - 3} more"
                return f"[{len(value)} items: {preview}{suffix}]"
            return f"[{len(value)} items]"
        if all(isinstance(v, (str, int, float, bool)) for v in value):
            preview_vals = ", ".join(self._trim_settings_text(v, 24) for v in value[:4])
            suffix = "" if len(value) <= 4 else f", +{len(value) - 4} more"
            return f"[{preview_vals}{suffix}]"
        return f"[{len(value)} items]"

    def _format_settings_value(self, value) -> str:
        if isinstance(value, bool):
            return "On" if value else "Off"
        if value is None:
            return "None"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return f"\"{self._trim_settings_text(value, 80)}\""
        if isinstance(value, list):
            return self._summarize_settings_list(value)
        if isinstance(value, dict):
            return f"{{{len(value)} keys}}"
        return self._trim_settings_text(str(value), 80)

    def _pretty_setting_label(self, path: str) -> str:
        if not path:
            return "Settings"
        label = self._settings_label_map.get(path)
        if label:
            return label
        return path

    def _format_webhook_ref_for_diff(self, url: str, name: str) -> str:
        url = str(url or "").strip()
        name = str(name or "").strip()
        if name and url:
            return f"{self._trim_settings_text(name, 28)} ({self._trim_settings_text(url, 48)})"
        return name or self._trim_settings_text(url, 80) or "Webhook"

    def _normalize_webhook_users_for_diff(self, entry: dict) -> Optional[list[str]]:
        raw_users = entry.get("users", None)
        explicit = bool(entry.get("users_explicit", raw_users is not None))
        if not explicit:
            return None
        if not isinstance(raw_users, (list, tuple, set)):
            return []
        cleaned = [str(u).strip() for u in raw_users if str(u).strip()]
        return sorted({u for u in cleaned})

    def _format_webhook_users_for_diff(self, users: Optional[list[str]]) -> str:
        if users is None:
            return "All users"
        if not users:
            return "No users"
        preview = ", ".join(self._trim_settings_text(u, 16) for u in users[:3])
        suffix = "" if len(users) <= 3 else f", +{len(users) - 3} more"
        return f"{len(users)} user(s): {preview}{suffix}"

    def _normalize_webhook_biome_modes_for_diff(self, entry: dict) -> dict:
        modes_raw = entry.get("biome_modes", None)
        if isinstance(modes_raw, dict) and modes_raw:
            out = {}
            for k, v in modes_raw.items():
                key = str(k).strip().upper()
                if not key:
                    continue
                val = str(v).strip()
                if val not in ("None", "Message", "Everyone"):
                    val = val or "None"
                out[key] = val
            return out
        allowed = entry.get("biomes", []) or []
        allowed_set = {str(b).strip().upper() for b in allowed if str(b).strip()}
        return {b: "Message" for b in allowed_set}

    def _diff_webhooks_for_changes(self, old_list: list, new_list: list) -> list:
        def _idx_by_url(items: list) -> dict:
            out = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                url = str(it.get("url", "") or "").strip()
                if not url:
                    continue
                out.setdefault(url, []).append(it)
            return out

        old_by_url = _idx_by_url(old_list or [])
        new_by_url = _idx_by_url(new_list or [])
        urls = sorted(set(old_by_url.keys()) | set(new_by_url.keys()), key=lambda u: str(u))

        ordered_biomes = [str(b).strip().upper() for b in (GUI_BIOME_NAMES or []) if str(b).strip()]
        changes = []

        for url in urls:
            if url not in old_by_url:
                entry = new_by_url[url][0]
                changes.append(
                    f"Webhooks: (added) {self._format_webhook_ref_for_diff(url, entry.get('name', ''))}"
                )
                continue
            if url not in new_by_url:
                entry = old_by_url[url][0]
                changes.append(
                    f"Webhooks: (removed) {self._format_webhook_ref_for_diff(url, entry.get('name', ''))}"
                )
                continue

            old = old_by_url[url][0]
            new = new_by_url[url][0]

            segs = []
            old_name = str(old.get("name", "") or "").strip()
            new_name = str(new.get("name", "") or "").strip()
            if old_name != new_name:
                segs.append(f"name {self._format_settings_value(old_name)} -> {self._format_settings_value(new_name)}")

            old_users = self._normalize_webhook_users_for_diff(old)
            new_users = self._normalize_webhook_users_for_diff(new)
            if old_users != new_users:
                segs.append(f"users {self._format_webhook_users_for_diff(old_users)} -> {self._format_webhook_users_for_diff(new_users)}")

            old_modes = self._normalize_webhook_biome_modes_for_diff(old)
            new_modes = self._normalize_webhook_biome_modes_for_diff(new)
            biome_keys = list(ordered_biomes)
            extra_keys = sorted({*old_modes.keys(), *new_modes.keys()} - set(biome_keys))
            biome_keys.extend(extra_keys)
            changed_biomes = []
            for b in biome_keys:
                if (old_modes.get(b, "None") or "None") != (new_modes.get(b, "None") or "None"):
                    changed_biomes.append(b)

            if changed_biomes:
                parts = []
                for b in changed_biomes[:4]:
                    parts.append(f"{b} {old_modes.get(b, 'None')} -> {new_modes.get(b, 'None')}")
                suffix = "" if len(changed_biomes) <= 4 else f", +{len(changed_biomes) - 4} more"
                segs.append(f"biomes {', '.join(parts)}{suffix}")

            if segs:
                changes.append(
                    f"Webhook {self._format_webhook_ref_for_diff(url, new_name or old_name)}: " + "; ".join(segs)
                )

        return changes

    def _diff_settings_values(self, old, new, path: str = "") -> list:
        changes = []
        if isinstance(old, dict) and isinstance(new, dict):
            keys = sorted(set(old.keys()) | set(new.keys()), key=lambda k: str(k))
            for key in keys:
                sub_path = f"{path}.{key}" if path else str(key)
                if key not in old:
                    changes.append(f"{self._pretty_setting_label(sub_path)}: (added) {self._format_settings_value(new[key])}")
                    continue
                if key not in new:
                    changes.append(f"{self._pretty_setting_label(sub_path)}: (removed) {self._format_settings_value(old[key])}")
                    continue
                changes.extend(self._diff_settings_values(old[key], new[key], sub_path))
            return changes

        if isinstance(old, list) and isinstance(new, list):
            if old == new:
                return []
            if path == "webhooks":
                return self._diff_webhooks_for_changes(old, new)
            label = self._pretty_setting_label(path)
            return [f"{label}: {self._summarize_settings_list(old)} -> {self._summarize_settings_list(new)}"]

        if old != new:
            label = self._pretty_setting_label(path)
            changes.append(f"{label}: {self._format_settings_value(old)} -> {self._format_settings_value(new)}")
        return changes

    def _collect_webhooks_from_ui(self) -> list:
        try:
            _users_cfg = self.config_manager.load_users() or {}
        except Exception:
            _users_cfg = {}
        all_user_ids = {str(uid) for uid in _users_cfg.keys()} if isinstance(_users_cfg, dict) else set()

        webhooks = []
        rows = self.webhooks_table.rowCount()
        for r in range(rows):
            name_item = self.webhooks_table.item(r, 0)
            url_item = self.webhooks_table.item(r, 1)
            name = (name_item.text().strip() if name_item else "")
            url = (url_item.text().strip() if url_item else "")
            if not url:
                continue

            allowed = []
            biome_modes = {}
            data_item = name_item or url_item
            user_filter = data_item.data(Qt.ItemDataRole.UserRole) if data_item else None
            selected_users: list[str] = []
            explicit_users = False
            if isinstance(user_filter, (list, tuple, set)):
                explicit_users = True
                selected_users = [str(u).strip() for u in user_filter if str(u).strip()]
                selected_users = sorted({u for u in selected_users})
                if all_user_ids and set(selected_users) == all_user_ids:
                    explicit_users = False
                    selected_users = []

            for idx, biome_name in enumerate(GUI_BIOME_NAMES):
                w = self.webhooks_table.cellWidget(r, 2 + idx)
                cb = None
                if isinstance(w, QComboBox):
                    cb = w
                elif hasattr(w, "findChild"):
                    cb = w.findChild(QComboBox)

                if cb is not None:
                    mode = cb.currentText()
                    if str(biome_name).upper() in ("GLITCHED", "DREAMSPACE", "CYBERSPACE") and _bm_lock_enforced():
                        mode = "Everyone"
                    biome_modes[str(biome_name).upper()] = mode
                    if mode in ("Message", "Everyone"):
                        allowed.append(str(biome_name).upper())
                elif hasattr(w, "isChecked") and w.isChecked():
                    allowed.append(str(biome_name).upper())

            entry = {"name": name, "url": url, "biomes": allowed}
            if biome_modes:
                entry["biome_modes"] = biome_modes
            if explicit_users:
                entry["users"] = selected_users
                entry["users_explicit"] = True
            webhooks.append(entry)

        return webhooks

    def _get_settings_tab_snapshot(self) -> dict:
        snapshot = {
            "window_limit": int(self.settings_window_limit_input.value()),
            "spares_mode": bool(self.spares_mode_chk.isChecked()),
            "spares_fraction": str(self.spares_split_cmb.currentText()),
            "roblox_window_geometry": {
                "enforce_on_launch": bool(self.rbwin_geom_enforce_chk.isChecked()) if hasattr(self, "rbwin_geom_enforce_chk") else False,
                "x": int((getattr(self, "_roblox_window_geometry", {}) or {}).get("x", 0) or 0),
                "y": int((getattr(self, "_roblox_window_geometry", {}) or {}).get("y", 0) or 0),
                "w": int((getattr(self, "_roblox_window_geometry", {}) or {}).get("w", 0) or 0),
                "h": int((getattr(self, "_roblox_window_geometry", {}) or {}).get("h", 0) or 0),
            },
            "timeouts": {
                "initial_delay": int(self.settings_initial_delay_input.value()),
                "offline": int(self.settings_offline_threshold_input.value()),
                "launch_delay": int(self.settings_launch_delay_input.value()),
                "strap_threshold": int(self.settings_strap_threshold_input.value()),
                "handoff_lead": int(self.handoff_lead_input.value()),
                "early_join_window": int(self.early_join_window_input.value()),
            },
            "timeout_monitor": {
                "kill_enabled": bool(self.kill_after_enable_chk.isChecked()),
                "kill_timeout": int(self.kill_timeout_input.value()),
                "poll_interval": int(self.poll_interval_input.value()),
            },
            "alerts": {
                "webhook_url": str(self.webhook_input.text().strip()),
                "blackout_ping": str(self.blackout_ping_input.text().strip()),
                "cap_message": str(self.cap_msg_input.text().strip()),
                "bad_message": str(self.bad_msg_input.text().strip()) if hasattr(self, "bad_msg_input") else "",
                "hourly_users_report_enabled": bool(
                    self.hourly_users_report_chk.isChecked()
                ) if hasattr(self, "hourly_users_report_chk") else False,
                "hourly_users_report_interval_hours": int(
                    self.hourly_users_report_interval_spin.value()
                ) if hasattr(self, "hourly_users_report_interval_spin") else 1,
            },
            "webhooks": self._collect_webhooks_from_ui(),
            "ui": {
                "webhooks_hidden_biomes": sorted(
                    {str(b).strip().upper() for b in (getattr(self, "_webhooks_hidden_biomes", set()) or set()) if str(b).strip()}
                ),
                "show_tutorial_menu": bool(self.ui_show_tutorial_menu_chk.isChecked()) if hasattr(self, "ui_show_tutorial_menu_chk") else False,
                "show_selected_sets_bes_exempt_slot1": bool(
                    self.ui_show_selected_sets_bes_exempt_slot1_chk.isChecked()
                ) if hasattr(self, "ui_show_selected_sets_bes_exempt_slot1_chk") else False,
            },
            "multiscope": {
                "merchant_webhook": str(self.ms_merchant_webhook_input.text().strip()) if hasattr(self, "ms_merchant_webhook_input") else "",
                "enable_jester": bool(self.ms_enable_jester.isChecked()) if hasattr(self, "ms_enable_jester") else True,
                "enable_mari": bool(self.ms_enable_mari.isChecked()) if hasattr(self, "ms_enable_mari") else True,
                "enable_rin": bool(self.ms_enable_rin.isChecked()) if hasattr(self, "ms_enable_rin") else True,
                "merchant_detection_mode": str(self.ms_merchant_detection_mode.currentData() or "asset_id") if hasattr(self, "ms_merchant_detection_mode") else "asset_id",
                "jester_ping_type": str(self.ms_jester_type.currentText()) if hasattr(self, "ms_jester_type") else "None",
                "jester_ping_id": str(self.ms_jester_id.text().strip()) if hasattr(self, "ms_jester_id") else "",
                "mari_ping_type": str(self.ms_mari_type.currentText()) if hasattr(self, "ms_mari_type") else "None",
                "mari_ping_id": str(self.ms_mari_id.text().strip()) if hasattr(self, "ms_mari_id") else "",
                "rin_ping_type": str(self.ms_rin_type.currentText()) if hasattr(self, "ms_rin_type") else "None",
                "rin_ping_id": str(self.ms_rin_id.text().strip()) if hasattr(self, "ms_rin_id") else "",
            },
            "misc": {
                "skip_webhook_unknown_context": bool(
                    self.misc_skip_unknown_webhook_chk.isChecked()
                ) if hasattr(self, "misc_skip_unknown_webhook_chk") else False,
                "disable_log_based_merchant_detection_when_ocr_merchants_enabled": bool(
                    self.misc_disable_log_merchants_when_ocr_active_chk.isChecked()
                ) if hasattr(self, "misc_disable_log_merchants_when_ocr_active_chk") else True,
                "log_confirmed_launch_mode": bool(
                    self.misc_log_confirmed_launch_mode_chk.isChecked()
                ) if hasattr(self, "misc_log_confirmed_launch_mode_chk") else False,
                "disable_manager_bad_marking": bool(
                    self.misc_disable_manager_bad_marking_chk.isChecked()
                ) if hasattr(self, "misc_disable_manager_bad_marking_chk") else False,
                "msedgewebview2_limiter_enabled": bool(
                    self.misc_msedgewebview2_limiter_enabled_chk.isChecked()
                ) if hasattr(self, "misc_msedgewebview2_limiter_enabled_chk") else True,
            },
        }
        return snapshot

    def _get_settings_changes(self) -> list:
        baseline = getattr(self, "_settings_baseline", None)
        if baseline is None:
            return []
        current = self._get_settings_tab_snapshot()
        return self._diff_settings_values(baseline, current)

    def _format_settings_changes(self, changes: list, max_lines: int = 12) -> tuple[str, str]:
        if not changes:
            return ("No changes detected.", "No changes detected.")
        shown = changes
        truncated = False
        if len(changes) > max_lines:
            shown = changes[:max_lines]
            shown.append(f"...and {len(changes) - max_lines} more")
            truncated = True
        short_text = "\n".join(f"- {c}" for c in shown)
        full_text = "\n".join(f"- {c}" for c in changes) if truncated else short_text
        return short_text, full_text

    def _prompt_settings_save(
        self,
        title: str,
        message: str,
        allow_cancel: bool,
        changes: Optional[list] = None,
        no_text: Optional[str] = None,
    ):
        if changes is None:
            changes = self._get_settings_changes()
        if not changes:
            return None
        short_text, full_text = self._format_settings_changes(changes)
        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText(message)
        msg.setInformativeText(f"Changes:\n{short_text}")
        if full_text != short_text:
            msg.setDetailedText(f"Changes:\n{full_text}")
        if allow_cancel:
            msg.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel
            )
            msg.setDefaultButton(QMessageBox.StandardButton.Yes)
        else:
            msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            msg.setDefaultButton(QMessageBox.StandardButton.Yes)

        if no_text:
            try:
                btn = msg.button(QMessageBox.StandardButton.No)
                if btn is not None:
                    btn.setText(str(no_text))
            except Exception:
                pass
        return msg.exec()

    def _on_tab_changed(self, new_index: int) -> None:
        if self._tab_change_guard:
            return
        prev_index = self._last_tab_index
        self._last_tab_index = new_index

        try:
            if self.users_tab_index is not None and new_index == self.users_tab_index and self._users_table_dirty:
                self._schedule_users_table_refresh(force_full=True)
        except Exception:
            pass
        try:
            if self.multiscope_tab_index is not None and new_index == self.multiscope_tab_index and self._multiscope_table_dirty:
                self._schedule_multiscope_table_refresh(immediate=True)
        except Exception:
            pass
        try:
            if getattr(self, "accounts_tab_index", None) is not None and new_index == self.accounts_tab_index:
                self.refresh_accounts_list()
        except Exception:
            pass

        if not self._settings_prompt_ready or prev_index is None:
            return
        if self.settings_tab_index is None:
            return
        if prev_index != self.settings_tab_index or new_index == self.settings_tab_index:
            return

        changes = self._get_settings_changes()
        if not changes:
            return

        result = self._prompt_settings_save(
            title="Unsaved Settings",
            message="You have unsaved settings changes. Save before leaving Settings?",
            allow_cancel=True,
            changes=changes,
            no_text="Revert",
        )
        if result == QMessageBox.StandardButton.Yes:
            if not self.save_settings(confirm=False):
                self._tab_change_guard = True
                try:
                    self.tab_widget.setCurrentIndex(prev_index)
                finally:
                    self._tab_change_guard = False
                    self._last_tab_index = prev_index
            return
        if result == QMessageBox.StandardButton.Cancel:
            self._tab_change_guard = True
            try:
                self.tab_widget.setCurrentIndex(prev_index)
            finally:
                self._tab_change_guard = False
                self._last_tab_index = prev_index
        if result == QMessageBox.StandardButton.No:
            self.load_settings_tab()

    def load_settings_tab(self):
        """Populate Settings UI from settings.json (timings + shutdown monitor + tri-mode webhooks + optional merchant/pings)."""
        settings = self.config_manager.load_settings()

        # ---------- Basic ----------
        self.settings_window_limit_input.setValue(settings.get("window_limit", 1))
        self.spares_mode_chk.setChecked(bool(settings.get("spares_mode", False)))
        self.spares_split_cmb.setCurrentText(settings.get("spares_fraction", "1/2"))

        # ---------- Roblox Window Geometry ----------
        rwg = settings.get("roblox_window_geometry", {}) or {}
        if not isinstance(rwg, dict):
            rwg = {}
        if hasattr(self, "rbwin_geom_enforce_chk"):
            try:
                self.rbwin_geom_enforce_chk.setChecked(bool(rwg.get("enforce_on_launch", False)))
            except Exception:
                pass
        try:
            self._roblox_window_geometry = {
                "x": int(rwg.get("x", 0) or 0),
                "y": int(rwg.get("y", 0) or 0),
                "w": int(rwg.get("w", 0) or 0),
                "h": int(rwg.get("h", 0) or 0),
            }
        except Exception:
            self._roblox_window_geometry = {"x": 0, "y": 0, "w": 0, "h": 0}
        try:
            self._refresh_rbwin_geom_status()
        except Exception:
            pass

        # ---------- Timing (timeouts) ----------
        t = settings.get("timeouts", {}) or {}
        self.settings_initial_delay_input.setValue(t.get("initial_delay", 10))
        self.settings_offline_threshold_input.setValue(t.get("offline", 120))
        self.settings_launch_delay_input.setValue(t.get("launch_delay", 10))
        self.settings_strap_threshold_input.setValue(t.get("strap_threshold", 10))
        self.handoff_lead_input.setValue(t.get("handoff_lead", 25))
        self.early_join_window_input.setValue(t.get("early_join_window", 60))

        # ---------- Shutdown monitor (timeout_monitor) ----------
        tm = settings.get("timeout_monitor", {}) or {}
        self.kill_timeout_input.setValue(tm.get("kill_timeout", 850))
        self.poll_interval_input.setValue(tm.get("poll_interval", 10))
        self.kill_after_enable_chk.setChecked(bool(tm.get("kill_enabled", False)))
        self.kill_timeout_input.setEnabled(self.kill_after_enable_chk.isChecked())

        # ---------- Alerts ----------
        alerts = settings.get("alerts", {}) or {}
        if not isinstance(alerts, dict):
            alerts = {}
        self.webhook_input.setText(str(alerts.get("webhook_url") or tm.get("webhook_url", "") or ""))
        self.blackout_ping_input.setText(
            str(alerts.get("blackout_ping") or alerts.get("ping_message") or tm.get("ping_message", "") or "")
        )
        self.cap_msg_input.setText(str(alerts.get("cap_message") or ""))
        if hasattr(self, "bad_msg_input"):
            self.bad_msg_input.setText(str(alerts.get("bad_message") or ""))
        hourly_enabled = bool(alerts.get("hourly_users_report_enabled", False))
        try:
            hourly_interval = int(alerts.get("hourly_users_report_interval_hours", 1) or 1)
        except Exception:
            hourly_interval = 1
        hourly_interval = max(1, min(168, hourly_interval))
        if hasattr(self, "hourly_users_report_chk"):
            self.hourly_users_report_chk.setChecked(hourly_enabled)
        if hasattr(self, "hourly_users_report_interval_spin"):
            self.hourly_users_report_interval_spin.setValue(hourly_interval)
            self.hourly_users_report_interval_spin.setEnabled(hourly_enabled)
        self._hourly_users_report_last_sent_at = 0.0
        self._hourly_users_report_interval_hours = hourly_interval

        # ---------- Webhooks table (tri-mode + legacy) ----------
        ui = settings.get("ui", {}) or {}
        if not isinstance(ui, dict):
            ui = {}
        ui_defaults = self.config_manager.default_settings.get("ui", {}) or {}
        if not isinstance(ui_defaults, dict):
            ui_defaults = {}
        hidden_biomes = ui.get("webhooks_hidden_biomes", []) or []
        show_tutorial = bool(ui.get("show_tutorial_menu", False))
        show_selected_sets_bes_slot1 = bool(
            ui.get(
                "show_selected_sets_bes_exempt_slot1",
                ui_defaults.get("show_selected_sets_bes_exempt_slot1", False),
            )
        )
        if hasattr(self, "_apply_webhook_biome_column_visibility"):
            try:
                self._apply_webhook_biome_column_visibility(hidden_biomes)
            except Exception:
                pass
        if hasattr(self, "ui_show_tutorial_menu_chk"):
            try:
                self.ui_show_tutorial_menu_chk.setChecked(show_tutorial)
            except Exception:
                pass
        if hasattr(self, "ui_show_selected_sets_bes_exempt_slot1_chk"):
            try:
                self.ui_show_selected_sets_bes_exempt_slot1_chk.setChecked(show_selected_sets_bes_slot1)
            except Exception:
                pass
        self._apply_tutorial_menu_visibility(show_tutorial)

        self._clear_webhook_rows()
        for wh in (settings.get("webhooks", []) or []):
            name    = wh.get("name", "")
            url     = wh.get("url", "")
            allowed = wh.get("biomes", []) or []      # legacy list your worker already uses
            modes   = wh.get("biome_modes", {}) or {} # optional per-biome tri-state
            raw_users = wh.get("users", None)
            users_explicit = bool(wh.get("users_explicit", raw_users is not None))
            users   = raw_users if users_explicit else None  # optional per-user routing
            if url:
                try:
                    self._add_webhook_row(name, url, allowed, modes, users)
                except TypeError:
                    self._add_webhook_row(name, url, allowed)

        # ---------- Optional: Merchant + Pings (safe if widgets exist) ----------
        ms = settings.get("multiscope", {}) or {}
        if hasattr(self, "ms_merchant_webhook_input"):
            self.ms_merchant_webhook_input.setText(ms.get("merchant_webhook", ""))

        if hasattr(self, "ms_enable_jester"):
            self.ms_enable_jester.setChecked(bool(ms.get("enable_jester", True)))
        if hasattr(self, "ms_enable_mari"):
            self.ms_enable_mari.setChecked(bool(ms.get("enable_mari", True)))
        if hasattr(self, "ms_enable_rin"):
            self.ms_enable_rin.setChecked(bool(ms.get("enable_rin", True)))
        if hasattr(self, "ms_merchant_detection_mode"):
            mode = str(ms.get("merchant_detection_mode", "asset_id") or "asset_id").strip().lower()
            if mode in {"legacy", "chat", "merchant", "merchant_chat"}:
                mode = "legacy_chat"
            idx = self.ms_merchant_detection_mode.findData(mode)
            if idx < 0:
                idx = self.ms_merchant_detection_mode.findData("asset_id")
            if idx >= 0:
                self.ms_merchant_detection_mode.setCurrentIndex(idx)

        if hasattr(self, "ms_jester_type"):
            self.ms_jester_type.setCurrentText(ms.get("jester_ping_type", "None"))
        if hasattr(self, "ms_jester_id"):
            self.ms_jester_id.setText(ms.get("jester_ping_id", ""))

        if hasattr(self, "ms_mari_type"):
            self.ms_mari_type.setCurrentText(ms.get("mari_ping_type", "None"))
        if hasattr(self, "ms_mari_id"):
            self.ms_mari_id.setText(ms.get("mari_ping_id", ""))
        if hasattr(self, "ms_rin_type"):
            self.ms_rin_type.setCurrentText(ms.get("rin_ping_type", "None"))
        if hasattr(self, "ms_rin_id"):
            self.ms_rin_id.setText(ms.get("rin_ping_id", ""))

        # ---------- Misc ----------
        misc = settings.get("misc", {}) or {}
        skip_unknown = None
        disable_log_merchants_when_ocr_active = None
        log_confirmed_launch_mode = None
        disable_manager_bad_marking = None
        msedge_limiter_enabled = None
        if isinstance(misc, dict) and "skip_webhook_unknown_context" in misc:
            skip_unknown = bool(misc.get("skip_webhook_unknown_context", False))
        if isinstance(misc, dict) and MISC_DISABLE_LOG_MERCHANTS_WHEN_OCR_ACTIVE_KEY in misc:
            disable_log_merchants_when_ocr_active = bool(
                misc.get(MISC_DISABLE_LOG_MERCHANTS_WHEN_OCR_ACTIVE_KEY, True)
            )
        if isinstance(misc, dict) and "log_confirmed_launch_mode" in misc:
            log_confirmed_launch_mode = bool(misc.get("log_confirmed_launch_mode", False))
        if isinstance(misc, dict) and "disable_manager_bad_marking" in misc:
            disable_manager_bad_marking = bool(misc.get("disable_manager_bad_marking", False))
        if isinstance(misc, dict) and "msedgewebview2_limiter_enabled" in misc:
            msedge_limiter_enabled = bool(misc.get("msedgewebview2_limiter_enabled", True))
        if skip_unknown is None:
            ocr_cfg = settings.get("ocr", {}) or {}
            if isinstance(ocr_cfg, dict) and "skip_webhook_unknown_context" in ocr_cfg:
                skip_unknown = bool(ocr_cfg.get("skip_webhook_unknown_context", False))
        if skip_unknown is None:
            skip_unknown = bool(
                self.config_manager.default_settings.get("misc", {}).get("skip_webhook_unknown_context", False)
            )
        if disable_log_merchants_when_ocr_active is None:
            disable_log_merchants_when_ocr_active = bool(
                self.config_manager.default_settings.get("misc", {}).get(
                    MISC_DISABLE_LOG_MERCHANTS_WHEN_OCR_ACTIVE_KEY,
                    True,
                )
            )
        if log_confirmed_launch_mode is None:
            log_confirmed_launch_mode = bool(
                self.config_manager.default_settings.get("misc", {}).get("log_confirmed_launch_mode", False)
            )
        if disable_manager_bad_marking is None:
            disable_manager_bad_marking = bool(
                self.config_manager.default_settings.get("misc", {}).get("disable_manager_bad_marking", False)
            )
        if msedge_limiter_enabled is None:
            msedge_limiter_enabled = bool(
                self.config_manager.default_settings.get("misc", {}).get("msedgewebview2_limiter_enabled", True)
            )
        if hasattr(self, "misc_skip_unknown_webhook_chk"):
            self.misc_skip_unknown_webhook_chk.setChecked(bool(skip_unknown))
        if hasattr(self, "misc_disable_log_merchants_when_ocr_active_chk"):
            self.misc_disable_log_merchants_when_ocr_active_chk.setChecked(
                bool(disable_log_merchants_when_ocr_active)
            )
        if hasattr(self, "misc_log_confirmed_launch_mode_chk"):
            self.misc_log_confirmed_launch_mode_chk.setChecked(bool(log_confirmed_launch_mode))
        if hasattr(self, "misc_disable_manager_bad_marking_chk"):
            self.misc_disable_manager_bad_marking_chk.setChecked(bool(disable_manager_bad_marking))
            self._sync_misc_disable_bad_marking_warning(
                self.misc_disable_manager_bad_marking_chk.isChecked()
            )
        if hasattr(self, "misc_msedgewebview2_limiter_enabled_chk"):
            self.misc_msedgewebview2_limiter_enabled_chk.setChecked(bool(msedge_limiter_enabled))

        # ---------- OCR tab ----------
        self._apply_ocr_settings_to_ui(settings.get("ocr", {}))
        try:
            self._settings_baseline = self._get_settings_tab_snapshot()
        except Exception:
            self._settings_baseline = None

    def _refresh_rbwin_geom_status(self) -> None:
        try:
            geom = getattr(self, "_roblox_window_geometry", {}) or {}
        except Exception:
            geom = {}

        def _i(v, default=0) -> int:
            try:
                return int(v)
            except Exception:
                return int(default)

        x = _i(geom.get("x", 0))
        y = _i(geom.get("y", 0))
        w = _i(geom.get("w", 0))
        h = _i(geom.get("h", 0))

        if not hasattr(self, "rbwin_geom_status_lbl"):
            return

        if w > 0 and h > 0:
            self.rbwin_geom_status_lbl.setText(f"Recorded: x={x}, y={y}, w={w}, h={h}")
        else:
            self.rbwin_geom_status_lbl.setText("Recorded: none")

    def _sync_misc_disable_bad_marking_warning(self, checked: bool) -> None:
        try:
            self.misc_disable_manager_bad_marking_warn_lbl.setVisible(bool(checked))
        except Exception:
            pass

    def _warn_misc_disable_bad_marking_enabled(self, checked: bool) -> None:
        if not checked:
            return
        QMessageBox.warning(
            self,
            "Rate Limit Warning",
            "Disabling manager auto-BAD marking can cause invalid cookies to keep retrying "
            "Roblox authentication requests, which may trigger rate limiting.",
        )

    def _record_roblox_window_geometry(self) -> None:
        try:
            import psutil
            import win32gui as _wgui
            import win32process as _wproc
        except Exception as e:
            QMessageBox.warning(self, "Missing Dependencies", f"Window capture requires pywin32/psutil.\n\n{e}")
            return

        def _is_roblox_pid(pid: int) -> bool:
            try:
                return str(psutil.Process(int(pid)).name()).lower() == "robloxplayerbeta.exe"
            except Exception:
                return False

        hwnd = None
        try:
            fg = _wgui.GetForegroundWindow()
            if fg and _wgui.IsWindow(fg):
                _, pid = _wproc.GetWindowThreadProcessId(fg)
                if pid and _is_roblox_pid(int(pid)):
                    hwnd = int(fg)
        except Exception:
            hwnd = None

        if not hwnd:
            try:
                wins = enum_roblox_windows()
            except Exception:
                wins = []

            best_hwnd = None
            best_area = -1
            for w in wins:
                try:
                    if _wgui.IsIconic(int(w.hwnd)):
                        continue
                    wl, wt, wr, wb = _wgui.GetWindowRect(int(w.hwnd))
                    area = int(wr - wl) * int(wb - wt)
                    if area > best_area:
                        best_area = area
                        best_hwnd = int(w.hwnd)
                except Exception:
                    continue
            hwnd = best_hwnd

        if not hwnd:
            QMessageBox.warning(self, "No Roblox Window", "No visible Roblox windows were found.")
            return

        try:
            wl, wt, wr, wb = _wgui.GetWindowRect(int(hwnd))
            w = int(wr - wl)
            h = int(wb - wt)
        except Exception as e:
            QMessageBox.warning(self, "Record Failed", f"Failed to read window size/position.\n\n{e}")
            return

        if w <= 0 or h <= 0:
            QMessageBox.warning(self, "Record Failed", "Invalid Roblox window size detected.")
            return

        self._roblox_window_geometry = {"x": int(wl), "y": int(wt), "w": int(w), "h": int(h)}
        self._refresh_rbwin_geom_status()

        QMessageBox.information(
            self,
            "Recorded",
            "Roblox window size/position recorded.\n\nClick “Save Settings” to persist this change.",
        )

    def save_settings(self, *args, confirm: bool = True):
        """Collect Settings UI and persist to settings.json, then live-apply."""
        if confirm:
            changes = self._get_settings_changes()
            if not changes:
                QMessageBox.information(self, "Save Settings", "No changes to save.")
                return False
            result = self._prompt_settings_save(
                title="Save Settings",
                message="Save the following settings changes?",
                allow_cancel=True,
                changes=changes,
                no_text="Revert",
            )
            if result == QMessageBox.StandardButton.No:
                self.load_settings_tab()
                return False
            if result != QMessageBox.StandardButton.Yes:
                return False

        settings = self.config_manager.load_settings()

        # ---------- Basic ----------
        settings["window_limit"] = self.settings_window_limit_input.value()
        settings["spares_mode"]  = bool(self.spares_mode_chk.isChecked())
        settings["spares_fraction"] = self.spares_split_cmb.currentText()

        # ---------- Roblox Window Geometry ----------
        rwg = settings.get("roblox_window_geometry", {}) or {}
        if not isinstance(rwg, dict):
            rwg = {}
        if hasattr(self, "rbwin_geom_enforce_chk"):
            try:
                rwg["enforce_on_launch"] = bool(self.rbwin_geom_enforce_chk.isChecked())
            except Exception:
                rwg["enforce_on_launch"] = False
        geom = getattr(self, "_roblox_window_geometry", {}) or {}
        try:
            rwg["x"] = int(geom.get("x", 0) or 0)
        except Exception:
            rwg["x"] = 0
        try:
            rwg["y"] = int(geom.get("y", 0) or 0)
        except Exception:
            rwg["y"] = 0
        try:
            rwg["w"] = int(geom.get("w", 0) or 0)
        except Exception:
            rwg["w"] = 0
        try:
            rwg["h"] = int(geom.get("h", 0) or 0)
        except Exception:
            rwg["h"] = 0
        settings["roblox_window_geometry"] = rwg

        # ---------- Timing (timeouts) ----------
        t = settings.get("timeouts", {}) or {}
        t["initial_delay"]     = self.settings_initial_delay_input.value()
        t["offline"]           = self.settings_offline_threshold_input.value()
        t["launch_delay"]      = self.settings_launch_delay_input.value()
        t["strap_threshold"]   = self.settings_strap_threshold_input.value()
        t["handoff_lead"]      = self.handoff_lead_input.value()
        t["early_join_window"] = self.early_join_window_input.value()
        settings["timeouts"]   = t

        # ---------- Shutdown monitor (timeout_monitor) ----------
        tm = settings.get("timeout_monitor", {}) or {}
        tm["kill_enabled"]  = bool(self.kill_after_enable_chk.isChecked())
        tm["kill_timeout"]  = self.kill_timeout_input.value()
        tm["poll_interval"] = self.poll_interval_input.value()
        settings["timeout_monitor"] = tm

        # ---------- Alerts ----------
        alerts = settings.get("alerts", {}) or {}
        if not isinstance(alerts, dict):
            alerts = {}
        alerts["webhook_url"] = self.webhook_input.text().strip()
        alerts["blackout_ping"] = self.blackout_ping_input.text().strip()
        alerts["cap_message"] = self.cap_msg_input.text().strip()
        alerts["bad_message"] = self.bad_msg_input.text().strip() if hasattr(self, "bad_msg_input") else ""
        alerts["hourly_users_report_enabled"] = bool(
            self.hourly_users_report_chk.isChecked()
        ) if hasattr(self, "hourly_users_report_chk") else False
        interval_h = 1
        if hasattr(self, "hourly_users_report_interval_spin"):
            try:
                interval_h = int(self.hourly_users_report_interval_spin.value())
            except Exception:
                interval_h = 1
        alerts["hourly_users_report_interval_hours"] = max(1, min(168, interval_h))
        settings["alerts"] = alerts

        # Backwards compatibility (older builds read these from timeout_monitor)
        tm["webhook_url"] = alerts["webhook_url"]
        tm["ping_message"] = alerts["blackout_ping"]

        # ---------- Webhooks table (tri-mode + legacy-compatible) ----------
        settings["webhooks"] = self._collect_webhooks_from_ui()

        # ---------- UI: Webhooks column visibility ----------
        ui = settings.get("ui", {}) or {}
        if not isinstance(ui, dict):
            ui = {}
        hidden = getattr(self, "_webhooks_hidden_biomes", set()) or set()
        if not isinstance(hidden, (list, tuple, set)):
            hidden = set()
        ui["webhooks_hidden_biomes"] = sorted({str(b).strip().upper() for b in hidden if str(b).strip()})
        if hasattr(self, "ui_show_tutorial_menu_chk"):
            ui["show_tutorial_menu"] = bool(self.ui_show_tutorial_menu_chk.isChecked())
        if hasattr(self, "ui_show_selected_sets_bes_exempt_slot1_chk"):
            ui["show_selected_sets_bes_exempt_slot1"] = bool(
                self.ui_show_selected_sets_bes_exempt_slot1_chk.isChecked()
            )
        settings["ui"] = ui

        # ---------- Optional: Merchant + Pings (safe if widgets exist) ----------
        ms = settings.get("multiscope", {}) or {}
        if hasattr(self, "ms_merchant_webhook_input"):
            ms["merchant_webhook"] = self.ms_merchant_webhook_input.text().strip()

        if hasattr(self, "ms_enable_jester"):
            ms["enable_jester"] = bool(self.ms_enable_jester.isChecked())
        if hasattr(self, "ms_enable_mari"):
            ms["enable_mari"]   = bool(self.ms_enable_mari.isChecked())
        if hasattr(self, "ms_enable_rin"):
            ms["enable_rin"]    = bool(self.ms_enable_rin.isChecked())
        if hasattr(self, "ms_merchant_detection_mode"):
            ms["merchant_detection_mode"] = str(self.ms_merchant_detection_mode.currentData() or "asset_id")

        if hasattr(self, "ms_jester_type"):
            ms["jester_ping_type"] = self.ms_jester_type.currentText()
        if hasattr(self, "ms_jester_id"):
            ms["jester_ping_id"]   = self.ms_jester_id.text().strip()
        if hasattr(self, "ms_mari_type"):
            ms["mari_ping_type"]   = self.ms_mari_type.currentText()
        if hasattr(self, "ms_mari_id"):
            ms["mari_ping_id"]     = self.ms_mari_id.text().strip()
        if hasattr(self, "ms_rin_type"):
            ms["rin_ping_type"]    = self.ms_rin_type.currentText()
        if hasattr(self, "ms_rin_id"):
            ms["rin_ping_id"]      = self.ms_rin_id.text().strip()

        # ---------- Misc ----------
        misc = settings.get("misc", {}) or {}
        if not isinstance(misc, dict):
            misc = {}
        if hasattr(self, "misc_skip_unknown_webhook_chk"):
            misc["skip_webhook_unknown_context"] = bool(self.misc_skip_unknown_webhook_chk.isChecked())
        if hasattr(self, "misc_disable_log_merchants_when_ocr_active_chk"):
            misc[MISC_DISABLE_LOG_MERCHANTS_WHEN_OCR_ACTIVE_KEY] = bool(
                self.misc_disable_log_merchants_when_ocr_active_chk.isChecked()
            )
        if hasattr(self, "misc_log_confirmed_launch_mode_chk"):
            misc["log_confirmed_launch_mode"] = bool(self.misc_log_confirmed_launch_mode_chk.isChecked())
        if hasattr(self, "misc_disable_manager_bad_marking_chk"):
            misc["disable_manager_bad_marking"] = bool(
                self.misc_disable_manager_bad_marking_chk.isChecked()
            )
        if hasattr(self, "misc_msedgewebview2_limiter_enabled_chk"):
            misc["msedgewebview2_limiter_enabled"] = bool(
                self.misc_msedgewebview2_limiter_enabled_chk.isChecked()
            )
        settings["misc"] = misc

        # ---------- OCR settings ----------
        settings["ocr"] = self._get_ocr_settings_from_ui()

        # convenience: build mention strings the engine can use immediately (still optional)
        def _mk_ping(typ: str, ident: str) -> str:
            ident = (ident or "").strip()
            if not ident:
                return ""
            if typ == "User ID":
                return f"<@{ident}>"
            if typ == "Role ID":
                return f"<@&{ident}>"
            return ident  # "None" or raw like "@everyone"

        if ms:
            ms["jester_ping"] = _mk_ping(ms.get("jester_ping_type", "None"), ms.get("jester_ping_id", ""))
            ms["mari_ping"]   = _mk_ping(ms.get("mari_ping_type", "None"),   ms.get("mari_ping_id", ""))
            ms["rin_ping"]    = _mk_ping(ms.get("rin_ping_type", "None"),    ms.get("rin_ping_id", ""))
            settings["multiscope"] = ms

        # ---------- Persist & live-apply ----------
        if self.config_manager.save_settings(settings):
            try:
                ui_cfg = settings.get("ui", {}) or {}
                if isinstance(ui_cfg, dict):
                    self._apply_tutorial_menu_visibility(bool(ui_cfg.get("show_tutorial_menu", False)))
            except Exception:
                pass
            if self.worker_thread and self.worker_thread.isRunning():
                self.worker_thread.apply_new_settings(settings)
            if self.ocr_worker and self.ocr_worker.isRunning():
                ms_cfg = settings.get("multiscope", {}) or {}
                misc_cfg = settings.get("misc", {}) or {}
                if isinstance(ms_cfg, dict) and isinstance(misc_cfg, dict):
                    ms_cfg = dict(ms_cfg)
                    ms_cfg["skip_webhook_unknown_context"] = bool(
                        misc_cfg.get("skip_webhook_unknown_context", False)
                    )
                self.ocr_worker.update_settings(settings.get("ocr", {}), ms_cfg)
            QMessageBox.information(self, "Success", "Settings saved and applied!")
            try:
                self._settings_baseline = self._get_settings_tab_snapshot()
            except Exception:
                self._settings_baseline = None
            return True
        else:
            QMessageBox.critical(self, "Error", "Failed to save settings.")
            return False


    def reset_settings(self):
        """Load the hard-coded defaults from ConfigManager into the UI."""
        defaults = self.config_manager.default_settings          # ← one source of truth
        t        = defaults["timeouts"]                          # short alias

        # ── basic limits ──────────────────────────────────────────
        self.settings_window_limit_input.setValue(defaults["window_limit"])
        try:
            rwg = defaults.get("roblox_window_geometry", {}) or {}
            if not isinstance(rwg, dict):
                rwg = {}
            if hasattr(self, "rbwin_geom_enforce_chk"):
                self.rbwin_geom_enforce_chk.setChecked(bool(rwg.get("enforce_on_launch", False)))
            self._roblox_window_geometry = {
                "x": int(rwg.get("x", 0) or 0),
                "y": int(rwg.get("y", 0) or 0),
                "w": int(rwg.get("w", 0) or 0),
                "h": int(rwg.get("h", 0) or 0),
            }
            self._refresh_rbwin_geom_status()
        except Exception:
            pass

        # ── launch / restart timings ──────────────────────────────
        self.settings_initial_delay_input.setValue(t["initial_delay"])
        self.settings_launch_delay_input.setValue(t["launch_delay"])
        self.settings_offline_threshold_input.setValue(t["offline"])

        # ── helper / strap limiter ────────────────────────────────
        self.settings_strap_threshold_input.setValue(t["strap_threshold"])

        # ── timeout-monitor block (kill / poll / webhook) ─────────
        tm = defaults.get("timeout_monitor", {}) or {}
        if not isinstance(tm, dict):
            tm = {}
        self.kill_timeout_input.setValue(int(tm.get("kill_timeout", t.get("kill_timeout", 1740))))
        self.poll_interval_input.setValue(int(tm.get("poll_interval", t.get("poll_interval", 10))))

        alerts = defaults.get("alerts", {}) or {}
        if not isinstance(alerts, dict):
            alerts = {}
        self.webhook_input.setText(str(alerts.get("webhook_url", t.get("webhook_url", ""))))
        self.blackout_ping_input.setText(
            str(alerts.get("blackout_ping", alerts.get("ping_message", t.get("ping_message", ""))))
        )
        self.cap_msg_input.setText(str(alerts.get("cap_message", "")))
        if hasattr(self, "bad_msg_input"):
            self.bad_msg_input.setText(str(alerts.get("bad_message", "")))
        hourly_enabled = bool(alerts.get("hourly_users_report_enabled", False))
        try:
            hourly_interval = int(alerts.get("hourly_users_report_interval_hours", 1) or 1)
        except Exception:
            hourly_interval = 1
        hourly_interval = max(1, min(168, hourly_interval))
        if hasattr(self, "hourly_users_report_chk"):
            self.hourly_users_report_chk.setChecked(hourly_enabled)
        if hasattr(self, "hourly_users_report_interval_spin"):
            self.hourly_users_report_interval_spin.setValue(hourly_interval)
            self.hourly_users_report_interval_spin.setEnabled(hourly_enabled)
        self._hourly_users_report_last_sent_at = 0.0
        self._hourly_users_report_interval_hours = hourly_interval

        # Reset UI-only settings (column visibility)
        if hasattr(self, "_apply_webhook_biome_column_visibility"):
            try:
                self._apply_webhook_biome_column_visibility([])
            except Exception:
                pass
        try:
            ui_defaults = defaults.get("ui", {}) or {}
            if not isinstance(ui_defaults, dict):
                ui_defaults = {}
            show_tutorial = bool(ui_defaults.get("show_tutorial_menu", False))
            if hasattr(self, "ui_show_tutorial_menu_chk"):
                self.ui_show_tutorial_menu_chk.setChecked(show_tutorial)
            self._apply_tutorial_menu_visibility(show_tutorial)
            show_selected_sets_bes_slot1 = bool(ui_defaults.get("show_selected_sets_bes_exempt_slot1", False))
            if hasattr(self, "ui_show_selected_sets_bes_exempt_slot1_chk"):
                self.ui_show_selected_sets_bes_exempt_slot1_chk.setChecked(show_selected_sets_bes_slot1)
        except Exception:
            pass

        # -- OCR --
        self._apply_ocr_settings_to_ui(defaults.get("ocr", {}))
        if self.ocr_worker and self.ocr_worker.isRunning():
            self._stop_ocr_worker()

        # -- Misc --
        if hasattr(self, "misc_skip_unknown_webhook_chk"):
            self.misc_skip_unknown_webhook_chk.setChecked(
                bool(defaults.get("misc", {}).get("skip_webhook_unknown_context", False))
            )
        if hasattr(self, "misc_disable_log_merchants_when_ocr_active_chk"):
            self.misc_disable_log_merchants_when_ocr_active_chk.setChecked(
                bool(defaults.get("misc", {}).get(MISC_DISABLE_LOG_MERCHANTS_WHEN_OCR_ACTIVE_KEY, True))
            )
        if hasattr(self, "misc_log_confirmed_launch_mode_chk"):
            self.misc_log_confirmed_launch_mode_chk.setChecked(
                bool(defaults.get("misc", {}).get("log_confirmed_launch_mode", False))
            )
        if hasattr(self, "misc_disable_manager_bad_marking_chk"):
            self.misc_disable_manager_bad_marking_chk.setChecked(
                bool(defaults.get("misc", {}).get("disable_manager_bad_marking", False))
            )
            self._sync_misc_disable_bad_marking_warning(
                self.misc_disable_manager_bad_marking_chk.isChecked()
            )
        if hasattr(self, "misc_msedgewebview2_limiter_enabled_chk"):
            self.misc_msedgewebview2_limiter_enabled_chk.setChecked(
                bool(defaults.get("misc", {}).get("msedgewebview2_limiter_enabled", True))
            )

        QMessageBox.information(
            self,
            "Reset Complete",
            "All settings have been restored to their default values.\n"
            "Click “Save Settings” to confirm them."
        )

    def _clear_bad_flags(self):
        users = self.config_manager.load_users()
        for info in users.values():
            if isinstance(info, dict):
                info["bad"] = False
                info["cap"] = False
        self.config_manager.save_users(users)
        QMessageBox.information(self, "Done", "All flags cleared.")
        self.refresh_users()                # live update
        self.refresh_accounts_list()
        self.load_settings_tab()            # if you show counts here

    def _clear_selected_bad_flags(self):
        table = getattr(self, "accounts_list", None)
        user_ids = self._get_selected_user_ids(table)
        if not user_ids:
            QMessageBox.information(self, "Select Accounts", "Select one or more account rows first.")
            return

        users = self.config_manager.load_users()
        changed = 0
        for uid in user_ids:
            info = users.get(uid)
            if not isinstance(info, dict):
                continue
            had_flag = bool(info.get("bad", False) or info.get("cap", False))
            if had_flag:
                info["bad"] = False
                info["cap"] = False
                changed += 1

        if changed == 0:
            QMessageBox.information(self, "No Changes", "No selected accounts have flags set.")
            return

        if self.config_manager.save_users(users):
            QMessageBox.information(self, "Done", f"Cleared flags for {changed} account(s).")
            self.refresh_users()
            self.refresh_accounts_list()
            self.load_settings_tab()
        else:
            err = self.config_manager.get_cookie_error()
            msg = "Failed to save users.json."
            if err:
                msg = msg + "\n\n" + err
            QMessageBox.critical(self, "Error", msg)

    def show_config_location(self):
        config_info = self.config_manager.get_config_info()

        msg = QMessageBox(self)
        msg.setWindowTitle("Configuration Location")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText("JARAM Configuration Files")
        msg.setDetailedText(
            f"Configuration Directory:\n{config_info['config_dir']}\n\n"
            f"Users File:\n{config_info['users_file']}\n\n"
            f"Settings File:\n{config_info['settings_file']}\n\n"
            f"Backups Directory:\n{config_info['backup_dir']}\n\n"
            "All configuration files are automatically backed up before changes."
        )

        open_button = msg.addButton("Open Folder", QMessageBox.ButtonRole.ActionRole)
        msg.addButton(QMessageBox.StandardButton.Ok)

        msg.exec()

        if msg.clickedButton() == open_button:
            try:
                os.startfile(config_info['config_dir'])
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to open folder: {e}")

    def show_about(self):
        config_info = self.config_manager.get_config_info()
        QMessageBox.about(self, "About J.JARAM",
                         "J.JARAM (Jirach1's Just Another Roblox Account Manager) JX 2x51\n\n"
                         "Advanced multi-account Roblox session manager\n"
                         "with automated log based monitoring and process management.\n\n"
                         "Built with PySide6 and modern design principles.\n\n"
                         f"Configuration stored in:\n{config_info['config_dir']}")

    def restart_all_sessions(self):
        if not self.worker_thread or not self.worker_thread.isRunning():
            QMessageBox.warning(self, "Manager Not Running", "Please start the manager first.")
            return

        reply = QMessageBox.question(self, "Confirm Restart",
                                   "Are you sure you want to restart all sessions?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        online_uids: set[str] = set()
        try:
            for uid, row in (getattr(self, "user_data", {}) or {}).items():
                if not isinstance(row, dict):
                    continue
                pids = row.get("pids") or []
                if isinstance(pids, (list, tuple)) and len(pids) > 0:
                    online_uids.add(str(uid))
        except Exception:
            online_uids = set()

        # Fallback: if the dashboard hasn't received a status update yet, use the tracker's
        # view of running PIDs.
        if not online_uids:
            try:
                manager = getattr(self.worker_thread, "manager", None)
                process_mgr = getattr(self.worker_thread, "process_mgr", None)
                tracker = getattr(manager, "process_tracker", None) if manager else None
                if tracker and process_mgr:
                    for uid, pids in (getattr(tracker, "user_processes", {}) or {}).items():
                        try:
                            if any(process_mgr.verify_process_active(pid) for pid in (pids or [])):
                                online_uids.add(str(uid))
                        except Exception:
                            continue
            except Exception:
                pass

        restartables: list[str] = []
        for user_id, state in (self.worker_thread.user_states or {}).items():
            uid = str(user_id)
            if uid not in online_uids:
                continue
            info = (state or {}).get("user_info", {}) if isinstance(state, dict) else {}
            if isinstance(info, dict) and (info.get("bad", False) or info.get("cap", False) or info.get("disabled", False)):
                continue
            restartables.append(uid)

        if not restartables:
            QMessageBox.information(self, "No Online Sessions", "No online sessions were found to restart.")
            return

        try:
            launch_delay_s = float(getattr(getattr(self.worker_thread, "launcher", None), "launch_delay", 0) or 0)
        except Exception:
            launch_delay_s = 0.0

        def delayed_restart():
            for i, user_id in enumerate(restartables):
                delay_ms = int(max(0.0, float(i) * launch_delay_s) * 1000)
                QTimer.singleShot(delay_ms, lambda uid=user_id: self.worker_thread.restart_user_session(uid))

        run_id = int(getattr(self, "_restart_all_run_id", 0) or 0) + 1
        self._restart_all_run_id = run_id

        total_span_s = max(0.0, float(len(restartables) - 1) * float(launch_delay_s))
        self.add_log(
            f"Restarting All Sessions...: {len(restartables)} online sessions "
            f"(delay={launch_delay_s}s, span≈{total_span_s:.1f}s)"
        )
        delayed_restart()

        # "Stop" = the queue finished firing restart_user_session calls (not necessarily fully reconnected).
        def _log_done(_rid=run_id, _n=len(restartables)):
            if int(getattr(self, "_restart_all_run_id", 0) or 0) != _rid:
                return
            self.add_log(f"All Sessions Restarted ({_n} sessions).")

        completion_delay_ms = int(total_span_s * 1000) + 50
        QTimer.singleShot(completion_delay_ms, _log_done)
            
    def kill_all_processes(self):
        reply = QMessageBox.question(self, "Confirm Kill All",
                                   "Are you sure you want to kill ALL Roblox processes?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        # If the manager is running, route through the worker so its tracker stays consistent.
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.kill_all_processes()
            return

        # Manager is stopped: kill RobloxPlayerBeta.exe processes directly.
        try:
            pm = ProcessManager(excluded_pid=0)
            killed = bool(pm.terminate_process(None, None))
        except Exception as e:
            QMessageBox.critical(self, "Kill All Failed", f"Failed to kill Roblox processes: {e}")
            return

        if killed:
            self.add_log("[UI] Killed all Roblox processes (manager stopped).")
        else:
            self.add_log("[UI] No Roblox processes found to kill.")

    def cleanup_processes(self):
        if not self.worker_thread or not self.worker_thread.isRunning():
            QMessageBox.warning(self, "Manager Not Running", "Please start the manager first.")
            return

        self.worker_thread.cleanup_dead_processes()

    def restart_user_session(self, user_id):
        if not self.worker_thread or not self.worker_thread.isRunning():
            QMessageBox.warning(self, "Manager Not Running", "Please start the manager first.")
            return

        self.worker_thread.restart_user_session(user_id)

    # ---------------- Accounts tab helpers ----------------
    def _infer_account_server_type(self, user_info: dict) -> str:
        if not isinstance(user_info, dict):
            return "private"
        raw = str(user_info.get("server_type", "") or "").strip().lower()
        if raw in ("private", "public"):
            return raw
        private_link = str(user_info.get("private_server_link", "") or "").strip()
        place = str(user_info.get("place", "") or user_info.get("place_id", "") or "").strip()
        if private_link:
            return "private"
        if place:
            return "public"
        return "private"

    def _confirm_account_server_warning(self, server_type: str, place_id: str) -> bool:
        if server_type == "public":
            if self.skip_account_public_place_warning:
                return True
        else:
            if self.skip_account_private_link_warning:
                return True

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)

        if server_type == "public":
            box.setWindowTitle("Public Server Mode")
            if place_id:
                text = (
                    "Public server mode is selected.\n\n"
                    f"This account will launch into a public server using Place ID {place_id}."
                )
            else:
                text = (
                    "Public server mode is selected.\n\n"
                    "This account will launch into the Sols RNG public lobby."
                )
        else:
            box.setWindowTitle("No Private Server Link")
            text = (
                "You did not enter a Private Server Link.\n\n"
                "If you continue, the account will launch into a public server "
                "using Place ID (or the Sols RNG public lobby if both missing)."
            )

        box.setText(text)
        box.setInformativeText("Save anyway?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)

        chk = QCheckBox("Don't warn me again")
        box.setCheckBox(chk)

        decision = box.exec() == QMessageBox.StandardButton.Yes
        if decision and chk.isChecked():
            if server_type == "public":
                self.skip_account_public_place_warning = True
            else:
                self.skip_account_private_link_warning = True
        return decision

    def _find_alternate_account(self, users_config: dict, exclude_user_id: Optional[str] = None) -> Optional[Tuple[str, dict]]:
        for uid, info in (users_config or {}).items():
            if exclude_user_id is not None and str(uid) == str(exclude_user_id):
                continue
            if isinstance(info, dict) and bool(info.get("alternate_launch", False)):
                return str(uid), info
        return None

    def _confirm_account_alternate_switch(self, users_config: dict, user_id: str) -> bool:
        existing = self._find_alternate_account(users_config, exclude_user_id=user_id)
        if not existing:
            return True
        prev_uid, prev_info = existing
        prev_name = prev_info.get("username", f"User_{prev_uid}")
        text = (
            f"Alternate launch is already set for {prev_name} ({prev_uid}).\n\n"
            "If you continue, it will be removed from that account."
        )
        reply = QMessageBox.question(
            self,
            "Switch Alternate Account",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _fetch_authenticated_user(self, cookie: str) -> Tuple[Optional[dict], str]:
        cookie = str(cookie or "").strip()
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
        if not cookie:
            return None, cookie
        try:
            session = requests.Session()
            try:
                session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
            except Exception:
                session.cookies.set(".ROBLOSECURITY", cookie)

            response = session.get("https://users.roblox.com/v1/users/authenticated", timeout=8)
            if extract_roblosecurity_from_requests_response is not None:
                try:
                    updated = extract_roblosecurity_from_requests_response(response, session=session)
                except Exception:
                    updated = None
                if updated and updated != cookie:
                    cookie = updated

            if response.status_code != 200:
                return None, cookie
            data = response.json()
            if not isinstance(data, dict):
                return None, cookie
            if "id" not in data or "name" not in data:
                return None, cookie
            return data, cookie
        except Exception:
            return None, cookie

    def extract_account_cookie_from_browser(self):
        try:
            if bool(getattr(self, "account_alternate_launch", None) and self.account_alternate_launch.isChecked()):
                QMessageBox.information(
                    self,
                    "Alternate Launch",
                    "This account is set to Alternate launch mode.\n\nIt does not use cookies, so cookie extraction is disabled.",
                )
                return

            btn = getattr(self, "account_browser_login_btn", None)
            if btn is not None:
                btn.setEnabled(False)
                btn.setText("Extracting...")

            if getattr(self, "cookie_extractor", None) is None:
                self.cookie_extractor = CookieExtractor(self)

            self.cookie_extractor.extract_cookie_async(
                callback=self._on_account_cookie_extraction_complete,
                parent_widget=self,
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start cookie extraction: {str(e)}")
            self._reset_account_browser_button()

    def _on_account_cookie_extraction_complete(self, cookie: Optional[str]):
        try:
            if cookie:
                self.account_cookie.setText(cookie)
                user_info, updated_cookie = self._fetch_authenticated_user(cookie)
                if updated_cookie and updated_cookie != cookie:
                    cookie = updated_cookie
                    self.account_cookie.setText(cookie)
                extra = ""
                if user_info:
                    if hasattr(self, "account_user_id"):
                        self.account_user_id.setText(str(user_info.get("id", "")))
                    if hasattr(self, "account_username"):
                        self.account_username.setText(str(user_info.get("name", "")))
                    extra = f"\n\nUser info filled: {user_info.get('name', '')} ({user_info.get('id', '')})"
                else:
                    extra = "\n\nCould not fetch user info; you can enter it manually."
                QMessageBox.information(
                    self,
                    "Success",
                    "Cookie extracted successfully!\n\n"
                    "The cookie has been automatically filled in the input field."
                    f"{extra}",
                )
            else:
                QMessageBox.information(
                    self,
                    "Extraction Cancelled",
                    "Cookie extraction was cancelled or failed.\n\n"
                    "You can try again or enter the cookie manually.",
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error handling extracted cookie: {str(e)}")
        finally:
            self._reset_account_browser_button()

    def _reset_account_browser_button(self):
        btn = getattr(self, "account_browser_login_btn", None)
        if btn is not None:
            alternate = bool(getattr(self, "account_alternate_launch", None) and self.account_alternate_launch.isChecked())
            btn.setEnabled(not alternate)
            btn.setText("Login with Browser")

    def _on_account_alternate_launch_toggled(self, checked: bool) -> None:
        checked = bool(checked)
        try:
            self.account_cookie.setEnabled(not checked)
            self.account_cookie.setToolTip("Disabled: Alternate launch mode does not use cookies." if checked else "")
        except Exception:
            pass
        try:
            btn = getattr(self, "account_browser_login_btn", None)
            if btn is not None:
                btn.setEnabled(not checked)
                btn.setToolTip("Disabled: Alternate launch mode does not use cookies." if checked else "Open browser to login and automatically extract cookie")
        except Exception:
            pass

    def on_account_server_type_changed(self):
        if self.account_private_radio.isChecked():
            if hasattr(self, "account_private_link_label"):
                self.account_private_link_label.show()
            self.account_private_link.setEnabled(True)
            self.account_private_link.show()
            self.account_place_id_label.hide()
            self.account_place_id.hide()
        else:
            if hasattr(self, "account_private_link_label"):
                self.account_private_link_label.hide()
            self.account_private_link.setEnabled(False)
            self.account_private_link.hide()
            self.account_place_id_label.show()
            self.account_place_id.show()

    def clear_account_form(self):
        self.account_user_id.clear()
        self.account_username.clear()
        self.account_private_link.clear()
        self.account_place_id.clear()
        self.account_cookie.clear()
        self.account_disabled.setChecked(False)
        try:
            self.account_alternate_launch.setChecked(False)
        except Exception:
            pass
        try:
            self.account_skip_reconnect_on_log_disconnect.setChecked(False)
        except Exception:
            pass
        self.account_private_radio.setChecked(True)
        self.on_account_server_type_changed()
        try:
            self._on_account_alternate_launch_toggled(False)
        except Exception:
            pass
        self.account_user_id.setEnabled(True)
        if self.add_account_btn.text() != "Add Account":
            self.add_account_btn.setText("Add Account")
        try:
            self.add_account_btn.clicked.disconnect()
        except Exception:
            pass
        self.add_account_btn.clicked.connect(self.add_account)

    def add_account(self):
        user_id = self.account_user_id.text().strip()
        username = self.account_username.text().strip()
        private_link = self.account_private_link.text().strip()
        place = self.account_place_id.text().strip()
        cookie = self.account_cookie.text().strip()
        disabled = self.account_disabled.isChecked()
        alternate = bool(getattr(self, "account_alternate_launch", None) and self.account_alternate_launch.isChecked())
        skip_reconnect_on_log_disconnect = bool(
            getattr(self, "account_skip_reconnect_on_log_disconnect", None)
            and self.account_skip_reconnect_on_log_disconnect.isChecked()
        )
        server_type = "private" if self.account_private_radio.isChecked() else "public"

        if not user_id:
            QMessageBox.warning(self, "Error", "User ID is required!")
            return
        if not username:
            username = f"User_{user_id}"
        if (not alternate) and (not cookie):
            QMessageBox.warning(self, "Error", "Cookie is required!")
            return
        if server_type == "private" and not private_link:
            if not self._confirm_account_server_warning("private", place):
                self.account_private_link.setFocus()
                return
        if server_type == "public" and not place:
            if not self._confirm_account_server_warning("public", place):
                self.account_place_id.setFocus()
                return

        users_config = self.config_manager.load_users()
        if user_id in users_config:
            QMessageBox.warning(self, "Error", f"User {user_id} already exists!")
            return

        if alternate:
            if not self._confirm_account_alternate_switch(users_config, user_id):
                return
            for uid, info in (users_config or {}).items():
                if not isinstance(info, dict):
                    continue
                info["alternate_launch"] = False
                users_config[uid] = info

        account_data = {
            "username": username,
            "server_type": server_type,
            "private_server_link": private_link if server_type == "private" else "",
            "place": place if server_type == "public" else "",
            "cookie": cookie,
            "disabled": disabled,
            "alternate_launch": alternate,
            "skip_reconnect_on_log_disconnect": skip_reconnect_on_log_disconnect,
            "cap": False,
        }
        users_config[user_id] = account_data
        if self.config_manager.save_users(users_config):
            QMessageBox.information(self, "Success", f"Account {user_id} ({username}) added successfully!")
            self.clear_account_form()
            self.refresh_accounts_list()
            self.refresh_users()
        else:
            QMessageBox.critical(self, "Error", "Failed to save account!")

    def refresh_accounts_list(self):
        users_config = self.config_manager.load_users()
        self.accounts_list.setRowCount(len(users_config))
        for row, (user_id, user_info) in enumerate(users_config.items()):
            if isinstance(user_info, dict):
                username = user_info.get("username", f"User_{user_id}")
                server_type = self._infer_account_server_type(user_info)
                disabled = user_info.get("disabled", False)
                bad_flag = user_info.get("bad", False)
                cap_flag = user_info.get("cap", False)
                is_alternate = bool(user_info.get("alternate_launch", False))
            else:
                username = f"User_{user_id}"
                server_type = "private"
                disabled = False
                bad_flag = False
                cap_flag = False
                is_alternate = False

            self.accounts_list.setItem(row, 0, QTableWidgetItem(user_id))
            username_item = QTableWidgetItem(username)
            if is_alternate:
                username_item.setForeground(QColor("#f1c40f"))
            self.accounts_list.setItem(row, 1, username_item)
            self.accounts_list.setItem(row, 2, QTableWidgetItem(server_type.title()))

            status_text = "Disabled" if disabled else "Enabled"
            flags = []
            if bad_flag:
                flags.append("bad")
            if cap_flag:
                flags.append("CAP")
            if flags:
                status_text = f"{status_text} ({'/'.join(flags)})"
            status_item = QTableWidgetItem(status_text)
            if disabled:
                status_item.setForeground(QColor("#FF6666"))
            elif bad_flag or cap_flag:
                status_item.setForeground(QColor(ModernStyle.ERROR))
            else:
                status_item.setForeground(QColor("#66FF66"))
            self.accounts_list.setItem(row, 3, status_item)

            edit_btn = QPushButton("Edit")
            edit_btn.setStyleSheet(
                "QPushButton {background-color:%s; color:white; border:none; padding:2px 4px; border-radius:3px; font-size:8px; font-weight:bold; min-width:50px; max-width:80px; min-height:18px; max-height:22px;} QPushButton:hover {background-color:%s;}"
                % (ModernStyle.PRIMARY, ModernStyle.PRIMARY_VARIANT)
            )
            edit_btn.clicked.connect(lambda _, uid=user_id: self.edit_account(uid))
            self.accounts_list.setCellWidget(row, 4, edit_btn)

            delete_btn = QPushButton("Del")
            delete_btn.setStyleSheet(
                "QPushButton {background-color:#f44336; color:white; border:none; padding:2px 4px; border-radius:3px; font-size:8px; font-weight:bold; min-width:40px; max-width:70px; min-height:18px; max-height:22px;} QPushButton:hover {background-color:#da190b;}"
            )
            delete_btn.clicked.connect(lambda _, uid=user_id: self.delete_account(uid))
            self.accounts_list.setCellWidget(row, 5, delete_btn)

    def edit_account(self, user_id):
        users_config = self.config_manager.load_users()
        user_info = users_config.get(user_id, {})
        if not isinstance(user_info, dict):
            return
        self.account_user_id.setText(user_id)
        self.account_user_id.setEnabled(False)
        self.account_username.setText(user_info.get("username", f"User_{user_id}"))
        self.account_private_link.setText(user_info.get("private_server_link", ""))
        place = user_info.get("place", "") or user_info.get("place_id", "")
        self.account_place_id.setText(place)
        self.account_cookie.setText(user_info.get("cookie", ""))
        self.account_disabled.setChecked(user_info.get("disabled", False))
        try:
            self.account_alternate_launch.setChecked(bool(user_info.get("alternate_launch", False)))
        except Exception:
            pass
        try:
            self.account_skip_reconnect_on_log_disconnect.setChecked(
                bool(user_info.get("skip_reconnect_on_log_disconnect", False))
            )
        except Exception:
            pass
        server_type = self._infer_account_server_type(user_info)
        if server_type == "public":
            self.account_public_radio.setChecked(True)
        else:
            self.account_private_radio.setChecked(True)
        self.on_account_server_type_changed()
        try:
            self._on_account_alternate_launch_toggled(bool(getattr(self, "account_alternate_launch", None) and self.account_alternate_launch.isChecked()))
        except Exception:
            pass
        self.add_account_btn.setText("Update Account")
        try:
            self.add_account_btn.clicked.disconnect()
        except Exception:
            pass
        self.add_account_btn.clicked.connect(lambda: self.update_account(user_id))

    def update_account(self, user_id):
        if self.config_manager.cookie_encryption_enabled() and not self.config_manager.is_cookie_unlocked():
            QMessageBox.warning(
                self,
                "Cookies Locked",
                "Cookie encryption is enabled and cookies are currently locked.\n\n"
                "Accounts can't be updated until cookies are unlocked.\n\n"
                "Use File -> Cookie Encryption... -> Unlock Cookies.",
            )
            return

        username = self.account_username.text().strip() or f"User_{user_id}"
        private_link = self.account_private_link.text().strip()
        place = self.account_place_id.text().strip()
        cookie = self.account_cookie.text().strip()
        disabled = self.account_disabled.isChecked()
        alternate = bool(getattr(self, "account_alternate_launch", None) and self.account_alternate_launch.isChecked())
        skip_reconnect_on_log_disconnect = bool(
            getattr(self, "account_skip_reconnect_on_log_disconnect", None)
            and self.account_skip_reconnect_on_log_disconnect.isChecked()
        )
        server_type = "private" if self.account_private_radio.isChecked() else "public"

        if (not alternate) and (not cookie):
            QMessageBox.warning(self, "Error", "Cookie is required!")
            return
        if server_type == "private" and not private_link:
            if not self._confirm_account_server_warning("private", place):
                self.account_private_link.setFocus()
                return
        if server_type == "public" and not place:
            if not self._confirm_account_server_warning("public", place):
                self.account_place_id.setFocus()
                return

        users_config = self.config_manager.load_users()
        existing = users_config.get(user_id, {})
        if not isinstance(existing, dict):
            existing = {}

        existing_cookie = str(existing.get("cookie", "") or "")
        cookie_changed = str(cookie or "") != existing_cookie
        bad_flag = bool(existing.get("bad", False))
        if cookie_changed and bad_flag:
            bad_flag = False

        if alternate:
            if not self._confirm_account_alternate_switch(users_config, user_id):
                return
            for uid, info in (users_config or {}).items():
                if str(uid) == str(user_id):
                    continue
                if not isinstance(info, dict):
                    continue
                info["alternate_launch"] = False
                users_config[uid] = info

        account_data = {
            "username": username,
            "server_type": server_type,
            "private_server_link": private_link if server_type == "private" else "",
            "place": place if server_type == "public" else "",
            "cookie": cookie,
            "disabled": disabled,
            "bad": bad_flag,
            "cap": existing.get("cap", False),
            "alternate_launch": alternate,
            "skip_reconnect_on_log_disconnect": skip_reconnect_on_log_disconnect,
        }
        users_config[user_id] = account_data
        if self.config_manager.save_users(users_config):
            QMessageBox.information(self, "Success", f"Account {user_id} ({username}) updated successfully!")
            self.clear_account_form()
            self.account_user_id.setEnabled(True)
            self.add_account_btn.setText("Add Account")
            try:
                self.add_account_btn.clicked.disconnect()
            except Exception:
                pass
            self.add_account_btn.clicked.connect(self.add_account)
            self.refresh_accounts_list()
            self.refresh_users()
        else:
            err = self.config_manager.get_cookie_error()
            QMessageBox.critical(self, "Error", err or "Failed to update account!")

    def delete_account(self, user_id):
        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Are you sure you want to delete account {user_id}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        users_config = self.config_manager.load_users()
        if user_id in users_config:
            users_config.pop(user_id, None)
            if self.config_manager.save_users(users_config):
                QMessageBox.information(self, "Success", f"Account {user_id} deleted successfully!")
                self.refresh_accounts_list()
                self.refresh_users()
            else:
                QMessageBox.critical(self, "Error", "Failed to delete account!")

    def toggle_user_enabled(self, user_id):
        """Toggle the disabled flag for an account and refresh UI."""
        try:
            users_config = self.config_manager.load_users()
            if user_id not in users_config:
                QMessageBox.warning(self, "Error", f"Account {user_id} not found.")
                return False
            info = users_config[user_id]
            if not isinstance(info, dict):
                QMessageBox.warning(self, "Error", "Cannot modify legacy account format. Please update account configuration.")
                return False
            info["disabled"] = not info.get("disabled", False)
            if self.config_manager.save_users(users_config):
                if info["disabled"] and self.worker_thread and self.worker_thread.isRunning():
                    self.worker_thread.kill_user_processes(user_id)
                self.refresh_users()
                status = "disabled" if info["disabled"] else "enabled"
                QMessageBox.information(self, "Account Status Changed", f"Account {user_id} has been {status}.")
                return True
            QMessageBox.critical(self, "Error", "Failed to save account configuration.")
            return False
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to toggle account status: {e}")
            return False

    def toggle_user_enabled(self, user_id):
        try:
            users_config = self.config_manager.load_users()
            if user_id not in users_config:
                QMessageBox.warning(self, "Error", f"Account {user_id} not found.")
                return False
            user_info = users_config[user_id]
            if not isinstance(user_info, dict):
                QMessageBox.warning(self, "Error", "Cannot modify legacy account format. Please update account configuration.")
                return False
            user_info["disabled"] = not user_info.get("disabled", False)
            if self.config_manager.save_users(users_config):
                if user_info["disabled"] and self.worker_thread and self.worker_thread.isRunning():
                    self.worker_thread.kill_user_processes(user_id)
                self.refresh_users()
                status = "disabled" if user_info["disabled"] else "enabled"
                QMessageBox.information(self, "Account Status Changed", f"Account {user_id} has been {status}.")
                return True
            QMessageBox.critical(self, "Error", "Failed to save account configuration.")
            return False
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to toggle account status: {e}")
            return False

    def _get_selected_user_ids(self, table=None) -> List[str]:
        if table is None:
            table = getattr(self, "users_table", None)
        if table is None:
            return []
        selection = table.selectionModel()
        rows = selection.selectedRows() if selection else []
        if not rows:
            row = int(table.currentRow())
            if row >= 0:
                rows = [table.model().index(row, 0)]
        ordered: List[Tuple[int, str]] = []
        for idx in rows:
            item = table.item(idx.row(), 0)
            if item is None:
                continue
            uid = item.text().strip()
            if uid:
                ordered.append((idx.row(), uid))
        ordered.sort(key=lambda x: x[0])
        seen = set()
        result: List[str] = []
        for _row, uid in ordered:
            if uid in seen:
                continue
            seen.add(uid)
            result.append(uid)
        return result

    def _selection_label_for_table(self, table) -> str:
        if table is getattr(self, "accounts_list", None):
            return "Accounts"
        return "Users"

    def disable_selected_users(self, table=None):
        user_ids = self._get_selected_user_ids(table)
        if not user_ids:
            label = self._selection_label_for_table(table)
            QMessageBox.information(self, f"Select {label}", f"Select one or more {label.lower()} rows first.")
            return
        users_config = self.config_manager.load_users()
        changed = 0
        disabled_now: List[str] = []
        for uid in user_ids:
            info = users_config.get(uid)
            if not isinstance(info, dict):
                continue
            if not info.get("disabled", False):
                info["disabled"] = True
                changed += 1
                disabled_now.append(uid)
        if changed == 0:
            QMessageBox.information(self, "No Changes", "Selected users are already disabled.")
            return
        if self.config_manager.save_users(users_config):
            if disabled_now and self.worker_thread and self.worker_thread.isRunning():
                for uid in disabled_now:
                    self.worker_thread.kill_user_processes(uid)
            self.refresh_users()
            self.refresh_accounts_list()
            QMessageBox.information(self, "Accounts Disabled", f"Disabled {changed} account(s).")
        else:
            QMessageBox.critical(self, "Error", "Failed to save account configuration.")

    def toggle_selected_users_status(self, table=None):
        user_ids = self._get_selected_user_ids(table)
        if not user_ids:
            label = self._selection_label_for_table(table)
            QMessageBox.information(self, f"Select {label}", f"Select one or more {label.lower()} rows first.")
            return
        users_config = self.config_manager.load_users()
        changed = 0
        disabled_now: List[str] = []
        enabled_now = 0
        for uid in user_ids:
            info = users_config.get(uid)
            if not isinstance(info, dict):
                continue
            info["disabled"] = not info.get("disabled", False)
            changed += 1
            if info["disabled"]:
                disabled_now.append(uid)
            else:
                enabled_now += 1
        if changed == 0:
            QMessageBox.information(self, "No Changes", "No selected users could be updated.")
            return
        if self.config_manager.save_users(users_config):
            if disabled_now and self.worker_thread and self.worker_thread.isRunning():
                for uid in disabled_now:
                    self.worker_thread.kill_user_processes(uid)
            self.refresh_users()
            self.refresh_accounts_list()
            QMessageBox.information(
                self,
                "Status Toggled",
                f"Toggled {changed} account(s): {len(disabled_now)} disabled, {enabled_now} enabled.",
            )
        else:
            QMessageBox.critical(self, "Error", "Failed to save account configuration.")

    def _bring_hwnd_to_foreground(self, hwnd: int) -> bool:
        try:
            import ctypes
            import win32api as _wapi
            import win32con as _wcon
            import win32gui as _wgui
            import win32process as _wproc
        except Exception:
            return False

        try:
            if not hwnd or not _wgui.IsWindow(hwnd):
                return False
            try:
                if _wgui.IsIconic(hwnd):
                    _wgui.ShowWindow(hwnd, _wcon.SW_RESTORE)
            except Exception:
                pass

            cur_tid = None
            win_tid = None
            try:
                cur_tid = _wapi.GetCurrentThreadId()
                win_tid = _wproc.GetWindowThreadProcessId(hwnd)[0]
                ctypes.windll.user32.AttachThreadInput(int(cur_tid), int(win_tid), True)
                _wgui.BringWindowToTop(hwnd)
                _wgui.SetForegroundWindow(hwnd)
            finally:
                try:
                    if cur_tid is not None and win_tid is not None:
                        ctypes.windll.user32.AttachThreadInput(int(cur_tid), int(win_tid), False)
                except Exception:
                    pass
            return True
        except Exception:
            try:
                _wgui.SetForegroundWindow(hwnd)
                return True
            except Exception:
                return False

    def _find_roblox_hwnd_for_user(self, user_id: str) -> Optional[int]:
        uid = str(user_id or "").strip()
        if not uid:
            return None

        runtime = (self.user_data or {}).get(uid, None)
        if runtime is None:
            try:
                runtime = (self.user_data or {}).get(int(uid), None)  # type: ignore[arg-type]
            except Exception:
                runtime = None
        runtime = runtime or {}

        pids = runtime.get("pids", []) or []
        if not isinstance(pids, (list, tuple)):
            pids = [pids]
        pid_list: List[int] = []
        for pid in pids:
            try:
                pid_i = int(pid)
            except Exception:
                continue
            if pid_i > 0:
                pid_list.append(pid_i)
        if not pid_list:
            return None

        try:
            wins = enum_roblox_windows()
        except Exception:
            return None

        pid_to_hwnd: Dict[int, int] = {}
        for w in wins or []:
            try:
                pid_to_hwnd[int(w.pid)] = int(w.hwnd)
            except Exception:
                continue

        for pid_i in pid_list:
            hwnd = pid_to_hwnd.get(pid_i)
            if hwnd:
                return hwnd
        return None

    def _ui_show_selected_sets_bes_exempt_slot1_enabled(self) -> bool:
        try:
            chk = getattr(self, "ui_show_selected_sets_bes_exempt_slot1_chk", None)
            if chk is not None:
                return bool(chk.isChecked())
        except Exception:
            pass

        try:
            settings = self.config_manager.peek_settings() or {}
            ui = settings.get("ui", {}) or {}
            defaults = self.config_manager.default_settings.get("ui", {}) or {}
            if not isinstance(ui, dict) or not isinstance(defaults, dict):
                return False
            return bool(
                ui.get(
                    "show_selected_sets_bes_exempt_slot1",
                    defaults.get("show_selected_sets_bes_exempt_slot1", False),
                )
            )
        except Exception:
            return False

    def _set_bes_exempt_slot1_to_user(self, user_id: str) -> None:
        uid = str(user_id or "").strip()
        if not uid:
            return

        combos = getattr(self, "bes_exempt_combos", None) or []
        if combos:
            combo = combos[0]
            try:
                current = str(combo.currentData() or "").strip()
            except Exception:
                current = ""
            if current == uid:
                return

            try:
                if combo.findData(uid) < 0:
                    combo.insertItem(1, f"Unknown ({uid})", uid)
                combo.setCurrentIndex(max(0, combo.findData(uid)))
            except Exception:
                pass

            # Make sure cache + debounced persistence run even if signals are blocked.
            try:
                self._on_bes_ui_changed()
            except Exception:
                pass
            return

        # Fallback when the BES tab isn't available: write config directly.
        try:
            settings = self.config_manager.load_settings() or {}
        except Exception:
            settings = dict(self.config_manager.default_settings or {})

        bes = settings.get("bes", {}) or {}
        if not isinstance(bes, dict):
            bes = {}

        exempt = bes.get("exempt_users", ["", "", ""]) or ["", "", ""]
        if not isinstance(exempt, list):
            exempt = ["", "", ""]
        while len(exempt) < 3:
            exempt.append("")
        exempt = exempt[:3]

        if str(exempt[0] or "").strip() == uid:
            return

        exempt[0] = uid
        bes["exempt_users"] = exempt
        settings["bes"] = bes
        if self.config_manager.save_settings(settings):
            try:
                with self._bes_cfg_lock:
                    cfg = dict(self._bes_cfg_cache or {})
                    cfg["exempt_users"] = list(exempt)
                    self._bes_cfg_cache = cfg
            except Exception:
                pass

    def show_selected_user_window(self) -> None:
        table = getattr(self, "users_table", None)
        if table is None:
            return

        row = int(table.currentRow())
        if row < 0:
            QMessageBox.information(self, "Show Selected", "Select a user row first.")
            return

        uid_item = table.item(row, 0)
        uid = str(uid_item.text() or "").strip() if uid_item is not None else ""
        if not uid:
            QMessageBox.information(self, "Show Selected", "Select a user row first.")
            return

        if self._ui_show_selected_sets_bes_exempt_slot1_enabled():
            self._set_bes_exempt_slot1_to_user(uid)

        hwnd = self._find_roblox_hwnd_for_user(uid)
        if hwnd is None:
            QMessageBox.information(self, "Show Selected", f"No Roblox window found for user {uid}.")
            return
        if not self._bring_hwnd_to_foreground(hwnd):
            QMessageBox.information(self, "Show Selected", f"Failed to focus the Roblox window for user {uid}.")

    def kill_selected_user(self):
        table = getattr(self, "users_table", None)
        if table is None:
            return

        row = int(table.currentRow())
        if row < 0:
            QMessageBox.information(self, "Select a User", "Select a user row first.")
            return

        uid_item = table.item(row, 0)
        if uid_item is None:
            QMessageBox.information(self, "Select a User", "Select a user row first.")
            return

        uid = str(uid_item.text() or "").strip()
        if not uid:
            QMessageBox.information(self, "Select a User", "Select a user row first.")
            return

        self.kill_user_processes(uid)

    def kill_user_processes(self, user_id):
        if not self.worker_thread or not self.worker_thread.isRunning():
            QMessageBox.warning(self, "Manager Not Running", "Please start the manager first.")
            return

        reply = QMessageBox.question(self, "Confirm Kill",
                                   f"Are you sure you want to kill processes for user {user_id}?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.worker_thread.kill_user_processes(user_id)

    def closeEvent(self, event):
        if self.worker_thread and self.worker_thread.isRunning():
            reply = QMessageBox.question(self, "Confirm Exit",
                                       "The manager is still running. Do you want to stop it and exit?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.stop_manager()
                if self.ocr_worker and self.ocr_worker.isRunning():
                    self._stop_ocr_worker_with_timeout(timeout_ms=10000)
                if getattr(self, "antiafk", None):
                    try:
                        self.antiafk.shutdown()
                    except Exception:
                        pass
                if getattr(self, "auto_item_engine", None):
                    try:
                        self.auto_item_engine.stop()
                    except Exception:
                        pass
                if getattr(self, "bes_controller", None):
                    try:
                        self.bes_controller.shutdown()
                    except Exception:
                        pass
                if getattr(self, "trimmer_tab", None):
                    try:
                        self.trimmer_tab.shutdown()
                    except Exception:
                        pass
                try:
                    self._unregister_auto_item_hotkey()
                except Exception:
                    pass
                try:
                    QThreadPool.globalInstance().waitForDone(2000)
                except Exception:
                    pass
                try:
                    self._terminate_multiprocessing_children(timeout_s=2.0)
                except Exception:
                    pass
                event.accept()
            else:
                event.ignore()
        else:
            if self.ocr_worker and self.ocr_worker.isRunning():
                self._stop_ocr_worker_with_timeout(timeout_ms=10000)
            if getattr(self, "antiafk", None):
                try:
                    self.antiafk.shutdown()
                except Exception:
                    pass
            if getattr(self, "auto_item_engine", None):
                try:
                    self.auto_item_engine.stop()
                except Exception:
                    pass
            if getattr(self, "bes_controller", None):
                try:
                    self.bes_controller.shutdown()
                except Exception:
                    pass
            if getattr(self, "trimmer_tab", None):
                try:
                    self.trimmer_tab.shutdown()
                except Exception:
                    pass
            try:
                self._unregister_auto_item_hotkey()
            except Exception:
                pass
            try:
                QThreadPool.globalInstance().waitForDone(2000)
            except Exception:
                pass
            try:
                self._terminate_multiprocessing_children(timeout_s=2.0)
            except Exception:
                pass
            event.accept()
    
    def setup_multiscope_tab(self):
        multiscope_widget = QWidget()
        layout = QVBoxLayout(multiscope_widget)

        self.multiscope_table = QTableWidget()
        self.multiscope_table.setColumnCount(9)
        self.multiscope_table.setHorizontalHeaderLabels([
            "Server", "Users", "Username", "In-Menu", "Last Biome", "Biome Age", "Last Merchant", "Merchant Age", "Events"
        ])
        header = self.multiscope_table.horizontalHeader()
        # NOTE: ResizeToContents becomes expensive with frequent updates; prefer fixed/interactive sizing.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Fixed)
        self.multiscope_table.setColumnWidth(0, 160)
        self.multiscope_table.setColumnWidth(3, 80)
        self.multiscope_table.setColumnWidth(4, 140)
        self.multiscope_table.setColumnWidth(5, 90)
        self.multiscope_table.setColumnWidth(6, 140)
        self.multiscope_table.setColumnWidth(7, 110)
        self.multiscope_table.setColumnWidth(8, 80)
        self.multiscope_table.verticalHeader().setDefaultSectionSize(44)
        self.multiscope_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.multiscope_table.setWordWrap(False)
        try:
            self.multiscope_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        except Exception:
            pass

        layout.addWidget(self.multiscope_table)

        hint = QLabel(
            "Multiscope groups accounts by the exact server they’re in.\n"
            "Biome and merchant alerts persist across handoffs."
        )
        hint.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(hint)

        self.multiscope_tab_index = self.tab_widget.addTab(multiscope_widget, "Multiscope")

    def _schedule_multiscope_table_refresh(self, *, immediate: bool = False) -> None:
        self._multiscope_table_dirty = True
        if self._multiscope_table_refresh_pending:
            return
        try:
            if self.multiscope_tab_index is not None and self.tab_widget.currentIndex() != self.multiscope_tab_index:
                return
        except Exception:
            return
        self._multiscope_table_refresh_pending = True
        QTimer.singleShot(0 if immediate else 150, self._flush_multiscope_table_refresh)

    def _flush_multiscope_table_refresh(self) -> None:
        self._multiscope_table_refresh_pending = False
        if not self._multiscope_table_dirty:
            return
        try:
            if self.multiscope_tab_index is not None and self.tab_widget.currentIndex() != self.multiscope_tab_index:
                return
        except Exception:
            return

        rows = self._multiscope_table_latest_rows or []
        self._multiscope_table_dirty = False
        self._render_multiscope_table(rows)

    def _set_multiscope_table_item(self, row: int, col: int, text: str) -> None:
        table = getattr(self, "multiscope_table", None)
        if table is None:
            return
        try:
            item = table.item(row, col)
            s = str(text)
            if item is None:
                table.setItem(row, col, QTableWidgetItem(s))
            elif item.text() != s:
                item.setText(s)
        except Exception:
            return

    def _render_multiscope_table(self, rows: list) -> None:
        table = getattr(self, "multiscope_table", None)
        if table is None:
            return

        try:
            table.setUpdatesEnabled(False)
        except Exception:
            pass

        try:
            try:
                if int(table.rowCount()) != len(rows):
                    table.setRowCount(len(rows))
            except Exception:
                table.setRowCount(len(rows))

            settings = {}
            try:
                manager = getattr(self.worker_thread, "manager", None)
                if manager and isinstance(getattr(manager, "settings", None), dict):
                    settings = manager.settings
            except Exception:
                settings = {}

            for r, row in enumerate(rows or []):
                if not isinstance(row, dict):
                    continue
                server = row.get("server", "")
                users_list = row.get("users", []) or []
                if not isinstance(users_list, (list, tuple)):
                    users_list = [users_list]
                users = ", ".join(map(str, users_list)) if users_list else ""

                usernames_list = []
                for uid in (users_list or []):
                    info = settings.get(str(uid), {}) if isinstance(settings, dict) else {}
                    uname = str(info.get("username", "") or "").strip() if isinstance(info, dict) else ""
                    usernames_list.append(uname or str(uid))
                usernames = ", ".join(usernames_list) if usernames_list else ""

                in_menu_val = row.get("in_menu")
                if in_menu_val is None:
                    in_menu_txt = "None"
                else:
                    in_menu_txt = "True" if in_menu_val else "False"

                # accept both key styles
                last_biome = row.get("last_biome", row.get("biome", ""))
                biome_age = row.get("biome_age")
                last_merchant = row.get("last_merchant", row.get("merchant", ""))
                merchant_age = row.get("merchant_age")
                events = str(row.get("events", 0))

                self._set_multiscope_table_item(r, 0, server)
                self._set_multiscope_table_item(r, 1, users)
                self._set_multiscope_table_item(r, 2, usernames)
                self._set_multiscope_table_item(r, 3, in_menu_txt)
                self._set_multiscope_table_item(r, 4, last_biome)
                self._set_multiscope_table_item(r, 5, f"{biome_age}s" if biome_age is not None else "")
                self._set_multiscope_table_item(r, 6, last_merchant)
                self._set_multiscope_table_item(r, 7, f"{merchant_age}s" if merchant_age is not None else "")
                self._set_multiscope_table_item(r, 8, events)

        finally:
            try:
                table.setUpdatesEnabled(True)
            except Exception:
                pass

    def update_multiscope(self, rows: list):
        # rows: [{server, users, in_menu, last_biome|biome, biome_age, last_merchant|merchant, merchant_age, events}]
        try:
            grace_until = float(getattr(self, "_ms_resume_grace_until", 0.0) or 0.0)
            if self._last_multiscope_rows and time.time() < grace_until:
                has_signal = False
                try:
                    for r in (rows or []):
                        if not isinstance(r, dict):
                            continue
                        if r.get("in_menu", None) is not None:
                            has_signal = True
                            break
                        b = r.get("last_biome", r.get("biome", ""))
                        if str(b or "").strip():
                            has_signal = True
                            break
                        m = r.get("last_merchant", r.get("merchant", ""))
                        if str(m or "").strip():
                            has_signal = True
                            break
                        try:
                            if int(r.get("events", 0) or 0) > 0:
                                has_signal = True
                                break
                        except Exception:
                            pass
                except Exception:
                    has_signal = False

                if (not rows) or (not has_signal):
                    return
        except Exception:
            pass

        try:
            if isinstance(rows, list):
                self._last_multiscope_rows = list(rows)
        except Exception:
            pass

        try:
            with self._ms_biome_lock:
                self._ms_biome_by_server = {}
                self._ms_in_menu_by_server = {}
                self._ms_biome_by_uid = {}
                self._ms_in_menu_by_uid = {}
                for row in (rows or []):
                    server_label = str(row.get("server", "") or "").strip()
                    server_key = str(row.get("server_key", server_label) or "").strip()
                    biome = str(row.get("last_biome", row.get("biome", "")) or "").strip().upper()
                    if server_key:
                        self._ms_biome_by_server[server_key] = biome
                        val = row.get("in_menu", None)
                        self._ms_in_menu_by_server[server_key] = None if val is None else bool(val)
                    users_list = row.get("users", []) or []
                    val = row.get("in_menu", None)
                    in_menu_val = None if val is None else bool(val)
                    for uid in users_list:
                        uid_str = str(uid).strip()
                        if not uid_str:
                            continue
                        self._ms_biome_by_uid[uid_str] = biome
                        self._ms_in_menu_by_uid[uid_str] = in_menu_val
        except Exception:
            pass

        try:
            if isinstance(rows, list):
                self._multiscope_table_latest_rows = list(rows)
            else:
                self._multiscope_table_latest_rows = []
            self._schedule_multiscope_table_refresh()
        except Exception:
            pass

    def test_selected_webhook(self):
        try:
            import requests
        except Exception:
            QMessageBox.warning(self, "Missing dependency", "The 'requests' library is required to send test webhooks.")
            return

        table = getattr(self, "webhooks_table", None)
        if table is None:
            QMessageBox.warning(self, "Webhooks", "Webhook table is not available.")
            return

        rows = sorted({idx.row() for idx in table.selectedIndexes()})
        if not rows:
            current = int(table.currentRow())
            if current >= 0:
                rows = [current]
        if not rows:
            QMessageBox.information(self, "Select Webhook", "Select a webhook row to test.")
            return
        if len(rows) > 1:
            QMessageBox.information(self, "Select Webhook", "Select a single webhook row to test.")
            return

        row = rows[0]
        name_item = table.item(row, 0)
        url_item = table.item(row, 1)
        name = name_item.text().strip() if name_item else ""
        url = url_item.text().strip() if url_item else ""
        if not url:
            QMessageBox.warning(self, "Missing Webhook", "Selected webhook row has no URL.")
            return

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        title = name or f"Webhook {row + 1}"
        payload = {
            "content": "",
            "embeds": [{
                "title": "Webhook Test",
                "description": f"Test message for {title} webhook.",
                "timestamp": now_iso,
                "color": 0x3498DB
            }]
        }

        try:
            resp = requests.post(url, json=payload, timeout=8)
        except Exception as exc:
            QMessageBox.critical(self, "Webhook Test Failed", f"Failed to send test message: {exc}")
            return

        if resp.ok:
            QMessageBox.information(self, "Webhook Test", f"Sent test message to {title}.")
        else:
            QMessageBox.warning(self, "Webhook Test Failed", f"Status {resp.status_code} from {title}.")

    def test_merchant_pings(self):
        try:
            import requests
        except Exception:
            QMessageBox.warning(self, "Missing dependency", "The 'requests' library is required to send test webhooks.")
            return

        if not hasattr(self, "ms_merchant_webhook_input"):
            QMessageBox.warning(self, "Merchant Pings", "Merchant webhook input is not available.")
            return

        merchant_hook = self.ms_merchant_webhook_input.text().strip()
        if not merchant_hook:
            QMessageBox.information(self, "Merchant Pings", "Enter a Merchant Webhook URL first.")
            return

        ms_cfg = self._ms_settings_from_ui()
        tests = []
        if ms_cfg.get("enable_jester", True):
            tests.append(("Jester", ms_cfg.get("jester_ping", ""), 0xA352FF))
        if ms_cfg.get("enable_mari", True):
            tests.append(("Mari", ms_cfg.get("mari_ping", ""), 0xFF82AB))
        if ms_cfg.get("enable_rin", True):
            tests.append(("Rin", ms_cfg.get("rin_ping", ""), 0xFF9F1C))
        if not tests:
            QMessageBox.information(self, "Merchant Pings", "Enable Jester, Mari, or Rin pings to send a test.")
            return

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ok = 0
        fail = 0
        for name, ping, color in tests:
            desc = f"This is a test {name} merchant ping from Settings."
            payload = {
                "content": ping or "",
                "embeds": [{
                    "title": f"Merchant Test - {name}",
                    "description": desc,
                    "timestamp": now_iso,
                    "color": color
                }]
            }
            try:
                resp = requests.post(merchant_hook, json=payload, timeout=8)
                if resp.ok:
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1

        QMessageBox.information(self, "Merchant Ping Test", f"Sent: {ok}  Failed: {fail}")

    def test_multiscope_webhooks(self):
        # Legacy entry point kept for compatibility.
        self.test_merchant_pings()
        return
        try:
            import requests
        except Exception:
            QMessageBox.warning(self, "Missing dependency", "The 'requests' library is required to send test webhooks.")
            return

        # Build fake embeds
        biome_hooks_txt = self.ms_biome_webhooks_input.text().strip()
        biome_hooks = [s.strip() for s in biome_hooks_txt.split(",") if s.strip()] if biome_hooks_txt else []
        merchant_hook = self.ms_merchant_webhook_input.text().strip()
        j_ping = self.ms_jester_ping_input.text().strip()
        m_ping = self.ms_mari_ping_input.text().strip()

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        biome_payload = {"content": "", "embeds": [{
            "title": "🌍 Biome — TEST_BIOME",
            "description": "**User:** `TestUser`\n**Server:** `PS:TEST…`",
            "timestamp": now_iso, "color": 0x3BA55D
        }]}

        merch_payload_j = {"content": j_ping, "embeds": [{
            "title": "🛒 Merchant — Jester",
            "description": "**User:** `TestUser`\n**Server:** `PS:TEST…`",
            "timestamp": now_iso, "color": 0x5865F2
        }]}
        merch_payload_m = {"content": m_ping, "embeds": [{
            "title": "🛒 Merchant — Mari",
            "description": "**User:** `TestUser`\n**Server:** `PS:TEST…`",
            "timestamp": now_iso, "color": 0xE67E22
        }]}

        ok = 0; fail = 0
        try:
            for url in biome_hooks:
                if not url: continue
                r = requests.post(url, json=biome_payload, timeout=8)
                (ok if r.ok else fail).__iadd__(1)
            if merchant_hook:
                r1 = requests.post(merchant_hook, json=merch_payload_j, timeout=8); (ok if r1.ok else fail).__iadd__(1)
                r2 = requests.post(merchant_hook, json=merch_payload_m, timeout=8); (ok if r2.ok else fail).__iadd__(1)
        except Exception:
            fail += 1

        QMessageBox.information(self, "Webhook Test", f"Sent: {ok}  •  Failed: {fail}")

def main():
    # Silence noisy Windows DPI-awareness warning:
    # "qt.qpa.window: SetProcessDpiAwarenessContext() failed: Access is denied."
    # This happens when DPI awareness was already set (e.g., manifest / earlier call).
    try:
        from PySide6.QtCore import QLoggingCategory

        QLoggingCategory.setFilterRules("qt.qpa.window.warning=false")
    except Exception:
        pass

    app = QApplication(sys.argv)

    app.setApplicationName("J.JARAM")
    app.setApplicationVersion("JX 2x51")
    app.setOrganizationName("Jirach1")
    # Qt 6 ships a Windows 11 style that can change widget visuals. Force the
    # Windows 10-era style for consistent UI across OS versions.
    try:
        from PySide6.QtWidgets import QStyleFactory

        if "windowsvista" in {str(k).lower() for k in QStyleFactory.keys()}:
            style = QStyleFactory.create("windowsvista")
            if style is not None:
                app.setStyle(style)
    except Exception:
        pass

    try:
        from qt_event_filters import NoEnterInPopupsFilter

        app._no_enter_in_popups_filter = NoEnterInPopupsFilter(app)
        app.installEventFilter(app._no_enter_in_popups_filter)
    except Exception:
        pass

    icon_path = _get_icon_path()
    if icon_path and os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    window = RobloxManagerGUI()
    window.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    # Needed for frozen executables (Nuitka/PyInstaller) that use multiprocessing/ProcessPoolExecutor
    # e.g., OCRWorker starts a ProcessPoolExecutor for OCR tasks.
    from multiprocessing import freeze_support

    freeze_support()
    main()
