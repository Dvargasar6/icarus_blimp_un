/*
 * color_lut - the learned colour table of stage 1.
 *
 * The HSV test of stage 0 accepts a box in hue/saturation/value space: one
 * hue window, two floors. The balloon does not fit a box. Its foil mirrors
 * the room, so beside its red it shows dark purple-blue reflections, a
 * shadowed side and white highlights, while brown wood sits just past red's
 * hue on the other side (docs/learned-detector-report.md §5). A window wide
 * enough for the first takes in the second.
 *
 * The table drops the box. An RGB565 pixel has only 65 536 possible values,
 * so the decision for every one of them can be stored outright: one bit per
 * value, 8 kB. The bits are learned on the PC from hand-labelled frames
 * (host/train/train_lut.py), and the set they mark can have any shape - red
 * and the purple reflections in, the orange-brown between them out.
 *
 * At run time it is one load and one shift per pixel, cheaper than the HSV
 * arithmetic it replaces. Bit px & 7 of byte px >> 3 belongs to RGB565 value
 * px, after the byte order is resolved: the table is about colours, not about
 * how the camera happens to send them.
 *
 * Portable: no ESP-IDF, no heap.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "blob_detect.h"
#include "tc_pipeline.h"

#ifdef __cplusplus
extern "C" {
#endif

#define COLOR_LUT_BYTES     8192            /* 65 536 colours, one bit each   */
#define COLOR_LUT_MAGIC     "TCLU"
#define COLOR_LUT_VERSION   1               /* payload: the 8192 bytes, bare  */

/* Check a model record (tc_pipeline.h) and copy its table into `bits`, which
   the caller keeps for as long as the table is in use. On any error `bits` is
   left untouched and the caller runs without stage 1. */
tc_model_err_t color_lut_load(const uint8_t *rec, size_t len,
                              uint8_t bits[COLOR_LUT_BYTES], uint32_t *train_id);

/* The table that reproduces cfg's HSV test exactly. Used by the host tests:
   stage 1 with this table must report what stage 0 does, bit for bit, which
   tests the plumbing apart from anything learned. */
void color_lut_from_hsv(const blob_cfg_t *cfg, uint8_t bits[COLOR_LUT_BYTES]);

/* Colours the table accepts, 0..65536. */
int color_lut_count(const uint8_t bits[COLOR_LUT_BYTES]);

#ifdef __cplusplus
}
#endif
