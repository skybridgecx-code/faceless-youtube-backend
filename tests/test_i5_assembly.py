from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import shutil
import socket
import struct
import subprocess
from typing import Iterator, Mapping
import wave

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.production.assembly as assembly_module
import app.production.persistence as production_persistence
import app.production.renderer as renderer_module
from app.config import Settings
from app.db import (
    Approval,
    Artifact,
    Campaign,
    GateDecision,
    GenerationJob,
)
from app.editorial.contracts import canonical_json, canonical_sha256
from app.models import PublishRecord
from app.production.assembly import assemble_campaign

from app.production.local_visuals import (
    DataPoint,
    I5_LOCAL_VISUAL_HEIGHT,
    I5_LOCAL_VISUAL_WIDTH,
    LocalVisualRequest,
    VisualMode,
    png_dimensions,
    render_local_visual,
)
from app.production.renderer import (
    I5_AUDIO_CODEC,
    I5_RENDER_FRAME_RATE,
    I5_RENDER_HEIGHT,
    I5_RENDER_WIDTH,
    I5_VIDEO_CODEC,
    I5_VIDEO_PIXEL_FORMAT,
    RendererError,
    ToolVersions,
    assemble_final_video,
    assert_tool_versions,
    build_final_assembly_plan,
    build_scene_segment_plan,
    build_wav_concat_plan,
    capture_tool_versions,
    concat_wav_files,
    probe_media,
    render_scene_segment,
    sha256_file,
    validate_final_video,
    validate_wav,
)
from app.production.media import build_media_manifest, create_full_voiceover
from app.production.persistence import (
    I5_ASSEMBLY_MANIFEST_KIND,
    I5_FINAL_RENDER_KIND,
    I5_MEDIA_MANIFEST_KIND,
    persist_assembly_outputs,
    persist_media_manifest,
)
from app.workflows.persistence import (
    WorkflowReplayConflict,
)
from scripts.migrate_db import upgrade_database
from tests.test_i5_media import (
    _CampaignState,
    _complete_scene_media,
    _seed_planned_campaign,
    _sessions,
    _Snapshot,
    _wire_sessions,
)


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
requires_media_tools = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="canonical I5 assembly requires local ffmpeg and ffprobe",
)
CLAIM_HASH = "a" * 64


@dataclass(frozen=True)
class _AssemblySnapshots:
    assembled: _Snapshot
    final_artifact_values: Mapping[str, object]
    packet: Mapping[str, object]
    preassembly: _Snapshot


@dataclass(frozen=True)
class _AssemblyCase:
    engine: Engine
    final_artifact_values: Mapping[str, object]
    packet: Mapping[str, object]
    sessions: sessionmaker[Session]
    settings: Settings
    state: _CampaignState


def _test_tool_versions() -> ToolVersions:
    assert FFMPEG is not None and FFPROBE is not None
    return ToolVersions(
        ffmpeg_binary=FFMPEG,
        ffmpeg_version="ffmpeg version test-only",
        ffprobe_binary=FFPROBE,
        ffprobe_version="ffprobe version test-only",
    )


def _wire_assembly_sessions(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
) -> None:
    _wire_sessions(monkeypatch, sessions)
    monkeypatch.setattr(assembly_module, "SessionLocal", sessions)


def _wire_test_tool_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = _test_tool_versions()
    monkeypatch.setattr(assembly_module, "capture_tool_versions", lambda: versions)
    monkeypatch.setattr(
        renderer_module,
        "capture_tool_versions",
        lambda **_: versions,
    )


