from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tools.project_agent_runtime.safe_build import run_fast_guarded_build


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


class _WriteFeature:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def __call__(self, **kwargs: object) -> object:
        (self.root / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        return SimpleNamespace(
            thread_id="thread-cleanup",
            turn_id="turn-cleanup",
            status="completed",
            final_response="done",
        )


def test_fast_build_removes_only_disposable_test_caches(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "phase/cache-cleanup")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    (root / ".gitignore").write_text(
        ".pytest_cache/\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    (root / "architecture.lock.json").write_text("{}\n", encoding="utf-8")
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("\n", encoding="utf-8")
    (root / "app" / "base.py").write_text("BASE = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_feature.py").write_text(
        "from app.feature import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    result = asyncio.run(
        run_fast_guarded_build(
            workspace_root=root,
            task="add feature",
            allowed_paths=("app",),
            architecture_paths=("architecture.lock.json",),
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            turn_runner=_WriteFeature(root),
            max_changed_files=5,
            validation_venv=None,
        )
    )

    assert result.ready_for_audit
    assert any("pytest" in item.argv for item in result.validations)
    assert not (root / ".pytest_cache").exists()
    assert not any(path.name == "__pycache__" for path in root.rglob("__pycache__"))
    assert not any(path.suffix == ".pyc" for path in root.rglob("*.pyc"))
