# tcview — target_cam viewer and hand calibration

A PC program that shows what the ESP32-CAM sees and what it says, so the colour
detector can be calibrated by hand. Everything goes over the same USB serial
cable used for flashing.

Four screens:

| Screen | Link | What it shows | Same as flight? |
|---|---|---|---|
| **Image & calibration** | 115200 (at most 460800: faster fails on the ESP32-CAM-MB), image on | The camera's raw RGB565 pixels at ×4, a mask overlay, sliders, and pixel/region colour readouts | **No.** Sending a 38 kB frame blocks the camera's loop, so `dt_ms` and FPS are not representative. The screen says so. |
| **Flight data** | 115200, image off | Only the CSV rows, as a dashboard: target status, the 4 blobs, a field-of-view plot, 30 s plots, link health, and the last raw line | **Yes**, when the green FLIGHT-IDENTICAL banner is shown. The banner is driven by the camera's own `#!CFG` report, not by what the viewer asked for. |
| **Label frames** | None needed | Saved frames, painted by hand as balloon / background, with the detector's boxes and its score against the labels | Not applicable: it works on files. |
| **Blobs (classifier data)** | 115200, `!collect 1`, `g` for frames | One 24×24 crop per blob the camera reported, with its 12 features, labelled by hand; plus blobs from labelled frames; saved as a dataset for the stage 2 classifier | **No.** Records slow the loop like frames do. |

The overlay and the PC-side boxes are computed by the **same C file the camera
runs** (`main/blob_detect.c`, built as `host/libblob_detect.so` and loaded
with ctypes), so what you see on the PC is the camera's rule, pixel for
pixel. The library is rebuilt automatically whenever the C source is newer.

## Setup (once)

A dedicated conda env, kept separate from `blimp-tools`:

```bash
~/miniconda3/condabin/conda create -n targetcam python=3.10 -y
# BEFORE any pip install: keep user-site packages and ROS out of this env
~/miniconda3/condabin/conda env config vars set PYTHONNOUSERSITE=1 PYTHONPATH= -n targetcam
conda activate targetcam
pip install -r requirements.txt
```

- **`PYTHONPATH=` is deliberate.** ROS Humble's setup puts its own
  site-packages on `PYTHONPATH`, and its `launch_testing` pytest plugin then
  breaks `pytest` in this env. Blanking it inside the env fixes that without
  touching your ROS shell. Conda prints "overwriting environment variables set
  in the machine" on each activation; that warning is expected.
- **Qt on Linux:** Qt 6.5+ may need `sudo apt install libxcb-cursor0`. The
  symptom is an error about the `xcb` platform plugin.
- The viewer needs a C compiler (`cc`) to build `libblob_detect.so`. You can
  also build it by hand with `make -C .. lib`.
- Your user needs access to the serial port (the `dialout` group).

## Running

```bash
conda activate targetcam
cd firmware/projects/target_cam/host/viewer
python -m tcview --port /dev/ttyACM0     # the real camera, through the ESP32-CAM-MB
python -m tcview --fake                  # a synthetic camera; no hardware needed
python -m tcview --label [DIR]           # label saved frames only; no camera (default DIR: target_cam/data)
```

Opening the port does **not** reset the camera: the viewer lowers DTR and RTS
before it opens the port. The camera always boots at 115200, and the viewer
always starts at 115200.

`--fake` runs a simulated camera on a pseudo-terminal: a red disc that wanders
in front of a wood patch and a grey wall. It runs through the real C detector
and speaks the full protocol, including baud changes and the 3 s safety
revert. `--fake-corrupt N` injects a log line into every Nth frame, so you can
see a dropped frame and the resync.

## Image & calibration screen

1. Press **Grab frame** for one image, or tick **Live** for one per camera
   loop. At 115200 a frame takes about 3.4 s on the wire; pick a faster speed
   in the baud menu and press **Set**. The viewer and the camera switch
   together and confirm with `k`. If the camera hears nothing valid at the new
   speed within 3 s, it falls back to 115200 by itself, and so does the viewer.
   A bad speed can never lose the board.
