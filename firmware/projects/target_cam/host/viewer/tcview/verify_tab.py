"""
Verify: score one model, or compare two, on labelled test folders.

A model is a model set at a stage (evaluation.Candidate), or stage 0 (the HSV
test with each frame's own saved settings). The same set at two stages is a
fair comparison too: a set trained for stage 2 holds a copy of its table, so
"set X at stage 1" against "set X at stage 2" is the table alone against the
table with its classifier; that is what Train's "Verify this set" opens.

The figures are eval_frames.py's (evaluation.py), per frame, so the frame list
can show which frames fail and draw each one: the mask of the stage that ran
(tinted), the hand-labelled balloons (dotted), and the reported blobs, green
in a balloon and red outside one, numbered as the camera reports them and with
the classifier's confidence from stage 2. The verdicts are evaluation.judge
and evaluation.compare: the README's rule (every balloon found, no false blob
1), the A/B rule, and "cannot conclude" when the figures pull both ways or the
set is too small for one frame to mean anything.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QProgressBar, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from . import evaluation as E
from . import jobs as J
from . import library as Lib
from .data_picker import DataPicker
from .ui import persist, theme
from .ui.widgets import (Banner, CollapsibleSection, CopyButton, GripSplitter,
                         PixelImageView, ReadoutTable, SegmentedButtons, value_label)

HSV = "__hsv__"                 # the model combo's entry for stage 0
OUTCOME_TONE = {"ok": "ok_text", "partial": "warn_text", "false": "warn_text",
                "missed": "err_text", "false b1": "err_text"}
FILTERS = (("all", "All"), ("fail", "Failing"), ("pass", "Passing"), ("diff", "A ≠ B"))
METRICS = (("frames", "frames"), ("balloons", "balloons"), ("tp", "TP"), ("fn", "FN"),
           ("fp", "FP"), ("split", "split"), ("false_b1", "false blob 1"),
           ("precision", "blob precision"), ("recall", "blob recall"),
           ("px_precision", "pixel precision"), ("px_recall", "pixel recall"),
           ("px_f1", "pixel F1"))


def metric_text(t: E.Totals, key: str) -> str:
    if key == "false_b1":
        return "%d / %d" % (t.false_b1, t.empty)
    v = getattr(t, key)
    return E.pct(v) if isinstance(v, float) else str(v)


class ModelPicker(QWidget):
    """A model set and one of its stages, or stage 0 (HSV)."""
    changed = pyqtSignal()

    def __init__(self, models_dir: Path, firmware_dir: Path, parent=None):
        super().__init__(parent)
        self.models_dir, self.firmware_dir = Path(models_dir), Path(firmware_dir)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.set_combo = QComboBox()
        self.stage_combo = QComboBox()
        self.set_combo.currentIndexChanged.connect(self._set_changed)
        self.stage_combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        lay.addWidget(self.set_combo, 1)
        lay.addWidget(self.stage_combo)
        self.info = value_label("", wrap=True)
        self.reload()

    def reload(self):
        keep, st = self.set_combo.currentData(), self.stage()
        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        self.set_combo.addItem("stage 0 · HSV (each frame's settings)", HSV)
        for s in Lib.find_sets(self.models_dir, self.firmware_dir):
            self.set_combo.addItem("%s  (stages %s)" % (s.name, ", ".join(map(str, s.stages))),
                                   str(s.path))
        i = self.set_combo.findData(keep)
        self.set_combo.setCurrentIndex(i if i >= 0 else min(1, self.set_combo.count() - 1))
        self.set_combo.blockSignals(False)
        self._set_changed(keep_stage=st)

    def mset(self) -> Lib.ModelSet | None:
        p = self.set_combo.currentData()
        if not p or p == HSV:
            return None
        return Lib.load_set(Path(p), firmware=Path(p).resolve() == self.firmware_dir.resolve())

    def stage(self) -> int | None:
        return self.stage_combo.currentData()

    def candidate(self) -> E.Candidate | None:
        if self.set_combo.currentData() == HSV:
            return E.Candidate(None, 0)
        s, st = self.mset(), self.stage()
        return None if s is None or st is None else E.Candidate(s, st)

    def select(self, path, stage: int | None = None):
        i = self.set_combo.findData(HSV if path == HSV else str(path))
        if i >= 0:
            self.set_combo.setCurrentIndex(i)
        if stage is not None:
            j = self.stage_combo.findData(stage)
            if j >= 0:
                self.stage_combo.setCurrentIndex(j)

    def _set_changed(self, *_, keep_stage=None):
        s = self.mset()
        self.stage_combo.blockSignals(True)
        self.stage_combo.clear()
        if self.set_combo.currentData() == HSV:
            self.stage_combo.addItem("stage 0", 0)
        elif s is not None:
            for st in s.stages:
                self.stage_combo.addItem("stage %d" % st, st)
        j = self.stage_combo.findData(keep_stage)
        self.stage_combo.setCurrentIndex(j if j >= 0 else self.stage_combo.count() - 1)
        self.stage_combo.blockSignals(False)
        self.info.setText("" if s is None else "\n".join(
            "%s: %s" % (k, s.summary(k)) for k in Lib.KINDS if s.has(k)))
        self.changed.emit()


class VerifyTab(QWidget):
    def __init__(self, main=None, data_dir: Path = Lib.DATA_DIR,
                 models_dir: Path = Lib.MODELS_DIR, firmware_dir: Path = Lib.FIRMWARE_MODELS):
        super().__init__()
        self.main = main
        self.res_a: E.EvalResult | None = None
        self.res_b: E.EvalResult | None = None
        self.cmp: E.Comparison | None = None
        self.worker: J.EvalWorker | None = None
        self._queue: list = []
        self._rows: list = []           # (FrameEval A, FrameEval B or None) per list row

        self.split = GripSplitter(Qt.Orientation.Horizontal)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.split)

        # ---- left: what to evaluate on what -------------------------------------------
        form = QWidget()
        fl = QVBoxLayout(form)
        fl.setContentsMargins(12, 12, 8, 12)
        fl.setSpacing(10)
        sec = CollapsibleSection("Model A")
        self.pick_a = ModelPicker(models_dir, firmware_dir)
        sec.add_widget(self.pick_a)
        sec.add_widget(self.pick_a.info)
        fl.addWidget(sec)
        self.compare = QCheckBox("Compare with model B")
        fl.addWidget(self.compare)
        self.sec_b = CollapsibleSection("Model B")
        self.pick_b = ModelPicker(models_dir, firmware_dir)
        self.sec_b.add_widget(self.pick_b)
        self.sec_b.add_widget(self.pick_b.info)
        fl.addWidget(self.sec_b)
        sec = CollapsibleSection("Test data")
        self.picker = DataPicker(data_dir)
        sec.add_widget(self.picker, 1)
        fl.addWidget(sec, 1)
        row = QHBoxLayout()
        self.eval_btn = QPushButton("Evaluate")
        theme.set_role(self.eval_btn, "primary")
        self.eval_btn.clicked.connect(self.evaluate)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        row.addWidget(self.eval_btn)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.progress, 1)
        fl.addLayout(row)
        self.why = value_label("", mono=False, wrap=True)
        fl.addWidget(self.why)
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setFrameShape(QScrollArea.Shape.NoFrame)
        left.setWidget(form)
        self.split.addWidget(left)

        # ---- right: results over frames ------------------------------------------------
        self.rsplit = GripSplitter(Qt.Orientation.Vertical)
        res = QWidget()
        rl = QVBoxLayout(res)
        rl.setContentsMargins(8, 12, 12, 4)
        rl.setSpacing(8)
        hdr = QHBoxLayout()
        cap = QLabel("RESULTS")
        cap.setFont(theme.caption_font())
        hdr.addWidget(cap)
        hdr.addStretch(1)
        hdr.addWidget(CopyButton(self.report, "Copy report"))
        rl.addLayout(hdr)
        self.banner = Banner("neutral", "Choose a model and test folders, then Evaluate.")
        rl.addWidget(self.banner)
        # A's and B's own verdicts side by side: the comparison above is the
        # headline, and stacked they would push the figures off the screen.
        ab = QHBoxLayout()
        self.banner_a = Banner("neutral", "")
        self.banner_b = Banner("neutral", "")
        ab.addWidget(self.banner_a, 1)
        ab.addWidget(self.banner_b, 1)
        rl.addLayout(ab)
        grid = QGridLayout()
        self.totals = ReadoutTable(["figure", "A", "B"])
        self.sessions = ReadoutTable(["session", "A: TP/FN/FP", "A: false b1", "A: px F1",
                                      "B: TP/FN/FP", "B: false b1", "B: px F1", "better"])
        grid.addWidget(self.totals, 0, 0)
        grid.addWidget(self.sessions, 0, 1)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 3)
        rl.addLayout(grid)
        rl.addStretch(1)
        self._fit_rows(self.totals, len(METRICS))
        res_scroll = QScrollArea()
        res_scroll.setWidgetResizable(True)
        res_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        res_scroll.setWidget(res)
        self.rsplit.addWidget(res_scroll)

        frames = QWidget()
        frl = QHBoxLayout(frames)
        frl.setContentsMargins(8, 4, 12, 12)
        lcol = QVBoxLayout()
        self.filter = SegmentedButtons(FILTERS, "all")
        self.filter.changed.connect(lambda _v: self._fill_list())
        lcol.addWidget(self.filter)
        self.listw = QListWidget()
        self.listw.setFont(theme.mono_font())
        self.listw.currentRowChanged.connect(self._show_frame)
        lcol.addWidget(self.listw, 1)
        frl.addLayout(lcol, 2)
        vcol = QVBoxLayout()
        vrow = QHBoxLayout()
        self.which = SegmentedButtons((("a", "Draw A"), ("b", "Draw B")), "a")
        self.which.changed.connect(lambda _v: self._show_frame(self.listw.currentRow()))
        vrow.addWidget(self.which)
        self.show_mask = QCheckBox("Mask")
        self.show_mask.setChecked(True)
        self.show_mask.toggled.connect(lambda _v: self._show_frame(self.listw.currentRow()))
        vrow.addWidget(self.show_mask)
        vrow.addStretch(1)
        vcol.addLayout(vrow)
        self.view = PixelImageView()
        vcol.addWidget(self.view, 1)
        self.frame_info = value_label("", wrap=True)
        vcol.addWidget(self.frame_info)
        frl.addLayout(vcol, 3)
        self.rsplit.addWidget(frames)
        self.rsplit.setStretchFactor(0, 1)
        self.rsplit.setStretchFactor(1, 1)
        self.split.addWidget(self.rsplit)
        left.setMinimumWidth(300)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 3)

        for w in (self.pick_a, self.pick_b, self.picker):
            w.changed.connect(self._refresh)
        self.compare.toggled.connect(self._refresh)
        self._refresh()

    @staticmethod
    def _fit_rows(table: ReadoutTable, n: int):
        """Tall enough for n rows, so the figures read without scrolling the
        table (the results pane scrolls instead)."""
        h = table.horizontalHeader().sizeHint().height() + n * table.verticalHeader() \
            .defaultSectionSize() + 2 * table.frameWidth() + 4
        table.setMinimumHeight(h)

    # ---- state ---------------------------------------------------------------------------

    def reload_sets(self):
        self.pick_a.reload()
        self.pick_b.reload()

    def problems(self) -> list[str]:
        out = []
        if self.worker is not None:
            out.append("An evaluation is running.")
        if self.pick_a.candidate() is None:
            out.append("Choose model A.")
        if self.compare.isChecked() and self.pick_b.candidate() is None:
            out.append("Choose model B.")
        ss = self.picker.sessions()
        if not ss:
            out.append("Check at least one test folder.")
        return out

    def _refresh(self, *_):
        self.sec_b.setVisible(self.compare.isChecked())
        probs = self.problems()
        self.eval_btn.setEnabled(not probs)
        self.cancel_btn.setEnabled(self.worker is not None)
        self.why.setText(" ".join(p for p in probs if "running" not in p))

    # ---- evaluation ----------------------------------------------------------------------

    def verify(self, path: Path):
        """Train's "Verify this set": the set at its top stage, against the
        same set one stage lower when it has one (the table alone)."""
        self.reload_sets()
        s = Lib.load_set(Path(path))
        if s is None:
            return
        self.pick_a.select(Path(path), s.top_stage)
        if s.top_stage >= 2:
            self.pick_b.select(Path(path), s.top_stage - 1)
            self.compare.setChecked(True)
        self.banner.set("info", "Check test folders the set was not trained on, then Evaluate. "
                        "A = %s at stage %d%s." % (s.name, s.top_stage,
                                                   ", B = the same set at stage %d"
                                                   % (s.top_stage - 1)
                                                   if s.top_stage >= 2 else ""))
        self._refresh()

    def evaluate(self) -> bool:
        if self.problems():
            return False
        ss = self.picker.sessions()
        self._queue = [("a", self.pick_a.candidate())]
        if self.compare.isChecked():
            self._queue.append(("b", self.pick_b.candidate()))
        self.res_a = self.res_b = self.cmp = None
        self._sessions = ss
        self.banner.set("info", "Evaluating…")
        self._next()
        return True

    def _next(self):
        if not self._queue:
            self.worker = None
            self._show_results()
            self._refresh()
            return
        which, cand = self._queue.pop(0)
        self.worker = J.EvalWorker(cand, self._sessions, self)
        self.worker.progress.connect(lambda d, n, w=which: self._progress(w, d, n))
        self.worker.done.connect(lambda r, w=which: self._done(w, r))
        self.worker.failed.connect(self._failed)
        self.worker.start()
        self._refresh()

    def _progress(self, which, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.progress.setFormat("%s: %%v / %%m frames" % which.upper())

    def _done(self, which, res):
        self.worker.wait()
        setattr(self, "res_" + which, res)
        self._next()

    def _failed(self, msg):
        self.worker.wait()
        self.worker = None
        self._queue = []
        self.banner.set("err", "Evaluation failed: %s" % msg)
        self._refresh()

    def cancel(self):
        if self.worker is not None:
            self._queue = []
            self.worker.cancel()

    def wait(self):
        """Block until the running evaluations end (tests)."""
        from PyQt6.QtWidgets import QApplication
        while self.worker is not None:
            self.worker.wait()
            QApplication.processEvents()

    # ---- results -------------------------------------------------------------------------

    def _show_results(self):
        a, b = self.res_a, self.res_b
        if a is None:
            return
        va = E.judge(a)
        self.banner_a.set(va.tone, "A · %s\n%s" % (a.label, va.text()))
        self.banner_b.setVisible(b is not None)
        if b is not None:
            vb = E.judge(b)
            self.banner_b.set(vb.tone, "B · %s\n%s" % (b.label, vb.text()))
            self.cmp = E.compare(a, b)
            self.banner.set(self.cmp.tone, self.cmp.text())
        else:
            self.banner.set(va.tone, "%s: %s" % (a.label, va.title))
        ta, tb = a.totals(), (b.totals() if b else None)
        self.totals.set_rows([(lbl, metric_text(ta, k), metric_text(tb, k) if tb else "")
                              for k, lbl in METRICS])
        rows = []
        for s in a.sessions:
            sa = a.totals(s.key)
            r = [s.key, "%d/%d/%d" % (sa.tp, sa.fn, sa.fp), "%d/%d" % (sa.false_b1, sa.empty),
                 E.pct(sa.px_f1)]
            if b is not None:
                sb = b.totals(s.key)
                d = E.SessionDiff(s.key, sa, sb).better()
                r += ["%d/%d/%d" % (sb.tp, sb.fn, sb.fp), "%d/%d" % (sb.false_b1, sb.empty),
                      E.pct(sb.px_f1), d]
            else:
                r += ["", "", "", ""]
            rows.append(r)
        self.sessions.set_rows(rows)
        self._fit_rows(self.sessions, max(len(rows), 1))
        self.filter.buttons["diff"].setEnabled(b is not None)
        self.which.buttons["b"].setEnabled(b is not None)
        if b is None:
            self.which.set_value("a")
            if self.filter.value() == "diff":
                self.filter.set_value("all")
        self._fill_list()

    def _fill_list(self):
        a, b = self.res_a, self.res_b
        self.listw.clear()
        self._rows = []
        if a is None:
            return
        byb = {(f.session, f.name): f for f in b.frames} if b else {}
        diff = {(fa.session, fa.name) for fa, _ in self.cmp.differing_frames()} \
            if self.cmp else set()
        mode = self.filter.value()
        for fa in a.frames:
            fb = byb.get((fa.session, fa.name))
            fails = fa.outcome() != "ok" or (fb is not None and fb.outcome() != "ok")
            if (mode == "fail" and not fails) or (mode == "pass" and fails) or \
                    (mode == "diff" and (fa.session, fa.name) not in diff):
                continue
            text = "%-9s" % fa.outcome() + (" | %-9s" % fb.outcome() if fb else "") + \
                "  %s/%s" % (Path(fa.session).name, fa.name)
            it = QListWidgetItem(text)
            worst = fa if fb is None or fa.outcome() != "ok" else fb
            it.setForeground(theme.qcolor(OUTCOME_TONE.get(worst.outcome(), "text")))
            self.listw.addItem(it)
            self._rows.append((fa, fb))
        if self._rows:
            self.listw.setCurrentRow(0)
        else:
            self._show_frame(-1)

    def _show_frame(self, row: int):
        if row < 0 or row >= len(self._rows):
            self.view.set_image(np.full((120, 160, 3), 40, np.uint8))
            self.view.set_overlay(None)
            self.view.set_boxes([])
            self.frame_info.setText("")
            return
        fa, fb = self._rows[row]
        f = fb if (self.which.value() == "b" and fb is not None) else fa
        self.view.set_image(f.sf.frame.rgb())
        if self.show_mask.isChecked():
            ov = np.zeros((f.mask.shape[0], f.mask.shape[1], 4), np.uint8)
            ov[f.mask.astype(bool)] = (242, 193, 78, 110)
            self.view.set_overlay(ov)
        else:
            self.view.set_overlay(None)
        T = theme.TOKENS
        self.view.set_boxes(E.boxes(f, T["ok_dot"], T["err_dot"], T["label_balloon"]))
        self.view.set_tool_text(f.name)
        lines = []
        for tag, x in (("A", fa), ("B", fb)):
            if x is None:
                continue
            s = x.score
            lines.append("%s: %s  balloons %d  TP %d FN %d FP %d split %d  blob 1 %s%s" % (
                tag, x.outcome(), s.n_balloons, s.tp, s.fn, s.fp, s.split, s.b1,
                "  conf " + " ".join("%.2f" % b.conf for b in x.blobs)
                if any(b.conf >= 0 for b in x.blobs) else ""))
        self.frame_info.setText("%s\n%s" % (Lib.rel(f.sf.stem), "\n".join(lines)))

    def report(self) -> str:
        """Everything on the results side, for pasting into notes."""
        if self.res_a is None:
            return ""
        parts = [self.res_a.text(), E.judge(self.res_a).text()]
        if self.res_b is not None:
            parts += [self.res_b.text(), E.judge(self.res_b).text(), self.cmp.text()]
        return "\n\n".join(parts)

    # ---- shell hooks -----------------------------------------------------------------------

    def on_disconnected(self):
        pass

    def save_state(self, store):
        for tag, p in (("a", self.pick_a), ("b", self.pick_b)):
            store.setValue("model_" + tag, p.set_combo.currentData() or "")
            store.setValue("stage_" + tag, p.stage() if p.stage() is not None else -1)
        store.setValue("compare", self.compare.isChecked())
        store.setValue("root", str(self.picker.root))
        store.setValue("checked", [Lib.rel(p) for p in self.picker.checked_paths()])
        store.setValue("split", self.split.saveState())
        store.setValue("rsplit", self.rsplit.saveState())

    def restore_state(self, store):
        root = store.value("root", "", type=str)
        if root and Path(root).is_dir():
            self.picker.set_root(Path(root))
        for tag, p in (("a", self.pick_a), ("b", self.pick_b)):
            m = store.value("model_" + tag, "", type=str)
            if m:
                p.select(m if m == HSV else Path(m),
                         persist.read_int(store, "stage_" + tag, 0, 3))
        on = persist.read_bool(store, "compare")
        self.compare.setChecked(bool(on))
        self.picker.set_checked([Lib.resolve(p) for p in store.value("checked", [], type=list)
                                 or []])
        for key, sp in (("split", self.split), ("rsplit", self.rsplit)):
            v = store.value(key)
            if v is not None:
                sp.restoreState(v)
        self._refresh()
