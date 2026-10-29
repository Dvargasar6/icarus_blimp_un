/*
 * Host-side verification of blob_detect, with no camera and no ESP32.
 *
 * blob_detect.c is portable C, so the exact code that runs on the camera
 * compiles here. Each test paints a synthetic RGB565 frame whose answer we know
 * - a disc of a chosen colour at a chosen sub-pixel position, on a background
 * chosen to be hard - and checks the detector recovers it.
 *
 * Backgrounds are not blank. A detector that only works on a black frame tells
 * us nothing, so the default scene is grey-level value noise (zero saturation,
 * which must be rejected) plus a faint colour cast and per-pixel colour noise.
 *
 * Frames are stored big-endian, as esp32-camera delivers them; test 7 checks
 * that the byte-order flag is honoured rather than silently ignored.
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../main/blob_detect.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define W 160
#define H 120

static uint8_t  frame[W * H * 2];
static uint32_t work_buf[(W * H * 3) / 2 + 64];   /* > 113 kB, 4-byte aligned */
static const float F_PX = 157.0f;   /* QQVGA focal length, 54 deg h-FOV */
static int failures;

static uint32_t rng = 12345u;
static int rnd(int n)                               /* 0..n-1 */
{
    rng = rng * 1664525u + 1013904223u;
    return (int)((rng >> 8) % (uint32_t)n);
}

static uint16_t rgb(int r, int g, int b)
{
    if (r < 0) r = 0;
    if (r > 255) r = 255;
    if (g < 0) g = 0;
    if (g > 255) g = 255;
    if (b < 0) b = 0;
    if (b > 255) b = 255;
    return (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
}

static void put(int x, int y, uint16_t px, int big_endian)
{
    uint8_t *p = frame + ((size_t)y * W + (size_t)x) * 2;
    if (big_endian) { p[0] = (uint8_t)(px >> 8); p[1] = (uint8_t)px; }
    else            { p[0] = (uint8_t)px; p[1] = (uint8_t)(px >> 8); }
}

/* Grey walls with a little colour noise: saturation stays well under the
   default sat_min, so none of it should ever match. */
static void background(int big_endian)
{
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            int v = 60 + rnd(140);
            put(x, y, rgb(v + rnd(12) - 6, v + rnd(12) - 6, v + 4 + rnd(12) - 6),
                big_endian);
        }
}

/* A disc, painted where the pixel CENTRE falls inside it. The true centroid of
   that pixel set is what we compare against, computed here independently. */
static void disc(float cx, float cy, float r, int R, int G, int B,
                 double *tx, double *ty, int *tn, int big_endian)
{
    double sx = 0, sy = 0;
    int n = 0;
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            float dx = (float)x + 0.5f - cx, dy = (float)y + 0.5f - cy;
            if (dx * dx + dy * dy <= r * r) {
                /* +/-8 per channel of shading noise, like a real balloon */
                put(x, y, rgb(R + rnd(16) - 8, G + rnd(16) - 8, B + rnd(16) - 8),
                    big_endian);
                sx += x + 0.5; sy += y + 0.5; n++;
            }
        }
    if (tx) *tx = sx / n;
    if (ty) *ty = sy / n;
    if (tn) *tn = n;
}

static blob_cfg_t red_cfg(void)
{
    blob_cfg_t c = {
        .width = W, .height = H, .cell = 2, .big_endian = 1,
        .hue = 0, .hue_tol = 15, .sat_min = 120, .val_min = 50, .min_area = 30,
    };
    return c;
}

static void check(int cond, const char *what)
{
    if (!cond) {
        printf("    FAIL: %s\n", what);
        failures++;
    }
}

/* ------------------------------------------------------------------ */

