from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContentType, Video, VideoPerformanceMetric

PerformanceBand = Literal["unknown", "needs_data", "weak", "average", "strong"]


@dataclass(frozen=True)
class AnalyticsFeedback:
    analytics_signal: Literal["neutral", "positive", "negative"]
    analytics_confidence_adjustment: int
    analytics_reason: str
    analytics_sample_size: int
    analytics_adjusted_total_score: int
    analytics_band: PerformanceBand


def _safe_float(value: float | int | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def compute_ctr(impressions: int, clicks: int, existing_ctr: float | None = None) -> float | None:
    if existing_ctr is not None:
        return float(existing_ctr)
    if impressions <= 0:
        return None
    return round((max(0, clicks) / impressions) * 100.0, 2)


def ctr_band(ctr: float | None) -> PerformanceBand:
    if ctr is None:
        return "needs_data"
    if ctr >= 6.0:
        return "strong"
    if ctr >= 3.0:
        return "average"
    if ctr >= 1.0:
        return "weak"
    return "weak"


def retention_band(
    average_percentage_viewed: float | None,
    average_view_duration_seconds: float | None,
) -> PerformanceBand:
    pct = _safe_float(average_percentage_viewed)
    avd = _safe_float(average_view_duration_seconds)
    if pct is None and avd is None:
        return "needs_data"
    if (pct is not None and pct >= 45.0) or (avd is not None and avd >= 70.0):
        return "strong"
    if (pct is not None and pct >= 28.0) or (avd is not None and avd >= 40.0):
        return "average"
    return "weak"


def performance_band_from_metrics(
    *,
    views: int,
    impressions: int,
    ctr_value: float | None,
    average_percentage_viewed: float | None,
    average_view_duration_seconds: float | None,
    watch_time_minutes: float | None,
) -> PerformanceBand:
    if views <= 0 and impressions <= 0 and (watch_time_minutes or 0.0) <= 0:
        return "needs_data"
    ctr_state = ctr_band(ctr_value)
    retention_state = retention_band(average_percentage_viewed, average_view_duration_seconds)
    if ctr_state == "strong" and retention_state == "strong":
        return "strong"
    if ctr_state == "weak" or retention_state == "weak":
        return "weak"
    if ctr_state == "needs_data" and retention_state == "needs_data":
        return "needs_data"
    return "average"


def recommendation_for_band(band: PerformanceBand, ctr_state: PerformanceBand, retention_state: PerformanceBand) -> str:
    if band == "needs_data":
        return "Collect more local/manual metrics before changing strategy."
    if band == "strong":
        return "Keep this angle and create adjacent follow-up opportunities."
    if ctr_state == "weak":
        return "Improve hook/title/thumbnail positioning; test a sharper opening promise."
    if retention_state == "weak":
        return "Tighten structure and pacing; reduce filler in the first 30 seconds."
    return "Iterate incrementally and continue collecting local/manual metrics."


def latest_performance_for_video(db: Session, video_id: int) -> VideoPerformanceMetric | None:
    return db.scalar(
        select(VideoPerformanceMetric)
        .where(VideoPerformanceMetric.video_id == video_id)
        .order_by(VideoPerformanceMetric.measured_at.desc(), VideoPerformanceMetric.updated_at.desc())
        .limit(1)
    )


def performance_payload_for_video(video_id: int, metric: VideoPerformanceMetric | None) -> dict[str, object]:
    if metric is None:
        return {
            "video_id": video_id,
            "has_data": False,
            "performance_band": "needs_data",
            "ctr_band": "needs_data",
            "retention_band": "needs_data",
            "next_recommendation": "Collect local/manual metrics first.",
            "is_manual_local": True,
            "manual_local_note": "Manual/local metrics only — no YouTube API connected yet.",
        }

    ctr_value = compute_ctr(metric.impressions, metric.clicks, metric.ctr)
    ctr_state = ctr_band(ctr_value)
    retention_state = retention_band(metric.average_percentage_viewed, metric.average_view_duration_seconds)
    band = performance_band_from_metrics(
        views=metric.views,
        impressions=metric.impressions,
        ctr_value=ctr_value,
        average_percentage_viewed=metric.average_percentage_viewed,
        average_view_duration_seconds=metric.average_view_duration_seconds,
        watch_time_minutes=metric.watch_time_minutes,
    )
    return {
        "id": metric.id,
        "video_id": metric.video_id,
        "platform": metric.platform,
        "published_url": metric.published_url,
        "impressions": metric.impressions,
        "views": metric.views,
        "clicks": metric.clicks,
        "ctr": ctr_value,
        "average_view_duration_seconds": metric.average_view_duration_seconds,
        "average_percentage_viewed": metric.average_percentage_viewed,
        "watch_time_minutes": metric.watch_time_minutes,
        "likes": metric.likes,
        "comments": metric.comments,
        "subscribers_gained": metric.subscribers_gained,
        "published_at": metric.published_at,
        "measured_at": metric.measured_at,
        "notes": metric.notes,
        "created_at": metric.created_at,
        "updated_at": metric.updated_at,
        "has_data": True,
        "performance_band": band,
        "ctr_band": ctr_state,
        "retention_band": retention_state,
        "next_recommendation": recommendation_for_band(band, ctr_state, retention_state),
        "is_manual_local": True,
        "manual_local_note": "Manual/local metrics only — no YouTube API connected yet.",
    }


def _latest_metrics_rows(db: Session, channel_id: int) -> list[tuple[Video, VideoPerformanceMetric]]:
    videos = list(db.scalars(select(Video).where(Video.channel_id == channel_id)))
    rows: list[tuple[Video, VideoPerformanceMetric]] = []
    for video in videos:
        metric = latest_performance_for_video(db, video.id)
        if metric is None:
            continue
        rows.append((video, metric))
    return rows


def build_analytics_feedback_for_opportunity(
    db: Session,
    *,
    channel_id: int,
    pillar_hint: str,
    content_type: ContentType = ContentType.long,
    base_total_score: int,
) -> AnalyticsFeedback:
    all_rows = _latest_metrics_rows(db, channel_id)
    scoped = [
        (video, metric)
        for video, metric in all_rows
        if video.content_type == content_type and video.pillar.strip().lower() == (pillar_hint or "").strip().lower()
    ]
    if not scoped:
        return AnalyticsFeedback(
            analytics_signal="neutral",
            analytics_confidence_adjustment=0,
            analytics_reason="No local/manual performance sample exists yet for this pillar/content type; keeping score neutral.",
            analytics_sample_size=0,
            analytics_adjusted_total_score=max(1, int(base_total_score)),
            analytics_band="needs_data",
        )

    bands: list[PerformanceBand] = []
    ctr_values: list[float] = []
    retention_states: list[PerformanceBand] = []
    for _, metric in scoped:
        ctr_value = compute_ctr(metric.impressions, metric.clicks, metric.ctr)
        if ctr_value is not None:
            ctr_values.append(ctr_value)
        band = performance_band_from_metrics(
            views=metric.views,
            impressions=metric.impressions,
            ctr_value=ctr_value,
            average_percentage_viewed=metric.average_percentage_viewed,
            average_view_duration_seconds=metric.average_view_duration_seconds,
            watch_time_minutes=metric.watch_time_minutes,
        )
        bands.append(band)
        retention_states.append(retention_band(metric.average_percentage_viewed, metric.average_view_duration_seconds))

    strong_count = sum(1 for band in bands if band == "strong")
    weak_count = sum(1 for band in bands if band == "weak")
    sample_size = len(scoped)
    avg_ctr = (sum(ctr_values) / len(ctr_values)) if ctr_values else None
    ctr_state = ctr_band(avg_ctr)
    strong_retention = sum(1 for state in retention_states if state == "strong")
    weak_retention = sum(1 for state in retention_states if state == "weak")

    adjustment = 0
    signal: Literal["neutral", "positive", "negative"] = "neutral"
    signal_band: PerformanceBand = "average"
    reason = "Local/manual pillar sample is mixed; keeping opportunity score neutral."

    if strong_count >= max(1, sample_size // 2) and strong_retention >= max(1, sample_size // 2) and ctr_state == "strong":
        adjustment = 2
        signal = "positive"
        signal_band = "strong"
        reason = "Local/manual metrics in this pillar show strong CTR and retention; applying a small confidence lift."
    elif weak_count >= max(1, sample_size // 2) or weak_retention >= max(1, sample_size // 2) or ctr_state == "weak":
        adjustment = -2
        signal = "negative"
        signal_band = "weak"
        reason = "Local/manual metrics in this pillar show weak CTR or retention; applying a small confidence reduction."

    adjustment = max(-3, min(3, adjustment))
    adjusted = max(1, int(base_total_score) + adjustment)
    return AnalyticsFeedback(
        analytics_signal=signal,
        analytics_confidence_adjustment=adjustment,
        analytics_reason=reason,
        analytics_sample_size=sample_size,
        analytics_adjusted_total_score=adjusted,
        analytics_band=signal_band if signal != "neutral" else "average",
    )


def performance_summary_rows(db: Session, limit: int = 20) -> dict[str, object]:
    metrics = list(db.scalars(select(VideoPerformanceMetric)))
    latest_by_video: dict[int, VideoPerformanceMetric] = {}
    for row in metrics:
        current = latest_by_video.get(row.video_id)
        if current is None:
            latest_by_video[row.video_id] = row
            continue
        current_key = (current.measured_at or datetime.min, current.updated_at or datetime.min, current.id)
        row_key = (row.measured_at or datetime.min, row.updated_at or datetime.min, row.id)
        if row_key > current_key:
            latest_by_video[row.video_id] = row

    items: list[dict[str, object]] = []
    for video_id, row in latest_by_video.items():
        video = db.get(Video, video_id)
        if video is None:
            continue
        payload = performance_payload_for_video(video_id, row)
        items.append(
            {
                "video_id": video.id,
                "title": video.title,
                "content_type": video.content_type.value,
                "pillar": video.pillar,
                "views": int(row.views),
                "ctr": payload.get("ctr"),
                "retention": row.average_percentage_viewed,
                "performance_band": payload.get("performance_band", "needs_data"),
            }
        )
    ranked = sorted(
        items,
        key=lambda item: (
            {"strong": 3, "average": 2, "weak": 1, "needs_data": 0, "unknown": 0}.get(str(item["performance_band"]), 0),
            float(item["ctr"]) if isinstance(item["ctr"], (int, float)) else -1.0,
            int(item["views"]),
        ),
        reverse=True,
    )
    bounded = max(1, min(100, int(limit)))
    return {
        "top_videos": ranked[:bounded],
        "bottom_videos": list(reversed(ranked[-bounded:])),
        "total_videos_with_manual_metrics": len(ranked),
        "manual_local_note": "Manual/local metrics only — no YouTube API connected yet.",
    }
