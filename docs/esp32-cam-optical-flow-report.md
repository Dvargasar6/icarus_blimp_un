# ESP32-CAM downward optical flow — status report

**Date:** 2026-10-11
**Project:** `firmware/projects/optflow_cam`
**Hardware:** AI-Thinker ESP32-CAM (ESP32-D0WD rev v1.0, 4 MB flash, MAC
24:a1:60:c5:b4:e4) on an ESP32-CAM-MB programmer, ESP-IDF v5.5.5.

This document records what was searched, what was built, how it is configured,
what has been measured on real hardware, and — importantly — which conclusions
are **not** yet established. It supersedes the status claims in
[`optflow_cam/README.md`](../firmware/projects/optflow_cam/README.md), which
still contains three figures that live measurement has since disproved (listed
in §9).

**Current status in one line:** the scale deficit that blocked calibration is
**fixed and verified on hardware**. It was sub-pixel locking in the matcher's
parabolic refinement (§8.11); three estimators were measured on the host, where
the **V-fit** recovers 91% of true distance against the parabola's 51% and the
Gaussian's 22% (§8.13); and a same-rig A/B on real images over a 270 mm
mechanical reference now reads **107.7% with the V-fit against 73.5% with the
parabola** (§8.14). `FLOW_SUBPIX_VFIT` is the committed default. Rolling the camera to
90° and then 43.5° (§8.15) showed that the 10-point out/back spread is **not** a
direction asymmetry but the §8.7 bias projected onto the moving axis, that the
bias is **image-fixed and therefore ours, not the room's**, and that there is **no
cross-axis scale difference** (1.021). At 0.15 px/frame — near the 0.12 px/frame a
2 m hover produces — the host sweep and real images now **agree to 2.6 points**.
**The dominant remaining error is a constant ~4 mm/s image-axis bias; zero-mean
SAD is the fix in the queue.**

---

## 1. Why a camera at all

The [sensor fusion plan](sensor-fusion-plan.md) established arithmetically that
**horizontal position is not observable** with the blimp's existing sensors: at
the measured 70 mg accelerometer bias, dead reckoning drifts 0.34 m in 1 s and
34 m in 10 s. No filter recovers that — the information is simply absent. A new
sensor is required, and a downward-looking camera is the cheapest one that
measures horizontal *velocity* directly rather than integrating acceleration.

The design target is a **2 m hover**, matching the altitude band
(1.85–2.25 m) already chosen for the PING))) 28015 altimeter.

---

## 2. Firmware survey

Five existing ESP32-CAM / optical-flow projects were examined before writing
anything:

| Project | Verdict |
|---|---|
| `espressif/esp32-camera` | **Adopted.** The official sensor driver, Apache-2.0, actively maintained. Every other option depends on it anyway. Pinned as `espressif/esp32-camera: "^2.0.0"` in `main/idf_component.yml`. |
| `thomas-pegot/esp32-motion` | Full-frame Lucas–Kanade plus block motion estimation. Genuinely capable, but built around dense per-pixel motion fields — far more computation than a blimp needs, and no honest-failure reporting. |
| `qqqlab/ESP32-Optical-Flow` | Closest in intent: downward flow for a drone. Used as a **reference for the geometry**, but the code is entangled with that project's own flight stack and not separable. |
| `djtrance/ESP32-CAM-opticalFlow` | Arduino sketch doing 1:1 full-frame correlation. Too slow, and coupled to the Arduino core. |
| `tovask/ESP32-CAM` | Streaming and experiment scaffolding, not a flow estimator. |

**Conclusion of the survey:** none of them is a drop-in sparse block matcher
that reports its own failures, so only the sensor driver was reused and the
matcher was written from scratch — **234 lines** in
`main/flow_match.c`, as portable C with **no ESP-IDF dependency**. That
constraint is the single most valuable decision in the project: it is what made
host-side verification possible before any hardware was available (§6).

---

## 3. Colour or grayscale?

**Grayscale — 8-bit, one byte per pixel, straight out of the sensor. No colour
data is ever captured, transferred, or converted.**

Set in `camera_start()` in `main/optflow_main.c`:

```c
.pixel_format = PIXFORMAT_GRAYSCALE,
.frame_size   = FRAMESIZE_QQVGA,        /* 160 x 120 */
.fb_location  = CAMERA_FB_IN_DRAM,
.fb_count     = 2,
.grab_mode    = CAMERA_GRAB_LATEST,
```

This was verified against the driver source rather than assumed:

- `PIXFORMAT_GRAYSCALE` on an OV2640 is routed through the driver's YUV422
  register set (`sensors/ov2640.c:120-122`) and the **Y (luma) plane is handed
  up directly**. There is no RGB conversion, no JPEG encode, and no JPEG decode
  anywhere in the path.
- A QQVGA grayscale frame is **19 200 bytes**, so two buffers fit in internal
  RAM. `CAMERA_FB_IN_DRAM` allocates with `MALLOC_CAP_INTERNAL`
  (`driver/cam_hal.c:492-495`), and that code path carries **no JPEG-or-PSRAM
  restriction** — confirmed by reading it. The SPI PSRAM is therefore never
  clocked for pixel traffic, which both saves power and lets the firmware run on
  a board with no PSRAM fitted.

Why grayscale is the right choice here, not a compromise: block matching
operates on intensity. Colour would triple the memory traffic and the match
cost while adding no information the matcher can use.

### Sensor tuning

Auto-exposure and auto-gain are deliberately left **on** — floor brightness
varies, and a fixed exposure would blow out or black out the texture the matcher
depends on. Everything that costs cycles or *invents* detail is turned off:

| Setting | Value | Reason |
|---|---|---|
| `set_sharpness` | 0 | Sharpening invents edges that the matcher would track as real features. |
| `set_denoise` | 0 | Denoising smears the very texture being matched. |
| `set_aec2` | 0 | Extra AE algorithm, not needed. |
| `set_gainceiling` | `GAINCEILING_4X` | **Caps auto-gain.** High gain on a dim floor buys brightness by adding noise, and noise is exactly what makes a block match to the wrong place. |
| `set_lenc` | 1 | Lens correction flattens vignetting, which otherwise biases the variance gate toward frame centre. |
| `set_bpc` / `set_wpc` | 1 | Dead-pixel correction — a stuck pixel is a fixed feature that would read as zero motion. |
| `set_whitebal` / `set_awb_gain` | 0 | Meaningless in grayscale. |

---

## 4. Strategy

### 4.1 Pipeline

```
OV2640 ──grayscale QQVGA──> frame buffer (DRAM)
                                  │
                     copy to s_prev at end of each loop
                                  │
            flow_match(prev, cur) ── 5x5 grid of 16x16 blocks
                                  │
       variance gate ──> full SAD surface ──> boundary check
                                  │
          parabolic sub-pixel fit ──> median across blocks
                                  │
              dx_px, dy_px, spread, diagnostic counts
                                  │
        vx = -dx * mm_per_px / dt   (sign: ground left = airframe right)
                                  │
                      CSV line on UART0 @ 115200
```

### 4.2 The matcher, step by step

Per block, in this order — the order matters:

1. **Variance gate.** A block whose intensity variance is below
   `CONFIG_OPTFLOW_MIN_VAR` (default 50) is dropped as featureless and counted
   in `n_flat`. Matching a blank patch produces a confident, meaningless answer,
   so it must never be attempted.
2. **Full SAD surface.** Sum of absolute differences over the whole ±7 px search
   window — **no early exit.** An early exit would be faster but would clamp the
   neighbouring SAD values that the parabola in step 4 needs, corrupting the
   sub-pixel estimate.
3. **Boundary-minimum rejection.** If the SAD minimum sits on the edge of the
   search window, the true motion is probably *outside* it. The block is
   rejected into `n_edge` rather than reported pegged at the limit. This is the
   detectable-failure property that block matching has and Lucas–Kanade does
   not.
4. **Parabolic sub-pixel fit** on the three SAD values around the minimum, per
   axis.
5. **Median across all surviving blocks**, with the median absolute deviation
   reported as `spread`. Median, not mean, so a single false match cannot drag
   the result.

Integer arithmetic throughout — no gradients, no matrix inverse.

### 4.3 Honest failure as a design rule

The firmware header documents three drivers: **HEAT, HONEST FAILURE, NO IMU
HERE.** The second is the one that shaped the code. Every stage above can refuse
to answer, and the refusal is reported with its reason:

| Field | Meaning |
|---|---|
| `ok` | 0 = the frame produced **no usable estimate**. Not "stationary" — *no information*. |
| `n_valid` / `n_total` | blocks that survived, out of the grid |
| `n_flat` | rejected by the variance gate (featureless surface) |
| `n_edge` | SAD minimum on the search boundary (motion too fast) |
| `n_ambig` | ambiguous minimum |
| `spread` | MAD across blocks — the disagreement measure |

This paid off immediately on first power-up: pointed at a white ceiling, the
firmware returned `ok=0, flat=25/25` — refusing to report rather than inventing
motion.

### 4.4 Geometry

`mm_per_pixel() = 2 · height · tan(hfov/2) / width`

| | at 2.0 m (design) | at 1.5 m (bench) |
|---|---|---|
| Ground footprint | 2.04 × 1.53 m | 1.53 × 1.15 m |
| GSD, QQVGA | **12.74 mm/px** | **9.55 mm/px** |
| Max measurable (6 px @ 12.5 FPS) | 956 mm/s | **717 mm/s** |

