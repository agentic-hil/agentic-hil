//! Frames synthesized on the undriven RX pin, and the register-level control.
//!
//! There is no wiring on this bench, so a frame reaches the receiver in one of
//! two in-chip ways. The USART's own half-duplex readback carries the
//! correct-parity values (see `readers.rs`). Everything else is made here:
//! DMA2 writes one word per bit of the frame into a register of GPIOA on every
//! TIM8 update, so the line follows the word list at 19200 baud. DMA2 is the
//! only controller whose peripheral port reaches the AHB1 GPIO ports, and
//! TIM8_UP is its stream 1, channel 7.
//!
//! The line is PA9, the single wire of half-duplex mode, because that is the
//! line the receiver demonstrably reads: PA10 stays high under its own
//! pull-down on this board (`pin_lo` of every `ctl` record), so nothing its
//! pull bits do reaches the receiver, while the half-duplex readback carries
//! every value of every pattern over PA9. The pin is an open-drain alternate
//! function there, so the transmitter never drives it high and its pull bits
//! own the line; `Drive` and `select_drive()` send a trial frame per way of
//! driving it and keep the first that arrives.
//!
//! Neither frame source has run on this board before, so both prove themselves:
//! `measure_frame()` reads one frame straight out of `USART_DR` through its
//! address, bypassing every Embassy reader, shows where RM0390 puts the parity
//! bit and stamps when the hardware raises PE relative to RXNE.
//!
//! Registers are addressed directly rather than through the PAC: the layout is
//! RM0390's and the same addresses the repository's C images use, so nothing
//! here depends on a generated field name.

use crate::console::Static;

// ---- registers (RM0390) ----------------------------------------------------

const RCC_BASE: u32 = 0x4002_3800;
pub const RCC_CFGR: u32 = RCC_BASE + 0x08;
const RCC_PLLCFGR: u32 = RCC_BASE + 0x04;
const RCC_AHB1ENR: u32 = RCC_BASE + 0x30;
const RCC_APB2ENR: u32 = RCC_BASE + 0x44;
pub const RCC_CSR: u32 = RCC_BASE + 0x74;
pub const RCC_CSR_RMVF: u32 = 1 << 24;

const GPIOA_BASE: u32 = 0x4002_0000;
const GPIOA_MODER: u32 = GPIOA_BASE + 0x00;
const GPIOA_OTYPER: u32 = GPIOA_BASE + 0x04;
const GPIOA_PUPDR: u32 = GPIOA_BASE + 0x0C;
const GPIOA_IDR: u32 = GPIOA_BASE + 0x10;
const GPIOA_ODR: u32 = GPIOA_BASE + 0x14;
const GPIOA_BSRR: u32 = GPIOA_BASE + 0x18;
const GPIOA_AFRH: u32 = GPIOA_BASE + 0x24;
/// PA9 is USART1_TX and, in single-wire half-duplex mode, the one line the
/// receiver listens to. Its two pull bits sit at 19:18 of PUPDR, its four
/// alternate-function bits at 7:4 of AFRH (pin 9 is the first of the high half),
/// and its input level is bit 9 of IDR.
const PA9_SHIFT: u32 = 18;
const PA9_AF_SHIFT: u32 = 4;
const PA9_PIN: u32 = 9;
const PA9_BIT: u32 = 1 << 9;

pub const USART1_BASE: u32 = 0x4001_1000;
pub const USART1_SR: u32 = USART1_BASE + 0x00;
pub const USART1_DR: u32 = USART1_BASE + 0x04;
pub const USART1_BRR: u32 = USART1_BASE + 0x08;
pub const USART1_CR1: u32 = USART1_BASE + 0x0C;

pub const SR_PE: u32 = 1 << 0;
pub const SR_FE: u32 = 1 << 1;
pub const SR_NF: u32 = 1 << 2;
pub const SR_ORE: u32 = 1 << 3;
pub const SR_RXNE: u32 = 1 << 5;
/// Anything that lets a blocking read return, so a poll before it can time out.
pub const SR_READABLE: u32 = SR_PE | SR_FE | SR_NF | SR_ORE | SR_RXNE;