2. **Hover** over the image to read x, y, RGB, H/S/V and whether that pixel
   matches.
3. **Drag the cyan rectangle** over the balloon. The panel shows that region's
   hue, spread, saturation and value (with percentiles) and a hue histogram.
   **Use region as target** fills the sliders with the same rule as the
   camera's `c` command (tol = 3·spread clamped to 10–40, sat ≥ max(40, S/2),
   val ≥ max(25, 2V/5)). It refuses a region below saturation 70, as `c` does.
4. Move the **sliders**. The overlay is recomputed on the frozen frame
   immediately. The value goes to the camera when you release the slider (or
   after 400 ms of no movement). Each control's chip reads:
   - **not sent**: you changed it on the PC only;
   - **sending**: the command is on its way;
   - **applied**: the camera's `#!CFG` reports this value.
5. The overlay can be switched between a tint, the bare mask, or off. Boxes are
   the PC's detection on this frame. Crosses are the blobs from the camera's own
   CSV row for the same frame (paired by `t_ms`). They should coincide.
6. **Save to camera** writes the live settings to flash (`!save`). The camera
   boots with them from then on. **Reload saved** (`!load`) and **Factory defaults**
   (`!reset`, which asks first) are next to it.
7. **Save frame...** writes a `.png`, the raw `.rgb565` bytes and the settings as
   `.json` to `target_cam/data/`, which is enough to reproduce a case in the
   host test suite.
8. **Rotate 180°**, next to Save frame, is for a board whose image arrives
   upside down. It sends `!flip 1` (and `!flip 0` to undo), so the camera turns
   the image in the sensor: detection, az/el, saved frames and blob crops come
   out the right way up, not just the picture on screen. **Save to camera**
   keeps it. The button stays greyed out with a firmware that has no `!flip`.

Other controls: the preset menu, white balance (Auto / Frozen / Fixed, the
fixed R G B gains, and *From white card*, which sends `Wc`), the RGB565 byte order, and
`c` (calibrate on the centre 16×16 patch, shown dashed on the image).

**The Detector section shows the calibration of the chosen stage only.** Pick
the stage in *Detector stage* (it follows the camera's, and *Set* sends
`!stage N`):

| Stage | What is shown |
|---|---|
| 0 | The HSV window: preset, hue bar, hue, tolerance, min sat, min value, `c`, and the Region section |
| 1 | The colour table: its id on the PC and on the camera (the HSV settings are kept for stage 0, hidden) |
| 2 | Also the blob classifier (PC and camera ids, the threshold it runs with) and its **Threshold** control, `!thr2` in hundredths, 0 = the model's own. The PC boxes run at the threshold the control shows |
| 3 | Also the crop CNN (PC and camera ids, its running threshold), its **Threshold** (`!thr3`) and **Blobs checked** (`!cnnk`), and *Measure the CNN on the camera* (`!cnnbench`: time per crop, cycles per multiply-add, and the share of the frame period with that many blobs checked) |

To collect data for a stage, run the one below it: blobs for a classifier
are collected at stage 1 behind the table it will be trained with.

**Zoom** (here and on Label frames): **−** / **+** step through Fit ×1.25 …
×8, **Fit** goes back; Ctrl+wheel does the same about the cursor. The scale is
rounded to whole screen pixels, so every camera pixel is the same size, and a
step that would not change it is skipped. Fit never shrinks the image below
1:1; on a screen too small for that, it scrolls.

## Flight data screen

Entering it sends `v0` (image off) and `B115200`, then `k`. It shows:

- **TARGET / NO TARGET** in large type. `ok=0` means no measurement, not
  "dead ahead".
- A table of up to 4 blobs: az, el, range, plus area, w×h, fill and clipped for
  the largest.
