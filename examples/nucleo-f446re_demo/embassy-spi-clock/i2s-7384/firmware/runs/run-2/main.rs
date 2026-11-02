//! Agentic HIL evidence firmware for embassy-rs/embassy#7384 on a NUCLEO-F446RE.
//!
//! It runs the sequence of tests/stm32/src/bin/i2s_dma.rs (same constructor, pins, DMA stream,
//! wavetable, ring buffer and writes) and reports on USART2, the ST-LINK virtual COM port,
//! instead of defmt-rtt and `bkpt`. Differences from the upstream test:
//! - The clocks come from HSI 16 MHz / 8 instead of HSE 8 MHz bypass / 4. Both PLLs see the
//!   same 2 MHz input, so SYSCLK (180 MHz), APB1 (45 MHz), APB2 (90 MHz) and the PLLI2S R
//!   output (60 MHz) are the test's.
//! - Every write and stop is bounded by a timeout. The test discards each write's result with
//!   `.ok()`; here every outcome is reported by write index.
//! - Afterwards it times 400 more writes against the cycle counter, which gives the frame rate
//!   the DMA really drains, and reads back the SPI3/I2S registers.
//!
//! `RESULT pass` needs what the upstream test needs (the sequence runs to its end without a
//! panic and without a write or stop that hangs) and a frame rate within 1 % of the one the I2S
//! registers program. Write errors are reported, not judged, as in the upstream test.
//!
//! Every line is kept and the whole report is sent again every 2 s, so a serial session opened
//! after the reset still receives it. The panic handler adds its line and keeps repeating.

#![no_std]
#![no_main]

use core::cell::UnsafeCell;
use core::fmt::{self, Write as _};
use core::ptr::{read_volatile, write_volatile};

use cortex_m::peripheral::DWT;
use embassy_executor::Spawner;
use embassy_stm32::i2s::{Config, Error as I2sError, Format, I2S};
use embassy_stm32::usart::{self, UartTx};
use embassy_stm32::{bind_interrupts, dma, peripherals};
use embassy_time::{with_timeout, Duration, TimeoutError, Timer};

bind_interrupts!(struct Irqs {
    DMA1_STREAM7 => dma::InterruptHandler<peripherals::DMA1_CH7>;
});

const VARIANT: &str = env!("CARGO_PKG_NAME");
const EMBASSY: &str = env!("I2S7384_EMBASSY");

const USART2: usize = 0x4000_4400;
const SPI3: usize = 0x4000_3C00;
const RCC: usize = 0x4002_3800;
/// About 2 s at the 180 MHz core clock, for the repeats from the panic handler.
const REPEAT_CYCLES: u32 = 360_000_000;
const WRITE_TIMEOUT: Duration = Duration::from_millis(500);
const RATE_WARMUP_WRITES: u32 = 10;
const RATE_WRITES: u32 = 400;
/// 1200 u16 words per write, two words per Data16Channel32 frame.
const FRAMES_PER_WRITE: u64 = 600;
const RATE_LIMIT_PPM: i64 = 10_000;

/// Single-core firmware; the DMA interrupt never touches these.
struct Global<T>(UnsafeCell<T>);
unsafe impl<T> Sync for Global<T> {}

const REPORT_CAPACITY: usize = 4096;
static REPORT: Global<([u8; REPORT_CAPACITY], usize)> = Global(UnsafeCell::new(([0; REPORT_CAPACITY], 0)));
static REPEATS: Global<u32> = Global(UnsafeCell::new(0));

fn reg(address: usize) -> u32 {
    unsafe { read_volatile(address as *const u32) }
}

fn uart_write(bytes: &[u8]) {
    for &byte in bytes {
        unsafe {
            while read_volatile(USART2 as *const u32) & (1 << 7) == 0 {}
            write_volatile((USART2 + 0x04) as *mut u32, byte as u32);
        }
    }
}

struct Line {
    buf: [u8; 320],
    len: usize,
}

