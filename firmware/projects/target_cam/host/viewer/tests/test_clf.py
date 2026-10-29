"""The stage 2 blob classifier: its record as the trainer writes it and the
camera checks it, the C confidence against the trainer's, stage 2 through the
pipeline, the trainer end to end on the committed frames, and the committed
main/models/clf_model.* agreeing with itself and with the table."""
import json
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
SESSIONS = [TESTDATA / n for n in ("red_s1", "red_s2", "red_s3", "red_s4")]
CLF_H = D.CLF_MODEL.with_suffix(".h")
sys.path.insert(0, str(HOST / "train"))
import train_clf  # noqa: E402


def lut():
    return D.models(D.LUT_MODEL)


def model(w=None, bias=0.0, mean=None):
    n = D.BLOB_FEAT_N
    return train_clf.Model(np.zeros(n) if mean is None else np.asarray(mean, float),
                           np.ones(n), np.zeros(n) if w is None else np.asarray(w, float),
                           bias, 1.0)


def test_record_round_trip_and_table_binding():
    m = lut()
    w = np.linspace(-1, 1, D.BLOB_FEAT_N)
    rec = train_clf.record(model(w, 0.3, np.arange(D.BLOB_FEAT_N)), 0.6, m.lut_id, 1792400000)
    assert len(rec) == 20 + 16 + 12 * D.BLOB_FEAT_N
    clf, tid = D.load_clf(rec)
    assert tid == 1792400000 and clf.lut_id == m.lut_id
    assert clf.threshold == pytest.approx(0.6) and clf.bias == pytest.approx(0.3)
    assert list(clf.w) == pytest.approx(list(w))
    D.add_clf(m, rec)
    assert m.clf and m.clf_id == 1792400000
    # Behind another table: refused when asked for, left out otherwise.
    other = train_clf.record(model(), 0.5, m.lut_id + 1, 1)
    with pytest.raises(D.ModelError, match="behind colour table"):
        D.add_clf(lut(), other)
    assert not D.add_clf(lut(), other, strict=False).clf


def test_damaged_and_unflyable_records_are_refused():
    rec = bytearray(train_clf.record(model(), 0.5, 7, 1))
    for off, why in ((0, "magic"), (9, "size"), (40, "crc")):
        r = bytearray(rec)
        r[off] ^= 1
        with pytest.raises(D.ModelError, match=why):
            D.load_clf(bytes(r))
    for thr in (0.0, 1.0, float("nan")):
        with pytest.raises(D.ModelError, match="value"):
            D.load_clf(train_clf.record(model(), thr, 7, 1))
    # The trainer's CRC is zlib's, which the C check computes the same way.
    assert struct.unpack_from("<I", rec, 16)[0] == zlib.crc32(rec[:16] + rec[20:])


def test_c_confidence_is_the_trainers():
    rng = np.random.default_rng(3)
    x = rng.normal(size=(50, D.BLOB_FEAT_N)) * 3
    mdl = train_clf.fit(x, (x[:, 0] + rng.normal(size=50) > 0).astype(np.int8), 0.1)
    clf, _ = D.load_clf(train_clf.record(mdl, 0.5, 7, 1))
    want = mdl.conf(x)
    got = np.array([D.clf_conf(clf, v) for v in x])
    assert np.abs(got - want).max() < 1e-5
    bad = x[0].copy()
    bad[4] = np.nan
    assert D.clf_conf(clf, bad) == 0.0 and mdl.conf(bad[None])[0] == 0.0


def frames():
    out = []
    for d in SESSIONS:
        for stem in L.list_frames(d):
            sf = L.load_frame(stem)
            if sf.label_path.exists():
                out.append(sf)
    return out


def test_stage_2_believing_everything_is_stage_1_and_believing_nothing_reports_nothing():
    base = lut()
    yes = D.add_clf(lut(), train_clf.record(model(bias=30.0), 0.5, base.lut_id, 1))
    no = D.add_clf(lut(), train_clf.record(model(bias=-30.0), 0.5, base.lut_id, 1))
    n_blobs = 0
    for sf in frames():
        st = sf.settings()
        r1 = D.pipeline(st, sf.frame.data, 1, base)
        ry = D.pipeline(st, sf.frame.data, 2, yes)
        rn = D.pipeline(st, sf.frame.data, 2, no)
        assert ry.stage == 2 and rn.stage == 2
        k = r1.r.n_report
        n_blobs += k
        assert bytes(ry.r) == bytes(r1.r) and ry.n_rejected == 0
        assert all(ry.conf[i] == 1.0 for i in range(k))
        assert rn.r.found == 0 and rn.r.n_report == 0 and rn.n_rejected == k
        assert rn.r.matched == r1.r.matched
        # the pixel test is the table's at every stage from 1
        assert np.array_equal(D.mask(st, sf.frame.data, 2, no), D.mask(st, sf.frame.data, 1, base))
    assert n_blobs > 10


