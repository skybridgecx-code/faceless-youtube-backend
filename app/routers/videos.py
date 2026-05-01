from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    AssetType,
    AuditEvent,
    Channel,
    ContentAsset,
    PublishRecord,
    Review,
    Video,
    VideoPerformanceMetric,
    VideoStatus,
    VisualAssetPlan,
    VisualAssetPrompt,
    VisualGeneratedAsset,
    VisualGenerationJob,
    VisualScene,
)
from app.schemas import (
    AssetPreview,
    AssetRead,
    AssetUpdate,
    AuditEventRead,
    BulkIdeaRequest,
    ComplianceReport,
    GenerateRequest,
    OperatorExport,
    OperatorExportVisualAsset,
    PackageResponse,
    PreviewReviewUpdate,
    PreviewStatus,
    ThumbnailGenerationResponse,
    VideoPerformanceRead,
    VideoPerformanceUpdate,
    ReviewCreate,
    ReviewRead,
    VideoBatchCreate,
    VideoCreate,
    VideoPublishUpdate,
    VideoRead,
    VideoReadiness,
    VideoUpdate,
)
from app.services.audit import log_audit_event
from app.services.compliance import run_compliance_checks
from app.services.content_engine import (
    build_all_assets,
    build_brief,
    build_description,
    build_script,
    build_shorts,
    build_thumbnail_prompt,
    build_youtube_metadata,
    generate_video_ideas,
)
from app.services.package_builder import build_video_package, slugify
from app.services.performance_feedback import latest_performance_for_video, performance_payload_for_video
from app.services.preview_visuals import build_preview_visual_manifest, build_visual_asset_review_summary
from app.services.thumbnail_generation import generate_thumbnail_image
from app.services.visual_asset_review import asset_review_fields, update_visual_asset_review
from app.services.visual_assets import build_visual_plan_for_video

router = APIRouter(prefix="/videos", tags=["videos"])
PREVIEW_FILENAME = "draft.mp4"
PREVIEW_VOICEOVER_FILENAME = "voiceover.aiff"
PREVIEW_RENDER_META_FILENAME = "render_meta.json"
PREVIEW_MIN_DURATION_SECONDS = 20
PREVIEW_MAX_DURATION_SECONDS = 60
PREVIEW_RESOLUTION = "1280x720"
PREVIEW_FPS = "30"
PREVIEW_DEFAULT_OPENAI_MODEL = "gpt-4o-mini-tts"
PREVIEW_DEFAULT_OPENAI_VOICE = "marin"
PREVIEW_DEFAULT_ELEVEN_MODEL = "eleven_multilingual_v2"


@dataclass
class VoiceoverResult:
    provider: str
    voice: str | None
    model: str | None
    path: Path | None
    audio_generated: bool
    silent_reason: str | None
    error_message: str | None = None


