from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from tools.project_agent_runtime.architecture import compile_context_capsule, load_architecture_snapshot
from tools.project_agent_runtime.codex_transport import CodexTransportError, run_codex_turn
from tools.project_agent_runtime.config import ProjectConfigError, load_project_config
from tools.project_agent_runtime.gates import run_preflight_gate
from tools.project_agent_runtime.git_state import inspect_repo, normalize_github_remote
from tools.project_agent_runtime.prompting import developer_instructions
from tools.project_agent_runtime.state import (
    RuntimeStateError,
    advance_runtime_state,
    load_runtime_state,
    resolve_resume_thread,
    save_runtime_state,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "tooling/test")
    _git(root, "config", "user.name", "Runtime Test")
    _git(root, "config", "user.email", "runtime@example.invalid")
    _git(root, "remote", "add", "origin", "git@github.com:skybridgecx-code/faceless-youtube-backend.git")
    lock = {
        "schema_version": 3,
        "architecture_status": "target_during_controlled_migration",
        "current_repository": {"repository": "skybridgecx-code/faceless-youtube-backend", "branch": "youmo-clone-v2"},
        "runtime_stages": ["topic", "research", "release"],
        "hard_invariants": ["no_auto_public_release"],
        "forbidden_v1": ["dynamic_agent_swarms"],
        "migration_rules": ["evolve_existing_repository"],
        "commercial_success_contract": {"phase_requirements": {"I4": ["commercial_topic_ranking"]}},
    }
    (root / "architecture.lock.json").write_text(json.dumps(lock) + "\n", encoding="utf-8")
    for relative in ("ARCHITECTURE.md", "COMMERCIAL_SUCCESS.md", ".agents/rules/youtube-automation-project.md"):
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
        "architecture_sources": ["ARCHITECTURE.md", "COMMERCIAL_SUCCESS.md", ".agents/rules/youtube-automation-project.md"],
        "state_dir": ".youmo",
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
    _git(root, "commit", "-m", "fixture")
    return root, manifest_path


