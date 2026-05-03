from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test_content_factory.db"
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_factory_test_"))

import app.main as main_module  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import VisualAssetPlan, VisualGeneratedAsset, VisualScene  # noqa: E402
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
# Test helpers
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


def _write_final_export(video_id: int) -> Path:
    final_path = get_settings().output_path / "final_exports" / f"video_{video_id}" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    return final_path


def _create_and_approve_visual_asset(client: TestClient, video_id: int, plan_id: int) -> int:
    asset_path = get_settings().output_path / "generated" / f"prod_asset_{video_id}.png"
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    db = SessionLocal()
    try:
        first_scene = db.scalar(
            select(VisualScene)
            .where(VisualScene.plan_id == plan_id)
            .order_by(VisualScene.scene_number.asc())
            .limit(1)
        )
        assert first_scene is not None
        asset = VisualGeneratedAsset(
            visual_asset_plan_id=plan_id,
            visual_scene_id=first_scene.id,
            generation_job_id=None,
            asset_type="image",
            file_path=str(asset_path),
            file_exists=True,
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
    finally:
        db.close()

    resp = client.post(f"/visual-generation/assets/{asset_id}/approve")
    assert resp.status_code == 200
    return asset_id


def _write_clean_description(video_id: int) -> None:
    """Insert a clean description asset (no placeholder text) so metadata check passes."""
    from app.models import ContentAsset, AssetType as AT
    db = SessionLocal()
    try:
        asset = ContentAsset(
            video_id=video_id,
            asset_type=AT.description,
            body="A production-ready video about AI automation for local businesses. Subscribe for more.",
        )
        db.add(asset)
        db.commit()
    finally:
        db.close()


def _full_workflow(client: TestClient, title: str) -> dict[str, object]:
    """Run video through workflow up to youtube-payload stage. Returns video dict + plan_id."""
    channel_id = client.post("/channels", json={"name": f"FP Channel {title}"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": title}).json()
    video_id = video["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "ok"}).status_code == 200
    plan_resp = client.post(f"/visual-assets/from-video/{video_id}")
    assert plan_resp.status_code == 200
    plan_id = plan_resp.json()["id"]
    _write_preview_file(video_id)
    assert client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{video_id}/package").status_code == 200
    assert client.post(f"/publish/{video_id}/prepare-youtube-payload").status_code == 200
    return {"video": video, "video_id": video_id, "plan_id": plan_id}


# ---------------------------------------------------------------------------
# Phase 30 tests
# ---------------------------------------------------------------------------

def test_draft_preview_does_not_make_production_ready() -> None:
    """Draft preview alone must never satisfy production_ready."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Draft No Production")
    video_id = ctx["video_id"]

    # Only draft preview exists — no render_meta, no final export, no approved visuals
    status = client.get(f"/videos/{video_id}/final-production/status").json()
    assert status["production_ready"] is False
    assert status["final_export_ready"] is False
    assert status["blockers"]
    final_export_path = get_settings().output_path / "previews" / str(video_id) / "draft.mp4"
    assert status["final_export_path"] != str(final_export_path.resolve()) or status["final_export_path"] is None


def test_draft_preview_does_not_unlock_manual_upload() -> None:
    """Publishing payload ready_for_manual_upload must be False without final export."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Draft No Upload")
    video_id = ctx["video_id"]

    resp = client.post(f"/videos/{video_id}/publishing-payload/generate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready_for_manual_upload"] is False
    assert any("final" in b.lower() or "export" in b.lower() for b in body["blockers"])


def test_missing_final_export_blocks_manual_upload() -> None:
    """Missing final.mp4 must produce the required blocker message."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Missing Export Video")
    video_id = ctx["video_id"]

    _write_final_voiceover(video_id, provider="openai")
    _create_and_approve_visual_asset(client, video_id, ctx["plan_id"])

    resp = client.post(f"/videos/{video_id}/publishing-payload/generate")
    body = resp.json()
    assert body["ready_for_manual_upload"] is False
    assert any("Final video export must be generated before manual upload" in b for b in body["blockers"])


def test_insert_link_blocks_metadata() -> None:
    """[INSERT LINK] in description blocks final metadata readiness."""
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Insert Link Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Insert Link Video"}).json()
    video_id = video["id"]

    # Generate assets with a script containing [INSERT LINK]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200

    # Directly inject a description asset with placeholder text via generate override isn't easy,
    # so test via title containing the pattern
    from app.db import SessionLocal
    from app.models import Video as VideoModel
    db = SessionLocal()
    try:
        v = db.get(VideoModel, video_id)
        v.title = "My Video [INSERT LINK] Here"
        db.commit()
    finally:
        db.close()

    status = client.get(f"/videos/{video_id}/final-production/status").json()
    assert status["final_metadata_ready"] is False
    assert any("placeholder" in b.lower() or "INSERT LINK" in b for b in status["blockers"])


def test_how_to_i_built_blocks_metadata() -> None:
    """'How to I Built' pattern in title blocks final metadata readiness."""
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "HowTo Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "How to I Built This"}).json()
    video_id = video["id"]

    status = client.get(f"/videos/{video_id}/final-production/status").json()
    assert status["final_metadata_ready"] is False
    assert any("editorial cleanup" in b.lower() for b in status["blockers"])


def test_draft_tts_providers_block_voice() -> None:
    """macos/silent/fallback/local TTS providers must block final voice readiness."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Draft Voice Video")
    video_id = ctx["video_id"]

    for provider in ["macos", "silent", "fallback", "local"]:
        _write_final_voiceover(video_id, provider=provider)
        status = client.get(f"/videos/{video_id}/final-production/status").json()
        assert status["final_voice_ready"] is False, f"provider={provider} should block"
        assert any("voiceover" in b.lower() or "production voice" in b.lower() for b in status["blockers"])


def test_unapproved_visuals_block_production() -> None:
    """Pending or rejected visual assets must block final_visuals_ready."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Unapproved Visuals Video")
    video_id = ctx["video_id"]
    plan_id = ctx["plan_id"]

    # Create visual asset but do NOT approve it
    asset_path = get_settings().output_path / "generated" / f"unapproved_{video_id}.png"
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    db = SessionLocal()
    try:
        first_scene = db.scalar(
            select(VisualScene)
            .where(VisualScene.plan_id == plan_id)
            .order_by(VisualScene.scene_number.asc())
            .limit(1)
        )
        assert first_scene is not None
        asset = VisualGeneratedAsset(
            visual_asset_plan_id=plan_id,
            visual_scene_id=first_scene.id,
            generation_job_id=None,
            asset_type="image",
            file_path=str(asset_path),
            file_exists=True,
        )
        db.add(asset)
        db.commit()
    finally:
        db.close()

    _write_final_voiceover(video_id, provider="openai")
    _write_final_export(video_id)

    status = client.get(f"/videos/{video_id}/final-production/status").json()
    assert status["final_visuals_ready"] is False
    assert status["production_ready"] is False
    assert any("visual" in b.lower() for b in status["blockers"])


def test_get_final_production_status_returns_blockers() -> None:
    """GET final-production/status returns structured status with blockers when not ready."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Status Endpoint Video")
    video_id = ctx["video_id"]

    resp = client.get(f"/videos/{video_id}/final-production/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["video_id"] == video_id
    assert "production_ready" in body
    assert "final_export_ready" in body
    assert "final_voice_ready" in body
    assert "final_visuals_ready" in body
    assert "final_metadata_ready" in body
    assert "blockers" in body
    assert isinstance(body["blockers"], list)
    assert body["production_ready"] is False
    assert body["blockers"]


def test_post_final_production_export_refuses_when_blockers() -> None:
    """POST export returns blocked status and creates no files when blockers exist."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Export Blocked Video")
    video_id = ctx["video_id"]

    final_path = get_settings().output_path / "final_exports" / f"video_{video_id}" / "final.mp4"
    assert not final_path.exists()

    resp = client.post(f"/videos/{video_id}/final-production/export")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["production_ready"] is False
    assert body["blockers"]
    # Must not create final.mp4
    assert not final_path.exists()


def test_full_production_ready_state() -> None:
    """With all 4 checks satisfied, production_ready becomes True."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Full Production Video")
    video_id = ctx["video_id"]
    plan_id = ctx["plan_id"]

    _create_and_approve_visual_asset(client, video_id, plan_id)
    _write_final_voiceover(video_id, provider="openai")
    _write_clean_description(video_id)
    final_path = _write_final_export(video_id)

    resp = client.get(f"/videos/{video_id}/final-production/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["production_ready"] is True
    assert body["final_export_ready"] is True
    assert body["final_voice_ready"] is True
    assert body["final_visuals_ready"] is True
    assert body["final_metadata_ready"] is True
    assert body["final_export_path"] is not None
    assert "final_exports" in body["final_export_path"]
    assert body["blockers"] == []

    export_resp = client.post(f"/videos/{video_id}/final-production/export")
    assert export_resp.status_code == 200
    export_body = export_resp.json()
    assert export_body["status"] == "production_ready"
    assert export_body["production_ready"] is True
    assert export_body["blockers"] == []


def test_publishing_payload_uses_final_export_path_when_production_ready() -> None:
    """video_file_path in payload equals final export path when production_ready."""
    client = TestClient(app)
    ctx = _full_workflow(client, "Payload Final Export Path")
    video_id = ctx["video_id"]
    plan_id = ctx["plan_id"]

    _create_and_approve_visual_asset(client, video_id, plan_id)
    _write_final_voiceover(video_id, provider="openai")
    _write_clean_description(video_id)
    final_path = _write_final_export(video_id)

    resp = client.post(f"/videos/{video_id}/publishing-payload/generate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready_for_manual_upload"] is True
    assert body["video_file_path"] is not None
    assert "final_exports" in body["video_file_path"]
    assert body["blockers"] == []


def test_publishing_payload_never_uses_draft_preview_path() -> None:
    """video_file_path must never point to out/previews/{id}/draft.mp4."""
    client = TestClient(app)
    ctx = _full_workflow(client, "No Draft Preview Path")
    video_id = ctx["video_id"]

    # Only draft preview exists
    resp = client.post(f"/videos/{video_id}/publishing-payload/generate")
    assert resp.status_code == 200
    body = resp.json()

    draft_preview_str = str(
        (get_settings().output_path / "previews" / str(video_id) / "draft.mp4").resolve()
    )
    assert body["video_file_path"] != draft_preview_str
    # video_file_path should be None when no final export
    assert body["video_file_path"] is None or "final_exports" in str(body["video_file_path"])
    # Verify draft preview is not used in written JSON payload either
    written = json.loads(Path(body["payload_path"]).read_text(encoding="utf-8"))
    assert written["paths"]["video_file_path"] != draft_preview_str
