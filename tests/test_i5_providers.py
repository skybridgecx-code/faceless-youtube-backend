from __future__ import annotations

import io
import json
import wave

import httpx
import pytest

from app.production.local_visuals import (
    LocalVisualRequest,
    VisualMode,
    render_local_visual,
)
from app.production.providers import (
    GPTImageProvider,
    I5_MAX_GENERATED_PROMPT_CHARS,
    I5_TTS_SAFE_INPUT_CHARS,
    ImageRequest,
    OpenAITTSProvider,
    ProviderConfigurationError,
    ProviderError,
    SoraConfiguration,
    SoraCreateRequest,
    SoraProvider,
    TTSRequest,
    is_valid_sora_provider_job_id,
    safe_generated_media_prompt,
    tts_cost_microunits,
    tts_reservation_microunits,
)


def _wav_bytes(*, frames: int = 800, sample_rate: int = 8_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def _png_bytes() -> bytes:
    return render_local_visual(
        LocalVisualRequest(
            mode=VisualMode.DETERMINISTIC_MOTION_GRAPHIC,
            title="Conceptual systems",
            scene_position=0,
            visual_purpose="Create a provider-returned test plate",
        )
    ).png_bytes


@pytest.mark.parametrize(
    ("model", "unit_price"),
    (("tts-1-hd", 30), ("tts-1", 15)),
)
def test_tts_exact_request_and_deterministic_cost(
    model: str,
    unit_price: int,
) -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert request.method == "POST"
        assert request.url.path == "/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, content=_wav_bytes(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        request = TTSRequest(text="Exact narration.", model=model)  # type: ignore[arg-type]
        result = OpenAITTSProvider(client=client).generate(
            request,
            api_key="test-key",
        )

    assert calls == [
        {
            "input": "Exact narration.",
            "model": model,
            "response_format": "wav",
            "voice": "onyx",
        }
    ]
    expected_cost = len("Exact narration.") * unit_price
    assert result.audio_bytes.startswith(b"RIFF")
    assert result.character_count == len("Exact narration.")
    assert result.cost_microunits == expected_cost
    assert result.reservation_microunits == (expected_cost * 120 + 99) // 100


def test_tts_cost_helpers_cover_primary_and_fallback() -> None:
    assert tts_cost_microunits(100, "tts-1-hd") == 3_000
    assert tts_reservation_microunits(100, "tts-1-hd") == 3_600
    assert tts_cost_microunits(100, "tts-1") == 1_500
    assert tts_reservation_microunits(100, "tts-1") == 1_800


def test_tts_input_is_never_truncated_or_dispatched_above_safe_limit() -> None:
    with pytest.raises(ValueError, match="safe limit"):
        TTSRequest(text="x" * (I5_TTS_SAFE_INPUT_CHARS + 1))
    exact = TTSRequest(text="x" * I5_TTS_SAFE_INPUT_CHARS)
    assert len(exact.text) == I5_TTS_SAFE_INPUT_CHARS


def test_tts_rejects_invalid_wav_with_sanitized_error() -> None:
    secret = "sk-test-secret-that-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(secret)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as raised:
            OpenAITTSProvider(client=client).generate(
                TTSRequest(text="Narration"),
                api_key=secret,
            )

    assert secret not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("quality", "reservation"),
    (("medium", 100_000), ("low", 25_000)),
)
def test_gpt_image_2_exact_request_and_quality_reservation(
    quality: str,
    reservation: int,
) -> None:
    calls: list[dict[str, object]] = []
    png = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        import base64

        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(png).decode()}]},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        request = ImageRequest(
            concept="Abstract distributed systems moving through light",
            quality=quality,  # type: ignore[arg-type]
        )
        result = GPTImageProvider(client=client).generate(request, api_key="test-key")

    assert calls == [
        {
            "background": "opaque",
            "model": "gpt-image-2",
            "n": 1,
            "output_format": "png",
            "prompt": request.prompt,
            "quality": quality,
            "size": "1280x720",
        }
    ]
    assert result.image_bytes == png
    assert result.model == "gpt-image-2"
    assert result.size == "1280x720"
    assert result.reservation_microunits == reservation


def test_generated_prompt_removes_factual_requests_and_adds_explicit_guardrails() -> None:
    prompt = safe_generated_media_prompt(
        "Exact Acme UI screenshot with Q4 chart, logo, benchmark 42, and a citation"
    )
    lowered = prompt.lower()

    assert len(prompt) <= I5_MAX_GENERATED_PROMPT_CHARS
    assert "acme" in lowered
    assert "q4" not in lowered
    assert "42" not in lowered
    assert "not evidence" in lowered
    assert "no readable text" in lowered
    assert "no" in lowered and "logos" in lowered
    assert "identifiable people" in lowered


