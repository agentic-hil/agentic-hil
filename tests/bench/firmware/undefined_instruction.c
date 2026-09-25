/*
 * A board that boots, says so once, and faults.
 *
 * One line on USART2 as soon as the core is up, then an instruction the core
 * has no meaning for. With the configurable fault handlers still disabled, as
 * they are out of reset, that undefined instruction escalates to a HardFault,
 * and the handler is a plain endless loop: nothing restarts the core and
 * nothing touches the debug port, so the probe still reaches a core that sits
 * in the fault for as long as it is left there. That is the board a debugger
 * has to report as faulted, a reset has to bring back to its boot line, and a
 * flash has to replace.
 *
 * Built by the bench tier as the demo's main.c, with the demo's startup code,
 * linker script and toolchain file, and put on the board through the product.
 */

#include <stdint.h>

#define RCC_AHB1ENR (*(volatile uint32_t *)0x40023830U)
#define RCC_APB1ENR (*(volatile uint32_t *)0x40023840U)
#define GPIOA_MODER (*(volatile uint32_t *)0x40020000U)
#define GPIOA_AFRL (*(volatile uint32_t *)0x40020020U)
#define USART2_SR (*(volatile uint32_t *)0x40004400U)
#define USART2_DR (*(volatile uint32_t *)0x40004404U)
#define USART2_BRR (*(volatile uint32_t *)0x40004408U)
#define USART2_CR1 (*(volatile uint32_t *)0x4000440CU)
#define USART2_CR2 (*(volatile uint32_t *)0x40004410U)
#define USART2_CR3 (*(volatile uint32_t *)0x40004414U)

#define GPIOAEN (1U << 0)
#define USART2EN (1U << 17)
#define USART2_TX_PIN 2U
#define USART2_RX_PIN 3U
#define GPIO_MODER_AF(pin) (2U << ((pin) * 2U))
#define GPIO_MODER_MASK(pin) (3U << ((pin) * 2U))
#define GPIO_AFRL_AF7(pin) (7U << ((pin) * 4U))
#define GPIO_AFRL_MASK(pin) (0xFU << ((pin) * 4U))
#define USART_SR_TC (1U << 6)
#define USART_SR_TXE (1U << 7)
#define USART_CR1_RE (1U << 2)
#define USART_CR1_TE (1U << 3)
#define USART_CR1_UE (1U << 13)
#define USART2_BRR_115200_PCLK16MHZ 139U

void SystemInit(void)
{
    /* Keep the reset clock defaults: HSI at 16 MHz, as the demo does. */
}

static void usart2_init(void)
{
    RCC_AHB1ENR |= GPIOAEN;
    RCC_APB1ENR |= USART2EN;

    GPIOA_MODER = (GPIOA_MODER & ~(GPIO_MODER_MASK(USART2_TX_PIN) |
                                    GPIO_MODER_MASK(USART2_RX_PIN))) |
                   GPIO_MODER_AF(USART2_TX_PIN) |
                   GPIO_MODER_AF(USART2_RX_PIN);
    GPIOA_AFRL = (GPIOA_AFRL & ~(GPIO_AFRL_MASK(USART2_TX_PIN) |
                                  GPIO_AFRL_MASK(USART2_RX_PIN))) |
                 GPIO_AFRL_AF7(USART2_TX_PIN) |
                 GPIO_AFRL_AF7(USART2_RX_PIN);

    USART2_CR1 = 0U;
    USART2_CR2 = 0U;
    USART2_CR3 = 0U;
    USART2_BRR = USART2_BRR_115200_PCLK16MHZ;
    USART2_CR1 = USART_CR1_RE | USART_CR1_TE | USART_CR1_UE;
}

static void usart2_send(const char *text)
{
    while (*text != '\0') {
        while ((USART2_SR & USART_SR_TXE) == 0U) {
        }
        USART2_DR = (uint32_t)(unsigned char)*text;
        ++text;
    }
    /* The whole line on the wire before the fault, not most of it. */
    while ((USART2_SR & USART_SR_TC) == 0U) {
    }
}

void HardFault_Handler(void)
{
    /* Stay in the fault: no restart, no return, and the debug port left alone. */
    for (;;) {
    }
}

int main(void)
{
    usart2_init();
    usart2_send("undefined instruction image booted\n");

    /* Permanently undefined in Thumb: the core can only take the fault. */
    __asm volatile ("udf #0");

    for (;;) {
    }
}
