from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .architecture import ArchitectureError, load_architecture_snapshot
from .audit_engine import AuditGuardError, load_build_evidence, verify_audit_preconditions
from .checkpoint_engine import CheckpointError, load_audit_evidence
from .config import ProjectConfig
from .hygiene import WorkspaceHygieneError, ignored_untracked_files
from .operation_lease import OperationLeaseError, inspect_operation_lease
from .run_manifest import (
    RunManifest,
    RunManifestError,
    list_run_manifests,
    resolve_evidence,
    run_directory,
)
from .state import state_directory
from .workspace import WorkspaceError, verify_executor_workspace


class DoctorError(RuntimeError):
    """Raised when YouMo doctor state itself is malformed or unreadable."""


@dataclass(frozen=True)
class WorkspaceDiagnosis:
    workspace: str
    state: str
    run_id: str | None
    run_stage: str | None
    branch: str | None
    head: str | None
    clean: bool | None
    lease: str
    ignored_artifacts: tuple[str, ...]
    warnings: tuple[str, ...]
    next_action: str
    detail: str

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DoctorReport:
    workspaces: tuple[WorkspaceDiagnosis, ...]

    @property
    def healthy(self) -> bool:
        bad = {
            "WORKSPACE_INVALID",
            "ARCHITECTURE_DRIFT",
            "STALE_EVIDENCE",
            "DIRTY_UNAUDITED",
            "INTERRUPTED_BUILD",
            "INTERRUPTED_AUDIT",
            "INTERRUPTED_CHECKPOINT",
            "BUILD_FAILED",
            "AUDIT_FAILED",
        }
        return all(item.state not in bad for item in self.workspaces)

    def to_json(self) -> str:
        return json.dumps(
            {
                "healthy": self.healthy,
                "workspaces": [item.to_mapping() for item in self.workspaces],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"


def _registry_paths(control_root: Path, config: ProjectConfig) -> tuple[Path, ...]:
    path = state_directory(control_root, config.state_dir) / "executor-workspaces.json"
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DoctorError(f"executor registry is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise DoctorError("executor registry schema is invalid")
    if payload.get("project_id") != config.project_id:
        raise DoctorError("executor registry project identity mismatch")
    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, dict):
        raise DoctorError("executor registry workspaces must be an object")
    result: list[Path] = []
    for raw in workspaces:
        if not isinstance(raw, str) or not raw:
            raise DoctorError("executor registry contains an invalid workspace path")
        result.append(Path(raw).expanduser().resolve())
    return tuple(sorted(result, key=str))


def registered_workspaces(control_root: Path, config: ProjectConfig) -> tuple[Path, ...]:
    return _registry_paths(control_root.resolve(), config)


def _expected_architecture(config: ProjectConfig, snapshot: object) -> dict[str, str]:
    source_hashes = getattr(snapshot, "source_sha256")
    result = {config.architecture_lock: getattr(snapshot, "lock_sha256")}
    result.update({path: source_hashes[path] for path in config.architecture_sources})
    return result


def _latest_run(control_root: Path, config: ProjectConfig, workspace: Path) -> RunManifest | None:
    runs = list_run_manifests(control_root, config, workspace=workspace)
    return runs[0] if runs else None


def _evidence_for_run(
    control_root: Path,
    config: ProjectConfig,
    run: RunManifest,
    *,
    audit_required: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    parent = run_directory(control_root, config, run.run_id)
    if run.build_evidence is None:
        raise DoctorError("run has no build evidence binding")
    build_path = resolve_evidence(run.build_evidence, expected_parent=parent)
    build = load_build_evidence(build_path)
    if not audit_required:
        return build, None
    if run.audit_evidence is None:
        raise DoctorError("run has no audit evidence binding")
    audit_path = resolve_evidence(run.audit_evidence, expected_parent=parent)
    audit = load_audit_evidence(audit_path)
    return build, audit


def _warnings_for_workspace(workspace: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        ignored = ignored_untracked_files(workspace)
    except WorkspaceHygieneError as exc:
        return (), (f"ignored-artifact inspection failed: {exc}",)
    warnings: list[str] = []
    if ignored:
        warnings.append(f"ignored artifacts present: {list(ignored[:20])!r}")
    return ignored, tuple(warnings)


def diagnose_workspace(
    control_root: Path,
    config: ProjectConfig,
    workspace: Path,
) -> WorkspaceDiagnosis:
    control = control_root.resolve()
    target = workspace.expanduser().resolve()
    warnings: list[str] = []
    try:
        report = verify_executor_workspace(
            control, target, config, require_clean=False, require_registry=True
        )
    except WorkspaceError as exc:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="WORKSPACE_INVALID",
            run_id=None,
            run_stage=None,
            branch=None,
            head=None,
            clean=None,
            lease="UNKNOWN",
            ignored_artifacts=(),
            warnings=(),
            next_action="inspect executor workspace manually; no YouMo execution is authorized",
            detail=str(exc),
        )
    if not report.passed or report.state is None:
        failures = tuple(
            f"{item.name}: {item.detail}" for item in report.checks if not item.passed
        )
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="WORKSPACE_INVALID",
            run_id=None,
            run_stage=None,
            branch=report.state.branch if report.state else None,
            head=report.state.head if report.state else None,
            clean=report.state.clean if report.state else None,
            lease="UNKNOWN",
            ignored_artifacts=(),
            warnings=failures,
            next_action="repair executor identity/binding before any agent execution",
            detail="executor verification failed",
        )

    state = report.state
    try:
        lease = inspect_operation_lease(control, config, target)
    except OperationLeaseError as exc:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="WORKSPACE_INVALID",
            run_id=None,
            run_stage=None,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease="INVALID",
            ignored_artifacts=(),
            warnings=(str(exc),),
            next_action="inspect malformed operation lease; no execution is authorized",
            detail="operation lease inspection failed",
        )

    ignored, ignored_warnings = _warnings_for_workspace(target)
    warnings.extend(ignored_warnings)

    try:
        control_arch = load_architecture_snapshot(control, config)
        executor_arch = load_architecture_snapshot(target, config)
    except ArchitectureError as exc:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="ARCHITECTURE_DRIFT",
            run_id=None,
            run_stage=None,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="recreate/rebase executor from an architecture-compatible base",
            detail=str(exc),
        )
    if (
        control_arch.lock_sha256 != executor_arch.lock_sha256
        or control_arch.source_sha256 != executor_arch.source_sha256
    ):
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="ARCHITECTURE_DRIFT",
            run_id=None,
            run_stage=None,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="do not resume; executor architecture differs from controller authority",
            detail="architecture authority mismatch",
        )

    try:
        run = _latest_run(control, config, target)
    except RunManifestError as exc:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="STALE_EVIDENCE",
            run_id=None,
            run_stage=None,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="inspect malformed run state; resume is disabled",
            detail=str(exc),
        )

    if lease.active:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="LEASE_ACTIVE",
            run_id=run.run_id if run else None,
            run_stage=run.stage if run else None,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="leave the active YouMo operation alone and re-run doctor after it exits",
            detail="executor is currently leased",
        )
    if lease.stale:
        warnings.append(lease.detail)

    if run is None:
        diagnosis = "READY_FOR_BUILD" if state.clean else "DIRTY_UNAUDITED"
        return WorkspaceDiagnosis(
            workspace=str(target),
            state=diagnosis,
            run_id=None,
            run_stage=None,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action=(
                "start a new guarded build"
                if state.clean
                else "inspect the unbound dirty diff; YouMo will not infer its provenance"
            ),
            detail="no run manifest is bound to this workspace",
        )

    if run.branch != state.branch:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="STALE_EVIDENCE",
            run_id=run.run_id,
            run_stage=run.stage,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="do not resume; run branch no longer matches executor branch",
            detail=f"run branch={run.branch!r}; executor branch={state.branch!r}",
        )

    if run.stage == "CHECKPOINTED":
        valid = (
            run.checkpoint_commit is not None
            and state.head == run.checkpoint_commit
            and state.clean
        )
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="CHECKPOINTED_CLEAN" if valid else "STALE_EVIDENCE",
            run_id=run.run_id,
            run_stage=run.stage,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action=(
                "start the next guarded build from this checkpoint"
                if valid
                else "inspect executor HEAD/worktree; checkpoint manifest no longer matches"
            ),
            detail="checkpoint binding verified" if valid else "checkpoint binding mismatch",
        )

    if state.head != run.base_head:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="STALE_EVIDENCE",
            run_id=run.run_id,
            run_stage=run.stage,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="do not resume; executor HEAD moved away from run base",
            detail=f"run base={run.base_head}; executor head={state.head}",
        )
    if run.architecture_lock_sha256 != control_arch.lock_sha256:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="STALE_EVIDENCE",
            run_id=run.run_id,
            run_stage=run.stage,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="do not resume; run architecture lock changed",
            detail="run architecture binding mismatch",
        )

    interrupted = {
        "BUILD_RUNNING": "INTERRUPTED_BUILD",
        "AUDIT_RUNNING": "INTERRUPTED_AUDIT",
        "CHECKPOINT_RUNNING": "INTERRUPTED_CHECKPOINT",
    }
    if run.stage in interrupted:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state=interrupted[run.stage],
            run_id=run.run_id,
            run_stage=run.stage,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="run doctor and inspect interrupted state; automatic repair is intentionally disabled",
            detail="run stage says operation was running but no active lease exists",
        )

    if run.stage in {"BUILD_FAILED", "AUDIT_FAILED"}:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state=run.stage,
            run_id=run.run_id,
            run_stage=run.stage,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="inspect run evidence/error; failed build/audit is never auto-promoted",
            detail=run.last_error or run.stage,
        )

    expected_arch = _expected_architecture(config, control_arch)
    try:
        if run.stage == "READY_FOR_AUDIT":
            build, _ = _evidence_for_run(control, config, run, audit_required=False)
            verify_audit_preconditions(target, run.task, build, expected_arch)
            return WorkspaceDiagnosis(
                workspace=str(target),
                state="READY_FOR_AUDIT",
                run_id=run.run_id,
                run_stage=run.stage,
                branch=state.branch,
                head=state.head,
                clean=state.clean,
                lease=lease.detail,
                ignored_artifacts=ignored,
                warnings=tuple(warnings),
                next_action=f"youmo-resume --run-id {run.run_id}",
                detail="build evidence exactly binds the current dirty diff",
            )
        if run.stage in {"READY_FOR_CHECKPOINT", "CHECKPOINT_FAILED"}:
            build, audit = _evidence_for_run(control, config, run, audit_required=True)
            assert audit is not None
            head, branch, _, diff_sha = verify_audit_preconditions(
                target, run.task, build, expected_arch
            )
            if (
                audit.get("base_head") != head
                or audit.get("branch") != branch
                or audit.get("task_sha256") != build.get("task_sha256")
                or audit.get("diff_sha256") != diff_sha
            ):
                raise DoctorError("audit evidence no longer binds current executor diff")
            return WorkspaceDiagnosis(
                workspace=str(target),
                state=(
                    "READY_FOR_CHECKPOINT"
                    if run.stage == "READY_FOR_CHECKPOINT"
                    else "CHECKPOINT_RETRYABLE"
                ),
                run_id=run.run_id,
                run_stage=run.stage,
                branch=state.branch,
                head=state.head,
                clean=state.clean,
                lease=lease.detail,
                ignored_artifacts=ignored,
                warnings=tuple(warnings),
                next_action=f"youmo-resume --run-id {run.run_id}",
                detail="build and passing audit evidence exactly bind the current diff",
            )
    except (RunManifestError, AuditGuardError, CheckpointError, DoctorError) as exc:
        return WorkspaceDiagnosis(
            workspace=str(target),
            state="STALE_EVIDENCE",
            run_id=run.run_id,
            run_stage=run.stage,
            branch=state.branch,
            head=state.head,
            clean=state.clean,
            lease=lease.detail,
            ignored_artifacts=ignored,
            warnings=tuple(warnings),
            next_action="do not resume; evidence binding failed",
            detail=str(exc),
        )

    return WorkspaceDiagnosis(
        workspace=str(target),
        state="STALE_EVIDENCE",
        run_id=run.run_id,
        run_stage=run.stage,
        branch=state.branch,
        head=state.head,
        clean=state.clean,
        lease=lease.detail,
        ignored_artifacts=ignored,
        warnings=tuple(warnings),
        next_action="inspect unsupported run state",
        detail=f"unhandled run stage: {run.stage}",
    )


def diagnose_all(
    control_root: Path,
    config: ProjectConfig,
    *,
    workspace: Path | None = None,
) -> DoctorReport:
    targets = (workspace.expanduser().resolve(),) if workspace is not None else registered_workspaces(control_root, config)
    return DoctorReport(
        workspaces=tuple(diagnose_workspace(control_root, config, target) for target in targets)
    )
