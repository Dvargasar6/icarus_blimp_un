"""
The target_cam serial protocol, parsed. No Qt and no serial port here, so all
of it is tested on its own (tests/test_protocol.py).

What comes out of the camera on UART0 is one byte stream that mixes:

  text lines (each ends "\\r\\n"; the console turns every "\\n" into "\\r\\n")
    t_ms,ok,cx_px,...          the CSV row - the only thing the ESP32-S3 uses
                               (v1: 25 columns; v2: 35, the last one csv_v=2)
    # ...                      human-readable banner, replies, mask dumps
    #!CFG k=v k=v ...          the settings the camera is really using
    #!ACK <cmd>                a command arrived intact and was accepted
    #!NAK <cmd> -- <reason>    a command arrived and was refused
    #!BAUD <n>                 the camera is switching to n baud now
    #!CAL ok|refused hue= ...  the result of 'c' (calibrate on centre)
    #!WBCAL ok|refused|cancelled ...  the result of 'Wc' (white card)
    #!WB mode= ctrl1= ...      'W': the sensor's white-balance registers
    #!END seq=<n>              closes a binary packet

  binary packets: a header line ending in len= crc=, exactly len raw bytes,
  then #!END
    #!FRAME seq= t_ms= w= h= fmt= len= crc=   a camera frame ('g' or 'v1')
    #!BLOB  seq= t_ms= k= ... feat= ... len= crc=
                                              one reported blob's features and
                                              24x24 crop ('!collect 1'), see
                                              BlobRec

The raw bytes are binary: they can contain 0x0A, so the splitter has to switch
from "read a line" to "read len bytes" after a packet header. A log line from
another task on the camera can land inside the payload. Then the CRC fails, the
frame is dropped, and the tail of the payload comes out as junk text until the
next newline - the parser resynchronises by itself on the following line.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# The CSV row, identical to firmware's print_banner() and target_monitor.py.
#
# v1 had no version column; it is recognised by its 25 columns. v2 appends ten
# after proc_us, so every v1 column keeps its index and an old reader that only
# takes the first 25 still works. A v2 row ends in csv_v=2, and a 35-column row
# claiming another version is refused rather than guessed at.
#   stage            effective detector stage, 0..3 (after any fallback)
#   conf, bN_conf    confidence 0..1 per reported blob; -1 = not computed
#   t_px_us          per-pixel colour test (HSV, or the learned table)
#   t_b_us, t_c_us   blob classifier, CNN
#   loop_us          the PREVIOUS loop's busy time, CSV print included
COLUMNS_V1 = ["t_ms", "ok", "cx_px", "cy_px", "az_deg", "el_deg", "area_px",
              "w_px", "h_px", "fill", "range_mm", "clipped", "blobs", "matched_px",
              "b2_az", "b2_el", "b2_range_mm", "b3_az", "b3_el", "b3_range_mm",
              "b4_az", "b4_el", "b4_range_mm", "dt_ms", "proc_us"]
COLUMNS_V2 = COLUMNS_V1 + ["stage", "conf", "b2_conf", "b3_conf", "b4_conf",
                           "t_px_us", "t_b_us", "t_c_us", "loop_us", "csv_v"]
CSV_VERSION = 2
COLUMNS = COLUMNS_V2            # what the current firmware prints

# What a v1 row did not measure. Timings are NaN, not 0: "not reported" must
# not read as "took no time". stage 0 and conf -1 are what v1 firmware really
# ran: the HSV detector, with no classifier.
V1_FILL = {"stage": 0.0, "conf": -1.0, "b2_conf": -1.0, "b3_conf": -1.0,
           "b4_conf": -1.0, "t_px_us": float("nan"), "t_b_us": float("nan"),
           "t_c_us": float("nan"), "loop_us": float("nan"), "csv_v": 1.0}


def columns_for(version: int) -> list:
    return COLUMNS_V1 if version == 1 else COLUMNS_V2

BOOT_BAUD = 115200
BAUDS = [115200, 230400, 460800, 921600, 1500000, 2000000]


def crc32(data: bytes) -> int:
    """Standard CRC-32, the same as the firmware's esp_rom_crc32_le(0, ...)."""
    return zlib.crc32(data) & 0xFFFFFFFF


