from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AssetType, Channel, ContentAsset, Review, Video, VideoStatus
from app.schemas import (
    AssetRead,
    AssetUpdate,
    BulkIdeaRequest,
    GenerateRequest,
    PackageResponse,
    ReviewCreate,
    ReviewRead,
    VideoCreate,
    VideoRead,
    VideoUpdate,
    VideoPublishUpdate,
    VideoReadiness,
    VideoBatchCreate,
)
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
from app.services.package_builder import build_video_package

router = APIRouter(prefix="/videos", tags=["videos"])


def get_video_or_404(db: Session, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


@router.post("", response_model=VideoRead)
def create_video(payload: VideoCreate, db: Session = Depends(get_db)) -> Video:
    channel = db.get(Channel, payload.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    video = Video(**payload.model_dump())
    db.add(video)
    db.commit()
    db.refresh(video)
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
    return videos


@router.get("", response_model=list[VideoRead])
def list_videos(
    channel_id: int | None = None,
    status: VideoStatus | None = None,
    publish_status: str | None = None,
    approved: bool | None = None,
    search: str | None = None,
    db: Session = Depends(get_db)
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
                Video.notes.ilike(search_term)
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
            
    videos = []
    for item in payload.videos:
        video = Video(
            channel_id=payload.channel_id,
            title=item.title.strip(),
            niche=item.niche,
            target_audience=item.target_audience,
            angle=item.angle,
            notes=item.notes,
            status=VideoStatus.idea,
            approved=False
        )
        db.add(video)
        videos.append(video)
        
    db.commit()
    for v in videos:
        db.refresh(v)
    return videos


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
    return video


@router.delete("/{video_id}")
def delete_video(video_id: int, db: Session = Depends(get_db)):
    video = get_video_or_404(db, video_id)
    db.delete(video)
    db.commit()
    return {"ok": True}


@router.patch("/{video_id}/publishing", response_model=VideoRead)
def update_video_publishing(video_id: int, payload: VideoPublishUpdate, db: Session = Depends(get_db)) -> Video:
    video = get_video_or_404(db, video_id)
    update_data = payload.model_dump(exclude_unset=True)

    if "publish_status" in update_data:
        new_status = update_data["publish_status"]
        if new_status in ("scheduled", "ready") and not video.approved:
            raise HTTPException(status_code=400, detail="Cannot set publish status to ready or scheduled for an unapproved video")

    for key, value in update_data.items():
        setattr(video, key, value)
    
    db.commit()
    db.refresh(video)
    return video


@router.get("/{video_id}/readiness", response_model=VideoReadiness)
def get_video_readiness(video_id: int, db: Session = Depends(get_db)) -> VideoReadiness:
    video = get_video_or_404(db, video_id)
    
    assets_generated = len(video.assets) > 0
    review_approved = video.approved
    
    # Check for package (simple check: if status is packaged, publish_ready, published)
    # Or checking if a manifest asset exists
    package_created = any(a.asset_type == AssetType.package_manifest for a in video.assets)
    
    # Check if youtube metadata is prepared (in PublishRecord or assets)
    # The payload prepares and saves a PublishRecord with platform='youtube'
    from app.models import PublishRecord
    youtube_record = db.scalar(select(PublishRecord).where(PublishRecord.video_id == video.id, PublishRecord.platform == "youtube").limit(1))
    youtube_metadata_prepared = youtube_record is not None
    
    publish_date_set = video.publish_date is not None
    publish_status_ready_or_scheduled = video.publish_status in ("ready", "scheduled")
    
    blocking_reasons = []
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
        
    return VideoReadiness(
        assets_generated=assets_generated,
        review_approved=review_approved,
        package_created=package_created,
        youtube_metadata_prepared=youtube_metadata_prepared,
        publish_date_set=publish_date_set,
        publish_status_ready_or_scheduled=publish_status_ready_or_scheduled,
        blocking_reasons=blocking_reasons
    )



@router.get("/{video_id}/assets", response_model=list[AssetRead])
def list_assets(video_id: int, db: Session = Depends(get_db)) -> list[ContentAsset]:
    get_video_or_404(db, video_id)
    stmt = select(ContentAsset).where(ContentAsset.video_id == video_id).order_by(ContentAsset.created_at.desc())
    return list(db.scalars(stmt))


@router.post("/{video_id}/generate", response_model=list[AssetRead])
def generate_assets(video_id: int, payload: GenerateRequest, db: Session = Depends(get_db)) -> list[ContentAsset]:
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
    db.commit()
    for asset in assets:
        db.refresh(asset)
    return assets


@router.patch("/{video_id}/assets/{asset_type}", response_model=AssetRead)
def update_asset(video_id: int, asset_type: AssetType, payload: AssetUpdate, db: Session = Depends(get_db)) -> ContentAsset:
    video = get_video_or_404(db, video_id)
    stmt = select(ContentAsset).where(ContentAsset.video_id == video.id, ContentAsset.asset_type == asset_type).order_by(ContentAsset.created_at.desc()).limit(1)
    asset = db.scalar(stmt)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    
    asset.body = payload.body
    asset.version += 1
    
    video.status = VideoStatus.needs_review
    video.approved = False
    
    db.commit()
    db.refresh(asset)
    return asset


@router.post("/{video_id}/assets/{asset_type}/regenerate", response_model=AssetRead)
def regenerate_asset(video_id: int, asset_type: AssetType, db: Session = Depends(get_db)) -> ContentAsset:
    video = get_video_or_404(db, video_id)
    stmt = select(ContentAsset).where(ContentAsset.video_id == video.id, ContentAsset.asset_type == asset_type).order_by(ContentAsset.created_at.desc()).limit(1)
    asset = db.scalar(stmt)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    new_body = ""
    if asset_type == AssetType.brief:
        new_body = build_brief(video)
    elif asset_type == AssetType.script:
        new_body = build_script(video)
    elif asset_type == AssetType.shorts:
        new_body = build_shorts(video)
    elif asset_type == AssetType.description:
        new_body = build_description(video)
    elif asset_type == AssetType.thumbnail_prompt:
        new_body = build_thumbnail_prompt(video)
    elif asset_type == AssetType.youtube_metadata:
        new_body = build_youtube_metadata(video)
    elif asset_type == AssetType.package_manifest:
        script_stmt = select(ContentAsset).where(ContentAsset.video_id == video.id, ContentAsset.asset_type == AssetType.script).order_by(ContentAsset.created_at.desc()).limit(1)
        script_asset = db.scalar(script_stmt)
        script_body = script_asset.body if script_asset else build_script(video)
        from app.services.compliance import build_review_checklist
        new_body = build_review_checklist(script_body)
    else:
        raise HTTPException(status_code=400, detail="Cannot regenerate this asset type")

    asset.body = new_body
    asset.version += 1
    
    video.status = VideoStatus.needs_review
    video.approved = False
    
    db.commit()
    db.refresh(asset)
    return asset


@router.post("/{video_id}/review", response_model=ReviewRead)
def review_video(video_id: int, payload: ReviewCreate, db: Session = Depends(get_db)) -> Review:
    video = get_video_or_404(db, video_id)
    review = Review(video_id=video.id, **payload.model_dump())
    db.add(review)
    video.approved = payload.passed
    video.status = VideoStatus.approved if payload.passed else VideoStatus.rejected
    db.commit()
    db.refresh(review)
    return review


@router.post("/{video_id}/package", response_model=PackageResponse)
def package_video(video_id: int, db: Session = Depends(get_db)) -> PackageResponse:
    video = get_video_or_404(db, video_id)
    try:
        package_dir, manifest_asset = build_video_package(db, video)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PackageResponse(video_id=video.id, package_dir=str(package_dir), manifest_asset_id=manifest_asset.id)
