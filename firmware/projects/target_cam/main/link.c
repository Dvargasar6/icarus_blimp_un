/*
 * link - see link.h for what and why.
 */
#include "link.h"

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "driver/uart.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"

#define LINK_UART       UART_NUM_0
#define LINK_REVERT_MS  3000

static int     s_baud = LINK_BOOT_BAUD;
static int64_t s_revert_at_us;          /* 0 = no revert pending */
static uint32_t s_seq;

static const int k_allowed[] = { 115200, 230400, 460800, 921600, 1500000, 2000000 };

/* Standard CRC-32 (zlib.crc32). The ROM routine wraps its argument and result
   in '~', so for init 0xFFFFFFFF and xorout 0xFFFFFFFF the call reduces to
   esp_rom_crc32_le(0, ...). Checked against the published check value at boot,
   because the PC rejects every frame if this is wrong. */
static uint32_t crc32(const uint8_t *buf, size_t len)
{
    return esp_rom_crc32_le(0, buf, (uint32_t)len);
}

void link_init(void)
{
    s_baud = LINK_BOOT_BAUD;
    uint32_t c = crc32((const uint8_t *)"123456789", 9);
    printf("# link         crc32 self-test %08lx (%s)\n", (unsigned long)c,
           c == 0xCBF43926u ? "ok" : "WRONG - the viewer will reject every frame");
    fflush(stdout);
}

int link_baud(void)
{
    return s_baud;
}

bool link_baud_allowed(int baud)
{
    for (size_t i = 0; i < sizeof k_allowed / sizeof k_allowed[0]; i++)
        if (k_allowed[i] == baud) return true;
    return false;
}

static void switch_to(int baud)
{
    fflush(stdout);
    uart_wait_tx_done(LINK_UART, pdMS_TO_TICKS(200));
    uart_set_baudrate(LINK_UART, (uint32_t)baud);
    /* Bytes that arrived mid-switch are garbage at either speed. */
    uart_flush_input(LINK_UART);
    s_baud = baud;
}

void link_set_baud(int baud)
{
    printf("#!BAUD %d\n", baud);
    switch_to(baud);
    s_revert_at_us = baud == LINK_BOOT_BAUD ? 0
                   : esp_timer_get_time() + (int64_t)LINK_REVERT_MS * 1000;
}

void link_note_command(void)
{
    s_revert_at_us = 0;
}

void link_poll(void)
{
    if (s_revert_at_us && esp_timer_get_time() >= s_revert_at_us) {
        int was = s_baud;
        s_revert_at_us = 0;
        switch_to(LINK_BOOT_BAUD);
        printf("#!BAUD %d\n", LINK_BOOT_BAUD);
        printf("# link         no command arrived at %d baud within %d ms; "
               "reverted to %d\n", was, LINK_REVERT_MS, LINK_BOOT_BAUD);
        fflush(stdout);
    }
}

void link_send_packet(const char *kind, const char *fields, const uint8_t *buf,
                      size_t len)
{
    uint32_t seq = s_seq++;
    printf("#!%s seq=%lu %s len=%u crc=%08lx\n", kind, (unsigned long)seq, fields,
           (unsigned)len, (unsigned long)crc32(buf, len));
    /* stdout must be empty before the raw bytes go out, or the header could
       land after them. The payload bypasses stdio because the console turns
       every 0x0A into 0x0D 0x0A, which would corrupt the pixels. */
    fflush(stdout);
    uart_write_bytes(LINK_UART, buf, len);
    printf("\n#!END seq=%lu\n", (unsigned long)seq);
    fflush(stdout);
}

void link_send_frame(const uint8_t *buf, size_t len, int w, int h,
                     bool big_endian, int64_t t_ms)
{
    char fields[96];
    snprintf(fields, sizeof fields, "t_ms=%lld w=%d h=%d fmt=%s", (long long)t_ms, w, h,
             big_endian ? "rgb565be" : "rgb565le");
    link_send_packet("FRAME", fields, buf, len);
}
