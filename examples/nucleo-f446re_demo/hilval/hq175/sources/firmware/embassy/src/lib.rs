//! The measurement rig both hq175 images share: it prints nothing but raw values.
//!
//! The images drive TIM1 CH1 on PA8 and TIM1 CH1N on PA7 with a PWM of a known
//! period, duty and dead time, switch the two outputs on and off, and measure both
//! pins entirely inside the chip: TIM8 requests one DMA transfer per update event
//! and DMA2 stream 1 copies `GPIOA_IDR` into RAM, so a buffer of samples holds the
//! level of both outputs over many PWM periods. The sampling interval (16 ticks) and
//! the PWM period (159 ticks) are coprime, so 159 consecutive samples visit every
//! tick offset of the period exactly once and a high fraction is a duty cycle.
//!
//! Nothing here calls the API this job proposes; the two binaries differ only in
//! which calls they make between the register readback and the measurement. The
//! sampler, the pins' pulls and the register readback go through raw register
//! addresses, so what is measured cannot be changed by the driver under test.
//!
//! Every record is a line `HQ175 <kind> key=value ...` on USART2 (PA2/PA3, 115200
//! baud 8N1); the tests in tests/hq175/ judge the values. The record schema is
//! described in firmware/hq175/README.md.
#![no_std]

use core::cell::UnsafeCell;
use core::fmt::Write as _;
use core::sync::atomic::{Ordering, compiler_fence};

use embassy_stm32::mode::Blocking;
use embassy_stm32::usart::Uart;

/// The tag every line of both images carries.
pub const TAG: &str = "HQ175";

/// Samples per measurement: 12 x 159, twelve whole sweeps of the PWM period, so an
/// expected high count is an exact multiple and an expected edge count is exact.
pub const SAMPLES: usize = 1908;
/// Timer ticks between two samples. gcd(16, 159) = 1 is what makes the phase sweep.
pub const TICK: u32 = 16;
/// TIM1 period in ticks (PSC = 0, ARR = PERIOD - 1), no multiple of TICK.
pub const PERIOD: u32 = 159;
/// CCR1: OC1REF is high for 40 of the 159 ticks, a duty of 25.2 %.
pub const HIGH: u32 = 40;
/// Dead time in ticks; `set_dead_time(8)` maps to CKD = Div1 with DTG = 8.
pub const DEAD: u16 = 8;
/// Repetitions of each state sequence; the issue asks for at least 100.
pub const REPS: u32 = 100;

/// One output switched per step, so every step has an output that must not change,
/// and the four states (both off, main only, both on, complementary only) are reached.
pub const GRAY: [&str; 4] = ["main_on", "comp_on", "main_off", "comp_off"];
/// The two calls that exist today, which switch both outputs of a channel together.
pub const LEGACY: [&str; 2] = ["enable", "disable"];

/// Settle time after a CCER change before the sampler is armed: about twelve PWM
/// periods at 16 MHz, and far longer than the ~0.4 us a released pin needs to reach
/// its pull's level.
const SETTLE_CYCLES: u32 = 2000;

/// 4 samples per byte: 477 bytes, sent as 8 `trace` records of at most 64 bytes.
const TRACE_PARTS: usize = 8;
const TRACE_PART_BYTES: usize = 64;
const TRACE_BYTES: usize = SAMPLES / 4;

/// PA8 carries TIM1_CH1, PA7 carries TIM1_CH1N, so one read of GPIOA_IDR catches both.
const CH1_BIT: u16 = 1 << 8;
const CH1N_BIT: u16 = 1 << 7;

/// Transfer-complete flag of DMA2 stream 1 in the `flags` field, which is
/// `(DMA2_LISR >> 6) & 0x3F`: stream 1's five flags moved down to bit 0.
const FLAG_TCIF: u32 = 1 << 5;
/// The five flags of one stream, as they sit in the low interrupt status register.
const STREAM_FLAGS: u32 = 0x3D;
/// Stream 1's flags start at bit 6 of DMA2_LISR.
const STREAM_SHIFT: u32 = 6;

