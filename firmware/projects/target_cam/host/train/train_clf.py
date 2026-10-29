#!/usr/bin/env python3
"""
Train the stage 2 blob classifier from hand-labelled frames.

Stage 2 runs behind a colour table: each blob the table reports gets a
confidence from its 12 features (main/blob_feat.c), and the blobs below the
threshold are dropped (main/blob_clf.h). So a classifier is trained for one
table. Every argument is a folder of saved frames with their .labels.png, one
capture session each, as for train_lut.py. The table's own pipeline is run on
every frame (the shared C library), so the blobs, their features and their
labels are exactly the ones the camera would have with that table:

  - a blob is a balloon if its centroid lies in a painted balloon's box,
    otherwise not (tcview/blobset.py, the rule the frame scores use);
  - an orange or pink balloon painted as background is therefore a "not",
    which is what stage 2 is for.

The model is written beside the table, three ways, as the table is:

  clf_model.bin    the record (tc_pipeline.h header, blob_clf.h payload)
  clf_model.h      the same bytes as a C array (the firmware)
  clf_model.json   how it was made: weights by feature name, data, fold scores

How it is learned. Logistic regression on the standardised features, with
an L2 penalty and the two classes weighted equally (balloons and false blobs
are rarely equal in number). It is fitted by Newton's method, which for 13
parameters converges in a few steps and gives the same answer every run.

How the penalty and the threshold are chosen: leave one session out, as
train_lut.py does, but scored on frames, not on blobs, because what matters
is what the blimp would steer to. For each held-out frame the blobs the
threshold keeps are scored with the frame's labels (tcview.labels.score, the
rule eval_frames.py uses). The cost of a setting is

    false blob 1 on frames without a balloon
  + frames with a balloon whose blob 1 is not one
  + balloons no kept blob lands in (FN)

each one a frame where the blimp steers wrong or loses its target, so each
counts one. Ties go to fewer false blobs, then to the lower threshold (the one
that drops less).

A setting that loses more than --max-lost (default 10 %) of the balloons
stage 1 found on the held-out frames is EXCLUDED and ranked after every other.
Without that rule a degenerate classifier can win: when stage 1 puts many
false blob 1s on empty frames, rejecting every blob costs only the balloons
(measured 2026-10-29: cost 66 by dropping all 66 balloons, against 94 for
stage 1 alone), and a classifier that sees nothing is no use to the blimp.
If every setting is excluded the trainer still writes the best one, with a
WARNING, so the operator sees how far it is from usable. The same cost is given for stage 1 alone on the same
frames, so it is visible whether the classifier helps at all.

Blob sets. A session can also be a blob set (.npz, tcview/blobset.py): the
records the camera sent with '!collect 1', labelled by hand in tcview's
Blobs tab. Those are the camera's own features, but not its frames, so:

  - the blobs must have been found behind the same table. A set records
    the camera's lut_id with every blob (the viewer takes it from #!CFG); a
    set behind another table is refused, and one from before that column
    (lut_id -1) is used with a DATA WARNING. Blobs from stage 0 (HSV) are
    refused unless --allow-stage0: HSV finds other blobs than the table, so a
    classifier learned from them filters mistakes the table does not make.
    Stage 2 blobs are used with a warning: the ones the classifier of the
    time dropped are missing.
  - the blobs are grouped back into frames (consecutive records of one
    capture, k = 0, 1, ...) and scored with the same cost, from their labels
    alone: a frame has a balloon if one of its blobs is one, and a balloon
    blob the threshold drops counts as a lost balloon. A balloon the table
    never found is not in the set, so stage 1 alone has FN 0 there by
    construction, and a balloon split into two blobs counts twice.
    Frames holding an unlabelled ("?") blob are left out.

Folders and blob sets can be mixed; each is one session. Sessions whose
white-balance gains differ, or are not fixed, get a DATA WARNING: their
colours are not comparable (README, White balance).

Usage (from target_cam/):
  python host/train/train_clf.py --lut models/T/lut_model.bin data/train/red_s1 data/train/red_s2
  python host/train/train_clf.py --lut models/T/lut_model.bin data/blobs/s1.npz data/blobs/s2.npz
  python host/train/train_clf.py --threshold 0.5 --l2 1 DIR      (no search; one session is enough)

--out defaults to the folder of --lut, so the classifier lands beside its
table; for the firmware's table (the default --lut) that is main/models.
"""
from __future__ import annotations

