from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import ProjectConfig
from .doctor import diagnose_workspace
from .run_manifest import (
    EvidenceBinding,
    RunManifest,
    RunManifestError,
    load_run_manifest,
    resolve_evidence,
    run_directory,
)
from .state import state_directory
from .usage import (
    CACHE_RATIO_DEFINITION,
    UsageError,
    UsageRecord,
    UsageTotals,
    UsageWarningThresholds,
    aggregate_usage,
    load_usage_record,
    usage_by_model,
    usage_for_utc_date,
    usage_warnings,
    warning_thresholds_from_env,
)


class OverviewError(RuntimeError):
    """Raised when local YouMo operator state cannot be summarized safely."""


_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_FLOW_STATES = {
    "READY_FOR_AUDIT",
    "READY_FOR_CHECKPOINT",
    "CHECKPOINT_RETRYABLE",
    "CHECKPOINTED_CLEAN",
}


@dataclass(frozen=True)
class RunOverview:
    run_id: str
    workspace: str
    branch: str
    stage: str
    doctor_state: str
    detail: str
    latest_for_workspace: bool
    safe_to_flow: bool
    next_command: str
    workspace_exists: bool
    cleanup_classification: str
    build_usage: UsageRecord | None
    audit_usage: UsageRecord | None
    usage_total: UsageTotals
    usage_warnings: tuple[str, ...]


