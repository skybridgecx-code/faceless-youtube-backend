from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .config import ProjectConfig
from .doctor import diagnose_workspace
from .run_manifest import RunManifest, RunManifestError, load_run_manifest, run_directory
from .state import state_directory


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
    items: tuple[RunOverview, ...]

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


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
    return f"youmo-doctor --workspace {json.dumps(str(Path(manifest.workspace).expanduser().resolve()))}"


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
        items=tuple(items),
    )
