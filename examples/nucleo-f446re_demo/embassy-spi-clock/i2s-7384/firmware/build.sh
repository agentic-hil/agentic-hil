#!/usr/bin/env bash
# Builds one variant: build.sh <base|before|after>. Writes build-<variant>.json next to this script.
# Expects the Embassy revision of the variant checked out at C:/t/e7384-<variant> (Git Bash on Windows).
set -euo pipefail
v=$1
here=$(cd "$(dirname "$0")" && pwd -W)
here_win=$(cygpath -w "$here")
cargo_home=$(cygpath -m "${CARGO_HOME:-$USERPROFILE/.cargo}")
cargo_home_win=$(cygpath -w "$cargo_home")
tree=C:/t/e7384-$v
sha=$(git -C "$tree" rev-parse HEAD)
sep=$'\x1f'
flags="--remap-path-prefix=C:\\t\\e7384-$v=/embassy${sep}--remap-path-prefix=C:/t/e7384-$v=/embassy"
flags+="${sep}--remap-path-prefix=$cargo_home_win=/cargo${sep}--remap-path-prefix=$cargo_home=/cargo"
flags+="${sep}--remap-path-prefix=$here_win=/fw${sep}--remap-path-prefix=$here=/fw"
cd "$here/variants/$v"
export I2S7384_EMBASSY=${sha:0:12} CARGO_TARGET_DIR=C:/t/i2s-$v CARGO_ENCODED_RUSTFLAGS="$flags"
cargo +1.97 build --release --target thumbv7em-none-eabi 2>&1 | tail -25
elf=C:/t/i2s-$v/thumbv7em-none-eabi/release/i2s7384-$v
python -I - "$elf" "$v" "$sha" "$here/build-$v.json" <<'PY'
import hashlib, json, subprocess, sys
elf, variant, sha, out = sys.argv[1:]
data = open(elf, "rb").read()
json.dump({"variant": variant, "embassy_commit": sha, "elf": elf, "bytes": len(data),
           "sha256": hashlib.sha256(data).hexdigest(),
           "rustc": subprocess.run(["rustc", "+1.97", "-V"], capture_output=True, text=True).stdout.strip(),
           "command": "cargo +1.97 build --release --target thumbv7em-none-eabi"},
          open(out, "w"), indent=1)
print(variant, len(data), hashlib.sha256(data).hexdigest())
PY
