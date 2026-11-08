"""The stage 3 crop CNN: the trainer's gradients, the integer network against
blob_cnn.c bit for bit, the record checks, stage 3 through the pipeline, and
the trainer end to end on the committed frames."""
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
sys.path.insert(0, str(HOST / "train"))
import train_clf  # noqa: E402
import train_cnn as T  # noqa: E402


def small_net(n=16, seed=1):
    return T.Net(n, (2, 3, 3), seed)


def test_gradients_match_finite_differences():
    rng = np.random.default_rng(0)
    net = small_net(8)
    x = rng.random((3, 3, 8, 8))
    y = np.array([1.0, 0.0, 1.0])
    sw = np.array([1.0, 2.0, 1.0])
    _, g = T.loss_grad(net, x, y, sw, 1e-3)
    for k in ("w1", "b1", "w2", "w3", "wd", "bd"):
        p = net.p[k]
        for _ in range(4):
            i = tuple(rng.integers(0, s) for s in p.shape)
            old = p[i]
            p[i] = old + 1e-5
            lp, _ = T.loss_grad(net, x, y, sw, 1e-3)
            p[i] = old - 1e-5
            lm, _ = T.loss_grad(net, x, y, sw, 1e-3)
            p[i] = old
            num = (lp - lm) / 2e-5
            assert abs(num - g[k][i]) <= 1e-5 + 1e-3 * abs(num), (k, i, num, g[k][i])


def random_q(n=16, seed=0):
    """A trained-shape integer network from a random float one."""
    rng = np.random.default_rng(seed)
    net = T.Net(n, T.CHANNELS, seed)
    calib = rng.integers(0, 256, (20, 3, n, n), dtype=np.uint8)
    return T.quantise(net, calib, 0.5), rng


