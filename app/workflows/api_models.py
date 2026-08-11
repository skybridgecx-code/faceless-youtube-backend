from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CampaignCreateRequest(BaseModel):
    channel_id: int = Field(ge=1)
    risk_tier: str = Field(default="standard", min_length=1, max_length=40)


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
