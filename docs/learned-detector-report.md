# target_cam learned detector — status report

**Date:** 2026-10-15, updated 2026-10-16
**Project:** `firmware/projects/target_cam` (branch `target-cam-learned-detector`)
**Hardware:** AI-Thinker ESP32-CAM (OV2640, 160×120 RGB565) on an ESP32-CAM-MB
programmer, ESP-IDF v5.5.5. The task statement is
[`claude_code_prompt_learned detector.md`](claude_code_prompt_learned%20detector.md).

This report covers Phase 0 (groundwork), Phase A1 (labelled frames and the
stage 0 baseline), Phase A2 (the learned colour table, stage 1, §8), the
plan for the next frames (§9) and Phase B1 (blob features and data collection
for the stage 2 classifier, §10). Later phases append to it. Each section keeps
**measured** (numbers from the board or from `eval_frames.py` on labelled
frames) apart from **reasoned** (what the numbers suggest, not yet tested).
§7 lists what is not established.

**Current status in one line:** pacing is locked to the sensor at 80.0 ms on
every row (measured, 3752 rows). On 20 hand-labelled red-balloon frames, a
well-chosen HSV calibration already finds every balloon, but **which
calibration you choose swings false blobs from 0 to 38 on the same frames**,
and at pixel level the best setting matches only about half of each balloon.
That calibration sensitivity, and the pixel figures, are what the learned
colour table (Phase A2) has to beat.

**Phase A2 in one line:** held out by session, the learned table finds all 14
balloons (HSV: 13) with blob 1 the balloon every time, and raises pixel F1
from 54 to 66 % (s1 → s2) and from 60 to 75 % (s2 → s1). It pays one extra
false second blob over the two folds. On the board (§8.5) it keeps 80.0 ms on
every row, and on one still scene it cuts the pixel pass from 8.5 to 4.7 ms
and the total detection time from 9.6 to 8.8 ms. On that scene it finds the
balloon in every row, where HSV finds it in 43 %.

---

## 1. The detector being improved

Stage 0 is the existing HSV detector (`main/blob_detect.c`). A pixel matches if
its hue is within `hue ± tol` and its saturation and value are above `sat` and
`val`. The matching pixels are grouped into 8-connected blobs, and the up to 4
largest blobs of at least `area` px are reported, largest first. The ESP32-S3
will steer to blob 1.

Stages 1–3 (a learned colour table, a blob classifier, a small int8 CNN) are
planned on top of it. Each is selected with `!stage N` and falls back to the
highest stage whose models are all valid.

## 2. Phase 0: pacing (measured, 2026-10-14)

The camera's loop is now paced by the sensor's own frame clock (`sensor_us` =
79 968 µs), not by a software timer.

| Recording | Rows | dt ≠ 80 | Rate | Lost | Notes |
|---|---|---|---|---|---|
| `run_20261014_232303.csv`, 300 s, stage 0, a `k` command every 5 s | 3752 | **0** | 12.505 rows/s | 0 | `proc_us` p50 7.7 ms (pixel pass 6.8 ms). `loop_us` p50 8.5 ms, max 34.9 ms. The max is only on rows where the ~400 B `#!CFG` reply was printed, which leaves 45 ms of margin. |
| `run_20261014_232812.csv`, `!stage 3` with no models | 251 | **0** | — | 0 | The camera fell back to `stage_eff=0` on every row, as designed. |

At 115200 baud a CSV v2 row takes 11.5–12.2 ms on the wire. That is a
separate budget from `loop_us`, because the UART's FIFO drains after the print
returns.

## 3. The labelled set (Phase A1)

### 3.1 Capture

All frames were taken on 2026-10-15 in one room, with AWB frozen (`w0`) and the
camera's raw 160×120 RGB565 bytes saved alongside their `.json` settings. The
balloon is a red foil balloon: an oval body with a neck, about 1.3–1.5 times
taller than wide in the image.

| Session | Time | Frames | Calibration | Balloon range |
|---|---|---|---|---|
| `red_s1` | 21:50–22:03 | 7, each with the balloon | **Recalibrated before almost every frame** (hue 0–21, tol 24–38, sat 79–127, val 20–53) | 0.9–2.9 m, 343–3026 px |
| `red_s2` | 22:19–22:32 | 7 with the balloon + **6 clutter-only** | **One setting**: the factory `red` preset (hue 0 ± 12, sat ≥ 110, val ≥ 50) | 1.25–5.2 m, 126–1830 px |

At 4.2 and 5.2 m the balloon is 10 px wide (145 and 126 px of balloon).

The two sessions are in the same room, 15–30 minutes apart, so they are
**not** independent venues (§7).

The 20 frames and their labels are committed under
`firmware/projects/target_cam/host/testdata/red_s{1,2}/`. Five earlier frames
of a green balloon (AWB on) in `target_cam/data/` were used only to try out the
tools.

### 3.2 Labels

Each frame has a `<stem>.labels.png`, painted by hand in tcview's Label tab:
0 unlabelled, 1 balloon, 2 background. "Rest -> background" leaves a 2 px band
around the balloon unlabelled. Rim pixels mix balloon and wall colour, so they
are kept out of both training and scoring.

A balloon, for scoring, is an 8-connected component of label 1 of at least
4 px, with its bounding box. Specular highlights and the dark reflections on
the foil were labelled as balloon: deciding which balloon colours a colour
table can afford to match is the training's job, not the label's.

### 3.3 Scoring rule (`host/tools/eval_frames.py`, shared with the Label tab through `tcview/labels.py`)

- **TP:** a labelled balloon with a reported blob's centroid inside its box.
- **FN:** a balloon no reported blob landed in.
- **FP:** a reported blob in no balloon's box.
- **split:** a second blob in an already-found balloon. It is not a false
  detection, but it costs a report slot.
- **blob 1:** is blob 1 a balloon? On a clutter-only frame, any blob 1 is a
  false target.
- **Pixel precision / recall:** the matched mask against balloon and
  background labels, counting labelled pixels only.

Centroids use the camera's convention (pixel *i* spans [i, i+1)). The first
version of `labels.py` used pixel indices, a 0.5 px offset that would have
scored a centroid in a box's last column as a false detection. The host test
`test_component_centroid_matches_the_c_detector` now guards this.

Each frame is run through the camera's own C pipeline (the shared library),
so these are the numbers the board would produce for the same bytes.

## 4. Stage 0 baseline (measured)

### 4.1 Session 2, its own single calibration (the `red` preset)

| | TP | FN | FP | split | blob 1 is the balloon | false blob 1 on clutter-only | pixel P | pixel R |
|---|---|---|---|---|---|---|---|---|
| red_s2 | 5 | **2** | 0 | 1 | 5 / 7 | **0 / 6** | 75.7 % | **37.3 %** |

The two misses are the 4.2 m and 5.2 m frames. The preset matches only a few
of the balloon's dark-red pixels, fewer than the 25 px area floor. The tight
window never produces a false blob, but it doesn't see the balloon at range.

### 4.2 One calibration across all frames: how much the choice matters

Each of session 1's seven calibrations was applied unchanged to the frames
(`eval_frames.py --cfg`):