const CR1_RE: u32 = 1 << 2;
const CR1_PS: u32 = 1 << 9;
const CR1_PCE: u32 = 1 << 10;
const CR1_M: u32 = 1 << 12;
const CR1_UE: u32 = 1 << 13;

const TIM8_BASE: u32 = 0x4001_0400;
const TIM8_CR1: u32 = TIM8_BASE + 0x00;
const TIM8_DIER: u32 = TIM8_BASE + 0x0C;
const TIM8_SR: u32 = TIM8_BASE + 0x10;
const TIM8_EGR: u32 = TIM8_BASE + 0x14;
const TIM8_CNT: u32 = TIM8_BASE + 0x24;
const TIM8_PSC: u32 = TIM8_BASE + 0x28;
const TIM8_ARR: u32 = TIM8_BASE + 0x2C;
const TIM_CR1_CEN: u32 = 1 << 0;
const TIM_CR1_URS: u32 = 1 << 2;
const TIM_EGR_UG: u32 = 1 << 0;
const TIM_DIER_UDE: u32 = 1 << 8;

const DMA2_BASE: u32 = 0x4002_6400;
const DMA2_LISR: u32 = DMA2_BASE + 0x00;
const DMA2_LIFCR: u32 = DMA2_BASE + 0x08;
/// Stream 1: the register block at 0x10 + 0x18 * 1.
const DMA2_S1: u32 = DMA2_BASE + 0x10 + 0x18;
const DMA2_S1_CR: u32 = DMA2_S1 + 0x00;
const DMA2_S1_NDTR: u32 = DMA2_S1 + 0x04;
const DMA2_S1_PAR: u32 = DMA2_S1 + 0x08;
const DMA2_S1_M0AR: u32 = DMA2_S1 + 0x0C;
const DMA2_S1_FCR: u32 = DMA2_S1 + 0x14;
/// Stream 1's five interrupt flags in LISR and LIFCR start at bit 6.
const TCIF1: u32 = 1 << 11;
const TEIF1: u32 = 1 << 9;
const DMEIF1: u32 = 1 << 8;
const FLAGS1: u32 = (1 << 6) | DMEIF1 | TEIF1 | (1 << 10) | TCIF1;

const DEMCR: u32 = 0xE000_EDFC;
const DWT_CTRL: u32 = 0xE000_1000;
const DWT_CYCCNT: u32 = 0xE000_1004;

/// 16 MHz / 19200 = 833.33 core cycles per bit; 833 is +0.04 %, which is 0.5 %
/// of a bit over the longest frame and far inside the receiver's tolerance.
pub const BIT_CYCLES: u32 = 833;
const CYCLES_PER_MS: u32 = 16_000;

#[inline]
pub fn rd(address: u32) -> u32 {
    // SAFETY: every address above is a memory-mapped register of this part.
    unsafe { core::ptr::read_volatile(address as *const u32) }
}

#[inline]
pub fn wr(address: u32, value: u32) {
    // SAFETY: as above.
    unsafe { core::ptr::write_volatile(address as *mut u32, value) }
}

// ---- time measured in the MCU ----------------------------------------------

pub fn enable_cycle_counter() {
    wr(DEMCR, rd(DEMCR) | (1 << 24));
    wr(DWT_CYCCNT, 0);
    wr(DWT_CTRL, rd(DWT_CTRL) | 1);
}

#[inline]
pub fn cyccnt() -> u32 {
    rd(DWT_CYCCNT)
}

pub fn wait_cycles(count: u32) {
    let start = cyccnt();
    while cyccnt().wrapping_sub(start) < count {}
}

// ---- the clock configuration the image measures with -----------------------

const HSI_HZ: u32 = 16_000_000;
/// The ST-LINK/V2-1 feeds 8 MHz into HSE in bypass mode on this board.
const HSE_HZ: u32 = 8_000_000;

pub fn sysclk_hz() -> u32 {
    let cfgr = rd(RCC_CFGR);
    match (cfgr >> 2) & 3 {
        0 => HSI_HZ,
        1 => HSE_HZ,
        2 => {
            let pll = rd(RCC_PLLCFGR);
            let m = pll & 0x3F;
            let n = (pll >> 6) & 0x1FF;
            let p = 2 * (((pll >> 16) & 3) + 1);
            let source = if pll & (1 << 22) != 0 { HSE_HZ } else { HSI_HZ };
            if m == 0 {
                0
            } else {
                ((source as u64 * n as u64) / (m as u64 * p as u64)) as u32
            }
        }
        _ => 0,
    }
}

