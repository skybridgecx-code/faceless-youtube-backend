from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import tools.project_agent_runtime.publish as publish_module
from tools.project_agent_runtime.architecture import load_architecture_snapshot
from tools.project_agent_runtime.build_engine import changed_files, diff_fingerprint
from tools.project_agent_runtime.checkpoint_engine import create_checkpoint
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.git_state import inspect_repo
from tools.project_agent_runtime.publish import PublishError, execute_publish, prepare_publish
from tools.project_agent_runtime.run_manifest import (
    create_run_manifest,
    run_directory,
    transition_run,
    write_run_evidence,
)
from tools.project_agent_runtime.workspace import initialize_executor_workspace


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"
BRANCH = "phase/publish-test"


def _git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def _bare_sha(bare: Path, branch: str) -> str | None:
    completed = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _fixture(tmp_path: Path):
    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-b", "tooling/test")
    _git(control, "config", "user.name", "Publish Test")
    _git(control, "config", "user.email", "publish@example.invalid")
    _git(control, "remote", "add", "origin", f"git@github.com:{REPOSITORY}.git")
    (control / ".gitignore").write_text(
        ".pytest_cache/\n__pycache__/\n*.pyc\n", encoding="utf-8"
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
        branch=BRANCH,
        _clone_source_url=str(control),
    )
    _git(executor, "config", "user.name", "Publish Test")
    _git(executor, "config", "user.email", "publish@example.invalid")

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True)
    _git(control, "push", str(bare), f"{base}:refs/heads/youmo-clone-v2")
    return control, executor, config, manifest_path, base, bare


