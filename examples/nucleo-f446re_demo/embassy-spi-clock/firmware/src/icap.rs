//! Capture path (HQ #340): TIM8_CH2 input capture on PC7 (AF3) for edges arriving over the
//! fitted jumper CN10-25 (PB10) to CN10-19 (PC7), qualified against a TIM2_CH3 PWM of exactly
//! known frequency on PB10 (AF1).
//!
//! TIM8 runs free at its kernel clock (PSC 0, ARR 0xFFFF). Every rising edge on TI2 latches
//! the counter into CCR2, and the CC2 DMA request (RM0390 Rev 9 Table 29: DMA2 stream 3,
//! channel 7) copies `CAPTURE_PATH_CAPTURES` timestamps into SRAM. The pad sampler uses TIM8
//! too, so the capture path runs in its own phase after `sampler::qualify` and leaves TIM8,
//! the DMA stream and both pins as it found them: the sampler reprograms TIM8 per capture.
//!
//! PC7 is only ever an input: the jumper joins it to PB10, and PC7 also carries SPI2_SCK on
//! AF5. PB10 drives the PWM only while a configuration is measured and returns to input after.
//!
//! The boundary matrix (HQ #342) uses the same input for SPI2_SCK, which the driver puts on
//! PB10 (AF5): `capture_frame` arms TIM8_CH2 and DMA2 stream 3 for the rising edges of one
//! frame around the driver's own transfer. It touches only TIM8, the DMA stream and PC7, never
//! PB10 or TIM2, which belong to the SPI driver and the qualification reference.

use core::ptr::{addr_of_mut, read_volatile, write_volatile};

use cortex_m::peripheral::DWT;

use crate::frozen::{self, CapturePathConfig, CAPTURE_PATH_CAPTURES};

const RCC: usize = 0x4002_3800;
const GPIOB: usize = 0x4002_0400;
const GPIOC: usize = 0x4002_0800;
const DMA2: usize = 0x4002_6400;
const DMA2_STREAM3: usize = DMA2 + 0x10 + 3 * 0x18;
const TIM2: usize = 0x4000_0000;
const TIM8: usize = 0x4001_0400;

const REF_PIN: u32 = 10; // PB10: TIM2_CH3 on AF1
const CAP_PIN: u32 = 7; // PC7: TIM8_CH2 on AF3
/// LISR: TEIF3 | DMEIF3 | FEIF3. A FIFO error can be flagged in direct mode too (RM0390 9.3.19),
/// so it is reported with the others before release() clears it.
const STREAM3_ERRORS: u32 = (1 << 25) | (1 << 24) | (1 << 22);
const STREAM3_CLEAR: u32 = 0x0F40_0000; // LIFCR: CFEIF3 CDMEIF3 CTEIF3 CHTIF3 CTCIF3
/// Upper bound for one capture window: 0.1 s at the 64 MHz core clock, far above the 64
/// periods of the slowest reference (64 us each).
const WINDOW_LIMIT_CYCLES: u32 = 6_400_000;
/// Rising SCK edges of one matrix frame: SPI mode 0 idles low, so eight per byte.
const FRAME_EDGES: usize = 8 * frozen::TRANSFER_LEN;

static mut STAMPS: [u16; CAPTURE_PATH_CAPTURES] = [0; CAPTURE_PATH_CAPTURES];
static mut FRAME: [u16; FRAME_EDGES] = [0; FRAME_EDGES];

