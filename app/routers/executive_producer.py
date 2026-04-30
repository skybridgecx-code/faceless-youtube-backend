from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    ExecutiveProducerRecommendation,
    OpportunityReviewStatus,
    ProducerConfidenceLabel,
    VideoOpportunity,
)
from app.schemas import ExecutiveProducerRecommendationRead
from app.services.audit import log_audit_event

router = APIRouter(prefix="/executive-producer", tags=["executive-producer"])


def serialize_recommendation(item: ExecutiveProducerRecommendation) -> ExecutiveProducerRecommendationRead:
    return ExecutiveProducerRecommendationRead(
        id=item.id,
        selected_opportunity_id=item.selected_opportunity_id,
        selected_review_status=OpportunityReviewStatus(item.selected_review_status) if item.selected_review_status else None,
        recommended_topic=item.recommended_topic,
        niche_lane=item.niche_lane,
        assigned_agent=item.assigned_agent,
        recommended_title=item.recommended_title,
        thumbnail_angle=item.thumbnail_angle,
        recommended_cta=item.recommended_cta,
        monetization_path=item.monetization_path,
        why_make_today=item.why_make_today,
        risks_to_review=item.risks_to_review,
        operator_checklist=item.operator_checklist,
        production_brief=item.production_brief,
        confidence_label=ProducerConfidenceLabel(item.confidence_label),
        selection_score=item.selection_score,
        empty_state_message=item.empty_state_message,
        created_at=item.created_at,
    )


def monetization_bonus(path: str | None) -> int:
    if not path:
        return 0
    lowered = path.lower()
    if "service" in lowered or "consult" in lowered or "audit" in lowered:
        return 3
    if "affiliate" in lowered:
        return 2
    if "sponsor" in lowered:
        return 2
    return 1


def compute_selection_score(opportunity: VideoOpportunity) -> int:
    score = opportunity.total_score * 4
    score += opportunity.buyer_intent * 4
    score += opportunity.product_connection * 4
    score += monetization_bonus(opportunity.expected_monetization_path)
    score -= opportunity.compliance_risk * 3
    score -= opportunity.production_difficulty * 2
    return score


def confidence_from_score(score: int, status: str) -> ProducerConfidenceLabel:
    if status == OpportunityReviewStatus.approved_for_video.value and score >= 70:
        return ProducerConfidenceLabel.high
    if score >= 55:
        return ProducerConfidenceLabel.medium
    return ProducerConfidenceLabel.low


def build_empty_recommendation() -> ExecutiveProducerRecommendation:
    return ExecutiveProducerRecommendation(
        selected_opportunity_id=None,
        selected_review_status=None,
        recommended_topic=None,
        niche_lane=None,
        assigned_agent="Executive Producer Agent (Deterministic Local)",
        recommended_title=None,
        thumbnail_angle=None,
        recommended_cta=None,
        monetization_path=None,
        why_make_today=None,
        risks_to_review="No reviewed opportunities ready for decision.",
        operator_checklist="1. Review opportunity queue.\n2. Shortlist or approve candidates.\n3. Run Executive Producer again.",
        production_brief="No production brief yet.",
        confidence_label=ProducerConfidenceLabel.low.value,
        selection_score=None,
        empty_state_message="No reviewed opportunities are ready. Shortlist or approve an opportunity first.",
    )


def build_empty_recommendation_read() -> ExecutiveProducerRecommendationRead:
    return ExecutiveProducerRecommendationRead(
        id=0,
        selected_opportunity_id=None,
        selected_review_status=None,
        recommended_topic=None,
        niche_lane=None,
        assigned_agent="Executive Producer Agent (Deterministic Local)",
        recommended_title=None,
        thumbnail_angle=None,
        recommended_cta=None,
        monetization_path=None,
        why_make_today=None,
        risks_to_review="No reviewed opportunities ready for decision.",
        operator_checklist="1. Review opportunity queue.\n2. Shortlist or approve candidates.\n3. Run Executive Producer again.",
        production_brief="No production brief yet.",
        confidence_label=ProducerConfidenceLabel.low,
        selection_score=None,
        empty_state_message="No reviewed opportunities are ready. Shortlist or approve an opportunity first.",
        created_at=datetime.utcnow(),
    )


def choose_best_opportunity(opportunities: list[VideoOpportunity]) -> VideoOpportunity | None:
    status_order = [
        OpportunityReviewStatus.approved_for_video.value,
        OpportunityReviewStatus.shortlisted.value,
        OpportunityReviewStatus.needs_more_research.value,
    ]
    for review_status in status_order:
        subset = [item for item in opportunities if item.review_status == review_status]
        if subset:
            subset.sort(
                key=lambda item: (
                    -compute_selection_score(item),
                    -item.total_score,
                    -item.created_at.timestamp(),
                )
            )
            return subset[0]
    return None