**The usable search radius is `search − 1`, not `search`.** A displacement of
exactly `search` px puts the SAD minimum on the edge of the window, where the
boundary check (§4.2 step 3) rejects it — verified on the host suite, which
fails its own 5 px and 6 px fixtures when run at `search = 5`. An earlier
version of this table used the full ±7 and so overstated the range by 17%.

*(assuming the nominal 54° h-FOV — see §8, this is not yet calibrated)*

### 4.5 The rotation problem

Rotating the camera by θ shifts the image by `f·θ` pixels, where
`f = (W/2)/tan(hfov/2)` = 157.0 px at QQVGA. Converting to ground distance
multiplies by the GSD, and since `f × GSD ≡ height`, the two height terms
**cancel exactly**:

```
phantom ground velocity = height × ω
                        = 2000 mm × (pi/180) per deg/s
                        ≈ 34.9 mm/s per deg/s      (at 2 m, at any height scaled)
```

So **~35 mm/s of fake translation per 1 °/s of yaw**, and it is
indistinguishable from real motion. A blimp yawing at a gentle 10 °/s
manufactures 0.35 m/s of velocity that does not exist.

**This firmware cannot correct it — there is no IMU on the camera board.** It
therefore reports raw `dx_px`/`dy_px` *alongside* the velocities, so whichever
board owns the gyro can subtract `f·ω·dt` before use. `vx`/`vy` are valid only
while the airframe is not turning.

*(An earlier draft of this figure said 37.7 mm/s per °/s. That was wrong; 34.9
is the correct value, derived above.)*

### 4.6 Thermal strategy

The 10–20 FPS cap is partly a thermal measure, per the original requirement.
ESP32-CAM boards are widely reported above 90 °C under load; the main causes are
the AMS1117 linear regulator dissipating (5 V − 3.3 V) × I, and WiFi transmit
bursts at ~240 mA.

| Measure | Mechanism | Status |
|---|---|---|
| Radios never initialised | no `esp_wifi_*` / `esp_bt_*` call anywhere | **Verified** — `nm` on the linked ELF shows `esp_wifi_init` and `esp_bt_controller_init` absent |
| ~~XCLK 10 MHz, not 20~~ | `CONFIG_OPTFLOW_XCLK_MHZ` | **REVERSED — now 20 MHz.** This was the single largest thermal measure and it has been traded away for 12.5 FPS (§7.3). Sensor clock dissipation roughly doubles. Unmeasured. |
| ~~CPU 160 MHz, not 240~~ | `sdkconfig.defaults` | **REVERSED — now 240 MHz**, required to fit the match inside the shorter 79.95 ms period. |
| QQVGA grayscale | least pixel traffic of any usable format | active |
| Frame pacing | `xTaskDelayUntil` on a measured period | active, but see §9 — idle fraction is far smaller than originally claimed |
| Flash LED forced low | `quiesce_heat_sources()` drives GPIO4 low | active — GPIO4 feeds a 1 W LED that floats bright on some boards |
| Frame buffers in DRAM | PSRAM never clocked for pixels | active |
| Brownout detector on | a supply/heat failure becomes a logged reset, not silent corruption | active |

Note: **die temperature cannot be read in software on this chip.** The original
ESP32 has no usable internal temperature sensor (S2/S3/C3/C6 and later do). An
external probe or IR thermometer on the shield can is required.

---

## 5. Configuration

`main/Kconfig.projbuild`:

| Option | Default | Note |
|---|---|---|
| `OPTFLOW_BOARD` | AI-Thinker | also ESP-EYE, XIAO-S3 maps in `camera_pins.h` |
| `OPTFLOW_TARGET_FPS` | 15 | **inert.** The sensor is slower than any pacing value, so this never binds — the rate is set by XCLK alone. |
| `OPTFLOW_XCLK_MHZ` | **20** | sets the frame rate: 10 MHz → 6.25 FPS, 20 MHz → 12.5 FPS |
| `OPTFLOW_HEIGHT_MM` | 2000 | runtime-settable with `h<mm>` |
| `OPTFLOW_HFOV_DEG` | 54 | **NOT CALIBRATED — see §8** |
| `OPTFLOW_BLOCK` | 16 | |
| `OPTFLOW_SEARCH` | 7 | ±7 px |
| `OPTFLOW_GRID` | 5 | 5×5 = 25 blocks |
| `OPTFLOW_MIN_VAR` | 50 | variance gate |

Runtime UART commands (non-blocking poll, no console component):
`h<mm>` set height · `f<fps>` set pacing (inert, see above) · `r<px>` set the
search radius · `z` zero the integrator · `s` toggle streaming · `?` print
banner.

`r<px>` exists because the match cost scales as `(2·search+1)²` and is what
decides whether the loop keeps up with the sensor — and because reflashing this
board needs the BOOT button physically held (§7.1), so it must be tunable over
serial.

---

## 6. Host verification (no hardware)

> `host/` now contains two programs. `test_flow_match` is the correctness suite
> described below. **`bias_sweep`** (`make -C host sweep`) measures the
> sub-pixel transfer curve, the integrated recovery over many frames, and the
> illumination sensitivity — see §8.11. It is the acceptance test for any change
> to the sub-pixel estimator, and it needs no hardware. `./bias_sweep` runs all
> three estimators (§8.13); `./bias_sweep curve [estimator]` dumps a 201-point
> transfer curve for inverting recorded traces (§8.12).


Because `flow_match.c` has no ESP-IDF dependency, it compiles and runs on the
PC. `host/test_flow_match.c` (202 lines) generates two octaves of value noise
(deterministic LCG, seed 12345), shifts it by known amounts, and checks what
comes back. **Actual output:**

```
1. integer shifts        8/8 exact to 0.00 px
2. sub-pixel shifts      7/7, worst 0.089 px
3. flat grey -> ok=0 valid 0/25 flat 25   ok
4. +8 -> ok=0 edge 25/25 | +10 -> ok=0 edge 25/25 | +12 -> ok=1 dx=-3.71 spread 1.05 (not pegged)
mean |error| 0.024 px over 30 axes, worst 0.089 px
PASSED (0 failures)
```

Test 4 specifically asserts that over-range motion is **not silently pegged** at
the search limit. The 0.089 px worst case corresponds to ~17 mm/s at the 2 m
geometry.

**The algorithm is correct. Everything unresolved in this project is about the
image the sensor actually delivers, not about the matcher.**

---

## 7. Hardware bring-up

### 7.1 The GPIO0 flashing lockout — the main trap

On the AI-Thinker pinout, **XCLK is GPIO0, which is also the download-strap
pin.** Once the firmware starts LEDC on it, GPIO0 becomes a strong push-pull
output and the programmer's DTR line can no longer pull it low. The board is
then unreachable by esptool in every mode.

This is why the board probed fine before the first flash and went completely
silent afterwards. **The BOOT button on the ESP32-CAM-MB (IO0 → GND), held
during reset, is the only way back in.** Any reflash of this board requires it.

**The working recipe, verbatim** — six attempts were wasted rediscovering it:

1. Hold **BOOT** down and keep it held.
2. **While holding BOOT, unplug and replug the USB cable.** This is the reset.
3. Flash with **`--before no_reset --after no_reset`** at **115200**.
4. Release BOOT, then unplug/replug again to run the new firmware.

**esptool's own reset does not work on this programmer**, and the failure is
silent: `--before default_reset` toggles DTR/RTS, nothing happens, the chip
keeps running the application, and esptool reports only
`Failed to connect: No serial data received`. This was *proved*, not assumed —
the board's uptime counter ran straight through a failed attempt (18.5 s →
65.9 s, no reboot), so the reset never reached the chip. On this board the
power cycle **is** the reset.

Two failure modes produce that identical error message and must be ruled out
first: a **stale recorder still holding `/dev/ttyACM0`** (check with `fuser`),
and a **wedged bridge** — enumerated on the bus but mute in both directions,
cleared only by a replug. A quick `?` over pyserial distinguishes them: a live
board answers with its banner.

A hypothesis that the silence was a **brownout** was stated and is **wrong** —
once flashed with BOOT held, the board ran normally on the same supply.

A second, self-inflicted factor: the original firmware printed its banner only
*after* `esp_camera_init()`, so a board dying during camera init emitted zero
bytes. Fixed by adding, before camera init:

- `boot_report()` — reset reason, chip revision, free heap, PSRAM, with a loud
  block on `ESP_RST_BROWNOUT`
- `escape_hatch_delay()` — a 5-second countdown window before the camera (and
  therefore GPIO0) is touched
- explicit logs bracketing `esp_camera_init()` on both success and failure

`app_main()` order is now: `boot_report()` → `quiesce_heat_sources()` → config
validate → buffer alloc → UART install → `escape_hatch_delay()` → camera init →
AE settle (10 frames) → banner → loop.

### 7.2 The programmer

The ESP32-CAM-MB in use is **not** a classic CH340. It enumerates as a CDC-ACM
device, `0843:5740` "FIREPHX USB SER" serial FX2348N, on `/dev/ttyACM0` —
likely a CH9102/CH343 revision.

**Reasoning error worth recording:** from that USB descriptor I concluded
`/dev/ttyACM0` was the ESP32-S3-Zero. That was wrong — a CDC bridge tells you
nothing about what is on the far end, so the descriptor could not support the
conclusion either way.

Serial notes: `idf.py monitor` buffers when not on a TTY, so all bench testing
goes through pyserial helpers that set `dtr=False; rts=False` **before**
`open()`, to avoid resetting the board on connect.

