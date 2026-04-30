from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Channel
from app.schemas import ChannelCreate, ChannelRead
from app.services.agents import seed_default_agents_if_empty

router = APIRouter(prefix="/channels", tags=["channels"])


@router.post("", response_model=ChannelRead)
def create_channel(payload: ChannelCreate, db: Session = Depends(get_db)) -> Channel:
    channel = Channel(**payload.model_dump())
    db.add(channel)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Channel already exists") from exc
    db.refresh(channel)
    seed_default_agents_if_empty(db, channel.id)
    return channel


@router.get("", response_model=list[ChannelRead])
def list_channels(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Channel]:
    return list(
        db.scalars(
            select(Channel)
            .order_by(Channel.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )
