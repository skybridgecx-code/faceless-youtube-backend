from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.models import Video

_OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
_DEFAULT_IMAGE_MODEL = "gpt-image-1"
_PLACEHOLDER_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z2ioAAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class ThumbnailGenerationResult:
    video_id: int
    thumbnail_path: str
    provider: str
    generated: bool
    fallback_used: bool
    warnings: list[str]
    prompt_used: str


def thumbnail_output_path(video_id: int) -> Path:
    output_path = (get_settings().output_path / "thumbnails" / f"video_{int(video_id)}" / "thumbnail.png").resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def _write_placeholder_thumbnail(path: Path) -> None:
    path.write_bytes(_PLACEHOLDER_PNG_BYTES)


def _extract_image_b64(payload: dict[str, object]) -> str:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError("Image response did not include data entries.")
    first = data[0]
    if not isinstance(first, dict):
        raise RuntimeError("Image response entry was not an object.")
    b64_value = first.get("b64_json")
    if not isinstance(b64_value, str) or not b64_value.strip():
        raise RuntimeError("Image response entry did not include b64_json.")
    return b64_value


def _generate_with_openai_images(*, prompt: str, model: str, api_key: str, output_path: Path) -> None:
    payload = {
        "model": model,
        "prompt": prompt,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    request = urllib.request.Request(
        _OPENAI_IMAGES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:  # noqa: S310 - fixed OpenAI endpoint
            raw = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Image generation request failed: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Image generation response was not valid JSON.") from exc

    image_bytes = base64.b64decode(_extract_image_b64(parsed), validate=True)
    if not image_bytes:
        raise RuntimeError("Image generation response returned empty image bytes.")
    output_path.write_bytes(image_bytes)


def generate_thumbnail_image(video: Video, thumbnail_prompt: str | None = None) -> ThumbnailGenerationResult:
    prompt = (thumbnail_prompt or "").strip() or f"Create a clean local-business thumbnail for: {video.title}".strip()
    output_path = thumbnail_output_path(video.id)
    settings = get_settings()
    provider = (settings.image_generation_provider or "placeholder").strip().lower() or "placeholder"
    api_key = (settings.image_generation_api_key or "").strip()
    model = (settings.image_generation_model or _DEFAULT_IMAGE_MODEL).strip() or _DEFAULT_IMAGE_MODEL
    warnings: list[str] = []

    fallback_used = False
    provider_used = provider
    generated = False

    if provider in {"placeholder", "local_placeholder"}:
        fallback_used = True
        provider_used = "placeholder"
        warnings.append("Using deterministic local placeholder thumbnail.")
    elif not api_key:
        fallback_used = True
        provider_used = "placeholder"
        warnings.append("Image provider key not configured; wrote deterministic placeholder thumbnail.")
    elif provider in {"openai", "openai_images"}:
        try:
            _generate_with_openai_images(prompt=prompt, model=model, api_key=api_key, output_path=output_path)
            generated = True
            provider_used = "openai"
        except Exception as exc:  # noqa: BLE001
            fallback_used = True
            provider_used = "placeholder"
            warnings.append(f"Provider generation failed; wrote deterministic placeholder thumbnail. ({exc})")
    else:
        fallback_used = True
        provider_used = "placeholder"
        warnings.append(f"Unsupported image provider '{provider}'; wrote deterministic placeholder thumbnail.")

    if fallback_used:
        _write_placeholder_thumbnail(output_path)
        generated = True

    return ThumbnailGenerationResult(
        video_id=video.id,
        thumbnail_path=str(output_path),
        provider=provider_used,
        generated=generated and output_path.is_file(),
        fallback_used=fallback_used,
        warnings=warnings,
        prompt_used=prompt,
    )