static void test_centroid(void)
{
    printf("1. centroid of a red disc, sub-pixel positions, 2x2 cells\n");
    blob_cfg_t c = red_cfg();
    const float pos[][2] = {
        {80.0f, 60.0f}, {33.3f, 21.7f}, {120.6f, 90.2f}, {47.5f, 88.25f},
        {101.1f, 30.9f}, {20.4f, 60.0f},
    };
    double worst = 0;
    for (size_t i = 0; i < sizeof pos / sizeof pos[0]; i++) {
        double tx, ty;
        int tn;
        background(1);
        disc(pos[i][0], pos[i][1], 9.0f, 220, 30, 25, &tx, &ty, &tn, 1);
        blob_result_t r;
        blob_detect(&c, frame, work_buf, &r);
        double e = hypot(r.b[0].cx - tx, r.b[0].cy - ty);
        if (e > worst) worst = e;
        printf("   (%6.2f,%6.2f) -> (%6.2f,%6.2f)  err %.3f px  area %d/%d\n",
               tx, ty, r.b[0].cx, r.b[0].cy, e, r.b[0].area, tn);
        check(r.found, "disc not found");
        check(e < 0.05, "centroid error >= 0.05 px");
        check(abs(r.b[0].area - tn) <= tn / 50 + 1, "area off by more than 2%");
        check(r.n_blobs == 1 && r.n_report == 1, "phantom second blob");
        check(!r.b[0].clipped, "unclipped disc reported clipped");
    }
    printf("   worst centroid error %.3f px\n", worst);
}

static void test_hue_wrap(void)
{
    printf("2. red straddling 0/360: hue 350 and 10 both match a hue-0 target\n");
    blob_cfg_t c = red_cfg();
    int h, s, v;
    blob_rgb565_hsv(rgb(230, 20, 60), &h, &s, &v);  /* magenta-red, ~349 */
    printf("   rgb(230,20,60) -> hue %d sat %d val %d\n", h, s, v);
    check(h > 340 && h < 360, "expected hue just below 360");
    check(blob_pixel_matches(&c, rgb(230, 20, 60)), "hue ~349 rejected");
    blob_rgb565_hsv(rgb(230, 60, 20), &h, &s, &v);  /* orange-red, ~11 */
    printf("   rgb(230,60,20) -> hue %d sat %d val %d\n", h, s, v);
    check(h > 0 && h < 20, "expected hue just above 0");
    check(blob_pixel_matches(&c, rgb(230, 60, 20)), "hue ~11 rejected");
    check(!blob_pixel_matches(&c, rgb(20, 220, 30)), "green accepted as red");
    check(!blob_pixel_matches(&c, rgb(200, 200, 30)), "yellow accepted as red");
}

static void test_no_target(void)
{
    printf("3. no target: grey scene plus a green disc -> found=0\n");
    blob_cfg_t c = red_cfg();
    background(1);
    disc(80, 60, 15, 30, 200, 40, NULL, NULL, NULL, 1);
    blob_result_t r;
    int f = blob_detect(&c, frame, work_buf, &r);
    printf("   found %d, matched pixels %d\n", f, r.matched);
    check(f == 0 && r.found == 0, "reported a red target that is not there");
    check(r.b[0].cx == 0.0f && r.b[0].cy == 0.0f, "fields not zeroed on no-detection");
}

static void test_two_blobs(void)
{
    printf("4. two red discs: larger first, runner-up second\n");
    blob_cfg_t c = red_cfg();
    double tx, ty;
    int big, small;
    background(1);
    disc(40, 40, 6, 220, 30, 25, NULL, NULL, &small, 1);
    disc(110, 75, 12, 220, 30, 25, &tx, &ty, &big, 1);
    blob_result_t r;
    blob_detect(&c, frame, work_buf, &r);
    printf("   chose (%.2f,%.2f) area %d; second %d; n_blobs %d\n",
           r.b[0].cx, r.b[0].cy, r.b[0].area, r.b[1].area, r.n_blobs);
    check(r.found && hypot(r.b[0].cx - tx, r.b[0].cy - ty) < 0.05, "picked the wrong disc");
    check(r.n_blobs == 2, "expected n_blobs = 2");
    check(abs(r.b[1].area - small) <= small / 20 + 1, "runner-up area wrong");
    check(r.n_report == 2 && hypot(r.b[1].cx - 40, r.b[1].cy - 40) < 0.05,
          "runner-up position wrong");
}

