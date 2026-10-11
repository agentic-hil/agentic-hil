//! hq175 `baseline`: the same measurement against the unmodified embassy-stm32.
//!
//! Built from the untouched checkout of the source cache, so it cannot call the
//! methods the job's patch adds. It runs three sequences on TIM1 CH1 (PA8) and
//! CH1N (PA7):
//!
//! * `raw8` and `raw0`: the four CC1E/CC1NE states written straight to TIM1_CCER,
//!   at dead time 8 and 0 ticks. No driver code sits between the write and the
//!   pins, so these are the hardware controls for the output-control table every
//!   expectation about the new API rests on.
//! * `legacy`: today's `enable()` and `disable()`, which switch both outputs of a
//!   channel together. That is the behaviour the issue asks to keep, and the
//!   baseline the patched image is compared against.
//!
//! Every value printed is a count or a register word; the tests judge them.
#![no_std]
#![no_main]

use embassy_executor::Spawner;
use embassy_stm32::gpio::{OutputType, Pull, Speed};
use embassy_stm32::time::khz;
use embassy_stm32::timer::Channel;
use embassy_stm32::timer::complementary_pwm::{ComplementaryPwm, ComplementaryPwmPin};
use embassy_stm32::timer::simple_pwm::{PwmPin, PwmPinConfig};
use embassy_stm32::usart::{Config, Uart};

use hq175::{DEAD, GRAY, HIGH, LEGACY, PERIOD, Rig};

#[embassy_executor::main]
async fn main(_spawner: Spawner) {
    let p = embassy_stm32::init(Default::default());
    let uart = Uart::new_blocking(p.USART2, p.PA2, p.PA3, Config::default()).unwrap();

    // TIM1_CH1 on PA8 and TIM1_CH1N on PA7, both AF1, each with a pull so a pin the
    // timer has released reads a defined level. Both are in GPIOA, so one read of
    // GPIOA_IDR catches both outputs at the same instant.
    let pin_config = PwmPinConfig {
        output_type: OutputType::PushPull,
        speed: Speed::VeryHigh,
        pull: Pull::Down,
    };
    let ch1 = PwmPin::new_with_config(p.PA8, pin_config);
    let ch1n = ComplementaryPwmPin::new_with_config(p.PA7, pin_config);
    let mut pwm = ComplementaryPwm::new(
        p.TIM1,
        Some(ch1),
        Some(ch1n),
        None,
        None,
        None,
        None,
        None,
        None,
        // Any representable request will do: the period in ticks is then written
        // exactly, because the expectations are tick ratios (see `force_period`).
        khz(100),
        // CountingMode::EdgeAlignedUp, so one period is one count up to ARR.
        Default::default(),
    );
    hq175::force_period(PERIOD);
    pwm.set_duty(Channel::Ch1, HIGH);
    pwm.set_dead_time(DEAD);

    // The timer clock as the driver reads it back; the rig measures HCLK against it.
    let mut rig = Rig::new(uart, "baseline", pwm.get_frequency().0 * PERIOD);

    rig.print_setup("raw8");
    hq175::run_sequence(&mut rig, "raw8", &GRAY, 2, |step| {
        raw_step(step);
        None
    });

    pwm.set_dead_time(0);
    rig.print_setup("raw0");
    hq175::run_sequence(&mut rig, "raw0", &GRAY, 2, |step| {
        raw_step(step);
        None
    });

    pwm.set_dead_time(DEAD);
    rig.print_setup("legacy");
    hq175::run_sequence(&mut rig, "legacy", &LEGACY, 1, |step| {
        if step == "enable" {
            pwm.enable(Channel::Ch1);
        } else {
            pwm.disable(Channel::Ch1);
        }
        // The unmodified driver has no way to read CC1NE back, so no getter fields.
        None
    });

    rig.finish();
    // `pwm` must outlive the measurements: dropping it would gate TIM1's clock.
    loop {}
}

/// One step of the four-state sequence, written straight to TIM1_CCER.
///
/// Each write touches one bit of CCER and leaves the other alone, exactly as
/// `Timer::enable_channel` and `Timer::enable_complementary_channel` do, so what the
/// pins then show is the hardware's answer and not any driver's.
fn raw_step(step: &str) {
    match step {
        "main_on" => hq175::set_cc1e(true),
        "comp_on" => hq175::set_cc1ne(true),
        "main_off" => hq175::set_cc1e(false),
        _ => hq175::set_cc1ne(false),
    }
}
