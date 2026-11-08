/*
 * tc_pipeline - the detector as a stack of stages, one entry point.
 *
 *   stage 0   HSV colour test + blobs (blob_detect), always available
 *   stage 1   learned colour table in place of the HSV test        (A)
 *   stage 2   1 + a classifier on each blob's features            (B)
 *   stage 3   2 + a small int8 CNN on crops of the top blobs       (C)
 *
 * Why one function for all of it: the camera calls it, and so does the PC
 * viewer through the same source compiled as a shared library. If the stages
 * were wired together in target_main.c, the viewer would have to repeat that
 * wiring in Python and could quietly disagree with the camera about what a
 * stage does. Here there is only one copy.
 *
 * Fallback is by prefix. A stage needs every model below it, so the stage that
 * actually runs is min(requested, highest N whose models 1..N are all present).
 * A model that failed its checks at load is passed as NULL, so it is simply
 * absent here: no per-frame validation, and no way to run a stage whose model
 * is broken. The effective stage goes out in every CSV row, so a fallback is
 * always visible downstream rather than silent.
 *
 * Portable like blob_detect: no ESP-IDF, no heap. Time comes from a clock the
 * caller passes in (or NULL for no timing).
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "blob_detect.h"
#include "blob_feat.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TC_STAGE_MAX    3

struct blob_clf;        /* blob_clf.h, which includes this file */
struct blob_cnn;        /* blob_cnn.h, likewise                 */

/* Validated models, one per learned stage; NULL = absent. Each becomes a
   concrete type in the phase that introduces its stage. */
typedef struct {
    const uint8_t *lut; /* stage 1: colour table, COLOR_LUT_BYTES of bits
                           (color_lut.h)                                      */
    const struct blob_clf *clf; /* stage 2: blob classifier (blob_clf.h). The
                           caller passes it only behind the colour table it
                           was trained with (blob_clf.h, lut_id).            */
    const struct blob_cnn *cnn; /* stage 3: crop CNN (blob_cnn.h), behind the
                           same colour table                                 */
    int cnn_k;          /* blobs the CNN checks, the largest of those stage 2
                           kept: 1..BLOB_MAX_REPORT (0 is taken as 2). The
                           ones past it are reported with stage 2's verdict:
                           the CNN's cost is per crop, and blob 1, the one the
                           blimp steers to, is always checked                */
} tc_models_t;

typedef struct {
    blob_result_t r;            /* blobs that survived every stage that ran   */
    int     stage;              /* effective stage, 0..TC_STAGE_MAX           */
    float   conf[BLOB_MAX_REPORT];  /* per reported blob, 0..1; -1 = not
                                   computed (stage < 2) or empty slot. From
                                   stage 2 every reported blob has conf >=
                                   the classifier's threshold: the rest were
                                   dropped, and the survivors moved up in
                                   size order. r.n_blobs drops by the same
                                   count; r.matched (pixels) does not.       */
    int32_t t_px_us;            /* per-pixel colour test (HSV or table)       */
    int32_t t_b_us;             /* blob features + classifier                 */
    int32_t t_c_us;             /* crops + CNN                                */
    int     n_cnn;              /* blobs the CNN checked (stage 3)            */
    int     n_rejected3;        /* of those, the ones it dropped              */
    int32_t t_total_us;         /* the whole call                             */
    int     n_feat;             /* entries of feat[] filled: r.n_report when
                                   features were computed, else 0             */
    blob_feat_t feat[BLOB_MAX_REPORT];  /* per reported blob (blob_feat.h)    */
    int     n_rejected;         /* blobs stage 2 dropped from the report:
                                   colour-stage blobs whose conf fell below
                                   the classifier's threshold. 0 below 2.    */
} tc_result_t;

/* tc_pipeline_run() flags. */
#define TC_RUN_FEATURES 0x1     /* compute blob features even below stage 2,
                                   which needs them anyway: for collecting
                                   training data (!collect), and for timing
                                   them on the camera (t_b_us) before any
                                   classifier exists                          */

/* Highest stage the given models allow, 0..TC_STAGE_MAX. */
int tc_stage_available(const tc_models_t *m);

/* The stage that will run for a request: min(request, available). A request
   outside 0..TC_STAGE_MAX is clamped into it. */
