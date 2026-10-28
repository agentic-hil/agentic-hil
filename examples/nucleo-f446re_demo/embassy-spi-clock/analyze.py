"""Evidence bundle analyzer for HQ #184 (SPI prescaler selection on the NUCLEO-F446RE).

Standard library only. evaluate_bundle only reads the bundle; it never writes. It judges
integrity and provenance first, then decodes the firmware's UART record, derives every
clock in exact Fraction arithmetic, measures the SCK frequency interval from the TIM8_CH2
edge timestamps of the capture path (HQ #342: raw timestamps on XFER records; HQ #346: the
multiset of intra-byte spans on one XSUM record per configuration, with the same interval
rule) and classifies each applied configuration
against both readings of Config.frequency. No classification decides pass or fail: the two
readings are properties of a row, and only integrity, capture and data errors are errors.

Every frozen occurrence gets a row with a status: observed_pass (the measured interval
agrees with the register divider on a capture configuration that passed the ICQUAL of its
own boot), observed_fail, observed_panic or not_observed. evaluation["lifecycle"] states
which parts of the frozen lifecycle were received.

Mode "qualification" judges a setup-only bundle (scope "setup", one run with purpose
"qualification"): the records from BOOT to BLOCK-END n=1 and, per capture configuration,
the highest PWM step the pad sampler reproduces within the frozen rule. It yields no rows.
Hardware mode refuses such a bundle (setup_only_in_hardware_mode). When the frozen inputs
carry "capture_path" (HQ #340), it also judges the ICQUAL records of the TIM8_CH2 capture
path per configuration as observed_pass, observed_fail or not_observed.
"""
import base64
import hashlib
import json
import re
import zlib
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.sax.saxutils import quoteattr

MODES = ("synthetic", "hardware", "qualification")
SCHEMA = "hq184-evaluation/1"
BUNDLE_SCHEMA = "hq184-bundle/1"
EMBASSY_REV = "c70760de42e2c8103138da6ff91626ff5a2688ba"
SEQUENCE_ORDER = (
    "construct", "set_config_ascending", "set_config_descending", "set_config_revisited",
    "recovery",
)
SET_CONFIG_SEQUENCES = SEQUENCE_ORDER[1:4]
# The order in which the firmware runs the sequences (the construct sequence ends the first
# boot with its deliberate panic, so it comes after the set_config sequences).
EXECUTION_ORDER = (
    "set_config_ascending", "set_config_descending", "set_config_revisited", "construct",
    "recovery",
)
# Records of a boot's setup phase, which must precede its first occurrence. ICQUAL (HQ #340,
# the TIM8_CH2 capture path) follows the QUAL records of the pad sampler.
SETUP_KINDS = ("RCC", "CAL", "CALISR", "QUAL", "ICQUAL")
DIVIDERS = (2, 4, 8, 16, 32, 64, 128, 256)
# compute_baud_rate at the pinned commit: (largest quotient of the range, BR code).
PINNED_ARMS = ((2, 0), (5, 1), (11, 2), (23, 3), (39, 4), (95, 5), (191, 6))
MIN_VALID_TRANSFERS = 20
# Rising SCK edges per byte (mode 0 idles low): seven periods lie between the first and the
# last, and only those enter a byte's interval, never a gap between bytes or frames.
EDGES_PER_BYTE = 8
CSV_COLUMNS = (
    "sequence", "position", "case", "path", "f_req_hz", "k_hz", "outcome", "br", "cr1",
    "f_br_hz", "reported_hz", "upstream_code_consistent", "reading_ceiling",
    "ceiling_expected_hz", "reading_nearest", "nearest_expected_hz", "matches", "electrical",
    "measured_lo_hz", "measured_hi_hz", "tolerance_ticks", "transfers_valid", "data_bit_errors",
    "status",
)
CSV_HEADER = ",".join(CSV_COLUMNS)
ROW_STATUSES = ("observed_pass", "observed_fail", "observed_panic", "not_observed")
# HQ #356: a construct position after the panic a variant's contract demands is never run.
NOT_REACHED = "not_reached"
CONTRACTS = ("never_exceed",)
VARIANT_KEYS = ("name", "contract", "baseline", "patch_sha256")
BEFORE_AFTER_COLUMNS = (
    "sequence", "position", "case", "path", "f_req_hz", "before_outcome", "before_hz",
    "before_status", "before_reading_ceiling", "after_outcome", "after_hz", "after_status",
    "after_reading_ceiling", "verdict",
)
READING_KEY = "reading_constructor_panics_where_set_config_rejects"
LIFECYCLE_KEYS = ("set_config_sequences_complete", "panic_observed", "recovery_boots",
                  "recovery_occurrence_observed", "end_observed", READING_KEY)
PROVENANCE_KEYS = (
    "embassy_rev", "elf_sha256", "build_argv", "toolchain", "qualification_rule_sha256",
    "upstream_recheck", "bench",
)
BENCH_KEYS = ("device", "probe", "backend")
RUN_HANDLE = re.compile(r"run-[0-9a-f]{16}")

KIND_FIELDS = {
    "BOOT": ("build_id", "embassy_rev", "cases_sha256", "measurement_sha256", "rustc"),
    "RCC": ("cfgr", "pllcfgr", "sysclk_hz", "hclk_hz", "pclk1_hz", "pclk2_hz"),
    "CAL": ("cfg", "runs", "failed", "overhead_cycles", "min_cycles", "max_cycles"),
    "CALISR": ("entries", "spurious", "sr", "cnt"),
    "QUAL": ("cfg", "step", "psc", "arr", "cycles", "samples", "ndtr", "dma_err", "data"),
    "ICQUAL": ("cfg", "pwm_psc", "pwm_arr", "captures", "ndtr", "dma_err", "overcapture",
               "timeout", "data"),
    "OCC": ("sequence", "position", "case", "path", "f_req", "outcome", "cr1", "reported_hz",
            "error"),
    # HQ #342: a transfer is the TIM8_CH2 capture of its frame, the timestamps as data.
    "XFER": ("sequence", "position", "case", "transfer", "cfg", "seed", "captures", "ndtr",
             "dma_err", "overcapture", "data"),
    # HQ #346: one compact summary per applied configuration instead of XFER records.
    "XSUM": ("sequence", "position", "case", "cfg", "transfers", "clean", "ndtr", "dma_err",
             "overcapture", "timeout", "drv_err", "cfg_changes", "seeds", "spans",
             "spans_dropped"),
    "PANIC": ("sequence", "position", "case"),
    "RECOVERY-BOOT": ("build_id",),
    "BLOCK-END": ("n",),
    "END": (),
}
INT_FIELDS = {
    "RCC": ("sysclk_hz", "hclk_hz", "pclk1_hz", "pclk2_hz"),
    "CAL": ("runs", "failed", "overhead_cycles", "min_cycles", "max_cycles"),
    "CALISR": ("entries", "spurious", "sr", "cnt"),
    "QUAL": ("step", "psc", "arr", "cycles", "samples", "ndtr", "dma_err"),
    "ICQUAL": ("pwm_psc", "pwm_arr", "captures", "ndtr", "dma_err", "overcapture", "timeout"),
    "OCC": ("position", "f_req", "reported_hz"),
    "XFER": ("position", "transfer", "captures", "ndtr", "dma_err", "overcapture"),
    "PANIC": ("position",),
    "BLOCK-END": ("n",),
}
HEX_FIELDS = {"RCC": ("cfgr", "pllcfgr"), "OCC": ("cr1",)}
# XSUM counts: the transfers attempted, the clean ones, those with each condition, the
# transfers whose divider differed from the first, and the spans the chip could not keep.
SUMMARY_CONDITIONS = ("ndtr", "dma_err", "overcapture", "timeout", "drv_err", "cfg_changes")
SUMMARY_COUNTS = ("transfers", "clean") + SUMMARY_CONDITIONS + ("spans_dropped",)


# ---------------------------------------------------------------------------------------
# Reading and containment
# ---------------------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _escapes(root_resolved, text):
    """True when an index path is not a plain relative path that stays inside the root."""
    if not isinstance(text, str) or not text or "\x00" in text:
        return True
    if PureWindowsPath(text).anchor or PureWindowsPath(text).drive:
        return True
    if PurePosixPath(text).is_absolute() or text.startswith(("/", "\\")):
        return True
    if ".." in re.split(r"[\\/]", text):
        return True
    try:
        resolved = (root_resolved / text).resolve()
    except (OSError, ValueError):
        return True
    return not (resolved == root_resolved or root_resolved in resolved.parents)


