"""Installing a table into (a scratch copy of) main/models, the build check,
the camera check, the classifier page's verdict, and the two new pages'
state. Nothing here touches the real main/models, a board or ESP-IDF."""
import shutil
from pathlib import Path

import numpy as np
import pytest

from tcview import detector as D
from train_gui import install, jobs, paths
from train_gui import clf_page

import train_clf
import train_lut


def make_table(folder: Path, train_id: int, clf_for=None, hue=0):
    """A table folder from an HSV setting (a valid record the camera would
    accept), with a classifier behind table `clf_for` if given."""
    folder.mkdir(parents=True)
    bits = D.hsv_table(D.Settings(hue=hue))
    rec = train_lut.record(bits, train_id)
    meta = dict(train_id=train_id, sigma=0, k=1, min_evidence=1, colours=1, sessions=["x"])
    (folder / "lut_model.bin").write_bytes(rec)
    (folder / "lut_model.h").write_text(train_lut.c_header(rec, meta))
    (folder / "lut_model.json").write_text('{"train_id": %d, "sessions": ["x"]}' % train_id)
    if clf_for is not None:
        n = D.BLOB_FEAT_N
        mdl = train_clf.Model(np.zeros(n), np.ones(n), np.zeros(n), 0.0, 1.0)
        crec = train_clf.record(mdl, 0.5, clf_for, train_id + 1)
        cmeta = dict(train_id=train_id + 1, lut_id=clf_for, l2=1, threshold=0.5, sessions=["x"])
        (folder / "clf_model.bin").write_bytes(crec)
        (folder / "clf_model.h").write_text(train_clf.c_header(crec, cmeta))
        (folder / "clf_model.json").write_text('{"train_id": %d, "lut_id": %d}'
                                               % (train_id + 1, clf_for))
    return paths.load_table(folder)


@pytest.fixture
def scratch(tmp_path):
    models, tables = tmp_path / "models", tmp_path / "tables"
    make_table(models, 1000)
    (models / "clf_model.h").write_text(train_clf.NONE_HEADER)
    tables.mkdir()
    return models, tables


def test_install_backs_up_copies_and_undoes(scratch):
    models, tables = scratch
    t = make_table(tables / "nueva", 2000, clf_for=2000, hue=10)
    assert install.check_installable(t) == ""
    r = install.install(t, models, tables, now=0)
    assert r.backup is not None and (r.backup / "lut_model.bin").exists()
    assert not (r.backup / "clf_model.h").exists()      # the placeholder is not a model
    now = install.installed(models, tables)
    assert (now.lut_id, now.clf_id, now.clf_state, now.same_as) == (2000, 2001, "ok", "nueva")
    assert (models / "clf_model.h").read_bytes() == (t.path / "clf_model.h").read_bytes()

    # Undo = install the backup: it has no classifier, so the placeholder
    # goes back and no stale stage 2 remains. No second backup: what was
    # installed is still tables/nueva.
    back = paths.load_table(r.backup)
    r2 = install.install(back, models, tables)
    assert r2.backup is None and "nueva" in r2.backup_note
    now = install.installed(models, tables)
    assert (now.lut_id, now.clf_state) == (1000, "none")
    assert not (models / "clf_model.bin").exists()
    assert (models / "clf_model.h").read_text() == train_clf.NONE_HEADER


def test_a_classifier_for_another_table_is_not_installable(scratch):
    _, tables = scratch
    t = make_table(tables / "mezcla", 3000, clf_for=999)
    assert "clasificador" in install.check_installable(t)
    assert "OTRA" in t.clf_summary()
    with pytest.raises(ValueError):
        install.install(t, *scratch)


def test_build_has_reads_the_records_from_the_image(scratch, tmp_path):
    models, tables = scratch
    app = tmp_path / "target_cam.bin"
    assert install.build_has(models, app) is None
    lut = (models / "lut_model.bin").read_bytes()
    app.write_bytes(b"\0" * 100 + lut + b"\0" * 50)
    assert install.build_has(models, app) is True
    t = make_table(tables / "c", 4000, clf_for=4000)
    install.install(t, models, tables)
    assert install.build_has(models, app) is False          # old table in the image
    img = (models / "lut_model.bin").read_bytes() + (models / "clf_model.bin").read_bytes()
    app.write_bytes(img)
    assert install.build_has(models, app) is True
    # A classifier record left in an image built for a table without one.
    back = next(d for d in tables.iterdir() if d.name.startswith("firmware_antes"))
    install.install(paths.load_table(back), models, tables)
    app.write_bytes((models / "lut_model.bin").read_bytes() + img)
    assert install.build_has(models, app) is False


