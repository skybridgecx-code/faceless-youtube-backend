from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.project_agent_runtime import audit_cli, build_cli
from tools.project_agent_runtime.codex_transport import CodexTransportError
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.doctor import diagnose_workspace
from tools.project_agent_runtime.git_state import inspect_repo
from tools.project_agent_runtime.resume_cli import main as resume_main
from tools.project_agent_runtime.run_manifest import load_run_manifest
from tools.project_agent_runtime.workspace import initialize_executor_workspace


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"
TASK = "Exercise guarded recovery behavior"


def _git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=check, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _fixture(tmp_path: Path, *, executor_identity: bool = True):
    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-b", "tooling/recovery-control")
    _git(control, "config", "user.name", "Recovery Test")
    _git(control, "config", "user.email", "recovery@example.invalid")
    _git(control, "remote", "add", "origin", f"git@github.com:{REPOSITORY}.git")
    (control / ".gitignore").write_text(
        ".venv\n.venv/\n.pytest_cache/\n__pycache__/\n*.pyc\n",
        encoding="utf-8",
    )
    lock = {
        "schema_version": 3,
        "architecture_status": "target_during_controlled_migration",
        "current_repository": {"repository": REPOSITORY, "branch": "youmo-clone-v2"},
        "runtime_stages": ["topic", "release"],
        "hard_invariants": ["no_auto_public_release"],
        "forbidden_v1": ["dynamic_agent_swarms"],
        "migration_rules": ["evolve_existing_repository"],
        "commercial_success_contract": {"phase_requirements": {}},
    }
    (control / "architecture.lock.json").write_text(json.dumps(lock) + "\n", encoding="utf-8")
    for relative in (
        "ARCHITECTURE.md",
        "COMMERCIAL_SUCCESS.md",
        ".agents/rules/youtube-automation-project.md",
    ):
        path = control / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    (control / "app").mkdir()
    (control / "app" / "__init__.py").write_text("\n", encoding="utf-8")
    (control / "app" / "base.py").write_text("BASE = 1\n", encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "project_id": "youmo",
        "display_name": "YouMo",
        "repository": REPOSITORY,
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
    _git(control, "commit", "-m", "recovery base")

    config = load_project_config(control, manifest_path)
    base = inspect_repo(control).head
    executor = tmp_path / "executor"
    initialize_executor_workspace(
        control,
        executor,
        config,
        base_sha=base,
        branch="phase/recovery-pilot",
        _clone_source_url=str(control),
    )
    if executor_identity:
        _git(executor, "config", "user.name", "Recovery Test")
        _git(executor, "config", "user.email", "recovery@example.invalid")

    validation_venv = tmp_path / "trusted-validation-venv"
    (validation_venv / "bin").mkdir(parents=True)
    os.symlink(Path(sys.executable).resolve(), validation_venv / "bin" / "python")
    return control, executor, config, manifest_path, base, validation_venv


def _ready_sdk() -> SimpleNamespace:
    return SimpleNamespace(
        ready=True,
        installed=True,
        compatible=True,
        version="0.144.4",
        requirement="openai-codex==0.144.4",
        detail="fake SDK ready",
    )


def _value(output: str, key: str) -> str:
    prefix = key + "="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"missing {key}=... in output:\n{output}")


