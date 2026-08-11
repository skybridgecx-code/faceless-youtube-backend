from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence, cast

from sqlalchemy import select

from app.config import Settings
from app.db import Artifact, SessionLocal
from app.editorial.contracts import canonical_sha256
from app.workflows.persistence import WorkflowReplayConflict, WorkflowTransitionError

from .media import CanonicalMediaStore, canonical_i5_path
from .persistence import (
    I5_ASSEMBLY_MANIFEST_KIND,
    I5_FINAL_RENDER_KIND,
    binary_artifact_values,
    latest_json_artifact,
    persist_assembly_outputs,
    reconcile_media_effect_set,
)
from .renderer import (
    assemble_final_video,
    capture_tool_versions,
    render_scene_segment,
)


def _portable_probe_metadata(probe) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        key: value
        for key, value in probe.metadata().items()
        if key != "path"
    }


def _profile_tool_identity(profile_payload: Mapping[str, object]) -> dict[str, str]:
    renderer = dict(profile_payload.get("renderer") or {})
    expected = {
        "ffmpeg_version": str(renderer.get("ffmpeg_version") or ""),
        "ffprobe_version": str(renderer.get("ffprobe_version") or ""),
    }
    observed = capture_tool_versions()
    if (
        observed.ffmpeg_version != expected["ffmpeg_version"]
        or observed.ffprobe_version != expected["ffprobe_version"]
    ):
        raise WorkflowReplayConflict(
            "FFmpeg or ffprobe identity changed after production profile binding"
        )
    return {
        "ffmpeg_version": observed.ffmpeg_version,
        "ffprobe_version": observed.ffprobe_version,
    }


def _existing_assembly(campaign_id: int) -> dict[str, object] | None:
    with SessionLocal() as db:
        manifests = list(
            db.scalars(
                select(Artifact).where(
                    Artifact.campaign_id == campaign_id,
                    Artifact.kind == I5_ASSEMBLY_MANIFEST_KIND,
                )
            )
        )
        finals = list(
            db.scalars(
                select(Artifact).where(
                    Artifact.campaign_id == campaign_id,
                    Artifact.kind == I5_FINAL_RENDER_KIND,
                )
            )
        )
        if not manifests and not finals:
            return None
        if len(manifests) != 1 or len(finals) != 1:
            raise WorkflowReplayConflict("Assembly artifact effect set is not exact")
        manifest = manifests[0]
        _, payload = latest_json_artifact(db, campaign_id, I5_ASSEMBLY_MANIFEST_KIND)
        if payload is None or finals[0].sha256 != payload.get("final_render_sha256"):
            raise WorkflowReplayConflict("Assembly manifest final render is invalid")
        return {
            "final_artifact": finals[0],
            "manifest": payload,
            "manifest_artifact": manifest,
        }


