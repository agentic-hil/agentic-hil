/* NUCLEO-H533RE images for measuring a first flash with connect_mode: under_reset (#752).
 *
 * One source, three images, chosen by PREVIOUS at compile time:
 *   0  next       the image a measured flash writes: no interrupt, no watchdog
 *   1  previous   runs out of flash under a 1 kHz SysTick interrupt whose vector and handler are in flash
 *   2  previous   runs out of flash with the independent watchdog started by software, about 100 ms
 * Build one with the GNU Arm toolchain of STM32CubeCLT 1.22.0, in this directory:
 *   arm-none-eabi-gcc -mcpu=cortex-m33 -mthumb -mfloat-abi=soft -O1 -g0 -std=gnu11 -Wall -Wextra -Werror \
 *     -ffreestanding -nostdlib -nostartfiles -Wl,--build-id=none -Wl,--fatal-warnings \
 *     -DPREVIOUS=0 -T h533re_under_reset.ld h533re_under_reset.c -o h533re_under_reset_next.elf
 * The other two are -DPREVIOUS=1 (h533re_under_reset_previous_systick.elf) and -DPREVIOUS=2
 * (h533re_under_reset_previous_iwdg.elf). The recordings name the SHA-256 of each.
 *
 * Each image prints the reset flags of its own boot on USART2 (PA2, the on-board ST-LINK's virtual COM
 * port, 115200 8N1) and then clears them, so the flags the next boot prints are those of the resets
 * since. RM0481 table 118: a reset that came in through the NRST pin alone sets PINRSTF and no other
 * flag, and every reset the part makes itself sets PINRSTF beside its own flag.
 *
 * The way back is part of the image. The clock tree, the power mode, the debug pins and the option bytes
 * stay as they are out of reset, so the debug port answers whatever the image does. The watchdog is
 * started by software only, three seconds after a boot that no watchdog reset began, so a reset always
 * ends it and a boot it caused leaves it off. A failed flash leaves that flag set until an image boots and
 * clears it, so flash the next image and reset once before the next attempt, or the watchdog image leaves
 * its watchdog off. A flash that stops after its erase leaves no code to run.
 */
#include <stdint.h>

#ifndef PREVIOUS
#error "PREVIOUS selects the image: 0, 1 or 2"
#endif

#define REGISTER(address) (*(volatile uint32_t *)(address))
#define RCC_AHB2ENR REGISTER(0x44020C8CU)
#define RCC_APB1LENR REGISTER(0x44020C9CU)
#define RCC_RSR REGISTER(0x44020CF4U)
#define GPIOA_MODER REGISTER(0x42020000U)
#define GPIOA_AFRL REGISTER(0x42020020U)
#define USART2_CR1 REGISTER(0x40004400U)
#define USART2_BRR REGISTER(0x4000440CU)
#define USART2_ISR REGISTER(0x4000441CU)
#define USART2_TDR REGISTER(0x40004428U)
#define IWDG_KR REGISTER(0x40003000U)
#define IWDG_PR REGISTER(0x40003004U)
#define IWDG_RLR REGISTER(0x40003008U)
#define IWDG_SR REGISTER(0x4000300CU)
#define SYST_CSR REGISTER(0xE000E010U)
#define SYST_RVR REGISTER(0xE000E014U)
#define SYST_CVR REGISTER(0xE000E018U)

#define GPIOAEN (1U << 0)
#define USART2EN (1U << 17)
#define RMVF (1U << 23)
#define IWDGRSTF (1U << 29)
#define USART_UE (1U << 0)
#define USART_TE (1U << 3)
#define USART_TC (1U << 6)
#define USART_TXE (1U << 7)
#define BRR_115200_32MHZ 278U
#define TICKS_PER_MS 32000U
#define SYST_ENABLE (1U << 0)
#define SYST_TICKINT (1U << 1)
#define SYST_CLKSOURCE (1U << 2)
#define SYST_COUNTFLAG (1U << 16)
#define BEAT_MS 500U
#define ARM_BEAT 6U
#define PAYLOAD_WORDS 7424U

/* A live register by name, for a read that resets nothing. */
__asm__(
    ".global rcc_rsr\n"
    ".type rcc_rsr, %object\n"
    ".set rcc_rsr, 0x44020CF4\n"
    ".size rcc_rsr, 4\n"
);

extern uint32_t _estack;
void Reset_Handler(void);
void SysTick_Handler(void);

static void stop(void)
{
    for (;;) {
    }
}

