/*
 * Host-side verification of blob_feat: features and crops on synthetic blobs
 * whose answer is known in advance.
 *
 * What a feature is FOR (does it separate balloons from red furniture?) is a
 * question about data, answered in Phase B2. What is checked here is that
 * each feature measures what its definition says:
 *
 *   - a disc fills pi/4 of its box and has aspect 1; a rectangle fills it
 *     exactly and has the aspect of its sides;
 *   - a flat-coloured blob has no texture but a sharp edge, and the frame
 *     edge cutting it is not counted as its edge;
 *   - a highlight, a hue wrapping through red, and colour just outside the
 *     box each move exactly the feature meant to see them;
 *   - the sampling stride keeps a blob's cost bounded, and byte order changes
 *     nothing;
 *   - crops: an evenly coloured frame gives an evenly coloured crop at either
 *     size, and pixels outside the frame come out black.
 *
 * The crop and the 24 -> 16 shrink are also checked pixel for pixel against an
 * independent numpy version (viewer/tests/test_blobfeat.py), since the PC
 * must be able to make the same crops from saved frames.
 */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../main/blob_feat.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define W 160
#define H 120

static uint8_t frame[W * H * 2];
static uint8_t frame_le[W * H * 2];
static int failures;

static void check(int cond, const char *what)
{
    if (!cond) {
        printf("    FAIL: %s\n", what);
        failures++;
    }
}

static uint16_t rgb(int r, int g, int b)
{
    return (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
}

static void put565(int x, int y, uint16_t px)
{
    uint8_t *p = frame + ((size_t)y * W + (size_t)x) * 2;
    p[0] = (uint8_t)(px >> 8);                      /* big-endian, as the camera */
    p[1] = (uint8_t)px;
}

static void put(int x, int y, int r, int g, int b) { put565(x, y, rgb(r, g, b)); }

static void fill_frame(int r, int g, int b)
{
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) put(x, y, r, g, b);
}

static blob_cfg_t red_cfg(void)
{
    blob_cfg_t c = {
        .width = W, .height = H, .cell = 2, .big_endian = 1,
        .hue = 0, .hue_tol = 20, .sat_min = 120, .val_min = 50, .min_area = 30,
    };
    return c;
}

/* The blob a perfect detector would report for whatever red is in the frame:
   box, area and centroid of every matching pixel. Built by hand so these
   tests depend on blob_feat alone, not on the labelling. */
static blob_t blob_of_frame(const blob_cfg_t *c)
{
    blob_t b = { 0 };
    b.x0 = W; b.y0 = H; b.x1 = -1; b.y1 = -1;
    double sx = 0, sy = 0;
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            if (!blob_pixel_matches(c, blob_pixel(c, frame, x, y))) continue;
            b.area++;
            sx += x + 0.5;
            sy += y + 0.5;
            if (x < b.x0) b.x0 = x;
            if (x > b.x1) b.x1 = x;
            if (y < b.y0) b.y0 = y;
            if (y > b.y1) b.y1 = y;
        }
    if (b.area) {
        b.cx = (float)(sx / b.area);
        b.cy = (float)(sy / b.area);
    }
    return b;
}

static void disc(float cx, float cy, float rr, int r, int g, int b)
{
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            float dx = (float)x + 0.5f - cx, dy = (float)y + 0.5f - cy;
            if (dx * dx + dy * dy <= rr * rr) put(x, y, r, g, b);
        }
}

static void rect(int x0, int y0, int x1, int y1, int r, int g, int b)
{
    for (int y = y0; y <= y1; y++)
        for (int x = x0; x <= x1; x++) put(x, y, r, g, b);
}

static void show(const char *what, const blob_feat_t *f)
{
    printf("   %-14s", what);
    for (int i = 0; i < BLOB_FEAT_N; i++)
        printf(" %s=%.3g", blob_feat_names[i], (double)f->v[i]);
    printf("\n");
}

enum { FILL, ASPECT, HSPREAD, SMEAN, SSTD, VMEAN, HIGHL, EDGE, TEXT, RING, CYN, LAREA };

