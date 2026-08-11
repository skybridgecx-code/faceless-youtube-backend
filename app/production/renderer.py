from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
from typing import Literal, Sequence


I5_RENDER_WIDTH = 1_280
I5_RENDER_HEIGHT = 720
I5_RENDER_FRAME_RATE = 30
I5_VIDEO_CODEC = "h264"
I5_VIDEO_PIXEL_FORMAT = "yuv420p"
I5_AUDIO_CODEC = "aac"
I5_RENDERER_CONTRACT_VERSION = "i5-ffmpeg-renderer-v1"
I5_DEFAULT_RENDER_TIMEOUT_SECONDS = 300.0


class RendererError(RuntimeError):
    """A bounded, sanitized local media-tool failure."""


@dataclass(frozen=True, slots=True)
class ToolVersions:
    ffmpeg_binary: str
    ffmpeg_version: str
    ffprobe_binary: str
    ffprobe_version: str

    def metadata(self) -> dict[str, str]:
        return {
            "ffmpeg_binary": self.ffmpeg_binary,
            "ffmpeg_version": self.ffmpeg_version,
            "ffprobe_binary": self.ffprobe_binary,
            "ffprobe_version": self.ffprobe_version,
        }


@dataclass(frozen=True, slots=True)
class CommandPlan:
    operation: str
    argv: tuple[str, ...]
    contract_version: str = I5_RENDERER_CONTRACT_VERSION

    def metadata(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "contract_version": self.contract_version,
            "operation": self.operation,
        }


@dataclass(frozen=True, slots=True)
class MediaProbe:
    path: str
    duration_seconds: float
    format_name: str
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    video_codec: str | None
    pixel_format: str | None
    frame_rate: float | None
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channels: int | None

    def metadata(self) -> dict[str, object]:
        return {
            "audio_channels": self.audio_channels,
            "audio_codec": self.audio_codec,
            "audio_sample_rate": self.audio_sample_rate,
            "duration_seconds": self.duration_seconds,
            "format_name": self.format_name,
            "frame_rate": self.frame_rate,
            "has_audio": self.has_audio,
            "has_video": self.has_video,
            "height": self.height,
            "path": self.path,
            "pixel_format": self.pixel_format,
            "video_codec": self.video_codec,
            "width": self.width,
        }


@dataclass(frozen=True, slots=True)
class RenderResult:
    path: str
    sha256: str
    byte_size: int
    plan: CommandPlan
    probe: MediaProbe

    def metadata(self) -> dict[str, object]:
        return {
            "byte_size": self.byte_size,
            "path": self.path,
            "plan": self.plan.metadata(),
            "probe": self.probe.metadata(),
            "sha256": self.sha256,
        }


def capture_tool_versions(
    *,
    ffmpeg_binary: str | None = None,
    ffprobe_binary: str | None = None,
) -> ToolVersions:
    ffmpeg = _resolve_binary(ffmpeg_binary, "ffmpeg")
    ffprobe = _resolve_binary(ffprobe_binary, "ffprobe")
    return ToolVersions(
        ffmpeg_binary=ffmpeg,
        ffmpeg_version=_version_first_line(ffmpeg, "ffmpeg"),
        ffprobe_binary=ffprobe,
        ffprobe_version=_version_first_line(ffprobe, "ffprobe"),
    )


def assert_tool_versions(
    expected: ToolVersions,
    *,
    ffmpeg_binary: str | None = None,
    ffprobe_binary: str | None = None,
) -> ToolVersions:
    observed = capture_tool_versions(
        ffmpeg_binary=ffmpeg_binary or expected.ffmpeg_binary,
        ffprobe_binary=ffprobe_binary or expected.ffprobe_binary,
    )
    if (
        observed.ffmpeg_version != expected.ffmpeg_version
        or observed.ffprobe_version != expected.ffprobe_version
    ):
        raise RendererError(
            "FFmpeg or ffprobe identity changed after production profile binding"
        ) from None
    return observed


