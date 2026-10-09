/*
 * Validation firmware for zephyrproject-rtos/zephyr#120620 on NUCLEO-F446RE.
 *
 * Boots at the clock configuration of its overlay, records the over-drive,
 * regulator and clock tree state after boot and around every Stop mode exit,
 * measures the core and TIM5 clocks against the board's 32.768 kHz LSE
 * crystal, and then answers one line per request on the console UART, so
 * that a test plan can read every value without racing the output.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/pm/pm.h>
#include <zephyr/pm/policy.h>
#include <zephyr/sys/printk.h>

#include <soc.h>
#include <stm32_ll_adc.h>

#define STOP_CYCLES          6U
#define STOP_SLEEP_MS        1500
#define FIRST_STOP_AFTER_MS  5000
#define LSE_HZ               32768U
#define LSE_EDGES            2048U
#define LSE_START_TIMEOUT_MS 2000
#define EDGE_TIMEOUT_CYCLES  2000000U
#define VREFINT_SAMPLES      16U
#define RTC_ALARM_EXTI_LINE  17U
/* IPSR while the RTC alarm interrupt runs: its IRQ number plus 16 */
#define RTC_ALARM_EXCEPTION  ((uint32_t)RTC_Alarm_IRQn + 16U)

/* IWDG on LSI (about 32 kHz): prescaler /64, reload 3000, about 6 s. */
#define IWDG_PR_DIV64 4U
#define IWDG_RELOAD   3000U

#define SWS_HSI 0U
#define SWS_PLL 2U

struct clock_regs {
	uint32_t pwr_cr;
	uint32_t pwr_csr;
	uint32_t rcc_cr;
	uint32_t rcc_pllcfgr;
	uint32_t rcc_cfgr;
	uint32_t flash_acr;
};

struct clock_sample {
	uint32_t core_hz;
	uint32_t tim5_hz;
	/* 0 ok, 1 no first LSE edge, 2 LSE edge lost, 3 overcapture, 4 LSE not running */
	uint32_t status;
};

struct stop_cycle {
	/* pm notifier, before the core enters Stop mode */
	uint32_t entries;
	uint32_t entry_exti_imr;
	uint32_t entry_exti_emr;
	uint32_t entry_rtc_cr;
	uint32_t entry_rtc_alarm_nvic;
	uint32_t entry_dbgmcu_cr;
	/* stm32_clock_control_init() called from pm_state_exit_post_ops(), inside
	 * the RTC alarm interrupt and before the alarm's handler
	 */
	uint32_t restores;
	uint32_t restore_ipsr;
	uint32_t restore_exti_pr;
	uint32_t restore_rtc_irqs; /* alarm handlers run since the Stop entry */
	int restore_ret;
	struct clock_regs exit;  /* as Stop mode left them, before the restore */
	struct clock_regs after; /* right after the restore */
	/* the RTC alarm handler, seen through stm32_exti_clear_pending(17) */
	uint32_t rtc_irqs;
	uint32_t irq_restores; /* restores run since the Stop entry */
	uint32_t irq_exti_pr;
	uint32_t irq_rcc_cfgr;
	/* main thread, once it runs again */
	struct clock_regs main;
	struct clock_sample clock;
};

/* Index 0 collects anything that happens outside the Stop cycles. */
static struct stop_cycle cycles[STOP_CYCLES + 1U];
static volatile uint32_t cycle_index;
/* Since the last Stop entry */
static volatile uint32_t restores_since_entry;
static volatile uint32_t rtc_irqs_since_entry;

/* Over every Stop exit since boot */
static uint32_t entries_total;
static uint32_t exits_total;
static uint32_t rtc_irqs_total;
static uint32_t other_entries;
static uint32_t exit_od_or;
static uint32_t after_od_and = 0xFU;
static uint32_t after_od_or;
static bool every_exit_on_hsi = true;
static bool every_restore_in_alarm_irq = true;
static bool every_after_on_pll = true;
/* Over every RTC alarm handler since boot */
static bool every_alarm_handler_after_restore = true;
/* Over every Stop entry since boot */
static bool every_entry_wake_rtc_alarm_only = true;
static bool every_entry_debug_stop_off = true;

