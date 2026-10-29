"""The Verify tab (tcview/verify_tab.py): one model or two, the same set at
two stages, the frame list and its filters, the report, and the hand-off
from Train."""
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PyQt6.QtCore import QSettings

from tcview import detector as D
from tcview import evaluation as E
from tcview import library as Lib
from tcview import verify_tab as V

HOST = Path(__file__).resolve().parents[2]
TESTDATA = HOST / "testdata"


@pytest.fixture
def dirs(tmp_path):
    import sys
    sys.path.insert(0, str(HOST / "train"))
    import train_clf
    data = tmp_path / "data"
    for n in ("red_s2", "red_s3"):
        shutil.copytree(TESTDATA / n, data / n)
    models = tmp_path / "models"
    fw = Lib.load_set(Lib.FIRMWARE_MODELS, firmware=True)
    out = Lib.new_set("none_pass", fw, "lut", models)
    n = D.BLOB_FEAT_N
    mdl = train_clf.Model(np.zeros(n), np.ones(n), np.zeros(n), -30.0, 1.0)
    (out / "clf_model.bin").write_bytes(train_clf.record(mdl, 0.5, fw.lut_id, 9))
    (out / "clf_model.json").write_text(json.dumps(dict(train_id=9, lut_id=fw.lut_id,
                                                        threshold=0.5)))
    return data, models


def tab(dirs):
    data, models = dirs
    return V.VerifyTab(None, data, models, Lib.FIRMWARE_MODELS)


def test_what_evaluate_needs(dirs):
    data, _ = dirs
    t = tab(dirs)
    assert "Check at least one test folder." in t.problems()
    t.picker.set_checked([data / "red_s2"])
    assert t.problems() == [] and t.eval_btn.isEnabled()
    t.compare.setChecked(True)
    assert t.sec_b.isVisibleTo(t)


def test_verify_opens_the_set_against_its_table_alone(dirs):
    _, models = dirs
    t = tab(dirs)
    t.verify(models / "none_pass")
    ca, cb = t.pick_a.candidate(), t.pick_b.candidate()
    assert (ca.mset.name, ca.stage) == ("none_pass", 2)
    assert (cb.mset.name, cb.stage) == ("none_pass", 1)
    assert t.compare.isChecked()


def test_one_model_hsv(dirs):
    data, _ = dirs
    t = tab(dirs)
    t.pick_a.select(V.HSV)
    assert t.pick_a.candidate().stage == 0
    t.picker.set_checked([data / "red_s2"])
    assert t.evaluate()
    t.wait()
    assert t.res_a is not None and t.res_b is None
    assert t.listw.count() == t.res_a.totals().frames == 13
    assert t.totals.rows()[0][:2] == ["frames", "13"]
    assert not t.filter.buttons["diff"].isEnabled()
    assert "stage 0" in t.report() and t.banner.tone() in ("ok", "info", "warn", "err")


def test_compare_two_stages_and_draw_frames(dirs):
    data, models = dirs
    t = tab(dirs)
    t.verify(models / "none_pass")
    t.picker.set_checked([data / "red_s2", data / "red_s3"])
    assert t.evaluate()
    t.wait()
    ta, tb = t.res_a.totals(), t.res_b.totals()
    # A believes no blob: no TP and no FP; B is the table alone.
    assert ta.tp == 0 and ta.fp == 0 and tb.tp > 0
    assert t.cmp is not None and "A = none_pass · stage 2" in t.report()
    assert len(t.sessions.rows()) == 2
    n_all = t.listw.count()
    t.filter.set_value("diff")
    assert 0 < t.listw.count() <= n_all
    t.which.set_value("b")
    t.listw.setCurrentRow(0)
    fa, fb = t._rows[0]
    assert t.view.boxes == E.boxes(fb, *[V.theme.TOKENS[k] for k in
                                         ("ok_dot", "err_dot", "label_balloon")])
    assert "A: " in t.frame_info.text() and "B: " in t.frame_info.text()
    t.filter.set_value("pass")
    assert all(a.outcome() == "ok" and b.outcome() == "ok" for a, b in t._rows)


def test_state_round_trip(dirs):
    data, models = dirs
    t = tab(dirs)
    t.verify(models / "none_pass")
    t.picker.set_checked([data / "red_s3"])
    s = QSettings("icarus_blimp", "tcview")
    s.beginGroup("verify")
    t.save_state(s)
    t2 = tab(dirs)
    t2.restore_state(s)
    s.endGroup()
    assert t2.compare.isChecked() and t2.pick_b.candidate().stage == 1
    assert t2.picker.checked_paths() == [data / "red_s3"]


def test_train_hands_a_new_set_to_verify(dirs):
    from tcview.__main__ import MainWindow
    _, models = dirs
    w = MainWindow()
    try:
        w.verify.pick_a.models_dir = models
        w.verify.pick_b.models_dir = models
        w.train.last_set = models / "none_pass"
        w.train.verify_requested.emit(models / "none_pass")
        assert w.tabs.current_page() is w.verify
        assert w.verify.pick_a.candidate().mset.name == "none_pass"
    finally:
        w.close()