### 7.3 Frame rate — 6.25 FPS raised to 12.5 FPS

The frame rate is set by **XCLK alone**. The OV2640 in this mode consumes a
fixed 1.599 M XCLK cycles per frame (it reads the full array and downsamples to
QQVGA), so the period scales inversely with the sensor clock:

| XCLK | frame period | rate |
|---|---|---|
| 10 MHz | 159.9 ms | 6.25 FPS |
| **20 MHz** | **80.00 ms** | **12.50 FPS** |

Measured after the change: `dt` = **80.00 ms**, with 199 of 200 frames landing
on exactly 80.0 ms. Target achieved precisely.

#### How this was established, including a wrong turn

159.9 ms is exactly 2 × 79.95 ms, which suggested the camera was already
producing 12.5 FPS while the loop — 74–78 ms of matching plus ~7 ms of blocking
`printf` — overran the period and collected every *second* frame.

Two measurements disproved that:

1. **Pacing is not the limiter.** `dt` stayed at 159.9 ms under `f15`, `f25` and
   `f30`, with min = max = 159.9 across 227 frames. Zero jitter.
2. **Loop speed is not the limiter.** Raising the CPU to 240 MHz cut
   `flow_match()` from 74–78 ms to 49–52 ms — a clean 1.5× scaling, leaving the
   loop body around 57 ms, well inside 79.95 ms. **`dt` did not budge.**

A 57 ms loop would have caught every frame of a 79.95 ms stream. It didn't, so
the sensor was genuinely delivering 6.25 FPS and the frame-skipping theory was
wrong. Doubling XCLK was then the only lever, and it worked exactly as the
cycle-count model predicted.

`proc_us` brackets **only** `flow_match()` (`optflow_main.c:458-461`), so it is
pure computation with no frame wait included — which is what made step 2
conclusive.

#### Current budget at 12.5 FPS

| | value |
|---|---|
| Frame period | 80.00 ms |
| `flow_match()` | 47.4–54.6 ms |
| Margin | **25.4 ms** |
| CPU duty | ~65% |
| Valid blocks (textured scene) | 20–23 of 25 |

**The cost is thermal.** XCLK 20 MHz was previously the project's largest
single heat measure, and it has been spent. CPU duty also rose from ~49% at
160 MHz to ~65% at 240 MHz. None of this has been measured with a probe — see
§10.

### 7.4 Operational note: the USB bridge wedges after a fast flash

Twice, immediately after flashing at **460800 baud**, the board went completely
silent: zero bytes at nine different baud rates (38400 → 921600), and esptool
could not connect either. The adapter still enumerated normally (`0843:5740`,
115200), and a line-level reset via RTS did **not** recover it.

**Only unplugging and replugging the USB cable recovered it**, both times. It has
not happened on any slower session, so flash at **115200** to avoid it.

Diagnostic value: a silent *ROM bootloader* means the fault is below the
application. The ROM runs before any project code and knows nothing about
`sdkconfig`, so its silence cannot be caused by a firmware option — which ruled
out the 240 MHz change and pointed at the bridge. (I initially read esptool's
failure as proof the firmware was running and driving GPIO0; that inference was
wrong, since a chip that is not booting at all also answers nothing.)

## 8. Live measurements

Bench setup: camera resting on a flat table facing a wall at a measured
**1.5 m**, slid by hand along the table surface. Height set with `h1500`.

### 8.1 Stationary noise floor — good

At **12.5 FPS**, stationary, 501 frames over 40.1 s, `ok=1` on all 501:

| Quantity | at 6.25 FPS | at 12.5 FPS |
|---|---|---|
| `dx` sigma | ~0.02 px | **0.0207 px** (peak 0.129) |
| `spread` | 0.016–0.047 px | **0.009–0.029 px** |
| `vx` sigma | 1.24 mm/s | **2.47 mm/s** (rms 2.6) |
| Drift, x | −0.53 mm/s | **−0.75 mm/s** |
| Drift, y | −1.14 mm/s | −0.21 mm/s |

Doubling the frame rate behaved exactly as theory predicts: `dx` noise is
unchanged in pixels, velocity noise **doubles** (same pixel error over half the
interval), and integration drift grows by **√2** (0.53 × 1.41 = 0.75 — observed
0.75).

**The drift is systematic, not random walk.** Mean `dx` over the 501 stationary
frames is **+0.0062 px**, and 0.0062 px × 9.55 mm ÷ 0.08 s = 0.74 mm/s, which
accounts for essentially all of the observed −0.75 mm/s. A random walk could not
be explained by a constant mean. This matters because a fixed bias is
**calibratable** — subtracting the stationary mean `dx` should largely remove the
drift, which is not something a random walk would permit. Not yet implemented.

### 8.2 Static vs moving discrimination — works

Asked directly whether the firmware can tell static from moving: **yes, by
thresholding velocity**, with a wide margin between the stationary noise
(σ = 2.47 mm/s at 12.5 FPS) and the ≈60 mm/s signal of a slow hand slide — about
24×. A threshold of ~0.1 px/frame (≈12 mm/s at 12.5 FPS) separates them
cleanly. Four qualifications:

1. There is **no static/moving flag** — threshold the velocity yourself, and
   only when `ok=1`.
2. `ok=0` means *no information*, **not** static.
3. **Fast** motion also yields `ok=0`, with `n_edge` rising — the opposite
   condition to static. `n_edge` must be inspected to tell them apart.
4. With no gyro, pure yaw reads as translation at ≈35 mm/s per °/s.

### 8.3 Scale calibration — FAILED, four inconsistent runs

Four 300 mm hand slides:

| Run | Speed | measured x | vs 300 mm | note |
|---|---|---|---|---|
| 1 | fast | −183.5 mm | 61% | exceeded the 418 mm/s ceiling |
| 2 | ~5 s | −337.5 mm | **112%** | |
| 3 | ~5 s | −201.7 mm | **67%** | |
| 4 | ~5 s, full trace | −409.9 mm | **137%** | |

**A 2.2× spread. No scale factor can be extracted from this.** An h-FOV of 48.7°
was derived from run 2 alone; that figure is **withdrawn** —
`CONFIG_OPTFLOW_HFOV_DEG` remains at the nominal 54.

> **Resolved in §8.11.** Part of this scatter was block starvation (§8.8) and
> part was clipping, but the floor under all of it is sub-pixel locking: at the
> 0.12 px per-frame displacements these slides produce, the matcher recovers
> **51%** of true distance. That accounts for the always-short sign and for why
> the slowest run was the worst.


A fifth capture recorded 90 s of pure noise because the recorder's fixed window
expired before the slide happened — a protocol error (a fixed timer placed in
front of a human action), since fixed.

### 8.4 Root cause, from the full trace of run 4

Run 4 was captured frame-by-frame (133 frames, 8 s motion window). The cause is
**not** dropped frames and **not** timing:

- `dt` constant at 159.9 ms throughout; `ok=1` on **all 133 frames**;
  `n_edge > 0` on only 9.
- Position integration is literally `Σ(−dx · mm_per_px)` — `dt` cancels, so
  frame timing cannot bias the result at all.
- The `dx` sequence is physically impossible for a smooth hand slide:
  `0.99, −0.01, 1.17, −0.36, −1.07, 4.25, 1.78, 3.09, 1.35, …`

Two signatures are superimposed:

1. **Duplicate frames** — near-zero `dx` wedged between ~1 px steps. Harmless:
   `s_prev` copies the duplicate, so no displacement is lost.
2. **Spurious multi-pixel spikes** — the actual problem. They correlate with
   block starvation:

| row | `dx` | valid blocks | `spread` |
|---|---|---|---|
| 47 | 4.25 | 8 | 0.383 |
| 49 | 3.09 | 8 | **1.012** |
| 56 | 3.81 | **5** | 0.598 |
| 80 | 5.17 | **5** | 0.086 |
| 82 | 1.59 | **6** | **0.859** |

**Six frames out of 133 carry 49% of the total measured displacement.** Their
valid-block counts are 5, 5, 7, 8, 8, 8.

The wall is too featureless: `n_flat` is **12–20 of 25 on every single frame**,
leaving the median to vote on 4–13 blocks, and at the low end it votes on noise.
One spike (`dx` = 5.17 px) has `spread` = 0.086 — five surviving blocks agreeing
*confidently* on a false match — so `spread` alone cannot catch it.

### 8.5 Gate analysis — and why it is not yet a fix

Post-hoc gates applied to the run-4 trace:

| gate | x_mm | % of 300 mm |
|---|---|---|
| none (current firmware) | −409.9 | 137% |
| `spread` ≤ 0.80 | −365.2 | 122% |
| **`spread` ≤ 0.50** | **−295.6** | **99%** |
| `spread` ≤ 0.30 | −220.1 | 73% |
| `n_valid` ≥ 8 | −234.3 | 78% |
| `n_edge` == 0 | −252.4 | 84% |
| `\|dx\|` ≤ 2.0 px | −210.7 | 70% |

`spread ≤ 0.50` hitting 99% is **curve-fitting to one run and is not claimed as
a result.** Runs 1–3 were not recorded frame-by-frame, so there is no
cross-validation set.

More fundamentally, **every gate must undercount by construction**: in
`optflow_main.c:491` the line

```c
memcpy(s_prev, fb->buf, FRAME_W * FRAME_H);
```

runs **unconditionally, outside the `if (ok)`**. When a frame is rejected the
reference still advances, so the real displacement across that interval is
discarded permanently. Gating trades spurious spikes for genuine lost motion —
which is exactly why the tighter gates collapse to 70–78%.

