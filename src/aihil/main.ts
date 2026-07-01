#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { listAvailableComPorts } from "./comports.js";
import { runComStdio } from "./comstdio.js";
import { ConfigError, configSchemaText, DEFAULT_CONFIG_PATH, displayPath, loadConfig } from "./config.js";
import { createDebuggerBackend } from "./debugger.js";
import { runStdioServer } from "./stdio.js";
import type { JsonObject } from "./types.js";
import { packageVersion } from "./version.js";

export const DEFAULT_CONFIG_TEMPLATE = `target:
  name: "example-target"
  controller: "unknown-controller"

debugger:
  type: "openocd"
  executable: null
  probe_id: null
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 60

artifacts:
  allowed_roots:
    - "build"
  upload_directory: ".aihil/artifacts"
  allowed_extensions:
    - ".elf"
    - ".hex"
    - ".bin"
  max_upload_size_mb: 64
  allow_upload: true

com_ports: {}

can_buses: {}

validation:
  require_existing_file: true
  require_allowed_root: true
  require_allowed_extension: true
  compute_sha256: true
  inspect_known_formats: true

permissions:
  allow_probe: true
  allow_flash: true
  allow_reset: true
  allow_com_read: true
  allow_com_write: true
  allow_can_read: true
  allow_can_write: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false

reports:
  directory: ".aihil/reports"

logs:
  directory: ".aihil/logs"
`;

interface ParsedCommand {
  command: string;
  config?: string | null;
  force?: boolean;
  output?: string | null;
  target?: string | null;
  agent?: string | null;
  port?: string;
  maxReadBytes?: number | null;
  readWaitTimeoutS?: number;
  eofIdleTimeoutS?: number;
}

interface SkillAgent {
  id: string;
  displayName: string;
  aliases: string[];
  defaultTargetPath: () => string;
  registration: "skills-directory" | "agents-md";
}

const SKILL_NAME = "aihil-config-setup";
const SKILL_FILE = "SKILL.md";
const AIHIL_REGISTRATION_START = "<!-- AI-HIL skill registration start -->";
const AIHIL_REGISTRATION_END = "<!-- AI-HIL skill registration end -->";

const SKILL_AGENTS: SkillAgent[] = [
  {
    id: "opencode",
    displayName: "opencode",
    aliases: ["opencode", "open-code"],
    defaultTargetPath: () => path.join(homedir(), ".config", "opencode", "skills", SKILL_NAME, SKILL_FILE),
    registration: "skills-directory",
  },
  {
    id: "claude-code",
    displayName: "Claude Code",
    aliases: ["claude-code", "claude", "claude_code"],
    defaultTargetPath: () => path.join(homedir(), ".claude", "skills", SKILL_NAME, SKILL_FILE),
    registration: "skills-directory",
  },
  {
    id: "codex",
    displayName: "Codex",
    aliases: ["codex", "codex-cli", "openai-codex"],
    defaultTargetPath: () => path.join(homedir(), ".codex", "skills", SKILL_NAME, SKILL_FILE),
    registration: "agents-md",
  },
];

export async function main(argv = process.argv.slice(2)): Promise<number> {
  if (argv.length === 0) {
    process.stderr.write(helpText());
    return 2;
  }
  if (isHelpCommand(argv[0])) {
    process.stdout.write(helpText());
    return 0;
  }
  if (isVersionCommand(argv[0])) {
    process.stdout.write(`${packageVersion()}\n`);
    return 0;
  }

  let args: ParsedCommand;
  try {
    args = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    return 2;
  }

  if (args.command === "init") {
    const result = await initConfig(args.config, Boolean(args.force));
    printJson(result);
    return result.ok ? 0 : 1;
  }
  if (args.command === "schema") {
    const result = schema(args.output, Boolean(args.force));
    if (args.output === undefined || args.output === null) {
      process.stdout.write(String(result.schema));
    } else {
      printJson(result);
    }
    return result.ok ? 0 : 1;
  }
  if (args.command === "skill-install") {
    const result = installSkill(args.agent, args.target, Boolean(args.force));
    printJson(result);
    return result.ok ? 0 : 1;
  }
  if (args.command === "doctor") {
    const result = await doctor(args.config);
    printJson(result);
    return result.ok ? 0 : 1;
  }
  if (args.command === "com-ports") {
    const result = await listAvailableComPorts();
    printJson(result);
    return result.ok ? 0 : 1;
  }
  if (args.command === "mcp-stdio") {
    return mcpStdio(args.config);
  }
  if (args.command === "com-stdio") {
    if (!args.port) {
      process.stderr.write("com-stdio requires --port\n");
      return 2;
    }
    return comStdio(args.config, args.port, args.maxReadBytes ?? null, args.readWaitTimeoutS ?? 0.05, args.eofIdleTimeoutS ?? 0.5);
  }

  process.stderr.write(`unknown command: ${args.command}\n`);
  return 2;
}

