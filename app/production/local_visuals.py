from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import zlib


I5_LOCAL_VISUAL_WIDTH = 1_280
I5_LOCAL_VISUAL_HEIGHT = 720
I5_LOCAL_VISUAL_RENDERER_VERSION = "i5-local-visuals-v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SPACE = re.compile(r"\s+")

Color = tuple[int, int, int]


class VisualMode(StrEnum):
    DOCUMENT = "DOCUMENT"
    DIAGRAM = "DIAGRAM"
    DATA_VISUALIZATION = "DATA_VISUALIZATION"
    TIMELINE = "TIMELINE"
    CODE = "CODE"
    UI_RECONSTRUCTION = "UI_RECONSTRUCTION"
    KINETIC_TEXT = "KINETIC_TEXT"
    DETERMINISTIC_MOTION_GRAPHIC = "DETERMINISTIC_MOTION_GRAPHIC"


@dataclass(frozen=True, slots=True)
class DataPoint:
    label: str
    value: float
    display_value: str

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.display_value.strip():
            raise ValueError("verified data points require labels and display values")
        if not math.isfinite(self.value):
            raise ValueError("verified data point values must be finite")


@dataclass(frozen=True, slots=True)
class LocalVisualRequest:
    mode: VisualMode | str
    title: str
    scene_position: int
    visual_purpose: str
    factual_overlay: str = ""
    claim_hashes: tuple[str, ...] = ()
    source_keys: tuple[str, ...] = ()
    detail_lines: tuple[str, ...] = ()
    data_points: tuple[DataPoint, ...] = ()
    rights_basis: str = "original_deterministic"
    disclosure_state: str = "deterministic_visualization"

    def __post_init__(self) -> None:
        try:
            mode = VisualMode(self.mode)
        except ValueError:
            raise ValueError("unsupported deterministic visual mode") from None
        object.__setattr__(self, "mode", mode)
        if self.scene_position < 0:
            raise ValueError("scene position must be non-negative")
        if not _clean(self.title) or not _clean(self.visual_purpose):
            raise ValueError("local visuals require a title and visual purpose")
        if len(self.title) > 180 or len(self.visual_purpose) > 240:
            raise ValueError("local visual text exceeds its canonical bound")
        if len(self.factual_overlay) > 320:
            raise ValueError("factual overlay exceeds its canonical bound")
        if len(self.detail_lines) > 8 or any(len(line) > 160 for line in self.detail_lines):
            raise ValueError("local visual detail lines exceed their canonical bounds")
        if len(set(self.claim_hashes)) != len(self.claim_hashes) or any(
            _SHA256.fullmatch(value) is None for value in self.claim_hashes
        ):
            raise ValueError("claim lineage must contain unique full SHA-256 values")
        if len(set(self.source_keys)) != len(self.source_keys) or any(
            not _clean(value) for value in self.source_keys
        ):
            raise ValueError("source lineage must contain unique nonblank keys")
        if self.factual_overlay.strip() and not self.claim_hashes:
            raise ValueError("factual overlays require claim-hash lineage")
        if mode == VisualMode.DATA_VISUALIZATION:
            if not self.data_points:
                raise ValueError(
                    "data visualization requires supplied verified numeric data"
                )
            if not self.claim_hashes:
                raise ValueError("verified data visualization requires claim lineage")
        elif self.data_points:
            raise ValueError("numeric data points belong only to DATA_VISUALIZATION")
        if self.rights_basis not in {
            "original_deterministic",
            "reference_only",
            "owned",
            "licensed",
            "public_domain",
        }:
            raise ValueError("local visual rights basis is invalid")
        if not _clean(self.disclosure_state):
            raise ValueError("local visual disclosure state must be nonblank")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "claim_hashes": list(self.claim_hashes),
            "contract_version": "i5-local-visual-request-v1",
            "data_points": [
                {
                    "display_value": point.display_value,
                    "label": point.label,
                    "value": point.value,
                }
                for point in self.data_points
            ],
            "detail_lines": list(self.detail_lines),
            "disclosure_state": self.disclosure_state,
            "factual_overlay": self.factual_overlay,
            "mode": str(self.mode),
            "renderer_version": I5_LOCAL_VISUAL_RENDERER_VERSION,
            "rights_basis": self.rights_basis,
            "scene_position": self.scene_position,
            "source_keys": list(self.source_keys),
            "title": self.title,
            "visual_purpose": self.visual_purpose,
        }


