# Icarus Blimp

An indoor autonomous blimp built for the **IAR-LARC 2026** race. It has to hover at
**2 m** (allowed band 1.85–2.25 m) and fly between four **red mylar waypoint
balloons** (22", 50–60 cm, centre 2 m above the floor, on a 4 m × 2 m rectangle in a
10 m × 5 m arena).

The airframe carries three ESP32 boards. Each one does a single job and reports plain
CSV over UART:

| Board | Firmware | Job |
|---|---|---|
| ESP32-S3 (Waveshare ESP32-S3-Zero) | [`firmware/projects/i2c_tools/`](firmware/projects/i2c_tools/) | Flight controller in progress: GY-88 IMU/magnetometer/barometer, ToF altimeter, four DRV8833 thrusters |
| ESP32-CAM (AI-Thinker), looking down | [`firmware/projects/optflow_cam/`](firmware/projects/optflow_cam/) | Optical flow for ground speed |
| ESP32-CAM (AI-Thinker), looking forward | [`firmware/projects/target_cam/`](firmware/projects/target_cam/) | **Balloon detector**: finds the waypoints and reports their bearing and range |

> **Status (2026-11):** each board works on the bench by itself. The boards are not
> linked yet, and the blimp has not flown autonomously. Throughout this repository,
> figures marked *measured* come from hardware or labelled data, and *reasoned* marks
> what is inferred but not yet tested.

---

## The balloon detector (`target_cam`)

The forward camera finds up to **four balloons** in every frame and sends one CSV row
per frame. Each row gives the bearing (azimuth/elevation), the apparent size, the range
(from the known 550 mm diameter) and, from stage 2 on, a confidence. That row is
everything the flight controller will ever learn about the waypoints.

```
OV2640 ──RGB565 160×120 @ 12.5 FPS──> frame buffer
   │
 [0] HSV pixel test ──┐
 [1] learned colour table ─┴─> mask ─> 2×2 cells ─> union-find ─> 4 largest blobs
                                                                    │
 [2] 12 features per blob ─> logistic classifier ─> confidence ─> filter
                                                                    │
 [3] 16×16 crop of the top blobs ─> int8 CNN ─> confidence ─> filter
                                                                    │
          az/el (pinhole), range = 550 mm · f / longer box side ─> CSV v2 row
```

**Design rules every stage follows:**

- **The camera's frame rate sets the pace.** The loop takes each frame as the sensor
  delivers it, so `dt_ms` is 80.0 on every row with all stages enabled (*measured*:
  3 752 rows in a row, none late).
- **Portable C only.** The detector is plain C next to `blob_detect.c`, with no
  TensorFlow Lite Micro and no heap. It runs bit-for-bit the same on the PC as on the
  camera.
- **It fails honestly.** `ok=0` means "no measurement", not "target dead ahead". A blob
  with low confidence is dropped. The detector never guesses a position.
- **The overlay on the PC is the camera's own code.** The viewer loads the firmware's
  C files as a shared library (`libblob_detect.so`, through ctypes), so what you see
  on the PC is exactly what the camera decides.
- **Fallback is never silent.** A stage runs only when all the models below it are
  valid. Every row's `stage` column and the camera's `#!CFG` report say which stage
  actually ran.

<details>
<summary><b>Why a cascade, and not a neural network over the whole frame</b></summary>

<br>

| Option | Verdict |
|---|---|
| **Colour proposals + small per-blob models (this)** | A few ms per frame and deterministic. Every failure can be explained with the mask dump. The cost grows with the number of blobs (at most 4), not with the frame size. |
| ESP-DL / TFLM person-style detector over the frame | Even MobileNet v1 0.25 at 96×96 is about 7.5 M MACs, or 125–300+ ms per frame on the original ESP32 (no SIMD). It does not fit in an 80 ms period, and the board runs hot. |
| Stream JPEG to a laptop and run YOLO | The most capable option, but WiFi is the largest heat source on this board. It adds latency, and the airframe would depend on a radio link. The firmware never starts a radio. |

The colour stage is a cheap way to propose candidates. Each later stage looks only at
what the stage before it found.

</details>

<details>
<summary><b>Stage 0 — HSV colour test (the safety net)</b></summary>

<br>

Source: [`main/blob_detect.c`](firmware/projects/target_cam/main/blob_detect.c). Status:
**implemented, tested on the host and on the board.**

1. **Pixel test.** A pixel matches when its hue is within ±tol of the target hue and
   its saturation and brightness clear their floors. The tests run from cheapest to
   most expensive, and saturation is compared without a divide, so most pixels are
   rejected before any division.
