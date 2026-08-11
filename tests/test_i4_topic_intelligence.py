from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.editorial.contracts import (
    CandidateDemandSnapshot,
    ClaimSeed,
    DemandSnapshot,
    DemandVideo,
    EditorialSeed,
    EvidenceSourceInput,
    TopicCandidate,
    ViewerPromise,
)
from app.editorial.demand import (
    DemandProviderError,
    DemandRequest,
    YouTubeDemandProvider,
)
from app.editorial.topic_intelligence import (
    COMMERCIAL_SCORE_WEIGHTS,
    compile_topic_packet,
    current_demand_score,
    novelty_score,
    score_candidate,
    topic_hard_failures,
)


RETRIEVED_AT = "2026-08-11T12:00:00+00:00"
CHANNEL_NICHE = "AI automation creator systems"
CHANNEL_AUDIENCE = "creator operators and small business teams"


def _source(
    source_key: str,
    source_class: str,
    publisher: str,
) -> EvidenceSourceInput:
    return EvidenceSourceInput(
        source_key=source_key,
        source_uri=f"https://evidence.example/{source_key}",
        publisher=publisher,
        source_class=source_class,
        evidence_snippet=f"Bounded evidence excerpt for {source_key}.",
    )


def _claim(
    assertion_text: str,
    *,
    claim_type: str = "fact",
    role: str = "evidence",
    source_keys: tuple[str, ...] = ("official-data",),
    attribution: str | None = None,
    assumptions: tuple[str, ...] = (),
) -> ClaimSeed:
    return ClaimSeed(
        assertion_text=assertion_text,
        material=True,
        claim_type=claim_type,
        role=role,
        source_keys=source_keys,
        attribution=attribution,
        assumptions=assumptions,
    )


def _candidate(
    candidate_key: str = "operator-evidence",
    *,
    topic: str = "AI automation evidence systems for creator operators",
    angle: str = "A skeptical source-backed creator operations investigation",
    sponsor_categories: tuple[str, ...] = ("AI automation creator software",),
    sensitivity_tags: tuple[str, ...] = (),
) -> TopicCandidate:
    return TopicCandidate(
        candidate_key=candidate_key,
        topic=topic,
        angle=angle,
        target_viewer="Creator operators and small business teams",
        viewer_promise="A source-backed map of what the workflow can and cannot do.",
        core_question="Can this automation workflow improve creator operations without hype?",
        why_now="Current public adoption evidence makes the operational tradeoffs timely.",
        stakes="Teams can waste time, trust, and scarce operating resources on weak systems.",
        novelty="The analysis compares evidence and uncertainty instead of repeating product claims.",
        broad_interest_bridge="The same evidence discipline affects how people choose consequential software.",
        expected_takeaway="Viewers will leave with a practical evidence checklist and explicit limits.",
        visual_modes=(
            "DOCUMENT",
            "DIAGRAM",
            "DATA_VISUALIZATION",
            "UI_RECONSTRUCTION",
        ),
        shelf_life="evergreen",
        sponsor_categories=sponsor_categories,
        sensitivity_tags=sensitivity_tags,
        evidence_sources=(
            _source("official-data", "official", "Public Data Office"),
            _source("secondary-report", "reputable_secondary", "Independent Review"),
            _source("company-statement", "company_claim", "Example Company"),
        ),
        claims=(
            _claim("The official dataset defines the observed workflow sample.", role="context"),
            _claim("The observed workflow completed the documented processing step."),
            _claim("A failed handoff can consume operator time and reduce trust.", role="stakes"),
            _claim(
                "The company describes the feature as an operational assistant.",
                claim_type="attributed_claim",
                role="counterpoint",
                source_keys=("company-statement",),
                attribution="Example Company",
            ),
            _claim(
                "The available sample could suggest a bounded efficiency improvement.",
                claim_type="estimate",
                role="uncertainty",
                source_keys=("secondary-report",),
                assumptions=("The documented sample is representative of this workflow.",),
            ),
            _claim(
                "Editorial caution is more useful than treating a demo as proof.",
                claim_type="opinion",
                role="outlook",
                source_keys=(),
            ),
        ),
    )


def _replace_candidate(candidate: TopicCandidate, **updates: object) -> TopicCandidate:
    payload = candidate.model_dump(mode="python")
    payload.update(updates)
    return TopicCandidate.model_validate(payload)


