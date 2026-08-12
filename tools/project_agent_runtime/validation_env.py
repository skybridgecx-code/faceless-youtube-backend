from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ValidationEnvironmentError(RuntimeError):
    """Raised when a trusted external validation environment cannot be bound safely."""


def resolve_validation_venv(control_root: Path) -> Path | None:
    configured = os.environ.get("YOUMO_VALIDATION_VENV", "").strip()
    candidate = Path(configured).expanduser() if configured else control_root.resolve() / ".venv"
    if not candidate.exists():
        return None
    resolved = candidate.resolve()
    python = resolved / "bin" / "python"
    if not python.is_file():
        raise ValidationEnvironmentError(
            f"validation virtualenv has no bin/python: {resolved}"
        )
    return resolved


@contextmanager
def bound_workspace_validation_venv(
    workspace_root: Path,
    validation_venv: Path | None,
) -> Iterator[bool]:
    workspace = workspace_root.resolve()
    link = workspace / ".venv"
    if validation_venv is None:
        yield False
        return

    target = validation_venv.resolve()
    try:
        target.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise ValidationEnvironmentError(
            "validation virtualenv must live outside the executor workspace"
        )

    if link.exists() or link.is_symlink():
        raise ValidationEnvironmentError(
            "executor already contains .venv; YouMo will not replace or reuse an untrusted workspace environment"
        )

    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        raise ValidationEnvironmentError(
            f"failed to bind trusted validation virtualenv: {link} -> {target}"
        ) from exc

    try:
        yield True
    finally:
        if not link.is_symlink():
            raise ValidationEnvironmentError(
                "trusted validation .venv binding was removed or replaced during validation"
            )
        actual = link.resolve(strict=False)
        if actual != target:
            raise ValidationEnvironmentError(
                f"trusted validation .venv binding target changed: {actual} != {target}"
            )
        try:
            link.unlink()
        except OSError as exc:
            raise ValidationEnvironmentError(
                f"failed to remove trusted validation .venv binding: {link}"
            ) from exc
