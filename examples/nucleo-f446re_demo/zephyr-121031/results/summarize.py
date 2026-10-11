"""Reproduce the result tables of the README from the serial logs of a run.

Usage:

    python3 summarize.py head=<path to the head image's dut_uart.jsonl> \
                         base=<path to the base image's dut_uart.jsonl>

Each log is the serial recording Agentic HIL made of one image: JSON lines whose
`rx` events carry the bytes the board sent, as hex. The script joins those bytes,
keeps the lines tagged `HQ172 ` (the tag of the validation job that produced the
images), prints the `build`, `cts` and `done` lines as they are and, for the
`rep` records, prints every field per group (`cts=released`, `cts=held`) as
min/median/max over the repetitions, or as a count per value where there are at
most four distinct values. Register fields are printed in hexadecimal. The
derived rows at the end of each group are the differences the README discusses.
"""
import json
import statistics
import sys
from collections import Counter

TAG = "HQ172 "


def load_lines(path):
    data = bytearray()
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            ev = json.loads(raw)
            if ev.get("direction") == "rx" and "hex" in ev:
                data += bytes.fromhex(ev["hex"])
    text = data.decode("ascii", errors="replace")
    return [ln.rstrip("\r") for ln in text.split("\n")]


def parse(line):
    parts = line.split()
    kind = parts[1]
    fields = {}
    for p in parts[2:]:
        if "=" in p:
            k, v = p.split("=", 1)
            try:
                fields[k] = int(v, 0)
            except ValueError:
                fields[k] = v
    return kind, fields


def stats(values, as_hex=False):
    vals = [v for v in values if v is not None]
    if not vals:
        return "-"
    if as_hex:
        return dict(Counter(hex(v) if isinstance(v, int) else v for v in vals))
    if not all(isinstance(v, int) for v in vals):
        return dict(Counter(vals))
    distinct = sorted(set(vals))
    if len(distinct) <= 4:
        return dict(sorted(Counter(vals).items()))
    vals.sort()
    return (
        f"min {vals[0]} med {int(statistics.median(vals))} max {vals[-1]} "
        f"(n={len(vals)}, distinct {len(distinct)})"
    )


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for arg in sys.argv[1:]:
        name, path = arg.split("=", 1) if "=" in arg else (arg, arg)
        lines = load_lines(path)
        tagged = [ln for ln in lines if ln.startswith(TAG)]
        untagged = [ln for ln in lines if ln and not ln.startswith(TAG)]
        print(f"===== {name}: {len(tagged)} tagged lines, {len(untagged)} untagged non-empty lines")
        for u in untagged[:5]:
            print("  untagged:", u[:160])
        reps = []
        for ln in tagged:
            kind, f = parse(ln)
            if kind in ("build", "cts", "done"):
                print(" ", ln[:500])
            elif kind == "rep":
                reps.append(f)
            else:
                print("  other kind:", ln[:200])
        if not reps:
            continue
        ns = [r["n"] for r in reps]
        print(f"  rep lines: {len(reps)}; n {min(ns)}..{max(ns)}; distinct n {len(set(ns))}")
        keys = []
        for r in reps:
            for k in r:
                if k not in keys:
                    keys.append(k)
        for group in ("released", "held"):
            rs = [r for r in reps if r.get("cts") == group]
            print(f"  --- cts={group}: {len(rs)} repetitions")
            for k in keys:
                if k in ("n", "cts"):
                    continue
                vals = [r.get(k) for r in rs]
                as_hex = k.startswith("sr") or k.endswith("_sr") or k in ("cr1", "cr3")
                print(f"    {k}: {stats(vals, as_hex)}")

            def derived(label, fn):
                vals = []
                for r in rs:
                    try:
                        vals.append(fn(r))
                    except (KeyError, TypeError):
                        pass
                print(f"    [{label}]: {stats(vals)}")

            derived("end1 = start1 + dur1", lambda r: r["start1"] + r["dur1"])
            derived("progress - end1", lambda r: r["progress"] - (r["start1"] + r["dur1"]))
            derived("progress - release", lambda r: r["progress"] - r["release"])
            derived("pending - release", lambda r: r["pending"] - r["release"])
            derived("end1 - release", lambda r: r["start1"] + r["dur1"] - r["release"])
            derived("end1 - pending", lambda r: r["start1"] + r["dur1"] - r["pending"])
            derived("start2 - end1", lambda r: r["start2"] - (r["start1"] + r["dur1"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
