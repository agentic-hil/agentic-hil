/*
 * A board that answers: the serial and CAN counterparty of the bench tier.
 *
 * The demo prints one line and reads nothing, so over the real line it can
 * prove a banner and nothing a caller sends. This image is the other end of
 * the product's serial tools instead: it reads what the product writes, answers
 * out of a table, and reports what arrived, the way the container tier's
 * scripted peer (tests/container/pty_responder.py) does over a pseudo-terminal.
 *
 * The line. USART2 on PA2 (TX) and PA3 (RX), which the NUCLEO-F446RE wires to
 * the ST-LINK virtual COM port. 8 data bits, no parity, one stop bit, 115200
 * baud from boot, on the demo's clock (HSI, 16 MHz on every bus). It keeps the
 * demo's `uptime_ms` SysTick witness, and LD2 blinks twice a second.
 *
 * It prints one line at boot, `@peer ready`, and after that speaks only when
 * a rule, a setting or a control line below says so.
 *
 * Lines. A line is every byte received up to a LF (0x0A). The CRs (0x0D) at
 * its end are removed, and what remains is the line: exactly the container
 * peer's `line.rstrip(b"\r")`. A line longer than 3072 bytes is an overlong
 * line: counted, never matched, never answered. A line without its LF yet is
 * kept until the LF comes; sending "\r\n" first finishes whatever is left.
 *
 * Payload lines. A line that does not start with the six bytes "@peer " is a
 * payload line. A payload line equal to the request of a reply rule is
 * answered with that rule's response, byte for byte; a line no rule names is
 * not answered. With a delay set, each answer is written that many ms after its
 * request arrived or after the answer before it, whichever is later, which is
 * the container peer sleeping before each answer. At most 16 answers wait at a
 * time; a request that finds 16 waiting is not answered.
 *
 * Control lines start with "@peer ". They are not payload: never matched, never
 * echoed, and taken out of the statistics again, line ending included. Each
 * one is answered with exactly one line, `@peer ok <command>...` when it was
 * applied and `@peer error <command> <reason>` when it was not:
 *
 *   @peer rule REQUEST=RESPONSE  answer REQUEST with RESPONSE (replaces an
 *                                earlier rule for REQUEST; 8 rules at most)
 *   @peer unrule REQUEST         forget the rule for REQUEST
 *   @peer clear                  forget every rule
 *   @peer delay MS               wait MS ms (0 to 60000) before each answer
 *   @peer announce MS TEXT       write TEXT unprompted, at once and then every
 *                                MS ms (1 to 60000), without being asked
 *   @peer announce off           stop announcing
 *   @peer echo on|off            write every payload byte back as it arrives
 *   @peer flood N                write N bytes (1 to 100000000) of the digits
 *                                "0123456789" over and over, after the answer
 *   @peer flood on|off           the same without end, or stop it
 *   @peer baud RATE              answer at the old rate, then switch to RATE
 *                                (1200 to 1000000)
 *   @peer silence on|off         write nothing but control answers; switching
 *                                it on drops waiting answers and stops a flood
 *   @peer temp REQUEST           answer REQUEST with a temperature measurement
 *   @peer temp                   stop answering it
 *   @peer stats                  `@peer ok stats bytes=N crc32=HHHHHHHH lines=N
 *                                overlong=N lost=N`
 *   @peer reset                  every setting back to its boot default, the
 *                                statistics zeroed, anything not yet sent
 *                                discarded; answered at the old rate, then
 *                                115200 again
 *
 * Changing the rules, the temperature request or the delay drops the answers
 * still waiting, as do silence and reset. Errors name a reason: `syntax` (the line does not parse),
 * `range` (a number out of bounds), `escape` (a malformed escape), `long` (a
 * request over 64 bytes, a response or an announcement over 512), `full` (no
 * free rule), `absent` (no such rule). An unknown command is answered
 * `@peer error unknown`, an overlong control line `@peer error overlong`.
 *
 * Escapes. REQUEST, RESPONSE and TEXT are read under Python's escape rules,
 * as the container peer reads its table through `unicode_escape`, so a table
 * entry is spelled the same for both peers and any byte, CR and LF included,
 * can be asked for or answered: \\ \' \" \a \b \f \n \r \t \v, \xhh (exactly
 * two hex digits), \ooo (one to three octal digits), \uhhhh and \Uhhhhhhhh
 * (up to 0xff, since these are bytes). \N{...} is not taken. A backslash
 * before any other character stands for itself, as in Python. A malformed
 * escape changes nothing and is answered `escape`. As in the container peer,
 * REQUEST and RESPONSE are split at the first "=" of the text as written, so
 * an "=" inside a request is spelled \x3d.
 *
 * Statistics. `bytes` and `crc32` cover every payload byte received since boot
 * or the last reset, line endings included: the count, and the CRC-32 that
 * Python's `zlib.crc32` computes over the same bytes, as eight lowercase hex
 * digits. Together they replace the container peer's record file. `lines`
 * counts complete payload lines, `overlong` the ones among them that were too
 * long, `lost` the bytes the UART could not hand over (an overrun, or a full
 * receive ring).
 *
 * Temperature. The answer is `TEMP=<degrees, one decimal> RAW=<n>\r\n`. RAW is
 * the mean of 16 conversions of ADC1 channel 18, the internal temperature
 * sensor (TSVREFE set and VBATE clear in ADC_CCR, 480 cycle sampling, 12 bits),
 * and TEMP is the reference manual's formula applied to it with the
 * datasheet's typical values: (VSENSE - V25) / Avg_Slope + 25, with VSENSE =
 * RAW * 3.3 V / 4095, V25 = 0.76 V and Avg_Slope = 2.5 mV per degree, rounded to
 * the nearest tenth. A conversion that does not finish is answered
 * `TEMP=unavailable\r\n`.
 *
 * Bounded everywhere: the receive ring holds 4096 bytes and the transmit ring
 * 2048, both served by the USART2 interrupt; a byte that cannot be queued for
 * half a second is dropped rather than waited for, and no wait in here is
 * without a limit.
 *
 * CAN. bxCAN CAN1 on PB8 (RX) and PB9 (TX), alternate function 9, with the
 * pull-up on PB8, so that with nothing on the pin it reads recessive. 500
 * kbit/s from the image's APB1 clock, which is the reset clock, HSI at 16 MHz:
 * a prescaler of 2 makes the time quantum 125 ns, and a bit is 16 quanta, one
 * for synchronisation, 13 before the sample point and 2 after it, which puts
 * the sample point at 87.5% of the bit. The resynchronisation jump width is
 * one quantum (CAN_BTR 0x001C0001 before the mode bits). Classic CAN only:
 * bxCAN has no CAN FD, and the CAN statistics say `fd=unsupported` rather
 * than leave it to be found out.
 *
 * Modes. From boot and after a reset the controller runs in silent loopback
 * (LBKM and SILM set): it hears every frame it sends, its answers included,
 * and drives nothing on PB9, so it needs neither a transceiver nor a second
 * node. Normal mode is the one a transceiver on PB8 and PB9 needs: the peer
 * then hears the other nodes and answers them, and no longer hears itself. A
 * frame no node acknowledges is sent again until one does. Switching the mode
 * restarts the controller: whatever was not sent by then is dropped and
 * counted as unsent, and the rules, the filters and the statistics stay.
 *
 * The CAN answer table is the container tier's CAN peer's
 * (tests/container/can_peer.py), spelled as its --reply takes it:
 * ID/DATA=ID/DATA, identifiers in hexadecimal with or without 0x, payloads as
 * pairs of hexadecimal digits, eight bytes at most, an empty payload written
 * as nothing (0x200/=0x201/ff). A standard data frame with a rule's
 * identifier and payload is answered with the rule's standard data frame,
 * extended and remote frames are heard, counted and never answered, and a
 * later rule for the same frame replaces the earlier one, all as there. What
 * loopback adds: the peer hears its own answers, so an answer that is itself
 * a frame some rule names is answered in turn.
 *
 * CAN control lines are answered like the others, `@peer ok can <command>...`
 * or `@peer error can <command> <reason>`, and `@peer error can syntax` when
 * the command is none of these:
 *
 *   @peer can mode loopback|normal   switch the controller to that mode
 *   @peer can rule ID/DATA=ID/DATA   answer that frame (8 rules at most)
 *   @peer can unrule ID/DATA         forget the rule for that frame
 *   @peer can clear                  forget every rule
 *   @peer can send [extended] ID/DATA [COUNT]
 *                                    send the frame COUNT times (1 to 100000,
 *                                    1 when left out), answered
 *                                    `@peer ok can send COUNT` once queued;
 *                                    DATA written R or R0 to R8 is a remote
 *                                    frame of that length. 8 sends wait at
 *                                    most, and answers go out before them
 *   @peer can filter [extended] ID MASK
 *                                    hear only what the acceptance filters
 *                                    take: a bxCAN filter bank in 32-bit mask
 *                                    mode each, taking the frames whose
 *                                    identifier equals ID in every bit MASK
 *                                    sets. A standard filter takes standard
 *                                    frames, an extended one extended frames,
 *                                    data and remote alike (14 at most)
 *   @peer can filter off             hear every frame again, as from boot
 *   @peer can stats                  `@peer ok can stats mode=M queued=N
 *                                    sent=N received=N answered=N
 *                                    digest=HHHHHHHH lost=N unsent=N tec=N
 *                                    rec=N state=S fd=unsupported`
 *   @peer can last                   the last frame heard, spelled the way a
 *                                    send spells it, or `none`
 *
 * Reasons: `syntax`, `range` (an identifier or mask over 0x7ff, or over
 * 0x1fffffff for an extended one, a remote length over 8, a count out of
 * bounds), `long` (a payload over 8 bytes), `full` (no free rule, filter or
 * send), `absent` (no such rule), `init` (the controller did not take the
 * mode, and runs in silent loopback again).
 *
 * CAN statistics, since boot or the last reset: `mode` loopback or normal, or
 * failed for a controller that would not start; `queued` the frames waiting
 * to be sent, the ones in the mailboxes included; `sent` the frames the
 * controller confirmed; `received` the frames heard; `answered` the frames a
 * rule answered; `lost` the frames heard and not kept (a receive FIFO
 * overrun, or the receive ring full); `unsent` the frames that will never be
 * sent (an answer that found 16 waiting, or what a mode switch dropped);
 * `tec` and `rec` the controller's error counters; `state` active, warning,
 * passive or busoff. `digest` covers every frame heard, in whatever order:
 * the sum modulo 2^32 of one CRC-32 per frame, as zlib.crc32 computes it,
 * over the frame's identifier word (4 bytes, most significant first, plus
 * 0x80000000 for an extended identifier and 0x40000000 for a remote frame),
 * its length code, and its data bytes, none for a remote frame. A sum,
 * because in loopback a frame and the answers to the frames before it meet in
 * an order nothing decides.
 *
 * The CAN receive ring holds 64 frames and is filled by the CAN1 RX0
 * interrupt, and every wait on the controller ends after 10 ms at most. The
 * acceptance filters are rewritten once the mailboxes are empty, so none of
 * the peer's own frames is on the bus meanwhile.
 *
 * Built by the bench tier as the demo's main.c, with the demo's startup code,
 * linker script and toolchain file, and put on the board through the product.
 */

