# target_cam — forward-looking colour-target detection on an ESP32-CAM

The blimp's nose camera. Its job in the IAR-LARC 2026 race is to find the
**red mylar waypoint balloons** (22", 50–60 cm, centre 2 m above the floor, four
of them on a 4 m × 2 m rectangle) so the airframe can steer from one to the
next. It reports up to **four blobs** of one colour, largest (usually nearest)
first, and streams them as CSV on UART0: bearing left/right and up/down,
apparent size, and range from the known 550 mm balloon size.

Four colour presets are built in, for bench testing with whatever balloons
are to hand: **red** (the competition colour, default), **blue**, **green**
and **orange**.

It is the sibling of [`optflow_cam`](../optflow_cam/README.md) and is built the
same way. The detector is portable C, verified on the host before any hardware.
The firmware never starts a radio, paces its frames, reports its own failures,
and keeps that project's board handling: a boot report before the camera powers
up, the GPIO0 reflash window, and the flash LED driven low.

> **Hardware status: first bench run 2026-10-13; not yet calibrated.**
> - **Measured on the bench (AI-Thinker ESP32-CAM on an ESP32-CAM-MB):** it
>   boots cleanly, holds a steady **10.0 FPS** (`dt_ms` ≈ 100), and detection
>   costs **≈ 8.2 ms** per frame. The RGB565 byte order is **big-endian**,
>   as the source said.
> - **Since 2026-10-14 the default pace is 12.5 FPS (80 ms), measured the same
>   day:** 751 rows in 60 s (12.505 rows/s), `dt_ms` 80.0 on every row, no
>   missing rows, detection 7.3–8.2 ms. Under `f30` the loop waits on the
>   sensor and runs at the same 12.505 FPS, so this is the sensor's own rate in
>   RGB565 and every frame is used. The 10 FPS figure above is from the old
>   default. On the camera's clock the period averages 79.97 ms, not 80.00.
> - **Since 2026-10-14 (later) the sensor is the clock.** A 2-min run showed
>   the 80.000 ms wait beating against the sensor's measured 79.968 ms frame:
>   the frame taken fell 32 µs further behind every period, a frame was
>   missed about every 200 s (one `dt_ms` 99.1 seen), and after it the loop
>   waited up to 19 ms in `fb_get`. The loop now takes each frame as the
>   sensor delivers it, so there is nothing left to beat against.
> - **The red preset did not detect the real balloon at 1.5 m.** The centre
>   patch read hue 304 ± 109, sat 78, val 188: a wide hue spread and low
>   saturation, probably from the mylar mirroring the room. Loose thresholds
>   (`H3 T45 S35 V40`) found a 17×17 px blob. `H0 T30 S50 V40` also matched
>   wood clutter in the lower left of the view. Calibrating against the real
>   image is the next step; that is what the [PC viewer](host/viewer/README.md)
>   is for.
> - **Built but not yet run on hardware:** the viewer protocol (frames, baud
>   switching, `#!` lines) and settings in flash. Their PC side is tested
>   against a simulated camera only.
> - The firmware **builds clean for `esp32` with ESP-IDF v5.5.5 and
>   esp32-camera 2.1.8** (zero warnings in this project), and
>   `esp_wifi_init` / `esp_bt_controller_init` are absent from the ELF.

---

## Why colour blobs, not a neural network

| Option | Verdict |
|---|---|
| **Colour blob (this)** | A few ms per QQVGA frame, deterministic, and every failure can be explained with the `m` mask dump. Works on any target you can paint. |
| ESP-DL person/face models | Needs PSRAM and runs at ~1–3 FPS on the original ESP32, near full CPU, so the board runs hot. And it only knows the classes it was trained on. |
| Stream JPEG to a laptop, run YOLO | The most capable option. But WiFi is the largest heat source on this board (see optflow_cam), it adds latency, and the airframe depends on a link. |

Hue is the key choice. A red balloon in shadow is *darker*, not *less red*. So
testing hue, with floors on saturation and brightness, survives lighting
changes that would break a plain RGB threshold.

## Pipeline

```
OV2640 ──RGB565 QQVGA──> frame buffer (DRAM)
                             │
   per pixel: hue within ±tol, sat ≥ S, val ≥ V      (no divide until both floors pass)
                             │
   pool into 2x2 cells: a cell is set when ≥ half its pixels match   → speckle gone
                             │
   connected cells, 4-connectivity, union-find (memory bounded for ANY input)
                             │
   4 largest blobs → exact centroid and box from their matching PIXELS
                     (own cells, plus edge cells that touch them)
                             │
   az/el from pinhole model, range = 550 mm · f / larger box side
                             │
                    CSV line on UART0 @ 115200
```

### The edge-pixel fix (found by the host suite)

The first version measured the centroid only from the blob's own cells. Along
a curved edge many 2×2 cells hold just one matching pixel, so they fall below
the majority threshold. Dropping them lost **3% of the area** and biased
sub-pixel centroids by up to **0.07 px**. The final pass now also takes the
matching pixels from below-threshold cells that touch the blob. On the same
frames, centroid error is **0.000 px** and area is exact. Speckle away from the
blob is still excluded.

## Colour presets

| Preset | Hue window | Sat ≥ | Val ≥ | Notes |
|---|---|---|---|---|
| **red** (default) | 348°–12° | 110 | 50 | Competition waypoints. Pink walls and skin fall below the saturation floor. |
| blue | 190°–250° | 90 | 40 | |
| green | 90°–160° | 80 | 40 | |
| orange | 15°–35° | 120 | 60 | **Wood and brown furniture also pass.** Test orange against a plain background. |

