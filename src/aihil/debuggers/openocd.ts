import { existsSync, statSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { spawnSync } from "node:child_process";
import type { DebuggerBackend } from "../debugger.js";
import type { AIHILConfig, JsonObject } from "../types.js";
import { displayPath, resolveWorkPath } from "../config.js";
import { logsDirectory, readLastReport, timestampForFilename, utcNowIso, writeReport } from "../report.js";

const OPENOCD_NOT_FOUND: JsonObject = {
  ok: false,
  backend: "openocd",
  error_type: "debugger_not_found",
  backend_error_type: "openocd_not_found",
  summary: "Debugger executable could not be found.",
  likely_causes: [
    "debugger.executable is not configured",
    "debugger executable is not installed",
    "debugger executable is not in PATH",
  ],
};

const BACKEND_ERROR_TO_PUBLIC_ERROR: Record<string, string> = {
  openocd_not_found: "debugger_not_found",
  interface_config_not_found: "debugger_config_not_found",
  target_config_not_found: "debugger_config_not_found",
  config_file_not_found: "debugger_config_not_found",
};

const OPENOCD_DISABLE_TCP_SERVER_COMMANDS = ["gdb_port disabled", "tcl_port disabled", "telnet_port disabled"];

const OPENOCD_SUCCESS_MARKERS: Record<string, string> = {
  aihil_probe_target: "AIHIL_RESULT:probe_target:ok",
  aihil_flash_firmware: "AIHIL_RESULT:flash_firmware:ok",
  aihil_reset_target: "AIHIL_RESULT:reset_target:ok",
};

export class OpenOCDBackend implements DebuggerBackend {
  private readonly backendName = "openocd";

  constructor(private readonly config: AIHILConfig) {}

  resolveExecutable(): JsonObject {
    return this.resolveExecutableInternal();
  }

  async info(): Promise<JsonObject> {
    const resolved = this.resolveExecutableInternal();
    if (!resolved.ok) {
      return { tool: "aihil_debugger_info", ...resolved };
    }
    const command = [...this.invocation(String(resolved.executable_path)), "--version"];
    const completed = spawnCommand(command, this.config.workDir, Math.min(this.config.debugger.timeout_s, 10));
    if (completed.notFound) {
      return { tool: "aihil_debugger_info", ...OPENOCD_NOT_FOUND };
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
        summary: this.summaryForError(errorType, backendErrorType),
      };
    }
    return {
      ok: true,
      tool: "aihil_debugger_info",
      backend: this.backendName,
      executable: resolved.executable,
      probe_id: this.config.debugger.probe_id,
      version: output.split(/\r?\n/)[0] || "OpenOCD version output was empty.",
      summary: "OpenOCD is available.",
    };
  }

  async probeTarget(): Promise<JsonObject> {
    if (!this.config.permissions.allow_probe) {
      return this.permissionDenied("aihil_probe_target", "Probing is disabled by .aihil/config.yaml.");
    }
    const marker = OPENOCD_SUCCESS_MARKERS.aihil_probe_target;
    const result = this.runOpenocd("aihil_probe_target", `init; targets; echo "${marker}"; shutdown`, marker);
    if (result.ok) {
      result.target_detected = true;
      result.summary = "Target detected through OpenOCD.";
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

    const commandPath = escapeTclDoubleQuotedWord(openocdPathForCommand(String(artifact.resolved_path)));
    const marker = OPENOCD_SUCCESS_MARKERS.aihil_flash_firmware;
    const result = this.runOpenocd(
      "aihil_flash_firmware",
      `program "${commandPath}" verify reset; echo "${marker}"; shutdown`,
      marker,
    );
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
    const marker = OPENOCD_SUCCESS_MARKERS.aihil_reset_target;
    const result = this.runOpenocd("aihil_reset_target", `init; reset ${mode}; echo "${marker}"; shutdown`, marker);
    result.mode = mode;
    if (result.ok) {
      result.summary = `Target reset with mode '${mode}'.`;
    }
    return this.writeActionReport(result);
  }

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
          return { ...OPENOCD_NOT_FOUND };
        }
        return { ok: true, executable: resolved, executable_path: resolved };
      }
      const found = which(configured);
      if (found === null) {
        return { ...OPENOCD_NOT_FOUND };
      }
      return { ok: true, executable: found, executable_path: found };
    }

    const found = which("openocd");
    if (found === null) {
      return { ...OPENOCD_NOT_FOUND };
    }
    return { ok: true, executable: found, executable_path: found };
  }

  private runOpenocd(tool: string, openocdCommand: string, successMarker?: string): JsonObject {
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

    const args = [
      ...this.invocation(String(resolved.executable_path)),
      "-f",
      this.config.debugger.interface_cfg,
      ...this.probeSelectionCommands(),
      "-f",
      this.config.debugger.target_cfg,
      ...OPENOCD_DISABLE_TCP_SERVER_COMMANDS.flatMap((command) => ["-c", command]),
      "-c",
      openocdCommand,
    ];
    const logPath = path.join(logsDirectory(this.config), `openocd-${timestampForFilename()}-${tool}.log`);
    const completed = spawnCommand(args, this.config.workDir, this.config.debugger.timeout_s);
    const finishedAt = utcNowIso();
    const elapsedMs = Math.trunc(performance.now() - start);

    if (completed.notFound) {
      return {
        tool,
        backend: this.backendName,
        started_at: startedAt,
        ...OPENOCD_NOT_FOUND,
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
        return this.openocdFailureResult(tool, startedAt, finishedAt, elapsedMs, backendErrorType, logPath);
      }
      if (successMarker !== undefined && !output.includes(successMarker)) {
        return this.openocdFailureResult(tool, startedAt, finishedAt, elapsedMs, this.unconfirmedBackendErrorType(tool), logPath);
      }
      const result: JsonObject = {
        ok: true,
        tool,
        backend: this.backendName,
        started_at: startedAt,
        finished_at: finishedAt,
        elapsed_ms: elapsedMs,
        summary: "OpenOCD command completed successfully.",
        log_path: displayPath(this.config, logPath),
      };
      if (successMarker !== undefined) {
        result.success_confirmed = true;
      }
      return result;
    }

    return this.openocdFailureResult(tool, startedAt, finishedAt, elapsedMs, this.classifyOutput(output, tool), logPath);
  }

  private openocdFailureResult(
    tool: string,
    startedAt: string,
    finishedAt: string,
    elapsedMs: number,
    backendErrorType: string,
    logPath: string,
  ): JsonObject {
    const errorType = this.publicErrorType(backendErrorType);
    return {
      ok: false,
      tool,
      backend: this.backendName,
      started_at: startedAt,
      finished_at: finishedAt,
      elapsed_ms: elapsedMs,
      error_type: errorType,
      backend_error_type: backendErrorType,
      summary: this.summaryForError(errorType, backendErrorType),
      likely_causes: this.likelyCauses(errorType),
      log_path: displayPath(this.config, logPath),
    };
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

  private unconfirmedBackendErrorType(tool: string): string {
    return (
      {
        aihil_probe_target: "target_not_detected",
        aihil_flash_firmware: "flash_failed",
        aihil_reset_target: "reset_failed",
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

  private invocation(executablePath: string): string[] {
    if (executablePath.endsWith(".js") || executablePath.endsWith(".mjs")) {
      return [process.execPath, executablePath];
    }
    return [executablePath];
  }

  private probeSelectionCommands(): string[] {
    const probeId = this.config.debugger.probe_id;
    if (probeId === null) {
      return [];
    }
    return ["-c", `adapter serial ${probeId}`];
  }

  private classifyOutput(output: string, tool?: string): string {
    const lower = output.toLowerCase();
    const interfaceConfig = this.config.debugger.interface_cfg.toLowerCase();
    const targetConfig = this.config.debugger.target_cfg.toLowerCase();
    if (lower.includes(interfaceConfig) && containsAny(lower, ["not found", "can't find", "couldn't find", "couldn't open"])) {
      return "interface_config_not_found";
    }
    if (lower.includes(targetConfig) && containsAny(lower, ["not found", "can't find", "couldn't find", "couldn't open"])) {
      return "target_config_not_found";
    }
    if (
      containsAny(lower, [
        "adapter not found",
        "no adapter",
        "no device found",
        "unable to open",
        "open failed",
        "libusb_open",
      ])
    ) {
      return "adapter_not_found";
    }
    if (containsAny(lower, ["target not examined", "target not detected", "unable to connect", "failed to read"])) {
      return "target_not_detected";
    }
    if (lower.includes("verify") && containsAny(lower, ["failed", "mismatch", "error"])) {
      return "verify_failed";
    }
    if (lower.includes("reset") && containsAny(lower, ["failed", "error"])) {
      return "reset_failed";
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

  private summaryForError(errorType: string, backendErrorType?: string): string {
    const summaries: Record<string, string> = {
      debugger_not_found: "Debugger executable could not be found.",
      debugger_config_not_found: "Debugger configuration file could not be found.",
      adapter_not_found: "Debugger adapter could not be found or opened.",
      target_not_detected: "Debugger could not detect the target.",
      flash_failed: "Debugger failed to flash the firmware.",
      verify_failed: "Debugger failed to verify the flashed firmware.",
      reset_failed: "Debugger failed to reset the target.",
      timeout: "Debugger command timed out.",
      unknown_debugger_error: "Debugger failed with an unknown error.",
    };
    const summary = summaries[errorType] ?? "Debugger failed with an unknown error.";
    if (backendErrorType === "interface_config_not_found" || backendErrorType === "target_config_not_found") {
      return `${summary}`;
    }
    return summary;
  }

  private likelyCauses(errorType: string): string[] {
    const causes: Record<string, string[]> = {
      target_not_detected: [
        "DUT is not powered",
        "wrong interface configuration",
        "SWD/JTAG wiring issue",
        "debug probe already in use",
      ],
      adapter_not_found: [
        "debug probe is not connected",
        "debug probe driver is missing",
        "debug probe is already in use",
        "Windows USB driver is not bound to the ST-Link adapter",
      ],
      verify_failed: [
        "flash write did not persist correctly",
        "wrong target configuration",
        "firmware image does not match target memory layout",
      ],
      flash_failed: ["target flash is locked", "wrong target configuration", "firmware image is invalid for this target"],
      reset_failed: ["reset line wiring issue", "target is not responding", "wrong reset configuration"],
      timeout: ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"],
      debugger_not_found: [
        "debugger.executable is not configured",
        "debugger executable is not installed",
        "debugger executable is not in PATH",
      ],
      debugger_config_not_found: [
        "debugger interface configuration is missing",
        "debugger target configuration is missing",
        "debugger search path is incomplete",
      ],
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

function containsAny(value: string, needles: string[]): boolean {
  return needles.some((needle) => value.includes(needle));
}

function containsFailureText(output: string): boolean {
  return containsAny(output.toLowerCase(), ["error:", "failed", "failure", "mismatch"]);
}

function openocdPathForCommand(value: string): string {
  return process.platform === "win32" ? value.replace(/\\/g, "/") : value;
}

function escapeTclDoubleQuotedWord(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\$/g, "\\$").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
}

function commandForLog(args: string[]): string {
  return args.map((arg) => (/[\s"\\]/u.test(arg) ? `"${escapeCommandLogArg(arg)}"` : arg)).join(" ");
}

function escapeCommandLogArg(arg: string): string {
  return arg.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
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
