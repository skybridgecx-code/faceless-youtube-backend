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


app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(publish.router)

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/app", include_in_schema=False)
def serve_app():
    return FileResponse(os.path.join(static_dir, "index.html"))
