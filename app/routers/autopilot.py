from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    AutopilotRunRead,
    AutopilotRunRequest,
    FinalApprovalDecisionRequest,
    FinalApprovalDecisionResponse,
    FinalApprovalQueueItem,
)
from app.services.autopilot_production import (
    apply_final_approval_decision,
    get_autopilot_run_or_404,
    list_autopilot_runs,
    list_final_approval_queue,
    run_autopilot_batch,
)

router = APIRouter(prefix="/review-prep", tags=["review-prep"])
compat_router = APIRouter(prefix="/autopilot", tags=["autopilot-compat"])


@router.post("/runs", response_model=AutopilotRunRead)
@compat_router.post("/runs", response_model=AutopilotRunRead, include_in_schema=False)
def create_review_prep_run(payload: AutopilotRunRequest, db: Session = Depends(get_db)) -> AutopilotRunRead:
    return run_autopilot_batch(db, payload)


@router.get("/runs", response_model=list[AutopilotRunRead])
@compat_router.get("/runs", response_model=list[AutopilotRunRead], include_in_schema=False)
def get_review_prep_runs(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[AutopilotRunRead]:
    return list_autopilot_runs(db, limit=limit)


@router.get("/runs/{run_id}", response_model=AutopilotRunRead)
@compat_router.get("/runs/{run_id}", response_model=AutopilotRunRead, include_in_schema=False)
def get_review_prep_run(run_id: int, db: Session = Depends(get_db)) -> AutopilotRunRead:
    return get_autopilot_run_or_404(db, run_id)


@router.get("/final-review-queue", response_model=list[FinalApprovalQueueItem])
@compat_router.get("/final-approval-queue", response_model=list[FinalApprovalQueueItem], include_in_schema=False)
def get_final_review_queue(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[FinalApprovalQueueItem]:
    return list_final_approval_queue(db, limit=limit)


@router.post("/videos/{video_id}/final-review-decision", response_model=FinalApprovalDecisionResponse)
@compat_router.post("/videos/{video_id}/final-approval", response_model=FinalApprovalDecisionResponse, include_in_schema=False)
def post_final_review_decision(
    video_id: int,
    payload: FinalApprovalDecisionRequest,
    db: Session = Depends(get_db),
) -> FinalApprovalDecisionResponse:
    return apply_final_approval_decision(
        db,
        video_id=video_id,
        decision=payload.decision,
        notes=payload.notes,
    )
