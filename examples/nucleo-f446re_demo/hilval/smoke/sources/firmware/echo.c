/*
 * echo: bytes from the bench, received in four frame formats.
 *
 * The bench always writes 8N1 at 115200 baud. Before stimulus n this image
 * prints "SMK cfg seq=n mode=..." and "SMK ready seq=n" in 8N1, waits until
 * the line has left, switches USART2's receiver to the mode while the line is
 * idle, and receives until the line goes idle again (or 5 s pass). It switches
 * back to 8N1 and prints the status and data register of every byte.
 *
 * Read as 7E1 or 7O1, an 8N1 byte is a 7-bit character whose bit 7 is the
 * parity bit. Read as 8E1, a single 8N1 byte is a character whose parity bit
 * is the host's stop bit (1), followed by the idle line as the stop bit.
 */
#include "board.h"

#define MAX_BYTES 64u
#define RECEIVE_TIMEOUT_CYCLES (5u * SYSCLK_HZ)

struct frame_format {
    const char *mode;
    uint32_t cr1;
};

static const struct frame_format formats[] = {
    {"8n1", 0u},
    {"7e1", USART_CR1_PCE},
    {"7o1", USART_CR1_PCE | USART_CR1_PS},
    {"8e1", USART_CR1_M | USART_CR1_PCE},
    {"8e1", USART_CR1_M | USART_CR1_PCE},
};

static uint32_t received_sr[MAX_BYTES];
static uint32_t received_dr[MAX_BYTES];

int main(void)
{
    const uint32_t console = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
    board_boot("echo");

    for (uint32_t index = 0u; index < sizeof formats / sizeof formats[0]; index++) {
        uint32_t seq = index + 1u;
        line_begin("cfg");
        field_u("seq", seq);
        field_s("mode", formats[index].mode);
        line_end();
        line_begin("ready");
        field_u("seq", seq);
        line_end();
        console_flush();

        /* Drop anything the receiver holds; the reference manual allows
         * changing M, PCE and PS while the line is idle. */
        (void)USART_SR(USART2_BASE);
        (void)USART_DR(USART2_BASE);
        USART_CR1(USART2_BASE) = console | formats[index].cr1;

        uint32_t count = 0u;
        bool timed_out = false;
        uint32_t start = cycles();
        for (;;) {
            uint32_t status = USART_SR(USART2_BASE);
            if ((status & USART_SR_RXNE) != 0u) {
                /* Status first, then data: the read of DR clears PE, FE, NF and ORE. */
                uint32_t data = USART_DR(USART2_BASE);
                if (count < MAX_BYTES) {
                    received_sr[count] = status;
                    received_dr[count] = data;
                }
                count++;
                continue;
            }
            if (count > 0u && (status & USART_SR_IDLE) != 0u) {
                (void)USART_DR(USART2_BASE);
                break;
            }
            if (cycles() - start > RECEIVE_TIMEOUT_CYCLES) {
                timed_out = true;
                break;
            }
        }
        USART_CR1(USART2_BASE) = console;

        line_begin("rx");
        field_u("seq", seq);
        field_s("mode", formats[index].mode);
        field_u("count", count);
        field_u("timeout", timed_out ? 1u : 0u);
        line_end();
        for (uint32_t byte = 0u; byte < count && byte < MAX_BYTES; byte++) {
            uint32_t status = received_sr[byte];
            line_begin("rxb");
            field_u("seq", seq);
            field_u("i", byte);
            field_x("dr", received_dr[byte]);
            field_u("pe", (status & USART_SR_PE) != 0u ? 1u : 0u);
            field_u("fe", (status & USART_SR_FE) != 0u ? 1u : 0u);
            field_u("nf", (status & USART_SR_NF) != 0u ? 1u : 0u);
            field_u("ore", (status & USART_SR_ORE) != 0u ? 1u : 0u);
            field_x("sr", status);
            line_end();
        }
    }
    finish();
}
