/*
 * Copyright (c) 2026 the hq179 job
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * hq179: what Zephyr PR #119508's Pulse IO STM32 timer-DMA backend puts on a
 * pin of a NUCLEO-F446RE.
 *
 * Twelve cases run 20 repetitions each, and all of them run again after the
 * image's own software reset. Every case prints raw numbers only: the return
 * codes the API gave, the prescaler and reload value the driver programmed, the
 * levels read out of the port, and the run lengths of the measured window in
 * samples. No line here says whether a number is right; tests/hq179 computes
 * every verdict from first principles or from the control measured in the same
 * window (src/sampler.c).
 *
 * Nothing is wired to the board and no LED is attached, so this image says
 * nothing about LEDs: it reports timing relative to the MCU's own clock on the
 * four resolutions it measures.
 */

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/pulse_io.h>
#include <zephyr/kernel.h>
#include <zephyr/linker/section_tags.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/util.h>
#include <zephyr/version.h>

#include <soc.h>

#include "sampler.h"

#define TAG "HQ179"

#define NREP   20u /* repetitions per case per reset phase */
#define PERIOD 64u /* cell period in ticks */
#define CELLS  8u  /* cells per frame */

/* The port is left idle for this long after every record line.
 *
 * The two starts print some 300 kB in all, which at 115200 baud is around 26
 * seconds of traffic with no gap in it, and a reader that stops reading for a
 * moment loses whatever the port sent meanwhile. Two bench runs of this image
 * lost a few kilobytes in the middle of a case that way: once a line arrived
 * cut in two, once whole repetitions were missing between two intact lines.
 * These gaps hold the port at about a quarter of its capacity, so a reader that
 * stalls for a whole second has under 3 kB to catch up with instead of 11 kB,
 * and they still leave the image printing its done line after about 110 of the
 * 180 seconds the plan waits for it.
 */
#define PACE_MS 75

/* The resolutions measured. Each one divides 96 MHz exactly, so the API's
 * "nearest achievable" tick rate is the requested rate.
 */
#define RES_500K 500000u
#define RES_2M   2000000u
#define RES_6M   6000000u
#define RES_8M   8000000u

/* D covers duty 0, 1, period-1, period and above period (80 clamps to 64) in
 * one frame and ends with a duty-equals-period cell, so its last tick is
 * visible against a low idle level.
 */
static const uint16_t FRAME_D[CELLS] = {32, 1, 63, 0, 56, 8, 80, 64};
/* 512 words: exactly the buffer-size the overlay asks for. */
static const uint16_t FRAME_F[CELLS] = {64, 64, 64, 64, 64, 64, 64, 64};
/* One cell more than the buffer holds. */
static const uint16_t FRAME_F9[CELLS + 1u] = {64, 64, 64, 64, 64, 64, 64, 64, 64};
/* H begins and ends with a duty-0 cell, so its first and last tick are visible
 * against a high idle level.
 */
static const uint16_t FRAME_H[CELLS] = {0, 32, 1, 63, 56, 8, 80, 0};
/* Run before the idle-high frame: it ends low and its own idle level is low, so
 * the line rests low on both images before the high idle level is configured.
 */
static const uint16_t FRAME_P[2] = {32, 0};

/* State carried across a reset, used only after a reset this image caused: the
 * marker is armed in the last instructions before that reset and invalidated at
 * every start before anything else runs.
 */
#define MARKER_MAGIC 0x48513179u
static struct {
	uint32_t magic;
	uint32_t armed;
} marker __noinit;

/* RCC_CSR (RM0390 6.3.21): SFTRSTF (28) set, and BORRSTF (25), PORRSTF (27),
 * IWDGRSTF (29), WWDGRSTF (30) and LPWRRSTF (31) clear. PINRSTF (26) is ignored
 * because a system reset also drives NRST on the F4, so it says nothing about
 * who asked for the reset. RMVF (24) clears the flags for the next start.
 */
