from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .audit_engine import AuditGuardError, load_build_evidence, verify_audit_preconditions
from .config import ProjectConfig
from .hygiene import (
    WorkspaceHygieneError,
    cleanup_ignored_untracked,
    require_no_ignored_untracked,
)
from .workspace import (
    WorkspaceError,
    _load_marker,
    _register_workspace,
    _write_marker,
    verify_executor_workspace,
)


class CheckpointError(RuntimeError):
    """Raised when an audited executor diff cannot be checkpointed safely."""


@dataclass(frozen=True)
class CheckpointResult:
    status: str
    commit_sha: str
    parent_sha: str
    branch: str
    changed_files: tuple[str, ...]
    task_sha256: str
    diff_sha256: str
    build_evidence_sha256: str
    audit_evidence_sha256: str
    removed_ignored_artifacts: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _run_git(
    root: Path,
    *args: str,
    timeout: int = 30,
    input_text: str | None = None,
    allow_failure: bool = False,
) -> str:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, env=env, input=input_text,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckpointError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0 and not allow_failure:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CheckpointError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout.rstrip("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_audit_evidence(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CheckpointError(f"audit evidence not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"audit evidence is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise CheckpointError("audit evidence root must be an object")
    required = {
        "status", "passed", "verdict", "base_head", "branch", "task_sha256",
        "diff_sha256", "violations",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise CheckpointError(f"audit evidence missing keys: {', '.join(missing)}")
    if payload.get("status") != "AUDIT_PASS" or payload.get("passed") is not True:
        raise CheckpointError("audit evidence is not AUDIT_PASS")
    if payload.get("verdict") != "PASS":
        raise CheckpointError("audit evidence verdict is not PASS")
    if payload.get("violations") not in ([], ()):
        raise CheckpointError("audit evidence contains violations")
    return payload


def _bind_evidence(build: dict[str, Any], audit: dict[str, Any]) -> None:
    for key in ("base_head", "branch", "task_sha256", "diff_sha256"):
        if audit.get(key) != build.get(key):
            raise CheckpointError(f"build/audit evidence mismatch for {key}")


def _verify_identity(root: Path) -> None:
    name = _run_git(root, "config", "user.name", allow_failure=True).strip()
    email = _run_git(root, "config", "user.email", allow_failure=True).strip()
    if not name or not email:
        raise CheckpointError(
            "Git user.name and user.email must already be configured; checkpoint will not rewrite Git config"
        )


def _commit_message(
    subject: str,
    *,
    task_sha256: str,
    diff_sha256: str,
    build_evidence_sha256: str,
    audit_evidence_sha256: str,
) -> str:
    title = " ".join(subject.strip().split())
    if not title:
        title = "YouMo audited checkpoint"
    title = title[:72]
    return (
        f"{title}\n\n"
        f"YouMo-Task-SHA256: {task_sha256}\n"
        f"YouMo-Diff-SHA256: {diff_sha256}\n"
        f"YouMo-Build-Evidence-SHA256: {build_evidence_sha256}\n"
        f"YouMo-Audit-Evidence-SHA256: {audit_evidence_sha256}\n"
    )


def verify_checkpoint_preconditions(
    *,
    control_root: Path,
    workspace_root: Path,
    config: ProjectConfig,
    task: str,
    build_evidence: dict[str, Any],
    audit_evidence: dict[str, Any],
    expected_architecture: dict[str, str],
) -> tuple[str, str, tuple[str, ...], str, tuple[str, ...]]:
    control = control_root.resolve()
    workspace = workspace_root.resolve()
    report = verify_executor_workspace(
        control, workspace, config, require_clean=False, require_registry=True
    )
    if not report.passed or report.state is None:
        failures = "; ".join(
            f"{check.name}: {check.detail}" for check in report.checks if not check.passed
        )
        raise CheckpointError(f"executor isolation/identity gate failed: {failures}")

    _bind_evidence(build_evidence, audit_evidence)
    try:
        removed = cleanup_ignored_untracked(workspace)
        require_no_ignored_untracked(workspace, context="before checkpoint")
    except WorkspaceHygieneError as exc:
        raise CheckpointError(str(exc)) from exc

    try:
        head, branch, files, diff_sha = verify_audit_preconditions(
            workspace, task, build_evidence, expected_architecture
        )
    except AuditGuardError as exc:
        raise CheckpointError(f"checkpoint evidence preflight failed: {exc}") from exc
    if head != audit_evidence["base_head"] or branch != audit_evidence["branch"]:
        raise CheckpointError("audit evidence no longer binds current executor identity")
    if diff_sha != audit_evidence["diff_sha256"]:
        raise CheckpointError("audit evidence no longer binds current executor diff")
    return head, branch, files, diff_sha, removed


def create_checkpoint(
    *,
    control_root: Path,
    workspace_root: Path,
    config: ProjectConfig,
    task: str,
    build_evidence: dict[str, Any],
    audit_evidence: dict[str, Any],
    expected_architecture: dict[str, str],
    build_evidence_sha256: str,
    audit_evidence_sha256: str,
    subject: str,
) -> CheckpointResult:
    control = control_root.resolve()
    workspace = workspace_root.resolve()
    head, branch, files, diff_sha, removed = verify_checkpoint_preconditions(
        control_root=control,
        workspace_root=workspace,
        config=config,
        task=task,
        build_evidence=build_evidence,
        audit_evidence=audit_evidence,
        expected_architecture=expected_architecture,
    )
    _verify_identity(workspace)
    old_marker = _load_marker(workspace)
    if old_marker.base_sha != head or old_marker.branch != branch:
        raise CheckpointError("executor marker is not bound to pre-checkpoint HEAD/branch")

    ref = f"refs/heads/{branch}"
    ref_advanced = False
    marker_advanced = False
    try:
        _run_git(workspace, "add", "-A", "--", *files)
        staged = tuple(
            sorted(
                line for line in _run_git(
                    workspace, "diff", "--cached", "--name-only", "--no-renames", "--"
                ).splitlines() if line
            )
        )
        if staged != files:
            raise CheckpointError(
                f"staged file set mismatch: staged={list(staged)!r}; expected={list(files)!r}"
            )
        unstaged = tuple(
            line for line in _run_git(workspace, "diff", "--name-only", "--no-renames", "--").splitlines() if line
        )
        untracked = tuple(
            line for line in _run_git(workspace, "ls-files", "--others", "--exclude-standard", "--").splitlines() if line
        )
        if unstaged or untracked:
            raise CheckpointError(
                f"workspace is not fully represented by the staged checkpoint: unstaged={list(unstaged)!r}; untracked={list(untracked)!r}"
            )
        _run_git(workspace, "diff", "--cached", "--check")
        tree_sha = _run_git(workspace, "write-tree").strip()
        message = _commit_message(
            subject,
            task_sha256=build_evidence["task_sha256"],
            diff_sha256=diff_sha,
            build_evidence_sha256=build_evidence_sha256,
            audit_evidence_sha256=audit_evidence_sha256,
        )
        commit_sha = _run_git(
            workspace, "commit-tree", tree_sha, "-p", head, input_text=message
        ).strip()
        parent = _run_git(workspace, "rev-parse", f"{commit_sha}^").strip()
        if parent != head:
            raise CheckpointError(f"checkpoint commit parent mismatch: {parent} != {head}")
        committed_files = tuple(
            sorted(
                line for line in _run_git(
                    workspace, "diff-tree", "--no-commit-id", "--name-only", "-r",
                    "--no-renames", head, commit_sha, "--"
                ).splitlines() if line
            )
        )
        if committed_files != files:
            raise CheckpointError(
                f"checkpoint commit file set mismatch: committed={list(committed_files)!r}; expected={list(files)!r}"
            )

        _run_git(workspace, "update-ref", ref, commit_sha, head)
        ref_advanced = True
        new_marker = replace(old_marker, base_sha=commit_sha)
        _write_marker(workspace, new_marker)
        marker_advanced = True
        _register_workspace(control, workspace, config, new_marker)

        post = verify_executor_workspace(
            control, workspace, config, require_clean=True, require_registry=True
        )
        if not post.passed or post.state is None:
            failures = "; ".join(
                f"{check.name}: {check.detail}" for check in post.checks if not check.passed
            )
            raise CheckpointError(f"post-checkpoint executor verification failed: {failures}")
        require_no_ignored_untracked(workspace, context="after checkpoint")
        if post.state.head != commit_sha or post.state.branch != branch:
            raise CheckpointError("post-checkpoint HEAD/branch mismatch")

        return CheckpointResult(
            status="CHECKPOINTED",
            commit_sha=commit_sha,
            parent_sha=head,
            branch=branch,
            changed_files=files,
            task_sha256=build_evidence["task_sha256"],
            diff_sha256=diff_sha,
            build_evidence_sha256=build_evidence_sha256,
            audit_evidence_sha256=audit_evidence_sha256,
            removed_ignored_artifacts=removed,
        )
    except Exception as exc:
        try:
            if marker_advanced:
                _write_marker(workspace, old_marker)
                _register_workspace(control, workspace, config, old_marker)
            if ref_advanced:
                current = _run_git(workspace, "rev-parse", ref).strip()
                _run_git(workspace, "update-ref", ref, head, current)
            _run_git(workspace, "reset", "--mixed", head)
        except Exception:
            pass
        if isinstance(exc, CheckpointError):
            raise
        if isinstance(exc, (WorkspaceError, WorkspaceHygieneError)):
            raise CheckpointError(str(exc)) from exc
        raise CheckpointError(f"checkpoint transaction failed: {exc}") from exc