### 8.6 Illumination is a first-order error source — the lamp control

§8.1 attributed the stationary drift to a sub-pixel bias that varied with the
scene and was therefore **not** calibratable. That attribution was wrong for the
dominant component.

The hypothesis that a lamp or a cast shadow could produce the drift came from
the operator, not from this analysis. It was tested with a control in which the
**camera was never touched** and only the room lighting changed:

| | lamp on | lamp off |
|---|---|---|
| `x` drift | **−2.257 mm/s** | **−0.114 mm/s** |
| `n_valid` | 20 / 25 | 19 / 25 |
| `n_flat` | 5 | 6 |
| `spread` | 0.016 | 0.017 |
| `n_edge` | 0 | 5 in 1938 frames |

A **20× reduction** in drift from switching off a lamp, with the matcher's own
quality metrics essentially unchanged. Darkness cost almost nothing.

Mechanism: `sad_block()` in `flow_match.c` computes a **plain** sum of absolute
differences with no brightness normalisation. A lamp to one side imposes a
brightness gradient across the frame; mains ripple modulates its amplitude
frame to frame; and a gradient whose amplitude changes between the two frames
displaces the SAD minimum **along the gradient direction**. The lamp was off to
one side, which is why the error was x-dominant.

This is confirmed numerically on the host in §8.11, part C.

**Consequence:** lamp-off is a bench workaround, not a flight solution. The
matcher needs brightness invariance (zero-mean SAD, or normalised correlation),
and the sensor needs AEC/AGC pinned rather than hunting.

### 8.7 A persistent y-axis bias that survives lighting and scene changes

A second, smaller drift is present on the **y** axis in every recording made,
and unlike the lamp artifact it does not respond to lighting:

| condition | y drift (mm/s) |
|---|---|
| position 1, lamp on | −0.570 |
| position 2, lamp on | −0.242 |
| position 2, lamp **off** | −0.627 |
| slide7 pre-slide, lamp off | −1.058 |
| `oab1` still segments (six) | −0.660, −0.945, −0.831, −0.766, −0.460, −0.666 |

The two longest `oab1` segments agree to **0.006 mm/s**. A straight-line fit
over 155 s gives −0.6283 mm/s with a **residual RMS of 0.24 mm over 99 mm
travelled** — 0.24%. Ten-second bins show no trend, `n_flat` is constant, and
there is no autocorrelation beyond 1 s.

A drifting shadow or a creeping rig would not be this linear, and a rig cannot
creep 72 mm in y while creeping 3 mm in x on the same frames — which is what
the cross-axis comparison in §8.4 showed. It is an estimator artifact.

> **Confirmed independently in §8.15.** Rolling the camera 46.5° between runs
> moved this bias's direction in the image by approximately zero, where anything
> fixed in the world — shadow, draught, creeping mount — would have swung with
> the roll. The bias is image-fixed. §8.15 also measures it larger than recorded
> here, −1 to −4 mm/s depending on the run, and shows its sign depends on which
> sub-pixel estimator is running.

Being constant across scenes and lighting makes it a **structural** suspect,
and therefore fixable. §8.11 shows the residual at zero motion and zero
gradient is −0.0023 px ≈ −0.27 mm/s, the same order.

### 8.8 The target matters more than the floor — starvation quantified

Every live test before `oab4` was run against a poorly-textured surface, and the
difference this makes is larger than any other effect measured:

| | starved surface | printed symbols |
|---|---|---|
| `n_valid` | **7.4–11.9 / 25** | **14–16 / 25** |
| `n_flat` | 13–17.5 | 9 |
| `y` drift at rest | **−2.6 mm/s** | −0.55 to −0.73 mm/s |
| phantom jumps while stationary | yes — see below | none |

On the starved surface the integrator moved **18.5 mm in x during a 10 s window
in which the rig was stationary**, with a 4.555 px frame and 11 edge
rejections. A stationary camera cannot move 18 mm. The at-rest bias is roughly
**4× worse** when the block grid is starved, so bias magnitude is partly a
function of available texture, not a fixed sensor property.

A rock-steady `n_valid` is itself diagnostic. During the starved runs the count
sat at 9.0–9.1 in *every* 5 s bin. Noise would make it wander; a count that
never varies means the same blocks pass every frame and the same blocks fail —
the signature of a **fixed island of texture surrounded by blank surface**.

**Target geometry at 1.5 m** (the arithmetic that was missing):

| quantity | value |
|---|---|
| mm per pixel | 9.5385 |
| frame footprint | 1526 × 1145 mm |
| one 16 px block sees | 153 mm square |
| block centre spacing (32 px) | **305 mm** |

So a usable target must cover of order 1.5 × 1.1 m **plus the travel**, and its
features must be resolvable and spaced near 305 mm. Two consequences that were
each discovered the hard way:

- **An A4 sheet at 1.5 m is 22 × 31 px** — about 4% of frame area, covering one
  or two of the 25 blocks. A single A4 target contributes almost nothing; the
  blocks that passed were reading incidental features (drawer edges, the
  handle, shadows), not the intended pattern.
- **A 1 cm grid at 1.5 m is 1.05 px per square**, at the resolution limit, so it
  averages to flat rather than resolving. Likewise **newsprint text is ~2 mm =
  0.2 px** and is simply invisible to the sensor. A suggestion to lay newspaper
  down was made in this project and was wrong for that reason, independently of
  being aimed at the wrong surface.

