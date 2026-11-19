"""The shared bench is documented in the same change as its listener (#891).

Each test reads the pages one point of the documentation lives in and says
what is missing. The CI example is held to the shape the issue asks for: it
builds, submits with `agentic-hil remote submit --wait --junit-xml` and
publishes the result, under the trust rule of the existing hardware job.

These pages are repository content a source distribution does not carry, so
MANIFEST.in excludes this module.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
# The addresses a page may show: loopback, the wildcard, the private ranges and the documentation ranges.
SHOWABLE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("127.0.0.0/8", "0.0.0.0/32", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
IPV4_LITERAL = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def read(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def words(text: str) -> str:
    """The text as one lower-case line, so a sentence wrapped anywhere still reads as itself."""
    return " ".join(text.split()).lower()


def missing_from(text: str, required: tuple[str, ...]) -> list[str]:
    flat = words(text)
    return [phrase for phrase in required if phrase.lower() not in flat]


def stray_addresses(text: str) -> list[str]:
    """Every IPv4 literal outside loopback, the wildcard, the private and the documentation ranges."""
    stray = []
    for literal in IPV4_LITERAL.findall(text):
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            continue
        if not any(address in network for network in SHOWABLE_NETWORKS):
            stray.append(literal)
    return stray


def section(text: str, heading: str) -> str:
    """The body under the first `## ` heading that contains `heading`, up to the next one."""
    for part in text.split("\n## ")[1:]:
        title, _, body = part.partition("\n")
        if heading.lower() in title.lower():
            return body
    return ""


def test_the_shared_bench_page_is_published_with_its_boundary() -> None:
    navigation = read("mkdocs.yml")
    if "shared-bench.md" not in navigation:
        raise AssertionError("docs/shared-bench.md is not in the mkdocs.yml navigation")
    page = read("docs/shared-bench.md")
    missing = missing_from(page, (
        # Setup and configuration, with the Host rule.
        "shared bench", "bench server", "agentic-hil serve", "server.listen", "allowed_networks", "hostnames", "127.0.0.1", "0.0.0.0",
        # Admin answers 127.0.0.1 alone, so a listener bound elsewhere cannot be reached for it.
        "drain", "recover", "loopback",
        # The queue, the submitter name and what a drain lasts.
        "queue", "anonymous", "restart", "--user", "login name", "self-declared", "everyone",
        # Submitting with curl and with the client.
        "curl", "agentic-hil remote submit", "--wait", "--junit-xml", "agentic_hil_bench_url", ".agentic-hil/remote/",
        # What this release protects and what it does not.
        "authenticat", "max_job_runtime_s", "origin", "firewall",
    ))
    assert missing == [], f"docs/shared-bench.md does not say: {missing}"
    assert re.search(r"unreachable|cannot be reached|cannot reach|not reachable", words(page)), "docs/shared-bench.md does not say where drain and recover cannot be reached"
    assert stray_addresses(page) == []


def test_readme_names_the_shared_bench() -> None:
    readme = read("README.md")
    if "shared bench" not in words(readme) or "docs/shared-bench.md" not in readme:
        raise AssertionError("README.md does not name the shared bench")
    assert missing_from(readme, ("curl", "mcp", "http")) == []


def test_security_design_states_the_shared_bench_boundary() -> None:
    design = read("docs/security-design.md")
    headings = [line for line in design.splitlines() if line.startswith("#")]
    if not any("shared bench" in line.lower() for line in headings):
        raise AssertionError("docs/security-design.md has no section on the shared bench")
    boundary = section(design, "shared bench")
    assert missing_from(boundary, ("authenticat", "127.0.0.1", "allowed_networks", "origin", "host", "max_job_runtime_s")) == []
    assert "creates no listener" not in words(design)
    assert stray_addresses(design) == []


def test_support_and_action_design_say_no_hosted_bench() -> None:
    support = read("docs/support.md")
    action = read("docs/github-action-design.md")
    if "no hosted bench" not in words(support) or "no hosted bench" not in words(action):
        raise AssertionError("docs/support.md and docs/github-action-design.md do not say no hosted bench")
    assert "no remote bench" not in words(support)
    assert "hosted or remote bench" not in words(action)
    assert missing_from(support, ("agentic-hil serve",)) == []
    assert missing_from(action, ("agentic-hil serve", "`local` is the only accepted value")) == []


def test_mcp_hosts_documents_the_bench_server_over_http() -> None:
    hosts = read("docs/mcp-hosts.md")
    entry = next((part for part in hosts.split("\n## ")[1:] if "HTTP" in part.partition("\n")[0]), None)
    if entry is None:
        raise AssertionError("docs/mcp-hosts.md has no HTTP entry for the bench server")
    title = entry.partition("\n")[0]
    assert not any(pinned in title for pinned in ("Tool Annotations", "Claude Code", "OpenCode", "OpenAI Codex")), title
    assert missing_from(entry, ("/mcp", "streamable http", "agentic-hil serve")) == []


def test_configuration_says_a_shared_bench_is_configured_on_its_own_machine() -> None:
    configuration = read("docs/configuration.md")
    flat = words(configuration)
    if "configured once" not in flat or "plugged into" not in flat:
        raise AssertionError("docs/configuration.md does not say a shared bench is configured once, on the machine the hardware is plugged into")
    assert missing_from(configuration, ("allowed_networks",)) == []
    assert "does not open a listener" not in flat


def test_can_service_design_says_the_broker_never_listens_on_the_network() -> None:
    if "never listens on the network" not in words(read("docs/can-service-design.md")):
        raise AssertionError("docs/can-service-design.md does not say the broker never listens on the network")


def test_agent_instructions_name_the_bench_server() -> None:
    texts = {name: words(read(name)) for name in ("CLAUDE.md", "AGENTS.md")}
    if not all("agentic-hil serve" in text and "bench server" in text for text in texts.values()):
        raise AssertionError("CLAUDE.md and AGENTS.md do not name the bench server")


def test_the_ci_example_submits_to_a_shared_bench_and_publishes_the_result() -> None:
    page = read("docs/ci-examples.md")
    if "examples/ci/shared-bench.yml" not in page:
        raise AssertionError("docs/ci-examples.md does not name examples/ci/shared-bench.yml")
    text = read("examples/ci/shared-bench.yml")
    workflow = yaml.safe_load(text)
    hardware = yaml.safe_load(read("examples/ci/github-actions.yml"))["jobs"]["hardware"]

    # PyYAML reads a bare `on` as the boolean true.
    triggers = workflow.get("on", workflow.get(True, {}))
    assert not {"pull_request_target", "workflow_run"} & set(triggers or {}), triggers
    (job,) = workflow["jobs"].values()
    runs_on = job["runs-on"]
    assert "self-hosted" in (runs_on if isinstance(runs_on, list) else [runs_on]), runs_on
    # The trust rule of the hardware job, unchanged: no fork, no pull_request_target, no workflow_run.
    assert job["if"] == hardware["if"]

    steps = job["steps"]
    commands = [str(step.get("run", "")) for step in steps]
    submit = next(index for index, command in enumerate(commands) if "agentic-hil remote submit" in command)
    assert "--wait" in commands[submit] and "--junit-xml" in commands[submit], commands[submit]
    assert any("build" in str(step.get("name", "")).lower() for step in steps[:submit]), "nothing is built before the plan is submitted"
    published = [step for step in steps[submit + 1:] if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert published and published[0].get("if") == "always()", published
    assert all(PINNED_ACTION.match(str(step["uses"])) for step in steps if "uses" in step), [step.get("uses") for step in steps]
    # The version comes from the repository's own variable, never a literal this project would have to keep in step.
    assert any("agentic-hil==${{ vars.AGENTIC_HIL_VERSION }}" in command for command in commands), commands
    assert re.search(r"agentic-hil==\d", text) is None
    assert "AGENTIC_HIL_BENCH_URL" in text
