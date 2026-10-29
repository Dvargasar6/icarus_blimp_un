#!/usr/bin/env python3
"""
Train the stage 1 colour table from hand-labelled frames.

Every argument is a folder of saved frames with their .labels.png (tcview's
Label tab), and each folder is one capture session. The table that comes out
is the model record main/color_lut.c checks at boot, written three ways:

  main/models/lut_model.bin    the record itself (tcview, eval_frames)
  main/models/lut_model.h      the same bytes as a C array (the firmware)
  main/models/lut_model.json   how it was made: parameters, data, fold scores

How a table is learned (the reasoning is in docs/learned-detector-report.md):

 1. Count. For each session, how often each of the 65 536 RGB565 values
    occurs among balloon pixels and among background pixels. Unlabelled
    pixels (the rim band) count for neither.
 2. Smooth. Twenty frames show about 2 500 distinct balloon colours, and the
    next frame will show neighbours of them that were never seen. Both counts
    are blurred with a 3-D Gaussian over the 32x64x32 RGB grid, so a colour
    inherits the evidence of its neighbours. sigma is in 8-bit units on every
    axis: green has 64 levels and red/blue 32, so one green step is half a
    red step, and a fixed blur in grid steps would not be round.
 3. Decide. A colour is in the table when it is k times likelier among
    balloon pixels than among background ones (each count divided by its own
    total, so it does not matter that background outnumbers balloon 20 to 1)
    and has at least min_evidence (smoothed) balloon pixels behind it. A
    colour with no evidence stays out: the table never claims what it has not
    seen.

How sigma, k and min_evidence are chosen: leave one session out. Train on all
sessions but one, score on the one left out, for each session in turn. The
objective is the held-out pixel F1, averaged over the folds so each session
weighs the same. Blob counts are not the objective: with 14 balloons one
frame moves recall by 7 points, too coarse to choose between settings, and
the blobs follow from the pixels anyway. One blob outcome is a hard rule,
though: a setting that puts a false blob 1 on any held-out frame without a
balloon is excluded, because blob 1 is what the blimp steers to. Every
setting's blob counts are printed and kept in the .json. The final table is
then trained on every session with the chosen parameters.

The parameters are chosen on the same folds they are scored on, so the fold
scores are slightly optimistic. A session kept out of all of this is the only
unbiased test.

Usage (from target_cam/):
  python host/train/train_lut.py --out data/lut_try data/red_s1 data/red_s2   (a trial)
  python host/train/train_lut.py host/testdata/red_s1 host/testdata/red_s2   (writes main/models)
  python host/train/train_lut.py --sigma 8 --k 7 DIR...   (no search; one session is enough)

The step-by-step routine, how to judge a new table and what to do when
something goes wrong are in the README, "Retraining the colour table".
"""
from __future__ import annotations

import argparse
import itertools
import json
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "viewer"))
from tcview import detector as D        # noqa: E402
from tcview import labels as L          # noqa: E402

MAGIC = b"TCLU"
VERSION = 1
HEADER_BYTES = 20
NCOL = 65536
GRID = (32, 64, 32)                     # r, g, b levels of RGB565
STEP = (255 / 31, 255 / 63, 255 / 31)   # 8-bit units per grid step

SIGMAS = (0, 2, 4, 6, 8, 12, 16)
KS = (2, 3, 5, 7, 10, 15, 20, 30)
MIN_EVIDENCE = (0.5, 1.0, 2.0)


# --- data -------------------------------------------------------------------

@dataclass
class Frame:
    sf: L.SavedFrame
    lab: np.ndarray
    px: np.ndarray          # (h, w) RGB565 values, byte order resolved


def load_session(folder: Path) -> list[Frame]:
    out = []
    for stem in L.list_frames(folder):
        sf = L.load_frame(stem)
        if not sf.label_path.exists():
            continue
        f = sf.frame
        lab = L.load_labels(sf.label_path, f.w, f.h)
        px = np.frombuffer(f.data, ">u2" if f.big_endian else "<u2").reshape(f.h, f.w)
        out.append(Frame(sf, lab, px))
    return out


