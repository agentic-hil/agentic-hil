import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { appendFileSync, existsSync, statSync } from "node:fs";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { displayPath, resolveWorkPath } from "./config.js";
import { logsDirectory, timestampForFilename, utcNowIso, writeReport } from "./report.js";
import type { AIHILConfig, CanBusConfig, JsonObject } from "./types.js";

const SUPPORTED_CAN_ADAPTERS = ["peak", "process"];

interface CanFrame {
  id: number;
  extended: boolean;
  rtr: boolean;
  data: Buffer;
}

interface CanBusSession {
  busId: string;
  busConfig: CanBusConfig;
  adapterSession: CanAdapterSession;
  logPath: string;
  startedAt: string;
  active: boolean;
}

interface CanAdapter {
  readonly name: string;
  open(busId: string, busConfig: CanBusConfig, options: CanSessionOptions): Promise<JsonObject>;
}

interface CanSessionOptions {
  clearRxQueue: boolean;
}

interface CanAdapterSession {
  readonly adapterName: string;
  send(frame: CanFrame): Promise<JsonObject>;
  read(maxFrames: number, waitTimeoutS: number): Promise<JsonObject>;
  close(): Promise<void>;
  status(): JsonObject;
}

interface BridgeCommand {
  command: string[];
  env?: NodeJS.ProcessEnv;
}

type BridgeCommandFactory = (config: AIHILConfig, busId: string, busConfig: CanBusConfig) => JsonObject;

interface PendingRequest {
  resolve: (value: JsonObject) => void;
  timer: NodeJS.Timeout;
}

export class CanBusService {
  private readonly sessions = new Map<string, CanBusSession>();

  constructor(private readonly config: AIHILConfig) {}

  async listBuses(): Promise<JsonObject> {
    const buses: JsonObject = {};
    for (const [busId, busConfig] of Object.entries(this.config.can_buses)) {
      buses[busId] = this.busStatus(busConfig, this.sessions.get(busId) ?? null);
    }
    return {
      ok: true,
      tool: "aihil_can_buses_list",
      buses,
      supported_adapters: SUPPORTED_CAN_ADAPTERS,
      summary: `${Object.keys(buses).length} configured CAN bus(es).`,
    };
  }

  async sessionStart(busId: string, clearRxQueue = true): Promise<JsonObject> {
    const bus = this.configuredBus(busId, "aihil_can_session_start");
    if (!bus.ok) {
      return this.writeReport(bus);
    }
    if (!this.config.permissions.allow_can_read && !this.config.permissions.allow_can_write) {
      return this.writeReport(
        this.permissionDenied("aihil_can_session_start", "CAN reading and writing are disabled by .aihil/config.yaml.", busId),
      );
    }

    const existing = this.sessions.get(busId);
    if (existing && this.sessionIsActive(existing)) {
      return this.writeReport({
        ok: true,
        tool: "aihil_can_session_start",
        bus_id: busId,
        already_active: true,
        session: this.sessionStatus(existing),
        summary: "CAN bus session is already active.",
      });
    }
    if (existing) {
      this.sessions.delete(busId);
    }

    const busConfig = bus.bus_config as CanBusConfig;
    const adapter = createCanAdapter(this.config, busConfig);
    const opened = await adapter.open(busId, busConfig, { clearRxQueue });
    if (!opened.ok) {
      return this.writeReport(opened);
    }

    const adapterSession = opened.session as CanAdapterSession;
    if (clearRxQueue && this.config.permissions.allow_can_read) {
      await adapterSession.read(busConfig.max_buffer_frames, 0);
    }
    const logPath = path.join(logsDirectory(this.config), `can-${timestampForFilename()}-${safeFilename(busId)}.jsonl`);
    const session: CanBusSession = {
      busId,
      busConfig,
      adapterSession,
      logPath,
      startedAt: utcNowIso(),
      active: true,
    };
    this.sessions.set(busId, session);
    this.writeSessionLog(session, {
      event: "start",
      bus_id: busId,
      adapter: busConfig.adapter,
      channel: busConfig.channel,
      bitrate: busConfig.bitrate,
    });

    return this.writeReport({
      ok: true,
      tool: "aihil_can_session_start",
      bus_id: busId,
      already_active: false,
      adapter: adapter.name,
      adapter_result: publicBackendResult(opened),
      session: this.sessionStatus(session),
      summary: "CAN bus session started.",
    });
  }

  async sessionStop(busId: string): Promise<JsonObject> {
    const bus = this.configuredBus(busId, "aihil_can_session_stop");
    if (!bus.ok) {
      return this.writeReport(bus);
    }
    const session = this.sessions.get(busId) ?? null;
    this.sessions.delete(busId);
    if (session === null) {
      return this.writeReport({
        ok: true,
        tool: "aihil_can_session_stop",
        bus_id: busId,
        was_active: false,
        summary: "CAN bus session was not active.",
      });
    }
    await this.stopSession(session, "requested");
    return this.writeReport({
      ok: true,
      tool: "aihil_can_session_stop",
      bus_id: busId,
      was_active: true,
      session: this.sessionStatus(session),
      summary: "CAN bus session stopped.",
    });
  }

