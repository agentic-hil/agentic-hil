/*
 * A board the watchdog keeps restarting.
 *
 * Every boot prints one line on USART2: how many boots the board has counted,
 * and what the reset controller says started this one.
 *
 *     watchdog boot 7 cause iwdg
 *
 * Then the independent watchdog is started with a timeout of about a quarter
 * of a second and never fed, so the board restarts itself for as long as it is
 * left to run. The count lives in a small record in RAM that neither the
 * startup code nor this image's stack reaches, checked by a magic word and the
 * count's complement: a restart keeps it and counts one up, and anything that
 * loses the RAM starts it again from zero.
 *
 * A system reset stops a watchdog that software started, so a core the
 * debugger resets into halt stays silent, and the boot after a reset from the
 * debugger names that reset rather than the watchdog.
 *
 * Built by the bench tier as the demo's main.c, with the demo's startup code,
 * linker script and toolchain file, and put on the board through the product.
 */

#include <stdint.h>

#define RCC_AHB1ENR (*(volatile uint32_t *)0x40023830U)
#define RCC_APB1ENR (*(volatile uint32_t *)0x40023840U)
#define RCC_CSR (*(volatile uint32_t *)0x40023874U)
#define GPIOA_MODER (*(volatile uint32_t *)0x40020000U)
#define GPIOA_AFRL (*(volatile uint32_t *)0x40020020U)
#define USART2_SR (*(volatile uint32_t *)0x40004400U)
#define USART2_DR (*(volatile uint32_t *)0x40004404U)
#define USART2_BRR (*(volatile uint32_t *)0x40004408U)
#define USART2_CR1 (*(volatile uint32_t *)0x4000440CU)
#define USART2_CR2 (*(volatile uint32_t *)0x40004410U)
#define USART2_CR3 (*(volatile uint32_t *)0x40004414U)
#define IWDG_KR (*(volatile uint32_t *)0x40003000U)
#define IWDG_PR (*(volatile uint32_t *)0x40003004U)
#define IWDG_RLR (*(volatile uint32_t *)0x40003008U)
#define IWDG_SR (*(volatile uint32_t *)0x4000300CU)

#define GPIOAEN (1U << 0)
#define USART2EN (1U << 17)
#define USART2_TX_PIN 2U
#define USART2_RX_PIN 3U
#define GPIO_MODER_AF(pin) (2U << ((pin) * 2U))
#define GPIO_MODER_MASK(pin) (3U << ((pin) * 2U))
#define GPIO_AFRL_AF7(pin) (7U << ((pin) * 4U))
#define GPIO_AFRL_MASK(pin) (0xFU << ((pin) * 4U))
#define USART_SR_TC (1U << 6)
#define USART_SR_TXE (1U << 7)
#define USART_CR1_RE (1U << 2)
#define USART_CR1_TE (1U << 3)
#define USART_CR1_UE (1U << 13)
#define USART2_BRR_115200_PCLK16MHZ 139U

#define RCC_CSR_RMVF (1U << 24)
#define RCC_CSR_BORRSTF (1U << 25)
#define RCC_CSR_PINRSTF (1U << 26)
#define RCC_CSR_PORRSTF (1U << 27)
#define RCC_CSR_SFTRSTF (1U << 28)
#define RCC_CSR_IWDGRSTF (1U << 29)

#define IWDG_KEY_START 0xCCCCU
#define IWDG_KEY_ACCESS 0x5555U
#define IWDG_KEY_RELOAD 0xAAAAU
/* The LSI runs at about 32 kHz, so a divider of 32 makes a count about a
 * millisecond, and 250 counts about a quarter of a second. */
#define IWDG_PRESCALER_DIV32 3U
#define IWDG_RELOAD_COUNTS 250U

struct boot_record {
    uint32_t magic;
    uint32_t count;
    uint32_t check;
};

/* Above everything the startup code copies or clears, below the stack, and
 * outside the RAM the debugger borrows for its flash loader. */
#define BOOT_RECORD (*(volatile struct boot_record *)0x20010000U)
#define BOOT_RECORD_MAGIC 0x57444F47U