def _checkpointed_run(tmp_path: Path):
    control, executor, config, manifest_path, base, bare = _fixture(tmp_path)
    task = "Implement safe publisher fixture"
    (executor / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    files = changed_files(executor)
    diff_sha, fingerprints = diff_fingerprint(executor, files)
    task_sha = hashlib.sha256(task.encode("utf-8")).hexdigest()
    build = {
        "status": "READY_FOR_AUDIT",
        "ready_for_audit": True,
        "base_head": base,
        "branch": BRANCH,
        "task_sha256": task_sha,
        "allowed_paths": ["app"],
        "changed_files": list(files),
        "diff_sha256": diff_sha,
        "file_fingerprints": fingerprints,
        "validations": [],
        "violations": [],
    }
    audit = {
        "status": "AUDIT_PASS",
        "passed": True,
        "verdict": "PASS",
        "summary": "clean",
        "findings": [],
        "base_head": base,
        "branch": BRANCH,
        "task_sha256": task_sha,
        "diff_sha256": diff_sha,
        "validations": [],
        "violations": [],
    }
    architecture = load_architecture_snapshot(control, config)
    run = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch=BRANCH,
        base_head=base,
        task=task,
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    build_binding = write_run_evidence(
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
        build_evidence=build_binding,
    )
    run = transition_run(control, config, run, new_stage="AUDIT_RUNNING")
    audit_binding = write_run_evidence(
        control,
        config,
        run,
        stage_name="audit",
        payload=json.dumps(audit, indent=2, sort_keys=True) + "\n",
    )
    run = transition_run(
        control,
        config,
        run,
        new_stage="READY_FOR_CHECKPOINT",
        audit_evidence=audit_binding,
    )
    running = transition_run(control, config, run, new_stage="CHECKPOINT_RUNNING")
    expected_architecture = {
        config.architecture_lock: architecture.lock_sha256,
        **architecture.source_sha256,
    }
    checkpoint = create_checkpoint(
        control_root=control,
        workspace_root=executor,
        config=config,
        task=task,
        build_evidence=build,
        audit_evidence=audit,
        expected_architecture=expected_architecture,
        build_evidence_sha256=build_binding.sha256,
        audit_evidence_sha256=audit_binding.sha256,
        subject="Safe publish fixture",
    )
    checkpoint_binding = write_run_evidence(
        control,
        config,
        running,
        stage_name="checkpoint",
        payload=checkpoint.to_json(),
    )
    final = transition_run(
        control,
        config,
        running,
        new_stage="CHECKPOINTED",
        checkpoint_evidence=checkpoint_binding,
        checkpoint_commit=checkpoint.commit_sha,
    )
    return control, executor, config, manifest_path, final, checkpoint, bare, base


def _make_sibling_commit(tmp_path: Path, control: Path, base: str, name: str) -> tuple[Path, str]:
    sibling = tmp_path / name
    _git(tmp_path, "clone", "--no-hardlinks", str(control), str(sibling))
    _git(sibling, "config", "user.name", "Race Writer")
    _git(sibling, "config", "user.email", "race@example.invalid")
    _git(sibling, "checkout", "-B", f"{name}-branch", base)
    (sibling / f"{name}.txt").write_text(name + "\n", encoding="utf-8")
    _git(sibling, "add", ".")
    _git(sibling, "commit", "-m", name)
    return sibling, _git(sibling, "rev-parse", "HEAD")


def test_publish_creates_noncanonical_remote_and_preserves_canonical(tmp_path: Path) -> None:
    control, executor, config, _, run, checkpoint, bare, base = _checkpointed_run(tmp_path)
    plan = prepare_publish(control, config, run.run_id, _remote_target=str(bare))
    assert plan.mode == "CREATE_REMOTE_BRANCH"
    assert plan.remote_before is None

    result = execute_publish(control, config, run.run_id, _remote_target=str(bare))
    assert result.status == "PUBLISHED"
    assert result.remote_after == checkpoint.commit_sha
    assert _bare_sha(bare, BRANCH) == checkpoint.commit_sha
    assert _bare_sha(bare, "youmo-clone-v2") == base
    evidence = json.loads(Path(result.evidence_path).read_text(encoding="utf-8"))
    assert evidence["canonical_branch_mutated"] is False
    assert evidence["merge_started"] is False


def test_publish_fast_forwards_existing_remote_parent(tmp_path: Path) -> None:
    control, _, config, _, run, checkpoint, bare, base = _checkpointed_run(tmp_path)
    _git(control, "push", str(bare), f"{base}:refs/heads/{BRANCH}")

    plan = prepare_publish(control, config, run.run_id, _remote_target=str(bare))
    assert plan.mode == "FAST_FORWARD_REMOTE_BRANCH"
    assert plan.remote_before == base
    result = execute_publish(control, config, run.run_id, _remote_target=str(bare))
    assert result.remote_before == base
    assert _bare_sha(bare, BRANCH) == checkpoint.commit_sha


def test_publish_is_idempotent_and_publish_evidence_is_immutable(tmp_path: Path) -> None:
    control, _, config, _, run, checkpoint, bare, _ = _checkpointed_run(tmp_path)
    first = execute_publish(control, config, run.run_id, _remote_target=str(bare))
    evidence = Path(first.evidence_path)
    original = evidence.read_bytes()

    second = execute_publish(control, config, run.run_id, _remote_target=str(bare))
    assert second.status == "ALREADY_PUBLISHED"
    assert second.remote_after == checkpoint.commit_sha
    assert evidence.read_bytes() == original


def test_publish_rejects_canonical_branch_even_before_executor_checks(tmp_path: Path) -> None:
    control, _, config, _, run, _, bare, _ = _checkpointed_run(tmp_path)
    canonicalized = replace(config, canonical_branch=run.branch)
    with pytest.raises(PublishError, match="canonical branch is forbidden"):
        prepare_publish(control, canonicalized, run.run_id, _remote_target=str(bare))


def test_publish_rejects_dirty_executor(tmp_path: Path) -> None:
    control, executor, config, _, run, _, bare, _ = _checkpointed_run(tmp_path)
    (executor / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(PublishError, match="executor verification failed"):
        prepare_publish(control, config, run.run_id, _remote_target=str(bare))


def test_publish_rejects_wrong_origin(tmp_path: Path) -> None:
    control, executor, config, _, run, _, bare, _ = _checkpointed_run(tmp_path)
    _git(executor, "remote", "set-url", "origin", "git@github.com:other/repo.git")
    with pytest.raises(PublishError, match="executor verification failed"):
        prepare_publish(control, config, run.run_id, _remote_target=str(bare))


def test_publish_rejects_stale_checkpoint_head(tmp_path: Path) -> None:
    control, executor, config, _, run, _, bare, _ = _checkpointed_run(tmp_path)
    (executor / "after.txt").write_text("later\n", encoding="utf-8")
    _git(executor, "add", "after.txt")
    _git(executor, "commit", "-m", "after checkpoint")
    with pytest.raises(PublishError, match="executor verification failed"):
        prepare_publish(control, config, run.run_id, _remote_target=str(bare))


def test_publish_rejects_tampered_checkpoint_evidence(tmp_path: Path) -> None:
    control, _, config, _, run, _, bare, _ = _checkpointed_run(tmp_path)
    assert run.checkpoint_evidence is not None
    Path(run.checkpoint_evidence.path).write_text("{}\n", encoding="utf-8")
    with pytest.raises(PublishError, match="hash mismatch"):
        prepare_publish(control, config, run.run_id, _remote_target=str(bare))


def test_publish_rejects_divergent_remote_branch(tmp_path: Path) -> None:
    control, _, config, _, run, _, bare, base = _checkpointed_run(tmp_path)
    sibling, sibling_sha = _make_sibling_commit(tmp_path, control, base, "divergent")
    _git(sibling, "push", str(bare), f"{sibling_sha}:refs/heads/{BRANCH}")

    with pytest.raises(PublishError, match="not an ancestor"):
        execute_publish(control, config, run.run_id, _remote_target=str(bare))
    assert _bare_sha(bare, BRANCH) == sibling_sha
    assert not (run_directory(control, config, run.run_id) / "publish.json").exists()


def test_force_with_lease_rejects_remote_race_between_plan_and_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, executor, config, _, run, _, bare, base = _checkpointed_run(tmp_path)
    _git(control, "push", str(bare), f"{base}:refs/heads/{BRANCH}")
    racer, racer_sha = _make_sibling_commit(tmp_path, control, base, "racer")
    original_git = publish_module._git
    raced = False

    def racing_git(root: Path, *args: str, timeout: int = 120) -> str:
        nonlocal raced
        if args and args[0] == "push" and not raced:
            raced = True
            _git(racer, "push", str(bare), f"{racer_sha}:refs/heads/{BRANCH}")
        return original_git(root, *args, timeout=timeout)

    monkeypatch.setattr(publish_module, "_git", racing_git)
    with pytest.raises(PublishError, match="git push failed"):
        execute_publish(control, config, run.run_id, _remote_target=str(bare))
    assert raced
    assert _bare_sha(bare, BRANCH) == racer_sha
    assert _git(executor, "rev-parse", "HEAD") == run.checkpoint_commit
    assert not (run_directory(control, config, run.run_id) / "publish.json").exists()
