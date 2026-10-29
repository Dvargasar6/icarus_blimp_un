"""The few icons the viewer needs, painted at run time: no image files, no
icon theme (Linux desktops differ in which ones they ship)."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QIcon, QPainter, QPainterPath, QPen, QPixmap

from .. import theme

_SIZE = 16          # logical px; drawn at 2x so it stays sharp on HiDPI


def _canvas():
    pm = QPixmap(_SIZE * 2, _SIZE * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pm, p


def copy_icon(colour: str = "text_muted") -> QIcon:
    pm, p = _canvas()
    pen = QPen(theme.qcolor(colour), 1.4)
    p.setPen(pen)
    back, front = QRectF(2.5, 2.0, 8, 8.5), QRectF(5.5, 5.5, 8, 8.5)
    p.drawRoundedRect(back, 1.5, 1.5)
    # Punch the front sheet out of the back one, then outline it.
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.setBrush(Qt.GlobalColor.black)
    p.drawRoundedRect(front.adjusted(-1, -1, 1, 1), 2, 2)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(front, 1.5, 1.5)
    p.end()
    return QIcon(pm)


def check_icon(colour: str = "ok_dot") -> QIcon:
    pm, p = _canvas()
    p.setPen(QPen(theme.qcolor(colour), 1.8, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    path = QPainterPath(QPointF(3.5, 8.5))
    path.lineTo(6.5, 11.5)
    path.lineTo(12.5, 4.5)
    p.drawPath(path)
    p.end()
    return QIcon(pm)
