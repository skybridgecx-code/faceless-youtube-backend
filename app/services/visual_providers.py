"""Visual asset generation providers.

This module produces real, on-disk image assets for the visual generation
worker. It has two modes:

* ``placeholder`` (default): a deterministic, seeded 1280x720 PNG "card".
  Requires no paid provider and no network. The output is a genuine RGB PNG
  (not a 1x1 stub), so it is immediately usable by the ffmpeg final renderer.

* an external provider (e.g. ``openai_image``): when configured *and* an API
  key is present, attempt a real generation. ANY failure (missing key, network
  error, bad response) degrades gracefully to the deterministic card and records
  a structured warning. Provider failures never raise out of here and never
  crash the worker.

Generated assets are always left for manual review by the caller — this module
never approves anything.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
_BLOCK = 64  # stripe block width in pixels
_ROW_BAND = 48  # stripe band height in pixels
_LOCAL_PROVIDERS = {"", "placeholder", "local", "manual", "stub"}
_OPENAI_PROVIDERS = {"openai", "openai_image", "gpt-image"}


@dataclass(frozen=True)
class ProviderResult:
    """Outcome of a single visual asset generation attempt."""

    ok: bool
    provider: str
    requested_provider: str
    fallback_used: bool
    mime_type: str
    width: int
    height: int
    note: str
    warning: str | None


# ---------------------------------------------------------------------------
# PNG encoding (stdlib only)
# ---------------------------------------------------------------------------

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, rows: list[bytes]) -> bytes:
    """Encode RGB scanlines into a valid PNG byte string.

    ``rows`` must contain ``height`` entries, each ``width * 3`` bytes (RGB).
    """
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0 (none)
        raw.extend(row)
    compressed = zlib.compress(bytes(raw), 9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def render_deterministic_card(
    seed_text: str,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> bytes:
    """Render a deterministic, seeded gradient card as a real RGB PNG.

    Same ``seed_text`` always yields identical bytes. Built by tiling stripe
    blocks per row so it stays fast even at 720p.
    """
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    top = (digest[0], digest[1], digest[2])
    bottom = (digest[3], digest[4], digest[5])
    accent = digest[6]
    factors = (1.0, 0.86, 0.72)
    blocks = (width + _BLOCK - 1) // _BLOCK

    rows: list[bytes] = []
    for y in range(height):
        blend = y / max(1, height - 1)
        base_r = int(top[0] * (1.0 - blend) + bottom[0] * blend)
        base_g = int(top[1] * (1.0 - blend) + bottom[1] * blend)
        base_b = int(top[2] * (1.0 - blend) + bottom[2] * blend)
        band = y // _ROW_BAND
        row = bytearray()
        for block_index in range(blocks):
            factor = factors[(block_index + band + accent) % 3]
            pixel = bytes(
                (
                    int(base_r * factor),
                    int(base_g * factor),
                    int(base_b * factor),
                )
            )
            span = min(_BLOCK, width - block_index * _BLOCK)
            row.extend(pixel * span)
        rows.append(bytes(row))

    return encode_png(width, height, rows)


# ---------------------------------------------------------------------------
# Optional external provider (OpenAI Images)
# ---------------------------------------------------------------------------

def _openai_image_bytes(
    *,
    prompt: str,
    settings: Settings,
    size: str = "1280x720",
    timeout: float = 60.0,
) -> bytes:
    """Call the OpenAI image API and return decoded PNG bytes.

    Raises on any error so the caller can fall back deterministically.
    """
    api_key = (settings.image_generation_api_key or settings.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("image_generation_api_key is not set")

    model = (settings.image_generation_model or "gpt-image-1").strip()
    body = json.dumps(
        {
            "model": model,
            "prompt": prompt[:4000],
            "size": size,
            "n": 1,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url="https://api.openai.com/v1/images/generations",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    data = payload.get("data") or []
    if not data:
        raise RuntimeError("provider returned no image data")
    b64 = data[0].get("b64_json")
    if not b64:
        raise RuntimeError("provider response missing b64_json")
    raw = base64.b64decode(b64)
    if not raw:
        raise RuntimeError("provider returned empty image bytes")
    return raw


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_visual_asset(
    *,
    seed_text: str,
    prompt: str,
    negative_prompt: str | None,
    out_path: Path,
    settings: Settings,
) -> ProviderResult:
    """Generate one visual asset to ``out_path``.

    Always writes a usable file (falling back to a deterministic card if a real
    provider is unavailable or fails). Raises ``OSError`` only if even the
    fallback card cannot be written to disk.
    """
    requested = (settings.image_generation_provider or "placeholder").strip().lower()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Local / placeholder path — no network, no key.
    if requested in _LOCAL_PROVIDERS:
        out_path.write_bytes(render_deterministic_card(seed_text))
        return ProviderResult(
            ok=True,
            provider="placeholder",
            requested_provider=requested or "placeholder",
            fallback_used=False,
            mime_type="image/png",
            width=DEFAULT_WIDTH,
            height=DEFAULT_HEIGHT,
            note="Deterministic local placeholder card (no external provider). Pending manual review.",
            warning=None,
        )

    # External provider path — attempt real generation, degrade gracefully.
    if requested in _OPENAI_PROVIDERS:
        try:
            image_bytes = _openai_image_bytes(prompt=prompt, settings=settings)
            out_path.write_bytes(image_bytes)
            return ProviderResult(
                ok=True,
                provider=requested,
                requested_provider=requested,
                fallback_used=False,
                mime_type="image/png",
                width=DEFAULT_WIDTH,
                height=DEFAULT_HEIGHT,
                note=f"Generated by external provider '{requested}'. Pending manual review.",
                warning=None,
            )
        except (urllib.error.URLError, OSError, ValueError, RuntimeError, KeyError) as exc:
            warning = f"External provider '{requested}' failed ({exc}); used deterministic fallback card."
            out_path.write_bytes(render_deterministic_card(seed_text))
            return ProviderResult(
                ok=True,
                provider="placeholder",
                requested_provider=requested,
                fallback_used=True,
                mime_type="image/png",
                width=DEFAULT_WIDTH,
                height=DEFAULT_HEIGHT,
                note=f"Fallback card after '{requested}' failure. Pending manual review.",
                warning=warning,
            )

    # Unknown provider — never crash, fall back deterministically.
    out_path.write_bytes(render_deterministic_card(seed_text))
    return ProviderResult(
        ok=True,
        provider="placeholder",
        requested_provider=requested,
        fallback_used=True,
        mime_type="image/png",
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        note=f"Unknown provider '{requested}'; used deterministic fallback card. Pending manual review.",
        warning=f"Unknown image_generation_provider '{requested}'; used deterministic fallback card.",
    )


def build_seed_text(*, video_title: str, job: Any) -> str:
    """Stable seed for deterministic card rendering."""
    scene_title = ""
    scene = getattr(job, "scene", None)
    if scene is not None:
        scene_title = getattr(scene, "scene_title", "") or ""
    prompt = getattr(job, "prompt", "") or ""
    job_id = getattr(job, "id", "") or ""
    job_type = getattr(job, "job_type", "") or ""
    return f"{video_title}|{scene_title}|{job_type}|{prompt}|{job_id}"
