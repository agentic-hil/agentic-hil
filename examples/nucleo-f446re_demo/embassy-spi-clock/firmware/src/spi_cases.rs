//! The frozen cases run through the unmodified embassy-stm32 SPI driver at the pinned commit.
//!
//! Blocking SPI2, transmit only, SCK on PB10 (AF5) and MOSI on PB15 (HQ #342). The fitted
//! jumper CN10-25 (PB10) to CN10-19 (PC7) carries SCK to the TIM8_CH2 capture input. Every
//! applied configuration is recorded (OCC), then transferred 20 times with the capture path
//! recording the rising SCK edges of each frame; one XSUM record (HQ #346) summarises them
//! with condition counters, a seed digest and the multiset of byte spans. Every one of those
//! transfers goes through the driver's own `blocking_write`, and a failed one is counted on
//! the XSUM record (`drv_err`). The constructor path (`Spi::new_blocking_txonly`) and the reconfiguration
//! path (`set_config`) are both exercised; the readback is `get_current_config` plus the raw
//! CR1, and a rejected `set_config` names its error on the OCC record.

use core::fmt::Write as _;
use core::ptr::read_volatile;

use embassy_stm32::spi::{self, Spi};
use embassy_stm32::time::Hertz;
use embassy_stm32::Peripherals;

use crate::frozen::{self, Case};
use crate::icap;
use crate::report::{self, Hq, Summary};

const SPI2_CR1: usize = 0x4000_3800;

fn spi_cr1() -> u32 {
    unsafe { read_volatile(SPI2_CR1 as *const u32) }
}

fn config_for(f_req: u32) -> spi::Config {
    let mut config = spi::Config::default();
    config.frequency = Hertz(f_req);
    config
}

/// The Debug name of a driver error, cut to letters, digits and underscores so that it stays
/// one field of the line protocol.
struct ErrorName {
    buf: [u8; 32],
    len: usize,
}

impl ErrorName {
    fn text(text: &str) -> Self {
        let mut name = ErrorName { buf: [0; 32], len: 0 };
        let _ = name.write_str(text);
        name
    }

    fn of(error: &impl core::fmt::Debug) -> Self {
        let mut name = ErrorName::text("");
        let _ = write!(name, "{:?}", error);
        if name.len == 0 {
            let _ = name.write_str("unreported");
        }
        name
    }

    fn as_str(&self) -> &str {
        core::str::from_utf8(&self.buf[..self.len]).unwrap_or("unreported")
    }
}

impl core::fmt::Write for ErrorName {
    fn write_str(&mut self, text: &str) -> core::fmt::Result {
        for &byte in text.as_bytes() {
            if self.len < self.buf.len() {
                self.buf[self.len] = if byte.is_ascii_alphanumeric() { byte } else { b'_' };
                self.len += 1;
            }
        }
        Ok(())
    }
}

/// The byte spans of one clean frame (HQ #346): for each byte, the sum of the seven
/// differences between its adjacent rising-edge timestamps, each mod 2^16, so a gap between
/// bytes or frames never enters a span. `stamps` holds the little-endian u16 timestamps.
fn byte_spans(stamps: &[u8], summary: &mut Summary) {
    for byte in 0..frozen::TRANSFER_LEN {
        let mut span = 0u32;
        for bit in 0..7 {
            let at = 2 * (8 * byte + bit);
            let earlier = u16::from_le_bytes([stamps[at], stamps[at + 1]]);
            let later = u16::from_le_bytes([stamps[at + 2], stamps[at + 3]]);
            span += later.wrapping_sub(earlier) as u32;
        }
        summary.add_span(span);
    }
}

