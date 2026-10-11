/*
 * hq177 image `dual-dac`: the dual-channel DAC of an STM32F446 driven by DMA
 * on one timer trigger, read back by ADC1 on the DAC's own pins.
 *
 * The image measures and prints; tests/hq177/ computes the verdicts. Phases,
 * in order:
 *
 *  1. boot:   USART2 up, TIM2 running as the only clock of this image, the
 *             device and clock registers, the constants below and the SHA-256
 *             of every source the build compiled.
 *  2. adc:    ADC1 on, then the internal reference (IN17) against its factory
 *             calibration word, the control that says the ADC converts sanely.
 *  3/4. static: known codes on both channels, output buffer on and then off,
 *             each read back on its own pin. Buffered codes stop at 3700
 *             because the buffered output is specified only to 0.2 V below
 *             VDDA; 4000 is measured unbuffered only, where the question is
 *             the LD2 load on PA5 and not the absolute level.
 *  5. dma:    the example in dual_dac_dma.c, then PLATEAUS plateaus of 10 ms,
 *             each sampled after a 1 ms settle. Nothing is printed between
 *             the console_flush() before the call and the end of that loop:
 *             the console blocks, one line costs more than one plateau, and
 *             a late poll of the sticky update flag would read a DOR that has
 *             already moved on. The records are held in RAM and printed after.
 *  6. udr:    with the stream disabled the triggers keep coming and nobody
 *             serves them, so DMAUDR1 has to appear; then it is cleared.
 *  7. done.
 *
 * Nothing is wired to the board: every stimulus and every measurement is
 * in-chip, and all time is counted in TIM2, never on the host.
 */
#include <stdbool.h>
#include <stdint.h>

#include <libopencm3/cm3/common.h>
#include <libopencm3/stm32/adc.h>
#include <libopencm3/stm32/dac.h>
#include <libopencm3/stm32/dma.h>
#include <libopencm3/stm32/gpio.h>
#include <libopencm3/stm32/rcc.h>
#include <libopencm3/stm32/timer.h>

#include "console.h"
#include "dual_dac_dma.h"

#define IMAGE_NAME "dual-dac"

#define ARRAY_COUNT(array) ((uint32_t)(sizeof (array) / sizeof (array)[0]))

/* The run, as the gate's constants name it. */
#define N 8u		/* words in the DMA buffer, one per plateau of a cycle */
#define CYCLES 12u	/* complete buffer cycles recorded */
#define PLATEAUS (1u + N * CYCLES)	/* the preload plateau and every cycle */
/* TIM7's prescaler and reload, named for the trigger and not for the timer:
 * TIM7_PSC and TIM7_ARR are the library's own names for those two registers
 * (timer_common_all.h), and redefining them would not compile. */
#define TRIGGER_PSC 15u		/* 16 MHz / (15 + 1) = 1 MHz */
#define TRIGGER_ARR 9999u	/* 10 000 counts of 1 us: one plateau lasts 10 ms */
#define SAMPLES 8u	/* ADC conversions per pin per plateau and per code */
#define VREF_SAMPLES 10u

#define SYSCLK_HZ 16000000u	/* the reset HSI; this image never switches it */
#define COUNTS_PER_US (SYSCLK_HZ / 1000000u)
/* 1 ms after the trigger before the first conversion: the DAC settles in
 * microseconds, so this is slack, not a measured limit. */
#define SETTLE_COUNTS 16000u
/* One TIM7 period in TIM2 counts. Both run from the same clock, so the
 * figure holds whatever the HSI's error is. */
#define PERIOD_COUNTS ((TRIGGER_PSC + 1u) * (TRIGGER_ARR + 1u))
/* Two periods: a trigger that never came is recorded, not waited for. */
#define TRIGGER_TIMEOUT_COUNTS (2u * PERIOD_COUNTS)
/* 480 + 12 ADC cycles at 8 MHz are about 62 us; 1 ms is ample. */
#define CONVERSION_TIMEOUT_COUNTS 16000u

