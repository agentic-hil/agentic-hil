/*
 * Registers and helpers the smoke images share. NUCLEO-F446RE, reset clock
 * (HSI, 16 MHz, every bus undivided), no interrupts, no C library.
 *
 * Every image prints lines "SMK <kind> key=value ..." on USART2 (PA2/PA3, the
 * ST-LINK virtual COM port, 115200 baud 8N1) and ends with "SMK done". The
 * lines carry raw measurements; the tests that run the images judge them.
 */
#ifndef HILVAL_SMOKE_BOARD_H
#define HILVAL_SMOKE_BOARD_H

#include <stdbool.h>
#include <stdint.h>

#define TAG "SMK"
#define SYSCLK_HZ 16000000u

#define REG32(address) (*(volatile uint32_t *)(uintptr_t)(address))
#define REG16(address) (*(volatile uint16_t *)(uintptr_t)(address))

/* RCC */
#define RCC_BASE 0x40023800u
#define RCC_CR REG32(RCC_BASE + 0x00u)
#define RCC_CFGR REG32(RCC_BASE + 0x08u)
#define RCC_APB2RSTR REG32(RCC_BASE + 0x24u)
#define RCC_AHB1ENR REG32(RCC_BASE + 0x30u)
#define RCC_APB1ENR REG32(RCC_BASE + 0x40u)
#define RCC_APB2ENR REG32(RCC_BASE + 0x44u)
#define RCC_CSR REG32(RCC_BASE + 0x74u)

#define RCC_AHB1ENR_GPIOAEN (1u << 0)
#define RCC_AHB1ENR_GPIOBEN (1u << 1)
#define RCC_AHB1ENR_GPIOCEN (1u << 2)
#define RCC_AHB1ENR_DMA2EN (1u << 22)
#define RCC_APB1ENR_TIM2EN (1u << 0)
#define RCC_APB1ENR_TIM3EN (1u << 1)
#define RCC_APB1ENR_TIM5EN (1u << 3)
#define RCC_APB1ENR_USART2EN (1u << 17)
#define RCC_APB1ENR_PWREN (1u << 28)
#define RCC_APB1ENR_DACEN (1u << 29)
#define RCC_APB2ENR_TIM1EN (1u << 0)
#define RCC_APB2ENR_TIM8EN (1u << 1)
#define RCC_APB2ENR_USART1EN (1u << 4)
#define RCC_APB2ENR_ADC1EN (1u << 8)
#define RCC_APB2RSTR_USART1RST (1u << 4)
#define RCC_CSR_RMVF (1u << 24)
#define RCC_CSR_PINRSTF (1u << 26)
#define RCC_CSR_PORRSTF (1u << 27)
#define RCC_CSR_SFTRSTF (1u << 28)
#define RCC_CSR_IWDGRSTF (1u << 29)

/* GPIO */
#define GPIOA_BASE 0x40020000u
#define GPIOB_BASE 0x40020400u
#define GPIOC_BASE 0x40020800u
#define GPIO_MODER(port) REG32((port) + 0x00u)
#define GPIO_OTYPER(port) REG32((port) + 0x04u)
#define GPIO_OSPEEDR(port) REG32((port) + 0x08u)
#define GPIO_PUPDR(port) REG32((port) + 0x0Cu)
#define GPIO_IDR(port) REG32((port) + 0x10u)
#define GPIO_ODR(port) REG32((port) + 0x14u)
#define GPIO_BSRR(port) REG32((port) + 0x18u)
#define GPIO_AFRL(port) REG32((port) + 0x20u)
#define GPIO_AFRH(port) REG32((port) + 0x24u)

#define GPIO_MODE_INPUT 0u
#define GPIO_MODE_OUTPUT 1u
#define GPIO_MODE_AF 2u
#define GPIO_MODE_ANALOG 3u
#define GPIO_PULL_NONE 0u
#define GPIO_PULL_UP 1u
#define GPIO_PULL_DOWN 2u

/* USART (STM32F4 register layout) */
#define USART1_BASE 0x40011000u
#define USART2_BASE 0x40004400u
#define USART_SR(base) REG32((base) + 0x00u)
#define USART_DR(base) REG32((base) + 0x04u)
#define USART_BRR(base) REG32((base) + 0x08u)
#define USART_CR1(base) REG32((base) + 0x0Cu)
#define USART_CR2(base) REG32((base) + 0x10u)
#define USART_CR3(base) REG32((base) + 0x14u)

