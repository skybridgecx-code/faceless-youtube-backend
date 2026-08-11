from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


RUNTIME_STAGES = (
    "topic",
    "research",
    "script",
    "storyboard",
    "media",
    "assembly",
    "machine_qa",
    "private_upload",
    "human_approval",
    "release",
)
_RUNTIME_STAGE_SQL = ", ".join(f"'{stage}'" for stage in RUNTIME_STAGES)


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(f"current_stage IN ({_RUNTIME_STAGE_SQL})", name="ck_campaigns_current_stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    current_stage: Mapped[str] = mapped_column(String(40), default="topic", index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(240), nullable=True, index=True)
    risk_tier: Mapped[str] = mapped_column(String(40), default="standard")
    policy_version: Mapped[str] = mapped_column(String(120), default="v1")
    legacy_video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("campaign_id", "source_uri", name="uq_sources_campaign_uri"),
        CheckConstraint("length(content_sha256) = 64", name="ck_sources_content_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    source_uri: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(240), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    source_class: Mapped[str] = mapped_column(String(80), default="unknown")
    rights_status: Mapped[str] = mapped_column(String(80), default="unknown")
    evidence_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(
            "state IN ('VERIFIED', 'OPINION', 'ESTIMATE', 'REJECTED')",
            name="ck_claims_state",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    assertion_text: Mapped[str] = mapped_column(Text)
    material: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    state: Mapped[str] = mapped_column(String(20), default="ESTIMATE", index=True)
    structured_value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ClaimSource(Base):
    __tablename__ = "claim_sources"

    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("length(sha256) = 64", name="ck_artifacts_sha256"),
        CheckConstraint("byte_size >= 0", name="ck_artifacts_byte_size"),
        CheckConstraint(f"source_stage IN ({_RUNTIME_STAGE_SQL})", name="ck_artifacts_source_stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    kind: Mapped[str] = mapped_column(String(120), index=True)
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(120))
    source_stage: Mapped[str] = mapped_column(String(40), index=True)
    provider_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider_model: Mapped[str | None] = mapped_column(String(240), nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Scene(Base):
    __tablename__ = "scenes"
    __table_args__ = (
        UniqueConstraint("campaign_id", "position", name="uq_scenes_campaign_position"),
        CheckConstraint("position >= 0", name="ck_scenes_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    visual_mode: Mapped[str] = mapped_column(String(80), index=True)
    narration_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    overlay_spec_json: Mapped[str] = mapped_column(Text, default="{}")
    disclosure_state: Mapped[str] = mapped_column(String(80), default="pending")
    storyboard_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
