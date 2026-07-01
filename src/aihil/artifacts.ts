import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import type { AIHILConfig, JsonObject } from "./types.js";
import { displayPath, resolveWorkPath } from "./config.js";

export class ArtifactManager {
  constructor(private readonly config: AIHILConfig) {}

  upload(payload: JsonObject | null = {}): JsonObject {
    if (!this.config.artifacts.allow_upload) {
      return {
        ok: false,
        tool: "aihil_artifact_upload",
        error_type: "permission_denied",
        summary: "Artifact upload is disabled by .aihil/config.yaml.",
      };
    }

    const hasImagePath = payload?.image_path !== undefined && payload.image_path !== null;
    const hasDataBase64 = payload?.data_base64 !== undefined && payload.data_base64 !== null;
    if (hasImagePath === hasDataBase64) {
      return {
        ok: false,
        tool: "aihil_artifact_upload",
        error_type: "invalid_argument",
        summary: "Provide exactly one of image_path or data_base64.",
      };
    }
    if (hasImagePath) {
      return this.uploadLocalPath(String(payload.image_path));
    }

    const filename = uploadFilename(payload?.filename);
    if (!filename.ok) {
      return filename;
    }
    const decoded = decodeBase64Payload(payload?.data_base64);
    if (!decoded.ok) {
      return decoded;
    }

    return this.storeUploadedData(decoded.data as Buffer, String(filename.filename));
  }

  private uploadLocalPath(imagePath: string): JsonObject {
    const source = this.validateLocalPath(imagePath);
    if (!source.ok) {
      source.tool = "aihil_artifact_upload";
      return source;
    }

    let data: Buffer;
    try {
      data = readFileSync(String((source.artifact as JsonObject).resolved_path));
    } catch (error) {
      return {
        ok: false,
        tool: "aihil_artifact_upload",
        error_type: "artifact_not_found",
        summary: "Firmware artifact could not be read.",
        backend_error: error instanceof Error ? error.message : String(error),
      };
    }

    return this.storeUploadedData(data, path.basename(imagePath), displayPath(this.config, imagePath));
  }

  private storeUploadedData(data: Buffer, filename: string, sourcePath?: string): JsonObject {
    const maxBytes = Math.max(0, this.config.artifacts.max_upload_size_mb) * 1024 * 1024;
    if (data.length > maxBytes) {
      return {
        ok: false,
        tool: "aihil_artifact_upload",
        error_type: "artifact_too_large",
        summary: "Uploaded artifact exceeds configured max_upload_size_mb.",
        bytes: data.length,
        max_bytes: maxBytes,
      };
    }

    const sha256 = sha256Buffer(data);
    const extension = path.extname(filename).toLowerCase();
    const artifactId = `${sha256}${extension}`;
    const uploadDirectory = resolveWorkPath(this.config, this.config.artifacts.upload_directory);
    const storedPath = path.join(uploadDirectory, artifactId);
    mkdirSync(uploadDirectory, { recursive: true });
    writeFileSync(storedPath, data);

    const validation = this.validateLocalPath(storedPath);
    if (!validation.ok) {
      removeIfExists(storedPath);
      validation.tool = "aihil_artifact_upload";
      validation.artifact_id = artifactId;
      return validation;
    }

    const artifact = validation.artifact as JsonObject;
    artifact.source = "upload";
    artifact.artifact_id = artifactId;
    artifact.original_filename = filename;
    if (sourcePath !== undefined) {
      artifact.source_path = sourcePath;
    }
    return {
      ok: true,
      tool: "aihil_artifact_upload",
      artifact_id: artifactId,
      artifact,
      validation: validation.validation,
      summary: "Firmware artifact uploaded and validated.",
    };
  }

