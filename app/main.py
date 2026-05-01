from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, init_db
from app.models import AuditEvent, PublishRecord, Video, VideoStatus, VisualAssetPlan, VisualGeneratedAsset, VisualGenerationJob
from app.routers import (
    agents,
    channels,
    command_center,
    executive_producer,
    opportunities,
    performance,
    pipeline,
    production_briefs,
    publish,
    research,
    shorts,
    videos,
    visual_generation,
    visual_assets,
)
from app.security import InMemoryRateLimiter, auth_error_payload, check_internal_api_key, client_ip, is_ai_cost_route, is_public_path, needs_auth
from app.schemas import AuditEventRead, PipelineActionItem, PipelineSummary, VideoRead

settings = get_settings()
rate_limiter = InMemoryRateLimiter()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings.validate_startup()
    init_db()
    yield


app = FastAPI(
    title="Faceless YouTube Content Factory API",
    version="0.1.0",
    description="Backend for original, review-gated faceless YouTube content production.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    path = request.url.path
    method = request.method.upper()

    if is_public_path(path):
        return await call_next(request)

    if needs_auth(path, method) and not check_internal_api_key(settings, request):
        return JSONResponse(status_code=401, content=auth_error_payload())

    limit: int | None = None
    if is_ai_cost_route(path, method):
        limit = settings.rate_limit_ai_per_minute
    elif method in {"POST", "PATCH", "DELETE"}:
        limit = settings.rate_limit_write_per_minute

    if limit is not None and limit > 0:
        rate_scope = path if method in {"POST", "PATCH", "DELETE"} else "read"
        rate_key = f"{client_ip(request)}:{method}:{rate_scope}"
        allowed = rate_limiter.allow(rate_key, limit=limit)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please retry shortly."},
            )

    return await call_next(request)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "faceless-youtube-backend",
        "youtube_uploads_enabled": settings.enable_youtube_uploads,
        "review_required": settings.require_human_review,
    }


app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(publish.router)
app.include_router(opportunities.router)
app.include_router(performance.router)
app.include_router(executive_producer.router)
app.include_router(agents.router)
app.include_router(command_center.router)
app.include_router(production_briefs.router)
app.include_router(pipeline.router)
app.include_router(research.router)
app.include_router(visual_assets.router)
app.include_router(visual_generation.router)
app.include_router(shorts.router)


def has_preview_file(video: Video) -> bool:
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
            return True
    return False


