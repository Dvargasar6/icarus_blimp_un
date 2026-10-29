"""
Where sessions and tables live, and how a table is named.

A session is a folder that directly holds saved frames (<stem>.rgb565 +
.json, labelled with .labels.png). They are looked for under data/, three
levels deep, so data/train/red_s5, data/test/red_s6 and
data/train/Without_AWB/red_s1 are all found and keep their group (the parent
folder: train, test, train/Without_AWB) for display. Two sessions may share a
name in different groups, so a session is identified by its key, the path
under data/ (test/red_s1 is not train/Without_AWB/red_s1).

For the classifier a session can also be a blob set: an .npz under data/
(tcview's Blobs tab, blobs_from_frames.py), one capture session per file. It
holds blobs, not frames, so it trains a classifier but cannot train a table
or be verified; find_sessions lists sets only when asked (blobsets=True), and
its counts are of blobs. Its BlobSetInfo says what train_clf.py will check:
the stage the blobs were found at, the table behind them, the gains.

A table is a folder holding lut_model.bin (the record the camera checks),
with the trainer's lut_model.h and lut_model.json beside it, and, once one is
trained for it, the blob classifier clf_model.{bin,h,json} (stage 2): a
classifier belongs to the table it was trained behind, so it lives in the
table's folder. The folders under tables/ are the tables trained here;
main/models/ is the firmware's table, listed read-only. Only the Firmware
page writes main/models, and only when asked (install.py).

The suggested table name follows the user's pattern tabla_n_r_m_o_j_p_l:
table number n, trained on red session(s) m, orange j, pink l. Several
sessions of one colour are joined with "-" (r_5-7), none is written 0 (p_0).
A folder that is not <colour>_s<N> is appended as x_<folder>, so the name
still says what went in. The user may type any other name instead;
check_table_name says whether it can be a folder under tables/. What the
table was trained on is in its lut_model.json either way, so a free name
loses nothing.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import TARGET_CAM

DATA_DIR = TARGET_CAM / "data"
TABLES_DIR = TARGET_CAM / "tables"
FIRMWARE_MODELS = TARGET_CAM / "main" / "models"
TRAIN_SCRIPT = TARGET_CAM / "host" / "train" / "train_lut.py"
TRAIN_CLF_SCRIPT = TARGET_CAM / "host" / "train" / "train_clf.py"

COLOURS = ("red", "orange", "pink")
COLOUR_LETTER = {"red": "r", "orange": "o", "pink": "p"}
SESSION_RE = re.compile(r"^(red|orange|pink)_s(\d+)$")
TABLE_RE = re.compile(r"^tabla_(\d+)(?:_|$)")
# A table name becomes one folder name: letters, digits, "_", "-", "." only,
# so it never makes a subfolder, a hidden folder or a shell-quoting problem
# in the command shown for copying.
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")
LABEL_SUFFIX = ".labels.png"


# ---- sessions ------------------------------------------------------------------

@dataclass
class BlobSetInfo:
    stages: dict            # stage -> blobs
    lut_ids: dict           # table id -> blobs at stage >= 1 (-1: not recorded)
    gains: list             # distinct "fixed r,g,b" / "auto" / ... texts
    balloon: int = 0
    not_balloon: int = 0

    def text(self) -> str:
        st = ", ".join("etapa %d: %d" % kv for kv in sorted(self.stages.items()))
        ids = ", ".join("sin registrar" if i < 0 else str(i) for i in sorted(self.lut_ids))
        return "%d globo, %d no · %s · tabla %s · blanco %s" % (
            self.balloon, self.not_balloon, st, ids or "—", ", ".join(self.gains) or "?")


@dataclass
class Session:
    path: Path
    group: str              # "train", "test", or the folder's parent relative to data/
    n_frames: int           # blob set: blobs
    n_labelled: int         # blob set: labelled blobs
    rel: str = ""           # the path under data/; "" outside it
    blobs: BlobSetInfo | None = None     # set for a blob set (.npz)

    @property
    def is_blobset(self) -> bool:
        return self.blobs is not None

    @property
    def name(self) -> str:
        return self.path.stem if self.is_blobset else self.path.name

    @property
    def key(self) -> str:
        """What identifies the session: unique where names are not."""
        return self.rel or self.name

    @property
    def colour(self) -> str | None:
        m = SESSION_RE.match(self.name)
        return m.group(1) if m else None

    @property
    def number(self) -> int | None:
        m = SESSION_RE.match(self.name)
        return int(m.group(2)) if m else None

    def sort_key(self):
        return (COLOURS.index(self.colour) if self.colour else len(COLOURS),
                self.number or 0, self.name)


def _is_session_dir(d: Path) -> bool:
    return d.is_dir() and any(d.glob("*.rgb565"))


def blobset_info(path: Path) -> BlobSetInfo | None:
    """What a blob set holds, or None if it is not one (or is unreadable)."""
    import numpy as np
    try:
        z = np.load(path, allow_pickle=False)
        lab, stage = z["label"], z["stage"]
        lut = z["lut_id"] if "lut_id" in z.files else np.full(len(lab), -1)
        if "wb" in z.files:
            gains = sorted({("fixed %d,%d,%d" % tuple(g)) if w == "fixed" else str(w)
                            for w, g in zip(z["wb"].tolist(), z["wb_gain"].tolist())})
        else:
            gains = ["sin registrar"]
    except (OSError, KeyError, ValueError):
        return None
    return BlobSetInfo(dict(Counter(int(v) for v in stage)),
                       dict(Counter(int(i) for i, st in zip(lut, stage) if st >= 1)),
                       gains, int((lab == 1).sum()), int((lab == 0).sum()))


def find_sessions(root: Path = DATA_DIR, depth: int = 3,
                  blobsets: bool = False) -> list[Session]:
    """Every session folder under root, grouped by its parent; with
    blobsets, the blob sets (.npz) too."""
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.rglob("*")):
        if len(d.relative_to(root).parts) > depth:
            continue
        if blobsets and d.is_file() and d.suffix == ".npz":
            info = blobset_info(d)
            if info is not None:
                rel = d.relative_to(root).parent
                out.append(Session(d, str(rel) if str(rel) != "." else "otras",
                                   sum(info.stages.values()), info.balloon + info.not_balloon,
                                   str(d.relative_to(root)), info))
            continue
        if not d.is_dir() or not _is_session_dir(d):
            continue
        stems = [p.with_suffix("") for p in d.glob("*.rgb565") if p.with_suffix(".json").exists()]
        labelled = sum(1 for s in stems if Path(str(s) + LABEL_SUFFIX).exists())
        rel = d.relative_to(root).parent
        group = str(rel) if str(rel) != "." else "otras"
        out.append(Session(d, group, len(stems), labelled, str(d.relative_to(root))))
    out.sort(key=lambda s: (s.group.split("/")[0] != "train", s.group, s.sort_key()))
    return out


# ---- tables --------------------------------------------------------------------

@dataclass
class Table:
    path: Path                      # the folder
    firmware: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "firmware (main/models)" if self.firmware else self.path.name

    @property
    def bin(self) -> Path:
        return self.path / "lut_model.bin"

    @property
    def train_id(self) -> int | None:
        return self.meta.get("train_id")

    @property
    def sessions(self) -> list[str]:
        """Folder names the table was trained on, from its .json."""
        return list(self.meta.get("sessions") or [])

    @property
    def session_paths(self) -> list[Path]:
        """Resolved folders it was trained on, when the .json records them
        (tables from before this was recorded have names only)."""
        return [_resolve(p) for p in self.meta.get("session_paths") or []]

    @property
    def number(self) -> int | None:
        m = TABLE_RE.match(self.path.name)
        return int(m.group(1)) if m else None

    # -- the classifier trained behind this table, if any --

    @property
    def clf_bin(self) -> Path:
        return self.path / "clf_model.bin"

    @property
    def clf_meta(self) -> dict:
        j = self.path / "clf_model.json"
        if not hasattr(self, "_clf_meta"):
            try:
                self._clf_meta = json.loads(j.read_text()) if j.exists() else {}
            except (OSError, ValueError):
                self._clf_meta = {}
        return self._clf_meta

    @property
    def has_clf(self) -> bool:
        return self.clf_bin.exists()

    def clf_matches(self) -> bool:
        """The classifier belongs to this table (its .json says so; the C
        check at load is the authority)."""
        return self.has_clf and self.clf_meta.get("lut_id") == self.train_id

    def clf_summary(self) -> str:
        if not self.has_clf:
            return "sin clasificador (etapa 2 no disponible)"
        c = self.clf_meta
        if not self.clf_matches():
            return "clasificador de OTRA tabla (id %s): no se usa" % c.get("lut_id", "?")
        return "clasificador id %s · umbral %s · l2 %s · %s" % (
            c.get("train_id", "?"), c.get("threshold", "?"), c.get("l2", "?"),
            " + ".join(c.get("sessions") or []) or "?")

    def summary(self) -> str:
        m = self.meta
        if not m:
            return "sin lut_model.json"
        return "id %s · sigma %s · k %s · min_ev %s · %s colores · %s" % (
            m.get("train_id", "?"), m.get("sigma", "?"), m.get("k", "?"),
            m.get("min_evidence", "?"), m.get("colours", "?"),
            " + ".join(self.sessions) or "?")


def _resolve(p: str) -> Path:
    q = Path(p)
    return (q if q.is_absolute() else TARGET_CAM / q).resolve()


def load_table(folder: Path, firmware: bool = False) -> Table | None:
    folder = Path(folder)
    if not (folder / "lut_model.bin").exists():
        return None
    meta = {}
    j = folder / "lut_model.json"
    if j.exists():
        try:
            meta = json.loads(j.read_text())
        except (OSError, ValueError):
            meta = {}
    return Table(folder, firmware, meta)


def find_tables() -> list[Table]:
    """The firmware's table first, then tables/ newest number first."""
    out = []
    fw = load_table(FIRMWARE_MODELS, firmware=True)
    if fw:
        out.append(fw)
    if TABLES_DIR.is_dir():
        own = [t for t in (load_table(d) for d in TABLES_DIR.iterdir() if d.is_dir()) if t]
        own.sort(key=lambda t: (-(t.number if t.number is not None else -1), t.name))
        out.extend(own)
    return out


