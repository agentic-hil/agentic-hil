/*
 * The image's only output: lines `HQ177 <kind> key=value ...` on USART2
 * (PA2/PA3, 115200 8N1), the bench's ST-LINK virtual COM port. Every write
 * blocks until the transmitter has taken the byte, so no line may be printed
 * while something is being measured.
 */
#ifndef HQ177_CONSOLE_H
#define HQ177_CONSOLE_H

#include <stdint.h>

/** Bring USART2 up and end whatever partial line the reset left. */
void console_init(void);

/** Wait until the last byte has left the transmitter. */
void console_flush(void);

void line_begin(const char *kind);
void field_u(const char *key, uint32_t value);
void field_x(const char *key, uint32_t value);
void field_s(const char *key, const char *value);
void line_end(void);

#endif
