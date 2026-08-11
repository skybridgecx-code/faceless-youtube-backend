from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.project_agent_runtime.controller import (
    DISABLED_PUSH_URL,
    MANAGED_LAUNCHER_MARKER,
    ControllerError,
    default_layout,
)
from tools.project_agent_runtime.controller_release import (
    EXTRA_LAUNCHERS,
    inspect_controller_release,
    install_controller_release,
)


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}.git"
SOURCE_REF = "tooling/source"
ALL_LAUNCHERS = (
    "youmo",
    "youmo-build",
    "youmo-audit",
    "youmo-checkpoint",
    "youmo-controller",
    "youmo-doctor",
    "youmo-resume",
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _synthetic_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", SOURCE_REF)
    _git(source, "config", "user.name", "Release Test")
    _git(source, "config", "user.email", "release@example.invalid")
    _git(source, "remote", "add", "origin", REPOSITORY_URL)

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
    manifest_path = source / "tools/project_agent_runtime/projects/youmo.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    lock = {
        "schema_version": 3,
        "architecture_status": "target_during_controlled_migration",
        "current_repository": {
            "repository": REPOSITORY,
            "branch": "youmo-clone-v2",
        },
        "runtime_stages": [],
        "hard_invariants": ["no_auto_public_release"],
        "forbidden_v1": [],
        "migration_rules": [],
        "commercial_success_contract": {"phase_requirements": {}},
    }
    (source / "architecture.lock.json").write_text(json.dumps(lock) + "\n", encoding="utf-8")
    for relative in (
        "ARCHITECTURE.md",
        "COMMERCIAL_SUCCESS.md",
        ".agents/rules/youtube-automation-project.md",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")

    (source / "requirements.txt").write_text("", encoding="utf-8")
    (source / "requirements-youmo-cli.txt").write_text("", encoding="utf-8")
    scripts = source / "scripts"
    scripts.mkdir()
    for name in ALL_LAUNCHERS:
        (scripts / name).write_text(
            "#!/usr/bin/env python3\nprint('synthetic release launcher')\n",
            encoding="utf-8",
        )

    _git(source, "add", ".")
    _git(source, "commit", "-m", "synthetic release")
    return source, _git(source, "rev-parse", "HEAD")


def _install(tmp_path: Path):
    source, sha = _synthetic_source(tmp_path)
    layout = default_layout(tmp_path / "controller")
    source_head = _git(source, "rev-parse", "HEAD")
    source_status = _git(source, "status", "--porcelain=v1", "-uall")
    status = install_controller_release(
        expected_sha=sha,
        source_ref=SOURCE_REF,
        layout=layout,
        source_root=source,
        repository_url=REPOSITORY_URL,
        clone_source_url=str(source),
        python_executable=sys.executable,
    )
    assert _git(source, "rev-parse", "HEAD") == source_head
    assert _git(source, "status", "--porcelain=v1", "-uall") == source_status
    return source, sha, layout, status


def test_release_install_manages_all_user_facing_launchers(tmp_path: Path) -> None:
    source, sha, layout, status = _install(tmp_path)
    assert status.ready
    assert status.head == sha
    assert status.launchers_ready
    assert status.push_disabled
    assert _git(layout.repo, "remote", "get-url", "--push", "origin") == DISABLED_PUSH_URL
    assert layout.repo.resolve() != source.resolve()

    for name in ALL_LAUNCHERS:
        target = layout.bin / name
        assert target.is_file()
        body = target.read_text(encoding="utf-8")
        assert MANAGED_LAUNCHER_MARKER in body
        assert str(layout.repo / "scripts" / name) in body
        assert str(layout.venv / "bin" / "python") in body


def test_release_readiness_fails_if_doctor_or_resume_wrapper_is_missing(tmp_path: Path) -> None:
    _, _, layout, status = _install(tmp_path)
    assert status.ready
    assert EXTRA_LAUNCHERS == ("youmo-doctor", "youmo-resume")

    (layout.bin / "youmo-doctor").unlink()
    observed = inspect_controller_release(layout)
    assert not observed.ready
    assert not observed.launchers_ready
    assert "doctor/resume" in observed.detail


def test_release_preflight_refuses_unmanaged_extra_launcher_before_clone(tmp_path: Path) -> None:
    source, sha = _synthetic_source(tmp_path)
    layout = default_layout(tmp_path / "controller")
    layout.bin.mkdir(parents=True)
    collision = layout.bin / "youmo-resume"
    collision.write_text("#!/bin/sh\necho user-owned\n", encoding="utf-8")

    with pytest.raises(ControllerError, match="unmanaged launcher"):
        install_controller_release(
            expected_sha=sha,
            source_ref=SOURCE_REF,
            layout=layout,
            source_root=source,
            repository_url=REPOSITORY_URL,
            clone_source_url=str(source),
            python_executable=sys.executable,
        )

    assert not layout.repo.exists()
    assert not layout.venv.exists()
    assert collision.read_text(encoding="utf-8") == "#!/bin/sh\necho user-owned\n"


def test_bootstrap_accepts_home_before_install_options_without_mutation(tmp_path: Path) -> None:
    home = tmp_path / "custom-controller"
    script = Path(__file__).resolve().parents[1] / "scripts" / "youmo-bootstrap"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--home",
            str(home),
            "--sha",
            "a" * 40,
            "--ref",
            "tooling/example",
        ],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "CONTROLLER_INSTALL=DRY_RUN" in completed.stdout
    assert f"CONTROLLER_REPO={home.resolve() / 'repo'}" in completed.stdout
    assert "MANAGED_LAUNCHERS=youmo,youmo-build,youmo-audit,youmo-checkpoint,youmo-controller,youmo-doctor,youmo-resume" in completed.stdout
    assert "ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE" in completed.stdout
    assert not home.exists()
