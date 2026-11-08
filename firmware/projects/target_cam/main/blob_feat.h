/*
 * blob_feat - what a blob looks like, as numbers and as a small picture.
 *
 * Stage 2 of the detector (tc_pipeline.h) is a classifier that looks at each
 * reported blob and says "balloon" or "not": the red door, the red chair and
 * the red shirt pass the colour test as well as the balloons do, and what
 * tells them apart is shape and surface, not colour. This file measures those
 * things. Phase B1 only collects them, with the operator's labels, so B2 has
 * data to learn from; nothing here decides anything yet.
 *
 * Two outputs per blob:
 *
 *   - BLOB_FEAT_N features (blob_features), cheap enough for the camera to
 *     compute on every reported blob of every frame. Each one has a reason
 *     to separate a mylar balloon from other red things; see the table in
 *     blob_feat.c.
 *   - a 24x24 RGB565 crop centred on the blob (blob_crop), for the later CNN
 *     (stage 3) and for the person labelling. 24 and not 16, because a 24
 *     crop can be shrunk to 16 later (blob_crop_down16) while a 16 crop
 *     cannot be grown back; which size the CNN uses is Phase C's question.
 *
 * Portable like blob_detect: no ESP-IDF, no heap. The same file runs on the
 * camera and, through libblob_detect.so, in the PC viewer and the training
 * scripts, so the features the classifier is trained on are computed by the
 * code that will compute them in flight.
 *
 * Cost is bounded by the blob, not the frame: the box is sampled on a grid
 * coarse enough to keep at most BLOB_FEAT_SAMPLES samples, so a blob filling
 * the frame costs the same as a 32x32 one.
 */
#pragma once

#include <stdint.h>

#include "blob_detect.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Bumped whenever a feature is added, removed or changes its definition. It
   travels with every collected record and into every dataset, so a classifier
   is never fed features computed by rules it was not trained on. */
#define BLOB_FEAT_VERSION   1
#define BLOB_FEAT_N         12
#define BLOB_FEAT_SAMPLES   1024    /* most box pixels one blob may cost      */

typedef struct {
    float v[BLOB_FEAT_N];
} blob_feat_t;

/* Short names, in v[] order: "fill", "log_aspect", ... They are what the
   dataset columns and the training report call the features. */
extern const char *const blob_feat_names[BLOB_FEAT_N];

/* Sampling stride for a w x h box: the smallest s with
   ceil(w/s)*ceil(h/s) <= BLOB_FEAT_SAMPLES. */
int blob_feat_step(int w, int h);

/* The features of one blob. `cfg` must be the configuration the blob was
   detected with (tc_stage_cfg's, so the colour table when stage >= 1): the
   blob's pixels are re-identified with blob_pixel_matches(). */
void blob_features(const blob_cfg_t *cfg, const uint8_t *frame, const blob_t *b,
                   blob_feat_t *out);

#define BLOB_CROP           24      /* crop side, pixels                       */
#define BLOB_CROP_SMALL     16      /* blob_crop_down16's side                 */
#define BLOB_CROP_MIN_SIDE  12      /* smallest source square                  */

/* The source square a crop is taken from: [xs, xs+side) x [ys, ys+side), in
   frame pixels, possibly partly outside the frame. */
typedef struct {
    int xs, ys, side;
} blob_crop_box_t;

/* side = max(1.5 x the longer side of the box, BLOB_CROP_MIN_SIDE), centred on
   the pixel holding the centroid. 1.5x keeps the blob's own edge and some of
   its surroundings in the picture; the floor keeps a tiny blob from becoming
   a crop of three magnified pixels. */
void blob_crop_box(const blob_t *b, blob_crop_box_t *out);

/* Resample the square to BLOB_CROP x BLOB_CROP RGB565 values (native pixel
   values, not bytes; byte order is cfg->big_endian's business on the way in
   and the caller's on the way out). Output pixel i covers source columns
   [xs + i*side/24, xs + (i+1)*side/24) (integer division; at least one column
   when side < 24), and the same for rows. Each channel (R5, G6, B5) is the
   rounded mean over that rectangle, pixels outside the frame counting as 0,
   so a blob at the edge gives a crop with a black border rather than a
   stretched one. Integer arithmetic only: the PC reproduces it exactly. */
void blob_crop(const blob_cfg_t *cfg, const uint8_t *frame, const blob_crop_box_t *box,
               uint16_t out[BLOB_CROP * BLOB_CROP]);

/* 24x24 -> 16x16 by exact area weighting: each output pixel covers 1.5 x 1.5
   input pixels, i.e. weights 2:1 or 1:2 along each axis, computed as the mean
   of the 3x3 samples in[(3i+a)/2][(3j+b)/2], a, b = 0..2. Per channel,
   rounded. */
void blob_crop_down16(const uint16_t in[BLOB_CROP * BLOB_CROP],
                      uint16_t out[BLOB_CROP_SMALL * BLOB_CROP_SMALL]);

#ifdef __cplusplus
}
#endif