// ---- registers -------------------------------------------------------------
//
// Addresses from RM0390 for the STM32F446. The sampler and the readback use them
// directly for two reasons: the measurement must not depend on a driver that could
// change what it measures, and the technique (a raw DMA stream copying GPIOA_IDR on
// another timer's update) is the one the job's issue names.

const RCC_CFGR: u32 = 0x4002_3808;
const RCC_AHB1ENR: u32 = 0x4002_3830;
const RCC_APB2ENR: u32 = 0x4002_3844;
const RCC_AHB1ENR_GPIOAEN: u32 = 1 << 0;
const RCC_AHB1ENR_DMA2EN: u32 = 1 << 22;
const RCC_APB2ENR_TIM8EN: u32 = 1 << 1;

const GPIOA_MODER: u32 = 0x4002_0000;
const GPIOA_PUPDR: u32 = 0x4002_000C;
const GPIOA_IDR: u32 = 0x4002_0010;
const GPIOA_AFRL: u32 = 0x4002_0020;
const GPIOA_AFRH: u32 = 0x4002_0024;

const TIM1: u32 = 0x4001_0000;
const TIM8: u32 = 0x4001_0400;
const TIM_CR1: u32 = 0x00;
const TIM_CR2: u32 = 0x04;
const TIM_DIER: u32 = 0x0C;
const TIM_SR: u32 = 0x10;
const TIM_EGR: u32 = 0x14;
const TIM_CCER: u32 = 0x20;
const TIM_CNT: u32 = 0x24;
const TIM_PSC: u32 = 0x28;
const TIM_ARR: u32 = 0x2C;
const TIM_CCR1: u32 = 0x34;
const TIM_BDTR: u32 = 0x44;
const TIM_CR1_CEN: u32 = 1 << 0;
const TIM_EGR_UG: u32 = 1 << 0;
const TIM_DIER_UDE: u32 = 1 << 8;

const DMA2_LISR: u32 = 0x4002_6400;
const DMA2_LIFCR: u32 = 0x4002_6408;
// Stream 1: its control block starts 0x18 bytes after stream 0's at base + 0x10.
const DMA2_S1CR: u32 = 0x4002_6428;
const DMA2_S1NDTR: u32 = 0x4002_642C;
const DMA2_S1PAR: u32 = 0x4002_6430;
const DMA2_S1M0AR: u32 = 0x4002_6434;
const DMA2_S1FCR: u32 = 0x4002_643C;
/// DMA2 stream 1, channel 7 is TIM8_UP.
const DMA2_S1_TIM8_UP: u32 = 7;
/// Peripheral to memory, 16 bit on both sides, memory incrementing, high priority,
/// channel 7. Every interrupt enable stays clear, so embassy's DMA interrupt
/// handlers, which it enables for every stream, never see this one.
const DMA2_S1CR_SETUP: u32 = (DMA2_S1_TIM8_UP << 25) | (2 << 16) | (1 << 13) | (1 << 11) | (1 << 10);
const DMA2_SCR_EN: u32 = 1 << 0;

const DEMCR: u32 = 0xE000_EDFC;
const DWT_CTRL: u32 = 0xE000_1000;
const DWT_CYCCNT: u32 = 0xE000_1004;
const DWT_LAR: u32 = 0xE000_1FB0;

const USART2_SR: u32 = 0x4000_4400;
const USART2_DR: u32 = 0x4000_4404;
const USART_SR_TXE: u32 = 1 << 7;

#[inline]
fn read(address: u32) -> u32 {
    unsafe { (address as *const u32).read_volatile() }
}

#[inline]
fn write(address: u32, value: u32) {
    unsafe { (address as *mut u32).write_volatile(value) }
}

/// The core's cycle counter, which counts HCLK cycles. Time is measured here and
/// never on the host.
#[inline]
pub fn cycles() -> u32 {
    read(DWT_CYCCNT)
}