import argparse
import itertools
import json
import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "viewer"))
from tcview import detector as D        # noqa: E402
from tcview import labels as L          # noqa: E402

MAGIC = b"TCBC"
VERSION = 1
HEADER_BYTES = 20
FEAT_VERSION = 1                        # blob_feat.h BLOB_FEAT_VERSION

L2S = (0.01, 0.1, 1.0, 10.0)
THRESHOLDS = tuple(round(t, 2) for t in np.arange(0.05, 0.951, 0.05))


# --- data -------------------------------------------------------------------

@dataclass
class FrameBlobs:
    """One labelled frame, run through stage 1: its candidate blobs (largest
    first) with their features, and what is needed to score any subset of
    them as the reported blobs."""
    session: str
    name: str
    lab: np.ndarray
    mask: np.ndarray
    cents: list                         # (cx, cy) per candidate
    feat: np.ndarray                    # (n, F) float32
    y: np.ndarray                       # (n,) 1 balloon, 0 not
    n_balloons: int = 0
    gain: tuple = ()                    # (wb mode, (r, g, b)) the pixels were made with
    _cache: dict = field(default_factory=dict)

    def score(self, keep: tuple) -> L.FrameScore:
        """The frame's score if only the candidates in `keep` were reported.
        A frame has at most four candidates, so few subsets ever occur."""
        s = self._cache.get(keep)
        if s is None:
            s = L.score(self.name, self.lab, [self.cents[k] for k in keep], self.mask, 2)
            self._cache[keep] = s
        return s


@dataclass
class SetScore:
    """What tally() reads from a frame score, for a frame rebuilt from a
    blob set (no paint: the blob labels are all there is)."""
    n_balloons: int
    tp: int
    fn: int
    fp: int
    b1: str


@dataclass
class SetFrame:
    """One capture's blobs from a blob set, largest first, all labelled.
    Scored like FrameBlobs: each balloon blob stands for a balloon."""
    session: str
    name: str
    cents: list
    feat: np.ndarray
    y: np.ndarray
    gain: tuple = ()

    @property
    def n_balloons(self) -> int:
        return int((self.y == 1).sum())

    def score(self, keep: tuple) -> SetScore:
        tp = int(sum(self.y[k] == 1 for k in keep))
        b1 = "none" if not keep else ("balloon" if self.y[keep[0]] == 1 else "false")
        return SetScore(self.n_balloons, tp, self.n_balloons - tp, len(keep) - tp, b1)


def is_blobset(p: Path) -> bool:
    return p.suffix == ".npz"


def session_names(folders: list[Path]) -> list[str]:
    """The folder's name (a blob set's, without .npz), or parent/name where
    two chosen sessions share a name (data/test/red_s1 and
    data/train/Without_AWB/red_s1)."""
    base = [d.stem if is_blobset(d) else d.name for d in folders]
    return [("%s/%s" % (d.parent.name, n)) if base.count(n) > 1 else n
            for d, n in zip(folders, base)]


class SetRefused(Exception):
    """A blob set this table's classifier cannot be trained on."""