/* System memory: the factory calibration of the internal reference, taken at
 * VDDA = 3.3 V (F446 datasheet). Never the unique device ID. */
#define VREFINT_CAL MMIO16(0x1FFF7A2Au)
/* The debug MCU identity register (RM0390); always clocked. */
#define DBGMCU_IDCODE MMIO32(0xE0042000u)

/*
 * The two level sequences the DMA writes, one per channel. Every value lies
 * in 500..3500, away from both rails; the sets share no value and no two
 * levels written one after the other are close, so a channel swap, a wrong
 * shift or a stale register cannot hide in the noise. Channel 2 stays at or
 * below 1800 (about 1.45 V) because PA5 also drives LD2, which loads that
 * output once its level nears the LED's forward voltage.
 */
static const uint16_t ch1_levels[N] = {
	700u, 1500u, 2300u, 3100u, 2700u, 1900u, 900u, 3500u
};
static const uint16_t ch2_levels[N] = {
	1400u, 600u, 1800u, 1000u, 500u, 1600u, 800u, 1100u
};

/* Every level of both sequences, and four codes above them that document the
 * LD2 load. All inside the output buffer's specified range. */
static const uint16_t buffered_codes[] = {
	500u, 600u, 700u, 800u, 900u, 1000u, 1100u, 1400u, 1500u, 1600u,
	1800u, 1900u, 2300u, 2500u, 2700u, 3000u, 3100u, 3300u, 3500u, 3700u
};
/* With the buffer off the output reaches the rail, so 4000 is measured here. */
static const uint16_t unbuffered_codes[] = {500u, 1500u, 2500u, 3500u, 4000u};

/* The sources the build compiled, with the digests it embedded. Printed from
 * these literals and never from anything computed at run time, so the digest
 * of the example's file is in the binary as ASCII. */
static const char *const source_names[] = {
	"main.c", "dual_dac_dma.c", "console.c", "faults.c"
};
static const char *const source_digests[] = {
	HQ177_SHA_MAIN_C, HQ177_SOURCE_SHA, HQ177_SHA_CONSOLE_C,
	HQ177_SHA_FAULTS_C
};

struct series {
	uint32_t min;
	uint32_t max;
	uint32_t sum;
	uint32_t failed;
};

struct plateau {
	uint32_t t;
	uint32_t isr;
	uint32_t sr;
	uint16_t dor1;
	uint16_t dor2;
	uint16_t ndtr;
	uint8_t timeout;
	struct series channel[2];
};

/* Everything the example configured, read back right after the call returns
 * and again after the last plateau. */
struct registers {
	uint32_t ndtr;
	uint32_t isr;
	uint32_t sr;
	uint32_t cr;
	uint32_t par;
	uint32_t m0ar;
	uint32_t words;
	uint32_t fcr;
	uint32_t dac_cr;
	uint32_t t7_psc;
	uint32_t t7_arr;
	uint32_t t7_cr1;
	uint32_t t7_cr2;
};

static uint32_t dma_words[N];
static struct plateau table[PLATEAUS];

/*
 * TIM2's counter and TIM7's update flag are read straight from their
 * registers rather than through timer_get_counter() and timer_get_flag().
 * Those live in the library, so they cannot be inlined, and the trigger poll
 * below would then take tens of cycles per turn; its latency is what the
 * recorded timestamps carry, and the gate allows 64 counts of it.
 */
static uint32_t now(void)
{
	return TIM_CNT(TIM2);
}

static bool update_pending(void)
{
	return (TIM_SR(TIM7) & TIM_SR_UIF) != 0u;
}

static void delay_us(uint32_t microseconds)
{
	uint32_t start = now();
	uint32_t counts = microseconds * COUNTS_PER_US;

	while (now() - start < counts) {
	}
}

static void series_reset(struct series *series)
{
	series->min = 0xFFFFFFFFu;
	series->max = 0u;
	series->sum = 0u;
	series->failed = 0u;
}

