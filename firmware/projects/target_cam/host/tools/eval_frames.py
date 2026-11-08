#!/usr/bin/env python3
"""
How good is the detector on labelled frames?

Runs the camera's own pipeline (the shared library, the same C the firmware
runs) on every saved frame that has a <stem>.labels.png, and scores it against
the hand labels (tcview's Label tab):

  blob level, what the ESP32-S3 sees (the <= 4 reported blobs, largest first):
    TP     a balloon with a reported blob's centroid inside its box
    FN     a balloon no reported blob landed in (a miss)
    FP     a reported blob in no balloon's box (a false detection)
    split  a further blob in a balloon already found: the balloon broke up.
           Not a false detection, but it costs a report slot.
    b1     blob 1 is the one the controller steers to: is it a balloon?
  pixel level, on labelled pixels only (unlabelled ones make no claim):
    precision  matched balloon px / all matched labelled px
    recall     matched balloon px / all balloon px

Each frame is run with the settings the camera had when it was saved (its
.json), unless --cfg gives one settings file for all of them, which is how
two calibrations are compared on the same frames. From stage 1 up the pixel
test is the colour table, not the HSV settings: the committed one
(main/models/lut_model.bin) unless --lut names another record. Stage 2 adds
the blob classifier: the committed one if it belongs to the committed table,
or the record --clf names, which must belong to the table in use. The pixel
figures do not change at stage 2 (the classifier drops blobs, not pixels).

Usage:
  eval_frames.py target_cam/data/frames
  eval_frames.py --stage 1 --cfg some_frame.json target_cam/host/testdata
  eval_frames.py --stage 1 --lut /tmp/lut/lut_model.bin target_cam/host/testdata
  eval_frames.py --stage 2 --lut T/lut_model.bin --clf T/clf_model.bin DIR
  eval_frames.py --csv scores.csv DIR

Exit status 2 if no labelled frame was found.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "viewer"))
from tcview import detector as D        # noqa: E402
from tcview import labels as L          # noqa: E402


def pct(x: float) -> str:
    return "  -  " if math.isnan(x) else "%5.1f" % (100.0 * x)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("dirs", nargs="+", type=Path, help="folders of saved frames "
                    "(searched recursively)")
    ap.add_argument("--stage", type=int, default=0, choices=range(D.TC_STAGE_MAX + 1),
                    help="detector stage to request (falls back like the camera)")
    ap.add_argument("--cfg", type=Path, help="a saved frame's .json: use its camera "
                    "settings for every frame")
    ap.add_argument("--lut", type=Path, help="a colour table record for stage >= 1 "
                    "(default: the committed main/models/lut_model.bin)")
    ap.add_argument("--clf", type=Path, help="a blob classifier record for stage >= 2, "
                    "trained behind the table in use (default: the committed one, if it is)")
    ap.add_argument("--csv", type=Path, help="also write one row per frame here")
    ap.add_argument("-q", "--quiet", action="store_true", help="totals only")
    a = ap.parse_args(argv)

    try:
        m = D.models(a.lut or (D.LUT_MODEL if a.clf else None), a.clf)
    except (OSError, D.ModelError) as e:
        print("cannot use %s: %s" % (a.clf or a.lut, e))
        return 2

    st_all = None
    if a.cfg:
        st_all = D.Settings.from_cfg(json.loads(a.cfg.read_text()).get("camera_cfg") or {})

    scores, unlabelled = [], 0
    for d in a.dirs:
        for stem in L.list_frames(d):
            sf = L.load_frame(stem)
            if not sf.label_path.exists():
                unlabelled += 1
                continue
            lab = L.load_labels(sf.label_path, sf.frame.w, sf.frame.h)
            st = None
            if st_all is not None:
                st = D.Settings(**{**asdict(st_all), "be": sf.settings().be})
            scores.append(L.score_frame(sf, lab, a.stage, st, m))
    if not scores:
        print("no labelled frames under %s (%d saved frames without labels)"
              % (", ".join(map(str, a.dirs)), unlabelled))
        return 2

    if not a.quiet:
        print("%-36s %3s %3s %3s %3s %5s  %-7s %6s %6s" % (
            "frame", "bal", "TP", "FN", "FP", "split", "blob 1", "px P%", "px R%"))
        for s in scores:
            print("%-36s %3d %3d %3d %3d %5d  %-7s %6s %6s" % (
                s.name[:36], s.n_balloons, s.tp, s.fn, s.fp, s.split, s.b1,
                pct(L.ratio(s.px_tp, s.px_tp + s.px_fp)), pct(L.ratio(s.px_tp, s.px_tp + s.px_fn))))
        print()

    tot = {k: sum(getattr(s, k) for s in scores)
           for k in ("n_balloons", "tp", "fn", "fp", "split", "px_tp", "px_fp", "px_fn")}
    stages = sorted({s.stage_eff for s in scores})
    with_bal = [s for s in scores if s.n_balloons]
    without = [s for s in scores if not s.n_balloons]
    print("frames        %d labelled (%d with a balloon, %d without); %d saved frames "
          "not labelled" % (len(scores), len(with_bal), len(without), unlabelled))
    print("stage         requested %d, ran %s; settings from %s" % (
        a.stage, "/".join(map(str, stages)), a.cfg or "each frame's own .json"))
    print("blobs         TP %d  FN %d  FP %d  split %d   precision %s %%  recall %s %%" % (
        tot["tp"], tot["fn"], tot["fp"], tot["split"],
        pct(L.ratio(tot["tp"], tot["tp"] + tot["fp"])),
        pct(L.ratio(tot["tp"], tot["n_balloons"]))))
    b1_ok = sum(1 for s in with_bal if s.b1 == "balloon")
    print("blob 1        a balloon in %d of %d frames with one; a false blob 1 in %d of "
          "%d frames without" % (b1_ok, len(with_bal),
                                 sum(1 for s in without if s.b1 == "false"), len(without)))
    if m.lut and max(stages) >= 1:
        print("colour table  id %d, %s" % (m.lut_id, m.lut_path))
    if m.clf and max(stages) >= 2:
        print("classifier    id %d, threshold %.2f, %s" % (m.clf_id, m.clf_model.threshold,
                                                          m.clf_path))
    test = "/".join("HSV window" if n == 0 else "colour table" for n in stages)
    print("pixels        precision %s %%  recall %s %%   (%s; on labelled pixels only)" % (
        pct(L.ratio(tot["px_tp"], tot["px_tp"] + tot["px_fp"])),
        pct(L.ratio(tot["px_tp"], tot["px_tp"] + tot["px_fn"])), test))

    if a.csv:
        keys = [k for k in asdict(scores[0]) if k != "notes"]
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, keys, extrasaction="ignore")
            w.writeheader()
            for s in scores:
                w.writerow(asdict(s))
        print("per-frame rows written to %s" % a.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
