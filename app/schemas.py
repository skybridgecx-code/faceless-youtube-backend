from datetime import datetime
from typing import Literal
import re

from pydantic import BaseModel, Field, field_validator

from app.models import AssetType, ContentType, OpportunityReviewStatus, ProducerConfidenceLabel, ProductionBriefStatus, VideoStatus


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
    title: str = Field(max_length=200)
    content_type: ContentType = ContentType.long
    pillar: str = "AI call handling"
    target_viewer: str = "Local service business owner"
    pain_point: str = "Missed calls, slow lead follow-up, and scattered customer details"
    demo_idea: str = "AI receptionist and dashboard walkthrough"
    thumbnail_text: str = Field(default="AI BUSINESS SYSTEM", max_length=80)
    niche: str | None = Field(default=None, max_length=100)
    target_audience: str | None = Field(default=None, max_length=200)
    angle: str | None = Field(default=None, max_length=150)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("title", "niche", "target_audience", "angle", "notes", mode="before")
    @classmethod
    def reject_html_payload(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        if re.search(r"<[^>]+>", text) or re.search(r"(?i)<\s*script\b", text):
            raise ValueError("HTML or script tags are not allowed.")
        return text

class VideoUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    niche: str | None = Field(default=None, max_length=100)
    target_audience: str | None = Field(default=None, max_length=200)
    angle: str | None = Field(default=None, max_length=150)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("title", "niche", "target_audience", "angle", "notes", mode="before")
    @classmethod
    def reject_html_payload(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        if re.search(r"<[^>]+>", text) or re.search(r"(?i)<\s*script\b", text):
            raise ValueError("HTML or script tags are not allowed.")
        return text



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
    title: str = Field(max_length=200)
    niche: str | None = Field(default=None, max_length=100)
    target_audience: str | None = Field(default=None, max_length=200)
    angle: str | None = Field(default=None, max_length=150)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("title", "niche", "target_audience", "angle", "notes", mode="before")
    @classmethod
    def reject_html_payload(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        if re.search(r"<[^>]+>", text) or re.search(r"(?i)<\s*script\b", text):
            raise ValueError("HTML or script tags are not allowed.")
        return text


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


class ResearchRunRequest(BaseModel):
    niche_lane: str
    query: str
    assigned_agent_id: int | None = None
    max_results: int = Field(default=10, ge=1, le=25)


class ResearchSourceVideoRead(BaseModel):
    id: int
    run_id: int
    youtube_video_id: str
    youtube_channel_id: str
    title: str
    channel_title: str
    description_snippet: str
    published_at: datetime | None = None
    duration: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    thumbnail_url: str | None = None
    position: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ResearchSourceChannelRead(BaseModel):
    id: int
    run_id: int
    youtube_channel_id: str
    title: str
    description_snippet: str
    subscriber_count: int | None = None
    video_count: int | None = None
    view_count: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ResearchPatternRead(BaseModel):
    id: int
    run_id: int
    pattern_type: str
    label: str
    details: str
    signal_strength: int = Field(ge=1, le=5)
    created_at: datetime

    model_config = {"from_attributes": True}


class ResearchStrategyRead(BaseModel):
    id: int
    run_id: int
    assigned_agent_id: int | None = None
    recommended_agent_name: str | None = None
    niche_lane: str
    query: str
    trend_thesis: str
    winning_patterns: str
    original_video_angles: str
    recommended_topics: list[str]
    title_directions: str
    thumbnail_directions: str
    hook_directions: str
    monetization_path: str
    differentiation_strategy: str
    what_not_to_copy: str
    compliance_risks: str
    recommended_next_action: str
    top_pattern: str | None = None
    created_at: datetime
    updated_at: datetime


class ResearchSummaryRead(BaseModel):
    run_id: int
    strategy_id: int
    niche_lane: str
    query: str
    trend_thesis: str
    top_pattern: str | None = None
    recommended_next_action: str
    recommended_agent_name: str | None = None
    created_at: datetime


class ResearchRunRead(BaseModel):
    id: int
    channel_id: int | None = None
    assigned_agent_id: int | None = None
    niche_lane: str
    query: str
    max_results: int
    status: str
    setup_required: bool = False
    setup_message: str | None = None
    source_video_count: int
    source_channel_count: int
    created_at: datetime
    updated_at: datetime
    strategy: ResearchSummaryRead | None = None

    model_config = {"from_attributes": True}


class ResearchRunDetailRead(ResearchRunRead):
    source_videos: list[ResearchSourceVideoRead]
    source_channels: list[ResearchSourceChannelRead]
    patterns: list[ResearchPatternRead]
    strategy_detail: ResearchStrategyRead | None = None


class ResearchCreateOpportunitiesResult(BaseModel):
    run_id: int
    strategy_id: int
    requested_topics: int
    created_count: int
    skipped_duplicates: int
    created_ids: list[int]
    message: str


class CommandCenterAction(BaseModel):
    key: str
    label: str
    reason: str
    target_page: Literal["dashboard", "opportunities", "producer", "briefs", "content", "assets", "publishing", "compliance", "audit"]
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
    latest_research_strategy: ResearchSummaryRead | None = None
    assigned_agent: ContentAgentRead | None = None
    next_best_action: CommandCenterAction
    operator_checklist: list[str]
    blockers: list[CommandCenterTaskItem]
    needs_preview_review: list[CommandCenterTaskItem]
    needs_compliance_review: list[CommandCenterTaskItem]
    ready_for_packaging: list[CommandCenterTaskItem]
    ready_for_payload: list[CommandCenterTaskItem]
    briefs_needing_review: list["CommandCenterBriefItem"]
    approved_briefs_ready_to_promote: list["CommandCenterBriefItem"]
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


class ProductionBriefRead(BaseModel):
    id: int
    opportunity_id: int
    assigned_agent_id: int | None = None
    promoted_video_id: int | None = None
    status: ProductionBriefStatus = ProductionBriefStatus.draft
    topic: str
    niche_lane: str
    target_audience: str
    monetization_path: str
    title: str
    thumbnail_angle: str
    hook: str
    outline: str
    script_plan: str
    b_roll_plan: str
    voiceover_style: str
    cta: str
    compliance_notes: str
    claims_to_verify: str
    operator_review_notes: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProductionBriefReviewUpdate(BaseModel):
    status: Literal["draft", "needs_revision", "approved"]
    operator_review_notes: str | None = None


class CommandCenterBriefItem(BaseModel):
    brief_id: int
    opportunity_id: int
    status: ProductionBriefStatus
    title: str
    topic: str
    agent_name: str | None = None
    target_page: Literal["briefs"] = "briefs"


class ProductionBriefCreateResult(BaseModel):
    brief: ProductionBriefRead
    message: str


class VisualAssetPromptRead(BaseModel):
    id: int
    plan_id: int
    scene_id: int | None = None
    prompt_type: str
    label: str
    prompt_text: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VisualSceneRead(BaseModel):
    id: int
    plan_id: int
    scene_number: int
    scene_title: str
    narrative_beat: str
    on_screen_text: str
    image_prompt: str
    animation_prompt: str
    b_roll_prompt: str
    dashboard_demo_prompt: str
    safety_notes: str
    created_at: datetime
    updated_at: datetime
    prompts: list[VisualAssetPromptRead] = []

    model_config = {"from_attributes": True}


class VisualAssetPlanRead(BaseModel):
    id: int
    video_id: int | None = None
    brief_id: int | None = None
    source_type: str
    status: str
    title: str
    thumbnail_prompt: str
    thumbnail_text: str
    motion_style: str
    color_direction: str
    plan_notes: str | None = None
    safety_notes: str
    ready_marked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    scenes: list[VisualSceneRead] = []
    prompts: list[VisualAssetPromptRead] = []

    model_config = {"from_attributes": True}


class VisualAssetPlanUpdate(BaseModel):
    status: str | None = Field(default=None, max_length=40)
    thumbnail_prompt: str | None = None
    thumbnail_text: str | None = Field(default=None, max_length=120)
    motion_style: str | None = Field(default=None, max_length=240)
    color_direction: str | None = Field(default=None, max_length=240)
    plan_notes: str | None = None
    safety_notes: str | None = None

    @field_validator(
        "status",
        "thumbnail_prompt",
        "thumbnail_text",
        "motion_style",
        "color_direction",
        "plan_notes",
        "safety_notes",
        mode="before",
    )
    @classmethod
    def reject_html_payload(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        if re.search(r"<[^>]+>", text) or re.search(r"(?i)<\s*script\b", text):
            raise ValueError("HTML or script tags are not allowed.")
        return text


class VisualSceneUpdate(BaseModel):
    scene_title: str | None = Field(default=None, max_length=200)
    narrative_beat: str | None = None
    on_screen_text: str | None = Field(default=None, max_length=180)
    image_prompt: str | None = None
    animation_prompt: str | None = None
    b_roll_prompt: str | None = None
    dashboard_demo_prompt: str | None = None
    safety_notes: str | None = None

    @field_validator(
        "scene_title",
        "narrative_beat",
        "on_screen_text",
        "image_prompt",
        "animation_prompt",
        "b_roll_prompt",
        "dashboard_demo_prompt",
        "safety_notes",
        mode="before",
    )
    @classmethod
    def reject_html_payload(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        if re.search(r"<[^>]+>", text) or re.search(r"(?i)<\s*script\b", text):
            raise ValueError("HTML or script tags are not allowed.")
        return text


class PipelineOpportunityItem(BaseModel):
    id: int
    topic: str
    niche_lane: str
    review_status: OpportunityReviewStatus
    total_score: int
    assigned_agent_id: int | None = None
    assigned_agent: str | None = None
    created_at: datetime


class PipelineRecommendationItem(BaseModel):
    recommendation_id: int
    selected_opportunity_id: int | None = None
    recommended_topic: str | None = None
    niche_lane: str | None = None
    assigned_agent: str | None = None
    confidence_label: ProducerConfidenceLabel
    created_at: datetime


class PipelineBriefItem(BaseModel):
    brief_id: int
    opportunity_id: int
    title: str
    topic: str
    status: ProductionBriefStatus
    assigned_agent_id: int | None = None
    assigned_agent: str | None = None
    updated_at: datetime


class PipelineVideoItem(BaseModel):
    video_id: int
    title: str
    workflow_status: VideoStatus
    publish_status: str
    approved: bool
    preview_rendered: bool
    preview_reviewed: bool
    updated_at: datetime
    reason: str | None = None


class PipelineSummaryCounts(BaseModel):
    opportunities_to_review: int
    producer_recommendations: int
    briefs_to_review: int
    approved_briefs_ready_to_promote: int
    videos_needing_assets: int
    videos_missing_visual_plans: int
    videos_needing_preview: int
    videos_needing_preview_review: int
    videos_needing_compliance: int
    videos_needing_manual_approval: int
    videos_ready_to_package: int
    videos_ready_for_payload: int
    completed_payloads: int


class PipelineNextStep(BaseModel):
    key: str
    label: str
    reason: str
    target_page: Literal["opportunities", "producer", "briefs", "content", "assets", "publishing", "compliance", "audit"]
    video_id: int | None = None
    opportunity_id: int | None = None
    brief_id: int | None = None


class DailyPipelineRead(BaseModel):
    opportunities_to_review: list[PipelineOpportunityItem]
    producer_recommendations: list[PipelineRecommendationItem]
    briefs_to_review: list[PipelineBriefItem]
    approved_briefs_ready_to_promote: list[PipelineBriefItem]
    videos_needing_assets: list[PipelineVideoItem]
    videos_missing_visual_plans: list[PipelineVideoItem]
    videos_needing_preview: list[PipelineVideoItem]
    videos_needing_preview_review: list[PipelineVideoItem]
    videos_needing_compliance: list[PipelineVideoItem]
    videos_needing_manual_approval: list[PipelineVideoItem]
    videos_ready_to_package: list[PipelineVideoItem]
    videos_ready_for_payload: list[PipelineVideoItem]
    completed_payloads: list[PipelineVideoItem]
    summary_counts: PipelineSummaryCounts
    next_step: PipelineNextStep