def _video(
    index: int,
    *,
    age_days: int,
    views_per_day: int,
    engagement_rate: float = 0.08,
) -> DemandVideo:
    retrieved = datetime.fromisoformat(RETRIEVED_AT)
    views = max(1, age_days * views_per_day)
    likes = round(views * engagement_rate * 0.75)
    comments = round(views * engagement_rate * 0.25)
    return DemandVideo(
        video_id=f"video-{index}",
        title=f"Public workflow evidence result {index}",
        channel_id=f"channel-{index}",
        channel_title=f"Public Channel {index}",
        published_at=(retrieved - timedelta(days=age_days)).isoformat(),
        view_count=views,
        like_count=likes,
        comment_count=comments,
        channel_subscriber_count=10_000 + index,
    )


def _snapshot(
    candidate: TopicCandidate,
    *,
    views_per_day: int = 1_000_000,
    result_count: int = 10,
) -> CandidateDemandSnapshot:
    return CandidateDemandSnapshot(
        candidate_key=candidate.candidate_key,
        query=f"{candidate.topic} {candidate.angle}",
        videos=tuple(
            _video(
                index,
                age_days=10 + index,
                views_per_day=views_per_day,
            )
            for index in range(result_count)
        ),
    )


def _demand(
    *candidates: TopicCandidate,
    velocities: dict[str, int] | None = None,
) -> DemandSnapshot:
    velocity_by_key = velocities or {}
    return DemandSnapshot(
        retrieved_at=RETRIEVED_AT,
        candidates=tuple(
            _snapshot(
                candidate,
                views_per_day=velocity_by_key.get(candidate.candidate_key, 1_000_000),
            )
            for candidate in candidates
        ),
    )


def _compile(
    seed: EditorialSeed,
    demand: DemandSnapshot,
    *,
    history: tuple[str, ...] = (),
) -> dict[str, object]:
    return compile_topic_packet(
        campaign_id=41,
        seed_hash=seed.sha256(41),
        seed=seed,
        demand=demand,
        channel_niche=CHANNEL_NICHE,
        channel_audience=CHANNEL_AUDIENCE,
        novelty_history=history,
    )


def test_commercial_weights_are_exact_and_total_one_hundred() -> None:
    assert COMMERCIAL_SCORE_WEIGHTS == {
        "audience_relevance": 15,
        "current_demand": 20,
        "novelty": 10,
        "consequence_stakes": 10,
        "narrative_tension": 10,
        "broad_interest_bridge": 10,
        "visual_potential": 8,
        "shelf_life": 7,
        "authority_opportunity": 7,
        "sponsor_compatibility": 3,
    }
    assert sum(COMMERCIAL_SCORE_WEIGHTS.values()) == 100


def test_topic_compilation_is_deterministic_and_all_dimensions_are_bounded() -> None:
    candidate = _candidate()
    seed = EditorialSeed(candidates=(candidate,))
    demand = _demand(candidate)

    first = _compile(seed, demand)
    second = _compile(seed, demand)

    assert first == second
    assert first["gate"] == {
        "outcome": "PASS",
        "reasons": ["topic_is_sourceable_safe_and_editorially_ready"],
    }
    breakdown = first["commercial_score_breakdown"]
    assert isinstance(breakdown, dict)
    assert set(breakdown) == set(COMMERCIAL_SCORE_WEIGHTS)
    assert all(0 <= int(value) <= 100 for value in breakdown.values())


def test_current_demand_uses_fixed_snapshot_time_and_documented_composition() -> None:
    retrieved = datetime.fromisoformat(RETRIEVED_AT)
    snapshot = CandidateDemandSnapshot(
        candidate_key="demand-math",
        query="bounded demand math",
        videos=(
            DemandVideo(
                video_id="recent",
                title="Recent result",
                published_at=(retrieved - timedelta(days=10)).isoformat(),
                view_count=1_000,
                like_count=40,
                comment_count=10,
            ),
            DemandVideo(
                video_id="older",
                title="Older result",
                published_at=(retrieved - timedelta(days=100)).isoformat(),
                view_count=1_000,
                like_count=40,
                comment_count=10,
            ),
        ),
    )

    score, summary = current_demand_score(snapshot, retrieved_at=RETRIEVED_AT)

    assert score == 39
    assert summary == {
        "coverage_score": 20,
        "engagement_score": 62,
        "median_engagement_rate": 0.05,
        "median_views_per_day": 55.0,
        "recent_share_score": 50,
        "result_count": 2,
        "velocity_score": 29,
    }


