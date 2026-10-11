//! What every Embassy USART reader returns with parity enabled, on a NUCLEO-F446RE.
//!
//! Two images are built from this one crate, one against the merge base of
//! Embassy PR #7224 and one against its head; the code here is the same in
//! both. Each image runs the whole matrix (five readers, four line
//! configurations, two in-chip frame sources, twenty repetitions), resets
//! itself through SYSRESETREQ and runs it again, then prints `HQ178 done` and
//! stops.
//!
//! The image prints raw measurements only: the values each read returned, its
//! result, and after an error two snapshots of `USART_SR`. It never prints a
//! verdict; `tests/hq178` computes every expectation from the value sent, the
//! parity rule and the base or head code. `firmware/hq178/README.md` explains
//! the records.

#![no_std]
#![no_main]

use embassy_executor::Spawner;
use embassy_stm32::usart::{Config, Uart};
use embassy_time::Timer;

use crate::console::{Console, Static};
use crate::readers::{Bus, MAX_CALLS, Reader, RunState};
use crate::synth::{Par, SHAPES, Shape, Synth};

mod console;
mod readers;
mod synth;

/// Repetitions per configuration, reader and phase.
const REPS: u32 = 20;
/// Words of one `val` record: 24 four-digit words stay inside the 128
/// characters a field may carry.
const PART: usize = 24;
/// The frame of a wrong-parity sequence whose parity bit is inverted.
const BAD_INDEX: usize = 2;

/// The pull request's own two values after every word of the width, so the
/// gate's pattern and the image's agree without either reading the other.
const PATTERN7_LEN: usize = 0x80 + 2;
const PATTERN8_LEN: usize = 0x100 + 2;
static PATTERN7: Static<[u8; PATTERN7_LEN]> = Static::new([0; PATTERN7_LEN]);
static PATTERN8: Static<[u8; PATTERN8_LEN]> = Static::new([0; PATTERN8_LEN]);

/// The six frames of a control or wrong-parity sequence: alternating bits, so a
/// lost or reordered word cannot look like its neighbour.
static W7: [u8; 6] = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66];
static W8: [u8; 6] = [0x91, 0x22, 0xB3, 0x44, 0xD5, 0x66];

/// The three frames the register control sends per configuration.
const CTL_FRAMES: [(&str, u8); 3] = [("ok", 0x01), ("ok", 0x03), ("bad", 0x01)];

/// RCC_CSR: SFTRSTF (28) set and every other reset flag clear. PINRSTF (26) is
/// ignored because a system reset also pulls NRST on this part, and RMVF (24)
/// because the image sets it itself.
const RESET_MASK: u32 = 0xFA00_0000;
const RESET_SOFT: u32 = 0x1000_0000;

/// `HQ17` as a word: a marker another image's leftovers cannot imitate.
const MAGIC: u32 = 0x4851_3137;
const ARMED: [u32; 3] = [MAGIC, 1, !1u32];

/// Three words of RAM the startup code does not touch, so they survive the
/// reset the image causes itself. They are read and zeroed at every boot and
/// only ever honoured together with the reset cause, so a reset the bench
/// caused always starts the matrix from the beginning.
#[unsafe(link_section = ".uninit.HQ178")]
static MARKER: Static<[u32; 3]> = Static::new([0; 3]);

static RUN: Static<RunState> = Static::new(RunState::new());

#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    // Nothing more is printed, so the bench's wait for `HQ178 done` fails and
    // the gate reports an image that did not finish.
    loop {}
}

#[derive(Copy, Clone, PartialEq, Eq)]
enum Src {
    Readback,
    Synth,
}

impl Src {
    fn name(self) -> &'static str {
        match self {
            Src::Readback => "rb",
            Src::Synth => "sy",
        }
    }
}

#[derive(Copy, Clone, PartialEq, Eq)]
enum Kind {
    Full,
    Ctl,
    Bad,
}

impl Kind {
    fn name(self) -> &'static str {
        match self {
            Kind::Full => "full",
            Kind::Ctl => "ctl",
            Kind::Bad => "bad",
        }
    }
}

struct Step {
    reader: Reader,
    src: Src,
    kind: Kind,
}

const fn step(reader: Reader, src: Src, kind: Kind) -> Step {
    Step { reader, src, kind }
}