/* ------------------------------------------------------------------ */

static void test_disc(void)
{
    printf("1. a flat red disc on grey: round, smooth, sharp-edged, isolated\n");
    blob_cfg_t c = red_cfg();
    fill_frame(120, 120, 124);
    disc(80.0f, 50.0f, 20.0f, 220, 30, 30);
    blob_t b = blob_of_frame(&c);
    blob_feat_t f;
    blob_features(&c, frame, &b, &f);
    show("disc r=20", &f);
    check(fabsf(f.v[FILL] - (float)M_PI / 4) < 0.02f, "disc fill is not pi/4");
    check(fabsf(f.v[ASPECT]) < 0.03f, "disc aspect is not 1");
    check(f.v[HSPREAD] < 1.0f, "a single colour has hue spread");
    check(f.v[SSTD] < 0.5f, "a single colour has saturation spread");
    check(f.v[HIGHL] == 0.0f, "no highlight was painted");
    check(f.v[TEXT] == 0.0f, "flat colour has texture");
    /* Luma of this red is 85, of the grey 121: a straight edge pixel sees
       a step of 36 on one axis, a corner on both. */
    check(f.v[EDGE] > 36.0f, "red on grey must give a strong edge");
    check(f.v[RING] == 0.0f, "nothing red outside the box");
    check(fabsf(f.v[CYN] - 50.0f / H) < 0.01f, "cy_norm wrong");
    check(fabsf(f.v[LAREA] - logf((float)b.area)) < 1e-5f, "log_area wrong");
}

static void test_rect(void)
{
    printf("2. rectangles: fill 1, aspect = ln(w/h)\n");
    blob_cfg_t c = red_cfg();
    int sizes[][2] = { { 40, 20 }, { 20, 40 }, { 10, 10 }, { 64, 8 } };
    for (size_t i = 0; i < sizeof sizes / sizeof sizes[0]; i++) {
        int w = sizes[i][0], h = sizes[i][1];
        fill_frame(120, 120, 124);
        rect(50, 30, 50 + w - 1, 30 + h - 1, 220, 30, 30);
        blob_t b = blob_of_frame(&c);
        blob_feat_t f;
        blob_features(&c, frame, &b, &f);
        printf("   %2dx%-2d fill %.4f aspect %+.4f (want %+.4f) edge %.1f texture %.1f\n",
               w, h, (double)f.v[FILL], (double)f.v[ASPECT], log((double)w / h),
               (double)f.v[EDGE], (double)f.v[TEXT]);
        check(f.v[FILL] == 1.0f, "rectangle fill is not 1");
        check(fabsf(f.v[ASPECT] - logf((float)w / (float)h)) < 1e-5f, "aspect wrong");
        check(f.v[TEXT] == 0.0f && f.v[EDGE] >= 36.0f, "flat rectangle: edge but no texture");
    }
}

static void test_frame_edge(void)
{
    printf("3. a blob cut by the frame edge: the cut is not an edge\n");
    blob_cfg_t c = red_cfg();
    fill_frame(220, 30, 30);                        /* all red: the box is the frame */
    blob_t b = blob_of_frame(&c);
    blob_feat_t f;
    blob_features(&c, frame, &b, &f);
    show("whole frame", &f);
    check(f.v[EDGE] == 0.0f, "frame border counted as an edge");
    check(f.v[RING] == 0.0f, "no ring exists outside the frame");
    check(f.v[FILL] == 1.0f, "whole frame fill is not 1");
}

