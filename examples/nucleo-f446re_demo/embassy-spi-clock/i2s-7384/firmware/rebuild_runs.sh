#!/usr/bin/env bash
# Rebuilds the images of runs 1 to 3 with build.sh and compares each SHA-256 with its record in
# ../builds/run-<n>/build-<variant>.json. Needs what build.sh needs. src/main.rs is the run 3
# source; it is put back at the end.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cp "$here/src/main.rs" "$here/main.rs.run-3"
trap 'mv -f "$here/main.rs.run-3" "$here/src/main.rs"; rm -f "$here"/build-base.json "$here"/build-before.json "$here"/build-after.json' EXIT
sha() { python -c 'import json, sys; print(json.load(open(sys.argv[1]))["sha256"])' "$1"; }
mismatches=0
for run in 1 2 3; do
  if [ "$run" = 3 ]; then source="$here/main.rs.run-3"; else source="$here/runs/run-$run/main.rs"; fi
  cp "$source" "$here/src/main.rs"
  for variant in base before after; do
    if ! output=$(bash "$here/build.sh" "$variant" 2>&1); then
      echo "$output"
      exit 1
    fi
    built=$(sha "$here/build-$variant.json")
    recorded=$(sha "$here/../builds/run-$run/build-$variant.json")
    if [ "$built" = "$recorded" ]; then
      echo "run-$run $variant matches $built"
    else
      echo "run-$run $variant DIFFERS: built $built, recorded $recorded"
      mismatches=$((mismatches + 1))
    fi
  done
done
[ "$mismatches" = 0 ]
