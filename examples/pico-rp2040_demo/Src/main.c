#include <stdint.h>

#define REG32(address) (*(volatile uint32_t *)(address))

#define XOSC_CTRL         REG32(0x40024000u)
#define XOSC_STATUS       REG32(0x40024004u)
#define XOSC_STARTUP      REG32(0x4002400cu)
#define CLK_REF_CTRL      REG32(0x40008030u)
#define CLK_REF_SELECTED  REG32(0x40008038u)
#define CLK_SYS_CTRL      REG32(0x4000803cu)
#define CLK_SYS_SELECTED  REG32(0x40008044u)
#define CLK_PERI_CTRL     REG32(0x40008048u)
#define RESETS_RESET_CLR  REG32(0x4000f000u)
#define RESETS_RESET_DONE REG32(0x4000c008u)
#define GPIO0_CTRL        REG32(0x40014004u)
#define GPIO1_CTRL        REG32(0x4001400cu)
#define GPIO1_PAD         REG32(0x4001c008u)
#define UART0_DR          REG32(0x40034000u)
#define UART0_FR          REG32(0x40034018u)
#define UART0_IBRD        REG32(0x40034024u)
#define UART0_FBRD        REG32(0x40034028u)
#define UART0_LCR_H       REG32(0x4003402cu)
#define UART0_CR          REG32(0x40034030u)

static void clock_init(void)
{
    XOSC_STARTUP = 47;                   // About 1 ms in units of 256 crystal cycles.
    XOSC_CTRL = (0xfabu << 12) | 0xaa0u;  // Enable the 1 to 15 MHz crystal range.
    while ((XOSC_STATUS & (1u << 31)) == 0) {
    }

    CLK_REF_CTRL = 2;                    // Select XOSC.
    while ((CLK_REF_SELECTED & (1u << 2)) == 0) {
    }
    CLK_SYS_CTRL = 0;                    // Select clk_ref.
    while ((CLK_SYS_SELECTED & 1u) == 0) {
    }
    CLK_PERI_CTRL = 1u << 11;           // Enable clk_peri from clk_sys.
}

static void uart0_init(void)
{
    const uint32_t reset_mask = (1u << 5) | (1u << 8) | (1u << 22); // I/O, pads, UART0.
    RESETS_RESET_CLR = reset_mask;
    while ((RESETS_RESET_DONE & reset_mask) != reset_mask) {
    }

    UART0_CR = 0;
    UART0_IBRD = 6;                     // 12 MHz / (16 * 115200), integer part.
    UART0_FBRD = 33;                    // Fractional part in units of 1/64.
    UART0_LCR_H = 3u << 5;              // Eight data bits, no parity, one stop bit.
    UART0_CR = (1u << 9) | (1u << 8) | 1u; // RXE, TXE, UARTEN.

    GPIO1_PAD = (GPIO1_PAD & ~(1u << 2)) | (1u << 3); // Pull RX high when undriven.
    GPIO0_CTRL = 2;                     // UART0 TX.
    GPIO1_CTRL = 2;                     // UART0 RX.
}

static void uart_puts(const char *line)
{
    while (*line != '\0') {
        while ((UART0_FR & (1u << 5)) != 0) { // Wait while TX is full.
        }
        UART0_DR = (uint8_t)*line++;
    }
}

void reset_handler(void)
{
    clock_init();
    uart0_init();
    uart_puts("Hello World\n");

    for (;;) {
        __asm__ volatile ("wfi");
    }
}
