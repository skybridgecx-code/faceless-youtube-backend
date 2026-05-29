from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_retention.db")
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_retention_test_"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AssetType, ContentAsset  # noqa: E402
from app.services.retention import analyze_script, retention_prompt_guidance  # noqa: E402

STRONG_LONG_SCRIPT = """## 0:00 Hook
Most contractors lose money on the phone, and you probably do too. But here's
the problem nobody tells you about.
## 0:08 Setup
Stick around, because by the end I'll show you the exact AI receptionist
workflow that books jobs while you sleep.
## 0:25 Step 1
First, set up the dashboard so every missed call gets a text-back in 30 seconds.
## 1:10 Step 2
Then we connect the system to your calendar — this is the part that 95% of
people skip.
## 2:30 Payoff
Here's the checklist. Now subscribe and watch the next video to wire up billing.
"""

WEAK_SCRIPT = "Today we talk about some stuff. It is fine. The thing is okay. Thanks."


def _reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_strong_script_scores_higher_than_weak() -> None:
    strong = analyze_script(STRONG_LONG_SCRIPT, content_type="long")
    weak = analyze_script(WEAK_SCRIPT, content_type="long")
    assert strong.score > weak.score
    assert strong.score >= 70


def test_analysis_is_deterministic() -> None:
    a = analyze_script(STRONG_LONG_SCRIPT, content_type="long")
    b = analyze_script(STRONG_LONG_SCRIPT, content_type="long")
    assert a.score == b.score
    assert [c.passed for c in a.checks] == [c.passed for c in b.checks]


def test_prompt_guidance_differs_by_content_type() -> None:
    long_guidance = retention_prompt_guidance("long")
    short_guidance = retention_prompt_guidance("short")
    assert long_guidance != short_guidance
    assert "short" in short_guidance.lower()


def test_endpoint_returns_empty_report_when_no_script() -> None:
    _reset_db()
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Retention Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "No Script Yet"}).json()
    resp = client.get(f"/videos/{video['id']}/retention-analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["script_present"] is False
    assert body["video_id"] == video["id"]
    assert {"score", "grade", "summary", "checks"} <= set(body)


def test_endpoint_scores_existing_script() -> None:
    _reset_db()
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Retention Channel 2"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Has A Script"}).json()
    video_id = video["id"]

    db = SessionLocal()
    try:
        db.add(ContentAsset(video_id=video_id, asset_type=AssetType.script, body=STRONG_LONG_SCRIPT))
        db.commit()
    finally:
        db.close()

    resp = client.get(f"/videos/{video_id}/retention-analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["script_present"] is True
    assert body["score"] >= 70
    assert len(body["checks"]) == 6
