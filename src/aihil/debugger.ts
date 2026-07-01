import type { AIHILConfig, JsonObject } from "./types.js";
import { ConfigError } from "./config.js";
import { OpenOCDBackend } from "./debuggers/openocd.js";
import { STLinkBackend } from "./debuggers/stlink.js";

export interface DebuggerBackend {
  info(): Promise<JsonObject>;
  probeTarget(): Promise<JsonObject>;
  flashFirmware(artifact: JsonObject): Promise<JsonObject>;
  resetTarget(mode?: string): Promise<JsonObject>;
  debugStartSession(artifact: JsonObject, mode?: string, timeoutS?: number): Promise<JsonObject>;
  debugStopSession(timeoutS?: number): Promise<JsonObject>;
  debugGetSessionStatus(): Promise<JsonObject>;
  debugSetBreakpoint(location: JsonObject): Promise<JsonObject>;
  debugListBreakpoints(): Promise<JsonObject>;
  debugClearBreakpoints(): Promise<JsonObject>;
  debugContinue(timeoutS?: number): Promise<JsonObject>;
  debugHalt(timeoutS?: number): Promise<JsonObject>;
  debugGetStopReason(): Promise<JsonObject>;
  debugSymbolInfo(symbol: string): Promise<JsonObject>;
  debugDumpSymbolIhex(symbol: string, output: JsonObject): Promise<JsonObject>;
  classifyLastError(): Promise<JsonObject>;
  close(): Promise<void>;
}

export function createDebuggerBackend(config: AIHILConfig): DebuggerBackend {
  if (config.debugger.type === "openocd") {
    return new OpenOCDBackend(config);
  }
  if (config.debugger.type === "stlink") {
    return new STLinkBackend(config);
  }
  throw new ConfigError("config_invalid", "Unsupported debugger.type.", {
    field: "debugger.type",
    value: config.debugger.type,
    allowed_values: ["openocd", "stlink"],
  });
}
