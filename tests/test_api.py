import os
import shutil
import tempfile
import urllib.error
import json
from pathlib import Path

import pytest
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test_content_factory.db"
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="yt_factory_test_")

from fastapi.testclient import TestClient  # noqa: E402

import app.routers.videos as videos_router  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent, ContentAgent, VideoOpportunity  # noqa: E402
from app.services.agents import seed_default_agents_if_empty  # noqa: E402


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def _write_real_preview_file(video_id: int) -> Path:
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    preview_path = output_dir / "previews" / str(video_id) / "draft.mp4"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    )
    return preview_path


@pytest.mark.usefixtures("monkeypatch")
def test_render_draft_preview_creates_non_empty_mp4_when_ffmpeg_available(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Renderer Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post("/videos", json={"channel_id": channel_id, "title": "Renderer Demo"})
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200
    patch_response = client.patch(
        f"/videos/{video_id}/assets/script",
        json={"body": "This is a safe draft script for local preview review only."},
    )
    assert patch_response.status_code == 200
    review_response = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"})
    assert review_response.status_code == 200
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PREVIEW_TTS_PROVIDER", "auto")

    def fake_which(binary: str) -> str | None:
        if binary == "ffmpeg":
            return "/usr/bin/ffmpeg"
        if binary == "qlmanage":
            return "/usr/bin/qlmanage"
        if binary == "say":
            return None
        return shutil.which(binary)

    class FakeCompletedProcess:
        def __init__(self, returncode: int = 0) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(command: list[str], capture_output: bool, text: bool) -> FakeCompletedProcess:
        if command and command[0].endswith("qlmanage"):
            output_dir = Path(command[command.index("-o") + 1])
            html_path = Path(command[-1])
            png_path = output_dir / f"{html_path.name}.png"
            png_path.parent.mkdir(parents=True, exist_ok=True)
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        else:
            output_path = Path(command[-1])
            if output_path.suffix == ".mp4":
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(
                    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat"
                )
        return FakeCompletedProcess(0)

    monkeypatch.setattr(videos_router.shutil, "which", fake_which)
    monkeypatch.setattr(videos_router.subprocess, "run", fake_run)

    render_response = client.post(f"/videos/{video_id}/preview/render-draft")
    assert render_response.status_code == 200
    render_payload = render_response.json()
    assert render_payload["preview_exists"] is True
    assert render_payload["audio_generated"] is False
    assert "tts_provider" in render_payload
    assert "tts_voice" in render_payload
    assert "tts_model" in render_payload

    preview_path = Path(render_payload["preview_path"])
    assert preview_path.exists()
    assert preview_path.stat().st_size > 0


@pytest.mark.usefixtures("monkeypatch")
def test_preview_status_does_not_expose_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "No Leak Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]
    video_response = client.post("/videos", json={"channel_id": channel_id, "title": "No Leak Demo"})
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "ok"})

    monkeypatch.setenv("PREVIEW_TTS_PROVIDER", "auto")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "LEAK_TEST_ELEVEN_KEY")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice_x")
    monkeypatch.setenv("OPENAI_API_KEY", "LEAK_TEST_OPENAI_KEY")
    monkeypatch.setenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    monkeypatch.setenv("OPENAI_TTS_VOICE", "marin")

    def fake_which(binary: str) -> str | None:
        if binary == "ffmpeg":
            return "/usr/bin/ffmpeg"
        if binary == "qlmanage":
            return "/usr/bin/qlmanage"
        if binary == "say":
            return None
        return shutil.which(binary)

    class FakeCompletedProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def fake_run(command: list[str], capture_output: bool, text: bool) -> FakeCompletedProcess:
        if command and command[0].endswith("qlmanage"):
            output_dir = Path(command[command.index("-o") + 1])
            html_path = Path(command[-1])
            (output_dir / f"{html_path.name}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        else:
            output_path = Path(command[-1])
            if output_path.suffix == ".mp4":
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        return FakeCompletedProcess()

    def fake_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        raise urllib.error.URLError("network disabled for test")

    monkeypatch.setattr(videos_router.shutil, "which", fake_which)
    monkeypatch.setattr(videos_router.subprocess, "run", fake_run)
    monkeypatch.setattr(videos_router.urllib.request, "urlopen", fake_urlopen)

    render_response = client.post(f"/videos/{video_id}/preview/render-draft")
    assert render_response.status_code == 200
    status_response = client.get(f"/videos/{video_id}/preview/status")
    assert status_response.status_code == 200
    body = status_response.text
    assert "LEAK_TEST_ELEVEN_KEY" not in body
    assert "LEAK_TEST_OPENAI_KEY" not in body


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_content_workflow() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Test Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post(
        "/videos",
        json={
            "channel_id": channel_id,
            "title": "I Built an AI Receptionist for a Roofing Company",
            "thumbnail_text": "AI ROOFING RECEPTIONIST",
        },
    )
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200
    assert len(generated_response.json()) >= 5

    bad_script_response = client.patch(
        f"/videos/{video_id}/assets/script",
        json={"body": "Here is an income guarantee that you will make 10k a month."},
    )
    assert bad_script_response.status_code == 200

    compliance_response = client.post(f"/videos/{video_id}/compliance/run")
    assert compliance_response.status_code == 200
    assert compliance_response.json()["overall_status"] == "blocked"

    blocked_package = client.post(f"/videos/{video_id}/package")
    assert blocked_package.status_code == 409

    review_response_fail = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "Looks safe"})
    assert review_response_fail.status_code == 400
    assert "compliance" in review_response_fail.json()["detail"].lower()

    client.patch(f"/videos/{video_id}/assets/script", json={"body": "A normal safe script about AI."})
    compliance_response = client.post(f"/videos/{video_id}/compliance/run")
    assert compliance_response.status_code == 200
    assert compliance_response.json()["overall_status"] != "blocked"

    review_response = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "Looks safe"})
    assert review_response.status_code == 200
    assert review_response.json()["passed"] is True

    readiness_before_preview = client.get(f"/videos/{video_id}/readiness")
    assert readiness_before_preview.status_code == 200
    assert readiness_before_preview.json()["preview_reviewed"] is False

    blocked_package_without_preview = client.post(f"/videos/{video_id}/package")
    assert blocked_package_without_preview.status_code == 409
    assert "preview" in blocked_package_without_preview.json()["detail"].lower()

    blocked_payload_without_preview_review = client.post(f"/publish/{video_id}/prepare-youtube-payload")
    assert blocked_payload_without_preview_review.status_code == 409
    assert "preview" in blocked_payload_without_preview_review.json()["detail"].lower()

    _write_real_preview_file(video_id)

    preview_status = client.get(f"/videos/{video_id}/preview/status")
    assert preview_status.status_code == 200
    assert preview_status.json()["preview_exists"] is True
    assert preview_status.json()["preview_reviewed"] is False

    mark_preview_reviewed = client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True})
    assert mark_preview_reviewed.status_code == 200
    assert mark_preview_reviewed.json()["preview_reviewed"] is True

    package_response = client.post(f"/videos/{video_id}/package")
    assert package_response.status_code == 200
    assert "package_dir" in package_response.json()

    payload_response = client.post(f"/publish/{video_id}/prepare-youtube-payload")
    assert payload_response.status_code == 200
    assert payload_response.json()["privacy_status"] == "private"