static void series_add(struct series *series, uint32_t value)
{
	series->min = value < series->min ? value : series->min;
	series->max = value > series->max ? value : series->max;
	series->sum += value;
}

static bool convert(uint8_t channel, uint32_t *value)
{
	uint32_t start;

	ADC_SQR3(ADC1) = channel;
	adc_start_conversion_regular(ADC1);
	start = now();
	while (!adc_eoc(ADC1)) {
		if (now() - start > CONVERSION_TIMEOUT_COUNTS) {
			return false;
		}
	}
	*value = adc_read_regular(ADC1) & 0xFFFu;
	return true;
}

static void sample(uint8_t channel, uint32_t count, struct series *result)
{
	series_reset(result);
	for (uint32_t index = 0u; index < count; index++) {
		uint32_t value = 0u;

		if (convert(channel, &value)) {
			series_add(result, value);
		} else {
			result->failed++;
		}
	}
}

/* `attempted` conversions were started; `n` is how many of them are in min,
 * max and sum, so a mean taken as sum/n is a mean of the values it names and a
 * conversion that timed out shows in both n and failed. */
static void print_series(const struct series *series, uint32_t attempted)
{
	field_u("n", attempted - series->failed);
	field_u("min", series->min);
	field_u("max", series->max);
	field_u("sum", series->sum);
	field_u("failed", series->failed);
}

/* The stream's own five flags, shifted down to bit 0 so that the gate reads
 * them as DMA_TCIF, DMA_HTIF, DMA_TEIF, DMA_DMEIF and DMA_FEIF. */
static uint32_t stream_flags(void)
{
	return (DMA_HISR(DMA1) >> DMA_ISR_OFFSET(DMA_STREAM5)) & DMA_ISR_FLAGS;
}

/*
 * The phases below are kept out of line so that main stays the short
 * sequence of calls a reader (and the gate's disassembly check) sees.
 */

static void __attribute__((noinline)) boot(void)
{
	uint32_t idcode;

	/* TIM2 is this image's only clock: 32 bits, undivided, free running. */
	rcc_periph_clock_enable(RCC_TIM2);
	timer_set_prescaler(TIM2, 0u);
	timer_set_period(TIM2, 0xFFFFFFFFu);
	timer_generate_event(TIM2, TIM_EGR_UG);
	timer_clear_flag(TIM2, TIM_SR_UIF);
	timer_enable_counter(TIM2);

	console_init();

	line_begin("boot");
	field_s("image", IMAGE_NAME);
	line_end();

	idcode = DBGMCU_IDCODE;
	line_begin("id");
	field_x("dev", idcode & 0xFFFu);
	field_x("rev", idcode >> 16);
	line_end();

	line_begin("clock");
	field_x("cfgr", RCC_CFGR);
	field_u("sysclk", SYSCLK_HZ);
	line_end();

	line_begin("cfg");
	field_u("n", N);
	field_u("cycles", CYCLES);
	field_u("plateaus", PLATEAUS);
	field_u("psc", TRIGGER_PSC);
	field_u("arr", TRIGGER_ARR);
	field_u("settle", SETTLE_COUNTS);
	field_u("samples", SAMPLES);
	line_end();

	for (uint32_t index = 0u; index < ARRAY_COUNT(source_names); index++) {
		line_begin("src");
		field_s("file", source_names[index]);
		field_s("sha", source_digests[index]);
		line_end();
	}
}

