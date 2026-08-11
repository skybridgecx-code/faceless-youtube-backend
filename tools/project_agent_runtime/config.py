from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProjectConfigError(ValueError):
    """Raised when the YouMo runtime manifest is invalid."""


@dataclass(frozen=True)
class CodexPolicy:
    sdk_requirement: str
    implementation_model: str
    implementation_reasoning: str
    audit_model: str
    audit_reasoning: str
    require_explicit_execute: bool


@dataclass(frozen=True)
class ProjectConfig:
    schema_version: int
    project_id: str
    display_name: str
    repository: str
    canonical_branch: str
    allowed_branch_prefixes: tuple[str, ...]
    execution_branch_prefixes: tuple[str, ...]
    architecture_lock: str
    architecture_sources: tuple[str, ...]
    state_dir: str
    codex: CodexPolicy

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ProjectConfig":
        required = {
            "schema_version",
            "project_id",
            "display_name",
            "repository",
            "canonical_branch",
            "allowed_branch_prefixes",
            "architecture_lock",
            "architecture_sources",
            "state_dir",
            "codex",
        }
        missing = sorted(required - data.keys())
        if missing:
            raise ProjectConfigError(
                f"project manifest missing required keys: {', '.join(missing)}"
            )
        if data["schema_version"] != 1:
            raise ProjectConfigError(
                f"unsupported project manifest schema_version={data['schema_version']!r}"
            )

        prefixes = data["allowed_branch_prefixes"]
        execution_prefixes = data.get("execution_branch_prefixes", prefixes)
        sources = data["architecture_sources"]
        if not isinstance(prefixes, list) or not all(
            isinstance(value, str) and value for value in prefixes
        ):
            raise ProjectConfigError("allowed_branch_prefixes must be a list of strings")
        if not isinstance(execution_prefixes, list) or not all(
            isinstance(value, str) and value for value in execution_prefixes
        ):
            raise ProjectConfigError("execution_branch_prefixes must be a list of strings")
        if not isinstance(sources, list) or not all(
            isinstance(value, str) and value for value in sources
        ):
            raise ProjectConfigError("architecture_sources must be a list of strings")

        scalar_keys = (
            "project_id",
            "display_name",
            "repository",
            "canonical_branch",
            "architecture_lock",
            "state_dir",
        )
        for key in scalar_keys:
            if not isinstance(data[key], str) or not data[key].strip():
                raise ProjectConfigError(f"{key} must be a non-empty string")

        repository = data["repository"].strip().removesuffix(".git")
        if repository.count("/") != 1:
            raise ProjectConfigError("repository must use owner/name form")

        codex = data["codex"]
        if not isinstance(codex, dict):
            raise ProjectConfigError("codex must be an object")
        codex_required = {
            "sdk_requirement",
            "implementation_model",
            "implementation_reasoning",
            "audit_model",
            "audit_reasoning",
            "require_explicit_execute",
        }
        codex_missing = sorted(codex_required - codex.keys())
        if codex_missing:
            raise ProjectConfigError(
                f"codex config missing required keys: {', '.join(codex_missing)}"
            )
        for key in (
            "sdk_requirement",
            "implementation_model",
            "implementation_reasoning",
            "audit_model",
            "audit_reasoning",
        ):
            if not isinstance(codex[key], str) or not codex[key].strip():
                raise ProjectConfigError(f"codex.{key} must be a non-empty string")
        allowed_reasoning = {"none", "low", "medium", "high", "xhigh", "max"}
        if codex["implementation_reasoning"] not in allowed_reasoning:
            raise ProjectConfigError("codex.implementation_reasoning is invalid")
        if codex["audit_reasoning"] not in allowed_reasoning:
            raise ProjectConfigError("codex.audit_reasoning is invalid")
        if not isinstance(codex["require_explicit_execute"], bool):
            raise ProjectConfigError("codex.require_explicit_execute must be boolean")

        return cls(
            schema_version=1,
            project_id=data["project_id"].strip(),
            display_name=data["display_name"].strip(),
            repository=repository,
            canonical_branch=data["canonical_branch"].strip(),
            allowed_branch_prefixes=tuple(prefixes),
            execution_branch_prefixes=tuple(execution_prefixes),
            architecture_lock=data["architecture_lock"].strip(),
            architecture_sources=tuple(sources),
            state_dir=data["state_dir"].strip(),
            codex=CodexPolicy(
                sdk_requirement=codex["sdk_requirement"].strip(),
                implementation_model=codex["implementation_model"].strip(),
                implementation_reasoning=codex["implementation_reasoning"].strip(),
                audit_model=codex["audit_model"].strip(),
                audit_reasoning=codex["audit_reasoning"].strip(),
                require_explicit_execute=codex["require_explicit_execute"],
            ),
        )


def load_project_config(repo_root: Path, manifest_path: str | Path) -> ProjectConfig:
    path = Path(manifest_path)
    if not path.is_absolute():
        path = repo_root / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectConfigError(f"project manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProjectConfigError(f"project manifest is not valid JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise ProjectConfigError("project manifest root must be a JSON object")
    return ProjectConfig.from_mapping(payload)
