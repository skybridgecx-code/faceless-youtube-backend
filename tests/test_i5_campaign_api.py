from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = ROOT / "test_content_factory.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE}"

from dbos import DBOS  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

import app.editorial.persistence as editorial_persistence  # noqa: E402
import app.routers.campaigns as campaigns_router  # noqa: E402
import app.workflows.i5_production_workflow as i5_workflow  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import (  # noqa: E402
    Artifact,
    Campaign,
    CampaignBudgetOverride,
    GateDecision,
    SessionLocal,
    engine,
)
from app.editorial.claims import compile_research_packet  # noqa: E402
from app.editorial.contracts import I4_POLICY_VERSION, canonical_sha256  # noqa: E402
from app.editorial.script_compiler import compile_script_packet  # noqa: E402
from app.editorial.topic_intelligence import compile_topic_packet  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Channel  # noqa: E402
from app.production.budget import load_campaign_budget_policy  # noqa: E402
from app.production.persistence import I5_PRODUCTION_PROFILE_KIND  # noqa: E402
from app.production.renderer import RendererError, ToolVersions  # noqa: E402
from app.workflows.dbos_runtime import (  # noqa: E402
    launch_dbos_runtime,
    shutdown_dbos_runtime,
)
from tests.db_helpers import reset_migrated_test_database  # noqa: E402
from tests.i4_test_data import happy_demand, happy_seed  # noqa: E402


TEST_OPENAI_KEY = "test-only-i5-openai-key"
TEST_FFMPEG_VERSION = "ffmpeg version i5-api-test"
TEST_FFPROBE_VERSION = "ffprobe version i5-api-test"


class _FakeWorkflowHandle:
    def __init__(self, workflow_id: str) -> None:
        self._workflow_id = workflow_id

    def get_workflow_id(self) -> str:
        return self._workflow_id

    def get_status(self) -> SimpleNamespace:
        return SimpleNamespace(status="PENDING")


@pytest.fixture(autouse=True)
def refuse_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("I5 campaign API tests attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_i5_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    shutdown_dbos_runtime()
    reset_migrated_test_database()
    system_database = tmp_path / "test_i5_campaign_api_dbos.db"
    output_directory = tmp_path / "i5-output"
    settings = Settings(
        _env_file=None,
        database_url=str(engine.url),
        dbos_system_database_url=f"sqlite:///{system_database}",
        output_dir=str(output_directory),
        openai_api_key=TEST_OPENAI_KEY,
    )
    launch_dbos_runtime(settings)
    monkeypatch.setattr(i5_workflow, "get_settings", lambda: settings)
    monkeypatch.setattr(
        i5_workflow,
        "capture_tool_versions",
        lambda: ToolVersions(
            ffmpeg_binary="/test-only/ffmpeg",
            ffmpeg_version=TEST_FFMPEG_VERSION,
            ffprobe_binary="/test-only/ffprobe",
            ffprobe_version=TEST_FFPROBE_VERSION,
        ),
    )

    dispatches: list[dict[str, object]] = []

    def fake_start_workflow(
        workflow: object,
        campaign_id: int,
        script_hash: str,
        profile_hash: str,
    ) -> _FakeWorkflowHandle:
        with SessionLocal() as db:
            campaign = db.get(Campaign, campaign_id)
            assert campaign is not None
            assert campaign.production_workflow_id is not None
            workflow_id = campaign.production_workflow_id
        dispatches.append(
            {
                "campaign_id": campaign_id,
                "profile_hash": profile_hash,
                "script_hash": script_hash,
                "workflow": workflow,
                "workflow_id": workflow_id,
            }
        )
        return _FakeWorkflowHandle(workflow_id)

    monkeypatch.setattr(DBOS, "start_workflow", staticmethod(fake_start_workflow))
    try:
        yield {
            "dispatches": dispatches,
            "output_directory": output_directory,
            "settings": settings,
            "system_database": system_database,
        }
    finally:
        shutdown_dbos_runtime()


def _create_campaign(*, accepted_i4_script: bool = True) -> int:
    with SessionLocal() as db:
        channel = Channel(
            name="I5 campaign API channel",
            niche="AI infrastructure developer tools",
            audience="technical AI operators and developer teams",
            brand_voice="careful technical analysis",
            visual_style="evidence-led diagrams",
        )
        db.add(channel)
        db.flush()
        campaign = Campaign(
            channel_id=channel.id,
            current_stage="topic",
            workflow_id=None,
            production_workflow_id=None,
            risk_tier="standard",
            policy_version=I4_POLICY_VERSION,
        )
        db.add(campaign)
        db.commit()
        campaign_id = campaign.id

    if not accepted_i4_script:
        return campaign_id

    seed = happy_seed()
    editorial_persistence.persist_editorial_seed(campaign_id, seed)
    editorial_persistence.bind_i4_campaign_workflow(campaign_id)
    topic = compile_topic_packet(
        campaign_id=campaign_id,
        seed_hash=seed.sha256(campaign_id),
        seed=seed,
        demand=happy_demand(),
        channel_niche="AI infrastructure developer tools",
        channel_audience="technical AI operators and developer teams",
        novelty_history=(),
    )
    research = compile_research_packet(
        campaign_id=campaign_id,
        seed=seed,
        topic_packet=topic,
        topic_packet_hash=canonical_sha256(topic),
    )
    script = compile_script_packet(
        campaign_id=campaign_id,
        topic_packet=topic,
        topic_packet_hash=canonical_sha256(topic),
        research_packet=research,
        research_packet_hash=canonical_sha256(research),
    )
    editorial_persistence.persist_topic_stage(topic)
    editorial_persistence.persist_research_stage(research)
    editorial_persistence.persist_script_stage(script)
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "storyboard"
    return campaign_id


def _production_binding(campaign_id: int) -> tuple[str | None, int]:
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        profile_count = db.scalar(
            select(func.count(Artifact.id)).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_PRODUCTION_PROFILE_KIND,
            )
        )
        return campaign.production_workflow_id, int(profile_count or 0)


