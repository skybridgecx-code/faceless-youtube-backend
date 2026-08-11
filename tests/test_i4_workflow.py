from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import urllib.parse
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
TEST_DATABASE = ROOT / "test_content_factory.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE}"

from dbos import DBOS  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import (  # noqa: E402
    Approval,
    Artifact,
    Campaign,
    Claim,
    ClaimSource,
    GateDecision,
    GenerationJob,
    Scene,
    SessionLocal,
    Source,
    engine,
)
from app.editorial.claims import compile_research_packet  # noqa: E402
from app.editorial.contracts import EditorialSeed, I4_POLICY_VERSION, canonical_sha256  # noqa: E402
from app.editorial.demand import (  # noqa: E402
    I4_HTTP_TIMEOUT_SECONDS,
    DemandCandidateRequest,
    DemandProviderError,
    DemandRequest,
    YouTubeDemandProvider,
)
from app.editorial.persistence import (  # noqa: E402
    I4_RESEARCH_KIND,
    I4_SCRIPT_KIND,
    I4_SEED_KIND,
    I4_TOPIC_KIND,
    bind_i4_campaign_workflow,
    i4_campaign_workflow_id,
    load_active_editorial_seed,
    persist_editorial_seed,
    persist_research_stage,
    persist_script_stage,
    persist_topic_stage,
)
from app.editorial.script_compiler import compile_script_packet  # noqa: E402
from app.editorial.topic_intelligence import compile_topic_packet  # noqa: E402
from app.models import Channel, PublishRecord  # noqa: E402
from app.services.research import (  # noqa: E402
    ResearchFetchError,
    SourceChannel,
    SourceVideo,
    fetch_youtube_sources,
)
from app.workflows.dbos_runtime import launch_dbos_runtime, shutdown_dbos_runtime  # noqa: E402
from app.workflows.i4_campaign_workflow import (  # noqa: E402
    I4_PROVIDER_STEP_MAX_ATTEMPTS,
    I4_WORKFLOW_MAX_RECOVERY_ATTEMPTS,
    _seed_from_payload,
    start_i4_campaign_workflow,
)
from app.workflows.persistence import WorkflowReplayConflict, WorkflowTransitionError  # noqa: E402
from tests.db_helpers import reset_migrated_test_database  # noqa: E402
from tests.i4_test_data import (  # noqa: E402
    changed_seed_payload,
    happy_demand,
    happy_seed,
    happy_seed_payload,
)


@pytest.fixture(autouse=True)
def isolated_application_database(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    shutdown_dbos_runtime()
    reset_migrated_test_database()
    monkeypatch.setattr(
        "app.workflows.i4_campaign_workflow.get_settings",
        lambda: Settings(youtube_data_api_key="test-only-key"),
    )
    yield
    shutdown_dbos_runtime()


def _launch_test_runtime(system_database: Path) -> None:
    launch_dbos_runtime(
        Settings(
            database_url=str(engine.url),
            dbos_system_database_url=f"sqlite:///{system_database}",
        )
    )


def _create_campaign(*, persist_seed: bool = True) -> int:
    with SessionLocal() as db:
        channel = Channel(
            name="I4 workflow channel",
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
            risk_tier="standard",
            policy_version=I4_POLICY_VERSION,
        )
        db.add(campaign)
        db.commit()
        campaign_id = campaign.id
    if persist_seed:
        persist_editorial_seed(campaign_id, happy_seed())
    return campaign_id


def _counts(campaign_id: int) -> dict[str, int]:
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
            "claims": db.scalar(
                select(func.count()).select_from(Claim).where(Claim.campaign_id == campaign_id)
            )
            or 0,
            "claim_sources": db.scalar(
                select(func.count())
                .select_from(ClaimSource)
                .join(Claim, Claim.id == ClaimSource.claim_id)
                .where(Claim.campaign_id == campaign_id)
            )
            or 0,
            "gate_decisions": db.scalar(
                select(func.count())
                .select_from(GateDecision)
                .where(GateDecision.campaign_id == campaign_id)
            )
            or 0,
            "generation_jobs": db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.campaign_id == campaign_id)
            )
            or 0,
            "publish_records": db.scalar(
                select(func.count())
                .select_from(PublishRecord)
                .where(PublishRecord.campaign_id == campaign_id)
            )
            or 0,
            "scenes": db.scalar(
                select(func.count()).select_from(Scene).where(Scene.campaign_id == campaign_id)
            )
            or 0,
            "sources": db.scalar(
                select(func.count()).select_from(Source).where(Source.campaign_id == campaign_id)
            )
            or 0,
        }


