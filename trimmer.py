import time
import threading
import queue
from multiprocessing import get_context

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


class TrimmerWorker:
    def __init__(self, log_callback):
        """log_callback(msg: str) -> None"""
        self.log_callback = log_callback
        self._mp_ctx = get_context("spawn")
        self._process = None
        self._stop_event = None
        self._config_queue = None
        self._log_queue = None
        self._log_thread = None

    def start(self):
        if self._process and self._process.is_alive():
            return
        try:
            from trimmer_mp_worker import run_trimmer_worker
        except Exception as e:
            self._safe_log(f"[ERROR] Failed to import trimmer worker: {e!r}")
            return

        try:
            self._stop_event = self._mp_ctx.Event()
            self._config_queue = self._mp_ctx.Queue()
            self._log_queue = self._mp_ctx.Queue()

            self._process = self._mp_ctx.Process(
                target=run_trimmer_worker,
                args=(self._config_queue, self._log_queue, self._stop_event),
                daemon=True,
            )
            self._process.start()

            self._log_thread = threading.Thread(target=self._forward_logs, daemon=True)
            self._log_thread.start()
        except Exception as e:
            self._safe_log(f"[ERROR] Failed to start trimmer worker process: {e!r}")
            self._process = None
            self._stop_event = None
            self._config_queue = None
            self._log_queue = None
            self._log_thread = None
            return

        self._safe_log("[INFO] Trimmer worker started.")

    def stop(self):
        try:
            if self._stop_event is not None:
                self._stop_event.set()
        except Exception:
            pass
        try:
            if self._config_queue is not None:
                self._config_queue.put_nowait(None)
        except Exception:
            pass

        if self._process and self._process.is_alive():
            self._process.join(timeout=2.0)
            if self._process.is_alive():
                try:
                    self._process.terminate()
                except Exception:
                    pass
                self._process.join(timeout=2.0)

        try:
            if self._log_queue is not None:
                self._log_queue.put_nowait(None)
        except Exception:
            pass
        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=1.0)

        for q in (self._config_queue, self._log_queue):
            try:
                if q is not None:
                    q.close()
            except Exception:
                pass
            try:
                if q is not None:
                    q.cancel_join_thread()
            except Exception:
                pass

        self._process = None
        self._stop_event = None
        self._config_queue = None
        self._log_queue = None
        self._log_thread = None

        self._safe_log("[INFO] Trimmer worker stopped.")

    def _safe_log(self, msg: str):
        try:
            self.log_callback(msg)
        except Exception:
            pass

    def update_config(self, enabled: bool, interval_s: float, threshold_mb):
        if self._config_queue is None:
            return
        msg = {
            "type": "config",
            "enabled": bool(enabled),
            "interval_s": float(interval_s),
            "threshold_mb": threshold_mb,
        }
        try:
            self._config_queue.put_nowait(msg)
        except Exception:
            try:
                self._config_queue.put(msg, timeout=0.1)
            except Exception:
                pass

    def _forward_logs(self):
        while True:
            try:
                if self._log_queue is None:
                    break
                item = self._log_queue.get(timeout=0.2)
            except queue.Empty:
                if self._process is not None and not self._process.is_alive():
                    break
                continue
            except Exception:
                break

            if item is None:
                break
            self._safe_log(str(item))


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
            log_callback=self._queue_log,
        )

        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._flush_logs)
        self._log_timer.start(200)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_settings_from_ui)

        if _native_trim_targets is None:
            self.enabled_chk.setEnabled(False)
            self._append_log(
                "[ERROR] Native RAM trimmer module `ram_limiter_native` is not available. "
                "Build it from `native/` and place the `.pyd` next to `trimmer.py`."
            )
            self._update_status()
            return

        self.worker.start()
        try:
            enabled, interval_s, threshold = self._get_current_config()
            self.worker.update_config(enabled, interval_s, threshold)
        except Exception:
            pass
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

        try:
            worker = getattr(self, "worker", None)
            if worker is not None:
                threshold = threshold_mb if use_threshold else None
                worker.update_config(enabled, interval_s, threshold)
        except Exception:
            pass

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
