"""CopyButton (§4.2): puts the CURRENT text on the clipboard. It takes a
callable, not a string, so a button made once copies the value shown at the
moment of the click."""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QToolButton

from .. import theme
from .icons import check_icon, copy_icon

FEEDBACK_MS = 1200


class CopyButton(QToolButton):
    def __init__(self, get_text: Callable[[], str], label: Optional[str] = None,
                 tooltip: str = "Copy to clipboard", parent=None):
        super().__init__(parent)
        self.get_text = get_text
        self.label = label
        self._icon, self._done = copy_icon(), check_icon()
        self.setIcon(self._icon)
        self.setIconSize(QSize(14, 14))
        self.setToolTip(tooltip)
        if label:
            self.setText(label)
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            theme.set_prop(self, "small", True)
        else:
            theme.set_prop(self, "iconOnly", True)
            theme.set_prop(self, "flat", True)
        self._timer = QTimer(self, singleShot=True, interval=FEEDBACK_MS)
        self._timer.timeout.connect(self._restore)
        self.clicked.connect(self.copy)

    def copy(self) -> str:
        text = self.get_text() or ""
        QApplication.clipboard().setText(text)
        self.setIcon(self._done)
        if self.label:
            self.setText("Copied")
        self._timer.start()
        return text

    def _restore(self):
        self.setIcon(self._icon)
        if self.label:
            self.setText(self.label)
