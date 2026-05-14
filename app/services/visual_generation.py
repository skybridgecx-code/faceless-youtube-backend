from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import VisualAssetPlan, VisualGeneratedAsset, VisualGenerationJob

# Minimal valid 1×1 white PNG for local placeholder stubs.
_PNG_PLACEHOLDER = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

ACTIVE_JOB_STATUSES = {"queued", "exported", "imported"}


def job_type_for_prompt_type(prompt_type: str) -> str | None:
    mapping = {
        "thumbnail": "thumbnail",
        "image": "image",
        "animation": "animation",
        "b_roll": "broll",
        "dashboard_demo": "dashboard",
    }
    return mapping.get(prompt_type)


def build_provider_payload(
    *,
    job: VisualGenerationJob,
    plan: VisualAssetPlan,
    scene_context: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "visual_asset_plan_id": plan.id,
        "video_id": plan.video_id,
        "brief_id": plan.brief_id,
        "job_type": job.job_type,
        "provider": job.provider,
        "prompt": job.prompt,
        "negative_prompt": job.negative_prompt,
        "scene_context": scene_context,
        "safety_notes": plan.safety_notes,
        "provider_hint": {
            "manual": "Manual/local generation workflow",
            "openai_image": "OpenAI image generation payload shape",
            "runway": "Runway image/video generation payload shape",
            "pika": "Pika animation payload shape",
            "luma": "Luma motion payload shape",
            "other": "Custom provider payload shape",
        }.get(job.provider, "Custom provider payload shape"),
    }


def build_scene_context(plan: VisualAssetPlan, scene_id: int | None) -> dict[str, Any] | None:
    if scene_id is None:
        return None
    scene = next((row for row in plan.scenes if row.id == scene_id), None)
    if scene is None:
        return None
    return {
        "scene_id": scene.id,
        "scene_number": scene.scene_number,
        "scene_title": scene.scene_title,
        "narrative_beat": scene.narrative_beat,
        "on_screen_text": scene.on_screen_text,
        "safety_notes": scene.safety_notes,
        "asset_status": scene.asset_status,
        "generated_asset_path": scene.generated_asset_path,
    }


def queue_key(job: VisualGenerationJob) -> tuple[int | None, str]:
    return (job.visual_scene_id, job.job_type)


def summarize_plan_generation(plan: VisualAssetPlan) -> dict[str, int]:
    jobs = list(plan.generation_jobs)
    assets = list(plan.generated_assets)
    return {
        "jobs_total": len(jobs),
        "jobs_queued": sum(1 for row in jobs if row.status == "queued"),
        "jobs_exported": sum(1 for row in jobs if row.status == "exported"),
        "jobs_imported": sum(1 for row in jobs if row.status == "imported"),
        "assets_registered": sum(1 for row in assets if row.file_exists),
    }


def recompute_plan_generation_status(plan: VisualAssetPlan) -> dict[str, int]:
    jobs = list(plan.generation_jobs)
    active_jobs = [row for row in jobs if row.status in ACTIVE_JOB_STATUSES]
    required_keys = {queue_key(row) for row in active_jobs}
    imported_keys = {queue_key(row) for row in active_jobs if row.status == "imported"}

    counts = summarize_plan_generation(plan)
    if required_keys and required_keys.issubset(imported_keys):
        plan.status = "assets_generated"
    elif counts["jobs_queued"] > 0 or counts["jobs_exported"] > 0 or counts["jobs_imported"] > 0:
        if plan.status not in {"ready_for_generation"}:
            plan.status = "generation_in_progress"

    return counts


def payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _local_asset_path(video_id: int, job_id: int, job_type: str) -> Path:
    output_dir = get_settings().output_path / "visual_assets" / f"video_{video_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"job_{job_id}_{job_type}_placeholder.png"


def run_local_generation_job(job: VisualGenerationJob, db: Session) -> dict[str, Any]:
    """Run a queued job locally using a deterministic placeholder asset.

    Leaves the generated asset in pending review — never auto-approves.
    Provider failures become structured warnings, not crashes.
    """
    if job.status not in {"queued", "exported"}:
        return {
            "job_id": job.id,
            "status": job.status,
            "warning": f"Job is already in status '{job.status}'; skipped.",
            "asset_id": None,
        }

    plan = job.plan
    video_id = plan.video_id

    try:
        asset_path = _local_asset_path(video_id, job.id, job.job_type)
        asset_path.write_bytes(_PNG_PLACEHOLDER)
    except OSError as exc:
        job.status = "failed"
        job.failure_reason = f"Local placeholder write failed: {exc}"
        db.commit()
        return {
            "job_id": job.id,
            "status": "failed",
            "warning": job.failure_reason,
            "asset_id": None,
        }

    existing_asset = db.scalar(
        select(VisualGeneratedAsset).where(VisualGeneratedAsset.generation_job_id == job.id).limit(1)
    )
    if existing_asset is None:
        asset = VisualGeneratedAsset(
            visual_asset_plan_id=job.visual_asset_plan_id,
            visual_scene_id=job.visual_scene_id,
            generation_job_id=job.id,
            asset_type=job.job_type,
            file_path=str(asset_path),
            file_exists=True,
            mime_type="image/png",
        )
        db.add(asset)
        db.flush()
    else:
        existing_asset.file_path = str(asset_path)
        existing_asset.file_exists = True
        existing_asset.mime_type = "image/png"
        asset = existing_asset

    job.output_path = str(asset_path)
    job.status = "imported"
    job.failure_reason = None

    if job.scene is not None:
        job.scene.asset_status = "generated"
        job.scene.generated_asset_path = str(asset_path)

    recompute_plan_generation_status(plan)
    db.commit()
    db.refresh(asset)

    return {
        "job_id": job.id,
        "status": "imported",
        "asset_id": asset.id,
        "file_path": str(asset_path),
        "review_status": "pending",
        "warning": None,
    }
