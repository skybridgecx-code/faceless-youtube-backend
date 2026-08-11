import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test_content_factory.db"
os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="yt_factory_test_review_"))

import app.main as main_module  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import VisualAssetPlan, VisualGeneratedAsset, VisualScene  # noqa: E402
from app.security import InMemoryRateLimiter  # noqa: E402
from tests.db_helpers import reset_migrated_test_database  # noqa: E402


@pytest.fixture(autouse=True)
def reset_test_db() -> None:
    reset_migrated_test_database()
    main_module.rate_limiter = InMemoryRateLimiter()
    main_module.settings.internal_api_key = None
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def _write_headers() -> dict[str, str]:
    if main_module.settings.internal_api_key:
        return {"X-Internal-API-Key": str(main_module.settings.internal_api_key)}
    return {}


def _write_real_visual_asset_file(name: str = "visual_asset_review.png") -> Path:
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    asset_path = output_dir / "generated" / name
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return asset_path


def _write_real_preview_file(video_id: int) -> Path:
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    preview_path = output_dir / "previews" / str(video_id) / "draft.mp4"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat"
    )
    return preview_path


def _create_approved_video_for_preview(client: TestClient, channel_name: str, title: str) -> int:
    headers = _write_headers()
    channel_response = client.post("/channels", json={"name": channel_name}, headers=headers)
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]
    video_response = client.post("/videos", json={"channel_id": channel_id, "title": title}, headers=headers)
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}, headers=headers).status_code == 200
    assert client.post(
        f"/videos/{video_id}/review",
        json={"passed": True, "notes": "approved"},
        headers=headers,
    ).status_code == 200
    return video_id


