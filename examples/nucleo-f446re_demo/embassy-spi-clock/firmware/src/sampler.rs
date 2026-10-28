//! Pad sampler: TIM8 update events trigger DMA2 stream 1 (channel 7) to copy one byte of
//! GPIOB_IDR into SRAM. It runs in the setup block only: its window calibration and its
//! qualification against a TIM1_CH1N PWM of exactly known frequency on PB13. Since HQ #342 no
//! matrix transfer is measured with it; the matrix uses the TIM8_CH2 capture path (icap.rs).
//!
//! The end of a capture window is timed by a second timer: TIM9 runs one-pulse for exactly the
//! window length and its update interrupt records the DWT cycle counter and the DMA's remaining
//! transfer count at that instant. The foreground may therefore be busy inside the SPI driver
//! for any length of time without disturbing the measurement; a DMA that missed requests shows
//! up as a non-zero count at the end of the window.
//!
//! Everything here is raw register access. The register addresses and the DMA request mapping
//! are taken from the reference manual from memory and are listed as unverified (prerequisite
//! P7) in the campaign README; the analyzer, not this file, decides whether the capture is
//! usable.

use core::ptr::{addr_of_mut, read_volatile, write_volatile};
use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use cortex_m::peripheral::DWT;

use crate::frozen::{self, Capture, GUARD, WINDOW};

const RCC: usize = 0x4002_3800;
const GPIOB: usize = 0x4002_0400;
const DMA2: usize = 0x4002_6400;
const DMA2_STREAM1: usize = DMA2 + 0x10 + 0x18;
const TIM1: usize = 0x4001_0000;
const TIM8: usize = 0x4001_0400;
const TIM9: usize = 0x4001_4000;
const NVIC_ISER0: usize = 0xE000_E100;

const SCK_PIN: u32 = 13; // PB13: SPI2_SCK (AF5) and TIM1_CH1N (AF1)
const ERROR_FLAGS: u32 = (1 << 9) | (1 << 8); // TEIF1 | DMEIF1
const STREAM1_CLEAR: u32 = 0x0F40; // LIFCR: CFEIF1 CDMEIF1 CTEIF1 CHTIF1 CTCIF1

static mut BUFFER: [u8; WINDOW] = [0; WINDOW];
/// Signed cycles between the nominal end of the sample window and the moment the end-of-window
/// interrupt recorded the counter, measured by `calibrate` on the slowest capture configuration
/// and subtracted from every capture's cycle count (stored as the bits of an i32).
static OVERHEAD: AtomicU32 = AtomicU32::new(0);
static END_DONE: AtomicBool = AtomicBool::new(false);
static END_CYCLES: AtomicU32 = AtomicU32::new(0);
static END_NDTR: AtomicU32 = AtomicU32::new(0);
// Diagnostics of the end-of-window interrupt, reported on the CAL record.
static ISR_ENTRIES: AtomicU32 = AtomicU32::new(0);
static ISR_SPURIOUS: AtomicU32 = AtomicU32::new(0);
static ISR_SR: AtomicU32 = AtomicU32::new(0);
static ISR_CNT: AtomicU32 = AtomicU32::new(0);
const WINDOW_IRQ: u32 = embassy_stm32::pac::Interrupt::TIM1_BRK_TIM9 as u32;

pub struct CaptureResult {
    pub cycles: u32,
    pub ndtr: u32,
    pub dma_err: u32,
    /// 1 if the end-of-window interrupt never arrived: the cycle count is then a polling
    /// timeout, not a measurement, and the capture must not be used.
    pub timed_out: u32,
}

unsafe fn rd(address: usize) -> u32 {
    read_volatile(address as *const u32)
}

unsafe fn wr(address: usize, value: u32) {
    write_volatile(address as *mut u32, value)
}

unsafe fn set_bits(address: usize, mask: u32) {
    wr(address, rd(address) | mask)
}

/// End-of-window interrupt (TIM9 update). Record the time first, then the DMA state.
#[no_mangle]
pub extern "C" fn TIM1_BRK_TIM9() {
    let now = DWT::cycle_count();
    unsafe {
        let status = rd(TIM9 + 0x10);
        ISR_SR.store(status, Ordering::Relaxed);
        ISR_CNT.store(rd(TIM9 + 0x24), Ordering::Relaxed);
        ISR_ENTRIES.store(ISR_ENTRIES.load(Ordering::Relaxed).wrapping_add(1), Ordering::Relaxed);
        if status & 1 == 0 {
            // Not a TIM9 update: mask the line so a stuck source cannot starve the foreground.
            ISR_SPURIOUS.store(ISR_SPURIOUS.load(Ordering::Relaxed).wrapping_add(1), Ordering::Relaxed);
            wr(NVIC_ISER0 + 0x80 + 4 * (WINDOW_IRQ as usize / 32), 1 << (WINDOW_IRQ % 32)); // ICER
            return;
        }
        if rd(TIM9) & 1 != 0 {
            // The one-pulse timer is still counting: a genuine end of window stops it first.
            ISR_SPURIOUS.store(ISR_SPURIOUS.load(Ordering::Relaxed).wrapping_add(1), Ordering::Relaxed);
            wr(TIM9 + 0x10, 0);
            return;
        }
        let ndtr = rd(DMA2_STREAM1 + 0x04);
        wr(TIM9 + 0x10, 0); // SR: clear UIF
        END_CYCLES.store(now, Ordering::Relaxed);
        END_NDTR.store(ndtr, Ordering::Relaxed);
    }
    END_DONE.store(true, Ordering::Release);
}