- A field-of-view plot: the ±h-FOV/2 × ±v-FOV/2 rectangle, with each blob drawn
  to its apparent size.
- Rolling 30 s plots of az, el and range.
- Link health: rows/s, `dt_ms` mean and jitter, `proc_us`, ok %, and bytes/s.
  The last raw CSV line is shown verbatim.
- **Record**: writes the rows verbatim, with a header, to
  `target_cam/data/flight_<date>_<time>.csv`.

The bottom console shows every human `#` line from the camera. It accepts any
raw command, for example `?`, `m`, `H350` or `p red`. **Show CSV rows** echoes
the stream there.

## Label frames screen

Hand labels are the ground truth the learned detector is trained on and scored
against. One labelling pass gives both: the balloon and background pixels the
colour table (stage 1) learns from, and the balloons `host/tools/eval_frames.py`
counts hits and misses on.

Labels are written next to each frame as `<stem>.labels.png`, 8-bit greyscale
at the frame's size: 0 unlabelled, 1 balloon, 2 background. "Unlabelled" is a
class on purpose. A balloon's rim is a mix of balloon and wall colour, and
forcing it into either class would teach the colour table something false. A
balloon, for scoring, is an 8-connected component of label 1 (4 px or more)
with its bounding box.

The quick way through a frame:

1. **Ellipse** tool (the default), **Balloon** class: drag a box around the
   balloon. Fix the edge with the **Brush**. The right mouse button erases
   with the current tool. With **Fill interior** (F) ticked, a brush stroke
   drawn round a region paints its inside too: the stroke is closed from its
   end back to its start, so the ends need not meet. One Undo takes back the
   whole stroke.
2. **Rest -> background**: every unlabelled pixel becomes background, except a
   band (2 px by default) around the balloon, which stays unlabelled.
3. Check the result and move on with **PgDown**. Labels are saved when you
   change frame, with **Save** (Ctrl+S), and when the window closes.

A frame with no balloon in it is labelled by step 2 alone. Those frames are
what measure false detections. Label the mylar's white highlights as balloon:
whether that colour should match is the training's decision, made from how
often it is balloon versus background.

Keys: 1 balloon, 2 background, 0 erase, B/R/E brush/rectangle/ellipse, F fill interior, Ctrl+Z
undo (50 steps), PgDown/PgUp next/previous frame. The list marks labelled
frames `[x]`. Subfolders are searched, so keep one subfolder per capture
session. The training uses that to split train and validation by session.

Below the image, the detector's result on the frame is drawn (yellow box =
blob 1). This uses the camera settings saved in the frame's `.json`, through
the same library. Under it is the live score against the labels: the same
TP / FN / FP / split rule and pixel precision/recall as `eval_frames.py`,
because both import `tcview/labels.py`.

## Blobs screen

The data for stage 2, the blob classifier (Phase B). Each tile is one blob:
its 24×24 crop at ×3, framed grey (unlabelled, "?"), green (balloon) or red
(not a balloon). Click a tile to see its crop at ×8 and its 12 features.

- **Collect from camera** sends `!collect 1`; the button follows the camera's
  `#!CFG collect=`, not the click. Every 4th frame, and every frame it
  sends to the PC (`g`, `v1`), the camera then sends one `#!BLOB` record per
  reported blob. They arrive unlabelled. 115200 is enough: about 5 blobs/s
  with no image.
- **Add from labelled frames…** runs the camera's pipeline (stage 1) on every
  saved frame under a folder that has a `.labels.png`, and labels each blob
  from the paint: balloon if its centroid is in a painted balloon's box, not
  a balloon otherwise. A frame already in the set is not added twice.
- **Label**: select tiles (click, shift/ctrl-click, or drag) and press **B**
  (balloon), **N** (not) or **U** (unlabelled). A double-click cycles one tile.
