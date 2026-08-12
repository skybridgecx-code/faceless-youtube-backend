from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.project_agent_runtime.audit_engine import AuditGuardError, run_guarded_audit
from tools.project_agent_runtime.build_engine import (
    architecture_fingerprint,
    changed_files,
    diff_fingerprint,
)


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
    (root / "app" / "base.py").write_text("BASE=1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def _build_evidence(root: Path, task: str = "Implement feature") -> dict[str, object]:
    (root / "app" / "feature.py").write_text("VALUE=2\n", encoding="utf-8")
    files = changed_files(root)
    diff_sha, fingerprints = diff_fingerprint(root, files)
    return {
        "status": "READY_FOR_AUDIT",
        "ready_for_audit": True,
        "base_head": _git(root, "rev-parse", "HEAD"),
        "branch": _git(root, "branch", "--show-current"),
        "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
        "allowed_paths": ["app"],
        "changed_files": list(files),
        "diff_sha256": diff_sha,
        "file_fingerprints": fingerprints,
        "violations": [],
    }


def _expected_arch(root: Path) -> dict[str, str]:
    return architecture_fingerprint(
        root, ("architecture.lock.json", "ARCHITECTURE.md")
    )


def _turn_result(payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        thread_id="thr_audit",
        turn_id="turn_audit",
        status="completed",
        final_response=json.dumps(payload),
    )


def _run(
    root: Path,
    evidence: dict[str, object],
    runner: object,
    *,
    task: str = "Implement feature",
    validations: tuple[tuple[str, ...], ...] = (
        ("python3", "-c", "print('ok')"),
    ),
):
    return asyncio.run(
        run_guarded_audit(
            workspace_root=root,
            task=task,
            build_evidence=evidence,
            expected_architecture=_expected_arch(root),
            developer_instructions="audit rules",
            model="gpt-5.6-sol",
            reasoning="high",
            turn_runner=runner,  # type: ignore[arg-type]
            validation_commands=validations,
        )
    )


def test_guarded_audit_passes_clean_evidence(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)

    async def runner(**kwargs: object) -> SimpleNamespace:
        assert kwargs["sandbox_name"] == "read_only"
        assert kwargs["thread_id"] is None
        return _turn_result(
            {"verdict": "PASS", "summary": "Correct", "findings": []}
        )

    result = _run(root, evidence, runner)
    assert result.passed
    assert result.verdict == "PASS"


def test_guarded_audit_rejects_task_hash_mismatch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)

    async def runner(**_: object) -> SimpleNamespace:
        raise AssertionError("runner should not execute")

    with pytest.raises(AuditGuardError, match="task text"):
        _run(root, evidence, runner, task="Different task")


def test_guarded_audit_rejects_stale_diff(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)
    (root / "app" / "feature.py").write_text("VALUE=3\n", encoding="utf-8")

    async def runner(**_: object) -> SimpleNamespace:
        raise AssertionError("runner should not execute")

    with pytest.raises(AuditGuardError, match="diff fingerprint"):
        _run(root, evidence, runner)


def test_guarded_audit_rejects_malformed_json(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)

    async def runner(**_: object) -> SimpleNamespace:
        return SimpleNamespace(
            thread_id="t", turn_id="u", status="completed", final_response="PASS"
        )

    result = _run(root, evidence, runner)
    assert not result.passed
    assert any("strict JSON" in item for item in result.violations)


def test_guarded_audit_fails_on_fail_verdict(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)

    async def runner(**_: object) -> SimpleNamespace:
        return _turn_result(
            {
                "verdict": "FAIL",
                "summary": "Bug",
                "findings": [
                    {
                        "severity": "high",
                        "message": "broken",
                        "path": "app/feature.py",
                    }
                ],
            }
        )

    result = _run(root, evidence, runner)
    assert not result.passed
    assert "independent semantic auditor returned FAIL" in result.violations


def test_guarded_audit_rejects_pass_with_high_finding(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)

    async def runner(**_: object) -> SimpleNamespace:
        return _turn_result(
            {
                "verdict": "PASS",
                "summary": "looks ok",
                "findings": [
                    {
                        "severity": "high",
                        "message": "actually broken",
                        "path": None,
                    }
                ],
            }
        )

    result = _run(root, evidence, runner)
    assert not result.passed
    assert any("PASS verdict cannot" in item for item in result.violations)


def test_guarded_audit_detects_auditor_mutation(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)

    async def runner(**_: object) -> SimpleNamespace:
        (root / "app" / "feature.py").write_text("MUTATED=1\n", encoding="utf-8")
        return _turn_result(
            {"verdict": "PASS", "summary": "Correct", "findings": []}
        )

    result = _run(root, evidence, runner)
    assert not result.passed
    assert any("mutated or became stale" in item for item in result.violations)


def test_guarded_audit_validation_failure_blocks_model(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)
    called = False

    async def runner(**_: object) -> SimpleNamespace:
        nonlocal called
        called = True
        return _turn_result(
            {"verdict": "PASS", "summary": "Correct", "findings": []}
        )

    result = _run(
        root,
        evidence,
        runner,
        validations=(("python3", "-c", "raise SystemExit(9)"),),
    )
    assert not called
    assert not result.passed
    assert any("deterministic validation failed" in item for item in result.violations)


def test_guarded_audit_rejects_staged_state(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)
    _git(root, "add", "app/feature.py")

    async def runner(**_: object) -> SimpleNamespace:
        raise AssertionError("runner should not execute")

    with pytest.raises(AuditGuardError, match="index is not empty"):
        _run(root, evidence, runner)


def test_guarded_audit_rejects_active_exclude_pattern(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evidence = _build_evidence(root)
    git_dir = Path(_git(root, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("hidden.tmp\n", encoding="utf-8")

    async def runner(**_: object) -> SimpleNamespace:
        raise AssertionError("runner should not execute")

    with pytest.raises(AuditGuardError, match="exclude contains active patterns"):
        _run(root, evidence, runner)
