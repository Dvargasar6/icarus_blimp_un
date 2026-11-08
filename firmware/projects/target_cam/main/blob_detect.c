/*
 * blob_detect - see blob_detect.h for what and why.
 *
 * Integer arithmetic on the per-pixel path, no heap, no globals. The only float
 * work is the final centroid division and blob_sample(), which runs once on a
 * calibration command, not per frame.
 */
#include "blob_detect.h"

#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* Workspace layout, in this order so every array is naturally aligned when the
   base is 4-byte aligned:
 *
 *   uint32_t area[L]     matching pixels per provisional label (roots only)
 *   uint16_t label[N]    label per cell, 0 = background
 *   uint16_t parent[L]   union-find forest over labels
 *   uint8_t  count[N]    matching pixels per cell
 *
 * N is the cell count. With 4-connectivity a new label is only created when
 * neither the left nor the upper neighbour is set, so the worst case - a
 * checkerboard - creates ceil(N/2) labels. L = N/2 + 2 covers that plus the
 * reserved label 0, which is what makes the label arrays safe against any
 * input, including a frame of pure noise. */

static int grid_w(const blob_cfg_t *c) { return c->width / c->cell; }
static int grid_h(const blob_cfg_t *c) { return c->height / c->cell; }
static size_t n_cells(const blob_cfg_t *c) { return (size_t)grid_w(c) * (size_t)grid_h(c); }
static size_t n_labels(const blob_cfg_t *c) { return n_cells(c) / 2 + 2; }

int blob_cfg_valid(const blob_cfg_t *c)
{
    if (c == NULL) return 0;
    if (c->width < 4 || c->height < 4) return 0;
    if (c->cell != 1 && c->cell != 2 && c->cell != 4) return 0;
    if (c->width % c->cell != 0 || c->height % c->cell != 0) return 0;
    if (n_cells(c) > 65534) return 0;               /* labels are uint16 */
    if (c->hue < 0 || c->hue > 359) return 0;
    if (c->hue_tol < 1 || c->hue_tol > 90) return 0;
    if (c->sat_min < 0 || c->sat_min > 255) return 0;
    if (c->val_min < 0 || c->val_min > 255) return 0;
    if (c->min_area < 1) return 0;
    return 1;
}

size_t blob_workspace_bytes(const blob_cfg_t *c)
{
    size_t n = n_cells(c), l = n_labels(c);
    return l * sizeof(uint32_t) + n * sizeof(uint16_t) + l * sizeof(uint16_t) + n;
}

uint16_t blob_pixel(const blob_cfg_t *c, const uint8_t *frame, int x, int y)
{
    const uint8_t *p = frame + ((size_t)y * (size_t)c->width + (size_t)x) * 2;
    return (uint16_t)(c->big_endian ? (p[0] << 8) | p[1] : (p[1] << 8) | p[0]);
}

/* 5/6-bit channels expanded to 8 bits by bit replication, so full scale maps
   to 255 rather than 248 and grey stays grey. */
static void unpack(uint16_t px, int *r, int *g, int *b)
{
    int r5 = (px >> 11) & 0x1f, g6 = (px >> 5) & 0x3f, b5 = px & 0x1f;
    *r = (r5 << 3) | (r5 >> 2);
    *g = (g6 << 2) | (g6 >> 4);
    *b = (b5 << 3) | (b5 >> 2);
}

static int hue_of(int r, int g, int b, int mx, int d)
{
    int h;
    if (mx == r)      h =       60 * (g - b) / d;
    else if (mx == g) h = 120 + 60 * (b - r) / d;
    else              h = 240 + 60 * (r - g) / d;
    return h < 0 ? h + 360 : h;
}

void blob_rgb565_hsv(uint16_t px, int *hue, int *sat, int *val)
{
    int r, g, b;
    unpack(px, &r, &g, &b);
    int mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    int mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
    int d = mx - mn;
    *val = mx;
    *sat = mx ? d * 255 / mx : 0;
    *hue = d ? hue_of(r, g, b, mx, d) : 0;
}