def frame_seconds(nbytes: int, baud: int) -> float:
    """Time on the wire: 8N1 is 10 bits per byte."""
    return nbytes * 10.0 / baud


# --------------------------------------------------------------------------
# Events

@dataclass
class Row:
    """One CSV row: what the ESP32-S3 will receive."""
    v: dict
    raw: str


@dataclass
class Cfg:
    """#!CFG: the settings the camera reports it is using."""
    v: dict
    raw: str


@dataclass
class Ack:
    cmd: str


@dataclass
class Nak:
    cmd: str
    reason: str


@dataclass
class Baud:
    baud: int


@dataclass
class Cal:
    ok: bool
    v: dict


@dataclass
class WbCal:
    """#!WBCAL: how 'Wc' ended. status is ok, refused or cancelled (a 'w' or
    'W' arrived while it ran); v holds the gains (ok) or reason= (refused)."""
    status: str
    v: dict


@dataclass
class End:
    seq: int


@dataclass
class Comment:
    """A '# ...' line, or any other readable text the camera printed."""
    text: str


@dataclass
class Junk:
    """Undecodable bytes: noise, a wrong baud, or the tail of a broken frame."""
    text: str


@dataclass
class Frame:
    seq: int
    t_ms: int
    w: int
    h: int
    fmt: str
    data: bytes

    @property
    def big_endian(self) -> bool:
        return self.fmt == "rgb565be"

    def pixels(self) -> np.ndarray:
        """The RGB565 words as a (h, w) uint16 array, byte order honoured."""
        dt = ">u2" if self.big_endian else "<u2"
        return np.frombuffer(self.data, dtype=dt).reshape(self.h, self.w).astype(np.uint16)

    def rgb(self) -> np.ndarray:
        return rgb565_to_rgb888(self.pixels())


@dataclass
class FrameError:
    """A binary packet that failed: CRC, or truncated. kind is the header's
    tag, "FRAME" or "BLOB"."""
    seq: Optional[int]
    reason: str
    kind: str = "FRAME"


CROP = 24                   # blob_feat.h BLOB_CROP
FEAT_N = 12                 # blob_feat.h BLOB_FEAT_N
FEAT_VERSION = 1            # blob_feat.h BLOB_FEAT_VERSION


@dataclass
class BlobRec:
    """One reported blob, as '!collect 1' sends it: what stage 2's classifier
    will see (feat) and what the CNN and the person labelling will see (crop).

    The blob is the k-th of the n reported in the CSV row with the same t_ms,
    so a record can always be tied back to its row (and to a frame, if one was
    grabbed in the same loop). crop_box is the source square in frame pixels,
    (xs, ys, side), possibly partly outside the frame; the crop is that square
    resampled to 24x24 by blob_crop()."""
    seq: int
    t_ms: int
    k: int
    n: int
    stage: int
    csv: int
    fv: int                     # feature definition version
    fw: str
    cx: float
    cy: float
    area: int
    box: tuple                  # x0, y0, x1, y1 inclusive
    crop_box: tuple             # xs, ys, side
    conf: float                 # -1 = no classifier ran
    feat: np.ndarray            # (FEAT_N,) float32
    crop: np.ndarray            # (24, 24) uint16 RGB565 values
    wb: str = "unrecorded"      # auto | frozen | fixed; older firmware sent none
    wb_gain: tuple = (0, 0, 0)  # R, G, B in use, 64 = 1.0; 0,0,0 = not known

    def rgb(self) -> np.ndarray:
        return rgb565_to_rgb888(self.crop)


UNKNOWN_GAIN = (0, 0, 0)


