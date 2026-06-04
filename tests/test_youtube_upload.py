from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_youtube_upload.db")
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_upload_test_"))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Video  # noqa: E402
from app.services.youtube import YouTubePayload  # noqa: E402
from app.services.youtube_upload import upload_private_video  # noqa: E402


def _reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _payload() -> YouTubePayload:
    return YouTubePayload(title="Test", description="Body", tags=["a", "b"])


def _settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    base = dict(
        enable_youtube_uploads=False,
        youtube_oauth_client_id="",
        youtube_oauth_client_secret="",
        youtube_oauth_refresh_token="",
    )
    base.update(overrides)
    return Settings(**base)


def test_upload_disabled_returns_setup_required(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "final.mp4"
    f.write_bytes(b"\x00" * 32)
    result = upload_private_video(video_file_path=f, payload=_payload(), settings=_settings())
    assert result.status == "setup_required"
    assert result.privacy_status == "private"
    assert result.video_id is None


def test_upload_missing_credentials_returns_setup_required(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "final.mp4"
    f.write_bytes(b"\x00" * 32)
    result = upload_private_video(
        video_file_path=f,
        payload=_payload(),
        settings=_settings(enable_youtube_uploads=True),
    )
    assert result.status == "setup_required"
    assert "YOUTUBE_OAUTH_CLIENT_ID" in result.detail


def test_upload_missing_file_is_blocked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    missing = tmp_path / "does_not_exist.mp4"
    result = upload_private_video(
        video_file_path=missing,
        payload=_payload(),
        settings=_settings(
            enable_youtube_uploads=True,
            youtube_oauth_client_id="cid",
            youtube_oauth_client_secret="secret",
            youtube_oauth_refresh_token="refresh",
        ),
    )
    assert result.status == "blocked"


def test_upload_always_forces_private(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Even a payload that asks for public must stay private.
    f = tmp_path / "final.mp4"
    f.write_bytes(b"\x00" * 32)
    payload = YouTubePayload(title="T", description="D", tags=["x"], privacy_status="public")
    result = upload_private_video(
        video_file_path=f,
        payload=payload,
        settings=_settings(
            enable_youtube_uploads=True,
            youtube_oauth_client_id="cid",
            youtube_oauth_client_secret="secret",
            youtube_oauth_refresh_token="refresh",
        ),
    )
    # Without google libs installed this lands on setup_required, but privacy is always private.
    assert result.privacy_status == "private"
    assert result.status in {"uploaded", "setup_required", "error"}


def test_endpoint_blocks_unapproved_video() -> None:
    _reset_db()
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Upload Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Upload Me"}).json()
    resp = client.post(f"/publish/{video['id']}/youtube/upload")
    assert resp.status_code == 409
    assert "review" in resp.json()["detail"].lower()


def test_endpoint_blocks_when_final_gate_not_approved() -> None:
    _reset_db()
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Upload Channel 2"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Upload Me 2"}).json()

    db = SessionLocal()
    try:
        row = db.get(Video, video["id"])
        row.approved = True
        row.final_approval_status = "pending"
        db.commit()
    finally:
        db.close()

    resp = client.post(f"/publish/{video['id']}/youtube/upload")
    assert resp.status_code == 409
    assert "final approval" in resp.json()["detail"].lower()
