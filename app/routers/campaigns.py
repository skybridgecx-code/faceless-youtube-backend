from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import Campaign, get_db
from app.models import Channel
from app.workflows.api_models import (
    CampaignCreateRequest,
    CampaignRead,
    CampaignWorkflowStartRead,
)
from app.workflows.campaign_workflow import (
    I3_POLICY_VERSION,
    start_i3_campaign_workflow,
)
from app.workflows.dbos_runtime import DBOSRuntimeError
from app.workflows.persistence import (
    CampaignNotFoundError,
    WorkflowReplayConflict,
    WorkflowTransitionError,
)


router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def _campaign_or_404(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.post("", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
def create_campaign(
    payload: CampaignCreateRequest,
    db: Session = Depends(get_db),
) -> CampaignRead:
    if db.get(Channel, payload.channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    campaign = Campaign(
        channel_id=payload.channel_id,
        current_stage="topic",
        workflow_id=None,
        risk_tier=payload.risk_tier,
        policy_version=I3_POLICY_VERSION,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.get("/{campaign_id}", response_model=CampaignRead)
def read_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
) -> CampaignRead:
    return CampaignRead.model_validate(_campaign_or_404(db, campaign_id))


@router.post(
    "/{campaign_id}/workflow/start",
    response_model=CampaignWorkflowStartRead,
)
def start_campaign_workflow(
    campaign_id: int,
    db: Session = Depends(get_db),
) -> CampaignWorkflowStartRead:
    try:
        handle = start_i3_campaign_workflow(db, campaign_id)
    except CampaignNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Campaign not found") from exc
    except (WorkflowReplayConflict, WorkflowTransitionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DBOSRuntimeError as exc:
        raise HTTPException(status_code=503, detail="Durable workflow runtime unavailable") from exc

    campaign = _campaign_or_404(db, campaign_id)
    workflow_id = handle.get_workflow_id()
    return CampaignWorkflowStartRead(
        campaign=CampaignRead.model_validate(campaign),
        workflow_id=workflow_id,
        durable_status=handle.get_status().status,
    )
