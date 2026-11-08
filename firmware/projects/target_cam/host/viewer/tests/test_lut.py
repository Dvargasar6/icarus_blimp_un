"""The stage 1 colour table: the plumbing proved with a table that copies the
HSV test, the model record refused when damaged, the committed model agreeing
with itself and with its trainer, and the trained table no worse than the
HSV window on the labelled frames."""
import re
import struct
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

from tcview import detector as D
from tcview import labels as L

HOST = Path(__file__).resolve().parents[2]
TESTDATA = HOST / "testdata"
SESSIONS = [TESTDATA / "red_s1", TESTDATA / "red_s2"]
MODEL_H = D.LUT_MODEL.with_suffix(".h")
sys.path.insert(0, str(HOST / "train"))
import train_lut  # noqa: E402


def labelled():
    out = []
    for d in SESSIONS:
        for stem in L.list_frames(d):
            sf = L.load_frame(stem)
            out.append((sf, L.load_labels(sf.label_path, sf.frame.w, sf.frame.h)))
    assert len(out) == 20
    return out


def totals(scores):
    return {k: sum(getattr(s, k) for s in scores)
            for k in ("tp", "fn", "fp", "split", "px_tp", "px_fp", "px_fn")}


def test_hsv_table_reproduces_stage_0_on_recorded_frames():
    """Stage 1 with the table made from a frame's own HSV settings must score
    exactly as stage 0 does: same blobs, same mask. Real frames, not synthetic
    ones, so the byte order and every colour the camera produced are covered."""
    for sf, lab in labelled():
        st = sf.settings()
        m = D.lut_models(bits=D.hsv_table(st))
        s0 = L.score_frame(sf, lab, 0, st, D.NO_MODELS)
        s1 = L.score_frame(sf, lab, 1, st, m)
        assert s1.stage_eff == 1
        assert (s0.tp, s0.fn, s0.fp, s0.split, s0.b1, s0.px_tp, s0.px_fp, s0.px_fn) == \
               (s1.tp, s1.fn, s1.fp, s1.split, s1.b1, s1.px_tp, s1.px_fp, s1.px_fn), sf.stem.name
        assert np.array_equal(D.mask(st, sf.frame.data, 0), D.mask(st, sf.frame.data, 1, m))


def test_damaged_records_are_refused():
    rec = D.LUT_MODEL.read_bytes()
    D.lut_models(record=rec)                                  # the intact one loads
    bad = {
        "crc": rec[:100] + bytes([rec[100] ^ 1]) + rec[101:],     # one payload bit
        "magic": b"XCLU" + rec[4:],
        "version": rec[:4] + struct.pack("<H", 2) + rec[6:],
        "short": rec[:-1],
    }
    for why, r in bad.items():
        with pytest.raises(D.ModelError, match=why):
            D.lut_models(record=r)


def test_without_a_table_stage_1_falls_back_to_hsv():
    sf, lab = labelled()[0]
    st = sf.settings()
    s = L.score_frame(sf, lab, 1, st, D.NO_MODELS)
    assert s.stage_eff == 0
    assert np.array_equal(D.mask(st, sf.frame.data, 1, D.NO_MODELS),
                          D.mask(st, sf.frame.data, 0))


def test_committed_header_and_bin_are_the_same_record():
    rec = D.LUT_MODEL.read_bytes()
    body = MODEL_H.read_text().split("{", 1)[1].split("}", 1)[0]
    from_h = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-f]{2})", body))
    assert from_h == rec
    assert "lut_model_rec[%d]" % len(rec) in MODEL_H.read_text()
    # Its CRC is the standard CRC-32 (zlib) over header bytes 0..15 + payload.
    assert struct.unpack_from("<I", rec, 16)[0] == zlib.crc32(rec[:16] + rec[20:])
    assert D.crc32(rec[:16] + rec[20:]) == zlib.crc32(rec[:16] + rec[20:])


def test_trainer_reproduces_the_committed_table(tmp_path):
    """The .json records how the table was made; rerunning the trainer with
    those parameters and that id must give the same bytes. A table nobody can
    regenerate cannot be retrained at the venue with confidence.

    The sessions are looked up by name in host/testdata/, the committed copy,
    because data/ is not in git. A table trained on a new session therefore
    fails here until that session is copied into host/testdata/."""
    import json
    meta = json.loads(D.LUT_MODEL.with_suffix(".json").read_text())
    dirs = []
    for name in meta["sessions"]:
        d = TESTDATA / name
        if not d.is_dir():
            pytest.fail("the committed table was trained on session %s, which is not in "
                        "host/testdata/. Copy the labelled folder there "
                        "(cp -r data/%s host/testdata/) so the table can be rebuilt from "
                        "the repository." % (name, name))
        have = sorted(stem.name for stem in L.list_frames(d)
                      if L.load_frame(stem).label_path.exists())
        assert have == sorted(meta["frames"][name]), (
            "host/testdata/%s does not hold the frames the table was trained on" % name)
        dirs.append(str(d))
    assert train_lut.main([
        "--sigma", str(meta["sigma"]), "--k", str(meta["k"]),
        "--min-evidence", str(meta["min_evidence"]), "--train-id", str(meta["train_id"]),
        "--out", str(tmp_path), *dirs]) == 0
    assert (tmp_path / "lut_model.bin").read_bytes() == D.LUT_MODEL.read_bytes()
    assert (tmp_path / "lut_model.h").read_text() == MODEL_H.read_text()


def test_trained_table_is_no_worse_than_the_hsv_window():
    """A regression guard, not evidence: these are the frames the table was
    trained on. The held-out figures are in docs/learned-detector-report.md."""
    m = D.models()
    assert m.lut and m.lut_id
    frames = labelled()
    s0 = [L.score_frame(sf, lab, 0, None, D.NO_MODELS) for sf, lab in frames]
    s1 = [L.score_frame(sf, lab, 1, None, m) for sf, lab in frames]
    t0, t1 = totals(s0), totals(s1)
    assert all(s.stage_eff == 1 for s in s1)
    assert t1["fn"] <= t0["fn"]
    assert t1["fp"] <= t0["fp"]
    assert sum(s.b1 == "false" for s in s1) == 0
    f1 = lambda t: 2 * t["px_tp"] / (2 * t["px_tp"] + t["px_fp"] + t["px_fn"])
    assert f1(t1) >= f1(t0)


def test_table_blur_is_round_in_8_bit_units():
    """One count at the grid's centre, blurred: equal spread in 8-bit units
    along red, green and blue, although green has twice the levels."""
    c = np.zeros(65536)
    r, g, b = 16, 32, 16
    c[(r << 11) | (g << 5) | b] = 1.0
    s = train_lut.smooth(c, 16.0).reshape(32, 64, 32)
    assert s[r, g, b] == pytest.approx(1.0)                   # peak keeps its weight
    # ~16 units away along each axis: 2 red/blue steps (16.5) vs 4 green (16.2).
    assert s[r + 2, g, b] == pytest.approx(s[r, g, b + 2])
    assert s[r, g + 4, b] == pytest.approx(s[r + 2, g, b], rel=0.05)
