/* SPDX-License-Identifier: Apache-2.0 */
/*
 * hq172: what a device-PM suspend of USART1 does while hardware CTS holds one frame.
 *
 * One application source, two images: the Zephyr tree is the only difference between them, so a
 * difference in behaviour is the one PR #121031 makes. This image only measures and prints; the
 * gate in tests/hq172 computes every verdict from the raw values on these lines.
 *
 *   HQ172 build ...      once: what this image was built as, and how the USART came up.
 *   HQ172 cts n=0..2     does the PA11 pull flip hold and release USART1 at all? Measured
 *                        before any power-management action is run.
 *   HQ172 rep n=0..104   5 repetitions with CTS released before the suspend, then 100 with CTS
 *                        held and exactly one frame waiting in the transmitter.
 *   HQ172 done
 *
 * Nothing is wired to the board. CTS (PA11, alternate function 7) is deasserted by the pin's own
 * pull-up, which is what holds the transmitter, and asserted by switching GPIOA->PUPDR to
 * pull-down from a k_timer interrupt: that interrupt runs whether or not the thread which called
 * the suspend is stuck, which is the point of the measurement. PA11 and PA12 reach only the
 * morpho header, and nothing is measured outside the chip. No claim is made about modems or
 * external flow-control partners.
 *
 * Time is k_cycle_get_32(), the counter the driver's WAIT_FOR polls. Every stamp is printed as an
 * unsigned 32-bit cycle offset from the repetition's own t0 (printed raw), so the counter's wrap
 * every 44.7 s at 96 MHz never enters a difference. Every wait in this image is bounded; the one
 * wait that cannot be bounded from the thread that performs it (the base tree's spin inside the
 * driver) is ended by the rescue interrupt, and every repetition it fired in is marked as such.
 */

#include <string.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/pm/device.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>
#include <zephyr/version.h>
#include <soc.h>

#define TAG "HQ172"

/* The repetition plan. The build record prints all four so that a reader, and the gate, knows
 * which plan produced the records that follow.
 */
#define N_CTS 3U
#define N_CONTROL 5U
#define N_HELD 100U
#define RELEASE_MS 300U
#define RESCUE_MS 1500U

/* The PR's Kconfig symbol exists in the head tree only; the base tree has no limit to report. */
#ifdef CONFIG_UART_STM32_SUSPEND_TIMEOUT_US
#define SUSPEND_TIMEOUT_US CONFIG_UART_STM32_SUSPEND_TIMEOUT_US
#else
#define SUSPEND_TIMEOUT_US 0
#endif

/* Which of the driver's three waits this SoC even compiles: the F4 USART has neither flag, so
 * only the transmission-complete wait is exercised by this report.
 */
#ifdef USART_ISR_BUSY
#define HAS_BUSY 1U
#else
#define HAS_BUSY 0U
#endif
#ifdef USART_ISR_REACK
#define HAS_REACK 1U
#else
#define HAS_REACK 0U
#endif

/* The transmission-complete flag (USART_SR bit 6), the transmit-data-register-empty flag (bit 7)
 * and the CTS-enable bit (USART_CR3 bit 9) come from the CMSIS device header through <soc.h>:
 * the same definitions the driver itself uses, and the bit numbers the gate reads out of the
 * registers printed below. PA11 is the CTS pin, and its two bits in GPIOA->PUPDR are the whole
 * of the in-chip stimulus.
 */
#define CTS_PIN 11U
#define PULL_UP 1U
#define PULL_DOWN 2U

/* Every bound in this image, in milliseconds or microseconds.
 *
 * TXE_WAIT and TC_WAIT are a hundred-odd frame times at 115200 baud (one frame is 87 us), long
 * enough that a healthy transmitter always makes them and short enough that a repetition which
 * does not cannot stall the run. HELD_WAIT is the window in which a frame must *not* complete
 * while CTS is held; one frame would do, 2 ms is twenty of them. SETTLE lets the pin's own pull
 * resistor move the line and a started frame finish before SR is read. PENDING, measured from
 * t0, covers the release at 300 ms plus one frame. WORK_WAIT is far above the longest suspend
 * this image can see (the release at 300 ms, or the rescue at 1500 ms).
 */
