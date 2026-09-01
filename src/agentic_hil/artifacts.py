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
    ConfigError,
    atomic_write_bytes,
    configured_work_path,
    display_path,
    is_path_within,
    is_path_within_frozen,
    resolve_stable_directory,
    resolve_work_path,
    safe_configured_directory,
    safe_open_binary,
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

    def release_stage(self, artifact: JsonObject) -> None:
        if artifact.get("staged") is not True:
            return
        path = Path(str(artifact.get("resolved_path", "")))
        staging_root = Path(self._staging.name)
        if not is_path_within_frozen(path, staging_root):
            return
        with suppress(OSError):
            path.unlink()
        with suppress(OSError):
            path.parent.rmdir()

    def upload(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        if not self.config.artifacts.allow_upload:
            return {
                "ok": False,
                "tool": "artifact_upload",
                "error_type": "permission_denied",
                "summary": "Artifact upload is disabled by the authoritative config.",
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
        resolved = configured_work_path(self.config, image_path)
        validation: JsonObject = {
            "path_traversal_safe": not has_traversal_segment(image_path),
            "exists": False,
            "allowed_root": self._is_under_allowed_roots(resolved),
            "allowed_extension": resolved.suffix.lower() in self.config.artifacts.allowed_extensions,
            # Both views, because either one alone lies here. Lexical alone
            # reports a junction that leaves the workspace as contained: the
            # very case whose refusal this flag exists to name. Resolved alone
            # would call a path contained that only reaches the workspace by
            # following a link, which safe_open_binary's O_NOFOLLOW gate refuses.
            "within_workspace": is_path_within_frozen(resolved, Path(self.config.work_dir)) and is_path_within(resolved, Path(self.config.work_dir)),
            "sha256_computed": False,
        }
        validation["require_allowed_root"] = self.config.validation.require_allowed_root

        if not validation["path_traversal_safe"]:
            return self._validation_error("Firmware artifact path contains traversal segments.", validation)
        if self.config.validation.require_allowed_root and not validation["allowed_root"]:
            return self._validation_error("Firmware artifact is outside allowed artifact roots.", validation)
        if self.config.validation.require_allowed_extension and not validation["allowed_extension"]:
            return self._validation_error("Firmware artifact extension is not allowed.", validation)
        # safe_open_binary below refuses this too, and no longer as a symlink or
        # a regular-file claim: `_validated_absolute_file_path`, the first check
        # it makes, answers a path outside the workspace with "Path leaves the
        # workspace." That sentence is lost a few lines further down, where every
        # refusal out of the guarded open is folded into "must be a single-link
        # regular file that can be opened safely" with the real reason left in
        # backend_error. Name it here instead: no configuration key relaxes
        # workspace containment, so a caller told "regular file" would keep
        # retrying a path that can never work.
        if not validation["within_workspace"]:
            return self._validation_error(
                "Firmware artifact is outside workspace_root. Artifact paths must stay inside the configured workspace; no permission relaxes this.",
                validation,
            )

        sha256: str | None = None
        size_bytes: int | None = None
        integrity_sha256: str | None = None
        try:
            with safe_open_binary(resolved, workspace=self.config.work_dir) as handle:
                opened = os.fstat(handle.fileno())
                validation.update({"exists": True, "regular_file": True, "single_link": True})
                size_bytes = opened.st_size
                if size_bytes > self._max_upload_bytes():
                    return self._artifact_too_large(size_bytes, "flash_firmware")
                data = handle.read(self._max_upload_bytes() + 1)
        except FileNotFoundError:
            if self.config.validation.require_existing_file:
                return self._validation_error("Firmware artifact does not exist.", validation, "artifact_not_found")
            data = b""
        except (ConfigError, OSError) as error:
            # Asked before the object claims below, because a refusal about the
            # configured workspace establishes nothing about the file: the read
            # never got as far as looking at it, so writing regular_file: false
            # would assert a finding nobody made, on top of a summary that sends
            # the caller to check a file type that was never the problem.
            permanent = self._permanent_path_refusal(resolved, error, "flash_firmware")
            if permanent is not None:
                return {**permanent, "validation": validation}
            validation.update({"regular_file": False, "single_link": False})
            return self._validation_error("Firmware artifact must be a single-link regular file that can be opened safely.", {**validation, "backend_error": str(error)})
        if validation["exists"]:
            if len(data) > self._max_upload_bytes():
                return self._artifact_too_large(len(data), "flash_firmware")
            integrity_sha256 = hashlib.sha256(data).hexdigest()
            if self.config.validation.compute_sha256:
                sha256 = integrity_sha256
                validation["sha256_computed"] = True
            if self.config.validation.inspect_known_formats:
                validation.update(self._inspect_format_bytes(resolved.suffix.lower(), data))

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
                "integrity_sha256": integrity_sha256,
                "size_bytes": size_bytes,
                "validation": validation,
            },
            "validation": validation,
        }

    def stage_for_backend(self, artifact: JsonObject, tool: str) -> JsonObject:
        """Copy a validated artifact into private staging for the backend.

        Every refusal below happens before the backend is invoked, so each one
        carries side_effect_committed: False. Without that marker
        _result_requires_quarantine treats a plain re-validation failure as an
        unconfirmed hardware effect and quarantines the probe lease, which puts
        a bench into recovery over a firmware file that was never flashed."""
        source = Path(str(artifact["resolved_path"]))
        try:
            source_context = safe_open_binary(source, workspace=self.config.work_dir)
            source_file = source_context.__enter__()
            descriptor = source_file.fileno()
        except (ConfigError, OSError) as error:
            # A missing file that validation never saw either did not change:
            # it was never there. Saying "changed after validation" sends the
            # caller back to re-validate, which will keep succeeding under
            # validation.require_existing_file: false; the artifact has to be
            # built instead.
            if isinstance(error, FileNotFoundError) and artifact.get("integrity_sha256") is None:
                return {
                    "ok": False,
                    "tool": tool,
                    "error_type": "artifact_not_found",
                    "summary": "Firmware artifact does not exist and did not exist when it was validated. Build it first, then retry.",
                    "side_effect_committed": False,
                    "side_effect_status": "not_started",
                    "retry_safe": True,
                }
            permanent = self._permanent_path_refusal(source, error, tool)
            if permanent is not None:
                return permanent
            return {"ok": False, "tool": tool, "error_type": "artifact_changed", "summary": "Firmware artifact changed after validation.", "backend_error": str(error), "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}

        temporary_path: Path | None = None
        staging_directory: Path | None = None
        try:
            source_stat = os.fstat(descriptor)
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
                return {"ok": False, "tool": tool, "error_type": "artifact_changed", "summary": "Firmware artifact is no longer a single-link regular file.", "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}
            if source_stat.st_size > self._max_upload_bytes():
                return self._artifact_too_large(source_stat.st_size, tool)
            digest = hashlib.sha256()
            copied_bytes = 0
            suffix = source.suffix.lower()
            staging_directory = Path(tempfile.mkdtemp(dir=self._staging.name, prefix="stage-"))
            with tempfile.NamedTemporaryFile(dir=staging_directory, prefix=".tmp-", suffix=suffix, delete=False) as staged_file:
                temporary_path = Path(staged_file.name)
                for chunk in iter(lambda: source_file.read(SHA256_CHUNK_BYTES), b""):
                    copied_bytes += len(chunk)
                    if copied_bytes > self._max_upload_bytes():
                        return self._artifact_too_large(copied_bytes, tool)
                    digest.update(chunk)
                    staged_file.write(chunk)
                staged_file.flush()
                os.fsync(staged_file.fileno())
            actual_sha256 = digest.hexdigest()
            expected_sha256 = artifact.get("integrity_sha256") or artifact.get("sha256")
            # integrity_sha256 is None only when validation saw no file at all,
            # which validation.require_existing_file: false permits. Everything
            # that inspects content (size, ELF/HEX/BIN plausibility, the hash)
            # was skipped for those bytes, so a file that appears between
            # validation and staging would otherwise be flashed unchecked.
            if expected_sha256 is None:
                return {
                    "ok": False,
                    "tool": tool,
                    "error_type": "artifact_changed",
                    "summary": "Firmware artifact did not exist when it was validated, so its content was never checked. Build the artifact first, then retry.",
                    "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True,
                }
            if actual_sha256 != expected_sha256:
                return {"ok": False, "tool": tool, "error_type": "artifact_changed", "summary": "Firmware artifact content changed after validation.", "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}
            staged_path = staging_directory / source.name
            os.replace(temporary_path, staged_path)
            temporary_path = None
            staged_artifact = dict(artifact)
            staged_artifact.update({"resolved_path": str(staged_path), "sha256": actual_sha256, "integrity_sha256": actual_sha256, "size_bytes": copied_bytes, "staged": True})
            return {"ok": True, "artifact": staged_artifact}
        except OSError as error:
            return {
                "ok": False,
                "tool": tool,
                "error_type": "artifact_staging_failed",
                "summary": "Firmware artifact could not be copied into private backend staging.",
                "backend_error": str(error),
                "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True,
            }
        finally:
            source_context.__exit__(None, None, None)
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink()
            if staging_directory is not None:
                with suppress(OSError):
                    staging_directory.rmdir()

    def _permanent_path_refusal(self, source: Path, error: ConfigError | OSError, tool: str) -> JsonObject | None:
        """The guarded read's own refusal, wherever no retry can lift it.

        The guarded read's refusals arrive at a caller that folds them into one
        summary of its own: validation says the artifact "must be a single-link
        regular file that can be opened safely" and leaves the real sentence in
        `backend_error`. That fold is right about the case it was written for, a
        file that is not the object it has to be, and wrong about the two
        refusals that are about the configured workspace rather than about the
        file. A workspace whose spelling resolves somewhere else, which is what
        an MSIX AppContainer does to a project under a virtualized
        `%LOCALAPPDATA%`, refuses every read under it for as long as the
        configuration names that spelling; a path that leaves the workspace is
        refused because no configuration key relaxes containment. Neither of
        those moves because a caller tries again, so an answer about the file's
        type sends an operator to inspect a file that was never the problem,
        once and then forever.

        The workspace decides it, not the artifact's own parent, because
        redirection is a property of the tree: a root that resolves to itself is
        outside a redirected one, and a root that does not takes every path under
        it with it. An ancestor below a healthy workspace that becomes a symlink
        resolves elsewhere too, and that one really is about the file, as are a
        hard link and a directory standing where a file should be, so those keep
        the summary the caller already gives them.

        Both spellings the refusal carries come across with it: `path` is what
        the configuration names, `resolved_parent` is where it lands, and the
        resolved one is the answer, being the spelling that works. The fix comes
        from the error catalogue through `to_dict`, scoped by the type, so the
        remediation in the refusal and the one a caller looks up cannot drift
        apart.

        `side_effect_committed: false` rather than a new member of
        `_result_requires_quarantine`'s exempt set. That set is a claim about a
        whole error type, and it outranks even `side_effect_status: unknown`;
        `unsafe_configured_path` is raised by output writes and ledger appends
        too, where a side effect may well be in flight, so exempting the type
        would tell a bench that nothing happened on the strength of a name. The
        marker is the narrower claim and the true one: this refusal happens
        before the backend is invoked, so nothing was started here.
        """
        if not isinstance(error, ConfigError) or error.error_type != "unsafe_configured_path":
            return None
        if is_path_within_frozen(source, Path(self.config.work_dir)) and self._workspace_resolves_to_itself():
            return None
        return {
            "tool": tool,
            **error.to_dict(),
            "side_effect_committed": False,
            "side_effect_status": "not_started",
            "retry_safe": False,
        }

    def _workspace_resolves_to_itself(self) -> bool:
        """Whether the configured workspace is outside any redirected tree.

        `resolve_stable_directory` is that rule, asked rather than restated: the
        record walk's hand-rolled copy of this same comparison is what #361 took
        out, so that the question one caller asks and the rule every enforcer
        applies cannot drift apart. It answers a workspace this host cannot
        resolve at all the same way, and correctly: that is a configured root no
        retry repairs either.
        """
        try:
            resolve_stable_directory(Path(self.config.work_dir), field="workspace_root")
        except ConfigError:
            return False
        return True

    def resolve_artifact_id(self, artifact_id: str, tool: str = "flash_firmware") -> JsonObject:
        if not self.config.artifacts.allow_upload:
            return {
                "ok": False,
                "tool": tool,
                "error_type": "permission_denied",
                "summary": "Using uploaded artifacts is disabled by the authoritative config.",
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

    def validate_output_path(
        self,
        output_path: str,
        tool: str,
        allowed_extensions: list[str] | None = None,
        *,
        create_parent: bool = False,
    ) -> JsonObject:
        allowed_extensions = allowed_extensions or [".hex", ".ihex"]
        # resolve_work_path() resolves symlinks, and work_dir is already
        # resolved at load, so this comparison sees through a symlinked
        # directory inside the workspace that points somewhere else.
        resolved = Path(resolve_work_path(self.config, output_path))
        validation: JsonObject = {
            "path_traversal_safe": not has_traversal_segment(output_path),
            "allowed_root": self._is_under_allowed_roots(resolved),
            "allowed_extension": resolved.suffix.lower() in allowed_extensions,
            "within_workspace": is_path_within_frozen(resolved, Path(self.config.work_dir)),
        }
        if not validation["path_traversal_safe"]:
            return self._output_validation_error(tool, "Output path contains traversal segments.", validation)
        # Unconditional, unlike the allowed-roots check: no configuration key
        # relaxes workspace containment, and this is the reactor's only chance
        # to reject the path before a plan starts flashing and halting a board.
        if not validation["within_workspace"]:
            return self._output_validation_error(tool, "Output path is outside workspace_root; no permission relaxes this.", validation)
        if self.config.validation.require_allowed_root and not validation["allowed_root"]:
            return self._output_validation_error(tool, "Output path is outside allowed artifact roots.", validation)
        if not validation["allowed_extension"]:
            return self._output_validation_error(tool, "Output path extension is not allowed for this debug dump.", validation)
        if create_parent:
            safe_configured_directory(self.config, str(resolved.parent), f"{tool}.output_path")
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
        staged = self.stage_for_backend(source["artifact"], "artifact_upload")
        if not staged["ok"]:
            return staged
        staged_path = Path(staged["artifact"]["resolved_path"])
        try:
            data = staged_path.read_bytes()
        except OSError as error:
            return {
                "ok": False,
                "tool": "artifact_upload",
                "error_type": "artifact_not_found",
                "summary": "Firmware artifact could not be read.",
                "backend_error": str(error),
            }
        finally:
            self.release_stage(staged["artifact"])
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

    def _artifact_too_large(self, size_bytes: int, tool: str = "artifact_upload") -> JsonObject:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "artifact_too_large",
            "summary": "Firmware artifact exceeds configured max_upload_size_mb.",
            "bytes": size_bytes,
            "max_bytes": self._max_upload_bytes(),
            "side_effect_committed": False,
        }

    def _validation_error(self, summary: str, validation: JsonObject, error_type: str = "artifact_validation_failed") -> JsonObject:
        return {"ok": False, "tool": "flash_firmware", "error_type": error_type, "summary": summary, "validation": validation}

    def _output_validation_error(self, tool: str, summary: str, validation: JsonObject) -> JsonObject:
        return {"ok": False, "tool": tool, "error_type": "output_validation_failed", "summary": summary, "validation": validation}

    def _is_under_allowed_roots(self, resolved_path: Path) -> bool:
        roots = [configured_work_path(self.config, root) for root in self.config.artifacts.allowed_roots]
        if self.config.artifacts.allow_upload:
            roots.append(configured_work_path(self.config, self.config.artifacts.upload_directory))
        # Callers hand this both lexical and symlink-resolved paths, while the
        # roots are built lexically. Comparing in one view only would refuse a
        # legitimate dump into an allowed root that is itself a link inside the
        # workspace (pre-existing: validate_output_path has always resolved).
        # Containment is enforced separately by within_workspace, so matching in
        # either view widens nothing that policy relies on.
        return any(is_path_within_frozen(resolved_path, root) or is_path_within(resolved_path, root) for root in roots)

    def _inspect_format_bytes(self, suffix: str, data: bytes) -> JsonObject:
        if suffix == ".elf":
            return {"elf_header": data[:4] == b"\x7fELF"}
        if suffix == ".hex":
            return {"hex_parseable": looks_like_intel_hex_bytes(data)}
        if suffix == ".bin":
            return {"bin_size_plausible": bool(data)}
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


def looks_like_intel_hex_bytes(data: bytes) -> bool:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return False
    saw_record = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            return False
        payload = line[1:]
        if len(payload) < 10 or len(payload) % 2 != 0 or re.fullmatch(r"[0-9a-fA-F]+", payload) is None:
            return False
        record = bytes.fromhex(payload)
        if len(record) != record[0] + 5 or sum(record) & 0xFF:
            return False
        saw_record = True
    return saw_record


def has_traversal_segment(value: str) -> bool:
    return ".." in re.split(r"[/\\]+", value)
