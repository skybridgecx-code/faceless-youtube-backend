from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools.project_agent_runtime.architecture import load_architecture_snapshot
from tools.project_agent_runtime.build_engine import diff_fingerprint, changed_files
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.doctor import diagnose_workspace
from tools.project_agent_runtime.git_state import inspect_repo
from tools.project_agent_runtime.resume_cli import main as resume_main
from tools.project_agent_runtime.run_manifest import (
    RunManifestError,
    create_run_manifest,
    load_run_manifest,
    resolve_evidence,
    run_directory,
    transition_run,
    write_run_evidence,
)
from tools.project_agent_runtime.workspace import initialize_executor_workspace


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _fixture(tmp_path: Path):
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
        f"git@github.com:{REPOSITORY}.git",
    )
    (control / ".gitignore").write_text(
        ".pytest_cache/\n__pycache__/\n*.pyc\n", encoding="utf-8"
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

    manifest_payload = {
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
    manifest_path.write_text(json.dumps(manifest_payload) + "\n", encoding="utf-8")
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
        branch="phase/run-test",
        _clone_source_url=str(control),
    )
    return control, executor, config, manifest_path, base


def _ready_run(tmp_path: Path):
    control, executor, config, manifest_path, base = _fixture(tmp_path)
    task = "Implement immutable run feature"
    feature = executor / "app" / "feature.py"
    feature.write_text("VALUE = 2\n", encoding="utf-8")
    files = changed_files(executor)
    diff_sha, fingerprints = diff_fingerprint(executor, files)
    task_sha = hashlib.sha256(task.encode("utf-8")).hexdigest()
    build = {
        "status": "READY_FOR_AUDIT",
        "ready_for_audit": True,
        "base_head": base,
        "branch": "phase/run-test",
        "task_sha256": task_sha,
        "allowed_paths": ["app"],
        "changed_files": list(files),
        "diff_sha256": diff_sha,
        "file_fingerprints": fingerprints,
        "validations": [],
        "violations": [],
    }
    architecture = load_architecture_snapshot(control, config)
    run = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/run-test",
        base_head=base,
        task=task,
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    binding = write_run_evidence(
        control,
        config,
        run,
        stage_name="build",
        payload=json.dumps(build, indent=2, sort_keys=True) + "\n",
    )
    run = transition_run(
        control,
        config,
        run,
        new_stage="READY_FOR_AUDIT",
        build_evidence=binding,
    )
    return control, executor, config, manifest_path, run, build


def _advance_to_checkpoint(control, executor, config, run, build):
    audit = {
        "status": "AUDIT_PASS",
        "passed": True,
        "verdict": "PASS",
        "summary": "clean",
        "findings": [],
        "base_head": build["base_head"],
        "branch": build["branch"],
        "task_sha256": build["task_sha256"],
        "diff_sha256": build["diff_sha256"],
        "validations": [],
        "violations": [],
    }
    running = transition_run(control, config, run, new_stage="AUDIT_RUNNING")
    binding = write_run_evidence(
        control,
        config,
        running,
        stage_name="audit",
        payload=json.dumps(audit, indent=2, sort_keys=True) + "\n",
    )
    return transition_run(
        control,
        config,
        running,
        new_stage="READY_FOR_CHECKPOINT",
        audit_evidence=binding,
    )


def test_run_evidence_is_immutable_and_hash_bound(tmp_path: Path) -> None:
    control, _, config, _, run, _ = _ready_run(tmp_path)
    assert run.build_evidence is not None
    parent = run_directory(control, config, run.run_id)
    path = resolve_evidence(run.build_evidence, expected_parent=parent)

    with pytest.raises(RunManifestError, match="refusing to overwrite immutable build evidence"):
        write_run_evidence(control, config, run, stage_name="build", payload="{}\n")

    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RunManifestError, match="hash mismatch"):
        resolve_evidence(run.build_evidence, expected_parent=parent)


def test_run_stage_transition_is_fail_closed(tmp_path: Path) -> None:
    control, _, config, _, run, _ = _ready_run(tmp_path)
    with pytest.raises(RunManifestError, match="invalid run stage transition"):
        transition_run(control, config, run, new_stage="CHECKPOINTED")
    observed = load_run_manifest(control, config, run.run_id)
    assert observed.stage == "READY_FOR_AUDIT"


def test_doctor_classifies_exact_ready_for_audit_run(tmp_path: Path) -> None:
    control, executor, config, _, run, _ = _ready_run(tmp_path)
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "READY_FOR_AUDIT"
    assert diagnosis.run_id == run.run_id
    assert diagnosis.next_action == f"youmo-resume --run-id {run.run_id}"


def test_doctor_rejects_tampered_build_evidence(tmp_path: Path) -> None:
    control, executor, config, _, run, _ = _ready_run(tmp_path)
    assert run.build_evidence is not None
    Path(run.build_evidence.path).write_text("{}\n", encoding="utf-8")
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "STALE_EVIDENCE"
    assert diagnosis.run_id == run.run_id