#define TXE_WAIT_MS 10U
#define TC_WAIT_MS 10U
#define HELD_WAIT_MS 2U
#define PENDING_MS 350U
#define WORK_WAIT_MS 2000U
#define SETTLE_US 1000U

static const struct device *const usart1 = DEVICE_DT_GET(DT_NODELABEL(usart1));

static struct k_work suspend_work;
static struct k_work progress_work;
static struct k_timer release_timer;
static struct k_timer rescue_timer;
static struct k_sem suspend_done;
static struct k_sem progress_done;

/* What the work items and the two interrupts measure. t0 is the reference of every stamp here;
 * all of them are zeroed at the start of a repetition, so a stamp of 0 in a record means its
 * event did not happen in that repetition. Index 0 is the suspend under test, index 1 the
 * suspend after CTS was released.
 */
static volatile uint32_t t0;
static volatile uint32_t slot;
static volatile uint32_t start_at[2];
static volatile uint32_t end_at[2];
static volatile int suspend_rc[2];
static volatile int suspend_state[2];
static volatile uint32_t failed_sr;
static volatile uint32_t progress_at;
static volatile uint32_t progressed;
static volatile uint32_t release_at;
static volatile uint32_t released;
static volatile uint32_t rescue_at;
static volatile uint32_t rescued;

/* What one repetition measures in the main thread. */
struct repetition {
	uint32_t n;
	uint32_t held;
	uint32_t ctse;
	uint32_t pin_held;
	uint32_t sent1;
	uint32_t sr_pre;
	uint32_t sr1;
	uint32_t resumed1;
	int resume1_rc;
	uint32_t pin_released;
	uint32_t pending_tc;
	uint32_t pending;
	uint32_t sent2;
	uint32_t regular_tc;
	uint32_t regular_cycles;
	uint32_t resumed2;
	int resume2_rc;
	uint32_t waited;
};

static inline uint32_t since_t0(void)
{
	return k_cycle_get_32() - t0;
}

/* The pin level CTS really has, read in alternate-function mode from the input data register. */
static inline uint32_t cts_level(void)
{
	return (GPIOA->IDR >> CTS_PIN) & 1U;
}

/* The in-chip equivalent of a jumper on the CTS line: PA11's own pull resistor is all that
 * decides whether the USART sees CTS asserted. Called from the main thread and from the release
 * interrupt, so the read-modify-write is locked.
 */
static void cts_pull(uint32_t pull)
{
	unsigned int key = irq_lock();

	GPIOA->PUPDR = (GPIOA->PUPDR & ~(3UL << (2U * CTS_PIN))) | (pull << (2U * CTS_PIN));
	irq_unlock(key);
}

/* Clear the transmission-complete flag (writing 0 to it, RM0390) so that the next frame, and
 * only the next frame, can set it again: that is what makes a frame time measurable.
 */
static void clear_tc(void)
{
	USART1->SR = ~USART_SR_TC;
}

/* 1 when `mask` appeared in USART_SR within `limit` cycles, `*at` then being the raw cycle stamp
 * at which it was seen; 0 when it did not, `*at` untouched. Used for every flag this image waits
 * for, so that no wait here can spin without a limit.
 */
static uint32_t wait_sr(uint32_t mask, uint32_t limit, uint32_t *at)
{
	uint32_t start = k_cycle_get_32();

	do {
		if ((USART1->SR & mask) != 0U) {
			*at = k_cycle_get_32();
			return 1U;
		}
	} while (k_cycle_get_32() - start < limit);

	return 0U;
}

/* The same, bounded by a deadline measured from t0: this is how the main thread waits for a
 * frame that only the release interrupt can let out.
 */
