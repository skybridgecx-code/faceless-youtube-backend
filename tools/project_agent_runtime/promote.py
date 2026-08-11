from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .config import ProjectConfig
from .hygiene import WorkspaceHygieneError, cleanup_ignored_untracked
from .operation_lease import OperationLeaseError, operation_lease
from .promotion import (
    PromotionCheckError,
    PromotionReadiness,
    _check_promotion_readiness_unlocked,
    check_promotion_readiness,
)
from .publish import (
    PublishError,
    _git,
    _git_result,
    _validate_remote_target,
    resolve_remote_branch_sha,
)
from .run_manifest import RunManifestError, load_run_manifest, run_directory
from .state import state_directory
from .validation_env import (
    ValidationEnvironmentError,
    bound_workspace_validation_venv,
    resolve_validation_venv,
)


class PromotionTransactionError(RuntimeError):
    """Raised when a canonical fast-forward promotion cannot be completed safely."""


@dataclass(frozen=True)
class PromotionValidation:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class PromotionPlan:
    run_id: str
    workspace: str
    repository: str
    candidate_branch: str
    candidate_sha: str
    canonical_branch: str
    canonical_sha: str
    classification: str
    validation_venv: str | None

    @property
    def executable(self) -> bool:
        return self.classification == "READY_FAST_FORWARD"

    def to_json(self) -> str:
        payload = asdict(self)
        payload["executable"] = self.executable
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class PromotionResult:
    status: str
    run_id: str
    candidate_branch: str
    candidate_sha: str
    canonical_branch: str
    canonical_before: str
    canonical_after: str
    validations: tuple[PromotionValidation, ...]
    removed_ignored_artifacts: tuple[str, ...]
    intent_path: str
    evidence_path: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


DEFAULT_PROMOTION_VALIDATIONS: tuple[tuple[str, ...], ...] = (
    (".venv/bin/python", "-m", "pytest", "-q"),
    ("node", "--check", "app/static/app.js"),
)


