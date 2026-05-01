from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Video, VisualAssetPlan, VisualGeneratedAsset, VisualGenerationJob
from app.schemas import (
    VisualGeneratedAssetReviewQueueItem,
    VisualGeneratedAssetRead,
    VisualGenerationJobRead,
    VisualGenerationJobUpdate,
    VisualGenerationQueueRequest,
    VisualGenerationQueueResult,
    VisualGenerationRegisterOutputRequest,
)
from app.services.audit import log_audit_event
from app.services.preview_visuals import build_preview_visual_manifest
from app.services.visual_asset_review import asset_review_fields, update_visual_asset_review, visual_generated_asset_payload
from app.services.visual_generation import (
    build_provider_payload,
    build_scene_context,
    job_type_for_prompt_type,
    payload_json,
    recompute_plan_generation_status,
)

router = APIRouter(prefix="/visual-generation", tags=["visual-generation"])


class VisualAssetReviewUpdate(BaseModel):
    review_status: Literal["pending", "approved", "rejected"]
    review_notes: str | None = Field(default=None, max_length=1000)

    @field_validator("review_notes", mode="before")
    @classmethod
    def reject_html_payload(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        if re.search(r"<[^>]+>", text) or re.search(r"(?i)<\s*script\b", text):
            raise ValueError("HTML or script tags are not allowed.")
        return text


class VisualAssetRejectRequest(BaseModel):
    review_notes: str | None = Field(default=None, max_length=1000)

    @field_validator("review_notes", mode="before")
    @classmethod
    def reject_html_payload(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        if re.search(r"<[^>]+>", text) or re.search(r"(?i)<\s*script\b", text):
            raise ValueError("HTML or script tags are not allowed.")
        return text


def get_plan_for_generation_or_404(db: Session, plan_id: int) -> VisualAssetPlan:
    stmt = (
        select(VisualAssetPlan)
        .where(VisualAssetPlan.id == plan_id)
        .options(
            selectinload(VisualAssetPlan.scenes),
            selectinload(VisualAssetPlan.prompts),
            selectinload(VisualAssetPlan.generation_jobs),
            selectinload(VisualAssetPlan.generated_assets),
        )
        .limit(1)
    )
    plan = db.scalar(stmt)
    if not plan:
        raise HTTPException(status_code=404, detail="Visual asset plan not found")
    return plan


def get_job_or_404(db: Session, job_id: int) -> VisualGenerationJob:
    stmt = (
        select(VisualGenerationJob)
        .where(VisualGenerationJob.id == job_id)
        .options(
            selectinload(VisualGenerationJob.plan).selectinload(VisualAssetPlan.scenes),
            selectinload(VisualGenerationJob.plan).selectinload(VisualAssetPlan.generation_jobs),
            selectinload(VisualGenerationJob.plan).selectinload(VisualAssetPlan.generated_assets),
        )
        .limit(1)
    )
    job = db.scalar(stmt)
    if not job:
        raise HTTPException(status_code=404, detail="Visual generation job not found")
    return job


def get_visual_generated_asset_or_404(db: Session, asset_id: int) -> VisualGeneratedAsset:
    row = db.get(VisualGeneratedAsset, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Visual generated asset not found")
    return row


@router.post("/plans/{plan_id}/queue", response_model=VisualGenerationQueueResult)
def queue_generation_jobs_from_plan(
    plan_id: int,
    payload: VisualGenerationQueueRequest,
    db: Session = Depends(get_db),
) -> VisualGenerationQueueResult:
    plan = get_plan_for_generation_or_404(db, plan_id)

    existing_active_keys = {
        (row.visual_scene_id, row.job_type)
        for row in plan.generation_jobs
        if row.status not in {"failed", "cancelled"}
    }

    created_jobs: list[VisualGenerationJob] = []
    skipped_jobs = 0

    ordered_prompts = sorted(
        plan.prompts,
        key=lambda row: (
            row.scene_id is not None,
            row.scene_id or 0,
            row.prompt_type,
            row.id,
        ),
    )

    for prompt in ordered_prompts:
        job_type = job_type_for_prompt_type(prompt.prompt_type)
        if not job_type:
            continue
        key = (prompt.scene_id, job_type)
        if key in existing_active_keys:
            skipped_jobs += 1
            continue

        job = VisualGenerationJob(
            visual_asset_plan_id=plan.id,
            visual_scene_id=prompt.scene_id,
            prompt_id=prompt.id,
            job_type=job_type,
            provider=payload.provider,
            status="queued",
            prompt=prompt.prompt_text,
            negative_prompt=payload.negative_prompt,
            provider_payload_json="{}",
        )
        db.add(job)
        db.flush()

        provider_payload = build_provider_payload(
            job=job,
            plan=plan,
            scene_context=build_scene_context(plan, prompt.scene_id),
        )
        job.provider_payload_json = payload_json(provider_payload)

        created_jobs.append(job)
        existing_active_keys.add(key)

    if created_jobs and plan.status != "assets_generated":
        plan.status = "generation_in_progress"

    recompute_plan_generation_status(plan)
    db.commit()

    log_audit_event(
        db,
        "visual_generation_jobs_queued",
        f"Queued {len(created_jobs)} visual generation jobs for plan #{plan.id}",
        video_id=plan.video_id,
        metadata={
            "plan_id": plan.id,
            "created_jobs": len(created_jobs),
            "skipped_jobs": skipped_jobs,
            "provider": payload.provider,
        },
    )

    return VisualGenerationQueueResult(
        plan_id=plan.id,
        created_jobs=len(created_jobs),
        skipped_jobs=skipped_jobs,
        jobs=[VisualGenerationJobRead.model_validate(row) for row in created_jobs],
    )


@router.get("/jobs", response_model=list[VisualGenerationJobRead])
def list_visual_generation_jobs(
    plan_id: int | None = None,
    scene_id: int | None = None,
    status: str | None = None,
    provider: str | None = None,
    video_id: int | None = None,
    limit: int = Query(default=250, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[VisualGenerationJobRead]:
    stmt = select(VisualGenerationJob).order_by(VisualGenerationJob.created_at.desc()).limit(limit)
    if plan_id is not None:
        stmt = stmt.where(VisualGenerationJob.visual_asset_plan_id == plan_id)
    if scene_id is not None:
        stmt = stmt.where(VisualGenerationJob.visual_scene_id == scene_id)
    if status:
        stmt = stmt.where(VisualGenerationJob.status == status)
    if provider:
        stmt = stmt.where(VisualGenerationJob.provider == provider)
    if video_id is not None:
        stmt = stmt.join(VisualAssetPlan, VisualAssetPlan.id == VisualGenerationJob.visual_asset_plan_id).where(
            VisualAssetPlan.video_id == video_id
        )

    rows = list(db.scalars(stmt))
    return [VisualGenerationJobRead.model_validate(row) for row in rows]


@router.get("/jobs/{job_id}", response_model=VisualGenerationJobRead)
def get_visual_generation_job(job_id: int, db: Session = Depends(get_db)) -> VisualGenerationJobRead:
    row = get_job_or_404(db, job_id)
    return VisualGenerationJobRead.model_validate(row)


@router.patch("/jobs/{job_id}", response_model=VisualGenerationJobRead)
def update_visual_generation_job(
    job_id: int,
    payload: VisualGenerationJobUpdate,
    db: Session = Depends(get_db),
) -> VisualGenerationJobRead:
    job = get_job_or_404(db, job_id)
    update_data = payload.model_dump(exclude_unset=True)

    for key, value in update_data.items():
        setattr(job, key, value)

    provider_payload = build_provider_payload(
        job=job,
        plan=job.plan,
        scene_context=build_scene_context(job.plan, job.visual_scene_id),
    )
    job.provider_payload_json = payload_json(provider_payload)

    recompute_plan_generation_status(job.plan)
    db.commit()

    log_audit_event(
        db,
        "visual_generation_job_updated",
        f"Updated visual generation job #{job.id}",
        video_id=job.plan.video_id,
        metadata={"job_id": job.id, "updated_fields": sorted(update_data.keys())},
    )
    return VisualGenerationJobRead.model_validate(job)


@router.post("/jobs/{job_id}/export-payload")
def export_visual_generation_payload(job_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    job = get_job_or_404(db, job_id)
    provider_payload = build_provider_payload(
        job=job,
        plan=job.plan,
        scene_context=build_scene_context(job.plan, job.visual_scene_id),
    )
    job.provider_payload_json = payload_json(provider_payload)
    if job.status != "imported":
        job.status = "exported"

    recompute_plan_generation_status(job.plan)
    db.commit()

    log_audit_event(
        db,
        "visual_generation_payload_exported",
        f"Exported provider payload for visual generation job #{job.id}",
        video_id=job.plan.video_id,
        metadata={"job_id": job.id, "job_type": job.job_type, "provider": job.provider},
    )

    return {
        "job": VisualGenerationJobRead.model_validate(job).model_dump(),
        "provider_payload": provider_payload,
    }


@router.post("/jobs/{job_id}/register-output", response_model=VisualGeneratedAssetRead)
def register_visual_generation_output(
    job_id: int,
    payload: VisualGenerationRegisterOutputRequest,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    job = get_job_or_404(db, job_id)

    raw_path = Path(payload.output_path).expanduser()
    resolved_path = raw_path if raw_path.is_absolute() else (Path.cwd() / raw_path)
    resolved_path = resolved_path.resolve()

    if not resolved_path.exists() or not resolved_path.is_file():
        raise HTTPException(status_code=400, detail="Output path must point to an existing local file.")

    mime_type = payload.mime_type or mimetypes.guess_type(str(resolved_path))[0]

    asset = db.scalar(
        select(VisualGeneratedAsset).where(VisualGeneratedAsset.generation_job_id == job.id).limit(1)
    )
    if asset is None:
        asset = VisualGeneratedAsset(
            visual_asset_plan_id=job.visual_asset_plan_id,
            visual_scene_id=job.visual_scene_id,
            generation_job_id=job.id,
            asset_type=job.job_type,
            file_path=str(resolved_path),
            file_exists=True,
            mime_type=mime_type,
            duration_seconds=payload.duration_seconds,
            width=payload.width,
            height=payload.height,
            notes=payload.notes,
        )
        db.add(asset)
    else:
        asset.file_path = str(resolved_path)
        asset.file_exists = True
        asset.mime_type = mime_type
        asset.duration_seconds = payload.duration_seconds
        asset.width = payload.width
        asset.height = payload.height
        asset.notes = payload.notes

    job.output_path = str(resolved_path)
    job.status = "imported"
    job.failure_reason = None

    if job.scene is not None:
        job.scene.asset_status = "generated"
        job.scene.generated_asset_path = str(resolved_path)

    recompute_plan_generation_status(job.plan)
    db.commit()
    db.refresh(asset)

    log_audit_event(
        db,
        "visual_generation_output_registered",
        f"Registered local generated asset for visual generation job #{job.id}",
        video_id=job.plan.video_id,
        metadata={
            "job_id": job.id,
            "asset_id": asset.id,
            "file_path": str(resolved_path),
            "asset_type": asset.asset_type,
            "scene_id": asset.visual_scene_id,
        },
    )
    return visual_generated_asset_payload(db, asset)


@router.get("/assets", response_model=list[VisualGeneratedAssetRead])
def list_visual_generated_assets(
    plan_id: int | None = None,
    scene_id: int | None = None,
    job_id: int | None = None,
    video_id: int | None = None,
    limit: int = Query(default=250, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    stmt = select(VisualGeneratedAsset).order_by(VisualGeneratedAsset.created_at.desc()).limit(limit)
    if plan_id is not None:
        stmt = stmt.where(VisualGeneratedAsset.visual_asset_plan_id == plan_id)
    if scene_id is not None:
        stmt = stmt.where(VisualGeneratedAsset.visual_scene_id == scene_id)
    if job_id is not None:
        stmt = stmt.where(VisualGeneratedAsset.generation_job_id == job_id)
    if video_id is not None:
        stmt = stmt.join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id).where(
            VisualAssetPlan.video_id == video_id
        )

    rows = list(db.scalars(stmt))
    return [visual_generated_asset_payload(db, row) for row in rows]


@router.get("/assets/review-queue", response_model=list[VisualGeneratedAssetReviewQueueItem])
def list_visual_generated_asset_review_queue(
    review_status: Literal["pending", "approved", "rejected", "all"] = "pending",
    video_id: int | None = None,
    plan_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[VisualGeneratedAssetReviewQueueItem]:
    stmt = (
        select(VisualGeneratedAsset)
        .options(
            selectinload(VisualGeneratedAsset.plan).selectinload(VisualAssetPlan.video),
            selectinload(VisualGeneratedAsset.scene),
        )
        .order_by(VisualGeneratedAsset.created_at.desc())
    )
    if plan_id is not None:
        stmt = stmt.where(VisualGeneratedAsset.visual_asset_plan_id == plan_id)
    if video_id is not None:
        stmt = stmt.join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id).where(
            VisualAssetPlan.video_id == video_id
        )

    rows = list(db.scalars(stmt))
    items: list[VisualGeneratedAssetReviewQueueItem] = []
    for row in rows:
        review_fields = asset_review_fields(db, row)
        status = str(review_fields.get("review_status") or "pending")
        if review_status != "all" and status != review_status:
            continue
        items.append(
            VisualGeneratedAssetReviewQueueItem(
                id=row.id,
                video_id=row.plan.video_id if row.plan is not None else None,
                video_title=row.plan.video.title if row.plan is not None and row.plan.video is not None else None,
                plan_id=row.visual_asset_plan_id,
                scene_id=row.visual_scene_id,
                scene_number=row.scene.scene_number if row.scene is not None else None,
                asset_type=row.asset_type,
                file_path=row.file_path,
                file_exists=bool(row.file_exists),
                review_status=status,
                review_notes=review_fields.get("review_notes"),
                reviewed_at=review_fields.get("reviewed_at"),
                created_at=row.created_at,
            )
        )
        if len(items) >= limit:
            break
    return items


@router.get("/assets/{asset_id}", response_model=VisualGeneratedAssetRead)
def get_visual_generated_asset(asset_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    row = get_visual_generated_asset_or_404(db, asset_id)
    return visual_generated_asset_payload(db, row)


@router.patch("/assets/{asset_id}/review")
def review_visual_generated_asset(
    asset_id: int,
    payload: VisualAssetReviewUpdate,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    asset = get_visual_generated_asset_or_404(db, asset_id)
    try:
        result = update_visual_asset_review(db, asset, payload.review_status, payload.review_notes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    log_audit_event(
        db,
        "visual_asset_review_updated",
        f"Updated visual generated asset #{asset.id} review status to {payload.review_status}",
        video_id=asset.plan.video_id,
        metadata={"asset_id": asset.id, "review_status": payload.review_status},
    )
    return result


@router.post("/assets/{asset_id}/approve")
def approve_visual_generated_asset(asset_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    asset = get_visual_generated_asset_or_404(db, asset_id)
    result = update_visual_asset_review(db, asset, "approved", None)
    db.commit()
    log_audit_event(
        db,
        "visual_asset_approved",
        f"Approved visual generated asset #{asset.id}",
        video_id=asset.plan.video_id,
        metadata={"asset_id": asset.id, "review_status": "approved"},
    )
    return result


@router.post("/assets/{asset_id}/reject")
def reject_visual_generated_asset(
    asset_id: int,
    payload: VisualAssetRejectRequest | None = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    asset = get_visual_generated_asset_or_404(db, asset_id)
    notes = payload.review_notes if payload else None
    result = update_visual_asset_review(db, asset, "rejected", notes)
    db.commit()
    log_audit_event(
        db,
        "visual_asset_rejected",
        f"Rejected visual generated asset #{asset.id}",
        video_id=asset.plan.video_id,
        metadata={"asset_id": asset.id, "review_status": "rejected"},
    )
    return result


@router.get("/preview-assets/videos/{video_id}")
def get_video_preview_visual_asset_manifest(video_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return build_preview_visual_manifest(db, video)
