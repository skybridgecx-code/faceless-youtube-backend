from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

import tools.project_agent_runtime.flow_cli as flow_cli
from tests.test_project_agent_runs import _fixture, _ready_run
from tools.project_agent_runtime.flow_cli import FlowError


RUN_ID = "a" * 32


def _fake_manifest(workspace: Path, stage: str) -> SimpleNamespace:
    return SimpleNamespace(run_id=RUN_ID, workspace=str(workspace), stage=stage)


def test_flow_new_dry_run_never_executes_child_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, _, manifest_path, _ = _fixture(tmp_path)
    calls: list[list[str]] = []

    def fake_build(argv: list[str] | None) -> int:
        values = list(argv or [])
        calls.append(values)
        assert "--execute" not in values
        print("BUILD_EXECUTION=NOT_STARTED")
        return 0

    monkeypatch.setattr(flow_cli, "build_main", fake_build)
    rc = flow_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--workspace",
            str(executor),
            "--task",
            "Implement a bounded change",
            "--allow-path",
            "app",
        ]
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 1
    assert "FLOW_MODE=DRY_RUN" in output
    assert "RUN_MANIFEST=NOT_CREATED" in output
    assert "CODEX_TRANSPORT=NOT_STARTED" in output
    assert "CANONICAL_PROMOTION=NEVER_AUTOMATIC" in output
    assert "YOUMO_PROMOTE_EXECUTE=NOT_STARTED" in output


def test_flow_full_safe_chain_stops_after_promotion_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, _, manifest_path, _ = _fixture(tmp_path)
    state = {"stage": "BUILD_RUNNING"}
    calls: list[str] = []

    def fake_build(argv: list[str] | None) -> int:
        assert "--execute" in list(argv or [])
        calls.append("build")
        state["stage"] = "READY_FOR_AUDIT"
        print(f"RUN_ID={RUN_ID}")
        return 0

    def fake_resume(argv: list[str] | None) -> int:
        assert "--execute" in list(argv or [])
        if state["stage"] == "READY_FOR_AUDIT":
            calls.append("audit")
            state["stage"] = "READY_FOR_CHECKPOINT"
        elif state["stage"] == "READY_FOR_CHECKPOINT":
            calls.append("checkpoint")
            state["stage"] = "CHECKPOINTED"
        else:
            raise AssertionError(f"unexpected stage {state['stage']}")
        return 0

    def fake_publish(argv: list[str] | None) -> int:
        assert "--execute" in list(argv or [])
        calls.append("publish")
        return 0

    def fake_promotion_check(argv: list[str] | None) -> int:
        assert "--execute" not in list(argv or [])
        calls.append("promotion-check")
        print("PROMOTION_CLASSIFICATION=READY_FAST_FORWARD")
        return 0

    def fake_load(*args: object, **kwargs: object) -> SimpleNamespace:
        return _fake_manifest(executor, state["stage"])

    def fake_safe(*args: object, **kwargs: object) -> str:
        return {
            "READY_FOR_AUDIT": "READY_FOR_AUDIT",
            "READY_FOR_CHECKPOINT": "READY_FOR_CHECKPOINT",
            "CHECKPOINTED": "CHECKPOINTED_CLEAN",
        }[state["stage"]]

    monkeypatch.setattr(flow_cli, "build_main", fake_build)
    monkeypatch.setattr(flow_cli, "resume_main", fake_resume)
    monkeypatch.setattr(flow_cli, "publish_main", fake_publish)
    monkeypatch.setattr(flow_cli, "promotion_check_main", fake_promotion_check)
    monkeypatch.setattr(flow_cli, "load_run_manifest", fake_load)
    monkeypatch.setattr(flow_cli, "_safe_existing_state", fake_safe)
    monkeypatch.setattr(flow_cli, "_verify_published", lambda *args, **kwargs: None)

    rc = flow_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--workspace",
            str(executor),
            "--task",
            "Implement a bounded change",
            "--allow-path",
            "app",
            "--execute",
        ]
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert calls == ["build", "audit", "checkpoint", "publish", "promotion-check"]
    assert "FLOW_STEPS_EXECUTED=5" in output
    assert "CANONICAL_PROMOTION=NEVER_AUTOMATIC" in output
    assert "YOUMO_PROMOTE_EXECUTE=NOT_STARTED" in output
    assert not hasattr(flow_cli, "execute_promotion")
    assert not hasattr(flow_cli, "promote_main")


