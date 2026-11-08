/*
 * model_store - see model_store.h for what and why.
 */
#include "model_store.h"

#include <string.h>

#define PART_SUBTYPE    0x40
#define PART_LABEL      "tcmodels"

static const struct {
    const char *name;
    uint32_t off, size;
} k_slot[MS_KINDS] = {
    { "lut", 0x0000, 0x4000 },
    { "clf", 0x4000, 0x1000 },
    { "cnn", 0x5000, 0x4000 },
};

static const esp_partition_t *s_part;

bool ms_init(void)
{
    s_part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, PART_SUBTYPE, PART_LABEL);
    return s_part != NULL && s_part->size >= k_slot[MS_CNN].off + k_slot[MS_CNN].size;
}

bool ms_available(void)
{
    return s_part != NULL;
}

const char *ms_name(ms_kind_t k)
{
    return (unsigned)k < MS_KINDS ? k_slot[k].name : "?";
}

int ms_kind(const char *name)
{
    for (int k = 0; k < MS_KINDS; k++) {
        if (strcmp(name, k_slot[k].name) == 0) return k;
    }
    return -1;
}

size_t ms_capacity(ms_kind_t k)
{
    return k_slot[k].size - 4;
}

const uint8_t *ms_map(ms_kind_t k, size_t *len, esp_partition_mmap_handle_t *h)
{
    if (s_part == NULL) return NULL;
    uint32_t n;
    if (esp_partition_read(s_part, k_slot[k].off, &n, sizeof n) != ESP_OK) return NULL;
    if (n == 0xFFFFFFFFu || n == 0 || n > ms_capacity(k)) return NULL;
    const void *p;
    /* mmap works in 64 kB pages: map the whole slot, the record is inside. */
    if (esp_partition_mmap(s_part, k_slot[k].off, k_slot[k].size, ESP_PARTITION_MMAP_DATA,
                           &p, h) != ESP_OK) {
        return NULL;
    }
    *len = n;
    return (const uint8_t *)p + 4;
}

void ms_unmap(esp_partition_mmap_handle_t h)
{
    esp_partition_munmap(h);
}

esp_err_t ms_erase(ms_kind_t k)
{
    if (s_part == NULL) return ESP_ERR_NOT_FOUND;
    return esp_partition_erase_range(s_part, k_slot[k].off, k_slot[k].size);
}

esp_err_t ms_write(ms_kind_t k, size_t off, const uint8_t *buf, size_t n)
{
    if (s_part == NULL) return ESP_ERR_NOT_FOUND;
    if (off + n > ms_capacity(k)) return ESP_ERR_INVALID_SIZE;
    return esp_partition_write(s_part, k_slot[k].off + 4 + off, buf, n);
}

esp_err_t ms_commit(ms_kind_t k, size_t len)
{
    if (s_part == NULL) return ESP_ERR_NOT_FOUND;
    uint32_t n = (uint32_t)len;
    return esp_partition_write(s_part, k_slot[k].off, &n, sizeof n);
}