| Calibration (from red_s1 frame) | on red_s1: FP / FN | on red_s2: FP / FN | red_s2 false blob 1 (of 6) | red_s2 pixel P / R |
|---|---|---|---|---|
| `90cm_wood_door_2` (hue 0 ± 24, sat 110, val 35) | 0 / 0 | **1 / 0** | **0** | 48.3 / 60.8 % |
| `170cm_wall_shelve` | 1 / 0 | 5 / 0 | 2 | 45.2 / 59.2 % |
| `270cm_door` | 2 / 0 | 6 / 1 | 2 | 32.3 / 59.5 % |
| `290cm_doors` | 6 / 0 | 9 / 0 | 3 | 28.5 / 70.4 % |
| `90cm_wood_door` | 8 / 0 | 18 / 0 | 2 | 29.1 / 66.4 % |
| `110cm_black_shelve` | 8 / 0 | 20 / 0 | 3 | 24.2 / 72.5 % |
| `130cm_wood` (hue 21 ± 38) | 13 / 1 | **38 / 2** | **6** | 10.3 / 61.8 % |

The red_s1 frames scored with their own per-frame settings gave TP 7, FN 0,
FP 3, pixel P 69.6 % and R 72.1 %. That figure is optimistic and is not used as
the baseline: in flight there is one setting.

### 4.3 The baseline A2 is judged against

The fair comparison for a learned table is held-out, so the HSV baseline is
held-out too: a setting chosen on one session, scored on the other.

| Direction | Setting | TP | FN | FP | split | false blob 1 | pixel P | pixel R |
|---|---|---|---|---|---|---|---|---|
| s1 → s2 | best on s1 (`90cm_wood_door_2`) | 7 | 0 | 1 | 0 | 0 / 6 | 48.3 % | 60.8 % |
| s2 → s1 | s2's preset | 6 | 1 | 0 | 2 | — | 92.4 % | 44.9 % |

- The s1 → s2 FP is in `165cm_shelve`. The s2 → s1 FN is `290cm_doors`, the
  smallest balloon in s1.
- At blob level, a well-chosen HSV window is already close to perfect on these
  20 frames. The weakness is the pixel level, and how fragile the choice of
  setting is.

## 5. Where stage 0 goes wrong (measured on the frames, causes reasoned)

**False pixels.** These are mostly dark brown wood: louvred doors and
furniture, with median hue 23–51 and V 41–90.
- A window that reaches past hue ~20, as `130cm_wood` (hue 21 ± 38) does,
  matches whole door panels. It produced a false blob 1 on 6 of 6 clutter-only
  frames.
- Tighter windows leave isolated speckle, which rarely reaches the 25 px blob
  floor.
- In session 1, only 19–55 % of the false pixels sit in the top quarter of the
  background's edge strength, so they are mostly not edge fringes. On the green
  frames, 55–84 % did.
- Whether the speckle is sensor noise in the dark has not been measured.

**Missed pixels.** These are dark blue-purple reflections on the foil (median
hue 199–321, often V < 50), the shadowed side of the balloon, and white
highlights (S < 50, V ≈ 255). The balloon mirrors a dim room, so much of its
surface isn't red. In the backlit `130cm_window` frame, the preset matched only
15 % of the balloon, essentially the top highlight rim.

**What a learned colour table can and cannot fix (reasoned):**
- **It can** use the fact that, in these frames, the balloon's red sits around
  hue 330–360 with S ≥ 100, wood around hue 20–50, and the reflections are dark
  purple. A table can include purple and magenta and exclude orange-brown. A
  single convex HSV box can't do both.
- **It cannot** separate colours that really overlap: dark red balloon against
  dark wood, or a white highlight against a white wall. Excluding these costs
  balloon pixels, and including them costs false pixels. That is stage B's
  job, using shape, fill and edges.
- **The real gain to expect** is pixel recall and stability across settings
  more than blob counts, which are already near the ceiling at these ranges.
  The far frames (≥ 4 m) are where pixel recall decides TP versus FN.

## 6. How Phase A2 will be evaluated

- **Two-fold by session.** Train the table on red_s1 and score it on red_s2,
  then the reverse. Compare each fold with the §4.3 row for the same
  direction. Consecutive frames are near-duplicates, so splitting by frame
  would leak the answer.
- **Report all of it.** Blob TP/FN/FP/split, false blob 1 on the 6 clutter-only
  frames, and pixel P/R, so a gain in one figure can't hide a loss in another.
- **Check it on the board.** `csv_report.py` at stage 1 must still show
  dt = 80.0 on every row. Also report `t_px_us` at stage 1 against stage 0's
  6.8 ms.

## 7. Not established

- **Generalisation to another venue.** Both sessions are one room on one
  evening, with one balloon. A held-out session here measures robustness to
  position, distance and calibration, not to a new room or light. The plan
  expects retraining at the flight venue.
- **The flight venue's lighting.** These frames are dim (balloon V mostly
  50–110). A brighter venue will change both the baseline and the trained
  table.
- **Live-scene behaviour.** The S1/S2 CSV scenes (60 s each, balloon + clutter
  and clutter only) planned for A1 were not recorded, so jumps per minute in a
  moving scene are not measured. Frames are single snapshots.
- **Statistical weight.** 14 balloon instances and 6 clutter-only frames. One
  frame changes recall by 7 points. Differences of one or two blobs between
  stages are within what a different set of 20 frames could produce.
- **The cause of the dark-wood speckle** (sensor noise is suspected, not
  measured).
- **Range beyond 5.2 m** is untested.

---

## 8. Phase A2: the learned colour table (stage 1)

### 8.1 What it is

Stage 1 replaces the HSV test with a table: one bit for each of the 65 536
RGB565 values, 8 kB (`main/color_lut.{c,h}`). Everything after the pixel test
(cells, labelling, the area floor, the blob order) is stage 0's, so any change
in what is reported comes from which pixels match.

- **Why a table.** An RGB565 pixel has only 65 536 values, so the decision for
  each can be stored outright. The accepted set can then have any shape,
  where the HSV test is one box. On the board it is one load and one shift per
  pixel instead of the HSV arithmetic.
- **Where it lives.** It is compiled in as a model record
  (`main/models/lut_model.h`), checked at boot by `color_lut_load()` (magic,
  version, size, CRC-32) and copied to an 8 kB DRAM buffer. A refused record
  is reported in the boot log and `#!CFG` (`lut=bad`), and `!stage 1` then
  falls back to stage 0. A RAM copy keeps random lookups off the flash cache,
  and it is what Phase D's NVS upload will fill, through the same check.
- **One pixel test.** `blob_pixel_matches()` reads the table when
  `blob_cfg_t.lut` is set. The detector's passes, `blob_mask()`, the camera's
  `m` dump and the viewer's overlay all go through it.

### 8.2 Training (`host/train/train_lut.py`)

1. **Count** balloon and background labels per RGB565 value, per session.
   Unlabelled rim pixels count for neither.
2. **Smooth** both counts with a 3-D Gaussian on the 32 × 64 × 32 grid. The
   20 frames show about 2 500 distinct balloon colours, and the next frame
   will show their neighbours. sigma is in 8-bit units on every axis (a green
   step is 4.0 units, a red or blue step 8.2). The kernel's peak is 1, so a
   smoothed count reads as "pixels' worth of evidence at this colour".
3. **Decide.** A colour is in when it is k times likelier among balloon pixels
   than among background (each count over its own total) and has at least
   `min_evidence` balloon evidence. A colour with no evidence stays out.

