"""Compatibility bridges from the current YouMo runtime into M1 contracts."""

from __future__ import annotations

from .config import ProjectConfig
from .core.contracts import (
    ArchitectureSpec,
    ExecutionSpec,
    HarnessSpec,
    ProjectProfile,
    RepositorySpec,
    ValidationSpec,
)


def project_profile_from_legacy(config: ProjectConfig) -> ProjectProfile:
    """Normalize a schema-v1 YouMo project config without changing that manifest.

    This adapter is deliberately one-way: the existing schema-v1 file and
    ``load_project_config`` remain the compatibility authority in M1.
    """

    if config.schema_version != 1:
        raise ValueError(
            f"unsupported legacy project config schema_version={config.schema_version!r}"
        )

    return ProjectProfile(
        project_id=config.project_id,
        display_name=config.display_name,
        repository=RepositorySpec(
            repository=config.repository,
            canonical_branch=config.canonical_branch,
            allowed_branch_prefixes=config.allowed_branch_prefixes,
            execution_branch_prefixes=config.execution_branch_prefixes,
        ),
        architecture=ArchitectureSpec(
            lock_path=config.architecture_lock,
            source_paths=config.architecture_sources,
        ),
        execution=ExecutionSpec(state_directory=config.state_dir),
        validation=ValidationSpec(require_explicit_execute=config.codex.require_explicit_execute),
        harness=HarnessSpec(
            adapter_id="codex",
            sdk_requirement=config.codex.sdk_requirement,
            implementation_model=config.codex.implementation_model,
            implementation_reasoning=config.codex.implementation_reasoning,
            audit_model=config.codex.audit_model,
            audit_reasoning=config.codex.audit_reasoning,
        ),
    )