def _create_visual_asset_for_review(client: TestClient, filename: str) -> tuple[int, int, int]:
    video_id = _create_approved_video_for_preview(
        client,
        channel_name=f"Asset Review Channel {filename}",
        title=f"Asset Review Video {filename}",
    )
    plan_response = client.post(f"/visual-assets/from-video/{video_id}", headers=_write_headers())
    assert plan_response.status_code == 200
    plan = plan_response.json()
    real_asset_path = _write_real_visual_asset_file(filename)

    db = SessionLocal()
    try:
        plan_row = db.scalar(select(VisualAssetPlan).where(VisualAssetPlan.id == plan["id"]).limit(1))
        assert plan_row is not None
        first_scene = db.scalar(
            select(VisualScene)
            .where(VisualScene.plan_id == plan_row.id)
            .order_by(VisualScene.scene_number.asc())
            .limit(1)
        )
        assert first_scene is not None
        asset = VisualGeneratedAsset(
            visual_asset_plan_id=plan_row.id,
            visual_scene_id=first_scene.id,
            generation_job_id=None,
            asset_type="image",
            file_path=str(real_asset_path),
            file_exists=True,
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
    finally:
        db.close()
    return video_id, plan["id"], asset_id


def test_visual_asset_review_endpoints_require_internal_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")
    _, _, asset_id = _create_visual_asset_for_review(client, "review_requires_key.png")

    assert client.post(f"/visual-generation/assets/{asset_id}/approve").status_code == 401
    assert client.post(f"/visual-generation/assets/{asset_id}/reject", json={"review_notes": "bad"}).status_code == 401
    assert client.patch(
        f"/visual-generation/assets/{asset_id}/review",
        json={"review_status": "approved"},
    ).status_code == 401


def test_visual_asset_approve_sets_review_status_and_reviewed_at(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")
    video_id, _, asset_id = _create_visual_asset_for_review(client, "review_approve.png")

    before = client.get(f"/videos/{video_id}").json()
    response = client.post(
        f"/visual-generation/assets/{asset_id}/approve",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["review_status"] == "approved"
    assert payload["reviewed_at"] is not None

    after = client.get(f"/videos/{video_id}").json()
    assert after["approved"] == before["approved"]
    assert after["preview_reviewed"] == before["preview_reviewed"]


def test_visual_asset_reject_sets_notes_and_does_not_mark_preview_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")
    video_id, _, asset_id = _create_visual_asset_for_review(client, "review_reject.png")

    response = client.post(
        f"/visual-generation/assets/{asset_id}/reject",
        json={"review_notes": "Composition does not match the scene."},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["review_status"] == "rejected"
    assert payload["review_notes"] == "Composition does not match the scene."
    assert payload["reviewed_at"] is not None

    video = client.get(f"/videos/{video_id}").json()
    assert video["preview_reviewed"] is False


def test_preview_manifest_includes_review_status_and_pending_warning() -> None:
    client = TestClient(app)
    video_id, _, asset_id = _create_visual_asset_for_review(client, "review_manifest_pending.png")

    response = client.get(f"/visual-generation/preview-assets/videos/{video_id}")
    assert response.status_code == 200
    manifest = response.json()
    matching_assets = [asset for asset in manifest["assets"] if asset["asset_id"] == asset_id]
    assert matching_assets
    assert matching_assets[0]["review_status"] == "pending"
    assert any("manual approval" in warning for warning in manifest["visual_asset_warnings"])


def test_preview_manifest_warning_clears_after_asset_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")
    video_id, _, asset_id = _create_visual_asset_for_review(client, "review_manifest_approved.png")

    approve_response = client.post(
        f"/visual-generation/assets/{asset_id}/approve",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert approve_response.status_code == 200

    manifest_response = client.get(f"/visual-generation/preview-assets/videos/{video_id}")
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    matching_assets = [asset for asset in manifest["assets"] if asset["asset_id"] == asset_id]
    assert matching_assets
    assert matching_assets[0]["review_status"] == "approved"
    assert not any(f"Asset {asset_id} is pending" in warning for warning in manifest["visual_asset_warnings"])


def test_readiness_reflects_visual_review_counts_and_clears_warning_after_approval() -> None:
    client = TestClient(app)
    video_id, _, asset_id = _create_visual_asset_for_review(client, "review_readiness_counts.png")

    readiness_response = client.get(f"/videos/{video_id}/readiness")
    assert readiness_response.status_code == 200
    readiness = readiness_response.json()
    assert readiness["visual_assets_registered_count"] >= 1
    assert readiness["visual_assets_pending_count"] >= 1
    assert readiness["visual_assets_approved_count"] == 0
    assert readiness["preview_has_unapproved_visual_assets"] is True
    assert any("pending/rejected" in warning.lower() for warning in readiness["warnings"])

    approve_response = client.post(f"/visual-generation/assets/{asset_id}/approve")
    assert approve_response.status_code == 200

    readiness_after_response = client.get(f"/videos/{video_id}/readiness")
    assert readiness_after_response.status_code == 200
    readiness_after = readiness_after_response.json()
    assert readiness_after["visual_assets_registered_count"] >= 1
    assert readiness_after["visual_assets_pending_count"] == 0
    assert readiness_after["visual_assets_rejected_count"] == 0
    assert readiness_after["visual_assets_approved_count"] >= 1
    assert readiness_after["preview_has_unapproved_visual_assets"] is False
    assert not any("pending/rejected" in warning.lower() for warning in readiness_after["warnings"])


def test_package_and_payload_remain_blocked_until_existing_preview_review_gate() -> None:
    client = TestClient(app)
    video_id, _, asset_id = _create_visual_asset_for_review(client, "review_blocking_gates.png")
    _write_real_preview_file(video_id)

    assert client.post(f"/visual-generation/assets/{asset_id}/approve").status_code == 200

    blocked_package = client.post(f"/videos/{video_id}/package")
    assert blocked_package.status_code == 409
    assert "preview" in blocked_package.json()["detail"].lower()

    blocked_payload = client.post(f"/publish/{video_id}/prepare-youtube-payload")
    assert blocked_payload.status_code == 409
    assert "preview" in blocked_payload.json()["detail"].lower()
