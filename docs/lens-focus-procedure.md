# Lens focus for target_cam: does it matter, and how to fix it with the tools we have

*2026-10-23. Analysis only; no code was changed. Everything below is either
taken from the bench pages cited at the end, read from this repository's code,
or reasoned. Nothing in it has been measured on the target_cam board yet.*

## 1. The question

The OV2640 modules for the forward camera look out of focus. The lens-sorting
bench of 2026-10-22 measured a sharpness spread from 80 (module E) to 899
(module B) within the same 58° lens type, an elevenfold range that it put down
to how far each barrel was screwed in at the factory.

The question here is whether target_cam (`firmware/projects/target_cam/`) and
its viewer `tcview` (`host/viewer/`) can be used, as they stand, to check and
correct focus on the three modules we would fly or keep as spares (for example
B, N and H), the same way the 180° rotation (`!flip`) and the white balance
(`Wc`) are now set per board. If an update turns out to be needed, it gets
planned separately.

**Answer:** yes, focus can be checked and corrected with the current firmware
and viewer. But the reasoning in §2 says the flight image may not show the
defocus at all, so the first step (§3) is a test that decides whether focusing
is worth doing.

## 2. The flight image is 160×120 *(reasoned)*

The sharpness figures on the bench were measured on 1600×1200 stills.
target_cam flies at QQVGA, 160×120 (`FRAME_SIZE FRAMESIZE_QQVGA`,
`main/target_main.c:58`). One flight pixel therefore spans 10 sensor pixels:
about 22 µm on the sensor instead of 2.2 µm.

Thin-lens defocus: the blur-disc diameter on the sensor is about
`b ≈ (f²/N) · |1/s − 1/d|`, with `f` the focal length, `N` the F-number, `s`
the distance the lens is focused at and `d` the object distance. Setting `b` to
one pixel gives the focus error, in dioptres (1/m), that first becomes visible.

| | 58° type (B, N) | 54° type (H) | narrow A |
|---|---|---|---|
| f, from the bench f(px) × 2.2 µm | 3.1 mm | 3.5 mm | 4.3 mm |
| Focus error that blurs **one 160×120 pixel** | ≈ 4.5 D | ≈ 3.6 D | ≈ 2.4 D |
| Same, at 1600×1200 | ≈ 0.45 D | ≈ 0.36 D | ≈ 0.24 D |
| Same, at 320×240 (if ever used) | ≈ 2.2 D | ≈ 1.8 D | ≈ 1.2 D |

Assumed: **F2.0**, common for these lenses but not verified. A slower lens
tolerates even more.

The balloons sit 2–5 m away, which is 0.2–0.5 D. So a lens can be clearly out
of focus at 1600×1200 and still be as sharp as it can ever look at 160×120. The
elevenfold sharpness spread may not exist in the image the detector sees. It is
also possible that a module like E is far enough off to matter. The numbers
cannot decide which, so §3 tests it.

A 160×120 frame shown at 4× zoom looks soft with any lens. **Do not judge focus
by eye in tcview.** Compare modules with the numeric readouts below.

## 3. Step 0: does focus matter in flight? (≈ 30 min)

This needs no change to any tool.

1. **Scene.** Fix the ESP32-CAM on a stand that does not move. Put a printed
   black-and-white bar chart about 1 m away, with bars 10–15 mm wide. At 1 m one
   flight pixel covers about 7 mm (58° type) or 6.3 mm (54° type), so the bars
   come out at about 1.5–2 px, the scale where blur shows most. Put a red
   balloon about 3 m away. Keep the same light throughout.
2. **Module E** (bench sharpness 80): fit it, `!flip 1` (it is a 58° type),
   `Wc` on a white card, then three times **Grab frame** and **Save frame**.
3. **Module B** (sharpness 899): fit it and repeat. N and H can be added the
   same way.
4. **Compare**, using readouts tcview already has:
   - **Region readout** (Image & calibration screen). Drag the cyan rectangle
     over the bar chart and read the value percentiles P10/P50/P90. Use
     `(P90 − P10) / (P90 + P10)` as the contrast figure. The ratio cancels the
     auto-exposure, which stays on.
   - **`edge` feature** (Blobs screen). With `!collect 1` and `g`, click the
     balloon's tile and read `edge`, the mean luma gradient along the blob's
     boundary (`main/blob_feat.c`). It measures sharpness on the real target,
     but it also depends on light and distance, so compare only within one
     scene.