def _move_bound_campaign_to_media(campaign_id: int) -> str:
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.production_workflow_id is not None
        campaign.current_stage = "media"
        workflow_id = campaign.production_workflow_id
        db.commit()
    return workflow_id


def test_production_start_at_storyboard_binds_canonical_identity_and_is_idempotent(
    isolated_i5_api: dict[str, Any],
) -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()

    first = client.post(f"/campaigns/{campaign_id}/production/start")
    second = client.post(f"/campaigns/{campaign_id}/production/start")

    assert first.status_code == second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload == second_payload
    assert first_payload["campaign"]["current_stage"] == "storyboard"
    assert first_payload["campaign"]["workflow_id"].startswith(
        f"campaign:{campaign_id}:i4:"
    )
    assert first_payload["production_workflow_id"].startswith(
        f"campaign:{campaign_id}:i5:"
    )
    assert (
        first_payload["campaign"]["production_workflow_id"]
        == first_payload["production_workflow_id"]
    )
    assert len(first_payload["production_profile_hash"]) == 64
    assert first_payload["durable_status"] == "PENDING"
    assert _production_binding(campaign_id) == (
        first_payload["production_workflow_id"],
        1,
    )
    dispatches = isolated_i5_api["dispatches"]
    assert len(dispatches) == 2
    assert dispatches[0]["workflow_id"] == dispatches[1]["workflow_id"]
    assert dispatches[0]["profile_hash"] == dispatches[1]["profile_hash"]
    assert dispatches[0]["script_hash"] == dispatches[1]["script_hash"]


def test_production_start_rejects_stage_before_storyboard_without_binding() -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.current_stage = "script"
        db.commit()

    response = client.post(f"/campaigns/{campaign_id}/production/start")

    assert response.status_code == 409
    assert "accepted I4 script at storyboard" in response.json()["detail"]
    assert _production_binding(campaign_id) == (None, 0)


def test_production_start_rejects_duplicate_i4_predecessor_effects() -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()
    with SessionLocal() as db:
        canonical_gate = db.scalar(
            select(GateDecision).where(
                GateDecision.campaign_id == campaign_id,
                GateDecision.stage == "script",
            )
        )
        assert canonical_gate is not None
        db.add(
            GateDecision(
                campaign_id=campaign_id,
                stage="script",
                outcome=canonical_gate.outcome,
                policy_version=canonical_gate.policy_version,
                input_hash="f" * 64,
                output_hash=canonical_gate.output_hash,
                reasons_json=canonical_gate.reasons_json,
            )
        )
        db.commit()

    response = client.post(f"/campaigns/{campaign_id}/production/start")

    assert response.status_code == 409
    assert "script gate effect set is not exact" in response.json()["detail"]
    assert _production_binding(campaign_id) == (None, 0)


