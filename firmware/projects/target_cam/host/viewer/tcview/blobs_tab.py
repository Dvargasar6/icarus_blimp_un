"""
The Blobs tab: collect and label the data stage 2's classifier (Phase B2)
will learn from.

Each item is one blob: its 24x24 crop with a label badge under it ("?",
balloon, not). Items come from two places (blobset.py explains both):
'!collect 1' records from the camera, which arrive unlabelled, and saved
labelled frames, whose blobs are labelled from the paint. Labelling is by
hand here: select one or many (shift / ctrl / drag) and press B, N or U, or
double-click to cycle one.

The agreement line is the B1 board check. The camera computes the features
and crops in flight, the PC computes them for training, and the classifier is
only valid if the two are the same numbers. When a record arrives for a frame
that was also grabbed ('g' or 'v1'), the PC re-runs the same C code on those
pixels and compares.

Layout (docs/GUI_REDESIGN.md §6.4): the dataset toolbar across the top; on
the left the check line, the labelling toolbar (filter, selection buttons,
thumbnail size) and the thumbnail grid; on the right the inspector for the
blob clicked last: its crop at x6, its record, its label and its 12 features.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QListView, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QScrollArea, QStyle,
                             QStyledItemDelegate, QVBoxLayout, QWidget)

from . import blobset as B
from . import detector as D
from . import protocol as P
from .calib_tab import DATA_DIR, _group, _key
from .ui import persist, theme
from .ui.keys import add_shortcut
from .ui.widgets import (Banner, CopyButton, FlowLayout, GripSplitter, ReadoutCard, Segment,
                         SegmentedButtons, StackedBar, StatusPill, ToggleSwitch, value_label)
from .widgets import _Slider

THUMB_MIN, THUMB_MAX, THUMB_DEFAULT = 48, 128, 72     # 72 = x3: a 24 px balloon edge is visible
BIG = 6 * P.CROP            # the inspector's preview, x6 = 144 px
LABEL_TEXT = {B.BALLOON: "balloon", B.NOT_BALLOON: "not", B.UNLABELLED: "?"}
LABEL_TOKEN = {B.BALLOON: "label_balloon", B.NOT_BALLOON: "label_not", B.UNLABELLED: ""}
CYCLE = {B.UNLABELLED: B.BALLOON, B.BALLOON: B.NOT_BALLOON, B.NOT_BALLOON: B.UNLABELLED}
FILTERS = (("all", "All"), (B.BALLOON, "Balloon"), (B.NOT_BALLOON, "Not"), (B.UNLABELLED, "?"))
INDEX_ROLE = Qt.ItemDataRole.UserRole           # the sample's index in self.samples
LABEL_ROLE = Qt.ItemDataRole.UserRole + 1       # its label, so a relabel repaints the item
HINT = ("Click selects, shift-click adds a run, ctrl-click toggles one, drag selects; B balloon, "
        "N not a balloon, U unlabelled; double-click cycles one. Live records come "
        "unlabelled; blobs from labelled frames are labelled from the paint.")
NOT_CHECKED = ("Collect, then send 'g': the frame comes with its records, "
               "and the PC recomputes them on the same pixels.")


def crop_image(s: B.Sample) -> QImage:
    rgb = np.ascontiguousarray(s.rgb())
    return QImage(rgb.data, P.CROP, P.CROP, 3 * P.CROP, QImage.Format.Format_RGB888).copy()


def crop_pixmap(s: B.Sample, size: int) -> QPixmap:
    # Nearest neighbour: the classifier sees these pixels, not smoothed ones.
    return QPixmap.fromImage(crop_image(s).scaled(size, size))


class _ThumbDelegate(QStyledItemDelegate):
    """One grid cell: the crop at the thumbnail size, nearest-neighbour, with
    a 1 px border (2 px accent when selected) and the label badge under it."""
    PAD = 6
    BADGE_H = 16

    def __init__(self, tab: "BlobsTab"):
        super().__init__(tab)
        self.tab = tab
        self.size = THUMB_DEFAULT
        self.font = theme.ui_font(11)

    def cell(self) -> QSize:
        return QSize(self.size + 2 * self.PAD, self.size + 2 * self.PAD + self.BADGE_H + 4)

    def sizeHint(self, option, index) -> QSize:
        return self.cell()

    def paint(self, p: QPainter, option, index):
        i = index.data(INDEX_ROLE)
        lab = index.data(LABEL_ROLE)
        if i is None or i >= len(self.tab._imgs):
            return
        r = option.rect
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if sel or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor("selection" if sel else "raised"))
            p.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 6, 6)
        s = self.size
        x = r.x() + (r.width() - s) // 2
        y = r.y() + self.PAD
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.drawImage(QRectF(x, y, s, s), self.tab._imgs[i])
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if sel:
            pen = theme.qcolor("accent")
            p.setPen(pen)
            p.drawRect(x - 1, y - 1, s + 1, s + 1)
            p.drawRect(x - 2, y - 2, s + 3, s + 3)
        else:
            p.setPen(theme.qcolor("border_strong"))
            p.drawRect(x - 1, y - 1, s + 1, s + 1)
        # The badge: a filled pill for a label, faint text for "?".
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(self.font)
        text = LABEL_TEXT.get(lab, "?")
        tw = QFontMetrics(self.font).horizontalAdvance(text) + 12
        badge = QRectF(r.x() + (r.width() - tw) / 2, y + s + 5, tw, self.BADGE_H)
        token = LABEL_TOKEN.get(lab, "")
        if token:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(token))
            p.drawRoundedRect(badge, self.BADGE_H / 2, self.BADGE_H / 2)
            p.setPen(theme.qcolor("on_accent"))
        else:
            p.setPen(theme.qcolor("text_muted"))
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()


class BlobsTab(QWidget):
    def __init__(self, main=None):
        super().__init__()
        self.main = main
        self.samples: list[B.Sample] = []
        self._imgs: list[QImage] = []   # each sample's crop, made once
        self.cfg: dict = {}
        self.last_frame: P.Frame | None = None
        self.agree = B.Agreement()
        self.dirty = False
        self.autosave_dir: Path | None = DATA_DIR / "blobs"
        self._live_t = []           # arrival times, for the rate
        self._shown: int | None = None  # the sample the inspector shows

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_dataset_bar())
        self.split = GripSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.split, 1)
        self.split.addWidget(self._build_grid_pane())
        self.split.addWidget(self._build_inspector())
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setCollapsible(0, False)
        self.split.setCollapsible(1, False)
        self.split.setSizes([900, 440])

        for key, lab in (("B", B.BALLOON), ("N", B.NOT_BALLOON), ("U", B.UNLABELLED)):
            add_shortcut(self, key, lambda lab=lab: self.set_label(self.selected(), lab))
        self._refresh_counts()
        self._refresh_agreement()
        self._refresh_collect()
        self._show_selected()

    # ---- building ---------------------------------------------------------------

    def _build_dataset_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Panel")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        flow = FlowLayout(bar, hspacing=16, vspacing=6)
        flow.setContentsMargins(10, 8, 10, 8)
        self.collect_btn = ToggleSwitch("Collect from camera")
        self.collect_btn.setToolTip(
            "'!collect 1': every 4th frame, and every frame sent to the PC ('g', 'v1'), "
            "the camera sends the features and a 24x24 crop of each reported blob. "
            "115200 is enough. It slows the loop by the transfer time, so "
            "rows taken while collecting are not flight timing.")
        self.collect_btn.toggled.connect(self._collect_toggled)
        self.collect_chip = StatusPill()
        flow.addWidget(_group(self.collect_btn, self.collect_chip))
        btns = []
        for text, slot, tip, role in (
                ("Add from labelled frames…", self._ask_folder,
                 "Every blob the stage 1 pipeline finds on the saved frames in a folder "
                 "(subfolders too), labelled from the painted labels", ""),
                ("Load dataset…", self._ask_load, "Append the samples of a .npz", ""),
                ("Save dataset…", self._ask_save, "All samples, labelled or not, to a .npz",
                 "primary"),
                ("Clear…", self._ask_clear, "Forget every sample", "danger")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            if role:
                theme.set_role(b, role)
            btns.append(b)
        flow.addWidget(_group(*btns))
        self.count_lbl = QLabel()
        self.count_lbl.setFont(theme.ui_font(13, QFont.Weight.DemiBold))
        self.mix_lbl = value_label()
        theme.set_prop(self.mix_lbl, "muted", True)
        self.mix_bar = StackedBar(height=8)
        self.mix_bar.setFixedWidth(120)
        self.unsaved = StatusPill("warn", "unsaved")
        self.unsaved.setToolTip("Changed since the last save. On close an unsaved session is "
                                "written to data/blobs/ rather than lost.")
        flow.addWidget(_group(self.count_lbl, self.mix_lbl, self.mix_bar, self.unsaved))
        return bar

    def _build_grid_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(8, 8, 4, 6)
        v.setSpacing(6)

        check = QHBoxLayout()
        check.setSpacing(8)
        self.agree_chip = StatusPill()
        check.addWidget(self.agree_chip, 0, Qt.AlignmentFlag.AlignTop)
        self.agree_lbl = value_label(mono=False, wrap=True)
        theme.set_prop(self.agree_lbl, "muted", True)
        check.addWidget(self.agree_lbl, 1)
        self.agree_copy = CopyButton(self.agree_text, tooltip="Copy the board = PC check")
        check.addWidget(self.agree_copy, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(check)

        tb = QWidget()
        flow = FlowLayout(tb, hspacing=16, vspacing=6)
        flow.setContentsMargins(0, 0, 0, 0)
        self.filter_seg = SegmentedButtons(FILTERS, "all")
        self.filter_seg.setToolTip("Show only the blobs with this label")
        self.filter_seg.changed.connect(lambda _k: self._apply_filter())
        flow.addWidget(_group(self.filter_seg))
        self.sel_lbl = QLabel()
        theme.set_prop(self.sel_lbl, "muted", True)
        self.label_btns = {}
        for lab, text, hint, tip in (
                (B.BALLOON, "Balloon", "B", "Label the selection balloon (B)"),
                (B.NOT_BALLOON, "Not a balloon", "N", "Label the selection not a balloon (N)"),
                (B.UNLABELLED, "Unlabel", "U", "Back to unlabelled (U)")):
            b = Segment(text, hint, LABEL_TOKEN[lab])
            b.setCheckable(False)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, lab=lab: self.set_label(self.selected(), lab))
            self.label_btns[lab] = b
        flow.addWidget(_group(self.sel_lbl, *self.label_btns.values()))
        self.size_slider = _Slider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(THUMB_MIN, THUMB_MAX)
        self.size_slider.setSingleStep(8)
        self.size_slider.setPageStep(24)
        self.size_slider.setValue(THUMB_DEFAULT)
        self.size_slider.setFixedWidth(120)
        self.size_slider.setToolTip("Thumbnail size: the 24x24 crop drawn at %d-%d px"
                                    % (THUMB_MIN, THUMB_MAX))
        self.size_lbl = value_label()
        self.size_lbl.setMinimumWidth(QFontMetrics(theme.mono_font()).horizontalAdvance("128 px"))
        self.size_slider.valueChanged.connect(self.set_thumb_size)
        flow.addWidget(_group(_key("Size"), self.size_slider, self.size_lbl))
        v.addWidget(tb)

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setMouseTracking(True)
        self.delegate = _ThumbDelegate(self)
        self.grid.setItemDelegate(self.delegate)
        self.grid.itemSelectionChanged.connect(self._show_selected)
        self.grid.itemDoubleClicked.connect(self._cycle)
        v.addWidget(self.grid, 1)

        hint = QLabel(HINT)
        hint.setWordWrap(True)
        theme.set_prop(hint, "faint", True)
        v.addWidget(hint)
        self.set_thumb_size(THUMB_DEFAULT)
        return pane

    def _build_inspector(self) -> QWidget:
        insp = QWidget()
        insp.setObjectName("Panel")
        insp.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        # The x6 preview beside the record's keys and values: narrower than
        # this, the values could not wrap enough to fit.
        insp.setMinimumWidth(380)
        v = QVBoxLayout(insp)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.title_lbl = QLabel()
        self.title_lbl.setFont(theme.ui_font(13, QFont.Weight.DemiBold))
        self.copy_all = CopyButton(self.all_text, "Copy all",
                                   tooltip="Copy the record and the feature vector")
        head = QHBoxLayout()
        head.addWidget(self.title_lbl)
        head.addStretch(1)
        head.addWidget(self.copy_all)
        head.setContentsMargins(12, 8, 8, 8)
        v.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(8, 0, 8, 8)
        bl.setSpacing(8)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        self.multi = Banner("info", "")
        self.multi.hide()
        bl.addWidget(self.multi)

        self.record_card = ReadoutCard("Record")
        self.record_card.set_copy_source(self.record_text)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.big = QLabel()
        self.big.setFixedSize(BIG + 2, BIG + 2)
        self.big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.big.setStyleSheet("border: 1px solid %s;" % theme.TOKENS["border_strong"])
        row.addWidget(self.big, 0, Qt.AlignmentFlag.AlignTop)
        kv = QGridLayout()
        kv.setHorizontalSpacing(10)
        kv.setVerticalSpacing(3)
        kv.setColumnStretch(1, 1)
        self.rec = {}
        for r, name in enumerate(("source", "blob", "stage", "fw", "wb", "centroid", "area",
                                  "box", "crop")):
            k = _key("white bal." if name == "wb" else name)
            k.setContentsMargins(0, 1, 0, 0)
            val = value_label(wrap=True)
            kv.addWidget(k, r, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            kv.addWidget(val, r, 1, Qt.AlignmentFlag.AlignTop)
            self.rec[name] = val
        kv.setRowStretch(len(self.rec), 1)
        row.addLayout(kv, 1)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        self.record_card.add_widget(holder)
        bl.addWidget(self.record_card)

        lab_row = QHBoxLayout()
        lab_row.setContentsMargins(4, 0, 0, 0)
        lab_row.setSpacing(10)
        lab_row.addWidget(_key("Label"))
        self.label_seg = SegmentedButtons(
            [(B.BALLOON, "Balloon", "B", "label_balloon"),
             (B.NOT_BALLOON, "Not", "N", "label_not"), (B.UNLABELLED, "?", "U")],
            B.UNLABELLED)
        self.label_seg.setToolTip("This blob's label; with several selected, all of them")
        # clicked, not changed: pressing the label a mixed selection's last
        # blob already has must still apply it to the others.
        for lab, b in self.label_seg.buttons.items():
            b.clicked.connect(lambda _=False, lab=lab: self.set_label(self.selected(), lab))
        lab_row.addWidget(self.label_seg)
        lab_row.addStretch(1)
        bl.addLayout(lab_row)

        self.feat_card = ReadoutCard("Feature vector", "%d inputs" % D.BLOB_FEAT_N)
        self.feat_card.set_copy_source(self.feat_text)
        self.feat_table = self.feat_card.add_table(["#", "feature", "value"])
        hh = self.feat_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.feat_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bl.addWidget(self.feat_card)
        bl.addStretch(1)
        return insp

    # ---- events from the camera ---------------------------------------------

    def on_cfg(self, v: dict):
        self.cfg = v
        self._refresh_collect()

    def on_disconnected(self):
        """The link is gone: whether the camera collects is unknown. The
        samples stay."""
        self.cfg = {}
        self._refresh_collect()

    def on_frame(self, f: P.Frame):
        self.last_frame = f

    def on_blob(self, rec: P.BlobRec):
        if rec.fv != P.FEAT_VERSION:
            # Features from another definition cannot be mixed into this set.
            self._say("# [viewer] blob record with feature version %d ignored (the viewer "
                      "has %d)" % (rec.fv, P.FEAT_VERSION))
            return
        f = self.last_frame
        if f is not None and f.t_ms == rec.t_ms:
            self.agree.add(rec, f, D.Settings.from_cfg(self.cfg))
            self._refresh_agreement()
        now = time.monotonic()
        self._live_t = [t for t in self._live_t if now - t < 10.0] + [now]
        self.add([B.from_record(rec, self._lut_id())])

    def _lut_id(self) -> int:
        """The camera's colour table now, for the record: #!BLOB does not
        carry it, and a classifier is valid behind one table only."""
        try:
            return int(self.cfg["lut_id"])
        except (KeyError, TypeError, ValueError):
            return B.UNKNOWN_LUT

    # ---- samples ------------------------------------------------------------

    def add(self, samples: list[B.Sample]):
        if not samples:
            return
        bottom = self.grid.verticalScrollBar().value() >= self.grid.verticalScrollBar().maximum()
        for s in samples:
            self.samples.append(s)
            self._imgs.append(crop_image(s))
            it = QListWidgetItem()
            it.setData(INDEX_ROLE, len(self.samples) - 1)
            it.setData(LABEL_ROLE, s.label)
            it.setToolTip("%s %s  blob %d  area %d" % (s.source, s.ref, s.k, s.area))
            self.grid.addItem(it)
        self.dirty = True
        self._apply_filter()
        if bottom:
            self.grid.scrollToBottom()
        self._refresh_counts()
        self._show_selected()

    def selected(self) -> list[int]:
        return sorted(it.data(INDEX_ROLE) for it in self.grid.selectedItems())

    def set_label(self, idx: list[int], lab: int):
        for i in idx:
            self.samples[i].label = lab
            self.grid.item(i).setData(LABEL_ROLE, lab)
        if idx:
            self.dirty = True
            self._refresh_counts()
            self._apply_filter()
            self._show_selected()

    def _cycle(self, it: QListWidgetItem):
        i = it.data(INDEX_ROLE)
        self.set_label([i], CYCLE[self.samples[i].label])

    def clear(self):
        self.samples = []
        self._imgs = []
        self.grid.clear()
        self.dirty = False
        self._refresh_counts()
        self._show_selected()

    def add_folder(self, folder: Path) -> int:
        """Blobs of the labelled frames under folder, except frames already
        in the set (adding a folder twice, or a parent, counts each once)."""
        have = {s.ref for s in self.samples if s.source == "frame"}
        new = [s for s in B.from_folder(Path(folder), stage=1) if s.ref not in have]
        self.add(new)
        return len(new)

    def save_to(self, path: Path):
        B.save(Path(path), self.samples)
        self.dirty = False
        self._refresh_counts()
        self._say("# [viewer] %d blobs saved to %s" % (len(self.samples), path))

    def load_from(self, path: Path) -> int:
        new = B.load(Path(path))
        self.add(new)
        return len(new)

    def autosave(self) -> Path | None:
        """On close: records exist only on the wire, so an unsaved session is
        written rather than lost. Nothing is asked, so closing never blocks."""
        if not self.dirty or not self.samples or self.autosave_dir is None:
            return None
        self.autosave_dir.mkdir(parents=True, exist_ok=True)
        path = self.autosave_dir / time.strftime("blobs_autosave_%Y%m%d_%H%M%S.npz")
        self.save_to(path)
        return path

    # ---- buttons --------------------------------------------------------------

    def _collect_toggled(self, on: bool):
        if self.main is not None:
            self.main.send("!collect %d" % int(on))

    def _ask_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Saved frames to take blobs from",
                                             str(DATA_DIR))
        if d:
            n = self.add_folder(Path(d))
            self._say("# [viewer] %d blobs from the labelled frames under %s" % (n, d))

    def _ask_load(self):
        p, _ = QFileDialog.getOpenFileName(self, "Load blob dataset", str(DATA_DIR),
                                           "Blob datasets (*.npz)")
        if p:
            try:
                self.load_from(Path(p))
            except (ValueError, KeyError, OSError) as e:
                QMessageBox.warning(self, "Load dataset", "%s: %s" % (p, e))

    def _ask_save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        p, _ = QFileDialog.getSaveFileName(
            self, "Save blob dataset", str(DATA_DIR / time.strftime("blobs_%Y%m%d_%H%M%S.npz")),
            "Blob datasets (*.npz)")
        if p:
            self.save_to(Path(p if p.endswith(".npz") else p + ".npz"))

    def _ask_clear(self):
        if self.dirty and QMessageBox.question(
                self, "Clear", "%d blobs, not saved since the last change. Forget them?"
                % len(self.samples)) != QMessageBox.StandardButton.Yes:
            return
        self.clear()

    # ---- grid -----------------------------------------------------------------

    # ---- remembered layout (§9) --------------------------------------------------

    def save_state(self, store):
        persist.save_splitter(store, "split", self.split)
        store.setValue("thumb", self.size_slider.value())

    def restore_state(self, store):
        persist.restore_splitter(store, "split", self.split)
        px = persist.read_int(store, "thumb", THUMB_MIN, THUMB_MAX)
        if px is not None:
            self.set_thumb_size(px)

    def set_thumb_size(self, px: int):
        self.delegate.size = int(px)
        self.grid.setGridSize(self.delegate.cell())
        self.size_lbl.setText("%d px" % px)
        if self.size_slider.value() != px:
            self.size_slider.setValue(px)
        self.grid.doItemsLayout()

    def _apply_filter(self):
        """Hide what the filter leaves out, and unselect it: B/N/U act on the
        selection, and a hidden blob must never be relabelled unseen."""
        f = self.filter_seg.value()
        self.grid.blockSignals(True)
        for r in range(self.grid.count()):
            it = self.grid.item(r)
            hide = f != "all" and it.data(LABEL_ROLE) != f
            it.setHidden(hide)
            if hide and it.isSelected():
                it.setSelected(False)
        self.grid.blockSignals(False)
        self._show_selected()

    # ---- display --------------------------------------------------------------

    def _say(self, text: str):
        if self.main is not None:
            self.main._say(text)

    def _refresh_collect(self):
        on = bool(self.cfg.get("collect", 0))
        self.collect_btn.blockSignals(True)
        self.collect_btn.setChecked(on)
        self.collect_btn.blockSignals(False)
        self.collect_btn.update()
        if "collect" not in self.cfg:
            self.collect_chip.set("neutral", "camera: unknown")
        elif on:
            self.collect_chip.set("ok", "camera: collecting, %.1f blobs/s"
                                  % (len(self._live_t) / 10.0))
        else:
            self.collect_chip.set("neutral", "camera: not collecting")

    def _refresh_counts(self):
        c = B.counts(self.samples)
        self.count_lbl.setText("%d blobs" % c["n"])
        self.mix_lbl.setText("%d balloon · %d not · %d ?" % (c["balloon"], c["not"],
                                                             c["unlabelled"]))
        self.mix_bar.set_parts([(c["balloon"], "label_balloon"), (c["not"], "label_not"),
                                (c["unlabelled"], "text_faint")])
        self.unsaved.setVisible(self.dirty)
        n = {"all": c["n"], B.BALLOON: c["balloon"], B.NOT_BALLOON: c["not"],
             B.UNLABELLED: c["unlabelled"]}
        for key, name in FILTERS:
            b = self.filter_seg.buttons[key]
            b.setText("%s %d" % (name, n[key]))
            b.updateGeometry()

    def count_text(self) -> str:
        """The old count line: '76 blobs: 0 balloon, 0 not, 76 ?  (unsaved)'."""
        c = B.counts(self.samples)
        return "%d blobs: %d balloon, %d not, %d ?%s" % (
            c["n"], c["balloon"], c["not"], c["unlabelled"], "  (unsaved)" if self.dirty else "")

    def _refresh_agreement(self):
        a = self.agree
        if not a.checked and not a.missing:
            self.agree_chip.set("neutral", "board = PC: not checked")
            self.agree_lbl.setText(NOT_CHECKED)
            return
        if a.ok():
            self.agree_chip.set("ok", "board = PC ✓")
        else:
            self.agree_chip.set("warn", "board != PC")
        self.agree_lbl.setText(a.text())

    def tick(self):
        """1 Hz, from the main window: the collection rate."""
        now = time.monotonic()
        self._live_t = [t for t in self._live_t if now - t < 10.0]
        self._refresh_collect()

    def _show_selected(self):
        idx = self.selected()
        n = len(idx)
        self.sel_lbl.setText("%d selected" % n)
        for b in self.label_btns.values():
            b.setEnabled(bool(n))
        self.label_seg.setEnabled(bool(n))
        if not idx:
            self._shown = None
            self.title_lbl.setText("No blob selected" if self.samples else "No blobs yet")
            self.multi.hide()
            self.big.clear()
            for v in self.rec.values():
                v.setText("—")
            self.feat_table.set_rows([])
            self.copy_all.setEnabled(False)
            return
        # The one clicked last (the current item), when it is selected.
        cur = self.grid.currentItem()
        i = cur.data(INDEX_ROLE) if cur is not None and cur.isSelected() else idx[-1]
        self._shown = i
        s = self.samples[i]
        self.title_lbl.setText("Blob %d of %d" % (i + 1, len(self.samples)))
        self.multi.set("info", "%d blobs selected — the label buttons apply to all of them. "
                       "Showing the last one clicked." % n)
        self.multi.setVisible(n > 1)
        self.big.setPixmap(crop_pixmap(s, BIG))
        # A frame ref is one long word; zero-width spaces let it wrap at its
        # separators (the copy text has none).
        self.rec["source"].setText("%s %s" % (s.source, s.ref.replace("/", "/\u200b")
                                               .replace("_", "_\u200b")))
        self.rec["blob"].setText("%d" % s.k)
        self.rec["stage"].setText("%d · conf %.3g" % (s.stage, s.conf))
        self.rec["fw"].setText("%s · fv %d" % (s.fw, s.fv))
        self.rec["wb"].setText(P.wb_text(s.wb, s.wb_gain))
        self.rec["centroid"].setText("%.1f, %.1f" % (s.cx, s.cy))
        self.rec["area"].setText("%d px" % s.area)
        self.rec["box"].setText("%d,%d - %d,%d" % s.box)
        self.rec["crop"].setText("x %d  y %d  side %d" % s.crop_box)
        self.label_seg.set_value(s.label, emit=False)
        rows = [(k, n_, "%.4g" % v) for k, (n_, v) in enumerate(zip(D.feat_names(), s.feat))]
        self.feat_table.set_rows(rows)
        for r in range(len(rows)):
            for c in (0, 2):
                self.feat_table.item(r, c).setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.copy_all.setEnabled(True)

    # ---- copy texts (§7) ----------------------------------------------------------

    def agree_text(self) -> str:
        """The check line: the chip, then the counts or the hint."""
        return "%s\n%s" % (self.agree_chip.text(), self.agree_lbl.text())

    def record_text(self) -> str:
        """The old side panel's record block, line for line, plus the white
        balance (firmware with fixed gains records it)."""
        if self._shown is None:
            return ""
        s = self.samples[self._shown]
        return "\n".join(["%s  %s  blob %d" % (s.source, s.ref, s.k),
                          "label     %s" % LABEL_TEXT[s.label],
                          "stage %d   conf %.3g   fw %s   fv %d" % (s.stage, s.conf, s.fw, s.fv),
                          "white bal. %s" % P.wb_text(s.wb, s.wb_gain),
                          "table id  %s" % (s.lut_id if s.lut_id >= 0 else "not recorded"),
                          "centroid  %.1f, %.1f   area %d" % (s.cx, s.cy, s.area),
                          "box       %d,%d - %d,%d" % s.box,
                          "crop      x %d  y %d  side %d" % s.crop_box])

    def feat_text(self) -> str:
        """CSV: the feature names, then this blob's values."""
        if self._shown is None:
            return ""
        s = self.samples[self._shown]
        return ",".join(D.feat_names()) + "\n" + ",".join("%.4g" % v for v in s.feat)

    def all_text(self) -> str:
        return "\n\n".join(t for t in (self.record_text(), self.feat_text()) if t)