#include <stdint.h>

#define RCC_AHB1ENR (*(volatile uint32_t *)0x40023830U)
#define RCC_APB1ENR (*(volatile uint32_t *)0x40023840U)
#define RCC_APB2ENR (*(volatile uint32_t *)0x40023844U)
#define GPIOA_MODER (*(volatile uint32_t *)0x40020000U)
#define GPIOA_AFRL (*(volatile uint32_t *)0x40020020U)
#define GPIOA_ODR (*(volatile uint32_t *)0x40020014U)
#define USART2_SR (*(volatile uint32_t *)0x40004400U)
#define USART2_DR (*(volatile uint32_t *)0x40004404U)
#define USART2_BRR (*(volatile uint32_t *)0x40004408U)
#define USART2_CR1 (*(volatile uint32_t *)0x4000440CU)
#define USART2_CR2 (*(volatile uint32_t *)0x40004410U)
#define USART2_CR3 (*(volatile uint32_t *)0x40004414U)
#define ADC1_SR (*(volatile uint32_t *)0x40012000U)
#define ADC1_CR1 (*(volatile uint32_t *)0x40012004U)
#define ADC1_CR2 (*(volatile uint32_t *)0x40012008U)
#define ADC1_SMPR1 (*(volatile uint32_t *)0x4001200CU)
#define ADC1_SQR1 (*(volatile uint32_t *)0x4001202CU)
#define ADC1_SQR3 (*(volatile uint32_t *)0x40012034U)
#define ADC1_DR (*(volatile uint32_t *)0x4001204CU)
#define ADC_CCR (*(volatile uint32_t *)0x40012304U)
#define NVIC_ISER1 (*(volatile uint32_t *)0xE000E104U)
#define SCB_CPACR (*(volatile uint32_t *)0xE000ED88U)
#define SYST_CSR (*(volatile uint32_t *)0xE000E010U)
#define SYST_RVR (*(volatile uint32_t *)0xE000E014U)
#define SYST_CVR (*(volatile uint32_t *)0xE000E018U)
#define RCC_APB1RSTR (*(volatile uint32_t *)0x40023820U)
#define GPIOB_MODER (*(volatile uint32_t *)0x40020400U)
#define GPIOB_PUPDR (*(volatile uint32_t *)0x4002040CU)
#define GPIOB_AFRH (*(volatile uint32_t *)0x40020424U)
#define NVIC_ISER0 (*(volatile uint32_t *)0xE000E100U)
#define CAN1_MCR (*(volatile uint32_t *)0x40006400U)
#define CAN1_MSR (*(volatile uint32_t *)0x40006404U)
#define CAN1_TSR (*(volatile uint32_t *)0x40006408U)
#define CAN1_RF0R (*(volatile uint32_t *)0x4000640CU)
#define CAN1_IER (*(volatile uint32_t *)0x40006414U)
#define CAN1_ESR (*(volatile uint32_t *)0x40006418U)
#define CAN1_BTR (*(volatile uint32_t *)0x4000641CU)
#define CAN1_TIR(box) (*(volatile uint32_t *)(0x40006580U + 0x10U * (box)))
#define CAN1_TDTR(box) (*(volatile uint32_t *)(0x40006584U + 0x10U * (box)))
#define CAN1_TDLR(box) (*(volatile uint32_t *)(0x40006588U + 0x10U * (box)))
#define CAN1_TDHR(box) (*(volatile uint32_t *)(0x4000658CU + 0x10U * (box)))
#define CAN1_RI0R (*(volatile uint32_t *)0x400065B0U)
#define CAN1_RDT0R (*(volatile uint32_t *)0x400065B4U)
#define CAN1_RDL0R (*(volatile uint32_t *)0x400065B8U)
#define CAN1_RDH0R (*(volatile uint32_t *)0x400065BCU)
#define CAN1_FMR (*(volatile uint32_t *)0x40006600U)
#define CAN1_FM1R (*(volatile uint32_t *)0x40006604U)
#define CAN1_FS1R (*(volatile uint32_t *)0x4000660CU)
#define CAN1_FFA1R (*(volatile uint32_t *)0x40006614U)
#define CAN1_FA1R (*(volatile uint32_t *)0x4000661CU)
#define CAN1_FR1(bank) (*(volatile uint32_t *)(0x40006640U + 8U * (bank)))
#define CAN1_FR2(bank) (*(volatile uint32_t *)(0x40006644U + 8U * (bank)))

#define GPIOAEN (1U << 0)
#define USART2EN (1U << 17)
#define ADC1EN (1U << 8)
#define LD2_PIN 5U
#define LD2_MODER_MASK (3U << (LD2_PIN * 2U))
#define LD2_MODER_OUTPUT (1U << (LD2_PIN * 2U))
#define USART2_TX_PIN 2U
#define USART2_RX_PIN 3U
#define GPIO_MODER_AF(pin) (2U << ((pin) * 2U))
#define GPIO_MODER_MASK(pin) (3U << ((pin) * 2U))
#define GPIO_AFRL_AF7(pin) (7U << ((pin) * 4U))
#define GPIO_AFRL_MASK(pin) (0xFU << ((pin) * 4U))
#define USART_SR_ORE (1U << 3)
#define USART_SR_RXNE (1U << 5)
#define USART_SR_TC (1U << 6)
#define USART_SR_TXE (1U << 7)
#define USART_CR1_RE (1U << 2)
#define USART_CR1_TE (1U << 3)
#define USART_CR1_RXNEIE (1U << 5)
#define USART_CR1_TXEIE (1U << 7)
#define USART_CR1_UE (1U << 13)
#define USART2_IRQ_BIT (1U << (38U - 32U))
#define ADC_SR_EOC (1U << 1)
#define ADC_CR2_ADON (1U << 0)
#define ADC_CR2_SWSTART (1U << 30)
#define ADC_CCR_ADCPRE_MASK (3U << 16)
#define ADC_CCR_VBATE (1U << 22)
#define ADC_CCR_TSVREFE (1U << 23)
#define ADC_SMPR1_SMP18_MASK (7U << 24)
#define ADC_SMPR1_SMP18_480 (7U << 24)
#define ADC_TEMPERATURE_CHANNEL 18U
#define SYST_CSR_ENABLE (1U << 0)
#define SYST_CSR_TICKINT (1U << 1)
#define SYST_CSR_CLKSOURCE (1U << 2)
#define SYSTICK_1MS_AT_16MHZ (16000U - 1U)
#define GPIOBEN (1U << 1)
#define CAN1EN (1U << 25)
#define CAN1RST (1U << 25)
#define CAN_RX_PIN 8U
#define CAN_TX_PIN 9U
#define GPIO_PUPDR_MASK(pin) (3U << ((pin) * 2U))
#define GPIO_PUPDR_UP(pin) (1U << ((pin) * 2U))
#define GPIO_AFRH_AF9(pin) (9U << (((pin) - 8U) * 4U))
#define GPIO_AFRH_MASK(pin) (0xFU << (((pin) - 8U) * 4U))
#define CAN1_RX0_IRQ_BIT (1U << 20)
#define CAN_MCR_INRQ (1U << 0)
#define CAN_MCR_TXFP (1U << 2)
#define CAN_MCR_DBF (1U << 16)
#define CAN_MSR_INAK (1U << 0)
#define CAN_MSR_SLAK (1U << 1)
#define CAN_TSR_RQCP0 (1U << 0)
#define CAN_TSR_TXOK0 (1U << 1)
#define CAN_TSR_ABRQ0 (1U << 7)
#define CAN_TSR_TME0 (1U << 26)
#define CAN_TSR_TME_ALL (7U << 26)
#define CAN_RF0R_FMP0 (3U << 0)
#define CAN_RF0R_FOVR0 (1U << 4)
#define CAN_RF0R_RFOM0 (1U << 5)
#define CAN_IER_FMPIE0 (1U << 1)
#define CAN_ESR_EWGF (1U << 0)
#define CAN_ESR_EPVF (1U << 1)
#define CAN_ESR_BOFF (1U << 2)
#define CAN_BTR_LBKM (1U << 30)
#define CAN_BTR_SILM (1U << 31)
#define CAN_ID_TXRQ (1U << 0)
#define CAN_ID_RTR (1U << 1)
#define CAN_ID_IDE (1U << 2)
#define CAN_FMR_FINIT (1U << 0)
/* 500 kbit/s from PCLK1 at 16 MHz: prescaler 2 (125 ns quanta), 13 quanta
 * before the sample point and 2 after it, a jump width of 1 (0x001C0001). */
#define CAN_BTR_PRESCALER 2U
#define CAN_BTR_SEGMENT1 13U
#define CAN_BTR_SEGMENT2 2U
#define CAN_BTR_JUMP 1U
#define CAN_BTR_500K (((CAN_BTR_JUMP - 1U) << 24) | ((CAN_BTR_SEGMENT2 - 1U) << 20) | \
                      ((CAN_BTR_SEGMENT1 - 1U) << 16) | (CAN_BTR_PRESCALER - 1U))

#define PCLK1_HZ 16000000U
#define BOOT_BAUD 115200U
#define BAUD_MIN 1200U
#define BAUD_MAX 1000000U

