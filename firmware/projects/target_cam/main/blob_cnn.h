/*
 * blob_cnn - the crop CNN of stage 3.
 *
 * Stage 2 judges a blob from 12 numbers (blob_feat.h); some false blobs have
 * balloon-like numbers and still do not look like a balloon. Stage 3 looks at
 * the blob's crop itself (blob_crop(), 24x24, or its exact 16x16 reduction)
 * with a small int8 network and gives a second confidence:
 *
 *   input   N x N x 3, the crop's 8-bit R, G, B          N = 16 or 24
 *   conv 3x3, 3 -> C1, ReLU        N   x N
 *   max-pool 2x2                   N/2
 *   conv 3x3, C1 -> C2, ReLU       N/2
 *   max-pool 2x2                   N/4
 *   conv 3x3, C2 -> C3, ReLU       N/4
 *   global average                 C3
 *   dense C3 -> 1, logistic        confidence
 *
 * The shape is the one the feasibility analysis costed (docs/artifacts/
 * Balloon_NN_Feasibility.md §06): C1 8, C2 16, C3 16 is about 166 k
 * multiply-adds at N = 16 and 373 k at N = 24, 3.7 k weights. Plain C, no
 * TensorFlow Lite Micro: there is no SIMD on this ESP32 to make a runtime
 * worth its size, and this file runs unchanged in the host tests and in the
 * PC viewer, so the camera's verdict is the PC's.
 *
 * Integer all the way to the logit, so the PC reproduces it bit for bit:
 *   - activations are uint8 (0..255); the input is the crop's 8-bit channels
 *     (bit replication, as blob_detect expands RGB565);
 *   - weights int8, one scale per layer; biases int32 in the accumulator's
 *     scale;
 *   - after a conv, ReLU, then y = min(255, (acc * M + 2^(s-1)) >> s) with a
 *     per-layer multiplier M and shift s (only acc > 0 gets there, so the
 *     shift never sees a negative number);
 *   - pooling is a max; the global average rounds half up;
 *   - the dense layer's int32 sum times one float scale is the logit, and
 *     only the final logistic is float (a ulp apart on two machines at most).
 *
 * A CNN belongs to one colour table, as a classifier does: the table decides
 * which blobs exist and where their boxes are, so a CNN trained on one table's
 * crops is not used behind another (the record carries the table's train id).
 *
 * Portable: no ESP-IDF, no heap. The activations live in static buffers here,
 * sized for the largest network, so blob_cnn_conf() is not reentrant: the
 * camera calls it from one task, the PC from one thread at a time.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "blob_feat.h"
#include "tc_pipeline.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BLOB_CNN_MAGIC      "TCCN"
#define BLOB_CNN_VERSION    1
#define BLOB_CNN_MAX_C      16      /* channels per layer, at most            */

/* Payload of a version 1 record, little-endian, no padding:
 *
 *   offset  size  field
 *        0     1  N, the input side: 16 or 24
 *        1     3  C1, C2, C3 (1..BLOB_CNN_MAX_C)
 *        4     4  train id of the colour table it was trained behind
 *        8     4  threshold, float: a blob is kept when conf >= threshold
 *       12     4  logit scale, float: logit = dense sum * scale
 *       16     -  three conv layers, each: int8 w[Cout][Cin][3][3],
 *                 int32 bias[Cout], int32 M, uint8 shift (Cin of the first is 3)
 *        -     -  dense: int8 w[C3], int32 bias
 */

typedef struct {
    int     cin, cout;
    int8_t  w[BLOB_CNN_MAX_C * BLOB_CNN_MAX_C * 9];
    int32_t bias[BLOB_CNN_MAX_C];
    int32_t mult;               /* M > 0                                      */
    int     shift;              /* s, 1..62                                   */
} blob_cnn_conv_t;

typedef struct blob_cnn {   /* named: tc_pipeline.h forward-declares it */
    uint32_t lut_id;
    float   threshold;
    float   logit_scale;
    int     n;                  /* input side, 16 or 24                       */
    blob_cnn_conv_t conv[3];
    int8_t  dense_w[BLOB_CNN_MAX_C];
    int32_t dense_bias;
} blob_cnn_t;

/* Payload bytes of a network of this shape. */
size_t blob_cnn_payload_bytes(int c1, int c2, int c3);

/* Check a record and unpack it into *out (left untouched on any error).
   Beyond the record checks: TC_MODEL_SIZE for a payload that is not the
   size its shape needs, TC_MODEL_VALUE for an input side other than 16 or
   24, a channel count out of range, a multiplier or shift that cannot be
   applied, or a threshold outside (0, 1) or non-finite scale. */
tc_model_err_t blob_cnn_load(const uint8_t *rec, size_t len, blob_cnn_t *out,
                             uint32_t *train_id);

/* The dense layer's integer sum for one crop: N*N native RGB565 values, row
   by row (blob_crop()'s output for N = 24, blob_crop_down16()'s for 16). */
int32_t blob_cnn_sum(const blob_cnn_t *cnn, const uint16_t *crop);

/* Confidence 0..1 that the crop is a balloon: the logistic of
   blob_cnn_sum() * logit_scale. */
float blob_cnn_conf(const blob_cnn_t *cnn, const uint16_t *crop);

/* Multiply-adds of one crop, for the cost check (!cnnbench). */
long blob_cnn_macs(const blob_cnn_t *cnn);

/* sizeof(blob_cnn_t), for the PC binding to check its copy of the struct. */
size_t blob_cnn_struct_bytes(void);

#ifdef __cplusplus
}
#endif
