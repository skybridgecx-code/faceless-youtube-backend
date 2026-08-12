from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Sequence

from .usage import UsageRecord, capture_turn_usage


class BuildGuardError(RuntimeError):
    """Raised when a guarded build cannot be proven safe."""


@dataclass(frozen=True)
class ValidationResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class BuildResult:
    status: str
    thread_id: str | None
    turn_id: str | None
    turn_status: str | None
    base_head: str
    branch: str
    task_sha256: str
    allowed_paths: tuple[str, ...]
    changed_files: tuple[str, ...]
    diff_sha256: str | None
    file_fingerprints: dict[str, dict[str, Any]]
    validations: tuple[ValidationResult, ...]
    violations: tuple[str, ...]
    model_response: str | None
    usage: UsageRecord | None = None

    @property
    def ready_for_audit(self) -> bool:
        return self.status == "READY_FOR_AUDIT" and not self.violations

    def to_json(self) -> str:
        payload = asdict(self)
        payload["ready_for_audit"] = self.ready_for_audit
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


DEFAULT_VALIDATION_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "pytest", "-q"),
    ("node", "--check", "app/static/app.js"),
)


def _run_git(root: Path, *args: str, timeout: int = 30) -> str:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildGuardError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise BuildGuardError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout.rstrip("\n")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _branch(root: Path) -> str:
    return _run_git(root, "branch", "--show-current").strip()


def _head(root: Path) -> str:
    return _run_git(root, "rev-parse", "HEAD").strip()


def _staged_files(root: Path) -> tuple[str, ...]:
    return tuple(
        line
        for line in _run_git(root, "diff", "--cached", "--name-only", "--").splitlines()
        if line
    )


def _tracked_changes(root: Path) -> set[str]:
    names: set[str] = set()
    for args in (
        ("diff", "--name-only", "--no-renames", "HEAD", "--"),
        ("diff", "--cached", "--name-only", "--no-renames", "HEAD", "--"),
    ):
        for line in _run_git(root, *args).splitlines():
            if line:
                names.add(line)
    return names


def _untracked_changes(root: Path) -> set[str]:
    return {
        line
        for line in _run_git(root, "ls-files", "--others", "--exclude-standard", "--").splitlines()
        if line
    }


def changed_files(root: Path) -> tuple[str, ...]:
    return tuple(sorted(_tracked_changes(root) | _untracked_changes(root)))