/// Busy-waits `count` HCLK cycles.
pub fn delay_cycles(count: u32) {
    let start = cycles();
    while cycles().wrapping_sub(start) < count {}
}

// ---- the sample buffer -----------------------------------------------------

/// Where DMA2 stream 1 writes. Only `measure`, `trace_byte` and the arming code
/// touch it, one transfer at a time: the image is single-threaded (one task, no
/// interrupt handler of its own), so there is never a second user.
struct SampleBuffer(UnsafeCell<[u16; SAMPLES]>);

// SAFETY: see the type's documentation; nothing else ever holds a reference to it.
unsafe impl Sync for SampleBuffer {}

static SAMPLES_BUFFER: SampleBuffer = SampleBuffer(UnsafeCell::new([0; SAMPLES]));

fn samples_pointer() -> *mut u16 {
    SAMPLES_BUFFER.0.get() as *mut u16
}

fn sample(index: usize) -> u16 {
    unsafe { samples_pointer().add(index).read_volatile() }
}

// ---- the pins, the clocks and the cycle counter ----------------------------

/// Clocks what the rig uses itself: GPIOA for the pins and their pulls, DMA2 for the
/// sampler and TIM8 for its trigger. embassy clocks GPIOA and TIM1 for its own
/// drivers; setting a bit that is already set changes nothing.
fn enable_clocks() {
    write(RCC_AHB1ENR, read(RCC_AHB1ENR) | RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_DMA2EN);
    write(RCC_APB2ENR, read(RCC_APB2ENR) | RCC_APB2ENR_TIM8EN);
    // The reference manual asks for a read-back before the clocked block is used.
    let _ = read(RCC_APB2ENR);
}

fn enable_cycle_counter() {
    write(DEMCR, read(DEMCR) | (1 << 24)); // TRCENA
    write(DWT_LAR, 0xC5AC_CE55); // unlocks the DWT on parts that lock it
    write(DWT_CYCCNT, 0);
    write(DWT_CTRL, read(DWT_CTRL) | 1); // CYCCNTENA
}

/// Pull-up (`up`) or pull-down on PA7 and PA8, in PUPDR beside the alternate
/// function the timer drives them with.
///
/// An output the timer has released floats, so it reads 1 under the pull-up and 0
/// under the pull-down. That difference is what tells a released output from one the
/// timer holds low, which no single pull could show.
fn set_pull(up: bool) {
    let bits: u32 = if up { 1 } else { 2 };
    let mask = (3 << 14) | (3 << 16); // PA7 is pair 7, PA8 is pair 8
    write(GPIOA_PUPDR, (read(GPIOA_PUPDR) & !mask) | (bits << 14) | (bits << 16));
}

/// Writes TIM1's prescaler and period in ticks and loads them at once.
///
/// Every expectation in the gate is a ratio of ticks of a 159-tick period, while
/// `ComplementaryPwm::set_frequency` can only land on a period the prescaler and the
/// reload register happen to represent for the clock the timer runs at. Writing the
/// two registers says exactly what the period is; the `setup` record reads both back
/// and the gate asserts them, so a wrong period fails as a set-up error.
pub fn force_period(period: u32) {
    write(TIM1 + TIM_PSC, 0);
    write(TIM1 + TIM_ARR, period - 1);
    // ARR is preloaded (the driver sets ARPE), so an update event loads it now.
    write(TIM1 + TIM_EGR, TIM_EGR_UG);
}

/// Sets or clears CC1E alone, which is what `Timer::enable_channel` does.
pub fn set_cc1e(on: bool) {
    set_ccer_bit(0, on);
}

/// Sets or clears CC1NE alone, which is what `Timer::enable_complementary_channel` does.
pub fn set_cc1ne(on: bool) {
    set_ccer_bit(2, on);
}

fn set_ccer_bit(bit: u32, on: bool) {
    let ccer = read(TIM1 + TIM_CCER);
    write(TIM1 + TIM_CCER, if on { ccer | (1 << bit) } else { ccer & !(1 << bit) });
}

// ---- the sampler -----------------------------------------------------------