  async send(busId: string, payload: JsonObject): Promise<JsonObject> {
    const bus = this.configuredBus(busId, "aihil_can_send");
    if (!bus.ok) {
      return this.writeReport(bus);
    }
    if (!this.config.permissions.allow_can_write) {
      return this.writeReport(this.permissionDenied("aihil_can_send", "CAN writing is disabled by .aihil/config.yaml.", busId));
    }
    const sessionResult = this.activeSession(busId, "aihil_can_send");
    if (!sessionResult.ok) {
      return this.writeReport(sessionResult);
    }

    const session = sessionResult.session as CanBusSession;
    const parsed = this.payloadFrame(session.busConfig, payload);
    if (!parsed.ok) {
      parsed.bus_id = busId;
      return this.writeReport(parsed);
    }
    const frame = parsed.frame as CanFrame;
    const sent = await session.adapterSession.send(frame);
    if (!sent.ok) {
      const result = {
        tool: "aihil_can_send",
        bus_id: busId,
        adapter: session.adapterSession.adapterName,
        frame: frameResult(frame),
        log_path: displayPath(this.config, session.logPath),
        ...sent,
      };
      this.writeSessionLog(session, { event: "error", direction: "tx", ...result });
      return this.writeReport(result);
    }

    const result = {
      ok: true,
      tool: "aihil_can_send",
      bus_id: busId,
      adapter: session.adapterSession.adapterName,
      frame: frameResult(frame),
      adapter_result: publicBackendResult(sent),
      log_path: displayPath(this.config, session.logPath),
      summary: "CAN frame sent.",
    };
    this.writeSessionLog(session, { direction: "tx", ...result });
    return this.writeReport(result);
  }

  async read(busId: string, maxFrames?: unknown, waitTimeoutS: unknown = 0.0): Promise<JsonObject> {
    const bus = this.configuredBus(busId, "aihil_can_read");
    if (!bus.ok) {
      return this.writeReport(bus);
    }
    if (!this.config.permissions.allow_can_read) {
      return this.writeReport(this.permissionDenied("aihil_can_read", "CAN reading is disabled by .aihil/config.yaml.", busId));
    }
    const sessionResult = this.activeSession(busId, "aihil_can_read");
    if (!sessionResult.ok) {
      return this.writeReport(sessionResult);
    }
    const session = sessionResult.session as CanBusSession;

    const parsedMaxFrames = maxFrames === undefined || maxFrames === null ? session.busConfig.max_buffer_frames : Number.parseInt(String(maxFrames), 10);
    let parsedWaitTimeoutS = Number(waitTimeoutS);
    if (!Number.isFinite(parsedMaxFrames) || !Number.isFinite(parsedWaitTimeoutS)) {
      return this.writeReport({
        ok: false,
        tool: "aihil_can_read",
        bus_id: busId,
        error_type: "invalid_argument",
        summary: "max_frames must be an integer and wait_timeout_s must be a number.",
      });
    }
    if (parsedMaxFrames < 1 || parsedMaxFrames > session.busConfig.max_buffer_frames) {
      return this.writeReport({
        ok: false,
        tool: "aihil_can_read",
        bus_id: busId,
        error_type: "invalid_argument",
        summary: "max_frames must be between 1 and configured max_buffer_frames.",
        max_buffer_frames: session.busConfig.max_buffer_frames,
      });
    }
    parsedWaitTimeoutS = Math.max(0, Math.min(parsedWaitTimeoutS, 60));

    const read = await session.adapterSession.read(parsedMaxFrames, parsedWaitTimeoutS);
    if (!read.ok) {
      const result = {
        tool: "aihil_can_read",
        bus_id: busId,
        adapter: session.adapterSession.adapterName,
        log_path: displayPath(this.config, session.logPath),
        ...read,
      };
      this.writeSessionLog(session, { event: "error", direction: "rx", ...result });
      return this.writeReport(result);
    }

    const frames = normalizeReceivedFrames(read.frames ?? []);
    const result = {
      ok: true,
      tool: "aihil_can_read",
      bus_id: busId,
      adapter: session.adapterSession.adapterName,
      frames_read: frames.length,
      frames,
      adapter_result: publicBackendResult(read, ["frames"]),
      log_path: displayPath(this.config, session.logPath),
      summary: frames.length ? "CAN frame(s) read." : "No CAN frames were available.",
    };
    this.writeSessionLog(session, { direction: "rx", ...result });
    return this.writeReport(result);
  }

  async close(): Promise<void> {
    const sessions = Array.from(this.sessions.values());
    this.sessions.clear();
    await Promise.all(sessions.map((session) => this.stopSession(session, "shutdown")));
  }

  private configuredBus(busId: string, tool: string): JsonObject {
    if (!busId) {
      return {
        ok: false,
        tool,
        error_type: "invalid_argument",
        summary: "bus_id is required.",
      };
    }
    const busConfig = this.config.can_buses[busId];
    if (busConfig === undefined) {
      return {
        ok: false,
        tool,
        bus_id: busId,
        error_type: "can_bus_not_configured",
        summary: "CAN bus is not configured in .aihil/config.yaml.",
        configured_buses: Object.keys(this.config.can_buses).sort(),
      };
    }
    return { ok: true, bus_config: busConfig };
  }

