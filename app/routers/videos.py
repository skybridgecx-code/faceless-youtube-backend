from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    AssetType,
    AuditEvent,
    Channel,
    ContentAsset,
    PublishRecord,
    Review,
    Video,
    VideoStatus,
)
from app.schemas import (
    AssetPreview,
    AssetRead,
    AssetUpdate,
    AuditEventRead,
    BulkIdeaRequest,
    ComplianceReport,
    GenerateRequest,
    OperatorExport,
    PackageResponse,
    ReviewCreate,
    ReviewRead,
    VideoBatchCreate,
    VideoCreate,
    VideoPublishUpdate,
    VideoRead,
    VideoReadiness,
    VideoUpdate,
)
from app.services.audit import log_audit_event
from app.services.compliance import run_compliance_checks
from app.services.content_engine import (
    build_all_assets,
    build_brief,
    build_description,
    build_script,
    build_shorts,
    build_thumbnail_prompt,
    build_youtube_metadata,
    generate_video_ideas,
)
from app.services.package_builder import build_video_package, slugify

router = APIRouter(prefix="/videos", tags=["videos"])


def get_video_or_404(db: Session, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def parse_asset_type_or_400(asset_type: str) -> AssetType:
    try:
        return AssetType(asset_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid asset type: {asset_type}") from exc


def latest_asset(video: Video, asset_type: AssetType) -> ContentAsset | None:
    assets = [asset for asset in video.assets if asset.asset_type == asset_type]
    return sorted(assets, key=lambda asset: asset.created_at, reverse=True)[0] if assets else None


def build_readiness(video: Video, db: Session) -> VideoReadiness:
    assets_generated = len(video.assets) > 0
    review_approved = video.approved
    package_created = latest_asset(video, AssetType.package_manifest) is not None

    youtube_record = db.scalar(
        select(PublishRecord)
        .where(PublishRecord.video_id == video.id, PublishRecord.platform == "youtube")
        .limit(1)
    )
    youtube_metadata_prepared = youtube_record is not None
    publish_date_set = video.publish_date is not None
    publish_status_ready_or_scheduled = video.publish_status in ("ready", "scheduled")

    blocking_reasons: list[str] = []
    if not assets_generated:
        blocking_reasons.append("Assets must be generated first")
    if not review_approved:
        blocking_reasons.append("Video must be approved via manual review")
    if not package_created:
        blocking_reasons.append("Video must be packaged")
    if not youtube_metadata_prepared:
        blocking_reasons.append("YouTube metadata must be prepared")
    if publish_status_ready_or_scheduled and not publish_date_set:
        blocking_reasons.append("Publish date should be set if status is ready or scheduled")

    report = run_compliance_checks(video)

    return VideoReadiness(
        assets_generated=assets_generated,
        review_approved=review_approved,
        package_created=package_created,
        youtube_metadata_prepared=youtube_metadata_prepared,
        publish_date_set=publish_date_set,
        publish_status_ready_or_scheduled=publish_status_ready_or_scheduled,
        blocking_reasons=blocking_reasons,
        compliance_status=report.overall_status,
        compliance_blockers_count=sum(1 for check in report.checks if check.status == "blocked"),
        compliance_warnings_count=sum(1 for check in report.checks if check.status == "warning"),
    )


def package_dir_for(video: Video) -> str | None:
    if latest_asset(video, AssetType.package_manifest) is None:
        return None
    return str(get_settings().output_path / f"{video.id:04d}-{slugify(video.title)}")


def asset_previews(video: Video) -> list[AssetPreview]:
    previews: list[AssetPreview] = []
    for asset in sorted(video.assets, key=lambda item: (item.asset_type.value, item.created_at)):
        safe_preview = " ".join(asset.body.split())[:180]
        previews.append(
            AssetPreview(
                id=asset.id,
                asset_type=asset.asset_type,
                version=asset.version,
                created_at=asset.created_at,
                body_length=len(asset.body),
                body_preview=safe_preview,
            )
        )
    return previews


@router.post("", response_model=VideoRead)
def create_video(payload: VideoCreate, db: Session = Depends(get_db)) -> Video:
    channel = db.get(Channel, payload.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    video = Video(**payload.model_dump())
    db.add(video)
    db.commit()
    db.refresh(video)
    log_audit_event(
        db,
        "video_created",
        f"Created video idea: {video.title}",
        video_id=video.id,
        metadata={"title": video.title, "channel_id": video.channel_id},
    )
    return video


@router.post("/ideas/bulk", response_model=list[VideoRead])
def create_bulk_ideas(payload: BulkIdeaRequest, db: Session = Depends(get_db)) -> list[Video]:
    channel = db.get(Channel, payload.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    videos: list[Video] = []
    for idea in generate_video_ideas(payload.count):
        video = Video(channel_id=payload.channel_id, **idea)
        db.add(video)
        videos.append(video)
    db.commit()
    for video in videos:
        db.refresh(video)
    log_audit_event(
        db,
        "batch_created",
        f"Generated {len(videos)} bulk video ideas",
        metadata={"channel_id": payload.channel_id, "count": len(videos), "video_ids": [v.id for v in videos]},
    )
    return videos


@router.get("", response_model=list[VideoRead])
def list_videos(
    channel_id: int | None = None,
    status: VideoStatus | None = None,
    publish_status: str | None = None,
    approved: bool | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
) -> list[Video]:
    stmt = select(Video).order_by(Video.created_at.desc())
    if channel_id is not None:
        stmt = stmt.where(Video.channel_id == channel_id)
    if status is not None:
        stmt = stmt.where(Video.status == status)
    if publish_status is not None:
        stmt = stmt.where(Video.publish_status == publish_status)
    if approved is not None:
        stmt = stmt.where(Video.approved == approved)
    if search:
        search_term = f"%{search}%"
        stmt = stmt.where(
            or_(
                Video.title.ilike(search_term),
                Video.niche.ilike(search_term),
                Video.target_audience.ilike(search_term),
                Video.angle.ilike(search_term),
                Video.notes.ilike(search_term),
            )
        )
    return list(db.scalars(stmt))


@router.post("/batch", response_model=list[VideoRead])
def create_video_batch(payload: VideoBatchCreate, db: Session = Depends(get_db)) -> list[Video]:
    channel = db.get(Channel, payload.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    for item in payload.videos:
        if not item.title or not item.title.strip():
            raise HTTPException(status_code=400, detail="Title cannot be empty")

    videos: list[Video] = []
    for item in payload.videos:
        video = Video(
            channel_id=payload.channel_id,
            title=item.title.strip(),
            niche=item.niche,
            target_audience=item.target_audience,
            angle=item.angle,
            notes=item.notes,
            status=VideoStatus.idea,
            approved=False,
        )
        db.add(video)
        videos.append(video)

    db.commit()
    for video in videos:
        db.refresh(video)

    log_audit_event(
        db,
        "batch_created",
        f"Created {len(videos)} video ideas from batch import",
        metadata={"channel_id": payload.channel_id, "count": len(videos), "video_ids": [v.id for v in videos]},
    )
    return videos


@router.get("/{video_id}/audit", response_model=list[AuditEventRead])
def get_video_audit(
    video_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[AuditEvent]:
    get_video_or_404(db, video_id)
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.video_id == video_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


@router.get("/{video_id}/operator-export", response_model=OperatorExport)
def export_operator_summary(video_id: int, db: Session = Depends(get_db)) -> OperatorExport:
    video = get_video_or_404(db, video_id)
    readiness = build_readiness(video, db)
    report = run_compliance_checks(video)
    audit_events = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.video_id == video.id)
            .order_by(AuditEvent.created_at.desc())
            .limit(100)
        )
    )

    package_dir = package_dir_for(video)
    has_youtube_metadata_asset = latest_asset(video, AssetType.youtube_metadata) is not None
    youtube_record = db.scalar(
        select(PublishRecord)
        .where(PublishRecord.video_id == video.id, PublishRecord.platform == "youtube")
        .limit(1)
    )
    youtube_payload_ready = youtube_record is not None

    return OperatorExport(
        video=video,
        workflow_status=video.status.value,
        publishing_plan={
            "publish_date": video.publish_date,
            "publish_status": video.publish_status,
            "publish_notes": video.publish_notes,
        },
        readiness=readiness,
        compliance_summary={
            "overall_status": report.overall_status,
            "blockers": sum(1 for check in report.checks if check.status == "blocked"),
            "warnings": sum(1 for check in report.checks if check.status == "warning"),
        },
        assets=asset_previews(video),
        audit_events=audit_events,
        package_dir=package_dir,
        youtube_payload_readiness={
            "approved": video.approved,
            "package_exists": package_dir is not None,
            "youtube_metadata_asset_exists": has_youtube_metadata_asset,
            "publish_record_exists": youtube_payload_ready,
            "ready": video.approved and package_dir is not None and has_youtube_metadata_asset and youtube_payload_ready,
        },
    )


@router.get("/{video_id}", response_model=VideoRead)
def read_video(video_id: int, db: Session = Depends(get_db)) -> Video:
    return get_video_or_404(db, video_id)


@router.patch("/{video_id}", response_model=VideoRead)
def update_video(video_id: int, payload: VideoUpdate, db: Session = Depends(get_db)) -> Video:
    video = get_video_or_404(db, video_id)
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(video, key, value)
    db.commit()
    db.refresh(video)
    log_audit_event(
        db,
        "video_updated",
        f"Updated video idea: {video.title}",
        video_id=video.id,
        metadata={"updated_fields": sorted(update_data.keys())},
    )
    return video


@router.delete("/{video_id}")
def delete_video(video_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    video = get_video_or_404(db, video_id)
    title = video.title
    asset_count = len(video.assets)
    db.delete(video)
    db.commit()
    log_audit_event(
        db,
        "video_deleted",
        f"Deleted video idea: {title}",
        metadata={"deleted_video_id": video_id, "title": title, "asset_count": asset_count},
    )
    return {"ok": True}


@router.patch("/{video_id}/publishing", response_model=VideoRead)
def update_video_publishing(video_id: int, payload: VideoPublishUpdate, db: Session = Depends(get_db)) -> Video:
    video = get_video_or_404(db, video_id)
    update_data = payload.model_dump(exclude_unset=True)

    if "publish_status" in update_data:
        new_status = update_data["publish_status"]
        if new_status in ("scheduled", "ready") and not video.approved:
            raise HTTPException(
                status_code=400,
                detail="Cannot set publish status to ready or scheduled for an unapproved video",
            )

    for key, value in update_data.items():
        setattr(video, key, value)

    db.commit()
    db.refresh(video)
    log_audit_event(
        db,
        "publishing_updated",
        f"Updated publishing plan for: {video.title}",
        video_id=video.id,
        metadata={"updated_fields": sorted(update_data.keys()), "publish_status": video.publish_status},
    )
    return video


@router.get("/{video_id}/readiness", response_model=VideoReadiness)
def get_video_readiness(video_id: int, db: Session = Depends(get_db)) -> VideoReadiness:
    video = get_video_or_404(db, video_id)
    return build_readiness(video, db)


@router.get("/{video_id}/assets", response_model=list[AssetRead])
def list_assets(video_id: int, db: Session = Depends(get_db)) -> list[ContentAsset]:
    get_video_or_404(db, video_id)
    stmt = select(ContentAsset).where(ContentAsset.video_id == video_id).order_by(ContentAsset.created_at.desc())
    return list(db.scalars(stmt))


@router.post("/{video_id}/generate", response_model=list[AssetRead])
def generate_assets(video_id: int, payload: GenerateRequest = GenerateRequest(), db: Session = Depends(get_db)) -> list[ContentAsset]:
    video = get_video_or_404(db, video_id)
    generated = []

    if payload.stage == "all":
        generated = build_all_assets(video)
    elif payload.stage == "brief":
        generated = [("brief", build_brief(video))]
    elif payload.stage == "script":
        generated = [("script", build_script(video))]
    elif payload.stage == "shorts":
        generated = [("shorts", build_shorts(video))]
    elif payload.stage == "description":
        generated = [("description", build_description(video))]
    elif payload.stage == "thumbnail_prompt":
        generated = [("thumbnail_prompt", build_thumbnail_prompt(video))]
    elif payload.stage == "metadata":
        generated = [("youtube_metadata", build_youtube_metadata(video))]

    assets: list[ContentAsset] = []
    for item in generated:
        if isinstance(item, tuple):
            asset_type, body = item
        else:
            asset_type, body = item.asset_type, item.body
        asset = ContentAsset(video_id=video.id, asset_type=AssetType(asset_type), body=body)
        db.add(asset)
        assets.append(asset)

    video.status = VideoStatus.needs_review
    video.approved = False
    db.commit()
    for asset in assets:
        db.refresh(asset)

    log_audit_event(
        db,
        "assets_generated",
        f"Generated {len(assets)} asset(s) for: {video.title}",
        video_id=video.id,
        metadata={"stage": payload.stage, "asset_types": [asset.asset_type.value for asset in assets], "count": len(assets)},
    )
    return assets


@router.patch("/{video_id}/assets/{asset_type}", response_model=AssetRead)
def update_asset(video_id: int, asset_type: str, payload: AssetUpdate, db: Session = Depends(get_db)) -> ContentAsset:
    parsed_asset_type = parse_asset_type_or_400(asset_type)
    video = get_video_or_404(db, video_id)
    stmt = (
        select(ContentAsset)
        .where(ContentAsset.video_id == video.id, ContentAsset.asset_type == parsed_asset_type)
        .order_by(ContentAsset.created_at.desc())
        .limit(1)
    )
    asset = db.scalar(stmt)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset.body = payload.body
    asset.version += 1
    video.status = VideoStatus.needs_review
    video.approved = False

    db.commit()
    db.refresh(asset)
    log_audit_event(
        db,
        "asset_updated",
        f"Updated {parsed_asset_type.value} asset for: {video.title}",
        video_id=video.id,
        metadata={"asset_type": parsed_asset_type.value, "version": asset.version, "body_length": len(payload.body)},
    )
    return asset


@router.post("/{video_id}/assets/{asset_type}/regenerate", response_model=AssetRead)
def regenerate_asset(video_id: int, asset_type: str, db: Session = Depends(get_db)) -> ContentAsset:
    parsed_asset_type = parse_asset_type_or_400(asset_type)
    video = get_video_or_404(db, video_id)
    stmt = (
        select(ContentAsset)
        .where(ContentAsset.video_id == video.id, ContentAsset.asset_type == parsed_asset_type)
        .order_by(ContentAsset.created_at.desc())
        .limit(1)
    )
    asset = db.scalar(stmt)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    if parsed_asset_type == AssetType.brief:
        new_body = build_brief(video)
    elif parsed_asset_type == AssetType.script:
        new_body = build_script(video)
    elif parsed_asset_type == AssetType.shorts:
        new_body = build_shorts(video)
    elif parsed_asset_type == AssetType.description:
        new_body = build_description(video)
    elif parsed_asset_type == AssetType.thumbnail_prompt:
        new_body = build_thumbnail_prompt(video)
    elif parsed_asset_type == AssetType.youtube_metadata:
        new_body = build_youtube_metadata(video)
    elif parsed_asset_type == AssetType.package_manifest:
        from app.services.compliance import build_review_checklist

        script_asset = latest_asset(video, AssetType.script)
        script_body = script_asset.body if script_asset else build_script(video)
        new_body = build_review_checklist(script_body)
    else:
        raise HTTPException(status_code=400, detail="Cannot regenerate this asset type")

    asset.body = new_body
    asset.version += 1
    video.status = VideoStatus.needs_review
    video.approved = False

    db.commit()
    db.refresh(asset)
    log_audit_event(
        db,
        "asset_regenerated",
        f"Regenerated {parsed_asset_type.value} asset for: {video.title}",
        video_id=video.id,
        metadata={"asset_type": parsed_asset_type.value, "version": asset.version, "body_length": len(new_body)},
    )
    return asset


@router.post("/{video_id}/review", response_model=ReviewRead)
def review_video(video_id: int, payload: ReviewCreate, db: Session = Depends(get_db)) -> Review:
    video = get_video_or_404(db, video_id)

    if payload.passed:
        report = run_compliance_checks(video)
        if report.overall_status == "blocked":
            raise HTTPException(status_code=400, detail="Cannot approve video with blocked compliance checks.")

    review = Review(video_id=video.id, **payload.model_dump())
    db.add(review)
    video.approved = payload.passed
    video.status = VideoStatus.approved if payload.passed else VideoStatus.rejected
    db.commit()
    db.refresh(review)

    log_audit_event(
        db,
        "review_approved" if payload.passed else "review_rejected",
        f"{'Approved' if payload.passed else 'Rejected'} video after manual review: {video.title}",
        video_id=video.id,
        metadata={"passed": payload.passed, "review_id": review.id, "notes_length": len(payload.notes or "")},
    )
    return review


@router.post("/{video_id}/package", response_model=PackageResponse)
def package_video(video_id: int, db: Session = Depends(get_db)) -> PackageResponse:
    video = get_video_or_404(db, video_id)
    try:
        package_dir, manifest_asset = build_video_package(db, video)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    log_audit_event(
        db,
        "package_created",
        f"Created local package for: {video.title}",
        video_id=video.id,
        metadata={"package_dir": str(package_dir), "manifest_asset_id": manifest_asset.id},
    )
    return PackageResponse(video_id=video.id, package_dir=str(package_dir), manifest_asset_id=manifest_asset.id)


@router.post("/{video_id}/compliance/run", response_model=ComplianceReport)
def run_compliance(video_id: int, db: Session = Depends(get_db)) -> ComplianceReport:
    video = get_video_or_404(db, video_id)
    report = run_compliance_checks(video)
    log_audit_event(
        db,
        "compliance_run",
        f"Ran compliance check for: {video.title}",
        video_id=video.id,
        metadata={
            "overall_status": report.overall_status,
            "blockers": sum(1 for check in report.checks if check.status == "blocked"),
            "warnings": sum(1 for check in report.checks if check.status == "warning"),
        },
    )
    return report
