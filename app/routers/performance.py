from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import PerformanceSummaryResponse
from app.services.performance_feedback import performance_summary_rows

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("/summary", response_model=PerformanceSummaryResponse)
def get_performance_summary(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PerformanceSummaryResponse:
    payload = performance_summary_rows(db, limit=limit)
    return PerformanceSummaryResponse.model_validate(payload)
