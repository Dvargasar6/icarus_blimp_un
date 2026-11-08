/*
 * blob_clf - the blob classifier of stage 2.
 *
 * Stage 1 decides pixel by pixel, on colour alone. Some of what it lets
 * through is not a balloon: a lamp or a window (clipped white, which the
 * table accepts because mylar highlights are white), a red patch on a larger
 * object, a piece split off the balloon's rim. Stage 2 looks at each reported
 * blob as a whole, through its BLOB_FEAT_N features (blob_feat.h: shape,
 * surface, context), and gives it a confidence that it is a balloon. Blobs
 * below the model's threshold are dropped from the report, so the blimp never
 * steers to them.
 *
 * The model is logistic regression on the standardised features:
 *
 *     z_i  = (x_i - mean_i) * scale_i          scale_i = 1 / sd_i, or 0
 *     conf = 1 / (1 + exp(-(bias + sum_i w_i z_i)))
 *
 * Why so small a model. The labelled set is a few hundred blobs from a few
 * sessions (learned-detector report §10.7), which is enough to fit 13 numbers
 * and not enough to fit a tree ensemble or a network without it learning the
 * sessions instead of the balloons. Every weight can be read off the .json
 * and argued with, and the camera's cost is 12 multiply-adds and one expf per
 * blob. A larger model is worth trying only once more sessions show that this
 * one is the limit, not the data.
 *
 * A classifier belongs to one colour table. The features are computed on the
 * pixels the table accepts (blob_features re-finds the blob's pixels with
 * it), so a different table gives different features for the same blob. The
 * record therefore carries the train id of the table it was trained behind,
 * and the caller must not use it behind any other (target_main.c reports
 * that as clf=mismatch and runs stage 1).
 *
 * Portable: no ESP-IDF, no heap.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "blob_feat.h"
#include "tc_pipeline.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BLOB_CLF_MAGIC      "TCBC"
#define BLOB_CLF_VERSION    1

/* Payload of a version 1 record, little-endian:
 *
 *   offset  size  field
 *        0     2  n_feat, must be BLOB_FEAT_N
 *        2     2  feature version, must be BLOB_FEAT_VERSION
 *        4     4  train id of the colour table it was trained behind
 *        8     4  threshold, float: a blob is kept when conf >= threshold
 *       12     4  bias, float
 *       16  4·N   mean[N], float
 *    16+4N  4·N   scale[N], float
 *    16+8N  4·N   w[N], float
 */
#define BLOB_CLF_PAYLOAD_BYTES  (16 + 12 * BLOB_FEAT_N)

typedef struct blob_clf {   /* named: tc_pipeline.h forward-declares it */
    uint32_t lut_id;            /* the colour table's train id                */
    float threshold;            /* keep a blob when conf >= threshold         */
    float bias;
    float mean[BLOB_FEAT_N];
    float scale[BLOB_FEAT_N];
    float w[BLOB_FEAT_N];
} blob_clf_t;

/* Check a model record (tc_pipeline.h) and unpack it into *out, which the
 * caller keeps for as long as the classifier is in use. Beyond the record
 * checks it refuses, with TC_MODEL_VERSION, a model for another feature
 * count or feature version (its weights would be applied to the wrong
 * numbers), and with TC_MODEL_VALUE one holding a non-finite number or a
 * threshold outside (0, 1). On any error *out is left untouched. */
tc_model_err_t blob_clf_load(const uint8_t *rec, size_t len, blob_clf_t *out,
                             uint32_t *train_id);

/* Confidence 0..1 that the blob with these features is a balloon. A
   non-finite feature gives 0: a blob the features cannot describe is not one
   the blimp should steer to. */
float blob_clf_conf(const blob_clf_t *clf, const blob_feat_t *f);

/* sizeof(blob_clf_t), for the PC binding to check its copy of the struct. */
size_t blob_clf_struct_bytes(void);

#ifdef __cplusplus
}
#endif