static struct clock_regs boot_regs;
static struct clock_sample boot_clock;
static uint32_t rcc_csr_at_boot;
static uint32_t dbgmcu_at_main;
static bool after_watchdog;
static bool watchdog_running;
static const char *lse_state = "off";
static uint32_t cycles_done;
static uint32_t vdda_mv;
static uint32_t vrefint_cal;
static uint32_t vrefint_avg;

static const struct device *const console = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

static void read_clock_regs(struct clock_regs *r)
{
	r->pwr_cr = PWR->CR;
	r->pwr_csr = PWR->CSR;
	r->rcc_cr = RCC->CR;
	r->rcc_pllcfgr = RCC->PLLCFGR;
	r->rcc_cfgr = RCC->CFGR;
	r->flash_acr = FLASH->ACR;
}

static uint32_t bit(uint32_t reg, uint32_t mask)
{
	return (reg & mask) != 0U ? 1U : 0U;
}

static uint32_t field(uint32_t reg, uint32_t mask, uint32_t pos)
{
	return (reg & mask) >> pos;
}

/* ODEN, ODSWEN, ODRDY, ODSWRDY as bits 0..3 */
static uint32_t od_bits(const struct clock_regs *r)
{
	return bit(r->pwr_cr, PWR_CR_ODEN) | (bit(r->pwr_cr, PWR_CR_ODSWEN) << 1) |
	       (bit(r->pwr_csr, PWR_CSR_ODRDY) << 2) | (bit(r->pwr_csr, PWR_CSR_ODSWRDY) << 3);
}

static uint32_t sws(const struct clock_regs *r)
{
	return field(r->rcc_cfgr, RCC_CFGR_SWS, RCC_CFGR_SWS_Pos);
}

static uint32_t ahb_div(uint32_t cfgr)
{
	static const uint16_t div[8] = {2, 4, 8, 16, 64, 128, 256, 512};
	uint32_t hpre = field(cfgr, RCC_CFGR_HPRE, RCC_CFGR_HPRE_Pos);

	return (hpre & 0x8U) != 0U ? div[hpre & 0x7U] : 1U;
}

static uint32_t apb_div(uint32_t ppre)
{
	return (ppre & 0x4U) != 0U ? (2U << (ppre & 0x3U)) : 1U;
}

static void print_regs(const struct clock_regs *r)
{
	printk("ODEN=%u ODSWEN=%u ODRDY=%u ODSWRDY=%u VOS=%u VOSRDY=%u SWS=%u PLLRDY=%u "
	       "HSEBYP=%u PLLSRC=%u PLLM=%u PLLN=%u PLLP=%u LATENCY=%u AHB_DIV=%u APB1_DIV=%u "
	       "APB2_DIV=%u PWR_CR=0x%08x PWR_CSR=0x%08x RCC_CR=0x%08x RCC_PLLCFGR=0x%08x "
	       "RCC_CFGR=0x%08x FLASH_ACR=0x%08x",
	       bit(r->pwr_cr, PWR_CR_ODEN), bit(r->pwr_cr, PWR_CR_ODSWEN),
	       bit(r->pwr_csr, PWR_CSR_ODRDY), bit(r->pwr_csr, PWR_CSR_ODSWRDY),
	       field(r->pwr_cr, PWR_CR_VOS, PWR_CR_VOS_Pos), bit(r->pwr_csr, PWR_CSR_VOSRDY),
	       sws(r), bit(r->rcc_cr, RCC_CR_PLLRDY), bit(r->rcc_cr, RCC_CR_HSEBYP),
	       bit(r->rcc_pllcfgr, RCC_PLLCFGR_PLLSRC),
	       field(r->rcc_pllcfgr, RCC_PLLCFGR_PLLM, RCC_PLLCFGR_PLLM_Pos),
	       field(r->rcc_pllcfgr, RCC_PLLCFGR_PLLN, RCC_PLLCFGR_PLLN_Pos),
	       (field(r->rcc_pllcfgr, RCC_PLLCFGR_PLLP, RCC_PLLCFGR_PLLP_Pos) + 1U) * 2U,
	       field(r->flash_acr, FLASH_ACR_LATENCY, FLASH_ACR_LATENCY_Pos),
	       ahb_div(r->rcc_cfgr),
	       apb_div(field(r->rcc_cfgr, RCC_CFGR_PPRE1, RCC_CFGR_PPRE1_Pos)),
	       apb_div(field(r->rcc_cfgr, RCC_CFGR_PPRE2, RCC_CFGR_PPRE2_Pos)), r->pwr_cr,
	       r->pwr_csr, r->rcc_cr, r->rcc_pllcfgr, r->rcc_cfgr, r->flash_acr);
}

