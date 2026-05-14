from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers.videos import get_video_or_404
from app.schemas import FinalProductionExportResponse, FinalProductionStatus
from app.services.final_production import (
    assess_final_production,
    assess_preexport_readiness,
    build_local_export_artifacts,
)

router = APIRouter(prefix="/videos", tags=["final-production"])


@router.get("/{video_id}/final-production/status", response_model=FinalProductionStatus)
def get_final_production_status(video_id: int, db: Session = Depends(get_db)) -> FinalProductionStatus:
    video = get_video_or_404(db, video_id)
    return assess_final_production(video, db)


@router.post("/{video_id}/final-production/export", response_model=FinalProductionExportResponse)
def run_final_production_export(video_id: int, db: Session = Depends(get_db)) -> FinalProductionExportResponse:
    video = get_video_or_404(db, video_id)
    pre_ready, pre_blockers = assess_preexport_readiness(video, db)
    if not pre_ready:
        return FinalProductionExportResponse(
            video_id=video_id,
            production_ready=False,
            final_export_path=None,
            blockers=pre_blockers,
            status="blocked",
        )
    artifacts = build_local_export_artifacts(video, db)
    return FinalProductionExportResponse(
        video_id=video_id,
        production_ready=True,
        final_export_path=artifacts["final_export_path"],
        blockers=[],
        status="production_ready",
        final_video_stub_path=artifacts["final_video_stub_path"],
        manifest_path=artifacts["manifest_path"],
    )
