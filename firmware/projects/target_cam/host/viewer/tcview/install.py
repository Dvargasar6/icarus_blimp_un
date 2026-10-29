"""
Making a model set the firmware's, then building and flashing it: README
"Retraining the colour table" steps 5 and 6, as functions the Firmware tab
calls. No Qt here, so the tests run them on a scratch main/models.

  install()          moves what main/models holds into models/ (as
                     firmware_<lut_id>_<date>, unless a set there already holds
                     the same bytes), then copies the chosen set in: its table,
                     its classifier if it has one belonging to the table, else
                     the "no classifier" placeholder, so the firmware never
                     keeps a stage 2 trained for another table. Undoing is
                     installing the backup.
  build_command()    idf.py build through the ESP-IDF activation script (the
                     project CLAUDE.md: idf.py is a shell function after it).
  flash_command()    esptool at 115200 with no_reset, from build/: what the
                     README gives for the ESP32-CAM-MB with BOOT held.
  build_has()        whether build/target_cam.bin holds main/models' records.
                     They are compiled in as byte arrays, so they appear
                     verbatim in the image: "the build is current" from the
                     bytes, not from file dates.
  camera_verdict()   does the camera's #!CFG report the installed ids?

The activation script is looked for, not assumed: on this project's machines
ESP-IDF has lived at different paths (~/.espressif/v5.5.5 in CLAUDE.md,
~/esp/esp-idf-v5.5.5 on another), and EIM always writes the script to
~/.espressif/tools/activate_idf_v<version>.sh, which knows its own IDF_PATH.
"""
from __future__ import annotations

import filecmp
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import detector as D
from . import library as Lib

BUILD_DIR = Lib.TARGET_CAM / "build"
APP_BIN = BUILD_DIR / "target_cam.bin"
FILES = {k: tuple("%s_model.%s" % (k, e) for e in ("bin", "h", "json")) for k in Lib.KINDS}
CLF_MAGIC_HDR = b"TCBC\x01\x00\x14\x00"     # magic, version 1, 20-byte header
CNN_MAGIC_HDR = b"TCCN\x01\x00\x14\x00"


def none_header(kind: str = "clf") -> str:
    """main/models/<kind>_model.h with no such model: the trainer's own copy,
    which a test checks the committed file against."""
    import sys
    sys.path.insert(0, str(Lib.TARGET_CAM / "host" / "train"))
    if kind == "cnn":
        import train_cnn
        return train_cnn.NONE_HEADER
    import train_clf
    return train_clf.NONE_HEADER


# ---- what is installed -----------------------------------------------------------

@dataclass
class Installed:
    lut_id: int             # 0 none, -1 refused by the C check
    clf_id: int             # 0 none
    clf_state: str          # "none" | "ok" | "mismatch" | "bad"
    same_as: str            # a set under models/ with the same bytes, or ""
    cnn_id: int = 0
    cnn_state: str = "none"


def installed(fw: Path = Lib.FIRMWARE_MODELS, models: Path = Lib.MODELS_DIR) -> Installed:
    lut_id = 0
    lut = fw / "lut_model.bin"
    if lut.exists():
        try:
            lut_id = D.lut_models(record=lut.read_bytes()).lut_id
        except D.ModelError:
            lut_id = -1
    clf_id, state = 0, "none"
    clf = fw / "clf_model.bin"
    if clf.exists():
        try:
            c, clf_id = D.load_clf(clf.read_bytes())
            state = "ok" if c.lut_id == lut_id else "mismatch"
        except D.ModelError:
            state = "bad"
    cnn_id, cstate = 0, "none"
    cnn = fw / "cnn_model.bin"
    if cnn.exists():
        try:
            c, cnn_id = D.load_cnn(cnn.read_bytes())
            cstate = "ok" if c.lut_id == lut_id else "mismatch"
        except D.ModelError:
            cstate = "bad"
    return Installed(lut_id, clf_id, state, same_set(fw, models), cnn_id, cstate)


def _same(a: Path, b: Path) -> bool:
    return a.exists() == b.exists() and (not a.exists() or filecmp.cmp(a, b, shallow=False))


