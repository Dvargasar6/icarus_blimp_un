"""Blob features, crops and the blob dataset (Phase B1): the C code through
ctypes against an independent numpy reference, and blobset's labels and
file format on synthetic frames with known answers."""
import math

import numpy as np
import pytest

from tcview import blobset as B
from tcview import detector as D
from tcview import labels as L
from tcview import protocol as P
from test_detector import frame_with_disc, red
from test_labels import make_set

W, H = 160, 120


def ref_crop(img: np.ndarray, xs: int, ys: int, side: int) -> np.ndarray:
    """blob_crop() written again from its header comment, in numpy."""
    out = np.zeros((24, 24), np.uint16)
    for i in range(24):
        y0, y1 = ys + i * side // 24, ys + (i + 1) * side // 24
        y1 = max(y1, y0 + 1)
        for j in range(24):
            x0, x1 = xs + j * side // 24, xs + (j + 1) * side // 24
            x1 = max(x1, x0 + 1)
            n = (x1 - x0) * (y1 - y0)
            win = img[max(y0, 0):max(min(y1, H), 0), max(x0, 0):max(min(x1, W), 0)]
            win = win.astype(np.uint32)
            r = (int(((win >> 11) & 31).sum()) + n // 2) // n
            g = (int(((win >> 5) & 63).sum()) + n // 2) // n
            b = (int((win & 31).sum()) + n // 2) // n
            out[i, j] = r << 11 | g << 5 | b
    return out


def ref_down16(c: np.ndarray) -> np.ndarray:
    out = np.zeros((16, 16), np.uint16)
    for i in range(16):
        for j in range(16):
            px = np.array([c[(3 * i + a) // 2, (3 * j + b) // 2]
                           for a in range(3) for b in range(3)], np.uint32)
            r, g, b = (int(v.sum()) for v in ((px >> 11) & 31, (px >> 5) & 63, px & 31))
            out[i, j] = (r + 4) // 9 << 11 | (g + 4) // 9 << 5 | (b + 4) // 9
    return out


def test_feature_names_come_from_the_library():
    names = D.feat_names()
    assert len(names) == D.BLOB_FEAT_N == P.FEAT_N
    assert names[0] == "fill" and names[-1] == "log_area"
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("box", [(10, 20, 30), (-7, -5, 18), (150, 110, 24), (0, 0, 12),
                                 (40, 30, 61), (155, -3, 13)])
@pytest.mark.parametrize("be", [1, 0])
def test_crop_matches_numpy_reference(box, be):
    rng = np.random.default_rng(sum(box) + be)
    img = rng.integers(0, 65536, (H, W), dtype=np.uint16)
    data = img.astype(">u2" if be else "<u2").tobytes()
    st = red()
    st.be = be
    assert np.array_equal(D.crop(st, data, box), ref_crop(img, *box))


def test_down16_matches_numpy_reference():
    rng = np.random.default_rng(3)
    c = rng.integers(0, 65536, (24, 24), dtype=np.uint16)
    assert np.array_equal(D.crop_down16(c), ref_down16(c))
    flat = np.full((24, 24), 0xF800 | 0x07E0 // 2, np.uint16)
    assert np.array_equal(D.crop_down16(flat), np.full((16, 16), flat[0, 0], np.uint16))


def test_disc_features_are_what_a_disc_should_give():
    st = red()
    data, _ = frame_with_disc(80, 60, 15)
    tr = D.pipeline(st, data, 0, D.NO_MODELS, features=True)
    assert tr.n_feat == tr.r.n_report == 1
    f = dict(zip(D.feat_names(), tr.features()[0]))
    assert abs(f["fill"] - math.pi / 4) < 0.03         # a disc fills pi/4 of its box
    assert abs(f["log_aspect"]) < 0.05                 # and is as wide as tall
    assert abs(f["log_area"] - math.log(tr.r.b[0].area)) < 1e-4
    assert f["hue_spread"] < 2.0                       # one flat colour
    assert f["ring"] == 0.0                            # grey all around


def test_pipeline_features_equal_blob_features():
    st = red()
    data, _ = frame_with_disc(50, 70, 12)
    img = np.frombuffer(data, ">u2").reshape(H, W).copy()
    img[10:30, 110:150] = img[70, 50]                   # a red rectangle too
    data = img.astype(">u2").tobytes()
    tr = D.pipeline(st, data, 0, D.NO_MODELS, features=True)
    assert tr.n_feat == 2
    for k in range(2):
        assert np.array_equal(tr.features()[k],
                              D.blob_features(st, data, tr.r.b[k], 0, D.NO_MODELS))
    # Without the flag, below stage 2, nothing is computed.
    assert D.pipeline(st, data, 0, D.NO_MODELS).n_feat == 0


def test_crop_box_centres_on_the_blob():
    st = red()
    data, _ = frame_with_disc(80, 60, 10)
    b = D.detect(st, data).b[0]
    xs, ys, side = D.crop_box(b)
    w = b.x1 - b.x0 + 1
    assert side == max((3 * max(w, b.y1 - b.y0 + 1) + 1) // 2, 12)
    assert xs + side / 2 == pytest.approx(b.cx, abs=1.0)
    assert ys + side / 2 == pytest.approx(b.cy, abs=1.0)


# ---- blobset -------------------------------------------------------------------

def test_frame_blobs_are_labelled_from_the_paint(tmp_path):
    make_set(tmp_path)
    s = B.from_folder(tmp_path, stage=0, m=D.NO_MODELS)
    # The unlabelled frame is skipped; the labelled one has the disc (a
    # balloon) and the square (painted background: not a balloon).
    assert sorted(x.label for x in s) == [B.NOT_BALLOON, B.BALLOON]
    disc = next(x for x in s if x.label == B.BALLOON)
    assert disc.ref == tmp_path.name + "/disc_and_square" and disc.source == "frame"
    assert abs(disc.cx - 60) < 1 and abs(disc.cy - 60) < 1
    every = B.from_folder(tmp_path, stage=0, m=D.NO_MODELS, labelled_only=False)
    assert sorted(x.label for x in every) == [B.UNLABELLED, B.NOT_BALLOON, B.BALLOON]
    assert B.counts(every) == {"n": 3, "balloon": 1, "not": 1, "unlabelled": 1}


def test_auto_label_rule():
    lab = np.zeros((H, W), np.uint8)
    assert B.auto_label(lab, 10, 10) == B.UNLABELLED          # nothing painted
    lab[40:50, 40:50] = L.BALLOON
    assert B.auto_label(lab, 45, 45) == B.BALLOON
    assert B.auto_label(lab, 50.5, 45) == B.NOT_BALLOON       # just outside the box
    lab[0:2, 0:1] = L.BALLOON                                 # a stray dab, not a balloon
    assert B.auto_label(lab, 0.5, 0.5) == B.NOT_BALLOON


def test_npz_round_trip(tmp_path):
    make_set(tmp_path)
    s = B.from_folder(tmp_path, stage=0, m=D.NO_MODELS, labelled_only=False)
    rec = P.BlobRec(5, 1234, 0, 1, 1, P.CSV_VERSION, P.FEAT_VERSION, "v-test", 12.5, 30.25, 77,
                    (5, 6, 20, 21), (1, 2, 30), -1.0, np.arange(12, dtype=np.float32),
                    np.arange(576, dtype=np.uint16).reshape(24, 24))
    s.append(B.from_record(rec))
    assert s[-1].label == B.UNLABELLED and s[-1].source == "live"
    s[-1].label = B.BALLOON
    path = tmp_path / "set.npz"
    B.save(path, s)
    z = np.load(path, allow_pickle=False)                    # plain arrays only
    assert z["feat"].shape == (4, 12) and z["crop"].shape == (4, 24, 24)
    assert list(z["feat_names"]) == D.feat_names() and int(z["feat_version"]) == 1
    back = B.load(path)
    for a, b in zip(s, back):
        assert np.array_equal(a.feat, b.feat) and np.array_equal(a.crop, b.crop)
        assert (a.label, a.source, a.ref, a.t_ms, a.k, a.box, a.crop_box, a.fw) == \
               (b.label, b.source, b.ref, b.t_ms, b.k, b.box, b.crop_box, b.fw)
        assert a.cx == pytest.approx(b.cx) and a.conf == pytest.approx(b.conf)
    B.save(tmp_path / "empty.npz", [])
    assert B.load(tmp_path / "empty.npz") == []


def test_npz_keeps_the_white_balance(tmp_path):
    """Fixed gains travel into the set; a set saved before the column existed
    loads as unrecorded rather than as some guess."""
    make_set(tmp_path)
    s = B.from_folder(tmp_path, stage=0, m=D.NO_MODELS, labelled_only=False)
    s[0].wb, s[0].wb_gain = "fixed", (97, 64, 58)
    path = tmp_path / "set.npz"
    B.save(path, s)
    back = B.load(path)
    assert (back[0].wb, back[0].wb_gain) == ("fixed", (97, 64, 58))
    assert all(b.wb == a.wb and b.wb_gain == a.wb_gain for a, b in zip(s, back))
    z = dict(np.load(path, allow_pickle=False))
    del z["wb"], z["wb_gain"]
    np.savez(tmp_path / "old.npz", **z)
    old = B.load(tmp_path / "old.npz")
    assert {(o.wb, o.wb_gain) for o in old} == {("unrecorded", P.UNKNOWN_GAIN)}


def test_mixed_feature_versions_refuse_to_save(tmp_path):
    make_set(tmp_path)
    s = B.from_folder(tmp_path, stage=0, m=D.NO_MODELS)
    s[0].fv = 99
    with pytest.raises(ValueError):
        B.save(tmp_path / "x.npz", s)


def records_for(st, data, t_ms, stage=0, m=D.NO_MODELS, seq=0, fmt_be=True):
    """What the camera would send for this frame, through the wire format."""
    tr = D.pipeline(st, data, stage, m, features=True)
    recs = []
    for k in range(tr.n_feat):
        b = tr.r.b[k]
        box = D.crop_box(b)
        pkt = P.encode_blob(seq + k, t_ms, k, tr.n_feat, list(tr.feat[k].v),
                            D.crop(st, data, box), b.cx, b.cy, b.area,
                            (b.x0, b.y0, b.x1, b.y1), box, stage=tr.stage, conf=tr.conf[k])
        recs += [e for e in P.StreamParser().feed(pkt) if isinstance(e, P.BlobRec)]
    return recs


def test_agreement_check():
    st = red()
    data, _ = frame_with_disc(70, 50, 13)
    frame = P.Frame(1, 500, W, H, "rgb565be", data)
    recs = records_for(st, data, 500)
    a = B.Agreement()
    assert "not" in a.text() or "no record" in a.text()
    assert a.add(recs[0], frame, st, D.NO_MODELS)
    assert a.ok() and a.max_rel < 2e-5
    # A record whose pixels differ from the frame's: the crop no longer agrees.
    bad = recs[0]
    bad.crop = bad.crop.copy()
    bad.crop[0, 0] ^= 1
    bad.feat = bad.feat * 1.01
    a.add(bad, frame, st, D.NO_MODELS)
    assert not a.ok() and a.crop_equal == 1 and a.feat_equal == 1 and a.max_rel > 5e-3


def test_blobs_from_frames_cli(tmp_path, capsys):
    import sys
    from test_labels import TOOLS
    (tmp_path / "set").mkdir()
    make_set(tmp_path / "set")
    sys.path.insert(0, str(TOOLS))
    import blobs_from_frames
    out = tmp_path / "b.npz"
    assert blobs_from_frames.main([str(tmp_path / "set"), "-o", str(out), "--stage", "0"]) == 0
    assert "2 blobs (1 balloon, 1 not, 0 ?)" in capsys.readouterr().out
    assert B.counts(B.load(out))["n"] == 2
    # Overlapping folders: each frame once.
    assert blobs_from_frames.main([str(tmp_path), str(tmp_path / "set"), "-o", str(out),
                                   "--stage", "0"]) == 0
    assert B.counts(B.load(out))["n"] == 2
    assert blobs_from_frames.main([str(tmp_path / "empty"), "-o", str(out)]) == 2
