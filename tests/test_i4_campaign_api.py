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
from app.db import Artifact, Campaign, GateDecision, GenerationJob, SessionLocal, engine  # noqa: E402
from app.editorial.contracts import EditorialSeed, I4_POLICY_VERSION  # noqa: E402
from app.editorial.demand import YouTubeDemandProvider  # noqa: E402
from app.editorial.persistence import (  # noqa: E402
    I4_SEED_KIND,
    bind_i4_campaign_workflow,
    i4_campaign_workflow_id,
)
from app.main import app  # noqa: E402
from app.models import Channel  # noqa: E402
from app.workflows.dbos_runtime import launch_dbos_runtime, shutdown_dbos_runtime  # noqa: E402
from tests.db_helpers import reset_migrated_test_database  # noqa: E402
from tests.i4_test_data import (  # noqa: E402
    changed_seed_payload,
    happy_demand,
    happy_seed,
    happy_seed_payload,
)


@pytest.fixture(autouse=True)
def isolated_i4_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    shutdown_dbos_runtime()
    reset_migrated_test_database()
    system_database = tmp_path / "test_dbos_i4_api.db"
    launch_dbos_runtime(
        Settings(
            database_url=str(engine.url),
            dbos_system_database_url=f"sqlite:///{system_database}",
        )
    )
    monkeypatch.setattr(
        "app.workflows.i4_campaign_workflow.get_settings",
        lambda: Settings(
            database_url=str(engine.url),
            dbos_system_database_url=f"sqlite:///{system_database}",
            youtube_data_api_key="test-only-key",
        ),
    )
    monkeypatch.setattr(
        YouTubeDemandProvider,
        "acquire",
        lambda self, request, *, api_key: happy_demand(),
    )
    yield
    shutdown_dbos_runtime()


def _create_channel() -> int:
    with SessionLocal() as db:
        channel = Channel(
            name="I4 API channel",
            niche="AI infrastructure developer tools",
            audience="technical AI operators and developer teams",
            brand_voice="careful technical analysis",
            visual_style="evidence-led diagrams",
        )
        db.add(channel)
        db.commit()
        return channel.id


def _create_campaign(client: TestClient) -> int:
    response = client.post("/campaigns", json={"channel_id": _create_channel()})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def test_campaign_api_defaults_to_i4_without_starting_workflow() -> None:
    client = TestClient(app)
    channel_id = _create_channel()
    created = client.post("/campaigns", json={"channel_id": channel_id})

    assert created.status_code == 201, created.text
    campaign = created.json()
    assert campaign["policy_version"] == I4_POLICY_VERSION
    assert campaign["current_stage"] == "topic"
    assert campaign["workflow_id"] is None
    assert DBOS.get_workflow_status(
        i4_campaign_workflow_id(int(campaign["id"]), happy_seed().sha256(int(campaign["id"])))
    ) is None

    invalid_policy = client.post(
        "/campaigns",
        json={"channel_id": channel_id, "policy_version": "unknown-policy"},
    )
    assert invalid_policy.status_code == 422


def test_editorial_seed_api_versions_latest_and_replays_exact_payload() -> None:
    client = TestClient(app)
    campaign_id = _create_campaign(client)

    first = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=happy_seed_payload(),
    )
    exact = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=happy_seed_payload(),
    )
    changed_payload = changed_seed_payload()
    second = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=changed_payload,
    )
    old_replay = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=happy_seed_payload(),
    )

    assert first.status_code == exact.status_code == second.status_code == 200
    assert first.json()["created"] is True and first.json()["active"] is True
    assert exact.json()["artifact_id"] == first.json()["artifact_id"]
    assert exact.json()["seed_hash"] == first.json()["seed_hash"]
    assert exact.json()["created"] is False and exact.json()["active"] is True
    assert second.json()["created"] is True and second.json()["active"] is True
    assert second.json()["artifact_id"] != first.json()["artifact_id"]
    assert old_replay.status_code == 200
    assert old_replay.json()["artifact_id"] == first.json()["artifact_id"]
    assert old_replay.json()["created"] is False and old_replay.json()["active"] is False

    current = client.get(f"/campaigns/{campaign_id}/editorial/seed")
    assert current.status_code == 200
    assert current.json()["artifact_id"] == second.json()["artifact_id"]
    assert current.json()["seed"]["candidates"][0]["angle"] == (
        EditorialSeed.model_validate(changed_payload).candidates[0].angle
    )


def test_seed_api_rejects_different_payload_after_workflow_identity_binding() -> None:
    client = TestClient(app)
    campaign_id = _create_campaign(client)
    seed = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=happy_seed_payload(),
    )
    assert seed.status_code == 200
    binding = bind_i4_campaign_workflow(campaign_id)

    same = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=happy_seed_payload(),
    )
    different = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=changed_seed_payload(),
    )

    assert same.status_code == 200
    assert same.json()["created"] is False and same.json()["active"] is True
    assert different.status_code == 409
    assert "immutable after workflow binding" in different.json()["detail"]
    assert binding["workflow_id"] == i4_campaign_workflow_id(
        campaign_id,
        happy_seed().sha256(campaign_id),
    )


