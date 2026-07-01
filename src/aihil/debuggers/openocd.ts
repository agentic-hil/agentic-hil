import { existsSync, statSync, writeFileSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import type { DebuggerBackend } from "../debugger.js";
import {
  GdbMiClient,
  miField,
  miString,
  parseGdbInteger,
  writeIntelHexFile,
  type GdbMiCommandResult,
  type GdbMiStopResult,
} from "../gdbmi.js";
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

const GDB_NOT_FOUND: JsonObject = {
  ok: false,
  backend: "openocd",
  error_type: "debugger_not_found",
  backend_error_type: "gdb_not_found",
  summary: "GDB executable could not be found.",
  likely_causes: [
    "debug.gdb_executable is not configured",
    "arm-none-eabi-gdb, gdb-multiarch, or gdb is not installed",
    "GDB executable is not in PATH",
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

interface DebugBreakpoint {
  id: number;
  backend_id: string | null;
  location: JsonObject;
  gdb_location: string;
}

interface OpenOCDDebugSession {
  id: string;
  artifact: JsonObject;
  mode: string;
  status: "starting" | "running" | "halted" | "stopped" | "error";
  stopReason: JsonObject | null;
  breakpoints: DebugBreakpoint[];
  nextBreakpointId: number;
  gdbPort: number;
  gdb: GdbMiClient | null;
  openocd: ChildProcess;
  openocdArgs: string[];
  openocdStdout: string;
  openocdStderr: string;
  gdbStdout: string;
  gdbStderr: string;
  gdbCommands: Array<Record<string, unknown>>;
  logPath: string;
  startedAt: string;
}

export class OpenOCDBackend implements DebuggerBackend {
  private readonly backendName = "openocd";
  private debugSession: OpenOCDDebugSession | null = null;

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

  async debugStartSession(artifact: JsonObject, mode = "attach", timeoutS?: number): Promise<JsonObject> {
    const tool = "aihil_debug_start_session";
    const startedAt = utcNowIso();
    const start = performance.now();
    const timeout = operationTimeout(timeoutS, this.config.debugger.timeout_s);
    const allowedModes = ["attach", "reset_halt", "load"];
    if (!allowedModes.includes(mode)) {
      return {
        ok: false,
        tool,
        error_type: "invalid_argument",
        summary: "Invalid debug session mode.",
        allowed_values: allowedModes,
      };
    }
    if (this.debugSession !== null && this.debugSession.status !== "stopped") {
      return {
        ok: false,
        tool,
        error_type: "session_already_active",
        summary: "A debug session is already active. Stop it before starting a new one.",
        session: this.sessionStatus(this.debugSession),
      };
    }
    const permission = this.debugStartPermission(mode, tool);
    if (!permission.ok) {
      return permission;
    }

    const resolvedOpenOcd = this.resolveExecutableInternal();
    if (!resolvedOpenOcd.ok) {
      return this.writeActionReport({
        tool,
        backend: this.backendName,
        started_at: startedAt,
        ...resolvedOpenOcd,
        finished_at: utcNowIso(),
        elapsed_ms: Math.trunc(performance.now() - start),
      });
    }
    const resolvedGdb = this.resolveGdbExecutableInternal();
    if (!resolvedGdb.ok) {
      return this.writeActionReport({
        tool,
        backend: this.backendName,
        started_at: startedAt,
        ...resolvedGdb,
        finished_at: utcNowIso(),
        elapsed_ms: Math.trunc(performance.now() - start),
      });
    }

    const gdbPort = await reserveTcpPort();
    const logPath = path.join(logsDirectory(this.config), `openocd-${timestampForFilename()}-${tool}.log`);
    const openocdCommand = mode === "attach" ? "init; halt" : "init; reset halt";
    const args = [
      ...this.invocation(String(resolvedOpenOcd.executable_path)),
      "-f",
      this.config.debugger.interface_cfg,
      ...this.probeSelectionCommands(),
      "-f",
      this.config.debugger.target_cfg,
      "-c",
      `gdb_port ${gdbPort}`,
      ...OPENOCD_DISABLE_TCP_SERVER_COMMANDS.filter((command) => !command.startsWith("gdb_port")).flatMap((command) => ["-c", command]),
      "-c",
      openocdCommand,
    ];
    const child = spawn(args[0], args.slice(1), {
      cwd: this.config.workDir,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const session: OpenOCDDebugSession = {
      id: `openocd-${timestampForFilename()}`,
      artifact,
      mode,
      status: "starting",
      stopReason: null,
      breakpoints: [],
      nextBreakpointId: 1,
      gdbPort,
      gdb: null,
      openocd: child,
      openocdArgs: args,
      openocdStdout: "",
      openocdStderr: "",
      gdbStdout: "",
      gdbStderr: "",
      gdbCommands: [],
      logPath,
      startedAt,
    };
    child.stdout?.on("data", (chunk) => {
      session.openocdStdout += decodeOutput(chunk);
    });
    child.stderr?.on("data", (chunk) => {
      session.openocdStderr += decodeOutput(chunk);
    });

    const portState = await waitForTcpPort(gdbPort, timeout, () => child.exitCode !== null || child.killed);
    if (portState !== "ready") {
      session.status = "error";
      await this.cleanupDebugSession(session, 1);
      const output = `${session.openocdStdout}${session.openocdStderr}`;
      const backendErrorType = portState === "timeout" ? "timeout" : this.classifyOutput(output, tool);
      const errorType = backendErrorType === "timeout" ? "timeout" : this.publicErrorType(backendErrorType);
      const result = {
        ok: false,
        tool,
        backend: this.backendName,
        started_at: startedAt,
        finished_at: utcNowIso(),
        elapsed_ms: Math.trunc(performance.now() - start),
        error_type: errorType,
        backend_error_type: backendErrorType,
        summary: portState === "timeout" ? "Timed out waiting for OpenOCD GDB server." : this.summaryForError(errorType, backendErrorType),
        likely_causes: this.likelyCauses(errorType),
        log_path: displayPath(this.config, logPath),
      };
      return this.writeActionReport(result);
    }

    session.gdb = new GdbMiClient(String(resolvedGdb.executable_path), this.config.workDir);
    this.debugSession = session;
    const initResult = await this.initializeGdbSession(session, mode, timeout);
    if (!initResult.ok) {
      session.status = "error";
      await this.cleanupDebugSession(session, 1);
      this.debugSession = null;
      return this.writeActionReport({
        ...initResult,
        started_at: startedAt,
        finished_at: utcNowIso(),
        elapsed_ms: Math.trunc(performance.now() - start),
        log_path: displayPath(this.config, logPath),
      });
    }

    session.status = "halted";
    this.writeDebugSessionLog(session);
    return this.writeActionReport({
      ok: true,
      tool,
      backend: this.backendName,
      started_at: startedAt,
      finished_at: utcNowIso(),
      elapsed_ms: Math.trunc(performance.now() - start),
      session: this.sessionStatus(session),
      artifact: this.debugArtifact(artifact),
      mode,
      gdb_port: gdbPort,
      log_path: displayPath(this.config, logPath),
      summary: "Debug session started and target is halted.",
    });
  }

  async debugStopSession(timeoutS?: number): Promise<JsonObject> {
    const tool = "aihil_debug_stop_session";
    const session = this.debugSession;
    if (session === null) {
      return {
        ok: true,
        tool,
        backend: this.backendName,
        active: false,
        status: "stopped",
        summary: "No debug session is active.",
      };
    }
    const startedAt = utcNowIso();
    const start = performance.now();
    const timeout = operationTimeout(timeoutS, Math.min(this.config.debugger.timeout_s, 5));
    await this.cleanupDebugSession(session, timeout);
    session.status = "stopped";
    this.debugSession = null;
    this.writeDebugSessionLog(session);
    return this.writeActionReport({
      ok: true,
      tool,
      backend: this.backendName,
      started_at: startedAt,
      finished_at: utcNowIso(),
      elapsed_ms: Math.trunc(performance.now() - start),
      session: this.sessionStatus(session),
      log_path: displayPath(this.config, session.logPath),
      summary: "Debug session stopped.",
    });
  }

  async debugGetSessionStatus(): Promise<JsonObject> {
    const session = this.debugSession;
    return {
      ok: true,
      tool: "aihil_debug_get_session_status",
      backend: this.backendName,
      active: session !== null,
      status: session?.status ?? "stopped",
      session: session === null ? null : this.sessionStatus(session),
    };
  }

  async debugSetBreakpoint(location: JsonObject): Promise<JsonObject> {
    const tool = "aihil_debug_set_breakpoint";
    const session = this.requireDebugSession(tool);
    if (!session.ok) {
      return session;
    }
    const activeSession = session.session as OpenOCDDebugSession;
    const normalized = normalizeBreakpointLocation(location, tool);
    if (!normalized.ok) {
      return normalized;
    }
    const result = await this.gdbCommand(activeSession, `-break-insert ${miString(String(normalized.gdb_location))}`, tool);
    if (result.resultClass !== "done") {
      return this.writeActionReport(this.gdbFailure(tool, activeSession, result, "Debugger failed to set breakpoint."));
    }
    const breakpoint: DebugBreakpoint = {
      id: activeSession.nextBreakpointId++,
      backend_id: miField(result.line, "number"),
      location: normalized.location as JsonObject,
      gdb_location: String(normalized.gdb_location),
    };
    activeSession.breakpoints.push(breakpoint);
    this.writeDebugSessionLog(activeSession);
    return this.writeActionReport({
      ok: true,
      tool,
      backend: this.backendName,
      session: this.sessionStatus(activeSession),
      breakpoint,
      log_path: displayPath(this.config, activeSession.logPath),
      summary: "Breakpoint set.",
    });
  }

  async debugListBreakpoints(): Promise<JsonObject> {
    const session = this.debugSession;
    return {
      ok: true,
      tool: "aihil_debug_list_breakpoints",
      backend: this.backendName,
      active: session !== null,
      breakpoints: session?.breakpoints ?? [],
    };
  }

  async debugClearBreakpoints(): Promise<JsonObject> {
    const tool = "aihil_debug_clear_breakpoints";
    const session = this.requireDebugSession(tool);
    if (!session.ok) {
      return session;
    }
    const activeSession = session.session as OpenOCDDebugSession;
    for (const breakpoint of activeSession.breakpoints) {
      if (breakpoint.backend_id !== null) {
        const result = await this.gdbCommand(activeSession, `-break-delete ${breakpoint.backend_id}`, tool);
        if (result.resultClass !== "done") {
          return this.writeActionReport(this.gdbFailure(tool, activeSession, result, "Debugger failed to clear breakpoints."));
        }
      }
    }
    activeSession.breakpoints = [];
    this.writeDebugSessionLog(activeSession);
    return this.writeActionReport({
      ok: true,
      tool,
      backend: this.backendName,
      session: this.sessionStatus(activeSession),
      breakpoints: [],
      log_path: displayPath(this.config, activeSession.logPath),
      summary: "Breakpoints cleared.",
    });
  }

  async debugContinue(timeoutS?: number): Promise<JsonObject> {
    const tool = "aihil_debug_continue";
    const session = this.requireDebugSession(tool);
    if (!session.ok) {
      return session;
    }
    const activeSession = session.session as OpenOCDDebugSession;
    const timeout = operationTimeout(timeoutS, this.config.debugger.timeout_s);
    const startedAt = utcNowIso();
    const start = performance.now();
    activeSession.status = "running";
    activeSession.stopReason = null;
    const continueResult = await this.gdbCommand(activeSession, "-exec-continue", tool, Math.min(timeout, 5));
    if (!["running", "done"].includes(continueResult.resultClass)) {
      activeSession.status = "error";
      const failure = this.gdbFailure(tool, activeSession, continueResult, "Debugger failed to continue target execution.");
      activeSession.stopReason = { stop_reason: "debugger_error", backend_stop_reason: continueResult.resultClass };
      return this.writeActionReport({
        ...failure,
        started_at: startedAt,
        finished_at: utcNowIso(),
        elapsed_ms: Math.trunc(performance.now() - start),
      });
    }

    const stopped = await activeSession.gdb?.waitForStop(timeout);
    if (stopped === undefined || stopped.timedOut) {
      await activeSession.gdb?.command("-exec-interrupt --all", Math.min(5, this.config.debugger.timeout_s));
      await activeSession.gdb?.waitForStop(Math.min(5, this.config.debugger.timeout_s));
      activeSession.status = "halted";
      activeSession.stopReason = { stop_reason: "timeout", backend_stop_reason: "timeout" };
      this.writeDebugSessionLog(activeSession);
      return this.writeActionReport({
        ok: false,
        tool,
        backend: this.backendName,
        started_at: startedAt,
        finished_at: utcNowIso(),
        elapsed_ms: Math.trunc(performance.now() - start),
        error_type: "timeout",
        stop_reason: "timeout",
        session: this.sessionStatus(activeSession),
        log_path: displayPath(this.config, activeSession.logPath),
        summary: "Target execution did not stop before timeout.",
      });
    }

    const stopReason = this.stopReasonFromGdb(stopped, activeSession);
    activeSession.status = stopReason.stop_reason === "debugger_error" ? "error" : "halted";
    activeSession.stopReason = stopReason;
    this.writeDebugSessionLog(activeSession);
    const ok = !["fault", "debugger_error"].includes(String(stopReason.stop_reason));
    return this.writeActionReport({
      ok,
      tool,
      backend: this.backendName,
      started_at: startedAt,
      finished_at: utcNowIso(),
      elapsed_ms: Math.trunc(performance.now() - start),
      ...(ok ? {} : { error_type: String(stopReason.stop_reason) === "fault" ? "target_fault" : "debugger_error" }),
      stop_reason: stopReason.stop_reason,
      stop: stopReason,
      session: this.sessionStatus(activeSession),
      log_path: displayPath(this.config, activeSession.logPath),
      summary: ok ? `Target stopped: ${stopReason.stop_reason}.` : "Target execution stopped with an error.",
    });
  }

  async debugHalt(timeoutS?: number): Promise<JsonObject> {
    const tool = "aihil_debug_halt";
    const session = this.requireDebugSession(tool);
    if (!session.ok) {
      return session;
    }
    const activeSession = session.session as OpenOCDDebugSession;
    const timeout = operationTimeout(timeoutS, Math.min(this.config.debugger.timeout_s, 5));
    const result = await activeSession.gdb?.command("-exec-interrupt --all", timeout);
    const stopped = await activeSession.gdb?.waitForStop(timeout);
    if (result !== undefined && !["done", "running"].includes(result.resultClass)) {
      return this.writeActionReport(this.gdbFailure(tool, activeSession, result, "Debugger failed to halt target."));
    }
    activeSession.status = "halted";
    activeSession.stopReason = stopped === undefined ? { stop_reason: "unknown" } : this.stopReasonFromGdb(stopped, activeSession);
    this.writeDebugSessionLog(activeSession);
    return this.writeActionReport({
      ok: true,
      tool,
      backend: this.backendName,
      stop: activeSession.stopReason,
      session: this.sessionStatus(activeSession),
      log_path: displayPath(this.config, activeSession.logPath),
      summary: "Target halted.",
    });
  }

  async debugGetStopReason(): Promise<JsonObject> {
    const tool = "aihil_debug_get_stop_reason";
    const session = this.requireDebugSession(tool);
    if (!session.ok) {
      return session;
    }
    const activeSession = session.session as OpenOCDDebugSession;
    if (activeSession.stopReason === null) {
      return {
        ok: false,
        tool,
        backend: this.backendName,
        error_type: "stop_reason_not_available",
        summary: "No stop reason is available for the active debug session yet.",
        session: this.sessionStatus(activeSession),
      };
    }
    return {
      ok: true,
      tool,
      backend: this.backendName,
      stop_reason: activeSession.stopReason.stop_reason,
      stop: activeSession.stopReason,
      session: this.sessionStatus(activeSession),
    };
  }

  async debugSymbolInfo(symbol: string): Promise<JsonObject> {
    const tool = "aihil_debug_symbol_info";
    const session = this.requireDebugSession(tool);
    if (!session.ok) {
      return session;
    }
    const activeSession = session.session as OpenOCDDebugSession;
    const symbolResult = await this.resolveDebugSymbol(activeSession, symbol, tool);
    if (!symbolResult.ok) {
      return this.writeActionReport(symbolResult);
    }
    return this.writeActionReport({
      ok: true,
      tool,
      backend: this.backendName,
      symbol,
      address: symbolResult.address,
      size_bytes: symbolResult.size_bytes,
      session: this.sessionStatus(activeSession),
      log_path: displayPath(this.config, activeSession.logPath),
      summary: "Debug symbol resolved.",
    });
  }

  async debugDumpSymbolIhex(symbol: string, output: JsonObject): Promise<JsonObject> {
    const tool = "aihil_debug_dump_symbol_ihex";
    const session = this.requireDebugSession(tool);
    if (!session.ok) {
      return session;
    }
    const activeSession = session.session as OpenOCDDebugSession;
    const symbolResult = await this.resolveDebugSymbol(activeSession, symbol, tool);
    if (!symbolResult.ok) {
      return this.writeActionReport(symbolResult);
    }
    const sizeBytes = Number(symbolResult.size_bytes);
    if (sizeBytes > this.config.debug.max_dump_size_bytes) {
      return this.writeActionReport({
        ok: false,
        tool,
        backend: this.backendName,
        error_type: "permission_denied",
        summary: "Symbol dump exceeds debug.max_dump_size_bytes.",
        symbol,
        size_bytes: sizeBytes,
        max_dump_size_bytes: this.config.debug.max_dump_size_bytes,
      });
    }

    const data = await this.readMemoryBytes(activeSession, Number(symbolResult.address_value), sizeBytes, tool);
    if (!data.ok) {
      return this.writeActionReport(data);
    }
    writeIntelHexFile(String(output.resolved_path), Number(symbolResult.address_value), data.data as Buffer);
    this.writeDebugSessionLog(activeSession);
    return this.writeActionReport({
      ok: true,
      tool,
      backend: this.backendName,
      symbol,
      address: symbolResult.address,
      size_bytes: sizeBytes,
      output_path: output.path,
      output_format: "ihex",
      session: this.sessionStatus(activeSession),
      log_path: displayPath(this.config, activeSession.logPath),
      summary: "Symbol memory dumped as Intel HEX.",
    });
  }

  async close(): Promise<void> {
    if (this.debugSession !== null) {
      await this.cleanupDebugSession(this.debugSession, 1);
      this.debugSession = null;
    }
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

  private debugStartPermission(mode: string, tool: string): JsonObject {
    if (!this.config.permissions.allow_probe) {
      return this.permissionDenied(tool, "Debug sessions are disabled because probing is disabled by .aihil/config.yaml.");
    }
    if (this.config.permissions.allow_raw_debugger_commands) {
      return this.permissionDenied(tool, "Debug sessions are disabled while raw debugger commands are allowed.");
    }
    if (mode !== "attach" && !this.config.permissions.allow_reset) {
      return this.permissionDenied(tool, "Debug session reset is disabled by .aihil/config.yaml.");
    }
    if (mode === "load" && !this.config.permissions.allow_flash) {
      return this.permissionDenied(tool, "Debug session load is disabled because flashing is disabled by .aihil/config.yaml.");
    }
    if (mode === "load" && this.config.permissions.allow_mass_erase) {
      return this.permissionDenied(tool, "Debug session load is disabled while mass erase is allowed.");
    }
    return { ok: true };
  }

  private resolveGdbExecutableInternal(): JsonObject {
    const configured = this.config.debug.gdb_executable;
    if (configured !== null) {
      const hasPathSeparator = configured.includes("/") || configured.includes("\\");
      if (path.isAbsolute(configured) || hasPathSeparator) {
        const resolved = resolveWorkPath(this.config, configured);
        if (!existsSync(resolved) || !statSync(resolved).isFile()) {
          return { ...GDB_NOT_FOUND };
        }
        return { ok: true, executable: resolved, executable_path: resolved };
      }
      const found = which(configured);
      if (found === null) {
        return { ...GDB_NOT_FOUND };
      }
      return { ok: true, executable: found, executable_path: found };
    }

    for (const candidate of ["arm-none-eabi-gdb", "gdb-multiarch", "gdb"]) {
      const found = which(candidate);
      if (found !== null) {
        return { ok: true, executable: found, executable_path: found };
      }
    }
    return { ...GDB_NOT_FOUND };
  }

  private async initializeGdbSession(session: OpenOCDDebugSession, mode: string, timeout: number): Promise<JsonObject> {
    const tool = "aihil_debug_start_session";
    const commands = [
      "-gdb-set pagination off",
      "-gdb-set confirm off",
      `-file-exec-and-symbols ${miString(String(session.artifact.resolved_path))}`,
      `-target-select extended-remote localhost:${session.gdbPort}`,
    ];
    for (const command of commands) {
      const result = await this.gdbCommand(session, command, tool, timeout);
      if (result.resultClass !== "done" && result.resultClass !== "connected") {
        return this.gdbFailure(tool, session, result, "Debugger failed to initialize GDB session.");
      }
    }
    if (mode !== "attach") {
      const reset = await this.gdbCommand(session, `-interpreter-exec console ${miString("monitor reset halt")}`, tool, timeout);
      if (reset.resultClass !== "done") {
        return this.gdbFailure(tool, session, reset, "Debugger failed to reset and halt target.");
      }
    }
    if (mode === "load") {
      const load = await this.gdbCommand(session, "-target-download", tool, timeout);
      if (load.resultClass !== "done") {
        return this.gdbFailure(tool, session, load, "Debugger failed to load debug artifact.");
      }
      const reset = await this.gdbCommand(session, `-interpreter-exec console ${miString("monitor reset halt")}`, tool, timeout);
      if (reset.resultClass !== "done") {
        return this.gdbFailure(tool, session, reset, "Debugger failed to reset and halt target after load.");
      }
    }
    return { ok: true };
  }

  private async cleanupDebugSession(session: OpenOCDDebugSession, timeoutS: number): Promise<void> {
    if (session.gdb !== null) {
      await session.gdb.close(timeoutS);
      session.gdb = null;
    }
    if (session.openocd.exitCode === null && !session.openocd.killed) {
      session.openocd.kill();
      await waitForChildExit(session.openocd, Math.max(0, timeoutS) * 1000);
    }
    this.writeDebugSessionLog(session);
  }

  private requireDebugSession(tool: string): JsonObject {
    if (this.debugSession === null || this.debugSession.status === "stopped") {
      return {
        ok: false,
        tool,
        backend: this.backendName,
        error_type: "session_not_active",
        summary: "No debug session is active. Start one with aihil_debug_start_session first.",
      };
    }
    return { ok: true, session: this.debugSession };
  }

  private async gdbCommand(
    session: OpenOCDDebugSession,
    command: string,
    tool: string,
    timeoutS = Math.min(this.config.debugger.timeout_s, 10),
  ): Promise<GdbMiCommandResult> {
    if (session.gdb === null) {
      return {
        resultClass: "error",
        line: null,
        records: [],
        timedOut: false,
        errorMessage: "GDB session is not active.",
      };
    }
    const result = await session.gdb.command(command, timeoutS);
    this.writeDebugSessionLog(session);
    if (result.resultClass === "error" || result.timedOut) {
      session.stopReason = { stop_reason: "debugger_error", backend_stop_reason: result.resultClass, tool };
    }
    return result;
  }

  private gdbFailure(tool: string, session: OpenOCDDebugSession, result: GdbMiCommandResult, summary: string): JsonObject {
    const errorType = result.timedOut ? "timeout" : "debugger_error";
    return {
      ok: false,
      tool,
      backend: this.backendName,
      error_type: errorType,
      backend_error_type: result.timedOut ? "gdb_timeout" : "gdb_error",
      summary,
      backend_error: result.errorMessage ?? miField(result.line, "msg") ?? result.resultClass,
      session: this.sessionStatus(session),
      log_path: displayPath(this.config, session.logPath),
    };
  }

  private stopReasonFromGdb(stopped: GdbMiStopResult, session: OpenOCDDebugSession): JsonObject {
    if (stopped.timedOut) {
      return { stop_reason: "timeout", backend_stop_reason: "timeout" };
    }
    if (stopped.errorMessage !== undefined) {
      return { stop_reason: "debugger_error", backend_stop_reason: stopped.reason, backend_error: stopped.errorMessage };
    }
    const line = stopped.line ?? "";
    const lower = line.toLowerCase();
    const backendReason = stopped.reason;
    let stopReason = "unknown";
    if (backendReason === "breakpoint-hit") {
      stopReason = "breakpoint_hit";
    } else if (backendReason === "exited-normally" || backendReason === "exited") {
      stopReason = "target_exit";
    } else if (backendReason === "signal-received" || containsAny(lower, ["hardfault", "memmanage", "busfault", "usagefault"])) {
      stopReason = "fault";
    } else if (containsAny(lower, ["reset_handler", "reset"])) {
      stopReason = "reset";
    } else if (backendReason === "debugger-error") {
      stopReason = "debugger_error";
    }
    const backendBreakpointId = miField(line, "bkptno");
    const breakpoint = backendBreakpointId === null ? null : session.breakpoints.find((item) => item.backend_id === backendBreakpointId) ?? null;
    const result: JsonObject = {
      stop_reason: stopReason,
      backend_stop_reason: backendReason,
    };
    if (backendBreakpointId !== null) {
      result.backend_breakpoint_id = backendBreakpointId;
    }
    if (breakpoint !== null) {
      result.breakpoint_id = breakpoint.id;
      result.breakpoint = breakpoint;
    }
    const frame: JsonObject = {};
    const func = miField(line, "func");
    const addr = miField(line, "addr");
    const file = miField(line, "file");
    const lineNumber = miField(line, "line");
    if (func !== null) {
      frame.function = func;
    }
    if (addr !== null) {
      frame.address = addr;
    }
    if (file !== null) {
      frame.file = file;
    }
    if (lineNumber !== null) {
      frame.line = Number.parseInt(lineNumber, 10);
    }
    if (Object.keys(frame).length > 0) {
      result.frame = frame;
    }
    return result;
  }

  private async resolveDebugSymbol(session: OpenOCDDebugSession, symbol: string, tool: string): Promise<JsonObject> {
    const symbolValidation = validateDebugSymbol(symbol, tool, this.config.debug.allowed_symbols);
    if (!symbolValidation.ok) {
      return symbolValidation;
    }
    const addressResult = await this.gdbCommand(session, `-data-evaluate-expression ${miString(`(unsigned long)&${symbol}`)}`, tool);
    if (addressResult.resultClass !== "done") {
      return this.symbolFailure(tool, session, symbol, addressResult);
    }
    const sizeResult = await this.gdbCommand(session, `-data-evaluate-expression ${miString(`sizeof(${symbol})`)}`, tool);
    if (sizeResult.resultClass !== "done") {
      return this.symbolFailure(tool, session, symbol, sizeResult);
    }
    const addressText = miField(addressResult.line, "value");
    const sizeText = miField(sizeResult.line, "value");
    const address = addressText === null ? null : parseGdbInteger(addressText);
    const size = sizeText === null ? null : parseGdbInteger(sizeText);
    if (address === null || size === null || size <= 0) {
      return {
        ok: false,
        tool,
        backend: this.backendName,
        error_type: "symbol_resolution_failed",
        summary: "Debugger did not return a valid symbol address and size.",
        symbol,
        session: this.sessionStatus(session),
        log_path: displayPath(this.config, session.logPath),
      };
    }
    return {
      ok: true,
      symbol,
      address: `0x${address.toString(16)}`,
      address_value: address,
      size_bytes: size,
    };
  }

  private symbolFailure(tool: string, session: OpenOCDDebugSession, symbol: string, result: GdbMiCommandResult): JsonObject {
    const message = result.errorMessage ?? miField(result.line, "msg") ?? "GDB failed to resolve symbol.";
    const lower = message.toLowerCase();
    const errorType = lower.includes("no symbol") || lower.includes("not defined") ? "symbol_not_found" : lower.includes("ambiguous") ? "symbol_ambiguous" : "symbol_resolution_failed";
    return {
      ok: false,
      tool,
      backend: this.backendName,
      error_type: errorType,
      summary: errorType === "symbol_not_found" ? "Debug symbol could not be found." : "Debug symbol could not be resolved.",
      symbol,
      backend_error: message,
      session: this.sessionStatus(session),
      log_path: displayPath(this.config, session.logPath),
    };
  }

  private async readMemoryBytes(session: OpenOCDDebugSession, address: number, length: number, tool: string): Promise<JsonObject> {
    const chunks: Buffer[] = [];
    let offset = 0;
    while (offset < length) {
      const chunkLength = Math.min(1024, length - offset);
      const result = await this.gdbCommand(session, `-data-read-memory-bytes 0x${(address + offset).toString(16)} ${chunkLength}`, tool);
      if (result.resultClass !== "done") {
        return {
          ok: false,
          tool,
          backend: this.backendName,
          error_type: result.timedOut ? "timeout" : "memory_read_failed",
          backend_error_type: result.timedOut ? "gdb_timeout" : "gdb_error",
          summary: "Debugger failed to read target memory.",
          backend_error: result.errorMessage ?? miField(result.line, "msg") ?? result.resultClass,
          session: this.sessionStatus(session),
          log_path: displayPath(this.config, session.logPath),
        };
      }
      const contents = miField(result.line, "contents");
      if (contents === null || !/^(?:[0-9a-fA-F]{2})*$/.test(contents)) {
        return {
          ok: false,
          tool,
          backend: this.backendName,
          error_type: "memory_read_failed",
          summary: "Debugger returned invalid memory bytes.",
          session: this.sessionStatus(session),
          log_path: displayPath(this.config, session.logPath),
        };
      }
      const bytes = Buffer.from(contents, "hex");
      chunks.push(bytes);
      offset += bytes.length;
      if (bytes.length === 0) {
        break;
      }
    }
    const data = Buffer.concat(chunks);
    if (data.length !== length) {
      return {
        ok: false,
        tool,
        backend: this.backendName,
        error_type: "memory_read_failed",
        summary: "Debugger returned fewer memory bytes than requested.",
        requested_bytes: length,
        received_bytes: data.length,
        session: this.sessionStatus(session),
        log_path: displayPath(this.config, session.logPath),
      };
    }
    return { ok: true, data };
  }

  private sessionStatus(session: OpenOCDDebugSession): JsonObject {
    return {
      session_id: session.id,
      status: session.status,
      mode: session.mode,
      artifact: this.debugArtifact(session.artifact),
      breakpoints: session.breakpoints,
      stop_reason: session.stopReason,
      gdb_port: session.gdbPort,
    };
  }

  private debugArtifact(artifact: JsonObject): JsonObject {
    return {
      source: artifact.source ?? "path",
      path: artifact.path,
      sha256: artifact.sha256,
    };
  }

  private writeDebugSessionLog(session: OpenOCDDebugSession): void {
    if (session.gdb !== null) {
      session.gdbStdout = session.gdb.stdoutText;
      session.gdbStderr = session.gdb.stderrText;
      session.gdbCommands = session.gdb.commandHistory;
    }
    writeFileSync(
      session.logPath,
      `${JSON.stringify(
        {
          session_id: session.id,
          status: session.status,
          mode: session.mode,
          artifact: this.debugArtifact(session.artifact),
          openocd_command: commandForLog(session.openocdArgs),
          openocd_stdout: session.openocdStdout,
          openocd_stderr: session.openocdStderr,
          gdb_stdout: session.gdbStdout,
          gdb_stderr: session.gdbStderr,
          gdb_commands: session.gdbCommands,
          breakpoints: session.breakpoints,
          stop_reason: session.stopReason,
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
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

async function reserveTcpPort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address !== null ? address.port : null;
      server.close(() => {
        if (port === null) {
          reject(new Error("Could not reserve a local TCP port."));
        } else {
          resolve(port);
        }
      });
    });
  });
}

async function waitForTcpPort(port: number, timeoutSeconds: number, aborted: () => boolean): Promise<"ready" | "timeout" | "aborted"> {
  const deadline = Date.now() + Math.max(0, timeoutSeconds) * 1000;
  while (Date.now() <= deadline) {
    if (aborted()) {
      return "aborted";
    }
    if (await canConnectToTcpPort(port)) {
      return "ready";
    }
    await sleep(50);
  }
  return aborted() ? "aborted" : "timeout";
}

function canConnectToTcpPort(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    let settled = false;
    const finish = (result: boolean) => {
      if (settled) {
        return;
      }
      settled = true;
      socket.destroy();
      resolve(result);
    };
    socket.setTimeout(200);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

function waitForChildExit(child: ChildProcess, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    const timeout = setTimeout(resolve, timeoutMs);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function operationTimeout(value: number | undefined, defaultValue: number): number {
  const timeout = Number(value ?? defaultValue);
  return Number.isFinite(timeout) && timeout > 0 ? timeout : defaultValue;
}

function normalizeBreakpointLocation(raw: JsonObject, tool: string): JsonObject {
  const value = raw.location ?? raw;
  if (typeof value === "string") {
    if (!isValidDebugSymbol(value)) {
      return invalidArgument(tool, "Breakpoint symbol/function must be a safe C/C++ identifier.");
    }
    return { ok: true, gdb_location: value, location: { type: "symbol", symbol: value } };
  }
  if (!isRecord(value)) {
    return invalidArgument(tool, "Breakpoint location must be a symbol string or a location object.");
  }
  const symbol = value.symbol ?? value.function;
  if (symbol !== undefined && symbol !== null) {
    const symbolText = String(symbol);
    if (!isValidDebugSymbol(symbolText)) {
      return invalidArgument(tool, "Breakpoint symbol/function must be a safe C/C++ identifier.");
    }
    return { ok: true, gdb_location: symbolText, location: { type: "symbol", symbol: symbolText } };
  }
  if (value.file !== undefined && value.line !== undefined) {
    const file = String(value.file);
    const line = Number.parseInt(String(value.line), 10);
    if (!/^[A-Za-z0-9_./\\:-]+$/.test(file) || file.includes("..") || !Number.isInteger(line) || line <= 0) {
      return invalidArgument(tool, "Breakpoint file/line location is invalid.");
    }
    return { ok: true, gdb_location: `${file}:${line}`, location: { type: "file_line", file, line } };
  }
  return invalidArgument(tool, "Breakpoint location requires symbol/function or file and line.");
}

function validateDebugSymbol(symbol: string, tool: string, allowedSymbols: string[]): JsonObject {
  if (!isValidDebugSymbol(symbol)) {
    return invalidArgument(tool, "symbol must be a safe C/C++ identifier.");
  }
  if (allowedSymbols.length > 0 && !allowedSymbols.includes(symbol)) {
    return {
      ok: false,
      tool,
      error_type: "permission_denied",
      summary: "Symbol is not allowed by debug.allowed_symbols.",
      symbol,
    };
  }
  return { ok: true };
}

function isValidDebugSymbol(value: string): boolean {
  return /^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$/.test(value);
}

function invalidArgument(tool: string, summary: string): JsonObject {
  return {
    ok: false,
    tool,
    error_type: "invalid_argument",
    summary,
  };
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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
