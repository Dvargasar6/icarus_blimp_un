"""Entrenamiento y verificación de la tabla de color: python -m train_gui --help"""
from __future__ import annotations

import argparse
import sys

from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QTabWidget

from tcview.ui import persist, theme

from .clf_page import ClfPage
from .compare_page import ComparePage
from .firmware_page import FirmwarePage
from .train_page import TrainPage
from .verify_page import VerifyPage


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("target_cam · tabla de color")
        self.store = QSettings("icarus_blimp", "train_gui")
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.train = TrainPage(self.store)
        self.verify = VerifyPage(self.store)
        self.compare = ComparePage(self.store)
        self.clf = ClfPage(self.store)
        self.firmware = FirmwarePage(self.store)
        self.tabs.addTab(self.train, "Entrenar")
        self.tabs.addTab(self.verify, "Verificar")
        self.tabs.addTab(self.compare, "Comparar")
        self.tabs.addTab(self.clf, "Clasificador")
        self.tabs.addTab(self.firmware, "Firmware")
        self.setCentralWidget(self.tabs)
        for page in (self.train, self.clf, self.firmware):
            page.tables_changed.connect(self._tables_changed)
        self.train.trained.connect(self._verify_new)
        self.clf.trained.connect(self._verify_clf)
        # main/models may change on another tab (or by hand): look again
        # whenever the Firmware tab is shown.
        self.tabs.currentChanged.connect(
            lambda i: self.firmware.refresh() if self.tabs.widget(i) is self.firmware else None)
        self._splitters = {
            "train/split": self.train.split,
            "verify/split": self.verify.split, "verify/hsplit": self.verify.hsplit,
            "compare/split": self.compare.split, "compare/hsplit": self.compare.hsplit,
            "clf/split": self.clf.split, "firmware/split": self.firmware.split,
        }
        for key, sp in self._splitters.items():
            persist.restore_splitter(self.store, key, sp)
        i = persist.read_int(self.store, "tab", 0, self.tabs.count() - 1)
        if i is not None:
            self.tabs.setCurrentIndex(i)

    def _tables_changed(self):
        self.verify.reload_tables()
        self.compare.reload_tables()
        self.clf.reload_tables()
        self.firmware.reload_tables()

    def _verify_new(self, name: str):
        self.verify.select_table(name)
        self.compare.select_b(name)
        self.tabs.setCurrentWidget(self.verify)

    def _verify_clf(self, name: str):
        """A classifier was trained into tables/<name>: verify the table with
        it, and line up Compare as "the table alone" against "with it"."""
        self.verify.select_table(name, 2)
        self.compare.table_a.select(name, 1)
        self.compare.select_b(name, 2)
        self.tabs.setCurrentWidget(self.verify)

    def closeEvent(self, e):
        for key, sp in self._splitters.items():
            persist.save_splitter(self.store, key, sp)
        persist.save_geometry(self.store, "geometry", self)
        self.store.setValue("tab", self.tabs.currentIndex())
        self.train.job.stop()
        self.clf.job.stop()
        self.firmware.job.stop()
        super().closeEvent(e)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m train_gui", description=__doc__)
    ap.add_argument("--quit-after", type=float, metavar="S",
                    help="cerrar a los S segundos (pruebas)")
    args = ap.parse_args(argv)
    app = QApplication(sys.argv[:1])
    theme.apply(app)
    win = MainWindow()
    if not persist.restore_geometry(win.store, "geometry", win, 1500, 950):
        win.resize(1500, 950)
    win.show()
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), win.close)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
