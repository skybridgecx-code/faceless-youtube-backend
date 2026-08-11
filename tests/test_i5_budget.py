from __future__ import annotations

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Iterator

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import app.editorial.persistence as editorial_persistence
import app.production.persistence as production_persistence
import app.production.providers as provider_module
import app.production.media as production_media
from app.config import Settings
from app.db import Artifact, Campaign, CampaignBudgetOverride, GenerationJob, Scene
from app.editorial.claims import compile_research_packet
from app.editorial.contracts import I4_POLICY_VERSION, canonical_json, canonical_sha256
from app.editorial.script_compiler import compile_script_packet
from app.editorial.topic_intelligence import compile_topic_packet
from app.models import Channel
from app.production.budget import (
    CAMPAIGN_BUDGET_POLICY_PATH,
    BudgetDecision,
    BudgetOption,
    BudgetPolicyError,
    CampaignBudgetPolicy,
    choose_budget_option,
    effective_campaign_cost,
    load_campaign_budget_policy,
)
from app.production.persistence import (
    authorized_campaign_cap,
    claim_metered_dispatch,
    complete_metered_job,
    create_budget_override,
    reserve_tts_plan,
    reserve_visual_job,
)
from app.production.media import (
    build_tts_plan_requests,
    scene_contract,
    visual_input_hash,
    visual_request_options,
)
from app.production.profile import build_production_profile
from app.production.storyboard import compile_storyboard
from app.workflows.persistence import WorkflowReplayConflict
from scripts.migrate_db import upgrade_database
from tests.i4_test_data import happy_demand, happy_seed


@dataclass(frozen=True)
class BoundMediaCampaign:
    campaign_id: int
    profile_hash: str
    scene_ids: tuple[int, ...]
    storyboard_hash: str


