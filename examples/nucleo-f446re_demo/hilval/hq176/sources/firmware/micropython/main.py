# hq176: how long does UART.sendbreak() hold the bus low on an STM32F4, and when
# does the method return? Measured inside the chip on a NUCLEO-F446RE.
#
# Nothing is wired to the headers. The test UART is one the REPL does not use
# (UART4 with TX on PA0, or UART3 with TX on PB10 as a fallback; both are on
# APB1, which is what lets them reach 1200 baud at this board's clocks). That
# pin's own GPIO input data register is copied into RAM by DMA2 stream 1 on
# every TIM8 update, 32 samples per bit, 4096 samples per window: the port uses
# neither TIM8 nor that stream on this board (see README.md).
#
# Each repetition sends a 0x55 frame (ten edges: the bit time), a 0x00 frame (a
# low run of known length), the break, and a 0x55 after it. The sample index at
# which the method returned is read from the DMA stream's remaining-transfer
# count, and the cost of that read and of a trivial call on the same object are
# measured in the same conditions, so the tests can place the return inside the
# break without assuming anything about MicroPython's speed.
#
# This script prints raw numbers only: register words, sample indices and cycle
# counts. Every verdict is computed by tests/hq176/. See README.md for the
# record kinds.

import array
import gc
import machine
import micropython
import time
import uctypes
from micropython import const

TAG = "H176"

NS = const(4096)  # samples per window: 128 bit times at every baud below
SPB = const(32)  # samples per bit the sample period aims for
REPS = const(24)  # repetitions of the main phase per configuration
REPS_SBK = const(5)  # repetitions of the CR1.SBK poll phase
CONTROLS = const(16)  # iterations of each overhead control
MAX_EDGES = const(64)  # indices the edge buffer holds; a repetition needs 24
EDGES_PER_LINE = const(16)
# Last-resort bound on the register polls below, so a sampler that never
# advances cannot hang the image. It is far more iterations than the
# sample-count bound needs, so that bound is the one that normally ends a poll.
SPINS = const(1000000)

# RM0390. firmware/smoke/board.h holds the same values. A peripheral address is
# above MicroPython's small-integer range, so these are plain names and not
# const(): nothing in a measured path looks one up, because the rep functions
# bind what they need to a local first.
RCC_CFGR = 0x40023808
RCC_AHB1ENR = 0x40023830
RCC_APB2ENR = 0x40023844
RCC_DMA2EN = const(1 << 22)
RCC_TIM8EN = const(1 << 1)

GPIOA = 0x40020000
GPIOB = 0x40020400
GPIO_MODER = const(0x00)
GPIO_IDR = const(0x10)
GPIO_AFRL = const(0x20)
GPIO_AFRH = const(0x24)

TIM8_CR1 = 0x40010400
TIM8_CR2 = 0x40010404
TIM8_SMCR = 0x40010408
TIM8_DIER = 0x4001040C
TIM8_SR = 0x40010410
TIM8_EGR = 0x40010414
TIM8_PSC = 0x40010428
TIM8_ARR = 0x4001042C
TIM8_RCR = 0x40010430
TIM_CEN = const(1 << 0)
TIM_UG = const(1 << 0)
TIM_UDE = const(1 << 8)

DMA2_LISR = 0x40026400
DMA2_LIFCR = 0x40026408
DMA2_S1CR = 0x40026428
DMA2_S1NDTR = 0x4002642C
DMA2_S1PAR = 0x40026430
DMA2_S1M0AR = 0x40026434
DMA2_S1FCR = 0x4002643C
DMA_STREAM1_SHIFT = const(6)  # stream 1's flag group sits at bits 6..11 of LISR
DMA_FLAGS = const(0x3D)  # FEIF, DMEIF, TEIF, HTIF, TCIF of one stream
DMA_TCIF = const(1 << 5)
DMA_EN = const(1 << 0)
# channel 7 (TIM8_UP), priority high, 16-bit peripheral and memory, memory
# increment, peripheral to memory, no FIFO
DMA_S1CR_VALUE = const((7 << 25) | (2 << 16) | (1 << 13) | (1 << 11) | (1 << 10))

