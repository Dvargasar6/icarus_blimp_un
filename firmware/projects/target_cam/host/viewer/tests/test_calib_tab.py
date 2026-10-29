"""The Image & calibration screen (docs/GUI_REDESIGN.md §6.1), driven without a
camera: a fake scene frame and a #!CFG are handed to it the way the main
window hands them over, and what it shows and copies is checked."""
import time

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from tcview import detector as D
from tcview import protocol as P
from tcview.__main__ import MainWindow
from tcview.fake_cam import DEFAULT, Scene
from tcview.ui import theme


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


@pytest.fixture
def win(app):
    w = MainWindow()
    w.resize(1280, 720)
    w.show()
    w.tabs.setCurrentWidget(w.calib)
    pump(app)
    yield w
    w.close()


def pump(app, s=0.2):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def frame(seq=7, t_ms=12345) -> P.Frame:
    px = Scene(seed=3).render(0.0)          # balloon centred at (115, 60)
    return P.Frame(seq, t_ms, 160, 120, "rgb565be", px.astype(">u2").tobytes())


def cfg(**kw) -> dict:
    v = dict(DEFAULT, w=160, h=120, image=0, settings="defaults",
             lut="ok" if D.models().lut else "none", lut_id=D.models().lut_id)
    v.update(kw)
    v.setdefault("stage_eff", v["stage"])
    return v


def test_pixel_readout_and_copy(app, win):
    cal = win.calib
    assert cal._pixel_text() == "hover over the image for a pixel's colour"
    cal.on_cfg(cfg())
    cal.on_frame(frame())
    cal.view.hovered.emit(115, 60)
    t = cal._pixel_text()
    assert t.startswith("x 115  y 60  rgb565 ") and t.endswith("MATCH")
    assert "RGB " in t and " -> H " in t
    assert cal.px_xy.text() == "x 115 · y 60" and cal.px_match.text() == "match"
    cal.view.hovered.emit(10, 10)                       # the grey wall
    assert cal._pixel_text().endswith("no match")
    cal.view.left.emit()
    assert cal.px_565.text() == "-"


def test_region_card_and_summary(app, win):
    cal = win.calib
    cal.on_cfg(cfg())
    cal.on_frame(frame())
    cal.view.set_region(108, 53, 16, 16)
    lines = cal.region_text.split("\n")
    assert lines[0].startswith("16x16 at (108,53): hue ") and \
        lines[0].endswith("(camera's sampler)")
    assert lines[1].startswith("sat p10/50/90 ") and "  val p10/50/90 " in lines[1]
    assert lines[2].startswith("matching now: ") and " of 256 px (" in lines[2]
    assert cal.reg_box.text() == "16×16 at (108, 53)"
    assert cal.sections["region"].summary().startswith("hue ")
    assert cal.sections["region"].summary().endswith("%")


def test_detection_card_without_and_with_the_camera_row(app, win):
    cal = win.calib
    cal.on_cfg(cfg())
    cal.on_frame(frame(t_ms=500))
    assert cal.compare_text.startswith("PC (controls): TARGET  blobs ")
    assert cal.compare_text.endswith("\ncamera (its row): not received for this frame")
    assert cal.det_badge.text() == "no camera row"
    r = D.pipeline(cal.settings(), cal.frame.data, 0).r
    row = {"t_ms": 500, "ok": 1, "blobs": r.n_blobs, "area_px": r.b[0].area,
           "matched_px": r.matched, "cx_px": 115.0, "cy_px": 60.0}
    cal.on_row(P.Row(row, ""))
    assert cal.det_badge.text() == "✓ PC = camera"
    assert "\ncamera (its row): TARGET  blobs %d  area %d  matched %d" % (
        r.n_blobs, r.b[0].area, r.matched) in cal.compare_text
    assert cal.view.marks == [(115.0, 60.0)]


def test_settings_card(app, win):
    cal = win.calib
    assert cal._settings_text() == "Camera settings: no camera"
    cal.on_cfg(cfg(settings="saved", preset="red"))
    assert cal._settings_text() == ("Camera settings: SAVED in flash\n"
                                    "detector stage 0 · HSV window; preset red; "
                                    "byte order big-endian")
    assert cal.sections["flash"].summary() == "● saved"
    cal.on_cfg(cfg(settings="modified"))
    assert cal._settings_text().startswith("Camera settings: MODIFIED, NOT SAVED")
    assert cal.sections["flash"].summary() == "● unsaved"


def test_stage_banner_and_faded_hsv_controls(app, win):
    cal = win.calib
    assert cal.stage_lbl.isHidden()
    cal.on_cfg(cfg(stage=0))
    assert cal.stage_lbl.isHidden() and not cal._dim.isEnabled()
    assert cal.sections["colour"].summary() == "red · H 0° ±12° · S≥110 · V≥50"
    if D.models().lut:
        cal.on_cfg(cfg(stage=1))
        assert cal.stage_lbl.tone() == "info" and cal._dim.isEnabled()
        assert cal.sections["colour"].summary().startswith("stage 1 · colour table ")
    cal.on_cfg(cfg(stage=3, stage_eff=1))
    assert "requested, running 1" in cal.stage_lbl.text() and cal.stage_lbl.tone() == "warn"
    cal.on_disconnected()
    assert cal.stage_lbl.isHidden() and not cal._dim.isEnabled()
    assert cal._settings_text() == "Camera settings: no camera"