**Choosing sigma, k and min_evidence** is by leave-one-session-out over 168
settings (sigma 0–16, k 2–30, min_evidence 0.5–2). The objective is the
held-out pixel F1, averaged over the two folds. A setting that puts a false
blob 1 on any held-out clutter-only frame is excluded.

What was tried first (measured): ranking by blob F0.5, with a sum-normalised
kernel. It picked sigma 12, k 3, whose held-out pixel recall (39 % and 43 %)
was **below** the HSV baseline. On 14 balloons one frame moves blob recall by
7 points, so blob counts are too coarse to choose between settings. They follow
from the pixels, and the pixel counts (17 753 balloon, 366 247 background)
are not too coarse. With the absolute evidence floor, the sum-normalised
kernel also made `min_evidence` mean something different for every sigma.

**Result:** sigma 8, k 7, min_evidence 0.5. The neighbourhood is flat: sigma
6–8 with k 5–7 are all within 0.006 pixel F1 of it, so the choice is not on a
knife edge. The final table is trained on both sessions: 7 321 colours
accepted, train id 1792123782. `main/models/lut_model.json` records the
parameters, the frames and the search, and a host test regenerates the
committed bytes from it.

### 8.3 Held-out scores (measured, `train_lut.py`)

Each table is trained on one session and scored on the other. The HSV rows are
§4.3.

| Direction | Detector | TP | FN | FP | split | false blob 1 | px P | px R | px F1 |
|---|---|---|---|---|---|---|---|---|---|
| s1 → s2 | HSV, best setting on s1 | 7 | 0 | 1 | 0 | 0 / 6 | 48.3 % | 60.8 % | 53.8 % |
| s1 → s2 | **table trained on s1** | 7 | 0 | 1 | 0 | 0 / 6 | **73.8 %** | 60.0 % | **66.2 %** |
| s2 → s1 | HSV, s2's preset | 6 | 1 | 0 | 2 | — | 92.4 % | 44.9 % | 60.4 % |
| s2 → s1 | **table trained on s2** | **7** | **0** | 1 | **0** | — | 91.9 % | **63.4 %** | **75.0 %** |

- **The gain is in the pixels, as §5 expected.** Precision went up where HSV was
  loose (s1 → s2) and recall where it was tight (s2 → s1), with no loss on the
  other figure beyond 0.8 points.
- **The far and backlit frames**, with the table trained on s1 only:

  | Frame | HSV preset | Table |
  |---|---|---|
  | `420cm_2do_door` | missed, recall 26 % | found, recall 43 % |
  | `520cm_2do_room` | missed, recall 12 % | found, recall 22 % |
  | `130cm_window` | recall 15 % | recall 28 % |

- **The cost:** one more false blob over both folds (2 against 1). Both are
  second blobs, in `130cm_wood` (s1) and `130cm_window` (s2). Blob 1 is the
  balloon in all 14 balloon frames, and in no clutter-only frame is there a
  false blob 1.
- **Caveat:** sigma, k and min_evidence were chosen on these same two folds,
  so the table rows are slightly optimistic. A session kept out of all of
  this, such as the students' `red_s3`, is the unbiased test.

On its own training frames the final table scores TP 14, FN 0, FP 2, pixel
P 88.1 %, R 64.9 % (`eval_frames.py --stage 1 host/testdata`). This is
training data, so it shows the table loaded and works, not how good it is.

### 8.4 What the table learned (measured on the 20 frames)

The final table is compared with s2's HSV settings (hue 0 ± 12, sat ≥ 110,
val ≥ 50; 3 875 colours) over all labelled pixels:

| Pixel group (HSV) | Balloon px | Table accepts | HSV accepts | Background px | Table accepts | HSV accepts |
|---|---|---|---|---|---|---|
| red, hue 330–20, S ≥ 100 | 12 442 | 84.0 % | 59.9 % | 12 673 | 8.4 % | 9.7 % |
| purple/blue, hue 180–330 | 1 823 | 15.4 % | 0 % | 71 499 | 0.1 % | 0 % |
| highlights, S < 50 | 1 050 | 34.4 % | 0 % | 87 888 | 0.3 % | 0 % |
| wood hue 20–60, S ≥ 50 | 1 287 | 15.2 % | 0 % | 71 717 | 0.1 % | 0 % |

- **Most of the gain is inside red.** The table takes 84 % of the balloon's
  saturated red against 60 %, while accepting less of the background's red.
  No single hue window with two floors can match that.
- **It reaches outside the box only a little.** It takes 15 % of the purple
  reflections and 34 % of the highlights, at a 0.1–0.3 % background cost.
- **Not fixed: red on the background.** 8.4 % of saturated-red background
  pixels still match. These are colours the balloon and the room really share
  (§5), which is stage B's job.

### 8.5 On the board (measured, 2026-10-15)

The firmware is `bc92ee1-dirty` (the A2 working tree). The camera was still,
pointed at the red balloon with clutter in view (the camera's own range
estimate: 1.57 m median).
Two `csv_report.py` recordings were taken without moving anything: stage 1
for 120 s (`data/run_20261015_233153.csv`), then stage 0 for 60 s
(`data/run_20261015_233703.csv`).

- **The table loaded and matches the PC's.** `#!CFG` reported `lut=ok
  lut_id=1792123782`. With `g` at stage 1, the camera's own row and the PC
  library's result for the same frame agreed exactly: 2 blobs, blob 1 area
  1099 px, 1501 matched pixels. Neither the compiled-in table nor the byte
  order differs between the camera and the PC.

| Same scene | Stage 0 (HSV) | Stage 1 (table) |
|---|---|---|
| rows, dt ≠ 80 ms | 752, **0** | 1502, **0** |
| pixel pass `t_px_us` p50 / max | 8 541 / 8 598 µs | **4 698 / 4 698 µs** |
| labelling (`proc_us − t_px_us`) p50 | 1.0 ms | 4.1 ms |
| total `proc_us` p50 / max | 9 592 / 10 864 µs | **8 798 / 9 159 µs** |
| worst `loop_us` (first row excluded) | 13.4 ms | 11.9 ms |
| matched pixels p50 | 338 | 1 499 |
| rows with a target | 42.7 % | **100 %** |
| blob 1 area when found, p50 | 37 px | 1 073 px |
| jumps (blob 1 moves > 10 px) | 82 | **0** |

- **The pixel pass is 45 % faster and its time does not vary.** At stage 1
  every pixel costs the same load and shift, so the time is 4 698 µs on every
  row. The HSV pass took 6.8 ms in the Phase 0 scene and 8.5 ms in this one,
  so its cost depends on the colours in view. The drop is smaller than on the
  PC (62 %). *Reasoned, not measured:* at about 59 CPU cycles per pixel, most
  of the per-pixel time goes to fetching the pixel and adding it to its cell,
  not to the colour test.
- **Labelling costs more because more pixels match.** Its time grows with the
  matched pixels, which went from 338 to 1 499. The pixel pass saves more
  than labelling adds, so the total still drops by 0.8 ms.
- **HSV saw this balloon only in pieces.** Its blob 1 had a median of 37 px,
  and it switched between pieces 82 times in a minute. The table's blob 1 is
  the whole balloon, in the same place on every row (cx ≈ 91 px). In the
  grabbed frame its box covers the balloon and runs 3–4 px past it on the
  right. That is one or two 2-px cells. *Reasoned:* the cause is either
  blended edge colours that the blur accepted, or the box rounding up to whole
  cells. The centroid is about 0.3° off at most.
- **A false second blob is present on every stage 1 row** (median az −3.5°,
  el +18.8°: the orange box in the grabbed frame). It is wood in the
  background (identified on the image by the user), the same kind of error
  as the held-out false second blob in `130cm_wood` (§8.3): a colour that the
  balloon and the wood share. The balloon stays blob 1, so the flight output is
  not affected, but it is the case stage B is meant to remove.
- **The 38.3 ms `loop_us` in the stage 0 recording is its first row.** That
  iteration handled `!stage 0` and printed `#!CFG`, which the report's margin
  line includes.