#define CSR_MASK       0xFA000000u
#define CSR_CLEAN_SOFT 0x10000000u
#define CSR_RMVF       BIT(24)

#define USART_SR_TC   BIT(6) /* transmission complete: the shift register is empty */
#define TC_WAIT_CYCLES (5u * 96000u) /* 5 ms, far longer than one character takes */

#ifdef BUILD_VERSION
#define HQ_ZEPHYR_REVISION STRINGIFY(BUILD_VERSION)
#else
#define HQ_ZEPHYR_REVISION ""
#endif

static const struct device *const dev0 = DEVICE_DT_GET(DT_NODELABEL(pulseio0));
static const struct device *const dev1 = DEVICE_DT_GET(DT_NODELABEL(pulseio1));
static struct pulse_io_channel *chan0;

static struct pulse_cell cells[CELLS + 1u];
static struct pulse_io_tx_req req;

/* The thread that aborts a transfer with stop() while the frame is on the wire.
 * It runs at a lower priority than main, so it starts its wait exactly when
 * main blocks inside the transmit.
 */
static K_SEM_DEFINE(stopper_go, 0, 1);
static K_SEM_DEFINE(stopper_done, 0, 1);
static volatile uint32_t stop_at;
static volatile uint32_t idle_stop;

/** One repetition of one window, as the image prints it; -1 means "not this case". */
struct rec {
	const char *name;
	uint32_t rep;
	uint32_t win;
	int32_t res;
	int32_t psc;
	int32_t arr;
	int32_t rc;
	int32_t rc2;
	int32_t rc_cfg;
	int32_t ret;
	int32_t done_rc;
	int32_t idle_cfg;
	int32_t idle_end;
	int32_t idle_stop;
	int32_t stop_at;
	int32_t s5_pl;
	int32_t s7_pl;
	int32_t flags;
	int32_t ndtr;
	int32_t dur;
	int32_t nruns;
	int32_t first;
	int32_t first_len;
	int32_t last;
	int32_t last_len;
	int32_t pwm_edges;
	int32_t pwm_first;
	int32_t pwm_last;
	int32_t pwm_hi_min;
	int32_t pwm_hi_max;
	int32_t pwm_lo_min;
	int32_t pwm_lo_max;
	int32_t pwm_per_min;
	int32_t pwm_per_max;
	int32_t pc0_set;
	int32_t pc0_rst;
	uint32_t inner_count;
	uint32_t inner[HQ179_RUNS_KEPT];
};

static const char *zephyr_revision(void)
{
	static const char revision[] = HQ_ZEPHYR_REVISION;

	/* A record value may not be empty, so a tree git describe said nothing
	 * about is reported as such instead of producing an unparseable line.
	 */
	return revision[0] != '\0' ? revision : "none";
}

static struct rec rec_init(const char *name, uint32_t rep, uint32_t win)
{
	struct rec r = {
		.name = name,
		.rep = rep,
		.win = win,
		/* Return codes that do not apply are 0, measurements that do not
		 * apply are -1, which no level, index or cycle count can be.
		 */
		.res = -1,
		.psc = -1,
		.arr = -1,
		.ret = -1,
		.idle_cfg = -1,
		.idle_end = -1,
		.idle_stop = -1,
		.stop_at = -1,
		.s5_pl = -1,
		.s7_pl = -1,
		.flags = -1,
		.ndtr = -1,
		.dur = -1,
		.nruns = -1,
		.first = -1,
		.first_len = -1,
		.last = -1,
		.last_len = -1,
		.pwm_edges = -1,
		.pwm_first = -1,
		.pwm_last = -1,
		.pwm_hi_min = -1,
		.pwm_hi_max = -1,
		.pwm_lo_min = -1,
		.pwm_lo_max = -1,
		.pwm_per_min = -1,
		.pwm_per_max = -1,
		.pc0_set = -1,
		.pc0_rst = -1,
	};

