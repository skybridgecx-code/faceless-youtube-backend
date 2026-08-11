from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone

from .contracts import (
    CandidateDemandSnapshot,
    DemandSnapshot,
    EditorialSeed,
    I4_POLICY_VERSION,
    TopicCandidate,
    canonical_sha256,
)
from .safety import compliance_failures, sensitivity_failures


I4_MIN_COMMERCIAL_SCORE = 60
I4_EXPLORATION_MAX_SCORE_DELTA = 15

COMMERCIAL_SCORE_WEIGHTS: dict[str, int] = {
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

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_QUESTION_WORDS = ("how", "why", "what", "when", "where", "which", "can", "will", "does", "is")


def normalized_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _TOKEN_PATTERN.findall(value.casefold())
        if len(token) > 1
    )


def _bounded_score(value: float) -> int:
    return max(0, min(100, round(value)))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def audience_relevance_score(
    candidate: TopicCandidate,
    *,
    channel_niche: str,
    channel_audience: str,
) -> int:
    channel_tokens = normalized_tokens(f"{channel_niche} {channel_audience}")
    candidate_tokens = normalized_tokens(
        f"{candidate.topic} {candidate.target_viewer} {candidate.angle}"
    )
    if not channel_tokens or not candidate_tokens:
        return 0
    overlap = channel_tokens & candidate_tokens
    channel_coverage = len(overlap) / len(channel_tokens)
    candidate_coverage = len(overlap) / len(candidate_tokens)
    return _bounded_score(100 * ((0.7 * channel_coverage) + (0.3 * candidate_coverage)))


def _parse_snapshot_time(value: str) -> datetime:
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def current_demand_score(
    snapshot: CandidateDemandSnapshot,
    *,
    retrieved_at: str,
) -> tuple[int, dict[str, object]]:
    retrieval_time = _parse_snapshot_time(retrieved_at)
    videos = snapshot.videos
    coverage = _bounded_score(100 * (len(videos) / 10))

    recent_flags: list[bool] = []
    velocities: list[float] = []
    engagement_rates: list[float] = []
    for video in videos:
        published_at = (
            _parse_snapshot_time(video.published_at)
            if video.published_at is not None
            else None
        )
        if published_at is not None:
            age_days = max((retrieval_time - published_at).total_seconds() / 86_400, 1.0)
            recent_flags.append(age_days <= 90)
            if video.view_count is not None:
                velocities.append(video.view_count / age_days)
        if video.view_count and (
            video.like_count is not None or video.comment_count is not None
        ):
            engagement_rates.append(
                ((video.like_count or 0) + (video.comment_count or 0))
                / video.view_count
            )

    recent_share = (
        _bounded_score(100 * (sum(recent_flags) / len(recent_flags)))
        if recent_flags
        else 0
    )
    median_velocity = statistics.median(velocities) if velocities else 0.0
    velocity_score = _bounded_score(
        100 * min(1.0, math.log10(1.0 + median_velocity) / 6.0)
    )
    median_engagement = statistics.median(engagement_rates) if engagement_rates else 0.0
    engagement_score = _bounded_score(100 * min(1.0, median_engagement / 0.08))

    score = _bounded_score(
        (coverage * 0.20)
        + (recent_share * 0.25)
        + (velocity_score * 0.35)
        + (engagement_score * 0.20)
    )
    return score, {
        "coverage_score": coverage,
        "engagement_score": engagement_score,
        "median_engagement_rate": round(median_engagement, 8),
        "median_views_per_day": round(median_velocity, 4),
        "recent_share_score": recent_share,
        "result_count": len(videos),
        "velocity_score": velocity_score,
    }


def novelty_score(candidate: TopicCandidate, history: tuple[str, ...]) -> int:
    if not history:
        return 100
    candidate_tokens = normalized_tokens(f"{candidate.topic} {candidate.angle}")
    maximum_similarity = max(
        (_jaccard(candidate_tokens, normalized_tokens(item)) for item in history),
        default=0.0,
    )
    return _bounded_score(100 * (1.0 - maximum_similarity))


def consequence_stakes_score(candidate: TopicCandidate) -> int:
    substantive_text = min(60, len(normalized_tokens(candidate.stakes)) * 6)
    source_keys = {
        source.source_key
        for source in candidate.evidence_sources
        if source.source_class != "discovery_only"
    }
    backed_stakes = sum(
        1
        for claim in candidate.claims
        if claim.material
        and claim.role == "stakes"
        and bool(set(claim.source_keys) & source_keys)
    )
    return _bounded_score(substantive_text + min(40, backed_stakes * 20))