def test_i4_start_requires_seed_without_binding_campaign() -> None:
    client = TestClient(app)
    campaign_id = _create_campaign(client)

    response = client.post(f"/campaigns/{campaign_id}/workflow/start")

    assert response.status_code == 409
    assert "requires an editorial seed" in response.json()["detail"]
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.workflow_id is None


def test_i4_start_missing_key_creates_no_binding_or_dbos_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    campaign_id = _create_campaign(client)
    seed_response = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=happy_seed_payload(),
    )
    seed_hash = seed_response.json()["seed_hash"]
    workflow_id = i4_campaign_workflow_id(campaign_id, seed_hash)
    monkeypatch.setattr(
        "app.workflows.i4_campaign_workflow.get_settings",
        lambda: Settings(
            database_url=str(engine.url),
            dbos_system_database_url="sqlite:///unused-test-only.db",
            youtube_data_api_key="",
        ),
    )

    response = client.post(f"/campaigns/{campaign_id}/workflow/start")

    assert response.status_code == 409
    assert "YOUTUBE_DATA_API_KEY is required" in response.json()["detail"]
    assert DBOS.get_workflow_status(workflow_id) is None
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.workflow_id is None
        assert campaign.current_stage == "topic"
        assert (
            db.query(Artifact)
            .filter(Artifact.campaign_id == campaign_id, Artifact.kind == I4_SEED_KIND)
            .count()
            == 1
        )
        assert db.query(GateDecision).filter(GateDecision.campaign_id == campaign_id).count() == 0


def test_i4_api_start_is_idempotent_and_editorial_snapshot_is_canonical() -> None:
    client = TestClient(app)
    campaign_id = _create_campaign(client)
    seed = client.post(
        f"/campaigns/{campaign_id}/editorial/seed",
        json=happy_seed_payload(),
    )
    seed_hash = seed.json()["seed_hash"]
    expected_workflow_id = i4_campaign_workflow_id(campaign_id, seed_hash)

    first = client.post(f"/campaigns/{campaign_id}/workflow/start")
    second = client.post(f"/campaigns/{campaign_id}/workflow/start")

    assert first.status_code == second.status_code == 200
    assert first.json()["workflow_id"] == second.json()["workflow_id"] == expected_workflow_id
    result = DBOS.retrieve_workflow(expected_workflow_id).get_result(polling_interval_sec=0.01)
    assert result["final_stage"] == "storyboard"

    snapshot = client.get(f"/campaigns/{campaign_id}/editorial")
    assert snapshot.status_code == 200, snapshot.text
    payload = snapshot.json()
    assert payload["active_seed_hash"] == seed_hash
    assert payload["current_stage"] == "storyboard"
    assert payload["workflow_id"] == expected_workflow_id
    assert payload["selected_topic"] == "Open inference stacks for technical AI teams"
    assert payload["viewer_promise"] == happy_seed().candidates[0].viewer_promise_contract().model_dump(
        mode="json"
    )
    assert payload["topic_score"] == 90
    assert sum(payload["topic_score_breakdown"].values()) > 0
    assert payload["source_counts"]["discovery_only"] == 10
    assert payload["claim_state_counts"] == {
        "ESTIMATE": 1,
        "OPINION": 1,
        "VERIFIED": 4,
    }
    assert len(payload["script_hash"]) == 64
    assert 8 <= payload["script_runtime_estimate_minutes"] <= 12
    assert payload["last_gate_outcome"] == "PASS"
    assert payload["seed_present"] is True

    with SessionLocal() as db:
        assert (
            db.query(GenerationJob)
            .filter(GenerationJob.campaign_id == campaign_id)
            .count()
            == 3
        )
        assert all(
            job.cost_microunits == 0
            for job in db.query(GenerationJob).filter(GenerationJob.campaign_id == campaign_id)
        )


def test_editorial_read_is_useful_before_seed_and_unknown_policy_fails_closed() -> None:
    client = TestClient(app)
    campaign_id = _create_campaign(client)

    missing_seed = client.get(f"/campaigns/{campaign_id}/editorial/seed")
    snapshot = client.get(f"/campaigns/{campaign_id}/editorial")
    assert missing_seed.status_code == 404
    assert snapshot.status_code == 200
    assert snapshot.json()["seed_present"] is False
    assert snapshot.json()["current_stage"] == "topic"

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.policy_version = "unknown-policy"
        db.commit()
    rejected = client.post(f"/campaigns/{campaign_id}/workflow/start")
    assert rejected.status_code == 409
    assert "Unsupported campaign policy" in rejected.json()["detail"]