static void test_reject_grey_dark(void)
{
    printf("5. low-saturation and dark pixels are rejected even with a red hue\n");
    blob_cfg_t c = red_cfg();
    int h, s, v;
    blob_rgb565_hsv(rgb(170, 140, 140), &h, &s, &v); /* pinkish grey */
    printf("   rgb(170,140,140) -> hue %d sat %d -> %s\n", h, s,
           blob_pixel_matches(&c, rgb(170, 140, 140)) ? "match" : "reject");
    check(!blob_pixel_matches(&c, rgb(170, 140, 140)), "desaturated pink matched");
    blob_rgb565_hsv(rgb(40, 5, 5), &h, &s, &v);      /* red in deep shadow */
    printf("   rgb(40,5,5)      -> hue %d val %d -> %s\n", h, v,
           blob_pixel_matches(&c, rgb(40, 5, 5)) ? "match" : "reject");
    check(!blob_pixel_matches(&c, rgb(40, 5, 5)), "near-black matched");
    check(!blob_pixel_matches(&c, rgb(128, 128, 128)), "pure grey matched");
}

static void test_speckle(void)
{
    printf("6. 300 isolated red pixels: pooled away, found=0\n");
    blob_cfg_t c = red_cfg();
    background(1);
    for (int i = 0; i < 300; i++)
        put(rnd(W), rnd(H), rgb(220, 30, 25), 1);
    blob_result_t r;
    blob_detect(&c, frame, work_buf, &r);
    printf("   matched %d pixels, found %d, n_blobs %d\n", r.matched, r.found, r.n_blobs);
    check(r.matched > 250, "speckle not even classified as red");
    check(!r.found, "speckle reported as a target");
}

static void test_byte_order(void)
{
    printf("7. byte order: little-endian frame read as big-endian must not match\n");
    blob_cfg_t c = red_cfg();
    background(0);
    disc(80, 60, 12, 220, 30, 25, NULL, NULL, NULL, 0);
    blob_result_t r;
    c.big_endian = 1;
    blob_detect(&c, frame, work_buf, &r);
    int wrong = r.found;
    c.big_endian = 0;
    blob_detect(&c, frame, work_buf, &r);
    printf("   wrong order found %d, right order found %d at (%.1f,%.1f)\n",
           wrong, r.found, r.b[0].cx, r.b[0].cy);
    check(!wrong, "swapped bytes still detected - flag has no effect");
    check(r.found && fabs(r.b[0].cx - 80) < 0.1 && fabs(r.b[0].cy - 60) < 0.1,
          "correct byte order not detected");
}

static void test_clipped(void)
{
    printf("8. disc running off the right edge is flagged clipped\n");
    blob_cfg_t c = red_cfg();
    background(1);
    disc(155, 60, 12, 220, 30, 25, NULL, NULL, NULL, 1);
    blob_result_t r;
    blob_detect(&c, frame, work_buf, &r);
    printf("   box x %d..%d, clipped %d\n", r.b[0].x0, r.b[0].x1, r.b[0].clipped);
    check(r.found && r.b[0].clipped && r.b[0].x1 == W - 1, "edge contact not flagged");
}

static void test_worst_case_labels(void)
{
    printf("9. worst case for the label arrays: full-frame checkerboard, every\n"
           "   cell size (run under -fsanitize=address)\n");
    for (int cs = 1; cs <= 4; cs *= 2) {
        blob_cfg_t c = red_cfg();
        c.cell = cs;
        c.min_area = 1;
        for (int y = 0; y < H; y++)
            for (int x = 0; x < W; x++) {
                int on = ((x / cs) + (y / cs)) % 2 == 0;
                put(x, y, on ? rgb(220, 30, 25) : rgb(100, 100, 100), 1);
            }
        blob_result_t r;
        blob_detect(&c, frame, work_buf, &r);
        int expect = (W / cs) * (H / cs) / 2;
        printf("   cell %d: %5d blobs (expect %5d), workspace %zu B\n",
               cs, r.n_blobs, expect, blob_workspace_bytes(&c));
        check(r.n_blobs == expect, "checkerboard blob count wrong");
        check(blob_workspace_bytes(&c) <= sizeof work_buf, "test buffer too small");
    }
}