/// What every reader runs where parity is enabled: the whole pattern read back
/// from the USART's own transmission, a synthesized control with the right
/// parity, and the same six frames with one parity bit inverted. The
/// ring-buffered reader cannot read back (`read()` switches the transmitter
/// off), so its pattern is synthesized too.
static PARITY_STEPS: [Step; 14] = [
    step(Reader::Blk, Src::Readback, Kind::Full),
    step(Reader::Blk, Src::Synth, Kind::Ctl),
    step(Reader::Blk, Src::Synth, Kind::Bad),
    step(Reader::Asy, Src::Readback, Kind::Full),
    step(Reader::Asy, Src::Synth, Kind::Ctl),
    step(Reader::Asy, Src::Synth, Kind::Bad),
    step(Reader::Idl, Src::Readback, Kind::Full),
    step(Reader::Idl, Src::Synth, Kind::Ctl),
    step(Reader::Idl, Src::Synth, Kind::Bad),
    step(Reader::Buf, Src::Readback, Kind::Full),
    step(Reader::Buf, Src::Synth, Kind::Ctl),
    step(Reader::Buf, Src::Synth, Kind::Bad),
    step(Reader::Rng, Src::Synth, Kind::Full),
    step(Reader::Rng, Src::Synth, Kind::Bad),
];

/// 8N1 has no parity bit, so there is no wrong parity to inject.
static PLAIN_STEPS: [Step; 9] = [
    step(Reader::Blk, Src::Readback, Kind::Full),
    step(Reader::Blk, Src::Synth, Kind::Ctl),
    step(Reader::Asy, Src::Readback, Kind::Full),
    step(Reader::Asy, Src::Synth, Kind::Ctl),
    step(Reader::Idl, Src::Readback, Kind::Full),
    step(Reader::Idl, Src::Synth, Kind::Ctl),
    step(Reader::Buf, Src::Readback, Kind::Full),
    step(Reader::Buf, Src::Synth, Kind::Ctl),
    step(Reader::Rng, Src::Synth, Kind::Full),
];

fn steps(shape: &Shape) -> &'static [Step] {
    if shape.parity == Par::None { &PLAIN_STEPS } else { &PARITY_STEPS }
}

fn build_patterns() {
    let seven = PATTERN7.get();
    for (at, slot) in seven.iter_mut().enumerate().take(0x80) {
        *slot = at as u8;
    }
    seven[0x80] = 0x01;
    seven[0x81] = 0x03;
    let eight = PATTERN8.get();
    for (at, slot) in eight.iter_mut().enumerate().take(0x100) {
        *slot = at as u8;
    }
    eight[0x100] = 0x01;
    eight[0x101] = 0x03;
}

fn pattern_of(shape: &Shape) -> &'static [u8] {
    if shape.data_bits == 7 { &PATTERN7.shared()[..] } else { &PATTERN8.shared()[..] }
}

fn frames_of(shape: &Shape) -> &'static [u8] {
    if shape.data_bits == 7 { &W7 } else { &W8 }
}

// ---- the marker that crosses the image's own reset -------------------------

fn read_marker() -> [u32; 3] {
    let at = MARKER.pointer() as *const u32;
    // SAFETY: three words of a NOLOAD section this image owns; read word by
    // word so the compiler cannot assume what uninitialised RAM holds.
    unsafe { [at.read_volatile(), at.add(1).read_volatile(), at.add(2).read_volatile()] }
}

fn write_marker(words: [u32; 3]) {
    let at = MARKER.pointer() as *mut u32;
    // SAFETY: as above.
    unsafe {
        at.write_volatile(words[0]);
        at.add(1).write_volatile(words[1]);
        at.add(2).write_volatile(words[2]);
    }
}

// ---- printing --------------------------------------------------------------

struct Where {
    phase: u32,
    rep: u32,
}

fn sequence_fields(console: &mut Console<'_>, at: &Where, shape: &Shape, step: &Step) {
    console.u("phase", at.phase);
    console.u("rep", at.rep);
    console.s("cfg", shape.name);
    console.s("rd", step.reader.name());
    console.s("src", step.src.name());
    console.s("kind", step.kind.name());
}

