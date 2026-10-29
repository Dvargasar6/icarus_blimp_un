"""
Making a table (and its classifier) the firmware's: README "Retraining the
colour table", steps 5 and 6, as functions the Firmware page calls. No Qt
here, so the tests can run them on a scratch copy of main/models.

  install()       backs up main/models, then copies the table's lut_model.*
                  and, if it has one, its clf_model.*; a table without a
                  classifier puts the "no classifier" placeholder back, so
                  the firmware never keeps a stage 2 trained for another
                  table. The backup is itself a table folder under tables/
                  (firmware_antes_<date>), so undoing is installing it.
  build_command() idf.py build, through the ESP-IDF activation script (the
                  project CLAUDE.md: idf.py is a shell function after it).
  flash_command() esptool at 115200 with no_reset, from build/, the command
                  the README gives for the ESP32-CAM-MB with BOOT held.
  build_has()     whether build/target_cam.bin holds the installed records.
                  The records are compiled in as byte arrays, so they appear
                  verbatim in the image: this says "the build is current" from
                  the bytes themselves, not from file dates.
  query_camera()  asks a running camera for #!CFG ('k') without resetting it
                  (DTR and RTS low before the port opens, as tcview does) and
                  returns its lut_id / clf state: step 6's check.
"""
from __future__ import annotations

import filecmp
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tcview import detector as D
from tcview import protocol as P

from . import paths

HOME = Path.home()
IDF_ACTIVATE = HOME / ".espressif" / "tools" / "activate_idf_v5.5.5.sh"
IDF_PYTHON = HOME / ".espressif" / "tools" / "python" / "v5.5.5" / "venv" / "bin" / "python"
BUILD_DIR = paths.TARGET_CAM / "build"
APP_BIN = BUILD_DIR / "target_cam.bin"

LUT_FILES = ("lut_model.bin", "lut_model.h", "lut_model.json")
CLF_FILES = ("clf_model.bin", "clf_model.h", "clf_model.json")


def none_header() -> str:
    """main/models/clf_model.h with no classifier: train_clf.py's copy."""
    import train_clf
    return train_clf.NONE_HEADER


# ---- what is installed ---------------------------------------------------------

@dataclass
class Installed:
    lut_id: int
    clf_id: int             # 0 when none
    clf_state: str          # "none", "ok", "mismatch", "bad"
    same_as: str            # a tables/ folder holding the same bytes, or ""


def installed(models: Path = paths.FIRMWARE_MODELS, tables: Path = paths.TABLES_DIR) -> Installed:
    lut = models / "lut_model.bin"
    lut_id = 0
    if lut.exists():
        try:
            lut_id = D.lut_models(record=lut.read_bytes()).lut_id
        except D.ModelError:
            lut_id = -1
    clf_id, clf_state = 0, "none"
    clf = models / "clf_model.bin"
    if clf.exists():
        try:
            c, clf_id = D.load_clf(clf.read_bytes())
            clf_state = "ok" if c.lut_id == lut_id else "mismatch"
        except D.ModelError:
            clf_state = "bad"
    return Installed(lut_id, clf_id, clf_state, _same_table(models, tables))


def _same_table(models: Path, tables: Path) -> str:
    """A table under tables/ with the same table bytes, and the same
    classifier (or none), as `models`."""
    if not tables.is_dir() or not (models / "lut_model.bin").exists():
        return ""
    for d in sorted(tables.iterdir()):
        if not (d / "lut_model.bin").exists():
            continue
        if not filecmp.cmp(d / "lut_model.bin", models / "lut_model.bin", shallow=False):
            continue
        a, b = d / "clf_model.bin", models / "clf_model.bin"
        if a.exists() != b.exists() or (a.exists() and not filecmp.cmp(a, b, shallow=False)):
            continue
        return d.name
    return ""


def check_installable(t: paths.Table) -> str:
    """Why `t` cannot be installed, or "": the camera's own checks, run here
    first, so a table the firmware would refuse never reaches it."""
    if t.firmware:
        return "Esta ya es la tabla del firmware."
    try:
        m = D.models(t.bin)
    except (OSError, D.ModelError) as e:
        return "La tabla no pasa la comprobación de la cámara: %s" % e
    if t.has_clf:
        try:
            D.add_clf(m, t.clf_bin.read_bytes())
        except (OSError, D.ModelError) as e:
            return ("Su clasificador no pasa la comprobación de la cámara: %s. Reentrénalo "
                    "para esta tabla o bórralo de la carpeta." % e)
    for f in LUT_FILES[:2]:
        if not (t.path / f).exists():
            return "Falta %s en %s." % (f, t.path)
    if t.has_clf and not (t.path / "clf_model.h").exists():
        return "Falta clf_model.h en %s." % t.path
    return ""


# ---- installing ----------------------------------------------------------------

@dataclass
class InstallResult:
    backup: Optional[Path]      # None when the old firmware table was already a table
    backup_note: str
    lut_id: int
    clf_id: int