def test_doctor_detects_interrupted_audit_without_active_lease(tmp_path: Path) -> None:
    control, executor, config, _, run, _ = _ready_run(tmp_path)
    transition_run(control, config, run, new_stage="AUDIT_RUNNING")
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "INTERRUPTED_AUDIT"
    assert "automatic repair" in diagnosis.next_action


def test_doctor_classifies_retryable_checkpoint_failure(tmp_path: Path) -> None:
    control, executor, config, _, run, build = _ready_run(tmp_path)
    run = _advance_to_checkpoint(control, executor, config, run, build)
    running = transition_run(control, config, run, new_stage="CHECKPOINT_RUNNING")
    failed = transition_run(
        control,
        config,
        running,
        new_stage="CHECKPOINT_FAILED",
        last_error="missing git identity",
    )
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.state == "CHECKPOINT_RETRYABLE"
    assert diagnosis.run_id == failed.run_id


def test_resume_dry_run_dispatches_only_exact_next_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    control, _, _, manifest_path, run, _ = _ready_run(tmp_path)
    rc = resume_main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run.run_id,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "NEXT_GATE=AUDIT" in captured.out
    assert "EXECUTION=DRY_RUN" in captured.out
    assert "MUTATIONS=NONE" in captured.out


def test_resume_explicitly_requested_valid_run_ignores_newer_failed_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    control, executor, config, manifest_path, old_run, _ = _ready_run(tmp_path)
    architecture = load_architecture_snapshot(control, config)
    newer = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/run-test",
        base_head=old_run.base_head,
        task="A later run",
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    newer = transition_run(
        control,
        config,
        newer,
        new_stage="BUILD_FAILED",
        last_error="synthetic newer run",
    )

    diagnosis = diagnose_workspace(control, config, executor, run_id=old_run.run_id)
    assert diagnosis.state == "READY_FOR_AUDIT"
    assert diagnosis.run_id == old_run.run_id

    rc = resume_main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            old_run.run_id,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "NEXT_GATE=AUDIT" in captured.out
    assert "EXECUTION=DRY_RUN" in captured.out
    assert "MUTATIONS=NONE" in captured.out

    resumed = load_run_manifest(control, config, old_run.run_id)
    assert resumed.stage == "READY_FOR_AUDIT"
    assert resumed.workspace == old_run.workspace
    assert resumed.build_evidence == old_run.build_evidence
    assert load_run_manifest(control, config, newer.run_id).stage == "BUILD_FAILED"


@pytest.mark.parametrize(
    ("stage", "with_build_evidence", "branch", "base_head", "architecture_lock"),
    (
        ("READY_FOR_AUDIT", False, "phase/run-test", None, None),
        ("BUILD_FAILED", True, "phase/run-test", None, None),
        ("BUILD_FAILED", False, "phase/other", None, None),
        ("BUILD_FAILED", False, "phase/run-test", "0" * 40, None),
        ("BUILD_FAILED", False, "phase/run-test", None, "f" * 64),
    ),
    ids=("ready", "failed-with-build-evidence", "different-branch", "different-base", "different-architecture"),
)
def test_explicit_run_blocks_newer_owning_or_conflicting_manifest(
    tmp_path: Path,
    stage: str,
    with_build_evidence: bool,
    branch: str,
    base_head: str | None,
    architecture_lock: str | None,
) -> None:
    control, executor, config, _, old_run, build = _ready_run(tmp_path)
    architecture = load_architecture_snapshot(control, config)
    newer = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch=branch,
        base_head=base_head or old_run.base_head,
        task="newer conflicting run",
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture_lock or architecture.lock_sha256,
    )
    if stage == "READY_FOR_AUDIT":
        binding = write_run_evidence(
            control, config, newer, stage_name="build", payload=json.dumps(build) + "\n"
        )
        transition_run(control, config, newer, new_stage=stage, build_evidence=binding)
    else:
        binding = (
            write_run_evidence(
                control, config, newer, stage_name="build", payload=json.dumps(build) + "\n"
            )
            if with_build_evidence
            else None
        )
        transition_run(
            control, config, newer, new_stage=stage, build_evidence=binding,
            last_error="synthetic collision",
        )

    diagnosis = diagnose_workspace(control, config, executor, run_id=old_run.run_id)
    assert diagnosis.state == "STALE_EVIDENCE"
    assert diagnosis.run_id == old_run.run_id
    assert "newer blocking run=" in diagnosis.detail


def test_default_diagnosis_still_reports_newest_manifest(tmp_path: Path) -> None:
    control, executor, config, _, old_run, _ = _ready_run(tmp_path)
    architecture = load_architecture_snapshot(control, config)
    newer = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/run-test",
        base_head=old_run.base_head,
        task="newest collision",
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    newer = transition_run(
        control, config, newer, new_stage="BUILD_FAILED", last_error="synthetic collision"
    )
    diagnosis = diagnose_workspace(control, config, executor)
    assert diagnosis.run_id == newer.run_id
    assert diagnosis.state == "BUILD_FAILED"
