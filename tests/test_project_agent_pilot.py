from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.project_agent_runtime import pilot, pilot_cli
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.git_state import inspect_repo
from tools.project_agent_runtime.operation_lease import (
    acquire_operation_lease,
    release_operation_lease,
)
from tools.project_agent_runtime.pilot import PilotError, inspect_live_readiness, resolve_remote_branch_sha
from tools.project_agent_runtime.workspace import (
    WorkspaceReport,
    initialize_executor_workspace,
)


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _control_fixture(tmp_path: Path):
    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-b", "tooling/pilot-test")
    _git(control, "config", "user.name", "Pilot Test")
    _git(control, "config", "user.email", "pilot@example.invalid")
    _git(control, "remote", "add", "origin", f"git@github.com:{REPOSITORY}.git")
    (control / ".gitignore").write_text(
        ".env\n.venv\n.venv/\n.pytest_cache/\n__pycache__/\n*.pyc\n",
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
    _git(control, "commit", "-m", "pilot control base")
    config = load_project_config(control, manifest_path)
    return control, config, manifest_path


def _fake_release_ready() -> SimpleNamespace:
    return SimpleNamespace(
        ready=True,
        push_disabled=True,
        detail="controller release ready",
    )


def _fake_sdk_ready() -> SimpleNamespace:
    return SimpleNamespace(ready=True, detail="SDK ready")


def test_remote_branch_resolution_is_read_only(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-b", "youmo-clone-v2")
    _git(remote, "config", "user.name", "Remote Test")
    _git(remote, "config", "user.email", "remote@example.invalid")
    (remote / "README.md").write_text("base\n", encoding="utf-8")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "remote base")
    expected = _git(remote, "rev-parse", "HEAD")

    control = tmp_path / "resolver"
    control.mkdir()
    _git(control, "init", "-b", "tooling/resolve")
    _git(control, "config", "user.name", "Resolver Test")
    _git(control, "config", "user.email", "resolver@example.invalid")
    (control / "tracked.txt").write_text("unchanged\n", encoding="utf-8")
    _git(control, "add", ".")
    _git(control, "commit", "-m", "resolver base")
    _git(control, "remote", "add", "origin", str(remote))

    before_head = _git(control, "rev-parse", "HEAD")
    before_refs = _git(control, "show-ref")
    before_status = _git(control, "status", "--porcelain=v1", "-uall")
    resolved = resolve_remote_branch_sha(control, "youmo-clone-v2")

    assert resolved == expected
    assert _git(control, "rev-parse", "HEAD") == before_head
    assert _git(control, "show-ref") == before_refs
    assert _git(control, "status", "--porcelain=v1", "-uall") == before_status


def test_remote_branch_resolution_rejects_invalid_ref_before_lookup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "tooling/test")
    with pytest.raises(PilotError, match="invalid Git branch name"):
        resolve_remote_branch_sha(repo, "../bad ref")


def test_prepare_dry_run_resolves_exact_base_without_creating_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, _, manifest_path = _control_fixture(tmp_path)
    workspace = tmp_path / "executor"
    expected = "a" * 40
    calls: list[tuple[Path, str]] = []

    def fake_resolve(root: Path, branch: str, *, remote: str = "origin") -> str:
        calls.append((root.resolve(), branch))
        return expected

    monkeypatch.setattr(pilot_cli, "resolve_remote_branch_sha", fake_resolve)
    rc = pilot_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "prepare",
            "--workspace",
            str(workspace),
            "--branch",
            "phase/pilot-test",
            "--base-ref",
            "youmo-clone-v2",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert calls == [(control.resolve(), "youmo-clone-v2")]
    assert "PILOT_PREPARE=DRY_RUN" in captured.out
    assert f"BASE_SHA={expected}" in captured.out
    assert "ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE" in captured.out
    assert not workspace.exists()


def test_prepare_execute_passes_resolved_sha_to_executor_initializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, _, manifest_path = _control_fixture(tmp_path)
    workspace = tmp_path / "executor"
    expected = "b" * 40
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        pilot_cli,
        "resolve_remote_branch_sha",
        lambda *_args, **_kwargs: expected,
    )

    def fake_initialize(control_root, destination, config, *, base_sha, branch):
        observed.update(
            {
                "control": Path(control_root).resolve(),
                "destination": Path(destination).resolve(),
                "base_sha": base_sha,
                "branch": branch,
                "project_id": config.project_id,
            }
        )
        state = SimpleNamespace(branch=branch, head=base_sha)
        return SimpleNamespace(passed=True, state=state, checks=())

    monkeypatch.setattr(pilot_cli, "initialize_executor_workspace", fake_initialize)
    rc = pilot_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "prepare",
            "--workspace",
            str(workspace),
            "--branch",
            "phase/pilot-test",
            "--execute",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert observed == {
        "control": control.resolve(),
        "destination": workspace.resolve(),
        "base_sha": expected,
        "branch": "phase/pilot-test",
        "project_id": "youmo",
    }
    assert f"BASE_SHA={expected}" in captured.out
    assert "CODEX_TRANSPORT=NOT_STARTED" in captured.out


