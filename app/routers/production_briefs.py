from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Channel,
    ContentAgent,
    OpportunityReviewStatus,
    ProductionBrief,
    ProductionBriefStatus,
    Video,
    VideoOpportunity,
    VideoStatus,
)
from app.schemas import (
    ProductionBriefCreateResult,
    ProductionBriefRead,
    ProductionBriefReviewUpdate,
    VideoRead,
)
from app.services.agents import ensure_channel_agents, resolve_agent_for_opportunity
from app.services.audit import log_audit_event

router = APIRouter(prefix="/production-briefs", tags=["production-briefs"])


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def get_opportunity_or_404(db: Session, opportunity_id: int) -> VideoOpportunity:
    opportunity = db.get(VideoOpportunity, opportunity_id)
    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return opportunity


def get_brief_or_404(db: Session, brief_id: int) -> ProductionBrief:
    brief = db.get(ProductionBrief, brief_id)
    if not brief:
        raise HTTPException(status_code=404, detail="Production brief not found")
    return brief


def lane_template(lane: str) -> dict[str, str]:
    normalized = clean_text(lane).lower()
    if "tool" in normalized:
        return {
            "hook_frame": "Show the exact before/after operator workflow and where each tool adds leverage.",
            "outline_frame": "Problem -> criteria -> tool breakdown -> implementation steps -> caveats -> CTA",
            "voiceover_style": "Clear reviewer tone, practical, comparison-first, no hype.",
            "b_roll": "Dashboard screens, checklist overlays, and workflow arrows. No fake metrics or testimonials.",
        }
    if "business automation" in normalized:
        return {
            "hook_frame": "Open with the operational bottleneck and quantify time/process friction only.",
            "outline_frame": "Bottleneck -> workflow map -> implementation SOP -> risk checks -> handoff CTA",
            "voiceover_style": "Consultative operator tone with concrete process language.",
            "b_roll": "SOP boards, call flow diagrams, and CRM-like process boards using sample data.",
        }
    if "side hustle" in normalized or "business model" in normalized:
        return {
            "hook_frame": "Set expectations early: realistic model, real effort, no income guarantees.",
            "outline_frame": "Model thesis -> offer mechanics -> cost/time assumptions -> risk controls -> CTA",
            "voiceover_style": "Grounded and conservative. Explicitly reject get-rich framing.",
            "b_roll": "Cost spreadsheets, timeline cards, and process diagrams.",
        }
    if "faceless" in normalized or "creator" in normalized:
        return {
            "hook_frame": "Frame the creator workflow as an operating system, not a growth hack.",
            "outline_frame": "System overview -> daily workflow -> quality gates -> compliance checklist -> CTA",
            "voiceover_style": "Creator-operator voice, tactical and direct.",
            "b_roll": "Content pipeline board, script review cards, and packaging checklist views.",
        }
    if "ecommerce" in normalized:
        return {
            "hook_frame": "Start from store operations pain and show one workflow that improves decision speed.",
            "outline_frame": "Ops pain -> research workflow -> implementation checkpoints -> risk/caveats -> CTA",
            "voiceover_style": "Operations-first and evidence-aware.",
            "b_roll": "Product research sheets, workflow kanban, and catalog process overlays.",
        }
    if "local business" in normalized:
        return {
            "hook_frame": "Lead with missed call/lead handling pain and operator workflow fix.",
            "outline_frame": "Pain -> intake workflow -> call/appointment flow -> manual review gates -> CTA",
            "voiceover_style": "Local operator voice, practical and clear.",
            "b_roll": "Call flow maps, intake forms, and service scheduling boards.",
        }
    if "career" in normalized or "productivity" in normalized:
        return {
            "hook_frame": "Anchor on workload clarity and execution quality, not magical productivity claims.",
            "outline_frame": "Current state -> productivity workflow -> execution rhythm -> caveats -> CTA",
            "voiceover_style": "Calm, structured, and execution-focused.",
            "b_roll": "Task planning boards, weekly calendar slices, and prioritization cards.",
        }
    return {
        "hook_frame": "Start with a concrete operator pain point and explicit caveats.",
        "outline_frame": "Context -> workflow -> checkpoints -> compliance/risk notes -> CTA",
        "voiceover_style": "Direct and practical with clear disclaimers.",
        "b_roll": "Workflow visuals, checklist overlays, and process cards.",
    }