__attribute__((section(".vectors"), used))
static void (*const vectors[16])(void) = {
    (void (*)(void))&_estack, Reset_Handler, stop, stop, stop, stop, stop, stop,
    0, 0, 0, stop, stop, 0, stop, SysTick_Handler,
};

/* 29 KiB the core keeps reading, so every image covers sectors 0 to 3 and its download outlasts the watchdog. */
__attribute__((section(".rodata.payload"), used))
static const volatile uint32_t payload[PAYLOAD_WORDS] = {[0 ... PAYLOAD_WORDS - 1U] = 0x5A0FF1A5U + PREVIOUS};

volatile uint32_t boot_rsr;
static volatile uint32_t milliseconds;
static uint32_t watchdog_on;

void SysTick_Handler(void)
{
    ++milliseconds;
}

static void send_text(const char *text)
{
    while (*text != '\0') {
        while ((USART2_ISR & USART_TXE) == 0U) {
        }
        USART2_TDR = (uint32_t)(unsigned char)*text;
        ++text;
    }
    while ((USART2_ISR & USART_TC) == 0U) {
    }
}

static void send_hex(uint32_t value)
{
    char text[9];
    for (int i = 7; i >= 0; --i) {
        uint32_t digit = value & 0xFU;
        text[i] = (char)(digit < 10U ? '0' + digit : 'a' + digit - 10U);
        value >>= 4;
    }
    text[8] = '\0';
    send_text(text);
}

static void feed_watchdog(void)
{
    if (watchdog_on != 0U) {
        IWDG_KR = 0xAAAAU;
    }
}

static void start_watchdog(void)
{
    IWDG_KR = 0xCCCCU;
    IWDG_KR = 0x5555U;
    IWDG_PR = 0U;
    IWDG_RLR = 800U;
    while (IWDG_SR & 0x7U) {
    }
    IWDG_KR = 0xAAAAU;
    watchdog_on = 1U;
}

static void wait_one_millisecond(void)
{
#if PREVIOUS == 1
    uint32_t now = milliseconds;
    while (milliseconds == now) {
    }
#else
    while ((SYST_CSR & SYST_COUNTFLAG) == 0U) {
    }
#endif
    feed_watchdog();
}

static void start_console(void)
{
    RCC_AHB2ENR |= GPIOAEN;
    RCC_APB1LENR |= USART2EN;
    (void)RCC_APB1LENR;
    GPIOA_AFRL = (GPIOA_AFRL & ~(0xFU << 8)) | (7U << 8);
    GPIOA_MODER = (GPIOA_MODER & ~(3U << 4)) | (2U << 4);
    USART2_BRR = BRR_115200_32MHZ;
    USART2_CR1 = USART_TE | USART_UE;
}

static void start_ticks(void)
{
    SYST_RVR = TICKS_PER_MS - 1U;
    SYST_CVR = 0U;
#if PREVIOUS == 1
    SYST_CSR = SYST_CLKSOURCE | SYST_TICKINT | SYST_ENABLE;
#else
    SYST_CSR = SYST_CLKSOURCE | SYST_ENABLE;
#endif
}

int main(void)
{
    boot_rsr = RCC_RSR;
    RCC_RSR |= RMVF;
    RCC_RSR &= ~RMVF;
    start_console();
    start_ticks();

    for (uint32_t beat = 0U;; ++beat) {
        uint32_t sum = 0U;
        for (uint32_t ms = 0U; ms < BEAT_MS; ++ms) {
            wait_one_millisecond();
            for (uint32_t i = 0U; i < PAYLOAD_WORDS; i += 9U) {
                sum += payload[i];
            }
        }
        if (PREVIOUS == 2 && beat == ARM_BEAT && (boot_rsr & IWDGRSTF) == 0U) {
            start_watchdog();
        }
        send_text("ahil752 image=");
        send_text(PREVIOUS == 0 ? "next" : PREVIOUS == 1 ? "previous-systick" : "previous-iwdg");
        send_text(" boot_rsr=");
        send_hex(boot_rsr);
        send_text(" live_rsr=");
        send_hex(RCC_RSR);
        send_text(" watchdog=");
        send_text(watchdog_on != 0U ? "on" : "off");
        send_text(" sum=");
        send_hex(sum);
        send_text(" beat=");
        send_hex(beat);
        send_text("\r\n");
        feed_watchdog();
    }
}

extern uint32_t _sbss, _ebss;

__attribute__((noreturn))
void Reset_Handler(void)
{
    for (volatile uint32_t *word = &_sbss; word < &_ebss; ++word) {
        *word = 0U;
    }
    (void)main();
    stop();
    __builtin_unreachable();
}