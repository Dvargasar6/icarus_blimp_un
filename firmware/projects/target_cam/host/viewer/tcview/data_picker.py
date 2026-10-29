"""
DataPicker: the data folders as a tree with check boxes, for the Train and
Verify tabs.

A checked folder is one session: every frame below it (library.py). Checking a
folder unchecks the folders above and below it, so no frame is counted in two
sessions; checking data/train/s3 is how its colour subfolders become one
session. Blob sets (.npz) are listed as leaves when the caller accepts them
(stage 2 training), greyed out otherwise. Folders with no labelled frame cannot
be checked: no trainer or score can use them.

Each row shows its frames, how many are labelled, and the white-balance gains
its frames were taken with, because sessions with different gains have
colours that are not comparable (learned-detector report §11); the line under
the tree says whether the checked sessions mix gains.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QFileDialog, QHBoxLayout, QPushButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from . import library as Lib
from .ui import theme
from .ui.widgets import Banner, ElidedLabel

PATH_ROLE = Qt.ItemDataRole.UserRole
COLS = ("Folder", "Frames", "Labelled", "White balance")


class DataPicker(QWidget):
    changed = pyqtSignal()

    def __init__(self, root: Path = Lib.DATA_DIR, blobsets: bool = False, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self._blobsets = blobsets
        self._nodes: dict = {}          # path -> DataNode (folders) or None (blob sets)
        self._busy = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.root_lbl = ElidedLabel("", mono=True)
        self.open_btn = QPushButton("Open…")
        self.open_btn.setToolTip("Choose another data folder")
        self.open_btn.clicked.connect(self._choose_root)
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.clicked.connect(self.rescan)
        top.addWidget(self.root_lbl, 1)
        top.addWidget(self.open_btn)
        top.addWidget(self.rescan_btn)
        lay.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLS))
        self.tree.setHeaderLabels(COLS)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(140)
        self.tree.itemChanged.connect(self._item_changed)
        lay.addWidget(self.tree, 1)

        self.summary = Banner("neutral")
        lay.addWidget(self.summary)
        self.rescan()

    # ---- building ----------------------------------------------------------------

    def set_root(self, root: Path):
        self.root = Path(root)
        self.rescan()

    def set_blobsets(self, on: bool):
        """Accept blob sets (stage 2) or not; unaccepted ones are unchecked."""
        self._blobsets = on
        self._busy = True
        for it in self._items():
            p = it.data(0, PATH_ROLE)
            if p and Path(p).suffix == ".npz":
                self._enable(it, on)
                if not on:
                    it.setCheckState(0, Qt.CheckState.Unchecked)
        self._busy = False
        self._refresh_summary()
        self.changed.emit()

    def rescan(self):
        checked = self.checked_paths()
        self._busy = True
        self.tree.clear()
        self._nodes = {}
        self.root_lbl.setText(Lib.rel(self.root) if self.root.is_relative_to(Lib.TARGET_CAM)
                               else str(self.root))
        tree = Lib.data_tree(self.root)
        if tree is None:
            self.summary.set("err", "%s does not exist." % self.root)
        else:
            for c in tree.children:
                self.tree.addTopLevelItem(self._item(c))
            for p in tree.blobsets:
                self.tree.addTopLevelItem(self._blob_item(p))
            self.tree.expandToDepth(0)
        for i in range(len(COLS)):
            self.tree.resizeColumnToContents(i)
        self._busy = False
        self.set_checked(checked)

    def _item(self, node: Lib.DataNode) -> QTreeWidgetItem:
        it = QTreeWidgetItem([node.path.name, str(node.n_frames), str(node.n_labelled),
                              Lib.gains_summary(node.gains)])
        it.setData(0, PATH_ROLE, str(node.path))
        self._nodes[str(node.path)] = node
        for col in (1, 2):
            it.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        it.setFont(3, theme.mono_font())
        if len(node.gains) > 1:
            it.setForeground(3, theme.qcolor("warn_text"))
        it.setCheckState(0, Qt.CheckState.Unchecked)
        self._enable(it, node.n_labelled > 0)
        if node.n_labelled == 0 and node.n_frames:
            it.setToolTip(0, "No labelled frame here: label them in the Label frames tab")
        for c in node.children:
            it.addChild(self._item(c))
        for p in node.blobsets:
            it.addChild(self._blob_item(p))
        return it

    def _blob_item(self, p: Path) -> QTreeWidgetItem:
        try:
            s = Lib.session(p)
            n, lab = str(s.n_frames), str(s.n_labelled)
        except Exception:                       # unreadable: listed, never usable
            n, lab = "?", "?"
        it = QTreeWidgetItem([p.name, n, lab, "blob set"])
        it.setData(0, PATH_ROLE, str(p))
        it.setToolTip(0, "A blob set (.npz) from the Blobs tab: classifier training only")
        it.setCheckState(0, Qt.CheckState.Unchecked)
        self._nodes[str(p)] = None
        self._enable(it, self._blobsets and lab not in ("0", "?"))
        return it

    @staticmethod
    def _enable(it: QTreeWidgetItem, on: bool):
        """Checkable or not. Never by disabling the item: Qt greys out and
        locks a disabled item's children too, and a folder with no labelled
        frame of its own can still hold usable blob sets or subfolders."""
        f = it.flags() | Qt.ItemFlag.ItemIsEnabled
        it.setFlags((f | Qt.ItemFlag.ItemIsUserCheckable) if on
                    else (f & ~Qt.ItemFlag.ItemIsUserCheckable))
        for col in range(3):
            # Usable rows keep the palette's text colour, whatever the theme.
            it.setData(col, Qt.ItemDataRole.ForegroundRole,
                       None if on else theme.qcolor("text_faint"))

    @staticmethod
    def _usable(it: QTreeWidgetItem) -> bool:
        return bool(it.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def _items(self):
        out = []

        def walk(it):
            out.append(it)
            for i in range(it.childCount()):
                walk(it.child(i))
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return out

    # ---- checking ------------------------------------------------------------------

    def _item_changed(self, it: QTreeWidgetItem, col: int):
        if self._busy or col != 0:
            return
        if it.checkState(0) == Qt.CheckState.Checked:
            # One session per frame: no checked folder above or below this one.
            self._busy = True
            p = it.parent()
            while p is not None:
                p.setCheckState(0, Qt.CheckState.Unchecked)
                p = p.parent()

            def clear(x):
                for i in range(x.childCount()):
                    x.child(i).setCheckState(0, Qt.CheckState.Unchecked)
                    clear(x.child(i))
            clear(it)
            self._busy = False
        self._refresh_summary()
        self.changed.emit()

    def checked_paths(self) -> list:
        return [Path(it.data(0, PATH_ROLE)) for it in self._items()
                if it.checkState(0) == Qt.CheckState.Checked]

    def set_checked(self, paths):
        want = {str(Path(p)) for p in paths}
        self._busy = True
        for it in self._items():
            p = it.data(0, PATH_ROLE)
            on = p in want and self._usable(it)
            it.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            if on:
                x = it.parent()
                while x is not None:
                    x.setExpanded(True)
                    x = x.parent()
        self._busy = False
        self._refresh_summary()
        self.changed.emit()

    def sessions(self) -> list:
        return [Lib.session(p) for p in self.checked_paths()]

    def selection_gains(self) -> Counter:
        g = Counter()
        for p in self.checked_paths():
            node = self._nodes.get(str(p))
            if node is not None:
                g.update(node.gains)
        return g

    def _refresh_summary(self):
        ps = self.checked_paths()
        if not ps:
            self.summary.set("neutral", "Check one folder per capture session. A folder "
                             "includes everything below it.")
            return
        frames = sum(n.n_labelled for n in (self._nodes.get(str(p)) for p in ps) if n)
        sets = sum(1 for p in ps if p.suffix == ".npz")
        g = self.selection_gains()
        text = "%d session(s): %d labelled frames%s." % (
            len(ps), frames, ", %d blob set(s)" % sets if sets else "")
        if len(g) > 1:
            text += " White balance is MIXED (%s): colours are not comparable between " \
                    "these sessions." % ", ".join(g)
            self.summary.set("warn", text)
            return
        if g:
            text += " White balance: %s." % next(iter(g))
        self.summary.set("neutral", text)

    def _choose_root(self):
        d = QFileDialog.getExistingDirectory(self, "Data folder", str(self.root))
        if d:
            self.set_root(Path(d))
            self.changed.emit()