def probe_media(
    path: str | Path,
    *,
    ffprobe_binary: str | None = None,
) -> MediaProbe:
    media_path = _required_file(path, "media input")
    ffprobe = _resolve_binary(ffprobe_binary, "ffprobe")
    command = (
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(media_path),
    )
    completed = _run(command, operation="ffprobe validation", timeout_seconds=60.0)
    try:
        payload = json.loads(completed.stdout)
        streams = payload["streams"]
        format_payload = payload["format"]
        if not isinstance(streams, list) or not isinstance(format_payload, dict):
            raise TypeError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RendererError("ffprobe returned invalid media metadata") from None

    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    duration = _duration_from_probe(format_payload, video, audio)
    return MediaProbe(
        path=str(media_path),
        duration_seconds=duration,
        format_name=str(format_payload.get("format_name") or ""),
        has_video=video is not None,
        has_audio=audio is not None,
        width=_optional_int(video, "width"),
        height=_optional_int(video, "height"),
        video_codec=_optional_string(video, "codec_name"),
        pixel_format=_optional_string(video, "pix_fmt"),
        frame_rate=_frame_rate(video),
        audio_codec=_optional_string(audio, "codec_name"),
        audio_sample_rate=_optional_int(audio, "sample_rate"),
        audio_channels=_optional_int(audio, "channels"),
    )


def validate_wav(
    path: str | Path,
    *,
    ffprobe_binary: str | None = None,
) -> MediaProbe:
    probe = probe_media(path, ffprobe_binary=ffprobe_binary)
    if probe.has_video or not probe.has_audio or probe.duration_seconds <= 0:
        raise RendererError("WAV validation requires nonempty audio and no video") from None
    if "wav" not in probe.format_name and probe.audio_codec not in {
        "pcm_s16le",
        "pcm_s24le",
        "pcm_s32le",
        "pcm_f32le",
    }:
        raise RendererError("narration audio is not a supported WAV stream") from None
    return probe


def build_wav_concat_plan(
    input_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    ffmpeg_binary: str | None = None,
) -> CommandPlan:
    if not input_paths:
        raise ValueError("WAV concatenation requires at least one input")
    ffmpeg = _resolve_binary(ffmpeg_binary, "ffmpeg")
    inputs = tuple(_required_file(path, "WAV input") for path in input_paths)
    output = _output_path(output_path, ".wav")
    argv: list[str] = list(_ffmpeg_prefix(ffmpeg))
    for input_path in inputs:
        argv.extend(("-i", str(input_path)))
    filters = [
        (
            f"[{index}:a:0]"
            "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono,"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        for index in range(len(inputs))
    ]
    filters.append(
        "".join(f"[a{index}]" for index in range(len(inputs)))
        + f"concat=n={len(inputs)}:v=0:a=1[outa]"
    )
    argv.extend(
        (
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outa]",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            str(output),
        )
    )
    return CommandPlan(operation="concat_wav", argv=tuple(argv))


def concat_wav_files(
    input_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    ffmpeg_binary: str | None = None,
    ffprobe_binary: str | None = None,
    timeout_seconds: float = I5_DEFAULT_RENDER_TIMEOUT_SECONDS,
) -> RenderResult:
    input_probes = [
        validate_wav(path, ffprobe_binary=ffprobe_binary) for path in input_paths
    ]
    plan = build_wav_concat_plan(
        input_paths,
        output_path,
        ffmpeg_binary=ffmpeg_binary,
    )
    output = Path(plan.argv[-1])
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(plan.argv, operation=plan.operation, timeout_seconds=timeout_seconds)
    probe = validate_wav(output, ffprobe_binary=ffprobe_binary)
    expected_duration = sum(item.duration_seconds for item in input_probes)
    if abs(probe.duration_seconds - expected_duration) > max(0.08, len(input_probes) * 0.02):
        raise RendererError("concatenated WAV duration conflicts with scene narration") from None
    return _result(output, plan, probe)


