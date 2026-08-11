from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import ProjectConfig
from .state import state_directory


class RunManifestError(RuntimeError):
    """Raised when a YouMo execution run cannot be proven or persisted safely."""


RUN_STAGES = frozenset(
    {
        "BUILD_RUNNING",
        "READY_FOR_AUDIT",
        "BUILD_FAILED",
        "AUDIT_RUNNING",
        "READY_FOR_CHECKPOINT",
        "AUDIT_FAILED",
        "CHECKPOINT_RUNNING",
        "CHECKPOINTED",
        "CHECKPOINT_FAILED",
    }
)

_STAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "BUILD_RUNNING": frozenset({"READY_FOR_AUDIT", "BUILD_FAILED"}),
    "READY_FOR_AUDIT": frozenset({"AUDIT_RUNNING"}),
    "AUDIT_RUNNING": frozenset({"READY_FOR_CHECKPOINT", "AUDIT_FAILED", "READY_FOR_AUDIT"}),
    "READY_FOR_CHECKPOINT": frozenset({"CHECKPOINT_RUNNING"}),
    "CHECKPOINT_RUNNING": frozenset({"CHECKPOINTED", "CHECKPOINT_FAILED", "READY_FOR_CHECKPOINT"}),
    "CHECKPOINT_FAILED": frozenset({"CHECKPOINT_RUNNING"}),
    "BUILD_FAILED": frozenset(),
    "AUDIT_FAILED": frozenset(),
    "CHECKPOINTED": frozenset(),
}

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class EvidenceBinding:
    path: str
    sha256: str

    @classmethod
    def from_mapping(cls, value: object, *, name: str) -> "EvidenceBinding | None":
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RunManifestError(f"run manifest {name} evidence binding must be an object or null")
        path = value.get("path")
        sha256 = value.get("sha256")
        if not isinstance(path, str) or not path:
            raise RunManifestError(f"run manifest {name} evidence path is invalid")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise RunManifestError(f"run manifest {name} evidence sha256 is invalid")
        return cls(path=path, sha256=sha256)