2. **2×2 cells.** A cell is set when at least half its pixels match. This removes
   speckle and makes labelling 4× cheaper.
3. **Connected components** (4-connectivity, union-find). The memory is sized for the
   worst case, a full-frame checkerboard, so no input can overflow it (checked with
   ASan).
4. **The 4 largest blobs** get an exact centroid and box computed from their matching
   *pixels*, including edge cells below the threshold. Without that edge pass, curved
   edges lost about 3 % of the area and biased the centroid by up to 0.07 px. With it,
   the error is 0.000 px.

Hue is used instead of RGB because a red balloon in shadow is *darker*, not *less red*.
Four colour presets are built in: `red` (the competition colour), `blue`, `green` and
`orange`. Stage 0 needs no model, can be re-tuned at the venue in minutes, and is
where every other stage falls back to.

**What limits it (measured):** the mylar mirrors the room. On the bench the balloon's
centre read hue 304 ± 109°, saturation 78, and the red preset missed the balloon at
1.5 m. On 20 hand-labelled frames, **the choice of calibration swings false blobs
from 0 to 38**, and even the best setting matches only about half of each balloon's
pixels.

</details>

<details>
<summary><b>Stage 1 — Learned colour table</b></summary>

<br>

Sources: [`main/color_lut.c`](firmware/projects/target_cam/main/color_lut.c),
[`host/train/train_lut.py`](firmware/projects/target_cam/host/train/train_lut.py). Status:
**implemented, tested on the host and on the board.**

The HSV test is replaced by a lookup: **one bit for each of the 65 536 RGB565
colours, 8 KB in all**, learned from hand-labelled frames. Everything after the pixel
test is unchanged. The trainer chooses its own smoothing and evidence settings by
leaving one session out at a time.

**Held-out scores (measured, trained on one session and scored on the other):**

| Direction | Detector | Balloons found | Pixel F1 |
|---|---|---|---|
| s1 → s2 | HSV, best setting | 7 / 7 | 53.8 % |
| s1 → s2 | **colour table** | 7 / 7 | **66.2 %** |
| s2 → s1 | HSV preset | 6 / 7 | 60.4 % |
| s2 → s1 | **colour table** | **7 / 7** | **75.0 %** |

**On the board (measured, same still scene, 2026-10-15):**

| | Stage 0 | Stage 1 |
|---|---|---|
| Rows with a target | 42.7 % | **100 %** |
| Blob 1 jumps > 10 px | 82 | **0** |
| Pixel pass, p50 | 8.5 ms | **4.7 ms** (the same on every frame) |
| Total detection, p50 | 9.6 ms | **8.8 ms** |
| Rows off the 80 ms period | 0 | 0 |

The table learns the camera's raw colours, so it belongs to the lighting and white
balance it was trained under. Retrain it at a new venue, and fix the white-balance
gains first (`Wc`, then `!save`).

</details>

<details>
<summary><b>Stage 2 — Blob classifier</b></summary>

<br>

Sources: [`main/blob_feat.c`](firmware/projects/target_cam/main/blob_feat.c),
[`main/blob_clf.c`](firmware/projects/target_cam/main/blob_clf.c),
[`host/train/train_clf.py`](firmware/projects/target_cam/host/train/train_clf.py). Status:
**implemented. Training is in progress.**

Each blob gets 12 shape and colour features (mean saturation, hue spread, a highlight
"ring" around the blob, fill and others), computed in about 1.9 ms per frame. A
logistic classifier turns them into a confidence. Blobs below the threshold (`!thr2`)
are dropped, and the rest move up in size order. Training data comes from the camera
itself: `!collect 1` sends one `#!BLOB` record (features plus a 24×24 crop) per
reported blob, and tcview labels them.

**Where it stands (measured, 2026-10-29):** orange and pink balloons are the hard
negatives. On colour alone, stage 1 puts a false blob 1 on 77 of 102 frames that have
no red balloon in them. Held out, the first classifier lowers the cost only from 94 to
83. Sessions recorded with different white-balance gains are a likely cause
(*reasoned*), so the next data is collected with fixed gains.

</details>

<details>
<summary><b>Stage 3 — Crop CNN</b></summary>

<br>

Sources: [`main/blob_cnn.c`](firmware/projects/target_cam/main/blob_cnn.c),
[`host/train/train_cnn.py`](firmware/projects/target_cam/host/train/train_cnn.py). Status:
**implemented, and its first training run is done.**

The CNN is small: three 3×3 convolutions (8–16–16 channels, ReLU), two max-pools, a
global average and one logistic unit, run on a 16×16 crop (24×24 also supported).

