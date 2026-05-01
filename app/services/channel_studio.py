from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelStudioAgent,
    ContentType,
    PublishingPayload,
    Video,
    VideoPerformanceMetric,
    VisualAssetPlan,
    VisualGeneratedAsset,
)
from app.services.performance_feedback import compute_ctr, retention_band
from app.services.visual_asset_review import asset_review_fields

DEFAULT_CHANNEL_STUDIO_AGENTS: list[dict[str, object]] = [
    {
        "name": "AI Tools / Automation",
        "niche": "AI software tools and workflow automation",
        "target_viewer": "Founders, operators, and creators evaluating AI tools",
        "content_pillars": ["Tool comparisons", "Workflow automations", "Prompt engineering", "AI operations playbooks"],
        "title_style": "Clear outcome-led hooks with practical constraints.",
        "thumbnail_style": "Simple contrast headline + one core tool visual.",
        "script_style": "Hook quickly, show one repeatable workflow, close with next action.",
        "compliance_notes": "No guaranteed outcomes. Demonstrations only with realistic caveats.",
        "launch_wave": 1,
    },
    {
        "name": "Personal Finance Education",
        "niche": "Budgeting, debt strategy, and financial literacy basics",
        "target_viewer": "Young professionals and families building money discipline",
        "content_pillars": ["Budget frameworks", "Debt reduction education", "Savings systems", "Financial habits"],
        "title_style": "Educational, specific, and non-hype framing.",
        "thumbnail_style": "Clean numeric contrast and plain-language outcome text.",
        "script_style": "One concept per short with clear definitions and caution notes.",
        "compliance_notes": "Educational only. No investment promises or income guarantees.",
        "launch_wave": 1,
    },
    {
        "name": "Business Ideas / Side Hustles",
        "niche": "Practical small business and side-hustle concept breakdowns",
        "target_viewer": "Operators exploring realistic, low-risk side business ideas",
        "content_pillars": ["Business model breakdowns", "Offer design", "Validation workflows", "Execution checklists"],
        "title_style": "Risk-first with realistic effort framing.",
        "thumbnail_style": "One concept headline + execution cue visual.",
        "script_style": "Hook, one core idea, one realistic next step.",
        "compliance_notes": "No get-rich claims. Explicitly label assumptions and uncertainty.",
        "launch_wave": 1,
    },
    {
        "name": "Real Estate / Home Services",
        "niche": "Real estate and home service operator workflows",
        "target_viewer": "Contractors, service owners, and real estate operators",
        "content_pillars": ["Lead handling", "Operations SOPs", "Client communication", "Service workflow optimization"],
        "title_style": "Operational pain-point first with practical angle.",
        "thumbnail_style": "Before/after workflow contrast and simple promise text.",
        "script_style": "3-second hook, one operational fix, clear CTA.",
        "compliance_notes": "No fake client outcomes or unverified earnings claims.",
        "launch_wave": 2,
    },
    {
        "name": "Health/Fitness Education",
        "niche": "Evidence-aware wellness and fitness education",
        "target_viewer": "General audience seeking sustainable health habits",
        "content_pillars": ["Habit systems", "Training basics", "Nutrition literacy", "Recovery fundamentals"],
        "title_style": "Educational and specific without sensational claims.",
        "thumbnail_style": "One behavior cue and one concise headline.",
        "script_style": "Short educational explainers with conservative language.",
        "compliance_notes": "No medical guarantees. Encourage professional consultation where relevant.",
        "launch_wave": 2,
    },
    {
        "name": "Tech Tutorials / Software",
        "niche": "Software walkthroughs and practical technical tutorials",
        "target_viewer": "Builders and operators learning tools quickly",
        "content_pillars": ["Quick tutorials", "Debug tips", "Productivity setups", "Dev workflows"],
        "title_style": "Problem/solution framing with concrete scope.",
        "thumbnail_style": "Tool/UI visual + one concise fix statement.",
        "script_style": "Show setup, one key action, and expected result.",
        "compliance_notes": "No fabricated benchmarks. Reproducible local demonstrations only.",
        "launch_wave": 2,
    },
    {
        "name": "Weird History / Facts / Explainers",
        "niche": "Curiosity-driven history and fact explainers",
        "target_viewer": "General curiosity audience and explainer fans",
        "content_pillars": ["Historical oddities", "Myth vs fact", "Timeline explainers", "Context-driven trivia"],
        "title_style": "Curiosity hook + grounded context.",
        "thumbnail_style": "Single striking visual + short curiosity line.",
        "script_style": "Hook, one clear point, context, and loop ending.",
        "compliance_notes": "Avoid unverifiable claims. Use conservative wording for uncertain facts.",
        "launch_wave": 3,
    },
]