fn print_run(console: &mut Console<'_>, at: &Where, shape: &Shape, step: &Step, state: &mut RunState) {
    state.res.seal();
    console.begin("run");
    sequence_fields(console, at, shape, step);
    console.u("n", state.n as u32);
    console.u("width", state.width as u32);
    console.u("calls", state.res.calls());
    console.s("res", state.res.as_str());
    console.u("ticks", state.ticks);
    console.s("synth", state.synth.name());
    console.end();

    if state.detail {
        // A control or wrong-parity sequence: one record per call, because what
        // each call returned is the measurement.
        for index in 0..state.calls.min(MAX_CALLS) {
            console.begin("call");
            sequence_fields(console, at, shape, step);
            console.u("i", index as u32);
            let call = &state.call[index];
            console.s("res", call.res.name());
            console.u("n", u32::from(call.returned));
            console.words("hex", &call.words[..call.words_len], state.width);
            match call.sr0 {
                Some(value) => console.x("sr0", value),
                None => console.s("sr0", "-"),
            }
            match call.sr1 {
                Some(value) => console.x("sr1", value),
                None => console.s("sr1", "-"),
            }
            console.end();
        }
    } else {
        // A full sequence: the word list, split over as many records as it needs.
        let mut part = 0u32;
        let mut from = 0usize;
        while from < state.values_len {
            let to = (from + PART).min(state.values_len);
            console.begin("val");
            sequence_fields(console, at, shape, step);
            console.u("part", part);
            console.words("hex", &state.values[from..to], state.width);
            console.end();
            part += 1;
            from = to;
        }
    }
}

// ---- one phase -------------------------------------------------------------

fn register_controls(console: &mut Console<'_>, bus: &mut Bus, synth: &mut Synth, phase: u32) {
    for shape in SHAPES.iter().filter(|shape| shape.parity != Par::None) {
        // The instance clocks the peripheral and configures the pins; CR1 is
        // then written from RM0390 and every frame read straight out of DR, so
        // the control shares no code with the readers it is a control for.
        let uart = Uart::new_blocking(
            bus.usart.reborrow(),
            bus.tx.reborrow(),
            bus.rx.reborrow(),
            readers::line(shape, embassy_stm32::gpio::Pull::Up),
        )
        .unwrap();
        let probe = synth::configure_by_hand(shape, synth);
        for (kind, value) in CTL_FRAMES {
            let control = synth::measure_frame(synth, shape, value, kind == "bad");
            console.begin("ctl");
            console.u("phase", phase);
            console.s("cfg", shape.name);
            console.s("kind", kind);
            console.x("val", u32::from(value));
            console.x("sr", control.sr);
            console.x("dr", control.dr);
            console.i("pe_lead", control.pe_lead);
            console.u("poll", control.poll);
            console.u("timeout", control.timeout);
            // What the stimulus itself did, with the receiver off: the pin's
            // level under each pull, PA10's mode and alternate function as the
            // constructor left them, and the cycles DMA2 took for `pwords`
            // updates. A frame the readers never see is one of these.
            console.x("pin_lo", probe.low);
            console.x("pin_hi", probe.high);
            console.u("pin_mode", probe.moder);
            console.u("pin_af", probe.af);
            console.u("pwords", probe.words);
            console.u("prate", probe.rate);
            console.end();
        }
        drop(uart);
    }
}

async fn run_step(bus: &mut Bus, shape: &Shape, step: &Step, synth: &mut Synth, state: &mut RunState) {
    let bad = if step.kind == Kind::Bad { Some(BAD_INDEX) } else { None };
    let pattern = pattern_of(shape);
    let frames = frames_of(shape);
    match (step.reader, step.src) {
        (Reader::Blk, Src::Readback) => readers::blk_readback(bus, shape, pattern, state).await,
        (Reader::Blk, Src::Synth) => readers::blk_synth(bus, shape, frames, bad, synth, state).await,
        (Reader::Asy, Src::Readback) => readers::asy_readback(bus, shape, pattern, state).await,
        (Reader::Asy, Src::Synth) => readers::asy_synth(bus, shape, frames, bad, synth, state).await,
        (Reader::Idl, Src::Readback) => readers::idl_readback(bus, shape, pattern, state).await,
        (Reader::Idl, Src::Synth) => readers::idl_synth(bus, shape, frames, bad, synth, state).await,
        (Reader::Buf, Src::Readback) => readers::buf_readback(bus, shape, pattern, state).await,
        (Reader::Buf, Src::Synth) => readers::buf_synth(bus, shape, frames, bad, synth, state).await,
        (Reader::Rng, _) => {
            if step.kind == Kind::Full {
                readers::rng_synth(bus, shape, pattern, None, pattern.len(), synth, state).await;
            } else {
                readers::rng_synth(bus, shape, frames, bad, frames.len(), synth, state).await;
            }
        }
    }
}

