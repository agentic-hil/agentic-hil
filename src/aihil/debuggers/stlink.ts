import { existsSync, readdirSync, statSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { spawnSync } from "node:child_process";
import type { DebuggerBackend } from "../debugger.js";
import type { AIHILConfig, JsonObject } from "../types.js";
import { displayPath, resolveWorkPath } from "../config.js";
import { logsDirectory, readLastReport, timestampForFilename, utcNowIso, writeReport } from "../report.js";

const STLINK_NOT_FOUND: JsonObject = {
  ok: false,
  backend: "stlink",
  error_type: "debugger_not_found",
  backend_error_type: "stm32_programmer_cli_not_found",
  summary: "STM32CubeProgrammer CLI executable could not be found.",
  likely_causes: [
    "debugger.executable is not configured",
    "STM32CubeProgrammer is not installed",
    "STM32_Programmer_CLI executable is not in PATH",
  ],
};

const BACKEND_ERROR_TO_PUBLIC_ERROR: Record<string, string> = {
  stm32_programmer_cli_not_found: "debugger_not_found",
  probe_not_found: "adapter_not_found",
  probe_unconfirmed: "target_not_detected",
  flash_unconfirmed: "flash_failed",
  reset_unconfirmed: "reset_failed",
};

const STLINK_SUCCESS_CONFIRMATION: Record<string, string[]> = {
  aihil_probe_target: ["ST-LINK SN", "Device name"],
  aihil_flash_firmware: ["Download verified successfully"],
  aihil_reset_target: ["MCU Reset", "reset is performed"],
};

export class STLinkBackend implements DebuggerBackend {
  private readonly backendName = "stlink";

  constructor(private readonly config: AIHILConfig) {}

  async info(): Promise<JsonObject> {
    const resolved = this.resolveExecutableInternal();
    if (!resolved.ok) {
      return { tool: "aihil_debugger_info", ...resolved };
    }
    const command = [...this.invocation(String(resolved.executable_path)), "--version"];
    const completed = spawnCommand(command, this.config.workDir, Math.min(this.config.debugger.timeout_s, 10));
    if (completed.notFound) {
      return { tool: "aihil_debugger_info", ...STLINK_NOT_FOUND };
    }
    if (completed.timedOut) {
      return {
        ok: false,
        tool: "aihil_debugger_info",
        backend: this.backendName,
        executable: resolved.executable,
        error_type: "timeout",
        summary: "Debugger version check timed out.",
      };
    }

    const output = `${completed.stdout}${completed.stderr}`.trim();
    if (completed.returncode !== 0) {
      const backendErrorType = this.classifyOutput(output);
      const errorType = this.publicErrorType(backendErrorType);
      return {
        ok: false,
        tool: "aihil_debugger_info",
        backend: this.backendName,
        executable: resolved.executable,
        error_type: errorType,
        backend_error_type: backendErrorType,
        summary: this.summaryForError(errorType),
      };
    }
    return {
      ok: true,
      tool: "aihil_debugger_info",
      backend: this.backendName,
      executable: resolved.executable,
      probe_id: this.config.debugger.probe_id,
      interface: this.config.debugger.interface,
      version: versionLine(output),
      summary: "STM32CubeProgrammer CLI is available.",
    };
  }

  async probeTarget(): Promise<JsonObject> {
    if (!this.config.permissions.allow_probe) {
      return this.permissionDenied("aihil_probe_target", "Probing is disabled by .aihil/config.yaml.");
    }
    const result = this.runStlink("aihil_probe_target", this.connectionArgs());
    if (result.ok) {
      result.target_detected = true;
      result.summary = "Target detected through ST-Link.";
    }
    return this.writeActionReport(result);
  }

  async flashFirmware(artifact: JsonObject): Promise<JsonObject> {
    if (!this.config.permissions.allow_flash) {
      return this.permissionDenied("aihil_flash_firmware", "Flashing is disabled by .aihil/config.yaml.");
    }
    if (this.config.permissions.allow_raw_debugger_commands) {
      return this.permissionDenied(
        "aihil_flash_firmware",
        "Flashing is disabled while raw debugger commands are allowed.",
      );
    }
    if (this.config.permissions.allow_mass_erase) {
      return this.permissionDenied("aihil_flash_firmware", "Flashing is disabled while mass erase is allowed.");
    }

    const artifactPath = String(artifact.resolved_path);
    const writeArgs = ["-w", artifactPath];
    if (path.extname(artifactPath).toLowerCase() === ".bin") {
      if (this.config.debugger.flash_address === null) {
        return {
          ok: false,
          tool: "aihil_flash_firmware",
          backend: this.backendName,
          error_type: "invalid_argument",
          summary: "Flashing .bin artifacts with ST-Link requires debugger.flash_address.",
          artifact: {
            source: artifact.source ?? "path",
            path: artifact.path,
            sha256: artifact.sha256,
          },
        };
      }
      writeArgs.push(this.config.debugger.flash_address);
    }

    const result = this.runStlink("aihil_flash_firmware", [...this.connectionArgs(), ...writeArgs, "-v", "-rst"]);
    result.artifact = {
      source: artifact.source ?? "path",
      path: artifact.path,
      sha256: artifact.sha256,
    };
    result.verify = true;
    result.reset_after_flash = true;
    if (result.ok) {
      result.summary = "Firmware flashed, verified, and target reset.";
    }
    return this.writeActionReport(result);
  }

  async resetTarget(mode = "run"): Promise<JsonObject> {
    const allowedModes = ["run", "halt", "init"];
    if (!allowedModes.includes(mode)) {
      return {
        ok: false,
        tool: "aihil_reset_target",
        error_type: "invalid_argument",
        summary: "Invalid reset mode.",
        allowed_values: allowedModes,
      };
    }
    if (!this.config.permissions.allow_reset) {
      return this.permissionDenied("aihil_reset_target", "Reset is disabled by .aihil/config.yaml.");
    }
    const modeArgs: Record<string, string[]> = {
      run: ["-rst"],
      halt: ["-halt"],
      init: ["-halt"],
    };
    const result = this.runStlink("aihil_reset_target", [...this.connectionArgs(), ...modeArgs[mode]]);
    result.mode = mode;
    if (result.ok) {
      result.summary = `Target reset with mode '${mode}'.`;
    }
    return this.writeActionReport(result);
  }

  async debugStartSession(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_start_session");
  }

  async debugStopSession(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_stop_session");
  }

  async debugGetSessionStatus(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_get_session_status");
  }

  async debugSetBreakpoint(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_set_breakpoint");
  }

  async debugListBreakpoints(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_list_breakpoints");
  }

  async debugClearBreakpoints(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_clear_breakpoints");
  }

  async debugContinue(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_continue");
  }

  async debugHalt(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_halt");
  }

  async debugGetStopReason(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_get_stop_reason");
  }

  async debugSymbolInfo(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_symbol_info");
  }

  async debugDumpSymbolIhex(): Promise<JsonObject> {
    return this.unsupportedDebugTool("aihil_debug_dump_symbol_ihex");
  }

  async close(): Promise<void> {}

  async classifyLastError(): Promise<JsonObject> {
    const report = readLastReport(this.config);
    if (!report.ok && report.error_type === "report_not_found") {
      return {
        ok: false,
        tool: "aihil_classify_last_error",
        error_type: "report_not_found",
        summary: "No AI-HIL report has been written yet.",
      };
    }
    if (report.ok) {
      return {
        ok: true,
        tool: "aihil_classify_last_error",
        error_type: null,
        summary: "Last AI-HIL report did not contain an error.",
      };
    }
    const errorType = String(report.error_type ?? "unknown_debugger_error");
    const result: JsonObject = {
      ok: true,
      tool: "aihil_classify_last_error",
      error_type: errorType,
      summary: report.summary ?? "Last AI-HIL report contained an error.",
      likely_causes: report.likely_causes ?? this.likelyCauses(errorType),
      report_path: report.report_path,
      log_path: report.log_path,
    };
    if (report.backend_error_type !== undefined) {
      result.backend_error_type = report.backend_error_type;
    }
    return result;
  }

  private resolveExecutableInternal(): JsonObject {
    const configured = this.config.debugger.executable;
    if (configured) {
      const hasPathSeparator = configured.includes("/") || configured.includes("\\");
      if (path.isAbsolute(configured) || hasPathSeparator) {
        const resolved = resolveWorkPath(this.config, configured);
        if (!existsSync(resolved) || !statSync(resolved).isFile()) {
          return { ...STLINK_NOT_FOUND };
        }
        return { ok: true, executable: resolved, executable_path: resolved };
      }
      const found = which(configured);
      if (found === null) {
        return { ...STLINK_NOT_FOUND };
      }
      return { ok: true, executable: found, executable_path: found };
    }

    const found = findStm32ProgrammerCli();
    if (found === null) {
      return { ...STLINK_NOT_FOUND };
    }
    return { ok: true, executable: found, executable_path: found };
  }

  private runStlink(tool: string, actionArgs: string[]): JsonObject {
    const startedAt = utcNowIso();
    const start = performance.now();
    const resolved = this.resolveExecutableInternal();
    if (!resolved.ok) {
      return {
        tool,
        backend: this.backendName,
        started_at: startedAt,
        ...resolved,
        finished_at: utcNowIso(),
        elapsed_ms: Math.trunc(performance.now() - start),
      };
    }

    const args = [...this.invocation(String(resolved.executable_path)), "-q", ...actionArgs];
    const logPath = path.join(logsDirectory(this.config), `stlink-${timestampForFilename()}-${tool}.log`);
    const completed = spawnCommand(args, this.config.workDir, this.config.debugger.timeout_s);
    const finishedAt = utcNowIso();
    const elapsedMs = Math.trunc(performance.now() - start);

    if (completed.notFound) {
      return {
        tool,
        backend: this.backendName,
        started_at: startedAt,
        ...STLINK_NOT_FOUND,
        finished_at: finishedAt,
        elapsed_ms: elapsedMs,
      };
    }

    this.writeLog(logPath, args, completed.stdout, completed.stderr, completed.returncode, completed.timedOut);
    if (completed.timedOut) {
      return {
        ok: false,
        tool,
        backend: this.backendName,
        started_at: startedAt,
        finished_at: finishedAt,
        elapsed_ms: elapsedMs,
        error_type: "timeout",
        summary: "Debugger command timed out.",
        likely_causes: this.likelyCauses("timeout"),
        log_path: displayPath(this.config, logPath),
      };
    }

    const output = `${completed.stdout}${completed.stderr}`;
    if (completed.returncode === 0) {
      const backendErrorType = this.backendErrorFromOutput(output, tool);
      if (backendErrorType !== null) {
        return this.stlinkFailureResult(tool, startedAt, finishedAt, elapsedMs, backendErrorType, logPath);
      }
      const confirmation = this.confirmOperationSuccess(output, tool);
      if (!confirmation.confirmed) {
        return this.stlinkFailureResult(tool, startedAt, finishedAt, elapsedMs, this.unconfirmedBackendErrorType(tool), logPath, {
          confirmed: false,
          expected_success_text: confirmation.expected,
        });
      }
      return {
        ok: true,
        tool,
        backend: this.backendName,
        started_at: startedAt,
        finished_at: finishedAt,
        elapsed_ms: elapsedMs,
        success_confirmed: true,
        operation_result: {
          confirmed: true,
          matched_success_text: confirmation.matched,
        },
        summary: "STM32CubeProgrammer CLI command completed successfully.",
        log_path: displayPath(this.config, logPath),
      };
    }

    return this.stlinkFailureResult(tool, startedAt, finishedAt, elapsedMs, this.classifyOutput(output, tool), logPath);
  }

  private connectionArgs(): string[] {
    const args = ["-c", `port=${this.config.debugger.interface}`];
    if (this.config.debugger.probe_id !== null) {
      args.push(`sn=${this.config.debugger.probe_id}`);
    }
    return args;
  }

  private stlinkFailureResult(
    tool: string,
    startedAt: string,
    finishedAt: string,
    elapsedMs: number,
    backendErrorType: string,
    logPath: string,
    operationResult?: JsonObject,
  ): JsonObject {
    const errorType = this.publicErrorType(backendErrorType);
    const result: JsonObject = {
      ok: false,
      tool,
      backend: this.backendName,
      started_at: startedAt,
      finished_at: finishedAt,
      elapsed_ms: elapsedMs,
      error_type: errorType,
      backend_error_type: backendErrorType,
      summary: this.summaryForError(errorType),
      likely_causes: this.likelyCauses(errorType),
      log_path: displayPath(this.config, logPath),
    };
    if (operationResult !== undefined) {
      result.operation_result = operationResult;
    }
    return result;
  }

  private backendErrorFromOutput(output: string, tool: string): string | null {
    const backendErrorType = this.classifyOutput(output, tool);
    if (backendErrorType !== "unknown_debugger_error") {
      return backendErrorType;
    }
    if (containsFailureText(output)) {
      return backendErrorType;
    }
    return null;
  }

  private confirmOperationSuccess(output: string, tool: string): JsonObject {
    const expected = STLINK_SUCCESS_CONFIRMATION[tool] ?? [];
    if (expected.length === 0) {
      return { confirmed: true, matched: null, expected };
    }
    const lower = output.toLowerCase();
    const matched = expected.filter((marker) => lower.includes(marker.toLowerCase()));
    return {
      confirmed: matched.length === expected.length,
      matched,
      expected,
    };
  }

  private unconfirmedBackendErrorType(tool: string): string {
    return (
      {
        aihil_probe_target: "probe_unconfirmed",
        aihil_flash_firmware: "flash_unconfirmed",
        aihil_reset_target: "reset_unconfirmed",
      } as Record<string, string>
    )[tool] ?? "unknown_debugger_error";
  }

  private writeActionReport(result: JsonObject): JsonObject {
    return writeReport(this.config, result);
  }

  private writeLog(
    logPath: string,
    args: string[],
    stdout: string,
    stderr: string,
    returncode: number | null,
    timedOut: boolean,
  ): void {
    writeFileSync(
      logPath,
      `${JSON.stringify(
        {
          command: commandForLog(args),
          returncode,
          timed_out: timedOut,
          stdout,
          stderr,
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
  }

  private permissionDenied(tool: string, summary: string): JsonObject {
    return {
      ok: false,
      tool,
      error_type: "permission_denied",
      summary,
    };
  }

  private unsupportedDebugTool(tool: string): JsonObject {
    return {
      ok: false,
      tool,
      backend: this.backendName,
      error_type: "not_supported",
      summary: "Typed debug sessions require the OpenOCD backend in this AI-HIL version.",
    };
  }

  private invocation(executablePath: string): string[] {
    if (executablePath.endsWith(".js") || executablePath.endsWith(".mjs")) {
      return [process.execPath, executablePath];
    }
    return [executablePath];
  }

  private classifyOutput(output: string, tool?: string): string {
    const lower = output.toLowerCase();
    if (containsAny(lower, ["no st-link", "no stlink", "st-link not found", "stlink not found", "no debug probe"])) {
      return "probe_not_found";
    }
    if (containsAny(lower, ["no stm32 target found", "cannot connect to target", "can not connect to target", "failed to connect"])) {
      return "target_not_detected";
    }
    if (containsAny(lower, ["no device found", "device not found", "unable to connect"])) {
      return "target_not_detected";
    }
    if (lower.includes("verify") && containsAny(lower, ["failed", "mismatch", "error"])) {
      return "verify_failed";
    }
    if (lower.includes("reset") && containsAny(lower, ["failed", "error"])) {
      return "reset_failed";
    }
    if (tool === "aihil_flash_firmware" && containsAny(lower, ["download failed", "write failed", "failed to download"])) {
      return "flash_failed";
    }
    if (containsAny(lower, ["can't find", "couldn't find", "couldn't open", "not found"])) {
      return "config_file_not_found";
    }
    if (tool === "aihil_flash_firmware" && containsAny(lower, ["failed", "error"])) {
      return "flash_failed";
    }
    return "unknown_debugger_error";
  }

  private publicErrorType(backendErrorType: string): string {
    return BACKEND_ERROR_TO_PUBLIC_ERROR[backendErrorType] ?? backendErrorType;
  }

  private summaryForError(errorType: string): string {
    const summaries: Record<string, string> = {
      debugger_not_found: "Debugger executable could not be found.",
      adapter_not_found: "Debugger adapter could not be found or opened.",
      target_not_detected: "Debugger could not detect the target.",
      flash_failed: "Debugger failed to flash the firmware.",
      verify_failed: "Debugger failed to verify the flashed firmware.",
      reset_failed: "Debugger failed to reset the target.",
      timeout: "Debugger command timed out.",
      config_file_not_found: "Debugger input file could not be found.",
      unknown_debugger_error: "Debugger failed with an unknown error.",
    };
    return summaries[errorType] ?? "Debugger failed with an unknown error.";
  }

  private likelyCauses(errorType: string): string[] {
    const causes: Record<string, string[]> = {
      target_not_detected: [
        "DUT is not powered",
        "wrong SWD/JTAG interface selection",
        "SWD/JTAG wiring issue",
        "debug probe already in use",
      ],
      adapter_not_found: [
        "debug probe is not connected",
        "debugger.probe_id does not match a connected ST-Link serial number",
        "debug probe driver is missing",
        "debug probe is already in use",
      ],
      verify_failed: [
        "flash write did not persist correctly",
        "firmware image does not match target memory layout",
      ],
      flash_failed: ["target flash is locked", "firmware image is invalid for this target", "debugger.flash_address is wrong"],
      reset_failed: ["reset line wiring issue", "target is not responding"],
      timeout: ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"],
      debugger_not_found: [
        "debugger.executable is not configured",
        "STM32CubeProgrammer is not installed",
        "STM32_Programmer_CLI executable is not in PATH",
      ],
      config_file_not_found: ["firmware artifact path is missing", "STM32CubeProgrammer CLI path is incomplete"],
    };
    return causes[errorType] ?? ["inspect the debugger log for details"];
  }
}

interface CompletedCommand {
  stdout: string;
  stderr: string;
  returncode: number | null;
  timedOut: boolean;
  notFound: boolean;
}

function spawnCommand(command: string[], cwd: string, timeoutSeconds: number): CompletedCommand {
  const completed = spawnSync(command[0], command.slice(1), {
    cwd,
    encoding: "utf8",
    timeout: Math.max(0, timeoutSeconds) * 1000,
    windowsHide: true,
    maxBuffer: 10 * 1024 * 1024,
  });
  const errorCode = typeof completed.error === "object" && completed.error !== null ? (completed.error as NodeJS.ErrnoException).code : undefined;
  return {
    stdout: decodeOutput(completed.stdout),
    stderr: decodeOutput(completed.stderr),
    returncode: completed.status,
    timedOut: errorCode === "ETIMEDOUT",
    notFound: errorCode === "ENOENT",
  };
}

function decodeOutput(value: string | Buffer | null | undefined): string {
  if (value === null || value === undefined) {
    return "";
  }
  return Buffer.isBuffer(value) ? value.toString("utf8") : value;
}

function versionLine(output: string): string {
  const match = output.match(/STM32CubeProgrammer version:\s*(.+)/i);
  if (match !== null) {
    return `STM32CubeProgrammer ${match[1].trim()}`;
  }
  return output.split(/\r?\n/).find((line) => line.trim())?.trim() ?? "STM32CubeProgrammer version output was empty.";
}

function containsAny(value: string, needles: string[]): boolean {
  return needles.some((needle) => value.includes(needle));
}

function containsFailureText(output: string): boolean {
  return containsAny(output.toLowerCase(), ["error:", "failed", "failure", "mismatch"]);
}

function commandForLog(args: string[]): string {
  return args.map((arg) => (/[\s"\\]/u.test(arg) ? `"${escapeCommandLogArg(arg)}"` : arg)).join(" ");
}

function escapeCommandLogArg(arg: string): string {
  return arg.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function findStm32ProgrammerCli(): string | null {
  const fromPath = which("STM32_Programmer_CLI");
  if (fromPath !== null) {
    return fromPath;
  }
  const fromPathExe = which("STM32_Programmer_CLI.exe");
  if (fromPathExe !== null) {
    return fromPathExe;
  }
  for (const candidate of commonStm32ProgrammerPaths()) {
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return candidate;
    }
  }
  return null;
}

function commonStm32ProgrammerPaths(): string[] {
  const candidates: string[] = [];
  const programFiles = [process.env.ProgramFiles, process.env["ProgramFiles(x86)"]].filter((value): value is string => Boolean(value));
  for (const root of programFiles) {
    candidates.push(path.join(root, "STMicroelectronics", "STM32Cube", "STM32CubeProgrammer", "bin", "STM32_Programmer_CLI.exe"));
  }
  candidates.push(...cubeIdeBundledProgrammerPaths("C:/ST"));
  return candidates;
}

function cubeIdeBundledProgrammerPaths(root: string): string[] {
  const candidates: string[] = [];
  try {
    for (const cubeIdeDirectory of readdirSync(root)) {
      if (!cubeIdeDirectory.startsWith("STM32CubeIDE_")) {
        continue;
      }
      const pluginRoot = path.join(root, cubeIdeDirectory, "STM32CubeIDE", "plugins");
      if (!existsSync(pluginRoot)) {
        continue;
      }
      for (const pluginDirectory of readdirSync(pluginRoot)) {
        if (!pluginDirectory.startsWith("com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.win32_")) {
          continue;
        }
        candidates.push(path.join(pluginRoot, pluginDirectory, "tools", "bin", "STM32_Programmer_CLI.exe"));
      }
    }
  } catch {
    return candidates;
  }
  return candidates.sort().reverse();
}

function which(executable: string): string | null {
  const searchPath = process.env.PATH ?? "";
  const extensions = process.platform === "win32" ? (process.env.PATHEXT ?? ".EXE;.CMD;.BAT;.COM").split(";") : [""];
  for (const directory of searchPath.split(path.delimiter)) {
    if (!directory) {
      continue;
    }
    const candidates = process.platform === "win32" && path.extname(executable) ? [executable] : extensions.map((ext) => `${executable}${ext}`);
    for (const candidate of candidates) {
      const fullPath = path.join(directory, candidate);
      if (existsSync(fullPath) && statSync(fullPath).isFile()) {
        return fullPath;
      }
    }
  }
  if (os.platform() !== "win32" && existsSync(executable) && statSync(executable).isFile()) {
    return path.resolve(executable);
  }
  return null;
}
