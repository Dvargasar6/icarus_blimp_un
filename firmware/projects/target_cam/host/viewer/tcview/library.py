"""
Where the training data and the trained models live, for the Train, Verify
and Firmware tabs.

Data. A session is a folder the operator picks under target_cam/data/: every
saved frame below it, subfolders included (labels.list_frames is recursive),
is one capture session for the trainers' leave-one-session-out. That is how a
session split by balloon colour into subfolders (data/train/s3/table_red,
.../table_orange, ...) stays one session: pick data/train/s3. Holding out one
colour of one session is not a held-out session (learned-detector report
§11.2), so the choice of level is the operator's and the tree shows it.
A blob set (.npz, tcview/blobset.py) is a session too, for the classifier only.

Models. A model set is a folder under target_cam/models/ holding the models of
one detector, built up stage by stage:

    lut_model.{bin,h,json}   stage 1, the colour table
    clf_model.{bin,h,json}   stage 2, the blob classifier, trained behind it
    cnn_model.{bin,h,json}   stage 3, the crop CNN (phase P7)
    *_log.txt                what the trainers printed

A classifier is only valid behind the table it was trained with (blob_clf.h),
so it lives in that table's folder: training stage 2 on table X makes a new
set holding a copy of X plus the classifier, and X stays as it was. A set is
installed whole (the Firmware tab), so a classifier never travels without its
table. main/models/ is the set the firmware compiles in; it is listed
read-only here, and only the Firmware tab writes it.

Which stages a set offers comes from its .json files (train ids); the C
loaders are the authority when it is evaluated (detector.models refuses a
classifier of another table, with the reason).
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

TARGET_CAM = Path(__file__).resolve().parents[3]
DATA_DIR = TARGET_CAM / "data"
MODELS_DIR = TARGET_CAM / "models"
FIRMWARE_MODELS = TARGET_CAM / "main" / "models"
TRAIN_LUT = TARGET_CAM / "host" / "train" / "train_lut.py"
TRAIN_CLF = TARGET_CAM / "host" / "train" / "train_clf.py"

KINDS = ("lut", "clf", "cnn")           # stage 1, 2, 3
LABEL_SUFFIX = ".labels.png"
# A set name becomes one folder name: no subfolders, no hidden folders, nothing
# that needs quoting in the command shown for copying.
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")


def rel(p: Path) -> str:
    """p relative to target_cam/ when it is inside it, else absolute. What the
    trainers record as session paths, so a model set means the same folders
    on every checkout."""
    p = Path(p).resolve()
    try:
        return str(p.relative_to(TARGET_CAM))
    except ValueError:
        return str(p)


def resolve(p: str) -> Path:
    q = Path(p)
    return (q if q.is_absolute() else TARGET_CAM / q).resolve()


# ---- data ------------------------------------------------------------------------

@dataclass
class Session:
    path: Path
    n_frames: int           # blob set: blobs
    n_labelled: int         # blob set: labelled blobs
    is_blobset: bool = False

    @property
    def name(self) -> str:
        return self.path.stem if self.is_blobset else self.path.name

    @property
    def key(self) -> str:
        """Unique where names are not (test/red_s1 and train/x/red_s1)."""
        return rel(self.path)


def count_frames(folder: Path) -> tuple[int, int]:
    """(saved frames, labelled frames) below folder, subfolders included."""
    n = lab = 0
    for p in Path(folder).rglob("*.rgb565"):
        if p.with_suffix(".json").exists():
            n += 1
            lab += Path(str(p.with_suffix("")) + LABEL_SUFFIX).exists()
    return n, lab


def session(path: Path) -> Session:
    path = Path(path)
    if path.suffix == ".npz":
        import numpy as np
        z = np.load(path, allow_pickle=False)
        lab = z["label"]
        return Session(path, len(lab), int((lab >= 0).sum()), True)
    n, lab = count_frames(path)
    return Session(path, n, lab)


@dataclass
class DataNode:
    """A folder of the data tree: its own frames and its children's. A node
    with frames anywhere below it can be picked as a session."""
    path: Path
    n_frames: int = 0       # below it, subfolders included
    n_labelled: int = 0
    own_frames: int = 0     # directly in it
    children: list = field(default_factory=list)
    blobsets: list = field(default_factory=list)        # .npz paths directly in it


def data_tree(root: Path = DATA_DIR) -> DataNode | None:
    """The folders under root that hold saved frames or blob sets, as a tree;
    empty folders are left out. None if root does not exist."""
    root = Path(root)
    if not root.is_dir():
        return None

    def walk(d: Path) -> DataNode:
        node = DataNode(d)
        for c in sorted(d.iterdir()):
            if c.is_dir():
                sub = walk(c)
                if sub.n_frames or sub.blobsets or sub.children:
                    node.children.append(sub)
                    node.n_frames += sub.n_frames
                    node.n_labelled += sub.n_labelled
            elif c.suffix == ".rgb565" and c.with_suffix(".json").exists():
                node.own_frames += 1
                node.n_frames += 1
                node.n_labelled += Path(str(c.with_suffix("")) + LABEL_SUFFIX).exists()
            elif c.suffix == ".npz":
                node.blobsets.append(c)
        return node

    return walk(root)


def overlapping(paths: list[Path]) -> list[tuple[Path, Path]]:
    """Pairs where one chosen folder is inside another: their frames would
    count in two sessions at once."""
    ps = [Path(p).resolve() for p in paths]
    return [(a, b) for a in ps for b in ps if a != b and b.is_relative_to(a)]


# ---- model sets ------------------------------------------------------------------

def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


@dataclass
class ModelSet:
    path: Path
    firmware: bool = False
    meta: dict = field(default_factory=dict)     # kind -> its .json

    @property
    def name(self) -> str:
        return "firmware (main/models)" if self.firmware else self.path.name

    def bin(self, kind: str) -> Path:
        return self.path / ("%s_model.bin" % kind)

    def has(self, kind: str) -> bool:
        return self.bin(kind).exists()

    def train_id(self, kind: str) -> int | None:
        return self.meta.get(kind, {}).get("train_id")

    @property
    def lut_id(self) -> int | None:
        return self.train_id("lut")

    def belongs(self, kind: str) -> bool:
        """A classifier or CNN trained behind this set's table."""
        return self.has(kind) and self.lut_id is not None and \
            self.meta.get(kind, {}).get("lut_id") == self.lut_id

    @property
    def stages(self) -> list[int]:
        """Stages this set can run, by prefix as on the camera: 1 needs the
        table, 2 the table and its classifier, 3 all three."""
        out = []
        if self.has("lut"):
            out.append(1)
            if self.belongs("clf"):
                out.append(2)
                if self.belongs("cnn"):
                    out.append(3)
        return out

    @property
    def top_stage(self) -> int:
        return max(self.stages, default=0)

    def sessions(self, kind: str) -> list[str]:
        return list(self.meta.get(kind, {}).get("sessions") or [])

    def session_paths(self, kind: str) -> list[Path]:
        """Folders the model of that kind was trained on, resolved (older
        sets record names only, and give [])."""
        return [resolve(p) for p in self.meta.get(kind, {}).get("session_paths") or []]

    def summary(self, kind: str) -> str:
        m = self.meta.get(kind)
        if not self.has(kind):
            return "none"
        if not m:
            return "no %s_model.json" % kind
        if kind == "lut":
            return "id %s · sigma %s · k %s · %s colours · %s" % (
                m.get("train_id", "?"), m.get("sigma", "?"), m.get("k", "?"),
                m.get("colours", "?"), " + ".join(self.sessions(kind)) or "?")
        bad = "" if self.belongs(kind) else "  (trained behind table %s: NOT USED)" \
            % m.get("lut_id", "?")
        return "id %s · threshold %s · %s%s" % (
            m.get("train_id", "?"), m.get("threshold", "?"),
            " + ".join(self.sessions(kind)) or "?", bad)

    def ids(self) -> dict:
        """kind -> train id, for the models present (Firmware tab, #!CFG)."""
        return {k: self.train_id(k) for k in KINDS if self.has(k)}