@dataclass(frozen=True)
class RunManifest:
    schema_version: int
    run_id: str
    project_id: str
    workspace: str
    branch: str
    base_head: str
    task: str
    task_sha256: str
    allowed_paths: tuple[str, ...]
    max_changed_files: int
    architecture_lock_sha256: str
    stage: str
    build_evidence: EvidenceBinding | None
    audit_evidence: EvidenceBinding | None
    checkpoint_evidence: EvidenceBinding | None
    checkpoint_commit: str | None
    last_error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_mapping(cls, payload: dict[str, Any], *, project_id: str) -> "RunManifest":
        if payload.get("schema_version") != 1:
            raise RunManifestError("unsupported run manifest schema_version")
        if payload.get("project_id") != project_id:
            raise RunManifestError("run manifest project identity mismatch")

        run_id = _required_string(payload.get("run_id"), "run_id")
        if not _RUN_ID_RE.fullmatch(run_id):
            raise RunManifestError("run manifest run_id is invalid")
        workspace = _required_string(payload.get("workspace"), "workspace")
        branch = _required_string(payload.get("branch"), "branch")
        base_head = _required_string(payload.get("base_head"), "base_head")
        if not _GIT_SHA_RE.fullmatch(base_head):
            raise RunManifestError("run manifest base_head is invalid")
        task = _required_string(payload.get("task"), "task")
        task_sha256 = _required_string(payload.get("task_sha256"), "task_sha256")
        if not _SHA256_RE.fullmatch(task_sha256):
            raise RunManifestError("run manifest task_sha256 is invalid")
        if _sha256_text(task) != task_sha256:
            raise RunManifestError("run manifest task text does not match task_sha256")

        raw_paths = payload.get("allowed_paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise RunManifestError("run manifest allowed_paths must be a non-empty list")
        paths: list[str] = []
        for value in raw_paths:
            if not isinstance(value, str) or not value:
                raise RunManifestError("run manifest allowed_paths contains an invalid value")
            paths.append(value)

        max_changed_files = payload.get("max_changed_files")
        if not isinstance(max_changed_files, int) or max_changed_files < 1:
            raise RunManifestError("run manifest max_changed_files is invalid")
        architecture_lock_sha256 = _required_string(
            payload.get("architecture_lock_sha256"), "architecture_lock_sha256"
        )
        if not _SHA256_RE.fullmatch(architecture_lock_sha256):
            raise RunManifestError("run manifest architecture_lock_sha256 is invalid")
        stage = _required_string(payload.get("stage"), "stage")
        if stage not in RUN_STAGES:
            raise RunManifestError(f"run manifest stage is unsupported: {stage!r}")

        checkpoint_commit = _optional_string(payload.get("checkpoint_commit"), "checkpoint_commit")
        if checkpoint_commit is not None and not _GIT_SHA_RE.fullmatch(checkpoint_commit):
            raise RunManifestError("run manifest checkpoint_commit is invalid")
        last_error = _optional_string(payload.get("last_error"), "last_error")
        created_at = _required_string(payload.get("created_at"), "created_at")
        updated_at = _required_string(payload.get("updated_at"), "updated_at")

        return cls(
            schema_version=1,
            run_id=run_id,
            project_id=project_id,
            workspace=workspace,
            branch=branch,
            base_head=base_head,
            task=task,
            task_sha256=task_sha256,
            allowed_paths=tuple(paths),
            max_changed_files=max_changed_files,
            architecture_lock_sha256=architecture_lock_sha256,
            stage=stage,
            build_evidence=EvidenceBinding.from_mapping(payload.get("build_evidence"), name="build"),
            audit_evidence=EvidenceBinding.from_mapping(payload.get("audit_evidence"), name="audit"),
            checkpoint_evidence=EvidenceBinding.from_mapping(
                payload.get("checkpoint_evidence"), name="checkpoint"
            ),
            checkpoint_commit=checkpoint_commit,
            last_error=last_error,
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunManifestError(f"run manifest {name} must be a non-empty string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RunManifestError(f"run manifest {name} must be null or a non-empty string")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runs_root(control_root: Path, config: ProjectConfig) -> Path:
    return state_directory(control_root, config.state_dir) / "runs"


def run_directory(control_root: Path, config: ProjectConfig, run_id: str) -> Path:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise RunManifestError("run_id must be a 32-character lowercase hexadecimal identifier")
    return runs_root(control_root, config) / run_id


def run_manifest_path(control_root: Path, config: ProjectConfig, run_id: str) -> Path:
    return run_directory(control_root, config, run_id) / "manifest.json"


def _atomic_json(path: Path, payload: dict[str, Any], *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_only and path.exists():
        raise RunManifestError(f"refusing to replace existing run state: {path}")
    temp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        if create_only and path.exists():
            raise RunManifestError(f"refusing to replace existing run state: {path}")
        os.replace(temp, path)
    except OSError as exc:
        raise RunManifestError(f"failed to persist run state: {path}") from exc
    finally:
        temp.unlink(missing_ok=True)


def create_run_manifest(
    control_root: Path,
    config: ProjectConfig,
    *,
    workspace: Path,
    branch: str,
    base_head: str,
    task: str,
    allowed_paths: Iterable[str],
    max_changed_files: int,
    architecture_lock_sha256: str,
) -> RunManifest:
    normalized_task = task.strip()
    if not normalized_task:
        raise RunManifestError("run task must be non-empty")
    normalized_paths = tuple(path.strip() for path in allowed_paths if path.strip())
    if not normalized_paths:
        raise RunManifestError("run allowed_paths must be non-empty")
    if not _GIT_SHA_RE.fullmatch(base_head):
        raise RunManifestError("run base_head must be an exact 40-character Git SHA")
    if max_changed_files < 1:
        raise RunManifestError("run max_changed_files must be positive")
    if not _SHA256_RE.fullmatch(architecture_lock_sha256):
        raise RunManifestError("run architecture_lock_sha256 is invalid")

    now = _utc_now()
    for _ in range(8):
        run_id = secrets.token_hex(16)
        path = run_manifest_path(control_root, config, run_id)
        if path.exists():
            continue
        manifest = RunManifest(
            schema_version=1,
            run_id=run_id,
            project_id=config.project_id,
            workspace=str(workspace.expanduser().resolve()),
            branch=branch,
            base_head=base_head,
            task=normalized_task,
            task_sha256=_sha256_text(normalized_task),
            allowed_paths=normalized_paths,
            max_changed_files=max_changed_files,
            architecture_lock_sha256=architecture_lock_sha256,
            stage="BUILD_RUNNING",
            build_evidence=None,
            audit_evidence=None,
            checkpoint_evidence=None,
            checkpoint_commit=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        _atomic_json(path, manifest.to_mapping(), create_only=True)
        return manifest
    raise RunManifestError("failed to allocate a unique run_id")


def load_run_manifest(control_root: Path, config: ProjectConfig, run_id: str) -> RunManifest:
    path = run_manifest_path(control_root, config, run_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunManifestError(f"run manifest not found: {run_id}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RunManifestError(f"run manifest is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RunManifestError("run manifest root must be an object")
    manifest = RunManifest.from_mapping(payload, project_id=config.project_id)
    if manifest.run_id != run_id:
        raise RunManifestError("run manifest identifier does not match its directory")
    return manifest


def save_run_manifest(
    control_root: Path,
    config: ProjectConfig,
    manifest: RunManifest,
) -> Path:
    existing = load_run_manifest(control_root, config, manifest.run_id)
    if existing.created_at != manifest.created_at:
        raise RunManifestError("run manifest created_at changed unexpectedly")
    path = run_manifest_path(control_root, config, manifest.run_id)
    _atomic_json(path, manifest.to_mapping())
    return path


def transition_run(
    control_root: Path,
    config: ProjectConfig,
    manifest: RunManifest,
    *,
    new_stage: str,
    last_error: str | None = None,
    build_evidence: EvidenceBinding | None = None,
    audit_evidence: EvidenceBinding | None = None,
    checkpoint_evidence: EvidenceBinding | None = None,
    checkpoint_commit: str | None = None,
) -> RunManifest:
    if new_stage not in RUN_STAGES:
        raise RunManifestError(f"unsupported run stage: {new_stage}")
    allowed = _STAGE_TRANSITIONS.get(manifest.stage, frozenset())
    if new_stage not in allowed:
        raise RunManifestError(
            f"invalid run stage transition: {manifest.stage} -> {new_stage}"
        )
    if checkpoint_commit is not None and not _GIT_SHA_RE.fullmatch(checkpoint_commit):
        raise RunManifestError("checkpoint_commit must be an exact 40-character Git SHA")

    updated = replace(
        manifest,
        stage=new_stage,
        build_evidence=build_evidence or manifest.build_evidence,
        audit_evidence=audit_evidence or manifest.audit_evidence,
        checkpoint_evidence=checkpoint_evidence or manifest.checkpoint_evidence,
        checkpoint_commit=checkpoint_commit or manifest.checkpoint_commit,
        last_error=last_error,
        updated_at=_utc_now(),
    )
    save_run_manifest(control_root, config, updated)
    return updated


def update_run_error(
    control_root: Path,
    config: ProjectConfig,
    manifest: RunManifest,
    *,
    last_error: str | None,
) -> RunManifest:
    updated = replace(manifest, last_error=last_error, updated_at=_utc_now())
    save_run_manifest(control_root, config, updated)
    return updated


def write_run_evidence(
    control_root: Path,
    config: ProjectConfig,
    manifest: RunManifest,
    *,
    stage_name: str,
    payload: str,
) -> EvidenceBinding:
    if stage_name not in {"build", "audit", "checkpoint"}:
        raise RunManifestError(f"unsupported evidence stage: {stage_name}")
    path = run_directory(control_root, config, manifest.run_id) / f"{stage_name}.json"
    if path.exists():
        raise RunManifestError(
            f"refusing to overwrite immutable {stage_name} evidence for run {manifest.run_id}"
        )
    temp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        temp.write_text(payload, encoding="utf-8")
        os.chmod(temp, 0o600)
        if path.exists():
            raise RunManifestError(
                f"refusing to overwrite immutable {stage_name} evidence for run {manifest.run_id}"
            )
        os.replace(temp, path)
    except OSError as exc:
        raise RunManifestError(f"failed to persist {stage_name} evidence: {path}") from exc
    finally:
        temp.unlink(missing_ok=True)
    return EvidenceBinding(path=str(path.resolve()), sha256=sha256_file(path))


def resolve_evidence(binding: EvidenceBinding, *, expected_parent: Path) -> Path:
    path = Path(binding.path).expanduser().resolve()
    parent = expected_parent.resolve()
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise RunManifestError(f"run evidence escapes its run directory: {path}") from exc
    if not path.is_file() or path.is_symlink():
        raise RunManifestError(f"run evidence is missing or not a regular file: {path}")
    actual = sha256_file(path)
    if actual != binding.sha256:
        raise RunManifestError(
            f"run evidence hash mismatch: {path}; actual={actual}; expected={binding.sha256}"
        )
    return path


def list_run_manifests(
    control_root: Path,
    config: ProjectConfig,
    *,
    workspace: Path | None = None,
) -> tuple[RunManifest, ...]:
    root = runs_root(control_root, config)
    if not root.is_dir():
        return ()
    wanted = str(workspace.expanduser().resolve()) if workspace is not None else None
    manifests: list[RunManifest] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not _RUN_ID_RE.fullmatch(child.name):
            continue
        manifest = load_run_manifest(control_root, config, child.name)
        if wanted is None or manifest.workspace == wanted:
            manifests.append(manifest)
    manifests.sort(key=lambda item: (item.created_at, item.run_id), reverse=True)
    return tuple(manifests)