def test_trainer_end_to_end(tmp_path, capsys):
    """On the committed frames: it writes a record the camera accepts behind
    the committed table, and its fold report is in the .json."""
    dirs = [str(d) for d in SESSIONS]
    # Only red_s3 has false blobs, so a fold without it cannot learn: the
    # search refuses and says why rather than fitting nonsense.
    assert train_clf.main(["--out", str(tmp_path), *dirs]) == 2
    assert "only balloon blobs or only false ones" in capsys.readouterr().out
    assert train_clf.main(["--lut", str(D.LUT_MODEL), "--out", str(tmp_path), "--l2", "1",
                           "--threshold", "0.3", "--train-id", "1792400001", *dirs]) == 0
    out = capsys.readouterr().out
    assert "final classifier" in out
    rec = (tmp_path / "clf_model.bin").read_bytes()
    m = D.add_clf(lut(), rec)
    assert m.clf_id == 1792400001
    assert "clf_model_rec[%d]" % len(rec) in (tmp_path / "clf_model.h").read_text()
    meta = json.loads((tmp_path / "clf_model.json").read_text())
    assert meta["lut_id"] == m.lut_id and meta["sessions"] == [d.name for d in SESSIONS]
    assert set(meta["weights"]) == set(D.feat_names())
    # One session is enough with fixed parameters, if it has both kinds.
    assert train_clf.main(["--out", str(tmp_path / "one"), "--l2", "1", "--threshold", "0.3",
                           dirs[2]]) == 0


def synthetic_session(name, rng, n=12):
    """Frames with one painted balloon and two candidate blobs, one in it and
    one outside; feature 0 tells them apart (noisily), the rest is noise."""
    out = []
    for i in range(n):
        lab = np.full((120, 160), L.BACKGROUND, np.uint8)
        has = i % 3 != 0
        if has:
            lab[20:50, 20:50] = L.BALLOON
        cents = ([(35.0, 35.0)] if has else []) + [(120.0, 90.0)]
        y = np.array(([1] if has else []) + [0], np.int8)
        feat = rng.normal(size=(len(cents), D.BLOB_FEAT_N)).astype(np.float32)
        feat[:, 0] += np.where(y == 1, 2.0, -2.0)
        out.append(train_clf.FrameBlobs(name, "f%d" % i, lab, np.zeros((120, 160), np.uint8),
                                        cents, feat, y, int(has)))
    return out


def test_search_lowers_the_cost_when_a_feature_separates():
    rng = np.random.default_rng(1)
    sessions = {n: synthetic_session(n, rng) for n in ("a", "b", "c")}
    base = sum(train_clf.tally(fr, train_clf.keep_all)["cost"] for fr in sessions.values())
    rows = train_clf.search(sessions, train_clf.L2S, train_clf.THRESHOLDS)
    assert base > 0
    assert rows[0]["cost"] <= base // 4


def test_same_folder_names_are_kept_apart(tmp_path):
    a, b = tmp_path / "test" / "red_s1", tmp_path / "train" / "x" / "red_s1"
    assert train_clf.session_names([a, b]) == ["test/red_s1", "x/red_s1"]
    assert train_clf.session_names([a, tmp_path / "red_s2"]) == ["red_s1", "red_s2"]


def test_committed_classifier_header_agrees():
    """Either the placeholder (no classifier) exactly as the trainer would
    write it back, or a record whose .h and .bin are the same bytes and which
    belongs to the committed table."""
    text = CLF_H.read_text()
    if "CLF_MODEL_NONE" in text:
        assert text == train_clf.NONE_HEADER
        assert not D.CLF_MODEL.exists()
        return
    rec = D.CLF_MODEL.read_bytes()
    assert "clf_model_rec[%d]" % len(rec) in text
    body = text.split("{", 1)[1].split("}", 1)[0]
    assert bytes(int(x, 16) for x in body.replace(",", " ").split()) == rec
    clf, _ = D.load_clf(rec)
    assert clf.lut_id == lut().lut_id, "main/models holds a classifier for another table"


# ---- blob sets (the Blobs tab's .npz) as sessions ------------------------------

