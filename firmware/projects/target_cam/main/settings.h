/*
 * settings - the hand-calibrated detector settings, kept in NVS.
 *
 * Calibration is done at the bench with the PC viewer, but the camera flies
 * with nobody at a PC. So whatever was tuned has to survive a power cycle, and
 * the boot banner has to say whether the board is running saved settings or
 * the Kconfig defaults - otherwise a flight on forgotten defaults looks exactly
 * like a flight on a careful calibration.
 *
 * The record is a versioned, fixed-size struct. A record of another version or
 * size is ignored (defaults are used, and the banner says so) rather than
 * reinterpreted. The exceptions are the v1 to v4 records, which
 * settings_load() migrates field by field: see there.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    int16_t hue;            /* blob_cfg_t fields                              */
    int16_t hue_tol;
    int16_t sat_min;
    int16_t val_min;
    int32_t min_area;
    int32_t target_mm;      /* 0 = range off                                  */
    float   hfov_deg;
    int16_t period_ms;      /* frame period; v1 stored an integer fps here    */
    int8_t  awb;            /* white balance: 1 auto, 0 frozen, 2 fixed (v5)  */
    int8_t  big_endian;
    int8_t  preset;         /* blob_preset_t, or -1 for hand-tuned            */
    int8_t  stage;          /* requested detector stage, 0..3 (v3)            */
    int8_t  cnn_k;          /* blobs the CNN checks per frame, 1..4 (v3)      */
    int8_t  flip;           /* image turned 180 deg on top of the build (v4)  */
    uint8_t wb_gain[3];     /* R, G, B gains for awb 2, 64 = 1.0 (v5)         */
    uint8_t spare;          /* zero; makes the 32 B size explicit             */
} tc_settings_t;

/* Initialise NVS. A partition that is full or from a newer IDF is erased:
   losing a calibration is better than a camera that will not boot. */
esp_err_t settings_init(void);

/* true and *out filled if a valid saved record exists. *migrated is set when
   it was a v1 record, whose frame rate was replaced by the build default. A v2
   record loads without that flag: it gains only stage 0, which is what every
   v2 build ran, so nothing the user tuned has changed. Nor does a v3 record:
   it gains flip 0, the image as every v3 build delivered it; nor a v4 one,
   which gains unity white-balance gains that no v4 build could use. */
bool settings_load(tc_settings_t *out, bool *migrated);

esp_err_t settings_save(const tc_settings_t *s);

/* Remove the saved record, so the next boot uses the Kconfig defaults. */
esp_err_t settings_erase(void);