def assemble_campaign(campaign_id: int, settings: Settings) -> dict[str, object]:
    existing = _existing_assembly(campaign_id)
    if existing is not None:
        final = cast(Artifact, existing["final_artifact"])
        return persist_assembly_outputs(
            cast(dict[str, object], existing["manifest"]),
            final_artifact_values={
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
            },
            settings=settings,
        )

    with SessionLocal() as db:
        context = reconcile_media_effect_set(db, campaign_id, settings=settings)
        campaign = context["campaign"]
        if campaign.current_stage != "assembly":  # type: ignore[union-attr]
            raise WorkflowTransitionError(
                f"Campaign stage is {campaign.current_stage}; expected assembly"  # type: ignore[union-attr]
            )
        media_packet = cast(Mapping[str, object], context["media_packet"])
        profile_payload = cast(Mapping[str, object], context["profile_payload"])
        versions = _profile_tool_identity(profile_payload)
        scene_media = [
            dict(item)
            for item in cast(Sequence[Mapping[str, object]], media_packet["scene_media"])
        ]
        voiceover = dict(media_packet["voiceover"])  # type: ignore[arg-type]
        input_hash = canonical_sha256(
            {
                "contract_version": "i5-assembly-input-v1",
                "media_manifest_hash": context["media_hash"],
                "ordered_scene_media": scene_media,
                "production_profile_hash": context["profile_hash"],
                "voiceover_hash": voiceover["artifact_hash"],
            }
        )

    store = CanonicalMediaStore(settings.output_path, campaign_id)
    work_root = canonical_i5_path(
        settings.output_path,
        f"campaign_{campaign_id}",
        "work",
        input_hash,
    )
    work_root.mkdir(parents=True, exist_ok=True)
    segment_results = []
    segment_records: list[dict[str, object]] = []
    expected_duration = 0.0
    for item in scene_media:
        position = int(item["scene_position"])
        narration = dict(item["narration"])  # type: ignore[arg-type]
        visual = dict(item["visual"])  # type: ignore[arg-type]
        audio_path = store.resolve(
            str(narration["object_uri"]),
            expected_sha256=str(narration["artifact_hash"]),
        )
        visual_path = store.resolve(
            str(visual["object_uri"]),
            expected_sha256=str(visual["artifact_hash"]),
        )
        duration = float(narration["duration_seconds"])
        expected_duration += duration
        segment_path = work_root / f"segment_{position:03d}.mp4"
        visual_kind = "video" if str(visual["mime_type"]).startswith("video/") else "image"
        result = render_scene_segment(
            visual_path,
            audio_path,
            segment_path,
            scene_position=position,
            visual_kind=visual_kind,  # type: ignore[arg-type]
            expected_duration_seconds=duration,
        )
        segment_results.append(result)
        segment_records.append(
            {
                "command": result.plan.metadata(),
                "narration_hash": narration["artifact_hash"],
                "observed_duration_seconds": round(result.probe.duration_seconds, 6),
                "scene_position": position,
                "segment_sha256": result.sha256,
                "visual_hash": visual["artifact_hash"],
                "visual_kind": visual_kind,
            }
        )

    final_work_path = work_root / "final.mp4"
    final_result = assemble_final_video(
        [Path(result.path) for result in segment_results],
        final_work_path,
        expected_duration_seconds=expected_duration,
    )
    final_bytes = final_work_path.read_bytes()
    stored = store.write(final_bytes, extension="mp4")
    final_artifact_values = binary_artifact_values(
        campaign_id=campaign_id,
        kind=I5_FINAL_RENDER_KIND,
        source_stage="assembly",
        uri=stored.uri,
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        mime_type="video/mp4",
        provider_name="ffmpeg_local",
        provider_model="i5-ffmpeg-renderer-v1",
        input_hash=input_hash,
        provenance={
            "campaign_id": campaign_id,
            "media_manifest_hash": context["media_hash"],
            "media_validation": _portable_probe_metadata(final_result.probe),
            "production_profile_hash": context["profile_hash"],
            "rights_basis": "assembled_canonical_i5_media",
        },
    )
    packet: dict[str, object] = {
        "campaign_id": campaign_id,
        "command_spec": {
            "final_assembly": final_result.plan.metadata(),
            "scene_segments": [record["command"] for record in segment_records],
        },
        "contract_version": "i5-assembly-manifest-v1",
        "expected_duration_seconds": round(expected_duration, 6),
        "ffmpeg_version": versions["ffmpeg_version"],
        "ffprobe_version": versions["ffprobe_version"],
        "final_render_byte_size": stored.byte_size,
        "final_render_object_uri": stored.uri,
        "final_render_sha256": stored.sha256,
        "media_manifest_hash": context["media_hash"],
        "observed_duration_seconds": round(final_result.probe.duration_seconds, 6),
        "ordered_scene_segments": segment_records,
        "production_profile_hash": context["profile_hash"],
        "script_hash": context["script_hash"],
        "storyboard_hash": context["storyboard_hash"],
        "stream_validation": _portable_probe_metadata(final_result.probe),
    }
    packet["gate"] = {
        "outcome": "PASS",
        "reasons": ["canonical_render_and_ffprobe_validation_passed"],
    }
    return persist_assembly_outputs(
        packet,
        final_artifact_values=final_artifact_values,
        settings=settings,
    )
