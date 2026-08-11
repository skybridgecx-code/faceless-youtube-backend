from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .models_content import _RUNTIME_STAGE_SQL


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_generation_jobs_attempt"),
        Index(
            "uq_generation_jobs_attempt_identity",
            "campaign_id",
            "provider",
            "model",
            "input_hash",
            "attempt",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    scene_id: Mapped[int | None] = mapped_column(ForeignKey("scenes.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(120), index=True)
    model: Mapped[str] = mapped_column(String(240))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    output_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True, index=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(240), nullable=True, index=True)
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_microunits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GateDecision(Base):
    __tablename__ = "gate_decisions"
    __table_args__ = (
        CheckConstraint(f"stage IN ({_RUNTIME_STAGE_SQL})", name="ck_gate_decisions_stage"),
        CheckConstraint("outcome IN ('PASS', 'FAIL', 'NEEDS_HUMAN')", name="ck_gate_decisions_outcome"),
        Index(
            "uq_gate_decisions_replay_identity",
            "campaign_id",
            "stage",
            "policy_version",
            "input_hash",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40), index=True)
    outcome: Mapped[str] = mapped_column(String(20), index=True)
    policy_version: Mapped[str] = mapped_column(String(120))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("length(final_render_sha256) = 64", name="ck_approvals_render_sha256"),
        CheckConstraint("length(metadata_sha256) = 64", name="ck_approvals_metadata_sha256"),
        CheckConstraint("length(thumbnail_manifest_sha256) = 64", name="ck_approvals_thumbnail_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    final_render_sha256: Mapped[str] = mapped_column(String(64))
    metadata_sha256: Mapped[str] = mapped_column(String(64))
    thumbnail_manifest_sha256: Mapped[str] = mapped_column(String(64))
    disclosure_state: Mapped[str] = mapped_column(String(80))
    private_youtube_video_id: Mapped[str] = mapped_column(String(240))
    intended_release_at: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(240))
    approved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    external_video_id: Mapped[str] = mapped_column(String(240), index=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    engaged_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_minutes_watched: Mapped[float | None] = mapped_column(nullable=True)
    average_view_duration: Mapped[float | None] = mapped_column(nullable=True)
    average_view_percentage: Mapped[float | None] = mapped_column(nullable=True)
    subscribers_gained: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subscribers_lost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