  validateLocalPath(imagePath: string): JsonObject {
    const resolved = resolveWorkPath(this.config, imagePath);
    const validation: JsonObject = {
      path_traversal_safe: !hasTraversalSegment(imagePath),
      exists: existsSync(resolved),
      allowed_root: this.isUnderAllowedRoots(resolved),
      allowed_extension: this.config.artifacts.allowed_extensions.includes(path.extname(resolved).toLowerCase()),
      sha256_computed: false,
    };
    validation.require_allowed_root = validation.allowed_root;

    if (!validation.path_traversal_safe) {
      return this.validationError("Firmware artifact path contains traversal segments.", validation);
    }
    if (this.config.validation.require_existing_file && !validation.exists) {
      return this.validationError("Firmware artifact does not exist.", validation, "artifact_not_found");
    }
    if (this.config.validation.require_allowed_root && !validation.allowed_root) {
      return this.validationError("Firmware artifact is outside allowed artifact roots.", validation);
    }
    if (this.config.validation.require_allowed_extension && !validation.allowed_extension) {
      return this.validationError("Firmware artifact extension is not allowed.", validation);
    }

    let sha256: string | null = null;
    let sizeBytes: number | null = null;
    if (validation.exists) {
      sizeBytes = statSync(resolved).size;
      if (this.config.validation.compute_sha256) {
        sha256 = sha256File(resolved);
        validation.sha256_computed = true;
      }
      if (this.config.validation.inspect_known_formats) {
        Object.assign(validation, this.inspectFormat(resolved));
      }
    }

    const failedPlausibility = ["elf_header", "hex_parseable", "bin_size_plausible"].filter(
      (key) => validation[key] === false,
    );
    if (failedPlausibility.length > 0) {
      return this.validationError("Firmware artifact failed basic format plausibility checks.", validation);
    }

    return {
      ok: true,
      artifact: {
        source: "path",
        path: displayPath(this.config, imagePath),
        resolved_path: resolved,
        sha256,
        size_bytes: sizeBytes,
        validation,
      },
      validation,
    };
  }

  resolveArtifactId(artifactId: string, tool = "aihil_flash_firmware"): JsonObject {
    if (!this.config.artifacts.allow_upload) {
      return {
        ok: false,
        tool,
        error_type: "permission_denied",
        summary: "Using uploaded artifacts is disabled by .aihil/config.yaml.",
        artifact_id: artifactId,
      };
    }
    if (!isSafeArtifactId(artifactId)) {
      return {
        ok: false,
        tool,
        error_type: "invalid_argument",
        summary: "artifact_id must be a safe uploaded artifact id.",
        artifact_id: artifactId,
      };
    }
    const resolved = path.join(resolveWorkPath(this.config, this.config.artifacts.upload_directory), artifactId);
    if (!existsSync(resolved)) {
      return {
        ok: false,
        tool,
        error_type: "artifact_not_found",
        summary: "Uploaded artifact could not be found.",
        artifact_id: artifactId,
      };
    }

    const validation = this.validateLocalPath(resolved);
    if (!validation.ok) {
      validation.artifact_id = artifactId;
      validation.tool = tool;
      return validation;
    }
    const artifact = validation.artifact as JsonObject;
    artifact.source = "upload";
    artifact.artifact_id = artifactId;
    return {
      ok: true,
      artifact,
      validation: validation.validation,
    };
  }

  validateOutputPath(outputPath: string, tool: string, allowedExtensions = [".hex", ".ihex"]): JsonObject {
    const resolved = resolveWorkPath(this.config, outputPath);
    const validation: JsonObject = {
      path_traversal_safe: !hasTraversalSegment(outputPath),
      allowed_root: this.isUnderAllowedRoots(resolved),
      allowed_extension: allowedExtensions.includes(path.extname(resolved).toLowerCase()),
    };

    if (!validation.path_traversal_safe) {
      return this.outputValidationError(tool, "Output path contains traversal segments.", validation);
    }
    if (this.config.validation.require_allowed_root && !validation.allowed_root) {
      return this.outputValidationError(tool, "Output path is outside allowed artifact roots.", validation);
    }
    if (!validation.allowed_extension) {
      return this.outputValidationError(tool, "Output path extension is not allowed for this debug dump.", validation);
    }

    mkdirSync(path.dirname(resolved), { recursive: true });
    return {
      ok: true,
      output: {
        path: displayPath(this.config, outputPath),
        resolved_path: resolved,
      },
      validation,
    };
  }

  private validationError(summary: string, validation: JsonObject, errorType = "artifact_validation_failed"): JsonObject {
    return {
      ok: false,
      tool: "aihil_flash_firmware",
      error_type: errorType,
      summary,
      validation,
    };
  }

