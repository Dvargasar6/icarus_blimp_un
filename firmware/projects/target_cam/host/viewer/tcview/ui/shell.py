"""The application shell (§5): connection bar, tabs, console dock, status bar.

These are the pieces around the four tabs. They hold no camera logic: the
main window wires them to the SerialLink and decides what each state means.
The rule that shapes them is §2's: at 1280×720 the command line and the
status bar are visible on every tab, so the tab pages scroll (PageTabs)
rather than push the bottom of the window off the screen.
"""
from __future__ import annotations

import html
import os
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDockWidget, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
                             QScrollArea, QSizePolicy, QTabWidget, QVBoxLayout, QWidget)
from serial.tools import list_ports

from .. import protocol as P
from . import theme
from .widgets import CopyButton, FlowLayout, HeaderButton, StatusPill

FRAME_BYTES = 160 * 120 * 2
QWIDGETSIZE_MAX = 16777215


# ---- connection bar ------------------------------------------------------------

def ports() -> list[tuple[str, str, bool]]:
    """(device, label, is_usb) for the ports worth offering, USB ones first.
    The legacy ttyS* ports a Linux PC always lists are never the camera
    (its programmer is a USB bridge), so they are left out; a non-USB port
    with a real description (a pty, a Bluetooth serial port) stays."""
    usb, other = [], []
    for p in list_ports.comports():
        is_usb = p.vid is not None
        if not is_usb and os.path.basename(p.device).startswith("ttyS"):
            continue
        desc = p.description if p.description and p.description != "n/a" else ""
        label = "%s — %s" % (p.device, desc) if desc else p.device
        (usb if is_usb else other).append((p.device, label, is_usb))
    return sorted(usb) + sorted(other)


def baud_label(b: int) -> str:
    return "%d · frame %.2g s" % (b, P.frame_seconds(FRAME_BYTES, b))


class PortCombo(QComboBox):
    """Editable, so a path that is not listed (/dev/serial/by-id/..., a pty)
    can be typed. The list is read again every time it opens, so a camera
    plugged in after the viewer started shows up without a restart.

    The list shows each port's full name ("/dev/ttyUSB0 — CP2102 USB to UART
    Bridge Controller"); the box shows only the device path, so the box can be
    narrow and the connection bar fits one line at 1024 px."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(13)     # "/dev/ttyUSB0" and a little room
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.lineEdit().setPlaceholderText("no port found")
        self.extra: list[tuple[str, str]] = []      # always offered first, e.g. the fake
        self.usb: list[str] = []
        self.currentIndexChanged.connect(self._short)
        self.activated.connect(self._short)
        self.refresh()

    def showPopup(self):
        self.refresh()
        # The list may be wider than the box, so the full names show there.
        v = self.view()
        v.setMinimumWidth(v.sizeHintForColumn(0) + v.verticalScrollBar().sizeHint().width() + 12)
        super().showPopup()

    def _short(self, *_):
        """A listed port's full name in the box becomes its device path."""
        i = self.findText(self.currentText())
        if i >= 0:
            self.setEditText(self.itemData(i))

    def refresh(self):
        keep = self.port()
        self.blockSignals(True)
        self.clear()
        for dev, label in self.extra:
            self.addItem(label, dev)
        self.usb = []
        for dev, label, usb in ports():
            if self.findData(dev) < 0:
                self.addItem(label, dev)
                if usb:
                    self.usb.append(dev)
        self.blockSignals(False)
        if keep:
            self.set_port(keep)
        self._short()

    def add_extra(self, dev: str, label: str):
        self.extra.append((dev, label))
        self.refresh()

    def has(self, dev: str) -> bool:
        return self.findData(dev) >= 0

    def port(self) -> str:
        """The device path: the item's data when a listed entry is shown,
        otherwise what was typed (anything after " — " is a description)."""
        text = self.currentText().strip()
        i = self.findText(text)
        if i >= 0:
            return self.itemData(i)
        return text.split(" — ")[0].strip()

    def set_port(self, dev: str):
        i = self.findData(dev)
        if i >= 0:
            self.setCurrentIndex(i)
            self._short()
        else:
            self.setEditText(dev)


