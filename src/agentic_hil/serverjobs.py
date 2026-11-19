"""Prepare job data and run it under the authoritative bench's policy.

The launcher supplies identity and bench bindings. Nothing in a job directory
is configuration or executable code, and this module provides no admission,
queue, listener or retention service.
"""
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from agentic_hil.config import (
    ConfigError,
    absolute_without_symlinks,
    atomic_write_bytes,
    configured_work_path,
    configured_workspace_path,
    is_path_within,
    is_path_within_frozen,
    load_authoritative_config,
    safe_configured_directory,
    safe_directory,
    safe_file_lock,
    safe_file_path,
    validated_state_root_paths,
)
from agentic_hil.coordination import HardwareCoordinator, project_resource
from agentic_hil.knowledge import remediation_fields
from agentic_hil.reactorrun import run_plan
from agentic_hil.report import job_report_context, overall_success, with_job_fields, write_report
from agentic_hil.types import AgenticHILConfig, BenchOwnership, JsonObject


def _job_config(bench: AgenticHILConfig, job_id: str) -> AgenticHILConfig:
    if bench.server is None:
        raise ConfigError("config_invalid", "This bench has no server configuration.", {"field": "server"})
    # IDs are supplied by the trusted launcher and name one fresh directory.
    if not job_id or job_id in {".", ".."} or any(c in job_id for c in "/\\:") or Path(job_id).name != job_id:
        raise ConfigError("invalid_argument", "job_id must name one directory.", {"field": "job_id"})
    workspace = absolute_without_symlinks(Path(bench.server.job_root) / job_id / "workspace")
    validated_state_root_paths(bench.state_root, workspace.resolve(), bench.config_path)
    config_path = Path(bench.config_path)
    if is_path_within_frozen(config_path, workspace) or is_path_within(config_path.resolve(), workspace.resolve()):
        raise ConfigError("config_invalid", "The authoritative config must be stored outside the workspace.",
                          {"path": str(config_path.resolve()), "workspace_root": str(workspace)})

    def rebase(value: str, field: str) -> str:
        pinned = Path(configured_workspace_path(bench, value, field))
        return str(workspace / pinned.relative_to(bench.work_dir))

    ownership = BenchOwnership(project_resource(bench), str(Path(bench.work_dir).resolve()), str(config_path.resolve()))
    return replace(
        bench, work_dir=str(workspace), workspace_root=str(workspace), bench_ownership=ownership,
        artifacts=replace(
            bench.artifacts,
            allowed_roots=[rebase(root, "artifacts.allowed_roots") for root in bench.artifacts.allowed_roots],
            upload_directory=rebase(bench.artifacts.upload_directory, "artifacts.upload_directory"),
        ),
        reports=replace(bench.reports, directory=rebase(bench.reports.directory, "reports.directory")),
        logs=replace(bench.logs, directory=rebase(bench.logs.directory, "logs.directory")),
    )


def prepare_job(
    bench_config: AgenticHILConfig, *, job_id: str, submitted_by: str,
    plan_name: str, plan_bytes: bytes, artifacts: Mapping[str, bytes],
) -> AgenticHILConfig:
    """Place original bytes under guarded nested paths in a fresh workspace.

    The workspace is fresh when it is not there yet, or is the empty directory
    the job store made for it with the rest of the job's directory. Anything
    else is a workspace something already wrote into, and is refused."""
    job = _job_config(bench_config, job_id)
    # Check all targets before creating directories, using the writer's own
    # validator so escaped names keep the local refusal unchanged.
    files = [(plan_name, plan_bytes), *artifacts.items()]
    targets = [(safe_file_path(configured_work_path(job, name), workspace=job.work_dir), data) for name, data in files]
    root = safe_directory(Path(job.work_dir).parent.parent)
    with safe_file_lock(root / ("." + job_id + ".lock"), workspace=root):
        workspace = Path(job.work_dir)
        if os.path.lexists(workspace) and (workspace.is_symlink() or not workspace.is_dir() or any(workspace.iterdir())):
            raise FileExistsError(str(workspace))
        safe_directory(job.work_dir)
        for path, data in targets:
            safe_configured_directory(job, str(path.parent), "job.content")
            atomic_write_bytes(path, data, workspace=job.work_dir)
    return job


def run_job(
    *, bench_config_path: str, bench_workspace: str, job_id: str,
    submitted_by: str, plan_name: str, run_handle: str | None = None,
) -> JsonObject:
    """Run a prepared plan using only launcher-selected authoritative policy.

    `run_handle` is the handle the scheduler allocated for this attempt, which
    the run registers under so the scheduler finds its record."""
    with job_report_context(job_id, submitted_by):
        try:
            bench = load_authoritative_config(bench_workspace, config_path=bench_config_path)
            job = _job_config(bench, job_id)
            # A worker opens existing content. It never creates a missing job.
            if not Path(job.work_dir).is_dir():
                raise ConfigError("config_invalid", "Job workspace does not exist.", {"path": job.work_dir})
            safe_directory(job.work_dir)
            # A standing audit incident is a bench gate, not a failed job
            # effect to recover by resetting hardware at reactor teardown.
            # Read it before entering the reactor; acquire still rechecks it
            # under the normal coordination locks on every hardware call.
            status = HardwareCoordinator(job, "reactor").status(settle_dead_owner=False)
            if status["incident_stands"]:
                return write_report(job, {
                    "ok": False, "tool": "test_reactor_run", "error_type": "resource_quarantined",
                    "summary": "This bench has an unresolved incident; recover it before declaring a run.",
                    "quarantine_id": status["quarantine_id"], "cleanup_reasons": status["cleanup_reasons"],
                    "cleanup_required": True, "quarantined": True, "retry_safe": False,
                    **remediation_fields("resource_quarantined"),
                })
            return with_job_fields(run_plan(job, plan_name, run_handle=run_handle, return_failed_report=True))
        except ConfigError as error:
            return with_job_fields(error.to_dict())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-config", required=True)
    parser.add_argument("--bench-workspace", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--submitted-by", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--run-handle", default=None)
    args = parser.parse_args()
    result = run_job(bench_config_path=args.bench_config, bench_workspace=args.bench_workspace,
                     job_id=args.job_id, submitted_by=args.submitted_by, plan_name=args.plan,
                     run_handle=args.run_handle)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if overall_success(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
