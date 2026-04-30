from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class VideoStatus(str, Enum):
    idea = "idea"
    drafted = "drafted"
    needs_review = "needs_review"
    approved = "approved"
    packaged = "packaged"
    publish_ready = "publish_ready"
    published = "published"
    rejected = "rejected"


class ContentType(str, Enum):
    long = "long"
    short = "short"


class AssetType(str, Enum):
    brief = "brief"
    script = "script"
    shorts = "shorts"
    description = "description"
    thumbnail_prompt = "thumbnail_prompt"
    youtube_metadata = "youtube_metadata"
    package_manifest = "package_manifest"


class OpportunityReviewStatus(str, Enum):
    unreviewed = "unreviewed"
    shortlisted = "shortlisted"
    rejected = "rejected"
    needs_more_research = "needs_more_research"
    approved_for_video = "approved_for_video"


class ProducerConfidenceLabel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    niche: Mapped[str] = mapped_column(String(240))
    audience: Mapped[str] = mapped_column(String(500))
    brand_voice: Mapped[str] = mapped_column(String(500))
    visual_style: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    videos: Mapped[list["Video"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    title: Mapped[str] = mapped_column(String(240), index=True)
    content_type: Mapped[ContentType] = mapped_column(SAEnum(ContentType), default=ContentType.long)
    pillar: Mapped[str] = mapped_column(String(120), default="AI call handling")
    target_viewer: Mapped[str] = mapped_column(String(240), default="Local business owner")
    pain_point: Mapped[str] = mapped_column(String(500), default="Missed calls and slow follow-up")
    demo_idea: Mapped[str] = mapped_column(String(500), default="Dashboard demo")
    thumbnail_text: Mapped[str] = mapped_column(String(80), default="AI BUSINESS SYSTEM")
    niche: Mapped[str | None] = mapped_column(String(240), nullable=True)
    target_audience: Mapped[str | None] = mapped_column(String(500), nullable=True)
    angle: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[VideoStatus] = mapped_column(SAEnum(VideoStatus), default=VideoStatus.idea, index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    publish_status: Mapped[str] = mapped_column(String(60), default="draft")
    publish_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_preview_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_rendered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    preview_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    preview_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    channel: Mapped[Channel] = relationship(back_populates="videos")
    assets: Mapped[list["ContentAsset"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    reviews: Mapped[list["Review"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    publish_records: Mapped[list["PublishRecord"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    audit_events: Mapped[list["AuditEvent"]] = relationship(back_populates="video", cascade="all, delete-orphan")


class ContentAsset(Base):
    __tablename__ = "content_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    asset_type: Mapped[AssetType] = mapped_column(SAEnum(AssetType), index=True)
    body: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="assets")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer: Mapped[str] = mapped_column(String(120), default="operator")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="reviews")


class PublishRecord(Base):
    __tablename__ = "publish_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    platform: Mapped[str] = mapped_column(String(60), default="youtube")
    external_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    metadata_body: Mapped[str] = mapped_column(Text)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="publish_records")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    message: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video | None] = relationship(back_populates="audit_events")


class VideoOpportunity(Base):
    __tablename__ = "video_opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    topic: Mapped[str] = mapped_column(String(240), index=True)
    niche_lane: Mapped[str] = mapped_column(String(240))
    audience: Mapped[str] = mapped_column(String(500))
    monetization_path: Mapped[str] = mapped_column(String(240))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    search_demand: Mapped[int] = mapped_column(Integer, default=3)
    buyer_intent: Mapped[int] = mapped_column(Integer, default=3)
    affiliate_potential: Mapped[int] = mapped_column(Integer, default=3)
    sponsorship_potential: Mapped[int] = mapped_column(Integer, default=3)
    production_difficulty: Mapped[int] = mapped_column(Integer, default=3)
    compliance_risk: Mapped[int] = mapped_column(Integer, default=2)
    trend_freshness: Mapped[int] = mapped_column(Integer, default=3)
    product_connection: Mapped[int] = mapped_column(Integer, default=3)
    total_score: Mapped[int] = mapped_column(Integer, default=24, index=True)

    expected_monetization_path: Mapped[str] = mapped_column(String(240), default="Lead magnet")
    why_make_this: Mapped[str] = mapped_column(Text, default="Deterministic local estimate. Validate manually.")
    recommended_title: Mapped[str] = mapped_column(String(240))
    thumbnail_angle: Mapped[str] = mapped_column(String(240), default="Clear before/after operator workflow")
    recommended_cta: Mapped[str] = mapped_column(String(240), default="Comment your workflow bottleneck")
    assigned_agent: Mapped[str] = mapped_column(String(120), default="Opportunity Research Agent")
    compliance_risk_note: Mapped[str] = mapped_column(Text, default="Estimate only. Run compliance checks after assets.")
    review_status: Mapped[str] = mapped_column(String(40), default=OpportunityReviewStatus.unreviewed.value, index=True)
    operator_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    promoted_video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id"), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExecutiveProducerRecommendation(Base):
    __tablename__ = "executive_producer_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    selected_opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("video_opportunities.id"), nullable=True, index=True)
    selected_review_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recommended_topic: Mapped[str | None] = mapped_column(String(240), nullable=True)
    niche_lane: Mapped[str | None] = mapped_column(String(240), nullable=True)
    assigned_agent: Mapped[str | None] = mapped_column(String(120), nullable=True)
    recommended_title: Mapped[str | None] = mapped_column(String(240), nullable=True)
    thumbnail_angle: Mapped[str | None] = mapped_column(String(240), nullable=True)
    recommended_cta: Mapped[str | None] = mapped_column(String(240), nullable=True)
    monetization_path: Mapped[str | None] = mapped_column(String(240), nullable=True)
    why_make_today: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks_to_review: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_checklist: Mapped[str | None] = mapped_column(Text, nullable=True)
    production_brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_label: Mapped[str] = mapped_column(String(20), default=ProducerConfidenceLabel.low.value)
    selection_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    empty_state_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
