/*
 * model_store - model records uploaded over the serial link (Phase D), kept in
 * their own flash partition.
 *
 * Retraining at the venue should not need a rebuild and a reflash with the
 * BOOT button held: '!upload' writes a model record here, and from then on it
 * is used instead of the one compiled in. The record is the same bytes as
 * main/models/<x>_model.bin and goes through the same loader (magic, version,
 * size, CRC-32, values, and the colour table it belongs to), so an upload adds
 * transport and nothing else, as tc_pipeline.h's record format intended.
 *
 * Why a partition of its own and not NVS: the settings NVS partition is
 * 24 kB, the colour table record alone is 8 kB, and NVS needs room for the
 * new copy of a blob before it drops the old one. A 64 kB raw data partition
 * ("tcmodels", subtype 0x40, after the app; partitions.csv) holds one slot
 * per model:
 *
 *   slot   offset   size     holds
 *   lut    0x0000   16 kB    u32 length, then the record (8 212 B)
 *   clf    0x4000    4 kB    u32 length, then the record (180 B)
 *   cnn    0x5000   16 kB    u32 length, then the record (up to ~5.4 kB)
 *
 * An erased slot reads length 0xFFFFFFFF: nothing uploaded. An upload erases
 * the slot, writes the record as it arrives, and writes the length last, so a
 * transfer that stops halfway leaves a slot that reads as empty. Records are
 * read through a temporary flash mapping at load time and copied out by the
 * loaders, so nothing stays mapped and a later write cannot leave a stale
 * mapping behind.
 *
 * A firmware built with the old partition table has no "tcmodels"
 * partition: ms_init() says so, uploads are refused with the reason, and the
 * compiled-in models are used as before.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_partition.h"

typedef enum { MS_LUT = 0, MS_CLF, MS_CNN, MS_KINDS } ms_kind_t;

/* Find the partition. false if this build's partition table has none. */
bool ms_init(void);
bool ms_available(void);

/* "lut", "clf", "cnn"; and back (-1 for anything else). */
const char *ms_name(ms_kind_t k);
int ms_kind(const char *name);

/* Largest record a slot holds. */
size_t ms_capacity(ms_kind_t k);

/* The uploaded record of kind k, mapped read-only, or NULL if the slot is
   empty (or holds a length that cannot be right). ms_unmap() when done. */
const uint8_t *ms_map(ms_kind_t k, size_t *len, esp_partition_mmap_handle_t *h);
void ms_unmap(esp_partition_mmap_handle_t h);

/* Erase a slot: the model of that kind is the compiled-in one again. */
esp_err_t ms_erase(ms_kind_t k);

/* Write part of a record into an erased slot (off counts from the record's
   first byte), and then its length, which makes it visible. */
esp_err_t ms_write(ms_kind_t k, size_t off, const uint8_t *buf, size_t n);
esp_err_t ms_commit(ms_kind_t k, size_t len);
