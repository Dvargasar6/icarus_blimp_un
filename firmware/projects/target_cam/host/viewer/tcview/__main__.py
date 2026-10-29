"""
target_cam viewer.

  python -m tcview                         choose the port in the window, then Connect
  python -m tcview --port /dev/ttyACM0     connect to the camera at once (ESP32-CAM-MB)
  python -m tcview --fake                  a synthetic camera on a pseudo-terminal
  python -m tcview --label [DIR]           label saved frames; no camera needed

Four screens: "Image & calibration" (see the real pixels, tune the detector
by hand, save frames, save to the camera's flash), "Flight data" (only the CSV
the ESP32-S3 will receive), "Label frames" (mark balloon and background on
saved frames, for training and scoring) and "Blobs" (collect and label the
blobs stage 2's classifier will learn from). The bar at the top picks the port
and connects; the console at the bottom shows everything else the camera says,
and takes raw commands.
"""
from __future__ import annotations

import argparse
import sys
import time

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

from . import detector as D
from . import protocol as P
from .blobs_tab import BlobsTab
from .calib_tab import CalibTab
from .flight_tab import FlightTab
from .label_tab import LabelTab
from .link import CLOSE_S, LOST, OPEN_FAILED, REVERT_S, SerialLink, open_error
from .ui import persist, theme
from .ui.keys import add_shortcut
from .ui.shell import (ConnectionBar, ConsoleDock, PageTabs, StatsBar,
                       counter_segments)


