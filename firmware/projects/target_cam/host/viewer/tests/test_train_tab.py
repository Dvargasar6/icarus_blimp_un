"""The Train tab (tcview/train_tab.py) and its data picker: sessions, the
command it runs, real training runs into a scratch models/ folder, and the
banner read back from the trainer's .json."""
import json
import shutil
from pathlib import Path

import pytest
from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer

from tcview import library as Lib
from tcview import train_tab as T
from tcview.data_picker import DataPicker

TESTDATA = Path(__file__).resolve().parents[2] / "testdata"


@pytest.fixture
def dirs(tmp_path):
    """data/ with red_s1 and red_s2 as sessions and red_s3 split into two
    subfolders (one capture session by colour, as data/train/s3 is)."""
    data = tmp_path / "data"
    for n in ("red_s1", "red_s2"):
        shutil.copytree(TESTDATA / n, data / n)
    s3 = sorted(p for p in (TESTDATA / "red_s3").iterdir() if p.suffix == ".rgb565")
    for i, p in enumerate(s3):
        sub = data / "s3" / ("a" if i % 2 else "b")
        sub.mkdir(parents=True, exist_ok=True)
        for f in (TESTDATA / "red_s3").glob(p.stem + ".*"):
            shutil.copy(f, sub / f.name)
    (data / "empty_session").mkdir()
    one = next((TESTDATA / "red_s1").glob("*.rgb565"))
    for suf in (".rgb565", ".json"):                     # a frame without its labels
        shutil.copy(one.with_suffix(suf), data / "empty_session")
    models = tmp_path / "models"
    models.mkdir()
    return data, models


def tab(dirs):
    data, models = dirs
    return T.TrainTab(None, data, models, Lib.FIRMWARE_MODELS)


def wait(t, timeout_ms=180000):
    loop = QEventLoop()
    t.job.finished.connect(lambda *_: QTimer.singleShot(0, loop.quit))
    QTimer.singleShot(timeout_ms, loop.quit)
    if t.job.running():
        loop.exec()


def item(picker, name):
    for it in picker._items():
        if Path(it.data(0, Qt.ItemDataRole.UserRole)).name == name:
            return it
    raise KeyError(name)


# ---- data picker ---------------------------------------------------------------------

def test_picker_one_session_per_frame(dirs):
    data, _ = dirs
    p = DataPicker(data)
    s3, a = item(p, "s3"), item(p, "a")
    a.setCheckState(0, Qt.CheckState.Checked)
    s3.setCheckState(0, Qt.CheckState.Checked)       # the parent wins, the child clears
    assert p.checked_paths() == [data / "s3"]
    a.setCheckState(0, Qt.CheckState.Checked)
    assert p.checked_paths() == [data / "s3" / "a"]
    ss = p.sessions()
    assert ss[0].n_labelled == len(list((data / "s3" / "a").glob("*.labels.png")))


def test_picker_refuses_unlabelled_folders_and_flags_gains(dirs):
    data, _ = dirs
    p = DataPicker(data)
    e = item(p, "empty_session")
    assert not p._usable(e)
    p.set_checked([data / "empty_session", data / "red_s1"])
    assert p.checked_paths() == [data / "red_s1"]
    # The committed sessions predate fixed gains: all "unrecorded", one kind.
    p.set_checked([data / "red_s1", data / "red_s2"])
    assert p.summary.tone() == "neutral" and "2 session(s)" in p.summary.text()


def test_picker_reports_mixed_gains(dirs):
    data, _ = dirs
    for f in (data / "red_s2").glob("*.json"):
        m = json.loads(f.read_text())
        m.setdefault("camera_cfg", {}).update(wb="fixed", wb_gain="76,64,75")
        f.write_text(json.dumps(m))
    p = DataPicker(data)
    p.set_checked([data / "red_s1", data / "red_s2"])
    assert p.summary.tone() == "warn" and "MIXED" in p.summary.text()


# ---- verdicts from the .json ---------------------------------------------------------

def test_judge_lut():
    fold = dict(held_out="a", fn=0, false_b1=0, empty_frames=3)
    ok = dict(train_id=1, colours=10, sigma=8, k=7,
              search=[dict(false_b1=0, px_f1=0.6, folds=[fold, dict(fold, held_out="b")])])
    assert T.judge_lut(ok)[0] == "ok"
    lost = json.loads(json.dumps(ok))
    lost["search"][0]["folds"][0]["fn"] = 2
    assert T.judge_lut(lost)[0] == "warn" and "2 balloon(s) lost" in T.judge_lut(lost)[1]
    bad = json.loads(json.dumps(ok))
    bad["search"][0]["false_b1"] = 1
    bad["search"][0]["folds"][1].update(false_b1=1)
    tone, text = T.judge_lut(bad)
    assert tone == "err" and "b 1/3" in text and "Do not fly" in text
    assert T.judge_lut(dict(ok, search=[]))[0] == "info"


