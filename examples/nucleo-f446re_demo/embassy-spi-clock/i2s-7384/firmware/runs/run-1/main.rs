//! Agentic HIL evidence firmware for embassy-rs/embassy#7384 on a NUCLEO-F446RE.
//!
//! It runs the sequence of tests/stm32/src/bin/i2s_dma.rs (same constructor, pins, DMA stream,
//! wavetable, ring buffer and writes) and reports on USART2, the ST-LINK virtual COM port,
//! instead of defmt-rtt and `bkpt`. Differences from the upstream test:
//! - The clocks come from HSI 16 MHz / 8 instead of HSE 8 MHz bypass / 4. Both PLLs see the
//!   same 2 MHz input, so SYSCLK (180 MHz), APB1 (45 MHz), APB2 (90 MHz) and the PLLI2S R
//!   output (60 MHz) are the test's.
//! - Every write and stop is bounded by a timeout and counted instead of ignored.
//! - Afterwards it times 400 more writes against the cycle counter, which gives the frame rate
//!   the DMA really drains, and reads back the SPI3/I2S registers.
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
use embassy_stm32::i2s::{Config, Format, I2S};
use embassy_stm32::usart::{self, UartTx};
use embassy_stm32::{bind_interrupts, dma, peripherals};
use embassy_time::{with_timeout, Duration, Timer};

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
    let mut failed = false;
    for cycle in 1..=3 {
        i2s.start();
        let (mut ok, mut errors, mut timeouts) = (0, 0, 0);
        for _ in 0..10 {
            match with_timeout(WRITE_TIMEOUT, i2s.write(&wavetable)).await {
                Ok(Ok(())) => ok += 1,
                Ok(Err(_)) => errors += 1,
                Err(_) => timeouts += 1,
            }
        }
        let stopped = with_timeout(WRITE_TIMEOUT, i2s.stop()).await.is_ok();
        failed |= errors + timeouts > 0 || !stopped;
        emit(format_args!(
            "CYCLE n={} writes_ok={} write_errors={} write_timeouts={} stop={}",
            cycle,
            ok,
            errors,
            timeouts,
            if stopped { "ok" } else { "timeout" },
        ));
    }

    // Frame rate the DMA drains: once the ring buffer is full, every write waits for the I2S
    // peripheral to consume the same amount, so writes per cycle count give frames per second.
    i2s.start();
    let mut rate_failures = 0;
    for _ in 0..RATE_WARMUP_WRITES {
        if !matches!(with_timeout(WRITE_TIMEOUT, i2s.write(&wavetable)).await, Ok(Ok(()))) {
            rate_failures += 1;
        }
    }
    let start = DWT::cycle_count();
    for _ in 0..RATE_WRITES {
        if !matches!(with_timeout(WRITE_TIMEOUT, i2s.write(&wavetable)).await, Ok(Ok(()))) {
            rate_failures += 1;
        }
    }
    let cycles = DWT::cycle_count().wrapping_sub(start);
    emit_registers("running");
    let stopped = with_timeout(WRITE_TIMEOUT, i2s.stop()).await.is_ok();

    let frames = RATE_WRITES as u64 * FRAMES_PER_WRITE;
    let measured_mhz = frames * sys as u64 * 1000 / (cycles as u64).max(1);
    // RM0390 28.4.4: Fs = I2SxCLK / (frame bits * (2 * I2SDIV + ODD)) without master clock.
    let i2spr = reg(SPI3 + 0x20);
    let frame_bits: u64 = if reg(SPI3 + 0x1C) & 1 == 1 { 64 } else { 32 };
    let divider = (2 * (i2spr & 0xFF) + ((i2spr >> 8) & 1)) as u64;
    let expected_mhz = plli2s_r as u64 * 1000 / (frame_bits * divider.max(1));
    let deviation_ppm = (measured_mhz as i64 - expected_mhz as i64) * 1_000_000 / (expected_mhz as i64).max(1);
    emit(format_args!(
        "RATE writes={} frames={} cycles={} sys={} measured_mhz={} expected_mhz={} deviation_ppm={} write_failures={} stop={}",
        RATE_WRITES,
        frames,
        cycles,
        sys,
        measured_mhz,
        expected_mhz,
        deviation_ppm,
        rate_failures,
        if stopped { "ok" } else { "timeout" },
    ));
    failed |= rate_failures > 0 || !stopped || deviation_ppm.abs() > 10_000;
    emit(format_args!("RESULT {}", if failed { "fail" } else { "pass" }));

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