export async function initConfig(configPath?: string | null, force = false): Promise<JsonObject> {
  const targetPath = configPath ?? DEFAULT_CONFIG_PATH;
  if (existsSync(targetPath) && !force) {
    return {
      ok: false,
      error_type: "config_exists",
      summary: "AI-HIL configuration already exists. Use --force to overwrite it.",
      path: targetPath,
    };
  }
  mkdirSync(path.dirname(targetPath), { recursive: true });
  writeFileSync(targetPath, DEFAULT_CONFIG_TEMPLATE, "utf8");
  try {
    loadConfig(targetPath);
  } catch (error) {
    if (error instanceof ConfigError) {
      const result = error.toDict();
      result.summary = "AI-HIL starter configuration was written but failed validation.";
      result.path = targetPath;
      return result;
    }
    throw error;
  }
  const availableComPorts = await listAvailableComPorts();
  return {
    ok: true,
    summary: "AI-HIL starter configuration written.",
    path: targetPath,
    available_com_ports: availableComPorts,
    next_steps: initNextSteps(availableComPorts),
  };
}

function initNextSteps(availableComPorts: JsonObject): string[] {
  const nextSteps = [
    "Keep this .aihil/config.yaml with the firmware project; use npm exec or one user-local aihil command instead of reinstalling per project.",
    "Edit target.name and target.controller for your board.",
    "Set debugger.interface_cfg and debugger.target_cfg for your OpenOCD setup.",
    "If multiple debug probes are connected, set debugger.probe_id to the intended probe serial number.",
  ];
  if (availableComPorts.ok) {
    const ports = Array.isArray(availableComPorts.ports) ? availableComPorts.ports : [];
    if (ports.length > 0) {
      const devices = ports.slice(0, 5).map((port) => String((port as JsonObject).device ?? "")).join(", ");
      const suffix = ports.length <= 5 ? "" : `, and ${ports.length - 5} more`;
      nextSteps.push(`Detected COM ports: ${devices}${suffix}. Add the DUT UART under com_ports if serial feedback is needed.`);
    } else {
      nextSteps.push("No host COM ports detected. Connect USB serial hardware and run: aihil com-ports");
    }
  } else {
    nextSteps.push("COM port discovery failed. Run: aihil com-ports after checking the serialport installation.");
  }
  nextSteps.push("For CAN access, add a named bus under can_buses, for example adapter: socketcan, channel: can0, bitrate: 500000 on Linux.");
  nextSteps.push("Run: aihil doctor", "Create .mcp.json from the documented portable template if your MCP client needs project discovery.");
  return nextSteps;
}

export function schema(output?: string | null, force = false): JsonObject {
  const text = configSchemaText();
  if (output === undefined || output === null) {
    return {
      ok: true,
      schema: text,
    };
  }
  if (existsSync(output) && !force) {
    return {
      ok: false,
      error_type: "schema_exists",
      summary: "AI-HIL configuration schema already exists. Use --force to overwrite it.",
      path: output,
    };
  }
  mkdirSync(path.dirname(output), { recursive: true });
  writeFileSync(output, text, "utf8");
  return {
    ok: true,
    summary: "AI-HIL configuration schema written.",
    path: output,
  };
}