def test_one_viral_result_cannot_dominate_median_demand_signals() -> None:
    ordinary = tuple(
        _video(index, age_days=30, views_per_day=100)
        for index in range(9)
    )
    viral = _video(99, age_days=30, views_per_day=1_000_000_000)
    baseline = CandidateDemandSnapshot(
        candidate_key="viral-resistance",
        query="viral resistance",
        videos=ordinary,
    )
    with_outlier = CandidateDemandSnapshot(
        candidate_key="viral-resistance",
        query="viral resistance",
        videos=(*ordinary[:-1], viral),
    )

    baseline_score, baseline_summary = current_demand_score(
        baseline,
        retrieved_at=RETRIEVED_AT,
    )
    outlier_score, outlier_summary = current_demand_score(
        with_outlier,
        retrieved_at=RETRIEVED_AT,
    )

    assert outlier_score == baseline_score
    assert outlier_summary["median_views_per_day"] == baseline_summary["median_views_per_day"]
    assert outlier_summary["median_engagement_rate"] == baseline_summary["median_engagement_rate"]


def test_missing_demand_metrics_are_not_fabricated() -> None:
    snapshot = CandidateDemandSnapshot(
        candidate_key="missing-metrics",
        query="missing metrics",
        videos=(DemandVideo(video_id="unknown", title="Unknown metrics"),),
    )

    score, summary = current_demand_score(snapshot, retrieved_at=RETRIEVED_AT)

    assert score == 2
    assert summary["coverage_score"] == 10
    assert summary["recent_share_score"] == 0
    assert summary["velocity_score"] == 0
    assert summary["engagement_score"] == 0


def test_novelty_uses_topic_and_angle_history_without_performance_metrics() -> None:
    candidate = _candidate()
    exact_history = f"{candidate.topic} {candidate.angle}"

    assert novelty_score(candidate, ()) == 100
    assert novelty_score(candidate, (exact_history,)) == 0
    assert novelty_score(candidate, ("marine biology field notes",)) >= 80


@pytest.mark.parametrize(
    ("updates", "reason_fragment"),
    [
        ({"sensitivity_tags": ("politics_elections",)}, "excluded_sensitivity_tag:politics_elections"),
        ({"topic": "An investment strategy for retirement funds"}, "excluded_sensitive_text:finance"),
        ({"viewer_promise": "A guaranteed workflow result for every operator."}, "blocked_compliance_phrase:"),
    ],
)
def test_sensitive_and_blocked_topics_are_hard_rejected(
    updates: dict[str, object],
    reason_fragment: str,
) -> None:
    candidate = _replace_candidate(_candidate(), **updates)

    failures = topic_hard_failures(candidate)

    assert any(reason_fragment in reason for reason in failures)


def test_sourceability_failure_is_not_rescued_by_a_high_commercial_score() -> None:
    candidate = _candidate()
    discovery_sources = tuple(
        source.model_copy(update={"source_class": "discovery_only"})
        for source in candidate.evidence_sources
    )
    candidate = _replace_candidate(candidate, evidence_sources=discovery_sources)
    demand = _demand(candidate)
    scored = score_candidate(
        candidate,
        demand=demand,
        channel_niche=CHANNEL_NICHE,
        channel_audience=CHANNEL_AUDIENCE,
        novelty_history=(),
    )
    packet = _compile(EditorialSeed(candidates=(candidate,)), demand)

    assert int(scored["commercial_score"]) >= 60
    assert scored["eligible"] is False
    assert "fewer_than_two_non_discovery_sources" in scored["hard_failure_reasons"]
    assert packet["gate"] == {
        "outcome": "FAIL",
        "reasons": ["no_hard_eligible_candidate"],
    }
    assert packet["selected_candidate_key"] is None


