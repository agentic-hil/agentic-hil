"""Delivery acceptance for the decoder, its immutable corpus and Rust-free replay."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import yaml

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from support import scaled_time_bound
from test_defmt_corpus import FIXTURE_BLOB_OIDS

from agentic_hil import defmt

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/defmt"

PRESERVED_MYPY_FILES = ['src/agentic_hil/artifacts.py', 'src/agentic_hil/backends/rs3005p.py', 'src/agentic_hil/bench.py', 'src/agentic_hil/config.py', 'src/agentic_hil/configstate.py', 'src/agentic_hil/configwrite.py', 'src/agentic_hil/contracts.py', 'src/agentic_hil/coordination.py', 'src/agentic_hil/device_access.py', 'src/agentic_hil/mcp.py', 'src/agentic_hil/power_supply.py', 'src/agentic_hil/redact.py', 'src/agentic_hil/report.py', 'src/agentic_hil/stdio.py', 'src/agentic_hil/tools.py', 'src/agentic_hil/types.py']
PRESERVED_MANIFEST_LINES = ['include README.md', 'include AGENTS.md', 'include AI_AGENT_QUICKSTART.md', 'include TROUBLESHOOTING.md', 'include SECURITY.md', 'include LICENSE', 'include docs/mcp-hosts.md', 'include docs/safety-model.md', 'include docs/security-design.md', 'include docs/installation.md', 'recursive-include src/agentic_hil/schemas *.json', 'recursive-include src/agentic_hil/templates *.json', 'recursive-include src/agentic_hil/skills *.md', 'recursive-include tests *.py', 'recursive-include tests/fixtures *.json', 'exclude tests/test_install_eval*.py', 'exclude tests/test_windows_install_eval_scripts.py', 'exclude tests/test_hil_eval_probe.py', 'exclude tests/test_loop_in_container.py', 'exclude tests/test_shipped_references.py', 'exclude tests/test_version_consistency.py', 'exclude tests/test_ci_examples.py', 'exclude tests/test_release_requirements.py', 'exclude tests/test_shipped_profiles.py', 'exclude tests/test_verify_published_examples.py', 'exclude tests/test_record_cli_surface.py', 'exclude tests/test_ci_linux.py', 'exclude tests/test_bench_in_container.py', 'exclude tests/test_bench_distributions.py', 'exclude tests/test_install_cmsis_pack.py', 'exclude tests/test_check_image_pip_locks.py', 'exclude tests/test_bench_image_store.py', 'exclude tests/test_run_lock_command.py', 'exclude tests/test_tool_definition_score.py', 'exclude tests/test_tool_definition_score_model.py', 'exclude tests/test_withhold.py', 'exclude tests/test_agent_registration_gate.py', 'exclude tests/test_bench_battery.py', 'exclude tests/test_bench_can_peer_statistics.py', 'exclude tests/test_bench_esp32_images.py', 'exclude tests/container/test_protocol_startup_refusal.py', 'exclude tests/test_install_scripts.py', 'exclude tests/test_process_table_grouping.py', 'exclude tests/test_macos_installer_recording.py', 'exclude tests/test_readthedocs_moved.py', 'recursive-include examples *.py *.md *.yaml *.yml', 'prune examples/nucleo-f446re_demo']
PRESERVED_COLLECTION_STEP = {'name': "Collect the source distribution's tests", 'shell': 'bash', 'run': 'rm -rf sdist-check\nmkdir sdist-check\ntar xzf dist/agentic_hil-*.tar.gz -C sdist-check\ncd sdist-check/agentic_hil-*\npython -m pytest --collect-only -q\n'}
PRESERVED_CHANGELOG_PARAGRAPH = '- Native defmt decoding on serial lines. `com_session_start` with `defmt: true` decodes the line with the table of the ELF the last confirmed `flash_firmware` wrote, checked again by its digest, and `defmt: {"image_path": ...}` names the ELF of a board flashed some other way (`proven: false` unless it is the flashed file). `com_read` on such a session hands out whole decoded frames (level, timestamp, format string, typed arguments, rendered message and the line `defmt-print` prints) instead of text and hex, and `until` matches a frame\'s line. `flash_firmware` takes `capture.defmt` and returns the decoded boot output of the image it flashed. Bytes that do not decode are records with `error` and their bytes, never skipped. Plan format version 7 adds `defmt:` on `uart_open` and the `defmt_expect` action, which waits for a frame by `level`, `format`, `text` or `pattern`, and typed `args`; frames after the match stay for the next step. New error types: `defmt_table_not_available`, `defmt_table_changed`, `defmt_table_invalid`, `defmt_expect_timeout`. The decoder (`agentic_hil.defmt`) is plain Python for defmt wire versions 3 and 4 (defmt 0.3 and 1.x), both the rzCOBS and the raw encoding, every parameter type and display hint, and is held to the official `defmt-decoder` 1.2 on a real cross-compiled image recorded under `tests/fixtures/defmt/` by `tools/defmt_reference.py`.'
def test_decoder_is_in_the_configured_mypy_scope():
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    files = tomllib.loads(source)["tool"]["mypy"]["files"]
    if "src/agentic_hil/defmt.py" not in files:
        raise AssertionError("native defmt decoder must be an explicit mypy target")
    assert set(PRESERVED_MYPY_FILES) <= set(files)


def test_manifest_includes_replay_fixtures():
    source = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    lines = [line.strip() for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    positions = [lines.index(line) for line in PRESERVED_MANIFEST_LINES]
    assert positions == sorted(positions)
    assert all(line in lines for line in PRESERVED_MANIFEST_LINES)

    def applies(line: str, path: str) -> bool:
        parts = line.split()
        directive = parts[0]
        if directive in {"include", "exclude"}:
            return any(fnmatch.fnmatchcase(path, pattern) for pattern in parts[1:])
        if directive in {"global-include", "global-exclude"}:
            return any(fnmatch.fnmatchcase(Path(path).name, pattern) for pattern in parts[1:])
        if directive in {"recursive-include", "recursive-exclude"}:
            directory = parts[1].rstrip("/")
            return path.startswith(directory + "/") and any(fnmatch.fnmatchcase(Path(path).name, pattern) for pattern in parts[2:])
        if directive in {"graft", "prune"}:
            directory = parts[1].rstrip("/")
            return path.startswith(directory + "/")
        return False

    def is_addition(line: str) -> bool:
        return line.split()[0] in {"include", "global-include", "recursive-include", "graft"}

    def included(path: str) -> bool:
        result = False
        for line in lines:
            if applies(line, path):
                result = is_addition(line)
        return result

    elf_paths = (
        "tests/fixtures/defmt/defmt-fixture-raw.elf",
        "tests/fixtures/defmt/defmt-fixture-rzcobs.elf",
    )
    firmware_sources = (
        "tests/fixtures/defmt/firmware/Cargo.toml",
        "tests/fixtures/defmt/firmware/Cargo.lock",
        "tests/fixtures/defmt/firmware/build.rs",
        "tests/fixtures/defmt/firmware/memory.x",
        "tests/fixtures/defmt/firmware/.cargo/config.toml",
        "tests/fixtures/defmt/firmware/src/main.rs",
    )
    has_elf_rule = any(is_addition(line) and any(applies(line, path) for path in elf_paths) for line in lines)
    if has_elf_rule or not all(included(path) for path in firmware_sources):
        raise AssertionError("defmt source distributions must ship firmware sources instead of ELF images")


def _shell_commands(script):
    """Read active shell commands, excluding comments and heredoc bodies."""
    commands = []
    heredoc = None
    pending = ""
    for line in script.splitlines():
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        pending += line
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        lexer = shlex.shlex(pending, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
        pending = ""
        if not tokens:
            continue
        commands.append(tokens)
        for index, token in enumerate(tokens):
            if token in {"<<", "<<-"}:
                heredoc = tokens[index + 1]
    assert not pending and heredoc is None, "unfinished replay shell command"
    return commands


def _validate_ci_replay_step(replay):
    condition = replay.get("if", True)
    assert condition is True or (isinstance(condition, str) and re.sub(r"\s+", "", condition) in {"true", "${{true}}", "success()", "${{success()}}", "always()", "${{always()}}"}), "CI replay must run on every successful matrix leg"
    assert replay["shell"] == "bash"
    assert str(replay.get("env", {}).get("PIP_NO_INDEX")) == "1"
    assert not replay.get("continue-on-error", False)
    commands = _shell_commands(replay["run"])
    # GitHub's bash shell propagates failures. Do not let shell control flow,
    # background jobs, or an explicit error override suppress the replay.
    for tokens in commands:
        assert tokens[0] not in {"if", "then", "else", "elif", "fi", "for", "while", "until", "case", "function", "trap", "!", "exit", "return", ":", "true"}
        assert not any(token in {"&&", "||", ";", "&", "{", "}"} for token in tokens)
        assert not (tokens[0] == "set" and any(token.startswith("+") and "e" in token for token in tokens[1:]))
        assert not (tokens[0] == "set" and tokens[1:] == ["+o", "errexit"])
    active = "\n".join(" ".join(tokens) for tokens in commands)
    directory_changes = [(index, tokens) for index, tokens in enumerate(commands) if tokens[0] in {"cd", "pushd", "popd"}]
    captures = [(index, tokens[0].split("=", 1)[0]) for index, tokens in enumerate(commands) if re.match(r"^[A-Za-z_]\w*=", tokens[0]) and "sys.executable" in " ".join(tokens)]
    assert len(captures) == 1, "capture the absolute Python before hiding Rust"
    capture_index, variable = captures[0]
    path_indices = [index for index, tokens in enumerate(commands) if any(token.startswith("PATH=") for token in tokens)]
    assert path_indices and capture_index < min(path_indices)
    script = "\n".join(line for line in replay["run"].splitlines() if not line.lstrip().startswith("#"))
    assert all(tool in script for tool in ("cargo", "rustc", "rustup")) and "shutil.which" in script
    invocations = [(index, tokens) for index, tokens in enumerate(commands) if tokens[0] in {"$" + variable, "${" + variable + "}"} and tokens[1:3] == ["-m", "pytest"]]
    assert len(invocations) == 1
    index, invocation = invocations[0]
    assert max(path_indices) < index
    preceding_changes = [tokens for position, tokens in directory_changes if position < index]
    assert preceding_changes and preceding_changes[-1] == ["cd", "sdist-check/agentic_hil-*"], "enter the extracted distribution before replay"
    assert {"tests/test_defmt.py", "tests/test_defmt_regressions.py"} <= set(invocation[3:])
    assert not any(token in active for token in ("--collect-only", "--deselect", "--ignore", "--lf", "--last-failed", "PYTHONPATH"))
    assert not ({"-k", "-m"} & set(invocation[3:]))
    assert not re.search(r"\b(?:cargo|rustc|rustup)\s+(?:run|build|install)", active)


def test_ci_replays_the_extracted_source_distribution():
    source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    matches = [(job, index, step) for job in workflow["jobs"].values() for index, step in enumerate(job.get("steps", [])) if step.get("name") == "Replay native defmt from the source distribution"]
    if not matches:
        raise AssertionError("CI must replay the extracted defmt corpus without Rust")
    assert len(matches) == 1
    job, index, replay = matches[0]
    assert job["strategy"]["matrix"]
    collection_index, collection = next((i, step) for i, step in enumerate(job["steps"]) if step.get("name") == "Collect the source distribution's tests")
    assert collection == PRESERVED_COLLECTION_STEP
    assert collection_index < index
    _validate_ci_replay_step(replay)


def test_documented_replay_examples_match_public_results(defmt_images):
    table = defmt.load_table_bytes(defmt_images["rzcobs"].read_bytes())
    batch = defmt.decode_stream(table, (FIXTURES / "stream-rzcobs.bin").read_bytes())
    expected = [json.loads(line)["line"] for line in (FIXTURES / "expected-rzcobs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(batch.records) == 46 and [record["text"] for record in batch.records] == expected
    assert table == defmt.load_table(defmt_images["rzcobs"])
    raw = defmt.load_table(defmt_images["raw"])
    raw_batch = defmt.decode_stream(raw, (FIXTURES / "stream-raw.bin").read_bytes())
    raw_expected = [json.loads(line)["line"] for line in (FIXTURES / "expected-raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(raw_batch.records) == 46 and [record["text"] for record in raw_batch.records] == raw_expected
    docs = (ROOT / "docs/testing.md").read_text(encoding="utf-8")
    for term in ("tests/test_defmt.py", "tests/test_defmt_regressions.py", "tests/fixtures/defmt", "stream-rzcobs.bin", "stream-raw.bin", "expected-rzcobs.jsonl", "load_table_bytes", "decode_stream", "DEFMT_RZCOBS_ELF", "DEFMT_RAW_ELF", "raw_hex", "size_bytes", "raw_truncated", "64", ":cbor", "DWARF"):
        assert term in docs, term
    text = docs.lower()
    for concepts in (("rust-free", "without rust"), ("regenerat",), ("optional",), ("desynchron",), ("incomplete",), ("final",), ("consumed",), ("error",), ("full", "whole", "entire"), ("byte",), ("source distribution", "sdist")):
        assert any(concept in text for concept in concepts), concepts
    # Run the actual Python replay example rather than only pinning words.
    examples = re.findall(r"```python\s*\n(.*?)```", docs, re.DOTALL)
    matching = [example for example in examples if "load_table_bytes" in example and "decode_stream" in example]
    assert matching, "document a runnable example using both public operations"
    for example in matching:
        environment = {
            **os.environ,
            "PYTHONPATH": str(Path(defmt.__file__).resolve().parents[1]),
            "PYTHONIOENCODING": "utf-8",
            "DEFMT_RAW_ELF": str(defmt_images["raw"].resolve()),
            "DEFMT_RZCOBS_ELF": str(defmt_images["rzcobs"].resolve()),
        }
        result = subprocess.run([sys.executable, "-c", example], cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", check=True, timeout=scaled_time_bound(30))
        assert result.stdout == "\n".join(expected) + "\n"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert PRESERVED_CHANGELOG_PARAGRAPH in changelog
    start = changelog.index(PRESERVED_CHANGELOG_PARAGRAPH)
    preceding = changelog[:start]
    heading = list(re.finditer(r"(?m)^#{2,6} .+$", preceding))[-1]
    assert re.search(r"pending[ -]integration", heading.group(), re.IGNORECASE)
    end = re.search(r"(?m)^#{2,6} ", changelog[start:])
    section = changelog[heading.start(): start + end.start() if end else len(changelog)]
    assert all(issue in section for issue in ("#809", "#810", "#811", "#813"))
    assert re.search(r"decoder[ -]only", section, re.IGNORECASE)
    assert re.search(r"(?:separate|pending|remain|unverified)", section, re.IGNORECASE)
    assert re.search(r"board|qualification", section, re.IGNORECASE)
    other_bullets = [line for line in changelog.replace(PRESERVED_CHANGELOG_PARAGRAPH, "").splitlines() if line.startswith("- ") and "#809" in line]
    assert other_bullets and any("decoder" in line.lower() for line in other_bullets)


def _run(arguments, cwd, *, env=None, input=None, timeout=180):
    result = subprocess.run([sys.executable, *arguments], cwd=cwd, env=env, input=input, capture_output=True, text=True, timeout=scaled_time_bound(timeout))
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _blob_oid(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _safe_member(root, name):
    destination = (root / name).resolve()
    assert destination.is_relative_to(root.resolve()), name
    return destination


# Every permitted Python child installs these guards before executing its
# requested code. Further descendants and all unaudited launches are refused.
PACKAGED_AUDIT = r'''
import os
import sys
from pathlib import Path

def install_audit(root, fixture_root, permitted=None):
    root = Path(root).resolve()
    fixture_root = Path(fixture_root).resolve()
    reads = set()
    def library_lookup():
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_globals.get("__name__") == "ctypes.util":
                return True
            frame = frame.f_back
        return False
    def audit(event, args):
        # The unchanged autouse fixture reserves an ephemeral loopback port.
        # Construction and that local bind do not communicate with a peer.
        if event == "socket.bind" and args[1] != ("127.0.0.1", 0):
            raise AssertionError("packaged replay must not use a network")
        if event in {"socket.connect", "socket.connect_ex", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "urllib.Request"}:
            raise AssertionError("packaged replay must not use a network")
        if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "pty.spawn"}:
            # Python 3.13 and later go on from subprocess.Popen to os.posix_spawn
            # with the same executable and argv, so both meet the same key.
            if event not in {"subprocess.Popen", "os.posix_spawn"} or permitted is None:
                raise AssertionError("packaged replay forbids unaudited subprocesses")
            command = args[1] if isinstance(args[1], str) else tuple(args[1])
            key = (str(Path(os.fsdecode(args[0])).resolve()) if args[0] is not None else None, command)
            # Collecting the whole sdist imports pyOCD, whose J-Link probe calls
            # ctypes.util.find_library at import time. On Linux that lookup runs
            # ldconfig and the compiler tools, and it is the only caller allowed
            # through without a key.
            assert key in permitted or library_lookup(), "packaged replay forbids unaudited subprocesses"
        if event == "import":
            assert not any(name in str(args[0]).lower() for name in ("defmt_reference", "reference-decoder")), "packaged replay forbids regeneration imports"
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if "fixtures" in path.parts and "defmt" in path.parts and (args[1] is None or "r" in str(args[1])):
                assert path.is_relative_to(fixture_root), path
                reads.add(str(path))
    sys.addaudithook(audit)
    return reads
'''


# The driver records real imports and fixture opens. Python -c helpers receive
# PACKAGED_AUDIT as a prefix; the audit event itself checks the wrapped argv.
PACKAGED_DRIVER = "AUDIT_SOURCE = " + repr(PACKAGED_AUDIT) + "\n" + r'''
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

packet = json.loads(sys.stdin.read())
root = Path(packet["root"]).resolve()
assert all(shutil.which(tool) is None for tool in ("cargo", "rustc", "rustup"))
if sys.platform == "win32":
    host = platform.uname()
    windows_platform = {
        "system": host.system,
        "node": host.node,
        "release": host.release,
        "version": host.version,
        "machine": host.machine,
    }
    native = sys.getwindowsversion()
    windows_version = {"major": native.major, "minor": native.minor, "build": native.build}
    assert os.name == "nt" and windows_platform["system"] == "Windows"
    assert all(isinstance(value, str) and value for value in windows_platform.values())
    assert native.major > 0 and native.minor >= 0 and native.build >= 0
    windows_platform["windows_version"] = windows_version
permitted = set()
exec(AUDIT_SOURCE)
reads = install_audit(root, packet["fixture_root"], permitted)
if sys.platform == "win32":
    assert platform.system() == windows_platform["system"]
    host = platform.uname()
    assert {
        "system": host.system,
        "node": host.node,
        "release": host.release,
        "version": host.version,
        "machine": host.machine,
    } == {key: windows_platform[key] for key in ("system", "node", "release", "version", "machine")}
    native = sys.getwindowsversion()
    assert {"major": native.major, "minor": native.minor, "build": native.build} == windows_version
    print("PACKAGED_PLATFORM=" + json.dumps(windows_platform))
descendant_bootstrap = AUDIT_SOURCE + "\ninstall_audit(" + repr(str(root)) + ", " + repr(packet["fixture_root"]) + ")\n"
original_popen = subprocess.Popen

class AuditedPopen(original_popen):
    def __init__(self, args, *positional, **keywords):
        key = None
        if isinstance(args, (list, tuple)) and len(args) == 3 and str(Path(args[0]).resolve()) == str(Path(sys.executable).resolve()) and args[1] == "-c" and not keywords.get("shell", False) and keywords.get("executable") in (None, sys.executable):
            args = [args[0], "-c", descendant_bootstrap + args[2]]
            keywords["executable"] = sys.executable
            command = subprocess.list2cmdline(args) if os.name == "nt" else tuple(args)
            key = (str(Path(args[0]).resolve()), command)
            permitted.add(key)
        try:
            super().__init__(args, *positional, **keywords)
        finally:
            if key is not None:
                permitted.remove(key)

subprocess.Popen = AuditedPopen

# Exercise the installed guards in real children, without touching a network
# or invoking a tool. A child cannot start an uninstrumented grandchild either.
control = subprocess.run([sys.executable, "-c", "print('audited-child-ready')"], capture_output=True, text=True, timeout=15)
assert control.returncode == 0 and control.stdout.strip() == "audited-child-ready", control.stderr
reservation = "import socket\nwith socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unused:\n    unused.bind(('127.0.0.1', 0))\n    assert unused.getsockname()[1] > 0\nprint('port-reservation-ready')"
control = subprocess.run([sys.executable, "-c", reservation], capture_output=True, text=True, timeout=15)
assert control.returncode == 0 and control.stdout.strip() == "port-reservation-ready", control.stderr
probes = [
    ("import socket; socket.socket().connect(('127.0.0.1', 9))", "packaged replay must not use a network"),
    ("import socket; socket.getaddrinfo('example.invalid', 443)", "packaged replay must not use a network"),
    ("import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 9))", "packaged replay must not use a network"),
    ("import socket; socket.socket().bind(('0.0.0.0', 0))", "packaged replay must not use a network"),
    ("import subprocess; subprocess.run(['cargo', '--version'])", "packaged replay forbids unaudited subprocesses"),
    ("import subprocess, sys; subprocess.run([sys.executable, '-c', 'import socket; socket.socket()'])", "packaged replay forbids unaudited subprocesses"),
    ("import defmt_reference", "packaged replay forbids regeneration imports"),
]
for source, message in probes:
    child = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=15)
    assert child.returncode != 0 and message in child.stderr, child.stderr
try:
    original_popen([sys.executable, "-c", "import socket; socket.socket()"])
except AssertionError as error:
    assert str(error) == "packaged replay forbids unaudited subprocesses"
else:
    raise AssertionError("an unaudited Python child was allowed")

sys.path.insert(0, str(root / packet["import_path"]))
from agentic_hil import defmt, elfsymbols
assert Path(defmt.__file__).resolve().is_relative_to(root)
assert Path(elfsymbols.__file__).resolve().is_relative_to(root)
fixtures = Path(packet["fixtures"]).resolve()
assert fixtures.is_relative_to(Path(packet["fixture_root"]).resolve())

if packet["mode"] == "pytest":
    import pytest
    class Results:
        def __init__(self):
            self.calls = []
            self.skipped = 0
            self.deselected = 0
            self.collected = 0
        def pytest_collection_finish(self, session):
            self.collected = len(session.items)
        def pytest_deselected(self, items):
            self.deselected += len(items)
        def pytest_runtest_logreport(self, report):
            if report.skipped or hasattr(report, "wasxfail"):
                self.skipped += 1
            if report.when == "call":
                self.calls.append(report.outcome)
    results = Results()
    code = pytest.main(packet["arguments"] + ["-p", "no:cacheprovider", "-o", "addopts="], plugins=[results])
    assert code == 0, code
    if not packet.get("collect"):
        assert results.collected == 73 and results.deselected == 0
        assert results.calls == ["passed"] * (results.collected - results.skipped), results.calls
        assert results.skipped > 0
        for name in ("defmt_wire", "scenario", "test_defmt", "test_defmt_regressions"):
            assert Path(sys.modules[name].__file__).resolve().is_relative_to(root), name
        expected_reads = {str((fixtures / name).resolve()) for name in packet["required_reads"]}
        assert expected_reads <= reads, expected_reads - reads
else:
    # Wheel imports are isolated in another fresh child. The wheel contains
    # implementation; protected replay inputs come from the extracted sdist.
    descriptions = {}
    for encoding in ("raw", "rzcobs"):
        image = Path(packet["images"][encoding])
        table = defmt.load_table(image)
        assert table == defmt.load_table_bytes(image.read_bytes())
        descriptions[encoding] = table.describe()
        data = (fixtures / ("stream-" + encoding + ".bin")).read_bytes()
        records = defmt.decode_stream(table, data).records
        oracle = [json.loads(line) for line in (fixtures / ("expected-" + encoding + ".jsonl")).read_text(encoding="utf-8").splitlines()]
        assert len(records) == len(oracle) == 46
        assert [record["text"] for record in records] == [record["line"] for record in oracle]
    assert descriptions == packet["descriptions"]
    table = defmt.load_table(Path(packet["images"]["rzcobs"]))
    data = (fixtures / "stream-errors.bin").read_bytes()
    batch = defmt.decode_stream(table, data)
    oracle = [json.loads(line) for line in (fixtures / "expected-errors.jsonl").read_text(encoding="utf-8").splitlines()]
    actual = [{"error": "malformed"} if "error" in record else {"index": record["index"], "level": record["level"], "timestamp": record.get("timestamp"), "message": record["message"], "line": record["text"]} for record in batch.records]
    assert actual == oracle and batch.consumed == len(data) - 2
    assert defmt.decode_stream(table, data, final=True).records[-1]["error"] == "incomplete_frame"
print("PACKAGED_REPLAY=" + json.dumps({"reads": sorted(reads), "decoder": defmt.__file__}))
'''


def test_distributions_collect_and_replay_without_rust(tmp_path, defmt_images):
    table = defmt.load_table(defmt_images["rzcobs"])
    descriptions = {"rzcobs": table.describe(), "raw": defmt.load_table(defmt_images["raw"]).describe()}
    expected = defmt.decode_stream(table, (FIXTURES / "stream-rzcobs.bin").read_bytes()).records
    oracle = [json.loads(line) for line in (FIXTURES / "expected-rzcobs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(expected) == 46 and [record["text"] for record in expected] == [record["line"] for record in oracle]
    checkout = tmp_path / "copy"
    excluded = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", ".tox", ".nox"}
    def ignore(directory, names):
        ignored = []
        for name in names:
            path = Path(directory) / name
            if name in excluded or name.endswith(".egg-info") or path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()) or path.resolve() == tmp_path.resolve():
                ignored.append(name)
        return ignored
    shutil.copytree(ROOT, checkout, ignore=ignore)
    dist = tmp_path / "dist"
    _run(["-m", "build", "--no-isolation", "--outdir", str(dist)], checkout)
    sdists, wheels = list(dist.glob("*.tar.gz")), list(dist.glob("*.whl"))
    assert len(sdists) == len(wheels) == 1
    _run(["-m", "twine", "check", str(sdists[0]), str(wheels[0])], checkout)
    extracted = tmp_path / "sdist"
    extracted.mkdir()
    packaged_oids = {name: oid for name, oid in FIXTURE_BLOB_OIDS.items() if "/firmware/" not in name and "/reference-decoder/" not in name}
    with tarfile.open(sdists[0], "r:gz") as archive:
        members = archive.getmembers()
        prefixes = {member.name.split("/")[0] for member in members}
        assert len(prefixes) == 1
        prefix = prefixes.pop()
        by_name = {member.name: member for member in members}
        for name in ("src/agentic_hil/defmt.py", "src/agentic_hil/elfsymbols.py", "tests/test_defmt.py", "tests/test_defmt_regressions.py", "tests/defmt_wire.py", *packaged_oids):
            member = by_name[prefix + "/" + name]
            assert member.isfile()
            content = archive.extractfile(member).read()
            assert hashlib.sha256(content).digest() == hashlib.sha256((ROOT / name).read_bytes()).digest(), name
            if name in packaged_oids:
                assert _blob_oid(content) == packaged_oids[name], name
        # Extract regular files/directories only, validating every destination.
        for member in members:
            target = _safe_member(extracted, member.name)
            assert member.isdir() or member.isfile(), member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    package = extracted / prefix
    wheel_root = tmp_path / "wheel"
    wheel_root.mkdir()
    with zipfile.ZipFile(wheels[0]) as archive:
        assert {"agentic_hil/defmt.py", "agentic_hil/elfsymbols.py"} <= set(archive.namelist())
        for member in archive.infolist():
            target = _safe_member(wheel_root, member.filename)
            assert (member.external_attr >> 16) & 0o170000 != 0o120000
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    empty = tmp_path / "no-rust"
    empty.mkdir()
    cargo, rustup = tmp_path / "cargo", tmp_path / "rustup"
    cargo.mkdir()
    rustup.mkdir()
    common = {**os.environ, "PATH": str(empty), "PIP_NO_INDEX": "1", "CARGO_HOME": str(cargo), "RUSTUP_HOME": str(rustup), "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    common.pop("PYTHONPYCACHEPREFIX", None)
    fixtures = package / "tests/fixtures/defmt"
    packet = {"root": str(package), "fixtures": str(fixtures), "fixture_root": str(package), "import_path": "src", "mode": "pytest", "collect": True, "arguments": ["--collect-only", "-q"], "required_reads": [], "images": {name: str(path) for name, path in defmt_images.items()}}
    env = {**common, "PYTHONPATH": str(package / "src")}
    _run(["-c", PACKAGED_DRIVER], package, env=env, input=json.dumps(packet))
    packet.update(collect=False, arguments=["-q", "tests/test_defmt.py", "tests/test_defmt_regressions.py"])
    replay = _run(["-c", PACKAGED_DRIVER], package, env=env, input=json.dumps(packet))
    assert "PACKAGED_REPLAY=" in replay.stdout
    packet.update(root=str(wheel_root), import_path=".", mode="wheel", descriptions=descriptions)
    # Wheel code is imported separately; fixtures stay under the sdist root.
    _run(["-c", PACKAGED_DRIVER], wheel_root, env={**common, "PYTHONPATH": str(wheel_root)}, input=json.dumps(packet))