UART4 = 0x40004C00
USART3 = 0x40004800
USART_BRR = const(0x08)
USART_CR1 = const(0x0C)
USART_CR2 = const(0x10)
USART_CR3 = const(0x14)
USART_SBK = const(1 << 0)

# (id, baud, parity, stop); parity 0 is even, None is no parity. 1200, 9600 and
# 19200 baud at 8N1 are the bauds the reports name; 8E1 shows the port's rule
# that the parity bit counts in the USART word length, and 8N2 decides whether
# the stop-bit setting lengthens the break.
CONFIGS = (
    (0, 1200, None, 1),
    (1, 9600, None, 1),
    (2, 19200, None, 1),
    (3, 9600, 0, 1),
    (4, 9600, None, 2),
)
# (UART number, GPIO port letter, pin, USART base, GPIO base), in the order they
# are tried. The boot record names the one that initialised.
CANDIDATES = ((4, "a", 0, UART4, GPIOA), (3, "b", 10, USART3, GPIOB))

BYTE55 = b"\x55"
BYTE00 = b"\x00"

mem32 = machine.mem32
samples = bytearray(2 * NS)
SAMPLES_ADDR = uctypes.addressof(samples)
marks = array.array("H", [0] * MAX_EDGES)


# Stores in `out` the index of every sample at which `mask` changed and returns
# their number. An index is the first sample that shows the new level, so an
# interval between two indices is within one sample period of the truth.
@micropython.viper
def edges_of(buf: ptr16, count: int, mask: int, out: ptr16, limit: int) -> int:
    found = 0
    previous = buf[0] & mask
    index = 1
    while index < count:
        level = buf[index] & mask
        if level != previous:
            if found < limit:
                out[found] = index
            found = found + 1
            previous = level
        index = index + 1
    return found


# Polls `reg` until `mask` reads zero and returns `(seen << 16) | NDTR` at the
# moment it stopped. The running sampler is the clock: the poll gives up once
# `span` more samples have been stored, which is the axis the edge indices are
# on. `spins` bounds the loop as well, so a stalled sampler ends it too instead
# of hanging the image.
@micropython.viper
def wait_zero(reg: ptr32, mask: int, ndtr: ptr32, span: int, spins: int) -> int:
    floor = ndtr[0] - span
    if floor < 0:
        floor = 0
    seen = 0
    left = spins
    now = ndtr[0]
    while now > floor:
        if (reg[0] & mask) == 0:
            seen = 1
            break
        left = left - 1
        if left < 1:
            break
        now = ndtr[0]
    return (seen << 16) | (ndtr[0] & 0xFFFF)


def clean(text):
    """`text` cut and mapped to what a record value may hold: printable ASCII, no space, no '='."""
    out = ""
    for character in text[:120]:
        code = ord(character)
        if 0x21 <= code <= 0x3C or 0x3E <= code <= 0x7E:
            out += character
        else:
            out += "_"
    if not out:
        out = "_"
    return out


def fault(text):
    print(TAG, "fault", "err=" + clean(text))


def arm(period, idr):
    """Start the sampler: DMA2 stream 1 stores `idr` on every TIM8 update."""
    mem32[TIM8_CR1] = 0
    mem32[TIM8_DIER] = 0
    mem32[DMA2_S1CR] = 0
    while mem32[DMA2_S1CR] & DMA_EN:
        pass
    mem32[DMA2_LIFCR] = DMA_FLAGS << DMA_STREAM1_SHIFT
    mem32[DMA2_S1PAR] = idr
    mem32[DMA2_S1M0AR] = SAMPLES_ADDR
    mem32[DMA2_S1NDTR] = NS
    mem32[DMA2_S1FCR] = 0
    mem32[DMA2_S1CR] = DMA_S1CR_VALUE
    mem32[DMA2_S1CR] = mem32[DMA2_S1CR] | DMA_EN
    # CR2, SMCR and the repetition counter back to their reset values, so the
    # timer counts its own clock and raises an update on every overflow whatever
    # anything else may have left behind in it.
    mem32[TIM8_CR2] = 0
    mem32[TIM8_SMCR] = 0
    mem32[TIM8_RCR] = 0
    mem32[TIM8_PSC] = 0
    mem32[TIM8_ARR] = period - 1
    mem32[TIM8_EGR] = TIM_UG  # load PSC and ARR now, while no request can be made
    mem32[TIM8_SR] = 0
    mem32[TIM8_DIER] = TIM_UDE  # from here an update asks the stream for a sample
    mem32[TIM8_CR1] = TIM_CEN