@pytest.fixture(autouse=True)
def refuse_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("I5 assembly tests attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(scope="module")
def assembly_snapshots(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_AssemblySnapshots]:
    if not FFMPEG or not FFPROBE:
        pytest.skip("canonical I5 assembly requires local ffmpeg and ffprobe")

    root = tmp_path_factory.mktemp("i5_assembly_snapshots")
    database = root / "preassembly.db"
    output = root / "output"
    output.mkdir()
    upgrade_database(f"sqlite:///{database}")

    patcher = pytest.MonkeyPatch()
    engine, sessions = _sessions(database)
    _wire_assembly_sessions(patcher, sessions)
    _wire_test_tool_versions(patcher)
    settings = Settings(
        _env_file=None,
        openai_api_key="test-only-key",
        output_dir=str(output),
    )
    state = _seed_planned_campaign(sessions, settings)
    _complete_scene_media(state, settings, patcher)
    create_full_voiceover(state.campaign_id, settings)
    media_packet = build_media_manifest(state.campaign_id, settings)
    media_result = persist_media_manifest(media_packet, settings=settings)
    assert media_result["current_stage"] == "assembly"
    engine.dispose()

    preassembly = _Snapshot(database=database, output_dir=output, state=state)
    assembled_database = root / "assembled.db"
    shutil.copy2(database, assembled_database)
    assembled_engine, assembled_sessions = _sessions(assembled_database)
    _wire_assembly_sessions(patcher, assembled_sessions)
    assembly_result = assemble_campaign(state.campaign_id, settings)
    assert assembly_result["current_stage"] == "machine_qa"
    with assembled_sessions() as db:
        manifest = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == state.campaign_id,
                Artifact.kind == I5_ASSEMBLY_MANIFEST_KIND,
            )
        )
        final = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == state.campaign_id,
                Artifact.kind == I5_FINAL_RENDER_KIND,
            )
        )
        assert manifest is not None and manifest.payload_json is not None
        assert final is not None
        packet = json.loads(manifest.payload_json)
        final_artifact_values = {
            key: getattr(final, key)
            for key in (
                "byte_size",
                "campaign_id",
                "kind",
                "mime_type",
                "payload_json",
                "prompt_template_version",
                "provenance_json",
                "provider_model",
                "provider_name",
                "sha256",
                "source_stage",
                "uri",
            )
        }
    assembled_engine.dispose()
    assembled = _Snapshot(
        database=assembled_database,
        output_dir=output,
        state=state,
    )

    try:
        yield _AssemblySnapshots(
            assembled=assembled,
            final_artifact_values=final_artifact_values,
            packet=packet,
            preassembly=preassembly,
        )
    finally:
        patcher.undo()


def _case_from_assembly_snapshot(
    source: _Snapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    *,
    final_artifact_values: Mapping[str, object],
    packet: Mapping[str, object],
) -> _AssemblyCase:
    database = tmp_path / f"{label}.db"
    shutil.copy2(source.database, database)
    engine, sessions = _sessions(database)
    _wire_assembly_sessions(monkeypatch, sessions)
    _wire_test_tool_versions(monkeypatch)
    return _AssemblyCase(
        engine=engine,
        final_artifact_values=dict(final_artifact_values),
        packet=dict(packet),
        sessions=sessions,
        settings=Settings(
            _env_file=None,
            openai_api_key="test-only-key",
            # Assembly command manifests intentionally bind absolute canonical
            # paths, so DB copies must continue validating the original
            # module-scoped disposable output root.
            output_dir=str(source.output_dir),
        ),
        state=source.state,
    )


@pytest.fixture
def assembled_case(
    assembly_snapshots: _AssemblySnapshots,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_AssemblyCase]:
    case = _case_from_assembly_snapshot(
        assembly_snapshots.assembled,
        tmp_path,
        monkeypatch,
        "assembled_case",
        final_artifact_values=assembly_snapshots.final_artifact_values,
        packet=assembly_snapshots.packet,
    )
    try:
        yield case
    finally:
        case.engine.dispose()


@pytest.fixture
def preassembly_case(
    assembly_snapshots: _AssemblySnapshots,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_AssemblyCase]:
    case = _case_from_assembly_snapshot(
        assembly_snapshots.preassembly,
        tmp_path,
        monkeypatch,
        "preassembly_case",
        final_artifact_values=assembly_snapshots.final_artifact_values,
        packet=assembly_snapshots.packet,
    )
    try:
        yield case
    finally:
        case.engine.dispose()