	return r;
}

static void print_case(const char *kind, uint32_t phase, const struct rec *r)
{
	char flags_text[12];

	if (r->flags < 0) {
		(void)strcpy(flags_text, "-1");
	} else {
		(void)snprintk(flags_text, sizeof(flags_text), "0x%x", (unsigned int)r->flags);
	}
	printk(TAG " %s phase=%u rep=%u name=%s res=%d psc=%d arr=%d rc=%d rc2=%d rc_cfg=%d "
	       "ret=%d done_rc=%d idle_cfg=%d idle_end=%d idle_stop=%d stop_at=%d s5_pl=%d "
	       "s7_pl=%d flags=%s ndtr=%d dur=%d nruns=%d first=%d first_len=%d last=%d "
	       "last_len=%d pwm_edges=%d pwm_first=%d pwm_last=%d pwm_hi_min=%d pwm_hi_max=%d "
	       "pwm_lo_min=%d pwm_lo_max=%d pwm_per_min=%d pwm_per_max=%d pc0_set=%d "
	       "pc0_rst=%d\n",
	       kind, phase, r->rep, r->name, r->res, r->psc, r->arr, r->rc, r->rc2, r->rc_cfg,
	       r->ret, r->done_rc, r->idle_cfg, r->idle_end, r->idle_stop, r->stop_at, r->s5_pl,
	       r->s7_pl, flags_text, r->ndtr, r->dur, r->nruns, r->first, r->first_len, r->last,
	       r->last_len, r->pwm_edges, r->pwm_first, r->pwm_last, r->pwm_hi_min, r->pwm_hi_max,
	       r->pwm_lo_min, r->pwm_lo_max, r->pwm_per_min, r->pwm_per_max, r->pc0_set,
	       r->pc0_rst);
	/* printk returned once the last character reached the transmitter, so this
	 * is a gap on the wire (PACE_MS above). No window is armed while a record
	 * is printed, so nothing being measured waits for it.
	 */
	k_msleep(PACE_MS);

	/* The inner run lengths, in order, 16 to a record: a record value holds
	 * at most 128 characters and a run length at most five digits.
	 */
	for (uint32_t part = 0u; part * 16u < r->inner_count; part++) {
		char values[112];
		uint32_t at = 0u;
		uint32_t from = part * 16u;
		uint32_t to = MIN(r->inner_count, from + 16u);

		for (uint32_t index = from; index < to; index++) {
			at += (uint32_t)snprintk(&values[at], sizeof(values) - at, "%s%u",
						 index == from ? "" : ",", r->inner[index]);
		}
		printk(TAG " runs phase=%u rep=%u name=%s win=%u part=%u v=%s\n", phase, r->rep,
		       r->name, r->win, part, values);
		k_msleep(PACE_MS);
	}
}

static const struct pulse_io_tx_req *request(const uint16_t *duties, size_t count,
					     uint32_t loop_count)
{
	for (size_t index = 0u; index < count; index++) {
		cells[index].duty = duties[index];
		cells[index].level = 0u;
		cells[index].rsvd = 0u;
	}
	req.cells = cells;
	req.count = count;
	req.loop_count = loop_count;

	return &req;
}

static int configure(const struct device *dev, struct pulse_io_channel *chan, uint32_t resolution,
		     bool idle_high)
{
	struct pulse_io_config cfg = {
		.resolution_hz = resolution,
		.cell_period_ticks = PERIOD,
		.mode = PULSE_IO_MODE_CELL,
		.dir = PULSE_IO_DIR_TX,
		.idle_high = idle_high,
	};

	return pulse_io_channel_configure(dev, chan, &cfg);
}

/* What the driver programmed into the timer it was given, read back from the
 * registers: the signature that says which of the two drivers is in the image.
 */
static void read_timer(struct rec *r, uint32_t resolution)
{
	r->res = (int32_t)resolution;
	r->psc = (int32_t)TIM1->PSC;
	r->arr = (int32_t)TIM1->ARR;
}