def build_scene_segment_plan(
    visual_path: str | Path,
    narration_wav_path: str | Path,
    output_path: str | Path,
    *,
    scene_position: int,
    visual_kind: Literal["image", "video"],
    narration_duration_seconds: float,
    ffmpeg_binary: str | None = None,
) -> CommandPlan:
    if scene_position < 0:
        raise ValueError("scene position must be non-negative")
    if visual_kind not in {"image", "video"}:
        raise ValueError("scene visual kind must be image or video")
    duration = _positive_duration(narration_duration_seconds)
    ffmpeg = _resolve_binary(ffmpeg_binary, "ffmpeg")
    visual = _required_file(visual_path, "scene visual")
    narration = _required_file(narration_wav_path, "scene narration")
    output = _output_path(output_path, ".mp4")
    argv: list[str] = list(_ffmpeg_prefix(ffmpeg))
    if visual_kind == "image":
        argv.extend(("-loop", "1", "-framerate", "30", "-i", str(visual)))
        motion_delta = 0.00020 + (scene_position % 4) * 0.00003
        video_filter = (
            "[0:v:0]scale=1344:756:force_original_aspect_ratio=increase,"
            "crop=1344:756,"
            f"zoompan=z='min(zoom+{motion_delta:.5f},1.05)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={I5_RENDER_WIDTH}x{I5_RENDER_HEIGHT}:fps={I5_RENDER_FRAME_RATE},"
            f"trim=duration={_seconds(duration)},setpts=PTS-STARTPTS[v]"
        )
    else:
        argv.extend(("-i", str(visual)))
        video_filter = (
            "[0:v:0]"
            f"scale={I5_RENDER_WIDTH}:{I5_RENDER_HEIGHT}:"
            "force_original_aspect_ratio=increase,"
            f"crop={I5_RENDER_WIDTH}:{I5_RENDER_HEIGHT},"
            f"fps={I5_RENDER_FRAME_RATE},"
            f"trim=duration={_seconds(duration)},setpts=PTS-STARTPTS[v]"
        )
    argv.extend(("-i", str(narration)))
    argv.extend(
        (
            "-filter_complex",
            video_filter,
            "-map",
            "[v]",
            # Explicitly select only canonical narration. Provider-video audio is ignored.
            "-map",
            "1:a:0",
            "-t",
            _seconds(duration),
            *_canonical_mp4_encoding(),
            str(output),
        )
    )
    return CommandPlan(operation="render_scene_segment", argv=tuple(argv))


def render_scene_segment(
    visual_path: str | Path,
    narration_wav_path: str | Path,
    output_path: str | Path,
    *,
    scene_position: int,
    visual_kind: Literal["image", "video"] = "image",
    expected_duration_seconds: float | None = None,
    ffmpeg_binary: str | None = None,
    ffprobe_binary: str | None = None,
    timeout_seconds: float = I5_DEFAULT_RENDER_TIMEOUT_SECONDS,
) -> RenderResult:
    narration_probe = validate_wav(
        narration_wav_path,
        ffprobe_binary=ffprobe_binary,
    )
    duration = narration_probe.duration_seconds
    if expected_duration_seconds is not None and abs(
        duration - _positive_duration(expected_duration_seconds)
    ) > 0.15:
        raise RendererError("scene narration duration conflicts with the media plan") from None
    if visual_kind == "video":
        visual_probe = probe_media(visual_path, ffprobe_binary=ffprobe_binary)
        if not visual_probe.has_video:
            raise RendererError("generated-video scene input has no video stream") from None
        if visual_probe.duration_seconds + 0.05 < duration:
            raise RendererError(
                "generated video is shorter than narration; use the declared still fallback"
            ) from None
    plan = build_scene_segment_plan(
        visual_path,
        narration_wav_path,
        output_path,
        scene_position=scene_position,
        visual_kind=visual_kind,
        narration_duration_seconds=duration,
        ffmpeg_binary=ffmpeg_binary,
    )
    output = Path(plan.argv[-1])
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(plan.argv, operation=plan.operation, timeout_seconds=timeout_seconds)
    probe = validate_final_video(
        output,
        expected_duration_seconds=duration,
        ffprobe_binary=ffprobe_binary,
        duration_tolerance_seconds=0.15,
    )
    return _result(output, plan, probe)