def load_blobset(path: Path, name: str, m: D.TcModels, allow_stage0: bool = False
                 ) -> tuple[list[SetFrame], list[str]]:
    """A blob set as frames, and its data warnings. Raises SetRefused."""
    from tcview import blobset as B
    try:
        samples = B.load(path)
        fv = int(np.load(path, allow_pickle=False)["feat_version"])
    except (OSError, KeyError, ValueError) as e:
        raise SetRefused("cannot read it: %s" % e)
    if fv != FEAT_VERSION:
        raise SetRefused("its features are version %d, this trainer's %d" % (fv, FEAT_VERSION))
    if not samples:
        raise SetRefused("it holds no blobs")
    notes = []
    stages = sorted({s.stage for s in samples})
    if 0 in stages:
        n0 = sum(s.stage == 0 for s in samples)
        if not allow_stage0:
            raise SetRefused(
                "%d of its %d blobs were found at stage 0 (HSV), not behind a colour table. "
                "Stage 2 runs behind the table, which finds other blobs; collect again with "
                "the table flashed and !stage 1 (or pass --allow-stage0 to try anyway)"
                % (n0, len(samples)))
        notes.append("%s: %d blobs from stage 0 (HSV) used as if found behind table %d "
                     "(--allow-stage0)" % (name, n0, m.lut_id))
    if any(st >= 2 for st in stages):
        notes.append("%s: some blobs were collected at stage 2, so the ones that classifier "
                     "dropped are missing; collect at stage 1" % name)
    ids = sorted({s.lut_id for s in samples if s.stage >= 1})
    other = [i for i in ids if i >= 0 and i != m.lut_id]
    if other:
        raise SetRefused("its blobs were found behind colour table %s, not %d. Train behind "
                         "that table, or collect again with this one flashed"
                         % (", ".join(map(str, other)), m.lut_id))
    if B.UNKNOWN_LUT in ids:
        notes.append("%s: the set does not record which table its blobs were found behind "
                     "(collected before tcview recorded it); it is assumed to be %d"
                     % (name, m.lut_id))

    # Back into frames: a capture's records arrive together, k = 0, 1, ...
    groups, cur = [], []
    for s in samples:
        if cur and (s.ref != cur[-1].ref or s.k <= cur[-1].k):
            groups.append(cur)
            cur = []
        cur.append(s)
    if cur:
        groups.append(cur)
    out, skipped = [], 0
    for g in groups:
        g.sort(key=lambda s: s.k)
        y = np.array([s.label for s in g], np.int8)
        if (y < 0).any():
            skipped += 1
            continue
        out.append(SetFrame(name, g[0].ref, [(s.cx, s.cy) for s in g],
                            np.stack([s.feat for s in g]).astype(np.float32), y,
                            (g[0].wb, tuple(g[0].wb_gain))))
    if skipped:
        notes.append("%s: %d of %d frames left out, they hold an unlabelled blob"
                     % (name, skipped, len(groups)))
    return out, notes


def gain_notes(sessions: dict) -> list[str]:
    """Data warnings about white balance across the chosen sessions."""
    from tcview import protocol as P
    seen = {}
    for name, fr in sessions.items():
        for f in fr:
            if f.gain:
                seen.setdefault(name, set()).add(f.gain)
    notes = []
    loose = sorted(n for n, g in seen.items() if any(m != "fixed" for m, _ in g))
    if loose:
        notes.append("white balance not fixed (or not recorded) in %s: its colours are not "
                     "known, so the blobs may not be the ones a fixed-gain flight shows"
                     % ", ".join(loose))
    fixed = {g for gs in seen.values() for m, g in gs if m == "fixed"}
    if len(fixed) > 1:
        notes.append("the sessions mix white-balance gains (%s): colour from different "
                     "gains is not comparable" % ", ".join(
                         P.wb_text("fixed", g) for g in sorted(fixed)))
    return notes


def load_session(folder: Path, name: str, m: D.TcModels) -> list[FrameBlobs]:
    from tcview import blobset as B
    from tcview import protocol as P
    out = []
    for stem in L.list_frames(folder):
        sf = L.load_frame(stem)
        if not sf.label_path.exists():
            continue
        lab = L.load_labels(sf.label_path, sf.frame.w, sf.frame.h)
        st = sf.settings()
        tr = D.pipeline(st, sf.frame.data, 1, m, features=True)
        if tr.stage != 1:
            raise RuntimeError("the colour table was not used (stage %d ran)" % tr.stage)
        cents = [(tr.r.b[k].cx, tr.r.b[k].cy) for k in range(tr.n_feat)]
        y = np.array([B.auto_label(lab, cx, cy) for cx, cy in cents], np.int8)
        fb = FrameBlobs(name, sf.stem.name, lab, D.mask(st, sf.frame.data, 1, m), cents,
                        tr.features(), y, len(L.balloons(lab)), P.wb_of(sf.meta.get(
                            "camera_cfg") or {}))
        out.append(fb)
    return out


# --- the model ----------------------------------------------------------------

@dataclass
class Model:
    mean: np.ndarray
    scale: np.ndarray
    w: np.ndarray
    bias: float
    l2: float

    def score(self, x: np.ndarray) -> np.ndarray:
        return self.bias + ((x - self.mean) * self.scale) @ self.w

    def conf(self, x: np.ndarray) -> np.ndarray:
        """As blob_clf_conf, in float64: a non-finite feature gives 0."""
        x = np.asarray(x, np.float64)
        ok = np.isfinite(x).all(axis=1)
        p = np.zeros(len(x))
        if ok.any():
            p[ok] = 1.0 / (1.0 + np.exp(-self.score(x[ok])))
        return p


