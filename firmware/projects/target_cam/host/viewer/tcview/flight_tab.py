"""
Flight data screen: only what leaves the camera in flight.

Entering this screen turns the image and blob collection off (v0,
!collect 0) and returns the link to 115200 baud, so what arrives is exactly the CSV stream the ESP32-S3 will read - same
rows, same rate, same timing. Nothing here comes from the PC detector: every
figure is decoded from the camera's own rows.

The green "flight-identical" banner is shown only when the camera's own #!CFG
confirms image off, collection off, 115200 baud and streaming on, and no
frame has arrived recently. It is the camera's word, not the viewer's assumption.

The pacing check counts every row whose dt_ms is not the configured period
(80.0 ms at 12.5 FPS). The hard requirement is zero such rows with every stage
enabled, so the count runs since connection (or the last reset), not over a
window: one late frame in ten minutes must still show.

Layout (docs/GUI_REDESIGN.md §6.2): the banner and the recording controls on
top; the readouts in a scrolling column on the left (verdict, blobs table,
blob 1, link health), and the field of view, blob 1 over time and the last
raw row on the right. Each card copies its text in the old wording, so a
pasted link-health block reads exactly as it always has.
"""
from __future__ import annotations

import math
import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QBrush, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea,
                             QSizePolicy, QTableWidgetItem, QVBoxLayout, QWidget)

from . import protocol as P
from .calib_tab import DATA_DIR
from .ui import persist, theme
from .ui.widgets import (Bar, Banner, CopyButton, GripSplitter, ReadoutCard, ReadoutTable,
                         value_label)

W, H = 160, 120
HISTORY_S = 30.0

DASH = "—"
BLOB_RGB = (255, 40, 40)                    # the balloons are red
# Verdict tile: (background, text) per state. TARGET has its own green, the
# others the tone colours, so a glance at the colour is enough.
VERDICT = {
    "TARGET": ("verdict_target", "#ffffff"),
    "NO TARGET": ("err_bg", "err_text"),
    "NO ROWS": ("raised", "text_muted"),
    "NO DATA": ("raised", "text_muted"),
}
# The time-series strips: (title, row key, unit, colour token, value format).
SERIES = (("az", "az_deg", "deg", "series_az", "%.2f"),
          ("el", "el_deg", "deg", "series_el", "%.2f"),
          ("range", "range_mm", "mm", "series_range", "%.0f"))
# The link-health table's rows, in the order of the old text block.
HEALTH_ROWS = ("rows/s", "dt_ms", "dt!=", "proc_us", "t_px_us", "t_b_us", "t_c_us",
               "loop_us", "ok", "wire")


def _colour(name: str) -> str:
    return theme.TOKENS.get(name, name)


def _dot_icon(colour: str) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(theme.qcolor(colour))
    p.drawEllipse(QRectF(1.5, 1.5, 9, 9))
    p.end()
    return QIcon(pm)


