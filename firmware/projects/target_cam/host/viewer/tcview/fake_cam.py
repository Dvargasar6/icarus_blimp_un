"""
A stand-in for the camera on a pseudo-terminal, so the viewer can be developed
and tested with no hardware: `python -m tcview --fake`.

It speaks the firmware's protocol (CSV rows, #!CFG/#!ACK/#!NAK, frames, baud
changes with the 3 s revert) and runs every synthetic frame through the real C
detector, so its rows are what the camera would print for that picture. It is
NOT a model of the OV2640: the scene is a drawn red disc drifting over a grey
wall with a brown "wood" patch, chosen so the red-vs-wood confusion seen on the
bench on 2026-10-13 can be reproduced on the PC.

Wire timing is simulated: a frame takes len*10/baud seconds to "send".
"""
from __future__ import annotations

import math
import os
import pty
import threading
import time
import tty

import numpy as np

from . import detector as D
from .protocol import (BAUDS, BOOT_BAUD, COLUMNS, CSV_VERSION, FEAT_VERSION, encode_blob,
                       encode_frame, frame_seconds)

W, H = 160, 120
# The sensor is the clock, as on the camera: a frame every 79.968 ms at XCLK
# 20 MHz, and the rows come every n-th one ('f' picks n).
SENSOR_US = 1599360.0 / 20
COLLECT_EVERY = 4       # target_main.c: records on every 4th frame
DEFAULT = dict(hue=0, tol=12, sat=110, val=50, area=25, mm=550, fov=54.0,
               every=1, awb=1, be=1, preset="red", stage=0, cnn_k=2, flip=0,
               wb_fixed=(64, 64, 64))
WB_NAMES = {0: "frozen", 1: "auto", 2: "fixed"}     # target_main.c wb_name()


