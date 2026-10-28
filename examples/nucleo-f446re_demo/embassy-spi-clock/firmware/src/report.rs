//! UART line protocol of the campaign firmware.
//!
//! Every record is one ASCII line `HQ184 <KIND> seq=<n> key=value ... crc32=<hex>`. The CRC is
//! the zlib CRC-32 of the text before ` crc32=`. USART2 (the ST-LINK virtual COM port) is
//! initialised by Embassy once; all output goes through raw register writes so that the panic
//! handler can use the same path.

use core::cell::UnsafeCell;
use core::mem::MaybeUninit;
use core::ptr::{read_volatile, write_volatile};

use crate::frozen;
use crate::sampler::CaptureResult;

const USART2: usize = 0x4000_4400;
const LINE_CAPACITY: usize = 1024;
const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const MARK_MAGIC: u32 = 0x4851_3834;

/// Single-core, interrupt-free firmware: plain interior mutability is enough.
struct Global<T>(UnsafeCell<T>);
unsafe impl<T> Sync for Global<T> {}

#[derive(Clone, Copy)]
struct Context {
    sequence: &'static str,
    position: i32,
    case: &'static str,
}

static CONTEXT: Global<Context> =
    Global(UnsafeCell::new(Context { sequence: "boot", position: 0, case: "none" }));
static SEQ: Global<u32> = Global(UnsafeCell::new(0));

#[link_section = ".uninit.hq184"]
static MARK: Global<MaybeUninit<[u32; 2]>> = Global(UnsafeCell::new(MaybeUninit::uninit()));

pub fn set_context(sequence: &'static str, position: i32, case: &'static str) {
    unsafe { *CONTEXT.0.get() = Context { sequence, position, case } };
}

fn context() -> Context {
    unsafe { *CONTEXT.0.get() }
}

fn next_seq(restart: bool) -> u32 {
    unsafe {
        let seq = SEQ.0.get();
        *seq = if restart { 0 } else { (*seq).wrapping_add(1) };
        *seq
    }
}

pub fn uart_write(bytes: &[u8]) {
    for &byte in bytes {
        unsafe {
            while read_volatile(USART2 as *const u32) & (1 << 7) == 0 {}
            write_volatile((USART2 + 0x04) as *mut u32, byte as u32);
        }
    }
}

fn uart_flush() {
    unsafe { while read_volatile(USART2 as *const u32) & (1 << 6) == 0 {} }
}

const GPIOA: usize = 0x4002_0000;
const RCC_AHB1ENR: usize = 0x4002_3830;

/// Hold the run silently until the host sends the exact line `GO\n` on USART2 RX (PA3, AF7,
/// the ST-LINK virtual COM port). Called after BLOCK-END n=1, so the session buffer of a
/// route B capture only ever holds the setup block until the host has read it (HQ #340), and
/// after every later BLOCK-END of the matrix (HQ #342). The
/// core keeps running: it polls the receiver and sends nothing. Only a whole line counts: after
/// the first byte that does not fit `GO\n`, the rest of that line is discarded up to and
/// including its LF, so `NOGO\n` or `GGO\n` never release the matrix.
pub fn wait_for_go() {
    const LINE: &[u8] = b"GO\n";
    unsafe {
        // GPIOA clock, PA3 to alternate function 7, then the receiver enable (CR1.RE).
        write_volatile(RCC_AHB1ENR as *mut u32, read_volatile(RCC_AHB1ENR as *const u32) | 1);
        let afrl = (GPIOA + 0x20) as *mut u32;
        write_volatile(afrl, (read_volatile(afrl) & !(0xF << 12)) | (7 << 12));
        let moder = GPIOA as *mut u32;
        write_volatile(moder, (read_volatile(moder) & !(3 << 6)) | (2 << 6));
        let cr1 = (USART2 + 0x0C) as *mut u32;
        write_volatile(cr1, read_volatile(cr1) | (1 << 2));
    }
    let mut matched = 0;
    let mut discarding = false;
    loop {
        // SR.RXNE (bit 5) or SR.ORE (bit 3): reading DR after SR clears both.
        let status = unsafe { read_volatile(USART2 as *const u32) };
        if status & ((1 << 5) | (1 << 3)) == 0 {
            continue;
        }
        let byte = unsafe { read_volatile((USART2 + 0x04) as *const u32) } as u8;
        if discarding {
            // Skip the rest of a line that already failed; its LF starts a fresh line.
            discarding = byte != b'\n';
            continue;
        }
        if byte == LINE[matched] {
            matched += 1;
        } else {
            matched = 0;
            discarding = byte != b'\n';
        }
        if matched == LINE.len() {
            return;
        }
    }
}

pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        crc ^= byte as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

pub fn fnv1a32(data: &[u8]) -> u32 {
    let mut value = 0x811C_9DC5u32;
    for &byte in data {
        value ^= byte as u32;
        value = value.wrapping_mul(0x0100_0193);
    }
    value
}

/// A text buffer that numbers and formats record fields without any allocation.
pub struct Line {
    buf: [u8; LINE_CAPACITY],
    len: usize,
}

impl Line {
    pub const fn new() -> Self {
        Line { buf: [0; LINE_CAPACITY], len: 0 }
    }

    pub fn str(&mut self, text: &str) -> &mut Self {
        for &byte in text.as_bytes() {
            self.byte(byte);
        }
        self
    }

    pub fn byte(&mut self, byte: u8) -> &mut Self {
        if self.len < LINE_CAPACITY {
            self.buf[self.len] = byte;
            self.len += 1;
        }
        self
    }

    pub fn dec(&mut self, value: i64) -> &mut Self {
        if value < 0 {
            self.byte(b'-');
        }
        let mut magnitude = value.unsigned_abs();
        let mut digits = [0u8; 20];
        let mut count = 0;
        loop {
            digits[count] = b'0' + (magnitude % 10) as u8;
            count += 1;
            magnitude /= 10;
            if magnitude == 0 {
                break;
            }
        }
        while count > 0 {
            count -= 1;
            self.byte(digits[count]);
        }
        self
    }

    pub fn hex(&mut self, value: u32, digits: usize) -> &mut Self {
        for index in (0..digits).rev() {
            let nibble = ((value >> (4 * index)) & 0xF) as u8;
            self.byte(if nibble < 10 { b'0' + nibble } else { b'a' + nibble - 10 });
        }
        self
    }

    pub fn b64(&mut self, data: &[u8]) -> &mut Self {
        for chunk in data.chunks(3) {
            let b0 = chunk[0];
            let b1 = if chunk.len() > 1 { chunk[1] } else { 0 };
            let b2 = if chunk.len() > 2 { chunk[2] } else { 0 };
            self.byte(B64[(b0 >> 2) as usize]);
            self.byte(B64[(((b0 & 3) << 4) | (b1 >> 4)) as usize]);
            self.byte(if chunk.len() > 1 { B64[(((b1 & 15) << 2) | (b2 >> 6)) as usize] } else { b'=' });
            self.byte(if chunk.len() > 2 { B64[(b2 & 63) as usize] } else { b'=' });
        }
        self
    }

    /// Append ` crc32=<8 hex>` over everything so far, then the newline, and send the line.
    pub fn send(&mut self) {
        let crc = crc32(&self.buf[..self.len]);
        self.str(" crc32=").hex(crc, 8).byte(b'\n');
        uart_write(&self.buf[..self.len]);
        self.len = 0;
    }

    pub fn open(&mut self, kind: &str, restart: bool) -> &mut Self {
        self.len = 0;
        let seq = next_seq(restart);
        self.str("HQ184 ").str(kind).str(" seq=").dec(seq as i64);
        self
    }

    pub fn field(&mut self, key: &str) -> &mut Self {
        self.byte(b' ').str(key).byte(b'=')
    }
}

pub fn payload(seed: u32, out: &mut [u8]) {
    let mut value = seed;
    for slot in out.iter_mut() {
        value ^= value << 13;
        value ^= value >> 17;
        value ^= value << 5;
        *slot = value as u8;
    }
}

/// FNV-1a-32 over `<prng_seed>|<sequence>|<position>|<case>|<transfer>`; zero maps to one.
pub fn transfer_seed(sequence: &str, position: i32, case: &str, transfer: u32) -> u32 {
    let mut text = Line::new();
    text.dec(frozen::PRNG_SEED as i64).byte(b'|').str(sequence).byte(b'|').dec(position as i64);
    text.byte(b'|').str(case).byte(b'|').dec(transfer as i64);
    let seed = fnv1a32(&text.buf[..text.len]);
    if seed == 0 { 1 } else { seed }
}

