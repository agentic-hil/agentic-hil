//! One sequence per reader and frame source, and what each call returned.
//!
//! The five readers the pull request touches are `blocking_read`,
//! `UartRx::read`, `read_until_idle`, `BufferedUartRx::read` and
//! `RingBufferedUartRx::read`. The first four read the USART's own half-duplex
//! transmission back, which is where the correct-parity values come from;
//! `RingBufferedUartRx::read()` switches the transmitter off, so it only ever
//! sees the frames `synth.rs` makes. Wrong parity is always synthesized.
//!
//! Nothing here judges a value. Each call's result, the words it returned and,
//! after an error, two snapshots of `USART_SR` two bit times apart are recorded
//! raw; `tests/hq178` computes what the base and the head code predict.
//!
//! Every call has a timeout, every sequence ends with one call that must find
//! nothing, and a fresh instance is constructed per sequence, so each one
//! starts from a USART its constructor has just reset.

use core::future::Future;

use embassy_futures::join::join;
use embassy_stm32::gpio::Pull;
use embassy_stm32::mode::{Async, Blocking};
use embassy_stm32::peripherals;
use embassy_stm32::usart::{
    BufferedUart, Config, DataBits, Error, HalfDuplexReadback, OutputConfig, Parity, StopBits, Uart,
    UartRx,
};
use embassy_stm32::{Peri, bind_interrupts, usart};
use embassy_time::{Duration, Instant, Timer, with_timeout};

use crate::console::{ResString, Static};
use crate::synth::{self, Par, Shape, Synth, Synthesis};

bind_interrupts!(struct Irqs {
    // The buffered handler first: while no BufferedUart exists its reference
    // count is zero and it returns at once, and while one exists it must see
    // IDLE and TC before the plain handler could disable their interrupts.
    USART1 => usart::BufferedInterruptHandler<peripherals::USART1>,
        usart::InterruptHandler<peripherals::USART1>;
    DMA2_STREAM2 => embassy_stm32::dma::InterruptHandler<peripherals::DMA2_CH2>;
    DMA2_STREAM7 => embassy_stm32::dma::InterruptHandler<peripherals::DMA2_CH7>;
});

pub const BAUD: u32 = 19200;
/// The core clock the deadlines below are counted in; the `clk` record proves it.
const CYCLES_PER_MS: u32 = 16_000;

/// One word of a readback sequence, so a stalled line cannot hang the image.
const WORD_MS: u32 = 200;
/// A whole pattern, or one chunk of it.
const FULL_MS: u64 = 1000;
/// Every call of a synthesized six-frame sequence, and every probe: the frames
/// of such a sequence are less than a millisecond apart.
const PROBE_MS: u32 = 20;

const CHUNK: usize = 64;
/// A synthesized six-frame sequence needs at most eight calls and one probe.
const MAX_SYNTH_CALLS: u32 = 10;
/// Enough chunk reads for the longest pattern; the probe is counted on top.
const MAX_CHUNK_CALLS: u32 = 32;
/// `read_until_idle` gets more room than the frames it expects, so its transfer
/// never completes and the call returns on the idle line or on the error.
const IDLE_MARGIN: usize = 16;
/// How much of the buffer a failed `read_until_idle` shows: the frames up to and
/// including the one that carried the inverted parity bit.
const IDLE_PREFIX: usize = 3;

/// What an untouched word of a read buffer holds. No value any sequence sends
/// equals it, so a word the hardware never wrote is visible in the record.
const SENTINEL8: u8 = 0xFF;
const SENTINEL16: u16 = 0xFFFF;

pub const MAX_VALUES: usize = 300;
pub const MAX_CALLS: usize = 12;
/// `read_until_idle` shows the longest buffer of the calls recorded one by one.
pub const MAX_CALL_WORDS: usize = 20;

// ---- the peripherals every sequence borrows --------------------------------

/// USART1 on PA9 (TX) and PA10 (RX), both AF7 and both free on a NUCLEO-F446RE.
///
/// USART2 is the console. The transmit DMA is DMA2 stream 7 and the receive DMA
/// is DMA2 stream 2 (RM0390: USART1_TX and USART1_RX on channel 4 of those
/// streams); neither is stream 1, which `synth.rs` drives from TIM8_UP.
pub struct Bus {
    pub usart: Peri<'static, peripherals::USART1>,
    pub tx: Peri<'static, peripherals::PA9>,
    pub rx: Peri<'static, peripherals::PA10>,
    pub tx_dma: Peri<'static, peripherals::DMA2_CH7>,
    pub rx_dma: Peri<'static, peripherals::DMA2_CH2>,
}

