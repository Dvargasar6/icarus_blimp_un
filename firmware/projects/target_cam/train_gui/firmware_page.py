"""
Firmware: make a table (and its classifier) the camera's, then build, flash
and check. README "Retraining the colour table", steps 5 and 6, one button
each, in order:

  1. Instalar   back up main/models into tables/firmware_antes_<date>, copy
                the table in (install.install). Asked for with a dialog,
                since it changes what the next build flies.
  2. Compilar   idf.py build. The page then checks that the image holds the
                installed records (install.build_has), from the bytes.
  3. Flashear   esptool at 115200 with BOOT held, the routine that works on
                the ESP32-CAM-MB. The user does the physical part; the page
                says what, and refuses to flash an image that does not hold
                the installed table.
  4. Comprobar  after releasing BOOT and replugging: asks the camera for
                #!CFG and compares its lut_id / clf with main/models.

Undo is step 1 with the backup chosen as the table. Step 7 of the README
(copying the sessions into host/testdata so the viewer tests can rebuild the
committed table) stays manual; the page says so after installing.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QVBoxLayout, QWidget)

from tcview.ui import theme
from tcview.ui.widgets import Banner, CollapsibleSection, GripSplitter, value_label

from . import install, jobs, paths
from .widgets import LogView, TablePicker, caption, scroll_column

FLASH_STEPS = ("Antes de flashear: mantén pulsado BOOT en la ESP32-CAM-MB, desconecta y "
               "vuelve a conectar el USB sin soltar BOOT. Pulsa «3. Flashear» con BOOT aún "
               "pulsado. Al terminar, suelta BOOT y reconecta el USB para arrancar el "
               "firmware nuevo. Cierra tcview antes: el puerto debe estar libre.")


class FirmwarePage(QWidget):
    tables_changed = pyqtSignal()       # a backup table was written

    def __init__(self, store: QSettings, parent=None, confirm=None):
        super().__init__(parent)
        self.store = store
        # confirm(title, text) -> bool; a dialog by default, replaceable in tests
        self.confirm = confirm or self._ask
        self.job = jobs.TrainJob(self)
        self.job.output.connect(lambda s: self.log.append_text(s))
        self.job.finished.connect(self._job_finished)
        self.job_kind = ""
        self.query: jobs.CameraQuery | None = None

        split = GripSplitter(Qt.Orientation.Horizontal)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(split)

        form = QWidget()
        ll = QVBoxLayout(form)
        ll.setContentsMargins(12, 12, 8, 4)
        ll.setSpacing(10)

        sec = CollapsibleSection("Firmware actual", "main/models y la última compilación")
        self.current_lbl = value_label("", wrap=True)
        sec.add_widget(self.current_lbl)
        self.build_lbl = value_label("", wrap=True)
        theme.set_prop(self.build_lbl, "muted", True)
        sec.add_widget(self.build_lbl)
        ll.addWidget(sec)

        sec = CollapsibleSection("1. Tabla principal", "la que se compila y flashea")
        self.table = TablePicker(store, "firmware/table")
        sec.add_widget(self.table)
        self.check_lbl = value_label("", wrap=True)
        sec.add_widget(self.check_lbl)
        self.install_btn = QPushButton("1. Instalar en main/models")
        theme.set_role(self.install_btn, "primary")
        self.install_btn.clicked.connect(self._install)
        sec.add_widget(self.install_btn)
        hint = QLabel("La tabla actual se respalda primero en tables/firmware_antes_<fecha>. "
                      "Para deshacer, elige ese respaldo aquí e instálalo.")
        hint.setWordWrap(True)
        theme.set_prop(hint, "faint", True)
        sec.add_widget(hint)
        ll.addWidget(sec)

        sec = CollapsibleSection("2. Compilar")
        self.build_btn = QPushButton("2. Compilar (idf.py build)")
        self.build_btn.clicked.connect(self._build)
        sec.add_widget(self.build_btn)
        ll.addWidget(sec)

        sec = CollapsibleSection("3. Flashear", "ESP32-CAM-MB, con BOOT pulsado")
        sec.add_widget(caption("Puerto"))
        prow = QHBoxLayout()
        self.port = QComboBox()
        self.port.setEditable(True)
        theme.mono(self.port)
        prow.addWidget(self.port, 1)
        b = QPushButton("Buscar")
        theme.set_prop(b, "small", True)
        b.clicked.connect(self._ports)
        prow.addWidget(b)
        sec.add_layout(prow)
        steps = QLabel(FLASH_STEPS)
        steps.setWordWrap(True)
        sec.add_widget(steps)
        self.flash_btn = QPushButton("3. Flashear")
        self.flash_btn.clicked.connect(self._flash)
        sec.add_widget(self.flash_btn)
        ll.addWidget(sec)

        sec = CollapsibleSection("4. Comprobar la cámara")
        hint = QLabel("Con el firmware nuevo arrancado (BOOT suelto, USB reconectado), pide a "
                      "la cámara sus ajustes (#!CFG) sin reiniciarla y compara la tabla y el "
                      "clasificador con main/models.")
        hint.setWordWrap(True)
        theme.set_prop(hint, "faint", True)
        sec.add_widget(hint)
        self.query_btn = QPushButton("4. Comprobar")
        self.query_btn.clicked.connect(self._query)
        sec.add_widget(self.query_btn)
        ll.addWidget(sec)
        ll.addStretch(1)

        left = QWidget()
        fl = QVBoxLayout(left)
        fl.setContentsMargins(0, 0, 8, 12)
        fl.addWidget(scroll_column(form, 340), 1)
        split.addWidget(left)
        split.setChildrenCollapsible(False)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 12, 12, 12)
        rl.setSpacing(8)
        t = QLabel("Salida")
        t.setFont(theme.ui_font(13, QFont.Weight.DemiBold))
        rl.addWidget(t)
        self.result = Banner("info", "Elige la tabla principal y sigue los pasos 1 a 4.")
        rl.addWidget(self.result)
        self.log = LogView()
        rl.addWidget(self.log, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.stop_btn = QPushButton("Detener")
        theme.set_role(self.stop_btn, "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.job.stop)
        row.addWidget(self.stop_btn)
        rl.addLayout(row)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([460, 800])
        self.split = split

        self.port.setEditText(str(store.value("firmware/port", "/dev/ttyACM0")))
        self.table.changed.connect(self.refresh)
        self._ports(keep=True)
        self.refresh()

    # ---- state -----------------------------------------------------------------

    def reload_tables(self):
        self.table.reload()

    def refresh(self, *_):
        now = install.installed()
        same = (" = tables/%s" % now.same_as) if now.same_as else ""
        clf = {"none": "sin clasificador", "ok": "clasificador id %d" % now.clf_id,
               "mismatch": "clasificador id %d de OTRA tabla (la cámara no lo usará)"
                           % now.clf_id,
               "bad": "clasificador dañado"}[now.clf_state]
        self.current_lbl.setText("main/models: tabla id %d%s · %s" % (now.lut_id, same, clf))
        has = install.build_has()
        self.build_lbl.setText({
            None: "Sin compilación todavía (build/target_cam.bin no existe).",
            True: "La última compilación contiene esta tabla%s: lista para flashear." % (
                " y su clasificador" if now.clf_state == "ok" else ""),
            False: "La última compilación NO contiene lo que hay en main/models: compila "
                   "antes de flashear."}[has])
        t = self.table.current()
        why = install.check_installable(t) if t else "No hay tablas."
        if t is not None and not why:
            self.check_lbl.setText("Lista para instalar: %s; %s." % (
                "id %s" % t.train_id, t.clf_summary()))
            theme.set_prop(self.check_lbl, "muted", True)
        else:
            self.check_lbl.setText(why)
        busy = self.job.running() or self.query is not None
        self.install_btn.setEnabled(not why and not busy)
        tool = install.toolchain_problem()
        self.build_btn.setEnabled(not busy and not tool)
        self.flash_btn.setEnabled(not busy and not tool and has is True)
        self.query_btn.setEnabled(not busy)
        if tool:
            self.build_btn.setToolTip(tool)
        self.stop_btn.setEnabled(self.job.running())

    def _ports(self, keep: bool = False):
        cur = self.port.currentText()
        self.port.clear()
        ports = install.serial_ports()
        self.port.addItems(ports)
        if keep or cur:
            self.port.setEditText(cur if cur else (ports[0] if ports else "/dev/ttyACM0"))

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    # ---- 1. install --------------------------------------------------------------

    def _install(self):
        t = self.table.current()
        if t is None or install.check_installable(t):
            self.refresh()
            return
        now = install.installed()
        text = ("Copiar %s a main/models%s.\n\nLa tabla actual (id %d) %s.\n\n"
                "Después hay que compilar y flashear." % (
                    t.name, " con su clasificador" if t.has_clf
                    else " (sin clasificador: la etapa 2 queda desactivada)",
                    now.lut_id, "ya está en tables/%s" % now.same_as if now.same_as
                    else "se respalda en tables/firmware_antes_<fecha>"))
        if not self.confirm("Instalar tabla en el firmware", text):
            return
        try:
            r = install.install(t)
        except (OSError, ValueError, RuntimeError) as e:
            self.result.set("err", "No se instaló: %s" % e)
            self.refresh()
            return
        self.log.append_text("\n# instalada %s en main/models (tabla id %d%s); %s\n" % (
            t.name, r.lut_id, ", clasificador id %d" % r.clf_id if r.clf_id else "",
            r.backup_note))
        self.result.set("ok", "Instalada %s; %s. Siguiente: 2. Compilar. Antes de hacer "
                        "commit, copia sus sesiones a host/testdata/ y corre los tests del "
                        "viewer (README, paso 7)." % (t.name, r.backup_note))
        if r.backup is not None:
            self.tables_changed.emit()
        self.refresh()

    # ---- 2. build, 3. flash --------------------------------------------------------

    def _build(self):
        self.job_kind = "build"
        cmd = install.build_command()
        self.log.append_text("\n$ %s\n" % " ".join(cmd))
        self.result.set("info", "Compilando… (la primera vez tarda unos minutos)")
        self.job.start(cmd)
        self.refresh()

    def _flash(self):
        port = self.port.currentText().strip()
        if not port:
            self.result.set("warn", "Escribe el puerto (p. ej. /dev/ttyACM0).")
            return
        if install.build_has() is not True:
            self.result.set("err", "La compilación no contiene la tabla instalada: compila "
                            "primero.")
            self.refresh()
            return
        if not self.confirm("Flashear", FLASH_STEPS + "\n\n¿BOOT está pulsado y el USB "
                            "reconectado?"):
            return
        self.store.setValue("firmware/port", port)
        self.job_kind = "flash"
        cmd = install.flash_command(port)
        self.log.append_text("\n$ (cd build && %s)\n" % " ".join(cmd))
        self.result.set("info", "Flasheando por %s a 115200…" % port)
        self.job.start(cmd, cwd=install.BUILD_DIR)
        self.refresh()

    def _job_finished(self, code: int, text: str):
        kind, self.job_kind = self.job_kind, ""
        if kind == "build":
            has = install.build_has()
            if code == 0 and has:
                self.result.set("ok", "Compilado; la imagen contiene la tabla instalada. "
                                "Siguiente: 3. Flashear.")
            elif code == 0:
                self.result.set("err", "Compiló, pero la imagen no contiene la tabla de "
                                "main/models. Revisa la salida.")
            else:
                self.result.set("err", "La compilación falló (código %d). Revisa la salida." % code)
        elif kind == "flash":
            if code == 0 and "Hash of data verified" in text:
                self.result.set("ok", "Flasheado y verificado. Suelta BOOT, reconecta el USB "
                                "y pulsa 4. Comprobar.")
            elif code == 0:
                self.result.set("warn", "esptool terminó sin error pero no informó la "
                                "verificación. Revisa la salida.")
            else:
                self.result.set("err", "El flasheo falló (código %d). ¿BOOT estaba pulsado al "
                                "reconectar? ¿tcview tiene el puerto abierto? Vuelve a "
                                "intentarlo; la tabla instalada no cambia." % code)
        self.refresh()

    # ---- 4. check ------------------------------------------------------------------

    def _query(self):
        port = self.port.currentText().strip()
        if not port:
            return
        self.store.setValue("firmware/port", port)
        self.result.set("info", "Preguntando a la cámara en %s (hasta 15 s)…" % port)
        self.query = jobs.CameraQuery(port, self)
        self.query.done.connect(self._query_done)
        self.query.failed.connect(self._query_failed)
        self.query.start()
        self.refresh()

    def _query_done(self, cfg: dict):
        self.query = None
        tone, text = install.camera_verdict(cfg, install.installed())
        self.log.append_text("\n# %s\n" % text)
        self.result.set(tone, text)
        self.refresh()

    def _query_failed(self, msg: str):
        self.query = None
        self.result.set("err", "No se pudo leer la cámara: %s. ¿Soltaste BOOT y reconectaste? "
                        "¿Otro programa tiene el puerto?" % msg)
        self.refresh()
