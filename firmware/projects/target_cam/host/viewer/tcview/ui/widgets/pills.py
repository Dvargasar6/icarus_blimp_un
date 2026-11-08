"""StatusPill and Banner (§4.6): the two ways a state is shown in colour.

A pill is a short state word with a dot (Connected, Saved, board = PC); a
banner is a sentence (why the run is NOT flight-identical). Both take a tone
from theme.TONES, and the dot or icon means the tone never rests on hue
alone."""
from __future__ import annotations

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from .. import theme

ICON = {"ok": "✓", "warn": "!", "err": "✕", "info": "i", "neutral": "·"}


class StatusPill(QWidget):
    def __init__(self, tone: str = "neutral", text: str = "", parent=None):
        super().__init__(parent)
        self._tone, self._text = "neutral", ""
        self.setFont(theme.ui_font(12))
        self.set(tone, text)

    def tone(self) -> str:
        return self._tone

    def text(self) -> str:
        return self._text

    def set(self, tone: str, text: str):
        self._tone = tone if tone in theme.TONES else "neutral"
        self._text = text
        self.setProperty("tone", self._tone)
        self.setToolTip(text)
        self.updateGeometry()
        self.update()

    def setText(self, text: str):
        self.set(self._tone, text)

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        # 22 px for the dot on the left, 10 px of padding on the right
        return QSize(fm.horizontalAdvance(self._text) + 34, max(22, fm.height() + 8))

    def minimumSizeHint(self) -> QSize:
        return QSize(40, self.sizeHint().height())

    def paintEvent(self, _):
        c = theme.tone_colours(self._tone)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(theme.qcolor(c["border"]))
        p.setBrush(theme.qcolor(c["bg"]))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(c["dot"]))
        cy = r.center().y()
        p.drawEllipse(QRectF(10, cy - 3.5, 7, 7))
        p.setPen(theme.qcolor(c["text"]))
        fm = QFontMetrics(self.font())
        tr = self.rect().adjusted(22, 0, -10, 0)
        p.drawText(tr, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(self._text, Qt.TextElideMode.ElideRight, tr.width()))
        p.end()


class Banner(QFrame):
    """An inline message. It wraps rather than elides, so nothing is ever
    cut; the text stays selectable and is repeated in the tooltip."""

    def __init__(self, tone: str = "info", text: str = "", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.icon.setFont(theme.ui_font(13, QFont.Weight.Bold))
        lay.addWidget(self.icon)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                           | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        lay.addWidget(self.label, 1)
        self._tone = None
        self.set(tone, text)

    def tone(self) -> str:
        return self._tone

    def text(self) -> str:
        return self.label.text()

    def set(self, tone: str, text: str):
        self.label.setText(text)
        self.setToolTip(text)
        if tone != self._tone:
            self._tone = tone if tone in theme.TONES else "neutral"
            c = theme.tone_colours(self._tone)
            self.setStyleSheet(
                "Banner {{ background: {bg}; border: 1px solid {border}; border-radius: 6px; }}"
                " QLabel {{ color: {text}; }}".format(**c))
            self.icon.setText(ICON[self._tone])

    def setText(self, text: str):
        self.set(self._tone, text)
