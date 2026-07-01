import path from "node:path";
import { ArtifactManager } from "./artifacts.js";
import { CanBusService } from "./can.js";
import { ComPortService } from "./comports.js";
import type { DebuggerBackend } from "./debugger.js";
import { createDebuggerBackend } from "./debugger.js";
import { readLastReport } from "./report.js";
import type { AIHILConfig, JsonObject } from "./types.js";

export class AIHILToolService {
  readonly backend: DebuggerBackend;
  readonly artifacts: ArtifactManager;
  readonly comPorts: ComPortService;
  readonly canBuses: CanBusService;

  constructor(
    private readonly config: AIHILConfig,
    backend?: DebuggerBackend,
    artifacts?: ArtifactManager,
    comPorts?: ComPortService,
    canBuses?: CanBusService,
  ) {
    this.backend = backend ?? createDebuggerBackend(config);
    this.artifacts = artifacts ?? new ArtifactManager(config);
    this.comPorts = comPorts ?? new ComPortService(config);
    this.canBuses = canBuses ?? new CanBusService(config);
  }

  debuggerInfo(): Promise<JsonObject> {
    return this.backend.info();
  }

  probeTarget(): Promise<JsonObject> {
    return this.backend.probeTarget();
  }

  async flashFirmware(payload: JsonObject | null = {}): Promise<JsonObject> {
    const imagePath = payload?.image_path;
    const artifactId = payload?.artifact_id;
    if (Boolean(imagePath) === Boolean(artifactId)) {
      return {
        ok: false,
        tool: "aihil_flash_firmware",
        error_type: "invalid_argument",
        summary: "Provide exactly one of image_path or artifact_id.",
      };
    }
    const validation = imagePath
      ? this.artifacts.validateLocalPath(String(imagePath))
      : this.artifacts.resolveArtifactId(String(artifactId));
    if (!validation.ok) {
      return validation;
    }
    return this.backend.flashFirmware(validation.artifact as JsonObject);
  }

  async artifactUpload(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.artifacts.upload(payload);
  }

  resetTarget(mode = "run"): Promise<JsonObject> {
    return this.backend.resetTarget(mode);
  }

  async debugStartSession(payload: JsonObject | null = {}): Promise<JsonObject> {
    const tool = "aihil_debug_start_session";
    const imagePath = payload?.image_path;
    const artifactId = payload?.artifact_id;
    if (Boolean(imagePath) === Boolean(artifactId)) {
      return toolError(tool, "invalid_argument", "Provide exactly one of image_path or artifact_id.");
    }
    const validation = imagePath
      ? this.artifacts.validateLocalPath(String(imagePath))
      : this.artifacts.resolveArtifactId(String(artifactId), tool);
    if (!validation.ok) {
      validation.tool = tool;
      return validation;
    }
    const artifact = validation.artifact as JsonObject;
    if (path.extname(String(artifact.resolved_path)).toLowerCase() !== ".elf") {
      return toolError(tool, "artifact_validation_failed", "Debug sessions require an ELF artifact with debug symbols.");
    }
    return this.backend.debugStartSession(artifact, String(payload?.mode ?? "attach"), numberArgument(payload?.timeout_s));
  }

  debugStopSession(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.backend.debugStopSession(numberArgument(payload?.timeout_s));
  }

  debugGetSessionStatus(): Promise<JsonObject> {
    return this.backend.debugGetSessionStatus();
  }

  debugSetBreakpoint(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.backend.debugSetBreakpoint({ location: payload?.location ?? payload ?? {} });
  }

  debugListBreakpoints(): Promise<JsonObject> {
    return this.backend.debugListBreakpoints();
  }

  debugClearBreakpoints(): Promise<JsonObject> {
    return this.backend.debugClearBreakpoints();
  }

  debugContinue(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.backend.debugContinue(numberArgument(payload?.timeout_s));
  }

  debugHalt(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.backend.debugHalt(numberArgument(payload?.timeout_s));
  }

  debugGetStopReason(): Promise<JsonObject> {
    return this.backend.debugGetStopReason();
  }

  debugSymbolInfo(payload: JsonObject | null = {}): Promise<JsonObject> {
    const symbol = payload?.symbol;
    if (typeof symbol !== "string" || symbol.trim() === "") {
      return Promise.resolve(toolError("aihil_debug_symbol_info", "invalid_argument", "symbol must be a non-empty string."));
    }
    return this.backend.debugSymbolInfo(symbol.trim());
  }

  async debugDumpSymbolIhex(payload: JsonObject | null = {}): Promise<JsonObject> {
    const tool = "aihil_debug_dump_symbol_ihex";
    const symbol = payload?.symbol;
    const outputPath = payload?.output_path;
    if (typeof symbol !== "string" || symbol.trim() === "") {
      return toolError(tool, "invalid_argument", "symbol must be a non-empty string.");
    }
    if (typeof outputPath !== "string" || outputPath.trim() === "") {
      return toolError(tool, "invalid_argument", "output_path must be a non-empty string.");
    }
    const output = this.artifacts.validateOutputPath(outputPath, tool);
    if (!output.ok) {
      return output;
    }
    return this.backend.debugDumpSymbolIhex(symbol.trim(), output.output as JsonObject);
  }