fn ahb_divider(cfgr: u32) -> u32 {
    // HPRE: 0xxx is /1, then /2 /4 /8 /16 /64 /128 /256 /512 (no /32).
    const DIVIDERS: [u32; 8] = [2, 4, 8, 16, 64, 128, 256, 512];
    let bits = (cfgr >> 4) & 0xF;
    if bits < 8 { 1 } else { DIVIDERS[(bits - 8) as usize] }
}

fn apb_divider(bits: u32) -> u32 {
    // PPRE: 0xx is /1, then /2 /4 /8 /16.
    if bits < 4 { 1 } else { 1 << (bits - 3) }
}

/// USART2's kernel clock.
pub fn pclk1_hz() -> u32 {
    let cfgr = rd(RCC_CFGR);
    sysclk_hz() / ahb_divider(cfgr) / apb_divider((cfgr >> 10) & 7)
}

/// USART1's kernel clock.
pub fn pclk2_hz() -> u32 {
    let cfgr = rd(RCC_CFGR);
    sysclk_hz() / ahb_divider(cfgr) / apb_divider((cfgr >> 13) & 7)
}

/// TIM8's counter clock: PCLK2 when the APB2 prescaler is 1, twice it otherwise
/// (DCKCFGR.TIMPRE is left at its reset value).
pub fn tim8_hz() -> u32 {
    let cfgr = rd(RCC_CFGR);
    let pclk = pclk2_hz();
    if apb_divider((cfgr >> 13) & 7) == 1 { pclk } else { pclk * 2 }
}

// ---- the configurations under test -----------------------------------------

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Par {
    None,
    Even,
    Odd,
}

impl Par {
    pub fn name(self) -> &'static str {
        match self {
            Par::None => "none",
            Par::Even => "even",
            Par::Odd => "odd",
        }
    }
}

/// One line configuration, as `configure()` maps it on this part: 7 data bits
/// with parity is the 8-bit word (parity in bit 7), 8 data bits with parity the
/// 9-bit word (parity in bit 8).
#[derive(Copy, Clone)]
pub struct Shape {
    pub name: &'static str,
    pub data_bits: u8,
    pub parity: Par,
}

pub const SHAPES: [Shape; 4] = [
    Shape { name: "7e1", data_bits: 7, parity: Par::Even },
    Shape { name: "7o1", data_bits: 7, parity: Par::Odd },
    Shape { name: "8e1", data_bits: 8, parity: Par::Even },
    Shape { name: "8n1", data_bits: 8, parity: Par::None },
];

impl Shape {
    /// The parity bit a transmitter computes over the data bits.
    pub fn parity_bit(&self, value: u8) -> u8 {
        let data = value & (((1u16 << self.data_bits) - 1) as u8);
        let ones = (data.count_ones() & 1) as u8;
        match self.parity {
            Par::None => 0,
            Par::Even => ones,
            Par::Odd => 1 - ones,
        }
    }

    /// The word the hardware builds from `value`: the data bits with the parity
    /// bit above them, where RM0390 puts it (bit 7 of the 8-bit word, bit 8 of
    /// the 9-bit one).
    ///
    /// Only `select_drive()` uses this, to tell a frame that arrived from one
    /// that did not. Every value a record carries is printed as it was read.
    pub fn raw_word(&self, value: u8, invert: bool) -> u32 {
        let data = u32::from(value) & ((1u32 << self.data_bits) - 1);
        match self.parity {
            Par::None => u32::from(value),
            _ => {
                let parity = u32::from(self.parity_bit(value) ^ u8::from(invert));
                data | (parity << self.data_bits)
            }
        }
    }
}

// ---- the synthesizer -------------------------------------------------------

pub const MAX_FRAMES: usize = 258;
const MAX_WORDS: usize = 2 + MAX_FRAMES * 13;

