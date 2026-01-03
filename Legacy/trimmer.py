import os
import time
import threading
import ctypes
from ctypes import wintypes
import queue

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

try:
    from ram_limiter_native import trim_targets as _native_trim_targets  # type: ignore

    _USING_NATIVE = True
except Exception:  # pragma: no cover
    _native_trim_targets = None
    _USING_NATIVE = False

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QTextEdit,
    QScrollArea,
)
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont


TARGET_PROCESS_NAME = "RobloxPlayerBeta.exe"


_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_SET_QUOTA = 0x0100

    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    EmptyWorkingSet = psapi.EmptyWorkingSet
    EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    EmptyWorkingSet.restype = wintypes.BOOL
else:
    kernel32 = None
    psapi = None
    OpenProcess = None
    CloseHandle = None
    EmptyWorkingSet = None


def _open_process_for_trimming(pid: int):
    if not _IS_WINDOWS:
        return None
    access = PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION
    handle = OpenProcess(access, False, pid)
    if not handle:
        return None
    return handle


def _trim_working_set(pid: int) -> bool:
    if not _IS_WINDOWS:
        return False
    handle = _open_process_for_trimming(pid)
    if not handle:
        return False
    try:
        return bool(EmptyWorkingSet(handle))
    finally:
        try:
            CloseHandle(handle)
        except Exception:
            pass


def _human_mb(bytes_val: int) -> float:
    return float(bytes_val) / (1024.0 * 1024.0)


class TrimmerWorker:
    def __init__(self, get_config_callback, log_callback):
        """
        get_config_callback() -> (enabled: bool, interval_s: float, threshold_mb: float | None)
        log_callback(msg: str) -> None
        """
        self.get_config_callback = get_config_callback
        self.log_callback = log_callback
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._safe_log("[INFO] Trimmer worker started.")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._safe_log("[INFO] Trimmer worker stopped.")

    def _safe_log(self, msg: str):
        try:
            self.log_callback(msg)
        except Exception:
            pass

    def _sleep_interruptible(self, total_s: float):
        end_time = time.time() + max(0.1, float(total_s))
        while time.time() < end_time and not self._stop_event.is_set():
            time.sleep(0.1)

    def _run_loop(self):
        if not _IS_WINDOWS:
            self._safe_log("[ERROR] RAM trimmer requires Windows.")
            return

        while not self._stop_event.is_set():
            try:
                enabled, interval_s, threshold_mb = self.get_config_callback()
            except Exception as e:
                self._safe_log(f"[ERROR] Failed to read config: {e!r}")
                enabled, interval_s, threshold_mb = False, 15.0, None

            if not enabled:
                self._sleep_interruptible(0.5)
                continue

            try:
                self._clean_targets(threshold_mb)
            except Exception as e:
                self._safe_log(f"[ERROR] Unexpected error in trim loop: {e!r}")

            self._sleep_interruptible(interval_s)

    def _clean_targets(self, threshold_mb):
        if _native_trim_targets is not None:
            try:
                for line in _native_trim_targets(TARGET_PROCESS_NAME, threshold_mb):
                    self._safe_log(line)
                return
            except Exception as e:
                self._safe_log(f"[WARN] Native RAM trimmer failed, falling back: {e!r}")

        if psutil is None:
            self._safe_log("[ERROR] psutil is required for the pure-Python trimmer fallback.")
            return

        target_name = TARGET_PROCESS_NAME.lower()

        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name != target_name:
                    continue

                before_info = proc.info.get("memory_info")
                before_rss = getattr(before_info, "rss", 0) or 0
                before_mb = _human_mb(before_rss)

                if threshold_mb is not None and before_mb < float(threshold_mb):
                    self._safe_log(
                        f"[SKIP] {proc.info.get('name', '?')} (PID {proc.pid}) "
                        f"{before_mb:.1f} MB (< {float(threshold_mb):.1f} MB threshold)"
                    )
                    continue

                if not _trim_working_set(proc.pid):
                    self._safe_log(
                        f"[FAIL] Could not trim {proc.info.get('name', '?')} (PID {proc.pid})"
                    )
                    continue

                time.sleep(0.05)
                after_rss = proc.memory_info().rss
                after_mb = _human_mb(after_rss)
                freed_mb = before_mb - after_mb

                self._safe_log(
                    f"[OK]   {proc.info.get('name', '?')} (PID {proc.pid}): "
                    f"{before_mb:.1f} -> {after_mb:.1f} MB "
                    f"(freed {freed_mb:.1f} MB)"
                )

            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                self._safe_log(
                    f"[WARN] Skipping PID {getattr(proc, 'pid', '?')}: {e.__class__.__name__}"
                )
                continue
            except OSError as e:
                self._safe_log(f"[WARN] OSError while scanning processes: {e}")
                continue
            except Exception as e:
                self._safe_log(
                    f"[WARN] Unexpected error while scanning "
                    f"(PID {getattr(proc, 'pid', '?')}): {e!r}"
                )
                continue


