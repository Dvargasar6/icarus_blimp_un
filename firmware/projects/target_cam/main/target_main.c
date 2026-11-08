/*
 * Forward-looking colour-target detection on an ESP32-CAM.
 *
 * Grabs paced RGB565 frames, finds the largest blob of one configured colour,
 * and streams its bearing, size and (optionally) range as CSV on UART0. Written
 * for the blimp's nose camera: point it at a balloon, ball or painted tag and
 * the flight board gets "target is 6.2 deg right, 1.4 deg up, ~3.1 m away".
 *
 * Built on the same rules as optflow_cam, and sharing its hard-won board
 * handling (boot report before the camera powers up, the GPIO0 reflash window,
 * flash LED driven low, radios never started):
 *
 *  1. HEAT. QQVGA, paced frames, no WiFi. The idle time is the point. The
 *     sensor sets the pace: at XCLK 20 MHz it makes a frame every 79.968 ms
 *     (12.5 FPS), the loop takes each one as it completes, and nothing faster
 *     exists. 'f' can only choose every n-th frame.
 *
 *  2. HONEST FAILURE. ok=0 means NO TARGET, never "target dead ahead". A blob
 *     touching the frame edge is flagged clipped and gets no range, because
 *     its apparent size is an underestimate. The runner-up blob's size is
 *     printed so an ambiguous scene - two red things - is visible downstream.
 *
 *  3. COLOUR IS NOT FREE. Hue survives shadow far better than RGB does, but
 *     auto white balance actively works against a detector when the target
 *     fills much of the frame: it shifts the whole image to make that colour
 *     greyer. AWB is left on by default and can be frozen with 'w0' once the
 *     scene is lit; which is better on this airframe is a measurement, not a
 *     calculation.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_camera.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_chip_info.h"
#include "esp_app_desc.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "sdkconfig.h"

#include "camera_pins.h"
#include "blob_detect.h"
#include "color_lut.h"
#include "blob_clf.h"
#include "blob_cnn.h"
#include "tc_pipeline.h"
#include "models/lut_model.h"
#include "models/clf_model.h"
#include "models/cnn_model.h"
#include "link.h"
#include "settings.h"
#include "model_store.h"

static const char *TAG = "target";

#define FRAME_SIZE      FRAMESIZE_QQVGA     /* 160x120 */
#define FRAME_W         160
#define FRAME_H         120

/* The CSV row format. Bumped whenever a column is added, removed or changes
   meaning; it goes out as the last column of every row and in #!CFG, so a
   reader can refuse a format it does not know instead of misreading it.
   v1 (no version column) had 25 columns; v2 appends 10, see print_banner(). */
#define CSV_VERSION     2

/* Seconds of reflash window before GPIO0 becomes XCLK. See
   escape_hatch_delay(). */
#define TARGETCAM_ESCAPE_SECONDS  5

/* Side of the centre patch the 'c' command calibrates from. 16 px is ~5.4 deg
   at QQVGA: a 300 mm balloon fills it out to about 3 m. */
#define CAL_PATCH       16

/* White balance, 'w' and 'W'. AUTO is the OV2640's own AWB, which re-solves
   the gains on every frame from whatever is in view. FROZEN stops it where it
   is, at gains nobody knows. FIXED loads gains we chose, so they can be
   written down next to every frame and record. The numbers are those 'w'
   takes, and 0 and 1 mean what they meant before v5. */
#define WB_FROZEN       0
#define WB_AUTO         1
#define WB_FIXED        2

/* The OV2640's DSP-bank registers for white balance. get_reg()/set_reg()
   take the bank in bit 8, and the DSP bank is 0, so these are plain. The
   driver's presets (ov2640.c set_wb_mode) set bit 6 of 0xC7 to select manual
   gains and then write 0xCC, 0xCD, 0xCE as R, G, B. In all of its presets G is
   0x3F-0x41 (sunny 5E,41,54; home 42,3F,71), which is why 64 is taken here as
   a gain of 1.0. That is inferred, not from a datasheet. */
#define DSP_CTRL1       0xC3    /* bit 3 AWB on, bit 2 AWB gains applied     */
#define DSP_AWB_CTRL    0xC7    /* bit 6: manual gains (0xCC-0xCE)            */
#define DSP_AWB_GAIN_R  0xCC    /* then G at 0xCD and B at 0xCE              */

/* 'Wc' (white card): find the R and B gains at which the centre patch reads
   R = G = B. Each gain is searched for on its own, assuming only that a
   larger register value gives a stronger channel. The first version scaled
   each gain by G/R as if the register were a plain multiplier, and on the
   bench (2026-10-19) it overshot and then oscillated: from 64,64,64 (R/G
   0.53) to 120,64,105 (R/G 2.6) to 46,64,44 (R/G 0.07, B 0). The response is
   much steeper than proportional; fitted to those three points it looks like
   a multiplier with an offset near 44, but three points do not establish a
   law. So: small damped steps until the answer is bracketed, then the
   secant between the two closest readings on either side. G stays where it
   is: exposure control already sets the brightness, and leaving one gain
   alone keeps the answer unique. */
#define WBCAL_STEPS     16      /* steps before giving up                    */
#define WBCAL_SETTLE    3       /* frames from a register write to its frame */
#define WBCAL_TOL       0.03f   /* R/G and B/G within 3 %                    */
#define WBCAL_MIN_G     40.0f   /* 0..255; darker and noise sets the answer  */
#define WBCAL_GAIN_MIN  16      /* lowest register value tried               */
#define WBCAL_CLIP      (CAL_PATCH * CAL_PATCH / 10)  /* 10 % of the patch    */

/* Kconfig bools are undefined when off; turn them into 0/1 for the sensor. */
#ifdef CONFIG_TARGETCAM_VFLIP
#define CAM_VFLIP       1
#else
#define CAM_VFLIP       0
#endif
#ifdef CONFIG_TARGETCAM_HMIRROR
#define CAM_HMIRROR     1
#else
#define CAM_HMIRROR     0
#endif

/* Runtime-adjustable state. Single-threaded, so no locking. */
static int   s_every_n    = 1;      /* use every n-th sensor frame ('f') */
static float s_hfov_deg   = (float)CONFIG_TARGETCAM_HFOV_DEG;
static float s_target_mm  = (float)CONFIG_TARGETCAM_TARGET_MM;
static bool  s_streaming  = true;
static bool  s_dump_mask;           /* set by 'm', served on the next frame */
static bool  s_calibrate;           /* set by 'c', served on the next frame */
static int   s_wb         = 1;      /* WB_AUTO; see set_wb()               */
static uint8_t s_wb_gain[3] = {64, 64, 64};  /* fixed R, G, B ('W')         */
static int   s_wbcal;               /* 'Wc': steps left, 0 = not running   */
static int   s_wbcal_wait;          /* frames until a gain change shows    */
static int   s_wbcal_prev;          /* the mode and gains to restore if    */
static uint8_t s_wbcal_prev_gain[3]; /* the calibration is refused          */
/* The search for one gain (R or B): the register values known to read too
   weak (lo) and too strong (hi), -1 until seen, with their errors (channel/G
   - 1), and whether this gain is settled. */
typedef struct { int lo, hi; float elo, ehi; bool done; } wbcal_ch_t;
static wbcal_ch_t s_wbcal_ch[2];
static bool  s_flip;                /* '!flip': image turned 180 deg       */
static bool  s_image;               /* 'v1': send every frame to the PC viewer */
static bool  s_grab;                /* 'g': send the next frame only */
static int   s_pending_baud;        /* 'B': switched once its ACK is out */
static int   s_stage_req;           /* '!stage': the level asked for, 0..3 */
static int   s_cnn_k      = 2;      /* blobs stage 3 checks (Phase C)      */
static bool  s_collect;             /* '!collect 1': blob records (Phase B1).
                                       Never saved: collecting is a bench
                                       session, and a board that boots into
                                       it would be slow in flight.            */
static int   s_collect_n;           /* frames since the last records          */

/* Records go out on every COLLECT_EVERY-th frame: 12.5 / 4 = 3.1 frames a
   second, up to four blobs each. Consecutive frames 80 ms apart are nearly
   the same picture, so taking all of them would add bytes, not examples;
   and four records are ~6 kB, 0.05 s at 921600 baud but 0.5 s at 115200,
   which is the real ceiling on the slow link. */
#define COLLECT_EVERY   4

/* The crops of the frame being collected, cut while the frame is still held
   and sent after its CSV row. Static, 4.6 kB: allocated once, never in the
   loop. */
static uint16_t        s_crops[BLOB_MAX_REPORT][BLOB_CROP * BLOB_CROP];
static blob_crop_box_t s_crop_box[BLOB_MAX_REPORT];

/* The learned models, each validated once at boot; NULL = absent or rejected,
   and the pipeline falls back past it. The state strings are what #!CFG
   reports: none (not built in), ok, or bad (built in but failed its checks). */
static tc_models_t s_models;
static const char *s_lut_state = "none";
static uint32_t    s_lut_id;          /* the table's train id; #!CFG lut_id= */

/* The colour table is copied out of flash into DRAM at boot rather than read
   in place. Stage 1 looks it up at random 65 536-colour addresses for every
   pixel, and a flash-cached table would compete with the frame for the cache
   and add jitter to the 80 ms loop; a RAM copy costs a fixed 8 kB. The copy
   is also what the later NVS upload (Phase D) needs, so both delivery paths
   end in the same buffer, checked by the same color_lut_load(). */
static uint8_t s_lut[COLOR_LUT_BYTES];

/* The classifier and the CNN, unpacked from their records. Either can come
   from the build or from an upload (model_store.h), so they exist whether or
   not one is compiled in. A state adds "mismatch" to the lut's: an intact
   model trained behind a different colour table than the one running. Its
   inputs would come from other pixels than it learned from, so it is left
   out and the stage falls back. */
static blob_clf_t  s_clf_rec;         /* as the record holds it               */
static blob_clf_t  s_clf;             /* what runs: s_clf_rec with '!thr2'    */
static blob_cnn_t  s_cnn_rec;         /* likewise; ~5 kB each, so static      */
static blob_cnn_t  s_cnn;
/* '!thr2 N' / '!thr3 N': the thresholds in hundredths, 0 = the model's own.
   A threshold trades lost balloons against false ones, and the right trade
   depends on the venue's clutter, so it is a setting (saved by !save) rather
   than something only retraining can change. */
static int         s_thr2, s_thr3;
static const char *s_clf_state = "none", *s_cnn_state = "none";
static uint32_t    s_clf_id, s_cnn_id;    /* #!CFG clf_id= cnn_id=            */
/* Where each running model came from: "build" (main/models, compiled in),
   "upload" (!upload, model_store.h) or "none". #!CFG lut_src= clf_src= ... */
static const char *s_lut_src = "none", *s_clf_src = "none", *s_cnn_src = "none";

static void reload_models(void);       /* below: needs every loader         */

/* The records compiled in; NULL where main/models holds a placeholder. */
#ifdef CLF_MODEL_NONE
#define CLF_BUILD_REC   NULL
#define CLF_BUILD_LEN   0
#else
#define CLF_BUILD_REC   clf_model_rec
#define CLF_BUILD_LEN   sizeof clf_model_rec
#endif
#ifdef CNN_MODEL_NONE
#define CNN_BUILD_REC   NULL
#define CNN_BUILD_LEN   0
#else
#define CNN_BUILD_REC   cnn_model_rec
#define CNN_BUILD_LEN   sizeof cnn_model_rec
#endif

/* s_clf / s_cnn = the record's model with the '!thr2' / '!thr3' threshold,
   if one is set. Called whenever either changes. */
static void apply_thr2(void)
{
    s_clf = s_clf_rec;
    if (s_thr2 > 0) s_clf.threshold = (float)s_thr2 / 100.0f;
}

static void apply_thr3(void)
{
    s_cnn = s_cnn_rec;
    if (s_thr3 > 0) s_cnn.threshold = (float)s_thr3 / 100.0f;
}

/* The thresholds stages 2 and 3 run with, or -1 with the model not running. */
static float clf_threshold(void)
{
    return s_models.clf != NULL ? s_clf.threshold : -1.0f;
}

static float cnn_threshold(void)
{
    return s_models.cnn != NULL ? s_cnn.threshold : -1.0f;
}

/* Busy time of the previous loop iteration, from waking up to the end of its
   CSV row. The row cannot carry its own (the print is part of it), so each row
   reports the one before. Against the 80 ms period, this is the margin. */
static int64_t s_loop_us;

static void *s_work;                /* blob_detect workspace, allocated once */