  async getLastReport(): Promise<JsonObject> {
    const report = readLastReport(this.config);
    if (!report.ok && ["report_not_found", "config_invalid"].includes(String(report.error_type))) {
      return report;
    }
    return {
      ok: true,
      tool: "aihil_get_last_report",
      report,
    };
  }

  classifyLastError(): Promise<JsonObject> {
    return this.backend.classifyLastError();
  }

  comPortsList(): Promise<JsonObject> {
    return this.comPorts.listPorts();
  }

  comSessionStart(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.comPorts.sessionStart(String(payload?.port_id ?? ""), Boolean(payload?.clear_buffer ?? true));
  }

  comSessionStop(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.comPorts.sessionStop(String(payload?.port_id ?? ""));
  }

  comWrite(payload: JsonObject | null = {}): Promise<JsonObject> {
    const portId = String(payload?.port_id ?? "");
    const writePayload = Object.fromEntries(Object.entries(payload ?? {}).filter(([key]) => ["text", "hex"].includes(key)));
    return this.comPorts.write(portId, writePayload);
  }

  comRead(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.comPorts.read(String(payload?.port_id ?? ""), payload?.max_bytes, payload?.wait_timeout_s ?? 0.0);
  }

  canBusesList(): Promise<JsonObject> {
    return this.canBuses.listBuses();
  }

  canSessionStart(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.canBuses.sessionStart(String(payload?.bus_id ?? ""), Boolean(payload?.clear_rx_queue ?? true));
  }

  canSessionStop(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.canBuses.sessionStop(String(payload?.bus_id ?? ""));
  }

  canSend(payload: JsonObject | null = {}): Promise<JsonObject> {
    const busId = String(payload?.bus_id ?? "");
    const sendPayload = Object.fromEntries(Object.entries(payload ?? {}).filter(([key]) => key !== "bus_id"));
    return this.canBuses.send(busId, sendPayload);
  }

  canRead(payload: JsonObject | null = {}): Promise<JsonObject> {
    return this.canBuses.read(String(payload?.bus_id ?? ""), payload?.max_frames, payload?.wait_timeout_s ?? 0.0);
  }

  async close(): Promise<void> {
    await this.backend.close();
    await this.comPorts.close();
    await this.canBuses.close();
  }

  async call(name: string, arguments_: JsonObject | null = {}): Promise<JsonObject> {
    const args = arguments_ ?? {};
    if (name === "aihil_debugger_info") {
      return this.debuggerInfo();
    }
    if (name === "aihil_probe_target") {
      return this.probeTarget();
    }
    if (name === "aihil_flash_firmware") {
      return this.flashFirmware(args);
    }
    if (name === "aihil_artifact_upload") {
      return this.artifactUpload(args);
    }
    if (name === "aihil_reset_target") {
      return this.resetTarget(String(args.mode ?? "run"));
    }
    if (name === "aihil_debug_start_session") {
      return this.debugStartSession(args);
    }
    if (name === "aihil_debug_stop_session") {
      return this.debugStopSession(args);
    }
    if (name === "aihil_debug_get_session_status") {
      return this.debugGetSessionStatus();
    }
    if (name === "aihil_debug_set_breakpoint") {
      return this.debugSetBreakpoint(args);
    }
    if (name === "aihil_debug_list_breakpoints") {
      return this.debugListBreakpoints();
    }
    if (name === "aihil_debug_clear_breakpoints") {
      return this.debugClearBreakpoints();
    }
    if (name === "aihil_debug_continue") {
      return this.debugContinue(args);
    }
    if (name === "aihil_debug_halt") {
      return this.debugHalt(args);
    }
    if (name === "aihil_debug_get_stop_reason") {
      return this.debugGetStopReason();
    }
    if (name === "aihil_debug_symbol_info") {
      return this.debugSymbolInfo(args);
    }
    if (name === "aihil_debug_dump_symbol_ihex") {
      return this.debugDumpSymbolIhex(args);
    }
    if (name === "aihil_get_last_report") {
      return this.getLastReport();
    }
    if (name === "aihil_classify_last_error") {
      return this.classifyLastError();
    }
    if (name === "aihil_com_ports_list") {
      return this.comPortsList();
    }
    if (name === "aihil_com_session_start") {
      return this.comSessionStart(args);
    }
    if (name === "aihil_com_session_stop") {
      return this.comSessionStop(args);
    }
    if (name === "aihil_com_write") {
      return this.comWrite(args);
    }
    if (name === "aihil_com_read") {
      return this.comRead(args);
    }
    if (name === "aihil_can_buses_list") {
      return this.canBusesList();
    }
    if (name === "aihil_can_session_start") {
      return this.canSessionStart(args);
    }
    if (name === "aihil_can_session_stop") {
      return this.canSessionStop(args);
    }
    if (name === "aihil_can_send") {
      return this.canSend(args);
    }
    if (name === "aihil_can_read") {
      return this.canRead(args);
    }
    return {
      ok: false,
      tool: name,
      error_type: "unknown_tool",
      summary: "Unknown AI-HIL tool.",
    };
  }
}

function numberArgument(value: unknown): number | undefined {
  if (value === undefined || value === null) {
    return undefined;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}

function toolError(tool: string, errorType: string, summary: string): JsonObject {
  return {
    ok: false,
    tool,
    error_type: errorType,
    summary,
  };
}
