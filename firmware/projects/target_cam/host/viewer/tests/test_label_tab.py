"""Label frames (docs/GUI_REDESIGN.md §6.3): a stroke must land on the pixel
under the cursor at every zoom and scroll position, since a label is only as
good as the pixels it names; and the copied texts keep their old wording."""
import re

import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from tcview import labels as L
from tcview.label_tab import DONE_ROLE, LabelTab
from tcview.ui import theme
from tcview.ui.widgets import ZOOMS
from test_detector import frame_with_disc, red
from test_labels import make_set, save_frame
from test_pixel_view import mouse

LEFT, RIGHT = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton
PRESS, MOVE, RELEASE = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                        QEvent.Type.MouseButtonRelease)


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


@pytest.fixture
def tab(app, tmp_path):
    make_set(tmp_path)
    (tmp_path / "orange_s3").mkdir()
    data, _ = frame_with_disc(80, 60, 10)
    save_frame(tmp_path / "orange_s3", "bal_orange_150cm", data, red())
    t = LabelTab(tmp_path)
    t.resize(1300, 800)
    t.show()
    QTest.qWaitForWindowExposed(t)
    t.activateWindow()
    QTest.qWaitForWindowActive(t)
    app.processEvents()
    t.step(1)                           # "not_labelled": nothing painted yet
    assert t.sf.stem.name == "not_labelled" and not t.lab.any()
    yield t
    t.dirty = False                     # nothing is written on close
    t.close()


def drag(tab, a, b, button=LEFT, via=()):
    v = tab.view
    mouse(v, PRESS, a, button)
    for p in tuple(via) + (b,):
        mouse(v, MOVE, p, button)
    mouse(v, RELEASE, b, button)


def zoom_to_corner(app, tab, zoom):
    tab.view.set_zoom(zoom)
    app.processEvents()
    if zoom != "fit":
        for sb in (tab.view.horizontalScrollBar(), tab.view.verticalScrollBar()):
            assert sb.maximum() > 0
            sb.setValue(sb.maximum())
        app.processEvents()


def visible_pixel(tab):
    """The image pixel at the middle of what the viewport shows."""
    v = tab.view
    c = QPointF(v.viewport().width() / 2 + v.horizontalScrollBar().value(),
                v.viewport().height() / 2 + v.verticalScrollBar().value())
    return v.pixel_at(c)


@pytest.mark.parametrize("zoom", ZOOMS)
def test_a_brush_click_paints_the_pixel_under_the_cursor(app, tab, zoom):
    tab.tool_seg.set_value("brush")
    tab.radius.setValue(0)
    zoom_to_corner(app, tab, zoom)
    x, y = visible_pixel(tab)
    mouse(tab.view, PRESS, (x, y), LEFT)
    mouse(tab.view, RELEASE, (x, y), LEFT)
    assert list(zip(*np.nonzero(tab.lab))) == [(y, x)]
    assert tab.lab[y, x] == L.BALLOON and tab.dirty


@pytest.mark.parametrize("zoom", ZOOMS)
def test_a_shape_drag_previews_then_fills_the_box(app, tab, zoom):
    tab.tool_seg.set_value("rect")
    zoom_to_corner(app, tab, zoom)
    x, y = visible_pixel(tab)
    v = tab.view
    mouse(v, PRESS, (x - 3, y - 2), LEFT)
    mouse(v, MOVE, (x + 3, y + 2), LEFT)
    # While dragging: a dashed preview through the pixel centres, nothing painted.
    assert v._preview == ("rect", x - 2.5, y - 1.5, 6.0, 4.0)
    assert not tab.lab.any()
    mouse(v, RELEASE, (x + 3, y + 2), LEFT)
    assert v._preview is None
    ys, xs = np.nonzero(tab.lab == L.BALLOON)
    assert (xs.min(), xs.max(), ys.min(), ys.max()) == (x - 3, x + 3, y - 2, y + 2)
    tab.do_undo()
    assert not tab.lab.any()