- Weights are int8 and activations uint8, with integer requantisation, so everything
  up to the logit is integer and the PC reproduces it bit for bit.
- About **166 k multiply-adds per crop**, **3.7 KB of weights**, and about 15 KB of
  static buffers.
- Only the `cnn_k` largest blobs are checked (`!cnnk`). `!cnnbench` times the network
  on the board.
- The trainer is **numpy only**: hand-written backpropagation checked against finite
  differences, Adam, augmentation and post-training quantisation. A numpy copy of the
  integer network is checked against the C version on every run.

**First run (measured, PC):** 205 crops, 23 s to train. The integer network decides
like the float one on every crop. Held out, it does not yet lower the cost below stage
2 alone, because there is too little data and the sessions used mixed white balance.
More data under fixed gains comes next.

</details>

<details>
<summary><b>Geometry and range</b></summary>

<br>

QQVGA with the stock 65° diagonal lens gives a 54° horizontal FOV, so
f = 80 / tan 27° = **157 px**, about **0.365° per pixel** at the centre.

| Range | Balloon radius | Detected | Box range reads |
|---|---|---|---|
| 1 m | 43 px | yes | 1.08 m |
| 2 m | 22 px | yes | 2.11 m |
| 4 m | 11 px | yes | 4.32 m |
| 10 m | 4 px | yes | 10.79 m |

These figures come from the host suite's shaded mylar model. Every balloon in the
arena is within detection range.

- **Range uses the longer side of the box, not the area.** The specular highlight and
  the dark far side both fail the colour test, so the area undercounts the balloon.
  The remaining bias is systematic and is calibrated with `d<mm>` against a balloon at
  a measured distance.
- **A clipped target reports `range_mm = -1`.** A balloon cut off by the frame edge
  looks smaller, so its range would read long.
- **Angles are in the body frame.** The camera has no IMU, so roll and pitch tilt
  az/el. De-rotation belongs on the flight controller.

</details>

<details>
<summary><b>Output format (CSV v2, 35 columns at 115200 baud)</b></summary>

<br>

```
t_ms,ok,cx_px,cy_px,az_deg,el_deg,area_px,w_px,h_px,fill,range_mm,clipped,blobs,matched_px,
b2_az,b2_el,b2_range_mm,b3_az,b3_el,b3_range_mm,b4_az,b4_el,b4_range_mm,dt_ms,proc_us,
stage,conf,b2_conf,b3_conf,b4_conf,t_px_us,t_b_us,t_c_us,loop_us,csv_v
```

| Field | Meaning |
|---|---|
| `ok` | **0 means no target**, and every position field must then be ignored |
| `cx_px` … `clipped` | The largest blob, usually the nearest balloon |
| `az_deg`, `el_deg` | Bearing from the optical axis; +az is right, +el is up |
| `range_mm` | `550 · f / max(w, h)`, or −1 when clipped or turned off |
| `bN_*` | Blobs 2–4 in decreasing size. Choosing the next waypoint is the navigator's job. |
| `stage` | The stage that actually ran for this row, after any fallback |
| `conf`, `bN_conf` | Confidence from 0 to 1; −1 when not computed |
| `dt_ms`, `proc_us`, `t_*_us`, `loop_us` | Timing for each row, so the 80 ms budget can be checked in flight |

