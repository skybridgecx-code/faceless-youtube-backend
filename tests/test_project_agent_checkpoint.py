from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.project_agent_runtime.build_engine import (
    architecture_fingerprint,
    changed_files,
    diff_fingerprint,
)
from tools.project_agent_runtime.checkpoint_engine import (
    CheckpointError,
    create_checkpoint,
    load_audit_evidence,
)
from tools.project_agent_runtime.codex_transport import CodexTransportError, run_codex_turn
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.git_state import inspect_repo
from tools.project_agent_runtime.workspace import (
    initialize_executor_workspace,
    verify_executor_workspace,
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _fixture(tmp_path: Path, *, configure_executor_identity: bool = True):
    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-b", "tooling/test")
    _git(control, "config", "user.name", "Runtime Test")
    _git(control, "config", "user.email", "runtime@example.invalid")
    _git(
        control, "remote", "add", "origin",
        "git@github.com:skybridgecx-code/faceless-youtube-backend.git",
    )
    (control / ".gitignore").write_text(
        ".env\n.pytest_cache/\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    lock = {
        "schema_version": 3,
        "architecture_status": "target_during_controlled_migration",
        "current_repository": {
            "repository": "skybridgecx-code/faceless-youtube-backend",
            "branch": "youmo-clone-v2",
        },
        "runtime_stages": ["topic", "release"],
        "hard_invariants": ["no_auto_public_release"],
        "forbidden_v1": ["dynamic_agent_swarms"],
        "migration_rules": ["evolve_existing_repository"],
        "commercial_success_contract": {"phase_requirements": {}},
    }
    (control / "architecture.lock.json").write_text(
        json.dumps(lock) + "\n", encoding="utf-8"
    )
    for relative in (
        "ARCHITECTURE.md",
        "COMMERCIAL_SUCCESS.md",
        ".agents/rules/youtube-automation-project.md",
    ):
        path = control / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    (control / "app").mkdir()
    (control / "app" / "base.py").write_text("BASE=1\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "project_id": "youmo",
        "display_name": "YouMo",
        "repository": "skybridgecx-code/faceless-youtube-backend",
        "canonical_branch": "youmo-clone-v2",
        "allowed_branch_prefixes": ["phase/", "fix/", "audit/", "tooling/"],
        "execution_branch_prefixes": ["phase/", "fix/", "tooling/", "agent/"],
        "architecture_lock": "architecture.lock.json",
        "architecture_sources": [
            "ARCHITECTURE.md",
            "COMMERCIAL_SUCCESS.md",
            ".agents/rules/youtube-automation-project.md",
        ],
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
    _git(control, "add", ".")
    _git(control, "commit", "-m", "base")

    config = load_project_config(control, manifest_path)
    base = inspect_repo(control).head
    executor = tmp_path / "executor"
    initialize_executor_workspace(
        control,
        executor,
        config,
        base_sha=base,
        branch="phase/checkpoint-test",
        _clone_source_url=str(control),
    )
    if configure_executor_identity:
        _git(executor, "config", "user.name", "Runtime Test")
        _git(executor, "config", "user.email", "runtime@example.invalid")
    expected_arch = architecture_fingerprint(
        control, (config.architecture_lock, *config.architecture_sources)
    )
    return control, executor, config, base, expected_arch


def _evidence(executor: Path, task: str = "Implement checkpoint feature"):
    (executor / "app" / "feature.py").write_text("VALUE=2\n", encoding="utf-8")
    files = changed_files(executor)
    diff_sha, fingerprints = diff_fingerprint(executor, files)
    task_sha = hashlib.sha256(task.encode("utf-8")).hexdigest()
    base = _git(executor, "rev-parse", "HEAD")
    branch = _git(executor, "branch", "--show-current")
    build = {
        "status": "READY_FOR_AUDIT",
        "ready_for_audit": True,
        "base_head": base,
        "branch": branch,
        "task_sha256": task_sha,
        "allowed_paths": ["app"],
        "changed_files": list(files),
        "diff_sha256": diff_sha,
        "file_fingerprints": fingerprints,
        "violations": [],
    }
    audit = {
        "status": "AUDIT_PASS",
        "passed": True,
        "verdict": "PASS",
        "base_head": base,
        "branch": branch,
        "task_sha256": task_sha,
        "diff_sha256": diff_sha,
        "violations": [],
    }
    return build, audit


def test_checkpoint_creates_one_bound_commit_and_advances_workspace(tmp_path: Path) -> None:
    control, executor, config, base, expected_arch = _fixture(tmp_path)
    task = "Implement checkpoint feature"
    build, audit = _evidence(executor, task)
    cache = executor / ".pytest_cache" / "state"
    cache.parent.mkdir(parents=True)
    cache.write_text("cache\n", encoding="utf-8")

    result = create_checkpoint(
        control_root=control,
        workspace_root=executor,
        config=config,
        task=task,
        build_evidence=build,
        audit_evidence=audit,
        expected_architecture=expected_arch,
        build_evidence_sha256="b" * 64,
        audit_evidence_sha256="a" * 64,
        subject="Checkpoint test",
    )

    assert result.status == "CHECKPOINTED"
    assert result.parent_sha == base
    assert result.changed_files == ("app/feature.py",)
    assert ".pytest_cache/state" in result.removed_ignored_artifacts
    assert _git(executor, "rev-parse", "HEAD") == result.commit_sha
    assert _git(executor, "rev-parse", f"{result.commit_sha}^") == base
    assert _git(executor, "status", "--porcelain=v1") == ""
    message = _git(executor, "show", "-s", "--format=%B", result.commit_sha)
    assert "YouMo-Build-Evidence-SHA256: " + ("b" * 64) in message
    assert "YouMo-Audit-Evidence-SHA256: " + ("a" * 64) in message
    report = verify_executor_workspace(
        control, executor, config, require_clean=True, require_registry=True
    )
    assert report.passed
    assert report.marker is not None
    assert report.marker.base_sha == result.commit_sha


def test_checkpoint_rejects_build_audit_mismatch(tmp_path: Path) -> None:
    control, executor, config, _, expected_arch = _fixture(tmp_path)
    build, audit = _evidence(executor)
    audit["diff_sha256"] = "0" * 64
    with pytest.raises(CheckpointError, match="mismatch for diff_sha256"):
        create_checkpoint(
            control_root=control,
            workspace_root=executor,
            config=config,
            task="Implement checkpoint feature",
            build_evidence=build,
            audit_evidence=audit,
            expected_architecture=expected_arch,
            build_evidence_sha256="b" * 64,
            audit_evidence_sha256="a" * 64,
            subject="Checkpoint test",
        )


def test_checkpoint_missing_git_identity_fails_without_moving_head(tmp_path: Path) -> None:
    control, executor, config, base, expected_arch = _fixture(
        tmp_path, configure_executor_identity=False
    )
    build, audit = _evidence(executor)
    with pytest.raises(CheckpointError, match="user.name and user.email"):
        create_checkpoint(
            control_root=control,
            workspace_root=executor,
            config=config,
            task="Implement checkpoint feature",
            build_evidence=build,
            audit_evidence=audit,
            expected_architecture=expected_arch,
            build_evidence_sha256="b" * 64,
            audit_evidence_sha256="a" * 64,
            subject="Checkpoint test",
        )
    assert _git(executor, "rev-parse", "HEAD") == base
    assert _git(executor, "diff", "--cached", "--name-only") == ""
    assert (executor / "app" / "feature.py").is_file()


def test_load_audit_evidence_requires_pass(tmp_path: Path) -> None:
    path = tmp_path / "audit.json"
    path.write_text(
        json.dumps(
            {
                "status": "AUDIT_FAIL",
                "passed": False,
                "verdict": "FAIL",
                "base_head": "x",
                "branch": "phase/x",
                "task_sha256": "t",
                "diff_sha256": "d",
                "violations": ["bad"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CheckpointError, match="not AUDIT_PASS"):
        load_audit_evidence(path)


class _HiddenThread:
    def __init__(self, root: Path) -> None:
        self.id = "thr_hidden"
        self.root = root

    async def run(self, prompt: str, **kwargs: object) -> object:
        (self.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        return SimpleNamespace(
            id="turn_hidden",
            status=SimpleNamespace(value="completed"),
            final_response="done",
            usage=None,
        )


class _HiddenCodex:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def thread_start(self, **kwargs: object) -> _HiddenThread:
        return _HiddenThread(Path(str(kwargs["cwd"])))

    async def thread_resume(self, thread_id: str, **kwargs: object) -> _HiddenThread:
        return _HiddenThread(Path(str(kwargs["cwd"])))


class _HiddenSdk:
    AsyncCodex = _HiddenCodex
    Sandbox = SimpleNamespace(read_only="READ", workspace_write="WRITE")


def test_workspace_write_transport_rejects_hidden_ignored_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "phase/test")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "tracked.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    with pytest.raises(CodexTransportError, match="hidden/ignored artifacts"):
        asyncio.run(
            run_codex_turn(
                repo_root=root,
                prompt="write",
                developer_instructions="rules",
                model="gpt-5.6-terra",
                reasoning="high",
                sandbox_name="workspace_write",
                sdk=_HiddenSdk,
            )
        )
    assert (root / ".env").is_file()


def test_workspace_write_transport_rejects_preexisting_ignored_artifact(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "phase/test")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "tracked.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

    with pytest.raises(CodexTransportError, match="before workspace-write"):
        asyncio.run(
            run_codex_turn(
                repo_root=root,
                prompt="write",
                developer_instructions="rules",
                model="gpt-5.6-terra",
                reasoning="high",
                sandbox_name="workspace_write",
                sdk=_HiddenSdk,
            )
        )