@pytest.fixture(autouse=True)
def refuse_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("I5 budget tests attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def isolated_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[sessionmaker[Session]]:
    database = tmp_path / "test_i5_budget.db"
    database_url = f"sqlite:///{database}"
    upgrade_database(database_url)
    isolated_engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 10},
        future=True,
        poolclass=NullPool,
    )
    sessions = sessionmaker(
        bind=isolated_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
    monkeypatch.setattr(editorial_persistence, "SessionLocal", sessions)
    monkeypatch.setattr(production_persistence, "SessionLocal", sessions)
    monkeypatch.setattr(production_media, "SessionLocal", sessions)
    try:
        yield sessions
    finally:
        isolated_engine.dispose()


def _compile_i4_packets(
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


def _seed_bound_media_campaign(
    sessions: sessionmaker[Session],
) -> BoundMediaCampaign:
    with sessions() as db:
        channel = Channel(
            name="I5 budget channel",
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

    seed = happy_seed()
    editorial_persistence.persist_editorial_seed(campaign_id, seed)
    editorial_persistence.bind_i4_campaign_workflow(campaign_id)
    topic, research, script = _compile_i4_packets(campaign_id)
    editorial_persistence.persist_topic_stage(topic)
    editorial_persistence.persist_research_stage(research)
    editorial_persistence.persist_script_stage(script)

    script_hash = canonical_sha256(script)
    profile = build_production_profile(
        campaign_id=campaign_id,
        i4_script_sha256=script_hash,
        settings=Settings(_env_file=None, openai_api_key="test-only-key"),
        budget_policy=load_campaign_budget_policy(),
        ffmpeg_version="ffmpeg version test-only",
        ffprobe_version="ffprobe version test-only",
    )
    production_persistence.bind_i5_production_profile(campaign_id, profile)
    storyboard = compile_storyboard(
        campaign_id=campaign_id,
        script_packet=script,
        script_hash=script_hash,
        production_profile_hash=profile.sha256,
    )
    production_persistence.persist_storyboard(storyboard)

    with sessions() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "media"
        scene_ids = tuple(
            db.scalars(
                select(Scene.id)
                .where(Scene.campaign_id == campaign_id)
                .order_by(Scene.position)
            )
        )
    assert len(scene_ids) >= 2
    return BoundMediaCampaign(
        campaign_id=campaign_id,
        profile_hash=profile.sha256,
        scene_ids=scene_ids,
        storyboard_hash=canonical_sha256(storyboard),
    )


def _add_cost_job(
    sessions: sessionmaker[Session],
    campaign_id: int,
    *,
    identity: str,
    status: str,
    cost_microunits: int | None,
    reserved_cost_microunits: int | None,
) -> None:
    with sessions() as db:
        db.add(
            GenerationJob(
                campaign_id=campaign_id,
                scene_id=None,
                provider="budget_test",
                model=identity,
                attempt=1,
                status=status,
                input_hash=canonical_sha256({"identity": identity}),
                output_artifact_id=None,
                provider_job_id=None,
                usage_json="{}",
                cost_microunits=cost_microunits,
                reserved_cost_microunits=reserved_cost_microunits,
                error_json=None,
            )
        )
        db.commit()


def test_authoritative_policy_parse_hash_limits_and_required_controls() -> None:
    raw = json.loads(CAMPAIGN_BUDGET_POLICY_PATH.read_text(encoding="utf-8"))
    policy = load_campaign_budget_policy()

    assert policy.policy_sha256 == canonical_sha256(raw)
    assert policy.canonical_payload() == raw
    assert policy.policy_version == "campaign-budget-v1"
    assert policy.currency == "USD"
    assert policy.limits_microusd.model_dump() == {
        "target": 20_000_000,
        "soft_warning": 25_000_000,
        "default_hard_cap": 35_000_000,
    }
    assert policy.requirements.model_dump() == {
        "require_preflight_estimate": True,
        "fallback_after_soft_warning": True,
        "block_request_above_hard_cap": True,
        "owner_override_required": True,
        "override_must_be_campaign_specific": True,
        "override_must_be_audited": True,
    }
    assert policy.override_contract.providers_may_mutate_global_policy is False
    assert policy.override_contract.analytics_may_mutate_global_policy is False


def test_policy_validation_rejects_changed_locked_limits() -> None:
    raw = json.loads(CAMPAIGN_BUDGET_POLICY_PATH.read_text(encoding="utf-8"))
    raw["limits_microusd"]["default_hard_cap"] = 36_000_000

    with pytest.raises(ValidationError, match="locked policy"):
        CampaignBudgetPolicy.model_validate(
            {**raw, "policy_sha256": canonical_sha256(raw)}
        )


def test_effective_cost_prefers_known_cost_and_retains_ambiguous_reservations() -> None:
    jobs = (
        SimpleNamespace(
            status="completed",
            cost_microunits=120,
            reserved_cost_microunits=999,
        ),
        SimpleNamespace(
            status="reserved",
            cost_microunits=None,
            reserved_cost_microunits=200,
        ),
        SimpleNamespace(
            status="ambiguous_dispatch",
            cost_microunits=None,
            reserved_cost_microunits=300,
        ),
        SimpleNamespace(
            status="blocked_budget",
            cost_microunits=None,
            reserved_cost_microunits=400,
        ),
        SimpleNamespace(
            status="completed",
            cost_microunits=0,
            reserved_cost_microunits=0,
        ),
    )

    cost = effective_campaign_cost(jobs)

    assert cost.committed_microusd == 120
    assert cost.reserved_microusd == 500
    assert cost.total_microusd == 620


@pytest.mark.parametrize(
    ("cost", "reservation"),
    [(-1, None), (None, -1)],
)
def test_effective_cost_rejects_negative_financial_values(
    cost: int | None,
    reservation: int | None,
) -> None:
    with pytest.raises(BudgetPolicyError):
        effective_campaign_cost(
            (
                SimpleNamespace(
                    status="ambiguous_dispatch",
                    cost_microunits=cost,
                    reserved_cost_microunits=reservation,
                ),
            )
        )


def test_budget_decision_allows_primary_below_warning_and_cap() -> None:
    choice = choose_budget_option(
        current_cost_microusd=10_000_000,
        authorized_cap_microusd=35_000_000,
        primary=BudgetOption("primary", 1_000_000),
        fallbacks=(BudgetOption("fallback", 500_000),),
    )

    assert choice.decision == BudgetDecision.ALLOW_PRIMARY
    assert choice.selected == BudgetOption("primary", 1_000_000)
    assert choice.projected_cost_microusd == 11_000_000
    assert choice.soft_warning_active is False


def test_budget_decision_uses_cheaper_fallback_at_soft_warning() -> None:
    choice = choose_budget_option(
        current_cost_microusd=24_500_000,
        authorized_cap_microusd=35_000_000,
        primary=BudgetOption("primary", 1_000_000),
        fallbacks=(BudgetOption("fallback", 250_000),),
    )

    assert choice.decision == BudgetDecision.USE_LOWER_COST_FALLBACK
    assert choice.selected == BudgetOption("fallback", 250_000)
    assert choice.projected_cost_microusd == 24_750_000
    assert choice.soft_warning_active is True


def test_budget_decision_uses_cheaper_fallback_when_primary_breaches_cap() -> None:
    choice = choose_budget_option(
        current_cost_microusd=34_500_000,
        authorized_cap_microusd=35_000_000,
        primary=BudgetOption("primary", 1_000_000),
        fallbacks=(BudgetOption("fallback", 400_000),),
    )

    assert choice.decision == BudgetDecision.USE_LOWER_COST_FALLBACK
    assert choice.selected == BudgetOption("fallback", 400_000)
    assert choice.projected_cost_microusd == 34_900_000


def test_budget_decision_blocks_when_no_suitable_option_fits() -> None:
    choice = choose_budget_option(
        current_cost_microusd=34_900_000,
        authorized_cap_microusd=35_000_000,
        primary=BudgetOption("primary", 1_000_000),
        fallbacks=(BudgetOption("fallback", 200_000),),
    )

    assert choice.decision == BudgetDecision.BLOCK_NEEDS_HUMAN
    assert choice.selected is None
    assert choice.projected_cost_microusd == 35_900_000


def test_whole_campaign_tts_primary_model_and_reservations_are_atomic_and_replayable(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    first = build_tts_plan_requests(bound.campaign_id)
    replay = build_tts_plan_requests(bound.campaign_id)

    assert first["blocked"] is False
    assert first["selected_model"] == "tts-1-hd"
    assert replay == {**first, "replayed": True}
    with isolated_sessions() as db:
        jobs = list(
            db.scalars(
                select(GenerationJob)
                .where(
                    GenerationJob.campaign_id == bound.campaign_id,
                    GenerationJob.provider == "openai_tts",
                )
                .order_by(GenerationJob.scene_id)
            )
        )
    assert len(jobs) == len(bound.scene_ids)
    assert {job.model for job in jobs} == {"tts-1-hd"}
    assert {job.status for job in jobs} == {"reserved"}
    assert sorted(job.reserved_cost_microunits for job in jobs) == sorted(
        (int(request["character_count"]) * 30 * 120 + 99) // 100
        for request in first["requests"]
    )
    assert all(job.cost_microunits is None for job in jobs)


def test_whole_campaign_tts_selects_one_fallback_model_before_any_dispatch(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    _add_cost_job(
        isolated_sessions,
        bound.campaign_id,
        identity="near-soft-warning",
        status="completed",
        cost_microunits=24_990_000,
        reserved_cost_microunits=None,
    )
    result = build_tts_plan_requests(bound.campaign_id)

    assert result["blocked"] is False
    assert result["selected_model"] == "tts-1"
    with isolated_sessions() as db:
        jobs = list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.campaign_id == bound.campaign_id,
                    GenerationJob.provider == "openai_tts",
                )
            )
        )
    assert {job.model for job in jobs} == {"tts-1"}
    assert sorted(job.reserved_cost_microunits for job in jobs) == sorted(
        (int(request["character_count"]) * 15 * 120 + 99) // 100
        for request in result["requests"]
    )
    assert all(job.status == "reserved" for job in jobs)


def test_tts_reservation_rejects_noncanonical_character_count_and_identity(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)

    with pytest.raises(
        WorkflowReplayConflict,
        match="canonical scene narration",
    ):
        reserve_tts_plan(
            campaign_id=bound.campaign_id,
            storyboard_hash=bound.storyboard_hash,
            requests=(
                {
                    "character_count": 1,
                    "input_hash": "a" * 64,
                    "narration_sha256": "b" * 64,
                    "scene_id": bound.scene_ids[0],
                    "scene_position": 0,
                },
            ),
        )


def test_dispatch_requires_intact_reservation_and_completion_cannot_exceed_it(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    plan = build_tts_plan_requests(bound.campaign_id)
    first_job_id = int(plan["job_ids"][0])

    with isolated_sessions() as db:
        job = db.get(GenerationJob, first_job_id)
        assert job is not None
        original_reservation = int(job.reserved_cost_microunits or 0)
        job.reserved_cost_microunits = None
        db.commit()
    with pytest.raises(WorkflowReplayConflict, match="reservation fields"):
        claim_metered_dispatch(first_job_id)

    with isolated_sessions() as db:
        job = db.get(GenerationJob, first_job_id)
        assert job is not None
        job.reserved_cost_microunits = original_reservation
        db.commit()
    claimed = claim_metered_dispatch(first_job_id)
    assert claimed["dispatch"] is True
    with pytest.raises(WorkflowReplayConflict, match="conservative reservation"):
        complete_metered_job(
            job_id=first_job_id,
            artifact_values={},
            final_cost_microunits=original_reservation + 1,
            usage={},
        )


def test_dispatch_recovery_reconciles_reservation_before_marking_ambiguity(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    plan = build_tts_plan_requests(bound.campaign_id)
    first_job_id = int(plan["job_ids"][0])
    assert claim_metered_dispatch(first_job_id)["dispatch"] is True

    with isolated_sessions() as db:
        job = db.get(GenerationJob, first_job_id)
        assert job is not None
        original_reservation = int(job.reserved_cost_microunits or 0)
        job.reserved_cost_microunits = 1
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="reservation accounting"):
        claim_metered_dispatch(first_job_id)

    with isolated_sessions() as db:
        job = db.get(GenerationJob, first_job_id)
        assert job is not None
        assert job.status == "dispatching"
        job.reserved_cost_microunits = original_reservation
        db.commit()

    assert claim_metered_dispatch(first_job_id)["status"] == "ambiguous_dispatch"
    with isolated_sessions() as db:
        job = db.get(GenerationJob, first_job_id)
        assert job is not None
        job.usage_json = "{}"
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="reservation accounting"):
        claim_metered_dispatch(first_job_id)


def test_visual_dispatch_requires_exact_budget_decision_metadata(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    context = production_persistence.load_bound_i5_context(bound.campaign_id)
    with isolated_sessions() as db:
        scene = next(
            item
            for item in db.scalars(
                select(Scene)
                .where(Scene.campaign_id == bound.campaign_id)
                .order_by(Scene.position)
            )
            if scene_contract(item)["visual_mode"] == "GENERATED_CINEMATIC"
        )
        contract = scene_contract(scene)
        scene_id = scene.id
    reservation = reserve_visual_job(
        campaign_id=bound.campaign_id,
        scene_id=scene_id,
        input_hash=visual_input_hash(
            campaign_id=bound.campaign_id,
            scene=contract,
            profile_hash=str(context["profile_hash"]),
            storyboard_hash=bound.storyboard_hash,
        ),
        options=visual_request_options(
            context["profile_payload"],  # type: ignore[arg-type]
            allow_video=False,
        ),
    )
    job_id = int(reservation["job_id"])
    with isolated_sessions() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None and job.usage_json is not None
        usage = json.loads(job.usage_json)
        usage.pop("budget_decision")
        job.usage_json = json.dumps(usage, sort_keys=True, separators=(",", ":"))
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="usage conflicts"):
        claim_metered_dispatch(job_id)


def test_tts_dispatch_reconciles_exact_media_plan_governance_artifact(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    plan = build_tts_plan_requests(bound.campaign_id)
    first_job_id = int(plan["job_ids"][0])
    with isolated_sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == bound.campaign_id,
                Artifact.kind == "i5_media_plan",
            )
        )
        assert artifact is not None and artifact.payload_json is not None
        payload = json.loads(artifact.payload_json)
        payload["budget_decision"] = "BLOCK_NEEDS_HUMAN"
        artifact.payload_json = canonical_json(payload)
        artifact.sha256 = canonical_sha256(payload)
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="media plan payload conflicts"):
        claim_metered_dispatch(first_job_id)


