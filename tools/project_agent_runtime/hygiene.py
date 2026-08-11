from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath


class WorkspaceHygieneError(RuntimeError):
    """Raised when hidden/ignored workspace artifacts cannot be handled safely."""


_DISPOSABLE_DIR_NAMES = frozenset({".pytest_cache", "__pycache__"})
_DISPOSABLE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _run_git(root: Path, *args: str, timeout: int = 30) -> str:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceHygieneError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorkspaceHygieneError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout


def ignored_untracked_files(root: Path) -> tuple[str, ...]:
    raw = _run_git(
        root.resolve(), "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--"
    )
    values = tuple(sorted(value for value in raw.split("\0") if value))
    for value in values:
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts:
            raise WorkspaceHygieneError(f"unsafe ignored path returned by Git: {value!r}")
    return values


def require_no_ignored_untracked(root: Path, *, context: str) -> None:
    values = ignored_untracked_files(root)
    if values:
        preview = list(values[:20])
        suffix = "..." if len(values) > 20 else ""
        raise WorkspaceHygieneError(
            f"{context}: ignored untracked workspace artifacts are present: {preview!r}{suffix}"
        )


def _is_disposable_ignored_artifact(relative: str) -> bool:
    pure = PurePosixPath(relative)
    if any(part in _DISPOSABLE_DIR_NAMES for part in pure.parts):
        return True
    return pure.suffix in _DISPOSABLE_SUFFIXES


def cleanup_ignored_untracked(root: Path, *, max_entries: int = 500) -> tuple[str, ...]:
    base = root.resolve()
    values = ignored_untracked_files(base)
    if len(values) > max_entries:
        raise WorkspaceHygieneError(
            f"refusing to clean {len(values)} ignored artifacts; cap is {max_entries}"
        )

    protected = tuple(
        relative for relative in values if not _is_disposable_ignored_artifact(relative)
    )
    if protected:
        preview = list(protected[:20])
        suffix = "..." if len(protected) > 20 else ""
        raise WorkspaceHygieneError(
            "refusing to clean non-disposable ignored workspace artifacts: "
            f"{preview!r}{suffix}"
        )

    removed: list[str] = []
    parents: set[Path] = set()
    for relative in values:
        candidate = base / Path(*PurePosixPath(relative).parts)
        try:
            candidate.parent.resolve().relative_to(base)
        except ValueError as exc:
            raise WorkspaceHygieneError(
                f"ignored artifact parent escapes workspace: {relative!r}"
            ) from exc
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
            removed.append(relative)
            parents.add(candidate.parent)
        elif candidate.exists():
            raise WorkspaceHygieneError(
                f"ignored artifact is not a regular file/symlink: {relative!r}"
            )

    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        current = parent
        while current != base:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
    return tuple(removed)
