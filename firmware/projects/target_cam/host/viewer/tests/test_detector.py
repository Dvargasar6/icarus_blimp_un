"""The ctypes binding gives the C suite's answers: same synthetic disc, same
presets, and blob_mask agreeing with the per-pixel test."""
import math

import numpy as np

from tcview import detector as D

W, H = 160, 120


def frame_with_disc(cx, cy, r, fg=(220, 20, 30), bg=(128, 128, 128), be=True):
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.empty((H, W, 3), dtype=np.int32)
    img[:] = bg
    disc = (xx + 0.5 - cx) ** 2 + (yy + 0.5 - cy) ** 2 <= r * r
    img[disc] = fg
    px = ((img[..., 0] >> 3) << 11) | ((img[..., 1] >> 2) << 5) | (img[..., 2] >> 3)
    return px.astype(">u2" if be else "<u2").tobytes(), int(disc.sum())


def red():
    h, t, s, v = D.preset("red")
    return D.Settings(hue=h, tol=t, sat=s, val=v, area=25, be=1)


def test_presets_come_from_the_c_table():
    assert D.preset("red") == (0, 12, 110, 50)
    assert D.preset("blue") == (220, 30, 90, 40)


def test_disc_found_at_its_centre():
    frame, n = frame_with_disc(100.0, 40.0, 12.0)
    r = D.detect(red(), frame)
    assert r.found == 1 and r.n_blobs == 1
    b = r.b[0]
    assert abs(b.cx - 100.0) < 0.5 and abs(b.cy - 40.0) < 0.5
    assert abs(b.area - n) <= 0.1 * n
    assert not b.clipped


def test_clipped_at_edge():
    frame, _ = frame_with_disc(5.0, 60.0, 12.0)
    assert D.detect(red(), frame).b[0].clipped == 1


def test_grey_scene_is_no_target():
    frame, _ = frame_with_disc(80, 60, 0.1, fg=(128, 128, 128))
    r = D.detect(red(), frame)
    assert r.found == 0 and r.matched == 0


def test_mask_matches_per_pixel_rule_and_count():
    frame, n = frame_with_disc(70.0, 70.0, 20.0)
    st = red()
    m = D.mask(st, frame)
    assert m.shape == (H, W) and set(np.unique(m)) <= {0, 1}
    assert int(m.sum()) == D.detect(st, frame).matched
    lib = D.lib()
    c = st.c()
    px = np.frombuffer(frame, dtype=">u2").reshape(H, W)
    for y in range(0, H, 7):
        for x in range(0, W, 5):
            assert m[y, x] == lib.blob_pixel_matches(c, int(px[y, x]))


def test_wrong_byte_order_breaks_detection():
    frame, _ = frame_with_disc(80.0, 60.0, 15.0)
    st = red()
    st.be = 0
    assert D.detect(st, frame).found == 0


def test_lut_agrees_with_direct_call():
    lut = D.hsv_lut()
    for px in (0x0000, 0xF800, 0x07E0, 0x001F, 0xFFFF, 0x8410, 0xFA20, 0x1234):
        assert tuple(int(a) for a in lut[px]) == D.hsv(px)


def test_sample_and_calibration_rule():
    frame, _ = frame_with_disc(80.0, 60.0, 20.0)
    col = D.sample(red(), frame, 72, 52, 16, 16)
    assert col.hue_spread <= 2 and col.sat > 200
    cal = D.calibrate_from(col)
    assert not cal.refused
    assert cal.tol == 10                    # 3*spread clamped up to 10
    assert cal.sat == max(40, col.sat // 2)
    assert cal.val == max(25, col.val * 2 // 5)
    grey = D.BlobColor(0, 180, 30, 120)
    assert D.calibrate_from(grey).refused


def test_sample_clips_to_frame():
    frame, _ = frame_with_disc(80.0, 60.0, 20.0)
    D.sample(red(), frame, -10, -10, 500, 500)      # must not read out of bounds
    out = D.sample(red(), frame, 200, 200, 10, 10)
    assert out.sat == 0 and out.val == 0


def test_pipeline_stage0_equals_detect():
    frame, _ = frame_with_disc(70.0, 50.0, 10.0)
    ref = D.detect(red(), frame)
    for stage in range(D.TC_STAGE_MAX + 1):
        t = D.pipeline(red(), frame, stage, D.NO_MODELS)
        # Without models every request runs stage 0 and says so.
        assert t.stage == 0
        assert bytes(t.r) == bytes(ref)
        assert list(t.conf) == [-1.0] * D.BLOB_MAX_REPORT
        assert t.t_px_us == 0 and t.t_total_us == 0       # no clock, no times
    # With the committed colour table, stages 1-3 run stage 1 (no classifier yet).
    assert [D.pipeline(red(), frame, s).stage for s in range(4)] == [0, 1, 1, 1]


def test_pipeline_timed_reports_times():
    frame, _ = frame_with_disc(70.0, 50.0, 10.0)
    t = D.pipeline(red(), frame, 0, timed=True)
    assert t.r.found and t.t_total_us >= t.t_px_us >= 0 and t.t_b_us == 0


def test_c_crc_matches_zlib():
    import zlib
    for data in (b"123456789", b"", bytes(range(256)) * 7):
        assert D.crc32(data) == zlib.crc32(data)
