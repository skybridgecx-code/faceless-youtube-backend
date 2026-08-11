from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import Artifact, Campaign, SessionLocal, get_db
from app.editorial.contracts import EditorialSeed, I4_POLICY_VERSION
from app.editorial.persistence import (
    editorial_snapshot,
    load_active_editorial_seed,
    persist_editorial_seed,
)
from app.models import Channel
from app.workflows.api_models import (
    CampaignCreateRequest,
    CampaignRead,
    CampaignWorkflowStartRead,
    EditorialSeedRead,
    EditorialSnapshotRead,
)
from app.workflows.campaign_workflow import (
    I3_POLICY_VERSION,
    start_i3_campaign_workflow,
)
from app.workflows.dbos_runtime import DBOSRuntimeError
from app.workflows.i4_campaign_workflow import (
    I4ConfigurationError,
    start_i4_campaign_workflow,
)
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
        policy_version=payload.policy_version,
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
    "/{campaign_id}/editorial/seed",
    response_model=EditorialSeedRead,
)
def create_editorial_seed(
    campaign_id: int,
    payload: EditorialSeed,
) -> EditorialSeedRead:
    try:
        persisted = persist_editorial_seed(campaign_id, payload)
        active_artifact, active_seed = load_active_editorial_seed(campaign_id)
    except CampaignNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Campaign not found") from exc
    except (WorkflowReplayConflict, WorkflowTransitionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return EditorialSeedRead(
        artifact_id=int(persisted["artifact_id"]),
        seed_hash=str(persisted["seed_hash"]),
        active=active_artifact.id == int(persisted["artifact_id"]),
        created=bool(persisted["created"]),
        created_at=(
            active_artifact.created_at
            if active_artifact.id == int(persisted["artifact_id"])
            else _seed_artifact_created_at(campaign_id, int(persisted["artifact_id"]))
        ),
        seed=(
            active_seed
            if active_artifact.id == int(persisted["artifact_id"])
            else payload
        ),
    )


def _seed_artifact_created_at(campaign_id: int, artifact_id: int) -> datetime:
    with SessionLocal() as db:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None or artifact.campaign_id != campaign_id:
            raise WorkflowReplayConflict("Editorial seed artifact disappeared")
        return artifact.created_at


@router.get(
    "/{campaign_id}/editorial/seed",
    response_model=EditorialSeedRead,
)
def read_editorial_seed(campaign_id: int) -> EditorialSeedRead:
    try:
        artifact, seed = load_active_editorial_seed(campaign_id)
    except CampaignNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Campaign not found") from exc
    except WorkflowTransitionError as exc:
        raise HTTPException(status_code=404, detail="Editorial seed not found") from exc
    except WorkflowReplayConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return EditorialSeedRead(
        artifact_id=artifact.id,
        seed_hash=artifact.sha256,
        active=True,
        created=False,
        created_at=artifact.created_at,
        seed=seed,
    )


@router.get(
    "/{campaign_id}/editorial",
    response_model=EditorialSnapshotRead,
)
def read_editorial_snapshot(campaign_id: int) -> EditorialSnapshotRead:
    try:
        return EditorialSnapshotRead.model_validate(editorial_snapshot(campaign_id))
    except CampaignNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Campaign not found") from exc
    except WorkflowReplayConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{campaign_id}/workflow/start",
    response_model=CampaignWorkflowStartRead,
)
def start_campaign_workflow(
    campaign_id: int,
    db: Session = Depends(get_db),
) -> CampaignWorkflowStartRead:
    try:
        campaign = _campaign_or_404(db, campaign_id)
        if campaign.policy_version == I3_POLICY_VERSION:
            handle = start_i3_campaign_workflow(db, campaign_id)
        elif campaign.policy_version == I4_POLICY_VERSION:
            db.rollback()
            handle = start_i4_campaign_workflow(campaign_id)
        else:
            raise WorkflowTransitionError(
                f"Unsupported campaign policy: {campaign.policy_version}"
            )
    except CampaignNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Campaign not found") from exc
    except (WorkflowReplayConflict, WorkflowTransitionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DBOSRuntimeError as exc:
        raise HTTPException(status_code=503, detail="Durable workflow runtime unavailable") from exc
    except I4ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.expire_all()
    campaign = _campaign_or_404(db, campaign_id)
    workflow_id = handle.get_workflow_id()
    return CampaignWorkflowStartRead(
        campaign=CampaignRead.model_validate(campaign),
        workflow_id=workflow_id,
        durable_status=handle.get_status().status,
    )
