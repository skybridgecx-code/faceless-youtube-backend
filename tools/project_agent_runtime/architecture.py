from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .git_state import RepoState


class ArchitectureError(RuntimeError):
    """Raised when architecture authority cannot be loaded deterministically."""


@dataclass(frozen=True)
class ArchitectureSnapshot:
    lock_sha256: str
    source_sha256: dict[str, str]
    lock: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_architecture_snapshot(repo_root: Path, config: ProjectConfig) -> ArchitectureSnapshot:
    lock_path = repo_root / config.architecture_lock
    try:
        raw = lock_path.read_text(encoding="utf-8")
        lock = json.loads(raw)
    except FileNotFoundError as exc:
        raise ArchitectureError(f"architecture lock missing: {lock_path}") from exc
    except json.JSONDecodeError as exc:
        raise ArchitectureError(f"architecture lock is invalid JSON: {lock_path}") from exc
    if not isinstance(lock, dict):
        raise ArchitectureError("architecture lock root must be a JSON object")

    source_hashes: dict[str, str] = {}
    for relative in config.architecture_sources:
        path = repo_root / relative
        if not path.is_file():
            raise ArchitectureError(f"architecture source missing: {relative}")
        source_hashes[relative] = _sha256(path)

    return ArchitectureSnapshot(
        lock_sha256=_sha256(lock_path),
        source_sha256=source_hashes,
        lock=lock,
    )


def compile_context_capsule(
    config: ProjectConfig,
    repo_state: RepoState,
    snapshot: ArchitectureSnapshot,
) -> dict[str, Any]:
    lock = snapshot.lock
    commercial = lock.get("commercial_success_contract", {})
    phase_requirements = commercial.get("phase_requirements", {}) if isinstance(commercial, dict) else {}
    return {
        "capsule_schema_version": 1,
        "project": {
            "id": config.project_id,
            "display_name": config.display_name,
            "repository": config.repository,
            "canonical_branch": config.canonical_branch,
        },
        "repository_state": {
            "branch": repo_state.branch,
            "head": repo_state.head,
            "clean": repo_state.clean,
            "staged_files": list(repo_state.staged_files),
        },
        "architecture": {
            "lock_file": config.architecture_lock,
            "lock_sha256": snapshot.lock_sha256,
            "schema_version": lock.get("schema_version"),
            "status": lock.get("architecture_status"),
            "runtime_stages": lock.get("runtime_stages", []),
            "hard_invariants": lock.get("hard_invariants", []),
            "forbidden_v1": lock.get("forbidden_v1", []),
            "migration_rules": lock.get("migration_rules", []),
            "phase_requirements": phase_requirements,
            "source_sha256": snapshot.source_sha256,
        },
        "codex_policy": {
            "implementation_model": config.codex.implementation_model,
            "implementation_reasoning": config.codex.implementation_reasoning,
            "audit_model": config.codex.audit_model,
            "audit_reasoning": config.codex.audit_reasoning,
        },
    }


def canonical_capsule_json(capsule: dict[str, Any]) -> str:
    return json.dumps(capsule, indent=2, sort_keys=True) + "\n"
