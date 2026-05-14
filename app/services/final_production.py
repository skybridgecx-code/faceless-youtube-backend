from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AssetType, ContentAsset, PublishingPayload, Video, VisualAssetPlan, VisualGeneratedAsset
from app.schemas import FinalProductionStatus
from app.services.final_voiceover import assess_final_voiceover
from app.services.visual_asset_review import asset_review_fields

_PLACEHOLDER_PATTERNS = ["[INSERT LINK]", "How to I Built", "Draft Preview"]

# Minimal valid MP4 ftyp box used for local placeholder stubs.
_MP4_STUB_HEADER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def _export_dir_for_video(video_id: int) -> Path:
    return get_settings().output_path / "final_exports" / f"video_{video_id}"


def final_export_path_for_video(video_id: int) -> Path:
    return _export_dir_for_video(video_id) / "final.mp4"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for v in values:
        msg = v.strip()
        if msg and msg not in seen:
            seen.add(msg)
            result.append(msg)
    return result


def final_voice_ready_for_video(video_id: int) -> tuple[bool, list[str]]:
    status = assess_final_voiceover(video_id)
    if not status.voiceover_ready:
        return False, status.blockers
    return True, []


def final_visuals_ready_for_video(video_id: int, db: Session) -> tuple[bool, list[str]]:
    rows = list(
        db.scalars(
            select(VisualGeneratedAsset)
            .join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id)
            .where(
                VisualAssetPlan.video_id == video_id,
                VisualGeneratedAsset.file_exists.is_(True),
            )
        )
    )
    if not rows:
        return False, ["Final visual assets must be approved for production"]
    for row in rows:
        review = asset_review_fields(db, row)
        status = str(review.get("review_status") or "pending")
        if status != "approved":
            return False, ["Final visual assets must be approved for production"]
    return True, []


def _latest_asset_body(video: Video, asset_type: AssetType) -> str | None:
    assets = [a for a in video.assets if a.asset_type == asset_type]
    if not assets:
        return None
    latest: ContentAsset = sorted(assets, key=lambda a: a.created_at, reverse=True)[0]
    body = latest.body.strip()
    return body if body else None


def metadata_blockers_for_video(video: Video) -> list[str]:
    title = (video.title or "").strip()
    description = (
        _latest_asset_body(video, AssetType.description)
        or _latest_asset_body(video, AssetType.script)
        or ""
    )
    blockers: list[str] = []
    if not title:
        blockers.append("YouTube metadata requires editorial cleanup")
    for pattern in _PLACEHOLDER_PATTERNS:
        if pattern in title or pattern in description:
            if pattern == "[INSERT LINK]":
                blockers.append("YouTube metadata contains placeholder text")
            else:
                blockers.append("YouTube metadata requires editorial cleanup")
    return _dedupe(blockers)


def assess_preexport_readiness(video: Video, db: Session) -> tuple[bool, list[str]]:
    """Check voice, visuals, and metadata — not the export file itself.

    Used by the export endpoint to decide whether artifact creation is allowed.
    """
    voice_ready, voice_blockers = final_voice_ready_for_video(video.id)
    visuals_ready, visual_blockers = final_visuals_ready_for_video(video.id, db)
    meta_blockers = metadata_blockers_for_video(video)
    all_blockers = _dedupe(voice_blockers + visual_blockers + meta_blockers)
    ready = voice_ready and visuals_ready and not meta_blockers
    return ready, all_blockers


def build_local_export_artifacts(video: Video, db: Session) -> dict[str, str]:
    """Create deterministic local export stubs and manifest. Returns artifact paths."""
    video_id = video.id
    export_dir = _export_dir_for_video(video_id)
    export_dir.mkdir(parents=True, exist_ok=True)

    final_path = export_dir / "final.mp4"
    stub_path = export_dir / "final_video_stub.mp4"
    manifest_path = export_dir / "final_export_manifest.json"

    final_path.write_bytes(_MP4_STUB_HEADER + b"FINAL_LOCAL_EXPORT")
    stub_path.write_bytes(_MP4_STUB_HEADER + b"STUB_LOCAL_EXPORT")

    voiceover_status = assess_final_voiceover(video_id)
    voiceover_path = voiceover_status.voiceover_path

    payload_row = db.scalar(
        select(PublishingPayload).where(PublishingPayload.video_id == video_id)
    )
    publishing_payload_path = (
        payload_row.payload_path if payload_row and payload_row.payload_path else None
    )

    vis_rows = list(
        db.scalars(
            select(VisualGeneratedAsset)
            .join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id)
            .where(
                VisualAssetPlan.video_id == video_id,
                VisualGeneratedAsset.file_exists.is_(True),
            )
        )
    )
    approved_count = sum(
        1
        for row in vis_rows
        if str(asset_review_fields(db, row).get("review_status") or "pending") == "approved"
    )

    manifest: dict = {
        "video_id": video_id,
        "title": video.title or "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "final_voiceover_path": voiceover_path,
        "publishing_payload_path": publishing_payload_path,
        "visual_asset_summary": {"total": len(vis_rows), "approved": approved_count},
        "readiness_status": "production_ready",
        "final_export_path": str(final_path.resolve()),
        "final_video_stub_path": str(stub_path.resolve()),
        "note": "This is a local export artifact. It has not been uploaded to YouTube.",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "final_export_path": str(final_path.resolve()),
        "final_video_stub_path": str(stub_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
    }


def assess_final_production(video: Video, db: Session) -> FinalProductionStatus:
    video_id = video.id

    export_path = final_export_path_for_video(video_id)
    try:
        final_export_ready = export_path.is_file() and export_path.stat().st_size > 0
    except OSError:
        final_export_ready = False

    voice_ready, voice_blockers = final_voice_ready_for_video(video_id)
    visuals_ready, visual_blockers = final_visuals_ready_for_video(video_id, db)
    meta_blockers = metadata_blockers_for_video(video)
    metadata_ready = len(meta_blockers) == 0

    all_blockers: list[str] = []
    if not final_export_ready:
        all_blockers.append("Final video export must be generated before manual upload")
    all_blockers.extend(voice_blockers)
    all_blockers.extend(visual_blockers)
    all_blockers.extend(meta_blockers)
    all_blockers = _dedupe(all_blockers)

    production_ready = final_export_ready and voice_ready and visuals_ready and metadata_ready

    return FinalProductionStatus(
        video_id=video_id,
        production_ready=production_ready,
        final_export_ready=final_export_ready,
        final_voice_ready=voice_ready,
        final_visuals_ready=visuals_ready,
        final_metadata_ready=metadata_ready,
        final_export_path=str(export_path.resolve()) if final_export_ready else None,
        blockers=all_blockers,
        warnings=[],
    )