static void test_highlight(void)
{
    printf("4. a white highlight inside the disc moves 'highlight' and nothing it should not\n");
    blob_cfg_t c = red_cfg();
    fill_frame(120, 120, 124);
    disc(80.0f, 60.0f, 20.0f, 220, 30, 30);
    rect(72, 48, 79, 55, 250, 250, 250);            /* 8x8 = 64 px */
    blob_t b = blob_of_frame(&c);
    blob_feat_t f;
    blob_features(&c, frame, &b, &f);
    int bw = b.x1 - b.x0 + 1, bh = b.y1 - b.y0 + 1;
    float want = 64.0f / (float)(bw * bh);
    show("disc+specular", &f);
    printf("   highlight %.4f, expected 64 / %d = %.4f\n", (double)f.v[HIGHL], bw * bh,
           (double)want);
    check(fabsf(f.v[HIGHL] - want) < 1e-6f, "highlight is not the white pixels' share");
    check(f.v[HSPREAD] < 1.0f, "the highlight leaked into the hue (it does not match)");
    check(f.v[TEXT] == 0.0f, "flat interior away from the hole must stay smooth");
}

static void test_hue_wrap(void)
{
    printf("5. hue straddling 0/360 (350 and 10 deg) is a 10 deg spread, not 170\n");
    blob_cfg_t c = red_cfg();
    fill_frame(120, 120, 124);
    /* hue 350: r max, b = r/6; hue 10: r max, g = r/6. Stripes two rows
       high: one row high would be invisible to the central difference. */
    for (int y = 40; y < 80; y++)
        for (int x = 60; x < 100; x++) {
            int t = (y >> 1) & 1;
            put(x, y, 240, t ? 40 : 0, t ? 0 : 40);
        }
    blob_t b = blob_of_frame(&c);
    blob_feat_t f;
    blob_features(&c, frame, &b, &f);
    int h1, h2, s, v;
    blob_rgb565_hsv(rgb(240, 40, 0), &h1, &s, &v);
    blob_rgb565_hsv(rgb(240, 0, 40), &h2, &s, &v);
    printf("   pixel hues %d and %d, spread %.2f deg, texture %.1f\n", h1, h2,
           (double)f.v[HSPREAD], (double)f.v[TEXT]);
    check(f.v[HSPREAD] > 5.0f && f.v[HSPREAD] < 15.0f, "hue spread does not wrap");
    check(f.v[TEXT] > 0.0f, "striped rows must read as texture");
}

static void test_ring(void)
{
    printf("6. 'ring' counts colour just outside the box, and only there\n");
    blob_cfg_t c = red_cfg();
    blob_feat_t f;
    fill_frame(120, 120, 124);
    rect(60, 40, 79, 59, 220, 30, 30);
    blob_t b = blob_of_frame(&c);
    /* Paint the whole 2-px ring red, then pretend the blob is only the inner
       rectangle (as when pooling splits a big region): ring must read 1. */
    rect(58, 38, 81, 61, 220, 30, 30);
    blob_features(&c, frame, &b, &f);
    printf("   ring fully red: %.3f\n", (double)f.v[RING]);
    check(f.v[RING] == 1.0f, "a red ring must read 1");
    /* Only the top band: rows 38-39 over x 58..81 = 48 of the ring's
       2*(24*2) + 2*(20*2) = 176 pixels. */
    fill_frame(120, 120, 124);
    rect(60, 40, 79, 59, 220, 30, 30);
    rect(58, 38, 81, 39, 220, 30, 30);
    blob_features(&c, frame, &b, &f);
    printf("   top band red:   %.4f (want 48/176 = %.4f)\n", (double)f.v[RING], 48.0 / 176);
    check(fabsf(f.v[RING] - 48.0f / 176.0f) < 1e-6f, "ring share wrong");
}

static void test_step(void)
{
    printf("7. sampling stride: at most %d samples, and the smallest stride that does it\n",
           BLOB_FEAT_SAMPLES);
    int bad = 0;
    for (int w = 1; w <= W; w++)
        for (int h = 1; h <= H; h++) {
            int s = blob_feat_step(w, h);
            int n = ((w + s - 1) / s) * ((h + s - 1) / s);
            int n1 = s > 1 ? ((w + s - 2) / (s - 1)) * ((h + s - 2) / (s - 1)) : 0;
            if (n > BLOB_FEAT_SAMPLES || (s > 1 && n1 <= BLOB_FEAT_SAMPLES)) bad++;
        }
    printf("   step(32,32) %d, step(160,120) %d, %d bad sizes\n",
           blob_feat_step(32, 32), blob_feat_step(160, 120), bad);
    check(bad == 0, "stride rule broken");
    check(blob_feat_step(32, 32) == 1, "a 32x32 blob must be read in full");
}