#if defined(CONFIG_TARGETCAM_PRESET_BLUE)
#define DEFAULT_PRESET  BLOB_PRESET_BLUE
#elif defined(CONFIG_TARGETCAM_PRESET_GREEN)
#define DEFAULT_PRESET  BLOB_PRESET_GREEN
#elif defined(CONFIG_TARGETCAM_PRESET_ORANGE)
#define DEFAULT_PRESET  BLOB_PRESET_ORANGE
#else
#define DEFAULT_PRESET  BLOB_PRESET_RED
#endif

/* The colour preset in use, or -1 once thresholds are hand-tuned (H/T/S/V or
   'c'), so the banner never claims "red" for settings that are not red's. */
static int s_preset = DEFAULT_PRESET;

/* What the next boot would run: the record in flash, or the Kconfig defaults
   when nothing is saved. #!CFG compares the live settings against it, so the
   viewer can say "modified, not saved" - a calibration that dies with the next
   power cycle should never look finished. */
static tc_settings_t s_ref;
static bool          s_ref_saved;
static bool          s_nvs_ok;

/* Why the last command was refused, for its #!NAK line. */
static char s_why[64];

static blob_cfg_t s_cfg = {
    .width      = FRAME_W,
    .height     = FRAME_H,
    .cell       = 2,
    /* esp32-camera hands RGB565 over high byte first. If a first power-up
       detects nothing at all, even after 'c', try 'o' to flip this. */
    .big_endian = 1,
    /* hue, hue_tol, sat_min, val_min come from the preset in app_main() */
    .min_area   = CONFIG_TARGETCAM_MIN_AREA,
};

/* ------------------------------------------------------------------ */

/* The OV2640 spends a fixed number of XCLK cycles on a QQVGA frame, so XCLK
   alone sets its period: 1 599 360 cycles, measured 2026-10-14 as driver frame
   timestamps 79 968 us apart at 20 MHz (optflow_cam's 159.9 ms at 10 MHz
   agrees). The loop keeps no time of its own (see app_main()), so a row period
   is always a whole number of these. */
#define SENSOR_FRAME_US     (1599360.0 / CONFIG_TARGETCAM_XCLK_MHZ)

/* What the driver's timestamps say the period really is; shown in #!CFG, so a
   board whose sensor differs shows it. The pacing does not depend on it. */
static double s_sensor_us = SENSOR_FRAME_US;

/* Sensor frames per row for a requested period: at least 1, since the sensor
   is never faster than itself, and at most what fits in 1 s. */
static int every_n_for_ms(double ms)
{
    long n = lround(ms * 1000.0 / SENSOR_FRAME_US);
    long n_max = (long)(1000000.0 / SENSOR_FRAME_US);
    if (n_max < 1) {
        n_max = 1;
    }
    return (int)(n < 1 ? 1 : n > n_max ? n_max : n);
}

/* The row period. Kept as a period because 12.5 FPS is not an integer; FPS is
   only how it is shown. Nominal, not measured, so a saved record compares
   equal to the live settings however the measurement wobbles. */
static double period_ms(void)
{
    return s_every_n * SENSOR_FRAME_US / 1000.0;
}

static float frame_fps(void)
{
    return (float)(1000.0 / period_ms());
}

/* Focal length in pixels, from the pinhole model. Bearing and range both go
   through this, so an uncalibrated FOV scales every angle and every range. */
static float focal_px(void)
{
    float hfov_rad = s_hfov_deg * (float)M_PI / 180.0f;
    return ((float)FRAME_W * 0.5f) / tanf(hfov_rad * 0.5f);
}

static void quiesce_heat_sources(void)
{
#if CAM_PIN_FLASH_LED >= 0
    gpio_config_t led = {
        .pin_bit_mask = 1ULL << CAM_PIN_FLASH_LED,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&led));
    ESP_ERROR_CHECK(gpio_set_level(CAM_PIN_FLASH_LED, 0));
    ESP_LOGI(TAG, "flash LED on GPIO%d driven low", CAM_PIN_FLASH_LED);
#endif
    /* Nothing initialises WiFi or Bluetooth anywhere in this firmware. */
}

static esp_err_t camera_init_at(camera_fb_location_t where)
{
    camera_config_t config = {
        .pin_pwdn       = CAM_PIN_PWDN,
        .pin_reset      = CAM_PIN_RESET,
        .pin_xclk       = CAM_PIN_XCLK,
        .pin_sccb_sda   = CAM_PIN_SIOD,
        .pin_sccb_scl   = CAM_PIN_SIOC,
        .pin_d7         = CAM_PIN_D7,
        .pin_d6         = CAM_PIN_D6,
        .pin_d5         = CAM_PIN_D5,
        .pin_d4         = CAM_PIN_D4,
        .pin_d3         = CAM_PIN_D3,
        .pin_d2         = CAM_PIN_D2,
        .pin_d1         = CAM_PIN_D1,
        .pin_d0         = CAM_PIN_D0,
        .pin_vsync      = CAM_PIN_VSYNC,
        .pin_href       = CAM_PIN_HREF,
        .pin_pclk       = CAM_PIN_PCLK,

        .xclk_freq_hz   = CONFIG_TARGETCAM_XCLK_MHZ * 1000 * 1000,
        .ledc_timer     = LEDC_TIMER_0,
        .ledc_channel   = LEDC_CHANNEL_0,

        /* RGB565 straight from the sensor: two bytes per pixel, no JPEG
           encode or decode anywhere. Colour is the whole signal here, so
           unlike optflow_cam we cannot drop to grayscale. */
        .pixel_format   = PIXFORMAT_RGB565,
        .frame_size     = FRAME_SIZE,

        /* 2 x 38400 B. Internal DRAM first: it keeps the SPI PSRAM unclocked
           for pixel traffic (less heat) and runs on a board without PSRAM. */
        .fb_location    = where,
        .fb_count       = 2,
        .grab_mode      = CAMERA_GRAB_LATEST,

        .jpeg_quality   = 12,   /* unused in RGB565, must still be sane */
    };
    return esp_camera_init(&config);
}

/* '!flip 1' turns the image 180 deg on top of the build's VFLIP/HMIRROR. A
   student's board with the same parts delivered its image upside down, and a
   flip in the sensor (rather than on the PC) is what keeps everything after it
   right: +el above the nose, +az right of it, and the frames, labels and blob
   crops the same way up as every other camera's. Like set_wb(), before the
   sensor is up this only records the wish; camera_start() applies it. */
static void set_flip(bool on)
{
    sensor_t *s = esp_camera_sensor_get();
    if (s && s->set_vflip)   { s->set_vflip(s, CAM_VFLIP ^ (on ? 1 : 0)); }
    if (s && s->set_hmirror) { s->set_hmirror(s, CAM_HMIRROR ^ (on ? 1 : 0)); }
    s_flip = on;
}

static esp_err_t camera_start(void)
{
    esp_err_t err = camera_init_at(CAMERA_FB_IN_DRAM);
    if (err != ESP_OK && heap_caps_get_total_size(MALLOC_CAP_SPIRAM) > 0) {
        ESP_LOGW(TAG, "init with buffers in DRAM failed (%s); retrying in PSRAM",
                 esp_err_to_name(err));
        esp_camera_deinit();
        err = camera_init_at(CAMERA_FB_IN_PSRAM);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_camera_init failed: %s", esp_err_to_name(err));
        ESP_LOGE(TAG, "check the ribbon seating, and that the board is fed 5 V "
                      "- a camera that browns out looks exactly like this");
        return err;
    }

    sensor_t *s = esp_camera_sensor_get();
    if (s == NULL) {
        ESP_LOGE(TAG, "no sensor handle after init");
        return ESP_FAIL;
    }

    camera_sensor_info_t *info = esp_camera_sensor_get_info(&s->id);
    ESP_LOGI(TAG, "sensor PID 0x%02x (%s)", s->id.PID,
             info ? info->name : "unknown");

    /* Colour pipeline ON: white balance and its gains, a little extra
       saturation to pull markers away from walls, lens correction so the
       frame corners are not darker (and so less saturated) than the centre. */
    if (s->set_whitebal)    { s->set_whitebal(s, 1); }
    if (s->set_awb_gain)    { s->set_awb_gain(s, 1); }
    if (s->set_saturation)  { s->set_saturation(s, 1); }
    if (s->set_lenc)        { s->set_lenc(s, 1); }
    if (s->set_bpc)         { s->set_bpc(s, 1); }
    if (s->set_wpc)         { s->set_wpc(s, 1); }
    if (s->set_raw_gma)     { s->set_raw_gma(s, 1); }
    if (s->set_dcw)         { s->set_dcw(s, 1); }
    set_flip(s_flip);
    /* Same gain cap as optflow: high gain buys brightness with chroma noise,
       and chroma noise is exactly what scatters hue. */
    if (s->set_gain_ctrl)   { s->set_gain_ctrl(s, 1); }
    if (s->set_agc_gain)    { s->set_agc_gain(s, 0); }
    if (s->set_gainceiling) { s->set_gainceiling(s, GAINCEILING_4X); }
    if (s->set_exposure_ctrl) { s->set_exposure_ctrl(s, 1); }
    if (s->set_aec2)        { s->set_aec2(s, 0); }

    return ESP_OK;
}

static const char *wb_name(int m)
{
    return m == WB_FIXED ? "fixed" : m == WB_FROZEN ? "frozen" : "auto";
}

/* The gains every frame is made with, for #!CFG and the blob records:
   0,0,0 means not known, which is what auto and frozen are. A fixed gain is
   never 0, so the two cannot be confused. */
static void wb_gain_in_use(uint8_t g[3])
{
    for (int i = 0; i < 3; i++) g[i] = s_wb == WB_FIXED ? s_wb_gain[i] : 0;
}

/* The gains into 0xCC-0xCE, then manual mode on or off. The gains go first,
   so no frame is made with manual mode and the previous gains. */
static void wb_write(sensor_t *s, int m)
{
    if (s->set_reg == NULL) {
        return;
    }
    if (m == WB_FIXED) {
        for (int i = 0; i < 3; i++) {
            s->set_reg(s, DSP_AWB_GAIN_R + i, 0xFF, s_wb_gain[i]);
        }
    }
    s->set_reg(s, DSP_AWB_CTRL, 0x40, m == WB_FIXED ? 0x40 : 0);
}

/* Fixed sets AWB on as well as manual gains, the combination the driver's
   presets use (and so the one known to work); auto and frozen leave manual
   mode off, which is exactly what they were before v5. Any change of mode
   ends a 'Wc' that is running. Like set_flip(), before the sensor is up this
   only records the wish. */
static void set_wb(int m)
{
    if (s_wbcal > 0) {
        s_wbcal = 0;
        printf("# wb cal       cancelled\n#!WBCAL cancelled\n");
    }
    sensor_t *s = esp_camera_sensor_get();
    if (s) {
        wb_write(s, m);
        if (s->set_whitebal) { s->set_whitebal(s, m == WB_FROZEN ? 0 : 1); }
    }
    s_wb = m;
}

/* 'W': what the sensor really holds. Whether 0xCC-0xCE follow the AWB's own
   gains while it runs is not documented; reading them in a warm scene and
   then a cool one answers it. If they do, frozen gains could be recorded
   too. */
static void print_wb_regs(void)
{
    sensor_t *s = esp_camera_sensor_get();
    if (s == NULL || s->get_reg == NULL) {
        printf("# wb           no sensor to read\n");
        return;
    }
    int ctrl1 = s->get_reg(s, DSP_CTRL1, 0xFF);
    int c7 = s->get_reg(s, DSP_AWB_CTRL, 0xFF);
    int g[3];
    for (int i = 0; i < 3; i++) g[i] = s->get_reg(s, DSP_AWB_GAIN_R + i, 0xFF);
    printf("# wb           %s; sensor holds CTRL1 0x%02x (AWB %s, gains %s), "
           "C7 0x%02x (manual %s), CC CD CE = %d %d %d\n",
           wb_name(s_wb), ctrl1 & 0xFF, (ctrl1 & 0x08) ? "on" : "off",
           (ctrl1 & 0x04) ? "applied" : "off", c7 & 0xFF, (c7 & 0x40) ? "on" : "off",
           g[0], g[1], g[2]);
    printf("#!WB mode=%s ctrl1=%d c7=%d cc=%d cd=%d ce=%d\n", wb_name(s_wb),
           ctrl1, c7, g[0], g[1], g[2]);
}

/* ------------------------------------------------------------------ */
/* Live settings <-> the record kept in flash                          */