def test_pipeline_and_batch() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Test Channel 2"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    batch_response = client.post(
        "/videos/batch",
        json={
            "channel_id": channel_id,
            "videos": [
                {"title": "Video 1", "thumbnail_text": "Thumb 1", "pillar": "pillar1", "target_view": "tv1"},
                {"title": "Video 2", "thumbnail_text": "Thumb 2", "pillar": "pillar2", "target_view": "tv2"},
            ],
        },
    )
    assert batch_response.status_code == 200
    videos = batch_response.json()
    assert len(videos) == 2

    invalid_batch = client.post(
        "/videos/batch",
        json={"channel_id": channel_id, "videos": [{"title": "Valid"}, {"title": "   "}]},
    )
    assert invalid_batch.status_code == 400

    summary_response = client.get("/pipeline/summary")
    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["status_counts"]["idea"] >= 2

    filter_response = client.get("/videos?search=Video 1")
    assert filter_response.status_code == 200
    filtered_videos = filter_response.json()
    assert len(filtered_videos) == 1
    assert filtered_videos[0]["title"] == "Video 1"

    filter_status_response = client.get("/videos?status=idea")
    assert filter_status_response.status_code == 200
    assert len(filter_status_response.json()) >= 2


def test_preview_route_and_review_state() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Preview Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post("/videos", json={"channel_id": channel_id, "title": "Preview Demo"})
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    missing_preview = client.get(f"/videos/{video_id}/preview")
    assert missing_preview.status_code == 404
    missing_preview_review = client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True})
    assert missing_preview_review.status_code == 404

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200
    review_response = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"})
    assert review_response.status_code == 200

    _write_real_preview_file(video_id)

    served_preview = client.get(f"/videos/{video_id}/preview")
    assert served_preview.status_code == 200
    assert served_preview.headers["content-type"].startswith("video/mp4")

    preview_review = client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True})
    assert preview_review.status_code == 200
    assert preview_review.json()["preview_reviewed"] is True

    video_after_review = client.get(f"/videos/{video_id}").json()
    assert video_after_review["preview_reviewed"] is True


