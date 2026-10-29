"""
The pieces the three pages share: choosing sessions and tables, the metric
cards, the per-frame table and the frame viewer. All of them are built from
the viewer's widgets and tokens (tcview.ui), so the two programs look alike.
"""
from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QPlainTextEdit,
                             QPushButton, QScrollArea, QSizePolicy, QTableWidget,
                             QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from tcview.ui import theme
from tcview.ui.widgets import (Bar, ElidedLabel, FlowLayout, PixelImageView, StatusPill,
                               value_label)
from tcview.ui.widgets import ReadoutCard as _ReadoutCard

from . import engine as E
from . import paths

OUTCOME_TONE = {"ok": "ok", "parcial": "warn", "falso": "warn", "perdido": "err",
                "b1 falso": "err"}

# Overlay and box colours in the frame viewer. The mask is the old viewer's
# yellow so it never reads as a label; the balloon boxes from the labels are
# dotted green, the camera's blobs solid green (a balloon) or red (false).
MASK_RGBA = (242, 193, 78, 110)
GT_COLOUR = theme.TOKENS["label_balloon"]
OK_COLOUR = theme.TOKENS["ok_dot"]
BAD_COLOUR = theme.TOKENS["err_dot"]


def caption(text: str) -> QLabel:
    lbl = QLabel(text)
    theme.set_prop(lbl, "caption", True)
    lbl.setFont(theme.caption_font())
    return lbl


def pct(x: float) -> str:
    return "–" if math.isnan(x) else "%.1f" % (100 * x)


