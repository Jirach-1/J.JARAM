import sys
import json
import time
import os
import shutil
import requests
import re
import threading
from typing import Dict, Set, List, Tuple, Optional
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from PIL import Image
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                            QHBoxLayout, QGridLayout, QTabWidget, QTableWidget,
                            QTableWidgetItem, QPushButton, QLabel, QLineEdit,
                            QSpinBox, QDoubleSpinBox, QSlider, QTextEdit, QGroupBox,
                            QComboBox, QCheckBox, QSplitter,
                            QAbstractSpinBox, QStyle, QStyleOptionSpinBox,
                            QHeaderView, QMessageBox, QDialog, QDialogButtonBox,
                            QFormLayout, QScrollArea, QSizePolicy,
                            QAbstractItemView, QHeaderView, QScrollArea, QRubberBand,
                            QRadioButton, QListWidget, QListWidgetItem, QKeySequenceEdit)
from PyQt6.QtCore import (
    QTimer,
    QThread,
    pyqtSignal,
    Qt,
    QSize,
    QBuffer,
    QByteArray,
    QIODevice,
    QPointF,
    QRect,
    QPoint,
    QAbstractNativeEventFilter,
)
from PyQt6.QtGui import QFont, QIcon, QColor, QPixmap, QMovie, QRegion, QPainter, QPainterPath, QImage, QTextCursor, QKeySequence
# ---------- Qt6 enum shims (keep PyQt5-style constants working) ----------
# Paste directly below your current PyQt6 imports.

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
                # PyQt6 can pass a QByteArray-like here.
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

from main import RobloxManager, ProcessManager, GameLauncher
from cookie_extractor import CookieExtractor
from RAM_export import transform         # re-use your parsing helper
from main import limit_strap_helpers
from log_utils import find_log_for_username
from biomes import biome_names
from utilities_tab import setup_UTILITIES_tab
# Exclude NORMAL from the Settings table (still exists internally, we just don't offer it as a toggle)
GUI_BIOME_NAMES = [b for b in biome_names() if str(b).upper() != "NORMAL"]
from multiscope import MultiScopeEngine
from antiafk import AntiAFK
from ocr_worker import (
    OCRWorker,
    enum_roblox_windows,
    capture_window_image,
    preprocess_for_ocr,
    ColorFilter,
    get_ocr_device_summary,
    get_ocr_available_devices,
    compute_frame_hash,
    frame_hash_diff_percent,
)

# --- Auto Item engine (local module) ---
try:
    from auto_item_automation import AutoItemEngine  # type: ignore
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

