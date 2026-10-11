/*
 * Copyright (c) 2026 the hq179 job
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * The measurement path: TIM8 CC4 -> DMA2 stream 7 -> GPIOC_IDR into RAM, with a
 * TIM3 PWM of known period and duty on PC6 inside every window as its control.
 *
 * TIM8 has no devicetree node on the F446 under Zephyr and TIM3 none that is
 * enabled, so both are driven through their registers here; stream 7 of DMA2 is
 * claimed the same way. The measurement must yield to the product: the sampling
 * stream is programmed at the lowest priority level and is the higher-numbered
 * of the two, so the driver's stream 5 wins every arbitration tie. The image
 * reads both priority fields back and the gate asserts them.
 *
 * The stream raises no interrupt (no enable bit is set in its control register
 * and the FIFO error interrupt stays off), so Zephyr's DMA driver never sees an
 * interrupt for a stream it did not configure and cannot clear the flags this
 * window is judged by. The flags still latch in DMA2_HISR, and the image reads
 * them once the window must be over: while it is being sampled the processor
 * waits asleep and makes no bus access of its own (hq179_sampler_wait).
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/util.h>

#include <soc.h>

#include "sampler.h"

/* RCC peripheral clock enables (RM0390 6.3). GPIOC and DMA2 are enabled by
 * Zephyr's own drivers as well; writing them again is idempotent and says what
 * this file depends on. TIM3 and TIM8 have no enabled devicetree node, so
 * nothing else turns them on.
 */
#define HQ_AHB1_GPIOC BIT(2)
#define HQ_AHB1_DMA2  BIT(22)
#define HQ_APB1_TIM3  BIT(1)
#define HQ_APB2_TIM8  BIT(1)

/* Timer bits (RM0390 17.4, 18.4). */
#define HQ_TIM_CR1_CEN   BIT(0)
#define HQ_TIM_EGR_UG    BIT(0)
#define HQ_TIM_CCER_CC1E BIT(0)
#define HQ_TIM_DIER_CC4DE BIT(12)
/* PWM mode 1 on channel 1 (OC1M = 110) with its preload enabled (OC1PE). */
#define HQ_TIM_CCMR1_PWM1 ((6u << 4) | BIT(3))
/* MMS = 001: the timer's counter-enable signal is what its trigger output
 * carries. Nothing else of CR2 is used by either timer here.
 */
#define HQ_TIM_CR2_MMS_ENABLE (1u << 4)
/* SMS = 110 (trigger mode) with TS = 000 (internal trigger 0, which is TIM1 for
 * TIM8 in RM0390's internal trigger connection table): a rising edge of that
 * trigger starts the counter and only starts it. The counter is not
 * reinitialised, by that edge or by any later one, and it keeps its own clock,
 * so one window is sampled on one uninterrupted cadence.
 */
#define HQ_TIM_SMCR_TRIGGER_ITR0 6u

/* DMA stream bits (RM0390 9.5). Peripheral to memory, 16-bit both sides,
 * memory incrementing, channel 7 (TIM8_CH4/TRIG/COM on stream 7), priority
 * level 0 (the lowest), no interrupt enable of any kind.
 */
#define HQ_DMA_CR_EN      BIT(0)
#define HQ_DMA_CR_MINC    BIT(10)
#define HQ_DMA_CR_PSIZE16 (1u << 11)
#define HQ_DMA_CR_MSIZE16 (1u << 13)
#define HQ_DMA_CR_CHSEL7  (7u << 25)
#define HQ_DMA_CR_PL_SHIFT 16u
#define HQ_SAMPLER_CR \
	(HQ_DMA_CR_CHSEL7 | HQ_DMA_CR_MSIZE16 | HQ_DMA_CR_PSIZE16 | HQ_DMA_CR_MINC)

/* The five per-stream flags of DMA2_HISR, and where stream 7's group starts. */
#define HQ_DMA_FLAGS       0x3Du
#define HQ_DMA_TCIF        0x20u
#define HQ_STREAM7_SHIFT   22u
/* The window's own length in timer clocks, and the clocks per microsecond of
 * the 96 MHz the image prints as sysclk.
 */