def fit(x: np.ndarray, y: np.ndarray, l2: float, iters: int = 50) -> Model:
    """Class-weighted logistic regression, L2 on the weights (not the bias),
    by Newton's method. Features with no spread get scale 0: they carry no
    information and would divide by zero."""
    x = np.asarray(x, np.float64)
    mean = x.mean(axis=0)
    sd = x.std(axis=0)
    scale = np.where(sd > 1e-9, 1.0 / np.where(sd > 1e-9, sd, 1.0), 0.0)
    z = np.hstack([(x - mean) * scale, np.ones((len(x), 1))])
    n1, n0 = max(int((y == 1).sum()), 1), max(int((y == 0).sum()), 1)
    sw = np.where(y == 1, len(y) / (2 * n1), len(y) / (2 * n0))
    beta = np.zeros(z.shape[1])
    reg = np.full(z.shape[1], l2)
    reg[-1] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(z @ beta, -60, 60)))
        g = z.T @ (sw * (p - y)) + reg * beta
        h = (z * (sw * p * (1 - p))[:, None]).T @ z + np.diag(reg) + 1e-9 * np.eye(len(beta))
        step = np.linalg.solve(h, g)
        beta -= step
        if np.abs(step).max() < 1e-10:
            break
    return Model(mean, scale, beta[:-1], float(beta[-1]), l2)


def record(mdl: Model, threshold: float, lut_id: int, train_id: int) -> bytes:
    """The model record: tc_pipeline.h's header, blob_clf.h's payload."""
    n = len(mdl.w)
    payload = struct.pack("<HHIff", n, FEAT_VERSION, lut_id, threshold, mdl.bias)
    payload += struct.pack("<%df" % n, *mdl.mean)
    payload += struct.pack("<%df" % n, *mdl.scale)
    payload += struct.pack("<%df" % n, *mdl.w)
    head = struct.pack("<4sHHII", MAGIC, VERSION, HEADER_BYTES, len(payload), train_id)
    crc = zlib.crc32(head + payload) & 0xFFFFFFFF
    return head + struct.pack("<I", crc) + payload


def c_header(rec: bytes, meta: dict) -> str:
    lines = [", ".join("0x%02x" % x for x in rec[i:i + 16]) for i in range(0, len(rec), 16)]
    return (
        "/*\n"
        " * Generated by host/train/train_clf.py - do not edit.\n"
        " *\n"
        " * The stage 2 blob classifier as a model record (tc_pipeline.h,\n"
        " * blob_clf.h), the same bytes as clf_model.bin. target_main.c checks it\n"
        " * with blob_clf_load() at boot, and uses it only behind the colour table\n"
        " * it was trained with.\n"
        " *\n"
        " * train id %d, behind colour table %d, l2 %g, threshold %g\n"
        " * trained on: %s\n"
        " */\n"
        "#pragma once\n\n"
        "#include <stdint.h>\n\n"
        "static const uint8_t clf_model_rec[%d] = {\n    %s,\n};\n"
        % (meta["train_id"], meta["lut_id"], meta["l2"], meta["threshold"],
           ", ".join(meta["sessions"]), len(rec), ",\n    ".join(lines)))


# What main/models/clf_model.h holds when no classifier is installed.
# tcview's Firmware tab writes it back when it installs a table without one; the text is
# kept here, beside the generated header, so the two are edited together (a
# test checks a fresh checkout's file against it).
NONE_HEADER = """/*
 * No blob classifier is built in: stage 2 is unavailable, and a request for
 * it runs stage 1 (#!CFG clf=none).
 *
 * host/train/train_clf.py replaces this file with a trained model record
 * (and tcview's Firmware tab puts this placeholder back when it installs a
 * colour table that has no classifier). The file always exists, rather than
 * being included only when present, so the build system sees it change: a
 * header that appears where none was before would not make target_main.c
 * rebuild, and the firmware would quietly keep its old stage 2.
 */
#pragma once

#include <stdint.h>

#define CLF_MODEL_NONE 1

static const uint8_t clf_model_rec[1] = { 0 };
"""


# --- scoring ------------------------------------------------------------------

def empty_tally() -> dict:
    return dict(frames=0, balloons=0, tp=0, fn=0, fp=0, false_b1=0, empty_frames=0,
                wrong_b1=0, dropped=0, dropped_balloons=0)