static void window_begin(uint32_t rep)
{
	hq179_sampler_arm(rep);
	hq179_sampler_start();
	/* The frame this window is opened for starts at this repetition's phase
	 * of the sampling grid: without that the 8-clock samples would measure
	 * the same pulse the same way in every repetition and their mean could
	 * not resolve below one sample (sampler.c).
	 */
	hq179_grid_align(rep);
}

/* Read while the streams are still armed, so the levels are the ones the window
 * ran with and not what disabling the sampler leaves behind.
 */
static void window_priorities(struct rec *r)
{
	uint32_t driver_pl;
	uint32_t sampler_pl;

	hq179_sampler_priorities(&driver_pl, &sampler_pl);
	r->s5_pl = (int32_t)driver_pl;
	r->s7_pl = (int32_t)sampler_pl;
}

static void window_finish(struct rec *r, uint32_t bit)
{
	struct hq179_window win;
	struct hq179_runs runs;
	struct hq179_pwm pwm;

	hq179_sampler_wait(&win);
	hq179_runs_of(bit, &runs);
	hq179_pwm_of(HQ179_BIT_PC6, &pwm);

	r->flags = (int32_t)win.flags;
	r->ndtr = (int32_t)win.ndtr;
	r->dur = (int32_t)win.dur;
	r->nruns = (int32_t)runs.nruns;
	r->first = (int32_t)runs.first;
	r->first_len = (int32_t)runs.first_len;
	r->last = (int32_t)runs.last;
	r->last_len = (int32_t)runs.last_len;
	r->pwm_edges = (int32_t)pwm.edges;
	r->pwm_first = (int32_t)pwm.first;
	r->pwm_last = (int32_t)pwm.last;
	r->pwm_hi_min = (int32_t)pwm.hi_min;
	r->pwm_hi_max = (int32_t)pwm.hi_max;
	r->pwm_lo_min = (int32_t)pwm.lo_min;
	r->pwm_lo_max = (int32_t)pwm.lo_max;
	r->pwm_per_min = (int32_t)pwm.per_min;
	r->pwm_per_max = (int32_t)pwm.per_max;
	r->inner_count = runs.inner_count;
	(void)memcpy(r->inner, runs.inner, sizeof(r->inner));
}

/* ---- the cases ----------------------------------------------------------- */

/* The measurement path proves itself: the bit the sampler reads follows the pin
 * the driver writes, and an undriven window shows one run and the control's PWM.
 */
static void case_qual(uint32_t phase, uint32_t rep)
{
	struct rec r = rec_init("qual", rep, 1u);

	hq179_drive(HQ179_BIT_PC0, 1u);
	hq179_spin(96u); /* one microsecond: far more than the input register needs */
	r.pc0_set = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	hq179_drive(HQ179_BIT_PC0, 0u);
	hq179_spin(96u);
	r.pc0_rst = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);

	window_begin(rep);
	window_priorities(&r);
	window_finish(&r, HQ179_BIT_PC0);
	print_case("case", phase, &r);
}

/* One frame D at one resolution: the per-cell durations and the idle level. */
static void case_duration(uint32_t phase, uint32_t rep, const char *name, uint32_t resolution)
{
	struct rec r = rec_init(name, rep, 1u);

	r.rc_cfg = configure(dev0, chan0, resolution, false);
	read_timer(&r, resolution);

	window_begin(rep);
	r.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_D, CELLS, 0u), K_MSEC(50));
	r.ret = (int32_t)hq179_elapsed();
	r.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	window_priorities(&r);
	window_finish(&r, HQ179_BIT_PC0);
	print_case("case", phase, &r);
}

