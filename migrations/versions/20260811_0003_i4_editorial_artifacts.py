"""Add canonical I4 editorial artifact content and claim identities.

Revision ID: 20260811_0003
Revises: 20260810_0002
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa


revision = "20260811_0003"
down_revision = "20260810_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("payload_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "claims",
        sa.Column("claim_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_claims_claim_hash",
        "claims",
        ["claim_hash"],
        unique=False,
    )
    op.create_index(
        "uq_claims_replay_identity",
        "claims",
        ["campaign_id", "claim_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_claims_replay_identity", table_name="claims")
    op.drop_index("ix_claims_claim_hash", table_name="claims")
    op.drop_column("claims", "claim_hash")
    op.drop_column("artifacts", "payload_json")