LAUNCH_STATUS_VALUES = {"planning", "ready_to_launch", "launched", "paused", "killed"}


def _pillars_to_json(pillars: list[str] | tuple[str, ...]) -> str:
    normalized = [str(item).strip() for item in pillars if str(item).strip()]
    return json.dumps(normalized[:8])


def parse_pillars(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def seed_default_channel_studio_agents(db: Session) -> list[ChannelStudioAgent]:
    existing = list(db.scalars(select(ChannelStudioAgent).order_by(ChannelStudioAgent.id.asc())))
    existing_by_name = {row.name.strip().lower(): row for row in existing}
    created = False

    for item in DEFAULT_CHANNEL_STUDIO_AGENTS:
        name = str(item["name"]).strip()
        key = name.lower()
        if key in existing_by_name:
            continue
        row = ChannelStudioAgent(
            name=name,
            niche=str(item["niche"]).strip(),
            target_viewer=str(item["target_viewer"]).strip(),
            content_pillars_json=_pillars_to_json(item.get("content_pillars", [])),
            title_style=str(item["title_style"]).strip(),
            thumbnail_style=str(item["thumbnail_style"]).strip(),
            script_style=str(item["script_style"]).strip(),
            compliance_notes=str(item["compliance_notes"]).strip(),
            launch_wave=int(item.get("launch_wave", 1)),
            launch_status="planning",
            channel_url=None,
            channel_handle=None,
            notes=None,
        )
        db.add(row)
        created = True

    if created:
        db.commit()

    return list(db.scalars(select(ChannelStudioAgent).order_by(ChannelStudioAgent.id.asc())))


@dataclass(frozen=True)
class ScoreboardRow:
    agent_id: int
    agent_name: str
    niche: str
    launch_status: str
    launch_wave: int
    videos_created: int
    shorts_created: int
    approved_count: int
    payload_ready_count: int
    thumbnail_pending_count: int
    metrics_sample_size: int
    average_ctr: float | None
    average_retention: float | None
    readiness_score: int
    recommended_action: str


def _latest_metrics_for_videos(db: Session, video_ids: list[int]) -> dict[int, VideoPerformanceMetric]:
    if not video_ids:
        return {}
    metrics = list(
        db.scalars(select(VideoPerformanceMetric).where(VideoPerformanceMetric.video_id.in_(video_ids)))
    )
    latest: dict[int, VideoPerformanceMetric] = {}
    for row in metrics:
        current = latest.get(row.video_id)
        if current is None:
            latest[row.video_id] = row
            continue
        current_key = (current.measured_at, current.updated_at, current.id)
        row_key = (row.measured_at, row.updated_at, row.id)
        if row_key > current_key:
            latest[row.video_id] = row
    return latest


def _thumbnail_pending_count(db: Session, video_ids: list[int]) -> int:
    if not video_ids:
        return 0
    assets = list(
        db.scalars(
            select(VisualGeneratedAsset)
            .join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id)
            .where(
                VisualAssetPlan.video_id.in_(video_ids),
                VisualGeneratedAsset.file_exists.is_(True),
                VisualGeneratedAsset.asset_type == "thumbnail",
            )
        )
    )
    pending = 0
    for asset in assets:
        review_status = str(asset_review_fields(db, asset).get("review_status") or "pending")
        if review_status != "approved":
            pending += 1
    return pending


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _recommended_action(*, videos_created: int, approved_count: int, payload_ready_count: int, thumbnail_pending_count: int) -> str:
    if videos_created == 0:
        return "Generate first shorts batch for this agent."
    if thumbnail_pending_count > 0:
        return "Review pending thumbnail/visual assets before launch planning."
    if approved_count < max(1, videos_created // 2):
        return "Run compliance and manual approval for the strongest shorts."
    if payload_ready_count < max(1, approved_count // 2):
        return "Generate publishing payloads for approved candidates."
    return "Agent is near launch readiness. Continue controlled manual QA."


def _readiness_score(
    *,
    videos_created: int,
    approved_count: int,
    payload_ready_count: int,
    thumbnail_pending_count: int,
    metrics_sample_size: int,
    average_ctr: float | None,
    average_retention: float | None,
) -> int:
    score = 15
    score += min(25, videos_created * 3)
    score += min(20, approved_count * 5)
    score += min(20, payload_ready_count * 8)
    score -= min(20, thumbnail_pending_count * 5)

    if metrics_sample_size == 0:
        score -= 3
    else:
        if average_ctr is not None:
            if average_ctr >= 6.0:
                score += 4
            elif average_ctr < 2.0:
                score -= 4
        retention_state = retention_band(average_retention, None)
        if retention_state == "strong":
            score += 4
        elif retention_state == "weak":
            score -= 4

    return max(0, min(100, int(score)))


def build_channel_studio_scoreboard(db: Session) -> dict[str, object]:
    agents = list(db.scalars(select(ChannelStudioAgent).order_by(ChannelStudioAgent.launch_wave.asc(), ChannelStudioAgent.id.asc())))
    rows: list[ScoreboardRow] = []

    for agent in agents:
        videos = list(
            db.scalars(
                select(Video)
                .where(Video.channel_studio_agent_id == agent.id)
                .order_by(Video.created_at.desc())
            )
        )
        video_ids = [video.id for video in videos]
        videos_created = len(videos)
        shorts_created = sum(1 for video in videos if video.content_type == ContentType.short)
        approved_count = sum(1 for video in videos if video.approved)
        payload_ready_count = sum(
            1
            for row in db.scalars(
                select(PublishingPayload).where(
                    PublishingPayload.video_id.in_(video_ids) if video_ids else PublishingPayload.video_id == -1,
                    PublishingPayload.ready_for_manual_upload.is_(True),
                )
            )
        ) if video_ids else 0
        thumbnail_pending_count = _thumbnail_pending_count(db, video_ids)

        latest_metrics = _latest_metrics_for_videos(db, video_ids)
        ctr_values: list[float] = []
        retention_values: list[float] = []
        for row in latest_metrics.values():
            ctr_value = compute_ctr(row.impressions, row.clicks, row.ctr)
            if ctr_value is not None:
                ctr_values.append(float(ctr_value))
            if row.average_percentage_viewed is not None:
                retention_values.append(float(row.average_percentage_viewed))

        avg_ctr = _avg(ctr_values)
        avg_retention = _avg(retention_values)
        sample_size = len(latest_metrics)

        readiness_score = _readiness_score(
            videos_created=videos_created,
            approved_count=approved_count,
            payload_ready_count=payload_ready_count,
            thumbnail_pending_count=thumbnail_pending_count,
            metrics_sample_size=sample_size,
            average_ctr=avg_ctr,
            average_retention=avg_retention,
        )
        recommended_action = _recommended_action(
            videos_created=videos_created,
            approved_count=approved_count,
            payload_ready_count=payload_ready_count,
            thumbnail_pending_count=thumbnail_pending_count,
        )

        rows.append(
            ScoreboardRow(
                agent_id=agent.id,
                agent_name=agent.name,
                niche=agent.niche,
                launch_status=agent.launch_status if agent.launch_status in LAUNCH_STATUS_VALUES else "planning",
                launch_wave=max(1, int(agent.launch_wave or 1)),
                videos_created=videos_created,
                shorts_created=shorts_created,
                approved_count=approved_count,
                payload_ready_count=payload_ready_count,
                thumbnail_pending_count=thumbnail_pending_count,
                metrics_sample_size=sample_size,
                average_ctr=avg_ctr,
                average_retention=avg_retention,
                readiness_score=readiness_score,
                recommended_action=recommended_action,
            )
        )

    wave_map: dict[int, list[ScoreboardRow]] = {}
    for row in rows:
        wave_map.setdefault(row.launch_wave, []).append(row)

    launch_waves: list[dict[str, object]] = []
    for wave in sorted(wave_map.keys()):
        wave_rows = sorted(wave_map[wave], key=lambda item: item.readiness_score, reverse=True)
        avg_wave_score = int(sum(item.readiness_score for item in wave_rows) / len(wave_rows)) if wave_rows else 0
        blockers: list[str] = []
        if any(item.videos_created == 0 for item in wave_rows):
            blockers.append("Some agents have no generated shorts yet.")
        if any(item.thumbnail_pending_count > 0 for item in wave_rows):
            blockers.append("Pending thumbnail/visual reviews remain.")
        if any(item.payload_ready_count == 0 and item.approved_count > 0 for item in wave_rows):
            blockers.append("Approved videos still missing ready publishing payloads.")

        launch_waves.append(
            {
                "launch_wave": wave,
                "agents": [row.__dict__ for row in wave_rows],
                "readiness_summary": f"Average readiness score: {avg_wave_score}/100",
                "blockers": blockers,
                "recommended_action": (
                    "Resolve blockers and complete manual QA before launch handoff."
                    if blockers
                    else "Wave appears ready for controlled manual launch planning."
                ),
            }
        )

    return {
        "manual_local_note": "Planning/local only - no YouTube channels are created here.",
        "agents": [row.__dict__ for row in rows],
        "launch_waves": launch_waves,
    }
