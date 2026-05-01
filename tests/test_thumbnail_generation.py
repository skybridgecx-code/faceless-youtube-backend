from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import app.services.thumbnail_generation as thumbnail_generation


class _DummySettings:
    def __init__(
        self,
        *,
        output_path: Path,
        image_generation_provider: str = "placeholder",
        image_generation_api_key: str | None = None,
        image_generation_model: str = "gpt-image-1",
    ) -> None:
        self.output_path = output_path
        self.image_generation_provider = image_generation_provider
        self.image_generation_api_key = image_generation_api_key
        self.image_generation_model = image_generation_model


def test_thumbnail_service_writes_placeholder_without_provider_key(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _DummySettings(output_path=tmp_path, image_generation_provider="openai", image_generation_api_key=None)
    monkeypatch.setattr(thumbnail_generation, "get_settings", lambda: settings)

    video = SimpleNamespace(id=101, title="No Key Thumbnail")
    result = thumbnail_generation.generate_thumbnail_image(video, "Safe thumbnail prompt")

    path = Path(result.thumbnail_path)
    assert path.exists()
    assert result.generated is True
    assert result.fallback_used is True
    assert result.provider == "placeholder"
    assert any("placeholder" in warning.lower() for warning in result.warnings)


def test_thumbnail_service_provider_failure_falls_back_to_placeholder(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _DummySettings(
        output_path=tmp_path,
        image_generation_provider="openai",
        image_generation_api_key="test-key",
        image_generation_model="gpt-image-1",
    )
    monkeypatch.setattr(thumbnail_generation, "get_settings", lambda: settings)

    def fail_provider(**kwargs) -> None:  # noqa: ANN003
        raise RuntimeError("provider offline")

    monkeypatch.setattr(thumbnail_generation, "_generate_with_openai_images", fail_provider)
    video = SimpleNamespace(id=202, title="Provider Failure Thumbnail")
    result = thumbnail_generation.generate_thumbnail_image(video, "Safe thumbnail prompt")

    path = Path(result.thumbnail_path)
    assert path.exists()
    assert result.generated is True
    assert result.fallback_used is True
    assert result.provider == "placeholder"
    assert any("provider generation failed" in warning.lower() for warning in result.warnings)