  private activeSession(busId: string, tool: string): JsonObject {
    const bus = this.configuredBus(busId, tool);
    if (!bus.ok) {
      return bus;
    }
    const session = this.sessions.get(busId) ?? null;
    if (session === null || !this.sessionIsActive(session)) {
      return {
        ok: false,
        tool,
        bus_id: busId,
        error_type: "session_not_active",
        summary: "CAN bus session is not active. Start it with aihil_can_session_start first.",
      };
    }
    return { ok: true, session };
  }

  private busStatus(busConfig: CanBusConfig, session: CanBusSession | null): JsonObject {
    const result: JsonObject = {
      adapter: busConfig.adapter,
      channel: busConfig.channel,
      bitrate: busConfig.bitrate,
      fd: busConfig.fd,
      max_buffer_frames: busConfig.max_buffer_frames,
      max_frame_data_bytes: busConfig.max_frame_data_bytes,
      session_active: false,
    };
    if (session !== null) {
      Object.assign(result, this.sessionStatus(session));
    }
    return result;
  }

  private sessionStatus(session: CanBusSession): JsonObject {
    return {
      session_active: this.sessionIsActive(session),
      started_at: session.startedAt,
      adapter: session.adapterSession.adapterName,
      adapter_status: session.adapterSession.status(),
      log_path: displayPath(this.config, session.logPath),
    };
  }

  private sessionIsActive(session: CanBusSession): boolean {
    return session.active && session.adapterSession.status().active !== false;
  }

  private async stopSession(session: CanBusSession, reason: string): Promise<void> {
    session.active = false;
    try {
      await session.adapterSession.close();
    } catch {
      // Closing an already-closed CAN adapter session is harmless during shutdown.
    }
    this.writeSessionLog(session, { event: "stop", reason });
  }

  private payloadFrame(busConfig: CanBusConfig, payload: JsonObject): JsonObject {
    const rawId = payload.frame_id ?? payload.id;
    const parsedId = parseCanId(rawId);
    if (parsedId === null) {
      return {
        ok: false,
        tool: "aihil_can_send",
        error_type: "invalid_argument",
        summary: "frame_id must be an integer or hexadecimal string such as 0x123.",
      };
    }
    const extended = Boolean(payload.extended ?? false);
    const rtr = Boolean(payload.rtr ?? false);
    const maxId = extended ? 0x1fffffff : 0x7ff;
    if (parsedId < 0 || parsedId > maxId) {
      return {
        ok: false,
        tool: "aihil_can_send",
        error_type: "invalid_argument",
        summary: extended ? "Extended CAN frame_id must be between 0 and 0x1fffffff." : "Standard CAN frame_id must be between 0 and 0x7ff.",
      };
    }

    const dataHex = payload.data_hex ?? payload.hex ?? "";
    if (typeof dataHex !== "string") {
      return {
        ok: false,
        tool: "aihil_can_send",
        error_type: "invalid_argument",
        summary: "data_hex must be a string.",
      };
    }
    const data = parseHexBytes(dataHex);
    if (data === null) {
      return {
        ok: false,
        tool: "aihil_can_send",
        error_type: "invalid_argument",
        summary: "data_hex must contain valid hexadecimal bytes.",
      };
    }
    if (data.length > busConfig.max_frame_data_bytes) {
      return {
        ok: false,
        tool: "aihil_can_send",
        error_type: "invalid_argument",
        summary: "CAN frame data exceeds configured max_frame_data_bytes.",
        bytes_requested: data.length,
        max_frame_data_bytes: busConfig.max_frame_data_bytes,
      };
    }
    return {
      ok: true,
      frame: {
        id: parsedId,
        extended,
        rtr,
        data,
      } satisfies CanFrame,
    };
  }

  private writeSessionLog(session: CanBusSession, event: JsonObject): void {
    const entry = { ...event };
    entry.time ??= utcNowIso();
    appendFileSync(session.logPath, `${JSON.stringify(entry)}\n`, "utf8");
  }

  private writeReport(result: JsonObject): JsonObject {
    return writeReport(this.config, result);
  }

  private permissionDenied(tool: string, summary: string, busId?: string): JsonObject {
    const result: JsonObject = {
      ok: false,
      tool,
      error_type: "permission_denied",
      summary,
    };
    if (busId) {
      result.bus_id = busId;
    }
    return result;
  }
}

class ProcessCanAdapter implements CanAdapter {
  constructor(
    private readonly config: AIHILConfig,
    readonly name: string,
    private readonly commandFactory: BridgeCommandFactory,
  ) {}