static void live_defaults(void)
{
    s_every_n        = every_n_for_ms(CONFIG_TARGETCAM_FRAME_PERIOD_MS);
    s_hfov_deg       = (float)CONFIG_TARGETCAM_HFOV_DEG;
    s_target_mm      = (float)CONFIG_TARGETCAM_TARGET_MM;
    s_cfg.min_area   = CONFIG_TARGETCAM_MIN_AREA;
    s_cfg.big_endian = 1;
    s_preset         = DEFAULT_PRESET;
    s_stage_req      = 0;
    s_cnn_k          = 2;
    s_thr2           = 0;
    apply_thr2();
    s_thr3           = 0;
    apply_thr3();
    s_models.cnn_k   = s_cnn_k;
    set_flip(false);
    blob_preset_apply(&s_cfg, s_preset);
    /* Before the camera starts there is no sensor yet; set_wb() then only
       records the wish, and app_main() applies it once the sensor is up. */
    s_wb_gain[0] = s_wb_gain[1] = s_wb_gain[2] = 64;
    set_wb(WB_AUTO);
}

/* Zeroed first, so the struct's padding is zero too and two captures of the
   same settings compare equal with memcmp(). */
static tc_settings_t live_capture(void)
{
    tc_settings_t t;
    memset(&t, 0, sizeof t);
    t.hue        = (int16_t)s_cfg.hue;
    t.hue_tol    = (int16_t)s_cfg.hue_tol;
    t.sat_min    = (int16_t)s_cfg.sat_min;
    t.val_min    = (int16_t)s_cfg.val_min;
    t.min_area   = s_cfg.min_area;
    t.target_mm  = (int32_t)s_target_mm;
    t.hfov_deg   = s_hfov_deg;
    t.period_ms  = (int16_t)lround(period_ms());
    t.awb        = (int8_t)s_wb;
    t.big_endian = (int8_t)s_cfg.big_endian;
    t.preset     = (int8_t)s_preset;
    t.stage      = (int8_t)s_stage_req;
    t.cnn_k      = (int8_t)s_cnn_k;
    t.flip       = s_flip ? 1 : 0;
    t.thr2       = (uint8_t)s_thr2;
    t.thr3       = (uint8_t)s_thr3;
    memcpy(t.wb_gain, s_wb_gain, sizeof t.wb_gain);
    return t;
}

/* Apply a stored record, or change nothing and return false if any field is
   outside the range its command accepts: a record from a buggy build must not
   become a detector that silently matches nothing. */
static bool live_apply(const tc_settings_t *t)
{
    if (t->hue < 0 || t->hue > 359 || t->hue_tol < 1 || t->hue_tol > 90 ||
        t->sat_min < 0 || t->sat_min > 255 || t->val_min < 0 || t->val_min > 255 ||
        t->min_area < 1 || t->min_area > FRAME_W * FRAME_H ||
        t->target_mm < 0 || t->target_mm > 5000 ||
        !(t->hfov_deg >= 10.0f && t->hfov_deg <= 150.0f) ||
        t->period_ms < 33 || t->period_ms > 1000 ||
        t->awb < WB_FROZEN || t->awb > WB_FIXED ||
        t->wb_gain[0] == 0 || t->wb_gain[1] == 0 || t->wb_gain[2] == 0 ||
        (t->big_endian != 0 && t->big_endian != 1) ||
        t->preset < -1 || t->preset >= BLOB_PRESET_COUNT ||
        t->stage < 0 || t->stage > TC_STAGE_MAX ||
        t->cnn_k < 1 || t->cnn_k > BLOB_MAX_REPORT ||
        (t->flip != 0 && t->flip != 1) || t->thr2 > 99 || t->thr3 > 99) {
        return false;
    }
    s_cfg.hue        = t->hue;
    s_cfg.hue_tol    = t->hue_tol;
    s_cfg.sat_min    = t->sat_min;
    s_cfg.val_min    = t->val_min;
    s_cfg.min_area   = t->min_area;
    s_cfg.big_endian = t->big_endian;
    s_target_mm      = (float)t->target_mm;
    s_hfov_deg       = t->hfov_deg;
    s_every_n        = every_n_for_ms(t->period_ms);
    s_preset         = t->preset;
    s_stage_req      = t->stage;
    s_cnn_k          = t->cnn_k;
    s_models.cnn_k   = s_cnn_k;
    s_thr2           = t->thr2;
    apply_thr2();
    s_thr3           = t->thr3;
    apply_thr3();
    /* Keep the preset's name only if its numbers still match: a firmware
       update may have retuned the preset since this record was saved. */
    if (s_preset >= 0) {
        blob_cfg_t p = s_cfg;
        blob_preset_apply(&p, s_preset);
        if (p.hue != s_cfg.hue || p.hue_tol != s_cfg.hue_tol ||
            p.sat_min != s_cfg.sat_min || p.val_min != s_cfg.val_min) {
            s_preset = -1;
        }
    }
    memcpy(s_wb_gain, t->wb_gain, sizeof s_wb_gain);
    set_wb(t->awb);
    set_flip(t->flip != 0);
    return true;
}

/* "saved" and "defaults" both mean a power cycle gives back exactly what is
   running now; "modified" means it would not. */
static const char *settings_state(void)
{
    tc_settings_t now = live_capture();
    if (memcmp(&now, &s_ref, sizeof now) != 0) {
        return "modified";
    }
    return s_ref_saved ? "saved" : "defaults";
}

/* The machine-readable config. Printed after every accepted command, so the
   viewer's controls always show what the camera really uses rather than what
   the PC last asked for. awb is the 'w' mode number (0 frozen, 1 auto, 2
   fixed) and wb its name; wb_gain is the gains the frames are made with
   (0,0,0 = not known) and wb_fixed the ones 'w2' would load. A saved frame's
   .json carries this line, so every frame says how its colour was made. */
static void print_cfg(void)
{
    uint8_t g[3];
    wb_gain_in_use(g);
    printf("#!CFG w=%d h=%d cell=%d hue=%d tol=%d sat=%d val=%d area=%d mm=%.0f "
           "fov=%.1f fps=%.3g period_ms=%.1f sensor_us=%.0f awb=%d wb=%s "
           "wb_gain=%d,%d,%d wb_fixed=%d,%d,%d be=%d preset=%s baud=%d image=%d stream=%d settings=%s stage=%d stage_eff=%d lut=%s lut_id=%u "
           "clf=%s cnn=%s cnn_k=%d csv=%d fw=%s collect=%d fv=%d flip=%d clf_id=%u "
           "thr2=%d clf_thr=%.2f cnn_id=%u thr3=%d cnn_thr=%.2f lut_src=%s clf_src=%s "
           "cnn_src=%s upload=%d\n",
           FRAME_W, FRAME_H, s_cfg.cell, s_cfg.hue, s_cfg.hue_tol, s_cfg.sat_min,
           s_cfg.val_min, s_cfg.min_area, s_target_mm, s_hfov_deg, frame_fps(),
           period_ms(), s_sensor_us, s_wb, wb_name(s_wb), g[0], g[1], g[2],
           s_wb_gain[0], s_wb_gain[1], s_wb_gain[2], s_cfg.big_endian,
           s_preset >= 0 ? blob_preset_name(s_preset) : "custom",
           link_baud(), s_image ? 1 : 0, s_streaming ? 1 : 0, settings_state(),
           s_stage_req, tc_stage_effective(s_stage_req, &s_models),
           s_lut_state, (unsigned)s_lut_id, s_clf_state, s_cnn_state, s_cnn_k, CSV_VERSION,
           esp_app_get_description()->version, s_collect ? 1 : 0, BLOB_FEAT_VERSION,
           s_flip ? 1 : 0, (unsigned)s_clf_id, s_thr2, (double)clf_threshold(),
           (unsigned)s_cnn_id, s_thr3, (double)cnn_threshold(), s_lut_src, s_clf_src,
           s_cnn_src, ms_available() ? 1 : 0);
}

/* ------------------------------------------------------------------ */

static void print_banner(void)
{
    float f = focal_px();
    printf("\n# ESP32-CAM forward colour target\n");
    printf("# frame        %dx%d RGB565 (%s-endian%s), XCLK %d MHz, sensor frame "
           "%.3f ms; every %d -> %.3g FPS (%.1f ms)\n",
           FRAME_W, FRAME_H, s_cfg.big_endian ? "big" : "little",
           s_flip ? ", turned 180 deg" : "",
           CONFIG_TARGETCAM_XCLK_MHZ, s_sensor_us / 1000.0, s_every_n, frame_fps(),
           period_ms());
    printf("# colour       %s: hue %d +/-%d deg, sat >= %d, val >= %d\n",
           s_preset >= 0 ? blob_preset_name(s_preset) : "custom",
           s_cfg.hue, s_cfg.hue_tol, s_cfg.sat_min, s_cfg.val_min);
    if (s_wb == WB_FIXED) {
        printf("# white bal.   fixed: gains R %d G %d B %d (64 = 1.0)\n",
               s_wb_gain[0], s_wb_gain[1], s_wb_gain[2]);
    } else {
        printf("# white bal.   %s (gains not known; fixed would use %d,%d,%d)\n",
               s_wb == WB_AUTO ? "auto" : "frozen", s_wb_gain[0], s_wb_gain[1],
               s_wb_gain[2]);
    }
    printf("# blobs        %dx%d cells, min area %d px, up to %d reported\n",
           s_cfg.cell, s_cfg.cell, s_cfg.min_area, BLOB_MAX_REPORT);
    printf("# geometry     h-FOV %.1f deg -> f %.1f px, %.3f deg/px at centre\n",
           s_hfov_deg, f, atanf(1.0f / f) * 180.0f / (float)M_PI);
    if (s_target_mm > 0.0f) {
        printf("# range        target %.0f mm; lost below min area beyond ~%.1f m\n",
               s_target_mm,
               s_target_mm * f / (2.0f * sqrtf((float)s_cfg.min_area / (float)M_PI))
               / 1000.0f);
    } else {
        printf("# range        off (set the target size with d<mm>)\n");
    }
    printf("# stage        %d requested, %d running (colour table %s, classifier %s, "
           "CNN %s)\n", s_stage_req, tc_stage_effective(s_stage_req, &s_models),
           s_lut_state, s_clf_state, s_cnn_state);
    const char *st = settings_state();
    printf("# settings     %s\n",
           strcmp(st, "saved") == 0    ? "saved (loaded from flash)" :
           strcmp(st, "defaults") == 0 ? "defaults (nothing saved in flash)" :
                                         "modified (not saved: !save keeps them)");
    printf("# commands     p<red|blue|green|orange> preset | c calibrate on centre | "
           "H<deg> hue | T<deg> tol | S<0-255> sat | V<0-255> val\n");
    printf("#              a<px> area | d<mm> size | F<deg> h-FOV |");
    printf(" f<fps> rate (f12.5 ok) | o byte order | m mask | s stream | ? info\n");
    printf("#              w<0|1|2> white balance frozen|auto|fixed | W<r>,<g>,<b> fixed "
           "gains | Wc gains from a white card | W sensor registers\n");
    printf("#              k config | g grab frame | v<0|1> every frame | "
           "B<baud> link speed | !save | !load | !reset\n");
    printf("#              !stage <0-3> detector level | "
           "!collect <0|1> blob records for training | !flip <0|1> image 180 deg\n");
    printf("#              !thr2 <0-99> classifier threshold, hundredths (0 = the model's)\n");
    printf("#              !thr3 <0-99> CNN threshold | !cnnk <1-4> blobs the CNN checks | "
           "!cnnbench [16|24] time the CNN\n");
    printf("#              !dump <lut|clf|cnn> send a model | !upload <kind> <len> <crc> "
           "store one | !unload <kind|all>\n");
    printf("# columns      t_ms,ok,cx_px,cy_px,az_deg,el_deg,area_px,w_px,h_px,"
           "fill,range_mm,clipped,blobs,matched_px,b2_az,b2_el,b2_range_mm,"
           "b3_az,b3_el,b3_range_mm,b4_az,b4_el,b4_range_mm,dt_ms,proc_us,"
           "stage,conf,b2_conf,b3_conf,b4_conf,t_px_us,t_b_us,t_c_us,loop_us,"
           "csv_v (v%d)\n", CSV_VERSION);
    fflush(stdout);
}

/* Parse an integer argument. Trailing junk is refused, not ignored: on the
   bench a burst of commands arrived with bytes dropped and changed, and
   "H33x" must not quietly become hue 33. */
