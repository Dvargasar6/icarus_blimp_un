"""The whole viewer against the fake camera on a pty: the protocol paths a
bench session uses, without hardware. Slow-ish (~20 s)."""
import time

import pytest
from PyQt6.QtWidgets import QApplication

from tcview import detector as D
from tcview import protocol as P
from tcview.__main__ import MainWindow
from tcview.fake_cam import FakeCamera


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def rig(app):
    cam = FakeCamera(corrupt_every=4).start()
    win = MainWindow(cam.port, " test")
    win.show()
    win.connect_link()
    yield win, cam
    win.close()
    cam.stop()


def wait(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_bench_session(app, rig):
    win, cam = rig
    cal, flight = win.calib, win.flight
    assert wait(app, lambda: cal.cfg.get("hue") == 0), "no #!CFG from the camera"
    assert wait(app, lambda: flight.last_row is not None)
    assert cal.cfg["fps"] == 12.5 and cal.cfg["period_ms"] == 80

    # The sensor is the clock: a rate is rounded to whole sensor frames, and
    # #!CFG says what was really set. Nothing is faster than every frame.
    assert cal.cfg["sensor_us"] == 79968
    win.send("f15")
    assert wait(app, lambda: win.link.pending() == 0)
    assert cal.cfg["period_ms"] == 80
    win.send("f6.25")
    assert wait(app, lambda: cal.cfg.get("period_ms") == 159.9)
    win.send("f12.5")
    assert wait(app, lambda: cal.cfg.get("period_ms") == 80 and win.link.pending() == 0)
    # A letter that takes no argument, with one, is refused, not obeyed.
    win.send("s=1")
    assert wait(app, lambda: win.link.counters.naks >= 1)
    assert cam.stream

    # Grab one frame: it arrives, its row pairs with it by t_ms.
    win.send("g")
    assert wait(app, lambda: cal.frame is not None)
    assert wait(app, lambda: cal.frame_row is not None)
    assert cal.frame_row["t_ms"] == cal.frame.t_ms

    # Move the hue control: applied only once the camera's #!CFG agrees.
    cal.p["hue"].set_local(350, send=True)
    assert wait(app, lambda: cam.cfg["hue"] == 350)
    assert wait(app, lambda: cal.p["hue"].camera == 350 and not cal.p["hue"].in_flight)
    assert cal.cfg["settings"] == "modified"

    # Out of range is refused, and the control goes back to the camera's value.
    cal.p["tol"].set_local(12)
    win.send("T95")
    assert wait(app, lambda: win.link.counters.naks >= 2)

    # Use a region as the target: same rule as 'c'.
    cal.view.set_region(60, 40, 16, 16)
    app.processEvents()
    cal._use_region()
    assert wait(app, lambda: cal.p["val"].camera == cal.p["val"].local
                and not any(c.in_flight for c in cal.p.values()), timeout=15)

    # Faster link, live frames, with every 4th frame corrupted: dropped, not shown.
    win.send("B921600")
    # pending() clears on the ACK of the confirming 'k'; the #!CFG it prints
    # comes on the next line, so wait for that too rather than racing it.
    assert wait(app, lambda: win.link.baud == 921600 and win.link.pending() == 0
                and cal.cfg.get("baud") == 921600)
    win.send("v1")
    n0 = win.link.counters.frames_ok
    assert wait(app, lambda: win.link.counters.frames_ok >= n0 + 5, timeout=20)
    assert win.link.counters.frames_bad >= 1

    # Save, then the flight tab returns the link to flight state.
    win.send("!save")
    assert wait(app, lambda: cal.cfg.get("settings") == "saved")
    win.tabs.setCurrentWidget(flight)
    assert wait(app, lambda: win.link.baud == P.BOOT_BAUD and cal.cfg.get("image") == 0
                and win.link.pending() == 0, timeout=15)
    assert wait(app, lambda: flight.banner.text().startswith("FLIGHT-IDENTICAL"), timeout=6)
    flight.tick()
    assert "rows/s" in flight.health.copy_text()

    # A stage with no model is accepted, and falls back visibly: #!CFG and
    # every v2 row say what really runs. The colour table exists and the
    # classifier does not, so stage 3 runs stage 1.
    win.send("!stage 3")
    assert wait(app, lambda: cal.cfg.get("stage") == 3)
    assert cal.cfg["stage_eff"] == 1 and cal.cfg["csv"] == P.CSV_VERSION
    assert cal.cfg["lut"] == "ok" and cal.cfg["lut_id"] == D.models().lut_id
    assert "requested, running 1" in cal.stage_lbl.text()
    assert wait(app, lambda: flight.last_row.v["csv_v"] == 2 and flight.last_row.v["stage"] == 1)
    win.send("!stage 9")
    assert wait(app, lambda: win.link.counters.naks >= 3)
    flight.tick()
    assert "dt != 80" in flight.health.copy_text() and "stage      1 running" in flight.health.copy_text()


def test_rotate_180_turns_the_image_in_the_camera(app, rig):
    """Rotate 180° sends !flip; the camera turns the image itself, so the
    wood patch (lower left in the scene) arrives upper right."""
    win, cam = rig
    cal = win.calib

    def wood_corner():
        rgb = cal.frame.rgb().astype(int)
        lower_left, upper_right = rgb[110, 10], rgb[9, 149]
        return "lower left" if lower_left[0] - lower_left[2] > 50 else \
            "upper right" if upper_right[0] - upper_right[2] > 50 else "?"

    def grab():
        # The rig corrupts every 4th frame, and a corrupt one is dropped.
        seq = cal.frame.seq if cal.frame is not None else None
        for _ in range(3):
            win.send("g")
            if wait(app, lambda: cal.frame is not None and cal.frame.seq != seq, 6.0):
                return
        raise AssertionError("no frame")

    assert wait(app, lambda: "flip" in cal.cfg)
    assert cal.flip_btn.isEnabled() and not cal.flip_btn.isChecked()
    # The fake writes a frame at once and then sleeps for its wire time, so
    # at 115200 its replies lag 3 s behind the frame; a fast link keeps the
    # commands below inside their timeout.
    win.send("B921600")
    assert wait(app, lambda: win.link.baud == 921600 and win.link.pending() == 0
                and cal.cfg.get("baud") == 921600)
    grab()
    assert wood_corner() == "lower left"

    cal.flip_btn.click()
    assert wait(app, lambda: cal.cfg.get("flip") == 1)
    assert cam.cfg["flip"] == 1 and cal.flip_btn.isChecked()
    assert cal.cfg["settings"] == "modified"
    assert "turned 180°" in cal.sections["sensor"].summary()
    grab()
    assert wood_corner() == "upper right"

    win.send("!flip 2")
    assert wait(app, lambda: win.link.counters.naks >= 1)
    assert cam.cfg["flip"] == 1
    cal.flip_btn.click()
    assert wait(app, lambda: cal.cfg.get("flip") == 0)
    assert not cal.flip_btn.isChecked() and cal.cfg["settings"] == "defaults"

    # A camera whose firmware has no !flip never reports flip=.
    cal.on_cfg({k: v for k, v in cal.cfg.items() if k != "flip"})
    assert not cal.flip_btn.isEnabled()


def test_white_balance_session(app, rig):
    """w2, W<r>,<g>,<b> and Wc against the fake: the gains reach #!CFG, the
    controls and the blob records' fields."""
    win, cam = rig
    cal = win.calib
    assert wait(app, lambda: cal.cfg.get("wb") == "auto")
    assert cal.cfg["wb_gain"] == "0,0,0" and cal.wbcal_btn.isEnabled()
    win.send("B921600")                 # see test_rotate_180: replies keep up
    assert wait(app, lambda: win.link.baud == 921600 and win.link.pending() == 0)
    for sp, g in zip(cal.wb_spin, (97, 64, 58)):
        sp.setValue(g)
    cal.wb_set_btn.click()
    assert wait(app, lambda: cal.cfg.get("wb_gain") == "97,64,58")
    assert cal.cfg["wb"] == "fixed" and cal.wb_seg.value() == "fixed"
    assert cam._wb_gain() == (97, 64, 58)
    naks = win.link.counters.naks
    win.send("W300,64,64")
    assert wait(app, lambda: win.link.counters.naks > naks)
    assert cam.cfg["wb_fixed"] == (97, 64, 58)
    cal.wb_seg.buttons["auto"].click()
    assert wait(app, lambda: cal.cfg.get("wb") == "auto")
    assert cal.cfg["wb_fixed"] == "97,64,58"            # kept for the next w2
    cal.wbcal_btn.click()
    assert wait(app, lambda: cal.cfg.get("wb") == "fixed"
                and cal.cfg.get("wb_fixed") != "97,64,58")
    r, g, b = cam.cfg["wb_fixed"]
    assert g == 64 and b > r                            # the fake wall is warm


def test_camera_revert_when_pc_never_confirms(app):
    """Firmware side of the handshake, on the fake: no command at the new
    speed within 3 s -> back to 115200."""
    cam = FakeCamera().start()
    import os
    fd = os.open(cam.port, os.O_RDWR | os.O_NOCTTY)
    try:
        os.write(fd, b"\nB921600\n")
        assert wait(app, lambda: cam.baud == 921600, timeout=3)
        assert wait(app, lambda: cam.baud == P.BOOT_BAUD, timeout=5)
    finally:
        os.close(fd)
        cam.stop()


def test_link_recovers_when_the_fast_speed_goes_bad(app):
    """The 2026-10-17 hang: switched up, image on, then the camera's output
    turns to noise while it is still at the fast speed. The viewer must ask
    it down before following it to 115200, or the two stay apart."""
    cam = FakeCamera().start()
    win = MainWindow(cam.port, " test")
    win.show()
    win.connect_link()
    try:
        win.send("B460800")
        assert wait(app, lambda: win.link.baud == 460800 and win.link.pending() == 0)
        win.send("v1")
        assert wait(app, lambda: win.link.pending() == 0)
        cam.noisy_fast = True
        assert wait(app, lambda: cam.baud == P.BOOT_BAUD and win.link.baud == P.BOOT_BAUD,
                    timeout=15)
        rows = win.link.counters.rows
        assert wait(app, lambda: win.link.counters.rows > rows, timeout=10)
    finally:
        win.close()
        cam.stop()


def test_collect_records_match_the_pc(app, tmp_path):
    """'!collect 1' and 'g' at 115200, the bench procedure: a grabbed frame
    carries its records, and every record is re-computed on the PC from that
    frame and must be the same numbers (the B1 check, here against the fake,
    which runs the same C code; on the board it is the real test). Then label
    some, save, and nothing is autosaved twice."""
    from tcview import blobset as B
    cam = FakeCamera().start()
    win = MainWindow(cam.port, " test")
    win.show()
    win.connect_link()
    blobs = win.blobs
    blobs.autosave_dir = tmp_path / "auto"
    try:
        assert wait(app, lambda: "collect" in blobs.cfg)
        assert not blobs.collect_btn.isChecked() and blobs.cfg["collect"] == 0
        blobs.collect_btn.setChecked(True)                # sends !collect 1
        assert wait(app, lambda: blobs.cfg.get("collect") == 1)
        # A grab is 3.3 s on the wire at 115200; each one brings its records.
        for _ in range(4):
            win.send("g")
            n = blobs.agree.checked
            assert wait(app, lambda: blobs.agree.checked > n, timeout=10), blobs.agree.text()
            if blobs.agree.checked >= 3:
                break
        assert blobs.agree.checked >= 3, blobs.agree.text()
        assert blobs.agree.missing == 0, blobs.agree.text()     # same blobs found on both
        assert blobs.agree.ok(), blobs.agree.text()
        assert all(s.source == "live" and s.label == B.UNLABELLED for s in blobs.samples)
        assert win.link.counters.blobs_ok >= 3 and win.link.counters.blobs_bad == 0
        # Entering the flight screen ends collection (and the image); the
        # banner names it while it is on.
        win.flight.tick()
        assert "blob collection is ON" in win.flight.banner.text()
        win.tabs.setCurrentWidget(win.flight)
        assert wait(app, lambda: blobs.cfg.get("collect") == 0 and blobs.cfg.get("image") == 0
                    and win.link.pending() == 0, timeout=15)
        assert not blobs.collect_btn.isChecked()

        blobs.set_label([0, 1], B.BALLOON)
        assert B.counts(blobs.samples)["balloon"] == 2 and blobs.dirty
        blobs.save_to(tmp_path / "s.npz")
        assert not blobs.dirty
        back = B.load(tmp_path / "s.npz")
        assert len(back) >= len(blobs.samples) - 4      # more may have arrived since
        assert [s.label for s in back[:2]] == [B.BALLOON, B.BALLOON]
    finally:
        win.close()
        cam.stop()


# ---- connection management (docs/GUI_REDESIGN.md §13.1) -----------------------

def connected(win):
    return win.conn == "on" and win.calib.cfg.get("hue") is not None


def disconnected(win):
    return win.link is None and win.conn == "off"


def test_connect_from_the_gui_and_back(app, tmp_path, monkeypatch):
    """Port chosen in the bar, Connect, Disconnect, Connect again: the camera
    state is forgotten in between, the counters start again, and a recording
    does not span the two connections."""
    from tcview import flight_tab
    monkeypatch.setattr(flight_tab, "DATA_DIR", tmp_path)
    cam = FakeCamera().start()
    win = MainWindow(cam.port, " test", fake=True)
    win.show()
    bar = win.bar
    try:
        assert disconnected(win) and bar.connect_btn.text() == "Connect"
        assert bar.connect_btn.property("role") == "primary"
        assert bar.port_combo.port() == cam.port
        assert bar.port_combo.currentText() == cam.port       # the full name is in the list
        assert bar.port_combo.itemText(0) == "FAKE camera (%s)" % cam.port
        assert win.stats.copy_text().startswith("baud — | kB/s — |")
        win.tabs.setCurrentWidget(win.label)
        assert not bar.offline_note.isHidden()
        win.tabs.setCurrentWidget(win.calib)
        assert bar.offline_note.isHidden()

        # Typed while disconnected: not sent, and the console says so.
        win.entry.setText("?")
        win.entry.returnPressed.emit()
        assert "# [viewer] not connected: '?' not sent" in win.console.text()

        bar.connect_btn.click()
        assert wait(app, lambda: connected(win))
        assert bar.pill.tone() == "ok"
        assert bar.pill.text() == "Connected · %s · 115200 baud" % cam.port
        assert not bar.port_combo.isEnabled() and bar.set_baud_btn.isEnabled()
        assert cam.port in win.windowTitle()
        assert win.calib.p["hue"].camera == 0
        assert wait(app, lambda: win.link.counters.rows > 2)
        win._tick1()
        assert win.stats.copy_text().startswith("baud 115200 | kB/s ")
        first = win.link
        win.flight.rec_btn.setChecked(True)
        assert wait(app, lambda: win.flight.rec_n > 0)

        bar.connect_btn.click()
        assert wait(app, lambda: disconnected(win))
        assert bar.pill.text() == "Not connected" and bar.port_combo.isEnabled()
        assert win.calib.cfg == {} and win.calib.p["hue"].camera is None
        assert win.calib.p["hue"].status.text() == "no camera"
        assert win.blobs.collect_chip.text() == "camera: unknown"
        win.flight.tick()
        assert "no #!CFG" in win.flight.banner.text()
        assert win.flight.rec is None and "recording stopped" in win.console.text()
        assert len(list(tmp_path.glob("flight_*.csv"))) == 1
        assert win.calib.frame_row is None or win.calib.frame is not None   # frames stay
        assert win.stats.copy_text().startswith("baud — |")
        assert win.windowTitle().endswith("not connected")

        bar.connect_btn.click()
        assert wait(app, lambda: connected(win))
        assert win.link is not first and win.link.counters.frames_ok == 0
    finally:
        win.close()
        cam.stop()


def test_disconnect_brings_the_camera_back_to_115200(app):
    cam = FakeCamera().start()
    win = MainWindow(cam.port, " test")
    win.show()
    win.connect_link()
    try:
        assert wait(app, lambda: connected(win))
        win.bar.set_baud(460800)
        win.bar.set_baud_btn.click()
        assert wait(app, lambda: win.link.baud == 460800 and win.link.pending() == 0
                    and cam.baud == 460800)
        assert win.bar.pill.text().endswith("460800 baud")
        win.bar.connect_btn.click()
        assert win.bar.pill.text() == "Disconnecting…"
        assert wait(app, lambda: disconnected(win), timeout=8)
        assert cam.baud == P.BOOT_BAUD
        assert not cam.revert_at            # came down by the handshake, not the revert
        assert win.bar.pill.text() == "Not connected"
    finally:
        win.close()
        cam.stop()


def test_window_close_brings_the_camera_back_to_115200(app):
    cam = FakeCamera().start()
    win = MainWindow(cam.port, " test")
    win.show()
    win.connect_link()
    try:
        assert wait(app, lambda: connected(win))
        win.send("B921600")
        assert wait(app, lambda: win.link.baud == 921600 and win.link.pending() == 0)
        win.close()
        assert cam.baud == P.BOOT_BAUD
    finally:
        cam.stop()


def test_a_port_that_is_not_there(app):
    win = MainWindow("/dev/ttyNOPE_tcview")
    win.show()
    try:
        win.connect_link()
        assert wait(app, lambda: disconnected(win))
        assert win.bar.pill.tone() == "err"
        assert win.bar.pill.text() == "Port not found: /dev/ttyNOPE_tcview"
        log = win.console.text()
        assert "cannot open /dev/ttyNOPE_tcview: " in log and "No such file" in log
        assert "unplugged, or a different port name" in log
        assert win.bar.connect_btn.isEnabled() and win.bar.connect_btn.text() == "Connect"
    finally:
        win.close()


def test_one_viewer_per_port(app):
    """The port is opened exclusively: a second viewer is told it is in use
    instead of quietly sharing the byte stream."""
    cam = FakeCamera().start()
    first = MainWindow(cam.port, " test")
    second = MainWindow(cam.port, " test")
    first.show()
    second.show()
    try:
        first.connect_link()
        assert wait(app, lambda: connected(first))
        second.connect_link()
        assert wait(app, lambda: disconnected(second))
        assert second.bar.pill.text() == "Port in use: %s" % cam.port
        assert "in use by another program" in second.console.text()
        assert first.conn == "on"
    finally:
        second.close()
        first.close()
        cam.stop()


def test_link_lost(app):
    """Unplugged: the GUI goes disconnected and says so, and does not
    reconnect by itself."""
    cam = FakeCamera().start()
    win = MainWindow(cam.port, " test")
    win.show()
    win.connect_link()
    try:
        assert wait(app, lambda: connected(win))
        cam.stop()
        assert wait(app, lambda: disconnected(win))
        assert win.bar.pill.tone() == "err"
        assert win.bar.pill.text() == "Link lost · %s" % cam.port
        assert "link lost: " in win.console.text()
        assert win.calib.cfg == {}
        app.processEvents()
        assert win.link is None and win.bar.connect_btn.text() == "Connect"
    finally:
        win.close()