/// Leaves TIM8 requesting one DMA transfer per update, one update every `TICK`
/// ticks, stopped. Only CEN and CNT change afterwards, so the `setup` record's
/// `tim8_arr` is the interval every measurement ran at.
fn sampler_init() {
    write(TIM8 + TIM_CR1, 0);
    write(TIM8 + TIM_DIER, 0);
    write(TIM8 + TIM_PSC, 0);
    write(TIM8 + TIM_ARR, TICK - 1);
    write(TIM8 + TIM_EGR, TIM_EGR_UG);
    write(TIM8 + TIM_SR, 0);
    // UDE is set after the update event above, so that event requests no transfer and
    // a measurement takes exactly SAMPLES updates after CEN.
    write(TIM8 + TIM_DIER, TIM_DIER_UDE);
}

/// HCLK in hertz, measured in-chip against the timer clock.
///
/// TIM8 counts timer ticks with PSC = 0 while the cycle counter counts HCLK cycles,
/// so counting both over one interval gives the ratio, and the timer clock gives the
/// scale. Only the sampler's expected duration uses this number, and only as the
/// ratio to `tim_hz`.
fn measure_hclk(tim_hz: u32) -> u32 {
    // 20000 ticks is 1.25 ms at 16 MHz and well inside TIM8's 16-bit range, so the
    // ratio is measured to better than 0.1 % without a wrap to reason about.
    const TICKS: u32 = 20_000;
    write(TIM8 + TIM_CR1, 0);
    write(TIM8 + TIM_DIER, 0);
    write(TIM8 + TIM_PSC, 0);
    write(TIM8 + TIM_ARR, 0xFFFF);
    write(TIM8 + TIM_EGR, TIM_EGR_UG);
    write(TIM8 + TIM_SR, 0);
    write(TIM8 + TIM_CNT, 0);
    write(TIM8 + TIM_CR1, TIM_CR1_CEN);
    let first = read(TIM8 + TIM_CNT) & 0xFFFF;
    let start = cycles();
    // Even an HCLK forty times the timer clock finishes inside this bound, so a TIM8
    // that never counts ends the wait instead of hanging the image.
    let limit = 40 * TICKS;
    while (read(TIM8 + TIM_CNT).wrapping_sub(first) & 0xFFFF) < TICKS
        && cycles().wrapping_sub(start) < limit
    {}
    let elapsed = cycles().wrapping_sub(start);
    let ticks = read(TIM8 + TIM_CNT).wrapping_sub(first) & 0xFFFF;
    write(TIM8 + TIM_CR1, 0);
    if ticks == 0 {
        // TIM8 did not count; the boot record then carries the timer clock, and the
        // sampler's duration check in the gate fails on every record.
        return tim_hz;
    }
    ((elapsed as u64 * tim_hz as u64) / ticks as u64) as u32
}

fn stream_flags() -> u32 {
    (read(DMA2_LISR) >> STREAM_SHIFT) & 0x3F
}

fn disarm_stream() {
    write(DMA2_S1CR, 0);
    while read(DMA2_S1CR) & DMA2_SCR_EN != 0 {}
    write(DMA2_LIFCR, STREAM_FLAGS << STREAM_SHIFT);
}

/// What one measurement found. Raw counts and flags; no verdict.
pub struct Measurement {
    /// HCLK cycles the transfer took, measured by the core's cycle counter.
    pub dur: u32,
    /// Transfers still outstanding in NDTR; 0 after a complete transfer.
    pub left: u32,
    /// `(DMA2_LISR >> 6) & 0x3F`: stream 1's flags, read once before anything is cleared.
    pub flags: u32,
    pub h1: u32,
    pub h1n: u32,
    pub both: u32,
    pub e1: u32,
    pub e1n: u32,
}