static uint32_t wait_sr_until(uint32_t mask, uint32_t deadline, uint32_t *at)
{
	do {
		if ((USART1->SR & mask) != 0U) {
			*at = k_cycle_get_32();
			return 1U;
		}
	} while (since_t0() < deadline);

	return 0U;
}

static void wait_released(uint32_t deadline)
{
	while (released == 0U && since_t0() < deadline) {
	}
}

/* The suspend under test, run from the system work queue: the context the issue describes as
 * "whichever thread or work queue runs the suspend".
 */
static void suspend_handler(struct k_work *work)
{
	enum pm_device_state state = PM_DEVICE_STATE_OFF;
	uint32_t which = slot;
	int rc;

	ARG_UNUSED(work);

	start_at[which] = since_t0();
	rc = pm_device_action_run(usart1, PM_DEVICE_ACTION_SUSPEND);
	end_at[which] = since_t0();
	suspend_rc[which] = rc;
	/* A failed read leaves the state at OFF, which is no state this sequence can produce. */
	(void)pm_device_state_get(usart1, &state);
	suspend_state[which] = (int)state;
	if (rc != 0) {
		/* The PM core leaves the device active when the action returns an error, so the
		 * clock is still on and SR still shows what the suspend gave up on. After a
		 * successful suspend the clock is off and no USART register may be read here.
		 */
		failed_sr = USART1->SR;
	}
	k_sem_give(&suspend_done);
}

/* The progress evidence: a second item on the same queue, behind the suspend. The time it ran is
 * the whole measurement; it computes nothing.
 */
static void progress_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	progress_at = since_t0();
	progressed = 1U;
	k_sem_give(&progress_done);
}

/* The release: an interrupt, so it happens at its time whether or not the work queue is stuck. */
static void release_expiry(struct k_timer *timer)
{
	ARG_UNUSED(timer);

	release_at = since_t0();
	cts_pull(PULL_DOWN);
	released = 1U;
}

/* The last resort against a wait without a limit. With CTSE clear the transmitter stops asking
 * CTS, finishes its frame and sets TC, so a run that would otherwise spin on reaches its done
 * line; the repetition is marked rescued and the gate rejects it. The next repetition sets CTSE
 * again. Nothing but a spinning suspend can bring the image here.
 */
static void rescue_expiry(struct k_timer *timer)
{
	ARG_UNUSED(timer);

	rescue_at = since_t0();
	USART1->CR3 &= ~USART_CR3_CTSE;
	rescued = 1U;
}

static void print_build(void)
{
	const char *version = STRINGIFY(BUILD_VERSION);
	uint32_t moder = GPIOA->MODER;
	uint32_t afrh = GPIOA->AFR[1];
	uint32_t pupdr = GPIOA->PUPDR;

	printk(TAG " build zephyr=%s timeout_us=%u limit_cycles=%u rate=%u ticks_per_sec=%u"
		   " has_busy=%u has_reack=%u cr1=0x%x cr3=0x%x pa11_mode=%u pa11_af=%u"
		   " pa11_pull=%u n_control=%u n_held=%u release_ms=%u rescue_ms=%u\n",
	       version[0] != '\0' ? version : "none", (uint32_t)SUSPEND_TIMEOUT_US,
	       k_us_to_cyc_ceil32(SUSPEND_TIMEOUT_US), (uint32_t)sys_clock_hw_cycles_per_sec(),
	       (uint32_t)CONFIG_SYS_CLOCK_TICKS_PER_SEC, HAS_BUSY, HAS_REACK, USART1->CR1,
	       USART1->CR3, (moder >> (2U * CTS_PIN)) & 3U,
	       (afrh >> (4U * (CTS_PIN - 8U))) & 0xFU, (pupdr >> (2U * CTS_PIN)) & 3U, N_CONTROL,
	       N_HELD, RELEASE_MS, RESCUE_MS);
}