static void __attribute__((noinline)) adc_start(void)
{
	struct series reference;

	rcc_periph_clock_enable(RCC_ADC1);
	/* The ADC clock keeps its reset prescaler, APB2 / 2 = 8 MHz, well
	 * inside the F446's 36 MHz limit; 480 sampling cycles are then 60 us,
	 * far longer than the DAC output needs to charge the sample
	 * capacitor. */
	adc_enable_temperature_sensor();	/* also switches VREFINT to IN17 */
	adc_disable_scan_mode(ADC1);
	adc_set_single_conversion_mode(ADC1);
	adc_set_right_aligned(ADC1);
	adc_set_sample_time(ADC1, ADC_CHANNEL4, ADC_SMPR_SMP_480CYC);
	adc_set_sample_time(ADC1, ADC_CHANNEL5, ADC_SMPR_SMP_480CYC);
	adc_set_sample_time(ADC1, ADC_CHANNEL_VREF, ADC_SMPR_SMP_480CYC);
	adc_power_on(ADC1);
	delay_us(100u);		/* ADC and internal reference start-up */

	line_begin("cal");
	field_u("vrefint", VREFINT_CAL);
	line_end();

	sample(ADC_CHANNEL_VREF, VREF_SAMPLES, &reference);
	line_begin("adc");
	field_u("ch", ADC_CHANNEL_VREF);
	print_series(&reference, VREF_SAMPLES);
	line_end();
}

static void __attribute__((noinline)) dac_pins(void)
{
	rcc_periph_clock_enable(RCC_GPIOA);
	rcc_periph_clock_enable(RCC_DAC);
	/* Analog in: the DAC drives the pad, ADC1 reads the same pad as IN4
	 * and IN5, and no digital driver fights either of them. */
	gpio_mode_setup(GPIOA, GPIO_MODE_ANALOG, GPIO_PUPD_NONE, GPIO4 | GPIO5);
}

/*
 * Known codes on both channels with no trigger and no DMA request, read back
 * on their own pins. DAC_CR goes out with every record so the tests judge the
 * configuration the conversion saw and not only the label.
 */
static void __attribute__((noinline))
static_control(bool buffered, const uint16_t *codes, uint32_t count)
{
	uint32_t control = DAC_CR_EN1 | DAC_CR_EN2;

	if (!buffered) {
		control |= DAC_CR_BOFF1 | DAC_CR_BOFF2;
	}
	DAC_CR(DAC1) = 0u;
	DAC_DHR12R1(DAC1) = 0u;
	DAC_DHR12R2(DAC1) = 0u;
	DAC_CR(DAC1) = control;
	delay_us(100u);		/* the DAC wakes up in about 10 us */

	for (uint32_t index = 0u; index < count; index++) {
		uint32_t code = codes[index];
		uint32_t register_value;
		uint32_t output[2];

		DAC_DHR12R1(DAC1) = code;
		DAC_DHR12R2(DAC1) = code;
		delay_us(200u);
		register_value = DAC_CR(DAC1);
		output[0] = DAC_DOR1(DAC1);
		output[1] = DAC_DOR2(DAC1);

		for (uint32_t pin = 4u; pin <= 5u; pin++) {
			struct series series;

			sample((uint8_t)pin, SAMPLES, &series);
			line_begin("static");
			field_u("buf", buffered ? 1u : 0u);
			field_u("code", code);
			field_s("pin", pin == 4u ? "pa4" : "pa5");
			field_u("ch", pin - 3u);
			field_u("dor", output[pin - 4u]);
			field_x("cr", register_value);
			print_series(&series, SAMPLES);
			line_end();
		}
	}
	DAC_CR(DAC1) = 0u;
}

static void read_registers(struct registers *out)
{
	out->ndtr = DMA_SNDTR(DMA1, DMA_STREAM5);
	out->isr = stream_flags();
	out->sr = DAC_SR(DAC1);
	out->cr = DMA_SCR(DMA1, DMA_STREAM5);
	out->par = (uint32_t)DMA_SPAR(DMA1, DMA_STREAM5);
	out->m0ar = (uint32_t)DMA_SM0AR(DMA1, DMA_STREAM5);
	out->words = (uint32_t)dma_words;
	out->fcr = DMA_SFCR(DMA1, DMA_STREAM5);
	out->dac_cr = DAC_CR(DAC1);
	out->t7_psc = TIM_PSC(TIM7);
	out->t7_arr = TIM_ARR(TIM7);
	out->t7_cr1 = TIM_CR1(TIM7);
	out->t7_cr2 = TIM_CR2(TIM7);
}