/* The buffer's boundary: a frame that fills it exactly, then one cell more. */
static void case_full(uint32_t phase, uint32_t rep)
{
	struct rec r = rec_init("full", rep, 1u);

	r.rc_cfg = configure(dev0, chan0, RES_2M, false);
	read_timer(&r, RES_2M);

	window_begin(rep);
	r.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_F, CELLS, 0u), K_MSEC(50));
	r.ret = (int32_t)hq179_elapsed();
	r.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	window_priorities(&r);
	window_finish(&r, HQ179_BIT_PC0);

	r.rc2 = pulse_io_transmit_sync(dev0, chan0, request(FRAME_F9, CELLS + 1u, 0u), K_MSEC(50));
	print_case("case", phase, &r);
}

/* A transfer cut short by its own timeout, then a normal transfer. */
static void case_timeout(uint32_t phase, uint32_t rep)
{
	struct rec r = rec_init("timeout", rep, 1u);
	struct rec r2 = rec_init("timeout", rep, 2u);

	r.rc_cfg = configure(dev0, chan0, RES_500K, false);
	read_timer(&r, RES_500K);
	r2.rc_cfg = r.rc_cfg;
	read_timer(&r2, RES_500K);

	window_begin(rep);
	r.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_F, CELLS, 0u), K_USEC(300));
	r.ret = (int32_t)hq179_elapsed();
	r.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	window_priorities(&r);
	window_finish(&r, HQ179_BIT_PC0);
	print_case("case", phase, &r);

	/* No reconfiguration in between: whatever the abort left on the line is
	 * what the follow-up frame starts from.
	 */
	window_begin(rep);
	r2.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_D, CELLS, 0u), K_MSEC(50));
	r2.ret = (int32_t)hq179_elapsed();
	r2.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	window_priorities(&r2);
	window_finish(&r2, HQ179_BIT_PC0);
	print_case("case2", phase, &r2);
}

/* A transfer stopped from another thread, then a normal transfer. */
static void case_stop(uint32_t phase, uint32_t rep)
{
	struct rec r = rec_init("stop", rep, 1u);
	struct rec r2 = rec_init("stop", rep, 2u);

	r.rc_cfg = configure(dev0, chan0, RES_500K, false);
	read_timer(&r, RES_500K);
	r2.rc_cfg = r.rc_cfg;
	read_timer(&r2, RES_500K);

	window_begin(rep);
	k_sem_give(&stopper_go);
	/* The driver's own timeout is far longer than the frame, so a return can
	 * only come from the stop; a frame that ran to its end returns 0 instead.
	 */
	r.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_F, CELLS, 0u), K_MSEC(3));
	r.ret = (int32_t)hq179_elapsed();
	r.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	window_priorities(&r);
	window_finish(&r, HQ179_BIT_PC0);
	r.done_rc = k_sem_take(&stopper_done, K_MSEC(10));
	r.stop_at = (int32_t)stop_at;
	r.idle_stop = (int32_t)idle_stop;
	print_case("case", phase, &r);

	window_begin(rep);
	r2.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_D, CELLS, 0u), K_MSEC(50));
	r2.ret = (int32_t)hq179_elapsed();
	r2.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	window_priorities(&r2);
	window_finish(&r2, HQ179_BIT_PC0);
	print_case("case2", phase, &r2);
}

/* Two frames in one window: the line must rest at the idle level between them. */
static void case_twice(uint32_t phase, uint32_t rep)
{
	struct rec r = rec_init("twice", rep, 1u);

	r.rc_cfg = configure(dev0, chan0, RES_6M, false);
	read_timer(&r, RES_6M);

	window_begin(rep);
	r.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_D, CELLS, 0u), K_MSEC(50));
	r.rc2 = pulse_io_transmit_sync(dev0, chan0, request(FRAME_D, CELLS, 0u), K_MSEC(50));
	r.ret = (int32_t)hq179_elapsed();
	r.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	window_priorities(&r);
	window_finish(&r, HQ179_BIT_PC0);
	print_case("case", phase, &r);
}

