from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.editorial.contracts import EditorialSeed


class CampaignCreateRequest(BaseModel):
    channel_id: int = Field(ge=1)
    risk_tier: str = Field(default="standard", min_length=1, max_length=40)
    policy_version: Literal["i3-gate-v1", "i4-editorial-v1"] = "i4-editorial-v1"


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    current_stage: str
    workflow_id: str | None
    risk_tier: str
    policy_version: str
    created_at: datetime
    updated_at: datetime


class CampaignWorkflowStartRead(BaseModel):
    campaign: CampaignRead
    workflow_id: str
    durable_status: str


class EditorialSeedRead(BaseModel):
    artifact_id: int
    seed_hash: str
    active: bool
    created: bool
    created_at: datetime
    seed: EditorialSeed


class EditorialSnapshotRead(BaseModel):
    campaign_id: int
    active_seed_hash: str | None
    current_stage: str
    workflow_id: str | None
    selected_topic: str | None
    viewer_promise: dict[str, object] | None
    topic_score: int | None
    topic_score_breakdown: dict[str, int] | None
    source_counts: dict[str, int]
    claim_state_counts: dict[str, int]
    script_hash: str | None
    script_runtime_estimate_minutes: float | None
    last_gate_outcome: str | None
    seed_present: bool