def test_concurrent_reservations_cannot_both_observe_unreserved_cap_capacity(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    barrier = Barrier(2)
    _add_cost_job(
        isolated_sessions,
        bound.campaign_id,
        identity="near-hard-cap",
        status="completed",
        cost_microunits=34_960_000,
        reserved_cost_microunits=None,
    )
    context = production_persistence.load_bound_i5_context(bound.campaign_id)
    with isolated_sessions() as db:
        generated_scenes = [
            scene
            for scene in db.scalars(
                select(Scene)
                .where(Scene.campaign_id == bound.campaign_id)
                .order_by(Scene.position)
            )
            if scene_contract(scene)["visual_mode"] == "GENERATED_CINEMATIC"
        ]
    assert len(generated_scenes) >= 2
    reservations = []
    for scene in generated_scenes[:2]:
        contract = scene_contract(scene)
        reservations.append(
            (
                scene.id,
                visual_input_hash(
                    campaign_id=bound.campaign_id,
                    scene=contract,
                    profile_hash=str(context["profile_hash"]),
                    storyboard_hash=bound.storyboard_hash,
                ),
                visual_request_options(
                    context["profile_payload"],  # type: ignore[arg-type]
                    allow_video=False,
                ),
            )
        )

    def reserve(index: int) -> dict[str, object]:
        barrier.wait(timeout=5)
        scene_id, input_hash, options = reservations[index]
        return reserve_visual_job(
            campaign_id=bound.campaign_id,
            scene_id=scene_id,
            input_hash=input_hash,
            options=options,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, (0, 1)))

    assert sorted(str(result["provider"]) for result in results) == [
        "local",
        "openai_image",
    ]
    with isolated_sessions() as db:
        jobs = list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.campaign_id == bound.campaign_id,
                    GenerationJob.provider == "openai_image",
                )
            )
        )
        cost = production_persistence.campaign_cost(db, bound.campaign_id)
        cap = authorized_campaign_cap(db, bound.campaign_id)
    assert len(jobs) == 1
    assert cost.reserved_microusd == 25_000
    assert cost.total_microusd <= cap == 35_000_000

    local_index = next(
        index for index, result in enumerate(results) if result["provider"] == "local"
    )
    with isolated_sessions() as db:
        pressure = db.scalar(
            select(GenerationJob).where(
                GenerationJob.campaign_id == bound.campaign_id,
                GenerationJob.provider == "budget_test",
                GenerationJob.model == "near-hard-cap",
            )
        )
        assert pressure is not None
        pressure.cost_microunits = 0
        db.commit()
    scene_id, input_hash, options = reservations[local_index]
    local_replay = reserve_visual_job(
        campaign_id=bound.campaign_id,
        scene_id=scene_id,
        input_hash=input_hash,
        options=options,
    )
    assert local_replay["provider"] == "local"
    assert local_replay["selected"] == "deterministic_local"
    assert local_replay["replayed"] is True


