"""Labels and their scores: the file format, balloons as components, the
scoring rules eval_frames reports, and the Label tab's painting, on
synthetic saved frames with known answers."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from tcview import detector as D
from tcview import labels as L
from test_detector import frame_with_disc, red

W, H = 160, 120
TOOLS = Path(__file__).resolve().parents[2] / "tools"


def save_frame(folder: Path, name: str, data: bytes, st: D.Settings) -> Path:
    stem = folder / name
    stem.with_suffix(".rgb565").write_bytes(data)
    cfg = {"hue": st.hue, "tol": st.tol, "sat": st.sat, "val": st.val, "area": st.area,
           "be": st.be}
    stem.with_suffix(".json").write_text(json.dumps(
        {"seq": 1, "t_ms": 0, "w": W, "h": H, "fmt": "rgb565be", "camera_cfg": cfg}))
    return stem


def disc_labels(cx, cy, r):
    yy, xx = np.mgrid[0:H, 0:W]
    lab = np.zeros((H, W), np.uint8)
    lab[(xx + 0.5 - cx) ** 2 + (yy + 0.5 - cy) ** 2 <= r * r] = L.BALLOON
    return lab


def test_label_file_round_trip(tmp_path):
    lab = np.random.default_rng(1).integers(0, 3, (H, W), dtype=np.uint8)
    L.save_labels(tmp_path / "a.labels.png", lab)
    assert np.array_equal(L.load_labels(tmp_path / "a.labels.png", W, H), lab)
    # No file: nothing labelled, not an error.
    assert not L.load_labels(tmp_path / "none.labels.png", W, H).any()
    with pytest.raises(ValueError):
        L.load_labels(tmp_path / "a.labels.png", 80, 60)
    with pytest.raises(ValueError):
        L.save_labels(tmp_path / "b.labels.png", lab + 1)


def test_components_are_8_connected_and_largest_first():
    b = np.zeros((H, W), bool)
    b[10:20, 10:20] = True            # 100 px
    b[20, 20] = True                  # touches the square diagonally only
    b[50:53, 50:53] = True            # 9 px, separate
    c = L.components(b)
    assert [x.area for x in c] == [101, 9]
    assert (c[0].x0, c[0].y0, c[0].x1, c[0].y1) == (10, 10, 20, 20)
    assert c[1].cx == pytest.approx(51.5) and c[1].cy == pytest.approx(51.5)
    assert L.components(b, min_px=10)[-1].area == 101


def test_component_centroid_matches_the_c_detector():
    """Same pixel convention as blob_detect: a labelled disc and the
    detected disc have the same centroid."""
    data, _ = frame_with_disc(70.3, 50.6, 15)
    r = D.detect(red(), data)
    c = L.balloons(disc_labels(70.3, 50.6, 15))[0]
    assert r.b[0].cx == pytest.approx(c.cx, abs=0.01)
    assert r.b[0].cy == pytest.approx(c.cy, abs=0.01)


def test_rest_to_background_leaves_the_rim_unlabelled():
    lab = disc_labels(80, 60, 10)
    out = L.rest_to_background(lab, band=2)
    assert (out[lab == L.BALLOON] == L.BALLOON).all()
    assert out[0, 0] == L.BACKGROUND
    # 2 px band: the pixel just outside the disc stays unlabelled, 3 px out is background.
    row = out[60]
    edge = np.flatnonzero(row == L.BALLOON).max()
    assert list(row[edge + 1:edge + 4]) == [L.UNLABELLED, L.UNLABELLED, L.BACKGROUND]
    assert (L.rest_to_background(lab, band=0)[lab != L.BALLOON] == L.BACKGROUND).all()


def test_score_rules():
    lab = L.rest_to_background(disc_labels(50, 50, 12))
    mask = lab == L.BALLOON
    # Found, plus a split fragment in the same balloon, plus one false blob.
    s = L.score("x", lab, [(50, 50), (45, 55), (130, 100)], mask)
    assert (s.n_balloons, s.tp, s.fn, s.fp, s.split, s.b1) == (1, 1, 0, 1, 1, "balloon")
    assert s.px_fp == 0 and s.px_fn == 0
    # Blob 1 false, the balloon missed.
    s = L.score("x", lab, [(130, 100)], np.zeros_like(mask))
    assert (s.tp, s.fn, s.fp, s.b1) == (0, 1, 1, "false")
    # No balloon labelled, nothing reported: a correct empty frame.
    s = L.score("x", L.rest_to_background(np.zeros((H, W), np.uint8)), [], np.zeros_like(mask))
    assert (s.n_balloons, s.tp, s.fn, s.fp, s.b1) == (0, 0, 0, 0, "none")


def make_set(tmp_path):
    """A frame with a red disc (labelled) next to a red square (labelled
    background: the false detection), and an unlabelled frame."""
    st = red()
    data, _ = frame_with_disc(60, 60, 14)
    img = np.frombuffer(data, ">u2").reshape(H, W).copy()
    img[20:34, 120:134] = img[60, 60]                     # same red as the disc
    stem = save_frame(tmp_path, "disc_and_square", img.astype(">u2").tobytes(), st)
    L.save_labels(Path(str(stem) + L.LABEL_SUFFIX), L.rest_to_background(disc_labels(60, 60, 14)))
    save_frame(tmp_path, "not_labelled", data, st)
    return st


def test_score_frame_through_the_pipeline(tmp_path):
    make_set(tmp_path)
    stems = L.list_frames(tmp_path)
    assert [s.name for s in stems] == ["disc_and_square", "not_labelled"]
    sf = L.load_frame(stems[0])
    s = L.score_frame(sf, L.load_labels(sf.label_path, W, H), stage=3, m=D.NO_MODELS)
    assert s.stage_eff == 0                                   # no models: falls back
    assert (s.tp, s.fn, s.fp, s.b1) == (1, 0, 1, "balloon")  # disc is bigger: blob 1
    assert s.px_fn == 0 and s.px_fp == 14 * 14


def test_eval_frames_cli(tmp_path, capsys):
    make_set(tmp_path)
    sys.path.insert(0, str(TOOLS))
    import eval_frames
    assert eval_frames.main([str(tmp_path), "--csv", str(tmp_path / "s.csv")]) == 0
    out = capsys.readouterr().out
    assert "TP 1  FN 0  FP 1  split 0" in out
    assert "1 saved frames not labelled" in out
    assert (tmp_path / "s.csv").read_text().count("\n") == 2
    assert eval_frames.main([str(tmp_path / "empty")]) == 2


def test_label_tab_paint_save_reload(tmp_path):
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    make_set(tmp_path)
    from tcview.label_tab import LabelTab
    tab = LabelTab(tmp_path)
    assert len(tab.stems) == 2 and tab.sf.stem.name == "disc_and_square"
    tab.step(1)                                               # the unlabelled frame
    assert tab.sf.stem.name == "not_labelled" and not tab.lab.any()
    # Ellipse round the disc (box 46..74), rest to background, save by moving on.
    tab._push_undo()
    tab.fill_shape(46, 46, 74, 74, True, L.BALLOON)
    tab.fill_rest()
    assert len(L.balloons(tab.lab)) == 1 and tab.dirty
    painted = tab.lab.copy()
    tab.step(-1)
    assert Path(str(tmp_path / "not_labelled") + L.LABEL_SUFFIX).exists()
    from tcview.label_tab import DONE_ROLE
    assert tab.listw.item(1).data(DONE_ROLE) is True             # the green tick
    tab.step(1)
    assert np.array_equal(tab.lab, painted)
    # Undo takes back the last action only.
    tab.clear()
    tab.do_undo()
    assert np.array_equal(tab.lab, painted)
    # The live score agrees with eval: the disc is found.
    assert "TP 1  FN 0  FP 0" in tab.det_card.copy_text()
    # The brush: a radius-1 dab is the full 3x3.
    tab.radius.setValue(1)
    tab.clear()
    tab.dab(10.5, 10.5, L.BACKGROUND)
    assert (tab.lab == L.BACKGROUND).sum() == 9
    app.processEvents()
