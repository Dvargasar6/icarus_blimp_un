"""ReadoutCard (§4.3) and ReadoutStrip (§4.4): blocks of values the user may
want to paste somewhere, so every value is selectable and every card copies.

A card's copied text defaults to what it shows (key/value lines, tables as
CSV, text blocks verbatim). Where the old UI already printed a block with
its own wording (§7), the tab passes set_copy_source() so the copy keeps that
wording and nothing downstream that parses it breaks."""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QFont, QKeySequence, QPainter, QPainterPath
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLayout,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import theme
from .copy_button import CopyButton
from .section import ElidedLabel, HeaderButton

SELECTABLE = (Qt.TextInteractionFlag.TextSelectableByMouse
              | Qt.TextInteractionFlag.TextSelectableByKeyboard)


def value_label(text: str = "", mono: bool = True, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setTextInteractionFlags(SELECTABLE)
    lbl.setWordWrap(wrap)
    if mono:
        theme.mono(lbl)
    return lbl


class FlowLayout(QLayout):
    """Left-to-right, wrapping to a new line when the width runs out (the Qt
    flow-layout example, trimmed). It records which items share a line so
    the owner can draw dividers between neighbours only."""

    def __init__(self, parent=None, hspacing: int = 0, vspacing: int = 0):
        super().__init__(parent)
        self._items = []
        self.hspacing, self.vspacing = hspacing, vspacing
        self.lines: list = []           # [[QRect, ...], ...] after each layout pass

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect, apply):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        lines, cur = [], []
        for it in self._items:
            if it.widget() is not None and it.widget().isHidden():
                continue
            hint = it.sizeHint()
            if cur and x + hint.width() > r.right() + 1:
                lines.append(cur)
                cur = []
                x, y = r.x(), y + line_h + self.vspacing
                line_h = 0
            cur.append((it, QRect(QPoint(x, y), hint)))
            x += hint.width() + self.hspacing
            line_h = max(line_h, hint.height())
        if cur:
            lines.append(cur)
        if apply:
            # Within a line every item takes the line's height, so the
            # dividers run full height; the last item takes the spare width.
            self.lines = []
            for line in lines:
                h = max(g.height() for _, g in line)
                geoms = []
                for k, (it, g) in enumerate(line):
                    g.setHeight(h)
                    if k == len(line) - 1:
                        g.setRight(max(g.right(), r.right()))
                    it.setGeometry(g)
                    geoms.append(g)
                self.lines.append(geoms)
        return y + line_h - rect.y() + m.bottom()


class ReadoutTable(QTableWidget):
    """A small read-only table whose cells can be selected; csv() is its copy."""

    def __init__(self, headers: Sequence[str], parent=None):
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(list(headers))
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setFont(theme.mono_font())
        self.setSizeAdjustPolicy(QAbstractItemView.SizeAdjustPolicy.AdjustToContents)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def headers(self):
        return [self.horizontalHeaderItem(c).text() for c in range(self.columnCount())]

    def set_rows(self, rows: Sequence[Sequence]):
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                it = self.item(r, c)
                text = "" if v is None else str(v)
                if it is None:
                    self.setItem(r, c, QTableWidgetItem(text))
                elif it.text() != text:
                    it.setText(text)

    def rows(self):
        return [[(self.item(r, c).text() if self.item(r, c) else "")
                 for c in range(self.columnCount())] for r in range(self.rowCount())]

    def csv(self) -> str:
        return "\n".join(",".join(row) for row in [self.headers()] + self.rows())

    def selected_tsv(self) -> str:
        """The selected cells as tab-separated lines, the bounding rows and
        columns of the selection (a spreadsheet pastes it into a block)."""
        idx = self.selectedIndexes()
        if not idx:
            return ""
        rows = sorted({i.row() for i in idx})
        cols = sorted({i.column() for i in idx})
        sel = {(i.row(), i.column()) for i in idx}
        return "\n".join("\t".join(self.item(r, c).text() if (r, c) in sel and self.item(r, c)
                                   else "" for c in cols) for r in rows)

    def keyPressEvent(self, e):
        # Qt's own Ctrl+C copies only the current cell.
        if e.matches(QKeySequence.StandardKey.Copy) and self.selectedIndexes():
            QApplication.clipboard().setText(self.selected_tsv())
            e.accept()
            return
        super().keyPressEvent(e)