/// Distinct span values one XSUM record can carry. Twenty transfers of four bytes give 80
/// byte spans; on a stable clock they take one or two values, and a span that finds no free
/// slot is counted as dropped (the analyzer then refuses the occurrence as inconsistent).
const SPAN_SLOTS: usize = 16;

/// The condition counters, seed digest and span multiset of one applied configuration
/// (HQ #346). Each counter counts transfers. A transfer is clean when its capture has no
/// outstanding DMA transfer, DMA error, overcapture or timeout, the driver reported success
/// and the divider in force is still the one of the first transfer; only clean transfers
/// contribute spans.
pub struct Summary {
    pub cfg: &'static str,
    first_br: usize,
    pub transfers: u32,
    pub clean: u32,
    pub ndtr: u32,
    pub dma_err: u32,
    pub overcapture: u32,
    pub timeout: u32,
    pub drv_err: u32,
    pub cfg_changes: u32,
    /// FNV-1a-32 over the per-transfer seeds as eight lowercase hex digits, joined by '|'.
    pub seeds: u32,
    spans: [(u32, u32); SPAN_SLOTS],
    distinct: usize,
    pub dropped: u32,
}

impl Summary {
    pub const fn new() -> Self {
        Summary {
            cfg: "none", first_br: 0, transfers: 0, clean: 0, ndtr: 0, dma_err: 0,
            overcapture: 0, timeout: 0, drv_err: 0, cfg_changes: 0, seeds: 0x811C_9DC5,
            spans: [(0, 0); SPAN_SLOTS], distinct: 0, dropped: 0,
        }
    }

    fn feed(&mut self, byte: u8) {
        self.seeds ^= byte as u32;
        self.seeds = self.seeds.wrapping_mul(0x0100_0193);
    }

    /// Count one transfer on divider code `br` with its seed; true when it is clean.
    pub fn count(&mut self, br: usize, seed: u32, result: &crate::icap::CaptureResult,
                 driver_ok: bool) -> bool {
        if self.transfers == 0 {
            self.first_br = br;
            self.cfg = frozen::CAPTURE_PATH[br].id;
        } else {
            self.feed(b'|');
        }
        for index in (0..8).rev() {
            let nibble = ((seed >> (4 * index)) & 0xF) as u8;
            self.feed(if nibble < 10 { b'0' + nibble } else { b'a' + nibble - 10 });
        }
        self.transfers += 1;
        self.ndtr += (result.ndtr != 0) as u32;
        self.dma_err += (result.dma_err != 0) as u32;
        self.overcapture += (result.overcapture != 0) as u32;
        self.timeout += (result.timed_out != 0) as u32;
        self.drv_err += (!driver_ok) as u32;
        self.cfg_changes += (br != self.first_br) as u32;
        let clean = result.ndtr == 0 && result.dma_err == 0 && result.overcapture == 0
            && result.timed_out == 0 && driver_ok && br == self.first_br;
        self.clean += clean as u32;
        clean
    }

    /// Add one byte span of a clean transfer to the multiset, kept ascending.
    pub fn add_span(&mut self, span: u32) {
        let mut slot = 0;
        while slot < self.distinct && self.spans[slot].0 < span {
            slot += 1;
        }
        if slot < self.distinct && self.spans[slot].0 == span {
            self.spans[slot].1 += 1;
        } else if self.distinct == SPAN_SLOTS {
            self.dropped += 1;
        } else {
            let mut index = self.distinct;
            while index > slot {
                self.spans[index] = self.spans[index - 1];
                index -= 1;
            }
            self.spans[slot] = (span, 1);
            self.distinct += 1;
        }
    }

    /// `value:count` pairs joined by commas, or `none` when no transfer was clean.
    pub fn write_spans(&self, l: &mut Line) {
        if self.distinct == 0 {
            l.str("none");
        }
        for (index, &(value, count)) in self.spans[..self.distinct].iter().enumerate() {
            if index > 0 {
                l.byte(b',');
            }
            l.dec(value as i64).byte(b':').dec(count as i64);
        }
    }
}

/// Run-level state of the record: the line builder, block bookkeeping and the occurrence count.
pub struct Hq {
    line: Line,
    block: u32,
    occurrences: u32,
}

impl Hq {
    pub const fn new() -> Self {
        Hq { line: Line::new(), block: 0, occurrences: 0 }
    }

