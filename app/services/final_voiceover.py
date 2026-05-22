from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AssetType, ContentAsset, Video
from app.schemas import FinalVoiceoverGenerateResponse, FinalVoiceoverStatus

_BLOCKED_PROVIDERS = {"macos", "silent", "fallback", "local", "placeholder", "none", ""}
_PRODUCTION_PROVIDERS = {"openai", "elevenlabs"}

_DEFAULT_OPENAI_MODEL = "gpt-4o-mini-tts"
_DEFAULT_OPENAI_VOICE = "onyx"
_DEFAULT_ELEVEN_MODEL = "eleven_multilingual_v2"


def final_voiceover_dir(video_id: int) -> Path:
    return get_settings().output_path / "final_voiceovers" / f"video_{video_id}"


def final_voiceover_path_for_video(video_id: int) -> Path:
    return final_voiceover_dir(video_id) / "voiceover.mp3"


def final_voiceover_meta_path_for_video(video_id: int) -> Path:
    return final_voiceover_dir(video_id) / "voiceover_meta.json"


def _read_meta(video_id: int) -> dict[str, object]:
    meta_path = final_voiceover_meta_path_for_video(video_id)
    if not meta_path.is_file():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for v in values:
        msg = v.strip()
        if msg and msg not in seen:
            seen.add(msg)
            result.append(msg)
    return result


def assess_final_voiceover(video_id: int) -> FinalVoiceoverStatus:
    mp3_path = final_voiceover_path_for_video(video_id)
    meta_path = final_voiceover_meta_path_for_video(video_id)

    try:
        voiceover_exists = mp3_path.is_file() and mp3_path.stat().st_size > 0
    except OSError:
        voiceover_exists = False

    meta_exists = meta_path.is_file()
    meta = _read_meta(video_id)

    provider = str(meta.get("provider", "")).lower().strip() if meta else ""
    voice = str(meta.get("voice", "")).strip() or None
    model = str(meta.get("model", "")).strip() or None

    blockers: list[str] = []
    if not voiceover_exists:
        blockers.append("Final voiceover file must be generated before final export")
    if not meta_exists:
        blockers.append("Final voiceover file must be generated before final export")
    elif provider in _BLOCKED_PROVIDERS or provider not in _PRODUCTION_PROVIDERS:
        blockers.append("Final voiceover must use a production voice provider")

    blockers = _dedupe(blockers)
    voiceover_ready = len(blockers) == 0

    return FinalVoiceoverStatus(
        video_id=video_id,
        voiceover_ready=voiceover_ready,
        voiceover_exists=voiceover_exists,
        meta_exists=meta_exists,
        provider=provider if provider else None,
        voice=voice,
        model=model,
        voiceover_path=str(mp3_path.resolve()) if voiceover_exists else None,
        blockers=blockers,
        warnings=[],
    )


def get_source_text_for_final_voiceover(video: Video) -> str | None:
    def _body(asset_type: AssetType) -> str | None:
        assets = [a for a in video.assets if a.asset_type == asset_type]
        if not assets:
            return None
        latest: ContentAsset = sorted(assets, key=lambda a: a.created_at, reverse=True)[0]
        body = latest.body.strip()
        return body if body else None

    return _body(AssetType.script) or _body(AssetType.description) or _body(AssetType.brief)


