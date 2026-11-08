#!/usr/bin/env python3
"""
Watch the CSV stream from the ESP32-CAM forward colour-target firmware.

  live     one refreshing line: bearing, size, range, and a small horizontal
           gauge showing where the target sits across the field of view
  log      append raw CSV to a file, for a measured run you want to analyse

The firmware's '#' lines (banner, command replies, 'm' mask dumps) are passed
through as-is, so a mask dump prints as a picture between status lines.

Usage:
  ./target_monitor.py --port /dev/ttyUSB0
  ./target_monitor.py --port /dev/ttyUSB0 --cmd c            # calibrate on centre
  ./target_monitor.py --port /dev/ttyUSB0 --cmd "p blue" --log run1.csv
"""
import argparse
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

# v1 (25 columns, no version) and v2 (35, ending in csv_v=2). The same lists
# as viewer/tcview/protocol.py; this script stays standalone on purpose.
COLUMNS_V1 = ["t_ms", "ok", "cx_px", "cy_px", "az_deg", "el_deg", "area_px",
              "w_px", "h_px", "fill", "range_mm", "clipped", "blobs", "matched_px",
              "b2_az", "b2_el", "b2_range_mm", "b3_az", "b3_el", "b3_range_mm",
              "b4_az", "b4_el", "b4_range_mm", "dt_ms", "proc_us"]
COLUMNS_V2 = COLUMNS_V1 + ["stage", "conf", "b2_conf", "b3_conf", "b4_conf",
                           "t_px_us", "t_b_us", "t_c_us", "loop_us", "csv_v"]

GAUGE = 31                      # characters across the field of view
HFOV_DEG = 54.0                 # only for drawing the gauge


def parse_row(line):
    parts = line.split(",")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == len(COLUMNS_V1):
        return dict(zip(COLUMNS_V1, vals), stage=0, loop_us=-1, csv_v=1)
    if len(parts) == len(COLUMNS_V2) and vals[-1] == 2:
        return dict(zip(COLUMNS_V2, vals))
    return None


def gauge(row):
    """'[...2...|...1.....]': blobs by azimuth, 1 = largest (usually nearest)."""
    g = ["."] * GAUGE
    g[GAUGE // 2] = "|"

    def mark(az, ch):
        i = int(round((az / HFOV_DEG + 0.5) * (GAUGE - 1)))
        g[max(0, min(GAUGE - 1, i))] = ch

    if row["ok"]:
        for k in range(4, 1, -1):            # draw smaller ones first
            if k <= row["blobs"]:
                mark(row["b%d_az" % k], str(k))
        mark(row["az_deg"], "1" if not row["clipped"] else ">")
    return "[" + "".join(g) + "]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--log", help="append raw CSV rows to this file")
    ap.add_argument("--cmd", action="append", default=[],
                    help="send this command on connect (repeatable)")
    ap.add_argument("--seconds", type=float,
                    help="stop after this long (default: run until Ctrl-C)")
    args = ap.parse_args()

    logf = open(args.log, "a") if args.log else None
    ser = serial.Serial(args.port, args.baud, timeout=1.0)

    # The ESP32 resets when the port opens, then spends 5 s in its reflash
    # window and ~1 s settling the sensor. Commands sent before that are lost,
    # so wait for the first CSV row before sending any.
    pending = list(args.cmd)

    started = time.time()
    rows = 0
    ok_rows = 0
    recent = []

    print("# connected to %s at %d baud; Ctrl-C to stop" % (args.port, args.baud))
    try:
        while True:
            if args.seconds and time.time() - started > args.seconds:
                break
            raw = ser.readline().decode("utf-8", "replace").strip()
            if not raw:
                continue
            if raw.startswith("#") or raw.startswith("I (") or \
               raw.startswith("W (") or raw.startswith("E ("):
                sys.stdout.write("\n" + raw)
                sys.stdout.flush()
                continue

            row = parse_row(raw)
            if row is None:
                sys.stdout.write("\n?? " + raw)
                continue

            while pending:
                ser.write((pending.pop(0) + "\n").encode())
                time.sleep(0.05)

            rows += 1
            if logf:
                logf.write(raw + "\n")
                logf.flush()
            if row["ok"]:
                ok_rows += 1
            recent.append(row)
            if len(recent) > 20:
                recent.pop(0)
            fps = (len(recent) - 1) / max(
                1e-6, (recent[-1]["t_ms"] - recent[0]["t_ms"]) / 1000.0
            ) if len(recent) > 1 else 0.0

            if row["ok"]:
                rng = ("%5.2f m" % (row["range_mm"] / 1000.0)
                       if row["range_mm"] >= 0 else "  --   ")
                status = ("az %+6.1f el %+6.1f deg  %4d px  %3dx%-3d fill %.2f  "
                          "range %s  blobs %d"
                          % (row["az_deg"], row["el_deg"], row["area_px"],
                             row["w_px"], row["h_px"], row["fill"], rng,
                             row["blobs"]))
            else:
                status = "no target  (matched %d px)" % row["matched_px"]

            sys.stdout.write("\r%s %s  %4.1f FPS  %4.0f us  stage %d  loop %s    "
                             % (gauge(row), status, fps, row["proc_us"], row["stage"],
                                "%.1f ms" % (row["loop_us"] / 1000.0)
                                if row["loop_us"] >= 0 else "-"))
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        if logf:
            logf.close()
        print("\n# %d rows, %d with a target (%.1f%%)"
              % (rows, ok_rows, 100.0 * ok_rows / rows if rows else 0.0))


if __name__ == "__main__":
    main()