def _pointer_receipts(run):
    """Receipt paths named by uart_pointers items of the {receipt, pointer} form."""
    return [item["receipt"] for item in run.get("uart_pointers", []) if isinstance(item, dict)]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _pointer(document, pointer):
    """RFC 6901 lookup; raises KeyError, IndexError, ValueError or TypeError on a miss."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise KeyError(pointer)
    node = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        elif isinstance(node, dict):
            node = node[token]
        else:
            raise KeyError(pointer)
    return node


# ---------------------------------------------------------------------------------------
# Clocks, sampling, edges
# ---------------------------------------------------------------------------------------

def _hpre(code):
    if code < 8:
        return 1
    return {8: 2, 9: 4, 10: 8, 11: 16, 12: 64, 13: 128, 14: 256, 15: 512}[code]


def _ppre(code):
    if code < 4:
        return 1
    return {4: 2, 5: 4, 6: 8, 7: 16}[code]


def _decode_clocks(cfgr, pllcfgr, hsi_hz):
    """Clock tree from the raw RCC registers; ValueError for a source this campaign does not use."""
    sws = (cfgr >> 2) & 3
    if sws == 0:
        sysclk = Fraction(hsi_hz)
    elif sws == 2:
        if (pllcfgr >> 22) & 1:
            raise ValueError("PLL source is not HSI")
        pllm = pllcfgr & 0x3F
        plln = (pllcfgr >> 6) & 0x1FF
        pllp = (((pllcfgr >> 16) & 3) + 1) * 2
        if pllm == 0:
            raise ValueError("PLLM is zero")
        sysclk = Fraction(hsi_hz) / pllm * plln / pllp
    else:
        raise ValueError("system clock source is not HSI or the HSI PLL")
    hclk = sysclk / _hpre((cfgr >> 4) & 15)
    apb1 = _ppre((cfgr >> 10) & 7)
    apb2 = _ppre((cfgr >> 13) & 7)
    return {"sysclk": sysclk, "hclk": hclk, "pclk1": hclk / apb1, "pclk2": hclk / apb2,
            "apb1_div": apb1, "apb2_div": apb2}


def _timer_clock(clocks, bus, timpre=0):
    """TIMxCLK by RM0390 6.2: with TIMPRE=0 PCLKx for APB prescaler 1, else 2 x PCLKx; with
    TIMPRE=1 HCLK for APB prescaler 1, 2 or 4, else 4 x PCLKx."""
    if bus == "apb1":
        pclk, divider = clocks["pclk1"], clocks["apb1_div"]
    elif bus == "apb2":
        pclk, divider = clocks["pclk2"], clocks["apb2_div"]
    else:
        raise ValueError("unknown timer bus " + repr(bus))
    if timpre:
        return clocks["hclk"] if divider in (1, 2, 4) else 4 * pclk
    return pclk * (1 if divider == 1 else 2)


def _kernel_clock(clocks, instance):
    if instance in ("SPI2", "SPI3"):
        return clocks["pclk1"]
    if instance in ("SPI1", "SPI4"):
        return clocks["pclk2"]
    raise ValueError("unknown SPI instance " + repr(instance))


def _sample_rate(clocks, config):
    return _timer_clock(clocks, config["timer_bus"]) / ((config["psc"] + 1) * (config["arr"] + 1))


def _rising_edges(data, bit):
    edges = []
    previous = (data[0] >> bit) & 1 if data else 0
    for index in range(1, len(data)):
        current = (data[index] >> bit) & 1
        if previous == 0 and current == 1:
            edges.append(index)
        previous = current
    return edges


def _edge_interval(edges, fs):
    """Frequency interval of a run of len(edges)-1 periods between edges, or None."""
    if len(edges) < 2:
        return None
    periods = len(edges) - 1
    span = edges[-1] - edges[0]
    if span <= 1:
        return None
    return (Fraction(periods) * fs / (span + 1), Fraction(periods) * fs / (span - 1))


def _overloaded(fields, fs, hclk, tolerance):
    window = Fraction(fields["cycles"]) * fs / hclk
    # A missed end-of-window interrupt (timeout) leaves no valid window at all.
    return (abs(fields["samples"] - window) > tolerance or fields["ndtr"] != 0
            or fields["dma_err"] != 0 or fields.get("timeout", 0) != 0)


def _fnv1a32(text):
    value = 0x811C9DC5
    for byte in text.encode("ascii"):
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


def _expected_seed(prng_seed, sequence, position, case, transfer):
    return _fnv1a32(f"{prng_seed}|{sequence}|{position}|{case}|{transfer}") or 1


# ---------------------------------------------------------------------------------------
# Readings of Config.frequency
# ---------------------------------------------------------------------------------------

def _pinned_br(quotient):
    for upper, code in PINNED_ARMS:
        if quotient <= upper:
            return code
    return 7


def _readings(kernel, f_req, outcome, f_br):
    candidates = [Fraction(kernel) / d for d in DIVIDERS]
    under = [c for c in candidates if c <= f_req]
    ceiling = max(under) if under else None
    best = min(abs(c - f_req) for c in candidates)
    nearest = sorted((c for c in candidates if abs(c - f_req) == best), reverse=True)
    if outcome == "accepted":
        if ceiling is None:
            ceiling_class = "accepted_though_unattainable"
        elif f_br == ceiling:
            ceiling_class = "conforms"
        elif f_br > ceiling:
            ceiling_class = "exceeds_request"
        else:
            ceiling_class = "slower_than_reading"
        if f_br in nearest:
            nearest_class = "conforms" if len(nearest) == 1 else "conforms_tie"
        else:
            nearest_class = "differs"
    else:
        ceiling_class = "conforms" if ceiling is None else "rejected_though_attainable"
        nearest_class = "rejected_though_attainable"
    ceiling_ok = ceiling_class == "conforms" and outcome == "accepted"
    nearest_ok = nearest_class in ("conforms", "conforms_tie")
    if ceiling_ok and nearest_ok:
        matches = "both"
    elif ceiling_ok:
        matches = "ceiling_only"
    elif nearest_ok:
        matches = "nearest_only"
    else:
        matches = "neither"
    return {
        "reading_ceiling": ceiling_class,
        "ceiling_expected_hz": None if ceiling is None else str(ceiling),
        "reading_nearest": nearest_class,
        "nearest_expected_hz": [str(c) for c in nearest],
        "matches": matches,
    }


def contract_outcome(contract, kernel_hz, f_req_hz):
    """HQ #356: what a clock contract of the corrected driver does with a request, as
    {"outcome", "br", "error"}.

    "never_exceed" (the proposed contract, pending maintainer agreement): the smallest divider
    2^(br+1) whose SCK kernel_hz / 2^(br+1) does not exceed the request, so a request at or
    above kernel_hz / 2 gets BR 0. A request below kernel_hz / 256 (0 included), which no
    divider can meet, is rejected as FrequencyTooLow; the constructor panics on it. An unknown
    contract raises ValueError."""
    if contract not in CONTRACTS:
        raise ValueError(f"unknown clock contract {contract!r}")
    if f_req_hz * DIVIDERS[-1] < kernel_hz:
        return {"outcome": "rejected", "br": None, "error": "FrequencyTooLow"}
    br = next(n for n in range(len(DIVIDERS)) if kernel_hz <= f_req_hz * 2 ** (n + 1))
    return {"outcome": "accepted", "br": br, "error": "none"}


def variant_expectations(index, cases_doc, kernel_hz):
    """HQ #356: the expected outcomes, panic position and provenance of a variant ("after")
    bundle, from its index's variant object {name, contract, baseline, patch_sha256}.

    Returns {"contract", "problems", "boot_rev", "patch_sha256", "outcomes", "rejected",
    "panic_key", "unreached"}. Every defect of the variant object is one of "problems" (the
    caller reports each as contract_violation); without a known contract, "contract" is None
    and nothing is expected of the occurrences. The BOOT record of a variant names
    "<EMBASSY_REV>+patch.<first 16 hex of patch_sha256>". The construct sequence panics at its
    first request the contract rejects; its later positions are never reached."""
    variant = index.get("variant")
    problems = []
    if not isinstance(variant, dict):
        problems.append("the variant is not an object")
        variant = {}
    for key in VARIANT_KEYS:
        if key not in variant:
            problems.append(f"the variant names no {key}")
    extra = sorted(set(variant) - set(VARIANT_KEYS))
    if extra:
        problems.append("the variant has unknown keys: " + ", ".join(map(str, extra)))
    if "name" in variant and variant["name"] != "after":
        problems.append("the variant is not named after")
    contract = variant.get("contract")
    if "contract" in variant and contract not in CONTRACTS:
        problems.append(f"unknown clock contract {contract!r}")
    if contract not in CONTRACTS:
        contract = None
    if "baseline" in variant and not (isinstance(variant["baseline"], str)
                                      and re.fullmatch(r"matrix-[0-9a-f]{8}", variant["baseline"])):
        problems.append("the variant baseline is not a matrix bundle name")
    patch_sha = variant.get("patch_sha256")
    if not (isinstance(patch_sha, str) and re.fullmatch(r"[0-9a-f]{64}", patch_sha)):
        if "patch_sha256" in variant:
            problems.append("the variant patch_sha256 is not a SHA-256")
        patch_sha = None
    expectation = {
        "contract": contract, "problems": problems,
        "boot_rev": None if patch_sha is None else f"{EMBASSY_REV}+patch.{patch_sha[:16]}",
        "patch_sha256": patch_sha, "outcomes": {}, "rejected": set(), "panic_key": None,
        "unreached": set(),
    }
    if contract is None:
        return expectation
    outcomes = {case["id"]: contract_outcome(contract, kernel_hz, case["f_req_hz"])
                for case in cases_doc["cases"]}
    rejected = {case for case, outcome in outcomes.items() if outcome["outcome"] == "rejected"}
    for role in ("start_case", "recovery_case"):
        if cases_doc[role] in rejected:
            problems.append(f"the contract rejects the frozen {role}")
    construct = cases_doc["sequences"]["construct"]
    panic_at = next((position for position, case in enumerate(construct) if case in rejected),
                    None)
    expectation.update(outcomes=outcomes, rejected=rejected)
    if panic_at is not None:
        expectation["panic_key"] = ("construct", panic_at)
        expectation["unreached"] = {("construct", position)
                                    for position in range(panic_at + 1, len(construct))}
    return expectation


def _contract_problem(expected, fields):
    """Why an OCC record contradicts the contract's outcome for its request, or None. The BR
    of a rejected set_config is the configuration kept, so only an accepted one is judged on
    it."""
    if fields["outcome"] != expected["outcome"]:
        return f"outcome {fields['outcome']}, the contract gives {expected['outcome']}"
    if expected["outcome"] == "accepted" and (fields["cr1"] >> 3) & 7 != expected["br"]:
        return f"BR {(fields['cr1'] >> 3) & 7}, the contract gives {expected['br']}"
    if expected["outcome"] == "rejected" and fields["error"] != expected["error"]:
        return f"error {fields['error']}, the contract gives {expected['error']}"
    return None


# ---------------------------------------------------------------------------------------
# UART record
# ---------------------------------------------------------------------------------------

def _parse_line(line):
    """(kind, seq, fields, crc_ok) for a well-formed HQ184 line; ValueError otherwise."""
    tokens = line.split(" ")
    if len(tokens) < 4 or tokens[0] != "HQ184" or not tokens[-1].startswith("crc32="):
        raise ValueError("malformed line")
    fields = {}
    for token in tokens[2:]:
        key, separator, value = token.partition("=")
        if not separator or not key or key in fields:
            raise ValueError("malformed field")
        fields[key] = value
    if not re.fullmatch(r"-?\d+", fields.get("seq", "")):
        raise ValueError("malformed seq")
    crc = fields.pop("crc32")
    if not re.fullmatch(r"[0-9a-f]{8}", crc):
        raise ValueError("malformed crc")
    prefix = line[: line.rindex(" crc32=")]
    crc_ok = format(zlib.crc32(prefix.encode("ascii")), "08x") == crc
    seq = int(fields.pop("seq"))
    return tokens[1], seq, fields, crc_ok


def _named_key(sequence, position):
    """The occurrence key (sequence, position) that two XSUM field values name, or None when
    either is absent or the position is no integer. A digit string beyond Python's integer
    conversion limit names no occurrence either: the caller falls back to `current`."""
    if sequence is None or position is None or not re.fullmatch(r"-?\d+", position):
        return None
    try:
        return (sequence, int(position))
    except ValueError:
        return None


def _unparsed_summary_key(line):
    """The occurrence key an unparseable XSUM line still names: its sequence= and position=
    tokens, each read only when it appears exactly once; None otherwise."""
    values = {"sequence": [], "position": []}
    for token in line.split(" ")[2:]:
        name, separator, value = token.partition("=")
        if separator and name in values:
            values[name].append(value)
    if len(values["sequence"]) != 1 or len(values["position"]) != 1:
        return None
    return _named_key(values["sequence"][0], values["position"][0])


def _mark_conflict(occurrences, named, current):
    """A defective XSUM record leaves its occurrence counting nothing: the occurrence it
    names, when that one was received, else the occurrence of the preceding OCC (`current`).
    No occurrence is ever created here; with neither, nothing is marked."""
    for key in (named, current):
        if key is not None and key in occurrences:
            occurrences[key]["conflict"] = True
            return


def _typed_summary(fields):
    """The typed fields of an XSUM record (HQ #346): the compact summary of one applied
    configuration with its condition counters, seed digest and span multiset. spans becomes
    a list of (value, count) pairs, ascending and distinct, or [] for "none"; ValueError on
    any defect. Whether the counts add up to the clean transfers' bytes is judged by the
    caller, which knows the frozen transfer length."""
    for key in KIND_FIELDS["XSUM"]:
        if key not in fields:
            raise ValueError("missing field " + key)
    typed = dict(fields)
    if not re.fullmatch(r"-?\d+", fields["position"]):
        raise ValueError("field position is not an integer")
    typed["position"] = int(fields["position"])
    for key in SUMMARY_COUNTS:
        if not re.fullmatch(r"\d+", fields[key]):
            raise ValueError("field " + key + " is not a count")
        typed[key] = int(fields[key])
    if not re.fullmatch(r"[0-9a-f]{8}", fields["seeds"]):
        raise ValueError("seeds is not eight hexadecimal digits")
    if typed["clean"] > typed["transfers"] or any(
            typed[key] > typed["transfers"] for key in SUMMARY_CONDITIONS):
        raise ValueError("a count exceeds the transfers")
    if fields["spans"] == "none":
        spans = []
    else:
        spans = []
        for pair in fields["spans"].split(","):
            match = re.fullmatch(r"(\d+):(\d+)", pair)
            if match is None or int(match.group(2)) == 0:
                raise ValueError("spans is not a list of value:count pairs")
            spans.append((int(match.group(1)), int(match.group(2))))
        values = [value for value, _count in spans]
        if values != sorted(set(values)):
            raise ValueError("span values are not ascending and distinct")
    if (typed["clean"] == 0) != (not spans):
        raise ValueError("spans must be none exactly when no transfer is clean")
    typed["spans"] = spans
    return typed


def _typed_fields(kind, fields):
    """Fields with the integer and hexadecimal ones converted; ValueError on any defect."""
    if kind == "XSUM":
        return _typed_summary(fields)
    for key in KIND_FIELDS.get(kind, ()):
        if key not in fields:
            raise ValueError("missing field " + key)
    typed = dict(fields)
    for key in INT_FIELDS.get(kind, ()):
        if not re.fullmatch(r"-?\d+", fields[key]):
            raise ValueError("field " + key + " is not an integer")
        typed[key] = int(fields[key])
    for key in HEX_FIELDS.get(kind, ()):
        if not re.fullmatch(r"0x[0-9a-fA-F]+", fields[key]):
            raise ValueError("field " + key + " is not hexadecimal")
        typed[key] = int(fields[key], 16)
    if kind in ("BOOT", "RECOVERY-BOOT") and not re.fullmatch(r"[0-9a-f]{8}", fields["build_id"]):
        raise ValueError("build_id is not eight hexadecimal digits")
    if kind == "XFER" and not re.fullmatch(r"[0-9a-f]{8}", fields["seed"]):
        raise ValueError("seed is not eight hexadecimal digits")
    for flag in ("drv_err", "timeout"):
        if kind in ("XFER", "QUAL") and flag in fields:
            if flag == "drv_err" and kind != "XFER":
                continue
            if fields[flag] not in ("0", "1"):
                raise ValueError(flag + " is not 0 or 1")
            typed[flag] = int(fields[flag])
    if kind == "QUAL":
        samples = base64.b64decode(fields["data"], validate=True)
        if len(samples) != typed["samples"]:
            raise ValueError("data length differs from samples")
        typed["data_bytes"] = samples
    if kind in ("ICQUAL", "XFER"):
        # A short or long timestamp list is judged by the capture path, not refused here.
        typed["data_bytes"] = base64.b64decode(fields["data"], validate=True)
    return typed


def _expected_occurrences(cases_doc):
    expected = {}
    sequences = cases_doc["sequences"]
    for name in SEQUENCE_ORDER:
        if name == "recovery":
            expected[(name, 0)] = cases_doc["recovery_case"]
        elif name == "construct":
            for position, case in enumerate(sequences[name]):
                expected[(name, position)] = case
        else:
            expected[(name, -1)] = cases_doc["start_case"]
            for position, case in enumerate(sequences[name]):
                expected[(name, position)] = case
    return expected


def _evaluate_transfer(fields, key, case, tim8, tolerance, length, prng_seed):
    """Judge one capture-path XFER (HQ #342). Returns {'codes', 'interval', 'cfg',
    'transfer'}.

    Any outstanding transfer, DMA error, overcapture or timeout makes the capture unusable
    (capture_overload). A frame must carry exactly 8 x length rising edges. Each byte's
    interval is [7 f_TIM8 / (span + tol), 7 f_TIM8 / (span - tol)], with span the sum of its
    seven intra-byte differences (mod 2^16) and tol the frozen period_tick_tolerance; the
    transfer's interval is the intersection over its bytes. A gap between bytes or frames
    never enters it. With the TIM8 clock unknown (an error of its own), there is no interval."""
    sequence, position = key
    result = {"codes": [], "interval": None, "cfg": fields["cfg"], "transfer": fields["transfer"]}
    overloaded = (any(fields[flag] != 0 for flag in ("ndtr", "dma_err", "overcapture"))
                  or fields.get("timeout", 0) != 0)
    if overloaded:
        result["codes"].append("capture_overload")
    if fields.get("drv_err", 0) != 0:
        result["codes"].append("driver_error")
    expected_seed = _expected_seed(prng_seed, sequence, position, case, fields["transfer"])
    if fields["seed"] != format(expected_seed, "08x"):
        result["codes"].append("seed_mismatch")
    if overloaded:
        return result
    edges = EDGES_PER_BYTE * length
    data = fields["data_bytes"]
    if fields["captures"] != edges or len(data) != 2 * edges:
        result["codes"].append("edge_count_mismatch")
        return result
    if tim8 is None:
        return result
    stamps = [int.from_bytes(data[index:index + 2], "little") for index in range(0, len(data), 2)]
    low, high = None, None
    for byte_index in range(length):
        byte_edges = stamps[EDGES_PER_BYTE * byte_index: EDGES_PER_BYTE * (byte_index + 1)]
        span = sum((later - earlier) % (1 << 16)
                   for earlier, later in zip(byte_edges, byte_edges[1:]))
        if span <= tolerance:
            result["codes"].append("inconsistent_capture")
            return result
        periods = Fraction(EDGES_PER_BYTE - 1)
        byte_low, byte_high = periods * tim8 / (span + tolerance), periods * tim8 / (span - tolerance)
        low = byte_low if low is None else max(low, byte_low)
        high = byte_high if high is None else min(high, byte_high)
    if low > high:
        result["codes"].append("inconsistent_capture")
        return result
    result["interval"] = (low, high)
    return result


def _evaluate_summary(fields, key, case, tim8, tolerance, prng_seed):
    """Judge one XSUM record (HQ #346), the summary of one applied configuration. Returns
    {'codes', 'interval', 'cfg', 'valid'}.

    The counters count transfers: any one with an outstanding transfer, a DMA error, an
    overcapture or a timeout is capture_overload, a driver error driver_error, and a seeds
    digest other than FNV-1a-32 over the '|'-joined per-transfer seeds seed_mismatch. A
    divider that changed during the occurrence (cfg_changes) is uart_field_invalid. The spans
    are the byte spans of the clean transfers, each the sum of its seven intra-byte
    differences (mod 2^16), so the interval is the HQ #342 intersection over every byte of
    every clean transfer, [7 f_TIM8 / (min_span + tol), 7 f_TIM8 / (max_span - tol)]; a gap
    between bytes or frames never enters it. A span dropped on chip, a span within the
    tolerance or an empty intersection is inconsistent_capture. Unlike XFER, the per-transfer
    grouping is not sent, so any code invalidates every transfer of the occurrence: valid is
    clean when no code applies and the TIM8 clock is known, else 0."""
    sequence, position = key
    result = {"codes": [], "interval": None, "cfg": fields["cfg"], "valid": 0}
    if any(fields[flag] for flag in ("ndtr", "dma_err", "overcapture", "timeout")):
        result["codes"].append("capture_overload")
    if fields["drv_err"]:
        result["codes"].append("driver_error")
    joined = "|".join(format(_expected_seed(prng_seed, sequence, position, case, transfer), "08x")
                      for transfer in range(fields["transfers"]))
    if fields["seeds"] != format(_fnv1a32(joined), "08x"):
        result["codes"].append("seed_mismatch")
    if fields["cfg_changes"]:
        result["codes"].append("uart_field_invalid")
    spans = fields["spans"]
    if fields["spans_dropped"] or (spans and spans[0][0] <= tolerance):
        result["codes"].append("inconsistent_capture")
    elif spans:
        periods = Fraction(EDGES_PER_BYTE - 1)
        if tim8 is not None:
            low = periods * tim8 / (spans[0][0] + tolerance)
            high = periods * tim8 / (spans[-1][0] - tolerance)
            if low > high:
                result["codes"].append("inconsistent_capture")
            else:
                result["interval"] = (low, high)
    if not result["codes"] and result["interval"] is not None:
        result["valid"] = fields["clean"]
    return result


def _evaluate_qualification(fields, config, step, clocks, rule):
    fs = _sample_rate(clocks, config)
    f_pwm = _timer_clock(clocks, config["pwm_timer_bus"]) / ((step["psc"] + 1) * (step["arr"] + 1))
    data = fields["data_bytes"]
    edges = _rising_edges(data, config["sck_bit"])
    interval = _edge_interval(edges, fs)
    passed = (
        not _overloaded(fields, fs, clocks["hclk"], rule["sample_count_tolerance"])
        and len(edges) >= 2
        and abs(len(edges) - Fraction(fields["samples"]) * f_pwm / fs) <= rule["edge_count_tolerance"]
        and interval is not None
    )
    if passed:
        bits = [(byte >> config["sck_bit"]) & 1 for byte in data]
        runs = []
        for bit in bits:
            if runs and runs[-1][0] == bit:
                runs[-1][1] += 1
            else:
                runs.append([bit, 1])
        interior = runs[1:-1]
        passed = (
            all(length >= rule["min_samples_per_half_period"] for _bit, length in interior)
            and interval[0] <= f_pwm <= interval[1]
            and (interval[1] - interval[0]) / f_pwm
            <= Fraction(str(rule["max_relative_interval_width"]))
        )
    return f_pwm, passed, interval


def _qualify(phase_name, records, configs, clocks, rule, fail):
    """Qualification of one boot. A qualified limit is granted only when every frozen PWM
    step of the configuration has exactly one result; it is the last of the consecutive
    passes from the lowest frequency. Returns (entries, highest limit per configuration)."""
    results = {config_id: {} for config_id in configs}
    for fields in records:
        config = configs.get(fields["cfg"])
        steps = config["pwm_steps"] if config else []
        if (config is None or clocks is None or not 0 <= fields["step"] < len(steps)
                or steps[fields["step"]]["psc"] != fields["psc"]
                or steps[fields["step"]]["arr"] != fields["arr"]):
            fail("uart_field_invalid", "QUAL does not match the frozen capture configuration")
            continue
        f_pwm, passed, interval = _evaluate_qualification(
            fields, config, steps[fields["step"]], clocks, rule)
        results[fields["cfg"]].setdefault(fields["step"], []).append({
            "step": fields["step"], "f_pwm_hz": str(f_pwm), "passed": passed,
            "interval_hz": None if interval is None else [str(interval[0]), str(interval[1])],
            "_f": f_pwm,
        })
    highest_map = {}
    entries = []
    for config_id, config in configs.items():
        by_step = results[config_id]
        steps = [result for step in sorted(by_step) for result in by_step[step]]
        highest = None
        highest_step = None
        if all(len(by_step.get(step, [])) == 1 for step in range(len(config["pwm_steps"]))):
            for step in sorted(steps, key=lambda result: result["_f"]):
                if not step["passed"]:
                    break
                highest = step["_f"]
                highest_step = step["step"]
        else:
            fail("qualification_incomplete",
                 f"{phase_name} {config_id}: not exactly one result per frozen PWM step")
        highest_map[config_id] = highest
        entries.append({
            "cfg": config_id, "phase": phase_name,
            "steps": [{key: value for key, value in step.items() if key != "_f"}
                      for step in steps],
            "highest_passing_hz": None if highest is None else str(highest),
            "highest_passing_step": highest_step,
        })
    return entries, highest_map


def _checked_clocks(rcc_records, hsi_hz, instance, spi_kernel_hz, fail):
    """The clock tree the RCC registers give; every record is checked against its report."""
    clocks = None
    for rcc in rcc_records:
        try:
            decoded = _decode_clocks(rcc["cfgr"], rcc["pllcfgr"], hsi_hz)
            reported = {"sysclk": rcc["sysclk_hz"], "hclk": rcc["hclk_hz"],
                        "pclk1": rcc["pclk1_hz"], "pclk2": rcc["pclk2_hz"]}
            consistent = (all(decoded[name] == value for name, value in reported.items())
                          and _kernel_clock(decoded, instance) == spi_kernel_hz)
        except ValueError:
            consistent = False
            decoded = None
        if not consistent:
            fail("rcc_inconsistent", "decoded RCC registers differ from the reported clocks")
        if clocks is None:
            # The registers decide; a disagreeing report is already an error of its own.
            clocks = decoded
        elif decoded is not None and decoded != clocks:
            fail("rcc_inconsistent", "the clock tree differs between the boots")
    return clocks


def _evaluate_setup(text, inputs, configs, rule, frozen, fail):
    """Qualification mode: judge the setup block of one boot, the records from BOOT up to and
    including the first BLOCK-END n=1. A record is a line ended by its LF; an unterminated
    fragment at the end of the text is no record, so a BLOCK-END without its LF does not end
    the block. Everything after the LF that ends BLOCK-END n=1 is ignored and only counted.

    The setup records must come in the order BOOT, RCC, CAL, CALISR, then the QUAL records
    and, in a capture-path firmware (HQ #340), the ICQUAL records, which
    _evaluate_capture_path judges,
    with exactly one each of BOOT, RCC, CAL and CALISR (lifecycle_violation otherwise; an out
    of order or repeated record is not used). If an acquisition record appears before
    BLOCK-END n=1, the setup block was cut short: that is a lifecycle_violation, and every
    further complete HQ184 line up to the end of the text is still checked for its crc32 but
    is no setup record. crc_ok and crc_failed count every complete HQ184 line checked this
    way, so crc_failed equals the number of uart_crc_mismatch errors.
    Returns (qualification entries, setup summary)."""
    hsi_hz, instance, spi_kernel_hz = frozen
    setup = {"block_end_seen": False, "trailing_bytes": 0, "crc_ok": 0, "crc_failed": 0,
             "qual_records": 0, "clocks": None, "cal": None, "calisr": None}
    order = ("BOOT",) + SETUP_KINDS
    stage = -1
    cut_short = False
    previous_seq = None
    timeout_reported = False
    rcc_records, cal_records, calisr_records, qual_records = [], [], [], []
    position = 0
    while position < len(text):
        newline = text.find("\n", position)
        if newline < 0:
            break
        line = text[position:newline].rstrip("\r")
        position = newline + 1
        if not line.startswith("HQ184 "):
            continue
        try:
            kind, seq, fields, crc_ok = _parse_line(line)
        except (ValueError, UnicodeEncodeError):
            fail("uart_field_invalid", line[:80])
            continue
        if crc_ok:
            setup["crc_ok"] += 1
        else:
            setup["crc_failed"] += 1
            fail("uart_crc_mismatch", f"{kind} seq={seq}")
        if cut_short:
            continue
        if kind == "BOOT":
            if seq != 0:
                fail("uart_sequence_gap", f"{kind} seq={seq}")
        elif previous_seq is None or seq != previous_seq + 1:
            fail("uart_sequence_gap", f"{kind} seq={seq} after {previous_seq}")
        previous_seq = seq
        if kind not in order and kind != "BLOCK-END":
            # An acquisition record: the setup block was left without its BLOCK-END n=1. Its
            # kind decides that, not its fields, so a record in an older firmware's shape (the
            # pad-sampler XFER of HQ #338) cuts the block short just the same.
            fail("lifecycle_violation", f"{kind} seq={seq} inside the setup block")
            cut_short = True
            continue
        try:
            typed = _typed_fields(kind, fields)
        except ValueError as problem:
            fail("uart_field_invalid", f"{kind} seq={seq}: {problem}")
            continue
        if kind in order:
            rank = order.index(kind)
            if rank < stage or (rank == stage and kind not in ("QUAL", "ICQUAL")):
                fail("lifecycle_violation",
                     f"{kind} seq={seq} out of the order BOOT, RCC, CAL, CALISR, QUAL, ICQUAL")
                continue
            if rank > stage + 1:
                fail("lifecycle_violation", f"{kind} seq={seq} before {order[stage + 1]}")
            stage = rank
            if kind == "BOOT":
                if typed["embassy_rev"] != EMBASSY_REV:
                    fail("provenance_incomplete", "BOOT embassy_rev is not the pinned commit")
                if (typed["cases_sha256"] != _sha256(inputs["input_cases"])
                        or typed["measurement_sha256"] != _sha256(inputs["input_measurement"])):
                    fail("input_digest_mismatch", "BOOT digests differ from the frozen inputs")
            elif kind == "RCC":
                rcc_records.append(typed)
            elif kind == "CAL":
                cal_records.append(dict(fields))
            elif kind == "CALISR":
                calisr_records.append(dict(fields))
            elif kind == "QUAL":
                qual_records.append(typed)
                if "timeout" not in typed and not timeout_reported:
                    timeout_reported = True
                    fail("uart_field_invalid", "capture records carry no timeout flag")
        elif kind == "BLOCK-END" and typed["n"] == 1:
            setup["block_end_seen"] = True
            setup["trailing_bytes"] = len(text) - position
            break
        else:
            # A later BLOCK-END: the setup block was left without its BLOCK-END n=1.
            fail("lifecycle_violation", f"{kind} seq={seq} inside the setup block")
            cut_short = True

    if not setup["block_end_seen"]:
        fail("setup_incomplete", "no BLOCK-END n=1 record ends the setup block")
    for kind, records in (("RCC", rcc_records), ("CAL", cal_records),
                          ("CALISR", calisr_records)):
        if not records:
            fail("lifecycle_violation", f"the setup block has no {kind} record")
    setup["qual_records"] = len(qual_records)
    clocks = _checked_clocks(rcc_records, hsi_hz, instance, spi_kernel_hz, fail)
    if clocks is not None:
        setup["clocks"] = {f"{name}_hz": str(clocks[name])
                           for name in ("sysclk", "hclk", "pclk1", "pclk2")}
    if len(cal_records) != 1 or cal_records[0].get("failed") != "0":
        fail("calibration_failed", "expected exactly one CAL record with failed=0")
    setup["cal"] = cal_records[0] if cal_records else None
    setup["calisr"] = calisr_records[0] if calisr_records else None

    entries, _highest = _qualify("boot", qual_records, configs, clocks, rule, fail)
    # cfgN captures the SCK of divider 2^(N+1) (measurement.json sampler note): the limit
    # serves that configuration when it reaches the SCK frequency K / 2^(N+1).
    for index, entry in enumerate(entries):
        serves_hz = Fraction(spi_kernel_hz, 2 ** (index + 1))
        entry["serves_hz"] = str(serves_hz)
        entry["serves"] = (entry["highest_passing_hz"] is not None
                           and Fraction(entry["highest_passing_hz"]) >= serves_hz)
    return entries, setup


def _setup_block_records(text):
    """(kind, typed fields, crc_ok) of the well-formed records of the setup block: complete
    HQ184 lines up to BLOCK-END n=1, or up to the first record that is no setup record."""
    records = []
    position = 0
    while position < len(text):
        newline = text.find("\n", position)
        if newline < 0:
            break
        line = text[position:newline].rstrip("\r")
        position = newline + 1
        if not line.startswith("HQ184 "):
            continue
        try:
            kind, _seq, fields, crc_ok = _parse_line(line)
            typed = _typed_fields(kind, fields)
        except (ValueError, UnicodeEncodeError):
            continue
        if kind == "BLOCK-END" and typed["n"] == 1:
            break
        if kind not in ("BOOT",) + SETUP_KINDS:
            break
        records.append((kind, typed, crc_ok))
    return records


def _evaluate_capture_path(text, measurement, setup, fail):
    """Qualification mode, HQ #340: judge the ICQUAL records of the TIM8_CH2 capture path
    against the TIM2_CH3 reference PWM, one entry per frozen capture_path configuration.

    The timer clocks come from the RCC registers and TIMPRE (bit 24 of the reported
    dckcfgr), by RM0390 6.2. The reference period must be a whole number of TIM8 ticks
    (capture_reference_inexact otherwise). A record passes when every flag is 0, it carries
    the frozen number of timestamps and every successive difference (mod 2^16) lies within
    period_tick_tolerance of that period. A failing configuration is a result, not an error;
    one without a record, or whose only record failed its crc32, is not observed
    (capture_path_incomplete). serves: passed, and the reference equals K / 2^(N+1), the SCK
    frequency of divider 2^(N+1)."""
    # The clock source is the first RCC record whose crc32 checks out: a corrupted record must
    # not stand in for the registers, so without one the timer clocks stay unknown.
    rcc = None
    rcc_crc_failed = False
    icquals = []
    for kind, typed, crc_ok in _setup_block_records(text):
        if kind == "RCC" and rcc is None:
            if crc_ok:
                rcc = typed
            else:
                rcc_crc_failed = True
        elif kind == "ICQUAL":
            icquals.append((typed, crc_ok))
    entries, timpre, _tim8 = _judge_capture_path(rcc, rcc_crc_failed, icquals, measurement, fail)
    setup["icqual_records"] = len(icquals)
    setup["timpre"] = timpre
    return entries


def _judge_capture_path(rcc, rcc_crc_failed, icquals, measurement, fail):
    """The capture-path judgement of one boot: `rcc` is the first RCC record whose crc32
    checks out (None without one; `rcc_crc_failed` says whether one failed it), `icquals` the
    (typed ICQUAL, crc_ok) pairs of that boot. Returns (entries, TIMPRE, TIM8 clock), the
    clock None when it is unknown."""
    path = measurement["capture_path"]
    captures = path["captures"]
    tolerance = path["period_tick_tolerance"]
    spi_kernel_hz = measurement["clock"]["spi_kernel_hz"]

    clocks = timpre = None
    if rcc is not None:
        try:
            clocks = _decode_clocks(rcc["cfgr"], rcc["pllcfgr"], measurement["clock"]["hsi_hz"])
        except ValueError:
            clocks = None
        raw = rcc.get("dckcfgr")
        if isinstance(raw, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,8}", raw):
            timpre = (int(raw, 16) >> 24) & 1
    tim2 = tim8 = None
    if rcc is None and rcc_crc_failed:
        fail("timer_clock_unknown", "the RCC record failed its crc32: the timer clocks are unknown")
    elif timpre is None:
        fail("timer_clock_unknown", "the RCC record carries no dckcfgr: TIMPRE is unknown")
    elif clocks is None:
        fail("timer_clock_unknown", "the RCC registers give no clock tree")
    else:
        tim2 = _timer_clock(clocks, path["reference"]["bus"], timpre)
        tim8 = _timer_clock(clocks, path["timer_bus"], timpre)

    frozen_ids = [config["id"] for config in path["configurations"]]
    by_cfg = {}
    for typed, crc_ok in icquals:
        if typed["cfg"] not in frozen_ids:
            fail("uart_field_invalid", f"ICQUAL for the unknown configuration {typed['cfg']}")
            continue
        by_cfg.setdefault(typed["cfg"], []).append((typed, crc_ok))

    entries = []
    not_observed = []
    for n, config in enumerate(path["configurations"]):
        config_id, psc, arr = config["id"], config["pwm_psc"], config["pwm_arr"]
        serves_hz = Fraction(spi_kernel_hz, 2 ** (n + 1))
        entry = {"cfg": config_id, "pwm_psc": psc, "pwm_arr": arr, "status": "not_observed",
                 "reasons": [], "f_ref_hz": None, "period_ticks": None,
                 "serves_hz": str(serves_hz), "interval_hz": None, "serves": False}
        entries.append(entry)
        f_ref = period = None
        if tim2 is not None:
            f_ref = tim2 / ((psc + 1) * (arr + 1))
            entry["f_ref_hz"] = str(f_ref)
            ticks = tim8 / f_ref
            if ticks.denominator != 1:
                fail("capture_reference_inexact",
                     f"{config_id}: the reference period is {ticks} TIM8 ticks")
            else:
                period = int(ticks)
                entry["period_ticks"] = period
        records = by_cfg.get(config_id, [])
        if len(records) > 1:
            fail("uart_field_invalid", f"{config_id}: {len(records)} ICQUAL records")
        usable = [typed for typed, crc_ok in records if crc_ok]
        if not usable:
            entry["reasons"] = (["the only record failed its crc32"] if records
                                else ["no record received"])
            not_observed.append(config_id)
            continue
        record = usable[0]
        reasons = []
        if (record["pwm_psc"], record["pwm_arr"]) != (psc, arr):
            reasons.append(f"pwm_psc={record['pwm_psc']} pwm_arr={record['pwm_arr']} differ "
                           f"from the frozen {psc} and {arr}")
        for flag in ("ndtr", "dma_err", "overcapture", "timeout"):
            if record[flag] != 0:
                reasons.append(f"{flag}={record[flag]}")
        data = record["data_bytes"]
        if record["captures"] != captures:
            reasons.append(f"captures={record['captures']}, frozen {captures}")
        if len(data) != 2 * record["captures"]:
            reasons.append(f"data carries {len(data)} bytes for captures={record['captures']}")
        stamps = [int.from_bytes(data[index:index + 2], "little")
                  for index in range(0, len(data) - 1, 2)]
        if period is None:
            reasons.append("the reference period in TIM8 ticks is unknown or not whole")
        elif period + tolerance >= 1 << 16:
            reasons.append(f"the reference period of {period} ticks exceeds the 16-bit counter")
        elif len(stamps) < 2:
            reasons.append("fewer than two timestamps")
        else:
            differences = [(later - earlier) % (1 << 16)
                           for earlier, later in zip(stamps, stamps[1:])]
            outside = [ticks for ticks in differences if abs(ticks - period) > tolerance]
            if outside:
                reasons.append(f"{len(outside)} of {len(differences)} periods outside "
                               f"{period} +- {tolerance} ticks (min {min(differences)}, "
                               f"max {max(differences)})")
            span = sum(differences)
            if span > 1:
                entry["interval_hz"] = [str(Fraction(len(differences)) * tim8 / (span + 1)),
                                        str(Fraction(len(differences)) * tim8 / (span - 1))]
        entry["reasons"] = reasons
        entry["status"] = "observed_fail" if reasons else "observed_pass"
        entry["serves"] = not reasons and f_ref == serves_hz
    if not_observed:
        fail("capture_path_incomplete", "not observed: " + ", ".join(not_observed))
    return entries, timpre, tim8


# ---------------------------------------------------------------------------------------
# evaluate_bundle
# ---------------------------------------------------------------------------------------

def evaluate_bundle(root, mode):
    if mode not in MODES:
        raise ValueError("mode must be synthetic, hardware or qualification")
    root = Path(root)
    errors = []
    evaluation = {"schema": SCHEMA, "mode": mode, "synthetic": False, "errors": errors,
                  "qualification": [], "rows": []}

    def fail(code, detail):
        errors.append({"code": code, "detail": detail})

    try:
        index = json.loads(_read(root / "index.json").decode("utf-8"))
        files = index["files"]
        hil = index["hil"]
        plans = hil["plans"]
        runs = hil["runs"]
        paths = [entry["path"] for entry in files]
        paths += [entry["path"] for entry in plans]
        for run in runs:
            paths += [run["plan"], run["receipt"], run["uart"]] + _pointer_receipts(run)
        if (index.get("schema") != BUNDLE_SCHEMA or not isinstance(index.get("provenance", {}), dict)
                or not all(isinstance(entry, dict) for entry in files + plans + runs)):
            raise ValueError("unexpected index shape")
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        fail("index_invalid", "index.json is missing, not valid JSON or not a hq184 bundle index")
        return evaluation
    evaluation["synthetic"] = index.get("synthetic") is True

    # 1. Containment before any indexed read.
    root_resolved = root.resolve()
    escaping = [path for path in paths if _escapes(root_resolved, path)]
    if escaping:
        for path in escaping:
            fail("path_escape", str(path))
        return evaluation

    # 2. Files. The inventory is the only source of bytes: every path appears once, and every
    # path the plans and runs refer to must be listed so that its hash and size are judged.
    listed_paths = [entry["path"] for entry in files]
    if len(set(listed_paths)) != len(listed_paths):
        fail("index_invalid", "a path is listed more than once in the file inventory")
        return evaluation
    data = {}
    for entry in files:
        target = root / entry["path"]
        if not target.is_file():
            fail("missing_file", entry["path"])
            continue
        content = _read(target)
        data[entry["path"]] = content
        if _sha256(content) != entry.get("sha256") or len(content) != entry.get("size"):
            fail("hash_mismatch", entry["path"])
    referenced = [entry["path"] for entry in plans]
    for run in runs:
        referenced += [run["plan"], run["receipt"], run["uart"]] + _pointer_receipts(run)
    for path in dict.fromkeys(referenced):
        if path not in listed_paths:
            fail("missing_file", f"{path}: referenced but not in the file inventory")

    def content_of(path):
        return data.get(path)

    inputs = {}
    for role in ("input_cases", "input_measurement"):
        listed = [entry["path"] for entry in files if entry.get("role") == role]
        inputs[role] = data.get(listed[0]) if len(listed) == 1 else None
    try:
        cases_doc = json.loads(inputs["input_cases"].decode("utf-8"))
        measurement = json.loads(inputs["input_measurement"].decode("utf-8"))
        rule = measurement["qualification_rule"]
        configs = {config["id"]: config for config in measurement["capture_configurations"]}
        length = measurement["transfer_length_bytes"]
        prng_seed = measurement["prng_seed"]
        instance = measurement["spi"]["instance"]
        hsi_hz = measurement["clock"]["hsi_hz"]
        spi_kernel_hz = measurement["clock"]["spi_kernel_hz"]
        expected = _expected_occurrences(cases_doc)
        frequencies = {case["id"]: case["f_req_hz"] for case in cases_doc["cases"]}
        for case in expected.values():
            frequencies[case]
        if not all(isinstance(f, int) and not isinstance(f, bool) and f > 0
                   for f in frequencies.values()):
            raise ValueError("a frozen frequency is not a positive integer")
        if not isinstance(spi_kernel_hz, int) or spi_kernel_hz <= 0:
            raise ValueError("the frozen kernel clock is not a positive integer")
    except (AttributeError, KeyError, TypeError, ValueError):
        fail("index_invalid", "the frozen inputs are missing or malformed")
        return evaluation
    # HQ #356: a bundle of a variant build is judged against its clock contract. It carries the
    # patch of the variant as its input_patch file, whose digest the variant names.
    variant = None
    contract = None
    if index.get("variant") is not None:
        variant = variant_expectations(index, cases_doc, spi_kernel_hz)
        contract = variant["contract"]
        for problem in variant["problems"]:
            fail("contract_violation", problem)
        named = index["variant"] if isinstance(index["variant"], dict) else {}
        evaluation["variant"] = {key: named.get(key) for key in VARIANT_KEYS}
        listed = [entry["path"] for entry in files if entry.get("role") == "input_patch"]
        patch_bytes = data.get(listed[0]) if len(listed) == 1 else None
        if patch_bytes is None:
            fail("contract_violation", "the bundle carries no single readable input_patch file")
        elif _sha256(patch_bytes) != named.get("patch_sha256"):
            fail("contract_violation", "the input_patch digest differs from the variant's patch_sha256")

    # 3. Plans.
    for entry in plans:
        content = content_of(entry["path"])
        try:
            canonical = _sha256(json.dumps(json.loads(content.decode("utf-8")),
                                           sort_keys=True).encode())
        except (AttributeError, ValueError):
            fail("plan_hash_mismatch", entry["path"])
            continue
        if (entry.get("canonical_sha256") != canonical
                or entry.get("remote_receipt_path") != f".agentic-hil/runner-plans/{canonical}.json"):
            fail("plan_hash_mismatch", entry["path"])

    # 4. Receipts.
    for run in runs:
        problem = None
        try:
            receipt = json.loads(content_of(run["receipt"]).decode("utf-8"))
            uart = content_of(run["uart"]).decode("utf-8")
            # A pointer is an RFC 6901 pointer into the run receipt, or {receipt, pointer}
            # into another listed receipt (route B: the text of each com_read).
            pieces = []
            for item in run["uart_pointers"]:
                if isinstance(item, dict):
                    source = json.loads(content_of(item["receipt"]).decode("utf-8"))
                    pieces.append(_pointer(source, item["pointer"]))
                else:
                    pieces.append(_pointer(receipt, item))
            joined = "".join(pieces)
            if not isinstance(run.get("handle"), str) or not RUN_HANDLE.fullmatch(run["handle"]):
                problem = "the run handle is not run- plus 16 hexadecimal digits"
            elif run["handle"] not in set(_strings(receipt)):
                problem = "the receipt does not contain the run handle"
            elif joined != uart:
                problem = "the UART file differs from the receipt text"
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            problem = "the receipt or UART file cannot be linked"
        if problem:
            fail("receipt_link_missing", f"{run.get('purpose')}: {problem}")

    # 5. Provenance.
    provenance = index.get("provenance", {})
    if any(key not in provenance for key in PROVENANCE_KEYS) or not isinstance(
            provenance.get("bench"), dict) or any(
            key not in provenance["bench"] for key in BENCH_KEYS):
        fail("provenance_incomplete", "a required provenance key is missing")
    if provenance.get("embassy_rev") != EMBASSY_REV:
        fail("provenance_incomplete", "embassy_rev is not the pinned commit")
    rule_digest = _sha256(json.dumps(rule, sort_keys=True, separators=(",", ":")).encode())
    if provenance.get("qualification_rule_sha256") != rule_digest:
        fail("provenance_incomplete", "qualification_rule_sha256 differs from the frozen rule")

    # Qualification mode judges the setup block of a setup-only bundle and nothing else: no
    # occurrence, no row and no limit table.
    if mode == "qualification":
        qualifying = [run for run in runs if run.get("purpose") == "qualification"]
        if index.get("scope") != "setup" or len(qualifying) != 1:
            fail("index_invalid",
                 "a qualification needs a setup bundle with exactly one qualification run")
            return evaluation
        try:
            text = content_of(qualifying[0]["uart"]).decode("utf-8")
        except (AttributeError, ValueError):
            fail("index_invalid", "the qualification UART file is missing")
            return evaluation
        evaluation["qualification"], evaluation["setup"] = _evaluate_setup(
            text, inputs, configs, rule, (hsi_hz, instance, spi_kernel_hz), fail)
        if "capture_path" in measurement:
            evaluation["capture_path"] = _evaluate_capture_path(
                text, measurement, evaluation["setup"], fail)
        return evaluation

    # 6. Mode.
    if mode == "hardware":
        has_receipt = any(entry.get("role") == "receipt" for entry in files)
        if (index.get("mode") != "hardware" or index.get("synthetic") is True or not has_receipt
                or any(run.get("run_ok") is not True or not run.get("runner_evidence_path")
                       for run in runs)):
            fail("synthetic_in_hardware_mode",
                 "a hardware evaluation needs a hardware bundle with runner evidence")
        if index.get("scope") == "setup":
            # A setup-only bundle has no acquisition: it never counts as matrix evidence.
            fail("setup_only_in_hardware_mode",
                 "a setup-only bundle cannot be evaluated as hardware evidence of the matrix")
            return evaluation
    # Extra obligations of a bundle that claims to be hardware evidence: a driver result on
    # every transfer and a complete qualification after the recovery boot. A synthetic bundle
    # is already refused above and is not judged twice.
    strict = (mode == "hardware" and index.get("mode") == "hardware"
              and index.get("synthetic") is not True)
    acquire = [run for run in runs if run.get("purpose") == "acquire"]
    if len(acquire) != 1:
        fail("index_invalid", "exactly one run must have purpose acquire")
        return evaluation
    try:
        uart_text = content_of(acquire[0]["uart"]).decode("utf-8")
    except (AttributeError, ValueError):
        fail("index_invalid", "the acquire UART file is missing")
        return evaluation
    # HQ #342: every row is measured on the TIM8_CH2 capture path with its frozen tolerance.
    try:
        tolerance = measurement["capture_path"]["period_tick_tolerance"]
        path_ids = [config["id"] for config in measurement["capture_path"]["configurations"]]
        if not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0:
            raise ValueError("the frozen period_tick_tolerance is not a whole tick count")
    except (AttributeError, KeyError, TypeError, ValueError):
        fail("index_invalid", "the frozen inputs carry no usable capture_path")
        return evaluation

    # UART record. The frozen lifecycle: the set_config sequences, then the construct sequence,
    # whose single request above the kernel clock panics by design (and only that one), then
    # exactly one RECOVERY-BOOT with the recovery occurrence, then END.
    # HQ #356: a variant's contract decides instead which construct request panics; the
    # construct positions after it are never reached.
    panic_key = None
    unreached = set()
    if contract is not None:
        panic_key, unreached = variant["panic_key"], variant["unreached"]
    else:
        for (name, position), case in expected.items():
            if name == "construct" and spi_kernel_hz // frequencies[case] == 0:
                panic_key = (name, position)
    rank = {key: index for index, key in enumerate(
        sorted(expected, key=lambda key: (EXECUTION_ORDER.index(key[0]), key[1])))}
    boot_build = None
    previous_seq = None
    previous_kind = None
    last_kind = None
    phase = "boot"
    recovery_boots = 0
    drv_field_reported = False
    timeout_field_reported = False
    # start -> setup (BOOT, RCC, CAL, QUAL) -> acquire (occurrences) -> panicked -> RECOVERY-BOOT
    # -> recovery_setup -> recovery_acquire -> ended (END).
    state = "start"
    setup_seen = set()
    rcc_lines = []
    cal_lines = {"boot": [], "recovery": []}
    qual_lines = {"boot": [], "recovery": []}
    # Per boot: the first RCC record whose crc32 checks out, whether one failed it, and the
    # ICQUAL records with their crc32 result (the capture-path qualification of that boot).
    path_rcc = {"boot": None, "recovery": None}
    path_rcc_failed = {"boot": False, "recovery": False}
    icqual_lines = {"boot": [], "recovery": []}
    end_seen = False
    # A record whose crc32 fails is reported and still keeps its place in the lifecycle, but it
    # is no evidence: it never counts as a valid transfer or as an observed occurrence, PANIC,
    # RECOVERY-BOOT or END.
    verified_recovery_boots = 0
    occurrences = {}
    order_log = []
    current = None
    # A record is a line ended by its LF; an unterminated fragment at the end is no record.
    pieces = uart_text.split("\n")
    fragment = pieces.pop().rstrip("\r")
    for raw in pieces:
        line = raw.rstrip("\r")
        if not line.startswith("HQ184 "):
            continue
        try:
            kind, seq, fields, crc_ok = _parse_line(line)
        except (ValueError, UnicodeEncodeError):
            fail("uart_field_invalid", line[:80])
            if line.startswith("HQ184 XSUM "):
                _mark_conflict(occurrences, _unparsed_summary_key(line), current)
            continue
        last_kind = kind
        if not crc_ok:
            fail("uart_crc_mismatch", f"{kind} seq={seq}")
        if kind in ("BOOT", "RECOVERY-BOOT"):
            if seq != 0:
                fail("uart_sequence_gap", f"{kind} seq={seq}")
        elif previous_seq is None or seq != previous_seq + 1:
            fail("uart_sequence_gap", f"{kind} seq={seq} after {previous_seq}")
        previous_seq = seq
        try:
            typed = _typed_fields(kind, fields)
        except ValueError as problem:
            fail("uart_field_invalid", f"{kind} seq={seq}: {problem}")
            if kind == "XSUM":
                _mark_conflict(occurrences,
                               _named_key(fields.get("sequence"), fields.get("position")),
                               current)
            previous_kind = kind
            continue
        if state == "ended":
            fail("lifecycle_violation", f"{kind} seq={seq} after END")
        elif kind == "BOOT":
            if state != "start":
                fail("lifecycle_violation", f"a second BOOT at seq={seq}")
            state, setup_seen = "setup", set()
        elif kind == "RECOVERY-BOOT":
            state, setup_seen = "recovery_setup", set()
        elif state == "start":
            fail("lifecycle_violation", f"{kind} seq={seq} before BOOT")
        elif kind in SETUP_KINDS:
            if state not in ("setup", "recovery_setup"):
                fail("lifecycle_violation", f"{kind} seq={seq} after acquisition began")
            elif kind != "RCC" and "RCC" not in setup_seen:
                fail("lifecycle_violation", f"{kind} seq={seq} before the RCC record")
            setup_seen.add(kind)
        elif kind in ("OCC", "PANIC"):
            if state == "setup":
                state = "acquire"
            elif state == "recovery_setup":
                state = "recovery_acquire"
            if kind == "PANIC":
                state = "panicked"
        elif kind in ("XFER", "XSUM"):
            if state not in ("acquire", "recovery_acquire"):
                fail("lifecycle_violation", f"{kind} seq={seq} outside an acquisition phase")
        elif kind == "END":
            state = "ended"
            end_seen = crc_ok
        if kind == "RECOVERY-BOOT":
            recovery_boots += 1
            if crc_ok:
                verified_recovery_boots += 1
            phase = "recovery"
            if previous_kind != "PANIC" or typed["build_id"] != boot_build:
                fail("unexpected_recovery_boot", f"seq={seq}")
        if kind == "BOOT":
            boot_build = typed["build_id"]
            if variant is None:
                if typed["embassy_rev"] != EMBASSY_REV:
                    fail("provenance_incomplete", "BOOT embassy_rev is not the pinned commit")
            elif typed["embassy_rev"] != variant["boot_rev"]:
                fail("provenance_incomplete",
                     "BOOT embassy_rev is not the pinned commit with the variant's patch")
            if (typed["cases_sha256"] != _sha256(inputs["input_cases"])
                    or typed["measurement_sha256"] != _sha256(inputs["input_measurement"])):
                fail("input_digest_mismatch", "BOOT digests differ from the frozen inputs")
        elif kind == "RCC":
            rcc_lines.append((phase, typed))
            if path_rcc[phase] is None:
                if crc_ok:
                    path_rcc[phase] = typed
                else:
                    path_rcc_failed[phase] = True
        elif kind == "ICQUAL":
            icqual_lines[phase].append((typed, crc_ok))
        elif kind == "CAL":
            cal_lines[phase].append(typed)
        elif kind == "QUAL":
            qual_lines[phase].append(typed)
            if strict and "timeout" not in typed and not timeout_field_reported:
                timeout_field_reported = True
                fail("uart_field_invalid", "capture records carry no timeout flag")
        elif kind in ("OCC", "PANIC"):
            occurrence_key = (typed["sequence"], typed["position"])
            current = None
            if (occurrence_key not in expected or typed["case"] != expected[occurrence_key]
                    or occurrence_key in occurrences):
                fail("uart_field_invalid", f"{kind} for an unknown or repeated occurrence")
            elif occurrence_key in unreached:
                fail("contract_violation",
                     f"{kind} at {occurrence_key}, after the construct panic of the contract")
            elif kind == "PANIC" and occurrence_key != panic_key:
                fail("uart_field_invalid" if contract is None else "contract_violation",
                     f"PANIC at {occurrence_key}, which must not panic")
            elif (occurrence_key[0] == "recovery") != (phase == "recovery"):
                fail("uart_field_invalid", f"{kind} on the wrong side of the recovery boot")
            else:
                occurrences[occurrence_key] = {"kind": kind, "fields": typed, "xfers": [],
                                               "summaries": [], "conflict": False,
                                               "cr1_text": fields.get("cr1"),
                                               "crc_ok": crc_ok}
                order_log.append(occurrence_key)
                if kind == "OCC":
                    current = occurrence_key
                    problem = (None if contract is None
                               else _contract_problem(variant["outcomes"][typed["case"]], typed))
                    if problem:
                        fail("contract_violation", f"{occurrence_key}: {problem}")
        elif kind == "XFER":
            occurrence_key = (typed["sequence"], typed["position"])
            # The capture configuration is the one of the divider in force: cfg<BR> of the
            # preceding OCC's CR1.
            in_force = (None if current is None
                        else f"cfg{(occurrences[current]['fields']['cr1'] >> 3) & 7}")
            if (current != occurrence_key or typed["case"] != expected.get(occurrence_key)
                    or typed["cfg"] not in path_ids or typed["cfg"] != in_force):
                fail("uart_field_invalid", "XFER does not match the preceding OCC")
            else:
                # A repeated index is an error, but the record stays in the evaluation so
                # that its data errors are counted; only the first record of an index can
                # count as a valid transfer.
                if any(seen["transfer"] == typed["transfer"]
                       for seen in occurrences[occurrence_key]["xfers"]):
                    fail("uart_field_invalid",
                         f"{occurrence_key} repeats transfer index {typed['transfer']}")
                    typed["duplicate"] = True
                if not crc_ok:
                    typed["corrupt"] = True
                occurrence = occurrences[occurrence_key]
                if occurrence["summaries"] and not occurrence["conflict"]:
                    fail("uart_field_invalid", f"{occurrence_key} mixes XFER with XSUM")
                    occurrence["conflict"] = True
                occurrence["xfers"].append(typed)
            if strict and "drv_err" not in typed and not drv_field_reported:
                drv_field_reported = True
                fail("uart_field_invalid", "XFER records carry no drv_err driver result")
            if strict and "timeout" not in typed and not timeout_field_reported:
                timeout_field_reported = True
                fail("uart_field_invalid", "capture records carry no timeout flag")
        elif kind == "XSUM":
            # HQ #346: one summary per applied configuration, on the capture configuration of
            # the divider in force (cfg<BR> of the preceding OCC's CR1), never beside XFER.
            occurrence_key = (typed["sequence"], typed["position"])
            in_force = (None if current is None
                        else f"cfg{(occurrences[current]['fields']['cr1'] >> 3) & 7}")
            listed = sum(count for _value, count in typed["spans"])
            if (current != occurrence_key or typed["case"] != expected.get(occurrence_key)
                    or typed["cfg"] not in path_ids or typed["cfg"] != in_force):
                fail("uart_field_invalid", "XSUM does not match the preceding OCC")
                _mark_conflict(occurrences, occurrence_key, current)
            elif typed["spans_dropped"] == 0 and listed != typed["clean"] * length:
                # A span the chip could not keep is inconsistent_capture of its own; without
                # one, the counts add up to every byte of every clean transfer.
                fail("uart_field_invalid",
                     f"XSUM seq={seq}: {listed} spans for {typed['clean']} clean transfers")
                _mark_conflict(occurrences, occurrence_key, current)
            else:
                occurrence = occurrences[occurrence_key]
                if (occurrence["summaries"] or occurrence["xfers"]) and not occurrence["conflict"]:
                    fail("uart_field_invalid",
                         f"{occurrence_key} has a second XSUM or mixes it with XFER")
                    occurrence["conflict"] = True
                if not crc_ok:
                    typed["corrupt"] = True
                occurrence["summaries"].append(typed)
        previous_kind = kind

    if fragment:
        fail("uart_field_invalid", fragment[:80])
    if last_kind != "END":
        fail("uart_field_invalid", "the acquire log does not end with END")
    if strict:
        for phase_name in (("boot", "recovery") if recovery_boots else ("boot",)):
            cals = cal_lines[phase_name]
            if len(cals) != 1 or cals[0].get("failed") != 0:
                fail("calibration_failed",
                     f"{phase_name}: expected exactly one CAL record with failed=0")
    if panic_key is not None:
        if panic_key not in occurrences or occurrences[panic_key]["kind"] != "PANIC":
            fail("missing_occurrence", f"{panic_key} must end in PANIC")
        if recovery_boots != 1:
            fail("missing_occurrence", f"expected exactly one RECOVERY-BOOT, saw {recovery_boots}")
    ranks = [rank[key] for key in order_log]
    if ranks != sorted(ranks):
        fail("uart_field_invalid", "occurrences are not in the frozen execution order")

    # Clocks.
    if not any(rcc_phase == "boot" for rcc_phase, _rcc in rcc_lines):
        fail("uart_field_invalid", "the acquire log has no RCC line")
    if strict and phase == "recovery" and not any(
            rcc_phase == "recovery" for rcc_phase, _rcc in rcc_lines):
        fail("uart_field_invalid", "the recovery boot has no RCC line")
    clocks = _checked_clocks([rcc for _rcc_phase, rcc in rcc_lines], hsi_hz, instance,
                             spi_kernel_hz, fail)
    kernel = _kernel_clock(clocks, instance) if clocks else Fraction(spi_kernel_hz)

    # Pad-sampler qualification, once per boot, kept as received; no row is measured with it.
    qualification, _highest_boot = _qualify("boot", qual_lines["boot"], configs, clocks, rule,
                                            fail)
    if qual_lines["recovery"] or strict:
        recovery_entries, _highest_recovery = _qualify("recovery", qual_lines["recovery"],
                                                       configs, clocks, rule, fail)
        qualification += recovery_entries
    evaluation["qualification"] = qualification

    # Capture-path qualification (ICQUAL), once per boot: a row counts only on a capture
    # configuration that passed in its own boot, and its TIM8 clock comes from that boot's
    # RCC registers and TIMPRE.
    capture_qualification = {}
    qualified = {}
    tim8_by_phase = {}
    for phase_name in (("boot", "recovery") if recovery_boots or strict else ("boot",)):
        def phase_fail(code, detail, phase_name=phase_name):
            fail(code, f"{phase_name}: {detail}")
        entries, _timpre, tim8 = _judge_capture_path(
            path_rcc[phase_name], path_rcc_failed[phase_name], icqual_lines[phase_name],
            measurement, phase_fail)
        capture_qualification[phase_name] = entries
        qualified[phase_name] = {entry["cfg"] for entry in entries
                                 if entry["status"] == "observed_pass"}
        tim8_by_phase[phase_name] = tim8
    evaluation["capture_path_qualification"] = capture_qualification

    # Rows: one per frozen occurrence, also for one that was not received.
    rows = []
    for (sequence, position), case in expected.items():
        entry = occurrences.get((sequence, position))
        f_req = frequencies[case]
        if (sequence, position) in unreached:
            rows.append(_not_observed_row(sequence, position, case, f_req, kernel, NOT_REACHED))
            continue
        if entry is None or not entry["crc_ok"]:
            # A missing occurrence is an error of its own; a received one whose OCC or PANIC
            # failed its crc32 is already reported as uart_crc_mismatch, and is no observation.
            if entry is None:
                fail("missing_occurrence", f"{sequence} position {position} ({case})")
            rows.append(_not_observed_row(sequence, position, case, f_req, kernel))
            continue
        fields = entry["fields"]
        panic = entry["kind"] == "PANIC"
        if not panic and fields["f_req"] != f_req:
            fail("uart_field_invalid", f"OCC f_req differs from the frozen case {case}")
        outcome = "panic" if panic else fields["outcome"]
        path = "construct" if panic else fields["path"]
        # The API exercised is part of the frozen occurrence: the initial construction of each
        # set_config sequence, every construct entry and the recovery use the constructor; the
        # other set_config entries use set_config, which may accept or reject.
        expected_path = "set_config" if sequence in SET_CONFIG_SEQUENCES and position >= 0 else "construct"
        if not panic and (path != expected_path or outcome not in ("accepted", "rejected")
                          or (expected_path == "construct" and outcome != "accepted")):
            fail("uart_field_invalid",
                 f"{sequence} position {position}: path {path} / outcome {outcome}, "
                 f"expected the {expected_path} API")
        br = None if panic else (fields["cr1"] >> 3) & 7
        f_br = None if br is None else kernel / 2 ** (br + 1)
        if f_br is not None and Fraction(fields["reported_hz"]) != f_br:
            fail("frequency_readback_mismatch",
                 f"{sequence} position {position}: the driver reported {fields['reported_hz']} Hz, "
                 f"CR1 gives {f_br} Hz")
        quotient = int(Fraction(kernel) // f_req)
        if contract is not None:
            # HQ #356: a variant row agrees with the code when it follows the contract.
            promised = variant["outcomes"][case]
            consistent = (promised["outcome"] == "rejected" if panic
                          else _contract_problem(promised, fields) is None)
        elif quotient == 0:
            consistent = ((outcome == "rejected" and path == "set_config")
                          or (outcome == "panic" and path == "construct"))
        else:
            consistent = outcome == "accepted" and br == _pinned_br(quotient)
        phase_name = "recovery" if sequence == "recovery" else "boot"
        # The valid measurements of the row as (cfg, interval, transfers): one per valid XFER,
        # or one for the valid transfers of its XSUM (HQ #346). An occurrence with a second
        # XSUM, or with XSUM and XFER together, counts nothing.
        valid = []
        if not panic and clocks is not None and not entry["conflict"]:
            tim8 = tim8_by_phase.get(phase_name)
            seen = set()
            for xfer in entry["xfers"]:
                result = _evaluate_transfer(xfer, (sequence, position), case, tim8, tolerance,
                                            length, prng_seed)
                for code in result["codes"]:
                    fail(code, f"{sequence} position {position} transfer {xfer['transfer']}")
                if (not result["codes"] and result["interval"] is not None
                        and not xfer.get("duplicate") and not xfer.get("corrupt")
                        and result["transfer"] not in seen):
                    seen.add(result["transfer"])
                    valid.append((result["cfg"], result["interval"], 1))
            for summary in entry["summaries"]:
                result = _evaluate_summary(summary, (sequence, position), case, tim8, tolerance,
                                           prng_seed)
                for code in result["codes"]:
                    fail(code, f"{sequence} position {position} summary")
                if result["valid"] and not summary.get("corrupt"):
                    valid.append((result["cfg"], result["interval"], result["valid"]))
        count = sum(transfers for _cfg, _interval, transfers in valid)
        if not panic and count < MIN_VALID_TRANSFERS:
            fail("insufficient_transfers", f"{sequence} position {position}: {count} valid")
        lows = [interval[0] for _cfg, interval, _transfers in valid]
        highs = [interval[1] for _cfg, interval, _transfers in valid]
        measured = None
        if valid and max(lows) <= min(highs):
            measured = [str(max(lows)), str(min(highs))]
        if not valid:
            electrical = "not_measured"
        elif any(cfg not in qualified.get(phase_name, set()) for cfg, _interval, _t in valid):
            electrical = "out_of_qualified_range"
        elif all(interval[0] <= f_br <= interval[1] for _cfg, interval, _t in valid):
            electrical = "agrees"
        else:
            electrical = "disagrees"
        if panic:
            status = "observed_panic"
        elif electrical == "agrees" and count >= MIN_VALID_TRANSFERS:
            status = "observed_pass"
        else:
            status = "observed_fail"
        row = {
            "sequence": sequence, "position": position, "case": case, "path": path,
            "f_req_hz": f_req, "k_hz": str(Fraction(kernel)), "outcome": outcome, "br": br,
            "cr1": None if panic else entry["cr1_text"],
            "f_br_hz": None if f_br is None else str(f_br),
            "reported_hz": None if panic else fields["reported_hz"],
            "upstream_code_consistent": consistent,
            "electrical": electrical, "measured_interval_hz": measured,
            "tolerance_ticks": None if panic else tolerance,
            "transfers_valid": count,
            # The capture path observes SCK only; no MOSI bit is seen, so none is counted.
            "data_bit_errors": None,
            "error": None if panic else fields["error"],
            "status": status,
        }
        row.update(_readings(kernel, f_req, outcome, f_br))
        rows.append(row)
    evaluation["rows"] = rows
    verified = {key: entry for key, entry in occurrences.items() if entry["crc_ok"]}
    if contract is not None:
        rejects, error_name = (lambda case: case in variant["rejected"]), "FrequencyTooLow"
    else:
        rejects, error_name = (lambda case: Fraction(kernel) // frequencies[case] == 0), "FrequencyTooHigh"
    evaluation["lifecycle"] = _lifecycle(expected, verified, panic_key, verified_recovery_boots,
                                         end_seen, rejects, error_name)
    return evaluation


def _not_observed_row(sequence, position, case, f_req, kernel, status="not_observed"):
    """The row of a frozen occurrence that was not received (or, with status not_reached, of
    a construct position a variant's contract never reaches): nothing is known but the
    request, so every observed field stays empty."""
    expected_path = ("set_config" if sequence in SET_CONFIG_SEQUENCES and position >= 0
                     else "construct")
    return {
        "sequence": sequence, "position": position, "case": case, "path": expected_path,
        "f_req_hz": f_req, "k_hz": str(Fraction(kernel)), "outcome": None, "br": None,
        "cr1": None, "f_br_hz": None, "reported_hz": None, "upstream_code_consistent": None,
        "reading_ceiling": None, "ceiling_expected_hz": None, "reading_nearest": None,
        "nearest_expected_hz": None, "matches": None, "electrical": "not_measured",
        "measured_interval_hz": None, "tolerance_ticks": None, "transfers_valid": 0,
        "data_bit_errors": None, "error": None, "status": status,
    }


def _lifecycle(expected, occurrences, panic_key, recovery_boots, end_seen, rejects, error_name):
    """Which parts of the frozen lifecycle were received, and the reading that the
    constructor panics where set_config rejects the request with error_name: observed when
    the constructor's PANIC and every set_config rejection of the requests `rejects` names
    with that error were received; contradicted when such a request was accepted or the
    rejection named another error; not_observed otherwise (part of it missing, or the error
    unreported). At c70760de that is a request above the kernel clock and FrequencyTooHigh;
    for a never_exceed variant (HQ #356), one below the kernel clock / 256 and
    FrequencyTooLow."""
    set_config_keys = [key for key in expected if key[0] in SET_CONFIG_SEQUENCES]
    panic_observed = (panic_key is not None and panic_key in occurrences
                      and occurrences[panic_key]["kind"] == "PANIC")
    above = [key for key in set_config_keys if key[1] >= 0 and rejects(expected[key])]
    received = [occurrences[key]["fields"] for key in above if key in occurrences]
    construct_accepted = (panic_key is not None and panic_key in occurrences
                          and occurrences[panic_key]["kind"] == "OCC")
    if (construct_accepted or any(fields.get("outcome") == "accepted" for fields in received)
            or any(fields.get("outcome") == "rejected"
                   and fields.get("error") not in (error_name, "unreported", "none")
                   for fields in received)):
        reading = "contradicted"
    elif (panic_observed and above and len(received) == len(above)
          and all(fields.get("outcome") == "rejected" and fields.get("error") == error_name
                  for fields in received)):
        reading = "observed"
    else:
        reading = "not_observed"
    return {
        "set_config_sequences_complete": all(key in occurrences for key in set_config_keys),
        "panic_observed": panic_observed,
        "recovery_boots": recovery_boots,
        "recovery_occurrence_observed": ("recovery", 0) in occurrences,
        "end_observed": end_seen,
        READING_KEY: reading,
    }


# ---------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------

def _sorted_rows(evaluation):
    def order(row):
        sequence = row.get("sequence")
        rank = SEQUENCE_ORDER.index(sequence) if sequence in SEQUENCE_ORDER else len(SEQUENCE_ORDER)
        return (rank, str(sequence), row.get("position", 0))

    return sorted(evaluation.get("rows", []), key=order)


def _cell(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    text = str(value)
    if any(character in text for character in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def _decimal(text):
    """An exact rational rounded to 0.1 (half to even), written as a decimal."""
    tenths = round(Fraction(text) * 10)
    sign = "-" if tenths < 0 else ""
    return f"{sign}{abs(tenths) // 10}.{abs(tenths) % 10}"


def _before_after_verdict(after_row):
    """not_reached for a row the after run never reached; conforms when the after run
    observed it (applied, or the constructor's panic) and its SCK conforms to the reading
    "never exceed the request"; nonconforming otherwise (failed, not observed, missing, or
    not conforming)."""
    if after_row is not None and after_row.get("status") == NOT_REACHED:
        return NOT_REACHED
    if (after_row is not None and after_row.get("status") in ("observed_pass", "observed_panic")
            and after_row.get("reading_ceiling") == "conforms"):
        return "conforms"
    return "nonconforming"


def render_before_after(before_evaluation, after_evaluation):
    """HQ #356: the before and after tables side by side, as {"csv", "text"}.

    One row per before row, in the before evaluation's order, matched to the after row of the
    same sequence and position. The hz columns are the SCK frequency the register divider
    gives (f_br_hz), empty where there is none (a panic, a row not reached or not observed).
    The text block is ASCII, one line per row: the row id, the API path, the requested,
    before and after frequencies ("none" where empty), both outcomes and statuses, and the
    verdict. Neither evaluation is changed."""
    after_rows = {(row.get("sequence"), row.get("position")): row
                  for row in after_evaluation.get("rows", [])}

    def plain(value):
        return "" if value is None else str(value)

    csv_lines = [",".join(BEFORE_AFTER_COLUMNS)]
    text_lines = []
    for row in before_evaluation.get("rows", []):
        after = after_rows.get((row.get("sequence"), row.get("position")))
        other = after or {}
        verdict = _before_after_verdict(after)
        values = (row.get("sequence"), row.get("position"), row.get("case"), row.get("path"),
                  row.get("f_req_hz"), row.get("outcome"), row.get("f_br_hz"), row.get("status"),
                  row.get("reading_ceiling"), other.get("outcome"), other.get("f_br_hz"),
                  other.get("status"), other.get("reading_ceiling"), verdict)
        csv_lines.append(",".join(plain(value) for value in values))
        text_lines.append(" ".join([
            f"{row.get('sequence')}/{row.get('position')}/{row.get('case')}",
            f"path={_word(row.get('path'))}",
            f"requested={_word(row.get('f_req_hz'))}",
            f"before={_word(row.get('f_br_hz'))}",
            f"after={_word(other.get('f_br_hz'))}",
            f"before_outcome={_word(row.get('outcome'))}",
            f"after_outcome={_word(other.get('outcome'))}",
            f"after_error={_word(other.get('error'))}",
            f"before_status={_word(row.get('status'))}",
            f"after_status={_word(other.get('status'))}",
            f"verdict={verdict}",
        ]))
    return {"csv": "\n".join(csv_lines) + "\n", "text": "\n".join(text_lines) + "\n"}


TABLE_COLUMNS = (
    "sequence", "position", "case", "path", "f_req_hz", "outcome", "br", "cr1", "f_br_hz",
    "reported_hz", "measured_hz", "tolerance_ticks", "electrical", "ceiling", "nearest",
    "matches", "valid", "status",
)


def render_tables(evaluation):
    """tables.csv, evaluation.json and the table block of report.md: the "before" table, one
    row per frozen occurrence with the requested frequency, the raw CR1, the driver-reported
    frequency and the measured interval with its tolerance in TIM8 ticks."""
    rows = _sorted_rows(evaluation)
    csv_lines = [CSV_HEADER]
    for row in rows:
        measured = row.get("measured_interval_hz") or [None, None]
        values = dict(row, measured_lo_hz=measured[0], measured_hi_hz=measured[1])
        csv_lines.append(",".join(_cell(values.get(column)) for column in CSV_COLUMNS))
    markdown = [
        "| " + " | ".join(TABLE_COLUMNS) + " |",
        "| " + " | ".join("---" for _column in TABLE_COLUMNS) + " |",
    ]
    for row in rows:
        measured = row.get("measured_interval_hz")
        measured_text = "" if not measured else f"{_decimal(measured[0])} .. {_decimal(measured[1])}"
        cells = [
            row.get("sequence"), row.get("position"), row.get("case"), row.get("path"),
            row.get("f_req_hz"), row.get("outcome"), row.get("br"), row.get("cr1"),
            row.get("f_br_hz"), row.get("reported_hz"), measured_text,
            row.get("tolerance_ticks"), row.get("electrical"), row.get("reading_ceiling"),
            row.get("reading_nearest"), row.get("matches"), row.get("transfers_valid"),
            row.get("status"),
        ]
        markdown.append("| " + " | ".join("" if cell is None else str(cell) for cell in cells) + " |")
    ordered = dict(evaluation)
    ordered["rows"] = rows
    return {
        "report_block": "\n".join(markdown) + "\n",
        "tables_csv": "\n".join(csv_lines) + "\n",
        "evaluation_json": json.dumps(ordered, sort_keys=True, indent=2) + "\n",
    }


def _word(value):
    if value is None:
        return "none"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def render_qualification(evaluation):
    """The qualification block of report.md: one row per capture configuration, the RCC and
    CAL values of the setup block as key=value text and every error code. ASCII, sorted, so
    the order of the entries does not matter."""
    entries = sorted(evaluation.get("qualification", []),
                     key=lambda entry: (str(entry.get("cfg")), str(entry.get("phase"))))
    lines = [
        "| cfg | phase | highest_passing_step | highest_passing_hz | serves_hz | serves |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        cells = [entry.get("cfg"), entry.get("phase"), entry.get("highest_passing_step"),
                 entry.get("highest_passing_hz"), entry.get("serves_hz"), entry.get("serves")]
        lines.append("| " + " | ".join(_word(cell) for cell in cells) + " |")
    lines.append("")
    setup = evaluation.get("setup") or {}
    clocks = setup.get("clocks")
    if clocks:
        lines.append("RCC: " + " ".join(f"{name}={clocks[name]}" for name in
                                        ("sysclk_hz", "hclk_hz", "pclk1_hz", "pclk2_hz")))
    else:
        lines.append("RCC: none")
    for kind, names in (("CAL", KIND_FIELDS["CAL"]), ("CALISR", KIND_FIELDS["CALISR"])):
        record = setup.get(kind.lower())
        if record:
            lines.append(f"{kind}: " + " ".join(f"{name}={record.get(name)}" for name in names))
        else:
            lines.append(f"{kind}: none")
    lines.append("Setup block: " + " ".join(
        f"{name}={_word(setup.get(name))}" for name in
        ("block_end_seen", "crc_ok", "crc_failed", "qual_records", "trailing_bytes")))
    codes = sorted({str(error.get("code")) for error in evaluation.get("errors", [])})
    lines.append("Errors: " + (", ".join(codes) if codes else "none"))
    return "\n".join(lines) + "\n"


def _cfg_order(entry):
    config_id = str(entry.get("cfg"))
    return (len(config_id), config_id)


def render_capture_path(evaluation):
    """The capture-path block of report.md (HQ #340): one line per frozen configuration, the
    observed ones (passing or failing) apart from the ones not observed, then the setup-block
    counters and every error code. ASCII and sorted, so the order of the entries does not
    matter."""
    entries = sorted(evaluation.get("capture_path", []), key=_cfg_order)
    lines = ["Capture path TIM8_CH2 (PC7) against the TIM2_CH3 reference (PB10), per "
             "configuration:", "", "Observed:"]
    groups = (
        [entry for entry in entries if entry.get("status") != "not_observed"],
        [entry for entry in entries if entry.get("status") == "not_observed"],
    )
    for title, group in zip((None, "Not observed:"), groups):
        if title:
            lines.append(title)
        if not group:
            lines.append("    none")
        for entry in group:
            reasons = "; ".join(str(reason) for reason in entry.get("reasons") or []) or "none"
            lines.append("    " + " ".join([
                str(entry.get("cfg")), str(entry.get("status")),
                f"f_ref_hz={_word(entry.get('f_ref_hz'))}",
                f"period_ticks={_word(entry.get('period_ticks'))}",
                f"serves_hz={_word(entry.get('serves_hz'))}",
                f"serves={_word(entry.get('serves'))}",
                f"reasons={reasons}",
            ]))
    lines.append("")
    setup = evaluation.get("setup") or {}
    lines.append("Setup block: " + " ".join(
        f"{name}={_word(setup.get(name))}" for name in
        ("block_end_seen", "crc_ok", "crc_failed", "qual_records", "icqual_records", "timpre",
         "trailing_bytes")))
    codes = sorted({str(error.get("code")) for error in evaluation.get("errors", [])})
    lines.append("Errors: " + (", ".join(codes) if codes else "none"))
    return "\n".join(lines) + "\n"


def render_junit(evaluation):
    rows = _sorted_rows(evaluation)
    errors = sorted(evaluation.get("errors", []),
                    key=lambda error: (error.get("code", ""), error.get("detail", "")))
    failing = [row for row in rows if row.get("status") == "observed_fail"]
    # HQ #356: a row the run never reached was not measured, so it is reported as skipped,
    # never as a pass. The attribute appears only when there is such a row, so the suite of
    # a bundle without one keeps its bytes.
    skipped = [row for row in rows if row.get("status") == "not_reached"]
    skipped_attribute = f' skipped="{len(skipped)}"' if skipped else ""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="hq184" tests="{len(rows) + len(errors)}" '
        f'failures="{len(errors) + len(failing)}"{skipped_attribute}>',
    ]
    for error in errors:
        lines.append(f'  <testcase classname="hq184.errors" name={quoteattr(error.get("code", ""))}>')
        lines.append(f'    <failure message={quoteattr(error.get("detail", ""))}/>')
        lines.append("  </testcase>")
    for row in rows:
        name = f'{row.get("sequence")}/{row.get("position")}/{row.get("case")}'
        lines.append(f'  <testcase classname="hq184.rows" name={quoteattr(name)}>')
        if row in failing:
            message = (f'status={row.get("status")} electrical={row.get("electrical")} '
                       f'transfers_valid={row.get("transfers_valid")}')
            lines.append(f"    <failure message={quoteattr(message)}/>")
        if row in skipped:
            lines.append(f'    <skipped message={quoteattr("status=not_reached")}/>')
        properties = (("reading_ceiling", row.get("reading_ceiling")),
                      ("reading_nearest", row.get("reading_nearest")),
                      ("matches", row.get("matches")), ("electrical", row.get("electrical")),
                      ("status", row.get("status")))
        lines.append("    <properties>")
        for key, value in properties:
            lines.append(f"      <property name={quoteattr(key)} value={quoteattr(str(value))}/>")
        lines.append("    </properties>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


def render_matrix(evaluation):
    """The matrix block of README.md (HQ #342): every row as sequence/position/case and its
    status, grouped by status (observed_pass, observed_fail, observed_panic, not_observed),
    then the lifecycle and every error code. ASCII and sorted, so the order of the rows does
    not matter."""
    rows = _sorted_rows(evaluation)
    lines = ["Rows of the boundary matrix, by status:", ""]
    # HQ #356: a variant evaluation also lists the construct positions it never reached.
    statuses = ROW_STATUSES + ((NOT_REACHED,) if any(row.get("status") == NOT_REACHED
                                                     for row in rows) else ())
    for status in statuses:
        group = [row for row in rows if row.get("status") == status]
        lines.append(f"{status} ({len(group)}):")
        if not group:
            lines.append("    none")
        for row in group:
            measured = row.get("measured_interval_hz")
            measured_text = ("none" if not measured
                             else f"{_decimal(measured[0])}..{_decimal(measured[1])}")
            lines.append("    " + " ".join([
                f"{row.get('sequence')}/{row.get('position')}/{row.get('case')}", status,
                f"f_req_hz={_word(row.get('f_req_hz'))}", f"cr1={_word(row.get('cr1'))}",
                f"reported_hz={_word(row.get('reported_hz'))}",
                f"measured_hz={measured_text}",
                f"tolerance_ticks={_word(row.get('tolerance_ticks'))}",
                f"electrical={_word(row.get('electrical'))}",
                f"valid={_word(row.get('transfers_valid'))}",
                f"error={_word(row.get('error'))}",
            ]))
    lines.append("")
    lifecycle = evaluation.get("lifecycle") or {}
    lines.append("Lifecycle: " + " ".join(f"{key}={_word(lifecycle.get(key))}"
                                          for key in LIFECYCLE_KEYS))
    codes = sorted({str(error.get("code")) for error in evaluation.get("errors", [])})
    lines.append("Errors: " + (", ".join(codes) if codes else "none"))
    return "\n".join(lines) + "\n"
