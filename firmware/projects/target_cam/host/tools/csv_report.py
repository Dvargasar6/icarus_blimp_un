#!/usr/bin/env python3
"""
Does the camera hold its 80 ms pace, and what does each stage cost?

The hard requirement of the learned detector is dt_ms = 80.0 on EVERY row with
every stage enabled. This script is the check: it reads a recorded CSV (or
records one from the port) and reports

  pacing   rows/s from the camera's own t_ms, and every row whose dt_ms is not
           the period. Exit status 1 if there is even one.
  timing   p50 / p99 / max of dt_ms, proc_us and the v2 stage columns. loop_us
           is the CPU's busy time, the print call included, so max(loop_us)
           against the 80 000 us period is the CPU margin that is left.
  wire     the row's time on the wire. A separate budget: printf returns once
           the row is in the UART's 128 B hardware FIFO, so the last 128 B go
           out after loop_us has stopped counting.
  link     rows lost on the wire (a t_ms gap of two periods with no row) and
           lines that did not parse. A lost row is a link fault, not a pacing
           fault: the camera's dt for the next row still reads 80.
  scene    ok-rate, and "jumps": blob 1 moving more than --jump-px between two
           consecutive ok rows. Jumps are how false detections show up in the
           output (a wood patch winning for one frame), so they are counted
           here for the before/after comparison of each stage.

Usage:
  csv_report.py data/flight_20261014_101500.csv
  csv_report.py --port /dev/ttyUSB0 --seconds 120 --cmd '!stage 0'   (single quotes: bash expands ! inside double quotes)

With --port the port is opened WITHOUT resetting the camera (DTR and RTS held
low before open, as the viewer does), each --cmd is sent on its own and must be
ACKed, and the rows are saved under target_cam/data/ with the camera's #!CFG
line at the top as a '#' comment, so the file records what produced it.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "viewer"))
from tcview import protocol as P        # noqa: E402  (one copy of the columns)

DATA_DIR = HERE.parents[1] / "data"     # target_cam/data, as the viewer uses

TIMING = ["dt_ms", "proc_us", "t_px_us", "t_b_us", "t_c_us", "loop_us"]


def pct(vals: list, q: float) -> float:
    """Nearest-rank percentile; NaN for no data."""
    v = sorted(x for x in vals if math.isfinite(x))
    if not v:
        return float("nan")
    k = max(0, min(len(v) - 1, math.ceil(q / 100.0 * len(v)) - 1))
    return v[k]


def read_file(path: Path):
    """-> (rows, cfg dict or None, unparsed line count). Header lines, '#'
    lines and blank lines are skipped; the last #!CFG seen is kept."""
    rows, cfg, bad = [], None, 0
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("t_ms,"):
            continue
        if line.startswith("#"):
            body = line.lstrip("# ")
            if body.startswith("!CFG "):
                cfg = P.parse_kv(body[5:])
            continue
        r = P.parse_row(line)
        if r is None:
            bad += 1
        else:
            r["_bytes"] = len(line) + 2          # the console sends CRLF
            rows.append(r)
    return rows, cfg, bad


