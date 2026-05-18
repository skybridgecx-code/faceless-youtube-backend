from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import PublishingPayload, Video, VisualAssetPlan, VisualGeneratedAsset
from app.services.final_voiceover import final_voiceover_path_for_video
from app.services.visual_asset_review import asset_review_fields

_RENDERER = "ffmpeg"
_FRAME_RATE = "30"
_WIDTH = 1280
_HEIGHT = 720
_TARGET_FILTER = f"scale={_WIDTH}:{_HEIGHT}:force_original_aspect_ratio=increase,crop={_WIDTH}:{_HEIGHT}"


def _export_dir_for_video(video_id: int) -> Path:
    return get_settings().output_path / "final_exports" / f"video_{video_id}"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = value.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_roots() -> list[Path]:
    roots = [get_settings().output_path.resolve()]
    try:
        roots.append(Path.cwd().resolve())
    except OSError:
        pass
    return roots


def _safe_existing_path(raw_path: str) -> Path | None:
    value = str(raw_path or "").strip()
    if not value or "\x00" in value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (get_settings().output_path / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not any(_is_relative_to(candidate, root) for root in _allowed_roots()):
        return None
    if not candidate.is_file():
        return None
    return candidate


def _query_approved_visual_assets(video_id: int, db: Session) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(VisualGeneratedAsset)
            .options(
                selectinload(VisualGeneratedAsset.scene),
                selectinload(VisualGeneratedAsset.generation_job),
            )
            .join(VisualAssetPlan, VisualAssetPlan.id == VisualGeneratedAsset.visual_asset_plan_id)
            .where(
                VisualAssetPlan.video_id == video_id,
                VisualGeneratedAsset.file_exists.is_(True),
            )
            .order_by(VisualGeneratedAsset.created_at.asc())
        )
    )

    assets: list[dict[str, Any]] = []
    for row in rows:
        review_status = str(asset_review_fields(db, row).get("review_status") or "pending")
        if review_status != "approved":
            continue
        safe_path = _safe_existing_path(row.file_path)
        if safe_path is None:
            continue
        scene_number = row.scene.scene_number if row.scene is not None else None
        scene_title = row.scene.scene_title if row.scene is not None else None
        prompt_text = row.generation_job.prompt if row.generation_job is not None else None
        assets.append(
            {
                "asset_id": row.id,
                "visual_scene_id": row.visual_scene_id,
                "scene_number": scene_number,
                "scene_title": scene_title,
                "prompt_text": prompt_text,
                "asset_type": row.asset_type,
                "path": safe_path,
            }
        )

    assets.sort(key=lambda item: ((item.get("scene_number") or 10_000), int(item.get("asset_id") or 0)))
    return assets


def _extract_stderr_excerpt(stderr_text: str, *, max_lines: int = 6, max_chars: int = 500) -> str:
    lines = [line.strip() for line in (stderr_text or "").splitlines() if line.strip()]
    if not lines:
        return "unknown ffmpeg error"
    snippet = " | ".join(lines[-max_lines:])
    return snippet[:max_chars]


