"""Table names, the verdict rules, and that the scores match eval_frames.py."""
from pathlib import Path

import numpy as np
import pytest

from tcview import labels as L
from train_gui import engine as E
from train_gui import paths

TESTDATA = paths.TARGET_CAM / "host" / "testdata"


# ---- names -----------------------------------------------------------------------

def S(name, group="train"):
    return paths.Session(Path("/x") / group / name, group, 1, 1)


def test_table_name_follows_the_pattern():
    assert paths.table_name(3, [S("red_s5"), S("red_s7"), S("orange_s5")]) == \
        "tabla_3_r_5-7_o_5_p_0"
    assert paths.table_name(1, [S("pink_s6")]) == "tabla_1_r_0_o_0_p_6"
    assert paths.table_name(2, [S("red_s1"), S("scene1")]) == "tabla_2_r_1_o_0_p_0_x_scene1"


def test_check_table_name(tmp_path):
    assert paths.check_table_name("prueba_awb2.v1", tmp_path) == ""
    assert paths.check_table_name("", tmp_path)
    for bad in ("con espacio", "a/b", "../x", ".oculta", "-x", "tabla_ñ"):
        assert paths.check_table_name(bad, tmp_path), bad
    (tmp_path / "ya").mkdir()
    assert "Ya existe" in paths.check_table_name("ya", tmp_path)


def test_next_table_number_skips_used_numbers(tmp_path):
    assert paths.next_table_number(tmp_path) == 1
    (tmp_path / "tabla_4_r_1_o_0_p_0").mkdir()
    (tmp_path / "lut_before").mkdir()
    assert paths.next_table_number(tmp_path) == 5


def test_find_sessions_groups_by_parent(tmp_path):
    for g, n in (("train", "red_s1"), ("test", "orange_s2"), ("train/Without_AWB", "red_s1")):
        d = tmp_path / g / n
        d.mkdir(parents=True)
        (d / "a.rgb565").write_bytes(b"\0" * 4)
        (d / "a.json").write_text("{}")
    (tmp_path / "train" / "red_s1" / "a.labels.png").write_bytes(b"")
    ss = paths.find_sessions(tmp_path)
    assert [(s.group, s.name, s.n_labelled) for s in ss] == \
        [("train", "red_s1", 1), ("train/Without_AWB", "red_s1", 0), ("test", "orange_s2", 0)]
    # Same name, different folders: the key keeps them apart.
    assert [s.key for s in ss] == ["train/red_s1", "train/Without_AWB/red_s1", "test/orange_s2"]


# ---- verdicts, on made-up scores ------------------------------------------------

def frame(session, name, n_bal, tp=0, fn=0, fp=0, b1="none", px=(10, 0, 0)):
    sc = L.FrameScore(name, 1, n_bal, tp + fp, tp, fn, fp, 0, b1, *px)
    return E.FrameEval(session, None, None, None, [], sc)


def result(table_sessions, frames, chosen=("s1",)):
    t = paths.Table(Path("/t"), meta={"sessions": list(table_sessions)})
    return E.EvalResult(t, [S(n) for n in chosen], frames)


def test_judge_fails_on_a_false_blob_1_and_on_a_miss():
    good = result([], [frame("s1", "a", 1, tp=1, b1="balloon"), frame("s1", "e", 0)])
    assert E.judge(good).ok
    miss = result([], [frame("s1", "a", 1, fn=1)])
    v = E.judge(miss)
    assert not v.ok and "FN" in v.reasons[0]
    false = result([], [frame("s1", "a", 1, tp=1, b1="balloon"), frame("s1", "e", 0, fp=1, b1="false")])
    v = E.judge(false)
    assert not v.ok and v.tone == "err" and "blob 1 falso" in v.reasons[0]


def test_judge_warns_when_scored_on_training_data():
    r = result(["s1"], [frame("s1", "a", 1, tp=1, b1="balloon")])
    assert "optimistas" in E.judge(r).caveats[0]


def test_compare_prefers_the_table_without_a_false_blob_1():
    a = result([], [frame("s1", "a", 1, tp=1, b1="balloon"), frame("s1", "e", 0, fp=1, b1="false")])
    b = result([], [frame("s1", "a", 1, tp=1, b1="balloon"), frame("s1", "e", 0)])
    assert E.compare(a, b).winner == "B"
    assert E.compare(b, a).winner == "A"


