/*
 * USART2 at 115200 8N1 through libopencm3's USART API, with the field
 * formatters the image contract needs. The clock stays at the reset HSI,
 * 16 MHz, which libopencm3 assumes until told otherwise, so
 * usart_set_baudrate's default rcc_apb1_frequency is the right one.
 */
#include <stdbool.h>

#include <libopencm3/stm32/gpio.h>
#include <libopencm3/stm32/rcc.h>
#include <libopencm3/stm32/usart.h>

#include "console.h"

/* The tag every line starts with. Prefixed like the include guards, so no
 * library header can own the name (TIM7_PSC was that lesson once). */
#define HQ177_TAG "HQ177"

/* About 200 us at 16 MHz, long enough for the line to settle before the
 * first byte. Counted in nops because the timers are main's business. */
#define SETTLE_NOPS 3200u

static void put_char(char character)
{
	usart_send_blocking(USART2, (uint16_t)(uint8_t)character);
}

static void put_text(const char *text)
{
	while (*text != '\0') {
		put_char(*text++);
	}
}

static void put_decimal(uint32_t value)
{
	char digits[10];
	uint32_t count = 0u;

	do {
		digits[count++] = (char)('0' + value % 10u);
		value /= 10u;
	} while (value != 0u);
	while (count > 0u) {
		put_char(digits[--count]);
	}
}

static void put_hex(uint32_t value)
{
	static const char hex[] = "0123456789abcdef";
	bool started = false;

	put_text("0x");
	for (int shift = 28; shift >= 0; shift -= 4) {
		uint32_t nibble = (value >> shift) & 0xFu;

		if (nibble != 0u || started || shift == 0) {
			put_char(hex[nibble]);
			started = true;
		}
	}
}

static void field_key(const char *key)
{
	put_char(' ');
	put_text(key);
	put_char('=');
}

void console_init(void)
{
	rcc_periph_clock_enable(RCC_GPIOA);
	rcc_periph_clock_enable(RCC_USART2);
	usart_set_baudrate(USART2, 115200);
	usart_set_databits(USART2, 8);
	usart_set_stopbits(USART2, USART_STOPBITS_1);
	usart_set_parity(USART2, USART_PARITY_NONE);
	usart_set_flow_control(USART2, USART_FLOWCONTROL_NONE);
	usart_set_mode(USART2, USART_MODE_TX_RX);
	usart_enable(USART2);
	/* The transmitter already holds the line idle when the pin is handed
	 * to it, so the host sees no spurious start bit. */
	gpio_mode_setup(GPIOA, GPIO_MODE_AF, GPIO_PUPD_PULLUP, GPIO2 | GPIO3);
	gpio_set_af(GPIOA, GPIO_AF7, GPIO2 | GPIO3);
	for (uint32_t spin = 0u; spin < SETTLE_NOPS; spin++) {
		__asm__ volatile ("nop");
	}
	/* Ends whatever partial line the reset left on the host's side; an
	 * untagged line is ignored by the gate. */
	put_text("\r\n");
}

void console_flush(void)
{
	while (!usart_get_flag(USART2, USART_SR_TC)) {
	}
}

void line_begin(const char *kind)
{
	put_text(HQ177_TAG " ");
	put_text(kind);
}

void field_u(const char *key, uint32_t value)
{
	field_key(key);
	put_decimal(value);
}

void field_x(const char *key, uint32_t value)
{
	field_key(key);
	put_hex(value);
}

void field_s(const char *key, const char *value)
{
	field_key(key);
	put_text(value);
}

void line_end(void)
{
	put_text("\r\n");
}
