#include "board.h"

uint32_t board_reset_flags;

static void put_char(char character)
{
    while ((USART_SR(USART2_BASE) & USART_SR_TXE) == 0u) {
    }
    USART_DR(USART2_BASE) = (uint8_t)character;
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

void line_begin(const char *kind)
{
    put_text(TAG " ");
    put_text(kind);
}

static void field_key(const char *key)
{
    put_char(' ');
    put_text(key);
    put_char('=');
}

void field_u(const char *key, uint32_t value)
{
    field_key(key);
    put_decimal(value);
}

void field_i(const char *key, int32_t value)
{
    field_key(key);
    if (value < 0) {
        put_char('-');
        put_decimal(0u - (uint32_t)value);
    } else {
        put_decimal((uint32_t)value);
    }
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

void console_flush(void)
{
    while ((USART_SR(USART2_BASE) & USART_SR_TC) == 0u) {
    }
}

void delay_cycles(uint32_t count)
{
    uint32_t start = cycles();
    while (cycles() - start < count) {
    }
}

void delay_us(uint32_t microseconds)
{
    delay_cycles(microseconds * (SYSCLK_HZ / 1000000u));
}

void gpio_configure(uint32_t port, uint32_t pin, uint32_t mode, uint32_t pull, uint32_t alternate)
{
    volatile uint32_t *afr = pin < 8u ? &GPIO_AFRL(port) : &GPIO_AFRH(port);
    uint32_t nibble = (pin % 8u) * 4u;
    uint32_t pair = pin * 2u;
    *afr = (*afr & ~(0xFu << nibble)) | ((alternate & 0xFu) << nibble);
    GPIO_PUPDR(port) = (GPIO_PUPDR(port) & ~(3u << pair)) | ((pull & 3u) << pair);
    GPIO_MODER(port) = (GPIO_MODER(port) & ~(3u << pair)) | ((mode & 3u) << pair);
}

void gpio_set_pull(uint32_t port, uint32_t pin, uint32_t pull)
{
    uint32_t pair = pin * 2u;
    GPIO_PUPDR(port) = (GPIO_PUPDR(port) & ~(3u << pair)) | ((pull & 3u) << pair);
}

void gpio_write(uint32_t port, uint32_t pin, bool high)
{
    GPIO_BSRR(port) = high ? (1u << pin) : (1u << (pin + 16u));
}

void board_boot(const char *image)
{
    board_reset_flags = RCC_CSR;
    RCC_CSR |= RCC_CSR_RMVF;

    DEMCR |= 1u << 24; /* TRCENA */
    DWT_LAR = 0xC5ACCE55u;
    DWT_CYCCNT = 0u;
    DWT_CTRL |= 1u; /* CYCCNTENA */

    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN;
    (void)RCC_APB1ENR;
    USART_BRR(USART2_BASE) = USART_BRR_115200_AT_16MHZ;
    USART_CR1(USART2_BASE) = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
    /* The transmitter already holds the line idle when the pin is handed to it. */
    gpio_configure(GPIOA_BASE, 2u, GPIO_MODE_AF, GPIO_PULL_UP, 7u);
    gpio_configure(GPIOA_BASE, 3u, GPIO_MODE_AF, GPIO_PULL_UP, 7u);
    delay_us(200u);

    /* Ends whatever partial line the reset left on the host's side. */
    put_text("\r\n");
    line_begin("boot");
    field_s("image", image);
    field_x("rcc_csr", board_reset_flags);
    field_x("cfgr", RCC_CFGR);
    field_u("sysclk_hz", SYSCLK_HZ);
    line_end();
}

void finish(void)
{
    line_begin("done");
    line_end();
    console_flush();
    /* No WFI: the debugger has to reach the core for the next flash. */
    for (;;) {
    }
}

/* Called by fault_handler with the exception stack frame. */
__attribute__((used, noreturn)) void fault_report(const uint32_t *frame)
{
    line_begin("fault");
    field_x("cfsr", SCB_CFSR);
    field_x("hfsr", SCB_HFSR);
    field_x("mmfar", SCB_MMFAR);
    field_x("bfar", SCB_BFAR);
    field_x("pc", frame[6]);
    field_x("lr", frame[5]);
    field_x("xpsr", frame[7]);
    line_end();
    console_flush();
    for (;;) {
    }
}

__attribute__((naked)) void fault_handler(void)
{
    __asm volatile(
        "tst lr, #4\n"
        "ite eq\n"
        "mrseq r0, msp\n"
        "mrsne r0, psp\n"
        "b fault_report\n");
}
