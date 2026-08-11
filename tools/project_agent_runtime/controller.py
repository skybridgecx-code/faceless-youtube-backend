from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import load_project_config
from .gates import run_preflight_gate
from .git_state import inspect_repo


class ControllerError(RuntimeError):
    """Raised when the dedicated YouMo controller cannot be provisioned safely."""


DEFAULT_CONTROLLER_REF = "tooling/youmo-cli-i7"
DEFAULT_RUNTIME_BRANCH = "tooling/controller-runtime"
DEFAULT_REPOSITORY_URL = "https://github.com/skybridgecx-code/faceless-youtube-backend.git"
MANAGED_LAUNCHER_MARKER = "# YouMo managed launcher v1"
_MANIFEST = "tools/project_agent_runtime/projects/youmo.json"
_REQUIREMENTS = ("requirements.txt", "requirements-youmo-cli.txt")
_LAUNCHERS = ("youmo", "youmo-build", "youmo-audit", "youmo-checkpoint", "youmo-controller")


@dataclass(frozen=True)
class ControllerLayout:
    home: Path
    repo: Path
    venv: Path
    bin: Path
    metadata: Path
    dependency_stamp: Path


@dataclass(frozen=True)
class ControllerMetadata:
    schema_version: int
    repository: str
    source_ref: str
    commit_sha: str
    runtime_branch: str
    repo_path: str
    venv_path: str
    bin_path: str


