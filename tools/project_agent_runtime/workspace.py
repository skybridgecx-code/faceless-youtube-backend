from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import ProjectConfig
from .git_state import GitInspectionError, RepoState, inspect_repo, normalize_github_remote
from .state import state_directory


class WorkspaceError(RuntimeError):
    """Raised when an executor workspace cannot be proven isolated and safe."""


@dataclass(frozen=True)
class WorkspaceMarker:
    schema_version: int
    project_id: str
    repository: str
    base_sha: str
    branch: str
    created_at: str

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> "WorkspaceMarker":
        if data.get("schema_version") != 1:
            raise WorkspaceError("unsupported executor workspace marker schema_version")
        values: dict[str, str] = {}
        for key in ("project_id", "repository", "base_sha", "branch", "created_at"):
            value = data.get(key)
            if not isinstance(value, str) or not value:
                raise WorkspaceError(f"executor workspace marker {key} is invalid")
            values[key] = value
        return cls(schema_version=1, **values)


@dataclass(frozen=True)
class WorkspaceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class WorkspaceReport:
    workspace_root: Path
    state: RepoState | None
    marker: WorkspaceMarker | None
    checks: tuple[WorkspaceCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_git(root: Path, *args: str, timeout: int = 60) -> str:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorkspaceError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout.rstrip("\n")


def _absolute_git_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _marker_path(workspace_root: Path) -> Path:
    return workspace_root / ".git" / "youmo-executor.json"


def _registry_path(control_repo_root: Path, config: ProjectConfig) -> Path:
    return state_directory(control_repo_root, config.state_dir) / "executor-workspaces.json"


def _load_registry(control_repo_root: Path, config: ProjectConfig) -> dict[str, object]:
    path = _registry_path(control_repo_root, config)
    if not path.exists():
        return {"schema_version": 1, "project_id": config.project_id, "workspaces": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"failed to load executor workspace registry: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise WorkspaceError("executor workspace registry schema is invalid")
    if payload.get("project_id") != config.project_id:
        raise WorkspaceError("executor workspace registry project identity mismatch")
    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, dict):
        raise WorkspaceError("executor workspace registry workspaces must be an object")
    return payload


def _save_registry(control_repo_root: Path, config: ProjectConfig, payload: dict[str, object]) -> Path:
    path = _registry_path(control_repo_root, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        temp.write_text(encoded, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise WorkspaceError(f"failed to persist executor workspace registry: {path}") from exc
    return path


def _register_workspace(
    control_repo_root: Path,
    workspace_root: Path,
    config: ProjectConfig,
    marker: WorkspaceMarker,
) -> None:
    payload = _load_registry(control_repo_root, config)
    workspaces = payload["workspaces"]
    assert isinstance(workspaces, dict)
    workspaces[str(workspace_root.resolve())] = asdict(marker)
    _save_registry(control_repo_root, config, payload)


def _unregister_workspace(
    control_repo_root: Path, workspace_root: Path, config: ProjectConfig
) -> None:
    payload = _load_registry(control_repo_root, config)
    workspaces = payload["workspaces"]
    assert isinstance(workspaces, dict)
    workspaces.pop(str(workspace_root.resolve()), None)
    _save_registry(control_repo_root, config, payload)


def _load_marker(workspace_root: Path) -> WorkspaceMarker:
    path = _marker_path(workspace_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceError(f"executor workspace marker is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"executor workspace marker is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceError("executor workspace marker root must be an object")
    return WorkspaceMarker.from_mapping(payload)


def _write_marker(workspace_root: Path, marker: WorkspaceMarker) -> Path:
    path = _marker_path(workspace_root)
    if not (workspace_root / ".git").is_dir():
        raise WorkspaceError("executor workspace must have its own .git directory")
    temp = path.with_suffix(".tmp")
    payload = json.dumps(asdict(marker), indent=2, sort_keys=True) + "\n"
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise WorkspaceError(f"failed to write executor workspace marker: {path}") from exc
    return path


def branch_allowed_for_execution(branch: str, config: ProjectConfig) -> bool:
    return branch != config.canonical_branch and any(
        branch.startswith(prefix) for prefix in config.execution_branch_prefixes
    )


def verify_executor_workspace(
    control_repo_root: Path,
    workspace_root: Path,
    config: ProjectConfig,
    *,
    require_clean: bool = True,
    require_registry: bool = True,
) -> WorkspaceReport:
    control = control_repo_root.resolve()
    workspace = workspace_root.expanduser().resolve()
    checks: list[WorkspaceCheck] = []
    checks.append(
        WorkspaceCheck(
            "distinct_workspace_root",
            workspace != control,
            f"workspace={workspace}; control={control}",
        )
    )
    checks.append(
        WorkspaceCheck(
            "workspace_outside_control_tree",
            not workspace.is_relative_to(control),
            f"workspace={workspace}; control={control}",
        )
    )
    dot_git = workspace / ".git"
    checks.append(
        WorkspaceCheck(
            "private_git_directory",
            dot_git.is_dir() and not dot_git.is_symlink(),
            f".git={dot_git}; is_dir={dot_git.is_dir()}; is_symlink={dot_git.is_symlink()}",
        )
    )
    if not checks[-1].passed:
        return WorkspaceReport(workspace, None, None, tuple(checks))

    try:
        state = inspect_repo(workspace)
        git_dir = _absolute_git_path(workspace, _run_git(workspace, "rev-parse", "--git-dir"))
        common_dir = _absolute_git_path(workspace, _run_git(workspace, "rev-parse", "--git-common-dir"))
    except (GitInspectionError, WorkspaceError) as exc:
        checks.append(WorkspaceCheck("git_inspection", False, str(exc)))
        return WorkspaceReport(workspace, None, None, tuple(checks))

    expected_git_dir = dot_git.resolve()
    checks.append(
        WorkspaceCheck(
            "independent_git_metadata",
            git_dir == expected_git_dir and common_dir == expected_git_dir,
            f"git_dir={git_dir}; common_dir={common_dir}; expected={expected_git_dir}",
        )
    )
    alternates = dot_git / "objects" / "info" / "alternates"
    checks.append(
        WorkspaceCheck(
            "no_object_alternates",
            not alternates.exists(),
            "no alternates file" if not alternates.exists() else f"alternates={alternates}",
        )
    )
    checks.append(
        WorkspaceCheck(
            "repository_identity",
            state.normalized_origin == config.repository,
            f"origin={state.normalized_origin!r}; expected={config.repository!r}",
        )
    )
    checks.append(
        WorkspaceCheck(
            "execution_branch",
            branch_allowed_for_execution(state.branch, config),
            f"branch={state.branch!r}; canonical={config.canonical_branch!r}; execution_prefixes={list(config.execution_branch_prefixes)!r}",
        )
    )
    if require_clean:
        checks.append(
            WorkspaceCheck(
                "clean_worktree",
                state.clean,
                "clean" if state.clean else f"dirty entries={len(state.status_lines)}",
            )
        )
        checks.append(
            WorkspaceCheck(
                "empty_index",
                not state.staged_files,
                "no staged files" if not state.staged_files else f"staged={list(state.staged_files)!r}",
            )
        )

    try:
        marker = _load_marker(workspace)
    except WorkspaceError as exc:
        checks.append(WorkspaceCheck("workspace_marker", False, str(exc)))
        return WorkspaceReport(workspace, state, None, tuple(checks))

    checks.append(WorkspaceCheck("workspace_marker", True, f"base_sha={marker.base_sha}"))
    checks.append(
        WorkspaceCheck(
            "marker_project_identity",
            marker.project_id == config.project_id and marker.repository == config.repository,
            f"marker_project={marker.project_id!r}; marker_repository={marker.repository!r}",
        )
    )
    checks.append(
        WorkspaceCheck(
            "marker_branch_identity",
            marker.branch == state.branch,
            f"marker_branch={marker.branch!r}; branch={state.branch!r}",
        )
    )
    checks.append(
        WorkspaceCheck(
            "base_head_identity",
            marker.base_sha == state.head,
            f"base_sha={marker.base_sha}; head={state.head}",
        )
    )
    if require_registry:
        try:
            registry = _load_registry(control, config)
            workspaces = registry.get("workspaces", {})
            entry = workspaces.get(str(workspace)) if isinstance(workspaces, dict) else None
            registered = isinstance(entry, dict) and all(
                entry.get(key) == value
                for key, value in asdict(marker).items()
            )
            checks.append(
                WorkspaceCheck(
                    "control_registry_binding",
                    registered,
                    "workspace marker matches controller registry"
                    if registered
                    else "workspace is not bound to matching controller registry state",
                )
            )
        except WorkspaceError as exc:
            checks.append(WorkspaceCheck("control_registry_binding", False, str(exc)))
    return WorkspaceReport(workspace, state, marker, tuple(checks))


def initialize_executor_workspace(
    control_repo_root: Path,
    workspace_root: Path,
    config: ProjectConfig,
    *,
    base_sha: str,
    branch: str,
    _clone_source_url: str | None = None,
) -> WorkspaceReport:
    control = control_repo_root.resolve()
    destination = workspace_root.expanduser().resolve()
    normalized_sha = base_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized_sha):
        raise WorkspaceError("base_sha must be an exact 40-character Git commit SHA")
    if not branch_allowed_for_execution(branch, config):
        raise WorkspaceError(
            f"execution branch must be non-canonical and start with one of {list(config.execution_branch_prefixes)!r}"
        )
    try:
        _run_git(control, "check-ref-format", "--branch", branch)
    except WorkspaceError as exc:
        raise WorkspaceError(f"execution branch name is invalid: {branch!r}") from exc
    if destination == control or destination.is_relative_to(control):
        raise WorkspaceError("executor workspace must be outside the control repository tree")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise WorkspaceError(f"executor workspace destination is not empty: {destination}")
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        origin_url = _run_git(control, "remote", "get-url", "origin")
    except WorkspaceError:
        raise
    if normalize_github_remote(origin_url) != config.repository:
        raise WorkspaceError("control repository origin does not match YouMo repository identity")

    try:
        completed = subprocess.run(
            ["git", "clone", "--no-hardlinks", "--no-checkout", _clone_source_url or origin_url, str(destination)],
            cwd=destination.parent,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"executor clone failed: {exc}") from exc
    if completed.returncode != 0:
        shutil.rmtree(destination, ignore_errors=True)
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorkspaceError(f"executor clone failed: {detail}")

    try:
        if _clone_source_url is not None:
            _run_git(destination, "remote", "set-url", "origin", origin_url)
        _run_git(destination, "checkout", "-b", branch, normalized_sha)
        actual_head = _run_git(destination, "rev-parse", "HEAD").strip().lower()
        if actual_head != normalized_sha:
            raise WorkspaceError(
                f"executor clone resolved unexpected HEAD {actual_head}; expected {normalized_sha}"
            )
        marker = WorkspaceMarker(
            schema_version=1,
            project_id=config.project_id,
            repository=config.repository,
            base_sha=normalized_sha,
            branch=branch,
            created_at=_utc_now(),
        )
        _write_marker(destination, marker)
        report = verify_executor_workspace(
            control, destination, config, require_clean=True, require_registry=False
        )
        if not report.passed:
            details = "; ".join(
                f"{check.name}: {check.detail}" for check in report.checks if not check.passed
            )
            raise WorkspaceError(f"executor workspace verification failed: {details}")
        _register_workspace(control, destination, config, marker)
        final_report = verify_executor_workspace(
            control, destination, config, require_clean=True, require_registry=True
        )
        if not final_report.passed:
            details = "; ".join(
                f"{check.name}: {check.detail}" for check in final_report.checks if not check.passed
            )
            raise WorkspaceError(f"executor workspace registry verification failed: {details}")
        return final_report
    except Exception:
        try:
            _unregister_workspace(control, destination, config)
        except WorkspaceError:
            pass
        shutil.rmtree(destination, ignore_errors=True)
        raise