@pytest.mark.parametrize("n", (16, 24))
def test_integer_network_is_blob_cnn_bit_for_bit(n):
    q, rng = random_q(n)
    rec = T.record(q, 1234, 99)
    cnn, tid = D.load_cnn(rec)
    assert tid == 99 and cnn.lut_id == 1234 and cnn.n == n
    # Crops as RGB565 (any 16-bit value), through the camera's conversion.
    crops = rng.integers(0, 65536, (30, n, n), dtype=np.uint16)
    from tcview import protocol as P
    x = np.array([P.rgb565_to_rgb888(c).transpose(2, 0, 1) for c in crops])
    want = T.int_forward(q, x)
    got = np.array([D.cnn_sum(cnn, c) for c in crops])
    assert np.array_equal(got, want)
    assert np.allclose([D.cnn_conf(cnn, c) for c in crops], T.int_conf(q, x), atol=1e-6)
    # The trainer's (3, n, n) form maps back to the same RGB565 values.
    assert np.array_equal([D.cnn_sum(cnn, xi) for xi in x], want)
    assert D.cnn_macs(cnn) == n * n * 8 * 27 + (n // 2) ** 2 * 16 * 8 * 9 + (n // 4) ** 2 * 16 * 16 * 9 + 16


def test_damaged_and_unflyable_records_are_refused():
    q, _ = random_q()
    rec = T.record(q, 7, 1)
    assert struct.unpack_from("<I", rec, 16)[0] == zlib.crc32(rec[:16] + rec[20:])
    for off, why in ((0, "magic"), (30, "crc")):
        r = bytearray(rec)
        r[off] ^= 1
        with pytest.raises(D.ModelError, match=why):
            D.load_cnn(bytes(r))
    for bad in (dict(threshold=1.0), dict(threshold=0.0), dict(n=20)):
        q2 = dict(q, **bad)
        with pytest.raises(D.ModelError, match="value"):
            D.load_cnn(T.record(q2, 7, 1))


def lut_and_clf(bias=30.0):
    m = D.models(D.LUT_MODEL)
    n = D.BLOB_FEAT_N
    rec = train_clf.record(train_clf.Model(np.zeros(n), np.ones(n), np.zeros(n), bias, 1.0),
                           0.5, m.lut_id, 1)
    return D.add_clf(m, rec)


def cnn_record(believe: bool, lut_id: int) -> bytes:
    """A network that believes every crop (dense bias huge) or none."""
    q, _ = random_q()
    q = dict(q, dense_w=np.zeros_like(q["dense_w"]),
             dense_b=10 ** 6 if believe else -10 ** 6, logit_scale=1.0)
    return T.record(q, lut_id, 5)


def frames():
    out = []
    for d in ("red_s3",):
        for stem in L.list_frames(TESTDATA / d):
            sf = L.load_frame(stem)
            out.append(sf)
    return out


def test_stage_3_through_the_pipeline():
    yes = D.add_cnn(lut_and_clf(), cnn_record(True, D.models(D.LUT_MODEL).lut_id), cnn_k=2)
    no = D.add_cnn(lut_and_clf(), cnn_record(False, D.models(D.LUT_MODEL).lut_id), cnn_k=2)
    two = lut_and_clf()
    seen = 0
    for sf in frames():
        st = sf.settings()
        r2 = D.pipeline(st, sf.frame.data, 2, two)
        ry = D.pipeline(st, sf.frame.data, 3, yes)
        rn = D.pipeline(st, sf.frame.data, 3, no)
        assert ry.stage == 3 and rn.stage == 3
        k = r2.r.n_report
        seen += k
        assert ry.r.n_report == k and ry.n_cnn == min(k, 2) and ry.n_rejected3 == 0
        # Believing nothing drops the two largest; the rest keep stage 2's verdict.
        assert rn.r.n_report == max(0, k - 2) and rn.n_rejected3 == min(k, 2)
        for i in range(rn.r.n_report):
            assert rn.r.b[i].area == r2.r.b[i + 2].area
            assert rn.conf[i] == pytest.approx(r2.conf[i + 2])
    assert seen > 10
    # A CNN of another table is refused, or left out.
    with pytest.raises(D.ModelError, match="behind colour table"):
        D.add_cnn(lut_and_clf(), cnn_record(True, 1))
    m = D.add_cnn(lut_and_clf(), cnn_record(True, 1), strict=False)
    assert not m.cnn and D.pipeline(frames()[0].settings(), frames()[0].frame.data, 3, m).stage == 2


def test_trainer_end_to_end(tmp_path, capsys):
    dirs = [str(TESTDATA / n) for n in ("red_s3", "red_s4")]
    clf_dir = tmp_path / "set"
    clf_dir.mkdir()
    # A classifier that keeps everything, so stage 3's scoring sees all blobs.
    m = lut_and_clf()
    n = D.BLOB_FEAT_N
    (clf_dir / "clf_model.bin").write_bytes(train_clf.record(
        train_clf.Model(np.zeros(n), np.ones(n), np.zeros(n), 30.0, 1.0), 0.5, m.lut_id, 1))
    rc = T.main(["--lut", str(D.LUT_MODEL), "--clf", str(clf_dir / "clf_model.bin"),
                 "--out", str(tmp_path / "out"), "--epochs", "3", "--train-id", "17",
                 "--threshold", "0.5", dirs[0]])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "integer vs float" in out and "multiply-adds per crop" in out
    rec = (tmp_path / "out" / "cnn_model.bin").read_bytes()
    cnn, tid = D.load_cnn(rec)
    assert tid == 17 and cnn.lut_id == m.lut_id and cnn.n == 16
    meta = json.loads((tmp_path / "out" / "cnn_model.json").read_text())
    assert meta["threshold"] == 0.5 and meta["clf_id"] == 1 and meta["n"] == 16
    assert "cnn_model_rec[%d]" % len(rec) in (tmp_path / "out" / "cnn_model.h").read_text()
    # Searching needs two sessions with both kinds; red_s4 alone has none.
    rc = T.main(["--lut", str(D.LUT_MODEL), "--out", str(tmp_path / "o2"), "--epochs", "2",
                 *dirs])
    out = capsys.readouterr().out
    assert rc in (0, 2), out
    if rc == 0:
        assert "thresholds, best first" in out


def test_none_header_is_the_committed_placeholder():
    h = D.MAIN_DIR / "models" / "cnn_model.h"
    text = h.read_text()
    if "CNN_MODEL_NONE" in text:
        assert text == T.NONE_HEADER and not D.CNN_MODEL.exists()
