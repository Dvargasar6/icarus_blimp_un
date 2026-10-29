"""Every shared widget on one page, themed; for eyeballing the theme.

  python -m tcview.ui.demo [--shot FILE]

The values are examples from real sessions (docs/GUI_REDESIGN.md §7)."""
from __future__ import annotations

import argparse
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QComboBox, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
                             QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from . import theme
from .widgets import (Banner, CollapsibleSection, CopyButton, GripSplitter, ReadoutCard,
                      ReadoutStrip, SegmentedButtons, StatusPill, ToggleSwitch)


def caption(text):
    lbl = QLabel(text)
    lbl.setFont(theme.caption_font())
    theme.set_prop(lbl, "muted", True)
    return lbl


def build() -> QMainWindow:
    win = QMainWindow()
    win.setWindowTitle("tcview widgets")
    split = GripSplitter(Qt.Orientation.Horizontal)

    # left: controls, pills, banners, the readout strip
    left = QWidget()
    lay = QVBoxLayout(left)
    lay.setContentsMargins(16, 16, 16, 16)
    lay.setSpacing(12)

    lay.addWidget(caption("Buttons"))
    row = QHBoxLayout()
    b = QPushButton("Grab frame")
    theme.set_role(b, "primary")
    row.addWidget(b)
    row.addWidget(QPushButton("Reload saved"))
    d = QPushButton("Factory defaults")
    theme.set_role(d, "danger")
    row.addWidget(d)
    dis = QPushButton("Disabled")
    dis.setEnabled(False)
    row.addWidget(dis)
    row.addWidget(CopyButton(lambda: "copied text", "Copy all"))
    row.addWidget(CopyButton(lambda: "copied text"))
    row.addStretch(1)
    lay.addLayout(row)

    lay.addWidget(caption("Segmented, switches, inputs"))
    row = QHBoxLayout()
    row.addWidget(SegmentedButtons([("fit", "Fit"), ("2", "2×"), ("3", "3×"), ("4", "4×")]))
    row.addWidget(SegmentedButtons([("b", "Balloon", "1"), ("g", "Background", "2"),
                                    ("e", "Erase", "0")]))
    row.addWidget(ToggleSwitch("Live", True))
    row.addWidget(ToggleSwitch("Record"))
    row.addStretch(1)
    lay.addLayout(row)
    row = QHBoxLayout()
    combo = QComboBox()
    combo.addItems(["/dev/ttyUSB0 — CP2102 USB to UART", "/dev/ttyACM0 — USB JTAG/serial"])
    row.addWidget(combo, 1)
    spin = QSpinBox()
    spin.setRange(0, 360)
    spin.setValue(341)
    spin.setSuffix(" °")
    row.addWidget(spin)
    sl = QSlider(Qt.Orientation.Horizontal)
    sl.setRange(0, 255)
    sl.setValue(110)
    row.addWidget(sl, 1)
    ed = QLineEdit()
    ed.setPlaceholderText("any camera command, e.g. ?  m  H350")
    row.addWidget(ed, 1)
    lay.addLayout(row)

    lay.addWidget(caption("Pills and banners"))
    row = QHBoxLayout()
    for tone, text in (("ok", "Connected · /dev/ttyUSB0 · 460800 baud"), ("warn", "Connecting…"),
                       ("err", "Link lost · /dev/ttyUSB0"), ("info", "stage 1"),
                       ("neutral", "Disconnected")):
        row.addWidget(StatusPill(tone, text))
    row.addStretch(1)
    lay.addLayout(row)
    lay.addWidget(Banner("ok", "FLIGHT-IDENTICAL: image off, collection off, 115200 baud, "
                               "streaming on. Settings: saved."))
    lay.addWidget(Banner("warn", "NOT flight-identical: image on; 460800 baud"))
    lay.addWidget(Banner("err", "cannot open /dev/ttyUSB0: [Errno 13] Permission denied — "
                                "add yourself to the dialout group"))
    lay.addWidget(Banner("info", "Stage 1: the colour table decides the pixels; hue, tol, "
                                 "sat, val and 'c' have no effect until stage 0."))

    strip = ReadoutStrip("Camera readouts")
    c = strip.add_card(ReadoutCard("Pixel", "under the cursor"))
    c.add_text("x 157  y 94  rgb565 10e3  RGB 16 28 24 -> H 160 S 109 V 28  no match")
    c = strip.add_card(ReadoutCard("Detection"))
    c.add_kv("PC (controls)", "TARGET  blobs 3  area 374  matched 653")
    c.add_kv("camera (its row)", "TARGET  blobs 3  area 374  matched 653")
    c = strip.add_card(ReadoutCard("Frame & link"))
    c.add_kv("frame", "seq 543, camera t 139.6 s, rgb565be")
    c.add_kv("link", "460800 baud, 1.8 kB/s")
    c.add_bar(0.42, "label_balloon")
    strip.set_summary_source(lambda: "TARGET · blobs 3 · seq 543 · 460800 baud")
    lay.addWidget(strip)
    strip2 = ReadoutStrip("Collapsed strip", expanded=False)
    strip2.add_card(ReadoutCard("Hidden"))
    strip2.set_summary_source(lambda: "TARGET · blobs 3 · seq 543 · 460800 baud · frames ok 11")
    lay.addWidget(strip2)
    log = QPlainTextEdit()
    log.setObjectName("Console")
    theme.mono(log)
    log.setPlainText("> k\n#!ACK k\n#!CFG stage=1 hue=341 tol=27 sat=110 val=50 image=0\n"
                     "# [viewer] port now at 115200 baud")
    lay.addWidget(log, 1)
    split.addWidget(left)

    # right: an inspector of collapsible sections
    insp = QScrollArea()
    insp.setWidgetResizable(True)
    inner = QWidget()
    inner.setObjectName("Panel")
    il = QVBoxLayout(inner)
    il.setContentsMargins(8, 8, 8, 8)
    il.setSpacing(4)
    head = QHBoxLayout()
    t = QLabel("Inspector")
    t.setFont(theme.ui_font(13, theme.QFont.Weight.DemiBold))
    head.addWidget(t)
    head.addStretch(1)
    sections = []
    exp, col = QPushButton("Expand all"), QPushButton("Collapse all")
    for btn in (exp, col):
        theme.set_prop(btn, "small", True)
        head.addWidget(btn)
    il.addLayout(head)
    s = CollapsibleSection("Colour test", "red · H 0° ±12° · S≥110 · V≥50")
    g = QGridLayout()
    for r, (k, v) in enumerate((("hue", 341), ("tol", 27), ("sat", 110), ("val", 50))):
        g.addWidget(QLabel(k), r, 0)
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(0, 360)
        sl.setValue(v)
        g.addWidget(sl, r, 1)
        g.addWidget(StatusPill("ok", "✓ applied"), r, 2)
    s.add_layout(g)
    sections.append(s)
    il.addWidget(s)
    s = CollapsibleSection("Blobs & geometry", "area ≥ 25 px · 550 mm · FOV 54.0°", expanded=False)
    s.add_widget(QLabel("…"))
    sections.append(s)
    il.addWidget(s)
    card = ReadoutCard("Blobs table")
    card.add_table(["#", "az_deg", "el_deg", "range_mm", "notes"],
                   [[1, "1.16", "3.35", 1599, ""], [2, "-4.02", "0.88", 2210, "clipped"]])
    il.addWidget(card)
    il.addStretch(1)
    exp.clicked.connect(lambda: [x.set_expanded(True) for x in sections])
    col.clicked.connect(lambda: [x.set_expanded(False) for x in sections])
    insp.setWidget(inner)
    split.addWidget(insp)
    split.setStretchFactor(0, 3)
    split.setStretchFactor(1, 1)
    split.setSizes([900, 400])
    win.setCentralWidget(split)
    return win


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tcview.ui.demo")
    ap.add_argument("--shot", metavar="FILE", help="save a screenshot and quit")
    ap.add_argument("--size", default="1360x820", help="window size, WxH")
    args = ap.parse_args(argv)
    app = QApplication(sys.argv[:1])
    theme.apply(app)
    win = build()
    w, h = (int(v) for v in args.size.split("x"))
    win.resize(w, h)
    win.show()
    if args.shot:
        def shot():
            win.grab().save(args.shot)
            app.quit()
        QTimer.singleShot(400, shot)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
