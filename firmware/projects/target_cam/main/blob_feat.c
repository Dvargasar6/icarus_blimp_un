/*
 * blob_feat - see blob_feat.h for what and why.
 *
 * The features, in v[] order, and the reason each one might separate a mylar
 * balloon from the other red things in a room. Whether it actually does is a
 * measurement for Phase B2, on labelled data; these are the candidates.
 *
 *   0 fill        area / box area. A round balloon fills pi/4 = 0.785 of its
 *                 box; a door seen square-on fills ~1, a cable or a chair leg
 *                 much less.
 *   1 log_aspect  ln(box w / box h). 0 for a balloon at any range; a door is
 *                 tall (< 0), a table edge wide (> 0). The log makes tall and
 *                 wide symmetric.
 *   2 hue_spread  circular standard deviation of hue over the blob's pixels,
 *                 degrees, weighted by saturation (as blob_sample). Mylar
 *                 reflects the room, so its hue wanders more than paint.
 *   3 sat_mean    mean saturation of the blob's pixels, 0..255.
 *   4 sat_std     their standard deviation. Specular mylar: high; paint: low.
 *   5 val_mean    mean value (brightness), 0..255.
 *   6 highlight   fraction of the BOX's pixels that are bright and nearly
 *                 grey (V >= 200, S < 60): the specular highlight a shiny
 *                 sphere always shows somewhere, which the colour test
 *                 rejects and which therefore sits inside the box but outside
 *                 the blob.
 *   7 edge        mean luma gradient on the blob's boundary pixels. A balloon
 *                 against a wall has a hard edge; a pink-ish glow on a wall
 *                 fades out.
 *   8 texture     mean luma gradient on its interior pixels. Smooth paint is
 *                 low; mylar wrinkles and printed fabric are high.
 *   9 ring        fraction of the pixels in a 2-pixel ring just outside the
 *                 box that also pass the colour test. Near 0 for an isolated
 *                 object; high for one fragment of a larger red region (a
 *                 sofa broken up by shadow) that the cell pooling split off.
 *  10 cy_norm     centroid row / frame height. Where in the picture it is:
 *                 balloons float, floors and furniture sit low. Allowed as a
 *                 feature only because the camera's mounting is fixed; the
 *                 training report must show it is not the only thing learned.
 *  11 log_area    ln(area). Size alone says little (range varies), but it
 *                 conditions the others: a 30 px blob has no texture to show.
 *
 * Gradient: |L(x+1,y) - L(x-1,y)| + |L(x,y+1) - L(x,y-1)|, coordinates
 * clamped to the frame, with L = (77 R + 150 G + 29 B) >> 8 (Rec. 601 luma in
 * integers) on the 8-bit channels. A blob pixel is a boundary pixel when one
 * of its four neighbours inside the frame fails the colour test; a neighbour
 * outside the frame counts as passing, so the frame edge cutting a blob is
 * not mistaken for the blob's own edge.
 *
 * Approximations, accepted and documented: the blob's pixels are re-found as
 * the pixels in its box that pass the colour test, so a second blob reaching
 * into the box, or matching pixels that the cell pooling discarded, count as
 * this blob's. And the box is sampled on a grid (blob_feat_step), so for a
 * large blob every feature but fill, aspect, cy and area is an estimate from
 * at most BLOB_FEAT_SAMPLES pixels.
 *
 * Board and PC must agree. All per-pixel work is integer (the hue vector uses
 * a Q14 sine table rather than sinf/cosf), so the sums are bit-identical on
 * both; only the final few float operations (logf, sqrtf) can differ, by an
 * ulp at most.
 */
#include "blob_feat.h"

#include <math.h>
#include <stddef.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

const char *const blob_feat_names[BLOB_FEAT_N] = {
    "fill", "log_aspect", "hue_spread", "sat_mean", "sat_std", "val_mean",
    "highlight", "edge", "texture", "ring", "cy_norm", "log_area",
};

/* sin(d degrees) * 16384, d = 0..90. */
static const int16_t k_sin_q14[91] = {
        0,   286,   572,   857,  1143,  1428,  1713,  1997,  2280,  2563,
     2845,  3126,  3406,  3686,  3964,  4240,  4516,  4790,  5063,  5334,
     5604,  5872,  6138,  6402,  6664,  6924,  7182,  7438,  7692,  7943,
     8192,  8438,  8682,  8923,  9162,  9397,  9630,  9860, 10087, 10311,
    10531, 10749, 10963, 11174, 11381, 11585, 11786, 11982, 12176, 12365,
    12551, 12733, 12911, 13085, 13255, 13421, 13583, 13741, 13894, 14044,
    14189, 14330, 14466, 14598, 14726, 14849, 14968, 15082, 15191, 15296,
    15396, 15491, 15582, 15668, 15749, 15826, 15897, 15964, 16026, 16083,
    16135, 16182, 16225, 16262, 16294, 16322, 16344, 16362, 16374, 16382,
    16384,
};