// ---- what the image records ------------------------------------------------

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Res {
    Ok,
    Parity,
    Framing,
    Noise,
    Overrun,
    TooLong,
    Timeout,
}

impl Res {
    pub fn name(self) -> &'static str {
        match self {
            Res::Ok => "o",
            Res::Parity => "p",
            Res::Framing => "f",
            Res::Noise => "n",
            Res::Overrun => "v",
            Res::TooLong => "b",
            Res::Timeout => "t",
        }
    }

    fn letter(self) -> u8 {
        self.name().as_bytes()[0]
    }

    fn is_error(self) -> bool {
        !matches!(self, Res::Ok | Res::Timeout)
    }
}

fn res_of(error: Error) -> Res {
    match error {
        Error::Parity => Res::Parity,
        Error::Framing => Res::Framing,
        Error::Noise => Res::Noise,
        Error::Overrun => Res::Overrun,
        Error::BufferTooLong => Res::TooLong,
        // `Error` is non_exhaustive upstream. A variant this image does not
        // know is recorded as a framing error, which no prediction allows, so
        // it fails the gate loudly instead of passing as something else.
        _ => Res::Framing,
    }
}

pub struct CallInfo {
    pub res: Res,
    pub returned: u16,
    pub words: [u16; MAX_CALL_WORDS],
    pub words_len: usize,
    pub sr0: Option<u32>,
    pub sr1: Option<u32>,
}

const BLANK_CALL: CallInfo = CallInfo {
    res: Res::Timeout,
    returned: 0,
    words: [0; MAX_CALL_WORDS],
    words_len: 0,
    sr0: None,
    sr1: None,
};

pub struct RunState {
    pub res: ResString,
    /// Words the Ok calls of this sequence returned, all of them together.
    pub n: usize,
    pub ticks: u32,
    pub synth: Synthesis,
    /// A control or wrong-parity sequence keeps every call; a full one keeps
    /// the word list.
    pub detail: bool,
    pub calls: usize,
    pub call: [CallInfo; MAX_CALLS],
    pub values: [u16; MAX_VALUES],
    pub values_len: usize,
    pub width: usize,
}

impl RunState {
    pub const fn new() -> Self {
        Self {
            res: ResString::new(),
            n: 0,
            ticks: 0,
            synth: Synthesis::NotUsed,
            detail: false,
            calls: 0,
            call: [BLANK_CALL; MAX_CALLS],
            values: [0; MAX_VALUES],
            values_len: 0,
            width: 2,
        }
    }

    fn begin(&mut self, detail: bool, width: usize) {
        self.res.reset();
        self.n = 0;
        self.ticks = 0;
        self.synth = Synthesis::NotUsed;
        self.detail = detail;
        self.calls = 0;
        self.values_len = 0;
        self.width = width;
    }

    /// One call: its result and the words of its buffer or chunk.
    ///
    /// Only an Ok call returned words. The buffer of a failed call is recorded
    /// too (the sentinel an async read left untouched, the prefix
    /// `read_until_idle` had already filled), because what the reader did with
    /// the erroneous frame is the measurement.
    fn record(&mut self, res: Res, shown: &[u16]) {
        let returned = if res == Res::Ok { shown.len() } else { 0 };
        self.res.push(res.letter());
        self.n += returned;
        if self.detail {
            if self.calls < MAX_CALLS {
                let keep = shown.len().min(MAX_CALL_WORDS);
                let slot = &mut self.call[self.calls];
                slot.res = res;
                slot.returned = returned as u16;
                slot.words[..keep].copy_from_slice(&shown[..keep]);
                slot.words_len = keep;
                slot.sr0 = None;
                slot.sr1 = None;
            }
            self.calls += 1;
        } else {
            for value in &shown[..returned] {
                if self.values_len < MAX_VALUES {
                    self.values[self.values_len] = *value;
                    self.values_len += 1;
                }
            }
        }
    }

    /// `USART_SR` right after an error and again two bit times later.
    ///
    /// Reading SR alone clears nothing on this part (a clear needs SR and then
    /// DR), so the second snapshot says whether the erroneous word reached DR
    /// before the next call's own pre-arm read could discard it. The wait is
    /// also the image's re-arm pattern: the next read follows an error by two
    /// bit times, far less than the twelve that separate two frames.
    fn note_error(&mut self) {
        let sr0 = synth::rd(synth::USART1_SR);
        synth::wait_cycles(2 * synth::BIT_CYCLES);
        let sr1 = synth::rd(synth::USART1_SR);
        if self.detail && self.calls > 0 && self.calls <= MAX_CALLS {
            let slot = &mut self.call[self.calls - 1];
            slot.sr0 = Some(sr0);
            slot.sr1 = Some(sr1);
        }
    }
}