def record(port: str, seconds: float, cmds: list, out: Path, poke: float = 0.0):
    import serial
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, P.BOOT_BAUD, 0.1
    s.dtr = False
    s.rts = False
    s.open()
    buf = bytearray()

    def lines(until: float):
        while time.monotonic() < until:
            buf.extend(s.read(4096))
            while b"\n" in buf:
                i = buf.index(b"\n")
                raw = bytes(buf[:i]).rstrip(b"\r")
                del buf[:i + 1]
                yield raw.decode("ascii", "replace")

    def send(cmd: str) -> dict | None:
        """One command, '\\n' first to flush a half line, wait for its ACK.
        Returns the #!CFG that follows, if any."""
        for _ in range(2):
            s.write(b"\n" + cmd.encode() + b"\n")
            acked = False
            for ln in lines(time.monotonic() + 2.0):
                if ln == "#!ACK " + cmd:
                    acked = True
                elif ln.startswith("#!NAK " + cmd):
                    sys.exit("camera refused %r: %s" % (cmd, ln))
                elif acked and ln.startswith("#!CFG "):
                    return P.parse_kv(ln[6:])
                elif ln.startswith("#!NAK "):
                    print(ln)              # a NAK for some other line: say which
                elif ln.startswith("#") and not ln.startswith("#!"):
                    print(ln)
            if acked:
                return None
        sys.exit("no ACK for %r: is the camera streaming at 115200?" % cmd)

    for c in cmds:
        send(c)
    cfg = send("k")
    cfg_line = " ".join("%s=%s" % kv for kv in (cfg or {}).items())
    # Rows queued while the commands were answered are not part of the run.
    s.reset_input_buffer()
    buf.clear()
    out.parent.mkdir(parents=True, exist_ok=True)
    rows, bad, pokes = [], 0, 0
    with open(out, "w") as f:
        f.write("# !CFG %s\n" % cfg_line)
        header = False
        t_end = time.monotonic() + seconds
        last_print = 0.0
        next_poke = time.monotonic() + poke if poke else float("inf")
        for ln in lines(t_end):
            if time.monotonic() >= next_poke:
                # 'k' is answered with an ACK and the ~400 B #!CFG line: the
                # heaviest thing a flight command makes the camera print. The
                # rows around it must still read 80.
                next_poke += poke
                s.write(b"\nk\n")
                pokes += 1
            if ln.startswith("#") and not ln.startswith("#!"):
                f.write(ln + "\n")             # comments (dt_diag) are kept
                print(ln, file=sys.stderr)
                continue
            if not ln or not ln[0].isdigit():
                continue
            r = P.parse_row(ln)
            if r is None:
                bad += 1
                continue
            if not header:
                f.write(",".join(P.columns_for(int(r["csv_v"]))) + "\n")
                header = True
            f.write(ln + "\n")
            r["_bytes"] = len(ln) + 2
            rows.append(r)
            if time.monotonic() - last_print > 5.0:
                last_print = time.monotonic()
                print("  %4.0f s left, %d rows" % (t_end - last_print, len(rows)),
                      file=sys.stderr)
    s.close()
    if pokes:
        print("sent 'k' %d times during the run" % pokes)
    return rows, cfg, bad


