from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AutopilotRun,
    Channel,
    ChannelStudioAgent,
    ContentType,
    OpportunityReviewStatus,
    ProductionBrief,
    PublishingPayload,
    Review,
    Video,
    VideoStatus,
    VideoOpportunity,
    VisualAssetPlan,
    VisualGeneratedAsset,
    VisualScene,
)
from app.routers import opportunities as opportunities_router
from app.routers import production_briefs as production_briefs_router
from app.routers import publishing_payloads as publishing_payloads_router
from app.routers import videos as videos_router
from app.routers import visual_assets as visual_assets_router
from app.schemas import (
    AutopilotFinalReadinessStatus,
    AutopilotRunRead,
    AutopilotRunRequest,
    AutopilotRunVideoSummary,
    FinalApprovalDecisionResponse,
    FinalApprovalQueueItem,
    GenerateRequest,
    OpportunityCreate,
    OpportunityReviewUpdate,
    ProductionBriefReviewUpdate,
)
from app.services.audit import log_audit_event
from app.services.compliance import run_compliance_checks
from app.services.content_engine import generate_video_ideas
from app.services.preview_visuals import build_preview_visual_manifest
from app.services.visual_asset_review import update_visual_asset_review

@dataclass(frozen=True)
class VideoPacketSummary:
    packet_path: str | None
    blockers: list[str]
    warnings: list[str]
    high_blockers: list[str]
    readiness_status: AutopilotFinalReadinessStatus
    next_required_action: str


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        message = str(value).strip()
        if not message or message in seen:
            continue
        seen.add(message)
        deduped.append(message)
    return deduped


def _primary_channel(db: Session) -> Channel:
    channel = db.scalar(select(Channel).order_by(Channel.created_at.asc(), Channel.id.asc()).limit(1))
    if channel is not None:
        return channel

    settings = get_settings()
    channel = Channel(
        name=settings.channel_default_name,
        niche="Local AI operator workflow",
        audience="Local/manual operator planning",
        brand_voice="Practical and conservative",
        visual_style="Local deterministic dashboard",
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


def _parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _serialize_run_video(item: dict[str, object]) -> AutopilotRunVideoSummary:
    return AutopilotRunVideoSummary(
        video_id=int(item.get("video_id") or 0),
        title=str(item.get("title") or "Untitled"),
        content_type=ContentType(str(item.get("content_type") or "short")),
        agent_id=int(item["agent_id"]) if isinstance(item.get("agent_id"), int) else None,
        agent_name=str(item["agent_name"]) if isinstance(item.get("agent_name"), str) else None,
        opportunity_id=int(item["opportunity_id"]) if isinstance(item.get("opportunity_id"), int) else None,
        brief_id=int(item["brief_id"]) if isinstance(item.get("brief_id"), int) else None,
        final_review_packet_path=(str(item["final_review_packet_path"]) if isinstance(item.get("final_review_packet_path"), str) else None),
        publishing_payload_path=(str(item["publishing_payload_path"]) if isinstance(item.get("publishing_payload_path"), str) else None),
        publishing_payload_status=(str(item["publishing_payload_status"]) if isinstance(item.get("publishing_payload_status"), str) else None),
        compliance_status=str(item.get("compliance_status") or "untested"),
        blockers=[str(v) for v in item.get("blockers", [])] if isinstance(item.get("blockers"), list) else [],
        warnings=[str(v) for v in item.get("warnings", [])] if isinstance(item.get("warnings"), list) else [],
        readiness_status=str(item.get("readiness_status") or "needs_human_fix"),
        next_required_action=str(item.get("next_required_action") or "Review the final packet and decide.")
    )


def _run_to_schema(run: AutopilotRun) -> AutopilotRunRead:
    try:
        parsed_videos = json.loads(run.videos_json or "[]")
    except json.JSONDecodeError:
        parsed_videos = []
    videos: list[AutopilotRunVideoSummary] = []
    if isinstance(parsed_videos, list):
        for item in parsed_videos:
            if isinstance(item, dict):
                videos.append(_serialize_run_video(item))

    blockers = _parse_json_list(run.blockers_json)
    warnings = _parse_json_list(run.warnings_json)
    next_required_action = "Review final approval queue."
    if run.blocked_count > 0:
        next_required_action = "Fix blockers in packet warnings, then rerun one-click prep for blocked candidates."
    elif run.ready_for_final_approval_count > 0:
        next_required_action = "Open final approval queue and approve/reject each packet."

    return AutopilotRunRead(
        run_id=run.id,
        run_status=str(run.run_status),
        requested_count=int(run.requested_count or 0),
        created_count=int(run.created_count or 0),
        ready_for_final_approval_count=int(run.ready_for_final_approval_count or 0),
        blocked_count=int(run.blocked_count or 0),
        videos=videos,
        blockers=blockers,
        warnings=warnings,
        next_required_action=next_required_action,
        created_at=run.created_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
    )


def _latest_asset_body(video: Video, asset_type: str) -> str | None:
    assets = [row for row in video.assets if row.asset_type.value == asset_type]
    if not assets:
        return None
    row = sorted(assets, key=lambda item: item.created_at, reverse=True)[0]
    text = (row.body or "").strip()
    return text if text else None


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _clip_text(value: str, *, max_len: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 3)].rstrip() + "..."