static bool arg_in(const char *s, int lo, int hi, int *out)
{
    char *end;
    long v = strtol(s, &end, 10);
    while (*end == ' ') end++;
    if (end == s || *end != '\0' || v < lo || v > hi) {
        snprintf(s_why, sizeof s_why, "out of range (%d..%d)", lo, hi);
        return false;
    }
    *out = (int)v;
    return true;
}

/* How a command went, for the protocol line poll_commands() prints after it. */
typedef enum {
    CMD_NAK,        /* refused; s_why says why                                 */
    CMD_OK,         /* done: #!ACK, then #!CFG                                 */
    CMD_LATER,      /* accepted, finishes later: #!ACK now, no #!CFG yet       */
} cmd_res_t;

/* The '!' commands touch flash, so they are whole words: no single corrupted
   byte can turn into a write. */
static cmd_res_t run_store_command(const char *line)
{
    if (!s_nvs_ok) {
        snprintf(s_why, sizeof s_why, "flash storage (NVS) did not initialise");
        return CMD_NAK;
    }
    if (strcmp(line, "!save") == 0) {
        tc_settings_t t = live_capture();
        esp_err_t err = settings_save(&t);
        if (err != ESP_OK) {
            snprintf(s_why, sizeof s_why, "save failed: %s", esp_err_to_name(err));
            return CMD_NAK;
        }
        s_ref = t;
        s_ref_saved = true;
        printf("# settings saved to flash; every boot now starts with them\n");
        return CMD_OK;
    }
    if (strcmp(line, "!load") == 0) {
        tc_settings_t t;
        bool migrated;
        if (!settings_load(&t, &migrated)) {
            snprintf(s_why, sizeof s_why, "nothing saved in flash");
            return CMD_NAK;
        }
        if (!live_apply(&t)) {
            snprintf(s_why, sizeof s_why, "saved record is out of range; ignored");
            return CMD_NAK;
        }
        s_ref = live_capture();
        s_ref_saved = true;
        printf("# settings reloaded from flash%s\n",
               migrated ? " (v1 record: frame rate set to the build default)" : "");
        return CMD_OK;
    }
    if (strcmp(line, "!reset") == 0) {
        esp_err_t err = settings_erase();
        if (err != ESP_OK) {
            snprintf(s_why, sizeof s_why, "erase failed: %s", esp_err_to_name(err));
            return CMD_NAK;
        }
        live_defaults();
        s_ref = live_capture();
        s_ref_saved = false;
        printf("# saved settings erased; running the Kconfig defaults\n");
        return CMD_OK;
    }
    /* s_why is 64 bytes: the full list is in the banner ('?'). */
    snprintf(s_why, sizeof s_why, "unknown '!' command ('?' lists them)");
    return CMD_NAK;
}

/* '!stage N'. A whole word even though it writes no flash: one corrupted
   byte must not be able to change the detector level in flight. It needs no
   NVS either, so it is handled before the store commands. Persisting it is
   !save's job, as for every other setting. */
static cmd_res_t run_stage_command(const char *line)
{
    int v;
    if (line[6] != ' ' || !arg_in(line + 7, 0, TC_STAGE_MAX, &v)) {
        snprintf(s_why, sizeof s_why, "usage: !stage <0-%d>", TC_STAGE_MAX);
        return CMD_NAK;
    }
    s_stage_req = v;
    int eff = tc_stage_effective(v, &s_models);
    if (eff == v) {
        printf("# stage %d\n", v);
    } else {
        printf("# stage %d requested, running %d: the model for stage %d is %s\n",
               v, eff, eff + 1,
               eff == 0 ? s_lut_state : eff == 1 ? s_clf_state : s_cnn_state);
    }
    if (eff >= 1) {
        printf("# stage %d: the colour table decides which pixels match; hue, tol, "
               "sat, val and 'c' have no effect until !stage 0\n", eff);
    }
    return CMD_OK;
}

/* '!thr2 N', 0..99: the classifier's threshold in hundredths, 0 for the
   model's own. A whole word for the same reason as !stage: a stray byte must
   not change which blobs the blimp is allowed to steer to. Accepted without
   a classifier too (it is a setting, kept for the next one); the reply says
   it has no effect then. */
static cmd_res_t run_thr2_command(const char *line)
{
    int v;
    if (line[5] != ' ' || !arg_in(line + 6, 0, 99, &v)) {
        snprintf(s_why, sizeof s_why, "usage: !thr2 <0-99> (hundredths; 0 = the model's own)");
        return CMD_NAK;
    }
    s_thr2 = v;
    apply_thr2();
    if (s_models.clf == NULL) {
        printf("# thr2 %d kept, but no classifier is running (clf=%s): no effect now\n",
               v, s_clf_state);
    } else {
        printf("# classifier threshold %.2f (%s)\n", (double)clf_threshold(),
               v ? "set by !thr2" : "the model's own");
    }
    return CMD_OK;
}

/* '!thr3 N', 0..99: the CNN's threshold, as !thr2 is the classifier's. */
static cmd_res_t run_thr3_command(const char *line)
{
    int v;
    if (line[5] != ' ' || !arg_in(line + 6, 0, 99, &v)) {
        snprintf(s_why, sizeof s_why, "usage: !thr3 <0-99> (hundredths; 0 = the model's own)");
        return CMD_NAK;
    }
    s_thr3 = v;
    apply_thr3();
    if (s_models.cnn == NULL) {
        printf("# thr3 %d kept, but no CNN is running (cnn=%s): no effect now\n",
               v, s_cnn_state);
    } else {
        printf("# CNN threshold %.2f (%s)\n", (double)cnn_threshold(),
               v ? "set by !thr3" : "the model's own");
    }
    return CMD_OK;
}

/* '!cnnk N', 1..4: how many of the blobs stage 2 kept the CNN checks, the
   largest first. Each costs a crop's worth of multiply-adds (!cnnbench); the
   ones past N are reported with stage 2's verdict. A whole word: it changes
   which blobs are verified in flight. */
static cmd_res_t run_cnnk_command(const char *line)
{
    int v;
    if (line[5] != ' ' || !arg_in(line + 6, 1, BLOB_MAX_REPORT, &v)) {
        snprintf(s_why, sizeof s_why, "usage: !cnnk <1-%d>", BLOB_MAX_REPORT);
        return CMD_NAK;
    }
    s_cnn_k = v;
    s_models.cnn_k = v;
    printf("# the CNN checks the %d largest blob(s) stage 2 keeps\n", v);
    return CMD_OK;
}

/* '!cnnbench [16|24]': phase C0's measurement, on the board. Runs the CNN on
   a fixed synthetic crop 20 times and prints the time per crop and the
   cycles per multiply-add, so the input size and cnn_k can be chosen from a
   measured cost instead of the 4-10 cycles estimate (feasibility report).
   With a CNN built in it times that one (the argument must match its input
   size, or is left out); without one, a network of the default shape (8, 16,
   16 channels) with zero weights, whose cost is the same, since the loops do
   not depend on the values. It stalls the loop for 20 crops (~0.1-0.4 s):
   a bench command, one late row. */
static cmd_res_t run_cnnbench_command(const char *line)
{
    int n = 0;
    if (line[9] == ' ' && !arg_in(line + 10, 16, 24, &n)) n = -1;
    if (line[9] != ' ' && line[9] != '\0') n = -1;
    if (n != 0 && n != 16 && n != 24) {
        snprintf(s_why, sizeof s_why, "usage: !cnnbench [16|24]");
        return CMD_NAK;
    }
    static blob_cnn_t dummy;
    const blob_cnn_t *net = NULL;
    if (s_models.cnn != NULL && (n == 0 || n == s_cnn.n)) net = &s_cnn;
    if (net == NULL) {
        static const int ch[3] = { 8, 16, 16 };
        memset(&dummy, 0, sizeof dummy);
        dummy.n = n ? n : 16;
        dummy.threshold = 0.5f;
        dummy.logit_scale = 1.0f;
        int cin = 3;
        for (int l = 0; l < 3; l++) {
            dummy.conv[l].cin = cin;
            dummy.conv[l].cout = ch[l];
            dummy.conv[l].mult = 1;
            dummy.conv[l].shift = 1;
            cin = ch[l];
        }
        net = &dummy;
    }
    static uint16_t crop[24 * 24];
    for (int i = 0; i < net->n * net->n; i++) crop[i] = (uint16_t)(i * 2654435761u >> 16);
    const int reps = 20;
    volatile float sink = 0;
    int64_t t0 = esp_timer_get_time();
    for (int r = 0; r < reps; r++) sink += blob_cnn_conf(net, crop);
    int64_t us = esp_timer_get_time() - t0;
    (void)sink;
    double per = (double)us / reps;
    long macs = blob_cnn_macs(net);
    printf("#!CNNBENCH n=%d channels=%d,%d,%d macs=%ld us_per_crop=%.0f cycles_per_mac=%.2f "
           "model=%s\n", net->n, net->conv[0].cout, net->conv[1].cout, net->conv[2].cout,
           macs, per, per * CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ / (double)macs,
           net == &dummy ? "dummy" : "built-in");
    return CMD_OK;
}

/* Phase D: model records over the link (model_store.h). '!dump' and
   '!upload' are served by the main loop (serve_model_transfer), after the
   command's #!ACK, so the PC never mistakes a packet or a "ready" for the
   reply, and sends an upload's bytes only once the slot is erased. */
static int      s_dump = -1;          /* kind to send, or -1                  */
static struct {
    int      kind;                    /* -1 = none pending                    */
    uint32_t len, crc;
} s_up = { -1, 0, 0 };

static bool kind_arg(const char *a, int *k)
{
    while (*a == ' ') a++;
    *k = ms_kind(a);
    return *k >= 0;
}

/* '!dump <lut|clf|cnn>': send the record of the model that is running, as a
   #!MODEL packet, so the PC can keep a copy of what the camera really holds
   (the Firmware tab's backup), not of what main/models says it should. */
static cmd_res_t run_dump_command(const char *line)
{
    int k;
    if (line[5] != ' ' || !kind_arg(line + 6, &k)) {
        snprintf(s_why, sizeof s_why, "usage: !dump <lut|clf|cnn>");
        return CMD_NAK;
    }
    s_dump = k;
    return CMD_OK;
}

/* '!upload <lut|clf|cnn> <len> <crc hex>': the next len bytes are a model
   record with that CRC-32. A whole word, as every command that writes flash
   is, and checked three times: the transfer's CRC, then the record's own
   checks in the loader, then the table binding. */
static cmd_res_t run_upload_command(const char *line)
{
    char kind[8];
    unsigned long len, crc;
    int used = 0;
    if (line[7] != ' ' || sscanf(line + 8, "%7s %lu %lx%n", kind, &len, &crc, &used) != 3 ||
        line[8 + used] != '\0') {
        snprintf(s_why, sizeof s_why, "usage: !upload <lut|clf|cnn> <len> <crc32 hex>");
        return CMD_NAK;
    }
    int k = ms_kind(kind);
    if (k < 0) {
        snprintf(s_why, sizeof s_why, "kind must be lut, clf or cnn");
        return CMD_NAK;
    }
    if (!ms_available()) {
        snprintf(s_why, sizeof s_why, "no 'tcmodels' partition: rebuild and flash");
        return CMD_NAK;
    }
    if (len < TC_MODEL_HDR_BYTES || len > ms_capacity((ms_kind_t)k)) {
        snprintf(s_why, sizeof s_why, "len out of range (%d..%u)", TC_MODEL_HDR_BYTES,
                 (unsigned)ms_capacity((ms_kind_t)k));
        return CMD_NAK;
    }
    s_up.kind = k;
    s_up.len = (uint32_t)len;
    s_up.crc = (uint32_t)crc;
    return CMD_OK;
}

/* '!unload <lut|clf|cnn|all>': erase upload slots; the compiled-in models
   run again at once. */
static cmd_res_t run_unload_command(const char *line)
{
    int k = -1;
    bool all = strcmp(line, "!unload all") == 0;
    if (!all && (line[7] != ' ' || !kind_arg(line + 8, &k))) {
        snprintf(s_why, sizeof s_why, "usage: !unload <lut|clf|cnn|all>");
        return CMD_NAK;
    }
    if (!ms_available()) {
        snprintf(s_why, sizeof s_why, "no 'tcmodels' partition: nothing is uploaded");
        return CMD_NAK;
    }
    for (int i = 0; i < MS_KINDS; i++) {
        if ((all || i == k) && ms_erase((ms_kind_t)i) != ESP_OK) {
            snprintf(s_why, sizeof s_why, "flash erase failed (%s)", ms_name((ms_kind_t)i));
            return CMD_NAK;
        }
    }
    printf("# unloaded %s: the build's models run again\n", all ? "all" : ms_name((ms_kind_t)k));
    reload_models();
    return CMD_OK;
}