// ---- buffers that live outside the task future -----------------------------

static BUF8: Static<[u8; MAX_VALUES]> = Static::new([SENTINEL8; MAX_VALUES]);
static BUF16: Static<[u16; MAX_VALUES]> = Static::new([SENTINEL16; MAX_VALUES]);
static CHUNK8: Static<[u8; CHUNK]> = Static::new([SENTINEL8; CHUNK]);
static CHUNK16: Static<[u16; CHUNK]> = Static::new([SENTINEL16; CHUNK]);
static SHOWN: Static<[u16; MAX_VALUES]> = Static::new([0; MAX_VALUES]);
/// 1024 words, so the six frames of a wrong-parity sequence cannot half-fill
/// the ring: nothing but the parity interrupt can wake that reader.
static RING8: Static<[u8; 1024]> = Static::new([0; 1024]);
static RING16: Static<[u16; 1024]> = Static::new([0; 1024]);
/// Larger than the longest pattern, so the buffered handler never runs out of
/// room and no byte is lost to a full ring.
static TX_RING: Static<[u8; 512]> = Static::new([0; 512]);
static RX_RING: Static<[u8; 512]> = Static::new([0; 512]);

// ---- configuration ---------------------------------------------------------

pub fn line(shape: &Shape, rx_pull: Pull) -> Config {
    let mut config = Config::default();
    config.baudrate = BAUD;
    config.data_bits = if shape.data_bits == 7 { DataBits::DataBits7 } else { DataBits::DataBits8 };
    config.parity = match shape.parity {
        Par::None => Parity::ParityNone,
        Par::Even => Parity::ParityEven,
        Par::Odd => Parity::ParityOdd,
    };
    config.stop_bits = StopBits::STOP1;
    // A synthesized frame is made by switching this pin's pull bits, so the
    // line idles at mark through the pull-up while nothing drives it.
    config.rx_pull = rx_pull;
    // The half-duplex pin is released whenever the transmitter is switched off,
    // which every read does, and the receiver stays enabled on it. Push-pull
    // alone leaves it floating there (`OutputConfig::PushPull` is "no internal
    // pullup"), and a floating line is noise the receiver reads as start bits.
    // The internal pull-up holds it at mark instead, which is what
    // `new_half_duplex` asks an external pull-up for; the driven edges stay
    // push-pull.
    config.tx_config = OutputConfig::PushPullPullUp;
    config
}

/// True where only a `u16` reader can see the parity bit: 8 data bits and
/// parity is the 9-bit word, and `BufferedUart` has no `u16` API at all.
pub fn wide(shape: &Shape, reader: Reader) -> bool {
    shape.data_bits == 8 && shape.parity != Par::None && reader != Reader::Buf
}

fn width_of(shape: &Shape, reader: Reader) -> usize {
    if wide(shape, reader) { 4 } else { 2 }
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Reader {
    Blk,
    Asy,
    Idl,
    Buf,
    Rng,
}

impl Reader {
    pub fn name(self) -> &'static str {
        match self {
            Reader::Blk => "blk",
            Reader::Asy => "asy",
            Reader::Idl => "idl",
            Reader::Buf => "buf",
            Reader::Rng => "rng",
        }
    }
}

/// `CR1` and `BRR` of an instance configured for `shape`, for the `cfg` record.
pub fn configuration_registers(bus: &mut Bus, shape: &Shape) -> (u32, u32) {
    let uart = Uart::new_blocking_half_duplex(
        bus.usart.reborrow(),
        bus.tx.reborrow(),
        line(shape, Pull::None),
        HalfDuplexReadback::Readback,
    )
    .unwrap();
    let registers = (synth::rd(synth::USART1_CR1), synth::rd(synth::USART1_BRR));
    drop(uart);
    registers
}

// ---- helpers ---------------------------------------------------------------

fn probe() -> Duration {
    Duration::from_millis(u64::from(PROBE_MS))
}

/// Poll `USART_SR` until a blocking read could return, or give up.
///
/// This is the timeout of a blocking read: Embassy's own call spins until a
/// word arrives, so the image decides here whether there is one to read. A
/// pending error counts, because `check_rx_flags()` reports it before any data.
fn readable(deadline_ms: u32) -> bool {
    let limit = deadline_ms * CYCLES_PER_MS;
    let start = synth::cyccnt();
    loop {
        if synth::rd(synth::USART1_SR) & synth::SR_READABLE != 0 {
            return true;
        }
        if synth::cyccnt().wrapping_sub(start) > limit {
            return false;
        }
    }
}