@dataclass(frozen=True, slots=True)
class LocalVisualResult:
    png_bytes: bytes
    sha256: str
    request_sha256: str
    mode: str
    width: int = I5_LOCAL_VISUAL_WIDTH
    height: int = I5_LOCAL_VISUAL_HEIGHT
    renderer_version: str = I5_LOCAL_VISUAL_RENDERER_VERSION

    def metadata(self) -> dict[str, object]:
        return {
            "height": self.height,
            "mode": self.mode,
            "renderer_version": self.renderer_version,
            "request_sha256": self.request_sha256,
            "sha256": self.sha256,
            "width": self.width,
        }


def render_local_visual(request: LocalVisualRequest) -> LocalVisualResult:
    payload_bytes = json.dumps(
        request.canonical_payload(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    request_sha = hashlib.sha256(payload_bytes).hexdigest()
    palette = _palette_for(VisualMode(request.mode))
    canvas = _Canvas(
        I5_LOCAL_VISUAL_WIDTH,
        I5_LOCAL_VISUAL_HEIGHT,
        palette["background"],
    )
    _draw_frame(canvas, request, palette)
    treatment = {
        VisualMode.DOCUMENT: _draw_document,
        VisualMode.DIAGRAM: _draw_diagram,
        VisualMode.DATA_VISUALIZATION: _draw_data_visualization,
        VisualMode.TIMELINE: _draw_timeline,
        VisualMode.CODE: _draw_code,
        VisualMode.UI_RECONSTRUCTION: _draw_ui_reconstruction,
        VisualMode.KINETIC_TEXT: _draw_kinetic_text,
        VisualMode.DETERMINISTIC_MOTION_GRAPHIC: _draw_motion_graphic,
    }[VisualMode(request.mode)]
    treatment(canvas, request, palette, request_sha)
    _draw_lineage(canvas, request, palette)
    png_bytes = _encode_png(canvas)
    return LocalVisualResult(
        png_bytes=png_bytes,
        sha256=hashlib.sha256(png_bytes).hexdigest(),
        request_sha256=request_sha,
        mode=str(request.mode),
    )


def render_local_visual_png(request: LocalVisualRequest) -> bytes:
    return render_local_visual(request).png_bytes


def write_local_visual(path: Path, request: LocalVisualRequest) -> LocalVisualResult:
    result = render_local_visual(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.png_bytes)
    return result


def png_dimensions(content: bytes) -> tuple[int, int] | None:
    if (
        len(content) < 24
        or content[:8] != b"\x89PNG\r\n\x1a\n"
        or content[12:16] != b"IHDR"
    ):
        return None
    return struct.unpack(">II", content[16:24])


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _palette_for(mode: VisualMode) -> dict[str, Color]:
    palettes: dict[VisualMode, tuple[Color, Color, Color, Color, Color]] = {
        VisualMode.DOCUMENT: (
            (14, 24, 39),
            (241, 245, 249),
            (245, 158, 11),
            (15, 23, 42),
            (148, 163, 184),
        ),
        VisualMode.DIAGRAM: (
            (8, 24, 33),
            (20, 184, 166),
            (45, 212, 191),
            (236, 254, 255),
            (94, 234, 212),
        ),
        VisualMode.DATA_VISUALIZATION: (
            (15, 23, 42),
            (56, 189, 248),
            (129, 140, 248),
            (240, 249, 255),
            (125, 211, 252),
        ),
        VisualMode.TIMELINE: (
            (30, 20, 55),
            (168, 85, 247),
            (216, 180, 254),
            (250, 245, 255),
            (192, 132, 252),
        ),
        VisualMode.CODE: (
            (8, 15, 24),
            (34, 197, 94),
            (74, 222, 128),
            (220, 252, 231),
            (134, 239, 172),
        ),
        VisualMode.UI_RECONSTRUCTION: (
            (20, 28, 45),
            (59, 130, 246),
            (147, 197, 253),
            (239, 246, 255),
            (96, 165, 250),
        ),
        VisualMode.KINETIC_TEXT: (
            (45, 15, 25),
            (244, 63, 94),
            (251, 113, 133),
            (255, 241, 242),
            (253, 164, 175),
        ),
        VisualMode.DETERMINISTIC_MOTION_GRAPHIC: (
            (7, 20, 34),
            (6, 182, 212),
            (34, 211, 238),
            (236, 254, 255),
            (103, 232, 249),
        ),
    }
    background, primary, accent, text, muted = palettes[mode]
    return {
        "accent": accent,
        "background": background,
        "muted": muted,
        "primary": primary,
        "text": text,
    }


def _draw_frame(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
) -> None:
    canvas.rect(0, 0, canvas.width, 12, palette["primary"])
    canvas.text(str(request.mode).replace("_", " "), 56, 34, 3, palette["accent"])
    scene_label = f"SCENE {request.scene_position + 1:02d}"
    canvas.text(scene_label, 1_218 - _text_width(scene_label, 3), 34, 3, palette["muted"])
    title_lines = _wrap(_clean(request.title).upper(), 38, 2)
    scale = 6 if len(title_lines) == 1 and len(title_lines[0]) <= 30 else 5
    y = 82
    for line in title_lines:
        canvas.text(line, 56, y, scale, palette["text"])
        y += 8 * scale
    purpose = _wrap(_clean(request.visual_purpose), 72, 2)
    y = max(y + 8, 182)
    for line in purpose:
        canvas.text(line, 58, y, 2, palette["muted"])
        y += 20


def _draw_document(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
    _: str,
) -> None:
    canvas.rect(128, 246, 1_024, 300, palette["primary"])
    canvas.rect(144, 262, 992, 268, (250, 250, 248))
    canvas.text("EVIDENCE NOTE", 178, 286, 3, (15, 23, 42))
    for index in range(5):
        width = 760 if index != 4 else 520
        canvas.rect(178, 332 + index * 32, width, 6, (148, 163, 184))
    overlay = _clean(request.factual_overlay) or "CLAIM-BOUND CONTEXT, NOT SOURCE MEDIA"
    for index, line in enumerate(_wrap(overlay.upper(), 58, 3)):
        canvas.text(line, 178, 354 + index * 30, 3, (15, 23, 42))


def _draw_diagram(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
    _: str,
) -> None:
    labels = list(request.detail_lines[:3]) or ["CONTEXT", "MECHANISM", "OUTCOME"]
    while len(labels) < 3:
        labels.append(("MECHANISM", "OUTCOME")[len(labels) - 1])
    x_positions = (80, 470, 860)
    for index, (x, label) in enumerate(zip(x_positions, labels, strict=True)):
        canvas.rect(x, 300, 330, 150, palette["primary"] if index == 1 else (21, 53, 64))
        canvas.rect(x + 5, 305, 320, 140, canvas.blend(palette["background"], palette["primary"], 0.18))
        lines = _wrap(_clean(label).upper(), 17, 3)
        for line_index, line in enumerate(lines):
            canvas.text(line, x + 26, 340 + line_index * 30, 3, palette["text"])
        if index < 2:
            canvas.line(x + 330, 375, x + 390, 375, 8, palette["accent"])
            canvas.line(x + 378, 362, x + 390, 375, 5, palette["accent"])
            canvas.line(x + 378, 388, x + 390, 375, 5, palette["accent"])


def _draw_data_visualization(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
    _: str,
) -> None:
    points = request.data_points[:6]
    values = [point.value for point in points]
    low = min(0.0, min(values))
    high = max(values)
    span = high - low or 1.0
    chart_x, chart_y, chart_w, chart_h = 100, 272, 1_080, 260
    canvas.line(chart_x, chart_y + chart_h, chart_x + chart_w, chart_y + chart_h, 4, palette["muted"])
    slot = chart_w // len(points)
    for index, point in enumerate(points):
        normalized = max(0.04, (point.value - low) / span)
        height = int((chart_h - 55) * normalized)
        x = chart_x + index * slot + 22
        bar_w = max(28, slot - 44)
        canvas.rect(x, chart_y + chart_h - height, bar_w, height, palette["primary"] if index % 2 == 0 else palette["accent"])
        label = _clean(point.label).upper()[:12]
        display = _clean(point.display_value).upper()[:14]
        canvas.text(display, x, chart_y + chart_h - height - 28, 2, palette["text"])
        canvas.text(label, x, chart_y + chart_h + 14, 2, palette["muted"])
    canvas.text("VERIFIED VALUES SUPPLIED BY CLAIM LINEAGE", 100, 566, 2, palette["accent"])


def _draw_timeline(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
    _: str,
) -> None:
    items = list(request.detail_lines[:5]) or ["ORIGIN", "CHANGE", "CONSEQUENCE"]
    y = 392
    canvas.line(120, y, 1_160, y, 7, palette["primary"])
    spacing = 1_040 // max(1, len(items) - 1)
    for index, item in enumerate(items):
        x = 120 + index * spacing if len(items) > 1 else 640
        canvas.circle(x, y, 22, palette["accent"])
        canvas.circle(x, y, 10, palette["background"])
        lines = _wrap(_clean(item).upper(), 15, 2)
        for line_index, line in enumerate(lines):
            canvas.text(line, x - _text_width(line, 2) // 2, y + 45 + line_index * 20, 2, palette["text"])


def _draw_code(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
    _: str,
) -> None:
    canvas.rect(92, 246, 1_096, 320, (3, 10, 18))
    canvas.rect(92, 246, 1_096, 38, (17, 31, 45))
    canvas.text("ILLUSTRATIVE PSEUDOCODE", 118, 257, 2, palette["accent"])
    lines = request.detail_lines[:7] or (
        "INPUT  <- VERIFIED CONTEXT",
        "CHECK  <- SOURCE LINEAGE",
        "OUTPUT <- BOUNDED CONCLUSION",
    )
    for index, line in enumerate(lines):
        prefix = f"{index + 1:02d}  "
        canvas.text(prefix + _clean(line).upper()[:62], 122, 310 + index * 34, 2, palette["text"])


def _draw_ui_reconstruction(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
    _: str,
) -> None:
    canvas.rect(72, 240, 1_136, 56, palette["accent"])
    label = "UI RECONSTRUCTION - NOT A SCREENSHOT"
    canvas.text(label, 640 - _text_width(label, 3) // 2, 258, 3, (10, 20, 35))
    canvas.rect(72, 306, 1_136, 260, (226, 232, 240))
    canvas.rect(92, 326, 220, 220, (203, 213, 225))
    for index in range(5):
        canvas.rect(112, 350 + index * 36, 150, 12, (100, 116, 139))
    canvas.rect(338, 326, 390, 96, (248, 250, 252))
    canvas.rect(752, 326, 432, 96, (248, 250, 252))
    canvas.rect(338, 446, 846, 100, (248, 250, 252))
    canvas.text("CONCEPTUAL LAYOUT", 382, 360, 2, (51, 65, 85))
    canvas.text("NO PRODUCT UI CLAIM", 796, 360, 2, (51, 65, 85))


def _draw_kinetic_text(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
    request_sha: str,
) -> None:
    for index in range(7):
        offset = int(request_sha[index * 2 : index * 2 + 2], 16)
        x = 50 + index * 185 + offset % 70
        canvas.rect(x, 274 + (index % 3) * 76, 112, 14, palette["primary"] if index % 2 else palette["accent"])
    words = _wrap((_clean(request.factual_overlay) or _clean(request.title)).upper(), 25, 3)
    y = 316
    for line in words:
        scale = 6
        canvas.text(line, 640 - _text_width(line, scale) // 2, y, scale, palette["text"])
        y += 66


def _draw_motion_graphic(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
    request_sha: str,
) -> None:
    center_x, center_y = 640, 392
    for ring_index, radius in enumerate((180, 140, 100, 60)):
        color = palette["primary"] if ring_index % 2 else palette["accent"]
        for degree in range(0, 360, 6):
            if (degree // 6 + int(request_sha[ring_index], 16)) % 4 == 0:
                continue
            radians = math.radians(degree)
            x = center_x + int(math.cos(radians) * radius)
            y = center_y + int(math.sin(radians) * radius * 0.65)
            canvas.rect(x - 3, y - 3, 7, 7, color)
    canvas.circle(center_x, center_y, 44, palette["accent"])
    canvas.text("CONCEPT", center_x - _text_width("CONCEPT", 2) // 2, center_y - 8, 2, palette["background"])


def _draw_lineage(
    canvas: _Canvas,
    request: LocalVisualRequest,
    palette: dict[str, Color],
) -> None:
    if request.factual_overlay.strip() and VisualMode(request.mode) not in {
        VisualMode.DOCUMENT,
        VisualMode.KINETIC_TEXT,
    }:
        overlay = _wrap(_clean(request.factual_overlay).upper(), 72, 2)
        canvas.rect(56, 574, 1_168, 54, canvas.blend(palette["background"], palette["primary"], 0.22))
        for index, line in enumerate(overlay):
            canvas.text(line, 76, 588 + index * 20, 2, palette["text"])
    lineage = (
        "CLAIMS " + ",".join(value[:8] for value in request.claim_hashes)
        if request.claim_hashes
        else "NO FACTUAL OVERLAY"
    )
    sources = (
        " | SOURCES " + ",".join(_clean(value)[:18] for value in request.source_keys)
        if request.source_keys
        else ""
    )
    footer = f"{lineage}{sources} | {request.rights_basis.upper()} | {request.disclosure_state.upper()}"
    canvas.rect(0, 664, canvas.width, 56, (3, 9, 17))
    canvas.text(footer[:145], 42, 684, 2, palette["muted"])


class _Canvas:
    def __init__(self, width: int, height: int, background: Color) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(background) * width * height)

    @staticmethod
    def blend(left: Color, right: Color, amount: float) -> Color:
        return tuple(
            int(a * (1.0 - amount) + b * amount)
            for a, b in zip(left, right, strict=True)
        )

    def rect(self, x: int, y: int, width: int, height: int, color: Color) -> None:
        left = max(0, x)
        top = max(0, y)
        right = min(self.width, x + width)
        bottom = min(self.height, y + height)
        if left >= right or top >= bottom:
            return
        row = bytes(color) * (right - left)
        for row_y in range(top, bottom):
            start = (row_y * self.width + left) * 3
            self.pixels[start : start + len(row)] = row

    def line(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        thickness: int,
        color: Color,
    ) -> None:
        dx = abs(x1 - x0)
        sx = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0)
        sy = 1 if y0 < y1 else -1
        error = dx + dy
        radius = max(1, thickness // 2)
        while True:
            self.rect(x0 - radius, y0 - radius, radius * 2 + 1, radius * 2 + 1, color)
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy

    def circle(self, center_x: int, center_y: int, radius: int, color: Color) -> None:
        radius_squared = radius * radius
        for y in range(-radius, radius + 1):
            half_width = int(math.sqrt(max(0, radius_squared - y * y)))
            self.rect(center_x - half_width, center_y + y, half_width * 2 + 1, 1, color)

    def text(self, text: str, x: int, y: int, scale: int, color: Color) -> None:
        cursor = x
        for character in text.upper():
            glyph = _FONT.get(character, _FONT["?"])
            for row_index, row in enumerate(glyph):
                for column_index, bit in enumerate(row):
                    if bit == "1":
                        self.rect(
                            cursor + column_index * scale,
                            y + row_index * scale,
                            scale,
                            scale,
                            color,
                        )
            cursor += 6 * scale


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + len(word) + 1 <= width:
            current += " " + word
        else:
            lines.append(current[:width])
            current = word
            if len(lines) == max_lines - 1:
                break
    if len(lines) < max_lines:
        lines.append(current[:width])
    if len(lines) == max_lines and len(" ".join(words)) > len(" ".join(lines)):
        lines[-1] = lines[-1][: max(0, width - 3)].rstrip() + "..."
    return lines


def _text_width(text: str, scale: int) -> int:
    return len(text) * 6 * scale


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _encode_png(canvas: _Canvas) -> bytes:
    stride = canvas.width * 3
    raw = b"".join(
        b"\x00" + bytes(canvas.pixels[offset : offset + stride])
        for offset in range(0, len(canvas.pixels), stride)
    )
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", canvas.width, canvas.height, 8, 2, 0, 0, 0)
    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
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
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
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
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
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
