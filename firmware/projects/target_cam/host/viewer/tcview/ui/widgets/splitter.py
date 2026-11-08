"""A QSplitter whose handle is wide enough to grab (7 px) and shows a
dotted grip, so a split is visibly something that moves (§2)."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QSplitter, QSplitterHandle

from .. import theme

HANDLE_PX = 7


class GripHandle(QSplitterHandle):
    def paintEvent(self, e):
        super().paintEvent(e)           # the stylesheet's background and border
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("text_faint" if self.underMouse() else "border_strong"))
        c = QPointF(self.width() / 2, self.height() / 2)
        horiz = self.orientation() == Qt.Orientation.Horizontal
        for k in (-8, -4, 0, 4, 8):
            pt = QPointF(c.x(), c.y() + k) if horiz else QPointF(c.x() + k, c.y())
            p.drawEllipse(pt, 1.1, 1.1)
        p.end()


class GripSplitter(QSplitter):
    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setHandleWidth(HANDLE_PX)

    def createHandle(self):
        h = GripHandle(self.orientation(), self)
        h.setAttribute(Qt.WidgetAttribute.WA_Hover)
        return h
