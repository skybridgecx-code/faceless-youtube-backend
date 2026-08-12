from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RuntimeStateError(RuntimeError):
    """Raised when persistent YouMo agent state cannot be loaded or written safely."""


@dataclass(frozen=True)
class RuntimeState:
    schema_version: int
    project_id: str
    thread_id: str | None
    last_turn_id: str | None
    last_turn_status: str | None
    last_head: str | None
    architecture_lock_sha256: str | None
    updated_at: str

    @classmethod
    def empty(cls, project_id: str) -> "RuntimeState":
        return cls(
            schema_version=1,
            project_id=project_id,
            thread_id=None,
            last_turn_id=None,
            last_turn_status=None,
            last_head=None,
            architecture_lock_sha256=None,
            updated_at=_utc_now(),
        )

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, project_id: str) -> "RuntimeState":
        if data.get("schema_version") != 1:
            raise RuntimeStateError("unsupported runtime state schema_version")
        if data.get("project_id") != project_id:
            raise RuntimeStateError("runtime state project identity mismatch")
        return cls(
            schema_version=1,
            project_id=project_id,
            thread_id=_optional_str(data.get("thread_id"), "thread_id"),
            last_turn_id=_optional_str(data.get("last_turn_id"), "last_turn_id"),
            last_turn_status=_optional_str(data.get("last_turn_status"), "last_turn_status"),
            last_head=_optional_str(data.get("last_head"), "last_head"),
            architecture_lock_sha256=_optional_str(data.get("architecture_lock_sha256"), "architecture_lock_sha256"),
            updated_at=_required_str(data.get("updated_at"), "updated_at"),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeStateError(f"{name} must be null or a non-empty string")
    return value


def _required_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeStateError(f"{name} must be a non-empty string")
    return value


def state_directory(repo_root: Path, state_dir: str) -> Path:
    configured = Path(state_dir).expanduser()
    if configured.is_absolute():
        return configured.resolve()
    return (repo_root / configured).resolve()


def state_path(repo_root: Path, state_dir: str) -> Path:
    return state_directory(repo_root, state_dir) / "runtime-state.json"


def load_runtime_state(repo_root: Path, state_dir: str, *, project_id: str) -> RuntimeState:
    path = state_path(repo_root, state_dir)
    if not path.exists():
        return RuntimeState.empty(project_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeStateError(f"failed to load runtime state: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeStateError("runtime state root must be an object")
    return RuntimeState.from_mapping(payload, project_id=project_id)


def save_runtime_state(repo_root: Path, state_dir: str, state: RuntimeState) -> Path:
    path = state_path(repo_root, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    payload = json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeStateError(f"failed to persist runtime state: {path}") from exc
    return path


def advance_runtime_state(
    previous: RuntimeState,
    *,
    thread_id: str,
    turn_id: str,
    turn_status: str,
    head: str,
    architecture_lock_sha256: str,
) -> RuntimeState:
    return RuntimeState(
        schema_version=1,
        project_id=previous.project_id,
        thread_id=thread_id,
        last_turn_id=turn_id,
        last_turn_status=turn_status,
        last_head=head,
        architecture_lock_sha256=architecture_lock_sha256,
        updated_at=_utc_now(),
    )


def resolve_resume_thread(
    state: RuntimeState,
    *,
    current_head: str,
    architecture_lock_sha256: str,
    fresh_thread: bool,
) -> str | None:
    if fresh_thread or state.thread_id is None:
        return None
    if state.last_head != current_head:
        raise RuntimeStateError(
            "saved Codex thread HEAD does not match current repository HEAD"
        )
    if state.architecture_lock_sha256 != architecture_lock_sha256:
        raise RuntimeStateError(
            "saved Codex thread architecture lock does not match current authority"
        )
    return state.thread_id
