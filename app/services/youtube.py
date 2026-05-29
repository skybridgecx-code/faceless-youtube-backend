from __future__ import annotations

import json
from dataclasses import dataclass

from app.models import Video
from app.services.content_engine import DEFAULT_TAGS, build_description

# Required for 2026 YouTube policy: synthetic/altered media must be disclosed.
AI_DISCLOSURE_TEXT = (
    "This video was produced with AI assistance (script, voiceover, and/or visuals). "
    "Mark 'Altered or synthetic content' when uploading in YouTube Studio."
)


@dataclass(frozen=True)
class YouTubePayload:
    title: str
    description: str
    tags: list[str]
    category_id: str = "27"
    privacy_status: str = "private"
    made_for_kids: bool = False
    review_required: bool = True
    altered_or_synthetic_content: bool = True
    ai_disclosure: str = AI_DISCLOSURE_TEXT

    def as_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)


def prepare_payload(video: Video) -> YouTubePayload:
    return YouTubePayload(
        title=video.title[:100],
        description=build_description(video),
        tags=DEFAULT_TAGS,
        category_id="27",
        privacy_status="private",
        made_for_kids=False,
        review_required=True,
        altered_or_synthetic_content=True,
        ai_disclosure=AI_DISCLOSURE_TEXT,
    )


def upload_video_stub(*, video_file_path: str, payload: YouTubePayload) -> None:
    raise NotImplementedError(
        "YouTube upload is intentionally not enabled in this scaffold. "
        "Connect OAuth and YouTube Data API only after review, compliance, and quota setup."
    )
