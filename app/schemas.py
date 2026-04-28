from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import AssetType, ContentType, VideoStatus


class ChannelCreate(BaseModel):
    name: str = "Local AI Operator"
    niche: str = "AI automation for local service businesses"
    audience: str = "Roofers, HVAC companies, plumbers, restaurants, med spas, contractors, and local operators"
    brand_voice: str = "Direct, practical, slightly dramatic, not hypey"
    visual_style: str = "Dark premium dashboard style with cream typography and restrained orange/gold accents"


class ChannelRead(ChannelCreate):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class VideoCreate(BaseModel):
    channel_id: int
    title: str
    content_type: ContentType = ContentType.long
    pillar: str = "AI call handling"
    target_viewer: str = "Local service business owner"
    pain_point: str = "Missed calls, slow lead follow-up, and scattered customer details"
    demo_idea: str = "AI receptionist and dashboard walkthrough"
    thumbnail_text: str = Field(default="AI BUSINESS SYSTEM", max_length=80)
    niche: str | None = None
    target_audience: str | None = None
    angle: str | None = None
    notes: str | None = None

class VideoUpdate(BaseModel):
    title: str | None = None
    niche: str | None = None
    target_audience: str | None = None
    angle: str | None = None
    notes: str | None = None



class VideoRead(BaseModel):
    id: int
    channel_id: int
    title: str
    content_type: ContentType
    pillar: str
    target_viewer: str
    pain_point: str
    demo_idea: str
    thumbnail_text: str
    niche: str | None = None
    target_audience: str | None = None
    angle: str | None = None
    notes: str | None = None
    status: VideoStatus
    approved: bool
    publish_date: datetime | None = None
    publish_status: str
    publish_notes: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VideoPublishUpdate(BaseModel):
    publish_date: datetime | None = None
    publish_status: str | None = None
    publish_notes: str | None = None


class VideoReadiness(BaseModel):
    assets_generated: bool
    review_approved: bool
    package_created: bool
    youtube_metadata_prepared: bool
    publish_date_set: bool
    publish_status_ready_or_scheduled: bool
    blocking_reasons: list[str]

    model_config = {"from_attributes": True}


class BulkIdeaRequest(BaseModel):
    channel_id: int
    count: int = Field(default=15, ge=1, le=50)


class GenerateRequest(BaseModel):
    stage: Literal["brief", "script", "shorts", "description", "thumbnail_prompt", "metadata", "all"] = "all"


class AssetRead(BaseModel):
    id: int
    video_id: int
    asset_type: AssetType
    body: str
    version: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AssetUpdate(BaseModel):
    body: str


class ReviewCreate(BaseModel):
    passed: bool
    reviewer: str = "operator"
    notes: str = ""


class ReviewRead(BaseModel):
    id: int
    video_id: int
    passed: bool
    reviewer: str
    notes: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PackageResponse(BaseModel):
    video_id: int
    package_dir: str
    manifest_asset_id: int


class YouTubePayloadResponse(BaseModel):
    video_id: int
    title: str
    description: str
    tags: list[str]
    category_id: str
    privacy_status: str
    made_for_kids: bool
    review_required: bool


class MarkPublishedRequest(BaseModel):
    external_id: str
    metadata_body: str = ""