def _rgb565(r, g, b) -> np.ndarray:
    r = np.clip(r, 0, 255).astype(np.uint16)
    g = np.clip(g, 0, 255).astype(np.uint16)
    b = np.clip(b, 0, 255).astype(np.uint16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


class Scene:
    """A red balloon drifting in a circle, a wood patch lower-left, a grey
    gradient wall and sensor noise."""

    def __init__(self, seed: int = 1):
        self.rng = np.random.default_rng(seed)
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        self.xx, self.yy = xx, yy
        wall = 150 + 30 * (xx / W) - 20 * (yy / H)
        self.base = np.stack([wall, wall * 0.98, wall * 0.95], axis=-1)
        wood = (xx < 45) & (yy > 80)
        self.base[wood] = (150, 95, 60)

    def render(self, t: float) -> np.ndarray:
        img = self.base.copy()
        cx = W / 2 + 35 * math.cos(t * 0.4)
        cy = H / 2 + 20 * math.sin(t * 0.4)
        r = 14 + 4 * math.sin(t * 0.13)
        d = np.hypot(self.xx - cx, self.yy - cy)
        disc = d <= r
        shade = np.clip(1.0 - 0.5 * (self.xx - cx + r) / (2 * r), 0.45, 1.0)
        img[disc, 0] = 210 * shade[disc]
        img[disc, 1] = 30 * shade[disc]
        img[disc, 2] = 40 * shade[disc]
        hl = np.hypot(self.xx - (cx - r / 3), self.yy - (cy - r / 3)) < r / 5
        img[hl & disc] = (245, 225, 225)
        img += self.rng.normal(0, 4, img.shape)
        return _rgb565(img[..., 0], img[..., 1], img[..., 2])


class FakeCamera:
    def __init__(self, corrupt_every: int = 0, noisy_fast: bool = False):
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.port = os.ttyname(slave)
        self._slave = slave
        self.cfg = dict(DEFAULT)
        self.saved = None
        self.baud = BOOT_BAUD
        self.image = False
        self.grab = False
        self.stream = True
        self.calibrate = False
        self.wbcal = False
        self.collect = False
        self.collect_n = 0
        self.revert_at = 0.0
        self.corrupt_every = corrupt_every
        self.noisy_fast = noisy_fast
        self._noise = np.random.default_rng(7)
        self.seq = 0
        self.loop_us = 0
        self.scene = Scene()
        self._stop = threading.Event()
        self._rx = bytearray()
        self._wlock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    # ---- output -----------------------------------------------------------

    def _write(self, data: bytes):
        if self.noisy_fast and self.baud != BOOT_BAUD:
            # The likeliest reading of the 2026-10-17 hang on the ESP32-CAM-MB
            # (inferred, not measured): above 115200 and under image traffic
            # the camera runs on but its output arrives unreadable, while the
            # short commands still get through.
            data = bytes(self._noise.integers(0, 256, max(1, len(data) // 8), dtype=np.uint8))
        with self._wlock:
            view = memoryview(data)
            while view:
                try:
                    k = os.write(self.master, view)
                except BlockingIOError:
                    time.sleep(0.001)
                    continue
                except OSError:
                    return
                view = view[k:]

    def _print(self, s: str):
        self._write(s.replace("\n", "\r\n").encode())

    def _settings_state(self) -> str:
        ref = self.saved if self.saved is not None else DEFAULT
        if self.cfg != ref:
            return "modified"
        return "saved" if self.saved is not None else "defaults"

    def _print_cfg(self):
        c = self.cfg
        self._print("#!CFG w=%d h=%d cell=2 hue=%d tol=%d sat=%d val=%d area=%d mm=%d "
                    "fov=%.1f fps=%.3g period_ms=%.1f sensor_us=%.0f awb=%d wb=%s "
                    "wb_gain=%d,%d,%d wb_fixed=%d,%d,%d be=%d preset=%s baud=%d "
                    "image=%d stream=%d settings=%s stage=%d stage_eff=%d lut=%s lut_id=%d "
                    "clf=none cnn=none cnn_k=%d csv=%d fw=fake collect=%d fv=%d flip=%d\n"
                    % (W, H, c["hue"], c["tol"], c["sat"], c["val"], c["area"], c["mm"],
                       c["fov"], 1000.0 / self._period_ms(), self._period_ms(), SENSOR_US,
                       c["awb"], WB_NAMES[c["awb"]], *self._wb_gain(), *c["wb_fixed"],
                       c["be"], c["preset"], self.baud, int(self.image),
                       int(self.stream), self._settings_state(), c["stage"],
                       self._stage_eff(), "ok" if D.models().lut else "none",
                       D.models().lut_id, c["cnn_k"], CSV_VERSION, int(self.collect),
                       FEAT_VERSION, c["flip"]))

    def _wb_gain(self) -> tuple:
        """The gains in use, 0,0,0 = not known: as wb_gain_in_use()."""
        return tuple(self.cfg["wb_fixed"]) if self.cfg["awb"] == 2 else (0, 0, 0)

    def _period_ms(self) -> float:
        return self.cfg["every"] * SENSOR_US / 1000.0

    def _stage_eff(self) -> int:
        return D.lib().tc_stage_effective(self.cfg["stage"], D.models())

    # ---- commands ---------------------------------------------------------

    def _set_int(self, key, arg, lo, hi, custom=False):
        try:
            v = int(arg)
        except ValueError:
            return "out of range (%d..%d)" % (lo, hi)
        if not lo <= v <= hi:
            return "out of range (%d..%d)" % (lo, hi)
        self.cfg[key] = v
        if custom:
            self.cfg["preset"] = "custom"
        return None

    def _command(self, line: str):
        """Returns ('ok'|'later', None) or ('nak', reason)."""
        c, arg = line[0], line[1:].strip()
        err = None
        later = False
        if c in "comskg?" and len(line) > 1:
            # As on the camera: an echoed fragment of its own output must not
            # act as a command because it happens to start with one.
            err = "'%s' takes no argument" % c
        elif line == "!save":
            self.saved = dict(self.cfg)
        elif line == "!load":
            if self.saved is None:
                err = "nothing saved in flash"
            else:
                self.cfg = dict(self.saved)
        elif line == "!reset":
            self.saved = None
            self.cfg = dict(DEFAULT)
        elif line.split(" ")[0] == "!stage":
            a = line[7:] if line[6:7] == " " else ""
            if not (a.strip().isdigit() and 0 <= int(a) <= D.TC_STAGE_MAX):
                err = "usage: !stage <0-%d>" % D.TC_STAGE_MAX
            else:
                self.cfg["stage"] = int(a)
        elif line.split(" ")[0] == "!collect":
            a = line[9:] if line[8:9] == " " else ""
            if a.strip() not in ("0", "1"):
                err = "usage: !collect <0|1>"
            else:
                self.collect = a.strip() == "1"
                self.collect_n = 0
        elif line.split(" ")[0] == "!flip":
            a = line[6:] if line[5:6] == " " else ""
            if a.strip() not in ("0", "1"):
                err = "usage: !flip <0|1>"
            else:
                self.cfg["flip"] = int(a)
        elif c == "!":
            err = "unknown (!save !load !reset !stage !collect !flip)"
        elif c == "H":
            err = self._set_int("hue", arg, 0, 359, True)
        elif c == "T":
            err = self._set_int("tol", arg, 1, 90, True)
        elif c == "S":
            err = self._set_int("sat", arg, 0, 255, True)
        elif c == "V":
            err = self._set_int("val", arg, 0, 255, True)
        elif c == "a":
            err = self._set_int("area", arg, 1, W * H)
        elif c == "d":
            err = self._set_int("mm", arg, 0, 5000)
        elif c == "f":
            # Decimals allowed, rounded to whole sensor frames like the camera:
            # at least one, at most what fits in a second.
            try:
                v = float(arg)
                assert 1.0 <= v <= 30.0
                n = int(1e6 / v / SENSOR_US + 0.5)
                self.cfg["every"] = max(1, min(int(1e6 / SENSOR_US), n))
            except (ValueError, AssertionError):
                err = "out of range (1..30)"
        elif c == "w":
            err = self._set_int("awb", arg, 0, 2)
        elif c == "W":
            # The real gains change the sensor's colour; the synthetic scene
            # does not model that, only the protocol around it.
            if arg == "":
                self._print("#!WB mode=%s ctrl1=253 c7=%d cc=%d cd=%d ce=%d\n" % (
                    WB_NAMES[self.cfg["awb"]], 64 if self.cfg["awb"] == 2 else 0,
                    *self.cfg["wb_fixed"]))
            elif arg == "c":
                if self.wbcal:
                    err = "a white-card calibration is already running"
                else:
                    self.wbcal = True
                    later = True
            else:
                try:
                    g = tuple(int(x) for x in arg.split(","))
                    assert len(g) == 3 and all(1 <= x <= 255 for x in g)
                    self.cfg.update(wb_fixed=g, awb=2)
                except (ValueError, AssertionError):
                    err = "usage: W | Wc | W<r>,<g>,<b> (1..255, 64 = 1.0)"
        elif c == "F":
            try:
                v = float(arg)
                assert 10.0 <= v <= 150.0
                self.cfg["fov"] = round(v, 1)
            except (ValueError, AssertionError):
                err = "out of range (10..150)"
        elif c == "p":
            name = arg.strip().lower()
            idx = int(name) if name.isdigit() else next(
                (i for i, p in enumerate(D.PRESETS) if name and p[0] == name[0]), -1)
            if not 0 <= idx < len(D.PRESETS):
                err = "unknown preset (red, blue, green, orange)"
            else:
                h, t, s, v = D.preset(idx)
                self.cfg.update(hue=h, tol=t, sat=s, val=v, preset=D.PRESETS[idx])
        elif c == "o":
            self.cfg["be"] ^= 1
        elif c == "s":
            self.stream = not self.stream
        elif c == "v":
            if arg in ("0", "1"):
                self.image = arg == "1"
            else:
                err = "out of range (0..1)"
        elif c == "g":
            self.grab = True
        elif c == "c":
            self.calibrate = True
            later = True
        elif c == "B":
            try:
                v = int(arg)
            except ValueError:
                v = -1
            if v not in BAUDS:
                err = "unsupported (115200 230400 460800 921600 1500000 2000000)"
            else:
                return ("baud", v)
        elif c in "km?":
            pass
        else:
            err = "unknown command"
        if err:
            return ("nak", err)
        return ("later" if later else "ok", None)

    def _poll_commands(self):
        try:
            self._rx += os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            pass
        while True:
            idx = min((i for i in (self._rx.find(b"\n"), self._rx.find(b"\r")) if i >= 0),
                      default=-1)
            if idx < 0:
                return
            line = self._rx[:idx].decode("latin-1")
            del self._rx[:idx + 1]
            if not line:
                continue
            kind, arg = self._command(line)
            if kind == "nak":
                self._print("# %s\n#!NAK %s -- %s\n" % (arg, line, arg))
                continue
            self.revert_at = 0.0
            self._print("#!ACK %s\n" % line)
            if kind == "ok":
                self._print_cfg()
            elif kind == "baud":
                self._print("#!BAUD %d\n" % arg)
                self.baud = arg
                self.revert_at = time.monotonic() + 3.0 if arg != BOOT_BAUD else 0.0

    # ---- main loop --------------------------------------------------------

    def start(self):
        os.set_blocking(self.master, False)
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        self.thread.join(timeout=2)
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def _run(self):
        self._print("\n\n# ---- target_cam boot ---- (FAKE CAMERA: synthetic scene)\n"
                    "# settings      nothing saved: running Kconfig defaults\n"
                    "# link         crc32 self-test cbf43926 (ok)\n")
        self._print_cfg()
        t0 = time.monotonic()
        prev = None
        nxt = t0
        while not self._stop.is_set():
            nxt += self._period_ms() / 1000.0
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()
            wake = time.perf_counter()
            self._poll_commands()
            if self.revert_at and time.monotonic() >= self.revert_at:
                self.revert_at = 0.0
                self.baud = BOOT_BAUD
                self._print("#!BAUD %d\n# link         reverted\n" % BOOT_BAUD)
            now = time.monotonic()
            t_ms = int((now - t0) * 1000) + 6000
            px = self.scene.render(now - t0)
            if self.cfg["flip"]:
                px = px[::-1, ::-1]         # the sensor turns it, before anything else
            be = bool(self.cfg["be"])
            # The camera always sends high byte first; 'o' only changes how the
            # detector reads it - which is exactly what the 'o' test is for.
            frame = px.astype(">u2").tobytes()
            st = D.Settings(hue=self.cfg["hue"], tol=self.cfg["tol"], sat=self.cfg["sat"],
                            val=self.cfg["val"], area=self.cfg["area"], be=int(be))
            if self.calibrate:
                self.calibrate = False
                col = D.sample(st, frame, (W - D.CAL_PATCH) // 2, (H - D.CAL_PATCH) // 2,
                               D.CAL_PATCH, D.CAL_PATCH)
                cal = D.calibrate_from(col)
                self._print("#!CAL %s hue=%d spread=%d sat=%d val=%d\n" % (
                    "refused" if cal.refused else "ok", col.hue, col.hue_spread,
                    col.sat, col.val))
                if not cal.refused:
                    self.cfg.update(hue=cal.hue, tol=cal.tol, sat=cal.sat, val=cal.val,
                                    preset="custom")
                self._print_cfg()
            if self.wbcal:
                # One step, from the centre's mean: the synthetic wall is grey
                # with a slight warm cast, so the answer is a little more blue.
                self.wbcal = False
                c = px[(H - D.CAL_PATCH) // 2:(H + D.CAL_PATCH) // 2,
                       (W - D.CAL_PATCH) // 2:(W + D.CAL_PATCH) // 2].astype(np.float64)
                m = ((c // 2048).mean() * 255 / 31, ((c // 32) % 64).mean() * 255 / 63,
                     (c % 32).mean() * 255 / 31)
                g = (int(round(64 * m[1] / m[0])), 64, int(round(64 * m[1] / m[2])))
                self.cfg.update(wb_fixed=g, awb=2)
                self._print("#!WBCAL ok r=%d g=%d b=%d R=%.0f G=%.0f B=%.0f steps=1\n"
                            % (*g, *m))
                self._print_cfg()
            t1 = time.perf_counter()
            tr = D.pipeline(st, frame, self.cfg["stage"], timed=True,
                            features=self.collect or self.cfg["stage"] >= 2)
            proc_us = int((time.perf_counter() - t1) * 1e6)
            send_img = self.grab or self.image
            send_rec = False
            if self.collect:
                self.collect_n += 1
                if self.collect_n >= COLLECT_EVERY or send_img:
                    self.collect_n = 0
                    send_rec = True
            if send_img:
                self.grab = False
                pkt = encode_frame(self.seq, t_ms, frame, big_endian=be)
                self.seq += 1
                if self.corrupt_every and self.seq % self.corrupt_every == 0:
                    k = len(pkt) // 2
                    pkt = pkt[:k] + b"W (1234) cam_hal: EV-VSYNC-OVF\r\n" + pkt[k:]
                self._write(pkt)
                time.sleep(frame_seconds(len(pkt), self.baud))
            dt = 0.0 if prev is None else (now - prev) * 1000
            prev = now
            if self.stream:
                self._write(self._row(tr, t_ms, dt, proc_us).encode())
            # After the frame, so the shared seq runs in send order, as on
            # the camera.
            for pkt in (self._records(st, frame, tr, t_ms) if send_rec else []):
                self._write(pkt)
                time.sleep(frame_seconds(len(pkt), self.baud))
            self.loop_us = int((time.perf_counter() - wake) * 1e6)

    def _records(self, st, frame, tr, t_ms) -> list:
        """One #!BLOB packet per reported blob, as send_blob_records()."""
        out = []
        for k in range(tr.n_feat):
            b = tr.r.b[k]
            box = D.crop_box(b)
            out.append(encode_blob(self.seq, t_ms, k, tr.n_feat, list(tr.feat[k].v),
                                   D.crop(st, frame, box), b.cx, b.cy, b.area,
                                   (b.x0, b.y0, b.x1, b.y1), box, stage=tr.stage,
                                   conf=tr.conf[k], wb=WB_NAMES[self.cfg["awb"]],
                                   wb_gain=self._wb_gain()))
            self.seq += 1
        return out

    def _row(self, tr, t_ms, dt, proc_us) -> str:
        r = tr.r
        fov = self.cfg["fov"]
        f = (W / 2) / math.tan(math.radians(fov) / 2)
        mm = self.cfg["mm"]

        def geom(b):
            az = math.degrees(math.atan((b.cx - W / 2) / f))
            el = math.degrees(math.atan((H / 2 - b.cy) / f))
            d = max(b.x1 - b.x0 + 1, b.y1 - b.y0 + 1)
            rng = mm * f / d if mm > 0 and not b.clipped and d > 0 else -1
            return az, el, rng

        b0 = r.b[0]
        az = el = fill = 0.0
        rng = -1.0
        w = h = 0
        if r.found:
            az, el, rng = geom(b0)
            w, h = b0.x1 - b0.x0 + 1, b0.y1 - b0.y0 + 1
            fill = b0.area / (w * h)
        vals = ["%d" % t_ms, "%d" % r.found, "%.2f" % b0.cx, "%.2f" % b0.cy,
                "%.2f" % az, "%.2f" % el, "%d" % b0.area, "%d" % w, "%d" % h,
                "%.2f" % fill, "%.0f" % rng, "%d" % b0.clipped, "%d" % r.n_blobs,
                "%d" % r.matched]
        for k in range(1, 4):
            a2 = e2 = 0.0
            r2 = -1.0
            if k < r.n_report:
                a2, e2, r2 = geom(r.b[k])
            vals += ["%.2f" % a2, "%.2f" % e2, "%.0f" % r2]
        vals += ["%.1f" % dt, "%d" % proc_us]
        vals += ["%d" % tr.stage] + ["%.3g" % c for c in tr.conf]
        vals += ["%d" % tr.t_px_us, "%d" % tr.t_b_us, "%d" % tr.t_c_us,
                 "%d" % self.loop_us, "%d" % CSV_VERSION]
        assert len(vals) == len(COLUMNS)
        return ",".join(vals) + "\r\n"
