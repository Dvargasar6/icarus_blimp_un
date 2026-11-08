"""The stream splitter, on text, CSV and framed binary split at random read
boundaries, plus the ways a frame can arrive broken, and the #!BLOB records of
'!collect 1'."""
import random

import numpy as np
import pytest

from tcview import protocol as P

ROW = ("12345,1,80.50,60.25,0.17,-0.08,123,14,13,0.68,1520,0,1,140,"
       "0.00,0.00,-1,0.00,0.00,-1,0.00,0.00,-1,100.0,8200")


def payload(seed=0, n=160 * 120 * 2):
    rng = random.Random(seed)
    data = bytearray(rng.randrange(256) for _ in range(n))
    # Make sure the awkward bytes are in there: newlines and a fake header.
    data[100:102] = b"\r\n"
    data[500:509] = b"#!FRAME s"
    data[1000] = 0x0A
    return bytes(data)


def stream(frame_data):
    return (b"# ---- target_cam boot ----\r\n"
            b"#!CFG w=160 h=120 cell=2 hue=0 tol=12 sat=110 val=50 area=25 mm=550 "
            b"fov=54.0 fps=12.5 period_ms=80 awb=1 be=1 preset=red baud=115200 image=0 "
            b"stream=1 "
            b"settings=defaults\r\n"
            b"#!ACK g\r\n"
            + P.encode_frame(7, 12345, frame_data)
            + ROW.encode() + b"\r\n"
            b"#!NAK H999 -- out of range (0..359)\r\n"
            b"#!BAUD 921600\r\n"
            b"#!CAL refused hue=304 spread=109 sat=78 val=188\r\n")


def feed_chunked(parser, data, rng):
    events = []
    i = 0
    while i < len(data):
        k = rng.choice([1, 2, 3, 7, 64, 1000, 5000, 40000])
        events += parser.feed(data[i:i + k])
        i += k
    return events


def kinds(events):
    return [type(e).__name__ for e in events]


def test_crc_matches_firmware_check_value():
    # esp_rom_crc32_le(0, "123456789", 9), printed by link_init() at boot.
    assert P.crc32(b"123456789") == 0xCBF43926


@pytest.mark.parametrize("seed", range(20))
def test_mixed_stream_any_chunking(seed):
    data = payload(seed)
    ev = feed_chunked(P.StreamParser(), stream(data), random.Random(seed))
    assert kinds(ev) == ["Comment", "Cfg", "Ack", "Frame", "End", "Row", "Nak", "Baud", "Cal"]
    cfg, ack, frame, end, row, nak, baud, cal = ev[1:]
    assert cfg.v["hue"] == 0 and cfg.v["fov"] == 54.0 and cfg.v["preset"] == "red"
    assert cfg.v["settings"] == "defaults"
    # 12.5 FPS is the sensor's own rate; it must not be truncated to 12.
    assert cfg.v["fps"] == 12.5 and cfg.v["period_ms"] == 80
    assert ack.cmd == "g"
    assert frame.data == data and frame.seq == 7 and frame.t_ms == 12345
    assert frame.big_endian
    assert end.seq == 7
    assert row.v["t_ms"] == 12345 and row.v["ok"] == 1 and row.v["proc_us"] == 8200
    assert nak.cmd == "H999" and nak.reason == "out of range (0..359)"
    assert baud.baud == 921600
    assert not cal.ok and cal.v["hue"] == 304


def test_log_line_inside_payload_drops_frame_and_resyncs():
    data = payload(1)
    pkt = P.encode_frame(3, 500, data)
    k = len(pkt) // 2
    broken = pkt[:k] + b"W (1234) cam_hal: EV-VSYNC-OVF\r\n" + pkt[k:]
    p = P.StreamParser()
    ev = p.feed(broken + ROW.encode() + b"\r\n" + P.encode_frame(4, 600, data))
    assert isinstance(ev[0], P.FrameError) and ev[0].reason == "CRC mismatch"
    assert any(isinstance(e, P.Row) for e in ev), "the row after the broken frame survives"
    good = [e for e in ev if isinstance(e, P.Frame)]
    assert len(good) == 1 and good[0].seq == 4 and good[0].data == data
    assert p.frames_bad == 1 and p.frames_ok == 1


def test_truncated_frame_expires_and_text_after_it_is_recovered():
    data = payload(2)
    pkt = P.encode_frame(5, 700, data)
    cut = pkt[:2000]                       # the camera reset mid-frame...
    reboot = b"\r\n\r\n# ---- target_cam boot ----\r\n# reset reason  power-on\r\n"
    p = P.StreamParser()
    ev = p.feed(cut + reboot, now=0.0)
    assert ev == [] and p.in_frame         # the boot text is inside the "payload" for now
    assert p.expire(now=1.0, baud=115200) == []        # not overdue yet
    ev = p.expire(now=10.0, baud=115200)
    assert isinstance(ev[0], P.FrameError) and "truncated" in ev[0].reason
    texts = [e.text for e in ev if isinstance(e, P.Comment)]
    assert "# ---- target_cam boot ----" in texts and "# reset reason  power-on" in texts
    assert not p.in_frame