/// Enable the clocks of everything the sampler touches and the end-of-window interrupt.
pub fn init() {
    unsafe {
        set_bits(RCC + 0x30, (1 << 22) | (1 << 1)); // AHB1ENR: DMA2EN, GPIOBEN
        set_bits(RCC + 0x44, (1 << 16) | (1 << 1) | (1 << 0)); // APB2ENR: TIM9EN, TIM8EN, TIM1EN
        let _ = rd(RCC + 0x44); // let the enables settle before the first access
        wr(NVIC_ISER0 + 4 * (WINDOW_IRQ as usize / 32), 1 << (WINDOW_IRQ % 32));
    }
}

pub fn buffer() -> &'static [u8] {
    unsafe { &*addr_of_mut!(BUFFER) }
}

/// Capture `WINDOW` samples of the pad byte with the timing of `cap`. `during` runs once the
/// sampler is running; it is where the SPI transfer happens, and its value is returned.
pub fn capture<R>(cap: &Capture, during: impl FnOnce() -> R) -> (CaptureResult, R) {
    let cycles_per_sample = (cap.psc + 1) * (cap.arr + 1);
    let window = cycles_per_sample * WINDOW as u32;
    // TIM9 is a 16-bit timer: prescale by a power of two so the window is a whole tick count.
    let divider = if window > 131_072 { 4 } else if window > 65_536 { 2 } else { 1 };
    unsafe {
        // Stop everything and clear stale state.
        wr(TIM8, 0);
        wr(TIM8 + 0x0C, 0);
        wr(TIM9, 0);
        wr(TIM9 + 0x0C, 0);
        wr(DMA2_STREAM1, 0);
        while rd(DMA2_STREAM1) & 1 != 0 {}
        wr(DMA2 + 0x08, STREAM1_CLEAR);

        // Sampler timer: one update event every (psc+1)*(arr+1) timer ticks, none pending.
        wr(TIM8 + 0x28, cap.psc);
        wr(TIM8 + 0x2C, cap.arr);
        wr(TIM8 + 0x14, 1); // EGR.UG loads the registers and clears the counter
        wr(TIM8 + 0x10, 0); // SR

        // Window timer: one pulse, update event exactly `window` core cycles after it starts.
        wr(TIM9 + 0x28, divider - 1);
        wr(TIM9 + 0x2C, window / divider - 1);
        wr(TIM9, 1 << 3); // CR1: one-pulse mode, not started
        wr(TIM9 + 0x14, 1); // EGR.UG
        // The update flag the UG raises appears a few timer clocks after the write: wait for it
        // and clear it until it stays clear, or enabling the interrupt would fire at once.
        for _ in 0..16 {
            let _ = rd(TIM9 + 0x10);
        }
        loop {
            wr(TIM9 + 0x10, 0); // SR
            if rd(TIM9 + 0x10) & 1 == 0 {
                break;
            }
        }
        END_DONE.store(false, Ordering::Release);
        wr(NVIC_ISER0 + 4 * (WINDOW_IRQ as usize / 32), 1 << (WINDOW_IRQ % 32));
        wr(TIM9 + 0x0C, 1); // DIER.UIE

        // DMA: peripheral to memory, byte wide, memory increment, very high priority.
        wr(DMA2_STREAM1 + 0x08, (GPIOB + 0x10) as u32 + cap.idr_byte);
        wr(DMA2_STREAM1 + 0x0C, addr_of_mut!(BUFFER) as usize as u32);
        wr(DMA2_STREAM1 + 0x04, WINDOW as u32);
        wr(DMA2_STREAM1 + 0x14, 0); // FCR: direct mode
        let control = (7 << 25) | (3 << 16) | (1 << 10);
        wr(DMA2_STREAM1, control);
        wr(DMA2_STREAM1, control | 1);

        set_bits(TIM8 + 0x0C, 1 << 8); // DIER.UDE
        wr(TIM8, 1); // CR1.CEN: sampling starts
        wr(TIM9, (1 << 3) | 1); // CR1.CEN: the window starts a few cycles later
        let start = DWT::cycle_count();

        // Let a few idle samples pass before the transfer starts.
        let guard_limit = cycles_per_sample * (GUARD + 64);
        while rd(DMA2_STREAM1 + 0x04) > WINDOW as u32 - GUARD {
            if DWT::cycle_count().wrapping_sub(start) > guard_limit {
                break;
            }
        }
        let outcome = during();

        // Wait for the end-of-window interrupt (it may have fired long ago).
        let limit = window + 200_000;
        while !END_DONE.load(Ordering::Acquire) {
            if DWT::cycle_count().wrapping_sub(start) > limit {
                break;
            }
        }
        let finished = END_DONE.load(Ordering::Acquire);
        let observed = DWT::cycle_count();
        let flags = rd(DMA2);
        let ndtr = if finished { END_NDTR.load(Ordering::Relaxed) } else { rd(DMA2_STREAM1 + 0x04) };

        wr(TIM8, 0);
        wr(TIM8 + 0x0C, 0);
        wr(TIM9, 0);
        wr(TIM9 + 0x0C, 0);
        wr(DMA2_STREAM1, 0);
        while rd(DMA2_STREAM1) & 1 != 0 {}
        wr(DMA2 + 0x08, STREAM1_CLEAR);

        let raw = if finished {
            END_CYCLES.load(Ordering::Relaxed).wrapping_sub(start)
        } else {
            observed.wrapping_sub(start)
        };
        let corrected = (raw as i32).wrapping_sub(OVERHEAD.load(Ordering::Relaxed) as i32).max(0);
        (
            CaptureResult {
                cycles: corrected as u32,
                ndtr,
                dma_err: (flags & ERROR_FLAGS) >> 8,
                timed_out: !finished as u32,
            },
            outcome,
        )
    }
}