def _compiled_packets(
    campaign_id: int,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    seed = happy_seed()
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
    return topic, research, script


def test_i4_happy_path_is_zero_cost_and_stops_exactly_at_storyboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    def fixed_demand(
        self: YouTubeDemandProvider,
        request: object,
        *,
        api_key: str,
    ):  # type: ignore[no-untyped-def]
        nonlocal provider_calls
        provider_calls += 1
        assert api_key == "test-only-key"
        assert request.__class__.__name__ == "DemandRequest"
        return happy_demand()

    monkeypatch.setattr(YouTubeDemandProvider, "acquire", fixed_demand)
    _launch_test_runtime(tmp_path / "test_dbos_i4_happy.db")
    campaign_id = _create_campaign()

    handle = start_i4_campaign_workflow(campaign_id)
    result = handle.get_result(polling_interval_sec=0.01)

    assert result["final_stage"] == "storyboard"
    assert result["workflow_id"] == i4_campaign_workflow_id(
        campaign_id,
        happy_seed().sha256(campaign_id),
    )
    assert [stage["stage"] for stage in result["stage_results"]] == [
        "topic",
        "research",
        "script",
    ]
    assert [stage["outcome"] for stage in result["stage_results"]] == ["PASS"] * 3
    assert provider_calls == 1

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "storyboard"
        artifacts = list(
            db.scalars(
                select(Artifact)
                .where(Artifact.campaign_id == campaign_id)
                .order_by(Artifact.id)
            )
        )
        jobs = list(
            db.scalars(
                select(GenerationJob)
                .where(GenerationJob.campaign_id == campaign_id)
                .order_by(GenerationJob.id)
            )
        )
        gates = list(
            db.scalars(
                select(GateDecision)
                .where(GateDecision.campaign_id == campaign_id)
                .order_by(GateDecision.id)
            )
        )
        source_classes = list(
            db.scalars(select(Source.source_class).where(Source.campaign_id == campaign_id))
        )

    assert [artifact.kind for artifact in artifacts] == [
        I4_SEED_KIND,
        I4_TOPIC_KIND,
        I4_RESEARCH_KIND,
        I4_SCRIPT_KIND,
    ]
    assert all(artifact.payload_json for artifact in artifacts)
    assert [gate.stage for gate in gates] == ["topic", "research", "script"]
    assert [gate.outcome for gate in gates] == ["PASS"] * 3
    assert all(job.status == "completed" and job.cost_microunits == 0 for job in jobs)
    assert source_classes.count("discovery_only") == 10
    assert _counts(campaign_id) == {
        "approvals": 0,
        "artifacts": 4,
        "claims": 6,
        "claim_sources": 6,
        "gate_decisions": 3,
        "generation_jobs": 3,
        "publish_records": 0,
        "scenes": 0,
        "sources": 14,
    }


def test_duplicate_i4_start_reuses_the_seed_bound_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        YouTubeDemandProvider,
        "acquire",
        lambda self, request, *, api_key: happy_demand(),
    )
    _launch_test_runtime(tmp_path / "test_dbos_i4_duplicate.db")
    campaign_id = _create_campaign()

    first = start_i4_campaign_workflow(campaign_id)
    second = start_i4_campaign_workflow(campaign_id)

    expected_id = i4_campaign_workflow_id(campaign_id, happy_seed().sha256(campaign_id))
    assert first.get_workflow_id() == second.get_workflow_id() == expected_id
    assert first.get_result(polling_interval_sec=0.01) == second.get_result(
        polling_interval_sec=0.01
    )
    assert _counts(campaign_id)["artifacts"] == 4
    assert _counts(campaign_id)["generation_jobs"] == 3
    assert _counts(campaign_id)["gate_decisions"] == 3