def test_audit_trail_and_operator_export_are_safe() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Audit Test Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Audit Trail Demo"},
    )
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    audit_response = client.get("/audit")
    assert audit_response.status_code == 200
    assert any(event["event_type"] == "video_created" and event["video_id"] == video_id for event in audit_response.json())

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200

    video_audit_response = client.get(f"/videos/{video_id}/audit")
    assert video_audit_response.status_code == 200
    assert any(event["event_type"] == "assets_generated" for event in video_audit_response.json())

    compliance_response = client.post(f"/videos/{video_id}/compliance/run")
    assert compliance_response.status_code == 200

    video_audit_response = client.get(f"/videos/{video_id}/audit")
    event_types = [event["event_type"] for event in video_audit_response.json()]
    assert "compliance_run" in event_types

    missing_audit_response = client.get("/videos/999999/audit")
    assert missing_audit_response.status_code == 404

    long_body = "safe local operator script " * 60
    patch_response = client.patch(f"/videos/{video_id}/assets/script", json={"body": long_body})
    assert patch_response.status_code == 200

    export_response = client.get(f"/videos/{video_id}/operator-export")
    assert export_response.status_code == 200
    export_payload = export_response.json()
    assert export_payload["video"]["id"] == video_id
    assert "assets" in export_payload
    assert all("body" not in asset for asset in export_payload["assets"])
    assert any(asset["asset_type"] == "script" and asset["body_length"] == len(long_body) for asset in export_payload["assets"])
    assert long_body not in export_response.text
    assert "audit_events" in export_payload
    assert "youtube_payload_readiness" in export_payload


def test_opportunity_routes_and_promotion_workflow() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Opportunity Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    create_response = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Best AI tools for small business owners",
            "niche_lane": "Local business AI operations",
            "audience": "Small business owners",
            "monetization_path": "Affiliate + consulting audit",
            "notes": "Educational operator workflow only",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["topic"] == "Best AI tools for small business owners"
    assert created["score"]["total_score"] >= 8
    assert created["assigned_agent"]
    assert created["assigned_agent_id"] is not None

    list_response = client.get("/opportunities")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]

    top_response = client.get("/opportunities/top")
    assert top_response.status_code == 200
    top_items = top_response.json()
    assert len(top_items) == 1
    assert top_items[0]["id"] == created["id"]

    score_response = client.post(f"/opportunities/{created['id']}/score")
    assert score_response.status_code == 200
    rescored = score_response.json()
    assert rescored["score"]["total_score"] == created["score"]["total_score"]

    blocked_promote_response = client.post(f"/opportunities/{created['id']}/promote-to-video")
    assert blocked_promote_response.status_code == 400
    assert blocked_promote_response.json()["detail"] == "Opportunity must be approved for video before promotion."

    review_response = client.patch(
        f"/opportunities/{created['id']}/review",
        json={
            "review_status": "approved_for_video",
            "operator_notes": "Strong fit for local business audience.",
            "decision_summary": "Approve for production queue.",
        },
    )
    assert review_response.status_code == 200
    reviewed = review_response.json()
    assert reviewed["review_status"] == "approved_for_video"
    assert reviewed["reviewed_at"] is not None

    promote_response = client.post(f"/opportunities/{created['id']}/promote-to-video")
    assert promote_response.status_code == 200
    promoted_video = promote_response.json()
    assert promoted_video["title"] == created["recommended_title"]
    assert promoted_video["approved"] is False
    assert promoted_video["status"] == "idea"
    assert promoted_video["preview_reviewed"] is False
    assert promoted_video["assigned_agent_id"] == created["assigned_agent_id"]

    updated_opp = client.get("/opportunities").json()[0]
    assert updated_opp["promoted_video_id"] == promoted_video["id"]

    audit_response = client.get("/audit?limit=100")
    assert audit_response.status_code == 200
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "opportunity_created" in event_types
    assert "opportunity_scored" in event_types
    assert "opportunity_review_updated" in event_types
    assert "opportunity_promoted" in event_types