def build_brief_from_opportunity(opportunity: VideoOpportunity, agent: ContentAgent | None, channel: Channel | None) -> ProductionBrief:
    template = lane_template(opportunity.niche_lane)
    agent_name = agent.name if agent else (opportunity.assigned_agent or "Opportunity Research Agent")
    focus = clean_text(agent.focus) if agent else "Practical educational workflow content."
    production_rules = clean_text(agent.production_rules) if agent else "Keep steps reproducible and bounded."
    compliance_notes = clean_text(agent.compliance_notes) if agent else opportunity.compliance_risk_note
    monetization_focus = clean_text(agent.monetization_focus) if agent else opportunity.expected_monetization_path
    brand_voice = clean_text(channel.brand_voice) if channel else "Direct, practical, non-hype."

    title = clean_text(opportunity.recommended_title) or clean_text(opportunity.topic)
    hook = (
        f"{clean_text(opportunity.topic)} for {clean_text(opportunity.audience)}. "
        f"{template['hook_frame']} This is an educational draft plan, not performance guarantees."
    )
    outline = "\n".join(
        [
            f"1) Problem context: {clean_text(opportunity.topic)}",
            f"2) Workflow frame: {template['outline_frame']}",
            f"3) Agent perspective: {agent_name} focus on {focus}",
            "4) Compliance gate: run checklist before any approval/publishing step",
            f"5) Monetization handoff: {monetization_focus}",
        ]
    )
    script_plan = "\n".join(
        [
            "Opening (0-20s): define operator pain and set realistic expectations.",
            "Core section A: walk through workflow design and tool/process choices.",
            "Core section B: implementation checklist with manual review gates.",
            "Core section C: caveats, claims to verify, and compliance reminders.",
            f"Close: CTA -> {clean_text(opportunity.recommended_cta)}",
        ]
    )
    claims_to_verify = "\n".join(
        [
            "Any numeric outcomes must be labeled as examples unless independently verified.",
            "No income guarantees, no fake testimonials, no unsupported company claims.",
            "Disclose synthetic voice/media usage when applicable.",
        ]
    )
    brief = ProductionBrief(
        opportunity_id=opportunity.id,
        assigned_agent_id=agent.id if agent else opportunity.assigned_agent_id,
        status=ProductionBriefStatus.draft.value,
        topic=clean_text(opportunity.topic),
        niche_lane=clean_text(opportunity.niche_lane),
        target_audience=clean_text(opportunity.audience),
        monetization_path=clean_text(opportunity.expected_monetization_path or opportunity.monetization_path),
        title=title,
        thumbnail_angle=clean_text(opportunity.thumbnail_angle),
        hook=hook,
        outline=outline,
        script_plan=script_plan,
        b_roll_plan=template["b_roll"],
        voiceover_style=f"{template['voiceover_style']} Brand voice: {brand_voice}",
        cta=clean_text(opportunity.recommended_cta),
        compliance_notes=f"{compliance_notes} Keep educational framing unless claims are verified.",
        claims_to_verify=claims_to_verify,
        operator_review_notes=f"Agent: {agent_name}. Production rules: {production_rules}",
    )
    return brief