pub struct CaptureResult {
    pub captures: u32,
    /// DMA transfers still outstanding when the window closed (0 when every edge arrived).
    pub ndtr: u32,
    /// DMA2 stream 3 error flags, LISR shifted down by 22: bit 0 FEIF3 (FIFO error), bit 2
    /// DMEIF3 (direct-mode error), bit 3 TEIF3 (transfer error). 0 when none was raised.
    pub dma_err: u32,
    /// TIM8 SR.CC2OF: an edge was captured before the previous capture had been read.
    pub overcapture: u32,
    /// 1 if the window ran into WINDOW_LIMIT_CYCLES before every timestamp arrived.
    pub timed_out: u32,
    /// Smallest and largest successive difference (mod 2^16), diagnostics only.
    pub min_ticks: u32,
    pub max_ticks: u32,
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

unsafe fn pin_mode(port: usize, pin: u32, mode: u32) {
    wr(port, (rd(port) & !(3 << (2 * pin))) | (mode << (2 * pin)));
}

unsafe fn pin_af(port: usize, pin: u32, af: u32) {
    let afr = port + if pin < 8 { 0x20 } else { 0x24 };
    let shift = 4 * (pin % 8);
    wr(afr, (rd(afr) & !(0xF << shift)) | (af << shift));
}

fn stamps_bytes() -> &'static [u8] {
    unsafe {
        core::slice::from_raw_parts(addr_of_mut!(STAMPS) as *const u8, 2 * CAPTURE_PATH_CAPTURES)
    }
}

/// The matrix frame's timestamps, little-endian u16 each.
pub fn frame_stamps() -> &'static [u8] {
    unsafe { core::slice::from_raw_parts(addr_of_mut!(FRAME) as *const u8, 2 * FRAME_EDGES) }
}

/// Stop TIM8 and the capture DMA; put PC7 back to a plain input.
unsafe fn release_capture() {
    wr(TIM8, 0);
    wr(TIM8 + 0x0C, 0); // DIER
    wr(TIM8 + 0x20, 0); // CCER
    wr(TIM8 + 0x18, 0); // CCMR1
    wr(TIM8 + 0x10, 0); // SR
    wr(DMA2_STREAM3, 0);
    while rd(DMA2_STREAM3) & 1 != 0 {}
    wr(DMA2 + 0x08, STREAM3_CLEAR);
    pin_mode(GPIOC, CAP_PIN, 0);
    pin_af(GPIOC, CAP_PIN, 0);
}

/// Stop the reference PWM; put PB10 back to a plain input.
unsafe fn release_reference() {
    wr(TIM2, 0);
    wr(TIM2 + 0x20, 0); // CCER
    wr(TIM2 + 0x1C, 0); // CCMR2
    pin_mode(GPIOB, REF_PIN, 0);
    pin_af(GPIOB, REF_PIN, 0);
}

/// Stop TIM8, the capture DMA and the reference PWM; put PB10 and PC7 back to plain inputs.
unsafe fn release() {
    release_capture();
    release_reference();
}

/// Smallest and largest successive difference (mod 2^16) of a timestamp list.
fn tick_range(stamps: &[u16]) -> (u32, u32) {
    let (mut least, mut most) = (u32::MAX, 0u32);
    for pair in stamps.windows(2) {
        let ticks = pair[1].wrapping_sub(pair[0]) as u32;
        least = least.min(ticks);
        most = most.max(ticks);
    }
    if least == u32::MAX {
        least = 0;
    }
    (least, most)
}