/// Run `future`, starting the synthesizer on the first call of the sequence.
///
/// `join` polls its first future first, so the reader is armed (its DMA
/// started, its error interrupts enabled) before the first start bit leaves
/// the pull bits: nothing can wake a reader before the frame it waits for.
async fn with_stimulus<F: Future>(future: F, synth: &mut Synth, armed: &mut bool) -> F::Output {
    if *armed {
        return future.await;
    }
    *armed = true;
    let (outcome, ()) = join(future, async { synth.start() }).await;
    outcome
}

/// Let the stimulus run out before the rest of a full sequence is read.
///
/// `RingBufferedUartRx::read()` hands over whatever the ring holds, which while
/// the frames are still arriving is one word per call, and a sequence may make
/// at most `MAX_CHUNK_CALLS` of them. The longest pattern is 258 words and the
/// ring holds 1024, so a full sequence makes one call (which starts the
/// background reception before the first start bit), waits here for the last
/// frame, and then reads the pattern in chunks. A wrong-parity sequence waits
/// for nothing: what its reader does while the frames arrive is the
/// measurement.
fn settle(synth: &mut Synth, state: &mut RunState, detail: bool, settled: bool) -> bool {
    if detail || settled {
        return settled;
    }
    state.synth = synth.finish();
    true
}

fn widen(source: &[u8], target: &mut [u16]) {
    for (at, byte) in source.iter().enumerate() {
        target[at] = u16::from(*byte);
    }
}

/// The outer `Err` is the timeout `with_timeout` reports; the inner one the
/// reader's. The outer type stays generic so this does not name it.
fn classify_word<T>(outcome: Result<Result<(), Error>, T>) -> Res {
    match outcome {
        Ok(Ok(())) => Res::Ok,
        Ok(Err(error)) => res_of(error),
        Err(_) => Res::Timeout,
    }
}

fn classify_count<T>(outcome: Result<Result<usize, Error>, T>) -> (Res, usize) {
    match outcome {
        Ok(Ok(count)) => (Res::Ok, count),
        Ok(Err(error)) => (res_of(error), 0),
        Err(_) => (Res::Timeout, 0),
    }
}

// ---- blocking_read ---------------------------------------------------------

fn read_one_blocking(uart: &mut Uart<'_, Blocking>, use_u16: bool, shown: &mut [u16]) -> Res {
    if use_u16 {
        let mut buffer = [SENTINEL16; 1];
        match uart.blocking_read_u16(&mut buffer) {
            Ok(()) => {
                shown[0] = buffer[0];
                Res::Ok
            }
            Err(error) => res_of(error),
        }
    } else {
        let mut buffer = [SENTINEL8; 1];
        match uart.blocking_read(&mut buffer) {
            Ok(()) => {
                shown[0] = u16::from(buffer[0]);
                Res::Ok
            }
            Err(error) => res_of(error),
        }
    }
}

pub async fn blk_readback(bus: &mut Bus, shape: &Shape, pattern: &[u8], state: &mut RunState) {
    let use_u16 = wide(shape, Reader::Blk);
    state.begin(false, width_of(shape, Reader::Blk));
    let mut uart = Uart::new_blocking_half_duplex(
        bus.usart.reborrow(),
        bus.tx.reborrow(),
        line(shape, Pull::None),
        HalfDuplexReadback::Readback,
    )
    .unwrap();
    let shown = SHOWN.get();
    let start = Instant::now();
    let mut last = start;
    for value in pattern {
        // One word out, the same word back: with Readback the receiver stays
        // enabled while the transmitter shifts the frame out.
        let _ = uart.blocking_write(&[*value]);
        let res = if readable(WORD_MS) {
            read_one_blocking(&mut uart, use_u16, &mut shown[..])
        } else {
            Res::Timeout
        };
        state.record(res, &shown[..1]);
        if res == Res::Timeout {
            break;
        }
        last = Instant::now();
    }
    // The probe: nothing more was sent, so no word may arrive.
    let res = if readable(PROBE_MS) {
        read_one_blocking(&mut uart, use_u16, &mut shown[..])
    } else {
        Res::Timeout
    };
    state.record(res, &shown[..1]);
    state.ticks = (last - start).as_ticks() as u32;
}

