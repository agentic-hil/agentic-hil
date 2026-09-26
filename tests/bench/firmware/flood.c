/*
 * A board that never stops talking.
 *
 * USART2 at 115200 baud from boot on, as fast as the transmitter takes bytes:
 * an eight digit decimal counter and a newline, over and over, counting up from
 * zero at every boot and wrapping after 99999999. Nine bytes a line and nothing
 * else on the line, so line n starts at byte 9 * n of the stream, any stretch
 * of the stream says where in it it was taken, and a reader that lost bytes
 * can say exactly how many.
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
#define USART_SR_TXE (1U << 7)
#define USART_CR1_RE (1U << 2)
#define USART_CR1_TE (1U << 3)
#define USART_CR1_UE (1U << 13)
#define USART2_BRR_115200_PCLK16MHZ 139U

#define LINE_DIGITS 8U
#define COUNT_WRAP 100000000U

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

static void usart2_put(char byte)
{
    while ((USART2_SR & USART_SR_TXE) == 0U) {
    }
    USART2_DR = (uint32_t)(unsigned char)byte;
}

int main(void)
{
    char line[LINE_DIGITS + 1U];
    uint32_t count = 0U;

    usart2_init();

    for (;;) {
        uint32_t value = count;

        /* Built while the previous newline is still shifting out, so the
         * transmitter never waits on the arithmetic. */
        for (uint32_t digit = LINE_DIGITS; digit > 0U; --digit) {
            line[digit - 1U] = (char)('0' + (value % 10U));
            value /= 10U;
        }
        line[LINE_DIGITS] = '\n';
        for (uint32_t index = 0U; index <= LINE_DIGITS; ++index) {
            usart2_put(line[index]);
        }
        count = (count + 1U) % COUNT_WRAP;
    }
}