def _write_tone_wav(
    path: Path,
    *,
    duration_seconds: float,
    frequency: float = 440.0,
    sample_rate: int = 48_000,
) -> None:
    frame_count = round(duration_seconds * sample_rate)
    frames = bytearray()
    for index in range(frame_count):
        sample = int(7_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(bytes(frames))


def _write_visual(path: Path, *, scene_position: int, mode: VisualMode) -> None:
    request = LocalVisualRequest(
        mode=mode,
        title=f"Deterministic visual {scene_position + 1}",
        scene_position=scene_position,
        visual_purpose="Explain a claim-bound system without fabricated media",
        factual_overlay="The accepted evidence supports this bounded statement.",
        claim_hashes=(CLAIM_HASH,),
        source_keys=("source-a",),
        detail_lines=("CONTEXT", "VERIFICATION", "CONCLUSION"),
    )
    path.write_bytes(render_local_visual(request).png_bytes)


@pytest.mark.parametrize(
    "mode",
    (
        VisualMode.DOCUMENT,
        VisualMode.DIAGRAM,
        VisualMode.TIMELINE,
        VisualMode.CODE,
        VisualMode.UI_RECONSTRUCTION,
        VisualMode.KINETIC_TEXT,
        VisualMode.DETERMINISTIC_MOTION_GRAPHIC,
    ),
)
def test_local_visual_modes_are_deterministic_1280x720_and_distinct(
    mode: VisualMode,
) -> None:
    request = LocalVisualRequest(
        mode=mode,
        title="Canonical production evidence",
        scene_position=2,
        visual_purpose="Give this scene a meaningful factual treatment",
        factual_overlay="This statement is linked to accepted research.",
        claim_hashes=(CLAIM_HASH,),
        source_keys=("official-source",),
        detail_lines=("INPUT", "CHECK", "OUTPUT"),
        rights_basis="reference_only",
    )

    first = render_local_visual(request)
    second = render_local_visual(request)

    assert first.png_bytes == second.png_bytes
    assert first.sha256 == second.sha256
    assert first.request_sha256 == second.request_sha256
    assert png_dimensions(first.png_bytes) == (
        I5_LOCAL_VISUAL_WIDTH,
        I5_LOCAL_VISUAL_HEIGHT,
    )
    assert first.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(first.png_bytes) > 2_000


def test_local_visual_modes_produce_meaningfully_different_bytes() -> None:
    hashes = {
        render_local_visual(
            LocalVisualRequest(
                mode=mode,
                title="Same editorial beat",
                scene_position=0,
                visual_purpose="Compare visual treatments",
                detail_lines=("FIRST", "SECOND", "THIRD"),
            )
        ).sha256
        for mode in (
            VisualMode.DOCUMENT,
            VisualMode.DIAGRAM,
            VisualMode.TIMELINE,
            VisualMode.CODE,
            VisualMode.UI_RECONSTRUCTION,
            VisualMode.KINETIC_TEXT,
            VisualMode.DETERMINISTIC_MOTION_GRAPHIC,
        )
    }
    assert len(hashes) == 7


def test_data_visualization_requires_supplied_verified_values_and_claim_lineage() -> None:
    with pytest.raises(ValueError, match="supplied verified numeric data"):
        LocalVisualRequest(
            mode=VisualMode.DATA_VISUALIZATION,
            title="No invented values",
            scene_position=0,
            visual_purpose="Reject missing verified data",
            claim_hashes=(CLAIM_HASH,),
        )
    with pytest.raises(ValueError, match="claim lineage"):
        LocalVisualRequest(
            mode=VisualMode.DATA_VISUALIZATION,
            title="No unbound values",
            scene_position=0,
            visual_purpose="Reject unbound data",
            data_points=(DataPoint("Observed", 12.0, "12 verified"),),
        )

    request = LocalVisualRequest(
        mode=VisualMode.DATA_VISUALIZATION,
        title="Verified comparison",
        scene_position=0,
        visual_purpose="Visualize supplied evidence without inventing ticks",
        factual_overlay="Values are reproduced from the accepted claim.",
        claim_hashes=(CLAIM_HASH,),
        data_points=(
            DataPoint("Before", 12.0, "12 verified"),
            DataPoint("After", 19.0, "19 verified"),
        ),
    )
    result = render_local_visual(request)
    assert png_dimensions(result.png_bytes) == (1_280, 720)
    assert request.canonical_payload()["data_points"] == [
        {"display_value": "12 verified", "label": "Before", "value": 12.0},
        {"display_value": "19 verified", "label": "After", "value": 19.0},
    ]


def test_factual_overlay_cannot_render_without_claim_hash() -> None:
    with pytest.raises(ValueError, match="claim-hash lineage"):
        LocalVisualRequest(
            mode=VisualMode.DOCUMENT,
            title="Unbound claim",
            scene_position=0,
            visual_purpose="This should fail closed",
            factual_overlay="A factual statement",
        )


@requires_media_tools
def test_ffmpeg_and_ffprobe_versions_are_exact_and_drift_fails_closed() -> None:
    versions = capture_tool_versions()
    assert versions.ffmpeg_version.startswith("ffmpeg version ")
    assert versions.ffprobe_version.startswith("ffprobe version ")
    assert Path(versions.ffmpeg_binary).is_file()
    assert Path(versions.ffprobe_binary).is_file()
    assert assert_tool_versions(versions) == versions

    drifted = ToolVersions(
        ffmpeg_binary=versions.ffmpeg_binary,
        ffmpeg_version=versions.ffmpeg_version + " drift",
        ffprobe_binary=versions.ffprobe_binary,
        ffprobe_version=versions.ffprobe_version,
    )
    with pytest.raises(RendererError, match="identity changed"):
        assert_tool_versions(drifted)


@requires_media_tools
def test_wav_concat_is_ordered_valid_and_duration_bound(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    output = tmp_path / "voiceover.wav"
    _write_tone_wav(first, duration_seconds=0.30, frequency=330)
    _write_tone_wav(second, duration_seconds=0.45, frequency=550)

    plan = build_wav_concat_plan((first, second), output)
    assert plan == build_wav_concat_plan((first, second), output)
    assert plan.operation == "concat_wav"
    assert plan.argv.index(str(first.resolve())) < plan.argv.index(str(second.resolve()))

    result = concat_wav_files((first, second), output)
    probe = validate_wav(output)

    assert result.sha256 == sha256_file(output)
    assert result.byte_size == output.stat().st_size
    assert probe.has_audio is True
    assert probe.has_video is False
    assert probe.audio_codec == "pcm_s16le"
    assert probe.duration_seconds == pytest.approx(0.75, abs=0.04)


@requires_media_tools
def test_real_still_segments_and_final_assembly_are_canonical(tmp_path: Path) -> None:
    segment_paths: list[Path] = []
    expected_duration = 0.0
    for position, duration in enumerate((0.40, 0.55)):
        visual = tmp_path / f"visual-{position}.png"
        narration = tmp_path / f"narration-{position}.wav"
        segment = tmp_path / f"segment-{position}.mp4"
        _write_visual(
            visual,
            scene_position=position,
            mode=(VisualMode.DIAGRAM, VisualMode.KINETIC_TEXT)[position],
        )
        _write_tone_wav(
            narration,
            duration_seconds=duration,
            frequency=440 + position * 110,
        )
        result = render_scene_segment(
            visual,
            narration,
            segment,
            scene_position=position,
            visual_kind="image",
            expected_duration_seconds=duration,
        )
        assert result.probe.duration_seconds == pytest.approx(duration, abs=0.15)
        assert "zoompan" in result.plan.argv[result.plan.argv.index("-filter_complex") + 1]
        segment_paths.append(segment)
        expected_duration += result.probe.duration_seconds

    final_path = tmp_path / "final.mp4"
    pure_plan = build_final_assembly_plan(segment_paths, final_path)
    assert pure_plan == build_final_assembly_plan(segment_paths, final_path)
    assert pure_plan.argv.index(str(segment_paths[0].resolve())) < pure_plan.argv.index(
        str(segment_paths[1].resolve())
    )

    final = assemble_final_video(
        segment_paths,
        final_path,
        expected_duration_seconds=expected_duration,
    )
    probe = validate_final_video(
        final_path,
        expected_duration_seconds=expected_duration,
        duration_tolerance_seconds=0.25,
    )

    assert final.sha256 == sha256_file(final_path)
    assert final.byte_size > 0
    assert probe.video_codec == I5_VIDEO_CODEC
    assert probe.pixel_format == I5_VIDEO_PIXEL_FORMAT
    assert probe.audio_codec == I5_AUDIO_CODEC
    assert (probe.width, probe.height) == (I5_RENDER_WIDTH, I5_RENDER_HEIGHT)
    assert probe.frame_rate == pytest.approx(I5_RENDER_FRAME_RATE, abs=0.01)


@requires_media_tools
def test_generated_video_audio_is_ignored_and_narration_is_not_truncated(
    tmp_path: Path,
) -> None:
    assert FFMPEG is not None
    provider_clip = tmp_path / "provider-with-audio.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1280x720:r=30:d=0.8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=120:d=0.8:sample_rate=48000",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(provider_clip),
        ],
        check=True,
        capture_output=True,
    )
    narration = tmp_path / "canonical-narration.wav"
    _write_tone_wav(narration, duration_seconds=0.50, frequency=880)
    output = tmp_path / "generated-video-segment.mp4"

    plan = build_scene_segment_plan(
        provider_clip,
        narration,
        output,
        scene_position=1,
        visual_kind="video",
        narration_duration_seconds=0.50,
    )
    map_values = [
        plan.argv[index + 1]
        for index, value in enumerate(plan.argv[:-1])
        if value == "-map"
    ]
    assert map_values == ["[v]", "1:a:0"]
    assert all("0:a" not in value for value in map_values)

    rendered = render_scene_segment(
        provider_clip,
        narration,
        output,
        scene_position=1,
        visual_kind="video",
    )
    assert rendered.probe.duration_seconds == pytest.approx(0.50, abs=0.15)
    assert rendered.probe.has_audio and rendered.probe.has_video

    longer_narration = tmp_path / "longer-narration.wav"
    _write_tone_wav(longer_narration, duration_seconds=1.0, frequency=990)
    with pytest.raises(RendererError, match="declared still fallback"):
        render_scene_segment(
            provider_clip,
            longer_narration,
            tmp_path / "must-not-truncate.mp4",
            scene_position=1,
            visual_kind="video",
        )