def test_seed_versions_are_immutable_idempotent_and_bound_to_latest_hash() -> None:
    campaign_id = _create_campaign(persist_seed=False)
    first_seed = happy_seed()
    second_seed = EditorialSeed.model_validate(changed_seed_payload())

    first = persist_editorial_seed(campaign_id, first_seed)
    exact_replay = persist_editorial_seed(campaign_id, first_seed)
    second = persist_editorial_seed(campaign_id, second_seed)
    active_artifact, active_seed = load_active_editorial_seed(campaign_id)

    assert first["created"] is True
    assert exact_replay == {**first, "created": False}
    assert second["created"] is True
    assert active_artifact.sha256 == second_seed.sha256(campaign_id)
    assert active_seed == second_seed

    binding = bind_i4_campaign_workflow(campaign_id)
    assert binding["workflow_id"] == i4_campaign_workflow_id(
        campaign_id,
        second_seed.sha256(campaign_id),
    )
    assert persist_editorial_seed(campaign_id, second_seed)["created"] is False
    with pytest.raises(WorkflowTransitionError, match="immutable after workflow binding"):
        persist_editorial_seed(campaign_id, first_seed)


def test_missing_youtube_key_fails_before_binding_or_dbos_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _launch_test_runtime(tmp_path / "test_dbos_i4_missing_key.db")
    campaign_id = _create_campaign()
    expected_id = i4_campaign_workflow_id(campaign_id, happy_seed().sha256(campaign_id))

    from app.workflows.i4_campaign_workflow import I4ConfigurationError

    monkeypatch.setattr(
        "app.workflows.i4_campaign_workflow.get_settings",
        lambda: Settings(youtube_data_api_key=""),
    )

    with pytest.raises(I4ConfigurationError, match="YOUTUBE_DATA_API_KEY is required"):
        start_i4_campaign_workflow(campaign_id)

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.workflow_id is None
        assert campaign.current_stage == "topic"
    assert DBOS.get_workflow_status(expected_id) is None
    assert _counts(campaign_id)["artifacts"] == 1
    assert _counts(campaign_id)["gate_decisions"] == 0


def test_direct_persistence_replay_reconciles_every_i4_stage_and_link() -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    topic, research, script = _compiled_packets(campaign_id)

    for persist, packet in (
        (persist_topic_stage, topic),
        (persist_research_stage, research),
        (persist_script_stage, script),
    ):
        first = persist(packet)
        counts_after_first = _counts(campaign_id)
        replay = persist(packet)
        assert replay["replayed"] is True
        assert replay["artifact_id"] == first["artifact_id"]
        assert replay["generation_job_id"] == first["generation_job_id"]
        assert replay["gate_decision_id"] == first["gate_decision_id"]
        assert _counts(campaign_id) == counts_after_first

    assert _counts(campaign_id)["claim_sources"] == 6
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "storyboard"


def test_conflicting_output_replay_fails_closed_without_extra_rows() -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    topic, _, _ = _compiled_packets(campaign_id)
    persist_topic_stage(topic)
    before = _counts(campaign_id)
    conflict = deepcopy(topic)
    conflict["commercial_score"] = int(conflict["commercial_score"]) - 1

    with pytest.raises(
        WorkflowReplayConflict,
        match="Topic packet does not match deterministic compilation",
    ):
        persist_topic_stage(conflict)

    assert _counts(campaign_id) == before


def test_stage_persistence_rejects_an_unbound_i4_campaign() -> None:
    campaign_id = _create_campaign()
    topic, _, _ = _compiled_packets(campaign_id)

    with pytest.raises(WorkflowReplayConflict, match="not bound to a canonical I4 workflow"):
        persist_topic_stage(topic)

    assert _counts(campaign_id)["artifacts"] == 1
    assert _counts(campaign_id)["generation_jobs"] == 0
    assert _counts(campaign_id)["gate_decisions"] == 0