static int sin_q14(int d)               /* d = 0..359 */
{
    if (d < 90)  return k_sin_q14[d];
    if (d < 180) return k_sin_q14[180 - d];
    if (d < 270) return -k_sin_q14[d - 180];
    return -k_sin_q14[360 - d];
}

static int cos_q14(int d)
{
    return sin_q14(d < 270 ? d + 90 : d - 270);
}

/* Luma of pixel (x, y), coordinates clamped into the frame. Channels expanded
   to 8 bits as blob_detect.c's unpack() does. */
static int luma(const blob_cfg_t *c, const uint8_t *frame, int x, int y)
{
    if (x < 0) x = 0;
    if (x >= c->width) x = c->width - 1;
    if (y < 0) y = 0;
    if (y >= c->height) y = c->height - 1;
    uint16_t px = blob_pixel(c, frame, x, y);
    int r5 = (px >> 11) & 0x1f, g6 = (px >> 5) & 0x3f, b5 = px & 0x1f;
    int r = (r5 << 3) | (r5 >> 2), g = (g6 << 2) | (g6 >> 4), b = (b5 << 3) | (b5 >> 2);
    return (77 * r + 150 * g + 29 * b) >> 8;
}

/* Colour test at (x, y); outside the frame counts as a match (see above). */
static int matches_at(const blob_cfg_t *c, const uint8_t *frame, int x, int y)
{
    if (x < 0 || y < 0 || x >= c->width || y >= c->height) return 1;
    return blob_pixel_matches(c, blob_pixel(c, frame, x, y));
}

int blob_feat_step(int w, int h)
{
    int s = 1;
    while (((w + s - 1) / s) * ((h + s - 1) / s) > BLOB_FEAT_SAMPLES) s++;
    return s;
}

/* Ring pixels: count them and the matching ones. Pixels outside the frame are
   not part of the ring at all (unlike the boundary test, here "outside" must
   not count as either answer). */
static void ring_px(const blob_cfg_t *c, const uint8_t *frame, int x, int y,
                    int *n, int *m)
{
    if (x < 0 || y < 0 || x >= c->width || y >= c->height) return;
    (*n)++;
    *m += blob_pixel_matches(c, blob_pixel(c, frame, x, y));
}

void blob_features(const blob_cfg_t *c, const uint8_t *frame, const blob_t *b,
                   blob_feat_t *out)
{
    int w = b->x1 - b->x0 + 1, h = b->y1 - b->y0 + 1;
    int step = blob_feat_step(w, h);

    int n_box = 0, n_hi = 0, n_m = 0, n_e = 0, n_t = 0;
    uint32_t sum_s = 0, sum_s2 = 0, sum_v = 0, sum_ge = 0, sum_gt = 0, wsum = 0;
    int64_t hc = 0, hs = 0;             /* sat-weighted hue vector, Q14 */

    for (int y = b->y0; y <= b->y1; y += step) {
        for (int x = b->x0; x <= b->x1; x += step) {
            uint16_t px = blob_pixel(c, frame, x, y);
            int hue, s, v;
            blob_rgb565_hsv(px, &hue, &s, &v);
            n_box++;
            if (v >= 200 && s < 60) n_hi++;
            if (!blob_pixel_matches(c, px)) continue;

            n_m++;
            sum_s += (uint32_t)s;
            sum_s2 += (uint32_t)(s * s);
            sum_v += (uint32_t)v;
            wsum += (uint32_t)s;
            hc += (int64_t)s * cos_q14(hue);
            hs += (int64_t)s * sin_q14(hue);

            int gx = luma(c, frame, x + 1, y) - luma(c, frame, x - 1, y);
            int gy = luma(c, frame, x, y + 1) - luma(c, frame, x, y - 1);
            uint32_t g = (uint32_t)((gx < 0 ? -gx : gx) + (gy < 0 ? -gy : gy));
            int edge = !matches_at(c, frame, x - 1, y) || !matches_at(c, frame, x + 1, y) ||
                       !matches_at(c, frame, x, y - 1) || !matches_at(c, frame, x, y + 1);
            if (edge) { n_e++; sum_ge += g; }
            else      { n_t++; sum_gt += g; }
        }
    }

    /* The ring: two full rows above and below (corners included), two
       columns either side of the box's rows. Sampled with the same step along
       its length, every pixel across its 2-pixel width. */
    int n_r = 0, m_r = 0;
    for (int x = b->x0 - 2; x <= b->x1 + 2; x += step) {
        for (int d = 1; d <= 2; d++) {
            ring_px(c, frame, x, b->y0 - d, &n_r, &m_r);
            ring_px(c, frame, x, b->y1 + d, &n_r, &m_r);
        }
    }
    for (int y = b->y0; y <= b->y1; y += step) {
        for (int d = 1; d <= 2; d++) {
            ring_px(c, frame, b->x0 - d, y, &n_r, &m_r);
            ring_px(c, frame, b->x1 + d, y, &n_r, &m_r);
        }
    }

    float *f = out->v;
    f[0] = (float)b->area / (float)(w * h);
    f[1] = logf((float)w / (float)h);

    /* Hue spread as blob_sample computes it: sqrt(-2 ln R) of the mean
       resultant length R. No colour at all (wsum 0) is total spread. */
    float spread = 180.0f;
    if (wsum > 0) {
        float norm = (float)wsum * 16384.0f;
        float vc = (float)hc / norm, vs = (float)hs / norm;
        float r = sqrtf(vc * vc + vs * vs);
        if (r > 1.0f) r = 1.0f;         /* the table's rounding can just exceed it */
        float l = r > 1e-6f ? -2.0f * logf(r) : 1e9f;
        spread = l > 0.0f ? sqrtf(l) * (180.0f / (float)M_PI) : 0.0f;
        if (spread > 180.0f) spread = 180.0f;
    }
    f[2] = spread;

    if (n_m > 0) {
        float ms = (float)sum_s / (float)n_m;
        float var = (float)sum_s2 / (float)n_m - ms * ms;
        f[3] = ms;
        f[4] = var > 0.0f ? sqrtf(var) : 0.0f;
        f[5] = (float)sum_v / (float)n_m;
    } else {
        f[3] = f[4] = f[5] = 0.0f;
    }
    f[6] = n_box ? (float)n_hi / (float)n_box : 0.0f;
    f[7] = n_e ? (float)sum_ge / (float)n_e : 0.0f;
    f[8] = n_t ? (float)sum_gt / (float)n_t : 0.0f;
    f[9] = n_r ? (float)m_r / (float)n_r : 0.0f;
    f[10] = b->cy / (float)c->height;
    f[11] = logf((float)(b->area > 0 ? b->area : 1));
}