- **Save dataset…** writes every sample, labelled or not, as `.npz`
  (`tcview/blobset.py` documents the arrays; `np.load(allow_pickle=False)`
  reads it). **Load dataset…** appends one. If the window closes with unsaved
  samples, they are written to `target_cam/data/blobs/blobs_autosave_*.npz`
  rather than lost.
- Each live sample also keeps the camera's colour table id (`lut_id` from
  the last `#!CFG`; the `#!BLOB` header does not carry it). A classifier is
  valid behind one table only, so `train_clf.py` uses a set to train it only
  if its blobs were found behind that table. For a set meant for training,
  collect at `!stage 1` with the table flashed.

**The agreement line is the board check.** The classifier will be trained on
features the PC computes and run on features the camera computes, so the two
must be the same numbers. With frames on as well (`v1`, or `g` for one), every
record whose frame arrived is re-run on the PC through the same C code and
compared: features to the 6 significant digits the record carries, crops bit
for bit. Green "board = PC" means every checked record agreed; red names the
worst feature and the size of the difference.

`host/tools/blobs_from_frames.py DIR... -o out.npz` builds the same dataset
from labelled frames without the GUI.

## Train screen

Trains the learned stages into a new **model set**, `target_cam/models/<name>/`
(see `models/README.md`). Stage 0 is not here: it is calibrated by hand on
Image & calibration.

- **Training data** is a tree of `target_cam/data/` (*Open…* for another
  folder). A checked folder is **one capture session**, with every frame below
  it, so check `data/train/s3`, not its colour subfolders: holding out one
  colour of one session is not a held-out session. Checking a folder unchecks
  the ones above and below it. Folders without labelled frames cannot be
  checked. Blob sets (`.npz`) can be checked at stage 2 only. The **White
  balance** column and the line under the tree warn when the checked sessions
  mix gains: their colours are not comparable.
- **Stage 1** trains a colour table (`train_lut.py`). **Stage 2** trains a blob
  classifier behind the **parent model set**'s table (`train_clf.py`): the new
  set gets a copy of that table plus the classifier, and the parent is not
  changed. **Stage 3** trains the crop CNN (`train_cnn.py`) behind a parent set
  that runs stage 2: the new set gets copies of its table and classifier plus
  the CNN. Its parameters are the input size (16 or 24), the epochs, the blobs
  checked per frame (`cnn_k`) and, when fixed, the threshold. It takes minutes.
- **Parameters**: chosen by leaving one session out (two or more sessions), or
  fixed. *max balloons lost* (stage 2) excludes settings that lose more than
  that fraction of stage 1's balloons on held-out frames.
- **Output**: the set name (empty = the grey suggestion), and the exact
  command, which can be copied and run by hand from `target_cam/`.
- **Train** runs the trainer as a separate process, its output below and saved
  in the set (`train_log.txt`, `clf_train_log.txt`). The banner is read from the
  `.json` the trainer wrote: red if the chosen setting puts a false blob 1 on a
  held-out session (do not fly it), amber if it loses balloons or the trainer
  warned, green otherwise. A failed run leaves no folder behind.
  *Verify this set →* opens it in the Verify tab, against its table alone.

The firmware's set (`main/models/`) is never written here; installing a set is
the Firmware tab's job.

## Verify screen

Scores one model, or compares two, on labelled test folders, with the numbers
`eval_frames.py` gives (same functions, same C library).

- **Model A** (and, with *Compare with model B*, **Model B**): a model set and
  one of its stages, or *stage 0 · HSV*, which scores each frame with the
  settings it was saved with. A set trained for stage 2 holds a copy of its
  table, so the same set at stage 1 and at stage 2 compares **the table alone
  with the table plus its classifier**. Train's *Verify this set →* opens
  exactly that.
- **Test data**: the same folder tree as Train. Check folders the models were
  not trained on; when one was, the verdict says its figures are optimistic.
