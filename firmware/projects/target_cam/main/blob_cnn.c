/*
 * blob_cnn - see blob_cnn.h for what and why.
 */
#include "blob_cnn.h"

#include <math.h>
#include <string.h>

#define MAX_N   24

/* Activations, channel-major: a[c][y][x]. Sized for N = 24 and 16 channels
   (9 kB for the first layer); static, not on the 4 kB-ish task stack. */
static uint8_t s_in[3 * MAX_N * MAX_N];
static uint8_t s_a1[BLOB_CNN_MAX_C * MAX_N * MAX_N];
static uint8_t s_p1[BLOB_CNN_MAX_C * (MAX_N / 2) * (MAX_N / 2)];
static uint8_t s_a2[BLOB_CNN_MAX_C * (MAX_N / 2) * (MAX_N / 2)];
static uint8_t s_p2[BLOB_CNN_MAX_C * (MAX_N / 4) * (MAX_N / 4)];
static uint8_t s_a3[BLOB_CNN_MAX_C * (MAX_N / 4) * (MAX_N / 4)];

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 |
           (uint32_t)p[3] << 24;
}

static float rdf(const uint8_t *p)
{
    uint32_t u = rd32(p);
    float f;
    memcpy(&f, &u, sizeof f);
    return f;
}

static size_t conv_bytes(int cin, int cout)
{
    return (size_t)cout * (size_t)cin * 9u + 4u * (size_t)cout + 4u + 1u;
}

size_t blob_cnn_payload_bytes(int c1, int c2, int c3)
{
    return 16 + conv_bytes(3, c1) + conv_bytes(c1, c2) + conv_bytes(c2, c3) +
           (size_t)c3 + 4;
}

tc_model_err_t blob_cnn_load(const uint8_t *rec, size_t len, blob_cnn_t *out,
                             uint32_t *train_id)
{
    /* The payload size depends on the shape, which is inside the payload:
       take the size the header claims, let tc_model_open check it against
       the bytes and the CRC, then check it against the shape. */
    if (len < TC_MODEL_HDR_BYTES) return TC_MODEL_SHORT;
    uint32_t plen = rd32(rec + 8);
    const uint8_t *p;
    uint32_t id;
    tc_model_err_t e = tc_model_open(rec, len, BLOB_CNN_MAGIC, BLOB_CNN_VERSION, plen, &p, &id);
    if (e != TC_MODEL_OK) return e;
    if (plen < 16) return TC_MODEL_SIZE;
    int n = p[0], ch[3] = { p[1], p[2], p[3] };
    for (int i = 0; i < 3; i++) {
        if (ch[i] < 1 || ch[i] > BLOB_CNN_MAX_C) return TC_MODEL_VALUE;
    }
    if (plen != blob_cnn_payload_bytes(ch[0], ch[1], ch[2])) return TC_MODEL_SIZE;
    if (n != 16 && n != 24) return TC_MODEL_VALUE;

    /* Unpack into a static copy first, so a refusal leaves *out as it was
       (the struct is about 5 kB: too big for the stack). */
    static blob_cnn_t c;
    memset(&c, 0, sizeof c);
    c.n = n;
    c.lut_id = rd32(p + 4);
    c.threshold = rdf(p + 8);
    c.logit_scale = rdf(p + 12);
    if (!(c.threshold > 0.0f && c.threshold < 1.0f) || !isfinite(c.logit_scale) ||
        !(c.logit_scale > 0.0f)) {
        return TC_MODEL_VALUE;
    }
    const uint8_t *q = p + 16;
    int cin = 3;
    for (int l = 0; l < 3; l++) {
        blob_cnn_conv_t *L = &c.conv[l];
        L->cin = cin;
        L->cout = ch[l];
        size_t nw = (size_t)L->cout * (size_t)cin * 9u;
        memcpy(L->w, q, nw);            /* int8: no byte order */
        q += nw;
        for (int o = 0; o < L->cout; o++, q += 4) L->bias[o] = (int32_t)rd32(q);
        L->mult = (int32_t)rd32(q);
        q += 4;
        L->shift = *q++;
        if (L->mult <= 0 || L->shift < 1 || L->shift > 62) return TC_MODEL_VALUE;
        cin = L->cout;
    }
    memcpy(c.dense_w, q, (size_t)ch[2]);
    q += ch[2];
    c.dense_bias = (int32_t)rd32(q);
    *out = c;
    *train_id = id;
    return TC_MODEL_OK;
}