static void test_calibrate(void)
{
    printf("10. calibration sample recovers the colour of a patch\n");
    blob_cfg_t c = red_cfg();
    const int cols[][3] = { {220, 30, 25}, {230, 25, 70}, {30, 180, 60},
                            {30, 90, 220}, {240, 150, 20} };
    for (size_t i = 0; i < sizeof cols / sizeof cols[0]; i++) {
        background(1);
        disc(80, 60, 20, cols[i][0], cols[i][1], cols[i][2], NULL, NULL, NULL, 1);
        int h_true, s_true, v_true;
        blob_rgb565_hsv(rgb(cols[i][0], cols[i][1], cols[i][2]), &h_true, &s_true, &v_true);
        blob_color_t s;
        blob_sample(&c, frame, 72, 52, 16, 16, &s);
        int dh = abs(s.hue - h_true);
        if (dh > 180) dh = 360 - dh;
        printf("   rgb(%3d,%3d,%3d): true hue %3d, sampled %3d +/-%2d, sat %3d val %3d\n",
               cols[i][0], cols[i][1], cols[i][2], h_true, s.hue, s.hue_spread,
               s.sat, s.val);
        check(dh <= 4, "sampled hue more than 4 deg off");
        check(s.hue_spread < 10, "spread too wide for a uniform patch");
    }
}

static void test_cfg(void)
{
    printf("11. configuration validation\n");
    blob_cfg_t c = red_cfg();
    check(blob_cfg_valid(&c), "default config rejected");
    c.cell = 3;                         check(!blob_cfg_valid(&c), "cell 3 accepted");
    c = red_cfg(); c.hue = 360;         check(!blob_cfg_valid(&c), "hue 360 accepted");
    c = red_cfg(); c.hue_tol = 0;       check(!blob_cfg_valid(&c), "tol 0 accepted");
    c = red_cfg(); c.min_area = 0;      check(!blob_cfg_valid(&c), "min_area 0 accepted");
    c = red_cfg(); c.width = 162; c.cell = 4;
                                        check(!blob_cfg_valid(&c), "non-divisible accepted");
}

/* A red mylar balloon as the camera will see it: a sphere lit from the upper
   left, so one side is bright and the far side falls to a quarter of the
   brightness, plus a white specular highlight (zero saturation - must NOT
   match, so the blob has a hole in it) and a dark rim. The true centre is
   the disc centre; the detector sees only the coloured, lit part, so some
   centroid bias toward the lit side is expected and is what we measure. */
static void mylar(float cx, float cy, float r, int R, int G, int B, int *tn)
{
    int n = 0;
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            float dx = ((float)x + 0.5f - cx) / r, dy = ((float)y + 0.5f - cy) / r;
            float d2 = dx * dx + dy * dy;
            if (d2 > 1.0f) continue;
            float nz = sqrtf(1.0f - d2);
            /* light from upper-left-front */
            float lam = -0.5f * dx - 0.5f * dy + 0.707f * nz;
            if (lam < 0.0f) lam = 0.0f;
            float k = 0.25f + 0.75f * lam;
            float spec = powf(lam, 40.0f);
            int rr = (int)((float)R * k + spec * (255.0f - (float)R * k));
            int gg = (int)((float)G * k + spec * (255.0f - (float)G * k));
            int bb = (int)((float)B * k + spec * (255.0f - (float)B * k));
            put(x, y, rgb(rr + rnd(10) - 5, gg + rnd(10) - 5, bb + rnd(10) - 5), 1);
            n++;
        }
    if (tn) *tn = n;
}

static void fill_rect(int x0, int y0, int w, int h, int R, int G, int B)
{
    for (int y = y0; y < y0 + h; y++)
        for (int x = x0; x < x0 + w; x++)
            put(x, y, rgb(R + rnd(10) - 5, G + rnd(10) - 5, B + rnd(10) - 5), 1);
}