def wb_of(cfg: dict) -> tuple:
    """(mode, (r, g, b)) from a #!CFG dict, or from a saved frame's
    camera_cfg. Firmware before fixed gains sent only awb (1 auto, 0 frozen), and
    then the gains are not known; with neither, the mode is "unrecorded"."""
    if "wb" in cfg:
        try:
            gain = _ints(str(cfg.get("wb_gain", "")), 3)
        except ValueError:
            gain = UNKNOWN_GAIN
        return str(cfg["wb"]), gain
    if "awb" in cfg:
        return ("auto" if cfg["awb"] == 1 else "frozen"), UNKNOWN_GAIN
    return "unrecorded", UNKNOWN_GAIN


def wb_text(mode: str, gain: tuple) -> str:
    """One way to write a white balance everywhere it is shown."""
    if mode == "fixed":
        return "fixed %d,%d,%d" % tuple(gain)
    return mode


def rgb565_to_rgb888(px: np.ndarray) -> np.ndarray:
    """(h, w) uint16 -> (h, w, 3) uint8, for display only. The detector works on
    the 5/6/5-bit values itself (blob_rgb565_hsv), never on these."""
    px = px.astype(np.uint32)
    r = (px >> 11) & 0x1F
    g = (px >> 5) & 0x3F
    b = px & 0x1F
    out = np.empty(px.shape + (3,), dtype=np.uint8)
    out[..., 0] = (r << 3) | (r >> 2)
    out[..., 1] = (g << 2) | (g >> 4)
    out[..., 2] = (b << 3) | (b >> 2)
    return out


# --------------------------------------------------------------------------
# Line parsing

def _num(s: str):
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def parse_kv(s: str, convert: bool = True) -> dict:
    """key=value tokens. With convert=False the values stay strings: a hex CRC
    such as 76555e46 would otherwise come back as the float 7.6555e+50."""
    out = {}
    for tok in s.split():
        k, sep, v = tok.partition("=")
        if sep:
            out[k] = _num(v) if convert else v
    return out


def parse_row(line: str) -> Optional[dict]:
    """A CSV row of either version -> a dict with every v2 key (v1 rows get
    V1_FILL), or None if it is not a row this reader understands."""
    parts = line.split(",")
    if len(parts) not in (len(COLUMNS_V1), len(COLUMNS_V2)):
        return None
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == len(COLUMNS_V1):
        row = dict(zip(COLUMNS_V1, vals))
        row.update(V1_FILL)
        return row
    row = dict(zip(COLUMNS_V2, vals))
    if row["csv_v"] != CSV_VERSION:
        return None
    return row


def parse_line(line: str):
    """One text line (no line ending) -> one event."""
    if line.startswith("#!"):
        tag, _, rest = line[2:].partition(" ")
        if tag == "CFG":
            return Cfg(parse_kv(rest), line)
        if tag == "ACK":
            return Ack(rest)
        if tag == "NAK":
            cmd, _, reason = rest.partition(" -- ")
            return Nak(cmd, reason)
        if tag == "BAUD":
            try:
                return Baud(int(rest))
            except ValueError:
                return Junk(line)
        if tag == "CAL":
            status, _, kv = rest.partition(" ")
            return Cal(status == "ok", parse_kv(kv))
        if tag == "WBCAL":
            status, _, kv = rest.partition(" ")
            return WbCal(status, parse_kv(kv))
        if tag == "END":
            seq = parse_kv(rest).get("seq")
            return End(seq if isinstance(seq, int) else -1)
        return Comment(line)
    if line.startswith("#"):
        return Comment(line)
    if line[:1].isdigit():
        row = parse_row(line)
        if row is not None:
            return Row(row, line)
    if line and all(32 <= ord(c) < 127 for c in line):
        return Comment(line)          # boot ROM text, esp_log lines, ...
    return Junk(line)


# --------------------------------------------------------------------------
# Stream splitter

@dataclass
class _Pending:
    kind: str                   # "FRAME" or "BLOB"
    seq: int
    kv: dict                    # the header's fields, as strings
    length: int
    crc: int
    started: float
    buf: bytearray = field(default_factory=bytearray)


MAX_LINE = 4096     # longer than any real line; beyond this it is binary noise