def _successful_build(
    control: Path,
    executor: Path,
    manifest_path: Path,
    validation_venv: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> str:
    monkeypatch.setenv("YOUMO_VALIDATION_VENV", str(validation_venv))

    async def build_turn(**kwargs: object) -> SimpleNamespace:
        (executor / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        return SimpleNamespace(
            thread_id="recovery-build-thread",
            turn_id="recovery-build-turn",
            status="completed",
            final_response="implemented",
            usage=None,
        )

    monkeypatch.setattr(build_cli, "inspect_codex_sdk", lambda _: _ready_sdk())
    monkeypatch.setattr(build_cli, "run_codex_turn", build_turn)
    rc = build_cli.main(
        [
            "--repo", str(control),
            "--project", str(manifest_path),
            "--workspace", str(executor),
            "--task", TASK,
            "--allow-path", "app",
            "--execute",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    return _value(out, "RUN_ID")


def _passing_audit_patches(monkeypatch: pytest.MonkeyPatch, executor: Path) -> None:
    async def audit_turn(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            thread_id="recovery-audit-thread",
            turn_id="recovery-audit-turn",
            status="completed",
            final_response=json.dumps(
                {"verdict": "PASS", "summary": "Recovery audit passed", "findings": []}
            ),
            usage=None,
        )

    monkeypatch.setattr(audit_cli, "inspect_codex_sdk", lambda _: _ready_sdk())
    monkeypatch.setattr(audit_cli, "run_codex_turn", audit_turn)
    monkeypatch.setattr(
        audit_cli,
        "_validation_commands",
        lambda _bound: (("python3", "-m", "py_compile", "app/feature.py"),),
    )


def test_build_transport_failure_is_terminal_and_diagnosable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, config, manifest_path, base, validation_venv = _fixture(tmp_path)
    monkeypatch.setenv("YOUMO_VALIDATION_VENV", str(validation_venv))

    async def failing_turn(**kwargs: object) -> object:
        (executor / "app" / "partial.py").write_text("PARTIAL = True\n", encoding="utf-8")
        raise CodexTransportError("synthetic transport loss")

    monkeypatch.setattr(build_cli, "inspect_codex_sdk", lambda _: _ready_sdk())
    monkeypatch.setattr(build_cli, "run_codex_turn", failing_turn)
    rc = build_cli.main(
        [
            "--repo", str(control),
            "--project", str(manifest_path),
            "--workspace", str(executor),
            "--task", TASK,
            "--allow-path", "app",
            "--execute",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 8
    run_id = _value(captured.out, "RUN_ID")
    run = load_run_manifest(control, config, run_id)
    assert run.stage == "BUILD_FAILED"
    assert "synthetic transport loss" in (run.last_error or "")
    assert _git(executor, "rev-parse", "HEAD") == base
    assert (executor / "app" / "partial.py").is_file()
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "BUILD_FAILED"
    assert "never auto-promoted" in diagnosis.next_action


def test_operational_audit_failure_rolls_back_to_ready_for_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, config, manifest_path, _, validation_venv = _fixture(tmp_path)
    run_id = _successful_build(
        control, executor, manifest_path, validation_venv, monkeypatch, capsys
    )

    async def failing_audit(**kwargs: object) -> object:
        raise CodexTransportError("synthetic audit transport loss")

    monkeypatch.setattr(audit_cli, "inspect_codex_sdk", lambda _: _ready_sdk())
    monkeypatch.setattr(audit_cli, "run_codex_turn", failing_audit)
    monkeypatch.setattr(
        audit_cli,
        "_validation_commands",
        lambda _bound: (("python3", "-m", "py_compile", "app/feature.py"),),
    )
    rc = resume_main(
        [
            "--repo", str(control),
            "--project", str(manifest_path),
            "--run-id", run_id,
            "--execute",
        ]
    )
    capsys.readouterr()
    assert rc == 9
    run = load_run_manifest(control, config, run_id)
    assert run.stage == "READY_FOR_AUDIT"
    assert "synthetic audit transport loss" in (run.last_error or "")
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "READY_FOR_AUDIT"


def test_semantic_audit_fail_is_terminal_not_checkpointable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, config, manifest_path, _, validation_venv = _fixture(tmp_path)
    run_id = _successful_build(
        control, executor, manifest_path, validation_venv, monkeypatch, capsys
    )

    async def failing_verdict(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            thread_id="semantic-fail-thread",
            turn_id="semantic-fail-turn",
            status="completed",
            final_response=json.dumps(
                {
                    "verdict": "FAIL",
                    "summary": "Intentional semantic failure",
                    "findings": [
                        {"severity": "high", "message": "Synthetic defect", "path": "app/feature.py"}
                    ],
                }
            ),
            usage=None,
        )

    monkeypatch.setattr(audit_cli, "inspect_codex_sdk", lambda _: _ready_sdk())
    monkeypatch.setattr(audit_cli, "run_codex_turn", failing_verdict)
    monkeypatch.setattr(
        audit_cli,
        "_validation_commands",
        lambda _bound: (("python3", "-m", "py_compile", "app/feature.py"),),
    )
    rc = resume_main(
        [
            "--repo", str(control),
            "--project", str(manifest_path),
            "--run-id", run_id,
            "--execute",
        ]
    )
    capsys.readouterr()
    assert rc == 9
    run = load_run_manifest(control, config, run_id)
    assert run.stage == "AUDIT_FAILED"
    assert run.audit_evidence is not None
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "AUDIT_FAILED"

    retry = resume_main(
        [
            "--repo", str(control),
            "--project", str(manifest_path),
            "--run-id", run_id,
        ]
    )
    retry_capture = capsys.readouterr()
    assert retry == 11
    assert "not safe for automatic resume" in retry_capture.err


def test_checkpoint_failure_is_retryable_after_local_identity_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, config, manifest_path, base, validation_venv = _fixture(
        tmp_path, executor_identity=False
    )
    run_id = _successful_build(
        control, executor, manifest_path, validation_venv, monkeypatch, capsys
    )
    _passing_audit_patches(monkeypatch, executor)
    audit_rc = resume_main(
        [
            "--repo", str(control),
            "--project", str(manifest_path),
            "--run-id", run_id,
            "--execute",
        ]
    )
    capsys.readouterr()
    assert audit_rc == 0
    assert load_run_manifest(control, config, run_id).stage == "READY_FOR_CHECKPOINT"

    checkpoint_rc = resume_main(
        [
            "--repo", str(control),
            "--project", str(manifest_path),
            "--run-id", run_id,
            "--execute",
        ]
    )
    capsys.readouterr()
    assert checkpoint_rc == 10
    failed = load_run_manifest(control, config, run_id)
    assert failed.stage == "CHECKPOINT_FAILED"
    assert "user.name and user.email" in (failed.last_error or "")
    assert _git(executor, "rev-parse", "HEAD") == base
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "CHECKPOINT_RETRYABLE"

    _git(executor, "config", "user.name", "Recovery Test")
    _git(executor, "config", "user.email", "recovery@example.invalid")
    retry_rc = resume_main(
        [
            "--repo", str(control),
            "--project", str(manifest_path),
            "--run-id", run_id,
            "--execute",
        ]
    )
    retry_output = capsys.readouterr().out
    assert retry_rc == 0, retry_output
    final = load_run_manifest(control, config, run_id)
    assert final.stage == "CHECKPOINTED"
    assert final.checkpoint_commit == _git(executor, "rev-parse", "HEAD")
    assert _git(executor, "status", "--porcelain=v1", "-uall") == ""