impl fmt::Write for Line {
    fn write_str(&mut self, text: &str) -> fmt::Result {
        for &byte in text.as_bytes() {
            // Keep the last byte for the line end; map Windows separators in source paths.
            if self.len < self.buf.len() - 1 {
                self.buf[self.len] = if byte == b'\\' { b'/' } else { byte };
                self.len += 1;
            }
        }
        Ok(())
    }
}

/// Send one `I2S7384 ...` line now and keep it for the repeats.
fn emit(args: fmt::Arguments) {
    let mut line = Line { buf: [0; 320], len: 0 };
    let _ = line.write_str("I2S7384 ");
    let _ = line.write_fmt(args);
    line.buf[line.len] = b'\n';
    line.len += 1;
    uart_write(&line.buf[..line.len]);
    let (report, used) = unsafe { &mut *REPORT.0.get() };
    let take = line.len.min(REPORT_CAPACITY - *used);
    report[*used..*used + take].copy_from_slice(&line.buf[..take]);
    *used += take;
}

fn repeat() {
    let count = unsafe {
        let count = &mut *REPEATS.0.get();
        *count += 1;
        *count
    };
    let mut line = Line { buf: [0; 320], len: 0 };
    let _ = write!(line, "I2S7384 REPEAT n={} variant={} embassy={}\n", count, VARIANT, EMBASSY);
    uart_write(&line.buf[..line.len]);
    let (report, used) = unsafe { &*REPORT.0.get() };
    uart_write(&report[..*used]);
}

/// Outcomes of a series of writes, one bit per write index.
#[derive(Default)]
struct Writes {
    ok: u32,
    overrun: u32,
    other_error: u32,
    timeout: u32,
}

impl Writes {
    fn record(&mut self, index: u32, outcome: Result<Result<(), I2sError>, TimeoutError>) {
        let bit = 1 << index;
        match outcome {
            Ok(Ok(())) => self.ok |= bit,
            Ok(Err(I2sError::Overrun)) => self.overrun |= bit,
            Ok(Err(_)) => self.other_error |= bit,
            Err(_) => self.timeout |= bit,
        }
    }
}

/// Write indices from a bit mask, as `[0,3]`.
struct Indices(u32);

impl fmt::Display for Indices {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("[")?;
        let mut separator = "";
        for index in 0..32 {
            if self.0 & (1 << index) != 0 {
                write!(f, "{}{}", separator, index)?;
                separator = ",";
            }
        }
        f.write_str("]")
    }
}

fn emit_registers(stage: &str) {
    let cr1 = reg(SPI3);
    emit(format_args!(
        "REGS stage={} spi3_cr1=0x{:04x} br={} spi3_cr2=0x{:04x} i2scfgr=0x{:04x} i2spr=0x{:04x}",
        stage,
        cr1,
        (cr1 >> 3) & 0b111,
        reg(SPI3 + 0x04),
        reg(SPI3 + 0x1C),
        reg(SPI3 + 0x20),
    ));
}

