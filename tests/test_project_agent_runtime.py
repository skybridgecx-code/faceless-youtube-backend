from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.project_agent_runtime.architecture import (
    canonical_capsule_json,
    compile_context_capsule,
    load_architecture_snapshot,
)
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.gates import run_preflight_gate
from tools.project_agent_runtime.git_state import inspect_repo, normalize_github_remote


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "tooling/test")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    _git(
        root,
        "remote",
        "add",
        "origin",
        "git@github.com:skybridgecx-code/faceless-youtube-backend.git",
    )

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
        "commercial_success_contract": {
            "phase_requirements": {"I4": ["commercial_topic_ranking"]}
        },
    }
    (root / "architecture.lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )
    for relative in (
        "ARCHITECTURE.md",
        "COMMERCIAL_SUCCESS.md",
        ".agents/rules/youtube-automation-project.md",
    ):
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
        "architecture_lock": "architecture.lock.json",
        "architecture_sources": [
            "ARCHITECTURE.md",
            "COMMERCIAL_SUCCESS.md",
            ".agents/rules/youtube-automation-project.md",
        ],
        "state_dir": ".youmo",
    }
    manifest_path = root / "youmo.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root, manifest_path


def test_normalize_github_remote() -> None:
    expected = "skybridgecx-code/faceless-youtube-backend"
    assert normalize_github_remote(
        "https://github.com/skybridgecx-code/faceless-youtube-backend.git"
    ) == expected
    assert normalize_github_remote(
        "git@github.com:skybridgecx-code/faceless-youtube-backend.git"
    ) == expected
    assert normalize_github_remote(
        "ssh://git@github.com/skybridgecx-code/faceless-youtube-backend.git"
    ) == expected


def test_clean_fixture_passes_preflight_and_compiles_capsule(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    state = inspect_repo(root)
    report = run_preflight_gate(root, config, state)

    assert report.passed
    snapshot = load_architecture_snapshot(root, config)
    capsule = compile_context_capsule(config, state, snapshot)
    rendered = canonical_capsule_json(capsule)
    assert '"project"' in rendered
    assert '"no_auto_public_release"' in rendered
    assert capsule["repository_state"]["clean"] is True


def test_dirty_worktree_fails_closed(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    (root / "ARCHITECTURE.md").write_text("# changed\n", encoding="utf-8")
    state = inspect_repo(root)
    report = run_preflight_gate(root, config, state)

    checks = {check.name: check for check in report.checks}
    assert not report.passed
    assert checks["clean_worktree"].passed is False


def test_wrong_origin_fails_repository_identity(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    _git(root, "remote", "set-url", "origin", "git@github.com:someone/other.git")
    state = inspect_repo(root)
    report = run_preflight_gate(root, config, state)

    checks = {check.name: check for check in report.checks}
    assert checks["repository_identity"].passed is False
    assert not report.passed
