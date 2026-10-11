/*
 * Copyright (c) 2026 the hq179 job
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * The in-chip measurement path of hq179, and nothing of the driver under test.
 *
 * Nothing is wired to the board. DMA2 stream 7 copies the 16-bit GPIOC input
 * data register into RAM on every TIM8 CC4 event, one sample every
 * HQ179_SAMPLE_CLOCKS timer clocks, for HQ179_SAMPLES samples. The driver
 * drives PC0 (and PC1 for the second instance) by writing the same port's BSRR,
 * and a TIM3 PWM of known period and duty comes out of PC6, so the control and
 * the pin under test lie in the very same samples.
 *
 * These functions only measure and reduce; every verdict is the gate's.
 */

#ifndef HQ179_SAMPLER_H_
#define HQ179_SAMPLER_H_

#include <stdint.h>

/* 20000 samples of 8 timer clocks are 160000 clocks (1.667 ms): the longest
 * frame of this job (512 ticks of 193 clocks = 98816) plus a tail of more than
 * 60000 clocks, so a frame always ends well inside the window. 8 clocks is
 * shorter than the shortest judged pulse (one tick of 12 clocks at 8 MHz).
 */
#define HQ179_SAMPLES       20000u
#define HQ179_SAMPLE_CLOCKS 8u
/* Inner run lengths kept per window; the run count itself is always complete.
 * The frames of this job show at most 23 runs between the resting levels.
 */
#define HQ179_RUNS_KEPT 64u
/* The control: TIM3 CH1 on PC6, 159 clocks per period (prime, so the 8-clock
 * samples sweep every phase of it) with 40 clocks high and 119 low.
 */
#define HQ179_PWM_ARR 158u
#define HQ179_PWM_CCR 40u

/* The bits of GPIOC the window is read for. */
#define HQ179_BIT_PC0 0u
#define HQ179_BIT_PC1 1u
#define HQ179_BIT_PC6 6u

/** What the sampling stream itself reported about one window. */
struct hq179_window {
	/* DMA2_HISR of stream 7, shifted down to bit 0 and masked to the five
	 * stream flags: transfer complete, half transfer, and the FIFO, direct
	 * mode and transfer errors.
	 */
	uint32_t flags;
	uint32_t ndtr; /* transfers left; 0 when the whole window was recorded */
	uint32_t dur;  /* timer clocks from the window's start to transfer complete */
};

/** One bit of the port, run-length encoded over the whole window. */
struct hq179_runs {
	uint32_t nruns;     /* every run, the two at the window's ends included */
	uint32_t first;     /* level of the run the window begins in */
	uint32_t first_len; /* its length in samples */
	uint32_t last;      /* level of the run the window ends in */
	uint32_t last_len;
	uint32_t inner_count; /* min(nruns - 2, HQ179_RUNS_KEPT) */
	uint32_t inner[HQ179_RUNS_KEPT];
};

/** The control PWM as the same samples show it; lengths in samples. */
struct hq179_pwm {
	uint32_t edges; /* rising edges: a low sample followed by a high one */
	uint32_t first; /* sample index of the first rising edge */
	uint32_t last;  /* sample index of the last rising edge */
	uint32_t hi_min;
	uint32_t hi_max;
	uint32_t lo_min;
	uint32_t lo_max;
	uint32_t per_min; /* rising edge to rising edge */
	uint32_t per_max;
};

/** Clocks, the control PWM on PC6 and the pad slew rates; once per start. */
void hq179_sampler_setup(void);

/** Point the stream at a fresh window and shift the sampling grid by rep % 8. */
void hq179_sampler_arm(uint32_t rep);

/** Start the sampling timer and take the window's time origin. */
void hq179_sampler_start(void);

/** Timer clocks since that origin. */
uint32_t hq179_elapsed(void);

/** The DMA2 priority level fields of the driver's stream 5 and the sampler's 7. */
void hq179_sampler_priorities(uint32_t *driver_pl, uint32_t *sampler_pl);

/** Wait for the window to complete, then stop sampling. */
void hq179_sampler_wait(struct hq179_window *out);

/** Reduce the recorded window: one bit, run-length encoded. */
void hq179_runs_of(uint32_t bit, struct hq179_runs *out);

/** Reduce the recorded window: the control PWM's runs and periods. */
void hq179_pwm_of(uint32_t bit, struct hq179_pwm *out);

/** One bit of GPIOC_IDR, read now. */
uint32_t hq179_idr_bit(uint32_t bit);

/** Drive one GPIOC pin through BSRR, as the driver does. */
void hq179_drive(uint32_t pin, uint32_t level);

/** The MODER and OSPEEDR fields of one GPIOC pin. */
uint32_t hq179_moder(uint32_t pin);
uint32_t hq179_ospeedr(uint32_t pin);

/** Busy-wait a number of timer clocks (short waits, no scheduling). */
void hq179_spin(uint32_t clocks);

#endif /* HQ179_SAMPLER_H_ */