static WORDS: Static<[u32; MAX_WORDS]> = Static::new([0; MAX_WORDS]);

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Synthesis {
    /// A readback sequence: nothing was synthesized.
    NotUsed,
    Complete,
    Failed,
}

impl Synthesis {
    pub fn name(self) -> &'static str {
        match self {
            Synthesis::NotUsed => "na",
            Synthesis::Complete => "ok",
            Synthesis::Failed => "err",
        }
    }
}

/// What the stimulus itself does, measured with the receiver switched off.
///
/// `finish()` can only see that the word list ran out and that the line ended
/// where it started, which reads the same whether or not anything ever moved
/// the pin. These are the facts that say what did, measured in the same image
/// before the frames the readers judge.
#[derive(Copy, Clone)]
pub struct PinProbe {
    /// `GPIOA_IDR` bit 9 with PA9 pulled down: a line the pull can move reads 0.
    pub low: u32,
    /// The same bit with PA9 pulled up: a working pull reads `0x200`.
    pub high: u32,
    /// PA9's two `MODER` bits; 2 is alternate function.
    pub moder: u32,
    /// PA9's four `AFRH` bits; 7 is USART1.
    pub af: u32,
    /// PA9's `OTYPER` bit; 1 is open drain, which is what lets the pull bits and
    /// not the transmitter decide when the line is at mark.
    pub otyper: u32,
    /// Core cycles DMA2 took for `PROBE_WORDS` words, so one word per bit time
    /// can be told from a stream that ran at the bus rate.
    pub rate: u32,
    /// Words that run; the gate needs no constant of its own to read `rate`.
    pub words: u32,
    /// Which register the word list writes, and so what drove these frames.
    pub drive: &'static str,
}

/// How the line is driven, that is which register the word list writes.
///
/// The pull bits are what this job asks for. They cannot work on a pin an
/// output driver holds, which is why the line is PA9 with the transmit pin
/// configured open drain: the transmitter pulls it low and releases it, and the
/// pull bits decide where a released line rests. `select_drive()` tries each of
/// these in turn and keeps the first whose frame reaches `USART_DR`; every `ctl`
/// record names it.
#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Drive {
    /// PA9 stays the open-drain alternate function and DMA2 writes its two pull
    /// bits in `GPIOA_PUPDR`, so the ~40 kOhm internal pull drives the line.
    /// This is the register this job names.
    Pull,
    /// PA9 becomes an open-drain general-purpose output while `AFRH` keeps
    /// USART1 selected on it, and DMA2 writes its two bits in `GPIOA_BSRR`: the
    /// output driver pulls the line low and releases it to the pull-up for mark.
    /// The input path of a pin is active in output mode too, so the receiver
    /// reads what the driver leaves on the line.
    Open,
}

impl Drive {
    pub fn name(self) -> &'static str {
        match self {
            Drive::Pull => "pull",
            Drive::Open => "open",
        }
    }
}

/// Both ways of driving the line, in the order `select_drive()` tries them: the
/// pull bits the job asks for first, then the pin's own output driver.
const DRIVES: [Drive; 2] = [Drive::Pull, Drive::Open];

/// Take the pin over as an open-drain output, leaving `AFRH` alone.
///
/// `BSRR` writes only the bits named in the word, so the other fifteen pins of
/// the port keep their output state whichever drive is in effect. Open drain
/// rather than push-pull, so a word never drives the line high: the pull-up
/// holds mark, exactly as it does while the alternate function owns the pin.
fn as_open_drain_output(pin: u32, bit: u32) {
    wr(GPIOA_BSRR, bit); // release first: the pin takes over a line already at rest
    wr(GPIOA_OTYPER, rd(GPIOA_OTYPER) | bit);
    let shift = pin * 2;
    wr(GPIOA_MODER, (rd(GPIOA_MODER) & !(3 << shift)) | (1 << shift));
}

/// Give the pin back to its alternate function, leaving `AFRH` alone.
fn as_alternate(pin: u32) {
    let shift = pin * 2;
    wr(GPIOA_MODER, (rd(GPIOA_MODER) & !(3 << shift)) | (2 << shift));
}

fn pull_up(pin: u32) {
    let shift = pin * 2;
    wr(GPIOA_PUPDR, (rd(GPIOA_PUPDR) & !(3 << shift)) | (1 << shift));
}