def get_video_or_404(db: Session, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def parse_asset_type_or_400(asset_type: str) -> AssetType:
    try:
        return AssetType(asset_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid asset type: {asset_type}") from exc


def latest_asset(video: Video, asset_type: AssetType) -> ContentAsset | None:
    assets = [asset for asset in video.assets if asset.asset_type == asset_type]
    return sorted(assets, key=lambda asset: asset.created_at, reverse=True)[0] if assets else None


def preview_root_dir() -> Path:
    root = (get_settings().output_path / "previews").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def expected_preview_path(video_id: int) -> Path:
    return preview_root_dir() / str(video_id) / PREVIEW_FILENAME


def preview_voiceover_path(video_id: int) -> Path:
    return preview_root_dir() / str(video_id) / PREVIEW_VOICEOVER_FILENAME


def preview_meta_path(video_id: int) -> Path:
    return preview_root_dir() / str(video_id) / PREVIEW_RENDER_META_FILENAME


def resolve_preview_path(video: Video) -> Path | None:
    root = preview_root_dir()
    candidates: list[Path] = []

    if video.rendered_preview_path:
        configured_path = Path(video.rendered_preview_path).expanduser()
        if not configured_path.is_absolute():
            configured_path = (root / configured_path).resolve()
        candidates.append(configured_path)

    candidates.append(expected_preview_path(video.id).resolve())

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        if resolved.is_file():
            return resolved
    return None


def read_preview_meta(video_id: int) -> dict[str, object]:
    meta_path = preview_meta_path(video_id)
    if not meta_path.is_file():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict):
        return raw
    return {}


def write_preview_meta(video_id: int, data: dict[str, object]) -> None:
    meta_path = preview_meta_path(video_id)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")


def clean_script_text(script_text: str) -> str:
    text = script_text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_preview_source_text(video: Video) -> str:
    script_asset = latest_asset(video, AssetType.script)
    if script_asset and script_asset.body.strip():
        return clean_script_text(script_asset.body)

    description_asset = latest_asset(video, AssetType.description)
    if description_asset and description_asset.body.strip():
        return clean_script_text(description_asset.body)

    brief_asset = latest_asset(video, AssetType.brief)
    if brief_asset and brief_asset.body.strip():
        return clean_script_text(brief_asset.body)

    raise HTTPException(status_code=409, detail="No script-like assets found. Generate assets first.")


def split_preview_sections(script_text: str, max_sections: int = 8) -> list[str]:
    paragraphs = [segment.strip() for segment in script_text.split("\n\n") if segment.strip()]
    sections: list[str] = paragraphs[:max_sections]

    if not sections:
        sentence_parts = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", script_text) if segment.strip()]
        if sentence_parts:
            chunk_size = 2
            sections = [
                " ".join(sentence_parts[index:index + chunk_size])
                for index in range(0, len(sentence_parts), chunk_size)
            ][:max_sections]

    if not sections:
        sections = [script_text]

    return sections


def build_slide_text_blocks(video: Video, script_text: str) -> list[str]:
    sections = split_preview_sections(script_text, max_sections=7)
    title_parts = ["DRAFT PREVIEW", video.title]
    if video.niche:
        title_parts.append(f"Niche: {video.niche}")
    if video.target_audience:
        title_parts.append(f"Audience: {video.target_audience}")
    slides = ["\n".join(title_parts)]
    slides.extend(sections)
    return slides


def parse_preview_tts_rate() -> int | None:
    raw_value = os.getenv("PREVIEW_TTS_RATE", "").strip()
    if not raw_value:
        return None
    try:
        parsed = int(raw_value)
    except ValueError:
        return None
    return max(80, min(420, parsed))


def sanitize_tts_text(script_text: str, max_chars: int = 5000) -> str:
    text = script_text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(?im)^\s*[-*#>`]+", "", text)
    text = re.sub(r"(?im)\b(?:hook|title|cta|call to action|scene|shot|stage direction)\s*:\s*", "", text)
    text = re.sub(r"(?im)^\s*\[[^\]]+\]\s*$", "", text)
    text = re.sub(r"[`*_~]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + " ..."
    return text


def select_tts_chain() -> list[str]:
    configured = os.getenv("PREVIEW_TTS_PROVIDER", "auto").strip().lower() or "auto"
    if configured == "auto":
        return ["elevenlabs", "openai", "macos", "silent"]
    if configured == "elevenlabs":
        return ["elevenlabs", "openai", "macos", "silent"]
    if configured == "openai":
        return ["openai", "macos", "silent"]
    if configured == "macos":
        return ["macos", "silent"]
    if configured == "silent":
        return ["silent"]
    return ["elevenlabs", "openai", "macos", "silent"]


def elevenlabs_tts(text: str, preview_dir: Path) -> VoiceoverResult:
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    model_id = os.getenv("ELEVENLABS_MODEL_ID", PREVIEW_DEFAULT_ELEVEN_MODEL).strip() or PREVIEW_DEFAULT_ELEVEN_MODEL

    if not api_key:
        return VoiceoverResult("elevenlabs", voice_id or None, model_id, None, False, None, "ELEVENLABS_API_KEY is not set.")
    if not voice_id:
        return VoiceoverResult("elevenlabs", None, model_id, None, False, None, "ELEVENLABS_VOICE_ID is required for ElevenLabs.")

    output_path = (preview_dir / "voiceover.mp3").resolve()
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.4, "similarity_boost": 0.75, "style": 0.2, "use_speaker_boost": True},
    }
    request = urllib.request.Request(
        url=f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            audio_bytes = response.read()
        output_path.write_bytes(audio_bytes)
    except urllib.error.HTTPError as exc:
        return VoiceoverResult("elevenlabs", voice_id, model_id, None, False, None, f"ElevenLabs HTTP error: {exc.code}")
    except Exception as exc:  # noqa: BLE001
        return VoiceoverResult("elevenlabs", voice_id, model_id, None, False, None, f"ElevenLabs request failed: {exc}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        return VoiceoverResult("elevenlabs", voice_id, model_id, None, False, None, "ElevenLabs returned empty audio.")
    return VoiceoverResult("elevenlabs", voice_id, model_id, output_path, True, None, None)


def openai_tts(text: str, preview_dir: Path) -> VoiceoverResult:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_TTS_MODEL", PREVIEW_DEFAULT_OPENAI_MODEL).strip() or PREVIEW_DEFAULT_OPENAI_MODEL
    voice = os.getenv("OPENAI_TTS_VOICE", PREVIEW_DEFAULT_OPENAI_VOICE).strip() or PREVIEW_DEFAULT_OPENAI_VOICE
    if not api_key:
        return VoiceoverResult("openai", voice, model, None, False, None, "OPENAI_API_KEY is not set.")

    output_path = (preview_dir / "voiceover.mp3").resolve()
    instructions = (
        "Speak like a confident, clear YouTube narrator for business owners. "
        "Natural pacing, warm tone, not robotic, not overly excited."
    )
    payload = {
        "model": model,
        "voice": voice,
        "input": text,
        "format": "mp3",
        "instructions": instructions,
    }
    request = urllib.request.Request(
        url="https://api.openai.com/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            audio_bytes = response.read()
        output_path.write_bytes(audio_bytes)
    except urllib.error.HTTPError as exc:
        return VoiceoverResult("openai", voice, model, None, False, None, f"OpenAI HTTP error: {exc.code}")
    except Exception as exc:  # noqa: BLE001
        return VoiceoverResult("openai", voice, model, None, False, None, f"OpenAI request failed: {exc}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        return VoiceoverResult("openai", voice, model, None, False, None, "OpenAI returned empty audio.")
    return VoiceoverResult("openai", voice, model, output_path, True, None, None)


def macos_tts(text: str, preview_dir: Path, video_id: int) -> VoiceoverResult:
    say_bin = shutil.which("say")
    if not say_bin:
        return VoiceoverResult(
            "macos",
            "Samantha",
            None,
            None,
            False,
            "Silent draft preview generated because local text-to-speech was unavailable.",
            "say command not found.",
        )
    output_path = preview_voiceover_path(video_id).resolve()
    rate = parse_preview_tts_rate()
    command = [say_bin, "-o", str(output_path), "-v", "Samantha"]
    if rate is not None:
        command.extend(["-r", str(rate)])
    command.append(text)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        return VoiceoverResult(
            "macos",
            "Samantha",
            None,
            None,
            False,
            "Silent draft preview generated because local text-to-speech failed.",
            "macOS say failed.",
        )
    return VoiceoverResult("macos", "Samantha", None, output_path, True, None, None)


def generate_voiceover(text: str, preview_dir: Path, video_id: int) -> VoiceoverResult:
    clean_text = sanitize_tts_text(text)
    chain = select_tts_chain()
    errors: list[str] = []

    for provider in chain:
        if provider == "silent":
            reason = "Silent draft preview generated because all configured TTS providers failed."
            if errors:
                reason = f"{reason} Last error: {errors[-1]}"
            return VoiceoverResult("silent", None, None, None, False, reason, errors[-1] if errors else None)
        if provider == "elevenlabs":
            result = elevenlabs_tts(clean_text, preview_dir)
        elif provider == "openai":
            result = openai_tts(clean_text, preview_dir)
        elif provider == "macos":
            result = macos_tts(clean_text, preview_dir, video_id)
        else:
            continue

        if result.audio_generated:
            return result
        if result.error_message:
            errors.append(result.error_message)
        if result.silent_reason and provider == "macos":
            errors.append(result.silent_reason)

    reason = "Silent draft preview generated because no TTS provider succeeded."
    if errors:
        reason = f"{reason} Last error: {errors[-1]}"
    return VoiceoverResult("silent", None, None, None, False, reason, errors[-1] if errors else None)


def estimate_preview_duration_seconds(script_text: str) -> int:
    words = max(1, len(script_text.split()))
    estimated = math.ceil(words / 2.6)
    return max(PREVIEW_MIN_DURATION_SECONDS, min(PREVIEW_MAX_DURATION_SECONDS, estimated))


def wrap_slide_text(raw_text: str, width: int = 44, max_lines: int = 10) -> str:
    wrapped = textwrap.wrap(raw_text, width=width)
    if not wrapped:
        return ""
    truncated = wrapped[:max_lines]
    if len(wrapped) > max_lines:
        truncated[-1] = truncated[-1].rstrip(". ") + "..."
    return "\n".join(truncated)


def build_slide_html_document(video: Video, slide_text: str, slide_index: int, total_slides: int) -> str:
    safe_title = html.escape(video.title)
    safe_slide = "<br/>".join(html.escape(line) for line in slide_text.splitlines() if line.strip())
    safe_niche = html.escape(video.niche) if video.niche else "General"
    safe_audience = html.escape(video.target_audience) if video.target_audience else "General audience"
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    body {{
      margin: 0;
      width: 1280px;
      height: 720px;
      background: linear-gradient(180deg, #121722 0%, #0e1118 100%);
      color: #f4f6fb;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      padding: 56px 72px;
      box-sizing: border-box;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .top {{
      font-size: 24px;
      letter-spacing: 0.08em;
      color: #8fb5ff;
      text-transform: uppercase;
      font-weight: 700;
    }}
    .title {{
      margin-top: 14px;
      font-size: 44px;
      line-height: 1.18;
      font-weight: 700;
      max-width: 1120px;
    }}
    .caption {{
      margin-top: 28px;
      font-size: 34px;
      line-height: 1.34;
      color: #ffffff;
      max-width: 1110px;
      min-height: 330px;
    }}
    .footer {{
      margin-top: 16px;
      border-top: 1px solid #2a3346;
      padding-top: 16px;
      display: flex;
      justify-content: space-between;
      font-size: 22px;
      color: #9aa8c5;
    }}
    .badge {{
      display: inline-block;
      padding: 8px 14px;
      border: 1px solid #3d4e70;
      border-radius: 24px;
      color: #b6cbff;
      font-size: 20px;
      margin-top: 18px;
    }}
  </style>
</head>
<body>
  <div>
    <div class="top">Local AI Operator • DRAFT PREVIEW</div>
    <div class="title">{safe_title}</div>
    <div class="badge">Niche: {safe_niche} • Audience: {safe_audience}</div>
    <div class="caption">{safe_slide}</div>
  </div>
  <div class="footer">
    <span>Concept Review Only • Not Final Production</span>
    <span>Slide {slide_index}/{total_slides}</span>
  </div>
</body>
</html>
"""


def sync_preview_state(video: Video) -> Path | None:
    preview_path = resolve_preview_path(video)
    if preview_path is None:
        video.rendered_preview_path = None
        video.preview_rendered_at = None
        video.preview_reviewed = False
        video.preview_reviewed_at = None
        return None

    video.rendered_preview_path = str(preview_path)
    if video.preview_rendered_at is None:
        video.preview_rendered_at = datetime.utcfromtimestamp(preview_path.stat().st_mtime)
    return preview_path


def clear_preview_state(video: Video) -> None:
    video.rendered_preview_path = None
    video.preview_rendered_at = None
    video.preview_reviewed = False
    video.preview_reviewed_at = None
    for artifact in (expected_preview_path(video.id), preview_voiceover_path(video.id), preview_meta_path(video.id)):
        try:
            artifact.unlink(missing_ok=True)
        except OSError:
            continue


def visual_asset_summary_for_video(db: Session, video_id: int) -> tuple[int, str | None]:
    assets = list(
        db.scalars(
            select(VisualGeneratedAsset)
            .join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id)
            .where(VisualAssetPlan.video_id == video_id, VisualGeneratedAsset.file_exists.is_(True))
        )
    )
    thumbnail_asset = next((row for row in assets if row.asset_type == "thumbnail"), None)
    return len(assets), (thumbnail_asset.file_path if thumbnail_asset else None)


def _dedupe_messages(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        message = str(value).strip()
        if not message or message in seen:
            continue
        seen.add(message)
        deduped.append(message)
    return deduped


def _next_required_action(
    *,
    assets_generated: bool,
    has_visual_plan: bool,
    has_visual_jobs_pending: bool,
    preview_rendered: bool,
    preview_reviewed: bool,
    review_approved: bool,
    package_created: bool,
    youtube_metadata_prepared: bool,
    publish_status_ready_or_scheduled: bool,
    publish_date_set: bool,
    compliance_status: str,
    visual_assets_registered_count: int,
    visual_assets_approved_count: int,
    has_unapproved_visual_assets: bool,
) -> str:
    if not assets_generated:
        return "Generate core assets first."
    if not has_visual_plan:
        return "Create a visual asset plan before rendering preview."
    if has_visual_jobs_pending:
        return "Register visual generation outputs before rendering preview."
    if not preview_rendered:
        if visual_assets_approved_count > 0:
            return "Render draft preview using approved visual assets."
        if visual_assets_registered_count > 0 and has_unapproved_visual_assets:
            return "Review pending/rejected visual assets, then render draft preview."
        return "Render draft preview."
    if has_unapproved_visual_assets:
        return "Resolve pending/rejected visual assets and confirm preview review."
    if not preview_reviewed:
        return "Manually review the draft preview."
    if compliance_status == "blocked":
        return "Resolve compliance blockers before approval."
    if not review_approved:
        return "Complete manual approval/rejection."
    if not package_created:
        return "Package video assets."
    if not youtube_metadata_prepared:
        return "Prepare the YouTube payload."
    if publish_status_ready_or_scheduled and not publish_date_set:
        return "Set a publish date for ready/scheduled status."
    return "Ready for manual local upload review."


def build_preview_status(video: Video, preview_path: Path | None, db: Session) -> PreviewStatus:
    expected_path = expected_preview_path(video.id).resolve()
    voiceover_path = preview_voiceover_path(video.id).resolve()
    voiceover_exists = voiceover_path.is_file() and voiceover_path.stat().st_size > 0
    meta = read_preview_meta(video.id)
    audio_generated = bool(meta.get("audio_generated", voiceover_exists))
    silent_reason = str(meta.get("silent_reason")) if meta.get("silent_reason") else None
    duration_seconds_value = meta.get("duration_seconds")
    duration_seconds = float(duration_seconds_value) if isinstance(duration_seconds_value, (int, float)) else None
    provider = str(meta.get("provider")) if meta.get("provider") else None
    voice = str(meta.get("voice")) if meta.get("voice") else None
    model = str(meta.get("model")) if meta.get("model") else None
    visual_summary = build_visual_asset_review_summary(db, video)
    visual_manifest = visual_summary.get("manifest", {})
    included_asset_paths = [
        str(path_value)
        for path_value in visual_manifest.get("included_asset_paths", [])
        if isinstance(path_value, str)
    ]
    visual_asset_warnings = [str(warning) for warning in visual_summary.get("warnings", []) if isinstance(warning, str)]
    try:
        visual_assets_used_count = int(visual_manifest.get("visual_assets_used_count", len(included_asset_paths)))
    except (TypeError, ValueError):
        visual_assets_used_count = len(included_asset_paths)
    try:
        visual_assets_missing_count = int(visual_manifest.get("visual_assets_missing_count", 0))
    except (TypeError, ValueError):
        visual_assets_missing_count = 0
    preview_asset_mode = str(visual_manifest.get("preview_asset_mode", "fallback_only"))
    visual_thumbnail_path = next(
        (
            str(asset.get("file_path"))
            for asset in visual_manifest.get("assets", [])
            if isinstance(asset, dict)
            and asset.get("asset_type") == "thumbnail"
            and isinstance(asset.get("file_path"), str)
        ),
        None,
    )
    preview_rendered = preview_path is not None
    preview_reviewed = bool(video.preview_reviewed) and preview_rendered
    has_unapproved_visual_assets = bool(visual_summary.get("has_unapproved_visual_assets"))
    next_required_action = _next_required_action(
        assets_generated=len(video.assets) > 0,
        has_visual_plan=visual_manifest.get("plan_id") is not None,
        has_visual_jobs_pending=False,
        preview_rendered=preview_rendered,
        preview_reviewed=preview_reviewed,
        review_approved=bool(video.approved),
        package_created=latest_asset(video, AssetType.package_manifest) is not None,
        youtube_metadata_prepared=bool(
            db.scalar(
                select(PublishRecord.id)
                .where(PublishRecord.video_id == video.id, PublishRecord.platform == "youtube")
                .limit(1)
            )
        ),
        publish_status_ready_or_scheduled=video.publish_status in ("ready", "scheduled"),
        publish_date_set=video.publish_date is not None,
        compliance_status=run_compliance_checks(video).overall_status,
        visual_assets_registered_count=int(visual_summary.get("visual_assets_registered_count", 0)),
        visual_assets_approved_count=int(visual_summary.get("visual_assets_approved_count", 0)),
        has_unapproved_visual_assets=has_unapproved_visual_assets,
    )

    return PreviewStatus(
        video_id=video.id,
        title=video.title,
        preview_exists=preview_rendered,
        preview_url=f"/videos/{video.id}/preview" if preview_rendered else None,
        preview_path=str(preview_path) if preview_rendered else None,
        expected_path=str(expected_path),
        preview_rendered_at=video.preview_rendered_at,
        preview_reviewed=preview_reviewed,
        preview_reviewed_at=video.preview_reviewed_at,
        audio_generated=audio_generated,
        voiceover_path=str(voiceover_path) if voiceover_exists else None,
        silent_reason=silent_reason,
        duration_seconds=duration_seconds,
        tts_provider=provider,
        tts_voice=voice,
        tts_model=model,
        preview_asset_mode=preview_asset_mode,
        visual_assets_used_count=visual_assets_used_count,
        visual_assets_missing_count=visual_assets_missing_count,
        included_asset_paths=included_asset_paths,
        visual_asset_warnings=visual_asset_warnings,
        visual_assets_registered=visual_assets_used_count > 0,
        visual_assets_count=visual_assets_used_count,
        visual_thumbnail_path=visual_thumbnail_path,
        visual_assets_registered_count=int(visual_summary.get("visual_assets_registered_count", 0)),
        visual_assets_approved_count=int(visual_summary.get("visual_assets_approved_count", 0)),
        visual_assets_pending_count=int(visual_summary.get("visual_assets_pending_count", 0)),
        visual_assets_rejected_count=int(visual_summary.get("visual_assets_rejected_count", 0)),
        preview_has_unapproved_visual_assets=has_unapproved_visual_assets,
        next_required_action=next_required_action,
    )


def build_readiness(video: Video, db: Session) -> VideoReadiness:
    preview_path = sync_preview_state(video)
    assets_generated = len(video.assets) > 0
    has_visual_plan = (
        db.scalar(select(VisualAssetPlan.id).where(VisualAssetPlan.video_id == video.id).limit(1)) is not None
    )
    has_visual_jobs_pending = (
        db.scalar(
            select(VisualGenerationJob.id)
            .join(VisualAssetPlan, VisualAssetPlan.id == VisualGenerationJob.visual_asset_plan_id)
            .where(
                VisualAssetPlan.video_id == video.id,
                VisualGenerationJob.status.in_(("queued", "exported")),
            )
            .limit(1)
        )
        is not None
    )
    preview_rendered = preview_path is not None
    preview_reviewed = bool(video.preview_reviewed) and preview_rendered
    review_approved = video.approved
    package_created = latest_asset(video, AssetType.package_manifest) is not None

    youtube_record = db.scalar(
        select(PublishRecord)
        .where(PublishRecord.video_id == video.id, PublishRecord.platform == "youtube")
        .limit(1)
    )
    youtube_metadata_prepared = youtube_record is not None
    publish_date_set = video.publish_date is not None
    publish_status_ready_or_scheduled = video.publish_status in ("ready", "scheduled")
    visual_summary = build_visual_asset_review_summary(db, video)
    has_unapproved_visual_assets = bool(visual_summary.get("has_unapproved_visual_assets"))

    blocking_reasons: list[str] = []
    if not assets_generated:
        blocking_reasons.append("Assets must be generated first")
    if not has_visual_plan:
        blocking_reasons.append("Visual asset plan should be created before preview render")
    if has_visual_jobs_pending:
        blocking_reasons.append("Visual generation outputs should be registered before preview render")
    if not preview_rendered:
        blocking_reasons.append("Draft preview must be rendered before packaging")
    if preview_rendered and not preview_reviewed:
        blocking_reasons.append("Draft preview must be manually reviewed")
    if not review_approved:
        blocking_reasons.append("Video must be approved via manual review")
    if not package_created:
        blocking_reasons.append("Video must be packaged")
    if not youtube_metadata_prepared:
        blocking_reasons.append("YouTube metadata must be prepared")
    if publish_status_ready_or_scheduled and not publish_date_set:
        blocking_reasons.append("Publish date should be set if status is ready or scheduled")

    report = run_compliance_checks(video)
    if report.overall_status == "blocked":
        blocking_reasons.append("Compliance checks are blocked")
    warnings = [str(item) for item in visual_summary.get("warnings", []) if isinstance(item, str)]
    if has_unapproved_visual_assets:
        warnings.append("Registered visual assets include pending/rejected items awaiting operator review.")
    next_required_action = _next_required_action(
        assets_generated=assets_generated,
        has_visual_plan=has_visual_plan,
        has_visual_jobs_pending=has_visual_jobs_pending,
        preview_rendered=preview_rendered,
        preview_reviewed=preview_reviewed,
        review_approved=review_approved,
        package_created=package_created,
        youtube_metadata_prepared=youtube_metadata_prepared,
        publish_status_ready_or_scheduled=publish_status_ready_or_scheduled,
        publish_date_set=publish_date_set,
        compliance_status=report.overall_status,
        visual_assets_registered_count=int(visual_summary.get("visual_assets_registered_count", 0)),
        visual_assets_approved_count=int(visual_summary.get("visual_assets_approved_count", 0)),
        has_unapproved_visual_assets=has_unapproved_visual_assets,
    )

    return VideoReadiness(
        assets_generated=assets_generated,
        preview_rendered=preview_rendered,
        preview_reviewed=preview_reviewed,
        review_approved=review_approved,
        package_created=package_created,
        youtube_metadata_prepared=youtube_metadata_prepared,
        publish_date_set=publish_date_set,
        publish_status_ready_or_scheduled=publish_status_ready_or_scheduled,
        blocking_reasons=_dedupe_messages(blocking_reasons),
        warnings=_dedupe_messages(warnings),
        visual_assets_registered_count=int(visual_summary.get("visual_assets_registered_count", 0)),
        visual_assets_approved_count=int(visual_summary.get("visual_assets_approved_count", 0)),
        visual_assets_pending_count=int(visual_summary.get("visual_assets_pending_count", 0)),
        visual_assets_rejected_count=int(visual_summary.get("visual_assets_rejected_count", 0)),
        preview_has_unapproved_visual_assets=has_unapproved_visual_assets,
        next_required_action=next_required_action,
        compliance_status=report.overall_status,
        compliance_blockers_count=sum(1 for check in report.checks if check.status == "blocked"),
        compliance_warnings_count=sum(1 for check in report.checks if check.status == "warning"),
    )


def package_dir_for(video: Video) -> str | None:
    if latest_asset(video, AssetType.package_manifest) is None:
        return None
    return str(get_settings().output_path / f"{video.id:04d}-{slugify(video.title)}")


def asset_previews(video: Video) -> list[AssetPreview]:
    previews: list[AssetPreview] = []
    for asset in sorted(video.assets, key=lambda item: (item.asset_type.value, item.created_at)):
        safe_preview = " ".join(asset.body.split())[:180]
        previews.append(
            AssetPreview(
                id=asset.id,
                asset_type=asset.asset_type,
                version=asset.version,
                created_at=asset.created_at,
                body_length=len(asset.body),
                body_preview=safe_preview,
            )
        )
    return previews


def _latest_asset_reference(video: Video, asset_type: AssetType, preview_chars: int = 220) -> dict[str, object]:
    asset = latest_asset(video, asset_type)
    if asset is None:
        return {"exists": False}
    return {
        "exists": True,
        "asset_id": asset.id,
        "version": asset.version,
        "created_at": asset.created_at,
        "body_length": len(asset.body),
        "body_preview": " ".join(asset.body.split())[:preview_chars],
    }


def _latest_visual_plan_for_video(db: Session, video_id: int) -> VisualAssetPlan | None:
    return db.scalar(
        select(VisualAssetPlan)
        .where(VisualAssetPlan.video_id == video_id)
        .order_by(VisualAssetPlan.updated_at.desc(), VisualAssetPlan.created_at.desc())
        .limit(1)
    )


def _ensure_visual_plan_for_thumbnail(db: Session, video: Video, thumbnail_prompt: str) -> VisualAssetPlan:
    plan = _latest_visual_plan_for_video(db, video.id)
    if plan is not None:
        return plan

    draft = build_visual_plan_for_video(video)
    plan = VisualAssetPlan(
        source_type="video",
        video_id=video.id,
        brief_id=None,
        status="draft",
        title=draft.title,
        thumbnail_prompt=thumbnail_prompt or draft.thumbnail_prompt,
        thumbnail_text=video.thumbnail_text or draft.thumbnail_text,
        motion_style=draft.motion_style,
        color_direction=draft.color_direction,
        plan_notes=draft.plan_notes,
        safety_notes=draft.safety_notes,
    )
    db.add(plan)
    db.flush()

    db.add(
        VisualAssetPrompt(
            plan_id=plan.id,
            scene_id=None,
            prompt_type="thumbnail",
            label="Thumbnail prompt",
            prompt_text=thumbnail_prompt or draft.thumbnail_prompt,
        )
    )
    for scene in draft.scenes:
        scene_row = VisualScene(
            plan_id=plan.id,
            scene_number=scene.scene_number,
            scene_title=scene.scene_title,
            narrative_beat=scene.narrative_beat,
            on_screen_text=scene.on_screen_text,
            image_prompt=scene.image_prompt,
            animation_prompt=scene.animation_prompt,
            b_roll_prompt=scene.b_roll_prompt,
            dashboard_demo_prompt=scene.dashboard_demo_prompt,
            safety_notes=scene.safety_notes,
        )
        db.add(scene_row)
        db.flush()
        for prompt_type, label, prompt_text in [
            ("image", "Image prompt", scene.image_prompt),
            ("animation", "Animation prompt", scene.animation_prompt),
            ("b_roll", "B-roll prompt", scene.b_roll_prompt),
            ("dashboard_demo", "Dashboard/demo shot prompt", scene.dashboard_demo_prompt),
        ]:
            db.add(
                VisualAssetPrompt(
                    plan_id=plan.id,
                    scene_id=scene_row.id,
                    prompt_type=prompt_type,
                    label=f"Scene {scene.scene_number}: {label}",
                    prompt_text=prompt_text,
                )
            )
    db.flush()
    return plan


@router.post("", response_model=VideoRead)
def create_video(payload: VideoCreate, db: Session = Depends(get_db)) -> Video:
    channel = db.get(Channel, payload.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    video = Video(**payload.model_dump())
    db.add(video)
    db.commit()
    db.refresh(video)
    log_audit_event(
        db,
        "video_created",
        f"Created video idea: {video.title}",
        video_id=video.id,
        metadata={"title": video.title, "channel_id": video.channel_id},
    )
    return video


@router.post("/ideas/bulk", response_model=list[VideoRead])
def create_bulk_ideas(payload: BulkIdeaRequest, db: Session = Depends(get_db)) -> list[Video]:
    channel = db.get(Channel, payload.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    videos: list[Video] = []
    for idea in generate_video_ideas(payload.count):
        video = Video(channel_id=payload.channel_id, **idea)
        db.add(video)
        videos.append(video)
    db.commit()
    for video in videos:
        db.refresh(video)
    log_audit_event(
        db,
        "batch_created",
        f"Generated {len(videos)} bulk video ideas",
        metadata={"channel_id": payload.channel_id, "count": len(videos), "video_ids": [v.id for v in videos]},
    )
    return videos


@router.get("", response_model=list[VideoRead])
def list_videos(
    channel_id: int | None = None,
    status: VideoStatus | None = None,
    publish_status: str | None = None,
    approved: bool | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Video]:
    stmt = select(Video).order_by(Video.created_at.desc())
    if channel_id is not None:
        stmt = stmt.where(Video.channel_id == channel_id)
    if status is not None:
        stmt = stmt.where(Video.status == status)
    if publish_status is not None:
        stmt = stmt.where(Video.publish_status == publish_status)
    if approved is not None:
        stmt = stmt.where(Video.approved == approved)
    if search:
        search_term = f"%{search}%"
        stmt = stmt.where(
            or_(
                Video.title.ilike(search_term),
                Video.niche.ilike(search_term),
                Video.target_audience.ilike(search_term),
                Video.angle.ilike(search_term),
                Video.notes.ilike(search_term),
            )
        )
    stmt = stmt.offset(offset).limit(limit)
    videos = list(db.scalars(stmt))
    for video in videos:
        sync_preview_state(video)
    db.commit()
    return videos


@router.post("/batch", response_model=list[VideoRead])
def create_video_batch(payload: VideoBatchCreate, db: Session = Depends(get_db)) -> list[Video]:
    channel = db.get(Channel, payload.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    for item in payload.videos:
        if not item.title or not item.title.strip():
            raise HTTPException(status_code=400, detail="Title cannot be empty")

    videos: list[Video] = []
    for item in payload.videos:
        video = Video(
            channel_id=payload.channel_id,
            title=item.title.strip(),
            niche=item.niche,
            target_audience=item.target_audience,
            angle=item.angle,
            notes=item.notes,
            status=VideoStatus.idea,
            approved=False,
        )
        db.add(video)
        videos.append(video)

    db.commit()
    for video in videos:
        db.refresh(video)

    log_audit_event(
        db,
        "batch_created",
        f"Created {len(videos)} video ideas from batch import",
        metadata={"channel_id": payload.channel_id, "count": len(videos), "video_ids": [v.id for v in videos]},
    )
    return videos


@router.get("/{video_id}/audit", response_model=list[AuditEventRead])
def get_video_audit(
    video_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[AuditEvent]:
    get_video_or_404(db, video_id)
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.video_id == video_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


@router.get("/{video_id}/operator-export", response_model=OperatorExport)
def export_operator_summary(video_id: int, db: Session = Depends(get_db)) -> OperatorExport:
    video = get_video_or_404(db, video_id)
    preview_path = sync_preview_state(video)
    readiness = build_readiness(video, db)
    report = run_compliance_checks(video)
    visual_summary = build_visual_asset_review_summary(db, video)
    performance_metric = latest_performance_for_video(db, video.id)
    performance_payload = performance_payload_for_video(video.id, performance_metric)
    visual_manifest = visual_summary.get("manifest", {})
    manifest_assets = visual_manifest.get("assets", [])
    audit_events = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.video_id == video.id)
            .order_by(AuditEvent.created_at.desc())
            .limit(100)
        )
    )

    package_dir = package_dir_for(video)
    has_youtube_metadata_asset = latest_asset(video, AssetType.youtube_metadata) is not None
    youtube_record = db.scalar(
        select(PublishRecord)
        .where(PublishRecord.video_id == video.id, PublishRecord.platform == "youtube")
        .limit(1)
    )
    youtube_payload_ready = youtube_record is not None

    scene_number_by_scene_id: dict[int, int] = {}
    for row in visual_manifest.get("scenes", []):
        if not isinstance(row, dict):
            continue
        scene_id = row.get("scene_id")
        scene_number = row.get("scene_number")
        if isinstance(scene_id, int) and isinstance(scene_number, int):
            scene_number_by_scene_id[scene_id] = scene_number

    visual_assets: list[OperatorExportVisualAsset] = []
    for row in manifest_assets:
        if not isinstance(row, dict):
            continue
        file_path = row.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            continue
        scene_id = row.get("visual_scene_id") if isinstance(row.get("visual_scene_id"), int) else None
        visual_assets.append(
            OperatorExportVisualAsset(
                asset_id=int(row.get("asset_id")),
                video_id=video.id,
                plan_id=int(visual_manifest.get("plan_id")) if isinstance(visual_manifest.get("plan_id"), int) else None,
                scene_id=scene_id,
                scene_number=scene_number_by_scene_id.get(scene_id) if scene_id is not None else None,
                asset_type=str(row.get("asset_type") or "unknown"),
                file_path=file_path,
                review_status=str(row.get("review_status") or "pending"),
                review_notes=str(row.get("review_notes")) if row.get("review_notes") is not None else None,
                reviewed_at=row.get("reviewed_at"),
                created_at=row.get("created_at"),
            )
        )

    thumbnail_visual_asset = next((row for row in visual_assets if row.asset_type == "thumbnail"), None)
    thumbnail_image_path = thumbnail_visual_asset.file_path if thumbnail_visual_asset else None
    thumbnail_review_status = thumbnail_visual_asset.review_status if thumbnail_visual_asset else None

    blockers = list(readiness.blocking_reasons)
    if readiness.preview_has_unapproved_visual_assets:
        blockers.append("Registered visual assets include pending/rejected review states.")
    blockers = _dedupe_messages(blockers)
    warnings = _dedupe_messages(list(readiness.warnings))
    thumbnail_warning: str | None = None
    if not thumbnail_image_path:
        thumbnail_warning = "Thumbnail image is not generated yet."
        warnings.append(thumbnail_warning)
    elif thumbnail_review_status != "approved":
        thumbnail_warning = (
            f"Thumbnail visual asset review is '{thumbnail_review_status or 'pending'}'; manual approval is still required."
        )
        warnings.append(thumbnail_warning)
    warnings = _dedupe_messages(warnings)

    ready_for_manual_upload = len(blockers) == 0
    youtube_payload_readiness = {
        "approved": video.approved,
        "preview_exists": preview_path is not None,
        "preview_reviewed": bool(video.preview_reviewed),
        "package_exists": package_dir is not None,
        "youtube_metadata_asset_exists": has_youtube_metadata_asset,
        "publish_record_exists": youtube_payload_ready,
        "ready": (
            video.approved
            and preview_path is not None
            and bool(video.preview_reviewed)
            and package_dir is not None
            and has_youtube_metadata_asset
            and youtube_payload_ready
            and not readiness.preview_has_unapproved_visual_assets
        ),
    }

    export_dir = get_settings().output_path / "exports" / f"video_{video.id}"
    export_dir.mkdir(parents=True, exist_ok=True)
    export_file = export_dir / "operator_export.json"

    payload = OperatorExport(
        video=video,
        workflow_status=video.status.value,
        publishing_plan={
            "publish_date": video.publish_date,
            "publish_status": video.publish_status,
            "publish_notes": video.publish_notes,
        },
        readiness=readiness,
        compliance_summary={
            "overall_status": report.overall_status,
            "blockers": sum(1 for check in report.checks if check.status == "blocked"),
            "warnings": sum(1 for check in report.checks if check.status == "warning"),
        },
        assets=asset_previews(video),
        audit_events=audit_events,
        package_dir=package_dir,
        preview_path=str(preview_path) if preview_path is not None else None,
        visual_assets=visual_assets,
        content_references={
            "title": video.title,
            "thumbnail_text": video.thumbnail_text,
            "thumbnail_prompt": _latest_asset_reference(video, AssetType.thumbnail_prompt),
            "script": _latest_asset_reference(video, AssetType.script),
            "youtube_metadata": _latest_asset_reference(video, AssetType.youtube_metadata),
            "description": _latest_asset_reference(video, AssetType.description),
        },
        performance=performance_payload,
        thumbnail_image_path=thumbnail_image_path,
        thumbnail_review_status=thumbnail_review_status,
        thumbnail_warning=thumbnail_warning,
        ready_for_manual_upload=ready_for_manual_upload,
        blockers=blockers,
        warnings=warnings,
        export_path=str(export_file),
        youtube_payload_readiness=youtube_payload_readiness,
    )
    export_file.write_text(
        json.dumps(payload.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


@router.get("/{video_id}", response_model=VideoRead)
def read_video(video_id: int, db: Session = Depends(get_db)) -> Video:
    video = get_video_or_404(db, video_id)
    sync_preview_state(video)
    db.commit()
    db.refresh(video)
    return video


@router.patch("/{video_id}", response_model=VideoRead)
def update_video(video_id: int, payload: VideoUpdate, db: Session = Depends(get_db)) -> Video:
    video = get_video_or_404(db, video_id)
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(video, key, value)
    db.commit()
    db.refresh(video)
    log_audit_event(
        db,
        "video_updated",
        f"Updated video idea: {video.title}",
        video_id=video.id,
        metadata={"updated_fields": sorted(update_data.keys())},
    )
    return video


@router.delete("/{video_id}")
def delete_video(video_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    video = get_video_or_404(db, video_id)
    title = video.title
    asset_count = len(video.assets)
    db.delete(video)
    db.commit()
    log_audit_event(
        db,
        "video_deleted",
        f"Deleted video idea: {title}",
        metadata={"deleted_video_id": video_id, "title": title, "asset_count": asset_count},
    )
    return {"ok": True}


@router.patch("/{video_id}/publishing", response_model=VideoRead)
def update_video_publishing(video_id: int, payload: VideoPublishUpdate, db: Session = Depends(get_db)) -> Video:
    video = get_video_or_404(db, video_id)
    update_data = payload.model_dump(exclude_unset=True)

    if "publish_status" in update_data:
        new_status = update_data["publish_status"]
        if new_status in ("scheduled", "ready") and not video.approved:
            raise HTTPException(
                status_code=400,
                detail="Cannot set publish status to ready or scheduled for an unapproved video",
            )

    for key, value in update_data.items():
        setattr(video, key, value)

    db.commit()
    db.refresh(video)
    log_audit_event(
        db,
        "publishing_updated",
        f"Updated publishing plan for: {video.title}",
        video_id=video.id,
        metadata={"updated_fields": sorted(update_data.keys()), "publish_status": video.publish_status},
    )
    return video


@router.get("/{video_id}/readiness", response_model=VideoReadiness)
def get_video_readiness(video_id: int, db: Session = Depends(get_db)) -> VideoReadiness:
    video = get_video_or_404(db, video_id)
    readiness = build_readiness(video, db)
    db.commit()
    db.refresh(video)
    return readiness


@router.post("/{video_id}/performance", response_model=VideoPerformanceRead)
def save_video_performance(
    video_id: int,
    payload: VideoPerformanceUpdate,
    db: Session = Depends(get_db),
) -> VideoPerformanceRead:
    video = get_video_or_404(db, video_id)
    metric = latest_performance_for_video(db, video.id)
    now = datetime.utcnow()
    data = payload.model_dump()

    if metric is None:
        metric = VideoPerformanceMetric(
            video_id=video.id,
            platform=str(data.get("platform") or "youtube"),
            published_url=data.get("published_url"),
            impressions=int(data.get("impressions") or 0),
            views=int(data.get("views") or 0),
            clicks=int(data.get("clicks") or 0),
            ctr=data.get("ctr"),
            average_view_duration_seconds=data.get("average_view_duration_seconds"),
            average_percentage_viewed=data.get("average_percentage_viewed"),
            watch_time_minutes=data.get("watch_time_minutes"),
            likes=int(data.get("likes") or 0),
            comments=int(data.get("comments") or 0),
            subscribers_gained=int(data.get("subscribers_gained") or 0),
            published_at=data.get("published_at"),
            measured_at=now,
            notes=data.get("notes"),
        )
        db.add(metric)
    else:
        metric.platform = str(data.get("platform") or metric.platform or "youtube")
        metric.published_url = data.get("published_url")
        metric.impressions = int(data.get("impressions") or 0)
        metric.views = int(data.get("views") or 0)
        metric.clicks = int(data.get("clicks") or 0)
        metric.ctr = data.get("ctr")
        metric.average_view_duration_seconds = data.get("average_view_duration_seconds")
        metric.average_percentage_viewed = data.get("average_percentage_viewed")
        metric.watch_time_minutes = data.get("watch_time_minutes")
        metric.likes = int(data.get("likes") or 0)
        metric.comments = int(data.get("comments") or 0)
        metric.subscribers_gained = int(data.get("subscribers_gained") or 0)
        metric.published_at = data.get("published_at")
        metric.notes = data.get("notes")
        metric.measured_at = now

    if metric.ctr is None and metric.impressions > 0:
        metric.ctr = round((max(0, metric.clicks) / metric.impressions) * 100.0, 2)

    db.commit()
    db.refresh(metric)

    log_audit_event(
        db,
        "video_performance_saved",
        f"Saved manual/local performance metrics for: {video.title}",
        video_id=video.id,
        metadata={
            "performance_metric_id": metric.id,
            "platform": metric.platform,
            "views": metric.views,
            "impressions": metric.impressions,
            "clicks": metric.clicks,
            "ctr": metric.ctr,
            "is_manual_local": True,
        },
    )
    return VideoPerformanceRead.model_validate(performance_payload_for_video(video.id, metric))


@router.get("/{video_id}/performance", response_model=VideoPerformanceRead)
def get_video_performance(video_id: int, db: Session = Depends(get_db)) -> VideoPerformanceRead:
    get_video_or_404(db, video_id)
    metric = latest_performance_for_video(db, video_id)
    return VideoPerformanceRead.model_validate(performance_payload_for_video(video_id, metric))


@router.get("/{video_id}/preview/status", response_model=PreviewStatus)
def get_preview_status(video_id: int, db: Session = Depends(get_db)) -> PreviewStatus:
    video = get_video_or_404(db, video_id)
    preview_path = sync_preview_state(video)
    db.commit()
    db.refresh(video)
    return build_preview_status(video, preview_path, db)


@router.get("/{video_id}/preview")
def get_preview_file(video_id: int, db: Session = Depends(get_db)) -> FileResponse:
    video = get_video_or_404(db, video_id)
    preview_path = sync_preview_state(video)
    db.commit()
    db.refresh(video)
    if preview_path is None:
        raise HTTPException(status_code=404, detail="No rendered draft preview exists for this video.")
    return FileResponse(path=str(preview_path), media_type="video/mp4", filename=PREVIEW_FILENAME)


@router.post("/{video_id}/preview/review", response_model=PreviewStatus)
def mark_preview_reviewed(video_id: int, payload: PreviewReviewUpdate, db: Session = Depends(get_db)) -> PreviewStatus:
    video = get_video_or_404(db, video_id)
    preview_path = sync_preview_state(video)
    if preview_path is None:
        raise HTTPException(status_code=404, detail="Cannot review preview before a real draft preview file exists.")

    if payload.reviewed:
        video.preview_reviewed = True
        video.preview_reviewed_at = datetime.utcnow()
    else:
        video.preview_reviewed = False
        video.preview_reviewed_at = None

    db.commit()
    db.refresh(video)
    log_audit_event(
        db,
        "preview_reviewed" if payload.reviewed else "preview_unreviewed",
        f"{'Marked' if payload.reviewed else 'Cleared'} preview review state for: {video.title}",
        video_id=video.id,
        metadata={"preview_path": str(preview_path), "reviewed": payload.reviewed},
    )
    return build_preview_status(video, preview_path, db)


@router.post("/{video_id}/preview/render-draft", response_model=PreviewStatus)
def render_draft_preview(video_id: int, db: Session = Depends(get_db)) -> PreviewStatus:
    video = get_video_or_404(db, video_id)
    if not video.approved:
        raise HTTPException(status_code=409, detail="Video must be manually approved before rendering a draft preview.")

    ffmpeg_bin = shutil.which("ffmpeg")
    qlmanage_bin = shutil.which("qlmanage")
    expected_path = expected_preview_path(video.id).resolve()
    if not ffmpeg_bin:
        raise HTTPException(
            status_code=409,
            detail=(
                "No local preview renderer is configured. "
                f"Place a real draft MP4 at: {expected_path}"
            ),
        )
    if not qlmanage_bin:
        raise HTTPException(status_code=409, detail="Local preview renderer requires macOS qlmanage for slide rendering.")
    script_text = extract_preview_source_text(video)
    slides = build_slide_text_blocks(video, script_text)
    total_duration = estimate_preview_duration_seconds(script_text)
    per_slide_duration = max(3.0, total_duration / max(1, len(slides)))

    preview_dir = expected_path.parent
    preview_dir.mkdir(parents=True, exist_ok=True)
    voiceover_path = preview_voiceover_path(video.id).resolve()
    meta_path = preview_meta_path(video.id).resolve()
    staged_video_path = preview_dir / "draft_video_no_audio.mp4"
    final_render_path = preview_dir / "draft_rendered.mp4"
    concat_file_path = preview_dir / "slides_concat.txt"

    for artifact in [voiceover_path, meta_path, staged_video_path, final_render_path, concat_file_path]:
        try:
            artifact.unlink(missing_ok=True)
        except OSError:
            continue
    for stale_segment in preview_dir.glob("segment_*.mp4"):
        try:
            stale_segment.unlink(missing_ok=True)
        except OSError:
            continue
    for stale_text in preview_dir.glob("slide_*.*"):
        try:
            stale_text.unlink(missing_ok=True)
        except OSError:
            continue

    voiceover_result = generate_voiceover(script_text, preview_dir, video.id)
    audio_generated = voiceover_result.audio_generated
    silent_reason: str | None = voiceover_result.silent_reason
    if voiceover_result.path:
        voiceover_path = voiceover_result.path.resolve()

    segment_paths: list[Path] = []
    for index, slide_text in enumerate(slides, start=1):
        wrapped_text = wrap_slide_text(slide_text)
        slide_html_file = preview_dir / f"slide_{index}.html"
        slide_png_file = preview_dir / f"slide_{index}.html.png"
        slide_html_file.write_text(build_slide_html_document(video, wrapped_text, index, len(slides)), encoding="utf-8")
        quicklook_command = [
            qlmanage_bin,
            "-t",
            "-s",
            "1280",
            "-o",
            str(preview_dir),
            str(slide_html_file),
        ]
        quicklook_result = subprocess.run(quicklook_command, capture_output=True, text=True)
        if quicklook_result.returncode != 0 or not slide_png_file.exists() or slide_png_file.stat().st_size == 0:
            raise HTTPException(status_code=409, detail="Draft preview render failed while creating slide images.")

        segment_path = preview_dir / f"segment_{index}.mp4"
        segment_paths.append(segment_path)

        segment_command = [
            ffmpeg_bin,
            "-y",
            "-loop",
            "1",
            "-i",
            str(slide_png_file),
            "-t",
            f"{per_slide_duration:.2f}",
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
            "-r",
            PREVIEW_FPS,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(segment_path),
        ]
        segment_result = subprocess.run(segment_command, capture_output=True, text=True)
        if segment_result.returncode != 0 or not segment_path.exists() or segment_path.stat().st_size == 0:
            raise HTTPException(status_code=409, detail="Draft preview render failed while generating slide segments.")

    concat_lines = [f"file '{segment.resolve()}'" for segment in segment_paths]
    concat_file_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    concat_command = [
        ffmpeg_bin,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file_path),
        "-c",
        "copy",
        str(staged_video_path),
    ]
    concat_result = subprocess.run(concat_command, capture_output=True, text=True)
    if concat_result.returncode != 0 or not staged_video_path.exists() or staged_video_path.stat().st_size == 0:
        raise HTTPException(status_code=409, detail="Draft preview render failed while composing slideshow.")

    if audio_generated and voiceover_path.exists() and voiceover_path.stat().st_size > 0:
        mux_command = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(staged_video_path),
            "-i",
            str(voiceover_path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(final_render_path),
        ]
        mux_result = subprocess.run(mux_command, capture_output=True, text=True)
        if mux_result.returncode != 0 or not final_render_path.exists() or final_render_path.stat().st_size == 0:
            raise HTTPException(status_code=409, detail="Draft preview render failed while adding voiceover.")
    else:
        staged_video_path.replace(final_render_path)

    if not final_render_path.exists() or final_render_path.stat().st_size == 0:
        raise HTTPException(status_code=409, detail="Draft preview render failed to produce a video file.")

    final_render_path.replace(expected_path)

    duration_seconds = round(per_slide_duration * len(slides), 2)
    visual_manifest = build_preview_visual_manifest(db, video)
    try:
        visual_assets_used_count = int(visual_manifest.get("visual_assets_used_count", 0))
    except (TypeError, ValueError):
        visual_assets_used_count = 0
    try:
        visual_assets_missing_count = int(visual_manifest.get("visual_assets_missing_count", 0))
    except (TypeError, ValueError):
        visual_assets_missing_count = 0
    meta_payload = {
        "audio_generated": audio_generated,
        "silent_reason": silent_reason,
        "duration_seconds": duration_seconds,
        "slides_count": len(slides),
        "provider": voiceover_result.provider,
        "voice": voiceover_result.voice,
        "model": voiceover_result.model,
        "error_message": voiceover_result.error_message,
        "voiceover_path": str(voiceover_path) if audio_generated else None,
        "rendered_at": datetime.utcnow().isoformat(),
        "preview_asset_mode": str(visual_manifest.get("preview_asset_mode", "fallback_only")),
        "visual_assets_used_count": visual_assets_used_count,
        "visual_assets_missing_count": visual_assets_missing_count,
        "included_asset_paths": [
            str(path_value)
            for path_value in visual_manifest.get("included_asset_paths", [])
            if isinstance(path_value, str)
        ],
        "visual_asset_warnings": [
            str(warning)
            for warning in visual_manifest.get("visual_asset_warnings", visual_manifest.get("warnings", []))
            if isinstance(warning, str)
        ],
    }
    write_preview_meta(video.id, meta_payload)

    video.rendered_preview_path = str(expected_path)
    video.preview_rendered_at = datetime.utcnow()
    video.preview_reviewed = False
    video.preview_reviewed_at = None
    db.commit()
    db.refresh(video)
    log_audit_event(
        db,
        "preview_rendered",
        f"Rendered draft preview for: {video.title}",
        video_id=video.id,
        metadata={
            "preview_path": str(expected_path),
            "audio_generated": audio_generated,
            "silent_reason": silent_reason,
            "duration_seconds": duration_seconds,
            "slides_count": len(slides),
            "provider": voiceover_result.provider,
            "voice": voiceover_result.voice,
            "model": voiceover_result.model,
            "error_message": voiceover_result.error_message,
        },
    )
    return build_preview_status(video, expected_path, db)


@router.get("/{video_id}/assets", response_model=list[AssetRead])
def list_assets(video_id: int, db: Session = Depends(get_db)) -> list[ContentAsset]:
    get_video_or_404(db, video_id)
    stmt = select(ContentAsset).where(ContentAsset.video_id == video_id).order_by(ContentAsset.created_at.desc())
    return list(db.scalars(stmt))


@router.post("/{video_id}/generate", response_model=list[AssetRead])
def generate_assets(video_id: int, payload: GenerateRequest = GenerateRequest(), db: Session = Depends(get_db)) -> list[ContentAsset]:
    video = get_video_or_404(db, video_id)
    generated: list[tuple[str, str] | object] = []

    try:
        if payload.stage == "all":
            generated = build_all_assets(video)
        elif payload.stage == "brief":
            generated = [("brief", build_brief(video))]
        elif payload.stage == "script":
            generated = [("script", build_script(video))]
        elif payload.stage == "shorts":
            generated = [("shorts", build_shorts(video))]
        elif payload.stage == "description":
            generated = [("description", build_description(video))]
        elif payload.stage == "thumbnail_prompt":
            generated = [("thumbnail_prompt", build_thumbnail_prompt(video))]
        elif payload.stage == "metadata":
            generated = [("youtube_metadata", build_youtube_metadata(video))]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Asset generation failed: {exc}") from exc

    if not generated:
        raise HTTPException(status_code=502, detail="Asset generation returned no content.")

    assets: list[ContentAsset] = []
    for item in generated:
        if isinstance(item, tuple):
            asset_type, body = item
        else:
            asset_type, body = item.asset_type, item.body
        if not str(body or "").strip():
            raise HTTPException(status_code=502, detail=f"Generated {asset_type} content is empty.")
        asset = ContentAsset(video_id=video.id, asset_type=AssetType(asset_type), body=body)
        db.add(asset)
        assets.append(asset)

    video.status = VideoStatus.needs_review
    video.approved = False
    clear_preview_state(video)
    db.commit()
    for asset in assets:
        db.refresh(asset)

    log_audit_event(
        db,
        "assets_generated",
        f"Generated {len(assets)} asset(s) for: {video.title}",
        video_id=video.id,
        metadata={"stage": payload.stage, "asset_types": [asset.asset_type.value for asset in assets], "count": len(assets)},
    )
    return assets


@router.post("/{video_id}/thumbnail/generate", response_model=ThumbnailGenerationResponse)
def generate_thumbnail_image_asset(video_id: int, db: Session = Depends(get_db)) -> ThumbnailGenerationResponse:
    video = get_video_or_404(db, video_id)
    thumbnail_prompt_asset = latest_asset(video, AssetType.thumbnail_prompt)
    prompt_used = (thumbnail_prompt_asset.body if thumbnail_prompt_asset else build_thumbnail_prompt(video)).strip()
    result = generate_thumbnail_image(video, prompt_used)

    thumbnail_path = Path(result.thumbnail_path).resolve()
    if not thumbnail_path.is_file():
        raise HTTPException(status_code=502, detail="Thumbnail generation did not produce a local file.")

    plan = _ensure_visual_plan_for_thumbnail(db, video, result.prompt_used)
    plan.thumbnail_prompt = result.prompt_used
    if video.thumbnail_text:
        plan.thumbnail_text = video.thumbnail_text
    thumbnail_prompt_row = db.scalar(
        select(VisualAssetPrompt)
        .where(VisualAssetPrompt.plan_id == plan.id, VisualAssetPrompt.prompt_type == "thumbnail")
        .limit(1)
    )
    if thumbnail_prompt_row is None:
        db.add(
            VisualAssetPrompt(
                plan_id=plan.id,
                scene_id=None,
                prompt_type="thumbnail",
                label="Thumbnail prompt",
                prompt_text=result.prompt_used,
            )
        )
    else:
        thumbnail_prompt_row.prompt_text = result.prompt_used

    thumbnail_asset = db.scalar(
        select(VisualGeneratedAsset)
        .where(
            VisualGeneratedAsset.visual_asset_plan_id == plan.id,
            VisualGeneratedAsset.asset_type == "thumbnail",
        )
        .order_by(VisualGeneratedAsset.created_at.desc())
        .limit(1)
    )
    if thumbnail_asset is None:
        thumbnail_asset = VisualGeneratedAsset(
            visual_asset_plan_id=plan.id,
            visual_scene_id=None,
            generation_job_id=None,
            asset_type="thumbnail",
            file_path=str(thumbnail_path),
            file_exists=True,
            mime_type="image/png",
            notes="Generated by local thumbnail workflow.",
        )
        db.add(thumbnail_asset)
        db.flush()
    else:
        thumbnail_asset.file_path = str(thumbnail_path)
        thumbnail_asset.file_exists = True
        thumbnail_asset.mime_type = "image/png"
        thumbnail_asset.notes = "Generated by local thumbnail workflow."
        db.flush()

    update_visual_asset_review(db, thumbnail_asset, "pending", "Thumbnail requires manual operator review.")
    db.commit()
    db.refresh(video)
    db.refresh(thumbnail_asset)

    readiness = build_readiness(video, db)
    review_status = str(asset_review_fields(db, thumbnail_asset).get("review_status") or "pending")
    warnings = list(result.warnings)
    warnings.append("Thumbnail visual asset requires manual review before final use.")
    warnings = _dedupe_messages(warnings)

    log_audit_event(
        db,
        "thumbnail_image_generated",
        f"Generated local thumbnail image for: {video.title}",
        video_id=video.id,
        metadata={
            "visual_asset_id": thumbnail_asset.id,
            "thumbnail_path": str(thumbnail_path),
            "provider": result.provider,
            "fallback_used": result.fallback_used,
            "review_status": review_status,
            "plan_id": plan.id,
        },
    )
    return ThumbnailGenerationResponse(
        video_id=video.id,
        thumbnail_path=str(thumbnail_path),
        visual_asset_id=thumbnail_asset.id,
        provider=result.provider,
        fallback_used=result.fallback_used,
        generated=result.generated,
        review_status=review_status,
        warnings=warnings,
        next_required_action=readiness.next_required_action,
        prompt_used=result.prompt_used,
    )


@router.patch("/{video_id}/assets/{asset_type}", response_model=AssetRead)
def update_asset(video_id: int, asset_type: str, payload: AssetUpdate, db: Session = Depends(get_db)) -> ContentAsset:
    parsed_asset_type = parse_asset_type_or_400(asset_type)
    video = get_video_or_404(db, video_id)
    stmt = (
        select(ContentAsset)
        .where(ContentAsset.video_id == video.id, ContentAsset.asset_type == parsed_asset_type)
        .order_by(ContentAsset.created_at.desc())
        .limit(1)
    )
    asset = db.scalar(stmt)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset.body = payload.body
    asset.version += 1
    video.status = VideoStatus.needs_review
    video.approved = False
    clear_preview_state(video)

    db.commit()
    db.refresh(asset)
    log_audit_event(
        db,
        "asset_updated",
        f"Updated {parsed_asset_type.value} asset for: {video.title}",
        video_id=video.id,
        metadata={"asset_type": parsed_asset_type.value, "version": asset.version, "body_length": len(payload.body)},
    )
    return asset


@router.post("/{video_id}/assets/{asset_type}/regenerate", response_model=AssetRead)
def regenerate_asset(video_id: int, asset_type: str, db: Session = Depends(get_db)) -> ContentAsset:
    parsed_asset_type = parse_asset_type_or_400(asset_type)
    video = get_video_or_404(db, video_id)
    stmt = (
        select(ContentAsset)
        .where(ContentAsset.video_id == video.id, ContentAsset.asset_type == parsed_asset_type)
        .order_by(ContentAsset.created_at.desc())
        .limit(1)
    )
    asset = db.scalar(stmt)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    if parsed_asset_type == AssetType.brief:
        new_body = build_brief(video)
    elif parsed_asset_type == AssetType.script:
        new_body = build_script(video)
    elif parsed_asset_type == AssetType.shorts:
        new_body = build_shorts(video)
    elif parsed_asset_type == AssetType.description:
        new_body = build_description(video)
    elif parsed_asset_type == AssetType.thumbnail_prompt:
        new_body = build_thumbnail_prompt(video)
    elif parsed_asset_type == AssetType.youtube_metadata:
        new_body = build_youtube_metadata(video)
    elif parsed_asset_type == AssetType.package_manifest:
        from app.services.compliance import build_review_checklist

        script_asset = latest_asset(video, AssetType.script)
        script_body = script_asset.body if script_asset else build_script(video)
        new_body = build_review_checklist(script_body)
    else:
        raise HTTPException(status_code=400, detail="Cannot regenerate this asset type")

    asset.body = new_body
    asset.version += 1
    video.status = VideoStatus.needs_review
    video.approved = False
    clear_preview_state(video)

    db.commit()
    db.refresh(asset)
    log_audit_event(
        db,
        "asset_regenerated",
        f"Regenerated {parsed_asset_type.value} asset for: {video.title}",
        video_id=video.id,
        metadata={"asset_type": parsed_asset_type.value, "version": asset.version, "body_length": len(new_body)},
    )
    return asset


@router.post("/{video_id}/review", response_model=ReviewRead)
def review_video(video_id: int, payload: ReviewCreate, db: Session = Depends(get_db)) -> Review:
    video = get_video_or_404(db, video_id)

    if payload.passed:
        report = run_compliance_checks(video)
        if report.overall_status == "blocked":
            raise HTTPException(status_code=400, detail="Cannot approve video with blocked compliance checks.")

    review = Review(video_id=video.id, **payload.model_dump())
    db.add(review)
    video.approved = payload.passed
    video.status = VideoStatus.approved if payload.passed else VideoStatus.rejected
    db.commit()
    db.refresh(review)

    log_audit_event(
        db,
        "review_approved" if payload.passed else "review_rejected",
        f"{'Approved' if payload.passed else 'Rejected'} video after manual review: {video.title}",
        video_id=video.id,
        metadata={"passed": payload.passed, "review_id": review.id, "notes_length": len(payload.notes or "")},
    )
    return review


@router.post("/{video_id}/package", response_model=PackageResponse)
def package_video(video_id: int, db: Session = Depends(get_db)) -> PackageResponse:
    video = get_video_or_404(db, video_id)
    preview_path = sync_preview_state(video)
    if preview_path is None:
        raise HTTPException(
            status_code=409,
            detail=f"Draft preview is missing. Expected file: {expected_preview_path(video.id).resolve()}",
        )
    if not video.preview_reviewed:
        raise HTTPException(status_code=409, detail="Draft preview must be manually reviewed before packaging.")
    try:
        package_dir, manifest_asset = build_video_package(db, video)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    log_audit_event(
        db,
        "package_created",
        f"Created local package for: {video.title}",
        video_id=video.id,
        metadata={"package_dir": str(package_dir), "manifest_asset_id": manifest_asset.id},
    )
    return PackageResponse(video_id=video.id, package_dir=str(package_dir), manifest_asset_id=manifest_asset.id)


@router.post("/{video_id}/compliance/run", response_model=ComplianceReport)
def run_compliance(video_id: int, db: Session = Depends(get_db)) -> ComplianceReport:
    video = get_video_or_404(db, video_id)
    report = run_compliance_checks(video)
    log_audit_event(
        db,
        "compliance_run",
        f"Ran compliance check for: {video.title}",
        video_id=video.id,
        metadata={
            "overall_status": report.overall_status,
            "blockers": sum(1 for check in report.checks if check.status == "blocked"),
            "warnings": sum(1 for check in report.checks if check.status == "warning"),
        },
    )
    return report
