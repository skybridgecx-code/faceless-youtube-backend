from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .architecture import ArchitectureError, load_architecture_snapshot
from .config import ProjectConfig
from .git_state import RepoState


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GateReport:
    checks: tuple[GateCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def _branch_allowed(branch: str, config: ProjectConfig) -> bool:
    if branch == config.canonical_branch:
        return True
    return any(branch.startswith(prefix) for prefix in config.allowed_branch_prefixes)


def run_preflight_gate(
    repo_root: Path, config: ProjectConfig, state: RepoState
) -> GateReport:
    checks: list[GateCheck] = []

    checks.append(
        GateCheck(
            "repository_identity",
            state.normalized_origin == config.repository,
            f"origin={state.normalized_origin!r}; expected={config.repository!r}",
        )
    )
    checks.append(
        GateCheck(
            "attached_branch",
            not state.detached,
            f"branch={state.branch!r}" if state.branch else "detached HEAD",
        )
    )
    checks.append(
        GateCheck(
            "allowed_branch",
            bool(state.branch) and _branch_allowed(state.branch, config),
            f"branch={state.branch!r}; canonical={config.canonical_branch!r}; "
            f"prefixes={list(config.allowed_branch_prefixes)!r}",
        )
    )
    checks.append(
        GateCheck(
            "clean_worktree",
            state.clean,
            "clean" if state.clean else f"dirty entries={len(state.status_lines)}",
        )
    )
    checks.append(
        GateCheck(
            "empty_index",
            not state.staged_files,
            "no staged files"
            if not state.staged_files
            else f"staged={list(state.staged_files)!r}",
        )
    )

    try:
        snapshot = load_architecture_snapshot(repo_root, config)
    except ArchitectureError as exc:
        checks.append(GateCheck("architecture_authority", False, str(exc)))
        return GateReport(tuple(checks))

    checks.append(
        GateCheck(
            "architecture_authority",
            True,
            f"lock_sha256={snapshot.lock_sha256}",
        )
    )

    current_repo = snapshot.lock.get("current_repository")
    if isinstance(current_repo, dict):
        lock_repo = current_repo.get("repository")
        lock_branch = current_repo.get("branch")
    else:
        lock_repo = None
        lock_branch = None

    checks.append(
        GateCheck(
            "architecture_repository_identity",
            lock_repo == config.repository,
            f"lock repository={lock_repo!r}; expected={config.repository!r}",
        )
    )
    checks.append(
        GateCheck(
            "architecture_canonical_branch",
            lock_branch == config.canonical_branch,
            f"lock branch={lock_branch!r}; expected={config.canonical_branch!r}",
        )
    )

    return GateReport(tuple(checks))