async fn run_phase(console: &mut Console<'_>, bus: &mut Bus, synth: &mut Synth, phase: u32) {
    // The clock configuration this phase measures with. BRR comes from an
    // instance configured at 19200 baud, and the cycle counter is checked
    // against embassy-time over 100 ms, so neither rests on the other.
    let (_, brr) = readers::configuration_registers(bus, &SHAPES[3]);
    let before = synth::cyccnt();
    Timer::after_millis(100).await;
    let dwt100 = synth::cyccnt().wrapping_sub(before);
    console.begin("clk");
    console.u("usart1", synth::pclk2_hz());
    console.u("usart2", synth::pclk1_hz());
    console.u("tim8", synth::tim8_hz());
    console.x("cfgr", synth::rd(synth::RCC_CFGR));
    console.x("brr1", brr);
    console.u("dwt100", dwt100);
    console.end();

    register_controls(console, bus, synth, phase);

    let state = RUN.get();
    let mut runs = 0u32;
    for rep in 0..REPS {
        let at = Where { phase, rep };
        for shape in SHAPES.iter() {
            let (cr1, brr) = readers::configuration_registers(bus, shape);
            console.begin("cfg");
            console.u("phase", phase);
            console.u("rep", rep);
            console.s("cfg", shape.name);
            console.u("baud", readers::BAUD);
            console.u("data", u32::from(shape.data_bits));
            console.s("parity", shape.parity.name());
            console.u("stop", 1);
            console.x("cr1", cr1);
            console.x("brr", brr);
            console.end();
            for step in steps(shape) {
                run_step(bus, shape, step, synth, state).await;
                print_run(console, &at, shape, step, state);
                runs += 1;
            }
        }
    }

    console.begin("matrix");
    console.u("phase", phase);
    console.u("reps", REPS);
    console.u("runs", runs);
    console.end();
}

#[embassy_executor::main]
async fn main(_spawner: Spawner) {
    let p = embassy_stm32::init(Default::default());

    // The reset cause and the marker first, before anything else can change
    // either; the marker is zeroed at once, so a reset at any later moment
    // finds none.
    let csr = synth::rd(synth::RCC_CSR);
    let marker = read_marker();
    write_marker([0, 0, 0]);
    cortex_m::asm::dsb();
    let hit = marker == ARMED;
    let phase = if csr & RESET_MASK == RESET_SOFT && hit { 2 } else { 1 };
    let found = if hit {
        "hit"
    } else if marker[0] == MAGIC {
        "stale"
    } else {
        "none"
    };
    synth::wr(synth::RCC_CSR, csr | synth::RCC_CSR_RMVF);

    let mut console = Console::new(
        Uart::new_blocking(p.USART2, p.PA2, p.PA3, Config::default()).unwrap(),
    );
    console.blank();
    console.begin("boot");
    console.x("csr", csr);
    console.u("phase", phase);
    console.s("marker", found);
    console.end();

    synth::enable_cycle_counter();
    Synth::clocks_on();
    build_patterns();

    let mut bus = Bus {
        usart: p.USART1,
        tx: p.PA9,
        rx: p.PA10,
        tx_dma: p.DMA2_CH7,
        rx_dma: p.DMA2_CH2,
    };
    let mut synth = Synth::new();

    run_phase(&mut console, &mut bus, &mut synth, phase).await;

    if phase == 1 {
        // Nothing is printed between the matrix line and the reset, so the
        // capture shows that the second phase ran on a board that restarted.
        console.flush();
        write_marker(ARMED);
        cortex_m::asm::dsb();
        cortex_m::peripheral::SCB::sys_reset();
    }

    console.bare("done");
    console.flush();
    loop {
        cortex_m::asm::wfi();
    }
}