def test_topic_first_write_rejects_forged_selection_and_viewer_promise() -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    topic, _, _ = _compiled_packets(campaign_id)
    forged = deepcopy(topic)
    forged["selected_candidate_key"] = "forged-candidate"
    forged["viewer_promise"] = {
        **forged["viewer_promise"],  # type: ignore[arg-type]
        "viewer_promise": "A forged promise that was never in the bound seed.",
    }

    with pytest.raises(
        WorkflowReplayConflict,
        match="Topic packet does not match deterministic compilation",
    ):
        persist_topic_stage(forged)

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "topic"
    assert _counts(campaign_id)["artifacts"] == 1
    assert _counts(campaign_id)["generation_jobs"] == 0
    assert _counts(campaign_id)["gate_decisions"] == 0


def test_topic_first_write_rejects_forged_channel_compilation_context() -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    seed = happy_seed()
    forged = compile_topic_packet(
        campaign_id=campaign_id,
        seed_hash=seed.sha256(campaign_id),
        seed=seed,
        demand=happy_demand(),
        channel_niche="FORGED channel niche",
        channel_audience="FORGED channel audience",
        novelty_history=(),
    )

    with pytest.raises(
        WorkflowReplayConflict,
        match="compilation context conflicts with campaign lineage",
    ):
        persist_topic_stage(forged)

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "topic"
    assert _counts(campaign_id)["artifacts"] == 1
    assert _counts(campaign_id)["generation_jobs"] == 0
    assert _counts(campaign_id)["gate_decisions"] == 0


def test_child_stage_rejects_predecessor_effect_set_metadata_drift() -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    topic, research, _ = _compiled_packets(campaign_id)
    persist_topic_stage(topic)
    with SessionLocal() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I4_TOPIC_KIND,
            )
        )
        assert artifact is not None
        artifact.uri = f"{artifact.uri}/mutated"
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="artifact conflicts"):
        persist_research_stage(research)

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "research"
    assert _counts(campaign_id)["artifacts"] == 2
    assert _counts(campaign_id)["generation_jobs"] == 1
    assert _counts(campaign_id)["gate_decisions"] == 1


@pytest.mark.parametrize("identity_kind", ("source", "claim"))
def test_research_first_write_rejects_fabricated_semantic_hashes(
    identity_kind: str,
) -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    topic, research, _ = _compiled_packets(campaign_id)
    persist_topic_stage(topic)
    forged = deepcopy(research)
    records_key = "sources" if identity_kind == "source" else "claims"
    hash_key = "content_sha256" if identity_kind == "source" else "claim_hash"
    records = forged[records_key]
    assert isinstance(records, list)
    original_hash = str(records[0][hash_key])
    records[0][hash_key] = "0" * 64 if original_hash != "0" * 64 else "1" * 64

    with pytest.raises(
        WorkflowReplayConflict,
        match="Research packet does not match deterministic compilation",
    ):
        persist_research_stage(forged)

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "research"
    assert _counts(campaign_id)["sources"] == 0
    assert _counts(campaign_id)["claims"] == 0


def test_research_first_write_rejects_forged_selected_candidate_lineage() -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    topic, research, _ = _compiled_packets(campaign_id)
    persist_topic_stage(topic)
    forged = deepcopy(research)
    forged["selected_candidate_key"] = "forged-candidate"

    with pytest.raises(
        WorkflowReplayConflict,
        match="Research packet lineage conflicts with topic",
    ):
        persist_research_stage(forged)

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "research"
    assert _counts(campaign_id)["sources"] == 0
    assert _counts(campaign_id)["claims"] == 0


