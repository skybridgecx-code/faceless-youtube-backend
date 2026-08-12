from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from .build_engine import BuildResult, ValidationResult, run_guarded_build
from .hygiene import WorkspaceHygieneError, cleanup_ignored_untracked
from .validation_env import bound_workspace_validation_venv
from .validation_policy import targeted_build_validation_commands


TurnRunner = Callable[..., Awaitable[object]]


def _run_validations(
    root: Path,
    commands: Sequence[Sequence[str]],
) -> tuple[ValidationResult, ...]:
    results: list[ValidationResult] = []
    for command in commands:
        argv = tuple(str(part) for part in command)
        if not argv:
            continue
        try:
            completed = subprocess.run(
                list(argv),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append(ValidationResult(argv, 125, "", str(exc)))
            break
        results.append(
            ValidationResult(
                argv=argv,
                returncode=completed.returncode,
                stdout=completed.stdout[-12000:],
                stderr=completed.stderr[-12000:],
            )
        )
        if completed.returncode != 0:
            break
    return tuple(results)


async def run_fast_guarded_build(
    *,
    workspace_root: Path,
    task: str,
    allowed_paths: Sequence[str],
    architecture_paths: Sequence[str],
    developer_instructions: str,
    model: str,
    reasoning: str,
    turn_runner: TurnRunner,
    run_id: str | None = None,
    max_changed_files: int,
    validation_venv: Path | None,
) -> BuildResult:
    result = await run_guarded_build(
        workspace_root=workspace_root,
        task=task,
        allowed_paths=allowed_paths,
        architecture_paths=architecture_paths,
        developer_instructions=developer_instructions,
        model=model,
        reasoning=reasoning,
        turn_runner=turn_runner,
        run_id=run_id,
        max_changed_files=max_changed_files,
        validation_commands=(),
    )
    if not result.ready_for_audit:
        return result

    workspace = workspace_root.resolve()
    commands = targeted_build_validation_commands(workspace, result.changed_files)
    with bound_workspace_validation_venv(workspace, validation_venv) as bound:
        if bound:
            commands = tuple(
                tuple(".venv/bin/python" if part == "python3" else part for part in command)
                for command in commands
            )
        validations = _run_validations(workspace, commands)

    violations = list(result.violations)
    failed = next((item for item in validations if not item.passed), None)
    if failed is not None:
        violations.append(
            f"targeted validation failed ({' '.join(failed.argv)}): exit {failed.returncode}"
        )

    try:
        cleanup_ignored_untracked(workspace)
    except WorkspaceHygieneError as exc:
        violations.append(f"post-validation workspace hygiene failed: {exc}")

    return replace(
        result,
        status="READY_FOR_AUDIT" if not violations else "FAILED",
        validations=validations,
        violations=tuple(violations),
    )
