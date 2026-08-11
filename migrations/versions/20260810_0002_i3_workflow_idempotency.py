"""Add deterministic I3 workflow replay identities.

Revision ID: 20260810_0002
Revises: 20260810_0001
Create Date: 2026-08-10
"""

from alembic import op


revision = "20260810_0002"
down_revision = "20260810_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_campaigns_workflow_id",
        "campaigns",
        ["workflow_id"],
        unique=True,
    )
    op.create_index(
        "uq_gate_decisions_replay_identity",
        "gate_decisions",
        ["campaign_id", "stage", "policy_version", "input_hash"],
        unique=True,
    )
    op.create_index(
        "uq_artifacts_replay_identity",
        "artifacts",
        ["campaign_id", "kind", "sha256"],
        unique=True,
    )
    op.create_index(
        "uq_generation_jobs_attempt_identity",
        "generation_jobs",
        ["campaign_id", "provider", "model", "input_hash", "attempt"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_generation_jobs_attempt_identity",
        table_name="generation_jobs",
    )
    op.drop_index("uq_artifacts_replay_identity", table_name="artifacts")
    op.drop_index(
        "uq_gate_decisions_replay_identity",
        table_name="gate_decisions",
    )
    op.drop_index("uq_campaigns_workflow_id", table_name="campaigns")
