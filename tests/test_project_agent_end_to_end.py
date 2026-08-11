from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.project_agent_runtime import audit_cli, build_cli
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.doctor import diagnose_workspace
from tools.project_agent_runtime.git_state import inspect_repo
from tools.project_agent_runtime.resume_cli import main as resume_main
from tools.project_agent_runtime.run_manifest import load_run_manifest, resolve_evidence, run_directory
from tools.project_agent_runtime.workspace import initialize_executor_workspace


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"
TASK = "Add a small isolated pilot feature"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _fixture(tmp_path: Path):
    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-b", "tooling/pilot-control")
    _git(control, "config", "user.name", "Pilot Test")
    _git(control, "config", "user.email", "pilot@example.invalid")
    _git(control, "remote", "add", "origin", f"git@github.com:{REPOSITORY}.git")
    (control / ".gitignore").write_text(
        ".venv\n.venv/\n.pytest_cache/\n__pycache__/\n*.pyc\n",
        encoding="utf-8",
    )

    lock = {
        "schema_version": 3,
        "architecture_status": "target_during_controlled_migration",
        "current_repository": {
            "repository": REPOSITORY,
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
    _git(control, "commit", "-m", "pilot base")

    config = load_project_config(control, manifest_path)
    base = inspect_repo(control).head
    executor = tmp_path / "executor"
    initialize_executor_workspace(
        control,
        executor,
        config,
        base_sha=base,
        branch="phase/simulated-pilot",
        _clone_source_url=str(control),
    )
    _git(executor, "config", "user.name", "Pilot Test")
    _git(executor, "config", "user.email", "pilot@example.invalid")

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
        detail="fake SDK ready for simulation",
    )


def _value(output: str, key: str) -> str:
    prefix = key + "="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"missing {key}=... in output:\n{output}")


def test_simulated_cli_pilot_build_audit_checkpoint_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, config, manifest_path, base, validation_venv = _fixture(tmp_path)
    control_head_before = _git(control, "rev-parse", "HEAD")
    control_status_before = _git(control, "status", "--porcelain=v1", "-uall")
    monkeypatch.setenv("YOUMO_VALIDATION_VENV", str(validation_venv))

    async def fake_build_turn(**kwargs: object) -> SimpleNamespace:
        assert kwargs["sandbox_name"] == "workspace_write"
        assert Path(str(kwargs["repo_root"])).resolve() == executor.resolve()
        (executor / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        return SimpleNamespace(
            thread_id="sim-build-thread",
            turn_id="sim-build-turn",
            status="completed",
            final_response="implemented",
            usage=None,
        )

    monkeypatch.setattr(build_cli, "inspect_codex_sdk", lambda _: _ready_sdk())
    monkeypatch.setattr(build_cli, "run_codex_turn", fake_build_turn)

    build_rc = build_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--workspace",
            str(executor),
            "--task",
            TASK,
            "--allow-path",
            "app",
            "--max-changed-files",
            "5",
            "--execute",
        ]
    )
    build_output = capsys.readouterr().out
    assert build_rc == 0, build_output
    run_id = _value(build_output, "RUN_ID")
    assert _value(build_output, "RUN_STAGE") == "READY_FOR_AUDIT"
    assert _value(build_output, "NEXT_REQUIRED_GATE") == "FULL_AUDIT"
    assert (executor / "app" / "feature.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert _git(executor, "rev-parse", "HEAD") == base
    assert _git(executor, "diff", "--cached", "--name-only") == ""

    manifest = load_run_manifest(control, config, run_id)
    assert manifest.stage == "READY_FOR_AUDIT"
    assert manifest.build_evidence is not None
    build_path = resolve_evidence(
        manifest.build_evidence,
        expected_parent=run_directory(control, config, run_id),
    )
    assert build_path.name == "build.json"

    dry_resume_rc = resume_main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run_id,
        ]
    )
    dry_resume_output = capsys.readouterr().out
    assert dry_resume_rc == 0
    assert "NEXT_GATE=AUDIT" in dry_resume_output
    assert "MUTATIONS=NONE" in dry_resume_output

    async def fake_audit_turn(**kwargs: object) -> SimpleNamespace:
        assert kwargs["sandbox_name"] == "read_only"
        assert Path(str(kwargs["repo_root"])).resolve() == executor.resolve()
        return SimpleNamespace(
            thread_id="sim-audit-thread",
            turn_id="sim-audit-turn",
            status="completed",
            final_response=json.dumps(
                {"verdict": "PASS", "summary": "Simulated audit passed", "findings": []}
            ),
            usage=None,
        )

    monkeypatch.setattr(audit_cli, "inspect_codex_sdk", lambda _: _ready_sdk())
    monkeypatch.setattr(audit_cli, "run_codex_turn", fake_audit_turn)
    monkeypatch.setattr(
        audit_cli,
        "_validation_commands",
        lambda _bound: (("python3", "-m", "py_compile", "app/feature.py"),),
    )

    audit_rc = resume_main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run_id,
            "--execute",
        ]
    )
    audit_output = capsys.readouterr().out
    assert audit_rc == 0, audit_output
    assert "RUN_STAGE=READY_FOR_CHECKPOINT" in audit_output
    assert "VERDICT=PASS" in audit_output

    manifest = load_run_manifest(control, config, run_id)
    assert manifest.stage == "READY_FOR_CHECKPOINT"
    assert manifest.audit_evidence is not None
    audit_path = resolve_evidence(
        manifest.audit_evidence,
        expected_parent=run_directory(control, config, run_id),
    )
    assert audit_path.name == "audit.json"

    checkpoint_dry_rc = resume_main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run_id,
        ]
    )
    checkpoint_dry_output = capsys.readouterr().out
    assert checkpoint_dry_rc == 0
    assert "NEXT_GATE=CHECKPOINT" in checkpoint_dry_output
    assert "MUTATIONS=NONE" in checkpoint_dry_output

    checkpoint_rc = resume_main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run_id,
            "--subject",
            "Simulated pilot checkpoint",
            "--execute",
        ]
    )
    checkpoint_output = capsys.readouterr().out
    assert checkpoint_rc == 0, checkpoint_output
    commit_sha = _value(checkpoint_output, "COMMIT_SHA")
    assert _value(checkpoint_output, "RUN_STAGE") == "CHECKPOINTED"
    assert _git(executor, "rev-parse", "HEAD") == commit_sha
    assert _git(executor, "rev-parse", f"{commit_sha}^") == base
    assert _git(executor, "status", "--porcelain=v1", "-uall") == ""
    assert _git(executor, "show", "--format=", "--name-only", commit_sha).strip() == "app/feature.py"

    manifest = load_run_manifest(control, config, run_id)
    assert manifest.stage == "CHECKPOINTED"
    assert manifest.checkpoint_commit == commit_sha
    assert manifest.checkpoint_evidence is not None
    checkpoint_path = resolve_evidence(
        manifest.checkpoint_evidence,
        expected_parent=run_directory(control, config, run_id),
    )
    assert checkpoint_path.name == "checkpoint.json"

    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "CHECKPOINTED_CLEAN"
    assert diagnosis.run_id == run_id
    assert "next guarded build" in diagnosis.next_action

    final_resume_rc = resume_main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run_id,
            "--execute",
        ]
    )
    final_resume_output = capsys.readouterr().out
    assert final_resume_rc == 0
    assert "NEXT_GATE=NONE" in final_resume_output
    assert "GIT_PUSH=NOT_STARTED" in final_resume_output

    assert _git(control, "rev-parse", "HEAD") == control_head_before
    assert _git(control, "status", "--porcelain=v1", "-uall") == control_status_before