#[embassy_executor::main]
async fn main(_spawner: Spawner) {
    let config = {
        use embassy_stm32::rcc::*;

        let mut config = embassy_stm32::Config::default();
        config.rcc.hsi = true;
        config.rcc.pll_src = PllSource::Hsi;
        config.rcc.pll = Some(Pll {
            prediv: PllPreDiv::Div8,
            mul: PllMul::Mul180,
            divp: Some(PllPDiv::Div2), // 16mhz / 8 * 180 / 2 = 180Mhz.
            divq: None,
            divr: None,
        });
        config.rcc.ahb_pre = AHBPrescaler::Div1;
        config.rcc.apb1_pre = APBPrescaler::Div4;
        config.rcc.apb2_pre = APBPrescaler::Div2;
        config.rcc.sys = Sysclk::Pll1P;

        config.rcc.plli2s = Some(Pll {
            prediv: PllPreDiv::Div8,
            mul: PllMul::Mul60,
            divp: None,
            divq: None,
            divr: Some(PllRDiv::Div2), // 16mhz / 8 * 60 / 2 = 60Mhz.
        });
        config.enable_debug_during_sleep = true;

        config
    };

    let p = embassy_stm32::init(config);

    // ST-LINK virtual COM port: USART2 on PA2. Embassy sets it up; the writes are raw so that
    // the panic handler can use the same path.
    let mut uart_config = usart::Config::default();
    uart_config.baudrate = 115_200;
    match UartTx::new_blocking(p.USART2, p.PA2, uart_config) {
        Ok(tx) => core::mem::forget(tx),
        Err(_) => loop {
            cortex_m::asm::nop();
        },
    }
    let mut core = unsafe { cortex_m::Peripherals::steal() };
    core.DCB.enable_trace();
    core.DWT.enable_cycle_counter();

    let clocks = embassy_stm32::rcc::clocks(&p.RCC);
    let sys = clocks.sys.to_hertz().map_or(0, |hz| hz.0);
    let plli2s_r = clocks.plli2s1_r.to_hertz().map_or(0, |hz| hz.0);
    emit(format_args!("BOOT variant={} embassy={} board=NUCLEO-F446RE", VARIANT, EMBASSY));
    emit(format_args!(
        "CLOCKS sys={} pclk1={} pclk2={} plli2s_r={}",
        sys,
        clocks.pclk1.to_hertz().map_or(0, |hz| hz.0),
        clocks.pclk2.to_hertz().map_or(0, |hz| hz.0),
        plli2s_r,
    ));
    emit(format_args!(
        "RCC cfgr=0x{:08x} pllcfgr=0x{:08x} plli2scfgr=0x{:08x} dckcfgr=0x{:08x}",
        reg(RCC + 0x08),
        reg(RCC + 0x04),
        reg(RCC + 0x84),
        reg(RCC + 0x8C),
    ));

    // stereo wavetable generation
    let mut wavetable = [0u16; 1200];
    for (i, frame) in wavetable.chunks_mut(2).enumerate() {
        frame[0] = ((((i / 150) % 2) * 2048) as i16 - 1024) as u16; // 160 Hz square wave in left channel
        frame[1] = ((((i / 100) % 2) * 2048) as i16 - 1024) as u16; // 240 Hz square wave in right channel
    }

    // i2s configuration
    let mut dma_buffer = [0u16; 2400];

    let mut i2s_config = Config::default();
    i2s_config.format = Format::Data16Channel32;
    i2s_config.master_clock = false;
    emit(format_args!(
        "CONSTRUCT begin call=I2S::new_txonly_nomck(SPI3,PB5,PA15,PB3,DMA1_CH7) format=Data16Channel32 frequency={} master_clock=false",
        i2s_config.frequency.0,
    ));
    let mut i2s = I2S::new_txonly_nomck(
        p.SPI3,
        p.PB5,  // sd
        p.PA15, // ws
        p.PB3,  // ck
        p.DMA1_CH7,
        Irqs,
        &mut dma_buffer,
        i2s_config,
    );
    emit(format_args!("CONSTRUCT ok"));
    emit_registers("constructed");

    // The upstream test sequence: three times start, ten writes, stop.
    let (mut completed, mut stops, mut write_errors) = (0, 0, 0);
    for cycle in 1..=3 {
        i2s.start();
        let mut writes = Writes::default();
        for index in 0..10 {
            writes.record(index, with_timeout(WRITE_TIMEOUT, i2s.write(&wavetable)).await);
        }
        let stopped = with_timeout(WRITE_TIMEOUT, i2s.stop()).await.is_ok();
        completed += 10 - writes.timeout.count_ones();
        stops += stopped as u32;
        write_errors += (writes.overrun | writes.other_error).count_ones();
        emit(format_args!(
            "CYCLE n={} ok={} overrun={} other_error={} timeout={} stop={}",
            cycle,
            writes.ok.count_ones(),
            Indices(writes.overrun),
            Indices(writes.other_error),
            Indices(writes.timeout),
            if stopped { "ok" } else { "timeout" },
        ));
    }

    // Frame rate the DMA drains: once the ring buffer is full, every write waits for the I2S
    // peripheral to consume the same amount, so writes per cycle count give frames per second.
    // A failed write resets the ring buffer, so a timed series only counts without one.
    i2s.start();
    let mut warmup = Writes::default();
    for index in 0..RATE_WARMUP_WRITES {
        warmup.record(index, with_timeout(WRITE_TIMEOUT, i2s.write(&wavetable)).await);
    }
    emit(format_args!(
        "RATE warmup writes={} ok={} overrun={} other_error={} timeout={}",
        RATE_WARMUP_WRITES,
        warmup.ok.count_ones(),
        Indices(warmup.overrun),
        Indices(warmup.other_error),
        Indices(warmup.timeout),
    ));
    let start = DWT::cycle_count();
    let mut timed_failures = 0;
    for _ in 0..RATE_WRITES {
        if !matches!(with_timeout(WRITE_TIMEOUT, i2s.write(&wavetable)).await, Ok(Ok(()))) {
            timed_failures += 1;
        }
    }
    let cycles = DWT::cycle_count().wrapping_sub(start);
    emit_registers("running");
    let stopped = with_timeout(WRITE_TIMEOUT, i2s.stop()).await.is_ok();

    let frames = RATE_WRITES as u64 * FRAMES_PER_WRITE;
    let measured_millihertz = frames * sys as u64 * 1000 / (cycles as u64).max(1);
    // RM0390 28.4.4: Fs = I2SxCLK / (frame bits * (2 * I2SDIV + ODD)) without master clock.
    let i2spr = reg(SPI3 + 0x20);
    let frame_bits: u64 = if reg(SPI3 + 0x1C) & 1 == 1 { 64 } else { 32 };
    let divider = (2 * (i2spr & 0xFF) + ((i2spr >> 8) & 1)) as u64;
    let expected_millihertz = plli2s_r as u64 * 1000 / (frame_bits * divider.max(1));
    let deviation_ppm = (measured_millihertz as i64 - expected_millihertz as i64) * 1_000_000
        / (expected_millihertz as i64).max(1);
    emit(format_args!(
        "RATE timed writes={} failures={} frames={} cycles={} sys={} measured_hz={}.{:03} expected_hz={}.{:03} deviation_ppm={} stop={}",
        RATE_WRITES,
        timed_failures,
        frames,
        cycles,
        sys,
        measured_millihertz / 1000,
        measured_millihertz % 1000,
        expected_millihertz / 1000,
        expected_millihertz % 1000,
        deviation_ppm,
        if stopped { "ok" } else { "timeout" },
    ));

    // What the upstream test needs: the sequence ran to its end (a panic never gets here).
    let sequence = completed == 30 && stops == 3;
    // What this firmware adds: the DMA drains at the frame rate the I2S registers program.
    let rate = timed_failures == 0 && stopped && deviation_ppm.abs() <= RATE_LIMIT_PPM;
    emit(format_args!(
        "VERDICT upstream_sequence={} writes_completed={}/30 stops={}/3",
        if sequence { "pass" } else { "fail" },
        completed,
        stops,
    ));
    emit(format_args!(
        "VERDICT frame_rate={} deviation_ppm={} limit_ppm={}",
        if rate { "pass" } else { "fail" },
        deviation_ppm,
        RATE_LIMIT_PPM,
    ));
    emit(format_args!(
        "NOTE write_errors={} of 30 in the upstream sequence, reported and not judged: the test discards write results with .ok()",
        write_errors,
    ));
    emit(format_args!("RESULT {}", if sequence && rate { "pass" } else { "fail" }));

    loop {
        Timer::after_secs(2).await;
        repeat();
    }
}

#[panic_handler]
fn panic(info: &core::panic::PanicInfo) -> ! {
    cortex_m::interrupt::disable();
    match info.location() {
        Some(location) => emit(format_args!(
            "PANIC message=\"{}\" location={}:{}",
            info.message(),
            location.file(),
            location.line(),
        )),
        None => emit(format_args!("PANIC message=\"{}\"", info.message())),
    }
    emit(format_args!("RESULT panic"));
    loop {
        cortex_m::asm::delay(REPEAT_CYCLES);
        repeat();
    }
}
