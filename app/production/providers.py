from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import re
import struct
import time
import zlib
from typing import Callable, Literal, Protocol

import httpx


OPENAI_API_BASE_URL = "https://api.openai.com/v1"

I5_TTS_MODELS = ("tts-1-hd", "tts-1")
I5_TTS_VOICE = "onyx"
I5_TTS_RESPONSE_FORMAT = "wav"
I5_TTS_SAFE_INPUT_CHARS = 3_800
I5_TTS_PRICE_PER_CHARACTER_MICROUSD = {
    "tts-1-hd": 30,
    "tts-1": 15,
}

I5_IMAGE_MODEL = "gpt-image-2"
I5_IMAGE_SIZE = "1280x720"
I5_IMAGE_WIDTH = 1_280
I5_IMAGE_HEIGHT = 720
I5_IMAGE_QUALITIES = ("medium", "low")
I5_IMAGE_RESERVATION_MICROUSD = {
    "medium": 100_000,
    "low": 25_000,
}
I5_MAX_IMAGE_CONCEPT_CHARS = 240
I5_MAX_GENERATED_PROMPT_CHARS = 600

I5_SORA_MODEL = "sora-2"
I5_SORA_DURATION_SECONDS = 8
I5_SORA_SIZE = "1280x720"
I5_SORA_MAX_POLLS = 120
I5_SORA_RESERVATION_PER_SECOND_MICROUSD = 120_000
I5_SORA_COST_PER_SECOND_MICROUSD = 100_000

_SAFE_PROVIDER_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")
_SPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_UNSAFE_CONCEPT_PHRASES = re.compile(
    r"\b(?:"
    r"ui|user\s+interface|interface|screenshot|screen|source\s+document|document|"
    r"citation|chart|graph|benchmark|logo|wordmark|brand\s+mark|product\s+interface|"
    r"named\s+person|celebrity|likeness|exact\s+product|number|numerical\s+result"
    r")\b",
    re.IGNORECASE,
)


class ProviderError(RuntimeError):
    """A sanitized provider-boundary failure safe for persistence and logs."""


class ProviderConfigurationError(ProviderError):
    """The request conflicts with the locked I5 provider catalog."""


class _HttpClient(Protocol):
    def post(self, url: str, **kwargs: object) -> httpx.Response: ...

    def get(self, url: str, **kwargs: object) -> httpx.Response: ...


@dataclass(frozen=True, slots=True)
class TTSRequest:
    text: str
    model: Literal["tts-1-hd", "tts-1"] = "tts-1-hd"
    voice: Literal["onyx"] = I5_TTS_VOICE
    response_format: Literal["wav"] = I5_TTS_RESPONSE_FORMAT

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise ValueError("TTS input must be nonblank")
        if len(self.text) > I5_TTS_SAFE_INPUT_CHARS:
            raise ValueError(
                f"TTS input exceeds the {I5_TTS_SAFE_INPUT_CHARS}-character safe limit"
            )
        if self.model not in I5_TTS_MODELS:
            raise ValueError("TTS model conflicts with the locked I5 catalog")
        if self.voice != I5_TTS_VOICE:
            raise ValueError("TTS voice must be onyx")
        if self.response_format != I5_TTS_RESPONSE_FORMAT:
            raise ValueError("TTS response format must be wav")

    @property
    def character_count(self) -> int:
        return len(self.text)

    @property
    def cost_microunits(self) -> int:
        return tts_cost_microunits(self.character_count, self.model)

    @property
    def reservation_microunits(self) -> int:
        return tts_reservation_microunits(self.character_count, self.model)


@dataclass(frozen=True, slots=True)
class TTSResult:
    audio_bytes: bytes
    model: str
    voice: str
    response_format: str
    character_count: int
    cost_microunits: int
    reservation_microunits: int

    def metadata(self) -> dict[str, object]:
        return {
            "character_count": self.character_count,
            "cost_microunits": self.cost_microunits,
            "model": self.model,
            "reservation_microunits": self.reservation_microunits,
            "response_format": self.response_format,
            "voice": self.voice,
        }


