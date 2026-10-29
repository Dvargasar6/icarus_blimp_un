"""SegmentedButtons and ToggleSwitch (§4.6).

Both paint themselves from the theme tokens rather than through the
stylesheet: a segment needs its key hint in a second, fainter font, and a
switch is a shape, neither of which QSS can draw on a QPushButton."""
from __future__ import annotations

from typing import Optional, Sequence

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QPainter, QPainterPath
from PyQt6.QtWidgets import QAbstractButton, QButtonGroup, QHBoxLayout, QWidget

from .. import theme


class Segment(QAbstractButton):
    PAD = 10
    SWATCH = 10                         # a colour square before the text (label classes)

    def __init__(self, text: str, hint: str = "", swatch: str = "", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.hint = hint
        self.swatch = swatch            # a theme token, or ""
        self.pos_in_group = "only"      # first | middle | last | only
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFont(theme.ui_font())
        self._hint_font = theme.mono_font(11)
        if hint:
            self.setToolTip("%s (%s)" % (text, hint))

    def sizeHint(self) -> QSize:
        w = QFontMetrics(self.font()).horizontalAdvance(self.text()) + 2 * self.PAD
        if self.hint:
            w += QFontMetrics(self._hint_font).horizontalAdvance(self.hint) + 6
        if self.swatch:
            w += self.SWATCH + 6
        return QSize(w, 28)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = 6.0
        left = self.pos_in_group in ("first", "only")
        right = self.pos_in_group in ("last", "only")
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        # Square off the inner corners by uniting with plain rectangles.
        if not left:
            sq = QPainterPath()
            sq.addRect(QRectF(r.left(), r.top(), rad, r.height()))
            path = path.united(sq)
        if not right:
            sq = QPainterPath()
            sq.addRect(QRectF(r.right() - rad, r.top(), rad, r.height()))
            path = path.united(sq)
        on = self.isChecked()
        if on:
            bg = "selection"
        elif self.isDown():
            bg = "pressed"
        elif self.underMouse():
            bg = "hover"
        else:
            bg = "raised"
        p.setPen(theme.qcolor("accent" if on or self.hasFocus() else "border_strong"))
        p.setBrush(theme.qcolor(bg))
        p.drawPath(path)
        fg = "text" if self.isEnabled() else "text_faint"
        fm = QFontMetrics(self.font())
        tw = fm.horizontalAdvance(self.text())
        hw = QFontMetrics(self._hint_font).horizontalAdvance(self.hint) + 6 if self.hint else 0
        sw = self.SWATCH + 6 if self.swatch else 0
        x = (self.width() - sw - tw - hw) / 2
        if self.swatch:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(self.swatch))
            p.drawRoundedRect(QRectF(x, (self.height() - self.SWATCH) / 2,
                                     self.SWATCH, self.SWATCH), 2, 2)
            x += sw
        p.setPen(theme.qcolor(fg))
        p.setFont(self.font())
        p.drawText(QRectF(x, 0, tw + 1, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.hint:
            p.setFont(self._hint_font)
            p.setPen(theme.qcolor("text_faint"))
            p.drawText(QRectF(x + tw + 6, 0, hw, self.height()),
                       Qt.AlignmentFlag.AlignVCenter, self.hint)
        p.end()


class SegmentedButtons(QWidget):
    """One exclusive choice drawn as a single control. `options` are strings
    or (key, label[, hint[, swatch]]) tuples; value() is the key."""
    changed = pyqtSignal(object)

    def __init__(self, options: Sequence, value=None, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict = {}
        opts = [(o, o, "", "") if isinstance(o, str) else (tuple(o) + ("", ""))[:4]
                for o in options]
        for i, (key, label, hint, swatch) in enumerate(opts):
            b = Segment(str(label), hint, swatch)
            n = len(opts)
            b.pos_in_group = ("only" if n == 1 else "first" if i == 0
                              else "last" if i == n - 1 else "middle")
            self.group.addButton(b, i)
            self.buttons[key] = b
            lay.addWidget(b)
        self._keys = [o[0] for o in opts]
        self.group.idToggled.connect(self._toggled)
        self.set_value(value if value is not None else self._keys[0], emit=False)

    def value(self):
        i = self.group.checkedId()
        return self._keys[i] if i >= 0 else None

    def set_value(self, key, emit: bool = True):
        b = self.buttons.get(key)
        if b is None or b.isChecked():
            return
        self._quiet = not emit
        b.setChecked(True)
        self._quiet = False

    def _toggled(self, i, on):
        if on and not getattr(self, "_quiet", False):
            self.changed.emit(self._keys[i])


class ToggleSwitch(QAbstractButton):
    """A checkable button drawn as a switch, with its label to the right."""
    TRACK_W, TRACK_H = 32, 18

    def __init__(self, text: str = "", checked: bool = False, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFont(theme.ui_font())

    def sizeHint(self) -> QSize:
        w = self.TRACK_W
        if self.text():
            w += 8 + QFontMetrics(self.font()).horizontalAdvance(self.text())
        return QSize(w + 2, max(self.TRACK_H + 6, 26))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = (self.height() - self.TRACK_H) / 2
        track = QRectF(1, y, self.TRACK_W, self.TRACK_H)
        on = self.isChecked()
        enabled = self.isEnabled()
        p.setPen(theme.qcolor("accent" if self.hasFocus() else
                              ("accent" if on else "border_strong")))
        p.setBrush(theme.qcolor(("accent" if on else "raised") if enabled else "panel"))
        p.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        d = self.TRACK_H - 6
        kx = track.right() - d - 3 if on else track.left() + 3
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(("on_accent" if on else "text") if enabled else "text_faint"))
        p.drawEllipse(QRectF(kx, y + 3, d, d))
        if self.text():
            p.setPen(theme.qcolor("text" if enabled else "text_faint"))
            p.drawText(QRectF(track.right() + 8, 0, self.width(), self.height()),
                       Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()