/*
 * One record per plateau, held in RAM: the trigger's timestamp, both output
 * registers, the stream's remaining count and flags, the DAC's status and
 * eight conversions of each pin. Returns how often the stream reported a
 * completed transfer. Prints nothing.
 */
static uint32_t __attribute__((noinline)) plateau_loop(void)
{
	uint32_t completions = 0u;

	for (uint32_t index = 0u; index < PLATEAUS; index++) {
		struct plateau *entry = &table[index];
		uint32_t start = now();

		entry->timeout = 0u;
		/* A trigger that never came is recorded and the loop goes on,
		 * so the table stays complete and the gate can name the
		 * plateau that failed instead of counting records. */
		while (!update_pending()) {
			if (now() - start > TRIGGER_TIMEOUT_COUNTS) {
				entry->timeout = 1u;
				break;
			}
		}
		entry->t = now();
		timer_clear_flag(TIM7, TIM_SR_UIF);

		while (now() - entry->t < SETTLE_COUNTS) {
		}
		series_reset(&entry->channel[0]);
		series_reset(&entry->channel[1]);
		for (uint32_t round = 0u; round < SAMPLES; round++) {
			for (uint32_t pin = 0u; pin < 2u; pin++) {
				uint32_t value = 0u;
				uint8_t channel = (uint8_t)(ADC_CHANNEL4 + pin);

				if (convert(channel, &value)) {
					series_add(&entry->channel[pin], value);
				} else {
					entry->channel[pin].failed++;
				}
			}
		}

		entry->dor1 = (uint16_t)DAC_DOR1(DAC1);
		entry->dor2 = (uint16_t)DAC_DOR2(DAC1);
		entry->ndtr = (uint16_t)DMA_SNDTR(DMA1, DMA_STREAM5);
		entry->isr = stream_flags();
		entry->sr = DAC_SR(DAC1);
		/* The transfer-complete and half-transfer flags are counted
		 * and cleared so the next plateau reports its own; the three
		 * error flags are never cleared, so one sighting stays. */
		if ((entry->isr & (DMA_TCIF | DMA_HTIF)) != 0u) {
			if ((entry->isr & DMA_TCIF) != 0u) {
				completions++;
			}
			dma_clear_interrupt_flags(DMA1, DMA_STREAM5,
						  entry->isr &
						  (DMA_TCIF | DMA_HTIF));
		}
	}
	return completions;
}

static void __attribute__((noinline)) print_armed(const struct registers *armed)
{
	line_begin("dma");
	field_s("phase", "armed");
	field_u("ndtr", armed->ndtr);
	field_x("isr", armed->isr);
	field_x("sr", armed->sr);
	field_x("cr", armed->cr);
	field_x("par", armed->par);
	field_x("m0ar", armed->m0ar);
	field_x("words", armed->words);
	field_x("fcr", armed->fcr);
	field_x("dac_cr", armed->dac_cr);
	field_u("t7_psc", armed->t7_psc);
	field_u("t7_arr", armed->t7_arr);
	field_x("t7_cr1", armed->t7_cr1);
	field_x("t7_cr2", armed->t7_cr2);
	line_end();
}

static void __attribute__((noinline)) print_words(void)
{
	for (uint32_t index = 0u; index < N; index++) {
		line_begin("word");
		field_u("i", index);
		field_u("ch1", ch1_levels[index]);
		field_u("ch2", ch2_levels[index]);
		field_x("w", dma_words[index]);
		line_end();
	}
}