What worked: **four A4 sheets, one bold solid symbol each, ~150 mm across
(one block's worth), laid 2×2 with centres near 305 mm.** Solid shapes, not
outlines — a 10 mm stroke is 1 px and vanishes.

Two process errors are recorded here because both cost bench time: the camera
was assumed to be looking at a floor 1.5 m below when it was in fact facing a
drawer front at 1.5 m with a sheet taped to it, and that assumption was never
checked against the operator; and the operator's stated height was asked for a
second time after it had already been given.

### 8.9 Why single-direction slides cannot settle the scale

Scale error and drift bias both grow with elapsed time, so one slide cannot
separate them. On the slow 30 s run the choice of drift model alone swung the
answer by **68 mm — 23% of 300 mm**; on another run by 31 mm.

The **out-and-back** protocol separates them algebraically. With `g` the scale
gain, `b` the bias, `L` the true leg length and `T` each leg's duration:

```
out   A = −L·g + b·T_A
back  B = +L·g + b·T_B
```

Two measurements, two unknowns. With `T_A = T_B` the bias cancels exactly in
`(B − A)/2 = L·g`, and `(A + B)/2 = b·T` measures the bias **during motion**,
which need not equal the at-rest bias. With unequal legs the pair is still
solvable:

```
g = (T_A·B − T_B·A) / (L·(T_A + T_B))
```

The operator announces only "start" and "done"; leg boundaries are recovered
from the frames themselves. This was validated — on an earlier run the motion
window found from the data agreed with the operator's stopwatch to **0.14 s in
duration**, with both edges offset by a constant ~2.0 s of human reaction time.

**Caveat:** two equations in two unknowns has zero degrees of freedom, so the
fit is always perfect and never evidence. Two round trips are needed before the
residuals mean anything.

### 8.10 Run `oab4` — the first usable calibration run

First run over the printed-symbol target, 11,491 frames, one round trip of
300 mm. Structure recovered from per-second `dx`:

```
 t=111-140   out leg      x: 337.1 -> 115.8     30.0 s
 t=141-143   stopped      mean_dx +0.00, max|dx| 0.03-0.07
 t=144-171   back leg     x: 115.9 -> 305.6     27.5 s
 t=172+      still        max|dx| 0.013
```

Quality, against the best previous runs:

| | earlier runs | `oab4` |
|---|---|---|
| `n_valid` | 9 / 25 | **14–16 / 25** |
| frames with an edge-rejected block, in motion | 35–77% | **3.6%** |
| peak \|dx\| | 5.68 px of 6 usable | **4.10 px** |
| share of displacement in edge-affected frames | 36–77% | **13%** |
| frames rejected (`ok=0`) while moving | — | **0 of 775** |

Result:

```
out   −221.3 mm over 30.0 s
back  +189.7 mm over 27.5 s
joint solve:   g = 0.683    b = −0.55 mm/s
out leg alone: g = 0.738
```

The solved in-motion bias of −0.55 mm/s is consistent with the post-run at-rest
drift of −0.73 mm/s.

**Two snag events** are visible on the return and are retained deliberately,
because the airframe in flight will be less steady than a hand-slid rig:

```
 t  x_mm  mean_dx  max|dx|  edge
143 115.9   -0.000   0.057     0    stopped at the turn
144  64.4   +0.414   4.095     2    51 mm the wrong way
150 125.3   +0.016   0.054     0    stuck
151 105.1   +0.176   3.626     0    20 mm backwards
154 162.6   -0.142   0.400     5    recovering
```

**A withdrawn claim.** An automated turn-detector placed the turn at the global
minimum of `x`, which fell inside the return because the snags drove `x` below
its true turn value. That gave legs of 40.2 s and 15.1 s, `g = 0.781`, and an
implied h-FOV of **66.2°** — close enough to the OV2640's nominal 65–66° to
look like a confirmation. The operator's stopwatch said both legs were
near-equal, which was correct; the per-second trace gives 30.0 s and 27.5 s.
**The 66.2° figure is withdrawn, and `CONFIG_OPTFLOW_HFOV_DEG` remains at 54.**
§8.11 shows why changing it would have been a mistake.

### 8.11 Root cause of the scale deficit: sub-pixel locking

**The single number that reframes §8.3.** Over the 775 motion frames of `oab4`:

```
sum of |dx|       93.8 px
mean per frame    0.121 px
```

The measurement is **entirely sub-pixel**. At 1.5 m and hand-slide speeds the
integer part of the match is almost always zero, so every millimetre
integrated comes out of the parabolic refinement in step 4 of the matcher.

`host/bias_sweep.c` measures that refinement directly, with no hardware. The
texture is a **continuous** function — bilinearly interpolated value noise
evaluated analytically at any real offset — so both frames of a pair are
produced by one identical evaluation differing only in offset and 8-bit
quantisation. (The pre-existing `test_flow_match.c` shifts a raster with
bilinear interpolation, which smooths the shifted frame but not the reference;
that asymmetry is itself a bias source and would have contaminated this
measurement.)

**Single-step gain** (8 textures × 4 phases, "fine" texture):

| true px | measured | gain |
|---|---|---|
| 0.02 | 0.0083 | 0.416 |
| 0.05 | 0.0230 | 0.459 |
| 0.10 | 0.0492 | 0.492 |
| **0.12** | **0.0603** | **0.503** |
| 0.20 | 0.1122 | 0.561 |
| 0.30 | 0.1970 | 0.657 |
| 0.50 | 0.4902 | 0.980 |
| 0.70 | 0.7969 | **1.138** |
| 1.00 | 0.9995 | 0.999 |
| 2.00 | 1.9995 | 1.000 |

**Integrated over 200 consecutive frames:**

| step px | mm/frame | recovered |
|---|---|---|
| 0.10 | 0.95 | 49.7% |
| **0.12** | **1.14** | **51.1%** |
| 0.20 | 1.91 | 57.5% |
| 0.30 | 2.86 | 67.1% |
| 0.50 | 4.77 | **100.0%** |
| 1.00 | 9.54 | 100.0% |

This is textbook **pixel locking**: a parabola fitted to a SAD minimum — which
is a cusp, not a parabola — pulls estimates toward integer positions. Below
half a pixel they shrink toward zero; the 1.138 at 0.70 px is the opposite limb
of the same S-curve, pushing toward 1.

**It explains the whole of §8.3 and more:**

- the deficit is always *short*, never long, in every run;
- the gain varies 0.51–0.87 between runs, because it depends on speed and on
  the texture's spectrum;
- the **slow** 30 s run was the worst of all — a smaller per-frame displacement
  sits deeper in the locking regime;
- a single multiplicative correction factor **cannot** work, because the gain is
  ~0.50 at hover speed and 1.000 above 60 mm/s.

**Part C — illumination, quantified** (zero real motion, a brightness gradient
of the stated amplitude appearing in frame 2):

| ramp, counts across frame | measured `dx` | mm/s at 12.5 FPS |
|---|---|---|
| 0 | −0.0023 | −0.27 |
| 20 | −0.0038 | −0.46 |
| **40** | **−0.0250** | **−2.98** |

−2.98 mm/s of pure fiction from a 40-count gradient, against the **−2.26 mm/s**
measured with the lamp on in §8.6. Same mechanism, same magnitude. The residual
−0.27 mm/s at zero gradient is the same order as the persistent y bias of §8.7.

**This gets worse at the design altitude, not better.** mm/px rises from 9.5385
at 1.5 m to 12.72 at 2 m, so a given physical speed produces a **25% smaller**
pixel displacement and sits deeper in the locking regime.

**Candidate fixes, in order of cost:**

1. **A sub-pixel estimator matched to the cost function** instead of the
   parabola — a few lines, no latency and no extra computation. ~~Gaussian
   fit~~: **measured and rejected**, see §8.13 — the winner is the V-fit.
2. **Zero-mean SAD** — removes the illumination sensitivity of §8.6 and part C.
3. **Sum the per-block SAD surfaces and minimise once**, instead of taking 25
   per-block minima and a median. One smooth, well-sampled surface; textured
   blocks dominate naturally through deeper minima; cheaper after the SADs.
4. **Adaptive multi-frame baseline** — match against a frame *k* back so the
   displacement lands above 1 px where the gain is 0.999. Costs 19 kB per
   buffered frame and up to ~0.6 s of latency at hover speed. Needed only if
   1–3 leave a residual.

Only after these is a scale calibration meaningful, and only then is
`CONFIG_OPTFLOW_HFOV_DEG` worth committing.

> **Resolved in §8.12 and §8.13.** The curve was validated against the recorded
> `oab4` trace (68% → 97% of true distance), and all three estimators were
> measured: the V-fit recovers **91%** at the operating point where the parabola
> recovers 51% and the Gaussian only 22%.

### 8.12 The curve validated against recorded hardware data

The transfer curve of §8.11 was measured entirely on synthetic images. Before
trusting it, it was applied **blind** to the real `oab4` trace: the 201-point
curve (`dump_curve`, 0→3 px, quadratically spaced so it is dense where the
bench data lives) was inverted frame by frame, each measured `dx` mapped back
to the true displacement that would have produced it, and the result
re-integrated.

```
                                      raw        inverted    of true 300 mm
out leg    29.3 s  mean|dx| 0.1106   -227.4 mm   -309.4 mm    76% -> 103%
back leg   27.9 s  mean|dx| 0.1444   +180.3 mm   +275.1 mm    60% ->  92%

joint solve (scale + bias):  g = 0.677  ->  g = 0.973
```

A curve derived from synthetic texture, applied with no fitting of any kind to
hardware data, turns a 68% reading into 97%. Two consequences:

- **The nominal 54° h-FOV is right to within 3%** *if* the working distance of
  that run really was 1500 mm. It was never taped — and §8.14 later found the
  assumed distance stale by 9% in a different setup, so treat the 3% as "the
  optics are not the problem", not as a calibrated figure. It does
  independently confirm the withdrawal of the 66.2° figure in §8.10, which had
  been an artifact of mis-detected leg boundaries.
- **The sub-pixel model is the correct model.** Nothing else was changed, and
  the residual 3% is smaller than the snag artifact still present in the
  return leg.

**Inversion is a diagnostic, not a fix.** The stationary controls show why:

| | raw | inverted |
|---|---|---|
| pre-run, still | +72.3 mm | **+160.9 mm** |
| post-run, still | −16.3 mm | **−39.1 mm** |

Near zero the curve's slope is ~0.4, so inverting multiplies noise by ~2.5.
Dividing by the gain restores the signal and the drift equally. The estimator
itself has to be made unbiased.

### 8.13 Estimator comparison, measured

Three sub-pixel estimators were implemented as a selectable `flow_cfg_t.subpix`
(`FLOW_SUBPIX_PARABOLA` / `GAUSSIAN` / `VFIT`) and run through the same sweep,
so the choice is measured rather than asserted.

```c
parabola:  d = 0.5*(sm - sp) / (sm - 2*s0 + sp)
gaussian:  d = 0.5*(log sm - log sp) / (log sm - 2*log s0 + log sp)
vfit:      d = 0.5*(sm - sp) / (max(sm,sp) - s0)
```

The V-fit is the exact inverse of a V-shaped cost `s(k) = A·|k − δ|`, verified
algebraically for both signs of δ. That matters because a SAD minimum over real
texture *is* a cusp, not a parabola — which is the whole mechanism of §8.11.

**Gain at the displacement the bench actually produces** (texture "fine",
8 textures × 4 phases):

| true px | parabola | gaussian | **vfit** |
|---|---|---|---|
| 0.05 | 0.459 | 0.150 | **0.878** |
| 0.10 | 0.492 | 0.197 | **0.895** |
| **0.12** | **0.503** | **0.215** | **0.897** |
| 0.20 | 0.561 | 0.291 | **0.916** |
| 0.30 | 0.657 | 0.415 | **0.942** |
| 0.70 | 1.138 | 1.243 | **1.016** |
| ≥1.00 | 0.999 | 1.000 | **0.999** |

**Distance recovered over 200 consecutive frames:**

| step px | parabola | gaussian | **vfit** |
|---|---|---|---|
| 0.05 | 46.8% | 15.3% | **89.5%** |
| 0.10 | 49.7% | 20.1% | **90.4%** |
| **0.12** | **51.1%** | **22.0%** | **91.0%** |
| 0.20 | 57.5% | 30.3% | **93.5%** |
| 0.30 | 67.1% | 42.8% | **95.7%** |
| ≥0.50 | 100.0% | 100.0% | **100.0%** |

**The Gaussian is worse than the parabola it was meant to replace** — 22%
recovery against 51% at the operating point. The reason is specific: the
Gaussian estimator is built to refine a correlation *peak*, where the centre
value is large. Here it refines a SAD *minimum*, where `s0 → 0`; `log s0`
diverges downward, the denominator `log sm − 2 log s0 + log sp` inflates, and
the estimate collapses toward zero. It amplifies locking instead of curing it.
Candidate fix 1 of §8.11, which named the Gaussian as "the standard remedy",
was wrong for this cost function and is superseded by this measurement.

**The V-fit is near-flat across the whole sub-pixel range** — 0.90 at the
operating point against 0.50, and no 1.14 overshoot at 0.70 px, so it corrects
both limbs of the S-curve. The residual ~9% loss is the 8-bit quantisation of
the two frames, not the estimator.

**One cost, stated plainly.** Part C, illumination, at a 40-count gradient
appearing between frames with zero real motion:

| ramp, counts | parabola | gaussian | vfit |
|---|---|---|---|
| 0 | −0.27 mm/s | −0.04 mm/s | −0.53 mm/s |
| 20 | −0.46 mm/s | −0.22 mm/s | −0.89 mm/s |
| **40** | **−2.98 mm/s** | **−2.24 mm/s** | **−5.01 mm/s** |

The V-fit is ~1.7× more sensitive to illumination change. This is not a defect
of the estimator: a brightness gradient genuinely walks the plain-SAD minimum
(§8.6), and the parabola was suppressing half of that fictitious displacement
with the same factor ~0.5 that was suppressing half the real motion. Unity gain
reports both honestly. The fix belongs where the error originates — zero-mean
SAD, step 2 of §11 — not in the estimator.

### 8.14 Hardware A/B of the estimators — the V-fit confirmed on real images

The estimator was made runtime-selectable (`e<0|1|2>`) for exactly this test, so
both estimators could be measured on the **same** rig, lighting and motion
instead of across two runs on different days.

**New and better test motion.** Instead of sliding the rig by hand, the camera
was placed on a height-adjustable table and moved between two detented
positions, **730 mm and 1000 mm**, giving a true displacement of
**270 ± 10 mm**. The camera looks horizontally at the drawer, so a vertical
table move is a translation *parallel* to the target plane — the working
distance does not change — and it appears on the image's **y** axis, not x.
This is a far better calibration motion than a hand slide: the endpoints are
mechanical stops, the speed is steady, and it cannot snag.

**The geometry constant was stale, and it mattered.** A tape measure from the
lens to the paper read **1370 mm**, not the 1500 mm carried over from the
earlier horizontal slides — the whole table had been moved between setups.
`mm/px` scales linearly with distance, so this alone was a **9% scale error**
(9.554 → 8.726 mm/px) that was about to be misattributed to the estimator.

| | out | back | both legs | vs 540 mm |
|---|---|---|---|---|
| parabola (`oab6`) | 78.7% | 68.3% | 397.0 mm | **−26.5%** |
| **V-fit (`oab5`)** | 102.6% | 112.8% | 581.7 mm | **+7.7%** |
| ratio V-fit/parabola | 1.303 | 1.651 | **1.465** | sweep predicted 1.63 |

Both runs: **zero rejected frames**, `n_valid` 18/25 mean, peak `|dy|` 0.26–0.39 px
against the 6 px usable radius — a 15× margin. True motion was 23 mm/s =
**0.20 px/frame**, so this is still entirely the sub-pixel regime, just higher
up the curve than `oab4`'s 0.121 px.

**The V-fit's error is 3.4× smaller than the parabola's**, and at +7.7% it is
about twice the ±3.9% combined uncertainty (±10 mm on the table detents, ±15 mm
on the tape). `FLOW_SUBPIX_VFIT` is therefore the committed default in
`optflow_main.c`.

**Why the ratio is the load-bearing number.** `mm/px`, the height and the FOV
all cancel in the ratio between two estimators measured on one motion, so
1.465 ± nothing-geometric is directly comparable to the sweep's 1.63. The 10%
gap is the estimator model's error; everything else was geometry.

**The blend was *not* implemented, and the measurement is why.** Before the tape
reading, the two estimators straddled the truth almost symmetrically (−19.5% /
+17.9% at an assumed 1500 mm) and a 50/50 blend looked exact. With the true
1370 mm the picture changes completely: the V-fit is nearly correct alone, and a
50/50 blend would have been **worse than doing nothing**. A blend would now need
w ≈ 0.77 on the V-fit to be exact at this speed — but §8.15 measured the V-fit
reading 91–93% at 0.15 px/frame and 108–116% at 0.23–0.25, so the required weight
changes sign across the speed range and no single blend weight can hold.

**The sweep's absolute gains are pessimistic for real optics.** Hardware over
synthetic: parabola 0.735/0.561 = 1.31, V-fit 1.077/0.916 = 1.18. The synthetic
value-noise texture carries more high-frequency content than a defocused photo
of paper symbols, so its SAD basins are sharper. The sweep got the **ranking**
and the **mechanism** right, which is what it is for; its absolute numbers should
not be quoted as hardware predictions.

**What looked like the dominant error: direction asymmetry.** The V-fit's two
legs read 102.6% and 112.8% — a **10-point spread, larger than the 7.7% mean
error**. The parabola spreads by a similar 10.4 points (78.7% / 68.3%).

> **Superseded by §8.15.** Two of the statements in this paragraph are wrong. The
> parabola does not show "the same shape" — it shows the *mirrored* shape, out >
> back where the V-fit has out < back, which points at the estimator rather than
> away from it. And the spread is not a direction asymmetry at all: rolling the
> camera to 43.5° and solving gain and bias jointly dissolves it into a constant
> ~4 mm/s image-axis bias (§8.15). The rig candidates listed here — a
> non-vertical lift, illumination with height — are all excluded, because none of
> them can reverse sign when only the sub-pixel formula changes.

---

### 8.15 Rotating the camera: the asymmetry resolved, and two retractions

§8.14 closed on a 10-point out/back spread and named it the dominant error. Three
further runs — the camera rolled 90°, then 43.5°, on the same table rig at the
same taped 1370 mm — show that the spread is **not a direction asymmetry at all**.
It is a constant bias added on one leg and subtracted on the other. Two claims
made along the way are retracted below; both were between-run artifacts, and both
were killed by the same thing: measuring **within** a single run.

**The runs.**

| run | estimator | camera roll | motion lands on | frames |
|---|---|---|---|---|
| `oab5` | vfit | upright | image y | 3148 |
| `oab6` | parabola | upright | image y | 839 |
| `oab7` | vfit | 90° | image x | 895 |
| `oab8` | parabola | 90° | image x | 3121 |
| `oab9` | vfit | 43.5° | **both axes at once** | 1219 |

Rolling the camera is the whole point: the table motion stays identical in the
world, so anything that moves with the image axes is in the sensor or the
algorithm, and anything that stays put in the world is in the rig or the room.

#### A recording-discipline error worth writing down

Every one of these runs logged with `h1500` in force — 9.554 mm/px — because the
recorder scripts send `h1500` at startup and that line was carried forward by
`sed` from script to script. The true lens-to-target distance is 1370 mm, so every
reading needs ×0.9133. Having briefly believed `oab7` had been logged at `h1370`,
the first comparison put a corrected `oab5` against an uncorrected `oab7` and
produced a spurious 17.6% cross-axis difference.

The fix is not vigilance, it is that **the mm/px actually in force is recoverable
from the log**: field 6 is `vx` in mm/s and field 4 is `dx` in px/frame, so
`|vx| / |dx| × dt` returns the constant the firmware used. All three runs returned
9.558. Any analysis of a flow CSV should start by recovering that number rather
than trusting what the recorder was supposed to have sent.

#### The 2×2: the spread's sign belongs to the estimator

| | **y axis** | **x axis** (rolled 90°) | x/y |
|---|---|---|---|
| **vfit** | 107.7%  (+10.2 pts) | 115.7%  (+1.8 pts) | 1.074 |
| **parabola** | 73.5%  (−10.4 pts) | 82.6%  (−4.9 pts) | 1.123 |
| vfit/parabola | 1.465 | 1.401 | |

The *sign* of the out/back spread tracks the estimator without exception —
positive for the V-fit on both axes, negative for the parabola on both. That rules
out every mechanical and optical candidate §8.14 listed: no rig tilt, lift
geometry, or illumination-with-height can reverse sign when a sub-pixel formula is
changed over the same images. §8.14's reading that "the parabola shows the same
shape, so it is not an artifact of the estimator" was wrong — the parabola shows
the *mirrored* shape (out > back where the V-fit has out < back), which is the
opposite conclusion.

The vfit/parabola ratio is 1.465 on y and 1.401 on x, a 4.5% difference, so the
SAD basin is not meaningfully anisotropic and the two estimators stand in the same
relation on both axes.

#### The 43.5° run: one motion, both axes, and a joint solve

At 43.5° roll (set with a phone inclinometer — the best available) a single 270 mm
table move projects **185.9 mm onto image x and 195.9 mm onto image y
simultaneously**. Both axes then see the same motion under identical lighting,
distance, speed and texture, so cross-axis comparison has no between-run confound
left in it.

With out and back legs available, gain and a constant bias can be solved together
per axis — two equations, two unknowns, zero degrees of freedom:

```
out  = +g·L + b·T          back = −g·L + b·T
g·L  = (|out| + |back|)/2  b·T  = (out + back)/2
```

```
   x   gain 93.2%   bias -1.72 mm/s
   y   gain 91.3%   bias -0.57 mm/s
   gx/gy = 1.021
```

Raw, the same run looks alarming: the spread is **+21.1 points on x and −6.6
points on y**, and the apparent path angle differs by 8.5° between the legs (39.7°
out, 48.2° back) — which looks exactly like a table lift that does not retrace its
path. Fitted as gain-plus-bias, all of it collapses into the two bias terms above.
There is no direction asymmetry and no path hysteresis.

**Retraction 1 — there is no cross-axis scale gap.** The 2×2 above suggested the x
axis reads 7.4% high by the V-fit and 12.3% high by the parabola, which looked
like ~10% non-square pixels confirmed by two independent estimators. The
within-run measurement says **1.021**. One `mm_per_px` for both axes is correct and
the pixel-aspect idea is dropped. An earlier 17.6% version of the same claim came
from the `h1500` mix-up above.

**Retraction 2 — the "10-point direction asymmetry" named as the dominant error at
the end of §8.14 does not exist.** It is the §8.7 bias, projected onto whichever
axis carries the motion, with its sign set by the estimator. §11 step 2 as
previously written was chasing it in the wrong place.

#### The §8.7 bias is image-fixed, so it is ours

The 43.5° run also settles where the long-standing y drift lives. During 48 s at
rest before the move, in four consecutive 12 s windows:

```
   y  -3.83  -3.89  -3.99  -4.23  mm/s
   x  +0.95  +0.95  +1.01  +1.01  mm/s
```

A near-perfect constant, and it points **14–17° off the image y axis**. At 90° roll
(`oab7`, `oab8`) it pointed 11–17° off image y. **46.5° of camera roll moved the
drift direction in the image by approximately zero.** A drift with a fixed
direction in the world — air currents, a creeping mount, a horizontally shifting
lamp — would have swung by 46.5°. It is image-fixed: sensor or algorithm.

This also kills an intermediate hypothesis that looked good on the 90° runs alone:
that the drift followed world-horizontal, because the two rolled runs put the
larger drift on image y while the upright `oab5` put it on image x (+1.13 vs +0.75
mm/s). `oab5` remains the one run that does not fit, and its axis separation is
weak (1.5×) against 3–4× in the rolled runs.

Since the bias is ours and its sign depends on the estimator, **zero-mean SAD
(§11) is the right fix and is now the highest-value change in the queue** — it
targets precisely the mechanism that makes a sub-pixel estimator respond
asymmetrically to a texture or brightness gradient.

#### The host sweep validated at the hover operating point — and diverging above it

The V-fit gains measured on hardware, all bias-free, against the sweep's V-fit
transfer curve read at the same per-frame displacement:

| true px/frame | host sweep | hardware | run |
|---|---|---|---|
| 0.152 | 90.6% | **93.2%** | `oab9` x |
| 0.160 | 90.9% | **91.3%** | `oab9` y |
| 0.233 | 92.4% | 107.7% | `oab5` y |
| 0.254 | 93.0% | 115.7% | `oab7` x |

The bottom two rows are a problem; the top two are the best news in this report.
**At 0.15 px/frame — within touching distance of the 0.12 px/frame that a 2 m
hover actually produces — the host harness and real images agree to 2.6 points.**
That is the first direct validation of `bias_sweep` against hardware at the
operating point the blimp will live at, and it means the host harness can be
trusted for tuning work there without burning a BOOT-button reflash per idea.

Above the operating point the sweep's curve is flat to 3 points across
0.12 → 0.25 px/frame while the hardware spans 23 points, so **speed alone does not
explain the high-speed rows**. Two candidates remain, unresolved:

1. **Real texture has a far steeper speed dependence than synthetic value noise.**
   The sweep's texture has sharper SAD basins than a defocused photo of paper
   symbols, which §8.14 already showed makes its absolute gains pessimistic.
2. **Diagonal motion costs 15–20 points.** `oab9` is the only run where both axes
   carry sub-pixel displacement at once, so the per-axis V-fit is refining a
   cross-coupled 2-D basin rather than a clean 1-D cusp. If so the penalty applies
   to every real flight, where motion is never axis-aligned, and the three
   axis-aligned runs are the optimistic ones.

The obvious separator — repeat at 43.5° with a faster move — is not available: the
table has one speed. It can still be done by changing the geometry instead, since
px/frame scales as 1/h: at the same table speed, **halving the lens distance to
about 700 mm doubles the per-frame displacement to ~0.3 px**. Same roll, same
detents, one tape measurement. If the gain stays near 92% it is diagonality; if it
climbs toward 115% it is speed.

---

## 9. Corrections outstanding in `optflow_cam/README.md`

These claims in the README are **disproved by measurement** and still need
editing:

| README claim | Reality |
|---|---|
| "Match cost ~2 ms" per frame | **47–55 ms** at 240 MHz (74–78 ms at 160 MHz) |
| "At 15 FPS the CPU is idle ~97% of each period" | CPU duty is **~65%** |
| 15 FPS operation | **12.5 FPS**, set by XCLK 20 MHz |
| XCLK 10 MHz / CPU 160 MHz listed as thermal measures | both **reversed** to buy the frame rate |
| Max measurable uses the full ±search | usable radius is **`search − 1`** |
| "Hardware status: untested" banner | it runs; bring-up is done |
| `/dev/ttyACM0` is the ESP32-S3-Zero | it is the ESP32-CAM |

---

## 10. What is established, and what is not

**Established:**
- The matcher is correct — verified on host to 0.024 px mean error.
- Grayscale QQVGA capture into DRAM works on real hardware.
- Honest failure works: a blank surface returns `ok=0, flat=25/25`.
- Stationary noise floor is ±1.8 mm/s; drift ≈0.5–1.1 mm/s.
- Static vs moving is discriminable with ~33× margin.
- **Frame rate is 12.5 FPS** (XCLK 20 MHz, CPU 240 MHz), with 47–55 ms of
  matching per frame and 25 ms of margin. The rate is set by XCLK alone; the
  pacing config is inert.
- Stationary drift is a **systematic `dx` bias** (+0.0062 px), not a random
  walk — therefore calibratable.
- **Illumination is a first-order error source.** Switching off one lamp, with
  the camera untouched, cut the x drift 20× (−2.257 → −0.114 mm/s). Reproduced
  on the host: a 40-count brightness gradient appearing between frames
  fabricates −2.98 mm/s (§8.6, §8.11C).
- **A usable target must be matched to the geometry.** At 1.5 m the frame covers
  1526 × 1145 mm, one block sees 153 mm and block centres are 305 mm apart, so
  an A4 sheet spans 1–2 of 25 blocks and a 1 cm grid is 1.05 px per square.
  Four A4 sheets with solid ~150 mm symbols took `n_valid` from 9/25 to 14–16/25
  and cut in-motion edge rejections from 35–77% of frames to 3.6% (§8.8).
- **Block starvation drives the bias**, roughly 4×: −2.6 mm/s starved versus
  −0.55 to −0.73 mm/s over the symbol target, with phantom 18.5 mm jumps while
  stationary (§8.8).
- **The scale deficit is sub-pixel locking, not optics.** The bench measurement
  averages 0.121 px per frame and is therefore entirely sub-pixel; the host
  sweep measures a gain of **0.503 at 0.12 px**, rising to 1.000 above 0.5 px.
  Integrated over 200 frames that recovers **51%** of true distance, which
  accounts for the 51–87% family of §8.3, its always-short sign, and its speed
  dependence (§8.11).
- **The sub-pixel model is confirmed on real data.** Inverting that synthetic
  curve frame by frame on the recorded `oab4` trace — with no fitting — raises
  the joint-solve scale from **g = 0.677 to g = 0.973** (§8.12). Therefore
  **the nominal 54° h-FOV is correct to within 3%, and the optics were never
  the problem.** Inversion is a diagnostic only: it amplifies drift by the same
  factor (+72.3 mm of stationary drift becomes +160.9 mm).
- **The estimator choice is measured, not argued.** At the 0.12 px operating
  point, integrated over 200 frames: **V-fit 91.0%**, parabola 51.1%, Gaussian
  **22.0%** (§8.13). The Gaussian is *worse* than what it was meant to replace,
  because it is built for correlation peaks and `log s0` diverges at a SAD
  minimum. The V-fit is the exact inverse of a V-shaped cost, which is what a
  SAD surface over real texture is.
- **The V-fit is confirmed on hardware, same-rig A/B.** Over a 270 ± 10 mm
  mechanical reference (a height-adjustable table between two detents, which is
  a translation parallel to the target plane): **V-fit +7.7%, parabola −26.5%**
  — a 3.4× smaller error — with zero rejected frames in either run and a
  measured estimator ratio of 1.465 against the sweep's predicted 1.63 (§8.14).
- **`mm/px` must be measured, never assumed.** The working distance was 1370 mm
  by tape, not the 1500 mm carried over from an earlier setup: a 9% scale error
  that was about to be misattributed to the estimator (§8.14).
- **A height-adjustable table is a better calibration rig than a hand slide** —
  mechanical endpoints, steady speed, no snag, and the motion lands on the image
  y axis (§8.14).
- **The §8.7 bias is image-fixed, so it is the sensor's or the algorithm's, not
  the room's.** It points 14–17° off the image y axis at 43.5° camera roll and
  11–17° off it at 90° roll: 46.5° of roll moved it by ~0°, where anything fixed
  in the world would have swung by 46.5°. It is also near-perfectly constant
  (−3.83, −3.89, −3.99, −4.23 mm/s over four consecutive 12 s windows) (§8.15).
- **There is no cross-axis scale difference.** Solved within one run at 43.5°
  roll, where a single motion loads both axes at once: **gx/gy = 1.021** (x
  93.2%, y 91.3%). The 1.074/1.123 of the 2×2 and an earlier 1.176 were
  between-run artifacts; one `mm_per_px` for both axes is correct and the
  non-square-pixel hypothesis is dropped (§8.15).
- **The 10-point out/back spread is a constant bias, not a direction asymmetry.**
  Its sign tracks the estimator with no exception (V-fit positive on both axes,
  parabola negative on both), which no rig or illumination effect can do. At
  43.5° the raw spread is +21.1 points on x and −6.6 on y, and an 8.5° difference
  in apparent path angle between legs; a joint gain-plus-bias solve dissolves all
  of it into −1.72 and −0.57 mm/s (§8.15).
- **The host sweep is validated against hardware at the operating point.** At
  0.152 and 0.160 px/frame the sweep predicts 90.6% and 90.9% where hardware
  measures 93.2% and 91.3% — agreement to 2.6 points, within touching distance of
  the 0.12 px/frame a 2 m hover produces. `bias_sweep` can therefore be trusted
  for tuning near the operating point without a BOOT-button reflash per idea
  (§8.15).
- **The mm/px actually in force is recoverable from any flow log** as
  `|vx| / |dx| × dt` (fields 6 and 4). Every `oab5`–`oab9` run logged at `h1500`
  because the recorder scripts carried that line forward by `sed`; recovering the
  constant from the data rather than trusting the script is what caught it
  (§8.15).
- Flashing requires the BOOT button held, because XCLK = GPIO0; and the USB
  bridge must be replugged after a 460800-baud flash.

**Not established:**
- **Absolute scale / h-FOV**, as a *committed* number. §8.12 shows the nominal
  54° is right to within 3%, which is enough to stop suspecting the optics but
  not enough to commit: the 3% rests on one round trip, which has zero degrees
  of freedom, and its return leg still carries snag artifact. An implied 66.2°
  derived from `oab4` is **withdrawn** (§8.10); `CONFIG_OPTFLOW_HFOV_DEG` stays
  at 54 until two round trips are run with the fixed estimator.
- **The residual error of the V-fit**, and whether a V/parabola blend removes it.
  The residual is no longer one number: +7.7% at 0.23 px/frame, +15.7% at 0.25,
  and −7 to −9% at 0.15. A single blend weight cannot correct a gain that changes
  sign across the speed range, so the blend is deferred until the 92%-vs-115%
  question above is settled (§8.14, §8.15).
- **Why the gain is 92% on diagonal motion and 108–116% on axis-aligned
  motion.** The sweep's V-fit curve is flat to 3 points across
  0.12 → 0.25 px/frame while hardware spans 23 points, so speed alone does not
  explain it. Either real texture has a far steeper speed dependence than
  synthetic value noise, or **diagonal motion costs 15–20 points** because the
  per-axis V-fit refines a cross-coupled 2-D basin instead of a clean 1-D cusp —
  in which case the penalty applies to every real flight and the three
  axis-aligned runs are the optimistic ones. **This is now the largest open
  question about scale** (§8.15).
- **Illumination robustness after the estimator change.** The V-fit is ~1.7×
  more sensitive to a brightness gradient than the parabola (−5.01 vs
  −2.98 mm/s at 40 counts) because it no longer suppresses that fiction either.
  Zero-mean SAD is expected to remove it; unmeasured.
- The **mechanism** of the image-axis bias (§8.7, §8.15). It is now known to be
  image-fixed, constant, estimator-dependent in sign, and to run −1 to −4 mm/s
  depending on the run — four times the ~−0.66 mm/s first recorded in §8.7 — but
  it is still not traced to a specific line of code. Zero-mean SAD is the
  candidate fix and is untested on hardware.
- **Direction symmetry of the gain.** `oab4`'s two legs read 0.738 and 0.632
  before bias correction, but the return carried ~71 mm of snag artifact, so the
  question is open. One round trip is two equations in two unknowns — zero
  degrees of freedom, so its perfect fit is arithmetic, not evidence. **Two
  round trips are required.**
- Behaviour at the real **2 m** design height over the real floor.
- Thermal performance. Nothing has been measured with a probe; all thermal
  claims remain reasoned, not verified. **This now matters more than before:**
  the two largest heat measures (XCLK 10 MHz, CPU 160 MHz) were both spent to
  reach 12.5 FPS, so the thermal margin is unknown and smaller than designed.
- Whether a `spread`/`n_valid` gate helps, once frame-reference handling is
  fixed.

---

## 11. Next steps, in order

1. ~~**Fix the sub-pixel estimator.**~~ **DONE and verified on hardware.**
   Three estimators are selectable via `flow_cfg_t.subpix` and switchable at
   runtime with `e<0|1|2>`; `FLOW_SUBPIX_VFIT` is the committed default. Same-rig
   A/B against a 270 mm mechanical reference: **+7.7% against the parabola's
   −26.5%** (§8.14).
2. ~~**Chase the 10-point direction asymmetry.**~~ **DONE — and it does not
   exist.** The camera was rolled 90° and then 43.5°; the spread's sign tracks
   the estimator, and a joint gain-plus-bias solve dissolves it into a constant
   image-axis bias. The same runs showed the bias is image-fixed (so ours, not
   the room's) and that there is no cross-axis scale difference (§8.15).
3. **Zero-mean SAD — now the highest-value change in the queue.** It removes the
   illumination sensitivity of §8.6 and §8.11C, and it is the mechanism behind
   the constant image-axis bias that §8.15 showed to be the dominant remaining
   error: a sub-pixel estimator responding asymmetrically to a brightness or
   texture gradient. Validate against part C of the sweep — the −2.98 mm/s at a
   40-count gradient should collapse — then confirm on hardware that the ~4 mm/s
   at-rest y drift drops with it.
4. **Settle 92%-vs-115%: is the gain penalty speed or diagonality?** `oab9`
   (43.5° roll, both axes loaded) reads 92% where the axis-aligned runs read
   108–116%, and the sweep's flat curve rules out speed as the whole story. The
   table has only one speed, so change the geometry instead: same 43.5° roll,
   same detents, **lens at ~700 mm instead of 1370 mm**, which doubles the
   per-frame displacement to ~0.3 px. Tape the distance. Gain stays near 92% →
   diagonality, and it applies to every real flight; gain climbs toward 115% →
   speed (§8.15).
5. **Sum the per-block SAD surfaces and minimise once**, instead of 25 per-block
   minima plus a median. More robust on sparse targets and cheaper after the
   SADs are computed.
6. **Adaptive multi-frame baseline** — only if 3–5 leave a residual. Match
   against a frame *k* back so displacement lands above 1 px.
7. **Fix the frame reference:** do not advance `s_prev` when a frame is
   rejected, so a rejection costs *latency* instead of *displacement*. Note
   that this did **not** contribute to the `oab4` deficit — zero frames were
   rejected while moving — so it is a flight-robustness fix, not a scale fix.
   The search range must then widen to cover a two-period baseline.
8. **Then** add a `spread` / `n_valid` gate, validated against several recorded
   runs rather than one.
9. **Two** round trips on the height-adjustable table (§8.14 — mechanical
   endpoints, no snag), which over-determines the scale/bias system and makes
   its residuals meaningful. Tape the lens-to-target distance each time. Only
   then commit `CONFIG_OPTFLOW_HFOV_DEG`.
10. **Pin the sensor's exposure and gain** — `set_exposure_ctrl(0)`,
   `set_gain_ctrl(0)`, then `set_aec_value` / `set_agc_gain`; reach the OV2640
   banding-filter bits via `set_reg` if mains flicker persists. Requires a
   BOOT-button reflash at 115200 baud.
11. Re-test at 2 m over the actual floor. Expect locking to be **worse**: mm/px
   rises 9.5385 → 12.72, so a given speed yields 25% less pixel displacement.
12. **Measure can temperature with an external probe under sustained load** —
    XCLK 20 MHz and CPU 240 MHz together removed the project's two main thermal
    measures, so the margin is unknown. If it runs hot, XCLK 16 MHz gives 10 FPS
    (still inside the 10–20 FPS requirement) at lower dissipation.
13. Parameterise the host test fixtures by `CFG.search` — they currently
    hardcode 5 px and 6 px shifts and so fail spuriously at any radius below ±7.
14. Correct the README per §9.

### Known gaps carried forward

- No gyro de-rotation (no IMU on this board) — consumer must subtract `f·ω·dt`.
- Height is an operator-supplied constant, not fused from the altimeter.
- No height-change (looming) detection.
- Pure yaw is indistinguishable from translation.
- No exposure-failure detection beyond the variance gate.

---

## Appendix: hardware reference

**AI-Thinker ESP32-CAM pin map** (in `main/camera_pins.h`):

| Signal | GPIO | | Signal | GPIO |
|---|---|---|---|---|
| PWDN | 32 | | Y9 (D7) | 35 |
| RESET | −1 (none) | | Y8 (D6) | 34 |
| **XCLK** | **0** ⚠ strap pin | | Y7 (D5) | 39 |
| SIOD (SDA) | 26 | | Y6 (D4) | 36 |
| SIOC (SCL) | 27 | | Y5 (D3) | 21 |
| VSYNC | 25 | | Y4 (D2) | 19 |
| HREF | 23 | | Y3 (D1) | 18 |
| PCLK | 22 | | Y2 (D0) | 5 |
| Flash LED | 4 (1 W — heat source, forced low) | | | |

**Toolchain:** `source $HOME/.espressif/tools/activate_idf_v5.5.5.sh`.
`idf.py` is a shell *function* and is invisible to non-interactive shells;
invoke it directly:

```
$HOME/.espressif/tools/python/v5.5.5/venv/bin/python \
  $HOME/.espressif/v5.5.5/esp-idf/tools/idf.py -C <project> ...
```

**Output CSV format** (16 fields):

```
t_ms, ok, dx_px, dy_px, vx_mm_s, vy_mm_s, x_mm, y_mm,
spread_px, n_valid, n_total, n_flat, n_edge, n_ambig, dt_ms, proc_us
```