export async function doctor(configPath?: string | null): Promise<JsonObject> {
  let config;
  try {
    config = loadConfig(configPath);
  } catch (error) {
    if (error instanceof ConfigError) {
      const result = error.toDict();
      result.tool = "aihil_doctor";
      return result;
    }
    throw error;
  }
  const debuggerInfo = await createDebuggerBackend(config).info();
  const configDisplayPath = displayPath(config, config.configPath);
  return {
    ok: debuggerInfo.ok === true,
    tool: "aihil_doctor",
    summary: debuggerInfo.ok
      ? "AI-HIL configuration loaded and debugger checked."
      : "AI-HIL configuration loaded, but debugger check failed.",
    config_path: config.configPath,
    mcp: {
      transport: "stdio",
      command: "npm",
      args: ["exec", "--yes", "--package", "aihil", "--", "aihil", "mcp-stdio", "--config", configDisplayPath],
    },
    target: {
      name: config.target.name,
      controller: config.target.controller,
    },
    com_ports: Object.fromEntries(
      Object.entries(config.com_ports).map(([portId, port]) => [
        portId,
        { device: port.device, baudrate: port.baudrate, encoding: port.encoding },
      ]),
    ),
    can_buses: Object.fromEntries(
      Object.entries(config.can_buses).map(([busId, bus]) => [
        busId,
        { adapter: bus.adapter, channel: bus.channel, bitrate: bus.bitrate, fd: bus.fd },
      ]),
    ),
    debugger: debuggerInfo,
  };
}

export function installSkill(agent?: string | null, target?: string | null, force = false): JsonObject {
  const requestedAgent = agent ?? "opencode";
  const resolvedAgent = resolveSkillAgent(requestedAgent);
  if (!resolvedAgent && !target) {
    return {
      ok: false,
      error_type: "unsupported_agent",
      summary: "AI-HIL does not know this agent's default skill directory. Provide --target to install anyway.",
      agent: normalizeAgent(requestedAgent),
      allowed_agents: supportedSkillAgents(),
    };
  }
  const agentId = resolvedAgent?.id ?? normalizeAgent(requestedAgent);
  const agentName = resolvedAgent?.displayName ?? agentId;

  const sourcePath = bundledSkillPath();
  const targetPath = target ?? resolvedAgent!.defaultTargetPath();
  const sourceText = readFileSync(sourcePath, "utf8");
  const sourceVersion = skillVersion(sourceText) ?? packageVersion();
  if (existsSync(targetPath)) {
    const existingText = readFileSync(targetPath, "utf8");
    if (existingText === sourceText) {
      const registration = registerSkill(resolvedAgent, targetPath, sourceVersion, requestedAgent);
      return {
        ok: true,
        summary: `AI-HIL ${agentName} skill is already installed.`,
        agent: agentId,
        requested_agent: requestedAgent,
        skill: SKILL_NAME,
        source_path: sourcePath,
        target_path: targetPath,
        version: sourceVersion,
        installed: false,
        updated: false,
        registered: registration?.ok === true,
        registration,
      };
    }
    const existingVersion = skillVersion(existingText);
    if (isAihilSetupSkill(existingText) && existingVersion !== sourceVersion) {
      mkdirSync(path.dirname(targetPath), { recursive: true });
      writeFileSync(targetPath, sourceText, "utf8");
      const registration = registerSkill(resolvedAgent, targetPath, sourceVersion, requestedAgent);
      return {
        ok: true,
        summary: `AI-HIL ${agentName} skill updated to match the current CLI package.`,
        agent: agentId,
        requested_agent: requestedAgent,
        skill: SKILL_NAME,
        source_path: sourcePath,
        target_path: targetPath,
        previous_version: existingVersion,
        version: sourceVersion,
        installed: false,
        updated: true,
        registered: registration?.ok === true,
        registration,
      };
    }
    if (!force) {
      return {
        ok: false,
        error_type: "skill_exists",
        summary: "Target skill file already exists with different content and no CLI-version drift. Use --force to overwrite it.",
        agent: agentId,
        requested_agent: requestedAgent,
        skill: SKILL_NAME,
        source_path: sourcePath,
        target_path: targetPath,
        existing_version: existingVersion,
        version: sourceVersion,
      };
    }
  }

  mkdirSync(path.dirname(targetPath), { recursive: true });
  writeFileSync(targetPath, sourceText, "utf8");
  const registration = registerSkill(resolvedAgent, targetPath, sourceVersion, requestedAgent);
  return {
    ok: true,
    summary: `AI-HIL ${agentName} skill installed.`,
    agent: agentId,
    requested_agent: requestedAgent,
    skill: SKILL_NAME,
    source_path: sourcePath,
    target_path: targetPath,
    version: sourceVersion,
    installed: true,
    updated: false,
    registered: registration?.ok === true,
    registration,
  };
}

