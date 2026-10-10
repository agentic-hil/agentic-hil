/*
 * afin: can an image watch its own pins without a jumper?
 *
 * (A) TIM2 channel 1 input capture on PA5 while PA5 is a GPIO output that the
 *     CPU toggles, once with PA5's alternate function set to TIM2_CH1 (AF1) and
 *     once with AF0 as the control: does a timer see a pin whose mode is output?
 * (B) DMA2 copies GPIOA->IDR into RAM on every TIM1 update, at 1, 2 and 4 MHz,
 *     while the CPU toggles PA5 at known sample positions: how fast can a pin be
 *     sampled, and how well does a sample index map to time?
 * (C) TIM1 channel 1 PWM on PA8 (CH1) and PA7 (CH1N), sampled at 1 MHz by DMA2
 *     on TIM8 updates, in the four CC1E/CC1NE states.
 * (D) USART1 with CTS flow control: CTS (PA11) held high by the internal
 *     pull-up, then pulled down; once with PA11 in its alternate function, once
 *     as an open-drain GPIO output with the alternate function still selected.
 * (E) TIM5 input capture of TIM3's update event (TRGO, internal trigger ITR1)
 *     across the wrap of TIM5's 32-bit counter.
 *
 * PA5 drives the board's green LED, which flickers during (A) and (B). PA7,
 * PA8, PA9 and PA11 go to the headers only. Every pin is left analog.
 */
#include "board.h"

#define SAMPLES 2000u
#define EDGES 8u
#define GPIOA_IDR_ADDRESS (GPIOA_BASE + 0x10u)
#define DMA_FLAGS 0x3Du /* FEIF, DMEIF, TEIF, HTIF, TCIF of one stream */
#define DMA_TCIF (1u << 5)

static uint16_t samples[SAMPLES];

/* ---- DMA2, peripheral to memory, GPIOA->IDR into samples[] ---- */

static volatile uint32_t *dma_isr(uint32_t stream)
{
    return stream < 4u ? &DMA_LISR(DMA2_BASE) : &DMA_HISR(DMA2_BASE);
}

static volatile uint32_t *dma_ifcr(uint32_t stream)
{
    return stream < 4u ? &DMA_LIFCR(DMA2_BASE) : &DMA_HIFCR(DMA2_BASE);
}

static uint32_t dma_shift(uint32_t stream)
{
    static const uint8_t shifts[4] = {0u, 6u, 16u, 22u};
    return shifts[stream % 4u];
}

static uint32_t dma_flags(uint32_t stream)
{
    return (*dma_isr(stream) >> dma_shift(stream)) & DMA_FLAGS;
}

static void dma_disarm(uint32_t stream)
{
    DMA_SCR(DMA2_BASE, stream) = 0u;
    while ((DMA_SCR(DMA2_BASE, stream) & DMA_SCR_EN) != 0u) {
    }
    *dma_ifcr(stream) = DMA_FLAGS << dma_shift(stream);
}

static void dma_arm(uint32_t stream, uint32_t channel)
{
    for (uint32_t index = 0u; index < SAMPLES; index++) {
        samples[index] = 0xFFFFu;
    }
    dma_disarm(stream);
    DMA_SPAR(DMA2_BASE, stream) = GPIOA_IDR_ADDRESS;
    DMA_SM0AR(DMA2_BASE, stream) = (uint32_t)(uintptr_t)samples;
    DMA_SNDTR(DMA2_BASE, stream) = SAMPLES;
    DMA_SFCR(DMA2_BASE, stream) = 0u; /* direct mode */
    DMA_SCR(DMA2_BASE, stream) =
        DMA_SCR_CHSEL(channel) | DMA_SCR_PL_HIGH | DMA_SCR_MSIZE_16 | DMA_SCR_PSIZE_16 | DMA_SCR_MINC;
    DMA_SCR(DMA2_BASE, stream) |= DMA_SCR_EN;
}

/* A timer that requests one DMA transfer per update, stopped until CEN is set. */
static void trigger_timer(uint32_t timer, uint32_t period_cycles)
{
    TIM_CR1(timer) = 0u;
    TIM_DIER(timer) = 0u;
    TIM_PSC(timer) = 0u;
    TIM_ARR(timer) = period_cycles - 1u;
    TIM_EGR(timer) = TIM_EGR_UG;
    TIM_SR(timer) = 0u;
    TIM_DIER(timer) = TIM_DIER_UDE;
}