This is one scene, so it shows the direction of the effect and the timing,
not a miss rate. The S1/S2 scripted scenes (§6) have not been repeated at
stage 1.

### 8.6 Not established (A2)

- **Every §7 limit applies.** That includes one room and one evening, so the
  table is a model of this room's light and walls.
- **Exposure and white balance.** The table was trained with AWB frozen (`w0`).
  How far a different exposure moves the balloon's colours off the table has
  not been measured. Retraining at the venue is the plan (README,
  "Retraining the colour table").
- **The parameter search is small-sample.** A one-frame change in either
  session can reorder settings within the flat top.

---

## 9. Next collection: plan (2026-10-16)

Nothing in this section is measured yet. It sets out how the next frames
will be used, written before they are taken, so the use can't be fitted to
the results afterwards. The procedure for students is in the frame-collection
guide.

### 9.1 `red_s3`: an unbiased test first, training data second

- **Score before training.** The table's sigma, k and min_evidence were
  chosen on `red_s1` and `red_s2` (§8.3), so its scores on those sessions are
  slightly optimistic. Run `eval_frames.py --stage 1` on `red_s3` (and
  `--stage 0`, for the baseline) before `red_s3` is used for anything else.
  Those are the first scores of the table on frames that had no part in
  making it.
- **Then retrain on all three sessions,** with the leave-one-session-out
  search repeated over three folds.
- **The same session rules as before** (AWB frozen with `w0`, one
  calibration, no reset), because the table learns raw colours (§8.6).
- **What it should add:** wood, which gives the false second blob in
  `130cm_wood` and on the board (§8.5); frames at 4–8 m; backlit frames; and
  about a third of the frames with no balloon. A different room or light would
  be worth more than any of these (§7).

### 9.2 `orange_s1`: orange balloons as negatives

The competition rules mention only red balloons, so for this detector an
orange balloon is never a target. It is something the detector must not
report.

- **Its own folder, with the orange balloon painted as background.** The
  labels then say what the red detector should do. `eval_frames.py` counts a
  blob on the orange balloon as a false positive, and a false blob 1 on a
  frame with only the orange balloon as a false target. A frame with both
  balloons has the red one painted as balloon.
- **Stage B is where it should help** (reasoned). An orange balloon has the
  shape, fill and edges of the target, so a blob classifier can only reject
  it on colour. Without such examples, B could learn that "round and filled"
  means balloon.
- **The colour table does not train on it at first.** A red balloon's lit
  side drifts toward orange (`blob_detect.c`, the hue window comment), so
  orange-balloon pixels counted as background would also push some red
  highlight colours out of the table. `train_lut.py` reads only the folders
  it is given, so leaving `orange_s1` out needs no code. The plan is to
  measure first: score the current table on `orange_s1`. Then train with it
  only if the table accepts the orange balloon, and only if `red_s3`'s pixel
  recall does not drop.
- **The folder split is not a clean test** for B, because the same room and
  light as `red_s3` leak across it. It is a separate folder so that it can be
  left out of the table's training, not as an independent session.

### 9.3 Frames for Phase B1

Frames saved now are also Phase B1 data. The PC library runs the camera's
pipeline (shown equal on a grabbed frame in §8.5), so it can rebuild each
blob, its features and its 24 × 24 crop from a saved frame. The pixel labels
then label the blobs: a blob whose centre is inside a painted balloon is a
balloon, and any other is not. Saved frames also survive a retrained table,
because the blobs can be rebuilt from the pixels. Blobs collected live with
`!collect` cannot be. Their limit is volume: at about 3.3 s per frame they
give tens of blobs, against hundreds from `!collect`.

---

## 10. Phase B1: blob features and data collection (2026-10-16)

Stage 2 will be a classifier that looks at each blob the colour stage
reports and decides "balloon or not". B1 builds what it needs: the features,
a way to collect them from the camera, and a way to label them. **Nothing
decides anything yet.** At stage 2 the camera still falls back to stage 1,
because there is no classifier model.

### 10.1 What was built, and why each choice

- **`main/blob_feat.{h,c}`: 12 features and a 24 × 24 crop per reported
  blob.** The features are `fill`, `log_aspect`, `hue_spread`, `sat_mean`,
  `sat_std`, `val_mean`, `highlight`, `edge`, `texture`, `ring`, `cy_norm`
  and `log_area`. The reason for each is in the file's header. In short, they
  measure shape (a sphere fills π/4 of its box at aspect 1), surface (mylar
  reflects the room and has a specular highlight; paint is flat) and context
  (a fragment of a larger red region has red just outside its box). The plan
  listed about 10. `texture` (gradient inside the blob) and `ring` (colour
  just outside the box) were added because the false blobs in §5 are pieces
  of larger surfaces (louvred door panels, furniture). A piece can have a
  balloon-like box, and its colour already passed the colour test.
- **Portable C, integer per-pixel arithmetic.** The same file runs on the
  camera and, through `libblob_detect.so`, on the PC. So the classifier is
  trained on the numbers the camera will compute in flight, not on a Python
  re-implementation of them. Hue spread is a circular statistic, which
  normally needs `sinf`/`cosf`. A Q14 sine table replaces them, so the
  per-pixel sums are bit-identical on both machines. Only the final `logf` and
  `sqrtf` can differ, by an ulp.
- **Cost bounded by the blob, not the frame.** The box is sampled on the
  coarsest grid that keeps at most 1024 samples, so a blob filling the frame
  costs about what a 32 × 32 one does. Stage 2 has to fit in the 80 ms
  period beside stage 1's ~9 ms (§8.5).
- **`!collect 1` sends one `#!BLOB` record per reported blob, every 4th
  frame, and on every frame sent to the PC.** A record is about 1.44 kB: the features as text, plus the crop as
  1152 binary bytes, framed and CRC-checked like a `#!FRAME`. Every 4th
  frame, because a record costs 125 ms on the wire at 115200, and
  consecutive frames are near-duplicates anyway. A frame that goes to the PC
  (`g`, `v1`) always carries its records, so the agreement check works at
  115200 (added after the first board session, below). Collection is a
  bench mode: the flight screen turns it off and its banner names it.
- **The crop is 24 × 24, from a square 1.5 × the blob's longer side.** The
  margin keeps the balloon's edge and some background, which is what tells a
  balloon from a red patch on a larger object. 24 rather than 16, because a
  24 crop can be reduced to 16 exactly (`blob_crop_down16`, tested) and a 16
  crop cannot be enlarged back. The CNN's input size is Phase C's decision,
  and this data serves either. Area averaging uses integers only, so the PC
  reproduces the crop bit for bit.