def _git_dir(root: Path) -> Path:
    raw = _run_git(root, "rev-parse", "--git-dir")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _hash_optional_file(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "MISSING"
    if path.is_symlink():
        return "SYMLINK:" + _sha256_bytes(os.readlink(path).encode("utf-8"))
    if path.is_file():
        return _sha256_path(path)
    return "NON_FILE"


def git_control_fingerprint(root: Path) -> dict[str, str]:
    git_dir = _git_dir(root)
    targets = [
        git_dir / "HEAD",
        git_dir / "config",
        git_dir / "packed-refs",
        git_dir / "info" / "exclude",
    ]
    hooks = git_dir / "hooks"
    if hooks.exists():
        targets.extend(sorted(path for path in hooks.rglob("*") if path.is_file() or path.is_symlink()))
    result: dict[str, str] = {}
    for target in targets:
        try:
            key = str(target.relative_to(git_dir))
        except ValueError:
            key = str(target)
        result[key] = _hash_optional_file(target)
    return result


def architecture_fingerprint(root: Path, paths: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in paths:
        relative = PurePosixPath(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise BuildGuardError(f"invalid architecture authority path: {raw!r}")
        path = root / Path(*relative.parts)
        result[raw] = _hash_optional_file(path)
    return result


def _normalize_scope(raw: str) -> str:
    cleaned = raw.strip().strip("/")
    path = PurePosixPath(cleaned)
    if not cleaned or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise BuildGuardError(f"invalid allowed path scope: {raw!r}")
    return path.as_posix()


def normalize_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(_normalize_scope(scope) for scope in scopes))
    if not normalized:
        raise BuildGuardError("at least one allowed path scope is required")
    return normalized


def _path_in_scope(path: str, scopes: Sequence[str]) -> bool:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    return any(path == scope or path.startswith(scope + "/") for scope in scopes)


def _safe_changed_path(root: Path, relative: str) -> tuple[bool, str | None]:
    path = root / Path(*PurePosixPath(relative).parts)
    if not path.exists() and not path.is_symlink():
        return True, None
    if path.is_symlink():
        target = path.resolve(strict=False)
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return False, f"symlink escapes workspace: {relative} -> {target}"
    return True, None


def file_fingerprint(root: Path, relative: str) -> dict[str, Any]:
    path = root / Path(*PurePosixPath(relative).parts)
    if not path.exists() and not path.is_symlink():
        return {"kind": "deleted", "sha256": None, "mode": None}
    st = path.lstat()
    mode = stat.S_IMODE(st.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        return {"kind": "symlink", "sha256": _sha256_bytes(target.encode("utf-8")), "mode": mode, "target": target}
    if path.is_file():
        return {"kind": "file", "sha256": _sha256_path(path), "mode": mode, "size": st.st_size}
    return {"kind": "other", "sha256": None, "mode": mode}


def diff_fingerprint(root: Path, files: Sequence[str]) -> tuple[str, dict[str, dict[str, Any]]]:
    fingerprints = {path: file_fingerprint(root, path) for path in files}
    patch = _run_git(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--").encode("utf-8")
    payload = json.dumps(fingerprints, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(patch + b"\0" + payload), fingerprints


def _run_validations(root: Path, commands: Sequence[Sequence[str]]) -> tuple[ValidationResult, ...]:
    results: list[ValidationResult] = []
    for command in commands:
        argv = tuple(str(part) for part in command)
        if not argv:
            raise BuildGuardError("validation command cannot be empty")
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


def build_prompt(task: str, allowed_paths: Sequence[str]) -> str:
    scope = "\n".join(f"- {path}" for path in allowed_paths)
    return (
        "Implement exactly the following YouMo task in this isolated executor clone.\n\n"
        f"TASK:\n{task.strip()}\n\n"
        "WRITE SCOPE (exclusive):\n"
        f"{scope}\n\n"
        "Do not modify files outside that scope. Do not modify Git metadata, stage files, commit, push, merge, rebase, reset, stash, or switch branches. "
        "Do not modify architecture authorities. Run only checks useful to implementation; the controller independently validates after you return. "
        "Return a concise completion report, but do not claim success for checks you did not observe."
    )


TurnRunner = Callable[..., Awaitable[Any]]


async def run_guarded_build(
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
    max_changed_files: int = 20,
    validation_commands: Sequence[Sequence[str]] = DEFAULT_VALIDATION_COMMANDS,
) -> BuildResult:
    root = workspace_root.resolve()
    if not root.is_dir():
        raise BuildGuardError(f"executor workspace does not exist: {root}")
    if not task.strip():
        raise BuildGuardError("build task must be non-empty")
    if max_changed_files < 1:
        raise BuildGuardError("max_changed_files must be positive")
    scopes = normalize_scopes(allowed_paths)

    branch_before = _branch(root)
    head_before = _head(root)
    if not branch_before:
        raise BuildGuardError("executor workspace is detached")
    if changed_files(root):
        raise BuildGuardError("executor workspace must be clean before guarded build")
    if _staged_files(root):
        raise BuildGuardError("executor index must be empty before guarded build")

    git_before = git_control_fingerprint(root)
    arch_before = architecture_fingerprint(root, architecture_paths)
    task_sha = _sha256_bytes(task.strip().encode("utf-8"))

    result = await turn_runner(
        repo_root=root,
        prompt=build_prompt(task, scopes),
        developer_instructions=developer_instructions,
        model=model,
        reasoning=reasoning,
        sandbox_name="workspace_write",
        thread_id=None,
    )

    usage = capture_turn_usage(
        getattr(result, "usage", None),
        run_id=run_id,
        stage="build",
        model=model,
        reasoning_effort=reasoning,
    )

    violations: list[str] = []
    branch_after = _branch(root)
    head_after = _head(root)
    staged_after = _staged_files(root)
    git_after = git_control_fingerprint(root)
    arch_after = architecture_fingerprint(root, architecture_paths)
    files = changed_files(root)

    if branch_after != branch_before:
        violations.append(f"branch changed: {branch_before!r} -> {branch_after!r}")
    if head_after != head_before:
        violations.append(f"HEAD changed: {head_before} -> {head_after}")
    if staged_after:
        violations.append(f"index is not empty: {list(staged_after)!r}")
    if git_after != git_before:
        violations.append("Git control metadata changed")
    if arch_after != arch_before:
        violations.append("architecture authority changed")
    if not files:
        violations.append("Codex produced no file changes")
    if len(files) > max_changed_files:
        violations.append(f"changed-file cap exceeded: {len(files)} > {max_changed_files}")

    for path in files:
        if not _path_in_scope(path, scopes):
            violations.append(f"out-of-scope change: {path}")
        safe, detail = _safe_changed_path(root, path)
        if not safe and detail:
            violations.append(detail)

    turn_status = str(getattr(result, "status", "unknown"))
    if turn_status.lower() not in {"completed", "complete", "success", "succeeded"}:
        violations.append(f"Codex turn did not complete successfully: {turn_status}")

    validations: tuple[ValidationResult, ...] = ()
    if not violations:
        validations = _run_validations(root, validation_commands)
        failed = next((item for item in validations if not item.passed), None)
        if failed is not None:
            violations.append(
                f"validation failed ({' '.join(failed.argv)}): exit {failed.returncode}"
            )

    diff_sha: str | None = None
    fingerprints: dict[str, dict[str, Any]] = {}
    if files:
        diff_sha, fingerprints = diff_fingerprint(root, files)

    return BuildResult(
        status="READY_FOR_AUDIT" if not violations else "FAILED",
        thread_id=getattr(result, "thread_id", None),
        turn_id=getattr(result, "turn_id", None),
        turn_status=turn_status,
        base_head=head_before,
        branch=branch_before,
        task_sha256=task_sha,
        allowed_paths=scopes,
        changed_files=files,
        diff_sha256=diff_sha,
        file_fingerprints=fingerprints,
        validations=validations,
        violations=tuple(violations),
        model_response=getattr(result, "final_response", None),
        usage=usage,
    )