  private outputValidationError(tool: string, summary: string, validation: JsonObject): JsonObject {
    return {
      ok: false,
      tool,
      error_type: "output_validation_failed",
      summary,
      validation,
    };
  }

  private isUnderAllowedRoots(resolvedPath: string): boolean {
    if (this.config.artifacts.allowed_roots.some((root) => isRelativeTo(resolvedPath, resolveWorkPath(this.config, root)))) {
      return true;
    }
    return this.config.artifacts.allow_upload && isRelativeTo(resolvedPath, resolveWorkPath(this.config, this.config.artifacts.upload_directory));
  }

  private inspectFormat(filePath: string): JsonObject {
    const suffix = path.extname(filePath).toLowerCase();
    if (suffix === ".elf") {
      try {
        return { elf_header: readFileSync(filePath).subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46])) };
      } catch {
        return { elf_header: false };
      }
    }
    if (suffix === ".hex") {
      return { hex_parseable: looksLikeIntelHex(filePath) };
    }
    if (suffix === ".bin") {
      try {
        return { bin_size_plausible: statSync(filePath).size > 0 };
      } catch {
        return { bin_size_plausible: false };
      }
    }
    return {};
  }
}

function sha256File(filePath: string): string {
  const digest = createHash("sha256");
  digest.update(readFileSync(filePath));
  return digest.digest("hex");
}

function sha256Buffer(data: Buffer): string {
  const digest = createHash("sha256");
  digest.update(data);
  return digest.digest("hex");
}

function uploadFilename(value: unknown): JsonObject {
  if (typeof value !== "string" || value.trim() === "") {
    return {
      ok: false,
      tool: "aihil_artifact_upload",
      error_type: "invalid_argument",
      summary: "filename must be a non-empty string.",
    };
  }
  const filename = value.trim();
  if (filename.includes("/") || filename.includes("\\") || filename.includes("\0") || hasTraversalSegment(filename)) {
    return {
      ok: false,
      tool: "aihil_artifact_upload",
      error_type: "invalid_argument",
      summary: "filename must not contain path separators or traversal segments.",
    };
  }
  return { ok: true, filename };
}

function decodeBase64Payload(value: unknown): JsonObject {
  if (typeof value !== "string" || value.trim() === "") {
    return {
      ok: false,
      tool: "aihil_artifact_upload",
      error_type: "invalid_argument",
      summary: "data_base64 must be a non-empty base64 string.",
    };
  }
  const compact = value.replace(/\s+/g, "");
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(compact)) {
    return {
      ok: false,
      tool: "aihil_artifact_upload",
      error_type: "invalid_argument",
      summary: "data_base64 must contain valid padded base64 data.",
    };
  }
  const data = Buffer.from(compact, "base64");
  if (data.length === 0) {
    return {
      ok: false,
      tool: "aihil_artifact_upload",
      error_type: "invalid_argument",
      summary: "Uploaded artifact must not be empty.",
    };
  }
  return { ok: true, data };
}

function isSafeArtifactId(value: string): boolean {
  return /^[a-f0-9]{64}(?:\.[A-Za-z0-9_.-]+)?$/.test(value);
}

function removeIfExists(filePath: string): void {
  try {
    if (existsSync(filePath)) {
      unlinkSync(filePath);
    }
  } catch {
    // Validation failure is the useful error; cleanup failure should not hide it.
  }
}

function looksLikeIntelHex(filePath: string): boolean {
  let lines: string[];
  try {
    lines = readFileSync(filePath, "ascii")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  } catch {
    return false;
  }
  if (lines.length === 0) {
    return false;
  }
  for (const line of lines) {
    if (!line.startsWith(":")) {
      return false;
    }
    const payload = line.slice(1);
    if (payload.length < 10 || payload.length % 2 !== 0 || !/^[0-9a-fA-F]+$/.test(payload)) {
      return false;
    }
    const data = Buffer.from(payload, "hex");
    const byteCount = data[0];
    if (data.length !== byteCount + 5) {
      return false;
    }
    const sum = data.reduce((total, byte) => total + byte, 0);
    if ((sum & 0xff) !== 0) {
      return false;
    }
  }
  return true;
}

function isRelativeTo(candidate: string, root: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function hasTraversalSegment(value: string): boolean {
  return value.split(/[\\/]+/).includes("..");
}
