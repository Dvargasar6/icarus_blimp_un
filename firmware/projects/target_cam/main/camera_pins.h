/*
 * ESP32-CAM pin maps.
 *
 * AI-Thinker is the board nearly everyone means by "ESP32-CAM": the black
 * module with the microSD slot and the 1 W white LED. The other maps are here
 * so a different board needs a one-line change, not an edit of the main file.
 */
#pragma once

#if defined(CONFIG_TARGETCAM_BOARD_AI_THINKER)

#define CAM_PIN_PWDN    32
#define CAM_PIN_RESET   -1      /* not broken out; the driver soft-resets */
#define CAM_PIN_XCLK     0
#define CAM_PIN_SIOD    26
#define CAM_PIN_SIOC    27
#define CAM_PIN_D7      35
#define CAM_PIN_D6      34
#define CAM_PIN_D5      39
#define CAM_PIN_D4      36
#define CAM_PIN_D3      21
#define CAM_PIN_D2      19
#define CAM_PIN_D1      18
#define CAM_PIN_D0       5
#define CAM_PIN_VSYNC   25
#define CAM_PIN_HREF    23
#define CAM_PIN_PCLK    22

/* The 1 W flash LED. It is a serious heat source and we never want it on, but
   GPIO4 also floats bright on some boards, so we drive it low explicitly. */
#define CAM_PIN_FLASH_LED 4

#elif defined(CONFIG_TARGETCAM_BOARD_ESP_EYE)

#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK     4
#define CAM_PIN_SIOD    18
#define CAM_PIN_SIOC    23
#define CAM_PIN_D7      36
#define CAM_PIN_D6      37
#define CAM_PIN_D5      38
#define CAM_PIN_D4      39
#define CAM_PIN_D3      35
#define CAM_PIN_D2      14
#define CAM_PIN_D1      13
#define CAM_PIN_D0      34
#define CAM_PIN_VSYNC    5
#define CAM_PIN_HREF    27
#define CAM_PIN_PCLK    25
#define CAM_PIN_FLASH_LED -1

#elif defined(CONFIG_TARGETCAM_BOARD_XIAO_S3)

#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    10
#define CAM_PIN_SIOD    40
#define CAM_PIN_SIOC    39
#define CAM_PIN_D7      48
#define CAM_PIN_D6      11
#define CAM_PIN_D5      12
#define CAM_PIN_D4      14
#define CAM_PIN_D3      16
#define CAM_PIN_D2      18
#define CAM_PIN_D1      17
#define CAM_PIN_D0      15
#define CAM_PIN_VSYNC   38
#define CAM_PIN_HREF    47
#define CAM_PIN_PCLK    13
#define CAM_PIN_FLASH_LED -1

#else
#error "Select a camera board in menuconfig (Target Camera Configuration)"
#endif