def next_table_number(tables_dir: Path = TABLES_DIR) -> int:
    n = 0
    if tables_dir.is_dir():
        for d in tables_dir.iterdir():
            m = TABLE_RE.match(d.name)
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def check_table_name(name: str, tables_dir: Path = TABLES_DIR) -> str:
    """Why `name` cannot be a new table under tables_dir, or "" if it can."""
    if not name:
        return "Escribe un nombre para la tabla."
    if not NAME_RE.match(name):
        return ("El nombre solo puede tener letras sin tilde, números, «_», «-» y «.», "
                "sin espacios, y no puede empezar por «.» ni «-».")
    if (tables_dir / name).exists():
        return "Ya existe tables/%s: elige otro nombre." % name
    return ""


def table_name(n: int, sessions: list[Session]) -> str:
    parts = ["tabla_%d" % n]
    for colour in COLOURS:
        nums = sorted({s.number for s in sessions if s.colour == colour})
        parts.append("%s_%s" % (COLOUR_LETTER[colour],
                                "-".join(str(k) for k in nums) if nums else "0"))
    for s in sorted(sessions, key=lambda s: s.name):
        if s.colour is None:
            parts.append("x_%s" % s.name)
    return "_".join(parts)


def firmware_copy_name(t: Table) -> str:
    """The folder under tables/ that the firmware's table is copied to when a
    classifier is trained for it: main/models is written only on install."""
    return "firmware_id%s" % (t.train_id or "0")
