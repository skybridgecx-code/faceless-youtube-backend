from __future__ import annotations

import json
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    Channel,
    ContentAgent,
    ResearchPattern,
    ResearchRun,
    ResearchSourceChannel,
    ResearchSourceVideo,
    ResearchStrategy,
    VideoOpportunity,
)
from app.routers.opportunities import apply_scores
from app.schemas import (
    OpportunityRead,
    ResearchCreateOpportunitiesResult,
    ResearchPatternRead,
    ResearchRunDetailRead,
    ResearchRunRead,
    ResearchRunRequest,
    ResearchSourceChannelRead,
    ResearchSourceVideoRead,
    ResearchStrategyRead,
    ResearchSummaryRead,
)
from app.services.agents import ensure_channel_agents, first_active_agent_name, maybe_assign_agent_to_opportunity, resolve_agent_for_opportunity
from app.services.audit import log_audit_event
from app.services.opportunity_intake import normalize_text
from app.services.research import (
    ResearchFetchError,
    ResearchSetupRequiredError,
    build_research_patterns,
    build_research_strategy,
    fetch_youtube_sources,
)

router = APIRouter(prefix="/research", tags=["research"])


def _resolve_youtube_api_key() -> str:
    env_value = os.getenv("YOUTUBE_DATA_API_KEY", "").strip()
    if env_value:
        return env_value
    return (get_settings().youtube_data_api_key or "").strip()


def _channel_or_404(db: Session, channel_id: int | None = None) -> Channel:
    channel: Channel | None = None
    if channel_id is not None:
        channel = db.get(Channel, channel_id)
    if channel is None:
        channel = db.scalar(select(Channel).order_by(Channel.created_at.asc()).limit(1))
    if channel is None:
        raise HTTPException(status_code=404, detail="No channel found. Create a channel before running research.")
    return channel


