"""Reproduce the result tables of the README from the serial logs of a run.

Usage:

    python3 summarize.py baseline=<path to the baseline image's dut_uart.jsonl> \
                         patched=<path to the patched image's dut_uart.jsonl>

Each log is the serial recording Agentic HIL made of one image: JSON lines whose
`rx` events carry the bytes the board sent, as hex. The script joins those bytes,
keeps the lines tagged `H176 ` (the tag of the validation job that produced the
images) and lists the untagged ones, which are MicroPython's banner and prompt.

A repetition is one `rep` record and the `edges` records that follow it: the
sample indices at which the TX pin changed level, the first sample of each new
level. From those indices, for every configuration of every image, it prints:

* the bit time, as the 8 bit times from the start edge of the first 0x55 frame
  to the falling edge of its last data bit, which is at the same place in every
  frame format;
* the break, the low run between the 13th and 14th edge of a `phase=main`
  repetition (the 11th and 12th of a `phase=sbk` one, which sends no 0x00
  frame), in samples, in bit times and in microseconds (`samples * period /
  timclk`);
* the break as calculated from the configuration's BRR and the boot record's
  PCLK1, for the whole number of bit times the board showed;
* where the return mark `tr` lies against the break, in bit times, and the DWT
  cycles from just before the call to just after it (`cyc`) in microseconds;
* the logic 1 after the break, and every frame of the repetition decoded at the
  middle of its bits, with its parity and stop bits checked.

Nothing here is a threshold or a verdict: the script prints what the records
hold, and the README states what was expected of them.
"""
import json
import statistics
import sys

TAG = "H176 "
MAIN_EDGES = 24  # 0x55 (10 edges), 0x00 (2), break (2), 0x55 (10)
SBK_EDGES = 12  # 0x55 (10), break (2)


def load_lines(path):
    data = bytearray()
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            event = json.loads(raw)
            if event.get("direction") == "rx" and "hex" in event:
                data += bytes.fromhex(event["hex"])
    text = data.decode("ascii", errors="replace")
    return [line.rstrip("\r") for line in text.split("\n") if line.rstrip("\r")]


def parse(line):
    parts = line.split()
    fields = {}
    for part in parts[2:]:
        key, value = part.split("=", 1)
        if key == "i":
            fields[key] = [int(x) for x in value.split(",")]
            continue
        try:
            fields[key] = int(value, 0)
        except ValueError:
            fields[key] = value
    return parts[1], fields


def span(values, digits=2):
    low, high = min(values), max(values)
    if isinstance(low, int) and isinstance(high, int):
        return str(low) if low == high else f"{low}..{high}"
    if f"{low:.{digits}f}" == f"{high:.{digits}f}":
        return f"{low:.{digits}f}"
    return f"{low:.{digits}f}..{high:.{digits}f}"


def distinct(values):
    return ", ".join(str(v) for v in sorted(set(values), key=str))


def level_at(position, first, edges):
    """The pin's level at sample `position`: `first` toggled by every edge at or before it."""
    return first ^ (sum(1 for e in edges if e <= position) & 1)


def decode(start, bit, first, edges, parity, stop):
    """The frame whose start bit begins at `start`, read at the middle of each bit."""
    length = 1 + 8 + (1 if parity == "even" else 0) + stop
    bits = [level_at(start + (k + 0.5) * bit, first, edges) for k in range(length)]
    value = sum(b << k for k, b in enumerate(bits[1:9]))
    good = bits[0] == 0 and all(bits[9 + (1 if parity == "even" else 0):])
    if parity == "even":
        good = good and sum(bits[1:10]) % 2 == 0
    return value, good


def analyse(path):
    lines = load_lines(path)
    untagged = [line for line in lines if not line.startswith(TAG)]
    counts = {}
    boot = None
    done = False
    cfgs, reps, edges = {}, {}, {}
    for line in lines:
        if not line.startswith(TAG):
            continue
        kind, fields = parse(line)
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "boot":
            boot = (line, fields)
        elif kind == "cfg":
            cfgs[fields["id"]] = (line, fields)
        elif kind == "rep":
            reps[(fields["id"], fields["phase"], fields["rep"])] = fields
        elif kind == "edges":
            key = (fields["id"], fields["phase"], fields["rep"])
            edges.setdefault(key, {})[fields["seq"]] = fields["i"]
        elif kind == "done":
            done = True
    rows = {}
    for cid, (_, cfg) in sorted(cfgs.items()):
        sample_us = cfg["period"] / cfg["timclk"] * 1e6
        row = {
            "bit_smp": [], "low_smp": [], "low_bits": [], "low_us": [], "start_tr": [],
            "end_tr": [], "cyc_us": [], "high_bits": [], "frames": [], "sbk_end": [],
            "sbk_seen": [], "checks": [], "n_main": 0, "n_sbk": 0, "rounded": [],
        }
        for (rid, phase, number), rep in sorted(reps.items()):
            if rid != cid:
                continue
            parts = edges.get((rid, phase, number), {})
            idx = [i for seq in sorted(parts) for i in parts[seq]]
            expected = MAIN_EDGES if phase == "main" else SBK_EDGES
            row["checks"].append(
                (rep["n"] == len(idx) == expected, rep["flags"], rep["rem"], rep["first"],
                 rep["moder"], rep["afr"])
            )
            bit = (idx[8] - idx[0]) / 8
            if phase == "main":
                row["n_main"] += 1
                start, end = idx[12], idx[13]
                row["high_bits"].append((idx[14] - end) / bit)
                frame_starts = (idx[0], idx[10], idx[14])
            else:
                row["n_sbk"] += 1
                start, end = idx[10], idx[11]
                row["sbk_end"].append((rep["tsbk"] - end) / bit)
                row["sbk_seen"].append(rep["sbk"])
                frame_starts = (idx[0],)
            low = end - start
            row["bit_smp"].append(bit)
            row["low_smp"].append(low)
            row["low_bits"].append(low / bit)
            row["rounded"].append(round(low / bit))
            row["low_us"].append(low * sample_us)
            row["start_tr"].append((start - rep["tr"]) / bit)
            row["end_tr"].append((end - rep["tr"]) / bit)
            row["cyc_us"].append(rep["cyc"] / boot[1]["sysclk"] * 1e6)
            row["frames"].append(
                tuple(decode(s, bit, rep["first"], idx, cfg["parity"], cfg["stop"]) for s in frame_starts)
            )
        rows[cid] = row
    return {"untagged": untagged, "counts": counts, "boot": boot, "cfgs": cfgs, "rows": rows, "done": done}