def _readiness_fixture(tmp_path: Path):
    control, config, manifest_path = _control_fixture(tmp_path)
    base = inspect_repo(control).head
    executor = tmp_path / "executor"
    initialize_executor_workspace(
        control,
        executor,
        config,
        base_sha=base,
        branch="phase/readiness-test",
        _clone_source_url=str(control),
    )
    validation_venv = tmp_path / "trusted-venv"
    (validation_venv / "bin").mkdir(parents=True)
    (validation_venv / "bin" / "python").write_text("python\n", encoding="utf-8")
    return control, executor, config, manifest_path, validation_venv


def test_live_readiness_passes_only_when_all_hard_gates_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, executor, config, _, validation_venv = _readiness_fixture(tmp_path)
    monkeypatch.setattr(pilot, "inspect_controller_release", lambda _layout: _fake_release_ready())
    monkeypatch.setattr(pilot, "inspect_codex_sdk", lambda _requirement: _fake_sdk_ready())
    monkeypatch.setattr(pilot, "resolve_validation_venv", lambda _root: validation_venv)

    report = inspect_live_readiness(
        control,
        config,
        inspect_repo(control),
        executor,
        controller_layout=SimpleNamespace(),
    )

    assert report.ready
    assert all(check.passed for check in report.checks)
    assert not report.warnings


def test_live_readiness_fails_on_ignored_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, executor, config, _, validation_venv = _readiness_fixture(tmp_path)
    (executor / ".env").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.setattr(pilot, "inspect_controller_release", lambda _layout: _fake_release_ready())
    monkeypatch.setattr(pilot, "inspect_codex_sdk", lambda _requirement: _fake_sdk_ready())
    monkeypatch.setattr(pilot, "resolve_validation_venv", lambda _root: validation_venv)

    report = inspect_live_readiness(
        control,
        config,
        inspect_repo(control),
        executor,
        controller_layout=SimpleNamespace(),
    )
    ignored = next(check for check in report.checks if check.name == "ignored_artifacts")

    assert not report.ready
    assert not ignored.passed
    assert ".env" in ignored.detail
    assert (executor / ".env").read_text(encoding="utf-8") == "SECRET=1\n"


def test_live_readiness_fails_with_active_executor_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, executor, config, _, validation_venv = _readiness_fixture(tmp_path)
    monkeypatch.setattr(pilot, "inspect_controller_release", lambda _layout: _fake_release_ready())
    monkeypatch.setattr(pilot, "inspect_codex_sdk", lambda _requirement: _fake_sdk_ready())
    monkeypatch.setattr(pilot, "resolve_validation_venv", lambda _root: validation_venv)
    path, lease = acquire_operation_lease(control, config, executor, "build")
    try:
        report = inspect_live_readiness(
            control,
            config,
            inspect_repo(control),
            executor,
            controller_layout=SimpleNamespace(),
        )
    finally:
        release_operation_lease(path, lease)

    check = next(item for item in report.checks if item.name == "executor_lease")
    assert not report.ready
    assert not check.passed
    assert "active build lease" in check.detail


def test_live_readiness_rejects_validation_venv_inside_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, executor, config, _, _ = _readiness_fixture(tmp_path)
    inside = executor / "trusted-venv"
    (inside / "bin").mkdir(parents=True)
    (inside / "bin" / "python").write_text("python\n", encoding="utf-8")
    monkeypatch.setattr(pilot, "inspect_controller_release", lambda _layout: _fake_release_ready())
    monkeypatch.setattr(pilot, "inspect_codex_sdk", lambda _requirement: _fake_sdk_ready())
    monkeypatch.setattr(pilot, "resolve_validation_venv", lambda _root: inside)

    report = inspect_live_readiness(
        control,
        config,
        inspect_repo(control),
        executor,
        controller_layout=SimpleNamespace(),
    )
    check = next(item for item in report.checks if item.name == "validation_environment")

    assert not report.ready
    assert not check.passed
    assert "inside the executor" in check.detail
