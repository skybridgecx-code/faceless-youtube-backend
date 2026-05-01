from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AssetType, ChannelStudioAgent, ContentType, PublishingPayload, Video, VisualAssetPlan, VisualGeneratedAsset
from app.routers.videos import build_readiness, get_video_or_404, latest_asset, package_dir_for, sync_preview_state
from app.schemas import PublishingPayloadGenerateResponse, PublishingPayloadListItem, PublishingPayloadRead
from app.services.audit import log_audit_event
from app.services.content_engine import DEFAULT_TAGS
from app.services.performance_feedback import latest_performance_for_video, performance_payload_for_video
from app.services.visual_asset_review import asset_review_fields

router = APIRouter(tags=["publishing-payloads"])

_MANUAL_UPLOAD_NOTE = "Manual upload only - no YouTube API upload connected."
_VIDEO_FILE_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm")


def _manual_upload_checklist() -> list[str]:
    return [
        "Confirm title and description.",
        "Confirm thumbnail image reviewed/approved.",
        "Confirm preview reviewed.",
        "Confirm compliance passed.",
        "Confirm video/manual approval complete.",
        "Confirm exported files exist.",
        "Upload manually to YouTube Studio.",
        "Paste final URL back into performance/published_url when available.",
    ]


def _dedupe_messages(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        message = str(value).strip()
        if not message or message in seen:
            continue
        seen.add(message)
        deduped.append(message)
    return deduped


def _safe_existing_file(path_value: str | None) -> str | None:
    if not path_value:
        return None
    try:
        resolved = Path(path_value).expanduser().resolve()
    except OSError:
        return None
    return str(resolved) if resolved.is_file() else None


def _readiness_blockers(video: Video, db: Session) -> tuple[list[str], list[str], str]:
    readiness = build_readiness(video, db)
    blockers = list(readiness.blocking_reasons)
    warnings = list(readiness.warnings)
    if readiness.preview_has_unapproved_visual_assets:
        blockers.append("Registered visual assets include pending/rejected review states.")
    return _dedupe_messages(blockers), _dedupe_messages(warnings), readiness.next_required_action


def _latest_existing_video_file(video: Video, package_dir: str | None) -> str | None:
    preview_path = _safe_existing_file(video.rendered_preview_path)
    if preview_path:
        return preview_path
    if not package_dir:
        return None
    package_root = Path(package_dir)
    if not package_root.is_dir():
        return None
    candidates: list[Path] = []
    for item in sorted(package_root.iterdir()):
        if item.is_file() and item.suffix.lower() in _VIDEO_FILE_EXTENSIONS:
            candidates.append(item)
    return str(candidates[0].resolve()) if candidates else None


def _latest_thumbnail_asset(video_id: int, db: Session) -> tuple[str | None, str | None]:
    stmt = (
        select(VisualGeneratedAsset)
        .join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id)
        .where(
            VisualAssetPlan.video_id == video_id,
            VisualGeneratedAsset.asset_type == "thumbnail",
            VisualGeneratedAsset.file_exists.is_(True),
        )
        .order_by(VisualGeneratedAsset.created_at.desc(), VisualGeneratedAsset.id.desc())
        .limit(1)
    )
    asset = db.scalar(stmt)
    if asset is None:
        return None, None
    safe_path = _safe_existing_file(asset.file_path)
    if safe_path is None:
        return None, None
    review_status = str(asset_review_fields(db, asset).get("review_status") or "pending")
    return safe_path, review_status


def _latest_asset_body(video: Video, asset_type: AssetType) -> str | None:
    row = latest_asset(video, asset_type)
    if row is None:
        return None
    body = row.body.strip()
    return body if body else None


def _render_payload_status(*, existing: PublishingPayload | None, blockers: list[str]) -> str:
    if existing is not None:
        return "regenerated"
    if blockers:
        return "blocked"
    return "ready"


def _next_required_action(*, blockers: list[str], readiness_action: str) -> str:
    if blockers:
        return readiness_action or blockers[0]
    return "Generate or verify final files, then upload manually in YouTube Studio."


def _export_path_for_video(video_id: int) -> str | None:
    candidate = (get_settings().output_path / "exports" / f"video_{video_id}" / "operator_export.json").resolve()
    return str(candidate) if candidate.is_file() else None


def _existing_visual_assets_for_video(video_id: int, db: Session) -> list[dict[str, object]]:
    rows = list(
        db.scalars(
            select(VisualGeneratedAsset)
            .join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id)
            .where(
                VisualAssetPlan.video_id == video_id,
                VisualGeneratedAsset.file_exists.is_(True),
            )
            .order_by(VisualGeneratedAsset.created_at.asc(), VisualGeneratedAsset.id.asc())
        )
    )
    assets: list[dict[str, object]] = []
    for row in rows:
        path = _safe_existing_file(row.file_path)
        if path is None:
            continue
        review = asset_review_fields(db, row)
        assets.append(
            {
                "asset_id": row.id,
                "asset_type": row.asset_type,
                "file_path": path,
                "review_status": str(review.get("review_status") or "pending"),
                "review_notes": review.get("review_notes"),
                "reviewed_at": review.get("reviewed_at"),
            }
        )
    return assets