def test_flow_hard_step_cap_stops_before_promotion_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, _, manifest_path, _ = _fixture(tmp_path)
    state = {"stage": "BUILD_RUNNING"}
    promo_calls = 0

    def fake_build(argv: list[str] | None) -> int:
        state["stage"] = "READY_FOR_AUDIT"
        print(f"RUN_ID={RUN_ID}")
        return 0

    def fake_resume(argv: list[str] | None) -> int:
        state["stage"] = (
            "READY_FOR_CHECKPOINT"
            if state["stage"] == "READY_FOR_AUDIT"
            else "CHECKPOINTED"
        )
        return 0

    def fake_promotion(argv: list[str] | None) -> int:
        nonlocal promo_calls
        promo_calls += 1
        return 0

    monkeypatch.setattr(flow_cli, "build_main", fake_build)
    monkeypatch.setattr(flow_cli, "resume_main", fake_resume)
    monkeypatch.setattr(flow_cli, "publish_main", lambda argv: 0)
    monkeypatch.setattr(flow_cli, "promotion_check_main", fake_promotion)
    monkeypatch.setattr(
        flow_cli,
        "load_run_manifest",
        lambda *args, **kwargs: _fake_manifest(executor, state["stage"]),
    )
    monkeypatch.setattr(
        flow_cli,
        "_safe_existing_state",
        lambda *args, **kwargs: {
            "READY_FOR_AUDIT": "READY_FOR_AUDIT",
            "READY_FOR_CHECKPOINT": "READY_FOR_CHECKPOINT",
            "CHECKPOINTED": "CHECKPOINTED_CLEAN",
        }[state["stage"]],
    )
    monkeypatch.setattr(flow_cli, "_verify_published", lambda *args, **kwargs: None)

    rc = flow_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--workspace",
            str(executor),
            "--task",
            "Implement a bounded change",
            "--allow-path",
            "app",
            "--max-steps",
            "4",
            "--execute",
        ]
    )
    output = capsys.readouterr().out
    assert rc == 15
    assert promo_calls == 0
    assert "FLOW_BOUNDED_HALT=TRUE" in output
    assert "YOUMO_PROMOTE_EXECUTE=NOT_STARTED" in output


def test_flow_stops_immediately_when_child_gate_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, _, manifest_path, run, _ = _ready_run(tmp_path)
    calls: list[str] = []

    def failing_resume(argv: list[str] | None) -> int:
        calls.append("audit")
        return 9

    monkeypatch.setattr(flow_cli, "resume_main", failing_resume)
    monkeypatch.setattr(flow_cli, "publish_main", lambda argv: (_ for _ in ()).throw(AssertionError("publish must not run")))
    monkeypatch.setattr(
        flow_cli,
        "promotion_check_main",
        lambda argv: (_ for _ in ()).throw(AssertionError("promotion check must not run")),
    )

    rc = flow_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run.run_id,
            "--execute",
        ]
    )
    output = capsys.readouterr().out
    assert rc == 9
    assert calls == ["audit"]
    assert "audit stopped the flow" in output
    assert "YOUMO_PROMOTE_EXECUTE=NOT_STARTED" in output


def test_flow_rejects_false_success_when_journal_did_not_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, executor, _, manifest_path, run, _ = _ready_run(tmp_path)
    monkeypatch.setattr(flow_cli, "resume_main", lambda argv: 0)

    rc = flow_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run.run_id,
            "--until",
            "audit",
            "--execute",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 15
    assert "returned success but journal/doctor state" in captured.err
    assert "audit post-step proof failed" in captured.out


def test_flow_existing_dry_run_does_not_execute_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, _, _, manifest_path, run, _ = _ready_run(tmp_path)
    monkeypatch.setattr(
        flow_cli,
        "resume_main",
        lambda argv: (_ for _ in ()).throw(AssertionError("resume must not execute")),
    )
    rc = flow_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            run.run_id,
        ]
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert "FLOW_MODE=DRY_RUN" in output
    assert "NEXT_SAFE_GATE=AUDIT" in output
    assert "MUTATIONS=NONE" in output


def test_flow_argument_contract_rejects_run_id_plus_new_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, _, _, manifest_path, _, _ = _ready_run(tmp_path)
    rc = flow_cli.main(
        [
            "--repo",
            str(control),
            "--project",
            str(manifest_path),
            "--run-id",
            RUN_ID,
            "--task",
            "conflicting task",
        ]
    )
    assert rc == 15
    assert "only valid when starting from --workspace" in capsys.readouterr().err


def test_flow_never_imports_canonical_promotion_executor() -> None:
    source = Path(flow_cli.__file__).read_text(encoding="utf-8")
    assert "from .promote" not in source
    assert "execute_promotion" not in source
    assert "promote_main" not in source
    assert "YOUMO_PROMOTE_EXECUTE=NOT_STARTED" in source
