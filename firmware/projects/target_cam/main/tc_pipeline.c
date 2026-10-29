/*
 * tc_pipeline - see tc_pipeline.h for what and why.
 *
 * Stage 1 (Phase A2) is the colour table: the same detector with a different
 * pixel test, switched in through cfg->lut. Stage 2 (Phase B2) computes each
 * reported blob's features (Phase B1) and drops the blobs the classifier does
 * not believe. Stage 3 has no model yet, so a request for it falls back.
 */
#include "tc_pipeline.h"

#include "blob_clf.h"

#include <string.h>

int tc_stage_available(const tc_models_t *m)
{
    if (m == NULL || m->lut == NULL) return 0;
    if (m->clf == NULL) return 1;
    if (m->cnn == NULL) return 2;
    return 3;
}

int tc_stage_effective(int stage_req, const tc_models_t *m)
{
    if (stage_req < 0) stage_req = 0;
    if (stage_req > TC_STAGE_MAX) stage_req = TC_STAGE_MAX;
    int avail = tc_stage_available(m);
    return stage_req < avail ? stage_req : avail;
}

blob_cfg_t tc_stage_cfg(const blob_cfg_t *cfg, int stage_req, const tc_models_t *m)
{
    blob_cfg_t c = *cfg;
    c.lut = tc_stage_effective(stage_req, m) >= 1 ? m->lut : NULL;
    return c;
}

int tc_pipeline_mask(const blob_cfg_t *cfg, int stage_req, const tc_models_t *m,
                     const uint8_t *frame, uint8_t *mask)
{
    blob_cfg_t c = tc_stage_cfg(cfg, stage_req, m);
    return blob_mask(&c, frame, mask);
}

/* Stage 2: keep the blobs the classifier believes, in their size order, and
   compact them to the front so blob 1 is the largest believed one. A frame
   whose every blob is rejected reports no target (found 0), which is what
   the S3 must see: "nothing worth steering to", not the best of bad blobs.

   Only the reported blobs are classified. A balloon that was the fifth
   largest red blob is not promoted when one of the four above it is
   rejected: blob_detect keeps no record of it. At blimp ranges four false
   blobs larger than a balloon are not expected (reasoned, not measured). */
static void classify(const struct blob_clf *clf, tc_result_t *out)
{
    blob_result_t *r = &out->r;
    int n = 0;
    for (int k = 0; k < r->n_report; k++) {
        float p = blob_clf_conf(clf, &out->feat[k]);
        if (p < clf->threshold) continue;
        r->b[n] = r->b[k];
        out->feat[n] = out->feat[k];
        out->conf[n] = p;
        n++;
    }
    out->n_rejected = r->n_report - n;
    for (int k = n; k < BLOB_MAX_REPORT; k++) {
        memset(&r->b[k], 0, sizeof r->b[k]);
        memset(&out->feat[k], 0, sizeof out->feat[k]);
        out->conf[k] = -1.0f;
    }
    r->n_report = n;
    r->n_blobs -= out->n_rejected;
    r->found = n > 0;
    out->n_feat = n;
}

int tc_pipeline_run(const blob_cfg_t *cfg, int stage_req, int flags,
                    const tc_models_t *m, const uint8_t *frame, void *work,
                    blob_clock_fn now_us, tc_result_t *out)
{
    int64_t t0 = now_us ? now_us() : 0;

    out->stage = tc_stage_effective(stage_req, m);
    out->t_b_us = 0;
    out->t_c_us = 0;
    out->n_rejected = 0;
    for (int k = 0; k < BLOB_MAX_REPORT; k++) out->conf[k] = -1.0f;

    /* Stage 1 changes only the pixel test. Labelling, the moments and the
       area floor are stage 0's, so any difference in what is reported comes
       from which pixels match and nothing else. */
    blob_cfg_t c = tc_stage_cfg(cfg, stage_req, m);
    blob_detect_timed(&c, frame, work, &out->r, now_us, &out->t_px_us);

    /* Features after detection, from the frame again, rather than inside
       blob_detect's moment pass: that keeps stages 0 and 1 bit-identical to
       what they were and costs nothing when no one asks. The price is a
       second read of each blob's box, which blob_feat bounds per blob. */
    out->n_feat = 0;
    memset(out->feat, 0, sizeof out->feat);
    if ((flags & TC_RUN_FEATURES) || out->stage >= 2) {
        int64_t tb = now_us ? now_us() : 0;
        for (int k = 0; k < out->r.n_report; k++)
            blob_features(&c, frame, &out->r.b[k], &out->feat[k]);
        out->n_feat = out->r.n_report;
        if (out->stage >= 2) classify(m->clf, out);
        out->t_b_us = now_us ? (int32_t)(now_us() - tb) : 0;
    }

    out->t_total_us = now_us ? (int32_t)(now_us() - t0) : 0;
    return out->r.found;
}

uint32_t tc_crc32_update(uint32_t crc, const void *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    uint32_t c = crc ^ 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        c ^= p[i];
        for (int b = 0; b < 8; b++)
            c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
    }
    return c ^ 0xFFFFFFFFu;
}

uint32_t tc_crc32(const void *buf, size_t len)
{
    return tc_crc32_update(0u, buf, len);
}

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 |
           (uint32_t)p[3] << 24;
}

static uint16_t rd16(const uint8_t *p)
{
    return (uint16_t)(p[0] | p[1] << 8);
}

tc_model_err_t tc_model_open(const uint8_t *rec, size_t len, const char magic[4],
                             uint16_t version, uint32_t payload_len,
                             const uint8_t **payload, uint32_t *train_id)
{
    /* Cheapest and most telling checks first: a record of the wrong kind
       says "magic", not "crc". */
    if (rec == NULL || len < TC_MODEL_HDR_BYTES) return TC_MODEL_SHORT;
    if (memcmp(rec, magic, 4) != 0) return TC_MODEL_MAGIC;
    if (rd16(rec + 4) != version || rd16(rec + 6) != TC_MODEL_HDR_BYTES)
        return TC_MODEL_VERSION;
    uint32_t n = rd32(rec + 8);
    if (n != payload_len) return TC_MODEL_SIZE;
    if (len - TC_MODEL_HDR_BYTES < n) return TC_MODEL_SHORT;
    uint32_t crc = tc_crc32_update(tc_crc32(rec, 16), rec + TC_MODEL_HDR_BYTES, n);
    if (crc != rd32(rec + 16)) return TC_MODEL_CRC;
    *payload = rec + TC_MODEL_HDR_BYTES;
    *train_id = rd32(rec + 12);
    return TC_MODEL_OK;
}

const char *tc_model_err_str(tc_model_err_t e)
{
    switch (e) {
    case TC_MODEL_OK:      return "ok";
    case TC_MODEL_SHORT:   return "short";
    case TC_MODEL_MAGIC:   return "magic";
    case TC_MODEL_VERSION: return "version";
    case TC_MODEL_SIZE:    return "size";
    case TC_MODEL_CRC:     return "crc";
    case TC_MODEL_VALUE:   return "value";
    }
    return "?";
}
