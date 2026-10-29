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

Phase D adds the camera's own copy of its models (model_store.h):

  - Back up    '!dump' reads back the records the camera runs, which may be
               the build's or uploaded ones, and keeps them as a set,
               models/camera_<lut_id>_<date> (unless a set already holds those
               bytes). Install and Upload do it first whenever a camera is
               connected, so what the micro held is never lost, whatever
               main/models says.
  - Upload     '!upload' sends a set's records over the link into the camera's
               model partition, where they win over the build's: retraining at
               the venue needs no rebuild and no BOOT button. Kinds the set
               lacks are '!unload'ed if the camera has uploaded ones.
  - Unload     '!unload all': the build's models again. Flashing does it
               first, because uploaded models survive a reflash and would
               otherwise shadow the ones just built.

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
IDLE, UNLOADING, CLOSING, HOLD_BOOT, FLASHING, REPLUG, CHECKING = (
    "idle", "unloading", "closing", "hold_boot", "flashing", "replug", "checking")
XFER_TIMEOUT_MS = 20000         # one dump or upload step; 16 kB is 1.4 s at 115200
STEP_TEXT = {
    UNLOADING: "Unloading the camera's uploaded models first (they would shadow the "
               "build's)…",
    CLOSING: "Releasing the serial port…",
    HOLD_BOOT: "Hold the ESP32-CAM-MB's BOOT button, unplug and replug the USB cable, and keep "
               "holding BOOT. Then click \"BOOT held — flash now\".",
    FLASHING: "Flashing at 115200 with BOOT held… keep holding it.",
    REPLUG: "Release BOOT, then unplug and replug the USB cable so the new firmware runs. Then "
            "click \"Reconnect and check\".",
    CHECKING: "Waiting for the camera's #!CFG (it answers after its 5 s reflash window)…",
}


def _hrow(*widgets) -> QHBoxLayout:
    row = QHBoxLayout()
    for w in widgets:
        row.addWidget(w)
    row.addStretch(1)
    return row


