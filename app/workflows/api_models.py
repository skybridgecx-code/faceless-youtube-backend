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
    production_workflow_id: str | None
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


class ProductionWorkflowStartRead(BaseModel):
    campaign: CampaignRead
    production_workflow_id: str
    production_profile_hash: str
    durable_status: str


class ProductionSnapshotRead(BaseModel):
    assembly_manifest_hash: str | None
    authorized_hard_cap_microusd: int
    campaign_id: int
    current_effective_campaign_cost_microusd: int
    current_stage: str
    final_render_hash: str | None
    generated_media_counts: dict[str, object]
    i4_script_hash: str
    latest_budget_block: dict[str, object] | None
    latest_i5_gate: dict[str, object] | None
    media_manifest_hash: str | None
    production_profile_hash: str | None
    production_workflow_id: str | None
    reserved_cost_microusd: int
    scene_count: int
    soft_warning_active: bool
    storyboard_hash: str | None
    visual_mode_counts: dict[str, int]
    voiceover_hash: str | None


class CampaignBudgetOverrideRequest(BaseModel):
    new_authorized_cap_microusd: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=240)
    reason: str = Field(min_length=1, max_length=4_000)


class CampaignBudgetOverrideRead(BaseModel):
    created: bool
    message_delivered: bool
    new_authorized_cap_microusd: int
    override_hash: str
    override_id: int
    previous_authorized_cap_microusd: int
    production_workflow_id: str
