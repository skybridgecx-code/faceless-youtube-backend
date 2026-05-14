from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test_vgl.db"
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_vgl_test_"))

import app.main as main_module  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import VisualGeneratedAsset  # noqa: E402
from app.security import InMemoryRateLimiter  # noqa: E402


@pytest.fixture(autouse=True)
def reset_test_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    main_module.rate_limiter = InMemoryRateLimiter()
    output_dir = get_settings().output_path.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_preview_file(video_id: int) -> Path:
    preview_path = get_settings().output_path / "previews" / str(video_id) / "draft.mp4"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    return preview_path


def _write_final_voiceover(video_id: int, provider: str = "openai") -> None:
    voiceover_dir = get_settings().output_path / "final_voiceovers" / f"video_{video_id}"
    voiceover_dir.mkdir(parents=True, exist_ok=True)
    (voiceover_dir / "voiceover.mp3").write_bytes(b"\xff\xfb\x90\x00" * 16)
    meta = {"provider": provider, "voice": "onyx", "model": "gpt-4o-mini-tts", "status": "complete"}
    (voiceover_dir / "voiceover_meta.json").write_text(json.dumps(meta))


def _write_clean_description(video_id: int) -> None:
    from app.models import ContentAsset, AssetType as AT
    db = SessionLocal()
    try:
        asset = ContentAsset(
            video_id=video_id,
            asset_type=AT.description,
            body="A production-ready video about AI automation. Subscribe for more.",
        )
        db.add(asset)
        db.commit()
    finally:
        db.close()


def _setup_video_with_plan(client: TestClient, title: str) -> dict:
    """Run video through to having a queued visual generation job."""
    channel_id = client.post("/channels", json={"name": f"VGL Channel {title}"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": title}).json()
    video_id = video["id"]

    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "ok"}).status_code == 200

    plan_resp = client.post(f"/visual-assets/from-video/{video_id}")
    assert plan_resp.status_code == 200
    plan_id = plan_resp.json()["id"]

    queue_resp = client.post(f"/visual-generation/plans/{plan_id}/queue", json={"provider": "local"})
    assert queue_resp.status_code == 200
    jobs = queue_resp.json()["jobs"]
    assert len(jobs) > 0

    return {"video_id": video_id, "plan_id": plan_id, "jobs": jobs, "job_id": jobs[0]["id"]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_queued_job_becomes_imported() -> None:
    """run-local changes job status from queued to imported."""
    client = TestClient(app)
    ctx = _setup_video_with_plan(client, "Local Worker Job Status")
    job_id = ctx["job_id"]

    job_before = client.get(f"/visual-generation/jobs/{job_id}").json()
    assert job_before["status"] == "queued"

    resp = client.post(f"/visual-generation/jobs/{job_id}/run-local")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "imported"
    assert body["job_id"] == job_id

    job_after = client.get(f"/visual-generation/jobs/{job_id}").json()
    assert job_after["status"] == "imported"


def test_local_file_exists_on_disk() -> None:
    """run-local creates a real file under out/visual_assets/video_{id}/."""
    client = TestClient(app)
    ctx = _setup_video_with_plan(client, "Local Worker File")
    job_id = ctx["job_id"]
    video_id = ctx["video_id"]

    resp = client.post(f"/visual-generation/jobs/{job_id}/run-local")
    assert resp.status_code == 200
    body = resp.json()
    assert body["file_path"] is not None

    file_path = Path(body["file_path"])
    assert file_path.exists()
    assert file_path.stat().st_size > 0
    assert "visual_assets" in str(file_path)
    assert f"video_{video_id}" in str(file_path)


def test_generated_asset_row_is_created() -> None:
    """run-local creates a VisualGeneratedAsset row linked to the job."""
    client = TestClient(app)
    ctx = _setup_video_with_plan(client, "Local Worker Asset Row")
    job_id = ctx["job_id"]

    resp = client.post(f"/visual-generation/jobs/{job_id}/run-local")
    assert resp.status_code == 200
    asset_id = resp.json()["asset_id"]
    assert asset_id is not None

    db = SessionLocal()
    try:
        asset = db.get(VisualGeneratedAsset, asset_id)
        assert asset is not None
        assert asset.file_exists is True
        assert asset.generation_job_id == job_id
    finally:
        db.close()

    assets_resp = client.get("/visual-generation/assets", params={"job_id": job_id})
    assert assets_resp.status_code == 200
    assets = assets_resp.json()
    assert len(assets) == 1
    assert assets[0]["id"] == asset_id


def test_asset_review_status_remains_pending() -> None:
    """run-local never auto-approves the generated asset — it stays pending."""
    client = TestClient(app)
    ctx = _setup_video_with_plan(client, "Local Worker Pending Review")
    job_id = ctx["job_id"]
    video_id = ctx["video_id"]

    resp = client.post(f"/visual-generation/jobs/{job_id}/run-local")
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "pending"
    asset_id = body["asset_id"]

    queue_resp = client.get("/visual-generation/assets/review-queue", params={"video_id": video_id, "review_status": "pending"})
    assert queue_resp.status_code == 200
    queue = queue_resp.json()
    assert any(item["id"] == asset_id and item["review_status"] == "pending" for item in queue)


def test_final_production_blocks_until_visual_asset_manually_approved() -> None:
    """Final production remains blocked after run-local until the asset is manually approved."""
    client = TestClient(app)
    ctx = _setup_video_with_plan(client, "Local Worker Final Production Block")
    job_id = ctx["job_id"]
    video_id = ctx["video_id"]
    plan_id = ctx["plan_id"]

    _write_preview_file(video_id)
    assert client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{video_id}/package").status_code == 200
    assert client.post(f"/publish/{video_id}/prepare-youtube-payload").status_code == 200
    _write_final_voiceover(video_id, provider="openai")
    _write_clean_description(video_id)

    run_resp = client.post(f"/visual-generation/jobs/{job_id}/run-local")
    assert run_resp.status_code == 200
    asset_id = run_resp.json()["asset_id"]

    # Before approval: final production is still blocked on unapproved visuals
    status = client.get(f"/videos/{video_id}/final-production/status").json()
    assert status["final_visuals_ready"] is False
    assert status["production_ready"] is False
    assert any("visual" in b.lower() for b in status["blockers"])

    # After manual approval: visuals unblock
    approve_resp = client.post(f"/visual-generation/assets/{asset_id}/approve")
    assert approve_resp.status_code == 200

    status_after = client.get(f"/videos/{video_id}/final-production/status").json()
    assert status_after["final_visuals_ready"] is True


def test_run_local_on_already_imported_job_returns_warning() -> None:
    """Running run-local a second time on an already-imported job returns a warning, not an error."""
    client = TestClient(app)
    ctx = _setup_video_with_plan(client, "Local Worker Idempotent")
    job_id = ctx["job_id"]

    first_resp = client.post(f"/visual-generation/jobs/{job_id}/run-local")
    assert first_resp.status_code == 200
    assert first_resp.json()["status"] == "imported"

    second_resp = client.post(f"/visual-generation/jobs/{job_id}/run-local")
    assert second_resp.status_code == 200
    body = second_resp.json()
    assert body["warning"] is not None
    assert "already" in body["warning"].lower() or "skipped" in body["warning"].lower()
