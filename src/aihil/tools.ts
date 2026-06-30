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
