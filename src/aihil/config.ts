import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Ajv2020 } from "ajv/dist/2020.js";
import type { ErrorObject } from "ajv";
import YAML from "yaml";
import type {
  AIHILConfig,
  ArtifactsConfig,
  CanBusConfig,
  ComPortConfig,
  DebugInterfaceConfig,
  DebuggerConfig,
  JsonObject,
  LogsConfig,
  PermissionsConfig,
  ReportsConfig,
  TargetConfig,
  ValidationConfig,
} from "./types.js";

export const DEFAULT_CONFIG_PATH = ".aihil/config.yaml";
export const CONFIG_SCHEMA_ID = "https://aihil.local/schemas/config.schema.json";
export const CONFIG_SCHEMA_RESOURCE = "schemas/config.schema.json";

export class ConfigError extends Error {
  readonly errorType: string;
  readonly summary: string;
  readonly details: JsonObject;

  constructor(errorType: string, summary: string, details: JsonObject = {}) {
    super(summary);
    this.name = "ConfigError";
    this.errorType = errorType;
    this.summary = summary;
    this.details = details;
  }

  toDict(): JsonObject {
    return {
      ok: false,
      error_type: this.errorType,
      summary: this.summary,
      ...this.details,
    };
  }
}

export function configSchemaText(): string {
  return readFileSync(schemaPath(), "utf8");
}

export function configSchema(): JsonObject {
  return JSON.parse(configSchemaText()) as JsonObject;
}

export function validateConfigSchema(raw: JsonObject, configPath?: string): void {
  const schema = configSchema();
  const ajv = new Ajv2020({ allErrors: false, strict: false });
  try {
    ajv.compile(schema);
  } catch (error) {
    const details: JsonObject = {
      schema: CONFIG_SCHEMA_RESOURCE,
      schema_error: error instanceof Error ? error.message : String(error),
    };
    if (configPath !== undefined) {
      details.path = configPath;
    }
    throw new ConfigError("config_schema_invalid", "Bundled AI-HIL configuration schema is invalid.", details);
  }

  const validate = ajv.compile(schema);
  if (!validate(raw)) {
    raiseConfigValidationError(validate.errors?.[0], configPath);
  }
}

export function resolveConfigPath(configPath?: string | null): string {
  return configPath ?? DEFAULT_CONFIG_PATH;
}

export function loadConfig(configPath?: string | null, workDir?: string | null): AIHILConfig {
  const resolvedConfigPath = resolveConfigPath(configPath);
  const base = path.resolve(workDir ?? process.cwd());
  if (!existsSync(resolvedConfigPath)) {
    throw new ConfigError("config_file_not_found", "AI-HIL configuration file could not be found.", {
      path: resolvedConfigPath,
    });
  }

  let loaded: unknown;
  try {
    loaded = YAML.parse(readFileSync(resolvedConfigPath, "utf8"));
  } catch (error) {
    throw new ConfigError("config_invalid", "AI-HIL configuration file is not valid YAML.", {
      path: resolvedConfigPath,
    });
  }

  const raw = loaded ?? {};
  if (!isRecord(raw)) {
    throw new ConfigError("config_invalid", "AI-HIL configuration root must be a mapping.", {
      path: resolvedConfigPath,
    });
  }
  validateConfigSchema(raw, resolvedConfigPath);

  const targetRaw = mapping(raw.target, "target");
  const debuggerRaw = mapping(raw.debugger, "debugger");
  const debugRaw = mapping(raw.debug, "debug");
  const debuggerType = String(debuggerRaw.type ?? "openocd");
  const allowedDebuggerTypes = ["openocd", "stlink"];
  if (!allowedDebuggerTypes.includes(debuggerType)) {
    throw new ConfigError("config_invalid", "Unsupported debugger.type.", {
      field: "debugger.type",
      value: debuggerType,
      allowed_values: allowedDebuggerTypes,
    });
  }
  const artifactsRaw = mapping(raw.artifacts, "artifacts");
  const comPortsRaw = mapping(raw.com_ports, "com_ports");
  const canBusesRaw = mapping(raw.can_buses, "can_buses");
  const validationRaw = mapping(raw.validation, "validation");
  const permissionsRaw = mapping(raw.permissions, "permissions");
  const reportsRaw = mapping(raw.reports, "reports");
  const logsRaw = mapping(raw.logs, "logs");

  return {
    configPath: resolvedConfigPath,
    workDir: base,
    target: targetConfig(targetRaw),
    debugger: debuggerConfig(debuggerRaw, debuggerType),
    debug: debugInterfaceConfig(debugRaw),
    artifacts: artifactsConfig(artifactsRaw),
    com_ports: Object.fromEntries(Object.entries(comPortsRaw).map(([name, value]) => [name, comPortConfig(name, value)])),
    can_buses: Object.fromEntries(Object.entries(canBusesRaw).map(([name, value]) => [name, canBusConfig(name, value)])),
    validation: validationConfig(validationRaw),
    permissions: permissionsConfig(permissionsRaw),
    reports: reportsConfig(reportsRaw),
    logs: logsConfig(logsRaw),
  };
}

