from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from app.config import Settings
from app.editorial.contracts import canonical_sha256

from .budget import CampaignBudgetPolicy


I5_PRODUCTION_POLICY_VERSION = "i5-production-v1"
I5_BUDGET_GATE_POLICY_VERSION = "i5-budget-v1"
I5_PROVIDER_CATALOG_VERSION = "i5-provider-catalog-2026-08-11-v1"
I5_PRODUCTION_PROFILE_KIND = "i5_production_profile"
I5_RENDERER_CONTRACT_VERSION = "i5-ffmpeg-renderer-v1"
I5_LOCAL_VISUAL_RENDERER_VERSION = "i5-local-visuals-v1"

I5_MAX_GENERATED_CINEMATIC_SCENES = 3
I5_MAX_GENERATED_VIDEO_SCENES = 3
I5_MAX_GENERATED_VIDEO_SECONDS = 24
I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS = 45
I5_RENDER_WIDTH = 1280
I5_RENDER_HEIGHT = 720
I5_RENDER_FRAME_RATE = 30

_CONFIG_TOKEN = re.compile(r"[A-Za-z0-9._:-]+")


class I5ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProductionProfile:
    payload: dict[str, object]
    sha256: str


def _token(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized or _CONFIG_TOKEN.fullmatch(normalized) is None:
        raise I5ConfigurationError(f"{name} is invalid")
    return normalized


def build_production_profile(
    *,
    campaign_id: int,
    i4_script_sha256: str,
    settings: Settings,
    budget_policy: CampaignBudgetPolicy,
    ffmpeg_version: str,
    ffprobe_version: str,
) -> ProductionProfile:
    if campaign_id <= 0:
        raise I5ConfigurationError("campaign_id must be positive")
    if re.fullmatch(r"[0-9a-f]{64}", i4_script_sha256) is None:
        raise I5ConfigurationError("accepted I4 script hash is invalid")
    if not ffmpeg_version.strip() or not ffprobe_version.strip():
        raise I5ConfigurationError("ffmpeg and ffprobe identities are required")

    tts_provider = _token("I5_TTS_PROVIDER", settings.i5_tts_provider).lower()
    tts_model = _token("I5_TTS_MODEL", settings.i5_tts_model)
    tts_fallback = _token("I5_TTS_FALLBACK_MODEL", settings.i5_tts_fallback_model)
    tts_voice = _token("I5_TTS_VOICE", settings.i5_tts_voice)
    image_provider = _token(
        "I5_GENERATED_IMAGE_PROVIDER",
        settings.i5_generated_image_provider,
    ).lower()
    image_model = _token(
        "I5_GENERATED_IMAGE_MODEL",
        settings.i5_generated_image_model,
    )
    image_size = settings.i5_generated_image_size.strip()
    image_primary_quality = _token(
        "I5_GENERATED_IMAGE_PRIMARY_QUALITY",
        settings.i5_generated_image_primary_quality,
    ).lower()
    image_fallback_quality = _token(
        "I5_GENERATED_IMAGE_FALLBACK_QUALITY",
        settings.i5_generated_image_fallback_quality,
    ).lower()
    video_provider = _token("I5_VIDEO_PROVIDER", settings.i5_video_provider).lower()
    video_model = _token("I5_VIDEO_MODEL", settings.i5_video_model)

    if tts_provider != "openai" or (tts_model, tts_fallback, tts_voice) != (
        "tts-1-hd",
        "tts-1",
        "onyx",
    ):
        raise I5ConfigurationError("I5 TTS configuration conflicts with the locked catalog")
    if image_provider != "openai" or (
        image_model,
        image_size,
        image_primary_quality,
        image_fallback_quality,
    ) != ("gpt-image-2", "1280x720", "medium", "low"):
        raise I5ConfigurationError(
            "I5 generated-image configuration conflicts with the locked catalog"
        )
    if video_provider not in {"disabled", "openai", "sora"}:
        raise I5ConfigurationError("I5 video provider is invalid")
    if video_model != "sora-2":
        raise I5ConfigurationError("I5 video model conflicts with the locked catalog")
    if video_provider != "disabled" and not settings.i5_allow_deprecated_sora:
        raise I5ConfigurationError(
            "Sora requires explicit I5_ALLOW_DEPRECATED_SORA opt-in"
        )

    payload: dict[str, object] = {
        "budget_policy_sha256": budget_policy.policy_sha256,
        "budget_policy_version": budget_policy.policy_version,
        "campaign_id": campaign_id,
        "contract_version": "i5-production-profile-v1",
        "generated_image": {
            "fallback_quality": image_fallback_quality,
            "model": image_model,
            "primary_quality": image_primary_quality,
            "provider": image_provider,
            "size": image_size,
        },
        "generated_media_bounds": {
            "max_cinematic_coverage_seconds": I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS,
            "max_cinematic_scenes": I5_MAX_GENERATED_CINEMATIC_SCENES,
            "max_video_scenes": I5_MAX_GENERATED_VIDEO_SCENES,
            "max_video_total_seconds": I5_MAX_GENERATED_VIDEO_SECONDS,
        },
        "i4_script_sha256": i4_script_sha256,
        "local_visual_renderer_version": I5_LOCAL_VISUAL_RENDERER_VERSION,
        "production_policy_version": I5_PRODUCTION_POLICY_VERSION,
        "provider_catalog_version": I5_PROVIDER_CATALOG_VERSION,
        "renderer": {
            "contract_version": I5_RENDERER_CONTRACT_VERSION,
            "ffmpeg_version": ffmpeg_version.strip(),
            "ffprobe_version": ffprobe_version.strip(),
            "frame_rate": I5_RENDER_FRAME_RATE,
            "height": I5_RENDER_HEIGHT,
            "width": I5_RENDER_WIDTH,
        },
        "storage": {
            "root_strategy": "output_dir/canonical_i5/campaign_<id>/objects",
            "version": "i5-content-addressed-local-v1",
        },
        "tts": {
            "fallback_model": tts_fallback,
            "primary_model": tts_model,
            "provider": tts_provider,
            "response_format": "wav",
            "voice": tts_voice,
        },
        "video": {
            "allow_deprecated_sora": settings.i5_allow_deprecated_sora,
            "clip_duration_seconds": 8,
            "model": video_model,
            "provider": video_provider,
            "size": "1280x720",
        },
    }
    return ProductionProfile(payload=payload, sha256=canonical_sha256(payload))


def production_workflow_id(
    campaign_id: int,
    i4_script_sha256: str,
    production_profile_sha256: str,
) -> str:
    for label, value in (
        ("i4_script_sha256", i4_script_sha256),
        ("production_profile_sha256", production_profile_sha256),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"{label} must be a lowercase SHA-256 value")
    if campaign_id <= 0:
        raise ValueError("campaign_id must be positive")
    workflow_id = (
        f"campaign:{campaign_id}:i5:{i4_script_sha256}:{production_profile_sha256}"
    )
    if len(workflow_id) > 240:
        raise ValueError("I5 production workflow identity exceeds schema capacity")
    return workflow_id


def verify_profile_payload(payload: Mapping[str, object]) -> str:
    if payload.get("contract_version") != "i5-production-profile-v1":
        raise I5ConfigurationError("production profile contract version is invalid")
    return canonical_sha256(dict(payload))