def report(rows: list, cfg: dict | None, bad: int, period: float, jump_px: float,
           source: str) -> int:
    if len(rows) < 2:
        print("%s: %d rows - nothing to report" % (source, len(rows)))
        return 2
    versions = sorted({int(r["csv_v"]) for r in rows})
    span = (rows[-1]["t_ms"] - rows[0]["t_ms"]) / 1000.0
    print("source        %s" % source)
    print("rows          %d over %.1f s (camera clock), csv v%s"
          % (len(rows), span, "/".join(map(str, versions))))
    if cfg:
        keys = ("stage", "stage_eff", "lut", "clf", "cnn", "fw", "period_ms", "sensor_us",
                "image", "baud", "stream", "collect", "settings")
        print("config        " + " ".join("%s=%s" % (k, cfg[k]) for k in keys if k in cfg))
        if cfg.get("image") != 0 or cfg.get("baud") != P.BOOT_BAUD or cfg.get("collect", 0):
            print("              NOT flight-identical (image, baud or collect): "
                  "timing below is not the flight figure")

    # Pacing. The first row after boot has dt 0 (no previous capture).
    paced = [r for r in rows if r["dt_ms"] > 0]
    off = [r for r in paced if abs(r["dt_ms"] - period) >= 0.05]
    print("rate          %.3f rows/s from t_ms (target %.3f)"
          % ((len(rows) - 1) / span if span > 0 else 0.0, 1000.0 / period))
    print("pacing        dt_ms != %g: %d of %d rows   %s"
          % (period, len(off), len(paced), "FAIL" if off else "PASS"))
    for r in off[:10]:
        print("                t_ms %d  dt_ms %.1f  stage %d  loop_us %s"
              % (r["t_ms"], r["dt_ms"], r["stage"], r["loop_us"]))
    if len(off) > 10:
        print("                ... and %d more" % (len(off) - 10))

    # Link.
    missing = 0
    for a, b in zip(rows, rows[1:]):
        gap = b["t_ms"] - a["t_ms"]
        if gap > 1.5 * period:
            missing += int(round(gap / period)) - 1
    print("link          rows lost on the wire: %d; lines that did not parse: %d"
          % (missing, bad))

    stages = {}
    for r in rows:
        stages[int(r["stage"])] = stages.get(int(r["stage"]), 0) + 1
    print("stage         " + ", ".join("%d: %d rows" % kv for kv in sorted(stages.items())))

    print("timing            p50       p99       max")
    for k in TIMING:
        vals = [r[k] for r in (paced if k == "dt_ms" else rows)]
        if not any(math.isfinite(v) for v in vals):
            print("  %-10s  (not in csv v1)" % k)
            continue
        print("  %-10s %8.1f  %8.1f  %8.1f" % (k, pct(vals, 50), pct(vals, 99), pct(vals, 100)))
    lmax = pct([r["loop_us"] for r in rows], 100)
    if math.isfinite(lmax):
        print("  margin     %.1f ms of the %g ms period left at the worst loop"
              % ((period * 1000 - lmax) / 1000.0, period))

    nb = [r["_bytes"] for r in rows if "_bytes" in r]
    baud = (cfg or {}).get("baud", P.BOOT_BAUD)
    if nb:
        wire = max(nb) * 10.0 / baud * 1000.0      # 8N1: 10 bits per byte
        print("wire          row %.0f B mean, %d B max = %.1f ms of the %g ms period at %d baud"
              % (sum(nb) / len(nb), max(nb), wire, period, baud))

    # Scene.
    ok = sum(1 for r in rows if r["ok"])
    jumps = 0
    for a, b in zip(rows, rows[1:]):
        if a["ok"] and b["ok"] and b["t_ms"] - a["t_ms"] <= 1.5 * period:
            if math.hypot(b["cx_px"] - a["cx_px"], b["cy_px"] - a["cy_px"]) > jump_px:
                jumps += 1
    print("scene         ok %.1f %% of rows; jumps (blob 1 moved > %g px between "
          "consecutive ok rows): %d" % (100.0 * ok / len(rows), jump_px, jumps))
    return 1 if off else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("csv", nargs="?", type=Path, help="a recorded CSV file")
    ap.add_argument("--port", help="record from this port instead of reading a file")
    ap.add_argument("--seconds", type=float, default=120.0, help="recording length (--port)")
    ap.add_argument("--cmd", action="append", default=[],
                    help="send this command first and wait for its ACK (repeatable, --port)")
    ap.add_argument("--poke", type=float, default=0.0, metavar="S",
                    help="send 'k' every S seconds while recording: commands in "
                    "flight must not move a row off 80 ms (--port)")
    ap.add_argument("--out", type=Path, help="where to save the recording "
                    "(default target_cam/data/run_<time>.csv)")
    ap.add_argument("--period", type=float, help="expected dt_ms (default: the camera's "
                    "period_ms from #!CFG, else 80)")
    ap.add_argument("--jump-px", type=float, default=10.0)
    a = ap.parse_args()
    if (a.csv is None) == (a.port is None):
        ap.error("give a CSV file or --port, not both")
    if a.port:
        out = a.out or DATA_DIR / time.strftime("run_%Y%m%d_%H%M%S.csv")
        rows, cfg, bad = record(a.port, a.seconds, a.cmd, out, a.poke)
        source = "%s (recorded from %s)" % (out, a.port)
    else:
        rows, cfg, bad = read_file(a.csv)
        source = str(a.csv)
    period = a.period or float((cfg or {}).get("period_ms", 80.0))
    return report(rows, cfg, bad, period, a.jump_px, source)


if __name__ == "__main__":
    sys.exit(main())
