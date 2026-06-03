from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import PublishRecord, Video, VideoStatus
from app.schemas import MarkPublishedRequest, YouTubePayloadResponse
from app.services.audit import log_audit_event
from app.services.final_production import assess_final_production, final_export_path_for_video
from app.services.youtube import prepare_payload
from app.services.youtube_upload import upload_private_video

router = APIRouter(prefix="/publish", tags=["publish"])


class YouTubeUploadResponse(BaseModel):
    video_id: int
    status: str
    youtube_video_id: str | None = None
    privacy_status: str
    detail: str
    studio_disclosure_reminder: str
    warnings: list[str] = []


def get_video_or_404(db: Session, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def resolve_preview_file(video: Video) -> Path | None:
    settings = get_settings()
    root = (settings.output_path / "previews").resolve()
    expected = (root / str(video.id) / "draft.mp4").resolve()
    candidates = [expected]
    if video.rendered_preview_path:
        configured = Path(video.rendered_preview_path).expanduser()
        if not configured.is_absolute():
            configured = (root / configured).resolve()
        candidates.insert(0, configured)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        if resolved.is_file():
            return resolved
    return None


@router.post("/{video_id}/prepare-youtube-payload", response_model=YouTubePayloadResponse)
def prepare_youtube_payload(video_id: int, db: Session = Depends(get_db)) -> YouTubePayloadResponse:
    video = get_video_or_404(db, video_id)
    if not video.approved:
        raise HTTPException(status_code=409, detail="Video must pass review before YouTube payload preparation")
    preview_path = resolve_preview_file(video)
    if preview_path is None:
        raise HTTPException(status_code=409, detail="Draft preview must exist before preparing YouTube payload")
    if not video.preview_reviewed:
        raise HTTPException(status_code=409, detail="Draft preview must be manually reviewed before preparing YouTube payload")

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


@router.post("/{video_id}/youtube/upload", response_model=YouTubeUploadResponse)
def upload_to_youtube_private(video_id: int, db: Session = Depends(get_db)) -> YouTubeUploadResponse:
    """Upload a finished video to YouTube as PRIVATE for final human review.

    Strictly gated: the video must have passed content review, cleared the final
    approval gate, and have a non-empty final export. The upload is always
    private — the operator flips it to public in Studio after adding the AI
    disclosure. Disabled and credential-free environments degrade to a
    structured ``setup_required`` result rather than an error.
    """
    video = get_video_or_404(db, video_id)
    settings = get_settings()

    if not video.approved:
        raise HTTPException(status_code=409, detail="Video must pass review before upload.")
    if (video.final_approval_status or "").strip().lower() != "approved":
        raise HTTPException(status_code=409, detail="Video must clear the final approval gate before upload.")

    status = assess_final_production(video, db)
    if not status.production_ready:
        raise HTTPException(
            status_code=409,
            detail="Final production is not ready: " + "; ".join(status.blockers or ["unknown blocker"]),
        )

    payload = prepare_payload(video)
    result = upload_private_video(
        video_file_path=final_export_path_for_video(video.id),
        payload=payload,
        settings=settings,
    )

    if result.status == "uploaded":
        record = PublishRecord(
            video_id=video.id,
            platform="youtube",
            external_id=result.video_id,
            metadata_body=payload.as_json(),
            published=False,  # private upload — not yet public
        )
        db.add(record)
        db.commit()
        db.refresh(record)

    log_audit_event(
        db,
        "youtube_private_upload",
        f"YouTube private upload attempt for: {video.title} (status={result.status})",
        video_id=video.id,
        metadata={
            "status": result.status,
            "youtube_video_id": result.video_id,
            "privacy_status": result.privacy_status,
        },
    )

    return YouTubeUploadResponse(
        video_id=video.id,
        status=result.status,
        youtube_video_id=result.video_id,
        privacy_status=result.privacy_status,
        detail=result.detail,
        studio_disclosure_reminder=result.studio_disclosure_reminder,
        warnings=result.warnings,
    )


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