- **Results**: the comparison's verdict, then A's and B's own (README step 4:
  every balloon found and no false blob 1 on a frame without a balloon), the
  figures side by side, and one row per session. From stage 2 up the pixel
  figures do not change (the classifier removes blobs, not pixels); compare
  the blob figures. **Cannot conclude** means one figure improves and another
  gets worse, or the difference is one frame of a small set. *Copy report*
  copies all of it as text.
- **Frames**: every frame with its outcome (`ok`, `partial`, `false`,
  `missed`, `false b1`), A's and B's, filterable (All, Failing, Passing,
  A ≠ B). The selected frame is drawn with A's or B's result: the stage's mask
  (yellow), the labelled balloons (dotted cyan), and the reported blobs, green
  in a balloon and red outside one, numbered as the camera reports them, with
  the classifier's confidence from stage 2.

## Firmware screen

Puts a model set on the camera and checks that it is there. It is the only
part of the viewer that writes `main/models/`.

- **Installed now**: what `main/models/` holds (table and classifier ids, and
  the `models/` set with the same bytes, if any), whether the last build
  (`build/target_cam.bin`) contains those records, and what the connected
  camera's `#!CFG` reports against them.
- **1 · Install a model set**: choose a set from `models/` and *Install*. The
  current `main/models/` is first moved into `models/firmware_<lut_id>_<date>`
  (skipped when a set there already holds the same bytes), so it can be
  installed back. A set without a classifier puts the "no classifier"
  placeholder back. The camera's own record checks run first: a set the
  firmware would refuse is not installed.
- **Camera models (no flashing)** (phase D firmware): *Back up the camera's
  models* reads back what the camera runs (`!dump`) and keeps it as
  `models/camera_<lut_id>_<date>/`, unless a set already holds those bytes.
  *Upload the chosen set* backs up first, then sends the set's records with
  `!upload` (table, classifier, CNN; and `!unload`s uploaded kinds the set
  lacks), and checks `#!CFG`: the set runs at once, no rebuild, no BOOT
  button, and stays across power cycles and reflashes. *Back to the build's
  models* sends `!unload all`. **Install** also backs the camera's models up
  first when one is connected, and **Flash…** unloads uploaded models first,
  since they would otherwise shadow the build's.
- **2 · Build** warns when `sdkconfig` predates `sdkconfig.defaults` (idf.py
  applies the defaults only when it creates `sdkconfig`, so an old one keeps
  the partition table without `tcmodels`); *Regenerate sdkconfig* moves it to
  `sdkconfig.old` and the next build makes it again.
- **2 · Build**: `idf.py build` through the ESP-IDF activation script, found
  as `~/.espressif/tools/activate_idf_v*.sh` (or give its path).
- **3 · Flash and check** walks through the ESP32-CAM-MB's BOOT button:
  the viewer lets go of the port; you hold BOOT, replug the USB and keep
  holding, then click *BOOT held — flash now* (esptool at 115200, no
  resets); then release BOOT, replug, and click *Reconnect and check*. The
  viewer reconnects and compares the camera's `lut_id`, `clf` and `clf_id`
  with what was installed. To use a classifier afterwards: `!stage 2`, then
  `!save`.

## Protocol (firmware ↔ viewer)

The text protocol stays readable in a plain terminal and in `target_monitor.py`.
The machine lines all start with `#!`:

