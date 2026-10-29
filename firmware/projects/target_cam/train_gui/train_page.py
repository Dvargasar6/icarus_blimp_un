"""
Entrenar: choose sessions, choose how the parameters are set, train.

The output folder is tables/<name>: the name typed in the Name field, or,
left empty, tabla_n_r_m_o_j_p_l suggested from the chosen sessions. It is
never main/models, so the firmware's table is untouched whatever happens. The trainer's output is shown live and saved beside the
table as train_log.txt, and its two warnings (EXCLUDED on the chosen setting,
WARNING: every setting...) are turned into a banner, since they are what
decides whether the table may be flown.
"""
from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QRadioButton, QVBoxLayout, QWidget)

from tcview.ui import theme
from tcview.ui.widgets import Banner, CollapsibleSection, GripSplitter, value_label

from . import jobs, paths
from .widgets import LogView, SessionPicker, caption, scroll_column

# train_lut.py's own defaults when it cannot search (one session): the
# README's "--sigma 8 --k 7".
DEFAULT_SIGMA, DEFAULT_K, DEFAULT_MIN_EVIDENCE = 8.0, 7.0, 0.5


class TrainPage(QWidget):
    tables_changed = pyqtSignal()       # a table was written: pickers must reload
    trained = pyqtSignal(str)           # "verify this table": the new table's folder name

    def __init__(self, store: QSettings, parent=None):
        super().__init__(parent)
        self.store = store
        self.job = jobs.TrainJob(self)
        self.job.output.connect(self._output)
        self.job.finished.connect(self._finished)
        self.out_dir: Path | None = None

        split = GripSplitter(Qt.Orientation.Horizontal)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(split)

        # ---- left: what to train on, and how --------------------------------
        # The sections scroll; the banner and the buttons stay below them,
        # always in view.
        form = QWidget()
        ll = QVBoxLayout(form)
        ll.setContentsMargins(12, 12, 8, 4)
        ll.setSpacing(10)
        sec = CollapsibleSection("Sesiones de entrenamiento")
        self.sessions = SessionPicker(store, "train/sessions")
        sec.add_widget(self.sessions, 1)
        hint = QLabel("Cada carpeta es una sesión de captura. Con dos o más, el entrenador "
                      "elige sigma, k y min_evidence dejando una sesión fuera a la vez; con "
                      "una sola hay que fijarlos.")
        hint.setWordWrap(True)
        theme.set_prop(hint, "faint", True)
        sec.add_widget(hint)
        ll.addWidget(sec, 1)

        par = CollapsibleSection("Parámetros")
        self.auto = QRadioButton("Buscar (leave-one-session-out)")
        self.fixed = QRadioButton("Fijar:")
        self.auto.setChecked(True)
        par.add_widget(self.auto)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.addWidget(self.fixed, 0, 0, 1, 2)
        self.sigma = self._spin(0, 32, 1, DEFAULT_SIGMA)
        self.k = self._spin(1, 100, 1, DEFAULT_K)
        self.min_ev = self._spin(0, 20, 0.5, DEFAULT_MIN_EVIDENCE)
        for r, (name, w, tip) in enumerate((
                ("sigma", self.sigma, "Anchura del suavizado gaussiano sobre la rejilla RGB565, "
                 "en unidades de 8 bits. 0 = sin suavizar."),
                ("k", self.k, "Un color entra en la tabla si es k veces más probable en globo "
                 "que en fondo."),
                ("min_evidence", self.min_ev, "Píxeles de globo (suavizados) que un color "
                 "necesita para entrar.")), start=1):
            lbl = QLabel(name)
            lbl.setToolTip(tip)
            w.setToolTip(tip)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(w, r, 1)
        grid.setColumnStretch(2, 1)
        par.add_layout(grid)
        ll.addWidget(par)

        out = CollapsibleSection("Salida")
        # Empty = the suggested name, shown as the placeholder; anything typed
        # replaces it. The field is never overwritten by a session change, so
        # a name the user chose survives ticking another folder.
        out.add_widget(caption("Nombre de la tabla"))
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("tables/"))
        self.name_edit = QLineEdit()
        theme.mono(self.name_edit)
        self.name_edit.setClearButtonEnabled(True)
        self.name_edit.setToolTip("Vacío: se usa el nombre sugerido (gris). Letras sin tilde, "
                                  "números, _ - . y sin espacios.")
        name_row.addWidget(self.name_edit, 1)
        self.suggest_btn = QPushButton("Sugerido")
        theme.set_prop(self.suggest_btn, "small", True)
        self.suggest_btn.setToolTip("Escribir el nombre sugerido en el campo, para editarlo")
        self.suggest_btn.clicked.connect(
            lambda: self.name_edit.setText(self.name_edit.placeholderText()))
        name_row.addWidget(self.suggest_btn)
        out.add_layout(name_row)
        self.name_lbl = value_label("")
        theme.set_prop(self.name_lbl, "muted", True)
        out.add_widget(self.name_lbl)
        out.add_widget(caption("Comando equivalente"))
        self.cmd_lbl = value_label("", wrap=True)
        theme.set_prop(self.cmd_lbl, "muted", True)
        out.add_widget(self.cmd_lbl)
        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copiar comando")
        theme.set_prop(self.copy_btn, "small", True)
        self.copy_btn.clicked.connect(self._copy_cmd)
        row.addWidget(self.copy_btn)
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
        fl.addWidget(self.banner)
        btns = QHBoxLayout()
        btns.setContentsMargins(12, 0, 0, 0)
        self.train_btn = QPushButton("Entrenar")
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
        self.banner.setContentsMargins(12, 0, 0, 0)
        split.addWidget(left)
        split.setChildrenCollapsible(False)

        # ---- right: the trainer's output ---------------------------------------
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 12, 12, 12)
        rl.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("Salida del entrenador")
        t.setFont(theme.ui_font(13, QFont.Weight.DemiBold))
        head.addWidget(t)
        head.addStretch(1)
        self.result = Banner("info", "Elige sesiones y pulsa Entrenar.")
        rl.addLayout(head)
        rl.addWidget(self.result)
        self.log = LogView()
        rl.addWidget(self.log, 1)
        after = QHBoxLayout()
        self.verify_btn = QPushButton("Verificar esta tabla →")
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

        for w in (self.auto, self.fixed):
            w.toggled.connect(self._refresh)
        for w in (self.sigma, self.k, self.min_ev):
            w.valueChanged.connect(self._refresh)
        self.sessions.changed.connect(self._refresh)
        self.name_edit.textChanged.connect(self._refresh)
        self._refresh()

    @staticmethod
    def _spin(lo, hi, step, value) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(1)
        s.setValue(value)
        theme.mono(s)
        return s

    # ---- state -----------------------------------------------------------------

    def _params(self):
        if self.auto.isChecked():
            return None, None, None
        return self.sigma.value(), self.k.value(), self.min_ev.value()

    def table_name(self) -> str:
        """The name the table will get: what was typed, else the suggestion."""
        typed = self.name_edit.text().strip()
        return typed or self.name_edit.placeholderText()

    def _refresh(self, *_):
        sel = [s for s in self.sessions.checked() if s.n_labelled]
        fixed = self.fixed.isChecked()
        for w in (self.sigma, self.k, self.min_ev):
            w.setEnabled(fixed)
        self.name_edit.setPlaceholderText(
            paths.table_name(paths.next_table_number(), sel) if sel else "")
        name = self.table_name()
        typed = bool(self.name_edit.text().strip())
        self.name_lbl.setText(("nombre propio" if typed else "nombre sugerido: tabla n, "
                               "sesiones rojas r, naranjas o, rosadas p") if sel else "")
        problem = ""
        if not sel:
            problem = "Elige al menos una carpeta con fotogramas etiquetados."
        elif len(sel) < 2 and not fixed:
            problem = ("Con una sola sesión el entrenador no puede buscar los parámetros: "
                       "elige «Fijar» (el README sugiere sigma 8, k 7) o añade otra sesión.")
        else:
            problem = paths.check_table_name(name)
        self.banner.setVisible(bool(problem))
        self.banner.set("warn", problem)
        self.train_btn.setEnabled(not problem and not self.job.running())
        if sel and paths.NAME_RE.match(name):
            cmd = jobs.train_command(paths.TABLES_DIR / name, sel, *self._params())
            self.cmd_lbl.setText(jobs.pretty_command(cmd))
        else:
            self.cmd_lbl.setText("")

    def _copy_cmd(self):
        QApplication.clipboard().setText(self.cmd_lbl.text())

    # ---- training ----------------------------------------------------------------

    def _train(self):
        sel = [s for s in self.sessions.checked() if s.n_labelled]
        name = self.table_name()
        # Checked again here, not only in _refresh: a folder of that name may
        # have appeared since (another trainer run, a copy by hand).
        if not sel or paths.check_table_name(name):
            self._refresh()
            return
        self.out_dir = paths.TABLES_DIR / name
        cmd = jobs.train_command(self.out_dir, sel, *self._params())
        self.log.clear()
        self.log.append_text("$ %s\n\n" % jobs.pretty_command(cmd))
        self.result.set("info", "Entrenando %s…" % name)
        self.verify_btn.setEnabled(False)
        self.train_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.job.start(cmd)

    def _output(self, text: str):
        self.log.append_text(text)

    def _finished(self, code: int, text: str):
        self.stop_btn.setEnabled(False)
        out = self.out_dir
        ok = code == 0 and out is not None and (out / "lut_model.bin").exists()
        if out is not None and out.is_dir():
            try:
                (out / "train_log.txt").write_text(text)
            except OSError:
                pass
        if not ok:
            self.result.set("err", "El entrenador terminó con error (código %d). No se creó "
                            "ninguna tabla." % code if code else
                            "El entrenador no escribió la tabla.")
        else:
            tone, msg = self._judge_log(text)
            self.result.set(tone, "Tabla %s creada. %s" % (out.name, msg))
            self.verify_btn.setEnabled(True)
            # That name is taken now; the next table starts from a fresh suggestion.
            self.name_edit.clear()
        self._refresh()
        if ok:
            self.tables_changed.emit()

    @staticmethod
    def _judge_log(text: str) -> tuple[str, str]:
        """What the trainer's output says about the table it wrote."""
        if "WARNING: every setting puts a false blob 1" in text:
            return "err", ("ADVERTENCIA del entrenador: todo ajuste pone un blob 1 falso en "
                           "algún fotograma sin globo de las sesiones dejadas fuera. No volar "
                           "esta tabla; revisa las etiquetas de esos fotogramas.")
        fm = re.search(r"final table: (.*)", text)
        detail = fm.group(1) if fm else ""
        m = re.search(r"best first:\n(.*?)\n\n", text, re.S)
        if m is None:
            return "info", ("Parámetros fijados, sin búsqueda: no hay cifras de sesiones "
                            "dejadas fuera. Verifícala en carpetas que no usó. %s" % detail)
        # The search list is best first; the chosen setting is its first entry
        # and its "held out" lines run up to the second "  sigma" line.
        lines = m.group(1).split("\n")
        first, rest = lines[0], []
        for ln in lines[1:]:
            if ln.lstrip().startswith("sigma"):
                break
            rest.append(ln)
        if "EXCLUDED" in first:
            return "err", "El ajuste elegido está EXCLUIDO: pone un blob 1 falso en una sesión dejada fuera."
        held = [re.search(r"FN\s+(\d+) FP\s+\d+ split \d+  false b1 (\d+)/", ln) for ln in rest]
        held = [h for h in held if h]
        fn = sum(int(h.group(1)) for h in held)
        tone = "ok" if fn == 0 else "warn"
        return tone, ("Sin blob 1 falso en las sesiones dejadas fuera; %d globo(s) perdido(s) "
                      "ahí. %s" % (fn, detail))
