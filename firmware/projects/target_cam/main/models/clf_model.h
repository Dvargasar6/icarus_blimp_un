/*
 * No blob classifier is built in: stage 2 is unavailable, and a request for
 * it runs stage 1 (#!CFG clf=none).
 *
 * host/train/train_clf.py replaces this file with a trained model record
 * (and tcview's Firmware tab puts this placeholder back when it installs a
 * colour table that has no classifier). The file always exists, rather than
 * being included only when present, so the build system sees it change: a
 * header that appears where none was before would not make target_main.c
 * rebuild, and the firmware would quietly keep its old stage 2.
 */
#pragma once

#include <stdint.h>

#define CLF_MODEL_NONE 1

static const uint8_t clf_model_rec[1] = { 0 };
