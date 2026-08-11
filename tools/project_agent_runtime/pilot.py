from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .architecture import ArchitectureError, load_architecture_snapshot
from .codex_transport import inspect_codex_sdk
from .config import ProjectConfig
from .controller import ControllerLayout, default_layout
from .controller_release import inspect_controller_release
from .gates import run_preflight_gate
from .git_state import RepoState
from .hygiene import WorkspaceHygieneError, ignored_untracked_files
from .operation_lease import OperationLeaseError, inspect_operation_lease
from .validation_env import ValidationEnvironmentError, resolve_validation_venv
from .workspace import WorkspaceError, verify_executor_workspace


class PilotError(RuntimeError):
    """Raised when a pilot prerequisite cannot be inspected safely."""


@dataclass(frozen=True)
class PilotCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PilotReadiness:
    workspace: str
    checks: tuple[PilotCheck, ...]
    warnings: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ready": self.ready,
                "workspace": self.workspace,
                "checks": [asdict(check) for check in self.checks],
                "warnings": list(self.warnings),
                "codex_transport_started": False,
                "mutations_performed": False,
            },
            indent=2,
            sort_keys=True,
        ) + "\n"


def _run_git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PilotError(f"git {' '.join(args)} failed to start: {exc}") from exc


def validate_branch_name(root: Path, branch: str) -> str:
    normalized = branch.strip()
    if not normalized:
        raise PilotError("branch name must be non-empty")
    completed = _run_git(root, "check-ref-format", "--branch", normalized)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PilotError(f"invalid Git branch name {normalized!r}: {detail}")
    return normalized


def resolve_remote_branch_sha(
    control_root: Path,
    branch: str,
    *,
    remote: str = "origin",
) -> str:
    root = control_root.resolve()
    normalized = validate_branch_name(root, branch)
    completed = _run_git(
        root,
        "ls-remote",
        "--exit-code",
        remote,
        f"refs/heads/{normalized}",
        timeout=120,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PilotError(
            f"remote branch resolution failed for {remote}/{normalized}: {detail}"
        )
    rows = [line.split() for line in completed.stdout.splitlines() if line.strip()]
    matches = [row for row in rows if len(row) == 2 and row[1] == f"refs/heads/{normalized}"]
    if len(matches) != 1:
        raise PilotError(
            f"expected exactly one remote branch match for {remote}/{normalized}; got {len(matches)}"
        )
    sha = matches[0][0].lower()
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise PilotError(f"remote returned an invalid commit SHA: {sha!r}")
    return sha


def _controller_layout_for_repo(control_root: Path) -> ControllerLayout:
    root = control_root.resolve()
    if root.name == "repo":
        return default_layout(root.parent)
    return default_layout()


def inspect_live_readiness(
    control_root: Path,
    config: ProjectConfig,
    control_state: RepoState,
    workspace: Path,
    *,
    controller_layout: ControllerLayout | None = None,
) -> PilotReadiness:
    control = control_root.resolve()
    target = workspace.expanduser().resolve()
    checks: list[PilotCheck] = []
    warnings: list[str] = []

    preflight = run_preflight_gate(control, config, control_state)
    checks.append(
        PilotCheck(
            "controller_repo_preflight",
            preflight.passed,
            "pass" if preflight.passed else "; ".join(
                f"{item.name}: {item.detail}" for item in preflight.checks if not item.passed
            ),
        )
    )

    release = inspect_controller_release(
        controller_layout or _controller_layout_for_repo(control)
    )
    checks.append(
        PilotCheck(
            "controller_release",
            release.ready,
            release.detail,
        )
    )
    checks.append(
        PilotCheck(
            "controller_push_disabled",
            release.push_disabled,
            "push URL disabled" if release.push_disabled else "controller push URL is not disabled",
        )
    )

    sdk = inspect_codex_sdk(config.codex.sdk_requirement)
    checks.append(PilotCheck("codex_sdk", sdk.ready, sdk.detail))

    try:
        report = verify_executor_workspace(
            control,
            target,
            config,
            require_clean=True,
            require_registry=True,
        )
    except WorkspaceError as exc:
        report = None
        checks.append(PilotCheck("executor_workspace", False, str(exc)))
    else:
        checks.append(
            PilotCheck(
                "executor_workspace",
                report.passed,
                "isolation/identity/cleanliness pass"
                if report.passed
                else "; ".join(
                    f"{item.name}: {item.detail}" for item in report.checks if not item.passed
                ),
            )
        )

    try:
        control_arch = load_architecture_snapshot(control, config)
        executor_arch = load_architecture_snapshot(target, config)
        architecture_match = (
            control_arch.lock_sha256 == executor_arch.lock_sha256
            and control_arch.source_sha256 == executor_arch.source_sha256
        )
        checks.append(
            PilotCheck(
                "architecture_binding",
                architecture_match,
                "controller/executor architecture match"
                if architecture_match
                else "controller/executor architecture authority differs",
            )
        )
    except ArchitectureError as exc:
        checks.append(PilotCheck("architecture_binding", False, str(exc)))

    try:
        ignored = ignored_untracked_files(target)
    except WorkspaceHygieneError as exc:
        checks.append(PilotCheck("ignored_artifacts", False, str(exc)))
    else:
        checks.append(
            PilotCheck(
                "ignored_artifacts",
                not ignored,
                "none" if not ignored else f"present={list(ignored[:20])!r}",
            )
        )

    try:
        lease = inspect_operation_lease(control, config, target)
    except OperationLeaseError as exc:
        checks.append(PilotCheck("executor_lease", False, str(exc)))
    else:
        lease_pass = not lease.active
        checks.append(PilotCheck("executor_lease", lease_pass, lease.detail))
        if lease.stale:
            warnings.append(
                "stale local executor lease is recoverable automatically on the next guarded operation"
            )

    try:
        validation_venv = resolve_validation_venv(control)
    except ValidationEnvironmentError as exc:
        checks.append(PilotCheck("validation_environment", False, str(exc)))
    else:
        if validation_venv is None:
            checks.append(
                PilotCheck(
                    "validation_environment",
                    False,
                    "no trusted validation virtualenv is available",
                )
            )
        else:
            try:
                validation_venv.resolve().relative_to(target)
            except ValueError:
                outside = True
            else:
                outside = False
            checks.append(
                PilotCheck(
                    "validation_environment",
                    outside,
                    f"trusted external venv={validation_venv}"
                    if outside
                    else "validation virtualenv is inside the executor workspace",
                )
            )

    return PilotReadiness(
        workspace=str(target),
        checks=tuple(checks),
        warnings=tuple(warnings),
    )