@dataclass(frozen=True)
class ControllerStatus:
    installed: bool
    ready: bool
    repo_path: str
    venv_path: str
    bin_path: str
    head: str | None
    branch: str | None
    expected_head: str | None
    clean: bool | None
    dependencies_ready: bool
    launchers_ready: bool
    detail: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def default_layout(home: Path | None = None) -> ControllerLayout:
    root = (home or Path("~/.youmo/controller").expanduser()).resolve()
    return ControllerLayout(
        home=root,
        repo=root / "repo",
        venv=root / "venv",
        bin=root.parent / "bin",
        metadata=root / "controller.json",
        dependency_stamp=root / "dependencies.json",
    )


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
    runner: CommandRunner = subprocess.run,
) -> str:
    try:
        completed = runner(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControllerError(f"command failed to start: {argv!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ControllerError(
            f"command failed ({completed.returncode}): {' '.join(argv)}: {detail}"
        )
    return completed.stdout.strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_identity(repo: Path, python_executable: str) -> dict[str, object]:
    hashes: dict[str, str] = {}
    for relative in _REQUIREMENTS:
        path = repo / relative
        if not path.is_file():
            raise ControllerError(f"controller requirement file missing: {relative}")
        hashes[relative] = _sha256_file(path)
    return {
        "schema_version": 1,
        "python": str(Path(python_executable).resolve()),
        "requirements": hashes,
    }


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ControllerError(f"controller metadata not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"controller metadata is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise ControllerError(f"controller metadata root must be an object: {path}")
    return payload


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _metadata(layout: ControllerLayout, *, repository: str, source_ref: str, commit_sha: str) -> ControllerMetadata:
    return ControllerMetadata(
        schema_version=1,
        repository=repository,
        source_ref=source_ref,
        commit_sha=commit_sha,
        runtime_branch=DEFAULT_RUNTIME_BRANCH,
        repo_path=str(layout.repo),
        venv_path=str(layout.venv),
        bin_path=str(layout.bin),
    )


def _load_metadata(layout: ControllerLayout) -> ControllerMetadata:
    payload = _read_json(layout.metadata)
    if payload.get("schema_version") != 1:
        raise ControllerError("unsupported controller metadata schema")
    required = (
        "repository",
        "source_ref",
        "commit_sha",
        "runtime_branch",
        "repo_path",
        "venv_path",
        "bin_path",
    )
    values: dict[str, str] = {}
    for key in required:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ControllerError(f"controller metadata {key} is invalid")
        values[key] = value
    metadata = ControllerMetadata(schema_version=1, **values)
    if Path(metadata.repo_path).resolve() != layout.repo:
        raise ControllerError("controller metadata repo path does not match requested layout")
    if Path(metadata.venv_path).resolve() != layout.venv:
        raise ControllerError("controller metadata venv path does not match requested layout")
    if Path(metadata.bin_path).resolve() != layout.bin:
        raise ControllerError("controller metadata bin path does not match requested layout")
    return metadata


def _assert_safe_layout(layout: ControllerLayout, source_root: Path | None) -> None:
    if layout.home == Path.home().resolve():
        raise ControllerError("controller home may not be the user's home directory itself")
    if source_root is None:
        return
    source = source_root.resolve()
    for candidate in (layout.home, layout.repo, layout.venv, layout.bin):
        try:
            candidate.relative_to(source)
        except ValueError:
            continue
        raise ControllerError(
            f"dedicated controller path must not live inside the active source checkout: {candidate}"
        )


def _controller_gate(repo: Path, expected_sha: str) -> None:
    state = inspect_repo(repo)
    config = load_project_config(repo, _MANIFEST)
    gate = run_preflight_gate(repo, config, state)
    if not gate.passed:
        failures = "; ".join(
            f"{item.name}: {item.detail}" for item in gate.checks if not item.passed
        )
        raise ControllerError(f"controller repository preflight failed: {failures}")
    if state.branch != DEFAULT_RUNTIME_BRANCH:
        raise ControllerError(
            f"controller branch mismatch: {state.branch!r} != {DEFAULT_RUNTIME_BRANCH!r}"
        )
    if state.head != expected_sha:
        raise ControllerError(f"controller HEAD mismatch: {state.head} != {expected_sha}")


def _ensure_venv(layout: ControllerLayout, *, python_executable: str, runner: CommandRunner) -> None:
    python = layout.venv / "bin" / "python"
    if python.is_file():
        return
    if layout.venv.exists():
        raise ControllerError(f"controller venv exists but is incomplete: {layout.venv}")
    layout.venv.parent.mkdir(parents=True, exist_ok=True)
    _run((python_executable, "-m", "venv", str(layout.venv)), runner=runner)
    if not python.is_file():
        raise ControllerError("controller virtualenv creation produced no bin/python")


def _install_dependencies_if_needed(
    layout: ControllerLayout,
    *,
    python_executable: str,
    runner: CommandRunner,
) -> bool:
    identity = _dependency_identity(layout.repo, python_executable)
    if layout.dependency_stamp.is_file():
        try:
            current = _read_json(layout.dependency_stamp)
        except ControllerError:
            current = {}
        if current == identity:
            return False

    venv_python = layout.venv / "bin" / "python"
    for relative in _REQUIREMENTS:
        _run(
            (str(venv_python), "-m", "pip", "install", "-r", str(layout.repo / relative)),
            runner=runner,
            timeout=600,
        )
    _atomic_json(layout.dependency_stamp, identity)
    return True


def _launcher_content(layout: ControllerLayout, name: str) -> str:
    script = layout.repo / "scripts" / name
    python = layout.venv / "bin" / "python"
    return (
        "#!/bin/sh\n"
        f"{MANAGED_LAUNCHER_MARKER}\n"
        "set -eu\n"
        f"export YOUMO_VALIDATION_VENV={shlex.quote(str(layout.venv))}\n"
        f"exec {shlex.quote(str(python))} {shlex.quote(str(script))} \"$@\"\n"
    )


def _install_launchers(layout: ControllerLayout) -> None:
    layout.bin.mkdir(parents=True, exist_ok=True)
    for name in _LAUNCHERS:
        script = layout.repo / "scripts" / name
        if not script.is_file():
            raise ControllerError(f"controller launcher source is missing: {script}")
        target = layout.bin / name
        content = _launcher_content(layout, name)
        if target.exists() and not target.is_file():
            raise ControllerError(f"launcher path exists and is not a regular file: {target}")
        if target.is_file():
            existing = target.read_text(encoding="utf-8")
            if MANAGED_LAUNCHER_MARKER not in existing:
                raise ControllerError(f"refusing to overwrite unmanaged launcher: {target}")
            if existing == content:
                os.chmod(target, 0o755)
                continue
        temp = target.with_name(target.name + f".tmp-{os.getpid()}")
        try:
            temp.write_text(content, encoding="utf-8")
            os.chmod(temp, 0o755)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)


def install_controller(
    *,
    expected_sha: str,
    source_ref: str = DEFAULT_CONTROLLER_REF,
    layout: ControllerLayout | None = None,
    source_root: Path | None = None,
    repository_url: str = DEFAULT_REPOSITORY_URL,
    clone_source_url: str | None = None,
    python_executable: str = sys.executable,
    runner: CommandRunner = subprocess.run,
) -> ControllerStatus:
    if len(expected_sha) != 40 or any(ch not in "0123456789abcdef" for ch in expected_sha.lower()):
        raise ControllerError("expected controller SHA must be a full 40-character hex commit")
    layout = layout or default_layout()
    _assert_safe_layout(layout, source_root)
    if layout.metadata.exists() or layout.repo.exists() or layout.venv.exists():
        raise ControllerError(
            "controller installation already exists or is partial; use status/refresh instead of replacing it"
        )

    layout.home.mkdir(parents=True, exist_ok=True)
    clone_url = clone_source_url or repository_url
    try:
        _run(("git", "clone", "--no-checkout", clone_url, str(layout.repo)), runner=runner, timeout=600)
        _run(("git", "fetch", "--no-tags", "origin", source_ref), cwd=layout.repo, runner=runner, timeout=600)
        fetched = _run(("git", "rev-parse", "FETCH_HEAD"), cwd=layout.repo, runner=runner)
        if fetched != expected_sha:
            raise ControllerError(
                f"controller source ref resolved to unexpected commit: {fetched} != {expected_sha}"
            )
        _run(("git", "checkout", "-B", DEFAULT_RUNTIME_BRANCH, expected_sha), cwd=layout.repo, runner=runner)
        if clone_source_url is not None:
            _run(("git", "remote", "set-url", "origin", repository_url), cwd=layout.repo, runner=runner)
        _controller_gate(layout.repo, expected_sha)
        _ensure_venv(layout, python_executable=python_executable, runner=runner)
        _install_dependencies_if_needed(
            layout, python_executable=python_executable, runner=runner
        )
        _install_launchers(layout)
        config = load_project_config(layout.repo, _MANIFEST)
        metadata = _metadata(
            layout,
            repository=config.repository,
            source_ref=source_ref,
            commit_sha=expected_sha,
        )
        _atomic_json(layout.metadata, asdict(metadata))
        return inspect_controller(layout)
    except Exception:
        if not layout.metadata.exists():
            # A failed first installation is not silently deleted. Preserve evidence for inspection.
            pass
        raise


def refresh_controller(
    *,
    expected_sha: str,
    source_ref: str = DEFAULT_CONTROLLER_REF,
    layout: ControllerLayout | None = None,
    runner: CommandRunner = subprocess.run,
) -> ControllerStatus:
    layout = layout or default_layout()
    metadata = _load_metadata(layout)
    if metadata.repository != "skybridgecx-code/faceless-youtube-backend":
        raise ControllerError(f"unexpected controller repository identity: {metadata.repository}")
    _controller_gate(layout.repo, metadata.commit_sha)

    _run(("git", "fetch", "--no-tags", "origin", source_ref), cwd=layout.repo, runner=runner, timeout=600)
    fetched = _run(("git", "rev-parse", "FETCH_HEAD"), cwd=layout.repo, runner=runner)
    if fetched != expected_sha:
        raise ControllerError(
            f"controller source ref resolved to unexpected commit: {fetched} != {expected_sha}"
        )
    if fetched != metadata.commit_sha:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", metadata.commit_sha, fetched],
            cwd=layout.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if ancestor.returncode != 0:
            raise ControllerError("controller refresh is not a fast-forward from installed commit")
        _run(("git", "merge", "--ff-only", fetched), cwd=layout.repo, runner=runner)
    _controller_gate(layout.repo, fetched)
    _install_dependencies_if_needed(
        layout, python_executable=sys.executable, runner=runner
    )
    _install_launchers(layout)
    updated = ControllerMetadata(
        schema_version=1,
        repository=metadata.repository,
        source_ref=source_ref,
        commit_sha=fetched,
        runtime_branch=metadata.runtime_branch,
        repo_path=metadata.repo_path,
        venv_path=metadata.venv_path,
        bin_path=metadata.bin_path,
    )
    _atomic_json(layout.metadata, asdict(updated))
    return inspect_controller(layout)


def inspect_controller(layout: ControllerLayout | None = None) -> ControllerStatus:
    layout = layout or default_layout()
    if not layout.metadata.is_file():
        return ControllerStatus(
            installed=False,
            ready=False,
            repo_path=str(layout.repo),
            venv_path=str(layout.venv),
            bin_path=str(layout.bin),
            head=None,
            branch=None,
            expected_head=None,
            clean=None,
            dependencies_ready=False,
            launchers_ready=False,
            detail="controller is not installed",
        )
    try:
        metadata = _load_metadata(layout)
        state = inspect_repo(layout.repo)
        config = load_project_config(layout.repo, _MANIFEST)
        gate = run_preflight_gate(layout.repo, config, state)
        expected_identity = _dependency_identity(layout.repo, sys.executable)
        deps_ready = False
        if layout.dependency_stamp.is_file() and (layout.venv / "bin" / "python").is_file():
            try:
                deps_ready = _read_json(layout.dependency_stamp) == expected_identity
            except ControllerError:
                deps_ready = False
        launchers_ready = all(
            (layout.bin / name).is_file()
            and MANAGED_LAUNCHER_MARKER in (layout.bin / name).read_text(encoding="utf-8")
            for name in _LAUNCHERS
        )
        ready = (
            gate.passed
            and state.branch == metadata.runtime_branch
            and state.head == metadata.commit_sha
            and deps_ready
            and launchers_ready
        )
        return ControllerStatus(
            installed=True,
            ready=ready,
            repo_path=str(layout.repo),
            venv_path=str(layout.venv),
            bin_path=str(layout.bin),
            head=state.head,
            branch=state.branch,
            expected_head=metadata.commit_sha,
            clean=state.clean,
            dependencies_ready=deps_ready,
            launchers_ready=launchers_ready,
            detail="controller ready" if ready else "controller installed but one or more readiness checks failed",
        )
    except Exception as exc:
        return ControllerStatus(
            installed=True,
            ready=False,
            repo_path=str(layout.repo),
            venv_path=str(layout.venv),
            bin_path=str(layout.bin),
            head=None,
            branch=None,
            expected_head=None,
            clean=None,
            dependencies_ready=False,
            launchers_ready=False,
            detail=f"controller inspection failed: {exc}",
        )
