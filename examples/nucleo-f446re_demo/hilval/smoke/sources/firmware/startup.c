#include <stddef.h>
#include <stdint.h>

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;

int main(void);
void reset_handler(void);
void fault_handler(void);
static void unexpected_exception(void);

/* The core exceptions only: no image enables a peripheral interrupt. */
__attribute__((section(".isr_vector"), used)) static void (*const vector_table[16])(void) = {
    (void (*)(void))(&_estack),
    reset_handler,
    fault_handler, /* NMI */
    fault_handler, /* HardFault */
    fault_handler, /* MemManage */
    fault_handler, /* BusFault */
    fault_handler, /* UsageFault */
    0,
    0,
    0,
    0,
    unexpected_exception, /* SVCall */
    unexpected_exception, /* DebugMon */
    0,
    unexpected_exception, /* PendSV */
    unexpected_exception, /* SysTick */
};

void reset_handler(void)
{
    const uint32_t *source = &_sidata;
    for (uint32_t *destination = &_sdata; destination < &_edata;) {
        *destination++ = *source++;
    }
    for (uint32_t *destination = &_sbss; destination < &_ebss;) {
        *destination++ = 0u;
    }
    (void)main();
    for (;;) {
    }
}

static void unexpected_exception(void)
{
    for (;;) {
    }
}

/* GCC may call these for struct copies and initialisers even without a C library. */
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
