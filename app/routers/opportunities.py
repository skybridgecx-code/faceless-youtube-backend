from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Channel, OpportunityReviewStatus, Video, VideoOpportunity, VideoStatus
from app.schemas import (
    OpportunityCreate,
    OpportunityRead,
    OpportunityReviewUpdate,
    OpportunityScoreBreakdown,
    VideoRead,
)
from app.services.agents import ensure_channel_agents, first_active_agent_name, maybe_assign_agent_to_opportunity
from app.services.audit import log_audit_event

router = APIRouter(prefix="/opportunities", tags=["opportunities"])

REVIEW_STATUS_PRIORITY = {
    OpportunityReviewStatus.shortlisted.value: 0,
    OpportunityReviewStatus.needs_more_research.value: 1,
    OpportunityReviewStatus.unreviewed.value: 2,
    OpportunityReviewStatus.approved_for_video.value: 3,
    OpportunityReviewStatus.rejected.value: 4,
}


def clamp_score(value: int) -> int:
    return max(1, min(5, value))


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def has_any(text: str, terms: set[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def score_opportunity_fields(topic: str, audience: str, monetization_path: str, notes: str) -> dict[str, object]:
    combined = " ".join([topic, audience, monetization_path, notes]).lower()

    demand_terms = {"ai", "automation", "tools", "workflow", "how", "best", "guide", "audit", "system"}
    buyer_terms = {"owners", "business", "local", "service", "agency", "ecommerce", "operators", "leads", "sales"}
    affiliate_terms = {"tool", "software", "stack", "comparison", "review", "setup", "platform", "app"}
    sponsor_terms = {"b2b", "saas", "local", "business", "ecommerce", "operators", "growth", "automation"}
    hard_terms = {"interview", "case study", "full build", "from scratch", "podcast", "shoot", "field demo"}
    easy_terms = {"tutorial", "checklist", "framework", "workflow", "breakdown", "template"}
    risky_terms = {"guarantee", "income", "revenue", "profit", "client results", "10x", "make money fast"}
    blocked_terms = {"get rich", "guaranteed", "actual client results", "this company uses"}
    trend_terms = {"2026", "latest", "new", "now", "ai", "automation", "agent", "local ai"}
    product_terms = {"offer", "service", "audit", "consult", "course", "affiliate", "sponsor", "software"}

    search_demand = clamp_score(2 + (1 if len(topic.split()) >= 4 else 0) + min(2, has_any(topic, demand_terms)))
    buyer_intent = clamp_score(2 + min(2, has_any(combined, buyer_terms)) + (1 if "how to" in topic.lower() else 0))
    affiliate_potential = clamp_score(1 + min(3, has_any(combined, affiliate_terms)))
    sponsorship_potential = clamp_score(1 + min(3, has_any(combined, sponsor_terms)))

    production_difficulty = 3 + min(2, has_any(combined, hard_terms)) - min(2, has_any(combined, easy_terms))
    production_difficulty = clamp_score(production_difficulty)

    compliance_risk = 2 + min(2, has_any(combined, risky_terms)) + min(2, has_any(combined, blocked_terms))
    compliance_risk = clamp_score(compliance_risk)

    trend_freshness = clamp_score(1 + min(4, has_any(combined, trend_terms)))
    product_connection = clamp_score(1 + min(4, has_any(combined, product_terms)))

    positive = search_demand + buyer_intent + affiliate_potential + sponsorship_potential + trend_freshness + product_connection
    total_score = positive + (6 - production_difficulty) + (6 - compliance_risk)

    monetization_lower = monetization_path.lower()
    if "affiliate" in monetization_lower:
        expected_path = "Affiliate offers and tool stack recommendations"
        cta = "Comment your current tool stack and biggest bottleneck."
    elif "sponsor" in monetization_lower:
        expected_path = "Sponsorship-ready educational lane for operator audiences"
        cta = "Share your niche and we will map sponsor-fit angles."
    elif any(term in monetization_lower for term in ["service", "consult", "audit", "lead"]):
        expected_path = "Service lead generation from educational operator content"
        cta = "Book an automation audit if you want this mapped to your business."
    else:
        expected_path = clean_text(monetization_path) or "Educational content with clear offer handoff"
        cta = "Tell us which step you want operationalized next."

    recommended_title = clean_text(topic)
    if recommended_title and not recommended_title.lower().startswith(("how", "best", "why")):
        recommended_title = f"How to {recommended_title}"

    risk_note = "Low estimated compliance risk, but manual compliance review is still required."
    if compliance_risk >= 4:
        risk_note = "Higher estimated compliance risk. Avoid unsupported claims and run full checklist before approval."
    elif compliance_risk == 3:
        risk_note = "Moderate estimated compliance risk. Review wording around claims and disclosures."

    why_make_this = (
        "Estimated opportunity only (deterministic local score). "
        f"This topic shows search demand {search_demand}/5, buyer intent {buyer_intent}/5, and product connection {product_connection}/5. "
        "Use it to prioritize production order, then validate with real analytics later."
    )

    return {
        "search_demand": search_demand,
        "buyer_intent": buyer_intent,
        "affiliate_potential": affiliate_potential,
        "sponsorship_potential": sponsorship_potential,
        "production_difficulty": production_difficulty,
        "compliance_risk": compliance_risk,
        "trend_freshness": trend_freshness,
        "product_connection": product_connection,
        "total_score": total_score,
        "expected_monetization_path": expected_path,
        "why_make_this": why_make_this,
        "recommended_title": recommended_title or "Untitled Opportunity",
        "thumbnail_angle": f"{clean_text(topic)}: show problem-to-outcome for {clean_text(audience) or 'operators'}",
        "recommended_cta": cta,
        "compliance_risk_note": risk_note,
    }


def apply_scores(opportunity: VideoOpportunity) -> None:
    notes = clean_text(opportunity.notes)
    score_data = score_opportunity_fields(
        topic=clean_text(opportunity.topic),
        audience=clean_text(opportunity.audience),
        monetization_path=clean_text(opportunity.monetization_path),
        notes=notes,
    )
    opportunity.search_demand = int(score_data["search_demand"])
    opportunity.buyer_intent = int(score_data["buyer_intent"])
    opportunity.affiliate_potential = int(score_data["affiliate_potential"])
    opportunity.sponsorship_potential = int(score_data["sponsorship_potential"])
    opportunity.production_difficulty = int(score_data["production_difficulty"])
    opportunity.compliance_risk = int(score_data["compliance_risk"])
    opportunity.trend_freshness = int(score_data["trend_freshness"])
    opportunity.product_connection = int(score_data["product_connection"])
    opportunity.total_score = int(score_data["total_score"])
    opportunity.expected_monetization_path = str(score_data["expected_monetization_path"])
    opportunity.why_make_this = str(score_data["why_make_this"])
    opportunity.recommended_title = str(score_data["recommended_title"])
    opportunity.thumbnail_angle = str(score_data["thumbnail_angle"])
    opportunity.recommended_cta = str(score_data["recommended_cta"])
    opportunity.compliance_risk_note = str(score_data["compliance_risk_note"])
    opportunity.scored_at = datetime.utcnow()


def serialize_opportunity(opportunity: VideoOpportunity) -> OpportunityRead:
    score = OpportunityScoreBreakdown(
        search_demand=opportunity.search_demand,
        buyer_intent=opportunity.buyer_intent,
        affiliate_potential=opportunity.affiliate_potential,
        sponsorship_potential=opportunity.sponsorship_potential,
        production_difficulty=opportunity.production_difficulty,
        compliance_risk=opportunity.compliance_risk,
        trend_freshness=opportunity.trend_freshness,
        product_connection=opportunity.product_connection,
        total_score=opportunity.total_score,
    )
    return OpportunityRead(
        id=opportunity.id,
        channel_id=opportunity.channel_id,
        assigned_agent_id=opportunity.assigned_agent_id,
        topic=opportunity.topic,
        niche_lane=opportunity.niche_lane,
        audience=opportunity.audience,
        monetization_path=opportunity.monetization_path,
        notes=opportunity.notes,
        score=score,
        expected_monetization_path=opportunity.expected_monetization_path,
        why_make_this=opportunity.why_make_this,
        recommended_title=opportunity.recommended_title,
        thumbnail_angle=opportunity.thumbnail_angle,
        recommended_cta=opportunity.recommended_cta,
        assigned_agent=opportunity.assigned_agent,
        compliance_risk_note=opportunity.compliance_risk_note,
        review_status=OpportunityReviewStatus(opportunity.review_status),
        operator_notes=opportunity.operator_notes,
        rejection_reason=opportunity.rejection_reason,
        decision_summary=opportunity.decision_summary,
        reviewed_at=opportunity.reviewed_at,
        promoted_video_id=opportunity.promoted_video_id,
        promoted_at=opportunity.promoted_at,
        scored_at=opportunity.scored_at,
        created_at=opportunity.created_at,
        updated_at=opportunity.updated_at,
    )


def get_opportunity_or_404(db: Session, opportunity_id: int) -> VideoOpportunity:
    opportunity = db.get(VideoOpportunity, opportunity_id)
    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return opportunity


@router.get("", response_model=list[OpportunityRead])
def list_opportunities(db: Session = Depends(get_db)) -> list[OpportunityRead]:
    rows = list(db.scalars(select(VideoOpportunity).order_by(VideoOpportunity.total_score.desc(), VideoOpportunity.created_at.desc())))
    return [serialize_opportunity(row) for row in rows]


@router.get("/top", response_model=list[OpportunityRead])
def list_top_opportunities(
    limit: int = Query(default=5, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[OpportunityRead]:
    rows = list(
        db.scalars(
            select(VideoOpportunity)
            .order_by(VideoOpportunity.total_score.desc(), VideoOpportunity.created_at.desc())
            .limit(limit)
        )
    )
    return [serialize_opportunity(row) for row in rows]


@router.get("/review-queue", response_model=list[OpportunityRead])
def list_review_queue(
    review_status: OpportunityReviewStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[OpportunityRead]:
    stmt = select(VideoOpportunity)
    if review_status is not None:
        stmt = stmt.where(VideoOpportunity.review_status == review_status.value)

    rows = list(db.scalars(stmt))
    rows.sort(
        key=lambda item: (
            REVIEW_STATUS_PRIORITY.get(item.review_status, 99),
            -item.total_score,
            -item.created_at.timestamp(),
        )
    )
    return [serialize_opportunity(row) for row in rows[:limit]]


@router.post("", response_model=OpportunityRead)
def create_opportunity(payload: OpportunityCreate, db: Session = Depends(get_db)) -> OpportunityRead:
    channel = db.get(Channel, payload.channel_id)
    if not channel:
        channel = db.scalar(select(Channel).order_by(Channel.created_at.asc()).limit(1))
    if not channel:
        raise HTTPException(status_code=404, detail="No channel found. Create a channel before adding opportunities.")
    agents = ensure_channel_agents(db, channel.id)

    opportunity = VideoOpportunity(
        channel_id=channel.id,
        topic=payload.topic,
        niche_lane=payload.niche_lane,
        audience=payload.audience,
        monetization_path=payload.monetization_path,
        notes=payload.notes,
        assigned_agent=first_active_agent_name(agents),
        review_status=OpportunityReviewStatus.unreviewed.value,
    )
    apply_scores(opportunity)
    db.add(opportunity)
    db.commit()
    db.refresh(opportunity)
    maybe_assign_agent_to_opportunity(db, opportunity, reason="opportunity_created")
    db.commit()
    db.refresh(opportunity)

    log_audit_event(
        db,
        "opportunity_created",
        f"Created video opportunity: {opportunity.topic}",
        metadata={
            "opportunity_id": opportunity.id,
            "channel_id": opportunity.channel_id,
            "total_score": opportunity.total_score,
            "estimated": True,
        },
    )

    return serialize_opportunity(opportunity)


@router.patch("/{opportunity_id}/review", response_model=OpportunityRead)
def review_opportunity(
    opportunity_id: int,
    payload: OpportunityReviewUpdate,
    db: Session = Depends(get_db),
) -> OpportunityRead:
    opportunity = get_opportunity_or_404(db, opportunity_id)
    previous_status = opportunity.review_status
    next_status = payload.review_status.value

    opportunity.review_status = next_status
    opportunity.operator_notes = clean_text(payload.operator_notes) or None
    opportunity.decision_summary = clean_text(payload.decision_summary) or None
    opportunity.rejection_reason = clean_text(payload.rejection_reason) or None

    if next_status == OpportunityReviewStatus.unreviewed.value:
        opportunity.reviewed_at = None
    elif previous_status != next_status and (
        previous_status == OpportunityReviewStatus.unreviewed.value or opportunity.reviewed_at is None
    ):
        opportunity.reviewed_at = datetime.utcnow()
    maybe_assign_agent_to_opportunity(db, opportunity, reason="opportunity_review_updated")

    db.commit()
    db.refresh(opportunity)

    log_audit_event(
        db,
        "opportunity_review_updated",
        f"Updated opportunity review status: {opportunity.topic} -> {next_status}",
        metadata={
            "opportunity_id": opportunity.id,
            "previous_status": previous_status,
            "review_status": next_status,
            "has_operator_notes": bool(opportunity.operator_notes),
            "has_decision_summary": bool(opportunity.decision_summary),
            "has_rejection_reason": bool(opportunity.rejection_reason),
        },
    )
    return serialize_opportunity(opportunity)


@router.post("/{opportunity_id}/score", response_model=OpportunityRead)
def score_opportunity(opportunity_id: int, db: Session = Depends(get_db)) -> OpportunityRead:
    opportunity = get_opportunity_or_404(db, opportunity_id)
    ensure_channel_agents(db, opportunity.channel_id)
    apply_scores(opportunity)
    maybe_assign_agent_to_opportunity(db, opportunity, reason="opportunity_scored")
    db.commit()
    db.refresh(opportunity)

    log_audit_event(
        db,
        "opportunity_scored",
        f"Scored opportunity: {opportunity.topic}",
        metadata={
            "opportunity_id": opportunity.id,
            "total_score": opportunity.total_score,
            "estimated": True,
        },
    )

    return serialize_opportunity(opportunity)


@router.post("/{opportunity_id}/promote-to-video", response_model=VideoRead)
def promote_opportunity_to_video(opportunity_id: int, db: Session = Depends(get_db)) -> Video:
    opportunity = get_opportunity_or_404(db, opportunity_id)
    if opportunity.review_status != OpportunityReviewStatus.approved_for_video.value:
        raise HTTPException(status_code=400, detail="Opportunity must be approved for video before promotion.")
    channel = db.get(Channel, opportunity.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found for opportunity")

    notes = [
        "Promoted from Opportunity Research Agent.",
        "Scores are deterministic local estimates, not real analytics.",
        f"Expected monetization path: {opportunity.expected_monetization_path}",
        f"Suggested CTA: {opportunity.recommended_cta}",
    ]
    if opportunity.notes:
        notes.append(f"Operator notes: {opportunity.notes}")
    if opportunity.assigned_agent:
        notes.append(f"Assigned content agent: {opportunity.assigned_agent}")

    video = Video(
        channel_id=opportunity.channel_id,
        assigned_agent_id=opportunity.assigned_agent_id,
        title=opportunity.recommended_title,
        niche=opportunity.niche_lane,
        target_audience=opportunity.audience,
        angle=opportunity.thumbnail_angle,
        notes="\n".join(notes),
        status=VideoStatus.idea,
        approved=False,
        publish_status="draft",
        preview_reviewed=False,
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    opportunity.promoted_video_id = video.id
    opportunity.promoted_at = datetime.utcnow()
    db.commit()

    log_audit_event(
        db,
        "opportunity_promoted",
        f"Promoted opportunity to video idea: {video.title}",
        video_id=video.id,
        metadata={
            "opportunity_id": opportunity.id,
            "estimated": True,
            "video_id": video.id,
        },
    )

    return video
