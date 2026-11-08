# optflow_cam — downward-looking optical flow on an ESP32-CAM

Measures the blimp's horizontal motion by matching successive grayscale frames
of the floor, 2 m below. Paced to 10–20 FPS deliberately: that is all a blimp
needs, and running the camera slowly is the most effective thing available for
keeping an ESP32-CAM from cooking itself.

> **Hardware status: untested.** No ESP32-CAM was connected when this was
> written — `/dev/ttyACM0` is the ESP32-S3-Zero, and `/dev/video0/1` is the
> laptop's own webcam. The firmware **compiles clean for `esp32` with zero
> warnings**, and the matching algorithm is **verified on this PC** (see
> [Verification](#verification)), but nothing has been run against a real
> camera. The camera-specific parts — register settings, frame timing, achieved
> FPS, thermal behaviour — are reasoned from the datasheets and driver source,
> not measured.

---

## Why this approach

The [sensor fusion plan](../../../docs/sensor-fusion-plan.md) established, with
arithmetic rather than assertion, that **horizontal position is not observable**
with the blimp's current sensor set: at the measured 70 mg accelerometer bias,
dead reckoning drifts 0.34 m in 1 s and 34 m in 10 s. No filter fixes that. A new
sensor is required, and a downward camera is the cheapest one that directly
measures horizontal velocity.

### Surveyed projects

| Project | Verdict |
|---|---|
| `espressif/esp32-camera` | **Used.** The official driver, Apache-2.0, actively maintained. Everything else depends on it anyway. |
| `thomas-pegot/esp32-motion` | Full-frame Lucas–Kanade and block motion estimation. Capable, but far heavier than needed and built around per-pixel motion fields. |
| `qqqlab/ESP32-Optical-Flow` | Closest in spirit — downward flow for a drone. Useful as a reference for the geometry, but tangled with that project's own flight stack. |
| `djtrance/ESP32-CAM-opticalFlow` | Arduino sketch, 1:1 full-frame correlation. Too slow and too coupled to Arduino. |
| `tovask/ESP32-CAM` | Streaming/experiment scaffolding, not a flow estimator. |

None of them is a drop-in sparse block matcher with honest failure reporting, so
the matcher here (234 lines) is written from scratch — and written as portable
C with no ESP-IDF dependency, which is what makes host verification possible.

### Why SAD block matching, not Lucas–Kanade

- Integer arithmetic throughout; no image gradients, no matrix inverse.
- A 5×5 sparse grid costs ~25 × (15×15) × 256 byte-differences per frame — a
  couple of milliseconds at 160 MHz, leaving the rest of the period idle (which
  is the thermal plan).
- The full SAD surface is inspectable, so when it fails you can see *why*.
- Lucas–Kanade assumes small motion and fails silently outside that; block
  matching's failure is a match on the search boundary, which is **detectable**.

---

## Geometry at 2 m

Stock OV2640 lens, 65° diagonal, 4:3 → h-FOV 54.0°, v-FOV 41.8°.

| | QQVGA 160×120 (used) | QVGA 320×240 |
|---|---|---|
| Ground footprint | 2.04 × 1.53 m | 2.04 × 1.53 m |
| Ground sample distance | **12.74 mm/px** | 6.37 mm/px |
| Velocity per px @ 15 FPS | 191 mm/s | 96 mm/s |
| Resolution @ 0.1 px | **19 mm/s** | 10 mm/s |
| Max measurable (±7 px) | **±1.34 m/s** | ±0.67 m/s |
| Match cost | ~2 ms | ~8 ms |

QQVGA is the right choice: ±1.34 m/s comfortably covers a blimp, 19 mm/s
resolution is finer than it can hold station, and the frame is small enough to
keep both buffers in internal RAM.

### The rotation problem — read this before trusting `vx`/`vy`

Rotating the camera by angle θ shifts the image by `f·θ` pixels, where `f` is
the focal length in pixels. Converting that to ground distance multiplies by the
GSD, which is proportional to height — and the two height terms **cancel**:

```
apparent ground velocity from rotation  =  height × ω      [rad/s]
                                        =  2000 mm × (π/180)  per deg/s
                                        ≈  34.9 mm/s per deg/s
```

So about **35 mm/s of phantom translation for every 1 °/s of rotation**, at any
height, and it looks exactly like real motion. A blimp yawing at a leisurely
10 °/s manufactures 0.35 m/s of velocity that isn't there.

**This firmware cannot fix that — there is no IMU on the camera board.** It
therefore reports raw pixel flow (`dx_px`, `dy_px`) *alongside* the velocity, so
whichever board owns the gyro can subtract `f·ω·dt` before using it. Treat
`vx_mm_s`/`vy_mm_s` as valid only while the airframe is not turning.

---

## Thermal measures

ESP32-CAM boards are widely reported above 90 °C under load. The two main causes
are the AMS1117 linear regulator burning (5 V − 3.3 V) × current as heat, and
WiFi transmit bursts drawing ~240 mA. Every measure below is in the code:

| Measure | Where | Effect |
|---|---|---|
| Radios never initialised | no `esp_wifi_*` call anywhere | Removes the largest load. **Verified:** `esp_wifi_init` is absent from the linked ELF. |
| XCLK 10 MHz, not 20 | `CONFIG_OPTFLOW_XCLK_MHZ` | Roughly halves the sensor's own dissipation. |
| CPU 160 MHz, not 240 | `sdkconfig.defaults` | Lower core power; matcher only needs ~2 ms/frame. |
| QQVGA grayscale | `FRAME_SIZE` | Least pixel traffic of any useful format. |
| Explicit frame pacing | `xTaskDelayUntil` | At 15 FPS the CPU is idle ~97% of each period. **The idle time is the point.** |
| Flash LED driven low | `quiesce_heat_sources()` | GPIO4 drives a 1 W LED that floats bright on some boards. |
| Frame buffers in DRAM | `CAMERA_FB_IN_DRAM` | SPI PSRAM is never clocked for pixel traffic. |
| Brownout detector on | `sdkconfig.defaults` | A heat/supply failure becomes a logged reset, not silent corruption. |

Also do these in hardware: feed **5 V** to the 5 V pin (not 3.3 V), stick a
small heatsink on the shield can, and leave airflow around it. Under a blimp
envelope there is no airflow, so the pacing matters more than usual.

**You cannot read the die temperature in software here.** The original ESP32 has
no usable internal temperature sensor — only the S2/S3/C3/C6 and later do. Use
an external probe or an IR thermometer on the shield can.

---

## Building and flashing

```bash
source $HOME/.espressif/tools/activate_idf_v5.5.5.sh

# idf.py is a shell function in this install; call it directly from scripts
IDF_PY="$HOME/.espressif/tools/python/v5.5.5/venv/bin/python \
        $HOME/.espressif/v5.5.5/esp-idf/tools/idf.py"

$IDF_PY -C firmware/projects/optflow_cam set-target esp32
$IDF_PY -C firmware/projects/optflow_cam build
$IDF_PY -C firmware/projects/optflow_cam -p /dev/ttyUSB0 flash
```

Last clean build: **310 KB**, 70% of the app partition free, zero warnings.

> **Flashing an AI-Thinker ESP32-CAM needs a USB-serial adapter** — the board
> has no USB port. Wire adapter TX→U0R, RX→U0T, GND→GND, and **jumper GPIO0 to
> GND** before power-up to enter the bootloader; remove the jumper and reset to
> run. Power the board from 5 V, not the adapter's 3.3 V.

## Running

```bash
host/flow_monitor.py --port /dev/ttyUSB0
host/flow_monitor.py --port /dev/ttyUSB0 --cmd h1500 --log run1.csv
```

Runtime commands (type into the serial console, or pass with `--cmd`):

| Command | Effect |
|---|---|
| `h<mm>` | Set assumed height above the floor. **Velocity scales linearly with this** — get it wrong and every number is wrong by the same factor. |
| `f<fps>` | Change the target frame rate, 1–30. |
| `z` | Zero the integrated position. |
| `s` | Toggle the CSV stream. |
| `?` | Reprint the banner and current configuration. |

## Output format

```
t_ms,ok,dx_px,dy_px,vx_mm_s,vy_mm_s,x_mm,y_mm,spread_px,valid,total,flat,edge,ambig,dt_ms,proc_us
```

| Field | Meaning |
|---|---|
| `ok` | **0 means no measurement, not zero motion.** Never feed an `ok=0` row to an estimator. |
| `dx_px`, `dy_px` | Median image flow, previous→current. Image axes: +x right, +y down. **Use these for gyro de-rotation.** |
| `vx_mm_s`, `vy_mm_s` | Body velocity = −flow × GSD / dt. Rotation-contaminated; see above. |
| `x_mm`, `y_mm` | Integrated displacement. Dead reckoning — drifts without bound. For bench sanity checks only. |
| `spread_px` | Median absolute deviation across blocks. **The honest confidence number** — use this, not `valid`. Below ~0.3 px the blocks agree; above ~1 px something is wrong (a moving object, motion blur, or motion beyond the search radius). |
| `valid`/`total` | Blocks that produced a match. |
| `flat` | Rejected: variance below `min_var`. A featureless floor. |
| `edge` | Rejected: best match on the search boundary, i.e. **motion too fast to measure**. A rising `edge` count is the signal to raise FPS or lower the camera. |
| `ambig` | Rejected: no clean SAD minimum. Repetitive texture (tiles, carpet pattern). |
| `dt_ms` | **Measured** interval between captures, not the target. Velocity uses this. |
| `proc_us` | Matcher cost. Your real FPS headroom. |

## Verification

The matcher is portable C, so the exact code that runs on the camera is tested
here with no hardware:

```bash
cd firmware/projects/optflow_cam/host && make test
```

Against two octaves of bilinearly-upsampled value noise (aperiodic, so matches
are unambiguous, and band-limited, so sub-pixel shifting is meaningful):

```
1. integer shifts                       8/8 exact to 0.00 px
2. sub-pixel shifts                     7/7, worst error 0.089 px
3. featureless frame            correctly refuses: ok=0, flat 25/25
4. motion beyond search radius  correctly rejected, never pegged

mean |error| 0.024 px over 30 axes, worst 0.089 px
PASSED (0 failures)
```

Test 4 is the one that matters most for flight safety. A matcher that clamps an
over-range motion to ±search reports a confident, plausible, wrong number — the
failure that makes an estimator diverge while every health indicator looks fine.
Here, 8 px and 10 px motions return `ok=0` with `edge 25/25`.

0.089 px worst-case sub-pixel error corresponds to **17 mm/s** at this geometry.

## Algorithm

Per frame, for each of 25 blocks on a 5×5 grid:

1. **Variance gate** — reject the block if it has no texture to track.
2. **Full SAD surface** over a ±7 px search window (225 positions × 256 px).
   Deliberately *not* early-exited: early exit clamps the neighbour values that
   step 4 needs.
3. **Boundary rejection** — if the minimum is on the window edge, the true
   motion is outside the search range. Discard rather than report ±7.
4. **Parabolic sub-pixel fit** on the three SAD values straddling the minimum,
   independently per axis.

Then aggregate: **median** of the surviving per-block vectors, with the median
absolute deviation reported as `spread`. Median, not mean, specifically so that a
person walking under the airframe, or one blank patch of floor, cannot drag the
estimate with them. Fewer than 3 valid blocks returns no measurement — two
blocks cannot outvote anything.

## Files

| File | Lines | Purpose |
|---|---|---|
| `main/flow_match.h/.c` | 308 | The matcher. Portable C, no ESP-IDF dependency — this is why host testing works. |
| `main/optflow_main.c` | 399 | Camera setup, frame pacing, CSV output, runtime commands. |
| `main/camera_pins.h` | 75 | Pin maps: AI-Thinker, ESP-EYE, XIAO S3. |
| `main/Kconfig.projbuild` | 85 | `menuconfig` options for board, FPS, XCLK, height, FOV, matcher. |
| `sdkconfig.defaults` | 36 | Thermal and timing defaults, each one commented with its reason. |
| `host/test_flow_match.c` | 202 | The verification suite above. |
| `host/flow_monitor.py` | 123 | Live viewer and CSV logger (needs `pyserial`). |

## Known gaps

1. **Nothing has run on hardware.** Highest priority is a bench test: point it
   at a textured floor at a measured height, slide it a measured distance, and
   check `x_mm`.
2. **The FOV is assumed, not calibrated.** 54° is derived from the nominal 65°
   diagonal. It is a direct multiplier on every velocity. Calibrate it: at a
   known height, translate a known distance and solve for the FOV that makes
   `x_mm` correct.
3. **No gyro de-rotation** (see above). Architectural, not a bug: it requires
   the flow and gyro to meet, which happens on the S3.
4. **No height input.** Height is typed in. In flight it must come from the
   PING))) altimeter, or the velocity scale will be wrong whenever the blimp
   changes altitude.
5. **Motion blur is unmodelled.** Auto-exposure over a dim floor will pick a
   long integration time, and a blurred frame has less texture to match. Watch
   `flat` and `spread` rise together under poor light.
6. **Exposure steps look like motion.** Mitigated by letting AE settle before
   the first match and disabling AEC2, but a sudden lighting change will still
   produce one bad frame pair. `spread` should catch it.
7. **Frame rate is not verified.** 15 FPS is requested; whether the OV2640
   delivers it at 10 MHz XCLK is a measurement, not a calculation. `dt_ms` in
   the output is there to answer this on first power-up.