/* --- hooks into the code path under test ------------------------------- */

/* Linked with --wrap: only pm_state_exit_post_ops() reaches this, the boot
 * time call is made from inside the clock driver's own translation unit.
 *
 * STM32F4 selects PM_STATE_SET_IRQ_UNLOCKED, so _kernel.idle is still set
 * while pm_state_set() sleeps, and k_cpu_idle() unmasks interrupts as soon as
 * the core is awake. The RTC alarm that woke it is taken at once, and
 * _isr_wrapper() calls pm_system_resume(), and with it this restore, before
 * the alarm's own handler: the restore runs inside that interrupt, on HSI.
 */
int __real_stm32_clock_control_init(const struct device *dev);
int __wrap_stm32_clock_control_init(const struct device *dev);

int __wrap_stm32_clock_control_init(const struct device *dev)
{
	struct stop_cycle *c = &cycles[cycle_index];
	uint32_t ipsr = __get_IPSR();
	uint32_t exti_pr = EXTI->PR;
	struct clock_regs exit;
	int ret;

	read_clock_regs(&exit);
	ret = __real_stm32_clock_control_init(dev);
	read_clock_regs(&c->after);

	c->exit = exit;
	c->restores++;
	c->restore_ipsr = ipsr;
	c->restore_exti_pr = exti_pr;
	c->restore_rtc_irqs = rtc_irqs_since_entry;
	c->restore_ret = ret;
	restores_since_entry++;

	exits_total++;
	exit_od_or |= od_bits(&exit);
	after_od_and &= od_bits(&c->after);
	after_od_or |= od_bits(&c->after);
	if (sws(&exit) != SWS_HSI || bit(exit.rcc_cr, RCC_CR_PLLRDY) != 0U) {
		every_exit_on_hsi = false;
	}
	if (ipsr != RTC_ALARM_EXCEPTION || bit(exti_pr, BIT(RTC_ALARM_EXTI_LINE)) == 0U ||
	    c->restore_rtc_irqs != 0U) {
		every_restore_in_alarm_irq = false;
	}
	if (ret != 0 || sws(&c->after) != SWS_PLL || bit(c->after.rcc_cr, RCC_CR_PLLRDY) == 0U) {
		every_after_on_pll = false;
	}
	return ret;
}

/* Called by the RTC alarm's handler, which runs after the restore, inside the
 * same interrupt, with the core back on the PLL.
 */
int __real_stm32_exti_clear_pending(uint32_t line_num);
int __wrap_stm32_exti_clear_pending(uint32_t line_num);

int __wrap_stm32_exti_clear_pending(uint32_t line_num)
{
	if (line_num == RTC_ALARM_EXTI_LINE) {
		struct stop_cycle *c = &cycles[cycle_index];

		c->irq_exti_pr = EXTI->PR;
		c->irq_rcc_cfgr = RCC->CFGR;
		c->irq_restores = restores_since_entry;
		c->rtc_irqs++;
		rtc_irqs_total++;
		rtc_irqs_since_entry++;
		if (bit(c->irq_exti_pr, BIT(RTC_ALARM_EXTI_LINE)) == 0U || c->irq_restores != 1U ||
		    field(c->irq_rcc_cfgr, RCC_CFGR_SWS, RCC_CFGR_SWS_Pos) != SWS_PLL) {
			every_alarm_handler_after_restore = false;
		}
	}
	return __real_stm32_exti_clear_pending(line_num);
}

