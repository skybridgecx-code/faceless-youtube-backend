from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PublishRecord, Video, VideoStatus
from app.schemas import MarkPublishedRequest, YouTubePayloadResponse
from app.services.audit import log_audit_event
from app.services.youtube import prepare_payload

router = APIRouter(prefix="/publish", tags=["publish"])


def get_video_or_404(db: Session, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


@router.post("/{video_id}/prepare-youtube-payload", response_model=YouTubePayloadResponse)
def prepare_youtube_payload(video_id: int, db: Session = Depends(get_db)) -> YouTubePayloadResponse:
    video = get_video_or_404(db, video_id)
    if not video.approved:
        raise HTTPException(status_code=409, detail="Video must pass review before YouTube payload preparation")

    payload = prepare_payload(video)
    record = PublishRecord(video_id=video.id, platform="youtube", metadata_body=payload.as_json(), published=False)
    db.add(record)
    video.status = VideoStatus.publish_ready
    db.commit()
    db.refresh(record)

    log_audit_event(
        db,
        "youtube_payload_prepared",
        f"Prepared safe YouTube payload for: {video.title}",
        video_id=video.id,
        metadata={
            "publish_record_id": record.id,
            "privacy_status": payload.privacy_status,
            "review_required": payload.review_required,
            "made_for_kids": payload.made_for_kids,
        },
    )

    return YouTubePayloadResponse(video_id=video.id, **payload.__dict__)


@router.post("/{video_id}/mark-published")
def mark_published(video_id: int, payload: MarkPublishedRequest, db: Session = Depends(get_db)) -> dict[str, object]:
    video = get_video_or_404(db, video_id)
    if not video.approved:
        raise HTTPException(status_code=409, detail="Video must pass review before marking as published")

    record = PublishRecord(
        video_id=video.id,
        platform="youtube",
        external_id=payload.external_id,
        metadata_body=payload.metadata_body,
        published=True,
    )
    db.add(record)
    video.status = VideoStatus.published
    db.commit()
    db.refresh(record)

    log_audit_event(
        db,
        "publishing_updated",
        f"Marked video as manually published: {video.title}",
        video_id=video.id,
        metadata={"publish_record_id": record.id, "external_id": payload.external_id},
    )

    return {"ok": True, "video_id": video.id, "status": video.status.value, "external_id": payload.external_id}