def build_final_assembly_plan(
    scene_segment_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    ffmpeg_binary: str | None = None,
) -> CommandPlan:
    if not scene_segment_paths:
        raise ValueError("final assembly requires at least one scene segment")
    ffmpeg = _resolve_binary(ffmpeg_binary, "ffmpeg")
    segments = tuple(
        _required_file(path, "scene segment") for path in scene_segment_paths
    )
    output = _output_path(output_path, ".mp4")
    argv: list[str] = list(_ffmpeg_prefix(ffmpeg))
    for segment in segments:
        argv.extend(("-i", str(segment)))
    filters: list[str] = []
    for index in range(len(segments)):
        filters.append(f"[{index}:v:0]setpts=PTS-STARTPTS[v{index}]")
        filters.append(
            f"[{index}:a:0]aresample=48000,asetpts=PTS-STARTPTS[a{index}]"
        )
    filters.append(
        "".join(f"[v{index}][a{index}]" for index in range(len(segments)))
        + f"concat=n={len(segments)}:v=1:a=1[outv][outa]"
    )
    argv.extend(
        (
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            *_canonical_mp4_encoding(),
            str(output),
        )
    )
    return CommandPlan(operation="assemble_final_video", argv=tuple(argv))


def assemble_final_video(
    scene_segment_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    expected_duration_seconds: float | None = None,
    ffmpeg_binary: str | None = None,
    ffprobe_binary: str | None = None,
    timeout_seconds: float = I5_DEFAULT_RENDER_TIMEOUT_SECONDS,
) -> RenderResult:
    segment_probes = [
        validate_final_video(path, ffprobe_binary=ffprobe_binary)
        for path in scene_segment_paths
    ]
    expected = (
        _positive_duration(expected_duration_seconds)
        if expected_duration_seconds is not None
        else sum(probe.duration_seconds for probe in segment_probes)
    )
    plan = build_final_assembly_plan(
        scene_segment_paths,
        output_path,
        ffmpeg_binary=ffmpeg_binary,
    )
    output = Path(plan.argv[-1])
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(plan.argv, operation=plan.operation, timeout_seconds=timeout_seconds)
    probe = validate_final_video(
        output,
        expected_duration_seconds=expected,
        ffprobe_binary=ffprobe_binary,
        duration_tolerance_seconds=max(0.20, len(segment_probes) * 0.08),
    )
    return _result(output, plan, probe)


def validate_final_video(
    path: str | Path,
    *,
    expected_duration_seconds: float | None = None,
    ffprobe_binary: str | None = None,
    duration_tolerance_seconds: float = 0.25,
) -> MediaProbe:
    if duration_tolerance_seconds < 0:
        raise ValueError("duration tolerance cannot be negative")
    probe = probe_media(path, ffprobe_binary=ffprobe_binary)
    failures: list[str] = []
    if not probe.has_video:
        failures.append("missing video stream")
    if not probe.has_audio:
        failures.append("missing audio stream")
    if (probe.width, probe.height) != (I5_RENDER_WIDTH, I5_RENDER_HEIGHT):
        failures.append("video dimensions are not 1280x720")
    if probe.video_codec != I5_VIDEO_CODEC:
        failures.append("video codec is not H.264")
    if probe.pixel_format != I5_VIDEO_PIXEL_FORMAT:
        failures.append("video pixel format is not yuv420p")
    if probe.audio_codec != I5_AUDIO_CODEC:
        failures.append("audio codec is not AAC")
    if probe.frame_rate is None or abs(probe.frame_rate - I5_RENDER_FRAME_RATE) > 0.01:
        failures.append("video frame rate is not 30 fps")
    if probe.duration_seconds <= 0:
        failures.append("media duration is invalid")
    if expected_duration_seconds is not None and abs(
        probe.duration_seconds - _positive_duration(expected_duration_seconds)
    ) > duration_tolerance_seconds:
        failures.append("media duration conflicts with the canonical narration plan")
    if failures:
        raise RendererError("final media validation failed: " + "; ".join(failures)) from None
    return probe


