"""Model sets, data sessions and the evaluation engine behind the Train,
Verify and Firmware tabs (tcview/library.py, evaluation.py, jobs.py)."""
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from tcview import detector as D
from tcview import evaluation as E
from tcview import jobs as J
from tcview import labels as L
from tcview import library as Lib

HOST = Path(__file__).resolve().parents[2]
TESTDATA = HOST / "testdata"
sys.path.insert(0, str(HOST / "train"))
import train_clf  # noqa: E402


def sessions(*names):
    return [Lib.session(TESTDATA / n) for n in names]


def firmware():
    return Lib.load_set(Lib.FIRMWARE_MODELS, firmware=True)


def set_with_clf(tmp_path, bias, threshold=0.5, name="s"):
    """A model set: the firmware's table plus a classifier that believes
    everything (bias +30) or nothing (-30)."""
    fw = firmware()
    out = Lib.new_set(name, fw, "lut", tmp_path)
    n = D.BLOB_FEAT_N
    mdl = train_clf.Model(np.zeros(n), np.ones(n), np.zeros(n), bias, 1.0)
    (out / "clf_model.bin").write_bytes(train_clf.record(mdl, threshold, fw.lut_id, 42))
    (out / "clf_model.json").write_text(json.dumps(dict(train_id=42, lut_id=fw.lut_id,
                                                        threshold=threshold)))
    return Lib.load_set(out)


# ---- library ---------------------------------------------------------------------

def test_paths_point_at_target_cam():
    assert (Lib.TARGET_CAM / "main" / "tc_pipeline.c").exists()
    assert Lib.TRAIN_LUT.exists() and Lib.TRAIN_CLF.exists()
    assert Lib.rel(TESTDATA / "red_s1") == "host/testdata/red_s1"
    assert Lib.resolve("host/testdata/red_s1") == (TESTDATA / "red_s1").resolve()


def test_a_session_is_a_folder_with_everything_below_it(tmp_path):
    # Two subfolders of one capture session, one frame each.
    for sub, src in (("a", "red_s1"), ("b", "red_s2")):
        d = tmp_path / "s" / sub
        d.mkdir(parents=True)
        stem = L.list_frames(TESTDATA / src)[0]
        for suf in (".rgb565", ".json", ".labels.png"):
            shutil.copy(str(stem) + suf, d / (stem.name + suf))
    s = Lib.session(tmp_path / "s")
    assert (s.n_frames, s.n_labelled, s.is_blobset) == (2, 2, False)
    tree = Lib.data_tree(tmp_path)
    assert [c.path.name for c in tree.children] == ["s"]
    assert tree.n_frames == 2 and [c.path.name for c in tree.children[0].children] == ["a", "b"]
    assert Lib.overlapping([tmp_path / "s", tmp_path / "s" / "a"]) == \
        [((tmp_path / "s").resolve(), (tmp_path / "s" / "a").resolve())]
    assert Lib.data_tree(tmp_path / "missing") is None


def test_firmware_set_and_its_stages(tmp_path):
    fw = firmware()
    assert fw.firmware and fw.name == "firmware (main/models)"
    assert fw.lut_id == D.models().lut_id
    # main/models has the placeholder classifier: stage 1 only.
    assert fw.stages == [1] and not fw.has("clf")
    two = set_with_clf(tmp_path, 30.0)
    assert two.stages == [1, 2] and two.ids() == {"lut": fw.lut_id, "clf": 42}
    # A classifier behind another table is not offered.
    meta = json.loads((two.path / "clf_model.json").read_text())
    meta["lut_id"] = fw.lut_id + 1
    (two.path / "clf_model.json").write_text(json.dumps(meta))
    assert Lib.load_set(two.path).stages == [1]
    assert "NOT USED" in Lib.load_set(two.path).summary("clf")