static void __attribute__((noinline)) print_plateaus(void)
{
	static const char *const count_keys[2] = {"n1", "n2"};
	static const char *const min_keys[2] = {"min1", "min2"};
	static const char *const max_keys[2] = {"max1", "max2"};
	static const char *const sum_keys[2] = {"sum1", "sum2"};
	static const char *const fail_keys[2] = {"fail1", "fail2"};

	for (uint32_t index = 0u; index < PLATEAUS; index++) {
		const struct plateau *entry = &table[index];

		line_begin("plateau");
		field_u("p", index + 1u);
		field_u("t", entry->t);
		field_u("timeout", entry->timeout);
		field_u("dor1", entry->dor1);
		field_u("dor2", entry->dor2);
		field_u("ndtr", entry->ndtr);
		field_x("isr", entry->isr);
		field_x("sr", entry->sr);
		for (uint32_t channel = 0u; channel < 2u; channel++) {
			const struct series *series = &entry->channel[channel];

			/* As in print_series: the number of conversions that
			 * reached sum, not the number started. */
			field_u(count_keys[channel], SAMPLES - series->failed);
			field_u(min_keys[channel], series->min);
			field_u(max_keys[channel], series->max);
			field_u(sum_keys[channel], series->sum);
			field_u(fail_keys[channel], series->failed);
		}
		line_end();
	}
}

static void __attribute__((noinline))
print_end(const struct registers *end, uint32_t completions)
{
	line_begin("dma");
	field_s("phase", "end");
	field_u("ndtr", end->ndtr);
	field_x("isr", end->isr);
	field_u("tc", completions);
	field_x("sr", end->sr);
	field_x("cr", end->cr);
	field_x("dac_cr", end->dac_cr);
	line_end();
}

/*
 * The positive control of the underrun flag: with the stream disabled the
 * triggers keep coming and nobody serves the requests, so the second one must
 * set DMAUDR1. Channel 2 raises no request at all, because DMA is enabled for
 * channel 1 only, so DMAUDR2 has to stay clear. The timer is stopped before
 * the flag is written back to 1, so no trigger can set it again in between.
 */
static void __attribute__((noinline)) underrun_control(void)
{
	dma_disable_stream(DMA1, DMA_STREAM5);
	timer_clear_flag(TIM7, TIM_SR_UIF);
	for (uint32_t index = 0u; index < 3u; index++) {
		uint32_t start = now();

		while (!update_pending()) {
			if (now() - start > TRIGGER_TIMEOUT_COUNTS) {
				break;
			}
		}
		timer_clear_flag(TIM7, TIM_SR_UIF);
	}
	line_begin("udr");
	field_s("phase", "control");
	field_x("sr", DAC_SR(DAC1));
	field_u("dor1", DAC_DOR1(DAC1));
	field_u("dor2", DAC_DOR2(DAC1));
	line_end();

	timer_disable_counter(TIM7);
	DAC_SR(DAC1) = DAC_SR_DMAUDR1;
	line_begin("udr");
	field_s("phase", "cleared");
	field_x("sr", DAC_SR(DAC1));
	line_end();
}

int main(void)
{
	struct registers armed;
	struct registers end;
	uint32_t completions;

	boot();
	adc_start();
	dac_pins();
	static_control(true, buffered_codes, ARRAY_COUNT(buffered_codes));
	static_control(false, unbuffered_codes, ARRAY_COUNT(unbuffered_codes));
	/* dac_set_trigger_source only ORs bits into DAC_CR, so the example has
	 * to start from a register the static phases left at zero. */
	DAC_CR(DAC1) = 0u;
	DAC_SR(DAC1) = DAC_SR_DMAUDR1 | DAC_SR_DMAUDR2;

	/* Nothing may block between here and the end of the plateau loop. */
	console_flush();
	dual_dac_dma_start(dma_words, ch1_levels, ch2_levels, (uint16_t)N,
			   TRIGGER_PSC, TRIGGER_ARR);
	read_registers(&armed);
	completions = plateau_loop();
	read_registers(&end);

	print_armed(&armed);
	print_words();
	print_plateaus();
	print_end(&end, completions);

	underrun_control();

	dac_disable(DAC1, DAC_CHANNEL_BOTH);
	DAC_CR(DAC1) = 0u;
	adc_power_off(ADC1);

	line_begin("done");
	line_end();
	console_flush();
	/* No WFI: the debugger has to reach the core for the next flash. */
	for (;;) {
	}
}
