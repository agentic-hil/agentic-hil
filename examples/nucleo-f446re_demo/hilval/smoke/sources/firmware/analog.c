/*
 * analog: can an image measure its own DAC outputs?
 *
 * The DAC drives PA4 (channel 1) and PA5 (channel 2); ADC1 converts the same
 * pins as IN4 and IN5, without a jumper. PA5 also drives the board's green LED
 * through a resistor, so channel 2 carries a load that channel 1 does not. The
 * sweep runs once with the DAC's output buffers on and once with them off.
 *
 * Before the sweep the image prints the factory calibration values and ten
 * conversions each of the internal reference (IN17) and the temperature sensor
 * (IN18); from them the test derives VDDA, the ADC's reference.
 */
#include "board.h"

#define INTERNAL_SAMPLES 10u
#define PIN_SAMPLES 8u
/* 480 + 12 ADC cycles at 4 MHz are about 2000 CPU cycles. */
#define CONVERSION_TIMEOUT_CYCLES 16000u

struct series {
    uint32_t min;
    uint32_t max;
    uint32_t sum;
    uint32_t failed;
};

static const uint32_t codes[] = {100u, 500u, 2000u, 3500u, 4000u};

static bool convert(uint32_t channel, uint32_t *value)
{
    ADC_SQR3(ADC1_BASE) = channel;
    ADC_CR2(ADC1_BASE) |= ADC_CR2_SWSTART;
    uint32_t start = cycles();
    while ((ADC_SR(ADC1_BASE) & ADC_SR_EOC) == 0u) {
        if (cycles() - start > CONVERSION_TIMEOUT_CYCLES) {
            return false;
        }
    }
    *value = ADC_DR(ADC1_BASE) & 0xFFFu; /* reading DR clears EOC */
    return true;
}

static void sample(uint32_t channel, uint32_t count, struct series *result)
{
    result->min = 0xFFFFFFFFu;
    result->max = 0u;
    result->sum = 0u;
    result->failed = 0u;
    for (uint32_t index = 0u; index < count; index++) {
        uint32_t value = 0u;
        if (!convert(channel, &value)) {
            result->failed++;
            continue;
        }
        result->min = value < result->min ? value : result->min;
        result->max = value > result->max ? value : result->max;
        result->sum += value;
    }
}

static void print_series(const struct series *series, uint32_t count)
{
    field_u("n", count);
    field_u("min", series->min);
    field_u("max", series->max);
    field_u("sum", series->sum);
    field_u("failed", series->failed);
}

static void adc_on(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB1ENR |= RCC_APB1ENR_DACEN;
    RCC_APB2ENR |= RCC_APB2ENR_ADC1EN;
    (void)RCC_APB2ENR;
    gpio_configure(GPIOA_BASE, 4u, GPIO_MODE_ANALOG, GPIO_PULL_NONE, 0u);
    gpio_configure(GPIOA_BASE, 5u, GPIO_MODE_ANALOG, GPIO_PULL_NONE, 0u);

    ADC_CCR = ADC_CCR_ADCPRE_DIV4 | ADC_CCR_TSVREFE; /* 4 MHz ADC clock; VREFINT and sensor on */
    ADC_CR1(ADC1_BASE) = 0u;                          /* 12 bits, no scan */
    ADC_SMPR1(ADC1_BASE) = (7u << 21) | (7u << 24);   /* IN17, IN18: 480 cycles */
    ADC_SMPR2(ADC1_BASE) = (7u << 12) | (7u << 15);   /* IN4, IN5: 480 cycles */
    ADC_SQR1(ADC1_BASE) = 0u;                         /* one conversion */
    ADC_CR2(ADC1_BASE) = ADC_CR2_ADON;
    delay_us(100u); /* ADC and temperature sensor start-up */
}

static void internal_channels(void)
{
    line_begin("cal");
    field_u("vrefint", VREFINT_CAL);
    field_u("ts30", TS_CAL1);
    field_u("ts110", TS_CAL2);
    line_end();

    static const uint32_t channels[2] = {17u, 18u};
    for (uint32_t index = 0u; index < 2u; index++) {
        struct series series;
        sample(channels[index], INTERNAL_SAMPLES, &series);
        line_begin("adc");
        field_u("ch", channels[index]);
        print_series(&series, INTERNAL_SAMPLES);
        line_end();
    }
}

static void dac_sweep(bool buffered)
{
    uint32_t control = DAC_CR_EN1 | DAC_CR_EN2;
    if (!buffered) {
        control |= DAC_CR_BOFF1 | DAC_CR_BOFF2;
    }
    DAC_CR = 0u;
    DAC_DHR12R1 = 0u;
    DAC_DHR12R2 = 0u;
    DAC_CR = control; /* no trigger: a data register write reaches the output one APB cycle later */
    delay_us(100u);

    for (uint32_t index = 0u; index < sizeof codes / sizeof codes[0]; index++) {
        DAC_DHR12R1 = codes[index];
        DAC_DHR12R2 = codes[index];
        delay_us(200u);
        uint32_t output[2] = {DAC_DOR1, DAC_DOR2};

        for (uint32_t pin = 4u; pin <= 5u; pin++) {
            struct series series;
            sample(pin, PIN_SAMPLES, &series);
            line_begin("dac");
            field_u("buf", buffered ? 1u : 0u);
            field_u("ch", pin - 3u);
            field_s("pin", pin == 4u ? "pa4" : "pa5");
            field_u("code", codes[index]);
            field_u("dor", output[pin - 4u]);
            print_series(&series, PIN_SAMPLES);
            line_end();
        }
    }
    DAC_CR = 0u;
}

int main(void)
{
    board_boot("analog");
    adc_on();
    internal_channels();
    dac_sweep(true);
    dac_sweep(false);
    ADC_CR2(ADC1_BASE) = 0u;
    ADC_CCR = 0u;
    finish();
}