def _run_validations(
    root: Path,
    commands: Sequence[Sequence[str]],
) -> tuple[PromotionValidation, ...]:
    results: list[PromotionValidation] = []
    for raw in commands:
        argv = tuple(str(value) for value in raw)
        if not argv:
            raise PromotionTransactionError("promotion validation command may not be empty")
        try:
            completed = subprocess.run(
                list(argv),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append(PromotionValidation(argv, 125, "", str(exc)))
            break
        result = PromotionValidation(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout[-16000:],
            stderr=completed.stderr[-16000:],
        )
        results.append(result)
        if not result.passed:
            break
    return tuple(results)


def _atomic_create_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise PromotionTransactionError(f"refusing to overwrite immutable promotion journal: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        if path.exists():
            raise PromotionTransactionError(
                f"refusing to overwrite immutable promotion journal: {path}"
            )
        os.replace(temp, path)
    except OSError as exc:
        raise PromotionTransactionError(f"failed to persist promotion journal: {path}") from exc
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise PromotionTransactionError(f"promotion journal is not a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionTransactionError(f"promotion journal is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise PromotionTransactionError(f"promotion journal root must be an object: {path}")
    return payload


def _journal_paths(control_root: Path, config: ProjectConfig, run_id: str) -> tuple[Path, Path]:
    root = run_directory(control_root, config, run_id)
    return root / "promotion-intent.json", root / "promotion.json"


def _remote_source(workspace: Path, remote_target: str) -> str:
    target = _validate_remote_target(remote_target)
    if target != "origin":
        return target
    completed = _git_result(workspace, "remote", "get-url", "origin")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PromotionTransactionError(f"failed to resolve executor origin URL: {detail}")
    value = completed.stdout.strip()
    if not value:
        raise PromotionTransactionError("executor origin URL is empty")
    return value


def _require_external_work_root(control: Path, workspace: Path, work_root: Path) -> None:
    resolved = work_root.resolve()
    for forbidden, label in ((control.resolve(), "controller"), (workspace.resolve(), "executor")):
        try:
            resolved.relative_to(forbidden)
        except ValueError:
            pass
        else:
            raise PromotionTransactionError(
                f"promotion work root may not live inside {label} checkout: {resolved}"
            )


def _verify_clone_remote_refs(
    clone: Path,
    readiness: PromotionReadiness,
) -> None:
    candidate = resolve_remote_branch_sha(
        clone, readiness.candidate_branch, remote_target="origin"
    )
    canonical = resolve_remote_branch_sha(
        clone, readiness.canonical_branch, remote_target="origin"
    )
    if candidate != readiness.candidate_sha:
        raise PromotionTransactionError(
            f"promotion clone candidate remote mismatch: {candidate!r} != {readiness.candidate_sha!r}"
        )
    if canonical != readiness.canonical_sha:
        raise PromotionTransactionError(
            f"promotion clone canonical remote mismatch: {canonical!r} != {readiness.canonical_sha!r}"
        )


def prepare_promotion(
    control_root: Path,
    config: ProjectConfig,
    run_id: str,
    *,
    _remote_target: str = "origin",
) -> PromotionPlan:
    control = control_root.resolve()
    try:
        readiness = check_promotion_readiness(
            control, config, run_id, _remote_target=_remote_target
        )
        validation_venv = resolve_validation_venv(control)
    except (PromotionCheckError, ValidationEnvironmentError) as exc:
        raise PromotionTransactionError(str(exc)) from exc
    return PromotionPlan(
        run_id=readiness.run_id,
        workspace=readiness.workspace,
        repository=readiness.repository,
        candidate_branch=readiness.candidate_branch,
        candidate_sha=readiness.candidate_sha,
        canonical_branch=readiness.canonical_branch,
        canonical_sha=readiness.canonical_sha,
        classification=readiness.classification,
        validation_venv=str(validation_venv) if validation_venv else None,
    )


def _intent_payload(
    readiness: PromotionReadiness,
    validations: tuple[PromotionValidation, ...],
    removed: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "PREPARED_FOR_CANONICAL_PUSH",
        "run_id": readiness.run_id,
        "repository": readiness.repository,
        "candidate_branch": readiness.candidate_branch,
        "candidate_sha": readiness.candidate_sha,
        "canonical_branch": readiness.canonical_branch,
        "canonical_before": readiness.canonical_sha,
        "merge_mode": "ff-only",
        "merge_commit_created": False,
        "validation_passed": all(item.passed for item in validations),
        "validations": [asdict(item) for item in validations],
        "removed_ignored_artifacts": list(removed),
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }


def _verify_existing_intent(intent: dict[str, Any], readiness: PromotionReadiness) -> None:
    expected = {
        "schema_version": 1,
        "status": "PREPARED_FOR_CANONICAL_PUSH",
        "run_id": readiness.run_id,
        "repository": readiness.repository,
        "candidate_branch": readiness.candidate_branch,
        "candidate_sha": readiness.candidate_sha,
        "canonical_branch": readiness.canonical_branch,
        "canonical_before": readiness.canonical_sha,
        "merge_mode": "ff-only",
        "merge_commit_created": False,
        "validation_passed": True,
    }
    for key, value in expected.items():
        if intent.get(key) != value:
            raise PromotionTransactionError(
                f"existing promotion intent mismatch for {key}: {intent.get(key)!r} != {value!r}"
            )


def _final_payload(intent: dict[str, Any], canonical_after: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "PROMOTED",
        "run_id": intent["run_id"],
        "repository": intent["repository"],
        "candidate_branch": intent["candidate_branch"],
        "candidate_sha": intent["candidate_sha"],
        "canonical_branch": intent["canonical_branch"],
        "canonical_before": intent["canonical_before"],
        "canonical_after": canonical_after,
        "merge_mode": "ff-only",
        "merge_commit_created": False,
        "validation_passed": True,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }


def execute_promotion(
    control_root: Path,
    config: ProjectConfig,
    run_id: str,
    *,
    _remote_target: str = "origin",
    _validation_venv: Path | None = None,
    _validation_commands: Sequence[Sequence[str]] = DEFAULT_PROMOTION_VALIDATIONS,
) -> PromotionResult:
    control = control_root.resolve()
    target = _validate_remote_target(_remote_target)
    try:
        manifest = load_run_manifest(control, config, run_id)
    except RunManifestError as exc:
        raise PromotionTransactionError(str(exc)) from exc
    workspace = Path(manifest.workspace).expanduser().resolve()
    intent_path, evidence_path = _journal_paths(control, config, run_id)

    try:
        with operation_lease(control, config, workspace, "promote"):
            readiness = _check_promotion_readiness_unlocked(
                control, config, run_id, remote_target=target
            )

            existing_evidence = _read_json(evidence_path)
            if existing_evidence is not None:
                if existing_evidence.get("status") != "PROMOTED":
                    raise PromotionTransactionError("existing promotion evidence status is invalid")
                if existing_evidence.get("candidate_sha") != readiness.candidate_sha:
                    raise PromotionTransactionError("existing promotion evidence candidate mismatch")
                canonical_now = resolve_remote_branch_sha(
                    workspace, config.canonical_branch, remote_target=target
                )
                if canonical_now != readiness.candidate_sha:
                    raise PromotionTransactionError(
                        "promotion evidence exists but canonical remote no longer equals candidate"
                    )
                intent = _read_json(intent_path) or {}
                validations = tuple(
                    PromotionValidation(
                        tuple(item.get("argv", ())),
                        int(item.get("returncode", 1)),
                        str(item.get("stdout", "")),
                        str(item.get("stderr", "")),
                    )
                    for item in intent.get("validations", [])
                    if isinstance(item, dict)
                )
                return PromotionResult(
                    status="ALREADY_PROMOTED",
                    run_id=readiness.run_id,
                    candidate_branch=readiness.candidate_branch,
                    candidate_sha=readiness.candidate_sha,
                    canonical_branch=readiness.canonical_branch,
                    canonical_before=str(existing_evidence.get("canonical_before", readiness.canonical_sha)),
                    canonical_after=canonical_now,
                    validations=validations,
                    removed_ignored_artifacts=tuple(intent.get("removed_ignored_artifacts", ())),
                    intent_path=str(intent_path),
                    evidence_path=str(evidence_path),
                )

            existing_intent = _read_json(intent_path)
            if readiness.classification == "ALREADY_PROMOTED":
                if existing_intent is None:
                    raise PromotionTransactionError(
                        "canonical already equals candidate but no YouMo promotion intent exists; "
                        "treat this as an external promotion and audit it manually"
                    )
                _verify_existing_intent(existing_intent, replace_readiness_canonical(
                    readiness, str(existing_intent.get("canonical_before", ""))
                ))
                final = _final_payload(existing_intent, readiness.candidate_sha)
                _atomic_create_json(evidence_path, final)
                validations = tuple(
                    PromotionValidation(
                        tuple(item.get("argv", ())),
                        int(item.get("returncode", 1)),
                        str(item.get("stdout", "")),
                        str(item.get("stderr", "")),
                    )
                    for item in existing_intent.get("validations", [])
                    if isinstance(item, dict)
                )
                return PromotionResult(
                    status="RECOVERED_PROMOTION_EVIDENCE",
                    run_id=readiness.run_id,
                    candidate_branch=readiness.candidate_branch,
                    candidate_sha=readiness.candidate_sha,
                    canonical_branch=readiness.canonical_branch,
                    canonical_before=str(existing_intent["canonical_before"]),
                    canonical_after=readiness.candidate_sha,
                    validations=validations,
                    removed_ignored_artifacts=tuple(existing_intent.get("removed_ignored_artifacts", ())),
                    intent_path=str(intent_path),
                    evidence_path=str(evidence_path),
                )

            if readiness.classification != "READY_FAST_FORWARD":
                raise PromotionTransactionError(
                    f"canonical promotion is not eligible: {readiness.classification}"
                )
            if existing_intent is not None:
                raise PromotionTransactionError(
                    "a prior promotion intent exists while canonical is still unpromoted; "
                    "do not reuse stale validation evidence"
                )

            validation_venv = (
                _validation_venv.resolve()
                if _validation_venv is not None
                else resolve_validation_venv(control)
            )
            if validation_venv is None and tuple(tuple(x) for x in _validation_commands) == DEFAULT_PROMOTION_VALIDATIONS:
                raise PromotionTransactionError(
                    "trusted external validation venv is required for canonical promotion"
                )

            work_root = state_directory(control, config.state_dir) / "promotion-work"
            _require_external_work_root(control, workspace, work_root)
            work_root.mkdir(parents=True, exist_ok=True)
            os.chmod(work_root, 0o700)
            source = _remote_source(workspace, target)

            validations: tuple[PromotionValidation, ...]
            removed: tuple[str, ...]
            with tempfile.TemporaryDirectory(
                prefix=f"{run_id[:12]}-", dir=work_root
            ) as temp_dir:
                clone = Path(temp_dir) / "repo"
                _git(control, "clone", "--no-checkout", "--no-local", source, str(clone), timeout=600)
                _verify_clone_remote_refs(clone, readiness)
                _git(
                    clone,
                    "fetch",
                    "--no-tags",
                    "origin",
                    f"refs/heads/{readiness.canonical_branch}",
                    f"refs/heads/{readiness.candidate_branch}",
                    timeout=600,
                )
                _verify_clone_remote_refs(clone, readiness)
                _git(
                    clone,
                    "checkout",
                    "-B",
                    readiness.canonical_branch,
                    readiness.canonical_sha,
                )
                _git(clone, "merge", "--ff-only", readiness.candidate_sha)
                if _git(clone, "rev-parse", "HEAD") != readiness.candidate_sha:
                    raise PromotionTransactionError(
                        "ff-only merge did not land on exact candidate commit"
                    )
                if _git(clone, "branch", "--show-current") != readiness.canonical_branch:
                    raise PromotionTransactionError("promotion clone left canonical branch")
                if _git(clone, "status", "--porcelain=v1", "-uall"):
                    raise PromotionTransactionError("promotion clone is dirty immediately after ff-only merge")

                try:
                    with bound_workspace_validation_venv(clone, validation_venv):
                        validations = _run_validations(clone, _validation_commands)
                except ValidationEnvironmentError as exc:
                    raise PromotionTransactionError(str(exc)) from exc
                failed = next((item for item in validations if not item.passed), None)
                if failed is not None:
                    raise PromotionTransactionError(
                        f"merged canonical validation failed ({' '.join(failed.argv)}): "
                        f"exit {failed.returncode}"
                    )
                try:
                    removed = cleanup_ignored_untracked(clone)
                except WorkspaceHygieneError as exc:
                    raise PromotionTransactionError(str(exc)) from exc
                if _git(clone, "status", "--porcelain=v1", "-uall"):
                    raise PromotionTransactionError(
                        "promotion clone changed during post-merge validation"
                    )
                if _git(clone, "rev-parse", "HEAD") != readiness.candidate_sha:
                    raise PromotionTransactionError("promotion clone HEAD changed during validation")

                candidate_now = resolve_remote_branch_sha(
                    workspace,
                    readiness.candidate_branch,
                    remote_target=target,
                )
                canonical_now = resolve_remote_branch_sha(
                    workspace,
                    readiness.canonical_branch,
                    remote_target=target,
                )
                if candidate_now != readiness.candidate_sha:
                    raise PromotionTransactionError(
                        "candidate remote changed during merged-canonical validation"
                    )
                if canonical_now != readiness.canonical_sha:
                    raise PromotionTransactionError(
                        "canonical remote changed during merged-canonical validation"
                    )

                intent = _intent_payload(readiness, validations, removed)
                _atomic_create_json(intent_path, intent)

                ref = f"refs/heads/{readiness.canonical_branch}"
                lease = f"--force-with-lease={ref}:{readiness.canonical_sha}"
                _git(
                    clone,
                    "push",
                    "--porcelain",
                    lease,
                    "origin",
                    f"{readiness.candidate_sha}:{ref}",
                    timeout=300,
                )

                canonical_after = resolve_remote_branch_sha(
                    workspace,
                    readiness.canonical_branch,
                    remote_target=target,
                )
                candidate_after = resolve_remote_branch_sha(
                    workspace,
                    readiness.candidate_branch,
                    remote_target=target,
                )
                if canonical_after != readiness.candidate_sha:
                    raise PromotionTransactionError(
                        "post-promotion canonical remote verification failed"
                    )
                if candidate_after != readiness.candidate_sha:
                    raise PromotionTransactionError(
                        "candidate remote changed during canonical promotion"
                    )

            final_payload = _final_payload(intent, readiness.candidate_sha)
            _atomic_create_json(evidence_path, final_payload)
            return PromotionResult(
                status="PROMOTED",
                run_id=readiness.run_id,
                candidate_branch=readiness.candidate_branch,
                candidate_sha=readiness.candidate_sha,
                canonical_branch=readiness.canonical_branch,
                canonical_before=readiness.canonical_sha,
                canonical_after=readiness.candidate_sha,
                validations=validations,
                removed_ignored_artifacts=removed,
                intent_path=str(intent_path),
                evidence_path=str(evidence_path),
            )
    except (
        OperationLeaseError,
        PromotionCheckError,
        PublishError,
        ValidationEnvironmentError,
    ) as exc:
        raise PromotionTransactionError(str(exc)) from exc


def replace_readiness_canonical(
    readiness: PromotionReadiness,
    canonical_before: str,
) -> PromotionReadiness:
    """Build an evidence-verification view without changing the observed current remote."""
    return PromotionReadiness(
        run_id=readiness.run_id,
        workspace=readiness.workspace,
        repository=readiness.repository,
        candidate_branch=readiness.candidate_branch,
        candidate_sha=readiness.candidate_sha,
        canonical_branch=readiness.canonical_branch,
        canonical_sha=canonical_before,
        classification=readiness.classification,
        fast_forward_possible=readiness.fast_forward_possible,
        promotion_needed=readiness.promotion_needed,
        candidate_in_canonical=readiness.candidate_in_canonical,
        remote_stable=readiness.remote_stable,
    )
