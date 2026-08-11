from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .architecture import ArchitectureError, load_architecture_snapshot
from .config import ProjectConfig
from .git_state import GitInspectionError, inspect_repo
from .hygiene import WorkspaceHygieneError, require_no_ignored_untracked
from .operation_lease import OperationLeaseError, operation_lease
from .run_manifest import (
    RunManifest,
    RunManifestError,
    load_run_manifest,
    resolve_evidence,
    run_directory,
)
from .workspace import WorkspaceError, branch_allowed_for_execution, verify_executor_workspace


class PublishError(RuntimeError):
    """Raised when an audited checkpoint cannot be published safely."""


@dataclass(frozen=True)
class PublishPlan:
    run_id: str
    workspace: str
    branch: str
    commit_sha: str
    repository: str
    remote_before: str | None
    mode: str

    @property
    def already_published(self) -> bool:
        return self.mode == "ALREADY_PUBLISHED"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class PublishResult:
    status: str
    run_id: str
    workspace: str
    branch: str
    commit_sha: str
    repository: str
    remote_before: str | None
    remote_after: str
    mode: str
    evidence_path: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _git_result(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError(f"git {' '.join(args)} failed to start: {exc}") from exc


def _git(root: Path, *args: str, timeout: int = 120) -> str:
    completed = _git_result(root, *args, timeout=timeout)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PublishError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout.rstrip("\n")


def _validate_branch(root: Path, branch: str) -> str:
    normalized = branch.strip()
    if not normalized:
        raise PublishError("publish branch must be non-empty")
    completed = _git_result(root, "check-ref-format", "--branch", normalized)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PublishError(f"invalid publish branch {normalized!r}: {detail}")
    return normalized


def resolve_remote_branch_sha(root: Path, branch: str) -> str | None:
    normalized = _validate_branch(root, branch)
    completed = _git_result(root, "ls-remote", "origin", f"refs/heads/{normalized}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PublishError(f"remote branch inspection failed: {detail}")
    rows = [line.split() for line in completed.stdout.splitlines() if line.strip()]
    matches = [row for row in rows if len(row) == 2 and row[1] == f"refs/heads/{normalized}"]
    if not matches:
        return None
    if len(matches) != 1:
        raise PublishError(
            f"expected exactly one remote branch match for origin/{normalized}; got {len(matches)}"
        )
    sha = matches[0][0].lower()
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise PublishError(f"remote returned an invalid commit SHA: {sha!r}")
    return sha


def _load_checkpoint_payload(
    control_root: Path,
    config: ProjectConfig,
    manifest: RunManifest,
) -> dict[str, Any]:
    if manifest.stage != "CHECKPOINTED":
        raise PublishError(f"run is not checkpointed: stage={manifest.stage}")
    if manifest.checkpoint_commit is None or manifest.checkpoint_evidence is None:
        raise PublishError("checkpointed run is missing checkpoint commit/evidence")
    if manifest.build_evidence is None or manifest.audit_evidence is None:
        raise PublishError("checkpointed run is missing build/audit evidence bindings")

    parent = run_directory(control_root, config, manifest.run_id)
    try:
        build_path = resolve_evidence(manifest.build_evidence, expected_parent=parent)
        audit_path = resolve_evidence(manifest.audit_evidence, expected_parent=parent)
        checkpoint_path = resolve_evidence(manifest.checkpoint_evidence, expected_parent=parent)
    except RunManifestError as exc:
        raise PublishError(str(exc)) from exc

    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"checkpoint evidence is unreadable: {checkpoint_path}") from exc
    if not isinstance(payload, dict):
        raise PublishError("checkpoint evidence root must be an object")
    if payload.get("status") != "CHECKPOINTED":
        raise PublishError("checkpoint evidence status is not CHECKPOINTED")
    expected = {
        "commit_sha": manifest.checkpoint_commit,
        "branch": manifest.branch,
        "task_sha256": manifest.task_sha256,
        "build_evidence_sha256": manifest.build_evidence.sha256,
        "audit_evidence_sha256": manifest.audit_evidence.sha256,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise PublishError(
                f"checkpoint evidence mismatch for {key}: {payload.get(key)!r} != {value!r}"
            )
    # Resolve calls above prove the immutable files still hash to their manifest bindings.
    if not build_path.is_file() or not audit_path.is_file():
        raise PublishError("build/audit evidence disappeared during checkpoint verification")
    return payload


def _commit_known(root: Path, sha: str) -> bool:
    return _git_result(root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return _git_result(root, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _fetch_remote_branch_objects(root: Path, branch: str, expected_sha: str) -> None:
    _git(
        root,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "origin",
        f"refs/heads/{branch}",
        timeout=300,
    )
    observed = resolve_remote_branch_sha(root, branch)
    if observed != expected_sha:
        raise PublishError(
            "remote branch changed while ancestry was being proven; rerun publication preflight"
        )


def _verify_remote_fast_forward(
    root: Path,
    *,
    branch: str,
    remote_sha: str,
    commit_sha: str,
    checkpoint_parent: str | None,
    allow_fetch: bool,
) -> None:
    if remote_sha == commit_sha:
        return
    if checkpoint_parent == remote_sha:
        return
    if not _commit_known(root, remote_sha):
        if not allow_fetch:
            raise PublishError(
                "remote branch commit is not present locally; execute mode must fetch it before ancestry can be proven"
            )
        _fetch_remote_branch_objects(root, branch, remote_sha)
    if not _is_ancestor(root, remote_sha, commit_sha):
        raise PublishError(
            f"remote branch is not an ancestor of checkpoint commit: remote={remote_sha}; checkpoint={commit_sha}"
        )


def prepare_publish(
    control_root: Path,
    config: ProjectConfig,
    run_id: str,
    *,
    allow_fetch: bool = False,
) -> PublishPlan:
    control = control_root.resolve()
    try:
        manifest = load_run_manifest(control, config, run_id)
    except RunManifestError as exc:
        raise PublishError(str(exc)) from exc
    workspace = Path(manifest.workspace).expanduser().resolve()
    checkpoint = _load_checkpoint_payload(control, config, manifest)

    try:
        report = verify_executor_workspace(
            control, workspace, config, require_clean=True, require_registry=True
        )
    except WorkspaceError as exc:
        raise PublishError(str(exc)) from exc
    if not report.passed or report.state is None or report.marker is None:
        failures = "; ".join(
            f"{check.name}: {check.detail}" for check in report.checks if not check.passed
        )
        raise PublishError(f"executor verification failed: {failures}")
    state = report.state
    marker = report.marker
    commit_sha = manifest.checkpoint_commit
    assert commit_sha is not None

    if state.branch != manifest.branch or marker.branch != manifest.branch:
        raise PublishError("executor branch no longer matches checkpointed run")
    if state.head != commit_sha or marker.base_sha != commit_sha:
        raise PublishError("executor HEAD/marker no longer match checkpoint commit")
    if state.normalized_origin != config.repository:
        raise PublishError(
            f"executor origin mismatch: {state.normalized_origin!r} != {config.repository!r}"
        )
    if manifest.branch == config.canonical_branch:
        raise PublishError("publication to the canonical branch is forbidden")
    if not branch_allowed_for_execution(manifest.branch, config):
        raise PublishError(f"branch is not allowed for executor publication: {manifest.branch!r}")

    try:
        require_no_ignored_untracked(workspace, context="before publish")
    except WorkspaceHygieneError as exc:
        raise PublishError(str(exc)) from exc

    try:
        control_arch = load_architecture_snapshot(control, config)
        executor_arch = load_architecture_snapshot(workspace, config)
    except ArchitectureError as exc:
        raise PublishError(str(exc)) from exc
    if control_arch.lock_sha256 != manifest.architecture_lock_sha256:
        raise PublishError("run architecture lock no longer matches controller authority")
    if (
        executor_arch.lock_sha256 != control_arch.lock_sha256
        or executor_arch.source_sha256 != control_arch.source_sha256
    ):
        raise PublishError("executor architecture authority differs from controller")

    remote_before = resolve_remote_branch_sha(workspace, manifest.branch)
    if remote_before is None:
        mode = "CREATE_REMOTE_BRANCH"
    elif remote_before == commit_sha:
        mode = "ALREADY_PUBLISHED"
    else:
        _verify_remote_fast_forward(
            workspace,
            branch=manifest.branch,
            remote_sha=remote_before,
            commit_sha=commit_sha,
            checkpoint_parent=checkpoint.get("parent_sha") if isinstance(checkpoint.get("parent_sha"), str) else None,
            allow_fetch=allow_fetch,
        )
        mode = "FAST_FORWARD_REMOTE_BRANCH"

    return PublishPlan(
        run_id=manifest.run_id,
        workspace=str(workspace),
        branch=manifest.branch,
        commit_sha=commit_sha,
        repository=config.repository,
        remote_before=remote_before,
        mode=mode,
    )


def _publish_evidence_path(control_root: Path, config: ProjectConfig, run_id: str) -> Path:
    return run_directory(control_root, config, run_id) / "publish.json"


def _read_existing_publish(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise PublishError(f"publish evidence is not a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"publish evidence is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PublishError("publish evidence schema is invalid")
    return payload


def _persist_publish_evidence(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise PublishError(f"refusing to overwrite immutable publish evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        if path.exists():
            raise PublishError(f"refusing to overwrite immutable publish evidence: {path}")
        os.replace(temp, path)
    except OSError as exc:
        raise PublishError(f"failed to persist publish evidence: {path}") from exc
    finally:
        temp.unlink(missing_ok=True)


def execute_publish(
    control_root: Path,
    config: ProjectConfig,
    run_id: str,
) -> PublishResult:
    control = control_root.resolve()
    try:
        manifest = load_run_manifest(control, config, run_id)
    except RunManifestError as exc:
        raise PublishError(str(exc)) from exc
    workspace = Path(manifest.workspace).expanduser().resolve()
    evidence_path = _publish_evidence_path(control, config, run_id)

    try:
        with operation_lease(control, config, workspace, "publish"):
            plan = prepare_publish(control, config, run_id, allow_fetch=True)
            existing = _read_existing_publish(evidence_path)
            if existing is not None:
                remote = resolve_remote_branch_sha(workspace, plan.branch)
                expected = {
                    "run_id": plan.run_id,
                    "branch": plan.branch,
                    "commit_sha": plan.commit_sha,
                    "repository": plan.repository,
                    "remote_after": plan.commit_sha,
                }
                for key, value in expected.items():
                    if existing.get(key) != value:
                        raise PublishError(f"existing publish evidence mismatch for {key}")
                if remote != plan.commit_sha:
                    raise PublishError(
                        "publish evidence exists but remote branch no longer equals checkpoint commit"
                    )
                return PublishResult(
                    status="ALREADY_PUBLISHED",
                    run_id=plan.run_id,
                    workspace=plan.workspace,
                    branch=plan.branch,
                    commit_sha=plan.commit_sha,
                    repository=plan.repository,
                    remote_before=remote,
                    remote_after=remote,
                    mode="ALREADY_PUBLISHED",
                    evidence_path=str(evidence_path),
                )

            if not plan.already_published:
                ref = f"refs/heads/{plan.branch}"
                if plan.remote_before is None:
                    lease = f"--force-with-lease={ref}:"
                else:
                    lease = f"--force-with-lease={ref}:{plan.remote_before}"
                _git(
                    workspace,
                    "push",
                    "--porcelain",
                    lease,
                    "origin",
                    f"{plan.commit_sha}:{ref}",
                    timeout=300,
                )

            remote_after = resolve_remote_branch_sha(workspace, plan.branch)
            if remote_after != plan.commit_sha:
                raise PublishError(
                    f"post-publish remote verification failed: {remote_after!r} != {plan.commit_sha!r}"
                )

            payload = {
                "schema_version": 1,
                "status": "PUBLISHED",
                "run_id": plan.run_id,
                "workspace": plan.workspace,
                "branch": plan.branch,
                "commit_sha": plan.commit_sha,
                "repository": plan.repository,
                "remote_before": plan.remote_before,
                "remote_after": remote_after,
                "mode": plan.mode,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "canonical_branch_mutated": False,
                "merge_started": False,
            }
            _persist_publish_evidence(evidence_path, payload)
            return PublishResult(
                status="PUBLISHED" if not plan.already_published else "RECORDED_EXISTING_REMOTE",
                run_id=plan.run_id,
                workspace=plan.workspace,
                branch=plan.branch,
                commit_sha=plan.commit_sha,
                repository=plan.repository,
                remote_before=plan.remote_before,
                remote_after=remote_after,
                mode=plan.mode,
                evidence_path=str(evidence_path),
            )
    except (OperationLeaseError, GitInspectionError) as exc:
        raise PublishError(str(exc)) from exc