    pub fn boot(&mut self) {
        let l = &mut self.line;
        l.open("BOOT", true);
        l.field("build_id").str(frozen::BUILD_ID);
        l.field("embassy_rev").str(frozen::EMBASSY_REV);
        l.field("cases_sha256").str(frozen::CASES_SHA256);
        l.field("measurement_sha256").str(frozen::MEASUREMENT_SHA256);
        l.field("rustc").str(frozen::RUSTC);
        l.send();
    }

    pub fn recovery_boot(&mut self) {
        let l = &mut self.line;
        l.open("RECOVERY-BOOT", true);
        l.field("build_id").str(frozen::BUILD_ID);
        l.send();
    }

    /// Clock report. `dckcfgr` is the raw RCC_DCKCFGR, whose TIMPRE bit (24) decides the timer
    /// clocks (RM0390 6.2); the analyzer derives every timer clock from it.
    #[allow(clippy::too_many_arguments)]
    pub fn rcc(&mut self, cfgr: u32, pllcfgr: u32, dckcfgr: u32, sysclk: u32, hclk: u32, pclk1: u32,
               pclk2: u32) {
        let l = &mut self.line;
        l.open("RCC", false);
        l.field("cfgr").str("0x").hex(cfgr, 8);
        l.field("pllcfgr").str("0x").hex(pllcfgr, 8);
        l.field("dckcfgr").str("0x").hex(dckcfgr, 8);
        l.field("sysclk_hz").dec(sysclk as i64);
        l.field("hclk_hz").dec(hclk as i64);
        l.field("pclk1_hz").dec(pclk1 as i64);
        l.field("pclk2_hz").dec(pclk2 as i64);
        l.send();
    }

    /// Window-overhead calibration: not part of any table, kept as evidence of the correction.
    pub fn cal(&mut self, cfg: &str, runs: u32, overhead: i32, least: i32, most: i32, failed: u32) {
        let l = &mut self.line;
        l.open("CAL", false);
        l.field("cfg").str(cfg);
        l.field("runs").dec(runs as i64);
        l.field("failed").dec(failed as i64);
        l.field("overhead_cycles").dec(overhead as i64);

        l.field("min_cycles").dec(least as i64);
        l.field("max_cycles").dec(most as i64);
        l.send();
    }

    /// Diagnostics of the end-of-window interrupt (not part of any table).
    pub fn cal_isr(&mut self, entries: u32, spurious: u32, status: u32, counter: u32) {
        let l = &mut self.line;
        l.open("CALISR", false);
        l.field("entries").dec(entries as i64);
        l.field("spurious").dec(spurious as i64);
        l.field("sr").dec(status as i64);
        l.field("cnt").dec(counter as i64);
        l.send();
    }

    pub fn qual(&mut self, cfg: &str, step: u32, psc: u32, arr: u32, result: &CaptureResult, data: &[u8]) {
        let l = &mut self.line;
        l.open("QUAL", false);
        l.field("cfg").str(cfg);
        l.field("step").dec(step as i64);
        l.field("psc").dec(psc as i64);
        l.field("arr").dec(arr as i64);
        l.field("cycles").dec(result.cycles as i64);
        l.field("samples").dec(data.len() as i64);
        l.field("ndtr").dec(result.ndtr as i64);
        l.field("dma_err").dec(result.dma_err as i64);
        l.field("timeout").dec(result.timed_out as i64);
        l.field("data").b64(data);
        l.send();
    }

    /// One capture-path qualification (HQ #340): the TIM8_CH2 timestamps of the TIM2_CH3
    /// reference PWM, little-endian u16 each. min_ticks and max_ticks are on-chip diagnostics
    /// of the successive differences; the analyzer judges the raw timestamps only.
    pub fn icqual(&mut self, cfg: &str, pwm_psc: u32, pwm_arr: u32, result: &crate::icap::CaptureResult,
                  data: &[u8]) {
        let l = &mut self.line;
        l.open("ICQUAL", false);
        l.field("cfg").str(cfg);
        l.field("pwm_psc").dec(pwm_psc as i64);
        l.field("pwm_arr").dec(pwm_arr as i64);
        l.field("captures").dec(result.captures as i64);
        l.field("ndtr").dec(result.ndtr as i64);
        l.field("dma_err").dec(result.dma_err as i64);
        l.field("overcapture").dec(result.overcapture as i64);
        l.field("timeout").dec(result.timed_out as i64);
        l.field("min_ticks").dec(result.min_ticks as i64);
        l.field("max_ticks").dec(result.max_ticks as i64);
        l.field("data").b64(data);
        l.send();
    }

