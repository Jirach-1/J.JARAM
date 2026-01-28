from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QPlainTextEdit,
    QTextEdit,
)


class NoEnterInPopupsFilter(QObject):
    """
    Prevent Enter/Return from activating dialog buttons in popup windows.

    This is intentionally scoped to QDialog-based windows (including QMessageBox).
    Enter is still allowed for multi-line text editors.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: ARG002 - Qt signature
        try:
            if event.type() != QEvent.Type.KeyPress:
                return False

            key = event.key()
            if key not in (Qt.Key_Return, Qt.Key_Enter):
                return False

            app = QApplication.instance()
            if app is None:
                return False

            active = app.activeModalWidget() or app.activeWindow()
            if active is None or not isinstance(active, QDialog):
                return False

            focus = app.focusWidget()
            if focus is not None:
                if isinstance(focus, (QTextEdit, QPlainTextEdit)):
                    return False

            event.accept()
            return True
        except Exception:
            return False
