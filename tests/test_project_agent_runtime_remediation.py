from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.project_agent_runtime import audit_cli
from tools.project_agent_runtime import audit_engine
from tools.project_agent_runtime.architecture import load_architecture_snapshot
from tools.project_agent_runtime.build_engine import changed_files, diff_fingerprint
from tools.project_agent_runtime.codex_transport import CodexTransportError, run_codex_turn
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.doctor import diagnose_workspace
from tools.project_agent_runtime.operation_lease import acquire_operation_lease, release_operation_lease
from tools.project_agent_runtime.resume_cli import main as resume_main
from tools.project_agent_runtime.run_manifest import (
    create_run_manifest,
    load_run_manifest,
    transition_run,
    write_run_evidence,
)
from tools.project_agent_runtime.validation_policy import FULL_VALIDATION_COMMANDS
from tools.project_agent_runtime.workspace import initialize_executor_workspace


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"
TASK = "Exercise exact runtime remediation"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _transport_sdk(action: object) -> object:
    class Thread:
        id = "transport-thread"

        async def run(self, prompt: str, **kwargs: object) -> object:
            await action()
            return SimpleNamespace(
                id="transport-turn",
                status=SimpleNamespace(value="completed"),
                final_response="done",
                usage=None,
            )

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def thread_start(self, **kwargs: object) -> Thread:
            return Thread()

        async def thread_resume(self, thread_id: str, **kwargs: object) -> Thread:
            return Thread()

    return SimpleNamespace(
        AsyncCodex=Client,
        Sandbox=SimpleNamespace(read_only="READ", workspace_write="WRITE"),
    )


