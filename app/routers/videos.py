from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AssetType, Channel, ContentAsset, Review, Video, VideoStatus
from app.schemas import (
    AssetRead,
    BulkIdeaRequest,
    GenerateRequest,
    PackageResponse,
    ReviewCreate,
    ReviewRead,
    VideoCreate,
    VideoRead,
    VideoUpdate,
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
def list_videos(channel_id: int | None = None, status: VideoStatus | None = None, db: Session = Depends(get_db)) -> list[Video]:
    stmt = select(Video).order_by(Video.created_at.desc())
    if channel_id is not None:
        stmt = stmt.where(Video.channel_id == channel_id)
    if status is not None:
        stmt = stmt.where(Video.status == status)
    return list(db.scalars(stmt))


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