class TrimmerTab(QWidget):
    SETTINGS_KEY = "trimmer"
    LEGACY_SETTINGS_KEYS = ("limiter", "roblox_ram_limiter")
    DEFAULTS = {
        "enabled": False,
        "interval_s": 15,
        "use_threshold": True,
        "threshold_mb": 1024.0,
    }

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self._config_manager = config_manager
        self._config_lock = threading.Lock()
        self._config = {
            "enabled": False,
            "interval_s": 15.0,
            "use_threshold": True,
            "threshold_mb": 1024.0,
        }

        self.log_queue = queue.Queue()

        self._build_ui()
        self._load_settings_into_ui()
        self._update_config_snapshot()

        self.worker = TrimmerWorker(
            get_config_callback=self._get_current_config,
            log_callback=self._queue_log,
        )

        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._flush_logs)
        self._log_timer.start(200)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_settings_from_ui)

        if not _IS_WINDOWS:
            self.enabled_chk.setEnabled(False)
            self._append_log("[ERROR] Roblox RAM trimmer is Windows-only.")
        else:
            self.worker.start()
            self._update_status()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        header = QLabel("Trimmer")
        header.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        layout.addWidget(header)

        settings_group = QGroupBox("Settings")
        settings_layout = QHBoxLayout(settings_group)

        enabled_col = QVBoxLayout()
        self.enabled_chk = QCheckBox("Enabled")
        enabled_col.addWidget(self.enabled_chk)
        enabled_col.addStretch(1)
        settings_layout.addLayout(enabled_col)

        settings_layout.addSpacing(16)

        interval_col = QVBoxLayout()
        interval_col.addWidget(QLabel("Trim interval (seconds):"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 3600)
        self.interval_spin.setValue(15)
        interval_col.addWidget(self.interval_spin)
        settings_layout.addLayout(interval_col)

        settings_layout.addSpacing(16)

        threshold_col = QVBoxLayout()
        self.threshold_chk = QCheckBox("Use threshold (MB >=)")
        self.threshold_chk.setChecked(True)
        threshold_col.addWidget(self.threshold_chk)
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(1.0, 65536.0)
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setValue(1024.0)
        threshold_col.addWidget(self.threshold_spin)
        settings_layout.addLayout(threshold_col)

        settings_layout.addStretch(1)
        layout.addWidget(settings_group)

        self.status_lbl = QLabel("Status: Disabled")
        layout.addWidget(self.status_lbl)

        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 10))
        self.log_box.setMinimumHeight(200)
        log_layout.addWidget(self.log_box)
        layout.addWidget(log_group)

        layout.addStretch(1)

        self.interval_spin.valueChanged.connect(self._on_ui_changed)
        self.threshold_chk.toggled.connect(self._on_ui_changed)
        self.threshold_spin.valueChanged.connect(self._on_ui_changed)
        self.enabled_chk.toggled.connect(self._on_enabled_toggled)

    def _append_log(self, msg: str):
        try:
            self.log_box.append(str(msg))
        except Exception:
            pass

    def _queue_log(self, msg: str):
        try:
            timestamp = time.strftime("%H:%M:%S")
            self.log_queue.put(f"[{timestamp}] {msg}")
        except Exception:
            pass

    def _flush_logs(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(msg)

    def _update_config_snapshot(self):
        enabled = bool(self.enabled_chk.isChecked())
        interval_s = float(self.interval_spin.value())
        use_threshold = bool(self.threshold_chk.isChecked())
        threshold_mb = float(self.threshold_spin.value())

        with self._config_lock:
            self._config["enabled"] = enabled
            self._config["interval_s"] = interval_s
            self._config["use_threshold"] = use_threshold
            self._config["threshold_mb"] = threshold_mb

        self.threshold_spin.setEnabled(use_threshold)

    def _get_current_config(self):
        with self._config_lock:
            enabled = bool(self._config["enabled"])
            interval_s = float(self._config["interval_s"])
            use_threshold = bool(self._config["use_threshold"])
            threshold_mb = float(self._config["threshold_mb"])

        threshold = threshold_mb if use_threshold else None
        return enabled, interval_s, threshold

    def _on_ui_changed(self):
        self._update_config_snapshot()
        try:
            self._save_timer.start(300)
        except Exception:
            self._save_settings_from_ui()

    def _on_enabled_toggled(self, _checked: bool):
        self._on_ui_changed()
        self._update_status()

    def _update_status(self):
        try:
            enabled = bool(self.enabled_chk.isChecked())
        except Exception:
            enabled = False
        self.status_lbl.setText("Status: Enabled" if enabled else "Status: Disabled")

    def _load_settings_into_ui(self):
        cfg = {}
        migrated = False
        try:
            if self._config_manager is not None:
                settings = self._config_manager.load_settings() or {}
                new_cfg = settings.get(self.SETTINGS_KEY, {}) or {}
                legacy_cfgs = [settings.get(k, None) for k in self.LEGACY_SETTINGS_KEYS]

                cfg = new_cfg if isinstance(new_cfg, dict) else {}
                if self._is_default_cfg(cfg):
                    for legacy_cfg in legacy_cfgs:
                        if isinstance(legacy_cfg, dict):
                            cfg = legacy_cfg
                            migrated = True
                            break
        except Exception:
            cfg = {}

        enabled = bool(cfg.get("enabled", False))
        interval_s = int(cfg.get("interval_s", 15) or 15)
        use_threshold = bool(cfg.get("use_threshold", True))
        threshold_mb = float(cfg.get("threshold_mb", 1024.0) or 1024.0)

        try:
            self.enabled_chk.blockSignals(True)
            self.interval_spin.blockSignals(True)
            self.threshold_chk.blockSignals(True)
            self.threshold_spin.blockSignals(True)

            self.enabled_chk.setChecked(enabled)
            self.interval_spin.setValue(max(1, min(3600, interval_s)))
            self.threshold_chk.setChecked(use_threshold)
            self.threshold_spin.setValue(max(1.0, min(65536.0, threshold_mb)))
            self.threshold_spin.setEnabled(use_threshold)
        finally:
            try:
                self.enabled_chk.blockSignals(False)
                self.interval_spin.blockSignals(False)
                self.threshold_chk.blockSignals(False)
                self.threshold_spin.blockSignals(False)
            except Exception:
                pass
        if migrated:
            self._save_settings_from_ui()

    def _save_settings_from_ui(self):
        if self._config_manager is None:
            return

        try:
            settings = self._config_manager.load_settings() or {}
        except Exception:
            settings = {}

        settings[self.SETTINGS_KEY] = {
            "enabled": bool(self.enabled_chk.isChecked()),
            "interval_s": int(self.interval_spin.value()),
            "use_threshold": bool(self.threshold_chk.isChecked()),
            "threshold_mb": float(self.threshold_spin.value()),
        }
        for legacy_key in self.LEGACY_SETTINGS_KEYS:
            try:
                settings.pop(legacy_key, None)
            except Exception:
                pass
        try:
            self._config_manager.save_settings(settings)
        except Exception:
            pass

    def _is_default_cfg(self, cfg: dict) -> bool:
        try:
            d = dict(self.DEFAULTS or {})
            return (
                bool(cfg.get("enabled", d["enabled"])) == bool(d["enabled"])
                and int(cfg.get("interval_s", d["interval_s"])) == int(d["interval_s"])
                and bool(cfg.get("use_threshold", d["use_threshold"])) == bool(d["use_threshold"])
                and float(cfg.get("threshold_mb", d["threshold_mb"])) == float(d["threshold_mb"])
            )
        except Exception:
            return True

    def shutdown(self):
        try:
            self.worker.stop()
        except Exception:
            pass


def setup_TRIMMER_tab(main_window):
    tab = TrimmerTab(getattr(main_window, "config_manager", None))
    main_window.trimmer_tab = tab

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(tab)

    main_window.tab_widget.addTab(scroll, "Trimmer")