pub async fn blk_synth(
    bus: &mut Bus,
    shape: &Shape,
    frames: &[u8],
    bad: Option<usize>,
    synth: &mut Synth,
    state: &mut RunState,
) {
    let use_u16 = wide(shape, Reader::Blk);
    state.begin(true, width_of(shape, Reader::Blk));
    let mut uart = Uart::new_blocking(
        bus.usart.reborrow(),
        bus.tx.reborrow(),
        bus.rx.reborrow(),
        line(shape, Pull::Up),
    )
    .unwrap();
    let shown = SHOWN.get();
    synth.prepare(shape, frames, bad);
    let start = Instant::now();
    let mut last = start;
    synth.start();
    while state.res.calls() < MAX_SYNTH_CALLS {
        let res = if readable(PROBE_MS) {
            read_one_blocking(&mut uart, use_u16, &mut shown[..])
        } else {
            Res::Timeout
        };
        // An Err from blocking_read never reads DR, so it shows no word.
        let count = usize::from(res == Res::Ok);
        state.record(res, &shown[..count]);
        if res != Res::Timeout {
            last = Instant::now();
        }
        if res.is_error() {
            state.note_error();
        }
        if res == Res::Timeout {
            break;
        }
    }
    state.synth = synth.finish();
    state.ticks = (last - start).as_ticks() as u32;
}

// ---- UartRx::read ----------------------------------------------------------

pub async fn asy_readback(bus: &mut Bus, shape: &Shape, pattern: &[u8], state: &mut RunState) {
    let use_u16 = wide(shape, Reader::Asy);
    state.begin(false, width_of(shape, Reader::Asy));
    let uart = Uart::new_half_duplex(
        bus.usart.reborrow(),
        bus.tx.reborrow(),
        bus.tx_dma.reborrow(),
        bus.rx_dma.reborrow(),
        Irqs,
        line(shape, Pull::None),
        HalfDuplexReadback::Readback,
    )
    .unwrap();
    let (mut tx, mut rx) = uart.split();
    let shown = SHOWN.get();
    let words = pattern.len();
    let start = Instant::now();
    let mut last = start;
    // One call for the whole pattern. The read is the first future of the
    // join, so its DMA is armed before the write enables the transmitter.
    let (res, count) = if use_u16 {
        let buffer = BUF16.get();
        buffer[..words].fill(SENTINEL16);
        let outcome = with_timeout(Duration::from_millis(FULL_MS), async {
            let (read, _written) = join(rx.read_u16(&mut buffer[..words]), tx.write(pattern)).await;
            read
        })
        .await;
        let res = classify_word(outcome);
        if res == Res::Ok {
            shown[..words].copy_from_slice(&buffer[..words]);
            (res, words)
        } else {
            (res, 0)
        }
    } else {
        let buffer = BUF8.get();
        buffer[..words].fill(SENTINEL8);
        let outcome = with_timeout(Duration::from_millis(FULL_MS), async {
            let (read, _written) = join(rx.read(&mut buffer[..words]), tx.write(pattern)).await;
            read
        })
        .await;
        let res = classify_word(outcome);
        if res == Res::Ok {
            widen(&buffer[..words], &mut shown[..]);
            (res, words)
        } else {
            (res, 0)
        }
    };
    state.record(res, &shown[..count]);
    if res != Res::Timeout {
        last = Instant::now();
    }
    let res = probe_word(&mut rx, use_u16).await;
    state.record(res, &shown[..0]);
    state.ticks = (last - start).as_ticks() as u32;
}

pub async fn asy_synth(
    bus: &mut Bus,
    shape: &Shape,
    frames: &[u8],
    bad: Option<usize>,
    synth: &mut Synth,
    state: &mut RunState,
) {
    let use_u16 = wide(shape, Reader::Asy);
    state.begin(true, width_of(shape, Reader::Asy));
    let mut rx = UartRx::new(
        bus.usart.reborrow(),
        bus.rx.reborrow(),
        bus.rx_dma.reborrow(),
        Irqs,
        line(shape, Pull::Up),
    )
    .unwrap();
    let shown = SHOWN.get();
    synth.prepare(shape, frames, bad);
    let start = Instant::now();
    let mut last = start;
    let mut armed = false;
    // One word per call, which is the case the pull request's own doc comment
    // is about: a one-word read either returns the frame or reports the error.
    while state.res.calls() < MAX_SYNTH_CALLS {
        let res = if use_u16 {
            let mut buffer = [SENTINEL16; 1];
            let outcome =
                with_stimulus(with_timeout(probe(), rx.read_u16(&mut buffer)), synth, &mut armed).await;
            shown[0] = buffer[0];
            classify_word(outcome)
        } else {
            let mut buffer = [SENTINEL8; 1];
            let outcome =
                with_stimulus(with_timeout(probe(), rx.read(&mut buffer)), synth, &mut armed).await;
            shown[0] = u16::from(buffer[0]);
            classify_word(outcome)
        };
        // A failed read shows its buffer, which still holds the sentinel when
        // the DMA never moved the word; a probe that timed out shows nothing.
        let shows = usize::from(res != Res::Timeout);
        state.record(res, &shown[..shows]);
        if res != Res::Timeout {
            last = Instant::now();
        }
        if res.is_error() {
            state.note_error();
        }
        if res == Res::Timeout {
            break;
        }
    }
    state.synth = synth.finish();
    state.ticks = (last - start).as_ticks() as u32;
}