static void clocks_on(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_DMA2EN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM2EN | RCC_APB1ENR_TIM3EN | RCC_APB1ENR_TIM5EN;
    RCC_APB2ENR |= RCC_APB2ENR_TIM1EN | RCC_APB2ENR_TIM8EN | RCC_APB2ENR_USART1EN;
    (void)RCC_APB2ENR;
}

/* ---- (A) ---- */

static void capture_on_output_pin(uint32_t alternate)
{
    uint32_t toggled[EDGES];
    uint32_t status[EDGES];
    uint32_t captured[EDGES];

    gpio_write(GPIOA_BASE, 5u, false);
    gpio_configure(GPIOA_BASE, 5u, GPIO_MODE_OUTPUT, GPIO_PULL_NONE, alternate);

    TIM_CR1(TIM2_BASE) = 0u;
    TIM_CCER(TIM2_BASE) = 0u;
    TIM_PSC(TIM2_BASE) = 0u;
    TIM_ARR(TIM2_BASE) = 0xFFFFFFFFu;
    TIM_CCMR1(TIM2_BASE) = 1u; /* CC1S = 01: IC1 on TI1, no filter, no prescaler */
    TIM_CCER(TIM2_BASE) = TIM_CCER_CC1E | TIM_CCER_CC1P | TIM_CCER_CC1NP; /* both edges */
    TIM_EGR(TIM2_BASE) = TIM_EGR_UG;
    delay_us(10u);
    (void)TIM_CCR1(TIM2_BASE);
    TIM_SR(TIM2_BASE) = 0u;

    TIM_CR1(TIM2_BASE) = TIM_CR1_CEN;
    uint32_t start_cycles = cycles();
    uint32_t start_count = TIM_CNT(TIM2_BASE);
    for (uint32_t edge = 0u; edge < EDGES; edge++) {
        uint32_t target = 2000u * (edge + 1u) + 300u * edge * edge;
        while (cycles() - start_cycles < target) {
        }
        toggled[edge] = cycles() - start_cycles;
        gpio_write(GPIOA_BASE, 5u, (edge % 2u) == 0u);
        delay_cycles(200u);
        status[edge] = TIM_SR(TIM2_BASE);
        captured[edge] = TIM_CCR1(TIM2_BASE) - start_count;
        TIM_SR(TIM2_BASE) = 0u;
    }
    TIM_CR1(TIM2_BASE) = 0u;
    TIM_CCER(TIM2_BASE) = 0u;
    TIM_CCMR1(TIM2_BASE) = 0u;
    gpio_write(GPIOA_BASE, 5u, false);
    gpio_configure(GPIOA_BASE, 5u, GPIO_MODE_ANALOG, GPIO_PULL_NONE, 0u);

    for (uint32_t edge = 0u; edge < EDGES; edge++) {
        line_begin("capture");
        field_s("pin", "pa5");
        field_u("af", alternate);
        field_u("edge", edge);
        field_u("level", (edge % 2u) == 0u ? 1u : 0u);
        field_u("toggle_cyc", toggled[edge]);
        field_u("cap", captured[edge]);
        field_u("cc1if", (status[edge] & TIM_SR_CC1IF) != 0u ? 1u : 0u);
        field_u("cc1of", (status[edge] & TIM_SR_CC1OF) != 0u ? 1u : 0u);
        line_end();
    }
}

/* ---- (B) ---- */

static const uint32_t toggle_samples[EDGES] = {100u, 300u, 520u, 760u, 1000u, 1240u, 1500u, 1800u};