@dataclass(frozen=True)
class OverviewReport:
    state_root: str
    discovered_runs: int
    workspaces: int
    latest_runs: int
    safe_to_flow: int
    blocked_latest_runs: int
    missing_workspaces: int
    historical_runs: int
    usage_all_time: UsageTotals
    usage_today_utc: UsageTotals
    usage_today_utc_date: str
    usage_by_model: dict[str, UsageTotals]
    usage_warning_thresholds: UsageWarningThresholds
    usage_warnings: tuple[str, ...]
    cache_ratio_definition: str
    items: tuple[RunOverview, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _candidate_run_ids(state_root: Path) -> tuple[str, ...]:
    if not state_root.exists():
        return ()
    if not state_root.is_dir():
        raise OverviewError(f"YouMo state root is not a directory: {state_root}")
    values: set[str] = set()
    try:
        for path in state_root.rglob("*"):
            if path.is_dir() and not path.is_symlink() and _RUN_ID_RE.fullmatch(path.name):
                values.add(path.name)
    except OSError as exc:
        raise OverviewError(f"failed to scan YouMo state root: {state_root}") from exc
    return tuple(sorted(values))


def discover_run_manifests(
    control_root: Path,
    config: ProjectConfig,
) -> tuple[RunManifest, ...]:
    root = state_directory(control_root.resolve(), config.state_dir)
    manifests: list[RunManifest] = []
    for run_id in _candidate_run_ids(root):
        try:
            manifests.append(load_run_manifest(control_root, config, run_id))
        except RunManifestError:
            # 32-hex directories may belong to other state artifacts. Only valid
            # immutable run manifests participate in the operator overview.
            continue
    return tuple(manifests)


def _run_mtime(control_root: Path, config: ProjectConfig, manifest: RunManifest) -> int:
    try:
        return run_directory(control_root, config, manifest.run_id).stat().st_mtime_ns
    except OSError:
        return 0


def _latest_by_workspace(
    control_root: Path,
    config: ProjectConfig,
    manifests: Iterable[RunManifest],
) -> dict[str, str]:
    latest: dict[str, tuple[int, str]] = {}
    for manifest in manifests:
        workspace = str(Path(manifest.workspace).expanduser().resolve())
        candidate = (_run_mtime(control_root, config, manifest), manifest.run_id)
        current = latest.get(workspace)
        if current is None or candidate > current:
            latest[workspace] = candidate
    return {workspace: run_id for workspace, (_, run_id) in latest.items()}


def _next_command(manifest: RunManifest, doctor_state: str) -> str:
    if doctor_state in _SAFE_FLOW_STATES:
        return (
            f"youmo-flow --run-id {manifest.run_id} "
            "--until promotion-check --execute"
        )
    workspace = str(Path(manifest.workspace).expanduser().resolve())
    return f"youmo-doctor --workspace {json.dumps(workspace)}"


def _bound_usage(
    control_root: Path,
    config: ProjectConfig,
    manifest: RunManifest,
    binding: EvidenceBinding | None,
    *,
    expected_stage: str,
) -> UsageRecord | None:
    if binding is None:
        return None
    try:
        path = resolve_evidence(
            binding,
            expected_parent=run_directory(control_root, config, manifest.run_id),
        )
        record = load_usage_record(path)
    except (RunManifestError, UsageError) as exc:
        raise OverviewError(
            f"usage evidence verification failed for run {manifest.run_id} {expected_stage}: {exc}"
        ) from exc
    # Legacy evidence created before telemetry is valid and remains immutable.
    if record is None:
        return None
    if record.run_id != manifest.run_id:
        raise OverviewError(
            f"usage run_id mismatch for {expected_stage}: {record.run_id!r} != {manifest.run_id!r}"
        )
    if record.stage != expected_stage:
        raise OverviewError(
            f"usage stage mismatch for run {manifest.run_id}: {record.stage!r} != {expected_stage!r}"
        )
    return record


def build_overview(
    control_root: Path,
    config: ProjectConfig,
    *,
    include_history: bool = False,
) -> OverviewReport:
    control = control_root.resolve()
    root = state_directory(control, config.state_dir)
    manifests = discover_run_manifests(control, config)
    latest = _latest_by_workspace(control, config, manifests)
    try:
        thresholds = warning_thresholds_from_env()
    except UsageError as exc:
        raise OverviewError(f"usage warning threshold configuration is invalid: {exc}") from exc

    usage_by_run: dict[str, tuple[UsageRecord | None, UsageRecord | None]] = {}
    all_records: list[UsageRecord] = []
    report_warnings: list[str] = []
    for manifest in manifests:
        build_usage = _bound_usage(
            control, config, manifest, manifest.build_evidence, expected_stage="build"
        )
        audit_usage = _bound_usage(
            control, config, manifest, manifest.audit_evidence, expected_stage="audit"
        )
        usage_by_run[manifest.run_id] = (build_usage, audit_usage)
        run_records = tuple(record for record in (build_usage, audit_usage) if record is not None)
        all_records.extend(run_records)
        for warning in usage_warnings(run_records, thresholds):
            report_warnings.append(f"run {manifest.run_id}: {warning}")

    today = datetime.now(timezone.utc).date()
    all_tuple = tuple(all_records)
    all_time = aggregate_usage(all_tuple)
    today_usage = aggregate_usage(usage_for_utc_date(all_tuple, today))
    by_model = usage_by_model(all_tuple)
    items: list[RunOverview] = []

    for manifest in manifests:
        workspace_path = Path(manifest.workspace).expanduser().resolve()
        workspace = str(workspace_path)
        is_latest = latest.get(workspace) == manifest.run_id
        if not include_history and not is_latest:
            continue
        exists = workspace_path.is_dir()
        try:
            diagnosis = diagnose_workspace(control, config, workspace_path)
            doctor_state = diagnosis.state
            detail = diagnosis.detail
        except Exception as exc:
            doctor_state = "DOCTOR_ERROR"
            detail = f"doctor failed: {exc}"
        safe = is_latest and doctor_state in _SAFE_FLOW_STATES
        if not exists:
            cleanup = "MISSING_WORKSPACE_REGISTRATION_REVIEW"
        elif not is_latest:
            cleanup = "HISTORICAL_RUN_KEEP_EVIDENCE"
        else:
            cleanup = "KEEP"
        build_usage, audit_usage = usage_by_run[manifest.run_id]
        run_records = tuple(record for record in (build_usage, audit_usage) if record is not None)
        items.append(
            RunOverview(
                run_id=manifest.run_id,
                workspace=workspace,
                branch=manifest.branch,
                stage=manifest.stage,
                doctor_state=doctor_state,
                detail=detail,
                latest_for_workspace=is_latest,
                safe_to_flow=safe,
                next_command=_next_command(manifest, doctor_state),
                workspace_exists=exists,
                cleanup_classification=cleanup,
                build_usage=build_usage,
                audit_usage=audit_usage,
                usage_total=aggregate_usage(run_records),
                usage_warnings=usage_warnings(run_records, thresholds),
            )
        )

    items.sort(key=lambda item: (not item.latest_for_workspace, item.workspace, item.run_id))
    latest_items = [item for item in items if item.latest_for_workspace]
    return OverviewReport(
        state_root=str(root),
        discovered_runs=len(manifests),
        workspaces=len(latest),
        latest_runs=len(latest_items),
        safe_to_flow=sum(1 for item in latest_items if item.safe_to_flow),
        blocked_latest_runs=sum(1 for item in latest_items if not item.safe_to_flow),
        missing_workspaces=sum(1 for item in latest_items if not item.workspace_exists),
        historical_runs=max(0, len(manifests) - len(latest)),
        usage_all_time=all_time,
        usage_today_utc=today_usage,
        usage_today_utc_date=today.isoformat(),
        usage_by_model=by_model,
        usage_warning_thresholds=thresholds,
        usage_warnings=tuple(report_warnings),
        cache_ratio_definition=CACHE_RATIO_DEFINITION,
        items=tuple(items),
    )