def test_white_balance_controls(app, win):
    """The segment and the gains follow the camera; the fixed-gain controls
    wait for firmware that reports wb=, and a refused white card says why."""
    cal = win.calib
    assert not cal.wbcal_btn.isEnabled() and cal.wb_lbl.isHidden()
    cal.on_cfg(cfg(awb=0))                                  # older firmware
    assert cal.wb_seg.value() == "frozen" and not cal.wbcal_btn.isEnabled()
    assert cal.wb_lbl.text().startswith("Frozen: gains not known")
    cal.on_cfg(cfg(awb=2, wb="fixed", wb_gain="97,64,58", wb_fixed="97,64,58"))
    assert cal.wb_seg.value() == "fixed" and cal.wbcal_btn.isEnabled()
    assert [sp.value() for sp in cal.wb_spin] == [97, 64, 58]
    assert cal.wb_lbl.text().startswith("Fixed gains 97,64,58")
    assert "WB fixed 97,64,58" in cal.sections["sensor"].summary()
    # A gain typed but not sent survives a #!CFG with the same stored gains.
    cal.wb_spin[0].setValue(120)
    cal.on_cfg(cfg(awb=1, wb="auto", wb_gain="0,0,0", wb_fixed="97,64,58"))
    assert cal.wb_spin[0].value() == 120 and cal.wb_seg.value() == "auto"
    cal.on_wbcal(P.WbCal("refused", {"reason": "dark", "R": 12, "G": 20, "B": 9}))
    assert cal.wb_lbl.text().startswith("White card REFUSED: the centre is too dark")
    cal.on_disconnected()
    assert cal.wb_lbl.isHidden() and not cal.wb_set_btn.isEnabled()


def test_frame_and_link_card(app, win):
    cal = win.calib
    assert cal._frame_link_text() == "no frame yet: press Grab frame\nnot connected"
    cal.on_frame(frame(seq=543, t_ms=81300))
    t = cal._frame_link_text()
    assert t.startswith("frame seq 543, camera t 81.3 s, rgb565be; received ")
    assert cal.fl["seq"].text() == "543" and cal.fl["on the wire"].text() == "not connected"


def test_collapsing_the_strip_gives_the_image_the_height(app, win):
    cal = win.calib
    h0 = cal.view.height()
    cal.strip.set_expanded(False)
    pump(app)
    assert cal.view.height() > h0
    assert cal.strip_scroll.height() <= cal.strip.header.sizeHint().height() + 12
    cal.strip.set_expanded(True)
    pump(app)
    assert abs(cal.view.height() - h0) <= 2


def test_zoom_buttons_and_view_stay_in_step(app, win):
    cal = win.calib
    cal.zoom_seg.set_value("3")
    assert cal.view.zoom() == "3"
    cal.view.set_zoom("fit")
    assert cal.zoom_seg.value() == "fit"


def test_image_keeps_most_of_a_small_screen(app, win):
    """1024x600 with the console open is the "still usable" smoke size (§2):
    the image keeps more height than the strip, and at least 1:1."""
    win.resize(1024, 600)
    pump(app)
    cal = win.calib
    assert cal.view.height() > cal.strip_scroll.height()
    assert cal.view.width() >= 400 and cal.view.scale >= 1


def _wheel(w, steps=3):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    p = QPointF(w.width() / 2, w.height() / 2)
    e = QWheelEvent(p, QPointF(w.mapToGlobal(p.toPoint())), QPoint(), QPoint(0, 120 * steps),
                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(w, e)
    return e


def test_the_wheel_does_not_change_a_setting(app, win):
    """A wheel turned over the colour test must not move the calibration:
    the event is left for the scroll area, and nothing is sent."""
    cal = win.calib
    sent = []
    win.send = lambda cmd: sent.append(cmd) or True
    for key in ("hue", "tol", "sat", "val", "area", "mm", "fov"):
        pc = cal.p[key]
        before = pc.local
        for w in filter(None, (pc.slider, pc.spin)):
            assert not _wheel(w).isAccepted()
            assert w.focusPolicy() == Qt.FocusPolicy.StrongFocus
        pump(app, 0.05)
        assert pc.local == before and not pc.dragging()
    pump(app, 0.5)                          # past the 400 ms commit debounce
    assert sent == []


def test_grab_frame_sends_g_and_stays_pressed_briefly(app, win):
    from tcview import calib_tab
    cal = win.calib
    sent = []
    win.send = lambda cmd: sent.append(cmd) or True
    cal.grab_btn.click()
    assert sent == ["g"] and cal.grab_btn.isDown()
    pump(app, calib_tab.GRAB_HOLD_MS / 1000 + 0.15)
    assert not cal.grab_btn.isDown()