  async open(busId: string, busConfig: CanBusConfig, options: CanSessionOptions): Promise<JsonObject> {
    const commandResult = this.commandFactory(this.config, busId, busConfig);
    if (!commandResult.ok) {
      return {
        tool: "aihil_can_session_start",
        bus_id: busId,
        adapter: this.name,
        ...commandResult,
      };
    }
    const bridgeCommand = commandResult.bridge_command as BridgeCommand;
    const startedAt = utcNowIso();
    const start = performance.now();
    const child = spawn(bridgeCommand.command[0], bridgeCommand.command.slice(1), {
      cwd: this.config.workDir,
      env: { ...process.env, ...(bridgeCommand.env ?? {}) },
      windowsHide: true,
    });
    const bridge = new JsonLineBridgeProcess(child, this.name);
    const opened = await bridge.request(
      "open",
      {
        channel: busConfig.channel,
        bitrate: busConfig.bitrate,
        fd: busConfig.fd,
        data_bitrate: busConfig.data_bitrate,
        receive_own_messages: busConfig.receive_own_messages,
        listen_only: busConfig.listen_only,
        clear_rx_queue: options.clearRxQueue,
        poll_interval_ms: busConfig.poll_interval_ms,
      },
      busConfig.timeout_s,
    );
    const elapsedMs = Math.trunc(performance.now() - start);
    if (!opened.ok) {
      bridge.stop();
      return {
        tool: "aihil_can_session_start",
        bus_id: busId,
        adapter: this.name,
        started_at: startedAt,
        finished_at: utcNowIso(),
        elapsed_ms: elapsedMs,
        command: commandForLog(bridgeCommand.command),
        ...opened,
      };
    }
    return {
      ok: true,
      tool: "aihil_can_session_start",
      bus_id: busId,
      adapter: this.name,
      started_at: startedAt,
      finished_at: utcNowIso(),
      elapsed_ms: elapsedMs,
      command: commandForLog(bridgeCommand.command),
      backend: opened.backend ?? this.name,
      session: new ProcessCanAdapterSession(this.name, busConfig, bridge),
      summary: "CAN adapter bridge opened.",
    };
  }
}

class ProcessCanAdapterSession implements CanAdapterSession {
  constructor(
    readonly adapterName: string,
    private readonly busConfig: CanBusConfig,
    private readonly bridge: JsonLineBridgeProcess,
  ) {}

  async send(frame: CanFrame): Promise<JsonObject> {
    return this.bridge.request(
      "send",
      { frame: bridgeFrame(frame) },
      this.busConfig.timeout_s,
    );
  }

  async read(maxFrames: number, waitTimeoutS: number): Promise<JsonObject> {
    return this.bridge.request(
      "read",
      { max_frames: maxFrames, wait_timeout_s: waitTimeoutS, poll_interval_ms: this.busConfig.poll_interval_ms },
      Math.max(this.busConfig.timeout_s, waitTimeoutS + 1),
    );
  }

  async close(): Promise<void> {
    try {
      await this.bridge.request("close", {}, Math.min(this.busConfig.timeout_s, 1));
    } finally {
      this.bridge.stop();
    }
  }

  status(): JsonObject {
    return {
      active: !this.bridge.closed,
      backend: this.adapterName,
    };
  }
}

class JsonLineBridgeProcess {
  private nextRequestId = 1;
  private readonly pending = new Map<number, PendingRequest>();
  private stdoutBuffer = "";
  private stderrText = "";
  private exited = false;

  constructor(
    private readonly child: ChildProcessWithoutNullStreams,
    private readonly backendName: string,
  ) {
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (data: string) => this.handleStdout(data));
    child.stderr.on("data", (data: string) => {
      this.stderrText += data;
    });
    child.on("error", (error) => this.closeWithError("can_adapter_process_start_failed", error.message));
    child.on("exit", (code, signal) => {
      this.exited = true;
      if (this.pending.size > 0) {
        this.resolveAll({
          ok: false,
          adapter: this.backendName,
          error_type: "can_adapter_process_closed",
          summary: "CAN adapter bridge process exited before replying.",
          exit_code: code,
          signal,
          stderr_tail: this.stderrTail(),
        });
      }
    });
  }

  get closed(): boolean {
    return this.exited || this.child.killed;
  }

  request(op: string, payload: JsonObject, timeoutSeconds: number): Promise<JsonObject> {
    if (this.closed) {
      return Promise.resolve({
        ok: false,
        adapter: this.backendName,
        error_type: "can_adapter_process_closed",
        summary: "CAN adapter bridge process is not active.",
        stderr_tail: this.stderrTail(),
      });
    }
    const requestId = this.nextRequestId++;
    const timeoutMs = Math.max(0.1, timeoutSeconds) * 1000;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        resolve({
          ok: false,
          adapter: this.backendName,
          error_type: "timeout",
          summary: "CAN adapter bridge request timed out.",
          operation: op,
          stderr_tail: this.stderrTail(),
        });
      }, timeoutMs);
      this.pending.set(requestId, { resolve, timer });
      const message = JSON.stringify({ request_id: requestId, op, ...payload });
      this.child.stdin.write(`${message}\n`, "utf8", (error) => {
        if (error) {
          this.resolvePending(requestId, {
            ok: false,
            adapter: this.backendName,
            error_type: "can_adapter_process_write_failed",
            summary: "Failed to write to CAN adapter bridge process.",
            backend_error: error.message,
            stderr_tail: this.stderrTail(),
          });
        }
      });
    });
  }

  stop(): void {
    if (!this.child.killed) {
      this.child.kill();
    }
  }

  private handleStdout(data: string): void {
    this.stdoutBuffer += data;
    while (true) {
      const newline = this.stdoutBuffer.indexOf("\n");
      if (newline < 0) {
        return;
      }
      const rawLine = this.stdoutBuffer.slice(0, newline).trim();
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (!rawLine) {
        continue;
      }
      let message: JsonObject;
      try {
        message = JSON.parse(rawLine) as JsonObject;
      } catch {
        this.stderrText += `\nnon-json stdout: ${rawLine}`;
        continue;
      }
      const replyId = Number.parseInt(String(message.reply_id ?? ""), 10);
      if (Number.isFinite(replyId)) {
        delete message.reply_id;
        this.resolvePending(replyId, message);
      }
    }
  }

  private resolvePending(requestId: number, result: JsonObject): void {
    const pending = this.pending.get(requestId);
    if (pending === undefined) {
      return;
    }
    clearTimeout(pending.timer);
    this.pending.delete(requestId);
    pending.resolve(result);
  }

  private resolveAll(result: JsonObject): void {
    for (const [requestId] of this.pending) {
      this.resolvePending(requestId, result);
    }
  }

  private closeWithError(errorType: string, backendError: string): void {
    this.exited = true;
    this.resolveAll({
      ok: false,
      adapter: this.backendName,
      error_type: errorType,
      summary: "CAN adapter bridge process failed.",
      backend_error: backendError,
      stderr_tail: this.stderrTail(),
    });
  }

  private stderrTail(): string | null {
    const text = this.stderrText.trim();
    if (!text) {
      return null;
    }
    return text.slice(-4000);
  }
}

