"""Installing a model set into (a scratch copy of) main/models, the build and
flash commands, the camera check, and the Firmware tab's flash walk-through
with a fake esptool."""
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from PyQt6.QtCore import QEventLoop, QTimer

from tcview import detector as D
from tcview import firmware_tab as F
from tcview import install as I
from tcview import library as Lib

HOST = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HOST / "train"))
import train_clf  # noqa: E402


@pytest.fixture
def dirs(tmp_path):
    """fw: a copy of main/models (table, placeholder classifier); models/
    with 'with_clf' (that table + a classifier) and 'other' (another table)."""
    fw = tmp_path / "main_models"
    shutil.copytree(Lib.FIRMWARE_MODELS, fw)
    models = tmp_path / "models"
    base = Lib.load_set(fw, firmware=True)
    out = Lib.new_set("with_clf", base, "lut", models)
    n = D.BLOB_FEAT_N
    mdl = train_clf.Model(np.zeros(n), np.ones(n), np.zeros(n), 1.0, 1.0)
    rec = train_clf.record(mdl, 0.5, base.lut_id, 77)
    (out / "clf_model.bin").write_bytes(rec)
    (out / "clf_model.h").write_text(train_clf.c_header(rec, dict(
        train_id=77, lut_id=base.lut_id, l2=1.0, threshold=0.5, sessions=["x"])))
    (out / "clf_model.json").write_text(json.dumps(dict(train_id=77, lut_id=base.lut_id,
                                                        threshold=0.5)))
    other = models / "other"
    other.mkdir()
    for f in Lib.FIRMWARE_MODELS.glob("lut_model.*"):
        shutil.copy(f, other / f.name)
    return fw, models


# ---- install.py ------------------------------------------------------------------

def test_installed_reads_main_models(dirs):
    fw, models = dirs
    inst = I.installed(fw, models)
    assert inst.lut_id == D.models().lut_id and inst.clf_state == "none" and inst.clf_id == 0
    assert inst.same_as == "other"          # the same table bytes, no classifier


def test_install_backs_up_then_copies_and_undoes(dirs):
    fw, models = dirs
    s = Lib.load_set(models / "with_clf")
    assert I.check_installable(s) == ""
    r = I.install(s, fw, models)
    # main/models was already saved as models/other: no second copy.
    assert r.backup is None and "models/other" in r.note
    inst = I.installed(fw, models)
    assert inst.clf_state == "ok" and inst.clf_id == 77 and inst.same_as == "with_clf"
    # Back to a set without a classifier: the placeholder returns.
    r = I.install(Lib.load_set(models / "other"), fw, models)
    assert r.backup is None and r.clf_id == 0
    assert (fw / "clf_model.h").read_text() == train_clf.NONE_HEADER
    assert not (fw / "clf_model.bin").exists()


def test_install_moves_an_unsaved_main_models_into_models(dirs):
    fw, models = dirs
    shutil.rmtree(models / "other")
    r = I.install(Lib.load_set(models / "with_clf"), fw, models, now=1793300000)
    assert r.backup is not None and r.backup.name.startswith("firmware_%d_" % D.models().lut_id)
    assert sorted(p.name for p in r.backup.iterdir()) == ["lut_model.bin", "lut_model.h",
                                                          "lut_model.json"]
    assert Lib.load_set(r.backup).stages == [1]


def test_what_cannot_be_installed(dirs):
    fw, models = dirs
    assert "already the firmware" in I.check_installable(Lib.load_set(fw, firmware=True))
    s = Lib.load_set(models / "with_clf")
    n = D.BLOB_FEAT_N
    bad = train_clf.record(train_clf.Model(np.zeros(n), np.ones(n), np.zeros(n), 0, 1),
                           0.5, s.lut_id + 1, 5)
    (s.path / "clf_model.bin").write_bytes(bad)
    assert "classifier fails" in I.check_installable(s)
    with pytest.raises(ValueError):
        I.install(s, fw, models)
    (s.path / "lut_model.bin").write_bytes(b"junk")
    assert "colour table fails" in I.check_installable(Lib.load_set(s.path))