function skillVersion(text: string): string | null {
  const match = /^  aihil_version: "([^"]+)"$/m.exec(text);
  return match?.[1] ?? null;
}

function isAihilSetupSkill(text: string): boolean {
  return new RegExp(`^name: ${SKILL_NAME}$`, "m").test(text) && /^  origin: AI-HIL$/m.test(text);
}

function normalizeAgent(agent: string): string {
  return agent.trim().toLowerCase().replace(/_/g, "-");
}

function resolveSkillAgent(agent: string): SkillAgent | null {
  const normalized = normalizeAgent(agent);
  return SKILL_AGENTS.find((candidate) => candidate.aliases.map(normalizeAgent).includes(normalized)) ?? null;
}

function supportedSkillAgents(): string[] {
  return SKILL_AGENTS.map((agent) => agent.id);
}

function registerSkill(agent: SkillAgent | null, targetPath: string, version: string, requestedAgent: string): JsonObject | null {
  if (!agent) {
    return {
      ok: false,
      mode: "explicit-target",
      summary: "No automatic agent registration is known for this agent. The skill was written to the explicit target path.",
    };
  }
  if (agent.registration === "skills-directory") {
    return {
      ok: true,
      mode: "skills-directory",
      summary: `${agent.displayName} discovers installed skills from its skills directory.`,
      path: path.dirname(targetPath),
    };
  }

  const registrationPath = path.join(skillInstallRoot(targetPath), "AGENTS.md");
  const block = codexRegistrationBlock(targetPath, version, requestedAgent);
  const result = upsertMarkedBlock(registrationPath, block);
  return {
    ok: true,
    mode: "agents-md",
    summary: `${agent.displayName} registration written to AGENTS.md.`,
    path: registrationPath,
    updated: result.updated,
  };
}

function skillInstallRoot(targetPath: string): string {
  const skillDirectory = path.dirname(targetPath);
  const skillsDirectory = path.dirname(skillDirectory);
  if (path.basename(targetPath) === SKILL_FILE && path.basename(skillDirectory) === SKILL_NAME && path.basename(skillsDirectory) === "skills") {
    return path.dirname(skillsDirectory);
  }
  return path.dirname(targetPath);
}

function codexRegistrationBlock(targetPath: string, version: string, requestedAgent: string): string {
  return `${AIHIL_REGISTRATION_START}
## AI-HIL Skill

- Skill path: \`${targetPath}\`
- AI-HIL version: \`${version}\`
- AI-HIL is for embedded firmware development with local hardware-in-the-loop targets.
- For AI-HIL setup, configuration, MCP, or embedded hardware workflows, read and follow this skill before acting.
- If this version differs from \`aihil --version\` or the AI-HIL package version from npm exec, run \`aihil skill-install --agent ${requestedAgent}\` or \`npm exec --yes --package aihil -- aihil skill-install --agent ${requestedAgent}\` and use that CLI package as authoritative.
${AIHIL_REGISTRATION_END}`;
}