/// One transfer of `SAMPLES` reads of GPIOA_IDR, one every `TICK` timer ticks, and
/// the counts over what it recorded.
pub fn measure() -> Measurement {
    // 0xFFFF is no level either pin can read as part of a PWM, so a sample the DMA
    // never wrote cannot pass as a plausible one.
    for index in 0..SAMPLES {
        unsafe { samples_pointer().add(index).write_volatile(0xFFFF) };
    }
    write(TIM8 + TIM_CR1, 0);
    write(TIM8 + TIM_CNT, 0);
    write(TIM8 + TIM_SR, 0);
    disarm_stream();
    write(DMA2_S1PAR, GPIOA_IDR);
    write(DMA2_S1M0AR, samples_pointer() as u32);
    write(DMA2_S1NDTR, SAMPLES as u32);
    write(DMA2_S1FCR, 0); // direct mode: no FIFO, no burst
    write(DMA2_S1CR, DMA2_S1CR_SETUP);
    write(DMA2_S1CR, read(DMA2_S1CR) | DMA2_SCR_EN);
    compiler_fence(Ordering::SeqCst);

    let start = cycles();
    write(TIM8 + TIM_CR1, TIM_CR1_CEN);
    // The transfer lasts SAMPLES * TICK ticks; four times that plus a fixed margin
    // bounds the wait even when no transfer ever starts.
    let limit = 4 * SAMPLES as u32 * TICK + 100_000;
    while stream_flags() & FLAG_TCIF == 0 && cycles().wrapping_sub(start) < limit {}
    let dur = cycles().wrapping_sub(start);
    // Read the flags once, here: before NDTR, before TIM8 stops, before the stream is
    // disabled and before any write to the flag-clear register, so they are the flags
    // of this transfer whether it completed or the bound ended the wait.
    let flags = stream_flags();
    let left = read(DMA2_S1NDTR) & 0xFFFF;
    write(TIM8 + TIM_CR1, 0);
    disarm_stream();
    compiler_fence(Ordering::SeqCst);

    let mut h1 = 0;
    let mut h1n = 0;
    let mut both = 0;
    let mut e1 = 0;
    let mut e1n = 0;
    let mut previous = 0u16;
    for index in 0..SAMPLES {
        let value = sample(index);
        let ch1 = value & CH1_BIT != 0;
        let ch1n = value & CH1N_BIT != 0;
        if ch1 {
            h1 += 1;
        }
        if ch1n {
            h1n += 1;
        }
        if ch1 && ch1n {
            both += 1;
        }
        if index > 0 {
            let changed = value ^ previous;
            if changed & CH1_BIT != 0 {
                e1 += 1;
            }
            if changed & CH1N_BIT != 0 {
                e1n += 1;
            }
        }
        previous = value;
    }
    Measurement {
        dur,
        left,
        flags,
        h1,
        h1n,
        both,
        e1,
        e1n,
    }
}

/// Byte `index` of the packed trace: samples 4*index .. 4*index+3, two bits each,
/// bit 0 of a sample for PA8 and bit 1 for PA7.
fn trace_byte(index: usize) -> u8 {
    let mut byte = 0u8;
    for offset in 0..4 {
        let value = sample(4 * index + offset);
        let mut bits = 0u8;
        if value & CH1_BIT != 0 {
            bits |= 1;
        }
        if value & CH1N_BIT != 0 {
            bits |= 2;
        }
        byte |= bits << (2 * offset);
    }
    byte
}

// ---- the console -----------------------------------------------------------

/// USART2 (PA2 TX, PA3 RX, 115200 8N1) is the ST-LINK's virtual COM port, which the
/// bench reads.
struct Serial<'d>(Uart<'d, Blocking>);

impl core::fmt::Write for Serial<'_> {
    fn write_str(&mut self, text: &str) -> core::fmt::Result {
        self.0.blocking_write(text.as_bytes()).map_err(|_| core::fmt::Error)
    }
}

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    // USART2 is already running by the time anything can panic, so the fault line
    // reaches the host even though the driver that owned the port is gone. No `done`
    // line follows, and the gate refuses any image that printed a `fault` record.
    for byte in b"\r\nHQ175 fault kind=panic\r\n" {
        // A bounded wait: if USART2 is not running there is nowhere to print, and
        // spinning on TXE for ever would hide the fault behind the bench's timeout.
        let mut guard = 0u32;
        while read(USART2_SR) & USART_SR_TXE == 0 && guard < 1_000_000 {
            guard += 1;
        }
        write(USART2_DR, *byte as u32);
    }
    loop {}
}