def test_image_provider_rejects_wrong_png_dimensions() -> None:
    import base64
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    one_pixel = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(one_pixel).decode()}]},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="invalid PNG size"):
            GPTImageProvider(client=client).generate(
                ImageRequest(concept="Abstract motion"),
                api_key="test-key",
            )


def test_image_provider_rejects_truncated_png_header() -> None:
    import base64
    import struct

    truncated = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 1280, 720)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(truncated).decode()}]},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="invalid PNG content"):
            GPTImageProvider(client=client).generate(
                ImageRequest(concept="Abstract motion"),
                api_key="test-key",
            )


def test_sora_is_disabled_by_default_and_sora_pro_is_never_valid() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = SoraProvider(client=client)
        assert provider.enabled is False
        with pytest.raises(ProviderConfigurationError, match="disabled"):
            provider.create(SoraCreateRequest("Atmospheric concept"), api_key="test")

    assert calls == 0
    with pytest.raises(ProviderConfigurationError, match="sora-2"):
        SoraConfiguration(
            provider="openai",
            model="sora-2-pro",  # type: ignore[arg-type]
            allow_deprecated_sora=True,
        )


def test_sora_opt_in_create_bounded_poll_and_download() -> None:
    create_payloads: list[dict[str, object]] = []
    poll_calls = 0
    download_calls = 0
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls, download_calls
        if request.method == "POST":
            create_payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"id": "video_job_123", "status": "queued"},
                request=request,
            )
        if request.url.path.endswith("/content"):
            download_calls += 1
            return httpx.Response(200, content=mp4, request=request)
        poll_calls += 1
        status = "in_progress" if poll_calls == 1 else "completed"
        return httpx.Response(
            200,
            json={"id": "video_job_123", "status": status},
            request=request,
        )

    configuration = SoraConfiguration(
        provider="openai",
        allow_deprecated_sora=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = SoraProvider(configuration, client=client)
        created = provider.create(
            SoraCreateRequest("Abstract network transformation"),
            api_key="test-key",
        )
        terminal = provider.poll(
            created.provider_job_id,
            api_key="test-key",
            max_polls=2,
        )
        downloaded = provider.download(created.provider_job_id, api_key="test-key")

    assert create_payloads == [
        {
            "model": "sora-2",
            "prompt": SoraCreateRequest("Abstract network transformation").prompt,
            "seconds": "8",
            "size": "1280x720",
        }
    ]
    assert created.reservation_microunits == 960_000
    assert poll_calls == 2
    assert terminal.status == "completed"
    assert download_calls == 1
    assert downloaded.video_bytes == mp4
    assert downloaded.cost_microunits == 800_000


def test_sora_polling_is_read_only_and_strictly_bounded() -> None:
    get_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls
        assert request.method == "GET"
        get_calls += 1
        return httpx.Response(
            200,
            json={"id": "job_1", "status": "processing"},
            request=request,
        )

    configuration = SoraConfiguration(
        provider="openai",
        allow_deprecated_sora=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = SoraProvider(configuration, client=client)
        result = provider.poll("job_1", api_key="test", max_polls=3)
        with pytest.raises(ValueError, match="between 1 and 120"):
            provider.poll("job_1", api_key="test", max_polls=121)

    assert result.status == "processing"
    assert get_calls == 3


@pytest.mark.parametrize(
    ("provider_job_id", "expected"),
    (
        ("video_audit_1", True),
        ("video-job-123", True),
        ("video.valid.with.dot", False),
        ("video:invalid", False),
        ("invalid provider id", False),
        ("x" * 161, False),
    ),
)
def test_sora_provider_job_identity_has_one_canonical_boundary(
    provider_job_id: str,
    expected: bool,
) -> None:
    assert is_valid_sora_provider_job_id(provider_job_id) is expected


def test_provider_module_has_no_orm_or_session_boundary() -> None:
    import inspect
    import app.production.providers as providers

    source = inspect.getsource(providers)
    assert "sqlalchemy" not in source.lower()
    assert "SessionLocal" not in source
    assert not hasattr(OpenAITTSProvider(), "session")
    assert not hasattr(GPTImageProvider(), "session")
    assert not hasattr(SoraProvider(), "session")