def tally(frames: list, keep_of) -> dict:
    """Frame scores when keep_of(frame) gives the kept candidates."""
    t = empty_tally()
    for f in frames:
        keep = keep_of(f)
        s = f.score(keep)
        t["frames"] += 1
        t["balloons"] += s.n_balloons
        t["tp"] += s.tp
        t["fn"] += s.fn
        t["fp"] += s.fp
        t["dropped"] += len(f.cents) - len(keep)
        t["dropped_balloons"] += int(sum(f.y[k] == 1 for k in range(len(f.cents))
                                         if k not in keep))
        if s.n_balloons:
            t["wrong_b1"] += s.b1 == "false"
        else:
            t["empty_frames"] += 1
            t["false_b1"] += s.b1 == "false"
    t["cost"] = t["false_b1"] + t["wrong_b1"] + t["fn"]
    return t


def keep_all(f: FrameBlobs) -> tuple:
    return tuple(range(len(f.cents)))


def keep_above(conf: dict, t: float):
    return lambda f: tuple(k for k, p in enumerate(conf[id(f)]) if p >= t)


class OneClass(Exception):
    """With this session held out, the rest has only one kind of blob."""


def search(sessions: dict[str, list[FrameBlobs]], l2s, thresholds) -> list[dict]:
    """Leave one session out over l2 x threshold."""
    rows = []
    for l2 in l2s:
        confs = {}
        fits_ok = True
        for held in sessions:
            train = [f for n, fr in sessions.items() if n != held for f in fr]
            x, y = stack(train)
            if len(set(y.tolist())) < 2:
                fits_ok = False
                break
            mdl = fit(x, y, l2)
            for f in sessions[held]:
                confs[id(f)] = mdl.conf(f.feat) if len(f.cents) else np.zeros(0)
        if not fits_ok:
            raise OneClass(held)
        for t in thresholds:
            folds = []
            for held, fr in sessions.items():
                s = tally(fr, keep_above(confs, t))
                s["held_out"] = held
                folds.append(s)
            tot = {k: sum(f[k] for f in folds) for k in folds[0] if k != "held_out"}
            rows.append(dict(l2=l2, threshold=t, folds=folds, **tot))
    rows.sort(key=lambda r: (r["cost"], r["fp"], r["threshold"], -r["l2"]))
    return rows


def stack(frames: list[FrameBlobs]) -> tuple[np.ndarray, np.ndarray]:
    x = [f.feat for f in frames if len(f.cents)]
    y = [f.y for f in frames if len(f.cents)]
    if not x:
        return np.zeros((0, D.BLOB_FEAT_N)), np.zeros(0, np.int8)
    x, y = np.vstack(x).astype(np.float64), np.concatenate(y)
    ok = np.isfinite(x).all(axis=1) & (y >= 0)      # -1: a frame with no paint
    return x[ok], y[ok]


def exclude_lossy(rows: list[dict], base: dict, max_lost: float) -> list[dict]:
    """Mark the settings that lose more than max_lost of the balloons stage
    1 found (base, on the same held-out frames) and rank them last. The order
    within each group is search()'s."""
    allowed = max_lost * base["tp"]
    for r in rows:
        r["excluded"] = (r["fn"] - base["fn"]) > allowed
    return sorted(rows, key=lambda r: r["excluded"])         # stable


def describe(t: dict) -> str:
    return ("cost %2d = false b1 %d/%d + wrong b1 %d + FN %d   FP %2d  dropped %d (%d balloons)"
            % (t["cost"], t["false_b1"], t["empty_frames"], t["wrong_b1"], t["fn"], t["fp"],
               t["dropped"], t["dropped_balloons"]))