/* The record of a running model: the upload slot's (mapped; *mapped set) or
   the build's. NULL when none of that kind runs. */
static const uint8_t *running_record(int k, size_t *len, esp_partition_mmap_handle_t *h,
                                     bool *mapped, const char **src)
{
    const char *srcs[MS_KINDS] = { s_lut_src, s_clf_src, s_cnn_src };
    *src = srcs[k];
    *mapped = false;
    if (strcmp(*src, "upload") == 0) {
        const uint8_t *r = ms_map((ms_kind_t)k, len, h);
        *mapped = r != NULL;
        return r;
    }
    if (strcmp(*src, "build") != 0) return NULL;
    if (k == MS_LUT) { *len = sizeof lut_model_rec; return lut_model_rec; }
    if (k == MS_CLF) { *len = CLF_BUILD_LEN; return CLF_BUILD_REC; }
    *len = CNN_BUILD_LEN;
    return CNN_BUILD_REC;
}

static void upload_refused(int k, const char *why)
{
    ms_erase((ms_kind_t)k);
    printf("#!UPLOAD refused kind=%s reason=%s\n", ms_name((ms_kind_t)k), why);
}

static void serve_model_transfer(void)
{
    if (s_dump >= 0) {
        int k = s_dump;
        s_dump = -1;
        size_t len = 0;
        esp_partition_mmap_handle_t h;
        bool mapped;
        const char *src;
        const uint8_t *rec = running_record(k, &len, &h, &mapped, &src);
        if (rec == NULL) {
            printf("#!MODEL none kind=%s src=%s\n", ms_name((ms_kind_t)k), src);
        } else {
            char fields[64];
            snprintf(fields, sizeof fields, "kind=%s src=%s", ms_name((ms_kind_t)k), src);
            link_send_packet("MODEL", fields, rec, len);
            if (mapped) ms_unmap(h);
        }
        fflush(stdout);
    }
    if (s_up.kind < 0) return;
    int k = s_up.kind;
    uint32_t len = s_up.len, want = s_up.crc;
    s_up.kind = -1;
    if (ms_erase((ms_kind_t)k) != ESP_OK) {
        printf("#!UPLOAD refused kind=%s reason=erase\n", ms_name((ms_kind_t)k));
        return;
    }
    printf("#!UPLOAD ready kind=%s len=%u\n", ms_name((ms_kind_t)k), (unsigned)len);
    fflush(stdout);
    /* The bytes go to flash as they arrive: no 16 kB buffer in DRAM. A
       256-byte write takes well under a millisecond, far less than the UART
       ring buffer holds at any speed the link uses. */
    int64_t deadline = esp_timer_get_time() +
                       (int64_t)len * 10 * 1000000 / link_baud() * 3 / 2 + 3000000;
    uint8_t chunk[256];
    uint32_t got = 0, crc = 0;
    while (got < len && esp_timer_get_time() < deadline) {
        size_t want_n = len - got < sizeof chunk ? len - got : sizeof chunk;
        int n = uart_read_bytes(UART_NUM_0, chunk, want_n, pdMS_TO_TICKS(100));
        if (n <= 0) continue;
        crc = tc_crc32_update(crc, chunk, (size_t)n);
        if (ms_write((ms_kind_t)k, got, chunk, (size_t)n) != ESP_OK) {
            upload_refused(k, "write");
            return;
        }
        got += (uint32_t)n;
    }
    char why[48];
    if (got < len) {
        snprintf(why, sizeof why, "timeout_%u_of_%u", (unsigned)got, (unsigned)len);
        upload_refused(k, why);
        return;
    }
    if (crc != want) {
        upload_refused(k, "crc");
        return;
    }
    if (ms_commit((ms_kind_t)k, len) != ESP_OK) {
        upload_refused(k, "commit");
        return;
    }
    reload_models();
    const char *srcs[MS_KINDS] = { s_lut_src, s_clf_src, s_cnn_src };
    const char *states[MS_KINDS] = { s_lut_state, s_clf_state, s_cnn_state };
    uint32_t ids[MS_KINDS] = { s_lut_id, s_clf_id, s_cnn_id };
    if (strcmp(srcs[k], "upload") != 0) {
        /* The loader refused it (the reason is in the log above): the slot
           is erased, so the build's model is not shadowed at the next boot. */
        upload_refused(k, "record");
        reload_models();
        return;
    }
    printf("#!UPLOAD ok kind=%s id=%u state=%s\n", ms_name((ms_kind_t)k), (unsigned)ids[k],
           states[k]);
    print_cfg();
    fflush(stdout);
}

/* '!collect 0|1'. A whole word for the same reason as !stage: it slows the
   loop by the records' transfer time, so no stray byte may switch it on. */
static cmd_res_t run_collect_command(const char *line)
{
    int v;
    if (line[8] != ' ' || !arg_in(line + 9, 0, 1, &v)) {
        snprintf(s_why, sizeof s_why, "usage: !collect <0|1>");
        return CMD_NAK;
    }
    s_collect = v != 0;
    s_collect_n = 0;
    if (s_collect) {
        printf("# collect on: features and a %dx%d crop of each reported blob, every "
               "%d frames (timing is NOT flight-representative)\n",
               BLOB_CROP, BLOB_CROP, COLLECT_EVERY);
    } else {
        printf("# collect off\n");
    }
    return CMD_OK;
}

/* '!flip 0|1'. A whole word for the same reason as !stage: a stray byte that
   turned the image over in flight would flip the sign of every az and el. */
static cmd_res_t run_flip_command(const char *line)
{
    int v;
    if (line[5] != ' ' || !arg_in(line + 6, 0, 1, &v)) {
        snprintf(s_why, sizeof s_why, "usage: !flip <0|1>");
        return CMD_NAK;
    }
    set_flip(v != 0);
    printf("# image %s\n", s_flip ? "turned 180 deg" : "as the build mounts it");
    return CMD_OK;
}

/* 'W' alone reads the registers back; 'Wc' starts a white-card calibration;
   'W<r>,<g>,<b>' sets fixed gains and switches to them. */
static cmd_res_t run_wb_command(const char *line)
{
    const char *a = line + 1;
    if (*a == '\0') {
        print_wb_regs();
        return CMD_OK;
    }
    if (strcmp(a, "c") == 0) {
        if (s_wbcal > 0) {
            snprintf(s_why, sizeof s_why, "a white-card calibration is already running");
            return CMD_NAK;
        }
        s_wbcal_prev = s_wb;
        memcpy(s_wbcal_prev_gain, s_wb_gain, sizeof s_wb_gain);
        set_wb(WB_FIXED);
        s_wbcal = WBCAL_STEPS;
        s_wbcal_wait = WBCAL_SETTLE;
        for (int i = 0; i < 2; i++) {
            s_wbcal_ch[i] = (wbcal_ch_t){ .lo = -1, .hi = -1 };
        }
        printf("# wb cal       hold a white card over the centre; starting from "
               "%d,%d,%d\n", s_wb_gain[0], s_wb_gain[1], s_wb_gain[2]);
        return CMD_LATER;               /* #!WBCAL, then #!CFG, when it ends */
    }
    /* Three integers, strictly: as with arg_in(), trailing junk is refused. */
    long g[3];
    bool ok = true;
    for (int i = 0; i < 3 && ok; i++) {
        char *end;
        g[i] = strtol(a, &end, 10);
        ok = end != a && g[i] >= 1 && g[i] <= 255;
        if (ok && i < 2) {
            ok = *end == ',';
            end++;
        }
        a = end;
    }
    while (ok && *a == ' ') a++;
    if (!ok || *a != '\0') {
        snprintf(s_why, sizeof s_why, "usage: W | Wc | W<r>,<g>,<b> (1..255, 64 = 1.0)");
        return CMD_NAK;
    }
    for (int i = 0; i < 3; i++) s_wb_gain[i] = (uint8_t)g[i];
    set_wb(WB_FIXED);
    printf("# white balance fixed: gains %d,%d,%d\n",
           s_wb_gain[0], s_wb_gain[1], s_wb_gain[2]);
    return CMD_OK;
}

static cmd_res_t run_command(const char *line)
{
    int v;
    /* The letters that take no argument must arrive alone. On the bench the
       PC's tty echoed fragments of the camera's own output back to it as the
       port opened (2026-10-14: "^K", "...is mapped"); with the rest of the
       line ignored, a fragment starting at "cell=2 ..." would recalibrate the
       colour and one at "stream=1 ..." would stop the rows. */
    if (strchr("comskg?", line[0]) != NULL && line[1] != '\0') {
        snprintf(s_why, sizeof s_why, "'%c' takes no argument", line[0]);
        return CMD_NAK;
    }
    switch (line[0]) {
    case '!':
        if (strncmp(line, "!stage", 6) == 0 && (line[6] == ' ' || line[6] == '\0')) {
            return run_stage_command(line);
        }
        if (strncmp(line, "!collect", 8) == 0 && (line[8] == ' ' || line[8] == '\0')) {
            return run_collect_command(line);
        }
        if (strncmp(line, "!flip", 5) == 0 && (line[5] == ' ' || line[5] == '\0')) {
            return run_flip_command(line);
        }
        if (strncmp(line, "!thr2", 5) == 0 && (line[5] == ' ' || line[5] == '\0')) {
            return run_thr2_command(line);
        }
        if (strncmp(line, "!thr3", 5) == 0 && (line[5] == ' ' || line[5] == '\0')) {
            return run_thr3_command(line);
        }
        if (strncmp(line, "!cnnk", 5) == 0 && (line[5] == ' ' || line[5] == '\0')) {
            return run_cnnk_command(line);
        }
        if (strncmp(line, "!cnnbench", 9) == 0 && (line[9] == ' ' || line[9] == '\0')) {
            return run_cnnbench_command(line);
        }
        if (strncmp(line, "!dump", 5) == 0 && (line[5] == ' ' || line[5] == '\0')) {
            return run_dump_command(line);
        }
        if (strncmp(line, "!upload", 7) == 0 && (line[7] == ' ' || line[7] == '\0')) {
            return run_upload_command(line);
        }
        if (strncmp(line, "!unload", 7) == 0 && (line[7] == ' ' || line[7] == '\0')) {
            return run_unload_command(line);
        }
        return run_store_command(line);
    case 'c':
        s_calibrate = true;
        return CMD_LATER;               /* #!CFG follows the calibration */
    case 'p': {
        /* 'p red', 'pred', 'p0' and 'pr' all select red. */
        const char *a = line + 1;
        while (*a == ' ') a++;
        int p = (*a >= '0' && *a <= '9') ? atoi(a) : blob_preset_find(a);
        if (!blob_preset_apply(&s_cfg, p)) {
            snprintf(s_why, sizeof s_why, "unknown preset (red, blue, green, orange)");
            return CMD_NAK;
        }
        s_preset = p;
        printf("# preset %s: hue %d +/-%d, sat >= %d, val >= %d\n",
               blob_preset_name(p), s_cfg.hue, s_cfg.hue_tol,
               s_cfg.sat_min, s_cfg.val_min);
        return CMD_OK;
    }
    case 'H':
        if (!arg_in(line + 1, 0, 359, &v)) return CMD_NAK;
        s_cfg.hue = v; s_preset = -1; printf("# hue %d\n", v);
        return CMD_OK;
    case 'T':
        if (!arg_in(line + 1, 1, 90, &v)) return CMD_NAK;
        s_cfg.hue_tol = v; s_preset = -1; printf("# hue tol %d\n", v);
        return CMD_OK;
    case 'S':
        if (!arg_in(line + 1, 0, 255, &v)) return CMD_NAK;
        s_cfg.sat_min = v; s_preset = -1; printf("# sat min %d\n", v);
        return CMD_OK;
    case 'V':
        if (!arg_in(line + 1, 0, 255, &v)) return CMD_NAK;
        s_cfg.val_min = v; s_preset = -1; printf("# val min %d\n", v);
        return CMD_OK;
    case 'a':
        if (!arg_in(line + 1, 1, FRAME_W * FRAME_H, &v)) return CMD_NAK;
        s_cfg.min_area = v;
        printf("# min area %d px\n", v);
        return CMD_OK;
    case 'd':
        if (!arg_in(line + 1, 0, 5000, &v)) return CMD_NAK;
        s_target_mm = (float)v;
        printf("# target size %d mm%s\n", v, v ? "" : " (range off)");
        return CMD_OK;
    case 'F': {
        /* Fractional, because the FOV is measured, and 1 deg of h-FOV is ~2%
           of every bearing and range. */
        char *end;
        float f = strtof(line + 1, &end);
        while (*end == ' ') end++;
        if (end == line + 1 || *end != '\0' || !(f >= 10.0f && f <= 150.0f)) {
            snprintf(s_why, sizeof s_why, "out of range (10..150)");
            return CMD_NAK;
        }
        s_hfov_deg = f;
        printf("# h-FOV %.1f deg -> f %.1f px\n", f, focal_px());
        return CMD_OK;
    }
    case 'f': {
        /* Fractional, because the sensor's own rate is 12.5. The sensor is the
           clock, so the rate is rounded to a whole number of its frames and
           the reply says what was really set: f15 and f12.5 both give every
           frame (80 ms), f6.25 every second one, f10 rounds back to 12.5. */
        char *end;
        float f = strtof(line + 1, &end);
        while (*end == ' ') end++;
        if (end == line + 1 || *end != '\0' || !(f >= 1.0f && f <= 30.0f)) {
            snprintf(s_why, sizeof s_why, "out of range (1..30)");
            return CMD_NAK;
        }
        s_every_n = every_n_for_ms(1000.0 / f);
        printf("# frame period %.1f ms (%.3g FPS): every %d sensor frame%s\n",
               period_ms(), frame_fps(), s_every_n, s_every_n == 1 ? "" : "s");
        return CMD_OK;
    }
    case 'w':
        if (!arg_in(line + 1, WB_FROZEN, WB_FIXED, &v)) return CMD_NAK;
        set_wb(v);
        if (v == WB_FIXED) {
            printf("# white balance fixed: gains %d,%d,%d\n",
                   s_wb_gain[0], s_wb_gain[1], s_wb_gain[2]);
        } else {
            printf("# white balance %s\n", v == WB_AUTO ? "auto" : "frozen at current gains");
        }
        return CMD_OK;
    case 'W':
        return run_wb_command(line);
    case 'o':
        s_cfg.big_endian = !s_cfg.big_endian;
        printf("# RGB565 byte order now %s-endian\n",
               s_cfg.big_endian ? "big" : "little");
        return CMD_OK;
    case 'm':
        s_dump_mask = true;
        return CMD_OK;
    case 's':
        s_streaming = !s_streaming;
        printf("# streaming %s\n", s_streaming ? "on" : "off");
        return CMD_OK;
    case 'k':
        return CMD_OK;                  /* the #!CFG line is the answer */
    case 'g':
        s_grab = true;
        return CMD_OK;
    case 'v':
        if (!arg_in(line + 1, 0, 1, &v)) return CMD_NAK;
        s_image = v != 0;
        printf("# image %s\n", s_image ? "every frame (timing is NOT flight-representative)"
                                       : "off");
        return CMD_OK;
    case 'B':
        if (!arg_in(line + 1, 1, 4000000, &v)) return CMD_NAK;
        if (!link_baud_allowed(v)) {
            snprintf(s_why, sizeof s_why,
                     "unsupported (115200 230400 460800 921600 1500000 2000000)");
            return CMD_NAK;
        }
        s_pending_baud = v;
        return CMD_LATER;               /* the viewer confirms with 'k' */
    case '?':
        print_banner();
        return CMD_OK;
    default:
        snprintf(s_why, sizeof s_why, "unknown command");
        return CMD_NAK;
    }
}