/* The second instance, whose devicetree node has no buffer-size: what the
 * binding's default of 0 gives the driver. It drives PC1, so the window is read
 * for that bit.
 */
static void case_node1(uint32_t phase, uint32_t rep)
{
	struct rec r = rec_init("node1", rep, 1u);
	struct pulse_io_channel *chan1 = NULL;
	int got = pulse_io_channel_get(dev1, 0, &chan1);
	int ready = configure(dev1, chan1, RES_2M, false);

	r.rc_cfg = got != 0 ? got : ready;
	read_timer(&r, RES_2M);

	window_begin(rep);
	r.rc = pulse_io_transmit_sync(dev1, chan1, request(FRAME_D, CELLS, 0u), K_MSEC(50));
	r.ret = (int32_t)hq179_elapsed();
	r.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC1);
	window_priorities(&r);
	window_finish(&r, HQ179_BIT_PC1);

	(void)pulse_io_channel_release(dev1, chan1);
	(void)configure(dev0, chan0, RES_2M, false);
	print_case("case", phase, &r);
}

/* A request the backend says it cannot honour: it reports no loop support. */
static void case_loop(uint32_t phase, uint32_t rep)
{
	struct rec r = rec_init("loop", rep, 1u);

	r.rc_cfg = configure(dev0, chan0, RES_2M, false);
	read_timer(&r, RES_2M);

	r.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_D, CELLS, 1u), K_MSEC(50));
	r.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	print_case("case", phase, &r);
}

/* A high idle level: where the line rests before, between and after frames.
 * Runs last, because before the fix it leaves the line high until a frame ends.
 */
static void case_idlehigh(uint32_t phase, uint32_t rep)
{
	struct rec r = rec_init("idlehigh", rep, 1u);
	int rc_cfg = configure(dev0, chan0, RES_2M, false);
	int pre = pulse_io_transmit_sync(dev0, chan0, request(FRAME_P, 2u, 0u), K_MSEC(50));
	int again;

	/* FRAME_P ends low and its own idle level is low, so both images now rest
	 * low whatever the previous repetition left on the line.
	 */
	if (rc_cfg == 0) {
		rc_cfg = pre;
	}
	again = configure(dev0, chan0, RES_2M, true);
	if (rc_cfg == 0) {
		rc_cfg = again;
	}
	r.rc_cfg = rc_cfg;
	read_timer(&r, RES_2M);
	r.idle_cfg = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);

	window_begin(rep);
	r.rc = pulse_io_transmit_sync(dev0, chan0, request(FRAME_H, CELLS, 0u), K_MSEC(50));
	r.ret = (int32_t)hq179_elapsed();
	r.idle_end = (int32_t)hq179_idr_bit(HQ179_BIT_PC0);
	window_priorities(&r);
	window_finish(&r, HQ179_BIT_PC0);
	print_case("case", phase, &r);
}

static void stopper(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		(void)k_sem_take(&stopper_go, K_FOREVER);
		/* This wait begins when main blocks inside the transmit, because
		 * main runs at a higher priority than this thread.
		 */
		k_busy_wait(300);
		/* With the scheduler locked, the semaphore reset inside stop()
		 * wakes main but does not switch to it, so the level read below
		 * is the one stop() alone left on the line.
		 */
		k_sched_lock();
		stop_at = hq179_elapsed();
		(void)pulse_io_stop(dev0, chan0);
		idle_stop = hq179_idr_bit(HQ179_BIT_PC0);
		k_sched_unlock();
		k_sem_give(&stopper_done);
	}
}

K_THREAD_DEFINE(stopper_thread, 1024, stopper, NULL, NULL, NULL, 1, 0, 0);

static void run_cases(uint32_t phase)
{
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_qual(phase, rep);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_duration(phase, rep, "dur500k", RES_500K);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_duration(phase, rep, "dur2m", RES_2M);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_duration(phase, rep, "dur6m", RES_6M);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_duration(phase, rep, "dur8m", RES_8M);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_full(phase, rep);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_timeout(phase, rep);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_stop(phase, rep);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_twice(phase, rep);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_node1(phase, rep);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_loop(phase, rep);
	}
	for (uint32_t rep = 0u; rep < NREP; rep++) {
		case_idlehigh(phase, rep);
	}
}