- **`BLOB_FEAT_VERSION` (`fv`) travels with every record and dataset.**
  Changing a feature's definition changes the number. `blobset.save` refuses
  to mix versions, so a classifier is never fed features computed by rules it
  wasn't trained on.
- **Two sources of labelled blobs, one format (`tcview/blobset.py`).** The
  first is live records, labelled by hand in the new Blobs tab. The second is
  blobs rebuilt from saved, hand-labelled frames (§9.3): the tab's "Add from
  labelled frames…", or `host/tools/blobs_from_frames.py`. Saved frames are
  labelled automatically from the paint, by the rule the frame scores use: a
  blob is a balloon if its centroid lies in a painted balloon's box.
- **Board/PC agreement check, built into the tab.** When a record arrives for
  a frame that was also sent (`v1`, or `g`), the PC runs the same pipeline on
  those pixels and compares. Features must match to the record's 6
  significant digits, and the crop must match bit for bit. This is the B1
  board check, and it runs throughout collection, not once.

### 10.2 Deviations from the plan

- **Features run in a separate scan after detection, not inside
  `blob_detect`'s labelling pass.** The plan put them in "pass 4". They are
  computed in `tc_pipeline_run`, after the blobs are known, by scanning each
  reported blob's box (plus the ring). This keeps `blob_detect.c`, which is
  tested and committed, unchanged. It means the features only cost anything
  for the ≤ 4 reported blobs, never for the others. The price is the
  approximation in §10.4: the box scan re-finds the blob's pixels by the
  colour test.
- **The Blobs tab does not pre-fill labels from "the current stage's
  verdict".** No stage has a per-blob verdict yet (`conf` is −1 below stage
  2). `blobset.verdict()` will pre-fill from `conf` once stage 2 exists.
  Live records start unlabelled ("?"). Saved frames are labelled from the
  paint, which is better than any verdict.
- **Labelling keys:** a click selects (and shows the features), a
  double-click cycles one tile, and B / N / U label the whole selection. A
  single click that also changed the label would make it impossible to look
  at a blob without relabelling it.
- **`!stage 2` turns the features on** although no classifier exists. Stage 2
  then falls back to 1, but `t_b_us` measures what stage 2's features will
  cost, at 80 ms, before there is a model.

### 10.3 Measured (PC, no board yet)

- `make -C host test`: the three C suites pass under ASan and UBSan
  (`test_blob_feat`: fill, aspect, highlight fraction, hue across 0/360, ring
  band, stride rule, byte order, crop rule, all with exact expected values).
  The viewer's 98 tests pass, including the crop and its 16 × 16 reduction
  against a numpy re-implementation (bit for bit, at the frame edges, both
  byte orders), `#!BLOB` parsing at any read chunking, and a whole collection
  session against the fake camera, where every record agrees with the PC.
- The firmware builds with no warnings.
- **The committed test frames give 16 stage-1 blobs: 14 balloons and 2 not**
  (`blobs_from_frames.py host/testdata`). Both false blobs are clipped
  white, a lamp (`red_s1/130cm_wood`) and a window (`red_s2/130cm_window`),
  with mean saturation 1–3 and mean value 255. The colour table accepts pure
  white because the mylar highlights were labelled as balloon (§3.2). So
  `sat_mean` alone would reject both. **Two negatives are an anecdote, not a
  training set.** They show that the features measure what they were meant
  to, and that stage 1's remaining false blobs are not the "red door" kind
  the features were mostly designed for.

### 10.4 Approximations and what is not established

- **The blob's pixels are re-found as the box pixels that pass the colour
  test.** A second blob reaching into the box, or matching pixels that the
  cell pooling discarded, count as this blob's. For overlapping boxes the
  features mix. Reasoned to be rare at blimp ranges, but not measured.
- **For large blobs, every feature except fill, aspect, cy and area is
  estimated from ≤ 1024 samples.**
- **Not yet on the board:** the agreement check against real camera records,
  the collection rate, and the features' cost (`t_b_us`) at stage 1 on real
  scenes. These are the B1 board check.
- **Whether any feature separates balloons from real clutter** is Phase B2's
  measurement, and needs hundreds of labelled blobs from more than one session
  (§9).

### 10.5 First board session: the link hang (2026-10-17)

**Observed (user report):** with the link switched above 115200,
`!collect 1` and then `v1`, some records arrived in the Blobs tab. Then the
viewer's terminal showed only noise characters and the camera answered no
command. The user reports that the ESP32-CAM-MB cannot run faster than
460800, which agrees with the failures logged on 2026-10-14
(`docs/artifacts/target_cam and tcview.md`, link table).

**Cause (reasoned from the code, not measured):** the two ends were left at
different speeds. After a confirmed switch the camera stays at the new speed
for good; its 3 s revert only guards the handshake. The viewer's watchdog
drops to 115200 after 3 s of junk at a faster speed, on the assumption that
the camera rebooted. If the link instead fails under the image traffic, the
camera is still up at the fast speed, and from then on each side hears the
other as noise. A replug recovers, because every boot starts at 115200.

**Fixes:**
- The watchdog first sends `B115200` at the fast speed, then follows. A
  short command can get through a link that cannot carry a 38 kB frame. To a
  camera that really rebooted, the request is a few bytes of noise.
  `test_gui_fake.py` reproduces the hang with a fake whose output turns to
  noise at the fast speed. It fails without the fix and passes with it.
- A frame sent to the PC (`g`, `v1`) now always carries its records. Before,
  records went out on every 4th frame only, so at 115200 most grabs had
  nothing to compare against. That is why the board check had asked for
  921600. It now runs at 115200 with `g`.

Not established: whether the bridge, the USB host or the ESP32 side corrupts
the fast link, and whether the `B115200` request gets through on the real
bridge.

### 10.6 Board check (2026-10-18)

**Measured** on the ESP32-CAM-MB at 115200, after the GUI redesign:
`!collect 1`, then 3 × `g` with the balloon in view.

- **Agreement:** 4 records were checked against the PC library. Features
  were equal in 4 of 4 and crops identical in 4 of 4. The worst feature
  difference was 4×10⁻⁶ (relative, `hue_spread`), against the 2×10⁻⁵
  threshold.
- **Reading (reasoned):** the crops are integer-only, so they are expected
  to match exactly, and they do. The features differ only in the final
  float steps: the libm calls on two machines, and the 6-digit `%.6g` print.
  At 4×10⁻⁶ a classifier trained on PC-computed features sees the same
  numbers the camera computes.
- **Crops by eye:** the user opened new tiles; the big crop shows the
  balloon.

**Collection rate (measured, pill on the Blobs tab, 10 s average).** With
the balloon in view: 2.6–2.7 blobs/s at 115200, and 3.1–3.5 blobs/s at
460800 (the user raised the speed to compare; the link held).

- **Reading (reasoned, not measured):** records go out every 4th frame, so
  one blob per frame at 12.5 FPS gives about 3.1/s. At 115200 one 1.44 kB
  record takes about 125 ms on the wire, longer than the 80 ms frame, so
  the loop most likely waits on the UART and frames are lost; at 460800 it
  takes about 31 ms and fits. Rates above 3.1/s mean some sampled frames
  held a second red blob, since each blob gets its own record. The cause of
  the 115200 loss was not confirmed with a dt reading while collecting.
- **Consequence:** 115200 is enough for collecting (a few hundred blobs per
  scene in a couple of minutes) at about 20 % lower rate. Flight is not
  affected, since the Flight data tab turns collection off.

