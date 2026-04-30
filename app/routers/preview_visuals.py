from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Video
from app.services.preview_visuals import build_preview_visual_manifest

router = APIRouter(prefix="/preview-visuals", tags=["preview-visuals"])


@router.get("/videos/{video_id}")
def get_video_preview_visuals(video_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return build_preview_visual_manifest(db, video)