int blob_pixel_matches(const blob_cfg_t *c, uint16_t px)
{
    if (c->lut) return (c->lut[px >> 3] >> (px & 7)) & 1;

    int r, g, b;
    unpack(px, &r, &g, &b);
    int mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    int mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
    int d = mx - mn;

    /* Cheap rejections first, with no division: most of a frame is wall,
       floor and sky, and it fails one of these. sat >= sat_min is tested as
       d*255 >= sat_min*mx, which is the same comparison without the divide. */
    if (mx < c->val_min || d == 0) return 0;
    if (d * 255 < c->sat_min * mx) return 0;

    int dh = hue_of(r, g, b, mx, d) - c->hue;
    if (dh < 0) dh = -dh;
    if (dh > 180) dh = 360 - dh;                    /* red wraps through 0 */
    return dh <= c->hue_tol;
}

int blob_mask(const blob_cfg_t *c, const uint8_t *frame, uint8_t *mask)
{
    int n = 0;
    for (int y = 0; y < c->height; y++)
        for (int x = 0; x < c->width; x++) {
            int m = blob_pixel_matches(c, blob_pixel(c, frame, x, y));
            mask[(size_t)y * (size_t)c->width + (size_t)x] = (uint8_t)m;
            n += m;
        }
    return n;
}

static uint16_t uf_find(uint16_t *parent, uint16_t a)
{
    while (parent[a] != a) {
        parent[a] = parent[parent[a]];              /* path halving */
        a = parent[a];
    }
    return a;
}

static void uf_union(uint16_t *parent, uint16_t a, uint16_t b)
{
    a = uf_find(parent, a);
    b = uf_find(parent, b);
    if (a < b)      parent[b] = a;
    else if (b < a) parent[a] = b;
}

/* Report slot whose label sits on cell i, or on one of its 8 neighbours when
   `near` is set; -1 if none. */
static int slot_of(const uint16_t *label, const uint16_t *top, int ntop,
                   int gw, int gh, int gx, int gy, int near)
{
    for (int y = gy - near; y <= gy + near; y++)
        for (int x = gx - near; x <= gx + near; x++) {
            if (x < 0 || y < 0 || x >= gw || y >= gh) continue;
            uint16_t l = label[(size_t)y * (size_t)gw + (size_t)x];
            for (int k = 0; l && k < ntop; k++)
                if (top[k] == l) return k;
        }
    return -1;
}

int blob_detect(const blob_cfg_t *c, const uint8_t *frame, void *work,
                blob_result_t *out)
{
    int32_t unused;
    return blob_detect_timed(c, frame, work, out, NULL, &unused);
}

