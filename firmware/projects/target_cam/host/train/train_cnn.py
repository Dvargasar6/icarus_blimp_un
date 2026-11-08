#!/usr/bin/env python3
"""
Train the stage 3 crop CNN behind one colour table (and its classifier).

Stage 3 looks at the crop of each of the cnn_k largest blobs stage 2 kept and
drops the ones a small int8 network does not believe (main/blob_cnn.h). Like
the classifier, a CNN belongs to one colour table: the table decides which
blobs exist and where their crops are. Every argument is a session: a folder
of hand-labelled frames (everything below it), or a blob set (.npz) whose
blobs were found behind this table.

Training data: every blob the table reports (stage 1), not only the ones the
classifier keeps (RECOGNITION.md §10, decision 4), so the CNN does not have to
be retrained each time the classifier is; in flight it only ever sees a subset
of what it was trained on. A blob is a balloon if its centroid lies in a
painted balloon's box (tcview/blobset.py), so an orange or pink balloon
painted as background is a "not".

The network (blob_cnn.h) is trained in float with numpy, no other library:
im2col convolutions, max-pooling, global average, one logistic unit, class-
weighted cross-entropy, Adam, and two augmentations that keep a balloon a
balloon (a horizontal mirror, and brightness scaled by 0.75-1.25). Then it is
quantised after training: int8 weights per layer, uint8 activations whose
scales come from the 99.9th percentile of each layer's output over the
training crops, and integer requantisation (multiplier and shift). The
integer network is run here by int_forward(), a numpy copy of blob_cnn.c's
arithmetic, and the trainer reports how often it decides differently from the
float one: the camera runs the integer one.

The threshold is chosen by leaving one session out, scored on frames with the
classifier's frame cost (train_clf.py): false blob 1 on frames without a
balloon + frames whose blob 1 is not a balloon + balloons lost. Each held-out
frame is run as stage 3 runs in flight: the classifier first (when the set has
one, --clf), then the CNN on the cnn_k largest blobs it kept. Settings that
lose more than --max-lost of the balloons stage 2 found are EXCLUDED.

Usage (from target_cam/):
  python host/train/train_cnn.py --lut models/S/lut_model.bin --clf models/S/clf_model.bin \\
      --out models/S data/train/s1 data/train/s3
  python host/train/train_cnn.py --threshold 0.5 --out DIR DIR1         (no search)

The record (cnn_model.bin), its C array (cnn_model.h) and how it was made
(cnn_model.json) go to --out, beside the table they belong to.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "viewer"))
import train_clf as TC                  # noqa: E402  (frame scoring, sessions, data warnings)
from tcview import detector as D        # noqa: E402
from tcview import labels as L          # noqa: E402
from tcview import protocol as P        # noqa: E402

MAGIC = b"TCCN"
VERSION = 1
HEADER_BYTES = 20
CHANNELS = (8, 16, 16)
THRESHOLDS = tuple(round(t, 2) for t in np.arange(0.05, 0.951, 0.05))


# --- data -------------------------------------------------------------------------

def crop_input(crop24: np.ndarray, n: int) -> np.ndarray:
    """(24, 24) native RGB565 -> (3, n, n) uint8, the bytes blob_cnn_sum
    feeds its first layer (16: blob_crop_down16 first)."""
    c = crop24 if n == 24 else D.crop_down16(crop24)
    return P.rgb565_to_rgb888(c).transpose(2, 0, 1).copy()


def load_session(folder: Path, name: str, m: D.TcModels, n: int) -> list:
    """FrameBlobs (train_clf.py) of the stage-1 blobs, each with .crops."""
    from tcview import blobset as B
    out = []
    for stem in L.list_frames(folder):
        sf = L.load_frame(stem)
        if not sf.label_path.exists():
            continue
        lab = L.load_labels(sf.label_path, sf.frame.w, sf.frame.h)
        samples = B.from_frame(sf, lab, 1, m)
        if any(s.stage != 1 for s in samples):
            raise RuntimeError("the colour table was not used for %s" % stem)
        st = sf.settings()
        fb = TC.FrameBlobs(name, sf.stem.name, lab, D.mask(st, sf.frame.data, 1, m),
                           [(s.cx, s.cy) for s in samples],
                           np.array([s.feat for s in samples], np.float32).reshape(
                               len(samples), D.BLOB_FEAT_N),
                           np.array([s.label for s in samples], np.int8),
                           len(L.balloons(lab)), P.wb_of(sf.meta.get("camera_cfg") or {}))
        fb.crops = np.array([crop_input(s.crop, n) for s in samples], np.uint8).reshape(
            len(samples), 3, n, n)
        out.append(fb)
    return out


def load_blobset(path: Path, name: str, m: D.TcModels, n: int, allow_stage0: bool):
    """train_clf's SetFrames of a blob set, each with .crops."""
    from tcview import blobset as B
    frames, notes = TC.load_blobset(path, name, m, allow_stage0)
    by = {}
    for s in B.load(path):
        by.setdefault(s.ref, []).append(s)
    for f in frames:
        g = sorted(by[f.name], key=lambda s: s.k)[:len(f.cents)]
        f.crops = np.array([crop_input(s.crop, n) for s in g], np.uint8).reshape(
            len(g), 3, n, n)
    return frames, notes