class StreamParser:
    """Feed it bytes as they arrive, in any chunking; get events back.

    Counters (bytes_text, bytes_frame, frames_ok, frames_bad, blobs_ok,
    blobs_bad, junk_lines) are what the link-health display shows. bytes_frame
    counts every binary payload, blob crops included.
    """

    def __init__(self):
        self._line = bytearray()
        self._frame: Optional[_Pending] = None
        self.bytes_text = 0
        self.bytes_frame = 0
        self.frames_ok = 0
        self.frames_bad = 0
        self.blobs_ok = 0
        self.blobs_bad = 0
        self.junk_lines = 0

    def reset(self):
        """Drop a half-received line or frame (after a baud change); the
        counters are kept."""
        self._line.clear()
        self._frame = None

    @property
    def in_frame(self) -> bool:
        return self._frame is not None

    def feed(self, data: bytes, now: float = 0.0) -> list:
        events = []
        i = 0
        n = len(data)
        while i < n:
            if self._frame is not None:
                f = self._frame
                take = min(f.length - len(f.buf), n - i)
                f.buf += data[i:i + take]
                self.bytes_frame += take
                i += take
                if len(f.buf) == f.length:
                    events.append(self._finish_frame())
                continue
            j = data.find(b"\n", i)
            if j < 0:
                self._line += data[i:]
                self.bytes_text += n - i
                if len(self._line) > MAX_LINE:
                    events.append(self._junk(bytes(self._line)))
                    self._line.clear()
                break
            self._line += data[i:j + 1]
            self.bytes_text += j + 1 - i
            i = j + 1
            ev = self._take_line(now)
            if ev is not None:
                events.append(ev)
        return events

    def expire(self, now: float, baud: int, slack: float = 1.0) -> list:
        """Give up on a frame whose bytes stopped coming - the camera reset, or
        the header was a fluke. Its partial payload is re-read as text, so a
        boot log that arrived after a truncated frame is not swallowed."""
        f = self._frame
        if f is None or now - f.started < frame_seconds(f.length, baud) * 1.5 + slack:
            return []
        self._frame = None
        self._count(f.kind, False)
        self.bytes_frame -= len(f.buf)
        events = [FrameError(f.seq, "truncated: %d of %d bytes" % (len(f.buf), f.length),
                             f.kind)]
        events += self.feed(bytes(f.buf), now)
        return events

    def _junk(self, raw: bytes) -> Junk:
        self.junk_lines += 1
        return Junk(raw.decode("latin-1"))

    def _take_line(self, now: float):
        raw = bytes(self._line).rstrip(b"\r\n")
        self._line.clear()
        if not raw:
            return None
        for kind in PACKETS:
            if raw.startswith(b"#!" + kind.encode() + b" "):
                return self._start_packet(kind, raw, now)
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return self._junk(raw)
        ev = parse_line(text)
        if isinstance(ev, Junk):
            self.junk_lines += 1
        return ev

    def _count(self, kind: str, ok: bool):
        name = ("frames_" if kind == "FRAME" else "blobs_") + ("ok" if ok else "bad")
        setattr(self, name, getattr(self, name) + 1)

    def _start_packet(self, kind: str, raw: bytes, now: float):
        """A packet header: every kind ends in len= crc= and is closed by
        #!END. What else the header must say, and how long the payload may
        be, is the kind's own check (PACKETS). A header that fails it is junk,
        and its payload then arrives as junk lines until the parser is back
        in step."""
        kv = parse_kv(raw.decode("ascii", "replace")[len(kind) + 3:], convert=False)
        try:
            seq, length, crc = int(kv["seq"]), int(kv["len"]), int(kv["crc"], 16)
            ok = PACKETS[kind][0](kv, length)
        except (KeyError, ValueError):
            ok = False
        if not ok or not 0 < length <= 1 << 20:
            return self._junk(raw)
        self._frame = _Pending(kind, seq, kv, length, crc, now)
        return None

    def _finish_frame(self):
        f = self._frame
        self._frame = None
        data = bytes(f.buf)
        if crc32(data) != f.crc:
            self._count(f.kind, False)
            return FrameError(f.seq, "CRC mismatch", f.kind)
        try:
            ev = PACKETS[f.kind][1](f.seq, f.kv, data)
        except (KeyError, ValueError) as e:
            self._count(f.kind, False)
            return FrameError(f.seq, "bad header field: %s" % e, f.kind)
        self._count(f.kind, True)
        return ev


