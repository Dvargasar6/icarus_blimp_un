#!/usr/bin/env python3
"""
Blob dataset from labelled frames, for stage 2's classifier (Phase B).

Runs the camera's own pipeline (the shared library) on every saved frame
under the given folders and writes one row per reported blob: its features,
its 24x24 crop and a label taken from the frame's paint (tcview's Label tab).
A blob is a balloon if its centroid lies in a painted balloon's box, not a
balloon if the frame is labelled but no balloon contains it, and "?" (-1) if
the frame has no labels (with --all; by default such frames are skipped).
A frame reached through two of the folders counts once.
The rule and the file format are tcview/blobset.py's, the same ones the
viewer's Blobs tab uses, so this file and a tab-saved one can be merged.

--stage picks the detector the blobs come from. The default 1 (the colour
table) is what stage 2 will run behind, so it gives the false blobs stage 2
will really have to reject.

Usage:
  blobs_from_frames.py target_cam/data/red_s1 target_cam/data/red_s2 -o blobs.npz
  blobs_from_frames.py --stage 0 --all DIR -o out.npz

Exit status 2 if no blob was found.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "viewer"))
from tcview import blobset as B         # noqa: E402
from tcview import detector as D        # noqa: E402
from tcview import labels as L          # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True, help="the .npz to write")
    ap.add_argument("--stage", type=int, default=1, choices=range(0, D.TC_STAGE_MAX + 1))
    ap.add_argument("--lut", type=Path, help="colour table record (default: the committed one)")
    ap.add_argument("--all", action="store_true", help="unlabelled frames too, as '?'")
    args = ap.parse_args(argv)

    m = D.models(args.lut)
    samples = []
    seen = set()            # folders are searched recursively and may overlap
    for d in args.folders:
        new = []
        for stem in L.list_frames(d):
            sf = L.load_frame(stem)
            if stem.resolve() in seen or (not args.all and not sf.label_path.exists()):
                continue
            seen.add(stem.resolve())
            new += B.from_frame(sf, stage=args.stage, m=m)
        c = B.counts(new)
        print("%-40s %4d blobs: %d balloon, %d not, %d ?" % (
            d, c["n"], c["balloon"], c["not"], c["unlabelled"]))
        samples += new
    if not samples:
        print("no blobs found")
        return 2
    eff = sorted({s.stage for s in samples})
    if eff != [args.stage]:
        print("note: ran at stage %s (stage %d fell back: model missing)"
              % (",".join(map(str, eff)), args.stage))
    B.save(args.out, samples)
    c = B.counts(samples)
    print("%d blobs (%d balloon, %d not, %d ?) -> %s"
          % (c["n"], c["balloon"], c["not"], c["unlabelled"], args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
