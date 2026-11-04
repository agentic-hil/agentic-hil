# Use case: read defmt logs as frames, not as text

**The problem.** Rust firmware on Cortex-M usually logs with
[defmt](https://defmt.ferrous-systems.com/). defmt keeps every format string
out of the image: the board sends a 16-bit index into a table and the raw
arguments, and the table lives in the ELF the firmware was linked into. On the
wire that is binary. The usual way to read it is a host tool (`defmt-print`,
`probe-rs`) that joins the two back together and prints text, which a test then
greps. A detour through text loses what defmt already knows: which statement
logged, at which level, and the typed values it logged.

**What Agentic Hardware-in-the-Loop (Agentic HIL) does instead.** A COM session
opened with `defmt` decodes the line itself. `com_read` hands out whole frames,
each with its level, timestamp, format string, typed arguments, the rendered
message and the line `defmt-print` would print. A test plan waits for a frame
by what it is, with `defmt_expect`, not by a regular expression over text. No
host tool runs, and nothing is converted to text and parsed back.

## The table comes from the ELF that was flashed

A frame decoded with another build's table reads as plausible messages the
firmware never sent, so the table is taken from the ELF this server flashed,
and only while that file is byte for byte what was flashed:

| `defmt` | Table | `proven` |
|---|---|---|
| `true` | the `.elf` the last confirmed `flash_firmware` wrote, digest checked again | `true` |
| `{"image_path": "build/app.elf"}` | that ELF, validated like a flash image (inside the workspace, allowed roots and extensions) | `true` only if it is the flashed file |

Without a flashed ELF the session is refused with `defmt_table_not_available`;
an ELF rebuilt since the flash with `defmt_table_changed`; an image without a
usable table (a `.bin`, a firmware linked without `-Tdefmt.x`, a wire format
other than defmt 0.3 and 1.x) with `defmt_table_invalid` and its `reason`. All
three refuse before the port is opened.

## One call: flash, reset, read the decoded boot log

```json
{"name": "flash_firmware", "arguments": {
  "image_path": "build/app.elf",
  "reset_after_flash": true,
  "capture": {"port_id": "dut_uart", "defmt": true, "until": "INFO boot complete"}
}}
```

`capture.frames` holds the decoded boot output, through the first frame whose
line contains `until`. The image is read for its table before anything is
flashed, so an image without one flashes nothing. Cargo writes the ELF without
an extension (`target/thumbv7em-none-eabihf/release/app`); copy it to a name
ending in `.elf` first, because `flash_firmware` takes `.elf`, `.hex` and
`.bin`, and only a flashed `.elf` becomes the table `defmt: true` reads.

## Sessions

```json
{"name": "com_session_start", "arguments": {"port_id": "dut_uart", "defmt": true}}
{"name": "com_read", "arguments": {"port_id": "dut_uart", "until": "ERROR", "wait_timeout_s": 5}}
```

A frame in `frames`:

```json
{"index": 18, "level": "info", "timestamp": "2.000006", "timestamp_value": 2000006,
 "format": "temperature {=f32} C, channel {=u8}", "args": [21.5, 2],
 "message": "temperature 21.5 C, channel 2",
 "text": "2.000006 INFO temperature 21.5 C, channel 2"}
```

- Bytes leave the buffer only up to the end of the last complete frame, so a
  frame split across two reads is decoded once, by the read that sees its end.
- `until` matches a frame's `text` literally; the frames after the match stay
  buffered.
- A read hands out at most 256 records; `defmt.more_frames_buffered` says when
  more are waiting.
- Bytes that do not decode are records with `error` (`unknown_index`,
  `truncated`, `malformed`, `rzcobs_invalid`, `oversized_frame`) and their
  bytes, in stream order, never skipped. Text on the line (a bootloader banner)
  shows as `raw_text`. The first record a session hands out can be the tail of
  a frame the board was sending when the port opened, and says so with
  `at_session_start`.
- `unknown_index` almost always means the board runs another build than the
  table describes, and the answer's `defmt.advice` says so.

Arguments are JSON values in the order the firmware passed them: integers,
floats (as they print, so `0.1f32` is `0.1`; `NaN` and `inf` as strings),
booleans, strings, byte slices as lists of numbers. A value formatted through
its own `Format` impl keeps its format string: `{"format": "Fault({=u8})",
"args": [4], "text": "Fault(4)"}`. A primitive formatted with `{}` is just the
value.

## Plans

Plan format version 7 adds `defmt:` on `uart_open` and the `defmt_expect`
action:

```yaml
version: 7
name: defmt-boot
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut_uart, action: uart_open, defmt: true}
  - {device: dut, action: reset}
  - {device: dut_uart, action: defmt_expect, level: info, text: "boot complete", timeout_s: 10}
  - device: dut_uart
    action: defmt_expect
    format: "temperature {=f32} C, channel {=u8}"
    args: [{min: 15.0, max: 35.0}, {equals: 2}]
    timeout_s: 5
```

Every key a `defmt_expect` gives holds for one and the same frame: `level`
exactly, `format` exactly as written in the firmware, `text` or `pattern`
against the rendered message, and `args` by position (`equals`, or `min` and
`max`; `null` claims nothing). Frames after the matching one stay for the next
step, so two expectations met by one burst of output are both met. A step that
times out fails with `defmt_expect_timeout` and the last frames the line did
send; frames that could not be decoded are counted on both outcomes.
`uart_read` and `uart_expect` on a line opened with `defmt:`, and
`defmt_expect` on one opened without it, are refused before the run.

## What the decoder covers

The decoder is plain Python with no Rust or host tool behind it. It reads
defmt wire versions 3 and 4 (defmt 0.3 and 1.x), both encodings (`rzcobs`, the
default, and `raw`), every parameter type and display hint, derived `Format`
enums and structs, hand-written `Format` impls, `bitflags!`, `Display2Format`
and `Debug2Format`. It is held to the official `defmt-decoder` 1.2 on a real
cross-compiled image: `tools/defmt_reference.py` records what that decoder
printed for every statement of `tests/fixtures/defmt/firmware`, and the suite
requires the same lines from the same bytes.

Not covered: source locations (they come from DWARF, which a build with
`strip = "debuginfo"` does not carry either), CBOR diagnostic notation for the
`:cbor` hint (the bytes are shown), and transports other than a serial line.
defmt over RTT needs an RTT backend, which is a separate observation backend
with its own rules.