**Check 2 (measured).** Collection off, `!stage 2`, 115200, balloon in
view. The camera reports `stage 1 running (requested 2; clf none)`: with no
classifier yet the stage falls back, but the features are computed on
every frame, so this measures what stage 2 will cost before B2.

| Reading (Link health card, 60 s after Reset counts) | Value |
|---|---|
| rows/s | 12.5 |
| dt_ms mean / sd / max | 80.0 / 0.0 / 80.0 |
| dt ≠ 80 | **0 of 860 rows** |
| `t_px_us` p50 | 4706 |
| `t_b_us` p50 / max | 1864 / 1922 |
| `loop_us` p50 / max | 14062 / 14191 (budget 80000) |
| wire | 1864 B/s, 16.2 % of 115200 |

- The blob features cost about 1.9 ms per frame, and the whole loop uses
  about 18 % of the 80 ms period, so stage 2 keeps every row on time.
- A first reading without the reset showed 2891 late rows of 13009, worst
  3493 ms. That counter ran since connection and so included the collection
  period at 115200 and the baud changes (a 3.5 s gap is the length of a
  speed handshake); it is not a stage 2 result. The counter is cumulative by
  design (`flight_tab.py` header: one late frame in ten minutes must still
  show), which is why it needs **Reset counts** before a check.

**B1 board checks are complete.** Next is B2, the blob classifier.

### 10.7 First blob sets: a trial, and the white-balance finding (2026-10-18)

Three live sets were collected and labelled by hand (`target_cam/data/`,
untracked), stage 1, firmware `ea19ecb-dirty`. Each was saved as its own
file and is treated as one session.

| Set (saved) | Blobs | balloon | not | The "not" blobs (seen on contact sheets) |
|---|---|---|---|---|
| `blobs_20261018_154233` | 491 | 486 | 5 | fragments split off the balloon |
| `blobs_20261018_221652` | 589 | 332 | 257 | mostly a pink balloon; dark fabric patches, edge pieces |
| `blobs_20261018_222513` | 487 | 295 | 192 | mostly an orange balloon; dark patches, edge pieces |

The last two come from one power-up of the camera (its clock runs on from
one to the next) but show different rooms.

**Trial (measured, scratch script, not committed).** Logistic regression
on the 12 features, standardised, class-weighted, trained on two sets and
scored on the third:

| Held out | Average precision | At p ≥ 0.5 |
|---|---|---|
| 15:42 | 1.000 | 0 of 5 negatives pass (too few to mean much) |
| 22:16 | 0.684 | P 0.59, R 0.20 |
| 22:25 | 0.617 | P 0.65, R 0.99; 160 of 192 negatives pass |

In the 22:25 set the orange balloon was the **largest** blob 140 times, so
stage 1 would have steered to it. Inside one set (trained on one half in
time, scored on the other) average precision is 0.70–0.99, so the features
do separate within a scene and fail across scenes.

**Why (measured, then reasoned).** The features describe shape and
surface, and another balloon has the same ones. Only colour differs, and
the set has no colour-position feature (hue_spread is a spread, not a
mean). Adding the saturation-weighted mean hue of the crop's centre did not
help, because the red balloon's own hue moves more between scenes than the
gap to the other balloon:

| Set | Red balloon, median hue | Other balloon |
|---|---|---|
| 15:42 | −2° | — |
| 22:16 | −23° | −20° (pink) |
| 22:25 | +3° | +10° (orange) |

The ~25° drift of one balloon between rooms is most likely white balance
(reasoned, not shown). `w0` freezes whatever gains the camera holds at that
moment, those gains were never recorded, and in these three sessions it is
not known whether AWB was frozen at all.

**Decision (user, 2026-10-18).** Only red balloons are expected in the
flying area, but the user chose to fix the camera's colour rather than
leave the other balloons out of the training. The reason is that fixed
white-balance gains make the colours comparable between cameras and rooms.
Consequences, in order:

1. Firmware: fixed white-balance gains, recorded with every frame, CSV
   header and blob record.
2. Score the **current** table on new labelled frames taken with the fixed
   gains (`eval_frames.py`). Retrain it only if it falls short.
3. Collect B2's blob sets again. Features are computed on the pixels the
   table accepts, so these three sets do not carry over. They stay as a
   trial batch.

The commit before this change is recorded in the next section.

### 10.8 Fixed white-balance gains (2026-10-18)

**Commit before the change: `5dff256`** ("B1 board check done, first blob
sets and the white-balance finding"). Everything after it on this branch
belongs to the fixed-gain camera.

**A finding from the saved frames (measured).** Every saved frame in
`red_s1`–`red_s5` (58 frames) and `orange_s3`–`orange_s5` (10) has
`"awb": 0` in its `camera_cfg`. The colour table was therefore trained on
frozen gains, but the gains themselves are not recorded and differed from
session to session. Whatever gains a session froze cannot be recovered.

**What was built.**

- **`w2`, fixed.** `w0` (frozen) and `w1` (auto) keep their meaning. `w2`
  writes the R, G, B gains to the OV2640 DSP registers 0xCC, 0xCD and 0xCE,
  then sets manual-gain mode (0xC7 bit 6) with AWB on. That is the
  combination the driver's light presets use, so it is the one known to
  work.
- **`W<r>,<g>,<b>`** loads gains by hand.
- **`Wc`** derives gains from a white card held over the centre 16×16
  patch. As first built, each step scaled R by G/R and B by G/B, then waited
  3 frames for the sensor to apply the change. That failed on the board and
  was replaced by a bracketed search (see "First board run" below). G stays where it is, so overall brightness is
  not touched. It stops when both ratios are within 3 % of 1. It refuses
  when:
  - G is below 40 (dark);
  - more than 10 % of the patch is clipped;
  - a gain would leave 16–255 (0.25×–4×);
  - it has not converged within 12 steps.

  A refusal puts the previous mode and gains back. Every step is printed,
  so a calibration that fails shows how it failed.
- **`W` alone** reads the registers back (`#!WB`).
- **NVS settings v5.** The struct grew from 28 to 32 B, with the gains
  appended. A v1–v4 record is read as the first 28 B of a v5 record and
  gains unity gains; its awb (0 or 1) is kept.

**Where the gains are recorded.** The aim is that no pixel used for training
is again of unknown colour:

- `#!CFG` carries `wb=` (mode), `wb_gain=` (the gains in use, `0,0,0` in auto
  and frozen, where they are not known) and `wb_fixed=` (the gains `w2`
  would use). The viewer's saved frames keep `#!CFG` in `camera_cfg`, so
  they carry the gains too.
- Each `#!BLOB` record carries `wb=` and `wb_gain=`.
- Blob sets (`.npz`) get `wb` and `wb_gain` columns. Older sets load as
  `unrecorded`.
- The viewer's flight CSV gets a `# !CFG k=v ...` line at the start and at
  every change while recording.
- The boot banner says which mode is in use and with which gains.

**Reasoned, not measured.**

- **64 = gain 1.0** is inferred: in every one of the driver's presets G is
  0x3F–0x41. The OV2640 datasheet available here does not document these
  registers.
- **It is not known whether 0xCC–0xCE follow the AWB's own gains while it
  runs.** If they do, a `W` read in auto mode would show different values in
  a warm scene and a cool one. Frozen gains could then be recorded too, and
  `Wc` could start from them. Until that is measured, auto and frozen report
  `0,0,0`.
- A white card under the flying light is assumed to be a fair grey
  reference for that light. A card that is not neutral (e.g. bluish office
  paper) shifts all colours by its tint. It does so consistently, though, so
  sessions calibrated with the same card remain comparable.

**Tests (PC, measured).** 212 viewer tests pass, 6 of them new: blob-record
and `#!CFG` round trips with gains, `#!WBCAL` parsing, the `.npz` column
including an old file without it, the Calib tab's controls, and a fake-camera
session through `W<r>,<g>,<b>`, a refused `W300,…`, auto, and `Wc`. The C host
suite passes, and the firmware builds without warnings. **Nothing has been
run on the board yet.**

**Next (the order in §10.7).** On the board:

1. Check `W` in auto mode, in a warm scene and then a cool one.
2. Run `Wc` in the flying light and `!save`.
3. Collect labelled frames with the fixed gains, and score the **current**
   table on them (`eval_frames.py`) before deciding whether to retrain.

**First board run (measured, 2026-10-19).** White printer paper filled the
centre patch. `Wc` started from 64,64,64 in the frozen mode, and the patch
read as below (0–255 means):

| Step | Gains R,G,B | R | G | B | R/G | B/G |
|---|---|---|---|---|---|---|
| 1 | 64,64,64 | 61 | 115 | 70 | 0.53 | 0.61 |
| 2 | 120,64,105 | 172 | 66 | 158 | 2.61 | 2.39 |
| 3 | 46,64,44 | 10 | 147 | 0 | 0.07 | 0.00 |

After step 3 the proportional correction asked for an R gain of about 676
(46 / 0.068). It also asked for an unbounded B gain, because B read 0. Both
are outside 16–255, so the calibration was refused as `range`. Two things are measured here:

- **Unity gains are not neutral.** With all three registers at 64, white
  paper reads green (R/G 0.53, B/G 0.61). The OV2640's raw green channel is
  more sensitive, and the presets' larger R and B values fit this (§10.8
  inferred 64 = 1.0 from them; this does not contradict it).
