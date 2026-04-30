from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    AuditEvent,
    ContentAgent,
    ExecutiveProducerRecommendation,
    ProductionBrief,
    ProductionBriefStatus,
    ResearchStrategy,
    Video,
    VideoOpportunity,
    VideoStatus,
)
from app.routers.executive_producer import serialize_recommendation
from app.routers.opportunities import serialize_opportunity
from app.schemas import (
    AuditEventRead,
    CommandCenterAction,
    CommandCenterBriefItem,
    CommandCenterTaskItem,
    CommandCenterTodayRead,
    ResearchSummaryRead,
)

router = APIRouter(prefix="/command-center", tags=["command-center"])


def _preview_exists(video: Video) -> bool:
    root = (get_settings().output_path / "previews").resolve()
    expected = (root / str(video.id) / "draft.mp4").resolve()
    candidates = [expected]
    if video.rendered_preview_path:
        configured = Path(video.rendered_preview_path).expanduser()
        if not configured.is_absolute():
            configured = (root / configured).resolve()
        candidates.insert(0, configured)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        if resolved.is_file():
            return True
    return False


def _task(video: Video, reason: str, target_page: str) -> CommandCenterTaskItem:
    return CommandCenterTaskItem(
        video_id=video.id,
        title=video.title,
        workflow_status=video.status.value,
        publish_status=video.publish_status,
        reason=reason,
        target_page=target_page,
    )


def _brief_task(brief: ProductionBrief, agent_name: str | None) -> CommandCenterBriefItem:
    return CommandCenterBriefItem(
        brief_id=brief.id,
        opportunity_id=brief.opportunity_id,
        status=ProductionBriefStatus(brief.status),
        title=brief.title,
        topic=brief.topic,
        agent_name=agent_name,
        target_page="briefs",
    )


