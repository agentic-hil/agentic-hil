/*
 * basic: the bench path itself, and a watchdog reset with the port open.
 *
 * Prints the device's identity, a burst of 100 lines from a fixed pattern
 * (seed 0x12345678) that the test recomputes byte for byte, then arms the
 * independent watchdog and stops refreshing it. The reset that follows boots
 * this image again; it finds the breadcrumbs it left in no-init RAM and in an
 * RTC backup register, reports them, clears them and finishes. Only a
 * breadcrumb lost in the reset could make it arm again, and the plan's
 * timeout and the next flash end that.
 */
#include "board.h"

#define CRUMB_MAGIC 0x6876616Cu
#define BACKUP_MAGIC 0x736D6B31u
#define PATTERN_SEED 0x12345678u
#define BURST_LINES 100u
/* LSI (~32 kHz) / 4 = ~8 kHz; 800 counts are ~100 ms. */
#define WATCHDOG_RELOAD 800u

/* Neither copied nor cleared by the start-up code, so a reset leaves it as it was. */
__attribute__((section(".noinit"))) static volatile uint32_t crumb[3];

static void report_identity(void)
{
    uint32_t idcode = DBGMCU_IDCODE;
    line_begin("idcode");
    field_x("dev", idcode & 0xFFFu);
    field_x("rev", idcode >> 16);
    line_end();
    line_begin("flash");
    field_u("kb", FLASH_SIZE_KB);
    line_end();
}

static void burst(void)
{
    uint32_t value = PATTERN_SEED;
    uint32_t check = 0u;
    line_begin("burst-start");
    field_x("seed", PATTERN_SEED);
    field_u("lines", BURST_LINES);
    line_end();
    for (uint32_t index = 0u; index < BURST_LINES; index++) {
        value = value * 1664525u + 1013904223u;
        check ^= value;
        line_begin("burst");
        field_u("n", index);
        field_x("v", value);
        line_end();
    }
    line_begin("burst-end");
    field_x("xor", check);
    line_end();
}

static void backup_access(bool enable)
{
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    (void)RCC_APB1ENR;
    if (enable) {
        PWR_CR |= PWR_CR_DBP;
    } else {
        PWR_CR &= ~PWR_CR_DBP;
    }
}

__attribute__((noreturn)) static void arm_watchdog(void)
{
    crumb[0] = CRUMB_MAGIC;
    crumb[1] = 0u;
    crumb[2] = ~CRUMB_MAGIC;
    backup_access(true);
    RTC_BKP0R = BACKUP_MAGIC;
    uint32_t readback = RTC_BKP0R;

    line_begin("wdg");
    field_s("phase", "arm");
    field_u("prescaler", 4u);
    field_u("reload", WATCHDOG_RELOAD);
    field_x("bkp", readback);
    line_end();
    console_flush();

    IWDG_KR = 0xCCCCu; /* start; LSI starts with it */
    IWDG_KR = 0x5555u; /* unlock PR and RLR */
    IWDG_PR = 0u;      /* /4 */
    IWDG_RLR = WATCHDOG_RELOAD;
    uint32_t waited = cycles();
    while (IWDG_SR != 0u && cycles() - waited < SYSCLK_HZ) {
    }
    IWDG_KR = 0xAAAAu; /* reload with the new value */
    uint32_t armed = cycles();
    for (;;) {
        crumb[1] = cycles() - armed;
    }
}

static bool crumb_left(void)
{
    return crumb[0] == CRUMB_MAGIC && crumb[2] == ~CRUMB_MAGIC;
}

static bool backup_left(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    (void)RCC_APB1ENR;
    return RTC_BKP0R == BACKUP_MAGIC;
}

static void after_watchdog(void)
{
    bool crumb_ok = crumb_left();
    uint32_t elapsed = crumb[1];
    backup_access(true);
    uint32_t backup = RTC_BKP0R;
    RTC_BKP0R = 0u;
    backup_access(false);
    crumb[0] = 0u;
    crumb[1] = 0u;
    crumb[2] = 0u;

    line_begin("wdg");
    field_s("phase", "after");
    field_u("crumb", crumb_ok ? 1u : 0u);
    field_u("elapsed_cycles", elapsed);
    field_x("bkp", backup);
    line_end();
}

int main(void)
{
    board_boot("basic");
    /* A watchdog flag without a breadcrumb is stale (flags outlive pin resets
     * until cleared), so this image arms its own watchdog in that case too. */
    if ((board_reset_flags & RCC_CSR_IWDGRSTF) != 0u && (crumb_left() || backup_left())) {
        after_watchdog();
        finish();
    }
    report_identity();
    burst();
    arm_watchdog();
}