def test_production_start_requires_tts_key_before_profile_binding(
    monkeypatch: pytest.MonkeyPatch,
    isolated_i5_api: dict[str, Any],
) -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()
    baseline = isolated_i5_api["settings"]
    monkeypatch.setattr(
        i5_workflow,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            database_url=baseline.database_url,
            dbos_system_database_url=baseline.dbos_system_database_url,
            output_dir=baseline.output_dir,
            openai_api_key="",
        ),
    )

    response = client.post(f"/campaigns/{campaign_id}/production/start")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "OPENAI_API_KEY is required to start canonical I5 production."
    )
    assert TEST_OPENAI_KEY not in response.text
    assert _production_binding(campaign_id) == (None, 0)
    assert isolated_i5_api["dispatches"] == []


@pytest.mark.parametrize("missing_tool", ["ffmpeg", "ffprobe"])
def test_production_start_requires_media_tools_before_profile_binding(
    missing_tool: str,
    monkeypatch: pytest.MonkeyPatch,
    isolated_i5_api: dict[str, Any],
) -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()

    def missing_media_tool() -> ToolVersions:
        raise RendererError(f"Required executable is unavailable: {missing_tool}")

    monkeypatch.setattr(i5_workflow, "capture_tool_versions", missing_media_tool)

    response = client.post(f"/campaigns/{campaign_id}/production/start")

    assert response.status_code == 409
    assert missing_tool in response.json()["detail"]
    assert _production_binding(campaign_id) == (None, 0)
    assert isolated_i5_api["dispatches"] == []


def test_bound_production_profile_rejects_runtime_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()
    first = client.post(f"/campaigns/{campaign_id}/production/start")
    assert first.status_code == 200, first.text
    original_workflow_id = first.json()["production_workflow_id"]

    monkeypatch.setattr(
        i5_workflow,
        "capture_tool_versions",
        lambda: ToolVersions(
            ffmpeg_binary="/test-only/ffmpeg",
            ffmpeg_version=f"{TEST_FFMPEG_VERSION}-drifted",
            ffprobe_binary="/test-only/ffprobe",
            ffprobe_version=TEST_FFPROBE_VERSION,
        ),
    )
    replay = client.post(f"/campaigns/{campaign_id}/production/start")

    assert replay.status_code == 409
    assert "conflicting immutable production profile" in replay.json()["detail"]
    assert _production_binding(campaign_id) == (original_workflow_id, 1)


def test_production_snapshot_is_useful_and_secret_free(
    isolated_i5_api: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()
    started = client.post(f"/campaigns/{campaign_id}/production/start")
    assert started.status_code == 200, started.text

    response = client.get(f"/campaigns/{campaign_id}/production")

    assert response.status_code == 200, response.text
    payload = response.json()
    policy = load_campaign_budget_policy()
    assert payload == {
        "assembly_manifest_hash": None,
        "authorized_hard_cap_microusd": policy.limits_microusd.default_hard_cap,
        "campaign_id": campaign_id,
        "current_effective_campaign_cost_microusd": 0,
        "current_stage": "storyboard",
        "final_render_hash": None,
        "generated_media_counts": {},
        "i4_script_hash": isolated_i5_api["dispatches"][0]["script_hash"],
        "latest_budget_block": None,
        "latest_i5_gate": None,
        "media_manifest_hash": None,
        "production_profile_hash": started.json()["production_profile_hash"],
        "production_workflow_id": started.json()["production_workflow_id"],
        "reserved_cost_microusd": 0,
        "scene_count": 0,
        "soft_warning_active": False,
        "storyboard_hash": None,
        "visual_mode_counts": {},
        "voiceover_hash": None,
    }
    serialized = response.text.lower()
    assert TEST_OPENAI_KEY.lower() not in serialized
    assert str(isolated_i5_api["output_directory"]).lower() not in serialized
    assert "api_key" not in serialized

    secret = TEST_OPENAI_KEY.encode("utf-8")

    assert secret not in TEST_DATABASE.read_bytes()

    system_database = Path(isolated_i5_api["system_database"])
    if system_database.is_file():
        assert secret not in system_database.read_bytes()

    output_directory = Path(isolated_i5_api["output_directory"])
    if output_directory.exists():
        for path in output_directory.rglob("*"):
            if path.is_file():
                assert secret not in path.read_bytes()

    captured = capsys.readouterr()
    assert TEST_OPENAI_KEY not in captured.out
    assert TEST_OPENAI_KEY not in captured.err


def test_production_endpoints_return_not_found_for_unknown_campaign() -> None:
    client = TestClient(app)
    missing_id = 999_999
    request = {
        "actor": "owner@example.test",
        "new_authorized_cap_microusd": 40_000_000,
        "reason": "Approve the exact campaign production budget.",
    }

    assert client.post(f"/campaigns/{missing_id}/production/start").status_code == 404
    assert client.get(f"/campaigns/{missing_id}/production").status_code == 404
    assert (
        client.post(
            f"/campaigns/{missing_id}/production/budget/override",
            json=request,
        ).status_code
        == 404
    )


def test_budget_override_rejects_invalid_or_nonincreasing_cap() -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()
    started = client.post(f"/campaigns/{campaign_id}/production/start")
    assert started.status_code == 200, started.text
    _move_bound_campaign_to_media(campaign_id)
    policy = load_campaign_budget_policy()
    base_request = {
        "actor": "owner@example.test",
        "reason": "Approve additional spend for this campaign only.",
    }

    invalid = client.post(
        f"/campaigns/{campaign_id}/production/budget/override",
        json={**base_request, "new_authorized_cap_microusd": 0},
    )
    equal = client.post(
        f"/campaigns/{campaign_id}/production/budget/override",
        json={
            **base_request,
            "new_authorized_cap_microusd": policy.limits_microusd.default_hard_cap,
        },
    )
    lower = client.post(
        f"/campaigns/{campaign_id}/production/budget/override",
        json={
            **base_request,
            "new_authorized_cap_microusd": policy.limits_microusd.default_hard_cap - 1,
        },
    )

    assert invalid.status_code == 422
    assert equal.status_code == 409
    assert lower.status_code == 409
    assert "must strictly increase" in equal.json()["detail"]
    assert "must strictly increase" in lower.json()["detail"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count(CampaignBudgetOverride.id))) == 0