int main(void)
{
	uint32_t csr = RCC->CSR;
	uint32_t magic = marker.magic;
	uint32_t armed = marker.armed;
	bool clean_soft;
	uint32_t phase;
	const char *marker_text;
	struct pulse_io_caps caps = {0};
	bool ready0;
	bool ready1;
	uint32_t idr0;
	uint32_t idr1;
	int chan_rc;
	uint32_t start;

	/* Invalidated before anything else can run, so a reset the bench caused
	 * can never find the marker armed.
	 */
	marker.armed = 0u;
	__DSB();
	RCC->CSR |= CSR_RMVF;

	clean_soft = (csr & CSR_MASK) == CSR_CLEAN_SOFT;
	phase = (clean_soft && magic == MARKER_MAGIC && armed == 1u) ? 1u : 0u;
	marker_text = phase != 0u ? "hit" : (magic == MARKER_MAGIC ? "stale" : "none");

	/* The empty line first, so the capture begins at a known byte. */
	printk("\n");
	printk(TAG " boot phase=%u rcc_csr=0x%x marker=%s zephyr=%s sysclk=%u ticks=%u "
	       "enomem=%d etimedout=%d enotsup=%d einval=%d samples=%u sample_clocks=%u "
	       "pwm_arr=%u pwm_ccr=%u nrep=%u\n",
	       phase, csr, marker_text, zephyr_revision(),
	       (unsigned int)sys_clock_hw_cycles_per_sec(),
	       (unsigned int)CONFIG_SYS_CLOCK_TICKS_PER_SEC, ENOMEM, ETIMEDOUT, ENOTSUP, EINVAL,
	       HQ179_SAMPLES, HQ179_SAMPLE_CLOCKS, HQ179_PWM_ARR, HQ179_PWM_CCR, NREP);

	/* Read before anything configures a channel: this is where the driver's
	 * own initialisation left the two lines.
	 */
	ready0 = device_is_ready(dev0);
	ready1 = device_is_ready(dev1);
	idr0 = hq179_idr_bit(HQ179_BIT_PC0);
	idr1 = hq179_idr_bit(HQ179_BIT_PC1);

	hq179_sampler_setup();
	(void)pulse_io_get_capabilities(dev0, &caps);
	chan_rc = pulse_io_channel_get(dev0, 0, &chan0);

	printk(TAG " init phase=%u ready0=%d ready1=%d idr0=%u idr1=%u modes=%u min_tick_ns=%u "
	       "max_ticks=%u loop_max=%u channels=%u pc0_mode=%u pc0_ospeed=%u chan_rc=%d\n",
	       phase, (int)ready0, (int)ready1, idr0, idr1, caps.modes, caps.min_tick_ns,
	       caps.max_duration_ticks, caps.tx_loop_max, (unsigned int)caps.num_channels,
	       hq179_moder(HQ179_BIT_PC0), hq179_ospeedr(HQ179_BIT_PC0), chan_rc);

	run_cases(phase);

	if (phase == 0u) {
		/* The last byte must have left the shift register before the
		 * reset, or the capture would lose the end of this phase.
		 */
		start = k_cycle_get_32();
		while ((USART2->SR & USART_SR_TC) == 0u &&
		       k_cycle_get_32() - start < TC_WAIT_CYCLES) {
		}
		marker.magic = MARKER_MAGIC;
		marker.armed = 1u;
		__DSB();
		sys_reboot(SYS_REBOOT_COLD);
	}

	marker.magic = 0u;
	printk(TAG " done\n");
	for (;;) {
		(void)k_sleep(K_FOREVER);
	}

	return 0;
}
