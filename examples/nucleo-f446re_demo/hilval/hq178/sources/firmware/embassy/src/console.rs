//! The USART2 console, the record format, and the two helpers every module needs.
//!
//! Every line is `HQ178 <kind> key=value ...`; values are printable ASCII
//! without spaces or `=`, decimals without a leading zero (the gate reads them
//! with Python's `int(value, 0)`, which rejects `010`) and hexadecimal with a
//! `0x` prefix. The image prints raw measurements only: no line says whether a
//! value is right.

use core::cell::UnsafeCell;
use core::fmt::Write as _;

use embassy_stm32::mode::Blocking;
use embassy_stm32::usart::Uart;

/// The tag the bench's plan waits for and the gate parses.
pub const TAG: &str = "HQ178";

/// A buffer that lives for the whole program instead of inside the task future.
///
/// The image is one task that runs one sequence at a time, so exactly one
/// reference to each buffer exists at any moment; nothing here is shared with
/// an interrupt handler.
pub struct Static<T>(UnsafeCell<T>);

// SAFETY: the image is single-threaded and no handler touches these buffers.
unsafe impl<T> Sync for Static<T> {}

impl<T> Static<T> {
    pub const fn new(value: T) -> Self {
        Self(UnsafeCell::new(value))
    }

    /// The one reference to this buffer (see the type's invariant).
    #[allow(clippy::mut_from_ref)]
    pub fn get(&self) -> &mut T {
        // SAFETY: the invariant above; the caller holds no other reference.
        unsafe { &mut *self.0.get() }
    }

    /// A read-only view that lives as long as the buffer does.
    pub fn shared(&self) -> &T {
        // SAFETY: as above; a shared view is handed out only to readers.
        unsafe { &*self.0.get() }
    }

    /// The buffer's address, for the volatile accesses of a `.uninit` marker.
    pub fn pointer(&self) -> *mut T {
        self.0.get()
    }
}

/// USART2 (PA2 TX, PA3 RX, 115200 8N1) is the ST-LINK's virtual COM port.
pub struct Console<'d>(Uart<'d, Blocking>);

impl core::fmt::Write for Console<'_> {
    fn write_str(&mut self, text: &str) -> core::fmt::Result {
        self.0.blocking_write(text.as_bytes()).map_err(|_| core::fmt::Error)
    }
}

impl<'d> Console<'d> {
    pub fn new(uart: Uart<'d, Blocking>) -> Self {
        Self(uart)
    }

    /// The empty line every start prints before its first record.
    pub fn blank(&mut self) {
        let _ = write!(self, "\r\n");
    }

    pub fn begin(&mut self, kind: &str) {
        let _ = write!(self, "{} {}", TAG, kind);
    }

    pub fn end(&mut self) {
        let _ = write!(self, "\r\n");
    }

    /// One whole record with no fields, for `done`.
    pub fn bare(&mut self, kind: &str) {
        self.begin(kind);
        self.end();
    }

    pub fn u(&mut self, key: &str, value: u32) {
        let _ = write!(self, " {}={}", key, value);
    }

    pub fn i(&mut self, key: &str, value: i32) {
        let _ = write!(self, " {}={}", key, value);
    }

    pub fn x(&mut self, key: &str, value: u32) {
        let _ = write!(self, " {}=0x{:x}", key, value);
    }

    pub fn s(&mut self, key: &str, value: &str) {
        let _ = write!(self, " {}={}", key, value);
    }

    /// A word list as fixed-width lower-case hex without separators, `-` for none.
    ///
    /// The width is the reader's own word width (four digits for the `u16`
    /// readers at 8E1, two otherwise), so the gate can split the field back
    /// into words without guessing.
    pub fn words(&mut self, key: &str, values: &[u16], width: usize) {
        let _ = write!(self, " {}=", key);
        if values.is_empty() {
            let _ = write!(self, "-");
            return;
        }
        for value in values {
            if width == 4 {
                let _ = write!(self, "{:04x}", value);
            } else {
                let _ = write!(self, "{:02x}", value & 0xFF);
            }
        }
    }

    pub fn flush(&mut self) {
        let _ = self.0.blocking_flush();
    }
}

/// The call results of one sequence, run-length encoded as the gate reads them.
///
/// `o` is Ok, `p` `f` `n` `v` `b` are the five `Error` variants and `t` is a
/// call that timed out. A full sequence makes one call per word, so the letters
/// are counted rather than listed: `o258t1`.
pub struct ResString {
    buffer: [u8; 64],
    length: usize,
    letter: u8,
    run: u32,
    calls: u32,
}

impl ResString {
    pub const fn new() -> Self {
        Self { buffer: [0; 64], length: 0, letter: 0, run: 0, calls: 0 }
    }

    pub fn reset(&mut self) {
        self.length = 0;
        self.letter = 0;
        self.run = 0;
        self.calls = 0;
    }

    pub fn push(&mut self, letter: u8) {
        self.calls += 1;
        if self.run > 0 && self.letter == letter {
            self.run += 1;
            return;
        }
        self.seal();
        self.letter = letter;
        self.run = 1;
    }

    /// Writes the run that is still open; idempotent, so printing is safe.
    pub fn seal(&mut self) {
        if self.run == 0 {
            return;
        }
        self.put(self.letter);
        let mut digits = [0u8; 10];
        let mut rest = self.run;
        let mut at = 0;
        while rest > 0 {
            digits[at] = b'0' + (rest % 10) as u8;
            rest /= 10;
            at += 1;
        }
        while at > 0 {
            at -= 1;
            self.put(digits[at]);
        }
        self.run = 0;
    }

    fn put(&mut self, byte: u8) {
        if self.length < self.buffer.len() {
            self.buffer[self.length] = byte;
            self.length += 1;
        }
    }

    pub fn as_str(&self) -> &str {
        // Only `[opfnvbt0-9]` is ever written, so this cannot fail.
        core::str::from_utf8(&self.buffer[..self.length]).unwrap_or("utf8")
    }

    pub fn calls(&self) -> u32 {
        self.calls
    }
}