void SystemInit(void)
{
    /* Keep the reset clock defaults: HSI at 16 MHz, as the demo does. */
}

static uint32_t next_boot_count(void)
{
    uint32_t count = 0U;

    if (BOOT_RECORD.magic == BOOT_RECORD_MAGIC && BOOT_RECORD.check == ~BOOT_RECORD.count) {
        count = BOOT_RECORD.count + 1U;
    }
    BOOT_RECORD.magic = BOOT_RECORD_MAGIC;
    BOOT_RECORD.count = count;
    BOOT_RECORD.check = ~count;
    return count;
}

static const char *boot_cause(void)
{
    uint32_t flags = RCC_CSR;
    const char *cause = "unknown";

    /* Every reset also pulls NRST, so the pin flag comes last. */
    if ((flags & RCC_CSR_IWDGRSTF) != 0U) {
        cause = "iwdg";
    } else if ((flags & RCC_CSR_SFTRSTF) != 0U) {
        cause = "software";
    } else if ((flags & (RCC_CSR_PORRSTF | RCC_CSR_BORRSTF)) != 0U) {
        cause = "power";
    } else if ((flags & RCC_CSR_PINRSTF) != 0U) {
        cause = "pin";
    }
    /* Cleared, so the next boot reads only the reset that started it. */
    RCC_CSR |= RCC_CSR_RMVF;
    return cause;
}

static void usart2_init(void)
{
    RCC_AHB1ENR |= GPIOAEN;
    RCC_APB1ENR |= USART2EN;

    GPIOA_MODER = (GPIOA_MODER & ~(GPIO_MODER_MASK(USART2_TX_PIN) |
                                    GPIO_MODER_MASK(USART2_RX_PIN))) |
                   GPIO_MODER_AF(USART2_TX_PIN) |
                   GPIO_MODER_AF(USART2_RX_PIN);
    GPIOA_AFRL = (GPIOA_AFRL & ~(GPIO_AFRL_MASK(USART2_TX_PIN) |
                                  GPIO_AFRL_MASK(USART2_RX_PIN))) |
                 GPIO_AFRL_AF7(USART2_TX_PIN) |
                 GPIO_AFRL_AF7(USART2_RX_PIN);

    USART2_CR1 = 0U;
    USART2_CR2 = 0U;
    USART2_CR3 = 0U;
    USART2_BRR = USART2_BRR_115200_PCLK16MHZ;
    USART2_CR1 = USART_CR1_RE | USART_CR1_TE | USART_CR1_UE;
}

static void usart2_put(char byte)
{
    while ((USART2_SR & USART_SR_TXE) == 0U) {
    }
    USART2_DR = (uint32_t)(unsigned char)byte;
}

static void usart2_send(const char *text)
{
    while (*text != '\0') {
        usart2_put(*text);
        ++text;
    }
}

static void usart2_send_decimal(uint32_t value)
{
    char digits[10];
    uint32_t length = 0U;

    do {
        digits[length] = (char)('0' + (value % 10U));
        value /= 10U;
        ++length;
    } while (value != 0U);
    while (length > 0U) {
        --length;
        usart2_put(digits[length]);
    }
}

int main(void)
{
    uint32_t count = next_boot_count();
    const char *cause = boot_cause();

    usart2_init();
    usart2_send("watchdog boot ");
    usart2_send_decimal(count);
    usart2_send(" cause ");
    usart2_send(cause);
    usart2_send("\n");
    /* The whole line on the wire before the watchdog is started. */
    while ((USART2_SR & USART_SR_TC) == 0U) {
    }

    IWDG_KR = IWDG_KEY_START;
    IWDG_KR = IWDG_KEY_ACCESS;
    IWDG_PR = IWDG_PRESCALER_DIV32;
    IWDG_RLR = IWDG_RELOAD_COUNTS;
    while (IWDG_SR != 0U) {
    }
    IWDG_KR = IWDG_KEY_RELOAD;

    /* Never fed again. */
    for (;;) {
    }
}
