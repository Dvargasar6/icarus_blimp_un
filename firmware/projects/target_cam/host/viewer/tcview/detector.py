"""
The camera's own detector, called from Python.

host/libblob_detect.so is the camera's portable sources (../main/blob_detect.c,
tc_pipeline.c, color_lut.c, blob_feat.c and blob_clf.c, the list is SRC in host/Makefile) compiled for
this PC: the same files the firmware runs. So the viewer's mask overlay, its per-pixel
H/S/V readout and its region statistics are the camera's arithmetic, not a
Python re-implementation that could drift from it (the hue of a pixel, for
example, comes from integer 5/6/5-bit maths that a float HSV would not
reproduce exactly).

The library is rebuilt with `make lib` whenever the C source is newer.
"""
from __future__ import annotations

import ctypes as C
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HOST_DIR = Path(__file__).resolve().parents[2]          # target_cam/host
MAIN_DIR = HOST_DIR.parent / "main"
LIB_PATH = HOST_DIR / "libblob_detect.so"
SOURCES = [MAIN_DIR / n for n in ("blob_detect.c", "blob_detect.h",
                                   "tc_pipeline.c", "tc_pipeline.h",
                                   "color_lut.c", "color_lut.h",
                                   "blob_feat.c", "blob_feat.h",
                                   "blob_clf.c", "blob_clf.h")]
# The colour table the firmware compiles in (main/models/lut_model.h holds the
# same bytes; a host test checks they agree).
LUT_MODEL = MAIN_DIR / "models" / "lut_model.bin"
# The blob classifier (stage 2), when one is installed. It belongs to one
# colour table (blob_clf.h), so it is offered only behind that table.
CLF_MODEL = MAIN_DIR / "models" / "clf_model.bin"
COLOR_LUT_BYTES = 8192
MODEL_ERRORS = ["ok", "short", "magic", "version", "size", "crc", "value"]

BLOB_MAX_REPORT = 4
TC_STAGE_MAX = 3
PRESETS = ["red", "blue", "green", "orange"]
CAL_PATCH = 16          # target_main.c: the centre patch 'c' samples
TC_RUN_FEATURES = 0x1   # tc_pipeline.h
BLOB_FEAT_N = 12        # blob_feat.h; checked against the library at load
BLOB_CROP = 24
BLOB_CROP_SMALL = 16


class BlobCfg(C.Structure):
    _fields_ = [(n, C.c_int) for n in
                ("width", "height", "cell", "big_endian", "hue", "hue_tol",
                 "sat_min", "val_min", "min_area")] + [("lut", C.c_void_p)]


class Blob(C.Structure):
    _fields_ = [("cx", C.c_float), ("cy", C.c_float), ("area", C.c_int),
                ("x0", C.c_int), ("y0", C.c_int), ("x1", C.c_int), ("y1", C.c_int),
                ("clipped", C.c_int)]


class BlobResult(C.Structure):
    _fields_ = [("found", C.c_int), ("n_report", C.c_int), ("n_blobs", C.c_int),
                ("matched", C.c_int), ("b", Blob * BLOB_MAX_REPORT)]


class TcModels(C.Structure):
    """tc_models_t: validated model pointers, NULL = absent. The buffers
    they point into are kept on the instance (_keep), so they live exactly as
    long as the models do. lut_id is the table's train id, 0 when absent;
    clf_id and clf_path the same for the classifier."""
    _fields_ = [("lut", C.c_void_p), ("clf", C.c_void_p), ("cnn", C.c_void_p)]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._keep = []
        self.lut_id = 0
        self.lut_path = None
        self.clf_id = 0
        self.clf_path = None
        self.clf_model = None


class BlobClf(C.Structure):
    """blob_clf_t (blob_clf.h). Its size is checked against the library's
    at load, so a field added on one side cannot shift the other's."""
    _fields_ = [("lut_id", C.c_uint32), ("threshold", C.c_float), ("bias", C.c_float),
                ("mean", C.c_float * 12), ("scale", C.c_float * 12), ("w", C.c_float * 12)]


class BlobFeat(C.Structure):
    _fields_ = [("v", C.c_float * BLOB_FEAT_N)]


class CropBox(C.Structure):
    _fields_ = [("xs", C.c_int), ("ys", C.c_int), ("side", C.c_int)]


