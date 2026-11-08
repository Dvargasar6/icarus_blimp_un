/*
 * link - raw camera frames, blob records and baud-rate changes on UART0, for
 * the PC viewer.
 *
 * The flight output of this firmware is the CSV stream at 115200 baud: that is
 * what the ESP32-S3 will read. Everything here is bench-only. It lets the PC
 * see the exact pixels the detector sees, which is the only way to calibrate
 * colour honestly, and is off unless the viewer asks for it.
 *
 * A frame on the wire:
 *
 *   #!FRAME seq=<n> t_ms=<ms> w=160 h=120 fmt=rgb565be len=38400 crc=<hex8>\r\n
 *   <len raw bytes, exactly as the sensor delivered them>
 *   \r\n#!END seq=<n>\r\n
 *
 * followed by the ordinary CSV row for the same capture (same t_ms), so the PC
 * can draw the camera's own detection on the frame. crc is standard CRC-32
 * (zlib.crc32). A log line from another task can land inside the payload; the
 * CRC exposes it and the PC drops that frame and resynchronises on the next
 * "#!FRAME" header.
 *
 * Blob records ('!collect 1', Phase B1) use the same framing with the tag
 * "#!BLOB" and a 1152-byte payload (a 24x24 RGB565 crop); see
 * link_send_packet() below and the target_cam README.
 *
 * Sending a frame blocks the main loop for len*10/baud seconds - 3.3 s at
 * 115200, 0.42 s at 921600 - so while frames are being sent, dt_ms and FPS do
 * not represent flight. The viewer says so.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define LINK_BOOT_BAUD      115200

/* Record the boot baud and print the CRC self-test. Call after the UART driver
   is installed. */
void link_init(void);

int  link_baud(void);

/* true if `baud` is one of the speeds the viewer may select. */
bool link_baud_allowed(int baud);

/* Announce "#!BAUD <baud>" at the old speed, drain the FIFO, switch. Unless a
   complete command line arrives within LINK_REVERT_MS at the new speed (see
   link_note_command), link_poll() switches back to LINK_BOOT_BAUD, so a speed
   the USB bridge cannot carry never strands the board. */
void link_set_baud(int baud);

/* A whole command line was received: the PC can talk to us at this speed. */
void link_note_command(void);

/* Call once per loop: performs the safety revert when it is due. */
void link_poll(void);

/* Send one frame packet (see above). */
void link_send_frame(const uint8_t *buf, size_t len, int w, int h,
                     bool big_endian, int64_t t_ms);

/* Any binary packet, framed as a frame is:
 *
 *   #!<kind> seq=<n> <fields> len=<len> crc=<hex8>\r\n <len bytes> \r\n#!END seq=<n>\r\n
 *
 * `fields` is the kind's own key=value text, no line break. Frames and blob
 * records ('!collect', "#!BLOB") share the sequence counter, so a #!END can
 * never close the wrong packet. */
void link_send_packet(const char *kind, const char *fields, const uint8_t *buf,
                      size_t len);
