"""Rebuild the defmt fixture images and record what the official decoder says about them.

`agentic_hil.defmt` decodes defmt frames in Python. What makes that decoder
trustworthy is that it agrees with the reference implementation, the Rust
`defmt-decoder` crate that `defmt-print` and `probe-rs` are built on, on frames
from a real image. This tool produces that evidence under
`tests/fixtures/defmt/`:

1. cross-compiles `firmware/` for `thumbv7em-none-eabihf` twice, once with the
   default rzCOBS encoding and a timestamp, once with the raw encoding and
   none, with `DEFMT_LOG=trace` so every statement is kept. Nothing is run: the
   images are only read for their `.defmt` tables;
2. encodes the frames `scenario.py` lists into one stream per image, plus one
   stream of broken frames;
3. decodes each stream with `reference-decoder/`, a twenty-line harness around
   `defmt-decoder`, and writes what it printed to `expected-*.jsonl`.

`tests/test_defmt.py` then requires the Python decoder to print the same lines
for the same bytes, without needing Rust. Run this after changing the firmware,
the scenario or the pinned crate versions, and commit what it writes:

    python tools/defmt_reference.py

It needs `cargo` with the `thumbv7em-none-eabihf` target installed
(`rustup target add thumbv7em-none-eabihf`) and network access to crates.io for
the first build.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "defmt"
TARGET = "thumbv7em-none-eabihf"
# (stream name, cargo features, image file name)
VARIANTS = [("rzcobs", "ts", "defmt-fixture-rzcobs.elf"), ("raw", "raw", "defmt-fixture-raw.elf")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target-dir", help="Cargo target directory; a temporary one by default.")
    args = parser.parse_args(argv)
    cargo = shutil.which("cargo")
    if cargo is None:
        print("cargo is not on PATH", file=sys.stderr)
        return 2
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(FIXTURES)]
    import scenario

    from agentic_hil.defmt import load_table

    with tempfile.TemporaryDirectory(prefix="defmt-reference-") as scratch:
        target_dir = Path(args.target_dir or scratch)
        environment = {**os.environ, "DEFMT_LOG": "trace", "CARGO_TARGET_DIR": str(target_dir / "firmware")}
        for _, features, image in VARIANTS:
            subprocess.run([cargo, "build", "--release", "--locked", "--features", features], cwd=FIXTURES / "firmware", env=environment, check=True)
            shutil.copyfile(target_dir / "firmware" / TARGET / "release" / "defmt-fixture", FIXTURES / image)
        decoder_environment = {**os.environ, "CARGO_TARGET_DIR": str(target_dir / "decoder")}
        subprocess.run([cargo, "build", "--release", "--locked"], cwd=FIXTURES / "reference-decoder", env=decoder_environment, check=True)
        decoder = target_dir / "decoder" / "release" / ("defmt-reference-decoder" + (".exe" if os.name == "nt" else ""))

        streams: list[tuple[str, str, bytes]] = []
        for name, _, image in VARIANTS:
            table = load_table(FIXTURES / image)
            streams.append((name, image, scenario.stream(table)))
        streams.append(("errors", VARIANTS[0][2], scenario.error_stream(load_table(FIXTURES / VARIANTS[0][2]))))
        for name, image, data in streams:
            stream_path = FIXTURES / f"stream-{name}.bin"
            stream_path.write_bytes(data)
            printed = subprocess.run([str(decoder), str(FIXTURES / image), str(stream_path)], capture_output=True, text=True, encoding="utf-8", check=True).stdout
            (FIXTURES / f"expected-{name}.jsonl").write_text(printed, encoding="utf-8", newline="\n")
            print(f"{name}: {len(data)} bytes, {len(printed.splitlines())} records from {image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
