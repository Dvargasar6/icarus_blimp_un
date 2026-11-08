"""
Blob datasets for stage 2 (the blob classifier, Phase B): one row per blob,
with its features, its 24x24 crop and a label. No Qt here, so the Blobs tab,
host/tools/blobs_from_frames.py, the training script and the tests all use
this one copy of what a sample is and how it is saved.

Two sources, one format:

  live      '!collect 1' records from the camera (protocol.BlobRec): the
            features as the camera computed them, in the light and at the
            ranges the blimp will fly in. They arrive unlabelled ("?") - B1
            has no classifier to pre-fill a verdict - and are labelled by
            hand in the Blobs tab.
  frame     blobs rebuilt on the PC from saved, hand-labelled frames, through
            the camera's own C code (libblob_detect.so), so they are the same
            numbers the camera would have sent. They are labelled
            automatically: a blob is a balloon if its centroid lies inside a
            painted balloon component's box (labels.balloons(), the rule the
            frame scores already use); not a balloon if the frame was
            labelled but no balloon contains it; unlabelled if the frame has
            no labels at all.

Labels: 1 balloon, 0 not a balloon, -1 unlabelled. Unlabelled samples are
kept in the file (they are still data for a later pass) and the training
leaves them out.

The .npz holds only plain arrays (np.load(..., allow_pickle=False) reads it):

  feat        (N, F) float32      blob_feat.h, in feat_names order
  crop        (N, 24, 24) uint16  RGB565 values (not bytes)
  label       (N,) int8
  source      (N,) str            "live" or "frame"
  ref         (N,) str            live: "t<t_ms>"; frame: "<folder>/<stem>" (sessions
                                  reuse frame names, so the folder is part of it)
  t_ms, k, stage, area            (N,) int32
  cx, cy, conf                    (N,) float32
  box         (N, 4) int32        x0, y0, x1, y1 inclusive
  crop_box    (N, 3) int32        xs, ys, side
  fw          (N,) str            firmware version ("pc" for frame samples)
  wb          (N,) str            white balance the pixels were made with: auto,
                                  frozen, fixed, or unrecorded (older firmware; files
                                  without the column load as unrecorded)
  wb_gain     (N, 3) uint8        its R, G, B gains (64 = 1.0); 0,0,0 = not known.
                                  Colour from different gains is not comparable
                                  (learned-detector report §10.7), so a training
                                  set should not mix them blindly
  lut_id      (N,) int64          train id of the colour table the blob was found
                                  behind: live, the camera's #!CFG lut_id when the
                                  record arrived; frame, the PC's table. 0 = the
                                  camera had none, -1 = not known (files from
                                  before the column). Only meaningful at stage >= 1:
                                  a stage 2 classifier is valid behind that table
                                  alone (train_clf.py checks it)
  feat_version, csv_version, crop_size   scalars
  feat_names  (F,) str
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import detector as D
from . import labels as L
from . import protocol as P

BALLOON, NOT_BALLOON, UNLABELLED = 1, 0, -1
UNKNOWN_LUT = -1


@dataclass
class Sample:
    feat: np.ndarray            # (F,) float32
    crop: np.ndarray            # (24, 24) uint16
    label: int
    source: str                 # "live" | "frame"
    ref: str
    t_ms: int
    k: int
    stage: int
    cx: float
    cy: float
    area: int
    box: tuple
    crop_box: tuple
    conf: float
    fw: str
    fv: int = P.FEAT_VERSION
    wb: str = "unrecorded"
    wb_gain: tuple = P.UNKNOWN_GAIN
    lut_id: int = UNKNOWN_LUT

    def rgb(self) -> np.ndarray:
        return P.rgb565_to_rgb888(self.crop)


def verdict(conf: float) -> int:
    """The stage's own opinion as a starting label: none below stage 2
    (conf -1), so live B1 records start unlabelled."""
    if conf < 0:
        return UNLABELLED
    return BALLOON if conf >= 0.5 else NOT_BALLOON


def from_record(rec: P.BlobRec, lut_id: int = UNKNOWN_LUT) -> Sample:
    """lut_id: the camera's table at the time (#!CFG lut_id); the #!BLOB
    header does not carry it, so the viewer supplies it."""
    return Sample(rec.feat.astype(np.float32), rec.crop.astype(np.uint16), verdict(rec.conf),
                  "live", "t%d" % rec.t_ms, rec.t_ms, rec.k, rec.stage, rec.cx, rec.cy,
                  rec.area, tuple(rec.box), tuple(rec.crop_box), rec.conf, rec.fw, rec.fv,
                  rec.wb, tuple(rec.wb_gain), lut_id)


def auto_label(lab: np.ndarray, cx: float, cy: float) -> int:
    if not (lab != L.UNLABELLED).any():
        return UNLABELLED
    return BALLOON if any(c.contains(cx, cy) for c in L.balloons(lab)) else NOT_BALLOON


def frame_ref(sf: L.SavedFrame) -> str:
    return "%s/%s" % (sf.stem.parent.name, sf.stem.name)


def from_frame(sf: L.SavedFrame, lab: np.ndarray | None = None, stage: int = 1,
               m: D.TcModels | None = None, st: D.Settings | None = None) -> list[Sample]:
    """Every blob the camera's pipeline reports on a saved frame, at `stage`
    (default 1: the colour table, which is what stage 2 will run behind),
    with features and crops from the shared C code and labels from `lab`
    (default: the frame's own label file)."""
    st = st or sf.settings()
    m = m if m is not None else D.models()
    if lab is None:
        lab = L.load_labels(sf.label_path, sf.frame.w, sf.frame.h)
    data = sf.frame.data
    wb, gain = P.wb_of(sf.meta.get("camera_cfg") or {})
    tr = D.pipeline(st, data, stage, m, features=True)
    out = []
    for k in range(tr.n_feat):
        b = tr.r.b[k]
        box = D.crop_box(b)
        out.append(Sample(np.array(list(tr.feat[k].v), np.float32), D.crop(st, data, box),
                          auto_label(lab, b.cx, b.cy), "frame", frame_ref(sf), sf.frame.t_ms,
                          k, tr.stage, b.cx, b.cy, b.area, (b.x0, b.y0, b.x1, b.y1), box,
                          float(tr.conf[k]), "pc", wb=wb, wb_gain=gain,
                          lut_id=m.lut_id if tr.stage >= 1 else 0))
    return out


def from_folder(folder: Path, stage: int = 1, m: D.TcModels | None = None,
                labelled_only: bool = True) -> list[Sample]:
    """from_frame() over every saved frame under folder. labelled_only skips
    frames with no label file: their blobs could only be "?"."""
    out = []
    for stem in L.list_frames(folder):
        sf = L.load_frame(stem)
        if labelled_only and not sf.label_path.exists():
            continue
        out += from_frame(sf, stage=stage, m=m)
    return out


def counts(samples: list[Sample]) -> dict:
    lab = np.array([s.label for s in samples], dtype=np.int8)
    return {"n": len(samples), "balloon": int((lab == BALLOON).sum()),
            "not": int((lab == NOT_BALLOON).sum()), "unlabelled": int((lab == UNLABELLED).sum())}


def save(path: Path, samples: list[Sample]):
    fvs = {s.fv for s in samples}
    if len(fvs) > 1:
        # Features computed by two definitions cannot share one column.
        raise ValueError("samples mix feature versions %s" % sorted(fvs))
    n = len(samples)

    def col(f, dt):
        return np.array([f(s) for s in samples], dtype=dt)

    np.savez_compressed(
        path,
        feat=np.stack([s.feat for s in samples]).astype(np.float32) if n
        else np.zeros((0, D.BLOB_FEAT_N), np.float32),
        crop=np.stack([s.crop for s in samples]).astype(np.uint16) if n
        else np.zeros((0, P.CROP, P.CROP), np.uint16),
        label=col(lambda s: s.label, np.int8),
        source=col(lambda s: s.source, str), ref=col(lambda s: s.ref, str),
        t_ms=col(lambda s: s.t_ms, np.int32), k=col(lambda s: s.k, np.int32),
        stage=col(lambda s: s.stage, np.int32), area=col(lambda s: s.area, np.int32),
        cx=col(lambda s: s.cx, np.float32), cy=col(lambda s: s.cy, np.float32),
        conf=col(lambda s: s.conf, np.float32),
        box=col(lambda s: s.box, np.int32).reshape(n, 4),
        crop_box=col(lambda s: s.crop_box, np.int32).reshape(n, 3),
        fw=col(lambda s: s.fw, str), wb=col(lambda s: s.wb, str),
        wb_gain=col(lambda s: s.wb_gain, np.uint8).reshape(n, 3),
        lut_id=col(lambda s: s.lut_id, np.int64),
        feat_version=np.int32(fvs.pop() if fvs else P.FEAT_VERSION),
        csv_version=np.int32(P.CSV_VERSION), crop_size=np.int32(P.CROP),
        feat_names=np.array(D.feat_names()),
        created=np.array(time.strftime("%Y-%m-%dT%H:%M:%S")))


def load(path: Path) -> list[Sample]:
    z = np.load(path, allow_pickle=False)
    fv = int(z["feat_version"])
    has_wb = "wb" in z.files
    has_lut = "lut_id" in z.files
    out = []
    for i in range(len(z["label"])):
        out.append(Sample(z["feat"][i], z["crop"][i], int(z["label"][i]), str(z["source"][i]),
                          str(z["ref"][i]), int(z["t_ms"][i]), int(z["k"][i]),
                          int(z["stage"][i]), float(z["cx"][i]), float(z["cy"][i]),
                          int(z["area"][i]), tuple(int(v) for v in z["box"][i]),
                          tuple(int(v) for v in z["crop_box"][i]), float(z["conf"][i]),
                          str(z["fw"][i]), fv,
                          str(z["wb"][i]) if has_wb else "unrecorded",
                          tuple(int(v) for v in z["wb_gain"][i]) if has_wb
                          else P.UNKNOWN_GAIN,
                          int(z["lut_id"][i]) if has_lut else UNKNOWN_LUT))
    return out


@dataclass
class Agreement:
    """Board vs PC on the same pixels: a '!collect' record whose frame was
    also grabbed, re-run through the PC library. The B1 board check."""
    checked: int = 0
    feat_equal: int = 0
    crop_equal: int = 0
    max_rel: float = 0.0        # worst feature difference, relative
    worst: str = ""             # which feature, for the report
    missing: int = 0            # records the PC found no blob k for
    _cache: tuple = (None, None)

    def add(self, rec: P.BlobRec, frame: P.Frame, st: D.Settings,
            m: D.TcModels | None = None) -> bool:
        """Compare one record with the PC's answer for its frame, at the
        stage the camera ran. Returns False if the PC did not report a blob k
        (counted as missing: the two disagree on the detection itself)."""
        st.be = 1 if frame.big_endian else 0
        key = (frame.seq, frame.t_ms, rec.stage)
        if self._cache[0] != key:       # up to four records share one frame
            self._cache = (key, D.pipeline(st, frame.data, rec.stage, m, features=True))
        tr = self._cache[1]
        if rec.k >= tr.n_feat or tr.stage != rec.stage:
            self.missing += 1
            return False
        pc = np.array(list(tr.feat[rec.k].v), np.float32)
        b = tr.r.b[rec.k]
        crop = D.crop(st, frame.data, D.crop_box(b))
        # The record carries %.6g text: 6 significant digits, so agreement
        # is judged to that precision, not to the last bit.
        rel = np.abs(pc - rec.feat) / np.maximum(np.abs(pc), 1e-3)
        i = int(np.argmax(rel))
        self.checked += 1
        self.feat_equal += bool(np.all(rel < 2e-5))
        self.crop_equal += bool(np.array_equal(crop, rec.crop))
        if rel[i] > self.max_rel:
            self.max_rel = float(rel[i])
            self.worst = D.feat_names()[i]
        return True

    def text(self) -> str:
        if not self.checked and not self.missing:
            return "no record has had its frame grabbed yet"
        return ("%d records checked against the PC library: features equal in %d, crops "
                "identical in %d; worst feature difference %.2g (relative%s)%s" % (
                    self.checked, self.feat_equal, self.crop_equal, self.max_rel,
                    ", " + self.worst if self.worst else "",
                    "; %d with no matching PC blob" % self.missing if self.missing else ""))

    def ok(self) -> bool:
        return (self.checked > 0 and not self.missing and self.feat_equal == self.checked
                and self.crop_equal == self.checked)