@requires_media_tools
def test_probe_and_validation_fail_closed_on_missing_or_silent_media(
    tmp_path: Path,
) -> None:
    with pytest.raises(RendererError, match="missing or empty"):
        probe_media(tmp_path / "missing.mp4")

    assert FFMPEG is not None
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1280x720:r=30:d=0.3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(silent),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(RendererError, match="missing audio stream"):
        validate_final_video(silent)


def _assembly_effect_rows(
    case: _AssemblyCase,
) -> tuple[list[Artifact], list[Artifact], list[GenerationJob], list[GateDecision]]:
    campaign_id = case.state.campaign_id
    with case.sessions() as db:
        finals = list(
            db.scalars(
                select(Artifact).where(
                    Artifact.campaign_id == campaign_id,
                    Artifact.kind == I5_FINAL_RENDER_KIND,
                )
            )
        )
        manifests = list(
            db.scalars(
                select(Artifact).where(
                    Artifact.campaign_id == campaign_id,
                    Artifact.kind == I5_ASSEMBLY_MANIFEST_KIND,
                )
            )
        )
        jobs = list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.campaign_id == campaign_id,
                    GenerationJob.provider == "ffmpeg_local",
                    GenerationJob.model == "i5-ffmpeg-renderer-v1",
                )
            )
        )
        gates = list(
            db.scalars(
                select(GateDecision).where(
                    GateDecision.campaign_id == campaign_id,
                    GateDecision.stage == "assembly",
                )
            )
        )
        return finals, manifests, jobs, gates


