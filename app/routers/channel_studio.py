from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AssetType, Channel, ChannelStudioAgent, ContentAsset, ContentType, Video, VideoStatus
from app.routers.videos import build_readiness
from app.schemas import (
    ChannelStudioAgentRead,
    ChannelStudioAgentUpdate,
    ChannelStudioScoreboardResponse,
    ChannelStudioShortsBatchRequest,
    ChannelStudioShortsBatchResponse,
    ShortsBatchVideoSummary,
)
from app.services.audit import log_audit_event
from app.services.channel_studio import (
    LAUNCH_STATUS_VALUES,
    build_channel_studio_scoreboard,
    parse_pillars,
    seed_default_channel_studio_agents,
)
from app.services.content_engine import build_all_assets, generate_video_ideas

router = APIRouter(prefix="/agents", tags=["channel-studio"])


def _clean_text(value: str | None, default: str = "") -> str:
    if value is None:
        return default
    return re.sub(r"\s+", " ", str(value)).strip()


def _serialize_agent(agent: ChannelStudioAgent) -> ChannelStudioAgentRead:
    return ChannelStudioAgentRead(
        id=agent.id,
        name=agent.name,
        niche=agent.niche,
        target_viewer=agent.target_viewer,
        content_pillars=parse_pillars(agent.content_pillars_json),
        title_style=agent.title_style,
        thumbnail_style=agent.thumbnail_style,
        script_style=agent.script_style,
        compliance_notes=agent.compliance_notes,
        launch_wave=max(1, int(agent.launch_wave or 1)),
        launch_status=agent.launch_status if agent.launch_status in LAUNCH_STATUS_VALUES else "planning",
        channel_url=agent.channel_url,
        channel_handle=agent.channel_handle,
        notes=agent.notes,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _channel_studio_agent_or_404(db: Session, agent_id: int) -> ChannelStudioAgent:
    row = db.get(ChannelStudioAgent, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Channel studio agent not found")
    return row


def _primary_channel(db: Session) -> Channel:
    channel = db.scalar(select(Channel).order_by(Channel.created_at.asc(), Channel.id.asc()).limit(1))
    if channel is not None:
        return channel
    settings = get_settings()
    channel = Channel(
        name=settings.channel_default_name,
        niche="Multi-agent channel studio",
        audience="Operator planning workspace",
        brand_voice="Operational and realistic",
        visual_style="Local deterministic workflow dashboard",
    )
    db.add(channel)
    db.flush()
    return channel


def _short_title(base_title: str, topic_seed: str | None, index: int) -> str:
    prefix = _clean_text(topic_seed)
    if prefix:
        return f"{prefix} Shorts #{index + 1}"[:240]
    candidate = _clean_text(base_title, default=f"Shorts Candidate #{index + 1}")
    if candidate.lower().startswith("short:"):
        return candidate[:240]
    return f"Short: {candidate}"[:240]


@router.post("/seed-default-channel-studio", response_model=list[ChannelStudioAgentRead])
def seed_default_channel_studio(db: Session = Depends(get_db)) -> list[ChannelStudioAgentRead]:
    seeded = seed_default_channel_studio_agents(db)
    log_audit_event(
        db,
        "channel_studio_seeded",
        "Seeded default channel studio agents",
        metadata={"count": len(seeded)},
    )
    return [_serialize_agent(row) for row in seeded]


@router.get("/channel-studio", response_model=list[ChannelStudioAgentRead])
def list_channel_studio_agents(db: Session = Depends(get_db)) -> list[ChannelStudioAgentRead]:
    rows = list(
        db.scalars(
            select(ChannelStudioAgent).order_by(ChannelStudioAgent.launch_wave.asc(), ChannelStudioAgent.id.asc())
        )
    )
    return [_serialize_agent(row) for row in rows]


@router.get("/channel-studio/scoreboard", response_model=ChannelStudioScoreboardResponse)
def get_channel_studio_scoreboard(db: Session = Depends(get_db)) -> ChannelStudioScoreboardResponse:
    payload = build_channel_studio_scoreboard(db)
    return ChannelStudioScoreboardResponse.model_validate(payload)


@router.get("/channel-studio/{agent_id}", response_model=ChannelStudioAgentRead)
def get_channel_studio_agent(agent_id: int, db: Session = Depends(get_db)) -> ChannelStudioAgentRead:
    agent = _channel_studio_agent_or_404(db, agent_id)
    return _serialize_agent(agent)


@router.patch("/channel-studio/{agent_id}", response_model=ChannelStudioAgentRead)
def patch_channel_studio_agent(
    agent_id: int,
    payload: ChannelStudioAgentUpdate,
    db: Session = Depends(get_db),
) -> ChannelStudioAgentRead:
    agent = _channel_studio_agent_or_404(db, agent_id)
    updates = payload.model_dump(exclude_unset=True)

    if "name" in updates and updates["name"] is not None:
        agent.name = _clean_text(str(updates["name"]))[:180]
    if "niche" in updates and updates["niche"] is not None:
        agent.niche = _clean_text(str(updates["niche"]))[:240]
    if "target_viewer" in updates and updates["target_viewer"] is not None:
        agent.target_viewer = _clean_text(str(updates["target_viewer"]))[:240]
    if "content_pillars" in updates and updates["content_pillars"] is not None:
        pillars = [str(item).strip() for item in list(updates["content_pillars"]) if str(item).strip()]
        agent.content_pillars_json = json.dumps(pillars[:8])
    if "title_style" in updates and updates["title_style"] is not None:
        agent.title_style = _clean_text(str(updates["title_style"]))
    if "thumbnail_style" in updates and updates["thumbnail_style"] is not None:
        agent.thumbnail_style = _clean_text(str(updates["thumbnail_style"]))
    if "script_style" in updates and updates["script_style"] is not None:
        agent.script_style = _clean_text(str(updates["script_style"]))
    if "compliance_notes" in updates and updates["compliance_notes"] is not None:
        agent.compliance_notes = _clean_text(str(updates["compliance_notes"]))
    if "launch_wave" in updates and updates["launch_wave"] is not None:
        agent.launch_wave = max(1, int(updates["launch_wave"]))
    if "launch_status" in updates and updates["launch_status"] is not None:
        status_value = str(updates["launch_status"]).strip()
        if status_value not in LAUNCH_STATUS_VALUES:
            raise HTTPException(status_code=422, detail="Invalid launch status")
        agent.launch_status = status_value
    if "channel_url" in updates:
        agent.channel_url = _clean_text(str(updates["channel_url"])) if updates["channel_url"] else None
    if "channel_handle" in updates:
        agent.channel_handle = _clean_text(str(updates["channel_handle"]))[:120] if updates["channel_handle"] else None
    if "notes" in updates:
        agent.notes = _clean_text(str(updates["notes"])) if updates["notes"] else None

    db.commit()
    db.refresh(agent)
    log_audit_event(
        db,
        "channel_studio_agent_updated",
        f"Updated channel studio agent: {agent.name}",
        metadata={"agent_id": agent.id, "updated_fields": sorted(updates.keys())},
    )
    return _serialize_agent(agent)


@router.post("/{agent_id}/shorts-batch", response_model=ChannelStudioShortsBatchResponse)
def create_agent_shorts_batch(
    agent_id: int,
    payload: ChannelStudioShortsBatchRequest,
    db: Session = Depends(get_db),
) -> ChannelStudioShortsBatchResponse:
    agent = _channel_studio_agent_or_404(db, agent_id)
    requested_count = int(payload.count)
    batch_count = min(requested_count, 10)
    warnings: list[str] = []
    if requested_count > 10:
        warnings.append("Requested count exceeded max per batch. Created 10 shorts candidates.")

    channel = _primary_channel(db)
    ideas = generate_video_ideas(batch_count)
    pillars = parse_pillars(agent.content_pillars_json)

    created: list[Video] = []
    generated_assets_count: dict[int, int] = {}
    for index in range(batch_count):
        idea = ideas[index]
        title = _short_title(idea.get("title", f"Shorts Candidate #{index + 1}"), payload.topic_seed, index)
        chosen_pillar = pillars[index % len(pillars)] if pillars else agent.niche
        notes_parts = [
            f"Channel studio agent: {agent.name}",
            f"Title style: {agent.title_style}",
            f"Thumbnail style: {agent.thumbnail_style}",
            f"Script style: {agent.script_style}",
            f"Compliance notes: {agent.compliance_notes}",
        ]
        video = Video(
            channel_id=channel.id,
            assigned_agent_id=None,
            channel_studio_agent_id=agent.id,
            title=title,
            content_type=ContentType.short,
            pillar=chosen_pillar[:120],
            target_viewer=agent.target_viewer[:240],
            pain_point=(idea.get("pain_point") or f"{agent.niche} audience pain point").strip()[:500],
            demo_idea=(idea.get("demo_idea") or "Short-form educational workflow").strip()[:500],
            thumbnail_text=(idea.get("thumbnail_text") or "SHORTS WORKFLOW").strip()[:80],
            niche=agent.niche[:240],
            notes=" | ".join(part for part in notes_parts if part),
            status=VideoStatus.idea,
            approved=False,
            preview_reviewed=False,
        )
        db.add(video)
        db.flush()
        created.append(video)

        generated_assets_count[video.id] = 0
        if payload.auto_generate_assets:
            try:
                generated = build_all_assets(video)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Asset generation failed for video #{video.id}: {exc}")
                continue

            for item in generated:
                body = str(item.body or "").strip()
                if not body:
                    continue
                db.add(ContentAsset(video_id=video.id, asset_type=AssetType(item.asset_type), body=body))
                generated_assets_count[video.id] += 1
            if generated_assets_count[video.id] > 0:
                video.status = VideoStatus.needs_review
            video.approved = False
            video.preview_reviewed = False
            video.preview_reviewed_at = None

    db.commit()

    summaries: list[ShortsBatchVideoSummary] = []
    for video in created:
        db.refresh(video)
        readiness = build_readiness(video, db)
        summaries.append(
            ShortsBatchVideoSummary(
                id=video.id,
                title=video.title,
                content_type=video.content_type,
                pillar=video.pillar,
                target_viewer=video.target_viewer,
                workflow_status=video.status,
                approved=bool(video.approved),
                preview_reviewed=bool(video.preview_reviewed),
                generated_assets_count=generated_assets_count.get(video.id, 0),
                next_required_action=readiness.next_required_action,
            )
        )
        log_audit_event(
            db,
            "channel_studio_shorts_batch_video_created",
            f"Created channel studio shorts candidate: {video.title}",
            video_id=video.id,
            metadata={
                "channel_studio_agent_id": agent.id,
                "channel_studio_agent_name": agent.name,
                "generated_assets_count": generated_assets_count.get(video.id, 0),
            },
        )

    next_action = "Run compliance and manual review on generated shorts."
    if not payload.auto_generate_assets:
        next_action = "Generate assets for shorts candidates, then run compliance and manual review."

    return ChannelStudioShortsBatchResponse(
        agent_id=agent.id,
        agent_name=agent.name,
        requested_count=requested_count,
        created_count=len(summaries),
        videos=summaries,
        warnings=warnings,
        next_required_action=next_action,
    )
