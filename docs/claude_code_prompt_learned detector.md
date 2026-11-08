# Task: plan the "learned detector" upgrade for target_cam (ESP32-CAM)

Work in PLAN MODE first. Read the repository, then propose a phased plan. Do not modify code until I approve the plan. After approval, implement one phase at a time and stop after each phase so I can test it on the board.

## Context

- Firmware `target_cam` runs on an AI-Thinker ESP32-CAM (ESP32 LX6, 2 cores, 240 MHz, no SIMD, OV2640, 4 MB PSRAM). Branch: `target-cam-balloon-detector`. Create a new branch from it for this work.
- It detects red mylar waypoint balloons with an HSV colour-blob detector (`blob_detect.c`): 160×120 RGB565 frame → pixel test (HSV window) → 2×2 cells → union-find blobs → 4 largest blobs (centroid, box, fill) → range = diameter_mm × f ÷ apparent size px → 25-column CSV row over serial.
- `tcview` is the PC viewer: it shows the overlay using the camera's own C code through a shared library (ctypes), and has "Save frame…".
- Measured today: 12.5 FPS (80 ms pace, commit af8f6ce), dt_ms 80.0 on every row, detection 7.3–8.2 ms, loop busy ≈ 21 ms per frame (detection + blocking CSV print). Only 115200 baud works on the ESP32-CAM-MB.
- Observed problems: (1) missed detections, because mylar colour does not fit one HSV window (balloon centre measured H304 ± 109°, S78, V188); (2) false detections from clutter that passes the colour test (wood, skin, pink walls, red objects), which appear as sudden jumps in the output. Small per-frame jitter is accepted as sensor noise; it is out of scope.

## Hard constraints (do not change)

- Resolution stays 160×120 RGB565. Baud stays 115200.
- 12.5 FPS must be kept in normal operation: dt_ms = 80.0 on every row with every stage enabled. A data-collection mode may run slower, but only while it is explicitly enabled.
- Project rules:
  - Detector code is portable C next to `blob_detect.c`, tested on the host with recorded frames. Use no TensorFlow Lite Micro.
  - Fail honestly: `ok=0` means "no measurement". Low confidence means the blob is not reported, never a guessed position.
  - The tcview overlay is the camera's own code: expose every new stage through the shared library so tcview shows the camera's verdict.
  - The CSV format is versioned. Any new column bumps the version, and tcview plus the future ESP32-S3 reader are updated together.
  - Settings and models are stored as versioned NVS records with version and CRC. On mismatch, reject the record and fall back to HSV.
  - Flash writes need whole words (`!save`). Any model upload needs its own guarded command with a CRC.

## What to build

Three stages, each applied on top of the previous one and enabled in order with a single runtime command (for example `!stage N`, stored in NVS):

| Level | Pipeline |
|---|---|
| 0 | Current HSV detector (always available as fallback) |
| 1 | **A** · Learned colour table replaces the HSV pixel test: 65,536-bit table (8 KB), one bit per RGB565 value, built on the PC |
| 2 | A + **B** · Classifier on blob features. Logistic regression or 2-layer MLP on ~10 features per blob: fill ÷ box, aspect w/h, hue/sat spread inside the blob, bright low-saturation highlight present, edge sharpness, vertical position. Outputs a confidence per blob, plus a threshold. Computes extra features during the existing pixel pass. |
| 3 | A + B + **C** · Small int8 CNN on crops of the top 2 blobs (start at 16×16, allow 24×24): conv 3×3 3→8, pool, conv 3→8→16, pool, conv 16→16, global average, dense → 1 confidence. About 166 k multiply-adds per crop at 16×16, about 373 k at 24×24. |

Also add:

1. **Timing per stage** in the CSV (detection, A, B, C in µs or 0.1 ms) and a confidence column, so I can verify the 80 ms budget at every level. Include a simple PC check (in tcview or a script) that reports FPS and the count of rows where dt_ms ≠ 80.
2. **Data collection for training**, enabled only on command:
   - For A: full frames (the existing Save frame path), plus a labelling tool in tcview to mark balloon and background regions and export the labelled pixels.
   - For B and C: per-frame records of the top blobs with their features and a margin crop (24×24 RGB565, about 1 kB, about 0.1 s each at 115200), plus a tcview tool to label each blob as balloon or not-balloon. Every record carries the CSV/firmware version.
3. **PC training scripts (Python)**, one per stage, that turn the labelled data into firmware-ready artifacts: an 8 KB LUT for A, weights for B, and int8 weights for C. Output C headers first (compile-in), and plan the NVS upload command as a later phase.
4. **Host tests** for each stage with recorded frames, and checks that a missing or bad model falls back to the previous level.

## Plan requirements

- Phases ordered A → B → C. Each phase must be small, testable on the board, and end with a measurable check: FPS, stage times, and detections versus the hand-labelled frames.
- For each phase, list the files to touch, the new commands, the CSV changes, and the risks (heat, flash/RAM use, venue retraining time).
- Measure the real cycles per int8 multiply-add on the board before committing to C's crop size. The 4–10 cycles figure is only an estimate.
- When you give me commands to run, give at most 3 per step, then wait for my result.
- Explain the reasoning behind each design decision briefly. This is an academic project, and understanding matters as much as the result.