/*
 * Commands are case-sensitive: 'S' sets saturation, 's' toggles the stream.
 * Read without blocking so pacing is never disturbed.
 *
 * Every line gets "#!ACK <line>" or "#!NAK <line> -- <reason>", so the viewer
 * knows each command arrived intact instead of assuming it did; the human
 * "# ..." lines are unchanged, for a plain terminal and target_monitor.py.
 */
static void poll_commands(void)
{
    static char line[32];
    static int  len;
    uint8_t ch;

    while (uart_read_bytes(UART_NUM_0, &ch, 1, 0) == 1) {
        if (ch != '\r' && ch != '\n') {
            if (len < (int)sizeof(line) - 1) {
                line[len++] = (char)ch;
            }
            continue;
        }
        line[len] = '\0';
        if (len == 0) {
            continue;
        }
        len = 0;
        cmd_res_t res = run_command(line);
        if (res != CMD_NAK) {
            /* Only a line that parsed as a command proves the PC can talk at
               this baud; line noise can end in a newline by chance. */
            link_note_command();
        }
        if (res == CMD_NAK) {
            printf("# %s\n", s_why);
            printf("#!NAK %s -- %s\n", line, s_why);
        } else {
            printf("#!ACK %s\n", line);
            if (res == CMD_OK) {
                print_cfg();
            }
        }
        fflush(stdout);
        if (s_pending_baud) {
            /* After the ACK, which has to go out at the speed the PC is
               still listening on. */
            link_set_baud(s_pending_baud);
            s_pending_baud = 0;
        }
    }
}

/* Commands are served after the frame has been handed back and its row is
   out. A reply is ~400 B (ACK + #!CFG), ~24 ms of blocking print; served
   before the grab it pushed one capture late and the next early (dt 92 then
   68 on the bench), and served while the frame was held it starved the
   driver of a buffer. */
static void serve_commands(void)
{
    poll_commands();
    /* After poll_commands(), so a confirming command that queued up while
       a slow frame was going out counts before the revert deadline. */
    link_poll();
}

/*
 * 'c': take the colour of whatever is in the centre patch and make it the
 * target. Thresholds are set relative to what was seen, not to fixed numbers,
 * so a pastel target and a fluorescent one both calibrate sensibly - and the
 * warnings say when the target is a poor choice rather than failing later.
 */
static void calibrate(const uint8_t *frame)
{
    blob_color_t c;
    blob_sample(&s_cfg, frame, (FRAME_W - CAL_PATCH) / 2, (FRAME_H - CAL_PATCH) / 2,
                CAL_PATCH, CAL_PATCH, &c);

    int tol = 3 * c.hue_spread;
    if (tol < 10) tol = 10;
    if (tol > 40) tol = 40;
    int sat = c.sat / 2;
    if (sat < 40) sat = 40;
    int val = c.val * 2 / 5;
    if (val < 25) val = 25;

    printf("# calibrate    centre %dx%d: hue %d +/-%d, sat %d, val %d\n",
           CAL_PATCH, CAL_PATCH, c.hue, c.hue_spread, c.sat, c.val);
    printf("#!CAL %s hue=%d spread=%d sat=%d val=%d\n",
           c.sat < 70 ? "refused" : "ok", c.hue, c.hue_spread, c.sat, c.val);
    if (c.sat < 70) {
        printf("# calibrate    REFUSED: saturation %d is too low to separate from "
               "walls. Fill the centre with the target, or use a more vivid one.\n",
               c.sat);
        return;
    }
    if (c.hue_spread > 20) {
        printf("# calibrate    WARNING: hue spread %d deg - the patch is not one "
               "colour. Is the target filling the centre?\n", c.hue_spread);
    }
    s_preset = -1;
    s_cfg.hue = c.hue;
    s_cfg.hue_tol = tol;
    s_cfg.sat_min = sat;
    s_cfg.val_min = val;
    printf("# calibrate    set H%d T%d S%d V%d\n", s_cfg.hue, s_cfg.hue_tol,
           s_cfg.sat_min, s_cfg.val_min);
}

/* Mean R, G, B of the centre patch on a 0..255 scale, and how many of its
   pixels have each channel at full scale (a clipped channel reads low, not
   true, so it would pull the gains wrong). */
static void patch_rgb(const uint8_t *frame, float m[3], int clipped[3])
{
    int x0 = (FRAME_W - CAL_PATCH) / 2, y0 = (FRAME_H - CAL_PATCH) / 2;
    long sum[3] = {0, 0, 0};
    clipped[0] = clipped[1] = clipped[2] = 0;
    for (int y = y0; y < y0 + CAL_PATCH; y++) {
        for (int x = x0; x < x0 + CAL_PATCH; x++) {
            uint16_t p = blob_pixel(&s_cfg, frame, x, y);
            int r = p >> 11, g = (p >> 5) & 0x3F, b = p & 0x1F;
            sum[0] += r; sum[1] += g; sum[2] += b;
            clipped[0] += r == 0x1F;
            clipped[1] += g == 0x3F;
            clipped[2] += b == 0x1F;
        }
    }
    const float n = (float)(CAL_PATCH * CAL_PATCH);
    m[0] = (float)sum[0] * 255.0f / 31.0f / n;
    m[1] = (float)sum[1] * 255.0f / 63.0f / n;
    m[2] = (float)sum[2] * 255.0f / 31.0f / n;
}

/* The next register value for one gain, from its reading e = channel/G - 1
   at value v. Returns -1 if it needs more than 16..255 can give. */
static int wbcal_next(wbcal_ch_t *c, int v, float e)
{
    if (fabsf(e) <= WBCAL_TOL) {
        c->done = true;
        return v;
    }
    if (e < 0) { c->lo = v; c->elo = e; } else { c->hi = v; c->ehi = e; }
    if (c->lo >= 0 && c->hi >= 0) {
        if (c->hi - c->lo <= 1) {
            /* The register cannot land between them: take the closer. */
            c->done = true;
            return fabsf(c->elo) <= fabsf(c->ehi) ? c->lo : c->hi;
        }
        int n = (int)lroundf((float)c->lo
                             + (float)(c->hi - c->lo) * -c->elo / (c->ehi - c->elo));
        return n <= c->lo ? c->lo + 1 : n >= c->hi ? c->hi - 1 : n;
    }
    /* Not bracketed yet: half the proportional step (in log), because the
       measured response is steeper than proportional, and at least 1. */
    int n = (int)lroundf((float)v / sqrtf(1.0f + e));
    if (n == v) {
        n += e < 0 ? 1 : -1;
    }
    if (n < WBCAL_GAIN_MIN) n = WBCAL_GAIN_MIN;
    if (n > 255) n = 255;
    return n == v ? -1 : n;             /* at a limit and still short: refuse */
}

/* One step of 'Wc', on a frame made WBCAL_SETTLE frames after the last
   change. Every step is printed, so a calibration that does not converge
   shows on the bench how it failed. A refused one puts the mode and gains
   back as they were. */
static void wb_cal_step(const uint8_t *frame)
{
    if (s_wbcal_wait > 0) {
        s_wbcal_wait--;
        return;
    }
    float m[3];
    int clip[3];
    patch_rgb(frame, m, clip);
    int step = WBCAL_STEPS - s_wbcal + 1;
    printf("# wb cal %2d    centre R %.0f G %.0f B %.0f (clipped px R %d G %d B %d) with "
           "gains %d,%d,%d\n", step, m[0], m[1], m[2], clip[0], clip[1], clip[2],
           s_wb_gain[0], s_wb_gain[1], s_wb_gain[2]);
    const char *why = NULL, *tag = NULL;
    if (m[1] < WBCAL_MIN_G) {
        tag = "dark";
        why = "the centre is too dark: fill it with the white card, and light it";
    } else if (clip[1] > WBCAL_CLIP || (step == 1 && (clip[0] > WBCAL_CLIP
                                                      || clip[2] > WBCAL_CLIP))) {
        /* G is never changed here, and at the start nothing has been: the
           card itself is too bright. Later, R or B clipping is a trial gain
           too strong, which the search handles. */
        tag = "clipped";
        why = "the card is overexposed: move it away from the lamp";
    } else {
        int next[2];
        for (int i = 0; i < 2; i++) {
            int ch = i ? 2 : 0;
            /* A clipped channel reads low; it is at least "too strong". */
            float e = m[ch] / m[1] - 1.0f;
            if (clip[ch] > WBCAL_CLIP) {
                e = fmaxf(e, 2 * WBCAL_TOL);
            }
            next[i] = s_wbcal_ch[i].done ? s_wb_gain[ch]
                                         : wbcal_next(&s_wbcal_ch[i], s_wb_gain[ch], e);
        }
        if (next[0] < 0 || next[1] < 0) {
            tag = "range";
            why = "it needs a gain beyond what the register gives: is the card really white?";
        } else if (s_wbcal_ch[0].done && s_wbcal_ch[1].done) {
            s_wbcal = 0;
            s_wb_gain[0] = (uint8_t)next[0];
            s_wb_gain[2] = (uint8_t)next[1];
            sensor_t *s = esp_camera_sensor_get();
            if (s) wb_write(s, WB_FIXED);
            printf("# wb cal       done in %d steps: gains %d,%d,%d (!save keeps them)\n",
                   step, s_wb_gain[0], s_wb_gain[1], s_wb_gain[2]);
            printf("#!WBCAL ok r=%d g=%d b=%d R=%.0f G=%.0f B=%.0f steps=%d\n",
                   s_wb_gain[0], s_wb_gain[1], s_wb_gain[2], m[0], m[1], m[2], step);
            return;
        } else if (--s_wbcal == 0) {
            tag = "converge";
            why = "it did not settle within the steps allowed";
        } else {
            s_wb_gain[0] = (uint8_t)next[0];
            s_wb_gain[2] = (uint8_t)next[1];
            sensor_t *s = esp_camera_sensor_get();
            if (s) wb_write(s, WB_FIXED);
            s_wbcal_wait = WBCAL_SETTLE;
            return;
        }
    }
    s_wbcal = 0;
    memcpy(s_wb_gain, s_wbcal_prev_gain, sizeof s_wb_gain);
    set_wb(s_wbcal_prev);
    printf("# wb cal       REFUSED: %s. Back to %s, gains %d,%d,%d\n", why,
           wb_name(s_wb), s_wb_gain[0], s_wb_gain[1], s_wb_gain[2]);
    printf("#!WBCAL refused reason=%s R=%.0f G=%.0f B=%.0f steps=%d\n",
           tag, m[0], m[1], m[2], step);
}

