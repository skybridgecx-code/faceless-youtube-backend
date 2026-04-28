from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AssetType, ContentAsset, Video, VideoStatus


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:90] or "video"


def build_video_package(db: Session, video: Video) -> tuple[Path, ContentAsset]:
    if not video.approved:
        raise ValueError("Video must pass review before packaging.")

    settings = get_settings()
    package_dir = settings.output_path / f"{video.id:04d}-{slugify(video.title)}"
    package_dir.mkdir(parents=True, exist_ok=True)

    asset_files: list[dict[str, str]] = []
    for asset in video.assets:
        filename = f"{asset.asset_type.value}.md"
        path = package_dir / filename
        path.write_text(asset.body, encoding="utf-8")
        asset_files.append({"type": asset.asset_type.value, "file": filename})

    manifest = {
        "video_id": video.id,
        "title": video.title,
        "status": video.status.value,
        "approved": video.approved,
        "assets": asset_files,
        "next_steps": [
            "Record or generate voiceover after review.",
            "Create thumbnail from thumbnail_prompt.md.",
            "Edit video with original/licensed visuals.",
            "Upload manually or connect YouTube OAuth uploader.",
        ],
    }
    manifest_body = json.dumps(manifest, indent=2)
    (package_dir / "manifest.json").write_text(manifest_body, encoding="utf-8")

    manifest_asset = ContentAsset(video_id=video.id, asset_type=AssetType.package_manifest, body=manifest_body)
    db.add(manifest_asset)
    video.status = VideoStatus.packaged
    db.commit()
    db.refresh(manifest_asset)
    return package_dir, manifest_asset
