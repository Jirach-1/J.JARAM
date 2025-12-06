import sys
import json
import time
import os
import shutil
import requests
import psutil
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
                            QSpinBox, QDoubleSpinBox, QTextEdit, QGroupBox, QStackedLayout,
                            QProgressBar, QComboBox, QCheckBox, QSplitter,
                            QHeaderView, QMessageBox, QDialog, QDialogButtonBox,
                            QFormLayout, QScrollArea, QFrame, QSizePolicy,
                            QAbstractItemView, QHeaderView, QScrollArea, QRubberBand,
                            QRadioButton, QListWidget, QListWidgetItem)
from PyQt6.QtCore import QTimer, QThread, pyqtSignal, Qt, QSize,  QBuffer, QByteArray, QIODevice, QRectF, QPointF, QRect, QPoint
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor, QPixmap, QPainter, QMovie, QRegion, QPainterPath, QImage, QTextCursor
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
)

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
def _bm_relaxed() -> bool:
    try:
        if os.environ.get("JARAM_UNLOCK", "").strip() == "1":
            return True
    except Exception:
        pass
    try:
        # Check for the sentinel both in the current working directory and, if bundled,
        # alongside the frozen executable (PyInstaller exposes sys._MEIPASS).
        candidates = [Path("JARAM.biu")]
        try:
            import sys as _sys
            if getattr(_sys, "_MEIPASS", None):
                candidates.append(Path(getattr(_sys, "_MEIPASS")) / "JARAM.biu")
        except Exception:
            pass
        for sentinel in candidates:
            try:
                if sentinel.exists():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False

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
                "roi": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
                "color_filters": [
                    {"name": "white_text", "r": 255, "g": 255, "b": 255, "tol": 40, "enabled": True},
                    {"name": "purple_text", "r": 145, "g": 67, "b": 255, "tol": 40, "enabled": True},
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

    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self.ocr_worker: Optional[OCRWorker] = None
        self.ocr_roi: Optional[Tuple[float, float, float, float]] = None
        self._last_ocr_log: Optional[str] = None
        self.ocr_log_autoscroll: bool = True
        self._loading_ocr_settings = False
        self._loading_antiafk_settings = False
        self.settings_tab_index: Optional[int] = None
        self.process_data = {}
        self.config_manager = ConfigManager()

        # Anti-AFK engine instance (configured in setup_antiafk_tab)
        self.antiafk: Optional[AntiAFK] = None
        self.antiafk_status_box: Optional[QTextEdit] = None

        # Connect Anti-AFK cross-thread signals
        self.antiafk_log_signal.connect(self._on_antiafk_status)
        self.antiafk_state_signal.connect(self._on_antiafk_state_changed)

        self.setup_ui()
        # NEW: add the Multiscope tab
        self.setup_multiscope_tab()
        self.setup_timers()


    def setup_ui(self):
        self.setWindowTitle("Jirach1 + JARAM - Just Another Roblox Account Manager")
        self.setGeometry(100, 100, 1100, 720)

        icon_path = _get_icon_path()
        if icon_path and os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        header_layout = QHBoxLayout()

        title_label = QLabel("Jirach1 + JARAM - Just Another Roblox Account Manager")
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
        self.setup_processes_tab()
        self.setup_logs_tab()
        self.setup_ocr_tab()
        self.setup_antiafk_tab()
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
        self.users_table.setColumnCount(11)
        self.users_table.setHorizontalHeaderLabels([
            "User ID","Username","Private Server","Place",
            "Server",               # ← NEW
            "Status","PIDs","TTL(s)","Last Active",
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
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(10, QHeaderView.ResizeMode.Fixed)

        self.users_table.setColumnWidth(2, 200)
        self.users_table.setColumnWidth(3, 100)
        self.users_table.setColumnWidth(4, 120)
        self.users_table.setColumnWidth(7, 100)
        self.users_table.setColumnWidth(9, 160)
        self.users_table.setColumnWidth(10, 170)
        self.users_table.verticalHeader().setDefaultSectionSize(60)

        layout.addWidget(self.users_table)

        controls_layout = QHBoxLayout()
        refresh_users_btn = QPushButton("Refresh")
        refresh_users_btn.clicked.connect(self.refresh_users)
        controls_layout.addWidget(refresh_users_btn)

        add_user_btn = QPushButton("Modify Users")
        add_user_btn.clicked.connect(self.open_user_management)
        controls_layout.addWidget(add_user_btn)

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
        form_layout.addWidget(self.account_cookie)

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

    def setup_processes_tab(self):
        processes_widget = QWidget()
        layout = QVBoxLayout(processes_widget)

        self.processes_table = QTableWidget()
        self.processes_table.setColumnCount(5)
        self.processes_table.setHorizontalHeaderLabels([
            "PID", "User ID", "Created", "Windows", "Actions"
        ])

        header = self.processes_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)  
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)  

        self.processes_table.setColumnWidth(2, 100)  
        self.processes_table.setColumnWidth(4, 110)  

        self.processes_table.verticalHeader().setDefaultSectionSize(60)

        layout.addWidget(self.processes_table)

        controls_layout = QHBoxLayout()

        refresh_processes_btn = QPushButton("Refresh")
        refresh_processes_btn.clicked.connect(self.refresh_processes)
        controls_layout.addWidget(refresh_processes_btn)

        kill_selected_btn = QPushButton("Kill Selected")
        kill_selected_btn.setProperty("class", "danger")
        kill_selected_btn.clicked.connect(self.kill_selected_process)
        controls_layout.addWidget(kill_selected_btn)

        controls_layout.addStretch()

        layout.addLayout(controls_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(processes_widget)
        self.tab_widget.addTab(scroll, "Processes")

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

        # Show whether OCR preprocessing is using CPU or GPU
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

        controls_group = QGroupBox("Capture & OCR")
        controls_form = QFormLayout(controls_group)

        self.ocr_workers_spin = QSpinBox(); self.ocr_workers_spin.setRange(1, 16)
        self.ocr_workers_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_max_caps_spin = QSpinBox(); self.ocr_max_caps_spin.setRange(1, 60)
        self.ocr_max_caps_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_cooldown_spin = QSpinBox(); self.ocr_cooldown_spin.setRange(30, 7200); self.ocr_cooldown_spin.setSuffix(" s")
        self.ocr_cooldown_spin.valueChanged.connect(self._on_ocr_settings_changed)
        self.ocr_preprocess_chk = QCheckBox("Use preprocessing")
        self.ocr_preprocess_chk.toggled.connect(self._on_ocr_settings_changed)

        controls_form.addRow("OCR workers:", self.ocr_workers_spin)
        controls_form.addRow("Max captures / sec:", self.ocr_max_caps_spin)
        controls_form.addRow("Cooldown per PID:", self.ocr_cooldown_spin)
        controls_form.addRow("Preprocess chat image:", self.ocr_preprocess_chk)

        layout.addWidget(controls_group)

        btn_row = QHBoxLayout()
        calibrate_btn = QPushButton("Calibrate chat area")
        calibrate_btn.clicked.connect(self.calibrate_ocr_roi)
        preview_btn = QPushButton("Test preview")
        preview_btn.clicked.connect(self.show_ocr_preview)
        btn_row.addWidget(calibrate_btn)
        btn_row.addWidget(preview_btn)
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
        header = self.ocr_filter_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(1, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.ocr_filter_table.setColumnWidth(col, 80)
        self.ocr_filter_table.setColumnWidth(10, 150)
        vh = self.ocr_filter_table.verticalHeader()
        vh.setDefaultSectionSize(30)
        vh.setMinimumSectionSize(30)
        filters_layout.addWidget(self.ocr_filter_table)
        # React immediately when filter cells change (name/colors/tolerance/enabled)
        self.ocr_filter_table.itemChanged.connect(lambda _item: self._on_ocr_settings_changed())

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

        log_group = QGroupBox("OCR Log")
        log_layout = QVBoxLayout(log_group)
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
        controls_row = QHBoxLayout()
        controls_row.addWidget(self.ocr_auto_scroll_chk)
        controls_row.addStretch()
        controls_row.addWidget(clear_log_btn)
        log_layout.addLayout(controls_row)
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

        # Also forward to the global log view
        try:
            self.add_log(f"[Anti-AFK] {message}")
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
                # snapshot already normalizes None -> True, but keep the intent:
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

    def _add_filter_row(self, name: str = "white_text", r: int = 255, g: int = 255, b: int = 255, tol: int = 40, enabled: bool = True):
        row = self.ocr_filter_table.rowCount()
        self.ocr_filter_table.insertRow(row)

        enabled_item = QTableWidgetItem()
        enabled_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        enabled_item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self.ocr_filter_table.setItem(row, 0, enabled_item)

        self.ocr_filter_table.setItem(row, 1, QTableWidgetItem(str(name)))
        self.ocr_filter_table.setItem(row, 2, QTableWidgetItem(str(r)))
        self.ocr_filter_table.setItem(row, 3, QTableWidgetItem(str(g)))
        self.ocr_filter_table.setItem(row, 4, QTableWidgetItem(str(b)))
        self.ocr_filter_table.setItem(row, 5, QTableWidgetItem(str(tol)))

    def _remove_selected_filter_rows(self):
        rows = sorted({idx.row() for idx in self.ocr_filter_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.ocr_filter_table.removeRow(r)

    def _load_color_filters_table(self, filters: List[dict]):
        self.ocr_filter_table.setRowCount(0)
        defaults = (self.config_manager.default_settings.get("ocr", {}) or {}).get("color_filters", [])
        for f in filters or defaults:
            self._add_filter_row(
                f.get("name", ""),
                int(f.get("r", 0)),
                int(f.get("g", 0)),
                int(f.get("b", 0)),
                int(f.get("tol", 0)),
                bool(f.get("enabled", True)),
            )

    def _current_color_filters(self, as_dataclass: bool = False):
        filters = []
        rows = self.ocr_filter_table.rowCount()

        def _get_int(item: QTableWidgetItem, default: int = 0) -> int:
            try:
                return int(item.text())
            except Exception:
                return default

        for r in range(rows):
            enabled_item = self.ocr_filter_table.item(r, 0)
            name_item = self.ocr_filter_table.item(r, 1)
            r_item = self.ocr_filter_table.item(r, 2)
            g_item = self.ocr_filter_table.item(r, 3)
            b_item = self.ocr_filter_table.item(r, 4)
            tol_item = self.ocr_filter_table.item(r, 5)

            enabled = enabled_item.checkState() == Qt.CheckState.Checked if enabled_item else True
            name = name_item.text().strip() if name_item else ""
            rv = _get_int(r_item, 0) if r_item else 0
            gv = _get_int(g_item, 0) if g_item else 0
            bv = _get_int(b_item, 0) if b_item else 0
            tol = _get_int(tol_item, 0) if tol_item else 0

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

        # Reflect current CPU/GPU preprocessing device in the OCR tab
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
        """Update the OCR device label with current CPU/GPU status."""
        try:
            summary = get_ocr_device_summary()
        except Exception:
            summary = "Unknown (error checking Torch/Kornia)"
        self.ocr_device_label.setText(f"OCR Preprocessing Device: {summary}")

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
        # Save settings
        try:
            settings = self.config_manager.load_settings()
        except Exception:
            settings = self.config_manager.default_settings.copy()
        settings["ocr"] = self._get_ocr_settings_from_ui()
        try:
            self.config_manager.save_settings(settings)
        except Exception:
            pass
        # Live-apply to worker if running
        self._sync_ocr_worker_settings()

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

        header = self.webhooks_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # Make biome columns wide enough so the combobox text is visible when closed
        header.setMinimumSectionSize(150)
        vh = self.webhooks_table.verticalHeader()
        vh.setDefaultSectionSize(30)   # good-looking row height
        vh.setMinimumSectionSize(30)   # prevents squeeze below readable height

        for c in range(2, 2 + len(GUI_BIOME_NAMES)):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.webhooks_table.setColumnWidth(c, 150)
        webhooks_v.addWidget(self.webhooks_table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Webhook")
        rem_btn = QPushButton("Remove Selected")
        route_btn = QPushButton("Assign Users...")
        btn_row.addWidget(add_btn); btn_row.addWidget(rem_btn); btn_row.addWidget(route_btn); btn_row.addStretch()
        webhooks_v.addLayout(btn_row)

        MODE_ITEMS = ("None", "Message", "Everyone")  # tri-mode per biome cell

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

            # Height: slightly taller so it clears gridlines and looks centered
            cmb.setMinimumHeight(10)
            cmb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            cmb.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

            # Tighten vertical padding a bit; keep arrow space
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
            v.setContentsMargins(0, 1, 0, 1)   # tiny top/bottom breathing room
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
                if bkey in ("GLITCHED", "DREAMSPACE") and not _bm_relaxed():
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
                            if str(biome_name).upper() in ("GLITCHED", "DREAMSPACE") and not _bm_relaxed():
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

        # expose helpers for load/save
        self._add_webhook_row = add_webhook_row
        self._clear_webhook_rows = lambda: self.webhooks_table.setRowCount(0)

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

        discord_btn2 = QPushButton("https://discord.gg/YPvhKFTjEF")
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
        discord_btn2.clicked.connect(lambda: self.open_url("https://discord.gg/YPvhKFTjEF"))
        support_layout2.addWidget(discord_btn2)

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
        self.refresh_processes()

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
            pids = runtime.get('pids', [])
            self.users_table.setItem(row, 6, QTableWidgetItem(', '.join(map(str, pids)) or 'None'))

            ttl_list = runtime.get('ttl', [])
            self.users_table.setItem(row, 7, QTableWidgetItem(', '.join(f"{t}s" for t in ttl_list) or 'N/A'))

            last_active = runtime.get('last_active', 0)
            last_active_str = datetime.fromtimestamp(last_active).strftime("%H:%M:%S") if last_active else "Never"
            self.users_table.setItem(row, 8, QTableWidgetItem(last_active_str))

            inactive_since = runtime.get('inactive_since')
            dur = int(time.time() - inactive_since) if inactive_since else None
            self.users_table.setItem(row, 9, QTableWidgetItem(f"{dur}s" if dur else "N/A"))

            # action buttons
            actions_widget  = QWidget()
            actions_layout  = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(8, 5, 8, 5)
            actions_layout.setSpacing(12)

            restart_btn = QPushButton("Restart")
            restart_btn.setStyleSheet(self._get_action_button_style("primary"))
            restart_btn.clicked.connect(lambda _, uid=user_id: self.restart_user_session(uid))
            actions_layout.addWidget(restart_btn)

            kill_btn = QPushButton("Kill")
            kill_btn.setStyleSheet(self._get_action_button_style("danger"))
            kill_btn.clicked.connect(lambda _, uid=user_id: self.kill_user_processes(uid))
            actions_layout.addWidget(kill_btn)

            self.users_table.setCellWidget(row, 10, actions_widget)


    def refresh_processes(self):
        self.processes_table.setRowCount(len(self.process_data))

        for row, (pid, data) in enumerate(self.process_data.items()):
            self.processes_table.setItem(row, 0, QTableWidgetItem(str(pid)))
            self.processes_table.setItem(row, 1, QTableWidgetItem(data.get('user_id', 'Unknown')))
            self.processes_table.setItem(row, 2, QTableWidgetItem(data.get('created', 'Unknown')))

            windows = data.get('windows', 0)
            windows_item = QTableWidgetItem(str(windows))
            if windows > 1:
                windows_item.setForeground(QColor(ModernStyle.WARNING))
            self.processes_table.setItem(row, 3, windows_item)

            actions_widget = QWidget()
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(8, 5, 8, 5)

            kill_btn = QPushButton("Kill")
            kill_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {ModernStyle.ERROR};
                    color: {ModernStyle.TEXT_PRIMARY};
                    border: none;
                    padding: 4px 8px;
                    border-radius: 4px;
                    font-weight: 500;
                    font-size: 11px;
                    min-width: 50px;
                    max-width: 60px;
                    min-height: 26px;
                    max-height: 28px;
                }}
                QPushButton:hover {{
                    background-color: 
                }}
            """)
            kill_btn.clicked.connect(lambda checked, p=pid: self.kill_specific_process(p))
            actions_layout.addWidget(kill_btn)

            self.processes_table.setCellWidget(row, 4, actions_widget)
    
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
                    if str(biome_name).upper() in ("GLITCHED", "DREAMSPACE") and not _bm_relaxed():
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
        QMessageBox.about(self, "About JARAM",
                         "JARAM X Jirach1(Just Another Roblox Account Manager) v1.1\n\n"
                         "Advanced multi-account Roblox session manager\n"
                         "with automated presence monitoring and process management.\n\n"
                         "Built with PyQt6 and modern design principles.\n\n"
                         "Jirach1 was here.\n\n"
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
        if server_type == "private" and not private_link:
            QMessageBox.warning(self, "Error", "Private server link is required for private servers!")
            return
        if server_type == "public" and not place_id:
            QMessageBox.warning(self, "Error", "Place ID is required for public servers!")
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

            action_cell = QWidget()
            action_layout = QHBoxLayout(action_cell)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(4)

            edit_btn = QPushButton("Edit")
            edit_btn.setStyleSheet(
                "QPushButton {background-color:%s; color:white; border:none; padding:2px 4px; border-radius:3px; font-size:8px; font-weight:bold; min-width:50px; max-width:80px; min-height:18px; max-height:22px;} QPushButton:hover {background-color:%s;}"
                % (ModernStyle.PRIMARY, ModernStyle.PRIMARY_VARIANT)
            )
            edit_btn.clicked.connect(lambda _, uid=user_id: self.edit_account(uid))
            action_layout.addWidget(edit_btn)

            action_layout.addStretch()
            self.accounts_list.setCellWidget(row, 4, action_cell)

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

    def kill_user_processes(self, user_id):
        if not self.worker_thread or not self.worker_thread.isRunning():
            QMessageBox.warning(self, "Manager Not Running", "Please start the manager first.")
            return

        reply = QMessageBox.question(self, "Confirm Kill",
                                   f"Are you sure you want to kill processes for user {user_id}?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.worker_thread.kill_user_processes(user_id)

    def kill_specific_process(self, pid):
        if not self.worker_thread or not self.worker_thread.isRunning():
            QMessageBox.warning(self, "Manager Not Running", "Please start the manager first.")
            return

        reply = QMessageBox.question(self, "Confirm Kill",
                                   f"Are you sure you want to kill process {pid}?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            if self.worker_thread.process_mgr:
                self.worker_thread.process_mgr.terminate_process(
                    int(pid), self.worker_thread.manager.process_tracker
                )

    def kill_selected_process(self):
        current_row = self.processes_table.currentRow()
        if current_row >= 0:
            pid_item = self.processes_table.item(current_row, 0)
            if pid_item:
                pid = pid_item.text()
                self.kill_specific_process(pid)

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
        self.multiscope_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            server = row.get("server", "")
            users_list = row.get("users", [])
            users = ", ".join(users_list) if users_list else ""
            in_menu_val = row.get("in_menu")
            in_menu_txt = "True" if in_menu_val is None else ("True" if in_menu_val else "False")

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
    main()
