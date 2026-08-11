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
    _install_launchers,
    default_layout,
    inspect_controller,
    install_controller,
)
from tools.project_agent_runtime.controller_cli import main as controller_main


REPOSITORY = "skybridgecx-code/faceless-youtube-backend"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}.git"
SOURCE_REF = "tooling/source"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _synthetic_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", SOURCE_REF)
    _git(source, "config", "user.name", "Controller Test")
    _git(source, "config", "user.email", "controller@example.invalid")
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
    manifest_path = source / "tools" / "project_agent_runtime" / "projects" / "youmo.json"
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
    (source / "architecture.lock.json").write_text(
        json.dumps(lock) + "\n", encoding="utf-8"
    )
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
    for name in (
        "youmo",
        "youmo-build",
        "youmo-audit",
        "youmo-checkpoint",
        "youmo-controller",
    ):
        (scripts / name).write_text(
            "#!/usr/bin/env python3\nprint('synthetic controller launcher')\n",
            encoding="utf-8",
        )

    _git(source, "add", ".")
    _git(source, "commit", "-m", "synthetic controller source")
    return source, _git(source, "rev-parse", "HEAD")


def _installed(tmp_path: Path):
    source, sha = _synthetic_source(tmp_path)
    layout = default_layout(tmp_path / "controller")
    source_head = _git(source, "rev-parse", "HEAD")
    source_status = _git(source, "status", "--porcelain=v1", "-uall")

    status = install_controller(
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


def test_controller_install_is_independent_ready_and_push_disabled(tmp_path: Path) -> None:
    source, sha, layout, status = _installed(tmp_path)

    assert status.installed
    assert status.ready
    assert status.head == sha
    assert status.expected_head == sha
    assert status.branch == "tooling/controller-runtime"
    assert status.clean is True
    assert status.dependencies_ready
    assert status.launchers_ready
    assert status.push_disabled
    assert layout.repo.resolve() != source.resolve()
    assert layout.venv.parent == layout.home
    assert layout.venv not in layout.repo.parents
    assert _git(layout.repo, "remote", "get-url", "origin") == REPOSITORY_URL
    assert _git(layout.repo, "remote", "get-url", "--push", "origin") == DISABLED_PUSH_URL

    metadata = json.loads(layout.metadata.read_text(encoding="utf-8"))
    dependencies = json.loads(layout.dependency_stamp.read_text(encoding="utf-8"))
    assert metadata["commit_sha"] == sha
    assert metadata["repo_path"] == str(layout.repo)
    assert dependencies["venv_path"] == str(layout.venv.resolve())
    assert dependencies["python_path"] == str((layout.venv / "bin" / "python").absolute())

    for name in (
        "youmo",
        "youmo-build",
        "youmo-audit",
        "youmo-checkpoint",
        "youmo-controller",
    ):
        wrapper = layout.bin / name
        body = wrapper.read_text(encoding="utf-8")
        assert MANAGED_LAUNCHER_MARKER in body
        assert f"YOUMO_VALIDATION_VENV={layout.venv}" in body
        assert str(layout.repo / "scripts" / name) in body
        assert str(layout.venv / "bin" / "python") in body


def test_controller_readiness_fails_if_push_is_reenabled(tmp_path: Path) -> None:
    _, _, layout, status = _installed(tmp_path)
    assert status.ready
    _git(layout.repo, "remote", "set-url", "--push", "origin", REPOSITORY_URL)

    observed = inspect_controller(layout)
    assert observed.installed
    assert not observed.ready
    assert not observed.push_disabled


def test_controller_install_rejects_nested_destination_before_clone(tmp_path: Path) -> None:
    source, sha = _synthetic_source(tmp_path)
    layout = default_layout(source / ".youmo-controller")

    with pytest.raises(ControllerError, match="must not live inside"):
        install_controller(
            expected_sha=sha,
            source_ref=SOURCE_REF,
            layout=layout,
            source_root=source,
            repository_url=REPOSITORY_URL,
            clone_source_url=str(source),
            python_executable=sys.executable,
        )
    assert not layout.home.exists()
    assert _git(source, "status", "--porcelain=v1", "-uall") == ""


def test_controller_install_rejects_bad_sha_without_mutation(tmp_path: Path) -> None:
    source, _ = _synthetic_source(tmp_path)
    layout = default_layout(tmp_path / "controller")

    with pytest.raises(ControllerError, match="40-character"):
        install_controller(
            expected_sha="abc",
            source_ref=SOURCE_REF,
            layout=layout,
            source_root=source,
            repository_url=REPOSITORY_URL,
            clone_source_url=str(source),
            python_executable=sys.executable,
        )
    assert not layout.home.exists()


def test_controller_second_install_never_replaces_existing_install(tmp_path: Path) -> None:
    source, sha, layout, status = _installed(tmp_path)
    assert status.ready
    old_head = _git(layout.repo, "rev-parse", "HEAD")

    with pytest.raises(ControllerError, match="already exists or is partial"):
        install_controller(
            expected_sha=sha,
            source_ref=SOURCE_REF,
            layout=layout,
            source_root=source,
            repository_url=REPOSITORY_URL,
            clone_source_url=str(source),
            python_executable=sys.executable,
        )
    assert _git(layout.repo, "rev-parse", "HEAD") == old_head


def test_managed_launcher_install_refuses_unmanaged_collision(tmp_path: Path) -> None:
    _, _, layout, status = _installed(tmp_path)
    assert status.ready
    target = layout.bin / "youmo-build"
    target.write_text("#!/bin/sh\necho user-owned\n", encoding="utf-8")

    with pytest.raises(ControllerError, match="unmanaged launcher"):
        _install_launchers(layout)
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho user-owned\n"


def test_controller_cli_install_dry_run_is_non_mutating(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "controller"
    rc = controller_main(
        ["--home", str(home), "install", "--sha", "a" * 40, "--ref", "tooling/example"]
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert "CONTROLLER_INSTALL=DRY_RUN" in captured.out
    assert "CONTROLLER_PUSH_POLICY=DISABLED" in captured.out
    assert "ACTIVE_PROJECT_CHECKOUT_MUTATION=NONE" in captured.out
    assert not home.exists()


def test_controller_cli_rejects_invalid_sha_in_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "controller"
    rc = controller_main(["--home", str(home), "install", "--sha", "short"])
    captured = capsys.readouterr()

    assert rc == 3
    assert "40-character" in captured.err
    assert not home.exists()
