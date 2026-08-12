from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tools.project_agent_runtime.promotion_cli as promotion_cli
from tools.project_agent_runtime.promotion import PromotionReadiness


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
    _git(root, "config", "user.name", "Promotion CLI Test")
    _git(root, "config", "user.email", "promotion-cli@example.invalid")
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


def _result(classification: str) -> PromotionReadiness:
    return PromotionReadiness(
        run_id="a" * 32,
        workspace="/tmp/executor",
        repository=REPOSITORY,
        candidate_branch="phase/example",
        candidate_sha="b" * 40,
        canonical_branch="youmo-clone-v2",
        canonical_sha="c" * 40,
        classification=classification,
        fast_forward_possible=classification == "READY_FAST_FORWARD",
        promotion_needed=classification == "READY_FAST_FORWARD",
        candidate_in_canonical=classification in {"ALREADY_PROMOTED", "ALREADY_INCLUDED"},
        remote_stable=True,
    )


def test_promotion_cli_ready_is_inspection_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest = _control(tmp_path)
    monkeypatch.setattr(
        promotion_cli,
        "check_promotion_readiness",
        lambda *args, **kwargs: _result("READY_FAST_FORWARD"),
    )
    rc = promotion_cli.main(
        ["--repo", str(root), "--project", str(manifest), "--run-id", "a" * 32]
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert "PROMOTION_CLASSIFICATION=READY_FAST_FORWARD" in output
    assert "CANONICAL_BRANCH_MUTATION=NONE" in output
    assert "CANDIDATE_BRANCH_MUTATION=NONE" in output
    assert "MERGE=NOT_STARTED" in output
    assert "GIT_PUSH=NOT_STARTED" in output


def test_promotion_cli_blocked_exits_nonzero_with_safe_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest = _control(tmp_path)
    monkeypatch.setattr(
        promotion_cli,
        "check_promotion_readiness",
        lambda *args, **kwargs: _result("BLOCKED_DIVERGED"),
    )
    rc = promotion_cli.main(
        ["--repo", str(root), "--project", str(manifest), "--run-id", "a" * 32]
    )
    output = capsys.readouterr().out
    assert rc == 13
    assert "PROMOTION_CLASSIFICATION=BLOCKED_DIVERGED" in output
    assert "create a fresh executor from current canonical" in output
    assert "MERGE=NOT_STARTED" in output
    assert "GIT_PUSH=NOT_STARTED" in output
