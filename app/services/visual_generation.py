from __future__ import annotations

import json
from typing import Any

from app.models import VisualAssetPlan, VisualGenerationJob

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
