/*
 * A board that runs and says nothing.
 *
 * The same clock, the same SysTick witness and the same LED as the demo, and no
 * UART at all: the transmit pin is never switched to its alternate function, so
 * the line stays idle from reset on. A plan that waits for the demo's banner on
 * this board reads nothing and has to say so, which is the difference between a
 * board that answered wrongly and a bench that was never ready.
 *
 * Built by the bench tier as the demo's main.c, with the demo's startup code,
 * linker script and toolchain file, and put on the board through the product.
 */

#include <stdint.h>

#define RCC_AHB1ENR (*(volatile uint32_t *)0x40023830U)
#define GPIOA_MODER (*(volatile uint32_t *)0x40020000U)
#define GPIOA_ODR (*(volatile uint32_t *)0x40020014U)

#define GPIOAEN (1U << 0)
#define LD2_PIN 5U
#define LD2_MODER_MASK (3U << (LD2_PIN * 2U))
#define LD2_MODER_OUTPUT (1U << (LD2_PIN * 2U))

#define SYST_CSR (*(volatile uint32_t *)0xE000E010U)
#define SYST_RVR (*(volatile uint32_t *)0xE000E014U)
#define SYST_CVR (*(volatile uint32_t *)0xE000E018U)
#define SYST_CSR_ENABLE (1U << 0)
#define SYST_CSR_TICKINT (1U << 1)
#define SYST_CSR_CLKSOURCE (1U << 2)
#define SYSTICK_1MS_AT_16MHZ (16000U - 1U)

volatile uint32_t uptime_ms = 0U;

void SysTick_Handler(void)
{
    uptime_ms++;
}

void SystemInit(void)
{
    /* Keep the reset clock defaults: HSI at 16 MHz, as the demo does. */
}

int main(void)
{
    SYST_RVR = SYSTICK_1MS_AT_16MHZ;
    SYST_CVR = 0U;
    SYST_CSR = SYST_CSR_CLKSOURCE | SYST_CSR_TICKINT | SYST_CSR_ENABLE;

    RCC_AHB1ENR |= GPIOAEN;
    GPIOA_MODER = (GPIOA_MODER & ~LD2_MODER_MASK) | LD2_MODER_OUTPUT;

    for (;;) {
        GPIOA_ODR ^= (1U << LD2_PIN);
        for (volatile uint32_t i = 0; i < 1000000U; ++i) {
            __asm volatile ("nop");
        }
    }
}
