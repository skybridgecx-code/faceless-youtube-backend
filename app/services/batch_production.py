"""Batch content production from a demand-ranked slate.

Turns a niche/query into a ranked set of topics (using the demand engine, with
live YouTube signals when available) and produces full text-asset packages for
the top topics: brief, script, shorts, monetized description, thumbnail prompt,
and YouTube metadata.

It stops at the human review gate — every produced video lands in
``needs_review`` / ``approved=False``. It renders no video and uploads nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import AssetType, Channel, ContentAsset, ContentType, Video, VideoStatus
from app.services.content_engine import build_all_assets
from app.services.idea_demand import score_ideas, suggest_candidate_topics
from app.services.research import SourceVideo


@dataclass(frozen=True)
class ProducedVideo:
    video_id: int
    title: str
    demand_score: int
    saturation_risk: str
    asset_count: int


@dataclass(frozen=True)
class BatchResult:
    channel_id: int
    requested: int
    produced: int
    live_signal_used: bool
    note: str
    videos: list[ProducedVideo] = field(default_factory=list)


def _title_from_topic(topic: str) -> str:
    return topic.strip()[:240]


def produce_content_batch(
    db: Session,
    *,
    channel_id: int,
    niche_lane: str,
    query: str,
    count: int,
    source_videos: list[SourceVideo] | None = None,
    content_type: ContentType = ContentType.long,
    proven_keywords: set[str] | None = None,
    avoid_keywords: set[str] | None = None,
) -> BatchResult:
    """Create ``count`` review-ready videos with full text assets, ranked by demand.

    Raises ``ValueError`` if the channel does not exist. Each created video is
    left in ``needs_review`` so the existing human gate is preserved.
    """
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise ValueError(f"Channel {channel_id} not found")

    n = max(1, min(25, int(count)))
    sources = source_videos or []
    live_signal_used = len(sources) > 0

    candidates = suggest_candidate_topics(
        niche_lane=niche_lane,
        query=query,
        source_videos=sources,
        limit=max(n * 2, n),
    )
    ranked = score_ideas(
        niche_lane=niche_lane,
        query=query,
        candidate_topics=candidates,
        source_videos=sources,
        proven_keywords=proven_keywords,
        avoid_keywords=avoid_keywords,
    )
    chosen = ranked[:n]

    produced: list[ProducedVideo] = []
    for idea in chosen:
        video = Video(
            channel_id=channel_id,
            title=_title_from_topic(idea.topic),
            content_type=content_type,
            niche=niche_lane[:240] or None,
            angle=idea.topic[:500],
            notes=idea.rationale[:500],
            status=VideoStatus.needs_review,
            approved=False,
        )
        db.add(video)
        db.flush()  # assign video.id before building assets

        generated = build_all_assets(video)
        asset_count = 0
        for item in generated:
            asset_type = item.asset_type if not isinstance(item, tuple) else item[0]
            body = item.body if not isinstance(item, tuple) else item[1]
            if not str(body or "").strip():
                continue
            db.add(ContentAsset(video_id=video.id, asset_type=AssetType(asset_type), body=body))
            asset_count += 1

        produced.append(
            ProducedVideo(
                video_id=video.id,
                title=video.title,
                demand_score=idea.demand_score,
                saturation_risk=idea.saturation_risk,
                asset_count=asset_count,
            )
        )

    db.commit()
    for pv in produced:
        refreshed = db.get(Video, pv.video_id)
        if refreshed is not None:
            db.refresh(refreshed)

    if live_signal_used:
        note = (
            f"Produced {len(produced)} review-ready video(s) from {len(sources)} live YouTube "
            "signal(s). Each is in needs_review — generate previews, review, then upload manually."
        )
    else:
        note = (
            f"Produced {len(produced)} review-ready video(s) from the keyword/demand heuristic "
            "(no live signal). Set YOUTUBE_DATA_API_KEY for live demand ranking. Each is in "
            "needs_review — generate previews, review, then upload manually."
        )

    return BatchResult(
        channel_id=channel_id,
        requested=n,
        produced=len(produced),
        live_signal_used=live_signal_used,
        note=note,
        videos=produced,
    )