def report(image, result):
    boot_line, boot = result["boot"]
    print(f"## {image}\n")
    print("```text")
    print(boot_line)
    for _, (line, _) in sorted(result["cfgs"].items()):
        print(line)
    print("H176 done" if result["done"] else "(no done line)")
    print("```\n")
    counts = ", ".join(f"{k} {v}" for k, v in result["counts"].items())
    print(f"Tagged lines: {counts}. Untagged lines: {len(result['untagged'])}:")
    for line in result["untagged"]:
        print(f"    {line!r}")
    print()
    pclk1 = boot["pclk1"]
    print("| id | baud | format | BRR | reps (main+sbk) | bit, samples | break, samples | break, bit times "
          "| break calculated, us | break observed, us (min / mean / max) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for cid, row in result["rows"].items():
        cfg = result["cfgs"][cid][1]
        fmt = f"8{'E' if cfg['parity'] == 'even' else 'N'}{cfg['stop']}"
        bit_us = cfg["brr"] / pclk1 * 1e6  # OVER8 = 0: baud = PCLK1 / BRR
        rounded = sorted(set(row["rounded"]))
        calculated = "/".join(f"{r * bit_us:.1f}" for r in rounded)
        lows = row["low_us"]
        print(
            f"| {cid} | {cfg['baud']} | {fmt} | 0x{cfg['brr']:04x} | {row['n_main']}+{row['n_sbk']} "
            f"| {span(row['bit_smp'], 3)} | {span(row['low_smp'])} | {span(row['low_bits'], 3)} "
            f"(rounds to {distinct(row['rounded'])}) | {calculated} "
            f"| {min(lows):.1f} / {statistics.fmean(lows):.1f} / {max(lows):.1f} |"
        )
    print()
    print("| id | break starts after the return, bit times | break ends after the return, bit times "
          "| `cyc`, us | logic 1 after the break, bit times | frames decoded (main: 0x55, 0x00, 0x55; sbk: 0x55) "
          "| `CR1.SBK` seen clear, bit times after the break's end |")
    print("|---|---|---|---|---|---|---|")
    for cid, row in result["rows"].items():
        frames = {}
        for group in row["frames"]:
            key = ", ".join(f"0x{v:02x}{'' if ok else ' (bad framing)'}" for v, ok in group)
            frames[key] = frames.get(key, 0) + 1
        frame_text = "; ".join(f"{k} x{n}" for k, n in frames.items())
        sbk = f"{span(row['sbk_end'])} (seen {distinct(row['sbk_seen'])})" if row["sbk_end"] else "-"
        print(
            f"| {cid} | {span(row['start_tr'])} | {span(row['end_tr'])} | {span(row['cyc_us'])} "
            f"| {span(row['high_bits'])} | {frame_text} | {sbk} |"
        )
    print()
    checks = [c for row in result["rows"].values() for c in row["checks"]]
    print(
        f"Per repetition: edge count as expected in {sum(1 for c in checks if c[0])} of {len(checks)}; "
        f"flags {distinct(hex(c[1]) for c in checks)}; rem {distinct(c[2] for c in checks)}; "
        f"first sample {distinct(c[3] for c in checks)}; pin MODER {distinct(c[4] for c in checks)}, "
        f"AF {distinct(c[5] for c in checks)}.\n"
    )


def main(argv):
    images = dict(arg.split("=", 1) for arg in argv)
    results = {image: analyse(path) for image, path in images.items()}
    for image, result in results.items():
        report(image, result)
    if len(results) == 2:
        (a, ra), (b, rb) = results.items()
        same = all(
            ra["rows"][cid]["rounded"] == rb["rows"][cid]["rounded"] for cid in ra["rows"]
        ) and ra["rows"].keys() == rb["rows"].keys()
        print(f"Break length in whole bit times, repetition by repetition: {a} and {b} "
              f"{'agree in every one' if same else 'DIFFER'}.")


if __name__ == "__main__":
    main(sys.argv[1:])