/// `write` sends the frame through the driver and says whether the driver reported success.
/// Every transfer is captured and counted; one XSUM record summarises them afterwards.
fn transfers(hq: &mut Hq, sequence: &str, position: i32, case: &Case,
             write: &mut dyn FnMut(&[u8]) -> bool) {
    let mut payload = [0u8; frozen::TRANSFER_LEN];
    let mut summary = Summary::new();
    for transfer in 0..frozen::TRANSFERS {
        let seed = report::transfer_seed(sequence, position, case.id, transfer);
        report::payload(seed, &mut payload);
        // The capture-path configuration of the divider actually in force (the BR field of
        // CR1, read back from the hardware).
        let br = ((spi_cr1() >> 3) & 7) as usize;
        let cap = &frozen::CAPTURE_PATH[br];
        let (result, driver_ok) = icap::capture_frame(cap, || write(&payload));
        if summary.count(br, seed, &result, driver_ok) {
            byte_spans(icap::frame_stamps(), &mut summary);
        }
    }
    hq.xsum(sequence, position, case.id, &summary);
}

#[allow(clippy::too_many_arguments)]
fn occurrence(hq: &mut Hq, sequence: &str, position: i32, case: &Case, path: &str, outcome: &str,
              error: &str, reported_hz: u32, write: &mut dyn FnMut(&[u8]) -> bool) {
    hq.occ(sequence, position, case.id, path, case.f_req, outcome, spi_cr1(), reported_hz, error);
    transfers(hq, sequence, position, case, write);
    hq.occurrence_done();
}

/// One construction of the driver at the start case, then every entry applied with `set_config`.
pub fn run_set_config(hq: &mut Hq, p: &mut Peripherals, name: &'static str, entries: &[usize]) {
    let start = &frozen::CASES[frozen::START_CASE];
    report::set_context(name, -1, start.id);
    let mut spi = Spi::new_blocking_txonly(
        p.SPI2.reborrow(), p.PB10.reborrow(), p.PB15.reborrow(), config_for(start.f_req),
    );
    let reported = spi.get_current_config().frequency.0;
    occurrence(hq, name, -1, start, "construct", "accepted", "none", reported, &mut |data| {
        spi.blocking_write(data).is_ok()
    });
    for (index, &case_index) in entries.iter().enumerate() {
        let case = &frozen::CASES[case_index];
        let position = index as i32;
        report::set_context(name, position, case.id);
        let (outcome, error) = match spi.set_config(&config_for(case.f_req)) {
            Ok(()) => ("accepted", ErrorName::text("none")),
            Err(error) => ("rejected", ErrorName::of(&error)),
        };
        let reported = spi.get_current_config().frequency.0;
        occurrence(hq, name, position, case, "set_config", outcome, error.as_str(), reported,
                   &mut |data| spi.blocking_write(data).is_ok());
    }
}

/// A fresh construction per case. The last case asks for a frequency above the kernel clock,
/// where the pinned constructor panics; the panic handler records it and resets the chip.
pub fn run_construct(hq: &mut Hq, p: &mut Peripherals, entries: &[usize]) {
    for (index, &case_index) in entries.iter().enumerate() {
        let case = &frozen::CASES[case_index];
        let position = index as i32;
        report::set_context("construct", position, case.id);
        let mut spi = Spi::new_blocking_txonly(
            p.SPI2.reborrow(), p.PB10.reborrow(), p.PB15.reborrow(), config_for(case.f_req),
        );
        let reported = spi.get_current_config().frequency.0;
        occurrence(hq, "construct", position, case, "construct", "accepted", "none", reported,
                   &mut |data| spi.blocking_write(data).is_ok());
    }
}

/// The occurrence after the deliberate panic: one construction at the recovery case.
pub fn run_recovery(hq: &mut Hq, p: &mut Peripherals) {
    let case = &frozen::CASES[frozen::RECOVERY_CASE];
    report::set_context("recovery", 0, case.id);
    let mut spi = Spi::new_blocking_txonly(
        p.SPI2.reborrow(), p.PB10.reborrow(), p.PB15.reborrow(), config_for(case.f_req),
    );
    let reported = spi.get_current_config().frequency.0;
    occurrence(hq, "recovery", 0, case, "construct", "accepted", "none", reported, &mut |data| {
        spi.blocking_write(data).is_ok()
    });
}
