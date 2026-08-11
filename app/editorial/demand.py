from __future__ import annotations

from datetime import datetime, timezone

from pydantic import Field, field_validator

from app.services.research import (
    ResearchFetchError,
    ResearchSetupRequiredError,
    fetch_youtube_sources,
)

from .contracts import (
    CandidateDemandSnapshot,
    DemandSnapshot,
    DemandVideo,
    EditorialSeed,
    FrozenEditorialModel,
    I4_MAX_CANDIDATES,
    I4_MAX_YOUTUBE_RESULTS_PER_CANDIDATE,
    normalize_key,
)


I4_HTTP_TIMEOUT_SECONDS = 8
I4_MAX_DEMAND_QUERY_CHARS = 500


class DemandProviderError(RuntimeError):
    """Sanitized failure from the bounded public YouTube demand provider."""


class DemandCandidateRequest(FrozenEditorialModel):
    candidate_key: str
    query: str = Field(min_length=1, max_length=500)

    @field_validator("candidate_key")
    @classmethod
    def normalize_candidate_key(cls, value: str) -> str:
        return normalize_key(value)


class DemandRequest(FrozenEditorialModel):
    campaign_id: int = Field(ge=1)
    seed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: tuple[DemandCandidateRequest, ...] = Field(
        min_length=1,
        max_length=I4_MAX_CANDIDATES,
    )
    max_results_per_candidate: int = Field(
        default=I4_MAX_YOUTUBE_RESULTS_PER_CANDIDATE,
        ge=1,
        le=I4_MAX_YOUTUBE_RESULTS_PER_CANDIDATE,
    )
    timeout_seconds: float = Field(
        default=I4_HTTP_TIMEOUT_SECONDS,
        gt=0,
        le=I4_HTTP_TIMEOUT_SECONDS,
    )

    @classmethod
    def from_seed(
        cls,
        *,
        campaign_id: int,
        seed_hash: str,
        seed: EditorialSeed,
    ) -> DemandRequest:
        return cls(
            campaign_id=campaign_id,
            seed_hash=seed_hash,
            candidates=tuple(
                DemandCandidateRequest(
                    candidate_key=candidate.candidate_key,
                    query=" ".join(
                        f"{candidate.topic} {candidate.angle}".split()
                    )[:I4_MAX_DEMAND_QUERY_CHARS].rstrip(),
                )
                for candidate in seed.candidates
            ),
        )


class YouTubeDemandProvider:
    """State-free, read-only adapter for bounded public YouTube demand evidence."""

    provider = "youtube_public_data"
    model = "i4-demand-v1"

    def acquire(self, request: DemandRequest, *, api_key: str) -> DemandSnapshot:
        normalized_api_key = api_key.strip()
        if not normalized_api_key:
            raise DemandProviderError("YouTube demand configuration is unavailable.")

        snapshots: list[CandidateDemandSnapshot] = []
        for candidate in request.candidates[:I4_MAX_CANDIDATES]:
            try:
                videos, channels = fetch_youtube_sources(
                    api_key=normalized_api_key,
                    query=candidate.query,
                    max_results=min(
                        request.max_results_per_candidate,
                        I4_MAX_YOUTUBE_RESULTS_PER_CANDIDATE,
                    ),
                    timeout=request.timeout_seconds,
                )
            except (ResearchFetchError, ResearchSetupRequiredError):
                raise DemandProviderError(
                    "YouTube demand evidence could not be acquired."
                ) from None
            except Exception:  # noqa: BLE001 - sanitize provider boundary
                raise DemandProviderError(
                    "YouTube demand evidence could not be acquired."
                ) from None

            subscribers_by_channel = {
                channel.youtube_channel_id: channel.subscriber_count
                for channel in channels
            }
            evidence_records: list[DemandVideo] = []
            seen_video_ids: set[str] = set()
            for video in videos:
                if len(evidence_records) == I4_MAX_YOUTUBE_RESULTS_PER_CANDIDATE:
                    break
                if video.youtube_video_id in seen_video_ids:
                    continue
                seen_video_ids.add(video.youtube_video_id)
                evidence_records.append(
                    DemandVideo(
                        video_id=video.youtube_video_id,
                        title=video.title or "Untitled public video",
                        channel_id=video.youtube_channel_id,
                        channel_title=video.channel_title,
                        published_at=(
                            video.published_at.astimezone(timezone.utc).isoformat()
                            if video.published_at is not None
                            and video.published_at.tzinfo is not None
                            else video.published_at.replace(tzinfo=timezone.utc).isoformat()
                            if video.published_at is not None
                            else None
                        ),
                        view_count=video.view_count,
                        like_count=video.like_count,
                        comment_count=video.comment_count,
                        channel_subscriber_count=subscribers_by_channel.get(
                            video.youtube_channel_id
                        ),
                    )
                )
            evidence = tuple(evidence_records)
            snapshots.append(
                CandidateDemandSnapshot(
                    candidate_key=candidate.candidate_key,
                    query=candidate.query,
                    videos=evidence,
                )
            )

        retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return DemandSnapshot(
            retrieved_at=retrieved_at,
            candidates=tuple(snapshots),
        )