def counts(frames: list[Frame]) -> tuple[np.ndarray, np.ndarray]:
    """Balloon and background pixel counts per RGB565 value."""
    b = np.zeros(NCOL)
    g = np.zeros(NCOL)
    for f in frames:
        b += np.bincount(f.px[f.lab == L.BALLOON], minlength=NCOL)
        g += np.bincount(f.px[f.lab == L.BACKGROUND], minlength=NCOL)
    return b, g


# --- the table ----------------------------------------------------------------

def _blur_axis(a: np.ndarray, axis: int, sigma_steps: float) -> np.ndarray:
    """1-D Gaussian along one axis, zero beyond the grid's edge (no colour lies
    outside RGB565, so nothing leaks in from there). The kernel's peak is 1,
    not its sum: a colour keeps the full weight of its own pixels and its
    neighbours get a fraction of it. A smoothed count is then "pixels' worth
    of evidence at this colour" whatever sigma is, so one min_evidence means
    the same thing across the search. The ratio test is unaffected: both
    counts are scaled alike."""
    if sigma_steps <= 0:
        return a
    r = int(np.ceil(3 * sigma_steps))
    x = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (x / sigma_steps) ** 2)
    pad = [(0, 0)] * 3
    pad[axis] = (r, r)
    ap = np.pad(a, pad)
    out = np.zeros_like(a)
    n = a.shape[axis]
    for i, w in enumerate(k):
        sl = [slice(None)] * 3
        sl[axis] = slice(i, i + n)
        out += w * ap[tuple(sl)]
    return out


def smooth(c: np.ndarray, sigma: float) -> np.ndarray:
    """A separable 3-D Gaussian of sigma 8-bit units on the RGB565 grid. The
    index of value px = r<<11 | g<<5 | b is r*2048 + g*32 + b, which is
    exactly C order for shape (32, 64, 32)."""
    a = c.reshape(GRID)
    for axis in range(3):
        a = _blur_axis(a, axis, sigma / STEP[axis])
    return a.ravel()


def table(b: np.ndarray, g: np.ndarray, sigma: float, k: float,
          min_evidence: float) -> np.ndarray:
    """(8192,) uint8, bit px & 7 of byte px >> 3 for value px (color_lut.h)."""
    bs, gs = smooth(b, sigma), smooth(g, sigma)
    pb = bs / max(b.sum(), 1)
    pg = gs / max(g.sum(), 1)
    on = (pb > k * pg) & (bs >= min_evidence)
    return np.packbits(on.astype(np.uint8), bitorder="little")


def record(bits: np.ndarray, train_id: int) -> bytes:
    """The model record (tc_pipeline.h): a 20-byte little-endian header, with
    the CRC-32 over header bytes 0..15 and then the payload."""
    payload = bits.astype(np.uint8).tobytes()
    head = struct.pack("<4sHHII", MAGIC, VERSION, HEADER_BYTES, len(payload), train_id)
    crc = zlib.crc32(head + payload) & 0xFFFFFFFF
    return head + struct.pack("<I", crc) + payload


def c_header(rec: bytes, meta: dict) -> str:
    lines = [", ".join("0x%02x" % x for x in rec[i:i + 16]) for i in range(0, len(rec), 16)]
    return (
        "/*\n"
        " * Generated by host/train/train_lut.py - do not edit.\n"
        " *\n"
        " * The stage 1 colour table as a model record (tc_pipeline.h), the same\n"
        " * bytes as lut_model.bin. target_main.c checks it with color_lut_load()\n"
        " * at boot and runs without stage 1 if it is refused.\n"
        " *\n"
        " * train id %d, sigma %g, k %g, min_evidence %g, %d colours accepted\n"
        " * trained on: %s\n"
        " */\n"
        "#pragma once\n\n"
        "#include <stddef.h>\n"
        "#include <stdint.h>\n\n"
        "static const uint8_t lut_model_rec[%d] = {\n    %s,\n};\n"
        % (meta["train_id"], meta["sigma"], meta["k"], meta["min_evidence"],
           meta["colours"], ", ".join(meta["sessions"]), len(rec), ",\n    ".join(lines)))