/// Enough updates to time the stimulus without producing a start bit (every
/// word is the idle level), and short enough to cost under a millisecond.
const PROBE_WORDS: u32 = 16;

pub struct Synth {
    drive: Drive,
    /// The register DMA2 writes: `GPIOA_PUPDR` or `GPIOA_BSRR`.
    reg: u32,
    /// The word that puts the line at mark, and the one that puts it at space.
    mark: u32,
    space: u32,
    /// The driven pin's bit in `ODR`, where an output driver is the stimulus.
    bit: u32,
    limit_cycles: u32,
    running: bool,
    /// Whether a known frame has reached `USART_DR` through the drive in
    /// effect, which `select_drive()` measures once per phase. Until one has,
    /// no sequence may call itself complete, because nothing would have driven
    /// the line.
    proven: bool,
}

impl Synth {
    pub const fn new() -> Self {
        Self {
            drive: Drive::Pull,
            reg: GPIOA_PUPDR,
            mark: 0,
            space: 0,
            bit: 0,
            limit_cycles: 0,
            running: false,
            proven: false,
        }
    }

    pub fn drive(&self) -> Drive {
        self.drive
    }

    /// Configure the pin for the drive in effect and compute its two words.
    ///
    /// Every Embassy constructor configures the pin again, so this runs at the
    /// start of every sequence rather than once.
    fn apply(&mut self) {
        match self.drive {
            Drive::Pull => {
                // Whatever a previous sequence did with the pin, the alternate
                // function owns it here, so the transmitter's open-drain output
                // and the pull bits share the line.
                as_alternate(PA9_PIN);
                // The snapshot keeps every other pin's pull setting, since the
                // whole register is written on every bit.
                let kept = rd(GPIOA_PUPDR) & !(3 << PA9_SHIFT);
                self.reg = GPIOA_PUPDR;
                self.mark = kept | (1 << PA9_SHIFT);
                self.space = kept | (2 << PA9_SHIFT);
                self.bit = 0;
            }
            Drive::Open => {
                pull_up(PA9_PIN); // the released line rests at mark on its own
                as_open_drain_output(PA9_PIN, PA9_BIT);
                self.reg = GPIOA_BSRR;
                self.mark = PA9_BIT;
                self.space = PA9_BIT << 16;
                self.bit = PA9_BIT;
            }
        }
    }

    /// Whether the line is where the last word of a sequence left it.
    fn at_mark(&self) -> bool {
        match self.drive {
            Drive::Pull => rd(GPIOA_PUPDR) == self.mark,
            Drive::Open => rd(GPIOA_ODR) & self.bit != 0,
        }
    }

    /// Measure the stimulus itself. The caller must have switched the receiver
    /// off (`CR1.UE` clear), because this drives levels the receiver would
    /// otherwise read as start bits and frames of its own.
    pub fn probe(&mut self) -> PinProbe {
        // The pin as the constructor left it, before any drive is applied: its
        // mode, its alternate function and whether it is open drain, which is
        // what lets a pull bit and not the transmitter hold the line at mark.
        let moder = (rd(GPIOA_MODER) >> (PA9_PIN * 2)) & 3;
        let af = (rd(GPIOA_AFRH) >> PA9_AF_SHIFT) & 0xF;
        let otyper = (rd(GPIOA_OTYPER) >> PA9_PIN) & 1;

        let kept = rd(GPIOA_PUPDR) & !(3 << PA9_SHIFT);
        let up = kept | (1 << PA9_SHIFT);
        let down = kept | (2 << PA9_SHIFT);

        // One bit time is about eighty times the 40 kOhm pull's time constant
        // against the line's own capacitance, so a line the pull can move has
        // settled long before a receiver would sample it. Measured with the pin
        // still the alternate function, which is the state a frame is sent in.
        wr(GPIOA_PUPDR, down);
        wait_cycles(BIT_CYCLES);
        let low = rd(GPIOA_IDR) & PA9_BIT;
        wr(GPIOA_PUPDR, up);
        wait_cycles(BIT_CYCLES);
        let high = rd(GPIOA_IDR) & PA9_BIT;

        // The same DMA2 stream and timer the frames use, on a word list that
        // holds the line at mark: what is timed is the stimulus's clock, not
        // anything the USART does with it.
        self.apply();
        let words = WORDS.get();
        for slot in words[..PROBE_WORDS as usize].iter_mut() {
            *slot = self.mark;
        }
        core::sync::atomic::compiler_fence(core::sync::atomic::Ordering::SeqCst);
        self.limit_cycles = PROBE_WORDS * 4 * BIT_CYCLES + CYCLES_PER_MS;
        self.arm(words.as_ptr() as usize as u32, PROBE_WORDS);
        let begin = cyccnt();
        self.start();
        let mut rate = 0;
        loop {
            if rd(DMA2_LISR) & TCIF1 != 0 {
                rate = cyccnt().wrapping_sub(begin);
                break;
            }
            if cyccnt().wrapping_sub(begin) > self.limit_cycles {
                break;
            }
        }
        let _ = self.finish_words();
        PinProbe { low, high, moder, af, otyper, rate, words: PROBE_WORDS, drive: self.drive.name() }
    }