#define HQ_WINDOW_CLOCKS (HQ179_SAMPLES * HQ179_SAMPLE_CLOCKS)
#define HQ_CLOCKS_PER_US 96u
/* Added to the sleep that waits a window out: one kernel tick of 100 us, which
 * a sleep of a given length may round up by, and the time between the window's
 * origin and its first sample - the transmit's own setup, 512 encoded words and
 * one DMA configuration, before the frame starts the sampling counter. A sleep
 * that still ends early only costs another one in the loop below.
 */
#define HQ_SLEEP_SLACK_US 300u
/* Three window lengths: long enough that a completed transfer is never missed,
 * short enough that a stream which never completes does not stall the image.
 */
#define HQ_WAIT_CLOCKS (3u * HQ_WINDOW_CLOCKS)

static uint16_t samples[HQ179_SAMPLES];
static uint32_t window_t0;

static uint32_t field(uint32_t value, uint32_t pin, uint32_t width)
{
	return (value >> (width * pin)) & ((1u << width) - 1u);
}

static void set_field(volatile uint32_t *reg, uint32_t pin, uint32_t width, uint32_t value)
{
	uint32_t mask = ((1u << width) - 1u) << (width * pin);

	*reg = (*reg & ~mask) | ((value << (width * pin)) & mask);
}

static uint32_t sample_bit(uint32_t index, uint32_t bit)
{
	return (uint32_t)(samples[index] >> bit) & 1u;
}

void hq179_spin(uint32_t clocks)
{
	uint32_t start = k_cycle_get_32();

	while (k_cycle_get_32() - start < clocks) {
	}
}

uint32_t hq179_idr_bit(uint32_t bit)
{
	return (GPIOC->IDR >> bit) & 1u;
}

void hq179_drive(uint32_t pin, uint32_t level)
{
	GPIOC->BSRR = level != 0u ? BIT(pin) : BIT(pin) << 16;
}

uint32_t hq179_moder(uint32_t pin)
{
	return field(GPIOC->MODER, pin, 2u);
}

uint32_t hq179_ospeedr(uint32_t pin)
{
	return field(GPIOC->OSPEEDR, pin, 2u);
}

/* Tie the sampling grid to the frame: the driver's counter-enable signal starts
 * the sampling counter.
 *
 * One sample every 8 clocks measures a pulse of 12 or 13 clocks as one or two
 * samples, depending only on where the pulse falls between two samples, so the
 * mean over the 20 repetitions resolves below one sample only while that phase
 * moves from repetition to repetition.
 *
 * Two earlier versions of this image moved the phase before the frame started -
 * the first in the sampling counter, the second in the compare value plus a
 * wait on the sampling counter - and both measured every run of a frame as the
 * same number of samples in all 20 repetitions of both reset phases, which is
 * what a grid that never moves relative to the frame does. Anything that ties
 * the processor's timing to the sampling stream between such a shift and the
 * frame takes the shift back: the stream writes its samples into the same SRAM
 * that the driver's encoder writes its 512 words into, and a loop that is held
 * up by one clock whenever the two coincide settles at a fixed position in the
 * grid and carries the frame start with it.
 *
 * So the phase is taken from the driver's own timer, after any such loop has
 * run. TIM1's trigger output carries its counter-enable signal and that trigger
 * starts TIM8, from the count of 0 that arming leaves it at, so a frame begins
 * at count 0 of the sampling counter and the window's first sample falls CCR4
 * clocks later, which is where hq179_sampler_arm() puts this repetition's phase.
 *
 * The trigger starts the counter and never reinitialises it, and that is what
 * keeps every sample of a window exactly 8 clocks from the one before it. A
 * counter the trigger reinitialises - reset mode, which an earlier version of
 * this image used - moves the grid's phase in the middle of the window instead,
 * and the single interval across that step is shorter than 8 clocks whenever
 * the new phase precedes the old one. A short interval fits one more sample
 * into whichever run of the control PWM it falls in, and that is a sample the
 * control cannot have: 40 clocks of high time hold five samples of an 8-clock
 * grid, and a sixth only because a sample may be read late. Two bench runs of
 * these images reported 7 samples of high time and 17 of low that way, which is
 * the measurement and not the driver. Starting the counter leaves the step
 * before the first sample of the window, where there is nothing measured yet.
 *
 * Writing MMS selects what the driver's timer puts on its trigger output and
 * nothing else: no count, no update event, no DMA request and nothing on the
 * pin changes, and the driver itself never writes CR2 (the reference copy of
 * the driver under reference/hq179/ writes CR1, PSC, ARR, RCR, DIER and EGR).
 */
