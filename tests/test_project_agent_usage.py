from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_project_agent_overview import _tree_fingerprint
from tests.test_project_agent_runs import _fixture
from tools.project_agent_runtime.architecture import load_architecture_snapshot
from tools.project_agent_runtime.audit_engine import run_guarded_audit
from tools.project_agent_runtime.build_engine import (
    architecture_fingerprint,
    changed_files,
    diff_fingerprint,
    run_guarded_build,
)
from tools.project_agent_runtime.overview import build_overview
from tools.project_agent_runtime.overview_cli import main as overview_main
from tools.project_agent_runtime.run_manifest import (
    RunManifestError,
    create_run_manifest,
    resolve_evidence,
    run_directory,
    transition_run,
    write_run_evidence,
)
from tools.project_agent_runtime.usage import (
    CACHE_RATIO_DEFINITION,
    UsageError,
    aggregate_usage,
    capture_turn_usage,
    credit_rate_snapshot,
    estimate_credits,
    usage_warnings,
    warning_thresholds_from_env,
)


def _raw_usage(*, input_tokens: int, cached_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "total": {
            "total_tokens": 9999999,
            "input_tokens": 8888888,
            "cached_input_tokens": 7777777,
            "cache_write_input_tokens": 10,
            "output_tokens": 6666666,
            "reasoning_output_tokens": 5555555,
        },
        "last": {
            "total_tokens": input_tokens + output_tokens,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "cache_write_input_tokens": 7,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": 11,
        },
        "model_context_window": 200000,
    }


def test_sdk_usage_extraction_uses_last_turn_and_preserves_raw_breakdowns() -> None:
    record = capture_turn_usage(
        _raw_usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000),
        run_id="a" * 32,
        stage="build",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        timestamp="2026-08-11T20:00:00+00:00",
    )
    assert record.usage_available
    assert record.input_tokens == 1_000_000
    assert record.cached_input_tokens == 400_000
    assert record.output_tokens == 100_000
    assert record.sdk_total_tokens == 1_100_000
    assert record.sdk_reasoning_output_tokens == 11
    assert record.sdk_cache_write_input_tokens == 7
    assert record.sdk_model_context_window == 200000
    assert record.estimated_credits == 77.5
    assert record.credit_rate_snapshot.input_credits_per_million == 62.5
    assert record.credit_rate_snapshot.cached_input_credits_per_million == 6.25
    assert record.credit_rate_snapshot.output_credits_per_million == 375.0


def test_missing_sdk_usage_is_explicit_and_never_invented() -> None:
    record = capture_turn_usage(
        None,
        run_id="b" * 32,
        stage="audit",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        timestamp="2026-08-11T20:00:00+00:00",
    )
    assert not record.usage_available
    assert record.input_tokens is None
    assert record.cached_input_tokens is None
    assert record.output_tokens is None
    assert record.estimated_credits is None
    assert record.sdk_total_tokens is None


def test_credit_calculation_is_reproducible_from_captured_rate_snapshot() -> None:
    snapshot = credit_rate_snapshot("gpt-5.6-sol")
    assert snapshot.version == "openai-codex-rate-card-2026-08-11"
    assert estimate_credits(
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        output_tokens=100_000,
        snapshot=snapshot,
    ) == 155.0
    unknown = credit_rate_snapshot("gpt-unknown")
    assert estimate_credits(
        input_tokens=100,
        cached_input_tokens=10,
        output_tokens=5,
        snapshot=unknown,
    ) is None


def _architecture_paths(config) -> tuple[str, ...]:
    return (config.architecture_lock, *config.architecture_sources)


