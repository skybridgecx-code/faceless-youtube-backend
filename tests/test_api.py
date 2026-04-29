import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///./test_content_factory.db"
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="yt_factory_test_")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


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