#define USART_SR_PE (1u << 0)
#define USART_SR_FE (1u << 1)
#define USART_SR_NF (1u << 2)
#define USART_SR_ORE (1u << 3)
#define USART_SR_IDLE (1u << 4)
#define USART_SR_RXNE (1u << 5)
#define USART_SR_TC (1u << 6)
#define USART_SR_TXE (1u << 7)
#define USART_SR_CTS (1u << 9)
#define USART_CR1_RE (1u << 2)
#define USART_CR1_TE (1u << 3)
#define USART_CR1_PS (1u << 9)
#define USART_CR1_PCE (1u << 10)
#define USART_CR1_M (1u << 12)
#define USART_CR1_UE (1u << 13)
#define USART_CR3_CTSE (1u << 9)
/* 16 MHz / (16 * 8.6875) = 115108 baud, -0.08 %. */
#define USART_BRR_115200_AT_16MHZ 139u

/* Timers */
#define TIM1_BASE 0x40010000u
#define TIM2_BASE 0x40000000u
#define TIM3_BASE 0x40000400u
#define TIM5_BASE 0x40000C00u
#define TIM8_BASE 0x40010400u
#define TIM_CR1(base) REG32((base) + 0x00u)
#define TIM_CR2(base) REG32((base) + 0x04u)
#define TIM_SMCR(base) REG32((base) + 0x08u)
#define TIM_DIER(base) REG32((base) + 0x0Cu)
#define TIM_SR(base) REG32((base) + 0x10u)
#define TIM_EGR(base) REG32((base) + 0x14u)
#define TIM_CCMR1(base) REG32((base) + 0x18u)
#define TIM_CCER(base) REG32((base) + 0x20u)
#define TIM_CNT(base) REG32((base) + 0x24u)
#define TIM_PSC(base) REG32((base) + 0x28u)
#define TIM_ARR(base) REG32((base) + 0x2Cu)
#define TIM_CCR1(base) REG32((base) + 0x34u)
#define TIM_BDTR(base) REG32((base) + 0x44u)

#define TIM_CR1_CEN (1u << 0)
#define TIM_CR1_ARPE (1u << 7)
#define TIM_SR_UIF (1u << 0)
#define TIM_SR_CC1IF (1u << 1)
#define TIM_SR_CC1OF (1u << 9)
#define TIM_DIER_UDE (1u << 8)
#define TIM_EGR_UG (1u << 0)
#define TIM_CCER_CC1E (1u << 0)
#define TIM_CCER_CC1P (1u << 1)
#define TIM_CCER_CC1NE (1u << 2)
#define TIM_CCER_CC1NP (1u << 3)
#define TIM_BDTR_MOE (1u << 15)

/* DMA2 (the DMA whose peripheral port reaches the AHB1 GPIO ports) */
#define DMA2_BASE 0x40026400u
#define DMA_LISR(base) REG32((base) + 0x00u)
#define DMA_HISR(base) REG32((base) + 0x04u)
#define DMA_LIFCR(base) REG32((base) + 0x08u)
#define DMA_HIFCR(base) REG32((base) + 0x0Cu)
#define DMA_SCR(base, stream) REG32((base) + 0x10u + 0x18u * (stream))
#define DMA_SNDTR(base, stream) REG32((base) + 0x14u + 0x18u * (stream))
#define DMA_SPAR(base, stream) REG32((base) + 0x18u + 0x18u * (stream))
#define DMA_SM0AR(base, stream) REG32((base) + 0x1Cu + 0x18u * (stream))
#define DMA_SFCR(base, stream) REG32((base) + 0x24u + 0x18u * (stream))

#define DMA_SCR_EN (1u << 0)
#define DMA_SCR_MINC (1u << 10)
#define DMA_SCR_PSIZE_16 (1u << 11)
#define DMA_SCR_MSIZE_16 (1u << 13)
#define DMA_SCR_PL_HIGH (2u << 16)
#define DMA_SCR_CHSEL(channel) ((uint32_t)(channel) << 25)

