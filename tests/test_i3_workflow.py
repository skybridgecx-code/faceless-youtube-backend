from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import NoReturn

import pytest
from sqlalchemy import func, inspect, select


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
TEST_DATABASE = ROOT / "test_content_factory.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE}"

from app.config import Settings  # noqa: E402
from app.db import (  # noqa: E402
    Approval,
    Artifact,
    Campaign,
    GateDecision,
    GenerationJob,
    SessionLocal,
    engine,
)
from app.models import Channel, PublishRecord  # noqa: E402
from app.workflows.campaign_workflow import (  # noqa: E402
    I3_POLICY_VERSION,
    I3_STEP_MAX_ATTEMPTS,
    I3_WORKFLOW_MAX_RECOVERY_ATTEMPTS,
    campaign_workflow_id,
    execute_stage_once,
    start_i3_campaign_workflow,
)
from app.workflows.contracts import StageRequest  # noqa: E402
from app.workflows.dbos_runtime import (  # noqa: E402
    DBOSDatabaseSeparationError,
    DBOSRuntimeError,
    launch_dbos_runtime,
    shutdown_dbos_runtime,
    validate_dbos_database_separation,
)
from app.workflows.gates import evaluate_gate  # noqa: E402
from app.workflows.persistence import (  # noqa: E402
    WorkflowReplayConflict,
    build_stage_request,
    persist_stage_result,
)
from app.workflows.providers import (  # noqa: E402
    DeterministicStubProvider,
)
from tests.db_helpers import reset_migrated_test_database  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_application_database() -> Iterator[None]:
    shutdown_dbos_runtime()
    reset_migrated_test_database()
    yield
    shutdown_dbos_runtime()


def _launch_test_runtime(system_database: Path) -> None:
    launch_dbos_runtime(
        Settings(
            database_url=str(engine.url),
            dbos_system_database_url=f"sqlite:///{system_database}",
        )
    )


def _create_campaign(*, stage: str = "topic") -> int:
    with SessionLocal() as db:
        channel = Channel(
            name="I3 workflow channel",
            niche="test",
            audience="test",
            brand_voice="test",
            visual_style="test",
        )
        db.add(channel)
        db.flush()
        campaign = Campaign(
            channel_id=channel.id,
            current_stage=stage,
            workflow_id=None,
            risk_tier="standard",
            policy_version=I3_POLICY_VERSION,
        )
        db.add(campaign)
        db.commit()
        return campaign.id


def _canonical_counts(campaign_id: int) -> dict[str, int]:
    with SessionLocal() as db:
        return {
            "approvals": db.scalar(
                select(func.count()).select_from(Approval).where(Approval.campaign_id == campaign_id)
            )
            or 0,
            "artifacts": db.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.campaign_id == campaign_id)
            )
            or 0,
            "gate_decisions": db.scalar(
                select(func.count()).select_from(GateDecision).where(
                    GateDecision.campaign_id == campaign_id
                )
            )
            or 0,
            "generation_jobs": db.scalar(
                select(func.count()).select_from(GenerationJob).where(
                    GenerationJob.campaign_id == campaign_id
                )
            )
            or 0,
            "publish_records": db.scalar(
                select(func.count()).select_from(PublishRecord).where(
                    PublishRecord.campaign_id == campaign_id
                )
            )
            or 0,
        }


