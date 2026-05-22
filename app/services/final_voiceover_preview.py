from __future__ import annotations

import os
from typing import Literal

from app.models import Video
from app.schemas import FinalVoiceoverDryRunResponse
from app.services.final_voiceover import (
    _DEFAULT_ELEVEN_MODEL,
    _DEFAULT_OPENAI_MODEL,
    _DEFAULT_OPENAI_VOICE,
    get_source_text_for_final_voiceover,
)
from app.services.voiceover_readiness import assess_production_voiceover_readiness

_SAFETY_NOTE = "Dry run only. No external API call was made."


def _estimate_duration_seconds(word_count: int) -> int:
    if word_count <= 0:
        return 0
    # Rough narration pace: ~2.6 words/second.
    return max(1, round(word_count / 2.6))


def _excerpt(text: str, max_len: int = 280) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


def _request_preview_openai(model: str, voice: str, excerpt: str, char_count: int) -> dict[str, object]:
    return {
        "method": "POST",
        "url": "https://api.openai.com/v1/audio/speech",
        "headers": {
            "Authorization": "Bearer ***REDACTED***",
            "Content-Type": "application/json",
        },
        "payload": {
            "model": model,
            "voice": voice,
            "format": "mp3",
            "instructions": (
                "Speak like a confident, clear YouTube narrator for business owners. "
                "Natural pacing, warm tone, not robotic, not overly excited."
            ),
            "input_excerpt": excerpt,
            "input_character_count": char_count,
        },
    }


def _request_preview_elevenlabs(model: str, voice_id: str | None, excerpt: str, char_count: int) -> dict[str, object]:
    return {
        "method": "POST",
        "url": f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id or '<missing-voice-id>'}",
        "headers": {
            "xi-api-key": "***REDACTED***",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        "payload": {
            "model_id": model,
            "text_excerpt": excerpt,
            "text_character_count": char_count,
            "voice_settings": {
                "stability": 0.4,
                "similarity_boost": 0.75,
                "style": 0.2,
                "use_speaker_boost": True,
            },
        },
    }


def build_final_voiceover_dry_run(
    video: Video,
    *,
    provider: Literal["openai", "elevenlabs"],
    voice: str | None,
    max_chars: int,
) -> FinalVoiceoverDryRunResponse:
    readiness = assess_production_voiceover_readiness(video.id)
    provider_state = next((item for item in readiness.providers if item.provider == provider), None)
    if provider_state is None:
        # Should not happen when provider is validated upstream.
        return FinalVoiceoverDryRunResponse(
            video_id=video.id,
            provider=provider,
            configured=False,
            model=None,
            voice=voice,
            input_character_count=0,
            input_word_count=0,
            input_excerpt="",
            max_input_chars_used=max_chars,
            estimated_duration_seconds=0,
            request_preview={},
            blockers=[f"Unsupported provider: {provider}"],
            warnings=[],
            next_required_action="Select a supported provider.",
            safety_note=_SAFETY_NOTE,
        )

    raw_text = get_source_text_for_final_voiceover(video) or ""
    source_text = raw_text.strip()
    excerpt = _excerpt(source_text)
    input_text = source_text[:max_chars] if source_text else ""
    input_character_count = len(input_text)
    input_word_count = len(input_text.split()) if input_text else 0
    estimated_duration_seconds = _estimate_duration_seconds(input_word_count)

    blockers = list(provider_state.blockers)
    warnings = list(provider_state.warnings)

    if not source_text:
        blockers.append("No script text available for voiceover generation")

    if provider == "openai":
        model = (os.getenv("OPENAI_TTS_MODEL") or "").strip() or _DEFAULT_OPENAI_MODEL
        resolved_voice = (voice or os.getenv("OPENAI_TTS_VOICE") or "").strip() or _DEFAULT_OPENAI_VOICE
        request_preview = _request_preview_openai(model, resolved_voice, excerpt, input_character_count)
    else:
        model = (os.getenv("ELEVENLABS_MODEL_ID") or "").strip() or _DEFAULT_ELEVEN_MODEL
        resolved_voice = (voice or os.getenv("ELEVENLABS_VOICE_ID") or "").strip() or None
        request_preview = _request_preview_elevenlabs(model, resolved_voice, excerpt, input_character_count)

    if blockers:
        next_required_action = blockers[0]
    else:
        next_required_action = "Configuration and source text look ready. Run real production generation when you are ready to spend API credits."

    return FinalVoiceoverDryRunResponse(
        video_id=video.id,
        provider=provider,
        configured=provider_state.configured and bool(source_text),
        model=model,
        voice=resolved_voice,
        input_character_count=input_character_count,
        input_word_count=input_word_count,
        input_excerpt=excerpt,
        max_input_chars_used=max_chars,
        estimated_duration_seconds=estimated_duration_seconds,
        request_preview=request_preview,
        blockers=blockers,
        warnings=warnings,
        next_required_action=next_required_action,
        safety_note=_SAFETY_NOTE,
    )
