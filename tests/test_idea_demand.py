from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_idea_demand.db")
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_idea_test_"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.idea_demand import score_ideas, suggest_candidate_topics  # noqa: E402
from app.services.research import SourceVideo  # noqa: E402


def _video(title: str, *, views: int, age_days: int) -> SourceVideo:
    published = datetime.now(timezone.utc) - timedelta(days=age_days)
    return SourceVideo(
        youtube_video_id=f"id_{abs(hash(title)) % 100000}",
        youtube_channel_id="chan",
        title=title,
        channel_title="Some Channel",
        description="",
        published_at=published,
        duration="PT8M",
        view_count=views,
        like_count=None,
        comment_count=None,
        thumbnail_url=None,
    )


def test_scoring_is_deterministic() -> None:
    topics = ["AI receptionist workflow for contractors", "Generic productivity tips"]
    a = score_ideas(niche_lane="local business ai automation", query="ai receptionist", candidate_topics=topics)
    b = score_ideas(niche_lane="local business ai automation", query="ai receptionist", candidate_topics=topics)
    assert [i.topic for i in a] == [i.topic for i in b]
    assert [i.demand_score for i in a] == [i.demand_score for i in b]


def test_no_live_sources_uses_keyword_heuristic() -> None:
    topics = [
        "AI receptionist workflow for contractors",
        "Completely unrelated knitting tutorial",
    ]
    ranked = score_ideas(
        niche_lane="local business ai automation",
        query="ai receptionist contractor",
        candidate_topics=topics,
    )
    assert all(idea.signals["live_signal"] is False for idea in ranked)
    # The on-topic idea should outrank the unrelated one.
    assert ranked[0].topic == "AI receptionist workflow for contractors"


def test_live_view_velocity_boosts_demand() -> None:
    topics = ["AI receptionist for roofing companies"]
    hot = [_video("AI receptionist for roofing companies blew up", views=2_000_000, age_days=5)]
    cold = [_video("AI receptionist for roofing companies blew up", views=50, age_days=900)]
    hot_score = score_ideas(niche_lane="local ai", query="ai receptionist", candidate_topics=topics, source_videos=hot)[0]
    cold_score = score_ideas(niche_lane="local ai", query="ai receptionist", candidate_topics=topics, source_videos=cold)[0]
    assert hot_score.demand_score > cold_score.demand_score
    assert hot_score.signals["live_signal"] is True


def test_saturation_flagged_high_for_duplicate_titles() -> None:
    topics = ["AI receptionist for roofing companies"]
    dupes = [_video("AI receptionist for roofing companies", views=1000, age_days=30) for _ in range(5)]
    ranked = score_ideas(niche_lane="local ai", query="ai receptionist", candidate_topics=topics, source_videos=dupes)
    assert ranked[0].saturation_risk == "high"


def test_suggest_candidates_are_unique_and_bounded() -> None:
    topics = suggest_candidate_topics(niche_lane="faceless youtube creator automation", query="ai automation", limit=6)
    assert 1 <= len(topics) <= 6
    assert len(topics) == len(set(topics))


def test_rank_ideas_endpoint_returns_ranking_without_db_writes() -> None:
    client = TestClient(app)
    resp = client.post(
        "/research/rank-ideas",
        json={
            "niche_lane": "local business ai automation",
            "query": "ai receptionist contractor",
            "use_live_sources": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["live_signal_used"] is False
    assert len(body["ideas"]) >= 1
    first = body["ideas"][0]
    assert {"topic", "demand_score", "saturation_risk", "rationale", "signals"} <= set(first)
    assert 1 <= first["demand_score"] <= 100
