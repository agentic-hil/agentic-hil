#![no_std]
#![no_main]

use core::sync::atomic::{AtomicU32, Ordering};
use cortex_m_rt::entry;
use cortex_m as _;
use defmt_rtt as _;
use panic_halt as _;

static COUNT: AtomicU32 = AtomicU32::new(0);

#[cfg(feature = "ts")]
defmt::timestamp!("{=u64:us}", COUNT.load(Ordering::Relaxed) as u64);

#[derive(defmt::Format)]
struct Reading {
    channel: u8,
    millivolts: i32,
}

#[derive(defmt::Format)]
enum State {
    Idle,
    Running { speed: u16 },
    Fault(u8),
}

struct Manual(u8, u8);

impl defmt::Format for Manual {
    fn format(&self, f: defmt::Formatter) {
        defmt::write!(f, "Manual(");
        defmt::write!(f, "{=u8}, {=u8:x}", self.0, self.1);
        defmt::write!(f, ")");
    }
}

defmt::bitflags! {
    struct Flags: u8 {
        const READY = 1 << 0;
        const BUSY = 1 << 1;
        const ERROR = 1 << 7;
    }
}

#[inline(never)]
fn v<T>(x: T) -> T {
    core::hint::black_box(x)
}

#[entry]
fn main() -> ! {
    loop {
        COUNT.fetch_add(1, Ordering::Relaxed);
        defmt::info!("Hello, world!");
        defmt::debug!("unsigned {=u8} {=u16} {=u32} {=u64} {=u128}", v(1u8), v(2u16), v(3u32), v(4u64), v(5u128));
        defmt::trace!("signed {=i8} {=i16} {=i32} {=i64} {=i128}", v(-1i8), v(-2i16), v(-3i32), v(-4i64), v(-5i128));
        defmt::warn!("sizes {=usize} {=isize}", v(7usize), v(-8isize));
        defmt::error!("floats {=f32} {=f64}", v(1.5f32), v(-0.25f64));
        defmt::info!("bool {=bool} char {=char}", v(true), v('x'));
        defmt::info!("str {=str} istr {=istr}", v("dynamic"), defmt::intern!("interned"));
        defmt::info!("bytes {=[u8]} array {=[u8; 4]}", v(&[1u8, 2, 3][..]), v([9u8, 8, 7, 6]));
        defmt::info!("hex {=u32:x} HEX {=u32:#X} bin {=u8:#010b} oct {=u16:o} pad {=u16:05} sx {=i8:x}", v(0xbeefu32), v(0xbeefu32), v(5u8), v(8u16), v(42u16), v(-1i8));
        defmt::info!("ascii {=[u8]:a}", v(&b"hi\n\x01"[..]));
        defmt::info!("time {=u64:tus} secs {=u32:ms} tms {=u32:tms} ts {=u32:ts}", v(3_723_000_001u64), v(1234u32), v(90061001u32), v(90061u32));
        defmt::info!("dbg {=str:?}", v("q\"uote"));
        defmt::info!("struct {} enum {} {} {}", v(Reading { channel: 2, millivolts: -1200 }), v(State::Idle), v(State::Running { speed: 300 }), v(State::Fault(4)));
        defmt::info!("option {} {} result {}", v(Some(5u8)), v(None::<u8>), v(Ok::<u16, i8>(9)));
        defmt::info!("slice {=[?]} array {=[?; 3]}", v(&[1u16, 2][..]), v([State::Idle, State::Fault(1), State::Running { speed: 2 }]));
        defmt::info!("manual {}", v(Manual(3, 255)));
        defmt::info!("flags {}", v(Flags::READY | Flags::ERROR));
        defmt::info!("bitfields {0=0..4} {0=4..8} {0=8..16:x} {0=12..16:a} next {1=u16:#x}", v(0xab5cu16), v(0x4142u16));
        defmt::info!("wide bits {0=20..24} {0=1..2}", v(0x00f0_0002u32));
        defmt::info!("display {} debug {}", defmt::Display2Format(&v(17u32)), defmt::Debug2Format(&v("dbg")));
        defmt::info!("indexed {1=u8} {0=u8} {1=u8:x}", v(10u8), v(11u8));
        defmt::info!("prim format {} {} {}", v(42u8), v(-3i32), v(2.5f32));
        defmt::info!("floats {=f32} {=f32} {=f32} {=f32} {=f64} {=f64}", v(1.0e-7f32), v(f32::NAN), v(f32::INFINITY), v(123456789.0f32), v(1.0e300f64), v(0.1f64));
        defmt::info!("escaped {{braces}} {=u8}", v(1u8));
        defmt::info!("iso {=u64:iso8601ms}", v(1_700_000_000_123u64));
        defmt::info!("char {=char} {=char}", v('ä'), v('🦀'));
        defmt::info!("tuple {}", v((1u8, -2i16)));
        defmt::println!("println {=u8}", v(99u8));
        defmt::info!("nested {}", v(Some(Some(State::Fault(2)))));
        defmt::info!("empty slice {=[?]} empty bytes {=[u8]}", v(&[] as &[u8]), v(&[] as &[u8]));
        defmt::info!("str array {=[?]}", v(&["a", "bc"][..]));
        defmt::info!("hexbytes {=[u8]:x} {=[u8; 2]:#04x}", v(&[0xau8, 0xff][..]), v([1u8, 0x20]));
    }
}