function createCanAdapter(config: AIHILConfig, busConfig: CanBusConfig): CanAdapter {
  if (busConfig.adapter === "peak") {
    return new ProcessCanAdapter(config, "peak", peakBridgeCommand);
  }
  return new ProcessCanAdapter(config, "process", processBridgeCommand);
}

function processBridgeCommand(config: AIHILConfig, busId: string, busConfig: CanBusConfig): JsonObject {
  if (busConfig.executable === null) {
    return {
      ok: false,
      error_type: "config_invalid",
      summary: "CAN adapter process backend requires can_buses.<bus_id>.executable.",
      field: `can_buses.${busId}.executable`,
    };
  }
  const executable = resolveExecutable(config, busConfig.executable, "CAN adapter bridge executable could not be found.");
  if (!executable.ok) {
    return executable;
  }
  return {
    ok: true,
    bridge_command: {
      command: [...invocation(String(executable.executable_path)), ...busConfig.args],
    } satisfies BridgeCommand,
  };
}

function peakBridgeCommand(config: AIHILConfig, _busId: string, busConfig: CanBusConfig): JsonObject {
  let pcanBasicDirectory: string | null = null;
  if (busConfig.pcanbasic_dll !== null) {
    const dll = resolveConfiguredPath(config, busConfig.pcanbasic_dll);
    if (!existsSync(dll) || !statSync(dll).isFile()) {
      return {
        ok: false,
        error_type: "config_invalid",
        summary: "Configured PCANBasic.dll could not be found.",
        path: busConfig.pcanbasic_dll,
      };
    }
    pcanBasicDirectory = path.dirname(dll);
  }
  const script = peakPowerShellBridgeScript(pcanBasicDirectory);
  const executable = process.platform === "win32" ? "powershell.exe" : "pwsh";
  return {
    ok: true,
    bridge_command: {
      command: [
        executable,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        Buffer.from(script, "utf16le").toString("base64"),
      ],
    } satisfies BridgeCommand,
  };
}

function resolveExecutable(config: AIHILConfig, executable: string, notFoundSummary: string): JsonObject {
  const hasPathSeparator = executable.includes("/") || executable.includes("\\");
  if (path.isAbsolute(executable) || hasPathSeparator) {
    const resolved = resolveConfiguredPath(config, executable);
    if (!existsSync(resolved) || !statSync(resolved).isFile()) {
      return {
        ok: false,
        error_type: "can_adapter_backend_not_available",
        summary: notFoundSummary,
        executable,
      };
    }
    return { ok: true, executable: resolved, executable_path: resolved };
  }
  return { ok: true, executable, executable_path: executable };
}

function resolveConfiguredPath(config: AIHILConfig, configuredPath: string): string {
  return path.isAbsolute(configuredPath) ? configuredPath : resolveWorkPath(config, configuredPath);
}

function invocation(executablePath: string): string[] {
  if (executablePath.endsWith(".js") || executablePath.endsWith(".mjs")) {
    return [process.execPath, executablePath];
  }
  return [executablePath];
}

function bridgeFrame(frame: CanFrame): JsonObject {
  return {
    id: frame.id,
    id_hex: hexId(frame.id),
    extended: frame.extended,
    rtr: frame.rtr,
    data_hex: frame.data.toString("hex"),
    dlc: frame.data.length,
  };
}

