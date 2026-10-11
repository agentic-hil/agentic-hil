/*
 * What the image does when it gets something wrong, and the two functions the
 * compiler may call on its own.
 *
 * libopencm3's vector table points every fault at a weak `blocking_handler`
 * that spins silently; a strong definition here takes its place, so a fault
 * prints one `HQ177 fault ...` line instead of looking like a hang. The gate
 * fails any run that has such a line.
 */
#include <stddef.h>
#include <stdint.h>

#include <libopencm3/cm3/scb.h>

#include "console.h"

void nmi_handler(void);
void hard_fault_handler(void);
void mem_manage_handler(void);
void bus_fault_handler(void);
void usage_fault_handler(void);

static void report(const char *kind)
{
	line_begin("fault");
	field_s("kind", kind);
	field_x("cfsr", SCB_CFSR);
	field_x("hfsr", SCB_HFSR);
	field_x("bfar", SCB_BFAR);
	line_end();
	console_flush();
	for (;;) {
	}
}

void nmi_handler(void)
{
	report("nmi");
}

void hard_fault_handler(void)
{
	report("hard");
}

void mem_manage_handler(void)
{
	report("mem");
}

void bus_fault_handler(void)
{
	report("bus");
}

void usage_fault_handler(void)
{
	report("usage");
}

/* GCC may call these for struct copies and initialisers even without a C
 * library, and the image links with -nostdlib against libgcc alone. */
void *memcpy(void *destination, const void *source, size_t size)
{
	uint8_t *to = destination;
	const uint8_t *from = source;

	while (size-- > 0u) {
		*to++ = *from++;
	}
	return destination;
}

void *memset(void *destination, int value, size_t size)
{
	uint8_t *to = destination;

	while (size-- > 0u) {
		*to++ = (uint8_t)value;
	}
	return destination;
}
