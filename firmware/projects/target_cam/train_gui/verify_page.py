"""
Verificar: one table on one or more sessions, with the README's verdict, the
totals per session, every frame coloured by outcome and the chosen frame
drawn with the table's mask and the camera's blobs.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)  # noqa: F401

from tcview.ui import theme
from tcview.ui.widgets import Banner, CollapsibleSection, GripSplitter, SegmentedButtons

from . import engine as E
from . import jobs
from .widgets import (FrameTable, FrameViewer, MetricCards, Progress, SessionPicker,
                      SessionTable, TablePicker, scroll_column)


def _titled(title: str, w: QWidget) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    t = QLabel(title)
    t.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
    lay.addWidget(t)
    lay.addWidget(w)
    return box


def _column(form: QWidget, *fixed: QWidget) -> QWidget:
    """A page's left column: the form scrolls, the widgets after it (progress,
    the run button) stay below it, always in view."""
    col = QWidget()
    lay = QVBoxLayout(col)
    lay.setContentsMargins(0, 0, 8, 12)
    lay.setSpacing(8)
    lay.addWidget(scroll_column(form), 1)
    for w in fixed:
        w.setContentsMargins(12, 0, 0, 0)
        lay.addWidget(w)
    return col


class VerifyPage(QWidget):
    def __init__(self, store: QSettings, parent=None):
        super().__init__(parent)
        self.store = store
        self.worker: jobs.EvalWorker | None = None
        self.result: E.EvalResult | None = None

        split = GripSplitter(Qt.Orientation.Horizontal)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(split)

        form = QWidget()
        ll = QVBoxLayout(form)
        ll.setContentsMargins(12, 12, 8, 4)
        ll.setSpacing(10)
        sec = CollapsibleSection("Tabla")
        self.table = TablePicker(store, "verify/table", stages=True)
        sec.add_widget(self.table)
        ll.addWidget(sec)
        sec = CollapsibleSection("Carpetas de verificación")
        self.sessions = SessionPicker(store, "verify/sessions")
        sec.add_widget(self.sessions, 1)
        ll.addWidget(sec, 1)
        self.progress = Progress()
        self.run_btn = QPushButton("Evaluar")
        theme.set_role(self.run_btn, "primary")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.clicked.connect(self._run)
        split.addWidget(_column(form, self.progress, self.run_btn))
        split.setChildrenCollapsible(False)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 12, 12, 12)
        rl.setSpacing(8)
        self.banner = Banner("info", "Elige una tabla y carpetas, y pulsa Evaluar.")
        rl.addWidget(self.banner)
        self.cards = MetricCards()
        rl.addWidget(self.cards)
        # The per-session table sizes itself to its rows (fit_height), so it
        # sits above the splitter rather than in it: a splitter pane would
        # keep the space the table gives back.
        self.by_session = SessionTable()
        self.by_session.set_result(None)
        rl.addWidget(_titled("Por sesión", self.by_session))

        frames_box = QWidget()
        fl = QVBoxLayout(frames_box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(4)
        head = QHBoxLayout()
        t = QLabel("Fotogramas")
        t.setFont(theme.ui_font(12, QFont.Weight.DemiBold))
        head.addWidget(t)
        self.filter = SegmentedButtons([("all", "Todos"), ("fail", "Fallan"), ("ok", "Aciertan")])
        self.filter.changed.connect(lambda _k: self._fill_frames())
        head.addWidget(self.filter)
        head.addStretch(1)
        self.copy_btn = QPushButton("Copiar informe")
        theme.set_prop(self.copy_btn, "small", True)
        self.copy_btn.clicked.connect(self._copy)
        head.addWidget(self.copy_btn)
        fl.addLayout(head)
        hsplit = GripSplitter(Qt.Orientation.Horizontal)
        self.frames = FrameTable()
        self.frames.selected.connect(self.viewer_show)
        hsplit.addWidget(self.frames)
        self.viewer = FrameViewer("Fotograma elegido")
        hsplit.addWidget(self.viewer)
        hsplit.setStretchFactor(0, 3)
        hsplit.setStretchFactor(1, 2)
        hsplit.setSizes([640, 480])
        self.hsplit = hsplit
        fl.addWidget(hsplit, 1)
        rl.addWidget(frames_box, 1)
        split.addWidget(scroll_column(right, 400, hscroll=True))
        split.setStretchFactor(1, 1)
        split.setSizes([380, 1000])
        self.split = split


    def select_table(self, name: str, stage: int = 1):
        self.table.select(name, stage)

    def reload_tables(self):
        self.table.reload()

    # ---- running -----------------------------------------------------------------

    def _run(self):
        if self.worker is not None:
            self.worker.cancel()
            return
        t = self.table.current()
        sel = [s for s in self.sessions.checked() if s.n_labelled]
        if t is None or not sel:
            self.banner.set("warn", "Hace falta una tabla y al menos una carpeta etiquetada.")
            return
        self.run_btn.setText("Cancelar")
        stage = self.table.stage()
        self.banner.set("info", "Evaluando %s%s en %s…" % (
            t.name, " + clasificador" if stage >= 2 else "", ", ".join(s.key for s in sel)))
        self.worker = jobs.EvalWorker(t, sel, self, stage)
        self.worker.progress.connect(self.progress.set)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _finish_worker(self):
        self.worker = None
        self.run_btn.setText("Evaluar")

    def _failed(self, msg: str):
        self._finish_worker()
        self.banner.set("err", "La evaluación falló: %s" % msg)

    def _done(self, res: E.EvalResult):
        self._finish_worker()
        self.result = res
        self.progress.set(len(res.frames), len(res.frames), "%d fotogramas" % len(res.frames))
        v = E.judge(res)
        text = "<b>%s</b> — %s" % (v.title, " ".join(v.reasons))
        if v.caveats:
            text += "<br><i>%s</i>" % " ".join(v.caveats)
        self.banner.set(v.tone, text)
        self.cards.set_totals(res.totals())
        self.by_session.set_result(res)
        self._fill_frames()

    def _fill_frames(self):
        if self.result is None:
            return
        self.frames.set_frames(self.result.frames, self.filter.value())
        if self.frames.rowCount():
            self.frames.selectRow(0)
        else:
            self.viewer.show_frame(None)

    def viewer_show(self, f):
        self.viewer.show_frame(f)

    def _copy(self):
        if self.result is None:
            return
        QApplication.clipboard().setText(report(self.result))


def report(res: E.EvalResult) -> str:
    """A plain-text report in eval_frames.py's shape, for notes and commits."""
    v = E.judge(res)
    lines = ["tabla %s (%s)" % (res.label, res.table.summary())]
    if res.stage >= 2:
        lines.append("etapa 2: %s" % res.table.clf_summary())
    lines += ["carpetas %s" % ", ".join(s.key for s in res.sessions), "",
             "%-36s %3s %3s %3s %3s %5s  %-7s %6s %6s" % (
                 "frame", "bal", "TP", "FN", "FP", "split", "blob 1", "px P%", "px R%")]
    for f in res.frames:
        s = f.score
        lines.append("%-36s %3d %3d %3d %3d %5d  %-7s %6s %6s" % (
            s.name[:36], s.n_balloons, s.tp, s.fn, s.fp, s.split, s.b1,
            _p(E.L.ratio(s.px_tp, s.px_tp + s.px_fp)), _p(E.L.ratio(s.px_tp, s.px_tp + s.px_fn))))
    lines.append("")
    for name, t in list(res.by_session().items()) + [("total", res.totals())]:
        lines.append("%-12s frames %3d  TP %3d FN %3d FP %3d split %d  false b1 %d/%d  "
                     "px P %s R %s F1 %s" % (name, t.frames, t.tp, t.fn, t.fp, t.split,
                                            t.false_b1, t.empty, _p(t.px_precision),
                                            _p(t.px_recall), _p(t.px_f1)))
    lines += ["", v.title] + ["  - " + r for r in v.reasons] + ["  ! " + c for c in v.caveats]
    return "\n".join(lines)


def _p(x: float) -> str:
    return E.pct(x).replace(" %", "")