The first 25 columns are unchanged from v1. Readers tell versions apart by `csv_v`,
never by counting columns. The full field reference is in the
[target_cam README](firmware/projects/target_cam/README.md#output-format).

</details>

<details>
<summary><b>Runtime commands (serial)</b></summary>

<br>

| Command | Effect |
|---|---|
| `p red` / `p blue` / `p green` / `p orange` | Colour preset (stage 0) |
| `c` | Calibrate the colour from the centre 16×16 patch |
| `H` `T` `S` `V` `<n>` | Hue, hue tolerance, saturation floor, brightness floor |
| `d<mm>` | Real target diameter for range (default 550; `d0` turns range off) |
| `f<fps>` | Frame rate, as a whole number of sensor frames |
| `Wc` / `W<r>,<g>,<b>` / `w<0\|1\|2>` | Fixed white balance from a white card / set the gains / choose the mode |
| `!stage <0-3>` | Detector stage (falls back if a model is missing) |
| `!thr2` / `!thr3` / `!cnnk` | Classifier threshold / CNN threshold / how many blobs the CNN checks |
| `!collect <0\|1>` | Stream `#!BLOB` training records |
| `!dump` / `!upload` / `!unload` | Read, install or erase models over the serial link (CRC-checked) |
| `!flip <0\|1>` | Turn the image 180° for a board mounted upside down |
| `!save` / `!load` / `!reset` | Save the settings to flash (NVS), reload them, erase them |
| `g` / `v<0\|1>` / `m` | Send one raw frame / stream frames / dump the mask |
| `?` / `k` | Banner / machine-readable `#!CFG` |

All the commands are listed in the
[target_cam README](firmware/projects/target_cam/README.md#runtime-commands).

</details>

<details>
<summary><b>tcview — the PC viewer, labeller and trainer</b></summary>

<br>

[`host/viewer/`](firmware/projects/target_cam/host/viewer/) is a PyQt6 + pyqtgraph app that
connects over the same USB cable used for flashing. Opening the port does not reset the
camera.

| Tab | What it does |
|---|---|
| **Image & calibration** | Raw pixels at ×4, a mask overlay, sliders, pixel and region colour readouts, save to flash |
| **Flight data** | Only the CSV the flight controller will receive, as a dashboard. A **FLIGHT-IDENTICAL** banner, driven by the camera's own `#!CFG` report, confirms the timing matches flight. |
| **Label frames** | Paint saved frames as balloon or background, and score the detector against the labels |
| **Blobs** | Label the `#!BLOB` crops for the classifier |
| **Train / Verify** | Train stages 1–3, then check the candidates on held-out sessions |
| **Firmware** | Build and flash, install models, check what the camera actually holds |

```bash
conda activate targetcam
cd firmware/projects/target_cam/host/viewer
python -m tcview --port /dev/ttyACM0   # the real camera
python -m tcview --fake                # a simulated camera, no hardware needed
python -m tcview --label [DIR]         # label saved frames only
```

The setup from a clean Linux machine (in Spanish) is in [`GUI.md`](GUI.md).

</details>

<details>
<summary><b>Build, flash and test the detector</b></summary>

<br>

**Host tests (no hardware needed):**

```bash
make -C firmware/projects/target_cam/host test     # blob detector, pipeline, features
cd firmware/projects/target_cam/host/viewer && pytest
```

**Firmware** (ESP-IDF v5.5.5, target `esp32`):

```bash
idf.py -C firmware/projects/target_cam set-target esp32
idf.py -C firmware/projects/target_cam build
```

To flash through the **ESP32-CAM-MB**, hold **BOOT**, replug the USB cable, and then
run from `target_cam/build`:

```bash
python -m esptool --chip esp32 -p /dev/ttyACM0 -b 115200 \
    --before no_reset --after no_reset write_flash @flash_args
```

Then release BOOT and replug. Stay at 115200, because the bridge has wedged at higher
speeds. GPIO0 is the camera's XCLK on this board, so the firmware keeps a 5 s reflash
window after boot.

</details>

<details>
<summary><b>Retraining at a new venue</b></summary>

<br>

1. Fix the white balance against a white card (`Wc`, then `!save`).
2. Capture and label a session in tcview (`data/<session>/`).
3. Score the **current** table on it *before* training. This is the only unbiased
   score.
4. Train a trial table with `python host/train/train_lut.py --out data/lut_try data/red_s1 …`.
5. Keep the trial table only if it finds every balloon and puts no false blob 1 on any
   empty frame.
6. Install it, rebuild and flash, or upload it over the link with `!upload`.

**Safety net:** if a model misbehaves, `!stage 0`, recalibrate HSV and fly that.

The full procedure and a troubleshooting table are in the
[target_cam README](firmware/projects/target_cam/README.md#retraining-the-colour-table-stage-1).

</details>

<details>
<summary><b>Detector documentation</b></summary>

<br>

| Document | Contents |
|---|---|
| [`firmware/projects/target_cam/README.md`](firmware/projects/target_cam/README.md) | Firmware reference: commands, CSV, records, retraining |
| [`RECOGNITION.md`](RECOGNITION.md) | The whole recognition system in one place (Spanish) |
| [`docs/learned-detector-report.md`](docs/learned-detector-report.md) | Every measurement, phase by phase, with what is not yet established |
| [`docs/artifacts/Balloon_NN_Feasibility.md`](docs/artifacts/Balloon_NN_Feasibility.md) | CPU budget and the options that were considered |
| [`host/viewer/README.md`](firmware/projects/target_cam/host/viewer/README.md) | tcview and the `#!` serial protocol |
| [`docs/lens-focus-procedure.md`](docs/lens-focus-procedure.md) | Focusing the OV2640 lens |

</details>

---

## The rest of the system

<details>
<summary><b>Flight controller — ESP32-S3 (<code>i2c_tools</code>)</b></summary>

<br>

The flight controller is still being built. It grew out of Espressif's `i2c_tools`
example, so its console prompt is still `i2c-tools>`, and every sensor and motor has
its own bench command (`help` lists them).

- **Sensors:** GY-88 (MPU-6050 `0x68`, HMC5883L `0x1E`, BMP180 `0x77`) on a single I2C
  bus (SDA 4, SCL 5), using the new `driver/i2c_master.h` API.
- **Altimeter:** a VL53L1X time-of-flight sensor is the primary. Whether it reaches
  2.25 m under room ambient IR is still an open question, to be settled by
  measurement. A Parallax PING))) ultrasonic sensor is the backup (parked for now).
  Slant range is corrected for tilt: `h = d·cos θ`.
