"""
The serial link to the camera, on its own thread.

Two bench lessons are built in here:

  * Opening the port must not reset the ESP32-CAM. DTR and RTS are set low
    BEFORE open(): on the ESP32-CAM-MB programmer they drive EN and GPIO0.

  * Commands sent in a quick burst arrived corrupted on 2026-10-13 ('m' came
    through as 'W', "H330" as hue 3). So commands go one at a time: a "\\n"
    first to flush any half-line, then the command, then wait for the camera's
    #!ACK / #!NAK naming that exact command, and retry once if neither comes.
    Nothing is assumed to have arrived until the camera says so.

Baud changes follow the firmware's handshake (see main/link.h): the camera
ACKs 'B<n>', prints #!BAUD <n> at the old speed and switches; we switch and
send 'k'; if the camera hears nothing valid within 3 s it reverts to 115200.
If we hear nothing at the new speed we drop back to 115200 ourselves, after
waiting out the camera's revert. The same fallback runs if we are on a faster speed
and hear only junk for 3 s, after first asking the camera down (see
_watchdog: the camera may have rebooted, or the link may have failed under it).

The port is opened exclusively, so a second viewer (or a second person on a
shared machine) is told the port is in use instead of silently splitting
the byte stream. A disconnect first asks the camera back to 115200
(close()): every connection starts at 115200, and the camera's own revert
only guards the handshake, so a camera left at a fast speed would be
unreachable for the next viewer.
"""
from __future__ import annotations

import errno
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import serial
from PyQt6.QtCore import QThread, pyqtSignal

from . import protocol as P

REVERT_S = 3.0      # firmware LINK_REVERT_MS
CLOSE_S = 5.0       # how long a disconnect waits for the camera to come down to 115200

# closed(kind, detail)
STOPPED, OPEN_FAILED, LOST = "stopped", "open_failed", "lost"


def open_error(port: str, e: Exception) -> tuple[str, str]:
    """(short text for the pill, hint for the console) for a failed open.
    pyserial passes the OS errno through, also for the exclusive lock."""
    code = getattr(e, "errno", None)
    text = str(e)
    if code in (errno.ENOENT, errno.ENODEV, errno.ENXIO) or "No such file" in text:
        return "Port not found: %s" % port, "unplugged, or a different port name"
    if code in (errno.EACCES, errno.EPERM) or "Permission denied" in text:
        return "Permission denied: %s" % port, \
            "add your user to the `dialout` group, then log out and in"
    if code in (errno.EBUSY, errno.EAGAIN) or "lock" in text:
        return "Port in use: %s" % port, ("the port is in use by another program (another "
                                          "viewer, `idf.py monitor`, a serial terminal)")
    return "Cannot open %s" % port, ""


@dataclass
class _Cmd:
    text: str
    baud: Optional[int] = None          # set for a 'B' command
    sent_at: float = 0.0
    tries: int = 0


@dataclass
class Counters:
    bytes_total: int = 0
    bytes_text: int = 0
    bytes_frame: int = 0
    frames_ok: int = 0
    frames_bad: int = 0
    blobs_ok: int = 0
    blobs_bad: int = 0
    junk_lines: int = 0
    rows: int = 0
    naks: int = 0
    retries: int = 0
    timeouts: int = 0
    history: list = field(default_factory=list)     # (t, bytes_total) for rates

    def rate(self, window: float = 2.0) -> float:
        """Bytes per second over the last `window` seconds."""
        h = self.history
        if len(h) < 2:
            return 0.0
        t1, b1 = h[-1]
        for t0, b0 in h:
            if t1 - t0 <= window:
                return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0
        return 0.0


