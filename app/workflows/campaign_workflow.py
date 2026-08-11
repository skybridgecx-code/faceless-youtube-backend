from __future__ import annotations

from datetime import datetime
from typing import cast

from dbos import DBOS, SetWorkflowID, SetWorkflowTimeout, WorkflowHandle
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Campaign

from .contracts import (
    I3_PROVIDER_STAGES,
    I3_WORKFLOW_STAGES,
    GateOutcome,
    StageName,
)
from .dbos_runtime import require_dbos_runtime
from .gates import evaluate_gate
from .persistence import (
    CampaignNotFoundError,
    WorkflowReplayConflict,
    WorkflowTransitionError,
    build_stage_request,
    persist_stage_result,
)
from .providers import DeterministicStubProvider


I3_WORKFLOW_MAX_RECOVERY_ATTEMPTS = 3
I3_STEP_MAX_ATTEMPTS = 3
I3_WORKFLOW_TIMEOUT_SECONDS = 60
I3_POLICY_VERSION = "i3-gate-v1"


def campaign_workflow_id(campaign_id: int) -> str:
    if campaign_id <= 0:
        raise ValueError("campaign_id must be positive")
    return f"campaign:{campaign_id}:i3"


def execute_stage_once(campaign_id: int, stage: StageName) -> dict[str, object]:
    request = build_stage_request(campaign_id, stage)
    provider_result = None
    if stage in I3_PROVIDER_STAGES:
        provider_result = DeterministicStubProvider().generate(request)
    decision = evaluate_gate(request, provider_result)
    return persist_stage_result(request, provider_result, decision).as_dict()


@DBOS.step(
    name="i3_execute_campaign_stage",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I3_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def execute_stage_step(campaign_id: int, stage: StageName) -> dict[str, object]:
    return execute_stage_once(campaign_id, stage)


@DBOS.workflow(
    name="i3_campaign_workflow",
    max_recovery_attempts=I3_WORKFLOW_MAX_RECOVERY_ATTEMPTS,
)
def run_i3_campaign_workflow(campaign_id: int) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for stage in I3_WORKFLOW_STAGES:
        result = execute_stage_step(campaign_id, stage)
        results.append(result)
        outcome = cast(GateOutcome, result["outcome"])
        if outcome != "PASS":
            break

    final_stage = cast(StageName, results[-1]["current_stage"])
    return {
        "campaign_id": campaign_id,
        "final_stage": final_stage,
        "stage_results": results,
        "workflow_id": campaign_workflow_id(campaign_id),
    }


def bind_campaign_workflow_id(db: Session, campaign_id: int) -> Campaign:
    expected_workflow_id = campaign_workflow_id(campaign_id)
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
    if campaign.workflow_id is not None:
        if campaign.workflow_id != expected_workflow_id:
            raise WorkflowReplayConflict(
                "Campaign workflow identity conflicts with deterministic I3 identity"
            )
        return campaign
    if campaign.current_stage != "topic":
        raise WorkflowTransitionError(
            "An unbound I3 campaign must begin at the topic stage"
        )

    try:
        bound = db.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.workflow_id.is_(None))
            .values(workflow_id=expected_workflow_id, updated_at=datetime.utcnow())
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise WorkflowReplayConflict(
            "Campaign workflow identity violates uniqueness"
        ) from exc
    if bound.rowcount != 1:
        db.expire_all()
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.workflow_id != expected_workflow_id:
            raise WorkflowReplayConflict("Campaign workflow identity changed concurrently")
        return campaign
    db.expire_all()
    rebound = db.get(Campaign, campaign_id)
    assert rebound is not None
    return rebound


def start_i3_campaign_workflow(
    db: Session,
    campaign_id: int,
) -> WorkflowHandle[dict[str, object]]:
    require_dbos_runtime()
    campaign = bind_campaign_workflow_id(db, campaign_id)
    assert campaign.workflow_id is not None
    with SetWorkflowID(campaign.workflow_id), SetWorkflowTimeout(
        I3_WORKFLOW_TIMEOUT_SECONDS
    ):
        return DBOS.start_workflow(run_i3_campaign_workflow, campaign_id)