class MainWindow(QMainWindow):
    """The shell (§5) around the four tabs. `port` preselects a port (and
    `fake` lists it as the fake camera); nothing connects until
    connect_link(), which main() calls only for --port or --fake."""

    def __init__(self, port: str | None = None, title_note: str = "", fake: bool = False):
        super().__init__()
        self.port = port or ""              # of the current or last connection
        self.link: SerialLink | None = None
        self.conn = "off"                   # off / connecting / on / closing
        self._notes = {port: title_note} if port else {}
        self.store = QSettings("icarus_blimp", "tcview")

        self.bar = ConnectionBar()
        self.tabs = PageTabs()
        self.calib = CalibTab(self)
        self.flight = FlightTab(self)
        self.label = LabelTab()
        self.blobs = BlobsTab(self)
        self.tabs.add_page(self.calib, "Image && calibration")
        self.tabs.add_page(self.flight, "Flight data", "what the ESP32-S3 gets")
        self.tabs.add_page(self.label, "Label frames")
        self.tabs.add_page(self.blobs, "Blobs", "classifier data")
        self.tabs.currentChanged.connect(self._tab_changed)
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self.bar)
        cl.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.console = ConsoleDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.console)
        self.log, self.entry = self.console.log, self.console.entry
        self.show_rows = self.console.show_rows
        self.entry.returnPressed.connect(self._entry)
        self.console.send_btn.clicked.connect(self._entry)

        self.stats = StatsBar()
        self.statusBar().addWidget(self.stats, 1)

        combo = self.bar.port_combo
        if fake and port:
            combo.add_extra(port, "FAKE camera (%s)" % port)
        last = self.store.value("conn/port", "", type=str)
        if port:
            combo.set_port(port)
        elif last and combo.has(last):
            combo.set_port(last)
        elif combo.usb:
            combo.set_port(combo.usb[0])
        self.bar.set_baud(self.store.value("conn/baud", P.BOOT_BAUD, type=int))
        self.bar.baud_combo.currentIndexChanged.connect(
            lambda _i: self.store.setValue("conn/baud", self.bar.baud()))
        self.bar.connect_btn.clicked.connect(self._connect_clicked)
        self.bar.set_baud_btn.clicked.connect(lambda: self.send("B%d" % self.bar.baud()))

        self._t1 = QTimer(self, interval=1000)
        self._t1.timeout.connect(self._tick1)
        self._t1.start()
        self._t10 = QTimer(self, interval=100)
        self._t10.timeout.connect(self.flight.tick)
        self._t10.start()
        self._last_rows = (time.monotonic(), 0)
        add_shortcut(self, "Ctrl+L", self.focus_command, while_typing=True)
        self._set_conn("off")
        self._tick1()
        self._sized_console = False
        self.restore_layout()

    def showEvent(self, e):
        super().showEvent(e)
        if not self._sized_console:
            self._sized_console = True
            QTimer.singleShot(0, self._size_console)

    def _size_console(self):
        # A dock starts at its content's size hint, which is half the window
        # here. Unless a height was saved, a quarter of it leaves the tabs
        # their room; a saved one may come from a taller window.
        h = self._console_h or max(150, self.height() // 4)
        self.console.restore(self._console_open, max(80, min(h, self.height() * 3 // 5)))

    def focus_command(self):
        """Ctrl+L: to the command line, whatever has focus."""
        self.entry.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.entry.selectAll()

    # ---- remembered layout (§9) ------------------------------------------------

    def _tab_groups(self):
        return (("calib", self.calib), ("flight", self.flight), ("label", self.label),
                ("blobs", self.blobs))

    def restore_layout(self):
        """Everything but the window's size and place: main() sets those,
        after the default size, with restore_geometry()."""
        s = self.store
        for group, tab in self._tab_groups():
            s.beginGroup(group)
            tab.restore_state(s)
            s.endGroup()
        i = persist.read_int(s, "window/tab", 0, self.tabs.count() - 1)
        if i is not None:
            self.tabs.setCurrentIndex(i)
        on = persist.read_bool(s, "console/open")
        self._console_open = True if on is None else on
        self._console_h = persist.read_int(s, "console/height", 60, 5000) or 0

    def restore_geometry(self, w: int, h: int) -> bool:
        return persist.restore_geometry(self.store, "window/geometry", self, w, h)

    def save_layout(self):
        s = self.store
        persist.save_geometry(s, "window/geometry", self)
        s.setValue("window/tab", self.tabs.currentIndex())
        s.setValue("console/open", self.console.is_expanded())
        s.setValue("console/height", self.console.open_height())
        for group, tab in self._tab_groups():
            s.beginGroup(group)
            tab.save_state(s)
            s.endGroup()
        s.remove("calib/show")          # the old panel toggles (§13.3), gone with them
        s.sync()

    # ---- link ----------------------------------------------------------------

    def connect_link(self):
        """Open the port chosen in the bar. Returns at once: `opened` or
        `closed` from the link says how it went."""
        if self.link is not None:
            return
        port = self.bar.port_combo.port()
        if not port:
            self.bar.pill.set("err", "No port chosen")
            self._say("# [viewer] no port chosen: pick one under Port, or type its path", "err")
            return
        self.port = port
        link = SerialLink(port)
        link.event.connect(self._event)
        link.command_done.connect(self._done)
        link.state.connect(self._state)
        link.baud_changed.connect(self._baud_changed)
        link.opened.connect(self._opened)
        link.closed.connect(lambda kind, detail, l=link: self._closed(l, kind, detail))
        self.link = link
        self._set_conn("connecting")
        link.start()

    def disconnect_link(self):
        """Back to 115200 first if needed (bounded), then close the port.
        Returns at once; the GUI goes disconnected when the link has closed."""
        if self.link is None or self.conn == "closing":
            return
        if self.link.baud != P.BOOT_BAUD:
            self._say("# [viewer] asking the camera back to %d before closing the port"
                      % P.BOOT_BAUD)
        self._set_conn("closing")
        self.link.close()

    def _connect_clicked(self):
        if self.conn == "off":
            self.connect_link()
        else:
            self.disconnect_link()

    def _opened(self):
        self._last_rows = (time.monotonic(), 0)
        self.store.setValue("conn/port", self.port)
        self._set_conn("on")
        # Ask for the config at once: the camera may have booted long ago.
        self.send("k")

    def _closed(self, link: SerialLink, kind: str, detail: str):
        if link is not self.link:
            return                          # a link the window already let go of
        link.wait(2000)
        self.link = None
        port = link.port
        if kind == OPEN_FAILED:
            short, hint = open_error(port, link.open_exc or Exception(detail))
            self.bar.pill.set("err", short)
            if hint:
                self._say("# [viewer] %s: %s" % (short.split(":")[0].lower(), hint), "err")
        elif kind == LOST:
            self.bar.pill.set("err", "Link lost · %s" % port)
        else:
            self.bar.pill.set("neutral", "Not connected")
            if detail:
                self._say("# [viewer] " + detail, "err")
        self._set_conn("off", keep_pill=True)
        self.bar.port_combo.refresh()
        for tab in (self.calib, self.flight, self.blobs):
            tab.on_disconnected()
        self._tick1()

    def _set_conn(self, state: str, keep_pill: bool = False):
        self.conn = state
        bar = self.bar
        off = state == "off"
        bar.connect_btn.setText("Connect" if off else "Disconnect")
        theme.set_role(bar.connect_btn, "primary" if off else "")
        bar.connect_btn.setEnabled(state != "closing")
        bar.port_combo.setEnabled(off)
        bar.set_baud_btn.setEnabled(state == "on")
        if state == "connecting":
            bar.pill.set("warn", "Connecting…")
        elif state == "on":
            self._show_connected()
        elif state == "closing":
            bar.pill.set("warn", "Disconnecting…")
        elif not keep_pill:
            bar.pill.set("neutral", "Not connected")
        self.setWindowTitle("target_cam viewer - %s" % (
            "not connected" if off else self.port + self._notes.get(self.port, "")))
        self._show_offline_note()

    def _show_connected(self):
        self.bar.pill.set("ok", "Connected · %s · %d baud" % (self.port, self.link.baud))

    def _show_offline_note(self):
        self.bar.offline_note.setVisible(
            self.conn == "off" and self.tabs.current_page() is self.label)

    def _baud_changed(self, b: int):
        self._say("# [viewer] port now at %d baud" % b)
        self.bar.set_baud(b)                # the combo shows the speed in use
        # closeEvent lets the link go at once, and its last change, back to
        # 115200 on the way out, arrives queued after that.
        if self.conn == "on" and self.link is not None:
            self._show_connected()

    def send(self, cmd: str) -> bool:
        if self.link is None or self.conn == "closing":
            self._say("# [viewer] not connected: '%s' not sent" % cmd, "err")
            return False
        self._say("> " + cmd)
        self.link.send(cmd)
        return True

    def _say(self, text, kind=None):
        self.console.say(text, kind)

    def _state(self, text):
        bad = text.startswith(("cannot open", "link lost"))
        self._say("# [viewer] " + text, "err" if bad else None)

    def _done(self, cmd, ok, reason):
        if not ok:
            self._say("# [viewer] command %r failed: %s" % (cmd, reason), "err")
        self.calib.on_command_done(cmd, ok, reason)

    def _event(self, ev):
        if isinstance(ev, P.Row):
            self.calib.on_row(ev)
            self.flight.on_row(ev)
            if self.show_rows.isChecked():
                self._say(ev.raw)
        elif isinstance(ev, P.Frame):
            self.calib.on_frame(ev)
            self.flight.on_frame(ev)
            self.blobs.on_frame(ev)
        elif isinstance(ev, P.BlobRec):
            self.blobs.on_blob(ev)
        elif isinstance(ev, P.Cfg):
            self.calib.on_cfg(ev.v)
            self.flight.on_cfg(ev.v)
            self.blobs.on_cfg(ev.v)
        elif isinstance(ev, P.Cal):
            self.calib.on_cal(ev)
        elif isinstance(ev, P.WbCal):
            self.calib.on_wbcal(ev)
        elif isinstance(ev, P.Comment):
            self._say(ev.text)
        elif isinstance(ev, P.Nak):
            self._say("#!NAK %s -- %s" % (ev.cmd, ev.reason))
        elif isinstance(ev, P.FrameError):
            self._say("# [viewer] %s %s dropped: %s" % (ev.kind.lower(), ev.seq, ev.reason),
                      "err")
        elif isinstance(ev, P.Baud):
            self._say("#!BAUD %d" % ev.baud)

    def _entry(self):
        cmd = self.entry.text().strip()
        self.entry.clear()
        if cmd:
            self.entry.remember(cmd)
            self.send(cmd)

    def _tab_changed(self, i):
        page = self.tabs.page(i)
        # Entering Flight data puts the link in flight state; with no link
        # there is nothing to put right.
        if page is self.flight and self.link is not None:
            self.flight.entered()
        elif page is self.label:
            self.label.entered()
        self._show_offline_note()

    def _tick1(self):
        self.calib.tick()
        self.blobs.tick()
        if self.link is None or self.conn != "on":
            self.stats.set_segments(counter_segments(None, 0))
            return
        c = self.link.counters
        t0, n0 = self._last_rows
        now = time.monotonic()
        rps = (c.rows - n0) / (now - t0) if now > t0 else 0
        self._last_rows = (now, c.rows)
        self.stats.set_segments(counter_segments(self.link, rps))

    def closeEvent(self, e):
        self.save_layout()
        link, self.link = self.link, None
        if link is not None:
            # As Disconnect: the camera back to 115200 first, or the next
            # connection hears only junk. close() is bounded by CLOSE_S, and
            # a fallback inside it may sleep REVERT_S more.
            link.close()
            if not link.wait(int((CLOSE_S + REVERT_S + 1) * 1000)):
                link.stop()
        if self.flight.rec:
            self.flight.rec_btn.setChecked(False)
        self.label.save_if_dirty()
        path = self.blobs.autosave()
        if path is not None:
            print("tcview: unsaved blobs written to %s" % path)
        super().closeEvent(e)


class LabelWindow(QMainWindow):
    """--label: the Label tab alone, for labelling away from the camera."""

    def __init__(self, folder):
        super().__init__()
        self.setWindowTitle("target_cam viewer - label frames")
        self.store = QSettings("icarus_blimp", "tcview")
        self.label = LabelTab(folder)
        self.setCentralWidget(self.label)
        # The Label frames tab's own settings, shared with the full viewer;
        # a folder named on the command line wins over the remembered one.
        self.store.beginGroup("label")
        self.label.restore_state(self.store, folder=folder is None)
        self.store.endGroup()

    def restore_geometry(self, w: int, h: int) -> bool:
        return persist.restore_geometry(self.store, "labelwin/geometry", self, w, h)

    def closeEvent(self, e):
        persist.save_geometry(self.store, "labelwin/geometry", self)
        self.store.beginGroup("label")
        self.label.save_state(self.store)
        self.store.endGroup()
        self.store.sync()
        self.label.save_if_dirty()
        super().closeEvent(e)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tcview", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port of the camera, e.g. /dev/ttyACM0")
    ap.add_argument("--fake", action="store_true", help="use a synthetic camera instead")
    ap.add_argument("--fake-corrupt", type=int, default=0, metavar="N",
                    help="with --fake: inject a log line into every Nth frame")
    ap.add_argument("--label", nargs="?", const="", metavar="DIR",
                    help="only label saved frames (default folder target_cam/data)")
    ap.add_argument("--quit-after", type=float, metavar="S",
                    help="close after S seconds (smoke tests)")
    args = ap.parse_args(argv)

    app = QApplication(sys.argv[:1])
    theme.apply(app)        # before any window: pyqtgraph reads its colours at creation
    D.hsv_lut()             # ~0.3 s once, so the first hover does not stall
    if args.label is not None and not args.port and not args.fake:
        from pathlib import Path
        win = LabelWindow(Path(args.label) if args.label else None)
        win.restore_geometry(1300, 800)
        win.show()
        if args.quit_after:
            QTimer.singleShot(int(args.quit_after * 1000), win.close)
        return app.exec()
    fake = None
    port = args.port
    note = ""
    if args.fake:
        from .fake_cam import FakeCamera
        fake = FakeCamera(corrupt_every=args.fake_corrupt).start()
        port, note = fake.port, " (FAKE camera)"
    win = MainWindow(port, note, fake=fake is not None)
    # Last run's size and place; else 1450 x 1000, which shows the Calib tab
    # with every panel open, or what a smaller screen has (the inspector
    # scrolls).
    win.restore_geometry(1450, 1000)
    win.show()
    if port:                # --port or --fake; with neither, the window opens disconnected
        win.connect_link()
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), win.close)
    rc = app.exec()
    if fake:
        fake.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
