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
    OpportunityReviewStatus,
    ProductionBrief,
    ProductionBriefStatus,
    ProducerConfidenceLabel,
    Video,
    VisualGeneratedAsset,
    VisualGenerationJob,
    VisualAssetPlan,
    VideoOpportunity,
    VideoStatus,
)
from app.schemas import (
    DailyPipelineRead,
    PipelineBriefItem,
    PipelineNextStep,
    PipelineOpportunityItem,
    PipelineRecommendationItem,
    PipelineSummaryCounts,
    PipelineVideoItem,
)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


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


def _video_item(video: Video, preview_rendered: bool, reason: str) -> PipelineVideoItem:
    return PipelineVideoItem(
        video_id=video.id,
        title=video.title,
        workflow_status=video.status,
        publish_status=video.publish_status,
        approved=video.approved,
        preview_rendered=preview_rendered,
        preview_reviewed=video.preview_reviewed,
        updated_at=video.updated_at,
        reason=reason,
    )


@router.get("/daily", response_model=DailyPipelineRead)
def get_daily_pipeline(db: Session = Depends(get_db)) -> DailyPipelineRead:
    opportunities = list(
        db.scalars(
            select(VideoOpportunity).order_by(VideoOpportunity.total_score.desc(), VideoOpportunity.created_at.desc())
        )
    )
    recommendations = list(
        db.scalars(
            select(ExecutiveProducerRecommendation)
            .order_by(ExecutiveProducerRecommendation.created_at.desc())
            .limit(15)
        )
    )
    briefs = list(
        db.scalars(
            select(ProductionBrief).order_by(ProductionBrief.updated_at.desc(), ProductionBrief.created_at.desc())
        )
    )
    videos = list(db.scalars(select(Video).order_by(Video.updated_at.desc(), Video.created_at.desc())))
    agent_map = {agent.id: agent for agent in db.scalars(select(ContentAgent))}
    compliance_run_video_ids = {
        video_id
        for video_id in db.scalars(
            select(AuditEvent.video_id).where(
                AuditEvent.event_type == "compliance_run",
                AuditEvent.video_id.is_not(None),
            )
        )
        if video_id is not None
    }
    visual_plan_video_ids = {
        video_id
        for video_id in db.scalars(select(VisualAssetPlan.video_id).where(VisualAssetPlan.video_id.is_not(None)))
        if video_id is not None
    }
    visual_jobs_pending_video_ids = {
        video_id
        for video_id in db.scalars(
            select(VisualAssetPlan.video_id)
            .join(VisualGenerationJob, VisualGenerationJob.visual_asset_plan_id == VisualAssetPlan.id)
            .where(
                VisualAssetPlan.video_id.is_not(None),
                VisualGenerationJob.status.in_(("queued", "exported")),
            )
        )
        if video_id is not None
    }
    visual_assets_registered_video_ids = {
        video_id
        for video_id in db.scalars(
            select(VisualAssetPlan.video_id)
            .join(VisualGeneratedAsset, VisualGeneratedAsset.visual_asset_plan_id == VisualAssetPlan.id)
            .where(
                VisualAssetPlan.video_id.is_not(None),
                VisualGeneratedAsset.file_exists.is_(True),
            )
        )
        if video_id is not None
    }

    opportunities_to_review = [
        PipelineOpportunityItem(
            id=row.id,
            topic=row.topic,
            niche_lane=row.niche_lane,
            review_status=OpportunityReviewStatus(row.review_status),
            total_score=row.total_score,
            assigned_agent_id=row.assigned_agent_id,
            assigned_agent=row.assigned_agent,
            created_at=row.created_at,
        )
        for row in opportunities
        if row.review_status
        in {
            OpportunityReviewStatus.unreviewed.value,
            OpportunityReviewStatus.shortlisted.value,
            OpportunityReviewStatus.needs_more_research.value,
        }
    ]

    producer_recommendations = [
        PipelineRecommendationItem(
            recommendation_id=row.id,
            selected_opportunity_id=row.selected_opportunity_id,
            recommended_topic=row.recommended_topic,
            niche_lane=row.niche_lane,
            assigned_agent=row.assigned_agent,
            confidence_label=ProducerConfidenceLabel(row.confidence_label),
            created_at=row.created_at,
        )
        for row in recommendations
    ]

    briefs_to_review: list[PipelineBriefItem] = []
    approved_briefs_ready_to_promote: list[PipelineBriefItem] = []
    for brief in briefs:
        assigned_agent = agent_map.get(brief.assigned_agent_id) if brief.assigned_agent_id else None
        payload = PipelineBriefItem(
            brief_id=brief.id,
            opportunity_id=brief.opportunity_id,
            title=brief.title,
            topic=brief.topic,
            status=ProductionBriefStatus(brief.status),
            assigned_agent_id=brief.assigned_agent_id,
            assigned_agent=assigned_agent.name if assigned_agent else None,
            updated_at=brief.updated_at,
        )
        if brief.status in {ProductionBriefStatus.draft.value, ProductionBriefStatus.needs_revision.value}:
            briefs_to_review.append(payload)
        elif brief.status == ProductionBriefStatus.approved.value:
            approved_briefs_ready_to_promote.append(payload)

    videos_needing_assets: list[PipelineVideoItem] = []
    videos_missing_visual_plans: list[PipelineVideoItem] = []
    videos_visual_jobs_pending: list[PipelineVideoItem] = []
    videos_visual_assets_registered: list[PipelineVideoItem] = []
    videos_needing_preview: list[PipelineVideoItem] = []
    videos_needing_preview_review: list[PipelineVideoItem] = []
    videos_needing_compliance: list[PipelineVideoItem] = []
    videos_needing_manual_approval: list[PipelineVideoItem] = []
    videos_ready_to_package: list[PipelineVideoItem] = []
    videos_ready_for_payload: list[PipelineVideoItem] = []
    completed_payloads: list[PipelineVideoItem] = []

    for video in videos:
        preview_rendered = _preview_exists(video)
        assets_generated = len(video.assets) > 0
        has_compliance_run = video.id in compliance_run_video_ids
        has_visual_plan = video.id in visual_plan_video_ids
        has_visual_jobs_pending = video.id in visual_jobs_pending_video_ids
        has_visual_assets_registered = video.id in visual_assets_registered_video_ids

        if video.approved and has_visual_assets_registered:
            videos_visual_assets_registered.append(
                _video_item(video, preview_rendered, "Registered visual assets are available for preview/render inputs.")
            )

        # Assign each video to exactly one active stage using strict priority order.
        if not assets_generated and video.status != VideoStatus.published:
            videos_needing_assets.append(
                _video_item(video, preview_rendered, "Assets are missing. Generate assets first.")
            )
            continue

        if video.approved and not has_visual_plan:
            videos_missing_visual_plans.append(
                _video_item(video, preview_rendered, "Visual plan missing. Create visual plan before preview render.")
            )
            continue

        if video.approved and has_visual_jobs_pending and not has_visual_assets_registered:
            videos_visual_jobs_pending.append(
                _video_item(video, preview_rendered, "Visual generation jobs are queued/exported; register outputs before preview render.")
            )
            continue

        if video.approved and not preview_rendered:
            videos_needing_preview.append(
                _video_item(video, preview_rendered, "Approved video is missing draft preview render.")
            )
            continue

        if video.approved and preview_rendered and not video.preview_reviewed:
            videos_needing_preview_review.append(
                _video_item(video, preview_rendered, "Draft preview exists but manual preview review is pending.")
            )
            continue

        if assets_generated and not video.approved:
            if not has_compliance_run:
                videos_needing_compliance.append(
                    _video_item(video, preview_rendered, "Run compliance checks before manual approval.")
                )
                continue

            videos_needing_manual_approval.append(
                _video_item(video, preview_rendered, "Compliance has been run; manual approval/rejection is pending.")
            )
            continue

        if (
            video.approved
            and preview_rendered
            and video.preview_reviewed
            and video.status == VideoStatus.approved
        ):
            videos_ready_to_package.append(
                _video_item(video, preview_rendered, "Approved and preview-reviewed. Ready for packaging.")
            )
            continue

        if video.status == VideoStatus.packaged:
            videos_ready_for_payload.append(
                _video_item(video, preview_rendered, "Packaged and ready for payload preparation.")
            )
            continue

        if video.status in {VideoStatus.publish_ready, VideoStatus.published} or video.publish_status in {
            "ready",
            "scheduled",
            "published_manual",
        }:
            completed_payloads.append(
                _video_item(video, preview_rendered, "Payload prepared or publishing state advanced.")
            )

    next_step = PipelineNextStep(
        key="pipeline_clear",
        label="Pipeline is clear",
        reason="No urgent production blockers were detected.",
        target_page="audit",
    )
    if opportunities_to_review:
        top = opportunities_to_review[0]
        next_step = PipelineNextStep(
            key="review_opportunities",
            label=f"Review opportunity: {top.topic}",
            reason="Opportunities are waiting for operator review/decision.",
            target_page="opportunities",
            opportunity_id=top.id,
        )
    elif not producer_recommendations and opportunities:
        next_step = PipelineNextStep(
            key="run_producer",
            label="Run Executive Producer",
            reason="Generate a deterministic recommendation from reviewed opportunities.",
            target_page="producer",
        )
    elif briefs_to_review:
        top = briefs_to_review[0]
        next_step = PipelineNextStep(
            key="review_brief",
            label=f"Review brief: {top.title}",
            reason="Production brief requires review before approval and promotion.",
            target_page="briefs",
            brief_id=top.brief_id,
            opportunity_id=top.opportunity_id,
        )
    elif approved_briefs_ready_to_promote:
        top = approved_briefs_ready_to_promote[0]
        next_step = PipelineNextStep(
            key="promote_brief",
            label=f"Promote approved brief: {top.title}",
            reason="Approved brief is ready to become a video idea.",
            target_page="briefs",
            brief_id=top.brief_id,
            opportunity_id=top.opportunity_id,
        )
    elif videos_needing_assets:
        top = videos_needing_assets[0]
        next_step = PipelineNextStep(
            key="generate_assets",
            label=f"Generate assets: {top.title}",
            reason=top.reason or "Assets missing.",
            target_page="assets",
            video_id=top.video_id,
        )
    elif videos_missing_visual_plans:
        top = videos_missing_visual_plans[0]
        next_step = PipelineNextStep(
            key="create_visual_plan",
            label=f"Create visual plan: {top.title}",
            reason=top.reason or "Visual plan missing.",
            target_page="assets",
            video_id=top.video_id,
        )
    elif videos_visual_jobs_pending:
        top = videos_visual_jobs_pending[0]
        next_step = PipelineNextStep(
            key="register_visual_outputs",
            label=f"Register visual outputs: {top.title}",
            reason=top.reason or "Visual jobs are pending output registration.",
            target_page="assets",
            video_id=top.video_id,
        )
    elif videos_needing_preview:
        top = videos_needing_preview[0]
        next_step = PipelineNextStep(
            key="render_preview",
            label=f"Render draft preview: {top.title}",
            reason=top.reason or "Preview missing.",
            target_page="assets",
            video_id=top.video_id,
        )
    elif videos_needing_preview_review:
        top = videos_needing_preview_review[0]
        next_step = PipelineNextStep(
            key="review_preview",
            label=f"Watch draft preview: {top.title}",
            reason=top.reason or "Preview review pending.",
            target_page="assets",
            video_id=top.video_id,
        )
    elif videos_needing_compliance:
        top = videos_needing_compliance[0]
        next_step = PipelineNextStep(
            key="run_compliance",
            label=f"Run compliance: {top.title}",
            reason=top.reason or "Compliance check pending.",
            target_page="compliance",
            video_id=top.video_id,
        )
    elif videos_needing_manual_approval:
        top = videos_needing_manual_approval[0]
        next_step = PipelineNextStep(
            key="manual_approval",
            label=f"Manual review: {top.title}",
            reason=top.reason or "Manual approval pending.",
            target_page="compliance",
            video_id=top.video_id,
        )
    elif videos_ready_to_package:
        top = videos_ready_to_package[0]
        next_step = PipelineNextStep(
            key="package_video",
            label=f"Package video: {top.title}",
            reason=top.reason or "Ready for packaging.",
            target_page="assets",
            video_id=top.video_id,
        )
    elif videos_ready_for_payload:
        top = videos_ready_for_payload[0]
        next_step = PipelineNextStep(
            key="prepare_payload",
            label=f"Prepare payload: {top.title}",
            reason=top.reason or "Ready for payload.",
            target_page="publishing",
            video_id=top.video_id,
        )

    summary_counts = PipelineSummaryCounts(
        opportunities_to_review=len(opportunities_to_review),
        producer_recommendations=len(producer_recommendations),
        briefs_to_review=len(briefs_to_review),
        approved_briefs_ready_to_promote=len(approved_briefs_ready_to_promote),
        videos_needing_assets=len(videos_needing_assets),
        videos_missing_visual_plans=len(videos_missing_visual_plans),
        videos_visual_jobs_pending=len(videos_visual_jobs_pending),
        videos_visual_assets_registered=len(videos_visual_assets_registered),
        videos_needing_preview=len(videos_needing_preview),
        videos_needing_preview_review=len(videos_needing_preview_review),
        videos_needing_compliance=len(videos_needing_compliance),
        videos_needing_manual_approval=len(videos_needing_manual_approval),
        videos_ready_to_package=len(videos_ready_to_package),
        videos_ready_for_payload=len(videos_ready_for_payload),
        completed_payloads=len(completed_payloads),
    )

    return DailyPipelineRead(
        opportunities_to_review=opportunities_to_review,
        producer_recommendations=producer_recommendations,
        briefs_to_review=briefs_to_review,
        approved_briefs_ready_to_promote=approved_briefs_ready_to_promote,
        videos_needing_assets=videos_needing_assets,
        videos_missing_visual_plans=videos_missing_visual_plans,
        videos_visual_jobs_pending=videos_visual_jobs_pending,
        videos_visual_assets_registered=videos_visual_assets_registered,
        videos_needing_preview=videos_needing_preview,
        videos_needing_preview_review=videos_needing_preview_review,
        videos_needing_compliance=videos_needing_compliance,
        videos_needing_manual_approval=videos_needing_manual_approval,
        videos_ready_to_package=videos_ready_to_package,
        videos_ready_for_payload=videos_ready_for_payload,
        completed_payloads=completed_payloads,
        summary_counts=summary_counts,
        next_step=next_step,
    )