class FirmwareTab(QWidget):
    def __init__(self, main=None, models_dir: Path = Lib.MODELS_DIR,
                 firmware_dir: Path = Lib.FIRMWARE_MODELS, app_bin: Path = I.APP_BIN):
        super().__init__()
        self.main = main
        self.models_dir, self.fw_dir, self.app_bin = Path(models_dir), Path(firmware_dir), app_bin
        self.sdk_project = Lib.TARGET_CAM       # tests point it elsewhere
        self.cfg: dict = {}
        self.step = IDLE
        self.job = J.ProcessJob(self)
        self.job.output.connect(self._output)
        self.job.finished.connect(self._finished)
        self._job_kind = ""
        self.confirm = self._ask            # tests replace it
        self._poll = QTimer(self, interval=200)
        self._poll.timeout.connect(self._poll_tick)
        self.xfer = None                    # a dump / upload sequence in progress
        self._xfer_timer = QTimer(self, singleShot=True, interval=XFER_TIMEOUT_MS)
        self._xfer_timer.timeout.connect(lambda: self._xfer_fail("the camera did not answer"))

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

        sec = CollapsibleSection("Camera models (no flashing)")
        row = QHBoxLayout()
        self.backup_btn = QPushButton("Back up the camera's models")
        self.backup_btn.setToolTip("!dump: read back the records the camera runs and keep "
                                   "them under models/ (skipped if a set holds those bytes)")
        self.backup_btn.clicked.connect(lambda: self.backup())
        self.upload_btn = QPushButton("Upload the chosen set")
        theme.set_role(self.upload_btn, "primary")
        self.upload_btn.setToolTip("!upload: the set's records go into the camera's model "
                                   "partition and run at once, no rebuild, no BOOT button "
                                   "(the camera's models are backed up first)")
        self.upload_btn.clicked.connect(self.upload)
        self.unload_btn = QPushButton("Back to the build's models")
        self.unload_btn.setToolTip("!unload all: erase the uploaded models")
        self.unload_btn.clicked.connect(lambda: self.main.send("!unload all"))
        for b in (self.backup_btn, self.upload_btn, self.unload_btn):
            row.addWidget(b)
        row.addStretch(1)
        sec.add_layout(row)
        self.xfer_banner = Banner("neutral", "Needs a camera with a firmware from phase D "
                                  "(#!CFG upload=1).")
        sec.add_widget(self.xfer_banner)
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
        self.sdk_banner = Banner("warn", "")
        self.sdk_btn = QPushButton("Regenerate sdkconfig")
        self.sdk_btn.setToolTip("Moves sdkconfig to sdkconfig.old; the next Build makes it "
                                "again from sdkconfig.defaults")
        self.sdk_btn.clicked.connect(self._regenerate)
        sec.add_widget(self.sdk_banner)
        sec.add_layout(_hrow(self.sdk_btn))
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
        can_xfer = self.connected() and str(self.cfg.get("upload", "")) == "1" and \
            self.xfer is None and not busy
        self.backup_btn.setEnabled(can_xfer)
        self.upload_btn.setEnabled(can_xfer and s is not None and not I.check_installable(s))
        self.unload_btn.setEnabled(can_xfer and any(
            self.cfg.get("%s_src" % k) == "upload" for k in Lib.KINDS))
        if self.cfg and "upload" not in self.cfg:
            self.xfer_banner.set("neutral", "This camera's firmware predates model upload "
                                 "(phase D): build and flash once with the new partition table.")
        elif self.cfg and str(self.cfg.get("upload")) == "0":
            self.xfer_banner.set("warn", "The camera has no 'tcmodels' partition: its build "
                                 "used the old partition table. Build (sdkconfig regenerated) "
                                 "and flash once.")
        stale = I.stale_sdkconfig(self.sdk_project)
        self.sdk_banner.setVisible(bool(stale))
        self.sdk_btn.setVisible(bool(stale))
        if stale:
            self.sdk_banner.set("warn", "sdkconfig predates sdkconfig.defaults and lacks: %s. "
                                "Built like this, the firmware keeps the old settings (for "
                                "the partition table: no model upload). Regenerate it first."
                                % ", ".join(stale))
        if tool:
            why.append(tool)
        self.why.setText(" ".join(why))

    def _regenerate(self):
        try:
            old = I.regenerate_sdkconfig(self.sdk_project)
        except OSError as e:
            self.sdk_banner.set("err", "Could not move sdkconfig: %s" % e)
            return
        self.build_banner.set("info", "sdkconfig moved to %s; Build makes it again from "
                              "sdkconfig.defaults." % old.name)
        self._refresh()

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
        if self.connected() and str(self.cfg.get("upload", "")) == "1":
            text += ("\n\nThe camera's own models are read back and kept under models/ "
                     "first (!dump).")
        elif cam and cam["lut_id"] != inst.lut_id:
            text += ("\n\nNote: the camera reports table %d, not main/models' %d, and its "
                     "firmware cannot send its models back, so they are not backed up."
                     % (cam["lut_id"], inst.lut_id))
        if not self.confirm(text):
            return False
        if self.connected() and str(self.cfg.get("upload", "")) == "1":
            return self.backup(then=lambda: self._do_install(s))
        return self._do_install(s)

    def _do_install(self, s: Lib.ModelSet) -> bool:
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
        if self.connected() and any(self.cfg.get("%s_src" % k) == "upload" for k in Lib.KINDS):
            # Uploaded models survive a reflash and win over the build's:
            # unload them first, or the check after flashing would find the
            # old uploads running instead of what was just built.
            self._set_step(UNLOADING)
            self.main.send("!unload all")
            QTimer.singleShot(10000, self._unload_timeout)
            return True
        return self._release_port()

    def _unload_timeout(self):
        if self.step == UNLOADING:
            self.step_banner.set("warn", "The camera did not confirm '!unload all'; flashing "
                                 "anyway. Check its models after the flash.")
            self._release_port()

    def _release_port(self) -> bool:
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
        if self.step == UNLOADING and not any(v.get("%s_src" % k) == "upload"
                                              for k in Lib.KINDS):
            self._release_port()
            return
        if self.xfer is not None and self.xfer["mode"] == "verify":
            self._xfer_timer.stop()
            s = self.xfer["set"]
            self.xfer = None
            self.xfer_banner.set(*I.upload_verdict(self.cfg, s))
        if self.step == CHECKING:
            self.step = IDLE
            tone, text = I.camera_verdict(self.cfg, self.installed())
            self.step_banner.set(tone, "Flashed. " + text)
        self._refresh()

    def on_disconnected(self):
        self.cfg = {}
        if self.xfer is not None:
            self._xfer_fail("the link closed")
        self._refresh()

    # ---- Phase D: the camera's own models ----------------------------------------------

    def backup(self, then=None) -> bool:
        """Read back the camera's models (!dump lut/clf/cnn) and keep them as a
        set; then() afterwards (Install and Upload continue from here)."""
        if self.xfer is not None or not self.connected():
            return False
        self.xfer = dict(mode="backup", pending={"lut", "clf", "cnn"}, records={},
                         sources={}, then=then)
        self.xfer_banner.set("info", "Reading the camera's models back (!dump)…")
        for k in Lib.KINDS:
            self.main.send("!dump %s" % k)
        self._xfer_timer.start()
        self._refresh()
        return True

    def on_model(self, ev):
        """A #!MODEL packet or '#!MODEL none' line, during a backup."""
        x = self.xfer
        if x is None or x["mode"] != "backup" or ev.kind not in x["pending"]:
            return
        x["pending"].discard(ev.kind)
        if getattr(ev, "data", None) is not None:
            x["records"][ev.kind] = ev.data
            x["sources"][ev.kind] = ev.src
        if x["pending"]:
            self._xfer_timer.start()
            return
        self._xfer_timer.stop()
        self.xfer = None
        if not x["records"]:
            self.xfer_banner.set("warn", "The camera runs no model to back up.")
        else:
            try:
                name, new = I.save_camera_models(x["records"], x["sources"], self.models_dir)
            except Exception as e:                  # a broken record: say so, do not go on
                self._xfer_fail("its models could not be kept: %s" % e)
                return
            self.xfer_banner.set("ok", ("The camera's models (%s) were kept as models/%s."
                                        if new else "The camera's models (%s) are already "
                                        "saved as models/%s.") % (
                ", ".join("%s from %s" % (k, x["sources"][k]) for k in x["records"]), name))
            self.reload_sets()
        if x["then"] is not None:
            x["then"]()
        self._refresh()

    def upload(self) -> bool:
        """The chosen set into the camera's model partition: back up first,
        then the plan of install.upload_plan, then check #!CFG."""
        s = self.chosen()
        if s is None or I.check_installable(s) or self.xfer is not None:
            return False
        if not self.confirm("Upload %s to the camera?\n\nIts models run at once and stay "
                            "across power cycles and reflashes, until 'Back to the build's "
                            "models'. The camera's current models are backed up first."
                            % s.name):
            return False
        return self.backup(then=lambda: self._start_upload(s))

    def _start_upload(self, s: Lib.ModelSet):
        plan = I.upload_plan(s, self.cfg)
        self.xfer = dict(mode="upload", set=s, plan=plan, cur=None)
        self._next_upload()

    def _next_upload(self):
        x = self.xfer
        if not x["plan"]:
            x["mode"] = "verify"
            self.xfer_banner.set("info", "Checking what the camera runs now…")
            self.main.send("k")
            self._xfer_timer.start()
            return
        step = x["plan"].pop(0)
        x["cur"] = step
        if step[0] == "unload":
            self.xfer_banner.set("info", "Unloading the camera's uploaded %s (%s has none)…"
                                 % (step[1], x["set"].name))
            self.main.send("!unload %s" % step[1])
            self._next_upload()
            return
        self.xfer_banner.set("info", "Uploading %s (%d bytes)…" % (step[1], len(step[2])))
        self.main.send(I.upload_command(step[1], step[2]))
        self._xfer_timer.start()

    def on_upload(self, ev):
        """#!UPLOAD ready / ok / refused, during an upload."""
        x = self.xfer
        if x is None or x["mode"] != "upload" or x["cur"] is None or \
                ev.v.get("kind") != x["cur"][1]:
            return
        if ev.status == "ready":
            self.main.link.send_raw(x["cur"][2])
            self._xfer_timer.start()
        elif ev.status == "ok":
            self._next_upload()
        else:
            self._xfer_fail("the camera refused the %s: %s" % (x["cur"][1],
                                                               ev.v.get("reason", "?")))

    def _xfer_fail(self, why: str):
        self._xfer_timer.stop()
        mode = self.xfer["mode"] if self.xfer else "?"
        self.xfer = None
        self.xfer_banner.set("err", "%s stopped: %s." % ({"backup": "Backup",
                                                          "upload": "Upload",
                                                          "verify": "Upload check"}.get(
            mode, "Transfer"), why))
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
