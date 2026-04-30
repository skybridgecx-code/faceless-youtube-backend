from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _apply_sqlite_video_preview_columns()
    _apply_sqlite_video_opportunity_review_columns()
    _apply_sqlite_agent_system_columns()
    _seed_default_agents_for_existing_channels()


def _sqlite_table_exists(conn: Any, table_name: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:table_name"),
            {"table_name": table_name},
        ).fetchone()
    )


def _apply_sqlite_additive_columns(table_name: str, required_columns: dict[str, str]) -> None:
    with engine.begin() as conn:
        if not _sqlite_table_exists(conn, table_name):
            return

        existing_columns = {
            row[1]
            for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        }
        for column_name, alter_sql in required_columns.items():
            if column_name not in existing_columns:
                conn.execute(text(alter_sql))


def _apply_sqlite_video_preview_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    required_columns = {
        "rendered_preview_path": "ALTER TABLE videos ADD COLUMN rendered_preview_path TEXT",
        "preview_rendered_at": "ALTER TABLE videos ADD COLUMN preview_rendered_at DATETIME",
        "preview_reviewed": "ALTER TABLE videos ADD COLUMN preview_reviewed BOOLEAN DEFAULT 0",
        "preview_reviewed_at": "ALTER TABLE videos ADD COLUMN preview_reviewed_at DATETIME",
    }
    _apply_sqlite_additive_columns("videos", required_columns)


def _apply_sqlite_video_opportunity_review_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    required_columns = {
        "review_status": "ALTER TABLE video_opportunities ADD COLUMN review_status TEXT DEFAULT 'unreviewed'",
        "operator_notes": "ALTER TABLE video_opportunities ADD COLUMN operator_notes TEXT",
        "rejection_reason": "ALTER TABLE video_opportunities ADD COLUMN rejection_reason TEXT",
        "decision_summary": "ALTER TABLE video_opportunities ADD COLUMN decision_summary TEXT",
        "reviewed_at": "ALTER TABLE video_opportunities ADD COLUMN reviewed_at DATETIME",
    }
    _apply_sqlite_additive_columns("video_opportunities", required_columns)


def _apply_sqlite_agent_system_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    _apply_sqlite_additive_columns(
        "videos",
        {
            "assigned_agent_id": "ALTER TABLE videos ADD COLUMN assigned_agent_id INTEGER",
        },
    )
    _apply_sqlite_additive_columns(
        "video_opportunities",
        {
            "assigned_agent_id": "ALTER TABLE video_opportunities ADD COLUMN assigned_agent_id INTEGER",
        },
    )
    _apply_sqlite_additive_columns(
        "executive_producer_recommendations",
        {
            "matched_agent_id": "ALTER TABLE executive_producer_recommendations ADD COLUMN matched_agent_id INTEGER",
            "matched_agent_name": "ALTER TABLE executive_producer_recommendations ADD COLUMN matched_agent_name TEXT",
            "matched_agent_lane": "ALTER TABLE executive_producer_recommendations ADD COLUMN matched_agent_lane TEXT",
        },
    )
    _apply_sqlite_additive_columns(
        "content_agents",
        {
            "lane": "ALTER TABLE content_agents ADD COLUMN lane TEXT DEFAULT ''",
            "focus": "ALTER TABLE content_agents ADD COLUMN focus TEXT DEFAULT ''",
            "monetization_focus": "ALTER TABLE content_agents ADD COLUMN monetization_focus TEXT DEFAULT ''",
            "compliance_notes": "ALTER TABLE content_agents ADD COLUMN compliance_notes TEXT DEFAULT ''",
            "production_rules": "ALTER TABLE content_agents ADD COLUMN production_rules TEXT DEFAULT ''",
            "is_active": "ALTER TABLE content_agents ADD COLUMN is_active BOOLEAN DEFAULT 1",
            "updated_at": "ALTER TABLE content_agents ADD COLUMN updated_at DATETIME",
        },
    )


def _seed_default_agents_for_existing_channels() -> None:
    from app.models import Channel
    from app.services.agents import seed_default_agents_if_empty

    db = SessionLocal()
    try:
        channels = list(db.scalars(select(Channel)))
        for channel in channels:
            seed_default_agents_if_empty(db, channel.id)
    finally:
        db.close()
