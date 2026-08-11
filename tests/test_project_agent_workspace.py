from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.git_state import inspect_repo
from tools.project_agent_runtime.workspace import (
    WorkspaceError,
    initialize_executor_workspace,
    verify_executor_workspace,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "tooling/test")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    _git(root, "remote", "add", "origin", "git@github.com:skybridgecx-code/faceless-youtube-backend.git")
    lock = {
        "schema_version": 3,
        "architecture_status": "target_during_controlled_migration",
        "current_repository": {
            "repository": "skybridgecx-code/faceless-youtube-backend",
            "branch": "youmo-clone-v2",
        },
        "runtime_stages": ["topic", "research", "release"],
        "hard_invariants": ["no_auto_public_release"],
        "forbidden_v1": ["dynamic_agent_swarms"],
        "migration_rules": ["evolve_existing_repository"],
        "commercial_success_contract": {"phase_requirements": {}},
    }
    (root / "architecture.lock.json").write_text(json.dumps(lock) + "\n", encoding="utf-8")
    for relative in ("ARCHITECTURE.md", "COMMERCIAL_SUCCESS.md", ".agents/rules/youtube-automation-project.md"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "project_id": "youmo",
        "display_name": "YouMo",
        "repository": "skybridgecx-code/faceless-youtube-backend",
        "canonical_branch": "youmo-clone-v2",
        "allowed_branch_prefixes": ["phase/", "fix/", "audit/", "tooling/"],
        "execution_branch_prefixes": ["phase/", "fix/", "tooling/", "agent/"],
        "architecture_lock": "architecture.lock.json",
        "architecture_sources": ["ARCHITECTURE.md", "COMMERCIAL_SUCCESS.md", ".agents/rules/youtube-automation-project.md"],
        "state_dir": ".youmo",
        "codex": {
            "sdk_requirement": "openai-codex==0.144.4",
            "implementation_model": "gpt-5.6-terra",
            "implementation_reasoning": "high",
            "audit_model": "gpt-5.6-sol",
            "audit_reasoning": "high",
            "require_explicit_execute": True,
        },
    }
    manifest_path = root / "youmo.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root, manifest_path


def _init_executor(tmp_path: Path) -> tuple[Path, Path, object, object]:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    state = inspect_repo(root)
    executor = tmp_path / "executor"
    report = initialize_executor_workspace(
        root,
        executor,
        config,
        base_sha=state.head,
        branch="phase/runtime-test",
        _clone_source_url=str(root),
    )
    return root, executor, config, report


def test_executor_workspace_is_physically_independent(tmp_path: Path) -> None:
    root, executor, config, report = _init_executor(tmp_path)
    assert report.passed
    assert (executor / ".git").is_dir()
    assert not (executor / ".git" / "objects" / "info" / "alternates").exists()
    assert report.marker is not None
    assert report.state is not None
    assert report.marker.base_sha == inspect_repo(root).head
    assert report.state.normalized_origin == config.repository


def test_executor_workspace_rejects_canonical_branch(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    try:
        initialize_executor_workspace(
            root,
            tmp_path / "executor",
            config,
            base_sha=inspect_repo(root).head,
            branch=config.canonical_branch,
            _clone_source_url=str(root),
        )
    except WorkspaceError as exc:
        assert "non-canonical" in str(exc)
    else:
        raise AssertionError("canonical branch must never be a write executor")


def test_executor_workspace_rejects_shared_git_worktree(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    shared = tmp_path / "shared-worktree"
    _git(root, "worktree", "add", "-b", "phase/shared-test", str(shared))
    report = verify_executor_workspace(root, shared, config, require_clean=True)
    checks = {check.name: check for check in report.checks}
    assert not report.passed
    assert checks["private_git_directory"].passed is False


def test_executor_workspace_rejects_object_alternates(tmp_path: Path) -> None:
    root, executor, config, _ = _init_executor(tmp_path)
    alternates = executor / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(str(root / ".git" / "objects") + "\n", encoding="utf-8")
    report = verify_executor_workspace(root, executor, config, require_clean=True)
    checks = {check.name: check for check in report.checks}
    assert not report.passed
    assert checks["no_object_alternates"].passed is False


def test_executor_workspace_marker_binds_base_head(tmp_path: Path) -> None:
    root, executor, config, _ = _init_executor(tmp_path)
    _git(executor, "config", "user.name", "Runtime Test")
    _git(executor, "config", "user.email", "runtime@example.invalid")
    _git(executor, "commit", "--allow-empty", "-m", "unexpected head movement")
    report = verify_executor_workspace(root, executor, config, require_clean=True)
    checks = {check.name: check for check in report.checks}
    assert not report.passed
    assert checks["base_head_identity"].passed is False


def test_executor_workspace_rejects_nested_destination(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    try:
        initialize_executor_workspace(
            root,
            root / ".youmo" / "executor",
            config,
            base_sha=inspect_repo(root).head,
            branch="phase/runtime-test",
            _clone_source_url=str(root),
        )
    except WorkspaceError as exc:
        assert "outside the control repository tree" in str(exc)
    else:
        raise AssertionError("executor clone inside control checkout must be rejected")


def test_executor_workspace_requires_controller_registry_binding(tmp_path: Path) -> None:
    root, executor, config, _ = _init_executor(tmp_path)
    registry = root / config.state_dir / "executor-workspaces.json"
    assert registry.is_file()
    registry.unlink()
    report = verify_executor_workspace(root, executor, config, require_clean=True)
    checks = {check.name: check for check in report.checks}
    assert not report.passed
    assert checks["control_registry_binding"].passed is False