function frameResult(frame: CanFrame): JsonObject {
  return {
    id: frame.id,
    id_hex: hexId(frame.id),
    extended: frame.extended,
    rtr: frame.rtr,
    dlc: frame.data.length,
    data: {
      hex: frame.data.toString("hex"),
      bytes: frame.data.length,
    },
  };
}

function normalizeReceivedFrames(value: unknown): JsonObject[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const frames: JsonObject[] = [];
  for (const item of value) {
    if (!isRecord(item)) {
      continue;
    }
    const id = parseCanId(item.frame_id ?? item.id);
    if (id === null) {
      continue;
    }
    const dataHex = String(item.data_hex ?? item.hex ?? item.data?.hex ?? "");
    const data = parseHexBytes(dataHex);
    if (data === null) {
      continue;
    }
    const frame = frameResult({
      id,
      extended: Boolean(item.extended ?? false),
      rtr: Boolean(item.rtr ?? false),
      data,
    });
    for (const fieldName of ["timestamp_us", "timestamp_ms", "flags"]) {
      if (item[fieldName] !== undefined) {
        frame[fieldName] = item[fieldName];
      }
    }
    frames.push(frame);
  }
  return frames;
}

function parseCanId(value: unknown): number | null {
  if (typeof value === "number" && Number.isInteger(value)) {
    return value;
  }
  if (typeof value !== "string") {
    return null;
  }
  const text = value.trim();
  if (/^0x[0-9a-fA-F]+$/.test(text)) {
    return Number.parseInt(text.slice(2), 16);
  }
  if (/^[0-9]+$/.test(text)) {
    return Number.parseInt(text, 10);
  }
  return null;
}

function parseHexBytes(value: string): Buffer | null {
  const cleaned = value.replace(/\s+/g, "");
  if (cleaned.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(cleaned)) {
    return null;
  }
  return Buffer.from(cleaned, "hex");
}

function publicBackendResult(result: JsonObject, omitFields: string[] = []): JsonObject {
  const omitted = new Set(["ok", "session", "reply_id", ...omitFields]);
  return Object.fromEntries(Object.entries(result).filter(([key]) => !omitted.has(key)));
}

function commandForLog(command: string[]): string {
  const sanitized = [...command];
  for (let index = 1; index < sanitized.length; index += 1) {
    if (["-EncodedCommand", "-Command"].includes(sanitized[index - 1])) {
      sanitized[index] = "<redacted>";
    }
  }
  return sanitized.map((part) => JSON.stringify(part)).join(" ");
}

function safeFilename(value: string): string {
  return value.replace(/[^A-Za-z0-9_.-]/g, "_") || "bus";
}