def _built_run(tmp_path: Path):
    control, executor, config, manifest_path, base = _fixture(tmp_path)
    task = "Implement telemetry persistence"
    architecture = load_architecture_snapshot(control, config)
    run = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/run-test",
        base_head=base,
        task=task,
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )

    async def build_turn(**kwargs: object) -> SimpleNamespace:
        (executor / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        return SimpleNamespace(
            thread_id="usage-build-thread",
            turn_id="usage-build-turn",
            status="completed",
            final_response="implemented",
            usage=_raw_usage(input_tokens=1000, cached_tokens=400, output_tokens=100),
        )

    result = asyncio.run(
        run_guarded_build(
            workspace_root=executor,
            task=task,
            allowed_paths=("app",),
            architecture_paths=_architecture_paths(config),
            developer_instructions="rules",
            model="gpt-5.6-terra",
            reasoning="high",
            turn_runner=build_turn,
            run_id=run.run_id,
            max_changed_files=10,
            validation_commands=(("python3", "-c", "pass"),),
        )
    )
    assert result.ready_for_audit
    binding = write_run_evidence(
        control, config, run, stage_name="build", payload=result.to_json()
    )
    ready = transition_run(
        control, config, run, new_stage="READY_FOR_AUDIT", build_evidence=binding
    )
    return control, executor, config, manifest_path, ready, result


def test_build_usage_is_persisted_inside_immutable_hash_bound_evidence(tmp_path: Path) -> None:
    control, _, config, _, run, result = _built_run(tmp_path)
    assert result.usage is not None
    assert result.usage.run_id == run.run_id
    assert result.usage.stage == "build"
    assert run.build_evidence is not None
    parent = run_directory(control, config, run.run_id)
    path = resolve_evidence(run.build_evidence, expected_parent=parent)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["usage"]["run_id"] == run.run_id
    assert payload["usage"]["stage"] == "build"
    assert payload["usage"]["input_tokens"] == 1000
    before = path.read_bytes()
    with pytest.raises(RunManifestError, match="refusing to overwrite immutable build evidence"):
        write_run_evidence(control, config, run, stage_name="build", payload="{}\n")
    assert path.read_bytes() == before


def test_audit_usage_is_persisted_separately_and_hash_bound(tmp_path: Path) -> None:
    control, executor, config, _, ready, build_result = _built_run(tmp_path)
    assert ready.build_evidence is not None
    build_path = resolve_evidence(
        ready.build_evidence,
        expected_parent=run_directory(control, config, ready.run_id),
    )
    build_payload = json.loads(build_path.read_text(encoding="utf-8"))
    expected_arch = architecture_fingerprint(control, _architecture_paths(config))

    async def audit_turn(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            thread_id="usage-audit-thread",
            turn_id="usage-audit-turn",
            status="completed",
            final_response=json.dumps(
                {"verdict": "PASS", "summary": "clean", "findings": []}
            ),
            usage=_raw_usage(input_tokens=2000, cached_tokens=1000, output_tokens=200),
        )

    running = transition_run(control, config, ready, new_stage="AUDIT_RUNNING")
    audit = asyncio.run(
        run_guarded_audit(
            workspace_root=executor,
            task=ready.task,
            build_evidence=build_payload,
            expected_architecture=expected_arch,
            developer_instructions="rules",
            model="gpt-5.6-sol",
            reasoning="high",
            turn_runner=audit_turn,
            run_id=ready.run_id,
            validation_commands=(("python3", "-c", "pass"),),
        )
    )
    assert audit.passed
    assert audit.usage is not None
    assert audit.usage.run_id == ready.run_id
    assert audit.usage.stage == "audit"
    audit_binding = write_run_evidence(
        control, config, running, stage_name="audit", payload=audit.to_json()
    )
    final = transition_run(
        control,
        config,
        running,
        new_stage="READY_FOR_CHECKPOINT",
        audit_evidence=audit_binding,
    )
    assert final.build_evidence == ready.build_evidence
    assert final.audit_evidence is not None
    audit_path = resolve_evidence(
        final.audit_evidence,
        expected_parent=run_directory(control, config, final.run_id),
    )
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["usage"]["stage"] == "audit"
    assert payload["usage"]["model"] == "gpt-5.6-sol"
    assert payload["usage"]["input_tokens"] == 2000
    assert build_result.usage is not None
    assert build_result.usage.model == "gpt-5.6-terra"


def _overview_ready_run(tmp_path: Path):
    control, executor, config, manifest_path, base = _fixture(tmp_path)
    task = "Aggregate immutable usage"
    (executor / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    files = changed_files(executor)
    diff_sha, fingerprints = diff_fingerprint(executor, files)
    task_sha = hashlib.sha256(task.encode("utf-8")).hexdigest()
    architecture = load_architecture_snapshot(control, config)
    run = create_run_manifest(
        control,
        config,
        workspace=executor,
        branch="phase/run-test",
        base_head=base,
        task=task,
        allowed_paths=("app",),
        max_changed_files=10,
        architecture_lock_sha256=architecture.lock_sha256,
    )
    build_usage = capture_turn_usage(
        _raw_usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000),
        run_id=run.run_id,
        stage="build",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        timestamp="2026-08-11T20:00:00+00:00",
    )
    build = {
        "status": "READY_FOR_AUDIT",
        "ready_for_audit": True,
        "base_head": base,
        "branch": "phase/run-test",
        "task_sha256": task_sha,
        "allowed_paths": ["app"],
        "changed_files": list(files),
        "diff_sha256": diff_sha,
        "file_fingerprints": fingerprints,
        "validations": [],
        "violations": [],
        "usage": build_usage.to_mapping(),
    }
    build_binding = write_run_evidence(
        control,
        config,
        run,
        stage_name="build",
        payload=json.dumps(build, indent=2, sort_keys=True) + "\n",
    )
    ready = transition_run(
        control, config, run, new_stage="READY_FOR_AUDIT", build_evidence=build_binding
    )
    running = transition_run(control, config, ready, new_stage="AUDIT_RUNNING")
    audit_usage = capture_turn_usage(
        _raw_usage(input_tokens=2_000_000, cached_tokens=1_000_000, output_tokens=200_000),
        run_id=run.run_id,
        stage="audit",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        timestamp="2026-08-11T21:00:00+00:00",
    )
    audit = {
        "status": "AUDIT_PASS",
        "passed": True,
        "verdict": "PASS",
        "summary": "clean",
        "findings": [],
        "base_head": base,
        "branch": "phase/run-test",
        "task_sha256": task_sha,
        "diff_sha256": diff_sha,
        "validations": [],
        "violations": [],
        "usage": audit_usage.to_mapping(),
    }
    audit_binding = write_run_evidence(
        control,
        config,
        running,
        stage_name="audit",
        payload=json.dumps(audit, indent=2, sort_keys=True) + "\n",
    )
    final = transition_run(
        control,
        config,
        running,
        new_stage="READY_FOR_CHECKPOINT",
        audit_evidence=audit_binding,
    )
    return control, config, manifest_path, final


def test_overview_aggregates_run_project_models_cache_ratio_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, config, _, run = _overview_ready_run(tmp_path)
    monkeypatch.setenv("YOUMO_WARN_TURN_CREDITS", "50")
    monkeypatch.setenv("YOUMO_WARN_RUN_CREDITS", "100")
    report = build_overview(control, config)
    assert report.cache_ratio_definition == CACHE_RATIO_DEFINITION
    assert report.usage_all_time.input_tokens == 3_000_000
    assert report.usage_all_time.cached_input_tokens == 1_400_000
    assert report.usage_all_time.output_tokens == 300_000
    assert report.usage_all_time.cache_ratio == round(1_400_000 / 3_000_000, 6)
    assert report.usage_all_time.estimated_credits == 365.0
    assert report.usage_all_time.estimated_credits_complete
    assert set(report.usage_by_model) == {"gpt-5.6-sol", "gpt-5.6-terra"}
    assert report.usage_by_model["gpt-5.6-terra"].estimated_credits == 77.5
    assert report.usage_by_model["gpt-5.6-sol"].estimated_credits == 287.5
    assert len(report.items) == 1
    item = report.items[0]
    assert item.run_id == run.run_id
    assert item.build_usage is not None and item.build_usage.stage == "build"
    assert item.audit_usage is not None and item.audit_usage.stage == "audit"
    assert item.usage_total.estimated_credits == 365.0
    assert len(item.usage_warnings) == 3
    assert len(report.usage_warnings) == 3


def test_overview_json_usage_is_machine_readable_and_does_not_mutate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control, config, manifest_path, run = _overview_ready_run(tmp_path)
    state_root = Path(config.state_dir).expanduser().resolve()
    before = _tree_fingerprint(state_root)
    monkeypatch.setenv("YOUMO_WARN_TURN_CREDITS", "50")
    rc = overview_main(
        ["--repo", str(control), "--project", str(manifest_path), "--json"]
    )
    output = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert output["cache_ratio_definition"] == CACHE_RATIO_DEFINITION
    assert output["usage_all_time"]["input_tokens"] == 3_000_000
    assert output["usage_by_model"]["gpt-5.6-terra"]["estimated_credits"] == 77.5
    assert output["items"][0]["run_id"] == run.run_id
    assert output["items"][0]["build_usage"]["credit_rate_snapshot"]["version"] == (
        "openai-codex-rate-card-2026-08-11"
    )
    assert _tree_fingerprint(state_root) == before


def test_warning_thresholds_are_configurable_warnings_not_caps() -> None:
    record = capture_turn_usage(
        _raw_usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000),
        run_id="c" * 32,
        stage="build",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        timestamp="2026-08-11T20:00:00+00:00",
    )
    thresholds = warning_thresholds_from_env(
        {"YOUMO_WARN_TURN_CREDITS": "70", "YOUMO_WARN_RUN_CREDITS": "75"}
    )
    warnings = usage_warnings((record,), thresholds)
    assert len(warnings) == 2
    assert "warning threshold" in warnings[0]
    with pytest.raises(UsageError):
        warning_thresholds_from_env({"YOUMO_WARN_TURN_CREDITS": "not-a-number"})
    # Threshold evaluation is observational; the immutable record is unchanged.
    assert aggregate_usage((record,)).estimated_credits == 77.5