def test_sponsor_score_cannot_rescue_source_failure_or_change_safety_truth() -> None:
    base = _candidate(sponsor_categories=())
    discovery_sources = tuple(
        source.model_copy(update={"source_class": "discovery_only"})
        for source in base.evidence_sources
    )
    without_sponsor = _replace_candidate(base, evidence_sources=discovery_sources)
    with_sponsor = _replace_candidate(
        without_sponsor,
        sponsor_categories=("AI automation creator operators",),
    )
    demand = _demand(without_sponsor, with_sponsor)

    low = score_candidate(
        without_sponsor,
        demand=demand,
        channel_niche=CHANNEL_NICHE,
        channel_audience=CHANNEL_AUDIENCE,
        novelty_history=(),
    )
    high = score_candidate(
        with_sponsor,
        demand=demand,
        channel_niche=CHANNEL_NICHE,
        channel_audience=CHANNEL_AUDIENCE,
        novelty_history=(),
    )

    assert low["commercial_score_breakdown"]["sponsor_compatibility"] == 0  # type: ignore[index]
    assert high["commercial_score_breakdown"]["sponsor_compatibility"] > 0  # type: ignore[index]
    assert low["eligible"] is False
    assert high["eligible"] is False
    assert low["hard_failure_reasons"] == high["hard_failure_reasons"]


def test_viewer_promise_has_exact_required_fields_and_blank_input_is_rejected() -> None:
    expected_fields = {
        "viewer_promise",
        "core_question",
        "why_now",
        "stakes",
        "novelty",
        "target_viewer",
        "broad_interest_bridge",
        "expected_takeaway",
    }
    candidate = _candidate()

    assert set(ViewerPromise.model_fields) == expected_fields
    assert set(candidate.viewer_promise_contract().model_dump()) == expected_fields

    payload = candidate.model_dump(mode="python")
    payload["core_question"] = "   "
    with pytest.raises(ValidationError):
        TopicCandidate.model_validate(payload)


def test_controlled_exploration_selects_highest_novelty_bounded_alternative() -> None:
    winner = _candidate(candidate_key="a-winner")
    familiar = _candidate(candidate_key="b-familiar")
    novel = _candidate(
        candidate_key="c-novel",
        topic="Creator systems evidence map for automation operators",
        angle="A new operational map of overlooked creator evidence",
    )
    seed = EditorialSeed(candidates=(winner, familiar, novel))
    demand = _demand(
        winner,
        familiar,
        novel,
        velocities={
            winner.candidate_key: 1_000_000,
            familiar.candidate_key: 10,
            novel.candidate_key: 10,
        },
    )
    history = (f"{winner.topic} {winner.angle}",)

    packet = _compile(seed, demand, history=history)

    assert packet["selected_candidate_key"] == "a-winner"
    assert packet["exploration_candidate_key"] == "c-novel"
    ranked = {item["candidate_key"]: item for item in packet["ranked_candidates"]}
    assert (
        ranked["c-novel"]["commercial_score_breakdown"]["novelty"]
        > ranked["b-familiar"]["commercial_score_breakdown"]["novelty"]
    )
    assert (
        ranked["a-winner"]["commercial_score"]
        - ranked["c-novel"]["commercial_score"]
        <= 15
    )


def test_seed_rejects_more_than_three_candidates() -> None:
    candidates = tuple(
        _candidate(candidate_key=f"candidate-{index}")
        for index in range(4)
    )

    with pytest.raises(ValidationError):
        EditorialSeed(candidates=candidates)


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://",
        "https:///missing-host",
        "https://user:password@example.com/evidence",
        "https://example.com:invalid-port/evidence",
        "https://example.com/has whitespace",
    ),
)
def test_evidence_source_rejects_invalid_or_impossible_https_references(
    source_uri: str,
) -> None:
    with pytest.raises(ValidationError, match="valid HTTPS URL"):
        EvidenceSourceInput(
            source_key="invalid-reference",
            source_uri=source_uri,
            publisher="Invalid Source",
            source_class="official",
            evidence_snippet="This record must never pass sourceability validation.",
        )


def test_missing_youtube_key_fails_before_any_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    seed = EditorialSeed(candidates=(candidate,))
    request = DemandRequest.from_seed(
        campaign_id=41,
        seed_hash=seed.sha256(41),
        seed=seed,
    )
    calls = 0

    def forbidden_fetch(**_: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("fetch must not run without an API key")

    monkeypatch.setattr("app.editorial.demand.fetch_youtube_sources", forbidden_fetch)

    with pytest.raises(DemandProviderError, match="configuration is unavailable"):
        YouTubeDemandProvider().acquire(request, api_key="   ")
    assert calls == 0
