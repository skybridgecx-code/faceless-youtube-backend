from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import AssetType, ContentType, OpportunityReviewStatus, ProducerConfidenceLabel, VideoStatus


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
    assigned_agent_id: int | None = None
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


class OpportunityCreate(BaseModel):
    channel_id: int = 1
    topic: str
    niche_lane: str
    audience: str
    monetization_path: str
    notes: str | None = None


class OpportunityScoreBreakdown(BaseModel):
    search_demand: int = Field(ge=1, le=5)
    buyer_intent: int = Field(ge=1, le=5)
    affiliate_potential: int = Field(ge=1, le=5)
    sponsorship_potential: int = Field(ge=1, le=5)
    production_difficulty: int = Field(ge=1, le=5)
    compliance_risk: int = Field(ge=1, le=5)
    trend_freshness: int = Field(ge=1, le=5)
    product_connection: int = Field(ge=1, le=5)
    total_score: int


class OpportunityRead(BaseModel):
    id: int
    channel_id: int
    assigned_agent_id: int | None = None
    topic: str
    niche_lane: str
    audience: str
    monetization_path: str
    notes: str | None = None
    score: OpportunityScoreBreakdown
    expected_monetization_path: str
    why_make_this: str
    recommended_title: str
    thumbnail_angle: str
    recommended_cta: str
    assigned_agent: str
    compliance_risk_note: str
    review_status: OpportunityReviewStatus = OpportunityReviewStatus.unreviewed
    operator_notes: str | None = None
    rejection_reason: str | None = None
    decision_summary: str | None = None
    reviewed_at: datetime | None = None
    promoted_video_id: int | None = None
    promoted_at: datetime | None = None
    scored_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class OpportunityReviewUpdate(BaseModel):
    review_status: OpportunityReviewStatus
    operator_notes: str | None = None
    rejection_reason: str | None = None
    decision_summary: str | None = None


class ContentAgentRead(BaseModel):
    id: int
    channel_id: int
    name: str
    lane: str
    focus: str
    monetization_focus: str
    compliance_notes: str
    production_rules: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContentAgentUpdate(BaseModel):
    focus: str | None = None
    monetization_focus: str | None = None
    compliance_notes: str | None = None
    production_rules: str | None = None
    is_active: bool | None = None


class ExecutiveProducerRecommendationRead(BaseModel):
    id: int
    selected_opportunity_id: int | None = None
    matched_agent_id: int | None = None
    matched_agent_name: str | None = None
    matched_agent_lane: str | None = None
    matched_agent_focus: str | None = None
    matched_agent_monetization_focus: str | None = None
    matched_agent_compliance_notes: str | None = None
    matched_agent_production_rules: str | None = None
    selected_review_status: OpportunityReviewStatus | None = None
    recommended_topic: str | None = None
    niche_lane: str | None = None
    assigned_agent: str | None = None
    recommended_title: str | None = None
    thumbnail_angle: str | None = None
    recommended_cta: str | None = None
    monetization_path: str | None = None
    why_make_today: str | None = None
    risks_to_review: str | None = None
    operator_checklist: str | None = None
    production_brief: str | None = None
    confidence_label: ProducerConfidenceLabel = ProducerConfidenceLabel.low
    selection_score: int | None = None
    empty_state_message: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CommandCenterAction(BaseModel):
    key: str
    label: str
    reason: str
    target_page: Literal["dashboard", "opportunities", "producer", "content", "assets", "publishing", "compliance", "audit"]
    cta_label: str
    video_id: int | None = None
    opportunity_id: int | None = None


class CommandCenterTaskItem(BaseModel):
    video_id: int
    title: str
    workflow_status: str
    publish_status: str
    reason: str
    target_page: Literal["assets", "publishing", "compliance", "audit", "content"]


class CommandCenterTodayRead(BaseModel):
    best_opportunity: OpportunityRead | None = None
    executive_recommendation: ExecutiveProducerRecommendationRead | None = None
    assigned_agent: ContentAgentRead | None = None
    next_best_action: CommandCenterAction
    operator_checklist: list[str]
    blockers: list[CommandCenterTaskItem]
    needs_preview_review: list[CommandCenterTaskItem]
    needs_compliance_review: list[CommandCenterTaskItem]
    ready_for_packaging: list[CommandCenterTaskItem]
    ready_for_payload: list[CommandCenterTaskItem]
    recent_audit_events: list[AuditEventRead]
    summary_status: Literal["empty", "attention_needed", "on_track"]
    summary_message: str


class OpportunityDailySeedRequest(BaseModel):
    channel_id: int | None = None
    limit: int = Field(default=7, ge=1, le=50)


class OpportunityDailySeedResult(BaseModel):
    date: str
    channel_id: int
    requested_limit: int
    created_count: int
    skipped_duplicates: int
    created_ids: list[int]
    message: str


class OpportunityIntakeStatusRead(BaseModel):
    date: str
    channel_id: int | None = None
    todays_count: int
    potential_seed_count: int
