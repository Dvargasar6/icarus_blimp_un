"""
Clasificador: train the stage 2 blob classifier for one table.

A classifier belongs to the table it was trained behind (main/blob_clf.h: the
features are computed on the pixels the table accepts), so the page starts
from a table, and the classifier is written into that table's folder as
clf_model.{bin,h,json}, beside its lut_model.*. The firmware's table is
never written here: training for it first copies its lut_model.* to
tables/firmware_id<id>/, and the classifier goes there. Installing either into
the firmware is the Firmware page's job.

A table that already has a classifier keeps it unless "Reemplazar" is
ticked: the old one is renamed clf_model_prev.*, not deleted.

The sessions can be frame folders, blob sets (.npz from tcview's Blobs tab)
or both. A blob set is checked here as train_clf.py will check it, so the
button says why before the trainer does: its blobs must have been found
behind this table, and blobs from stage 0 (HSV) need the explicit
"Permitir etapa 0" box.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox, QGridLayout,
                             QHBoxLayout, QLabel, QPushButton, QRadioButton, QVBoxLayout,
                             QWidget)

from tcview.ui import theme
from tcview.ui.widgets import Banner, CollapsibleSection, GripSplitter, value_label

from . import jobs, paths
from .widgets import LogView, SessionPicker, TablePicker, caption, scroll_column

# train_clf.py has no defaults to fall back on; these are the middle of its
# search grid.
DEFAULT_L2, DEFAULT_THRESHOLD = 1.0, 0.5


class ClfPage(QWidget):
    tables_changed = pyqtSignal()
    trained = pyqtSignal(str)           # "verify this table at stage 2": its folder name

    def __init__(self, store: QSettings, parent=None):
        super().__init__(parent)
        self.store = store
        self.job = jobs.TrainJob(self)
        self.job.output.connect(self.log_append)
        self.job.finished.connect(self._finished)
        self.out_dir: Path | None = None

        split = GripSplitter(Qt.Orientation.Horizontal)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(split)

        form = QWidget()
        ll = QVBoxLayout(form)
        ll.setContentsMargins(12, 12, 8, 4)
        ll.setSpacing(10)
        sec = CollapsibleSection("Tabla", "detrás de la cual trabaja")
        self.table = TablePicker(store, "clf/table")
        sec.add_widget(self.table)
        ll.addWidget(sec)

        sec = CollapsibleSection("Sesiones de entrenamiento")
        self.sessions = SessionPicker(store, "clf/sessions", blobsets=True)
        sec.add_widget(self.sessions, 1)
        hint = QLabel("Carpetas de fotos: cada blob que la tabla encuentra es un ejemplo, globo "
                      "si su centro cae en un globo pintado, falso si no. Conjuntos «(blobs)»: "
                      "los .npz de la pestaña Blobs de tcview, etiquetados a mano; deben "
                      "recogerse con esta tabla en la cámara y !stage 1. Hacen falta blobs "
                      "falsos (globos de otro color, puertas, lámparas). Con dos o más "
                      "sesiones se buscan l2 y el umbral dejando una fuera a la vez.")
        hint.setWordWrap(True)
        theme.set_prop(hint, "faint", True)
        sec.add_widget(hint)
        self.allow0 = QCheckBox("Permitir blobs de etapa 0 (HSV)")
        self.allow0.setToolTip("Solo para probar: en vuelo la etapa 2 trabaja detrás de la "
                               "tabla, que encuentra otros blobs que el HSV")
        self.allow0.setVisible(False)
        sec.add_widget(self.allow0)
        ll.addWidget(sec, 1)

        par = CollapsibleSection("Parámetros")
        self.auto = QRadioButton("Buscar (leave-one-session-out)")
        self.fixed = QRadioButton("Fijar:")
        self.auto.setChecked(True)
        par.add_widget(self.auto)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.addWidget(self.fixed, 0, 0, 1, 2)
        self.l2 = self._spin(0.001, 100, 0.1, DEFAULT_L2, 3)
        self.threshold = self._spin(0.01, 0.99, 0.05, DEFAULT_THRESHOLD, 2)
        for r, (name, w, tip) in enumerate((
                ("l2", self.l2, "Penalización de los pesos: más alta, modelo más simple."),
                ("umbral", self.threshold, "Un blob se conserva si su confianza es ≥ umbral. "
                 "Más alto descarta más blobs falsos y también más globos.")), start=1):
            lbl = QLabel(name)
            lbl.setToolTip(tip)
            w.setToolTip(tip)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(w, r, 1)
        grid.setColumnStretch(2, 1)
        par.add_layout(grid)
        ll.addWidget(par)

        out = CollapsibleSection("Salida")
        out.add_widget(caption("Se escribe en"))
        self.out_lbl = value_label("", wrap=True)
        theme.set_prop(self.out_lbl, "muted", True)
        out.add_widget(self.out_lbl)
        # Short: a check box does not wrap, and a long one would widen the
        # whole column past the window.
        self.replace = QCheckBox("Reemplazar el clasificador existente")
        self.replace.setToolTip("El anterior se guarda como clf_model_prev.* en la misma carpeta")
        out.add_widget(self.replace)
        out.add_widget(caption("Comando equivalente"))
        self.cmd_lbl = value_label("", wrap=True)
        theme.set_prop(self.cmd_lbl, "muted", True)
        out.add_widget(self.cmd_lbl)
        row = QHBoxLayout()
        b = QPushButton("Copiar comando")
        theme.set_prop(b, "small", True)
        b.clicked.connect(lambda: QApplication.clipboard().setText(self.cmd_lbl.text()))
        row.addWidget(b)
        row.addStretch(1)
        out.add_layout(row)
        ll.addWidget(out)

        left = QWidget()
        fl = QVBoxLayout(left)
        fl.setContentsMargins(0, 0, 8, 12)
        fl.setSpacing(8)
        fl.addWidget(scroll_column(form), 1)
        self.banner = Banner("info", "")
        self.banner.setVisible(False)
        self.banner.setContentsMargins(12, 0, 0, 0)
        fl.addWidget(self.banner)
        btns = QHBoxLayout()
        btns.setContentsMargins(12, 0, 0, 0)
        self.train_btn = QPushButton("Entrenar clasificador")
        theme.set_role(self.train_btn, "primary")
        self.train_btn.setMinimumHeight(34)
        self.train_btn.clicked.connect(self._train)
        self.stop_btn = QPushButton("Detener")
        theme.set_role(self.stop_btn, "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.job.stop)
        btns.addWidget(self.train_btn, 1)
        btns.addWidget(self.stop_btn)
        fl.addLayout(btns)
        split.addWidget(left)
        split.setChildrenCollapsible(False)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 12, 12, 12)
        rl.setSpacing(8)
        t = QLabel("Salida del entrenador")
        t.setFont(theme.ui_font(13, QFont.Weight.DemiBold))
        rl.addWidget(t)
        self.result = Banner("info", "Elige una tabla y sesiones, y pulsa Entrenar "
                             "clasificador.")
        rl.addWidget(self.result)
        self.log = LogView()
        rl.addWidget(self.log, 1)
        after = QHBoxLayout()
        self.verify_btn = QPushButton("Verificar tabla + clasificador →")
        self.verify_btn.setEnabled(False)
        self.verify_btn.clicked.connect(lambda: self.trained.emit(self.out_dir.name))
        after.addStretch(1)
        after.addWidget(self.verify_btn)
        rl.addLayout(after)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([460, 800])
        self.split = split

        for w in (self.auto, self.fixed, self.replace, self.allow0):
            w.toggled.connect(self._refresh)
        for w in (self.l2, self.threshold):
            w.valueChanged.connect(self._refresh)
        self.sessions.changed.connect(self._refresh)
        self.table.changed.connect(self._refresh)
        self._refresh()

    @staticmethod
    def _spin(lo, hi, step, value, decimals) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(decimals)
        s.setValue(value)
        theme.mono(s)
        return s

    def reload_tables(self):
        self.table.reload()

    # ---- state -----------------------------------------------------------------

    def _params(self):
        if self.auto.isChecked():
            return None, None
        return self.l2.value(), self.threshold.value()

    def out_folder(self) -> Path | None:
        """Where the classifier goes: the table's own folder, or for the
        firmware's table a copy of it under tables/."""
        t = self.table.current()
        if t is None:
            return None
        return paths.TABLES_DIR / paths.firmware_copy_name(t) if t.firmware else t.path

    def _refresh(self, *_):
        t = self.table.current()
        sel = [s for s in self.sessions.checked() if s.n_labelled]
        fixed = self.fixed.isChecked()
        for w in (self.l2, self.threshold):
            w.setEnabled(fixed)
        out = self.out_folder()
        has = out is not None and (out / "clf_model.bin").exists()
        self.replace.setVisible(has)
        if out is not None:
            try:
                rel = out.relative_to(paths.TARGET_CAM)
            except ValueError:          # tables/ moved elsewhere (the tests do)
                rel = out
            self.out_lbl.setText("%s/clf_model.{bin,h,json}%s" % (
                rel, "  (copia de la tabla del firmware; main/models no se toca)"
                if t.firmware else ""))
        sets_problem = self._sets_problem(t, sel)
        problem = ""
        if t is None:
            problem = "No hay ninguna tabla."
        elif not sel:
            problem = "Elige al menos una carpeta con fotogramas etiquetados."
        elif len(sel) < 2 and not fixed:
            problem = ("Con una sola sesión no se pueden buscar los parámetros: elige «Fijar» "
                       "o añade otra sesión.")
        elif sets_problem:
            problem = sets_problem
        elif has and not self.replace.isChecked():
            problem = ("%s ya tiene un clasificador. Marca «Reemplazar» para entrenar otro."
                       % out.name)
        self.banner.setVisible(bool(problem))
        self.banner.set("warn", problem)
        self.train_btn.setEnabled(not problem and not self.job.running())
        if t is not None and sel:
            src = t if not t.firmware else paths.Table(out, meta=t.meta)
            self.cmd_lbl.setText(jobs.pretty_command(
                jobs.clf_command(src, out, sel, *self._params(), self._allow0())))
        else:
            self.cmd_lbl.setText("")

    def _allow0(self) -> bool:
        return not self.allow0.isHidden() and self.allow0.isChecked()

    def _sets_problem(self, t, sel) -> str:
        """Why the chosen blob sets cannot train this table's classifier
        (train_clf.py's checks, said before it runs), or ""."""
        sets = [s for s in sel if s.is_blobset]
        stage0 = [s.name for s in sets if s.blobs.stages.get(0)]
        self.allow0.setVisible(bool(stage0))
        if t is None:
            return ""
        other = sorted({(s.name, i) for s in sets for i in s.blobs.lut_ids
                        if i >= 0 and i != t.train_id})
        if other:
            return ("%s se recogió detrás de la tabla %s, no de esta (id %s): elige esa "
                    "tabla." % (other[0][0], other[0][1], t.train_id))
        if stage0 and not self.allow0.isChecked():
            return ("%s: blobs de etapa 0 (HSV). Recógelos con esta tabla y !stage 1, o "
                    "marca «Permitir blobs de etapa 0»." % ", ".join(stage0))
        return ""

    # ---- training ----------------------------------------------------------------

    def _train(self):
        t = self.table.current()
        sel = [s for s in self.sessions.checked() if s.n_labelled]
        out = self.out_folder()
        if t is None or not sel or out is None:
            return
        if t.firmware:
            # The firmware's table, copied out, so its classifier has a
            # table folder of its own and main/models is left alone.
            out.mkdir(parents=True, exist_ok=True)
            for f in ("lut_model.bin", "lut_model.h", "lut_model.json"):
                if (t.path / f).exists() and not (out / f).exists():
                    shutil.copy2(t.path / f, out / f)
            src = paths.load_table(out)
        else:
            src = t
        if (out / "clf_model.bin").exists():
            if not self.replace.isChecked():
                self._refresh()
                return
            for f in ("clf_model.bin", "clf_model.h", "clf_model.json", "clf_train_log.txt"):
                if (out / f).exists():
                    (out / f).replace(out / f.replace("clf_model", "clf_model_prev")
                                      .replace("clf_train_log", "clf_train_log_prev"))
        self.out_dir = out
        cmd = jobs.clf_command(src, out, sel, *self._params(), self._allow0())
        self.log.clear()
        self.log.append_text("$ %s\n\n" % jobs.pretty_command(cmd))
        self.result.set("info", "Entrenando el clasificador de %s…" % out.name)
        self.verify_btn.setEnabled(False)
        self.train_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.job.start(cmd)

    def log_append(self, text: str):
        self.log.append_text(text)

    def _finished(self, code: int, text: str):
        self.stop_btn.setEnabled(False)
        out = self.out_dir
        ok = code == 0 and out is not None and (out / "clf_model.bin").exists()
        if out is not None and out.is_dir():
            try:
                (out / "clf_train_log.txt").write_text(text)
            except OSError:
                pass
        if not ok:
            reason = next((ln for ln in text.splitlines()
                           if ln.startswith(("need at least", "with ", "choosing", "no labelled",
                                             "cannot use", "the threshold"))), "")
            self.result.set("err", "El entrenador terminó sin escribir el clasificador "
                            "(código %d). %s" % (code, reason))
        else:
            tone, msg = judge_log(text)
            data = re.findall(r"DATA WARNING: (.*)", text)
            if data:
                msg += " Ojo con los datos: %s." % "; ".join(data)
                if tone == "ok":
                    tone = "warn" if not all("blob sets hold only" in d for d in data) else tone
            self.result.set(tone, "Clasificador de %s creado. %s" % (out.name, msg))
            self.verify_btn.setEnabled(True)
            self.replace.setChecked(False)
        self.table.reload()
        self._refresh()
        if ok:
            self.tables_changed.emit()