def narrative_tension_score(candidate: TopicCandidate) -> int:
    question = candidate.core_question.strip().casefold()
    question_framing = "?" in question or question.startswith(_QUESTION_WORDS)
    roles = {claim.role for claim in candidate.claims if claim.material}
    return (
        (40 if question_framing else 0)
        + (30 if "counterpoint" in roles else 0)
        + (30 if "uncertainty" in roles else 0)
    )


def broad_interest_score(candidate: TopicCandidate) -> int:
    bridge_tokens = normalized_tokens(candidate.broad_interest_bridge)
    takeaway_tokens = normalized_tokens(candidate.expected_takeaway)
    target_tokens = normalized_tokens(candidate.target_viewer)
    return _bounded_score(
        min(50, len(bridge_tokens) * 5)
        + min(30, len(takeaway_tokens) * 3)
        + min(20, len(target_tokens) * 4)
    )


def authority_score(candidate: TopicCandidate) -> int:
    classes = [source.source_class for source in candidate.evidence_sources]
    if any(source_class in {"primary", "official"} for source_class in classes):
        return 100
    independent_secondary_publishers = {
        source.publisher.casefold().strip()
        for source in candidate.evidence_sources
        if source.source_class == "reputable_secondary"
    }
    if len(independent_secondary_publishers) >= 2:
        return 80
    if independent_secondary_publishers:
        return 50
    if "company_claim" in classes:
        return 25
    return 0


def sponsor_compatibility_score(candidate: TopicCandidate) -> int:
    if not candidate.sponsor_categories:
        return 0
    topic_tokens = normalized_tokens(
        f"{candidate.topic} {candidate.angle} {candidate.target_viewer}"
    )
    sponsor_tokens = normalized_tokens(" ".join(candidate.sponsor_categories))
    if not sponsor_tokens:
        return 0
    return _bounded_score(100 * (len(topic_tokens & sponsor_tokens) / len(sponsor_tokens)))


def topic_hard_failures(candidate: TopicCandidate) -> tuple[str, ...]:
    failures = set(sensitivity_failures(candidate)) | set(compliance_failures(candidate))
    source_keys = {source.source_key for source in candidate.evidence_sources}
    non_discovery_sources = [
        source
        for source in candidate.evidence_sources
        if source.source_class != "discovery_only"
    ]
    if len(non_discovery_sources) < 2:
        failures.add("fewer_than_two_non_discovery_sources")

    material_claims = [claim for claim in candidate.claims if claim.material]
    if len(material_claims) < 6:
        failures.add("fewer_than_six_material_claims")

    viewer_promise = candidate.viewer_promise_contract().model_dump()
    if any(not str(value).strip() for value in viewer_promise.values()):
        failures.add("viewer_promise_incomplete")

    for index, claim in enumerate(candidate.claims):
        unknown_keys = sorted(set(claim.source_keys) - source_keys)
        if unknown_keys:
            failures.add(
                f"claim_{index}_unknown_source_keys:{','.join(unknown_keys)}"
            )
        if (
            claim.material
            and claim.claim_type in {"fact", "attributed_claim"}
            and not claim.source_keys
        ):
            failures.add(f"claim_{index}_material_factual_claim_has_no_sources")
    return tuple(sorted(failures))


def _demand_for_candidate(
    demand: DemandSnapshot,
    candidate_key: str,
) -> CandidateDemandSnapshot:
    for snapshot in demand.candidates:
        if snapshot.candidate_key == candidate_key:
            return snapshot
    return CandidateDemandSnapshot(
        candidate_key=candidate_key,
        query=candidate_key,
        videos=(),
    )