# --- scoring ------------------------------------------------------------------

def score(frames: list[Frame], m: D.TcModels) -> dict:
    t = dict(frames=len(frames), balloons=0, tp=0, fn=0, fp=0, split=0,
             false_b1=0, empty_frames=0, px_tp=0, px_fp=0, px_fn=0)
    for f in frames:
        s = L.score_frame(f.sf, f.lab, 1, None, m)
        if s.stage_eff != 1:
            raise RuntimeError("the table was not used (stage %d ran)" % s.stage_eff)
        t["balloons"] += s.n_balloons
        for k in ("tp", "fn", "fp", "split", "px_tp", "px_fp", "px_fn"):
            t[k] += getattr(s, k)
        if not s.n_balloons:
            t["empty_frames"] += 1
            t["false_b1"] += s.b1 == "false"
    return t


def fbeta(tp: int, fp: int, fn: int, beta: float) -> float:
    d = (1 + beta * beta) * tp + beta * beta * fn + fp
    return (1 + beta * beta) * tp / d if d else 0.0


def search(sessions: dict[str, list[Frame]], grid) -> list[dict]:
    """Leave-one-session-out over the parameter grid."""
    cnt = {name: counts(fr) for name, fr in sessions.items()}
    rows = []
    for sigma, k, mev in grid:
        folds = []
        for held in sessions:
            b = sum(cnt[n][0] for n in sessions if n != held)
            g = sum(cnt[n][1] for n in sessions if n != held)
            bits = table(b, g, sigma, k, mev)
            s = score(sessions[held], D.lut_models(bits=bits))
            s["held_out"] = held
            s["colours"] = int(np.unpackbits(bits).sum())
            folds.append(s)
        tp, fp, fn = (sum(f[x] for f in folds) for x in ("tp", "fp", "fn"))
        px_f1 = float(np.mean([fbeta(f["px_tp"], f["px_fp"], f["px_fn"], 1.0) for f in folds]))
        rows.append(dict(sigma=sigma, k=k, min_evidence=mev, blob_f05=fbeta(tp, fp, fn, 0.5),
                         px_f1=px_f1, folds=folds))
    for r in rows:
        r["false_b1"] = sum(f["false_b1"] for f in r["folds"])
    rows.sort(key=lambda r: (r["false_b1"] > 0, -r["px_f1"], r["sigma"], r["k"]))
    return rows


def describe(f: dict) -> str:
    p = L.ratio(f["px_tp"], f["px_tp"] + f["px_fp"])
    r = L.ratio(f["px_tp"], f["px_tp"] + f["px_fn"])
    return ("TP %d FN %d FP %2d split %d  false b1 %d/%d  px P %5.1f R %5.1f" % (
        f["tp"], f["fn"], f["fp"], f["split"], f["false_b1"], f["empty_frames"],
        100 * p, 100 * r))