def test_find_sets_lists_firmware_first(tmp_path):
    set_with_clf(tmp_path, 30.0, name="one")
    (tmp_path / "empty").mkdir()
    sets = Lib.find_sets(tmp_path)
    assert sets[0].firmware and [s.name for s in sets[1:]] == ["one"]


def test_names(tmp_path):
    assert Lib.check_name("") and Lib.check_name("a b") and Lib.check_name(".x")
    assert Lib.check_name("lut_1") == ""
    (tmp_path / "lut_1").mkdir()
    assert "already exists" in Lib.check_name("lut_1", tmp_path)
    s = sessions("red_s1", "red_s2")
    n1 = Lib.suggest_name(1, s, models_dir=tmp_path, now=5.5 * 86400)
    assert n1.startswith("lut_19700106_red_s1-red_s2") and Lib.check_name(n1, tmp_path) == ""
    (tmp_path / n1).mkdir()
    assert Lib.suggest_name(1, s, models_dir=tmp_path, now=5.5 * 86400) == n1 + "_2"
    assert Lib.suggest_name(2, s, models_dir=tmp_path, now=5.5 * 86400).startswith("clf_")


def test_new_set_copies_the_parent_up_to_a_stage(tmp_path):
    two = set_with_clf(tmp_path, 30.0, name="parent")
    out = Lib.new_set("child", two, "lut", tmp_path)
    assert sorted(p.name for p in out.iterdir()) == ["lut_model.bin", "lut_model.h",
                                                     "lut_model.json"]
    out = Lib.new_set("child2", two, "clf", tmp_path)
    assert {"clf_model.bin", "lut_model.bin"} <= {p.name for p in out.iterdir()}
    with pytest.raises(ValueError):
        Lib.new_set("child", two, "lut", tmp_path)


# ---- evaluation ------------------------------------------------------------------

def test_stage_1_totals_are_eval_frames_numbers():
    ss = sessions("red_s1", "red_s2")
    res = E.evaluate(E.Candidate(firmware(), 1), ss)
    want = E.Totals()
    n = 0
    for s in ss:
        for stem in L.list_frames(s.path):
            sf = L.load_frame(stem)
            lab = L.load_labels(sf.label_path, sf.frame.w, sf.frame.h)
            sc = L.score_frame(sf, lab, 1)
            n += 1
            for k in ("tp", "fn", "fp", "split", "px_tp", "px_fp", "px_fn"):
                setattr(want, k, getattr(want, k) + getattr(sc, k))
    got = res.totals()
    assert got.frames == n == 20
    for k in ("tp", "fn", "fp", "split", "px_tp", "px_fp", "px_fn"):
        assert getattr(got, k) == getattr(want, k), k
    assert set(res.by_session()) == {"host/testdata/red_s1", "host/testdata/red_s2"}
    assert "blobs         TP" in res.text()


def test_stage_0_uses_each_frames_settings():
    res = E.evaluate(E.Candidate(None, 0), sessions("red_s2"))
    assert res.label.startswith("stage 0") and res.totals().frames == 13
    assert all(f.score.stage_eff == 0 for f in res.frames)


def test_stage_2_keeps_pixels_and_drops_blobs(tmp_path):
    ss = sessions("red_s1", "red_s2")
    one = E.evaluate(E.Candidate(firmware(), 1), ss)
    yes = E.evaluate(E.Candidate(set_with_clf(tmp_path, 30.0, name="y"), 2), ss)
    no = E.evaluate(E.Candidate(set_with_clf(tmp_path, -30.0, name="n"), 2), ss)
    t1, ty, tn = one.totals(), yes.totals(), no.totals()
    assert (ty.tp, ty.fp, ty.px_tp) == (t1.tp, t1.fp, t1.px_tp)
    assert tn.tp == 0 and tn.fp == 0 and tn.fn == t1.balloons and tn.px_tp == t1.px_tp
    assert all(b.conf == 1.0 for f in yes.frames for b in f.blobs)
    # A stage the set does not offer is refused, not silently run lower.
    with pytest.raises(D.ModelError, match="cannot run stage 2"):
        E.evaluate(E.Candidate(firmware(), 2), ss)