static void grid_trigger(void)
{
	TIM1->CR2 = HQ_TIM_CR2_MMS_ENABLE;
	TIM8->SMCR = HQ_TIM_SMCR_TRIGGER_ITR0;
}

void hq179_grid_trigger_regs(uint32_t *driver_cr2, uint32_t *sampler_smcr)
{
	*driver_cr2 = TIM1->CR2;
	*sampler_smcr = TIM8->SMCR;
}

void hq179_sampler_setup(void)
{
	RCC->AHB1ENR |= HQ_AHB1_GPIOC | HQ_AHB1_DMA2;
	RCC->APB1ENR |= HQ_APB1_TIM3;
	RCC->APB2ENR |= HQ_APB2_TIM8;
	(void)RCC->APB2ENR; /* the write must have reached the bus before the first access */

	/* The pads: PC6 carries TIM3_CH1 (alternate function 2), and all three
	 * pins get the fast slew rate. How fast a pad rises is the integrator's
	 * setting, not the driver's, so it is set here and printed.
	 */
	set_field(&GPIOC->AFR[0], HQ179_BIT_PC6, 4u, 2u);
	set_field(&GPIOC->MODER, HQ179_BIT_PC6, 2u, 2u);
	set_field(&GPIOC->OSPEEDR, HQ179_BIT_PC0, 2u, 2u);
	set_field(&GPIOC->OSPEEDR, HQ179_BIT_PC1, 2u, 2u);
	set_field(&GPIOC->OSPEEDR, HQ179_BIT_PC6, 2u, 2u);

	/* The control, running from here until the done line: 159 clocks per
	 * period with 40 high, read back from these registers by the gate.
	 */
	TIM3->CR1 = 0u;
	TIM3->PSC = 0u;
	TIM3->ARR = HQ179_PWM_ARR;
	TIM3->CCR1 = HQ179_PWM_CCR;
	TIM3->CCMR1 = HQ_TIM_CCMR1_PWM1;
	TIM3->CCER = HQ_TIM_CCER_CC1E;
	TIM3->EGR = HQ_TIM_EGR_UG;
	TIM3->SR = 0u;
	TIM3->CR1 = HQ_TIM_CR1_CEN;

	/* Set here as well as in every window, so the start can print the two
	 * registers the grid's phase rests on.
	 */
	grid_trigger();
}

void hq179_sampler_arm(uint32_t rep)
{
	/* A pattern no port read can produce for these three pins, so a sample
	 * the stream never wrote cannot pass for one it did.
	 */
	for (uint32_t index = 0u; index < HQ179_SAMPLES; index++) {
		samples[index] = 0xFFFFu;
	}

	DMA2_Stream7->CR = 0u;
	while ((DMA2_Stream7->CR & HQ_DMA_CR_EN) != 0u) {
	}
	DMA2->HIFCR = HQ_DMA_FLAGS << HQ_STREAM7_SHIFT;
	DMA2_Stream7->PAR = (uint32_t)(uintptr_t)&GPIOC->IDR;
	DMA2_Stream7->M0AR = (uint32_t)(uintptr_t)samples;
	DMA2_Stream7->NDTR = HQ179_SAMPLES;
	DMA2_Stream7->FCR = 0u; /* direct mode: no FIFO, no FIFO error interrupt */
	DMA2_Stream7->CR = HQ_SAMPLER_CR;
	DMA2_Stream7->CR |= HQ_DMA_CR_EN;

	/* One CC4 event every HQ179_SAMPLE_CLOCKS clocks, the first of them CCR4
	 * clocks after the counter starts, which grid_trigger() above has the
	 * driver's counter enable do at the start of a frame. So CCR4 is this
	 * repetition's phase of the grid: seven values for eight phases, three
	 * repetitions each and two for the seventh, which holds the quantisation
	 * left in the mean of 20 repetitions under 1.5 clocks. It stays between 1
	 * and ARR: 0 would leave it to the silicon whether a match at the
	 * counter's reload is an event at all, and a value above ARR would be no
	 * event ever. CR2 keeps CCDS at 0, so the request follows the compare
	 * event and not the update event.
	 */
	TIM8->CR1 = 0u;
	TIM8->DIER = 0u;
	TIM8->CR2 = 0u;
	TIM8->PSC = 0u;
	TIM8->ARR = HQ179_SAMPLE_CLOCKS - 1u;
	TIM8->CCR4 = 1u + rep % (HQ179_SAMPLE_CLOCKS - 1u);
	TIM8->CCMR2 = 0u; /* channel 4 an output, compare mode frozen */
	TIM8->CCER = 0u;  /* the pin is not driven; only the compare event is wanted */
	/* The trigger again in every window: the driver reconfigures its timer
	 * before each one, and this image does not depend on what that leaves
	 * in the two registers the grid's phase rests on.
	 */
	grid_trigger();
	/* Reloads the prescaler and leaves the counter at 0, so the first sample
	 * of the window falls CCR4 clocks after the trigger starts it. The
	 * counter is left stopped here: CR1 above cleared its enable, and a
	 * trigger edge from a transfer that runs between two windows can start it
	 * again, which is why arming clears it rather than assuming it.
	 */
	TIM8->EGR = HQ_TIM_EGR_UG;
	TIM8->SR = 0u;
	TIM8->DIER = HQ_TIM_DIER_CC4DE;
}