def test_bad_headers_are_junk_not_frames():
    p = P.StreamParser()
    ev = p.feed(b"#!FRAME seq=1 t_ms=2 w=160 h=120 fmt=rgb565be len=99 crc=0\r\n"
                b"#!FRAME seq=x\r\n" + ROW.encode() + b"\r\n")
    assert kinds(ev) == ["Junk", "Junk", "Row"]
    assert not p.in_frame


def test_rows_with_wrong_column_count_are_not_rows():
    assert P.parse_row(ROW) is not None
    assert P.parse_row(ROW + ",1") is None
    assert not isinstance(P.parse_line(ROW[:-5]), P.Row)


ROW_V2 = ROW + ",2,0.873,-1,-1,-1,4512,0,0,21340,2"


def test_v2_row_parses_with_its_new_columns():
    r = P.parse_row(ROW_V2)
    assert r is not None and len(P.COLUMNS_V2) == 35
    assert r["csv_v"] == 2 and r["stage"] == 2 and r["conf"] == 0.873
    assert r["b2_conf"] == -1 and r["t_px_us"] == 4512 and r["loop_us"] == 21340
    # The v1 columns keep their indices, so the old fields read the same.
    v1 = P.parse_row(ROW)
    assert all(r[k] == v1[k] for k in P.COLUMNS_V1)


def test_v1_row_is_filled_honestly():
    import math
    r = P.parse_row(ROW)
    assert r["csv_v"] == 1 and r["stage"] == 0 and r["conf"] == -1
    # Not measured by v1 firmware: NaN, never a reassuring 0.
    assert all(math.isnan(r[k]) for k in ("t_px_us", "t_b_us", "t_c_us", "loop_us"))


def test_v2_length_row_with_another_version_is_refused():
    assert P.parse_row(ROW_V2[:-1] + "3") is None
    assert P.parse_row(ROW_V2 + ",1") is None
    assert P.parse_row(ROW_V2.rsplit(",", 1)[0]) is None      # 34 columns


def test_v2_row_in_a_stream():
    ev = P.StreamParser().feed(ROW_V2.encode() + b"\r\n" + ROW.encode() + b"\r\n")
    assert kinds(ev) == ["Row", "Row"]
    assert ev[0].v["csv_v"] == 2 and ev[1].v["csv_v"] == 1


def test_binary_noise_without_newline_is_bounded():
    p = P.StreamParser()
    ev = p.feed(bytes(range(11, 256)) * 40)      # no 0x0A at all
    assert len(ev) == 1 and isinstance(ev[0], P.Junk)


def test_little_endian_frame_decodes_by_its_label():
    import numpy as np
    px = np.array([[0xF800, 0x07E0], [0x001F, 0xFFFF]], dtype=np.uint16)
    for be in (True, False):
        raw = px.astype(">u2" if be else "<u2").tobytes()
        f = P.Frame(0, 0, 2, 2, "rgb565be" if be else "rgb565le", raw)
        rgb = f.rgb()
        assert tuple(rgb[0, 0]) == (255, 0, 0)
        assert tuple(rgb[0, 1]) == (0, 255, 0)
        assert tuple(rgb[1, 0]) == (0, 0, 255)
        assert tuple(rgb[1, 1]) == (255, 255, 255)


def test_crc_that_looks_like_a_float_is_still_a_frame():
    # "76555e46" parses as 7.6555e+50; the header must not turn into junk.
    p = P.StreamParser()
    ev = p.feed(b"#!FRAME seq=1 t_ms=2 w=1 h=1 fmt=rgb565be len=2 crc=76555e46\r\n\x00\x00")
    assert len(ev) == 1 and isinstance(ev[0], P.FrameError) and ev[0].reason == "CRC mismatch"


# ---- #!BLOB records ('!collect 1') -------------------------------------------

def blob_packet(seq=9, t_ms=12345, k=1, crop_seed=0, feat=None):
    rng = random.Random(crop_seed)
    crop = np.array([rng.randrange(65536) for _ in range(P.CROP * P.CROP)],
                    np.uint16).reshape(P.CROP, P.CROP)
    crop[0, :3] = [0x0A0D, 0x0D0A, 0x0A0A]         # newline bytes in the payload
    feat = feat if feat is not None else [0.613, -0.37, 11.214, 160.092, 1e-7, 0, -0.0,
                                          25.443, 24.335, 0.0, 0.384, 7.156]
    pkt = P.encode_blob(seq, t_ms, k, 2, feat, crop, 80.5, 60.25, 1282, (60, 40, 101, 80),
                        (49, 28, 63), stage=1, conf=-1, fw="v-test")
    return pkt, crop, np.array(feat, np.float32)


