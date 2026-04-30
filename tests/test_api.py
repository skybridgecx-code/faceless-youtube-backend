import os
import shutil
import tempfile
import urllib.error
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = "sqlite:///./test_content_factory.db"
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="yt_factory_test_")

from fastapi.testclient import TestClient  # noqa: E402

import app.routers.videos as videos_router  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


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
    assert created["assigned_agent"] == "Opportunity Research Agent (Local Deterministic)"

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

    promote_response = client.post(f"/opportunities/{created['id']}/promote-to-video")
    assert promote_response.status_code == 200
    promoted_video = promote_response.json()
    assert promoted_video["title"] == created["recommended_title"]
    assert promoted_video["approved"] is False
    assert promoted_video["status"] == "idea"
    assert promoted_video["preview_reviewed"] is False

    updated_opp = client.get("/opportunities").json()[0]
    assert updated_opp["promoted_video_id"] == promoted_video["id"]

    audit_response = client.get("/audit?limit=100")
    assert audit_response.status_code == 200
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "opportunity_created" in event_types
    assert "opportunity_scored" in event_types
    assert "opportunity_promoted" in event_types
