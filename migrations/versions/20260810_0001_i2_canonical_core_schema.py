"""Add canonical campaign persistence beside the preserved legacy schema.

Revision ID: 20260810_0001
Revises: 22348998243b
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260810_0001"
down_revision = "22348998243b"
branch_labels = None
depends_on = None


RUNTIME_STAGES = "'topic', 'research', 'script', 'storyboard', 'media', 'assembly', 'machine_qa', 'private_upload', 'human_approval', 'release'"


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("current_stage", sa.String(length=40), nullable=False),
        sa.Column("workflow_id", sa.String(length=240), nullable=True),
        sa.Column("risk_tier", sa.String(length=40), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("legacy_video_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"current_stage IN ({RUNTIME_STAGES})", name="ck_campaigns_current_stage"),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["legacy_video_id"], ["videos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_channel_id", "campaigns", ["channel_id"], unique=False)
    op.create_index("ix_campaigns_current_stage", "campaigns", ["current_stage"], unique=False)
    op.create_index("ix_campaigns_legacy_video_id", "campaigns", ["legacy_video_id"], unique=False)
    op.create_index("ix_campaigns_workflow_id", "campaigns", ["workflow_id"], unique=False)

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("publisher", sa.String(length=240), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_class", sa.String(length=80), nullable=False),
        sa.Column("rights_status", sa.String(length=80), nullable=False),
        sa.Column("evidence_snippet", sa.Text(), nullable=True),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_sources_content_sha256"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "source_uri", name="uq_sources_campaign_uri"),
    )
    op.create_index("ix_sources_campaign_id", "sources", ["campaign_id"], unique=False)
    op.create_index("ix_sources_content_sha256", "sources", ["content_sha256"], unique=False)

    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("assertion_text", sa.Text(), nullable=False),
        sa.Column("material", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("structured_value_json", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('VERIFIED', 'OPINION', 'ESTIMATE', 'REJECTED')", name="ck_claims_state"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_claims_campaign_id", "claims", ["campaign_id"], unique=False)
    op.create_index("ix_claims_material", "claims", ["material"], unique=False)
    op.create_index("ix_claims_state", "claims", ["state"], unique=False)

    op.create_table(
        "claim_sources",
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("claim_id", "source_id"),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=120), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("source_stage", sa.String(length=40), nullable=False),
        sa.Column("provider_name", sa.String(length=120), nullable=True),
        sa.Column("provider_model", sa.String(length=240), nullable=True),
        sa.Column("prompt_template_version", sa.String(length=120), nullable=True),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(sha256) = 64", name="ck_artifacts_sha256"),
        sa.CheckConstraint("byte_size >= 0", name="ck_artifacts_byte_size"),
        sa.CheckConstraint(f"source_stage IN ({RUNTIME_STAGES})", name="ck_artifacts_source_stage"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_campaign_id", "artifacts", ["campaign_id"], unique=False)
    op.create_index("ix_artifacts_kind", "artifacts", ["kind"], unique=False)
    op.create_index("ix_artifacts_sha256", "artifacts", ["sha256"], unique=False)
    op.create_index("ix_artifacts_source_stage", "artifacts", ["source_stage"], unique=False)

    op.create_table(
        "scenes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("visual_mode", sa.String(length=80), nullable=False),
        sa.Column("narration_reference", sa.Text(), nullable=True),
        sa.Column("overlay_spec_json", sa.Text(), nullable=False),
        sa.Column("disclosure_state", sa.String(length=80), nullable=False),
        sa.Column("storyboard_artifact_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_scenes_position"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.ForeignKeyConstraint(["storyboard_artifact_id"], ["artifacts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "position", name="uq_scenes_campaign_position"),
    )
    op.create_index("ix_scenes_campaign_id", "scenes", ["campaign_id"], unique=False)
    op.create_index("ix_scenes_storyboard_artifact_id", "scenes", ["storyboard_artifact_id"], unique=False)
    op.create_index("ix_scenes_visual_mode", "scenes", ["visual_mode"], unique=False)

    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("scene_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=120), nullable=False),
        sa.Column("model", sa.String(length=240), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_artifact_id", sa.Integer(), nullable=True),
        sa.Column("provider_job_id", sa.String(length=240), nullable=True),
        sa.Column("usage_json", sa.Text(), nullable=True),
        sa.Column("cost_microunits", sa.Integer(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("attempt >= 1", name="ck_generation_jobs_attempt"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"]),
        sa.ForeignKeyConstraint(["output_artifact_id"], ["artifacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generation_jobs_campaign_id", "generation_jobs", ["campaign_id"], unique=False)
    op.create_index("ix_generation_jobs_input_hash", "generation_jobs", ["input_hash"], unique=False)
    op.create_index("ix_generation_jobs_output_artifact_id", "generation_jobs", ["output_artifact_id"], unique=False)
    op.create_index("ix_generation_jobs_provider", "generation_jobs", ["provider"], unique=False)
    op.create_index("ix_generation_jobs_provider_job_id", "generation_jobs", ["provider_job_id"], unique=False)
    op.create_index("ix_generation_jobs_scene_id", "generation_jobs", ["scene_id"], unique=False)
    op.create_index("ix_generation_jobs_status", "generation_jobs", ["status"], unique=False)

    op.create_table(
        "gate_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"stage IN ({RUNTIME_STAGES})", name="ck_gate_decisions_stage"),
        sa.CheckConstraint("outcome IN ('PASS', 'FAIL', 'NEEDS_HUMAN')", name="ck_gate_decisions_outcome"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gate_decisions_campaign_id", "gate_decisions", ["campaign_id"], unique=False)
    op.create_index("ix_gate_decisions_input_hash", "gate_decisions", ["input_hash"], unique=False)
    op.create_index("ix_gate_decisions_outcome", "gate_decisions", ["outcome"], unique=False)
    op.create_index("ix_gate_decisions_stage", "gate_decisions", ["stage"], unique=False)

    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("final_render_sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata_sha256", sa.String(length=64), nullable=False),
        sa.Column("thumbnail_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("disclosure_state", sa.String(length=80), nullable=False),
        sa.Column("private_youtube_video_id", sa.String(length=240), nullable=False),
        sa.Column("intended_release_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=240), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(final_render_sha256) = 64", name="ck_approvals_render_sha256"),
        sa.CheckConstraint("length(metadata_sha256) = 64", name="ck_approvals_metadata_sha256"),
        sa.CheckConstraint("length(thumbnail_manifest_sha256) = 64", name="ck_approvals_thumbnail_sha256"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approvals_campaign_id", "approvals", ["campaign_id"], unique=False)

    op.create_table(
        "metric_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("external_video_id", sa.String(length=240), nullable=False),
        sa.Column("measured_at", sa.DateTime(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.Column("engaged_views", sa.Integer(), nullable=True),
        sa.Column("estimated_minutes_watched", sa.Float(), nullable=True),
        sa.Column("average_view_duration", sa.Float(), nullable=True),
        sa.Column("average_view_percentage", sa.Float(), nullable=True),
        sa.Column("subscribers_gained", sa.Integer(), nullable=True),
        sa.Column("subscribers_lost", sa.Integer(), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("raw_metrics_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metric_snapshots_campaign_id", "metric_snapshots", ["campaign_id"], unique=False)
    op.create_index("ix_metric_snapshots_external_video_id", "metric_snapshots", ["external_video_id"], unique=False)
    op.create_index("ix_metric_snapshots_measured_at", "metric_snapshots", ["measured_at"], unique=False)

    with op.batch_alter_table("audit_events", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("campaign_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("workflow_id", sa.String(length=240), nullable=True))
        batch_op.add_column(sa.Column("stage", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("actor", sa.String(length=240), nullable=True))
        batch_op.create_foreign_key("fk_audit_events_campaign_id", "campaigns", ["campaign_id"], ["id"])
        batch_op.create_index("ix_audit_events_campaign_id", ["campaign_id"], unique=False)
        batch_op.create_index("ix_audit_events_stage", ["stage"], unique=False)

    with op.batch_alter_table("publish_records", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("campaign_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("publication_state", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("privacy_status", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("final_render_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("metadata_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("thumbnail_manifest_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("disclosure_state", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("provider_upload_id", sa.String(length=240), nullable=True))
        batch_op.create_foreign_key("fk_publish_records_campaign_id", "campaigns", ["campaign_id"], ["id"])
        batch_op.create_index("ix_publish_records_campaign_id", ["campaign_id"], unique=False)
        batch_op.create_index("ix_publish_records_publication_state", ["publication_state"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("publish_records", recreate="always") as batch_op:
        batch_op.drop_index("ix_publish_records_publication_state")
        batch_op.drop_index("ix_publish_records_campaign_id")
        batch_op.drop_constraint("fk_publish_records_campaign_id", type_="foreignkey")
        batch_op.drop_column("provider_upload_id")
        batch_op.drop_column("disclosure_state")
        batch_op.drop_column("thumbnail_manifest_sha256")
        batch_op.drop_column("metadata_sha256")
        batch_op.drop_column("final_render_sha256")
        batch_op.drop_column("privacy_status")
        batch_op.drop_column("publication_state")
        batch_op.drop_column("campaign_id")

    with op.batch_alter_table("audit_events", recreate="always") as batch_op:
        batch_op.drop_index("ix_audit_events_stage")
        batch_op.drop_index("ix_audit_events_campaign_id")
        batch_op.drop_constraint("fk_audit_events_campaign_id", type_="foreignkey")
        batch_op.drop_column("actor")
        batch_op.drop_column("stage")
        batch_op.drop_column("workflow_id")
        batch_op.drop_column("campaign_id")

    op.drop_table("claim_sources")
    for table_name, indexes in (
        ("metric_snapshots", ("ix_metric_snapshots_measured_at", "ix_metric_snapshots_external_video_id", "ix_metric_snapshots_campaign_id")),
        ("approvals", ("ix_approvals_campaign_id",)),
        ("gate_decisions", ("ix_gate_decisions_stage", "ix_gate_decisions_outcome", "ix_gate_decisions_input_hash", "ix_gate_decisions_campaign_id")),
        ("generation_jobs", ("ix_generation_jobs_status", "ix_generation_jobs_scene_id", "ix_generation_jobs_provider_job_id", "ix_generation_jobs_provider", "ix_generation_jobs_output_artifact_id", "ix_generation_jobs_input_hash", "ix_generation_jobs_campaign_id")),
        ("scenes", ("ix_scenes_visual_mode", "ix_scenes_storyboard_artifact_id", "ix_scenes_campaign_id")),
        ("artifacts", ("ix_artifacts_source_stage", "ix_artifacts_sha256", "ix_artifacts_kind", "ix_artifacts_campaign_id")),
        ("sources", ("ix_sources_content_sha256", "ix_sources_campaign_id")),
        ("claims", ("ix_claims_state", "ix_claims_material", "ix_claims_campaign_id")),
        ("campaigns", ("ix_campaigns_workflow_id", "ix_campaigns_legacy_video_id", "ix_campaigns_current_stage", "ix_campaigns_channel_id")),
    ):
        for index_name in indexes:
            op.drop_index(index_name, table_name=table_name)
        op.drop_table(table_name)
