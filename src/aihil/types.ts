export type JsonObject = Record<string, any>;

export interface TargetConfig {
  name: string;
  controller: string;
}

export interface DebuggerConfig {
  type: "openocd" | "stlink";
  executable: string | null;
  probe_id: string | null;
  interface: string;
  interface_cfg: string;
  target_cfg: string;
  flash_address: string | null;
  timeout_s: number;
}

export interface ArtifactsConfig {
  allowed_roots: string[];
  upload_directory: string;
  allowed_extensions: string[];
  max_upload_size_mb: number;
  allow_upload: boolean;
}

export interface ComPortConfig {
  device: string;
  baudrate: number;
  timeout_s: number;
  write_timeout_s: number;
  encoding: string;
  max_buffer_bytes: number;
  max_write_bytes: number;
}

export interface CanBusConfig {
  adapter: "peak" | "socketcan" | "process";
  channel: string;
  bitrate: number;
  fd: boolean;
  data_bitrate: number | null;
  pcanbasic_dll: string | null;
  executable: string | null;
  args: string[];
  timeout_s: number;
  poll_interval_ms: number;
  receive_own_messages: boolean;
  listen_only: boolean;
  max_buffer_frames: number;
  max_frame_data_bytes: number;
}

export interface ValidationConfig {
  require_existing_file: boolean;
  require_allowed_root: boolean;
  require_allowed_extension: boolean;
  compute_sha256: boolean;
  inspect_known_formats: boolean;
}

export interface PermissionsConfig {
  allow_probe: boolean;
  allow_flash: boolean;
  allow_reset: boolean;
  allow_com_read: boolean;
  allow_com_write: boolean;
  allow_can_read: boolean;
  allow_can_write: boolean;
  allow_raw_debugger_commands: boolean;
  allow_mass_erase: boolean;
}

export interface ReportsConfig {
  directory: string;
}

export interface LogsConfig {
  directory: string;
}

export interface AIHILConfig {
  configPath: string;
  workDir: string;
  target: TargetConfig;
  debugger: DebuggerConfig;
  artifacts: ArtifactsConfig;
  com_ports: Record<string, ComPortConfig>;
  can_buses: Record<string, CanBusConfig>;
  validation: ValidationConfig;
  permissions: PermissionsConfig;
  reports: ReportsConfig;
  logs: LogsConfig;
}