export function resolveWorkPath(config: AIHILConfig, requestedPath: string): string {
  const candidate = path.isAbsolute(requestedPath) ? requestedPath : path.join(config.workDir, requestedPath);
  return path.resolve(candidate);
}

export function displayPath(config: AIHILConfig, requestedPath: string): string {
  if (!path.isAbsolute(requestedPath)) {
    return toPosix(requestedPath);
  }
  const relative = path.relative(config.workDir, path.resolve(requestedPath));
  if (relative && !relative.startsWith("..") && !path.isAbsolute(relative)) {
    return toPosix(relative);
  }
  return requestedPath;
}

export function toPosix(value: string): string {
  return value.replace(/\\/g, "/");
}

function schemaPath(): string {
  const currentDir = path.dirname(fileURLToPath(import.meta.url));
  return path.join(currentDir, "schemas", "config.schema.json");
}

function raiseConfigValidationError(error: ErrorObject | undefined, configPath?: string): never {
  const details: JsonObject = { field: schemaErrorField(error) };
  if (configPath !== undefined) {
    details.path = configPath;
  }

  if (error?.keyword === "additionalProperties") {
    details.allowed_fields = schemaAllowedFields(error);
    throw new ConfigError("config_invalid", "Unknown AI-HIL configuration field.", details);
  }
  if (error?.keyword === "enum") {
    details.allowed_values = (error.params as JsonObject).allowedValues ?? [];
    details.value = error.data;
    throw new ConfigError("config_invalid", `${details.field} has an unsupported value.`, details);
  }
  if (error?.keyword === "type") {
    details.expected_type = error.schema;
    details.value = error.data;
    throw new ConfigError("config_invalid", `${details.field} has the wrong type.`, details);
  }

  details.schema_error = error?.message ?? "Configuration validation failed.";
  details.value = error?.data;
  throw new ConfigError("config_invalid", error?.message ?? "Configuration validation failed.", details);
}

function schemaErrorField(error: ErrorObject | undefined): string {
  if (error === undefined) {
    return "$";
  }
  const parts = error.instancePath.split("/").filter(Boolean).map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
  if (error.keyword === "additionalProperties") {
    const additional = (error.params as JsonObject).additionalProperty;
    if (additional !== undefined) {
      parts.push(String(additional));
    }
  }
  return formatFieldPath(parts);
}

function formatFieldPath(parts: Array<string | number>): string {
  let result = "";
  for (const part of parts) {
    if (typeof part === "number" || /^\d+$/.test(String(part))) {
      result = result ? `${result}[${part}]` : `[${part}]`;
    } else {
      result = result ? `${result}.${part}` : String(part);
    }
  }
  return result || "$";
}

function schemaAllowedFields(error: ErrorObject): string[] {
  const schema = error.parentSchema;
  if (!isRecord(schema?.properties)) {
    return [];
  }
  return Object.keys(schema.properties).sort();
}

function targetConfig(raw: JsonObject): TargetConfig {
  return {
    name: String(raw.name ?? "unknown-target"),
    controller: String(raw.controller ?? "unknown-controller"),
  };
}

function debuggerConfig(raw: JsonObject, debuggerType: string): DebuggerConfig {
  return {
    type: debuggerType as "openocd" | "stlink",
    executable: optionalString(raw.executable),
    probe_id: optionalString(raw.probe_id),
    interface: String(raw.interface ?? "SWD"),
    interface_cfg: String(raw.interface_cfg ?? "interface/stlink.cfg"),
    target_cfg: String(raw.target_cfg ?? "target/stm32f4x.cfg"),
    flash_address: optionalString(raw.flash_address),
    timeout_s: Number(raw.timeout_s ?? 60),
  };
}

function debugInterfaceConfig(raw: JsonObject): DebugInterfaceConfig {
  return {
    gdb_executable: optionalString(raw.gdb_executable),
    allowed_symbols: stringList(raw.allowed_symbols, []),
    max_dump_size_bytes: positiveIntegerConfig(raw.max_dump_size_bytes, 1024 * 1024, "debug.max_dump_size_bytes"),
  };
}

function artifactsConfig(raw: JsonObject): ArtifactsConfig {
  return {
    allowed_roots: stringList(raw.allowed_roots, ["build"]),
    upload_directory: String(raw.upload_directory ?? ".aihil/artifacts"),
    allowed_extensions: stringList(raw.allowed_extensions, [".elf", ".hex", ".bin"]).map((extension) => extension.toLowerCase()),
    max_upload_size_mb: Number.parseInt(String(raw.max_upload_size_mb ?? 64), 10),
    allow_upload: Boolean(raw.allow_upload ?? true),
  };
}