/* 'm': the mask exactly as the detector sees it at the running stage, 4x4
   pixels per character.
   '#' at least half the pixels match, '+' some do, '.' none; 'X' marks the
   reported centroid. The text fallback for when the PC viewer is not to hand.
   It tests pixels one by one rather than through blob_mask(), which would
   need a 19 kB buffer in DRAM that is already holding two frames. */
/*
 * '!collect' records, one #!BLOB packet per reported blob, after the CSV row
 * with the same t_ms: the row says what the detector reported, the records
 * what each of those blobs looked like. Header fields (protocol.py BlobRec):
 * k of n reported, the stage that ran, the CSV and feature versions and the
 * firmware, so a dataset row can always be traced to the code that made it;
 * the blob's centroid, area and box; the crop's source square; the stage's
 * confidence (-1 below stage 2); the features, %.6g, which is float
 * precision; and the white balance the pixels were made with, since colour
 * features from different gains are not comparable (report §10.7).
 *
 * At stage 2 the records cover only the blobs the classifier kept, because
 * those are the ones reported. Training data for the classifier is therefore
 * collected at stage 1, where every blob the colour table finds is sent.
 */
static void send_blob_records(const tc_result_t *tr, int n, int64_t t_ms)
{
    for (int k = 0; k < n; k++) {
        const blob_t *b = &tr->r.b[k];
        const blob_crop_box_t *cb = &s_crop_box[k];
        char feat[BLOB_FEAT_N * 16];
        size_t at = 0;
        for (int i = 0; i < BLOB_FEAT_N && at < sizeof feat; i++)
            at += (size_t)snprintf(feat + at, sizeof feat - at, "%s%.6g", i ? "," : "",
                                   (double)tr->feat[k].v[i]);
        uint8_t g[3];
        wb_gain_in_use(g);
        char fields[512];
        snprintf(fields, sizeof fields,
                 "t_ms=%lld k=%d n=%d stage=%d csv=%d fv=%d fw=%s c=%.2f,%.2f area=%d "
                 "box=%d,%d,%d,%d crop=%d,%d,%d conf=%.3g feat=%s wb=%s wb_gain=%d,%d,%d "
                 "cw=%d fmt=rgb565le",
                 (long long)t_ms, k, n, tr->stage, CSV_VERSION, BLOB_FEAT_VERSION,
                 esp_app_get_description()->version, b->cx, b->cy, b->area,
                 b->x0, b->y0, b->x1, b->y1, cb->xs, cb->ys, cb->side,
                 (double)tr->conf[k], feat, wb_name(s_wb), g[0], g[1], g[2], BLOB_CROP);
        /* The ESP32 is little-endian, so the uint16 array in memory is
           already rgb565le on the wire. */
        link_send_packet("BLOB", fields, (const uint8_t *)s_crops[k],
                         sizeof s_crops[k]);
    }
}

static void dump_mask(const uint8_t *frame, const blob_result_t *r)
{
    int xc = r->found ? (int)r->b[0].cx / 4 : -1;
    int yc = r->found ? (int)r->b[0].cy / 4 : -1;
    blob_cfg_t c = tc_stage_cfg(&s_cfg, s_stage_req, &s_models);
    printf("# mask (4x4 px per char, %s)\n", c.lut ? "colour table" : "HSV window");
    for (int by = 0; by < FRAME_H / 4; by++) {
        char row[FRAME_W / 4 + 1];
        for (int bx = 0; bx < FRAME_W / 4; bx++) {
            int k = 0;
            for (int y = by * 4; y < by * 4 + 4; y++)
                for (int x = bx * 4; x < bx * 4 + 4; x++)
                    k += blob_pixel_matches(&c, blob_pixel(&c, frame, x, y));
            row[bx] = (bx == xc && by == yc) ? 'X' : k >= 8 ? '#' : k ? '+' : '.';
        }
        row[FRAME_W / 4] = '\0';
        printf("# %s\n", row);
    }
    fflush(stdout);
}

static const char *reset_reason_str(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON:  return "power-on";
    case ESP_RST_SW:       return "software";
    case ESP_RST_PANIC:    return "PANIC (crash)";
    case ESP_RST_INT_WDT:  return "interrupt watchdog";
    case ESP_RST_TASK_WDT: return "task watchdog";
    case ESP_RST_WDT:      return "other watchdog";
    case ESP_RST_BROWNOUT: return "BROWNOUT (supply collapsed)";
    case ESP_RST_DEEPSLEEP: return "deep sleep wake";
    case ESP_RST_EXT:      return "external pin";
    default:               return "unknown";
    }
}

/* Printed before anything draws meaningful current, and before GPIO0 becomes
   an output - see optflow_cam for the brownout that taught us this. */
