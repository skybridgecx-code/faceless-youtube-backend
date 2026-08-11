from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Sequence


FULL_VALIDATION_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "pytest", "-q"),
    ("node", "--check", "app/static/app.js"),
)


def _existing_python_files(root: Path, files: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for relative in files:
        pure = PurePosixPath(relative)
        if pure.suffix != ".py":
            continue
        path = root / Path(*pure.parts)
        if path.is_file() and not path.is_symlink():
            result.append(relative)
    return tuple(result)


def _existing_javascript_files(root: Path, files: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for relative in files:
        pure = PurePosixPath(relative)
        if pure.suffix not in {".js", ".mjs", ".cjs"}:
            continue
        path = root / Path(*pure.parts)
        if path.is_file() and not path.is_symlink():
            result.append(relative)
    return tuple(result)


def _direct_tests(root: Path, files: Sequence[str]) -> tuple[str, ...]:
    tests: set[str] = set()
    for relative in files:
        pure = PurePosixPath(relative)
        if pure.parts and pure.parts[0] == "tests" and pure.suffix == ".py":
            path = root / Path(*pure.parts)
            if path.is_file() and not path.is_symlink():
                tests.add(relative)
            continue
        if pure.suffix != ".py" or not pure.name:
            continue
        candidate = root / "tests" / f"test_{pure.stem}.py"
        if candidate.is_file() and not candidate.is_symlink():
            tests.add(candidate.relative_to(root).as_posix())
    return tuple(sorted(tests))


def targeted_build_validation_commands(
    root: Path,
    changed_files: Sequence[str],
) -> tuple[tuple[str, ...], ...]:
    """Fast build-time validation; full regression remains mandatory at audit."""
    commands: list[tuple[str, ...]] = [("git", "diff", "--check")]

    python_files = _existing_python_files(root, changed_files)
    if python_files:
        commands.append(("python3", "-m", "py_compile", *python_files))

    direct_tests = _direct_tests(root, changed_files)
    if direct_tests:
        commands.append(("python3", "-m", "pytest", "-q", *direct_tests))

    for relative in _existing_javascript_files(root, changed_files):
        commands.append(("node", "--check", relative))

    return tuple(commands)
