"""Keyboard shortcuts (docs/GUI_REDESIGN.md §8) and the remembered layout
(§9). No camera: commands the window would send are collected instead, and
the store is the test session's own (conftest), emptied before each test."""
import time

import pytest
from PyQt6.QtCore import QByteArray, QSettings, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (QApplication, QLineEdit, QPushButton, QStackedWidget,
                             QVBoxLayout, QWidget)

from tcview.__main__ import LabelWindow, MainWindow
from tcview import labels as L
from tcview.ui import theme
from tcview.ui.keys import HintButton, add_shortcut, typing


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


def pump(app, s=0.2):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def store():
    return QSettings("icarus_blimp", "tcview")


def show(app, w, size=(1280, 720)):
    w.resize(*size)
    w.show()
    QTest.qWaitForWindowExposed(w)
    w.activateWindow()
    QTest.qWaitForWindowActive(w)
    pump(app)


@pytest.fixture
def win(app):
    w = MainWindow()
    sent = []
    w.send = lambda cmd: sent.append(cmd) or True
    w.sent = sent
    show(app, w)
    yield w
    w.close()


# ---- shortcuts ----------------------------------------------------------------------

def test_g_and_c_on_image_and_calibration(app, win):
    win.tabs.setCurrentWidget(win.calib)
    win.calib.view.setFocus()
    QTest.keyClick(win.calib.view, Qt.Key.Key_G)
    QTest.keyClick(win.calib.view, Qt.Key.Key_C)
    assert win.sent == ["g", "c"]
    # The console log is not a text field: the keys still work from there.
    win.log.setFocus()
    QTest.keyClick(win.log, Qt.Key.Key_G)
    assert win.sent == ["g", "c", "g"]


def test_keys_type_in_the_command_line(app, win):
    win.tabs.setCurrentWidget(win.calib)
    win.entry.setFocus()
    QTest.keyClicks(win.entry, "gc")
    assert win.entry.text() == "gc" and win.sent == []
    sb = win.calib.p["hue"].spin
    sb.setFocus()
    QTest.keyClick(sb, Qt.Key.Key_G)
    assert win.sent == []