class ConfigManager:

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
                "ping_message": "<@YourPing> This message is sent whenever your active processes drop to 1 or 0, for debugging. Leave webhook empty if not interested",
            },
            "multiscope": {
                "webhooks": [],   # ← NEW
                "enable_jester": True,
                "enable_mari": True,
                "jester_ping": "",
                "mari_ping": "",
                "merchant_rate_limit": 15,   # seconds (global cooldown for merchant alerts)
                "biome_min_interval": 2,     # seconds per server (dampen bursts)
            },
            "ocr": {
                "enabled": False,             # current desired state
                "workers": 1,
                "max_captures_per_second": 20,
                "cooldown_seconds": 600,
                "use_preprocess": True,
                "frame_diff_tolerance": 2,    # percent (skip OCR if frame changes <= this)
                "log_ocr_text": False,        # debug: include OCR text in OCR log
                "log_loop": True,             # include per-loop "[Loop N]" logs in OCR log
                "device_id": None,            # None => auto/default
                "roi": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
                "color_filters": [
                    {"name": "white_text", "r": 255, "g": 255, "b": 255, "tol": 60, "enabled": True},
                    {"name": "purple_text", "r": 145, "g": 67, "b": 255, "tol": 60, "enabled": True},
                ],
            },

            "antiafk": {
                "antiafk_enabled": False,
                "multi_instance_enabled": False,
                "antiafk_interval": 120,
            "antiafk_action": "space",
            "antiafk_user_safe": False,
            "antiafk_dev_mode": False,
            "antiafk_sequential_mode": False,
            "antiafk_sequential_delay": 0.75,
            "antiafk_menu_autoreconnect": False,
            },

              "auto_item": {
                   "enabled": False,
                   "tick_interval": 1.0,
                   "click_delay": 0.2,
                   "toggle_hotkey": "Ctrl+Alt+Space",
                   "users": [],
                  "coords": {
                     # Required coords are populated by the Auto Item tab (kept empty by default).
                     "conditional": {
                        "enabled": False,
                        "point": {"x": 0.0, "y": 0.0},
                        "color": "#FFFFFF",
                        "tolerance": 10,
                     }
                 },
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

        }


        self.default_user_structure = {
            "username": "",
            "cookie": "",
            "private_server_link": "",
            "place": "",
            "bad": False,
            "disabled": False,
        }
        

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

    def load_users(self):
        try:
            if self.users_file.exists():
                with open(self.users_file, 'r', encoding='utf-8') as f:
                    users_data = json.load(f)

                    return self._ensure_new_format(users_data)
            else:

                return self._migrate_old_config()
        except Exception as e:
            return {}

    def save_users(self, users_data):
        try:

            formatted_data = self._ensure_new_format(users_data)

            self._create_backup(self.users_file)

            self._safe_write_json(self.users_file, formatted_data)
            return True
        except Exception as e:
            return False

    def load_settings(self):
        try:
            if self.settings_file.exists():
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)

                settings = json.loads(json.dumps(self.default_settings))  # deep copy
                settings = self._deep_update(settings, loaded)
                return settings
            else:
                return json.loads(json.dumps(self.default_settings))
        except Exception:
            return json.loads(json.dumps(self.default_settings))

    def save_settings(self, settings_data):
        try:

            self._create_backup(self.settings_file)

            self._safe_write_json(self.settings_file, settings_data)
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
                    "bad": False
                }
            else:

                new_data[user_id] = cookie
        return new_data

    def _ensure_new_format(self, users_data):
        if not users_data:
            return {}

        new_data = {}
        for user_id, user_info in users_data.items():
            if isinstance(user_info, str):

                new_data[user_id] = {
                    "username": f"User_{user_id}",
                    "cookie": user_info,
                    "private_server_link": "",
                    "place": "",
                    "bad": False,
                    "disabled": False,
                }
            elif isinstance(user_info, dict):

                new_data[user_id] = {
                    "username": user_info.get("username", f"User_{user_id}"),
                    "cookie": user_info.get("cookie", ""),
                    "private_server_link": user_info.get("private_server_link", ""),
                    "place": user_info.get("place", ""),
                    "bad":  user_info.get("bad", False),
                    "disabled": user_info.get("disabled", False),
                }
            else:

                new_data[user_id] = {
                    "username": f"User_{user_id}",
                    "cookie": "",
                    "private_server_link": "",
                    "place": "",
                    "bad":  False,
                    "disabled": False,
                }
        return new_data

    def mark_bad_cookie(self, user_id: str, state: bool) -> None:
        users = self.load_users()
        if user_id in users and users[user_id].get("bad", False) != state:
            users[user_id]["bad"] = state
            self.save_users(users)

    def clear_all_bad_flags(self):
        users = self.load_users()
        for info in users.values():
            info["bad"] = False
        self.save_users(users)

    def get_users_for_manager(self):
        users = self.load_users()
        manager_format = {}
        for user_id, user_info in users.items():
            if isinstance(user_info, dict):

                manager_format[user_id] = user_info
            else:

                manager_format[user_id] = {
                    "username": f"User_{user_id}",
                    "cookie": user_info,
                    "private_server_link": "",
                    "place": "",
                    "disabled": False,
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
    log_signal     = pyqtSignal(str)
    status_signal  = pyqtSignal(dict)
    process_signal = pyqtSignal(dict)
    multiscope_signal = pyqtSignal(list)   # NEW: drives the Multiscope tab

    def __init__(self, cfg_manager):
        super().__init__()
        self.cfg_manager      = cfg_manager
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
        
        self._last_good_set = set()  # tracks which users are currently 'good' (bad/disabled == False)
        self._reservations_ttl = 60  # seconds a server is "held" by a handoff pre-join
        self.preconnect_grace = 120  # seconds to wait for username to show in logs on first connect
        self._waiting_usernames_since = {}  # uid -> epoch; cleaned up automatically
        self._boot_phase = True  # use initial_delay for any launches during ramp
        
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
        # Prefer live user_states (reflects in-memory bad/disabled flips immediately)
        if getattr(self, "user_states", None):
            source_items = [
                (uid, st.get("user_info", {}))
                for uid, st in self.user_states.items()
            ]
        else:
            # Fallback during very early init
            source_items = list(self.manager.settings.items())

        total_users = len(source_items)
        bad_count = sum(1 for _uid, info in source_items if info.get("bad", False))
        disabled_count = sum(1 for _uid, info in source_items if info.get("disabled", False))

        good_sorted = sorted(
            uid for uid, info in source_items
            if not info.get("bad", False) and not info.get("disabled", False)
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
            f"spare={len(self.spare_pool)} total={total_users} bad={bad_count} disabled={disabled_count}"
        )


    def _eligible_spares(self):
        now = time.time()
        for uid in sorted(self.spare_pool):
            st = self.user_states.get(uid)
            # Skip if bad in live state OR in settings (covers recent flips + reloads)
            is_bad_live = bool(st and st.get("user_info", {}).get("bad", False))
            is_bad_cfg  = bool(self.manager.settings.get(uid, {}).get("bad", False))
            is_disabled_live = bool(st and st.get("user_info", {}).get("disabled", False))
            is_disabled_cfg  = bool(self.manager.settings.get(uid, {}).get("disabled", False))
            if is_bad_live or is_bad_cfg or is_disabled_live or is_disabled_cfg:
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
            users_cfg = self.cfg_manager.load_users() or {}
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

        # 1) Live occupant check
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

        # Double-check disk bad flag
        users_cfg = self.cfg_manager.load_users() or {}
        if users_cfg.get(spare_uid, {}).get("bad", False):
            self._log(f"Skip spare {spare_uid}: marked bad in users.json")
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

        # Reserve this server while the spare is spinning up
        self._reserve_server(server_label, spare_uid, "handoff")

        cookie = override.get("cookie", "")
        ok = self.launcher.start_game_session(spare_uid, cookie, override)
        if ok:
            self.handoff_for[donor_uid] = spare_uid
            self.user_states[spare_uid]["last_launch"] = time.time()
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
    def initialize_manager(self) -> bool:
        try:
            self.manager = RobloxManager(config_manager=self.cfg_manager)
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

            # ───────────── Multiscope: set up the engine here ─────────────
            from multiscope import MultiScopeEngine

            def _get_username(uid: str) -> str:
                info = self.manager.settings.get(uid, {})
                return str(info.get("username", ""))

            def _get_ps_link(uid: str) -> str:
                # Hide link if this account is currently flagged bad
                st = self.user_states.get(uid, {}) if hasattr(self, "user_states") else {}
                is_bad = bool(st.get("user_info", {}).get("bad", False)
                            or self.manager.settings.get(uid, {}).get("bad", False))
                if is_bad:
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

            self.ms = MultiScopeEngine(
                get_username=_get_username,
                get_server_label=lambda uid: self.manager.process_tracker.user_server.get(uid, ""),
                get_ps_link_for_user=_get_ps_link,
                get_server_owner_for_user=_get_owner_for_ms,   # ← now fully separate
                get_cookie_for_user=_get_cookie,             # ← NEW
                log_fn=self._log,
            )



            # Provide full user list AFTER user_states are built
            self.ms.update_users(list(self.user_states.keys()))

            # Load and push webhook config
            cfg = self.cfg_manager.load_settings() or {}
            ms_cfg = (cfg.get("multiscope") or {})
            self.ms.configure_webhooks(
                biome_webhooks=cfg.get("webhooks", []),             # [{ "url": "...", "biomes": [...] }, ...]
                merchant_hook=ms_cfg.get("merchant_webhook", ""),
                enable_jester=ms_cfg.get("enable_jester", True),
                enable_mari=ms_cfg.get("enable_mari", True),
                jester_ping=ms_cfg.get("jester_ping", ""),
                mari_ping=ms_cfg.get("mari_ping", ""),
                merchant_rate_limit=float(ms_cfg.get("merchant_rate_limit", 15)),
                biome_min_interval=float(ms_cfg.get("biome_min_interval", 2)),
            )

            # ↓↓↓ ensure spares_mode, delays, and pools are live before first launch
            self.apply_new_settings(cfg)

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

        tm = cfg.get("timeout_monitor", {})
        self.manager.timeout_monitor.kill_enabled  = bool(tm.get("kill_enabled", True))
        self.manager.timeout_monitor.kill_timeout  = tm.get("kill_timeout", 1740)
        self.manager.timeout_monitor.poll_interval = tm.get("poll_interval", 10)
        self.manager.timeout_monitor.webhook_url   = tm.get("webhook_url", "")
        self.manager.timeout_monitor.ping_message  = tm.get("ping_message", "")


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
                ms_cfg = (cfg.get("multiscope") or {})
                self.ms.configure_webhooks(
                    biome_webhooks=cfg.get("webhooks", []),             # [{"url": "...", "biomes": [...], "biome_modes": {...}}, ...]
                    merchant_hook=ms_cfg.get("merchant_webhook", ""),
                    enable_jester=ms_cfg.get("enable_jester", True),
                    enable_mari=ms_cfg.get("enable_mari", True),
                    jester_ping=ms_cfg.get("jester_ping", ""),
                    mari_ping=ms_cfg.get("mari_ping", ""),
                    merchant_rate_limit=float(ms_cfg.get("merchant_rate_limit", 15)),
                    biome_min_interval=float(ms_cfg.get("biome_min_interval", 2)),
                    # if you persist per-biome modes, you can pass a merged map here:
                    # biome_modes=_merge_modes_from_webhooks(cfg.get("webhooks", []))
                )
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
                if info.get("bad", False):
                    continue

                cookie = info.get("cookie", "")
                # First user launches immediately; subsequent users spaced by initial_delay
                self.launcher.start_game_session(uid, cookie, info, skip_cleanup=True)
                self.user_states[uid]["last_launch"] = time.time()

                # optional: a quick warm tick; main loop is already ticking continuously
                try:
                    self._ms_prelaunch_tick()
                except Exception:
                    pass

                # Apply initial_delay only BETWEEN launches (i.e., after the first user)
                if i < total - 1:
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
                self.user_states[user_id]["inactive_since"] = None
                self.user_states[user_id]["requires_restart"] = False
                self.user_states[user_id]["status"] = "Restarting"
                self.user_states[user_id]["last_launch"] = time.time()
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
                
                # ---- Hot reload users.json (propagate "bad" flips etc.) ----
                try:
                    fresh_map = self.cfg_manager.get_users_for_manager() or {}
                except Exception:
                    fresh_map = {}

                # Only do work if something actually changed
                if fresh_map and fresh_map != self.manager.settings:
                    old_ids = set(self.manager.settings.keys())
                    new_ids = set(fresh_map.keys())

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

                    # 2) Add new users (if any)
                    now2 = time.time()
                    for uid in (new_ids - old_ids):
                        info = fresh_map.get(uid, {})
                        self.user_states[uid] = {
                            "last_active": now2,
                            "inactive_since": None,
                            "requires_restart": False,
                            "user_info": info.copy() if isinstance(info, dict) else {},
                            "status": "Offline",
                        }

                    # 3) Replace settings and re-compute pools
                    self.manager.settings = fresh_map
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
                    self.timing_trackers['cleanup'] = now

                if now - self.timing_trackers['window'] >= self.manager.check_intervals['window']:
                    try:
                        # enforce window cap: kill helpers with too many windows
                        win_counts = self.process_mgr.count_windows_by_process()
                        for pid, nwin in win_counts.items():
                            if nwin > self.manager.window_limit and pid != self.manager.excluded_pid:
                                self.process_mgr.terminate_process(pid, self.manager.process_tracker)
                    except Exception as e:
                        self._log(f"[WindowCheck] error: {e!r}")
                    self.timing_trackers['window'] = now
                
                # After housekeeping, before relaunch logic
                self._enforce_one_per_server()
                self._prune_reservations()

                # --- sync bad flags + evict from pools immediately ---
                try:
                    changed = False
                    for uid, cfg_info in list(self.manager.settings.items()):
                        st = self.user_states.get(uid)
                        if not st:
                            continue
                        bad_disk = bool(cfg_info.get("bad", False))
                        if st["user_info"].get("bad", False) != bad_disk:
                            st["user_info"]["bad"] = bad_disk
                            changed = True

                        # If bad, evict from both pools and cancel any in-flight handoff roles
                        if bad_disk:
                            if uid in self.active_pool or uid in self.spare_pool:
                                self.active_pool.discard(uid)
                                self.spare_pool.discard(uid)
                                changed = True
                            # If this bad user is being used as a spare for a donor, cancel it
                            donors = [d for d, s in list(self.handoff_for.items()) if s == uid]
                            for d in donors:
                                self.handoff_for.pop(d, None)
                    if changed and self.spares_mode:
                        self._recompute_pools()
                except Exception as _e:
                    self._log(f"[Sync] bad-flag sync error: {_e}")

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

                for uid, st in list(self.user_states.items()):
                    info = st["user_info"]
                    # bad users
                    if info.get("bad", False):
                        status[uid] = {
                            "status": "Bad",
                            "pids": [],
                            "needs_restart": False,
                            "last_active": st.get("last_active", 0),
                            "inactive_since": st.get("inactive_since"),
                            "ttl": [],
                            "server": self.manager.process_tracker.user_server.get(uid, ""),
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
                        }
                        st["requires_restart"] = False
                        continue
                    
                    live = [pid for pid in self.manager.process_tracker.user_processes.get(uid, [])
                            if self.process_mgr.verify_process_active(pid)]
                    
                    # --- NEW: pre-connect watchdog -------------------------------------------
                    # If this account has live processes but we still don't have a strict log
                    # match for its username within 2 minutes of launch, assume it failed to
                    # connect and recycle it.
                    now = time.time()
                    uname = str(info.get("username", "")).lower()

                    if live:
                        # Strict lookup: only returns a path once the username actually appears in logs
                        log_path = find_log_for_username(uname, allow_fallback=False)

                        if not log_path:
                            # oldest process start for this user
                            oldest_ct = min(self.manager.process_tracker.creation_timestamps.get(pid, now) for pid in live)
                            waited = now - oldest_ct

                            if waited >= self.preconnect_grace:
                                self._log(f"⚠️  {uname} did not appear in logs within {self.preconnect_grace}s — terminating")
                                self.kill_user_processes(uid)
                                st["requires_restart"] = True
                                # clean up a bit so next launch is fresh
                                try:
                                    self.manager.process_tracker.user_server.pop(uid, None)
                                except Exception:
                                    pass
                                live = []

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
                            }
                            continue

                    # active
                    if live:
                        st["status"] = "Active"
                        st["requires_restart"] = False
                        st["inactive_since"] = None
                        st["last_active"] = now
                    else:
                        if st.get("inactive_since") is None:
                            st["inactive_since"] = now
                        if (now - st.get("last_active", 0)) > self.restart_threshold:
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
                    }

                # After building status, keep pools synced with 'good' users immediately
                try:
                    current_good = {
                        u for u, s in self.user_states.items()
                        if not s.get("user_info", {}).get("bad", False)
                        and not s.get("user_info", {}).get("disabled", False)
                    }
                    if current_good != getattr(self, "_last_good_set", set()):
                        self._last_good_set = current_good
                        self._recompute_pools()
                except Exception as _e:
                    self._log(f"[Pools] good-set recompute error: {_e}")

                self.status_signal.emit(status)
                
                # Multiscope: update detection and push snapshot to the tab
                try:
                    if hasattr(self, "ms") and self.ms:
                        self.ms.tick(status)                 # feed current user/server state
                        # Handle MultiScope signals (disconnects, etc.)
                        for kind, uid, payload in self.ms.drain_events():
                            if kind == "disconnect":
                                uname = (self.manager.settings.get(uid, {}) or {}).get("username", uid)
                                self._log(f"[Disconnect] {uname} — {payload}; restarting now.")
                                try:
                                    self.kill_user_processes(uid)
                                except Exception:
                                    pass
                                st = self.user_states.get(uid, {})
                                st["requires_restart"] = True
                                st["inactive_since"]   = None
                                st["status"]           = "Offline"
                        rows = self.ms.snapshot()            # [{server, users, biome/merchant…}]
                        self.multiscope_signal.emit(rows)    # GUI will render it
                except Exception as _e:
                    self._log(f"[Multiscope] tick error: {_e}")

                # process table signal
                proc_info = {}
                for uid, pids in list(self.manager.process_tracker.user_processes.items()):
                    for pid in list(pids):  # snapshot the list in case it changes
                        if not self.process_mgr.verify_process_active(pid):
                            continue
                        created = datetime.fromtimestamp(
                            self.manager.process_tracker.creation_timestamps.get(pid, time.time())
                        ).strftime("%H:%M:%S")
                        windows = self.process_mgr.count_windows_by_process().get(pid, 0)
                        proc_info[pid] = {"user_id": uid, "created": created, "windows": windows}
                self.process_signal.emit(proc_info)


                # auto-restart queue (skip donors in handoff)
                try:
                    restartables = [
                        u for u, s in list(self.user_states.items())
                        if s.get("requires_restart")
                        and not s["user_info"].get("bad", False)
                        and not s["user_info"].get("disabled", False)
                        and u not in self.handoff_for
                    ]
                    # Per-user gating: honor backoff and per-user launch_delay up front,
                    # so we don't keep picking the same blocked uid over and over.
                    now = time.time()
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

                    if ordered and (now - self.timing_trackers['relaunch']) >= _global_gate:
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
                                self.user_states[uid]["inactive_since"]   = None
                                self.user_states[uid]["requires_restart"] = False
                                self.user_states[uid]["status"]           = "Restarting"
                                self.user_states[uid]["last_launch"]      = now
                                self.timing_trackers['relaunch']          = now
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
            if up.startswith("DISCONNECT") or up.startswith("UNKNOWN"):
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
                        self._log(f"[DEDUP] Killed extra instance for {uid} on {label}")

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

    def stop(self):
        self.running = False  # make the loop exit ASAP
        if getattr(self, "ms", None) and hasattr(self.ms, "shutdown"):
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

class UserManagementDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("User Account Management")
        self.setModal(True)

        self.resize(1400, 850)
        self.setMinimumSize(1200, 700)
        self.config_manager = ConfigManager()
        self.cookie_extractor = CookieExtractor(self)
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
            "bad": False
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

        if not cookie.startswith('_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_'):
            reply = QMessageBox.question(self, "Cookie Warning",
                                       "The cookie doesn't appear to be in the expected ROBLOSECURITY format. Continue anyway?",
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
                "bad": False
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

    def _on_cookie_extraction_complete(self, cookie: str):
        try:
            if cookie:
                self.cookie_input.setText(cookie)
                QMessageBox.information(self, "Success",
                                      "Cookie extracted successfully!\n\n"
                                      "The cookie has been automatically filled in the input field.")
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
    point_selected = pyqtSignal(tuple)

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
    roi_selected = pyqtSignal(tuple)

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
    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Chat Area")
        self._roi: Optional[Tuple[float, float, float, float]] = None

        layout = QVBoxLayout(self)
        label = _SelectableLabel(pixmap, self)
        label.roi_selected.connect(self._on_roi_selected)
        layout.addWidget(label)

        hint = QLabel("Drag to draw the chat box. Release to save.")
        hint.setStyleSheet(f"color:{ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(hint)

    def _on_roi_selected(self, roi: Tuple[float, float, float, float]):
        self._roi = roi
        self.accept()

    def selected_roi(self) -> Optional[Tuple[float, float, float, float]]:
        return self._roi


class RobloxManagerGUI(QMainWindow):
    # Bridge between AntiAFK worker threads and the Qt UI
    antiafk_log_signal = pyqtSignal(str)
    antiafk_state_signal = pyqtSignal(bool)
    autoitem_log_signal = pyqtSignal(str)
    bes_log_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self.ocr_worker: Optional[OCRWorker] = None
        self.ocr_roi: Optional[Tuple[float, float, float, float]] = None
        self._last_ocr_log: Optional[str] = None
        self._ocr_test_last_hash: Optional[int] = None
        self.ocr_log_autoscroll: bool = True
        self._last_ocr_device_id = None
        self._loading_ocr_settings = False
        self._loading_antiafk_settings = False
        self._loading_autoitem_settings = False
        self.settings_tab_index: Optional[int] = None
        self.process_data = {}
        self.user_data = {}
        self.config_manager = ConfigManager()
        self.cookie_extractor = CookieExtractor(self)

        # Anti-AFK engine instance (configured in setup_antiafk_tab)
        self.antiafk: Optional[AntiAFK] = None
        self.antiafk_status_box: Optional[QTextEdit] = None

        # Auto-Item engine + log view (configured in setup_auto_item_tab)
        self.auto_item_engine = None
        self.autoitem_status_box: Optional[QTextEdit] = None
        self._auto_item_hwnd_cache: Dict[int, int] = {}
        self._auto_item_hwnd_cache_ts: float = 0.0
        self._auto_item_hwnd_cache_lock = threading.Lock()
        self._ms_biome_by_server: Dict[str, str] = {}
        self._ms_in_menu_by_server: Dict[str, Optional[bool]] = {}
        self._ms_biome_lock = threading.Lock()
        self._auto_item_antiafk_was_running: bool = False

        # BES limiter controller (optional; Windows-only)
        self._loading_bes_settings = False
        self.bes_controller = (
            BESMultiProcessController(log=self.bes_log_signal.emit) if BESMultiProcessController is not None else None
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
        self.autoitem_log_signal.connect(self._on_autoitem_status)
        self.bes_log_signal.connect(self._on_bes_log)

        self.setup_ui()
        # NEW: add the Multiscope tab
        self.setup_multiscope_tab()
        self.setup_timers()


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
        self.setup_settings_tab()
        self.setup_RAMEXPORT_tab()
        setup_UTILITIES_tab(self)
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

        file_menu.addSeparator()

        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        help_menu = menubar.addMenu("Help")

        config_location_action = help_menu.addAction("Show Config Location")
        config_location_action.triggered.connect(self.show_config_location)

        help_menu.addSeparator()

        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

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

        cleanup_btn = QPushButton("Cleanup Dead Processes")
        cleanup_btn.clicked.connect(self.cleanup_processes)
        actions_layout.addWidget(cleanup_btn)

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
        self.tab_widget.addTab(scroll, "Dashboard")

    def setup_users_tab(self):
        users_widget = QWidget()
        layout = QVBoxLayout(users_widget)

        self.users_table = QTableWidget()
        self.users_table.setColumnCount(12)
        self.users_table.setHorizontalHeaderLabels([
            "User ID","Username","Private Server","Place",
            "Server",               # ← NEW
            "Status","PIDs","TTL(s)","Created","Last Active",
            "Inactive For","Actions"
        ])

        header = self.users_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)  # Server
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)  # Created
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)           # Last Active
        header.setSectionResizeMode(10, QHeaderView.ResizeMode.Fixed)            # Inactive For
        header.setSectionResizeMode(11, QHeaderView.ResizeMode.Fixed)            # Actions

        self.users_table.setColumnWidth(2, 200)
        self.users_table.setColumnWidth(3, 100)
        self.users_table.setColumnWidth(4, 120)
        self.users_table.setColumnWidth(7, 100)   # TTL(s)
        self.users_table.setColumnWidth(8, 100)   # Created
        self.users_table.setColumnWidth(10, 160)  # Inactive For
        self.users_table.setColumnWidth(11, 260)  # Actions
        self.users_table.verticalHeader().setDefaultSectionSize(60)

        try:
            self.users_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.users_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        except Exception:
            pass

        layout.addWidget(self.users_table)

        controls_layout = QHBoxLayout()
        refresh_users_btn = QPushButton("Refresh")
        refresh_users_btn.clicked.connect(self.refresh_users)
        controls_layout.addWidget(refresh_users_btn)

        add_user_btn = QPushButton("Modify Users")
        add_user_btn.clicked.connect(self.open_user_management)
        controls_layout.addWidget(add_user_btn)

        kill_selected_btn = QPushButton("Kill Selected")
        kill_selected_btn.setProperty("class", "danger")
        kill_selected_btn.clicked.connect(self.kill_selected_user)
        controls_layout.addWidget(kill_selected_btn)

        controls_layout.addStretch()
        layout.addLayout(controls_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(users_widget)
        self.tab_widget.addTab(scroll, "Users")

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
        form_layout.addWidget(QLabel("Private Server Link:"))
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

        self.account_disabled = QCheckBox("Disable this account")
        form_layout.addWidget(self.account_disabled)

        button_layout = QHBoxLayout()
        self.add_account_btn = QPushButton("Add Account")
        self.add_account_btn.setStyleSheet(f"background-color: {ModernStyle.PRIMARY}; color: white; border: none; padding: 8px 16px; border-radius: 4px; font-weight: bold;")
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
        list_layout.addWidget(list_title)

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
        list_layout.addWidget(self.accounts_list)

        main_layout.addWidget(list_widget)
        layout.addLayout(main_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(accounts_widget)
        self.tab_widget.addTab(scroll, "Accounts")
        self.refresh_accounts_list()

    def setup_logs_tab(self):
        logs_widget = QWidget()
        layout = QVBoxLayout(logs_widget)

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFont(QFont("Consolas", 10))
        layout.addWidget(self.log_display)

        controls_layout = QHBoxLayout()

        clear_logs_btn = QPushButton("Clear Logs")
        clear_logs_btn.clicked.connect(self.clear_logs)
        controls_layout.addWidget(clear_logs_btn)

        save_logs_btn = QPushButton("Save Logs")
        save_logs_btn.clicked.connect(self.save_logs)
        controls_layout.addWidget(save_logs_btn)

        controls_layout.addStretch()
        
        self.watch_hit_chk = QCheckBox("Show WATCH-HIT messages", self)
        self.watch_hit_chk.setChecked(False)  # hidden by default
        controls_layout.addWidget(self.watch_hit_chk)

        self.scan_trace_chk = QCheckBox("Show SCAN-TRACE messages", self)
        self.scan_trace_chk.setChecked(False)      # default = ON
        controls_layout.addWidget(self.scan_trace_chk)
        
        self.auto_scroll_checkbox = QCheckBox("Auto-scroll")
        self.auto_scroll_checkbox.setChecked(True)
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
        self.ocr_max_caps_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_cooldown_spin = QSpinBox(); self.ocr_cooldown_spin.setRange(30, 7200); self.ocr_cooldown_spin.setSuffix(" s")
        self.ocr_cooldown_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_preprocess_chk = QCheckBox("Use preprocessing")
        self.ocr_preprocess_chk.toggled.connect(self._on_ocr_settings_changed)
        self.ocr_frame_diff_tol_spin = QSpinBox(); self.ocr_frame_diff_tol_spin.setRange(0, 100); self.ocr_frame_diff_tol_spin.setSuffix(" %")
        self.ocr_frame_diff_tol_spin.setToolTip("Skip OCR when the chat frame changes by at most this amount compared to the previous frame.")
        self.ocr_frame_diff_tol_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_device_combo = QComboBox()
        self.ocr_device_combo.currentIndexChanged.connect(self._on_ocr_settings_changed)
        self._load_ocr_device_choices()

        controls_form.addRow("OCR workers:", self.ocr_workers_spin)
        controls_form.addRow("Max captures / sec:", self.ocr_max_caps_spin)
        controls_form.addRow("Cooldown per PID:", self.ocr_cooldown_spin)
        controls_form.addRow("Preprocess chat image:", self.ocr_preprocess_chk)
        controls_form.addRow("Processor:", self.ocr_device_combo)
        controls_form.addRow("Skip OCR if frame change ≤:", self.ocr_frame_diff_tol_spin)

        layout.addWidget(controls_group)

        btn_row = QHBoxLayout()
        calibrate_btn = QPushButton("Calibrate chat area")
        calibrate_btn.clicked.connect(self.calibrate_ocr_roi)
        preview_btn = QPushButton("Test preview")
        preview_btn.clicked.connect(self.show_ocr_preview)
        compare_btn = QPushButton("Test frame compare")
        compare_btn.clicked.connect(self.test_ocr_frame_compare)
        btn_row.addWidget(calibrate_btn)
        btn_row.addWidget(preview_btn)
        btn_row.addWidget(compare_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.ocr_roi_label = QLabel("ROI: not calibrated")
        layout.addWidget(self.ocr_roi_label)

        filters_group = QGroupBox("Color Filters")
        filters_layout = QVBoxLayout(filters_group)
        self.ocr_filter_table = QTableWidget()
        self.ocr_filter_table.setColumnCount(6)
        self.ocr_filter_table.setHorizontalHeaderLabels(["Enabled", "Name", "R", "G", "B", "Tol"])
        self.ocr_filter_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.ocr_filter_table.setMinimumHeight(180)
        self.ocr_filter_table.setShowGrid(False)
        self.ocr_filter_table.setAlternatingRowColors(False)
        header = self.ocr_filter_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.ocr_filter_table.setColumnWidth(0, 90)
        for col in range(2, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.ocr_filter_table.setColumnWidth(col, 95)
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
            """
        )
        filters_layout.addWidget(self.ocr_filter_table)
        # Changes are wired from per-cell widgets (see _add_filter_row).

        filter_btns = QHBoxLayout()
        add_filter_btn = QPushButton("Add Filter")
        add_filter_btn.clicked.connect(lambda: (self._add_filter_row(), self._on_ocr_settings_changed()))
        remove_filter_btn = QPushButton("Remove Selected")
        remove_filter_btn.clicked.connect(lambda: (self._remove_selected_filter_rows(), self._on_ocr_settings_changed()))
        filter_btns.addWidget(add_filter_btn)
        filter_btns.addWidget(remove_filter_btn)
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
        self.ocr_auto_scroll_chk = QCheckBox("Auto-scroll")
        self.ocr_auto_scroll_chk.setChecked(True)
        self.ocr_auto_scroll_chk.toggled.connect(lambda checked: setattr(self, "ocr_log_autoscroll", bool(checked)))
        clear_log_btn = QPushButton("Clear OCR Log")
        clear_log_btn.clicked.connect(self.ocr_log_box.clear)
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

        # True-AFK mode
        self.antiafk_user_safe_chk = QCheckBox("True-AFK Mode (only act when you're inactive)")
        settings_layout.addWidget(self.antiafk_user_safe_chk, 2, 0, 1, 2)

        # Sequential mode + delay
        self.antiafk_sequential_chk = QCheckBox("Sequential Mode (better for 5+ windows)")
        settings_layout.addWidget(self.antiafk_sequential_chk, 3, 0, 1, 2)

        settings_layout.addWidget(QLabel("Delay between actions (seconds):"), 4, 0)
        self.antiafk_seq_delay_spin = QDoubleSpinBox()
        self.antiafk_seq_delay_spin.setRange(0.1, 5.0)
        self.antiafk_seq_delay_spin.setSingleStep(0.05)
        self.antiafk_seq_delay_spin.setDecimals(2)
        self.antiafk_seq_delay_spin.setValue(0.75)
        settings_layout.addWidget(self.antiafk_seq_delay_spin, 4, 1)

        # Use AutoReconnect in main menu
        self.antiafk_menu_autoreconnect_chk = QCheckBox("Use AutoReconnect when in main menu")
        settings_layout.addWidget(self.antiafk_menu_autoreconnect_chk, 5, 0, 1, 2)

        layout.addWidget(settings_group)

        # Status/log view specific to Anti-AFK
        status_group = QGroupBox("Anti-AFK Log")
        status_layout = QVBoxLayout(status_group)
        self.antiafk_status_box = QTextEdit()
        self.antiafk_status_box.setReadOnly(True)
        self.antiafk_status_box.setFont(QFont("Consolas", 10))
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
        self.antiafk.is_pid_in_menu_callback = self._is_pid_in_menu

        # Apply config to UI without triggering change handlers
        self._loading_antiafk_settings = True
        try:
            cfg = self.antiafk.config or {}
            self.antiafk_interval_spin.setValue(int(cfg.get("antiafk_interval", 120)))
            self.antiafk_action_combo.setCurrentText(cfg.get("antiafk_action", "space"))
            self.antiafk_user_safe_chk.setChecked(bool(cfg.get("antiafk_user_safe", False)))
            self.antiafk_sequential_chk.setChecked(bool(cfg.get("antiafk_sequential_mode", False)))
            self.antiafk_seq_delay_spin.setValue(float(cfg.get("antiafk_sequential_delay", 0.75)))
            self.antiafk_menu_autoreconnect_chk.setChecked(bool(cfg.get("antiafk_menu_autoreconnect", False)))
            self.antiafk_enable_chk.setChecked(bool(cfg.get("antiafk_enabled", False)))
        finally:
            self._loading_antiafk_settings = False

        # React to UI changes
        self.antiafk_interval_spin.valueChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_action_combo.currentTextChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_user_safe_chk.toggled.connect(self._on_antiafk_ui_changed)
        self.antiafk_sequential_chk.toggled.connect(self._on_antiafk_ui_changed)
        self.antiafk_seq_delay_spin.valueChanged.connect(self._on_antiafk_ui_changed)
        self.antiafk_menu_autoreconnect_chk.toggled.connect(self._on_antiafk_ui_changed)
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

    def _on_antiafk_status(self, message: str):
        """Qt slot: update Anti-AFK tab and main log safely on the GUI thread."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"

        if self.antiafk_status_box is not None:
            try:
                self.antiafk_status_box.append(line)
            except Exception:
                pass

    def _on_antiafk_state_changed(self, enabled: bool):
        """Qt slot: keep Anti-AFK buttons and inputs in sync with worker state."""
        enabled = bool(enabled)
        for w in (
            getattr(self, "antiafk_interval_spin", None),
            getattr(self, "antiafk_action_combo", None),
            getattr(self, "antiafk_user_safe_chk", None),
            getattr(self, "antiafk_sequential_chk", None),
            getattr(self, "antiafk_seq_delay_spin", None),
        ):
            if w is not None:
                try:
                    w.setEnabled(not enabled)
                except Exception:
                    pass

    def _on_antiafk_ui_changed(self):
        """Apply current Anti-AFK UI values to the engine and persist them."""
        if self._loading_antiafk_settings or not self.antiafk:
            return

        try:
            interval = int(self.antiafk_interval_spin.value())
            action = self.antiafk_action_combo.currentText()
            user_safe = bool(self.antiafk_user_safe_chk.isChecked())
            sequential_mode = bool(self.antiafk_sequential_chk.isChecked())
            sequential_delay = float(self.antiafk_seq_delay_spin.value())
            menu_autoreconnect = bool(self.antiafk_menu_autoreconnect_chk.isChecked())
            enabled_flag = bool(self.antiafk_enable_chk.isChecked())

            # Push new settings into the AntiAFK engine so behavior changes immediately.
            self.antiafk.apply_host_config(
                interval=interval,
                action=action,
                user_safe=user_safe,
                sequential_mode=sequential_mode,
                sequential_delay=sequential_delay,
                menu_autoreconnect=menu_autoreconnect,
            )

            # If the manager is running, keep Anti-AFK running state in sync with the toggle.
            if self._is_manager_running():
                self.antiafk.toggle_antiafk(enabled_flag)

            # Persist Anti-AFK settings to disk so they survive relaunch.
            self._save_antiafk_settings()
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
            antiafk_cfg["antiafk_user_safe"] = bool(self.antiafk_user_safe_chk.isChecked())
            antiafk_cfg["antiafk_sequential_mode"] = bool(self.antiafk_sequential_chk.isChecked())
            antiafk_cfg["antiafk_sequential_delay"] = float(self.antiafk_seq_delay_spin.value())
            antiafk_cfg["antiafk_menu_autoreconnect"] = bool(self.antiafk_menu_autoreconnect_chk.isChecked())
            antiafk_cfg["antiafk_enabled"] = bool(self.antiafk_enable_chk.isChecked())
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
            self.antiafk_user_safe_chk.setChecked(bool(defaults.get("antiafk_user_safe", False)))
            self.antiafk_sequential_chk.setChecked(bool(defaults.get("antiafk_sequential_mode", False)))
            self.antiafk_seq_delay_spin.setValue(float(defaults.get("antiafk_sequential_delay", 0.75)))
            self.antiafk_menu_autoreconnect_chk.setChecked(bool(defaults.get("antiafk_menu_autoreconnect", False)))
            self.antiafk_enable_chk.setChecked(bool(defaults.get("antiafk_enabled", False)))
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
        self.antiafk.toggle_antiafk(True)

    def _on_antiafk_stop(self):
        """Stop the Anti-AFK loop."""
        if not self.antiafk:
            return
        self.antiafk.toggle_antiafk(False)
        self._save_antiafk_settings()

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

        settings_layout.addWidget(QLabel("Click/typing delay (seconds):"), 2, 0)
        self.auto_item_delay_spin = QDoubleSpinBox()
        self.auto_item_delay_spin.setRange(0.01, 2.0)
        self.auto_item_delay_spin.setDecimals(2)
        self.auto_item_delay_spin.setSingleStep(0.05)
        self.auto_item_delay_spin.setValue(0.2)
        settings_layout.addWidget(self.auto_item_delay_spin, 2, 1)

        settings_layout.addWidget(QLabel("Toggle hotkey:"), 3, 0)
        self.auto_item_hotkey_edit = QKeySequenceEdit()
        self.auto_item_hotkey_edit.setToolTip("Global hotkey to toggle Auto-Item enable/disable (default: Ctrl+Alt+Space).")
        try:
            self.auto_item_hotkey_edit.setKeySequence(QKeySequence("Ctrl+Alt+Space"))
        except Exception:
            pass
        settings_layout.addWidget(self.auto_item_hotkey_edit, 3, 1)

        self.auto_item_test_btn = QPushButton("Test Auto-Item (first selected user)")
        self.auto_item_test_btn.setToolTip("Runs the configured automation once on the first selected user window.")
        self.auto_item_test_btn.clicked.connect(self._auto_item_test_once)
        settings_layout.addWidget(self.auto_item_test_btn, 4, 0, 1, 2)

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
        self.auto_item_table.setColumnCount(6)
        self.auto_item_table.setHorizontalHeaderLabels(["Enabled", "Item", "Amount", "Cooldown (s)", "Biomes", "Users"])
        self.auto_item_table.setShowGrid(False)
        self.auto_item_table.setAlternatingRowColors(False)
        header = self.auto_item_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        vh = self.auto_item_table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(62)
        self.auto_item_table.setColumnWidth(0, 80)
        self.auto_item_table.setColumnWidth(2, 95)
        self.auto_item_table.setColumnWidth(3, 120)
        self.auto_item_table.setColumnWidth(4, 120)
        self.auto_item_table.setColumnWidth(5, 140)
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
        self.auto_item_enable_chk.toggled.connect(self._on_auto_item_ui_changed)
        self.auto_item_tick_spin.valueChanged.connect(self._on_auto_item_ui_changed)
        self.auto_item_delay_spin.valueChanged.connect(self._on_auto_item_ui_changed)
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
        if self.autoitem_status_box is not None:
            try:
                self.autoitem_status_box.append(line)
            except Exception:
                pass
        try:
            self.add_log(message)
        except Exception:
            pass

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
                    if server not in (self._ms_in_menu_by_server or {}):
                        return None
                    val = self._ms_in_menu_by_server.get(server, None)
                    if val is None:
                        return None
                    return bool(val)
            except Exception:
                return None

        self.auto_item_engine = AutoItemEngine(
            pid_provider=_pid_provider,
            hwnd_provider=_hwnd_provider,
            biome_provider=_biome_provider,
            in_menu_provider=_in_menu_provider,
            log=self.autoitem_log_signal.emit,
            pause_antiafk=self._auto_item_pause_antiafk,
            resume_antiafk=self._auto_item_resume_antiafk,
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

    def _auto_item_pause_antiafk(self):
        self._auto_item_antiafk_was_running = False
        if getattr(self, "antiafk", None) and getattr(self.antiafk, "antiafk_running", False):
            try:
                self._auto_item_antiafk_was_running = True
                if hasattr(self.antiafk, "pause_antiafk"):
                    self.antiafk.pause_antiafk(wait=True)
                else:
                    # Fallback for legacy hosts: fully stop before Auto-Item interactions.
                    t = getattr(self.antiafk, "antiafk_thread", None)
                    self.antiafk.stop_antiafk()
                    try:
                        if t is not None and getattr(t, "is_alive", None) and t.is_alive():
                            t.join()
                    except Exception:
                        pass
            except Exception:
                self._auto_item_antiafk_was_running = False

    def _auto_item_resume_antiafk(self):
        try:
            if (
                self._auto_item_antiafk_was_running
                and getattr(self, "antiafk", None)
                and self._is_manager_running()
                and bool(getattr(self, "antiafk_enable_chk", None) and self.antiafk_enable_chk.isChecked())
            ):
                # Prefer resume when the engine is still running; otherwise restart.
                if bool(getattr(self.antiafk, "antiafk_running", False)) and hasattr(self.antiafk, "resume_antiafk"):
                    self.antiafk.resume_antiafk()
                else:
                    self.antiafk.start_antiafk()
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
        if ctl is None:
            return

        try:
            with self._bes_cfg_lock:
                cfg = dict(self._bes_cfg_cache or {})
        except Exception:
            cfg = {}

        if not bool(cfg.get("enabled", False)):
            return

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
                self.autoitem_log_signal.emit("[Auto-Item] Toggle hotkey cleared (disabled).")
            return

        parsed = self._parse_hotkey_to_win32(s)
        if not parsed:
            if not quiet:
                QMessageBox.warning(
                    self,
                    "Auto-Item Hotkey",
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
                    self.autoitem_log_signal.emit(f"[Auto-Item] Toggle hotkey registered: {s}")
            else:
                self._auto_item_hotkey_registered = False
                self._auto_item_hotkey_hwnd = 0
                msg = f"Could not register hotkey '{s}'. It may be in use by another app."
                self.autoitem_log_signal.emit(f"[Auto-Item] {msg}")
                if not quiet:
                    QMessageBox.warning(self, "Auto-Item Hotkey", msg)
        except Exception as e:
            self._auto_item_hotkey_registered = False
            self._auto_item_hotkey_hwnd = 0
            if not quiet:
                QMessageBox.warning(self, "Auto-Item Hotkey", f"Failed to register hotkey:\n{e}")

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
        items.append({"enabled": True, "name": "", "amount": 1, "cooldown": 0, "biomes": []})
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
            btn.setText("All v")
            return
        if not isinstance(raw, (list, tuple)):
            btn.setProperty("users", None)
            btn.setText("All v")
            return

        users = [str(u).strip() for u in raw if str(u).strip()]
        btn.setProperty("users", users)
        label = "None" if not users else f"{len(users)} selected"
        btn.setText(f"{label} v")

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
        dlg.setWindowTitle("Select Users For This Item")
        v = QVBoxLayout(dlg)

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

                item = {
                    "enabled": bool(en.isChecked()) if isinstance(en, QCheckBox) else True,
                    "name": name.text().strip() if isinstance(name, QLineEdit) else "",
                    "amount": int(amt.value()) if isinstance(amt, QSpinBox) else 1,
                    "cooldown": int(cd.value()) if isinstance(cd, QSpinBox) else 0,
                    "biomes": (bbtn.property("biomes") or []) if isinstance(bbtn, QPushButton) else [],
                }
                if isinstance(ubtn, QPushButton):
                    raw_users = ubtn.property("users")
                    if isinstance(raw_users, (list, tuple, set)):
                        users_list = [str(u).strip() for u in raw_users if str(u).strip()]
                        item["users"] = users_list
                        item["users_explicit"] = True
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

    def _get_auto_item_settings_from_ui(self) -> dict:
        # Selected users
        users = [uid for uid, cb in (self.auto_item_user_checks or {}).items() if cb.isChecked()]

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
        self.bes_cycle_spin.setRange(10, 500)
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
        box = getattr(self, "bes_log_box", None)
        if box is not None:
            try:
                box.append(line)
            except Exception:
                pass

    def _bes_refresh_user_list(self) -> None:
        combos = getattr(self, "bes_exempt_combos", None) or []
        try:
            users_cfg = self.config_manager.load_users() or {}
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

    def _add_filter_row(self, name: str = "Blank", r: int = 255, g: int = 255, b: int = 255, tol: int = 40, enabled: bool = True, locked_name: bool = False):
        row = self.ocr_filter_table.rowCount()
        self.ocr_filter_table.insertRow(row)
        try:
            self.ocr_filter_table.setRowHeight(row, 62)
        except Exception:
            pass

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

        en = QCheckBox()
        en.setChecked(bool(enabled))
        en.setStyleSheet("background: transparent;")
        en.toggled.connect(self._on_ocr_settings_changed)
        self.ocr_filter_table.setCellWidget(row, 0, _wrap_cell(en, center=True))

        name_le = QLineEdit(str(name or ""))
        name_le.setPlaceholderText("Filter name")
        name_le.textChanged.connect(self._on_ocr_settings_changed)
        name_le.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if locked_name:
            name_le.setReadOnly(True)
            name_le.setProperty("ocr_default_filter", True)
        self.ocr_filter_table.setCellWidget(row, 1, _wrap_cell(name_le, center=False, margins=(6, 6, 6, 6)))

        def _mk_rgb_spin(value: int, *, maximum: int = 255) -> QSpinBox:
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

        r_sb = _mk_rgb_spin(r, maximum=255)
        g_sb = _mk_rgb_spin(g, maximum=255)
        b_sb = _mk_rgb_spin(b, maximum=255)
        tol_sb = _mk_rgb_spin(tol, maximum=255)

        self.ocr_filter_table.setCellWidget(row, 2, _wrap_cell(r_sb, center=True))
        self.ocr_filter_table.setCellWidget(row, 3, _wrap_cell(g_sb, center=True))
        self.ocr_filter_table.setCellWidget(row, 4, _wrap_cell(b_sb, center=True))
        self.ocr_filter_table.setCellWidget(row, 5, _wrap_cell(tol_sb, center=True))

    def _remove_selected_filter_rows(self):
        rows = sorted({idx.row() for idx in self.ocr_filter_table.selectedIndexes()}, reverse=True)
        default_filters = (self.config_manager.default_settings.get("ocr", {}) or {}).get("color_filters", [])
        default_names = {str(f.get("name", "")).strip() for f in default_filters if str(f.get("name", "")).strip()}
        def _unwrap(col_widget: QWidget, typ):
            if col_widget is None:
                return None
            if isinstance(col_widget, typ):
                return col_widget
            try:
                return col_widget.findChild(typ)
            except Exception:
                return None
        for r in rows:
            name_w = _unwrap(self.ocr_filter_table.cellWidget(r, 1), QLineEdit)
            name = name_w.text().strip() if isinstance(name_w, QLineEdit) else ""
            locked = False
            if isinstance(name_w, QLineEdit) and name_w.property("ocr_default_filter"):
                locked = True
            if name and name in default_names:
                locked = True
            if locked:
                continue
            self.ocr_filter_table.removeRow(r)

    def _load_color_filters_table(self, filters: List[dict]):
        self.ocr_filter_table.setRowCount(0)
        defaults = (self.config_manager.default_settings.get("ocr", {}) or {}).get("color_filters", [])
        default_names = [str(f.get("name", "")).strip() for f in defaults if str(f.get("name", "")).strip()]
        default_set = set(default_names)
        effective_filters = filters or defaults or []
        seen_defaults = set()
        for f in effective_filters:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name", "")).strip()
            locked = bool(name and name in default_set)
            if locked:
                seen_defaults.add(name)
            self._add_filter_row(
                name or "",
                int(f.get("r", 0)),
                int(f.get("g", 0)),
                int(f.get("b", 0)),
                int(f.get("tol", 0)),
                bool(f.get("enabled", True)),
                locked_name=locked,
            )

        for d in defaults:
            name = str(d.get("name", "")).strip()
            if not name or name in seen_defaults:
                continue
            self._add_filter_row(
                name,
                int(d.get("r", 0)),
                int(d.get("g", 0)),
                int(d.get("b", 0)),
                int(d.get("tol", 0)),
                bool(d.get("enabled", True)),
                locked_name=True,
            )

    def _current_color_filters(self, as_dataclass: bool = False):
        filters = []
        rows = self.ocr_filter_table.rowCount()

        for r in range(rows):
            def _unwrap(col_widget: QWidget, typ):
                if col_widget is None:
                    return None
                if isinstance(col_widget, typ):
                    return col_widget
                try:
                    return col_widget.findChild(typ)
                except Exception:
                    return None

            en = _unwrap(self.ocr_filter_table.cellWidget(r, 0), QCheckBox)
            name_w = _unwrap(self.ocr_filter_table.cellWidget(r, 1), QLineEdit)
            r_w = _unwrap(self.ocr_filter_table.cellWidget(r, 2), QSpinBox)
            g_w = _unwrap(self.ocr_filter_table.cellWidget(r, 3), QSpinBox)
            b_w = _unwrap(self.ocr_filter_table.cellWidget(r, 4), QSpinBox)
            tol_w = _unwrap(self.ocr_filter_table.cellWidget(r, 5), QSpinBox)

            enabled = bool(en.isChecked()) if isinstance(en, QCheckBox) else True
            name = name_w.text().strip() if isinstance(name_w, QLineEdit) else ""
            rv = int(r_w.value()) if isinstance(r_w, QSpinBox) else 0
            gv = int(g_w.value()) if isinstance(g_w, QSpinBox) else 0
            bv = int(b_w.value()) if isinstance(b_w, QSpinBox) else 0
            tol = int(tol_w.value()) if isinstance(tol_w, QSpinBox) else 0

            if as_dataclass:
                filters.append(ColorFilter(name, rv, gv, bv, tol, enabled))
            else:
                filters.append({"name": name, "r": rv, "g": gv, "b": bv, "tol": tol, "enabled": enabled})
        return filters

    def _get_ocr_settings_from_ui(self) -> dict:
        roi = self.ocr_roi or (0.0, 0.0, 0.0, 0.0)
        return {
            "enabled": bool(self.ocr_enable_chk.isChecked()),
            "workers": self.ocr_workers_spin.value(),
            "max_captures_per_second": self.ocr_max_caps_spin.value(),
            "cooldown_seconds": self.ocr_cooldown_spin.value(),
            "use_preprocess": bool(self.ocr_preprocess_chk.isChecked()),
            "frame_diff_tolerance": int(self.ocr_frame_diff_tol_spin.value()),
            "log_ocr_text": bool(getattr(self, "ocr_log_text_chk", None) and self.ocr_log_text_chk.isChecked()),
            "log_loop": bool(getattr(self, "ocr_loop_logs_chk", None) and self.ocr_loop_logs_chk.isChecked()),
            "device_id": self.ocr_device_combo.currentData() if hasattr(self, "ocr_device_combo") else None,
            "roi": {"x": roi[0], "y": roi[1], "w": roi[2], "h": roi[3]},
            "color_filters": self._current_color_filters(as_dataclass=False),
        }

    def _apply_ocr_settings_to_ui(self, cfg: dict):
        defaults = self.config_manager.default_settings.get("ocr", {}) or {}
        cfg = cfg or defaults
        self._loading_ocr_settings = True
        try:
            target_enabled = bool(cfg.get("enabled", False))
            self.ocr_enable_chk.setChecked(target_enabled)

            self.ocr_workers_spin.setValue(int(cfg.get("workers", defaults.get("workers", 1))))
            self.ocr_max_caps_spin.setValue(int(cfg.get("max_captures_per_second", defaults.get("max_captures_per_second", 20))))
            self.ocr_cooldown_spin.setValue(int(cfg.get("cooldown_seconds", defaults.get("cooldown_seconds", 600))))
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
            self._update_ocr_roi_label()

            self._load_color_filters_table(cfg.get("color_filters") or defaults.get("color_filters", []))
        finally:
            self._loading_ocr_settings = False

        # Reflect current OCR device in the OCR tab
        self._update_ocr_device_label()

        if self._is_manager_running() and self.ocr_enable_chk.isChecked():
            self._start_ocr_worker()

    def _update_ocr_roi_label(self):
        if self.ocr_roi:
            x, y, w, h = self.ocr_roi
            self.ocr_roi_label.setText(f"ROI: x={x:.3f}, y={y:.3f}, w={w:.3f}, h={h:.3f}")
        else:
            self.ocr_roi_label.setText("ROI: not calibrated")

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
        # Live-apply to worker if running (restart to apply device changes)
        if device_changed and self.ocr_worker and self.ocr_worker.isRunning():
            self._stop_ocr_worker()
            if self.ocr_enable_chk.isChecked():
                self._start_ocr_worker()
        else:
            self._sync_ocr_worker_settings()
        self._last_ocr_device_id = device_id

    def _reset_ocr_to_defaults(self):
        """Reset only the OCR tab to its default config and live-apply."""
        defaults = self.config_manager.default_settings.get("ocr", {}) or {}
        self._loading_ocr_settings = True
        try:
            self.ocr_enable_chk.setChecked(bool(defaults.get("enabled", False)))
            self.ocr_workers_spin.setValue(int(defaults.get("workers", 1)))
            self.ocr_max_caps_spin.setValue(int(defaults.get("max_captures_per_second", 20)))
            self.ocr_cooldown_spin.setValue(int(defaults.get("cooldown_seconds", 600)))
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
            self._update_ocr_roi_label()
            self._load_color_filters_table(defaults.get("color_filters", []))
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
        if not self.ocr_roi:
            QMessageBox.warning(self, "Calibrate OCR", "Please calibrate the chat area before enabling OCR.")
            self.ocr_enable_chk.setChecked(False)
            return

        ocr_cfg = self._get_ocr_settings_from_ui()
        ms_cfg = self._ms_settings_from_ui()

        self.ocr_worker = OCRWorker(
            ocr_settings=ocr_cfg,
            ms_settings=ms_cfg,
            context_provider=self._resolve_pid_context,
        )
        self.ocr_worker.log_signal.connect(self._handle_ocr_log)
        self.ocr_worker.status_signal.connect(self._handle_ocr_status)
        self.ocr_worker.start()
        self._handle_ocr_status("running")

    def _stop_ocr_worker(self):
        if self.ocr_worker:
            self.ocr_worker.stop()
            # wait up to 3s for a clean stop; force terminate if hung
            if not self.ocr_worker.wait(3000):
                self.add_log("[OCR] stop timed out; forcing terminate()")
                try:
                    self.ocr_worker.terminate()
                except Exception:
                    pass
                self.ocr_worker.wait(1000)
            self.ocr_worker = None
        self._handle_ocr_status("stopped")

    def _is_manager_running(self) -> bool:
        return bool(self.worker_thread and self.worker_thread.isRunning())

    def _ms_settings_from_ui(self) -> dict:
        ms = {}
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
        if hasattr(self, "ms_jester_type") and hasattr(self, "ms_jester_id"):
            ms["jester_ping_type"] = self.ms_jester_type.currentText()
            ms["jester_ping_id"] = self.ms_jester_id.text().strip()
        if hasattr(self, "ms_mari_type") and hasattr(self, "ms_mari_id"):
            ms["mari_ping_type"] = self.ms_mari_type.currentText()
            ms["mari_ping_id"] = self.ms_mari_id.text().strip()

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

    def show_ocr_preview(self):
        """
        Capture the calibrated chat area from a Roblox window and show what the
        OCR engine would see. Any unexpected errors are logged to the OCR log
        panel so users can debug missing dependencies or GPU issues.
        """
        if not self.ocr_roi:
            QMessageBox.warning(self, "Calibrate OCR", "Please calibrate the chat area first.")
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
                    self._current_color_filters(as_dataclass=True),
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

    def test_ocr_frame_compare(self):
        """
        Capture the OCR frame twice (click multiple times) and report how similar
        it is to the previous capture using the current tolerance setting.
        """
        if not self.ocr_roi:
            QMessageBox.warning(self, "Calibrate OCR", "Please calibrate the chat area first.")
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
                img_for_compare = preprocess_for_ocr(img, self._current_color_filters(as_dataclass=True))
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

    def _resolve_pid_context(self, pid: int) -> Dict[str, str]:
        ctx = {"user_id": "", "username": "", "server_label": "", "ps_link": "", "owner": ""}
        wt = self.worker_thread
        if wt and wt.manager:
            tracker = wt.manager.process_tracker
            uid = tracker.process_owners.get(pid)
            if uid:
                ctx["user_id"] = uid
                info = wt.manager.settings.get(uid, {}) or {}
                ctx["username"] = info.get("username", uid)
                ctx["server_label"] = tracker.user_server.get(uid, "")
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

        if (not ctx.get("username")) and pid in self.process_data:
            data = self.process_data.get(pid, {})
            uid = data.get("user_id", "")
            ctx["user_id"] = uid
            users_cfg = self.config_manager.load_users()
            info = users_cfg.get(uid, {}) if isinstance(users_cfg, dict) else {}
            ctx["username"] = info.get("username", uid)
            ctx["ps_link"] = info.get("private_server_link", "")

        return ctx

    def _handle_ocr_log(self, msg: str):
        if msg == self._last_ocr_log:
            return
        self._last_ocr_log = msg
        self.ocr_log_box.append(msg)
        if self.ocr_log_autoscroll:
            self.ocr_log_box.moveCursor(QTextCursor.MoveOperation.End)

    def _handle_ocr_status(self, status: str):
        status_upper = (status or "").strip().lower()
        if status_upper == "running":
            self.ocr_status_label.setText("Status: Running")
            self.ocr_status_label.setStyleSheet(f"color:{ModernStyle.SECONDARY}; font-weight: bold;")
            self._refresh_ocr_device_label()
        else:
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
        timing_group = QGroupBox("Timing Settings"); timing_layout = QFormLayout(timing_group)
        self.settings_offline_threshold_input = QSpinBox(); self.settings_offline_threshold_input.setRange(10, 120); self.settings_offline_threshold_input.setSuffix(" s")
        timing_layout.addRow("Restart Inactive After:", self.settings_offline_threshold_input)

        self.settings_initial_delay_input = QSpinBox(); self.settings_initial_delay_input.setRange(5, 60); self.settings_initial_delay_input.setSuffix(" s")
        timing_layout.addRow("Initial Launch Delay:", self.settings_initial_delay_input)

        self.settings_launch_delay_input = QSpinBox(); self.settings_launch_delay_input.setRange(1, 120); self.settings_launch_delay_input.setSuffix(" s")
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
        self.kill_after_enable_chk.setChecked(True)
        timeout_layout.addRow("Enable:", self.kill_after_enable_chk)

        # Optional: gray out the timeout field when disabled
        def _toggle_kill_inputs(checked: bool):
            self.kill_timeout_input.setEnabled(checked)
        self.kill_after_enable_chk.toggled.connect(_toggle_kill_inputs)

        self.kill_timeout_input = QSpinBox(); self.kill_timeout_input.setRange(60, 7200); self.kill_timeout_input.setSuffix(" s")
        self.kill_timeout_input.setToolTip("Time until window auto-closes (≤ 1,740s recommended)")
        timeout_layout.addRow("Kill After:", self.kill_timeout_input)

        self.poll_interval_input = QSpinBox(); self.poll_interval_input.setRange(1, 120); self.poll_interval_input.setSuffix(" s")
        self.poll_interval_input.setToolTip("Polling Interval + Kill After must stay under 1,800s")
        timeout_layout.addRow("Poll Interval:", self.poll_interval_input)

        self.webhook_input = QLineEdit(); self.webhook_input.setPlaceholderText("Discord webhook URL")
        timeout_layout.addRow("Webhook URL:", self.webhook_input)

        self.ping_msg_input = QLineEdit(); self.ping_msg_input.setPlaceholderText("Ping message (optional)")
        timeout_layout.addRow("Ping Message:", self.ping_msg_input)
        content_layout.addWidget(timeout_group)

        # ── Webhooks (Per-webhook biome filters) ─────────────────────────────────
        webhooks_group = QGroupBox("Webhooks (Per-Webhook Biome Filters)")
        webhooks_v = QVBoxLayout(webhooks_group)

        info_lbl = QLabel("Each row is a webhook. Choose how each biome should notify. "
                        "No selection = None. Legacy readers still use the allowed list.")
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
        btn_row.addWidget(add_btn); btn_row.addWidget(rem_btn); btn_row.addWidget(route_btn); btn_row.addWidget(cols_btn); btn_row.addStretch()
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
            hint = QLabel("Uncheck biomes to hide their columns in the table.\nHidden columns still load/save and still affect webhooks.")
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
                webhooks = []
                for entry in entries:
                    row_idx = entry["row"]
                    name_item = self.webhooks_table.item(row_idx, 0)
                    url_item  = self.webhooks_table.item(row_idx, 1)
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
                        # If the explicit selection equals "all users", treat it as no filter
                        # and omit the users list entirely.
                        if choice_ids and set(selected_users) == set(choice_ids):
                            explicit_users = False
                            selected_users = []

                    for idx, biome_name in enumerate(GUI_BIOME_NAMES):
                        w = self.webhooks_table.cellWidget(row_idx, 2 + idx)
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

                    entry_dict = {"name": name, "url": url, "biomes": allowed}
                    if biome_modes:
                        entry_dict["biome_modes"] = biome_modes
                    if explicit_users:
                        entry_dict["users"] = selected_users
                        entry_dict["users_explicit"] = True
                    webhooks.append(entry_dict)

                settings["webhooks"] = webhooks
                if self.config_manager.save_settings(settings):
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
        ms_box = QGroupBox("Multiscope — Merchant & Pings")
        ms_form = QFormLayout(ms_box)

        self.ms_merchant_webhook_input = QLineEdit()
        self.ms_merchant_webhook_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.ms_enable_jester = QCheckBox("Enable Jester pings")
        self.ms_enable_mari   = QCheckBox("Enable Mari pings")

        type_opts = ["None", "User ID", "Role ID"]

        self.ms_jester_type = QComboBox(); self.ms_jester_type.addItems(type_opts)
        self.ms_jester_id   = QLineEdit();  self.ms_jester_id.setPlaceholderText("numeric ID or @everyone")
        self.ms_mari_type   = QComboBox();  self.ms_mari_type.addItems(type_opts)
        self.ms_mari_id     = QLineEdit();  self.ms_mari_id.setPlaceholderText("numeric ID or @everyone")

        # tidy two-control rows without custom helpers:
        jester_row = QWidget(); jester_h = QHBoxLayout(jester_row); jester_h.setContentsMargins(0,0,0,0)
        jester_h.addWidget(self.ms_jester_type); jester_h.addWidget(self.ms_jester_id)

        mari_row = QWidget(); mari_h = QHBoxLayout(mari_row); mari_h.setContentsMargins(0,0,0,0)
        mari_h.addWidget(self.ms_mari_type); mari_h.addWidget(self.ms_mari_id)

        ms_form.addRow("Merchant Webhook URL", self.ms_merchant_webhook_input)
        ms_form.addRow(self.ms_enable_jester)
        ms_form.addRow("Jester ping type / ID", jester_row)
        ms_form.addRow(self.ms_enable_mari)
        ms_form.addRow("Mari ping type / ID", mari_row)

        content_layout.addWidget(ms_box)

        # ── Save / Reset ─────────────────────────────────────────────────────────
        buttons_layout = QHBoxLayout()
        save_settings_btn = QPushButton("Save Settings"); save_settings_btn.setProperty("class", "success"); save_settings_btn.clicked.connect(self.save_settings)
        reset_settings_btn = QPushButton("Reset to Defaults"); reset_settings_btn.clicked.connect(self.reset_settings)
        clear_bad_btn = QPushButton("Clear Bad Flags"); clear_bad_btn.clicked.connect(self._clear_bad_flags)
        buttons_layout.addWidget(save_settings_btn); buttons_layout.addWidget(reset_settings_btn); buttons_layout.addWidget(clear_bad_btn); buttons_layout.addStretch()
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
        self.tab_widget.addTab(scroll, "RAM Export")
        
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
                bytes_j = urlopen("https://kyl.neocities.org/jirachi.gif").read()
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
        self.config_manager._create_backup(self.config_manager.users_file)

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
                    if self.replace_ps_chk.isChecked():
                        merged[uid]["private_server_link"] = info.get("private_server_link", "")
                        merged[uid]["place"]               = info.get("place", "")

        if self.config_manager.save_users(merged):
            QMessageBox.information(self, "Success",
                f"Imported {len(new_users)} accounts.\n"
                f"Total users.json entries: {len(merged)}")
            self.add_log("RAM import complete — users.json updated.")
        else:
            QMessageBox.critical(self, "Save Error", "Failed to write users.json!")


    def open_url(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to open URL: {e}")

    def setup_timers(self):

        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.update_ui)
        self.ui_timer.start(1000)  

        self.uptime_timer = QTimer()
        self.uptime_timer.timeout.connect(self.update_uptime)
        self.uptime_timer.start(1000)

    def start_manager(self):
        if self.worker_thread and self.worker_thread.isRunning():
            return

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
            try:
                self.antiafk.toggle_antiafk(True)
            except Exception:
                pass

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Status: Running")
        self.status_label.setStyleSheet(f"color: {ModernStyle.SECONDARY}; font-weight: bold;")
        self.start_time = time.time()


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
            try:
                self.antiafk.toggle_antiafk(False)
            except Exception:
                pass

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Status: Stopped")
        self.status_label.setStyleSheet(f"color: {ModernStyle.ERROR}; font-weight: bold;")
        self.start_time = None

    def update_uptime(self):
        if self.start_time:
            uptime = time.time() - self.start_time
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            seconds = int(uptime % 60)
            self.uptime_label.setText(f"Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")
        else:
            self.uptime_label.setText("Uptime: 00:00:00")

    def update_ui(self):

        active_users = sum(1 for data in self.user_data.values() if data.get('status') == 'Active')
        total_processes = sum(len(data.get('pids', [])) for data in self.user_data.values())
        pending_restarts = sum(1 for data in self.user_data.values() if data.get('needs_restart', False))
        users_cfg = self.config_manager.load_users()
        good = [u for u, i in users_cfg.items() if not i.get("bad") and not i.get("disabled")]

        self.total_users_label.setText(f"{len(good)}")
        self.active_users_label.setText(str(active_users))
        self.total_processes_label.setText(str(total_processes))
        self.pending_restarts_label.setText(str(pending_restarts))

    def update_user_status(self, status_data):
        self.user_data = status_data
        self.refresh_users()

    def update_process_data(self, process_data):
        self.process_data = process_data
        try:
            self._refresh_users_created_column()
        except Exception:
            pass

    def refresh_users(self):
        self.users_table.setRowCount(len(self.user_data))
        users_cfg = self.config_manager.load_users()

        ordered = sorted(
            self.user_data.items(),
            key=lambda kv: (
                bool(users_cfg.get(kv[0], {}).get("bad", False)),
                bool(users_cfg.get(kv[0], {}).get("disabled", False))
            )
        )

        for row, (user_id, runtime) in enumerate(ordered):
            u_conf   = users_cfg.get(user_id, {})
            bad_flag = bool(u_conf.get("bad", False))
            disabled_flag = bool(u_conf.get("disabled", False))

            username  = u_conf.get("username", f"User_{user_id}")
            ps_link   = u_conf.get("private_server_link", "")
            place     = u_conf.get("place", "")
            server    = runtime.get("server", "")

            self.users_table.setItem(row, 0, QTableWidgetItem(user_id))
            self.users_table.setItem(row, 1, QTableWidgetItem(username))

            trimmed_link = ps_link[:25] + "..." if len(ps_link) > 25 else ps_link
            self.users_table.setItem(row, 2, QTableWidgetItem(trimmed_link))
            self.users_table.setItem(row, 3, QTableWidgetItem(place))
            self.users_table.setItem(row, 4, QTableWidgetItem(server))  # NEW

            # status cell
            if bad_flag:
                status_text, colour = "Bad", QColor(ModernStyle.ERROR)
            elif disabled_flag:
                status_text, colour = "Disabled", QColor(ModernStyle.TEXT_SECONDARY)
            else:
                raw = runtime.get("status", "Unknown")
                if "Active" in raw:
                    colour = QColor(ModernStyle.SECONDARY)
                elif "Inactive" in raw:
                    colour = QColor(ModernStyle.WARNING)
                elif "Restarting" in raw:
                    colour = QColor(ModernStyle.PRIMARY)
                else:
                    colour = QColor(ModernStyle.ERROR)
                status_text = raw
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(colour)
            self.users_table.setItem(row, 5, status_item)

            # runtime columns
            pids = runtime.get('pids', []) or []
            if not isinstance(pids, (list, tuple)):
                pids = [pids]
            self.users_table.setItem(row, 6, QTableWidgetItem(', '.join(map(str, pids)) or 'None'))

            ttl_list = runtime.get('ttl', []) or []
            if not isinstance(ttl_list, (list, tuple)):
                ttl_list = [ttl_list]
            self.users_table.setItem(row, 7, QTableWidgetItem(', '.join(f"{t}s" for t in ttl_list) or 'N/A'))

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
            self.users_table.setItem(row, 8, QTableWidgetItem(created_str))

            last_active_str = "Never"
            try:
                last_active_ts = float(runtime.get('last_active', 0) or 0)
                if last_active_ts > 0:
                    last_active_str = datetime.fromtimestamp(last_active_ts).strftime("%H:%M:%S")
            except Exception:
                last_active_str = "Never"
            self.users_table.setItem(row, 9, QTableWidgetItem(last_active_str))

            dur = None
            try:
                inactive_since = float(runtime.get('inactive_since') or 0)
                if inactive_since > 0:
                    dur = int(time.time() - inactive_since)
            except Exception:
                dur = None
            self.users_table.setItem(row, 10, QTableWidgetItem(f"{dur}s" if dur else "N/A"))

            # action buttons
            actions_widget  = QWidget()
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(6, 4, 6, 4)
            actions_layout.setSpacing(6)
            actions_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            restart_btn = QPushButton("Restart")
            restart_btn.clicked.connect(lambda _, uid=user_id: self.restart_user_session(uid))
            actions_layout.addWidget(restart_btn)

            kill_btn = QPushButton("Kill")
            try:
                kill_btn.setProperty("class", "danger")
            except Exception:
                pass
            kill_btn.clicked.connect(lambda _, uid=user_id: self.kill_user_processes(uid))
            actions_layout.addWidget(kill_btn)

            self.users_table.setCellWidget(row, 11, actions_widget)

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
                table.setItem(row, 8, QTableWidgetItem(created_str))
            except Exception:
                continue

    def append_log(self, message: str):
        """Compatibility wrapper so helpers can call parent.append_log()."""
        self.add_log(message)

    def add_log(self, message):
        if message.startswith("[MultiScope] Watch hit") and not self.watch_hit_chk.isChecked():
            return
        if message.startswith("[SCAN-TRACE]") and not self.scan_trace_chk.isChecked():
            return    
        print("add_log():", message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"

        self.log_display.append(formatted_message)
        self.activity_list.append(formatted_message)

        if self.auto_scroll_checkbox.isChecked():
            scrollbar = self.log_display.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        activity_text = self.activity_list.toPlainText()
        lines = activity_text.split('\n')
        if len(lines) > 10:
            self.activity_list.setPlainText('\n'.join(lines[-10:]))

    def clear_logs(self):
        self.log_display.clear()

    def save_logs(self):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"roblox_manager_logs_{timestamp}.txt"

            with open(filename, 'w') as f:
                f.write(self.log_display.toPlainText())

            QMessageBox.information(self, "Success", f"Logs saved to {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save logs: {e}")

    def open_user_management(self):
        dialog = UserManagementDialog(self)
        dialog.exec()

    def open_settings(self):
        idx = self.settings_tab_index if self.settings_tab_index is not None else 5
        self.tab_widget.setCurrentIndex(idx)

    def load_settings_tab(self):
        """Populate Settings UI from settings.json (timings + shutdown monitor + tri-mode webhooks + optional merchant/pings)."""
        settings = self.config_manager.load_settings()

        # ---------- Basic ----------
        self.settings_window_limit_input.setValue(settings.get("window_limit", 1))
        self.spares_mode_chk.setChecked(bool(settings.get("spares_mode", False)))
        self.spares_split_cmb.setCurrentText(settings.get("spares_fraction", "1/2"))

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
        self.webhook_input.setText(tm.get("webhook_url", ""))
        self.ping_msg_input.setText(tm.get("ping_message", ""))
        self.kill_after_enable_chk.setChecked(bool(tm.get("kill_enabled", True)))
        self.kill_timeout_input.setEnabled(self.kill_after_enable_chk.isChecked())

        # ---------- Webhooks table (tri-mode + legacy) ----------
        ui = settings.get("ui", {}) or {}
        if not isinstance(ui, dict):
            ui = {}
        hidden_biomes = ui.get("webhooks_hidden_biomes", []) or []
        if hasattr(self, "_apply_webhook_biome_column_visibility"):
            try:
                self._apply_webhook_biome_column_visibility(hidden_biomes)
            except Exception:
                pass

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

        if hasattr(self, "ms_jester_type"):
            self.ms_jester_type.setCurrentText(ms.get("jester_ping_type", "None"))
        if hasattr(self, "ms_jester_id"):
            self.ms_jester_id.setText(ms.get("jester_ping_id", ""))

        if hasattr(self, "ms_mari_type"):
            self.ms_mari_type.setCurrentText(ms.get("mari_ping_type", "None"))
        if hasattr(self, "ms_mari_id"):
            self.ms_mari_id.setText(ms.get("mari_ping_id", ""))

        # ---------- OCR tab ----------
        self._apply_ocr_settings_to_ui(settings.get("ocr", {}))

    def save_settings(self):
        """Collect Settings UI and persist to settings.json, then live-apply."""
        settings = self.config_manager.load_settings()

        # ---------- Basic ----------
        settings["window_limit"] = self.settings_window_limit_input.value()
        settings["spares_mode"]  = bool(self.spares_mode_chk.isChecked())
        settings["spares_fraction"] = self.spares_split_cmb.currentText()

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
        tm["webhook_url"]   = self.webhook_input.text().strip()
        tm["ping_message"]  = self.ping_msg_input.text().strip()
        settings["timeout_monitor"] = tm

        # ---------- Webhooks table (tri-mode + legacy-compatible) ----------
        try:
            _users_cfg = self.config_manager.load_users() or {}
        except Exception:
            _users_cfg = {}
        all_user_ids = {str(uid) for uid in _users_cfg.keys()} if isinstance(_users_cfg, dict) else set()
        webhooks = []
        rows = self.webhooks_table.rowCount()
        for r in range(rows):
            name_item = self.webhooks_table.item(r, 0)
            url_item  = self.webhooks_table.item(r, 1)
            name = (name_item.text().strip() if name_item else "")
            url = (url_item.text().strip() if url_item else "")
            if not url:
                continue

            allowed = []        # legacy list your worker already uses today
            biome_modes = {}    # new per-biome: "None"/"Message"/"Everyone"
            data_item = name_item or url_item
            user_filter = data_item.data(Qt.ItemDataRole.UserRole) if data_item else None
            selected_users: list[str] = []
            explicit_users = False
            if isinstance(user_filter, (list, tuple, set)):
                explicit_users = True
                selected_users = [str(u).strip() for u in user_filter if str(u).strip()]
                selected_users = sorted({u for u in selected_users})
                # If this explicit selection equals "all users", treat as no filter.
                if all_user_ids and set(selected_users) == all_user_ids:
                    explicit_users = False
                    selected_users = []

            # --- inside save_settings(), in the webhooks loop ---
            for idx, biome_name in enumerate(GUI_BIOME_NAMES):
                w = self.webhooks_table.cellWidget(r, 2 + idx)

                # support wrapped combo (holder) and raw combo
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
                elif hasattr(w, "isChecked") and w.isChecked():  # legacy checkbox fallback
                    allowed.append(str(biome_name).upper())

            entry = {"name": name, "url": url, "biomes": allowed}
            if biome_modes:
                entry["biome_modes"] = biome_modes
            if explicit_users:
                entry["users"] = selected_users
                entry["users_explicit"] = True
            webhooks.append(entry)

        settings["webhooks"] = webhooks

        # ---------- UI: Webhooks column visibility ----------
        ui = settings.get("ui", {}) or {}
        if not isinstance(ui, dict):
            ui = {}
        hidden = getattr(self, "_webhooks_hidden_biomes", set()) or set()
        if not isinstance(hidden, (list, tuple, set)):
            hidden = set()
        ui["webhooks_hidden_biomes"] = sorted({str(b).strip().upper() for b in hidden if str(b).strip()})
        settings["ui"] = ui

        # ---------- Optional: Merchant + Pings (safe if widgets exist) ----------
        ms = settings.get("multiscope", {}) or {}
        if hasattr(self, "ms_merchant_webhook_input"):
            ms["merchant_webhook"] = self.ms_merchant_webhook_input.text().strip()

        if hasattr(self, "ms_enable_jester"):
            ms["enable_jester"] = bool(self.ms_enable_jester.isChecked())
        if hasattr(self, "ms_enable_mari"):
            ms["enable_mari"]   = bool(self.ms_enable_mari.isChecked())

        if hasattr(self, "ms_jester_type"):
            ms["jester_ping_type"] = self.ms_jester_type.currentText()
        if hasattr(self, "ms_jester_id"):
            ms["jester_ping_id"]   = self.ms_jester_id.text().strip()
        if hasattr(self, "ms_mari_type"):
            ms["mari_ping_type"]   = self.ms_mari_type.currentText()
        if hasattr(self, "ms_mari_id"):
            ms["mari_ping_id"]     = self.ms_mari_id.text().strip()

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
            settings["multiscope"] = ms

        # ---------- Persist & live-apply ----------
        if self.config_manager.save_settings(settings):
            if self.worker_thread and self.worker_thread.isRunning():
                self.worker_thread.apply_new_settings(settings)
            if self.ocr_worker and self.ocr_worker.isRunning():
                self.ocr_worker.update_settings(settings.get("ocr", {}), settings.get("multiscope", {}))
            QMessageBox.information(self, "Success", "Settings saved and applied!")
        else:
            QMessageBox.critical(self, "Error", "Failed to save settings.")


    def reset_settings(self):
        """Load the hard-coded defaults from ConfigManager into the UI."""
        defaults = self.config_manager.default_settings          # ← one source of truth
        t        = defaults["timeouts"]                          # short alias

        # ── basic limits ──────────────────────────────────────────
        self.settings_window_limit_input.setValue(defaults["window_limit"])

        # ── launch / restart timings ──────────────────────────────
        self.settings_initial_delay_input.setValue(t["initial_delay"])
        self.settings_launch_delay_input.setValue(t["launch_delay"])
        self.settings_offline_threshold_input.setValue(t["offline"])

        # ── helper / strap limiter ────────────────────────────────
        self.settings_strap_threshold_input.setValue(t["strap_threshold"])

        # ── timeout-monitor block (kill / poll / webhook) ─────────
        self.kill_timeout_input.setValue(t["kill_timeout"])
        self.poll_interval_input.setValue(t["poll_interval"])
        self.webhook_input.setText(t["webhook_url"])
        self.ping_msg_input.setText(t["ping_message"])

        # Reset UI-only settings (column visibility)
        if hasattr(self, "_apply_webhook_biome_column_visibility"):
            try:
                self._apply_webhook_biome_column_visibility([])
            except Exception:
                pass

        # -- OCR --
        self._apply_ocr_settings_to_ui(defaults.get("ocr", {}))
        if self.ocr_worker and self.ocr_worker.isRunning():
            self._stop_ocr_worker()

        QMessageBox.information(
            self,
            "Reset Complete",
            "All settings have been restored to their default values.\n"
            "Click “Save Settings” to confirm them."
        )

    def _clear_bad_flags(self):
        users = self.config_manager.load_users()
        for info in users.values():
            info["bad"] = False
        self.config_manager.save_users(users)
        QMessageBox.information(self, "Done", "All bad-cookie marks cleared.")
        self.refresh_users()                # live update
        self.load_settings_tab()            # if you show counts here

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
                         "J.JARAM (Jirach1's Just Another Roblox Account Manager) JNX 2010\n\n"
                         "Advanced multi-account Roblox session manager\n"
                         "with automated presence monitoring and process management.\n\n"
                         "Built with PyQt6 and modern design principles.\n\n"
                         f"Configuration stored in:\n{config_info['config_dir']}")

    def restart_all_sessions(self):
        if not self.worker_thread or not self.worker_thread.isRunning():
            QMessageBox.warning(self, "Manager Not Running", "Please start the manager first.")
            return

        reply = QMessageBox.question(self, "Confirm Restart",
                                   "Are you sure you want to restart all sessions?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            restartables = [
                user_id
                for user_id, state in self.worker_thread.user_states.items()
                if not state["user_info"].get("bad", False)
            ]

            def delayed_restart():
                for i, user_id in enumerate(restartables):
                    delay = i * self.worker_thread.launcher.launch_delay
                    QTimer.singleShot(delay * 1000, lambda uid=user_id: self.worker_thread.restart_user_session(uid))

            self.add_log(f"Queued restart for {len(restartables)} sessions using delay={self.worker_thread.launcher.launch_delay}s")
            delayed_restart()
            
    def kill_all_processes(self):
        if not self.worker_thread or not self.worker_thread.isRunning():
            QMessageBox.warning(self, "Manager Not Running", "Please start the manager first.")
            return

        reply = QMessageBox.question(self, "Confirm Kill All",
                                   "Are you sure you want to kill ALL Roblox processes?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.worker_thread.kill_all_processes()

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
    def extract_account_cookie_from_browser(self):
        try:
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
                QMessageBox.information(
                    self,
                    "Success",
                    "Cookie extracted successfully!\n\n"
                    "The cookie has been automatically filled in the input field.",
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
            btn.setEnabled(True)
            btn.setText("Login with Browser")

    def on_account_server_type_changed(self):
        if self.account_private_radio.isChecked():
            self.account_place_id_label.hide()
            self.account_place_id.hide()
        else:
            self.account_place_id_label.show()
            self.account_place_id.show()

    def clear_account_form(self):
        self.account_user_id.clear()
        self.account_username.clear()
        self.account_private_link.clear()
        self.account_place_id.clear()
        self.account_cookie.clear()
        self.account_disabled.setChecked(False)
        self.account_private_radio.setChecked(True)
        self.on_account_server_type_changed()

    def add_account(self):
        user_id = self.account_user_id.text().strip()
        username = self.account_username.text().strip()
        private_link = self.account_private_link.text().strip()
        place_id = self.account_place_id.text().strip()
        cookie = self.account_cookie.text().strip()
        disabled = self.account_disabled.isChecked()
        server_type = "private" if self.account_private_radio.isChecked() else "public"

        if not user_id:
            QMessageBox.warning(self, "Error", "User ID is required!")
            return
        if not username:
            username = f"User_{user_id}"
        if not cookie:
            QMessageBox.warning(self, "Error", "Cookie is required!")
            return

        users_config = self.config_manager.load_users()
        if user_id in users_config:
            QMessageBox.warning(self, "Error", f"User {user_id} already exists!")
            return

        account_data = {
            "username": username,
            "server_type": server_type,
            "private_server_link": private_link if server_type == "private" else "",
            "place_id": place_id if server_type == "public" else "",
            "cookie": cookie,
            "disabled": disabled,
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
                server_type = user_info.get("server_type", "private")
                disabled = user_info.get("disabled", False)
                bad_flag = user_info.get("bad", False)
            else:
                username = f"User_{user_id}"
                server_type = "private"
                disabled = False
                bad_flag = False

            self.accounts_list.setItem(row, 0, QTableWidgetItem(user_id))
            self.accounts_list.setItem(row, 1, QTableWidgetItem(username))
            self.accounts_list.setItem(row, 2, QTableWidgetItem(server_type.title()))

            status_text = "Disabled" if disabled else "Enabled"
            if bad_flag:
                status_text = f"{status_text} (bad)"
            status_item = QTableWidgetItem(status_text)
            if disabled:
                status_item.setForeground(QColor("#FF6666"))
            elif bad_flag:
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
        self.account_place_id.setText(user_info.get("place_id", ""))
        self.account_cookie.setText(user_info.get("cookie", ""))
        self.account_disabled.setChecked(user_info.get("disabled", False))
        server_type = user_info.get("server_type", "private")
        if server_type == "public":
            self.account_public_radio.setChecked(True)
        else:
            self.account_private_radio.setChecked(True)
        self.on_account_server_type_changed()
        self.add_account_btn.setText("Update Account")
        try:
            self.add_account_btn.clicked.disconnect()
        except Exception:
            pass
        self.add_account_btn.clicked.connect(lambda: self.update_account(user_id))

    def update_account(self, user_id):
        username = self.account_username.text().strip() or f"User_{user_id}"
        private_link = self.account_private_link.text().strip()
        place_id = self.account_place_id.text().strip()
        cookie = self.account_cookie.text().strip()
        disabled = self.account_disabled.isChecked()
        server_type = "private" if self.account_private_radio.isChecked() else "public"

        if not cookie:
            QMessageBox.warning(self, "Error", "Cookie is required!")
            return
        if server_type == "private" and not private_link:
            QMessageBox.warning(self, "Error", "Private server link is required for private servers!")
            return
        if server_type == "public" and not place_id:
            QMessageBox.warning(self, "Error", "Place ID is required for public servers!")
            return

        users_config = self.config_manager.load_users()
        existing = users_config.get(user_id, {})
        account_data = {
            "username": username,
            "server_type": server_type,
            "private_server_link": private_link if server_type == "private" else "",
            "place_id": place_id if server_type == "public" else "",
            "cookie": cookie,
            "disabled": disabled,
            "bad": existing.get("bad", False),
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
            QMessageBox.critical(self, "Error", "Failed to update account!")

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
                    self._stop_ocr_worker()
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
                try:
                    self._unregister_auto_item_hotkey()
                except Exception:
                    pass
                event.accept()
            else:
                event.ignore()
        else:
            if self.ocr_worker and self.ocr_worker.isRunning():
                self._stop_ocr_worker()
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
            try:
                self._unregister_auto_item_hotkey()
            except Exception:
                pass
            event.accept()
    
    def setup_multiscope_tab(self):
        multiscope_widget = QWidget()
        layout = QVBoxLayout(multiscope_widget)

        self.multiscope_table = QTableWidget()
        self.multiscope_table.setColumnCount(8)
        self.multiscope_table.setHorizontalHeaderLabels([
            "Server", "Users", "In-Menu", "Last Biome", "Biome Age", "Last Merchant", "Merchant Age", "Events"
        ])
        header = self.multiscope_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        self.multiscope_table.verticalHeader().setDefaultSectionSize(44)

        layout.addWidget(self.multiscope_table)

        hint = QLabel(
            "Multiscope groups accounts by the exact server they’re in.\n"
            "Biome and merchant alerts persist across handoffs."
        )
        hint.setStyleSheet(f"color: {ModernStyle.TEXT_SECONDARY};")
        layout.addWidget(hint)

        self.tab_widget.addTab(multiscope_widget, "Multiscope")


    def update_multiscope(self, rows: list):
        # rows: [{server, users, in_menu, last_biome|biome, biome_age, last_merchant|merchant, merchant_age, events}]
        try:
            with self._ms_biome_lock:
                self._ms_biome_by_server = {
                    str(row.get("server", "") or ""): str(row.get("last_biome", row.get("biome", "")) or "").strip().upper()
                    for row in (rows or [])
                    if str(row.get("server", "") or "").strip()
                }
                self._ms_in_menu_by_server = {}
                for row in (rows or []):
                    server = str(row.get("server", "") or "").strip()
                    if not server:
                        continue
                    val = row.get("in_menu", None)
                    self._ms_in_menu_by_server[server] = None if val is None else bool(val)
        except Exception:
            pass

        self.multiscope_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            server = row.get("server", "")
            users_list = row.get("users", [])
            users = ", ".join(users_list) if users_list else ""
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

            self.multiscope_table.setItem(r, 0, QTableWidgetItem(server))
            self.multiscope_table.setItem(r, 1, QTableWidgetItem(users))
            self.multiscope_table.setItem(r, 2, QTableWidgetItem(in_menu_txt))
            self.multiscope_table.setItem(r, 3, QTableWidgetItem(last_biome))
            self.multiscope_table.setItem(r, 4, QTableWidgetItem(f"{biome_age}s" if biome_age is not None else ""))
            self.multiscope_table.setItem(r, 5, QTableWidgetItem(last_merchant))
            self.multiscope_table.setItem(r, 6, QTableWidgetItem(f"{merchant_age}s" if merchant_age is not None else ""))
            self.multiscope_table.setItem(r, 7, QTableWidgetItem(events))

    def test_multiscope_webhooks(self):
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
    app = QApplication(sys.argv)

    app.setApplicationName("JARAM")
    app.setApplicationVersion("1.1")
    app.setOrganizationName("cresqnt")

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