void hq179_sampler_start(void)
{
	/* The window's time origin, taken before the call under test. The
	 * sampling counter is not enabled here: the driver's own counter enable
	 * starts it at the frame's start (grid_trigger above), and
	 * hq179_sampler_wait() starts it for a window no frame ever began in.
	 */
	window_t0 = k_cycle_get_32();
}

uint32_t hq179_elapsed(void)
{
	return k_cycle_get_32() - window_t0;
}

void hq179_sampler_priorities(uint32_t *driver_pl, uint32_t *sampler_pl)
{
	*driver_pl = (DMA2_Stream5->CR >> HQ_DMA_CR_PL_SHIFT) & 3u;
	*sampler_pl = (DMA2_Stream7->CR >> HQ_DMA_CR_PL_SHIFT) & 3u;
}

void hq179_sampler_wait(struct hq179_window *out)
{
	uint32_t flags;
	uint32_t elapsed = hq179_elapsed();
	uint32_t left = HQ_WINDOW_CLOCKS;

	/* A window no frame ever began in: no counter-enable edge came, so
	 * nothing started the sampling counter, and the grid starts here instead.
	 * The qualification case makes no transfer at all, and a request the
	 * driver refuses before it reaches its timer - the instance without a
	 * buffer-size, on the driver as the PR has it - never enables that
	 * counter either. Such a window records the resting level of the line and
	 * the control PWM, which is exactly what those two cases are read for.
	 *
	 * There is no frame for the grid to be tied to here, so the counter is
	 * taken out of slave mode before it is started: a counter started by
	 * software while a trigger may still set the same bit is one more thing
	 * to reason about, and the phase of a window with no frame in it is of no
	 * consequence. The cadence is the same 8 clocks either way.
	 */
	out->self_started = 0u;
	if ((TIM8->CR1 & HQ_TIM_CR1_CEN) == 0u) {
		TIM8->SMCR = 0u;
		TIM8->CR1 = HQ_TIM_CR1_CEN;
		out->self_started = 1u;
	} else {
		left = elapsed < HQ_WINDOW_CLOCKS ? HQ_WINDOW_CLOCKS - elapsed : 0u;
	}

	/* Wait the window out asleep, and only then read the stream.
	 *
	 * Nothing the processor does can make the window shorter: it is
	 * HQ_WINDOW_CLOCKS long by construction. A loop that polls the stream's
	 * flags or the system timer meanwhile puts a processor access between two
	 * samples every few clocks, and an access that outlasts the slack one
	 * sample leaves on the buses delays the next sample's read of the port. A
	 * sample read late crosses whatever edge fell in between and is counted in
	 * the next run instead of its own, which is a whole sample of error in a
	 * control whose 40-clock high time is five samples. The window has room for
	 * one such sample and no more, and that room belongs to the driver's own
	 * traffic, which a window must tolerate because it is the thing being
	 * measured.
	 *
	 * Asleep there is no such access: Zephyr's idle thread halts the core, and
	 * the only accesses left in a window are the driver's own and the interrupt
	 * its transfer completes in.
	 */
	if (left != 0u) {
		k_sleep(K_USEC(left / HQ_CLOCKS_PER_US + HQ_SLEEP_SLACK_US));
	}
	for (;;) {
		flags = (DMA2->HISR >> HQ_STREAM7_SHIFT) & HQ_DMA_FLAGS;
		if ((flags & HQ_DMA_TCIF) != 0u) {
			break;
		}
		if (hq179_elapsed() >= HQ_WAIT_CLOCKS) {
			break;
		}
		/* The window is over by now; a stream that has not finished is
		 * waited for in whole kernel ticks, asleep as well.
		 */
		k_msleep(1);
	}
	out->dur = hq179_elapsed();
	out->flags = (DMA2->HISR >> HQ_STREAM7_SHIFT) & HQ_DMA_FLAGS;
	out->ndtr = DMA2_Stream7->NDTR;

	TIM8->CR1 = 0u;
	TIM8->DIER = 0u;
	DMA2_Stream7->CR = 0u;
	while ((DMA2_Stream7->CR & HQ_DMA_CR_EN) != 0u) {
	}
}

