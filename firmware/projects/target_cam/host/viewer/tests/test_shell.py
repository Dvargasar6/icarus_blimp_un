"""The application shell (docs/GUI_REDESIGN.md §5): console dock, command
line, status bar, tabs, and the 1280x720 rule. No camera: the window runs
disconnected."""
import time

import pytest
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from tcview.__main__ import MainWindow
from tcview.ui import theme
from tcview.ui.shell import ConsoleDock, StatsBar, baud_label, counter_segments


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


@pytest.fixture
def win(app):
    w = MainWindow()
    w.show()
    yield w
    w.close()


def pump(app, s=0.2):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_tabs_have_short_names_and_tooltips(win):
    t = win.tabs
    assert [t.tabText(i) for i in range(t.count())] == [
        "Image && calibration", "Flight data", "Label frames", "Blobs"]
    assert t.tabToolTip(1) == "what the ESP32-S3 gets" and t.tabToolTip(3) == "classifier data"
    assert [t.page(i) for i in range(4)] == [win.calib, win.flight, win.label, win.blobs]
    t.setCurrentWidget(win.blobs)
    assert t.current_page() is win.blobs


def test_baud_labels_show_the_frame_time():
    assert baud_label(115200) == "115200 · frame 3.3 s"
    assert baud_label(460800) == "460800 · frame 0.83 s"


@pytest.mark.parametrize("size", [(1280, 720), (1024, 600)])
def test_command_line_and_status_bar_always_visible(app, win, size):
    """§2: at 1280x720 (and 1024x600) the command line, Send and the status
    bar are on screen on every tab, with the console open or folded."""
    win.resize(*size)
    for expanded in (True, False):
        win.console.set_expanded(expanded)
        for i in range(win.tabs.count()):
            win.tabs.setCurrentIndex(i)
            pump(app, 0.15)
            assert win.width() <= size[0] and win.height() <= size[1], (win.size(), i)
            frame = QRect(QPoint(0, 0), win.size())
            for w in (win.entry, win.console.send_btn, win.stats.label, win.bar.connect_btn):
                assert w.isVisible(), (w, i, expanded)
                r = QRect(w.mapTo(win, QPoint(0, 0)), w.size())
                assert frame.contains(r), (w, r, frame, i, expanded)
    win.console.set_expanded(True)


def test_console_collapses_to_header_and_command_line(app, win):
    win.resize(1280, 720)
    pump(app)
    c = win.console
    win._say("> v1")
    win._say("# image on")
    open_h = c.height()
    c.set_expanded(False)
    pump(app)
    assert not c.log.isVisible() and c.last_line.isVisible()
    assert c.last_line.text() == "# image on"
    assert c.height() < 110 < open_h
    win._say("# [viewer] something failed", "err")
    assert c.last_line.text() == "# [viewer] something failed"
    c.set_expanded(True)
    pump(app)
    assert c.log.isVisible() and not c.last_line.isVisible()
    assert abs(c.height() - open_h) <= 2


def test_console_colours_copy_and_clear(app, win):
    c = win.console
    c.clear()
    win._say("> k")
    win._say("# camera says")
    win._say("# [viewer] cannot open x", "err")
    win._say("1,2,3")
    doc = c.log.document()
    colours = [doc.findBlockByNumber(i).begin().fragment().charFormat().foreground().color().name()
               for i in range(4)]
    T = theme.TOKENS
    assert colours == [T["text"], T["text_muted"], T["err_text"], T["text"]]
    assert c.copy_btn.copy() == "> k\n# camera says\n# [viewer] cannot open x\n1,2,3"
    assert c.log.maximumBlockCount() == ConsoleDock.MAX_LINES == 5000
    c.clear_btn.click()
    assert c.text() == ""
    assert win.show_rows.text() == "CSV rows" and not win.show_rows.isChecked()


def test_command_history(app, win):
    e = win.entry
    for cmd in ("?", "m", "H350"):
        e.setText(cmd)
        e.returnPressed.emit()
    assert e.text() == ""

    def key(k):
        e.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier))
    key(Qt.Key.Key_Up)
    assert e.text() == "H350"
    key(Qt.Key.Key_Up)
    key(Qt.Key.Key_Up)
    key(Qt.Key.Key_Up)
    assert e.text() == "?"
    key(Qt.Key.Key_Down)
    assert e.text() == "m"
    key(Qt.Key.Key_Down)
    key(Qt.Key.Key_Down)
    assert e.text() == ""
    # Not connected, so none of them went anywhere, and each said so.
    assert win.console.text().count("not connected") == 3


class _Link:
    baud = 460800

    class counters:
        frames_ok, frames_bad, blobs_ok, blobs_bad = 11, 0, 545, 2
        junk_lines, naks, retries, timeouts = 1, 0, 1, 0

        @staticmethod
        def rate(window=2.0):
            return 1800.0


def test_status_bar_segments(app):
    s = StatsBar()
    s.set_segments(counter_segments(_Link, 12.0))
    assert s.copy_text() == ("baud 460800 | kB/s 1.8 | rows/s 12.0 | frames ok/bad 11 / 0 | "
                             "blobs ok/bad 545 / 2 | junk 1 | NAK 0 | retries 1 | timeouts 0")
    html = s.label.text()
    warn = theme.TOKENS["warn_text"]
    # > 0 in the warn colour: blobs bad 2, junk 1, retries 1; not NAK 0 or frames bad 0
    assert html.count(warn) == 3
    assert s.label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    s.set_segments(counter_segments(None, 0))
    assert s.copy_text() == ("baud — | kB/s — | rows/s — | frames ok/bad — / — | "
                             "blobs ok/bad — / — | junk — | NAK — | retries — | timeouts —")