static void on_state_entry(enum pm_state state)
{
	struct stop_cycle *c = &cycles[cycle_index];

	if (state != PM_STATE_SUSPEND_TO_IDLE) {
		other_entries++;
		return;
	}
	c->entries++;
	entries_total++;
	c->entry_exti_imr = EXTI->IMR;
	c->entry_exti_emr = EXTI->EMR;
	c->entry_rtc_cr = RTC->CR;
	c->entry_rtc_alarm_nvic = NVIC_GetEnableIRQ(RTC_Alarm_IRQn) != 0U ? 1U : 0U;
	c->entry_dbgmcu_cr = DBGMCU->CR;
	restores_since_entry = 0U;
	rtc_irqs_since_entry = 0U;

	/* Stop mode wakes on EXTI lines only: line 17 alone, with alarm A armed. */
	if (c->entry_exti_imr != BIT(RTC_ALARM_EXTI_LINE) || c->entry_exti_emr != 0U ||
	    bit(c->entry_rtc_cr, RTC_CR_ALRAE) == 0U || bit(c->entry_rtc_cr, RTC_CR_ALRAIE) == 0U ||
	    c->entry_rtc_alarm_nvic == 0U) {
		every_entry_wake_rtc_alarm_only = false;
	}
	if ((c->entry_dbgmcu_cr & (DBGMCU_CR_DBG_STOP | DBGMCU_CR_DBG_STANDBY)) != 0U) {
		every_entry_debug_stop_off = false;
	}
}

static struct pm_notifier notifier = {
	.state_entry = on_state_entry,
};

/* --- watchdog ----------------------------------------------------------- */

/* Keeps the bench recoverable: a core that never leaves Stop mode, where the
 * probe cannot reach it, is reset after about 6 s. The next boot sees
 * IWDGRSTF and does not enter Stop mode again.
 */
static void watchdog_start(void)
{
	IWDG->KR = 0xCCCCU;
	IWDG->KR = 0x5555U;
	IWDG->PR = IWDG_PR_DIV64;
	IWDG->RLR = IWDG_RELOAD;
	for (uint32_t i = 0U; i < 100000U && IWDG->SR != 0U; i++) {
		k_busy_wait(1);
	}
	IWDG->KR = 0xAAAAU;
	watchdog_running = true;
}

static void watchdog_feed(void)
{
	if (watchdog_running) {
		IWDG->KR = 0xAAAAU;
	}
}

static void sleep_fed(int32_t ms)
{
	while (ms > 0) {
		int32_t step = ms > 200 ? 200 : ms;

		watchdog_feed();
		k_msleep(step);
		ms -= step;
	}
	watchdog_feed();
}

/* --- reference clock and supply ---------------------------------------- */

static bool lse_wait(void)
{
	for (int32_t waited = 0; waited < LSE_START_TIMEOUT_MS; waited += 10) {
		if ((RCC->BDCR & RCC_BDCR_LSERDY) != 0U) {
			return true;
		}
		sleep_fed(10);
	}
	return (RCC->BDCR & RCC_BDCR_LSERDY) != 0U;
}

/* The board's 32.768 kHz crystal, separate from the 8 MHz HSE that the
 * ST-LINK supplies. The RTC stays on LSI as the board configures it.
 */
static void lse_start(void)
{
	unsigned int key;

	if ((RCC->BDCR & RCC_BDCR_LSERDY) != 0U) {
		lse_state = "ok";
		return;
	}
	key = irq_lock();
	PWR->CR |= PWR_CR_DBP;
	RCC->BDCR |= RCC_BDCR_LSEON;
	irq_unlock(key);
	if (lse_wait()) {
		lse_state = "ok";
		return;
	}
	/* LSEMOD can only change while the oscillator is off. */
	key = irq_lock();
	PWR->CR |= PWR_CR_DBP;
	RCC->BDCR &= ~RCC_BDCR_LSEON;
	irq_unlock(key);
	for (int32_t i = 0; i < 100 && (RCC->BDCR & RCC_BDCR_LSERDY) != 0U; i++) {
		sleep_fed(1);
	}
	key = irq_lock();
	PWR->CR |= PWR_CR_DBP;
	RCC->BDCR |= RCC_BDCR_LSEMOD;
	RCC->BDCR |= RCC_BDCR_LSEON;
	irq_unlock(key);
	lse_state = lse_wait() ? "ok-high-drive" : "timeout";
}

/* TIM5 channel 4 captures LSE edges with the APB1 timer clock; DWT counts
 * core cycles between the same edges.
 */
