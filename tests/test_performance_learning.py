from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_perf_learning.db")
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_perf_learn_test_"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Channel, ContentType, Video, VideoPerformanceMetric  # noqa: E402
from app.services.idea_demand import score_ideas  # noqa: E402
from app.services.performance_feedback import build_performance_learning_signal  # noqa: E402
from tests.db_helpers import reset_migrated_test_database  # noqa: E402


def _reset_db() -> None:
    reset_migrated_test_database()


def _seed_channel_with_metrics() -> int:
    """Create a channel: a strong 'receptionist' video and a weak 'knitting' video."""
    db = SessionLocal()
    try:
        channel = Channel(
            name="Perf Learning Channel",
            niche="local business ai automation",
            audience="Local business owners",
            brand_voice="Practical, no hype",
            visual_style="Clean dashboard b-roll",
        )
        db.add(channel)
        db.commit()
        db.refresh(channel)

        strong = Video(
            channel_id=channel.id,
            title="AI receptionist workflow for roofing contractors",
            content_type=ContentType.long,
            pillar="AI call handling",
        )
        weak = Video(
            channel_id=channel.id,
            title="Relaxing knitting tutorial for beginners",
            content_type=ContentType.long,
            pillar="AI call handling",
        )
        db.add_all([strong, weak])
        db.commit()
        db.refresh(strong)
        db.refresh(weak)

        # Strong: high CTR + high retention.
        db.add(
            VideoPerformanceMetric(
                video_id=strong.id,
                impressions=10_000,
                views=4_000,
                clicks=800,  # 8% CTR -> strong
                average_percentage_viewed=55.0,  # strong retention
                watch_time_minutes=2200.0,
            )
        )
        # Weak: low CTR + low retention.
        db.add(
            VideoPerformanceMetric(
                video_id=weak.id,
                impressions=10_000,
                views=200,
                clicks=80,  # 0.8% CTR -> weak
                average_percentage_viewed=12.0,  # weak retention
                watch_time_minutes=40.0,
            )
        )
        db.commit()
        return channel.id
    finally:
        db.close()


def test_no_metrics_yields_no_signal() -> None:
    _reset_db()
    db = SessionLocal()
    try:
        channel = Channel(
            name="Empty Channel",
            niche="local business ai automation",
            audience="Local business owners",
            brand_voice="Practical, no hype",
            visual_style="Clean dashboard b-roll",
        )
        db.add(channel)
        db.commit()
        db.refresh(channel)
        signal = build_performance_learning_signal(db, channel_id=channel.id)
        assert signal.has_signal is False
        assert signal.sample_size == 0
        assert signal.proven_keywords == []
        assert signal.avoid_keywords == []
    finally:
        db.close()


def test_signal_extracts_proven_and_avoid_keywords() -> None:
    _reset_db()
    channel_id = _seed_channel_with_metrics()
    db = SessionLocal()
    try:
        signal = build_performance_learning_signal(db, channel_id=channel_id)
    finally:
        db.close()

    assert signal.has_signal is True
    assert signal.strong_count == 1
    assert signal.weak_count == 1
    assert "receptionist" in signal.proven_keywords
    assert "knitting" in signal.avoid_keywords
    # A proven keyword must never leak into the avoid list.
    assert not (set(signal.proven_keywords) & set(signal.avoid_keywords))


def test_proven_keywords_lift_ranking_in_score_ideas() -> None:
    topics = ["AI receptionist workflow teardown", "Cozy knitting patterns roundup"]
    baseline = score_ideas(niche_lane="local ai", query="workflow", candidate_topics=topics)
    biased = score_ideas(
        niche_lane="local ai",
        query="workflow",
        candidate_topics=topics,
        proven_keywords={"receptionist"},
        avoid_keywords={"knitting"},
    )
    base_map = {i.topic: i.demand_score for i in baseline}
    biased_map = {i.topic: i.demand_score for i in biased}
    # Proven angle should not lose ground; weak angle should not gain.
    assert biased_map["AI receptionist workflow teardown"] >= base_map["AI receptionist workflow teardown"]
    assert biased_map["Cozy knitting patterns roundup"] <= base_map["Cozy knitting patterns roundup"]
    # Proven outranks weak after biasing.
    assert biased[0].topic == "AI receptionist workflow teardown"


def test_score_ideas_default_unchanged_without_learning() -> None:
    topics = ["AI receptionist workflow teardown", "Cozy knitting patterns roundup"]
    a = score_ideas(niche_lane="local ai", query="workflow", candidate_topics=topics)
    b = score_ideas(
        niche_lane="local ai",
        query="workflow",
        candidate_topics=topics,
        proven_keywords=set(),
        avoid_keywords=set(),
    )
    assert [i.demand_score for i in a] == [i.demand_score for i in b]


def test_rank_ideas_endpoint_uses_channel_performance() -> None:
    _reset_db()
    channel_id = _seed_channel_with_metrics()
    client = TestClient(app)
    resp = client.post(
        "/research/rank-ideas",
        json={
            "niche_lane": "local business ai automation",
            "query": "ai receptionist",
            "use_live_sources": False,
            "channel_id": channel_id,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["performance_learning"] is not None
    learning = body["performance_learning"]
    assert learning["has_signal"] is True
    assert "receptionist" in learning["proven_keywords"]


def test_rank_ideas_endpoint_without_channel_has_no_learning() -> None:
    _reset_db()
    client = TestClient(app)
    resp = client.post(
        "/research/rank-ideas",
        json={
            "niche_lane": "local business ai automation",
            "query": "ai receptionist",
            "use_live_sources": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["performance_learning"] is None
