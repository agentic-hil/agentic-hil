"""Rebuild the defmt corpus or verify fresh output against its preserved oracle.

`agentic_hil.defmt` decodes defmt frames in Python. This tool can regenerate
the committed fixture corpus for maintenance, or independently rebuild the
firmware and official `defmt-decoder` harness in an isolated workspace and
retain exact parity evidence without changing that corpus.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
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
EXPECTED_COUNTS = {"rzcobs": 46, "raw": 46, "errors": 9}
ORACLE_FILES = {
    "image_rzcobs": FIXTURES / "defmt-fixture-rzcobs.elf",
    "image_raw": FIXTURES / "defmt-fixture-raw.elf",
    "stream_rzcobs": FIXTURES / "stream-rzcobs.bin",
    "stream_raw": FIXTURES / "stream-raw.bin",
    "stream_errors": FIXTURES / "stream-errors.bin",
    "expected_rzcobs": FIXTURES / "expected-rzcobs.jsonl",
    "expected_raw": FIXTURES / "expected-raw.jsonl",
    "expected_errors": FIXTURES / "expected-errors.jsonl",
    "firmware_cargo_lock": FIXTURES / "firmware" / "Cargo.lock",
    "decoder_cargo_lock": FIXTURES / "reference-decoder" / "Cargo.lock",
}


def compare_records(stream: str, sources: dict[str, list[dict]]) -> list[dict]:
    """Compare each named source with the expected records, preserving order."""
    expected = sources["expected"]
    discrepancies = []
    for source_name, records in sources.items():
        if source_name == "expected":
            continue
        for position in range(max(len(expected), len(records))):
            expected_present = position < len(expected)
            actual_present = position < len(records)
            if expected_present != actual_present:
                discrepancies.append(
                    {
                        "stream": stream,
                        "record": position + 1,
                        "field": "$record",
                        "sources": ["expected", source_name],
                        "expected": "present" if expected_present else "missing",
                        "actual": "present" if actual_present else "missing",
                    }
                )
                continue
            if not expected_present:
                continue

            expected_record = expected[position]
            actual_record = records[position]
            fields = list(expected_record)
            fields.extend(field for field in actual_record if field not in expected_record)
            for field in fields:
                expected_value = expected_record.get(field)
                actual_value = actual_record.get(field)
                if field not in expected_record or field not in actual_record or expected_value != actual_value:
                    discrepancies.append(
                        {
                            "stream": stream,
                            "record": position + 1,
                            "field": field,
                            "sources": ["expected", source_name],
                            "expected": expected_value,
                            "actual": actual_value,
                        }
                    )
    return discrepancies


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes())


def json_records(data: str) -> list[dict]:
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def jsonl_bytes(records: list[dict]) -> bytes:
    lines = [json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for record in records]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def python_records(image: Path, stream: bytes) -> list[dict]:
    from agentic_hil.defmt import decode_stream, load_table

    batch = decode_stream(load_table(image), stream)
    records = []
    for record in batch.records:
        if "error" in record:
            records.append({"error": "malformed"})
        else:
            records.append(
                {
                    "index": record["index"],
                    "level": record["level"],
                    "timestamp": record.get("timestamp"),
                    "message": record["message"],
                    "line": record["text"],
                }
            )
    return records


def run_command(command: list[str], *, cwd: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        print(f"command failed with exit code {result.returncode}: {subprocess.list2cmdline(command)}", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        raise RuntimeError("verification command failed")
    return result.stdout


def locked_decoder_version(lockfile: Path) -> str | None:
    text = lockfile.read_text(encoding="utf-8")
    for match in re.finditer(r"(?ms)^\[\[package\]\]\s*\n(.*?)(?=^\[\[package\]\]|\Z)", text):
        package = match.group(1)
        if re.search(r'^name = "defmt-decoder"$', package, re.MULTILINE):
            version = re.search(r'^version = "([^"]+)"$', package, re.MULTILINE)
            return version.group(1) if version else None
    return None


def official_records(decoder: Path, image: Path, stream_path: Path) -> list[dict]:
    result = subprocess.run([str(decoder), str(image), str(stream_path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        print(f"official decoder failed for {stream_path.name}: {result.stderr.strip()}", file=sys.stderr)
        raise RuntimeError("official decoder failed")
    return json_records(result.stdout)


def verification_discrepancy(stream: str, field: str, sources: list[str], expected: object, actual: object) -> dict:
    return {"stream": stream, "field": field, "sources": sources, "expected": expected, "actual": actual}


def evidence_differences(expected: object, actual: object, prefix: str = "evidence") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences = []
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{prefix}.{key}"
            if key not in expected:
                differences.append(f"{child}: unexpected value {actual[key]!r}")
            elif key not in actual:
                differences.append(f"{child}: missing value")
            else:
                differences.extend(evidence_differences(expected[key], actual[key], child))
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        differences = []
        for index in range(max(len(expected), len(actual))):
            child = f"{prefix}[{index}]"
            if index >= len(expected):
                differences.append(f"{child}: unexpected value {actual[index]!r}")
            elif index >= len(actual):
                differences.append(f"{child}: missing value")
            else:
                differences.extend(evidence_differences(expected[index], actual[index], child))
        return differences
    return [] if expected == actual else [f"{prefix}: expected {expected!r}, got {actual!r}"]


def verify(evidence_path: Path, target_dir: Path, *, write: bool) -> int:
    cargo = shutil.which("cargo")
    rustc = shutil.which("rustc")
    if cargo is None or rustc is None:
        print("cargo and rustc must be on PATH for fresh verification", file=sys.stderr)
        return 2

    before_hashes = {name: file_hash(path) for name, path in ORACLE_FILES.items()}
    decoder_version = locked_decoder_version(ORACLE_FILES["decoder_cargo_lock"])
    if decoder_version != "1.2.0":
        print(f"reference Cargo.lock pins defmt-decoder {decoder_version!r}, expected '1.2.0'", file=sys.stderr)
        return 1

    try:
        cargo_version = run_command([cargo, "--version"], cwd=ROOT, environment=os.environ.copy()).strip()
        rustc_details = run_command([rustc, "--version", "--verbose"], cwd=ROOT, environment=os.environ.copy())
    except RuntimeError:
        return 2
    rustc_lines = dict(line.split(": ", 1) for line in rustc_details.splitlines() if ": " in line)
    rustc_version = next((line for line in rustc_details.splitlines() if line.startswith("rustc ")), "unknown")
    host = rustc_lines.get("host", "unknown")

    try:
        with tempfile.TemporaryDirectory(prefix="defmt-reference-verify-") as temporary:
            scratch = Path(temporary)
            isolated_fixtures = scratch / "defmt"
            shutil.copytree(FIXTURES, isolated_fixtures)
            isolated_tests = scratch / "tests"
            isolated_tests.mkdir()
            shutil.copyfile(ROOT / "tests" / "defmt_wire.py", isolated_tests / "defmt_wire.py")
            sys.path[:0] = [str(ROOT / "src"), str(isolated_tests), str(isolated_fixtures)]
            try:
                import scenario

                for name, features, _ in VARIANTS:
                    cargo_environment = {
                        **os.environ,
                        "CARGO_NET_OFFLINE": "true",
                        "DEFMT_LOG": "trace",
                        "CARGO_TARGET_DIR": str(target_dir / "firmware" / name),
                    }
                    run_command(
                        [cargo, "build", "--offline", "--release", "--locked", "--features", features],
                        cwd=isolated_fixtures / "firmware",
                        environment=cargo_environment,
                    )

                decoder_target = target_dir / "decoder"
                decoder_environment = {**os.environ, "CARGO_NET_OFFLINE": "true", "CARGO_TARGET_DIR": str(decoder_target)}
                run_command([cargo, "build", "--offline", "--release", "--locked"], cwd=isolated_fixtures / "reference-decoder", environment=decoder_environment)
                decoder = decoder_target / "release" / ("defmt-reference-decoder" + (".exe" if os.name == "nt" else ""))

                rebuilt_images = {
                    name: target_dir / "firmware" / name / TARGET / "release" / "defmt-fixture"
                    for name, _, _ in VARIANTS
                }
                for name, image in rebuilt_images.items():
                    if not image.is_file():
                        raise RuntimeError(f"firmware build did not produce the {name} image")

                from agentic_hil.defmt import load_table

                regenerated_streams = {
                    name: scenario.stream(load_table(rebuilt_images[name])) for name, _, _ in VARIANTS
                }
                regenerated_streams["errors"] = scenario.error_stream(load_table(rebuilt_images["rzcobs"]))
                for name, data in regenerated_streams.items():
                    (scratch / f"stream-{name}.bin").write_bytes(data)

                evidence_streams = {}
                all_discrepancies = []
                for stream_name in ("rzcobs", "raw", "errors"):
                    image_name = "rzcobs" if stream_name == "errors" else stream_name
                    committed_image = isolated_fixtures / f"defmt-fixture-{image_name}.elf"
                    committed_stream = (isolated_fixtures / f"stream-{stream_name}.bin").read_bytes()
                    regenerated_stream = regenerated_streams[stream_name]
                    committed_stream_path = isolated_fixtures / f"stream-{stream_name}.bin"
                    regenerated_stream_path = scratch / f"stream-{stream_name}.bin"
                    expected_records = json_records((isolated_fixtures / f"expected-{stream_name}.jsonl").read_text(encoding="utf-8"))
                    official_committed = official_records(decoder, committed_image, committed_stream_path)
                    python_committed = python_records(committed_image, committed_stream)
                    official_rebuilt = official_records(decoder, rebuilt_images[image_name], regenerated_stream_path)
                    sources = {
                        "expected": expected_records,
                        "official_committed": official_committed,
                        "python_committed": python_committed,
                        "official_rebuilt": official_rebuilt,
                    }
                    discrepancies = compare_records(stream_name, sources)
                    expected_count = EXPECTED_COUNTS[stream_name]
                    for source_name, records in sources.items():
                        if len(records) != expected_count:
                            discrepancies.append(
                                verification_discrepancy(stream_name, "$count", ["required", source_name], expected_count, len(records))
                            )
                    stream_matches = committed_stream == regenerated_stream
                    if not stream_matches:
                        discrepancies.append(
                            verification_discrepancy(
                                stream_name,
                                "$stream_bytes",
                                ["committed", "regenerated"],
                                sha256(committed_stream),
                                sha256(regenerated_stream),
                            )
                        )
                    for discrepancy in discrepancies:
                        print(json.dumps(discrepancy, ensure_ascii=False, sort_keys=True), file=sys.stderr)
                    all_discrepancies.extend(discrepancies)
                    evidence_streams[stream_name] = {
                        "regenerated_matches_committed": stream_matches,
                        "sources": {
                            source_name: {
                                "record_count": len(records),
                                "jsonl_sha256": sha256(jsonl_bytes(records)),
                            }
                            for source_name, records in sources.items()
                        },
                        "discrepancies": discrepancies,
                    }

                after_hashes = {name: file_hash(path) for name, path in ORACLE_FILES.items()}
                changed_inputs = sorted(name for name in before_hashes if before_hashes[name] != after_hashes[name])
                if changed_inputs:
                    mutation = {"oracle_changed_inputs": changed_inputs}
                    print(json.dumps(mutation, sort_keys=True), file=sys.stderr)
                evidence = {
                    "toolchain": {
                        "cargo": cargo_version,
                        "rustc": rustc_version,
                        "host": host,
                        "python": platform.python_version(),
                        "target": TARGET,
                        "defmt_decoder": decoder_version,
                    },
                    "input_sha256_before": before_hashes,
                    "input_sha256_after": after_hashes,
                    "rebuilt_image_sha256": {name: file_hash(path) for name, path in rebuilt_images.items()},
                    "streams": evidence_streams,
                    "oracle_unchanged": not changed_inputs,
                }
                evidence_bytes = (json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            finally:
                for entry in (str(isolated_fixtures), str(isolated_tests)):
                    with contextlib.suppress(ValueError):
                        sys.path.remove(entry)
                sys.modules.pop("scenario", None)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(f"fresh verification failed: {error}", file=sys.stderr)
        return 1

    if changed_inputs:
        all_discrepancies.append({"oracle_changed_inputs": changed_inputs})
    if write:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(evidence_bytes)
        return 1 if all_discrepancies else 0

    if not evidence_path.is_file():
        print(f"evidence file is missing: {evidence_path}", file=sys.stderr)
        return 1
    existing_bytes = evidence_path.read_bytes()
    evidence_matches = existing_bytes == evidence_bytes
    if not evidence_matches:
        try:
            existing_evidence = json.loads(existing_bytes.decode("utf-8"))
            expected_evidence = json.loads(evidence_bytes.decode("utf-8"))
            differences = evidence_differences(expected_evidence, existing_evidence)
            if differences:
                for difference in differences:
                    print(difference, file=sys.stderr)
            else:
                print("evidence differs in serialization", file=sys.stderr)
        except (UnicodeDecodeError, json.JSONDecodeError):
            print("evidence is not valid UTF-8 JSON", file=sys.stderr)
    return 0 if evidence_matches and not all_discrepancies else 1


def regenerate(target_dir: Path | None) -> int:
    cargo = shutil.which("cargo")
    if cargo is None:
        print("cargo is not on PATH", file=sys.stderr)
        return 2
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(FIXTURES)]
    import scenario

    from agentic_hil.defmt import load_table

    with tempfile.TemporaryDirectory(prefix="defmt-reference-") as scratch:
        build_dir = target_dir or Path(scratch)
        environment = {**os.environ, "DEFMT_LOG": "trace", "CARGO_TARGET_DIR": str(build_dir / "firmware")}
        for _, features, image in VARIANTS:
            subprocess.run([cargo, "build", "--release", "--locked", "--features", features], cwd=FIXTURES / "firmware", env=environment, check=True)
            shutil.copyfile(build_dir / "firmware" / TARGET / "release" / "defmt-fixture", FIXTURES / image)
        decoder_environment = {**os.environ, "CARGO_TARGET_DIR": str(build_dir / "decoder")}
        subprocess.run([cargo, "build", "--release", "--locked"], cwd=FIXTURES / "reference-decoder", env=decoder_environment, check=True)
        decoder = build_dir / "decoder" / "release" / ("defmt-reference-decoder" + (".exe" if os.name == "nt" else ""))

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target-dir", help="Cargo target directory; a temporary one by default.")
    parser.add_argument("--verify", action="store_true", help="verify fresh output without changing the committed corpus")
    parser.add_argument("--evidence", help="evidence JSON path used by fresh verification")
    parser.add_argument("--write", action="store_true", help="write measured evidence, even when comparisons disagree")
    args = parser.parse_args(argv)
    if args.write and not args.verify:
        parser.error("--write requires --verify")
    if args.verify and not args.evidence:
        parser.error("--verify requires --evidence")
    if args.evidence and not args.verify:
        parser.error("--evidence requires --verify")
    target_dir = Path(args.target_dir).resolve() if args.target_dir else None
    if args.verify:
        evidence_path = Path(args.evidence)
        if not evidence_path.is_absolute():
            evidence_path = ROOT / evidence_path
        return verify(evidence_path, target_dir or ROOT / "target", write=args.write)
    return regenerate(target_dir)


if __name__ == "__main__":
    raise SystemExit(main())
