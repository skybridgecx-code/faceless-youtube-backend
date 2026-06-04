from __future__ import annotations

import struct
from pathlib import Path

import app.services.visual_providers as vp
from app.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "image_generation_provider": "placeholder",
        "image_generation_api_key": None,
        "image_generation_model": "gpt-image-1",
        "openai_api_key": None,
    }
    base.update(overrides)
    return Settings(**base)


def _assert_valid_png(path: Path, width: int = 1280, height: int = 720) -> None:
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "missing PNG signature"
    w, h = struct.unpack(">II", raw[16:24])
    assert (w, h) == (width, height)
    assert raw[24] == 8 and raw[25] == 2, "expected 8-bit RGB"


def test_deterministic_card_is_valid_and_stable(tmp_path: Path) -> None:
    a = vp.render_deterministic_card("seed-1|scene|image|prompt|1")
    b = vp.render_deterministic_card("seed-1|scene|image|prompt|1")
    c = vp.render_deterministic_card("seed-2|scene|image|prompt|2")
    assert a == b, "same seed must be deterministic"
    assert a != c, "different seed must differ"
    out = tmp_path / "card.png"
    out.write_bytes(a)
    _assert_valid_png(out)


def test_placeholder_provider_writes_real_png(tmp_path: Path) -> None:
    out = tmp_path / "asset.png"
    result = vp.generate_visual_asset(
        seed_text="t|s|image|p|1",
        prompt="A clean dashboard",
        negative_prompt=None,
        out_path=out,
        settings=_settings(image_generation_provider="placeholder"),
    )
    assert result.ok is True
    assert result.provider == "placeholder"
    assert result.fallback_used is False
    assert result.warning is None
    _assert_valid_png(out)


def test_unknown_provider_falls_back_with_warning(tmp_path: Path) -> None:
    out = tmp_path / "asset.png"
    result = vp.generate_visual_asset(
        seed_text="t|s|image|p|1",
        prompt="x",
        negative_prompt=None,
        out_path=out,
        settings=_settings(image_generation_provider="some_unsupported_provider"),
    )
    assert result.ok is True
    assert result.fallback_used is True
    assert result.warning is not None
    assert result.provider == "placeholder"
    _assert_valid_png(out)  # still produced a usable file


def test_openai_provider_without_key_falls_back(tmp_path: Path) -> None:
    out = tmp_path / "asset.png"
    result = vp.generate_visual_asset(
        seed_text="t|s|image|p|1",
        prompt="x",
        negative_prompt=None,
        out_path=out,
        settings=_settings(image_generation_provider="openai_image", image_generation_api_key=None),
    )
    assert result.ok is True
    assert result.fallback_used is True
    assert result.requested_provider == "openai_image"
    assert result.warning is not None
    _assert_valid_png(out)


def test_openai_provider_failure_falls_back(monkeypatch, tmp_path: Path) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(vp, "_openai_image_bytes", _boom)
    out = tmp_path / "asset.png"
    result = vp.generate_visual_asset(
        seed_text="t|s|image|p|1",
        prompt="x",
        negative_prompt=None,
        out_path=out,
        settings=_settings(image_generation_provider="openai_image", image_generation_api_key="sk-test"),
    )
    assert result.ok is True
    assert result.fallback_used is True
    assert "simulated network failure" in (result.warning or "")
    _assert_valid_png(out)


def test_openai_provider_success_uses_real_bytes(monkeypatch, tmp_path: Path) -> None:
    real_png = vp.render_deterministic_card("provider-returned-image")

    def _ok(*args, **kwargs):
        return real_png

    monkeypatch.setattr(vp, "_openai_image_bytes", _ok)
    out = tmp_path / "asset.png"
    result = vp.generate_visual_asset(
        seed_text="t|s|image|p|1",
        prompt="A studio photo",
        negative_prompt=None,
        out_path=out,
        settings=_settings(image_generation_provider="openai_image", image_generation_api_key="sk-test"),
    )
    assert result.ok is True
    assert result.fallback_used is False
    assert result.provider == "openai_image"
    assert result.warning is None
    assert out.read_bytes() == real_png