int blob_detect_timed(const blob_cfg_t *c, const uint8_t *frame, void *work,
                      blob_result_t *out, blob_clock_fn now_us, int32_t *t_px_us)
{
    blob_result_t z = {0};
    *out = z;
    *t_px_us = 0;
    int64_t t0 = now_us ? now_us() : 0;

    const int gw = grid_w(c), gh = grid_h(c), cs = c->cell;
    const size_t n = n_cells(c), l = n_labels(c);
    uint32_t *area   = (uint32_t *)work;
    uint16_t *label  = (uint16_t *)(area + l);
    uint16_t *parent = label + n;
    uint8_t  *count  = (uint8_t *)(parent + l);

    /* Pass 1: classify pixels, pool into cells. */
    int matched = 0;
    for (int gy = 0; gy < gh; gy++) {
        for (int gx = 0; gx < gw; gx++) {
            int k = 0;
            for (int y = gy * cs; y < (gy + 1) * cs; y++)
                for (int x = gx * cs; x < (gx + 1) * cs; x++)
                    k += blob_pixel_matches(c, blob_pixel(c, frame, x, y));
            count[(size_t)gy * (size_t)gw + (size_t)gx] = (uint8_t)k;
            matched += k;
        }
    }
    out->matched = matched;
    if (now_us) *t_px_us = (int32_t)(now_us() - t0);

    /* A cell is foreground when at least half its pixels match. Isolated noise
       pixels never reach that with cells of 2 or 4, so speckle costs nothing
       downstream. With cell 1 every matching pixel counts. */
    const int need = (cs * cs + 1) / 2;

    /* Pass 2: provisional labels, 4-connectivity, equivalences in the forest. */
    uint16_t next = 1;
    for (int gy = 0; gy < gh; gy++) {
        for (int gx = 0; gx < gw; gx++) {
            size_t i = (size_t)gy * (size_t)gw + (size_t)gx;
            if (count[i] < need) { label[i] = 0; continue; }
            uint16_t left = gx > 0 ? label[i - 1] : 0;
            uint16_t up   = gy > 0 ? label[i - (size_t)gw] : 0;
            if (left && up) {
                label[i] = left;
                uf_union(parent, left, up);
            } else if (left || up) {
                label[i] = left ? left : up;
            } else {
                parent[next] = next;
                area[next] = 0;
                label[i] = next++;
            }
        }
    }

    /* Pass 3: resolve to roots, accumulate area per root. */
    for (size_t i = 0; i < n; i++) {
        if (label[i]) {
            label[i] = uf_find(parent, label[i]);
            area[label[i]] += count[i];
        }
    }

    /* Keep the BLOB_MAX_REPORT largest roots, largest first (insertion into a
       tiny sorted array). The course has four identical red balloons, so the
       caller needs all of them, not just the nearest. */
    uint16_t top[BLOB_MAX_REPORT];
    uint32_t top_a[BLOB_MAX_REPORT];
    int ntop = 0;
    for (uint16_t k = 1; k < next; k++) {
        if (parent[k] != k || area[k] < (uint32_t)c->min_area) continue;
        out->n_blobs++;
        int j = ntop < BLOB_MAX_REPORT ? ntop++ : BLOB_MAX_REPORT;
        while (j > 0 && top_a[j - 1] < area[k]) {
            if (j < BLOB_MAX_REPORT) { top[j] = top[j - 1]; top_a[j] = top_a[j - 1]; }
            j--;
        }
        if (j < BLOB_MAX_REPORT) { top[j] = k; top_a[j] = area[k]; }
    }
    if (ntop == 0) return 0;

    /* Pass 4: exact moments and boxes of the reported blobs, from their
       matching PIXELS. Taken from each blob's own cells and also from any
       below-threshold cell touching it: along a curved edge many cells hold
       only one matching pixel, and dropping them all biased the centroid by up
       to 0.07 px and the area by 3% on the host suite. Speckle further away is
       still excluded. */
    double sx[BLOB_MAX_REPORT] = {0}, sy[BLOB_MAX_REPORT] = {0};
    for (int k = 0; k < ntop; k++) {
        out->b[k].x0 = c->width;  out->b[k].y0 = c->height;
        out->b[k].x1 = -1;        out->b[k].y1 = -1;
    }
    for (int gy = 0; gy < gh; gy++) {
        for (int gx = 0; gx < gw; gx++) {
            size_t i = (size_t)gy * (size_t)gw + (size_t)gx;
            int k;
            if (label[i])          k = slot_of(label, top, ntop, gw, gh, gx, gy, 0);
            else if (count[i] > 0) k = slot_of(label, top, ntop, gw, gh, gx, gy, 1);
            else                   continue;
            if (k < 0) continue;
            blob_t *b = &out->b[k];
            for (int y = gy * cs; y < (gy + 1) * cs; y++) {
                for (int x = gx * cs; x < (gx + 1) * cs; x++) {
                    if (!blob_pixel_matches(c, blob_pixel(c, frame, x, y))) continue;
                    b->area++;
                    sx[k] += x + 0.5;
                    sy[k] += y + 0.5;
                    if (x < b->x0) b->x0 = x;
                    if (x > b->x1) b->x1 = x;
                    if (y < b->y0) b->y0 = y;
                    if (y > b->y1) b->y1 = y;
                }
            }
        }
    }
    for (int k = 0; k < ntop; k++) {
        blob_t *b = &out->b[k];
        b->cx = (float)(sx[k] / b->area);
        b->cy = (float)(sy[k] / b->area);
        b->clipped = b->x0 == 0 || b->y0 == 0 ||
                     b->x1 == c->width - 1 || b->y1 == c->height - 1;
    }
    /* Edge pixels can reorder two near-equal blobs; sort by the exact area. */
    for (int i = 1; i < ntop; i++)
        for (int j = i; j > 0 && out->b[j - 1].area < out->b[j].area; j--) {
            blob_t t = out->b[j]; out->b[j] = out->b[j - 1]; out->b[j - 1] = t;
        }

    out->n_report = ntop;
    out->found = 1;
    return 1;
}