def _parse_topics(raw_topics_json: str | None) -> list[str]:
    if not raw_topics_json:
        return []
    try:
        parsed = json.loads(raw_topics_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _serialize_strategy_summary(strategy: ResearchStrategy) -> ResearchSummaryRead:
    return ResearchSummaryRead(
        run_id=strategy.run_id,
        strategy_id=strategy.id,
        niche_lane=strategy.niche_lane,
        query=strategy.query,
        trend_thesis=strategy.trend_thesis,
        top_pattern=strategy.top_pattern,
        recommended_next_action=strategy.recommended_next_action,
        recommended_agent_name=strategy.recommended_agent_name,
        created_at=strategy.created_at,
    )


def _serialize_strategy(strategy: ResearchStrategy) -> ResearchStrategyRead:
    return ResearchStrategyRead(
        id=strategy.id,
        run_id=strategy.run_id,
        assigned_agent_id=strategy.assigned_agent_id,
        recommended_agent_name=strategy.recommended_agent_name,
        niche_lane=strategy.niche_lane,
        query=strategy.query,
        trend_thesis=strategy.trend_thesis,
        winning_patterns=strategy.winning_patterns,
        original_video_angles=strategy.original_video_angles,
        recommended_topics=_parse_topics(strategy.recommended_topics_json),
        title_directions=strategy.title_directions,
        thumbnail_directions=strategy.thumbnail_directions,
        hook_directions=strategy.hook_directions,
        monetization_path=strategy.monetization_path,
        differentiation_strategy=strategy.differentiation_strategy,
        what_not_to_copy=strategy.what_not_to_copy,
        compliance_risks=strategy.compliance_risks,
        recommended_next_action=strategy.recommended_next_action,
        top_pattern=strategy.top_pattern,
        created_at=strategy.created_at,
        updated_at=strategy.updated_at,
    )


def _serialize_run(
    run: ResearchRun,
    strategy: ResearchStrategy | None,
) -> ResearchRunRead:
    return ResearchRunRead(
        id=run.id,
        channel_id=run.channel_id,
        assigned_agent_id=run.assigned_agent_id,
        niche_lane=run.niche_lane,
        query=run.query,
        max_results=run.max_results,
        status=run.status,
        setup_required=run.setup_required,
        setup_message=run.setup_message,
        source_video_count=run.source_video_count,
        source_channel_count=run.source_channel_count,
        created_at=run.created_at,
        updated_at=run.updated_at,
        strategy=_serialize_strategy_summary(strategy) if strategy else None,
    )


def _serialize_run_detail(db: Session, run: ResearchRun) -> ResearchRunDetailRead:
    source_videos = list(
        db.scalars(
            select(ResearchSourceVideo)
            .where(ResearchSourceVideo.run_id == run.id)
            .order_by(ResearchSourceVideo.position.asc(), ResearchSourceVideo.id.asc())
        )
    )
    source_channels = list(
        db.scalars(
            select(ResearchSourceChannel)
            .where(ResearchSourceChannel.run_id == run.id)
            .order_by(ResearchSourceChannel.id.asc())
        )
    )
    patterns = list(
        db.scalars(
            select(ResearchPattern)
            .where(ResearchPattern.run_id == run.id)
            .order_by(ResearchPattern.id.asc())
        )
    )
    strategy = db.scalar(
        select(ResearchStrategy)
        .where(ResearchStrategy.run_id == run.id)
        .order_by(ResearchStrategy.created_at.desc())
        .limit(1)
    )
    base = _serialize_run(run, strategy)
    return ResearchRunDetailRead(
        **base.model_dump(),
        source_videos=[ResearchSourceVideoRead.model_validate(item) for item in source_videos],
        source_channels=[ResearchSourceChannelRead.model_validate(item) for item in source_channels],
        patterns=[ResearchPatternRead.model_validate(item) for item in patterns],
        strategy_detail=_serialize_strategy(strategy) if strategy else None,
    )


@router.post("/youtube/run", response_model=ResearchRunDetailRead)
def run_youtube_research(payload: ResearchRunRequest, db: Session = Depends(get_db)) -> ResearchRunDetailRead:
    channel = _channel_or_404(db)
    ensure_channel_agents(db, channel.id)

    assigned_agent: ContentAgent | None = None
    if payload.assigned_agent_id is not None:
        assigned_agent = db.get(ContentAgent, payload.assigned_agent_id)
        if assigned_agent is None:
            raise HTTPException(status_code=404, detail="Assigned agent not found.")

    run = ResearchRun(
        channel_id=channel.id,
        assigned_agent_id=assigned_agent.id if assigned_agent else None,
        niche_lane=payload.niche_lane.strip(),
        query=payload.query.strip(),
        max_results=max(1, min(25, int(payload.max_results))),
        status="pending",
        setup_required=False,
        setup_message=None,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    api_key = _resolve_youtube_api_key()
    if not api_key:
        run.status = "setup_required"
        run.setup_required = True
        run.setup_message = "YOUTUBE_DATA_API_KEY is missing. Add it to run official YouTube research."
        run.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(run)
        return _serialize_run_detail(db, run)

    try:
        source_videos, source_channels = fetch_youtube_sources(
            api_key=api_key,
            query=run.query,
            max_results=run.max_results,
        )
    except ResearchSetupRequiredError as exc:
        run.status = "setup_required"
        run.setup_required = True
        run.setup_message = str(exc)
        run.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(run)
        return _serialize_run_detail(db, run)
    except ResearchFetchError as exc:
        run.status = "failed"
        run.setup_required = False
        run.setup_message = str(exc)
        run.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(run)
        return _serialize_run_detail(db, run)

    for idx, video in enumerate(source_videos, start=1):
        db.add(
            ResearchSourceVideo(
                run_id=run.id,
                youtube_video_id=video.youtube_video_id,
                youtube_channel_id=video.youtube_channel_id,
                title=video.title,
                channel_title=video.channel_title,
                description_snippet=video.description,
                published_at=video.published_at,
                duration=video.duration,
                view_count=video.view_count,
                like_count=video.like_count,
                comment_count=video.comment_count,
                thumbnail_url=video.thumbnail_url,
                position=idx,
            )
        )

    for channel_row in source_channels:
        db.add(
            ResearchSourceChannel(
                run_id=run.id,
                youtube_channel_id=channel_row.youtube_channel_id,
                title=channel_row.title,
                description_snippet=channel_row.description,
                subscriber_count=channel_row.subscriber_count,
                video_count=channel_row.video_count,
                view_count=channel_row.view_count,
            )
        )

    patterns = build_research_patterns(
        niche_lane=run.niche_lane,
        query=run.query,
        source_videos=source_videos,
        source_channels=source_channels,
    )
    for pattern in patterns:
        db.add(
            ResearchPattern(
                run_id=run.id,
                pattern_type=pattern.pattern_type,
                label=pattern.label,
                details=pattern.details,
                signal_strength=pattern.signal_strength,
            )
        )

    strategy_result = build_research_strategy(
        niche_lane=run.niche_lane,
        query=run.query,
        patterns=patterns,
    )
    strategy = ResearchStrategy(
        run_id=run.id,
        assigned_agent_id=assigned_agent.id if assigned_agent else None,
        recommended_agent_name=assigned_agent.name if assigned_agent else None,
        niche_lane=run.niche_lane,
        query=run.query,
        trend_thesis=strategy_result.trend_thesis,
        winning_patterns=strategy_result.winning_patterns,
        original_video_angles=strategy_result.original_video_angles,
        recommended_topics_json=json.dumps(strategy_result.recommended_topics),
        title_directions=strategy_result.title_directions,
        thumbnail_directions=strategy_result.thumbnail_directions,
        hook_directions=strategy_result.hook_directions,
        monetization_path=strategy_result.monetization_path,
        differentiation_strategy=strategy_result.differentiation_strategy,
        what_not_to_copy=strategy_result.what_not_to_copy,
        compliance_risks=strategy_result.compliance_risks,
        recommended_next_action=strategy_result.recommended_next_action,
        top_pattern=strategy_result.top_pattern,
    )
    db.add(strategy)

    run.status = "completed"
    run.setup_required = False
    run.setup_message = None
    run.source_video_count = len(source_videos)
    run.source_channel_count = len(source_channels)
    run.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(run)
    return _serialize_run_detail(db, run)


@router.get("/runs", response_model=list[ResearchRunRead])
def list_research_runs(
    limit: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[ResearchRunRead]:
    rows = list(
        db.scalars(
            select(ResearchRun)
            .order_by(ResearchRun.created_at.desc())
            .limit(limit)
        )
    )
    strategy_by_run: dict[int, ResearchStrategy] = {}
    for strategy in db.scalars(select(ResearchStrategy).order_by(ResearchStrategy.created_at.desc())):
        if strategy.run_id not in strategy_by_run:
            strategy_by_run[strategy.run_id] = strategy
    return [_serialize_run(run, strategy_by_run.get(run.id)) for run in rows]


@router.get("/runs/{run_id}", response_model=ResearchRunDetailRead)
def get_research_run(run_id: int, db: Session = Depends(get_db)) -> ResearchRunDetailRead:
    run = db.get(ResearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Research run not found.")
    return _serialize_run_detail(db, run)


@router.get("/strategies", response_model=list[ResearchStrategyRead])
def list_research_strategies(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[ResearchStrategyRead]:
    rows = list(
        db.scalars(
            select(ResearchStrategy)
            .order_by(ResearchStrategy.created_at.desc())
            .limit(limit)
        )
    )
    return [_serialize_strategy(row) for row in rows]


@router.post("/runs/{run_id}/create-opportunities", response_model=ResearchCreateOpportunitiesResult)
def create_opportunities_from_strategy(run_id: int, db: Session = Depends(get_db)) -> ResearchCreateOpportunitiesResult:
    run = db.get(ResearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Research run not found.")
    strategy = db.scalar(
        select(ResearchStrategy)
        .where(ResearchStrategy.run_id == run.id)
        .order_by(ResearchStrategy.created_at.desc())
        .limit(1)
    )
    if strategy is None:
        raise HTTPException(status_code=409, detail="No strategy found for this research run.")

    channel = _channel_or_404(db, run.channel_id)
    agents = ensure_channel_agents(db, channel.id)
    suggested_agent = db.get(ContentAgent, strategy.assigned_agent_id) if strategy.assigned_agent_id else None
    recommended_topics = _parse_topics(strategy.recommended_topics_json)
    if not recommended_topics:
        raise HTTPException(status_code=409, detail="Strategy has no recommended topics to promote.")

    existing = list(db.scalars(select(VideoOpportunity).where(VideoOpportunity.channel_id == channel.id)))
    existing_pairs = {
        (normalize_text(row.topic), normalize_text(row.niche_lane))
        for row in existing
    }

    created_ids: list[int] = []
    skipped_duplicates = 0
    for topic in recommended_topics:
        pair = (normalize_text(topic), normalize_text(strategy.niche_lane))
        if pair in existing_pairs:
            skipped_duplicates += 1
            continue

        opportunity = VideoOpportunity(
            channel_id=channel.id,
            assigned_agent_id=suggested_agent.id if suggested_agent else None,
            topic=topic,
            niche_lane=strategy.niche_lane,
            audience=f"Operators interested in {strategy.niche_lane}",
            monetization_path=strategy.monetization_path[:240],
            notes=(
                f"Research-backed deterministic opportunity from run #{run.id}, strategy #{strategy.id}. "
                "Estimate only, not real analytics."
            ),
            assigned_agent=(suggested_agent.name if suggested_agent else first_active_agent_name(agents)),
            review_status="unreviewed",
            expected_monetization_path=strategy.monetization_path[:240] or "Educational content with clear offer handoff",
            why_make_this=(
                "Derived from deterministic supervisor research patterns. "
                "Treat as an original strategy estimate and validate manually."
            ),
            recommended_title=f"How to {topic}" if not topic.lower().startswith(("how", "best", "why")) else topic,
            thumbnail_angle=strategy.thumbnail_directions[:240] or f"{topic}: show problem-to-outcome workflow",
            recommended_cta="Comment your workflow bottleneck and the lane you want next.",
            compliance_risk_note="Research-derived estimate only. Run compliance and manual review before approval.",
        )
        apply_scores(opportunity)
        db.add(opportunity)
        db.flush()
        maybe_assign_agent_to_opportunity(db, opportunity, reason="research_strategy_promote")
        db.flush()

        created_ids.append(opportunity.id)
        existing_pairs.add(pair)
        log_audit_event(
            db,
            "research_opportunity_created",
            f"Created opportunity from research strategy: {opportunity.topic}",
            metadata={
                "research_run_id": run.id,
                "research_strategy_id": strategy.id,
                "opportunity_id": opportunity.id,
                "estimated": True,
            },
        )

    db.commit()
    return ResearchCreateOpportunitiesResult(
        run_id=run.id,
        strategy_id=strategy.id,
        requested_topics=len(recommended_topics),
        created_count=len(created_ids),
        skipped_duplicates=skipped_duplicates,
        created_ids=created_ids,
        message=(
            f"Created {len(created_ids)} research-backed opportunity(ies); "
            f"skipped {skipped_duplicates} duplicate(s). Review and approve manually before promotion."
        ),
    )