Red and orange are only 25° apart, so their windows are narrow with a gap
between them. Host test 14 checks that each preset finds only its own balloon.
Select a preset at build time (`menuconfig` → Target colour preset) or at run
time with `p red`, `p blue`, `p green` or `p orange`. `c` (calibrate on the
centre of the view) replaces the preset with measured thresholds.

## Geometry (QQVGA, stock 65° diagonal lens → 54° h-FOV)

f = 80 / tan(27°) = **157 px**, so **0.365° per pixel** at the centre.

For a 550 mm waypoint balloon, from the host suite's shaded mylar model:

| Range | Radius | Detected | Bearing bias (toward the lit side) | Box range reads |
|---|---|---|---|---|
| 1 m | 43 px | yes | 5.3 px (1.9°) | 1.08 m |
| 2 m | 22 px | yes | 2.7 px (1.0°) | 2.11 m |
| 4 m | 11 px | yes | 1.4 px (0.5°) | 4.32 m |
| 6 m | 7 px | yes | 0.9 px | 6.64 m |
| 10 m | 4 px | yes | 0.5 px | 10.79 m |
| 12 m | 4 px | yes, 36 px area | 0.5 px | 12.34 m |

The arena is 10 m × 5 m, so any balloon in it is inside detection range.

**Range is taken from the box's larger side, not the area.** On mylar, the
white specular highlight and the dark far side both fail the colour test. The
area therefore undercounts the balloon, and range read 10–13% long. The box
spans the highlight hole and reads 5–11% long. The remaining bias is
systematic, so calibrate `d<mm>` against a real balloon at a measured distance.
The bearing bias is about 12% of the radius toward the light; it shrinks as the
balloon gets closer to the centre of view. **Clipped targets report
`range_mm = -1`**, because a target cut off by the frame edge looks smaller,
which would make the range read long.

## Building and flashing

Same toolchain as optflow_cam:

```bash
source $HOME/.espressif/tools/activate_idf_v5.5.5.sh
idf.py -C firmware/projects/target_cam set-target esp32
idf.py -C firmware/projects/target_cam build
```

**Flashing through the ESP32-CAM-MB** (the bench programmer, `/dev/ttyACM0`)
is what worked on 2026-10-13. `idf.py flash` and auto-reset did not:

1. Hold the board's **BOOT** button, unplug and replug the USB cable, and keep
   holding BOOT.
2. From `target_cam/build`:
   ```bash
   $HOME/.espressif/tools/python/v5.5.5/venv/bin/python -m esptool --chip esp32 \
       -p /dev/ttyACM0 -b 115200 --before no_reset --after no_reset write_flash @flash_args
   ```
   Stay at 115200: the bridge wedged once after a 460800 flash.
3. Release BOOT and replug to run the new firmware.

The 5-second reflash window after boot is there because GPIO0 is XCLK on the
AI-Thinker board. Once the camera starts, esptool can no longer force download
mode. Power the board from 5 V.

Opening the port with DTR or RTS asserted resets the board, and terminal
programs and pyserial do that by default. The viewer lowers both lines before
it opens the port, so it does not reset the board. `target_monitor.py` does
not lower them, so it resets the board on connect (harmless; you see the boot
log).

**Settings in flash.** A boot starts from the Kconfig defaults and then loads
any settings saved with `!save` (NVS namespace `targetcam`). The banner's
`settings` line says which. `!reset` erases them. A reflash does not erase them
unless you erase the whole flash. A record saved by the 10 FPS firmware (record
v1) is still loaded, calibration included, but its frame rate is replaced by the
build default of 80 ms, and the banner says so. A v2 record (before the
learned detector) loads with stage 0 and `cnn_k` 2, and a v3 record (before
`!flip`) loads with the image not turned. A v5 record loads as it is: its
spare byte, zero, is v6's `thr2`, and 0 means the model's own classifier
threshold. A v5 or v6 record is the first 32 B of a v7 one and gains `thr3`
0, the CNN's own threshold. `!save` writes the current version, v7.

For a camera mounted upside down, set `TARGETCAM_VFLIP` and `TARGETCAM_HMIRROR`
in `menuconfig`. That keeps +az as "right of the nose" and +el as "above it".
A single board whose image arrives upside down can be turned at run time
instead, without a rebuild: `!flip 1`, then `!save` (the viewer's **Rotate
180°** button). The sensor turns the image on top of the build's two flags, so
the detector, az/el, `g` frames and blob crops all see it turned.

## PC viewer: see the image and calibrate by hand

[`host/viewer/`](host/viewer/README.md) (`python -m tcview --port
/dev/ttyACM0`) has two screens:

- **Image & calibration:** the camera's raw pixels, a mask overlay, sliders,
  pixel and region colour readouts, and save-to-flash.
- **Flight data:** only the CSV the flight board will receive, as a dashboard.

Its overlay is computed by this project's own `blob_detect.c`. The setup, the
two screens and the `#!` protocol are described there. The checklist below
predates it and still works from a plain terminal, but the viewer replaces
steps 2–4 with a live image.

## Retraining the colour table (stage 1)

Retrain after adding a labelled session, and at a new venue or under new
light. The colour table learns the camera's raw colours, so it belongs to the
light and white balance it was trained under. The student guide (capture and
labelling) covers the same routine with screenshots. The method and its scores
are in `docs/learned-detector-report.md` §8 and §9.

**Safety net first.** Stage 0 needs no table. If a table misbehaves in a test,
switch the camera to stage 0 (`!stage 0`, or *Detector stage* in tcview's
Calib tab), calibrate the HSV window as before and fly that. Retraining can
wait.

Run everything from `target_cam/` with `conda activate targetcam`.