- **The response is much steeper than proportional.** R/G rose 4.9× for a
  1.9× gain, and fell 8× for a 0.72× gain. G also moved (115 → 66 → 147).
  That is most likely auto exposure answering the brightness change
  (reasoned, not shown).

Fitted to these three points, R/G ≈ 0.026·(v − 43) and B/G ≈ 0.031·(v − 44).
That is a multiplier with an offset near 44, perhaps a black level
subtracted after the gain. Three points cannot establish that law, so the
fit was not built into the firmware.

**What changed.** `Wc` now searches each gain on its own, assuming only that
a larger register value gives a stronger channel:

- Until the answer is bracketed, it takes half the proportional step (in
  log).
- Once bracketed, it uses the secant between the closest readings on either
  side.
- When the two sides are 1 register step apart, it takes the closer of the
  two.
- A clipped R or B counts as too strong. Only clipping of G, or clipping on
  the first reading, refuses as `clipped`.
- It allows at most 16 steps.

A Python copy of the search reached R/G and B/G within 3 % in 3–4 steps on
the offset fit and on a power law of exponent 2.5, and in 5–7 steps on a
plain multiplier, with or without 2 % reading noise (scratch script, not
committed). It has not been run on the board yet.


**Second board run, with the search (measured, 2026-10-19).** Same paper,
started from `w2` at 64,64,64:

| Step | Gains R,G,B | R | G | B | R/G | B/G |
|---|---|---|---|---|---|---|
| 1 | 64,64,64 | 71 | 125 | 82 | 0.57 | 0.66 |
| 2 | 85,64,79 | 115 | 99 | 108 | 1.16 | 1.09 |
| 3 | 79,64,76 | 92 | 98 | 98 | 0.94 | 1.00 |
| 4 | 81,64,76 | 97 | 96 | 96 | 1.01 | 1.00 |

It converged in 4 steps to **81,64,76**. The offset fit from the first run
had predicted 82,77 (§10.8 simulation). That agreement supports the fit but
does not prove it: it is one more scene under the same light.

---

## 11. Phase B2: the blob classifier, first run on fixed-gain frames (2026-10-29)

### 11.1 What was built

Ported to this branch from `recognize-interface` (commit `c362037`), where it
was written: `main/blob_clf.{c,h}` (logistic regression on the 12 standardised
features, record magic `TCBC`, 180 B, carrying the train id of the colour
table it was trained behind), stage 2 in `tc_pipeline.c` (blobs below the
threshold are dropped, the rest move up in size order), the boot check in
`target_main.c` (`clf=ok|none|bad|mismatch`, `clf_id=` in `#!CFG`), and
`host/train/train_clf.py` (Newton's method, L2 penalty and threshold chosen
by leave-one-session-out on a frame cost: false blob 1 on empty frames +
frames whose blob 1 is not the balloon + balloons lost). With no classifier
installed `main/models/clf_model.h` is a placeholder and stage 2 falls back
to 1. The C suites, 223 viewer tests and the firmware build pass (one viewer
layout test, `test_image_keeps_most_of_a_small_screen`, fails before and
after the port: the image gets scale 0.84 at 1024×600).

One change to the trainer, from the run below: a setting that loses more than
`--max-lost` (10 %) of the balloons stage 1 found on held-out frames is
EXCLUDED. Without it the search chose a classifier that drops every blob
(cost 66 = all 66 balloons lost, against 94 for stage 1 alone).

### 11.2 Data (measured)

Fixed white-balance frames in `target_cam/data/train`, grouped by capture
session (the `s3/table_*` and `s3/classifier_*` folders are one session split
by balloon colour, so holding out one of them is not a held-out session):

| Session | Frames | Gains R,G,B | Balloon frames | Others |
|---|---|---|---|---|
| s1 | 44 | 64,64,80 (and 63,64,81) | 26 red | orange balloon, room |
| s3 | 120 | 76,64,75 | 40 red | orange, pink balloon, room |
| s4 | 4 | 77,64,74 | 0 | room |

Orange and pink balloons are painted as background, as §9.2 planned.

### 11.3 Results (measured; table 1792216162 unless stated)

- **Stage 1 alone puts a false blob 1 on 77 of 102 frames without a red
  balloon**, mostly the orange and pink balloons. A table retrained on s1 + s3
  + s4 (sigma 8, k 5, 8 510 colours) does no better: 63 of 80 on its own s3
  training frames, and every setting of its search is EXCLUDED for false
  blob 1 when s3 is held out. On colour alone, these balloons are not
  separable in these frames.
- **The classifier lowers the held-out cost only from 94 to 83** (l2 10,
  threshold 0.25): 65 false blob 1s remain, and 2 more balloons are lost.
  Largest weights: `sat_mean` +0.66, `ring` −0.45, `hue_spread` +0.40.
- **Mean hue of each blob's centre** (saturated pixels of the 12×12 crop
  centre, median per folder):

  | Session | Red balloon | Orange | Pink |
  |---|---|---|---|
  | s1 (64,64,80) | −18° | −22° | — |
  | s3 (76,64,75) | +1° | +9° to +10° | −33° to −35° |
  | test/red_s1 (64,64,80) | −28° | −27° (orange_s1) | — |

### 11.4 Reading (reasoned, not shown)