/* Does the technique work at all? One frame is made to wait with nothing but PA11's pull-up, and
 * the pull-down must let it out within a frame time. Measured before any suspend, so a failure
 * here says the measurement is wrong and says nothing about the driver.
 */
static void cts_control(void)
{
	uint32_t n;

	for (n = 0U; n < N_CTS; n++) {
		uint32_t at = 0U;
		uint32_t flip;
		uint32_t pin_held;
		uint32_t held_tc;
		uint32_t held_sr;
		uint32_t released_tc;
		uint32_t release_cycles = 0U;
		uint32_t pin_released;
		uint32_t released_sr;

		cts_pull(PULL_UP);
		k_busy_wait(SETTLE_US);
		pin_held = cts_level();

		/* The byte is written only once TXE says the data register is free: uart_poll_out
		 * waits for TXE without a limit, which is not a wait this image may enter blind.
		 */
		if (wait_sr(USART_SR_TXE, k_ms_to_cyc_ceil32(TXE_WAIT_MS), &at) == 1U) {
			clear_tc();
			uart_poll_out(usart1, (unsigned char)(0x55U + n));
		}
		held_tc = wait_sr(USART_SR_TC, k_ms_to_cyc_ceil32(HELD_WAIT_MS), &at);
		held_sr = USART1->SR;

		flip = k_cycle_get_32();
		cts_pull(PULL_DOWN);
		released_tc = wait_sr(USART_SR_TC, k_ms_to_cyc_ceil32(TC_WAIT_MS), &at);
		if (released_tc == 1U) {
			release_cycles = at - flip;
		}
		pin_released = cts_level();
		released_sr = USART1->SR;

		cts_pull(PULL_UP);
		k_busy_wait(SETTLE_US);

		printk(TAG " cts n=%u pin_held=%u held_tc=%u held_txe=%u held_sr=0x%x"
			   " released_tc=%u release_cycles=%u pin_released=%u released_sr=0x%x\n",
		       n, pin_held, held_tc, (held_sr & USART_SR_TXE) != 0U ? 1U : 0U, held_sr,
		       released_tc, release_cycles, pin_released, released_sr);
	}
}

static void print_repetition(const struct repetition *r)
{
	uint32_t tc_pre = (r->sr_pre & USART_SR_TC) != 0U ? 1U : 0U;
	uint32_t txe_pre = (r->sr_pre & USART_SR_TXE) != 0U ? 1U : 0U;

	printk(TAG " rep n=%u cts=%s ctse=%u pin_held=%u sent1=%u sr_pre=0x%x tc_pre=%u"
		   " txe_pre=%u t0=%u start1=%u end1=%u dur1=%u rc1=%d state1=%d sr1=0x%x"
		   " resumed1=%u resume1_rc=%d released=%u release=%u pin_released=%u"
		   " progressed=%u progress=%u pending_tc=%u pending=%u sent2=%u regular_tc=%u"
		   " regular_cycles=%u start2=%u end2=%u dur2=%u rc2=%d state2=%d resumed2=%u"
		   " resume2_rc=%d rescued=%u rescue=%u waited=%u\n",
	       r->n, r->held == 1U ? "held" : "released", r->ctse, r->pin_held, r->sent1,
	       r->sr_pre, tc_pre, txe_pre, t0, start_at[0], end_at[0],
	       end_at[0] - start_at[0], suspend_rc[0], suspend_state[0], r->sr1, r->resumed1,
	       r->resume1_rc, released, release_at, r->pin_released, progressed, progress_at,
	       r->pending_tc, r->pending, r->sent2, r->regular_tc, r->regular_cycles,
	       start_at[1], end_at[1], end_at[1] - start_at[1], suspend_rc[1], suspend_state[1],
	       r->resumed2, r->resume2_rc, rescued, rescue_at, r->waited);
}

/* The sequence, identical for every repetition; the pull set at the start is the only difference
 * between a control repetition and a held one.
 */