class ConnectionBar(QWidget):
    """§5.1. It wraps onto a second line when the window is narrow (a flow
    layout), so nothing is pushed into a toolbar overflow menu."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ConnBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        flow = FlowLayout(self, hspacing=14, vspacing=6)
        flow.setContentsMargins(12, 8, 12, 8)

        mark = QLabel("target_cam")
        mark.setFont(theme.mono_font(14))
        flow.addWidget(mark)

        self.port_combo = PortCombo()
        self.port_combo.setToolTip("Serial port of the camera's programmer. The list is read "
                                   "again when it opens, so plug the camera in and open it; "
                                   "a path can also be typed.")
        flow.addWidget(self._group(self._caption("Port"), self.port_combo))

        self.baud_combo = QComboBox()
        for b in P.BAUDS:
            self.baud_combo.addItem(baud_label(b), b)
        self.baud_combo.setToolTip("Link speed for the image. Every connection starts at %d; "
                                   "Set baud asks the camera to switch (B<n>)." % P.BOOT_BAUD)
        self.set_baud_btn = QPushButton("Set baud")
        flow.addWidget(self._group(self._caption("Baud"), self.baud_combo, self.set_baud_btn))

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setMinimumWidth(96)
        self.pill = StatusPill("neutral", "Not connected")
        flow.addWidget(self._group(self.connect_btn, self.pill))

        # Straight into the flow, not in a group: the flow skips a hidden
        # widget, but a group around a hidden label is an empty widget that
        # still takes a place, and at 1024 px it wrapped onto a blank line.
        self.offline_note = QLabel("Labelling works offline")
        theme.set_prop(self.offline_note, "muted", True)
        self.offline_note.hide()
        flow.addWidget(self.offline_note)

    @staticmethod
    def _caption(text: str) -> QLabel:
        lbl = QLabel(text)
        theme.set_prop(lbl, "muted", True)
        return lbl

    @staticmethod
    def _group(*widgets) -> QWidget:
        g = QWidget()
        lay = QHBoxLayout(g)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        for w in widgets:
            lay.addWidget(w)
        lay.addStretch(1)
        return g

    def baud(self) -> int:
        return int(self.baud_combo.currentData())

    def set_baud(self, b: int):
        i = self.baud_combo.findData(b)
        if i >= 0:
            self.baud_combo.setCurrentIndex(i)


# ---- tabs ----------------------------------------------------------------------

class PageTabs(QTabWidget):
    """A QTabWidget is as tall as its tallest page's minimum, on every tab.
    Each page sits in a scroll area, so a page that does not fit scrolls
    instead of pushing the console and status bar off the screen. The
    pages themselves are what callers deal with: page(i), and
    setCurrentWidget(page) also accepts the page rather than its wrapper."""

    def add_page(self, page: QWidget, title: str, tooltip: str = "") -> int:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(page)
        i = self.addTab(scroll, title)
        if tooltip:
            self.setTabToolTip(i, tooltip)
        return i

    def page(self, i: int) -> QWidget | None:
        w = self.widget(i)
        return w.widget() if isinstance(w, QScrollArea) else w

    def current_page(self) -> QWidget | None:
        return self.page(self.currentIndex())

    def setCurrentWidget(self, w: QWidget):
        for i in range(self.count()):
            if self.page(i) is w or self.widget(i) is w:
                self.setCurrentIndex(i)
                return


# ---- console -------------------------------------------------------------------

class CommandLine(QLineEdit):
    """Enter sends (returnPressed); Up and Down walk through what was sent."""

    HISTORY = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history: list[str] = []
        self._i = 0

    def remember(self, cmd: str):
        if not self.history or self.history[-1] != cmd:
            self.history.append(cmd)
            del self.history[:-self.HISTORY]
        self._i = len(self.history)

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Up and self.history:
            self._i = max(0, self._i - 1)
            self.setText(self.history[self._i])
        elif k == Qt.Key.Key_Down and self.history:
            if self._i < len(self.history) - 1:
                self._i += 1
                self.setText(self.history[self._i])
            else:
                self._i = len(self.history)
                self.clear()
        else:
            super().keyPressEvent(e)


class ConsoleDock(QDockWidget):
    """§5.3. Only the log collapses: the command line and Send are outside
    the collapsible part, so a command can be typed on every tab even with
    the console folded away."""

    MAX_LINES = 5000

    def __init__(self, parent=None):
        super().__init__("Console", parent)
        self.setObjectName("ConsoleDock")
        self.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self._open_h = 200

        head = QWidget()
        head.setObjectName("ConsoleHeader")
        head.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(4, 2, 8, 2)
        hl.setSpacing(8)
        self.header = HeaderButton()
        self.header.setCheckable(True)
        self.chevron = self.header.add(QLabel())
        self.chevron.setFixedWidth(12)      # a glyph, as in CollapsibleSection
        theme.set_prop(self.chevron, "muted", True)
        title = self.header.add(QLabel("Console"))
        title.setFont(theme.ui_font(13, QFont.Weight.DemiBold))
        hl.addWidget(self.header)
        # The last line, when collapsed: a flat read-only line edit, so it
        # can be selected and copied whole although it is cut at the edge.
        self.last_line = QLineEdit()
        self.last_line.setReadOnly(True)
        self.last_line.setFrame(False)
        self.last_line.setObjectName("LastLine")
        self.last_line.setFont(theme.mono_font())
        self.last_line.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        hl.addWidget(self.last_line, 1)
        self.spacer = QWidget()
        self.spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        hl.addWidget(self.spacer, 1)
        self.show_rows = QCheckBox("CSV rows")
        self.show_rows.setToolTip("Also print every CSV row the camera sends in the console")
        hl.addWidget(self.show_rows)
        self.copy_btn = CopyButton(lambda: self.log.toPlainText(), "Copy",
                                   "Copy the whole console log")
        hl.addWidget(self.copy_btn)
        self.clear_btn = QPushButton("Clear")
        theme.set_prop(self.clear_btn, "small", True)
        hl.addWidget(self.clear_btn)
        self.setTitleBarWidget(head)

        body = QWidget()
        body.setObjectName("ConsoleBody")
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(8, 0, 8, 8)
        bl.setSpacing(6)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(self.MAX_LINES)
        self.log.setObjectName("Console")
        theme.mono(self.log)
        bl.addWidget(self.log, 1)
        row = QHBoxLayout()
        prompt = QLabel(">")
        prompt.setFont(theme.mono_font())
        theme.set_prop(prompt, "faint", True)
        row.addWidget(prompt)
        self.entry = CommandLine()
        self.entry.setFont(theme.mono_font())
        self.entry.setPlaceholderText("camera command, e.g.  ?  m  H350  p red  !save   "
                                      "(Up/Down: previous commands · Ctrl+L: here)")
        row.addWidget(self.entry, 1)
        self.send_btn = QPushButton("Send")
        row.addWidget(self.send_btn)
        bl.addLayout(row)
        self.setWidget(body)

        self._fmt = {}
        for kind, colour in (("text", "text"), ("muted", "text_muted"), ("err", "err_text")):
            f = QTextCharFormat()
            f.setForeground(theme.qcolor(colour))
            self._fmt[kind] = f
        self.clear_btn.clicked.connect(self.clear)
        self.header.toggled.connect(self._apply)
        self.header.setChecked(True)
        self._apply(True)

    # ---- log ---------------------------------------------------------------------

    @staticmethod
    def kind_of(text: str) -> str:
        return "text" if text.startswith("> ") or not text.startswith("#") else "muted"

    def say(self, text: str, kind: str | None = None):
        """One line. Sent commands and camera data in `text`, camera and
        viewer remarks (# ...) muted, viewer errors (kind="err") in red."""
        sb = self.log.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 2
        cur = QTextCursor(self.log.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        if not self.log.document().isEmpty():
            cur.insertBlock()
        cur.insertText(text, self._fmt[kind or self.kind_of(text)])
        if at_end:
            sb.setValue(sb.maximum())
        if not self.is_expanded():
            self._show_last(text)

    def _show_last(self, text: str):
        self.last_line.setText(text)
        self.last_line.setCursorPosition(0)
        self.last_line.setToolTip(text)

    def text(self) -> str:
        return self.log.toPlainText()

    def clear(self):
        self.log.clear()
        self.last_line.clear()

    # ---- collapse ----------------------------------------------------------------

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool):
        self.header.setChecked(on)

    def open_height(self) -> int:
        """The dock's height when open: now, or before it was folded."""
        return self.height() if self.is_expanded() else self._open_h

    def restore(self, expanded: bool, height: int):
        """Open or folded as last time, and `height` tall whenever open."""
        self._open_h = height
        if expanded:
            self.set_expanded(True)
            mw = self.parent()
            if isinstance(mw, QMainWindow):
                mw.resizeDocks([self], [height], Qt.Orientation.Vertical)
        else:
            self.set_expanded(False)
            self._open_h = height       # folding records the height it had

    def _apply(self, on: bool):
        self.chevron.setText("▾" if on else "▸")
        mw = self.parent()
        if not on:
            if self.log.isVisible():
                self._open_h = self.height()
            self.log.hide()
            doc = self.log.document()
            last = doc.lastBlock().text() if not doc.isEmpty() else ""
            self._show_last(last)
            self.last_line.show()
            self.spacer.hide()
            # header + command line, nothing more
            self.setMaximumHeight(self.titleBarWidget().sizeHint().height()
                                  + self.widget().sizeHint().height())
        else:
            self.last_line.hide()
            self.spacer.show()
            self.log.show()
            self.setMaximumHeight(QWIDGETSIZE_MAX)
            if isinstance(mw, QMainWindow) and self.isVisible():
                mw.resizeDocks([self], [self._open_h], Qt.Orientation.Vertical)


# ---- status bar ----------------------------------------------------------------

class StatsBar(QWidget):
    """§5.4: the link counters as `key value` segments. One selectable rich
    text label (so a drag selects across segments) in a scroll area that
    scrolls sideways rather than cut the line, and a copy button."""

    WARN = {"junk", "NAK", "retries", "timeouts"}

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.label = QLabel()
        self.label.setContentsMargins(8, 0, 8, 0)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = theme.mono_font()
        f.setPointSizeF(11.5 * 72 / 96)         # 11.5 px at the usual 96 dpi
        self.label.setFont(f)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.label)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # The text plus a 10 px scroll bar, which only appears if the line
        # does not fit; QScrollArea's own size hint would be far taller.
        h = self.label.fontMetrics().height() + 2 + 10
        self.scroll.setMinimumHeight(h)
        self.scroll.setMaximumHeight(h)
        lay.addWidget(self.scroll, 1)
        self.copy_btn = CopyButton(self.copy_text, tooltip="Copy the link counters")
        lay.addWidget(self.copy_btn)
        self.segments: list = []
        self.set_segments([])

    def set_segments(self, segments: list):
        """[(key, [(value text, warn), ...]), ...]; a key with several
        values (frames ok/bad) shows them as a/b."""
        self.segments = segments
        t = theme.TOKENS
        out = []
        for key, parts in segments:
            vals = "/".join('<span style="color:%s">%s</span>' % (
                t["warn_text"] if warn else t["text"], html.escape(v)) for v, warn in parts)
            out.append('<span style="color:%s">%s</span>&nbsp;%s' % (
                t["text_faint"], html.escape(key).replace(" ", "&nbsp;"), vals))
        self.label.setText("&nbsp;&nbsp;&nbsp;".join(out))

    def copy_text(self) -> str:
        return " | ".join("%s %s" % (k, " / ".join(v for v, _ in parts))
                          for k, parts in self.segments)


def counter_segments(link, rows_per_s: float) -> list:
    """The status bar's segments for a link, or dashes with no link."""
    keys = ["baud", "kB/s", "rows/s", "frames ok/bad", "blobs ok/bad",
            "junk", "NAK", "retries", "timeouts"]
    if link is None:
        return [(k, [("—", False)] * (2 if "/bad" in k else 1)) for k in keys]
    c = link.counters

    def n(v, warn=False):
        return ("%d" % v, warn and v > 0)
    return [("baud", [n(link.baud)]),
            ("kB/s", [("%.1f" % (c.rate() / 1000), False)]),
            ("rows/s", [("%.1f" % rows_per_s, False)]),
            ("frames ok/bad", [n(c.frames_ok), n(c.frames_bad, True)]),
            ("blobs ok/bad", [n(c.blobs_ok), n(c.blobs_bad, True)]),
            ("junk", [n(c.junk_lines, True)]),
            ("NAK", [n(c.naks, True)]),
            ("retries", [n(c.retries, True)]),
            ("timeouts", [n(c.timeouts, True)])]
