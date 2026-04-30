from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import VisualGeneratedAsset

VisualAssetReviewStatus = Literal["pending", "approved", "rejected"]
_ALLOWED_STATUSES = {"pending", "approved", "rejected"}


def ensure_visual_asset_review_columns(db: Session) -> None:
    """Add local SQLite review columns when running against an older database."""
    bind = db.get_bind()
    if bind.dialect.name != "sqlite":
        return

    rows = db.execute(text("PRAGMA table_info(visual_generated_assets)")).fetchall()
    existing = {row[1] for row in rows}
    if "review_status" not in existing:
        db.execute(text("ALTER TABLE visual_generated_assets ADD COLUMN review_status TEXT DEFAULT 'pending'"))
    if "review_notes" not in existing:
        db.execute(text("ALTER TABLE visual_generated_assets ADD COLUMN review_notes TEXT"))
    if "reviewed_at" not in existing:
        db.execute(text("ALTER TABLE visual_generated_assets ADD COLUMN reviewed_at DATETIME"))
    db.flush()


def _review_row(db: Session, asset_id: int) -> dict[str, Any]:
    ensure_visual_asset_review_columns(db)
    row = db.execute(
        text(
            "SELECT review_status, review_notes, reviewed_at "
            "FROM visual_generated_assets WHERE id = :asset_id"
        ),
        {"asset_id": asset_id},
    ).mappings().first()
    if row is None:
        return {"review_status": "pending", "review_notes": None, "reviewed_at": None}
    return {
        "review_status": row.get("review_status") or "pending",
        "review_notes": row.get("review_notes"),
        "reviewed_at": row.get("reviewed_at"),
    }


def asset_review_fields(db: Session, asset: VisualGeneratedAsset) -> dict[str, Any]:
    return _review_row(db, asset.id)


def visual_generated_asset_payload(db: Session, asset: VisualGeneratedAsset) -> dict[str, Any]:
    payload = {
        "id": asset.id,
        "visual_asset_plan_id": asset.visual_asset_plan_id,
        "visual_scene_id": asset.visual_scene_id,
        "generation_job_id": asset.generation_job_id,
        "asset_type": asset.asset_type,
        "file_path": asset.file_path,
        "file_exists": asset.file_exists,
        "mime_type": asset.mime_type,
        "duration_seconds": asset.duration_seconds,
        "width": asset.width,
        "height": asset.height,
        "notes": asset.notes,
        "created_at": asset.created_at,
        "updated_at": asset.updated_at,
    }
    payload.update(asset_review_fields(db, asset))
    return payload


def update_visual_asset_review(
    db: Session,
    asset: VisualGeneratedAsset,
    review_status: str,
    review_notes: str | None = None,
) -> dict[str, Any]:
    status = (review_status or "").strip().lower()
    if status not in _ALLOWED_STATUSES:
        raise ValueError("review_status must be pending, approved, or rejected")

    ensure_visual_asset_review_columns(db)
    reviewed_at: datetime | None = None if status == "pending" else datetime.utcnow()
    db.execute(
        text(
            "UPDATE visual_generated_assets "
            "SET review_status = :review_status, review_notes = :review_notes, reviewed_at = :reviewed_at "
            "WHERE id = :asset_id"
        ),
        {
            "asset_id": asset.id,
            "review_status": status,
            "review_notes": review_notes,
            "reviewed_at": reviewed_at,
        },
    )
    db.flush()
    payload = visual_generated_asset_payload(db, asset)
    payload["review_status"] = status
    payload["review_notes"] = review_notes
    payload["reviewed_at"] = reviewed_at
    return payload