def stop_sampler():
    mem32[TIM8_CR1] = 0
    mem32[TIM8_DIER] = 0
    mem32[DMA2_S1CR] = 0
    while mem32[DMA2_S1CR] & DMA_EN:
        pass


def wait_window(limit_us):
    """Stream 1's flag group once the window is full, or once `limit_us` has passed."""
    begin = time.ticks_us()
    while True:
        flags = (mem32[DMA2_LISR] >> DMA_STREAM1_SHIFT) & DMA_FLAGS
        if flags & DMA_TCIF:
            return flags
        if time.ticks_diff(time.ticks_us(), begin) > limit_us:
            return flags


def print_edges(cfg, number, phase, found):
    shown = found if found < MAX_EDGES else MAX_EDGES
    position = 0
    sequence = 0
    while position < shown:
        last = position + EDGES_PER_LINE
        if last > shown:
            last = shown
        parts = []
        while position < last:
            parts.append("%d" % marks[position])
            position += 1
        print(
            "%s edges id=%d rep=%d phase=%s seq=%d i=%s"
            % (TAG, cfg, number, phase, sequence, ",".join(parts))
        )
        sequence += 1


def controls(u, period, pins):
    """The cost of one mark and of one trivial call on the same object, with the sampler running.

    The largest of CONTROLS measurements of each, taken under the same
    interrupts-off conditions as the sendbreak() marks below, so what the tests
    compare the call against is the worst this path was seen to cost and not a
    single lucky reading.
    """
    idr = pins[0]
    ndtr = DMA2_S1NDTR
    m32 = mem32
    read = 0
    call = 0
    read_cycles = 0
    call_cycles = 0
    gc.collect()
    arm(period, idr)
    state = machine.disable_irq()
    for _ in range(CONTROLS):
        before = NS - m32[ndtr]
        after = NS - m32[ndtr]
        if after - before > read:
            read = after - before
        cycles0 = time.ticks_cpu()
        marked = NS - m32[ndtr]  # the mark being timed; its value is not used
        cycles1 = time.ticks_cpu()
        spent = time.ticks_diff(cycles1, cycles0)
        if spent > read_cycles:
            read_cycles = spent
        before = NS - m32[ndtr]
        u.any()
        after = NS - m32[ndtr]
        if after - before > call:
            call = after - before
        cycles0 = time.ticks_cpu()
        u.any()
        cycles1 = time.ticks_cpu()
        spent = time.ticks_diff(cycles1, cycles0)
        if spent > call_cycles:
            call_cycles = spent
    machine.enable_irq(state)
    stop_sampler()
    return read, call, read_cycles, call_cycles