def test_i3_happy_path_stops_at_human_approval_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_network(*_: object, **__: object) -> None:
        raise AssertionError("I3 attempted outbound network access")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    system_database = tmp_path / "test_dbos_happy.db"
    _launch_test_runtime(system_database)
    campaign_id = _create_campaign()

    with SessionLocal() as db:
        handle = start_i3_campaign_workflow(db, campaign_id)
    result = handle.get_result(polling_interval_sec=0.01)

    assert result["final_stage"] == "human_approval"
    assert result["workflow_id"] == campaign_workflow_id(campaign_id)
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.current_stage == "human_approval"
        assert campaign.workflow_id == campaign_workflow_id(campaign_id)
        decisions = list(
            db.scalars(
                select(GateDecision)
                .where(GateDecision.campaign_id == campaign_id)
                .order_by(GateDecision.id)
            )
        )
        jobs = list(
            db.scalars(
                select(GenerationJob)
                .where(GenerationJob.campaign_id == campaign_id)
                .order_by(GenerationJob.id)
            )
        )

    assert [decision.outcome for decision in decisions] == ["PASS"] * 8 + [
        "NEEDS_HUMAN"
    ]
    assert json.loads(decisions[-1].reasons_json) == [
        "explicit_human_approval_required"
    ]
    assert len(jobs) == 8
    assert all(job.status == "completed" for job in jobs)
    assert all(job.cost_microunits == 0 for job in jobs)
    assert _canonical_counts(campaign_id) == {
        "approvals": 0,
        "artifacts": 8,
        "gate_decisions": 9,
        "generation_jobs": 8,
        "publish_records": 0,
    }

    app_tables = set(inspect(engine).get_table_names())
    system_connection = sqlite3.connect(system_database)
    try:
        system_tables = {
            row[0]
            for row in system_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        system_connection.close()
    assert {"dbos_migrations", "workflow_status", "operation_outputs"} <= system_tables
    assert not ({"dbos_migrations", "workflow_status", "operation_outputs"} & app_tables)


def test_duplicate_workflow_start_reuses_one_logical_execution(tmp_path: Path) -> None:
    _launch_test_runtime(tmp_path / "test_dbos_duplicate.db")
    campaign_id = _create_campaign()

    with SessionLocal() as db:
        first = start_i3_campaign_workflow(db, campaign_id)
        second = start_i3_campaign_workflow(db, campaign_id)

    assert first.get_workflow_id() == second.get_workflow_id() == campaign_workflow_id(
        campaign_id
    )
    assert first.get_result(polling_interval_sec=0.01) == second.get_result(
        polling_interval_sec=0.01
    )
    assert _canonical_counts(campaign_id) == {
        "approvals": 0,
        "artifacts": 8,
        "gate_decisions": 9,
        "generation_jobs": 8,
        "publish_records": 0,
    }


def test_application_persistence_replay_reconciles_without_duplicate_or_double_advance() -> None:
    campaign_id = _create_campaign()

    first = execute_stage_once(campaign_id, "topic")
    counts_after_first = _canonical_counts(campaign_id)
    second = execute_stage_once(campaign_id, "topic")

    assert first["current_stage"] == second["current_stage"] == "research"
    assert first["generation_job_id"] == second["generation_job_id"]
    assert first["artifact_id"] == second["artifact_id"]
    assert first["gate_decision_id"] == second["gate_decision_id"]
    assert second["replayed"] is True
    assert _canonical_counts(campaign_id) == counts_after_first


def test_conflicting_gate_replay_fails_closed() -> None:
    campaign_id = _create_campaign()
    execute_stage_once(campaign_id, "topic")
    request = build_stage_request(campaign_id, "topic")
    provider_result = DeterministicStubProvider().generate(request)
    decision = evaluate_gate(request, provider_result)
    conflicting = replace(
        decision,
        outcome="FAIL",
        reasons=("conflicting_replay",),
    )

    with pytest.raises(WorkflowReplayConflict, match="gate decision conflicts"):
        persist_stage_result(request, provider_result, conflicting)


def test_policy_change_during_stage_persistence_fails_closed() -> None:
    campaign_id = _create_campaign()
    request = build_stage_request(campaign_id, "topic")
    provider_result = DeterministicStubProvider().generate(request)
    decision = evaluate_gate(request, provider_result)
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.policy_version = "changed-policy"
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="policy version changed"):
        persist_stage_result(request, provider_result, decision)