/// The console and the measurement hardware of one image.
pub struct Rig<'d> {
    serial: Serial<'d>,
}

impl<'d> Rig<'d> {
    /// Clocks the sampler, starts the cycle counter and prints the `boot` record.
    ///
    /// `tim_hz` is the timer clock the caller read back from the driver; it and the
    /// measured HCLK enter nothing but the sampler's expected duration, and only as
    /// a ratio, because every other expectation is a ratio of timer ticks.
    pub fn new(uart: Uart<'d, Blocking>, image: &str, tim_hz: u32) -> Self {
        enable_clocks();
        enable_cycle_counter();
        let hclk_hz = measure_hclk(tim_hz);
        sampler_init();
        set_pull(false);
        let mut rig = Rig { serial: Serial(uart) };
        // Ends whatever partial line the reset left on the host's side.
        let _ = write!(rig.serial, "\r\n");
        rig.line("boot");
        rig.s("image", image);
        rig.u("tim_hz", tim_hz);
        rig.u("hclk_hz", hclk_hz);
        rig.x("rcc_cfgr", read(RCC_CFGR));
        rig.u("reps", REPS);
        rig.u("n", SAMPLES as u32);
        rig.u("tick", TICK);
        rig.end();
        rig
    }

    fn line(&mut self, kind: &str) {
        let _ = write!(self.serial, "{} {}", TAG, kind);
    }

    fn u(&mut self, key: &str, value: u32) {
        let _ = write!(self.serial, " {}={}", key, value);
    }

    fn x(&mut self, key: &str, value: u32) {
        let _ = write!(self.serial, " {}={:#x}", key, value);
    }

    fn s(&mut self, key: &str, value: &str) {
        let _ = write!(self.serial, " {}={}", key, value);
    }

    fn end(&mut self) {
        let _ = write!(self.serial, "\r\n");
    }

    /// The `setup` record of one sequence: every number the gate's expectations use,
    /// read back from the registers that hold it.
    pub fn print_setup(&mut self, seq: &str) {
        let cr1 = read(TIM1 + TIM_CR1);
        let cr2 = read(TIM1 + TIM_CR2);
        let bdtr = read(TIM1 + TIM_BDTR);
        let moder = read(GPIOA_MODER);
        let afrl = read(GPIOA_AFRL);
        let afrh = read(GPIOA_AFRH);
        self.line("setup");
        self.s("seq", seq);
        self.u("psc", read(TIM1 + TIM_PSC) & 0xFFFF);
        self.u("arr", read(TIM1 + TIM_ARR) & 0xFFFF);
        self.u("ccr1", read(TIM1 + TIM_CCR1) & 0xFFFF);
        self.x("cr1", cr1);
        self.x("cr2", cr2);
        self.x("ccer", read(TIM1 + TIM_CCER));
        self.x("bdtr", bdtr);
        self.u("dtg", bdtr & 0xFF);
        self.u("ckd", (cr1 >> 8) & 3);
        self.u("moe", (bdtr >> 15) & 1);
        self.u("ossr", (bdtr >> 11) & 1);
        self.u("ossi", (bdtr >> 10) & 1);
        self.u("ois1", (cr2 >> 8) & 1);
        self.u("oisn1", (cr2 >> 9) & 1);
        self.u("moder7", (moder >> 14) & 3);
        self.u("moder8", (moder >> 16) & 3);
        self.u("af7", (afrl >> 28) & 0xF);
        self.u("af8", afrh & 0xF);
        self.u("tim8_arr", read(TIM8 + TIM_ARR) & 0xFFFF);
        self.end();
    }