def test_build_has(dirs, tmp_path):
    fw, models = dirs
    app = tmp_path / "app.bin"
    assert I.build_has(fw, app) is None
    app.write_bytes(b"x" * 10 + (fw / "lut_model.bin").read_bytes() + b"y")
    assert I.build_has(fw, app) is True
    app.write_bytes(app.read_bytes() + I.CLF_MAGIC_HDR)        # an old stage 2 inside
    assert I.build_has(fw, app) is False
    I.install(Lib.load_set(models / "with_clf"), fw, models)
    assert I.build_has(fw, app) is False                        # classifier not built in
    app.write_bytes(app.read_bytes() + (fw / "clf_model.bin").read_bytes())
    assert I.build_has(fw, app) is True


def test_commands(tmp_path):
    act = tmp_path / "activate idf.sh"
    b = I.build_command(act, Path("/p/target_cam"))
    assert b[:2] == ["bash", "-c"] and "'%s'" % act in b[2] and b[2].endswith(
        "idf.py -C /p/target_cam build")
    f = I.flash_command(act, "/dev/ttyACM0", Path("/p/build"))
    assert "cd /p/build && python -m esptool --chip esp32 -p /dev/ttyACM0 -b 115200" in f[2]
    assert "--before no-reset --after no-reset write-flash @flash_args" in f[2]
    assert I.toolchain_problem(None) and I.toolchain_problem(tmp_path / "nope.sh")
    act.write_text("")
    assert I.toolchain_problem(act) == ""


def test_camera_verdict(dirs):
    fw, models = dirs
    inst = I.installed(fw, models)
    cfg = dict(lut="ok", lut_id=inst.lut_id, clf="none", clf_id=0, fw="x", stage=1, stage_eff=1)
    assert I.camera_verdict(cfg, inst)[0] == "ok"
    assert I.camera_verdict(dict(cfg, lut_id=1), inst)[0] == "err"
    assert I.camera_verdict(dict(cfg, clf="ok"), inst)[0] == "warn"
    I.install(Lib.load_set(models / "with_clf"), fw, models)
    inst = I.installed(fw, models)
    assert I.camera_verdict(cfg, inst)[0] == "err"
    assert I.camera_verdict(dict(cfg, clf="ok", clf_id=77), inst)[0] == "ok"
    # Older firmware without clf_id: the state alone decides.
    old = dict(cfg, clf="ok")
    del old["clf_id"]
    assert I.camera_verdict(old, inst)[0] == "ok"


# ---- the tab ---------------------------------------------------------------------

def tab(dirs, tmp_path, app=True):
    fw, models = dirs
    bin_ = tmp_path / "target_cam.bin"
    if app:
        bin_.write_bytes((fw / "lut_model.bin").read_bytes())
    t = F.FirmwareTab(None, models, fw, bin_)
    t.port = lambda: "/dev/fake"
    act = tmp_path / "activate.sh"
    act.write_text("")
    t.activate.setText(str(act))
    t.confirm = lambda text: True
    return t


def wait_job(t, ms=30000):
    loop = QEventLoop()
    t.job.finished.connect(lambda *_: QTimer.singleShot(0, loop.quit))
    QTimer.singleShot(ms, loop.quit)
    if t.job.running():
        loop.exec()


def test_tab_installs_and_shows_the_build_is_stale(dirs, tmp_path):
    fw, models = dirs
    t = tab(dirs, tmp_path)
    assert t.build_banner.tone() == "ok"
    t.set_combo.setCurrentIndex(t.set_combo.findData(str(models / "with_clf")))
    assert t.install_btn.isEnabled() and t.install()
    assert t.install_banner.tone() == "ok" and "classifier 77" in t.install_banner.text()
    assert t.build_banner.tone() == "warn" and "NOT" in t.build_banner.text()
    assert "classifier id 77" in t.now_lbl.text()


