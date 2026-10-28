//! HQ #184 campaign firmware (NUCLEO-F446RE): measures what the SPI clock selection of
//! embassy-stm32 at c70760de42e2c8103138da6ff91626ff5a2688ba puts on the SCK pad.
//!
//! The run is autonomous: after reset it qualifies the pad sampler against a PWM of known
//! frequency, then runs the frozen cases (generated from ../cases.json and ../measurement.json
//! by build.rs, see frozen.rs) through the constructor and `set_config`, and reports every
//! step as `HQ184` lines on the ST-LINK virtual COM port. The last construct case panics by
//! design; the panic handler records it, resets the chip and the next boot reports
//! RECOVERY-BOOT with one more occurrence. Nothing here halts the core.

#![no_std]
#![no_main]

#[allow(dead_code)]
mod frozen {
    include!(concat!(env!("OUT_DIR"), "/frozen.rs"));
}
mod icap;
mod report;
mod sampler;
mod spi_cases;

use core::ptr::read_volatile;

use cortex_m_rt::entry;
use embassy_stm32::rcc::{
    APBPrescaler, AHBPrescaler, Pll, PllMul, PllPDiv, PllPreDiv, PllSource, Sysclk,
};
use embassy_stm32::usart::{self, UartTx};

use report::Hq;

const RCC_PLLCFGR: usize = 0x4002_3804;
const RCC_CFGR: usize = 0x4002_3808;
/// RCC_DCKCFGR (RM0390 6.3.25, offset 0x8C): bit 24 TIMPRE selects the timer clocks.
const RCC_DCKCFGR: usize = 0x4002_388C;
/// Start-up hold before the first record: 45 s at the 64 MHz core clock (below u32::MAX and
/// below the 67 s wrap of the DWT cycle counter).
const HOLD_CYCLES: u32 = 45 * 64_000_000;

fn reg(address: usize) -> u32 {
    unsafe { read_volatile(address as *const u32) }
}

#[entry]
fn main() -> ! {
    let mut config = embassy_stm32::Config::default();
    config.rcc.hsi = true;
    config.rcc.pll_src = PllSource::Hsi;
    config.rcc.pll = Some(Pll {
        prediv: PllPreDiv::Div16,
        mul: PllMul::Mul256,
        divp: Some(PllPDiv::Div4),
        divq: None,
        divr: None,
    });
    config.rcc.sys = Sysclk::Pll1P;
    config.rcc.ahb_pre = AHBPrescaler::Div1;
    config.rcc.apb1_pre = APBPrescaler::Div16;
    config.rcc.apb2_pre = APBPrescaler::Div1;
    let mut p = embassy_stm32::init(config);

    // ST-LINK virtual COM port: USART2 on PA2. Embassy only sets it up; the writes are raw.
    let mut uart_config = usart::Config::default();
    uart_config.baudrate = frozen::BAUD;
    match UartTx::new_blocking(p.USART2.reborrow(), p.PA2.reborrow(), uart_config) {
        Ok(tx) => core::mem::forget(tx),
        Err(_) => loop {
            cortex_m::asm::nop();
        },
    }

    // Cycle counter for the capture windows.
    let mut core = unsafe { cortex_m::Peripherals::steal() };
    core.DCB.enable_trace();
    core.DWT.enable_cycle_counter();

    sampler::init();
    let mut hq = Hq::new();

    // Stay silent for about 45 s after every start. A detached plan that only flashes and
    // resets ends right after its reset step, and the COM session that is opened afterwards
    // (it would block the reset if it were open earlier) still receives every record from
    // BOOT on. The same hold precedes RECOVERY-BOOT.
    sampler::delay_cycles(HOLD_CYCLES);

    if report::take_panic_marker() {
        hq.recovery_boot();
        // The reset lost the calibration of the first boot: establish clocks, calibration and
        // qualification again, so the recovery occurrence is judged on its own boot's data.
        bring_up(&mut hq, &p);
        spi_cases::run_recovery(&mut hq, &mut p);
        hq.end();
        park();
    }

    hq.boot();
    bring_up(&mut hq, &p);
    hq.block_end();
    // Nothing more is sent until the host asks for it: a route B capture reads the whole
    // setup block from the session buffer before the matrix starts (HQ #340).
    report::wait_for_go();

    spi_cases::run_set_config(&mut hq, &mut p, "set_config_ascending", frozen::SET_CONFIG_ASCENDING);
    spi_cases::run_set_config(&mut hq, &mut p, "set_config_descending", frozen::SET_CONFIG_DESCENDING);
    spi_cases::run_set_config(&mut hq, &mut p, "set_config_revisited", frozen::SET_CONFIG_REVISITED);
    // Ends in the panicking constructor case; control never returns from it.
    spi_cases::run_construct(&mut hq, &mut p, frozen::CONSTRUCT);

    // Only reached if the pinned constructor accepted the above-kernel-clock case.
    hq.end();
    park();
}

/// Report the clock tree, calibrate the capture window, qualify the pad sampler and then the
/// TIM8_CH2 capture path (HQ #340).
fn bring_up(hq: &mut Hq, p: &embassy_stm32::Peripherals) {
    let clocks = embassy_stm32::rcc::clocks(&p.RCC);
    hq.rcc(
        reg(RCC_CFGR),
        reg(RCC_PLLCFGR),
        reg(RCC_DCKCFGR),
        clocks.sys.to_hertz().unwrap().0,
        clocks.hclk1.to_hertz().unwrap().0,
        clocks.pclk1.to_hertz().unwrap().0,
        clocks.pclk2.to_hertz().unwrap().0,
    );
    sampler::calibrate(hq);
    sampler::qualify(hq);
    icap::qualify(hq);
}

fn park() -> ! {
    loop {
        cortex_m::asm::wfi();
    }
}
