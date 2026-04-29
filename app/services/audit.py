from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


SECRET_KEY_MARKERS = (
    "secret",
    "token",
    "api_key",
    "apikey",
    "password",
    "authorization",
    "credential",
    "auth",
)

BODY_KEY_MARKERS = (
    "body",
    "asset_body",
    "script",
    "description_body",
    "metadata_body",
    "full_text",
)


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None

    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        key_text = str(key)
        lowered = key_text.lower()

        if any(marker in lowered for marker in SECRET_KEY_MARKERS):
            safe[key_text] = "[redacted]"
            continue

        if any(marker in lowered for marker in BODY_KEY_MARKERS):
            if isinstance(value, str):
                safe[f"{key_text}_length"] = len(value)
            else:
                safe[key_text] = "[omitted]"
            continue

        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key_text] = value
        elif isinstance(value, (list, tuple)):
            safe[key_text] = [
                item if isinstance(item, (str, int, float, bool)) or item is None else str(item)
                for item in value[:50]
            ]
        elif isinstance(value, dict):
            safe[key_text] = _safe_metadata(value)
        else:
            safe[key_text] = str(value)

    return safe


def log_audit_event(
    db: Session,
    event_type: str,
    message: str,
    video_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent | None:
    """Persist a safe audit event without allowing audit logging to break the main workflow."""
    metadata_json = None
    safe_metadata = _safe_metadata(metadata)
    if safe_metadata:
        try:
            metadata_json = json.dumps(safe_metadata, default=str, sort_keys=True)
        except Exception:
            metadata_json = None

    event = AuditEvent(
        video_id=video_id,
        event_type=event_type,
        message=message,
        metadata_json=metadata_json,
        created_at=datetime.utcnow(),
    )

    try:
        db.add(event)
        db.commit()
        db.refresh(event)
        return event
    except Exception:
        db.rollback()
        return None