#define PREFIX_LEN 6U
#define LINE_MAX 3072U
#define RULES 8U
#define REQUEST_MAX 64U
#define RESPONSE_MAX 512U
#define PENDING_MAX 16U
#define RX_RING 4096U
#define TX_RING 2048U
#define TX_WAIT_MS 500U
#define DELAY_MAX_MS 60000U
#define ANNOUNCE_MAX_MS 60000U
#define FLOOD_MAX 100000000U
#define TEMPERATURE_SAMPLES 16U
#define ADC_WAIT_MS 5U
#define CAN_MAILBOXES 3U
#define CAN_RX_RING 64U
#define CAN_RULES 8U
#define CAN_ANSWERS 16U
#define CAN_SENDS 8U
#define CAN_FILTERS 14U
#define CAN_DATA_MAX 8U
#define CAN_STANDARD_MAX 0x7FFU
#define CAN_EXTENDED_MAX 0x1FFFFFFFU
#define CAN_SEND_MAX 100000U
#define CAN_WAIT_MS 10U
#define CAN_EXTENDED_FLAG 0x80000000U
#define CAN_REMOTE_FLAG 0x40000000U

#define CAN_MODE_LOOPBACK 0U
#define CAN_MODE_NORMAL 1U
#define CAN_MODE_FAILED 2U

#define CAN_OK 0U
#define CAN_SYNTAX 1U
#define CAN_RANGE 2U
#define CAN_LONG 3U

#define ESCAPE_BAD (-1)
#define ESCAPE_LONG (-2)

#define ANSWER_TEMPERATURE 0xFFU

static const uint8_t prefix[PREFIX_LEN] = {'@', 'p', 'e', 'e', 'r', ' '};

/* A millisecond uptime counter in RAM, the same witness the demo keeps. */
volatile uint32_t uptime_ms = 0U;

static volatile uint8_t rx_ring[RX_RING];
static volatile uint32_t rx_head;
static volatile uint32_t rx_tail;
static volatile uint32_t rx_lost;
static volatile uint8_t tx_ring[TX_RING];
static volatile uint32_t tx_head;
static volatile uint32_t tx_tail;
static uint8_t tx_stuck;

struct rule {
    uint8_t used;
    uint8_t request_len;
    uint16_t response_len;
    uint8_t request[REQUEST_MAX];
    uint8_t response[RESPONSE_MAX];
};

struct pending {
    uint8_t answer;
    uint32_t due_ms;
};

static struct rule rules[RULES];
static struct pending pending[PENDING_MAX];
static uint32_t pending_first;
static uint32_t pending_count;
static uint32_t last_due_ms;
static uint32_t delay_ms;
static uint8_t echo_on;
static uint8_t silent;
static uint8_t announce_on;
static uint32_t announce_every_ms;
static uint32_t announce_next_ms;
static uint32_t announce_len;
static uint8_t announce_text[RESPONSE_MAX];
static uint8_t flood_forever;
static uint32_t flood_remaining;
static uint32_t flood_position;
static uint8_t temperature_bound;
static uint32_t temperature_request_len;
static uint8_t temperature_request[REQUEST_MAX];
static uint32_t baud = BOOT_BAUD;

static uint32_t stat_bytes;
static uint32_t stat_crc = 0xFFFFFFFFU;
static uint32_t stat_lines;
static uint32_t stat_overlong;

static uint8_t line[LINE_MAX];
static uint32_t line_len;
static uint8_t line_overlong;
static uint8_t line_started;
static uint32_t prefix_seen;
static uint8_t line_kind;
static uint32_t snapshot_bytes;
static uint32_t snapshot_crc;

static uint8_t scratch_request[REQUEST_MAX];
static uint8_t scratch_response[RESPONSE_MAX];

#define LINE_UNDECIDED 0U
#define LINE_PAYLOAD 1U
#define LINE_CONTROL 2U

static void interrupts_off(void)
{
    __asm volatile ("cpsid i" ::: "memory");
}

static void interrupts_on(void)
{
    __asm volatile ("cpsie i" ::: "memory");
}

void SysTick_Handler(void)
{
    uptime_ms++;
}

void USART2_IRQHandler(void)
{
    uint32_t status = USART2_SR;

    if ((status & (USART_SR_RXNE | USART_SR_ORE)) != 0U) {
        /* Reading DR after SR clears both RXNE and an overrun. */
        uint8_t byte = (uint8_t)USART2_DR;
        uint32_t head = rx_head;

        if ((status & USART_SR_ORE) != 0U) {
            rx_lost++;
        }
        if (head - rx_tail < RX_RING) {
            rx_ring[head % RX_RING] = byte;
            rx_head = head + 1U;
        } else {
            rx_lost++;
        }
    }

    if ((USART2_CR1 & USART_CR1_TXEIE) != 0U && (status & USART_SR_TXE) != 0U) {
        uint32_t tail = tx_tail;

        if (tail != tx_head) {
            USART2_DR = tx_ring[tail % TX_RING];
            tx_tail = tail + 1U;
        } else {
            USART2_CR1 &= ~USART_CR1_TXEIE;
        }
    }
}

void SystemInit(void)
{
    /* Keep the reset clock defaults, HSI at 16 MHz on every bus, as the demo
     * does. The build uses the hard-float ABI, so the FPU is let in, although
     * nothing here computes in floating point. */
    SCB_CPACR |= (0xFU << 20);
}

static void systick_init(void)
{
    SYST_RVR = SYSTICK_1MS_AT_16MHZ;
    SYST_CVR = 0U;
    SYST_CSR = SYST_CSR_CLKSOURCE | SYST_CSR_TICKINT | SYST_CSR_ENABLE;
}

static void led_init(void)
{
    RCC_AHB1ENR |= GPIOAEN;
    GPIOA_MODER = (GPIOA_MODER & ~LD2_MODER_MASK) | LD2_MODER_OUTPUT;
}

static uint32_t divisor_for(uint32_t rate)
{
    return (PCLK1_HZ + rate / 2U) / rate;
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
    USART2_BRR = divisor_for(BOOT_BAUD);
    USART2_CR1 = USART_CR1_RE | USART_CR1_TE | USART_CR1_RXNEIE | USART_CR1_UE;
    NVIC_ISER1 = USART2_IRQ_BIT;
}

static void adc_init(void)
{
    RCC_APB2ENR |= ADC1EN;
    /* ADC clock PCLK2 / 2 = 8 MHz; the sensor on, VBAT off, because channel
     * 18 is shared and VBAT wins when both are enabled. */
    ADC_CCR = (ADC_CCR & ~(ADC_CCR_ADCPRE_MASK | ADC_CCR_VBATE)) | ADC_CCR_TSVREFE;
    ADC1_CR1 = 0U;
    ADC1_CR2 = 0U;
    /* 480 cycles at 8 MHz is 60 us, well over the sensor's minimum sampling time. */
    ADC1_SMPR1 = (ADC1_SMPR1 & ~ADC_SMPR1_SMP18_MASK) | ADC_SMPR1_SMP18_480;
    ADC1_SQR1 = 0U;
    ADC1_SQR3 = ADC_TEMPERATURE_CHANNEL;
    ADC1_CR2 = ADC_CR2_ADON;
}

static uint32_t tx_space(void)
{
    return TX_RING - (tx_head - tx_tail);
}

static void tx_kick(void)
{
    interrupts_off();
    USART2_CR1 |= USART_CR1_TXEIE;
    interrupts_on();
}

/* Queue one byte, waiting for room at most TX_WAIT_MS. Once a wait ran out,
 * later bytes are dropped at once until the ring has room again, so a stuck
 * line costs one wait and not one per byte. */
static void tx_put(uint8_t byte)
{
    uint32_t started = uptime_ms;

    while (tx_space() == 0U) {
        if (tx_stuck || uptime_ms - started > TX_WAIT_MS) {
            tx_stuck = 1U;
            return;
        }
    }
    tx_stuck = 0U;
    tx_ring[tx_head % TX_RING] = byte;
    tx_head = tx_head + 1U;
    tx_kick();
}

static void tx_discard(void)
{
    interrupts_off();
    USART2_CR1 &= ~USART_CR1_TXEIE;
    tx_tail = tx_head;
    interrupts_on();
}

static void put_bytes(const uint8_t *bytes, uint32_t count)
{
    for (uint32_t index = 0U; index < count; index++) {
        tx_put(bytes[index]);
    }
}

static void put_text(const char *text)
{
    while (*text != '\0') {
        tx_put((uint8_t)*text);
        text++;
    }
}

static void put_decimal(uint32_t value)
{
    char digits[10];
    uint32_t count = 0U;

    do {
        digits[count] = (char)('0' + (value % 10U));
        count++;
        value /= 10U;
    } while (value != 0U);
    while (count > 0U) {
        count--;
        tx_put((uint8_t)digits[count]);
    }
}

static void put_hex32(uint32_t value)
{
    static const char hex[] = "0123456789abcdef";

    for (int32_t shift = 28; shift >= 0; shift -= 4) {
        tx_put((uint8_t)hex[(value >> (uint32_t)shift) & 0xFU]);
    }
}

/* Wait until everything queued has left the pin, then run at `rate`. */
static void set_baud(uint32_t rate)
{
    uint32_t started = uptime_ms;

    while (tx_head != tx_tail && uptime_ms - started < 2000U) {
    }
    started = uptime_ms;
    while ((USART2_SR & USART_SR_TC) == 0U && uptime_ms - started < 100U) {
    }
    interrupts_off();
    USART2_CR1 &= ~USART_CR1_UE;
    USART2_BRR = divisor_for(rate);
    USART2_CR1 |= USART_CR1_UE;
    interrupts_on();
    baud = rate;
}

static uint32_t crc32_step(uint32_t crc, uint8_t byte)
{
    crc ^= byte;
    for (uint32_t bit = 0U; bit < 8U; bit++) {
        crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
    }
    return crc;
}

static int same_bytes(const uint8_t *left, uint32_t left_len, const uint8_t *right, uint32_t right_len)
{
    if (left_len != right_len) {
        return 0;
    }
    for (uint32_t index = 0U; index < left_len; index++) {
        if (left[index] != right[index]) {
            return 0;
        }
    }
    return 1;
}

static int is_word(const uint8_t *text, uint32_t len, const char *word)
{
    uint32_t index = 0U;

    while (word[index] != '\0') {
        if (index >= len || text[index] != (uint8_t)word[index]) {
            return 0;
        }
        index++;
    }
    return index == len;
}

