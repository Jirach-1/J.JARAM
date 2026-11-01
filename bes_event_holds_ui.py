"""BES tab controls for timed biome and merchant holds."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from bes_event_holds import normalize_event_holds, trigger_catalog


class BESEventHoldsWidget(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Biome / Merchant Hold BES", parent)
        self.setObjectName("besEventHolds")
        self.setStyleSheet("""
            QGroupBox#besEventHolds QTableWidget {
                background: #202020; alternate-background-color: #292929;
                color: #fafafa; gridline-color: #383838;
            }
            QGroupBox#besEventHolds QTableWidget::item { padding: 0 12px; }
            QGroupBox#besEventHolds QWidget[cellContainer="true"] { background: transparent; }
            QGroupBox#besEventHolds QCheckBox[tableToggle="true"] {
                background: transparent; margin: 0; padding: 0; spacing: 0;
            }
            QGroupBox#besEventHolds QCheckBox[tableToggle="true"]::indicator {
                width: 18px; height: 18px; border: 2px solid #71717a;
                border-radius: 4px; background: #27272a;
            }
            QGroupBox#besEventHolds QCheckBox[tableToggle="true"]::indicator:checked {
                background: #6366f1; border-color: #818cf8;
            }
            QGroupBox#besEventHolds QCheckBox[tableToggle="true"]::indicator:disabled {
                background: #27272a; border-color: #3f3f46;
            }
        """)
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox("Enable biome / merchant holds")
        layout.addWidget(self.enabled)
        hint = QLabel("Keep the finder account unthrottled for selected events. "
                      "Biomes use their catalog timer; merchants last 3 minutes. BES must be enabled.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.checkboxes = {}
        self.tables = {}
        self._timed_events = set()
        self._extra_events = {}
        for category, title in (("Biome", "Biomes"), ("Merchant", "Merchants")):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            buttons = QHBoxLayout()
            buttons.addStretch()
            for caption, checked in (("Select all", True), ("Clear", False)):
                button = QPushButton(caption)
                button.clicked.connect(lambda _=False, cat=category, on=checked: self._select(cat, on))
                buttons.addWidget(button)
            page_layout.addLayout(buttons)
            catalog = [row for row in trigger_catalog() if row[0].startswith(category + ":")]
            table = QTableWidget(len(catalog), 3)
            table.setHorizontalHeaderLabels(["Event", "Duration", "Hold BES"])
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            table.verticalHeader().hide()
            table.verticalHeader().setDefaultSectionSize(38)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            table.setAlternatingRowColors(True)
            table.setMinimumHeight(190)
            for row, (trigger, name, duration) in enumerate(catalog):
                table.setItem(row, 0, QTableWidgetItem(name.title()))
                table.setItem(row, 1, QTableWidgetItem(f"{duration} s" if duration else "No timer"))
                checkbox = QCheckBox()
                checkbox.setProperty("tableToggle", True)
                checkbox.setFixedSize(22, 22)
                checkbox.setAccessibleName(f"Hold BES for {name.title()}")
                checkbox.setEnabled(bool(duration))
                if duration:
                    self._timed_events.add(trigger)
                if not duration:
                    checkbox.setToolTip("This biome has no timer in biomes.json.")
                checkbox.toggled.connect(self.changed.emit)
                self.checkboxes[trigger] = checkbox
                container = QWidget()
                container.setProperty("cellContainer", True)
                cell = QHBoxLayout(container)
                cell.setContentsMargins(0, 0, 0, 0)
                cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.addWidget(checkbox)
                table.setCellWidget(row, 2, container)
            page_layout.addWidget(table)
            self.tables[category] = table
            self.tabs.addTab(page, title)
        self.enabled.toggled.connect(self.tabs.setEnabled)
        self.enabled.toggled.connect(self.changed.emit)
        self.tabs.setEnabled(False)

    def _select(self, category, checked):
        for trigger, checkbox in self.checkboxes.items():
            if trigger.startswith(category + ":") and checkbox.isEnabled():
                checkbox.setChecked(checked)

    def set_config(self, config):
        config = normalize_event_holds(config)
        self.blockSignals(True)
        try:
            self._extra_events = {key: value for key, value in config["events"].items() if key not in self.checkboxes}
            self.enabled.setChecked(config["enabled"])
            for trigger, checkbox in self.checkboxes.items():
                checkbox.setChecked(config["events"].get(trigger, False) and trigger in self._timed_events)
        finally:
            self.blockSignals(False)

    def config(self):
        return {"enabled": self.enabled.isChecked(),
                "events": dict(self._extra_events, **{key: field.isChecked() for key, field in self.checkboxes.items()})}