async fn probe_word(rx: &mut UartRx<'_, Async>, use_u16: bool) -> Res {
    if use_u16 {
        let mut buffer = [SENTINEL16; 1];
        let outcome = with_timeout(probe(), rx.read_u16(&mut buffer)).await;
        classify_word(outcome)
    } else {
        let mut buffer = [SENTINEL8; 1];
        let outcome = with_timeout(probe(), rx.read(&mut buffer)).await;
        classify_word(outcome)
    }
}

// ---- read_until_idle -------------------------------------------------------

pub async fn idl_readback(bus: &mut Bus, shape: &Shape, pattern: &[u8], state: &mut RunState) {
    let use_u16 = wide(shape, Reader::Idl);
    state.begin(false, width_of(shape, Reader::Idl));
    let uart = Uart::new_half_duplex(
        bus.usart.reborrow(),
        bus.tx.reborrow(),
        bus.tx_dma.reborrow(),
        bus.rx_dma.reborrow(),
        Irqs,
        line(shape, Pull::None),
        HalfDuplexReadback::Readback,
    )
    .unwrap();
    let (mut tx, mut rx) = uart.split();
    let shown = SHOWN.get();
    let words = pattern.len();
    let room = words + IDLE_MARGIN;
    let start = Instant::now();
    let mut last = start;
    let (res, count) = if use_u16 {
        let buffer = BUF16.get();
        buffer[..room].fill(SENTINEL16);
        let outcome = with_timeout(Duration::from_millis(FULL_MS), async {
            let (read, _written) =
                join(rx.read_until_idle_u16(&mut buffer[..room]), tx.write(pattern)).await;
            read
        })
        .await;
        let (res, got) = classify_count(outcome);
        shown[..got].copy_from_slice(&buffer[..got]);
        (res, got)
    } else {
        let buffer = BUF8.get();
        buffer[..room].fill(SENTINEL8);
        let outcome = with_timeout(Duration::from_millis(FULL_MS), async {
            let (read, _written) = join(rx.read_until_idle(&mut buffer[..room]), tx.write(pattern)).await;
            read
        })
        .await;
        let (res, got) = classify_count(outcome);
        widen(&buffer[..got], &mut shown[..]);
        (res, got)
    };
    state.record(res, &shown[..count]);
    if res != Res::Timeout {
        last = Instant::now();
    }
    let res = probe_idle(&mut rx, use_u16).await;
    state.record(res, &shown[..0]);
    state.ticks = (last - start).as_ticks() as u32;
}

pub async fn idl_synth(
    bus: &mut Bus,
    shape: &Shape,
    frames: &[u8],
    bad: Option<usize>,
    synth: &mut Synth,
    state: &mut RunState,
) {
    let use_u16 = wide(shape, Reader::Idl);
    state.begin(true, width_of(shape, Reader::Idl));
    let mut rx = UartRx::new(
        bus.usart.reborrow(),
        bus.rx.reborrow(),
        bus.rx_dma.reborrow(),
        Irqs,
        line(shape, Pull::Up),
    )
    .unwrap();
    let shown = SHOWN.get();
    synth.prepare(shape, frames, bad);
    let start = Instant::now();
    let mut last = start;
    let mut armed = false;
    while state.res.calls() < MAX_SYNTH_CALLS {
        // The transfer can never complete, so the call returns on the idle
        // line or on the error, and the buffer prefix shows what the DMA had
        // moved by then. inner_read masks only on success, so a failed call's
        // prefix is raw in both revisions.
        let (res, count) = if use_u16 {
            let buffer = BUF16.get();
            buffer[..IDLE_MARGIN].fill(SENTINEL16);
            let outcome = with_stimulus(
                with_timeout(probe(), rx.read_until_idle_u16(&mut buffer[..IDLE_MARGIN])),
                synth,
                &mut armed,
            )
            .await;
            let (res, got) = classify_count(outcome);
            let keep = shows_of(res, got);
            shown[..keep].copy_from_slice(&buffer[..keep]);
            (res, keep)
        } else {
            let buffer = BUF8.get();
            buffer[..IDLE_MARGIN].fill(SENTINEL8);
            let outcome = with_stimulus(
                with_timeout(probe(), rx.read_until_idle(&mut buffer[..IDLE_MARGIN])),
                synth,
                &mut armed,
            )
            .await;
            let (res, got) = classify_count(outcome);
            let keep = shows_of(res, got);
            widen(&buffer[..keep], &mut shown[..]);
            (res, keep)
        };
        state.record(res, &shown[..count]);
        if res != Res::Timeout {
            last = Instant::now();
        }
        if res.is_error() {
            state.note_error();
        }
        if res == Res::Timeout {
            break;
        }
    }
    state.synth = synth.finish();
    state.ticks = (last - start).as_ticks() as u32;
}

