from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .operation_lease import OperationLeaseError, operation_lease
from .publish import (
    PublishError,
    _commit_known,
    _git,
    _is_ancestor,
    _validate_remote_target,
    prepare_publish,
    resolve_remote_branch_sha,
)
from .run_manifest import RunManifestError, load_run_manifest, run_directory


class PromotionCheckError(RuntimeError):
    """Raised when promotion readiness cannot be proven safely."""


@dataclass(frozen=True)
class PromotionReadiness:
    run_id: str
    workspace: str
    repository: str
    candidate_branch: str
    candidate_sha: str
    canonical_branch: str
    canonical_sha: str
    classification: str
    fast_forward_possible: bool
    promotion_needed: bool
    candidate_in_canonical: bool
    remote_stable: bool

    @property
    def blocked(self) -> bool:
        return self.classification == "BLOCKED_DIVERGED"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _load_publish_evidence(
    control_root: Path,
    config: ProjectConfig,
    run_id: str,
) -> dict[str, Any]:
    try:
        manifest = load_run_manifest(control_root, config, run_id)
    except RunManifestError as exc:
        raise PromotionCheckError(str(exc)) from exc
    if manifest.stage != "CHECKPOINTED" or manifest.checkpoint_commit is None:
        raise PromotionCheckError(
            f"run must remain CHECKPOINTED before promotion review: stage={manifest.stage}"
        )

    path = run_directory(control_root, config, run_id) / "publish.json"
    if not path.is_file() or path.is_symlink():
        raise PromotionCheckError(f"immutable publish evidence is missing or invalid: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionCheckError(f"publish evidence is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PromotionCheckError("publish evidence schema is invalid")

    expected = {
        "status": "PUBLISHED",
        "run_id": manifest.run_id,
        "workspace": manifest.workspace,
        "branch": manifest.branch,
        "commit_sha": manifest.checkpoint_commit,
        "repository": config.repository,
        "remote_after": manifest.checkpoint_commit,
        "canonical_branch_mutated": False,
        "merge_started": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise PromotionCheckError(
                f"publish evidence mismatch for {key}: {payload.get(key)!r} != {value!r}"
            )
    return payload


def _fetch_exact_branch_commit(
    workspace: Path,
    branch: str,
    expected_sha: str,
    *,
    remote_target: str,
) -> None:
    target = _validate_remote_target(remote_target)
    _git(
        workspace,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        target,
        f"refs/heads/{branch}",
        timeout=300,
    )
    observed = resolve_remote_branch_sha(
        workspace,
        branch,
        remote_target=target,
    )
    if observed != expected_sha:
        raise PromotionCheckError(
            f"remote {branch} changed while its ancestry object was fetched: "
            f"{observed!r} != {expected_sha!r}"
        )
    if not _commit_known(workspace, expected_sha):
        raise PromotionCheckError(
            f"remote {branch} commit could not be materialized locally: {expected_sha}"
        )


def check_promotion_readiness(
    control_root: Path,
    config: ProjectConfig,
    run_id: str,
    *,
    _remote_target: str = "origin",
) -> PromotionReadiness:
    control = control_root.resolve()
    target = _validate_remote_target(_remote_target)
    try:
        manifest = load_run_manifest(control, config, run_id)
    except RunManifestError as exc:
        raise PromotionCheckError(str(exc)) from exc
    workspace = Path(manifest.workspace).expanduser().resolve()

    try:
        with operation_lease(control, config, workspace, "promote-check"):
            _load_publish_evidence(control, config, run_id)
            try:
                publish_plan = prepare_publish(
                    control,
                    config,
                    run_id,
                    allow_fetch=True,
                    _remote_target=target,
                )
            except PublishError as exc:
                raise PromotionCheckError(f"published candidate is no longer valid: {exc}") from exc
            if publish_plan.mode != "ALREADY_PUBLISHED":
                raise PromotionCheckError(
                    "published candidate remote no longer equals its checkpoint commit"
                )

            candidate_sha = publish_plan.commit_sha
            candidate_branch = publish_plan.branch
            candidate_before = resolve_remote_branch_sha(
                workspace,
                candidate_branch,
                remote_target=target,
            )
            if candidate_before != candidate_sha:
                raise PromotionCheckError(
                    "candidate remote changed before promotion ancestry proof"
                )

            canonical_before = resolve_remote_branch_sha(
                workspace,
                config.canonical_branch,
                remote_target=target,
            )
            if canonical_before is None:
                raise PromotionCheckError(
                    f"canonical remote branch is missing: {config.canonical_branch}"
                )

            if canonical_before == candidate_sha:
                classification = "ALREADY_PROMOTED"
                fast_forward_possible = False
                promotion_needed = False
                candidate_in_canonical = True
            else:
                if not _commit_known(workspace, canonical_before):
                    _fetch_exact_branch_commit(
                        workspace,
                        config.canonical_branch,
                        canonical_before,
                        remote_target=target,
                    )
                if _is_ancestor(workspace, canonical_before, candidate_sha):
                    classification = "READY_FAST_FORWARD"
                    fast_forward_possible = True
                    promotion_needed = True
                    candidate_in_canonical = False
                elif _is_ancestor(workspace, candidate_sha, canonical_before):
                    classification = "ALREADY_INCLUDED"
                    fast_forward_possible = False
                    promotion_needed = False
                    candidate_in_canonical = True
                else:
                    classification = "BLOCKED_DIVERGED"
                    fast_forward_possible = False
                    promotion_needed = False
                    candidate_in_canonical = False

            candidate_after = resolve_remote_branch_sha(
                workspace,
                candidate_branch,
                remote_target=target,
            )
            canonical_after = resolve_remote_branch_sha(
                workspace,
                config.canonical_branch,
                remote_target=target,
            )
            if candidate_after != candidate_before or canonical_after != canonical_before:
                raise PromotionCheckError(
                    "remote branch state changed during promotion readiness check; rerun"
                )

            return PromotionReadiness(
                run_id=manifest.run_id,
                workspace=str(workspace),
                repository=config.repository,
                candidate_branch=candidate_branch,
                candidate_sha=candidate_sha,
                canonical_branch=config.canonical_branch,
                canonical_sha=canonical_before,
                classification=classification,
                fast_forward_possible=fast_forward_possible,
                promotion_needed=promotion_needed,
                candidate_in_canonical=candidate_in_canonical,
                remote_stable=True,
            )
    except OperationLeaseError as exc:
        raise PromotionCheckError(str(exc)) from exc