def _publishing_payload_output_path(video_id: int) -> Path:
    output_path = (get_settings().output_path / "publishing" / f"video_{video_id}" / "publishing_payload.json").resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def _parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _payload_read_from_record(record: PublishingPayload, video: Video) -> PublishingPayloadRead:
    blockers = _parse_json_list(record.blockers_json)
    warnings = _parse_json_list(record.warnings_json)
    checklist = _parse_json_list(record.manual_upload_checklist_json)
    return PublishingPayloadRead(
        id=record.id,
        video_id=record.video_id,
        payload_path=record.payload_path,
        payload_status=str(record.payload_status),
        ready_for_manual_upload=bool(record.ready_for_manual_upload),
        title=record.title,
        description=record.description,
        tags=_parse_json_list(record.tags_json),
        category_id=record.category_id,
        privacy_status=record.privacy_status,
        made_for_kids=bool(record.made_for_kids),
        video_file_path=record.video_file_path,
        thumbnail_image_path=record.thumbnail_image_path,
        export_path=record.export_path,
        blockers=blockers,
        warnings=warnings,
        manual_upload_checklist=checklist,
        generated_at=record.generated_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        next_required_action=_next_required_action(
            blockers=blockers,
            readiness_action=blockers[0] if blockers else video.title,
        ),
    )