/// Capture the rising SCK edges of one matrix frame (HQ #342): TIM8 runs free, every rising
/// edge on PC7 latches CCR2 and DMA2 stream 3 copies FRAME_EDGES timestamps, while `during`
/// runs the driver's own transfer. Returns the capture flags and the transfer's result; the
/// timestamps are in `frame_stamps`. PB10 stays with the SPI driver throughout.
pub fn capture_frame<R>(_config: &CapturePathConfig, during: impl FnOnce() -> R) -> (CaptureResult, R) {
    unsafe {
        release_capture();
        (*addr_of_mut!(FRAME)).fill(0);

        // Capture input: PC7 to AF3, input only.
        pin_af(GPIOC, CAP_PIN, 3);
        pin_mode(GPIOC, CAP_PIN, 2);

        // TIM8 free-running; CC2 on TI2, rising edge, the qualified filter (N=8).
        wr(TIM8 + 0x28, 0);
        wr(TIM8 + 0x2C, 0xFFFF);
        wr(TIM8 + 0x18, (3 << 12) | (1 << 8)); // CCMR1: IC2F = 0011, CC2S = 01
        wr(TIM8 + 0x14, 1); // EGR.UG
        wr(TIM8 + 0x10, 0);

        // DMA2 stream 3, channel 7: TIM8_CCR2 to SRAM, half-words, memory increment.
        wr(DMA2 + 0x08, STREAM3_CLEAR);
        wr(DMA2_STREAM3 + 0x08, (TIM8 + 0x38) as u32);
        wr(DMA2_STREAM3 + 0x0C, addr_of_mut!(FRAME) as usize as u32);
        wr(DMA2_STREAM3 + 0x04, FRAME_EDGES as u32);
        wr(DMA2_STREAM3 + 0x14, 0); // FCR: direct mode
        let control = (7 << 25) | (3 << 16) | (1 << 13) | (1 << 11) | (1 << 10);
        wr(DMA2_STREAM3, control);
        wr(DMA2_STREAM3, control | 1);

        // Arm the capture before the transfer starts: SCK idles low between frames.
        wr(TIM8, 1); // CR1.CEN
        wr(TIM8 + 0x10, 0); // SR: no stale CC2IF or CC2OF
        wr(TIM8 + 0x0C, 1 << 10); // DIER.CC2DE
        wr(TIM8 + 0x20, 1 << 4); // CCER.CC2E, rising edge

        let outcome = during();

        // The driver may return before the last edge was latched: wait for the DMA.
        let start = DWT::cycle_count();
        let mut timed_out = 0;
        while rd(DMA2_STREAM3 + 0x04) != 0 {
            if DWT::cycle_count().wrapping_sub(start) > WINDOW_LIMIT_CYCLES {
                timed_out = 1;
                break;
            }
        }
        wr(TIM8 + 0x20, 0);
        let status = rd(TIM8 + 0x10);
        let ndtr = rd(DMA2_STREAM3 + 0x04);
        let flags = rd(DMA2);

        release_capture();

        let (least, most) = tick_range(&*addr_of_mut!(FRAME));
        (
            CaptureResult {
                captures: FRAME_EDGES as u32,
                ndtr,
                dma_err: (flags & STREAM3_ERRORS) >> 22,
                overcapture: (status >> 10) & 1,
                timed_out,
                min_ticks: least,
                max_ticks: most,
            },
            outcome,
        )
    }
}