def test_script_persistence_rejects_committed_research_entity_drift() -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    topic, research, script = _compiled_packets(campaign_id)
    persist_topic_stage(topic)
    persist_research_stage(research)
    with SessionLocal() as db:
        claim = db.scalar(select(Claim).where(Claim.campaign_id == campaign_id))
        assert claim is not None
        claim.assertion_text = "mutated semantic claim after the accepted research gate"
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="claim conflicts"):
        persist_script_stage(script)

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "script"
    assert _counts(campaign_id)["artifacts"] == 3
    assert _counts(campaign_id)["generation_jobs"] == 2
    assert _counts(campaign_id)["gate_decisions"] == 2


def test_workflow_seed_payload_must_match_exact_canonical_bound_bytes() -> None:
    campaign_id = _create_campaign()
    seed = happy_seed()
    seed_hash = seed.sha256(campaign_id)
    canonical_payload = seed.artifact_payload(campaign_id)

    assert _seed_from_payload(campaign_id, seed_hash, canonical_payload) == seed

    forged_payload = deepcopy(canonical_payload)
    forged_payload["campaign_id"] = campaign_id + 1
    forged_payload["contract_version"] = "wrong-contract"
    forged_payload["requires_youtube_demand"] = False
    with pytest.raises(ValueError, match="does not match the bound seed hash"):
        _seed_from_payload(campaign_id, seed_hash, forged_payload)


def test_i4_fail_gate_persists_zero_cost_evidence_without_advancing() -> None:
    campaign_id = _create_campaign(persist_seed=False)
    seed_payload = happy_seed_payload()
    candidates = seed_payload["candidates"]
    assert isinstance(candidates, list)
    sources = candidates[0]["evidence_sources"]
    assert isinstance(sources, list)
    for source in sources:
        source["source_class"] = "discovery_only"
    seed = EditorialSeed.model_validate(seed_payload)
    persist_editorial_seed(campaign_id, seed)
    bind_i4_campaign_workflow(campaign_id)
    topic = compile_topic_packet(
        campaign_id=campaign_id,
        seed_hash=seed.sha256(campaign_id),
        seed=seed,
        demand=happy_demand(),
        channel_niche="AI infrastructure developer tools",
        channel_audience="technical AI operators and developer teams",
        novelty_history=(),
    )

    persisted = persist_topic_stage(topic)

    assert topic["gate"]["outcome"] == "FAIL"  # type: ignore[index]
    assert persisted["outcome"] == "FAIL"
    assert persisted["current_stage"] == "topic"
    with SessionLocal() as db:
        job = db.get(GenerationJob, persisted["generation_job_id"])
        assert job is not None and job.cost_microunits == 0
    assert _counts(campaign_id)["artifacts"] == 2
    assert _counts(campaign_id)["generation_jobs"] == 1
    assert _counts(campaign_id)["gate_decisions"] == 1


def test_research_replay_detects_source_and_claim_storage_drift() -> None:
    campaign_id = _create_campaign()
    bind_i4_campaign_workflow(campaign_id)
    topic, research, _ = _compiled_packets(campaign_id)
    persist_topic_stage(topic)
    persist_research_stage(research)

    with SessionLocal() as db:
        source = db.scalar(
            select(Source).where(
                Source.campaign_id == campaign_id,
                Source.source_class == "official",
            )
        )
        assert source is not None
        source.publisher = "mutated publisher"
        db.commit()
    with pytest.raises(WorkflowReplayConflict, match="source conflicts"):
        persist_research_stage(research)

    with SessionLocal() as db:
        source = db.scalar(
            select(Source).where(
                Source.campaign_id == campaign_id,
                Source.source_class == "official",
            )
        )
        assert source is not None
        source.publisher = "vLLM Project"
        claim = db.scalar(select(Claim).where(Claim.campaign_id == campaign_id))
        assert claim is not None
        claim.assertion_text = "mutated semantic claim"
        db.commit()
    with pytest.raises(WorkflowReplayConflict, match="claim conflicts"):
        persist_research_stage(research)