def test_a_brush_drag_leaves_no_gaps_and_the_right_button_erases(app, tab):
    tab.tool_seg.set_value("brush")
    tab.radius.setValue(0)
    drag(tab, (10, 10), (40, 10))                 # one move event: the gap is filled
    # As in the old tab, the dabs start after the press point: at radius 0
    # the pixel under the press is left out (a larger radius covers it).
    assert tab.lab[10, 11:41].tolist() == [L.BALLOON] * 30
    assert (tab.lab != 0).sum() == 30
    drag(tab, (20, 10), (30, 10), RIGHT)
    assert tab.lab[10, 21:31].tolist() == [L.UNLABELLED] * 10
    assert (tab.lab == L.BALLOON).sum() == 20
    # One stroke is one undo step.
    tab.do_undo()
    assert (tab.lab == L.BALLOON).sum() == 30


def test_a_shape_click_paints_nothing(app, tab):
    tab.tool_seg.set_value("ellipse")
    mouse(tab.view, PRESS, (50, 50), LEFT)
    mouse(tab.view, RELEASE, (50, 50), LEFT)
    assert not tab.lab.any() and not tab.undo and not tab.dirty


def test_a_wobble_under_the_drag_distance_is_a_click(app, tab):
    tab.tool_seg.set_value("brush")
    tab.radius.setValue(0)
    v = tab.view
    p = v.to_widget(QPointF(30.5, 30.5))
    mouse(v, PRESS, (30, 30), LEFT)
    # Less than the platform's drag distance, in widget pixels.
    small = (QApplication.startDragDistance() - 1) / v.scale
    mouse(v, MOVE, (30.5 + small, 30.5), LEFT)
    mouse(v, RELEASE, (30.5 + small, 30.5), LEFT)
    assert list(zip(*np.nonzero(tab.lab))) == [(30, 30)]
    assert p == v.to_widget(QPointF(30.5, 30.5))


def test_the_copied_texts_keep_the_old_wording(app, tab):
    tab.step(-1)                                    # disc_and_square: labelled
    mouse(tab.view, MOVE, (60, 60))
    assert re.fullmatch(r"\( 60, 60\)  rgb +\d+ +\d+ +\d+  balloon",
                        tab.pixel_card.copy_text())
    assert tab.px_cls.text() == "balloon"
    s = tab.labels_card.copy_text().split("\n")
    assert s[0] == "disc_and_square   saved"
    assert re.fullmatch(r"balloon +\d+ px  background +\d+ px  unlabelled +\d+ px \(\d+ %\)", s[1])
    assert s[2].startswith("balloons (labelled objects): 1  [")
    d = tab.det_card.copy_text().split("\n")
    assert d[0] == "detector, stage 0 (hue 0 +/-12, sat >= 110, val >= 50): 2 blob(s) reported"
    assert d[1] == "vs labels: TP 1  FN 0  FP 1  split 0  blob 1 = balloon"
    assert re.fullmatch(r"pixels: precision [\d.]+ %  recall [\d.]+ %", d[2])
    # The strip copies all three, and the cards show the new wording.
    assert tab.strip.copy_text().count("\n\n") == 2
    assert tab.det_test.text() == "hue 0 ±12 · sat ≥ 110 · val ≥ 50"
    assert tab.det_blobs.text() == "2 blobs reported · blob 1 = balloon"
    assert tab.tiles["TP"].value.text() == "1" and tab.tiles["TP"]._tone == "ok"
    mouse(tab.view, MOVE, (-5, -5))                 # off the image
    assert tab.pixel_card.copy_text() == "hover over the image"


def test_the_summaries_say_what_a_folded_panel_holds(app, tab):
    assert tab.sections["paint"].summary() == "balloon · ellipse · r 2 px"
    assert tab.view.tool_text == "balloon · ellipse"
    assert tab.sections["show"].summary() == "labels 45 % · boxes on · stage 0"
    assert tab.sections["frames"].summary() == "3 · 2 unlabelled"
    tab.step(-1)
    tab.strip.set_expanded(False)
    assert re.fullmatch(r"balloon \d+ px · background \d+ px · unlabelled \d+ · "
                        r"TP 1 FN 0 FP 1 split 0 · precision [\d.]+ % · recall [\d.]+ %",
                        tab.strip.summary())