- Inside s3, one set of gains, the three balloons sit apart in mean hue
  (red +1°, orange +10°, pink −34°), with some overlap between red and
  orange. A classifier given a colour-position feature could use that.
- Between sessions the red balloon itself moves about 20° (−18° in s1, +1°
  in s3), more than the red–orange gap, which is what §10.7 found with AWB
  frozen. Both sessions were white-card calibrated, so either the card or
  the light differed, or `Wc`'s result does not make colour comparable
  across light; s1's 64,64,80 leaves R at unity, unlike every other
  calibration seen (81,64,76 on the first board run). Not established which.
- So the 12 features cannot be expected to reject another balloon, and
  colour cannot be trusted across sessions until the gains are. The next
  data should hold the gains equal across sessions (one `Wc`, saved, reused),
  and stage 2 or 3 should be judged on the orange/pink frames of a session
  it did not see.

### 11.5 Not established

- Whether a mean-hue feature (`BLOB_FEAT_VERSION` 2) or the stage 3 CNN on
  the crops separates the balloons across sessions.
- Whether `Wc` gives the same gains for the same light on two days.
- Nothing in this section has run on the board; no classifier is installed.

---

## 12. Phase C2: the crop CNN, stage 3 (2026-10-29)

### 12.1 What was built

- **`main/blob_cnn.{c,h}`**: the network costed in the feasibility analysis
  (three 3×3 convolutions with ReLU, 8–16–16 channels, two 2×2 max-pools,
  global average, one logistic unit), on 16×16 crops by default
  (`blob_crop_down16` of the 24×24 crop) or 24×24. Int8 weights per layer,
  uint8 activations, integer requantisation (multiplier and shift) and a
  rounded global average, so everything up to the logit is integer and the PC
  reproduces it bit for bit; only the final logistic is float. Record magic
  `TCCN`, carrying the train id of the colour table it was trained behind.
  Static activation buffers (~15 kB); no heap, no TensorFlow Lite Micro.
- **Stage 3 in `tc_pipeline.c`**: the CNN checks the `cnn_k` largest blobs
  stage 2 kept, drops the ones below its threshold, and the rest move up. The
  survivors past `cnn_k` keep stage 2's confidence (decided at implementation,
  open to review): blob 1 is always checked.
- **Firmware**: the CNN is loaded and checked at boot (`cnn=ok|none|bad|
  mismatch`, `cnn_id=`), `!thr3` and `!cnnk` set the threshold and the blobs
  checked (settings v7, 36 B; v5/v6 records read as a prefix), and
  `!cnnbench [16|24]` times the network on the board (phase C0).
- **`host/train/train_cnn.py`**: training in float with numpy alone (im2col
  convolutions, hand-written backpropagation checked against finite
  differences, Adam, class-weighted cross-entropy, mirror and brightness
  augmentation), post-training quantisation calibrated on the 99.9th
  percentile of each layer's output, and a numpy copy of the integer network
  that the C one is checked against on every run. Training data is every
  blob the colour table finds; the threshold is chosen by leaving one session
  out with the classifier's frame cost, running each held-out frame as stage
  3 runs in flight (the classifier, then the CNN on the `cnn_k` largest), with
  the `--max-lost` exclusion. Why numpy: RECOGNITION.md §10.
- **tcview**: Train (stage 3, behind a parent set that runs stage 2), Verify
  (stage 3 candidates), Firmware (installing a set with a CNN, the camera
  check), Image & calibration (the CNN's ids, `!thr3`, `!cnnk`).

### 12.2 Measured (PC)

- C suites pass; the viewer suite passes, with the new `test_cnn.py`
  (gradients within 1e-3 relative of finite differences; integer sums equal
  to `blob_cnn.c` on 30 random crops at 16 and 24; record refusals; stage 3
  through the pipeline).
- The firmware builds without warnings with and without a CNN compiled in.
- **First real training run** (behind `tabla_1_s3`, sessions `s1` and `s3`,
  205 crops of which 72 balloon, 16×16, 40 epochs): 23 s in all. The integer
  network decides like the float one on all 205 crops at the chosen threshold
  (largest confidence difference 0.034); 165 904 multiply-adds per crop,
  3 688 weight bytes, a 3 903 B record. Held out, the CNN **does not lower
  the cost** (49, as stage 2 alone): the chosen threshold, 0.10, keeps
  nearly everything. One fold trains on 49 crops, and the two sessions have
  different white-balance gains (§11.4).

### 12.3 Not established

- **The cost on the board.** Phase C0: `!cnnbench` gives µs per crop and
  cycles per multiply-add; the 16 vs 24 choice and `cnn_k` follow from it.
- Whether the CNN separates the balloons with enough sessions under equal
  gains. Nothing here says it does or does not.
- The temperature with stage 3 running for ten minutes or more.

---

## 13. Phase D: model records over the link (2026-10-29)

### 13.1 What was built

- **A flash partition of its own, `tcmodels`** (64 kB at 0x110000, after the
  app; `partitions.csv`), not NVS: the settings NVS partition is 24 kB, the
  colour table record alone is 8 kB, and NVS needs room for the new copy of
  a blob before it drops the old one. One slot per model (lut 16 kB, clf
  4 kB, cnn 16 kB), each a length then the record; an erased slot reads as
  empty, and the length is written last, so a transfer that stops halfway
  leaves a slot that reads as empty (`main/model_store.{c,h}`). The table
  keeps nvs, phy_init and the app where they were, so the saved settings
  survive the first flash with it.
- **Firmware**: `reload_models()` takes each model from its slot if it
  passes the same loader as a compiled-in record, else from the build, at
  boot and after every upload or unload (`#!CFG lut_src= clf_src= cnn_src=
  upload=`). `!dump <kind>` sends the running record as a `#!MODEL` packet
  (same framing and CRC as frames). `!upload <kind> <len> <crc>` erases the
  slot, answers `#!UPLOAD ready`, writes the raw bytes to flash as they
  arrive (256 B at a time; the UART receive buffer was raised to 1 kB),
  then checks the transfer CRC, the record and the table binding, and runs
  the model at once or refuses and erases. `!unload` erases slots.
- **tcview's Firmware tab**: back up the camera's models (`!dump` → a set
  `models/camera_<lut_id>_<date>/`, unless one holds those bytes), upload a
  set (backup first; table, classifier, CNN; unload uploaded kinds the set
  lacks; check `#!CFG`), unload. Install backs the camera's models up first
  when a camera is connected, which is the user's requirement that what the
  micro holds is never lost. Flash unloads uploaded models first: they
  survive a reflash and would shadow the models just built.

### 13.2 Measured (PC)

- The firmware builds without warnings with and without a classifier and a
  CNN compiled in; DRAM use 88 kB of 181 kB (49 %).
- The regenerated `sdkconfig` differs from the old one only in the partition
  table (checked with a diff).
- The new tests pass: the protocol at any chunking (and a damaged `#!MODEL`
  dropped), the backup kept once, the upload plan, and the whole tab against
  the fake camera on a pty (backup, upload, check, unload, a refused upload,
  unload before flash). The fake camera follows the firmware's sequence
  (ACK, ready, raw bytes, ok/refused).

### 13.3 Not established

- Nothing has run on the board: the partition, the upload at 115200 (8 212 B
  should take about 0.75 s), and whether the 1 kB receive buffer holds while
  flash writes run (reasoned: a 256 B write takes well under a millisecond).