**Reading the result:**

- **B and E read the same**: the detector cannot tell them apart, so focus is
  not limiting flight. Pick the module by field of view (the 58° type, per the
  simulation page) and stop here.
- **B is clearly higher**: focus matters at flight resolution. Go to §4.

## 4. Focusing procedure (only if Step 0 says it matters)

It works like the flip and white-balance steps, except that the adjustment is
mechanical. There is no focus setting in the sensor, the firmware does not
expose sharpening, and sharpening would not undo defocus anyway.

1. **Setup.** Board on the fixed stand, bar chart at 1 m, fixed white balance
   (`Wc`). Focusing at 1 m rather than the 2–5 m flight range costs about
   0.65 D, which is about 0.15 px at 160×120: negligible.
2. **Link.** Tick **Live**. At 115200 one frame takes about 3.4 s and the link
   is reliable. 460800 gives about 0.8 s per frame but has failed on the
   ESP32-CAM-MB before (`docs/learned-detector-report.md` §10.5). Either is
   slow, but focusing is turn, wait, read.
3. **Mark the barrel** with a pen line so the angle can be counted. Break the
   factory glue on the thread while holding the plastic lens holder, not the
   flex or the board: the holder is itself glued to the sensor.
4. **Turn in steps of about 15–20°.** After each step wait two frames and read
   the region contrast of §3. Go past the peak and come back to it. By §2,
   4.5 D on the 58° type is only about 44 µm of barrel travel, roughly 1/8 turn
   if the thread pitch is 0.35 mm (pitch not measured), so turn gently.
5. **Lock the barrel** with a dot of nail varnish or UV glue on the thread.
   **Do not use cyanoacrylate (superglue):** its fumes leave a white haze on the
   lens and the sensor.
6. **Redo the per-board steps, in this order:** `!flip`, `Wc`, `!save`. If
   the barrel moved a lot, check the field of view with `F<deg>` too, because
   refocusing shifts the focal length by up to about 1–2 %.

Repeat for each of the three modules.

## 5. Not established

- The lens F-number (F2.0 assumed in §2).
- The barrel thread pitch (0.35 mm assumed in §4).
- Whether the OV2640 averages or skips sensor pixels when it scales down to
  160×120. Skipping would make the flight image even less sensitive to blur
  than §2 says.
- Whether any module is far enough out of focus to show at 160×120. §3 is the
  test for that.

## 6. Other tools and possible updates

- The bench pages mention a `tools/lens_test` kit that takes 1600×1200 stills
  and scores them with a Laplacian sharpness. It is in the DroneUN repository,
  not this one, and it has not been reviewed here. It is the precise tool if
  focus is ever wanted at full resolution, but it needs its own firmware on the
  board and judges focus far more strictly than the flight image needs.
- If an update is needed, the plan would be small: a sharpness readout
  (Laplacian variance) in tcview's region panel, so §3 and §4 give a single
  number. It could also add a one-off higher-resolution grab, if target_cam ever
  moves to 320×240, where the tolerance halves to about 2 D.

## Sources

- *Fourteen OV2640 modules, three lenses* (Lens Sorting Results), bench of
  2026-10-22: <https://claude.ai/artifact/LqmWNonBoeGx3jmXmbDzrU>. Fields of
  view, focal lengths in pixels at 1600 wide, sharpness at 0.9 m, picture
  orientation per module.
- *Which lens should the blimp fly with?* (Simulated Lens Comparison),
  2026-10-22: <https://claude.ai/artifact/Y75oCFTbvaKpKjDrE3bsvM>. The 58×45°
  type completes every simulated loop with the ToF fitted; narrower lenses lose
  loops.
- This repository: `firmware/projects/target_cam/README.md` (commands, white
  balance, `!flip`), `host/viewer/README.md` (region readout, Live, Blobs
  screen), `main/blob_feat.c` (the `edge` feature),
  `docs/learned-detector-report.md` §10.5–10.6 (link speeds on the
  ESP32-CAM-MB).
