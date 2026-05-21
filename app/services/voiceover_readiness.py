from __future__ import annotations

import os

from app.schemas import ProductionVoiceoverReadiness, VoiceoverProviderReadiness
from app.services.final_voiceover import (
    _DEFAULT_ELEVEN_MODEL,
    _DEFAULT_OPENAI_MODEL,
    _DEFAULT_OPENAI_VOICE,
    assess_final_voiceover,
)

_PRODUCTION_PROVIDERS = {"openai", "elevenlabs"}

_GATE_EXPLANATION = (
    "Final export requires a production final voiceover from OpenAI or ElevenLabs. "
    "Readiness checks are local-only and never call external APIs."
)
_SAFETY_NOTE = (
    "Local/Mac/silent/fallback/placeholder preview audio providers are not accepted for final export."
)


def _configured(value: str | None) -> bool:
    return bool((value or "").strip())


def _provider_openai_readiness() -> VoiceoverProviderReadiness:
    api_key_configured = _configured(os.getenv("OPENAI_API_KEY"))
    env_model = (os.getenv("OPENAI_TTS_MODEL") or "").strip()
    env_voice = (os.getenv("OPENAI_TTS_VOICE") or "").strip()
    model_configured = bool(env_model or _DEFAULT_OPENAI_MODEL)
    voice_configured = bool(env_voice or _DEFAULT_OPENAI_VOICE)
    provider_allowed = "openai" in _PRODUCTION_PROVIDERS

    blockers: list[str] = []
    if not provider_allowed:
        blockers.append("OpenAI is not an allowed production final voiceover provider")
    if not api_key_configured:
        blockers.append("OPENAI_API_KEY is not configured")
    if not model_configured:
        blockers.append("OPENAI_TTS_MODEL is not configured and no default model is available")
    if not voice_configured:
        blockers.append("OPENAI_TTS_VOICE is not configured and no default voice is available")

    configured = provider_allowed and api_key_configured and model_configured and voice_configured
    if configured:
        next_required_action = "Provider is ready. You can generate a production final voiceover when desired."
    elif not api_key_configured:
        next_required_action = "Set OPENAI_API_KEY to enable OpenAI production final voiceover generation."
    else:
        next_required_action = "Complete remaining OpenAI final voiceover configuration blockers."

    return VoiceoverProviderReadiness(
        provider="openai",
        configured=configured,
        provider_allowed=provider_allowed,
        api_key_configured=api_key_configured,
        voice_configured=voice_configured,
        model_configured=model_configured,
        default_voice=_DEFAULT_OPENAI_VOICE,
        default_model=_DEFAULT_OPENAI_MODEL,
        blockers=blockers,
        warnings=[],
        next_required_action=next_required_action,
    )


def _provider_elevenlabs_readiness() -> VoiceoverProviderReadiness:
    api_key_configured = _configured(os.getenv("ELEVENLABS_API_KEY"))
    env_voice = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    env_model = (os.getenv("ELEVENLABS_MODEL_ID") or "").strip()
    voice_configured = bool(env_voice)
    model_configured = bool(env_model or _DEFAULT_ELEVEN_MODEL)
    provider_allowed = "elevenlabs" in _PRODUCTION_PROVIDERS

    blockers: list[str] = []
    if not provider_allowed:
        blockers.append("ElevenLabs is not an allowed production final voiceover provider")
    if not api_key_configured:
        blockers.append("ELEVENLABS_API_KEY is not configured")
    if not voice_configured:
        blockers.append("ELEVENLABS_VOICE_ID is not configured")
    if not model_configured:
        blockers.append("ELEVENLABS_MODEL_ID is not configured and no default model is available")

    configured = provider_allowed and api_key_configured and voice_configured and model_configured
    if configured:
        next_required_action = "Provider is ready. You can generate a production final voiceover when desired."
    elif not api_key_configured:
        next_required_action = "Set ELEVENLABS_API_KEY to enable ElevenLabs production final voiceover generation."
    elif not voice_configured:
        next_required_action = "Set ELEVENLABS_VOICE_ID to enable ElevenLabs production final voiceover generation."
    else:
        next_required_action = "Complete remaining ElevenLabs final voiceover configuration blockers."

    return VoiceoverProviderReadiness(
        provider="elevenlabs",
        configured=configured,
        provider_allowed=provider_allowed,
        api_key_configured=api_key_configured,
        voice_configured=voice_configured,
        model_configured=model_configured,
        default_voice=None,
        default_model=_DEFAULT_ELEVEN_MODEL,
        blockers=blockers,
        warnings=[],
        next_required_action=next_required_action,
    )


def assess_production_voiceover_readiness(video_id: int) -> ProductionVoiceoverReadiness:
    providers = [
        _provider_openai_readiness(),
        _provider_elevenlabs_readiness(),
    ]
    available_providers = [row.provider for row in providers if row.configured]
    blocked_providers = [row.provider for row in providers if not row.configured]
    status = assess_final_voiceover(video_id)

    if status.voiceover_ready:
        recommended_next_action = "Production final voiceover is already present. Continue to final export checks."
    elif available_providers:
        recommended_next_action = (
            "At least one production provider is configured. Generate final voiceover when you are ready to spend API credits."
        )
    else:
        recommended_next_action = "Configure OpenAI or ElevenLabs production voiceover environment variables before generation."

    return ProductionVoiceoverReadiness(
        video_id=video_id,
        available_providers=available_providers,
        blocked_providers=blocked_providers,
        providers=providers,
        current_final_voiceover_status=status,
        recommended_next_action=recommended_next_action,
        gate_explanation=_GATE_EXPLANATION,
        safety_note=_SAFETY_NOTE,
    )