def test_camera_verdict():
    want = install.Installed(7, 8, "ok", "")
    ok = dict(lut="ok", lut_id="7", clf="ok", clf_id="8", fw="x", stage="2", stage_eff="2")
    assert install.camera_verdict(ok, want)[0] == "ok"
    assert install.camera_verdict({**ok, "lut_id": "6"}, want)[0] == "err"
    assert install.camera_verdict({**ok, "clf": "none"}, want)[0] == "err"
    none = install.Installed(7, 0, "none", "")
    assert install.camera_verdict({**ok, "clf": "none"}, none)[0] == "ok"
    assert install.camera_verdict(ok, none)[0] == "warn"


def test_flash_command_is_the_readmes():
    cmd = install.flash_command("/dev/ttyACM0")
    assert cmd[1:] == ["-m", "esptool", "--chip", "esp32", "-p", "/dev/ttyACM0", "-b",
                       "115200", "--before", "no_reset", "--after", "no_reset", "write_flash",
                       "@flash_args"]
    assert "idf.py" in install.build_command()[-1]


LOG = """stage 1 alone on the same frames:  cost 38 = false b1 21/44 + wrong b1 7 + FN 10   FP 65  dropped 0 (0 balloons)

leave one session out, 76 settings, best first:
  l2    10  threshold 0.40  cost 33 = false b1 14/44 + wrong b1 3 + FN 16   FP 22  dropped 50 (7 balloons)
"""


def test_clf_log_verdicts():
    assert clf_page.judge_log(LOG)[0] == "ok"
    tone, text = clf_page.judge_log(LOG + "\nWARNING: the chosen setting loses balloons that "
                                    "stage 1 found (FN 10 -> 16).\n")
    assert tone == "warn" and "pierde globos" in text and "costo 33" in text
    assert clf_page.judge_log(LOG + "\nWARNING: the classifier does not lower the held-out "
                              "cost\n")[0] == "err"
    assert clf_page.judge_log("final classifier: ...")[0] == "info"


def test_pages_state():
    from PyQt6.QtWidgets import QApplication
    from tcview.ui import theme
    from train_gui.__main__ import MainWindow
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    win = MainWindow()
    win.show()
    app.processEvents()
    fw = win.firmware
    fw.table.select("firmware (main/models)")
    app.processEvents()
    # The firmware's own table cannot be installed over itself.
    assert not fw.install_btn.isEnabled()
    assert "firmware" in fw.check_lbl.text()
    assert fw.current_lbl.text().startswith("main/models: tabla id")
    # The classifier page, on the firmware's table, writes to a copy.
    c = win.clf
    c.table.select("firmware (main/models)")
    app.processEvents()
    assert c.out_folder().name.startswith("firmware_id")
    assert "main/models no se toca" in c.out_lbl.text()
    win.close()
    app.processEvents()


def test_blob_sets_are_sessions_for_the_classifier_only(tmp_path):
    import sys
    sys.path.insert(0, str(paths.TARGET_CAM / "host" / "viewer" / "tests"))
    from test_clf import blob_set       # the viewer's blob-set maker
    root = tmp_path / "data"
    (root / "datasets").mkdir(parents=True)
    blob_set(root / "datasets" / "hsv.npz", np.random.default_rng(0), 77, stage=0)
    blob_set(root / "datasets" / "t77.npz", np.random.default_rng(1), 77)
    (root / "datasets" / "junk.npz").write_bytes(b"not a set")
    assert paths.find_sessions(root) == []
    found = {s.name: s for s in paths.find_sessions(root, blobsets=True)}
    assert set(found) == {"hsv", "t77"}
    s = found["t77"]
    assert s.is_blobset and s.group == "datasets" and (s.n_frames, s.n_labelled) == (40, 40)
    assert s.blobs.lut_ids == {77: 40} and s.blobs.gains == ["fixed 76,64,75"]
    assert "etapa 0: 40" in found["hsv"].blobs.text()

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QSettings
    app = QApplication.instance() or QApplication([])
    page = clf_page.ClfPage(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    mine = paths.Table(tmp_path, meta={"train_id": 77})
    other = paths.Table(tmp_path, meta={"train_id": 78})
    assert page._sets_problem(mine, [s]) == ""
    assert "detrás de la tabla 77" in page._sets_problem(other, [s])
    assert "etapa 0" in page._sets_problem(mine, [found["hsv"]])
    assert not page.allow0.isHidden()
    page.allow0.setChecked(True)
    assert page._sets_problem(mine, [found["hsv"]]) == ""
    assert "--allow-stage0" in jobs.clf_command(mine, tmp_path, [s], None, None,
                                                page._allow0())
    page.close()
    app.processEvents()
