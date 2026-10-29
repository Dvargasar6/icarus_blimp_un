"""
Label frames: paint which pixels are balloon and which are background.

Works on saved frames (Calib tab, Save frame), with or without a camera: open
a folder, pick a frame, paint, and the labels are written next to it as
<stem>.labels.png (format in labels.py). They are saved when you move to
another frame, and with Save (Ctrl+S).

The quick way through a frame:
  1. Ellipse tool, Balloon class: drag a box around the balloon. A balloon is
     round, so this covers most of it in one stroke; fix the edge with the
     brush (Erase takes pixels back to unlabelled).
  2. "Rest -> background": every pixel not yet labelled becomes background,
     except a band around the balloon, which stays unlabelled because the rim
     is a mix of both colours.
  3. Check the rim band and any second balloon, then move on (PgDown).
  A frame with no balloon is labelled by "Rest -> background" alone.

The reflections on the mylar are part of the balloon: label them balloon.
Whether the colour table should match a white highlight is the training's
decision (it counts how often that colour is balloon versus background), not
the label's.

Right mouse button erases with the current tool. Keys (while this tab is
shown, and not typing in a text field): 1 balloon, 2 background, 0 erase,
B brush, R rectangle, E ellipse, Ctrl+Z undo, Ctrl+S save, PgDown / PgUp or
Right / Left next / previous frame.

Layout (docs/GUI_REDESIGN.md §6.3): the image gets every pixel it can, since
labelling needs zoom. The image pane is the toolbar, the PixelImageView in
paint mode and the Label stats strip; the Labelling inspector on the right
holds the frame list and the paint and display settings. Painting goes
through the view's one widget-to-image transform, so a stroke lands on the
pixel under the cursor at every zoom and scroll position.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QPushButton, QScrollArea, QStyle,
                             QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout,
                             QWidget)

from . import detector as D
from . import labels as L
from .calib_tab import BOX_COLOURS, DATA_DIR, STAGE_NAMES, _key, _row
from .ui import persist, theme
from .ui.keys import HintButton, add_shortcut
from .ui.widgets import (Bar, CollapsibleSection, ElidedLabel, GripSplitter, PixelImageView,
                         ReadoutCard, ReadoutStrip, SegmentedButtons, StackedBar,
                         StatusPill, StripSplitter, value_label)
from .widgets import _Slider, _SpinBox

# Label colours over the image: the theme's class colours. Neither is red or
# green, so a balloon of either colour stays readable under its tint, and
# they differ in lightness as well as hue (§3.1).
TINT = {L.BALLOON: "label_balloon", L.BACKGROUND: "label_not"}
CLASS_NAME = {L.UNLABELLED: "unlabelled", L.BALLOON: "balloon", L.BACKGROUND: "background"}
TOOLS = ("brush", "rect", "ellipse")        # _tool() is the index, as the old radio ids were
TOOL_NAME = {"brush": "brush", "rect": "rectangle", "ellipse": "ellipse"}
UNDO_DEPTH = 50
NO_HOVER = "hover over the image"
NO_FRAME = "no frame: open a folder of saved frames"
DONE_ROLE = Qt.ItemDataRole.UserRole + 1    # the frame has a labels file
SHOWN_BALLOONS = 4                          # balloons listed by position, as before


def _pct(x: float) -> str:
    return "-" if math.isnan(x) else "%.1f" % (100 * x)


class _FrameDelegate(QStyledItemDelegate):
    """A frame-list row: a green ✓ when the frame has labels, the subfolder
    in faint text and the file name in normal text. The name matters most,
    so when the row is too narrow the subfolder gives way first."""

    def paint(self, p, option, index):
        o = QStyleOptionViewItem(option)
        self.initStyleOption(o, index)
        text, o.text = o.text, ""
        style = o.widget.style() if o.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, o, p, o.widget)
        r = o.rect.adjusted(6, 0, -6, 0)
        fm = QFontMetrics(o.font)
        p.save()
        p.setFont(o.font)
        mid = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        if index.data(DONE_ROLE):
            p.setPen(theme.qcolor("ok_dot"))
            p.drawText(r, mid, "✓")
        r.setLeft(r.left() + fm.horizontalAdvance("✓") + 6)
        head, sep, name = text.rpartition("/")
        prefix = head + sep
        name_w = fm.horizontalAdvance(name)
        if prefix:
            room = max(0, r.width() - name_w)
            shown = fm.elidedText(prefix, Qt.TextElideMode.ElideLeft, room)
            p.setPen(theme.qcolor("text_faint"))
            p.drawText(r, mid, shown)
            r.setLeft(r.left() + fm.horizontalAdvance(shown))
        p.setPen(theme.qcolor("text"))
        p.drawText(r, mid, fm.elidedText(name, Qt.TextElideMode.ElideRight, r.width()))
        p.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), 24))


class _PathLine(QLineEdit):
    """The folder path, read-only and selectable. A long path shows its end,
    which names the folder; the whole path is in the tooltip."""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setObjectName("LastLine")
        theme.mono(self)

    def set_path(self, text: str):
        self.setText(text)
        self.setToolTip(text)
        self.setCursorPosition(len(text))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not self.hasSelectedText():
            self.setCursorPosition(len(self.text()))


class _CountTile(QFrame):
    """One count of the detector-vs-labels score, big, over its name."""

    def __init__(self, name: str, tip: str):
        super().__init__()
        self.setObjectName("CountTile")
        self.setToolTip(tip)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 5)
        lay.setSpacing(0)
        self.value = value_label("-")
        f = theme.mono_font(17)
        f.setWeight(QFont.Weight.DemiBold)
        self.value.setFont(f)
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.value)
        cap = QLabel(name)
        cap.setFont(theme.ui_font(11))
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.set_prop(cap, "muted", True)
        lay.addWidget(cap)
        self._tone = None
        self.set(None, "neutral")

    def set(self, n, tone: str = "neutral"):
        self.value.setText("-" if n is None else "%d" % n)
        if tone != self._tone:
            self._tone = tone
            c = theme.tone_colours(tone)
            self.setStyleSheet(
                "QFrame#CountTile {{ background: {bg}; border: 1px solid {border};"
                " border-radius: 6px; }} QFrame#CountTile QLabel#v {{ color: {text}; }}"
                .format(**c))
            self.value.setObjectName("v")
            theme.repolish(self.value)


class LabelTab(QWidget):
    def __init__(self, folder: Path | None = None):
        super().__init__()
        self.folder = Path(folder or DATA_DIR)
        self.stems: list[Path] = []
        self.sf: L.SavedFrame | None = None
        self.lab = np.zeros((120, 160), dtype=np.uint8)
        self.rgb = np.zeros((120, 160, 3), dtype=np.uint8)
        self.dirty = False
        self.undo: list[np.ndarray] = []
        self._drag0 = None
        self._last = None
        self._det = None                # detector result and mask for this frame
        self._mask = None
        self._stage_eff = 0
        self._score = None              # L.score of this frame, for the cards and summary
        self._det_boxes: list = []      # what the view draws: the detector's boxes,
        self._bal_boxes: list = []      # then the labelled balloons' (dotted)
        self._hover = None              # (x, y) image pixel under the cursor
        self._error = ""                # why the last frame failed to load

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.split = GripSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.split)
        self.split.addWidget(self._build_image_pane())
        self.split.addWidget(self._build_inspector())
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setCollapsible(0, False)
        self.split.setCollapsible(1, False)
        self.split.setSizes([900, 400])

        for keys, fn in (("1", lambda: self.cls_seg.set_value(L.BALLOON)),
                         ("2", lambda: self.cls_seg.set_value(L.BACKGROUND)),
                         ("0", lambda: self.cls_seg.set_value(L.UNLABELLED)),
                         ("B", lambda: self.tool_seg.set_value("brush")),
                         ("R", lambda: self.tool_seg.set_value("rect")),
                         ("E", lambda: self.tool_seg.set_value("ellipse")),
                         ("Ctrl+Z", self.do_undo), ("Ctrl+S", self.save),
                         ("PgDown", lambda: self.step(1)), ("PgUp", lambda: self.step(-1)),
                         ("Right", lambda: self.step(1)), ("Left", lambda: self.step(-1))):
            add_shortcut(self, keys, fn)

        self._paint_changed()
        self._show_changed()
        self._draw_labels()
        self.rescan()

    # ---- building ---------------------------------------------------------------

    def _build_image_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(8, 8, 4, 0)
        v.setSpacing(6)

        tb = QHBoxLayout()
        tb.setSpacing(8)
        self.prev_btn = QPushButton("◀")
        self.prev_btn.setToolTip("Previous frame (← or PgUp)")
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn = QPushButton("▶")
        self.next_btn.setToolTip("Next frame (→ or PgDown)")
        self.next_btn.clicked.connect(lambda: self.step(1))
        for b in (self.prev_btn, self.next_btn):
            b.setFixedWidth(34)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.name_lbl = ElidedLabel("", "text", mono=True)
        self.where_lbl = ElidedLabel("", "text_faint")
        self.where_lbl.setFont(theme.ui_font(12))
        names.addWidget(self.name_lbl)
        names.addWidget(self.where_lbl)
        name_box = QWidget()
        name_box.setLayout(names)
        names.setContentsMargins(4, 0, 4, 0)
        name_box.setMinimumWidth(120)
        self.pill = StatusPill("neutral", "no frame")
        self.zoom_seg = SegmentedButtons([("fit", "Fit"), ("2", "2×"), ("3", "3×"), ("4", "4×")])
        self.zoom_seg.setToolTip("Zoom (also Ctrl+wheel). Pan with the scroll bars, "
                                 "middle-drag or Space+drag.")
        tb.addWidget(self.prev_btn)
        tb.addWidget(name_box, 1)
        tb.addWidget(self.next_btn)
        tb.addSpacing(8)
        tb.addWidget(self.pill)
        tb.addSpacing(8)
        tb.addWidget(self.zoom_seg)
        v.addLayout(tb)

        self.view = PixelImageView(160, 120)
        self.view.paint_mode = True
        self.view.hovered.connect(self._hover_at)
        self.view.left.connect(lambda: self._hover_at(None, None))
        self.view.paintStroke.connect(self._stroke)
        self.zoom_seg.changed.connect(self.view.set_zoom)
        self.view.zoomChanged.connect(lambda k: self.zoom_seg.set_value(k, emit=False))

        self.strip = self._build_stats()
        self.vsplit = StripSplitter(self.view, self.strip, max_share=0.35)
        self.strip_scroll = self.vsplit.strip_scroll
        v.addWidget(self.vsplit, 1)
        return pane

    def _build_stats(self) -> ReadoutStrip:
        strip = ReadoutStrip("Label stats")

        c = self.pixel_card = ReadoutCard("Pixel")
        self.swatch = QLabel()
        self.swatch.setFixedSize(18, 18)        # a colour sample, not a panel: §2 allows it
        self.px_xy = value_label()
        c.body.addLayout(_row(self.swatch, self.px_xy))
        self.px_rgb = c.add_kv("RGB")
        self.px_cls = c.add_kv("label")
        c.set_copy_source(self.hover_text)
        strip.add_card(c)

        c = self.labels_card = ReadoutCard("Labels", "160 × 120 = 19200 px")
        c.subtitle_lbl.setMinimumWidth(c.subtitle_lbl.sizeHint().width())
        self.share_bar = c.add_widget(StackedBar())
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(3)
        self.cls_rows = {}
        for r, v in enumerate((L.BALLOON, L.BACKGROUND, L.UNLABELLED)):
            sw = QLabel()
            sw.setFixedSize(10, 10)             # a colour key, not a panel
            sw.setStyleSheet("background: %s; border-radius: 2px;" % theme.TOKENS[
                TINT.get(v, "border_strong")])
            g.addWidget(sw, r, 0, Qt.AlignmentFlag.AlignVCenter)
            g.addWidget(_key(CLASS_NAME[v]), r, 1)
            px, pc = value_label(), value_label()
            px.setAlignment(Qt.AlignmentFlag.AlignRight)
            pc.setAlignment(Qt.AlignmentFlag.AlignRight)
            g.addWidget(px, r, 2)
            g.addWidget(pc, r, 3)
            self.cls_rows[v] = (px, pc)
        g.setColumnStretch(4, 1)
        c.body.addLayout(g)
        self.n_balloons = c.add_kv("balloons (labelled objects)")
        self.balloon_list = c.add_text("")
        self.sum_note = c.add_text("", mono=False)
        theme.set_prop(self.sum_note, "faint", True)
        c.set_copy_source(self.sum_text)
        strip.add_card(c)

        c = self.det_card = ReadoutCard("Detector vs labels")
        self.det_stage = value_label()
        self.det_test = value_label()
        self.det_blobs = value_label()
        for w in (self.det_stage, self.det_test, self.det_blobs):
            c.add_widget(w)
        tiles = QHBoxLayout()
        tiles.setSpacing(6)
        self.tiles = {}
        for k, tip in (("TP", "balloons found: a reported blob's centroid in the balloon's box"),
                       ("FN", "balloons no reported blob landed in"),
                       ("FP", "reported blobs in no balloon's box"),
                       ("split", "extra blobs in an already-found balloon")):
            self.tiles[k] = _CountTile(k, tip)
            tiles.addWidget(self.tiles[k])
        tiles.addStretch(1)
        c.body.addLayout(tiles)
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(4)
        self.pr = {}
        for r, (k, tip) in enumerate((("precision", "of the matched pixels, how many you "
                                       "painted balloon"),
                                      ("recall", "of your balloon pixels, how many matched"))):
            g.addWidget(_key(k, tip), r, 0)
            bar = Bar(0.0, "label_balloon")
            g.addWidget(bar, r, 1, Qt.AlignmentFlag.AlignVCenter)
            val = value_label()
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            val.setMinimumWidth(QFontMetrics(theme.mono_font()).horizontalAdvance("100.0 %"))
            g.addWidget(val, r, 2)
            self.pr[k] = (bar, val)
        g.setColumnStretch(1, 1)
        c.body.addLayout(g)
        cap = QLabel("pixel level, detector vs your paint")
        cap.setFont(theme.ui_font(11))
        theme.set_prop(cap, "faint", True)
        c.add_widget(cap)
        c.set_copy_source(self.det_text)
        strip.add_card(c)

        strip.set_summary_source(self._strip_summary)
        return strip

    def _build_inspector(self) -> QWidget:
        insp = QWidget()
        insp.setObjectName("Panel")
        insp.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        v = QVBoxLayout(insp)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        title = QLabel("Labelling")
        title.setFont(theme.ui_font(13, QFont.Weight.DemiBold))
        exp = QPushButton("Expand all")
        col = QPushButton("Collapse all")
        for b in (exp, col):
            theme.set_prop(b, "small", True)
        head = _row(title, stretch=False)
        head.addStretch(1)
        head.addWidget(exp)
        head.addWidget(col)
        head.setContentsMargins(12, 8, 8, 8)
        v.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self._insp_layout = bl = QVBoxLayout(body)
        bl.setContentsMargins(4, 0, 4, 8)
        bl.setSpacing(2)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        self.sections = {
            "frames": self._build_frames(),
            "paint": self._build_paint(),
            "show": self._build_show(),
        }
        for s in self.sections.values():
            bl.addWidget(s)
        bl.addStretch(0)
        # The frame list takes the spare height while it is open; folded, the
        # sections below it stay at the top.
        self.sections["frames"].toggled.connect(self._frames_toggled)
        self._frames_toggled(True)
        exp.clicked.connect(lambda: [s.set_expanded(True) for s in self.sections.values()])
        col.clicked.connect(lambda: [s.set_expanded(False) for s in self.sections.values()])
        return insp

    def _frames_toggled(self, on: bool):
        self._insp_layout.setStretch(0, 1 if on else 0)
        self._insp_layout.setStretch(self._insp_layout.count() - 1, 0 if on else 1)

    def _build_frames(self) -> CollapsibleSection:
        s = CollapsibleSection("Frames")
        self.folder_line = _PathLine()
        b_open = QPushButton("Open…")
        b_open.setToolTip("Open a folder of saved frames")
        b_open.clicked.connect(self._choose_folder)
        b_scan = QPushButton("Rescan")
        b_scan.setToolTip("Look again for frames saved since the folder was opened")
        b_scan.clicked.connect(self.rescan)
        r = _row(self.folder_line, b_open, b_scan, stretch=False, spacing=6)
        r.setStretch(0, 1)
        s.add_layout(r)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter, e.g. red_s1 or 150cm")
        self.filter.setClearButtonEnabled(True)
        self.filter.setToolTip("Show only the frames whose path contains this (any case)")
        self.filter.textChanged.connect(lambda *_: self._apply_filter())
        s.add_widget(self.filter)
        self.listw = QListWidget()
        self.listw.setItemDelegate(_FrameDelegate(self.listw))
        self.listw.setUniformItemSizes(True)
        self.listw.setMinimumHeight(150)
        self.listw.currentRowChanged.connect(self._pick)
        s.add_widget(self.listw, 1)
        s.body_layout.setStretch(s.body_layout.count() - 1, 1)
        return s

    def _build_paint(self) -> CollapsibleSection:
        s = CollapsibleSection("Paint")
        self.cls_seg = SegmentedButtons([(L.BALLOON, "Balloon", "1", "label_balloon"),
                                         (L.BACKGROUND, "Background", "2", "label_not"),
                                         (L.UNLABELLED, "Erase", "0")])
        self.tool_seg = SegmentedButtons([("brush", "Brush", "B"), ("rect", "Rectangle", "R"),
                                          ("ellipse", "Ellipse", "E")], "ellipse")
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(8)
        g.addWidget(_key("Label"), 0, 0)
        g.addWidget(self.cls_seg, 0, 1, Qt.AlignmentFlag.AlignLeft)
        g.addWidget(_key("Shape"), 1, 0)
        g.addWidget(self.tool_seg, 1, 1, Qt.AlignmentFlag.AlignLeft)
        self.radius = _SpinBox()
        self.radius.setRange(0, 15)
        self.radius.setValue(2)
        self.radius.setSuffix(" px")
        self.radius.setToolTip("0 = one pixel")
        g.addWidget(_key("Brush radius"), 2, 0)
        g.addWidget(self.radius, 2, 1, Qt.AlignmentFlag.AlignLeft)
        g.setColumnStretch(1, 1)
        s.add_layout(g)
        b = QPushButton("Rest → background")
        b.setToolTip("Every unlabelled pixel becomes background, except a band around the "
                     "balloon (mixed rim colours stay unlabelled)")
        b.clicked.connect(self.fill_rest)
        self.band = _SpinBox()
        self.band.setRange(0, 6)
        self.band.setValue(2)
        self.band.setSuffix(" px")
        self.band.setToolTip("Rim width left unlabelled round each balloon")
        s.add_layout(_row(b, _key("band"), self.band))
        b_undo = HintButton("Undo", "Ctrl+Z")
        b_undo.setToolTip("Take back the last stroke or action (Ctrl+Z)")
        b_undo.clicked.connect(self.do_undo)
        b_clear = QPushButton("Clear all")
        theme.set_role(b_clear, "danger")
        b_clear.setToolTip("Every pixel back to unlabelled (Undo brings them back)")
        b_clear.clicked.connect(self.clear)
        self.save_btn = HintButton("Save", "Ctrl+S")
        theme.set_role(self.save_btn, "primary")
        self.save_btn.setToolTip("Write <frame>.labels.png now (Ctrl+S); moving to another "
                                 "frame saves too")
        self.save_btn.clicked.connect(self.save)
        r = _row(b_undo, b_clear, stretch=True, spacing=6)
        r.addWidget(self.save_btn)
        s.add_layout(r)
        for w in (self.cls_seg, self.tool_seg):
            w.changed.connect(lambda *_: self._paint_changed())
        self.radius.valueChanged.connect(lambda *_: self._paint_changed())
        return s

    def _build_show(self) -> CollapsibleSection:
        s = CollapsibleSection("Show")
        self.opacity = _Slider(Qt.Orientation.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(45)
        self.opacity_lbl = value_label()
        self.opacity_lbl.setMinimumWidth(QFontMetrics(theme.mono_font()).horizontalAdvance("100 %"))
        r = _row(_key("Label opacity"), self.opacity, self.opacity_lbl, stretch=False)
        r.setStretch(1, 1)
        s.add_layout(r)
        self.show_det = QCheckBox("Detector boxes")
        self.show_det.setChecked(True)
        s.add_widget(self.show_det)
        sub = QLabel("the camera's settings for this frame")
        sub.setFont(theme.ui_font(12))
        sub.setContentsMargins(26, 0, 0, 0)
        theme.set_prop(sub, "faint", True)
        s.add_widget(sub)
        self.stage_combo = QComboBox()
        self.stage_combo.addItems(STAGE_NAMES[:2])
        self.stage_combo.setToolTip("Stage 1 uses the committed colour table "
                                    "(main/models/lut_model.bin) instead of the HSV window.")
        r = _row(_key("Detector stage"), self.stage_combo, stretch=False)
        r.setStretch(1, 1)
        s.add_layout(r)
        self.opacity.valueChanged.connect(lambda *_: (self._show_changed(), self._draw_labels()))
        self.show_det.toggled.connect(lambda *_: (self._show_changed(), self._draw_detection()))
        self.stage_combo.currentIndexChanged.connect(
            lambda *_: (self._show_changed(), self._draw_detection()))
        return s

    def _paint_changed(self):
        cls = CLASS_NAME[self.cls_seg.value()] if self.cls_seg.value() != L.UNLABELLED else "erase"
        tool = TOOL_NAME[self.tool_seg.value()]
        self.sections["paint"].set_summary("%s · %s · r %d px" % (cls, tool, self.radius.value()))
        chip = "%s · %s" % (cls, tool)
        if tool == "brush":
            chip += " r %d" % self.radius.value()
        self.view.set_tool_text(chip)

    def _show_changed(self):
        self.opacity_lbl.setText("%d %%" % self.opacity.value())
        self.sections["show"].set_summary("labels %d %% · boxes %s · stage %d" % (
            self.opacity.value(), "on" if self.show_det.isChecked() else "off",
            self.stage_combo.currentIndex()))

    # ---- frames ------------------------------------------------------------------

    # ---- remembered layout (§9) --------------------------------------------------

    def save_state(self, store):
        persist.save_splitter(store, "split", self.split)
        persist.save_splitter(store, "vsplit", self.vsplit)
        persist.save_sections(store, self.sections)
        persist.save_zoom(store, self.view)
        store.setValue("opacity", self.opacity.value())
        store.setValue("folder", str(self.folder))

    def restore_state(self, store, folder: bool = True):
        """folder=False keeps the folder the tab was opened on (--label DIR)."""
        persist.restore_splitter(store, "split", self.split)
        persist.restore_splitter(store, "vsplit", self.vsplit)
        persist.restore_sections(store, self.sections)
        persist.restore_zoom(store, self.zoom_seg)
        v = persist.read_int(store, "opacity", self.opacity.minimum(), self.opacity.maximum())
        if v is not None:
            self.opacity.setValue(v)
        d = persist.read(store, "folder", str, lambda v: bool(v) and Path(v).is_dir())
        if folder and d is not None and Path(d) != self.folder:
            self.open_folder(Path(d))

    def entered(self):
        """The tab was shown: pick up frames saved meanwhile in the Calib tab."""
        self.rescan()

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder of saved frames", str(self.folder))
        if d:
            self.open_folder(Path(d))

    def open_folder(self, folder: Path):
        self.save_if_dirty()
        self.folder = Path(folder)
        self.sf = None
        self.rescan()

    def rescan(self):
        cur = self.sf.stem if self.sf else None
        self.stems = L.list_frames(self.folder) if self.folder.is_dir() else []
        self.folder_line.set_path(str(self.folder))
        self.listw.blockSignals(True)
        self.listw.clear()
        for s in self.stems:
            it = QListWidgetItem(str(s.relative_to(self.folder).as_posix()))
            it.setToolTip(it.text())
            it.setData(DONE_ROLE, self._done(s))
            self.listw.addItem(it)
        self.listw.blockSignals(False)
        self._apply_filter()
        if cur in self.stems:
            self.listw.setCurrentRow(self.stems.index(cur))
        elif self.stems:
            self.listw.setCurrentRow(0)
        self._show_frame_state()

    @staticmethod
    def _done(stem: Path) -> bool:
        return Path(str(stem) + L.LABEL_SUFFIX).exists()

    def _apply_filter(self):
        needle = self.filter.text().strip().lower()
        for row in range(self.listw.count()):
            it = self.listw.item(row)
            self.listw.setRowHidden(row, bool(needle) and needle not in it.text().lower())
        self._frames_summary()

    def _frames_summary(self):
        n = len(self.stems)
        if not n:
            text = "no frames"
        else:
            todo = sum(1 for row in range(n) if not self.listw.item(row).data(DONE_ROLE))
            text = "%d · %s" % (n, "all labelled" if not todo else "%d unlabelled" % todo)
            shown = sum(1 for row in range(n) if not self.listw.isRowHidden(row))
            if shown != n:
                text += " · %d shown" % shown
        self.sections["frames"].set_summary(text)

    def _pick(self, row: int):
        if not 0 <= row < len(self.stems):
            return
        if self.sf is not None and self.sf.stem == self.stems[row]:
            return
        self.save_if_dirty()
        self.load(self.stems[row])

    def step(self, d: int):
        """The next / previous frame the filter shows; at either end, stay."""
        if not self.stems:
            return
        r = self.listw.currentRow() + d
        while 0 <= r < len(self.stems) and self.listw.isRowHidden(r):
            r += d
        if 0 <= r < len(self.stems):
            self.listw.setCurrentRow(r)

    def load(self, stem: Path):
        try:
            self.sf = L.load_frame(stem)
            f = self.sf.frame
            self.lab = L.load_labels(self.sf.label_path, f.w, f.h)
        except (OSError, ValueError) as e:
            self.sf = None
            self._error = "cannot load %s: %s" % (stem.name, e)
            self._draw_detection()
            self._draw_labels()
            return
        self._error = ""
        self.rgb = f.rgb()
        self.view.set_image(self.rgb)
        self.undo.clear()
        self.dirty = False
        self._draw_detection()          # first: the label summary scores against it
        self._draw_labels()

    def _show_frame_state(self):
        """The toolbar: frame name, where it is, and whether its labels are saved."""
        n = len(self.stems)
        if self.sf is None:
            self.name_lbl.setText(self._error or "no frame")
            self.where_lbl.setText("%s/ · %d frames" % (self.folder.name, n))
            self.pill.set("err" if self._error else "neutral",
                          "cannot load" if self._error else "no frame")
            return
        stem = self.sf.stem
        rel = stem.relative_to(self.folder) if stem.is_relative_to(self.folder) else stem
        sub = self.folder.name + "/" + ("" if rel.parent == Path(".") else
                                        rel.parent.as_posix() + "/")
        i = self.stems.index(stem) + 1 if stem in self.stems else 0
        self.name_lbl.setText(stem.name)
        self.where_lbl.setText("%s · frame %d of %d" % (sub, i, n))
        if self.dirty:
            self.pill.set("warn", "not saved")
        elif self._done(stem):
            self.pill.set("ok", "saved")
        else:
            self.pill.set("neutral", "not labelled")

    # ---- painting ----------------------------------------------------------------

    def _cls(self, erase: bool) -> int:
        return L.UNLABELLED if erase else self.cls_seg.value()

    def _tool(self) -> int:
        return TOOLS.index(self.tool_seg.value())

    def _push_undo(self):
        self.undo.append(self.lab.copy())
        del self.undo[:-UNDO_DEPTH]

    def dab(self, x: float, y: float, cls: int):
        """A disc of the brush radius around pixel (x, y)."""
        r = self.radius.value()
        h, w = self.lab.shape
        xi, yi = int(math.floor(x)), int(math.floor(y))
        y0, y1, x0, x1 = max(0, yi - r), min(h, yi + r + 1), max(0, xi - r), min(w, xi + r + 1)
        if y0 >= y1 or x0 >= x1:
            return
        yy, xx = np.mgrid[y0:y1, x0:x1]
        # (r + 0.5)^2 gives round discs: r=0 one pixel, r=1 the full 3x3.
        disc = (yy - yi) ** 2 + (xx - xi) ** 2 <= (r + 0.5) ** 2
        self.lab[y0:y1, x0:x1][disc] = cls

    def fill_shape(self, ax: float, ay: float, bx: float, by: float, ellipse: bool,
                   cls: int):
        """Fill the pixels whose centres are inside the box (or the ellipse
        inscribed in it) with corners (ax, ay), (bx, by) in pixel units."""
        x0, x1 = sorted((ax, bx))
        y0, y1 = sorted((ay, by))
        h, w = self.lab.shape
        yy, xx = np.mgrid[0:h, 0:w]
        px, py = xx + 0.5, yy + 0.5
        if ellipse:
            rx, ry = max((x1 - x0) / 2, 0.5), max((y1 - y0) / 2, 0.5)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            m = ((px - cx) / rx) ** 2 + ((py - cy) / ry) ** 2 <= 1.0
        else:
            m = (px >= x0) & (px <= x1) & (py >= y0) & (py <= y1)
        self.lab[m] = cls

    def _stroke(self, kind: str, pos, erase: bool):
        """The view's paintStroke. As with the pyqtgraph drag events the tab
        used before, a drag's last position is a move as well as the end."""
        if kind == "click":
            self.click(pos, erase)
        elif kind == "start":
            self.drag_start(pos, erase)
        elif kind == "move":
            self.drag_move(pos, erase)
        else:
            self.drag_move(pos, erase)
            self.drag_end(pos, erase)

    def click(self, pos, erase: bool):
        if self.sf is None or self._tool() != 0:
            return
        self._push_undo()
        self.dab(pos.x(), pos.y(), self._cls(erase))
        self._changed()

    def drag_start(self, pos, erase: bool):
        if self.sf is None:
            return
        self._push_undo()
        self._drag0 = (pos.x(), pos.y())
        self._last = self._drag0

    def drag_move(self, pos, erase: bool):
        if self.sf is None or self._drag0 is None:
            return
        x, y = pos.x(), pos.y()
        if self._tool() == 0:
            # Fill the gap since the last event: a fast stroke moves several px.
            lx, ly = self._last
            n = max(1, int(math.hypot(x - lx, y - ly) * 2))
            for i in range(1, n + 1):
                self.dab(lx + (x - lx) * i / n, ly + (y - ly) * i / n, self._cls(erase))
            self._last = (x, y)
            self._draw_labels()
        else:
            ax, ay = self._drag0
            self.view.set_preview("ellipse" if self._tool() == 2 else "rect",
                                  min(ax, x), min(ay, y), abs(x - ax), abs(y - ay))

    def drag_end(self, pos, erase: bool):
        if self.sf is None or self._drag0 is None:
            return
        if self._tool() != 0:
            ax, ay = self._drag0
            self.fill_shape(ax, ay, pos.x(), pos.y(), self._tool() == 2, self._cls(erase))
        self.view.set_preview(None)
        self._drag0 = None
        self._changed()

    def fill_rest(self):
        if self.sf is None:
            return
        self._push_undo()
        self.lab = L.rest_to_background(self.lab, self.band.value())
        self._changed()

    def clear(self):
        if self.sf is None:
            return
        self._push_undo()
        self.lab[:] = L.UNLABELLED
        self._changed()

    def do_undo(self):
        if self.undo:
            self.lab = self.undo.pop()
            self._changed()

    def _changed(self):
        self.dirty = True
        self._draw_labels()

    # ---- saving ------------------------------------------------------------------

    def save(self):
        if self.sf is None:
            return
        L.save_labels(self.sf.label_path, self.lab)
        self.dirty = False
        row = self.stems.index(self.sf.stem) if self.sf.stem in self.stems else -1
        if row >= 0:
            self.listw.item(row).setData(DONE_ROLE, True)
            self._frames_summary()
        self._draw_labels()

    def save_if_dirty(self):
        if self.dirty:
            self.save()

    # ---- readouts ----------------------------------------------------------------

    def _hover_at(self, x, y):
        self._hover = None if x is None else (x, y)
        self._show_hover()

    def hover_text(self) -> str:
        """The Pixel card's copy: the old hover line."""
        if self._hover is None or self.sf is None:
            return NO_HOVER
        x, y = self._hover
        r, g, b = self.rgb[y, x]
        return "(%3d,%3d)  rgb %3d %3d %3d  %s" % (x, y, r, g, b,
                                                   CLASS_NAME[int(self.lab[y, x])])

    def _show_hover(self):
        if self._hover is None or self.sf is None:
            self.swatch.setStyleSheet("background: transparent; border: 1px dashed %s;"
                                      " border-radius: 3px;" % theme.TOKENS["border_strong"])
            self.px_xy.setText(NO_HOVER)
            self.px_rgb.setText("-")
            self.px_cls.setText("-")
        else:
            x, y = self._hover
            r, g, b = (int(c) for c in self.rgb[y, x])
            self.swatch.setStyleSheet("background: rgb(%d,%d,%d); border: 1px solid %s;"
                                      " border-radius: 3px;"
                                      % (r, g, b, theme.TOKENS["border_strong"]))
            self.px_xy.setText("(%d, %d)" % (x, y))
            self.px_rgb.setText("%d %d %d" % (r, g, b))
            self.px_cls.setText(CLASS_NAME[int(self.lab[y, x])])

    def sum_text(self) -> str:
        """The Labels card's copy: the old summary block, word for word."""
        if self.sf is None:
            return self._error or NO_FRAME
        bl = L.balloons(self.lab)
        n = self.lab.size
        return ("%s%s\nballoon %5d px  background %5d px  unlabelled %5d px (%.0f %%)\n"
                "balloons (labelled objects): %d%s" % (
                    self.sf.stem.name, "   * not saved" if self.dirty else "   saved",
                    (self.lab == L.BALLOON).sum(), (self.lab == L.BACKGROUND).sum(),
                    (self.lab == L.UNLABELLED).sum(),
                    100.0 * (self.lab == L.UNLABELLED).sum() / n,
                    len(bl), "".join("  [%d px at %.0f,%.0f]" % (c.area, c.cx, c.cy)
                                     for c in bl[:SHOWN_BALLOONS])))

    def det_text(self) -> str:
        """The Detector card's copy: the old detector block, word for word."""
        s, res = self._score, self._det
        if self.sf is None or s is None or res is None:
            return ""
        return ("detector, stage %d (%s): %d blob(s) reported\n"
                "vs labels: TP %d  FN %d  FP %d  split %d  blob 1 = %s\n"
                "pixels: precision %s %%  recall %s %%" % (
                    self._stage_eff, self._test_text(ascii_=True), res.n_report, s.tp, s.fn,
                    s.fp, s.split, s.b1, _pct(self._precision()), _pct(self._recall())))

    def _test_text(self, ascii_: bool) -> str:
        """What decided the pixels: the HSV window, or the colour table."""
        if self._stage_eff != 0:
            return "colour table %d" % D.models().lut_id
        st = self.sf.settings()
        fmt = ("hue %d +/-%d, sat >= %d, val >= %d" if ascii_ else
               "hue %d ±%d · sat ≥ %d · val ≥ %d")
        return fmt % (st.hue, st.tol, st.sat, st.val)

    def _precision(self) -> float:
        s = self._score
        return L.ratio(s.px_tp, s.px_tp + s.px_fp)

    def _recall(self) -> float:
        s = self._score
        return L.ratio(s.px_tp, s.px_tp + s.px_fn)

    def _strip_summary(self) -> str:
        if self.sf is None:
            return self._error or NO_FRAME
        out = ["balloon %d px" % (self.lab == L.BALLOON).sum(),
               "background %d px" % (self.lab == L.BACKGROUND).sum(),
               "unlabelled %d" % (self.lab == L.UNLABELLED).sum()]
        s = self._score
        if s is not None:
            out.append("TP %d FN %d FP %d split %d" % (s.tp, s.fn, s.fp, s.split))
            out.append("precision %s %%" % _pct(self._precision()))
            out.append("recall %s %%" % _pct(self._recall()))
        return " · ".join(out)

    # ---- drawing -----------------------------------------------------------------

    def _draw_labels(self):
        h, w = self.lab.shape
        a = int(round(self.opacity.value() * 2.55))
        ov = np.zeros((h, w, 4), dtype=np.uint8)
        for v, token in TINT.items():
            c = theme.qcolor(token)
            ov[self.lab == v] = (c.red(), c.green(), c.blue(), a)
        self.view.set_overlay(ov)
        bl = L.balloons(self.lab)
        self._bal_boxes = [(c.x0, c.y0, c.x1 - c.x0 + 1, c.y1 - c.y0 + 1,
                            theme.TOKENS["label_balloon"], "", "dot") for c in bl]
        self._set_boxes()
        self._show_counts(bl)
        self._show_hover()
        self._show_frame_state()
        self._draw_score()

    def _set_boxes(self):
        self.view.set_boxes(self._det_boxes + self._bal_boxes)

    def _show_counts(self, bl):
        n = self.lab.size
        counts = {v: int((self.lab == v).sum()) for v in (L.BALLOON, L.BACKGROUND, L.UNLABELLED)}
        have = self.sf is not None
        self.share_bar.set_parts([(counts[L.BALLOON] if have else 0, TINT[L.BALLOON]),
                                  (counts[L.BACKGROUND] if have else 0, TINT[L.BACKGROUND])]
                                 + ([(counts[L.UNLABELLED], "border_strong")] if have else []))
        for v, (px, pc) in self.cls_rows.items():
            px.setText("%d px" % counts[v] if have else "-")
            pc.setText("%.1f %%" % (100.0 * counts[v] / n) if have else "")
        self.n_balloons.setText("%d" % len(bl) if have else "-")
        lines = ["#%d · %d px at (%.0f, %.0f)" % (k + 1, c.area, c.cx, c.cy)
                 for k, c in enumerate(bl[:SHOWN_BALLOONS])]
        if len(bl) > SHOWN_BALLOONS:
            lines.append("+%d more" % (len(bl) - SHOWN_BALLOONS))
        self.balloon_list.setText("\n".join(lines) if have else "")
        self.balloon_list.setVisible(have and bool(lines))
        self.sum_note.setText(self._error or NO_FRAME)
        self.sum_note.setVisible(not have)

    def _draw_detection(self):
        self._det_boxes = []
        if self.sf is None:
            self._det = self._mask = self._score = None
            self._set_boxes()
            self._show_score()
            return
        st = self.sf.settings()
        stage = self.stage_combo.currentIndex()
        tr = D.pipeline(st, self.sf.frame.data, stage)
        self._det, self._mask = tr.r, D.mask(st, self.sf.frame.data, stage)
        self._stage_eff = tr.stage
        res = tr.r
        if self.show_det.isChecked():
            for k in range(res.n_report):
                b = res.b[k]
                self._det_boxes.append((b.x0, b.y0, b.x1 - b.x0 + 1, b.y1 - b.y0 + 1,
                                        BOX_COLOURS[0 if k == 0 else 1], "%d" % (k + 1)))
        self._set_boxes()
        self._draw_score()

    def _draw_score(self):
        """The eval_frames verdict for this frame, live while labelling."""
        res = self._det
        if self.sf is None or res is None:
            self._score = None
        else:
            self._score = L.score(self.sf.stem.name, self.lab,
                                  [(res.b[k].cx, res.b[k].cy) for k in range(res.n_report)],
                                  self._mask)
        self._show_score()

    def _show_score(self):
        s, res = self._score, self._det
        if s is None or res is None or self.sf is None:
            self.det_stage.setText("no frame")
            self.det_test.setText("")
            self.det_blobs.setText("")
            for t in self.tiles.values():
                t.set(None)
            for bar, val in self.pr.values():
                bar.set_fraction(0.0)
                val.setText("-")
        else:
            name = STAGE_NAMES[self._stage_eff].split(None, 1)[1]
            self.det_stage.setText("stage %d · %s" % (self._stage_eff, name))
            self.det_test.setText(self._test_text(ascii_=False))
            self.det_blobs.setText("%d blob%s reported · blob 1 = %s"
                                   % (res.n_report, "" if res.n_report == 1 else "s", s.b1))
            self.tiles["TP"].set(s.tp, "ok" if s.tp > 0 else "neutral")
            self.tiles["FN"].set(s.fn)
            self.tiles["FP"].set(s.fp)
            self.tiles["split"].set(s.split)
            for k, x in (("precision", self._precision()), ("recall", self._recall())):
                bar, val = self.pr[k]
                bar.set_fraction(0.0 if math.isnan(x) else x)
                val.setText("-" if math.isnan(x) else "%.1f %%" % (100 * x))
        self.strip.refresh_summary()