class TcResult(C.Structure):
    _fields_ = [("r", BlobResult), ("stage", C.c_int),
                ("conf", C.c_float * BLOB_MAX_REPORT),
                ("t_px_us", C.c_int32), ("t_b_us", C.c_int32), ("t_c_us", C.c_int32),
                ("t_total_us", C.c_int32), ("n_feat", C.c_int),
                ("feat", BlobFeat * BLOB_MAX_REPORT), ("n_rejected", C.c_int)]

    def features(self) -> np.ndarray:
        """(n_feat, BLOB_FEAT_N) float32: the features of the reported blobs,
        empty when the pipeline was not asked for them."""
        return np.array([list(self.feat[k].v) for k in range(self.n_feat)],
                        dtype=np.float32).reshape(self.n_feat, BLOB_FEAT_N)


# blob_clock_fn: int64_t (*)(void). A Python clock costs a microsecond or two
# per call, so times measured through it are the PC's, not the camera's.
CLOCK_FN = C.CFUNCTYPE(C.c_int64)
_PY_CLOCK = CLOCK_FN(lambda: time.perf_counter_ns() // 1000)


class BlobColor(C.Structure):
    _fields_ = [("hue", C.c_int), ("hue_spread", C.c_int), ("sat", C.c_int),
                ("val", C.c_int)]


def _stale() -> bool:
    if not LIB_PATH.exists():
        return True
    built = LIB_PATH.stat().st_mtime
    return any(s.stat().st_mtime > built for s in SOURCES)


def load_library() -> C.CDLL:
    if _stale():
        r = subprocess.run(["make", "-C", str(HOST_DIR), "lib"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("building libblob_detect.so failed:\n" + r.stdout + r.stderr)
    lib = C.CDLL(str(LIB_PATH))
    P = C.POINTER
    lib.blob_cfg_valid.argtypes = [P(BlobCfg)]
    lib.blob_workspace_bytes.argtypes = [P(BlobCfg)]
    lib.blob_workspace_bytes.restype = C.c_size_t
    lib.blob_rgb565_hsv.argtypes = [C.c_uint16, P(C.c_int), P(C.c_int), P(C.c_int)]
    lib.blob_rgb565_hsv.restype = None
    lib.blob_pixel_matches.argtypes = [P(BlobCfg), C.c_uint16]
    lib.blob_mask.argtypes = [P(BlobCfg), C.c_char_p, C.c_void_p]
    lib.blob_detect.argtypes = [P(BlobCfg), C.c_char_p, C.c_void_p, P(BlobResult)]
    lib.blob_sample.argtypes = [P(BlobCfg), C.c_char_p, C.c_int, C.c_int, C.c_int,
                                C.c_int, P(BlobColor)]
    lib.blob_sample.restype = None
    lib.blob_preset_apply.argtypes = [P(BlobCfg), C.c_int]
    lib.tc_stage_available.argtypes = [P(TcModels)]
    lib.tc_stage_effective.argtypes = [C.c_int, P(TcModels)]
    lib.tc_pipeline_run.argtypes = [P(BlobCfg), C.c_int, C.c_int, P(TcModels),
                                    C.c_char_p, C.c_void_p, CLOCK_FN, P(TcResult)]
    lib.blob_features.argtypes = [P(BlobCfg), C.c_char_p, P(Blob), P(BlobFeat)]
    lib.blob_features.restype = None
    lib.blob_feat_step.argtypes = [C.c_int, C.c_int]
    lib.blob_crop_box.argtypes = [P(Blob), P(CropBox)]
    lib.blob_crop_box.restype = None
    lib.blob_crop.argtypes = [P(BlobCfg), C.c_char_p, P(CropBox), C.c_void_p]
    lib.blob_crop.restype = None
    lib.blob_crop_down16.argtypes = [C.c_void_p, C.c_void_p]
    lib.blob_crop_down16.restype = None
    lib.tc_pipeline_mask.argtypes = [P(BlobCfg), C.c_int, P(TcModels), C.c_char_p,
                                     C.c_void_p]
    lib.tc_crc32.argtypes = [C.c_char_p, C.c_size_t]
    lib.tc_crc32.restype = C.c_uint32
    lib.color_lut_load.argtypes = [C.c_char_p, C.c_size_t, C.c_void_p,
                                   P(C.c_uint32)]
    lib.color_lut_from_hsv.argtypes = [P(BlobCfg), C.c_void_p]
    lib.color_lut_from_hsv.restype = None
    lib.color_lut_count.argtypes = [C.c_void_p]
    lib.blob_clf_load.argtypes = [C.c_char_p, C.c_size_t, P(BlobClf), P(C.c_uint32)]
    lib.blob_clf_conf.argtypes = [P(BlobClf), P(BlobFeat)]
    lib.blob_clf_conf.restype = C.c_float
    lib.blob_clf_struct_bytes.restype = C.c_size_t
    if lib.blob_clf_struct_bytes() != C.sizeof(BlobClf):
        raise RuntimeError("blob_clf_t is %d bytes in the library, %d here"
                           % (lib.blob_clf_struct_bytes(), C.sizeof(BlobClf)))
    return lib


_LIB = None


def lib() -> C.CDLL:
    global _LIB
    if _LIB is None:
        _LIB = load_library()
    return _LIB


@dataclass
class Settings:
    """The detector half of the camera's #!CFG."""
    hue: int = 0
    tol: int = 12
    sat: int = 110
    val: int = 50
    area: int = 25
    be: int = 1
    w: int = 160
    h: int = 120
    cell: int = 2

    def c(self) -> BlobCfg:
        return BlobCfg(self.w, self.h, self.cell, self.be, self.hue, self.tol,
                       self.sat, self.val, self.area, None)

    @classmethod
    def from_cfg(cls, v: dict) -> "Settings":
        s = cls()
        for k in ("hue", "tol", "sat", "val", "area", "be", "w", "h", "cell"):
            if isinstance(v.get(k), int):
                setattr(s, k, v[k])
        return s


def preset(name_or_index) -> tuple:
    """(hue, tol, sat, val) of a firmware preset, read from the C table."""
    i = PRESETS.index(name_or_index) if isinstance(name_or_index, str) else name_or_index
    c = Settings().c()
    if not lib().blob_preset_apply(C.byref(c), i):
        raise ValueError(name_or_index)
    return c.hue, c.hue_tol, c.sat_min, c.val_min


_LUT = None


def hsv_lut() -> np.ndarray:
    """(65536, 3) int16: H, S, V of every RGB565 value, from blob_rgb565_hsv().
    Built once (~0.1 s); after that any pixel's colour is an array lookup."""
    global _LUT
    if _LUT is None:
        f = lib().blob_rgb565_hsv
        h, s, v = C.c_int(), C.c_int(), C.c_int()
        out = np.empty((65536, 3), dtype=np.int16)
        for px in range(65536):
            f(px, C.byref(h), C.byref(s), C.byref(v))
            out[px] = (h.value, s.value, v.value)
        _LUT = out
    return _LUT


def hsv(px: int) -> tuple:
    h, s, v = C.c_int(), C.c_int(), C.c_int()
    lib().blob_rgb565_hsv(px, C.byref(h), C.byref(s), C.byref(v))
    return h.value, s.value, v.value


def mask(st: Settings, frame: bytes, stage: int = 0,
         m: TcModels | None = None) -> np.ndarray:
    """(h, w) uint8, 1 where the pixel passes the colour test of the stage
    that runs for this request (tc_pipeline_mask): the HSV window at stage 0,
    the colour table from stage 1 up, with the camera's fallback."""
    out = np.zeros((st.h, st.w), dtype=np.uint8)
    c = st.c()
    m = m if m is not None else models()
    lib().tc_pipeline_mask(C.byref(c), int(stage), C.byref(m), frame,
                           out.ctypes.data_as(C.c_void_p))
    return out


def detect(st: Settings, frame: bytes) -> BlobResult:
    c = st.c()
    if not lib().blob_cfg_valid(C.byref(c)):
        raise ValueError("invalid detector configuration")
    work = (C.c_uint32 * ((lib().blob_workspace_bytes(C.byref(c)) + 3) // 4))()
    r = BlobResult()
    lib().blob_detect(C.byref(c), frame, C.cast(work, C.c_void_p), C.byref(r))
    return r


NO_MODELS = TcModels()


class ModelError(ValueError):
    pass


def lut_models(record: bytes | None = None, bits: np.ndarray | bytes | None = None,
               train_id: int = 0, path=None) -> TcModels:
    """Models with a colour table, from a model record (checked by the C
    color_lut_load, exactly as the camera checks it at boot) or from bare
    table bits (training candidates, never sent anywhere)."""
    buf = (C.c_uint8 * COLOR_LUT_BYTES)()
    if record is not None:
        tid = C.c_uint32()
        e = lib().color_lut_load(record, len(record), buf, C.byref(tid))
        if e != 0:
            raise ModelError("colour table refused: %s" % MODEL_ERRORS[e])
        train_id = tid.value
    else:
        b = bytes(np.asarray(bits, np.uint8).tobytes()) if not isinstance(bits, bytes) else bits
        if len(b) != COLOR_LUT_BYTES:
            raise ModelError("a colour table is %d bytes, not %d" % (COLOR_LUT_BYTES, len(b)))
        C.memmove(buf, b, COLOR_LUT_BYTES)
    m = TcModels()
    m.lut = C.cast(buf, C.c_void_p)
    m._keep.append(buf)
    m.lut_id = train_id
    m.lut_path = path
    return m


def load_clf(record: bytes) -> tuple[BlobClf, int]:
    """(model, train id) from a classifier record, checked by the C
    blob_clf_load exactly as the camera checks it at boot."""
    out = BlobClf()
    tid = C.c_uint32()
    e = lib().blob_clf_load(record, len(record), C.byref(out), C.byref(tid))
    if e != 0:
        raise ModelError("blob classifier refused: %s" % MODEL_ERRORS[e])
    return out, tid.value


def add_clf(m: TcModels, record: bytes, path=None, strict: bool = True) -> TcModels:
    """Offer a classifier behind m's colour table. One trained behind
    another table is refused (ModelError) when strict, and otherwise left
    out, which is what the camera does with it (clf=mismatch)."""
    clf, tid = load_clf(record)
    if not m.lut or clf.lut_id != m.lut_id:
        if strict:
            raise ModelError("blob classifier %d was trained behind colour table %d, not %d"
                             % (tid, clf.lut_id, m.lut_id))
        return m
    m.clf = C.cast(C.pointer(clf), C.c_void_p)
    m._keep.append(clf)
    m.clf_model = clf
    m.clf_id = tid
    m.clf_path = path
    return m


def with_threshold(m: TcModels, threshold: float | None) -> TcModels:
    """m with its classifier run at `threshold` (the camera's !thr2), as
    target_main.c apply_thr2() does; m itself when there is no classifier or
    no threshold to apply. The table is shared, the classifier copied."""
    if not m.clf or threshold is None or threshold <= 0 or \
            abs(threshold - m.clf_model.threshold) < 1e-6:
        return m
    out = TcModels()
    out.lut, out.lut_id, out.lut_path = m.lut, m.lut_id, m.lut_path
    out._keep = list(m._keep)
    clf = BlobClf.from_buffer_copy(m.clf_model)
    clf.threshold = float(threshold)
    out.clf = C.cast(C.pointer(clf), C.c_void_p)
    out._keep.append(clf)
    out.clf_model, out.clf_id, out.clf_path = clf, m.clf_id, m.clf_path
    return out


_MODELS = None


def models(lut_path=None, clf_path=None) -> TcModels:
    """The models the firmware compiles in, or the table at lut_path (and the
    classifier at clf_path, which must belong to that table). With no table
    file, no models: every request then falls back to stage 0, as the camera
    does. The firmware's classifier is offered only when it belongs to the
    firmware's table, as on the camera."""
    global _MODELS
    if lut_path is not None:
        p = Path(lut_path)
        m = lut_models(record=p.read_bytes(), path=p)
        if clf_path is not None:
            add_clf(m, Path(clf_path).read_bytes(), Path(clf_path))
        return m
    if clf_path is not None:
        raise ValueError("a classifier needs the colour table it belongs to (lut_path)")
    if _MODELS is None:
        m = lut_models(record=LUT_MODEL.read_bytes(), path=LUT_MODEL) \
            if LUT_MODEL.exists() else TcModels()
        if m.lut and CLF_MODEL.exists():
            try:
                add_clf(m, CLF_MODEL.read_bytes(), CLF_MODEL, strict=False)
            except ModelError:
                pass                        # refused: the camera runs stage 1 too
        _MODELS = m
    return _MODELS


def clf_conf(clf: BlobClf, feat) -> float:
    """blob_clf_conf() for one feature vector."""
    f = BlobFeat()
    for i, v in enumerate(np.asarray(feat, np.float32)):
        f.v[i] = float(v)
    return float(lib().blob_clf_conf(C.byref(clf), C.byref(f)))


def hsv_table(st: Settings) -> np.ndarray:
    """(8192,) uint8: the colour table that reproduces st's HSV test."""
    out = np.zeros(COLOR_LUT_BYTES, np.uint8)
    c = st.c()
    lib().color_lut_from_hsv(C.byref(c), out.ctypes.data_as(C.c_void_p))
    return out


def pipeline(st: Settings, frame: bytes, stage: int = 0, m: TcModels | None = None,
             timed: bool = False, features: bool = False) -> TcResult:
    """tc_pipeline_run(): the camera's whole detector at the requested stage,
    with the same fallback. timed=True passes a PC clock, for the fake camera.
    features=True is TC_RUN_FEATURES: blob features below stage 2 too."""
    c = st.c()
    if not lib().blob_cfg_valid(C.byref(c)):
        raise ValueError("invalid detector configuration")
    work = (C.c_uint32 * ((lib().blob_workspace_bytes(C.byref(c)) + 3) // 4))()
    m = m if m is not None else models()
    out = TcResult()
    clock = _PY_CLOCK if timed else CLOCK_FN()      # CLOCK_FN() is NULL
    lib().tc_pipeline_run(C.byref(c), int(stage), TC_RUN_FEATURES if features else 0,
                          C.byref(m), frame, C.cast(work, C.c_void_p), clock, C.byref(out))
    return out


def feat_names() -> list:
    """blob_feat_names, from the library itself, so a dataset's column names
    are the ones the code that computed it uses."""
    names = (C.c_char_p * BLOB_FEAT_N).in_dll(lib(), "blob_feat_names")
    return [n.decode() for n in names]


def blob_features(st: Settings, frame: bytes, b: Blob, stage: int = 0,
                  m: TcModels | None = None) -> np.ndarray:
    """blob_features() for one blob, with the pixel test of `stage` (as the
    pipeline would run it). (BLOB_FEAT_N,) float32."""
    m = m if m is not None else models()
    c = st.c()
    if lib().tc_stage_effective(int(stage), C.byref(m)) >= 1:
        c.lut = m.lut
    out = BlobFeat()
    lib().blob_features(C.byref(c), frame, C.byref(b), C.byref(out))
    return np.array(list(out.v), dtype=np.float32)


def crop_box(b: Blob) -> tuple:
    """blob_crop_box(): the source square (xs, ys, side)."""
    out = CropBox()
    lib().blob_crop_box(C.byref(b), C.byref(out))
    return out.xs, out.ys, out.side


def crop(st: Settings, frame: bytes, box: tuple) -> np.ndarray:
    """blob_crop(): (24, 24) uint16 RGB565 values."""
    out = np.zeros((BLOB_CROP, BLOB_CROP), np.uint16)
    c = st.c()
    lib().blob_crop(C.byref(c), frame, C.byref(CropBox(*box)),
                    out.ctypes.data_as(C.c_void_p))
    return out


def crop_down16(crop24: np.ndarray) -> np.ndarray:
    """blob_crop_down16(): (24, 24) -> (16, 16) uint16."""
    src = np.ascontiguousarray(crop24, dtype=np.uint16)
    out = np.zeros((BLOB_CROP_SMALL, BLOB_CROP_SMALL), np.uint16)
    lib().blob_crop_down16(src.ctypes.data_as(C.c_void_p), out.ctypes.data_as(C.c_void_p))
    return out


def crc32(data: bytes) -> int:
    """tc_crc32(), the check model records carry."""
    return lib().tc_crc32(data, len(data))


def sample(st: Settings, frame: bytes, x0: int, y0: int, w: int, h: int) -> BlobColor:
    """blob_sample() on a rectangle, clipped to the frame (the C does not check)."""
    x1, y1 = min(st.w, x0 + w), min(st.h, y0 + h)
    x0, y0 = max(0, x0), max(0, y0)
    out = BlobColor()
    if x1 > x0 and y1 > y0:
        c = st.c()
        lib().blob_sample(C.byref(c), frame, x0, y0, x1 - x0, y1 - y0, C.byref(out))
    return out


@dataclass
class Calibration:
    hue: int
    tol: int
    sat: int
    val: int
    refused: bool       # the firmware would refuse: too grey to separate from walls
    warning: str


def calibrate_from(col: BlobColor) -> Calibration:
    """Thresholds from a sampled colour - the same rule as calibrate() in
    target_main.c, so 'Use as target' on the PC and 'c' on the camera agree."""
    tol = min(40, max(10, 3 * col.hue_spread))
    sat = max(40, col.sat // 2)
    val = max(25, col.val * 2 // 5)
    warn = ""
    refused = col.sat < 70
    if refused:
        warn = ("saturation %d is too low to separate from walls; the camera's 'c' "
                "would refuse this" % col.sat)
    elif col.hue_spread > 20:
        warn = "hue spread %d deg: the region is not one colour" % col.hue_spread
    return Calibration(col.hue, tol, sat, val, refused, warn)