def install(t: paths.Table, models: Path = paths.FIRMWARE_MODELS,
            tables: Path = paths.TABLES_DIR, now: Optional[float] = None) -> InstallResult:
    """Back up `models`, then copy table `t` (and its classifier) into it."""
    why = check_installable(t)
    if why:
        raise ValueError(why)
    before = installed(models, tables)
    backup, note = None, ""
    if before.same_as:
        note = "la tabla anterior ya estaba guardada como tables/%s" % before.same_as
    elif (models / "lut_model.bin").exists():
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
        backup = tables / ("firmware_antes_%s" % stamp)
        backup.mkdir(parents=True)
        for f in LUT_FILES + CLF_FILES:
            if (models / f).exists() and not (f == "clf_model.h" and
                                              not (models / "clf_model.bin").exists()):
                shutil.copy2(models / f, backup / f)
        note = "la tabla anterior quedó en tables/%s" % backup.name

    models.mkdir(parents=True, exist_ok=True)
    for f in LUT_FILES:
        if (t.path / f).exists():
            shutil.copy2(t.path / f, models / f)
    if t.has_clf:
        for f in CLF_FILES:
            if (t.path / f).exists():
                shutil.copy2(t.path / f, models / f)
    else:
        for f in ("clf_model.bin", "clf_model.json"):
            (models / f).unlink(missing_ok=True)
        (models / "clf_model.h").write_text(none_header())

    after = installed(models, tables)
    if after.lut_id != t.train_id and t.train_id is not None:
        raise RuntimeError("main/models no quedó con la tabla %s (id %s, hay %s)"
                           % (t.name, t.train_id, after.lut_id))
    return InstallResult(backup, note, after.lut_id, after.clf_id)


# ---- building and flashing -----------------------------------------------------

def build_command() -> list[str]:
    return ["bash", "-c", 'source "%s" >/dev/null && idf.py -C "%s" build'
            % (IDF_ACTIVATE, paths.TARGET_CAM)]


def flash_command(port: str) -> list[str]:
    """Run from BUILD_DIR (flash_args names its files relative to it)."""
    return [str(IDF_PYTHON), "-m", "esptool", "--chip", "esp32", "-p", port, "-b", "115200",
            "--before", "no_reset", "--after", "no_reset", "write_flash", "@flash_args"]


def toolchain_problem() -> str:
    if not IDF_ACTIVATE.exists():
        return "No encuentro ESP-IDF (%s)." % IDF_ACTIVATE
    if not IDF_PYTHON.exists():
        return "No encuentro el Python de ESP-IDF (%s)." % IDF_PYTHON
    return ""


def build_has(models: Path = paths.FIRMWARE_MODELS, app: Path = APP_BIN) -> Optional[bool]:
    """True if the compiled image holds main/models' table and classifier
    records (or no classifier, when none is installed), False if not, None
    when there is no build yet."""
    if not app.exists():
        return None
    img = app.read_bytes()
    lut = models / "lut_model.bin"
    if lut.exists() and lut.read_bytes() not in img:
        return False
    clf = models / "clf_model.bin"
    if clf.exists():
        return clf.read_bytes() in img
    # No classifier installed: an older image may still hold one. Its record
    # starts with the magic, version 1 and the 20-byte header size; the bare
    # magic string alone could be the loader's own literal.
    return b"TCBC\x01\x00\x14\x00" not in img


# ---- the camera ----------------------------------------------------------------

def serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return sorted(p.device for p in list_ports.comports())


def query_camera(port: str, timeout: float = 15.0,
                 cancelled: Callable[[], bool] = lambda: False) -> dict:
    """The camera's #!CFG as a dict, asked for with 'k' every 2 s until one
    arrives (the camera answers commands only once its loop runs, after the
    5 s reflash window). Raises OSError / TimeoutError."""
    import serial
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, P.BOOT_BAUD, 0.1
    s.dtr = False                       # low before open: no reset
    s.rts = False
    s.open()
    try:
        buf = b""
        t0 = time.monotonic()
        next_k = t0
        while time.monotonic() - t0 < timeout and not cancelled():
            if time.monotonic() >= next_k:
                s.write(b"k\n")
                next_k = time.monotonic() + 2.0
            buf += s.read(4096)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("ascii", "replace").strip()
                if text.startswith("#!CFG "):
                    return P.parse_kv(text[6:], convert=False)
        raise TimeoutError("la cámara no respondió a 'k' en %.0f s" % timeout)
    finally:
        s.close()


def camera_verdict(cfg: dict, want: Installed) -> tuple[str, str]:
    """(tone, text): does the camera run what main/models holds?"""
    lut_id = int(cfg.get("lut_id", "0") or 0)
    clf = cfg.get("clf", "?")
    clf_id = int(cfg.get("clf_id", "0") or 0)
    fw = cfg.get("fw", "?")
    parts = ["cámara: lut=%s lut_id=%d clf=%s%s fw=%s stage=%s→%s" % (
        cfg.get("lut", "?"), lut_id, clf, " clf_id=%d" % clf_id if "clf_id" in cfg else "",
        fw, cfg.get("stage", "?"), cfg.get("stage_eff", "?"))]
    if cfg.get("lut") != "ok" or lut_id != want.lut_id:
        return "err", " ".join(parts + ["— NO es la tabla instalada (id %d). Compila y "
                                        "flashea otra vez." % want.lut_id])
    if want.clf_state == "ok":
        if clf != "ok" or ("clf_id" in cfg and clf_id != want.clf_id):
            return "err", " ".join(parts + ["— la tabla es la correcta pero el clasificador "
                                            "no (instalado id %d)." % want.clf_id])
        return "ok", " ".join(parts + ["— tabla y clasificador correctos. Para usar el "
                                       "clasificador: !stage 2 y !save."])
    if clf == "ok":
        return "warn", " ".join(parts + ["— la tabla es la correcta, pero la cámara aún tiene "
                                         "un clasificador: flashea la última compilación."])
    return "ok", " ".join(parts + ["— tabla correcta (sin clasificador: etapa 1)."])