| Line | Meaning |
|---|---|
| `#!ACK <cmd>` / `#!NAK <cmd> -- <reason>` | Every command line gets one or the other, so a command garbled on the wire is caught instead of assumed. The viewer retries once. |
| `#!CFG w= h= cell= hue= tol= sat= val= area= mm= fov= fps= period_ms= awb= wb= wb_gain= wb_fixed= be= preset= baud= image= stream= settings=saved\|defaults\|modified` | The camera's live state. It is printed after every change and on `k`. `fps` can be fractional (`12.5`). `period_ms` is what the camera's loop actually waits on. |
| `#!CAL ok\|refused hue= spread= sat= val=` | The result of `c`. |
| `#!WBCAL ok\|refused\|cancelled r= g= b= R= G= B= steps=` / `reason=` | The end of `Wc`: the gains and the patch's last R, G, B means, or why it was refused. |
| `#!WB mode= ctrl1= c7= cc= cd= ce=` | `W`: the sensor's white-balance registers, as read back. |
| `#!BAUD <n>` | Printed just before the camera changes speed (and on the 3 s revert). |
| `#!FRAME seq= t_ms= w=160 h=120 fmt=rgb565be len=38400 crc=<hex>` | Followed by exactly `len` raw bytes, then `\n#!END seq=`. The CSV row for the same frame comes next. |
| `#!BLOB seq= t_ms= k= n= stage= csv= fv= fw= c= area= box= crop= conf= feat= wb= wb_gain= cw=24 fmt=rgb565le len=1152 crc=<hex>` | `!collect 1`: blob *k* of the row with the same `t_ms`, after that row. 1152 bytes of 24×24 crop, then `\n#!END seq=`. Framed and checked like a frame; frames and records share `seq`. The format is in the target_cam README. |

- The payload is written with `uart_write_bytes()`, bypassing the console's
  `\n`→`\r\n` translation.
- The CRC is CRC-32 (zlib's), checked against the firmware with the
  `"123456789"` → `CBF43926` vector that `link_init()` prints at boot.
- A log line from another task can land inside a frame. The CRC catches it: the
  viewer drops that frame and resyncs on the next `#!FRAME` or `#!BLOB`. The
  status bar counts frames and blob records, ok and bad, separately.
- A truncated frame (the camera reset mid-transfer) times out. Its bytes are
  re-read as text, so the boot log after it is not lost.
- The commands that touch flash are whole words (`!save`, `!load`, `!reset`).
  No single corrupted byte can write to flash. On the bench, one burst turned
  `m` into `W`.

## Tests (no hardware)

```bash
cd firmware/projects/target_cam/host/viewer
python -m pytest -q
```

- **`test_protocol.py`:** the stream splitter on mixed text, CSV and binary
  frames cut at random read boundaries. It also covers CRC parity with the
  firmware, a log line injected mid-frame, truncated frames, bad headers and
  binary noise; and `#!BLOB` records: round trip, a broken record (a BLOB
  error, not a frame error) and frame + row + records at any chunking.
- **`test_detector.py`:** the ctypes binding against the C suite's synthetic
  disc. `blob_mask` agrees with `blob_pixel_matches`, and the sampling and
  calibration rule match `c`.
- **`test_gui_fake.py`:** a whole bench session against the fake camera,
  offscreen: grab a frame and pair it with its row, a control applied only on
  the camera's `#!CFG`, a NAK, "use region as target", a switch to 921600 with
  live frames and some corrupted, `!save`, and the flight screen returning to
  FLIGHT-IDENTICAL. A second test checks the fake's own 3 s baud revert.
- **`test_labels.py`:** the label file round trip, connected components
  (including the diagonal case and the camera's centroid convention), the
  rest-to-background band, the scoring rules, `eval_frames.py` on a synthetic
  set, and the Label tab: paint, save on frame change, reload, undo.
- **`test_blobfeat.py`:** the crop and its 16×16 reduction against a numpy
  re-implementation, bit for bit, at the frame edges and both byte orders; a
  disc's features; pipeline features equal `blob_features()`; the dataset's
  auto-labels, `.npz` round trip and version check; `blobs_from_frames.py`;
  and the agreement check catching a changed crop and feature.
- **`test_gui_fake.py`** also collects from the fake camera at 115200 with
  `g`, as on the bench: every record must agree with the PC, then labels and
  saves. And it makes the fake's output noise after a switch to 460800 with
  image on (the 2026-10-17 hang): the viewer must ask the camera down to
  115200 and both must end up there, talking.