# --- main ---------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("sessions", nargs="+", type=Path, help="one folder per capture session")
    ap.add_argument("--sigma", type=float, help="fix sigma (8-bit units) instead of searching")
    ap.add_argument("--k", type=float, help="fix the likelihood ratio instead of searching")
    ap.add_argument("--min-evidence", type=float, default=None,
                    help="fix the balloon evidence floor (default: searched, or 0.5)")
    ap.add_argument("--out", type=Path, default=D.LUT_MODEL.parent,
                    help="output folder (default: main/models)")
    ap.add_argument("--train-id", type=int, help="record id (default: now, Unix seconds)")
    ap.add_argument("--top", type=int, default=8, help="search rows to print")
    a = ap.parse_args(argv)

    # A session is named by its folder, or parent/folder where two chosen
    # folders share a name (data/test/red_s1, data/train/Without_AWB/red_s1):
    # keyed by name alone, the second would silently replace the first.
    names = [d.name for d in a.sessions]
    sessions = {}
    for d in a.sessions:
        fr = load_session(d)
        if not fr:
            print("no labelled frames in %s" % d)
            return 2
        sessions["%s/%s" % (d.parent.name, d.name) if names.count(d.name) > 1 else d.name] = fr
    fixed = a.sigma is not None and a.k is not None
    if not fixed and len(sessions) < 2:
        print("choosing sigma and k needs two or more sessions; give --sigma and --k")
        return 2

    for name, fr in sessions.items():
        b, g = counts(fr)
        print("%-10s %2d frames  %6d balloon px (%d colours)  %7d background px" % (
            name, len(fr), b.sum(), (b > 0).sum(), g.sum()))

    grid = list(itertools.product(
        [a.sigma] if a.sigma is not None else SIGMAS,
        [a.k] if a.k is not None else KS,
        [a.min_evidence] if a.min_evidence is not None
        else (MIN_EVIDENCE if not fixed else (0.5,))))
    rows = search(sessions, grid) if len(sessions) >= 2 else []
    if rows:
        print("\nleave one session out, %d settings, best first:" % len(rows))
        for r in rows[:a.top]:
            print("  sigma %4g  k %4g  min_ev %3g  px F1 %.3f  blob F0.5 %.3f%s" % (
                r["sigma"], r["k"], r["min_evidence"], r["px_f1"], r["blob_f05"],
                "  EXCLUDED: false blob 1" if r["false_b1"] else ""))
            for f in r["folds"]:
                print("      held out %-8s %s" % (f["held_out"], describe(f)))
    if rows and rows[0]["false_b1"]:
        # Rows are sorted excluded-last, so an excluded first row means every
        # setting failed the hard rule. The trainer still writes the best of
        # them, so the person running it can look, but it must say so loudly:
        # this table would put a false target in front of the blimp.
        print("\nWARNING: every setting puts a false blob 1 on a held-out frame without a\n"
              "balloon. Do not fly this table. Check those frames' labels (is the balloon\n"
              "really out of view?) with eval_frames.py, and keep the current table.")
    best = rows[0] if rows else dict(sigma=a.sigma, k=a.k, min_evidence=a.min_evidence or 0.5)

    b, g = counts([f for fr in sessions.values() for f in fr])
    bits = table(b, g, best["sigma"], best["k"], best["min_evidence"])
    train_id = a.train_id if a.train_id is not None else int(time.time())
    rec = record(bits, train_id)
    m = D.lut_models(record=rec)            # the C check the camera runs at boot
    colours = int(np.unpackbits(bits).sum())
    print("\nfinal table: sigma %g  k %g  min_evidence %g, trained on %s: %d colours, id %d" % (
        best["sigma"], best["k"], best["min_evidence"], " + ".join(sessions), colours, train_id))
    for name, fr in sessions.items():
        print("  on %-8s (training data, optimistic) %s" % (name, describe(score(fr, m))))

    meta = dict(train_id=train_id, sigma=best["sigma"], k=best["k"],
                min_evidence=best["min_evidence"], colours=colours,
                sessions=list(sessions), session_paths=[str(d) for d in a.sessions],
                frames={n: [f.sf.stem.name for f in fr]
                                                 for n, fr in sessions.items()},
                objective="leave-one-session-out mean pixel F1; no false blob 1 on held-out empty frames",
                caveat="parameters chosen on the same folds they are scored on",
                search=[{k: v for k, v in r.items()} for r in rows[:a.top]],
                record_bytes=len(rec), crc32="%08x" % struct.unpack_from("<I", rec, 16)[0])
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "lut_model.bin").write_bytes(rec)
    (a.out / "lut_model.h").write_text(c_header(rec, meta))
    (a.out / "lut_model.json").write_text(json.dumps(meta, indent=1) + "\n")
    print("wrote %s/lut_model.{bin,h,json}" % a.out)
    if a.out.resolve() != D.LUT_MODEL.parent.resolve():
        print("next: eval_frames.py --stage 1 --lut %s %s\n"
              "      and, to use it, copy lut_model.* into main/models/, rebuild and flash"
              % (a.out / "lut_model.bin", " ".join(str(d) for d in a.sessions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
