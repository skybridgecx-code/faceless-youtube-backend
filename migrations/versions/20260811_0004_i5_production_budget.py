"""Add canonical I5 production identity and campaign budget reservations.

Revision ID: 20260811_0004
Revises: 20260811_0003
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa


revision = "20260811_0004"
down_revision = "20260811_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("production_workflow_id", sa.String(length=240), nullable=True),
    )
    op.create_index(
        "uq_campaigns_production_workflow_id",
        "campaigns",
        ["production_workflow_id"],
        unique=True,
    )
    op.add_column(
        "generation_jobs",
        sa.Column("reserved_cost_microunits", sa.Integer(), nullable=True),
    )
    op.create_table(
        "campaign_budget_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("previous_authorized_cap_microunits", sa.Integer(), nullable=False),
        sa.Column("new_authorized_cap_microunits", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=240), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("override_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "previous_authorized_cap_microunits >= 0",
            name="ck_campaign_budget_overrides_previous_cap_nonnegative",
        ),
        sa.CheckConstraint(
            "new_authorized_cap_microunits > previous_authorized_cap_microunits",
            name="ck_campaign_budget_overrides_cap_increase",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) > 0",
            name="ck_campaign_budget_overrides_actor_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_campaign_budget_overrides_reason_nonblank",
        ),
        sa.CheckConstraint(
            "length(override_hash) = 64",
            name="ck_campaign_budget_overrides_hash",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_campaign_budget_overrides_campaign_id",
        "campaign_budget_overrides",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_budget_overrides_override_hash",
        "campaign_budget_overrides",
        ["override_hash"],
        unique=False,
    )
    op.create_index(
        "uq_campaign_budget_overrides_replay_identity",
        "campaign_budget_overrides",
        ["campaign_id", "override_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_campaign_budget_overrides_replay_identity",
        table_name="campaign_budget_overrides",
    )
    op.drop_index(
        "ix_campaign_budget_overrides_override_hash",
        table_name="campaign_budget_overrides",
    )
    op.drop_index(
        "ix_campaign_budget_overrides_campaign_id",
        table_name="campaign_budget_overrides",
    )
    op.drop_table("campaign_budget_overrides")
    op.drop_column("generation_jobs", "reserved_cost_microunits")
    op.drop_index("uq_campaigns_production_workflow_id", table_name="campaigns")
    op.drop_column("campaigns", "production_workflow_id")