def test_compare_is_inconclusive_when_figures_move_both_ways():
    a = result([], [frame("s1", "a", 1, tp=1, b1="balloon"), frame("s1", "b", 1, fn=1),
                    frame("s1", "e", 0)])
    b = result([], [frame("s1", "a", 1, tp=1, b1="balloon"),
                    frame("s1", "b", 1, tp=1, b1="balloon"), frame("s1", "e", 0, fp=1, b1="false")])
    c = E.compare(a, b)
    # B finds the missed balloon but puts a false blob 1: A keeps the hard
    # rule, B keeps the balloon. Not comparable.
    assert c.winner == "inconclusive"
    same = E.compare(a, result([], list(a.frames)))
    assert same.winner == "tie"


def test_compare_b_better_when_strictly_better_somewhere_and_nowhere_worse():
    a = result([], [frame("s1", "a", 1, tp=1, fp=2, b1="balloon")])
    b = result([], [frame("s1", "a", 1, tp=1, fp=0, b1="balloon")])
    c = E.compare(a, b)
    assert c.winner == "B" and "FP 2 → 0" in c.reasons[0]


# ---- the real pipeline ------------------------------------------------------------

@pytest.mark.skipif(not (TESTDATA / "red_s1").exists(), reason="no testdata")
def test_evaluate_matches_eval_frames_on_the_committed_table():
    fw = paths.load_table(paths.FIRMWARE_MODELS, firmware=True)
    assert fw is not None
    s = paths.find_sessions(TESTDATA)
    s1 = next(x for x in s if x.name == "red_s1")
    res = E.evaluate(fw, [s1])
    # The same numbers eval_frames.py would print: score every frame by hand.
    from tcview import detector as D
    m = D.models(fw.bin)
    want = [L.score_frame(L.load_frame(st), L.load_labels(Path(str(st) + L.LABEL_SUFFIX), 160, 120),
                          1, None, m) for st in L.list_frames(s1.path)]
    got = [f.score for f in res.frames]
    assert [(w.tp, w.fn, w.fp, w.b1, w.px_tp, w.px_fp, w.px_fn) for w in want] == \
        [(g.tp, g.fn, g.fp, g.b1, g.px_tp, g.px_fp, g.px_fn) for g in got]
    # The committed table's .json predates session_paths: matched by name,
    # and the report says so.
    assert res.trained_on() == ["red_s1 (por nombre)"]
    fw.meta = {**fw.meta, "session_paths": [str(TESTDATA / "red_s2")]}
    assert res.trained_on() == []            # by path: red_s1 was not used
    f = res.frames[0]
    assert E.mask_overlay(f.mask).shape == (120, 160, 4)
    assert all(len(b) >= 6 for b in E.boxes(f, "#0f0", "#f00", "#00f"))


# ---- the window opens -------------------------------------------------------------

def test_main_window_opens_and_names_a_table():
    from PyQt6.QtWidgets import QApplication
    from tcview.ui import theme
    from train_gui.__main__ import MainWindow
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    win = MainWindow()
    win.show()
    app.processEvents()
    keys = [s.key for s in win.train.sessions.sessions if s.n_labelled]
    if keys:
        win.train.sessions.set_checked(keys[:1])
        app.processEvents()
        assert win.train.table_name().startswith("tabla_")      # the suggestion
        # one session and "search" cannot go together: the button says why
        assert not win.train.train_btn.isEnabled()
        win.train.fixed.setChecked(True)
        app.processEvents()
        assert win.train.train_btn.isEnabled()
        # a typed name replaces the suggestion, in the command too
        win.train.name_edit.setText("mi_tabla_awb2")
        app.processEvents()
        assert win.train.table_name() == "mi_tabla_awb2"
        assert "tables/mi_tabla_awb2 " in win.train.cmd_lbl.text()
        win.train.name_edit.setText("mal nombre")
        app.processEvents()
        assert not win.train.train_btn.isEnabled() and win.train.banner.isVisible()
        win.train.name_edit.clear()
    assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == \
        ["Entrenar", "Verificar", "Comparar", "Clasificador", "Firmware"]
    win.close()
    app.processEvents()