def main_rep(u, cfg, number, period, limit_us, pins):
    """One repetition: 0x55, 0x00, the break with a byte written during it, 0x55."""
    idr, mask, moder_address, moder_shift, afr_address, afr_shift = pins
    ndtr = DMA2_S1NDTR
    m32 = mem32
    gc.collect()
    arm(period, idr)
    cycles_begin = time.ticks_cpu()
    t0 = NS - m32[ndtr]
    w1 = u.write(BYTE55)
    w2 = u.write(BYTE00)
    # Interrupts off so neither the 1 ms SysTick nor the REPL's UART can land
    # between the call and the marks that place it on the sample axis.
    state = machine.disable_irq()
    cycles0 = time.ticks_cpu()
    tc = NS - m32[ndtr]
    u.sendbreak()
    tr = NS - m32[ndtr]
    cycles1 = time.ticks_cpu()
    # Wait, bounded by 3 bit times, until the line is actually low, so the byte
    # below is written during the break and never races its start.
    reply = wait_zero(idr, mask, ndtr, 3 * SPB, SPINS)
    machine.enable_irq(state)
    w3 = u.write(BYTE55)
    te = NS - m32[ndtr]
    cycles_end = time.ticks_cpu()
    cyc = time.ticks_diff(cycles1, cycles0)
    cyc_all = time.ticks_diff(cycles_end, cycles_begin)
    tb = NS - (reply & 0xFFFF)
    low = reply >> 16
    flags = wait_window(limit_us)
    rem = m32[ndtr] & 0xFFFF
    stop_sampler()
    ahb1enr = m32[RCC_AHB1ENR]
    moder = (m32[moder_address] >> moder_shift) & 3
    afr = (m32[afr_address] >> afr_shift) & 0xF
    found = edges_of(samples, NS, mask, marks, MAX_EDGES)
    first = 1 if (samples[0] | (samples[1] << 8)) & mask else 0
    print(
        "%s rep id=%d rep=%d phase=main t0=%d tc=%d tr=%d tb=%d low=%d te=%d cyc=%d cyc_all=%d"
        " w1=%s w2=%s w3=%s flags=0x%02x rem=%d n=%d first=%d moder=%d afr=%d ahb1enr=0x%08x"
        % (
            TAG, cfg, number, t0, tc, tr, tb, low, te, cyc, cyc_all,
            w1, w2, w3, flags, rem, found, first, moder, afr, ahb1enr,
        )
    )
    print_edges(cfg, number, "main", found)


def sbk_rep(u, cfg, number, period, limit_us, pins, cr1_address):
    """One repetition that polls CR1.SBK instead of writing during the break."""
    idr, mask, moder_address, moder_shift, afr_address, afr_shift = pins
    ndtr = DMA2_S1NDTR
    m32 = mem32
    gc.collect()
    arm(period, idr)
    t0 = NS - m32[ndtr]
    w1 = u.write(BYTE55)
    state = machine.disable_irq()
    cycles0 = time.ticks_cpu()
    tc = NS - m32[ndtr]
    u.sendbreak()
    tr = NS - m32[ndtr]
    cycles1 = time.ticks_cpu()
    # 16 bit times is longer than any break the frame format admits, plus the
    # delay before the USART starts it.
    reply = wait_zero(cr1_address, USART_SBK, ndtr, 16 * SPB, SPINS)
    machine.enable_irq(state)
    cyc = time.ticks_diff(cycles1, cycles0)
    tsbk = NS - (reply & 0xFFFF)
    sbk = reply >> 16
    flags = wait_window(limit_us)
    rem = m32[ndtr] & 0xFFFF
    stop_sampler()
    ahb1enr = m32[RCC_AHB1ENR]
    moder = (m32[moder_address] >> moder_shift) & 3
    afr = (m32[afr_address] >> afr_shift) & 0xF
    found = edges_of(samples, NS, mask, marks, MAX_EDGES)
    first = 1 if (samples[0] | (samples[1] << 8)) & mask else 0
    print(
        "%s rep id=%d rep=%d phase=sbk t0=%d tc=%d tr=%d tsbk=%d sbk=%d cyc=%d w1=%s"
        " flags=0x%02x rem=%d n=%d first=%d moder=%d afr=%d ahb1enr=0x%08x"
        % (
            TAG, cfg, number, t0, tc, tr, tsbk, sbk, cyc, w1,
            flags, rem, found, first, moder, afr, ahb1enr,
        )
    )
    print_edges(cfg, number, "sbk", found)


def check_registers(stm):
    """The names of the addresses above that the port's `stm` module reads differently.

    A name the module does not carry is not checked. CR1 of a timer and LISR of
    a DMA controller are at offset 0, so those two constants are the peripheral
    bases the module names.
    """
    wrong = []
    for name, expected in (
        ("GPIOA", GPIOA),
        ("GPIOB", GPIOB),
        ("GPIO_IDR", GPIO_IDR),
        ("TIM8", TIM8_CR1),
        ("DMA2", DMA2_LISR),
        ("UART4", UART4),
        ("USART3", USART3),
    ):
        actual = getattr(stm, name, None)
        if actual is not None and actual != expected:
            wrong.append(name)
    return wrong


