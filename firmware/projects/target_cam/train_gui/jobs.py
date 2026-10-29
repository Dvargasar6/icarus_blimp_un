"""
The long jobs, kept off the GUI thread.

Scoring runs in a QThread: a few hundred frames take a few seconds, and the
progress bar must move. Training (host/train/train_lut.py, train_clf.py),
building and flashing run as child processes through QProcess, so the GUI
runs exactly the commands the README teaches, shows their output as it comes
and cannot be stalled by them. The command line is kept, so the user can copy
it and run it by hand. Asking the camera for its settings is a QThread too:
it waits on the serial port for up to 15 s.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QProcess, QThread, pyqtSignal

from . import engine as E
from . import paths


class EvalWorker(QThread):
    progress = pyqtSignal(int, int)
    done = pyqtSignal(object)           # EvalResult
    failed = pyqtSignal(str)

    def __init__(self, table: paths.Table, sessions: list[paths.Session], parent=None,
                 stage: int = 1):
        super().__init__(parent)
        self.table, self.sessions, self.stage = table, list(sessions), stage
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        try:
            res = E.evaluate(self.table, self.sessions, self.progress.emit, lambda: self._stop,
                             self.stage)
        except Exception as e:                      # shown to the user, not swallowed
            self.failed.emit("%s: %s" % (type(e).__name__, e))
            return
        self.done.emit(res)


def train_command(out: Path, sessions: list[paths.Session], sigma: Optional[float],
                  k: Optional[float], min_evidence: Optional[float]) -> list[str]:
    cmd = [sys.executable, str(paths.TRAIN_SCRIPT), "--out", str(out)]
    if sigma is not None:
        cmd += ["--sigma", "%g" % sigma]
    if k is not None:
        cmd += ["--k", "%g" % k]
    if min_evidence is not None:
        cmd += ["--min-evidence", "%g" % min_evidence]
    cmd += [str(s.path) for s in sessions]
    return cmd


def clf_command(table: paths.Table, out: Path, sessions: list[paths.Session],
                l2: Optional[float], threshold: Optional[float],
                allow_stage0: bool = False) -> list[str]:
    cmd = [sys.executable, str(paths.TRAIN_CLF_SCRIPT), "--lut", str(table.bin),
           "--out", str(out)]
    if allow_stage0:
        cmd += ["--allow-stage0"]
    if l2 is not None:
        cmd += ["--l2", "%g" % l2]
    if threshold is not None:
        cmd += ["--threshold", "%g" % threshold]
    cmd += [str(s.path) for s in sessions]
    return cmd


class CameraQuery(QThread):
    """install.query_camera() off the GUI thread."""
    done = pyqtSignal(object)           # the #!CFG dict
    failed = pyqtSignal(str)

    def __init__(self, port: str, parent=None):
        super().__init__(parent)
        self.port = port
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        from . import install
        try:
            cfg = install.query_camera(self.port, cancelled=lambda: self._stop)
        except Exception as e:                      # shown to the user, not swallowed
            self.failed.emit("%s: %s" % (type(e).__name__, e))
            return
        self.done.emit(cfg)


def pretty_command(cmd: list[str]) -> str:
    """The command as typed in a terminal from target_cam/, paths relative."""
    out = []
    for i, a in enumerate(cmd):
        if i == 0:
            out.append("python")
            continue
        try:
            a = str(Path(a).relative_to(paths.TARGET_CAM))
        except ValueError:
            pass
        out.append(a)
    return " ".join(out)


class TrainJob(QObject):
    output = pyqtSignal(str)
    finished = pyqtSignal(int, str)     # exit code, the whole output

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.setWorkingDirectory(str(paths.TARGET_CAM))
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        self.text = ""
        self.cmd: list[str] = []

    def running(self) -> bool:
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self, cmd: list[str], cwd: Optional[Path] = None):
        self.text = ""
        self.cmd = list(cmd)
        self.proc.setWorkingDirectory(str(cwd or paths.TARGET_CAM))
        env = self.proc.processEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")        # lines as they are printed
        self.proc.setProcessEnvironment(env)
        self.proc.start(cmd[0], cmd[1:])

    def stop(self):
        if self.running():
            self.proc.kill()

    def _read(self):
        s = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        self.text += s
        self.output.emit(s)

    def _finished(self, code, _status):
        self.finished.emit(int(code), self.text)

    def _error(self, err):
        if err == QProcess.ProcessError.FailedToStart:
            msg = "no se pudo iniciar %s\n" % self.cmd[0]
            self.text += msg
            self.output.emit(msg)
            self.finished.emit(-1, self.text)