def test_saving_ticks_the_frame_and_the_pill_follows(app, tab):
    assert (tab.pill.tone(), tab.pill.text()) == ("neutral", "not labelled")
    assert tab.where_lbl.text() == "%s/ · frame 2 of 3" % tab.folder.name
    tab.fill_rest()
    assert (tab.pill.tone(), tab.pill.text()) == ("warn", "not saved")
    tab.view.canvas.setFocus()
    QTest.keyClick(tab.view.canvas, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert not tab.dirty and (tab.pill.tone(), tab.pill.text()) == ("ok", "saved")
    assert tab.listw.item(1).data(DONE_ROLE) is True
    assert tab.sections["frames"].summary() == "3 · 1 unlabelled"
    tab.step(1)
    assert tab.sf.stem.parent.name == "orange_s3"
    assert tab.where_lbl.text() == "%s/orange_s3/ · frame 3 of 3" % tab.folder.name


def test_the_filter_hides_frames_and_stepping_skips_them(app, tab):
    tab.filter.setText("ORANGE")
    assert [tab.listw.isRowHidden(r) for r in range(3)] == [True, True, False]
    assert tab.sections["frames"].summary() == "3 · 2 unlabelled · 1 shown"
    tab.step(1)
    assert tab.sf.stem.name == "bal_orange_150cm"
    tab.step(-1)                                    # nothing shown before it: stay
    assert tab.sf.stem.name == "bal_orange_150cm"
    tab.filter.clear()
    tab.step(-1)
    assert tab.sf.stem.name == "not_labelled"


def test_keys_choose_class_and_shape_but_not_while_typing(app, tab):
    tab.view.canvas.setFocus()
    for key, cls in ((Qt.Key.Key_2, L.BACKGROUND), (Qt.Key.Key_0, L.UNLABELLED),
                     (Qt.Key.Key_1, L.BALLOON)):
        QTest.keyClick(tab.view.canvas, key)
        assert tab.cls_seg.value() == cls
    for key, tool in ((Qt.Key.Key_B, "brush"), (Qt.Key.Key_R, "rect"), (Qt.Key.Key_E, "ellipse")):
        QTest.keyClick(tab.view.canvas, key)
        assert tab.tool_seg.value() == tool
    QTest.keyClick(tab.view.canvas, Qt.Key.Key_Right)
    assert tab.sf.stem.name == "bal_orange_150cm"
    QTest.keyClick(tab.view.canvas, Qt.Key.Key_Left)
    QTest.keyClick(tab.view.canvas, Qt.Key.Key_PageUp)
    assert tab.sf.stem.name == "disc_and_square"
    # In the filter the keys type.
    tab.filter.setFocus()
    QTest.keyClicks(tab.filter, "2b")
    assert tab.filter.text() == "2b"
    assert tab.cls_seg.value() == L.BALLOON and tab.tool_seg.value() == "ellipse"


def test_detector_boxes_and_balloon_boxes_are_drawn(app, tab):
    tab.step(-1)
    det = [b for b in tab.view.boxes if len(b) == 6]
    bal = [b for b in tab.view.boxes if len(b) == 7 and b[6] == "dot"]
    assert [b[5] for b in det] == ["1", "2"] and len(bal) == 1
    tab.show_det.setChecked(False)
    assert [b for b in tab.view.boxes if len(b) == 6] == []
    assert tab.sections["show"].summary() == "labels 45 % · boxes off · stage 0"


def test_it_fits_a_1024x600_screen(app, tab):
    tab.resize(1024, 560)                           # the window less its tab bar
    app.processEvents()
    assert tab.minimumSizeHint().width() <= 1024
    assert tab.split.widget(1).width() >= 320       # the inspector
    # The image keeps most of the pane with the strip open.
    tab.vsplit.size_strip()
    app.processEvents()
    assert tab.view.height() >= 0.6 * tab.vsplit.height()
    assert tab.view.scale >= 2