    /// DMA2 and TIM8; every other clock belongs to an Embassy driver.
    pub fn clocks_on() {
        wr(RCC_AHB1ENR, rd(RCC_AHB1ENR) | (1 << 22));
        wr(RCC_APB2ENR, rd(RCC_APB2ENR) | (1 << 1));
        let _ = rd(RCC_APB2ENR);
    }

    /// Build the word list of `values` and arm DMA2 and TIM8 without starting them.
    ///
    /// `bad` names the one frame whose parity bit is inverted. `apply()` runs
    /// first, so the words are the ones the drive in effect needs and, with the
    /// pull bits, carry every other pin's own setting through the sequence.
    pub fn prepare(&mut self, shape: &Shape, values: &[u8], bad: Option<usize>) {
        // The constructor of this sequence's reader has just configured both
        // pins, so the drive is applied again here, and whatever the line did
        // while it did so is not part of this sequence: SR and then DR clears
        // every receive flag on usart_v2.
        self.apply();
        let _ = rd(USART1_SR);
        let _ = rd(USART1_DR);
        let up = self.mark;
        let down = self.space;
        let words = WORDS.get();
        let mut at = 0usize;
        words[at] = up; // one idle word, so the line is at mark before the first start bit
        at += 1;
        for (index, value) in values.iter().enumerate() {
            let value = *value;
            words[at] = down; // start bit
            at += 1;
            for bit in 0..shape.data_bits {
                // Least significant data bit first, as the USART shifts it out.
                words[at] = if (value >> bit) & 1 != 0 { up } else { down };
                at += 1;
            }
            if shape.parity != Par::None {
                let mut parity = shape.parity_bit(value);
                if bad == Some(index) {
                    parity ^= 1;
                }
                words[at] = if parity != 0 { up } else { down };
                at += 1;
            }
            words[at] = up; // stop bit
            at += 1;
            words[at] = up; // two idle bits: more than a reader needs to take the
            at += 1; // word or to re-arm after an error, less than a frame, so
            words[at] = up; // no overrun can hide a parity error and IDLE does not
            at += 1; // fall between two frames
        }
        words[at] = up;
        at += 1;
        // The word list is ordinary memory; only DMA2 reads it. The fence keeps
        // the stores above from being moved past the register writes that hand
        // the list to the controller.
        core::sync::atomic::compiler_fence(core::sync::atomic::Ordering::SeqCst);
        self.limit_cycles = at as u32 * 2 * BIT_CYCLES + 20 * CYCLES_PER_MS;
        self.arm(words.as_ptr() as usize as u32, at as u32);
    }

