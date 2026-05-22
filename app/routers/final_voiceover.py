from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Literal
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers.videos import get_video_or_404
from app.schemas import (
    FinalVoiceoverDryRunResponse,
    FinalVoiceoverGenerateResponse,
    FinalVoiceoverStatus,
    ProductionVoiceoverReadiness,
)
from app.services.final_voiceover import assess_final_voiceover, generate_final_voiceover
from app.services.final_voiceover_preview import build_final_voiceover_dry_run
from app.services.voiceover_readiness import assess_production_voiceover_readiness

router = APIRouter(prefix="/videos", tags=["final-voiceover"])


class FinalVoiceoverGenerateRequest(BaseModel):
    provider: str = "openai"
    voice: str | None = None
    force: bool = False


class FinalVoiceoverDryRunRequest(BaseModel):
    provider: Literal["openai", "elevenlabs"] = "openai"
    voice: str | None = None
    max_chars: int = 5000


@router.get("/{video_id}/final-voiceover/status", response_model=FinalVoiceoverStatus)
def get_final_voiceover_status(video_id: int, db: Session = Depends(get_db)) -> FinalVoiceoverStatus:
    get_video_or_404(db, video_id)
    return assess_final_voiceover(video_id)


@router.get("/{video_id}/final-voiceover/readiness", response_model=ProductionVoiceoverReadiness)
def get_final_voiceover_readiness(video_id: int, db: Session = Depends(get_db)) -> ProductionVoiceoverReadiness:
    get_video_or_404(db, video_id)
    return assess_production_voiceover_readiness(video_id)


@router.post("/{video_id}/final-voiceover/generate", response_model=FinalVoiceoverGenerateResponse)
def post_generate_final_voiceover(
    video_id: int,
    body: FinalVoiceoverGenerateRequest,
    db: Session = Depends(get_db),
) -> FinalVoiceoverGenerateResponse:
    video = get_video_or_404(db, video_id)
    return generate_final_voiceover(video, db, body.provider, body.voice, body.force)


@router.post("/{video_id}/final-voiceover/dry-run", response_model=FinalVoiceoverDryRunResponse)
def post_final_voiceover_dry_run(
    video_id: int,
    body: FinalVoiceoverDryRunRequest,
    db: Session = Depends(get_db),
) -> FinalVoiceoverDryRunResponse:
    video = get_video_or_404(db, video_id)
    max_chars = min(10000, max(200, int(body.max_chars)))
    return build_final_voiceover_dry_run(
        video,
        provider=body.provider,
        voice=body.voice,
        max_chars=max_chars,
    )
