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
 * window is judged by. The flags still latch in DMA2_HISR, which is polled.
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
/* Three window lengths: long enough that a completed transfer is never missed,
 * short enough that a stream which never completes does not stall the image.
 */
#define HQ_WAIT_CLOCKS (3u * HQ179_SAMPLES * HQ179_SAMPLE_CLOCKS)

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

	/* One CC4 event every HQ179_SAMPLE_CLOCKS clocks. The counter is preset
	 * to rep % HQ179_SAMPLE_CLOCKS, so the sampling grid moves by one clock
	 * per repetition and the mean over the repetitions resolves below one
	 * sample. CR2 keeps CCDS at 0, so the request follows the compare event
	 * and not the update event.
	 */
	TIM8->CR1 = 0u;
	TIM8->DIER = 0u;
	TIM8->CR2 = 0u;
	TIM8->PSC = 0u;
	TIM8->ARR = HQ179_SAMPLE_CLOCKS - 1u;
	TIM8->CCR4 = HQ179_SAMPLE_CLOCKS - 1u;
	TIM8->CCMR2 = 0u; /* channel 4 an output, compare mode frozen */
	TIM8->CCER = 0u;  /* the pin is not driven; only the compare event is wanted */
	TIM8->EGR = HQ_TIM_EGR_UG;
	TIM8->CNT = rep % HQ179_SAMPLE_CLOCKS;
	TIM8->SR = 0u;
	TIM8->DIER = HQ_TIM_DIER_CC4DE;
}

void hq179_sampler_start(void)
{
	window_t0 = k_cycle_get_32();
	TIM8->CR1 = HQ_TIM_CR1_CEN;
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

	for (;;) {
		flags = (DMA2->HISR >> HQ_STREAM7_SHIFT) & HQ_DMA_FLAGS;
		if ((flags & HQ_DMA_TCIF) != 0u) {
			break;
		}
		if (hq179_elapsed() >= HQ_WAIT_CLOCKS) {
			break;
		}
		/* Reading the system timer costs the AHB1 bus nothing, so the
		 * stream keeps the bus to itself between two polls.
		 */
		k_busy_wait(10);
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