def blob_set(path, rng, lut_id, stage=1, frames=20, gain=(76, 64, 75), unlabelled=0):
    """A labelled blob set as the Blobs tab saves it: per frame a balloon
    blob (k 0, feature 0 high) and a false one (k 1, low)."""
    from tcview import blobset as B
    out = []
    for i in range(frames):
        for k, lab in enumerate((1, 0)):
            f = rng.normal(0, 1, D.BLOB_FEAT_N).astype(np.float32)
            f[0] = (3.0 if lab else -3.0) + rng.normal(0, 0.3)
            out.append(B.Sample(f, np.zeros((24, 24), np.uint16),
                                -1 if i < unlabelled else lab, "live", "t%d" % (1000 + i),
                                1000 + i, k, stage, 80.0 + k, 60.0, 100, (0, 0, 9, 9),
                                (0, 0, 24), -1.0, "test", wb="fixed", wb_gain=gain,
                                lut_id=lut_id))
    B.save(path, out)
    return path


def test_blob_set_lut_id_round_trip_and_old_files(tmp_path):
    from tcview import blobset as B
    p = blob_set(tmp_path / "a.npz", np.random.default_rng(0), 1234)
    assert {s.lut_id for s in B.load(p)} == {1234}
    # A file from before the column: loads as "not recorded".
    z = dict(np.load(p, allow_pickle=False))
    del z["lut_id"]
    np.savez(tmp_path / "old.npz", **z)
    assert {s.lut_id for s in B.load(tmp_path / "old.npz")} == {B.UNKNOWN_LUT}


def test_blob_sets_train_and_are_checked(tmp_path, capsys):
    rng = np.random.default_rng(1)
    lid = lut().lut_id
    a = blob_set(tmp_path / "s1.npz", rng, lid)
    b = blob_set(tmp_path / "s2.npz", rng, lid, unlabelled=3)
    out = tmp_path / "out"
    assert train_clf.main(["--out", str(out), "--train-id", "5", str(a), str(b)]) == 0
    log = capsys.readouterr().out
    assert "3 of 20 frames left out" in log
    meta = json.loads((out / "clf_model.json").read_text())
    assert meta["session_kinds"] == {"s1": "blobs", "s2": "blobs"}
    assert meta["search"][0]["cost"] == 0           # feature 0 separates them
    assert meta["stage1_held_out"]["false_b1"] == 0
    assert meta["stage1_held_out"]["wrong_b1"] == 0     # the balloon is blob 1 here
    assert D.add_clf(lut(), (out / "clf_model.bin").read_bytes()).clf

    # Behind another table, from HSV, or from mixed gains: refused or warned.
    c = blob_set(tmp_path / "other.npz", rng, lid + 1)
    assert train_clf.main(["--out", str(out), str(a), str(c)]) == 2
    assert "behind colour table %d" % (lid + 1) in capsys.readouterr().out
    h = blob_set(tmp_path / "hsv.npz", rng, lid, stage=0)
    assert train_clf.main(["--out", str(out), str(a), str(h)]) == 2
    assert "stage 0 (HSV)" in capsys.readouterr().out
    assert train_clf.main(["--out", str(out), "--allow-stage0", str(a), str(h)]) == 0
    g = blob_set(tmp_path / "g.npz", rng, lid, gain=(64, 64, 80))
    assert train_clf.main(["--out", str(out), str(a), str(g)]) == 0
    assert "mix white-balance gains" in capsys.readouterr().out


def test_blob_set_frames_score_from_labels():
    f = train_clf.SetFrame("s", "t1", [(0, 0)] * 3, np.zeros((3, D.BLOB_FEAT_N), np.float32),
                           np.array([0, 1, 1], np.int8))
    s = f.score((0, 1, 2))
    assert (s.n_balloons, s.tp, s.fn, s.fp, s.b1) == (2, 2, 0, 1, "false")
    s = f.score((2,))
    assert (s.tp, s.fn, s.fp, s.b1) == (1, 1, 0, "balloon")
    assert f.score(()).b1 == "none"


def test_settings_that_lose_balloons_are_excluded():
    """Rejecting everything must not win just because stage 1 had many false
    blob 1s: a setting losing more than max_lost of stage 1's balloons is
    ranked after every other one."""
    base = dict(tp=10, fn=0)
    drop_all = dict(cost=10, fn=10, fp=0, threshold=0.95, l2=1.0)
    useful = dict(cost=12, fn=1, fp=3, threshold=0.5, l2=1.0)
    rows = train_clf.exclude_lossy([drop_all, useful], base, 0.10)
    assert rows[0] is useful and not useful["excluded"] and drop_all["excluded"]
    assert train_clf.exclude_lossy([dict(drop_all)], base, 1.0)[0]["excluded"] is False