int tc_stage_effective(int stage_req, const tc_models_t *m);

/* The detector configuration a stage runs: cfg, with the colour table
   switched in from stage 1 up and out below it. tc_pipeline_run() uses it,
   and so does anything that draws the mask (the camera's 'm', the viewer's
   overlay), so a mask always shows the pixel test of the stage that runs. */
blob_cfg_t tc_stage_cfg(const blob_cfg_t *cfg, int stage_req, const tc_models_t *m);

/* The pixel test of the stage that would run, one byte per pixel, as
   blob_mask(). Returns the number of matching pixels. */
int tc_pipeline_mask(const blob_cfg_t *cfg, int stage_req, const tc_models_t *m,
                     const uint8_t *frame, uint8_t *mask);

/*
 * Run the stages up to tc_stage_effective(stage_req, m) on one frame.
 * `flags` is TC_RUN_* or 0.
 * `work` is blob_detect's workspace (blob_workspace_bytes(cfg) bytes, 4-byte
 * aligned). `now_us` may be NULL, and then every time field is 0.
 * Returns out->r.found.
 */
int tc_pipeline_run(const blob_cfg_t *cfg, int stage_req, int flags,
                    const tc_models_t *m, const uint8_t *frame, void *work,
                    blob_clock_fn now_us, tc_result_t *out);

/* Standard CRC-32 (zlib.crc32, reflected, poly 0xEDB88320, init and xorout
   0xFFFFFFFF; "123456789" -> 0xCBF43926). The camera's link uses the ROM
   routine, which a PC does not have; model records are checked on both, so
   they get this portable one. Bitwise and slow, which is fine: it runs when a
   model is loaded, never per frame. */
uint32_t tc_crc32(const void *buf, size_t len);

/* Continue a CRC-32 over more bytes: tc_crc32_update(tc_crc32(a), b) equals
   the CRC of a followed by b (zlib.crc32(b, crc) does the same). */
uint32_t tc_crc32_update(uint32_t crc, const void *buf, size_t len);

/*
 * Model records. Every learned model travels in one format, whether it is
 * compiled into the firmware (main/models/<x>_model.h is the record as a byte
 * array) or, later, uploaded into flash: the same bytes, the same check, so a
 * second delivery path adds transport and nothing else.
 *
 *   offset  size  field
 *        0     4  magic, four ASCII characters naming the kind ("TCLU")
 *        4     2  format version of that kind's payload
 *        6     2  header size, TC_MODEL_HDR_BYTES
 *        8     4  payload size in bytes
 *       12     4  train id: when it was trained (Unix seconds), so a row of
 *                 #!CFG ties the camera to one training run's report
 *       16     4  CRC-32 of bytes 0..15 followed by the payload
 *       20     -  payload
 *
 * Little-endian, read byte by byte, so neither alignment nor the compiler's
 * struct packing matters. The CRC covers the header too: a flipped bit in the
 * size or the train id is as much a corrupt model as one in the payload.
 */
#define TC_MODEL_HDR_BYTES  20

typedef enum {
    TC_MODEL_OK = 0,
    TC_MODEL_SHORT,         /* fewer bytes than the header and payload need   */
    TC_MODEL_MAGIC,         /* not a model of this kind                       */
    TC_MODEL_VERSION,       /* a payload format this firmware does not read   */
    TC_MODEL_SIZE,          /* the payload size is not this kind's            */
    TC_MODEL_CRC,           /* corrupt                                        */
    TC_MODEL_VALUE,         /* intact, but a value in it cannot be used (a
                               non-finite weight, a threshold outside 0..1)   */
} tc_model_err_t;

/* Check a record of the given kind. On TC_MODEL_OK, *payload points into
   rec and *train_id is set. `payload_len` is the size the kind requires. */
tc_model_err_t tc_model_open(const uint8_t *rec, size_t len, const char magic[4],
                             uint16_t version, uint32_t payload_len,
                             const uint8_t **payload, uint32_t *train_id);

/* "ok", "short", "magic", "version", "size", "crc", "value". */
const char *tc_model_err_str(tc_model_err_t e);

#ifdef __cplusplus
}
#endif
