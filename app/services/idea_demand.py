"""Demand-driven idea ranking.

Scores candidate video topics by estimated demand vs. saturation so an operator
picks topics that have pull, not just topics from a static seed list.

Design goals:
* Deterministic — same inputs always produce the same ranking.
* Works with no API key (keyword/lane heuristic).
* Enhanced when real source videos are supplied (view velocity, recency,
  saturation derived from actual YouTube search results via the existing
  research fetch).
* Never makes claims it can't support — every score carries a plain rationale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.opportunity_intake import normalize_text
from app.services.research import SourceVideo

_STOP_WORDS = {
    "the", "and", "for", "with", "from", "into", "your", "that", "this", "how",
    "best", "video", "youtube", "ai", "to", "of", "a", "in", "on", "by", "is",
    "are", "you", "it", "an", "or", "as", "at", "be", "we",
}


@dataclass(frozen=True)
class RankedIdea:
    topic: str
    demand_score: int  # 0-100
    saturation_risk: str  # "low" | "medium" | "high"
    rationale: str
    signals: dict[str, Any] = field(default_factory=dict)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in normalize_text(text).split(" ")
        if len(token) >= 3 and token not in _STOP_WORDS
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _recency_weight(published_at: datetime | None, *, now: datetime) -> float:
    if published_at is None:
        return 0.5
    moment = published_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - moment).total_seconds() / 86400.0)
    # ~6 month half-life: recent demand counts more.
    return _clamp01(math.exp(-age_days / 180.0))


def _view_velocity(video: SourceVideo, *, now: datetime) -> float:
    if not video.view_count or video.view_count <= 0:
        return 0.0
    age_days = 30.0
    if video.published_at is not None:
        moment = video.published_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        age_days = max(1.0, (now - moment).total_seconds() / 86400.0)
    per_day = video.view_count / age_days
    # Log-normalize: 10k views/day -> ~1.0
    return _clamp01(math.log10(per_day + 1.0) / 4.0)


def suggest_candidate_topics(
    *,
    niche_lane: str,
    query: str,
    source_videos: list[SourceVideo] | None = None,
    limit: int = 8,
) -> list[str]:
    """Build a deterministic set of candidate topics to rank."""
    lane = niche_lane.strip() or "operator workflow"
    q = query.strip()
    candidates: list[str] = []

    if q:
        candidates.append(f"How to {q} as a {lane} operator (step-by-step)")
        candidates.append(f"{q}: the workflow I actually use (with caveats)")
        candidates.append(f"{lane} vs manual: {q} compared honestly")

    # Token-driven angles from real source titles when available.
    token_counts: dict[str, int] = {}
    for video in source_videos or []:
        for token in _tokens(video.title):
            token_counts[token] = token_counts.get(token, 0) + 1
    top_tokens = [tok for tok, _ in sorted(token_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]]
    for token in top_tokens:
        candidates.append(f"The {token} workflow most {lane} operators get wrong")

    # Evergreen lane fallbacks.
    candidates.extend(
        [
            f"{lane} system teardown with manual review checkpoints",
            f"{lane} checklist for consistent output (no hype)",
            f"What nobody tells you about {lane}",
        ]
    )

    # De-dupe preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for topic in candidates:
        key = normalize_text(topic)
        if key and key not in seen:
            seen.add(key)
            unique.append(topic.strip())
    return unique[:limit]


def score_ideas(
    *,
    niche_lane: str,
    query: str,
    candidate_topics: list[str],
    source_videos: list[SourceVideo] | None = None,
    now: datetime | None = None,
) -> list[RankedIdea]:
    """Rank candidate topics by estimated demand. Deterministic."""
    now = now or datetime.now(timezone.utc)
    sources = source_videos or []
    has_live = len(sources) > 0

    corpus_tokens: set[str] = set()
    lane_query_tokens = _tokens(niche_lane) | _tokens(query)
    for video in sources:
        corpus_tokens |= _tokens(video.title)
    if not corpus_tokens:
        corpus_tokens = lane_query_tokens

    # Saturation: fraction of near-duplicate source titles overall.
    normalized_titles = [normalize_text(v.title) for v in sources if v.title.strip()]
    duplicate_ratio = 0.0
    if normalized_titles:
        duplicate_ratio = 1.0 - (len(set(normalized_titles)) / len(normalized_titles))

    ranked: list[RankedIdea] = []
    for topic in candidate_topics:
        topic = topic.strip()
        if not topic:
            continue
        t_tokens = _tokens(topic)
        if not t_tokens:
            continue

        # 1) Relevance: overlap with the demand corpus.
        relevance = len(t_tokens & corpus_tokens) / len(t_tokens)

        # 2) Demand: real view velocity of matching sources, else keyword proxy.
        matching = [v for v in sources if _tokens(v.title) & t_tokens]
        if has_live and matching:
            demand = sum(_view_velocity(v, now=now) for v in matching) / len(matching)
            freshness = sum(_recency_weight(v.published_at, now=now) for v in matching) / len(matching)
        else:
            # No live data (or no match): proxy from lane/query keyword fit.
            overlap = len(t_tokens & lane_query_tokens) / len(t_tokens)
            demand = 0.4 + 0.4 * overlap
            freshness = 0.5

        # 3) Saturation penalty: high when matching titles look repetitive.
        local_titles = [normalize_text(v.title) for v in matching if v.title.strip()]
        local_dup = 0.0
        if local_titles:
            local_dup = 1.0 - (len(set(local_titles)) / len(local_titles))
        saturation = max(duplicate_ratio, local_dup) if has_live else 0.2

        raw = (0.35 * relevance + 0.40 * _clamp01(demand) + 0.15 * freshness)
        score = raw * (1.0 - 0.30 * saturation)
        demand_score = max(1, min(100, round(score * 100)))

        if saturation >= 0.45:
            sat_label = "high"
        elif saturation >= 0.25:
            sat_label = "medium"
        else:
            sat_label = "low"

        if has_live and matching:
            rationale = (
                f"{len(matching)} live source(s) match this angle; "
                f"relevance {relevance:.0%}, demand {demand:.0%}, freshness {freshness:.0%}, "
                f"saturation {sat_label}."
            )
        else:
            rationale = (
                f"No live signal available; scored from lane/query keyword fit "
                f"(relevance {relevance:.0%}). Connect YOUTUBE_DATA_API_KEY for real demand signals."
            )

        ranked.append(
            RankedIdea(
                topic=topic,
                demand_score=demand_score,
                saturation_risk=sat_label,
                rationale=rationale,
                signals={
                    "relevance": round(relevance, 3),
                    "demand": round(_clamp01(demand), 3),
                    "freshness": round(freshness, 3),
                    "saturation": round(saturation, 3),
                    "matching_sources": len(matching),
                    "live_signal": bool(has_live and matching),
                },
            )
        )

    # Sort by score desc, then topic for stable deterministic ties.
    ranked.sort(key=lambda idea: (-idea.demand_score, idea.topic))
    return ranked