def run():
    import os
    import stm

    wrong = check_registers(stm)
    if wrong:
        fault("register-map-mismatch:" + ",".join(wrong))
        return

    mem32[RCC_AHB1ENR] = mem32[RCC_AHB1ENR] | RCC_DMA2EN
    mem32[RCC_APB2ENR] = mem32[RCC_APB2ENR] | RCC_TIM8EN
    time.ticks_cpu()  # the first call starts the DWT cycle counter this script reads

    frequencies = machine.freq()
    sysclk, hclk, pclk1, pclk2 = frequencies[0], frequencies[1], frequencies[2], frequencies[3]
    cfgr = mem32[RCC_CFGR]
    # RM0390: with an APB2 prescaler other than 1, the APB2 timers run at twice
    # PCLK2, which on this board is HCLK, so a sample period is also that many
    # CPU cycles. PPRE2 values 4..7 are the prescalers /2 to /16.
    timclk = pclk2 * 2 if ((cfgr >> 13) & 7) >= 4 else pclk2

    u = None
    for uart_id, port, pin, base, gpio in CANDIDATES:
        try:
            u = machine.UART(uart_id, 9600, bits=8, parity=None, stop=1, timeout=200, timeout_char=200)
            break
        except Exception:
            u = None
    if u is None:
        fault("no-test-uart")
        return
    pins = (
        gpio + GPIO_IDR,
        1 << pin,
        gpio + GPIO_MODER,
        2 * pin,
        gpio + (GPIO_AFRL if pin < 8 else GPIO_AFRH),
        4 * (pin & 7),
    )
    time.sleep_ms(5)

    print(
        "%s boot mpy=%s sysclk=%d hclk=%d pclk1=%d pclk2=%d cfgr=0x%08x ahb1enr=0x%08x"
        " apb2enr=0x%08x uart=%d port=%s pin=%d ns=%d spb=%d"
        % (
            TAG, clean(os.uname().version.split()[0]), sysclk, hclk, pclk1, pclk2, cfgr,
            mem32[RCC_AHB1ENR], mem32[RCC_APB2ENR], uart_id, port, pin, NS, SPB,
        )
    )

    for cfg, baud, parity, stop_bits in CONFIGS:
        u.init(baud, bits=8, parity=parity, stop=stop_bits, timeout=200, timeout_char=200)
        time.sleep_ms(5)
        # The nearest whole timer period to SPB samples per bit, in integers.
        ticks = SPB * baud
        period = (timclk + ticks // 2) // ticks
        limit_us = 2 * (NS * period // (timclk // 1000000)) + 50000
        read, call, read_cycles, call_cycles = controls(u, period, pins)
        print(
            "%s cfg id=%d baud=%d bits=8 parity=%s stop=%d brr=0x%04x cr1=0x%08x cr2=0x%08x"
            " cr3=0x%08x period=%d timclk=%d moder=%d afr=%d rd=%d cl=%d rd_cyc=%d cl_cyc=%d"
            % (
                TAG, cfg, baud, "even" if parity is not None else "none", stop_bits,
                mem32[base + USART_BRR], mem32[base + USART_CR1], mem32[base + USART_CR2],
                mem32[base + USART_CR3], period, timclk,
                (mem32[pins[2]] >> pins[3]) & 3, (mem32[pins[4]] >> pins[5]) & 0xF,
                read, call, read_cycles, call_cycles,
            )
        )
        for number in range(REPS):
            main_rep(u, cfg, number, period, limit_us, pins)
        for number in range(REPS_SBK):
            sbk_rep(u, cfg, number, period, limit_us, pins, base + USART_CR1)

    u.deinit()
    stop_sampler()


try:
    run()
except Exception as error:
    try:
        fault(repr(error))
    except Exception:
        print(TAG, "fault", "err=unprintable")
print(TAG, "done")
