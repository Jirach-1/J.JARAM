"""Live Multiscope diagnostics; consumes snapshots without querying the worker."""
from collections import deque
import copy
import time

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                               QPushButton, QGroupBox, QTabWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView,
                               QTextEdit, QCheckBox, QSplitter, QScrollArea, QFrame, QApplication)
from multiscope_metrics import TIMING_DESCRIPTIONS, TIMING_LABELS


ISSUE_LABELS = {
    'tracked_without_log': 'Tracked without an attached log',
    'requested_missing_from_engine': 'Requested but not tracked',
    'live_missing_from_request': 'Running but not requested',
    'live_missing_from_user_states': 'Running but absent from manager state',
    'live_missing_from_snapshot': 'Running but absent from server snapshot',
    'engine_not_requested': 'Tracked after removal from request',
}


def duration(milliseconds):
    if milliseconds is None:
        return '—'
    return f'{milliseconds/1000:.2f} s' if milliseconds >= 1000 else f'{milliseconds:.3f} ms' if milliseconds < 1 else f'{milliseconds:.1f} ms'


class MultiscopePanel(QWidget):
    export_requested = Signal()

    def __init__(self, servers_table, *, running=None, parent=None):
        super().__init__(parent)
        self._running = running or (lambda: False)
        self._report = {}
        self._received_at = None
        self._dirty = True
        self._logs = deque(maxlen=5000)
        self._card_columns = None
        self._split_initialized = False
        self._user_split_adjusted = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.page_scroll = QScrollArea()
        self.page_scroll.setAccessibleName('Multiscope page')
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.page_content = QWidget()
        self.page_scroll.setWidget(self.page_content)
        outer.addWidget(self.page_scroll)
        layout = QVBoxLayout(self.page_content)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        title_layout = QVBoxLayout()
        title_layout.setSpacing(3)
        title = QLabel('Multiscope diagnostics')
        title.setStyleSheet('font-size: 17px; font-weight: 600;')
        title_layout.addWidget(title)
        self.freshness = QLabel('Waiting for manager diagnostics')
        self.freshness.setWordWrap(True)
        self.freshness.setStyleSheet('color: #b4b4bd; font-size: 12px;')
        title_layout.addWidget(self.freshness)
        heading.addLayout(title_layout, 1)
        heading.addStretch()
        save = QPushButton('Save diagnostics + logs')
        self._compact_button(save)
        save.setToolTip('Export the latest diagnostic snapshot, timing samples and Multiscope log as JSON.')
        save.clicked.connect(self.export_requested)
        heading.addWidget(save)
        layout.addLayout(heading)
        self._card_layout = QGridLayout()
        self._card_layout.setSpacing(8)
        self._card_groups = []
        self.cards = {}
        for i, name in enumerate(('Reader', 'Accounts', 'Webhooks', 'Outcomes')):
            group = QGroupBox(name)
            card_layout = QVBoxLayout(group)
            card_layout.setContentsMargins(10, 16, 10, 8)
            label = QLabel('—')
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setStyleSheet('font-size: 12px;')
            card_layout.addWidget(label)
            self._card_groups.append(group)
            self.cards[name] = label
        self._arrange_cards()
        layout.addLayout(self._card_layout)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)
        self.splitter.setToolTip('Drag the divider to resize diagnostics and logs.')
        self.splitter.splitterMoved.connect(self._remember_split_adjustment)
        self.details = QTabWidget()
        self.details.setMinimumHeight(260)
        self.details.tabBar().setStyleSheet('QTabBar::tab { padding: 7px 16px; }')
        diagnostic_page = QWidget()
        diagnostics = QVBoxLayout(diagnostic_page)
        diagnostics.setContentsMargins(8, 6, 8, 6)
        diagnostics.setSpacing(5)
        self.notes = QLabel('Start the manager to collect live diagnostics.')
        self.notes.setWordWrap(True)
        diagnostics.addWidget(self.notes)
        self.timings = QTableWidget(len(TIMING_LABELS), 6)
        self.timings.setHorizontalHeaderLabels(['Timing', 'Last', 'p50', 'p95', 'Max', 'Samples'])
        self._configure_table(self.timings)
        self.timings.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            self.timings.setColumnWidth(column, 82)
        self.timings.setMinimumHeight(140)
        self.timings.setStyleSheet('QTableWidget { alternate-background-color: #303034; font-size: 13px; } '
                                  'QTableWidget::item { padding: 2px 8px; } '
                                  'QHeaderView::section { padding: 6px 8px; }')
        self.timings.setAlternatingRowColors(True)
        self.timings.horizontalHeaderItem(0).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.timings.horizontalHeader().setToolTip('p50: typical time · p95: 95% of samples are at or below this time · Max: slowest sample in the window')
        for row, (key, label) in enumerate(TIMING_LABELS.items()):
            self._cell(self.timings, row, 0, label)
            for column in range(1, 6):
                self._cell(self.timings, row, column, '—')
                self.timings.item(row, column).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.timings.item(row, 0).setToolTip(TIMING_DESCRIPTIONS.get(key, label))
        diagnostics.addWidget(self.timings, 1)
        explanation = QLabel('Latest 512 samples per stage · p50 median · p95 95th percentile · — no samples')
        explanation.setToolTip('Log timestamp age includes buffering and clock differences, not disk-write latency. '
                               'Acknowledgement timings cover successful sends; failed/expired alerts appear in Outcomes. '
                               'HTTP acknowledgement is not message display. '
                               'Reader batch waiting overlaps worker I/O and parsing; do not add the stages. '
                               'Hover over a timing name for its sampling unit and scope.')
        explanation.setStyleSheet('font-size: 11px; color: #b4b4bd;')
        explanation.setWordWrap(True)
        self.timing_explanation = explanation
        diagnostics.addWidget(explanation)
        self.details.addTab(servers_table, 'Servers')
        self.details.addTab(diagnostic_page, 'Timings')
        issue_page = QWidget()
        issue_layout = QVBoxLayout(issue_page)
        self.issue_caption = QLabel('Account tracking issues: waiting for diagnostics')
        self.issue_caption.setWordWrap(True)
        issue_layout.addWidget(self.issue_caption)
        self.issues = QTableWidget(0, 3)
        self.issues.setHorizontalHeaderLabels(['Issue', 'Username', 'User ID'])
        self._configure_table(self.issues)
        for column in range(3):
            self.issues.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.issues.setMinimumHeight(80)
        issue_layout.addWidget(self.issues, 1)
        self.details.addTab(issue_page, 'Account issues')
        self.splitter.addWidget(self.details)

        log_group = QWidget()
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(5)
        options = QHBoxLayout()
        log_title = QLabel('Multiscope log')
        log_title.setStyleSheet('font-weight: 600;')
        options.addWidget(log_title)
        options.addStretch()
        self.biome_logs = QCheckBox('Biome activity')
        self.biome_logs.setToolTip('Include future biome activity messages. Delivery warnings always appear.')
        self.biome_logs.setChecked(False)
        options.addWidget(self.biome_logs)
        self.autoscroll = QCheckBox('Auto-scroll')
        self.autoscroll.setChecked(True)
        options.addWidget(self.autoscroll)
        for checkbox in (self.biome_logs, self.autoscroll):
            checkbox.setStyleSheet('QCheckBox::indicator { width: 14px; height: 14px; }')
        clear = QPushButton('Clear log')
        self._compact_button(clear)
        clear.setStyleSheet(clear.styleSheet() + 'QPushButton { background-color: #39393d; }')
        clear.clicked.connect(self.clear_logs)
        options.addWidget(clear)
        log_layout.addLayout(options)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont('Consolas', 9))
        self.log_box.document().setMaximumBlockCount(5000)
        self.log_box.setMinimumHeight(240)
        self.log_box.setPlaceholderText('Multiscope activity and webhook delivery warnings will appear here.')
        log_layout.addWidget(self.log_box, 1)
        self.splitter.addWidget(log_group)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, 1)
        self.page_content.installEventFilter(self)
        self.page_scroll.viewport().installEventFilter(self)
        self._scroll_views = {view.viewport(): view for view in
                              (self.timings, servers_table, self.issues, self.log_box)}
        for viewport in self._scroll_views:
            viewport.installEventFilter(self)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    @staticmethod
    def _compact_button(button):
        button.setStyleSheet('QPushButton { min-height: 18px; min-width: 60px; padding: 5px 12px; }')

    def _arrange_cards(self):
        columns = 4 if self.page_scroll.viewport().width() >= 1000 else 2
        if columns == self._card_columns:
            return
        self._card_columns = columns
        for group in self._card_groups:
            self._card_layout.removeWidget(group)
        for column in range(4):
            self._card_layout.setColumnStretch(column, 1 if column < columns else 0)
        for i, group in enumerate(self._card_groups):
            self._card_layout.addWidget(group, i//columns, i%columns)

    def _initialize_split(self):
        if self._split_initialized or not self.isVisible():
            return
        self._update_page_height()
        self.page_content.layout().activate()
        self._split_initialized = True

    def _remember_split_adjustment(self, _position, _index):
        self._user_split_adjusted = True

    def _update_page_height(self):
        if not self.isVisible():
            return
        self._arrange_cards()
        self.page_content.layout().activate()
        # Include the explanation beneath the table in the opening viewport,
        # with some bottom clearance even when the summary cards wrap.
        table_height = max(260, self.page_scroll.viewport().height()-self.splitter.y()-12)
        self.splitter.setMinimumHeight(table_height+320+self.splitter.handleWidth())
        if not self._user_split_adjusted:
            self.splitter.setSizes([table_height, 320])

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and watched in (self.page_content, self.page_scroll.viewport()):
            QTimer.singleShot(0, self._update_page_height)
        elif event.type() == QEvent.Type.Wheel and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            view = self._scroll_views.get(watched)
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if view is not None and delta:
                inner = view.verticalScrollBar()
                outer = self.page_scroll.verticalScrollBar()
                at_edge = inner.value() <= inner.minimum() if delta > 0 else inner.value() >= inner.maximum()
                page_can_move = outer.value() > outer.minimum() if delta > 0 else outer.value() < outer.maximum()
                if at_edge and page_can_move:
                    QApplication.sendEvent(self.page_scroll.viewport(), event)
                    return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _configure_table(table):
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(26)
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

    @staticmethod
    def _cell(table, row, column, text):
        item = table.item(row, column)
        if item is None:
            table.setItem(row, column, QTableWidgetItem(str(text)))
        elif item.text() != str(text):
            item.setText(str(text))

    def receive_report(self, report):
        self._report = copy.deepcopy(report) if isinstance(report, dict) else {}
        self._received_at = time.monotonic()
        self._dirty = True
        self.refresh()

    def refresh(self):
        if not self.isVisible():
            return
        age = None if self._received_at is None else time.monotonic()-self._received_at
        try:
            running = bool(self._running())
        except RuntimeError:
            running = False
        if age is None:
            self.freshness.setText('Waiting for manager diagnostics' if running else 'Manager stopped · no diagnostics yet')
        else:
            source_age = self._report.get('reader_process', {}).get('seconds_since_diagnostics')
            state = 'Fresh snapshot' if running and age < 15 else 'No recent diagnostics' if running else 'Manager stopped · last snapshot'
            if running and self._report.get('reader_process', {}).get('alive') is False:
                state = 'Worker exited · last snapshot'
            elif running and source_age is not None and source_age+age >= 15:
                state = 'Reader diagnostics delayed'
            self.freshness.setText(f'{state} · updated {age:.0f} s ago · snapshots every 5 s')
        if self._dirty:
            self.render_report()
            self._dirty = False
        self.flush_logs()

    def render_report(self):
        report = self._report
        counts = report.get('counts', {})
        process = report.get('reader_process', {})
        index = report.get('reader_index', {})
        reader = report.get('reader_counters', {})
        delivery = report.get('delivery', {})
        get = lambda data, key: str(data[key]) if key in data else '—'
        health = report.get('reader_health', 'unknown').capitalize()
        if process.get('alive') is False:
            health = 'Worker exited'
        watcher = 'File watcher' if index.get('watcher_active') else 'Metadata polling' if 'watcher_active' in index else 'Watcher unknown'
        self.cards['Reader'].setText(f'{health} · {watcher}\n'
                                     f"Read errors: {get(reader, 'read_failures')}\nBacklogged accounts: {get(reader, 'backlog_users')}")
        if not report:
            self.cards['Reader'].setText('No reader snapshot yet\nRead errors: —\nBacklogged accounts: —')
        self.cards['Reader'].setStyleSheet('font-size: 12px;' + ('color: #ef4444;' if health in ('Worker exited', 'Unhealthy') else ''))
        self.cards['Accounts'].setText(f"Requested {get(counts, 'requested_users')} · tracked {get(counts, 'engine_tracked_users')}\n"
                                       f"Log attached {get(counts, 'attached_log_users')} · running {get(counts, 'live_process_users')}\n"
                                       f"In server snapshot {get(counts, 'snapshot_users')}")
        self.cards['Accounts'].setStyleSheet('font-size: 12px;' + ('color: #f59e0b;' if any(report.get('differences', {}).values()) else ''))
        oldest = duration(delivery['oldest_pending_seconds']*1000) if 'oldest_pending_seconds' in delivery else '—'
        self.cards['Webhooks'].setText(f"Pending {get(delivery, 'pending')} / {get(delivery, 'capacity')} · queued {get(delivery, 'queued')}\n"
                                       f"oldest {oldest} · retries {get(delivery, 'retried')}\n"
                                       f"Rate limits {get(delivery, 'rate_limited')} · last HTTP {get(delivery, 'last_http_status')}")
        self.cards['Outcomes'].setText(f"Delivered {get(delivery, 'delivered')} · failed total {get(delivery, 'failed')}\n"
                                       f"expired {get(delivery, 'expired')} · rejected {get(delivery, 'rejected')}\n"
                                       f"Unconfirmed {get(delivery, 'unconfirmed')} · cancelled {get(delivery, 'cancelled')}")
        self.cards['Outcomes'].setToolTip('Counters since the reader started. Expired and unconfirmed deliveries are included in failed total; queue rejections are separate.')
        self.cards['Outcomes'].setStyleSheet('font-size: 12px;' + ('color: #f59e0b;' if any(delivery.get(k, 0) for k in ('failed', 'expired', 'rejected', 'unconfirmed')) else ''))
        notes = []
        for key, label in [('global_cooldown_seconds', 'Global webhook cooldown'),
                           ('destination_cooldown_seconds', 'Longest destination cooldown')]:
            if delivery.get(key, 0) > 0:
                notes.append(f'{label}: {delivery[key]:.1f} s at snapshot')
        stats = report.get('statistics', {})
        if stats.get('last_error_type'):
            notes.append(f"Statistics save pending: {stats['last_error_type']} ({stats.get('save_failures', 0)} failures)")
        if delivery.get('oldest_pending_seconds', 0) >= 30:
            notes.append('Delivery backlog: oldest alert is approaching the 120-second expiry limit')
        self.notes.setText(' · '.join(notes) if notes else 'No active cooldown or statistics-save error in this snapshot.' if report else 'Start the manager to collect live diagnostics.')
        self.notes.setStyleSheet('color: #f59e0b;' if notes else '')
        for row, key in enumerate(TIMING_LABELS):
            timing = report.get('timings', {}).get(key, {})
            for column, field in enumerate(('last_ms', 'p50_ms', 'p95_ms', 'max_ms'), 1):
                self._cell(self.timings, row, column, duration(timing.get(field)))
            self._cell(self.timings, row, 5, timing.get('samples', '—'))
        issues = [(label, account.get('username', '—'), account.get('user_id', '—'))
                  for key, label in ISSUE_LABELS.items() for account in report.get('differences', {}).get(key, [])]
        self.issue_caption.setText(f'Account tracking issues: {len(issues)}' if report else 'Account tracking issues: waiting for diagnostics')
        if report and not issues:
            self.issue_caption.setText('No account tracking issues in this snapshot.')
        self.details.setTabText(2, f'Account issues ({len(issues)})' if report else 'Account issues')
        self.details.tabBar().setTabTextColor(2, QColor('#f59e0b') if issues else QColor('#b4b4bd'))
        self.issues.setVisible(bool(issues))
        self.issues.setRowCount(len(issues))
        for row, values in enumerate(issues):
            for column, value in enumerate(values):
                self._cell(self.issues, row, column, value)

    def queue_log(self, line):
        self._logs.append(str(line))

    def flush_logs(self):
        if not self.isVisible() or not self._logs:
            return
        scrollbar = self.log_box.verticalScrollBar()
        previous = scrollbar.value()
        batch = [self._logs.popleft() for _ in range(min(250, len(self._logs)))]
        cursor = QTextCursor(self.log_box.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText('\n'.join(batch)+'\n')
        scrollbar.setValue(scrollbar.maximum() if self.autoscroll.isChecked() else min(previous, scrollbar.maximum()))

    def clear_logs(self):
        self._logs.clear()
        self.log_box.clear()

    def log_text(self):
        return '\n'.join(part for part in (self.log_box.toPlainText().rstrip(), '\n'.join(self._logs)) if part)

    def export_data(self):
        return dict(captured_at_unix=time.time(), diagnostics=copy.deepcopy(self._report),
                    diagnostics_age_seconds=None if self._received_at is None else time.monotonic()-self._received_at,
                    log=self.log_text())

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        QTimer.singleShot(0, self._initialize_split)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_page_height)
