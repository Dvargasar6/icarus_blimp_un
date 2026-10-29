"""
Image & calibration screen.

Shows the camera's real pixels (fetched over the serial link as raw RGB565),
lets you read the colour of any pixel or region, and tune the detector by hand.

Everything drawn over the image is computed on the PC by the camera's own C
detector (libblob_detect.so) with the values the controls show - so moving a
slider updates the mask at once, before anything is sent. The camera's own
answer for the same frame (its CSV row) is drawn as white crosses. When the
controls are all "applied", the PC boxes and the camera crosses must agree; if
they ever do not, something other than the thresholds differs, and that is
worth knowing.

While frames are being sent the camera's loop is slowed by the transfer, so
dt_ms and FPS on this screen are NOT what flight will see. The Flight data
screen is the one to trust for timing.

Layout (docs/GUI_REDESIGN.md §6.1): the image pane on the left (toolbar,
PixelImageView, the Camera readouts strip under it) and the Calibration
inspector on the right, a column of collapsible sections that scrolls on a
small screen. A collapsed section still shows its values in its header.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QImage
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                             QGraphicsOpacityEffect, QGridLayout, QHBoxLayout,
                             QLabel, QMessageBox, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from . import detector as D
from . import protocol as P
from .ui import persist, theme
from .ui.keys import HintButton, add_shortcut
from .ui.widgets import (Banner, CollapsibleSection, FlowLayout, GripSplitter,
                         PixelImageView, ReadoutCard, ReadoutStrip, SegmentedButtons,
                         StripSplitter, ToggleSwitch, value_label)
from .widgets import AMBER, GREEN, GREY, RED, HueBar, ParamControl, _SpinBox, chip

DATA_DIR = Path(__file__).resolve().parents[3] / "data"      # target_cam/data
W, H = 160, 120

# Control key -> (command format, #!CFG key)
PARAMS = {
    "hue": ("H%d", "hue"),
    "tol": ("T%d", "tol"),
    "sat": ("S%d", "sat"),
    "val": ("V%d", "val"),
    "area": ("a%d", "area"),
    "mm": ("d%d", "mm"),
    "fov": ("F%.1f", "fov"),
}

# The detector levels '!stage N' selects (tc_pipeline.h). Each falls back to
# the highest one whose models the camera has.
STAGE_NAMES = ["0  HSV window", "1  colour table", "2  blob classifier", "3  CNN"]

BOX_COLOURS = ("#ffe600", "#ff9600")        # largest blob, the others
TINT = (0, 255, 255, 150)                   # matching pixels, "tinted" overlay
NO_HOVER = "hover over the image for a pixel's colour"
GRAB_HOLD_MS = 200                          # Grab frame stays pressed this long
NOT_FLIGHT = "While frames are sent, the camera's dt_ms / FPS are not flight timing."


def _row(*items, stretch: bool = True, spacing: int = 8) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for it in items:
        if isinstance(it, QWidget):
            lay.addWidget(it)
        else:
            lay.addLayout(it)
    if stretch:
        lay.addStretch(1)
    return lay


def _group(*widgets) -> QWidget:
    """Widgets that stay on one line when the toolbar wraps. The flow layout
    hands a line's spare width to its last item: the stretch soaks it up."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    for x in widgets:
        lay.addWidget(x)
    lay.addStretch(1)
    return w


def _chip_label(wrap: bool = False) -> QLabel:
    """A label for chip(): as tall as its text, not as tall as its row."""
    lbl = QLabel()
    lbl.setWordWrap(wrap)
    lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    return lbl


def _key(text: str, tip: str = "") -> QLabel:
    k = QLabel(text)
    k.setFont(theme.ui_font(12))
    theme.set_prop(k, "muted", True)
    if tip:
        k.setToolTip(tip)
    return k


class CalibTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.frame: P.Frame | None = None
        self.frame_row: dict | None = None
        self.frame_time = 0.0
        self.cfg: dict = {}
        self.pending_cmds: dict = {}        # command text -> ParamControl
        self._hover = None
        self._region_stats = None           # (hue, spread, matched fraction) for the summary
        self.compare_text = ""
        self.region_text = ""

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

        # The firmware defaults, until the camera's #!CFG says otherwise.
        for key, v in (("hue", 0), ("tol", 12), ("sat", 110), ("val", 50), ("area", 25),
                       ("mm", 550), ("fov", 54.0)):
            self.p[key]._set_widgets(v)
        for key, ctl in self.p.items():
            ctl.changed.connect(lambda _v: self._redraw())
            ctl.commit.connect(lambda v, k=key: self._commit(k, v))
        add_shortcut(self, "G", self._grab)
        add_shortcut(self, "C", self.cal_btn.click)
        self._show_settings()
        self._show_hover()
        self._redraw()

    # ---- remembered layout (§9) --------------------------------------------------

    def save_state(self, store):
        persist.save_splitter(store, "split", self.split)
        persist.save_splitter(store, "vsplit", self.vsplit)
        persist.save_sections(store, self.sections)
        persist.save_zoom(store, self.view)

    def restore_state(self, store):
        persist.restore_splitter(store, "split", self.split)
        persist.restore_splitter(store, "vsplit", self.vsplit)
        persist.restore_sections(store, self.sections)
        persist.restore_zoom(store, self.zoom_seg)

    # ---- building ---------------------------------------------------------------

    def _build_image_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(8, 8, 4, 0)
        v.setSpacing(6)

        tb = QWidget()
        flow = FlowLayout(tb, hspacing=16, vspacing=6)
        flow.setContentsMargins(0, 0, 0, 0)
        self.grab_btn = HintButton("Grab frame", "G")
        theme.set_role(self.grab_btn, "primary")
        self.grab_btn.clicked.connect(self._grab)
        self.live_chk = ToggleSwitch("Live")
        self.live_chk.setToolTip("A frame every loop (v1 / v0)")
        self.live_chk.clicked.connect(lambda on: self.main.send("v1" if on else "v0"))
        flow.addWidget(_group(self.grab_btn, self.live_chk))
        self.overlay_mode = QComboBox()
        self.overlay_mode.addItems(["matching pixels tinted", "mask only (white = match)", "off"])
        self.overlay_mode.currentIndexChanged.connect(lambda *_: self._redraw())
        self.show_boxes = QCheckBox("PC detection boxes")
        self.show_boxes.setChecked(True)
        self.show_boxes.toggled.connect(lambda *_: self._redraw())
        flow.addWidget(_group(_key("Overlay"), self.overlay_mode, self.show_boxes))
        self.zoom_seg = SegmentedButtons([("fit", "Fit"), ("2", "2×"), ("3", "3×"), ("4", "4×")])
        self.zoom_seg.setToolTip("Zoom (also Ctrl+wheel). Pan with the scroll bars, "
                                 "middle-drag or Space+drag.")
        self.save_btn = QPushButton("Save frame…")
        self.save_btn.setToolTip("PNG for viewing, the raw .rgb565 bytes and the settings as "
                                 ".json (enough to replay this frame through the host tests).")
        self.save_btn.clicked.connect(self._save_frame)
        # In the camera, not on screen: the sensor turns the image before the
        # detector sees it, so az/el, saved frames and blob crops come out
        # the right way up too. Enabled once the camera reports flip= (a
        # firmware without !flip never does).
        self.flip_btn = QPushButton("Rotate 180°")
        self.flip_btn.setCheckable(True)
        self.flip_btn.setEnabled(False)
        self.flip_btn.setToolTip("For a board whose image arrives upside down: the camera turns "
                                 "it 180° in the sensor ('!flip 1'), so detection, az/el, saved "
                                 "frames and blob crops are turned too. Save to camera keeps it. "
                                 "Needs a target_cam firmware with !flip.")
        self.flip_btn.clicked.connect(lambda on: self.main.send("!flip 1" if on else "!flip 0"))
        flow.addWidget(_group(self.zoom_seg, self.save_btn, self.flip_btn))
        v.addWidget(tb)

        self.view = PixelImageView(W, H)
        # Where the camera's 'c' samples: the 16x16 centre patch.
        c0x, c0y = (W - D.CAL_PATCH) // 2, (H - D.CAL_PATCH) // 2
        self.view.set_guides([(c0x, c0y, D.CAL_PATCH, D.CAL_PATCH)])
        self.view.set_region(c0x, c0y, D.CAL_PATCH, D.CAL_PATCH, emit=False)
        self.view.set_tool_text("no frame yet: Grab frame")
        self.view.hovered.connect(self._hover_at)
        self.view.left.connect(self._hover_left)
        self.view.regionChanged.connect(lambda _r: self._update_region())
        self.zoom_seg.changed.connect(self.view.set_zoom)
        self.view.zoomChanged.connect(lambda k: self.zoom_seg.set_value(k, emit=False))

        self.strip = self._build_readouts()
        self.vsplit = StripSplitter(self.view, self.strip)
        self.strip_scroll = self.vsplit.strip_scroll
        v.addWidget(self.vsplit, 1)
        return pane

    def _build_readouts(self) -> ReadoutStrip:
        strip = ReadoutStrip("Camera readouts")

        c = ReadoutCard("Pixel under cursor")
        self.swatch = QLabel()
        self.swatch.setFixedSize(18, 18)        # a colour sample, not a panel: §2 allows it
        self.px_xy = value_label()
        self.px_match = _chip_label()
        c.body.addLayout(_row(self.swatch, self.px_xy, self.px_match))
        self.px_565 = c.add_kv("rgb565")
        self.px_rgb = c.add_kv("RGB")
        self.px_hsv = c.add_kv("HSV")
        c.set_copy_source(self._pixel_text)
        strip.add_card(c)

        c = ReadoutCard("Detection", "PC vs camera")
        self.det_badge = _chip_label()
        c.add_header_widget(self.det_badge)
        # The subtitle gives way to anything in its row; here it should not.
        c.subtitle_lbl.setMinimumWidth(c.subtitle_lbl.sizeHint().width())
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(4)
        for col, h in enumerate(("", "verdict", "blobs", "area", "matched")):
            if h:
                lbl = QLabel(h)
                lbl.setFont(theme.ui_font(11))
                theme.set_prop(lbl, "faint", True)
                g.addWidget(lbl, 0, col)
        self.det_cells = {}
        for r, (k, name, tip) in enumerate((("pc", "PC", "PC (controls)"),
                                            ("cam", "Camera", "camera (its row)")), 1):
            g.addWidget(_key(name, tip), r, 0)
            cells = [_chip_label()] + [value_label() for _ in range(3)]
            for col, w in enumerate(cells, 1):
                g.addWidget(w, r, col)
            self.det_cells[k] = cells
        g.setColumnStretch(5, 1)
        c.body.addLayout(g)
        self.det_note = value_label(wrap=True, mono=False)
        theme.set_prop(self.det_note, "faint", True)
        c.add_widget(self.det_note)
        c.set_copy_source(lambda: self.compare_text)
        strip.add_card(c)

        c = ReadoutCard("Frame & link")
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(3)
        self.fl = {}
        for r, row in enumerate((("seq", "camera t"), ("format", "received"),
                                 ("on the wire",), ("frames",))):
            for k, key in enumerate(row):
                g.addWidget(_key(key), r, 2 * k, Qt.AlignmentFlag.AlignTop)
                self.fl[key] = value_label()
                g.addWidget(self.fl[key], r, 2 * k + 1, 1, 1 if len(row) > 1 else 3,
                            Qt.AlignmentFlag.AlignTop)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        c.body.addLayout(g)
        note = QLabel(NOT_FLIGHT)
        note.setWordWrap(True)
        note.setFont(theme.ui_font(12))
        note.setStyleSheet("color: %s;" % theme.TOKENS["warn_text"])
        c.add_widget(note)
        c.set_copy_source(self._frame_link_text)
        strip.add_card(c)

        c = ReadoutCard("Camera settings")
        self.settings_lbl = _chip_label(wrap=True)
        c.add_widget(self.settings_lbl)
        self.set_stage_v = c.add_kv("detector")
        self.set_preset_v = c.add_kv("preset")
        self.set_order_v = c.add_kv("byte order")
        c.set_copy_source(self._settings_text)
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
        title = QLabel("Calibration")
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
        bl = QVBoxLayout(body)
        bl.setContentsMargins(4, 0, 4, 8)
        bl.setSpacing(2)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        self.sections = {
            "region": self._build_region(),
            "colour": self._build_colour(),
            "blobs": self._build_blobs(),
            "sensor": self._build_sensor(),
            "flash": self._build_flash(),
        }
        for s in self.sections.values():
            bl.addWidget(s)
        bl.addStretch(1)
        exp.clicked.connect(lambda: [s.set_expanded(True) for s in self.sections.values()])
        col.clicked.connect(lambda: [s.set_expanded(False) for s in self.sections.values()])
        return insp

    def _build_region(self) -> CollapsibleSection:
        s = CollapsibleSection("Region")
        self.use_btn = QPushButton("Use region as target")
        self.use_btn.setToolTip("Sets hue/tolerance/sat/val from the region with the same "
                                "rule as the camera's 'c', and sends them.")
        self.use_btn.clicked.connect(self._use_region)
        hint = QLabel("drag / resize the cyan box")
        theme.set_prop(hint, "faint", True)
        s.add_layout(_row(self.use_btn, hint))
        self.use_warn = _chip_label(wrap=True)
        self.use_warn.hide()
        s.add_widget(self.use_warn)

        c = ReadoutCard("Camera sampler")
        self.reg_box = c.add_kv("box", "-")
        self.reg_hue = c.add_kv("hue", "-")
        self.reg_sat = c.add_kv("sat p10/50/90", "-")
        self.reg_val = c.add_kv("val p10/50/90", "-")
        self.reg_match = c.add_kv("matching", "-")
        self.reg_bar = c.add_bar(0.0)
        c.set_copy_source(lambda: self.region_text)
        s.add_widget(c)

        self.hist = pg.PlotWidget()
        self.hist.setMinimumHeight(100)
        self.hist.setMaximumHeight(150)
        self.hist.setMouseEnabled(False, False)
        self.hist.setMenuEnabled(False)
        self.hist.hideButtons()
        self.hist.setXRange(0, 360, padding=0)
        self.hist.getViewBox().setLimits(yMin=0)        # counts: never a negative axis
        self.hist.getAxis("left").setWidth(30)
        self.hist.getAxis("bottom").setTicks([[(h, str(h)) for h in range(0, 361, 60)]])
        self.hist.setLabel("bottom", "Hue of region pixels with sat ≥ 30")
        self.bars = pg.BarGraphItem(x=np.arange(5, 360, 10), height=np.zeros(36), width=9,
                                    brushes=[pg.mkBrush(QColor.fromHsv(h, 255, 230))
                                             for h in range(5, 360, 10)])
        self.hist.addItem(self.bars)
        pen = pg.mkPen(theme.TOKENS["text"], style=Qt.PenStyle.DashLine)
        self.win_lo = pg.InfiniteLine(angle=90, pen=pen)
        self.win_hi = pg.InfiniteLine(angle=90, pen=pen)
        self.hist.addItem(self.win_lo)
        self.hist.addItem(self.win_hi)
        s.add_widget(self.hist)
        return s

    def _build_colour(self) -> CollapsibleSection:
        s = CollapsibleSection("Colour test")
        # The stage decides which colour test runs. From stage 1 up it is the
        # colour table, and the HSV controls below no longer change which
        # pixels match: they are kept, and used again at stage 0.
        self.stage_combo = QComboBox()
        self.stage_combo.addItems(STAGE_NAMES)
        b = QPushButton("Set")
        b.setToolTip("!stage N. Not saved until 'Save to camera'.")
        b.clicked.connect(lambda: self.main.send("!stage %d" % self.stage_combo.currentIndex()))
        s.add_layout(_row(_key("Detector stage"), self.stage_combo, b))
        self.stage_lbl = Banner("neutral", "")
        self.stage_lbl.hide()
        s.add_widget(self.stage_lbl)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(D.PRESETS)
        pb = QPushButton("Apply")
        pb.setToolTip("p <name>: the preset's hue window")
        pb.clicked.connect(lambda: self.main.send("p " + self.preset_combo.currentText()))
        self.preset_lbl = value_label()
        theme.set_prop(self.preset_lbl, "muted", True)
        s.add_layout(_row(_key("Preset"), self.preset_combo, pb, self.preset_lbl))

        self.hsv_box = QWidget()
        hl = QVBoxLayout(self.hsv_box)
        hl.setContentsMargins(0, 4, 0, 0)
        hl.setSpacing(4)
        self.huebar = HueBar()
        hl.addWidget(self.huebar)
        self.p = {}
        self.p["hue"] = ParamControl("Hue", 0, 359, unit="°")
        self.p["tol"] = ParamControl("Tolerance", 1, 90, unit="°")
        self.p["sat"] = ParamControl("Min sat", 0, 255)
        self.p["val"] = ParamControl("Min value", 0, 255)
        for k in ("hue", "tol", "sat", "val"):
            hl.addWidget(self.p[k])
        s.add_widget(self.hsv_box)
        self._dim = QGraphicsOpacityEffect(self.hsv_box)
        self._dim.setOpacity(0.5)
        self._dim.setEnabled(False)
        self.hsv_box.setGraphicsEffect(self._dim)

        self.cal_btn = cal = HintButton("Calibrate on centre", "C")
        cal.setToolTip("Sends 'c': the camera samples the dashed 16x16 centre patch of "
                       "its NEXT frame.")
        cal.clicked.connect(lambda: self.main.send("c"))
        s.add_layout(_row(cal))
        self.cal_lbl = _chip_label(wrap=True)
        self.cal_lbl.hide()
        s.add_widget(self.cal_lbl)
        return s

    def _build_blobs(self) -> CollapsibleSection:
        s = CollapsibleSection("Blobs & geometry")
        self.p["area"] = ParamControl("Min area", 1, 2000, unit=" px")
        self.p["mm"] = ParamControl("Target size", 0, 5000, slider=False, unit=" mm")
        self.p["fov"] = ParamControl("h-FOV", 10, 150, step=0.1, decimals=1, slider=False,
                                     unit="°")
        for k in ("area", "mm", "fov"):
            s.add_widget(self.p[k])
        return s

    def _build_sensor(self) -> CollapsibleSection:
        s = CollapsibleSection("Sensor")
        # White balance (report §10.7): auto re-solves the gains from every
        # scene, so one balloon's hue drifted ~25° between rooms; fixed gains,
        # set once from a white card, make colour comparable and are recorded
        # with every frame and blob.
        # A segment's third field is a hint painted inside it, so the
        # explanations go in tooltips and the segments carry only names.
        self.wb_seg = SegmentedButtons([("auto", "Auto"), ("frozen", "Frozen"),
                                        ("fixed", "Fixed")])
        for key, tip in (
                ("auto", "w1: the sensor re-solves the gains from each scene. Gains "
                 "not known, so colour drifts with what is in view."),
                ("frozen", "w0: auto stopped where it is now. Gains not known."),
                ("fixed", "w2: the R, G, B gains below, recorded with every frame "
                 "and blob.")):
            self.wb_seg.buttons[key].setToolTip(tip)
        self.wb_seg.changed.connect(
            lambda k: self.main.send({"auto": "w1", "frozen": "w0", "fixed": "w2"}[k]))
        s.add_layout(_row(_key("White balance"), self.wb_seg))
        self.wb_spin = []
        for ch in "RGB":
            sp = _SpinBox()
            sp.setRange(1, 255)
            sp.setValue(64)
            sp.setPrefix(ch + " ")
            sp.setToolTip("64 = gain 1.0 (inferred from the driver's presets)")
            self.wb_spin.append(sp)
        self.wb_set_btn = set_btn = QPushButton("Set")
        set_btn.setToolTip("W<r>,<g>,<b>: load these gains and switch to Fixed.")
        set_btn.clicked.connect(lambda: self.main.send(
            "W%d,%d,%d" % tuple(sp.value() for sp in self.wb_spin)))
        s.add_layout(_row(_key("Gains"), *self.wb_spin, set_btn))
        self.wbcal_btn = QPushButton("From white card")
        self.wbcal_btn.setToolTip("Wc: hold a white card over the dashed centre patch. The "
                                  "camera adjusts R and B until the card reads grey (about "
                                  "1-4 s), then runs Fixed. Save to camera keeps the gains.")
        self.wbcal_btn.clicked.connect(lambda: self.main.send("Wc"))
        self.wb_lbl = _chip_label(wrap=True)
        self.wb_lbl.hide()
        self._wb_fixed_seen = None
        self._wb_enable(False)
        s.add_layout(_row(self.wbcal_btn))
        s.add_widget(self.wb_lbl)
        self.order_btn = QPushButton("Byte order: ?")
        self.order_btn.setToolTip("Flips how the camera reads RGB565 ('o'). Big-endian was "
                                  "confirmed correct on 2026-10-13; the wrong one looks like "
                                  "speckle everywhere.")
        self.order_btn.clicked.connect(lambda: self.main.send("o"))
        s.add_layout(_row(self.order_btn))
        return s

    def _build_flash(self) -> CollapsibleSection:
        s = CollapsibleSection("Camera flash memory")
        b1 = QPushButton("Save to camera")
        b1.setToolTip("!save: the camera boots with these settings from now on.")
        b1.clicked.connect(lambda: self.main.send("!save"))
        b2 = QPushButton("Reload saved")
        b2.setToolTip("!load: back to what the camera has saved.")
        b2.clicked.connect(lambda: self.main.send("!load"))
        b3 = QPushButton("Factory defaults…")
        theme.set_role(b3, "danger")
        b3.clicked.connect(self._factory)
        lay = FlowLayout(hspacing=8, vspacing=6)
        for b in (b1, b2, b3):
            lay.addWidget(b)
        w = QWidget()
        w.setLayout(lay)
        s.add_widget(w)
        return s

    # ---- from the main window ------------------------------------------------

    def settings(self) -> D.Settings:
        """The detector settings the controls show (may not be applied yet)."""
        s = D.Settings.from_cfg(self.cfg)
        for k in ("hue", "tol", "sat", "val", "area"):
            setattr(s, k, self.p[k].local)
        if self.frame is not None:
            s.be = int(self.frame.big_endian)
        return s

    def stage(self) -> int:
        """The stage the camera was asked for. The PC runs the same request
        through the same fallback, so it shows what the camera computes."""
        return int(self.cfg.get("stage", 0))

    def _show_stage(self, v: dict):
        req, eff = v.get("stage", 0), v.get("stage_eff", 0)
        if 0 <= req < len(STAGE_NAMES):
            self.stage_combo.setCurrentIndex(req)
        pc = D.models()
        cam_id = v.get("lut_id", 0)
        if v.get("lut") == "ok" and pc.lut and cam_id != pc.lut_id:
            self.stage_lbl.set("err", "Colour table MISMATCH: camera %s, PC %d. The PC overlay "
                               "is not what the camera computes; reflash, or retrain on the PC."
                               % (cam_id, pc.lut_id))
        elif eff >= 1 and not pc.lut:
            self.stage_lbl.set("err", "Camera runs stage %d, but the PC has no colour table "
                               "(main/models/lut_model.bin): the overlay falls back to HSV." % eff)
        elif eff != req:
            self.stage_lbl.set("warn", "Stage %d requested, running %d (colour table %s)." % (
                req, eff, v.get("lut", "?")))
        elif eff >= 1:
            self.stage_lbl.set("info", "Stage %d: colour table %s decides the pixels. Hue, "
                               "tolerance, sat, value and c have no effect until stage 0."
                               % (eff, cam_id))
        else:
            self.stage_lbl.set("neutral", "")
        # At stage 0 the HSV window decides, as the controls say: nothing to flag.
        self.stage_lbl.setVisible(bool(self.stage_lbl.text()))
        # The HSV controls stay editable at stage >= 1 (they are what stage 0
        # will use), but drawn faded, since they decide nothing now.
        self._dim.setEnabled(eff >= 1)

    def on_cfg(self, v: dict):
        self.cfg = v
        self._show_stage(v)
        for key, (_, ck) in PARAMS.items():
            if ck in v:
                self.p[key].set_camera(v[ck])
        self.live_chk.setChecked(v.get("image") == 1)
        self._show_wb(v)
        self.flip_btn.setEnabled("flip" in v)
        self.flip_btn.setChecked(v.get("flip") == 1)
        self.order_btn.setText("Byte order: %s-endian" % ("big" if v.get("be") == 1 else "LITTLE"))
        preset = v.get("preset", "?")
        self.preset_lbl.setText("camera: %s" % preset)
        if preset in D.PRESETS:
            self.preset_combo.setCurrentText(preset)
        self._show_settings()
        self._redraw()

    def on_disconnected(self):
        """The link is gone: forget everything the camera said, as at start.
        The frame on screen stays."""
        self.cfg = {}
        self.pending_cmds.clear()
        for ctl in self.p.values():
            ctl.forget_camera()
        self.stage_lbl.set("neutral", "")
        self.stage_lbl.hide()
        self._dim.setEnabled(False)
        self.preset_lbl.setText("")
        self.live_chk.setChecked(False)
        self.flip_btn.setChecked(False)
        self.flip_btn.setEnabled(False)
        self.order_btn.setText("Byte order: ?")
        self.wb_lbl.hide()
        self._wb_fixed_seen = None
        self._wb_enable(False)
        self._show_settings()
        self._redraw()

    def on_frame(self, frame: P.Frame):
        self.frame = frame
        self.view.set_tool_text("")
        self.frame_row = None
        self.frame_time = time.monotonic()
        self._redraw()
        self._update_region()

    def on_row(self, row: P.Row):
        if self.frame is not None and self.frame_row is None and \
                int(row.v["t_ms"]) == self.frame.t_ms:
            self.frame_row = row.v
            self._redraw()

    def on_command_done(self, cmd: str, ok: bool, reason: str):
        ctl = self.pending_cmds.pop(cmd, None)
        if ctl is not None:
            ctl.done(ok)

    def _show_wb(self, v: dict):
        mode, gain = P.wb_of(v)
        self.wb_seg.set_value(mode, emit=False)
        # Firmware before fixed gains sends awb= but no wb=, and would refuse
        # 'w2' and 'W': those controls wait for a camera that has them.
        self._wb_enable("wb" in v)
        try:
            fixed = P._ints(str(v.get("wb_fixed", "")), 3)
        except ValueError:
            fixed = None
        # Follow the camera's stored gains only when they change (a 'Wc', a
        # !load), so a #!CFG for some other command does not undo gains
        # typed here but not yet sent.
        if fixed is not None and fixed != self._wb_fixed_seen:
            self._wb_fixed_seen = fixed
            for sp, g in zip(self.wb_spin, fixed):
                sp.setValue(g)
        if mode == "fixed":
            chip(self.wb_lbl, "Fixed gains %d,%d,%d: recorded with every frame and blob."
                 % gain, GREEN)
        elif mode in ("auto", "frozen"):
            chip(self.wb_lbl, "%s: gains not known, so colour is not comparable between "
                 "scenes. Calibrate from a white card to fix them." % mode.capitalize(), AMBER)
        else:
            chip(self.wb_lbl, "This firmware does not report its white balance.", GREY)
        self.wb_lbl.show()

    def _wb_enable(self, on: bool):
        for w in (self.wb_seg.buttons["fixed"], *self.wb_spin, self.wb_set_btn,
                  self.wbcal_btn):
            w.setEnabled(on)

    def on_wbcal(self, cal: P.WbCal):
        """The end of 'Wc'. Only a refusal needs saying here: success shows
        as the new gains in the #!CFG that follows it."""
        v = cal.v
        if cal.status == "refused":
            why = {"dark": "the centre is too dark (fill it with the card, light it)",
                   "clipped": "the card is overexposed",
                   "range": "it needs a gain beyond what the sensor allows (is the card white?)",
                   "converge": "it did not settle"}.get(v.get("reason"), v.get("reason"))
            chip(self.wb_lbl, "White card REFUSED: %s. Camera read R %s G %s B %s; white "
                 "balance put back as it was." % (why, v.get("R"), v.get("G"), v.get("B")), RED)
        elif cal.status == "cancelled":
            chip(self.wb_lbl, "White card cancelled by another white-balance command.", AMBER)
        self.wb_lbl.show()

    def on_cal(self, cal: P.Cal):
        v = cal.v
        txt = "camera sampled hue %s +/-%s, sat %s, val %s" % (
            v.get("hue"), v.get("spread"), v.get("sat"), v.get("val"))
        if cal.ok:
            chip(self.cal_lbl, txt + ": applied", GREEN)
        else:
            chip(self.cal_lbl, txt + ": REFUSED (too grey)", RED)
        self.cal_lbl.show()

    def tick(self):
        """1 Hz from the main window: link figures."""
        self._show_frame_link()
        self.strip.refresh_summary()

    # ---- actions ---------------------------------------------------------------

    def _grab(self):
        # A mouse click is over in about 100 ms, too quick to see the pressed
        # colour, and the frame takes seconds to arrive at 115200 baud. Hold
        # the pressed look a little longer so the click visibly registered.
        self.grab_btn.setDown(True)
        QTimer.singleShot(GRAB_HOLD_MS, lambda: self.grab_btn.setDown(False))
        self.main.send("g")

    def _commit(self, key, v):
        fmt, _ = PARAMS[key]
        cmd = fmt % v
        self.pending_cmds[cmd] = self.p[key]
        if not self.main.send(cmd):         # not connected: nothing is in flight
            self.pending_cmds.pop(cmd, None)
            self.p[key].done(False)

    def _use_region(self):
        if self.frame is None:
            return
        x, y, w, h = self.view.region()
        col = D.sample(self.settings(), self.frame.data, x, y, w, h)
        cal = D.calibrate_from(col)
        if cal.warning:
            chip(self.use_warn, cal.warning, RED if cal.refused else AMBER)
        else:
            chip(self.use_warn, "hue %d +/-%d, sat >= %d, val >= %d sent" % (
                cal.hue, cal.tol, cal.sat, cal.val), GREEN)
        self.use_warn.show()
        for k, v in (("hue", cal.hue), ("tol", cal.tol), ("sat", cal.sat), ("val", cal.val)):
            self.p[k].set_local(v, send=True)

    def _factory(self):
        if QMessageBox.question(self, "Factory defaults",
                                "Erase the settings saved in the camera and go back to the "
                                "firmware defaults?") == QMessageBox.StandardButton.Yes:
            self.main.send("!reset")

    def _save_frame(self):
        if self.frame is None:
            self.main._say("# [viewer] no frame to save: press Grab frame first", "err")
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # The dialog opens where the last frame went: a capture session is one
        # subfolder (the training splits train/validation by session), chosen
        # once rather than navigated to for every frame.
        folder = getattr(self, "_save_dir", DATA_DIR)
        if not folder.is_dir():
            folder = DATA_DIR
        stem = time.strftime("frame_%Y%m%d_%H%M%S") + "_seq%d" % self.frame.seq
        path, _ = QFileDialog.getSaveFileName(self, "Save frame", str(folder / (stem + ".png")),
                                              "PNG (*.png)")
        if not path:
            return
        base = Path(path).with_suffix("")
        self._save_dir = base.parent
        rgb = np.ascontiguousarray(self.frame.rgb())
        QImage(rgb.data, W, H, 3 * W, QImage.Format.Format_RGB888).copy().save(str(base) + ".png")
        Path(str(base) + ".rgb565").write_bytes(self.frame.data)
        meta = {"seq": self.frame.seq, "t_ms": self.frame.t_ms, "w": W, "h": H,
                "fmt": self.frame.fmt, "camera_cfg": self.cfg,
                "camera_row": self.frame_row,
                "viewer_controls": {k: c.local for k, c in self.p.items()},
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        Path(str(base) + ".json").write_text(json.dumps(meta, indent=2))
        self.main._say("# [viewer] saved %s.{png,rgb565,json}" % base)

    # ---- readouts --------------------------------------------------------------

    def _hover_at(self, x, y):
        self._hover = (x, y)
        self._show_hover()

    def _hover_left(self):
        self._hover = None
        self._show_hover()

    def _pixel(self):
        """(x, y, rgb565, (r, g, b), (h, s, v), match) under the cursor, or None."""
        if self._hover is None or self.frame is None:
            return None
        x, y = self._hover
        px = int(self.frame.pixels()[y, x])
        hsv = tuple(int(a) for a in D.hsv_lut()[px])
        rgb = tuple(int(a) for a in P.rgb565_to_rgb888(np.array([[px]], dtype=np.uint16))[0, 0])
        m = bool(D.mask(self.settings(), self.frame.data, self.stage())[y, x])
        return x, y, px, rgb, hsv, m

    def _pixel_text(self) -> str:
        p = self._pixel()
        if p is None:
            return NO_HOVER
        x, y, px, (r, g, b), (hh, s, v), m = p
        return "x %d  y %d  rgb565 %04x  RGB %d %d %d -> H %d S %d V %d  %s" % (
            x, y, px, r, g, b, hh, s, v, "MATCH" if m else "no match")

    def _show_hover(self):
        p = self._pixel()
        if p is None:
            self.px_xy.setText(NO_HOVER)
            self.px_xy.setFont(theme.ui_font(12))
            self.swatch.hide()
            self.px_match.hide()
            for lbl in (self.px_565, self.px_rgb, self.px_hsv):
                lbl.setText("-")
        else:
            x, y, px, (r, g, b), (hh, s, v), m = p
            self.px_xy.setFont(theme.mono_font())
            self.px_xy.setText("x %d · y %d" % (x, y))
            self.swatch.setStyleSheet("background: rgb(%d,%d,%d); border: 1px solid %s;"
                                      " border-radius: 3px;" % (r, g, b,
                                                                theme.TOKENS["border_strong"]))
            self.swatch.show()
            chip(self.px_match, "match" if m else "no match", GREEN if m else GREY)
            self.px_match.show()
            self.px_565.setText("%04x" % px)
            self.px_rgb.setText("%d %d %d" % (r, g, b))
            self.px_hsv.setText("H %d  S %d  V %d" % (hh, s, v))
        self.strip.refresh_summary()

    def _frame_link_text(self) -> str:
        f = self.frame
        if f is None:
            first = "no frame yet: press Grab frame"
        else:
            first = "frame seq %d, camera t %.1f s, %s; received %.1f s ago" % (
                f.seq, f.t_ms / 1000, f.fmt, time.monotonic() - self.frame_time)
        link = self.main.link
        if link is None:
            return first + "\nnot connected"
        c = link.counters
        last = "" if f is None else ", last %.1f s ago" % (time.monotonic() - self.frame_time)
        return first + "\n%d baud, %.1f kB/s on the wire. frames ok %d, bad %d (CRC/truncated)%s" % (
            link.baud, c.rate() / 1000, c.frames_ok, c.frames_bad, last)

    def _show_frame_link(self):
        f = self.frame
        if f is None:
            for k in ("seq", "camera t", "format"):
                self.fl[k].setText("-")
            self.fl["received"].setText("no frame yet")
        else:
            self.fl["seq"].setText(str(f.seq))
            self.fl["camera t"].setText("%.1f s" % (f.t_ms / 1000))
            self.fl["format"].setText(f.fmt)
            self.fl["received"].setText("%.1f s ago" % (time.monotonic() - self.frame_time))
        link = self.main.link
        if link is None:
            self.fl["on the wire"].setText("not connected")
            self.fl["frames"].setText("-")
        else:
            c = link.counters
            self.fl["on the wire"].setText("%d baud · %.1f kB/s" % (link.baud, c.rate() / 1000))
            self.fl["frames"].setText("ok %d · bad %d (CRC/trunc.)" % (c.frames_ok, c.frames_bad))

    def _settings_state(self):
        """(tone colour, shown line, copied line) for the camera's settings state."""
        st = self.cfg.get("settings")
        if not self.cfg:
            return GREY, "no camera", "Camera settings: no camera"
        if st == "saved":
            return GREEN, "Saved in flash — a power cycle keeps them", \
                "Camera settings: SAVED in flash"
        if st == "defaults":
            return GREY, "Factory defaults (nothing saved)", \
                "Camera settings: factory defaults (nothing saved)"
        return AMBER, "Modified, NOT saved — a power cycle loses them. Press Save to camera " \
                      "when done.", "Camera settings: MODIFIED, NOT SAVED"

    def _show_settings(self):
        colour, shown, _ = self._settings_state()
        chip(self.settings_lbl, shown, colour)
        v = self.cfg
        if v:
            st = int(v.get("stage", 0))
            self.set_stage_v.setText(STAGE_NAMES[st] if 0 <= st < len(STAGE_NAMES) else str(st))
            self.set_preset_v.setText(str(v.get("preset", "?")))
            self.set_order_v.setText("big-endian" if v.get("be") == 1 else "LITTLE-endian")
        else:
            for lbl in (self.set_stage_v, self.set_preset_v, self.set_order_v):
                lbl.setText("-")

    def _settings_text(self) -> str:
        _, _, first = self._settings_state()
        if not self.cfg:
            return first
        return first + "\ndetector stage %s; preset %s; byte order %s" % (
            " · ".join(self.set_stage_v.text().split(None, 1)), self.set_preset_v.text(),
            self.set_order_v.text())

    def _strip_summary(self) -> str:
        parts = []
        p = self._pixel()
        if p is not None:
            x, y, _, _, (hh, s, v), _ = p
            parts.append("x %d y %d · H %d S %d V %d" % (x, y, hh, s, v))
        if self.frame is not None:
            parts.append(self._det_summary)
            parts.append("seq %d" % self.frame.seq)
        link = self.main.link
        if link is not None:
            parts.append("%.1f kB/s" % (link.counters.rate() / 1000))
        st = self.cfg.get("settings")
        if self.cfg:
            parts.append({"saved": "saved in flash", "defaults": "factory defaults"}.get(
                st, "NOT saved"))
        return " · ".join(parts) or "no frame yet"

    def _refresh_summaries(self):
        sec = self.sections
        if self._region_stats is not None:
            hue, spread, frac = self._region_stats
            sec["region"].set_summary("hue %d ±%d · %.0f %%" % (hue, spread, 100 * frac))
        else:
            sec["region"].set_summary("")
        v = self.cfg
        if v and int(v.get("stage_eff", 0)) >= 1:
            sec["colour"].set_summary("stage %d · colour table %s" % (
                int(v.get("stage_eff", 0)), v.get("lut_id", "?")))
        else:
            hsv = "H %d° ±%d° · S≥%d · V≥%d" % tuple(self.p[k].local
                                                    for k in ("hue", "tol", "sat", "val"))
            sec["colour"].set_summary(("%s · " % v["preset"] if v.get("preset") else "") + hsv)
        sec["blobs"].set_summary("area ≥ %d px · %d mm · FOV %.1f°" % (
            self.p["area"].local, self.p["mm"].local, self.p["fov"].local))
        if v:
            sec["sensor"].set_summary("WB %s · %s-endian%s" % (
                P.wb_text(*P.wb_of(v)), "big" if v.get("be") == 1 else "LITTLE",
                " · turned 180°" if v.get("flip") == 1 else ""))
            sec["flash"].set_summary({"saved": "● saved", "defaults": "● defaults"}.get(
                v.get("settings"), "● unsaved"))
        else:
            sec["sensor"].set_summary("")
            sec["flash"].set_summary("")

    # ---- drawing ---------------------------------------------------------------

    def _update_region(self):
        if self.frame is None:
            return
        x, y, w, h = self.view.region()
        x1, y1 = min(W, x + w), min(H, y + h)
        px = self.frame.pixels()[max(0, y):y1, max(0, x):x1].ravel()
        if px.size == 0:
            return
        hsv = D.hsv_lut()[px]
        col = D.sample(self.settings(), self.frame.data, x, y, w, h)
        sat, val = hsv[:, 1], hsv[:, 2]
        q = lambda a: "%d/%d/%d" % tuple(np.percentile(a, [10, 50, 90]).astype(int))
        st = self.settings()
        m = D.mask(st, self.frame.data, self.stage())[max(0, y):y1, max(0, x):x1]
        bw, bh = x1 - max(0, x), y1 - max(0, y)
        n, frac = int(m.sum()), m.sum() / m.size
        self.region_text = (
            "%dx%d at (%d,%d): hue %d +/-%d (camera's sampler)\n"
            "sat p10/50/90 %s  val p10/50/90 %s\n"
            "matching now: %d of %d px (%.0f%%)"
            % (bw, bh, x, y, col.hue, col.hue_spread, q(sat), q(val), n, m.size, 100.0 * frac))
        self.reg_box.setText("%d×%d at (%d, %d)" % (bw, bh, x, y))
        self.reg_hue.setText("%d ± %d" % (col.hue, col.hue_spread))
        self.reg_sat.setText(q(sat).replace("/", " / "))
        self.reg_val.setText(q(val).replace("/", " / "))
        self.reg_match.setText("%d / %d px (%.0f%%)" % (n, m.size, 100.0 * frac))
        self.reg_bar.set_fraction(frac)
        self._region_stats = (col.hue, col.hue_spread, frac)
        coloured = hsv[sat >= 30, 0]
        counts, _ = np.histogram(coloured, bins=36, range=(0, 360))
        self.bars.setOpts(height=counts)
        self.huebar.set_mark(col.hue)
        self._refresh_summaries()

    def _redraw(self):
        st = self.settings()
        self.huebar.set_window(st.hue, st.tol)
        self.win_lo.setPos((st.hue - st.tol) % 360)
        self.win_hi.setPos((st.hue + st.tol) % 360)
        self._refresh_summaries()
        self._show_frame_link()
        if self.frame is None:
            self._det_summary = ""
            self._show_detection(None, None)
            self.strip.refresh_summary()
            return
        rgb = self.frame.rgb()
        mask = D.mask(st, self.frame.data, self.stage())
        mode = self.overlay_mode.currentIndex()
        if mode == 1:
            self.view.set_image(np.repeat(mask[..., None] * 255, 3, axis=2).astype(np.uint8))
            self.view.set_overlay(None)
        else:
            self.view.set_image(rgb)
            if mode == 0:
                ov = np.zeros((H, W, 4), dtype=np.uint8)
                ov[mask == 1] = TINT
                self.view.set_overlay(ov)
            else:
                self.view.set_overlay(None)

        r = D.pipeline(st, self.frame.data, self.stage()).r
        boxes = []
        if self.show_boxes.isChecked():
            for k in range(min(r.n_report, D.BLOB_MAX_REPORT)):
                b = r.b[k]
                boxes.append((b.x0, b.y0, b.x1 - b.x0 + 1, b.y1 - b.y0 + 1,
                              BOX_COLOURS[0 if k == 0 else 1],
                              "%d%s" % (k + 1, " clipped" if b.clipped else "")))
        self.view.set_boxes(boxes)

        cam = []
        row = self.frame_row
        if row and row["ok"]:
            cam.append((row["cx_px"], row["cy_px"]))
            f = (W / 2) / math.tan(math.radians(self.cfg.get("fov", 54.0)) / 2)
            for k in range(2, int(min(row["blobs"], 4)) + 1):
                az, el = row["b%d_az" % k], row["b%d_el" % k]
                cam.append((W / 2 + f * math.tan(math.radians(az)),
                            H / 2 - f * math.tan(math.radians(el))))
        self.view.set_marks(cam)
        self._show_detection(r, row)
        self._show_hover()

    def _show_detection(self, r, row):
        cells = self.det_cells
        if r is None:
            for k in ("pc", "cam"):
                chip(cells[k][0], "-", GREY)
                for c in cells[k][1:]:
                    c.setText("")
            self.det_badge.hide()
            self.det_note.setText("no frame yet: press Grab frame")
            self.det_note.show()
            self.compare_text = ""
            return
        b0 = r.b[0]
        pc = (bool(r.found), int(r.n_blobs), int(b0.area), int(r.matched))
        self.compare_text = "PC (controls): %s  blobs %d  area %d  matched %d" % (
            "TARGET" if pc[0] else "NO TARGET", *pc[1:])
        self._fill_det(cells["pc"], pc)
        if row is None:
            self.compare_text += "\ncamera (its row): not received for this frame"
            chip(cells["cam"][0], "-", GREY)
            for c in cells["cam"][1:]:
                c.setText("")
            self.det_note.setText("camera row not received for this frame")
            self.det_note.show()
            chip(self.det_badge, "no camera row", GREY)
            agree = None
        else:
            cam = (bool(row["ok"]), int(row["blobs"]), int(row["area_px"]),
                   int(row["matched_px"]))
            self.compare_text += "\ncamera (its row): %s  blobs %d  area %d  matched %d" % (
                "TARGET" if cam[0] else "NO TARGET", *cam[1:])
            self._fill_det(cells["cam"], cam)
            self.det_note.hide()
            agree = cam == pc
            chip(self.det_badge, "✓ PC = camera" if agree else "PC ≠ camera",
                 GREEN if agree else AMBER)
        self.det_badge.show()
        verdict = "TARGET" if pc[0] else "NO TARGET"
        self._det_summary = "%s %d blobs%s" % (
            verdict, pc[1], "" if agree is None else
            (", PC = camera" if agree else ", PC ≠ camera"))

    @staticmethod
    def _fill_det(cells, vals):
        found, blobs, area, matched = vals
        chip(cells[0], "TARGET" if found else "NO TARGET", GREEN if found else GREY)
        for c, v in zip(cells[1:], (blobs, area, matched)):
            c.setText(str(v))
