from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers.videos import get_video_or_404
from app.schemas import FinalProductionExportResponse, FinalProductionStatus
from app.services.final_production import assess_final_production

router = APIRouter(prefix="/videos", tags=["final-production"])


@router.get("/{video_id}/final-production/status", response_model=FinalProductionStatus)
def get_final_production_status(video_id: int, db: Session = Depends(get_db)) -> FinalProductionStatus:
    video = get_video_or_404(db, video_id)
    return assess_final_production(video, db)


@router.post("/{video_id}/final-production/export", response_model=FinalProductionExportResponse)
def run_final_production_export(video_id: int, db: Session = Depends(get_db)) -> FinalProductionExportResponse:
    video = get_video_or_404(db, video_id)
    status = assess_final_production(video, db)
    if status.blockers:
        return FinalProductionExportResponse(
            video_id=video_id,
            production_ready=False,
            final_export_path=None,
            blockers=status.blockers,
            status="blocked",
        )
    return FinalProductionExportResponse(
        video_id=video_id,
        production_ready=status.production_ready,
        final_export_path=status.final_export_path,
        blockers=[],
        status="production_ready" if status.production_ready else "blocked",
    )
