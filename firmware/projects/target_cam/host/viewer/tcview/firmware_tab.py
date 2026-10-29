"""
Firmware: put a model set on the camera, and check that it is there.

  1. Install   main/models/ is moved into models/ (firmware_<lut_id>_<date>,
               unless a set there already holds the same bytes) and the chosen
               set is copied in (install.py). Nothing else writes main/models.
  2. Build     idf.py build, through the ESP-IDF activation script.
  3. Flash     the ESP32-CAM-MB has a physical step (README, Building and
               flashing): the viewer lets go of the port, the operator holds
               BOOT and replugs the USB, esptool writes at 115200 without
               resets, then the operator releases BOOT and replugs again. Each
               physical step waits for the operator's click, because the
               viewer cannot see the button.
  4. Check     the viewer reconnects and compares the camera's #!CFG (lut_id,
               clf, clf_id) with what main/models holds.

The "Installed now" card says at all times what main/models holds, whether
the last build contains it (the records appear verbatim in the image), and
what the connected camera reports, so a mismatch at any step is visible.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                             QVBoxLayout, QWidget)

from . import install as I
from . import jobs as J
from . import library as Lib
from .ui import theme
from .ui.widgets import Banner, CollapsibleSection, CopyButton, GripSplitter, value_label

# Flash steps, in order.
IDLE, CLOSING, HOLD_BOOT, FLASHING, REPLUG, CHECKING = (
    "idle", "closing", "hold_boot", "flashing", "replug", "checking")
STEP_TEXT = {
    CLOSING: "Releasing the serial port…",
    HOLD_BOOT: "Hold the ESP32-CAM-MB's BOOT button, unplug and replug the USB cable, and keep "
               "holding BOOT. Then click \"BOOT held — flash now\".",
    FLASHING: "Flashing at 115200 with BOOT held… keep holding it.",
    REPLUG: "Release BOOT, then unplug and replug the USB cable so the new firmware runs. Then "
            "click \"Reconnect and check\".",
    CHECKING: "Waiting for the camera's #!CFG (it answers after its 5 s reflash window)…",
}


class FirmwareTab(QWidget):
    def __init__(self, main=None, models_dir: Path = Lib.MODELS_DIR,
                 firmware_dir: Path = Lib.FIRMWARE_MODELS, app_bin: Path = I.APP_BIN):
        super().__init__()
        self.main = main
        self.models_dir, self.fw_dir, self.app_bin = Path(models_dir), Path(firmware_dir), app_bin
        self.cfg: dict = {}
        self.step = IDLE
        self.job = J.ProcessJob(self)
        self.job.output.connect(self._output)
        self.job.finished.connect(self._finished)
        self._job_kind = ""
        self.confirm = self._ask            # tests replace it
        self._poll = QTimer(self, interval=200)
        self._poll.timeout.connect(self._poll_tick)

        split = GripSplitter(Qt.Orientation.Horizontal)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(split)
        form = QWidget()
        fl = QVBoxLayout(form)
        fl.setContentsMargins(12, 12, 8, 12)
        fl.setSpacing(10)

        sec = CollapsibleSection("Installed now")
        self.now_lbl = value_label("", wrap=True)
        sec.add_widget(self.now_lbl)
        self.build_banner = Banner("neutral", "")
        sec.add_widget(self.build_banner)
        self.cam_banner = Banner("neutral", "No camera connected.")
        sec.add_widget(self.cam_banner)
        fl.addWidget(sec)

        sec = CollapsibleSection("1 · Install a model set")
        self.set_combo = QComboBox()
        self.set_combo.currentIndexChanged.connect(self._refresh)
        sec.add_widget(self.set_combo)
        self.set_lbl = value_label("", wrap=True)
        sec.add_widget(self.set_lbl)
        row = QHBoxLayout()
        self.install_btn = QPushButton("Install into main/models")
        theme.set_role(self.install_btn, "primary")
        self.install_btn.clicked.connect(self.install)
        row.addWidget(self.install_btn)
        row.addStretch(1)
        sec.add_layout(row)
        self.install_banner = Banner("neutral", "The current main/models is moved into models/ "
                                     "first, so it can be installed back.")
        sec.add_widget(self.install_banner)
        fl.addWidget(sec)

        sec = CollapsibleSection("2 · Build")
        row = QHBoxLayout()
        row.addWidget(QLabel("ESP-IDF activation"))
        self.activate = QLineEdit()
        theme.mono(self.activate)
        found = I.find_activate()
        self.activate.setText(str(found) if found else "")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.activate, 1)
        row.addWidget(browse)
        sec.add_layout(row)
        row = QHBoxLayout()
        self.build_btn = QPushButton("Build")
        theme.set_role(self.build_btn, "primary")
        self.build_btn.clicked.connect(self.build)
        row.addWidget(self.build_btn)
        row.addStretch(1)
        sec.add_layout(row)
        fl.addWidget(sec)

        sec = CollapsibleSection("3 · Flash and check")
        self.port_lbl = value_label("")
        sec.add_widget(self.port_lbl)
        row = QHBoxLayout()
        self.flash_btn = QPushButton("Flash…")
        theme.set_role(self.flash_btn, "primary")
        self.flash_btn.clicked.connect(self.start_flash)
        self.boot_btn = QPushButton("BOOT held — flash now")
        self.boot_btn.clicked.connect(self.flash_now)
        self.check_btn = QPushButton("Reconnect and check")
        self.check_btn.clicked.connect(self.reconnect)
        self.abort_btn = QPushButton("Abort")
        self.abort_btn.clicked.connect(self.abort)
        for b in (self.flash_btn, self.boot_btn, self.check_btn, self.abort_btn):
            row.addWidget(b)
        row.addStretch(1)
        sec.add_layout(row)
        self.step_banner = Banner("neutral", "Install and build first; then Flash… walks you "
                                  "through the BOOT button.")
        sec.add_widget(self.step_banner)
        fl.addWidget(sec)
        self.why = value_label("", mono=False, wrap=True)
        fl.addWidget(self.why)
        fl.addStretch(1)
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setFrameShape(QScrollArea.Shape.NoFrame)
        left.setWidget(form)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 12, 12, 12)
        hdr = QHBoxLayout()
        cap = QLabel("BUILD / FLASH OUTPUT")
        cap.setFont(theme.caption_font())
        hdr.addWidget(cap)
        hdr.addStretch(1)
        self.cmd_lbl = value_label("", wrap=True)
        hdr.addWidget(CopyButton(lambda: self.log.toPlainText(), "Copy"))
        rl.addLayout(hdr)
        rl.addWidget(self.cmd_lbl)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(theme.mono_font())
        self.log.setMaximumBlockCount(20000)
        rl.addWidget(self.log, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        self.activate.textChanged.connect(self._refresh)
        self.reload_sets()

    # ---- what is where -------------------------------------------------------------------

    def port(self) -> str:
        return self.main.bar.port_combo.port() if self.main is not None else ""

    def connected(self) -> bool:
        return self.main is not None and self.main.conn != "off"

    def activation(self) -> Path | None:
        t = self.activate.text().strip()
        return Path(t).expanduser() if t else None

    def reload_sets(self):
        keep = self.set_combo.currentData()
        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        for s in Lib.find_sets(self.models_dir, self.fw_dir):
            if not s.firmware:
                self.set_combo.addItem("%s  (stages %s)" % (s.name, ", ".join(map(str, s.stages))),
                                       str(s.path))
        i = self.set_combo.findData(keep)
        self.set_combo.setCurrentIndex(max(i, 0))
        self.set_combo.blockSignals(False)
        self._refresh()

    def chosen(self) -> Lib.ModelSet | None:
        p = self.set_combo.currentData()
        return Lib.load_set(Path(p)) if p else None

    def installed(self) -> I.Installed:
        return I.installed(self.fw_dir, self.models_dir)

    def _refresh(self, *_):
        inst = self.installed()
        fw = Lib.load_set(self.fw_dir, firmware=True)
        lines = ["main/models: table id %s · classifier %s" % (
            inst.lut_id or "none", "id %d" % inst.clf_id if inst.clf_state == "ok"
            else inst.clf_state)]
        if fw is not None:
            lines += ["  %s: %s" % (k, fw.summary(k)) for k in Lib.KINDS if fw.has(k)]
        lines.append("  saved as models/%s" % inst.same_as if inst.same_as
                     else "  not saved under models/ yet (Install moves it there)")
        self.now_lbl.setText("\n".join(lines))
        has = I.build_has(self.fw_dir, self.app_bin)
        self.build_banner.set(*{None: ("warn", "No build yet: Build before flashing."),
                                True: ("ok", "The last build holds these models."),
                                False: ("warn", "The last build does NOT hold these models: "
                                                "Build again before flashing.")}[has])
        if self.cfg:
            self.cam_banner.set(*I.camera_verdict(self.cfg, inst))
        elif not self.connected():
            self.cam_banner.set("neutral", "No camera connected: its models cannot be read.")
        s = self.chosen()
        self.set_lbl.setText("" if s is None else "\n".join(
            "%s: %s" % (k, s.summary(k)) for k in Lib.KINDS if s.has(k)))
        busy = self.job.running() or self.step not in (IDLE,)
        why = []
        if s is None:
            why.append("No model set under models/ to install.")
        else:
            w = I.check_installable(s)
            if w:
                why.append(w)
        tool = I.toolchain_problem(self.activation())
        self.install_btn.setEnabled(not busy and s is not None and not I.check_installable(s))
        self.build_btn.setEnabled(not busy and not tool)
        port = self.port()
        self.port_lbl.setText("port: %s" % (port or "none chosen (Port, at the top)"))
        self.flash_btn.setEnabled(not busy and not tool and bool(port) and has is not None)
        self.boot_btn.setVisible(self.step == HOLD_BOOT)
        self.check_btn.setVisible(self.step == REPLUG)
        self.abort_btn.setVisible(self.step not in (IDLE,) and not self.job.running())
        if tool:
            why.append(tool)
        self.why.setText(" ".join(why))

    # ---- 1. install ----------------------------------------------------------------------

    def _ask(self, text: str) -> bool:
        return QMessageBox.question(self, "Install model set", text) == \
            QMessageBox.StandardButton.Yes

    def install(self) -> bool:
        s = self.chosen()
        if s is None or I.check_installable(s):
            return False
        inst = self.installed()
        cam = I.camera_ids(self.cfg) if self.cfg else None
        text = ("Install %s into main/models?\n\nThe current main/models (table %s) is moved "
                "into models/ first%s." % (s.name, inst.lut_id,
                                           " (it is already saved as models/%s)" % inst.same_as
                                           if inst.same_as else ""))
        if cam and cam["lut_id"] != inst.lut_id:
            text += ("\n\nNote: the camera reports table %d, not main/models' %d, so the backup "
                     "is not what the camera holds." % (cam["lut_id"], inst.lut_id))
        if not self.confirm(text):
            return False
        try:
            r = I.install(s, self.fw_dir, self.models_dir)
        except (OSError, ValueError, RuntimeError) as e:
            self.install_banner.set("err", "Not installed: %s" % e)
            return False
        self.install_banner.set("ok", "Installed %s: table %d%s. %s. Next: Build, then Flash."
                                % (s.name, r.lut_id, ", classifier %d" % r.clf_id
                                   if r.clf_id else ", no classifier", r.note[0].upper()
                                   + r.note[1:]))
        self.reload_sets()
        if self.main is not None and hasattr(self.main, "verify"):
            self.main.verify.reload_sets()
            self.main.train.reload_sets()
        return True

    # ---- 2. build ------------------------------------------------------------------------

    def build(self) -> bool:
        act = self.activation()
        if self.job.running() or I.toolchain_problem(act):
            return False
        self._run("build", I.build_command(act))
        return True

    def _run(self, kind: str, cmd: list):
        self._job_kind = kind
        self.cmd_lbl.setText("$ " + cmd[-1].split("&& ", 1)[-1])
        self.log.setPlainText("$ %s\n" % cmd[-1])
        self.job.start(cmd)
        self._refresh()

    def _output(self, text: str):
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()

    def _finished(self, code: int, _text: str):
        kind, self._job_kind = self._job_kind, ""
        if kind == "build":
            self.build_banner.set("ok" if code == 0 else "err",
                                  "Build finished." if code == 0 else
                                  "Build failed (exit code %d): see the output." % code)
        elif kind == "flash":
            if code == 0:
                self._set_step(REPLUG)
            else:
                self.step_banner.set("err", "Flashing failed (exit code %d). Was BOOT held "
                                     "while the cable was replugged? Release BOOT, replug, and "
                                     "try Flash… again." % code)
                self.step = IDLE
        self._refresh()

    def _browse(self):
        f, _ = QFileDialog.getOpenFileName(self, "ESP-IDF activation script",
                                           str(Path.home() / ".espressif" / "tools"),
                                           "Shell scripts (*.sh)")
        if f:
            self.activate.setText(f)

    # ---- 3. flash ------------------------------------------------------------------------

    def _set_step(self, step: str):
        self.step = step
        if step in STEP_TEXT:
            self.step_banner.set("warn" if step in (HOLD_BOOT, REPLUG) else "info",
                                 STEP_TEXT[step])
        self._refresh()

    def start_flash(self) -> bool:
        if self.job.running() or self.step != IDLE or not self.port():
            return False
        self._flash_port = self.port()
        self.cfg = {}
        if self.connected():
            self._set_step(CLOSING)
            self.main.disconnect_link()
            self._poll.start()
        else:
            self._set_step(HOLD_BOOT)
        return True

    def _poll_tick(self):
        if self.step == CLOSING and not self.connected():
            self._poll.stop()
            self._set_step(HOLD_BOOT)

    def flash_now(self) -> bool:
        if self.step != HOLD_BOOT:
            return False
        self._set_step(FLASHING)
        self._run("flash", I.flash_command(self.activation(), self._flash_port))
        return True

    def reconnect(self) -> bool:
        if self.step != REPLUG:
            return False
        self._set_step(CHECKING)
        if self.main is not None:
            self.main.connect_link()
        return True

    def abort(self):
        self._poll.stop()
        self.step = IDLE
        self.step_banner.set("neutral", "Aborted. Reconnect from the top bar when the camera "
                             "is running.")
        self._refresh()

    # ---- 4. check (the shell's hooks) ------------------------------------------------------

    def on_cfg(self, v: dict):
        self.cfg = dict(v)
        if self.step == CHECKING:
            self.step = IDLE
            tone, text = I.camera_verdict(self.cfg, self.installed())
            self.step_banner.set(tone, "Flashed. " + text)
        self._refresh()

    def on_disconnected(self):
        self.cfg = {}
        self._refresh()

    def save_state(self, store):
        store.setValue("set", self.set_combo.currentData() or "")
        store.setValue("activate", self.activate.text())

    def restore_state(self, store):
        i = self.set_combo.findData(store.value("set", "", type=str))
        if i >= 0:
            self.set_combo.setCurrentIndex(i)
        a = store.value("activate", "", type=str)
        if a and Path(a).expanduser().is_file():
            self.activate.setText(a)
