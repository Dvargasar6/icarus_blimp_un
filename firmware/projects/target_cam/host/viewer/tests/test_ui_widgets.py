"""The theme and the shared widgets of tcview.ui (docs/GUI_REDESIGN.md §3, §4)."""
import re

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from tcview import widgets as W
from tcview.ui import theme
from tcview.ui.widgets import (Banner, CollapsibleSection, CopyButton, GripSplitter,
                               ReadoutCard, ReadoutStrip, SegmentedButtons, StatusPill,
                               ToggleSwitch)


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


def test_theme_tokens_and_tones(app):
    assert theme.TOKENS["bg"] == "#16181c" and theme.TOKENS["accent"] == "#4fc3d6"
    assert theme.tone_colours("err")["dot"] == "#f07a6a"
    assert theme.tone_colours("bogus") == theme.tone_colours("neutral")
    qss = theme.stylesheet()
    assert not re.search(r"\{[a-z_]+\}", qss)          # every {token} resolved
    assert "#16181c" in qss and theme.mono_family() in qss


def test_fonts_fall_back_to_something(app):
    assert theme.ui_family() and theme.mono_family()
    assert theme.pick_family(["No Such Font 123"], theme.QFontDatabase.SystemFont.FixedFont)
    lbl = theme.mono(QLabel("x"))
    assert lbl.property("mono") is True and lbl.font().pixelSize() == theme.MONO_PX


def test_role_property_repolishes(app):
    b = QPushButton("Go")
    theme.set_role(b, "primary")
    assert b.property("role") == "primary"


def test_chip_maps_old_colours_to_tones(app):
    lbl = QLabel()
    W.chip(lbl, "applied", W.GREEN)
    assert lbl.text() == "applied"
    assert theme.TOKENS["ok_bg"] in lbl.styleSheet()
    css = lbl.styleSheet()
    W.chip(lbl, "applied again", W.GREEN)
    assert lbl.styleSheet() == css and lbl.text() == "applied again"
    W.chip(lbl, "refused", W.RED)
    assert theme.TOKENS["err_bg"] in lbl.styleSheet()


def test_copy_button_copies_current_value(app):
    box = {"v": "first"}
    b = CopyButton(lambda: box["v"], "Copy all")
    box["v"] = "second"
    b.click()
    assert QApplication.clipboard().text() == "second"
    assert b.text() == "Copied"
    b._restore()
    assert b.text() == "Copy all"


def test_collapsible_section(app):
    s = CollapsibleSection("Colour test", "red · H 0° ±12°")
    seen = []
    s.toggled.connect(seen.append)
    s.add_widget(QLabel("body"))
    s.show()
    assert s.is_expanded() and s.body.isVisible()
    s.set_expanded(False)
    assert not s.body.isVisible() and seen == [False]
    s.set_summary("red · H 4° ±10°")
    assert s.summary() == "red · H 4° ±10°"
    assert s.summary_lbl.minimumSizeHint().width() < 40    # elides, never widens the inspector


def test_readout_card_copy_text(app):
    c = ReadoutCard("Detection")
    v = c.add_kv("PC (controls)", "TARGET  blobs 3")
    c.add_kv("camera (its row)")
    assert v.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    t = c.add_table(["#", "az_deg"], [[1, "1.16"]])
    assert t.csv() == "#,az_deg\n1,1.16"
    assert c.copy_text() == "PC (controls) TARGET  blobs 3\ncamera (its row) \n#,az_deg\n1,1.16"
    c.set_copy_source(lambda: "exact old wording")
    assert c.copy_text() == "exact old wording"


def test_readout_strip(app):
    s = ReadoutStrip("Camera readouts")
    a = s.add_card(ReadoutCard("A"))
    a.add_text("one")
    b = s.add_card(ReadoutCard("B"))
    b.add_text("two")
    assert a.objectName() == "Pane"
    assert s.copy_text() == "one\n\ntwo"
    s.set_summary_source(lambda: "TARGET · blobs 3")
    s.resize(300, 200)
    s.show()
    assert s.summary() == ""                    # only a collapsed strip summarises
    s.set_expanded(False)
    assert s.summary() == "TARGET · blobs 3" and not s.body.isVisible()


def test_readout_strip_wraps(app):
    s = ReadoutStrip("x")
    for k in range(4):
        c = s.add_card(ReadoutCard("card %d" % k))
        c.add_text("a fairly long value line %d" % k, wrap=False)
    s.resize(320, 400)
    s.show()
    app.processEvents()
    assert len(s.body.flow.lines) > 1


def test_pill_and_banner(app):
    p = StatusPill("ok", "Connected")
    assert p.tone() == "ok" and p.text() == "Connected"
    p.set("nonsense", "x")
    assert p.tone() == "neutral"
    b = Banner("warn", "NOT flight-identical: image on")
    assert b.text().startswith("NOT") and b.toolTip() == b.text()
    assert b.label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    b.set("ok", "FLIGHT-IDENTICAL")
    assert b.tone() == "ok"


def test_segmented_and_switch(app):
    s = SegmentedButtons([("fit", "Fit"), ("2", "2×"), ("b", "Balloon", "1")])
    got = []
    s.changed.connect(got.append)
    assert s.value() == "fit"
    s.buttons["2"].click()
    assert s.value() == "2" and got == ["2"]
    s.set_value("b", emit=False)
    assert s.value() == "b" and got == ["2"]
    assert not s.buttons["fit"].isChecked()
    t = ToggleSwitch("Live")
    t.click()
    assert t.isChecked()


def test_grip_splitter(app):
    sp = GripSplitter(Qt.Orientation.Vertical)
    sp.addWidget(QLabel("a"))
    sp.addWidget(QLabel("b"))
    assert sp.handleWidth() == 7
    assert type(sp.handle(1)).__name__ == "GripHandle"


def test_demo_builds(app):
    from tcview.ui import demo
    win = demo.build()
    win.resize(1024, 600)
    win.show()
    app.processEvents()
    win.close()