def test_flash_walk_through(dirs, tmp_path, monkeypatch):
    fw, models = dirs
    t = tab(dirs, tmp_path)
    monkeypatch.setattr(I, "flash_command", lambda act, port, build=None: [
        sys.executable, "-c", "print('Hash of data verified.')"])
    assert t.flash_btn.isEnabled()
    assert t.start_flash() and t.step == F.HOLD_BOOT and t.boot_btn.isVisibleTo(t)
    assert t.flash_now() and t.step == F.FLASHING
    wait_job(t)
    assert t.step == F.REPLUG and "Release BOOT" in t.step_banner.text()
    assert "Hash of data verified." in t.log.toPlainText()
    assert t.reconnect() and t.step == F.CHECKING
    inst = I.installed(fw, models)
    t.on_cfg(dict(lut="ok", lut_id=inst.lut_id, clf="none", clf_id=0, fw="t", stage=1,
                  stage_eff=1))
    assert t.step == F.IDLE and t.step_banner.tone() == "ok"
    assert t.step_banner.text().startswith("Flashed.") and t.cam_banner.tone() == "ok"


def test_flash_failure_and_abort(dirs, tmp_path, monkeypatch):
    t = tab(dirs, tmp_path)
    monkeypatch.setattr(I, "flash_command", lambda act, port, build=None: [
        sys.executable, "-c", "import sys; sys.exit(2)"])
    t.start_flash()
    t.flash_now()
    wait_job(t)
    assert t.step == F.IDLE and t.step_banner.tone() == "err" and "BOOT" in t.step_banner.text()
    t.start_flash()
    t.abort()
    assert t.step == F.IDLE and t.flash_btn.isEnabled()


def test_no_build_no_flash(dirs, tmp_path):
    t = tab(dirs, tmp_path, app=False)
    assert not t.flash_btn.isEnabled() and t.build_banner.tone() == "warn"
    t.activate.setText("")
    assert not t.build_btn.isEnabled() and "activation script" in t.why.text()


def test_install_a_set_with_a_cnn_and_back(dirs, tmp_path):
    import train_cnn
    fw, models = dirs
    s = Lib.load_set(models / "with_clf")
    q = train_cnn.quantise(train_cnn.Net(16), np.random.default_rng(0).integers(
        0, 256, (8, 3, 16, 16), dtype=np.uint8), 0.5)
    rec = train_cnn.record(q, s.lut_id, 88)
    (s.path / "cnn_model.bin").write_bytes(rec)
    (s.path / "cnn_model.h").write_text(train_cnn.c_header(rec, dict(
        train_id=88, lut_id=s.lut_id, n=16, threshold=0.5, sessions=["x"])))
    (s.path / "cnn_model.json").write_text(json.dumps(dict(train_id=88, lut_id=s.lut_id,
                                                           threshold=0.5, n=16)))
    s = Lib.load_set(s.path)
    assert s.stages == [1, 2, 3] and I.check_installable(s) == ""
    I.install(s, fw, models)
    inst = I.installed(fw, models)
    assert inst.cnn_state == "ok" and inst.cnn_id == 88
    cfg = dict(lut="ok", lut_id=inst.lut_id, clf="ok", clf_id=77, cnn="ok", cnn_id=88,
               fw="t", stage=3, stage_eff=3)
    assert I.camera_verdict(cfg, inst)[0] == "ok" and "!stage 3" in I.camera_verdict(cfg, inst)[1]
    assert I.camera_verdict(dict(cfg, cnn="none"), inst)[0] == "err"
    app = tmp_path / "app.bin"
    app.write_bytes((fw / "lut_model.bin").read_bytes() + (fw / "clf_model.bin").read_bytes())
    assert I.build_has(fw, app) is False                    # the CNN is not built in
    app.write_bytes(app.read_bytes() + rec)
    assert I.build_has(fw, app) is True
    # Back to a set without a CNN: its placeholder returns.
    I.install(Lib.load_set(models / "other"), fw, models)
    assert (fw / "cnn_model.h").read_text() == train_cnn.NONE_HEADER
    assert not (fw / "cnn_model.bin").exists() and I.installed(fw, models).cnn_state == "none"
    # A CNN without a classifier cannot be installed: stage 3 runs after 2.
    shutil.copy(s.path / "cnn_model.bin", models / "other" / "cnn_model.bin")
    assert "no classifier" in I.check_installable(Lib.load_set(models / "other"))