def create_recommendation_from_opportunity(opportunity: VideoOpportunity) -> ExecutiveProducerRecommendation:
    selection_score = compute_selection_score(opportunity)
    confidence = confidence_from_score(selection_score, opportunity.review_status)
    risk_lines: list[str] = []
    if opportunity.compliance_risk >= 4:
        risk_lines.append("High compliance risk estimate. Recheck claims and disclosure wording.")
    elif opportunity.compliance_risk >= 3:
        risk_lines.append("Moderate compliance risk estimate. Validate phrasing and disclosure notes.")
    if opportunity.production_difficulty >= 4:
        risk_lines.append("High production difficulty estimate. Scope storyboard before generating assets.")
    elif opportunity.production_difficulty >= 3:
        risk_lines.append("Moderate production effort. Keep script focused and avoid complexity.")
    if opportunity.review_status == OpportunityReviewStatus.needs_more_research.value:
        risk_lines.append("Marked needs_more_research. Confirm assumptions before production.")
    if not risk_lines:
        risk_lines.append("No major estimated blockers. Run compliance and manual review as normal.")

    checklist = [
        "1. Confirm topic still matches current channel strategy.",
        "2. Generate assets and run compliance check.",
        "3. Complete manual review checklist before approval.",
        "4. Render and review draft preview before packaging.",
    ]
    brief_parts = [
        f"Core topic: {opportunity.topic}",
        f"Niche lane: {opportunity.niche_lane}",
        f"Audience: {opportunity.audience}",
        f"Angle: {opportunity.thumbnail_angle}",
        f"Monetization: {opportunity.expected_monetization_path}",
    ]

    why_make_today = (
        "Deterministic local executive producer pick based on review status priority, total score, "
        "buyer intent, product connection, and risk penalties. "
        f"This candidate scored {selection_score} and is currently {opportunity.review_status}."
    )
    return ExecutiveProducerRecommendation(
        selected_opportunity_id=opportunity.id,
        selected_review_status=opportunity.review_status,
        recommended_topic=opportunity.topic,
        niche_lane=opportunity.niche_lane,
        assigned_agent="Executive Producer Agent (Deterministic Local)",
        recommended_title=opportunity.recommended_title,
        thumbnail_angle=opportunity.thumbnail_angle,
        recommended_cta=opportunity.recommended_cta,
        monetization_path=opportunity.expected_monetization_path,
        why_make_today=why_make_today,
        risks_to_review="\n".join(risk_lines),
        operator_checklist="\n".join(checklist),
        production_brief="\n".join(brief_parts),
        confidence_label=confidence.value,
        selection_score=selection_score,
        empty_state_message=None,
    )


@router.get("/recommendation", response_model=ExecutiveProducerRecommendationRead)
def get_current_recommendation(db: Session = Depends(get_db)) -> ExecutiveProducerRecommendationRead:
    latest = db.scalar(
        select(ExecutiveProducerRecommendation)
        .order_by(ExecutiveProducerRecommendation.created_at.desc())
        .limit(1)
    )
    if latest is None:
        return build_empty_recommendation_read()
    return serialize_recommendation(latest)


@router.post("/recommendation/run", response_model=ExecutiveProducerRecommendationRead)
def run_recommendation(db: Session = Depends(get_db)) -> ExecutiveProducerRecommendationRead:
    opportunities = list(
        db.scalars(
            select(VideoOpportunity).where(
                VideoOpportunity.review_status.in_(
                    [
                        OpportunityReviewStatus.shortlisted.value,
                        OpportunityReviewStatus.approved_for_video.value,
                        OpportunityReviewStatus.needs_more_research.value,
                    ]
                )
            )
        )
    )
    best = choose_best_opportunity(opportunities)
    recommendation = create_recommendation_from_opportunity(best) if best else build_empty_recommendation()

    db.add(recommendation)
    db.commit()
    db.refresh(recommendation)

    log_audit_event(
        db,
        "executive_producer_recommendation_created",
        "Generated deterministic executive producer recommendation",
        metadata={
            "recommendation_id": recommendation.id,
            "selected_opportunity_id": recommendation.selected_opportunity_id,
            "confidence_label": recommendation.confidence_label,
            "has_empty_state": recommendation.empty_state_message is not None,
        },
    )
    return serialize_recommendation(recommendation)


@router.get("/history", response_model=list[ExecutiveProducerRecommendationRead])
def get_recommendation_history(
    limit: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[ExecutiveProducerRecommendationRead]:
    rows = list(
        db.scalars(
            select(ExecutiveProducerRecommendation)
            .order_by(ExecutiveProducerRecommendation.created_at.desc())
            .limit(limit)
        )
    )
    return [serialize_recommendation(row) for row in rows]
