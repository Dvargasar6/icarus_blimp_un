"""CollapsibleSection (§4.1) and the elided one-line label its summary uses.

A collapsed section must still say what it holds, so its header carries a
summary of the current values on the right (e.g. `red · H 0° ±12° · S≥110`).
The summary is elided, never wrapped: the header stays one line however
narrow the inspector gets, and the full text is in the tooltip."""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import (QAbstractButton, QHBoxLayout, QLabel, QSizePolicy,
                             QVBoxLayout, QWidget)

from .. import theme


class ElidedLabel(QWidget):
    """One line of text, cut with "…" when it does not fit. Its minimum width
    is small on purpose: it gives way before anything else in its row."""

    def __init__(self, text: str = "", colour: str = "text_muted", mono: bool = False,
                 align=Qt.AlignmentFlag.AlignLeft, parent=None):
        super().__init__(parent)
        self._text = ""
        self.colour = colour
        self.align = align
        self.setFont(theme.mono_font() if mono else theme.ui_font())
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def text(self) -> str:
        return self._text

    def setText(self, text: str):
        self._text = text or ""
        self.setToolTip(self._text)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self._text) + 2, fm.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(16, QFontMetrics(self.font()).height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setPen(theme.qcolor(self.colour))
        fm = QFontMetrics(self.font())
        shown = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, self.width())
        p.drawText(self.rect(), self.align | Qt.AlignmentFlag.AlignVCenter, shown)
        p.end()


class HeaderButton(QAbstractButton):
    """A flat, full-width button whose content is an ordinary layout (chevron,
    title, summary...). QToolButton cannot hold a layout and size itself to
    it, so this paints its own hover and focus and takes its size from the
    layout."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(8, 6, 8, 6)
        self.row.setSpacing(8)

    def add(self, w: QWidget, stretch: int = 0):
        w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.row.addWidget(w, stretch)
        return w

    def sizeHint(self) -> QSize:
        return self.row.sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self.row.minimumSize()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        if self.underMouse() or self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor("hover" if not self.isDown() else "pressed"))
            p.drawRoundedRect(r, 6, 6)
        if self.hasFocus():
            p.setPen(theme.qcolor("accent"))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 6, 6)
        p.end()


class CollapsibleSection(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, title: str, summary: str = "", expanded: bool = True, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = HeaderButton()
        self.header.setCheckable(True)
        self.chevron = self.header.add(QLabel())
        self.chevron.setFixedWidth(12)     # a glyph, not a panel: §2 allows it
        theme.set_prop(self.chevron, "muted", True)
        self.title_lbl = self.header.add(QLabel(title))
        self.title_lbl.setFont(theme.ui_font(13, QFont.Weight.DemiBold))
        self.summary_lbl = self.header.add(
            ElidedLabel(summary, "text_faint", mono=True, align=Qt.AlignmentFlag.AlignRight), 1)
        lay.addWidget(self.header)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(12, 4, 8, 12)
        self.body_layout.setSpacing(6)
        lay.addWidget(self.body)
        self.header.toggled.connect(self._apply)
        self.header.setChecked(expanded)
        self._apply(expanded, emit=False)

    def title(self) -> str:
        return self.title_lbl.text()

    def summary(self) -> str:
        return self.summary_lbl.text()

    def set_summary(self, text: str):
        self.summary_lbl.setText(text)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool):
        self.header.setChecked(on)

    def add_widget(self, w: QWidget, stretch: int = 0) -> QWidget:
        self.body_layout.addWidget(w, stretch)
        return w

    def add_layout(self, layout):
        self.body_layout.addLayout(layout)
        return layout

    def _apply(self, on: bool, emit: bool = True):
        self.chevron.setText("▾" if on else "▸")
        self.body.setVisible(on)
        if emit:
            self.toggled.emit(on)
