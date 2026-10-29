"""Keyboard shortcuts (§8) and the faint key hint drawn on a push button.

A tab's shortcuts belong to the window, not to whichever widget has focus:
after Enter in the command line, or a click on the console log, G should
still grab a frame while Image & calibration is showing. So they are window
shortcuts that are enabled only while their tab is visible (two tabs both
use B; two enabled shortcuts on one key would cancel each other out), and
they do nothing while a text field or spin box has focus, so typing a
command never triggers one. A text field takes most of its own keys first
anyway (Qt's ShortcutOverride: "1" in the filter types a 1); the check here
covers the rest, Ctrl+S in the filter for one."""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QEvent, QObject, QRectF, QSize, Qt
from PyQt6.QtGui import QFontMetrics, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QLineEdit,
                             QPlainTextEdit, QPushButton, QStyle, QStyleOptionButton,
                             QTextEdit, QWidget)

from . import theme


def typing() -> bool:
    """A text field, spin box or editable combo has focus."""
    w = QApplication.focusWidget()
    while w is not None:
        if isinstance(w, (QLineEdit, QAbstractSpinBox)):
            return not (isinstance(w, QLineEdit) and w.isReadOnly())
        if isinstance(w, (QPlainTextEdit, QTextEdit)):
            return not w.isReadOnly()
        if isinstance(w, QComboBox):
            return w.isEditable()
        w = w.parentWidget()
    return False


class _FollowVisible(QObject):
    """Keeps an owner's shortcuts enabled exactly while the owner is shown."""

    def __init__(self, owner: QWidget):
        super().__init__(owner)
        self.shortcuts: list[QShortcut] = []
        owner.installEventFilter(self)

    def eventFilter(self, obj, e):
        if e.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            on = e.type() == QEvent.Type.Show
            for sc in self.shortcuts:
                sc.setEnabled(on)
        return False


def add_shortcut(owner: QWidget, keys: str, fn: Callable[[], object],
                 while_typing: bool = False) -> QShortcut:
    """`keys` triggers `fn` anywhere in owner's window while owner is shown,
    except while typing (unless while_typing, as for Ctrl+L)."""
    follow = owner.findChild(_FollowVisible, options=Qt.FindChildOption.FindDirectChildrenOnly)
    if follow is None:
        follow = _FollowVisible(owner)
    sc = QShortcut(QKeySequence(keys), owner)
    sc.setContext(Qt.ShortcutContext.WindowShortcut)
    sc.setEnabled(owner.isVisible())
    sc.activated.connect(lambda: None if (typing() and not while_typing) else fn())
    follow.shortcuts.append(sc)
    return sc


def hint_text(keys: str) -> str:
    """How a key sequence reads on a button: 'Ctrl+Z', '←'."""
    return {"Left": "←", "Right": "→"}.get(keys, keys)


class HintButton(QPushButton):
    """A push button with its shortcut after the text in faint mono, as the
    segmented buttons show theirs. The stylesheet draws the button itself;
    only the text is placed here, so text and hint sit centred together."""

    GAP = 7

    def __init__(self, text: str, hint: str = "", parent=None):
        super().__init__(text, parent)
        self.hint = hint
        self._hint_font = theme.mono_font(11)

    def _hint_w(self) -> int:
        return QFontMetrics(self._hint_font).horizontalAdvance(self.hint) + self.GAP

    def sizeHint(self) -> QSize:
        s = super().sizeHint()
        return QSize(s.width() + self._hint_w(), s.height()) if self.hint else s

    def minimumSizeHint(self) -> QSize:
        s = super().minimumSizeHint()
        return QSize(s.width() + self._hint_w(), s.height()) if self.hint else s

    def paintEvent(self, e):
        if not self.hint:
            return super().paintEvent(e)
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        p = QPainter(self)
        self.style().drawControl(QStyle.ControlElement.CE_PushButtonBevel, opt, p, self)
        tw = QFontMetrics(self.font()).horizontalAdvance(self.text())
        hw = self._hint_w()
        x = (self.width() - tw - hw) / 2
        primary = self.property("role") == "primary"
        if not self.isEnabled():
            fg, faint = "text_faint", "text_faint"
        elif primary:
            fg, faint = "on_accent", "on_accent"
        else:
            fg, faint = "text", "text_faint"
        p.setFont(self.font())
        p.setPen(theme.qcolor(fg))
        p.drawText(QRectF(x, 0, tw + 1, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.setFont(self._hint_font)
        c = theme.qcolor(faint)
        if primary and self.isEnabled():
            c.setAlphaF(0.65)           # faint on the accent fill, still legible
        p.setPen(c)
        p.drawText(QRectF(x + tw + self.GAP, 0, hw, self.height()),
                   Qt.AlignmentFlag.AlignVCenter, self.hint)
        p.end()
