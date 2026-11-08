"""The redesigned Blobs tab (docs/GUI_REDESIGN.md §6.4): the thumbnail grid
and its filter, labelling the selection, the inspector and its copy texts
(§7), and that it fits a 1024 px screen. Samples are synthetic, so no camera
and no saved frames are needed."""
import numpy as np
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from tcview import blobset as B
from tcview import detector as D
from tcview import protocol as P
from tcview.blobs_tab import BIG, THUMB_DEFAULT, BlobsTab
from tcview.ui import theme


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


def sample(i, label=B.UNLABELLED, source="live"):
    crop = np.full((P.CROP, P.CROP), (i * 997) & 0xFFFF, np.uint16)
    feat = (np.arange(D.BLOB_FEAT_N, dtype=np.float32) + 1) * (0.1 + i)
    return B.Sample(feat, crop, label, source, "t%d" % (1000 + i), 1000 + i, i % 4, 1,
                    80.25 + i, 50.75, 100 + i, (10, 20, 30, 40), (8, 18, 24), -1.0,
                    "c2ba88a-dirty")


@pytest.fixture
def tab(app):
    t = BlobsTab()
    t.add([sample(i) for i in range(6)])
    t.set_label([1, 2], B.BALLOON)
    t.set_label([3], B.NOT_BALLOON)
    t.dirty = False
    t._refresh_counts()
    t.resize(1300, 800)
    t.show()
    QTest.qWaitForWindowExposed(t)
    t.activateWindow()
    QTest.qWaitForWindowActive(t)
    yield t
    t.close()


def visible_rows(t):
    return [r for r in range(t.grid.count()) if not t.grid.item(r).isHidden()]


def select(t, rows, current=None):
    t.grid.clearSelection()
    for r in rows:
        t.grid.item(r).setSelected(True)
    if current is not None:
        t.grid.setCurrentItem(t.grid.item(current),
                              t.grid.selectionModel().SelectionFlag.NoUpdate)
        t._show_selected()


def test_counts_bar_and_filter(tab):
    assert tab.count_lbl.text() == "6 blobs"
    assert tab.mix_lbl.text() == "2 balloon · 1 not · 3 ?"
    assert [b.text() for b in tab.filter_seg.buttons.values()] == [
        "All 6", "Balloon 2", "Not 1", "? 3"]
    assert tab.count_text() == "6 blobs: 2 balloon, 1 not, 3 ?"
    assert not tab.unsaved.isVisible()
    tab.filter_seg.set_value(B.UNLABELLED)
    assert visible_rows(tab) == [0, 4, 5]
    tab.filter_seg.set_value(B.BALLOON)
    assert visible_rows(tab) == [1, 2]
    tab.filter_seg.set_value("all")
    assert visible_rows(tab) == list(range(6))


def test_keys_label_the_selection(tab):
    tab.grid.setFocus()
    select(tab, [0, 4])
    QTest.keyClick(tab.grid, Qt.Key.Key_B)
    assert [s.label for s in tab.samples] == [1, 1, 1, 0, 1, -1]
    assert tab.dirty and tab.unsaved.isVisible() and tab.count_text().endswith("(unsaved)")
    QTest.keyClick(tab.grid, Qt.Key.Key_N)
    assert tab.samples[0].label == B.NOT_BALLOON and tab.samples[4].label == B.NOT_BALLOON
    QTest.keyClick(tab.grid, Qt.Key.Key_U)
    assert tab.samples[0].label == B.UNLABELLED
    # The buttons do the same, and the badge follows the label.
    tab.label_btns[B.BALLOON].click()
    assert tab.grid.item(0).data(Qt.ItemDataRole.UserRole + 1) == B.BALLOON


def test_label_buttons_need_a_selection(tab):
    tab.grid.clearSelection()
    assert not any(b.isEnabled() for b in tab.label_btns.values())
    assert tab.sel_lbl.text() == "0 selected" and tab.title_lbl.text() == "No blob selected"
    assert tab.all_text() == "" and not tab.copy_all.isEnabled()
    select(tab, [2])
    assert all(b.isEnabled() for b in tab.label_btns.values())
    assert tab.sel_lbl.text() == "1 selected"


def test_a_filtered_out_blob_is_unselected(tab):
    """Labelling a '?' blob under the '?' filter hides it, and it leaves the
    selection, so a second key press cannot relabel it unseen."""
    tab.filter_seg.set_value(B.UNLABELLED)
    select(tab, [0, 4])
    tab.set_label(tab.selected(), B.BALLOON)
    assert visible_rows(tab) == [5] and tab.selected() == []
    tab.set_label(tab.selected(), B.NOT_BALLOON)
    assert tab.samples[0].label == B.BALLOON


def test_double_click_cycles(tab):
    it = tab.grid.item(0)
    for want in (B.BALLOON, B.NOT_BALLOON, B.UNLABELLED):
        tab.grid.itemDoubleClicked.emit(it)
        assert tab.samples[0].label == want