def _audio_duration_seconds(voiceover_path: Path) -> float | None:
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return None
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(voiceover_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        value = float((result.stdout or "").strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _write_deterministic_card(path: Path, seed_text: str) -> None:
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    a = (digest[0], digest[1], digest[2])
    b = (digest[3], digest[4], digest[5])
    accent = digest[6]

    width = _WIDTH
    height = _HEIGHT
    card = bytearray()
    for y in range(height):
        blend = y / max(1, height - 1)
        base_r = int(a[0] * (1.0 - blend) + b[0] * blend)
        base_g = int(a[1] * (1.0 - blend) + b[1] * blend)
        base_b = int(a[2] * (1.0 - blend) + b[2] * blend)
        row = bytearray()
        for x in range(width):
            stripe = ((x // 64) + (y // 48) + accent) % 3
            if stripe == 0:
                factor = 1.0
            elif stripe == 1:
                factor = 0.86
            else:
                factor = 0.72
            row.extend(
                (
                    int(base_r * factor),
                    int(base_g * factor),
                    int(base_b * factor),
                )
            )
        card.extend(row)

    with path.open("wb") as handle:
        handle.write(f"P6\n{width} {height}\n255\n".encode("ascii"))
        handle.write(card)


def render_final_video_with_ffmpeg(video: Video, db: Session) -> dict[str, Any]:
    video_id = int(video.id)
    export_dir = _export_dir_for_video(video_id)
    export_dir.mkdir(parents=True, exist_ok=True)

    final_path = export_dir / "final.mp4"
    staged_path = export_dir / "final_no_audio.mp4"
    concat_path = export_dir / "slides_concat.txt"
    render_plan_path = export_dir / "render_plan.json"
    render_command_path = export_dir / "render_command.json"
    manifest_path = export_dir / "final_export_manifest.json"

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return {
            "renderer": _RENDERER,
            "status": "blocked",
            "ffmpeg_available": False,
            "final_export_path": None,
            "manifest_path": None,
            "render_plan_path": None,
            "render_command_path": None,
            "blockers": ["ffmpeg is required for final render"],
            "warnings": [],
        }

    voiceover_path = final_voiceover_path_for_video(video_id).resolve()
    if not voiceover_path.is_file() or voiceover_path.stat().st_size <= 0:
        return {
            "renderer": _RENDERER,
            "status": "blocked",
            "ffmpeg_available": True,
            "final_export_path": None,
            "manifest_path": None,
            "render_plan_path": None,
            "render_command_path": None,
            "blockers": ["Final voiceover file must be generated before final export"],
            "warnings": [],
        }

    approved_assets = _query_approved_visual_assets(video_id, db)
    if not approved_assets:
        return {
            "renderer": _RENDERER,
            "status": "blocked",
            "ffmpeg_available": True,
            "final_export_path": None,
            "manifest_path": None,
            "render_plan_path": None,
            "render_command_path": None,
            "blockers": ["Final visual assets must be approved for production"],
            "warnings": [],
        }

    for artifact in [final_path, staged_path, concat_path]:
        artifact.unlink(missing_ok=True)
    for stale in export_dir.glob("segment_*.mp4"):
        stale.unlink(missing_ok=True)
    for stale in export_dir.glob("render_card_*.ppm"):
        stale.unlink(missing_ok=True)

    audio_seconds = _audio_duration_seconds(voiceover_path)
    if audio_seconds and audio_seconds > 0:
        per_slide_seconds = max(2.5, audio_seconds / max(1, len(approved_assets)))
    else:
        per_slide_seconds = 4.5

    plan_assets: list[dict[str, Any]] = []
    command_log: list[dict[str, Any]] = []
    warnings: list[str] = []
    segment_paths: list[Path] = []

    try:
        for index, asset in enumerate(approved_assets, start=1):
            input_path = Path(asset["path"])
            segment_path = export_dir / f"segment_{index}.mp4"
            seed_source = f"{video.title}|{asset.get('scene_title') or ''}|{asset.get('prompt_text') or ''}|{index}"
            fallback_card_path = export_dir / f"render_card_{index}.ppm"
            fallback_used = False

            command = [
                ffmpeg_bin,
                "-y",
                "-loop",
                "1",
                "-i",
                str(input_path),
                "-t",
                f"{per_slide_seconds:.2f}",
                "-vf",
                _TARGET_FILTER,
                "-r",
                _FRAME_RATE,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(segment_path),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            command_log.append(
                {
                    "step": f"segment_{index}",
                    "command": command,
                    "returncode": result.returncode,
                    "stderr_excerpt": _extract_stderr_excerpt(result.stderr),
                }
            )
            if result.returncode != 0 or not segment_path.exists() or segment_path.stat().st_size <= 0:
                _write_deterministic_card(fallback_card_path, seed_source)
                fallback_used = True
                fallback_command = [
                    ffmpeg_bin,
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    str(fallback_card_path),
                    "-t",
                    f"{per_slide_seconds:.2f}",
                    "-vf",
                    _TARGET_FILTER,
                    "-r",
                    _FRAME_RATE,
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    str(segment_path),
                ]
                fallback_result = subprocess.run(fallback_command, capture_output=True, text=True)
                command_log.append(
                    {
                        "step": f"segment_{index}_fallback",
                        "command": fallback_command,
                        "returncode": fallback_result.returncode,
                        "stderr_excerpt": _extract_stderr_excerpt(fallback_result.stderr),
                    }
                )
                if (
                    fallback_result.returncode != 0
                    or not segment_path.exists()
                    or segment_path.stat().st_size <= 0
                ):
                    final_path.unlink(missing_ok=True)
                    staged_path.unlink(missing_ok=True)
                    return {
                        "renderer": _RENDERER,
                        "status": "blocked",
                        "ffmpeg_available": True,
                        "final_export_path": None,
                        "manifest_path": None,
                        "render_plan_path": None,
                        "render_command_path": None,
                        "blockers": [f"ffmpeg render failed: {_extract_stderr_excerpt(fallback_result.stderr)}"],
                        "warnings": warnings,
                    }
                warnings.append(
                    f"Asset {asset['asset_id']} needed a deterministic fallback card due to render incompatibility."
                )

            segment_paths.append(segment_path)
            plan_assets.append(
                {
                    "asset_id": asset["asset_id"],
                    "asset_type": asset.get("asset_type"),
                    "scene_number": asset.get("scene_number"),
                    "scene_title": asset.get("scene_title"),
                    "source_path": str(input_path),
                    "segment_path": str(segment_path.resolve()),
                    "fallback_used": fallback_used,
                    "fallback_card_path": str(fallback_card_path.resolve()) if fallback_used else None,
                    "duration_seconds": round(per_slide_seconds, 2),
                }
            )

        concat_lines = [f"file '{segment.resolve()}'" for segment in segment_paths]
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        concat_command = [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            str(staged_path),
        ]
        concat_result = subprocess.run(concat_command, capture_output=True, text=True)
        command_log.append(
            {
                "step": "concat_segments",
                "command": concat_command,
                "returncode": concat_result.returncode,
                "stderr_excerpt": _extract_stderr_excerpt(concat_result.stderr),
            }
        )
        if concat_result.returncode != 0 or not staged_path.exists() or staged_path.stat().st_size <= 0:
            final_path.unlink(missing_ok=True)
            staged_path.unlink(missing_ok=True)
            return {
                "renderer": _RENDERER,
                "status": "blocked",
                "ffmpeg_available": True,
                "final_export_path": None,
                "manifest_path": None,
                "render_plan_path": None,
                "render_command_path": None,
                "blockers": [f"ffmpeg render failed: {_extract_stderr_excerpt(concat_result.stderr)}"],
                "warnings": warnings,
            }

        mux_command = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(staged_path),
            "-i",
            str(voiceover_path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(final_path),
        ]
        mux_result = subprocess.run(mux_command, capture_output=True, text=True)
        command_log.append(
            {
                "step": "mux_audio",
                "command": mux_command,
                "returncode": mux_result.returncode,
                "stderr_excerpt": _extract_stderr_excerpt(mux_result.stderr),
            }
        )
        if mux_result.returncode != 0 or not final_path.exists() or final_path.stat().st_size <= 0:
            final_path.unlink(missing_ok=True)
            staged_path.unlink(missing_ok=True)
            return {
                "renderer": _RENDERER,
                "status": "blocked",
                "ffmpeg_available": True,
                "final_export_path": None,
                "manifest_path": None,
                "render_plan_path": None,
                "render_command_path": None,
                "blockers": [f"ffmpeg render failed: {_extract_stderr_excerpt(mux_result.stderr)}"],
                "warnings": warnings,
            }

        payload_row = db.scalar(select(PublishingPayload).where(PublishingPayload.video_id == video_id).limit(1))
        publishing_payload_path = (
            payload_row.payload_path if payload_row is not None and payload_row.payload_path else None
        )

        plan_payload = {
            "video_id": video_id,
            "title": video.title or "",
            "renderer": _RENDERER,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "output_resolution": f"{_WIDTH}x{_HEIGHT}",
            "fps": int(_FRAME_RATE),
            "audio_duration_seconds": audio_seconds,
            "per_slide_seconds": round(per_slide_seconds, 2),
            "voiceover_path": str(voiceover_path),
            "approved_assets": plan_assets,
            "warnings": warnings,
        }
        render_plan_path.write_text(json.dumps(plan_payload, indent=2), encoding="utf-8")
        render_command_path.write_text(
            json.dumps({"renderer": _RENDERER, "commands": command_log}, indent=2),
            encoding="utf-8",
        )

        manifest = {
            "video_id": video_id,
            "title": video.title or "",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "final_voiceover_path": str(voiceover_path),
            "approved_visual_assets_used": plan_assets,
            "visual_asset_count": len(plan_assets),
            "publishing_payload_path": publishing_payload_path,
            "final_export_path": str(final_path.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "render_plan_path": str(render_plan_path.resolve()),
            "render_command_path": str(render_command_path.resolve()),
            "renderer": _RENDERER,
            "ffmpeg_available": True,
            "note": "This is a local render artifact. It has not been uploaded to YouTube.",
            "warnings": warnings,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return {
            "renderer": _RENDERER,
            "status": "rendered",
            "ffmpeg_available": True,
            "final_export_path": str(final_path.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "render_plan_path": str(render_plan_path.resolve()),
            "render_command_path": str(render_command_path.resolve()),
            "blockers": [],
            "warnings": warnings,
        }
    except Exception as exc:  # noqa: BLE001
        final_path.unlink(missing_ok=True)
        staged_path.unlink(missing_ok=True)
        return {
            "renderer": _RENDERER,
            "status": "blocked",
            "ffmpeg_available": True,
            "final_export_path": None,
            "manifest_path": None,
            "render_plan_path": None,
            "render_command_path": None,
            "blockers": _dedupe([f"ffmpeg render failed: {exc}"]),
            "warnings": warnings,
        }