void hq179_runs_of(uint32_t bit, struct hq179_runs *out)
{
	uint32_t level = sample_bit(0u, bit);
	uint32_t start = 0u;
	uint32_t closed = 0u; /* runs the window ended inside, so not the last one */

	for (uint32_t index = 0u; index < HQ179_RUNS_KEPT; index++) {
		out->inner[index] = 0u;
	}
	out->first = level;
	out->first_len = HQ179_SAMPLES;

	for (uint32_t index = 1u; index < HQ179_SAMPLES; index++) {
		uint32_t next = sample_bit(index, bit);

		if (next == level) {
			continue;
		}
		if (closed == 0u) {
			out->first_len = index - start;
		} else if (closed - 1u < HQ179_RUNS_KEPT) {
			/* Run number `closed`, which is an inner run unless the
			 * window ends in the next one; inner_count below drops it
			 * in that case.
			 */
			out->inner[closed - 1u] = index - start;
		}
		closed++;
		start = index;
		level = next;
	}

	out->nruns = closed + 1u;
	out->last = level;
	out->last_len = HQ179_SAMPLES - start;
	out->inner_count = out->nruns > 2u ? MIN(out->nruns - 2u, HQ179_RUNS_KEPT) : 0u;
}

void hq179_pwm_of(uint32_t bit, struct hq179_pwm *out)
{
	uint32_t level = sample_bit(0u, bit);
	uint32_t start = 0u;
	uint32_t previous_rise = 0u;

	out->edges = 0u;
	out->first = 0u;
	out->last = 0u;
	out->hi_min = UINT32_MAX;
	out->hi_max = 0u;
	out->lo_min = UINT32_MAX;
	out->lo_max = 0u;
	out->per_min = UINT32_MAX;
	out->per_max = 0u;

	for (uint32_t index = 1u; index < HQ179_SAMPLES; index++) {
		uint32_t next = sample_bit(index, bit);

		if (next == level) {
			continue;
		}
		if (start != 0u) {
			/* A run that both began and ended on an edge this window
			 * recorded. The partial run at each end of the window is
			 * not a measurement of the control's high or low time.
			 */
			uint32_t length = index - start;

			if (level != 0u) {
				out->hi_min = MIN(out->hi_min, length);
				out->hi_max = MAX(out->hi_max, length);
			} else {
				out->lo_min = MIN(out->lo_min, length);
				out->lo_max = MAX(out->lo_max, length);
			}
		}
		if (next != 0u) {
			out->edges++;
			if (out->edges == 1u) {
				out->first = index;
			} else {
				uint32_t period = index - previous_rise;

				out->per_min = MIN(out->per_min, period);
				out->per_max = MAX(out->per_max, period);
			}
			previous_rise = index;
			out->last = index;
		}
		start = index;
		level = next;
	}

	/* Nothing measured stays 0 rather than the sentinel, so the gate reports
	 * a control that did not run instead of an implausible length.
	 */
	if (out->hi_max == 0u) {
		out->hi_min = 0u;
	}
	if (out->lo_max == 0u) {
		out->lo_min = 0u;
	}
	if (out->per_max == 0u) {
		out->per_min = 0u;
	}
}
