"""
The long jobs of the Train, Verify and Firmware tabs, kept off the GUI thread.

Scoring runs in a QThread: a few hundred frames take seconds, and the progress
bar must move. Training (host/train/train_lut.py, train_clf.py) and, later,
building and flashing run as child processes through QProcess, so the GUI runs
exactly the commands the README teaches, shows their output as it comes and
cannot be stalled by them. Every command is kept, so the operator can copy it
and run it by hand from target_cam/.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QProcess, QThread, pyqtSignal

from . import evaluation as E
from . import library as Lib


class EvalWorker(QThread):
    progress = pyqtSignal(int, int)
    done = pyqtSignal(object)           # EvalResult
    failed = pyqtSignal(str)

    def __init__(self, cand: E.Candidate, sessions: list, parent=None):
        super().__init__(parent)
        self.cand, self.sessions = cand, list(sessions)
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        try:
            res = E.evaluate(self.cand, self.sessions, self.progress.emit, lambda: self._stop)
        except Exception as e:                      # shown to the user, not swallowed
            self.failed.emit("%s: %s" % (type(e).__name__, e))
            return
        self.done.emit(res)


# ---- training commands -----------------------------------------------------------

def lut_command(out: Path, sessions: list, sigma: Optional[float] = None,
                k: Optional[float] = None, min_evidence: Optional[float] = None) -> list:
    """train_lut.py into `out`. Session paths are given relative to target_cam/
    (the trainer's working directory), so the .json records portable paths."""
    cmd = [sys.executable, str(Lib.TRAIN_LUT), "--out", Lib.rel(out)]
    for flag, v in (("--sigma", sigma), ("--k", k), ("--min-evidence", min_evidence)):
        if v is not None:
            cmd += [flag, "%g" % v]
    return cmd + [Lib.rel(s.path) for s in sessions]


def clf_command(out: Path, sessions: list, l2: Optional[float] = None,
                threshold: Optional[float] = None, max_lost: Optional[float] = None,
                allow_stage0: bool = False) -> list:
    """train_clf.py behind the table already copied into `out` (library.new_set)."""
    cmd = [sys.executable, str(Lib.TRAIN_CLF), "--lut", Lib.rel(Path(out) / "lut_model.bin"),
           "--out", Lib.rel(out)]
    for flag, v in (("--l2", l2), ("--threshold", threshold), ("--max-lost", max_lost)):
        if v is not None:
            cmd += [flag, "%g" % v]
    if allow_stage0:
        cmd.append("--allow-stage0")
    return cmd + [Lib.rel(s.path) for s in sessions]


def cnn_command(out: Path, sessions: list, threshold: Optional[float] = None,
                max_lost: Optional[float] = None, size: int = 16, epochs: int = 40,
                cnn_k: int = 2, allow_stage0: bool = False) -> list:
    """train_cnn.py behind the table and classifier already copied into
    `out` (library.new_set with upto="clf")."""
    out = Path(out)
    # Always behind the classifier: stage 3 runs after stage 2, and the set
    # is made from a parent that has one (the Train tab checks it).
    cmd = [sys.executable, str(Lib.TRAIN_CNN), "--lut", Lib.rel(out / "lut_model.bin"),
           "--clf", Lib.rel(out / "clf_model.bin")]
    cmd += ["--out", Lib.rel(out), "--size", str(size), "--epochs", str(epochs),
            "--cnn-k", str(cnn_k)]
    for flag, v in (("--threshold", threshold), ("--max-lost", max_lost)):
        if v is not None:
            cmd += [flag, "%g" % v]
    if allow_stage0:
        cmd.append("--allow-stage0")
    return cmd + [Lib.rel(s.path) for s in sessions]


def pretty(cmd: list) -> str:
    """The command as typed in a terminal from target_cam/ (the env's python)."""
    out = ["python"]
    for a in cmd[1:]:
        try:
            a = str(Path(a).relative_to(Lib.TARGET_CAM))
        except ValueError:
            pass
        out.append(shlex.quote(a))
    return " ".join(out)


class ProcessJob(QObject):
    """One child process at a time, its merged output streamed as it comes."""
    output = pyqtSignal(str)
    finished = pyqtSignal(int, str)     # exit code (-1: did not start), all output

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        self.text = ""
        self.cmd: list = []

    def running(self) -> bool:
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self, cmd: list, cwd: Optional[Path] = None, env: Optional[dict] = None):
        self.text = ""
        self.cmd = list(cmd)
        self.proc.setWorkingDirectory(str(cwd or Lib.TARGET_CAM))
        e = self.proc.processEnvironment()
        if e.isEmpty():
            from PyQt6.QtCore import QProcessEnvironment
            e = QProcessEnvironment.systemEnvironment()
        e.insert("PYTHONUNBUFFERED", "1")           # lines as they are printed
        for k, v in (env or {}).items():
            e.insert(k, v)
        self.proc.setProcessEnvironment(e)
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
            msg = "could not start %s\n" % self.cmd[0]
            self.text += msg
            self.output.emit(msg)
            self.finished.emit(-1, self.text)