@router.get("/today", response_model=CommandCenterTodayRead)
def get_command_center_today(db: Session = Depends(get_db)) -> CommandCenterTodayRead:
    videos = list(db.scalars(select(Video).order_by(Video.created_at.desc())))
    best_opportunity_row = db.scalar(
        select(VideoOpportunity).order_by(VideoOpportunity.total_score.desc(), VideoOpportunity.created_at.desc()).limit(1)
    )
    latest_recommendation_row = db.scalar(
        select(ExecutiveProducerRecommendation)
        .order_by(ExecutiveProducerRecommendation.created_at.desc())
        .limit(1)
    )
    latest_research_strategy_row = db.scalar(
        select(ResearchStrategy)
        .order_by(ResearchStrategy.created_at.desc())
        .limit(1)
    )
    recent_audit_rows = list(
        db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(12))
    )
    briefs = list(
        db.scalars(select(ProductionBrief).order_by(ProductionBrief.updated_at.desc(), ProductionBrief.created_at.desc()))
    )
    agent_map = {agent.id: agent for agent in db.scalars(select(ContentAgent))}

    blockers: list[CommandCenterTaskItem] = []
    needs_preview_review: list[CommandCenterTaskItem] = []
    needs_compliance_review: list[CommandCenterTaskItem] = []
    ready_for_packaging: list[CommandCenterTaskItem] = []
    ready_for_payload: list[CommandCenterTaskItem] = []
    briefs_needing_review: list[CommandCenterBriefItem] = []
    approved_briefs_ready_to_promote: list[CommandCenterBriefItem] = []

    for video in videos:
        preview_exists = _preview_exists(video)
        assets_generated = len(video.assets) > 0
        manual_review_pending = (
            video.status in (VideoStatus.needs_review, VideoStatus.rejected)
            or (assets_generated and not video.approved)
        )

        if video.publish_status == "blocked":
            blockers.append(_task(video, "Publishing/compliance state is blocked.", "compliance"))
        if manual_review_pending:
            needs_compliance_review.append(_task(video, "Run compliance and complete manual approval/rejection.", "compliance"))
        if video.approved and not preview_exists:
            blockers.append(_task(video, "Draft preview is missing. Render preview before packaging.", "assets"))
        if video.approved and preview_exists and not video.preview_reviewed:
            needs_preview_review.append(_task(video, "Draft preview exists but has not been manually reviewed.", "assets"))
        if video.approved and preview_exists and video.preview_reviewed and video.status == VideoStatus.approved:
            ready_for_packaging.append(_task(video, "Approved and preview-reviewed. Ready for packaging.", "assets"))
        if video.status == VideoStatus.packaged:
            ready_for_payload.append(_task(video, "Packaged and ready for YouTube payload preparation.", "publishing"))

    for brief in briefs:
        agent = agent_map.get(brief.assigned_agent_id) if brief.assigned_agent_id else None
        agent_name = agent.name if agent else None
        if brief.status in (ProductionBriefStatus.draft.value, ProductionBriefStatus.needs_revision.value):
            briefs_needing_review.append(_brief_task(brief, agent_name))
        elif brief.status == ProductionBriefStatus.approved.value:
            approved_briefs_ready_to_promote.append(_brief_task(brief, agent_name))

    best_opportunity = serialize_opportunity(best_opportunity_row) if best_opportunity_row else None
    executive_recommendation = (
        serialize_recommendation(latest_recommendation_row, db) if latest_recommendation_row else None
    )
    latest_research_strategy = (
        ResearchSummaryRead(
            run_id=latest_research_strategy_row.run_id,
            strategy_id=latest_research_strategy_row.id,
            niche_lane=latest_research_strategy_row.niche_lane,
            query=latest_research_strategy_row.query,
            trend_thesis=latest_research_strategy_row.trend_thesis,
            top_pattern=latest_research_strategy_row.top_pattern,
            recommended_next_action=latest_research_strategy_row.recommended_next_action,
            recommended_agent_name=latest_research_strategy_row.recommended_agent_name,
            created_at=latest_research_strategy_row.created_at,
        )
        if latest_research_strategy_row
        else None
    )

    assigned_agent: ContentAgent | None = None
    if latest_recommendation_row and latest_recommendation_row.matched_agent_id:
        assigned_agent = db.get(ContentAgent, latest_recommendation_row.matched_agent_id)
    if assigned_agent is None and best_opportunity_row and best_opportunity_row.assigned_agent_id:
        assigned_agent = db.get(ContentAgent, best_opportunity_row.assigned_agent_id)

    if not videos and best_opportunity is None and executive_recommendation is None and latest_research_strategy is None and not briefs:
        return CommandCenterTodayRead(
            best_opportunity=None,
            executive_recommendation=None,
            latest_research_strategy=None,
            assigned_agent=None,
            next_best_action=CommandCenterAction(
                key="setup_opportunities",
                label="Create your first opportunity",
                reason="No opportunities or videos exist yet.",
                target_page="opportunities",
                cta_label="Open Opportunities",
                video_id=None,
                opportunity_id=None,
            ),
            operator_checklist=[
                "Create at least one opportunity in Opportunities.",
                "Score and review the opportunity.",
                "Run Executive Producer to generate a daily recommendation.",
                "Promote an approved opportunity to a video idea.",
            ],
            blockers=[],
            needs_preview_review=[],
            needs_compliance_review=[],
            ready_for_packaging=[],
            ready_for_payload=[],
            briefs_needing_review=[],
            approved_briefs_ready_to_promote=[],
            recent_audit_events=[AuditEventRead.model_validate(row) for row in recent_audit_rows],
            summary_status="empty",
            summary_message="No reviewed opportunities are ready yet. Start by creating and reviewing opportunities.",
        )

    next_action = CommandCenterAction(
        key="review_audit",
        label="Review recent activity",
        reason="No urgent blockers detected.",
        target_page="audit",
        cta_label="Open Audit",
    )
    summary_status = "on_track"
    summary_message = "Pipeline is stable. Continue with the next scheduled operator action."

    if approved_briefs_ready_to_promote:
        first_brief = approved_briefs_ready_to_promote[0]
        next_action = CommandCenterAction(
            key="promote_approved_brief",
            label=f"Promote approved brief: {first_brief.title}",
            reason="An approved production brief is ready to become a video idea.",
            target_page="briefs",
            cta_label="Open Briefs",
            opportunity_id=first_brief.opportunity_id,
        )
        summary_status = "attention_needed"
        summary_message = "Approved briefs are waiting for promotion to video ideas."
    elif briefs_needing_review:
        first_brief = briefs_needing_review[0]
        next_action = CommandCenterAction(
            key="review_production_brief",
            label=f"Review production brief: {first_brief.title}",
            reason="Brief requires operator review before approval and promotion.",
            target_page="briefs",
            cta_label="Open Briefs",
            opportunity_id=first_brief.opportunity_id,
        )
        summary_status = "attention_needed"
        summary_message = "Production briefs are waiting for operator review."
    elif blockers:
        first = blockers[0]
        next_action = CommandCenterAction(
            key="resolve_blocker",
            label=f"Resolve blocker: {first.title}",
            reason=first.reason,
            target_page=first.target_page,
            cta_label="Resolve Blocker",
            video_id=first.video_id,
        )
        summary_status = "attention_needed"
        summary_message = "Blockers found. Resolve blockers before advancing workflow stages."
    elif needs_compliance_review:
        first = needs_compliance_review[0]
        next_action = CommandCenterAction(
            key="run_compliance_review",
            label=f"Review and approve: {first.title}",
            reason=first.reason,
            target_page="compliance",
            cta_label="Open Compliance",
            video_id=first.video_id,
        )
        summary_status = "attention_needed"
        summary_message = "Videos are waiting on compliance/manual review."
    elif needs_preview_review:
        first = needs_preview_review[0]
        next_action = CommandCenterAction(
            key="preview_review",
            label=f"Watch draft preview: {first.title}",
            reason=first.reason,
            target_page="assets",
            cta_label="Open Assets",
            video_id=first.video_id,
        )
        summary_status = "attention_needed"
        summary_message = "Draft previews are pending manual review."
    elif ready_for_packaging:
        first = ready_for_packaging[0]
        next_action = CommandCenterAction(
            key="package_video",
            label=f"Package video: {first.title}",
            reason=first.reason,
            target_page="assets",
            cta_label="Open Packaging",
            video_id=first.video_id,
        )
    elif ready_for_payload:
        first = ready_for_payload[0]
        next_action = CommandCenterAction(
            key="prepare_payload",
            label=f"Prepare payload: {first.title}",
            reason=first.reason,
            target_page="publishing",
            cta_label="Open Publishing",
            video_id=first.video_id,
        )
    elif best_opportunity is not None:
        next_action = CommandCenterAction(
            key="review_best_opportunity",
            label="Review best opportunity",
            reason="Opportunity queue has candidates to review or approve for production.",
            target_page="opportunities",
            cta_label="Open Opportunities",
            opportunity_id=best_opportunity.id,
        )

    operator_checklist: list[str] = [
        f"Review best opportunity: {best_opportunity.topic}" if best_opportunity else "Review opportunity queue.",
        (
            "Review Executive Producer recommendation."
            if executive_recommendation and executive_recommendation.selected_opportunity_id
            else "Run Executive Producer recommendation if needed."
        ),
        (
            f"Watch draft previews pending review ({len(needs_preview_review)})."
            if needs_preview_review
            else "No draft preview reviews pending."
        ),
        (
            f"Complete compliance/manual review for pending videos ({len(needs_compliance_review)})."
            if needs_compliance_review
            else "No compliance/manual reviews pending."
        ),
        (
            f"Package approved videos ready for packaging ({len(ready_for_packaging)})."
            if ready_for_packaging
            else "No videos currently ready for packaging."
        ),
        (
            f"Review production briefs pending approval ({len(briefs_needing_review)})."
            if briefs_needing_review
            else "No production briefs pending review."
        ),
        (
            f"Promote approved briefs to video ideas ({len(approved_briefs_ready_to_promote)})."
            if approved_briefs_ready_to_promote
            else "No approved briefs waiting for promotion."
        ),
        (
            f"Prepare YouTube payloads for packaged videos ({len(ready_for_payload)})."
            if ready_for_payload
            else "No packaged videos waiting for payload."
        ),
    ]

    return CommandCenterTodayRead(
        best_opportunity=best_opportunity,
        executive_recommendation=executive_recommendation,
        latest_research_strategy=latest_research_strategy,
        assigned_agent=(assigned_agent if assigned_agent else None),
        next_best_action=next_action,
        operator_checklist=operator_checklist,
        blockers=blockers,
        needs_preview_review=needs_preview_review,
        needs_compliance_review=needs_compliance_review,
        ready_for_packaging=ready_for_packaging,
        ready_for_payload=ready_for_payload,
        briefs_needing_review=briefs_needing_review,
        approved_briefs_ready_to_promote=approved_briefs_ready_to_promote,
        recent_audit_events=[AuditEventRead.model_validate(row) for row in recent_audit_rows],
        summary_status=summary_status,
        summary_message=summary_message,
    )
