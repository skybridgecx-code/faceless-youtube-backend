from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CodexTransportError(RuntimeError):
    """Raised when the Codex SDK transport cannot be used safely."""


@dataclass(frozen=True)
class CodexSdkInfo:
    installed: bool
    compatible: bool
    version: str | None
    requirement: str
    detail: str

    @property
    def ready(self) -> bool:
        return self.installed and self.compatible


@dataclass(frozen=True)
class CodexTurnResult:
    thread_id: str
    turn_id: str
    status: str
    final_response: str | None
    usage: dict[str, Any] | None


def inspect_codex_sdk(requirement: str) -> CodexSdkInfo:
    try:
        version = importlib.metadata.version("openai-codex")
    except importlib.metadata.PackageNotFoundError:
        return CodexSdkInfo(False, False, None, requirement, "openai-codex is not installed")
    expected = requirement.partition("==")[2].strip()
    compatible = not expected or version == expected
    detail = (
        f"openai-codex {version} matches pinned requirement"
        if compatible
        else f"openai-codex {version} does not match pinned {requirement}"
    )
    return CodexSdkInfo(True, compatible, version, requirement, detail)


def _load_sdk() -> Any:
    try:
        return importlib.import_module("openai_codex")
    except ImportError as exc:
        raise CodexTransportError(
            "openai-codex is not installed; install the YouMo CLI tooling requirements first"
        ) from exc


def _status_text(status: object) -> str:
    value = getattr(status, "value", None)
    if isinstance(value, str):
        return value
    return str(status)


def _jsonable_model(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if isinstance(value, dict):
        return dict(value)
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return {str(key): item for key, item in data.items()}
    return {"repr": repr(value)}


async def run_codex_turn(
    *,
    repo_root: Path,
    prompt: str,
    developer_instructions: str,
    model: str,
    reasoning: str,
    sandbox_name: str,
    thread_id: str | None = None,
    sdk: Any | None = None,
) -> CodexTurnResult:
    if sandbox_name not in {"read_only", "workspace_write"}:
        raise CodexTransportError(f"unsupported sandbox preset: {sandbox_name}")
    module = sdk or _load_sdk()
    try:
        sandbox = getattr(module.Sandbox, sandbox_name)
    except AttributeError as exc:
        raise CodexTransportError(f"SDK missing sandbox preset: {sandbox_name}") from exc

    async with module.AsyncCodex() as codex:
        common = {
            "model": model,
            "cwd": str(repo_root),
            "developer_instructions": developer_instructions,
            "sandbox": sandbox,
            "config": {"model_reasoning_effort": reasoning},
        }
        if thread_id:
            thread = await codex.thread_resume(thread_id, **common)
        else:
            thread = await codex.thread_start(**common)
        result = await thread.run(
            prompt,
            cwd=str(repo_root),
            model=model,
            effort=reasoning,
            sandbox=sandbox,
        )

    actual_thread_id = getattr(thread, "id", None)
    turn_id = getattr(result, "id", None)
    if not isinstance(actual_thread_id, str) or not actual_thread_id:
        raise CodexTransportError("Codex SDK returned no thread id")
    if not isinstance(turn_id, str) or not turn_id:
        raise CodexTransportError("Codex SDK returned no turn id")
    return CodexTurnResult(
        thread_id=actual_thread_id,
        turn_id=turn_id,
        status=_status_text(getattr(result, "status", "unknown")),
        final_response=getattr(result, "final_response", None),
        usage=_jsonable_model(getattr(result, "usage", None)),
    )