def generate_final_voiceover(
    video: Video,
    db: Session,
    provider: str,
    voice: str | None,
    force: bool,
) -> FinalVoiceoverGenerateResponse:
    video_id = video.id
    provider = provider.lower().strip()

    if provider in _BLOCKED_PROVIDERS or provider not in _PRODUCTION_PROVIDERS:
        return FinalVoiceoverGenerateResponse(
            video_id=video_id,
            status="blocked",
            voiceover_path=None,
            provider=provider or None,
            voice=voice,
            blockers=["Final voiceover must use a production voice provider"],
        )

    if not force:
        mp3_path = final_voiceover_path_for_video(video_id)
        try:
            already_done = mp3_path.is_file() and mp3_path.stat().st_size > 0
        except OSError:
            already_done = False
        if already_done:
            meta = _read_meta(video_id)
            existing_provider = str(meta.get("provider", "")).lower().strip()
            if existing_provider in _PRODUCTION_PROVIDERS:
                return FinalVoiceoverGenerateResponse(
                    video_id=video_id,
                    status="skipped",
                    voiceover_path=str(mp3_path.resolve()),
                    provider=existing_provider,
                    voice=str(meta.get("voice", "")) or None,
                    blockers=[],
                )

    script_text = get_source_text_for_final_voiceover(video)
    if not script_text:
        return FinalVoiceoverGenerateResponse(
            video_id=video_id,
            status="blocked",
            voiceover_path=None,
            provider=provider,
            voice=voice,
            blockers=["No script text available for voiceover generation"],
        )

    output_dir = final_voiceover_dir(video_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = final_voiceover_path_for_video(video_id)

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return FinalVoiceoverGenerateResponse(
                video_id=video_id,
                status="blocked",
                voiceover_path=None,
                provider=provider,
                voice=voice,
                blockers=["OPENAI_API_KEY is not set"],
            )
        model = os.getenv("OPENAI_TTS_MODEL", _DEFAULT_OPENAI_MODEL).strip() or _DEFAULT_OPENAI_MODEL
        effective_voice = (
            voice
            or os.getenv("OPENAI_TTS_VOICE", _DEFAULT_OPENAI_VOICE).strip()
            or _DEFAULT_OPENAI_VOICE
        )
        payload = {
            "model": model,
            "voice": effective_voice,
            "input": script_text[:5000],
            "format": "mp3",
            "instructions": (
                "Speak like a confident, clear YouTube narrator for business owners. "
                "Natural pacing, warm tone, not robotic, not overly excited."
            ),
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
            with urllib.request.urlopen(request, timeout=60) as response:
                audio_bytes = response.read()
            output_path.write_bytes(audio_bytes)
        except urllib.error.HTTPError as exc:
            return FinalVoiceoverGenerateResponse(
                video_id=video_id,
                status="blocked",
                voiceover_path=None,
                provider=provider,
                voice=effective_voice,
                blockers=[f"OpenAI TTS HTTP error: {exc.code}"],
            )
        except Exception as exc:  # noqa: BLE001
            return FinalVoiceoverGenerateResponse(
                video_id=video_id,
                status="blocked",
                voiceover_path=None,
                provider=provider,
                voice=effective_voice,
                blockers=[f"OpenAI TTS request failed: {exc}"],
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            return FinalVoiceoverGenerateResponse(
                video_id=video_id,
                status="blocked",
                voiceover_path=None,
                provider=provider,
                voice=effective_voice,
                blockers=["OpenAI returned empty audio"],
            )
        meta_obj = {
            "provider": "openai",
            "voice": effective_voice,
            "model": model,
            "status": "complete",
        }
        final_voiceover_meta_path_for_video(video_id).write_text(
            json.dumps(meta_obj), encoding="utf-8"
        )
        return FinalVoiceoverGenerateResponse(
            video_id=video_id,
            status="generated",
            voiceover_path=str(output_path.resolve()),
            provider="openai",
            voice=effective_voice,
            blockers=[],
        )

    # provider == "elevenlabs"
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        return FinalVoiceoverGenerateResponse(
            video_id=video_id,
            status="blocked",
            voiceover_path=None,
            provider=provider,
            voice=voice,
            blockers=["ELEVENLABS_API_KEY is not set"],
        )
    voice_id = voice or os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if not voice_id:
        return FinalVoiceoverGenerateResponse(
            video_id=video_id,
            status="blocked",
            voiceover_path=None,
            provider=provider,
            voice=None,
            blockers=["ELEVENLABS_VOICE_ID is required for ElevenLabs"],
        )
    model_id = os.getenv("ELEVENLABS_MODEL_ID", _DEFAULT_ELEVEN_MODEL).strip() or _DEFAULT_ELEVEN_MODEL
    payload_el = {
        "text": script_text[:5000],
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
            "style": 0.2,
            "use_speaker_boost": True,
        },
    }
    request_el = urllib.request.Request(
        url=f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps(payload_el).encode("utf-8"),
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_el, timeout=60) as response:
            audio_bytes = response.read()
        output_path.write_bytes(audio_bytes)
    except urllib.error.HTTPError as exc:
        return FinalVoiceoverGenerateResponse(
            video_id=video_id,
            status="blocked",
            voiceover_path=None,
            provider=provider,
            voice=voice_id,
            blockers=[f"ElevenLabs TTS HTTP error: {exc.code}"],
        )
    except Exception as exc:  # noqa: BLE001
        return FinalVoiceoverGenerateResponse(
            video_id=video_id,
            status="blocked",
            voiceover_path=None,
            provider=provider,
            voice=voice_id,
            blockers=[f"ElevenLabs TTS request failed: {exc}"],
        )
    if not output_path.exists() or output_path.stat().st_size == 0:
        return FinalVoiceoverGenerateResponse(
            video_id=video_id,
            status="blocked",
            voiceover_path=None,
            provider=provider,
            voice=voice_id,
            blockers=["ElevenLabs returned empty audio"],
        )
    meta_obj_el = {
        "provider": "elevenlabs",
        "voice": voice_id,
        "model": model_id,
        "status": "complete",
    }
    final_voiceover_meta_path_for_video(video_id).write_text(
        json.dumps(meta_obj_el), encoding="utf-8"
    )
    return FinalVoiceoverGenerateResponse(
        video_id=video_id,
        status="generated",
        voiceover_path=str(output_path.resolve()),
        provider="elevenlabs",
        voice=voice_id,
        blockers=[],
    )
