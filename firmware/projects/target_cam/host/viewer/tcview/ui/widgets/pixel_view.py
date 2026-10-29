"""PixelImageView (§4.5): a camera frame drawn pixel-exact, with zoom and pan.

It is hand-painted rather than built on a pyqtgraph ViewBox (the choice §13.2
leaves open). The frame is only 160×120, so every pixel the user points at
matters: the hover readout, the region box and (Phase 5) the label brush must
all land on the same pixel at every zoom. Here one pair of numbers, `scale`
and `origin`, maps widget to image coordinates for all three, and for the
painting, so they cannot drift apart. A ViewBox inside a scroll area would
have two transforms (the ViewBox's and the scroll offset) to keep in step.

In paint mode (Label frames) the left and right buttons paint instead of
moving the region: paintStroke reports a click, or a drag's start, moves and
end, in image coordinates through that same transform. As with pyqtgraph's
mouse events the old Label tab used, a press only becomes a drag once the
mouse has moved the platform's drag distance; less than that is a click.

Scaling is nearest-neighbour (no smooth-pixmap hint), so a pixel stays a
square block. Above Fit the canvas is larger than the viewport and the
QScrollArea's real scroll bars move it, as small laptop screens need (§13.2);
middle-drag and Space+drag pan as well.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QImage, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QApplication, QFrame, QScrollArea, QWidget

from .. import theme

ZOOMS = ("fit", "2", "3", "4")
HANDLE_PX = 7           # half-size of a region corner handle, in widget pixels
MIN_FIT = 0.5           # the fit scale never goes below this (a 80×60 image)


def fit_scale(avail: QSize, w: int, h: int) -> float:
    """The largest scale at which a w×h image fits `avail`. If a whole-number
    scale fills at least 90 % of that, it is used instead: every image pixel
    is then the same number of screen pixels, which keeps the grid even."""
    s = min(avail.width() / w, avail.height() / h)
    n = math.floor(s)
    if n >= 1 and n / s >= 0.9:
        s = float(n)
    return max(MIN_FIT, s)


class _Canvas(QWidget):
    """The widget inside the scroll area. It only forwards paint and input
    to the view, which owns the state."""

    def __init__(self, view: "PixelImageView"):
        super().__init__()
        self.view = view
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        self.view._paint(p, self.visibleRegion().boundingRect())
        p.end()

    def mousePressEvent(self, e):
        self.view._press(e)

    def mouseMoveEvent(self, e):
        self.view._move(e)

    def mouseReleaseEvent(self, e):
        self.view._release(e)

    def leaveEvent(self, _):
        self.view._leave()

    def wheelEvent(self, e):
        # Ctrl+wheel zooms here; a plain wheel is ignored, so it goes up to
        # the scroll area and scrolls.
        if not self.view._wheel(e, self.mapTo(self.view.viewport(), e.position().toPoint())):
            e.ignore()

    def keyPressEvent(self, e):
        if not self.view._key(e, True):
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if not self.view._key(e, False):
            super().keyReleaseEvent(e)


class PixelImageView(QScrollArea):
    hovered = pyqtSignal(int, int)
    left = pyqtSignal()
    regionChanged = pyqtSignal(object)      # (x, y, w, h) in image pixels
    zoomChanged = pyqtSignal(str)
    # "click" | "start" | "move" | "end", image coordinates (QPointF), erase
    # (the right button). Only in paint mode.
    paintStroke = pyqtSignal(str, object, bool)

    def __init__(self, w: int = 160, h: int = 120, parent=None):
        super().__init__(parent)
        self.img_w, self.img_h = w, h
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(False)
        self.setMinimumSize(120, 90)        # §2: a small floor, never the image size
        self.canvas = _Canvas(self)
        self.setWidget(self.canvas)
        self.viewport().setStyleSheet("background: %s;" % theme.TOKENS["plot_bg"])

        self._image: Optional[QImage] = None
        self._overlay: Optional[QImage] = None
        self._overlay_opacity = 1.0
        self.guides: list = []          # dashed (x, y, w, h)
        self.boxes: list = []           # (x, y, w, h, colour, text[, "dot"])
        self.marks: list = []           # (x, y) crosses, image coordinates (float)
        self.tool_text = ""             # appended to the bottom-left chip
        self.paint_mode = False         # left/right buttons paint (paintStroke)
        self._preview = None            # ("rect" | "ellipse", x, y, w, h) while dragging a shape
        self._region: Optional[list] = None     # [x, y, w, h], ints; None = no region
        self._zoom = "fit"
        self.scale = 1.0
        self.origin = QPointF(0, 0)     # canvas position of image pixel (0, 0)
        self._hover: Optional[tuple] = None
        self._drag = None               # ("move", dx, dy) | ("tl"|"br",) | ("pan", QPoint)
        self._space = False
        self.set_image(np.full((h, w, 3), 40, dtype=np.uint8))

    # ---- content ---------------------------------------------------------------

    def set_image(self, rgb: np.ndarray):
        """An H×W×3 uint8 array. The QImage is copied, so the array may change."""
        rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
        h, w = rgb.shape[:2]
        self._image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        if (w, h) != (self.img_w, self.img_h):
            self.img_w, self.img_h = w, h
            self._relayout()
        self.canvas.update()

    def set_overlay(self, rgba: Optional[np.ndarray], opacity: float = 1.0):
        """An H×W×4 uint8 array drawn over the frame, or None."""
        if rgba is None:
            self._overlay = None
        else:
            rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
            h, w = rgba.shape[:2]
            self._overlay = QImage(rgba.data, w, h, 4 * w,
                                   QImage.Format.Format_RGBA8888).copy()
        self._overlay_opacity = opacity
        self.canvas.update()

    def set_boxes(self, boxes: Sequence):
        self.boxes = list(boxes)
        self.canvas.update()

    def set_marks(self, marks: Sequence):
        self.marks = list(marks)
        self.canvas.update()

    def set_guides(self, guides: Sequence):
        self.guides = list(guides)
        self.canvas.update()

    def set_tool_text(self, text: str):
        self.tool_text = text
        self.canvas.update()

    def set_preview(self, shape: Optional[str], x=0.0, y=0.0, w=0.0, h=0.0):
        """A dashed rectangle or ellipse in image coordinates (the shape
        being dragged), or None to remove it."""
        self._preview = None if shape is None else (shape, x, y, w, h)
        self.canvas.update()

    # ---- region ------------------------------------------------------------------

    def set_region(self, x: int, y: int, w: int, h: int, emit: bool = True):
        r = self._clamp_region([int(x), int(y), int(w), int(h)])
        changed = r != self._region
        self._region = r
        self.canvas.update()
        if changed and emit:
            self.regionChanged.emit(tuple(r))

    def region(self) -> Optional[tuple]:
        return None if self._region is None else tuple(self._region)

    def _clamp_region(self, r):
        x, y, w, h = r
        w = max(1, min(w, self.img_w))
        h = max(1, min(h, self.img_h))
        x = max(0, min(x, self.img_w - w))
        y = max(0, min(y, self.img_h - h))
        return [x, y, w, h]

    # ---- zoom and the transform -------------------------------------------------

    def zoom(self) -> str:
        return self._zoom

    def set_zoom(self, key: str, anchor: Optional[QPoint] = None):
        """`anchor` is a viewport position that keeps the same image point
        under it (the cursor for Ctrl+wheel); the viewport centre by default."""
        if key not in ZOOMS or key == self._zoom:
            return
        vp = self.viewport()
        a = anchor if anchor is not None else QPoint(vp.width() // 2, vp.height() // 2)
        img_pt = self.to_image(QPointF(a) + QPointF(self.horizontalScrollBar().value(),
                                                    self.verticalScrollBar().value()))
        self._zoom = key
        self._relayout()
        w = self.to_widget(img_pt)
        self.horizontalScrollBar().setValue(int(round(w.x() - a.x())))
        self.verticalScrollBar().setValue(int(round(w.y() - a.y())))
        self.zoomChanged.emit(key)

    def to_image(self, p: QPointF) -> QPointF:
        """Canvas coordinates -> image coordinates (pixel (x, y) spans x..x+1)."""
        return QPointF((p.x() - self.origin.x()) / self.scale,
                       (p.y() - self.origin.y()) / self.scale)

    def to_widget(self, p: QPointF) -> QPointF:
        return QPointF(self.origin.x() + p.x() * self.scale, self.origin.y() + p.y() * self.scale)

    def rect_to_widget(self, x, y, w, h) -> QRectF:
        return QRectF(self.origin.x() + x * self.scale, self.origin.y() + y * self.scale,
                      w * self.scale, h * self.scale)

    def pixel_at(self, p: QPointF) -> Optional[tuple]:
        q = self.to_image(p)
        x, y = math.floor(q.x()), math.floor(q.y())
        return (x, y) if 0 <= x < self.img_w and 0 <= y < self.img_h else None

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def sizeHint(self) -> QSize:
        return QSize(self.img_w * 4, self.img_h * 4)

    def _relayout(self):
        avail = self.maximumViewportSize()
        fit = fit_scale(avail, self.img_w, self.img_h)
        self.scale = fit if self._zoom == "fit" else fit * int(self._zoom)
        iw, ih = self.img_w * self.scale, self.img_h * self.scale
        if self._zoom == "fit":
            cw, ch = avail.width(), avail.height()
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            # A scroll bar in one direction narrows the viewport in the
            # other, which may then need a bar of its own: two passes settle it.
            sb = self.style().pixelMetric(self.style().PixelMetric.PM_ScrollBarExtent)
            need_h = iw > avail.width()
            need_v = ih > avail.height()
            need_h = iw > avail.width() - (sb if need_v else 0)
            need_v = ih > avail.height() - (sb if need_h else 0)
            vw = avail.width() - (sb if need_v else 0)
            vh = avail.height() - (sb if need_h else 0)
            cw, ch = max(math.ceil(iw), vw), max(math.ceil(ih), vh)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Whole-pixel origin, so the image's pixel edges fall on screen pixels.
        self.origin = QPointF(math.floor((cw - iw) / 2), math.floor((ch - ih) / 2))
        self.canvas.resize(int(cw), int(ch))
        self.canvas.update()

    # ---- painting ----------------------------------------------------------------

    def _paint(self, p: QPainter, visible: QRect):
        p.fillRect(visible, theme.qcolor("plot_bg"))
        target = self.rect_to_widget(0, 0, self.img_w, self.img_h)
        # No SmoothPixmapTransform: nearest-neighbour, a pixel stays a block.
        if self._image is not None:
            p.drawImage(target, self._image)
        if self._overlay is not None:
            p.setOpacity(self._overlay_opacity)
            p.drawImage(target, self._overlay)
            p.setOpacity(1.0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen = QPen(QColor(255, 255, 255, 120), 1, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for g in self.guides:
            p.drawRect(self.rect_to_widget(*g))

        fm = QFontMetrics(theme.mono_font())
        p.setFont(theme.mono_font())
        for x, y, w, h, colour, text, *style in self.boxes:
            r = self.rect_to_widget(x, y, w, h)
            if style and style[0] == "dot":
                p.setPen(QPen(QColor(colour), 1, Qt.PenStyle.DotLine))
            else:
                p.setPen(QPen(QColor(colour), 2))
            p.drawRect(r)
            if text:
                p.drawText(QPointF(r.left(), r.top() - fm.descent() - 2), text)

        if self._preview is not None:
            shape, x, y, w, h = self._preview
            p.setPen(QPen(Qt.GlobalColor.white, 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            r = self.rect_to_widget(x, y, w, h)
            p.drawEllipse(r) if shape == "ellipse" else p.drawRect(r)

        p.setPen(QPen(Qt.GlobalColor.white, 2))
        for x, y in self.marks:
            c = self.to_widget(QPointF(x, y))
            p.drawLine(QPointF(c.x() - 8, c.y()), QPointF(c.x() + 8, c.y()))
            p.drawLine(QPointF(c.x(), c.y() - 8), QPointF(c.x(), c.y() + 8))

        if self._hover is not None and self.scale >= 3:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            p.drawRect(self.rect_to_widget(self._hover[0], self._hover[1], 1, 1))
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._region is not None:
            r = self.rect_to_widget(*self._region)
            p.setPen(QPen(theme.qcolor("accent"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            p.setBrush(theme.qcolor("plot_bg"))
            p.setPen(QPen(theme.qcolor("accent"), 1.5))
            for c in (r.topLeft(), r.bottomRight()):
                p.drawPolygon(_diamond(c, HANDLE_PX - 2))

        self._paint_chip(p, visible)

    def _paint_chip(self, p: QPainter, visible: QRect):
        zoom = "fit" if self._zoom == "fit" else "%s×" % self._zoom
        text = "%d×%d · %s" % (self.img_w, self.img_h, zoom)
        if self.tool_text:
            text += " · " + self.tool_text
        font = theme.mono_font(11)
        fm = QFontMetrics(font)
        r = QRectF(visible.left() + 8, visible.bottom() - 8 - fm.height() - 4,
                   fm.horizontalAdvance(text) + 12, fm.height() + 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(r, 4, 4)
        p.setFont(font)
        p.setPen(theme.qcolor("text_muted"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    # ---- input -------------------------------------------------------------------

    def _handle_at(self, pos: QPointF) -> Optional[str]:
        if self._region is None:
            return None
        r = self.rect_to_widget(*self._region)
        for name, c in (("tl", r.topLeft()), ("br", r.bottomRight())):
            if abs(pos.x() - c.x()) <= HANDLE_PX and abs(pos.y() - c.y()) <= HANDLE_PX:
                return name
        if r.contains(pos):
            return "move"
        return None

    def _press(self, e):
        pos = e.position()
        b = e.button()
        if b == Qt.MouseButton.MiddleButton or (b == Qt.MouseButton.LeftButton and self._space):
            self._drag = ("pan", e.globalPosition().toPoint())
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self.paint_mode and b in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._drag = ("press", pos, b == Qt.MouseButton.RightButton, b)
            return
        if b != Qt.MouseButton.LeftButton:
            return
        what = self._handle_at(pos)
        if what == "move":
            q = self.to_image(pos)
            self._drag = ("move", q.x() - self._region[0], q.y() - self._region[1])
        elif what in ("tl", "br"):
            self._drag = (what,)

    def _move(self, e):
        pos = e.position()
        d = self._drag
        if d and d[0] == "pan":
            g = e.globalPosition().toPoint()
            delta = g - d[1]
            self._drag = ("pan", g)
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            return
        if d and d[0] == "press":
            if (pos - d[1]).manhattanLength() >= QApplication.startDragDistance():
                self._drag = ("paint", d[2], d[3])
                self.paintStroke.emit("start", self.to_image(d[1]), d[2])
                self.paintStroke.emit("move", self.to_image(pos), d[2])
        elif d and d[0] == "paint":
            self.paintStroke.emit("move", self.to_image(pos), d[1])
        elif d:
            q = self.to_image(pos)
            x, y, w, h = self._region
            if d[0] == "move":
                new = [int(round(q.x() - d[1])), int(round(q.y() - d[2])), w, h]
            elif d[0] == "tl":
                # The opposite corner stays put; snap to whole pixels.
                x1, y1 = x + w, y + h
                nx = max(0, min(int(round(q.x())), x1 - 1))
                ny = max(0, min(int(round(q.y())), y1 - 1))
                new = [nx, ny, x1 - nx, y1 - ny]
            else:
                nx1 = max(x + 1, min(int(round(q.x())), self.img_w))
                ny1 = max(y + 1, min(int(round(q.y())), self.img_h))
                new = [x, y, nx1 - x, ny1 - y]
            self.set_region(*new)
        else:
            what = self._handle_at(pos)
            self.canvas.setCursor(
                Qt.CursorShape.OpenHandCursor if self._space else
                Qt.CursorShape.SizeFDiagCursor if what in ("tl", "br") else
                Qt.CursorShape.SizeAllCursor if what == "move" else
                Qt.CursorShape.CrossCursor)
        self._set_hover(self.pixel_at(pos))

    def _release(self, e):
        d = self._drag
        if d and d[0] in ("press", "paint") and e.button() != d[-1]:
            return                      # another button let go mid-stroke
        if d and d[0] == "pan":
            self.canvas.setCursor(Qt.CursorShape.OpenHandCursor if self._space
                                  else Qt.CursorShape.CrossCursor)
        self._drag = None
        if d and d[0] == "press":
            self.paintStroke.emit("click", self.to_image(d[1]), d[2])
        elif d and d[0] == "paint":
            self.paintStroke.emit("end", self.to_image(e.position()), d[1])

    def _leave(self):
        if self._drag is None:
            self._set_hover(None)

    def _set_hover(self, px: Optional[tuple]):
        if px == self._hover:
            return
        self._hover = px
        self.canvas.update()
        if px is None:
            self.left.emit()
        else:
            self.hovered.emit(*px)

    def _key(self, e, down: bool) -> bool:
        if e.key() != Qt.Key.Key_Space:
            return False
        if not e.isAutoRepeat():
            self._space = down
            if self._drag is None:
                self.canvas.setCursor(Qt.CursorShape.OpenHandCursor if down
                                      else Qt.CursorShape.CrossCursor)
        return True

    def _wheel(self, e, anchor: QPoint) -> bool:
        """Ctrl+wheel steps through ZOOMS about `anchor` (viewport coordinates)."""
        if not e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        step = 1 if e.angleDelta().y() > 0 else -1 if e.angleDelta().y() < 0 else 0
        i = max(0, min(len(ZOOMS) - 1, ZOOMS.index(self._zoom) + step))
        self.set_zoom(ZOOMS[i], anchor)
        e.accept()
        return True

    def wheelEvent(self, e):
        # The scroll area receives the wheel in viewport coordinates.
        if not self._wheel(e, e.position().toPoint()):
            super().wheelEvent(e)


def _diamond(c: QPointF, r: float) -> QPolygonF:
    return QPolygonF([QPointF(c.x(), c.y() - r), QPointF(c.x() + r, c.y()),
                      QPointF(c.x(), c.y() + r), QPointF(c.x() - r, c.y())])