def load_set(folder: Path, firmware: bool = False) -> ModelSet | None:
    folder = Path(folder)
    if not (folder / "lut_model.bin").exists():
        return None
    return ModelSet(folder, firmware, {k: _json(folder / ("%s_model.json" % k)) for k in KINDS})


def find_sets(models_dir: Path = MODELS_DIR, firmware_dir: Path = FIRMWARE_MODELS) -> list[ModelSet]:
    """The firmware's set first, then models/ newest first."""
    out = []
    fw = load_set(firmware_dir, firmware=True)
    if fw:
        out.append(fw)
    if Path(models_dir).is_dir():
        own = [s for s in (load_set(d) for d in Path(models_dir).iterdir() if d.is_dir()) if s]
        own.sort(key=lambda s: (-max((s.train_id(k) or 0) for k in KINDS), s.name))
        out.extend(own)
    return out


def check_name(name: str, models_dir: Path = MODELS_DIR) -> str:
    """Why `name` cannot be a new set under models_dir, or "" if it can."""
    if not name:
        return "Type a name for the model set."
    if not NAME_RE.match(name):
        return ("Letters, digits, '_', '-' and '.' only, no spaces, not starting with "
                "'.' or '-'.")
    if (Path(models_dir) / name).exists():
        return "models/%s already exists: choose another name." % name
    return ""


def suggest_name(stage: int, sessions: list[Session], parent: ModelSet | None = None,
                 models_dir: Path = MODELS_DIR, now: float | None = None) -> str:
    """lut_/clf_/cnn_<yyyymmdd>_<sessions>, made unique with a counter. The
    .json records what went in either way; the name is only a reminder."""
    kind = KINDS[stage - 1]
    day = time.strftime("%Y%m%d", time.localtime(now))
    tag = "-".join(s.name for s in sessions)[:40] or "x"
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
    base = "%s_%s_%s" % (kind, day, tag)
    name, n = base, 2
    while check_name(name, models_dir):
        name, n = "%s_%d" % (base, n), n + 1
    return name


def new_set(name: str, parent: ModelSet | None, upto: str | None,
            models_dir: Path = MODELS_DIR) -> Path:
    """Create models/<name>/, with the parent's models up to and including
    `upto` copied in ("lut" for a classifier, "clf" for a CNN). The trainer
    then writes its own model beside them."""
    err = check_name(name, models_dir)
    if err:
        raise ValueError(err)
    out = Path(models_dir) / name
    out.mkdir(parents=True)
    if parent is not None and upto is not None:
        for kind in KINDS[:KINDS.index(upto) + 1]:
            for f in parent.path.glob("%s_model.*" % kind):
                shutil.copy2(f, out / f.name)
    return out