class SerialLink(QThread):
    event = pyqtSignal(object)              # protocol events, in arrival order
    command_done = pyqtSignal(str, bool, str)   # cmd, accepted, reason
    state = pyqtSignal(str)                 # human-readable link state
    baud_changed = pyqtSignal(int)
    opened = pyqtSignal()                   # the port is open, at BOOT_BAUD
    closed = pyqtSignal(str, str)           # STOPPED / OPEN_FAILED / LOST, detail

    def __init__(self, port: str, parent=None):
        super().__init__(parent)
        self.port = port
        self.baud = P.BOOT_BAUD
        self.counters = Counters()
        self.image_on = False               # from #!CFG; frames make the camera slow to answer
        self._q: "queue.Queue[_Cmd]" = queue.Queue()
        self._stop = threading.Event()
        self._ser: Optional[serial.Serial] = None
        self._cur: Optional[_Cmd] = None
        self._parser = P.StreamParser()
        self._last_valid = time.monotonic()
        self._switch: Optional[dict] = None     # baud switch in progress
        self._closing: Optional[float] = None   # close() deadline
        self.open_exc: Optional[Exception] = None

    # ---- API (any thread) ----------------------------------------------------

    def send(self, cmd: str):
        cmd = cmd.strip()
        if not cmd:
            return
        baud = None
        if cmd.startswith("B"):
            try:
                baud = int(cmd[1:])
            except ValueError:
                pass
        self._q.put(_Cmd(cmd, baud))

    def pending(self) -> int:
        return self._q.qsize() + (1 if self._cur else 0)

    def stop(self):
        self._stop.set()
        self.wait(3000)

    def close(self):
        """Ask the camera back to 115200 if it is not there (bounded by
        CLOSE_S), then close the port. Returns at once; `closed` follows.
        Commands not yet sent are dropped; nothing else is sent."""
        if self._closing is not None:
            return
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if self.baud != P.BOOT_BAUD or self._switch is not None:
            self._q.put(_Cmd("B%d" % P.BOOT_BAUD, P.BOOT_BAUD))
        self._closing = time.monotonic() + CLOSE_S

    def _closed_down(self) -> bool:
        if self._closing is None:
            return False
        if time.monotonic() > self._closing:
            return True
        return (self.baud == P.BOOT_BAUD and self._switch is None and self._cur is None
                and self._q.empty())

    # ---- thread --------------------------------------------------------------

    def run(self):
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = self.port, P.BOOT_BAUD, 0.02
            s.dtr = False
            s.rts = False
            s.exclusive = True
            s.open()
        except (serial.SerialException, OSError) as e:
            self.open_exc = e           # for open_error(): it keeps the errno
            self.state.emit("cannot open %s: %s" % (self.port, e))
            self.closed.emit(OPEN_FAILED, str(e))
            return
        self._ser = s
        self.state.emit("connected to %s at %d" % (self.port, self.baud))
        self.baud_changed.emit(self.baud)
        self.opened.emit()
        kind, detail = STOPPED, ""
        try:
            while not self._stop.is_set() and not self._closed_down():
                self._read()
                self._commands()
                self._watchdog()
        except (serial.SerialException, OSError) as e:
            self.state.emit("link lost: %s" % e)
            kind, detail = LOST, str(e)
        finally:
            s.close()
        if kind == STOPPED and self.baud != P.BOOT_BAUD:
            detail = "the camera did not come back to %d; it stays at %d until it reverts " \
                     "or is reset" % (P.BOOT_BAUD, self.baud)
        self.closed.emit(kind, detail)

    def _read(self):
        s = self._ser
        data = s.read(max(1, min(s.in_waiting, 65536)))
        now = time.monotonic()
        c = self.counters
        if data:
            c.bytes_total += len(data)
            events = self._parser.feed(data, now)
        else:
            events = []
        events += self._parser.expire(now, self.baud)
        p = self._parser
        c.bytes_text, c.bytes_frame = p.bytes_text, p.bytes_frame
        c.frames_ok, c.frames_bad, c.junk_lines = p.frames_ok, p.frames_bad, p.junk_lines
        c.blobs_ok, c.blobs_bad = p.blobs_ok, p.blobs_bad
        c.history.append((now, c.bytes_total))
        while c.history and now - c.history[0][0] > 10.0:
            c.history.pop(0)
        for ev in events:
            if not isinstance(ev, P.Junk):
                self._last_valid = now
            if isinstance(ev, P.Row):
                c.rows += 1
            elif isinstance(ev, P.Cfg):
                self.image_on = ev.v.get("image") == 1
            self._on_event(ev, now)
            self.event.emit(ev)

    def _on_event(self, ev, now):
        cur = self._cur
        if cur is None:
            return
        if isinstance(ev, P.Ack) and ev.cmd == cur.text:
            if cur.baud is not None and self._switch is None:
                # Accepted: the camera now prints #!BAUD and switches.
                self._switch = {"stage": "announce", "to": cur.baud, "since": now}
                return
            self._finish(True, "")
        elif isinstance(ev, P.Nak) and ev.cmd == cur.text:
            self.counters.naks += 1
            self._finish(False, ev.reason or "refused")
        elif isinstance(ev, P.Baud) and self._switch and self._switch["stage"] == "announce":
            self._do_switch(ev.baud, now)
        elif (isinstance(ev, P.Ack) and ev.cmd == "k" and self._switch
              and self._switch["stage"] == "confirm"):
            self._switch = None
            self._finish(True, "")
            self.state.emit("link at %d baud" % self.baud)

    def _do_switch(self, baud, now):
        self._set_port_baud(baud)
        # The first k goes out ~50 ms from now (0.7 s retry spacing below),
        # giving the camera time to finish its own uart_set_baudrate().
        self._switch = {"stage": "confirm", "to": baud, "since": now,
                        "sent": now - 0.65, "tries": 0}

    def _set_port_baud(self, baud):
        s = self._ser
        s.baudrate = baud
        s.reset_input_buffer()
        # A half line or half frame from the old speed is garbage now.
        self._parser.reset()
        self.baud = baud
        self._last_valid = time.monotonic()
        self.baud_changed.emit(baud)

    def _write(self, text: str):
        self._ser.write(("\n" + text + "\n").encode("ascii", "replace"))

    def _timeout(self) -> float:
        # The camera reads commands once per loop, and while it is sending a
        # frame its loop takes the whole transfer time.
        t = 1.0
        if self.image_on or self._parser.in_frame:
            t += P.frame_seconds(160 * 120 * 2 + 120, self.baud) * 1.3
        return t

    def _commands(self):
        now = time.monotonic()
        sw = self._switch
        if sw is not None:
            if sw["stage"] == "announce" and now - sw["since"] > self._timeout() + 1.0:
                # ACKed but no #!BAUD seen: assume it switched anyway.
                self._do_switch(sw["to"], now)
            elif sw["stage"] == "confirm":
                if sw["tries"] < 3 and now - sw["sent"] > 0.7:
                    self._write("k")
                    sw["sent"], sw["tries"] = now, sw["tries"] + 1
                elif sw["tries"] >= 3 and now - sw["sent"] > 0.7:
                    self._fallback("no answer at %d baud" % sw["to"])
            return
        if self._cur is None:
            try:
                self._cur = self._q.get_nowait()
            except queue.Empty:
                return
            self._cur.sent_at = now
            self._cur.tries = 1
            self._write(self._cur.text)
            return
        cur = self._cur
        if now - cur.sent_at > self._timeout():
            if cur.tries < 2:
                self.counters.retries += 1
                cur.tries += 1
                cur.sent_at = now
                self._write(cur.text)
            else:
                self.counters.timeouts += 1
                self._finish(False, "no reply from the camera")

    def _finish(self, ok, reason):
        cur, self._cur = self._cur, None
        if cur is not None:
            self.command_done.emit(cur.text, ok, reason)

    def _fallback(self, why):
        """Back to 115200 after the camera's own revert has had time to fire."""
        self.state.emit("%s; waiting for the camera to revert to %d" % (why, P.BOOT_BAUD))
        time.sleep(REVERT_S + 0.5)
        self._set_port_baud(P.BOOT_BAUD)
        self._switch = None
        self._finish(False, why + "; back at %d" % P.BOOT_BAUD)
        self.send("k")
        self.state.emit("link at %d baud (fallback)" % P.BOOT_BAUD)

    def _watchdog(self):
        """Only junk for 3 s at a faster speed has two causes. The camera
        rebooted (a replug, a brownout) and talks at 115200: follow it down.
        Or it is still at this speed and the link has gone bad under load,
        as the ESP32-CAM-MB's bridge appears to above 115200 with image on
        (2026-10-17, inferred): the camera's own revert only guards the handshake, so
        it would stay up here for good while we sat at 115200, each side
        hearing the other as noise. So ask it down first, at this speed -
        a short command can get through where a 38 kB frame cannot - and
        then follow. To a camera already at 115200 the request is a few
        bytes of noise."""
        if (self.baud != P.BOOT_BAUD and self._switch is None
                and time.monotonic() - self._last_valid > 3.0
                and self.counters.rate(1.0) > 0):
            was = self.baud
            self._ser.write(("\nB%d\n" % P.BOOT_BAUD).encode())
            self._ser.flush()       # out at this speed before the port changes
            self._set_port_baud(P.BOOT_BAUD)
            self.state.emit("only junk at %d for 3 s; asked the camera down and "
                            "followed to %d (if junk goes on, replug it)" % (was, P.BOOT_BAUD))
            self.send("k")
