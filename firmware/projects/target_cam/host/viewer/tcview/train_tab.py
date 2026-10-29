"""
Train: pick data folders and a stage, name the result, train.

Stage 1 trains a colour table (host/train/train_lut.py), stage 2 a blob
classifier behind a table (train_clf.py), stage 3 the crop CNN behind a table
and its classifier (train_cnn.py). Stage 0 is not here: it is calibrated by hand on the Image &
calibration tab. Every run writes a new model set, models/<name>/
(library.py): for stage 2 the parent set's table is copied in first and the
classifier is written beside it (for stage 3 the table and the classifier,
and the CNN beside them), so the parent is never changed and the
result can be installed whole. The firmware's set (main/models) is never
written here; that is the Firmware tab's job.

The trainer runs as a child process with its output shown live and saved in
the set (train_log.txt, clf_train_log.txt), and the command is shown so it can
be run by hand. What the trainer found is read back from the .json it wrote,
not from its text, and turned into the banner: whether the chosen setting put
a false blob 1 on a held-out session (do not fly), lost balloons there, or
could not be judged at all because the parameters were fixed.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
                             QSpinBox,
                             QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
                             QScrollArea, QVBoxLayout, QWidget)

from . import jobs as J
from . import library as Lib
from .data_picker import DataPicker
from .ui import persist, theme
from .ui.widgets import (Banner, CollapsibleSection, CopyButton, GripSplitter,
                         SegmentedButtons, value_label)

STAGES = ((1, "1 · colour table", "", ""), (2, "2 · blob classifier", "", ""),
          (3, "3 · crop CNN", "", ""))
LOG_NAME = {1: "train_log.txt", 2: "clf_train_log.txt", 3: "cnn_train_log.txt"}
# The trainers' own fallbacks when they cannot search (README: "--sigma 8 --k 7").
LUT_FIXED = dict(sigma=8.0, k=7.0, min_evidence=0.5)
CLF_FIXED = dict(l2=1.0, threshold=0.5)


# ---- what a finished run says ------------------------------------------------------

def judge_lut(meta: dict) -> tuple[str, str]:
    """Banner (tone, text) from a lut_model.json."""
    head = "Colour table id %s: %s colours, sigma %g, k %g." % (
        meta.get("train_id", "?"), meta.get("colours", "?"), meta.get("sigma", 0),
        meta.get("k", 0))
    rows = meta.get("search") or []
    if not rows:
        return "info", (head + " Parameters were fixed, so there are no held-out figures: "
                        "verify it on folders it did not see.")
    best = rows[0]
    folds = best.get("folds") or []
    if best.get("false_b1"):
        bad = ", ".join("%s %d/%d" % (f["held_out"], f["false_b1"], f["empty_frames"])
                        for f in folds if f.get("false_b1"))
        return "err", (head + " Every setting puts a false blob 1 on held-out frames without "
                       "a balloon (%s). Do not fly this table: check those frames' labels, "
                       "or add sessions." % bad)
    fn = sum(f.get("fn", 0) for f in folds)
    tone = "ok" if fn == 0 else "warn"
    return tone, (head + " No false blob 1 on the held-out sessions; %d balloon(s) lost "
                  "there. Pixel F1 %.1f %% (held out)." % (fn, 100 * best.get("px_f1", 0)))


def judge_clf(meta: dict) -> tuple[str, str]:
    """Banner (tone, text) from a clf_model.json."""
    head = "Classifier id %s behind table %s: threshold %.2f, l2 %g." % (
        meta.get("train_id", "?"), meta.get("lut_id", "?"), meta.get("threshold", 0),
        meta.get("l2", 0))
    rows, base = meta.get("search") or [], meta.get("stage1_held_out")
    warns = list(meta.get("warnings") or [])
    data = list(meta.get("data_warnings") or [])
    if not rows or not base:
        tone, text = "info", head + " Parameters were fixed: no held-out figures."
    else:
        b = rows[0]
        text = head + (" Held out: false blob 1 %d → %d, wrong blob 1 %d → %d, balloons lost "
                       "%d → %d (stage 1 alone → with the classifier)."
                       % (base["false_b1"], b["false_b1"], base["wrong_b1"], b["wrong_b1"],
                          base["fn"], b["fn"]))
        excluded = b.get("excluded") or any(w.startswith("every setting loses") for w in warns)
        tone = "err" if excluded else ("warn" if warns else "ok")
    if warns:
        text += " WARNING: " + " ".join(warns)
    if data:
        text += " Data: " + " ".join(data)
    return tone, text


def judge_cnn(meta: dict) -> tuple[str, str]:
    """Banner (tone, text) from a cnn_model.json."""
    iv = meta.get("int_vs_float") or {}
    head = ("CNN id %s behind table %s: %sx%s input, %s multiply-adds per crop, threshold "
            "%.2f; the integer network decides %d training crop(s) differently from the "
            "float one." % (meta.get("train_id", "?"), meta.get("lut_id", "?"),
                             meta.get("n", "?"), meta.get("n", "?"), meta.get("macs", "?"),
                             meta.get("threshold", 0), iv.get("flips", 0)))
    rows, base = meta.get("search") or [], meta.get("base_held_out")
    warns = list(meta.get("warnings") or [])
    if not rows or not base:
        tone, text = "info", head + " Threshold fixed: no held-out figures."
    else:
        b = rows[0]
        text = head + (" Held out: false blob 1 %d → %d, wrong blob 1 %d → %d, balloons lost "
                       "%d → %d (%s alone → with the CNN)." % (
                           base["false_b1"], b["false_b1"], base["wrong_b1"], b["wrong_b1"],
                           base["fn"], b["fn"], meta.get("base", "stage 2")))
        tone = "err" if b.get("excluded") else ("warn" if warns else "ok")
    if warns:
        text += " WARNING: " + " ".join(warns)
    if meta.get("data_warnings"):
        text += " Data: " + " ".join(meta["data_warnings"])
    return tone, text


def _spin(lo, hi, step, value, decimals=2) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    s.setValue(value)
    s.setKeyboardTracking(False)
    return s


class TrainTab(QWidget):
    sets_changed = pyqtSignal()         # a set was written: pickers elsewhere reload
    verify_requested = pyqtSignal(object)   # Path of the new set

    def __init__(self, main=None, data_dir: Path = Lib.DATA_DIR,
                 models_dir: Path = Lib.MODELS_DIR, firmware_dir: Path = Lib.FIRMWARE_MODELS):
        super().__init__()
        self.main = main
        self.models_dir, self.firmware_dir = Path(models_dir), Path(firmware_dir)
        self.job = J.ProcessJob(self)
        self.job.output.connect(self._output)
        self.job.finished.connect(self._finished)
        self.out_dir: Path | None = None
        self.last_set: Path | None = None
        self._stage = 1

        self.split = GripSplitter(Qt.Orientation.Horizontal)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.split)

        # ---- left: the data ------------------------------------------------------------
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(12, 12, 8, 12)
        cap = QLabel("TRAINING DATA")
        cap.setFont(theme.caption_font())
        ll.addWidget(cap)
        self.picker = DataPicker(data_dir)
        self.picker.changed.connect(self._refresh)
        ll.addWidget(self.picker, 1)
        self.split.addWidget(left)

        # ---- right: stage, parent, parameters, output, run ----------------------------
        form = QWidget()
        fl = QVBoxLayout(form)
        fl.setContentsMargins(8, 12, 12, 12)
        fl.setSpacing(10)

        sec = CollapsibleSection("Stage")
        self.stage = SegmentedButtons(STAGES, 1)
        self.stage.changed.connect(self._stage_changed)
        sec.add_widget(self.stage)
        self.stage_hint = value_label("", mono=False, wrap=True)
        theme.set_prop(self.stage_hint, "muted", True)
        sec.add_widget(self.stage_hint)
        fl.addWidget(sec)

        self.parent_sec = CollapsibleSection("Parent model set")
        self.parent_combo = QComboBox()
        self.parent_combo.currentIndexChanged.connect(self._refresh)
        self.parent_sec.add_widget(self.parent_combo)
        self.parent_info = value_label("", wrap=True)
        self.parent_sec.add_widget(self.parent_info)
        fl.addWidget(self.parent_sec)

        sec = CollapsibleSection("Parameters")
        self.auto = QRadioButton("Choose them by leaving one session out (needs 2+ sessions)")
        self.fixed = QRadioButton("Fixed")
        self.auto.setChecked(True)
        self.auto.toggled.connect(self._refresh)
        sec.add_widget(self.auto)
        sec.add_widget(self.fixed)
        grid = QGridLayout()
        self.sigma = _spin(0, 16, 1, LUT_FIXED["sigma"], 1)
        self.k = _spin(1, 100, 1, LUT_FIXED["k"], 1)
        self.min_ev = _spin(0.1, 5, 0.5, LUT_FIXED["min_evidence"], 1)
        self.l2 = _spin(0.001, 100, 0.5, CLF_FIXED["l2"], 3)
        self.threshold = _spin(0.05, 0.95, 0.05, CLF_FIXED["threshold"])
        self.cnn_threshold = _spin(0.05, 0.95, 0.05, 0.5)
        self.cnn_size = QComboBox()
        self.cnn_size.addItem("16 × 16 (about 166 k mult-adds)", 16)
        self.cnn_size.addItem("24 × 24 (about 373 k mult-adds)", 24)
        self.cnn_epochs = QSpinBox()
        self.cnn_epochs.setRange(1, 500)
        self.cnn_epochs.setValue(40)
        self.cnn_k = QSpinBox()
        self.cnn_k.setRange(1, 4)
        self.cnn_k.setValue(2)
        self.cnn_k.setToolTip("Blobs the CNN checks per frame, the largest stage 2 kept "
                              "(the camera's !cnnk)")
        self.max_lost = _spin(0, 1, 0.05, 0.10)
        self.max_lost.setToolTip("Exclude classifier settings that lose more than this "
                                 "fraction of the balloons stage 1 found (held out)")
        self._param_rows = {}
        for r, (key, lbl, w, stage) in enumerate((
                ("sigma", "sigma", self.sigma, 1), ("k", "k", self.k, 1),
                ("min_ev", "min evidence", self.min_ev, 1), ("l2", "L2 penalty", self.l2, 2),
                ("threshold", "threshold", self.threshold, 2),
                ("cnn_size", "input size", self.cnn_size, 3),
                ("cnn_epochs", "epochs", self.cnn_epochs, 3),
                ("cnn_k", "blobs checked (cnn_k)", self.cnn_k, 3),
                ("cnn_threshold", "threshold", self.cnn_threshold, 3),
                ("max_lost", "max balloons lost", self.max_lost, (2, 3)))):
            lab = QLabel(lbl)
            grid.addWidget(lab, r, 0)
            grid.addWidget(w, r, 1)
            self._param_rows[key] = (lab, w, stage)
            (w.currentIndexChanged if isinstance(w, QComboBox) else w.valueChanged).connect(
                self._refresh)
        sec.add_layout(grid)
        fl.addWidget(sec)

        sec = CollapsibleSection("Output")
        row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("model set name")
        self.name.textChanged.connect(self._refresh)
        self.suggest_btn = QPushButton("Suggest")
        self.suggest_btn.clicked.connect(lambda: self.name.setText(self._suggested()))
        row.addWidget(QLabel("models/"))
        row.addWidget(self.name, 1)
        row.addWidget(self.suggest_btn)
        sec.add_layout(row)
        self.name_err = value_label("", mono=False, wrap=True)
        theme.set_tone(self.name_err, "warn")
        sec.add_widget(self.name_err)
        row = QHBoxLayout()
        self.cmd_lbl = value_label("", wrap=True)
        row.addWidget(self.cmd_lbl, 1)
        row.addWidget(CopyButton(lambda: self.cmd_lbl.text(), "Copy command"))
        sec.add_layout(row)
        fl.addWidget(sec)

        row = QHBoxLayout()
        self.train_btn = QPushButton("Train")
        theme.set_role(self.train_btn, "primary")
        self.train_btn.clicked.connect(self.train)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.job.stop)
        self.verify_btn = QPushButton("Verify this set →")
        self.verify_btn.clicked.connect(lambda: self.verify_requested.emit(self.last_set))
        row.addWidget(self.train_btn)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        row.addWidget(self.verify_btn)
        fl.addLayout(row)
        self.why = value_label("", mono=False, wrap=True)
        fl.addWidget(self.why)
        self.banner = Banner("neutral", "No training run yet.")
        fl.addWidget(self.banner)

        sec = CollapsibleSection("Trainer output")
        hdr = QHBoxLayout()
        hdr.addStretch(1)
        hdr.addWidget(CopyButton(lambda: self.log.toPlainText(), "Copy"))
        sec.add_layout(hdr)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(theme.mono_font())
        self.log.setMaximumBlockCount(20000)
        self.log.setMinimumHeight(160)
        sec.add_widget(self.log, 1)
        fl.addWidget(sec, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(form)
        self.split.addWidget(scroll)
        # The tree's white-balance column matters (mixed gains), so the data
        # side starts wider.
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)

        self.reload_sets()
        self._stage_changed(1)

    # ---- model sets ----------------------------------------------------------------------

    def reload_sets(self):
        keep = self.parent_combo.currentData()
        self.parent_combo.blockSignals(True)
        self.parent_combo.clear()
        need = self._stage - 1
        for s in Lib.find_sets(self.models_dir, self.firmware_dir):
            if s.top_stage >= max(need, 1):
                self.parent_combo.addItem("%s  (stages %s)" % (
                    s.name, ", ".join(map(str, s.stages))), str(s.path))
        i = self.parent_combo.findData(keep)
        self.parent_combo.setCurrentIndex(max(i, 0))
        self.parent_combo.blockSignals(False)
        self._refresh()

    def parent_set(self) -> Lib.ModelSet | None:
        p = self.parent_combo.currentData()
        if not p:
            return None
        return Lib.load_set(Path(p), firmware=Path(p).resolve() == self.firmware_dir.resolve())

    # ---- state ---------------------------------------------------------------------------

    def _stage_changed(self, stage):
        self._stage = int(stage)
        self.picker.set_blobsets(self._stage >= 2)
        self.parent_sec.setVisible(self._stage >= 2)
        for key, (lab, w, st) in self._param_rows.items():
            on = self._stage in st if isinstance(st, tuple) else st == self._stage
            if key in ("cnn_size", "cnn_epochs", "cnn_k"):
                on = self._stage == 3       # used with Auto as well as Fixed
            lab.setVisible(on)
            w.setVisible(on)
        self.stage_hint.setText({
            1: "A colour table from the pixel labels: which RGB565 colours are balloon. "
               "Stage 0 (HSV) is calibrated by hand on Image & calibration.",
            2: "A blob classifier behind the parent's colour table, from the blobs that "
               "table finds in the labelled frames (or from blob sets collected at stage 1 "
               "with that table). The new set holds a copy of the table plus the classifier.",
            3: "A crop CNN behind the parent's table and classifier, from the crops of every "
               "blob the table finds (not only the ones the classifier keeps). The threshold "
               "is chosen as stage 3 runs in flight: the classifier first, then the CNN on "
               "the largest cnn_k blobs. The new set holds copies of the table and the "
               "classifier plus the CNN. Training takes minutes, not seconds."}[self._stage])
        self.reload_sets()

    def _suggested(self) -> str:
        return Lib.suggest_name(self._stage, self.picker.sessions(), self.parent_set(),
                                self.models_dir)

    def _params(self) -> dict:
        if self.auto.isChecked():
            return {}
        if self._stage == 1:
            return dict(sigma=self.sigma.value(), k=self.k.value(),
                        min_evidence=self.min_ev.value())
        if self._stage == 3:
            return dict(threshold=self.cnn_threshold.value())
        return dict(l2=self.l2.value(), threshold=self.threshold.value())

    def command(self, out: Path) -> list:
        ss = self.picker.sessions()
        if self._stage == 1:
            return J.lut_command(out, ss, **self._params())
        if self._stage == 3:
            return J.cnn_command(out, ss, max_lost=self.max_lost.value(),
                                 size=self.cnn_size.currentData(),
                                 epochs=self.cnn_epochs.value(), cnn_k=self.cnn_k.value(),
                                 **self._params())
        return J.clf_command(out, ss, max_lost=self.max_lost.value(), **self._params())

    def problems(self) -> list[str]:
        """Why Train is disabled, or []."""
        out = []
        ss = self.picker.sessions()
        if self.job.running():
            out.append("A training run is in progress.")
        if not ss:
            out.append("Check at least one data folder.")
        if self._stage == 1 and any(s.is_blobset for s in ss):
            out.append("A colour table trains on frames, not blob sets.")
        if self.auto.isChecked() and len(ss) < 2:
            out.append("Choosing the parameters needs two or more sessions: check another "
                       "folder, or use Fixed.")
        if self._stage >= 2:
            p = self.parent_set()
            if p is None:
                out.append("Choose the parent model set.")
            elif self._stage - 1 not in p.stages:
                out.append("%s cannot run stage %d." % (p.name, self._stage - 1))
        err = Lib.check_name(self.name.text().strip() or self._suggested(), self.models_dir)
        if err:
            out.append(err)
        return out

    def _refresh(self, *_):
        sug = self._suggested()
        self.name.setPlaceholderText(sug)
        name = self.name.text().strip()
        err = Lib.check_name(name, self.models_dir) if name else ""
        self.name_err.setText(err)
        self.name_err.setVisible(bool(err))
        p = self.parent_set()
        self.parent_info.setText(
            "" if p is None else "\n".join("%s: %s" % (k, p.summary(k))
                                           for k in Lib.KINDS if p.has(k)))
        out = self.models_dir / (name or sug)
        self.cmd_lbl.setText(J.pretty(self.command(out)) if self.picker.checked_paths() else "")
        probs = self.problems()
        running = self.job.running()
        self.train_btn.setEnabled(not probs)
        self.stop_btn.setEnabled(running)
        self.verify_btn.setEnabled(self.last_set is not None and not running)
        self.why.setText("" if running else " ".join(probs))
        self.why.setVisible(bool(probs) and not running)

    # ---- run -----------------------------------------------------------------------------

    def train(self) -> bool:
        if self.problems():
            return False
        name = self.name.text().strip() or self._suggested()
        parent = self.parent_set() if self._stage >= 2 else None
        try:
            self.out_dir = Lib.new_set(name, parent, {1: None, 2: "lut", 3: "clf"}[self._stage],
                                       self.models_dir)
        except (OSError, ValueError) as e:
            self.banner.set("err", "Cannot create models/%s: %s" % (name, e))
            return False
        self._run_stage = self._stage
        cmd = self.command(self.out_dir)
        self.log.setPlainText("$ %s\n" % J.pretty(cmd))
        self.banner.set("info", "Training into models/%s …" % name)
        self.last_set = None
        self.job.start(cmd)
        self._refresh()
        return True

    def _output(self, text: str):
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()

    def _finished(self, code: int, text: str):
        out, stage = self.out_dir, self._run_stage
        self.out_dir = None
        kind = Lib.KINDS[stage - 1]
        written = out is not None and (out / ("%s_model.bin" % kind)).exists()
        if code != 0 or not written:
            # Nothing usable: do not leave a half set (a copied table with no
            # classifier would look like a table someone trained here).
            if out is not None and out.is_relative_to(self.models_dir):
                shutil.rmtree(out, ignore_errors=True)
            self.banner.set("err", "The trainer stopped (exit code %d) and wrote nothing. "
                            "Its last lines are below." % code)
        else:
            (out / LOG_NAME[stage]).write_text(self.log.toPlainText())
            try:
                meta = json.loads((out / ("%s_model.json" % kind)).read_text())
            except (OSError, ValueError):
                meta = {}
            tone, msg = {1: judge_lut, 2: judge_clf, 3: judge_cnn}[stage](meta)
            self.banner.set(tone, "models/%s: %s" % (out.name, msg))
            self.last_set = out
            self.name.clear()
            self.sets_changed.emit()
        self.reload_sets()
        self.picker._refresh_summary()

    # ---- shell hooks -----------------------------------------------------------------------

    def on_disconnected(self):
        pass                            # training needs no camera

    def save_state(self, store):
        store.setValue("stage", self._stage)
        store.setValue("parent", self.parent_combo.currentData() or "")
        store.setValue("root", str(self.picker.root))
        store.setValue("checked", [Lib.rel(p) for p in self.picker.checked_paths()])
        store.setValue("split", self.split.saveState())

    def restore_state(self, store):
        root = store.value("root", "", type=str)
        if root and Path(root).is_dir():
            self.picker.set_root(Path(root))
        st = persist.read_int(store, "stage", 1, 3)
        if st:
            self.stage.set_value(st)
            self._stage_changed(st)
        par = store.value("parent", "", type=str)
        i = self.parent_combo.findData(par)
        if i >= 0:
            self.parent_combo.setCurrentIndex(i)
        checked = store.value("checked", [], type=list) or []
        self.picker.set_checked([Lib.resolve(p) for p in checked])
        sp = store.value("split")
        if sp is not None:
            self.split.restoreState(sp)
