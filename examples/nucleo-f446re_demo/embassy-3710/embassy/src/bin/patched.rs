//! hq175 `patched`: the API addition under test, on TIM1 CH1 (PA8) and CH1N (PA7).
//!
//! Built against a copy of the Embassy checkout with
//! `embassy-independent-outputs.patch` applied, which adds
//! `set_main_output_enable`, `set_complementary_output_enable`,
//! `get_main_output_enable` and `get_complementary_output_enable` to
//! `ComplementaryPwm`. This binary does not compile against the unmodified
//! checkout, which is why the two images are separate binaries of one crate.
//!
//! Three sequences:
//!
//! * `api8` and `api0`: the four states (both off, main only, both on,
//!   complementary only) reached one output at a time through the new methods, at
//!   dead time 8 and 0 ticks, each with both getters read back beside the register.
//!   At dead time 0 the main output's waveform does not change at all when the
//!   complementary output is switched, so "the other output is untouched" can be
//!   judged without the dead time shifting anything.
//! * `legacy`: `enable()` and `disable()`, which the patch does not touch and which
//!   must still switch both outputs of the channel together.
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
    let mut rig = Rig::new(uart, "patched", pwm.get_frequency().0 * PERIOD);

    rig.print_setup("api8");
    hq175::run_sequence(&mut rig, "api8", &GRAY, 2, |step| {
        match step {
            "main_on" => pwm.set_main_output_enable(Channel::Ch1, true),
            "comp_on" => pwm.set_complementary_output_enable(Channel::Ch1, true),
            "main_off" => pwm.set_main_output_enable(Channel::Ch1, false),
            _ => pwm.set_complementary_output_enable(Channel::Ch1, false),
        }
        Some((
            pwm.get_main_output_enable(Channel::Ch1),
            pwm.get_complementary_output_enable(Channel::Ch1),
        ))
    });

    pwm.set_dead_time(0);
    rig.print_setup("api0");
    // The same four steps again; written out rather than shared with the block above
    // so each sequence shows exactly which call it makes.
    hq175::run_sequence(&mut rig, "api0", &GRAY, 1, |step| {
        match step {
            "main_on" => pwm.set_main_output_enable(Channel::Ch1, true),
            "comp_on" => pwm.set_complementary_output_enable(Channel::Ch1, true),
            "main_off" => pwm.set_main_output_enable(Channel::Ch1, false),
            _ => pwm.set_complementary_output_enable(Channel::Ch1, false),
        }
        Some((
            pwm.get_main_output_enable(Channel::Ch1),
            pwm.get_complementary_output_enable(Channel::Ch1),
        ))
    });

    pwm.set_dead_time(DEAD);
    rig.print_setup("legacy");
    hq175::run_sequence(&mut rig, "legacy", &LEGACY, 1, |step| {
        if step == "enable" {
            pwm.enable(Channel::Ch1);
        } else {
            pwm.disable(Channel::Ch1);
        }
        None
    });

    rig.finish();
    // `pwm` must outlive the measurements: dropping it would gate TIM1's clock.
    loop {}
}