static void capture_setup(void)
{
	RCC->APB1ENR |= RCC_APB1ENR_TIM5EN;
	(void)RCC->APB1ENR;
	TIM5->CR1 = 0U;
	TIM5->PSC = 0U;
	TIM5->ARR = 0xFFFFFFFFU;
	TIM5->OR = TIM_OR_TI4_RMP_1;
	TIM5->CCMR2 = TIM_CCMR2_CC4S_0;
	TIM5->CCER = TIM_CCER_CC4E;
	TIM5->EGR = TIM_EGR_UG;
	TIM5->SR = 0U;
	TIM5->CR1 = TIM_CR1_CEN;

	DCB->DEMCR |= DCB_DEMCR_TRCENA_Msk;
	DWT->CYCCNT = 0U;
	DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

static bool wait_edge(void)
{
	uint32_t start = DWT->CYCCNT;

	while ((TIM5->SR & TIM_SR_CC4IF) == 0U) {
		if (DWT->CYCCNT - start > EDGE_TIMEOUT_CYCLES) {
			return false;
		}
	}
	return true;
}

static void measure_clock(struct clock_sample *s)
{
	uint32_t c0, c1 = 0U, t0, t1 = 0U;
	unsigned int key;

	s->core_hz = 0U;
	s->tim5_hz = 0U;
	if ((RCC->BDCR & RCC_BDCR_LSERDY) == 0U) {
		s->status = 4U;
		return;
	}
	key = irq_lock();
	(void)TIM5->CCR4;
	TIM5->SR = 0U;
	if (!wait_edge()) {
		s->status = 1U;
		goto out;
	}
	c0 = DWT->CYCCNT;
	t0 = TIM5->CCR4;
	for (uint32_t n = 0U; n < LSE_EDGES; n++) {
		if (!wait_edge()) {
			s->status = 2U;
			goto out;
		}
		c1 = DWT->CYCCNT;
		t1 = TIM5->CCR4;
	}
	if ((TIM5->SR & TIM_SR_CC4OF) != 0U) {
		s->status = 3U;
		goto out;
	}
	s->core_hz = (uint32_t)(((uint64_t)(c1 - c0) * LSE_HZ) / LSE_EDGES);
	s->tim5_hz = (uint32_t)(((uint64_t)(t1 - t0) * LSE_HZ) / LSE_EDGES);
	s->status = 0U;
out:
	irq_unlock(key);
}

/* VDDA from the internal reference and its factory calibration (taken at
 * 3.3 V). Flash wait states of 5 at 168 and 180 MHz assume 2.7 to 3.6 V.
 */
static void measure_supply(void)
{
	uint32_t sum = 0U;

	RCC->APB2ENR |= RCC_APB2ENR_ADC1EN;
	(void)RCC->APB2ENR;
	ADC123_COMMON->CCR = (ADC123_COMMON->CCR & ~ADC_CCR_ADCPRE) | ADC_CCR_ADCPRE_0 |
			     ADC_CCR_TSVREFE;
	ADC1->CR1 = 0U;
	ADC1->CR2 = 0U;
	ADC1->SMPR1 = (ADC1->SMPR1 & ~ADC_SMPR1_SMP17) | (7U << ADC_SMPR1_SMP17_Pos);
	ADC1->SQR1 = 0U;
	ADC1->SQR3 = 17U;
	ADC1->CR2 = ADC_CR2_ADON;
	k_busy_wait(1000);
	for (uint32_t i = 0U; i < VREFINT_SAMPLES; i++) {
		uint32_t waited = 0U;

		ADC1->SR = 0U;
		ADC1->CR2 |= ADC_CR2_SWSTART;
		while ((ADC1->SR & ADC_SR_EOC) == 0U && waited < 1000U) {
			k_busy_wait(1);
			waited++;
		}
		sum += ADC1->DR & 0xFFFU;
	}
	ADC1->CR2 = 0U;
	ADC123_COMMON->CCR &= ~ADC_CCR_TSVREFE;
	RCC->APB2ENR &= ~RCC_APB2ENR_ADC1EN;

	vrefint_cal = *VREFINT_CAL_ADDR;
	vrefint_avg = sum / VREFINT_SAMPLES;
	vdda_mv = sum != 0U ? (uint32_t)(((uint64_t)VREFINT_CAL_VREF * vrefint_cal *
					  VREFINT_SAMPLES) / sum)
			    : 0U;
}

/* --- answers ------------------------------------------------------------ */

/* Every answer is one line of at most 512 bytes, which is what a test plan's
 * serial read keeps and reports of one match.
 */
static void answer_cycle(uint32_t k)
{
	const struct stop_cycle *c = &cycles[k];

	printk("Z120620 cycle=%u entries=%u restores=%u rtc_irqs=%u entry_exti_imr=0x%08x "
	       "entry_exti_emr=0x%08x entry_alarm_a=%u entry_rtc_alarm_nvic=%u "
	       "entry_dbgmcu=0x%08x restore_ipsr=%u restore_exti_pr17=%u restore_rtc_irqs=%u "
	       "restore_ret=%d irq_restores=%u irq_exti_pr17=%u irq_SWS=%u main_od=0x%x "
	       "main_SWS=%u end\n",
	       k, c->entries, c->restores, c->rtc_irqs, c->entry_exti_imr, c->entry_exti_emr,
	       bit(c->entry_rtc_cr, RTC_CR_ALRAE) & bit(c->entry_rtc_cr, RTC_CR_ALRAIE),
	       c->entry_rtc_alarm_nvic, c->entry_dbgmcu_cr, c->restore_ipsr,
	       bit(c->restore_exti_pr, BIT(RTC_ALARM_EXTI_LINE)), c->restore_rtc_irqs,
	       c->restore_ret, c->irq_restores, bit(c->irq_exti_pr, BIT(RTC_ALARM_EXTI_LINE)),
	       field(c->irq_rcc_cfgr, RCC_CFGR_SWS, RCC_CFGR_SWS_Pos), od_bits(&c->main),
	       sws(&c->main));
}

static void answer_regs(const char *kind, uint32_t k, const struct clock_regs *r)
{
	printk("Z120620 %s=%u ", kind, k);
	print_regs(r);
	printk(" end\n");
}

static void answer_clock(uint32_t k)
{
	const struct clock_sample *s = k == 0U ? &boot_clock : &cycles[k].clock;

	printk("Z120620 clock=%u core_hz=%u tim5_hz=%u status=%u end\n", k, s->core_hz,
	       s->tim5_hz, s->status);
}

static void answer(const char *line)
{
	size_t len = strlen(line);
	uint32_t k = len > 0U ? (uint32_t)(line[len - 1U] - '0') : 99U;

	if (strcmp(line, "hello") == 0) {
		printk("Z120620 hello variant=%s zephyr=%s board=nucleo_f446re sysclk_dt_hz=%u "
		       "rcc_csr=0x%08x iwdg_reset=%u dbgmcu_at_main=0x%08x lse=%s end\n",
		       Z120620_VARIANT, Z120620_ZEPHYR, CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC,
		       rcc_csr_at_boot, after_watchdog ? 1U : 0U, dbgmcu_at_main, lse_state);
	} else if (strcmp(line, "boot") == 0) {
		printk("Z120620 boot ");
		print_regs(&boot_regs);
		printk(" end\n");
	} else if (strcmp(line, "supply") == 0) {
		printk("Z120620 supply vdda_mv=%u vrefint_cal=%u vrefint_avg=%u end\n", vdda_mv,
		       vrefint_cal, vrefint_avg);
	} else if (len == 7U && strncmp(line, "clock ", 6) == 0 && k <= cycles_done) {
		answer_clock(k);
	} else if (len == 7U && strncmp(line, "cycle ", 6) == 0 && k >= 1U && k <= cycles_done) {
		answer_cycle(k);
	} else if (len == 6U && strncmp(line, "exit ", 5) == 0 && k >= 1U && k <= cycles_done) {
		answer_regs("exit", k, &cycles[k].exit);
	} else if (len == 7U && strncmp(line, "after ", 6) == 0 && k >= 1U && k <= cycles_done) {
		answer_regs("after", k, &cycles[k].after);
	} else if (strcmp(line, "done") == 0) {
		printk("Z120620 done variant=%s cycles=%u entries=%u exits=%u rtc_irqs=%u "
		       "other_entries=%u outside_cycles=%u exit_od_or=0x%x after_od_and=0x%x "
		       "after_od_or=0x%x every_entry_wake_rtc_alarm_only=%u "
		       "every_entry_debug_stop_off=%u every_exit_on_hsi=%u "
		       "every_restore_in_alarm_irq=%u every_after_on_pll=%u "
		       "every_alarm_handler_after_restore=%u watchdog=%s end\n",
		       Z120620_VARIANT, cycles_done, entries_total, exits_total, rtc_irqs_total,
		       other_entries, cycles[0].entries + cycles[0].restores + cycles[0].rtc_irqs,
		       exit_od_or, after_od_and, after_od_or,
		       every_entry_wake_rtc_alarm_only ? 1U : 0U,
		       every_entry_debug_stop_off ? 1U : 0U, every_exit_on_hsi ? 1U : 0U,
		       every_restore_in_alarm_irq ? 1U : 0U, every_after_on_pll ? 1U : 0U,
		       every_alarm_handler_after_restore ? 1U : 0U, watchdog_running ? "on" : "off");
	} else {
		printk("Z120620 unknown end\n");
	}
}

/* Busy-polls the console: Stop mode is locked out from here on, and polling
 * keeps every byte of a request at 115200 baud.
 */
static void serve(void)
{
	char line[16];
	size_t len = 0U;

	for (;;) {
		unsigned char ch;

		watchdog_feed();
		if (uart_poll_in(console, &ch) != 0) {
			continue;
		}
		if (ch == '\r' || ch == '\n') {
			line[len] = '\0';
			if (len > 0U) {
				answer(line);
			}
			len = 0U;
		} else if (len < sizeof(line) - 1U) {
			line[len++] = (char)ch;
		} else {
			len = 0U;
		}
	}
}

int main(void)
{
	read_clock_regs(&boot_regs);
	rcc_csr_at_boot = RCC->CSR;
	dbgmcu_at_main = DBGMCU->CR;
	after_watchdog = (rcc_csr_at_boot & RCC_CSR_IWDGRSTF) != 0U;
	RCC->CSR |= RCC_CSR_RMVF;

	/* A halted core freezes the watchdog; the probe keeps working in Sleep. */
	DBGMCU->APB1FZ |= DBGMCU_APB1_FZ_DBG_IWDG_STOP;
	DBGMCU->CR |= DBGMCU_CR_DBG_SLEEP;

	/* No Stop mode outside the cycles below. */
	pm_policy_state_lock_get(PM_STATE_SUSPEND_TO_IDLE, PM_ALL_SUBSTATES);
	pm_notifier_register(&notifier);

	if (!after_watchdog) {
		watchdog_start();
	}
	printk("Z120620 log start variant=%s iwdg_reset=%u\n", Z120620_VARIANT,
	       after_watchdog ? 1U : 0U);

	lse_start();
	capture_setup();
	measure_supply();
	measure_clock(&boot_clock);
	printk("Z120620 log boot od=0x%x lse=%s core_hz=%u vdda_mv=%u\n", od_bits(&boot_regs),
	       lse_state, boot_clock.core_hz, vdda_mv);

	if (!after_watchdog) {
		sleep_fed(FIRST_STOP_AFTER_MS - (int32_t)k_uptime_get_32());
		/* Real Stop mode: no debug clocks kept running during the cycles. */
		DBGMCU->CR &= ~(DBGMCU_CR_DBG_STOP | DBGMCU_CR_DBG_STANDBY);

		for (uint32_t k = 1U; k <= STOP_CYCLES; k++) {
			struct stop_cycle *c = &cycles[k];
			unsigned int key;

			cycle_index = k;
			watchdog_feed();
			pm_policy_state_lock_put(PM_STATE_SUSPEND_TO_IDLE, PM_ALL_SUBSTATES);
			k_msleep(STOP_SLEEP_MS);
			pm_policy_state_lock_get(PM_STATE_SUSPEND_TO_IDLE, PM_ALL_SUBSTATES);
			watchdog_feed();

			key = irq_lock();
			read_clock_regs(&c->main);
			cycle_index = 0U;
			irq_unlock(key);

			measure_clock(&c->clock);
			cycles_done = k;
			printk("Z120620 log cycle=%u entries=%u restores=%u rtc_irqs=%u exit_od=0x%x "
			       "after_od=0x%x core_hz=%u\n",
			       k, c->entries, c->restores, c->rtc_irqs, od_bits(&c->exit),
			       od_bits(&c->after), c->clock.core_hz);
			sleep_fed(200);
		}
	}

	/* Stop mode stays locked from here on; let a probe in even if it were not. */
	DBGMCU->CR |= DBGMCU_CR_DBG_STOP | DBGMCU_CR_DBG_STANDBY;
	printk("Z120620 ready variant=%s cycles=%u iwdg_reset=%u end\n", Z120620_VARIANT,
	       cycles_done, after_watchdog ? 1U : 0U);
	serve();
	return 0;
}