static void run_repetition(uint32_t n, struct repetition *r)
{
	enum pm_device_state state = PM_DEVICE_STATE_OFF;
	uint32_t at = 0U;
	uint32_t write_at = 0U;
	unsigned int key;
	int first;
	int second;

	k_timer_stop(&release_timer);
	k_timer_stop(&rescue_timer);

	(void)memset(r, 0, sizeof(*r));
	r->n = n;
	r->held = (n >= N_CONTROL) ? 1U : 0U;

	start_at[0] = 0U;
	start_at[1] = 0U;
	end_at[0] = 0U;
	end_at[1] = 0U;
	suspend_rc[0] = 0;
	suspend_rc[1] = 0;
	suspend_state[0] = (int)PM_DEVICE_STATE_OFF;
	suspend_state[1] = (int)PM_DEVICE_STATE_OFF;
	failed_sr = 0U;
	progress_at = 0U;
	progressed = 0U;
	release_at = 0U;
	released = 0U;
	rescue_at = 0U;
	rescued = 0U;
	slot = 0U;
	k_sem_reset(&suspend_done);
	k_sem_reset(&progress_done);

	/* The device is active at this point in a healthy run; after a failure in an earlier
	 * repetition it may not be, and no USART register may be read while its clock is off.
	 */
	(void)pm_device_state_get(usart1, &state);
	if (state == PM_DEVICE_STATE_SUSPENDED) {
		(void)pm_device_action_run(usart1, PM_DEVICE_ACTION_RESUME);
	}

	/* Hardware flow control, which an earlier rescue may have switched off. */
	if ((USART1->CR3 & USART_CR3_CTSE) == 0U) {
		key = irq_lock();
		USART1->CR3 |= USART_CR3_CTSE;
		irq_unlock(key);
	}
	r->ctse = (USART1->CR3 & USART_CR3_CTSE) != 0U ? 1U : 0U;

	cts_pull(r->held == 1U ? PULL_UP : PULL_DOWN);
	k_busy_wait(SETTLE_US);
	r->pin_held = cts_level();

	/* Exactly one byte, so exactly one frame waits in the transmitter. A second one would make
	 * uart_poll_out spin on TXE, a wait the PR does not bound and this report is not about.
	 */
	r->sent1 = wait_sr(USART_SR_TXE, k_ms_to_cyc_ceil32(TXE_WAIT_MS), &at);
	if (r->sent1 == 1U) {
		clear_tc();
		uart_poll_out(usart1, 0x55);
	}
	k_busy_wait(SETTLE_US);
	r->sr_pre = USART1->SR;

	k_timer_start(&release_timer, K_MSEC(RELEASE_MS), K_NO_WAIT);
	k_timer_start(&rescue_timer, K_MSEC(RESCUE_MS), K_NO_WAIT);

	/* Both items are queued before the queue can run either of them, so the suspend runs
	 * first and the progress item behind it, on both trees; t0 is stamped between the submits
	 * and the unlock, which is why start1 is a bookkeeping delay and nothing else.
	 */
	k_sched_lock();
	k_work_submit(&suspend_work);
	k_work_submit(&progress_work);
	t0 = k_cycle_get_32();
	k_sched_unlock();

	first = k_sem_take(&suspend_done, K_MSEC(WORK_WAIT_MS));
	second = k_sem_take(&progress_done, K_MSEC(WORK_WAIT_MS));
	r->waited = (first != 0 || second != 0) ? 1U : 0U;

	if (suspend_state[0] == (int)PM_DEVICE_STATE_ACTIVE) {
		/* The suspend gave up and the PM core left the device active, so its clock is on
		 * and the held frame is still in the transmitter: wait for the release interrupt
		 * to let it out, and keep the registers the failed suspend left behind.
		 */
		r->sr1 = failed_sr;
		r->pending_tc = wait_sr_until(USART_SR_TC, k_ms_to_cyc_ceil32(PENDING_MS), &at);
		if (r->pending_tc == 1U) {
			r->pending = at - t0;
		}
		wait_released(k_ms_to_cyc_ceil32(PENDING_MS));
		/* A settling time after the flip, as at the start of the repetition: an unconnected
		 * pin follows its own pull resistor over a microsecond or so, and the level, not the
		 * register, is what this field reports.
		 */
		k_busy_wait(SETTLE_US);
		r->pin_released = cts_level();
	} else {
		/* The suspend returned with the device suspended and its clock off, so CTS is
		 * sampled first (the resume re-applies the pinctrl state, and with it PA11's
		 * pull-up) and no USART register is read before the resume.
		 */
		wait_released(k_ms_to_cyc_ceil32(PENDING_MS));
		k_busy_wait(SETTLE_US);
		r->pin_released = cts_level();
		r->resumed1 = 1U;
		r->resume1_rc = pm_device_action_run(usart1, PM_DEVICE_ACTION_RESUME);
		r->sr1 = USART1->SR;
		cts_pull(PULL_DOWN);
		r->pending_tc = wait_sr(USART_SR_TC, k_ms_to_cyc_ceil32(TC_WAIT_MS), &at);
		if (r->pending_tc == 1U) {
			r->pending = at - t0;
		}
	}

	k_timer_stop(&rescue_timer);

	/* One regular byte with CTS asserted: the transmitter has to work again. */
	r->sent2 = wait_sr(USART_SR_TXE, k_ms_to_cyc_ceil32(TXE_WAIT_MS), &at);
	if (r->sent2 == 1U) {
		clear_tc();
		write_at = k_cycle_get_32();
		uart_poll_out(usart1, 0xAA);
		r->regular_tc = wait_sr(USART_SR_TC, k_ms_to_cyc_ceil32(TC_WAIT_MS), &at);
		if (r->regular_tc == 1U) {
			r->regular_cycles = at - write_at;
		}
	}

	/* And the suspend after the release, on the same queue, with nothing left to wait for. */
	slot = 1U;
	k_timer_start(&rescue_timer, K_MSEC(RESCUE_MS), K_NO_WAIT);
	k_work_submit(&suspend_work);
	if (k_sem_take(&suspend_done, K_MSEC(WORK_WAIT_MS)) != 0) {
		r->waited = 1U;
	}
	k_timer_stop(&rescue_timer);

	if (suspend_state[1] == (int)PM_DEVICE_STATE_SUSPENDED) {
		r->resumed2 = 1U;
		/* The resume re-applies the pinctrl state, so PA11 carries its pull-up again and
		 * the next repetition starts with CTS held.
		 */
		r->resume2_rc = pm_device_action_run(usart1, PM_DEVICE_ACTION_RESUME);
	}
}

int main(void)
{
	struct repetition record;
	uint32_t n;

	if (!device_is_ready(usart1)) {
		/* Untagged, so the record parser ignores it and a reader of the serial log still
		 * sees it. Without a done line the gate fails this image as a whole, which is what
		 * a USART that did not come up deserves.
		 */
		printk("hq172: usart1 is not ready\n");
		for (;;) {
			k_busy_wait(SETTLE_US);
		}
	}

	k_sem_init(&suspend_done, 0, 1);
	k_sem_init(&progress_done, 0, 1);
	k_work_init(&suspend_work, suspend_handler);
	k_work_init(&progress_work, progress_handler);
	k_timer_init(&release_timer, release_expiry, NULL);
	k_timer_init(&rescue_timer, rescue_expiry, NULL);

	print_build();
	cts_control();

	for (n = 0U; n < N_CONTROL + N_HELD; n++) {
		run_repetition(n, &record);
		print_repetition(&record);
	}

	printk(TAG " done\n");
	for (;;) {
		/* Not k_cpu_idle(): the probe has to reach a running core for the next flash. */
		k_busy_wait(SETTLE_US);
	}
}
