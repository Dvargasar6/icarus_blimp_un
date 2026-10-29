/*
 * blob_clf - see blob_clf.h for what and why.
 */
#include "blob_clf.h"

#include <math.h>
#include <string.h>

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 |
           (uint32_t)p[3] << 24;
}

/* A float from its four little-endian bytes. memcpy rather than a pointer
   cast: the payload has no alignment guarantee, and the copy is what the
   compiler is allowed to assume nothing about. */
static float rdf(const uint8_t *p)
{
    uint32_t u = rd32(p);
    float f;
    memcpy(&f, &u, sizeof f);
    return f;
}

tc_model_err_t blob_clf_load(const uint8_t *rec, size_t len, blob_clf_t *out,
                             uint32_t *train_id)
{
    const uint8_t *p;
    uint32_t id;
    tc_model_err_t e = tc_model_open(rec, len, BLOB_CLF_MAGIC, BLOB_CLF_VERSION,
                                     BLOB_CLF_PAYLOAD_BYTES, &p, &id);
    if (e != TC_MODEL_OK) return e;
    if ((p[0] | p[1] << 8) != BLOB_FEAT_N || (p[2] | p[3] << 8) != BLOB_FEAT_VERSION)
        return TC_MODEL_VERSION;

    /* Unpack into a local first, so a refusal below leaves *out as it was. */
    blob_clf_t c;
    c.lut_id = rd32(p + 4);
    c.threshold = rdf(p + 8);
    c.bias = rdf(p + 12);
    int finite = isfinite(c.threshold) && isfinite(c.bias);
    for (int i = 0; i < BLOB_FEAT_N; i++) {
        c.mean[i] = rdf(p + 16 + 4 * i);
        c.scale[i] = rdf(p + 16 + 4 * (BLOB_FEAT_N + i));
        c.w[i] = rdf(p + 16 + 4 * (2 * BLOB_FEAT_N + i));
        finite = finite && isfinite(c.mean[i]) && isfinite(c.scale[i]) && isfinite(c.w[i]);
    }
    /* A threshold of 0 would keep every blob and 1 or more would keep none:
       both are a broken model, not a choice, so neither is flown. */
    if (!finite || !(c.threshold > 0.0f && c.threshold < 1.0f)) return TC_MODEL_VALUE;
    *out = c;
    *train_id = id;
    return TC_MODEL_OK;
}

float blob_clf_conf(const blob_clf_t *clf, const blob_feat_t *f)
{
    float s = clf->bias;
    for (int i = 0; i < BLOB_FEAT_N; i++) {
        if (!isfinite(f->v[i])) return 0.0f;
        s += clf->w[i] * ((f->v[i] - clf->mean[i]) * clf->scale[i]);
    }
    /* expf(-s) overflows to inf for s < about -88, which gives 0, the right
       limit; for large s it underflows to 0 and gives 1. No clamp needed. */
    return 1.0f / (1.0f + expf(-s));
}

size_t blob_clf_struct_bytes(void)
{
    return sizeof(blob_clf_t);
}
