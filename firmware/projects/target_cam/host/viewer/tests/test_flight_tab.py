"""The Flight data screen (docs/GUI_REDESIGN.md §6.2), driven without a camera:
CSV rows and a #!CFG are handed to it the way the main window hands them
over, and what it shows and copies (§7) is checked."""
import time

import pytest
from PyQt6.QtWidgets import QApplication

from tcview import protocol as P
from tcview.__main__ import MainWindow
from tcview.ui import theme

# The spec's example row (§6.2): one blob at az 1.16, el 3.35, 1599 mm.
BASE = dict(t_ms=35993, ok=1, cx_px=83.17, cy_px=50.81, az_deg=1.16, el_deg=3.35,
            area_px=860, w_px=42, h_px=54, fill=0.38, range_mm=1599, clipped=0, blobs=1,
            matched_px=934, b2_az=0, b2_el=0, b2_range_mm=-1, b3_az=0, b3_el=0,
            b3_range_mm=-1, b4_az=0, b4_el=0, b4_range_mm=-1, dt_ms=80.0, proc_us=2300,
            stage=1, conf=-1, b2_conf=-1, b3_conf=-1, b4_conf=-1, t_px_us=1200, t_b_us=0,
            t_c_us=0, loop_us=9000, csv_v=2)
FLIGHT_CFG = dict(image=0, collect=0, baud=P.BOOT_BAUD, stream=1, settings="saved",
                  stage=1, lut="ok", clf="none", cnn="none", fps=12.5, period_ms=80.0,
                  fov=54.0, mm=550)


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
    w.tabs.setCurrentWidget(w.flight)
    pump(app)
    yield w
    w.close()


def pump(app, s=0.2):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def row(version=2, **kw) -> P.Row:
    v = dict(BASE, **kw)
    raw = ",".join(("%g" % v[k]) for k in P.columns_for(version))
    return P.Row(P.parse_row(raw), raw)


def feed(flight, n=3, **kw):
    """n rows 80 ms apart, ending at t_ms (default the example's)."""
    t_end = kw.pop("t_ms", BASE["t_ms"])
    for i in range(n):
        flight.on_row(row(t_ms=t_end - 80 * (n - 1 - i), **kw))
    flight.tick()


def test_verdict_tile_states(app, win):
    fl = win.flight
    fl.tick()
    assert fl.status.text() == "NO DATA" and fl.verdict_text() == "NO DATA  no row from the camera yet"
    feed(fl, blobs=2, b2_az=-5.0, b2_el=1.0, b2_range_mm=2400)
    assert fl.verdict_text() == "TARGET  2 blob(s) in frame · largest first"
    assert theme.TOKENS["verdict_target"] in fl.verdict.styleSheet()
    feed(fl, ok=0, blobs=0)
    assert fl.status.text() == "NO TARGET"
    # A second without a row: stale.
    ts, v = fl.rows[-1]
    fl.rows[-1] = (ts - 2.0, v)
    fl.tick()
    assert fl.status.text() == "NO ROWS" and fl.status_sub.text().startswith("no row for ")


def test_blobs_table_and_csv(app, win):
    fl = win.flight
    feed(fl)
    assert fl.blobs_csv() == "#,az_deg,el_deg,range_mm,notes\n1,1.16,3.35,1599,"
    t = fl.table
    assert t.item(0, 0).text() == "1 largest"
    assert [t.item(1, c).text() for c in range(1, 4)] == ["—"] * 3
    assert t.item(1, 1).foreground().color() == theme.qcolor("text_faint")
    assert t.item(0, 1).foreground().color() == theme.qcolor("text")
    feed(fl, clipped=1, range_mm=-1, blobs=2, b2_az=-5.25, b2_el=1.5, b2_range_mm=2400)
    assert fl.blobs_csv() == ("#,az_deg,el_deg,range_mm,notes\n"
                              "1,1.16,3.35,-,clipped (no range)\n2,-5.25,1.50,2400,")
    assert fl.blobs_card.copy_text() == fl.blobs_csv()


def test_blob1_card(app, win):
    fl = win.flight
    assert fl.blob1_text() == "blob 1: no row yet"
    feed(fl)
    assert fl.blob1_card.copy_text() == (
        "blob 1: area 860 px  box 42x54  fill 0.38  centroid (83.2, 50.8)  matched px 934\n"
        "blobs in frame 1 (4 reported at most)")
    assert fl.b1["box"].text() == "42×54 px" and fl.b1["matched px"].text() == "934"
    assert fl.blob1_card.subtitle_lbl.text() == "1 in frame · 4 reported at most"