def test_fail_gate_persists_without_advancing() -> None:
    campaign_id = _create_campaign()
    request = build_stage_request(campaign_id, "topic")
    provider_result = DeterministicStubProvider().generate(request)
    decision = evaluate_gate(
        request,
        provider_result,
        hard_failures=("deterministic_hard_failure",),
    )

    persisted = persist_stage_result(request, provider_result, decision)

    assert persisted.outcome == "FAIL"
    assert persisted.current_stage == "topic"
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        gate = db.get(GateDecision, persisted.gate_decision_id)
        assert campaign is not None and campaign.current_stage == "topic"
        assert gate is not None and gate.outcome == "FAIL"


def test_human_approval_gate_needs_human_without_side_effects() -> None:
    campaign_id = _create_campaign(stage="human_approval")

    result = execute_stage_once(campaign_id, "human_approval")

    assert result["outcome"] == "NEEDS_HUMAN"
    assert result["current_stage"] == "human_approval"
    assert _canonical_counts(campaign_id) == {
        "approvals": 0,
        "artifacts": 0,
        "gate_decisions": 1,
        "generation_jobs": 0,
        "publish_records": 0,
    }


def test_stub_provider_boundary_is_immutable_and_has_no_state_access() -> None:
    campaign_id = _create_campaign()
    request = build_stage_request(campaign_id, "topic")
    before = _canonical_counts(campaign_id)

    with pytest.raises(FrozenInstanceError):
        request.stage = "release"  # type: ignore[misc]
    result = DeterministicStubProvider().generate(request)

    assert isinstance(request, StageRequest)
    assert result.provider == "stub"
    assert result.model == "i3-deterministic-v1"
    assert result.cost_microunits == 0
    assert _canonical_counts(campaign_id) == before


def test_same_database_and_hardlink_configurations_are_rejected(tmp_path: Path) -> None:
    database = tmp_path / "application.db"
    sqlite3.connect(database).close()
    same_url = f"sqlite:///{database}"

    with pytest.raises(DBOSDatabaseSeparationError):
        validate_dbos_database_separation(same_url, same_url)

    hardlink = tmp_path / "dbos-hardlink.db"
    try:
        os.link(database, hardlink)
    except OSError as exc:
        pytest.skip(f"filesystem does not support hardlink identity test: {exc}")
    with pytest.raises(DBOSDatabaseSeparationError):
        validate_dbos_database_separation(
            same_url,
            f"sqlite:///{hardlink}",
        )


def test_active_dbos_runtime_rejects_silent_system_database_switch(tmp_path: Path) -> None:
    _launch_test_runtime(tmp_path / "test_dbos_first.db")

    with pytest.raises(DBOSRuntimeError, match="different system database"):
        _launch_test_runtime(tmp_path / "test_dbos_second.db")


def test_dbos_step_retry_bound_is_finite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _launch_test_runtime(tmp_path / "test_dbos_retry.db")
    campaign_id = _create_campaign()
    attempts = 0

    def controlled_failure(
        self: DeterministicStubProvider,
        request: StageRequest,
    ) -> NoReturn:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("controlled retry failure")

    monkeypatch.setattr(DeterministicStubProvider, "generate", controlled_failure)
    with SessionLocal() as db:
        handle = start_i3_campaign_workflow(db, campaign_id)
    with pytest.raises(Exception, match="maximum of 3 retries"):
        handle.get_result(polling_interval_sec=0.01)

    assert attempts == I3_STEP_MAX_ATTEMPTS == 3
    assert I3_WORKFLOW_MAX_RECOVERY_ATTEMPTS == 3
    assert _canonical_counts(campaign_id) == {
        "approvals": 0,
        "artifacts": 0,
        "gate_decisions": 0,
        "generation_jobs": 0,
        "publish_records": 0,
    }


def _subprocess_environment(
    application_database: Path,
    system_database: Path,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": f"sqlite:///{application_database}",
            "DBOS_SYSTEM_DATABASE_URL": f"sqlite:///{system_database}",
            "OUTPUT_DIR": str(application_database.parent / "out"),
            "OPENAI_API_KEY": "",
            "YOUTUBE_DATA_API_KEY": "",
            "YOUTUBE_OAUTH_CLIENT_ID": "",
            "YOUTUBE_OAUTH_CLIENT_SECRET": "",
            "YOUTUBE_OAUTH_REFRESH_TOKEN": "",
        }
    )
    return environment


