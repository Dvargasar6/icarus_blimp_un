/*
 * blob_detect - colour-marker detection for a forward-looking camera.
 *
 * Deliberately portable, like optflow_cam's flow_match: this header and its .c
 * file have NO ESP-IDF, no FreeRTOS and no heap use, so the identical code that
 * runs on the camera also compiles on a host with gcc and is tested there
 * against synthetic frames whose answer we chose (see host/test_blob_detect.c).
 *
 * The problem is narrow on purpose. We are looking for ONE object of a known,
 * saturated colour - a balloon, a ball, a painted tag - and we want its position
 * in the image and its apparent size, so the airframe can steer at it and judge
 * the range. The pipeline:
 *
 *   1. Classify every pixel by hue / saturation / value. Hue is what makes a
 *      colour test tolerant of lighting: a red balloon in shadow is darker, not
 *      less red. Saturation and value floors reject the grey walls and dark
 *      corners whose hue is pure noise.
 *   2. Pool pixels into cells (default 2x2). A cell is set only when a majority
 *      of its pixels match, which removes single-pixel speckle for free and
 *      quarters the labelling cost and memory.
 *   3. Label connected cells (4-connectivity, union-find).
 *   4. Report the BLOB_MAX_REPORT largest blobs, largest first, each with a
 *      centroid computed from its matching pixels, so pooling costs no
 *      centroid precision. Several, not one: the IAR-LARC course is four
 *      identical red balloons, and the nearest is not always the next
 *      waypoint, so choosing among them is the navigator's job, not ours.
 *
 * Like flow_match, it fails honestly: no blob above min_area returns found=0,
 * never a guess.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int width;          /* frame width in pixels (row stride = 2*width bytes) */
    int height;         /* frame height in pixels                             */
    int cell;           /* pooling cell edge: 1, 2 or 4; must divide w and h  */
    int big_endian;     /* 1 if each RGB565 pixel is stored high byte first.
                           esp32-camera does this; most host tools do not.    */
    int hue;            /* target hue, degrees 0..359 (0 red, 120 green, ...)  */
    int hue_tol;        /* accept hue within +/- this, degrees 1..90           */
    int sat_min;        /* reject pixels less saturated than this, 0..255     */
    int val_min;        /* reject pixels darker than this, 0..255             */
    int min_area;       /* smallest blob reported, in matching pixels         */
    const uint8_t *lut; /* NULL: the HSV test above decides each pixel. Else a
                           learned colour table (color_lut.h), one bit per
                           RGB565 value, decides it instead and hue..val_min
                           are not used for matching. Set by tc_pipeline from
                           the stage; last, so every initialiser that names
                           its fields leaves it NULL.                         */
} blob_cfg_t;

#define BLOB_MAX_REPORT 4

typedef struct {
    float cx;           /* centroid, pixels. Pixel i spans [i, i+1), so the    */
    float cy;           /*   frame centre is exactly (width/2, height/2).      */
    int   area;         /* matching pixels in the blob, edge pixels included  */
    int   x0, y0;       /* bounding box of the blob's matching pixels,        */
    int   x1, y1;       /*   inclusive.                                       */
    int   clipped;      /* 1 if the box touches the frame edge: the object is
                           partly out of view, so its size (and any range
                           derived from it) is an underestimate.              */
} blob_t;

typedef struct {
    int    found;       /* 1 if a blob >= min_area exists. 0 = NO TARGET, not
                           a target at the centre; b[] is all zero.           */
    int    n_report;    /* entries filled in b[], 0..BLOB_MAX_REPORT          */
    int    n_blobs;     /* blobs with area >= min_area in the frame; can exceed
                           n_report                                           */
    int    matched;     /* matching pixels in the whole frame                 */
    blob_t b[BLOB_MAX_REPORT];  /* largest first                              */
} blob_result_t;

/* Colour presets for bench testing. RED is the competition target (the
   IAR-LARC waypoints are red mylar balloons); the others let the same
   firmware be tested with whatever balloons are to hand. Red and orange are
   only 25 degrees apart, so their windows are deliberately narrow and do not
   overlap - see host test 14 for the cross-confusion check. */