def test_inspector_shows_the_last_clicked(tab):
    select(tab, [1, 4, 2], current=4)
    assert tab.title_lbl.text() == "Blob 5 of 6"
    assert tab.multi.isVisible() and tab.multi.text().startswith("3 blobs selected")
    assert tab.rec["source"].text() == "live t1004"
    assert tab.rec["stage"].text() == "1 · conf -1"
    assert tab.rec["fw"].text() == "c2ba88a-dirty · fv 1"
    assert tab.big.pixmap().width() == BIG
    assert tab.label_seg.value() == B.UNLABELLED
    assert tab.feat_table.rowCount() == D.BLOB_FEAT_N
    assert tab.feat_table.item(0, 1).text() == D.feat_names()[0]
    select(tab, [2], current=2)
    assert not tab.multi.isVisible() and tab.label_seg.value() == B.BALLOON


def test_inspector_label_applies_to_the_selection(tab):
    """A mixed selection: pressing the label the shown blob already has still
    labels the others."""
    select(tab, [0, 1], current=1)              # ?, balloon; shows the balloon
    tab.label_seg.buttons[B.BALLOON].click()
    assert tab.samples[0].label == B.BALLOON and tab.samples[1].label == B.BALLOON


def test_copy_texts_keep_the_old_wording(tab):
    select(tab, [3], current=3)
    s = tab.samples[3]
    assert tab.record_text() == "\n".join([
        "live  t1003  blob 3",
        "label     not",
        "stage 1   conf -1   fw c2ba88a-dirty   fv 1",
        "white bal. unrecorded",
        "table id  not recorded",
        "centroid  83.2, 50.8   area 103",
        "box       10,20 - 30,40",
        "crop      x 8  y 18  side 24"])
    head, row = tab.feat_text().split("\n")
    assert head == ",".join(D.feat_names())
    assert row == ",".join("%.4g" % v for v in s.feat)
    assert tab.record_card.copy_text() == tab.record_text()
    assert tab.feat_card.copy_text() == tab.feat_text()
    assert tab.all_text() == tab.record_text() + "\n\n" + tab.feat_text()


def test_feature_cells_copy_as_tsv(tab):
    select(tab, [1], current=1)
    t = tab.feat_table
    t.setFocus()
    t.clearSelection()
    for r in (0, 1):
        for c in (1, 2):
            t.item(r, c).setSelected(True)
    QTest.keySequence(t, "Ctrl+C")
    got = QApplication.clipboard().text()
    assert got == "%s\t%s\n%s\t%s" % (t.item(0, 1).text(), t.item(0, 2).text(),
                                      t.item(1, 1).text(), t.item(1, 2).text())


def test_thumbnail_size_and_no_wheel(tab):
    assert tab.size_slider.value() == THUMB_DEFAULT and tab.size_lbl.text() == "72 px"
    cell = tab.grid.gridSize()
    tab.size_slider.setValue(128)
    assert tab.grid.gridSize().width() == cell.width() + 56 and tab.size_lbl.text() == "128 px"
    s = tab.size_slider
    pos = s.rect().center()
    ev = QWheelEvent(pos.toPointF(), s.mapToGlobal(pos).toPointF(), QPoint(), QPoint(0, -120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(s, ev)
    assert s.value() == 128


def test_collect_and_check_pills(tab):
    assert tab.collect_chip.text() == "camera: unknown" and not tab.collect_btn.isChecked()
    tab.on_cfg({"collect": 1})
    assert tab.collect_btn.isChecked() and tab.collect_chip.tone() == "ok"
    assert tab.collect_chip.text().startswith("camera: collecting")
    tab.on_cfg({"collect": 0})
    assert tab.collect_chip.text() == "camera: not collecting"
    tab.on_disconnected()
    assert tab.collect_chip.text() == "camera: unknown"
    assert tab.agree_chip.text() == "board = PC: not checked"
    tab.agree.checked, tab.agree.feat_equal, tab.agree.crop_equal = 3, 3, 3
    tab._refresh_agreement()
    assert tab.agree_chip.tone() == "ok" and tab.agree_chip.text() == "board = PC ✓"
    tab.agree.feat_equal = 2
    tab._refresh_agreement()
    assert tab.agree_chip.tone() == "warn" and "features equal in 2" in tab.agree_lbl.text()
    assert tab.agree_copy.copy() == QApplication.clipboard().text() == (
        "board != PC\n3 records checked against the PC library: features equal in 2, crops "
        "identical in 3; worst feature difference 0 (relative)")


def test_save_clears_unsaved_and_clear_empties(tab, tmp_path):
    tab.set_label([0], B.BALLOON)
    assert tab.unsaved.isVisible()
    tab.save_to(tmp_path / "s.npz")
    assert not tab.unsaved.isVisible() and not tab.dirty
    back = B.load(tmp_path / "s.npz")
    assert [s.label for s in back] == [s.label for s in tab.samples]
    tab.clear()
    assert tab.grid.count() == 0 and tab.count_lbl.text() == "0 blobs"
    assert tab.title_lbl.text() == "No blobs yet"


def test_fits_1024(app):
    t = BlobsTab()
    t.add([sample(i) for i in range(40)])
    t.resize(1000, 520)
    t.show()
    QTest.qWaitForWindowExposed(t)
    app.processEvents()
    assert t.minimumSizeHint().width() <= 1000
    assert t.split.widget(1).width() >= 380
    assert t.grid.height() >= 150
    t.close()