def _write_placeholder_visual(path: Path, *, scene_number: int, scene_title: str, prompt_excerpt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#111827"/>
    </linearGradient>
  </defs>
  <rect width="1280" height="720" fill="url(#bg)"/>
  <rect x="36" y="36" width="1208" height="648" rx="18" fill="none" stroke="#334155" stroke-width="2"/>
  <text x="72" y="116" fill="#93c5fd" font-family="Arial, Helvetica, sans-serif" font-size="30" font-weight="700">LOCAL PLACEHOLDER VISUAL</text>
  <text x="72" y="172" fill="#e2e8f0" font-family="Arial, Helvetica, sans-serif" font-size="38" font-weight="700">Scene {scene_number}: {_xml_escape(scene_title)}</text>
  <text x="72" y="236" fill="#cbd5e1" font-family="Arial, Helvetica, sans-serif" font-size="24">Prompt excerpt:</text>
  <foreignObject x="72" y="256" width="1136" height="340">
    <div xmlns="http://www.w3.org/1999/xhtml" style="color:#f8fafc;font-size:28px;line-height:1.35;font-family:Arial, Helvetica, sans-serif;">
      {_xml_escape(prompt_excerpt)}
    </div>
  </foreignObject>
  <text x="72" y="656" fill="#f59e0b" font-family="Arial, Helvetica, sans-serif" font-size="22">Review Prep placeholder only. Replace with approved production visual.</text>
</svg>
"""
    path.write_text(svg, encoding="utf-8")


def _create_scene_placeholders(db: Session, video: Video, plan: VisualAssetPlan) -> tuple[int, list[str]]:
    scenes = list(
        db.scalars(
            select(VisualScene)
            .where(VisualScene.plan_id == plan.id)
            .order_by(VisualScene.scene_number.asc(), VisualScene.id.asc())
        )
    )
    if not scenes:
        return 0, ["No visual scenes available for placeholder image generation."]

    output_dir = (get_settings().output_path / "visual_assets" / f"video_{video.id}").resolve()
    count = 0
    warnings: list[str] = []

    for scene in scenes:
        prompt_excerpt = _clip_text(scene.image_prompt or "", max_len=220)
        scene_title = _clip_text(scene.scene_title or "Untitled Scene", max_len=80)
        file_path = (output_dir / f"scene_{scene.scene_number:02d}.svg").resolve()
        _write_placeholder_visual(
            file_path,
            scene_number=int(scene.scene_number),
            scene_title=scene_title,
            prompt_excerpt=prompt_excerpt or "No prompt text available.",
        )

        existing = db.scalar(
            select(VisualGeneratedAsset)
            .where(
                VisualGeneratedAsset.visual_asset_plan_id == plan.id,
                VisualGeneratedAsset.visual_scene_id == scene.id,
                VisualGeneratedAsset.asset_type == "image",
            )
            .order_by(VisualGeneratedAsset.created_at.desc(), VisualGeneratedAsset.id.desc())
            .limit(1)
        )

        if existing is None:
            existing = VisualGeneratedAsset(
                visual_asset_plan_id=plan.id,
                visual_scene_id=scene.id,
                generation_job_id=None,
                asset_type="image",
                file_path=str(file_path),
                file_exists=True,
                mime_type="image/svg+xml",
                notes=(
                    "Review prep local placeholder visual. "
                    f"Scene {scene.scene_number}. Prompt excerpt: {prompt_excerpt or 'n/a'}"
                ),
            )
            db.add(existing)
            db.flush()
        else:
            existing.file_path = str(file_path)
            existing.file_exists = True
            existing.mime_type = "image/svg+xml"
            existing.notes = (
                "Review prep local placeholder visual. "
                f"Scene {scene.scene_number}. Prompt excerpt: {prompt_excerpt or 'n/a'}"
            )
            db.flush()

        update_visual_asset_review(
            db,
            existing,
            "pending",
            "Review prep placeholder visual requires manual review/replacement before real upload.",
        )
        scene.generated_asset_path = str(file_path)
        scene.asset_status = "generated"
        count += 1

    warnings.append("Generated local placeholder scene visuals. Manual review/replacement is still required.")
    return count, warnings


def _create_final_review_packet(
    *,
    db: Session,
    video: Video,
    agent: ChannelStudioAgent | None,
    opportunity: VideoOpportunity | None,
    brief: ProductionBrief | None,
    publishing_payload: PublishingPayload | None,
    export_path: str | None,
    blockers: list[str],
    warnings: list[str],
) -> VideoPacketSummary:
    preview_path = None
    if video.rendered_preview_path:
        candidate = Path(video.rendered_preview_path).expanduser().resolve()
        if candidate.is_file():
            preview_path = str(candidate)

    visual_manifest = build_preview_visual_manifest(db, video)
    visual_assets = [
        {
            "asset_id": int(row.get("asset_id") or 0),
            "asset_type": str(row.get("asset_type") or "unknown"),
            "scene_id": row.get("visual_scene_id"),
            "file_path": str(row.get("file_path") or ""),
            "review_status": str(row.get("review_status") or "pending"),
        }
        for row in visual_manifest.get("assets", [])
        if isinstance(row, dict) and isinstance(row.get("file_path"), str)
    ]

    thumbnail_path = None
    for row in visual_assets:
        if row["asset_type"] == "thumbnail" and row["file_path"]:
            thumbnail_path = str(row["file_path"])
            break

    compliance_report = run_compliance_checks(video)
    script_body = _latest_asset_body(video, "script")
    description_body = _latest_asset_body(video, "description")

    packet_blockers = list(blockers)
    packet_warnings = list(warnings)
    high_blockers: list[str] = []

    if compliance_report.overall_status == "blocked":
        message = "Compliance is blocked. Resolve high-severity compliance findings."
        packet_blockers.append(message)
        high_blockers.append(message)

    if not script_body:
        message = "Script asset is missing."
        packet_blockers.append(message)
        high_blockers.append(message)

    if not description_body:
        packet_warnings.append("Description asset is missing.")

    if not thumbnail_path:
        message = "Thumbnail image is missing."
        packet_blockers.append(message)
        high_blockers.append(message)

    if preview_path is None:
        message = "Preview file is missing."
        packet_blockers.append(message)
        high_blockers.append(message)

    if not visual_assets:
        message = "No visual asset files are registered."
        packet_blockers.append(message)
        high_blockers.append(message)

    packet_blockers = _dedupe(packet_blockers)
    packet_warnings = _dedupe(packet_warnings)
    high_blockers = _dedupe(high_blockers)

    readiness_status: AutopilotFinalReadinessStatus = "ready_for_final_approval"
    if high_blockers:
        readiness_status = "blocked"
    elif packet_blockers:
        readiness_status = "needs_human_fix"

    next_required_action = "Final packet ready for manual approve/reject decision."
    if readiness_status == "blocked":
        next_required_action = high_blockers[0]
    elif readiness_status == "needs_human_fix":
        next_required_action = packet_blockers[0]

    output_dir = (get_settings().output_path / "final_review" / f"video_{video.id}").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    packet_path = (output_dir / "final_review_packet.json").resolve()

    payload_doc = {
        "video": {
            "id": video.id,
            "title": video.title,
            "content_type": video.content_type.value,
            "workflow_status": video.status.value,
        },
        "agent": (
            {
                "id": agent.id,
                "name": agent.name,
                "niche": agent.niche,
                "target_viewer": agent.target_viewer,
                "launch_wave": agent.launch_wave,
                "launch_status": agent.launch_status,
            }
            if agent is not None
            else None
        ),
        "opportunity": (
            {
                "id": opportunity.id,
                "topic": opportunity.topic,
                "niche_lane": opportunity.niche_lane,
                "review_status": opportunity.review_status,
                "total_score": opportunity.total_score,
                "compliance_risk": opportunity.compliance_risk,
                "recommended_title": opportunity.recommended_title,
            }
            if opportunity is not None
            else None
        ),
        "brief": (
            {
                "id": brief.id,
                "title": brief.title,
                "status": brief.status,
                "hook": brief.hook,
                "cta": brief.cta,
                "compliance_notes": brief.compliance_notes,
            }
            if brief is not None
            else None
        ),
        "assets": {
            "script": script_body,
            "description": description_body,
            "thumbnail_prompt": _latest_asset_body(video, "thumbnail_prompt"),
            "youtube_metadata": _latest_asset_body(video, "youtube_metadata"),
            "shorts": _latest_asset_body(video, "shorts"),
        },
        "paths": {
            "preview_path": preview_path,
            "thumbnail_path": thumbnail_path,
            "visual_asset_paths": [str(row["file_path"]) for row in visual_assets if row.get("file_path")],
            "operator_export_path": export_path,
            "publishing_payload_path": publishing_payload.payload_path if publishing_payload is not None else None,
        },
        "visual_assets": visual_assets,
        "compliance": {
            "overall_status": compliance_report.overall_status,
            "checks": [check.model_dump(mode="json") for check in compliance_report.checks],
        },
        "publishing_payload": (
            {
                "status": publishing_payload.payload_status,
                "ready_for_manual_upload": bool(publishing_payload.ready_for_manual_upload),
                "payload_path": publishing_payload.payload_path,
                "blockers": _parse_json_list(publishing_payload.blockers_json),
                "warnings": _parse_json_list(publishing_payload.warnings_json),
            }
            if publishing_payload is not None
            else None
        ),
        "blockers": packet_blockers,
        "warnings": packet_warnings,
        "high_blockers": high_blockers,
        "readiness_status": readiness_status,
        "next_required_action": next_required_action,
        "final_approval_required": True,
        "local_only": True,
        "generated_at": datetime.utcnow().isoformat(),
    }
    packet_path.write_text(json.dumps(payload_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return VideoPacketSummary(
        packet_path=str(packet_path),
        blockers=packet_blockers,
        warnings=packet_warnings,
        high_blockers=high_blockers,
        readiness_status=readiness_status,
        next_required_action=next_required_action,
    )


def _read_final_packet(video: Video) -> dict[str, object] | None:
    if not video.final_review_packet_path:
        return None
    try:
        path = Path(video.final_review_packet_path).expanduser().resolve()
    except OSError:
        return None
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _video_queue_item(db: Session, video: Video) -> FinalApprovalQueueItem:
    agent = db.get(ChannelStudioAgent, video.channel_studio_agent_id) if video.channel_studio_agent_id else None
    packet = _read_final_packet(video)

    blockers: list[str] = []
    warnings: list[str] = []
    high_blockers: list[str] = []
    readiness_status: AutopilotFinalReadinessStatus = "blocked"
    next_required_action = "Run one-click prep to generate final packet."

    if packet is not None:
        blockers = [str(item) for item in packet.get("blockers", []) if str(item).strip()]
        warnings = [str(item) for item in packet.get("warnings", []) if str(item).strip()]
        high_blockers = [str(item) for item in packet.get("high_blockers", []) if str(item).strip()]
        readiness_status = str(packet.get("readiness_status") or "needs_human_fix")
        next_required_action = str(packet.get("next_required_action") or "Review final packet and decide.")

    payload_row = db.scalar(
        select(PublishingPayload)
        .where(PublishingPayload.video_id == video.id)
        .order_by(PublishingPayload.generated_at.desc(), PublishingPayload.updated_at.desc())
        .limit(1)
    )

    thumbnail_path = None
    preview_path = None
    if packet is not None and isinstance(packet.get("paths"), dict):
        paths = packet["paths"]
        thumbnail_path = str(paths.get("thumbnail_path")) if isinstance(paths.get("thumbnail_path"), str) else None
        preview_path = str(paths.get("preview_path")) if isinstance(paths.get("preview_path"), str) else None

    if packet is None:
        readiness_status = "blocked"

    if readiness_status == "ready_for_final_approval" and video.final_approval_status == "approved":
        next_required_action = "Final package already approved. Continue manual packaging/payload/upload checklist."

    return FinalApprovalQueueItem(
        video_id=video.id,
        title=video.title,
        content_type=video.content_type,
        agent_name=agent.name if agent is not None else None,
        final_review_packet_path=video.final_review_packet_path,
        preview_path=preview_path,
        thumbnail_path=thumbnail_path,
        compliance_status=run_compliance_checks(video).overall_status,
        publishing_payload_status=(payload_row.payload_status if payload_row is not None else None),
        blockers_count=len(blockers) + len(high_blockers),
        warnings_count=len(warnings),
        readiness_status=readiness_status,
        next_required_action=next_required_action,
        final_approval_status=str(video.final_approval_status or "pending"),
    )


def run_autopilot_batch(db: Session, payload: AutopilotRunRequest) -> AutopilotRunRead:
    requested_count = int(payload.count)
    batch_count = min(requested_count, 5)

    run = AutopilotRun(
        agent_id=payload.agent_id,
        run_status="running",
        content_type=payload.content_type.value,
        requested_count=requested_count,
        created_count=0,
        ready_for_final_approval_count=0,
        blocked_count=0,
        warnings_json="[]",
        blockers_json="[]",
        videos_json="[]",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    run_warnings: list[str] = []
    run_blockers: list[str] = []
    results: list[dict[str, object]] = []

    if requested_count > 5:
        run_warnings.append("Requested count exceeded max. Created up to 5 items in this run.")

    channel = _primary_channel(db)
    agent = db.get(ChannelStudioAgent, payload.agent_id) if payload.agent_id else None
    if payload.agent_id and agent is None:
        raise HTTPException(status_code=404, detail="Channel studio agent not found")

    ideas = generate_video_ideas(batch_count)

    for idx in range(batch_count):
        idea = ideas[idx]
        video_warnings: list[str] = []
        video_blockers: list[str] = []

        topic_suffix = f" [{payload.topic_seed}]" if payload.topic_seed else ""
        topic = f"{idea.get('title', f'Review Prep Topic {idx + 1}')}{topic_suffix}"[:240]
        niche_lane = (agent.niche if agent is not None else idea.get("pillar") or "Review prep lane")[:240]
        audience = (agent.target_viewer if agent is not None else idea.get("target_viewer") or "Local operator")[:500]
        monetization = "Manual/local educational workflow"

        opportunity = opportunities_router.create_opportunity(
            OpportunityCreate(
                channel_id=channel.id,
                topic=topic,
                niche_lane=niche_lane,
                audience=audience,
                monetization_path=monetization,
                notes="One-click review prep generated opportunity candidate.",
            ),
            db,
        )
        scored = opportunities_router.score_opportunity(opportunity.id, db)

        compliance_ok = int(scored.score.compliance_risk) <= 3
        review_status = OpportunityReviewStatus.approved_for_video if compliance_ok else OpportunityReviewStatus.shortlisted
        opportunities_router.review_opportunity(
            opportunity.id,
            OpportunityReviewUpdate(
                review_status=review_status,
                operator_notes=(
                    "Review prep auto-approved for brief promotion (compliance estimate acceptable)."
                    if compliance_ok
                    else "Review prep kept shortlisted; compliance risk estimate requires human review."
                ),
                decision_summary="Review prep production run review status applied.",
            ),
            db,
        )

        if not compliance_ok:
            message = "Opportunity compliance estimate is high; video was not auto-promoted."
            video_blockers.append(message)
            run_blockers.append(f"{topic}: {message}")
            results.append(
                {
                    "video_id": 0,
                    "title": str(scored.recommended_title),
                    "content_type": payload.content_type.value,
                    "agent_id": agent.id if agent is not None else None,
                    "agent_name": agent.name if agent is not None else None,
                    "opportunity_id": opportunity.id,
                    "brief_id": None,
                    "final_review_packet_path": None,
                    "publishing_payload_path": None,
                    "publishing_payload_status": None,
                    "compliance_status": "warning",
                    "blockers": _dedupe(video_blockers),
                    "warnings": _dedupe(video_warnings),
                    "readiness_status": "blocked",
                    "next_required_action": "Manually lower compliance risk or choose another opportunity.",
                }
            )
            continue

        brief_result = production_briefs_router.create_brief_from_opportunity(opportunity.id, db)
        brief = brief_result.brief
        production_briefs_router.review_production_brief(
            brief.id,
            ProductionBriefReviewUpdate(
                status="approved",
                operator_review_notes="Review prep approved brief for promotion (final approval still manual).",
            ),
            db,
        )

        promoted_video = production_briefs_router.promote_production_brief_to_video(brief.id, db)
        video = db.get(Video, promoted_video.id)
        if video is None:
            raise HTTPException(status_code=500, detail="Review prep promotion produced a missing video row.")

        video.content_type = payload.content_type
        video.channel_studio_agent_id = agent.id if agent is not None else None
        if agent is not None:
            video.niche = agent.niche[:240]
            video.target_viewer = agent.target_viewer[:240]
        video.pillar = str(idea.get("pillar") or video.pillar)[:120]
        video.pain_point = str(idea.get("pain_point") or video.pain_point)[:500]
        video.demo_idea = str(idea.get("demo_idea") or video.demo_idea)[:500]
        video.thumbnail_text = str(idea.get("thumbnail_text") or video.thumbnail_text)[:80]
        video.approved = False
        video.preview_reviewed = False
        video.preview_reviewed_at = None
        video.final_approval_status = "pending"
        video.final_approval_notes = None
        video.final_approval_decided_at = None
        db.commit()
        db.refresh(video)

        try:
            videos_router.generate_assets(video.id, GenerateRequest(stage="all"), db)
        except HTTPException as exc:
            message = f"Asset generation failed: {exc.detail}"
            video_blockers.append(message)
            run_blockers.append(f"Video {video.id}: {message}")

        plan_id: int | None = None
        try:
            plan_payload = visual_assets_router.create_visual_plan_from_video(video.id, db)
            plan_id = int(plan_payload.id)
        except HTTPException as exc:
            video_blockers.append(f"Visual plan creation failed: {exc.detail}")

        if plan_id is not None and payload.auto_generate_placeholders:
            plan_row = db.get(VisualAssetPlan, plan_id)
            if plan_row is not None:
                placeholder_count, placeholder_warnings = _create_scene_placeholders(db, video, plan_row)
                video_warnings.extend(placeholder_warnings)
                if placeholder_count == 0:
                    video_warnings.append("No placeholder visuals were created.")
                db.commit()

        if plan_id is not None and not payload.auto_generate_placeholders:
            video_warnings.append("Skipped placeholder visual generation by request.")

        try:
            thumbnail_result = videos_router.generate_thumbnail_image_asset(video.id, db)
            video_warnings.extend(thumbnail_result.warnings)
        except HTTPException as exc:
            video_blockers.append(f"Thumbnail generation failed: {exc.detail}")

        if payload.auto_render_preview:
            ffmpeg_bin = shutil.which("ffmpeg")
            qlmanage_bin = shutil.which("qlmanage")
            if ffmpeg_bin and qlmanage_bin:
                try:
                    videos_router.render_draft_preview(video.id, db)
                except HTTPException as exc:
                    video_warnings.append(f"Preview render skipped: {exc.detail}")
            else:
                video_warnings.append("Preview renderer is unavailable locally (ffmpeg/qlmanage missing).")
        else:
            video_warnings.append("Preview render skipped by request.")

        report = run_compliance_checks(video)
        if report.overall_status == "blocked":
            video_blockers.append("Compliance blocked. Resolve blocked findings before final approval.")

        export_payload = videos_router.export_operator_summary(video.id, db)
        export_path = export_payload.export_path

        payload_row: PublishingPayload | None = None
        payload_path: str | None = None
        payload_status: str | None = None
        if payload.auto_generate_payload:
            try:
                payload_result = publishing_payloads_router.generate_publishing_payload(video.id, db)
                payload_path = payload_result.payload_path
                payload_status = payload_result.payload_status
                payload_row = db.scalar(
                    select(PublishingPayload)
                    .where(PublishingPayload.video_id == video.id)
                    .order_by(PublishingPayload.generated_at.desc(), PublishingPayload.updated_at.desc())
                    .limit(1)
                )
                video_blockers.extend(payload_result.blockers)
                video_warnings.extend(payload_result.warnings)
            except HTTPException as exc:
                video_warnings.append(f"Publishing payload generation failed: {exc.detail}")
        else:
            video_warnings.append("Publishing payload generation skipped by request.")

        opportunity_row = db.get(VideoOpportunity, opportunity.id)
        brief_row = db.get(ProductionBrief, brief.id)
        packet = _create_final_review_packet(
            db=db,
            video=video,
            agent=agent,
            opportunity=opportunity_row,
            brief=brief_row,
            publishing_payload=payload_row,
            export_path=export_path,
            blockers=_dedupe(video_blockers),
            warnings=_dedupe(video_warnings),
        )

        video.final_review_packet_path = packet.packet_path
        video.final_approval_status = "pending"
        video.final_approval_notes = None
        video.final_approval_decided_at = None
        db.commit()
        db.refresh(video)

        log_audit_event(
            db,
            "review_prep_video_prepared",
            f"Review prep prepared final review packet for: {video.title}",
            video_id=video.id,
            metadata={
                "review_prep_run_id": run.id,
                "packet_path": packet.packet_path,
                "readiness_status": packet.readiness_status,
                "blockers_count": len(packet.blockers),
                "warnings_count": len(packet.warnings),
                "local_only": True,
            },
        )

        if packet.packet_path and run.final_review_packet_path is None:
            run.final_review_packet_path = packet.packet_path

        results.append(
            {
                "video_id": video.id,
                "title": video.title,
                "content_type": video.content_type.value,
                "agent_id": agent.id if agent is not None else None,
                "agent_name": agent.name if agent is not None else None,
                "opportunity_id": opportunity.id,
                "brief_id": brief.id,
                "final_review_packet_path": packet.packet_path,
                "publishing_payload_path": payload_path,
                "publishing_payload_status": payload_status,
                "compliance_status": report.overall_status,
                "blockers": packet.blockers,
                "warnings": packet.warnings,
                "readiness_status": packet.readiness_status,
                "next_required_action": packet.next_required_action,
            }
        )

    created_results = [row for row in results if int(row.get("video_id") or 0) > 0]
    ready_count = sum(1 for row in created_results if row.get("readiness_status") == "ready_for_final_approval")
    blocked_count = sum(1 for row in created_results if row.get("readiness_status") != "ready_for_final_approval")

    run.created_count = len(created_results)
    run.ready_for_final_approval_count = ready_count
    run.blocked_count = blocked_count
    run.warnings_json = json.dumps(_dedupe(run_warnings))
    run.blockers_json = json.dumps(_dedupe(run_blockers))
    run.videos_json = json.dumps(results)
    run.completed_at = datetime.utcnow()

    if run.created_count == 0 and run.blocked_count == 0:
        run.run_status = "failed"
    elif run.blocked_count > 0:
        run.run_status = "blocked"
    else:
        run.run_status = "completed"

    db.commit()
    db.refresh(run)

    log_audit_event(
        db,
        "review_prep_run_completed",
        f"Review prep run #{run.id} finished with status: {run.run_status}",
        metadata={
            "review_prep_run_id": run.id,
            "run_status": run.run_status,
            "requested_count": run.requested_count,
            "created_count": run.created_count,
            "ready_for_final_approval_count": run.ready_for_final_approval_count,
            "blocked_count": run.blocked_count,
        },
    )

    return _run_to_schema(run)


def list_autopilot_runs(db: Session, limit: int = 100) -> list[AutopilotRunRead]:
    rows = list(
        db.scalars(
            select(AutopilotRun)
            .order_by(AutopilotRun.created_at.desc(), AutopilotRun.id.desc())
            .limit(max(1, min(200, int(limit))))
        )
    )
    return [_run_to_schema(row) for row in rows]


def get_autopilot_run_or_404(db: Session, run_id: int) -> AutopilotRunRead:
    row = db.get(AutopilotRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Review prep run not found")
    return _run_to_schema(row)


def list_final_approval_queue(db: Session, limit: int = 100) -> list[FinalApprovalQueueItem]:
    rows = list(
        db.scalars(
            select(Video)
            .where(Video.final_review_packet_path.is_not(None))
            .order_by(Video.updated_at.desc(), Video.id.desc())
            .limit(max(1, min(200, int(limit))))
        )
    )
    return [_video_queue_item(db, row) for row in rows]


def apply_final_approval_decision(
    db: Session,
    *,
    video_id: int,
    decision: str,
    notes: str | None,
) -> FinalApprovalDecisionResponse:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")

    normalized = str(decision).strip().lower()
    if normalized not in {"approve", "reject", "needs_changes"}:
        raise HTTPException(status_code=422, detail="Invalid final approval decision")

    packet = _read_final_packet(video)
    if normalized == "approve":
        if packet is None:
            raise HTTPException(status_code=400, detail="Final review packet is required before approval.")
        report = run_compliance_checks(video)
        if report.overall_status == "blocked":
            raise HTTPException(status_code=400, detail="Cannot approve while compliance has blocked findings.")

        review = Review(
            video_id=video.id,
            passed=True,
            reviewer="review_prep_final_approval",
            notes=(notes or "Final package approved by operator."),
        )
        db.add(review)
        video.approved = True
        video.status = VideoStatus.approved
        video.final_approval_status = "approved"
        video.final_approval_notes = notes
        video.final_approval_decided_at = datetime.utcnow()
        db.commit()

        log_audit_event(
            db,
            "review_prep_final_approved",
            f"Final package approved for: {video.title}",
            video_id=video.id,
            metadata={"decision": normalized, "has_notes": bool(notes)},
        )

        return FinalApprovalDecisionResponse(
            video_id=video.id,
            decision="approve",
            approval_status="approved",
            next_required_action="Video approved locally. Continue package/payload/manual upload checklist as needed.",
        )

    if normalized == "reject":
        review = Review(
            video_id=video.id,
            passed=False,
            reviewer="review_prep_final_approval",
            notes=(notes or "Final package rejected by operator."),
        )
        db.add(review)
        video.approved = False
        video.status = VideoStatus.rejected
        video.final_approval_status = "rejected"
        video.final_approval_notes = notes
        video.final_approval_decided_at = datetime.utcnow()
        db.commit()

        log_audit_event(
            db,
            "review_prep_final_rejected",
            f"Final package rejected for: {video.title}",
            video_id=video.id,
            metadata={"decision": normalized, "has_notes": bool(notes)},
        )

        return FinalApprovalDecisionResponse(
            video_id=video.id,
            decision="reject",
            approval_status="rejected",
            next_required_action="Revise assets/brief/opportunity and rerun review prep when ready.",
        )

    # needs_changes
    video.approved = False
    if video.status == VideoStatus.rejected:
        video.status = VideoStatus.needs_review
    video.final_approval_status = "needs_changes"
    video.final_approval_notes = notes
    video.final_approval_decided_at = datetime.utcnow()
    db.commit()

    log_audit_event(
        db,
        "review_prep_final_needs_changes",
        f"Final package marked needs_changes for: {video.title}",
        video_id=video.id,
        metadata={"decision": normalized, "has_notes": bool(notes)},
    )

    return FinalApprovalDecisionResponse(
        video_id=video.id,
        decision="needs_changes",
        approval_status="needs_changes",
        next_required_action="Apply requested changes, regenerate packet, and re-run final approval.",
    )
