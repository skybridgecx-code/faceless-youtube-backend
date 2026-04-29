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
    rendered_preview_path: str | None = None
    preview_rendered_at: datetime | None = None
    preview_reviewed: bool = False
    preview_reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VideoPublishUpdate(BaseModel):
    publish_date: datetime | None = None
    publish_status: str | None = None
    publish_notes: str | None = None


class VideoReadiness(BaseModel):
    assets_generated: bool
    preview_rendered: bool
    preview_reviewed: bool
    review_approved: bool
    package_created: bool
    youtube_metadata_prepared: bool
    publish_date_set: bool
    publish_status_ready_or_scheduled: bool
    blocking_reasons: list[str]
    compliance_status: Literal["pass", "warning", "blocked", "untested"] = "untested"
    compliance_blockers_count: int = 0
    compliance_warnings_count: int = 0

    model_config = {"from_attributes": True}


class BulkIdeaRequest(BaseModel):
    channel_id: int
    count: int = Field(default=15, ge=1, le=50)


class VideoBatchItem(BaseModel):
    title: str
    niche: str | None = None
    target_audience: str | None = None
    angle: str | None = None
    notes: str | None = None


class VideoBatchCreate(BaseModel):
    channel_id: int = 1
    videos: list[VideoBatchItem]


class PipelineActionItem(BaseModel):
    video_id: int
    title: str
    workflow_status: str
    publish_status: str
    reason: str
    suggested_next_action: str


class PipelineSummary(BaseModel):
    total_videos: int
    status_counts: dict[str, int]
    publish_status_counts: dict[str, int]
    action_queue: list[PipelineActionItem]


class ComplianceCheck(BaseModel):
    id: str
    label: str
    status: Literal["pass", "warning", "blocked"]
    detail: str
    asset_type: str | None = None
    suggested_fix: str | None = None


class ComplianceReport(BaseModel):
    video_id: int
    title: str
    overall_status: Literal["pass", "warning", "blocked"]
    checks: list[ComplianceCheck]


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


class PreviewStatus(BaseModel):
    video_id: int
    title: str
    preview_exists: bool
    preview_url: str | None = None
    preview_path: str | None = None
    expected_path: str
    preview_rendered_at: datetime | None = None
    preview_reviewed: bool
    preview_reviewed_at: datetime | None = None
    audio_generated: bool = False
    voiceover_path: str | None = None
    silent_reason: str | None = None
    duration_seconds: float | None = None
    tts_provider: str | None = None
    tts_voice: str | None = None
    tts_model: str | None = None


class PreviewReviewUpdate(BaseModel):
    reviewed: bool = True


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


class AuditEventRead(BaseModel):
    id: int
    video_id: int | None = None
    event_type: str
    message: str
    metadata_json: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AssetPreview(BaseModel):
    id: int
    asset_type: AssetType
    version: int
    created_at: datetime
    body_length: int
    body_preview: str


class OperatorExport(BaseModel):
    video: VideoRead
    workflow_status: str
    publishing_plan: dict[str, object | None]
    readiness: VideoReadiness
    compliance_summary: dict[str, int | str]
    assets: list[AssetPreview]
    audit_events: list[AuditEventRead]
    package_dir: str | None = None
    youtube_payload_readiness: dict[str, object]
