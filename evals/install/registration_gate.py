"""Black-box registration gates for install.sh and the current source wheel.

The script target downloads the published package through the real installer;
the wheel target checks the current source offline. Neither receives credentials,
host homes, or hardware, and neither mocks an agent CLI or repairs a script's
registration before checking it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

try:
    import tomllib
except ImportError:  # Host-side report tests also run on Python 3.10.
    import tomli as tomllib

AGENTS = ("codex", "claude-code")
WHEEL_SCENARIOS = (
    "fresh-install",
    "merge-and-repeat",
    "setup",
    "project-refused",
    "invalid-config",
    "operator-conflict",
    "missing-registration-control",
    "broken-launcher-control",
)
SCRIPT_SCENARIOS = ("script-explicit-agent", "script-auto-detect")
SCENARIOS = WHEEL_SCENARIOS + SCRIPT_SCENARIOS
SCENARIOS_BY_MODE = {"wheel": WHEEL_SCENARIOS, "script": SCRIPT_SCENARIOS, "all": SCENARIOS}
REPORT_PREFIX = "AGENT_REGISTRATION_GATE="


def require(condition: object, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def validate_report(report: dict, *, mode: str = "all") -> None:
    """A green exit without every required case is a failure, including skips."""
    require(report.get("mode") == mode, f"expected {mode} registration evidence")
    expected = {(agent, scenario) for agent in AGENTS for scenario in SCENARIOS_BY_MODE[mode]}
    rows = report["cases"]
    actual = [(row["agent"], row["scenario"]) for row in rows]
    require(len(actual) == len(expected) and set(actual) == expected, "missing or duplicate registration cases")
    require(all(row.get("status") == "passed" for row in rows), "failed, skipped, or unfinished registration cases")
    require(report.get("ok") is True, "registration gate did not report success")
    require(set(report["versions"]) == set(AGENTS), "missing actual CLI versions")
    require(all(report["versions"].values()), "empty CLI version")


class Case:
    def __init__(self, agent: str, scenario: str):
        self.agent = agent
        self.home = Path("/home/eval/registration-cases") / agent / scenario
        self.home.mkdir(parents=True, mode=0o700)
        self.workspace = self.home / "firmware project"
        self.workspace.mkdir()
        self.other_workspace = self.home / "second project"
        self.other_workspace.mkdir()
        self.env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "USER": "eval",
            "PATH": f"{self.home}/.local/bin:/opt/agent-clis/node_modules/.bin:/usr/local/bin:/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "PIPX_HOME": str(self.home / ".local/share/pipx"),
            "PIPX_BIN_DIR": str(self.home / ".local/bin"),
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": "/opt/registration-wheels",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NO_COLOR": "1",
        }
        self.config = self.home / (".codex/config.toml" if agent == "codex" else ".claude.json")
        self.skill = self.home / (".codex" if agent == "codex" else ".claude") / "skills/agentic-hil/SKILL.md"
        self.launcher = self.home / ".local/bin/agentic-hil"
        self.cli = "codex" if agent == "codex" else "claude"
        self.expected_tools = Path(__file__).with_name("tools.list.expected").read_text().splitlines()

    def run(self, command: list[str], *, cwd: Path | None = None, input_text: str | None = None, timeout_s: int = 45) -> subprocess.CompletedProcess:
        result = subprocess.run(
            command, cwd=cwd or self.workspace, env=self.env,
            input=input_text, capture_output=True, text=True, timeout=timeout_s, check=False,
        )
        return result

    def checked(self, command: list[str], **kwargs) -> str:
        result = self.run(command, **kwargs)
        require(result.returncode == 0, f"{command}: exit={result.returncode}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def install_wheel(self) -> None:
        require(not self.config.exists() and not self.skill.exists(), "case did not start with a clean home")
        wheels = list(Path("/opt/registration-wheels").glob("agentic_hil-*.whl"))
        require(len(wheels) == 1, f"expected one wheel, got {wheels}")
        with zipfile.ZipFile(wheels[0]) as wheel:
            self.packaged_skill = wheel.read("agentic_hil/skills/agentic-hil/SKILL.md").decode("utf-8").replace("\r\n", "\n")
        self.checked(["pipx", "install", "--pip-args=--no-index --find-links=/opt/registration-wheels", str(wheels[0])])
        require(self.launcher.is_file() and self.launcher.resolve().is_relative_to(self.home), "launcher is not user-local")
        self.version = self.checked([str(self.launcher), "--version"]).strip()
        require(bool(self.version), "installed package reported no version")
        # pip installation alone does not register the integration.
        self.expect_missing()

    def expect_missing(self) -> None:
        result = self.run([self.cli, "mcp", "get", "agentic-hil"])
        require(result.returncode != 0, f"CLI sees a registration that should be absent: {result.stdout}")
        require("agentic-hil" in result.stdout + result.stderr, f"CLI failed for an unrelated reason: {result.stderr}")

    def integrate(self, command: str = "agent-install", *, success: bool = True) -> dict:
        result = self.run([str(self.launcher), command, "--agent", self.agent, "--json"])
        document = json.loads(result.stdout)
        require(document.get("ok") is success, f"unexpected {command} result: {document}")
        require(result.returncode == (0 if success else 1), f"exit code contradicts {command} result: {result}")
        return document

    def config_document(self) -> dict:
        text = self.config.read_text(encoding="utf-8")
        return tomllib.loads(text) if self.agent == "codex" else json.loads(text)

    def verify(self, *, provisioned: bool = False) -> None:
        require(self.skill.is_file(), f"missing skill: {self.skill}")
        skill_text = self.skill.read_text(encoding="utf-8")
        # Development packages can carry the last released skill version. The
        # installation must reproduce the wheel's actual skill, not invent a
        # version string from the running CLI or accept a stale installed copy.
        require("name: agentic-hil\n" in skill_text and skill_text == self.packaged_skill, "installed skill differs from the wheel")
        if self.agent == "codex":
            registration = (self.home / ".codex/AGENTS.md").read_text(encoding="utf-8")
            require(str(self.skill) in registration, "Codex AGENTS.md does not register the installed skill")
        key = "mcp_servers" if self.agent == "codex" else "mcpServers"
        entry = self.config_document()[key]["agentic-hil"]
        require(Path(entry["command"]).is_absolute(), "registration command is not absolute")
        require(Path(entry["command"]).resolve() == self.launcher.resolve(), "registration points at the wrong launcher")
        require(entry["args"] == ["mcp-stdio"], f"unexpected MCP arguments: {entry}")
        require(not set(entry) & {"cwd", "env", "url"}, f"registration is bound to one project or environment: {entry}")

        # A fresh CLI process in BOTH projects must discover the user entry.
        for workspace in (self.workspace, self.other_workspace):
            if self.agent == "codex":
                got = json.loads(self.checked(["codex", "mcp", "get", "agentic-hil", "--json"], cwd=workspace))
                listed = json.loads(self.checked(["codex", "mcp", "list", "--json"], cwd=workspace))
                require(got["name"] == "agentic-hil" and got["enabled"] is True, f"Codex registration disabled: {got}")
                require(got["transport"]["command"] == entry["command"], f"Codex loads a different command: {got}")
                require(got["transport"]["args"] == entry["args"], f"Codex loads different arguments: {got}")
                require(sum(item["name"] == "agentic-hil" and item["enabled"] is True for item in listed) == 1, f"Codex list: {listed}")
            else:
                got = self.checked(["claude", "mcp", "get", "agentic-hil"], cwd=workspace)
                require("Scope: User" in got and "Connected" in got, f"Claude has no connected user registration: {got}")
                require(f"Command: {entry['command']}" in got and "Args: mcp-stdio" in got, f"Claude loads another command: {got}")
                listed = self.checked(["claude", "mcp", "list"], cwd=workspace)
                require(any(line.startswith("agentic-hil:") and "Connected" in line for line in listed.splitlines()), f"Claude list: {listed}")
            self.probe([entry["command"], *entry["args"]], workspace, provisioned and workspace == self.workspace)
            require(not (workspace / ".mcp.json").exists(), "registration leaked into firmware project")
            require(not (workspace / ".agentic-hil/config.yaml").exists(), "authoritative config leaked into firmware project")

    def probe(self, command: list[str], cwd: Path, provisioned: bool) -> None:
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "registration-gate", "version": "1"},
            }},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "project_config_describe", "arguments": {}}},
        ]
        output = self.checked(command, cwd=cwd, input_text="".join(json.dumps(message) + "\n" for message in messages))
        responses = [json.loads(line) for line in output.splitlines()]
        require([item.get("id") for item in responses] == [1, 2, 3], f"incomplete MCP exchange: {output}")
        require(responses[0]["result"]["serverInfo"] == {"name": "agentic-hil", "version": self.version}, "wrong MCP server identity")
        tools = responses[1]["result"]["tools"]
        require(sorted(tool["name"] for tool in tools) == self.expected_tools, "MCP tools/list does not match the shipped contract")
        require(all(tool.get("annotations", {}).get("title") for tool in tools), "MCP tools lack annotations")
        result = responses[2]["result"]
        if provisioned:
            require(result["isError"] is False and result["structuredContent"]["ok"] is True, f"configured server failed: {result}")
        else:
            require(result["isError"] is True and result["structuredContent"]["error_type"] == "config_file_not_found", f"wrong unconfigured-server response: {result}")

    def seed(self, conflict: bool = False) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        name = "agentic-hil" if conflict else "operator-tool"
        if self.agent == "codex":
            text = f'# operator comment\nmodel = "operator-model"\n[mcp_servers.{name}]\ncommand = "/usr/bin/true"\nargs = []\nenabled = false\n'
        else:
            text = json.dumps({"theme": "dark", "mcpServers": {name: {"type": "stdio", "command": "/usr/bin/true", "args": []}}}) + "\n"
        self.config.write_text(text, encoding="utf-8")

    def exercise(self, scenario: str) -> None:
        require(scenario in WHEEL_SCENARIOS, f"not a wheel scenario: {scenario}")
        self.install_wheel()
        if scenario in {"invalid-config", "operator-conflict"}:
            if scenario == "invalid-config":
                self.config.parent.mkdir(parents=True, exist_ok=True)
                self.config.write_text("[broken", encoding="utf-8")
            else:
                self.seed(conflict=True)
            before = self.config.read_bytes()
            result = self.integrate(success=False)
            expected = "config_invalid" if scenario == "invalid-config" else "mcp_config_conflict"
            require(result["steps"]["mcp_config"]["error_type"] == expected, f"wrong refusal: {result}")
            require(result["rollback"]["attempted"] is True and result["rollback"]["ok"] is True, f"rollback failed: {result}")
            require(self.config.read_bytes() == before and not self.skill.exists(), "refusal modified operator config or left a partial skill")
            return
        if scenario == "merge-and-repeat":
            self.seed()
            before = self.config_document()
        if scenario == "project-refused":
            # A relative override is refused before discovery, without a device.
            self.env["AGENTIC_HIL_CONFIG"] = "relative-policy.yaml"
            result = self.integrate("setup", success=False)
            require(result["scopes"]["user"]["ok"] is True and result["scopes"]["project"]["ok"] is False, f"project failure removed user integration: {result}")
            del self.env["AGENTIC_HIL_CONFIG"]
        else:
            result = self.integrate("setup" if scenario == "setup" else "agent-install")
        if scenario == "merge-and-repeat":
            # Check before starting the client: Claude migrates preferences
            # (including theme) out of .claude.json during its own startup.
            after = self.config_document()
            key = "mcp_servers" if self.agent == "codex" else "mcpServers"
            del after[key]["agentic-hil"]
            require(after == before, "registration changed unrelated operator settings")
            if self.agent == "codex":
                require("# operator comment\n" in self.config.read_text(), "registration removed operator comment")
            snapshot = {path: path.read_bytes() for path in (self.config, self.skill)}
            self.integrate()
            require(all(path.read_bytes() == content for path, content in snapshot.items()), "repeat install changed registered files")
        self.verify(provisioned=scenario == "setup")
        if scenario.endswith("-control"):
            if scenario == "missing-registration-control":
                self.config.unlink()
                self.expect_missing()
            else:
                # Keep the registered path present and executable with exit 0.
                # Only the native health check/wire exchange detects this fault.
                self.launcher.resolve().write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            try:
                self.verify()
            except (AssertionError, KeyError, FileNotFoundError):
                return
            raise AssertionError(f"negative control went green: {scenario}")


class ScriptCase(Case):
    """The only installer here is the repository's unmodified install.sh."""

    def exercise(self, scenario: str) -> None:
        require(scenario in SCRIPT_SCENARIOS, f"not a script scenario: {scenario}")
        script = Path("/opt/registration/install.sh")
        # Start without a package, manager, wheelhouse or package cache. The
        # pinned uv bootstrap and PyPI download must happen inside install.sh.
        for key in ("PIPX_HOME", "PIPX_BIN_DIR", "PIP_NO_INDEX", "PIP_FIND_LINKS"):
            self.env.pop(key)
        for command in ("agentic-hil", "uv", "pipx"):
            require(shutil.which(command, path=self.env["PATH"]) is None, f"{command} was preinstalled")
        require(not Path("/opt/registration-wheels").exists(), "script test received prebuilt package wheels")
        require(not self.config.exists() and not self.skill.exists(), "script test did not start clean")
        self.expect_missing()
        command = ["sh", str(script)]
        if scenario == "script-explicit-agent":
            command.extend(["--agent", self.agent])
        result = self.run(command, timeout_s=300)
        print(f"install.sh ({self.agent}, {scenario}):\n{result.stdout}{result.stderr}", flush=True)
        require(result.returncode == 0, f"install.sh failed with exit {result.returncode}: {result.stdout}\n{result.stderr}")
        require(self.launcher.is_file() and self.launcher.resolve().is_relative_to(self.home), "install.sh did not install a user-local launcher")
        self.version = self.checked([str(self.launcher), "--version"]).strip()
        release = re.search(r'^RELEASE="(\d+)\.(\d+)\.(\d+)"$', script.read_text(), re.MULTILINE)
        installed = re.match(r"^(\d+)\.(\d+)\.(\d+)", self.version)
        require(release is not None and installed is not None, "script or installed package did not report a version")
        require(tuple(map(int, installed.groups())) >= tuple(map(int, release.groups())), "install.sh installed below its release floor")
        # Read the installed distribution's declaration through its interpreter.
        # The release downloaded by install.sh need not have the development
        # checkout's tool list or skill version. This does not register anything.
        interpreter = self.home / ".local/share/uv/tools/agentic-hil/bin/python"
        metadata = json.loads(self.checked([str(interpreter), "-c", (
            "import json; from importlib.metadata import distribution; "
            "from agentic_hil.contracts import MCP_TOOL_NAMES; "
            "d = distribution('agentic-hil'); "
            "s = d.locate_file('agentic_hil/skills/agentic-hil/SKILL.md'); "
            "print(json.dumps({'skill': s.read_text(), 'tools': sorted(MCP_TOOL_NAMES), 'version': d.version}))"
        )]))
        require(metadata["version"] == self.version, "script launcher and installed distribution disagree")
        self.packaged_skill = metadata["skill"]
        self.expected_tools = metadata["tools"]
        require({"project_config_create", "project_config_describe", "debugger_info", "flash_firmware"} <= set(self.expected_tools), "published package lacks core MCP tools")
        # No agent-install/setup call from the harness: the script must have
        # created the integration itself before either real CLI checks it.
        self.verify()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("wheel", "script"), default="wheel")
    mode = parser.parse_args(argv).mode
    require(sys.platform == "linux" and os.getuid() != 0, "registration gate requires its non-root Linux container")
    versions = {}
    pins = json.loads(Path("/opt/agent-clis/package.json").read_text())["dependencies"]
    for agent, package in (("codex", "@openai/codex"), ("claude-code", "@anthropic-ai/claude-code")):
        cli = "/opt/agent-clis/node_modules/.bin/" + ("codex" if agent == "codex" else "claude")
        result = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30, check=True)
        versions[agent] = result.stdout.strip()
        require(re.search(rf"(?<![\d.]){re.escape(pins[package])}(?![\d.])", result.stdout), f"CLI version differs from lock: {result.stdout}")
    rows = []
    for agent in AGENTS:
        for scenario in SCENARIOS_BY_MODE[mode]:
            started = time.monotonic()
            row = {"agent": agent, "scenario": scenario, "status": "failed"}
            try:
                case = (ScriptCase if mode == "script" else Case)(agent, scenario)
                case.exercise(scenario)
                row["package_version"] = case.version
                if mode == "script":
                    row["installer_sha256"] = hashlib.sha256(Path("/opt/registration/install.sh").read_bytes()).hexdigest()
                row["status"] = "passed"
            except Exception:
                row["detail"] = traceback.format_exc()
            row["seconds"] = round(time.monotonic() - started, 2)
            rows.append(row)
            print(json.dumps(row), flush=True)
    report = {"ok": all(row["status"] == "passed" for row in rows), "mode": mode, "versions": versions, "cases": rows}
    print(REPORT_PREFIX + json.dumps(report), flush=True)
    validate_report(report, mode=mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
