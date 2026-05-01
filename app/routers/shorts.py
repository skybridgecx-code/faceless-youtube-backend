from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AssetType, Channel, ContentAsset, ContentType, Video, VideoStatus
from app.schemas import (
    ShortsBatchQueueItem,
    ShortsBatchRequest,
    ShortsBatchResponse,
    ShortsBatchVideoSummary,
)
from app.services.audit import log_audit_event
from app.services.compliance import run_compliance_checks
from app.services.content_engine import build_all_assets, generate_video_ideas
from app.routers.videos import build_readiness

router = APIRouter(prefix="/shorts", tags=["shorts"])


def _primary_channel(db: Session) -> Channel:
    channel = db.scalar(select(Channel).order_by(Channel.created_at.asc(), Channel.id.asc()).limit(1))
    if channel is not None:
        return channel
    settings = get_settings()
    channel = Channel(
        name=settings.channel_default_name,
        niche="AI automation for local service businesses",
        audience="Local service operators",
        brand_voice="Direct and practical",
        visual_style="Dark premium dashboard",
    )
    db.add(channel)
    db.flush()
    return channel


def _short_title(base_title: str, topic_seed: str | None, index: int) -> str:
    prefix = (topic_seed or "").strip()
    if prefix:
        value = f"{prefix} Shorts #{index + 1}"
        return value[:240]
    value = base_title.strip()
    if value.lower().startswith("short:"):
        return value[:240]
    return f"Short: {value}"[:240]


@router.post("/batch", response_model=ShortsBatchResponse)
def create_shorts_batch(payload: ShortsBatchRequest, db: Session = Depends(get_db)) -> ShortsBatchResponse:
    requested_count = int(payload.count)
    batch_count = min(requested_count, 10)
    warnings: list[str] = []
    if requested_count > 10:
        warnings.append("Requested count exceeded max per batch. Created 10 shorts candidates.")

    channel = _primary_channel(db)
    ideas = generate_video_ideas(batch_count)

    created: list[Video] = []
    generated_assets_count: dict[int, int] = {}
    for index in range(batch_count):
        idea = ideas[index]
        title = _short_title(idea.get("title", f"Shorts Candidate #{index + 1}"), payload.topic_seed, index)
        video = Video(
            channel_id=channel.id,
            title=title,
            content_type=ContentType.short,
            pillar=(payload.pillar or idea.get("pillar") or "Short-form AI workflow").strip()[:120],
            target_viewer=(payload.target_viewer or idea.get("target_viewer") or "Local service business owner").strip()[:240],
            pain_point=(idea.get("pain_point") or "Operational friction in local business workflows").strip()[:500],
            demo_idea=(idea.get("demo_idea") or "Short-form educational workflow").strip()[:500],
            thumbnail_text=(idea.get("thumbnail_text") or "SHORTS WORKFLOW").strip()[:80],
            status=VideoStatus.idea,
            approved=False,
            preview_reviewed=False,
        )
        db.add(video)
        db.flush()
        created.append(video)

        generated_assets_count[video.id] = 0
        if payload.auto_generate_assets:
            try:
                generated = build_all_assets(video)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Asset generation failed for video #{video.id}: {exc}")
                continue

            for item in generated:
                body = str(item.body or "").strip()
                if not body:
                    continue
                db.add(ContentAsset(video_id=video.id, asset_type=AssetType(item.asset_type), body=body))
                generated_assets_count[video.id] += 1
            if generated_assets_count[video.id] > 0:
                video.status = VideoStatus.needs_review
            video.approved = False
            video.preview_reviewed = False
            video.preview_reviewed_at = None

    db.commit()

    for video in created:
        db.refresh(video)
        log_audit_event(
            db,
            "shorts_batch_video_created",
            f"Created shorts candidate: {video.title}",
            video_id=video.id,
            metadata={
                "content_type": video.content_type.value,
                "auto_generate_assets": payload.auto_generate_assets,
                "generated_assets_count": generated_assets_count.get(video.id, 0),
            },
        )

    batch_id = f"shorts_batch_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{created[0].id if created else 0}"
    summaries: list[ShortsBatchVideoSummary] = []
    for video in created:
        readiness = build_readiness(video, db)
        summaries.append(
            ShortsBatchVideoSummary(
                id=video.id,
                title=video.title,
                content_type=video.content_type,
                pillar=video.pillar,
                target_viewer=video.target_viewer,
                workflow_status=video.status,
                approved=bool(video.approved),
                preview_reviewed=bool(video.preview_reviewed),
                generated_assets_count=generated_assets_count.get(video.id, 0),
                next_required_action=readiness.next_required_action,
            )
        )

    next_action = "Run compliance and manual review on generated shorts."
    if not payload.auto_generate_assets:
        next_action = "Generate assets for shorts candidates, then run compliance and manual review."

    return ShortsBatchResponse(
        batch_id=batch_id,
        requested_count=requested_count,
        created_count=len(summaries),
        videos=summaries,
        warnings=warnings,
        next_required_action=next_action,
    )


@router.get("/batch-queue", response_model=list[ShortsBatchQueueItem])
def get_shorts_batch_queue(
    status: VideoStatus | None = None,
    pillar: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[ShortsBatchQueueItem]:
    stmt = select(Video).where(Video.content_type == ContentType.short).order_by(Video.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Video.status == status)
    if pillar:
        stmt = stmt.where(Video.pillar == pillar)

    rows = list(db.scalars(stmt))
    queue: list[ShortsBatchQueueItem] = []
    for video in rows:
        readiness = build_readiness(video, db)
        report = run_compliance_checks(video)
        queue.append(
            ShortsBatchQueueItem(
                video_id=video.id,
                title=video.title,
                content_type=video.content_type,
                pillar=video.pillar,
                target_viewer=video.target_viewer,
                pain_point=video.pain_point,
                workflow_status=video.status,
                approved=bool(video.approved),
                assets_generated=readiness.assets_generated,
                compliance_status=report.overall_status,
                preview_exists=readiness.preview_rendered,
                preview_reviewed=bool(video.preview_reviewed),
                created_at=video.created_at,
                next_required_action=readiness.next_required_action,
            )
        )
    return queue