    /// One applied configuration. `error` names the driver error of a rejected `set_config`
    /// (`none` when it was accepted).
    #[allow(clippy::too_many_arguments)]
    pub fn occ(&mut self, sequence: &str, position: i32, case: &str, path: &str, f_req: u32,
               outcome: &str, cr1: u32, reported_hz: u32, error: &str) {
        let l = &mut self.line;
        l.open("OCC", false);
        l.field("sequence").str(sequence);
        l.field("position").dec(position as i64);
        l.field("case").str(case);
        l.field("path").str(path);
        l.field("f_req").dec(f_req as i64);
        l.field("outcome").str(outcome);
        l.field("cr1").str("0x").hex(cr1, 4);
        l.field("reported_hz").dec(reported_hz as i64);
        l.field("error").str(error);
        l.send();
    }

    /// The summary of one applied configuration's transfers, captured on the TIM8_CH2 path
    /// (HQ #346, replacing one raw-timestamp record per transfer): the condition counters,
    /// the seed digest and the multiset of byte spans of the clean transfers.
    pub fn xsum(&mut self, sequence: &str, position: i32, case: &str, s: &Summary) {
        let l = &mut self.line;
        l.open("XSUM", false);
        l.field("sequence").str(sequence);
        l.field("position").dec(position as i64);
        l.field("case").str(case);
        l.field("cfg").str(s.cfg);
        l.field("transfers").dec(s.transfers as i64);
        l.field("clean").dec(s.clean as i64);
        l.field("ndtr").dec(s.ndtr as i64);
        l.field("dma_err").dec(s.dma_err as i64);
        l.field("overcapture").dec(s.overcapture as i64);
        l.field("timeout").dec(s.timeout as i64);
        l.field("drv_err").dec(s.drv_err as i64);
        l.field("cfg_changes").dec(s.cfg_changes as i64);
        l.field("seeds").hex(s.seeds, 8);
        l.field("spans");
        s.write_spans(l);
        l.field("spans_dropped").dec(s.dropped as i64);
        l.send();
    }

    /// Close an occurrence; every `OCC_PER_BLOCK`-th one ends a block. After each block the
    /// firmware sends nothing until the host answers with GO (HQ #342), so the session buffer
    /// never holds more than one block the host has not read yet.
    pub fn occurrence_done(&mut self) {
        self.occurrences += 1;
        if self.occurrences % frozen::OCC_PER_BLOCK == 0 {
            self.block_end();
            wait_for_go();
        }
    }

    pub fn block_end(&mut self) {
        self.block += 1;
        let l = &mut self.line;
        l.open("BLOCK-END", false);
        l.field("n").dec(self.block as i64);
        l.send();
    }

    pub fn end(&mut self) {
        self.line.open("END", false);
        self.line.send();
        uart_flush();
    }
}

// ---------------------------------------------------------------------------------------
// Panic recovery: a marker in no-init RAM tells the next boot that this reset was the
// deliberate end of a panicking constructor, not a power-on.
// ---------------------------------------------------------------------------------------

/// True once if the previous run ended in the panic handler of this very build.
pub fn take_panic_marker() -> bool {
    unsafe {
        let mark = (*MARK.0.get()).as_mut_ptr();
        let first = read_volatile(mark as *const u32);
        let second = read_volatile((mark as *const u32).add(1));
        let wanted = MARK_MAGIC ^ frozen::BUILD_ID_U32;
        write_volatile(mark as *mut u32, 0);
        write_volatile((mark as *mut u32).add(1), 0);
        first == wanted && second == !wanted
    }
}

fn set_panic_marker() {
    unsafe {
        let mark = (*MARK.0.get()).as_mut_ptr();
        let wanted = MARK_MAGIC ^ frozen::BUILD_ID_U32;
        write_volatile(mark as *mut u32, wanted);
        write_volatile((mark as *mut u32).add(1), !wanted);
    }
}

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    let context = context();
    let mut line = Line::new();
    line.open("PANIC", false);
    line.field("sequence").str(context.sequence);
    line.field("position").dec(context.position as i64);
    line.field("case").str(context.case);
    line.send();
    uart_flush();
    set_panic_marker();
    cortex_m::peripheral::SCB::sys_reset()
}