@app.get("/calendar", response_model=list[VideoRead])
def get_calendar(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Video]:
    stmt = (
        select(Video)
        .order_by(Video.publish_date.asc().nulls_last(), Video.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.scalars(stmt))


@app.get("/audit", response_model=list[AuditEventRead])
def get_audit_events(
    video_id: int | None = None,
    event_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[AuditEvent]:
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
    if video_id is not None:
        stmt = stmt.where(AuditEvent.video_id == video_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    return list(db.scalars(stmt))


@app.get("/pipeline/summary", response_model=PipelineSummary)
def get_pipeline_summary(db: Session = Depends(get_db)) -> PipelineSummary:
    all_videos = list(db.scalars(select(Video)))
    visual_plan_video_ids = {
        video_id
        for video_id in db.scalars(select(VisualAssetPlan.video_id).where(VisualAssetPlan.video_id.is_not(None)))
        if video_id is not None
    }
    visual_jobs_pending_video_ids = {
        video_id
        for video_id in db.scalars(
            select(VisualAssetPlan.video_id)
            .join(VisualGenerationJob, VisualGenerationJob.visual_asset_plan_id == VisualAssetPlan.id)
            .where(
                VisualAssetPlan.video_id.is_not(None),
                VisualGenerationJob.status.in_(("queued", "exported")),
            )
        )
        if video_id is not None
    }
    visual_assets_registered_video_ids = {
        video_id
        for video_id in db.scalars(
            select(VisualAssetPlan.video_id)
            .join(VisualGeneratedAsset, VisualGeneratedAsset.visual_asset_plan_id == VisualAssetPlan.id)
            .where(
                VisualAssetPlan.video_id.is_not(None),
                VisualGeneratedAsset.file_exists.is_(True),
            )
        )
        if video_id is not None
    }

    status_counts: dict[str, int] = {}
    publish_status_counts: dict[str, int] = {}

    for video in all_videos:
        status_counts[video.status.value] = status_counts.get(video.status.value, 0) + 1
        publish_status_counts[video.publish_status] = publish_status_counts.get(video.publish_status, 0) + 1

    action_queue: list[PipelineActionItem] = []

    for video in all_videos:
        if video.publish_status == "blocked":
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Video is blocked",
                    suggested_next_action="Resolve blocker or delete video",
                )
            )
            continue

        if video.status == VideoStatus.needs_review:
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Assets generated but need review",
                    suggested_next_action="Review and approve",
                )
            )
            continue

        if video.status in (VideoStatus.drafted, VideoStatus.idea) and len(video.assets) > 0 and not video.approved:
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Generated but not approved",
                    suggested_next_action="Review and approve",
                )
            )
            continue

        preview_exists = has_preview_file(video)
        has_visual_plan = video.id in visual_plan_video_ids
        has_visual_jobs_pending = video.id in visual_jobs_pending_video_ids
        has_visual_assets_registered = video.id in visual_assets_registered_video_ids

        if video.approved and not has_visual_plan:
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Visual plan missing",
                    suggested_next_action="Create visual plan",
                )
            )
            continue

        if video.approved and has_visual_jobs_pending and not has_visual_assets_registered:
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Visual outputs pending",
                    suggested_next_action="Register generated visual files",
                )
            )
            continue

        if video.approved and not preview_exists:
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Preview not available yet",
                    suggested_next_action="Render draft preview",
                )
            )
            continue

        if video.approved and preview_exists and not video.preview_reviewed:
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Preview ready but not reviewed",
                    suggested_next_action="Watch draft preview",
                )
            )
            continue

        if video.approved and video.status not in (VideoStatus.packaged, VideoStatus.publish_ready, VideoStatus.published):
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Approved but not packaged",
                    suggested_next_action="Package assets",
                )
            )
            continue

        if video.status == VideoStatus.packaged:
            action_queue.append(
                PipelineActionItem(
                    video_id=video.id,
                    title=video.title,
                    workflow_status=video.status.value,
                    publish_status=video.publish_status,
                    reason="Packaged but payload not ready",
                    suggested_next_action="Prepare YouTube Payload",
                )
            )
            continue

        if video.publish_status in ("scheduled", "ready"):
            has_package = any(asset.asset_type.value == "package_manifest" for asset in video.assets)
            has_metadata = (
                db.scalar(
                    select(PublishRecord)
                    .where(PublishRecord.video_id == video.id, PublishRecord.platform == "youtube")
                    .limit(1)
                )
                is not None
            )
            if not has_package or not has_metadata or not video.publish_date:
                action_queue.append(
                    PipelineActionItem(
                        video_id=video.id,
                        title=video.title,
                        workflow_status=video.status.value,
                        publish_status=video.publish_status,
                        reason="Scheduled/Ready but missing prerequisites",
                        suggested_next_action="Fix readiness issues",
                    )
                )

    urgency_map = {
        "Video is blocked": 0,
        "Assets generated but need review": 1,
        "Generated but not approved": 1,
        "Visual plan missing": 2,
        "Visual outputs pending": 2,
        "Preview not available yet": 2,
        "Preview ready but not reviewed": 2,
        "Approved but not packaged": 3,
        "Packaged but payload not ready": 4,
        "Scheduled/Ready but missing prerequisites": 5,
    }
    action_queue.sort(key=lambda item: urgency_map.get(item.reason, 99))

    return PipelineSummary(
        total_videos=len(all_videos),
        status_counts=status_counts,
        publish_status_counts=publish_status_counts,
        action_queue=action_queue,
    )


static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/app", include_in_schema=False)
def serve_app() -> FileResponse:
    return FileResponse(os.path.join(static_dir, "index.html"))
