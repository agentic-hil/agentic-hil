from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from agentic_hil.config import (
    atomic_write_bytes,
    configured_work_path,
    display_path,
    is_path_within_frozen,
    resolve_work_path,
    safe_configured_directory,
)
from agentic_hil.types import AgenticHILConfig, JsonObject


class ArtifactManager:
    def __init__(self, config: AgenticHILConfig):
        self.config = config
        self._staging = tempfile.TemporaryDirectory(prefix="agentic-hil-artifacts-")

    def reconfigure(self, config: AgenticHILConfig) -> None:
        self.config = config

    def close(self) -> None:
        self._staging.cleanup()

    def upload(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        if not self.config.artifacts.allow_upload:
            return {
                "ok": False,
                "tool": "artifact_upload",
                "error_type": "permission_denied",
                "summary": "Artifact upload is disabled by the effective policy.",
            }

        has_image_path = payload.get("image_path") is not None
        has_data_base64 = payload.get("data_base64") is not None
        if has_image_path == has_data_base64:
            return {
                "ok": False,
                "tool": "artifact_upload",
                "error_type": "invalid_argument",
                "summary": "Provide exactly one of image_path or data_base64.",
            }
        if has_image_path:
            return self._upload_local_path(str(payload["image_path"]))

        filename = upload_filename(payload.get("filename"))
        if not filename["ok"]:
            return filename
        decoded = decode_base64_payload(payload.get("data_base64"))
        if not decoded["ok"]:
            return decoded
        return self._store_uploaded_data(decoded["data"], str(filename["filename"]))

    def validate_local_path(self, image_path: str) -> JsonObject:
        resolved = Path(resolve_work_path(self.config, image_path))
        validation: JsonObject = {
            "path_traversal_safe": not has_traversal_segment(image_path),
            "exists": resolved.exists(),
            "allowed_root": self._is_under_allowed_roots(resolved),
            "allowed_extension": resolved.suffix.lower() in self.config.artifacts.allowed_extensions,
            "sha256_computed": False,
        }
        if validation["exists"]:
            validation["regular_file"] = resolved.is_file()
            validation["single_link"] = os.lstat(resolved).st_nlink == 1
        validation["require_allowed_root"] = self.config.validation.require_allowed_root

        if not validation["path_traversal_safe"]:
            return self._validation_error("Firmware artifact path contains traversal segments.", validation)
        if self.config.validation.require_existing_file and not validation["exists"]:
            return self._validation_error("Firmware artifact does not exist.", validation, "artifact_not_found")
        if self.config.validation.require_allowed_root and not validation["allowed_root"]:
            return self._validation_error("Firmware artifact is outside allowed artifact roots.", validation)
        if self.config.validation.require_allowed_extension and not validation["allowed_extension"]:
            return self._validation_error("Firmware artifact extension is not allowed.", validation)
        if validation.get("regular_file") is False or validation.get("single_link") is False:
            return self._validation_error("Firmware artifact must be a regular file with exactly one filesystem link.", validation)

        sha256: str | None = None
        size_bytes: int | None = None
        if validation["exists"]:
            size_bytes = resolved.stat().st_size
            if self.config.validation.compute_sha256:
                sha256 = sha256_file(resolved)
                validation["sha256_computed"] = True
            if self.config.validation.inspect_known_formats:
                validation.update(self._inspect_format(resolved))

        failed_plausibility = [
            key for key in ["elf_header", "hex_parseable", "bin_size_plausible"] if validation.get(key) is False
        ]
        if failed_plausibility:
            return self._validation_error("Firmware artifact failed basic format plausibility checks.", validation)

        return {
            "ok": True,
            "artifact": {
                "source": "path",
                "path": display_path(self.config, image_path),
                "resolved_path": str(resolved),
                "sha256": sha256,
                "size_bytes": size_bytes,
                "validation": validation,
            },
            "validation": validation,
        }

    def stage_for_backend(self, artifact: JsonObject, tool: str) -> JsonObject:
        source = Path(str(artifact["resolved_path"]))
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as error:
            return {"ok": False, "tool": tool, "error_type": "artifact_changed", "summary": "Firmware artifact changed after validation.", "backend_error": str(error)}

        temporary_path: Path | None = None
        try:
            source_stat = os.fstat(descriptor)
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
                return {"ok": False, "tool": tool, "error_type": "artifact_changed", "summary": "Firmware artifact is no longer a single-link regular file."}
            digest = hashlib.sha256()
            suffix = source.suffix.lower()
            with os.fdopen(descriptor, "rb") as source_file:
                descriptor = -1
                with tempfile.NamedTemporaryFile(dir=self._staging.name, prefix="stage-", suffix=suffix, delete=False) as staged_file:
                    temporary_path = Path(staged_file.name)
                    for chunk in iter(lambda: source_file.read(SHA256_CHUNK_BYTES), b""):
                        digest.update(chunk)
                        staged_file.write(chunk)
                    staged_file.flush()
                    os.fsync(staged_file.fileno())
            actual_sha256 = digest.hexdigest()
            expected_sha256 = artifact.get("sha256")
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                return {"ok": False, "tool": tool, "error_type": "artifact_changed", "summary": "Firmware artifact content changed after validation."}
            staged_path = Path(self._staging.name) / f"{actual_sha256}-{source.name}"
            os.replace(temporary_path, staged_path)
            temporary_path = None
            staged_artifact = dict(artifact)
            staged_artifact.update({"resolved_path": str(staged_path), "sha256": actual_sha256, "size_bytes": source_stat.st_size, "staged": True})
            return {"ok": True, "artifact": staged_artifact}
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink()

    def resolve_artifact_id(self, artifact_id: str, tool: str = "flash_firmware") -> JsonObject:
        if not self.config.artifacts.allow_upload:
            return {
                "ok": False,
                "tool": tool,
                "error_type": "permission_denied",
                "summary": "Using uploaded artifacts is disabled by the effective policy.",
                "artifact_id": artifact_id,
            }
        if not is_safe_artifact_id(artifact_id):
            return {
                "ok": False,
                "tool": tool,
                "error_type": "invalid_argument",
                "summary": "artifact_id must be a safe uploaded artifact id.",
                "artifact_id": artifact_id,
            }
        resolved = Path(safe_configured_directory(self.config, self.config.artifacts.upload_directory, "artifacts.upload_directory")) / artifact_id
        if not resolved.exists():
            return {
                "ok": False,
                "tool": tool,
                "error_type": "artifact_not_found",
                "summary": "Uploaded artifact could not be found.",
                "artifact_id": artifact_id,
            }

        validation = self.validate_local_path(str(resolved))
        if not validation["ok"]:
            validation["artifact_id"] = artifact_id
            validation["tool"] = tool
            return validation
        artifact = validation["artifact"]
        artifact["source"] = "upload"
        artifact["artifact_id"] = artifact_id
        return {"ok": True, "artifact": artifact, "validation": validation["validation"]}

    def validate_output_path(self, output_path: str, tool: str, allowed_extensions: list[str] | None = None) -> JsonObject:
        allowed_extensions = allowed_extensions or [".hex", ".ihex"]
        resolved = Path(resolve_work_path(self.config, output_path))
        validation: JsonObject = {
            "path_traversal_safe": not has_traversal_segment(output_path),
            "allowed_root": self._is_under_allowed_roots(resolved),
            "allowed_extension": resolved.suffix.lower() in allowed_extensions,
        }
        if not validation["path_traversal_safe"]:
            return self._output_validation_error(tool, "Output path contains traversal segments.", validation)
        if self.config.validation.require_allowed_root and not validation["allowed_root"]:
            return self._output_validation_error(tool, "Output path is outside allowed artifact roots.", validation)
        if not validation["allowed_extension"]:
            return self._output_validation_error(tool, "Output path extension is not allowed for this debug dump.", validation)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return {
            "ok": True,
            "output": {"path": display_path(self.config, output_path), "resolved_path": str(resolved)},
            "validation": validation,
        }

    def _upload_local_path(self, image_path: str) -> JsonObject:
        source = self.validate_local_path(image_path)
        if not source["ok"]:
            source["tool"] = "artifact_upload"
            return source
        size_bytes = int(source["artifact"].get("size_bytes") or 0)
        if size_bytes > self._max_upload_bytes():
            return self._artifact_too_large(size_bytes)
        try:
            data = Path(source["artifact"]["resolved_path"]).read_bytes()
        except OSError as error:
            return {
                "ok": False,
                "tool": "artifact_upload",
                "error_type": "artifact_not_found",
                "summary": "Firmware artifact could not be read.",
                "backend_error": str(error),
            }
        return self._store_uploaded_data(data, Path(image_path).name, display_path(self.config, image_path))

    def _store_uploaded_data(self, data: bytes, filename: str, source_path: str | None = None) -> JsonObject:
        if len(data) > self._max_upload_bytes():
            return self._artifact_too_large(len(data))

        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"{digest}{Path(filename).suffix.lower()}"
        upload_directory = Path(safe_configured_directory(self.config, self.config.artifacts.upload_directory, "artifacts.upload_directory"))
        stored_path = upload_directory / artifact_id
        if stored_path.is_symlink():
            return {
                "ok": False,
                "tool": "artifact_upload",
                "error_type": "unsafe_configured_path",
                "summary": "Uploaded artifact destination must not be a symlink.",
            }
        atomic_write_bytes(stored_path, data, workspace=self.config.work_dir)

        validation = self.validate_local_path(str(stored_path))
        if not validation["ok"]:
            with suppress(OSError):
                stored_path.unlink()
            validation["tool"] = "artifact_upload"
            validation["artifact_id"] = artifact_id
            return validation

        artifact = validation["artifact"]
        artifact.update({"source": "upload", "artifact_id": artifact_id, "original_filename": filename})
        if source_path is not None:
            artifact["source_path"] = source_path
        return {
            "ok": True,
            "tool": "artifact_upload",
            "artifact_id": artifact_id,
            "artifact": artifact,
            "validation": validation["validation"],
            "summary": "Firmware artifact uploaded and validated.",
        }

    def _max_upload_bytes(self) -> int:
        return max(0, self.config.artifacts.max_upload_size_mb) * 1024 * 1024

    def _artifact_too_large(self, size_bytes: int) -> JsonObject:
        return {
            "ok": False,
            "tool": "artifact_upload",
            "error_type": "artifact_too_large",
            "summary": "Uploaded artifact exceeds configured max_upload_size_mb.",
            "bytes": size_bytes,
            "max_bytes": self._max_upload_bytes(),
        }

    def _validation_error(self, summary: str, validation: JsonObject, error_type: str = "artifact_validation_failed") -> JsonObject:
        return {"ok": False, "tool": "flash_firmware", "error_type": error_type, "summary": summary, "validation": validation}

    def _output_validation_error(self, tool: str, summary: str, validation: JsonObject) -> JsonObject:
        return {"ok": False, "tool": tool, "error_type": "output_validation_failed", "summary": summary, "validation": validation}

    def _is_under_allowed_roots(self, resolved_path: Path) -> bool:
        roots = [configured_work_path(self.config, root) for root in self.config.artifacts.allowed_roots]
        if self.config.artifacts.allow_upload:
            roots.append(configured_work_path(self.config, self.config.artifacts.upload_directory))
        return any(is_path_within_frozen(resolved_path, root) for root in roots)

    def _inspect_format(self, file_path: Path) -> JsonObject:
        suffix = file_path.suffix.lower()
        if suffix == ".elf":
            try:
                with file_path.open("rb") as handle:
                    return {"elf_header": handle.read(4) == b"\x7fELF"}
            except OSError:
                return {"elf_header": False}
        if suffix == ".hex":
            return {"hex_parseable": looks_like_intel_hex(file_path)}
        if suffix == ".bin":
            try:
                return {"bin_size_plausible": file_path.stat().st_size > 0}
            except OSError:
                return {"bin_size_plausible": False}
        return {}


SHA256_CHUNK_BYTES = 1024 * 1024


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(SHA256_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_filename(value: object) -> JsonObject:
    if not isinstance(value, str) or not value.strip():
        return {"ok": False, "tool": "artifact_upload", "error_type": "invalid_argument", "summary": "filename must be a non-empty string."}
    filename = value.strip()
    if "/" in filename or "\\" in filename or "\0" in filename or has_traversal_segment(filename):
        return {
            "ok": False,
            "tool": "artifact_upload",
            "error_type": "invalid_argument",
            "summary": "filename must not contain path separators or traversal segments.",
        }
    return {"ok": True, "filename": filename}


def decode_base64_payload(value: object) -> JsonObject:
    if not isinstance(value, str) or not value.strip():
        return {"ok": False, "tool": "artifact_upload", "error_type": "invalid_argument", "summary": "data_base64 must be a non-empty base64 string."}
    compact = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?", compact):
        return {"ok": False, "tool": "artifact_upload", "error_type": "invalid_argument", "summary": "data_base64 must contain valid padded base64 data."}
    try:
        data = base64.b64decode(compact, validate=True)
    except binascii.Error:
        return {"ok": False, "tool": "artifact_upload", "error_type": "invalid_argument", "summary": "data_base64 must contain valid padded base64 data."}
    if not data:
        return {"ok": False, "tool": "artifact_upload", "error_type": "invalid_argument", "summary": "Uploaded artifact must not be empty."}
    return {"ok": True, "data": data}


def is_safe_artifact_id(value: str) -> bool:
    return re.fullmatch(r"[a-f0-9]{64}(?:\.[A-Za-z0-9_.-]+)?", value) is not None


def looks_like_intel_hex(file_path: Path) -> bool:
    saw_record = False
    try:
        with file_path.open("r", encoding="ascii") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                if not line.startswith(":"):
                    return False
                payload = line[1:]
                if len(payload) < 10 or len(payload) % 2 != 0 or re.fullmatch(r"[0-9a-fA-F]+", payload) is None:
                    return False
                data = bytes.fromhex(payload)
                byte_count = data[0]
                if len(data) != byte_count + 5:
                    return False
                if sum(data) & 0xFF:
                    return False
                saw_record = True
    except (OSError, UnicodeDecodeError):
        return False
    return saw_record


def has_traversal_segment(value: str) -> bool:
    return ".." in re.split(r"[/\\]+", value)