class Bar(QWidget):
    """A thin horizontal fraction bar (e.g. a blob's matched-pixel share)."""

    def __init__(self, fraction: float = 0.0, colour: str = "accent", parent=None):
        super().__init__(parent)
        self.fraction, self.colour = fraction, colour
        self.setMinimumHeight(6)
        self.setMaximumHeight(6)

    def set_fraction(self, f: float, colour: Optional[str] = None):
        self.fraction = max(0.0, min(1.0, f))
        if colour:
            self.colour = colour
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("border_strong"))
        p.drawRoundedRect(self.rect(), 3, 3)
        if self.fraction > 0:
            r = self.rect()
            r.setWidth(max(6, int(r.width() * self.fraction)))
            p.setBrush(theme.qcolor(self.colour))
            p.drawRoundedRect(r, 3, 3)
        p.end()


class StackedBar(QWidget):
    """Shares of one whole side by side (balloon / background / unlabelled),
    each in its own token colour. Parts are (amount, token); a zero amount
    takes no width."""

    def __init__(self, parts: Sequence = (), height: int = 8, parent=None):
        super().__init__(parent)
        self.parts = list(parts)
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.setMinimumWidth(60)

    def set_parts(self, parts: Sequence):
        self.parts = list(parts)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setClipPath(path)                     # round the two ends only
        p.fillRect(r, theme.qcolor("border_strong"))
        total = sum(a for a, _ in self.parts)
        x = r.left()
        for k, (a, token) in enumerate(self.parts):
            if a <= 0 or total <= 0:
                continue
            w = r.width() * a / total
            # 1 px gaps between neighbours, so two close colours still read as two.
            gap = 1 if k < len(self.parts) - 1 and x + w < r.right() - 0.5 else 0
            p.fillRect(QRectF(x, r.top(), max(0.0, w - gap), r.height()), theme.qcolor(token))
            x += w
        p.end()


class ReadoutCard(QFrame):
    """Title row (title, subtitle, stretch, copy button) over a body of
    key/value rows, tables, bars and text blocks, in the order added."""

    def __init__(self, title: str, subtitle: str = "", pane: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("Pane" if pane else "Card")
        self._parts: list = []          # ("kv", key, widget) | ("table", t) | ("text", lbl) | ("bar", b)
        self._copy_source: Optional[Callable[[], str]] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)
        top = self.top = QHBoxLayout()
        top.setSpacing(6)
        self.title_lbl = QLabel(title)
        self.title_lbl.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
        top.addWidget(self.title_lbl)
        self.subtitle_lbl = ElidedLabel(subtitle, "text_faint")
        self.subtitle_lbl.setVisible(bool(subtitle))
        top.addWidget(self.subtitle_lbl, 1)
        if not subtitle:
            top.addStretch(1)
        self.copy_btn = CopyButton(self.copy_text, tooltip="Copy %s" % title)
        top.addWidget(self.copy_btn)
        lay.addLayout(top)
        self.body = QVBoxLayout()
        self.body.setSpacing(6)
        lay.addLayout(self.body)
        lay.addStretch(1)               # a card taller than its content keeps it at the top
        self._grid: Optional[QGridLayout] = None

    def title(self) -> str:
        return self.title_lbl.text()

    def add_header_widget(self, w: QWidget) -> QWidget:
        """A badge or chip in the title row, just before the copy button."""
        self.top.insertWidget(self.top.indexOf(self.copy_btn), w)
        return w

    def set_subtitle(self, text: str):
        self.subtitle_lbl.setText(text)
        self.subtitle_lbl.setVisible(bool(text))

    # ---- body helpers ------------------------------------------------------

    def add_kv(self, key: str, value=None) -> QWidget:
        """A key/value row. `value` may be a widget, a string or None (an
        empty selectable mono label); the value widget is returned."""
        if self._grid is None or self._parts and self._parts[-1][0] != "kv":
            self._grid = QGridLayout()
            self._grid.setHorizontalSpacing(12)
            self._grid.setVerticalSpacing(3)
            self._grid.setColumnStretch(1, 1)
            self.body.addLayout(self._grid)
        if value is None or isinstance(value, str):
            value = value_label(value or "")
        k = QLabel(key)
        k.setFont(theme.ui_font(12))
        theme.set_prop(k, "muted", True)
        row = self._grid.rowCount()
        # Both top-aligned so a key stays level with the first line of a
        # value that wraps; the 1 px nudge matches the 12 px key to the mono value.
        k.setContentsMargins(0, 1, 0, 0)
        top = Qt.AlignmentFlag.AlignTop
        self._grid.addWidget(k, row, 0, Qt.AlignmentFlag.AlignLeft | top)
        self._grid.addWidget(value, row, 1, top)
        self._parts.append(("kv", key, value))
        return value

    def add_table(self, headers: Sequence[str], rows: Sequence[Sequence] = ()) -> ReadoutTable:
        t = ReadoutTable(headers)
        t.set_rows(rows)
        self.body.addWidget(t)
        self._parts.append(("table", t))
        return t

    def add_bar(self, fraction: float = 0.0, colour: str = "accent") -> Bar:
        b = Bar(fraction, colour)
        self.body.addWidget(b)
        self._parts.append(("bar", b))
        return b

    def add_text(self, text: str = "", mono: bool = True, wrap: bool = True) -> QLabel:
        """A selectable text block (several lines kept as printed)."""
        lbl = value_label(text, mono=mono, wrap=wrap)
        self.body.addWidget(lbl)
        self._parts.append(("text", lbl))
        return lbl

    def add_widget(self, w: QWidget, stretch: int = 0) -> QWidget:
        self.body.addWidget(w, stretch)
        return w

    # ---- copy ----------------------------------------------------------------

    def set_copy_source(self, fn: Optional[Callable[[], str]]):
        self._copy_source = fn

    def copy_text(self) -> str:
        if self._copy_source is not None:
            return self._copy_source()
        out = []
        for part in self._parts:
            kind = part[0]
            if kind == "kv":
                w = part[2]
                v = w.text() if hasattr(w, "text") else ""
                out.append("%s %s" % (part[1], v))
            elif kind == "table":
                out.append(part[1].csv())
            elif kind == "text":
                out.append(part[1].text())
        return "\n".join(out)


