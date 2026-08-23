from __future__ import annotations

import hashlib
import struct
import zlib

import pytest
from pydantic import ValidationError

from app.editorial.contracts import canonical_sha256
from app.production.thumbnail_renderer import (
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    ThumbnailRenderError,
    decode_png,
    render_thumbnail,
)
import app.production.thumbnail_renderer as thumbnail_renderer
from app.qa.contracts import (
    Rectangle,
    ThumbnailLayout,
    ThumbnailRenderSpec,
    VisualSourceBinding,
)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(
    color: tuple[int, int, int],
    *,
    alpha: int | None = None,
    width: int = 1280,
    height: int = 720,
) -> bytes:
    pixel = bytes((*color, alpha)) if alpha is not None else bytes(color)
    raw = b"".join(b"\x00" + pixel * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(
            b"IHDR",
            struct.pack(
                ">IIBBBBB", width, height, 8, 6 if alpha is not None else 2, 0, 0, 0
            ),
        )
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _layout(text: str = "SOURCE") -> ThumbnailLayout:
    raw = {
        "concept_id": "concept-0",
        "canvas_width_px": 1280,
        "canvas_height_px": 720,
        "text_elements": [
            {
                "text": text,
                "bounds": {"x": 40, "y": 40, "width": 700, "height": 120},
                "font_height_px": 48,
                "contrast_ratio": 7.0,
            }
        ],
        "visual_elements": [
            {"bounds": {"x": 300, "y": 180, "width": 800, "height": 450}}
        ],
    }
    return ThumbnailLayout(
        **raw,
        layout_spec_sha256=canonical_sha256(raw),
    )


def _spec(layout: ThumbnailLayout, source_sha256: str) -> ThumbnailRenderSpec:
    return ThumbnailRenderSpec(
        concept_id=layout.concept_id,
        layout_spec_sha256=layout.layout_spec_sha256,
        visual_bindings=(
            VisualSourceBinding(
                visual_index=0,
                source_artifact_sha256=source_sha256,
            ),
        ),
    )


def test_render_is_byte_identical_rgb_png_with_exact_hash() -> None:
    source = _png((20, 80, 160), alpha=180)
    source_sha256 = hashlib.sha256(source).hexdigest()
    layout = _layout()
    spec = _spec(layout, source_sha256)

    first = render_thumbnail(layout, spec, {source_sha256: source})
    second = render_thumbnail(layout, spec, {source_sha256: source})
    decoded = decode_png(first.png_bytes)

    assert first.png_bytes == second.png_bytes
    assert (decoded.width, decoded.height) == (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
    assert len(decoded.rgb_bytes) == THUMBNAIL_WIDTH * THUMBNAIL_HEIGHT * 3
    assert first.png_sha256 == hashlib.sha256(first.png_bytes).hexdigest()


def test_distinct_canonical_sources_produce_distinct_rendered_pixels() -> None:
    layout = _layout()
    blue = _png((20, 80, 160))
    red = _png((160, 40, 20))
    blue_sha256 = hashlib.sha256(blue).hexdigest()
    red_sha256 = hashlib.sha256(red).hexdigest()

    blue_result = render_thumbnail(layout, _spec(layout, blue_sha256), {blue_sha256: blue})
    red_result = render_thumbnail(layout, _spec(layout, red_sha256), {red_sha256: red})

    assert blue_result.png_sha256 != red_result.png_sha256


@pytest.mark.parametrize("mutation", (b"bad", b"\x89PNG\r\n\x1a\n"))
def test_decoder_rejects_malformed_png(mutation: bytes) -> None:
    with pytest.raises(ThumbnailRenderError):
        decode_png(mutation)


def test_decoder_rejects_crc_corruption() -> None:
    corrupted = bytearray(_png((1, 2, 3)))
    corrupted[-5] ^= 1

    with pytest.raises(ThumbnailRenderError, match="CRC"):
        decode_png(bytes(corrupted))


def test_render_rejects_missing_extra_and_incorrect_visual_bindings() -> None:
    source = _png((20, 80, 160))
    source_sha256 = hashlib.sha256(source).hexdigest()
    layout = _layout()
    missing = ThumbnailRenderSpec(
        concept_id=layout.concept_id,
        layout_spec_sha256=layout.layout_spec_sha256,
    )
    incorrect = _spec(layout, source_sha256).model_copy(
        update={
            "visual_bindings": (
                VisualSourceBinding(visual_index=1, source_artifact_sha256=source_sha256),
            )
        }
    )

    with pytest.raises(ThumbnailRenderError, match="exactly cover"):
        render_thumbnail(layout, missing, {})
    with pytest.raises(ThumbnailRenderError, match="runtime validation"):
        render_thumbnail(layout, incorrect, {source_sha256: source})


def test_render_spec_source_substitution_changes_content_addressed_identity() -> None:
    layout = _layout()
    first = _spec(layout, "1" * 64)
    second = _spec(layout, "2" * 64)

    assert first.sha256() != second.sha256()


def test_text_that_cannot_fit_its_declared_bounds_fails() -> None:
    layout = _layout("THIS TEXT CANNOT FIT")
    narrow_text = layout.text_elements[0].model_copy(
        update={"bounds": Rectangle(x=40, y=40, width=20, height=120)}
    )
    raw = layout.model_dump(mode="json")
    raw["text_elements"] = [narrow_text.model_dump(mode="json")]
    raw.pop("layout_spec_sha256")
    narrow_layout = ThumbnailLayout(
        **raw,
        layout_spec_sha256=canonical_sha256(raw),
    )
    source = _png((20, 80, 160))
    source_sha256 = hashlib.sha256(source).hexdigest()

    with pytest.raises(ThumbnailRenderError, match="text cannot fit"):
        render_thumbnail(
            narrow_layout,
            _spec(narrow_layout, source_sha256),
            {source_sha256: source},
        )


def _visual_layout(*, count: int = 1, text: str = "") -> ThumbnailLayout:
    raw = {
        "concept_id": "concept-0",
        "canvas_width_px": 1280,
        "canvas_height_px": 720,
        "text_elements": (
            [
                {
                    "text": text,
                    "bounds": {"x": 40, "y": 40, "width": 700, "height": 120},
                    "font_height_px": 48,
                    "contrast_ratio": 7.0,
                }
            ]
            if text
            else []
        ),
        "visual_elements": [
            {"bounds": {"x": 0, "y": 0, "width": 1280, "height": 720}}
            for _ in range(count)
        ],
    }
    return ThumbnailLayout(**raw, layout_spec_sha256=canonical_sha256(raw))


def _multi_spec(layout: ThumbnailLayout, hashes: tuple[str, ...]) -> ThumbnailRenderSpec:
    return ThumbnailRenderSpec(
        concept_id=layout.concept_id,
        layout_spec_sha256=layout.layout_spec_sha256,
        visual_bindings=tuple(
            VisualSourceBinding(visual_index=index, source_artifact_sha256=source_sha256)
            for index, source_sha256 in enumerate(hashes)
        ),
    )


def _pixel(png_bytes: bytes) -> tuple[int, int, int]:
    return tuple(decode_png(png_bytes).rgb_bytes[:3])  # type: ignore[return-value]


def test_runtime_revalidates_model_copy_policy_and_fit_mode() -> None:
    source = _png((20, 80, 160))
    source_sha256 = hashlib.sha256(source).hexdigest()
    layout = _layout()
    spec = _spec(layout, source_sha256)
    invalid_policy = spec.model_copy(update={"policy_version": "wrong"})
    invalid_fit = spec.model_copy(
        update={
            "visual_bindings": (
                spec.visual_bindings[0].model_copy(update={"fit_mode": "contain"}),
            )
        }
    )

    with pytest.raises(ThumbnailRenderError, match="runtime validation"):
        render_thumbnail(layout, invalid_policy, {source_sha256: source})
    with pytest.raises(ThumbnailRenderError, match="runtime validation"):
        render_thumbnail(layout, invalid_fit, {source_sha256: source})


def test_ordinary_render_spec_literal_construction_rejects_whitespace() -> None:
    layout = _layout()

    with pytest.raises(ValidationError):
        ThumbnailRenderSpec(
            policy_version=" i6-thumbnail-render-v1 ",
            concept_id=layout.concept_id,
            layout_spec_sha256=layout.layout_spec_sha256,
        )
    with pytest.raises(ValidationError):
        VisualSourceBinding(
            visual_index=0,
            source_artifact_sha256="1" * 64,
            fit_mode=" cover ",
        )


def test_runtime_rejects_whitespace_padded_literal_model_copies() -> None:
    source = _png((20, 80, 160))
    source_sha256 = hashlib.sha256(source).hexdigest()
    layout = _layout()
    spec = _spec(layout, source_sha256)
    invalid_policy = spec.model_copy(
        update={"policy_version": " i6-thumbnail-render-v1 "}
    )
    invalid_fit = spec.model_copy(
        update={
            "visual_bindings": (
                spec.visual_bindings[0].model_copy(update={"fit_mode": " cover "}),
            )
        }
    )

    with pytest.raises(ThumbnailRenderError, match="runtime validation"):
        render_thumbnail(layout, invalid_policy, {source_sha256: source})
    with pytest.raises(ThumbnailRenderError, match="runtime validation"):
        render_thumbnail(layout, invalid_fit, {source_sha256: source})


def test_runtime_rejects_model_copy_concept_id_canonicalization_drift() -> None:
    source = _png((20, 80, 160))
    source_sha256 = hashlib.sha256(source).hexdigest()
    layout = _layout()
    noncanonical = _spec(layout, source_sha256).model_copy(
        update={"concept_id": " Concept 0 "}
    )

    with pytest.raises(
        ThumbnailRenderError,
        match="thumbnail render spec changes during runtime validation",
    ):
        render_thumbnail(layout, noncanonical, {source_sha256: source})


def test_runtime_rejects_model_copy_layout_with_negative_coordinates() -> None:
    source = _png((20, 80, 160))
    source_sha256 = hashlib.sha256(source).hexdigest()
    layout = _layout()
    invalid_bounds = layout.visual_elements[0].bounds.model_copy(update={"x": -1})
    invalid_layout = layout.model_copy(
        update={
            "visual_elements": (
                layout.visual_elements[0].model_copy(update={"bounds": invalid_bounds}),
            )
        }
    )
    invalid_layout = invalid_layout.model_copy(
        update={"layout_spec_sha256": canonical_sha256(invalid_layout.hash_payload())}
    )

    with pytest.raises(ThumbnailRenderError, match="layout fails runtime validation"):
        render_thumbnail(
            invalid_layout,
            _spec(invalid_layout, source_sha256),
            {source_sha256: source},
        )


def test_runtime_rejects_noncanonical_binding_order_and_source_hash_mismatch() -> None:
    layout = _visual_layout(count=2)
    first = _png((20, 80, 160))
    second = _png((160, 40, 20))
    hashes = tuple(hashlib.sha256(source).hexdigest() for source in (first, second))
    spec = _multi_spec(layout, hashes)
    reordered = spec.model_copy(update={"visual_bindings": tuple(reversed(spec.visual_bindings))})

    with pytest.raises(ThumbnailRenderError, match="runtime validation"):
        render_thumbnail(layout, reordered, dict(zip(hashes, (first, second), strict=True)))
    with pytest.raises(ThumbnailRenderError, match="SHA"):
        render_thumbnail(_layout(), _spec(_layout(), hashes[0]), {hashes[0]: second})


def test_alpha_compositing_preserves_opaque_transparent_and_partial_pixels() -> None:
    layout = _visual_layout()
    background = (16, 24, 36)
    for alpha, expected in (
        (255, (200, 100, 50)),
        (0, background),
        (
            128,
            tuple((source * 128 + destination * 127 + 127) // 255 for source, destination in zip((200, 100, 50), background, strict=True)),
        ),
    ):
        source = _png((200, 100, 50), alpha=alpha)
        source_sha256 = hashlib.sha256(source).hexdigest()
        result = render_thumbnail(layout, _spec(layout, source_sha256), {source_sha256: source})

        assert _pixel(result.png_bytes) == expected


def test_overlapping_visuals_compose_in_layout_order() -> None:
    layout = _visual_layout(count=2)
    bottom = _png((200, 0, 0), alpha=255)
    top = _png((0, 200, 0), alpha=128)
    hashes = tuple(hashlib.sha256(source).hexdigest() for source in (bottom, top))
    result = render_thumbnail(layout, _multi_spec(layout, hashes), dict(zip(hashes, (bottom, top), strict=True)))

    assert _pixel(result.png_bytes) == (100, 100, 0)


def test_unsupported_text_and_outline_escape_fail_closed() -> None:
    source = _png((20, 80, 160))
    source_sha256 = hashlib.sha256(source).hexdigest()
    unsupported = _layout("A☃")
    text = _layout("A").text_elements[0].model_copy(
        update={"bounds": Rectangle(x=40, y=40, width=34, height=48)}
    )
    raw = _layout("A").model_dump(mode="json")
    raw["text_elements"] = [text.model_dump(mode="json")]
    raw.pop("layout_spec_sha256")
    outline_escape = ThumbnailLayout(**raw, layout_spec_sha256=canonical_sha256(raw))

    with pytest.raises(ThumbnailRenderError, match="unsupported deterministic glyph"):
        render_thumbnail(unsupported, _spec(unsupported, source_sha256), {source_sha256: source})
    with pytest.raises(ThumbnailRenderError, match="text cannot fit"):
        render_thumbnail(
            outline_escape,
            _spec(outline_escape, source_sha256),
            {source_sha256: source},
        )


def test_supported_deterministic_punctuation_renders_byte_identically() -> None:
    source = _png((20, 80, 160))
    source_sha256 = hashlib.sha256(source).hexdigest()
    layout = _visual_layout(text='_/<>=|"()%')
    spec = _spec(layout, source_sha256)

    first = render_thumbnail(layout, spec, {source_sha256: source})
    second = render_thumbnail(layout, spec, {source_sha256: source})

    assert first.png_bytes == second.png_bytes


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    candidates = (
        (abs(estimate - left), left),
        (abs(estimate - above), above),
        (abs(estimate - upper_left), upper_left),
    )
    return min(candidates, key=lambda candidate: candidate[0])[1]


def _filtered_png(filter_type: int) -> tuple[bytes, bytes]:
    width, height, channels = 3, 2, 3
    rows = (bytes((10, 20, 30, 40, 50, 60, 70, 80, 90)), bytes((15, 25, 35, 45, 55, 65, 75, 85, 95)))
    filtered = bytearray()
    previous = bytes(width * channels)
    for row in rows:
        filtered.append(filter_type)
        for index, value in enumerate(row):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            predictor = (
                0
                if filter_type == 0
                else left
                if filter_type == 1
                else above
                if filter_type == 2
                else (left + above) // 2
                if filter_type == 3
                else _paeth(left, above, upper_left)
            )
            filtered.append((value - predictor) & 0xFF)
        previous = row
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(filtered)))
        + _chunk(b"IEND", b""),
        b"".join(rows),
    )


