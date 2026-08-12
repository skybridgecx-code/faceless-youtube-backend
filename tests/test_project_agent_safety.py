from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.operation_lease import (
    OperationLeaseError,
    acquire_operation_lease,
    release_operation_lease,
)
from tools.project_agent_runtime.safe_build import run_fast_guarded_build
from tools.project_agent_runtime.validation_env import (
    ValidationEnvironmentError,
    bound_workspace_validation_venv,
)
from tools.project_agent_runtime.validation_policy import targeted_build_validation_commands


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _control_fixture(tmp_path: Path):
    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-b", "tooling/test")
    _git(control, "config", "user.name", "Runtime Test")
    _git(control, "config", "user.email", "runtime@example.invalid")
    _git(
        control,
        "remote",
        "add",
        "origin",
        "git@github.com:skybridgecx-code/faceless-youtube-backend.git",
    )
    manifest = {
        "schema_version": 1,
        "project_id": "youmo",
        "display_name": "YouMo",
        "repository": "skybridgecx-code/faceless-youtube-backend",
        "canonical_branch": "youmo-clone-v2",
        "allowed_branch_prefixes": ["tooling/", "phase/", "fix/", "audit/"],
        "execution_branch_prefixes": ["tooling/", "phase/", "fix/", "agent/"],
        "architecture_lock": "architecture.lock.json",
        "architecture_sources": [],
        "state_dir": str(tmp_path / "state"),
        "codex": {
            "sdk_requirement": "openai-codex==0.144.4",
            "implementation_model": "gpt-5.6-terra",
            "implementation_reasoning": "high",
            "audit_model": "gpt-5.6-sol",
            "audit_reasoning": "high",
            "require_explicit_execute": True,
        },
    }
    manifest_path = control / "youmo.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    config = load_project_config(control, manifest_path)
    return control, config


def test_operation_lease_blocks_same_executor_and_releases(tmp_path: Path) -> None:
    control, config = _control_fixture(tmp_path)
    workspace = tmp_path / "executor"
    workspace.mkdir()

    path, first = acquire_operation_lease(control, config, workspace, "build")
    with pytest.raises(OperationLeaseError, match="already leased"):
        acquire_operation_lease(control, config, workspace, "audit")

    release_operation_lease(path, first)
    path2, second = acquire_operation_lease(control, config, workspace, "checkpoint")
    assert second.operation == "checkpoint"
    release_operation_lease(path2, second)
    assert not path.exists()


def test_operation_leases_are_scoped_per_executor(tmp_path: Path) -> None:
    control, config = _control_fixture(tmp_path)
    first_workspace = tmp_path / "executor-a"
    second_workspace = tmp_path / "executor-b"
    first_workspace.mkdir()
    second_workspace.mkdir()

    path_a, lease_a = acquire_operation_lease(control, config, first_workspace, "build")
    path_b, lease_b = acquire_operation_lease(control, config, second_workspace, "build")
    assert path_a != path_b
    release_operation_lease(path_a, lease_a)
    release_operation_lease(path_b, lease_b)


def test_targeted_validation_uses_direct_tests_and_syntax_checks(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_worker.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (root / "app" / "ui.js").write_text("const x = 1;\n", encoding="utf-8")

    commands = targeted_build_validation_commands(
        root, ("app/worker.py", "tests/test_worker.py", "app/ui.js")
    )
    assert commands[0] == ("git", "diff", "--check")
    assert ("python3", "-m", "py_compile", "app/worker.py", "tests/test_worker.py") in commands
    assert ("python3", "-m", "pytest", "-q", "tests/test_worker.py") in commands
    assert ("node", "--check", "app/ui.js") in commands


def test_validation_venv_binding_is_temporary_and_external(tmp_path: Path) -> None:
    workspace = tmp_path / "executor"
    workspace.mkdir()
    venv = tmp_path / "trusted-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("python\n", encoding="utf-8")

    with bound_workspace_validation_venv(workspace, venv) as bound:
        assert bound
        link = workspace / ".venv"
        assert link.is_symlink()
        assert link.resolve() == venv.resolve()
    assert not (workspace / ".venv").exists()
    assert not (workspace / ".venv").is_symlink()


def test_validation_venv_never_replaces_workspace_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "executor"
    workspace.mkdir()
    existing = workspace / ".venv"
    existing.mkdir()
    marker = existing / "KEEP"
    marker.write_text("keep\n", encoding="utf-8")
    trusted = tmp_path / "trusted"
    (trusted / "bin").mkdir(parents=True)
    (trusted / "bin" / "python").write_text("python\n", encoding="utf-8")

    with pytest.raises(ValidationEnvironmentError, match="will not replace"):
        with bound_workspace_validation_venv(workspace, trusted):
            pass
    assert marker.read_text(encoding="utf-8") == "keep\n"


class _FakeTurn:
    def __init__(self, root: Path, content: str) -> None:
        self.root = root
        self.content = content

    async def __call__(self, **kwargs: object) -> object:
        target = self.root / "app" / "feature.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.content, encoding="utf-8")
        return SimpleNamespace(
            thread_id="thread-1",
            turn_id="turn-1",
            status="completed",
            final_response="done",
        )


def _build_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "phase/safety")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    (root / "architecture.lock.json").write_text("{}\n", encoding="utf-8")
    (root / "app").mkdir()
    (root / "app" / "base.py").write_text("BASE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def test_fast_build_passes_targeted_validation_without_full_suite(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    result = asyncio.run(
        run_fast_guarded_build(
            workspace_root=root,
            task="add feature",
            allowed_paths=("app",),
            architecture_paths=("architecture.lock.json",),
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            turn_runner=_FakeTurn(root, "VALUE = 2\n"),
            max_changed_files=5,
            validation_venv=None,
        )
    )
    assert result.ready_for_audit
    argv = [item.argv for item in result.validations]
    assert ("git", "diff", "--check") in argv
    assert ("python3", "-m", "py_compile", "app/feature.py") in argv
    assert not any("pytest" in command for command in argv)


def test_fast_build_fails_on_python_syntax_error(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    result = asyncio.run(
        run_fast_guarded_build(
            workspace_root=root,
            task="add broken feature",
            allowed_paths=("app",),
            architecture_paths=("architecture.lock.json",),
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            turn_runner=_FakeTurn(root, "def broken(:\n"),
            max_changed_files=5,
            validation_venv=None,
        )
    )
    assert result.status == "FAILED"
    assert any("targeted validation failed" in item for item in result.violations)