class _StripBody(QWidget):
    """The cards' container: paints the 1 px dividers between neighbours on
    the same line, never at the start of a wrapped line."""

    def __init__(self):
        super().__init__()
        self.flow = FlowLayout(self)
        self.flow.setContentsMargins(0, 0, 0, 0)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setPen(theme.qcolor("divider"))
        for line in self.flow.lines:
            for g in line[1:]:
                p.drawLine(g.left(), g.top() + 6, g.left(), g.bottom() - 6)
        for line in self.flow.lines[1:]:
            y = line[0].top() - 1
            p.drawLine(0, y, self.width(), y)
        p.end()


class ReadoutStrip(QFrame):
    """A collapsible band of pane-style cards under an image. Collapsing it
    hands the height back to the image; while collapsed the header shows a
    one-line summary so the values are not lost from view."""

    def __init__(self, title: str, expanded: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.cards: list = []
        self._summary_fn: Optional[Callable[[], str]] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(4)
        self.header = HeaderButton()
        self.header.setCheckable(True)
        self.chevron = self.header.add(QLabel())
        self.chevron.setFixedWidth(12)     # a glyph, not a panel: §2 allows it
        theme.set_prop(self.chevron, "muted", True)
        self.title_lbl = self.header.add(QLabel(title))
        self.title_lbl.setFont(theme.caption_font())
        theme.set_prop(self.title_lbl, "muted", True)
        self.summary_lbl = self.header.add(ElidedLabel("", "text_faint", mono=True), 1)
        head.addWidget(self.header, 1)
        self.copy_all = CopyButton(self.copy_text, "Copy all", tooltip="Copy every card")
        head.addWidget(self.copy_all)
        lay.addLayout(head)
        self.body = _StripBody()
        lay.addWidget(self.body)
        self.header.toggled.connect(self._apply)
        self.header.setChecked(expanded)
        self._apply(expanded)

    def add_card(self, card: ReadoutCard) -> ReadoutCard:
        card.setObjectName("Pane")
        theme.repolish(card)
        self.cards.append(card)
        self.body.flow.addWidget(card)
        return card

    def set_summary_source(self, fn: Callable[[], str]):
        self._summary_fn = fn
        self.refresh_summary()

    def refresh_summary(self):
        if self._summary_fn is not None and not self.is_expanded():
            self.summary_lbl.setText(self._summary_fn())

    def summary(self) -> str:
        return self.summary_lbl.text()

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool):
        self.header.setChecked(on)

    def copy_text(self) -> str:
        return "\n\n".join(c.copy_text() for c in self.cards)

    def _apply(self, on: bool):
        self.chevron.setText("▾" if on else "▸")
        self.body.setVisible(on)
        self.summary_lbl.setVisible(not on)
        self.refresh_summary()