static void test_mylar_range(void)
{
    printf("12. red mylar balloon (550 mm, rules item 21) at 1-12 m, red preset,\n"
           "    with skin, wood and a pink wall in the same frame\n");
    blob_cfg_t c = red_cfg();
    blob_preset_apply(&c, BLOB_PRESET_RED);
    const float ranges[] = { 1.0f, 2.0f, 4.0f, 6.0f, 8.0f, 10.0f, 12.0f };
    for (size_t i = 0; i < sizeof ranges / sizeof ranges[0]; i++) {
        background(1);
        fill_rect(0, 90, 40, 30, 225, 170, 140);    /* skin (a hand)     */
        fill_rect(120, 95, 40, 25, 160, 100, 50);   /* wooden table      */
        fill_rect(0, 0, 50, 30, 220, 170, 180);     /* pink wall         */
        float r = 0.5f * 550.0f * F_PX / (ranges[i] * 1000.0f);
        int tn;
        mylar(85.0f, 55.0f, r, 200, 20, 30, &tn);
        blob_result_t res;
        blob_detect(&c, frame, work_buf, &res);
        double e = res.found ? hypot(res.b[0].cx - 85.0, res.b[0].cy - 55.0) : -1;
        double d_px = res.found ? 2.0 * sqrt(res.b[0].area / M_PI) : 0;
        double rng = d_px > 0 ? 550.0 * F_PX / d_px / 1000.0 : 0;
        int bw = res.found ? res.b[0].x1 - res.b[0].x0 + 1 : 0;
        int bh = res.found ? res.b[0].y1 - res.b[0].y0 + 1 : 0;
        double rng_box = bw ? 550.0 * F_PX / (bw > bh ? bw : bh) / 1000.0 : 0;
        printf("   %4.1f m: radius %5.1f px  found %d  blobs %d  centroid off %5.2f px "
               "(%4.1f%% of r)  area %4d/%4d  range: area %5.2f m, box %5.2f m\n",
               ranges[i], r, res.found, res.n_blobs, e, 100.0 * e / r,
               res.found ? res.b[0].area : 0, tn, rng, rng_box);
        if (ranges[i] <= 10.0f) {
            check(res.found, "balloon not found within 10 m");
            check(res.n_blobs == 1, "a confounder was detected as red");
            check(e < 0.25 * r, "centroid more than a quarter radius off");
            check(fabs(rng_box / ranges[i] - 1.0) < 0.12, "box range more than 12% off");
        }
    }
}

static void test_four_balloons(void)
{
    printf("13. four red balloons in view (the course): all reported, nearest first\n");
    blob_cfg_t c = red_cfg();
    blob_preset_apply(&c, BLOB_PRESET_RED);
    background(1);
    /* waypoints at ~2.5, 4.5, 6.5 and 9 m */
    const float pos[4][2] = { {40, 60}, {100, 50}, {130, 75}, {70, 30} };
    const float dist[4] = { 2.5f, 4.5f, 6.5f, 9.0f };
    for (int k = 0; k < 4; k++)
        mylar(pos[k][0], pos[k][1], 0.5f * 550.0f * F_PX / (dist[k] * 1000.0f),
              200, 20, 30, NULL);
    blob_result_t res;
    blob_detect(&c, frame, work_buf, &res);
    printf("   n_blobs %d, n_report %d\n", res.n_blobs, res.n_report);
    check(res.n_report == 4, "expected four reported blobs");
    for (int k = 0; k < res.n_report; k++) {
        double e = hypot(res.b[k].cx - pos[k][0], res.b[k].cy - pos[k][1]);
        printf("   #%d at (%6.2f,%6.2f) area %4d  -> waypoint at %.1f m, off %.2f px\n",
               k, res.b[k].cx, res.b[k].cy, res.b[k].area, dist[k], e);
        check(e < 3.0, "blob order or position wrong");
    }
}