def same_set(fw: Path, models: Path) -> str:
    """A set under models/ whose model records are byte for byte main/models'
    (and which has a classifier or CNN exactly where main/models has one)."""
    if not models.is_dir() or not (fw / "lut_model.bin").exists():
        return ""
    for d in sorted(models.iterdir()):
        if d.is_dir() and all(_same(d / ("%s_model.bin" % k), fw / ("%s_model.bin" % k))
                              for k in Lib.KINDS):
            return d.name
    return ""


def check_installable(s: Lib.ModelSet) -> str:
    """Why set s cannot be installed, or "": the camera's own checks run here
    first, so a set the firmware would refuse never reaches it."""
    if s.firmware:
        return "This is already the firmware's set."
    try:
        m = D.models(s.bin("lut"))
    except (OSError, D.ModelError) as e:
        return "Its colour table fails the camera's check: %s" % e
    if s.has("clf"):
        try:
            D.add_clf(m, s.bin("clf").read_bytes())
        except (OSError, D.ModelError) as e:
            return ("Its classifier fails the camera's check: %s. Retrain it for this table "
                    "or remove it from the folder." % e)
    if s.has("cnn"):
        if not s.has("clf"):
            return "It has a CNN but no classifier: stage 3 needs both."
        try:
            D.add_cnn(m, s.bin("cnn").read_bytes())
        except (OSError, D.ModelError) as e:
            return ("Its CNN fails the camera's check: %s. Retrain it for this table or "
                    "remove it from the folder." % e)
    for k in Lib.KINDS:
        if s.has(k) and not (s.path / ("%s_model.h" % k)).exists():
            return "%s_model.h is missing in %s." % (k, s.path)
    return ""


@dataclass
class InstallResult:
    backup: Optional[Path]      # None when main/models was already saved under models/
    note: str
    lut_id: int
    clf_id: int


def backup_name(inst: Installed, now: Optional[float] = None) -> str:
    return "firmware_%s_%s" % (inst.lut_id if inst.lut_id > 0 else "unknown",
                               time.strftime("%Y%m%d_%H%M%S", time.localtime(now)))


def install(s: Lib.ModelSet, fw: Path = Lib.FIRMWARE_MODELS, models: Path = Lib.MODELS_DIR,
            now: Optional[float] = None) -> InstallResult:
    """Back main/models up into models/, then copy set s into main/models."""
    why = check_installable(s)
    if why:
        raise ValueError(why)
    before = installed(fw, models)
    backup, note = None, ""
    if before.same_as:
        note = "the previous models were already saved as models/%s" % before.same_as
    elif (fw / "lut_model.bin").exists():
        backup = models / backup_name(before, now)
        backup.mkdir(parents=True)
        for k in Lib.KINDS:
            if (fw / ("%s_model.bin" % k)).exists():   # not the placeholder clf_model.h
                for f in FILES[k]:
                    if (fw / f).exists():
                        shutil.copy2(fw / f, backup / f)
        note = "the previous models were moved to models/%s" % backup.name

    fw.mkdir(parents=True, exist_ok=True)
    for f in FILES["lut"]:
        if (s.path / f).exists():
            shutil.copy2(s.path / f, fw / f)
    for k in ("clf", "cnn"):    # check_installable ran the C checks: they are this table's
        if s.has(k):
            for f in FILES[k]:
                if (s.path / f).exists():
                    shutil.copy2(s.path / f, fw / f)
        else:
            for f in ("%s_model.bin" % k, "%s_model.json" % k):
                (fw / f).unlink(missing_ok=True)
            (fw / ("%s_model.h" % k)).write_text(none_header(k))
    after = installed(fw, models)
    if s.lut_id is not None and after.lut_id != s.lut_id:
        raise RuntimeError("main/models did not end up with %s's table (id %s, found %s)"
                           % (s.name, s.lut_id, after.lut_id))
    return InstallResult(backup, note, after.lut_id, after.clf_id)


# ---- building and flashing -------------------------------------------------------

def find_activate() -> Optional[Path]:
    """The newest EIM activation script, ~/.espressif/tools/activate_idf_v*.sh."""
    found = sorted((Path.home() / ".espressif" / "tools").glob("activate_idf_v*.sh"))
    return found[-1] if found else None


def toolchain_problem(activate: Optional[Path]) -> str:
    if activate is None or not Path(activate).is_file():
        return ("ESP-IDF's activation script was not found (looked for "
                "~/.espressif/tools/activate_idf_v*.sh). Give its path.")
    return ""


