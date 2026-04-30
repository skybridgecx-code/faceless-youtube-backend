from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Channel, ContentAgent, Video, VideoOpportunity
from app.routers.opportunities import serialize_opportunity
from app.schemas import ContentAgentRead, ContentAgentUpdate, OpportunityRead, VideoRead
from app.services.agents import ensure_channel_agents
from app.services.audit import log_audit_event

router = APIRouter(prefix="/agents", tags=["agents"])


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def get_agent_or_404(db: Session, agent_id: int) -> ContentAgent:
    agent = db.get(ContentAgent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get("", response_model=list[ContentAgentRead])
def list_agents(db: Session = Depends(get_db)) -> list[ContentAgent]:
    channels = list(db.scalars(select(Channel)))
    for channel in channels:
        ensure_channel_agents(db, channel.id)
    return list(db.scalars(select(ContentAgent).order_by(ContentAgent.id.asc())))


@router.get("/{agent_id}", response_model=ContentAgentRead)
def get_agent(agent_id: int, db: Session = Depends(get_db)) -> ContentAgent:
    return get_agent_or_404(db, agent_id)


@router.patch("/{agent_id}", response_model=ContentAgentRead)
def update_agent(
    agent_id: int,
    payload: ContentAgentUpdate,
    db: Session = Depends(get_db),
) -> ContentAgent:
    agent = get_agent_or_404(db, agent_id)
    before_active = agent.is_active
    updates: dict[str, object] = {}

    if payload.focus is not None:
        agent.focus = clean_text(payload.focus)
        updates["focus"] = True
    if payload.monetization_focus is not None:
        agent.monetization_focus = clean_text(payload.monetization_focus)
        updates["monetization_focus"] = True
    if payload.compliance_notes is not None:
        agent.compliance_notes = clean_text(payload.compliance_notes)
        updates["compliance_notes"] = True
    if payload.production_rules is not None:
        agent.production_rules = clean_text(payload.production_rules)
        updates["production_rules"] = True
    if payload.is_active is not None:
        agent.is_active = bool(payload.is_active)
        updates["is_active"] = True

    db.commit()
    db.refresh(agent)

    log_audit_event(
        db,
        "agent_updated",
        f"Updated content agent: {agent.name}",
        metadata={
            "agent_id": agent.id,
            "channel_id": agent.channel_id,
            "updated_fields": sorted(updates.keys()),
            "was_active": before_active,
            "is_active": agent.is_active,
        },
    )
    return agent


@router.get("/{agent_id}/opportunities", response_model=list[OpportunityRead])
def get_agent_opportunities(agent_id: int, db: Session = Depends(get_db)) -> list[OpportunityRead]:
    agent = get_agent_or_404(db, agent_id)
    rows = list(
        db.scalars(
            select(VideoOpportunity)
            .where(
                or_(
                    VideoOpportunity.assigned_agent_id == agent.id,
                    and_(
                        VideoOpportunity.assigned_agent_id.is_(None),
                        VideoOpportunity.assigned_agent == agent.name,
                    ),
                )
            )
            .order_by(VideoOpportunity.total_score.desc(), VideoOpportunity.created_at.desc())
        )
    )
    return [serialize_opportunity(row) for row in rows]


@router.get("/{agent_id}/videos", response_model=list[VideoRead])
def get_agent_videos(agent_id: int, db: Session = Depends(get_db)) -> list[Video]:
    agent = get_agent_or_404(db, agent_id)
    rows = list(
        db.scalars(
            select(Video)
            .where(
                or_(
                    Video.assigned_agent_id == agent.id,
                    and_(
                        Video.assigned_agent_id.is_(None),
                        Video.niche == agent.lane,
                    ),
                    and_(
                        Video.assigned_agent_id.is_(None),
                        Video.notes.ilike(f"%Assigned content agent: {agent.name}%"),
                    ),
                )
            )
            .order_by(Video.created_at.desc())
        )
    )
    return rows