static void test_presets(void)
{
    printf("14. presets: each colour finds only its own balloon\n");
    const int cols[BLOB_PRESET_COUNT][3] = {
        [BLOB_PRESET_RED]    = { 200,  20,  30 },
        [BLOB_PRESET_BLUE]   = {  25,  60, 190 },
        [BLOB_PRESET_GREEN]  = {  30, 170,  50 },
        [BLOB_PRESET_ORANGE] = { 235, 120,  20 },
    };
    const float pos[BLOB_PRESET_COUNT][2] = { {30, 35}, {120, 35}, {30, 85}, {120, 85} };
    background(1);
    for (int p = 0; p < BLOB_PRESET_COUNT; p++)
        mylar(pos[p][0], pos[p][1], 16.0f, cols[p][0], cols[p][1], cols[p][2], NULL);
    for (int p = 0; p < BLOB_PRESET_COUNT; p++) {
        blob_cfg_t c = red_cfg();
        check(blob_preset_apply(&c, p), "preset rejected");
        check(blob_cfg_valid(&c), "preset gives an invalid config");
        blob_result_t res;
        blob_detect(&c, frame, work_buf, &res);
        double e = res.found ? hypot(res.b[0].cx - pos[p][0], res.b[0].cy - pos[p][1]) : -1;
        printf("   %-6s (hue %3d +/-%2d): blobs %d, at (%6.2f,%6.2f), off %.2f px\n",
               blob_preset_name(p), c.hue, c.hue_tol, res.n_blobs,
               res.b[0].cx, res.b[0].cy, e);
        check(res.n_blobs == 1, "preset matched more than its own balloon");
        check(res.found && e < 4.0, "preset did not find its balloon");
    }
    check(blob_preset_find("red") == BLOB_PRESET_RED, "find red");
    check(blob_preset_find("Blue") == BLOB_PRESET_BLUE, "find Blue");
    check(blob_preset_find("g") == BLOB_PRESET_GREEN, "find g");
    check(blob_preset_find("orange") == BLOB_PRESET_ORANGE, "find orange");
    check(blob_preset_find("x") == -1 && blob_preset_find("") == -1, "find junk");
    blob_cfg_t c = red_cfg();
    check(!blob_preset_apply(&c, BLOB_PRESET_COUNT), "out-of-range preset accepted");
}

static void test_mask(void)
{
    printf("15. blob_mask agrees with blob_pixel_matches on every pixel, both\n"
           "    byte orders (the PC viewer's overlay is built from blob_mask)\n");
    static uint8_t mask[W * H];
    for (int be = 0; be <= 1; be++) {
        blob_cfg_t c = red_cfg();
        c.big_endian = be;
        background(be);
        disc(60, 50, 14, 220, 30, 25, NULL, NULL, NULL, be);
        disc(120, 80, 6, 230, 20, 60, NULL, NULL, NULL, be);
        int n = blob_mask(&c, frame, mask);
        int bad = 0, count = 0;
        for (int y = 0; y < H; y++)
            for (int x = 0; x < W; x++) {
                int m = blob_pixel_matches(&c, blob_pixel(&c, frame, x, y));
                bad += mask[y * W + x] != m;
                count += m;
            }
        blob_result_t r;
        blob_detect(&c, frame, work_buf, &r);
        printf("   %s-endian: %d matching, %d disagreements, detector matched %d\n",
               be ? "big" : "little", n, bad, r.matched);
        check(bad == 0, "mask disagrees with blob_pixel_matches");
        check(n == count && n == r.matched, "mask count differs from the detector's");
    }
}

int main(void)
{
    test_centroid();
    test_hue_wrap();
    test_no_target();
    test_two_blobs();
    test_reject_grey_dark();
    test_speckle();
    test_byte_order();
    test_clipped();
    test_worst_case_labels();
    test_calibrate();
    test_cfg();
    test_mylar_range();
    test_four_balloons();
    test_presets();
    test_mask();
    printf("\n%s (%d failure%s)\n", failures ? "FAILED" : "PASSED",
           failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