def test_blob_record_round_trip():
    pkt, crop, feat = blob_packet()
    p = P.StreamParser()
    ev = p.feed(pkt)
    assert kinds(ev) == ["BlobRec", "End"]
    r = ev[0]
    assert (r.seq, r.t_ms, r.k, r.n, r.stage, r.csv, r.fv, r.fw) == \
           (9, 12345, 1, 2, 1, P.CSV_VERSION, P.FEAT_VERSION, "v-test")
    assert (r.cx, r.cy, r.area, r.box, r.crop_box, r.conf) == \
           (80.5, 60.25, 1282, (60, 40, 101, 80), (49, 28, 63), -1.0)
    assert np.array_equal(r.crop, crop) and r.crop.dtype == np.uint16
    assert np.allclose(r.feat, feat, rtol=1e-6, atol=0) and r.feat.shape == (P.FEAT_N,)
    assert r.rgb().shape == (P.CROP, P.CROP, 3)
    assert (p.blobs_ok, p.blobs_bad, p.frames_ok, p.frames_bad) == (1, 0, 0, 0)


def test_broken_blob_is_a_blob_error_and_resyncs():
    pkt, _, _ = blob_packet()
    k = len(pkt) - 300
    broken = pkt[:k] + b"W (1) x\r\n" + pkt[k:]
    p = P.StreamParser()
    ev = p.feed(broken + ROW.encode() + b"\r\n" + blob_packet(seq=10)[0])
    err = [e for e in ev if isinstance(e, P.FrameError)]
    assert len(err) == 1 and err[0].kind == "BLOB" and err[0].reason == "CRC mismatch"
    assert any(isinstance(e, P.Row) for e in ev)
    assert [e.seq for e in ev if isinstance(e, P.BlobRec)] == [10]
    assert (p.blobs_ok, p.blobs_bad, p.frames_bad) == (1, 1, 0)


@pytest.mark.parametrize("seed", range(10))
def test_frame_row_and_blobs_any_chunking(seed):
    """The order '!collect 1' plus 'v1' puts on the wire: frame, row, records."""
    data = payload(seed)
    b0, c0, _ = blob_packet(seq=8, k=0, crop_seed=seed)
    b1, c1, _ = blob_packet(seq=9, k=1, crop_seed=seed + 100)
    wire = P.encode_frame(7, 12345, data) + ROW.encode() + b"\r\n" + b0 + b1
    p = P.StreamParser()
    ev = [e for e in feed_chunked(p, wire, random.Random(seed)) if not isinstance(e, P.End)]
    assert kinds(ev) == ["Frame", "Row", "BlobRec", "BlobRec"]
    assert ev[0].data == data
    assert np.array_equal(ev[2].crop, c0) and np.array_equal(ev[3].crop, c1)
    assert ev[0].t_ms == ev[1].v["t_ms"] == ev[2].t_ms == ev[3].t_ms
    assert (p.frames_ok, p.blobs_ok, p.frames_bad, p.blobs_bad) == (1, 2, 0, 0)


def test_blob_header_with_the_wrong_crop_size_is_not_a_record():
    pkt, _, _ = blob_packet()
    ev = P.StreamParser().feed(pkt.replace(b"cw=24", b"cw=16", 1))
    assert not any(isinstance(e, P.BlobRec) for e in ev)


# ---- white balance (report §10.8) --------------------------------------------

def test_blob_record_carries_its_white_balance():
    pkt, _, _ = blob_packet()
    assert P.StreamParser().feed(pkt)[0].wb == "auto"
    _, crop, feat = blob_packet()
    pkt = P.encode_blob(9, 12345, 1, 2, feat, crop, 80.5, 60.25, 1282, (60, 40, 101, 80),
                        (49, 28, 63), wb="fixed", wb_gain=(97, 64, 58))
    r = P.StreamParser().feed(pkt)[0]
    assert (r.wb, r.wb_gain) == ("fixed", (97, 64, 58))
    assert P.wb_text(r.wb, r.wb_gain) == "fixed 97,64,58"


def test_wb_of_reads_new_and_old_configs():
    """Firmware with fixed gains sends wb=; older firmware only awb=, whose
    gains were never known; a saved frame from before either has neither."""
    assert P.wb_of({"awb": 2, "wb": "fixed", "wb_gain": "97,64,58"}) == ("fixed", (97, 64, 58))
    assert P.wb_of({"awb": 1, "wb": "auto", "wb_gain": "0,0,0"}) == ("auto", (0, 0, 0))
    assert P.wb_of({"awb": 0}) == ("frozen", P.UNKNOWN_GAIN)
    assert P.wb_of({"awb": 1}) == ("auto", P.UNKNOWN_GAIN)
    assert P.wb_of({}) == ("unrecorded", P.UNKNOWN_GAIN)


def test_wbcal_lines():
    ok, bad, cfg = P.StreamParser().feed(
        b"#!WBCAL ok r=97 g=64 b=58 R=181 G=180 B=182 steps=4\r\n"
        b"#!WBCAL refused reason=clipped R=255 G=255 B=250 steps=0\r\n"
        b"#!CFG w=160 h=120 awb=2 wb=fixed wb_gain=97,64,58 wb_fixed=97,64,58\r\n")
    assert isinstance(ok, P.WbCal) and ok.status == "ok" and ok.v["r"] == 97
    assert bad.status == "refused" and bad.v["reason"] == "clipped"
    assert P.wb_of(cfg.v) == ("fixed", (97, 64, 58))