/* Hue windows: red 348..12, orange 15..35 - a gap between them, because a red
   balloon's lit side drifts toward orange and an orange one's shadow toward
   red. Green and blue are far from everything and get wide windows. The
   saturation floor is the main defence against skin, wood and walls. */
static const struct {
    const char *name;
    int hue, tol, sat, val;
} k_presets[BLOB_PRESET_COUNT] = {
    [BLOB_PRESET_RED]    = { "red",      0, 12, 110, 50 },
    [BLOB_PRESET_BLUE]   = { "blue",   220, 30,  90, 40 },
    [BLOB_PRESET_GREEN]  = { "green",  125, 35,  80, 40 },
    [BLOB_PRESET_ORANGE] = { "orange",  25, 10, 120, 60 },
};

int blob_preset_apply(blob_cfg_t *c, int p)
{
    if (p < 0 || p >= BLOB_PRESET_COUNT) return 0;
    c->hue = k_presets[p].hue;
    c->hue_tol = k_presets[p].tol;
    c->sat_min = k_presets[p].sat;
    c->val_min = k_presets[p].val;
    return 1;
}

const char *blob_preset_name(int p)
{
    return p >= 0 && p < BLOB_PRESET_COUNT ? k_presets[p].name : "?";
}

int blob_preset_find(const char *name)
{
    if (name == NULL || name[0] == '\0') return -1;
    char c0 = name[0];
    if (c0 >= 'A' && c0 <= 'Z') c0 = (char)(c0 - 'A' + 'a');
    for (int p = 0; p < BLOB_PRESET_COUNT; p++)
        if (k_presets[p].name[0] == c0) return p;
    return -1;
}

void blob_sample(const blob_cfg_t *c, const uint8_t *frame,
                 int x0, int y0, int w, int h, blob_color_t *out)
{
    double sc = 0.0, ss = 0.0, wsum = 0.0, sat = 0.0, val = 0.0;
    int n = 0;
    for (int y = y0; y < y0 + h; y++) {
        for (int x = x0; x < x0 + w; x++) {
            int hh, s, v;
            blob_rgb565_hsv(blob_pixel(c, frame, x, y), &hh, &s, &v);
            double a = hh * M_PI / 180.0;
            sc += s * cos(a);
            ss += s * sin(a);
            wsum += s;
            sat += s;
            val += v;
            n++;
        }
    }
    blob_color_t z = {0};
    *out = z;
    if (n == 0) return;
    out->sat = (int)(sat / n + 0.5);
    out->val = (int)(val / n + 0.5);
    if (wsum <= 0.0) {
        out->hue_spread = 180;                      /* grey: no hue at all */
        return;
    }
    double deg = atan2(ss, sc) * 180.0 / M_PI;
    if (deg < 0.0) deg += 360.0;
    out->hue = (int)(deg + 0.5) % 360;
    double r = sqrt(sc * sc + ss * ss) / wsum;      /* mean resultant length */
    double sd = r > 1e-6 ? sqrt(-2.0 * log(r)) * 180.0 / M_PI : 180.0;
    out->hue_spread = sd > 180.0 ? 180 : (int)(sd + 0.5);
}
