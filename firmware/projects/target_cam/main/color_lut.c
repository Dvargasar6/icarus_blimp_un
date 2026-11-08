/*
 * color_lut - see color_lut.h for what and why.
 */
#include "color_lut.h"

#include <string.h>

tc_model_err_t color_lut_load(const uint8_t *rec, size_t len,
                              uint8_t bits[COLOR_LUT_BYTES], uint32_t *train_id)
{
    const uint8_t *payload;
    uint32_t id;
    tc_model_err_t e = tc_model_open(rec, len, COLOR_LUT_MAGIC, COLOR_LUT_VERSION,
                                     COLOR_LUT_BYTES, &payload, &id);
    if (e != TC_MODEL_OK) return e;
    memcpy(bits, payload, COLOR_LUT_BYTES);
    *train_id = id;
    return TC_MODEL_OK;
}

void color_lut_from_hsv(const blob_cfg_t *cfg, uint8_t bits[COLOR_LUT_BYTES])
{
    blob_cfg_t c = *cfg;
    c.lut = NULL;
    memset(bits, 0, COLOR_LUT_BYTES);
    for (uint32_t px = 0; px < 65536u; px++)
        if (blob_pixel_matches(&c, (uint16_t)px))
            bits[px >> 3] = (uint8_t)(bits[px >> 3] | (1u << (px & 7u)));
}

int color_lut_count(const uint8_t bits[COLOR_LUT_BYTES])
{
    int n = 0;
    for (int i = 0; i < COLOR_LUT_BYTES; i++)
        for (uint8_t b = bits[i]; b; b &= (uint8_t)(b - 1u)) n++;
    return n;
}