def tts_cost_microunits(
    character_count: int,
    model: Literal["tts-1-hd", "tts-1"],
) -> int:
    if character_count < 0:
        raise ValueError("TTS character count cannot be negative")
    try:
        unit_price = I5_TTS_PRICE_PER_CHARACTER_MICROUSD[model]
    except KeyError:
        raise ValueError("TTS model conflicts with the locked I5 catalog") from None
    return character_count * unit_price


def tts_reservation_microunits(
    character_count: int,
    model: Literal["tts-1-hd", "tts-1"],
) -> int:
    predicted = tts_cost_microunits(character_count, model)
    return (predicted * 120 + 99) // 100


class OpenAITTSProvider:
    """Single-dispatch OpenAI speech adapter with no application state."""

    def __init__(
        self,
        *,
        client: _HttpClient | None = None,
        base_url: str = OPENAI_API_BASE_URL,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def generate(self, request: TTSRequest, *, api_key: str) -> TTSResult:
        key = _required_api_key(api_key)
        payload = {
            "input": request.text,
            "model": request.model,
            "response_format": request.response_format,
            "voice": request.voice,
        }
        response = _single_post(
            client=self._client,
            url=f"{self._base_url}/audio/speech",
            api_key=key,
            payload=payload,
            timeout_seconds=self._timeout_seconds,
            operation="OpenAI TTS",
        )
        audio_bytes = bytes(response.content)
        if not _is_wav(audio_bytes):
            raise ProviderError("OpenAI TTS returned invalid WAV audio") from None
        return TTSResult(
            audio_bytes=audio_bytes,
            model=request.model,
            voice=request.voice,
            response_format=request.response_format,
            character_count=request.character_count,
            cost_microunits=request.cost_microunits,
            reservation_microunits=request.reservation_microunits,
        )


@dataclass(frozen=True, slots=True)
class ImageRequest:
    concept: str
    quality: Literal["medium", "low"] = "medium"

    def __post_init__(self) -> None:
        if not self.concept or not self.concept.strip():
            raise ValueError("generated-image concept must be nonblank")
        if len(self.concept) > I5_MAX_IMAGE_CONCEPT_CHARS:
            raise ValueError(
                "generated-image concept exceeds the bounded I5 prompt input"
            )
        if self.quality not in I5_IMAGE_QUALITIES:
            raise ValueError("generated-image quality must be medium or low")

    @property
    def prompt(self) -> str:
        return safe_generated_media_prompt(self.concept)

    @property
    def reservation_microunits(self) -> int:
        return I5_IMAGE_RESERVATION_MICROUSD[self.quality]


@dataclass(frozen=True, slots=True)
class ImageResult:
    image_bytes: bytes
    model: str
    quality: str
    size: str
    output_format: str
    prompt: str
    reservation_microunits: int

    def metadata(self) -> dict[str, object]:
        return {
            "model": self.model,
            "output_format": self.output_format,
            "prompt": self.prompt,
            "quality": self.quality,
            "reservation_microunits": self.reservation_microunits,
            "size": self.size,
        }


def safe_generated_media_prompt(concept: str) -> str:
    """Return a bounded illustrative prompt with factual-media requests removed."""

    normalized = _SPACE.sub(" ", concept).strip()
    normalized = _DIGITS.sub("", normalized)
    normalized = _UNSAFE_CONCEPT_PHRASES.sub("", normalized)
    normalized = _SPACE.sub(" ", normalized).strip(" ,.;:-")
    if not normalized:
        normalized = "abstract technological systems in motion"
    normalized = normalized[:I5_MAX_IMAGE_CONCEPT_CHARS].rstrip()
    guard = (
        "Conceptual atmospheric illustration only, not evidence. "
        "No readable text, letters, numbers, logos, brand marks, charts, graphs, "
        "interfaces, screens, source documents, citations, benchmarks, exact products, "
        "or identifiable people."
    )
    prompt = f"{normalized}. {guard}"
    if len(prompt) > I5_MAX_GENERATED_PROMPT_CHARS:
        raise ValueError("generated-media prompt exceeds its canonical bound")
    return prompt


class GPTImageProvider:
    """Single-dispatch GPT Image 2 adapter for illustrative cinematic plates."""

    def __init__(
        self,
        *,
        client: _HttpClient | None = None,
        base_url: str = OPENAI_API_BASE_URL,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def generate(self, request: ImageRequest, *, api_key: str) -> ImageResult:
        key = _required_api_key(api_key)
        payload = {
            "background": "opaque",
            "model": I5_IMAGE_MODEL,
            "n": 1,
            "output_format": "png",
            "prompt": request.prompt,
            "quality": request.quality,
            "size": I5_IMAGE_SIZE,
        }
        response = _single_post(
            client=self._client,
            url=f"{self._base_url}/images/generations",
            api_key=key,
            payload=payload,
            timeout_seconds=self._timeout_seconds,
            operation="OpenAI image generation",
        )
        body = _response_json(response, operation="OpenAI image generation")
        try:
            encoded = body["data"][0]["b64_json"]
            if not isinstance(encoded, str):
                raise TypeError
            image_bytes = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error):
            raise ProviderError(
                "OpenAI image generation returned invalid image content"
            ) from None
        dimensions = validate_png_bytes(image_bytes)
        if dimensions != (I5_IMAGE_WIDTH, I5_IMAGE_HEIGHT):
            raise ProviderError(
                "OpenAI image generation returned an invalid PNG size"
            ) from None
        return ImageResult(
            image_bytes=image_bytes,
            model=I5_IMAGE_MODEL,
            quality=request.quality,
            size=I5_IMAGE_SIZE,
            output_format="png",
            prompt=request.prompt,
            reservation_microunits=request.reservation_microunits,
        )


@dataclass(frozen=True, slots=True)
class SoraConfiguration:
    provider: Literal["disabled", "openai", "sora"] = "disabled"
    model: Literal["sora-2"] = I5_SORA_MODEL
    allow_deprecated_sora: bool = False

    def __post_init__(self) -> None:
        if self.provider not in {"disabled", "openai", "sora"}:
            raise ProviderConfigurationError("I5 video provider is invalid")
        if self.model != I5_SORA_MODEL:
            raise ProviderConfigurationError("Only sora-2 is allowed for I5 video")
        if self.provider != "disabled" and not self.allow_deprecated_sora:
            raise ProviderConfigurationError(
                "Sora requires explicit deprecated-provider opt-in"
            )

    @property
    def enabled(self) -> bool:
        return self.provider in {"openai", "sora"} and self.allow_deprecated_sora


@dataclass(frozen=True, slots=True)
class SoraCreateRequest:
    concept: str

    def __post_init__(self) -> None:
        if not self.concept or not self.concept.strip():
            raise ValueError("Sora concept must be nonblank")
        if len(self.concept) > I5_MAX_IMAGE_CONCEPT_CHARS:
            raise ValueError("Sora concept exceeds the bounded I5 prompt input")

    @property
    def prompt(self) -> str:
        return safe_generated_media_prompt(self.concept)

    @property
    def reservation_microunits(self) -> int:
        return (
            I5_SORA_DURATION_SECONDS
            * I5_SORA_RESERVATION_PER_SECOND_MICROUSD
        )

    @property
    def successful_cost_microunits(self) -> int:
        return I5_SORA_DURATION_SECONDS * I5_SORA_COST_PER_SECOND_MICROUSD


@dataclass(frozen=True, slots=True)
class SoraJob:
    provider_job_id: str
    status: str
    model: str = I5_SORA_MODEL
    duration_seconds: int = I5_SORA_DURATION_SECONDS
    size: str = I5_SORA_SIZE
    reservation_microunits: int = (
        I5_SORA_DURATION_SECONDS * I5_SORA_RESERVATION_PER_SECOND_MICROUSD
    )

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class SoraDownload:
    provider_job_id: str
    video_bytes: bytes
    cost_microunits: int = (
        I5_SORA_DURATION_SECONDS * I5_SORA_COST_PER_SECOND_MICROUSD
    )


class SoraProvider:
    """Explicit-opt-in Sora job adapter with bounded GET-only polling."""

    def __init__(
        self,
        configuration: SoraConfiguration | None = None,
        *,
        client: _HttpClient | None = None,
        base_url: str = OPENAI_API_BASE_URL,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.configuration = configuration or SoraConfiguration()
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return self.configuration.enabled

    def create(self, request: SoraCreateRequest, *, api_key: str) -> SoraJob:
        self._require_enabled()
        key = _required_api_key(api_key)
        response = _single_post(
            client=self._client,
            url=f"{self._base_url}/videos",
            api_key=key,
            payload={
                "model": I5_SORA_MODEL,
                "prompt": request.prompt,
                "seconds": str(I5_SORA_DURATION_SECONDS),
                "size": I5_SORA_SIZE,
            },
            timeout_seconds=self._timeout_seconds,
            operation="Sora create",
        )
        return _sora_job_from_response(response, operation="Sora create")

    def poll_once(self, provider_job_id: str, *, api_key: str) -> SoraJob:
        self._require_enabled()
        job_id = _validated_provider_job_id(provider_job_id)
        response = _single_get(
            client=self._client,
            url=f"{self._base_url}/videos/{job_id}",
            api_key=_required_api_key(api_key),
            timeout_seconds=self._timeout_seconds,
            operation="Sora poll",
        )
        job = _sora_job_from_response(response, operation="Sora poll")
        if job.provider_job_id != job_id:
            raise ProviderError("Sora poll returned a conflicting job identity") from None
        return job

    def poll_until_terminal(
        self,
        provider_job_id: str,
        *,
        api_key: str,
        max_polls: int = I5_SORA_MAX_POLLS,
        interval_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> SoraJob:
        if not 1 <= max_polls <= I5_SORA_MAX_POLLS:
            raise ValueError(
                f"Sora max_polls must be between 1 and {I5_SORA_MAX_POLLS}"
            )
        if interval_seconds < 0:
            raise ValueError("Sora poll interval cannot be negative")
        last: SoraJob | None = None
        for poll_index in range(max_polls):
            last = self.poll_once(provider_job_id, api_key=api_key)
            if last.terminal:
                return last
            if poll_index + 1 < max_polls and interval_seconds:
                sleep(interval_seconds)
        assert last is not None
        return last

    # Concise alias for orchestration code that names the bounded primitive `poll`.
    poll = poll_until_terminal

    def download(self, provider_job_id: str, *, api_key: str) -> SoraDownload:
        self._require_enabled()
        job_id = _validated_provider_job_id(provider_job_id)
        response = _single_get(
            client=self._client,
            url=f"{self._base_url}/videos/{job_id}/content",
            api_key=_required_api_key(api_key),
            timeout_seconds=self._timeout_seconds,
            operation="Sora download",
        )
        content = bytes(response.content)
        if len(content) < 12 or b"ftyp" not in content[4:16]:
            raise ProviderError("Sora download returned invalid video content") from None
        return SoraDownload(provider_job_id=job_id, video_bytes=content)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ProviderConfigurationError(
                "Sora is disabled; configure the provider and explicit deprecated opt-in"
            ) from None


# Explicit aliases keep call sites descriptive without duplicating contracts.
OpenAITTSAdapter = OpenAITTSProvider
GPTImageAdapter = GPTImageProvider
SoraAdapter = SoraProvider
TTSProviderRequest = TTSRequest
TTSProviderResult = TTSResult
ImageProviderRequest = ImageRequest
ImageProviderResult = ImageResult


def _required_api_key(api_key: str) -> str:
    value = api_key.strip()
    if not value:
        raise ProviderConfigurationError("OpenAI API credential is required") from None
    return value


def _authorization_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _single_post(
    *,
    client: _HttpClient | None,
    url: str,
    api_key: str,
    payload: dict[str, object],
    timeout_seconds: float,
    operation: str,
) -> httpx.Response:
    try:
        if client is not None:
            response = client.post(
                url,
                headers=_authorization_headers(api_key),
                json=payload,
                timeout=timeout_seconds,
            )
        else:
            with httpx.Client() as owned_client:
                response = owned_client.post(
                    url,
                    headers=_authorization_headers(api_key),
                    json=payload,
                    timeout=timeout_seconds,
                )
    except Exception:
        raise ProviderError(f"{operation} transport failure") from None
    _raise_for_status(response, operation=operation)
    return response


def _single_get(
    *,
    client: _HttpClient | None,
    url: str,
    api_key: str,
    timeout_seconds: float,
    operation: str,
) -> httpx.Response:
    try:
        if client is not None:
            response = client.get(
                url,
                headers=_authorization_headers(api_key),
                timeout=timeout_seconds,
            )
        else:
            with httpx.Client() as owned_client:
                response = owned_client.get(
                    url,
                    headers=_authorization_headers(api_key),
                    timeout=timeout_seconds,
                )
    except Exception:
        raise ProviderError(f"{operation} transport failure") from None
    _raise_for_status(response, operation=operation)
    return response


def _raise_for_status(response: httpx.Response, *, operation: str) -> None:
    if not 200 <= response.status_code < 300:
        raise ProviderError(
            f"{operation} failed with HTTP {response.status_code}"
        ) from None


def _response_json(response: httpx.Response, *, operation: str) -> dict[str, object]:
    try:
        body = response.json()
    except Exception:
        raise ProviderError(f"{operation} returned invalid JSON") from None
    if not isinstance(body, dict):
        raise ProviderError(f"{operation} returned an invalid response") from None
    return body


def _is_wav(content: bytes) -> bool:
    return (
        len(content) >= 12
        and content[:4] == b"RIFF"
        and content[8:12] == b"WAVE"
    )


def _png_dimensions(content: bytes) -> tuple[int, int] | None:
    if len(content) < 57 or content[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    offset = 8
    dimensions: tuple[int, int] | None = None
    channels: int | None = None
    compressed = bytearray()
    saw_idat = False
    saw_iend = False
    while offset < len(content):
        if offset + 12 > len(content):
            return None
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        kind = content[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(content):
            return None
        data = content[data_start:data_end]
        expected_crc = struct.unpack(">I", content[data_end:crc_end])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            return None
        if dimensions is None:
            if kind != b"IHDR" or length != 13:
                return None
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", data)
            )
            channel_map = {0: 1, 2: 3, 4: 2, 6: 4}
            channels = channel_map.get(color_type)
            if (
                width <= 0
                or height <= 0
                or bit_depth != 8
                or channels is None
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                return None
            dimensions = (width, height)
        elif kind == b"IHDR":
            return None
        if kind == b"IDAT":
            if saw_iend:
                return None
            saw_idat = True
            compressed.extend(data)
        elif kind == b"IEND":
            if length != 0 or not saw_idat:
                return None
            saw_iend = True
            offset = crc_end
            break
        offset = crc_end
    if not saw_iend or offset != len(content) or dimensions is None or channels is None:
        return None
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error:
        return None
    width, height = dimensions
    row_size = 1 + width * channels
    if len(raw) != row_size * height:
        return None
    if any(raw[row * row_size] > 4 for row in range(height)):
        return None
    return dimensions


def validate_png_bytes(content: bytes) -> tuple[int, int]:
    """Fully validate the bounded non-interlaced PNG form accepted by I5."""

    dimensions = _png_dimensions(content)
    if dimensions is None:
        raise ProviderError("OpenAI image generation returned invalid PNG content") from None
    return dimensions


def is_valid_sora_provider_job_id(value: object) -> bool:
    return isinstance(value, str) and _SAFE_PROVIDER_ID.fullmatch(value) is not None


def _validated_provider_job_id(provider_job_id: str) -> str:
    value = provider_job_id.strip()
    if not is_valid_sora_provider_job_id(value):
        raise ProviderError("Sora provider job identity is invalid") from None
    return value


def _sora_job_from_response(
    response: httpx.Response,
    *,
    operation: str,
) -> SoraJob:
    body = _response_json(response, operation=operation)
    try:
        provider_job_id = _validated_provider_job_id(str(body["id"]))
        status = str(body["status"]).strip().lower()
    except (KeyError, TypeError, ValueError):
        raise ProviderError(f"{operation} returned an invalid job") from None
    if status not in {
        "queued",
        "in_progress",
        "processing",
        "completed",
        "failed",
        "cancelled",
    }:
        raise ProviderError(f"{operation} returned an invalid job status") from None
    return SoraJob(provider_job_id=provider_job_id, status=status)