def _in_idf(activate: Path, cmd: str) -> list:
    """bash running cmd after the activation script (idf.py is a function)."""
    return ["bash", "-c", "source %s >/dev/null 2>&1 && %s" % (shlex.quote(str(activate)), cmd)]


def build_command(activate: Path, project: Path = Lib.TARGET_CAM) -> list:
    return _in_idf(activate, "idf.py -C %s build" % shlex.quote(str(project)))


def flash_command(activate: Path, port: str, build: Path = BUILD_DIR) -> list:
    """BOOT held: no reset either side, 115200 (the bridge wedged once after a
    460800 flash, README). esptool v5 spelling (write-flash, no-reset), as
    idf.py itself prints it; the README's underscores are v4's."""
    return _in_idf(activate, "cd %s && python -m esptool --chip esp32 -p %s -b 115200 "
                   "--before no-reset --after no-reset write-flash @flash_args"
                   % (shlex.quote(str(build)), shlex.quote(port)))


def build_has(fw: Path = Lib.FIRMWARE_MODELS, app: Path = APP_BIN) -> Optional[bool]:
    """True if the image holds main/models' table and classifier (or no
    classifier, when none is installed), False if not, None with no build."""
    if not app.exists():
        return None
    img = app.read_bytes()
    lut = fw / "lut_model.bin"
    if lut.exists() and lut.read_bytes() not in img:
        return False
    for k, hdr in (("clf", CLF_MAGIC_HDR), ("cnn", CNN_MAGIC_HDR)):
        rec = fw / ("%s_model.bin" % k)
        if rec.exists():
            if rec.read_bytes() not in img:
                return False
        elif hdr in img:
            return False                # an older image still holds one
    return True


# ---- the camera ------------------------------------------------------------------

def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def camera_ids(cfg: dict) -> dict:
    """The model state a #!CFG reports."""
    return dict(lut=str(cfg.get("lut", "?")), lut_id=_int(cfg.get("lut_id")),
                clf=str(cfg.get("clf", "?")), clf_id=_int(cfg.get("clf_id")),
                has_clf_id="clf_id" in cfg, cnn=str(cfg.get("cnn", "?")),
                cnn_id=_int(cfg.get("cnn_id")), has_cnn_id="cnn_id" in cfg, fw=str(cfg.get("fw", "?")),
                stage=cfg.get("stage", "?"), stage_eff=cfg.get("stage_eff", "?"))


def camera_verdict(cfg: dict, want: Installed) -> tuple[str, str]:
    """(tone, text): does the camera run what main/models holds?"""
    c = camera_ids(cfg)
    head = "camera: lut=%s lut_id=%d clf=%s%s fw=%s stage %s→%s" % (
        c["lut"], c["lut_id"], c["clf"], " clf_id=%d" % c["clf_id"] if c["has_clf_id"] else "",
        c["fw"], c["stage"], c["stage_eff"])
    if c["lut"] != "ok" or c["lut_id"] != want.lut_id:
        return "err", head + " — NOT the installed table (id %d). Build and flash again." \
            % want.lut_id
    if want.cnn_state == "ok":
        if c["cnn"] != "ok" or (c["has_cnn_id"] and c["cnn_id"] != want.cnn_id):
            return "err", head + " — the CNN is not the installed one (id %d). Build and " \
                "flash again." % want.cnn_id
    elif c["cnn"] == "ok":
        return "warn", head + " — the camera still has a CNN: flash the latest build."
    if want.clf_state == "ok":
        if c["clf"] != "ok" or (c["has_clf_id"] and c["clf_id"] != want.clf_id):
            return "err", head + " — the table is right but the classifier is not " \
                "(installed id %d). Build and flash again." % want.clf_id
        if want.cnn_state == "ok":
            return "ok", head + " — table, classifier and CNN are the installed ones. To use " \
                "the CNN: !stage 3, then !save."
        return "ok", head + " — table and classifier are the installed ones. To use the " \
            "classifier: !stage 2, then !save."
    if c["clf"] == "ok":
        return "warn", head + " — the table is right, but the camera still has a " \
            "classifier: flash the latest build."
    return "ok", head + " — the installed table (no classifier: stage 1)."