def _assembly_effect_counts(case: _AssemblyCase) -> tuple[int, int, int, int]:
    return tuple(len(rows) for rows in _assembly_effect_rows(case))  # type: ignore[return-value]


@requires_media_tools
def test_assembly_persists_exact_canonical_effects_and_replays_same_ids(
    assembled_case: _AssemblyCase,
) -> None:
    campaign_id = assembled_case.state.campaign_id
    before = _assembly_effect_rows(assembled_case)
    first = assemble_campaign(campaign_id, assembled_case.settings)
    second = assemble_campaign(campaign_id, assembled_case.settings)
    after = _assembly_effect_rows(assembled_case)

    assert first == second
    assert first["outcome"] == "PASS"
    assert first["stage"] == "assembly"
    assert first["current_stage"] == "machine_qa"
    assert first["replayed"] is True
    assert tuple(len(rows) for rows in before) == (1, 1, 1, 1)
    assert tuple(len(rows) for rows in after) == (1, 1, 1, 1)
    assert tuple(tuple(row.id for row in rows) for rows in after) == tuple(
        tuple(row.id for row in rows) for rows in before
    )

    final = after[0][0]
    manifest = after[1][0]
    job = after[2][0]
    gate = after[3][0]
    assert first["final_render_artifact_id"] == final.id
    assert first["assembly_manifest_artifact_id"] == manifest.id
    assert first["generation_job_id"] == job.id
    assert first["gate_decision_id"] == gate.id
    assert first["final_render_hash"] == final.sha256
    assert first["output_hash"] == manifest.sha256
    assert final.source_stage == "assembly"
    assert final.mime_type == "video/mp4"
    assert final.provider_name == "ffmpeg_local"
    assert final.provider_model == "i5-ffmpeg-renderer-v1"
    assert manifest.payload_json == canonical_json(dict(assembled_case.packet))
    assert manifest.sha256 == canonical_sha256(dict(assembled_case.packet))
    assert job.output_artifact_id == final.id
    assert job.status == "completed"
    assert job.cost_microunits == 0
    assert job.reserved_cost_microunits == 0
    assert gate.output_hash == manifest.sha256
    assert gate.outcome == "PASS"

    with assembled_case.sessions() as db:
        campaign = db.get(Campaign, campaign_id)
        machine_qa_gates = db.scalar(
            select(func.count())
            .select_from(GateDecision)
            .where(
                GateDecision.campaign_id == campaign_id,
                GateDecision.stage == "machine_qa",
            )
        )
        approvals = db.scalar(
            select(func.count())
            .select_from(Approval)
            .where(Approval.campaign_id == campaign_id)
        )
        publish_records = db.scalar(
            select(func.count())
            .select_from(PublishRecord)
            .where(PublishRecord.campaign_id == campaign_id)
        )
    assert campaign is not None and campaign.current_stage == "machine_qa"
    assert machine_qa_gates == 0
    assert approvals == 0
    assert publish_records == 0


