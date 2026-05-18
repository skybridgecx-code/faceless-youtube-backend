from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers.videos import get_video_or_404
from app.schemas import FinalProductionExportResponse, FinalProductionStatus
from app.services.final_renderer import render_final_video_with_ffmpeg
from app.services.final_production import (
    assess_final_production,
    assess_preexport_readiness,
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
            manifest_path=None,
            render_plan_path=None,
            render_command_path=None,
            renderer="ffmpeg",
            blockers=pre_blockers,
            status="blocked",
        )
    render_result = render_final_video_with_ffmpeg(video, db)
    if render_result.get("status") != "rendered":
        return FinalProductionExportResponse(
            video_id=video_id,
            production_ready=False,
            final_export_path=None,
            manifest_path=render_result.get("manifest_path"),
            render_plan_path=render_result.get("render_plan_path"),
            render_command_path=render_result.get("render_command_path"),
            renderer=str(render_result.get("renderer") or "ffmpeg"),
            blockers=list(render_result.get("blockers") or ["Final render failed"]),
            status="blocked",
        )

    final_status = assess_final_production(video, db)
    return FinalProductionExportResponse(
        video_id=video_id,
        production_ready=final_status.production_ready,
        final_export_path=final_status.final_export_path,
        manifest_path=render_result.get("manifest_path"),
        render_plan_path=render_result.get("render_plan_path"),
        render_command_path=render_result.get("render_command_path"),
        renderer=str(render_result.get("renderer") or "ffmpeg"),
        blockers=final_status.blockers,
        status="production_ready" if final_status.production_ready else "blocked",
    )