def test_opportunity_review_status_validation_and_rejection() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Review Validation Channel"}).json()["id"]
    opportunity_id = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Local business AI automation audit",
            "niche_lane": "Local AI consulting",
            "audience": "Service business owners",
            "monetization_path": "Audit service",
        },
    ).json()["id"]

    invalid = client.patch(
        f"/opportunities/{opportunity_id}/review",
        json={"review_status": "not_a_status"},
    )
    assert invalid.status_code == 422

    rejected = client.patch(
        f"/opportunities/{opportunity_id}/review",
        json={
            "review_status": "rejected",
            "operator_notes": "Too generic compared with current queue.",
            "rejection_reason": "Low differentiation for this week.",
            "decision_summary": "Revisit with sharper angle and examples.",
        },
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["review_status"] == "rejected"
    assert body["rejection_reason"] == "Low differentiation for this week."
    assert body["reviewed_at"] is not None


def test_opportunity_review_queue_ordering_and_filtering() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Queue Channel"}).json()["id"]

    created_ids: list[int] = []
    for topic in [
        "Best AI tools for small business owners",
        "How to automate customer calls with AI",
        "AI side hustles that are actually useful",
        "Faceless YouTube automation workflow",
    ]:
        response = client.post(
            "/opportunities",
            json={
                "channel_id": channel_id,
                "topic": topic,
                "niche_lane": "Operator lane",
                "audience": "Business operators",
                "monetization_path": "Service + affiliate",
            },
        )
        assert response.status_code == 200
        created_ids.append(response.json()["id"])

    assert len(created_ids) == 4
    first, second, third, fourth = created_ids

    assert client.patch(f"/opportunities/{first}/review", json={"review_status": "unreviewed"}).status_code == 200
    assert client.patch(f"/opportunities/{second}/review", json={"review_status": "shortlisted"}).status_code == 200
    assert client.patch(f"/opportunities/{third}/review", json={"review_status": "needs_more_research"}).status_code == 200
    assert client.patch(f"/opportunities/{fourth}/review", json={"review_status": "rejected"}).status_code == 200

    queue_response = client.get("/opportunities/review-queue")
    assert queue_response.status_code == 200
    queue = queue_response.json()
    queue_by_id = {item["id"]: idx for idx, item in enumerate(queue)}
    assert queue_by_id[second] < queue_by_id[third] < queue_by_id[first] < queue_by_id[fourth]

    filter_response = client.get("/opportunities/review-queue?review_status=shortlisted")
    assert filter_response.status_code == 200
    filtered = filter_response.json()
    assert len(filtered) == 1
    assert filtered[0]["id"] == second


def test_executive_producer_run_empty_state_without_reviewed_opportunities() -> None:
    client = TestClient(app)

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    data = run_response.json()
    assert data["selected_opportunity_id"] is None
    assert data["empty_state_message"] == "No reviewed opportunities are ready. Shortlist or approve an opportunity first."

    current_response = client.get("/executive-producer/recommendation")
    assert current_response.status_code == 200
    assert current_response.json()["empty_state_message"] == data["empty_state_message"]


def test_executive_producer_prefers_approved_for_video_over_shortlisted() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer Priority Channel"}).json()["id"]

    shortlist = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "How to automate customer calls with AI",
            "niche_lane": "Call operations",
            "audience": "Local service business owners",
            "monetization_path": "Service audit",
        },
    ).json()
    approved = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Faceless YouTube automation workflow",
            "niche_lane": "YouTube operations",
            "audience": "Faceless channel operators",
            "monetization_path": "Affiliate + template service",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{shortlist['id']}/review",
        json={"review_status": "shortlisted"},
    ).status_code == 200
    assert client.patch(
        f"/opportunities/{approved['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    result = run_response.json()
    assert result["selected_opportunity_id"] == approved["id"]
    assert result["selected_review_status"] == "approved_for_video"


def test_executive_producer_penalizes_compliance_and_production_difficulty() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer Penalty Channel"}).json()["id"]

    high_risk = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Best AI tools for small business owners",
            "niche_lane": "AI tooling",
            "audience": "Small business owners",
            "monetization_path": "Affiliate stack",
        },
    ).json()
    low_risk = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Local business AI automation audit",
            "niche_lane": "AI consulting",
            "audience": "Local operators",
            "monetization_path": "Service audit",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{high_risk['id']}/review",
        json={"review_status": "shortlisted"},
    ).status_code == 200
    assert client.patch(
        f"/opportunities/{low_risk['id']}/review",
        json={"review_status": "shortlisted"},
    ).status_code == 200

    db = SessionLocal()
    try:
        risky_row = db.get(VideoOpportunity, high_risk["id"])
        safe_row = db.get(VideoOpportunity, low_risk["id"])
        assert risky_row is not None
        assert safe_row is not None
        risky_row.total_score = 34
        risky_row.compliance_risk = 5
        risky_row.production_difficulty = 5
        risky_row.buyer_intent = 2
        risky_row.product_connection = 2

        safe_row.total_score = 30
        safe_row.compliance_risk = 1
        safe_row.production_difficulty = 1
        safe_row.buyer_intent = 5
        safe_row.product_connection = 5
        db.commit()
    finally:
        db.close()

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    result = run_response.json()
    assert result["selected_opportunity_id"] == low_risk["id"]


