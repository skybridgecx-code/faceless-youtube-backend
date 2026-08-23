"""Deterministic, local I6 thumbnail rendering from canonical I5 PNG bytes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
from typing import Mapping
import zlib

from app.editorial.contracts import canonical_sha256
from app.qa.contracts import (
    I6_THUMBNAIL_RENDERER_VERSION,
    ThumbnailLayout,
    ThumbnailRenderSpec,
    revalidate_thumbnail_render_spec,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
THUMBNAIL_WIDTH = 1_280
THUMBNAIL_HEIGHT = 720
_MAX_PNG_DIMENSION = 10_000
_MAX_DECODED_BYTES = 128 * 1024 * 1024
_MAX_PNG_INPUT_BYTES = 16 * 1024 * 1024


class ThumbnailRenderError(ValueError):
    """The supplied render input cannot produce trustworthy pixels."""


@dataclass(frozen=True, slots=True)
class DecodedPNG:
    width: int
    height: int
    rgb_bytes: bytes
    rgba_bytes: bytes


@dataclass(frozen=True, slots=True)
class ThumbnailRenderResult:
    png_bytes: bytes
    png_sha256: str
    width: int = THUMBNAIL_WIDTH
    height: int = THUMBNAIL_HEIGHT
    mime_type: str = "image/png"
    renderer_version: str = I6_THUMBNAIL_RENDERER_VERSION


def decode_png(png_bytes: bytes) -> DecodedPNG:
    """Decode bounded, non-interlaced 8-bit RGB/RGBA PNG bytes fail-closed."""

    if not isinstance(png_bytes, bytes):
        raise ThumbnailRenderError("PNG bytes are invalid")
    if len(png_bytes) > _MAX_PNG_INPUT_BYTES:
        raise ThumbnailRenderError("PNG input exceeds the bound")
    if not png_bytes.startswith(PNG_SIGNATURE):
        raise ThumbnailRenderError("PNG signature is invalid")

    cursor = len(PNG_SIGNATURE)
    ihdr: tuple[int, int, int] | None = None
    idat_parts: list[bytes] = []
    saw_idat = False
    idat_finished = False
    saw_iend = False
    saw_plte = False

    while cursor < len(png_bytes):
        if len(png_bytes) - cursor < 12:
            raise ThumbnailRenderError("PNG chunk structure is truncated")
        length = struct.unpack(">I", png_bytes[cursor : cursor + 4])[0]
        chunk_type = png_bytes[cursor + 4 : cursor + 8]
        payload_start = cursor + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if payload_end < payload_start or crc_end > len(png_bytes):
            raise ThumbnailRenderError("PNG chunk length is invalid")
        payload = png_bytes[payload_start:payload_end]
        expected_crc = struct.unpack(">I", png_bytes[payload_end:crc_end])[0]
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
            raise ThumbnailRenderError("PNG chunk CRC is invalid")
        if len(chunk_type) != 4 or not all(
            65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type
        ):
            raise ThumbnailRenderError("PNG chunk type is invalid")
        if not 65 <= chunk_type[2] <= 90:
            raise ThumbnailRenderError("PNG chunk reserved bit is invalid")
        if saw_iend:
            raise ThumbnailRenderError("PNG contains bytes after IEND")

        if chunk_type == b"IHDR":
            if ihdr is not None or cursor != len(PNG_SIGNATURE) or length != 13:
                raise ThumbnailRenderError("PNG IHDR is invalid")
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            if (
                width <= 0
                or height <= 0
                or width > _MAX_PNG_DIMENSION
                or height > _MAX_PNG_DIMENSION
                or bit_depth != 8
                or color_type not in {2, 6}
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise ThumbnailRenderError("PNG form is unsupported")
            channels = 3 if color_type == 2 else 4
            if height * (1 + width * channels) > _MAX_DECODED_BYTES:
                raise ThumbnailRenderError("PNG decoded data exceeds the bound")
            ihdr = (width, height, channels)
        elif chunk_type == b"tRNS":
            raise ThumbnailRenderError("PNG tRNS is unsupported")
        elif chunk_type == b"PLTE":
            if (
                ihdr is None
                or saw_plte
                or saw_idat
                or ihdr[2] not in {3, 4}
                or length == 0
                or length > 768
                or length % 3
            ):
                raise ThumbnailRenderError("PNG PLTE is invalid")
            saw_plte = True
        elif chunk_type == b"IDAT":
            if ihdr is None or idat_finished:
                raise ThumbnailRenderError("PNG IDAT ordering is invalid")
            saw_idat = True
            idat_parts.append(payload)
        elif chunk_type == b"IEND":
            if ihdr is None or not saw_idat or length != 0:
                raise ThumbnailRenderError("PNG IEND is invalid")
            saw_iend = True
            cursor = crc_end
            if cursor != len(png_bytes):
                raise ThumbnailRenderError("PNG contains trailing bytes")
            break
        else:
            if ihdr is None:
                raise ThumbnailRenderError("PNG IHDR must be first")
            if saw_idat:
                idat_finished = True
            if 65 <= chunk_type[0] <= 90:
                raise ThumbnailRenderError("PNG critical chunk is unsupported")
        cursor = crc_end

    if ihdr is None or not saw_iend or cursor != len(png_bytes):
        raise ThumbnailRenderError("PNG is missing IEND")
    width, height, channels = ihdr
    raw = _decompress_png_data(b"".join(idat_parts), height * (1 + width * channels))
    rows = _unfilter_png_rows(raw, width, height, channels)
    if channels == 3:
        return DecodedPNG(
            width=width,
            height=height,
            rgb_bytes=rows,
            rgba_bytes=_opaque_rgba(rows),
        )
    return DecodedPNG(
        width=width,
        height=height,
        rgb_bytes=_rgba_to_rgb(rows),
        rgba_bytes=rows,
    )


def render_thumbnail(
    layout: ThumbnailLayout,
    render_spec: ThumbnailRenderSpec,
    source_png_bytes: Mapping[str, bytes],
) -> ThumbnailRenderResult:
    """Render exact local PNG bytes for a validated layout and render spec."""

    layout = _revalidate_thumbnail_layout(layout)
    render_spec = _validate_render_contract(layout, render_spec, source_png_bytes)
    canvas = bytearray(b"\x10\x18\x24" * THUMBNAIL_WIDTH * THUMBNAIL_HEIGHT)
    bindings = {binding.visual_index: binding for binding in render_spec.visual_bindings}
    for index, element in enumerate(layout.visual_elements):
        binding = bindings[index]
        source = decode_png(source_png_bytes[binding.source_artifact_sha256])
        destination = _scaled_bounds(
            element.bounds.x,
            element.bounds.y,
            element.bounds.width,
            element.bounds.height,
            layout.canvas_width_px,
            layout.canvas_height_px,
        )
        _composite_cover(canvas, source, destination)

    for element in layout.text_elements:
        destination = _scaled_bounds(
            element.bounds.x,
            element.bounds.y,
            element.bounds.width,
            element.bounds.height,
            layout.canvas_width_px,
            layout.canvas_height_px,
        )
        font_height = element.font_height_px * THUMBNAIL_HEIGHT // layout.canvas_height_px
        _draw_text(canvas, element.text, destination, font_height)

    png_bytes = _encode_rgb_png(bytes(canvas), THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
    return ThumbnailRenderResult(
        png_bytes=png_bytes,
        png_sha256=hashlib.sha256(png_bytes).hexdigest(),
    )


def _validate_render_contract(
    layout: ThumbnailLayout,
    render_spec: ThumbnailRenderSpec,
    source_png_bytes: Mapping[str, bytes],
) -> ThumbnailRenderSpec:
    try:
        render_spec = revalidate_thumbnail_render_spec(render_spec)
    except ValueError as exc:
        raise ThumbnailRenderError(str(exc)) from exc
    if render_spec.renderer_version != I6_THUMBNAIL_RENDERER_VERSION:
        raise ThumbnailRenderError("renderer version is not supported")
    if render_spec.concept_id != layout.concept_id:
        raise ThumbnailRenderError("render spec concept does not match layout")
    layout_sha256 = canonical_sha256(layout.hash_payload())
    if layout.layout_spec_sha256 != layout_sha256:
        raise ThumbnailRenderError("layout hash is invalid")
    if render_spec.layout_spec_sha256 != layout_sha256:
        raise ThumbnailRenderError("render spec layout hash is stale")
    _validate_layout_bounds(layout)
    expected_indexes = set(range(len(layout.visual_elements)))
    indexes = tuple(binding.visual_index for binding in render_spec.visual_bindings)
    if indexes != tuple(sorted(expected_indexes)):
        raise ThumbnailRenderError("visual bindings do not exactly cover layout slots")
    source_hashes = {binding.source_artifact_sha256 for binding in render_spec.visual_bindings}
    if set(source_png_bytes) != source_hashes:
        raise ThumbnailRenderError("source PNG bytes do not exactly match render bindings")
    if any(not isinstance(value, bytes) for value in source_png_bytes.values()):
        raise ThumbnailRenderError("source PNG values must be immutable bytes")
    for binding in render_spec.visual_bindings:
        source_bytes = source_png_bytes[binding.source_artifact_sha256]
        if hashlib.sha256(source_bytes).hexdigest() != binding.source_artifact_sha256:
            raise ThumbnailRenderError("source PNG bytes do not match render binding SHA")
    return render_spec


def _revalidate_thumbnail_layout(layout: ThumbnailLayout) -> ThumbnailLayout:
    """Re-establish the external-input boundary after model_copy mutations."""

    try:
        revalidated = ThumbnailLayout.model_validate(layout.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ThumbnailRenderError("thumbnail layout fails runtime validation") from exc
    if revalidated != layout:
        raise ThumbnailRenderError("thumbnail layout changes during runtime validation")
    return revalidated


def _validate_layout_bounds(layout: ThumbnailLayout) -> None:
    for element in (*layout.text_elements, *layout.visual_elements):
        bounds = element.bounds
        if (
            bounds.x + bounds.width > layout.canvas_width_px
            or bounds.y + bounds.height > layout.canvas_height_px
        ):
            raise ThumbnailRenderError("layout bounds exceed its canvas")


def _scaled_bounds(
    x: int,
    y: int,
    width: int,
    height: int,
    canvas_width: int,
    canvas_height: int,
) -> tuple[int, int, int, int]:
    left = x * THUMBNAIL_WIDTH // canvas_width
    top = y * THUMBNAIL_HEIGHT // canvas_height
    right = (x + width) * THUMBNAIL_WIDTH // canvas_width
    bottom = (y + height) * THUMBNAIL_HEIGHT // canvas_height
    if right <= left or bottom <= top:
        raise ThumbnailRenderError("layout bounds cannot map to output pixels")
    return left, top, right - left, bottom - top


def _composite_cover(
    canvas: bytearray,
    source: DecodedPNG,
    destination: tuple[int, int, int, int],
) -> None:
    target_x, target_y, target_width, target_height = destination
    if source.width * target_height > source.height * target_width:
        crop_height = source.height
        crop_width = source.height * target_width // target_height
        crop_x = (source.width - crop_width) // 2
        crop_y = 0
    else:
        crop_width = source.width
        crop_height = source.width * target_height // target_width
        crop_x = 0
        crop_y = (source.height - crop_height) // 2
    if crop_width <= 0 or crop_height <= 0:
        raise ThumbnailRenderError("source PNG cannot cover visual bounds")
    for target_row in range(target_height):
        source_y = crop_y + target_row * crop_height // target_height
        for target_column in range(target_width):
            source_x = crop_x + target_column * crop_width // target_width
            source_offset = (source_y * source.width + source_x) * 4
            target_offset = (
                ((target_y + target_row) * THUMBNAIL_WIDTH + target_x + target_column)
                * 3
            )
            alpha = source.rgba_bytes[source_offset + 3]
            for channel in range(3):
                canvas[target_offset + channel] = (
                    source.rgba_bytes[source_offset + channel] * alpha
                    + canvas[target_offset + channel] * (255 - alpha)
                    + 127
                ) // 255


def _draw_text(
    canvas: bytearray,
    text: str,
    destination: tuple[int, int, int, int],
    font_height: int,
) -> None:
    x, y, width, height = destination
    if font_height < 7:
        raise ThumbnailRenderError("text font height cannot fit its bounds")
    glyph_width = max(1, font_height * 5 // 7)
    spacing = max(1, font_height // 7)
    outline = max(1, font_height // 14)
    required_width = len(text) * glyph_width + max(0, len(text) - 1) * spacing
    if required_width + 2 * outline > width or font_height + 2 * outline > height:
        raise ThumbnailRenderError("text cannot fit its declared bounds")
    glyphs = tuple(_glyph(character) for character in text)
    x += outline
    y += outline
    for character_index, glyph in enumerate(glyphs):
        glyph_x = x + character_index * (glyph_width + spacing)
        for row_index, row in enumerate(glyph):
            top = y + row_index * font_height // 7
            bottom = y + (row_index + 1) * font_height // 7
            for column_index, bit in enumerate(row):
                if bit != "1":
                    continue
                left = glyph_x + column_index * glyph_width // 5
                right = glyph_x + (column_index + 1) * glyph_width // 5
                _rect(canvas, left - outline, top - outline, right + outline, bottom + outline, (0, 0, 0))
                _rect(canvas, left, top, right, bottom, (255, 255, 255))


def _rect(
    canvas: bytearray,
    left: int,
    top: int,
    right: int,
    bottom: int,
    color: tuple[int, int, int],
) -> None:
    left = max(0, left)
    top = max(0, top)
    right = min(THUMBNAIL_WIDTH, right)
    bottom = min(THUMBNAIL_HEIGHT, bottom)
    if left >= right or top >= bottom:
        return
    row = bytes(color) * (right - left)
    for pixel_y in range(top, bottom):
        offset = (pixel_y * THUMBNAIL_WIDTH + left) * 3
        canvas[offset : offset + len(row)] = row


def _decompress_png_data(compressed: bytes, expected_size: int) -> bytes:
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected_size + 1)
        if decoder.unconsumed_tail or len(raw) > expected_size:
            raise ThumbnailRenderError("PNG zlib data exceeds expected size")
        raw += decoder.flush(expected_size + 1 - len(raw))
    except zlib.error as exc:
        raise ThumbnailRenderError("PNG zlib data is invalid") from exc
    if (
        not decoder.eof
        or decoder.unused_data
        or len(raw) != expected_size
    ):
        raise ThumbnailRenderError("PNG zlib data has an invalid length")
    return raw


def _unfilter_png_rows(raw: bytes, width: int, height: int, channels: int) -> bytes:
    stride = width * channels
    rows = bytearray(stride * height)
    source_offset = 0
    for row_index in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        filtered = raw[source_offset : source_offset + stride]
        source_offset += stride
        if len(filtered) != stride or filter_type not in {0, 1, 2, 3, 4}:
            raise ThumbnailRenderError("PNG row filter is invalid")
        row_offset = row_index * stride
        previous_offset = row_offset - stride
        for index, value in enumerate(filtered):
            left = rows[row_offset + index - channels] if index >= channels else 0
            above = rows[previous_offset + index] if row_index else 0
            upper_left = (
                rows[previous_offset + index - channels]
                if row_index and index >= channels
                else 0
            )
            if filter_type == 0:
                reconstructed = value
            elif filter_type == 1:
                reconstructed = value + left
            elif filter_type == 2:
                reconstructed = value + above
            elif filter_type == 3:
                reconstructed = value + ((left + above) // 2)
            else:
                reconstructed = value + _paeth(left, above, upper_left)
            rows[row_offset + index] = reconstructed & 0xFF
    return bytes(rows)


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _opaque_rgba(rgb_bytes: bytes) -> bytes:
    rgba = bytearray(len(rgb_bytes) // 3 * 4)
    for source_offset in range(0, len(rgb_bytes), 3):
        target_offset = source_offset // 3 * 4
        rgba[target_offset : target_offset + 3] = rgb_bytes[source_offset : source_offset + 3]
        rgba[target_offset + 3] = 255
    return bytes(rgba)


def _rgba_to_rgb(rgba_bytes: bytes) -> bytes:
    rgb = bytearray(len(rgba_bytes) // 4 * 3)
    for source_offset in range(0, len(rgba_bytes), 4):
        target_offset = source_offset // 4 * 3
        rgb[target_offset : target_offset + 3] = rgba_bytes[source_offset : source_offset + 3]
    return bytes(rgb)


def _encode_rgb_png(rgb_bytes: bytes, width: int, height: int) -> bytes:
    if len(rgb_bytes) != width * height * 3:
        raise ThumbnailRenderError("RGB canvas size is invalid")
    stride = width * 3
    raw = b"".join(
        b"\x00" + rgb_bytes[offset : offset + stride]
        for offset in range(0, len(rgb_bytes), stride)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return PNG_SIGNATURE + _png_chunk(b"IHDR", ihdr) + _png_chunk(
        b"IDAT", _stored_zlib(raw)
    ) + _png_chunk(b"IEND", b"")


def _stored_zlib(raw: bytes) -> bytes:
    result = bytearray(b"\x78\x01")
    for offset in range(0, len(raw), 65_535):
        block = raw[offset : offset + 65_535]
        final = 1 if offset + len(block) == len(raw) else 0
        result.append(final)
        result.extend(struct.pack("<H", len(block)))
        result.extend(struct.pack("<H", 0xFFFF - len(block)))
        result.extend(block)
    result.extend(struct.pack(">I", zlib.adler32(raw) & 0xFFFFFFFF))
    return bytes(result)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


_FONT: dict[str, tuple[str, ...]] = {
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10101", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "10000", "10000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00110", "00100"),
    ":": ("00000", "00110", "00110", "00000", "00110", "00110", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "=": ("00000", "11111", "00000", "11111", "00000", "00000", "00000"),
    "|": ("00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "'": ("00100", "00100", "00000", "00000", "00000", "00000", "00000"),
    '"': ("01010", "01010", "00000", "00000", "00000", "00000", "00000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
}


def _glyph(character: str) -> tuple[str, ...]:
    if character in _FONT:
        return _FONT[character]
    if character.upper() in _FONT:
        return _FONT[character.upper()]
    raise ThumbnailRenderError(f"unsupported deterministic glyph: {character!r}")
