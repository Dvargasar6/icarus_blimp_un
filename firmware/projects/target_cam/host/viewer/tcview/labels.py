"""
Hand labels for saved frames, and the scores computed from them.

A saved frame is <stem>.rgb565 (the camera's raw bytes) + <stem>.json (its
settings and CSV row) + <stem>.png, written by the Calib tab's Save frame. Its
labels are <stem>.labels.png, 8-bit greyscale at the frame's size:

    0  unlabelled   no claim; left out of training and of every score
    1  balloon
    2  background

"Unlabelled" is a class of its own on purpose. The pixels on a balloon's rim
are part balloon, part wall, and forcing them into either class would teach
the colour table something false. Leaving them out costs nothing.

One labelling pass gives both things Phase A needs: the balloon and background
pixels the colour table is trained on, and the ground truth the detector is
scored against. A balloon, for scoring, is a connected component of label 1
(8-connected), with its bounding box.

This module is the one copy of that logic: the Label tab and
host/tools/eval_frames.py both import it, so they cannot disagree about what a
balloon is or what counts as a hit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import detector as D
from . import protocol as P

UNLABELLED, BALLOON, BACKGROUND = 0, 1, 2
LABEL_SUFFIX = ".labels.png"

# A balloon component smaller than this is taken as a stray brush dab, not a
# balloon: no detector is expected to find a 3-pixel target.
MIN_BALLOON_PX = 4


# ---- saved frames ------------------------------------------------------------

@dataclass
class SavedFrame:
    stem: Path              # the path without suffix
    meta: dict
    frame: P.Frame

    @property
    def label_path(self) -> Path:
        return Path(str(self.stem) + LABEL_SUFFIX)

    def settings(self) -> D.Settings:
        """The detector settings the camera ran when it took this frame. The
        byte order comes from the frame's own fmt: that is the truth about
        the bytes in the file."""
        st = D.Settings.from_cfg(self.meta.get("camera_cfg") or {})
        st.be = 1 if self.frame.big_endian else 0
        st.w, st.h = self.frame.w, self.frame.h
        return st


def list_frames(folder: Path) -> list[Path]:
    """Every saved frame under folder (subfolders included: one subfolder per
    capture session), as stems, sorted."""
    folder = Path(folder)
    return sorted(p.with_suffix("") for p in folder.rglob("*.rgb565")
                  if p.with_suffix(".json").exists())


def load_frame(stem: Path) -> SavedFrame:
    stem = Path(stem)
    meta = json.loads(stem.with_suffix(".json").read_text())
    data = stem.with_suffix(".rgb565").read_bytes()
    w, h = int(meta.get("w", 160)), int(meta.get("h", 120))
    if len(data) != w * h * 2:
        raise ValueError("%s: %d bytes, expected %d for %dx%d RGB565"
                         % (stem.name, len(data), w * h * 2, w, h))
    f = P.Frame(seq=int(meta.get("seq", 0)), t_ms=int(meta.get("t_ms", 0)), w=w, h=h,
                fmt=meta.get("fmt", "rgb565be"), data=data)
    return SavedFrame(stem, meta, f)


# ---- label files -------------------------------------------------------------
# QImage reads and writes the PNG: it needs no QApplication, and PyQt6 is
# already in the env, so no image library is added for one file format.

def load_labels(path: Path, w: int, h: int) -> np.ndarray:
    """(h, w) uint8; all UNLABELLED if the file does not exist."""
    from PyQt6.QtGui import QImage
    path = Path(path)
    if not path.exists():
        return np.zeros((h, w), dtype=np.uint8)
    img = QImage(str(path))
    if img.isNull():
        raise ValueError("%s: not a readable image" % path)
    if (img.width(), img.height()) != (w, h):
        raise ValueError("%s: %dx%d, the frame is %dx%d"
                         % (path, img.width(), img.height(), w, h))
    img = img.convertToFormat(QImage.Format.Format_Grayscale8)
    bits = img.constBits()
    bits.setsize(img.sizeInBytes())
    lab = np.frombuffer(bits, np.uint8).reshape(h, img.bytesPerLine())[:, :w].copy()
    if lab.max(initial=0) > BACKGROUND:
        raise ValueError("%s: value %d is not a label (0, 1, 2)" % (path, lab.max()))
    return lab


def save_labels(path: Path, lab: np.ndarray):
    from PyQt6.QtGui import QImage
    lab = np.ascontiguousarray(lab, dtype=np.uint8)
    if lab.max(initial=0) > BACKGROUND:
        raise ValueError("label array holds %d" % lab.max())
    h, w = lab.shape
    img = QImage(lab.data, w, h, w, QImage.Format.Format_Grayscale8).copy()
    if not img.save(str(path), "PNG"):
        raise OSError("could not write %s" % path)


# ---- balloons as connected components ----------------------------------------

@dataclass
class Component:
    area: int
    x0: int
    y0: int
    x1: int                 # inclusive
    y1: int
    cx: float               # blob_detect's convention: pixel i spans [i, i+1),
    cy: float               # so a pixel's centre is i + 0.5

    def contains(self, x: float, y: float) -> bool:
        """(x, y) in the same continuous coordinates as the camera's
        centroids: the box covers [x0, x1 + 1) x [y0, y1 + 1)."""
        return self.x0 <= x < self.x1 + 1 and self.y0 <= y < self.y1 + 1


def components(binary: np.ndarray, min_px: int = 1) -> list[Component]:
    """8-connected components of a boolean image, largest first. Union-find
    over the runs of each row: a 160x120 frame is a few hundred runs, so
    plain Python is fast enough and no scipy is needed."""
    h, w = binary.shape
    parent: list[int] = []

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    runs = []                                   # (y, x0, x1 exclusive, id)
    prev = []
    for y in range(h):
        row = np.flatnonzero(np.diff(np.concatenate(([0], binary[y].astype(np.int8), [0]))))
        cur = []
        for x0, x1 in zip(row[::2], row[1::2]):
            rid = len(parent)
            parent.append(rid)
            for py, px0, px1, pid in prev:      # 8-connected: overlap within 1 px
                if px0 <= x1 and x0 <= px1:
                    a, b = find(pid), find(rid)
                    if a != b:
                        parent[b] = a
            cur.append((y, int(x0), int(x1), rid))
        runs.extend(cur)
        prev = cur
    acc: dict[int, list] = {}
    for y, x0, x1, rid in runs:
        root = find(rid)
        n = x1 - x0
        a = acc.setdefault(root, [0, w, h, -1, -1, 0.0, 0.0])
        a[0] += n
        a[1], a[2] = min(a[1], x0), min(a[2], y)
        a[3], a[4] = max(a[3], x1 - 1), max(a[4], y)
        a[5] += (x0 + x1) * n / 2.0             # sum of (x + 0.5) over the run
        a[6] += (y + 0.5) * n
    out = [Component(a[0], a[1], a[2], a[3], a[4], a[5] / a[0], a[6] / a[0])
           for a in acc.values() if a[0] >= min_px]
    out.sort(key=lambda c: -c.area)
    return out


def balloons(lab: np.ndarray) -> list[Component]:
    return components(lab == BALLOON, MIN_BALLOON_PX)


def rest_to_background(lab: np.ndarray, band: int = 2) -> np.ndarray:
    """Every unlabelled pixel becomes background, except a band of `band` px
    around each balloon pixel, which stays unlabelled: the rim is mixed
    colour and belongs to neither class."""
    near = lab == BALLOON
    for _ in range(band):                       # dilation, 8-connected
        g = near.copy()
        g[1:] |= near[:-1]
        g[:-1] |= near[1:]
        g[:, 1:] |= near[:, :-1]
        g[:, :-1] |= near[:, 1:]
        g[1:, 1:] |= near[:-1, :-1]
        g[1:, :-1] |= near[:-1, 1:]
        g[:-1, 1:] |= near[1:, :-1]
        g[:-1, :-1] |= near[1:, 1:]
        near = g
    out = lab.copy()
    out[(lab == UNLABELLED) & ~near] = BACKGROUND
    return out


# ---- scoring -----------------------------------------------------------------

@dataclass
class FrameScore:
    """One frame against its labels. Blob level is what the ESP32-S3 sees:
    the (at most 4) reported blobs, largest first."""
    name: str
    stage_eff: int
    n_balloons: int
    n_reported: int
    tp: int = 0             # balloons found (a reported blob's centroid in its box)
    fn: int = 0             # balloons no reported blob landed in
    fp: int = 0             # reported blobs in no balloon's box
    split: int = 0          # extra blobs in an already-found balloon
    b1: str = "none"        # blob 1 is: "balloon", "false", or "none" (no blob)
    px_tp: int = 0          # mask on balloon-labelled pixels
    px_fp: int = 0          # mask on background-labelled pixels
    px_fn: int = 0          # balloon-labelled pixels the mask missed
    px_labelled: int = 0
    notes: list = field(default_factory=list)


def score(name: str, lab: np.ndarray, blobs: list, mask: np.ndarray,
          stage_eff: int = 0) -> FrameScore:
    """blobs: (cx, cy) of the reported blobs, largest first. mask: the pixel
    test, 1 = matches, at the frame's size."""
    bl = balloons(lab)
    s = FrameScore(name, stage_eff, len(bl), len(blobs))
    found = [False] * len(bl)
    for k, (cx, cy) in enumerate(blobs):
        hit = next((i for i, b in enumerate(bl) if b.contains(cx, cy)), None)
        if hit is None:
            s.fp += 1
            verdict = "false"
        elif found[hit]:
            s.split += 1
            verdict = "balloon"
        else:
            found[hit] = True
            s.tp += 1
            verdict = "balloon"
        if k == 0:
            s.b1 = verdict
    s.fn = found.count(False)
    m = mask.astype(bool)
    s.px_tp = int((m & (lab == BALLOON)).sum())
    s.px_fp = int((m & (lab == BACKGROUND)).sum())
    s.px_fn = int((~m & (lab == BALLOON)).sum())
    s.px_labelled = int((lab != UNLABELLED).sum())
    return s


def score_frame(sf: SavedFrame, lab: np.ndarray, stage: int = 0,
                st: D.Settings | None = None, m: D.TcModels | None = None) -> FrameScore:
    """Run the camera's pipeline (through the shared library) on a saved frame
    and score it. The pixel mask is the colour test of the stage that ran:
    the HSV window at 0, the colour table from 1 up. m: the models to offer
    (default: the ones the firmware compiles in)."""
    st = st or sf.settings()
    m = m if m is not None else D.models()
    r = D.pipeline(st, sf.frame.data, stage, m)
    blobs = [(r.r.b[k].cx, r.r.b[k].cy) for k in range(r.r.n_report)]
    return score(sf.stem.name, lab, blobs, D.mask(st, sf.frame.data, stage, m), r.stage)


def ratio(a: int, b: int) -> float:
    return a / b if b else float("nan")
