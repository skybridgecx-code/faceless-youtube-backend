from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from app.config import get_settings
from app.db import init_db
from app.routers import channels, publish, videos

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "faceless-youtube-backend",
        "youtube_uploads_enabled": settings.enable_youtube_uploads,
        "review_required": settings.require_human_review,
    }

from fastapi import Depends
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.db import get_db
from app.models import Video
from app.schemas import VideoRead, PipelineSummary, PipelineActionItem
from app.models import VideoStatus

app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(publish.router)

@app.get("/calendar", response_model=list[VideoRead])
def get_calendar(db: Session = Depends(get_db)):
    # Return videos grouped or sorted by publish_date, unscheduled at the end
    stmt = select(Video).order_by(Video.publish_date.asc().nulls_last())
    return list(db.scalars(stmt))


@app.get("/pipeline/summary", response_model=PipelineSummary)
def get_pipeline_summary(db: Session = Depends(get_db)):
    videos = list(db.scalars(select(Video)))
    
    total_videos = len(videos)
    status_counts = {}
    publish_status_counts = {}
    
    for v in videos:
        status_counts[v.status.value] = status_counts.get(v.status.value, 0) + 1
        publish_status_counts[v.publish_status] = publish_status_counts.get(v.publish_status, 0) + 1
        
    action_queue = []
    
    from app.models import PublishRecord
    
    for v in videos:
        if v.publish_status == "blocked":
            action_queue.append(PipelineActionItem(
                video_id=v.id, title=v.title, workflow_status=v.status.value, 
                publish_status=v.publish_status, reason="Video is blocked", 
                suggested_next_action="Resolve blocker or delete video"
            ))
            continue
            
        if v.status == VideoStatus.needs_review:
            action_queue.append(PipelineActionItem(
                video_id=v.id, title=v.title, workflow_status=v.status.value, 
                publish_status=v.publish_status, reason="Assets generated but need review", 
                suggested_next_action="Review and approve"
            ))
            continue
            
        if v.approved and v.status not in (VideoStatus.packaged, VideoStatus.publish_ready, VideoStatus.published):
            action_queue.append(PipelineActionItem(
                video_id=v.id, title=v.title, workflow_status=v.status.value, 
                publish_status=v.publish_status, reason="Approved but not packaged", 
                suggested_next_action="Package assets"
            ))
            continue
            
        if v.status == VideoStatus.packaged:
            action_queue.append(PipelineActionItem(
                video_id=v.id, title=v.title, workflow_status=v.status.value, 
                publish_status=v.publish_status, reason="Packaged but payload not ready", 
                suggested_next_action="Prepare YouTube Payload"
            ))
            continue
            
        if v.publish_status in ("scheduled", "ready"):
            has_package = any(a.asset_type.value == "package_manifest" for a in v.assets)
            has_metadata = db.scalar(select(PublishRecord).where(PublishRecord.video_id == v.id, PublishRecord.platform == "youtube").limit(1)) is not None
            if not has_package or not has_metadata or not v.publish_date:
                action_queue.append(PipelineActionItem(
                    video_id=v.id, title=v.title, workflow_status=v.status.value, 
                    publish_status=v.publish_status, reason="Scheduled/Ready but missing prerequisites", 
                    suggested_next_action="Fix readiness issues"
                ))
                continue
                
    urgency_map = {
        "Video is blocked": 0,
        "Assets generated but need review": 1,
        "Generated but not approved": 1,
        "Approved but not packaged": 2,
        "Packaged but payload not ready": 3,
        "Scheduled/Ready but missing prerequisites": 4
    }
    
    action_queue.sort(key=lambda x: urgency_map.get(x.reason, 99))

    return PipelineSummary(
        total_videos=total_videos,
        status_counts=status_counts,
        publish_status_counts=publish_status_counts,
        action_queue=action_queue
    )


static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/app", include_in_schema=False)
def serve_app():
    return FileResponse(os.path.join(static_dir, "index.html"))
