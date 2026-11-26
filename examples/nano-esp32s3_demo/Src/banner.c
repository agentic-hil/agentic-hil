/* The ESP32-S3 ROM loads this image into RAM from flash offset 0x0. */
#include <stdint.h>

#ifndef IMAGE
#error "Define IMAGE as the string A or B."
#endif

#define REG(address) (*(volatile uint32_t *)(address))

/* ESP-IDF v6.1: ESP32-S3 reg_base.h, rtc_cntl_reg.h, timer_group_reg.h. */
#define RTC_CNTL_BASE 0x60008000U
#define TIMERGROUP0_BASE 0x6001F000U
#define RTC_WDT_CONFIG (RTC_CNTL_BASE + 0x98U)
#define RTC_WDT_PROTECT (RTC_CNTL_BASE + 0xB0U)
#define SWD_CONFIG (RTC_CNTL_BASE + 0xB4U)
#define SWD_PROTECT (RTC_CNTL_BASE + 0xB8U)
#define SWD_AUTO_FEED (1U << 31)
#define TIMG0_WDT_CONFIG (TIMERGROUP0_BASE + 0x48U)
#define TIMG0_WDT_PROTECT (TIMERGROUP0_BASE + 0x64U)
#define WDT_KEY 0x50D83AA1U
#define SWD_KEY 0x8F1D312AU

extern int ets_printf(const char *format, ...);
extern void ets_delay_us(uint32_t us);

static void configure_watchdogs(void)
{
    REG(RTC_WDT_PROTECT) = WDT_KEY;
    REG(RTC_WDT_CONFIG) = 0U;
    REG(RTC_WDT_PROTECT) = 0U;

    REG(SWD_PROTECT) = SWD_KEY;
    REG(SWD_CONFIG) |= SWD_AUTO_FEED;
    REG(SWD_PROTECT) = 0U;

    REG(TIMG0_WDT_PROTECT) = WDT_KEY;
    REG(TIMG0_WDT_CONFIG) = 0U;
    REG(TIMG0_WDT_PROTECT) = 0U;
}

__attribute__((noreturn)) void call_start_cpu0(void)
{
    configure_watchdogs();
    for (uint32_t tick = 0U;; ++tick) {
        ets_printf("agentic-hil esp32s3 image %s tick %u\n", IMAGE, (unsigned)tick);
        ets_delay_us(100000U);
    }
}