def stack(frames: list) -> tuple:
    xs = [f.crops for f in frames if len(f.cents)]
    ys = [f.y for f in frames if len(f.cents)]
    if not xs:
        return np.zeros((0, 3, 1, 1), np.uint8), np.zeros(0, np.int8)
    x, y = np.concatenate(xs), np.concatenate(ys)
    ok = y >= 0
    return x[ok], y[ok]


# --- the float network -----------------------------------------------------------

def _cols(x: np.ndarray) -> np.ndarray:
    """(B, C, n, n) -> (B*n*n, C*9), zero padding: the 3x3 neighbourhoods."""
    b, c, n, _ = x.shape
    xp = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
    w = np.lib.stride_tricks.sliding_window_view(xp, (3, 3), axis=(2, 3))  # B C n n 3 3
    return w.transpose(0, 2, 3, 1, 4, 5).reshape(b * n * n, c * 9)


def _uncols(dcols: np.ndarray, shape: tuple) -> np.ndarray:
    b, c, n, _ = shape
    d = dcols.reshape(b, n, n, c, 3, 3)
    dxp = np.zeros((b, c, n + 2, n + 2), dcols.dtype)
    for ky in range(3):
        for kx in range(3):
            dxp[:, :, ky:ky + n, kx:kx + n] += d[:, :, :, :, ky, kx].transpose(0, 3, 1, 2)
    return dxp[:, :, 1:-1, 1:-1]


def _pool(x: np.ndarray):
    b, c, n, _ = x.shape
    m = n // 2
    r = x.reshape(b, c, m, 2, m, 2).transpose(0, 1, 2, 4, 3, 5).reshape(b, c, m, m, 4)
    idx = r.argmax(-1)
    return np.take_along_axis(r, idx[..., None], -1)[..., 0], idx


def _unpool(d: np.ndarray, idx: np.ndarray, n: int) -> np.ndarray:
    b, c, m, _ = d.shape
    r = np.zeros((b, c, m, m, 4), d.dtype)
    np.put_along_axis(r, idx[..., None], d[..., None], -1)
    return r.reshape(b, c, m, m, 2, 2).transpose(0, 1, 2, 4, 3, 5).reshape(b, c, n, n)