typedef enum {
    BLOB_PRESET_RED = 0,
    BLOB_PRESET_BLUE,
    BLOB_PRESET_GREEN,
    BLOB_PRESET_ORANGE,
    BLOB_PRESET_COUNT
} blob_preset_t;

/* Set hue, hue_tol, sat_min and val_min from a preset. Returns 0 for an
   unknown preset (cfg unchanged), 1 otherwise. */
int blob_preset_apply(blob_cfg_t *cfg, int preset);

/* "red", "blue", "green", "orange", or "?" */
const char *blob_preset_name(int preset);

/* Preset index for a name (case-insensitive, first letter is enough), or -1. */
int blob_preset_find(const char *name);

/* Returns 1 if the configuration is usable, 0 if not. */
int blob_cfg_valid(const blob_cfg_t *cfg);

/* Scratch memory blob_detect() needs for this configuration, in bytes. The
   caller allocates it once; nothing in here touches the heap. 28 kB for QQVGA
   with 2x2 cells, 113 kB with 1x1 - which is why 2x2 is the default. */
size_t blob_workspace_bytes(const blob_cfg_t *cfg);

/* Hue (0..359), saturation and value (0..255) of one RGB565 pixel. Hue is
   meaningless when sat is near 0 and is returned as 0 in that case. */
void blob_rgb565_hsv(uint16_t px, int *hue, int *sat, int *val);

/* Read pixel (x, y) from a frame, honouring cfg->big_endian. */
uint16_t blob_pixel(const blob_cfg_t *cfg, const uint8_t *frame, int x, int y);

/* 1 if this pixel passes the colour test: the table when cfg->lut is set, the
   HSV window otherwise. Every pass of the detector, blob_mask() and the
   camera's 'm' dump go through this one function, so they cannot disagree
   about a pixel. */
int blob_pixel_matches(const blob_cfg_t *cfg, uint16_t px);

/* The whole frame's colour test, one byte per pixel (1 match, 0 not), row-major
   into `mask`, which must hold width*height bytes. Exactly blob_pixel_matches()
   per pixel: the camera's 'm' dump and the PC viewer's overlay both come from
   here, so neither can drift from what the detector sees. Returns the number
   of matching pixels. */
int blob_mask(const blob_cfg_t *cfg, const uint8_t *frame, uint8_t *mask);

/*
 * Detect the largest blobs of the configured colour in an RGB565 frame.
 * `work` must be at least blob_workspace_bytes(cfg) bytes, 4-byte aligned.
 * Returns out->found.
 */
int blob_detect(const blob_cfg_t *cfg, const uint8_t *frame, void *work,
                blob_result_t *out);

/* A microsecond clock, supplied by the caller: esp_timer_get_time on the
   camera, clock_gettime on a PC. Passing it in is what keeps this file free of
   ESP-IDF while still letting the camera time the stages separately. */
typedef int64_t (*blob_clock_fn)(void);

/* blob_detect(), also timing pass 1 (the per-pixel colour test, the stage the
   learned colour table replaces) into *t_px_us. With now_us == NULL nothing is
   timed and *t_px_us is 0. The result is identical to blob_detect()'s. */
int blob_detect_timed(const blob_cfg_t *cfg, const uint8_t *frame, void *work,
                      blob_result_t *out, blob_clock_fn now_us, int32_t *t_px_us);

/* Colour statistics of a rectangle, used to calibrate on a target held in front
   of the lens. Hue is a circular mean weighted by saturation, so a red patch
   straddling 0/360 averages to red rather than cyan. */
typedef struct {
    int hue;            /* 0..359                                             */
    int hue_spread;     /* circular standard deviation, degrees               */
    int sat;            /* mean saturation, 0..255                            */
    int val;            /* mean value, 0..255                                 */
} blob_color_t;

void blob_sample(const blob_cfg_t *cfg, const uint8_t *frame,
                 int x0, int y0, int w, int h, blob_color_t *out);

#ifdef __cplusplus
}
#endif