def _transport_repo(tmp_path: Path) -> Path:
    root = tmp_path / "transport"
    root.mkdir()
    _git(root, "init", "-b", "tooling/remediation")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    (root / ".gitignore").write_text(
        ".pytest_cache/\n.ruff_cache/\n__pycache__/\n*.pyc\nprotected.bin\n", encoding="utf-8"
    )
    (root / "app").mkdir()
    (root / "app" / "base.py").write_text("BASE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


@pytest.mark.parametrize(
    ("relative", "contents"),
    (
        ("app/__pycache__/feature.pyc", "bytecode"),
        (".pytest_cache/v/cache/nodeids", "cache"),
        (".ruff_cache/metadata.json", "metadata"),
        (".ruff_cache/0.12.0/cache/content", "cache"),
    ),
)
def test_workspace_write_cleans_only_disposable_ignored_artifacts(
    tmp_path: Path, relative: str, contents: str
) -> None:
    root = _transport_repo(tmp_path)

    async def action() -> None:
        (root / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        artifact = root / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(contents, encoding="utf-8")

    result = asyncio.run(
        run_codex_turn(
            repo_root=root,
            prompt="implement",
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            sandbox_name="workspace_write",
            sdk=_transport_sdk(action),
        )
    )
    assert result.status == "completed"
    assert (root / "app" / "feature.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not (root / relative).exists()


def test_workspace_write_preserves_non_disposable_ignored_artifact(tmp_path: Path) -> None:
    root = _transport_repo(tmp_path)

    async def action() -> None:
        (root / "protected.bin").write_text("keep", encoding="utf-8")

    with pytest.raises(CodexTransportError, match="unsafe hidden/ignored artifacts"):
        asyncio.run(
            run_codex_turn(
                repo_root=root,
                prompt="implement",
                developer_instructions="rules",
                model="gpt-5.6-terra",
                reasoning="high",
                sandbox_name="workspace_write",
                sdk=_transport_sdk(action),
            )
        )
    assert (root / "protected.bin").read_text(encoding="utf-8") == "keep"


def test_read_only_transport_does_not_apply_workspace_hygiene(tmp_path: Path) -> None:
    root = tmp_path / "read-only"
    root.mkdir()

    async def action() -> None:
        (root / "protected.bin").write_text("unchanged behavior", encoding="utf-8")

    result = asyncio.run(
        run_codex_turn(
            repo_root=root,
            prompt="inspect",
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            sandbox_name="read_only",
            sdk=_transport_sdk(action),
        )
    )
    assert result.status == "completed"
    assert (root / "protected.bin").is_file()


def _runtime_fixture(
    tmp_path: Path,
    *,
    mutate_during_validation: bool = False,
    fail_during_validation: bool = False,
) -> tuple[Path, Path, object, Path, object]:
    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-b", "tooling/remediation-control")
    _git(control, "config", "user.name", "Runtime Test")
    _git(control, "config", "user.email", "runtime@example.invalid")
    _git(control, "remote", "add", "origin", f"git@github.com:{REPOSITORY}.git")
    (control / ".gitignore").write_text(
        ".venv/\n.pytest_cache/\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    lock = {
        "schema_version": 3,
        "architecture_status": "target_during_controlled_migration",
        "current_repository": {"repository": REPOSITORY, "branch": "youmo-clone-v2"},
        "runtime_stages": ["topic"],
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
    (control / "app" / "base.py").write_text("BASE = 1\n", encoding="utf-8")
    (control / "app" / "static").mkdir()
    (control / "app" / "static" / "app.js").write_text("const runtime = true;\n", encoding="utf-8")
    (control / "tests").mkdir()
    validation_body = (
        "def test_base() -> None:\n"
        "    from pathlib import Path\n"
        "    root = Path(__file__).resolve().parents[1]\n"
        "    assert (root / '.venv' / 'bin' / 'python').is_file()\n"
    )
    if mutate_during_validation:
        validation_body += (
            "    (root / 'app' / 'feature.py').write_text('VALUE = 3\\n', encoding='utf-8')\n"
        )
    if fail_during_validation:
        validation_body += "    raise AssertionError('synthetic validation failure')\n"
    (control / "tests" / "test_base.py").write_text(validation_body, encoding="utf-8")
    manifest_path = control / "youmo.json"
    manifest_path.write_text(
        json.dumps(
            {
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
        )
        + "\n",
        encoding="utf-8",
    )
    _git(control, "add", ".")
    _git(control, "commit", "-m", "base")
    config = load_project_config(control, manifest_path)
    base = _git(control, "rev-parse", "HEAD")
    executor = tmp_path / "executor"
    initialize_executor_workspace(
        control,
        executor,
        config,
        base_sha=base,
        branch="phase/remediation",
        _clone_source_url=str(control),
    )
    feature = executor / "app" / "feature.py"
    feature.write_text("VALUE = 2\n", encoding="utf-8")
    files = changed_files(executor)
    diff_sha, fingerprints = diff_fingerprint(executor, files)
    architecture = load_architecture_snapshot(control, config)
    run = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/remediation",
        base_head=base,
        task=TASK,
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    build = {
        "status": "READY_FOR_AUDIT",
        "ready_for_audit": True,
        "base_head": base,
        "branch": "phase/remediation",
        "task_sha256": hashlib.sha256(TASK.encode("utf-8")).hexdigest(),
        "allowed_paths": ["app"],
        "changed_files": list(files),
        "diff_sha256": diff_sha,
        "file_fingerprints": fingerprints,
        "validations": [],
        "violations": [],
    }
    binding = write_run_evidence(
        control, config, run, stage_name="build", payload=json.dumps(build) + "\n"
    )
    return control, executor, config, manifest_path, transition_run(
        control, config, run, new_stage="READY_FOR_AUDIT", build_evidence=binding
    )


def test_audit_binds_external_venv_only_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    control, executor, _, manifest_path, run = _runtime_fixture(tmp_path)
    trusted = tmp_path / "trusted-venv"
    (trusted / "bin").mkdir(parents=True)
    os.symlink(Path(sys.executable).resolve(), trusted / "bin" / "python")
    monkeypatch.setenv("YOUMO_VALIDATION_VENV", str(trusted))

    async def passing_turn(**kwargs: object) -> object:
        assert not (executor / ".venv").exists()
        assert not (executor / ".venv").is_symlink()
        return SimpleNamespace(
            thread_id="audit-thread",
            turn_id="audit-turn",
            status="completed",
            final_response=json.dumps(
                {"verdict": "PASS", "summary": "external validation passed", "findings": []}
            ),
            usage=None,
        )

    monkeypatch.setattr(audit_cli, "inspect_codex_sdk", lambda _: SimpleNamespace(ready=True))
    monkeypatch.setattr(audit_cli, "run_codex_turn", passing_turn)
    assert audit_cli.main([
        "--repo", str(control), "--project", str(manifest_path), "--run-id", run.run_id, "--execute"
    ]) == 0
    capsys.readouterr()
    expected = tuple(
        tuple(".venv/bin/python" if part == "python3" else part for part in command)
        for command in FULL_VALIDATION_COMMANDS
    )
    completed = load_run_manifest(control, load_project_config(control, manifest_path), run.run_id)
    assert completed.audit_evidence is not None
    audit = json.loads(Path(completed.audit_evidence.path).read_text(encoding="utf-8"))
    assert tuple(tuple(item["argv"]) for item in audit["validations"]) == expected
    assert not (executor / ".venv").exists()
    assert not (executor / ".venv").is_symlink()
    assert audit_cli._validation_commands(False) == FULL_VALIDATION_COMMANDS


def test_audit_rejects_validation_created_source_mutation_before_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    control, executor, _, manifest_path, run = _runtime_fixture(
        tmp_path, mutate_during_validation=True
    )
    trusted = tmp_path / "trusted-venv"
    (trusted / "bin").mkdir(parents=True)
    os.symlink(Path(sys.executable).resolve(), trusted / "bin" / "python")
    monkeypatch.setenv("YOUMO_VALIDATION_VENV", str(trusted))
    monkeypatch.setattr(audit_cli, "inspect_codex_sdk", lambda _: SimpleNamespace(ready=True))

    async def runner(**_: object) -> object:
        raise AssertionError("semantic auditor must not start after validation mutation")

    monkeypatch.setattr(audit_cli, "run_codex_turn", runner)
    assert audit_cli.main([
        "--repo", str(control), "--project", str(manifest_path), "--run-id", run.run_id, "--execute"
    ]) == 9
    assert "diff fingerprint does not match build evidence" in capsys.readouterr().err
    assert not (executor / ".venv").exists()
    assert not (executor / ".venv").is_symlink()


def test_audit_rechecks_evidence_after_failed_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    control, executor, _, manifest_path, run = _runtime_fixture(
        tmp_path, mutate_during_validation=True, fail_during_validation=True
    )
    trusted = tmp_path / "trusted-venv"
    (trusted / "bin").mkdir(parents=True)
    os.symlink(Path(sys.executable).resolve(), trusted / "bin" / "python")
    monkeypatch.setenv("YOUMO_VALIDATION_VENV", str(trusted))
    monkeypatch.setattr(audit_cli, "inspect_codex_sdk", lambda _: SimpleNamespace(ready=True))

    async def runner(**_: object) -> object:
        raise AssertionError("semantic auditor must not start after failed stale validation")

    monkeypatch.setattr(audit_cli, "run_codex_turn", runner)
    assert audit_cli.main([
        "--repo", str(control), "--project", str(manifest_path), "--run-id", run.run_id, "--execute"
    ]) == 9
    assert "diff fingerprint does not match build evidence" in capsys.readouterr().err
    assert not (executor / ".venv").exists()
    assert not (executor / ".venv").is_symlink()


def test_audit_rejects_stale_application_diff_before_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    control, executor, _, manifest_path, run = _runtime_fixture(tmp_path)
    (executor / "app" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")

    assert audit_cli.main([
        "--repo", str(control), "--project", str(manifest_path), "--run-id", run.run_id, "--execute"
    ]) == 9
    captured = capsys.readouterr()
    assert "audit evidence preflight failed" in captured.err
    assert "diff fingerprint" in captured.err


def test_audit_rejects_validation_venv_inside_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    control, executor, _, manifest_path, run = _runtime_fixture(tmp_path)
    internal = executor / ".venv"
    (internal / "bin").mkdir(parents=True)
    os.symlink(Path(sys.executable).resolve(), internal / "bin" / "python")
    monkeypatch.setenv("YOUMO_VALIDATION_VENV", str(internal))

    assert audit_cli.main([
        "--repo", str(control), "--project", str(manifest_path), "--run-id", run.run_id, "--execute"
    ]) == 9
    assert "validation virtualenv must live outside the executor workspace" in capsys.readouterr().err


def _guarded_audit_kwargs(tmp_path: Path, turn_runner: object) -> dict[str, object]:
    return {
        "workspace_root": tmp_path,
        "task": TASK,
        "build_evidence": {
            "task_sha256": "task-sha256",
            "base_head": "base-head",
            "branch": "tooling/remediation",
            "allowed_paths": [],
            "changed_files": [],
            "diff_sha256": "diff-sha256",
        },
        "expected_architecture": {},
        "developer_instructions": "rules",
        "model": "gpt-5.6-sol",
        "reasoning": "high",
        "turn_runner": turn_runner,
    }


def _stub_audit_preflight(*_: object) -> tuple[str, str, tuple[str, ...], str]:
    return "base-head", "tooling/remediation", (), "diff-sha256"


def test_audit_validation_default_timeout_is_1200_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[int] = []

    def successful_run(*_: object, **kwargs: object) -> object:
        received.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(audit_engine.subprocess, "run", successful_run)
    results = audit_engine._run_validations(tmp_path, (("test-default",),))

    assert audit_engine.DEFAULT_AUDIT_VALIDATION_TIMEOUT_SECONDS == 1200
    assert audit_engine.DEFAULT_AUDIT_VALIDATION_TIMEOUT_SECONDS > 300
    assert results[0].passed
    assert received == [1200]


def test_audit_threads_explicit_validation_timeout_to_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[int] = []

    def successful_run(*_: object, **kwargs: object) -> object:
        received.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    async def passing_turn(**_: object) -> object:
        return SimpleNamespace(
            thread_id="audit-thread",
            turn_id="audit-turn",
            status="completed",
            final_response=json.dumps({"verdict": "PASS", "summary": "passed", "findings": []}),
            usage=None,
        )

    monkeypatch.setattr(audit_engine, "_evidence_preflight", _stub_audit_preflight)
    monkeypatch.setattr(audit_engine.subprocess, "run", successful_run)
    result = asyncio.run(audit_engine.run_guarded_audit(
        **_guarded_audit_kwargs(tmp_path, passing_turn),
        validation_commands=(("test-explicit",),),
        validation_timeout_seconds=451,
    ))

    assert result.passed
    assert received == [451]


def test_audit_validation_timeout_fails_closed_without_starting_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = (("test-timeout",), ("must-not-run",))
    executed: list[tuple[str, ...]] = []

    def timeout_run(argv: list[str], **kwargs: object) -> object:
        executed.append(tuple(argv))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    async def must_not_start(**_: object) -> object:
        raise AssertionError("semantic auditor must not start after validation timeout")

    monkeypatch.setattr(audit_engine, "_evidence_preflight", _stub_audit_preflight)
    monkeypatch.setattr(audit_engine.subprocess, "run", timeout_run)
    result = asyncio.run(audit_engine.run_guarded_audit(
        **_guarded_audit_kwargs(tmp_path, must_not_start), validation_commands=commands
    ))

    assert result.status == "AUDIT_FAIL"
    assert result.validations[0].argv == commands[0]
    assert result.validations[0].returncode == 125
    assert executed == [commands[0]]


def test_audit_nonzero_validation_stops_sequence_without_starting_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = (("test-failure",), ("must-not-run",))
    executed: list[tuple[str, ...]] = []

    def failed_run(argv: list[str], **_: object) -> object:
        executed.append(tuple(argv))
        return SimpleNamespace(returncode=37, stdout="failure", stderr="details")

    async def must_not_start(**_: object) -> object:
        raise AssertionError("semantic auditor must not start after validation failure")

    monkeypatch.setattr(audit_engine, "_evidence_preflight", _stub_audit_preflight)
    monkeypatch.setattr(audit_engine.subprocess, "run", failed_run)
    result = asyncio.run(audit_engine.run_guarded_audit(
        **_guarded_audit_kwargs(tmp_path, must_not_start), validation_commands=commands
    ))

    assert result.status == "AUDIT_FAIL"
    assert result.validations[0].returncode == 37
    assert executed == [commands[0]]


def test_audit_validation_commands_preserve_declared_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = (("first", "one"), ("second",), ("third", "three"))
    executed: list[tuple[str, ...]] = []

    def successful_run(argv: list[str], **_: object) -> object:
        executed.append(tuple(argv))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(audit_engine.subprocess, "run", successful_run)
    results = audit_engine._run_validations(tmp_path, commands)

    assert all(result.passed for result in results)
    assert executed == list(commands)


@pytest.mark.parametrize("timeout_seconds", (0, -1, True, 1.5, "1200"))
def test_audit_rejects_invalid_validation_timeout(
    tmp_path: Path, timeout_seconds: object
) -> None:
    with pytest.raises(audit_engine.AuditGuardError, match="positive integer"):
        audit_engine._run_validations(
            tmp_path,
            (("must-not-run",),),
            timeout_seconds=timeout_seconds,
        )


def test_explicit_run_diagnosis_uses_requested_manifest_and_stays_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    control, executor, config, manifest_path, old_run = _runtime_fixture(tmp_path)
    architecture = load_architecture_snapshot(control, config)
    newer = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/remediation",
        base_head=old_run.base_head,
        task="newer failed run",
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    transition_run(control, config, newer, new_stage="BUILD_FAILED", last_error="synthetic failure")

    explicit = diagnose_workspace(control, config, executor, run_id=old_run.run_id)
    assert explicit.state == "READY_FOR_AUDIT"
    assert explicit.run_id == old_run.run_id
    assert diagnose_workspace(control, config, executor).run_id == newer.run_id
    assert resume_main([
        "--repo", str(control), "--project", str(manifest_path), "--run-id", old_run.run_id
    ]) == 0
    assert "NEXT_GATE=AUDIT" in capsys.readouterr().out

    path, lease = acquire_operation_lease(control, config, executor, "test")
    try:
        assert diagnose_workspace(control, config, executor, run_id=old_run.run_id).state == "LEASE_ACTIVE"
    finally:
        release_operation_lease(path, lease)

    (executor / "app" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert diagnose_workspace(control, config, executor, run_id=old_run.run_id).state == "STALE_EVIDENCE"

    foreign = create_run_manifest(
        control,
        config,
        workspace=tmp_path / "other-executor",
        branch="phase/remediation",
        base_head=old_run.base_head,
        task="foreign workspace run",
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    assert diagnose_workspace(control, config, executor, run_id=foreign.run_id).state == "STALE_EVIDENCE"
