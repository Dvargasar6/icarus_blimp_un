"""
Comparar: two tables on the same sessions. Both are scored, their totals are
set side by side with the differences, engine.compare gives the README's
verdict, and the frames where the two tables disagree are listed and drawn
one beside the other.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from tcview.ui import theme
from tcview.ui.widgets import Banner, CollapsibleSection, GripSplitter

from . import engine as E
from . import jobs
from .widgets import (OUTCOME_TONE, FrameViewer, MetricCards, Progress, SessionPicker,
                      TablePicker, _table_setup, _tint_row, fit_height, pct, scroll_column)
from .verify_page import _column, _titled


class CompareTable(QTableWidget):
    """Per session and in total: A, B and the direction of the change for
    each figure the verdict uses."""
    HEAD = ["Sesión", "FN A", "FN B", "FP A", "FP B", "b1 falso A", "b1 falso B",
            "px F1 A", "px F1 B", "Mejor"]

    def __init__(self, parent=None):
        super().__init__(0, len(self.HEAD), parent)
        _table_setup(self, self.HEAD)
        self.setSortingEnabled(False)

    def set_comparison(self, c: E.Comparison | None):
        self.setRowCount(0)
        if c is None:
            fit_height(self)
            return
        rows = [(d.session, d.a, d.b, d.better()) for d in c.sessions]
        if len(rows) > 1:
            ta, tb = c.a.totals(), c.b.totals()
            rows.append(("total", ta, tb, E.SessionDiff("total", ta, tb).better()))
        self.setRowCount(len(rows))
        for r, (name, a, b, better) in enumerate(rows):
            vals = [name, a.fn, b.fn, a.fp, b.fp, "%d/%d" % (a.false_b1, a.empty),
                    "%d/%d" % (b.false_b1, b.empty), pct(a.px_f1), pct(b.px_f1),
                    {"A": "A", "B": "B", "tie": "=", "mixed": "mixto"}[better]]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if col:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                if name == "total":
                    it.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
                self.setItem(r, col, it)
            _tint_row(self, r, {"A": "info", "B": "ok", "tie": "neutral", "mixed": "warn"}[better])
        fit_height(self)


class DiffTable(QTableWidget):
    HEAD = ["Sesión", "Fotograma", "Globos", "A: FN/FP", "A: blob 1", "A", "B: FN/FP",
            "B: blob 1", "B"]

    def __init__(self, parent=None):
        super().__init__(0, len(self.HEAD), parent)
        _table_setup(self, self.HEAD)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(0, hh.ResizeMode.ResizeToContents)
        hh.setStretchLastSection(True)
        self.setSortingEnabled(False)
        self.pairs: list = []

    def set_pairs(self, pairs):
        self.pairs = list(pairs)
        self.setRowCount(len(self.pairs))
        b1 = {"balloon": "globo", "false": "FALSO", "none": "ninguno"}
        for r, (fa, fb) in enumerate(self.pairs):
            vals = [fa.session, fa.name, fa.score.n_balloons,
                    "%d/%d" % (fa.score.fn, fa.score.fp), b1[fa.score.b1], fa.outcome(),
                    "%d/%d" % (fb.score.fn, fb.score.fp), b1[fb.score.b1], fb.outcome()]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if col >= 2:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                self.setItem(r, col, it)
            self.resizeColumnsToContents()
            ca, cb = theme.tone_colours(OUTCOME_TONE[fa.outcome()]), \
                theme.tone_colours(OUTCOME_TONE[fb.outcome()])
            for col in (3, 4, 5):
                self.item(r, col).setBackground(theme.qcolor(ca["bg"]))
                self.item(r, col).setForeground(theme.qcolor(ca["text"]))
            for col in (6, 7, 8):
                self.item(r, col).setBackground(theme.qcolor(cb["bg"]))
                self.item(r, col).setForeground(theme.qcolor(cb["text"]))

    def current_pair(self):
        rows = {i.row() for i in self.selectedIndexes()}
        return self.pairs[rows.pop()] if rows else None


class ComparePage(QWidget):
    def __init__(self, store: QSettings, parent=None):
        super().__init__(parent)
        self.store = store
        self.workers: list[jobs.EvalWorker] = []
        self.results: dict[str, E.EvalResult] = {}
        self.comparison: E.Comparison | None = None

        split = GripSplitter(Qt.Orientation.Horizontal)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(split)

        form = QWidget()
        ll = QVBoxLayout(form)
        ll.setContentsMargins(12, 12, 8, 4)
        ll.setSpacing(10)
        sec = CollapsibleSection("Tabla A", "la referencia (p. ej. la del firmware)")
        self.table_a = TablePicker(store, "compare/table_a", stages=True)
        sec.add_widget(self.table_a)
        ll.addWidget(sec)
        sec = CollapsibleSection("Tabla B", "la candidata")
        self.table_b = TablePicker(store, "compare/table_b", stages=True)
        sec.add_widget(self.table_b)
        ll.addWidget(sec)
        sec = CollapsibleSection("Carpetas de verificación")
        self.sessions = SessionPicker(store, "compare/sessions")
        sec.add_widget(self.sessions, 1)
        ll.addWidget(sec, 1)
        self.progress = Progress()
        self.run_btn = QPushButton("Comparar")
        theme.set_role(self.run_btn, "primary")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.clicked.connect(self._run)
        split.addWidget(_column(form, self.progress, self.run_btn))
        split.setChildrenCollapsible(False)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 12, 12, 12)
        rl.setSpacing(8)
        self.banner = Banner("info", "Elige dos tablas y carpetas, y pulsa Comparar.")
        rl.addWidget(self.banner)
        self.names = QLabel("A │ B")
        self.names.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
        self.names.setWordWrap(True)
        rl.addWidget(self.names)
        self.cards = MetricCards(pair=True)
        rl.addWidget(self.cards)

        self.by_session = CompareTable()
        self.by_session.set_comparison(None)
        rl.addWidget(_titled("Por sesión (A → B)", self.by_session))
        diff_box = QWidget()
        dl = QVBoxLayout(diff_box)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(4)
        head = QHBoxLayout()
        self.diff_title = QLabel("Fotogramas donde difieren")
        self.diff_title.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
        head.addWidget(self.diff_title)
        head.addStretch(1)
        self.copy_btn = QPushButton("Copiar informe")
        theme.set_prop(self.copy_btn, "small", True)
        self.copy_btn.clicked.connect(self._copy)
        head.addWidget(self.copy_btn)
        dl.addLayout(head)
        hsplit = GripSplitter(Qt.Orientation.Horizontal)
        self.diff = DiffTable()
        self.diff.itemSelectionChanged.connect(self._show_pair)
        hsplit.addWidget(self.diff)
        viewers = QWidget()
        vl = QHBoxLayout(viewers)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(8)
        self.view_a = FrameViewer("Tabla A")
        self.view_b = FrameViewer("Tabla B")
        vl.addWidget(self.view_a)
        vl.addWidget(self.view_b)
        hsplit.addWidget(viewers)
        hsplit.setStretchFactor(0, 2)
        hsplit.setStretchFactor(1, 3)
        hsplit.setSizes([520, 760])
        self.hsplit = hsplit
        dl.addWidget(hsplit, 1)
        rl.addWidget(diff_box, 1)
        split.addWidget(scroll_column(right, 400, hscroll=True))
        split.setStretchFactor(1, 1)
        split.setSizes([380, 1000])
        self.split = split

    def reload_tables(self):
        self.table_a.reload()
        self.table_b.reload()

    def select_b(self, name: str, stage: int = 1):
        self.table_b.select(name, stage)

    # ---- running -----------------------------------------------------------------

    def _run(self):
        if self.workers:
            for w in self.workers:
                w.cancel()
            return
        a, b = self.table_a.current(), self.table_b.current()
        sel = [s for s in self.sessions.checked() if s.n_labelled]
        if a is None or b is None or not sel:
            self.banner.set("warn", "Hacen falta dos tablas y al menos una carpeta etiquetada.")
            return
        sa, sb = self.table_a.stage(), self.table_b.stage()
        if a.bin.resolve() == b.bin.resolve() and sa == sb:
            self.banner.set("warn", "A y B son la misma tabla en la misma etapa.")
            return
        self.results = {}
        self.comparison = None
        self.run_btn.setText("Cancelar")
        self.banner.set("info", "Evaluando A y B en %s…" % ", ".join(s.key for s in sel))
        self._total = 0
        self._done_n = {"A": 0, "B": 0}
        for tag, t, st in (("A", a, sa), ("B", b, sb)):
            w = jobs.EvalWorker(t, sel, self, st)
            w.progress.connect(lambda d, n, tag=tag: self._progress(tag, d, n))
            w.done.connect(lambda res, tag=tag: self._done(tag, res))
            w.failed.connect(self._failed)
            self.workers.append(w)
            w.start()

    def _progress(self, tag, done, total):
        self._done_n[tag] = done
        self._total = total
        self.progress.set(sum(self._done_n.values()), 2 * total)

    def _failed(self, msg):
        for w in self.workers:
            w.cancel()
        self.workers = []
        self.run_btn.setText("Comparar")
        self.banner.set("err", "La evaluación falló: %s" % msg)

    def _done(self, tag, res):
        self.results[tag] = res
        if len(self.results) < 2:
            return
        self.workers = []
        self.run_btn.setText("Comparar")
        a, b = self.results["A"], self.results["B"]
        c = self.comparison = E.compare(a, b)
        text = "<b>%s</b> — %s" % (c.title, " ".join(c.reasons))
        if c.caveats:
            text += "<br><i>%s</i>" % " ".join(c.caveats)
        self.banner.set(c.tone, text)
        self.names.setText("A: %s  │  B: %s" % (a.label, b.label))
        self.cards.set_totals(a.totals(), b.totals())
        self.by_session.set_comparison(c)
        pairs = c.differing_frames()
        self.diff_title.setText("Fotogramas donde difieren (%d de %d: otro resultado de blobs, "
                                "o píxeles a más de 5 puntos)" % (len(pairs), len(b.frames)))
        self.diff.set_pairs(pairs)
        if pairs:
            self.diff.selectRow(0)
        else:
            self.view_a.show_frame(None)
            self.view_b.show_frame(None)

    def _show_pair(self):
        p = self.diff.current_pair()
        if p is None:
            return
        self.view_a.show_frame(p[0])
        self.view_b.show_frame(p[1])

    def _copy(self):
        c = self.comparison
        if c is None:
            return
        lines = ["A: %s (%s)" % (c.a.label, c.a.table.summary()),
                 "B: %s (%s)" % (c.b.label, c.b.table.summary()),
                 "carpetas: %s" % ", ".join(s.key for s in c.b.sessions), "",
                 "%-12s %5s %5s %5s %5s %9s %9s %8s %8s %s" % (
                     "sesion", "FN A", "FN B", "FP A", "FP B", "b1falso A", "b1falso B",
                     "pxF1 A", "pxF1 B", "mejor")]
        for d in c.sessions + [E.SessionDiff("total", c.a.totals(), c.b.totals())]:
            lines.append("%-12s %5d %5d %5d %5d %9s %9s %8s %8s %s" % (
                d.session, d.a.fn, d.b.fn, d.a.fp, d.b.fp, "%d/%d" % (d.a.false_b1, d.a.empty),
                "%d/%d" % (d.b.false_b1, d.b.empty), pct(d.a.px_f1), pct(d.b.px_f1), d.better()))
        lines += ["", c.title] + ["  - " + r for r in c.reasons] + ["  ! " + x for x in c.caveats]
        QApplication.clipboard().setText("\n".join(lines))