class Net:
    """blob_cnn.h's network in float64."""

    def __init__(self, n: int = 16, channels=CHANNELS, seed: int = 0):
        rng = np.random.default_rng(seed)
        self.n = n
        self.p = {}
        cin = 3
        for i, cout in enumerate(channels, 1):
            self.p["w%d" % i] = rng.normal(0, np.sqrt(2.0 / (cin * 9)), (cout, cin, 3, 3))
            self.p["b%d" % i] = np.zeros(cout)
            cin = cout
        self.p["wd"] = rng.normal(0, np.sqrt(1.0 / cin), cin)
        self.p["bd"] = np.zeros(1)

    def forward(self, x: np.ndarray, keep: bool = False):
        """x (B, 3, n, n) in 0..1 -> logits (B,), and what backward needs."""
        cache = {}
        a = x
        acts = []
        for i in (1, 2, 3):
            w = self.p["w%d" % i]
            b, c, n, _ = a.shape
            cols = _cols(a)
            z = (cols @ w.reshape(w.shape[0], -1).T + self.p["b%d" % i]).reshape(
                b, n, n, -1).transpose(0, 3, 1, 2)
            r = np.maximum(z, 0)
            acts.append(r)
            if keep:
                cache["cols%d" % i], cache["shape%d" % i], cache["z%d" % i] = cols, a.shape, z
            if i < 3:
                a, idx = _pool(r)
                if keep:
                    cache["idx%d" % i] = idx
            else:
                a = r
        g = a.mean(axis=(2, 3))
        logit = g @ self.p["wd"] + self.p["bd"][0]
        if keep:
            cache["g"], cache["a3"] = g, a
        return logit, cache, acts

    def backward(self, cache: dict, dlogit: np.ndarray) -> dict:
        gr = {}
        g = cache["g"]
        gr["wd"] = g.T @ dlogit
        gr["bd"] = np.array([dlogit.sum()])
        a3 = cache["a3"]
        da = (dlogit[:, None] * self.p["wd"][None, :])[:, :, None, None] * \
            np.ones_like(a3) / (a3.shape[2] * a3.shape[3])
        for i in (3, 2, 1):
            if i < 3:
                da = _unpool(da, cache["idx%d" % i], cache["z%d" % i].shape[2])
            dz = da * (cache["z%d" % i] > 0)
            w = self.p["w%d" % i]
            d2 = dz.transpose(0, 2, 3, 1).reshape(-1, w.shape[0])
            gr["w%d" % i] = (d2.T @ cache["cols%d" % i]).reshape(w.shape)
            gr["b%d" % i] = d2.sum(0)
            if i > 1:
                da = _uncols(d2 @ w.reshape(w.shape[0], -1), cache["shape%d" % i])
        return gr


def loss_grad(net: Net, x: np.ndarray, y: np.ndarray, sw: np.ndarray, l2: float):
    logit, cache, _ = net.forward(x, keep=True)
    p = 1.0 / (1.0 + np.exp(-np.clip(logit, -60, 60)))
    tot = sw.sum()
    loss = float((sw * (np.logaddexp(0, logit) - y * logit)).sum() / tot)
    grads = net.backward(cache, sw * (p - y) / tot)
    for k in grads:
        if k.startswith("w"):
            loss += 0.5 * l2 * float((net.p[k] ** 2).sum())
            grads[k] = grads[k] + l2 * net.p[k]
    return loss, grads


def augment(x: np.ndarray, rng) -> np.ndarray:
    """Mirror half the crops left-right; scale each one's brightness."""
    x = x.copy()
    flip = rng.random(len(x)) < 0.5
    x[flip] = x[flip][..., ::-1]
    gain = rng.uniform(0.75, 1.25, (len(x), 1, 1, 1))
    return np.clip(x * gain, 0.0, 1.0)


def train(xq: np.ndarray, y: np.ndarray, n: int, epochs: int = 40, lr: float = 3e-3,
          l2: float = 1e-4, batch: int = 64, seed: int = 0, channels=CHANNELS) -> Net:
    """Adam on class-weighted cross-entropy. xq: (B, 3, n, n) uint8."""
    rng = np.random.default_rng(seed)
    net = Net(n, channels, seed)
    x = xq.astype(np.float64) / 255.0
    y = y.astype(np.float64)
    n1, n0 = max((y == 1).sum(), 1), max((y == 0).sum(), 1)
    sw_all = np.where(y == 1, len(y) / (2 * n1), len(y) / (2 * n0))
    m = {k: np.zeros_like(v) for k, v in net.p.items()}
    v = {k: np.zeros_like(v) for k, v in net.p.items()}
    t = 0
    for _ in range(epochs):
        order = rng.permutation(len(x))
        for s in range(0, len(x), batch):
            ix = order[s:s + batch]
            _, gr = loss_grad(net, augment(x[ix], rng), y[ix], sw_all[ix], l2)
            t += 1
            for k in net.p:
                m[k] = 0.9 * m[k] + 0.1 * gr[k]
                v[k] = 0.999 * v[k] + 0.001 * gr[k] ** 2
                net.p[k] -= lr * (m[k] / (1 - 0.9 ** t)) / (np.sqrt(v[k] / (1 - 0.999 ** t)) + 1e-8)
    return net


