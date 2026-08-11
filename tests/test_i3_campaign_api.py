from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = ROOT / "test_content_factory.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE}"

from dbos import DBOS  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Approval, Campaign, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Channel, PublishRecord  # noqa: E402
from app.workflows.campaign_workflow import campaign_workflow_id  # noqa: E402
from app.workflows.dbos_runtime import (  # noqa: E402
    launch_dbos_runtime,
    shutdown_dbos_runtime,
)
from tests.db_helpers import reset_migrated_test_database  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_i3_api(tmp_path: Path) -> Iterator[None]:
    shutdown_dbos_runtime()
    reset_migrated_test_database()
    launch_dbos_runtime(
        Settings(
            database_url=str(engine.url),
            dbos_system_database_url=f"sqlite:///{tmp_path / 'test_dbos_api.db'}",
        )
    )
    yield
    shutdown_dbos_runtime()


def _create_channel() -> int:
    with SessionLocal() as db:
        channel = Channel(
            name="I3 API channel",
            niche="test",
            audience="test",
            brand_voice="test",
            visual_style="test",
        )
        db.add(channel)
        db.commit()
        return channel.id


def test_campaign_api_create_read_and_idempotent_start() -> None:
    channel_id = _create_channel()
    client = TestClient(app)

    created = client.post(
        "/campaigns",
        json={
            "channel_id": channel_id,
            "risk_tier": "standard",
            "policy_version": "i3-gate-v1",
        },
    )
    assert created.status_code == 201, created.text
    campaign = created.json()
    campaign_id = int(campaign["id"])
    assert campaign["current_stage"] == "topic"
    assert campaign["workflow_id"] is None
    assert campaign["policy_version"] == "i3-gate-v1"

    read = client.get(f"/campaigns/{campaign_id}")
    assert read.status_code == 200
    assert read.json() == campaign

    first_start = client.post(f"/campaigns/{campaign_id}/workflow/start")
    second_start = client.post(f"/campaigns/{campaign_id}/workflow/start")
    assert first_start.status_code == 200, first_start.text
    assert second_start.status_code == 200, second_start.text
    expected_workflow_id = campaign_workflow_id(campaign_id)
    assert first_start.json()["workflow_id"] == expected_workflow_id
    assert second_start.json()["workflow_id"] == expected_workflow_id

    result = DBOS.retrieve_workflow(expected_workflow_id).get_result(
        polling_interval_sec=0.01
    )
    assert result["final_stage"] == "human_approval"
    final = client.get(f"/campaigns/{campaign_id}")
    assert final.status_code == 200
    assert final.json()["current_stage"] == "human_approval"
    assert final.json()["workflow_id"] == expected_workflow_id

    with SessionLocal() as db:
        assert db.query(Approval).filter(Approval.campaign_id == campaign_id).count() == 0
        assert (
            db.query(PublishRecord)
            .filter(PublishRecord.campaign_id == campaign_id)
            .count()
            == 0
        )


def test_campaign_create_does_not_start_workflow() -> None:
    channel_id = _create_channel()
    response = TestClient(app).post(
        "/campaigns",
        json={"channel_id": channel_id, "policy_version": "i3-gate-v1"},
    )

    assert response.status_code == 201
    campaign_id = int(response.json()["id"])
    assert DBOS.get_workflow_status(campaign_workflow_id(campaign_id)) is None
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.current_stage == "topic"
        assert campaign.workflow_id is None


def test_campaign_api_rejects_missing_channel_and_campaign() -> None:
    client = TestClient(app)

    missing_channel = client.post("/campaigns", json={"channel_id": 999999})
    missing_campaign = client.get("/campaigns/999999")
    missing_start = client.post("/campaigns/999999/workflow/start")

    assert missing_channel.status_code == 404
    assert missing_campaign.status_code == 404
    assert missing_start.status_code == 404


def test_campaign_start_fails_closed_on_conflicting_workflow_identity() -> None:
    channel_id = _create_channel()
    client = TestClient(app)
    campaign_id = int(
        client.post(
            "/campaigns",
            json={"channel_id": channel_id, "policy_version": "i3-gate-v1"},
        ).json()["id"]
    )
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.workflow_id = "conflicting-workflow-id"
        db.commit()

    response = client.post(f"/campaigns/{campaign_id}/workflow/start")

    assert response.status_code == 409
    assert "conflicts with deterministic I3 identity" in response.json()["detail"]