def test_demand_failure_retries_are_bounded_and_leave_no_partial_topic_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    secret_marker = "provider-secret-must-not-escape"

    def controlled_failure(url: str, *, timeout: float) -> NoReturn:
        nonlocal attempts
        attempts += 1
        assert url.startswith("https://www.googleapis.com/youtube/v3/search?")
        assert timeout == I4_HTTP_TIMEOUT_SECONDS
        raise ResearchFetchError(secret_marker)

    monkeypatch.setattr("app.services.research._load_json_response", controlled_failure)
    system_database = tmp_path / "test_dbos_i4_provider_failure.db"
    _launch_test_runtime(system_database)
    campaign_id = _create_campaign()
    handle = start_i4_campaign_workflow(campaign_id)

    with pytest.raises(Exception) as exc_info:
        handle.get_result(polling_interval_sec=0.01)

    assert attempts == I4_PROVIDER_STEP_MAX_ATTEMPTS == 2
    assert I4_WORKFLOW_MAX_RECOVERY_ATTEMPTS == 3
    assert secret_marker not in str(exc_info.value)
    assert secret_marker.encode() not in system_database.read_bytes()
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "topic"
    assert _counts(campaign_id) == {
        "approvals": 0,
        "artifacts": 1,
        "claims": 0,
        "claim_sources": 0,
        "gate_decisions": 0,
        "generation_jobs": 0,
        "publish_records": 0,
        "scenes": 0,
        "sources": 0,
    }


