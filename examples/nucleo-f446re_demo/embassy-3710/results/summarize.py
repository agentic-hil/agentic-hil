"""Reproduce the result tables of the README from the serial logs of a run.

Usage:

    python3 summarize.py baseline=<path to the baseline image's dut_uart.jsonl> \
                         patched=<path to the patched image's dut_uart.jsonl>

Each log is the serial recording Agentic HIL made of one image: JSON lines whose
`rx` events carry the bytes the board sent, as hex. The script joins those bytes,
keeps the lines tagged `HQ175 ` (the tag of the validation job that produced the
images), prints the `boot`, `setup`, `done` and `fault` lines as they are and, for
the `pwm` records, prints one row per sequence, step and pull with every field as
a single value where all repetitions agree, or as `min..max (k distinct)` where
they do not. Register fields are printed in hexadecimal.

For every `trace` record group (the raw sample buffer of one measurement, sent in
parts) the script unpacks the 1908 samples, counts them again and compares its own
counts with the `h1`, `h1n`, `both`, `e1` and `e1n` the image printed for the same
step, so the image's counting can be checked against the samples themselves.
"""
import json
import sys
from collections import Counter

TAG = "HQ175 "
HEX_FIELDS = ("ccer0", "ccer", "bdtr", "flags", "cr1", "cr2", "rcc_cfgr")
# Fields that stay text even when they look like a number (an all-zero trace part would
# otherwise parse as the integer 0).
TEXT_FIELDS = ("image", "seq", "step", "kind", "data")
SKIP = ("seq", "rep", "step", "pull")
SAMPLES = 1908


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
            if k in TEXT_FIELDS:
                fields[k] = v
                continue
            try:
                fields[k] = int(v, 0)
            except ValueError:
                fields[k] = v
    return kind, fields


def show(values, as_hex=False):
    vals = [v for v in values if v is not None]
    if not vals:
        return "-"
    if as_hex:
        vals = [hex(v) if isinstance(v, int) else v for v in vals]
    distinct = sorted(set(vals), key=str)
    if len(distinct) == 1:
        return str(distinct[0])
    if all(isinstance(v, int) for v in vals):
        return f"{min(vals)}..{max(vals)} ({len(distinct)} distinct)"
    return " ".join(f"{v}x{n}" for v, n in sorted(Counter(vals).items(), key=lambda kv: str(kv[0])))


def unpack(data_hex):
    """The 1908 samples of a trace: two bits each, bit 0 is PA8 (CH1), bit 1 is PA7 (CH1N)."""
    buf = bytes.fromhex(data_hex)
    samples = []
    for b in buf:
        for i in range(4):
            samples.append((b >> (2 * i)) & 3)
    return samples[:SAMPLES]


def recount(samples):
    ch1 = [s & 1 for s in samples]
    ch1n = [(s >> 1) & 1 for s in samples]
    edges = lambda xs: sum(1 for a, b in zip(xs, xs[1:]) if a != b)
    return {
        "h1": sum(ch1),
        "h1n": sum(ch1n),
        "both": sum(1 for a, b in zip(ch1, ch1n) if a and b),
        "e1": edges(ch1),
        "e1n": edges(ch1n),
    }


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
        pwm = []
        traces = {}
        kinds = Counter()
        for ln in tagged:
            kind, f = parse(ln)
            kinds[kind] += 1
            if kind in ("boot", "setup", "done", "fault"):
                print(" ", ln[:400])
            elif kind == "pwm":
                pwm.append(f)
            elif kind == "trace":
                key = (f["seq"], f["rep"], f["step"])
                traces.setdefault(key, {})[f["part"]] = f["data"]
            else:
                print("  other kind:", ln[:200])
        print("  kinds:", dict(kinds))
        if not pwm:
            continue
        keys = []
        for r in pwm:
            for k in r:
                if k not in keys:
                    keys.append(k)
        seqs = []
        for r in pwm:
            if r["seq"] not in seqs:
                seqs.append(r["seq"])
        for seq in seqs:
            rs = [r for r in pwm if r["seq"] == seq]
            reps = sorted(set(r["rep"] for r in rs))
            steps = []
            for r in rs:
                if r["step"] not in steps:
                    steps.append(r["step"])
            per_rep = Counter(r["rep"] for r in rs)
            print(
                f"  --- seq={seq}: {len(rs)} pwm records, rep {reps[0]}..{reps[-1]} "
                f"({len(reps)} distinct, {show(list(per_rep.values()))} records per rep), steps {steps}"
            )
            for step in steps:
                for pull in (0, 1):
                    ss = [r for r in rs if r["step"] == step and r.get("pull") == pull]
                    if not ss:
                        continue
                    cells = []
                    for k in keys:
                        if k in SKIP:
                            continue
                        cells.append(f"{k}={show([r.get(k) for r in ss], k in HEX_FIELDS)}")
                    print(f"    step={step} pull={pull} ({len(ss)}): " + " ".join(cells))
        if traces:
            print(f"  --- traces: {len(traces)} measurements")
            by_key = {(r["seq"], r["rep"], r["step"]): r for r in pwm}
            for key in sorted(traces, key=lambda k: (seqs.index(k[0]), k[1], k[2])):
                parts = traces[key]
                data = "".join(parts[i] for i in sorted(parts))
                samples = unpack(data)
                counts = recount(samples)
                rec = by_key.get(key)
                if rec is None:
                    verdict = "no pwm record"
                else:
                    diffs = {k: (counts[k], rec.get(k)) for k in counts if rec.get(k) != counts[k]}
                    verdict = "matches the record" if not diffs else f"DIFFERS from the record: {diffs}"
                print(
                    f"    {key[0]} rep {key[1]} {key[2]}: {len(parts)} parts, {len(data) // 2} bytes, "
                    f"{len(samples)} samples; recount {counts}; {verdict}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