fn shows_of(res: Res, got: usize) -> usize {
    match res {
        Res::Ok => got,
        Res::Timeout => 0,
        _ => IDLE_PREFIX,
    }
}

async fn probe_idle(rx: &mut UartRx<'_, Async>, use_u16: bool) -> Res {
    if use_u16 {
        let mut buffer = [SENTINEL16; 2];
        let outcome = with_timeout(probe(), rx.read_until_idle_u16(&mut buffer)).await;
        classify_count(outcome).0
    } else {
        let mut buffer = [SENTINEL8; 2];
        let outcome = with_timeout(probe(), rx.read_until_idle(&mut buffer)).await;
        classify_count(outcome).0
    }
}

// ---- BufferedUartRx::read --------------------------------------------------

pub async fn buf_readback(bus: &mut Bus, shape: &Shape, pattern: &[u8], state: &mut RunState) {
    state.begin(false, width_of(shape, Reader::Buf));
    let tx_ring = TX_RING.get();
    let rx_ring = RX_RING.get();
    let uart = BufferedUart::new_half_duplex(
        bus.usart.reborrow(),
        bus.tx.reborrow(),
        Irqs,
        &mut tx_ring[..],
        &mut rx_ring[..],
        line(shape, Pull::None),
        HalfDuplexReadback::Readback,
    )
    .unwrap();
    let (mut tx, mut rx) = uart.split();
    let shown = SHOWN.get();
    let chunk = CHUNK8.get();
    let start = Instant::now();
    let mut last = start;
    // The whole pattern fits the transmit ring, so this returns at once and the
    // handler shifts the frames out and stores every one it reads back.
    let mut sent = 0usize;
    while sent < pattern.len() {
        let outcome = with_timeout(Duration::from_millis(FULL_MS), tx.write(&pattern[sent..])).await;
        match outcome {
            Ok(Ok(written)) if written > 0 => sent += written,
            _ => break,
        }
    }
    // The frames are all on the line before the first read. `read()` hands over
    // whatever the ring holds, which while the transmission runs is one byte per
    // call, and a sequence may make at most `MAX_CHUNK_CALLS` of them. The
    // receive ring is larger than the longest pattern, so nothing is lost by
    // waiting out the transmission first. The wait is counted from the frame
    // count and the baud rate at twelve bit times per frame, which is longer
    // than the ten or eleven a frame of these configurations takes, and it is
    // measured by embassy-time against the same clock the `clk` record shows.
    let airtime = (pattern.len() as u64 * 12_000) / u64::from(BAUD) + 3;
    Timer::after_millis(airtime).await;
    let mut got = 0usize;
    while got < pattern.len() && state.res.calls() < MAX_CHUNK_CALLS {
        let outcome = with_timeout(Duration::from_millis(FULL_MS), rx.read(&mut chunk[..])).await;
        let (res, count) = classify_count(outcome);
        widen(&chunk[..count], &mut shown[..]);
        state.record(res, &shown[..count]);
        got += count;
        if res == Res::Timeout {
            break;
        }
        last = Instant::now();
    }
    let outcome = with_timeout(probe(), rx.read(&mut chunk[..])).await;
    let (res, count) = classify_count(outcome);
    widen(&chunk[..count], &mut shown[..]);
    state.record(res, &shown[..count]);
    if res != Res::Timeout {
        last = Instant::now();
    }
    state.ticks = (last - start).as_ticks() as u32;
}