def test_budget_override_commits_before_send_and_exact_retry_resends_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    campaign_id = _create_campaign()
    started = client.post(f"/campaigns/{campaign_id}/production/start")
    assert started.status_code == 200, started.text
    workflow_id = _move_bound_campaign_to_media(campaign_id)
    request = {
        "actor": "owner@example.test",
        "new_authorized_cap_microusd": 40_000_000,
        "reason": "Approve one bounded campaign overage after review.",
    }
    sends: list[dict[str, object]] = []

    def send_after_commit(
        *,
        workflow_id: str,
        message: dict[str, object],
        idempotency_key: str,
    ) -> None:
        with SessionLocal() as db:
            row = db.scalar(
                select(CampaignBudgetOverride).where(
                    CampaignBudgetOverride.campaign_id == campaign_id,
                    CampaignBudgetOverride.override_hash == idempotency_key,
                )
            )
            assert row is not None
            assert row.new_authorized_cap_microunits == request[
                "new_authorized_cap_microusd"
            ]
        sends.append(
            {
                "idempotency_key": idempotency_key,
                "message": dict(message),
                "workflow_id": workflow_id,
            }
        )
        if len(sends) == 1:
            raise RuntimeError("test-only DBOS delivery outage")

    monkeypatch.setattr(campaigns_router, "send_i5_budget_override", send_after_commit)

    first = client.post(
        f"/campaigns/{campaign_id}/production/budget/override",
        json=request,
    )
    replay = client.post(
        f"/campaigns/{campaign_id}/production/budget/override",
        json=request,
    )

    assert first.status_code == 503
    assert first.json()["detail"] == (
        "Budget override persisted; durable message delivery is unavailable"
    )
    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()
    assert replay_payload["created"] is False
    assert replay_payload["message_delivered"] is True
    assert replay_payload["production_workflow_id"] == workflow_id
    assert replay_payload["new_authorized_cap_microusd"] == 40_000_000
    assert len(replay_payload["override_hash"]) == 64
    assert len(sends) == 2
    assert sends[0] == sends[1]
    assert sends[0] == {
        "idempotency_key": replay_payload["override_hash"],
        "message": {
            "campaign_id": campaign_id,
            "override_hash": replay_payload["override_hash"],
            "production_workflow_id": workflow_id,
        },
        "workflow_id": workflow_id,
    }
    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(CampaignBudgetOverride).where(
                    CampaignBudgetOverride.campaign_id == campaign_id
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].id == replay_payload["override_id"]
        assert rows[0].override_hash == replay_payload["override_hash"]