def test_ctrl_l_focuses_the_command_line_from_anywhere(app, win):
    win.entry.setText("H350")
    win.tabs.setCurrentWidget(win.label)
    win.label.filter.setFocus()
    QTest.keyClick(win.label.filter, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.focusWidget() is win.entry
    assert win.entry.selectedText() == "H350"


def test_keys_follow_the_tab(app, win):
    """B is Brush on Label frames and Balloon on Blobs; G does nothing away
    from Image & calibration."""
    win.tabs.setCurrentWidget(win.label)
    pump(app, 0.05)
    win.label.tool_seg.set_value("rect")
    win.label.view.setFocus()
    QTest.keyClick(win.label.view, Qt.Key.Key_B)
    QTest.keyClick(win.label.view, Qt.Key.Key_2)
    QTest.keyClick(win.label.view, Qt.Key.Key_G)
    assert win.label.tool_seg.value() == "brush"
    assert win.label.cls_seg.value() == L.BACKGROUND
    assert win.sent == []
    win.tabs.setCurrentWidget(win.calib)
    pump(app, 0.05)
    win.calib.view.setFocus()
    QTest.keyClick(win.calib.view, Qt.Key.Key_E)       # Ellipse is a Label key only
    assert win.label.tool_seg.value() == "brush"


def test_shortcut_ignored_while_typing(app):
    """Ctrl+S is not a text field's own key, so the check in add_shortcut
    is what keeps it from firing; while_typing lets Ctrl+L through."""
    w = QWidget()
    lay = QVBoxLayout(w)
    edit, btn = QLineEdit(), QPushButton("x")
    lay.addWidget(edit)
    lay.addWidget(btn)
    hits = []
    add_shortcut(w, "Ctrl+S", lambda: hits.append("s"))
    add_shortcut(w, "Ctrl+L", lambda: hits.append("l"), while_typing=True)
    show(app, w, (200, 100))
    edit.setFocus()
    assert typing()
    QTest.keyClick(edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(edit, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier)
    assert hits == ["l"]
    btn.setFocus()
    assert not typing()
    QTest.keyClick(btn, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert hits == ["l", "s"]
    edit.setReadOnly(True)
    edit.setFocus()
    assert not typing()
    w.close()


def test_only_the_shown_page_owns_a_key(app):
    w = QStackedWidget()
    a, b = QWidget(), QWidget()
    w.addWidget(a)
    w.addWidget(b)
    hits = []
    add_shortcut(a, "B", lambda: hits.append("a"))
    add_shortcut(b, "B", lambda: hits.append("b"))
    show(app, w, (200, 100))
    QTest.keyClick(w, Qt.Key.Key_B)
    w.setCurrentWidget(b)
    QTest.keyClick(w, Qt.Key.Key_B)
    assert hits == ["a", "b"]
    w.close()


def test_buttons_show_their_keys(app, win):
    assert win.calib.grab_btn.hint == "G" and win.calib.cal_btn.hint == "C"
    plain = QPushButton(win.calib.grab_btn.text())
    plain.setFont(win.calib.grab_btn.font())
    assert win.calib.grab_btn.sizeHint().width() > plain.sizeHint().width()
    assert win.label.save_btn.hint == "Ctrl+S"
    assert [b.hint for b in win.label.cls_seg.buttons.values()] == ["1", "2", "0"]
    assert [b.hint for b in win.label.tool_seg.buttons.values()] == ["B", "R", "E"]
    assert HintButton("x").sizeHint() == QPushButton("x").sizeHint()
    win.calib.grab_btn.grab()           # paints without error


# ---- persistence ----------------------------------------------------------------------

def test_layout_round_trip(app, tmp_path):
    w = MainWindow()
    show(app, w)
    w.tabs.setCurrentWidget(w.blobs)
    w.calib.split.setSizes([700, 573])
    w.calib.sections["colour"].set_expanded(False)
    w.calib.sections["flash"].set_expanded(False)
    w.calib.zoom_seg.set_value("3")
    w.calib.strip.set_expanded(False)
    w.label.open_folder(tmp_path)
    w.label.opacity.setValue(70)
    w.label.zoom_seg.set_value("2")
    w.label.sections["show"].set_expanded(False)
    w.blobs.size_slider.setValue(104)
    w.console.set_expanded(False)
    pump(app)
    split_w = w.calib.split.sizes()[1]
    console_h = w.console.open_height()
    w.close()

    w2 = MainWindow()
    show(app, w2)
    pump(app, 0.3)
    try:
        assert w2.tabs.current_page() is w2.blobs
        w2.tabs.setCurrentWidget(w2.calib)     # a hidden page reports stale sizes
        pump(app)
        assert abs(w2.calib.split.sizes()[1] - split_w) <= 2
        assert not w2.calib.sections["colour"].is_expanded()
        assert not w2.calib.sections["flash"].is_expanded()
        assert w2.calib.sections["region"].is_expanded()
        assert w2.calib.view.zoom() == "3" and w2.calib.zoom_seg.value() == "3"
        assert not w2.calib.strip.is_expanded()
        assert w2.label.folder == tmp_path
        assert w2.label.opacity.value() == 70
        assert w2.label.view.zoom() == "2"
        assert not w2.label.sections["show"].is_expanded()
        assert w2.blobs.size_slider.value() == 104 and w2.blobs.size_lbl.text() == "104 px"
        assert not w2.console.is_expanded()
        assert w2.console.open_height() == console_h
        # Unfolding gives back the height it had.
        w2.console.set_expanded(True)
        pump(app)
        assert abs(w2.console.height() - console_h) <= 2
    finally:
        w2.close()


def test_strip_height_is_remembered(app):
    w = MainWindow()
    show(app, w, (1440, 900))
    w.tabs.setCurrentWidget(w.label)
    pump(app)
    w.label.vsplit.strip_height = 120
    pump(app)
    w.close()
    assert store().value("label/vsplit/strip_h", type=int) == 120
    w2 = MainWindow()
    show(app, w2, (1440, 900))
    try:
        assert w2.label.vsplit.strip_height == 120
        assert w2.label.vsplit.sizes()[1] == 120
    finally:
        w2.close()


def test_geometry_round_trip(app):
    w = MainWindow()
    w.restore_geometry(700, 500)
    w.show()
    pump(app)
    assert (w.width(), w.height()) == (700, 500)
    w.resize(640, 480)
    pump(app)
    w.close()
    w2 = MainWindow()
    try:
        assert w2.restore_geometry(700, 500)
        assert (w2.width(), w2.height()) == (640, 480)
    finally:
        w2.close()


def test_geometry_bigger_than_the_screen_fits_it(app):
    """Saved on a bigger screen: Qt's restoreGeometry() already cuts it to
    this one, and restore_geometry() falls back to the default if not."""
    avail = QApplication.primaryScreen().availableGeometry()
    w = MainWindow()
    w.show()
    w.resize(avail.width() + 900, avail.height() + 700)
    pump(app)
    w.close()
    w2 = MainWindow()
    try:
        w2.restore_geometry(1450, 1000)
        assert w2.width() <= avail.width() and w2.height() <= avail.height()
    finally:
        w2.close()


def test_bad_values_are_ignored(app, tmp_path):
    s = store()
    for key, v in (("window/geometry", QByteArray(b"junk")), ("window/tab", "abc"),
                   ("console/open", "maybe"), ("console/height", -5),
                   ("calib/split", "junk"), ("calib/zoom", "7"), ("calib/open/colour", "?"),
                   ("calib/vsplit/strip_open", 3.5), ("label/opacity", 999),
                   ("label/folder", str(tmp_path / "gone")), ("label/zoom", ""),
                   ("blobs/thumb", 4096), ("flight/split", QByteArray())):
        s.setValue(key, v)
    s.sync()
    w = MainWindow()
    try:
        assert not w.restore_geometry(1000, 600)
        show(app, w)
        assert w.tabs.currentIndex() == 0
        assert w.console.is_expanded()
        assert w.calib.view.zoom() == "fit" and w.calib.sections["colour"].is_expanded()
        assert w.calib.strip.is_expanded()
        assert w.label.opacity.value() == 45 and w.label.folder.name == "data"
        assert w.blobs.size_slider.value() == 72
        assert w.calib.split.sizes()[1] > 0
    finally:
        w.close()


def test_label_window_folder_on_the_command_line_wins(app, tmp_path):
    saved, given = tmp_path / "saved", tmp_path / "given"
    saved.mkdir()
    given.mkdir()
    s = store()
    s.setValue("label/folder", str(saved))
    s.setValue("label/opacity", 30)
    lw = LabelWindow(given)
    try:
        assert lw.label.folder == given and lw.label.opacity.value() == 30
    finally:
        lw.close()
    lw = LabelWindow(None)
    try:
        assert lw.label.folder == given          # the last one used, saved on close
    finally:
        lw.close()