def judge_log(text: str) -> tuple[str, str]:
    """What train_clf.py's output says about the classifier it wrote."""
    warns = re.findall(r"WARNING: (.*)", text)
    base = re.search(r"stage 1 alone on the same frames:\s+cost\s+(\d+)", text)
    best = re.search(r"best first:\n\s+l2\s+\S+\s+threshold\s+(\S+)\s+cost\s+(\d+) = "
                     r"false b1 (\d+)/(\d+) \+ wrong b1 (\d+) \+ FN (\d+)", text)
    if best is None:
        return "info", ("Parámetros fijados, sin búsqueda: no hay cifras de sesiones dejadas "
                        "fuera. Verifícalo en carpetas que no usó.")
    thr, cost, fb1, empty, wb1, fn = best.groups()
    summary = ("En las sesiones dejadas fuera (umbral %s): costo %s frente a %s de la tabla "
               "sola; blob 1 falso %s/%s, blob 1 equivocado %s, globos perdidos %s." % (
                   thr, cost, base.group(1) if base else "?", fb1, empty, wb1, fn))
    if any("does not lower" in w for w in warns):
        return "err", ("No ayuda: con el clasificador el costo no baja frente a la tabla sola. "
                       "Sigue en etapa 1. " + summary)
    if warns:
        es = []
        if any("false blob 1" in w for w in warns):
            es.append("aún hay blob 1 falso en fotogramas sin globo")
        if any("loses balloons" in w for w in warns):
            es.append("pierde globos que la tabla sola encontraba")
        return "warn", "Ayuda, pero %s. %s" % (" y ".join(es), summary)
    return "ok", summary