    /// The raw samples of one measurement, as `TRACE_PARTS` `trace` records, so the
    /// tests can count them again and judge the counting this image did.
    pub fn dump_trace(&mut self, seq: &str, rep: u32, step: &str) {
        for part in 0..TRACE_PARTS {
            let first = part * TRACE_PART_BYTES;
            let last = if first + TRACE_PART_BYTES < TRACE_BYTES {
                first + TRACE_PART_BYTES
            } else {
                TRACE_BYTES
            };
            self.line("trace");
            self.s("seq", seq);
            self.u("rep", rep);
            self.s("step", step);
            self.u("part", part as u32);
            let _ = write!(self.serial, " data=");
            for index in first..last {
                let _ = write!(self.serial, "{:02x}", trace_byte(index));
            }
            self.end();
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn print_pwm(
        &mut self,
        seq: &str,
        rep: u32,
        step: &str,
        pull: u32,
        ccer0: u32,
        ccer: u32,
        bdtr: u32,
        measurement: &Measurement,
        getters: Option<(bool, bool)>,
    ) {
        self.line("pwm");
        self.s("seq", seq);
        self.u("rep", rep);
        self.s("step", step);
        self.u("pull", pull);
        self.x("ccer0", ccer0);
        self.x("ccer", ccer);
        self.x("bdtr", bdtr);
        self.u("cc1e", ccer & 1);
        self.u("cc1ne", (ccer >> 2) & 1);
        self.u("cc1p", (ccer >> 1) & 1);
        self.u("cc1np", (ccer >> 3) & 1);
        self.u("moe", (bdtr >> 15) & 1);
        self.u("ossr", (bdtr >> 11) & 1);
        self.u("ossi", (bdtr >> 10) & 1);
        self.u("dtg", bdtr & 0xFF);
        self.u("n", SAMPLES as u32);
        self.u("dur", measurement.dur);
        self.u("left", measurement.left);
        self.x("flags", measurement.flags);
        self.u("h1", measurement.h1);
        self.u("h1n", measurement.h1n);
        self.u("both", measurement.both);
        self.u("e1", measurement.e1);
        self.u("e1n", measurement.e1n);
        if let Some((main, complementary)) = getters {
            self.u("g1", main as u32);
            self.u("g1n", complementary as u32);
        }
        self.end();
    }

    /// Prints `HQ175 done` and waits until the last byte has left the port.
    pub fn finish(&mut self) {
        self.line("done");
        self.end();
        let _ = self.serial.0.blocking_flush();
    }
}

/// Runs one state sequence `REPS` times and prints one `pwm` record per step.
///
/// `call` makes the call under test for a step and returns what the image can read
/// back about the two outputs, if anything. This function never calls the API under
/// test itself, so both images print the same records in the same order and differ
/// only in what `call` does.
///
/// Per step: CCER is read immediately before the call and again immediately after,
/// so the pair of readings belongs to that one call and nothing in between; then the
/// outputs are given time to settle and the sampler runs. The pins' pull alternates
/// per repetition, which is what tells a released output from one driven to that level.
/// The first `traced_reps` repetitions also print their raw samples.
pub fn run_sequence<F>(
    rig: &mut Rig<'_>,
    seq: &str,
    steps: &[&'static str],
    traced_reps: u32,
    mut call: F,
) where
    F: FnMut(&'static str) -> Option<(bool, bool)>,
{
    for rep in 0..REPS {
        let up = rep % 2 == 1;
        set_pull(up);
        for &step in steps {
            let ccer0 = read(TIM1 + TIM_CCER);
            let getters = call(step);
            let ccer = read(TIM1 + TIM_CCER);
            let bdtr = read(TIM1 + TIM_BDTR);
            delay_cycles(SETTLE_CYCLES);
            let measurement = measure();
            rig.print_pwm(
                seq,
                rep,
                step,
                up as u32,
                ccer0,
                ccer,
                bdtr,
                &measurement,
                getters,
            );
            if rep < traced_reps {
                rig.dump_trace(seq, rep, step);
            }
        }
    }
}