# --------------------------------------------------------------------------
# Packet kinds: (header check, payload -> event). Both raise KeyError or
# ValueError on a missing or malformed field.

def _frame_ok(kv: dict, length: int) -> bool:
    w, h = int(kv["w"]), int(kv["h"])
    return kv["fmt"] in ("rgb565be", "rgb565le") and length == w * h * 2


def _frame_event(seq: int, kv: dict, data: bytes) -> Frame:
    return Frame(seq, int(kv["t_ms"]), int(kv["w"]), int(kv["h"]), kv["fmt"], data)


def _ints(s: str, n: int) -> tuple:
    v = tuple(int(x) for x in s.split(","))
    if len(v) != n:
        raise ValueError("%r: want %d values" % (s, n))
    return v


def _blob_ok(kv: dict, length: int) -> bool:
    cw = int(kv["cw"])
    return kv["fmt"] == "rgb565le" and length == cw * cw * 2 and cw == CROP


def _blob_event(seq: int, kv: dict, data: bytes) -> BlobRec:
    feat = np.array([float(x) for x in kv["feat"].split(",")], dtype=np.float32)
    cx, cy = (float(x) for x in kv["c"].split(","))
    crop = np.frombuffer(data, dtype="<u2").reshape(CROP, CROP).astype(np.uint16)
    wb, gain = wb_of(kv) if "wb" in kv else ("unrecorded", UNKNOWN_GAIN)
    return BlobRec(seq, int(kv["t_ms"]), int(kv["k"]), int(kv["n"]), int(kv["stage"]),
                   int(kv["csv"]), int(kv["fv"]), kv["fw"], cx, cy, int(kv["area"]),
                   _ints(kv["box"], 4), _ints(kv["crop"], 3), float(kv["conf"]), feat, crop,
                   wb, gain)


PACKETS = {"FRAME": (_frame_ok, _frame_event), "BLOB": (_blob_ok, _blob_event)}


def encode_blob(seq: int, t_ms: int, k: int, n: int, feat, crop: np.ndarray,
                cx: float, cy: float, area: int, box, crop_box, stage: int = 1,
                conf: float = -1.0, fw: str = "fake", fv: int = FEAT_VERSION,
                wb: str = "auto", wb_gain=UNKNOWN_GAIN) -> bytes:
    """A blob packet exactly as the firmware's send_blob_records() puts it on
    the wire. Used by the fake camera and the tests."""
    data = np.ascontiguousarray(crop, dtype="<u2").tobytes()
    head = ("#!BLOB seq=%d t_ms=%d k=%d n=%d stage=%d csv=%d fv=%d fw=%s c=%.2f,%.2f "
            "area=%d box=%d,%d,%d,%d crop=%d,%d,%d conf=%.3g feat=%s wb=%s wb_gain=%d,%d,%d "
            "cw=%d fmt=rgb565le len=%d crc=%08x\r\n") % (
        seq, t_ms, k, n, stage, CSV_VERSION, fv, fw, cx, cy, area, *box, *crop_box, conf,
        ",".join("%.6g" % float(v) for v in feat), wb, *wb_gain, CROP, len(data), crc32(data))
    return head.encode() + data + ("\r\n#!END seq=%d\r\n" % seq).encode()


def encode_frame(seq: int, t_ms: int, data: bytes, w: int = 160, h: int = 120,
                 big_endian: bool = True) -> bytes:
    """A frame packet exactly as link_send_frame() puts it on the wire (with the
    console's \\r\\n line endings). Used by the fake camera and the tests."""
    head = "#!FRAME seq=%d t_ms=%d w=%d h=%d fmt=%s len=%d crc=%08x\r\n" % (
        seq, t_ms, w, h, "rgb565be" if big_endian else "rgb565le", len(data), crc32(data))
    return head.encode() + data + ("\r\n#!END seq=%d\r\n" % seq).encode()