static void boot_report(void)
{
    esp_chip_info_t chip;
    esp_chip_info(&chip);
    esp_reset_reason_t rr = esp_reset_reason();

    printf("\n\n# ---- target_cam boot ----\n");
    printf("# reset reason  %s\n", reset_reason_str(rr));
    printf("# chip          ESP32 rev %d, %d core(s)\n",
           chip.revision, chip.cores);
    printf("# heap          %u B internal free\n",
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    size_t psram = heap_caps_get_total_size(MALLOC_CAP_SPIRAM);
    printf("# psram         %u B %s\n", (unsigned)psram,
           psram ? "detected" : "absent (not required; buffers live in DRAM)");

    if (rr == ESP_RST_BROWNOUT) {
        printf("#\n");
        printf("# *** The last reset was a BROWNOUT. The supply could not hold\n");
        printf("#     up. An ESP32-CAM needs 5 V able to source ~500 mA; a\n");
        printf("#     USB-serial adapter's 3.3 V pin cannot do it, and the\n");
        printf("#     camera's power-up is what pushes it over. Fix the supply\n");
        printf("#     before reading anything below as a sensor result. ***\n");
    }
    fflush(stdout);
}

/* One kind of model, from the upload slot if it holds one that passes the
   loader, else from the build. A refused upload is reported and the build's
   model used instead: a damaged slot must not take a stage away that the
   firmware itself can still run. Returns the source; *err is the build
   record's verdict when it was tried. */
typedef tc_model_err_t (*model_load_fn)(const uint8_t *rec, size_t len, uint32_t *id);

static tc_model_err_t try_lut(const uint8_t *rec, size_t len, uint32_t *id)
{
    return color_lut_load(rec, len, s_lut, id);
}

static tc_model_err_t try_clf(const uint8_t *rec, size_t len, uint32_t *id)
{
    return blob_clf_load(rec, len, &s_clf_rec, id);
}

static tc_model_err_t try_cnn(const uint8_t *rec, size_t len, uint32_t *id)
{
    return blob_cnn_load(rec, len, &s_cnn_rec, id);
}

static const char *load_one(ms_kind_t k, const uint8_t *build, size_t build_len,
                            model_load_fn fn, uint32_t *id, tc_model_err_t *err)
{
    *err = TC_MODEL_OK;
    esp_partition_mmap_handle_t h;
    size_t len;
    const uint8_t *rec = ms_map(k, &len, &h);
    if (rec != NULL) {
        tc_model_err_t e = fn(rec, len, id);
        ms_unmap(h);
        if (e == TC_MODEL_OK) return "upload";
        printf("# models        uploaded %s REFUSED (%s): using the build's\n", ms_name(k),
               tc_model_err_str(e));
    }
    if (build == NULL) return "none";
    *err = fn(build, build_len, id);
    return *err == TC_MODEL_OK ? "build" : "bad";
}

/* (Re)load every model: at boot, and after an upload or unload, so a new
   model runs at once and the table-binding checks are made again (a new
   table makes the old classifier a mismatch until it is replaced too). */
static void reload_models(void)
{
    uint32_t id = 0;
    tc_model_err_t e;
    s_models.lut = NULL;
    s_models.clf = NULL;
    s_models.cnn = NULL;
    s_lut_id = s_clf_id = s_cnn_id = 0;

    s_lut_src = load_one(MS_LUT, lut_model_rec, sizeof lut_model_rec, try_lut, &id, &e);
    if (strcmp(s_lut_src, "build") == 0 || strcmp(s_lut_src, "upload") == 0) {
        s_models.lut = s_lut;
        s_lut_state = "ok";
        s_lut_id = id;
        printf("# models        colour table ok (%s): id %u, %d colours accepted, %u B in DRAM\n",
               s_lut_src, (unsigned)id, color_lut_count(s_lut), (unsigned)sizeof s_lut);
    } else {
        s_lut_state = s_lut_src;
        printf("# models        colour table %s (%s): stage 1 and above unavailable\n",
               strcmp(s_lut_src, "bad") == 0 ? "REFUSED" : "absent", tc_model_err_str(e));
    }

    s_clf_src = load_one(MS_CLF, CLF_BUILD_REC, CLF_BUILD_LEN, try_clf, &id, &e);
    if (strcmp(s_clf_src, "none") == 0) {
        s_clf_state = "none";
        printf("# models        no blob classifier: stage 2 unavailable\n");
    } else if (strcmp(s_clf_src, "bad") == 0) {
        s_clf_state = "bad";
        printf("# models        blob classifier REFUSED (%s): stage 2 unavailable\n",
               tc_model_err_str(e));
    } else if (s_models.lut == NULL || s_clf_rec.lut_id != s_lut_id) {
        s_clf_state = "mismatch";
        s_clf_id = id;
        printf("# models        blob classifier id %u (%s) was trained behind colour table %u, "
               "not this one (%s %u): stage 2 unavailable\n", (unsigned)id, s_clf_src,
               (unsigned)s_clf_rec.lut_id, s_lut_state, (unsigned)s_lut_id);
    } else {
        s_models.clf = &s_clf;
        s_clf_state = "ok";
        s_clf_id = id;
        printf("# models        blob classifier ok (%s): id %u, threshold %.3f, behind table %u\n",
               s_clf_src, (unsigned)id, (double)s_clf_rec.threshold, (unsigned)s_clf_rec.lut_id);
    }

    s_cnn_src = load_one(MS_CNN, CNN_BUILD_REC, CNN_BUILD_LEN, try_cnn, &id, &e);
    if (strcmp(s_cnn_src, "none") == 0) {
        s_cnn_state = "none";
        printf("# models        no crop CNN: stage 3 unavailable\n");
    } else if (strcmp(s_cnn_src, "bad") == 0) {
        s_cnn_state = "bad";
        printf("# models        crop CNN REFUSED (%s): stage 3 unavailable\n",
               tc_model_err_str(e));
    } else if (s_models.lut == NULL || s_cnn_rec.lut_id != s_lut_id) {
        s_cnn_state = "mismatch";
        s_cnn_id = id;
        printf("# models        crop CNN id %u (%s) was trained behind colour table %u, not "
               "this one (%s %u): stage 3 unavailable\n", (unsigned)id, s_cnn_src,
               (unsigned)s_cnn_rec.lut_id, s_lut_state, (unsigned)s_lut_id);
    } else {
        s_models.cnn = &s_cnn;
        s_cnn_state = "ok";
        s_cnn_id = id;
        printf("# models        crop CNN ok (%s): id %u, %dx%d input, %ld multiply-adds per "
               "crop, threshold %.3f\n", s_cnn_src, (unsigned)id, s_cnn_rec.n, s_cnn_rec.n,
               blob_cnn_macs(&s_cnn_rec), (double)s_cnn_rec.threshold);
    }
    apply_thr2();
    apply_thr3();
    s_models.cnn_k = s_cnn_k;
    fflush(stdout);
}

/* On an AI-Thinker ESP32-CAM, XCLK is GPIO0 - the pin esptool pulls low for
   download mode. Once the camera clock starts, the board can no longer be put
   into download mode from DTR. This window keeps it reflashable. Do not
   remove it. */
static void escape_hatch_delay(void)
{
    printf("#\n# GPIO0 is XCLK on this board: once the camera starts, esptool\n");
    printf("# can no longer force download mode. Reflash window open:\n");
    for (int i = TARGETCAM_ESCAPE_SECONDS; i > 0; i--) {
        printf("#   %d...\n", i);
        fflush(stdout);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    printf("# window closed, starting the camera\n");
    fflush(stdout);
}

/* Bearing of a blob from the pinhole model, about the optical axis: +az right
   of the nose, +el above it (image +y is down, hence the sign on el). Range
   from the box's larger side, withheld (-1) when off or clipped.
 *
 * Why the box and not the area: on a shaded mylar balloon the white specular
 * highlight and the dark far side both fail the colour test, so the area
 * undercounts the balloon and the range read 10-13% long on the host suite.
 * The box spans the highlight hole and read 5-11% long. Either way the residual
 * is systematic, so calibrate s_target_mm against a balloon at a measured
 * distance rather than trusting 550 mm. A clipped target reads small, so its
 * range would read long; that is why it gets none. */
static void blob_geom(const blob_t *b, float *az, float *el, float *range_mm)
{
    float f = focal_px();
    *az = atanf((b->cx - FRAME_W * 0.5f) / f) * 180.0f / (float)M_PI;
    *el = atanf((FRAME_H * 0.5f - b->cy) / f) * 180.0f / (float)M_PI;
    int w = b->x1 - b->x0 + 1, h = b->y1 - b->y0 + 1;
    int d_px = w > h ? w : h;
    *range_mm = (s_target_mm > 0.0f && !b->clipped && d_px > 0)
              ? s_target_mm * f / (float)d_px : -1.0f;
}

void app_main(void)
{
    boot_report();
    quiesce_heat_sources();

    /* Kconfig defaults first; a saved calibration, if any, replaces them. */
    live_defaults();
    s_ref = live_capture();
    s_ref_saved = false;
    esp_err_t nvs_err = settings_init();
    s_nvs_ok = nvs_err == ESP_OK;
    if (!s_nvs_ok) {
        printf("# settings      NVS init failed (%s): running defaults, !save "
               "unavailable\n", esp_err_to_name(nvs_err));
    } else {
        tc_settings_t t;
        bool migrated;
        if (settings_load(&t, &migrated)) {
            if (live_apply(&t)) {
                s_ref = live_capture();
                s_ref_saved = true;
                printf("# settings      loaded from flash\n");
                if (migrated) {
                    printf("# settings      v1 record: frame rate set to the build "
                           "default %.1f ms; !save to rewrite it\n", period_ms());
                }
            } else {
                printf("# settings      saved record out of range, IGNORED: "
                       "running defaults\n");
            }
        } else {
            printf("# settings      nothing saved: running Kconfig defaults\n");
        }
    }
    fflush(stdout);

    if (!ms_init()) {
        printf("# models        no 'tcmodels' partition in this build's partition table: "
               "!upload unavailable, compiled-in models only\n");
    }
    reload_models();

    if (!blob_cfg_valid(&s_cfg)) {
        ESP_LOGE(TAG, "detector configuration is invalid for a %dx%d frame",
                 FRAME_W, FRAME_H);
        return;
    }

    size_t work_bytes = blob_workspace_bytes(&s_cfg);
    s_work = heap_caps_malloc(work_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (s_work == NULL) {
        ESP_LOGE(TAG, "could not allocate the %u-byte detector workspace",
                 (unsigned)work_bytes);
        return;
    }

    /* Take over UART0 so reads can be non-blocking. stdout keeps working. */
    /* 1 kB of receive buffer: an '!upload' streams up to 16 kB, written to
       flash 256 bytes at a time while the rest waits here. */
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, 1024, 0, 0, NULL, 0));
    link_init();

    escape_hatch_delay();

    /* Powering up the OV2640 is the largest current step this firmware takes.
       If the output stops here, it is the supply, not the code. */
    printf("# esp_camera_init: powering up the sensor now\n");
    fflush(stdout);
    if (camera_start() != ESP_OK) {
        printf("# camera init FAILED - see the error above\n");
        fflush(stdout);
        return;
    }
    printf("# esp_camera_init: ok\n");
    fflush(stdout);

    /* Let exposure and white balance settle before the first detection; the
       first frames after power-up have the wrong colour cast. */
    ESP_LOGI(TAG, "waiting for exposure and white balance to settle");
    for (int i = 0; i < 20; i++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) {
            esp_camera_fb_return(fb);
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    /* A saved "frozen" freezes here, at the gains the settle just found, and
       a saved "fixed" loads its gains - the camera had no sensor to talk to
       when the settings were loaded. */
    set_wb(s_wb);

    print_banner();
    print_cfg();
    fflush(stdout);

    int64_t prev_capture_us = 0;
    int64_t prev_fb_us = 0, prev_used_fb_us = 0;
    int     skipped = 0;

    while (1) {
        /* The sensor is the clock. fb_get() blocks until the OV2640 completes
           its next frame, and the task is idle meanwhile: the idle time is
           still the thermal budget, the sensor just measures it out now.
           Pacing on the FreeRTOS tick instead (80.000 ms against the sensor's
           79.968) made the two beat, measured 2026-10-14: the frame taken fell
           32 us further behind each period, its age wandered 80-160 ms, about
           every 200 s one frame was missed (dt 99 ms), and fb_get() then
           waited up to 19 ms a frame while the phase walked back. The tick
           itself also stepped 140 us against esp_timer every ~17 s. Locked
           to the sensor, rows are whole sensor periods apart and every frame
           is taken as it completes. */
        camera_fb_t *fb = esp_camera_fb_get();
        int64_t capture_us = esp_timer_get_time();
        if (fb == NULL) {
            ESP_LOGW(TAG, "frame grab failed");
            serve_commands();
            continue;
        }

        /* The driver stamps each frame (esp_timer, at DMA start, in its ISR);
           consecutive stamps measure the sensor's real period. */
        int64_t fb_us = (int64_t)fb->timestamp.tv_sec * 1000000 + fb->timestamp.tv_usec;
        if (prev_fb_us) {
            double d = (double)(fb_us - prev_fb_us);
            long n = lround(d / s_sensor_us);
            if (n >= 1 && n <= 16) {
                s_sensor_us += (d / (double)n - s_sensor_us) / 32.0;
            }
        }
        prev_fb_us = fb_us;

        /* 'f' slower than the sensor: use every n-th frame. The others go
           straight back, and commands are still answered on them. */
        if (++skipped < s_every_n) {
            esp_camera_fb_return(fb);
            serve_commands();
            continue;
        }
        skipped = 0;

        /* A capture off the sensor's own period is explained in a comment
           line, which CSV readers skip: how far apart the captures were, how
           far apart the frames were, and how old this one is. Expected after
           'g', 'v1', '?' or anything else that keeps the loop past a frame. */
        if (prev_capture_us) {
            int64_t dcap = capture_us - prev_capture_us;
            double expect = s_every_n * s_sensor_us;
            if (fabs((double)dcap - expect) >= 50.0) {
                printf("# late capture dt_us=%lld expected_us=%.0f fb_dt_us=%lld "
                       "fb_age_us=%lld\n", dcap, expect, fb_us - prev_used_fb_us,
                       capture_us - fb_us);
            }
        }
        prev_used_fb_us = fb_us;

        if (fb->format != PIXFORMAT_RGB565 ||
            fb->width != FRAME_W || fb->height != FRAME_H) {
            ESP_LOGE(TAG, "unexpected frame: %dx%d format %d",
                     fb->width, fb->height, fb->format);
            esp_camera_fb_return(fb);
            serve_commands();
            continue;
        }

        serve_model_transfer();
        if (s_calibrate) {
            s_calibrate = false;
            calibrate(fb->buf);
            print_cfg();
            fflush(stdout);
        }
        if (s_wbcal > 0) {
            wb_cal_step(fb->buf);
            if (s_wbcal == 0) {
                print_cfg();
            }
            fflush(stdout);
        }

        /* Features are computed on every frame while collecting, not only
           on the frames whose records go out, so t_b_us in the rows is a
           steady figure. Asking for stage 2 turns them on too: the
           classifier needs them, and if it is missing and the stage falls
           back, t_b_us still measures what stage 2 would cost at 80 ms. */
        int flags = (s_collect || s_stage_req >= 2) ? TC_RUN_FEATURES : 0;
        int64_t t0 = esp_timer_get_time();
        tc_result_t tr;
        int ok = tc_pipeline_run(&s_cfg, s_stage_req, flags, &s_models, fb->buf, s_work,
                                 esp_timer_get_time, &tr);
        int64_t proc_us = esp_timer_get_time() - t0;
        const blob_result_t r = tr.r;

        if (s_dump_mask) {
            s_dump_mask = false;
            dump_mask(fb->buf, &r);
        }
        /* Crops come from the pixels, so they are cut before the frame goes
           back; the records themselves go out after the row. A frame that
           goes to the PC ('g' or 'v1') always carries its records: the PC
           can only check a record against the pixels it came from, and next
           to a 38 kB frame the records' 6 kB are cheap. Without that, at
           115200 only one grab in four would be checkable. */
        bool send_img = s_grab || s_image;
        int n_rec = 0;
        if (s_collect && (++s_collect_n >= COLLECT_EVERY || send_img)) {
            s_collect_n = 0;
            n_rec = tr.n_feat;
            for (int k = 0; k < n_rec; k++) {
                blob_crop_box(&r.b[k], &s_crop_box[k]);
                blob_crop(&s_cfg, fb->buf, &s_crop_box[k], s_crops[k]);
            }
        }

        /* The frame goes out before its CSV row, with the same t_ms, so the PC
           can draw this detection on these pixels. The buffer is held while
           it is sent; with two buffers and GRAB_LATEST the driver keeps
           filling the other. */
        if (send_img) {
            s_grab = false;
            link_send_frame(fb->buf, fb->len, FRAME_W, FRAME_H,
                            s_cfg.big_endian, capture_us / 1000);
        }
        esp_camera_fb_return(fb);

        float dt_ms = prev_capture_us ? (float)(capture_us - prev_capture_us) / 1000.0f
                                      : 0.0f;
        prev_capture_us = capture_us;

        float az = 0.0f, el = 0.0f, range_mm = -1.0f, fill = 0.0f;
        int w = 0, h = 0;
        if (ok) {
            blob_geom(&r.b[0], &az, &el, &range_mm);
            w = r.b[0].x1 - r.b[0].x0 + 1;
            h = r.b[0].y1 - r.b[0].y0 + 1;
            fill = (float)r.b[0].area / (float)(w * h);
        }

        if (s_streaming) {
            printf("%lld,%d,%.2f,%.2f,%.2f,%.2f,%d,%d,%d,%.2f,%.0f,%d,%d,%d",
                   capture_us / 1000, ok, r.b[0].cx, r.b[0].cy, az, el,
                   r.b[0].area, w, h, fill, range_mm, r.b[0].clipped,
                   r.n_blobs, r.matched);
            /* The other balloons in view, largest (usually nearest) first.
               Absent ones print as 0,0,-1. */
            for (int k = 1; k < BLOB_MAX_REPORT; k++) {
                float a2 = 0.0f, e2 = 0.0f, r2 = -1.0f;
                if (k < r.n_report) {
                    blob_geom(&r.b[k], &a2, &e2, &r2);
                }
                printf(",%.2f,%.2f,%.0f", a2, e2, r2);
            }
            printf(",%.1f,%lld", dt_ms, proc_us);
            /* v2. %.3g keeps an absent confidence at two bytes ("-1"). */
            printf(",%d,%.3g,%.3g,%.3g,%.3g,%ld,%ld,%ld,%lld,%d\n", tr.stage,
                   tr.conf[0], tr.conf[1], tr.conf[2], tr.conf[3],
                   (long)tr.t_px_us, (long)tr.t_b_us, (long)tr.t_c_us,
                   s_loop_us, CSV_VERSION);
            fflush(stdout);
        }
        send_blob_records(&tr, n_rec, capture_us / 1000);
        serve_commands();
        /* Busy time from the frame's arrival to here - detection, the row,
           any command reply - reported in the next row. */
        s_loop_us = esp_timer_get_time() - capture_us;
    }
}