static void sample_rate(uint32_t period_cycles)
{
    uint32_t toggled[EDGES];
    uint32_t found[EDGES] = {0u};
    uint32_t transitions = 0u;

    gpio_write(GPIOA_BASE, 5u, false);
    gpio_configure(GPIOA_BASE, 5u, GPIO_MODE_OUTPUT, GPIO_PULL_NONE, 0u);
    trigger_timer(TIM1_BASE, period_cycles);
    dma_arm(5u, 6u); /* DMA2 stream 5 channel 6: TIM1_UP */

    uint32_t start = cycles();
    TIM_CR1(TIM1_BASE) = TIM_CR1_CEN;
    for (uint32_t edge = 0u; edge < EDGES; edge++) {
        uint32_t target = toggle_samples[edge] * period_cycles;
        while (cycles() - start < target) {
        }
        toggled[edge] = cycles() - start;
        gpio_write(GPIOA_BASE, 5u, (edge % 2u) == 0u);
    }
    uint32_t limit = SAMPLES * period_cycles * 4u + 100000u;
    while ((dma_flags(5u) & DMA_TCIF) == 0u && cycles() - start < limit) {
    }
    uint32_t duration = cycles() - start;
    uint32_t flags = dma_flags(5u);
    uint32_t remaining = DMA_SNDTR(DMA2_BASE, 5u);
    TIM_CR1(TIM1_BASE) = 0u;
    TIM_DIER(TIM1_BASE) = 0u;
    dma_disarm(5u);
    gpio_write(GPIOA_BASE, 5u, false);
    gpio_configure(GPIOA_BASE, 5u, GPIO_MODE_ANALOG, GPIO_PULL_NONE, 0u);

    uint32_t any_high = 0u;
    uint32_t all_high = 0xFFFFu;
    for (uint32_t index = 0u; index < SAMPLES; index++) {
        any_high |= samples[index];
        all_high &= samples[index];
        if (index > 0u && ((samples[index] ^ samples[index - 1u]) & (1u << 5)) != 0u) {
            if (transitions < EDGES) {
                found[transitions] = index;
            }
            transitions++;
        }
    }

    line_begin("sampling");
    field_u("period_cycles", period_cycles);
    field_u("samples", SAMPLES);
    field_u("duration", duration);
    field_u("remaining", remaining);
    field_x("flags", flags);
    field_x("first", samples[0]);
    field_x("or", any_high);
    field_x("and", all_high);
    field_u("pa5_edges", transitions);
    line_end();
    for (uint32_t edge = 0u; edge < EDGES; edge++) {
        line_begin("sample-edge");
        field_u("period_cycles", period_cycles);
        field_u("edge", edge);
        field_u("toggle_cyc", toggled[edge]);
        field_u("planned", toggle_samples[edge]);
        if (edge < transitions) {
            field_u("found", found[edge]);
        } else {
            field_s("found", "none");
        }
        line_end();
    }
}

/* ---- (C) ---- */

static void pwm_states(void)
{
    static const uint32_t states[4] = {
        TIM_CCER_CC1E | TIM_CCER_CC1NE,
        TIM_CCER_CC1E,
        TIM_CCER_CC1NE,
        0u,
    };

    gpio_configure(GPIOA_BASE, 8u, GPIO_MODE_AF, GPIO_PULL_DOWN, 1u); /* TIM1_CH1 */
    gpio_configure(GPIOA_BASE, 7u, GPIO_MODE_AF, GPIO_PULL_DOWN, 1u); /* TIM1_CH1N */
    for (uint32_t state = 0u; state < 4u; state++) {
        TIM_CR1(TIM1_BASE) = 0u;
        TIM_DIER(TIM1_BASE) = 0u;
        TIM_CCER(TIM1_BASE) = 0u;
        TIM_PSC(TIM1_BASE) = 0u;
        /* 159 cycles: no multiple of the 16-cycle sample period, so the samples
         * sweep every phase of the PWM period instead of the same ten. */
        TIM_ARR(TIM1_BASE) = 158u;
        TIM_CCR1(TIM1_BASE) = 40u;                    /* OC1REF high for 25 % */
        TIM_CCMR1(TIM1_BASE) = (6u << 4) | (1u << 3); /* OC1M = PWM mode 1, OC1PE */
        TIM_BDTR(TIM1_BASE) = TIM_BDTR_MOE | 8u;      /* 8 cycles (0.5 us) dead time */
        TIM_CCER(TIM1_BASE) = states[state];
        TIM_EGR(TIM1_BASE) = TIM_EGR_UG;
        TIM_CR1(TIM1_BASE) = TIM_CR1_CEN;
        delay_us(50u);

        trigger_timer(TIM8_BASE, 16u);
        dma_arm(1u, 7u); /* DMA2 stream 1 channel 7: TIM8_UP */
        uint32_t start = cycles();
        TIM_CR1(TIM8_BASE) = TIM_CR1_CEN;
        while ((dma_flags(1u) & DMA_TCIF) == 0u && cycles() - start < SAMPLES * 16u * 4u + 100000u) {
        }
        uint32_t duration = cycles() - start;
        uint32_t flags = dma_flags(1u);
        uint32_t remaining = DMA_SNDTR(DMA2_BASE, 1u);
        TIM_CR1(TIM8_BASE) = 0u;
        TIM_DIER(TIM8_BASE) = 0u;
        dma_disarm(1u);
        TIM_CR1(TIM1_BASE) = 0u;

        uint32_t ch1_high = 0u;
        uint32_t ch1_edges = 0u;
        uint32_t ch1n_high = 0u;
        uint32_t ch1n_edges = 0u;
        uint32_t both_high = 0u;
        for (uint32_t index = 0u; index < SAMPLES; index++) {
            bool ch1 = (samples[index] & (1u << 8)) != 0u;
            bool ch1n = (samples[index] & (1u << 7)) != 0u;
            ch1_high += ch1 ? 1u : 0u;
            ch1n_high += ch1n ? 1u : 0u;
            both_high += (ch1 && ch1n) ? 1u : 0u;
            if (index > 0u) {
                uint32_t changed = samples[index] ^ samples[index - 1u];
                ch1_edges += (changed & (1u << 8)) != 0u ? 1u : 0u;
                ch1n_edges += (changed & (1u << 7)) != 0u ? 1u : 0u;
            }
        }
        line_begin("pwm");
        field_u("cc1e", (states[state] & TIM_CCER_CC1E) != 0u ? 1u : 0u);
        field_u("cc1ne", (states[state] & TIM_CCER_CC1NE) != 0u ? 1u : 0u);
        field_u("samples", SAMPLES);
        field_u("duration", duration);
        field_u("remaining", remaining);
        field_x("flags", flags);
        field_u("ch1_high", ch1_high);
        field_u("ch1_edges", ch1_edges);
        field_u("ch1n_high", ch1n_high);
        field_u("ch1n_edges", ch1n_edges);
        field_u("both_high", both_high);
        line_end();
    }
    TIM_CCER(TIM1_BASE) = 0u;
    TIM_BDTR(TIM1_BASE) = 0u;
    TIM_CCMR1(TIM1_BASE) = 0u;
    gpio_configure(GPIOA_BASE, 8u, GPIO_MODE_ANALOG, GPIO_PULL_NONE, 0u);
    gpio_configure(GPIOA_BASE, 7u, GPIO_MODE_ANALOG, GPIO_PULL_NONE, 0u);
}