def test_judge_clf():
    base = dict(false_b1=10, wrong_b1=2, fn=0)
    best = dict(false_b1=1, wrong_b1=0, fn=0, excluded=False)
    m = dict(train_id=5, lut_id=7, threshold=0.4, l2=1, search=[best], stage1_held_out=base)
    tone, text = T.judge_clf(m)
    assert tone == "ok" and "false blob 1 10 → 1" in text
    assert T.judge_clf(dict(m, warnings=["the chosen setting still puts..."]))[0] == "warn"
    assert T.judge_clf(dict(m, search=[dict(best, excluded=True)]))[0] == "err"
    assert T.judge_clf(dict(m, search=[], stage1_held_out=None))[0] == "info"


# ---- the tab -------------------------------------------------------------------------

def test_what_train_needs(dirs):
    data, models = dirs
    t = tab(dirs)
    assert "Check at least one data folder." in t.problems()
    t.picker.set_checked([data / "red_s1"])
    assert any("two or more sessions" in p for p in t.problems())
    t.fixed.setChecked(True)
    assert t.problems() == []
    t.name.setText("bad name")
    assert any("Letters" in p for p in t.problems()) and not t.train_btn.isEnabled()
    t.name.setText("ok_name")
    assert t.train_btn.isEnabled()
    cmd = t.command(models / "ok_name")
    assert cmd[1].endswith("train_lut.py") and "--sigma" in cmd and cmd[-1] == str(data / "red_s1")
    # Stage 3 is not built; stage 1 refuses blob sets.
    assert not t.stage.buttons[3].isEnabled()


def test_train_a_table_then_a_classifier_behind_it(dirs):
    data, models = dirs
    t = tab(dirs)
    changed = []
    t.sets_changed.connect(lambda: changed.append(1))
    t.picker.set_checked([data / "red_s1", data / "red_s2"])
    t.fixed.setChecked(True)
    t.name.setText("table_a")
    assert t.train()
    wait(t)
    s = Lib.load_set(models / "table_a")
    assert s is not None and s.stages == [1] and changed
    assert (models / "table_a" / "train_log.txt").read_text().startswith("$ python")
    # Fixed parameters on two sessions are still scored session by session.
    assert t.banner.tone() in ("ok", "warn") and "held-out sessions" in t.banner.text()
    assert t.last_set == models / "table_a" and t.verify_btn.isEnabled()

    t.stage.set_value(2)
    i = t.parent_combo.findData(str(models / "table_a"))
    assert i >= 0
    t.parent_combo.setCurrentIndex(i)

    # A classifier behind it, from the session that has false blobs.
    t.picker.set_checked([data / "s3"])
    t.fixed.setChecked(True)
    t.name.setText("clf_ok")
    assert t.train()
    wait(t)
    s2 = Lib.load_set(models / "clf_ok")
    assert s2.stages == [1, 2] and s2.lut_id == s.lut_id
    assert (models / "clf_ok" / "clf_train_log.txt").exists()
    assert Lib.load_set(models / "table_a").stages == [1]      # parent untouched
    assert "Classifier id" in t.banner.text()


def test_a_failed_run_leaves_no_half_set(dirs, monkeypatch):
    """The stage 2 set gets the parent's table copied in before the trainer
    runs; if the trainer fails, that folder must not stay behind looking
    like a set someone trained."""
    import sys
    data, models = dirs
    t = tab(dirs)
    t.stage.set_value(2)
    t.picker.set_checked([data / "s3"])
    t.fixed.setChecked(True)
    t.name.setText("clf_fail")
    monkeypatch.setattr(t, "command", lambda out: [sys.executable, "-c",
                                                   "import sys; print('boom'); sys.exit(3)"])
    assert t.train()
    assert (models / "clf_fail" / "lut_model.bin").exists()      # copied before the run
    wait(t)
    assert t.banner.tone() == "err" and "exit code 3" in t.banner.text()
    assert not (models / "clf_fail").exists() and "boom" in t.log.toPlainText()
    assert t.last_set is None


def test_state_round_trip(dirs):
    data, _ = dirs
    t = tab(dirs)
    t.picker.set_checked([data / "red_s2", data / "s3" / "a"])
    t.stage.set_value(2)
    s = QSettings("icarus_blimp", "tcview")
    s.beginGroup("train")
    t.save_state(s)
    t2 = tab(dirs)
    t2.restore_state(s)
    s.endGroup()
    assert t2._stage == 2
    assert sorted(t2.picker.checked_paths()) == sorted([data / "red_s2", data / "s3" / "a"])


def test_main_window_has_the_tab():
    from tcview.__main__ import MainWindow
    w = MainWindow()
    try:
        titles = [w.tabs.tabText(i) for i in range(w.tabs.count())]
        assert "Train" in titles and w.tabs.page(titles.index("Train")) is w.train
    finally:
        w.close()