/* ADC1 and the ADC common registers */
#define ADC1_BASE 0x40012000u
#define ADC_SR(base) REG32((base) + 0x00u)
#define ADC_CR1(base) REG32((base) + 0x04u)
#define ADC_CR2(base) REG32((base) + 0x08u)
#define ADC_SMPR1(base) REG32((base) + 0x0Cu)
#define ADC_SMPR2(base) REG32((base) + 0x10u)
#define ADC_SQR1(base) REG32((base) + 0x2Cu)
#define ADC_SQR3(base) REG32((base) + 0x34u)
#define ADC_DR(base) REG32((base) + 0x4Cu)
#define ADC_CCR REG32(0x40012300u + 0x04u)

#define ADC_SR_EOC (1u << 1)
#define ADC_CR2_ADON (1u << 0)
#define ADC_CR2_SWSTART (1u << 30)
#define ADC_CCR_ADCPRE_DIV4 (1u << 16)
#define ADC_CCR_TSVREFE (1u << 23)

/* DAC */
#define DAC_BASE 0x40007400u
#define DAC_CR REG32(DAC_BASE + 0x00u)
#define DAC_DHR12R1 REG32(DAC_BASE + 0x08u)
#define DAC_DHR12R2 REG32(DAC_BASE + 0x14u)
#define DAC_DOR1 REG32(DAC_BASE + 0x2Cu)
#define DAC_DOR2 REG32(DAC_BASE + 0x30u)
#define DAC_CR_EN1 (1u << 0)
#define DAC_CR_BOFF1 (1u << 1)
#define DAC_CR_EN2 (1u << 16)
#define DAC_CR_BOFF2 (1u << 17)

/* Independent watchdog, power control and the RTC backup registers */
#define IWDG_KR REG32(0x40003000u)
#define IWDG_PR REG32(0x40003004u)
#define IWDG_RLR REG32(0x40003008u)
#define IWDG_SR REG32(0x4000300Cu)
#define PWR_CR REG32(0x40007000u)
#define PWR_CR_DBP (1u << 8)
#define RTC_BKP0R REG32(0x40002850u)

/* System memory: factory calibration and device data (never the unique ID). */
#define FLASH_SIZE_KB REG16(0x1FFF7A22u)
#define VREFINT_CAL REG16(0x1FFF7A2Au)
#define TS_CAL1 REG16(0x1FFF7A2Cu)
#define TS_CAL2 REG16(0x1FFF7A2Eu)

/* Cortex-M4 core */
#define DBGMCU_IDCODE REG32(0xE0042000u)
#define DEMCR REG32(0xE000EDFCu)
#define DWT_CTRL REG32(0xE0001000u)
#define DWT_CYCCNT REG32(0xE0001004u)
#define DWT_LAR REG32(0xE0001FB0u)
#define SCB_CFSR REG32(0xE000ED28u)
#define SCB_HFSR REG32(0xE000ED2Cu)
#define SCB_MMFAR REG32(0xE000ED34u)
#define SCB_BFAR REG32(0xE000ED38u)

/* RCC_CSR as it was at this boot, before the reset flags were cleared. */
extern uint32_t board_reset_flags;

/* Clocks the console and the cycle counter, prints "SMK boot image=<image> ...". */
void board_boot(const char *image);

static inline uint32_t cycles(void) { return DWT_CYCCNT; }
void delay_cycles(uint32_t count);
void delay_us(uint32_t microseconds);

/* Sets a pin's mode, pull and alternate function; the output type is left as it is. */
void gpio_configure(uint32_t port, uint32_t pin, uint32_t mode, uint32_t pull, uint32_t alternate);
void gpio_set_pull(uint32_t port, uint32_t pin, uint32_t pull);
void gpio_write(uint32_t port, uint32_t pin, bool high);

/* One line: line_begin("kind"), then fields, then line_end(). */
void line_begin(const char *kind);
void field_u(const char *key, uint32_t value);
void field_i(const char *key, int32_t value);
void field_x(const char *key, uint32_t value);
void field_s(const char *key, const char *value);
void line_end(void);

/* Waits until the last byte has left USART2 (TC). */
void console_flush(void);

/* Prints "SMK done" and stops. */
__attribute__((noreturn)) void finish(void);

#endif