def test_judge_and_trained_on():
    ss = sessions("red_s1", "red_s2", "red_s3", "red_s4")
    res = E.evaluate(E.Candidate(firmware(), 1), ss)
    v = E.judge(res)
    # The firmware table was trained on these four: the verdict says so.
    # (by path, or by name for a table from before paths were recorded)
    assert len(res.trained_on()) == 4
    t = res.totals()
    assert v.ok == (t.fn == 0 and t.false_b1 == 0)
    assert v.tone in ("ok", "info", "warn", "err") and v.text().startswith(v.title)


def test_compare_with_itself_is_a_tie_and_nothing_beats_a_false_blob_1(tmp_path):
    ss = sessions("red_s2")
    a = E.evaluate(E.Candidate(firmware(), 1), ss)
    c = E.compare(a, a)
    assert c.winner == "tie" and c.differing_frames() == []
    # Believing nothing removes every false blob 1 but loses every balloon:
    # the hard rule does not apply (FN rose), so it is a trade-off.
    b = E.evaluate(E.Candidate(set_with_clf(tmp_path, -30.0), 2), ss)
    c = E.compare(a, b)
    if a.totals().false_b1:
        assert c.winner == "inconclusive"
    assert "A = " in c.text()


def test_one_frame_on_a_small_set_is_inconclusive():
    def res(false_b1_frames):
        frames = []
        for i in range(6):
            sc = L.FrameScore("f%d" % i, 1, 0, 1 if i < false_b1_frames else 0,
                              fp=1 if i < false_b1_frames else 0,
                              b1="false" if i < false_b1_frames else "none")
            frames.append(E.FrameEval(Lib.Session(Path("s"), 6, 6).key, None, None, None, [], sc))
        r = E.EvalResult(E.Candidate(None, 0), [Lib.Session(Path("s"), 6, 6)], frames)
        return r
    c = E.compare(res(1), res(0))
    assert c.winner == "inconclusive" and "could reverse it" in c.text()


# ---- jobs --------------------------------------------------------------------------

def test_training_commands_are_portable(tmp_path):
    out = Lib.MODELS_DIR / "x"
    ss = sessions("red_s1", "red_s2")
    cmd = J.lut_command(out, ss, sigma=8, k=7)
    assert cmd[1] == str(Lib.TRAIN_LUT)
    assert cmd[2:] == ["--out", "models/x", "--sigma", "8", "--k", "7",
                       "host/testdata/red_s1", "host/testdata/red_s2"]
    cmd = J.clf_command(out, ss, max_lost=0.2)
    assert cmd[2:6] == ["--lut", "models/x/lut_model.bin", "--out", "models/x"]
    assert "--max-lost" in cmd and cmd[-1] == "host/testdata/red_s2"
    assert J.pretty(cmd).startswith("python host/train/train_clf.py --lut models/x/")


def test_process_job_runs_a_trainer_end_to_end(tmp_path):
    """Train a table through the job runner on one session with fixed
    parameters; the set it makes offers stage 1 and evaluates."""
    from PyQt6.QtCore import QEventLoop, QTimer
    out = Lib.new_set("t", None, None, tmp_path)
    job = J.ProcessJob()
    result = {}
    loop = QEventLoop()
    job.finished.connect(lambda code, text: (result.update(code=code, text=text), loop.quit()))
    QTimer.singleShot(120000, loop.quit)
    job.start(J.lut_command(out, sessions("red_s2"), sigma=8, k=7, min_evidence=0.5))
    loop.exec()
    assert result.get("code") == 0, result.get("text")
    s = Lib.load_set(out)
    assert s.stages == [1] and s.session_paths("lut") == [(TESTDATA / "red_s2").resolve()]
    res = E.evaluate(E.Candidate(s, 1), sessions("red_s2"))
    assert res.trained_on() == ["host/testdata/red_s2 (lut)"]