/* conv 3x3, zero padding, ReLU, requantise: in[cin][n][n] -> out[cout][n][n]. */
static void conv(const blob_cnn_conv_t *L, const uint8_t *in, uint8_t *out, int n)
{
    const int64_t half = (int64_t)1 << (L->shift - 1);
    const size_t nn = (size_t)(n * n);
    for (int o = 0; o < L->cout; o++) {
        const int8_t *wo = L->w + (size_t)o * (size_t)L->cin * 9u;
        for (int y = 0; y < n; y++) {
            for (int x = 0; x < n; x++) {
                int32_t acc = L->bias[o];
                for (int ci = 0; ci < L->cin; ci++) {
                    const uint8_t *ip = in + (size_t)ci * nn;
                    const int8_t *wk = wo + ci * 9;
                    for (int ky = 0; ky < 3; ky++) {
                        int yy = y + ky - 1;
                        if (yy < 0 || yy >= n) continue;
                        for (int kx = 0; kx < 3; kx++) {
                            int xx = x + kx - 1;
                            if (xx < 0 || xx >= n) continue;
                            acc += (int32_t)wk[ky * 3 + kx] * ip[yy * n + xx];
                        }
                    }
                }
                int32_t v = 0;
                if (acc > 0) {
                    int64_t r = ((int64_t)acc * L->mult + half) >> L->shift;
                    v = r > 255 ? 255 : (int32_t)r;
                }
                out[(size_t)o * nn + (size_t)(y * n + x)] = (uint8_t)v;
            }
        }
    }
}

/* max-pool 2x2: in[c][n][n] -> out[c][n/2][n/2]. */
static void pool(const uint8_t *in, uint8_t *out, int c, int n)
{
    int m = n / 2;
    for (int k = 0; k < c; k++) {
        const uint8_t *ip = in + (size_t)k * (size_t)(n * n);
        for (int y = 0; y < m; y++) {
            for (int x = 0; x < m; x++) {
                const uint8_t *a = ip + (2 * y) * n + 2 * x;
                uint8_t v = a[0];
                if (a[1] > v) v = a[1];
                if (a[n] > v) v = a[n];
                if (a[n + 1] > v) v = a[n + 1];
                out[(size_t)k * (size_t)(m * m) + (size_t)(y * m + x)] = v;
            }
        }
    }
}

int32_t blob_cnn_sum(const blob_cnn_t *cnn, const uint16_t *crop)
{
    int n = cnn->n;
    /* RGB565 -> 8-bit channels by bit replication, as blob_detect does. */
    for (int i = 0; i < n * n; i++) {
        uint16_t v = crop[i];
        int r5 = v >> 11, g6 = (v >> 5) & 0x3f, b5 = v & 0x1f;
        s_in[i] = (uint8_t)(r5 << 3 | r5 >> 2);
        s_in[n * n + i] = (uint8_t)(g6 << 2 | g6 >> 4);
        s_in[2 * n * n + i] = (uint8_t)(b5 << 3 | b5 >> 2);
    }
    conv(&cnn->conv[0], s_in, s_a1, n);
    pool(s_a1, s_p1, cnn->conv[0].cout, n);
    conv(&cnn->conv[1], s_p1, s_a2, n / 2);
    pool(s_a2, s_p2, cnn->conv[1].cout, n / 2);
    conv(&cnn->conv[2], s_p2, s_a3, n / 4);
    int m = n / 4, cnt = m * m;
    int32_t acc = cnn->dense_bias;
    for (int c = 0; c < cnn->conv[2].cout; c++) {
        int32_t s = 0;
        for (int i = 0; i < cnt; i++) s += s_a3[(size_t)(c * cnt + i)];
        int32_t g = (s + cnt / 2) / cnt;            /* round half up; s >= 0 */
        acc += (int32_t)cnn->dense_w[c] * g;
    }
    return acc;
}

float blob_cnn_conf(const blob_cnn_t *cnn, const uint16_t *crop)
{
    float logit = (float)blob_cnn_sum(cnn, crop) * cnn->logit_scale;
    return 1.0f / (1.0f + expf(-logit));
}

long blob_cnn_macs(const blob_cnn_t *cnn)
{
    long n = cnn->n, c1 = cnn->conv[0].cout, c2 = cnn->conv[1].cout, c3 = cnn->conv[2].cout;
    return n * n * c1 * 27 + (n / 2) * (n / 2) * c2 * c1 * 9 + (n / 4) * (n / 4) * c3 * c2 * 9 + c3;
}

size_t blob_cnn_struct_bytes(void)
{
    return sizeof(blob_cnn_t);
}
