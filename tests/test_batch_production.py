from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_batch_production.db")
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_batch_test_"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AssetType, ContentAsset, Video, VideoStatus  # noqa: E402
from app.services.batch_production import produce_content_batch  # noqa: E402
from tests.db_helpers import reset_migrated_test_database  # noqa: E402


def _reset_db() -> None:
    reset_migrated_test_database()


def _make_channel(client: TestClient, name: str) -> int:
    return client.post("/channels", json={"name": name}).json()["id"]


def test_produce_batch_creates_review_ready_videos_with_assets() -> None:
    _reset_db()
    client = TestClient(app)
    channel_id = _make_channel(client, "Batch Channel")

    resp = client.post(
        "/research/produce-batch",
        json={
            "channel_id": channel_id,
            "niche_lane": "local business ai automation",
            "query": "ai receptionist for contractors",
            "count": 3,
            "use_live_sources": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["produced"] >= 1
    assert body["live_signal_used"] is False
    assert len(body["videos"]) == body["produced"]
    first = body["videos"][0]
    assert {"video_id", "title", "demand_score", "saturation_risk", "asset_count"} <= set(first)
    assert first["asset_count"] >= 1


def test_produced_videos_are_unapproved_and_need_review() -> None:
    _reset_db()
    client = TestClient(app)
    channel_id = _make_channel(client, "Gate Channel")
    resp = client.post(
        "/research/produce-batch",
        json={"channel_id": channel_id, "niche_lane": "local ai", "query": "ai receptionist", "count": 2, "use_live_sources": False},
    )
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        videos = list(db.query(Video).filter(Video.channel_id == channel_id))
        assert len(videos) >= 1
        for video in videos:
            assert video.approved is False
            assert video.status == VideoStatus.needs_review
            # Description asset should exist (the monetization-bearing asset).
            descs = [a for a in video.assets if a.asset_type == AssetType.description]
            assert descs, f"video {video.id} missing description"
    finally:
        db.close()


def test_produce_batch_unknown_channel_returns_404() -> None:
    _reset_db()
    client = TestClient(app)
    resp = client.post(
        "/research/produce-batch",
        json={"channel_id": 999999, "niche_lane": "x", "query": "y", "count": 1, "use_live_sources": False},
    )
    assert resp.status_code == 404


def test_service_raises_for_missing_channel() -> None:
    _reset_db()
    db = SessionLocal()
    try:
        try:
            produce_content_batch(db, channel_id=424242, niche_lane="x", query="y", count=1)
            assert False, "expected ValueError"
        except ValueError:
            pass
    finally:
        db.close()


def test_batch_count_is_bounded() -> None:
    _reset_db()
    client = TestClient(app)
    channel_id = _make_channel(client, "Bounded Channel")
    resp = client.post(
        "/research/produce-batch",
        json={"channel_id": channel_id, "niche_lane": "local ai", "query": "ai receptionist", "count": 25, "use_live_sources": False},
    )
    assert resp.status_code == 200
    # Heuristic candidate pool may yield fewer than 25 unique topics; never more.
    assert resp.json()["produced"] <= 25