def test_executive_producer_history_and_audit_event_written() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer History Channel"}).json()["id"]

    opportunity = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI ecommerce product research workflow",
            "niche_lane": "Ecommerce ops",
            "audience": "Ecommerce founders",
            "monetization_path": "Service + affiliate",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{opportunity['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    first = client.post("/executive-producer/recommendation/run")
    second = client.post("/executive-producer/recommendation/run")
    assert first.status_code == 200
    assert second.status_code == 200

    history_response = client.get("/executive-producer/history")
    assert history_response.status_code == 200
    history = history_response.json()
    assert len(history) >= 2
    assert history[0]["id"] != history[1]["id"]

    audit_response = client.get("/audit?limit=100")
    assert audit_response.status_code == 200
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "executive_producer_recommendation_created" in event_types


def test_default_agents_seeded_and_listed() -> None:
    client = TestClient(app)
    channel_response = client.post("/channels", json={"name": "Agent Seed Channel"})
    assert channel_response.status_code == 200

    agents_response = client.get("/agents")
    assert agents_response.status_code == 200
    agents = agents_response.json()
    assert len(agents) >= 7
    names = {agent["name"] for agent in agents}
    assert "AI Tools Agent" in names
    assert "Local Business AI Agent" in names


def test_default_agent_seeding_twice_does_not_create_duplicates() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Agent Idempotent Seed Channel"}).json()["id"]

    db = SessionLocal()
    try:
        seed_default_agents_if_empty(db, channel_id)
        seed_default_agents_if_empty(db, channel_id)

        channel_agents = list(
            db.scalars(
                select(ContentAgent).where(ContentAgent.channel_id == channel_id).order_by(ContentAgent.id.asc())
            )
        )
        assert len(channel_agents) == len(seed_default_agents_if_empty(db, channel_id))
        assert len(channel_agents) == 7

        normalized_pairs = {(
            " ".join(agent.name.lower().split()),
            " ".join(agent.lane.lower().split()),
        ) for agent in channel_agents}
        assert len(normalized_pairs) == len(channel_agents)

        seed_events = list(
            db.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == "agent_created_seed")
                .order_by(AuditEvent.id.asc())
            )
        )
        channel_seed_events = [
            event
            for event in seed_events
            if (json.loads(event.metadata_json) if event.metadata_json else {}).get("channel_id") == channel_id
        ]
        assert len(channel_seed_events) == 7
    finally:
        db.close()


def test_init_db_seed_flow_twice_does_not_create_duplicates() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Agent InitDb Idempotent Channel"}).json()["id"]

    init_db()
    init_db()

    db = SessionLocal()
    try:
        channel_agents = list(
            db.scalars(
                select(ContentAgent).where(ContentAgent.channel_id == channel_id).order_by(ContentAgent.id.asc())
            )
        )
        assert len(channel_agents) == 7

        normalized_pairs = {(
            " ".join(agent.name.lower().split()),
            " ".join(agent.lane.lower().split()),
        ) for agent in channel_agents}
        assert len(normalized_pairs) == len(channel_agents)
    finally:
        db.close()