def score_candidate(
    candidate: TopicCandidate,
    *,
    demand: DemandSnapshot,
    channel_niche: str,
    channel_audience: str,
    novelty_history: tuple[str, ...],
) -> dict[str, object]:
    candidate_demand = _demand_for_candidate(demand, candidate.candidate_key)
    demand_score, demand_summary = current_demand_score(
        candidate_demand,
        retrieved_at=demand.retrieved_at,
    )
    breakdown = {
        "audience_relevance": audience_relevance_score(
            candidate,
            channel_niche=channel_niche,
            channel_audience=channel_audience,
        ),
        "authority_opportunity": authority_score(candidate),
        "broad_interest_bridge": broad_interest_score(candidate),
        "consequence_stakes": consequence_stakes_score(candidate),
        "current_demand": demand_score,
        "narrative_tension": narrative_tension_score(candidate),
        "novelty": novelty_score(candidate, novelty_history),
        "shelf_life": {
            "breaking": 30,
            "near_term": 60,
            "mixed": 80,
            "evergreen": 100,
        }[candidate.shelf_life],
        "sponsor_compatibility": sponsor_compatibility_score(candidate),
        "visual_potential": min(100, len(set(candidate.visual_modes)) * 25),
    }
    weighted_total = sum(
        breakdown[dimension] * weight
        for dimension, weight in COMMERCIAL_SCORE_WEIGHTS.items()
    )
    score = (weighted_total + 50) // 100
    failures = topic_hard_failures(candidate)
    return {
        "candidate_key": candidate.candidate_key,
        "commercial_score": score,
        "commercial_score_breakdown": breakdown,
        "demand_evidence": candidate_demand.model_dump(mode="json"),
        "demand_summary": demand_summary,
        "eligible": not failures,
        "hard_failure_reasons": list(failures),
        "topic": candidate.topic,
        "angle": candidate.angle,
    }


def compile_topic_packet(
    *,
    campaign_id: int,
    seed_hash: str,
    seed: EditorialSeed,
    demand: DemandSnapshot,
    channel_niche: str,
    channel_audience: str,
    novelty_history: tuple[str, ...] = (),
) -> dict[str, object]:
    ranked = [
        score_candidate(
            candidate,
            demand=demand,
            channel_niche=channel_niche,
            channel_audience=channel_audience,
            novelty_history=novelty_history,
        )
        for candidate in seed.candidates
    ]
    ranked.sort(
        key=lambda item: (
            not bool(item["eligible"]),
            -int(item["commercial_score"]),
            str(item["candidate_key"]),
        )
    )
    eligible = [item for item in ranked if bool(item["eligible"])]
    selected = eligible[0] if eligible else None

    gate_reasons: list[str] = []
    gate_outcome = "PASS"
    if selected is None:
        gate_outcome = "FAIL"
        gate_reasons.append("no_hard_eligible_candidate")
    elif int(selected["commercial_score"]) < I4_MIN_COMMERCIAL_SCORE:
        gate_outcome = "FAIL"
        gate_reasons.append("commercial_score_below_editorial_readiness_floor")
    else:
        gate_reasons.append("topic_is_sourceable_safe_and_editorially_ready")

    exploration: dict[str, object] | None = None
    if selected is not None:
        alternatives = [
            item
            for item in eligible[1:]
            if int(selected["commercial_score"]) - int(item["commercial_score"])
            <= I4_EXPLORATION_MAX_SCORE_DELTA
        ]
        alternatives.sort(
            key=lambda item: (
                -int(
                    dict(item["commercial_score_breakdown"])["novelty"]  # type: ignore[arg-type]
                ),
                -int(item["commercial_score"]),
                str(item["candidate_key"]),
            )
        )
        exploration = alternatives[0] if alternatives else None

    candidate_by_key = {candidate.candidate_key: candidate for candidate in seed.candidates}
    selected_candidate = (
        candidate_by_key[str(selected["candidate_key"])]
        if selected is not None
        else None
    )
    return {
        "campaign_id": campaign_id,
        "commercial_score": (
            int(selected["commercial_score"]) if selected is not None else None
        ),
        "commercial_score_breakdown": (
            selected["commercial_score_breakdown"] if selected is not None else None
        ),
        "compilation_context": {
            "channel_audience": channel_audience,
            "channel_niche": channel_niche,
            "novelty_history": list(novelty_history),
        },
        "contract_version": "i4-topic-packet-v1",
        "demand_snapshot_hash": canonical_sha256(demand.model_dump(mode="json")),
        "demand_evidence_summary": (
            selected["demand_summary"] if selected is not None else None
        ),
        "exploration_candidate_key": (
            str(exploration["candidate_key"]) if exploration is not None else None
        ),
        "gate": {"outcome": gate_outcome, "reasons": gate_reasons},
        "hard_gate_results": {
            str(item["candidate_key"]): {
                "eligible": bool(item["eligible"]),
                "reasons": item["hard_failure_reasons"],
            }
            for item in ranked
        },
        "policy_version": I4_POLICY_VERSION,
        "ranked_candidates": ranked,
        "retrieved_at": demand.retrieved_at,
        "seed_hash": seed_hash,
        "selected_candidate_key": (
            selected_candidate.candidate_key if selected_candidate is not None else None
        ),
        "viewer_promise": (
            selected_candidate.viewer_promise_contract().model_dump(mode="json")
            if selected_candidate is not None
            else None
        ),
    }