def _sqlite_campaign_counts(database: Path, campaign_id: int) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE campaign_id = ?',
                    (campaign_id,),
                ).fetchone()[0]
            )
            for table in ("artifacts", "gate_decisions", "generation_jobs")
        }
    finally:
        connection.close()


def test_real_dbos_restart_recovers_without_duplicate_application_effects(
    tmp_path: Path,
) -> None:
    application_database = tmp_path / "recovery-app.db"
    system_database = tmp_path / "test_dbos_recovery.db"
    marker = tmp_path / "research-step-entered"
    environment = _subprocess_environment(application_database, system_database)
    subprocess.run(
        [str(PYTHON), "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    connection = sqlite3.connect(application_database)
    try:
        channel_id = connection.execute(
            "INSERT INTO channels "
            "(name, niche, audience, brand_voice, visual_style, created_at) "
            "VALUES ('Recovery channel', 'test', 'test', 'test', 'test', CURRENT_TIMESTAMP)"
        ).lastrowid
        campaign_id = int(
            connection.execute(
                "INSERT INTO campaigns "
                "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
                "VALUES (?, 'topic', NULL, 'standard', 'i3-gate-v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (channel_id,),
            ).lastrowid
        )
        connection.commit()
    finally:
        connection.close()

    environment["I3_RECOVERY_MARKER"] = str(marker)
    interrupted_code = """
import os
import time
from pathlib import Path

from app.config import Settings
from app.db import SessionLocal
from app.workflows.campaign_workflow import start_i3_campaign_workflow
from app.workflows.dbos_runtime import launch_dbos_runtime
from app.workflows.providers import DeterministicStubProvider

marker = Path(os.environ["I3_RECOVERY_MARKER"])
original_generate = DeterministicStubProvider.generate

def block_research(self, request):
    if request.stage == "research":
        marker.write_text("entered", encoding="utf-8")
        while True:
            time.sleep(0.05)
    return original_generate(self, request)

DeterministicStubProvider.generate = block_research
launch_dbos_runtime(Settings())
with SessionLocal() as db:
    start_i3_campaign_workflow(db, int(os.environ["I3_CAMPAIGN_ID"]))
while True:
    time.sleep(1)
"""
    environment["I3_CAMPAIGN_ID"] = str(campaign_id)
    process = subprocess.Popen(
        [str(PYTHON), "-c", interrupted_code],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        assert marker.exists(), "workflow never reached the deterministic recovery barrier"
        assert _sqlite_campaign_counts(application_database, campaign_id) == {
            "artifacts": 1,
            "gate_decisions": 1,
            "generation_jobs": 1,
        }
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    recovery_code = """
import json
import os
from dbos import DBOS
from app.config import Settings
from app.workflows.campaign_workflow import campaign_workflow_id
from app.workflows.dbos_runtime import launch_dbos_runtime, shutdown_dbos_runtime

campaign_id = int(os.environ["I3_CAMPAIGN_ID"])
launch_dbos_runtime(Settings())
try:
    handle = DBOS.retrieve_workflow(campaign_workflow_id(campaign_id))
    result = handle.get_result(polling_interval_sec=0.01)
    print("RECOVERY_RESULT=" + json.dumps(result, sort_keys=True))
finally:
    shutdown_dbos_runtime()
"""
    recovered = subprocess.run(
        [str(PYTHON), "-c", recovery_code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    recovery_line = next(
        line for line in recovered.stdout.splitlines() if line.startswith("RECOVERY_RESULT=")
    )
    recovery_result = json.loads(recovery_line.split("=", 1)[1])

    assert recovery_result["final_stage"] == "human_approval"
    assert _sqlite_campaign_counts(application_database, campaign_id) == {
        "artifacts": 8,
        "gate_decisions": 9,
        "generation_jobs": 8,
    }
    connection = sqlite3.connect(application_database)
    try:
        assert connection.execute(
            "SELECT current_stage FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()[0] == "human_approval"
        assert connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM publish_records WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()