def test_provider_failure_boundary_suppresses_underlying_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_id = _create_campaign()
    seed = happy_seed()
    request = DemandRequest.from_seed(
        campaign_id=campaign_id,
        seed_hash=seed.sha256(campaign_id),
        seed=seed,
    )
    secret_marker = "provider-secret-must-not-escape"

    def controlled_failure(url: str, *, timeout: float) -> NoReturn:
        raise ResearchFetchError(secret_marker)

    monkeypatch.setattr("app.services.research._load_json_response", controlled_failure)

    with pytest.raises(
        DemandProviderError,
        match="YouTube demand evidence could not be acquired",
    ) as exc_info:
        YouTubeDemandProvider().acquire(request, api_key="test-only-key")

    assert secret_marker not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def test_state_free_provider_enforces_candidate_result_request_and_timeout_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_id = _create_campaign()
    request = DemandRequest(
        campaign_id=campaign_id,
        seed_hash=happy_seed().sha256(campaign_id),
        candidates=tuple(
            DemandCandidateRequest(
                candidate_key=f"candidate-{index}",
                query=f"bounded query {index}",
            )
            for index in range(3)
        ),
    )
    calls: list[dict[str, object]] = []
    published_at = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    videos = [
        SourceVideo(
            youtube_video_id=f"video-{index}",
            youtube_channel_id="channel-1",
            title=f"Bounded public result {index}",
            channel_title="Public Channel",
            description="ignored by I4 demand",
            published_at=published_at,
            duration=None,
            view_count=1_000 + index,
            like_count=100,
            comment_count=10,
            thumbnail_url=None,
        )
        for index in range(12)
    ]
    channels = [
        SourceChannel(
            youtube_channel_id="channel-1",
            title="Public Channel",
            description="ignored by I4 demand",
            subscriber_count=4_000,
            video_count=None,
            view_count=None,
        )
    ]

    def bounded_fetch(**kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return videos, channels

    monkeypatch.setattr("app.editorial.demand.fetch_youtube_sources", bounded_fetch)
    before = _counts(campaign_id)

    snapshot = YouTubeDemandProvider().acquire(request, api_key="  test-only-key  ")

    assert len(calls) == 3
    assert all(call["api_key"] == "test-only-key" for call in calls)
    assert all(call["max_results"] == 10 for call in calls)
    assert all(call["timeout"] == I4_HTTP_TIMEOUT_SECONDS == 8 for call in calls)
    assert all(len(candidate.videos) == 10 for candidate in snapshot.candidates)
    assert _counts(campaign_id) == before
    assert not hasattr(YouTubeDemandProvider(), "session")
    with pytest.raises(ValidationError):
        request.campaign_id = campaign_id + 1  # type: ignore[misc]


def test_demand_request_bounds_maximum_seed_text_before_workflow_binding() -> None:
    campaign_id = _create_campaign(persist_seed=False)
    payload = happy_seed_payload()
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    candidates[0]["topic"] = "t" * 4_000
    candidates[0]["angle"] = "a" * 4_000
    seed = EditorialSeed.model_validate(payload)

    request = DemandRequest.from_seed(
        campaign_id=campaign_id,
        seed_hash=seed.sha256(campaign_id),
        seed=seed,
    )

    assert len(request.candidates[0].query) == 500
    assert request.candidates[0].query == "t" * 500


def test_low_level_youtube_fetch_bounds_and_deduplicates_over_returned_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def mocked_response(url: str, *, timeout: float) -> dict[str, object]:
        calls.append((url, timeout))
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/search"):
            assert query["maxResults"] == ["10"]
            items = [
                {
                    "id": {"videoId": f"video-{index}"},
                    "snippet": {
                        "channelId": f"channel-{index % 2}",
                        "channelTitle": f"Channel {index % 2}",
                        "title": f"Result {index}",
                    },
                }
                for index in range(15)
            ]
            items.insert(1, deepcopy(items[0]))
            return {"items": items}
        if parsed.path.endswith("/videos"):
            requested_ids = query["id"][0].split(",")
            assert len(requested_ids) == 9
            assert query["maxResults"] == ["9"]
            return {
                "items": [
                    {
                        "contentDetails": {"duration": "PT4M"},
                        "id": video_id,
                        "snippet": {
                            "channelId": f"channel-{index % 2}",
                            "channelTitle": f"Channel {index % 2}",
                            "publishedAt": "2026-08-01T12:00:00Z",
                            "title": f"Result {index}",
                        },
                        "statistics": {"viewCount": str(1_000 + index)},
                    }
                    for index, video_id in enumerate(
                        [*requested_ids, "unrequested-video"]
                    )
                ]
            }
        assert parsed.path.endswith("/channels")
        requested_channels = query["id"][0].split(",")
        assert set(requested_channels) == {"channel-0", "channel-1"}
        return {
            "items": [
                {
                    "id": channel_id,
                    "snippet": {"title": channel_id},
                    "statistics": {"subscriberCount": "1000"},
                }
                for channel_id in [*requested_channels, "unrequested-channel"]
            ]
        }

    monkeypatch.setattr("app.services.research._load_json_response", mocked_response)

    videos, channels = fetch_youtube_sources(
        api_key="test-only-key",
        query="bounded public demand",
        max_results=10,
        timeout=8,
    )

    assert len(calls) == 3
    assert all(timeout == 8 for _, timeout in calls)
    assert len(videos) == 9
    assert len({video.youtube_video_id for video in videos}) == 9
    assert {channel.youtube_channel_id for channel in channels} == {
        "channel-0",
        "channel-1",
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
            "YOUTUBE_DATA_API_KEY": "test-only-key",
            "YOUTUBE_OAUTH_CLIENT_ID": "",
            "YOUTUBE_OAUTH_CLIENT_SECRET": "",
            "YOUTUBE_OAUTH_REFRESH_TOKEN": "",
        }
    )
    return environment


def _sqlite_i4_counts(database: Path, campaign_id: int) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        counts = {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE campaign_id = ?',
                    (campaign_id,),
                ).fetchone()[0]
            )
            for table in (
                "artifacts",
                "claims",
                "gate_decisions",
                "generation_jobs",
                "sources",
            )
        }
        counts["claim_sources"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM claim_sources cs "
                "JOIN claims c ON c.id = cs.claim_id WHERE c.campaign_id = ?",
                (campaign_id,),
            ).fetchone()[0]
        )
        return counts
    finally:
        connection.close()