@requires_media_tools
@pytest.mark.parametrize("tamper_target", ("assembly_envelope", "media_manifest"))
def test_tampered_manifest_or_extra_top_level_key_is_rejected_without_mutation(
    assembled_case: _AssemblyCase,
    tamper_target: str,
) -> None:
    campaign_id = assembled_case.state.campaign_id
    before = _assembly_effect_counts(assembled_case)
    with assembled_case.sessions() as db:
        kind = (
            I5_ASSEMBLY_MANIFEST_KIND
            if tamper_target == "assembly_envelope"
            else I5_MEDIA_MANIFEST_KIND
        )
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == kind,
            )
        )
        assert artifact is not None and artifact.payload_json is not None
        packet = json.loads(artifact.payload_json)
        packet["unexpected_top_level_effect"] = True
        artifact.payload_json = canonical_json(packet)
        artifact.sha256 = canonical_sha256(packet)
        artifact.byte_size = len(artifact.payload_json.encode("utf-8"))
        db.commit()

    match = (
        "envelope is noncanonical"
        if tamper_target == "assembly_envelope"
        else "media manifest no longer reconciles"
    )
    with pytest.raises(WorkflowReplayConflict, match=match):
        assemble_campaign(campaign_id, assembled_case.settings)

    assert _assembly_effect_counts(assembled_case) == before
    with assembled_case.sessions() as db:
        campaign = db.get(Campaign, campaign_id)
    assert campaign is not None and campaign.current_stage == "machine_qa"


