from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProjectConfigError(ValueError):
    """Raised when a project runtime manifest is invalid."""


@dataclass(frozen=True)
class ProjectConfig:
    schema_version: int
    project_id: str
    display_name: str
    repository: str
    canonical_branch: str
    allowed_branch_prefixes: tuple[str, ...]
    architecture_lock: str
    architecture_sources: tuple[str, ...]
    state_dir: str

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
        sources = data["architecture_sources"]
        if not isinstance(prefixes, list) or not all(
            isinstance(value, str) and value for value in prefixes
        ):
            raise ProjectConfigError("allowed_branch_prefixes must be a list of strings")
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

        return cls(
            schema_version=1,
            project_id=data["project_id"].strip(),
            display_name=data["display_name"].strip(),
            repository=repository,
            canonical_branch=data["canonical_branch"].strip(),
            allowed_branch_prefixes=tuple(prefixes),
            architecture_lock=data["architecture_lock"].strip(),
            architecture_sources=tuple(sources),
            state_dir=data["state_dir"].strip(),
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
