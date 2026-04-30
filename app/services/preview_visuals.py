from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Video, VisualAssetPlan, VisualGeneratedAsset, VisualScene


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_roots() -> list[Path]:
    settings = get_settings()
    roots = [settings.output_path.resolve()]
    try:
        roots.append(Path.cwd().resolve())
    except OSError:
        pass
    return roots


def _safe_existing_asset_path(raw_path: str) -> tuple[Path | None, str | None]:
    value = (raw_path or "").strip()
    if not value:
        return None, "Asset path is empty."
    if "\x00" in value:
        return None, "Asset path contains an invalid null byte."

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (get_settings().output_path / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if not any(_is_relative_to(candidate, root) for root in _allowed_roots()):
        return None, "Asset path is outside allowed local preview roots."
    if not candidate.is_file():
        return None, "Asset path does not exist as a local file."
    return candidate, None


def latest_visual_plan_for_video(db: Session, video_id: int) -> VisualAssetPlan | None:
    return db.scalar(
        select(VisualAssetPlan)
        .where(VisualAssetPlan.video_id == video_id)
        .order_by(VisualAssetPlan.updated_at.desc(), VisualAssetPlan.created_at.desc())
        .limit(1)
    )


def build_preview_visual_manifest(db: Session, video: Video) -> dict[str, Any]:
    plan = latest_visual_plan_for_video(db, video.id)
    if plan is None:
        return {
            "video_id": video.id,
            "title": video.title,
            "plan_id": None,
            "preview_asset_mode": "fallback_only",
            "visual_assets_used_count": 0,
            "visual_assets_missing_count": 0,
            "included_asset_paths": [],
            "assets": [],
            "scenes": [],
            "warnings": ["No visual asset plan exists. Preview renderer will use fallback slides."],
        }

    scenes = list(
        db.scalars(
            select(VisualScene)
            .where(VisualScene.plan_id == plan.id)
            .order_by(VisualScene.scene_number.asc())
        )
    )
    registered_assets = list(
        db.scalars(
            select(VisualGeneratedAsset)
            .where(
                VisualGeneratedAsset.visual_asset_plan_id == plan.id,
                VisualGeneratedAsset.file_exists.is_(True),
            )
            .order_by(VisualGeneratedAsset.created_at.asc())
        )
    )

    included_assets: list[dict[str, Any]] = []
    warnings: list[str] = []
    used_scene_ids: set[int] = set()

    for asset in registered_assets:
        safe_path, warning = _safe_existing_asset_path(asset.file_path)
        if safe_path is None:
            warnings.append(f"Skipped asset {asset.id}: {warning}")
            continue
        if asset.visual_scene_id is not None:
            used_scene_ids.add(asset.visual_scene_id)
        included_assets.append(
            {
                "asset_id": asset.id,
                "asset_type": asset.asset_type,
                "visual_scene_id": asset.visual_scene_id,
                "generation_job_id": asset.generation_job_id,
                "file_path": str(safe_path),
                "mime_type": asset.mime_type,
                "duration_seconds": asset.duration_seconds,
                "width": asset.width,
                "height": asset.height,
                "notes": asset.notes,
            }
        )

    scene_rows: list[dict[str, Any]] = []
    for scene in scenes:
        scene_assets = [asset for asset in included_assets if asset["visual_scene_id"] == scene.id]
        if not scene_assets:
            warnings.append(f"Scene {scene.scene_number} has no registered local asset; fallback slide will be used.")
        scene_rows.append(
            {
                "scene_id": scene.id,
                "scene_number": scene.scene_number,
                "scene_title": scene.scene_title,
                "asset_status": scene.asset_status,
                "generated_asset_path": scene.generated_asset_path,
                "registered_asset_paths": [asset["file_path"] for asset in scene_assets],
            }
        )

    missing_count = max(0, len(scenes) - len(used_scene_ids))
    used_count = len(included_assets)
    if used_count == 0:
        mode = "fallback_only"
    elif missing_count == 0:
        mode = "registered_assets"
    else:
        mode = "mixed"

    if mode == "fallback_only" and plan is not None:
        warnings.append("No registered local visual assets are available; preview renderer will use fallback slides.")
    elif mode == "mixed":
        warnings.append("Some registered assets are available, but missing scenes will use fallback slides.")

    return {
        "video_id": video.id,
        "title": video.title,
        "plan_id": plan.id,
        "preview_asset_mode": mode,
        "visual_assets_used_count": used_count,
        "visual_assets_missing_count": missing_count,
        "included_asset_paths": [asset["file_path"] for asset in included_assets],
        "assets": included_assets,
        "scenes": scene_rows,
        "warnings": warnings,
    }


def has_registered_preview_assets(db: Session, video_id: int) -> bool:
    video = db.get(Video, video_id)
    if video is None:
        return False
    manifest = build_preview_visual_manifest(db, video)
    return int(manifest.get("visual_assets_used_count", 0)) > 0