@router.post("/from-opportunity/{opportunity_id}", response_model=ProductionBriefCreateResult)
def create_brief_from_opportunity(opportunity_id: int, db: Session = Depends(get_db)) -> ProductionBriefCreateResult:
    opportunity = get_opportunity_or_404(db, opportunity_id)
    if opportunity.review_status not in (
        OpportunityReviewStatus.shortlisted.value,
        OpportunityReviewStatus.approved_for_video.value,
    ):
        raise HTTPException(status_code=400, detail="Opportunity must be shortlisted or approved_for_video to create a production brief.")

    ensure_channel_agents(db, opportunity.channel_id)
    matched_agent = resolve_agent_for_opportunity(db, opportunity)
    channel = db.get(Channel, opportunity.channel_id)
    brief = build_brief_from_opportunity(opportunity, matched_agent, channel)
    db.add(brief)
    db.commit()
    db.refresh(brief)

    log_audit_event(
        db,
        "production_brief_created",
        f"Created production brief from opportunity: {brief.topic}",
        metadata={
            "brief_id": brief.id,
            "opportunity_id": opportunity.id,
            "assigned_agent_id": brief.assigned_agent_id,
        },
    )
    return ProductionBriefCreateResult(
        brief=ProductionBriefRead.model_validate(brief),
        message="Production brief created. Next step: review and approve before promotion.",
    )


@router.get("", response_model=list[ProductionBriefRead])
def list_production_briefs(db: Session = Depends(get_db)) -> list[ProductionBriefRead]:
    rows = list(
        db.scalars(select(ProductionBrief).order_by(ProductionBrief.created_at.desc()))
    )
    return [ProductionBriefRead.model_validate(row) for row in rows]


@router.get("/{brief_id}", response_model=ProductionBriefRead)
def read_production_brief(brief_id: int, db: Session = Depends(get_db)) -> ProductionBriefRead:
    brief = get_brief_or_404(db, brief_id)
    return ProductionBriefRead.model_validate(brief)


@router.patch("/{brief_id}/review", response_model=ProductionBriefRead)
def review_production_brief(
    brief_id: int,
    payload: ProductionBriefReviewUpdate,
    db: Session = Depends(get_db),
) -> ProductionBriefRead:
    brief = get_brief_or_404(db, brief_id)
    previous_status = brief.status
    brief.status = payload.status
    if payload.operator_review_notes is not None:
        brief.operator_review_notes = clean_text(payload.operator_review_notes) or None
    db.commit()
    db.refresh(brief)

    log_audit_event(
        db,
        "production_brief_review_updated",
        f"Updated production brief status: {brief.title} -> {brief.status}",
        metadata={
            "brief_id": brief.id,
            "previous_status": previous_status,
            "status": brief.status,
        },
    )
    return ProductionBriefRead.model_validate(brief)


@router.post("/{brief_id}/promote-to-video", response_model=VideoRead)
def promote_production_brief_to_video(brief_id: int, db: Session = Depends(get_db)) -> Video:
    brief = get_brief_or_404(db, brief_id)
    if brief.status != ProductionBriefStatus.approved.value:
        raise HTTPException(status_code=400, detail="Production brief must be approved before promotion.")

    opportunity = get_opportunity_or_404(db, brief.opportunity_id)
    notes = [
        "Promoted from Production Brief.",
        "Draft planning artifact. Requires manual compliance, review, preview, and approval workflow.",
        f"Hook: {brief.hook}",
        f"Script plan: {brief.script_plan}",
        f"B-roll plan: {brief.b_roll_plan}",
        f"Claims to verify: {brief.claims_to_verify}",
    ]
    if brief.operator_review_notes:
        notes.append(f"Operator notes: {brief.operator_review_notes}")

    video = Video(
        channel_id=opportunity.channel_id,
        assigned_agent_id=brief.assigned_agent_id,
        title=brief.title,
        niche=brief.niche_lane,
        target_audience=brief.target_audience,
        angle=brief.thumbnail_angle,
        notes="\n".join(notes),
        status=VideoStatus.idea,
        approved=False,
        preview_reviewed=False,
        publish_status="draft",
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    brief.status = ProductionBriefStatus.promoted.value
    brief.promoted_video_id = video.id
    db.commit()
    db.refresh(brief)

    log_audit_event(
        db,
        "production_brief_promoted_to_video",
        f"Promoted production brief to video idea: {video.title}",
        video_id=video.id,
        metadata={
            "brief_id": brief.id,
            "opportunity_id": brief.opportunity_id,
            "video_id": video.id,
        },
    )
    return video
