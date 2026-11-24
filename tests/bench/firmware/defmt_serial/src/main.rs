#![no_std]
#![no_main]

use core::convert::Infallible;
use core::mem::MaybeUninit;
use core::ptr::{addr_of_mut, read_volatile, write_volatile};
use core::sync::atomic::{compiler_fence, Ordering};
use cortex_m_rt::entry;
use panic_halt as _;

const HSI_HZ: u32 = 16_000_000;
const BAUD: u32 = 115_200;
const TX_PIN: u32 = 2;
const USART2_AF: u32 = 7;

const RCC_AHB1ENR: *mut u32 = 0x4002_3830 as *mut u32;
const RCC_APB1ENR: *mut u32 = 0x4002_3840 as *mut u32;
const GPIOA_MODER: *mut u32 = 0x4002_0000 as *mut u32;
const GPIOA_AFRL: *mut u32 = 0x4002_0020 as *mut u32;
const USART2_SR: *mut u32 = 0x4000_4400 as *mut u32;
const USART2_DR: *mut u32 = 0x4000_4404 as *mut u32;
const USART2_BRR: *mut u32 = 0x4000_4408 as *mut u32;
const USART2_CR1: *mut u32 = 0x4000_440C as *mut u32;
const USART2_CR2: *mut u32 = 0x4000_4410 as *mut u32;
const USART2_CR3: *mut u32 = 0x4000_4414 as *mut u32;
const SYST_CSR: *mut u32 = 0xE000_E010 as *mut u32;
const SYST_RVR: *mut u32 = 0xE000_E014 as *mut u32;
const SYST_CVR: *mut u32 = 0xE000_E018 as *mut u32;

struct SingleCoreCriticalSection;
critical_section::set_impl!(SingleCoreCriticalSection);

unsafe impl critical_section::Impl for SingleCoreCriticalSection {
    unsafe fn acquire() -> critical_section::RawRestoreState {
        let primask: u32;
        core::arch::asm!("mrs {}, PRIMASK", out(reg) primask, options(nomem, nostack, preserves_flags));
        core::arch::asm!("cpsid i", options(nomem, nostack, preserves_flags));
        compiler_fence(Ordering::SeqCst);
        primask
    }

    unsafe fn release(primask: critical_section::RawRestoreState) {
        compiler_fence(Ordering::SeqCst);
        core::arch::asm!("msr PRIMASK, {}", in(reg) primask, options(nomem, nostack, preserves_flags));
    }
}

struct Usart2Tx;
static mut USART2_TX: MaybeUninit<Usart2Tx> = MaybeUninit::uninit();

impl Usart2Tx {
    fn new() -> Self {
        // The reset clock feeds APB1 at 16 MHz. PA2/AF7 drives the ST-LINK VCP.
        unsafe {
            write_volatile(RCC_AHB1ENR, read_volatile(RCC_AHB1ENR) | 1);
            write_volatile(RCC_APB1ENR, read_volatile(RCC_APB1ENR) | (1 << 17));
            // Read the clock enables back before touching their peripherals.
            read_volatile(RCC_AHB1ENR);
            read_volatile(RCC_APB1ENR);

            let mode_shift = TX_PIN * 2;
            let af_shift = TX_PIN * 4;
            write_volatile(
                GPIOA_AFRL,
                (read_volatile(GPIOA_AFRL) & !(0xf << af_shift)) | (USART2_AF << af_shift),
            );
            write_volatile(
                GPIOA_MODER,
                (read_volatile(GPIOA_MODER) & !(0x3 << mode_shift)) | (0x2 << mode_shift),
            );

            // Eight data bits, no parity, one stop bit and oversampling by 16.
            write_volatile(USART2_CR1, 0);
            write_volatile(USART2_CR2, 0);
            write_volatile(USART2_CR3, 0);
            write_volatile(USART2_BRR, (HSI_HZ + BAUD / 2) / BAUD);
            write_volatile(USART2_CR1, (1 << 13) | (1 << 3));
        }
        Self
    }
}

impl embedded_io::ErrorType for Usart2Tx {
    type Error = Infallible;
}

impl embedded_io::Write for Usart2Tx {
    fn write(&mut self, bytes: &[u8]) -> Result<usize, Self::Error> {
        for &byte in bytes {
            unsafe {
                while read_volatile(USART2_SR) & (1 << 7) == 0 {
                    core::hint::spin_loop();
                }
                write_volatile(USART2_DR, u32::from(byte));
            }
        }
        Ok(bytes.len())
    }

    fn flush(&mut self) -> Result<(), Self::Error> {
        while unsafe { read_volatile(USART2_SR) } & (1 << 6) == 0 {
            core::hint::spin_loop();
        }
        Ok(())
    }
}

fn wait_for_next_sample() {
    unsafe {
        write_volatile(SYST_RVR, HSI_HZ / 10 - 1);
        write_volatile(SYST_CVR, 0);
        write_volatile(SYST_CSR, (1 << 2) | 1);
        while read_volatile(SYST_CSR) & (1 << 16) == 0 {
            core::hint::spin_loop();
        }
        write_volatile(SYST_CSR, 0);
    }
}

#[entry]
fn main() -> ! {
    // The entry point initializes the writer once and gives the logger exclusive access.
    let tx = unsafe { (*addr_of_mut!(USART2_TX)).write(Usart2Tx::new()) };
    defmt_serial::defmt_serial(tx);
    defmt::info!("F446RE boot");
    loop {
        wait_for_next_sample();
        defmt::info!("F446RE sample {=u8} value {=u16}", 2u8, 37u16);
    }
}
