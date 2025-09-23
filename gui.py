import sys
import json
import time
import os
import shutil
import requests
import psutil
import re
from typing import Dict, Set
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                            QHBoxLayout, QGridLayout, QTabWidget, QTableWidget,
                            QTableWidgetItem, QPushButton, QLabel, QLineEdit,
                            QSpinBox, QTextEdit, QGroupBox,QStackedLayout,
                            QProgressBar, QComboBox, QCheckBox, QSplitter,
                            QHeaderView, QMessageBox, QDialog, QDialogButtonBox,
                            QFormLayout, QScrollArea, QFrame, QSizePolicy,
                            QAbstractItemView, QHeaderView, QScrollArea)
from PyQt6.QtCore import QTimer, QThread, pyqtSignal, Qt, QSize,  QBuffer, QByteArray, QIODevice, QRectF, QPointF
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor, QPixmap, QPainter, QMovie, QRegion, QPainterPath
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
from log_utils import find_log_for_username, R_DISC_REASON, R_DISC_NOTIFY, R_DISC_SENDING, R_CONN_LOST
from biomes import biome_names
from utilities_tab import setup_UTILITIES_tab
# Exclude NORMAL from the Settings table (still exists internally, we just don't offer it as a toggle)
GUI_BIOME_NAMES = [b for b in biome_names() if str(b).upper() != "NORMAL"]
from multiscope import MultiScopeEngine


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

        }


        self.default_user_structure = {
            "username": "",
            "cookie": "",
            "private_server_link": "",
            "place": "",
            "bad": False
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
                    "place": ""
                }
            elif isinstance(user_info, dict):

                new_data[user_id] = {
                    "username": user_info.get("username", f"User_{user_id}"),
                    "cookie": user_info.get("cookie", ""),
                    "private_server_link": user_info.get("private_server_link", ""),
                    "place": user_info.get("place", ""),
                    "bad":  user_info.get("bad", False)
                }
            else:

                new_data[user_id] = {
                    "username": f"User_{user_id}",
                    "cookie": "",
                    "private_server_link": "",
                    "place": "",
                    "bad":  ""
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
                    "place": ""
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
        
        self._last_good_set = set()  # tracks which users are currently 'good' (bad == False)
        self._reservations_ttl = 60  # seconds a server is "held" by a handoff pre-join
        self.preconnect_grace = 120  # seconds to wait for username to show in logs on first connect
        self._waiting_usernames_since = {}  # uid -> epoch; cleaned up automatically

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
        # Prefer live user_states (reflects in-memory bad flips immediately)
        if getattr(self, "user_states", None):
            source_items = [
                (uid, st.get("user_info", {}))
                for uid, st in self.user_states.items()
            ]
        else:
            # Fallback during very early init
            source_items = list(self.manager.settings.items())

        good = [uid for uid, info in source_items if not info.get("bad", False)]
        good_sorted = sorted(good)

        # NEW:
        if self.spares_mode:
            n = len(good_sorted)
            # ceil(n * num/den) without importing math
            target_active = max(1, (n * self._spares_num + self._spares_den - 1) // self._spares_den)
        else:
            target_active = len(good_sorted)

        self.active_pool = set(good_sorted[:target_active])
        self.spare_pool  = set(good_sorted[target_active:])

        self._log(
            f"Pools set — spares_mode={self.spares_mode} active={len(self.active_pool)} spare={len(self.spare_pool)}"
        )


    def _eligible_spares(self):
        now = time.time()
        for uid in sorted(self.spare_pool):
            st = self.user_states.get(uid)
            # Skip if bad in live state OR in settings (covers recent flips + reloads)
            is_bad_live = bool(st and st.get("user_info", {}).get("bad", False))
            is_bad_cfg  = bool(self.manager.settings.get(uid, {}).get("bad", False))
            if is_bad_live or is_bad_cfg:
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

        # first launch: either half (active_pool) or everyone
        if self.spares_mode:
            for i, uid in enumerate(sorted(self.active_pool)):
                info = self.user_states[uid]["user_info"]
                if info.get("bad", False):
                    continue
                cookie = info.get("cookie", "")
                self.launcher.start_game_session(uid, cookie, info, skip_cleanup=True)
                self.user_states[uid]["last_launch"] = time.time()
                if i < len(self.active_pool) - 1:
                    time.sleep(self.initial_delay)
        else:
            self.launcher.initialize_all_sessions(self.manager.settings)
            now = time.time()
            for uid in self.user_states:
                self.user_states[uid]["last_launch"] = now

        while self.running:
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
                        "user_info": info,
                        "requires_restart": False,
                        "status": "Initializing",
                    }

                # 3) Update info for existing users (this brings in new "bad" flags)
                for uid in (new_ids & old_ids):
                    self.user_states[uid]["user_info"] = fresh_map[uid]

                # 4) Swap the snapshot and recompute pools
                self.manager.settings = fresh_map
                self._recompute_pools()           # pools depend on "bad" flags in settings
                if self.ms:
                    self.ms.update_users(list(self.user_states.keys()))
            # ---- end hot reload ----


            # housekeeping
            if now - self.timing_trackers['cleanup'] >= self.manager.check_intervals['cleanup']:
                self.process_mgr.cleanup_dead_processes(self.manager.process_tracker)
                self.timing_trackers['cleanup'] = now
            if now - self.timing_trackers['window'] >= self.manager.check_intervals['window']:
                for pid, nwin in self.process_mgr.count_windows_by_process().items():
                    if nwin > self.manager.window_limit and pid != self.manager.excluded_pid:
                        self.process_mgr.terminate_process(pid, self.manager.process_tracker)
                self.timing_trackers['window'] = now
            
            # After housekeeping, before relaunch logic
            self._enforce_one_per_server()
            self._prune_reservations()

            # --- sync bad flags + evict from pools immediately ---
            try:
                changed = False
                for uid, cfg_info in self.manager.settings.items():
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
                        donors = [d for d, s in self.handoff_for.items() if s == uid]
                        for d in donors:
                            self.handoff_for.pop(d, None)
            except Exception as _e:
                self._log(f"[Sync] bad-flag sync error: {_e}")

            # If the good-set changed, rebuild pools from the live state (keeps odd/even split correct)
            try:
                current_good = {
                    u for u, s in self.user_states.items()
                    if not s.get("user_info", {}).get("bad", False)
                }
                if current_good != getattr(self, "_last_good_set", set()):
                    self._last_good_set = current_good
                    self._recompute_pools()
            except Exception as _e:
                self._log(f"[Pools] good-set recompute error: {_e}")


            # low-count watchdog
            STUCK_TIMEOUT = 300
            total_users = len([u for u, i in self.manager.settings.items() if not i.get("bad", False)])
            active_processes = sum(
                1 for p in psutil.process_iter(['name','pid'])
                if p.info['name'] == 'RobloxPlayerBeta.exe' and p.info['pid'] != self.manager.excluded_pid
            )
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

            for uid, st in self.user_states.items():
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
                
                live = [pid for pid in self.manager.process_tracker.user_processes.get(uid, []) if self.process_mgr.verify_process_active(pid)]
                
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

                        # (optional) remember we're waiting; handy for trace logs
                        # self._waiting_usernames_since.setdefault(uid, oldest_ct)

                        if waited >= self.preconnect_grace:
                            self._log(f"⚠️  {uname} did not appear in logs within {self.preconnect_grace}s — terminating")
                            self.kill_user_processes(uid)
                            st["requires_restart"] = True
                            # clean up a bit so next launch is fresh
                            self.log_pointers.pop(uid, None)
                            self.manager.process_tracker.user_server[uid] = "DISCONNECTED"
                            # (optional) stop tracking the wait
                            # self._waiting_usernames_since.pop(uid, None)
                            # Skip the rest of the log-based disconnect checks for this uid this tick
                            continue
                    else:
                        # (optional) we *did* see the username; stop tracking waits
                        # self._waiting_usernames_since.pop(uid, None)
                        pass
                # --- END new watchdog -----------------------------------------------------


                # disconnect via logs
                uname = str(info.get("username", "")).lower()
                log_path = find_log_for_username(uname, allow_fallback=False)
                if live and log_path and os.path.isfile(log_path):
                    try:
                        last_pos   = self.log_pointers.get(uid, 0)
                        cur_sz     = os.path.getsize(log_path)
                        if cur_sz < last_pos:
                            last_pos = cur_sz
                        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                            f.seek(last_pos)
                            chunk = f.read()
                        self.log_pointers[uid] = cur_sz
                        if chunk:
                            for line in chunk.splitlines():
                                pos_net = line.lower().find("[flog::network]")
                                pos_txt = line.lower().find("text:")
                                if pos_txt != -1 and pos_txt < pos_net:
                                    continue
                                if (m := R_DISC_REASON.search(line)) or (m := R_DISC_NOTIFY.search(line)) or (m := R_DISC_SENDING.search(line)) or R_CONN_LOST.search(line):
                                    self._log(f"⚠️  {uname} disconnect detected – terminating")
                                    self.kill_user_processes(uid)
                                    st["requires_restart"] = True
                                    live = []
                                    break
                    except Exception as e:
                        self._trace(uid, f"log scan error: {e}")

                # TTLs
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

                # normal state
                if live:
                    st["last_active"] = now
                    st["inactive_since"] = None
                    st["requires_restart"] = False
                    st["status"] = "Active"
                else:
                    if st["inactive_since"] is None:
                        st["inactive_since"] = now
                    idle = now - st["inactive_since"]
                    st["status"] = f"Inactive ({int(idle)} s)"
                    if idle >= self.restart_threshold:
                        st["requires_restart"] = True

                status[uid] = {
                    "status": st["status"],
                    "pids": live,
                    "needs_restart": st["requires_restart"],
                    "last_active": st.get("last_active", 0),
                    "inactive_since": st.get("inactive_since"),
                    "ttl": ttl_list,
                    "server": self.manager.process_tracker.user_server.get(uid, ""),
                }

                # pre-join spare
                if (self.spares_mode and uid in self.active_pool and min_ttl is not None
                    and uid not in self.handoff_for and not info.get("bad", False)):
                    if min_ttl <= self.handoff_lead or min_ttl <= self.early_join_window:
                        self._launch_spare_into_donors_server(uid, info)
            
            # --- detect changes to the good-set and rebuild pools on-the-fly ---
            try:
                current_good = {
                    u for u, s in self.user_states.items()
                    if not s.get("user_info", {}).get("bad", False)
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
                    rows = self.ms.snapshot()            # [{server, users, biome/merchant…}]
                    self.multiscope_signal.emit(rows)    # GUI will render it
            except Exception as _e:
                self._log(f"[Multiscope] tick error: {_e}")


            # process table signal
            proc_info = {}
            for uid, pids in self.manager.process_tracker.user_processes.items():
                for pid in pids:
                    if not self.process_mgr.verify_process_active(pid):
                        continue
                    created = datetime.fromtimestamp(self.manager.process_tracker.creation_timestamps.get(pid, time.time())).strftime("%H:%M:%S")
                    windows = self.process_mgr.count_windows_by_process().get(pid, 0)
                    proc_info[pid] = {"user_id": uid, "created": created, "windows": windows}
            self.process_signal.emit(proc_info)


            # auto-restart queue (skip donors in handoff)
            restartables = [
                u for u, s in self.user_states.items()
                if s.get("requires_restart")
                and not s["user_info"].get("bad", False)
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
                # per-user launch delay (prefer not to hammer)
                if now - st.get("last_launch", 0) < self.manager.timeouts["launch_delay"]:
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

            # Only attempt one successful launch per launch_delay window (global pacing)
            if ordered and (now - self.timing_trackers['relaunch']) >= self.manager.timeouts["launch_delay"]:
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

            time.sleep(self.manager.check_intervals['main_tick'])
    
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
        if self.manager and self.manager.timeout_monitor:
            self.manager.timeout_monitor.stop()
        self.running = False

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


        
class RobloxManagerGUI(QMainWindow):

    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self.process_data = {}
        self.config_manager = ConfigManager()
        self.setup_ui()
        # NEW: add the Multiscope tab
        self.setup_multiscope_tab()
        self.setup_timers()


    def setup_ui(self):
        self.setWindowTitle("Jirach1 + JARAM - Just Another Roblox Account Manager")
        self.setGeometry(100, 100, 1200, 800)

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
        self.setup_processes_tab()
        self.setup_logs_tab()
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

        self.tab_widget.addTab(dashboard_widget, "Dashboard")

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
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Fixed)

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

        self.tab_widget.addTab(users_widget, "Users")


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

        self.tab_widget.addTab(processes_widget, "Processes")

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

        self.tab_widget.addTab(logs_widget, "Logs")

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
        self.webhooks_table.setColumnCount(1 + len(GUI_BIOME_NAMES))  # URL + biomes...
        headers = ["Webhook URL"] + GUI_BIOME_NAMES
        self.webhooks_table.setHorizontalHeaderLabels(headers)
        self.webhooks_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        header = self.webhooks_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # Make biome columns wide enough so the combobox text is visible when closed
        header.setMinimumSectionSize(150)
        vh = self.webhooks_table.verticalHeader()
        vh.setDefaultSectionSize(30)   # good-looking row height
        vh.setMinimumSectionSize(30)   # prevents squeeze below readable height

        for c in range(1, 1 + len(GUI_BIOME_NAMES)):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.webhooks_table.setColumnWidth(c, 150)
        webhooks_v.addWidget(self.webhooks_table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Webhook")
        rem_btn = QPushButton("Remove Selected")
        btn_row.addWidget(add_btn); btn_row.addWidget(rem_btn); btn_row.addStretch()
        webhooks_v.addLayout(btn_row)

        MODE_ITEMS = ("None", "Message", "Everyone")  # tri-mode per biome cell

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


        def add_webhook_row(url: str = "", allowed_biomes=None, biome_modes=None):
            """
            allowed_biomes: Optional[List[str]]  -> kept for backward compatibility
            biome_modes:    Optional[Dict[str, str]] per-biome: "None"|"Message"|"Everyone"
                            If provided, it overrides the mode derived from allowed_biomes.
            """
            row = self.webhooks_table.rowCount()
            self.webhooks_table.insertRow(row)

            url_item = QTableWidgetItem(url)
            self.webhooks_table.setItem(row, 0, url_item)

            allowed_set = {str(b).upper() for b in (allowed_biomes or [])}
            biome_modes = biome_modes or {}

            for idx, biome in enumerate(GUI_BIOME_NAMES):
                bkey = str(biome).upper()
                default_mode = "Message" if bkey in allowed_set else "None"
                mode = biome_modes.get(bkey, default_mode)
                combo = _mk_mode_combo(mode)
                _place_centered(self.webhooks_table, row, 1 + idx, combo)

            # Center + size the row based on the first combo’s height
            first_holder = self.webhooks_table.cellWidget(row, 1)  # wrapper at first biome col
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

        add_btn.clicked.connect(lambda: add_webhook_row("", []))
        rem_btn.clicked.connect(remove_selected_rows)

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
        self.tab_widget.addTab(settings_widget, "Settings")
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
        self.tab_widget.addTab(tab, "RAM Export")
        
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
            holder.setText(fallback)
            holder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder.setStyleSheet(
                f"color:{ModernStyle.TEXT_SECONDARY};"
                f"border:1px dashed {ModernStyle.PRIMARY};"
            )

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
        try:
            bytes_j = Path(__file__).with_name("jirachi.gif").read_bytes()
        except FileNotFoundError:
            bytes_j = urlopen("https://kyl.neocities.org/jirachi.gif").read()

        developer_layout.addWidget(self._make_dev_card("Jirach1", bytes_j))

        # — cresqnt —
        try:
            bytes_c = Path(__file__).with_name("cresqnt.gif").read_bytes()
        except FileNotFoundError:
            bytes_c = urlopen("https://media1.tenor.com/m/CNBGgG2DU10AAAAd/nyan-cat-poptart.gif").read()

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

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Status: Running")
        self.status_label.setStyleSheet(f"color: {ModernStyle.SECONDARY}; font-weight: bold;")
        self.start_time = time.time()


    def stop_manager(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.stop()
            self.worker_thread.wait()

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
        good = [u for u, i in users_cfg.items() if not i.get("bad")]

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

        ordered = sorted(self.user_data.items(), key=lambda kv: bool(users_cfg.get(kv[0], {}).get("bad", False)))

        for row, (user_id, runtime) in enumerate(ordered):
            u_conf   = users_cfg.get(user_id, {})
            bad_flag = bool(u_conf.get("bad", False))

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
        self.tab_widget.setCurrentIndex(4)

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
            url     = wh.get("url", "")
            allowed = wh.get("biomes", []) or []      # legacy list your worker already uses
            modes   = wh.get("biome_modes", {}) or {} # optional per-biome tri-state
            if url:
                try:
                    self._add_webhook_row(url, allowed, modes)
                except TypeError:
                    self._add_webhook_row(url, allowed)

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
        webhooks = []
        rows = self.webhooks_table.rowCount()
        for r in range(rows):
            url_item = self.webhooks_table.item(r, 0)
            url = (url_item.text().strip() if url_item else "")
            if not url:
                continue

            allowed = []        # legacy list your worker already uses today
            biome_modes = {}    # new per-biome: "None"/"Message"/"Everyone"

            # --- inside save_settings(), in the webhooks loop ---
            for idx, biome_name in enumerate(GUI_BIOME_NAMES):
                w = self.webhooks_table.cellWidget(r, 1 + idx)

                # support wrapped combo (holder) and raw combo
                cb = None
                if isinstance(w, QComboBox):
                    cb = w
                elif hasattr(w, "findChild"):
                    cb = w.findChild(QComboBox)

                if cb is not None:
                    mode = cb.currentText()
                    biome_modes[str(biome_name).upper()] = mode
                    if mode in ("Message", "Everyone"):
                        allowed.append(str(biome_name).upper())
                elif hasattr(w, "isChecked") and w.isChecked():  # legacy checkbox fallback
                    allowed.append(str(biome_name).upper())

            entry = {"url": url, "biomes": allowed}
            if biome_modes:
                entry["biome_modes"] = biome_modes
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
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
    
    def setup_multiscope_tab(self):
        multiscope_widget = QWidget()
        layout = QVBoxLayout(multiscope_widget)

        self.multiscope_table = QTableWidget()
        self.multiscope_table.setColumnCount(7)
        self.multiscope_table.setHorizontalHeaderLabels([
            "Server", "Users", "Last Biome", "Biome Age", "Last Merchant", "Merchant Age", "Events"
        ])
        header = self.multiscope_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
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
        # rows: [{server, users, last_biome|biome, biome_age, last_merchant|merchant, merchant_age, events}]
        self.multiscope_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            server = row.get("server", "—")
            users_list = row.get("users", [])
            users = ", ".join(users_list) if users_list else "—"

            # accept both key styles
            last_biome = row.get("last_biome", row.get("biome", "—"))
            biome_age = row.get("biome_age")
            last_merchant = row.get("last_merchant", row.get("merchant", "—"))
            merchant_age = row.get("merchant_age")
            events = str(row.get("events", 0))

            self.multiscope_table.setItem(r, 0, QTableWidgetItem(server))
            self.multiscope_table.setItem(r, 1, QTableWidgetItem(users))
            self.multiscope_table.setItem(r, 2, QTableWidgetItem(last_biome))
            self.multiscope_table.setItem(r, 3, QTableWidgetItem(f"{biome_age}s" if biome_age is not None else "—"))
            self.multiscope_table.setItem(r, 4, QTableWidgetItem(last_merchant))
            self.multiscope_table.setItem(r, 5, QTableWidgetItem(f"{merchant_age}s" if merchant_age is not None else "—"))
            self.multiscope_table.setItem(r, 6, QTableWidgetItem(events))

            
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