def test_manifest_codex_policy_and_capsule(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    assert config.codex.implementation_model == "gpt-5.6-terra"
    assert config.codex.audit_model == "gpt-5.6-sol"
    state = inspect_repo(root)
    assert run_preflight_gate(root, config, state).passed
    capsule = compile_context_capsule(config, state, load_architecture_snapshot(root, config))
    assert capsule["codex_policy"]["implementation_reasoning"] == "high"


def test_manifest_rejects_invalid_reasoning(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["codex"]["audit_reasoning"] = "maximum"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    try:
        load_project_config(root, manifest)
    except ProjectConfigError as exc:
        assert "audit_reasoning" in str(exc)
    else:
        raise AssertionError("invalid reasoning must fail")


def test_runtime_state_round_trip(tmp_path: Path) -> None:
    root, _ = _fixture_repo(tmp_path)
    state = load_runtime_state(root, ".youmo", project_id="youmo")
    assert state.thread_id is None
    updated = advance_runtime_state(
        state,
        thread_id="thr_123",
        turn_id="turn_456",
        turn_status="completed",
        head="abc",
        architecture_lock_sha256="def",
    )
    save_runtime_state(root, ".youmo", updated)
    loaded = load_runtime_state(root, ".youmo", project_id="youmo")
    assert loaded.thread_id == "thr_123"
    assert loaded.last_turn_id == "turn_456"


@dataclass
class _FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 4

    def model_dump(self, mode: str = "python") -> dict[str, int]:
        assert mode == "json"
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


class _FakeThread:
    def __init__(self, thread_id: str) -> None:
        self.id = thread_id
        self.calls: list[dict[str, object]] = []

    async def run(self, prompt: str, **kwargs: object) -> object:
        self.calls.append({"prompt": prompt, **kwargs})
        return SimpleNamespace(
            id="turn_1",
            status=SimpleNamespace(value="completed"),
            final_response="plan",
            usage=_FakeUsage(),
        )


class _FakeCodex:
    started: list[dict[str, object]] = []
    resumed: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> "_FakeCodex":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def thread_start(self, **kwargs: object) -> _FakeThread:
        self.started.append(kwargs)
        return _FakeThread("thr_new")

    async def thread_resume(self, thread_id: str, **kwargs: object) -> _FakeThread:
        self.resumed.append((thread_id, kwargs))
        return _FakeThread(thread_id)


class _FakeSdk:
    AsyncCodex = _FakeCodex
    Sandbox = SimpleNamespace(read_only="READ", workspace_write="WRITE")


def test_codex_transport_starts_read_only_thread(tmp_path: Path) -> None:
    result = asyncio.run(
        run_codex_turn(
            repo_root=tmp_path,
            prompt="plan",
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            sandbox_name="read_only",
            sdk=_FakeSdk,
        )
    )
    assert result.thread_id == "thr_new"
    assert result.turn_id == "turn_1"
    assert result.status == "completed"
    assert result.usage == {"input_tokens": 10, "output_tokens": 4}
    assert _FakeCodex.started[-1]["sandbox"] == "READ"
    assert _FakeCodex.started[-1]["config"] == {"model_reasoning_effort": "high"}


def test_codex_transport_resumes_saved_thread(tmp_path: Path) -> None:
    result = asyncio.run(
        run_codex_turn(
            repo_root=tmp_path,
            prompt="continue",
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            sandbox_name="read_only",
            thread_id="thr_saved",
            sdk=_FakeSdk,
        )
    )
    assert result.thread_id == "thr_saved"
    assert _FakeCodex.resumed[-1][0] == "thr_saved"


def test_codex_transport_rejects_full_access(tmp_path: Path) -> None:
    try:
        asyncio.run(
            run_codex_turn(
                repo_root=tmp_path,
                prompt="x",
                developer_instructions="y",
                model="gpt-5.6-terra",
                reasoning="high",
                sandbox_name="full_access",
                sdk=_FakeSdk,
            )
        )
    except CodexTransportError as exc:
        assert "unsupported sandbox" in str(exc)
    else:
        raise AssertionError("full access must be rejected")


def test_plan_instructions_are_read_only(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    state = inspect_repo(root)
    capsule = compile_context_capsule(config, state, load_architecture_snapshot(root, config))
    text = developer_instructions(capsule, mode="plan")
    assert "Read-only" in text
    assert "Do not modify files" in text
    assert "architecture.lock.json" in text


def test_resume_thread_fails_closed_on_head_change(tmp_path: Path) -> None:
    root, _ = _fixture_repo(tmp_path)
    state = advance_runtime_state(
        load_runtime_state(root, ".youmo", project_id="youmo"),
        thread_id="thr_123",
        turn_id="turn_1",
        turn_status="completed",
        head="old-head",
        architecture_lock_sha256="lock-1",
    )
    try:
        resolve_resume_thread(
            state,
            current_head="new-head",
            architecture_lock_sha256="lock-1",
            fresh_thread=False,
        )
    except RuntimeStateError as exc:
        assert "HEAD" in str(exc)
    else:
        raise AssertionError("stale thread must fail closed")


def test_fresh_thread_discards_stale_resume_state(tmp_path: Path) -> None:
    root, _ = _fixture_repo(tmp_path)
    state = advance_runtime_state(
        load_runtime_state(root, ".youmo", project_id="youmo"),
        thread_id="thr_123",
        turn_id="turn_1",
        turn_status="completed",
        head="old-head",
        architecture_lock_sha256="old-lock",
    )
    assert resolve_resume_thread(
        state,
        current_head="new-head",
        architecture_lock_sha256="new-lock",
        fresh_thread=True,
    ) is None


def test_normalize_github_remote() -> None:
    expected = "skybridgecx-code/faceless-youtube-backend"
    assert normalize_github_remote("https://github.com/skybridgecx-code/faceless-youtube-backend.git") == expected
    assert normalize_github_remote("git@github.com:skybridgecx-code/faceless-youtube-backend.git") == expected
    assert normalize_github_remote("ssh://git@github.com/skybridgecx-code/faceless-youtube-backend.git") == expected


def test_dirty_worktree_fails_closed(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    (root / "ARCHITECTURE.md").write_text("# changed\n", encoding="utf-8")
    report = run_preflight_gate(root, config, inspect_repo(root))
    checks = {check.name: check for check in report.checks}
    assert not report.passed
    assert checks["clean_worktree"].passed is False


def test_wrong_origin_fails_repository_identity(tmp_path: Path) -> None:
    root, manifest = _fixture_repo(tmp_path)
    config = load_project_config(root, manifest)
    _git(root, "remote", "set-url", "origin", "git@github.com:someone/other.git")
    report = run_preflight_gate(root, config, inspect_repo(root))
    checks = {check.name: check for check in report.checks}
    assert checks["repository_identity"].passed is False
    assert not report.passed
