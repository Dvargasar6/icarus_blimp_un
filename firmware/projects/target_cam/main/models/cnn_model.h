/*
 * No crop CNN is built in: stage 3 is unavailable, and a request for it runs
 * stage 2 or lower (#!CFG cnn=none).
 *
 * host/train/train_cnn.py writes a trained model record in its place (and
 * tcview's Firmware tab puts this placeholder back when it installs a set
 * that has no CNN). The file always exists, so the build system sees it
 * change, as clf_model.h does.
 */
#pragma once

#include <stdint.h>

#define CNN_MODEL_NONE 1

static const uint8_t cnn_model_rec[1] = { 0 };
