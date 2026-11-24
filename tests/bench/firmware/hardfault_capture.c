/* Boot on USART2, then leave the stacked HardFault frame for the debugger. */
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

/* Absolute symbols let the debugger read live SCB registers at handler entry. */
__asm__(
    ".global scb_cfsr\n"
    ".type scb_cfsr, %object\n"
    ".set scb_cfsr, 0xE000ED28\n"
    ".size scb_cfsr, 4\n"
    ".global scb_hfsr\n"
    ".type scb_hfsr, %object\n"
    ".set scb_hfsr, 0xE000ED2C\n"
    ".size scb_hfsr, 4\n"
    ".global scb_mmfar\n"
    ".type scb_mmfar, %object\n"
    ".set scb_mmfar, 0xE000ED34\n"
    ".size scb_mmfar, 4\n"
    ".global scb_bfar\n"
    ".type scb_bfar, %object\n"
    ".set scb_bfar, 0xE000ED38\n"
    ".size scb_bfar, 4\n"
);

volatile uint32_t fault_frame[8];

void SystemInit(void)
{
    /* Keep the 16 MHz HSI and configurable fault handlers at reset defaults. */
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
    while ((USART2_SR & USART_SR_TC) == 0U) {
    }
}

__attribute__((noinline, noreturn))
void fault_captured(void)
{
    for (;;) {
    }
}

/* The assembly branch is the only reference the compiler cannot see. */
__attribute__((used, noreturn))
static void hard_fault_capture(const uint32_t *stacked)
{
    /* The basic exception frame is r0, r1, r2, r3, r12, lr, pc, xPSR. */
    for (unsigned int i = 0; i < 8U; ++i) {
        fault_frame[i] = stacked[i];
    }
    fault_captured();
}

__attribute__((naked))
void HardFault_Handler(void)
{
    /* EXC_RETURN bit 2 selects the stack that holds the frame. */
    __asm__ volatile (
        "tst lr, #4\n"
        "ite eq\n"
        "mrseq r0, msp\n"
        "mrsne r0, psp\n"
        "b hard_fault_capture\n"
    );
}

int main(void)
{
    usart2_init();
    usart2_send("hardfault capture image booted\n");
    __asm__ volatile ("udf #0");

    for (;;) {
    }
}
