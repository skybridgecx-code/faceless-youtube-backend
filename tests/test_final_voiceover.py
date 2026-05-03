from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite:///./test_content_factory.db"
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_factory_test_"))

import app.main as main_module  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
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

def _create_video(client: TestClient, title: str = "Voiceover Test Video") -> dict:
    channel_id = client.post("/channels", json={"name": f"VO Channel {title}"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": title}).json()
    video_id = video["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    return video


def _write_voiceover_mp3(video_id: int, empty: bool = False) -> Path:
    vo_dir = get_settings().output_path / "final_voiceovers" / f"video_{video_id}"
    vo_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = vo_dir / "voiceover.mp3"
    if empty:
        mp3_path.write_bytes(b"")
    else:
        mp3_path.write_bytes(b"\xff\xfb\x90\x00" * 16)
    return mp3_path


def _write_voiceover_meta(video_id: int, provider: str = "openai") -> Path:
    vo_dir = get_settings().output_path / "final_voiceovers" / f"video_{video_id}"
    vo_dir.mkdir(parents=True, exist_ok=True)
    meta_path = vo_dir / "voiceover_meta.json"
    meta = {"provider": provider, "voice": "onyx", "model": "gpt-4o-mini-tts", "status": "complete"}
    meta_path.write_text(json.dumps(meta))
    return meta_path


def _write_preview_render_meta(video_id: int, provider: str = "openai") -> None:
    meta_path = get_settings().output_path / "previews" / str(video_id) / "render_meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"provider": provider, "audio_generated": True}))


def _write_final_export(video_id: int) -> Path:
    final_path = get_settings().output_path / "final_exports" / f"video_{video_id}" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    return final_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_missing_final_voiceover_blocks() -> None:
    """GET status with no voiceover.mp3 returns blockers."""
    client = TestClient(app)
    video = _create_video(client, "No Voiceover")
    video_id = video["id"]

    resp = client.get(f"/videos/{video_id}/final-voiceover/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["voiceover_ready"] is False
    assert body["voiceover_exists"] is False
    assert any("voiceover" in b.lower() or "generated" in b.lower() for b in body["blockers"])


def test_empty_mp3_blocks() -> None:
    """A zero-byte voiceover.mp3 blocks readiness even with good metadata."""
    client = TestClient(app)
    video = _create_video(client, "Empty MP3")
    video_id = video["id"]

    _write_voiceover_mp3(video_id, empty=True)
    _write_voiceover_meta(video_id, provider="openai")

    resp = client.get(f"/videos/{video_id}/final-voiceover/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["voiceover_ready"] is False
    assert body["voiceover_exists"] is False
    assert body["blockers"]


def test_missing_metadata_blocks() -> None:
    """Non-empty mp3 without voiceover_meta.json blocks readiness."""
    client = TestClient(app)
    video = _create_video(client, "Missing Meta")
    video_id = video["id"]

    _write_voiceover_mp3(video_id)
    # No metadata written

    resp = client.get(f"/videos/{video_id}/final-voiceover/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["voiceover_ready"] is False
    assert body["meta_exists"] is False
    assert body["blockers"]


def test_blocked_providers_block() -> None:
    """macos/silent/fallback/local providers block voiceover readiness."""
    client = TestClient(app)
    video = _create_video(client, "Bad Provider")
    video_id = video["id"]

    for provider in ["macos", "silent", "fallback", "local"]:
        _write_voiceover_mp3(video_id)
        _write_voiceover_meta(video_id, provider=provider)

        resp = client.get(f"/videos/{video_id}/final-voiceover/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["voiceover_ready"] is False, f"provider={provider} should block"
        assert any("production voice" in b.lower() for b in body["blockers"]), (
            f"Expected 'production voice' blocker for provider={provider}"
        )


def test_openai_metadata_and_nonempty_mp3_passes() -> None:
    """OpenAI provider + non-empty mp3 → voiceover_ready=True."""
    client = TestClient(app)
    video = _create_video(client, "OpenAI Ready")
    video_id = video["id"]

    _write_voiceover_mp3(video_id)
    _write_voiceover_meta(video_id, provider="openai")

    resp = client.get(f"/videos/{video_id}/final-voiceover/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["voiceover_ready"] is True
    assert body["voiceover_exists"] is True
    assert body["meta_exists"] is True
    assert body["provider"] == "openai"
    assert body["blockers"] == []
    assert body["voiceover_path"] is not None
    assert "final_voiceovers" in body["voiceover_path"]


def test_elevenlabs_metadata_and_nonempty_mp3_passes() -> None:
    """ElevenLabs provider + non-empty mp3 → voiceover_ready=True."""
    client = TestClient(app)
    video = _create_video(client, "ElevenLabs Ready")
    video_id = video["id"]

    _write_voiceover_mp3(video_id)
    _write_voiceover_meta(video_id, provider="elevenlabs")

    resp = client.get(f"/videos/{video_id}/final-voiceover/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["voiceover_ready"] is True
    assert body["provider"] == "elevenlabs"
    assert body["blockers"] == []


def test_post_generate_missing_openai_key_does_not_create_file() -> None:
    """POST generate with provider=openai and no OPENAI_API_KEY must not create voiceover.mp3."""
    client = TestClient(app)
    video = _create_video(client, "No Key Generate")
    video_id = video["id"]

    saved_key = os.environ.pop("OPENAI_API_KEY", None)
    try:
        resp = client.post(
            f"/videos/{video_id}/final-voiceover/generate",
            json={"provider": "openai"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "blocked"
        assert body["voiceover_path"] is None
        assert any("OPENAI_API_KEY" in b for b in body["blockers"])
    finally:
        if saved_key is not None:
            os.environ["OPENAI_API_KEY"] = saved_key

    mp3_path = get_settings().output_path / "final_voiceovers" / f"video_{video_id}" / "voiceover.mp3"
    assert not mp3_path.exists(), "No voiceover.mp3 must be created when API key is missing"


def test_unsupported_provider_returns_blocker() -> None:
    """POST generate with an unknown provider returns a production voice blocker."""
    client = TestClient(app)
    video = _create_video(client, "Unknown Provider")
    video_id = video["id"]

    resp = client.post(
        f"/videos/{video_id}/final-voiceover/generate",
        json={"provider": "fakeai"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert any("production voice" in b.lower() for b in body["blockers"])


def test_final_production_no_longer_uses_preview_render_meta() -> None:
    """Final production voice check uses final voiceover, not preview render_meta.json."""
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Preview Meta Check"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Preview Meta Check"}).json()
    video_id = video["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200

    # Write a preview render_meta with a production provider (old Phase 30 approach)
    _write_preview_render_meta(video_id, provider="openai")

    # Voice should still be NOT ready because final voiceover doesn't exist
    status = client.get(f"/videos/{video_id}/final-production/status").json()
    assert status["final_voice_ready"] is False, (
        "Preview render_meta alone must NOT satisfy final voice readiness"
    )
    assert any(
        "voiceover" in b.lower() or "generated" in b.lower() for b in status["blockers"]
    )

    # Now write the actual final voiceover — voice should become ready
    _write_voiceover_mp3(video_id)
    _write_voiceover_meta(video_id, provider="openai")

    status2 = client.get(f"/videos/{video_id}/final-production/status").json()
    assert status2["final_voice_ready"] is True


def test_publishing_payload_blocked_without_final_voiceover() -> None:
    """Publishing payload remains blocked when final voiceover does not exist."""
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "VO Payload Block"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "VO Payload Block"}).json()
    video_id = video["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "ok"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{video_id}").status_code == 200

    preview_path = get_settings().output_path / "previews" / str(video_id) / "draft.mp4"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")

    assert client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{video_id}/package").status_code == 200
    assert client.post(f"/publish/{video_id}/prepare-youtube-payload").status_code == 200

    resp = client.post(f"/videos/{video_id}/publishing-payload/generate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready_for_manual_upload"] is False
    assert any(
        "voiceover" in b.lower() or "voice" in b.lower() or "generated" in b.lower()
        for b in body["blockers"]
    )