def scroll_column(w: QWidget, min_width: int = 300, hscroll: bool = False) -> QScrollArea:
    """A page column. Its minimum size must never reach the window: a window
    cannot be smaller than its contents' minimum, so a tall column on a
    laptop screen pushes the window off the screen, and a splitter squeezed
    below its children's minimum draws them on top of each other. In a
    scroll area the column keeps a small minimum and scrolls instead."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QScrollArea.Shape.NoFrame)
    if not hscroll:
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sa.setWidget(w)
    sa.setMinimumWidth(min_width)
    return sa


# ---- sessions ------------------------------------------------------------------

class SessionPicker(QWidget):
    """The session folders under data/ as a tree with check boxes, grouped
    (train, test, ...). Each row says how many frames are labelled, because
    an unlabelled folder trains and scores nothing."""
    changed = pyqtSignal()

    def __init__(self, store: QSettings, key: str, parent=None, blobsets: bool = False):
        super().__init__(parent)
        self.store, self.key = store, key
        self.blobsets = blobsets            # list blob sets (.npz) too: the classifier's
        self.sessions: list[paths.Session] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Carpeta", "Etiquetados"])
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, self.tree.header().ResizeMode.ResizeToContents)
        self.tree.setMinimumHeight(260)
        self.tree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        # Elided, not a plain label: a plain label's minimum width is its
        # text, which would force the whole column wider than the tree.
        self.summary = ElidedLabel("", "text_muted", mono=True)
        row.addWidget(self.summary, 1)
        for text, fn in (("Todo", self.check_all), ("Nada", self.check_none),
                         ("Recargar", self.reload)):
            b = QPushButton(text)
            theme.set_prop(b, "small", True)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        self.tree.itemChanged.connect(self._item_changed)
        self.reload()

    def reload(self):
        checked = set(self.checked_keys()) if self.sessions else set(
            str(self.store.value(self.key, "")).split(","))
        self.sessions = paths.find_sessions(blobsets=self.blobsets)
        self.tree.blockSignals(True)
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for s in self.sessions:
            g = groups.get(s.group)
            if g is None:
                g = QTreeWidgetItem([s.group, ""])
                g.setFlags(g.flags() | Qt.ItemFlag.ItemIsAutoTristate
                           | Qt.ItemFlag.ItemIsUserCheckable)
                g.setCheckState(0, Qt.CheckState.Unchecked)
                g.setFont(0, theme.ui_font(13, QFont.Weight.DemiBold))
                groups[s.group] = g
                self.tree.addTopLevelItem(g)
                g.setExpanded(True)
            it = QTreeWidgetItem([s.name + (" (blobs)" if s.is_blobset else ""),
                                  "%d / %d" % (s.n_labelled, s.n_frames)])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if s.key in checked
                             else Qt.CheckState.Unchecked)
            it.setData(0, Qt.ItemDataRole.UserRole, s.key)
            it.setToolTip(0, "data/" + s.key + (
                "\nconjunto de blobs: %s / %s blobs etiquetados\n%s" % (
                    s.n_labelled, s.n_frames, s.blobs.text()) if s.is_blobset else ""))
            it.setFont(1, theme.mono_font())
            it.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if s.n_labelled == 0:
                it.setForeground(0, theme.qcolor("text_faint"))
                it.setToolTip(0, "Sin etiquetas: no aporta nada al entrenamiento ni a la "
                              "verificación")
            g.addChild(it)
        self.tree.blockSignals(False)
        self._item_changed()

    def _leaves(self):
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                yield g.child(j)

    def checked_keys(self) -> list[str]:
        return [it.data(0, Qt.ItemDataRole.UserRole) for it in self._leaves()
                if it.checkState(0) == Qt.CheckState.Checked]

    def checked(self) -> list[paths.Session]:
        keys = set(self.checked_keys())
        return [s for s in self.sessions if s.key in keys]

    def set_checked(self, keys: Sequence[str]):
        """Check the sessions with these keys (paths under data/)."""
        names = set(keys)
        self.tree.blockSignals(True)
        for it in self._leaves():
            it.setCheckState(0, Qt.CheckState.Checked
                             if it.data(0, Qt.ItemDataRole.UserRole) in names
                             else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._item_changed()

    def check_all(self):
        self.set_checked([s.key for s in self.sessions])

    def check_none(self):
        self.set_checked([])

    def _item_changed(self, *_):
        sel = self.checked()
        n = sum(s.n_labelled for s in sel if not s.is_blobset)
        nb = sum(s.n_labelled for s in sel if s.is_blobset)
        self.summary.setText("%d carpeta%s · %d fotogramas%s etiquetados" % (
            len(sel), "" if len(sel) == 1 else "s", n, " + %d blobs" % nb if nb else ""))
        self.store.setValue(self.key, ",".join(s.key for s in sel))
        self.changed.emit()


# ---- tables --------------------------------------------------------------------

class TablePicker(QWidget):
    """A combo of the tables (firmware first) with the chosen one's
    parameters and training sessions under it. With stages=True a table that
    has a classifier is offered twice, alone (stage 1) and with it (stage 2),
    so the two can be verified and compared like two tables."""
    changed = pyqtSignal()

    def __init__(self, store: QSettings, key: str, parent=None, stages: bool = False):
        super().__init__(parent)
        self.store, self.key, self.stages = store, key, stages
        self.tables: list[paths.Table] = []
        self.entries: list[tuple[paths.Table, int]] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        row = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(24)
        row.addWidget(self.combo, 1)
        b = QPushButton("Recargar")
        theme.set_prop(b, "small", True)
        b.clicked.connect(self.reload)
        row.addWidget(b)
        lay.addLayout(row)
        self.info = value_label("", wrap=True)
        theme.set_prop(self.info, "muted", True)
        lay.addWidget(self.info)
        self.combo.currentIndexChanged.connect(self._changed)
        self.reload()

    @staticmethod
    def entry_key(t: paths.Table, stage: int) -> str:
        return t.name + ("|2" if stage >= 2 else "")

    def reload(self, select: Optional[str] = None, stage: Optional[int] = None):
        if select is not None:
            want = self.entry_key_for(select, stage)
        elif self.current():
            want = self.entry_key(self.current(), self.stage())
        else:
            want = str(self.store.value(self.key, ""))
        self.tables = paths.find_tables()
        self.entries = []
        for t in self.tables:
            self.entries.append((t, 1))
            if self.stages and t.has_clf:
                self.entries.append((t, 2))
        self.combo.blockSignals(True)
        self.combo.clear()
        for t, st in self.entries:
            self.combo.addItem(t.name + ("  + clasificador" if st >= 2 else ""))
        idx = next((i for i, (t, st) in enumerate(self.entries)
                    if self.entry_key(t, st) == want), None)
        if idx is None:     # the stage-2 entry is gone: fall back to the table
            idx = next((i for i, (t, st) in enumerate(self.entries)
                        if self.entry_key(t, st) == want.split("|")[0]), 0)
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(False)
        self._changed()

    def entry_key_for(self, name: str, stage: Optional[int]) -> str:
        return name + ("|2" if stage and stage >= 2 else "")

    def current(self) -> Optional[paths.Table]:
        i = self.combo.currentIndex()
        return self.entries[i][0] if 0 <= i < len(self.entries) else None

    def stage(self) -> int:
        """1, or 2 for a "+ clasificador" entry."""
        i = self.combo.currentIndex()
        return self.entries[i][1] if 0 <= i < len(self.entries) else 1

    def select(self, name: str, stage: Optional[int] = None):
        self.reload(select=name, stage=stage)

    def _changed(self, *_):
        t = self.current()
        if t is None:
            self.info.setText("No hay tablas en tables/ ni en main/models/")
        else:
            self.info.setText(t.summary() + "\n" + t.clf_summary())
            self.store.setValue(self.key, self.entry_key(t, self.stage()))
        self.changed.emit()


# ---- metrics -------------------------------------------------------------------

class MetricCards(QWidget):
    """The totals as four cards: frames, blobs, blob 1, pixels. In pair mode
    (Compare) every value reads "A │ B", so two tables fit in the same four
    cards instead of eight."""

    def __init__(self, pair: bool = False, parent=None):
        super().__init__(parent)
        self.pair = pair
        # A flow, not a grid: four cards in a row need about 900 px. On a
        # narrower window they wrap instead of forcing the window wider.
        lay = FlowLayout(self, hspacing=8, vspacing=8)
        lay.setContentsMargins(0, 0, 0, 0)

        def ReadoutCard(title, subtitle=""):
            return _ReadoutCard(title, "A │ B" if pair else subtitle)

        self.c_frames = ReadoutCard("Fotogramas")
        self.v_frames = self.c_frames.add_kv("etiquetados")
        self.v_with = self.c_frames.add_kv("con globo")
        self.v_empty = self.c_frames.add_kv("sin globo")
        self.c_blobs = ReadoutCard("Blobs", "lo que ve la S3")
        self.v_tp = self.c_blobs.add_kv("TP (aciertos)")
        self.v_fn = self.c_blobs.add_kv("FN (perdidos)")
        self.v_fp = self.c_blobs.add_kv("FP (falsos)")
        self.v_split = self.c_blobs.add_kv("split")
        self.v_prec = self.c_blobs.add_kv("precisión / recall")
        self.c_b1 = ReadoutCard("Blob 1", "hacia donde va el dirigible")
        self.v_b1_ok = self.c_b1.add_kv("globo, con globo")
        self.v_b1_false = self.c_b1.add_kv("falso, sin globo")
        self.b1_bar = self.c_b1.add_bar(0.0, "ok_dot")
        self.c_px = ReadoutCard("Píxeles", "solo etiquetados")
        self.v_px_p = self.c_px.add_kv("precisión")
        self.v_px_r = self.c_px.add_kv("recall")
        self.v_px_f1 = self.c_px.add_kv("F1")
        self.px_bar = self.c_px.add_bar(0.0, "accent")
        for c in (self.c_frames, self.c_blobs, self.c_b1, self.c_px):
            c.setMinimumWidth(200)
            c.setMaximumWidth(420)
            c.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            lay.addWidget(c)
        if pair:
            self.b1_bar.setVisible(False)
            self.px_bar.setVisible(False)
        self.set_totals(None)

    def _values(self, t: Optional[E.Totals]) -> dict:
        if t is None:
            return {}
        return dict(
            frames=str(t.frames), with_=str(t.with_balloon), empty=str(t.empty),
            tp="%d de %d" % (t.tp, t.balloons), fn=str(t.fn), fp=str(t.fp),
            split=str(t.split), prec="%s / %s %%" % (pct(t.precision), pct(t.recall)),
            b1_ok="%d de %d" % (t.b1_ok, t.with_balloon),
            b1_false="%d de %d" % (t.false_b1, t.empty),
            px_p=pct(t.px_precision) + " %", px_r=pct(t.px_recall) + " %",
            px_f1=pct(t.px_f1) + " %")

    def set_totals(self, t: Optional[E.Totals], tb: Optional[E.Totals] = None):
        labels = dict(frames=self.v_frames, with_=self.v_with, empty=self.v_empty,
                      tp=self.v_tp, fn=self.v_fn, fp=self.v_fp, split=self.v_split,
                      prec=self.v_prec, b1_ok=self.v_b1_ok, b1_false=self.v_b1_false,
                      px_p=self.v_px_p, px_r=self.v_px_r, px_f1=self.v_px_f1)
        va, vb = self._values(t), self._values(tb)
        for k, lbl in labels.items():
            a = va.get(k, "–")
            lbl.setText("%s │ %s" % (a, vb.get(k, "–")) if self.pair else a)
        if t is None:
            self.b1_bar.set_fraction(0)
            self.px_bar.set_fraction(0)
            return
        self.b1_bar.set_fraction(t.b1_ok / t.with_balloon if t.with_balloon else 0,
                                 "err_dot" if t.false_b1 else "ok_dot")
        self.px_bar.set_fraction(0 if math.isnan(t.px_f1) else t.px_f1)


class SessionTable(QTableWidget):
    """One row per session: the totals that decide a verdict."""
    HEAD = ["Sesión", "Fotog.", "Globos", "TP", "FN", "FP", "split", "b1 falso", "px P%",
            "px R%", "px F1%"]

    def __init__(self, parent=None):
        super().__init__(0, len(self.HEAD), parent)
        _table_setup(self, self.HEAD)
        self.setSortingEnabled(False)       # sessions in the order chosen, total last

    def set_result(self, res: Optional[E.EvalResult]):
        self.setRowCount(0)
        if res is None:
            fit_height(self)
            return
        rows = list(res.by_session().items())
        if len(rows) > 1:
            rows.append(("total", res.totals()))
        self.setRowCount(len(rows))
        trained = {t.split(" (")[0] for t in res.trained_on()}
        for r, (name, t) in enumerate(rows):
            vals = [name, t.frames, t.balloons, t.tp, t.fn, t.fp, t.split,
                    "%d/%d" % (t.false_b1, t.empty), pct(t.px_precision), pct(t.px_recall),
                    pct(t.px_f1)]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                if name == "total":
                    it.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
                self.setItem(r, c, it)
            tone = "err" if t.false_b1 else "warn" if t.fn else "ok"
            _tint_row(self, r, tone)
            if name in trained:
                self.item(r, 0).setText(name + " (entren.)")
                self.item(r, 0).setToolTip("La tabla se entrenó con esta carpeta: cifras "
                                           "optimistas")
        fit_height(self)


def _table_setup(t: QTableWidget, head: Sequence[str]):
    t.setHorizontalHeaderLabels(list(head))
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setFont(theme.mono_font())
    t.setAlternatingRowColors(False)
    t.setSortingEnabled(True)
    hh = t.horizontalHeader()
    hh.setStretchLastSection(False)
    hh.setSectionResizeMode(hh.ResizeMode.ResizeToContents)
    hh.setSectionResizeMode(0, hh.ResizeMode.Stretch)
    t.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


def fit_height(t: QTableWidget, cap: int = 190):
    """A summary table as tall as its rows, up to cap: with one session it
    must not hold 170 px of empty space above the frames."""
    h = t.horizontalHeader().height() + 2 * t.frameWidth() + 4
    for r in range(t.rowCount()):
        h += t.rowHeight(r)
    t.setMaximumHeight(min(cap, max(h, 60)))


def _tint_row(t: QTableWidget, r: int, tone: str):
    c = theme.tone_colours(tone)
    bg, fg = QColor(c["bg"]), QColor(c["text"])
    for col in range(t.columnCount()):
        it = t.item(r, col)
        if it is not None:
            it.setBackground(bg)
            it.setForeground(fg)


class _NumItem(QTableWidgetItem):
    """Sorts by number, shows the text."""

    def __init__(self, value, text: Optional[str] = None):
        super().__init__(text if text is not None else str(value))
        self.value = value
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, _NumItem):
            a, b = self.value, other.value
            a = -1 if (isinstance(a, float) and math.isnan(a)) else a
            b = -1 if (isinstance(b, float) and math.isnan(b)) else b
            return a < b
        return super().__lt__(other)


class FrameTable(QTableWidget):
    """One row per frame, coloured by outcome; selecting a row shows it in
    the viewer (selected signal carries the FrameEval, or a pair when two
    tables are shown)."""
    HEAD = ["Sesión", "Fotograma", "Globos", "TP", "FN", "FP", "split", "Blob 1", "px P%",
            "px R%", "Resultado"]
    selected = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(0, len(self.HEAD), parent)
        _table_setup(self, self.HEAD)
        # Every column as wide as its content and the last one takes the
        # rest: the frame name must not push the verdict columns off-screen.
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(0, hh.ResizeMode.ResizeToContents)
        hh.setStretchLastSection(True)
        self.itemSelectionChanged.connect(self._sel)
        self._payload: list = []

    def set_frames(self, frames: Sequence[E.FrameEval], only: str = "all"):
        """only: "all" | "fail" | "ok"."""
        self.setSortingEnabled(False)
        self.setRowCount(0)
        self._payload = []
        shown = [f for f in frames if only == "all" or (only == "ok") == f.is_ok()]
        self.setRowCount(len(shown))
        for r, f in enumerate(shown):
            s = f.score
            vals = [QTableWidgetItem(f.session), QTableWidgetItem(f.name),
                    _NumItem(s.n_balloons), _NumItem(s.tp), _NumItem(s.fn), _NumItem(s.fp),
                    _NumItem(s.split), QTableWidgetItem(_b1_text(s.b1)),
                    _NumItem(E.L.ratio(s.px_tp, s.px_tp + s.px_fp),
                             pct(E.L.ratio(s.px_tp, s.px_tp + s.px_fp))),
                    _NumItem(E.L.ratio(s.px_tp, s.px_tp + s.px_fn),
                             pct(E.L.ratio(s.px_tp, s.px_tp + s.px_fn))),
                    QTableWidgetItem(f.outcome())]
            vals[0].setData(Qt.ItemDataRole.UserRole, r)
            for c, it in enumerate(vals):
                self.setItem(r, c, it)
            _tint_row(self, r, OUTCOME_TONE[f.outcome()])
            self._payload.append(f)
        self.setSortingEnabled(True)
        self.resizeColumnsToContents()

    def current_frame(self):
        rows = {i.row() for i in self.selectedIndexes()}
        if not rows:
            return None
        r = rows.pop()
        k = self.item(r, 0).data(Qt.ItemDataRole.UserRole)
        return self._payload[k] if k is not None and k < len(self._payload) else None

    def _sel(self):
        f = self.current_frame()
        if f is not None:
            self.selected.emit(f)


def _b1_text(b1: str) -> str:
    return {"balloon": "globo", "false": "FALSO", "none": "ninguno"}.get(b1, b1)


# ---- the frame viewer ----------------------------------------------------------

class FrameViewer(QWidget):
    """A frame with the table's mask, the labelled balloons and the reported
    blobs, and a line saying what happened."""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        head = QHBoxLayout()
        self.title = QLabel(title)
        self.title.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
        head.addWidget(self.title)
        self.pill = StatusPill("neutral", "sin fotograma")
        head.addWidget(self.pill)
        head.addStretch(1)
        lay.addLayout(head)
        self.view = PixelImageView()
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self.view, 1)
        self.detail = value_label("", wrap=True)
        theme.set_prop(self.detail, "muted", True)
        lay.addWidget(self.detail)
        legend = QLabel('<span style="color:%s">■</span> máscara de la tabla &nbsp; '
                        '<span style="color:%s">▢</span> globo etiquetado &nbsp; '
                        '<span style="color:%s">▢</span> blob en un globo &nbsp; '
                        '<span style="color:%s">▢</span> blob falso' % (
                            "#f2c14e", GT_COLOUR, OK_COLOUR, BAD_COLOUR))
        theme.set_prop(legend, "faint", True)
        legend.setFont(theme.ui_font(12))
        legend.setWordWrap(True)            # else its one line sets the viewer's minimum width
        lay.addWidget(legend)

    def show_frame(self, f: Optional[E.FrameEval]):
        if f is None:
            self.pill.set("neutral", "sin fotograma")
            self.detail.setText("")
            self.view.set_overlay(None)
            self.view.set_boxes([])
            self.view.set_marks([])
            return
        self.view.set_image(E.rgb(f))
        self.view.set_overlay(E.mask_overlay(f.mask, MASK_RGBA))
        self.view.set_boxes(E.boxes(f, OK_COLOUR, BAD_COLOUR, GT_COLOUR))
        self.view.set_marks([(b.cx, b.cy) for b in f.blobs])
        out = f.outcome()
        self.pill.set(OUTCOME_TONE[out], out)
        s = f.score
        cfg = f.sf.meta.get("camera_cfg") or {}
        awb = {0: "congelado", 1: "AUTO", 2: "fijo"}.get(cfg.get("awb"), "?")
        self.detail.setText(
            "%s/%s · %d globo(s) etiquetado(s), %d blob(s) reportado(s): TP %d FN %d FP %d "
            "split %d · blob 1 %s · px P %s %% R %s %% · AWB %s" % (
                f.session, f.name, s.n_balloons, s.n_reported, s.tp, s.fn, s.fp, s.split,
                _b1_text(s.b1), pct(E.L.ratio(s.px_tp, s.px_tp + s.px_fp)),
                pct(E.L.ratio(s.px_tp, s.px_tp + s.px_fn)), awb))


# ---- progress and log ----------------------------------------------------------

class Progress(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.bar = Bar(0.0)
        lay.addWidget(self.bar, 1)
        self.text = value_label("")
        theme.set_prop(self.text, "muted", True)
        lay.addWidget(self.text)

    def set(self, done: int, total: int, text: str = ""):
        self.bar.set_fraction(done / total if total else 0)
        self.text.setText(text or ("%d / %d" % (done, total) if total else ""))


class LogView(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(theme.mono_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setMinimumHeight(140)

    def append_text(self, text: str):
        self.moveCursor(self.textCursor().MoveOperation.End)
        self.insertPlainText(text)
        self.moveCursor(self.textCursor().MoveOperation.End)