/* ---- (D) ---- */

static void cts_hold(bool gpio_output, bool blocked)
{
    if (gpio_output) {
        gpio_write(GPIOA_BASE, 11u, blocked); /* open drain: high releases the pin to the pull-up */
    } else {
        gpio_set_pull(GPIOA_BASE, 11u, blocked ? GPIO_PULL_UP : GPIO_PULL_DOWN);
    }
}

static void usart1_reset(void)
{
    RCC_APB2RSTR |= RCC_APB2RSTR_USART1RST;
    RCC_APB2RSTR &= ~RCC_APB2RSTR_USART1RST;
}

static void cts_flow_control(bool gpio_output)
{
    bool blocked_tc[3];
    uint32_t blocked_sr[3];
    bool released_tc[3];
    uint32_t release_cycles[3];
    uint32_t released_sr[3];

    usart1_reset();
    if (gpio_output) {
        GPIO_OTYPER(GPIOA_BASE) |= 1u << 11;
        gpio_write(GPIOA_BASE, 11u, true);
        gpio_configure(GPIOA_BASE, 11u, GPIO_MODE_OUTPUT, GPIO_PULL_UP, 7u);
    } else {
        gpio_configure(GPIOA_BASE, 11u, GPIO_MODE_AF, GPIO_PULL_UP, 7u); /* USART1_CTS */
    }
    delay_us(100u);
    USART_BRR(USART1_BASE) = USART_BRR_115200_AT_16MHZ;
    USART_CR3(USART1_BASE) = USART_CR3_CTSE;
    USART_CR1(USART1_BASE) = USART_CR1_UE | USART_CR1_TE;
    gpio_configure(GPIOA_BASE, 9u, GPIO_MODE_AF, GPIO_PULL_UP, 7u); /* USART1_TX */
    delay_us(2000u);
    uint32_t initial_sr = USART_SR(USART1_BASE);

    for (uint32_t rep = 0u; rep < 3u; rep++) {
        cts_hold(gpio_output, true);
        delay_us(200u);
        USART_SR(USART1_BASE) = ~(USART_SR_TC | USART_SR_CTS); /* both are cleared by writing 0 */
        USART_DR(USART1_BASE) = 0x55u + rep;
        uint32_t start = cycles();
        blocked_tc[rep] = false;
        while (cycles() - start < 2u * (SYSCLK_HZ / 1000u)) {
            if ((USART_SR(USART1_BASE) & USART_SR_TC) != 0u) {
                blocked_tc[rep] = true;
                break;
            }
        }
        blocked_sr[rep] = USART_SR(USART1_BASE);
        cts_hold(gpio_output, false);
        start = cycles();
        released_tc[rep] = false;
        while (cycles() - start < 10u * (SYSCLK_HZ / 1000u)) {
            if ((USART_SR(USART1_BASE) & USART_SR_TC) != 0u) {
                released_tc[rep] = true;
                break;
            }
        }
        release_cycles[rep] = cycles() - start;
        released_sr[rep] = USART_SR(USART1_BASE);
    }
    USART_CR1(USART1_BASE) = 0u;
    USART_CR3(USART1_BASE) = 0u;
    usart1_reset();
    gpio_configure(GPIOA_BASE, 9u, GPIO_MODE_ANALOG, GPIO_PULL_NONE, 0u);
    gpio_configure(GPIOA_BASE, 11u, GPIO_MODE_ANALOG, GPIO_PULL_NONE, 0u);
    GPIO_OTYPER(GPIOA_BASE) &= ~(1u << 11);

    for (uint32_t rep = 0u; rep < 3u; rep++) {
        line_begin("cts");
        field_s("mode", gpio_output ? "gpio-od" : "af");
        field_u("rep", rep);
        field_x("initial_sr", initial_sr);
        field_u("blocked_tc", blocked_tc[rep] ? 1u : 0u);
        field_x("blocked_sr", blocked_sr[rep]);
        field_u("released_tc", released_tc[rep] ? 1u : 0u);
        field_u("release_cycles", release_cycles[rep]);
        field_x("released_sr", released_sr[rep]);
        line_end();
    }
}