@pytest.mark.parametrize("filter_type", (0, 1, 2, 3, 4))
def test_decoder_supports_all_png_row_filters(filter_type: int) -> None:
    png_bytes, expected = _filtered_png(filter_type)

    assert decode_png(png_bytes).rgb_bytes == expected


def _png_with_chunks(*, before_idat: tuple[bytes, ...] = (), after_idat: tuple[bytes, ...] = ()) -> bytes:
    raw = b"\x00" + bytes((1, 2, 3))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + b"".join(before_idat)
        + _chunk(b"IDAT", zlib.compress(raw))
        + b"".join(after_idat)
        + _chunk(b"IEND", b"")
    )


def test_decoder_rejects_invalid_plte_reserved_bit_and_oversized_input() -> None:
    plte = _chunk(b"PLTE", bytes((0, 0, 0)))
    duplicate = _png_with_chunks(before_idat=(plte, plte))
    after_idat = _png_with_chunks(after_idat=(plte,))
    reserved_bit = _png_with_chunks(before_idat=(_chunk(b"abca", b""),))

    with pytest.raises(ThumbnailRenderError, match="PLTE"):
        decode_png(duplicate)
    with pytest.raises(ThumbnailRenderError, match="PLTE"):
        decode_png(after_idat)
    with pytest.raises(ThumbnailRenderError, match="reserved"):
        decode_png(reserved_bit)
    with pytest.raises(ThumbnailRenderError, match="input exceeds"):
        decode_png(b"\x89PNG\r\n\x1a\n" + b"x" * thumbnail_renderer._MAX_PNG_INPUT_BYTES)


@pytest.mark.parametrize(
    "color_type, transparency",
    (
        (2, b"\x00\x00\x00\x00\x00\x00"),
        (6, b""),
    ),
)
def test_decoder_rejects_trns_for_rgb_and_rgba(
    color_type: int, transparency: bytes
) -> None:
    channels = 3 if color_type == 2 else 4
    raw = b"\x00" + bytes((1, 2, 3, 255)[:channels])
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, color_type, 0, 0, 0))
        + _chunk(b"tRNS", transparency)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )

    with pytest.raises(ThumbnailRenderError, match="tRNS"):
        decode_png(png_bytes)