/* A decimal number of one to nine digits and nothing else. */
static int parse_decimal(const uint8_t *text, uint32_t len, uint32_t *value)
{
    uint32_t result = 0U;

    if (len == 0U || len > 9U) {
        return 0;
    }
    for (uint32_t index = 0U; index < len; index++) {
        if (text[index] < '0' || text[index] > '9') {
            return 0;
        }
        result = result * 10U + (uint32_t)(text[index] - '0');
    }
    *value = result;
    return 1;
}

static int hex_digit(uint8_t character, uint32_t *value)
{
    if (character >= '0' && character <= '9') {
        *value = (uint32_t)(character - '0');
        return 1;
    }
    if (character >= 'a' && character <= 'f') {
        *value = (uint32_t)(character - 'a') + 10U;
        return 1;
    }
    if (character >= 'A' && character <= 'F') {
        *value = (uint32_t)(character - 'A') + 10U;
        return 1;
    }
    return 0;
}

/* `digits` hex digits at text[at], as one value; 0 when they are not all there. */
static int hex_value(const uint8_t *text, uint32_t len, uint32_t at, uint32_t digits, uint32_t *value)
{
    uint32_t result = 0U;

    if (at + digits > len) {
        return 0;
    }
    for (uint32_t index = 0U; index < digits; index++) {
        uint32_t digit;

        if (!hex_digit(text[at + index], &digit)) {
            return 0;
        }
        result = (result << 4) | digit;
    }
    *value = result;
    return 1;
}

/* text[0..len) read under the escape rules into out (room for `room`
 * bytes); the number of bytes, or ESCAPE_BAD / ESCAPE_LONG. */
static int32_t unescape(const uint8_t *text, uint32_t len, uint8_t *out, uint32_t room)
{
    uint32_t written = 0U;
    uint32_t index = 0U;

    while (index < len) {
        uint32_t value = text[index];
        uint32_t used = 1U;

        if (text[index] == '\\') {
            if (index + 1U >= len) {
                return ESCAPE_BAD;
            }
            used = 2U;
            switch (text[index + 1U]) {
            case '\\': value = '\\'; break;
            case '\'': value = '\''; break;
            case '"': value = '"'; break;
            case 'a': value = 0x07U; break;
            case 'b': value = 0x08U; break;
            case 'f': value = 0x0CU; break;
            case 'n': value = 0x0AU; break;
            case 'r': value = 0x0DU; break;
            case 't': value = 0x09U; break;
            case 'v': value = 0x0BU; break;
            case 'x':
                if (!hex_value(text, len, index + 2U, 2U, &value)) {
                    return ESCAPE_BAD;
                }
                used = 4U;
                break;
            case 'u':
                if (!hex_value(text, len, index + 2U, 4U, &value) || value > 0xFFU) {
                    return ESCAPE_BAD;
                }
                used = 6U;
                break;
            case 'U':
                if (!hex_value(text, len, index + 2U, 8U, &value) || value > 0xFFU) {
                    return ESCAPE_BAD;
                }
                used = 10U;
                break;
            case 'N':
                /* A character by name: no byte to be had from it here. */
                return ESCAPE_BAD;
            case '0': case '1': case '2': case '3':
            case '4': case '5': case '6': case '7':
                value = 0U;
                used = 1U;
                while (used <= 3U && index + used < len && text[index + used] >= '0' && text[index + used] <= '7') {
                    value = value * 8U + (uint32_t)(text[index + used] - '0');
                    used++;
                }
                if (value > 0xFFU) {
                    return ESCAPE_BAD;
                }
                break;
            default:
                /* Python keeps an unknown escape as written: the backslash
                 * here, and the character after it on the next pass. */
                value = '\\';
                used = 1U;
                break;
            }
        }
        if (written >= room) {
            return ESCAPE_LONG;
        }
        out[written] = (uint8_t)value;
        written++;
        index += used;
    }
    return (int32_t)written;
}

static void reply_ok(const char *command)
{
    put_text("@peer ok ");
    put_text(command);
}

static void reply_end(void)
{
    put_text("\r\n");
}

static void reply_error(const char *command, const char *reason)
{
    put_text("@peer error ");
    put_text(command);
    put_text(" ");
    put_text(reason);
    reply_end();
}

static void drop_pending(void)
{
    pending_first = 0U;
    pending_count = 0U;
    last_due_ms = uptime_ms;
}

/* The mean of TEMPERATURE_SAMPLES conversions of the sensor; 0 when one did not finish. */
static int measure_temperature(uint32_t *raw)
{
    uint32_t sum = 0U;

    for (uint32_t sample = 0U; sample < TEMPERATURE_SAMPLES; sample++) {
        uint32_t started = uptime_ms;

        ADC1_SR = 0U;
        ADC1_CR2 |= ADC_CR2_SWSTART;
        while ((ADC1_SR & ADC_SR_EOC) == 0U) {
            if (uptime_ms - started > ADC_WAIT_MS) {
                return 0;
            }
        }
        sum += ADC1_DR & 0xFFFU;
    }
    *raw = (sum + TEMPERATURE_SAMPLES / 2U) / TEMPERATURE_SAMPLES;
    return 1;
}

static int32_t divide_rounded(int32_t numerator, int32_t denominator)
{
    if (numerator >= 0) {
        return (numerator + denominator / 2) / denominator;
    }
    return -((-numerator + denominator / 2) / denominator);
}

static void answer_temperature(void)
{
    uint32_t raw;

    if (!measure_temperature(&raw)) {
        put_text("TEMP=unavailable\r\n");
        return;
    }
    /* VSENSE in units of 10 uV, then (VSENSE - 0.76 V) / 2.5 mV + 25 in tenths of a degree. */
    int32_t vsense = (int32_t)((raw * 330000U + 2047U) / 4095U);
    int32_t tenths = 250 + divide_rounded(vsense - 76000, 25);
    uint32_t magnitude;

    put_text("TEMP=");
    if (tenths < 0) {
        put_text("-");
        magnitude = (uint32_t)(-tenths);
    } else {
        magnitude = (uint32_t)tenths;
    }
    put_decimal(magnitude / 10U);
    put_text(".");
    put_decimal(magnitude % 10U);
    put_text(" RAW=");
    put_decimal(raw);
    reply_end();
}

static void write_answer(uint8_t answer)
{
    if (answer == ANSWER_TEMPERATURE) {
        answer_temperature();
        return;
    }
    if (answer < RULES && rules[answer].used) {
        put_bytes(rules[answer].response, rules[answer].response_len);
    }
}

static void schedule(uint8_t answer)
{
    uint32_t now = uptime_ms;
    uint32_t start;

    if (delay_ms == 0U && pending_count == 0U) {
        write_answer(answer);
        return;
    }
    if (pending_count == PENDING_MAX) {
        return;
    }
    start = ((int32_t)(last_due_ms - now) > 0) ? last_due_ms : now;
    last_due_ms = start + delay_ms;
    pending[(pending_first + pending_count) % PENDING_MAX].answer = answer;
    pending[(pending_first + pending_count) % PENDING_MAX].due_ms = last_due_ms;
    pending_count++;
}

static void answer_line(const uint8_t *text, uint32_t len)
{
    if (silent) {
        return;
    }
    if (temperature_bound && same_bytes(text, len, temperature_request, temperature_request_len)) {
        schedule(ANSWER_TEMPERATURE);
        return;
    }
    for (uint32_t index = 0U; index < RULES; index++) {
        if (rules[index].used && same_bytes(text, len, rules[index].request, rules[index].request_len)) {
            schedule((uint8_t)index);
            return;
        }
    }
}

static void command_rule(const uint8_t *arguments, uint32_t len)
{
    uint32_t split = 0U;
    int32_t request_len;
    int32_t response_len;
    uint32_t slot = RULES;

    while (split < len && arguments[split] != '=') {
        split++;
    }
    if (split == len) {
        reply_error("rule", "syntax");
        return;
    }
    request_len = unescape(arguments, split, scratch_request, REQUEST_MAX);
    response_len = unescape(arguments + split + 1U, len - split - 1U, scratch_response, RESPONSE_MAX);
    if (request_len == ESCAPE_BAD || response_len == ESCAPE_BAD) {
        reply_error("rule", "escape");
        return;
    }
    if (request_len < 0 || response_len < 0) {
        reply_error("rule", "long");
        return;
    }
    for (uint32_t index = 0U; index < RULES; index++) {
        if (rules[index].used && same_bytes(rules[index].request, rules[index].request_len, scratch_request, (uint32_t)request_len)) {
            slot = index;
            break;
        }
    }
    if (slot == RULES) {
        for (uint32_t index = 0U; index < RULES; index++) {
            if (!rules[index].used) {
                slot = index;
                break;
            }
        }
    }
    if (slot == RULES) {
        reply_error("rule", "full");
        return;
    }
    for (int32_t index = 0; index < request_len; index++) {
        rules[slot].request[index] = scratch_request[index];
    }
    for (int32_t index = 0; index < response_len; index++) {
        rules[slot].response[index] = scratch_response[index];
    }
    rules[slot].request_len = (uint8_t)request_len;
    rules[slot].response_len = (uint16_t)response_len;
    rules[slot].used = 1U;
    drop_pending();
    reply_ok("rule");
    reply_end();
}

static void command_unrule(const uint8_t *arguments, uint32_t len)
{
    int32_t request_len = unescape(arguments, len, scratch_request, REQUEST_MAX);

    if (request_len == ESCAPE_BAD) {
        reply_error("unrule", "escape");
        return;
    }
    if (request_len < 0) {
        reply_error("unrule", "long");
        return;
    }
    for (uint32_t index = 0U; index < RULES; index++) {
        if (rules[index].used && same_bytes(rules[index].request, rules[index].request_len, scratch_request, (uint32_t)request_len)) {
            rules[index].used = 0U;
            drop_pending();
            reply_ok("unrule");
            reply_end();
            return;
        }
    }
    reply_error("unrule", "absent");
}

static void clear_rules(void)
{
    for (uint32_t index = 0U; index < RULES; index++) {
        rules[index].used = 0U;
    }
}

static void command_delay(const uint8_t *arguments, uint32_t len)
{
    uint32_t value;

    if (!parse_decimal(arguments, len, &value)) {
        reply_error("delay", "syntax");
        return;
    }
    if (value > DELAY_MAX_MS) {
        reply_error("delay", "range");
        return;
    }
    delay_ms = value;
    drop_pending();
    reply_ok("delay ");
    put_decimal(value);
    reply_end();
}

