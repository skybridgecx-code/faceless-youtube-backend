"""Stable database compatibility exports and canonical persistence models."""

from .base import Base
from .models_content import Artifact, Campaign, Claim, ClaimSource, Scene, Source
from .models_ops import (
    Approval,
    CampaignBudgetOverride,
    GateDecision,
    GenerationJob,
    MetricSnapshot,
)
from .schema import DatabaseSchemaError, validate_database_schema
from .session import SessionLocal, engine, get_db


def init_db() -> None:
    """Compatibility entry point that validates, but never repairs, the database."""

    # Importing legacy models registers their tables with the shared metadata.
    from app import models  # noqa: F401

    validate_database_schema(engine, Base.metadata.tables.values())


__all__ = [
    "Approval",
    "Artifact",
    "Base",
    "Campaign",
    "CampaignBudgetOverride",
    "Claim",
    "ClaimSource",
    "DatabaseSchemaError",
    "GateDecision",
    "GenerationJob",
    "MetricSnapshot",
    "Scene",
    "SessionLocal",
    "Source",
    "engine",
    "get_db",
    "init_db",
]