1. **Collect and label** each session in its own folder under `data/`
   (`data/red_s3`, `data/venue_s1`, ...), with fixed white-balance gains
   (`Wc` then `!save`, see [White balance](#white-balance)) as in flight.
   Leave out folders whose balloons are not targets, such as `orange_s1`.
2. **Score the current table on the new session**, before it is trained on,
   and keep the output. It is the only score from frames the table has never
   seen:
   ```bash
   python host/tools/eval_frames.py --stage 1 data/red_s3 | tee data/red_s3/eval_before.txt
   ```
3. **Train a trial table.** `--out` writes it to a trial folder and leaves the
   firmware's table alone. It takes about 10 s for 20 frames:
   ```bash
   python host/train/train_lut.py --out data/lut_try data/red_s1 data/red_s2 data/red_s3
   ```
   The trainer chooses sigma, k and min_evidence itself by leaving one session
   out at a time. With only one session it cannot, so give it the current
   values: `--sigma 8 --k 7`.
4. **Judge it.** Use the trial table only if all of these hold:
   - no `WARNING` and no `EXCLUDED` on the first (chosen) setting;
   - under that setting, every `held out` line shows `false b1 0/…` and no
     more `FN` than the old table had on that session (step 2 for the new
     one; report §8.3 for `red_s1` and `red_s2`);
   - on all sessions, every balloon is found and no empty frame has a false
     blob 1:
     ```bash
     python host/tools/eval_frames.py --stage 1 --lut data/lut_try/lut_model.bin data/red_s1 data/red_s2 data/red_s3
     ```
     (These are its own training frames, so its pixel figures look better
     than they will in flight. They are a check for gross failure, not a
     score.)

   If one figure improves and another gets worse, keep the old table.
5. **Install it.** Back up the current table, copy the trial in, then rebuild
   and flash as in [Building and flashing](#building-and-flashing):
   ```bash
   mkdir -p data/lut_before && cp main/models/lut_model.* data/lut_before/
   cp data/lut_try/lut_model.* main/models/
   ```
6. **Confirm on the board.**
   - The boot log reads `# models        colour table ok: id <new id>, …`,
     and `#!CFG` shows `lut=ok lut_id=<new id>`.
   - tcview's stage chip is green, not `MISMATCH`.
   - `csv_report.py --cmd '!stage 1'` shows 0 rows off 80 ms and the balloon
     found steadily.
7. **Keep it reproducible** before committing. Copy each session it was
   trained on into `host/testdata/` and run the viewer tests. They rebuild the
   committed table from those frames and must get the same bytes:
   ```bash
   cp -r data/red_s3 host/testdata/
   (cd host/viewer && python -m pytest -q tests)
   ```

**Undo.** Copy back the backup (`cp data/lut_before/lut_model.* main/models/`),
or `git checkout main/models/` for the last committed table, then rebuild and
flash.

**At a venue** *(reasoned, not yet tried)*: train on the venue sessions plus
the older ones. If the venue's `held out` line shows false blobs, train on the
venue sessions alone and compare that line again.

| Symptom | Cause | What to do |
|---|---|---|
| `no labelled frames in DIR` | Wrong folder, or its frames have no `.labels.png` | Check the path; label the frames in the Label tab |
| `choosing sigma and k needs two or more sessions` | Only one folder given | Add `--sigma 8 --k 7`, or add another session |
| `WARNING: every setting puts a false blob 1 …` | Some empty frame always gets a false target | Keep the old table. Run `eval_frames.py --stage 1 --lut …` to find the frame (`blob 1 false`), and check that its balloon really is out of view and its labels are right |
| `eval_frames.py` prints `ran 0`, or `cannot use …` | The table file was not found or is damaged | Check the `--lut` path; rerun the trainer |
| Boot log `colour table REFUSED`, `#!CFG` `lut=bad` | The table built into the firmware is damaged; the camera runs stage 0 instead | Rerun the trainer, copy, rebuild, flash |
| tcview chip `Colour table MISMATCH` | The camera and `main/models/` hold different tables | Flash the current build, or copy the right table into `main/models/` |
| `test_trainer_reproduces_the_committed_table` fails: `not in host/testdata/` | The table was trained on a session that is not committed | Step 7 |
| The same test fails: `does not hold the frames` | Frames were added, removed or relabelled after training | Copy the session again, or retrain from `host/testdata/` |
| Balloon found in the frames but not in the hall | Different light or white balance from training | Collect a session there, retrain; meanwhile fly stage 0 |

## Training the blob classifier (stage 2)

Stage 2 drops the blobs that passed the colour table but do not look like a
balloon. Each reported blob gets a confidence from its 12 features
(`main/blob_feat.c`: shape, surface, context) by logistic regression
(`main/blob_clf.h`); blobs below the model's threshold are removed and the
rest move up, so blob 1 is the largest blob the classifier believes. The
method and its first scores are in `docs/learned-detector-report.md` §11.

**A classifier belongs to one colour table.** The features are computed on
the pixels the table accepts, so its record carries the table's train id.
The camera uses it only behind that table: otherwise the boot log says so and
`#!CFG` reads `clf=mismatch` (stage 2 then runs as 1). Retraining the table
means retraining the classifier.

1. **Label sessions with false blobs.** The classifier learns from what the
   table reports: every blob whose centroid is in a painted balloon is a
   balloon, every other one is not. Sessions with no false blobs teach it
   nothing about them. Orange and pink balloons painted as background, doors,
   lamps and windows are what it needs.
2. **Train it behind the table**, two or more sessions so it can choose the
   L2 penalty and the threshold by leaving one session out:
   ```bash
   python host/train/train_clf.py --lut models/T/lut_model.bin --out models/T data/train/red_s1 data/train/orange_s1 ...
   ```
   It prints, for the same held-out frames, stage 1 alone and the chosen
   setting, as a cost: false blob 1 on empty frames + frames whose blob 1 is
   not a balloon + balloons lost. A setting that loses more than `--max-lost`
   (10 %) of the balloons stage 1 found is marked `EXCLUDED`, so "drop
   everything" can never win. If no fold can learn (a held-out session
   leaves only one kind of blob behind), it says so; give `--l2` and
   `--threshold` instead.

   A session can also be a **blob set** (`.npz`), collected live with
   `!collect 1` and labelled in tcview's Blobs tab; folders and sets can be
   mixed. Collect it with this table flashed and `!stage 1`: tcview records the
   camera's `lut_id` with every blob, and the trainer refuses a set found
   behind another table, and one found at stage 0 (HSV finds other blobs)
   unless `--allow-stage0`. A set from before tcview recorded `lut_id` is
   used with a `DATA WARNING`, as are sessions with different white-balance
   gains. A set holds only the balloons the table found, so its FN counts
   balloons the classifier drops, not ones the table missed: judge the
   result on frame folders (step 3).
3. **Judge it** like a table, at stage 2, on sessions it did not see:
   ```bash
   python host/tools/eval_frames.py --stage 2 --lut models/T/lut_model.bin --clf models/T/clf_model.bin data/test/...
   ```
   Use it only if it removes false blob 1s without losing balloons the table
   alone found. A `WARNING` from the trainer means it did not on the held-out
   sessions.
4. **Install** `clf_model.{bin,h,json}` with its table, rebuild, flash, and send `!stage 2` then `!save`. The boot
   log reads `# models        blob classifier ok: id …, threshold …, behind
   table …`. `main/models/clf_model.h` is a placeholder (`CLF_MODEL_NONE`) while
   no classifier is installed.

## Training the crop CNN (stage 3)

Stage 3 looks at the crop of each of the `cnn_k` largest blobs stage 2 kept
(`!cnnk`, default 2) with a small int8 network (`main/blob_cnn.h`: three 3×3
convolutions, 8–16–16 channels, global average, one logistic unit; 16×16 input
by default, about 166 k multiply-adds and 3.7 k weights) and drops the ones it
does not believe. The ones past `cnn_k` keep stage 2's verdict; blob 1, the one
the blimp steers to, is always checked. Like the classifier, a CNN belongs to
one colour table, and the camera uses it only behind that table
(`cnn=mismatch` otherwise). Stage 3 needs the classifier too.

**Measure first, on the board** (phase C0): `!cnnbench` (or `!cnnbench 24`)
runs the network on a synthetic crop 20 times and prints
`#!CNNBENCH ... us_per_crop=... cycles_per_mac=...`. The feasibility report's
4–10 cycles per multiply-add is an estimate; this is the measured figure that
decides the input size and `cnn_k` against the 80 ms period. Without a CNN
built in it times a zero-weight network of the default shape, which costs the
same.

`host/train/train_cnn.py` (or tcview's Train tab, stage 3) trains it from frame
folders and blob sets of the table, in float with numpy alone, then quantises
it (int8 weights, uint8 activations, integer requantisation) and checks that
the C network gives the same integer sums as its numpy copy. Training data is
every blob the table finds, not only the ones the classifier keeps; the
threshold is chosen by leaving one session out, running each held-out frame
as stage 3 runs in flight (classifier, then the CNN on the `cnn_k` largest):
```bash
python host/train/train_cnn.py --lut models/S/lut_model.bin --clf models/S/clf_model.bin --out models/S data/train/s1 data/train/s3
```
It takes minutes, not seconds (one network per held-out session, plus the
final one). Install the set with tcview's Firmware tab, then `!stage 3` and
`!save`; `!thr3 <0-99>` moves the threshold without retraining.

## First power-up checklist

1. Run `host/target_monitor.py --port /dev/ttyACM0`. Check that rows arrive and
   that `dt_ms` is ~80 ms (12.5 FPS target).
2. Hold the target so it fills the centre of the view, then send **`c`**. The
   firmware samples the central 16×16 patch and sets hue, tolerance and floors
   from it.
   - It **refuses** if saturation is below 70: the target is too dull to tell
     apart from walls.
   - It **warns** if the hue spread is over 20°: the patch is not one colour.
3. Send **`m`** to get the mask as ASCII art (`#` matches, `+` partial, `X`
   centroid). Check that the target is solid and the background is empty.
4. If **nothing** matches even right after a good `c`, send **`o`** to flip the
   RGB565 byte order. The source says big-endian. This check confirms it.
5. Move the target across the view and check `az_deg` against a protractor or
   a measured offset at a known distance. That calibrates `TARGETCAM_HFOV_DEG`,
   which scales every angle and every range.
6. Set fixed white-balance gains from a white card (**`Wc`**, see
   [White balance](#white-balance)) and `!save`. Auto white balance tries to
   neutralise a dominant colour, which pulls the target toward grey, and its
   gains are never known.

## Runtime commands

Commands are case-sensitive: `S` is saturation, `s` is the stream.

| Command | Effect |
|---|---|
| `p<name>` | Colour preset: `p red`, `p blue`, `p green`, `p orange` (or `p0`–`p3`, or the first letter) |
| `c` | Calibrate colour from the centre 16×16 patch |
| `H<deg>` | Target hue: 0 red, 30 orange, 60 yellow, 120 green, 210 blue, 300 magenta |
| `T<deg>` | Hue tolerance ±, 1–90 |
| `S<0-255>` | Minimum saturation |
| `V<0-255>` | Minimum brightness |
| `a<px>` | Smallest blob reported |
| `d<mm>` | Real target diameter for range (default 550); `d0` turns range off |
| `f<fps>` | Frame rate, 1–30, decimals allowed (`f12.5`). The sensor is the clock, so this picks every n-th sensor frame: `f12.5` and `f15` give every frame (79.968 ms at XCLK 20 MHz), `f6.25` every second one (159.9 ms), `f10` rounds back to 12.5. The reply and `#!CFG` give what was really set. |
| `w<0\|1\|2>` | White balance: `w0` frozen (auto stopped where it is), `w1` auto, `w2` fixed (the gains below). Only fixed gains are known and recorded; see [White balance](#white-balance). |
| `W<r>,<g>,<b>` | Load fixed R, G, B gains, 1–255 (64 = 1.0), and switch to `w2` |
| `Wc` | Fixed gains from a white card over the centre 16×16 patch: R and B are adjusted until it reads grey (up to 16 steps, ~1–6 s), then `#!WBCAL ok\|refused`. A refusal puts the previous mode and gains back. |
| `W` | Read the sensor's white-balance registers back (`#!WB`) |
| `F<deg>` | Horizontal FOV, 10–150 (decimals allowed), for angles and range |
| `o` | Flip RGB565 byte order |
| `m` | Dump the detection mask once |
| `s` | Toggle the CSV stream |
| `?` | Banner and current settings |
| `k` | Print the `#!CFG` line (the live settings, machine-readable) |
| `g` | Send one raw RGB565 frame (`#!FRAME`…`#!END`, see the viewer README) |
| `v<0\|1>` | Send a frame every loop / stop. **Timing is not flight-representative while on.** |
| `B<baud>` | Change speed: 115200, 230400, 460800, 921600, 1500000, 2000000. Reverts to 115200 if no valid command arrives at the new speed within 3 s. Every boot starts at 115200. |
| `!save` | Write the live settings to flash. The camera boots with them. |
| `!load` | Reload the settings saved in flash |
| `!reset` | Erase the saved settings and go back to the Kconfig defaults |
| `!stage <0-3>` | Detector level: 0 HSV, 1 learned colour table, 2 + blob classifier, 3 + CNN. A level whose model is missing or invalid is accepted but **falls back** to the highest complete one; `#!CFG` (`stage_eff`) and every row (`stage`) say what really runs. Live only; `!save` keeps it. |
| `!flip <0\|1>` | Turn the image 180° in the sensor (vertical flip and mirror together), on top of `TARGETCAM_VFLIP`/`HMIRROR`. For a board whose image arrives upside down. Live; `!save` keeps it. |
| `!thr3 <0-99>` | Stage 3's threshold, the CNN's, as `!thr2` is the classifier's. Live; `!save` keeps it. |
| `!cnnk <1-4>` | How many of the blobs stage 2 kept the CNN checks, largest first. Each costs a crop (`!cnnbench`); the rest keep stage 2's verdict. Live; `!save` keeps it. |
| `!cnnbench [16\|24]` | Time the CNN on the board (phase C0): `#!CNNBENCH n= macs= us_per_crop= cycles_per_mac=`. Stalls one loop by about 20 crops' time. |
| `!thr2 <0-99>` | Stage 2's threshold in hundredths: a blob is reported when the classifier's confidence reaches it. `0` = the model's own threshold. Higher drops more false blobs and more balloons, a trade that depends on the venue, which is why it is a setting and not only a training choice. Accepted with no classifier too (kept for the next one). Live; `!save` keeps it. |
| `!collect <0\|1>` | Training data for the blob classifier: every 4th frame and every sent frame (`g`, `v1`), one `#!BLOB` record (features + 24×24 crop) per reported blob, after the row. **Timing is not flight-representative while on.** Live only, never saved. See [Blob records](#blob-records-collect). |

Every command line is answered with `#!ACK <cmd>` or `#!NAK <cmd> -- <reason>`.
A setting change is followed by `#!CFG`. The commands that touch flash are
whole words, so one garbled byte (on the bench, `m` once arrived as `W`) can
never write to flash. `!stage` and `!collect` are whole words for the same
reason, although they write nothing: one corrupted byte must not change the
detector level in flight, or start a transfer that stalls the loop. `!flip` is
one too: a turned image swaps the signs of az and el. So is `!thr2`: it decides
which blobs the blimp may steer to.

From stage 1 up, the learned colour table decides which pixels match. The HSV
settings (`H T S V`, `c`, presets) are kept and still reported, but they have
no effect on detection until `!stage 0`, and `m` draws the running stage's
mask. The table is compiled in (`main/models/lut_model.h`) and checked at boot;
the boot log prints its id and how many colours it accepts, or why it was
refused.

The letters that take no argument (`c o m s k g ?`) must arrive alone on their
line; `s=1` or `k foo` is NAKed, not obeyed. On the bench, opening the port
made the tty echo camera output back, and a fragment of a `#!CFG` line could
otherwise have recalibrated the colour or stopped the stream.

`period_ms` in `#!CFG` has one decimal, like `dt_ms`, and `sensor_us` is the
sensor's frame period as the driver's timestamps measure it. `#!CFG` ends with
the learned-detector state: `stage=` (requested) `stage_eff=`
(running) `lut= clf= cnn=` (`ok`, `none` or `bad` for each model; `clf` can
also be `mismatch`, a classifier trained behind another colour table) `lut_id=`
(the colour table's train id, 0 without one) `cnn_k=`
`csv=2` (the row format) `fw=` (the app version) `collect=` (`!collect`, 0 or 1)
`fv=` (the blob feature definition, `BLOB_FEAT_VERSION`) `flip=` (`!flip`, 0 or 1)
`clf_id=` (the classifier's train id, 0 without one) `thr2=` (`!thr2`, 0 = the
model's) `clf_thr=` (the threshold stage 2 runs with, −1 with no classifier)
`cnn_id=` `thr3=` `cnn_thr=` (the same three for the CNN; `cnn=` can also be
`mismatch`).
After `awb=` (0, 1 or 2) come `wb=` (`frozen`, `auto` or `fixed`) `wb_gain=`
(the R,G,B gains in use, `0,0,0` when they are not known: in auto and frozen)
and `wb_fixed=` (the gains `w2` would use, kept while in auto).

### White balance

The OV2640's auto white balance re-solves its R, G, B gains from whatever is
in view, so one red balloon read about 25° apart in hue between two rooms
(`docs/learned-detector-report.md` §10.7). `w0` stops it, but at gains nobody
knows. **Fixed gains** (`w2`) are set once, from a white card (`Wc`) in the
flying light, saved with `!save`, and reported in `#!CFG`, in every `#!BLOB`
record, in the viewer's saved frames and blob sets, and in its flight CSVs.
Colour is then comparable between sessions with the same gains. The scale
(64 = 1.0) is inferred from the driver's presets, not documented
(report §10.8).

To calibrate: in the flying light, hold a matt white card (plain printer
paper) so that it fills the dashed centre square, not in shadow and not
shining. Send `Wc` (*From white card* in tcview). Keep it still until
`#!WBCAL ok`, then `!save`. A refusal says why: `dark` (fill and light the
patch), `clipped` (overexposed), `range` (a gain beyond what the register gives: is the card
white?), `converge` (it did not settle within 16 steps).

## Output format

CSV version 2, 35 columns:

```
t_ms,ok,cx_px,cy_px,az_deg,el_deg,area_px,w_px,h_px,fill,range_mm,clipped,blobs,matched_px,b2_az,b2_el,b2_range_mm,b3_az,b3_el,b3_range_mm,b4_az,b4_el,b4_range_mm,dt_ms,proc_us,stage,conf,b2_conf,b3_conf,b4_conf,t_px_us,t_b_us,t_c_us,loop_us,csv_v
```

The first 25 columns are version 1 unchanged, at the same indices, so a reader
of v1 fields keeps working. A reader tells the versions apart by the last
column (`csv_v`), never by guessing: a 35-column row whose `csv_v` is not 2 is
refused. Readers that know both: `tcview` and `host/target_monitor.py`. The
S3 reader is to be written against v2.

| Field | Meaning |
|---|---|
| `ok` | **0 means no target, not a target dead ahead.** All position fields are 0 and must be ignored. |
| `cx_px` … `clipped` | The **largest** blob, usually the nearest balloon. |
| `cx_px`, `cy_px` | Centroid. Pixel *i* spans [*i*, *i*+1), so the frame centre is exactly (80, 60). |
| `az_deg`, `el_deg` | Bearing from the optical axis. +az is right, +el is up. **Body frame**: no IMU here, so roll and pitch tilt these. |
| `area_px` | Matching pixels in the blob. |
| `w_px`, `h_px`, `fill` | Bounding box and area/box. A full disc gives ~0.79; mylar reads lower because of the highlight hole. Much lower means an irregular or partly hidden target. |
| `range_mm` | `target_mm · f / max(w, h)`. **−1** when off (`d0`) or when clipped. |
| `clipped` | The box touches the frame edge, so part of the target is out of view. |
| `blobs` | Blobs at or above `min_area` in the frame. Can exceed 4. At stage 2, minus the ones the classifier rejected. |
| `matched_px` | Matching pixels in the whole frame. Rising with no blob means scattered colour clutter: tighten `S` or `T`. |
| `bN_az`, `bN_el`, `bN_range_mm` | Blobs 2–4, in decreasing size. Absent ones read `0,0,-1`. **Choosing which one is the next waypoint is the navigator's job.** The largest is the nearest, which is not always the next in the course sequence. |
| `dt_ms` | Measured interval between captures. The loop takes each frame as the sensor completes it, so this is a whole number of sensor frames: 79.968 ms, printed `80.0`, at XCLK 20 MHz. A capture off that period also prints a `# late capture` comment saying why. |
| `proc_us` | Detector cost, all stages. Labelling time is `proc_us − t_px_us − t_b_us − t_c_us`. |
| `stage` | The level that **ran** for this row (0–3), after any fallback. |
| `conf`, `bN_conf` | Confidence 0–1 of blob 1 and blobs 2–4. **−1 = not computed** (stage < 2) or no blob in that slot. At stage 2 every reported blob has `conf` ≥ the classifier's threshold: the others were dropped and the rest moved up in size order, so `ok=0` can mean "only rejected blobs in view". |
| `t_px_us` | Pixel-classification pass (HSV at stage 0, the colour table at ≥ 1). |
| `t_b_us`, `t_c_us` | Blob features + classifier, and crops + CNN. 0 when the stage did not run. Below stage 2, `t_b_us` is the features alone when they are asked for (`!collect 1`, or `!stage 2` falling back with no classifier), so their cost can be measured before a classifier exists. |
| `loop_us` | CPU busy time of the **previous** loop, from the frame's arrival to after its row was handed to the UART and any command was answered. This is the CPU figure to hold against the 80 ms period. It does **not** include the row's time on the wire: printf returns once the row is in the UART's 128 B hardware FIFO, and a 129 B row then takes another ~11 ms to go out. That is a separate budget, which `csv_report.py` reports as `wire`. |
| `csv_v` | Row format version, 2. |

`host/tools/csv_report.py` checks a recording: rows/s, every row whose `dt_ms`
is not 80 (exit status 1 if there is one), rows lost on the wire, p50/p99/max
of every timing column, the ok-rate and the "jumps" of blob 1. It reads a file
or records from `--port` without resetting the camera.

### Blob records (`!collect`)

Stage 2 is a classifier that looks at each reported blob and decides
"balloon or not", because the false blobs that pass the colour table (a red
door, a chair, a shirt) differ from a mylar balloon in shape and surface, not
in colour (see [Training the blob classifier](#training-the-blob-classifier-stage-2)).
These records are data for it. **Collect at stage 1**: at stage 2 the records
cover only the blobs the classifier kept, so the ones it rejects, which are
what it must keep learning from, would be missing. With
`!collect 1`, on every 4th frame and on every frame it sends to the PC
(`g`, `v1`), the camera sends, after that frame's row, one packet per
reported blob:

```
#!BLOB seq=<n> t_ms=<ms> k=<0-3> n=<blobs> stage=<ran> csv=2 fv=1 fw=<version>
       c=<cx>,<cy> area=<px> box=<x0>,<y0>,<x1>,<y1> crop=<xs>,<ys>,<side>
       conf=<-1|0-1> feat=<12 values, %.6g> wb=<auto|frozen|fixed> wb_gain=<r>,<g>,<b>
       cw=24 fmt=rgb565le len=1152 crc=<hex8>\r\n
<1152 bytes: the 24x24 crop, RGB565 little-endian>
\r\n#!END seq=<n>\r\n
```

(one header line on the wire; wrapped here). `t_ms` and `k` tie the record to
blob *k* of the row with the same `t_ms`, and to the frame of that capture if
one was sent (`g`, `v1`). The framing, CRC and sequence counter are the
frame packet's (`main/link.h`), so a damaged record is dropped, never misread.

The 12 features (`main/blob_feat.c` gives the reason for each): `fill`,
`log_aspect`, `hue_spread`, `sat_mean`, `sat_std`, `val_mean`, `highlight`,
`edge`, `texture`, `ring`, `cy_norm`, `log_area`. They are computed by
portable C that the PC runs too, so the classifier is trained on the numbers
the camera will compute in flight. Their cost is bounded by the blob, not the
frame: a large blob is sampled on a grid of at most 1024 points. The crop is
the square 1.5× the blob's longer side (at least 12 px), centred on the
centroid and area-averaged down to 24×24, black where it leaves the frame. It
is for the person labelling and for the stage 3 CNN.

Records cost about 1.44 kB each on the wire (125 ms at 115200), which is why
collection takes every 4th frame and is a bench mode. A sent frame always
carries its records, because a record can only be checked against the pixels
it came from, and beside a 38 kB frame they cost little. 115200 is enough for
collecting: the faster speeds fail on the ESP32-CAM-MB.
The viewer's **Blobs** tab collects and labels them, and re-checks them
against the PC's own computation (see the viewer README).
`host/tools/blobs_from_frames.py` makes the same kind of dataset from saved,
hand-labelled frames.

## Verification

```bash
cd firmware/projects/target_cam/host && make test
```

This builds with AddressSanitizer and UBSan and runs synthetic RGB565 frames
(big-endian, like the camera). The background is grey value noise with colour
noise, not a blank frame. Result:

```
 1. red disc at 6 sub-pixel positions       centroid error 0.000 px, area exact
 2. red wrapping through 0/360              hue 350 and 12 both accepted
 3. no target (grey + a green disc)         found=0, 0 pixels matched
 4. two red discs                           larger first, runner-up second
 5. desaturated pink, near-black red        rejected
 6. 300 isolated red pixels                 classified, but pooled away: found=0
 7. byte order swapped                      not detected; correct order detected
 8. disc off the right edge                 clipped=1
 9. full-frame checkerboard, cells 1/2/4    9600/2400/600 blobs, no overrun
10. calibration on 5 colours                hue within 1 deg, spread <= 3 deg
11. config validation                       bad cell/hue/tol/area rejected
12. red mylar balloon 1-12 m, red preset,   found at every range; skin, wood,
    with skin, wood and a pink wall         pink wall never detected; centroid
                                            within 13.4% of r; box range +5..11%
13. four red balloons at 2.5/4.5/6.5/9 m    all four reported, nearest first
14. each preset vs. 4 coloured balloons     each finds only its own balloon
15. blob_mask vs. blob_pixel_matches        0 disagreements, both byte orders
PASSED (0 failures)
```

`make test` then runs `test_pipeline`: the CRC-32 that model records will
carry, against zlib's published values; the stage fallback rule over every
combination of present models; stage 0 through the pipeline equal to
`blob_detect()` bit for bit on 40 scenes; and the per-stage timing with an
injected clock. For stage 1 it checks that a table built from an HSV setting
gives stage 0's result and mask bit for bit, and that a damaged model record
is refused with the right reason and leaves the caller's table untouched.
For stage 2 it checks that a classifier record is refused for damage, for
another feature count or version and for values it cannot fly (a threshold
outside 0–1, a non-finite weight); that the confidence is the logistic
formula; and that a classifier that believes every blob changes nothing,
one that believes none reports no target, and one in between drops exactly
the blobs below threshold and moves the rest up in order. The viewer's
`test_cnn.py` covers stage 3: the trainer's gradients against finite
differences, the integer network equal to `blob_cnn.c` bit for bit at 16 and
24, the record checks, and stage 3 through the pipeline (a CNN that believes
nothing drops exactly the `cnn_k` largest blobs; the rest keep stage 2's
verdict).

`make test` last runs `test_blob_feat`: the features of shapes with known
answers (a disc's fill of π/4, a rectangle's aspect ln(w/h), a highlight's
exact fraction, a hue spread across 0/360, the ring band, the sampling stride
rule), byte-order independence, and the crop rule (box, flat colour stays
flat, black outside the frame). The viewer's tests add a numpy re-implementation
of the crop that the C must match bit for bit.

Test 9 matters most for robustness. A checkerboard is the worst case for
provisional labels. The label arrays are sized for it, so no input can overrun
them, and ASan confirms it.

Tests 1–11 use hard-edged discs. Tests 12–14 use a shaded sphere: lit from
the upper left, a dark far side at a quarter of the brightness, and a white
specular highlight. This approximates a mylar balloon, but it is a model.
**Real mylar also mirrors the room, and how the OV2640 renders that red is
unmeasured.** The first bench test should be a real waypoint balloon at
measured distances (see the checklist).

## Files

| File | Purpose |
|---|---|
| `main/blob_detect.h/.c` | The detector. Portable C, no ESP-IDF and no heap; the caller provides a workspace (28 KB at 2×2 cells). |
| `main/tc_pipeline.h/.c` | The staged detector entry point the camera and the viewer both call: stage fallback, per-stage timing, `tc_crc32()`, the model record check. Portable C. |
| `main/blob_feat.h/.c` | Blob features and 24×24 crops for the classifier (Phase B). Portable C, integer per-pixel arithmetic so the PC reproduces it. |
| `main/color_lut.h/.c` | Stage 1: the learned colour table (one bit per RGB565 value, 8 kB), its record loader and the HSV-equivalent table the tests use. Portable C. |
| `main/blob_clf.h/.c` | Stage 2: the blob classifier (logistic regression on the 12 features, 160 B), its record loader and the confidence. Portable C. |
| `main/models/lut_model.{h,bin,json}` | The trained colour table: the C array the firmware compiles in, the same bytes for the PC tools, and how it was made. Generated by `host/train/train_lut.py`; do not edit. |
| `main/blob_cnn.h/.c` | Stage 3: the crop CNN (int8, integer to the logit), its record loader, the confidence and its multiply-add count. Portable C, static activations (~15 kB). |
| `main/models/cnn_model.{h,bin,json}` | The crop CNN, the same way (`host/train/train_cnn.py`); `cnn_model.h` alone, a placeholder, when none is installed. |
| `main/models/clf_model.{h,bin,json}` | The blob classifier, the same way (`host/train/train_clf.py`). With none installed, `clf_model.h` alone, a placeholder. |
| `main/target_main.c` | Camera setup, pacing, calibration, mask dump, bearing/range, CSV, commands. |
| `main/link.h/.c` | Binary packets (`#!FRAME`, `#!BLOB`; CRC-32, one sequence counter) and run-time baud changes with the 3 s safety revert. |
| `main/settings.h/.c` | Versioned settings record in NVS (`!save` / `!load` / `!reset`). |
| `main/camera_pins.h` | Pin maps: AI-Thinker, ESP-EYE, XIAO S3 (shared with optflow_cam). |
| `main/Kconfig.projbuild` | Board, frame period, XCLK, FOV, flips, default colour, area, target size. |
| `sdkconfig.defaults` | 240 MHz, 1 kHz tick, PSRAM optional, brownout on. |
| `host/test_blob_detect.c` | The verification suite above. |
| `host/test_pipeline.c` | The pipeline suite (CRC, fallback, stage 0 equality, timing, the features flag). |
| `host/test_blob_feat.c` | The blob feature and crop suite. |
| `host/tools/csv_report.py` | Pacing, timing and scene report of a CSV recording or a live port. |
| `host/tools/eval_frames.py` | Scores the camera's pipeline on hand-labelled saved frames: blob TP/FN/FP/split, blob 1, pixel precision/recall. `--stage 1` uses the committed table, `--lut` another. |
| `host/tools/blobs_from_frames.py` | A blob dataset (`.npz`: features, crops, labels) from hand-labelled saved frames, labelled from the paint. Same format as the viewer's Blobs tab. |
| `host/train/train_lut.py` | Trains the colour table from labelled sessions, choosing its parameters by leave-one-session-out, and writes `main/models/lut_model.*`. Runs in the viewer's `targetcam` env. |
| `host/train/train_cnn.py` | Trains the stage 3 CNN behind a table and its classifier with numpy alone, quantises it, checks the C network against its integer copy, and chooses the threshold by leave-one-session-out as stage 3 runs in flight. |
| `host/train/train_clf.py` | Trains the stage 2 blob classifier behind one colour table, from frame folders or blob sets (`.npz`), choosing its L2 penalty and threshold by leave-one-session-out on frame outcomes, and writes `clf_model.*` beside the table (`--out`, default the table's folder). |
| `host/target_monitor.py` | Live viewer with an azimuth gauge, and a CSV logger (needs `pyserial`). |
| `host/viewer/` | `tcview`: image, hand calibration, flight-data, frame-labelling and blob-collection screens (PyQt6, own conda env). `tcview/labels.py` holds the label format and the scoring rule that `eval_frames.py` shares; `tcview/blobset.py` the blob dataset format. |
| `host/testdata/` | The labelled test set: two capture sessions of the red balloon (`red_s1`, `red_s2`; 14 balloon frames, 6 clutter-only), each frame as `.rgb565` + `.json` + `.png` + `.labels.png`. Baseline numbers in `docs/learned-detector-report.md`. |
| `data/` | Saved frames (`.rgb565` + `.json` + `.png`), their hand labels (`<stem>.labels.png`: 0 unlabelled, 1 balloon, 2 background) and CSV recordings. A working folder, untracked; the curated, committed test frames go to `host/testdata/`. |
| `host/Makefile` | `make test` runs the three suites; `make lib` builds `libblob_detect.so` (every portable source) for the viewer. |

## Known gaps

1. **Not calibrated on a real balloon.** The red preset missed one at 1.5 m
   (see the status note). The viewer protocol and settings in flash have not
   run on hardware yet.
2. **FOV is assumed** (54°). It scales every angle and range. Calibrate it.
3. **No attitude compensation.** Bearings are in the camera frame. The board
   that owns the IMU must rotate them before using them as world bearings.
4. **AWB versus a large target** is unmeasured (step 6 of the checklist).
5. **One target colour at a time**, up to four blobs. Tracking a particular
   balloon across frames, and knowing which waypoint is next, is the
   navigator's job on the S3.
6. **Two cameras, two UARTs.** optflow_cam and target_cam each stream on their
   own UART0. The flight board needs two serial inputs, or the two streams
   must be merged.