/* ---- (E) ---- */

static void trigger_capture(void)
{
    uint32_t captured[EDGES];
    uint32_t status[EDGES];
    const uint32_t preset = 0xFFFFFFFFu - 40000u;

    TIM_CR1(TIM3_BASE) = 0u;
    TIM_PSC(TIM3_BASE) = 15u;
    TIM_ARR(TIM3_BASE) = 999u;    /* an update every 16000 cycles */
    TIM_CR2(TIM3_BASE) = 2u << 4; /* MMS = 010: the update event is TRGO */
    TIM_EGR(TIM3_BASE) = TIM_EGR_UG;
    TIM_SR(TIM3_BASE) = 0u;

    TIM_CR1(TIM5_BASE) = 0u;
    TIM_CCER(TIM5_BASE) = 0u;
    TIM_PSC(TIM5_BASE) = 0u;
    TIM_ARR(TIM5_BASE) = 0xFFFFFFFFu;
    TIM_SMCR(TIM5_BASE) = 1u << 4; /* TS = 001: ITR1 (TIM3 TRGO); SMS = 000 */
    TIM_CCMR1(TIM5_BASE) = 3u;     /* CC1S = 11: IC1 on TRC */
    TIM_CCER(TIM5_BASE) = TIM_CCER_CC1E;
    TIM_EGR(TIM5_BASE) = TIM_EGR_UG;
    TIM_SR(TIM5_BASE) = 0u;
    TIM_CR1(TIM5_BASE) = TIM_CR1_CEN;
    TIM_CNT(TIM5_BASE) = preset; /* wraps between the second and the third capture */
    TIM_CR1(TIM3_BASE) = TIM_CR1_CEN;

    for (uint32_t index = 0u; index < EDGES; index++) {
        uint32_t start = cycles();
        while ((TIM_SR(TIM5_BASE) & TIM_SR_CC1IF) == 0u && cycles() - start < 100000u) {
        }
        status[index] = TIM_SR(TIM5_BASE);
        captured[index] = TIM_CCR1(TIM5_BASE);
        TIM_SR(TIM5_BASE) = 0u;
    }
    TIM_CR1(TIM3_BASE) = 0u;
    TIM_CR1(TIM5_BASE) = 0u;
    TIM_CR2(TIM3_BASE) = 0u;
    TIM_CCER(TIM5_BASE) = 0u;
    TIM_CCMR1(TIM5_BASE) = 0u;
    TIM_SMCR(TIM5_BASE) = 0u;

    for (uint32_t index = 0u; index < EDGES; index++) {
        line_begin("trc");
        field_u("i", index);
        field_x("cap", captured[index]);
        field_u("delta", captured[index] - (index == 0u ? preset : captured[index - 1u]));
        field_u("cc1if", (status[index] & TIM_SR_CC1IF) != 0u ? 1u : 0u);
        field_u("cc1of", (status[index] & TIM_SR_CC1OF) != 0u ? 1u : 0u);
        line_end();
    }
}

int main(void)
{
    board_boot("afin");
    clocks_on();
    capture_on_output_pin(1u);
    capture_on_output_pin(0u);
    sample_rate(16u);
    sample_rate(8u);
    sample_rate(4u);
    pwm_states();
    cts_flow_control(false);
    cts_flow_control(true);
    trigger_capture();
    finish();
}