def sha256_file(path: str | Path) -> str:
    source = _required_file(path, "hash input")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Compatibility aliases for orchestration names commonly used in the I5 media layer.
concat_wavs = concat_wav_files
render_segment = render_scene_segment
assemble_segments = assemble_final_video
ffmpeg_ffprobe_versions = capture_tool_versions


def _canonical_mp4_encoding() -> tuple[str, ...]:
    return (
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        I5_VIDEO_PIXEL_FORMAT,
        "-r",
        str(I5_RENDER_FRAME_RATE),
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-metadata",
        "creation_time=",
        "-metadata",
        "encoder=",
        "-movflags",
        "+faststart",
        "-threads",
        "1",
    )


def _ffmpeg_prefix(ffmpeg_binary: str) -> tuple[str, ...]:
    return (
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    )


def _resolve_binary(configured: str | None, default_name: str) -> str:
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(configured)
    else:
        resolved = shutil.which(default_name)
    if not resolved:
        raise RendererError(f"{default_name} is required for canonical I5 media") from None
    return str(Path(resolved).resolve())


def _version_first_line(binary: str, expected_name: str) -> str:
    completed = _run(
        (binary, "-version"),
        operation=f"{expected_name} version capture",
        timeout_seconds=15.0,
    )
    first_line = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    if not first_line.lower().startswith(f"{expected_name} version "):
        raise RendererError(f"{expected_name} returned an invalid version identity") from None
    return first_line


def _run(
    argv: Sequence[str],
    *,
    operation: str,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    if timeout_seconds <= 0:
        raise ValueError("media command timeout must be positive")
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        raise RendererError(f"{operation} could not execute") from None
    if completed.returncode != 0:
        detail = _safe_tool_error(completed.stderr)
        suffix = f": {detail}" if detail else ""
        raise RendererError(f"{operation} failed{suffix}") from None
    return completed


def _safe_tool_error(stderr: str) -> str:
    collapsed = " ".join(stderr.split())
    return collapsed[:360]


def _required_file(path: str | Path, label: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        raise RendererError(f"{label} is missing or empty") from None
    return candidate


def _output_path(path: str | Path, required_suffix: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    if candidate.suffix.lower() != required_suffix:
        raise ValueError(f"output path must use the {required_suffix} extension")
    return candidate


def _positive_duration(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("media duration must be a positive finite value")
    return value


def _seconds(value: float) -> str:
    return f"{_positive_duration(value):.6f}".rstrip("0").rstrip(".")


def _duration_from_probe(
    format_payload: dict[str, object],
    video: dict[str, object] | None,
    audio: dict[str, object] | None,
) -> float:
    candidates = [
        format_payload.get("duration"),
        video.get("duration") if video else None,
        audio.get("duration") if audio else None,
    ]
    for candidate in candidates:
        try:
            duration = float(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    return 0.0


def _optional_string(
    payload: dict[str, object] | None,
    key: str,
) -> str | None:
    if not payload or payload.get(key) is None:
        return None
    return str(payload[key])


def _optional_int(
    payload: dict[str, object] | None,
    key: str,
) -> int | None:
    if not payload or payload.get(key) is None:
        return None
    try:
        return int(payload[key])  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _frame_rate(video: dict[str, object] | None) -> float | None:
    if not video:
        return None
    value = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    try:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        if denominator_value == 0:
            return None
        return float(numerator) / denominator_value
    except (TypeError, ValueError):
        return None


def _result(path: Path, plan: CommandPlan, probe: MediaProbe) -> RenderResult:
    return RenderResult(
        path=str(path),
        sha256=sha256_file(path),
        byte_size=path.stat().st_size,
        plan=plan,
        probe=probe,
    )