@router.post("/videos/{video_id}/publishing-payload/generate", response_model=PublishingPayloadGenerateResponse)
def generate_publishing_payload(video_id: int, db: Session = Depends(get_db)) -> PublishingPayloadGenerateResponse:
    video = get_video_or_404(db, video_id)
    preview_path = sync_preview_state(video)
    blockers, warnings, readiness_action = _readiness_blockers(video, db)

    package_dir = package_dir_for(video)
    video_file_path = _latest_existing_video_file(video, package_dir)
    thumbnail_image_path, thumbnail_review_status = _latest_thumbnail_asset(video.id, db)
    export_path = _export_path_for_video(video.id)
    visual_assets = _existing_visual_assets_for_video(video.id, db)

    if thumbnail_image_path is None:
        warnings.append("Thumbnail image is not generated yet.")
    elif thumbnail_review_status != "approved":
        warnings.append(
            f"Thumbnail visual asset review is '{thumbnail_review_status or 'pending'}'; manual approval is still required."
        )

    if export_path is None:
        warnings.append("Operator export summary does not exist yet.")

    blockers = _dedupe_messages(blockers)
    warnings = _dedupe_messages(warnings)
    checklist = _manual_upload_checklist()
    ready_for_manual_upload = len(blockers) == 0
    next_required_action = _next_required_action(blockers=blockers, readiness_action=readiness_action)

    description = (
        _latest_asset_body(video, AssetType.description)
        or _latest_asset_body(video, AssetType.script)
        or "Manual/local publishing payload summary. Confirm final copy before upload."
    )
    payload_path = _publishing_payload_output_path(video.id)
    existing = db.scalar(select(PublishingPayload).where(PublishingPayload.video_id == video.id).limit(1))
    payload_status = _render_payload_status(existing=existing, blockers=blockers)
    performance_payload = performance_payload_for_video(video.id, latest_performance_for_video(db, video.id))
    channel_studio_agent = db.get(ChannelStudioAgent, video.channel_studio_agent_id) if video.channel_studio_agent_id else None

    payload_document = {
        "video_id": video.id,
        "payload_status": payload_status,
        "ready_for_manual_upload": ready_for_manual_upload,
        "next_required_action": next_required_action,
        "warnings": warnings,
        "blockers": blockers,
        "manual_upload_checklist": checklist,
        "manual_local_note": _MANUAL_UPLOAD_NOTE,
        "video_metadata": {
            "title": video.title[:100],
            "content_type": video.content_type.value,
            "pillar": video.pillar,
            "target_viewer": video.target_viewer,
            "publish_status": video.publish_status,
            "publish_date": video.publish_date,
            "channel_studio_agent": (
                {
                    "id": channel_studio_agent.id,
                    "name": channel_studio_agent.name,
                    "niche": channel_studio_agent.niche,
                    "target_viewer": channel_studio_agent.target_viewer,
                    "launch_wave": channel_studio_agent.launch_wave,
                    "launch_status": channel_studio_agent.launch_status,
                }
                if channel_studio_agent is not None
                else None
            ),
        },
        "youtube_payload": {
            "title": video.title[:100],
            "description": description,
            "tags": list(DEFAULT_TAGS),
            "category_id": "27",
            "privacy_status": "private",
            "made_for_kids": False,
        },
        "workflow_state": {
            "approved": bool(video.approved),
            "preview_exists": preview_path is not None,
            "preview_reviewed": bool(video.preview_reviewed) and preview_path is not None,
            "compliance_status": build_readiness(video, db).compliance_status,
            "publish_status": video.publish_status,
        },
        "paths": {
            "video_file_path": video_file_path,
            "thumbnail_image_path": thumbnail_image_path,
            "preview_path": str(preview_path) if preview_path is not None else None,
            "package_dir": package_dir,
            "export_path": export_path,
            "payload_path": str(payload_path),
        },
        "visual_assets": visual_assets,
        "performance": performance_payload,
        "generated_at": datetime.utcnow().isoformat(),
    }
    payload_path.write_text(json.dumps(payload_document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if existing is None:
        existing = PublishingPayload(video_id=video.id)
        db.add(existing)

    existing.payload_path = str(payload_path)
    existing.payload_status = payload_status
    existing.ready_for_manual_upload = ready_for_manual_upload
    existing.title = video.title[:240]
    existing.description = description
    existing.tags_json = json.dumps(list(DEFAULT_TAGS))
    existing.category_id = "27"
    existing.privacy_status = "private"
    existing.made_for_kids = False
    existing.video_file_path = video_file_path
    existing.thumbnail_image_path = thumbnail_image_path
    existing.export_path = export_path
    existing.blockers_json = json.dumps(blockers)
    existing.warnings_json = json.dumps(warnings)
    existing.manual_upload_checklist_json = json.dumps(checklist)
    existing.generated_at = datetime.utcnow()

    db.commit()
    db.refresh(existing)

    log_audit_event(
        db,
        "publishing_payload_generated",
        f"Generated local publishing payload summary for: {video.title}",
        video_id=video.id,
        metadata={
            "payload_id": existing.id,
            "payload_status": existing.payload_status,
            "ready_for_manual_upload": existing.ready_for_manual_upload,
            "payload_path": existing.payload_path,
        },
    )

    return PublishingPayloadGenerateResponse(
        video_id=video.id,
        payload_id=existing.id,
        payload_path=existing.payload_path,
        payload_status=str(existing.payload_status),
        ready_for_manual_upload=bool(existing.ready_for_manual_upload),
        blockers=blockers,
        warnings=warnings,
        manual_upload_checklist=checklist,
        next_required_action=next_required_action,
        video_file_path=existing.video_file_path,
        thumbnail_image_path=existing.thumbnail_image_path,
        export_path=existing.export_path,
    )


@router.get("/videos/{video_id}/publishing-payload", response_model=PublishingPayloadRead)
def get_video_publishing_payload(video_id: int, db: Session = Depends(get_db)) -> PublishingPayloadRead:
    video = get_video_or_404(db, video_id)
    record = db.scalar(select(PublishingPayload).where(PublishingPayload.video_id == video.id).limit(1))
    if record is None:
        raise HTTPException(status_code=404, detail="Publishing payload not found for video")
    return _payload_read_from_record(record, video)


@router.get("/publishing-payloads", response_model=list[PublishingPayloadListItem])
def list_publishing_payloads(
    status: str = Query(default="all", pattern="^(draft|blocked|ready|regenerated|all)$"),
    ready_for_manual_upload: bool | None = None,
    content_type: ContentType | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[PublishingPayloadListItem]:
    stmt = select(PublishingPayload, Video).join(Video, Video.id == PublishingPayload.video_id)
    if status != "all":
        stmt = stmt.where(PublishingPayload.payload_status == status)
    if ready_for_manual_upload is not None:
        stmt = stmt.where(PublishingPayload.ready_for_manual_upload.is_(ready_for_manual_upload))
    if content_type is not None:
        stmt = stmt.where(Video.content_type == content_type)
    stmt = stmt.order_by(PublishingPayload.generated_at.desc(), PublishingPayload.updated_at.desc()).limit(limit)

    rows = db.execute(stmt).all()
    response: list[PublishingPayloadListItem] = []
    for payload_row, video in rows:
        blockers = _parse_json_list(payload_row.blockers_json)
        warnings = _parse_json_list(payload_row.warnings_json)
        next_action = _next_required_action(
            blockers=blockers,
            readiness_action=blockers[0] if blockers else video.title,
        )
        response.append(
            PublishingPayloadListItem(
                payload_id=payload_row.id,
                video_id=video.id,
                title=video.title,
                content_type=video.content_type,
                payload_status=str(payload_row.payload_status),
                ready_for_manual_upload=bool(payload_row.ready_for_manual_upload),
                blockers_count=len(blockers),
                warnings_count=len(warnings),
                payload_path=payload_row.payload_path,
                generated_at=payload_row.generated_at,
                next_required_action=next_action,
            )
        )
    return response