static void command_announce(const uint8_t *arguments, uint32_t len)
{
    uint32_t split = 0U;
    uint32_t period;
    int32_t text_len;

    if (is_word(arguments, len, "off")) {
        announce_on = 0U;
        reply_ok("announce off");
        reply_end();
        return;
    }
    while (split < len && arguments[split] != ' ') {
        split++;
    }
    if (split == len || !parse_decimal(arguments, split, &period)) {
        reply_error("announce", "syntax");
        return;
    }
    if (period == 0U || period > ANNOUNCE_MAX_MS) {
        reply_error("announce", "range");
        return;
    }
    text_len = unescape(arguments + split + 1U, len - split - 1U, scratch_response, RESPONSE_MAX);
    if (text_len == ESCAPE_BAD) {
        reply_error("announce", "escape");
        return;
    }
    if (text_len < 0) {
        reply_error("announce", "long");
        return;
    }
    if (text_len == 0) {
        reply_error("announce", "syntax");
        return;
    }
    for (int32_t index = 0; index < text_len; index++) {
        announce_text[index] = scratch_response[index];
    }
    announce_len = (uint32_t)text_len;
    announce_every_ms = period;
    announce_on = 1U;
    reply_ok("announce ");
    put_decimal(period);
    reply_end();
    /* The first one right after this answer, as the container peer writes its
     * first announcement the moment its end is open. */
    announce_next_ms = uptime_ms;
}

static int switch_argument(const uint8_t *arguments, uint32_t len, uint8_t *value)
{
    if (is_word(arguments, len, "on")) {
        *value = 1U;
        return 1;
    }
    if (is_word(arguments, len, "off")) {
        *value = 0U;
        return 1;
    }
    return 0;
}

static void command_echo(const uint8_t *arguments, uint32_t len)
{
    uint8_t value;

    if (!switch_argument(arguments, len, &value)) {
        reply_error("echo", "syntax");
        return;
    }
    echo_on = value;
    reply_ok(value ? "echo on" : "echo off");
    reply_end();
}

static void command_flood(const uint8_t *arguments, uint32_t len)
{
    uint8_t forever;
    uint32_t count;

    if (switch_argument(arguments, len, &forever)) {
        flood_forever = forever;
        flood_remaining = 0U;
        flood_position = 0U;
        reply_ok(forever ? "flood on" : "flood off");
        reply_end();
        return;
    }
    if (!parse_decimal(arguments, len, &count)) {
        reply_error("flood", "syntax");
        return;
    }
    if (count == 0U || count > FLOOD_MAX) {
        reply_error("flood", "range");
        return;
    }
    reply_ok("flood ");
    put_decimal(count);
    reply_end();
    flood_forever = 0U;
    flood_remaining = count;
    flood_position = 0U;
}

static void command_baud(const uint8_t *arguments, uint32_t len)
{
    uint32_t rate;

    if (!parse_decimal(arguments, len, &rate)) {
        reply_error("baud", "syntax");
        return;
    }
    if (rate < BAUD_MIN || rate > BAUD_MAX) {
        reply_error("baud", "range");
        return;
    }
    reply_ok("baud ");
    put_decimal(rate);
    reply_end();
    set_baud(rate);
}

static void command_silence(const uint8_t *arguments, uint32_t len)
{
    uint8_t value;

    if (!switch_argument(arguments, len, &value)) {
        reply_error("silence", "syntax");
        return;
    }
    silent = value;
    if (silent) {
        drop_pending();
        flood_forever = 0U;
        flood_remaining = 0U;
    }
    reply_ok(value ? "silence on" : "silence off");
    reply_end();
}

static void command_temp(const uint8_t *arguments, uint32_t len, int has_arguments)
{
    int32_t request_len;

    if (!has_arguments) {
        temperature_bound = 0U;
        drop_pending();
        reply_ok("temp off");
        reply_end();
        return;
    }
    request_len = unescape(arguments, len, scratch_request, REQUEST_MAX);
    if (request_len == ESCAPE_BAD) {
        reply_error("temp", "escape");
        return;
    }
    if (request_len < 0) {
        reply_error("temp", "long");
        return;
    }
    for (int32_t index = 0; index < request_len; index++) {
        temperature_request[index] = scratch_request[index];
    }
    temperature_request_len = (uint32_t)request_len;
    temperature_bound = 1U;
    drop_pending();
    reply_ok("temp");
    reply_end();
}

struct can_frame {
    uint32_t identifier;
    uint8_t extended;
    uint8_t remote;
    /* The length code as sent or received: the data bytes of a data frame,
     * the requested length of a remote frame. */
    uint8_t length;
    uint8_t data[CAN_DATA_MAX];
};

/* A frame as the receive interrupt takes it out of FIFO 0. */
struct can_received {
    uint32_t identifier_register;
    uint32_t length_register;
    uint32_t low;
    uint32_t high;
};

struct can_rule {
    uint8_t used;
    struct can_frame heard;
    struct can_frame answer;
};

struct can_send {
    struct can_frame frame;
    uint32_t remaining;
};

struct can_filter {
    uint8_t extended;
    uint32_t identifier;
    uint32_t mask;
};

static const char can_hex[] = "0123456789abcdef";

static volatile struct can_received can_rx_ring[CAN_RX_RING];
static volatile uint32_t can_rx_head;
static volatile uint32_t can_rx_tail;
static volatile uint32_t can_lost;
static struct can_rule can_rules[CAN_RULES];
static struct can_frame can_answers[CAN_ANSWERS];
static uint32_t can_answers_first;
static uint32_t can_answers_count;
static struct can_send can_sends[CAN_SENDS];
static uint32_t can_sends_first;
static uint32_t can_sends_count;
static struct can_filter can_filters[CAN_FILTERS];
static uint32_t can_filter_count;
static uint32_t can_mode;
static uint32_t can_sent;
static uint32_t can_received;
static uint32_t can_answered;
static uint32_t can_digest;
static uint32_t can_unsent;
static uint8_t can_heard_any;
static struct can_frame can_last;
static uint32_t can_crc_table[256];

void CAN1_RX0_IRQHandler(void)
{
    for (uint32_t taken = 0U; taken < 3U && (CAN1_RF0R & CAN_RF0R_FMP0) != 0U; taken++) {
        uint32_t head = can_rx_head;

        if (head - can_rx_tail < CAN_RX_RING) {
            can_rx_ring[head % CAN_RX_RING].identifier_register = CAN1_RI0R;
            can_rx_ring[head % CAN_RX_RING].length_register = CAN1_RDT0R;
            can_rx_ring[head % CAN_RX_RING].low = CAN1_RDL0R;
            can_rx_ring[head % CAN_RX_RING].high = CAN1_RDH0R;
            can_rx_head = head + 1U;
        } else {
            can_lost++;
        }
        CAN1_RF0R = CAN_RF0R_RFOM0;
        /* FMP0 counts the frame just released until the release is done, a
         * few clock cycles; waited for, so no frame is read twice. */
        for (uint32_t wait = 0U; wait < 64U && (CAN1_RF0R & CAN_RF0R_RFOM0) != 0U; wait++) {
        }
    }
    if ((CAN1_RF0R & CAN_RF0R_FOVR0) != 0U) {
        CAN1_RF0R = CAN_RF0R_FOVR0;
        can_lost++;
    }
}

/* Wait until the bits `mask` of a controller register read `value`, at most
 * CAN_WAIT_MS; 0 when they did not. */
static int can_wait(volatile uint32_t *reg, uint32_t mask, uint32_t value)
{
    uint32_t started = uptime_ms;

    while ((*reg & mask) != value) {
        if (uptime_ms - started > CAN_WAIT_MS) {
            return 0;
        }
    }
    return 1;
}

static uint32_t can_data_length(const struct can_frame *frame)
{
    if (frame->remote) {
        return 0U;
    }
    return frame->length > CAN_DATA_MAX ? CAN_DATA_MAX : frame->length;
}

static uint32_t can_crc_byte(uint32_t crc, uint8_t byte)
{
    return can_crc_table[(crc ^ byte) & 0xFFU] ^ (crc >> 8);
}

/* The frame's CRC-32 as the digest counts it: identifier word, length code, data. */
static uint32_t can_frame_crc(const struct can_frame *frame)
{
    uint32_t word = frame->identifier | (frame->extended ? CAN_EXTENDED_FLAG : 0U) | (frame->remote ? CAN_REMOTE_FLAG : 0U);
    uint32_t crc = 0xFFFFFFFFU;

    for (int32_t shift = 24; shift >= 0; shift -= 8) {
        crc = can_crc_byte(crc, (uint8_t)(word >> (uint32_t)shift));
    }
    crc = can_crc_byte(crc, frame->length);
    for (uint32_t index = 0U; index < can_data_length(frame); index++) {
        crc = can_crc_byte(crc, frame->data[index]);
    }
    return ~crc;
}

static int can_same_frame(const struct can_frame *left, const struct can_frame *right)
{
    if (left->identifier != right->identifier || left->extended != right->extended || left->remote != right->remote) {
        return 0;
    }
    if (left->remote) {
        return left->length == right->length;
    }
    return same_bytes(left->data, can_data_length(left), right->data, can_data_length(right));
}

/* Every mailbox whose request completed counted, as sent or as unsent (an
 * abort), and made ready for the next frame. */
static void can_account(void)
{
    uint32_t status = CAN1_TSR;
    uint32_t done = 0U;

    for (uint32_t box = 0U; box < CAN_MAILBOXES; box++) {
        uint32_t shift = 8U * box;

        if ((status & (CAN_TSR_RQCP0 << shift)) != 0U) {
            if ((status & (CAN_TSR_TXOK0 << shift)) != 0U) {
                can_sent++;
            } else {
                can_unsent++;
            }
            done |= CAN_TSR_RQCP0 << shift;
        }
    }
    if (done != 0U) {
        CAN1_TSR = done;
    }
}

static uint32_t can_mailboxes_busy(void)
{
    uint32_t status = CAN1_TSR;
    uint32_t busy = 0U;

    for (uint32_t box = 0U; box < CAN_MAILBOXES; box++) {
        if ((status & (CAN_TSR_TME0 << box)) == 0U) {
            busy++;
        }
    }
    return busy;
}

