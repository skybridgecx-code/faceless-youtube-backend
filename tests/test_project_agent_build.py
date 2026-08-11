from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tools.project_agent_runtime.build_engine import run_guarded_build


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "phase/test")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    (root / "architecture.lock.json").write_text("{}\n", encoding="utf-8")
    (root / "ARCHITECTURE.md").write_text("# arch\n", encoding="utf-8")
    (root / "app").mkdir()
    (root / "app" / "base.py").write_text("BASE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def _turn_result() -> SimpleNamespace:
    return SimpleNamespace(
        thread_id="thr_1",
        turn_id="turn_1",
        status="completed",
        final_response="done",
    )


def _run(
    root: Path,
    runner: object,
    *,
    scopes: tuple[str, ...] = ("app",),
    max_changed: int = 20,
    validations: tuple[tuple[str, ...], ...] = (
        ("python3", "-c", "print('ok')"),
    ),
):
    return asyncio.run(
        run_guarded_build(
            workspace_root=root,
            task="Implement the test change",
            allowed_paths=scopes,
            architecture_paths=("architecture.lock.json", "ARCHITECTURE.md"),
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            turn_runner=runner,  # type: ignore[arg-type]
            max_changed_files=max_changed,
            validation_commands=validations,
        )
    )


def test_guarded_build_ready_for_audit(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**kwargs: object) -> SimpleNamespace:
        assert kwargs["sandbox_name"] == "workspace_write"
        assert kwargs["thread_id"] is None
        (root / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        return _turn_result()

    result = _run(root, runner)
    assert result.ready_for_audit
    assert result.changed_files == ("app/feature.py",)
    assert result.diff_sha256
    assert result.file_fingerprints["app/feature.py"]["kind"] == "file"


def test_guarded_build_rejects_out_of_scope_change(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**_: object) -> SimpleNamespace:
        (root / "README.md").write_text("bad\n", encoding="utf-8")
        return _turn_result()

    result = _run(root, runner)
    assert not result.ready_for_audit
    assert "out-of-scope change: README.md" in result.violations


def test_guarded_build_rejects_staging(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**_: object) -> SimpleNamespace:
        (root / "app" / "feature.py").write_text("x=1\n", encoding="utf-8")
        _git(root, "add", "app/feature.py")
        return _turn_result()

    result = _run(root, runner)
    assert any("index is not empty" in item for item in result.violations)


def test_guarded_build_rejects_head_movement(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**_: object) -> SimpleNamespace:
        (root / "app" / "feature.py").write_text("x=1\n", encoding="utf-8")
        _git(root, "add", "app/feature.py")
        _git(root, "commit", "-m", "bad")
        return _turn_result()

    result = _run(root, runner)
    assert any("HEAD changed" in item for item in result.violations)


def test_guarded_build_rejects_architecture_change(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**_: object) -> SimpleNamespace:
        (root / "app" / "feature.py").write_text("x=1\n", encoding="utf-8")
        (root / "ARCHITECTURE.md").write_text("# modified\n", encoding="utf-8")
        return _turn_result()

    result = _run(root, runner, scopes=("app", "ARCHITECTURE.md"))
    assert "architecture authority changed" in result.violations


def test_guarded_build_rejects_git_config_change(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**_: object) -> SimpleNamespace:
        (root / "app" / "feature.py").write_text("x=1\n", encoding="utf-8")
        _git(root, "config", "core.excludesfile", "/tmp/evil-ignore")
        return _turn_result()

    result = _run(root, runner)
    assert "Git control metadata changed" in result.violations


def test_guarded_build_rejects_validation_failure(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**_: object) -> SimpleNamespace:
        (root / "app" / "feature.py").write_text("x=1\n", encoding="utf-8")
        return _turn_result()

    result = _run(
        root,
        runner,
        validations=(("python3", "-c", "raise SystemExit(7)"),),
    )
    assert any("validation failed" in item for item in result.violations)


def test_guarded_build_rejects_empty_diff(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**_: object) -> SimpleNamespace:
        return _turn_result()

    result = _run(root, runner)
    assert "Codex produced no file changes" in result.violations


def test_guarded_build_enforces_changed_file_cap(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def runner(**_: object) -> SimpleNamespace:
        for index in range(2):
            (root / "app" / f"f{index}.py").write_text("x=1\n", encoding="utf-8")
        return _turn_result()

    result = _run(root, runner, max_changed=1)
    assert any("changed-file cap exceeded" in item for item in result.violations)


def test_guarded_build_rejects_symlink_escape(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    async def runner(**_: object) -> SimpleNamespace:
        os.symlink(outside, root / "app" / "escape")
        return _turn_result()

    result = _run(root, runner)
    assert any("symlink escapes workspace" in item for item in result.violations)
