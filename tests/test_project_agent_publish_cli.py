from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tools.project_agent_runtime.publish_cli as publish_cli
from tools.project_agent_runtime.publish import PublishPlan


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _control(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "control"
    root.mkdir()
    _git(root, "init", "-b", "tooling/test")
    _git(root, "config", "user.name", "CLI Test")
    _git(root, "config", "user.email", "cli@example.invalid")
    _git(root, "remote", "add", "origin", f"git@github.com:{REPOSITORY}.git")
    lock = {
        "schema_version": 3,
        "architecture_status": "target_during_controlled_migration",
        "current_repository": {"repository": REPOSITORY, "branch": "youmo-clone-v2"},
        "runtime_stages": [],
        "hard_invariants": [],
        "forbidden_v1": [],
        "migration_rules": [],
        "commercial_success_contract": {"phase_requirements": {}},
    }
    (root / "architecture.lock.json").write_text(json.dumps(lock) + "\n", encoding="utf-8")
    for relative in (
        "ARCHITECTURE.md",
        "COMMERCIAL_SUCCESS.md",
        ".agents/rules/youtube-automation-project.md",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "project_id": "youmo",
        "display_name": "YouMo",
        "repository": REPOSITORY,
        "canonical_branch": "youmo-clone-v2",
        "allowed_branch_prefixes": ["tooling/", "phase/", "fix/", "audit/"],
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
    manifest_path = root / "youmo.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "control")
    return root, manifest_path


def test_publish_cli_defaults_to_dry_run_and_never_calls_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest = _control(tmp_path)
    run_id = "a" * 32
    plan = PublishPlan(
        run_id=run_id,
        workspace=str(tmp_path / "executor"),
        branch="phase/example",
        commit_sha="b" * 40,
        repository=REPOSITORY,
        remote_before=None,
        mode="CREATE_REMOTE_BRANCH",
    )
    monkeypatch.setattr(publish_cli, "prepare_publish", lambda *args, **kwargs: plan)

    def forbidden_execute(*args: object, **kwargs: object) -> object:
        raise AssertionError("execute_publish must not run without --execute")

    monkeypatch.setattr(publish_cli, "execute_publish", forbidden_execute)
    rc = publish_cli.main(
        [
            "--repo",
            str(root),
            "--project",
            str(manifest),
            "--run-id",
            run_id,
        ]
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert "PUBLISH_PREFLIGHT=PASS" in output
    assert "CANONICAL_BRANCH_MUTATION=FORBIDDEN" in output
    assert "MERGE=NOT_STARTED" in output
    assert "GIT_PUSH=NOT_STARTED" in output
    assert "--execute" in output