static uint32_t can_queued(void)
{
    uint32_t queued = can_answers_count + can_mailboxes_busy();

    for (uint32_t index = 0U; index < can_sends_count; index++) {
        queued += can_sends[(can_sends_first + index) % CAN_SENDS].remaining;
    }
    return queued;
}

static void can_queue_answer(const struct can_frame *answer)
{
    if (can_answers_count == CAN_ANSWERS) {
        can_unsent++;
        return;
    }
    can_answers[(can_answers_first + can_answers_count) % CAN_ANSWERS] = *answer;
    can_answers_count++;
    can_answered++;
}

/* Every frame the receive interrupt kept: counted, added to the digest, and
 * answered when it is a standard data frame a rule names. */
static void can_take_received(void)
{
    while (can_rx_tail != can_rx_head) {
        uint32_t slot = can_rx_tail % CAN_RX_RING;
        uint32_t identifier_register = can_rx_ring[slot].identifier_register;
        uint32_t low = can_rx_ring[slot].low;
        uint32_t high = can_rx_ring[slot].high;
        struct can_frame frame;

        frame.extended = (identifier_register & CAN_ID_IDE) != 0U ? 1U : 0U;
        frame.remote = (identifier_register & CAN_ID_RTR) != 0U ? 1U : 0U;
        frame.identifier = frame.extended ? identifier_register >> 3 : identifier_register >> 21;
        frame.length = (uint8_t)(can_rx_ring[slot].length_register & 0xFU);
        for (uint32_t index = 0U; index < 4U; index++) {
            frame.data[index] = (uint8_t)(low >> (8U * index));
            frame.data[index + 4U] = (uint8_t)(high >> (8U * index));
        }
        can_rx_tail = can_rx_tail + 1U;

        can_received++;
        can_digest += can_frame_crc(&frame);
        can_last = frame;
        can_heard_any = 1U;
        if (!frame.extended && !frame.remote) {
            for (uint32_t index = 0U; index < CAN_RULES; index++) {
                if (can_rules[index].used && can_same_frame(&can_rules[index].heard, &frame)) {
                    can_queue_answer(&can_rules[index].answer);
                    break;
                }
            }
        }
    }
}

/* The next frame to send: an answer when one waits, else the next send. */
static int can_next_frame(struct can_frame *frame)
{
    if (can_answers_count > 0U) {
        *frame = can_answers[can_answers_first];
        can_answers_first = (can_answers_first + 1U) % CAN_ANSWERS;
        can_answers_count--;
        return 1;
    }
    if (can_sends_count > 0U) {
        struct can_send *send = &can_sends[can_sends_first];

        *frame = send->frame;
        send->remaining--;
        if (send->remaining == 0U) {
            can_sends_first = (can_sends_first + 1U) % CAN_SENDS;
            can_sends_count--;
        }
        return 1;
    }
    return 0;
}

static void can_load(uint32_t box, const struct can_frame *frame)
{
    uint32_t identifier_register = frame->extended ? ((frame->identifier << 3) | CAN_ID_IDE) : (frame->identifier << 21);

    if (frame->remote) {
        identifier_register |= CAN_ID_RTR;
    }
    CAN1_TIR(box) = identifier_register;
    CAN1_TDTR(box) = frame->length;
    CAN1_TDLR(box) = (uint32_t)frame->data[0] | ((uint32_t)frame->data[1] << 8) | ((uint32_t)frame->data[2] << 16) |
                     ((uint32_t)frame->data[3] << 24);
    CAN1_TDHR(box) = (uint32_t)frame->data[4] | ((uint32_t)frame->data[5] << 8) | ((uint32_t)frame->data[6] << 16) |
                     ((uint32_t)frame->data[7] << 24);
    CAN1_TIR(box) = identifier_register | CAN_ID_TXRQ;
}

/* Every empty mailbox given the next frame. TXFP is set, so the controller
 * sends them in the order they were loaded. */
static void can_fill_mailboxes(void)
{
    for (uint32_t box = 0U; box < CAN_MAILBOXES; box++) {
        uint32_t status = CAN1_TSR;
        struct can_frame frame;

        if ((status & (CAN_TSR_TME0 << box)) == 0U) {
            continue;
        }
        if ((status & (CAN_TSR_RQCP0 << (8U * box))) != 0U) {
            /* Loading the mailbox clears its completion, so it is counted first. */
            can_account();
        }
        if (!can_next_frame(&frame)) {
            return;
        }
        can_load(box, &frame);
    }
}

static void can_serve(void)
{
    can_account();
    can_take_received();
    can_fill_mailboxes();
}

/* The acceptance filters into the filter banks, CAN1's 0 to 13, each in
 * 32-bit mask mode and to FIFO 0; with none, bank 0 takes every frame. */
static void can_write_filters(void)
{
    uint32_t banks = 1U;

    CAN1_FMR |= CAN_FMR_FINIT;
    CAN1_FA1R = 0U;
    CAN1_FM1R = 0U;
    CAN1_FFA1R = 0U;
    if (can_filter_count == 0U) {
        CAN1_FR1(0U) = 0U;
        CAN1_FR2(0U) = 0U;
    } else {
        banks = 0U;
        for (uint32_t bank = 0U; bank < can_filter_count; bank++) {
            const struct can_filter *filter = &can_filters[bank];

            if (filter->extended) {
                CAN1_FR1(bank) = (filter->identifier << 3) | CAN_ID_IDE;
                CAN1_FR2(bank) = (filter->mask << 3) | CAN_ID_IDE;
            } else {
                CAN1_FR1(bank) = filter->identifier << 21;
                CAN1_FR2(bank) = (filter->mask << 21) | CAN_ID_IDE;
            }
            banks |= 1U << bank;
        }
    }
    CAN1_FS1R = banks;
    CAN1_FA1R = banks;
    CAN1_FMR &= ~CAN_FMR_FINIT;
}

/* The controller reset through RCC, set up again from what the peer keeps
 * (the mode asked for, the filters) and started; 0 when it would not start. */
static int can_start(uint32_t mode)
{
    RCC_APB1RSTR |= CAN1RST;
    RCC_APB1RSTR &= ~CAN1RST;
    interrupts_off();
    can_rx_tail = can_rx_head;
    interrupts_on();

    /* Out of sleep and into initialization, where the timing can be set. */
    CAN1_MCR = CAN_MCR_DBF | CAN_MCR_TXFP | CAN_MCR_INRQ;
    if (!can_wait(&CAN1_MSR, CAN_MSR_INAK | CAN_MSR_SLAK, CAN_MSR_INAK)) {
        return 0;
    }
    CAN1_BTR = CAN_BTR_500K | (mode == CAN_MODE_LOOPBACK ? (CAN_BTR_LBKM | CAN_BTR_SILM) : 0U);
    can_write_filters();
    CAN1_IER = CAN_IER_FMPIE0;
    /* Leaving initialization takes 11 recessive bits on the receive side:
     * the controller's own in loopback, PB8 in normal mode. */
    CAN1_MCR &= ~CAN_MCR_INRQ;
    return can_wait(&CAN1_MSR, CAN_MSR_INAK, 0U);
}

static void can_drop_queues(void)
{
    can_unsent += can_answers_count;
    for (uint32_t index = 0U; index < can_sends_count; index++) {
        can_unsent += can_sends[(can_sends_first + index) % CAN_SENDS].remaining;
    }
    can_answers_first = 0U;
    can_answers_count = 0U;
    can_sends_first = 0U;
    can_sends_count = 0U;
}

/* Whatever was not sent dropped and counted, what was heard kept, and the
 * controller restarted in `mode`; 0 when it would not take it, and then it
 * runs in silent loopback again. */
static int can_switch(uint32_t mode)
{
    CAN1_TSR = CAN_TSR_ABRQ0 | (CAN_TSR_ABRQ0 << 8) | (CAN_TSR_ABRQ0 << 16);
    (void)can_wait(&CAN1_TSR, CAN_TSR_TME_ALL, CAN_TSR_TME_ALL);
    can_account();
    can_take_received();
    can_unsent += can_mailboxes_busy();
    can_drop_queues();
    if (can_start(mode)) {
        can_mode = mode;
        return 1;
    }
    can_mode = can_start(CAN_MODE_LOOPBACK) ? CAN_MODE_LOOPBACK : CAN_MODE_FAILED;
    return 0;
}

/* The filters rewritten once the mailboxes are empty, so none of the peer's
 * own frames is on the bus while the banks change. */
static void can_apply_filters(void)
{
    (void)can_wait(&CAN1_TSR, CAN_TSR_TME_ALL, CAN_TSR_TME_ALL);
    can_account();
    can_take_received();
    can_write_filters();
}

static void can_clear_rules(void)
{
    for (uint32_t index = 0U; index < CAN_RULES; index++) {
        can_rules[index].used = 0U;
    }
}

/* The CAN half at its boot defaults: silent loopback, no rules, no filters,
 * nothing queued, the statistics zeroed. */
static void can_reset(void)
{
    can_clear_rules();
    can_answers_first = 0U;
    can_answers_count = 0U;
    can_sends_first = 0U;
    can_sends_count = 0U;
    can_filter_count = 0U;
    can_mode = can_start(CAN_MODE_LOOPBACK) ? CAN_MODE_LOOPBACK : CAN_MODE_FAILED;
    can_sent = 0U;
    can_received = 0U;
    can_answered = 0U;
    can_digest = 0U;
    can_unsent = 0U;
    can_lost = 0U;
    can_heard_any = 0U;
}

static void can_init(void)
{
    RCC_AHB1ENR |= GPIOBEN;
    RCC_APB1ENR |= CAN1EN;
    GPIOB_AFRH = (GPIOB_AFRH & ~(GPIO_AFRH_MASK(CAN_RX_PIN) | GPIO_AFRH_MASK(CAN_TX_PIN))) |
                 GPIO_AFRH_AF9(CAN_RX_PIN) | GPIO_AFRH_AF9(CAN_TX_PIN);
    GPIOB_PUPDR = (GPIOB_PUPDR & ~GPIO_PUPDR_MASK(CAN_RX_PIN)) | GPIO_PUPDR_UP(CAN_RX_PIN);
    GPIOB_MODER = (GPIOB_MODER & ~(GPIO_MODER_MASK(CAN_RX_PIN) | GPIO_MODER_MASK(CAN_TX_PIN))) |
                  GPIO_MODER_AF(CAN_RX_PIN) | GPIO_MODER_AF(CAN_TX_PIN);
    for (uint32_t index = 0U; index < 256U; index++) {
        can_crc_table[index] = crc32_step(0U, (uint8_t)index);
    }
    NVIC_ISER0 = CAN1_RX0_IRQ_BIT;
    can_reset();
}