def test_patch_agent_updates_editable_fields() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Agent Update Channel"}).json()["id"]
    agents = client.get("/agents").json()
    target = next(agent for agent in agents if agent["channel_id"] == channel_id and agent["name"] == "AI Tools Agent")

    patch_response = client.patch(
        f"/agents/{target['id']}",
        json={
            "focus": "Tool comparisons with operator walkthroughs.",
            "monetization_focus": "Affiliate software and templates.",
            "compliance_notes": "No unsupported vendor claims.",
            "production_rules": "Use practical setup demos.",
            "is_active": False,
        },
    )
    assert patch_response.status_code == 200
    body = patch_response.json()
    assert body["focus"] == "Tool comparisons with operator walkthroughs."
    assert body["monetization_focus"] == "Affiliate software and templates."
    assert body["compliance_notes"] == "No unsupported vendor claims."
    assert body["production_rules"] == "Use practical setup demos."
    assert body["is_active"] is False


def test_agent_opportunities_and_videos_routes_and_promotion_carry_agent() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Agent Mapping Channel"}).json()["id"]

    agents = client.get("/agents").json()
    local_agent = next(agent for agent in agents if agent["channel_id"] == channel_id and agent["name"] == "Local Business AI Agent")

    opportunity = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "How to automate customer calls with AI",
            "niche_lane": "local business AI automation",
            "audience": "Service business owners",
            "monetization_path": "SkybridgeCX leads + audits",
        },
    ).json()
    assert opportunity["assigned_agent_id"] == local_agent["id"]

    opp_list_response = client.get(f"/agents/{local_agent['id']}/opportunities")
    assert opp_list_response.status_code == 200
    opp_rows = opp_list_response.json()
    assert any(item["id"] == opportunity["id"] for item in opp_rows)

    assert client.patch(
        f"/opportunities/{opportunity['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    promoted_video = client.post(f"/opportunities/{opportunity['id']}/promote-to-video").json()
    assert promoted_video["assigned_agent_id"] == local_agent["id"]
    assert promoted_video["approved"] is False
    assert promoted_video["preview_reviewed"] is False
    assert promoted_video["status"] == "idea"

    agent_videos_response = client.get(f"/agents/{local_agent['id']}/videos")
    assert agent_videos_response.status_code == 200
    video_rows = agent_videos_response.json()
    assert any(item["id"] == promoted_video["id"] for item in video_rows)


def test_lane_alias_matching_for_ecommerce_and_local_business_agents() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Lane Alias Match Channel"}).json()["id"]

    ecommerce = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI ecommerce product research workflow",
            "niche_lane": "AI ecommerce ops",
            "audience": "ecommerce founders",
            "monetization_path": "affiliate tools + templates",
        },
    ).json()
    local_business = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "How to automate customer calls with AI",
            "niche_lane": "local business AI operations",
            "audience": "local business owners",
            "monetization_path": "SkybridgeCX leads + audits",
        },
    ).json()

    assert ecommerce["assigned_agent"] == "Ecommerce AI Agent"
    assert ecommerce["assigned_agent_id"] is not None
    assert local_business["assigned_agent"] == "Local Business AI Agent"
    assert local_business["assigned_agent_id"] is not None


def test_executive_producer_includes_matched_agent_profile() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer Agent Profile Channel"}).json()["id"]

    opportunity = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Faceless YouTube automation workflow",
            "niche_lane": "faceless YouTube / creator automation",
            "audience": "Creator operators",
            "monetization_path": "Templates + affiliate software",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{opportunity['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["selected_opportunity_id"] == opportunity["id"]
    assert payload["matched_agent_id"] is not None
    assert payload["matched_agent_name"] == "Faceless Creator Agent"
    assert payload["matched_agent_lane"] == "faceless YouTube / creator automation"
    assert payload["matched_agent_focus"]
    assert payload["matched_agent_monetization_focus"]
    assert payload["matched_agent_compliance_notes"]
    assert payload["matched_agent_production_rules"]


def test_executive_producer_alias_lane_includes_matched_agent_profile_fields() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer Alias Agent Profile Channel"}).json()["id"]

    opportunity = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI ecommerce automation for store operations",
            "niche_lane": "ecommerce founders",
            "audience": "ecommerce operators",
            "monetization_path": "Affiliate + consulting audit",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{opportunity['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["selected_opportunity_id"] == opportunity["id"]
    assert payload["matched_agent_name"] == "Ecommerce AI Agent"
    assert payload["matched_agent_lane"] == "ecommerce AI"
    assert payload["matched_agent_focus"]
    assert payload["matched_agent_monetization_focus"]
    assert payload["matched_agent_compliance_notes"]
    assert payload["matched_agent_production_rules"]