- **Thrusters:** four motors on DRV8833 drivers, using all eight LEDC channels for
  glitch-free reversal. The driver claims its pins before anything else boots.
  **Failsafe:** if no thrust command arrives within 1 s, every motor coasts.
- **Status LED:** a WS2812 on GPIO21, driven by RMT.

```bash
idf.py -C firmware/projects/i2c_tools set-target esp32s3
idf.py -C firmware/projects/i2c_tools -p /dev/ttyACM0 flash
```

The S3-Zero uses native USB, so opening `/dev/ttyACM0` resets it. Wait about 3 s
before you type commands.

</details>

<details>
<summary><b>Optical flow — downward ESP32-CAM (<code>optflow_cam</code>)</b></summary>

<br>

A SAD block matcher in portable C ([`main/flow_match.c`](firmware/projects/optflow_cam/main/flow_match.c))
measures ground speed from the downward camera. Like the detector, it **fails
honestly**: a match on the search boundary is rejected, never clamped, and `spread_px`
is its confidence figure. It reports raw `dx_px`/`dy_px`, because rotation
contaminates the flow (about 35 mm/s per °/s) and the gyro is on the S3.

```bash
make -C firmware/projects/optflow_cam/host test    # verification suite
make -C firmware/projects/optflow_cam/host sweep   # sub-pixel bias sweep
```

The full report is in [`docs/esp32-cam-optical-flow-report.md`](docs/esp32-cam-optical-flow-report.md).

</details>

<details>
<summary><b>Host tools (<code>software/</code>)</b></summary>

<br>

These Python bench tools (dynamics model, motor diagnostics, a GUI) were imported from
a separate quadcopter project, **esp-drone**. They speak CRTP over UDP to esp-drone
firmware, not to anything in this repository yet.
[`docs/esp-drone-tools-report.md`](docs/esp-drone-tools-report.md) §8 lists what would
have to change. Setup instructions are in [`software/SETUP.md`](software/SETUP.md).

</details>

<details>
<summary><b>Roadmap</b></summary>

<br>

- [x] Stage 0–1 detector on the board at 12.5 FPS
- [x] Stage 2 and 3 models, trainers and tcview tabs
- [x] Models over the serial link (`!dump` / `!upload` / `!unload`)
- [ ] More labelled sessions under fixed white balance, then retrain stages 2–3
- [ ] Settle the ToF altimeter's range at 2 m by measurement
- [ ] Link the cameras to the S3 over UART ([`docs/inter-board-link-plan.md`](docs/inter-board-link-plan.md))
- [ ] Sensor fusion on the S3 ([`docs/sensor-fusion-plan.md`](docs/sensor-fusion-plan.md), proposal)
- [ ] Altitude hold, then waypoint navigation

</details>

<details>
<summary><b>Repository layout</b></summary>

<br>

```
firmware/projects/
  i2c_tools/      ESP32-S3 flight controller (sensors, motors, console)
  optflow_cam/    downward optical flow + host test suite
  target_cam/     forward balloon detector
    main/         firmware: blob_detect, color_lut, blob_feat, blob_clf, blob_cnn, tc_pipeline
    host/         C tests, trainers (train/), evaluation tools (tools/), tcview (viewer/)
  blink/          toolchain smoke test
software/         esp-drone bench tools (Python)
docs/             technical reports (dated; measured kept separate from reasoned)
```

</details>

---

## Toolchain

- **ESP-IDF v5.5.5**. `sdkconfig` is generated. Persistent settings go in
  `sdkconfig.defaults*` or `Kconfig.projbuild`.
- **Python 3.10** in conda envs: `targetcam` for tcview and the trainers, and
  `blimp-tools` for `software/`. Set `PYTHONNOUSERSITE=1` before any `pip install`.