# --- main ---------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("sessions", nargs="+", type=Path,
                    help="one folder per capture session, or a blob set (.npz) per session")
    ap.add_argument("--lut", type=Path, default=D.LUT_MODEL,
                    help="the colour table it runs behind (default: main/models)")
    ap.add_argument("--out", type=Path, help="output folder (default: the table's folder)")
    ap.add_argument("--l2", type=float, help="fix the L2 penalty instead of searching")
    ap.add_argument("--threshold", type=float, help="fix the threshold instead of searching")
    ap.add_argument("--train-id", type=int, help="record id (default: now, Unix seconds)")
    ap.add_argument("--top", type=int, default=6, help="search rows to print")
    ap.add_argument("--max-lost", type=float, default=0.10,
                    help="exclude settings that lose more than this fraction of the balloons "
                    "stage 1 found on held-out frames (default 0.10)")
    ap.add_argument("--allow-stage0", action="store_true",
                    help="use blob-set blobs found at stage 0 (HSV) as if behind the table")
    a = ap.parse_args(argv)
    out = a.out or a.lut.parent

    try:
        m = D.models(a.lut)
    except (OSError, D.ModelError) as e:
        print("cannot use the colour table %s: %s" % (a.lut, e))
        return 2
    print("colour table %s, id %d" % (a.lut, m.lut_id))

    sessions: dict[str, list] = {}
    data_notes: list[str] = []
    for d, name in zip(a.sessions, session_names(a.sessions)):
        if is_blobset(d):
            try:
                fr, notes = load_blobset(d, name, m, a.allow_stage0)
            except SetRefused as e:
                print("cannot use blob set %s: %s" % (d, e))
                return 2
            data_notes += notes
            if not fr:
                print("no labelled frames in %s (every frame holds an unlabelled blob)" % d)
                return 2
        else:
            fr = load_session(d, name, m)
            if not fr:
                print("no labelled frames in %s" % d)
                return 2
        sessions[name] = fr
        y = np.concatenate([f.y for f in fr]) if fr else np.zeros(0)
        print("%-24s %3d frames  %3d blobs: %3d balloon, %3d not%s" % (
            name, len(fr), len(y), int((y == 1).sum()), int((y == 0).sum()),
            "   (blob set)" if is_blobset(d) else ""))
    data_notes += gain_notes(sessions)
    if any(is_blobset(d) for d in a.sessions):
        data_notes.append("blob sets hold only the balloons the table found: their FN counts "
                          "balloons the classifier drops, not ones the table missed")
    for n in data_notes:
        print("DATA WARNING: " + n)

    x, y = stack([f for fr in sessions.values() for f in fr])
    dropped_nf = sum(len(f.cents) for fr in sessions.values() for f in fr) - len(x)
    if dropped_nf:
        print("%d blobs with a non-finite feature left out of training (the camera "
              "gives them conf 0)" % dropped_nf)
    if (y == 1).sum() < 2 or (y == 0).sum() < 2:
        print("need at least two balloon blobs and two false blobs to learn from; "
              "have %d and %d" % ((y == 1).sum(), (y == 0).sum()))
        return 2

    fixed = a.l2 is not None and a.threshold is not None
    if not fixed and len(sessions) < 2:
        print("choosing l2 and the threshold needs two or more sessions; give --l2 and "
              "--threshold")
        return 2
    if a.threshold is not None and not 0 < a.threshold < 1:
        print("the threshold must be between 0 and 1")
        return 2

    rows = []
    if len(sessions) >= 2 and not fixed:
        try:
            rows = search(sessions, [a.l2] if a.l2 is not None else L2S,
                          [a.threshold] if a.threshold is not None else THRESHOLDS)
        except OneClass as e:
            print("with %s held out, the other sessions have only balloon blobs or only "
                  "false ones, so nothing can be learned for that fold. Add sessions with "
                  "both (e.g. an orange or pink balloon painted as background), or give "
                  "--l2 and --threshold" % e.args[0])
            return 2
    base = None
    if rows:
        base_folds = []
        for held, fr in sessions.items():
            s = tally(fr, keep_all)
            s["held_out"] = held
            base_folds.append(s)
        base = {k: sum(f[k] for f in base_folds) for k in base_folds[0] if k != "held_out"}
        rows = exclude_lossy(rows, base, a.max_lost)
        print("\nstage 1 alone on the same frames:  %s" % describe(base))
        for f in base_folds:
            print("      held out %-22s %s" % (f["held_out"], describe(f)))
        print("\nleave one session out, %d settings, best first:" % len(rows))
        for r in rows[:a.top]:
            print("  l2 %5g  threshold %.2f  %s%s" % (r["l2"], r["threshold"], describe(r),
                                                    "   EXCLUDED" if r["excluded"] else ""))
            for f in r["folds"]:
                print("      held out %-22s %s" % (f["held_out"], describe(f)))
    best = rows[0] if rows else dict(l2=a.l2, threshold=a.threshold)

    warnings = []
    if rows:
        if best["excluded"]:
            warnings.append("every setting loses more than %d %% of the balloons stage 1 found "
                            "on the held-out sessions; the least bad one is written. Do not fly "
                            "it: fix stage 1 (the colour table) or add sessions first."
                            % round(100 * a.max_lost))
        if best["cost"] >= base["cost"]:
            warnings.append("the classifier does not lower the held-out cost (%d, stage 1 "
                            "alone %d): it does not help on these sessions. Keep stage 1."
                            % (best["cost"], base["cost"]))
        if best["false_b1"]:
            warnings.append("the chosen setting still puts a false blob 1 on %d held-out "
                            "frame(s) without a balloon." % best["false_b1"])
        if best["fn"] > base["fn"]:
            warnings.append("the chosen setting loses balloons that stage 1 found (FN %d -> "
                            "%d)." % (base["fn"], best["fn"]))
    for w in warnings:
        print("\nWARNING: " + w)

    mdl = fit(x, y, best["l2"])
    lut_id = m.lut_id
    train_id = a.train_id if a.train_id is not None else int(time.time())
    rec = record(mdl, best["threshold"], lut_id, train_id)
    m2 = D.add_clf(D.models(a.lut), rec)        # the C check the camera runs at boot
    # The C model and this one must agree on every training blob (float32
    # against float64: only a blob sitting on the threshold could differ).
    disagree = 0
    for fr in sessions.values():
        for f in fr:
            for k in range(len(f.cents)):
                pc = D.clf_conf(m2.clf_model, f.feat[k])
                pn = float(mdl.conf(f.feat[k:k + 1])[0])
                disagree += (pc >= best["threshold"]) != (pn >= best["threshold"])
    names = D.feat_names()
    print("\nfinal classifier: l2 %g  threshold %.2f, behind table %d, trained on %s: "
          "%d blobs (%d balloon), id %d" % (best["l2"], best["threshold"], lut_id,
                                            " + ".join(sessions), len(y), int((y == 1).sum()),
                                            train_id))
    print("  weights (on standardised features, largest first):")
    for i in np.argsort(-np.abs(mdl.w)):
        print("    %-11s %+7.3f" % (names[i], mdl.w[i]))
    print("  bias %+.3f" % mdl.bias)
    if disagree:
        print("  note: %d training blob(s) on the threshold decided differently by the "
              "camera's float32" % disagree)
    for name, fr in sessions.items():
        conf = {id(f): D_conf(m2, f) for f in fr}
        print("  on %-22s (training data, optimistic) %s" % (
            name, describe(tally(fr, keep_above(conf, best["threshold"])))))

    meta = dict(train_id=train_id, lut_id=lut_id, lut=str(a.lut), l2=best["l2"],
                threshold=best["threshold"], feat_version=FEAT_VERSION,
                features=names, weights=dict(zip(names, map(float, mdl.w))),
                bias=mdl.bias, mean=list(map(float, mdl.mean)),
                scale=list(map(float, mdl.scale)),
                sessions=list(sessions), session_paths=[str(d) for d in a.sessions],
                blobs=dict(n=int(len(y)), balloon=int((y == 1).sum()), not_balloon=int((y == 0).sum())),
                objective="leave-one-session-out frame cost: false blob 1 + wrong blob 1 + FN",
                max_lost=a.max_lost,
                caveat="parameters chosen on the same folds they are scored on",
                stage1_held_out=base, warnings=warnings, data_warnings=data_notes,
                session_kinds={n: ("blobs" if is_blobset(d) else "frames")
                               for n, d in zip(sessions, a.sessions)},
                search=[{k: v for k, v in r.items()} for r in rows[:a.top]],
                record_bytes=len(rec), crc32="%08x" % struct.unpack_from("<I", rec, 16)[0])
    out.mkdir(parents=True, exist_ok=True)
    (out / "clf_model.bin").write_bytes(rec)
    (out / "clf_model.h").write_text(c_header(rec, meta))
    (out / "clf_model.json").write_text(json.dumps(meta, indent=1) + "\n")
    print("wrote %s/clf_model.{bin,h,json}" % out)
    if out.resolve() != D.LUT_MODEL.parent.resolve():
        print("next: eval_frames.py --stage 2 --lut %s --clf %s DIR..." % (
            a.lut, out / "clf_model.bin"))
    return 0


def D_conf(m: D.TcModels, f) -> np.ndarray:
    """The camera's confidences for a frame's candidates (blob_clf_conf)."""
    return np.array([D.clf_conf(m.clf_model, f.feat[k]) for k in range(len(f.cents))])


if __name__ == "__main__":
    sys.exit(main())