def test_owner_override_chain_increases_only_from_current_authorized_cap(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)

    first = create_budget_override(
        campaign_id=bound.campaign_id,
        new_authorized_cap_microusd=40_000_000,
        actor="owner:aatif",
        reason="Authorize complete canonical narration",
    )
    second = create_budget_override(
        campaign_id=bound.campaign_id,
        new_authorized_cap_microusd=45_000_000,
        actor="owner:aatif",
        reason="Authorize one additional generated visual",
    )

    assert first["previous_authorized_cap_microusd"] == 35_000_000
    assert second["previous_authorized_cap_microusd"] == 40_000_000
    with isolated_sessions() as db:
        rows = list(
            db.scalars(
                select(CampaignBudgetOverride)
                .where(CampaignBudgetOverride.campaign_id == bound.campaign_id)
                .order_by(CampaignBudgetOverride.id)
            )
        )
        assert authorized_campaign_cap(db, bound.campaign_id) == 45_000_000
    assert [row.previous_authorized_cap_microunits for row in rows] == [
        35_000_000,
        40_000_000,
    ]
    assert [row.new_authorized_cap_microunits for row in rows] == [
        40_000_000,
        45_000_000,
    ]
    assert all(row.actor == "owner:aatif" for row in rows)


def test_exact_owner_override_duplicate_reconciles_without_a_second_row(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    request = {
        "campaign_id": bound.campaign_id,
        "new_authorized_cap_microusd": 40_000_000,
        "actor": "owner:aatif",
        "reason": "Authorize canonical narration",
    }

    first = create_budget_override(**request)
    replay = create_budget_override(
        **{
            **request,
            "actor": "  owner:aatif  ",
            "reason": "  Authorize canonical narration  ",
        }
    )

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["override_id"] == first["override_id"]
    assert replay["override_hash"] == first["override_hash"]
    with isolated_sessions() as db:
        count = db.scalar(
            select(func.count())
            .select_from(CampaignBudgetOverride)
            .where(CampaignBudgetOverride.campaign_id == bound.campaign_id)
        )
    assert count == 1


def test_lower_equal_or_semantically_conflicting_override_is_rejected(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    create_budget_override(
        campaign_id=bound.campaign_id,
        new_authorized_cap_microusd=40_000_000,
        actor="owner:aatif",
        reason="Initial authorized increase",
    )

    for cap in (40_000_000, 39_000_000):
        with pytest.raises(ValueError, match="strictly increase"):
            create_budget_override(
                campaign_id=bound.campaign_id,
                new_authorized_cap_microusd=cap,
                actor="owner:aatif",
                reason="Conflicting attempt",
            )
    with pytest.raises(ValueError, match="actor and reason must be nonblank"):
        create_budget_override(
            campaign_id=bound.campaign_id,
            new_authorized_cap_microusd=41_000_000,
            actor=" ",
            reason="Invalid actor",
        )

    with isolated_sessions() as db:
        assert authorized_campaign_cap(db, bound.campaign_id) == 40_000_000


def test_tampered_override_chain_fails_closed(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    created = create_budget_override(
        campaign_id=bound.campaign_id,
        new_authorized_cap_microusd=40_000_000,
        actor="owner:aatif",
        reason="Authorized increase",
    )
    with isolated_sessions() as db:
        row = db.get(CampaignBudgetOverride, created["override_id"])
        assert row is not None
        row.reason = "Tampered after authorization"
        db.commit()

    with isolated_sessions() as db:
        with pytest.raises(WorkflowReplayConflict, match="chain is invalid"):
            authorized_campaign_cap(db, bound.campaign_id)


def test_tampered_override_timestamp_fails_closed(
    isolated_sessions: sessionmaker[Session],
) -> None:
    bound = _seed_bound_media_campaign(isolated_sessions)
    created = create_budget_override(
        campaign_id=bound.campaign_id,
        new_authorized_cap_microusd=40_000_000,
        actor="owner:aatif",
        reason="Timestamp-bound authorized increase",
    )
    with isolated_sessions() as db:
        row = db.get(CampaignBudgetOverride, created["override_id"])
        assert row is not None
        row.created_at = row.created_at.replace(year=2000)
        db.commit()

    with isolated_sessions() as db:
        with pytest.raises(WorkflowReplayConflict, match="chain is invalid"):
            authorized_campaign_cap(db, bound.campaign_id)


def test_state_free_provider_module_has_no_cap_mutation_surface(
    isolated_sessions: sessionmaker[Session],
) -> None:
    with isolated_sessions() as db:
        channel = Channel(
            name="Provider isolation channel",
            niche="test",
            audience="test",
            brand_voice="test",
            visual_style="test",
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
        before = authorized_campaign_cap(db, campaign_id)

    assert "SessionLocal" not in vars(provider_module)
    assert "CampaignBudgetOverride" not in vars(provider_module)
    for provider_type in (
        provider_module.OpenAITTSProvider,
        provider_module.GPTImageProvider,
        provider_module.SoraProvider,
    ):
        assert not hasattr(provider_type, "create_budget_override")
        assert not hasattr(provider_type, "authorized_campaign_cap")
        provider_type()

    with isolated_sessions() as db:
        assert authorized_campaign_cap(db, campaign_id) == before == 35_000_000