def _small(text: str = "", faint: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(theme.ui_font(11))
    theme.set_prop(lbl, "faint" if faint else "muted", True)
    return lbl


def _scroll(w: QWidget) -> QScrollArea:
    s = QScrollArea()
    s.setWidgetResizable(True)
    s.setFrameShape(QFrame.Shape.NoFrame)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    s.setWidget(w)
    return s


class _LabelColumn(QWidget):
    """A strip's side labels. The column never holds its strip open; when the
    strip is too short for every line, the secondary ones (min/max, unit)
    step aside instead of overlapping the main one."""

    def __init__(self, layout, secondary, big=None):
        super().__init__()
        self.setLayout(layout)
        self.secondary = secondary
        self.big = big                  # (label, full font, short-strip font)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        lay = self.layout()
        m = lay.contentsMargins()
        need = m.top() + m.bottom() + sum(
            lay.itemAt(i).widget().sizeHint().height() + lay.spacing()
            for i in range(lay.count()) if lay.itemAt(i).widget() is not None)
        for w in self.secondary:
            w.setVisible(self.height() >= need)
        if self.big:
            lbl, full, short = self.big
            lbl.setFont(full if self.height() >= QFontMetrics(full).height() + 4 else short)


class _FillSplitter(GripSplitter):
    """The banner wraps, so the page reports a height for its width, and the
    page's scroll area then makes it as tall as its preferred height - which
    counts pyqtgraph's 640x480 plot hints. Preferring the minimum instead
    lets the page fill the window and scroll only when it truly cannot fit."""

    def sizeHint(self):
        return self.minimumSizeHint()


class FlightTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.cfg: dict = {}
        self.rows: deque = deque()          # (pc_time, row dict)
        self.last_row: P.Row | None = None
        self.last_frame_at = 0.0
        self.rec = None
        self.rec_n = 0
        self.rec_path = None
        self.n_rows = 0                     # since connection / reset
        self.n_dt_bad = 0
        self.worst_dt = 0.0
        self.health_text = "waiting for rows"
        self._verdict = None
        self._fov_hfov = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 0)
        root.setSpacing(6)
        root.addWidget(self._build_toolbar())
        self.split = _FillSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self._build_readouts())
        self.split.addWidget(self._build_plots())
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setCollapsible(0, False)
        self.split.setCollapsible(1, False)
        self.split.setSizes([440, 900])
        root.addWidget(self.split, 1)
        self._set_verdict("NO DATA", "no row from the camera yet")
        self._fill_table([], 0)
        self._fov_frame()

    # ---- building ---------------------------------------------------------------

    def _build_toolbar(self) -> QWidget:
        tb = QWidget()
        v = QVBoxLayout(tb)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.banner = Banner("warn", "")
        v.addWidget(self.banner)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.rec_lbl = value_label(mono=False)
        theme.set_prop(self.rec_lbl, "muted", True)
        row.addWidget(self.rec_lbl, 1)
        self.rec_btn = QPushButton("Record")
        self.rec_btn.setIcon(_dot_icon("err_dot"))
        self.rec_btn.setCheckable(True)
        self.rec_btn.setToolTip("Write every row verbatim, with the camera's settings, to a new CSV file")
        self.rec_btn.toggled.connect(self._record)
        row.addWidget(self.rec_btn)
        self.path_lbl = value_label("rows → target_cam/data/", mono=False)
        self.path_lbl.setToolTip(str(DATA_DIR))
        theme.set_prop(self.path_lbl, "muted", True)
        row.addWidget(self.path_lbl)
        rst = QPushButton("Reset counts")
        rst.setToolTip("Zero the dt != period count, e.g. after changing the stage")
        rst.clicked.connect(self.reset_counts)
        row.addWidget(rst)
        v.addLayout(row)
        return tb

    def _build_readouts(self) -> QWidget:
        col = QWidget()
        col.setObjectName("Panel")
        col.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(12, 8, 8, 8)
        cap = QLabel("Flight readouts")
        cap.setFont(theme.caption_font())
        theme.set_prop(cap, "muted", True)
        head.addWidget(cap)
        head.addStretch(1)
        self.copy_all_btn = CopyButton(self.copy_all, "Copy all", "Copy every readout")
        head.addWidget(self.copy_all_btn)
        v.addLayout(head)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(8, 0, 8, 8)
        bl.setSpacing(8)

        # Verdict tile over the blobs table.
        self.verdict = QFrame()
        self.verdict.setObjectName("Verdict")
        tl = QVBoxLayout(self.verdict)
        tl.setContentsMargins(12, 8, 12, 8)
        tl.setSpacing(0)
        self.status = QLabel("NO DATA")
        self.status.setFont(theme.ui_font(26, QFont.Weight.Bold))
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_sub = QLabel()
        self.status_sub.setFont(theme.ui_font(12))
        self.status_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tl.addWidget(self.status)
        tl.addWidget(self.status_sub)
        bl.addWidget(self.verdict)

        c = self.blobs_card = ReadoutCard("Blobs", "largest first")
        self.table = ReadoutTable(["#", "az °", "el °", "range mm", "notes"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        hh = self.table.horizontalHeader()
        for k in range(4):
            hh.setSectionResizeMode(k, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet("QTableWidget::item { padding: 0 6px; }")
        self.table.setRowCount(4)
        c.add_widget(self.table)
        c.copy_btn.setToolTip("Copy the blobs as CSV")
        c.set_copy_source(self.blobs_csv)
        bl.addWidget(c)

        c = self.blob1_card = ReadoutCard("Blob 1", "")
        # The subtitle gives way to anything in its row; here it should not.
        c.subtitle_lbl.setMinimumWidth(
            c.subtitle_lbl.fontMetrics().horizontalAdvance("4 in frame · 4 reported at most") + 4)
        self.b1 = {k: c.add_kv(k, DASH) for k in ("area", "box", "fill", "centroid", "matched px")}
        c.set_copy_source(self.blob1_text)
        bl.addWidget(c)

        c = self.health = ReadoutCard("Link health", "from the rows themselves")
        self.stage_line = c.add_text("", mono=True, wrap=True)
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(1)
        for j, h in enumerate(("metric", "typical", "max")):
            g.addWidget(_small(h), 0, j)
        self.hc = {}
        r = 1
        for key in HEALTH_ROWS:
            name = QLabel(key)
            name.setFont(theme.ui_font(12))
            theme.set_prop(name, "muted", True)
            typ, mx = value_label(DASH), value_label("")
            note = _small()
            note.setVisible(False)
            g.addWidget(name, r, 0, Qt.AlignmentFlag.AlignTop)
            g.addWidget(typ, r, 1, Qt.AlignmentFlag.AlignTop)
            g.addWidget(mx, r, 2, Qt.AlignmentFlag.AlignTop)
            g.addWidget(note, r + 1, 1, 1, 2)
            bar = None
            if key in ("loop_us", "wire"):
                bar = Bar(0.0)
                bar.setVisible(False)
                g.addWidget(bar, r + 2, 1, 1, 2)
                r += 1
            self.hc[key] = (name, typ, mx, note, bar)
            r += 2
        g.setColumnStretch(1, 1)
        g.setColumnStretch(2, 1)
        c.body.addLayout(g)
        c.set_copy_source(lambda: self.health_text)
        bl.addWidget(c)
        bl.addStretch(1)
        # The column scrolls, so a card is never squeezed below its content.
        for w in (self.verdict, self.blobs_card, self.blob1_card, self.health):
            w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        scroll = _scroll(body)
        v.addWidget(scroll, 1)
        col.setMinimumWidth(300)
        return col

    def _build_plots(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(4, 0, 0, 0)
        v.setSpacing(6)
        self.vsplit = GripSplitter(Qt.Orientation.Vertical)

        # Field of view: where the blobs are, in degrees off the nose.
        box = QWidget()
        bv = QVBoxLayout(box)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(4)
        bv.addWidget(self._title("Field of view",
                                 "az right, el up · deg · white box = camera FOV"))
        self.fov = pg.PlotWidget()
        self.fov.setAspectLocked(True)
        self.fov.setMouseEnabled(False, False)
        self.fov.setMenuEnabled(False)
        self.fov.hideButtons()
        self.fov.setMinimumSize(160, 100)
        pen = lambda c, w=1, **kw: pg.mkPen(_colour(c), width=w, **kw)
        self.fov_minor = pg.PlotCurveItem(pen=pen("grid_minor"), connect="pairs")
        self.fov_major = pg.PlotCurveItem(pen=pen("grid_major"), connect="pairs")
        self.fov_zero = pg.PlotCurveItem(pen=pen("axis_zero"), connect="pairs")
        self.fov_rect = pg.PlotCurveItem(pen=pg.mkPen("w", width=2))
        self.fov_cross = pg.PlotCurveItem(pen=pen("accent", 1, style=Qt.PenStyle.DashLine),
                                          connect="pairs")
        for it in (self.fov_minor, self.fov_major, self.fov_zero, self.fov_rect, self.fov_cross):
            self.fov.addItem(it)
        self.blobs = pg.ScatterPlotItem(pxMode=False)
        self.fov.addItem(self.blobs)
        f = theme.ui_font(12, QFont.Weight.Bold)
        self.blob_lbls = []
        for k in range(4):
            t = pg.TextItem(str(k + 1), color="w", anchor=(0.5, 0.5))
            t.setFont(f)
            t.setVisible(False)
            self.fov.addItem(t)
            self.blob_lbls.append(t)
        bv.addWidget(self.fov, 1)
        self.vsplit.addWidget(box)

        # Blob 1 over time: three strips on one x-axis, camera seconds.
        ts = QWidget()
        tv = QVBoxLayout(ts)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(2)
        tv.addWidget(self._title("Blob 1 over time", "camera clock · gaps = clipped/off"))
        self.series = {}
        first = None
        for i, (name, key, unit, colour, fmt) in enumerate(SERIES):
            last = i == len(SERIES) - 1
            row = QHBoxLayout()
            row.setSpacing(6)
            left = QVBoxLayout()
            left.setContentsMargins(0, 2, 0, 2)
            left.setSpacing(0)
            nm = QLabel(name)
            nm.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
            nm.setStyleSheet("color: %s;" % _colour(colour))
            mx, mn = _small(), _small()
            for w in (mx, mn):
                theme.mono(w)
            left.addWidget(nm)
            left.addWidget(mx)
            left.addStretch(1)
            left.addWidget(mn)
            lw = _LabelColumn(left, (mx, mn))
            lw.setFixedWidth(64)         # a label column, not a panel (§2)
            row.addWidget(lw)
            p = pg.PlotWidget()
            p.setMenuEnabled(False)
            p.hideButtons()
            p.setMinimumHeight(36)
            pi = p.getPlotItem()
            pi.showGrid(x=True, y=True, alpha=0.18)
            pi.setMouseEnabled(x=False, y=True)
            pi.getAxis("left").setStyle(showValues=False)
            pi.getAxis("left").setWidth(6)
            if last:
                pi.setLabel("bottom", "s (camera clock)")
            else:
                pi.getAxis("bottom").setStyle(showValues=False)
                pi.getAxis("bottom").setHeight(6)
            pi.setXRange(-HISTORY_S, 0, padding=0)
            if first is None:
                first = pi
            else:
                pi.setXLink(first)
            curve = pi.plot(pen=pg.mkPen(_colour(colour), width=2), connect="finite")
            row.addWidget(p, 1)
            val = QLabel(DASH)
            val.setFont(theme.mono_font(20))
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            u = _small(unit)
            u.setAlignment(Qt.AlignmentFlag.AlignRight)
            rv = QVBoxLayout()
            rv.setContentsMargins(0, 0, 0, 0)
            rv.setSpacing(0)
            rv.addStretch(1)
            rv.addWidget(val)
            rv.addWidget(u)
            rv.addStretch(1)
            rw = _LabelColumn(rv, (u,), (val, theme.mono_font(20), theme.mono_font(13)))
            rw.setMinimumWidth(84)
            row.addWidget(rw)
            holder = QWidget()
            holder.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            tv.addWidget(holder, 1)
            self.series[name] = {"plot": pi, "curve": curve, "value": val, "max": mx,
                                 "min": mn, "fmt": fmt, "key": key}
        self.p_az, self.p_el, self.p_rg = (self.series[n]["plot"] for n in ("az", "el", "range"))
        self.c_az, self.c_el, self.c_rg = (self.series[n]["curve"] for n in ("az", "el", "range"))
        self.vsplit.addWidget(ts)
        self.vsplit.setStretchFactor(0, 1)
        self.vsplit.setStretchFactor(1, 1)
        self.vsplit.setCollapsible(0, False)
        self.vsplit.setCollapsible(1, False)
        self.vsplit.setSizes([500, 500])
        v.addWidget(self.vsplit, 1)

        # Last raw row: one line, scrolls sideways, copies verbatim.
        rr = QHBoxLayout()
        rr.setContentsMargins(0, 0, 0, 6)
        rr.setSpacing(8)
        cap = QLabel("Last raw row")
        cap.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
        rr.addWidget(cap)
        self.raw = QLineEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlaceholderText("no row yet")
        theme.mono(self.raw)
        rr.addWidget(self.raw, 1)
        self.raw_copy = CopyButton(self.raw.text, tooltip="Copy the raw CSV row")
        rr.addWidget(self.raw_copy)
        v.addLayout(rr)
        return pane

    @staticmethod
    def _title(title: str, sub: str) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 4, 0, 0)
        h.setSpacing(8)
        t = QLabel(title)
        t.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
        h.addWidget(t)
        s = _small(sub)
        s.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        h.addWidget(s, 1)
        return w

    # ---- remembered layout (§9) --------------------------------------------------

    def save_state(self, store):
        persist.save_splitter(store, "split", self.split)
        persist.save_splitter(store, "vsplit", self.vsplit)

    def restore_state(self, store):
        persist.restore_splitter(store, "split", self.split)
        persist.restore_splitter(store, "vsplit", self.vsplit)

    # ---- from the main window ------------------------------------------------

    def entered(self):
        """Put the link in its flight state: image and collection off, 115200."""
        if self.cfg.get("image") == 1:
            self.main.send("v0")
        if self.cfg.get("collect") == 1:
            self.main.send("!collect 0")
        link = self.main.link
        if link is not None and link.baud != P.BOOT_BAUD:
            self.main.send("B%d" % P.BOOT_BAUD)
        self.main.send("k")

    def on_cfg(self, v: dict):
        if self.rec and v != self.cfg:
            self._record_cfg(v)
        self.cfg = v

    def _record_cfg(self, v: dict):
        """The camera's settings into the recording as a '# !CFG' comment line,
        the form tools/csv_report.py writes and reads back, so a file always
        says what produced its rows - the white balance above all, since
        colour from different gains is not comparable."""
        self.rec.write("# !CFG %s\n" % " ".join("%s=%s" % kv for kv in v.items()))

    def on_disconnected(self):
        """The link is gone: no #!CFG any more, and a recording ends here,
        so a file never spans two connections."""
        self.cfg = {}
        if self.rec:
            path, n = self.rec_path, self.rec_n
            self.rec_btn.setChecked(False)
            self.main._say("# [viewer] link closed: recording stopped, %s (%d rows)" % (path, n))

    def on_frame(self, _frame):
        self.last_frame_at = time.monotonic()

    def on_row(self, row: P.Row):
        now = time.monotonic()
        self.rows.append((now, row.v))
        while self.rows and now - self.rows[0][0] > HISTORY_S + 2:
            self.rows.popleft()
        self.last_row = row
        v = row.v
        if v["dt_ms"] > 0:                  # the first row after boot has none
            self.n_rows += 1
            if abs(v["dt_ms"] - self._period()) >= 0.05:
                self.n_dt_bad += 1
                self.worst_dt = max(self.worst_dt, v["dt_ms"])
        if self.rec:
            if self.rec_n == 0:
                # The header names the columns of the rows that actually arrive,
                # so a v1 camera's file still reads back correctly.
                self.rec.write(",".join(P.columns_for(int(v["csv_v"]))) + "\n")
            self.rec.write(row.raw + "\n")
            self.rec_n += 1

    def _period(self) -> float:
        p = self.cfg.get("period_ms")
        return float(p) if isinstance(p, (int, float)) and p > 0 else 80.0

    def reset_counts(self):
        self.n_rows = self.n_dt_bad = 0
        self.worst_dt = 0.0

    def tick(self):
        """10 Hz from the main window."""
        self._banner()
        if self.last_row is None:
            return
        v = self.last_row.v
        if self.raw.text() != self.last_row.raw:
            self.raw.setText(self.last_row.raw)
            self.raw.setCursorPosition(0)
        age = time.monotonic() - self.rows[-1][0]
        n = int(v["blobs"]) if v["ok"] else 0
        if age > 1.0:
            self._set_verdict("NO ROWS", "no row for %.1f s" % age)
        elif v["ok"]:
            self._set_verdict("TARGET", "%d blob(s) in frame · largest first" % n)
        else:
            self._set_verdict("NO TARGET", "no blob in frame")
        self._table(v)
        self._fov(v)
        self._plots()
        self._health()
        if self.rec:
            self.rec_lbl.setText("recording %s: %d rows" % (self.rec_path.name, self.rec_n))

    # ---- copy ----------------------------------------------------------------------

    def verdict_text(self) -> str:
        return "%s  %s" % (self.status.text(), self.status_sub.text())

    def blobs_csv(self) -> str:
        """CSV of the blobs in the last row (§7): only those present."""
        out = ["#,az_deg,el_deg,range_mm,notes"]
        for r in range(self.table.rowCount()):
            cells = [self.table.item(r, c).text() if self.table.item(r, c) else ""
                     for c in range(1, 5)]
            if cells[0] in ("", DASH):
                continue
            out.append(",".join([str(r + 1)] + cells))
        return "\n".join(out)

    def blob1_text(self) -> str:
        if self.last_row is None:
            return "blob 1: no row yet"
        v = self.last_row.v
        return ("blob 1: area %d px  box %dx%d  fill %.2f  centroid (%.1f, %.1f)  matched px %d\n"
                "blobs in frame %d (4 reported at most)"
                % (v["area_px"], v["w_px"], v["h_px"], v["fill"], v["cx_px"], v["cy_px"],
                   v["matched_px"], v["blobs"]))

    def copy_all(self) -> str:
        return "\n\n".join((self.verdict_text(), self.blobs_csv(), self.blob1_text(),
                            self.health_text))

    # ---- pieces ------------------------------------------------------------------

    def _set_verdict(self, word: str, sub: str):
        if word != self._verdict:
            self._verdict = word
            bg, fg = VERDICT[word]
            self.verdict.setStyleSheet(
                "QFrame#Verdict { background: %s; border-radius: 8px; }"
                " QLabel { color: %s; background: transparent; }" % (_colour(bg), _colour(fg)))
            self.status.setText(word)
        if self.status_sub.text() != sub:
            self.status_sub.setText(sub)

    def _banner(self):
        c = self.cfg
        why = []
        if not c:
            why.append("no #!CFG from the camera yet")
        else:
            if c.get("image") != 0:
                why.append("image sending is ON")
            if c.get("collect") == 1:
                why.append("blob collection is ON ('!collect')")
            if c.get("baud") != P.BOOT_BAUD:
                why.append("link at %s baud, not 115200" % c.get("baud"))
            if c.get("stream") != 1:
                why.append("CSV stream is OFF ('s')")
        if time.monotonic() - self.last_frame_at < 3.0:
            why.append("a frame arrived in the last 3 s")
        link = self.main.link
        if link is not None and link.pending():
            why.append("commands still in flight")
        if why:
            self._say_banner("warn", "NOT flight-identical: " + "; ".join(why))
            return
        txt = ("FLIGHT-IDENTICAL: the camera reports image off, 115200 baud, stream on - "
               "these rows are exactly what the ESP32-S3 will receive.")
        if c.get("settings") == "modified":
            self._say_banner("warn", txt + "  BUT the settings are not saved: after a power "
                             "cycle the camera would fly different ones.")
        else:
            self._say_banner("ok", txt + "  Settings: %s." % c.get("settings"))

    def _say_banner(self, tone: str, text: str):
        if tone != self.banner.tone() or text != self.banner.text():
            self.banner.set(tone, text)

    def _table(self, v):
        rows = [(v["az_deg"], v["el_deg"], v["range_mm"],
                 "clipped (no range)" if v["clipped"] else "")]
        for k in (2, 3, 4):
            rows.append((v["b%d_az" % k], v["b%d_el" % k], v["b%d_range_mm" % k], ""))
        self._fill_table(rows, int(v["blobs"]) if v["ok"] else 0)
        self.blob1_card.set_subtitle("%d in frame · 4 reported at most" % v["blobs"])
        self.b1["area"].setText("%d px" % v["area_px"])
        self.b1["box"].setText("%d×%d px" % (v["w_px"], v["h_px"]))
        self.b1["fill"].setText("%.2f" % v["fill"])
        self.b1["centroid"].setText("(%.1f, %.1f)" % (v["cx_px"], v["cy_px"]))
        self.b1["matched px"].setText("%d" % v["matched_px"])

    def _fill_table(self, rows, n):
        """rows: (az, el, range, note) per reported slot; the first n are present."""
        faint, text = theme.qcolor("text_faint"), theme.qcolor("text")
        hi = QBrush(theme.qcolor("selection", 110))
        for i in range(4):
            present = i < min(n, 4, len(rows))
            az, el, rg, note = rows[i] if present else (0, 0, 0, "")
            cells = (["%.2f" % az, "%.2f" % el, "%.0f" % rg if rg >= 0 else "-", note]
                     if present else [DASH, DASH, DASH, ""])
            for j, t in enumerate(["1 largest" if i == 0 else str(i + 1)] + cells):
                it = self.table.item(i, j)
                if it is None:
                    it = QTableWidgetItem()
                    if i == 0:
                        it.setBackground(hi)
                    self.table.setItem(i, j, it)
                if it.text() != t:
                    it.setText(t)
                    if j == 4:
                        it.setToolTip(t)
                it.setForeground(text if present or j == 0 else faint)

    def _fov_frame(self):
        """The camera's FOV box and the degree grid, redrawn only when h-FOV changes."""
        hfov = float(self.cfg.get("fov", 54.0))
        if hfov == self._fov_hfov:
            return
        self._fov_hfov = hfov
        f = (W / 2) / math.tan(math.radians(hfov) / 2)
        vfov = 2 * math.degrees(math.atan((H / 2) / f))
        a, e = hfov / 2, vfov / 2
        self.fov_rect.setData([-a, a, a, -a, -a], [-e, -e, e, e, -e])
        xa, ya = a * 1.1, e * 1.1
        self.fov.setXRange(-xa, xa, padding=0)
        self.fov.setYRange(-ya, ya, padding=0)

        def grid(step, skip=None):
            xs, ys = [], []
            for g in np.arange(-math.floor(xa / step) * step, xa + 1e-9, step):
                if g == 0 or (skip and abs(g / skip - round(g / skip)) < 1e-9):
                    continue
                xs += [g, g]
                ys += [-ya, ya]
            for g in np.arange(-math.floor(ya / step) * step, ya + 1e-9, step):
                if g == 0 or (skip and abs(g / skip - round(g / skip)) < 1e-9):
                    continue
                xs += [-xa, xa]
                ys += [g, g]
            return xs, ys
        self.fov_minor.setData(*grid(5, skip=10))
        self.fov_major.setData(*grid(10))
        self.fov_zero.setData([-xa, xa, 0, 0], [0, 0, -ya, ya])
        self._fov_f = f
        self._fov_box = (xa, ya)

    def _fov(self, v):
        self._fov_frame()
        f = self._fov_f
        spots = []
        for t in self.blob_lbls:
            t.setVisible(False)
        self.fov_cross.setData([], [])
        if v["ok"]:
            n = int(min(v["blobs"], 4))
            mm = float(self.cfg.get("mm", 0) or 0)
            red = _colour("err_dot")
            for k in range(1, n + 1):
                if k == 1:
                    az, el = v["az_deg"], v["el_deg"]
                    d_px = max(v["w_px"], v["h_px"])
                    size = 2 * math.degrees(math.atan(d_px / 2 / f))
                    clipped = bool(v["clipped"])
                    xa, ya = self._fov_box
                    self.fov_cross.setData([-xa, xa, az, az], [el, el, -ya, ya])
                else:
                    az, el, rg = v["b%d_az" % k], v["b%d_el" % k], v["b%d_range_mm" % k]
                    size = 2 * math.degrees(math.atan(mm / 2 / rg)) if mm > 0 and rg > 0 else 2.0
                    clipped = rg < 0 and mm > 0
                spots.append({"pos": (az, el), "size": max(size, 1.0),
                              "pen": pg.mkPen(red if clipped else "w", width=3 if clipped else 1),
                              "brush": pg.mkBrush(*BLOB_RGB, 170 if k == 1 else 90)})
                self.blob_lbls[k - 1].setPos(az, el)
                self.blob_lbls[k - 1].setVisible(True)
        self.blobs.setData(spots)

    def _plots(self):
        if not self.rows:
            return
        t_now = self.rows[-1][1]["t_ms"]
        t = np.array([(r["t_ms"] - t_now) / 1000.0 for _, r in self.rows])
        ok = np.array([r["ok"] for _, r in self.rows]) > 0
        az = np.where(ok, [r["az_deg"] for _, r in self.rows], np.nan)
        el = np.where(ok, [r["el_deg"] for _, r in self.rows], np.nan)
        rg = np.array([r["range_mm"] for _, r in self.rows], dtype=float)
        rg[(~ok) | (rg < 0)] = np.nan
        keep = t >= -HISTORY_S
        for name, y in (("az", az), ("el", el), ("range", rg)):
            s = self.series[name]
            y = y[keep]
            s["curve"].setData(t[keep], y)
            fin = y[np.isfinite(y)]
            fmt = s["fmt"]
            s["max"].setText(fmt % fin.max() if fin.size else "")
            s["min"].setText(fmt % fin.min() if fin.size else "")
            s["value"].setText(fmt % y[-1] if y.size and np.isfinite(y[-1]) else DASH)

    def _cell(self, key, typ, mx="", note="", tone=None, bar=None):
        name, t, m, n, b = self.hc[key]
        t.setText(typ)
        m.setText(mx)
        n.setText(note)
        n.setVisible(bool(note))
        css = "color: %s;" % _colour("warn_text") if tone == "warn" else ""
        for w in (name, t, m):
            if w.styleSheet() != css:
                w.setStyleSheet(css)
        if b is not None:
            b.setVisible(bar is not None)
            if bar is not None:
                b.set_fraction(bar, "warn_dot" if bar > 0.8 else "accent")

    def _health(self):
        now = time.monotonic()
        recent = [r for ts, r in self.rows if now - ts <= 5.0]
        if len(recent) < 2:
            self.health_text = "waiting for rows"
            self.stage_line.setText("waiting for rows")
            for key in HEALTH_ROWS:
                self._cell(key, DASH)
            return
        span = (recent[-1]["t_ms"] - recent[0]["t_ms"]) / 1000.0
        rate = (len(recent) - 1) / span if span > 0 else 0.0
        dt = np.array([r["dt_ms"] for r in recent if r["dt_ms"] > 0])
        pu = np.array([r["proc_us"] for r in recent])
        okp = 100.0 * sum(1 for r in recent if r["ok"]) / len(recent)
        link = self.main.link
        bps = link.counters.rate() if link else 0.0
        c = self.cfg
        last = recent[-1]
        period = self._period()
        budget = int(period * 1000)
        cap = 100.0 * bps * 10 / link.baud if link and link.baud else 0

        def stats(key):
            a = np.array([r[key] for r in recent], dtype=float)
            a = a[np.isfinite(a)]
            return (float(np.median(a)), float(a.max())) if a.size else None

        def p50max(key):
            s = stats(key)
            return "p50 %6.0f max %6.0f" % s if s else "  (v1 row: not reported)"

        bad = ("%d of %d rows, worst %.1f" % (self.n_dt_bad, self.n_rows, self.worst_dt)
               if self.n_dt_bad else "0 of %d rows" % self.n_rows)
        dmean, dsd, dmax = ((dt.mean(), dt.std(), dt.max()) if dt.size else (0, 0, 0))
        self.health_text = (
            "stage      %d running (requested %s; lut %s, clf %s, cnn %s)  csv v%d\n"
            "rows/s     %5.1f   (camera fps setting %s)\n"
            "dt_ms      mean %6.1f  jitter (sd) %5.1f  max %6.1f\n"
            "dt != %-4g %s\n"
            "proc_us    mean %6.0f  max %6.0f\n"
            "t_px_us    %s\n"
            "t_b_us     %s\n"
            "t_c_us     %s\n"
            "loop_us    %s   (budget %d us)\n"
            "ok         %5.1f %% of rows in the last 5 s\n"
            "wire       %6.0f bytes/s at %s baud (%.1f %% of capacity)"
            % (last["stage"], c.get("stage", "?"), c.get("lut", "?"), c.get("clf", "?"),
               c.get("cnn", "?"), last["csv_v"],
               rate, c.get("fps", "?"),
               dmean, dsd, dmax, period, bad,
               pu.mean(), pu.max(),
               p50max("t_px_us"), p50max("t_b_us"), p50max("t_c_us"), p50max("loop_us"),
               budget, okp, bps,
               link.baud if link else "?", cap))

        self.stage_line.setText(
            "stage %d running · requested %s · lut %s · clf %s · cnn %s · csv v%d"
            % (last["stage"], c.get("stage", "?"), c.get("lut", "?"), c.get("clf", "?"),
               c.get("cnn", "?"), last["csv_v"]))
        self._cell("rows/s", "%.1f" % rate, "", "camera fps setting %s" % c.get("fps", "?"))
        self._cell("dt_ms", "mean %.1f" % dmean, "%.1f" % dmax, "jitter (sd) %.1f" % dsd)
        self.hc["dt!="][0].setText("dt≠%g" % period)
        if self.n_dt_bad:
            self._cell("dt!=", "%d of %d rows" % (self.n_dt_bad, self.n_rows),
                       "%.1f" % self.worst_dt, "worst gap", tone="warn")
        else:
            self._cell("dt!=", "0 of %d rows" % self.n_rows)
        self._cell("proc_us", "mean %.0f" % pu.mean(), "%.0f" % pu.max())
        for key in ("t_px_us", "t_b_us", "t_c_us", "loop_us"):
            s = stats(key)
            if s is None:
                self._cell(key, "not reported", "", "v1 row")
            elif key == "loop_us":
                self._cell(key, "p50 %.0f" % s[0], "%.0f" % s[1],
                           "budget %d us · max is %.0f %%" % (budget, 100.0 * s[1] / budget),
                           bar=s[1] / budget if budget else 0.0)
            else:
                self._cell(key, "p50 %.0f" % s[0], "%.0f" % s[1])
        self._cell("ok", "%.1f %%" % okp, "", "of rows in the last 5 s")
        self._cell("wire", "%.0f B/s" % bps, "",
                   "at %s baud · %.1f %% of capacity" % (link.baud if link else "?", cap),
                   bar=cap / 100.0)

    def _record(self, on):
        if on:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.rec_path = DATA_DIR / time.strftime("flight_%Y%m%d_%H%M%S.csv")
            self.rec = open(self.rec_path, "w")
            self.rec_n = 0                  # the header goes in with the first row
            if self.cfg:
                self._record_cfg(self.cfg)
            self.rec_btn.setText("Recording…")
            self.rec_btn.setToolTip("Recording to %s - click to stop" % self.rec_path)
            theme.set_role(self.rec_btn, "danger")
            self.rec_lbl.setText("recording %s: 0 rows" % self.rec_path.name)
        else:
            if self.rec:
                self.rec.close()
                self.rec_lbl.setText("saved %s (%d rows)" % (self.rec_path, self.rec_n))
            self.rec = None
            self.rec_btn.setText("Record")
            self.rec_btn.setToolTip("Write every row verbatim, with the camera's settings, to a new CSV file")
            theme.set_role(self.rec_btn, "")