pub async fn buf_synth(
    bus: &mut Bus,
    shape: &Shape,
    frames: &[u8],
    bad: Option<usize>,
    synth: &mut Synth,
    state: &mut RunState,
) {
    state.begin(true, width_of(shape, Reader::Buf));
    let tx_ring = TX_RING.get();
    let rx_ring = RX_RING.get();
    let uart = BufferedUart::new(
        bus.usart.reborrow(),
        bus.tx.reborrow(),
        bus.rx.reborrow(),
        Irqs,
        &mut tx_ring[..],
        &mut rx_ring[..],
        line(shape, Pull::Up),
    )
    .unwrap();
    let (_tx, mut rx) = uart.split();
    let shown = SHOWN.get();
    let chunk = CHUNK8.get();
    synth.prepare(shape, frames, bad);
    let start = Instant::now();
    let mut last = start;
    let mut armed = false;
    while state.res.calls() < MAX_SYNTH_CALLS {
        let outcome =
            with_stimulus(with_timeout(probe(), rx.read(&mut chunk[..])), synth, &mut armed).await;
        let (res, count) = classify_count(outcome);
        widen(&chunk[..count], &mut shown[..]);
        state.record(res, &shown[..count]);
        if res != Res::Timeout {
            last = Instant::now();
        }
        if res.is_error() {
            state.note_error();
        }
        if res == Res::Timeout {
            break;
        }
    }
    state.synth = synth.finish();
    state.ticks = (last - start).as_ticks() as u32;
}

// ---- RingBufferedUartRx::read ----------------------------------------------

pub async fn rng_synth(
    bus: &mut Bus,
    shape: &Shape,
    frames: &[u8],
    bad: Option<usize>,
    expect: usize,
    synth: &mut Synth,
    state: &mut RunState,
) {
    let detail = bad.is_some();
    state.begin(detail, width_of(shape, Reader::Rng));
    // The ring lives longer than the reader that owns it, so it is taken before
    // the reader exists.
    let ring8 = RING8.get();
    let ring16 = RING16.get();
    let rx = UartRx::new(
        bus.usart.reborrow(),
        bus.rx.reborrow(),
        bus.rx_dma.reborrow(),
        Irqs,
        line(shape, Pull::Up),
    )
    .unwrap();
    let shown = SHOWN.get();
    synth.prepare(shape, frames, bad);
    // A wrong-parity sequence stops at its first timeout, which is its probe; a
    // full one collects the pattern and then probes once.
    let timeout = if detail { probe() } else { Duration::from_millis(FULL_MS) };
    let limit = if detail { MAX_SYNTH_CALLS } else { MAX_CHUNK_CALLS };
    let start = Instant::now();
    let mut last = start;
    let mut armed = false;
    let mut got = 0usize;
    // Whether `synth.finish()` has already run, which a full sequence does
    // after its first call: the stimulus is over by then and the rest of the
    // pattern waits in the ring, so the chunks that follow are full ones.
    let mut settled = false;
    if wide(shape, Reader::Rng) {
        let mut ring = rx.into_ring_buffered(&mut ring16[..]);
        let chunk = CHUNK16.get();
        while state.res.calls() < limit {
            let outcome =
                with_stimulus(with_timeout(timeout, ring.read(&mut chunk[..])), synth, &mut armed).await;
            let (res, count) = classify_count(outcome);
            shown[..count].copy_from_slice(&chunk[..count]);
            state.record(res, &shown[..count]);
            got += count;
            settled = settle(synth, state, detail, settled);
            if res != Res::Timeout {
                last = Instant::now();
            }
            if res.is_error() {
                state.note_error();
            }
            if res == Res::Timeout || (!detail && got >= expect) {
                break;
            }
        }
        if !detail {
            let outcome = with_timeout(probe(), ring.read(&mut chunk[..])).await;
            let (res, count) = classify_count(outcome);
            shown[..count].copy_from_slice(&chunk[..count]);
            state.record(res, &shown[..count]);
            if res != Res::Timeout {
                last = Instant::now();
            }
        }
    } else {
        let mut ring = rx.into_ring_buffered(&mut ring8[..]);
        let chunk = CHUNK8.get();
        while state.res.calls() < limit {
            let outcome =
                with_stimulus(with_timeout(timeout, ring.read(&mut chunk[..])), synth, &mut armed).await;
            let (res, count) = classify_count(outcome);
            widen(&chunk[..count], &mut shown[..]);
            state.record(res, &shown[..count]);
            got += count;
            settled = settle(synth, state, detail, settled);
            if res != Res::Timeout {
                last = Instant::now();
            }
            if res.is_error() {
                state.note_error();
            }
            if res == Res::Timeout || (!detail && got >= expect) {
                break;
            }
        }
        if !detail {
            let outcome = with_timeout(probe(), ring.read(&mut chunk[..])).await;
            let (res, count) = classify_count(outcome);
            widen(&chunk[..count], &mut shown[..]);
            state.record(res, &shown[..count]);
            if res != Res::Timeout {
                last = Instant::now();
            }
        }
    }
    if !settled {
        state.synth = synth.finish();
    }
    state.ticks = (last - start).as_ticks() as u32;
}