/* Whether text starts with `word` and a space; if so, both are taken off it. */
static int take_word(const uint8_t **text, uint32_t *len, const char *word)
{
    uint32_t index = 0U;

    while (word[index] != '\0') {
        if (index >= *len || (*text)[index] != (uint8_t)word[index]) {
            return 0;
        }
        index++;
    }
    if (index >= *len || (*text)[index] != ' ') {
        return 0;
    }
    *text += index + 1U;
    *len -= index + 1U;
    return 1;
}

static const char *can_reason(uint32_t code)
{
    if (code == CAN_RANGE) {
        return "range";
    }
    if (code == CAN_LONG) {
        return "long";
    }
    return "syntax";
}

/* An identifier or mask as the container peer's int(text, 16) reads the
 * spellings its table documents: 0x or 0X or nothing, then hex digits. */
static uint32_t can_parse_number(const uint8_t *text, uint32_t len, uint32_t max, uint32_t *value)
{
    uint32_t at = 0U;
    uint32_t result = 0U;
    int over = 0;

    if (len >= 2U && text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) {
        at = 2U;
    }
    if (at == len) {
        return CAN_SYNTAX;
    }
    for (; at < len; at++) {
        uint32_t digit;

        if (!hex_digit(text[at], &digit)) {
            return CAN_SYNTAX;
        }
        if (result > 0x0FFFFFFFU) {
            over = 1;
        }
        result = (result << 4) | digit;
    }
    if (over || result > max) {
        return CAN_RANGE;
    }
    *value = result;
    return CAN_OK;
}

/* A payload as bytes.fromhex reads it: pairs of hex digits, none for an empty one. */
static uint32_t can_parse_data(const uint8_t *text, uint32_t len, struct can_frame *frame)
{
    uint32_t value;

    for (uint32_t index = 0U; index < len; index++) {
        if (!hex_digit(text[index], &value)) {
            return CAN_SYNTAX;
        }
    }
    if (len % 2U != 0U) {
        return CAN_SYNTAX;
    }
    if (len / 2U > CAN_DATA_MAX) {
        return CAN_LONG;
    }
    for (uint32_t index = 0U; index < len / 2U; index++) {
        (void)hex_value(text, len, 2U * index, 2U, &value);
        frame->data[index] = (uint8_t)value;
    }
    frame->length = (uint8_t)(len / 2U);
    return CAN_OK;
}

/* ID/DATA into a frame; DATA as R or R<length> is a remote frame where `remote_allowed`. */
static uint32_t can_parse_frame(const uint8_t *text, uint32_t len, uint8_t extended, int remote_allowed, struct can_frame *frame)
{
    uint32_t split = 0U;
    uint32_t code;

    frame->identifier = 0U;
    frame->extended = extended;
    frame->remote = 0U;
    frame->length = 0U;
    for (uint32_t index = 0U; index < CAN_DATA_MAX; index++) {
        frame->data[index] = 0U;
    }
    while (split < len && text[split] != '/') {
        split++;
    }
    if (split == len) {
        return CAN_SYNTAX;
    }
    code = can_parse_number(text, split, extended ? CAN_EXTENDED_MAX : CAN_STANDARD_MAX, &frame->identifier);
    if (code != CAN_OK) {
        return code;
    }
    text += split + 1U;
    len -= split + 1U;
    if (remote_allowed && len > 0U && text[0] == 'R') {
        uint32_t length = 0U;

        if (len > 1U && !parse_decimal(text + 1U, len - 1U, &length)) {
            return CAN_SYNTAX;
        }
        if (length > CAN_DATA_MAX) {
            return CAN_RANGE;
        }
        frame->remote = 1U;
        frame->length = (uint8_t)length;
        return CAN_OK;
    }
    return can_parse_data(text, len, frame);
}

static void put_hex_short(uint32_t value)
{
    int32_t shift = 28;

    while (shift > 0 && ((value >> (uint32_t)shift) & 0xFU) == 0U) {
        shift -= 4;
    }
    for (; shift >= 0; shift -= 4) {
        tx_put((uint8_t)can_hex[(value >> (uint32_t)shift) & 0xFU]);
    }
}

/* A frame spelled the way `can send` takes it. */
static void put_frame(const struct can_frame *frame)
{
    if (frame->extended) {
        put_text("extended ");
    }
    put_text("0x");
    put_hex_short(frame->identifier);
    put_text("/");
    if (frame->remote) {
        put_text("R");
        put_decimal(frame->length);
        return;
    }
    for (uint32_t index = 0U; index < can_data_length(frame); index++) {
        tx_put((uint8_t)can_hex[frame->data[index] >> 4]);
        tx_put((uint8_t)can_hex[frame->data[index] & 0xFU]);
    }
}

static void can_command_mode(const uint8_t *arguments, uint32_t len)
{
    uint32_t mode;

    if (is_word(arguments, len, "loopback")) {
        mode = CAN_MODE_LOOPBACK;
    } else if (is_word(arguments, len, "normal")) {
        mode = CAN_MODE_NORMAL;
    } else {
        reply_error("can mode", "syntax");
        return;
    }
    if (!can_switch(mode)) {
        reply_error("can mode", "init");
        return;
    }
    reply_ok(mode == CAN_MODE_LOOPBACK ? "can mode loopback" : "can mode normal");
    reply_end();
}

static void can_command_rule(const uint8_t *arguments, uint32_t len)
{
    uint32_t split = 0U;
    uint32_t slot = CAN_RULES;
    uint32_t code;
    struct can_frame heard;
    struct can_frame answer;

    while (split < len && arguments[split] != '=') {
        split++;
    }
    if (split == len) {
        reply_error("can rule", "syntax");
        return;
    }
    code = can_parse_frame(arguments, split, 0U, 0, &heard);
    if (code == CAN_OK) {
        code = can_parse_frame(arguments + split + 1U, len - split - 1U, 0U, 0, &answer);
    }
    if (code != CAN_OK) {
        reply_error("can rule", can_reason(code));
        return;
    }
    for (uint32_t index = 0U; index < CAN_RULES; index++) {
        if (can_rules[index].used && can_same_frame(&can_rules[index].heard, &heard)) {
            slot = index;
            break;
        }
    }
    if (slot == CAN_RULES) {
        for (uint32_t index = 0U; index < CAN_RULES; index++) {
            if (!can_rules[index].used) {
                slot = index;
                break;
            }
        }
    }
    if (slot == CAN_RULES) {
        reply_error("can rule", "full");
        return;
    }
    can_rules[slot].heard = heard;
    can_rules[slot].answer = answer;
    can_rules[slot].used = 1U;
    reply_ok("can rule");
    reply_end();
}

static void can_command_unrule(const uint8_t *arguments, uint32_t len)
{
    struct can_frame heard;
    uint32_t code = can_parse_frame(arguments, len, 0U, 0, &heard);

    if (code != CAN_OK) {
        reply_error("can unrule", can_reason(code));
        return;
    }
    for (uint32_t index = 0U; index < CAN_RULES; index++) {
        if (can_rules[index].used && can_same_frame(&can_rules[index].heard, &heard)) {
            can_rules[index].used = 0U;
            reply_ok("can unrule");
            reply_end();
            return;
        }
    }
    reply_error("can unrule", "absent");
}

static void can_command_send(const uint8_t *arguments, uint32_t len)
{
    uint8_t extended = 0U;
    uint32_t end = 0U;
    uint32_t count = 1U;
    uint32_t code;
    struct can_frame frame;

    if (take_word(&arguments, &len, "extended")) {
        extended = 1U;
    }
    while (end < len && arguments[end] != ' ') {
        end++;
    }
    code = can_parse_frame(arguments, end, extended, 1, &frame);
    if (code == CAN_OK && end < len) {
        if (!parse_decimal(arguments + end + 1U, len - end - 1U, &count)) {
            code = CAN_SYNTAX;
        } else if (count == 0U || count > CAN_SEND_MAX) {
            code = CAN_RANGE;
        }
    }
    if (code != CAN_OK) {
        reply_error("can send", can_reason(code));
        return;
    }
    if (can_sends_count == CAN_SENDS) {
        reply_error("can send", "full");
        return;
    }
    can_sends[(can_sends_first + can_sends_count) % CAN_SENDS].frame = frame;
    can_sends[(can_sends_first + can_sends_count) % CAN_SENDS].remaining = count;
    can_sends_count++;
    reply_ok("can send ");
    put_decimal(count);
    reply_end();
}

static void can_command_filter(const uint8_t *arguments, uint32_t len)
{
    uint8_t extended = 0U;
    uint32_t split = 0U;
    uint32_t max;
    uint32_t identifier = 0U;
    uint32_t mask = 0U;
    uint32_t code;

    if (is_word(arguments, len, "off")) {
        can_filter_count = 0U;
        can_apply_filters();
        reply_ok("can filter off");
        reply_end();
        return;
    }
    if (take_word(&arguments, &len, "extended")) {
        extended = 1U;
    }
    max = extended ? CAN_EXTENDED_MAX : CAN_STANDARD_MAX;
    while (split < len && arguments[split] != ' ') {
        split++;
    }
    if (split == len) {
        reply_error("can filter", "syntax");
        return;
    }
    code = can_parse_number(arguments, split, max, &identifier);
    if (code == CAN_OK) {
        code = can_parse_number(arguments + split + 1U, len - split - 1U, max, &mask);
    }
    if (code != CAN_OK) {
        reply_error("can filter", can_reason(code));
        return;
    }
    if (can_filter_count == CAN_FILTERS) {
        reply_error("can filter", "full");
        return;
    }
    can_filters[can_filter_count].extended = extended;
    can_filters[can_filter_count].identifier = identifier;
    can_filters[can_filter_count].mask = mask;
    can_filter_count++;
    can_apply_filters();
    reply_ok("can filter");
    reply_end();
}