function upsertMarkedBlock(filePath: string, block: string): { updated: boolean } {
  mkdirSync(path.dirname(filePath), { recursive: true });
  const existing = existsSync(filePath) ? readFileSync(filePath, "utf8") : "";
  const blockPattern = new RegExp(`${escapeRegExp(AIHIL_REGISTRATION_START)}[\\s\\S]*?${escapeRegExp(AIHIL_REGISTRATION_END)}`);
  const next = blockPattern.test(existing)
    ? existing.replace(blockPattern, block)
    : `${existing.trimEnd()}${existing.trimEnd() ? "\n\n" : ""}${block}\n`;
  if (next !== existing) {
    writeFileSync(filePath, next, "utf8");
    return { updated: true };
  }
  return { updated: false };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export async function mcpStdio(configPath?: string | null): Promise<number> {
  try {
    const config = loadConfig(configPath);
    return runStdioServer(config);
  } catch (error) {
    if (error instanceof ConfigError) {
      process.stderr.write(`${JSON.stringify(error.toDict(), null, 2)}\n`);
      return 2;
    }
    throw error;
  }
}

export async function comStdio(
  configPath: string | null | undefined,
  portId: string,
  maxReadBytes: number | null,
  readWaitTimeoutS: number,
  eofIdleTimeoutS: number,
): Promise<number> {
  try {
    const config = loadConfig(configPath);
    return runComStdio(config, portId, process.stdin, process.stdout, process.stderr, {
      maxReadBytes,
      readWaitTimeoutS,
      eofIdleTimeoutS,
    });
  } catch (error) {
    if (error instanceof ConfigError) {
      process.stderr.write(`${JSON.stringify(error.toDict(), null, 2)}\n`);
      return 2;
    }
    throw error;
  }
}

function parseArgs(argv: string[]): ParsedCommand {
  const command = argv[0];
  if (!command) {
    throw new Error("aihil requires a subcommand");
  }
  const parsed: ParsedCommand = { command };
  for (let index = 1; index < argv.length; index += 1) {
    const current = argv[index];
    if (current === "--force") {
      parsed.force = true;
      continue;
    }
    if (current === "--config") {
      parsed.config = requireValue(argv, ++index, current);
      continue;
    }
    if (current === "--output") {
      parsed.output = requireValue(argv, ++index, current);
      continue;
    }
    if (current === "--target") {
      parsed.target = requireValue(argv, ++index, current);
      continue;
    }
    if (current === "--agent") {
      parsed.agent = requireValue(argv, ++index, current);
      continue;
    }
    if (current === "--port") {
      parsed.port = requireValue(argv, ++index, current);
      continue;
    }
    if (current === "--max-read-bytes") {
      parsed.maxReadBytes = Number.parseInt(requireValue(argv, ++index, current), 10);
      continue;
    }
    if (current === "--read-wait-timeout-s") {
      parsed.readWaitTimeoutS = Number(requireValue(argv, ++index, current));
      continue;
    }
    if (current === "--eof-idle-timeout-s") {
      parsed.eofIdleTimeoutS = Number(requireValue(argv, ++index, current));
      continue;
    }
    throw new Error(`unknown argument: ${current}`);
  }
  return parsed;
}

function requireValue(argv: string[], index: number, flag: string): string {
  const value = argv[index];
  if (value === undefined) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

function printJson(value: JsonObject): void {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

function isHelpCommand(command: string | undefined): boolean {
  return command === "--help" || command === "-h" || command === "help";
}

function isVersionCommand(command: string | undefined): boolean {
  return command === "--version" || command === "-v" || command === "version";
}

function helpText(): string {
  return `AI-HIL local MCP stdio server\n\nUsage:\n  aihil <command> [options]\n\nCommands:\n  init [--config <path>] [--force]\n  doctor [--config <path>]\n  com-ports\n  mcp-stdio --config <path>\n  com-stdio --config <path> --port <port_id>\n  schema [--output <path>] [--force]\n  skill-install [--agent <${supportedSkillAgents().join("|")}>] [--target <path>] [--force]\n\nOptions:\n  --help, -h       Show this help.\n  --version, -v    Show the installed version.\n`;
}

function bundledSkillPath(): string {
  return path.resolve(path.dirname(modulePath), "..", "skills", "aihil-config-setup", "SKILL.md");
}

const invokedPath = process.argv[1] ? realpathOrResolve(process.argv[1]) : null;
const modulePath = realpathOrResolve(fileURLToPath(import.meta.url));
if (invokedPath === modulePath) {
  main().then(
    (code) => {
      process.exitCode = code;
    },
    (error) => {
      process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
      process.exitCode = 1;
    },
  );
}

function realpathOrResolve(value: string): string {
  try {
    return realpathSync.native(value);
  } catch {
    return path.resolve(value);
  }
}
