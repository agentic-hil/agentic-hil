/*
 * An ESP32 that says which image it is and how long it has been running.
 *
 * The ROM loads this image itself from flash offset 0x1000, the second-stage
 * bootloader's place, straight into RAM and jumps to it: no ESP-IDF, no
 * bootloader, no partition table, nothing in flash but this one image. Every
 * line it prints names the image (A or B, chosen at build time) and a tick count
 * that starts at 0 at boot and only ever grows. That is the whole of what the
 * bench's ESP32 stage reads back through the board's console:
 *
 *   - the image letter says which of two flashes the chip is running, so a
 *     flash that reported success but changed nothing cannot pass;
 *   - the tick count going backwards between two reads says the chip was
 *     restarted in between, and still growing says it was not;
 *   - no line at all after a reset into the ROM bootloader says the chip is
 *     held there and not running this image.
 *
 * The ROM arms the RTC watchdog and timer group 0's watchdog for a flash boot,
 * expecting the second-stage bootloader to take them over. This image takes
 * them over by switching both off, or the chip would restart itself every few
 * seconds and the tick count would mean nothing.
 *
 * Every address below is from ESP-IDF v6.1, in the file named beside it.
 * Built by tools/build_esp32_bench_images.py, which names the toolchain.
 */

#include <stdint.h>

#ifndef IMAGE
#error "build with -DIMAGE='\"A\"' or -DIMAGE='\"B\"'"
#endif

#define REG(address) (*(volatile uint32_t *)(address))

/* components/soc/esp32/register/soc/reg_base.h */
#define DR_REG_RTCCNTL_BASE 0x3FF48000U
#define DR_REG_TIMERGROUP0_BASE 0x3FF5F000U

/* components/soc/esp32/register/soc/rtc_cntl_reg.h */
#define RTC_CNTL_WDTCONFIG0_REG (DR_REG_RTCCNTL_BASE + 0x8CU)
#define RTC_CNTL_WDTWPROTECT_REG (DR_REG_RTCCNTL_BASE + 0xA4U)

/* components/soc/esp32/register/soc/timer_group_reg.h, REG_TIMG_BASE(0) */
#define TIMG0_WDTCONFIG0_REG (DR_REG_TIMERGROUP0_BASE + 0x48U)
#define TIMG0_WDTWPROTECT_REG (DR_REG_TIMERGROUP0_BASE + 0x64U)

/* components/esp_hal_wdt/esp32/include/hal/rwdt_ll.h and mwdt_ll.h: the key
 * that write-enables either watchdog's registers, and 0 locks them again. */
#define WDT_WKEY_VALUE 0x50D83AA1U

#define TICK_PERIOD_US 100000U

/* components/esp_rom/esp32/ld/esp32.rom.ld; the addresses are in esp32_ram.ld. */
extern int ets_printf(const char *format, ...);
extern void ets_delay_us(uint32_t us);

static void watchdogs_off(void)
{
    /* A zero configuration clears each watchdog's enable bit and its
     * flash-boot mode bit together, along with every stage's action. */
    REG(RTC_CNTL_WDTWPROTECT_REG) = WDT_WKEY_VALUE;
    REG(RTC_CNTL_WDTCONFIG0_REG) = 0U;
    REG(RTC_CNTL_WDTWPROTECT_REG) = 0U;

    REG(TIMG0_WDTWPROTECT_REG) = WDT_WKEY_VALUE;
    REG(TIMG0_WDTCONFIG0_REG) = 0U;
    REG(TIMG0_WDTWPROTECT_REG) = 0U;
}

__attribute__((noreturn)) void call_start_cpu0(void)
{
    watchdogs_off();
    for (uint32_t tick = 0U;; ++tick) {
        ets_printf("agentic-hil esp32 image %s tick %u\n", IMAGE, (unsigned)tick);
        ets_delay_us(TICK_PERIOD_US);
    }
}