def test_real_dbos_restart_reuses_checkpointed_demand_and_reconciles_commit(
    tmp_path: Path,
) -> None:
    application_database = tmp_path / "test_i4_recovery_app.db"
    system_database = tmp_path / "test_dbos_i4_recovery.db"
    marker = tmp_path / "topic-persistence-committed"
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
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                "I4 recovery channel",
                "AI infrastructure developer tools",
                "technical AI operators and developer teams",
                "careful technical analysis",
                "evidence-led diagrams",
            ),
        ).lastrowid
        campaign_id = int(
            connection.execute(
                "INSERT INTO campaigns "
                "(channel_id, current_stage, workflow_id, risk_tier, policy_version, created_at, updated_at) "
                "VALUES (?, 'topic', NULL, 'standard', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (channel_id, I4_POLICY_VERSION),
            ).lastrowid
        )
        connection.commit()
    finally:
        connection.close()

    environment["I4_CAMPAIGN_ID"] = str(campaign_id)
    environment["I4_RECOVERY_MARKER"] = str(marker)
    interrupted_code = """
import os
import time
from pathlib import Path

from app.config import Settings
from app.editorial.persistence import persist_editorial_seed
from tests.i4_test_data import happy_demand, happy_seed
import app.workflows.i4_campaign_workflow as workflow
from app.workflows.dbos_runtime import launch_dbos_runtime

campaign_id = int(os.environ["I4_CAMPAIGN_ID"])
marker = Path(os.environ["I4_RECOVERY_MARKER"])
workflow.YouTubeDemandProvider.acquire = lambda self, request, *, api_key: happy_demand()
original_persist_topic = workflow.persist_topic_stage

def commit_then_block(packet):
    result = original_persist_topic(packet)
    marker.write_text("committed", encoding="utf-8")
    while True:
        time.sleep(0.05)

workflow.persist_topic_stage = commit_then_block
persist_editorial_seed(campaign_id, happy_seed())
launch_dbos_runtime(Settings())
workflow.start_i4_campaign_workflow(campaign_id)
while True:
    time.sleep(1)
"""
    process = subprocess.Popen(
        [str(PYTHON), "-c", interrupted_code],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        assert marker.exists(), "I4 workflow never reached the commit-before-checkpoint barrier"
        assert _sqlite_i4_counts(application_database, campaign_id) == {
            "artifacts": 2,
            "claims": 0,
            "claim_sources": 0,
            "gate_decisions": 1,
            "generation_jobs": 1,
            "sources": 0,
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
from tests.i4_test_data import happy_seed
import app.workflows.i4_campaign_workflow as workflow
from app.editorial.persistence import i4_campaign_workflow_id
from app.workflows.dbos_runtime import launch_dbos_runtime, shutdown_dbos_runtime

campaign_id = int(os.environ["I4_CAMPAIGN_ID"])

def provider_must_not_repeat(*args, **kwargs):
    raise AssertionError("checkpointed YouTube demand provider was invoked during recovery")

workflow.YouTubeDemandProvider.acquire = provider_must_not_repeat
workflow_id = i4_campaign_workflow_id(campaign_id, happy_seed().sha256(campaign_id))
launch_dbos_runtime(Settings())
try:
    result = DBOS.retrieve_workflow(workflow_id).get_result(polling_interval_sec=0.01)
    print("I4_RECOVERY_RESULT=" + json.dumps(result, sort_keys=True))
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
        timeout=45,
    )
    recovery_line = next(
        line
        for line in recovered.stdout.splitlines()
        if line.startswith("I4_RECOVERY_RESULT=")
    )
    recovery_result = json.loads(recovery_line.split("=", 1)[1])

    assert recovery_result["final_stage"] == "storyboard"
    assert _sqlite_i4_counts(application_database, campaign_id) == {
        "artifacts": 4,
        "claims": 6,
        "claim_sources": 6,
        "gate_decisions": 3,
        "generation_jobs": 3,
        "sources": 14,
    }
    connection = sqlite3.connect(application_database)
    try:
        assert connection.execute(
            "SELECT current_stage FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()[0] == "storyboard"
        assert connection.execute(
            "SELECT COUNT(*) FROM scenes WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
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