/// Measure the window overhead on the slowest configuration, where it is a tiny fraction of a
/// sample, and report it. The mean is what later captures subtract.
pub fn calibrate(hq: &mut crate::report::Hq) {
    const RUNS: i32 = 16;
    let cap = &frozen::CAPTURES[frozen::CAPTURES.len() - 1];
    let nominal = ((cap.psc + 1) * (cap.arr + 1) * WINDOW as u32) as i32;
    OVERHEAD.store(0, Ordering::Relaxed);
    let (mut least, mut most, mut sum, mut failed) = (i32::MAX, i32::MIN, 0i32, 0u32);
    for _ in 0..RUNS {
        let result = capture(cap, || {}).0;
        if result.timed_out != 0 {
            // A missed window interrupt must never become part of the offset.
            failed += 1;
            continue;
        }
        let extra = result.cycles as i32 - nominal;
        least = least.min(extra);
        most = most.max(extra);
        sum += extra;
    }
    let good = RUNS - failed as i32;
    // With any failed run the offset stays zero and the CAL record says so (failed > 0); the
    // analyzer rejects a hardware bundle whose calibration did not complete cleanly.
    let mean = if failed == 0 { sum / good } else { 0 };
    OVERHEAD.store(mean as u32, Ordering::Relaxed);
    if good == 0 {
        least = 0;
        most = 0;
    }
    hq.cal(cap.id, RUNS as u32, mean, least, most, failed);
    hq.cal_isr(
        ISR_ENTRIES.load(Ordering::Relaxed),
        ISR_SPURIOUS.load(Ordering::Relaxed),
        ISR_SR.load(Ordering::Relaxed),
        ISR_CNT.load(Ordering::Relaxed),
    );
}

pub fn delay_cycles(cycles: u32) {
    let start = DWT::cycle_count();
    while DWT::cycle_count().wrapping_sub(start) < cycles {}
}

/// Start a 50 percent PWM of period `arr + 1` timer ticks (psc 0) on the SCK pad: TIM1_CH1N.
pub fn pwm_start(psc: u32, arr: u32) {
    unsafe {
        let moder = GPIOB;
        wr(moder, (rd(moder) & !(3 << (2 * SCK_PIN))) | (2 << (2 * SCK_PIN))); // alternate function
        set_bits(GPIOB + 0x08, 3 << (2 * SCK_PIN)); // OSPEEDR: very high
        let afrh = GPIOB + 0x24;
        wr(afrh, (rd(afrh) & !(0xF << (4 * (SCK_PIN - 8)))) | (1 << (4 * (SCK_PIN - 8)))); // AF1
        wr(TIM1, 0);
        wr(TIM1 + 0x28, psc);
        wr(TIM1 + 0x2C, arr);
        wr(TIM1 + 0x34, (arr + 1) / 2); // CCR1
        wr(TIM1 + 0x18, 0x60); // CCMR1: OC1M = PWM mode 1
        wr(TIM1 + 0x20, (1 << 2) | 1); // CCER: CC1NE | CC1E
        wr(TIM1 + 0x44, 1 << 15); // BDTR.MOE
        wr(TIM1 + 0x14, 1); // EGR.UG
        wr(TIM1 + 0x10, 0);
        wr(TIM1, 1); // CR1.CEN
    }
}

pub fn pwm_stop() {
    unsafe {
        wr(TIM1, 0);
        wr(TIM1 + 0x20, 0);
        wr(TIM1 + 0x44, 0);
    }
}

/// Run the PWM ladder of every capture configuration and print one QUAL record per step.
pub fn qualify(hq: &mut crate::report::Hq) {
    for cap in frozen::CAPTURES.iter() {
        for (step, &(psc, arr)) in cap.pwm_steps.iter().enumerate() {
            pwm_start(psc, arr);
            // The PWM runs for a few periods before the capture window opens.
            delay_cycles(4_000);
            let (result, ()) = capture(cap, || {});
            pwm_stop();
            hq.qual(cap.id, step as u32, psc, arr, &result, buffer());
        }
    }
}
