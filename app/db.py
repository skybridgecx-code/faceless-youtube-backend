from collections.abc import Generator

from sqlalchemy import create_engine, text
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


def _apply_sqlite_video_preview_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    required_columns = {
        "rendered_preview_path": "ALTER TABLE videos ADD COLUMN rendered_preview_path TEXT",
        "preview_rendered_at": "ALTER TABLE videos ADD COLUMN preview_rendered_at DATETIME",
        "preview_reviewed": "ALTER TABLE videos ADD COLUMN preview_reviewed BOOLEAN DEFAULT 0",
        "preview_reviewed_at": "ALTER TABLE videos ADD COLUMN preview_reviewed_at DATETIME",
    }

    with engine.begin() as conn:
        existing_columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(videos)")).fetchall()
        }
        for column_name, alter_sql in required_columns.items():
            if column_name not in existing_columns:
                conn.execute(text(alter_sql))


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

    with engine.begin() as conn:
        table_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='video_opportunities'")
        ).fetchone()
        if not table_exists:
            return

        existing_columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(video_opportunities)")).fetchall()
        }
        for column_name, alter_sql in required_columns.items():
            if column_name not in existing_columns:
                conn.execute(text(alter_sql))