def float_conf(net: Net, xq: np.ndarray, batch: int = 256) -> np.ndarray:
    out = []
    for s in range(0, len(xq), batch):
        logit, _, _ = net.forward(xq[s:s + batch].astype(np.float64) / 255.0)
        out.append(1.0 / (1.0 + np.exp(-np.clip(logit, -60, 60))))
    return np.concatenate(out) if out else np.zeros(0)


# --- quantisation and the integer network ----------------------------------------

def _mult_shift(f: float) -> tuple:
    """M, s with M / 2^s = f, M as large as fits in 31 bits."""
    s = int(np.clip(30 - np.floor(np.log2(f)), 1, 62))
    mlt = int(round(f * 2 ** s))
    while mlt >= 2 ** 31 and s > 1:
        s -= 1
        mlt = int(round(f * 2 ** s))
    return max(mlt, 1), s


def quantise(net: Net, calib_xq: np.ndarray, threshold: float) -> dict:
    """The integer network blob_cnn.c runs, as a dict int_forward() and
    record() read. Activation scales from the 99.9th percentile of each
    layer's float output on calib_xq."""
    _, _, acts = net.forward(calib_xq.astype(np.float64) / 255.0)
    s_in = 1.0 / 255.0
    q = dict(n=net.n, threshold=float(threshold), conv=[])
    for i in (1, 2, 3):
        w, b = net.p["w%d" % i], net.p["b%d" % i]
        sw = max(np.abs(w).max(), 1e-8) / 127.0
        s_out = max(float(np.percentile(acts[i - 1], 99.9)), 1e-6) / 255.0
        mlt, sh = _mult_shift(sw * s_in / s_out)
        q["conv"].append(dict(w=np.clip(np.round(w / sw), -127, 127).astype(np.int8),
                              b=np.round(b / (sw * s_in)).astype(np.int64),
                              mult=mlt, shift=sh))
        s_in = s_out
    wd = net.p["wd"]
    swd = max(np.abs(wd).max(), 1e-8) / 127.0
    q["dense_w"] = np.clip(np.round(wd / swd), -127, 127).astype(np.int8)
    q["dense_b"] = int(round(net.p["bd"][0] / (swd * s_in)))
    q["logit_scale"] = float(np.float32(swd * s_in))
    return q