function comPortConfig(name: string, value: unknown): ComPortConfig {
  const raw = mapping(value, `com_ports.${name}`);
  return {
    device: String(raw.device),
    baudrate: Number.parseInt(String(raw.baudrate ?? 115200), 10),
    timeout_s: Number(raw.timeout_s ?? 0.1),
    write_timeout_s: Number(raw.write_timeout_s ?? 1.0),
    encoding: String(raw.encoding ?? "utf-8"),
    max_buffer_bytes: Number.parseInt(String(raw.max_buffer_bytes ?? 65536), 10),
    max_write_bytes: Number.parseInt(String(raw.max_write_bytes ?? 4096), 10),
  };
}

function canBusConfig(name: string, value: unknown): CanBusConfig {
  const raw = mapping(value, `can_buses.${name}`);
  const adapter = String(raw.adapter ?? "peak");
  const allowedAdapters = ["peak", "socketcan", "process"];
  if (!allowedAdapters.includes(adapter)) {
    throw new ConfigError("config_invalid", "Unsupported can_buses adapter.", {
      field: `can_buses.${name}.adapter`,
      value: adapter,
      allowed_values: allowedAdapters,
    });
  }
  const fd = Boolean(raw.fd ?? false);
  return {
    adapter: adapter as "peak" | "socketcan" | "process",
    channel: String(raw.channel),
    bitrate: Number.parseInt(String(raw.bitrate ?? 500000), 10),
    fd,
    data_bitrate: raw.data_bitrate === undefined || raw.data_bitrate === null ? null : Number.parseInt(String(raw.data_bitrate), 10),
    pcanbasic_dll: optionalString(raw.pcanbasic_dll),
    executable: optionalString(raw.executable),
    args: stringList(raw.args, []),
    timeout_s: Number(raw.timeout_s ?? 10.0),
    poll_interval_ms: Number.parseInt(String(raw.poll_interval_ms ?? 10), 10),
    receive_own_messages: Boolean(raw.receive_own_messages ?? false),
    listen_only: Boolean(raw.listen_only ?? false),
    max_buffer_frames: Number.parseInt(String(raw.max_buffer_frames ?? 1024), 10),
    max_frame_data_bytes: Number.parseInt(String(raw.max_frame_data_bytes ?? (fd ? 64 : 8)), 10),
  };
}

function validationConfig(raw: JsonObject): ValidationConfig {
  return {
    require_existing_file: Boolean(raw.require_existing_file ?? true),
    require_allowed_root: Boolean(raw.require_allowed_root ?? true),
    require_allowed_extension: Boolean(raw.require_allowed_extension ?? true),
    compute_sha256: Boolean(raw.compute_sha256 ?? true),
    inspect_known_formats: Boolean(raw.inspect_known_formats ?? true),
  };
}

function permissionsConfig(raw: JsonObject): PermissionsConfig {
  return {
    allow_probe: Boolean(raw.allow_probe ?? true),
    allow_flash: Boolean(raw.allow_flash ?? true),
    allow_reset: Boolean(raw.allow_reset ?? true),
    allow_com_read: Boolean(raw.allow_com_read ?? true),
    allow_com_write: Boolean(raw.allow_com_write ?? true),
    allow_can_read: Boolean(raw.allow_can_read ?? true),
    allow_can_write: Boolean(raw.allow_can_write ?? true),
    allow_raw_debugger_commands: Boolean(raw.allow_raw_debugger_commands ?? false),
    allow_mass_erase: Boolean(raw.allow_mass_erase ?? false),
  };
}

function reportsConfig(raw: JsonObject): ReportsConfig {
  return { directory: String(raw.directory ?? ".aihil/reports") };
}

function logsConfig(raw: JsonObject): LogsConfig {
  return { directory: String(raw.directory ?? ".aihil/logs") };
}

function mapping(value: unknown, fieldName: string): JsonObject {
  if (value === undefined || value === null) {
    return {};
  }
  if (!isRecord(value)) {
    throw new ConfigError("config_invalid", `${fieldName} must be a mapping.`, { field: fieldName });
  }
  return value;
}

function optionalString(value: unknown): string | null {
  if (value === undefined || value === null) {
    return null;
  }
  const text = String(value);
  return text ? text : null;
}

function stringList(value: unknown, defaultValue: string[]): string[] {
  if (value === undefined || value === null) {
    return [...defaultValue];
  }
  if (!Array.isArray(value)) {
    throw new ConfigError("config_invalid", "Configuration value must be a list.");
  }
  return value.map((item) => String(item));
}

function positiveIntegerConfig(value: unknown, defaultValue: number, field: string): number {
  const parsed = Number(value ?? defaultValue);
  if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < 1) {
    throw new ConfigError("config_invalid", `${field} must be a finite integer >= 1.`, { field, value });
  }
  return parsed;
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