static void test_byte_order(void)
{
    printf("8. byte order changes nothing\n");
    blob_cfg_t c = red_cfg();
    fill_frame(120, 120, 124);
    disc(50.0f, 70.0f, 15.0f, 230, 40, 25);
    rect(44, 64, 47, 67, 250, 250, 250);
    for (size_t i = 0; i < sizeof frame; i += 2) {
        frame_le[i] = frame[i + 1];
        frame_le[i + 1] = frame[i];
    }
    blob_t b = blob_of_frame(&c);
    blob_cfg_t cl = c;
    cl.big_endian = 0;
    blob_feat_t fb, fl;
    blob_features(&c, frame, &b, &fb);
    blob_features(&cl, frame_le, &b, &fl);
    check(memcmp(&fb, &fl, sizeof fb) == 0, "features depend on byte order");
    blob_crop_box_t box;
    blob_crop_box(&b, &box);
    static uint16_t ca[BLOB_CROP * BLOB_CROP], cb[BLOB_CROP * BLOB_CROP];
    blob_crop(&c, frame, &box, ca);
    blob_crop(&cl, frame_le, &box, cb);
    check(memcmp(ca, cb, sizeof ca) == 0, "crop depends on byte order");
}

static void test_crop(void)
{
    printf("9. crops: box rule, flat colour stays flat, outside the frame is black\n");
    blob_cfg_t c = red_cfg();
    static uint16_t crop[BLOB_CROP * BLOB_CROP], small[BLOB_CROP_SMALL * BLOB_CROP_SMALL];
    uint16_t px = rgb(200, 100, 50);
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) put565(x, y, px);

    blob_t b = { .cx = 80.5f, .cy = 60.2f, .area = 100, .x0 = 70, .y0 = 55, .x1 = 89, .y1 = 64 };
    blob_crop_box_t box;
    blob_crop_box(&b, &box);
    printf("   20x10 box at (80.5, 60.2) -> square (%d, %d) side %d\n",
           box.xs, box.ys, box.side);
    check(box.side == 30 && box.xs == 65 && box.ys == 45, "crop square rule");
    blob_crop(&c, frame, &box, crop);
    blob_crop_down16(crop, small);
    int flat = 1;
    for (int i = 0; i < BLOB_CROP * BLOB_CROP; i++) flat &= crop[i] == px;
    for (int i = 0; i < BLOB_CROP_SMALL * BLOB_CROP_SMALL; i++) flat &= small[i] == px;
    check(flat, "a flat frame must give a flat crop at 24 and 16");

    blob_t tiny = { .cx = 3.0f, .cy = 2.5f, .area = 4, .x0 = 2, .y0 = 1, .x1 = 3, .y1 = 3 };
    blob_crop_box(&tiny, &box);
    printf("   2x3 box at (3.0, 2.5) -> square (%d, %d) side %d\n", box.xs, box.ys, box.side);
    check(box.side == BLOB_CROP_MIN_SIDE && box.xs == -3 && box.ys == -4,
          "tiny blob: minimum side, centred, partly outside");
    blob_crop(&c, frame, &box, crop);
    /* Side 12 -> each crop pixel is half a source pixel. Source column -3..-1
       and row -4..-1 are outside: crop columns 0-5 and rows 0-7 are black. */
    check(crop[0] == 0 && crop[7 * BLOB_CROP + 5] == 0, "outside the frame is not black");
    check(crop[8 * BLOB_CROP + 6] == px && crop[23 * BLOB_CROP + 23] == px,
          "inside the frame is not the frame");
}

int main(void)
{
    test_disc();
    test_rect();
    test_frame_edge();
    test_highlight();
    test_hue_wrap();
    test_ring();
    test_step();
    test_byte_order();
    test_crop();
    printf("\n%s (%d failure%s)\n", failures ? "FAILED" : "PASSED",
           failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