def test_link_health_copies_the_old_block(app, win):
    fl = win.flight
    assert fl.health.copy_text() == "waiting for rows"
    fl.on_cfg(dict(FLIGHT_CFG))
    feed(fl, n=4)
    lines = fl.health.copy_text().split("\n")
    assert lines[0] == "stage      1 running (requested 1; lut ok, clf none, cnn none)  csv v2"
    assert lines[1].startswith("rows/s      12.5   (camera fps setting 12.5)")
    assert lines[2] == "dt_ms      mean   80.0  jitter (sd)   0.0  max   80.0"
    assert lines[3] == "dt != 80   0 of 4 rows"
    assert lines[5] == "t_px_us    p50   1200 max   1200"
    assert lines[8] == "loop_us    p50   9000 max   9000   (budget 80000 us)"
    assert lines[10].startswith("wire ") and len(lines) == 11
    assert fl.stage_line.text() == ("stage 1 running · requested 1 · lut ok · clf none · "
                                    "cnn none · csv v2")
    name, typ, mx, note, bar = fl.hc["loop_us"]
    assert (typ.text(), mx.text()) == ("p50 9000", "9000")
    assert bar.isVisibleTo(fl) and abs(bar.fraction - 9000 / 80000) < 1e-9
    name, typ, mx, note, bar = fl.hc["dt!="]
    assert name.text() == "dt≠80" and typ.text() == "0 of 4 rows" and not name.styleSheet()


def test_late_rows_turn_the_pacing_line_amber(app, win):
    fl = win.flight
    fl.on_cfg(dict(FLIGHT_CFG))
    feed(fl, n=3)
    fl.on_row(row(t_ms=BASE["t_ms"] + 3353, dt_ms=3353.0))
    fl.tick()
    assert "dt != 80   1 of 4 rows, worst 3353.0" in fl.health.copy_text()
    name, typ, mx, note, _ = fl.hc["dt!="]
    assert (typ.text(), mx.text(), note.text()) == ("1 of 4 rows", "3353.0", "worst gap")
    assert theme.TOKENS["warn_text"] in name.styleSheet()
    assert typ.styleSheet() == name.styleSheet()
    fl.reset_counts()
    fl.tick()
    assert fl.hc["dt!="][1].text() == "0 of 0 rows" and not fl.hc["dt!="][0].styleSheet()


def test_v1_rows_say_not_reported(app, win):
    fl = win.flight
    feed(fl, version=1)
    assert "t_px_us      (v1 row: not reported)" in fl.health.copy_text()
    _, typ, _, note, bar = fl.hc["loop_us"]
    assert typ.text() == "not reported" and note.text() == "v1 row" and not bar.isVisibleTo(fl)


def test_last_raw_row_copies_verbatim(app, win):
    fl = win.flight
    r = row()
    fl.on_row(r)
    fl.tick()
    assert fl.raw.text() == r.raw and fl.raw.isReadOnly()
    assert fl.raw_copy.copy() == r.raw


def test_series_show_the_current_value_and_range(app, win):
    fl = win.flight
    for i, az in enumerate((0.5, 2.0, 1.16)):
        fl.on_row(row(t_ms=1000 + 80 * i, az_deg=az))
    fl.tick()
    s = fl.series["az"]
    assert (s["value"].text(), s["max"].text(), s["min"].text()) == ("1.16", "2.00", "0.50")
    assert fl.series["range"]["value"].text() == "1599"
    fl.on_row(row(t_ms=1240, ok=0, blobs=0))
    fl.tick()
    assert fl.series["az"]["value"].text() == "—"


def test_banner_tones(app, win):
    fl = win.flight
    fl.tick()
    assert fl.banner.tone() == "warn"
    assert fl.banner.text().startswith("NOT flight-identical: no #!CFG")
    fl.on_cfg(dict(FLIGHT_CFG))
    fl.tick()
    assert fl.banner.tone() == "ok" and fl.banner.text().startswith("FLIGHT-IDENTICAL")
    assert fl.banner.toolTip() == fl.banner.text()
    fl.on_cfg(dict(FLIGHT_CFG, settings="modified"))
    fl.tick()
    assert fl.banner.tone() == "warn" and "BUT the settings are not saved" in fl.banner.text()


def test_copy_all_joins_the_cards(app, win):
    fl = win.flight
    fl.on_cfg(dict(FLIGHT_CFG))
    feed(fl)
    parts = fl.copy_all().split("\n\n")
    assert parts == [fl.verdict_text(), fl.blobs_csv(), fl.blob1_text(), fl.health.copy_text()]


def test_record_button_states(app, win, tmp_path, monkeypatch):
    from tcview import flight_tab
    monkeypatch.setattr(flight_tab, "DATA_DIR", tmp_path)
    fl = win.flight
    fl.rec_btn.setChecked(True)
    assert fl.rec_btn.text() == "Recording…" and fl.rec_btn.property("role") == "danger"
    fl.on_row(row())
    fl.rec_btn.setChecked(False)
    assert fl.rec_btn.text() == "Record" and fl.rec_lbl.text().endswith("(1 rows)")
    lines = next(tmp_path.glob("flight_*.csv")).read_text().splitlines()
    assert lines == [",".join(P.COLUMNS), row().raw]


def test_small_screen_keeps_every_part(app, win):
    win.resize(1024, 600)
    pump(app)
    fl = win.flight
    feed(fl)
    pump(app)
    for w in (fl.fov, fl.series["az"]["plot"].getViewBox(), fl.raw, fl.verdict):
        assert w.isVisible()
    assert fl.fov.height() >= 100 and fl.raw.height() > 0
    assert fl.split.sizes()[0] >= 300