    fn arm(&mut self, source: u32, count: u32) {
        wr(DMA2_S1_CR, 0);
        while rd(DMA2_S1_CR) & 1 != 0 {}
        wr(DMA2_LIFCR, FLAGS1);
        wr(DMA2_S1_PAR, self.reg);
        wr(DMA2_S1_M0AR, source);
        wr(DMA2_S1_NDTR, count);
        wr(DMA2_S1_FCR, 0); // direct mode: one 32-bit word per request
        wr(
            DMA2_S1_CR,
            (7 << 25)      // channel 7 of stream 1: TIM8_UP
                | (2 << 16) // high priority
                | (2 << 13) // MSIZE 32 bit
                | (2 << 11) // PSIZE 32 bit
                | (1 << 10) // MINC: walk the word list
                | (1 << 6), // memory to peripheral
        );
        wr(DMA2_S1_CR, rd(DMA2_S1_CR) | 1);
        wr(TIM8_CR1, TIM_CR1_URS);
        wr(TIM8_DIER, 0);
        wr(TIM8_PSC, 0);
        wr(TIM8_ARR, BIT_CYCLES - 1);
        wr(TIM8_CNT, 0);
        wr(TIM8_EGR, TIM_EGR_UG); // load PSC and ARR; URS keeps it from requesting
        wr(TIM8_SR, 0);
        wr(TIM8_DIER, TIM_DIER_UDE);
        self.running = false;
    }

    /// Let the first bit out; the sequence runs on its own from here.
    pub fn start(&mut self) {
        wr(TIM8_CR1, TIM_CR1_URS | TIM_CR1_CEN);
        self.running = true;
    }

    /// Wait for the word list to run out, stop everything and say whether the
    /// line was driven exactly as asked.
    ///
    /// The word list running out and the line ending where it started are
    /// necessary and not sufficient: they read the same whether or not anything
    /// ever moved the pin, which is what `select_drive()` measures with a frame
    /// of its own. A sequence is complete only when both hold.
    pub fn finish(&mut self) -> Synthesis {
        let list = self.finish_words();
        if self.proven { list } else { Synthesis::Failed }
    }

    fn finish_words(&mut self) -> Synthesis {
        let mut complete = false;
        if self.running {
            let begin = cyccnt();
            loop {
                if rd(DMA2_LISR) & TCIF1 != 0 {
                    complete = true;
                    break;
                }
                if cyccnt().wrapping_sub(begin) > self.limit_cycles {
                    break;
                }
            }
        }
        wr(TIM8_CR1, 0);
        wr(TIM8_DIER, 0);
        let flags = rd(DMA2_LISR);
        wr(DMA2_S1_CR, 0);
        while rd(DMA2_S1_CR) & 1 != 0 {}
        let left = rd(DMA2_S1_NDTR);
        wr(DMA2_LIFCR, FLAGS1);
        self.running = false;
        let clean = complete && flags & (TEIF1 | DMEIF1) == 0 && left == 0 && self.at_mark();
        if clean { Synthesis::Complete } else { Synthesis::Failed }
    }
}

// ---- the register-level control --------------------------------------------

/// What one frame looked like in `USART_SR` and `USART_DR` themselves.
pub struct Control {
    pub sr: u32,
    pub dr: u32,
    /// Core cycles from the first read of SR that showed PE to the first that
    /// showed RXNE, or -1 when PE never appeared.
    pub pe_lead: i32,
    /// The longest single iteration of that poll, so the gate can tell a PE
    /// raised with RXNE from one raised a part of a bit earlier.
    pub poll: u32,
    pub timeout: u32,
}

/// Write `CR1` from RM0390 rather than from `configure()`, so the control does
/// not inherit an assumption from the code it measures. `BRR`, `CR2` and `CR3`
/// stay as the constructor left them, so `HDSEL` keeps the receiver on the one
/// line the stimulus drives, exactly as it is in every sequence.
///
/// The stimulus is measured here, between clearing `UE` and setting it again:
/// with the receiver off the probe's levels cannot be mistaken for a frame.
pub fn configure_by_hand(shape: &Shape, synth: &mut Synth) -> PinProbe {
    wr(USART1_CR1, rd(USART1_CR1) & !CR1_UE);
    let probe = synth.probe();
    let mut cr1 = CR1_RE; // receive only: the frame comes from the drive below
    if shape.parity != Par::None {
        cr1 |= CR1_PCE;
        if shape.data_bits == 8 {
            cr1 |= CR1_M; // 8 data bits and parity is the 9-bit word
        }
        if shape.parity == Par::Odd {
            cr1 |= CR1_PS;
        }
    }
    wr(USART1_CR1, cr1);
    wr(USART1_CR1, cr1 | CR1_UE);
    let _ = rd(USART1_SR);
    let _ = rd(USART1_DR);
    select_drive(synth, shape);
    PinProbe { drive: synth.drive().name(), ..probe }
}