@requires_media_tools
@pytest.mark.parametrize(
    "duplicate_effect",
    (
        "final_artifact",
        "manifest_artifact",
        "renderer_job",
        "assembly_gate",
        "assembly_gate_wrong_policy",
    ),
)
def test_unexpected_duplicate_assembly_effect_is_rejected(
    assembled_case: _AssemblyCase,
    duplicate_effect: str,
) -> None:
    campaign_id = assembled_case.state.campaign_id
    with assembled_case.sessions() as db:
        final = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_FINAL_RENDER_KIND,
            )
        )
        manifest = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_ASSEMBLY_MANIFEST_KIND,
            )
        )
        job = db.scalar(
            select(GenerationJob).where(
                GenerationJob.campaign_id == campaign_id,
                GenerationJob.provider == "ffmpeg_local",
                GenerationJob.model == "i5-ffmpeg-renderer-v1",
            )
        )
        gate = db.scalar(
            select(GateDecision).where(
                GateDecision.campaign_id == campaign_id,
                GateDecision.stage == "assembly",
            )
        )
        assert final is not None and manifest is not None
        assert job is not None and gate is not None
        if duplicate_effect in {"final_artifact", "manifest_artifact"}:
            source = final if duplicate_effect == "final_artifact" else manifest
            db.add(
                Artifact(
                    campaign_id=source.campaign_id,
                    kind=source.kind,
                    uri=source.uri,
                    sha256="b" * 64,
                    byte_size=source.byte_size,
                    mime_type=source.mime_type,
                    source_stage=source.source_stage,
                    provider_name=source.provider_name,
                    provider_model=source.provider_model,
                    prompt_template_version=source.prompt_template_version,
                    payload_json=source.payload_json,
                    provenance_json=source.provenance_json,
                )
            )
        elif duplicate_effect == "renderer_job":
            db.add(
                GenerationJob(
                    campaign_id=campaign_id,
                    scene_id=None,
                    provider=job.provider,
                    model=job.model,
                    attempt=1,
                    status="completed",
                    input_hash="b" * 64,
                    output_artifact_id=final.id,
                    provider_job_id=f"i5:local:{'b' * 64}",
                    usage_json=job.usage_json,
                    cost_microunits=0,
                    reserved_cost_microunits=0,
                    error_json=None,
                    completed_at=datetime.utcnow(),
                )
            )
        else:
            db.add(
                GateDecision(
                    campaign_id=campaign_id,
                    stage="assembly",
                    outcome="PASS",
                    policy_version=(
                        "wrong-assembly-policy"
                        if duplicate_effect == "assembly_gate_wrong_policy"
                        else gate.policy_version
                    ),
                    input_hash="b" * 64,
                    output_hash=manifest.sha256,
                    reasons_json=gate.reasons_json,
                )
            )
        db.commit()

    before_failure = _assembly_effect_counts(assembled_case)
    with pytest.raises(WorkflowReplayConflict):
        assemble_campaign(campaign_id, assembled_case.settings)
    assert _assembly_effect_counts(assembled_case) == before_failure
    with assembled_case.sessions() as db:
        campaign = db.get(Campaign, campaign_id)
        approval_count = db.scalar(
            select(func.count())
            .select_from(Approval)
            .where(Approval.campaign_id == campaign_id)
        )
        publish_count = db.scalar(
            select(func.count())
            .select_from(PublishRecord)
            .where(PublishRecord.campaign_id == campaign_id)
        )
    assert campaign is not None and campaign.current_stage == "machine_qa"
    assert approval_count == 0
    assert publish_count == 0


@requires_media_tools
def test_assembly_transaction_rolls_back_all_effects_if_gate_insert_fails(
    preassembly_case: _AssemblyCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_id = preassembly_case.state.campaign_id
    original_reconcile_gate = production_persistence._reconcile_gate

    def fail_assembly_gate(
        db: Session,
        expected: Mapping[str, object],
        *,
        allow_create: bool,
    ) -> GateDecision:
        if expected["stage"] == "assembly":
            raise RuntimeError("test-only assembly gate insertion failure")
        return original_reconcile_gate(db, expected, allow_create=allow_create)

    monkeypatch.setattr(
        production_persistence,
        "_reconcile_gate",
        fail_assembly_gate,
    )
    assert _assembly_effect_counts(preassembly_case) == (0, 0, 0, 0)
    with pytest.raises(RuntimeError, match="gate insertion failure"):
        persist_assembly_outputs(
            dict(preassembly_case.packet),
            final_artifact_values=preassembly_case.final_artifact_values,
            settings=preassembly_case.settings,
        )

    assert _assembly_effect_counts(preassembly_case) == (0, 0, 0, 0)
    with preassembly_case.sessions() as db:
        campaign = db.get(Campaign, campaign_id)
    assert campaign is not None and campaign.current_stage == "assembly"


@requires_media_tools
def test_assembly_compare_and_set_failure_rolls_back_created_effects(
    preassembly_case: _AssemblyCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_id = preassembly_case.state.campaign_id
    canonical_update = production_persistence.update

    def stale_campaign_update(entity: object):
        statement = canonical_update(entity)
        if entity is Campaign:
            statement = statement.where(Campaign.id == -1)
        return statement

    monkeypatch.setattr(production_persistence, "update", stale_campaign_update)

    with pytest.raises(WorkflowReplayConflict, match="compare-and-set advancement failed"):
        persist_assembly_outputs(
            dict(preassembly_case.packet),
            final_artifact_values=preassembly_case.final_artifact_values,
            settings=preassembly_case.settings,
        )

    assert _assembly_effect_counts(preassembly_case) == (0, 0, 0, 0)
    with preassembly_case.sessions() as db:
        campaign = db.get(Campaign, campaign_id)
    assert campaign is not None and campaign.current_stage == "assembly"