/// Measure one reference configuration: TIM2_CH3 PWM mode 1 at 50 percent duty with the
/// frozen PSC/ARR, captured by TIM8_CH2 on every rising edge.
fn measure(config: &CapturePathConfig) -> CaptureResult {
    unsafe {
        release();

        // Reference: TIM2_CH3 PWM, then PB10 to AF1 (output).
        wr(TIM2 + 0x28, config.pwm_psc);
        wr(TIM2 + 0x2C, config.pwm_arr);
        wr(TIM2 + 0x3C, (config.pwm_arr + 1) / 2); // CCR3
        wr(TIM2 + 0x1C, (6 << 4) | (1 << 3)); // CCMR2: OC3M = PWM mode 1, OC3PE
        wr(TIM2 + 0x20, 1 << 8); // CCER: CC3E
        wr(TIM2 + 0x14, 1); // EGR.UG
        wr(TIM2 + 0x10, 0);
        wr(TIM2, 1); // CR1.CEN
        pin_af(GPIOB, REF_PIN, 1);
        set_bits(GPIOB + 0x08, 3 << (2 * REF_PIN)); // OSPEEDR: very high
        pin_mode(GPIOB, REF_PIN, 2);

        // Capture input: PC7 to AF3, input only.
        pin_af(GPIOC, CAP_PIN, 3);
        pin_mode(GPIOC, CAP_PIN, 2);

        // TIM8 free-running; CC2 on TI2, rising edge, no prescaler, filter fCK_INT N=8
        // (8 timer clocks, 125 ns, below the 250 ns half period of the fastest reference).
        wr(TIM8 + 0x28, 0);
        wr(TIM8 + 0x2C, 0xFFFF);
        wr(TIM8 + 0x18, (3 << 12) | (1 << 8)); // CCMR1: IC2F = 0011, CC2S = 01
        wr(TIM8 + 0x14, 1); // EGR.UG
        wr(TIM8 + 0x10, 0);

        // DMA2 stream 3, channel 7: TIM8_CCR2 to SRAM, half-words, memory increment.
        wr(DMA2 + 0x08, STREAM3_CLEAR);
        wr(DMA2_STREAM3 + 0x08, (TIM8 + 0x38) as u32);
        wr(DMA2_STREAM3 + 0x0C, addr_of_mut!(STAMPS) as usize as u32);
        wr(DMA2_STREAM3 + 0x04, CAPTURE_PATH_CAPTURES as u32);
        wr(DMA2_STREAM3 + 0x14, 0); // FCR: direct mode
        let control = (7 << 25) | (3 << 16) | (1 << 13) | (1 << 11) | (1 << 10);
        wr(DMA2_STREAM3, control);
        wr(DMA2_STREAM3, control | 1);

        // Let the reference run a few periods, then arm the capture last, so the first DMA
        // transfer is the first edge after the counter started.
        crate::sampler::delay_cycles(40_000);
        wr(TIM8, 1); // CR1.CEN
        wr(TIM8 + 0x10, 0); // SR: no stale CC2IF or CC2OF
        wr(TIM8 + 0x0C, 1 << 10); // DIER.CC2DE
        wr(TIM8 + 0x20, 1 << 4); // CCER.CC2E, rising edge
        let start = DWT::cycle_count();

        let mut timed_out = 0;
        while rd(DMA2_STREAM3 + 0x04) != 0 {
            if DWT::cycle_count().wrapping_sub(start) > WINDOW_LIMIT_CYCLES {
                timed_out = 1;
                break;
            }
        }
        // Stop capturing at once: a further edge after the last transfer would otherwise set
        // CC2OF although nothing was lost inside the window.
        wr(TIM8 + 0x20, 0);
        let status = rd(TIM8 + 0x10);
        let ndtr = rd(DMA2_STREAM3 + 0x04);
        let flags = rd(DMA2);

        release();

        let stamps = &*addr_of_mut!(STAMPS);
        let (mut least, mut most) = (u32::MAX, 0u32);
        for pair in stamps.windows(2) {
            let ticks = pair[1].wrapping_sub(pair[0]) as u32;
            least = least.min(ticks);
            most = most.max(ticks);
        }
        if least == u32::MAX {
            least = 0;
        }
        CaptureResult {
            captures: CAPTURE_PATH_CAPTURES as u32,
            ndtr,
            dma_err: (flags & STREAM3_ERRORS) >> 22,
            overcapture: (status >> 10) & 1,
            timed_out,
            min_ticks: least,
            max_ticks: most,
        }
    }
}

/// Enable the clocks the capture path needs and qualify it for every frozen configuration,
/// one ICQUAL record each.
pub fn qualify(hq: &mut crate::report::Hq) {
    unsafe {
        set_bits(RCC + 0x30, (1 << 22) | (1 << 2) | (1 << 1)); // AHB1ENR: DMA2EN, GPIOCEN, GPIOBEN
        set_bits(RCC + 0x40, 1 << 0); // APB1ENR: TIM2EN
        set_bits(RCC + 0x44, 1 << 1); // APB2ENR: TIM8EN
        let _ = rd(RCC + 0x40); // let the enables settle before the first access
    }
    for config in frozen::CAPTURE_PATH.iter() {
        unsafe {
            let stamps = &mut *addr_of_mut!(STAMPS);
            stamps.fill(0);
        }
        let result = measure(config);
        hq.icqual(config.id, config.pwm_psc, config.pwm_arr, &result, stamps_bytes());
    }
}