void blob_crop_box(const blob_t *b, blob_crop_box_t *out)
{
    int w = b->x1 - b->x0 + 1, h = b->y1 - b->y0 + 1;
    int m = w > h ? w : h;
    int side = (3 * m + 1) / 2;
    if (side < BLOB_CROP_MIN_SIDE) side = BLOB_CROP_MIN_SIDE;
    out->side = side;
    out->xs = (int)floorf(b->cx) - side / 2;
    out->ys = (int)floorf(b->cy) - side / 2;
}

void blob_crop(const blob_cfg_t *c, const uint8_t *frame, const blob_crop_box_t *box,
               uint16_t out[BLOB_CROP * BLOB_CROP])
{
    int S = box->side;
    for (int i = 0; i < BLOB_CROP; i++) {
        int y0 = box->ys + i * S / BLOB_CROP, y1 = box->ys + (i + 1) * S / BLOB_CROP;
        if (y1 <= y0) y1 = y0 + 1;
        for (int j = 0; j < BLOB_CROP; j++) {
            int x0 = box->xs + j * S / BLOB_CROP, x1 = box->xs + (j + 1) * S / BLOB_CROP;
            if (x1 <= x0) x1 = x0 + 1;
            uint32_t sr = 0, sg = 0, sb = 0;
            uint32_t n = (uint32_t)((x1 - x0) * (y1 - y0));
            for (int y = y0; y < y1; y++) {
                if (y < 0 || y >= c->height) continue;
                for (int x = x0; x < x1; x++) {
                    if (x < 0 || x >= c->width) continue;
                    uint16_t px = blob_pixel(c, frame, x, y);
                    sr += (px >> 11) & 0x1fu;
                    sg += (px >> 5) & 0x3fu;
                    sb += px & 0x1fu;
                }
            }
            uint32_t r = (sr + n / 2) / n, g = (sg + n / 2) / n, bl = (sb + n / 2) / n;
            out[i * BLOB_CROP + j] = (uint16_t)(r << 11 | g << 5 | bl);
        }
    }
}

void blob_crop_down16(const uint16_t in[BLOB_CROP * BLOB_CROP],
                      uint16_t out[BLOB_CROP_SMALL * BLOB_CROP_SMALL])
{
    for (int i = 0; i < BLOB_CROP_SMALL; i++) {
        for (int j = 0; j < BLOB_CROP_SMALL; j++) {
            uint32_t sr = 0, sg = 0, sb = 0;
            for (int a = 0; a < 3; a++) {
                for (int bb = 0; bb < 3; bb++) {
                    uint16_t px = in[((3 * i + a) / 2) * BLOB_CROP + (3 * j + bb) / 2];
                    sr += (px >> 11) & 0x1fu;
                    sg += (px >> 5) & 0x3fu;
                    sb += px & 0x1fu;
                }
            }
            out[i * BLOB_CROP_SMALL + j] =
                (uint16_t)((sr + 4) / 9 << 11 | (sg + 4) / 9 << 5 | (sb + 4) / 9);
        }
    }
}