/// Keep the first way of driving the line whose frame reaches `USART_DR`.
///
/// Neither in-chip frame source has run on this board before, so each drive of
/// `DRIVES` sends one frame, read through the same register path the controls
/// use, and the first that arrives as RM0390 lays it out is kept for the whole
/// phase. `pin_lo` and `pin_hi` of every `ctl` record say whether the pull bits
/// move the line at all. These trial frames are not printed: they choose the
/// stimulus, and the nine `ctl` records that follow are the measurement.
///
/// If none of them arrives, the last stays in effect, every synthesized
/// sequence reports `synth=err` and the gate fails with the control's own
/// message, which says the measurement path and not Embassy is wrong.
pub fn select_drive(synth: &mut Synth, shape: &Shape) {
    // Alternating bits, so a line stuck at either level cannot pass as this.
    const TRIAL: u8 = 0x55;
    if synth.proven {
        return;
    }
    // Two frames per drive, because the first one a drive sends is also the
    // first time the pin is in that state: a drive is only rejected when it
    // carried nothing twice. The test is unchanged by that, the frame must
    // arrive exactly as RM0390 lays it out.
    for drive in DRIVES {
        synth.drive = drive;
        for _ in 0..2 {
            let control = measure_frame(synth, shape, TRIAL, false);
            if control.timeout == 0
                && control.dr == shape.raw_word(TRIAL, false)
                && control.sr & (SR_PE | SR_FE | SR_NF | SR_ORE) == 0
            {
                synth.proven = true;
                return;
            }
        }
    }
}

/// One synthesized frame, read straight out of `USART_DR`.
pub fn measure_frame(synth: &mut Synth, shape: &Shape, value: u8, bad: bool) -> Control {
    let _ = rd(USART1_SR);
    let _ = rd(USART1_DR); // SR then DR clears every flag on usart_v2
    synth.prepare(shape, &[value], if bad { Some(0) } else { None });
    // No interrupt may stretch one iteration of the poll, or the lead could not
    // be told from the poll's own resolution. The frame takes at most 13 bit
    // times and the poll gives up after 50 ms, far less than the second between
    // two interrupts of the time driver.
    let primask = cortex_m::register::primask::read();
    cortex_m::interrupt::disable();
    synth.start();
    let (pe_lead, poll, timeout) = poll_until_rxne();
    if primask.is_active() {
        // SAFETY: interrupts were enabled when this section began, so enabling
        // them again restores the state the caller had.
        unsafe { cortex_m::interrupt::enable() };
    }
    // SR before DR: reading DR would clear PE before it was recorded.
    let sr = rd(USART1_SR);
    let dr = rd(USART1_DR) & 0x1FF;
    // The word list only: this frame is its own control, so it does not depend
    // on a drive having proved itself, which is what it is measuring.
    let _ = synth.finish_words();
    Control { sr, dr, pe_lead, poll, timeout }
}

fn poll_until_rxne() -> (i32, u32, u32) {
    // The longest frame takes 13 bit times, about 11 000 cycles; 800 000 is
    // 50 ms, so a frame that never comes is a timeout and not a hang.
    const LIMIT: u32 = 800_000;
    let start = cyccnt();
    let mut previous = start;
    let mut worst = 0u32;
    let mut parity_at = 0u32;
    let mut seen_parity = false;
    loop {
        let sr = rd(USART1_SR);
        let now = cyccnt();
        let step = now.wrapping_sub(previous);
        if step > worst {
            worst = step;
        }
        previous = now;
        if !seen_parity && sr & SR_PE != 0 {
            parity_at = now;
            seen_parity = true;
        }
        if sr & SR_RXNE != 0 {
            let lead = if seen_parity { now.wrapping_sub(parity_at) as i32 } else { -1 };
            return (lead, worst, 0);
        }
        if now.wrapping_sub(start) > LIMIT {
            return (if seen_parity { 0 } else { -1 }, worst, 1);
        }
    }
}
