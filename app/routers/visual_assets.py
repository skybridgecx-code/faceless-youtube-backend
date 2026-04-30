from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import ProductionBrief, Video, VisualAssetPlan, VisualAssetPrompt, VisualScene
from app.schemas import VisualAssetPlanRead, VisualAssetPlanUpdate, VisualSceneUpdate
from app.services.audit import log_audit_event
from app.services.visual_assets import build_visual_plan_for_brief, build_visual_plan_for_video

router = APIRouter(prefix="/visual-assets", tags=["visual-assets"])


def get_plan_or_404(db: Session, plan_id: int) -> VisualAssetPlan:
    stmt = (
        select(VisualAssetPlan)
        .where(VisualAssetPlan.id == plan_id)
        .options(
            selectinload(VisualAssetPlan.scenes).selectinload(VisualScene.prompts),
            selectinload(VisualAssetPlan.prompts),
        )
        .limit(1)
    )
    plan = db.scalar(stmt)
    if not plan:
        raise HTTPException(status_code=404, detail="Visual asset plan not found")
    return plan


def get_scene_or_404(db: Session, scene_id: int) -> VisualScene:
    scene = db.get(VisualScene, scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Visual scene not found")
    return scene


def _create_plan_with_prompts(
    db: Session,
    *,
    source_type: str,
    video_id: int | None,
    brief_id: int | None,
    title: str,
    thumbnail_prompt: str,
    thumbnail_text: str,
    motion_style: str,
    color_direction: str,
    plan_notes: str,
    safety_notes: str,
    scenes_payload: list[dict[str, object]],
) -> VisualAssetPlan:
    plan = VisualAssetPlan(
        source_type=source_type,
        video_id=video_id,
        brief_id=brief_id,
        status="draft",
        title=title,
        thumbnail_prompt=thumbnail_prompt,
        thumbnail_text=thumbnail_text,
        motion_style=motion_style,
        color_direction=color_direction,
        plan_notes=plan_notes,
        safety_notes=safety_notes,
    )
    db.add(plan)
    db.flush()

    db.add(
        VisualAssetPrompt(
            plan_id=plan.id,
            scene_id=None,
            prompt_type="thumbnail",
            label="Thumbnail prompt",
            prompt_text=thumbnail_prompt,
        )
    )

    for row in scenes_payload:
        scene = VisualScene(
            plan_id=plan.id,
            scene_number=int(row["scene_number"]),
            scene_title=str(row["scene_title"]),
            narrative_beat=str(row["narrative_beat"]),
            on_screen_text=str(row["on_screen_text"]),
            image_prompt=str(row["image_prompt"]),
            animation_prompt=str(row["animation_prompt"]),
            b_roll_prompt=str(row["b_roll_prompt"]),
            dashboard_demo_prompt=str(row["dashboard_demo_prompt"]),
            safety_notes=str(row["safety_notes"]),
        )
        db.add(scene)
        db.flush()

        prompt_rows = [
            ("image", "Image prompt", scene.image_prompt),
            ("animation", "Animation prompt", scene.animation_prompt),
            ("b_roll", "B-roll prompt", scene.b_roll_prompt),
            ("dashboard_demo", "Dashboard/demo shot prompt", scene.dashboard_demo_prompt),
        ]
        for prompt_type, label, prompt_text in prompt_rows:
            db.add(
                VisualAssetPrompt(
                    plan_id=plan.id,
                    scene_id=scene.id,
                    prompt_type=prompt_type,
                    label=f"Scene {scene.scene_number}: {label}",
                    prompt_text=prompt_text,
                )
            )

    db.commit()
    return get_plan_or_404(db, plan.id)


def _sync_scene_prompt_rows(db: Session, scene: VisualScene) -> None:
    prompt_map = {
        "image": scene.image_prompt,
        "animation": scene.animation_prompt,
        "b_roll": scene.b_roll_prompt,
        "dashboard_demo": scene.dashboard_demo_prompt,
    }
    rows = list(
        db.scalars(
            select(VisualAssetPrompt).where(
                VisualAssetPrompt.scene_id == scene.id,
                VisualAssetPrompt.prompt_type.in_(tuple(prompt_map.keys())),
            )
        )
    )
    existing_types = {row.prompt_type for row in rows}
    for row in rows:
        row.prompt_text = prompt_map[row.prompt_type]

    for prompt_type, prompt_text in prompt_map.items():
        if prompt_type in existing_types:
            continue
        label = {
            "image": "Image prompt",
            "animation": "Animation prompt",
            "b_roll": "B-roll prompt",
            "dashboard_demo": "Dashboard/demo shot prompt",
        }[prompt_type]
        db.add(
            VisualAssetPrompt(
                plan_id=scene.plan_id,
                scene_id=scene.id,
                prompt_type=prompt_type,
                label=f"Scene {scene.scene_number}: {label}",
                prompt_text=prompt_text,
            )
        )


@router.post("/from-brief/{brief_id}", response_model=VisualAssetPlanRead)
def create_visual_plan_from_brief(brief_id: int, db: Session = Depends(get_db)) -> VisualAssetPlanRead:
    brief = db.get(ProductionBrief, brief_id)
    if not brief:
        raise HTTPException(status_code=404, detail="Production brief not found")

    draft = build_visual_plan_for_brief(brief)
    plan = _create_plan_with_prompts(
        db,
        source_type="brief",
        video_id=brief.promoted_video_id,
        brief_id=brief.id,
        title=draft.title,
        thumbnail_prompt=draft.thumbnail_prompt,
        thumbnail_text=draft.thumbnail_text,
        motion_style=draft.motion_style,
        color_direction=draft.color_direction,
        plan_notes=draft.plan_notes,
        safety_notes=draft.safety_notes,
        scenes_payload=[scene.__dict__ for scene in draft.scenes],
    )

    log_audit_event(
        db,
        "visual_plan_created_from_brief",
        f"Created visual asset plan from production brief: {brief.title}",
        video_id=brief.promoted_video_id,
        metadata={"plan_id": plan.id, "brief_id": brief.id, "scene_count": len(plan.scenes)},
    )
    return VisualAssetPlanRead.model_validate(plan)


@router.post("/from-video/{video_id}", response_model=VisualAssetPlanRead)
def create_visual_plan_from_video(video_id: int, db: Session = Depends(get_db)) -> VisualAssetPlanRead:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    draft = build_visual_plan_for_video(video)
    plan = _create_plan_with_prompts(
        db,
        source_type="video",
        video_id=video.id,
        brief_id=None,
        title=draft.title,
        thumbnail_prompt=draft.thumbnail_prompt,
        thumbnail_text=draft.thumbnail_text,
        motion_style=draft.motion_style,
        color_direction=draft.color_direction,
        plan_notes=draft.plan_notes,
        safety_notes=draft.safety_notes,
        scenes_payload=[scene.__dict__ for scene in draft.scenes],
    )

    log_audit_event(
        db,
        "visual_plan_created_from_video",
        f"Created visual asset plan from video: {video.title}",
        video_id=video.id,
        metadata={"plan_id": plan.id, "video_id": video.id, "scene_count": len(plan.scenes)},
    )
    return VisualAssetPlanRead.model_validate(plan)


@router.get("/plans", response_model=list[VisualAssetPlanRead])
def list_visual_asset_plans(
    video_id: int | None = None,
    brief_id: int | None = None,
    status: str | None = Query(default=None),
    limit: int = Query(default=150, ge=1, le=400),
    db: Session = Depends(get_db),
) -> list[VisualAssetPlanRead]:
    stmt = (
        select(VisualAssetPlan)
        .options(
            selectinload(VisualAssetPlan.scenes).selectinload(VisualScene.prompts),
            selectinload(VisualAssetPlan.prompts),
        )
        .order_by(VisualAssetPlan.created_at.desc())
        .limit(limit)
    )
    if video_id is not None:
        stmt = stmt.where(VisualAssetPlan.video_id == video_id)
    if brief_id is not None:
        stmt = stmt.where(VisualAssetPlan.brief_id == brief_id)
    if status:
        stmt = stmt.where(VisualAssetPlan.status == status)

    rows = list(db.scalars(stmt))
    return [VisualAssetPlanRead.model_validate(row) for row in rows]


@router.get("/plans/{plan_id}", response_model=VisualAssetPlanRead)
def get_visual_asset_plan(plan_id: int, db: Session = Depends(get_db)) -> VisualAssetPlanRead:
    plan = get_plan_or_404(db, plan_id)
    return VisualAssetPlanRead.model_validate(plan)


@router.patch("/plans/{plan_id}", response_model=VisualAssetPlanRead)
def update_visual_asset_plan(
    plan_id: int,
    payload: VisualAssetPlanUpdate,
    db: Session = Depends(get_db),
) -> VisualAssetPlanRead:
    plan = get_plan_or_404(db, plan_id)
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(plan, key, value)

    if "status" in update_data and str(update_data["status"]) == "ready_for_generation":
        plan.ready_marked_at = datetime.utcnow()
    elif "status" in update_data and str(update_data["status"]) != "ready_for_generation":
        plan.ready_marked_at = None

    if "thumbnail_prompt" in update_data:
        thumb_prompt = db.scalar(
            select(VisualAssetPrompt)
            .where(VisualAssetPrompt.plan_id == plan.id, VisualAssetPrompt.prompt_type == "thumbnail")
            .limit(1)
        )
        if thumb_prompt:
            thumb_prompt.prompt_text = str(plan.thumbnail_prompt)
        else:
            db.add(
                VisualAssetPrompt(
                    plan_id=plan.id,
                    scene_id=None,
                    prompt_type="thumbnail",
                    label="Thumbnail prompt",
                    prompt_text=str(plan.thumbnail_prompt),
                )
            )

    db.commit()
    refreshed = get_plan_or_404(db, plan.id)
    log_audit_event(
        db,
        "visual_plan_updated",
        f"Updated visual asset plan #{plan.id}",
        video_id=plan.video_id,
        metadata={"plan_id": plan.id, "updated_fields": sorted(update_data.keys())},
    )
    return VisualAssetPlanRead.model_validate(refreshed)


@router.patch("/scenes/{scene_id}", response_model=VisualAssetPlanRead)
def update_visual_scene(
    scene_id: int,
    payload: VisualSceneUpdate,
    db: Session = Depends(get_db),
) -> VisualAssetPlanRead:
    scene = get_scene_or_404(db, scene_id)
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(scene, key, value)

    _sync_scene_prompt_rows(db, scene)
    db.commit()

    plan = get_plan_or_404(db, scene.plan_id)
    log_audit_event(
        db,
        "visual_scene_updated",
        f"Updated visual scene #{scene.scene_number} for plan #{scene.plan_id}",
        video_id=plan.video_id,
        metadata={"plan_id": plan.id, "scene_id": scene.id, "updated_fields": sorted(update_data.keys())},
    )
    return VisualAssetPlanRead.model_validate(plan)


@router.post("/plans/{plan_id}/mark-ready", response_model=VisualAssetPlanRead)
def mark_visual_plan_ready(plan_id: int, db: Session = Depends(get_db)) -> VisualAssetPlanRead:
    plan = get_plan_or_404(db, plan_id)
    plan.status = "ready_for_generation"
    plan.ready_marked_at = datetime.utcnow()
    db.commit()

    refreshed = get_plan_or_404(db, plan.id)
    log_audit_event(
        db,
        "visual_plan_marked_ready",
        f"Marked visual asset plan #{plan.id} ready for generation",
        video_id=plan.video_id,
        metadata={"plan_id": plan.id, "status": plan.status},
    )
    return VisualAssetPlanRead.model_validate(refreshed)