function hexId(id: number): string {
  return `0x${id.toString(16)}`;
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function psSingleQuoted(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

function peakPowerShellBridgeScript(pcanBasicDirectory: string | null): string {
  const pcanPathSetup = pcanBasicDirectory
    ? `$env:PATH = ${psSingleQuoted(pcanBasicDirectory)} + ';' + $env:PATH`
    : "";
  return `
$ErrorActionPreference = 'Stop'
${pcanPathSetup}

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class PcanBasicNative {
  public const uint PCAN_ERROR_OK = 0x00000;
  public const uint PCAN_ERROR_QRCVEMPTY = 0x00020;

  [StructLayout(LayoutKind.Sequential)]
  public struct TPCANMsg {
    public uint ID;
    public byte MSGTYPE;
    public byte LEN;
    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 8)]
    public byte[] DATA;
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct TPCANTimestamp {
    public uint millis;
    public ushort millis_overflow;
    public ushort micros;
  }

  [DllImport("PCANBasic.dll", EntryPoint = "CAN_Initialize")]
  public static extern uint CAN_Initialize(ushort Channel, ushort Btr0Btr1, byte HwType, uint IOPort, ushort Interrupt);

  [DllImport("PCANBasic.dll", EntryPoint = "CAN_Uninitialize")]
  public static extern uint CAN_Uninitialize(ushort Channel);

  [DllImport("PCANBasic.dll", EntryPoint = "CAN_Reset")]
  public static extern uint CAN_Reset(ushort Channel);

  [DllImport("PCANBasic.dll", EntryPoint = "CAN_Write")]
  public static extern uint CAN_Write(ushort Channel, ref TPCANMsg Message);

  [DllImport("PCANBasic.dll", EntryPoint = "CAN_Read")]
  public static extern uint CAN_Read(ushort Channel, out TPCANMsg Message, out TPCANTimestamp Timestamp);

  [DllImport("PCANBasic.dll", EntryPoint = "CAN_GetErrorText", CharSet = CharSet.Ansi)]
  public static extern uint CAN_GetErrorText(uint Error, ushort Language, StringBuilder Buffer);
}
"@

$script:Open = $false
$script:Channel = [uint16]0
$script:Shutdown = $false

function Write-Json($value) {
  [Console]::Out.WriteLine(($value | ConvertTo-Json -Compress -Depth 8))
  [Console]::Out.Flush()
}

function Reply($requestId, $value) {
  $value.reply_id = $requestId
  Write-Json $value
}

function Error-Text([uint32]$status) {
  try {
    $buffer = New-Object System.Text.StringBuilder 256
    [void][PcanBasicNative]::CAN_GetErrorText($status, 0, $buffer)
    $text = $buffer.ToString()
    if ($text) { return $text }
  } catch {}
  return ('PCANBasic status 0x{0:x}' -f $status)
}

function Pcan-Error($summary, [uint32]$status) {
  return @{
    ok = $false
    backend = 'peak'
    error_type = 'can_adapter_error'
    backend_error_type = ('pcan_error_0x{0:x}' -f $status)
    summary = $summary
    backend_error = (Error-Text $status)
  }
}

function Channel-Value($value) {
  $text = ([string]$value).Trim()
  if ($text -match '^0x[0-9A-Fa-f]+$') { return [uint16][Convert]::ToUInt16($text.Substring(2), 16) }
  if ($text -match '^[0-9]+$') { return [uint16][Convert]::ToUInt16($text, 10) }
  if ($text -match '^PCAN_USBBUS([0-9]+)$') {
    $index = [int]$matches[1]
    if ($index -ge 1 -and $index -le 16) { return [uint16](0x50 + $index) }
  }
  throw "Unsupported PEAK channel '$text'. Use PCAN_USBBUS1..PCAN_USBBUS16 or a numeric handle such as 0x51."
}

function Baudrate-Value([int]$bitrate) {
  switch ($bitrate) {
    1000000 { return [uint16]0x0014 }
    800000 { return [uint16]0x0016 }
    500000 { return [uint16]0x001c }
    250000 { return [uint16]0x011c }
    125000 { return [uint16]0x031c }
    100000 { return [uint16]0x432f }
    95000 { return [uint16]0xc34e }
    83000 { return [uint16]0x852b }
    50000 { return [uint16]0x472f }
    47000 { return [uint16]0x1414 }
    33000 { return [uint16]0x8b2f }
    20000 { return [uint16]0x532f }
    10000 { return [uint16]0x672f }
    5000 { return [uint16]0x7f7f }
    default { throw "Unsupported PEAK classic CAN bitrate '$bitrate'." }
  }
}

function Property-Value($object, [string]$name, $defaultValue) {
  if ($null -eq $object) { return $defaultValue }
  $property = $object.PSObject.Properties[$name]
  if ($null -eq $property -or $null -eq $property.Value) { return $defaultValue }
  return $property.Value
}

function Hex-Bytes($hex) {
  if ($null -eq $hex) { $hex = '' }
  $clean = ([string]$hex -replace '\\s+', '')
  if (($clean.Length % 2) -ne 0 -or $clean -notmatch '^[0-9A-Fa-f]*$') { throw 'data_hex must contain valid hexadecimal bytes.' }
  $bytes = New-Object byte[] ([int]($clean.Length / 2))
  for ($i = 0; $i -lt $bytes.Length; $i++) {
    $bytes[$i] = [Convert]::ToByte($clean.Substring($i * 2, 2), 16)
  }
  return $bytes
}

function Message-FromFrame($frame) {
  $data = Hex-Bytes $frame.data_hex
  if ($data.Length -gt 8) { throw 'PEAK PCANBasic classic CAN frames support at most 8 data bytes.' }
  $message = New-Object PcanBasicNative+TPCANMsg
  $message.ID = [uint32]$frame.id
  $message.MSGTYPE = [byte]0
  if ([bool](Property-Value $frame 'extended' $false)) { $message.MSGTYPE = [byte]($message.MSGTYPE -bor 0x02) }
  if ([bool](Property-Value $frame 'rtr' $false)) { $message.MSGTYPE = [byte]($message.MSGTYPE -bor 0x01) }
  $message.LEN = [byte]$data.Length
  $message.DATA = New-Object byte[] 8
  for ($i = 0; $i -lt $data.Length; $i++) { $message.DATA[$i] = $data[$i] }
  return $message
}

function Frame-Object($message, $timestamp) {
  $parts = @()
  for ($i = 0; $i -lt [int]$message.LEN; $i++) { $parts += ('{0:x2}' -f $message.DATA[$i]) }
  $timestampUs = (([int64]$timestamp.millis) + ([int64]$timestamp.millis_overflow * 4294967296)) * 1000 + [int64]$timestamp.micros
  return @{
    id = [int64]$message.ID
    id_hex = ('0x{0:x}' -f $message.ID)
    extended = (($message.MSGTYPE -band 0x02) -ne 0)
    rtr = (($message.MSGTYPE -band 0x01) -ne 0)
    data_hex = ($parts -join '')
    dlc = [int]$message.LEN
    timestamp_us = $timestampUs
  }
}

function Handle-Request($request) {
  $requestId = $request.request_id
  $op = [string]$request.op
  switch ($op) {
    'open' {
      if ([bool](Property-Value $request 'fd' $false)) {
        Reply $requestId @{ ok = $false; backend = 'peak'; error_type = 'unsupported_can_option'; summary = 'The initial PEAK backend supports classic CAN frames only.' }
        return
      }
      if ([bool](Property-Value $request 'listen_only' $false)) {
        Reply $requestId @{ ok = $false; backend = 'peak'; error_type = 'unsupported_can_option'; summary = 'listen_only is not implemented by the initial PEAK backend.' }
        return
      }
      if ([bool](Property-Value $request 'receive_own_messages' $false)) {
        Reply $requestId @{ ok = $false; backend = 'peak'; error_type = 'unsupported_can_option'; summary = 'receive_own_messages is not implemented by the initial PEAK backend.' }
        return
      }
      $channel = Channel-Value $request.channel
      $baudrate = Baudrate-Value ([int]$request.bitrate)
      $status = [PcanBasicNative]::CAN_Initialize($channel, $baudrate, 0, 0, 0)
      if ($status -ne [PcanBasicNative]::PCAN_ERROR_OK) {
        Reply $requestId (Pcan-Error 'PEAK CAN channel could not be initialized.' $status)
        return
      }
      if ([bool](Property-Value $request 'clear_rx_queue' $true)) { [void][PcanBasicNative]::CAN_Reset($channel) }
      $script:Open = $true
      $script:Channel = $channel
      Reply $requestId @{ ok = $true; backend = 'peak'; channel = $request.channel; bitrate = [int]$request.bitrate; summary = 'PEAK PCANBasic channel initialized.' }
      return
    }
    'send' {
      if (-not $script:Open) { Reply $requestId @{ ok = $false; backend = 'peak'; error_type = 'session_not_active'; summary = 'PEAK CAN channel is not open.' }; return }
      $message = Message-FromFrame $request.frame
      $status = [PcanBasicNative]::CAN_Write($script:Channel, [ref]$message)
      if ($status -ne [PcanBasicNative]::PCAN_ERROR_OK) { Reply $requestId (Pcan-Error 'PEAK CAN frame write failed.' $status); return }
      Reply $requestId @{ ok = $true; backend = 'peak'; summary = 'PEAK CAN frame written.' }
      return
    }
    'read' {
      if (-not $script:Open) { Reply $requestId @{ ok = $false; backend = 'peak'; error_type = 'session_not_active'; summary = 'PEAK CAN channel is not open.' }; return }
      $maxFrames = [Math]::Max(1, [int]$request.max_frames)
      $waitTimeoutS = [Math]::Max(0, [double]$request.wait_timeout_s)
      $pollMs = [Math]::Max(1, [int](Property-Value $request 'poll_interval_ms' 10))
      $deadline = [DateTime]::UtcNow.AddMilliseconds($waitTimeoutS * 1000.0)
      $frames = @()
      while ($true) {
        while ($frames.Count -lt $maxFrames) {
          $message = New-Object PcanBasicNative+TPCANMsg
          $timestamp = New-Object PcanBasicNative+TPCANTimestamp
          $status = [PcanBasicNative]::CAN_Read($script:Channel, [ref]$message, [ref]$timestamp)
          if ($status -eq [PcanBasicNative]::PCAN_ERROR_OK) { $frames += (Frame-Object $message $timestamp); continue }
          if ($status -eq [PcanBasicNative]::PCAN_ERROR_QRCVEMPTY) { break }
          Reply $requestId (Pcan-Error 'PEAK CAN frame read failed.' $status)
          return
        }
        if ($frames.Count -gt 0 -or $waitTimeoutS -le 0 -or [DateTime]::UtcNow -ge $deadline) { break }
        Start-Sleep -Milliseconds $pollMs
      }
      Reply $requestId @{ ok = $true; backend = 'peak'; frames = @($frames); summary = 'PEAK CAN read completed.' }
      return
    }
    'close' {
      if ($script:Open) { [void][PcanBasicNative]::CAN_Uninitialize($script:Channel) }
      $script:Open = $false
      $script:Shutdown = $true
      Reply $requestId @{ ok = $true; backend = 'peak'; summary = 'PEAK PCANBasic channel closed.' }
      return
    }
    default {
      Reply $requestId @{ ok = $false; backend = 'peak'; error_type = 'invalid_argument'; summary = "Unknown bridge operation '$op'." }
      return
    }
  }
}

try {
  while (($line = [Console]::In.ReadLine()) -ne $null) {
    if (-not $line.Trim()) { continue }
    try {
      $request = $line | ConvertFrom-Json
      Handle-Request $request
      if ($script:Shutdown) { break }
    } catch {
      $fallbackId = $null
      try { $fallbackId = (($line | ConvertFrom-Json).request_id) } catch {}
      if ($fallbackId -ne $null) {
        Reply $fallbackId @{ ok = $false; backend = 'peak'; error_type = 'can_adapter_bridge_error'; summary = $_.Exception.Message }
      } else {
        Write-Json @{ ok = $false; backend = 'peak'; error_type = 'can_adapter_bridge_error'; summary = $_.Exception.Message }
      }
    }
  }
} finally {
  if ($script:Open) { [void][PcanBasicNative]::CAN_Uninitialize($script:Channel) }
}
`;
}