static void can_command_stats(void)
{
    uint32_t errors;

    can_account();
    can_take_received();
    errors = CAN1_ESR;
    reply_ok("can stats mode=");
    if (can_mode == CAN_MODE_LOOPBACK) {
        put_text("loopback");
    } else if (can_mode == CAN_MODE_NORMAL) {
        put_text("normal");
    } else {
        put_text("failed");
    }
    put_text(" queued=");
    put_decimal(can_queued());
    put_text(" sent=");
    put_decimal(can_sent);
    put_text(" received=");
    put_decimal(can_received);
    put_text(" answered=");
    put_decimal(can_answered);
    put_text(" digest=");
    put_hex32(can_digest);
    put_text(" lost=");
    put_decimal(can_lost);
    put_text(" unsent=");
    put_decimal(can_unsent);
    put_text(" tec=");
    put_decimal((errors >> 16) & 0xFFU);
    put_text(" rec=");
    put_decimal((errors >> 24) & 0xFFU);
    put_text(" state=");
    if ((errors & CAN_ESR_BOFF) != 0U) {
        put_text("busoff");
    } else if ((errors & CAN_ESR_EPVF) != 0U) {
        put_text("passive");
    } else if ((errors & CAN_ESR_EWGF) != 0U) {
        put_text("warning");
    } else {
        put_text("active");
    }
    put_text(" fd=unsupported");
    reply_end();
}

static void can_command_last(void)
{
    can_account();
    can_take_received();
    reply_ok("can last ");
    if (can_heard_any) {
        put_frame(&can_last);
    } else {
        put_text("none");
    }
    reply_end();
}

static void command_can(const uint8_t *text, uint32_t len)
{
    uint32_t word = 0U;
    const uint8_t *arguments = text;
    uint32_t arguments_len = 0U;
    int has_arguments = 0;

    while (word < len && text[word] != ' ') {
        word++;
    }
    if (word < len) {
        arguments = text + word + 1U;
        arguments_len = len - word - 1U;
        has_arguments = 1;
    }

    if (is_word(text, word, "mode") && has_arguments) {
        can_command_mode(arguments, arguments_len);
    } else if (is_word(text, word, "rule") && has_arguments) {
        can_command_rule(arguments, arguments_len);
    } else if (is_word(text, word, "unrule") && has_arguments) {
        can_command_unrule(arguments, arguments_len);
    } else if (is_word(text, word, "clear") && !has_arguments) {
        can_clear_rules();
        reply_ok("can clear");
        reply_end();
    } else if (is_word(text, word, "send") && has_arguments) {
        can_command_send(arguments, arguments_len);
    } else if (is_word(text, word, "filter") && has_arguments) {
        can_command_filter(arguments, arguments_len);
    } else if (is_word(text, word, "stats") && !has_arguments) {
        can_command_stats();
    } else if (is_word(text, word, "last") && !has_arguments) {
        can_command_last();
    } else if (is_word(text, word, "mode") || is_word(text, word, "rule") || is_word(text, word, "unrule") ||
               is_word(text, word, "clear") || is_word(text, word, "send") || is_word(text, word, "filter") ||
               is_word(text, word, "stats") || is_word(text, word, "last")) {
        put_text("@peer error can ");
        put_bytes(text, word);
        put_text(" syntax");
        reply_end();
    } else {
        reply_error("can", "syntax");
    }
}

static void command_stats(void)
{
    reply_ok("stats bytes=");
    put_decimal(stat_bytes);
    put_text(" crc32=");
    put_hex32(~stat_crc);
    put_text(" lines=");
    put_decimal(stat_lines);
    put_text(" overlong=");
    put_decimal(stat_overlong);
    put_text(" lost=");
    put_decimal(rx_lost);
    reply_end();
}

static void command_reset(void)
{
    tx_discard();
    clear_rules();
    drop_pending();
    delay_ms = 0U;
    echo_on = 0U;
    silent = 0U;
    announce_on = 0U;
    flood_forever = 0U;
    flood_remaining = 0U;
    flood_position = 0U;
    temperature_bound = 0U;
    stat_bytes = 0U;
    stat_crc = 0xFFFFFFFFU;
    stat_lines = 0U;
    stat_overlong = 0U;
    rx_lost = 0U;
    can_reset();
    reply_ok("reset");
    reply_end();
    if (baud != BOOT_BAUD) {
        set_baud(BOOT_BAUD);
    }
}

static void control(const uint8_t *text, uint32_t len)
{
    uint32_t word = 0U;
    const uint8_t *arguments = text;
    uint32_t arguments_len = 0U;
    int has_arguments = 0;

    while (word < len && text[word] != ' ') {
        word++;
    }
    if (word < len) {
        arguments = text + word + 1U;
        arguments_len = len - word - 1U;
        has_arguments = 1;
    }

    if (is_word(text, word, "rule") && has_arguments) {
        command_rule(arguments, arguments_len);
    } else if (is_word(text, word, "unrule") && has_arguments) {
        command_unrule(arguments, arguments_len);
    } else if (is_word(text, word, "clear") && !has_arguments) {
        clear_rules();
        drop_pending();
        reply_ok("clear");
        reply_end();
    } else if (is_word(text, word, "delay") && has_arguments) {
        command_delay(arguments, arguments_len);
    } else if (is_word(text, word, "announce") && has_arguments) {
        command_announce(arguments, arguments_len);
    } else if (is_word(text, word, "echo") && has_arguments) {
        command_echo(arguments, arguments_len);
    } else if (is_word(text, word, "flood") && has_arguments) {
        command_flood(arguments, arguments_len);
    } else if (is_word(text, word, "baud") && has_arguments) {
        command_baud(arguments, arguments_len);
    } else if (is_word(text, word, "silence") && has_arguments) {
        command_silence(arguments, arguments_len);
    } else if (is_word(text, word, "temp")) {
        command_temp(arguments, arguments_len, has_arguments);
    } else if (is_word(text, word, "stats") && !has_arguments) {
        command_stats();
    } else if (is_word(text, word, "reset") && !has_arguments) {
        command_reset();
    } else if (is_word(text, word, "can")) {
        if (has_arguments) {
            command_can(arguments, arguments_len);
        } else {
            reply_error("can", "syntax");
        }
    } else if (is_word(text, word, "rule") || is_word(text, word, "unrule") || is_word(text, word, "delay") ||
               is_word(text, word, "announce") || is_word(text, word, "echo") || is_word(text, word, "flood") ||
               is_word(text, word, "baud") || is_word(text, word, "silence")) {
        put_text("@peer error ");
        put_bytes(text, word);
        put_text(" syntax");
        reply_end();
    } else if (is_word(text, word, "clear") || is_word(text, word, "stats") || is_word(text, word, "reset")) {
        put_text("@peer error ");
        put_bytes(text, word);
        put_text(" syntax");
        reply_end();
    } else {
        put_text("@peer error unknown");
        reply_end();
    }
}

static void echo_bytes(const uint8_t *bytes, uint32_t count)
{
    if (echo_on && !silent) {
        put_bytes(bytes, count);
    }
}

static void finish_line(void)
{
    uint32_t len = line_len;

    while (len > 0U && line[len - 1U] == '\r') {
        len--;
    }
    if (line_kind == LINE_CONTROL) {
        /* Not payload: the statistics go back to where this line began. */
        stat_bytes = snapshot_bytes;
        stat_crc = snapshot_crc;
        if (line_overlong) {
            put_text("@peer error overlong");
            reply_end();
        } else {
            control(line + PREFIX_LEN, len - PREFIX_LEN);
        }
    } else {
        stat_lines++;
        if (line_overlong) {
            stat_overlong++;
        } else {
            answer_line(line, len);
        }
    }
    line_started = 0U;
}

static void take_byte(uint8_t byte)
{
    if (!line_started) {
        line_started = 1U;
        line_len = 0U;
        line_overlong = 0U;
        prefix_seen = 0U;
        line_kind = LINE_UNDECIDED;
        snapshot_bytes = stat_bytes;
        snapshot_crc = stat_crc;
    }
    stat_bytes++;
    stat_crc = crc32_step(stat_crc, byte);

    /* Whether a line is control or payload is known after its first six
     * bytes, so an echo holds those back until it is. */
    if (line_kind == LINE_UNDECIDED) {
        if (byte == prefix[prefix_seen]) {
            prefix_seen++;
            if (prefix_seen == PREFIX_LEN) {
                line_kind = LINE_CONTROL;
            }
        } else {
            line_kind = LINE_PAYLOAD;
            echo_bytes(prefix, prefix_seen);
            echo_bytes(&byte, 1U);
        }
    } else if (line_kind == LINE_PAYLOAD) {
        echo_bytes(&byte, 1U);
    }

    if (byte == '\n') {
        finish_line();
        return;
    }
    if (line_len < LINE_MAX) {
        line[line_len] = byte;
        line_len++;
    } else {
        line_overlong = 1U;
    }
}

static void serve_pending(void)
{
    while (pending_count > 0U && (int32_t)(uptime_ms - pending[pending_first].due_ms) >= 0) {
        uint8_t answer = pending[pending_first].answer;

        pending_first = (pending_first + 1U) % PENDING_MAX;
        pending_count--;
        write_answer(answer);
    }
}

static void serve_announcement(void)
{
    if (!announce_on || silent || (int32_t)(uptime_ms - announce_next_ms) < 0) {
        return;
    }
    put_bytes(announce_text, announce_len);
    announce_next_ms = uptime_ms + announce_every_ms;
}

static void serve_flood(void)
{
    static const uint8_t digits[10] = {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9'};
    uint32_t queued = 0U;

    if (silent) {
        return;
    }
    while ((flood_forever || flood_remaining > 0U) && tx_space() > 0U) {
        tx_ring[tx_head % TX_RING] = digits[flood_position];
        tx_head = tx_head + 1U;
        flood_position = (flood_position + 1U) % 10U;
        if (!flood_forever) {
            flood_remaining--;
        }
        queued++;
    }
    if (queued > 0U) {
        tx_kick();
    }
}

int main(void)
{
    uint32_t blinked = 0U;

    systick_init();
    led_init();
    usart2_init();
    adc_init();
    can_init();

    put_text("@peer ready");
    reply_end();

    for (;;) {
        if (uptime_ms - blinked >= 500U) {
            blinked = uptime_ms;
            GPIOA_ODR ^= (1U << LD2_PIN);
        }
        while (rx_tail != rx_head) {
            uint8_t byte = rx_ring[rx_tail % RX_RING];

            rx_tail = rx_tail + 1U;
            take_byte(byte);
        }
        serve_pending();
        serve_announcement();
        serve_flood();
        can_serve();
    }
}