def int_forward(q: dict, xq: np.ndarray) -> np.ndarray:
    """blob_cnn_sum() for each crop: (B, 3, n, n) uint8 -> (B,) int64. The
    same integer arithmetic as blob_cnn.c, so the sums are equal."""
    a = xq.astype(np.int64)
    for i, L_ in enumerate(q["conv"]):
        b, c, n, _ = a.shape
        cols = _cols(a)
        w = L_["w"].astype(np.int64).reshape(L_["w"].shape[0], -1)
        acc = (cols @ w.T + L_["b"]).reshape(b, n, n, -1).transpose(0, 3, 1, 2)
        half = 1 << (L_["shift"] - 1)
        r = np.where(acc > 0, np.minimum((acc * L_["mult"] + half) >> L_["shift"], 255), 0)
        if i < 2:
            mm = n // 2
            a = r.reshape(b, -1, mm, 2, mm, 2).max(axis=(3, 5))
        else:
            a = r
    cnt = a.shape[2] * a.shape[3]
    g = (a.sum(axis=(2, 3)) + cnt // 2) // cnt
    return q["dense_b"] + g @ q["dense_w"].astype(np.int64)


def int_conf(q: dict, xq: np.ndarray) -> np.ndarray:
    logit = (int_forward(q, xq).astype(np.float32) * np.float32(q["logit_scale"]))
    return 1.0 / (1.0 + np.exp(-logit.astype(np.float64)))


def record(q: dict, lut_id: int, train_id: int) -> bytes:
    """The model record: tc_pipeline.h's header, blob_cnn.h's payload."""
    ch = [L_["w"].shape[0] for L_ in q["conv"]]
    payload = struct.pack("<4BIff", q["n"], *ch, lut_id, q["threshold"], q["logit_scale"])
    for L_ in q["conv"]:
        payload += L_["w"].astype(np.int8).tobytes()
        payload += struct.pack("<%di" % len(L_["b"]), *[int(v) for v in L_["b"]])
        payload += struct.pack("<iB", L_["mult"], L_["shift"])
    payload += q["dense_w"].astype(np.int8).tobytes() + struct.pack("<i", q["dense_b"])
    head = struct.pack("<4sHHII", MAGIC, VERSION, HEADER_BYTES, len(payload), train_id)
    crc = zlib.crc32(head + payload) & 0xFFFFFFFF
    return head + struct.pack("<I", crc) + payload


def c_header(rec: bytes, meta: dict) -> str:
    lines = [", ".join("0x%02x" % x for x in rec[i:i + 16]) for i in range(0, len(rec), 16)]
    return (
        "/*\n"
        " * Generated by host/train/train_cnn.py - do not edit.\n"
        " *\n"
        " * The stage 3 crop CNN as a model record (tc_pipeline.h, blob_cnn.h), the\n"
        " * same bytes as cnn_model.bin. target_main.c checks it with blob_cnn_load()\n"
        " * at boot, and uses it only behind the colour table it was trained with.\n"
        " *\n"
        " * train id %d, behind colour table %d, input %dx%d, threshold %g\n"
        " * trained on: %s\n"
        " */\n"
        "#pragma once\n\n#include <stdint.h>\n\n"
        "static const uint8_t cnn_model_rec[%d] = {\n    %s,\n};\n"
        % (meta["train_id"], meta["lut_id"], meta["n"], meta["n"], meta["threshold"],
           ", ".join(meta["sessions"]), len(rec), ",\n    ".join(lines)))


# What main/models/cnn_model.h holds when no CNN is installed. tcview's
# Firmware tab writes it back when it installs a set without one.
NONE_HEADER = """/*
 * No crop CNN is built in: stage 3 is unavailable, and a request for it runs
 * stage 2 or lower (#!CFG cnn=none).
 *
 * host/train/train_cnn.py writes a trained model record in its place (and
 * tcview's Firmware tab puts this placeholder back when it installs a set
 * that has no CNN). The file always exists, so the build system sees it
 * change, as clf_model.h does.
 */
#pragma once

#include <stdint.h>

#define CNN_MODEL_NONE 1

static const uint8_t cnn_model_rec[1] = { 0 };
"""


# --- choosing the threshold ------------------------------------------------------

def stage3_keep(clf_conf: dict, cnn_conf: dict, clf_thr, cnn_k: int, t: float):
    """keep_of() for train_clf.tally: stage 3 as it runs in flight. The
    classifier keeps what it believes (all, with no classifier); the CNN
    checks the cnn_k largest of those and drops what it does not believe."""
    def keep(f):
        kept = [k for k in range(len(f.cents))
                if clf_thr is None or clf_conf[id(f)][k] >= clf_thr]
        return tuple(k for i, k in enumerate(kept) if i >= cnn_k or cnn_conf[id(f)][k] >= t)
    return keep


def search(sessions: dict, n: int, epochs: int, clf_conf: dict, clf_thr, cnn_k: int,
           seed: int, log=print) -> list:
    """Leave one session out over the thresholds: one network per fold."""
    cnn_conf = {}
    for held in sessions:
        x, y = stack([f for name, fr in sessions.items() if name != held for f in fr])
        if len(set(y.tolist())) < 2:
            raise TC.OneClass(held)
        log("  fold without %s: training on %d crops (%d balloon) ..." % (
            held, len(y), int((y == 1).sum())))
        net = train(x, y, n, epochs, seed=seed)
        for f in sessions[held]:
            cnn_conf[id(f)] = float_conf(net, f.crops) if len(f.cents) else np.zeros(0)
    rows = []
    for t in THRESHOLDS:
        folds = []
        for held, fr in sessions.items():
            s = TC.tally(fr, stage3_keep(clf_conf, cnn_conf, clf_thr, cnn_k, t))
            s["held_out"] = held
            folds.append(s)
        tot = {k: sum(f[k] for f in folds) for k in folds[0] if k != "held_out"}
        rows.append(dict(threshold=t, folds=folds, **tot))
    rows.sort(key=lambda r: (r["cost"], r["fp"], r["threshold"]))
    return rows


# --- main -------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("sessions", nargs="+", type=Path,
                    help="one folder per capture session, or a blob set (.npz) per session")
    ap.add_argument("--lut", type=Path, default=D.LUT_MODEL, help="the colour table")
    ap.add_argument("--clf", type=Path, help="the classifier stage 3 runs behind (the "
                    "set's clf_model.bin); without it the threshold is chosen on stage 1 alone")
    ap.add_argument("--out", type=Path, help="output folder (default: the table's folder)")
    ap.add_argument("--size", type=int, choices=(16, 24), default=16,
                    help="input side (default 16; 24 costs about 2.2x the multiply-adds)")
    ap.add_argument("--cnn-k", type=int, default=2, help="blobs the CNN checks per frame")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--threshold", type=float, help="fix the threshold instead of searching")
    ap.add_argument("--max-lost", type=float, default=0.10,
                    help="exclude thresholds that lose more than this fraction of the "
                    "balloons stage 2 found on held-out frames (default 0.10)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--train-id", type=int, help="record id (default: now, Unix seconds)")
    ap.add_argument("--top", type=int, default=6, help="search rows to print")
    ap.add_argument("--allow-stage0", action="store_true")
    a = ap.parse_args(argv)
    out = a.out or a.lut.parent

    try:
        m = D.models(a.lut, a.clf) if a.clf else D.models(a.lut)
    except (OSError, D.ModelError) as e:
        print("cannot use the models: %s" % e)
        return 2
    clf = m.clf_model if a.clf else None
    print("colour table %s, id %d%s" % (a.lut, m.lut_id, "; classifier id %d, threshold %.2f"
                                        % (m.clf_id, clf.threshold) if clf else ""))

    sessions, notes = {}, []
    for d, name in zip(a.sessions, TC.session_names(a.sessions)):
        if TC.is_blobset(d):
            try:
                fr, nt = load_blobset(d, name, m, a.size, a.allow_stage0)
            except TC.SetRefused as e:
                print("cannot use blob set %s: %s" % (d, e))
                return 2
            notes += nt
        else:
            fr = load_session(d, name, m, a.size)
        if not fr:
            print("no labelled frames in %s" % d)
            return 2
        sessions[name] = fr
        y = np.concatenate([f.y for f in fr]) if fr else np.zeros(0)
        print("%-24s %3d frames  %3d crops: %3d balloon, %3d not%s" % (
            name, len(fr), len(y), int((y == 1).sum()), int((y == 0).sum()),
            "   (blob set)" if TC.is_blobset(d) else ""))
    notes += TC.gain_notes(sessions)
    for nt in notes:
        print("DATA WARNING: " + nt)

    x, y = stack([f for fr in sessions.values() for f in fr])
    if (y == 1).sum() < 2 or (y == 0).sum() < 2:
        print("need at least two balloon crops and two false ones; have %d and %d"
              % ((y == 1).sum(), (y == 0).sum()))
        return 2
    fixed = a.threshold is not None
    if fixed and not 0 < a.threshold < 1:
        print("the threshold must be between 0 and 1")
        return 2
    if not fixed and len(sessions) < 2:
        print("choosing the threshold needs two or more sessions; give --threshold")
        return 2

    clf_conf = {id(f): np.array([D.clf_conf(clf, f.feat[k]) for k in range(len(f.cents))])
                for fr in sessions.values() for f in fr} if clf else {}
    clf_thr = clf.threshold if clf else None
    base_keep = (lambda f: tuple(k for k in range(len(f.cents)) if clf_conf[id(f)][k] >= clf_thr)) \
        if clf else TC.keep_all
    base_name = "stage 2" if clf else "stage 1"
    rows, base, warnings = [], None, []
    if not fixed:
        print("\nleave one session out, %d epochs per fold:" % a.epochs)
        try:
            rows = search(sessions, a.size, a.epochs, clf_conf, clf_thr, a.cnn_k, a.seed)
        except TC.OneClass as e:
            print("with %s held out, the other sessions have only balloon crops or only false "
                  "ones; add sessions with both, or give --threshold" % e.args[0])
            return 2
        base_folds = []
        for held, fr in sessions.items():
            s = TC.tally(fr, base_keep)
            s["held_out"] = held
            base_folds.append(s)
        base = {k: sum(f[k] for f in base_folds) for k in base_folds[0] if k != "held_out"}
        rows = TC.exclude_lossy(rows, base, a.max_lost)
        print("\n%s alone on the same frames:  %s" % (base_name, TC.describe(base)))
        print("\nthresholds, best first:")
        for r in rows[:a.top]:
            print("  threshold %.2f  %s%s" % (r["threshold"], TC.describe(r),
                                             "   EXCLUDED" if r["excluded"] else ""))
            for f in r["folds"]:
                print("      held out %-22s %s" % (f["held_out"], TC.describe(f)))
        best = rows[0]
        if best["excluded"]:
            warnings.append("every threshold loses more than %d %% of the balloons %s found on "
                            "the held-out sessions; the least bad one is written. Do not fly it."
                            % (round(100 * a.max_lost), base_name))
        if best["cost"] >= base["cost"]:
            warnings.append("the CNN does not lower the held-out cost (%d, %s alone %d): it "
                            "does not help on these sessions." % (best["cost"], base_name,
                                                                  base["cost"]))
        if best["false_b1"]:
            warnings.append("the chosen threshold still puts a false blob 1 on %d held-out "
                            "frame(s) without a balloon." % best["false_b1"])
        threshold = best["threshold"]
    else:
        threshold = a.threshold
    for w in warnings:
        print("\nWARNING: " + w)

    print("\nfinal network: %d crops (%d balloon), input %dx%d, %d epochs" % (
        len(y), int((y == 1).sum()), a.size, a.size, a.epochs))
    net = train(x, y, a.size, a.epochs, seed=a.seed)
    q = quantise(net, x, threshold)
    pf, pq = float_conf(net, x), int_conf(q, x)
    flips = int(((pf >= threshold) != (pq >= threshold)).sum())
    print("  integer vs float on the training crops: max |conf difference| %.3f, %d of %d "
          "decided differently at %.2f" % (float(np.abs(pf - pq).max()), flips, len(x),
                                           threshold))
    lut_id = m.lut_id
    train_id = a.train_id if a.train_id is not None else int(time.time())
    rec = record(q, lut_id, train_id)
    chk = D.load_cnn(rec)[0]                    # the C check the camera runs at boot
    c_sum = np.array([D.cnn_sum(chk, xi) for xi in x[:64]])
    if not np.array_equal(c_sum, int_forward(q, x[:64])):
        print("ERROR: the C network and the numpy integer network disagree")
        return 3
    macs = D.cnn_macs(chk)
    print("  %d multiply-adds per crop, %d weight bytes, record %d bytes, id %d" % (
        macs, sum(L_["w"].size for L_ in q["conv"]) + q["dense_w"].size, len(rec), train_id))
    for name, fr in sessions.items():
        conf = {id(f): int_conf(q, f.crops) if len(f.cents) else np.zeros(0) for f in fr}
        print("  on %-22s (training data, optimistic) %s" % (
            name, TC.describe(TC.tally(fr, stage3_keep(clf_conf, conf, clf_thr, a.cnn_k,
                                                       threshold)))))
    meta = dict(train_id=train_id, lut_id=lut_id, clf_id=m.clf_id if clf else 0,
                lut=str(a.lut), clf=str(a.clf) if a.clf else None, n=a.size,
                channels=list(CHANNELS), threshold=threshold, epochs=a.epochs, seed=a.seed,
                cnn_k=a.cnn_k, macs=int(macs), sessions=list(sessions),
                session_paths=[str(d) for d in a.sessions],
                crops=dict(n=int(len(y)), balloon=int((y == 1).sum()),
                           not_balloon=int((y == 0).sum())),
                int_vs_float=dict(max_conf_diff=float(np.abs(pf - pq).max()), flips=flips),
                objective="leave-one-session-out frame cost, run as stage 3 runs in flight",
                caveat="threshold chosen on the same folds it is scored on",
                base_held_out=base, base=base_name, warnings=warnings, data_warnings=notes,
                search=[{k: v for k, v in r.items()} for r in rows[:a.top]],
                record_bytes=len(rec), crc32="%08x" % struct.unpack_from("<I", rec, 16)[0])
    out.mkdir(parents=True, exist_ok=True)
    (out / "cnn_model.bin").write_bytes(rec)
    (out / "cnn_model.h").write_text(c_header(rec, meta))
    (out / "cnn_model.json").write_text(json.dumps(meta, indent=1) + "\n")
    print("wrote %s/cnn_model.{bin,h,json}" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
