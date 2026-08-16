from __future__ import annotations

import json

import pytest
from app.editorial.claims import compile_research_packet
from app.editorial.contracts import (
    CandidateDemandSnapshot,
    ClaimSeed,
    DemandSnapshot,
    EditorialSeed,
    EvidenceSourceInput,
    TopicCandidate,
    canonical_json,
    canonical_sha256,
)
from app.editorial.script_compiler import compile_script_packet
from app.editorial.topic_intelligence import compile_topic_packet
from app.production.profile import (
    I5_PROVIDER_CATALOG_VERSION,
    I5_RENDERER_CONTRACT_VERSION,
    I5_RENDER_FRAME_RATE,
    I5_RENDER_HEIGHT,
    I5_RENDER_WIDTH,
)
from app.production.storyboard import compile_storyboard
from app.qa import (
    ArtifactSnapshot,
    BinaryArtifactSnapshot,
    CanonicalLineage,
    GateDecisionSnapshot,
    MachineQAInput,
    PackagingConcept,
    PackagingImplication,
    PackagingQAInput,
    ThumbnailLayout,
    evaluate_machine_qa,
)


CAMPAIGN_ID = 71


def _candidate(*, include_rejected_claim: bool = False) -> TopicCandidate:
    sources = (
        EvidenceSourceInput(source_key="official-one", source_uri="https://one.example/evidence", publisher="Public Office One", source_class="official", evidence_snippet="Official bounded evidence one."),
        EvidenceSourceInput(source_key="official-two", source_uri="https://two.example/evidence", publisher="Public Office Two", source_class="official", evidence_snippet="Official bounded evidence two."),
        *(
            (
                EvidenceSourceInput(
                    source_key="discovery-only",
                    source_uri="https://discovery.example/evidence",
                    publisher="Discovery Index",
                    source_class="discovery_only",
                    evidence_snippet="Discovery-only evidence cannot verify a fact.",
                ),
            )
            if include_rejected_claim
            else ()
        ),
    )
    claims = (
        ClaimSeed(assertion_text="The official record defines the observed sample.", claim_type="fact", role="context", source_keys=("official-one",)),
        ClaimSeed(assertion_text="The documented workflow completed the reported step.", claim_type="fact", role="evidence", source_keys=("official-one",)),
        ClaimSeed(assertion_text="A failed handoff can consume operator time and reduce trust.", claim_type="fact", role="stakes", source_keys=("official-two",)),
        ClaimSeed(assertion_text="The public office describes the feature as an operational assistant.", claim_type="attributed_claim", role="counterpoint", source_keys=("official-two",), attribution="Public Office"),
        ClaimSeed(assertion_text="The bounded sample could suggest an efficiency improvement.", claim_type="estimate", role="uncertainty", source_keys=("official-one",), assumptions=("The bounded sample represents this workflow.",)),
        ClaimSeed(assertion_text="Editorial caution is more useful than treating a demo as proof.", claim_type="opinion", role="outlook"),
        *(
            (
                ClaimSeed(
                    assertion_text="The rejected record guarantees an outcome.",
                    claim_type="fact",
                    role="counterpoint",
                    source_keys=("discovery-only",),
                ),
            )
            if include_rejected_claim
            else ()
        ),
    )
    return TopicCandidate(
        candidate_key="evidence-workflow", topic="Evidence workflow", angle="A deterministic investigation", target_viewer="Creator operators",
        viewer_promise="Teams can waste time, trust, and scarce resources on unsupported systems.", core_question="Can the workflow support its operational claims without hype?",
        why_now="Current implementation choices are being made before the bounded record is fully understood.", stakes="Teams can waste time, trust, and scarce resources on unsupported systems.",
        novelty="The analysis separates verified evidence from estimates and opinion.", broad_interest_bridge="Evidence discipline affects consequential software choices.",
        expected_takeaway="Viewers leave with an evidence checklist and explicit limits.", visual_modes=("DOCUMENT", "DIAGRAM", "DATA_VISUALIZATION", "TIMELINE"), shelf_life="evergreen", evidence_sources=sources, claims=claims,
    )


def _artifact(kind: str, stage: str, payload: dict[str, object]) -> ArtifactSnapshot:
    return ArtifactSnapshot(campaign_id=CAMPAIGN_ID, kind=kind, source_stage=stage, sha256=canonical_sha256(payload), payload=payload)  # type: ignore[arg-type]


def _binary(kind: str, stage: str, sha: str, mime: str, provider: str, model: str, provenance: dict[str, object], input_hash: str | None = None) -> BinaryArtifactSnapshot:
    if "media_validation" not in provenance:
        validation = {"height": 720, "valid": True, "width": 1280} if mime == "image/png" else ({"audio_stream": True, "format": "wav", "valid": True} if provider == "openai_tts" else {"has_audio": True, "has_video": False, "duration_seconds": 1.0, "format_name": "wav", "audio_stream": {"codec": "pcm_s16le"}})
        provenance = {**provenance, "media_validation": validation}
    full = {
        "campaign_id": CAMPAIGN_ID, "content_sha256": sha, "contract_version": "i5-binary-artifact-provenance-v1", "generation_input_hash": input_hash or canonical_sha256({"kind": kind, "sha": sha}),
        "hash_scope": "exact_binary_bytes", "immutable": True, "origin_class": "generated" if provider.startswith("openai_") else "original",
        "provider_model": model, "provider_name": provider, "prompt_template_version": "i5-media-request-v1",
        "source_class": "metered_provider_output" if provider.startswith("openai_") else "deterministic_local_output", **provenance,
    }
    return BinaryArtifactSnapshot(campaign_id=CAMPAIGN_ID, kind=kind, source_stage=stage, uri=f"object://{kind}/{sha}", sha256=sha, byte_size=100, mime_type=mime, provider_name=provider, provider_model=model, prompt_template_version="i5-media-request-v1", provenance_json=canonical_json(full))  # type: ignore[arg-type]


def _wav_probe(duration_seconds: float) -> dict[str, object]:
    return {"audio_channels": 1, "audio_codec": "pcm_s16le", "audio_sample_rate": 24_000, "duration_seconds": duration_seconds, "format_name": "wav", "frame_rate": None, "has_audio": True, "has_video": False, "height": None, "pixel_format": None, "video_codec": None, "width": None}


def _video_probe(duration_seconds: float) -> dict[str, object]:
    return {"audio_channels": 2, "audio_codec": "aac", "audio_sample_rate": 48_000, "duration_seconds": duration_seconds, "format_name": "mov,mp4,m4a,3gp,3g2,mj2", "frame_rate": 30.0, "has_audio": True, "has_video": True, "height": 720, "pixel_format": "yuv420p", "video_codec": "h264", "width": 1280}


def _gate(stage: str, artifact: ArtifactSnapshot, payload: dict[str, object], media: dict[str, object] | None = None) -> GateDecisionSnapshot:
    if stage == "topic":
        identity = {"compilation_context": payload["compilation_context"], "contract_version": "i4-topic-stage-input-v1", "demand_snapshot_hash": payload["demand_snapshot_hash"], "policy_version": payload["policy_version"], "seed_hash": payload["seed_hash"], "stage": stage}
    elif stage == "research":
        identity = {"contract_version": "i4-research-stage-input-v1", "policy_version": payload["policy_version"], "selected_candidate_key": payload["selected_candidate_key"], "stage": stage, "topic_packet_hash": payload["topic_packet_hash"]}
    elif stage == "script":
        identity = {"contract_version": "i4-script-stage-input-v1", "policy_version": payload["policy_version"], "research_packet_hash": payload["research_packet_hash"], "stage": stage, "topic_packet_hash": payload["topic_packet_hash"]}
    elif stage == "storyboard":
        identity = {"contract_version": "i5-storyboard-stage-input-v1", "production_profile_hash": payload["production_profile_hash"], "script_hash": payload["script_hash"]}
    elif stage == "media":
        identity = {"contract_version": "i5-media-stage-input-v1", "media_plan_hash": payload["media_plan_hash"], "production_profile_hash": payload["production_profile_hash"], "storyboard_hash": payload["storyboard_hash"], "voiceover_hash": payload["voiceover"]["artifact_hash"]}  # type: ignore[index]
    else:
        assert media is not None
        identity = {"contract_version": "i5-assembly-input-v1", "media_manifest_hash": canonical_sha256(media), "ordered_scene_media": media["scene_media"], "production_profile_hash": payload["production_profile_hash"], "voiceover_hash": media["voiceover"]["artifact_hash"]}  # type: ignore[index]
    return GateDecisionSnapshot(campaign_id=CAMPAIGN_ID, stage=stage, outcome="PASS", policy_version="i4-editorial-v1" if stage in {"topic", "research", "script"} else "i5-production-v1", input_hash=canonical_sha256(identity), output_hash=artifact.sha256, reasons_json=canonical_json(payload["gate"]["reasons"]))  # type: ignore[index,arg-type]


def _layout(concept_id: str, text: str, concept: str = "") -> ThumbnailLayout:
    del concept
    raw = {"concept_id": concept_id, "canvas_width_px": 1280, "canvas_height_px": 720, "text_elements": ([{"text": text, "bounds": {"x": 60, "y": 80, "width": 500, "height": 80}, "font_height_px": 48, "contrast_ratio": 7.0}] if text else []), "visual_elements": [{"bounds": {"x": 500, "y": 160, "width": 600, "height": 500}}, {"bounds": {"x": 80, "y": 300, "width": 200, "height": 100}}]}
    return ThumbnailLayout(**raw, layout_spec_sha256=canonical_sha256(raw))


def _profile(script_hash: str) -> dict[str, object]:
    return {
        "budget_policy_sha256": "5" * 64, "budget_policy_version": "campaign-budget-v1", "campaign_id": CAMPAIGN_ID, "contract_version": "i5-production-profile-v1",
        "generated_image": {"fallback_quality": "low", "model": "gpt-image-2", "primary_quality": "medium", "provider": "openai", "size": "1280x720"},
        "generated_media_bounds": {"max_cinematic_coverage_seconds": 45, "max_cinematic_scenes": 3, "max_video_scenes": 3, "max_video_total_seconds": 24},
        "i4_script_sha256": script_hash, "local_visual_renderer_version": "i5-local-visuals-v1", "production_policy_version": "i5-production-v1", "provider_catalog_version": I5_PROVIDER_CATALOG_VERSION,
        "renderer": {"contract_version": I5_RENDERER_CONTRACT_VERSION, "ffmpeg_version": "ffmpeg test 1", "ffprobe_version": "ffprobe test 1", "frame_rate": I5_RENDER_FRAME_RATE, "height": I5_RENDER_HEIGHT, "width": I5_RENDER_WIDTH},
        "storage": {"root_strategy": "output_dir/canonical_i5/campaign_<id>/objects", "version": "i5-content-addressed-local-v1"},
        "tts": {"fallback_model": "tts-1", "primary_model": "tts-1-hd", "provider": "openai", "response_format": "wav", "voice": "onyx"},
        "video": {"allow_deprecated_sora": False, "clip_duration_seconds": 8, "model": "sora-2", "provider": "disabled", "size": "1280x720"},
    }


def _input(
    *,
    include_rejected_claim: bool = False,
    candidate: TopicCandidate | None = None,
) -> MachineQAInput:
    if candidate is not None and include_rejected_claim:
        raise ValueError("custom candidates cannot add the rejected-claim fixture")
    seed = EditorialSeed(
        candidates=(candidate or _candidate(include_rejected_claim=include_rejected_claim),)
    )
    demand = DemandSnapshot(retrieved_at="2026-08-11T12:00:00+00:00", candidates=(CandidateDemandSnapshot(candidate_key="evidence-workflow", query="Evidence workflow", videos=()),))
    topic = compile_topic_packet(campaign_id=CAMPAIGN_ID, seed_hash=seed.sha256(CAMPAIGN_ID), seed=seed, demand=demand, channel_niche="evidence", channel_audience="Creator operators")
    research = compile_research_packet(campaign_id=CAMPAIGN_ID, seed=seed, topic_packet=topic, topic_packet_hash=canonical_sha256(topic))
    script = compile_script_packet(campaign_id=CAMPAIGN_ID, topic_packet=topic, topic_packet_hash=canonical_sha256(topic), research_packet=research, research_packet_hash=canonical_sha256(research))
    assert topic["gate"]["outcome"] == "PASS"
    if not include_rejected_claim:
        assert research["gate"]["outcome"] == script["gate"]["outcome"] == "PASS"
    profile = _profile(canonical_sha256(script))
    profile_artifact = _artifact("i5_production_profile", "storyboard", profile)
    storyboard = compile_storyboard(campaign_id=CAMPAIGN_ID, script_packet=script, script_hash=canonical_sha256(script), production_profile_hash=profile_artifact.sha256)
    storyboard_artifact = _artifact("i5_storyboard", "storyboard", storyboard)
    scene_media: list[dict[str, object]] = []
    narrations: list[BinaryArtifactSnapshot] = []
    visuals: list[BinaryArtifactSnapshot] = []
    for scene in storyboard["scenes"]:  # type: ignore[index]
        position = int(scene["position"])
        narration_hash = canonical_sha256({"narration": position, "storyboard": storyboard_artifact.sha256})
        visual_hash = canonical_sha256({"visual": position, "storyboard": storyboard_artifact.sha256})
        narration_input = canonical_sha256({"campaign_id": CAMPAIGN_ID, "contract_version": "i5-scene-tts-request-v1", "narration": scene["narration"], "narration_sha256": scene["narration_sha256"], "production_profile_hash": profile_artifact.sha256, "scene_position": position, "storyboard_hash": storyboard_artifact.sha256, "voice": "onyx"})
        narrations.append(_binary(f"i5_scene_narration_{position:03d}", "media", narration_hash, "audio/wav", "openai_tts", "tts-1-hd", {"narration_sha256": scene["narration_sha256"], "scene_position": position, "storyboard_hash": storyboard_artifact.sha256, "production_profile_hash": profile_artifact.sha256, "rights_basis": "original_i4_narration", "disclosure_state": "not_required", "duration_seconds": scene["estimated_duration_seconds"]}, narration_input))
        visuals.append(_binary(f"i5_scene_visual_{position:03d}", "media", visual_hash, "image/png", "deterministic_local", "i5-local-visuals-v1", {"scene_position": position, "storyboard_hash": storyboard_artifact.sha256, "production_profile_hash": profile_artifact.sha256, "rights_basis": "original_deterministic", "disclosure_state": scene["disclosure_state"], "visual_mode": scene["visual_mode"], "claim_hashes": scene["claim_hashes"], "source_keys": scene["source_keys"], "fallback": False}))
        scene_media.append({"scene_position": position, "narration": {"artifact_hash": narration_hash, "duration_seconds": scene["estimated_duration_seconds"], "mime_type": "audio/wav", "object_uri": narrations[-1].uri}, "visual": {"artifact_hash": visual_hash, "mime_type": "image/png", "object_uri": visuals[-1].uri, "provider": "deterministic_local", "visual_mode": scene["visual_mode"]}})
    voice_hash = canonical_sha256([artifact.sha256 for artifact in narrations])
    voice_input = canonical_sha256({"contract_version": "i5-voiceover-input-v1", "production_profile_hash": profile_artifact.sha256, "scene_audio_hashes": [artifact.sha256 for artifact in narrations], "storyboard_hash": storyboard_artifact.sha256})
    voice_duration = sum(float(item["narration"]["duration_seconds"]) for item in scene_media)
    voiceover = _binary("i5_voiceover", "media", voice_hash, "audio/wav", "ffmpeg_local", "i5-wav-concat-v1", {"ordered_scene_audio_hashes": [artifact.sha256 for artifact in narrations], "storyboard_hash": storyboard_artifact.sha256, "production_profile_hash": profile_artifact.sha256, "rights_basis": "original_i4_narration", "disclosure_state": "not_required", "duration_seconds": voice_duration, "media_validation": _wav_probe(voice_duration)}, voice_input)
    media = {"authorized_cap_microusd": 1_000_000, "budget_policy_sha256": "5" * 64, "budget_policy_version": "campaign-budget-v1", "campaign_id": CAMPAIGN_ID, "contract_version": "i5-media-manifest-v1", "effective_metered_campaign_cost_microusd": 0, "generated_media_summary": {"generated_image_scene_count": 0, "generated_video_scene_count": 0, "generated_video_total_seconds": 0.0, "local_visual_scene_count": len(scene_media)}, "integrity": {"all_content_hashes_verified": True, "all_objects_inside_canonical_store": True, "all_scene_media_present": True, "generated_media_is_illustrative_only": True, "rights_gate_passed": True}, "media_plan_hash": canonical_sha256({"plan": "valid", "storyboard": storyboard_artifact.sha256}), "production_profile_hash": profile_artifact.sha256, "provider_fallback_summary": {"fallback_scene_count": 0, "provider_scene_counts": {"deterministic_local": len(scene_media), "openai_image": 0, "openai_video": 0}}, "reserved_in_flight_cost_microusd": 0, "scene_media": scene_media, "selected_tts_model": "tts-1-hd", "soft_warning_active": False, "storyboard_hash": storyboard_artifact.sha256, "tts_voice": "onyx", "voiceover": {"artifact_hash": voice_hash, "duration_seconds": voice_duration, "object_uri": voiceover.uri}, "gate": {"outcome": "PASS", "reasons": ["all_scene_media_and_budget_integrity_checks_passed"]}}
    media_artifact = _artifact("i5_media_manifest", "media", media)
    input_hash = canonical_sha256({"contract_version": "i5-assembly-input-v1", "media_manifest_hash": media_artifact.sha256, "ordered_scene_media": scene_media, "production_profile_hash": profile_artifact.sha256, "voiceover_hash": voice_hash})
    final_hash = canonical_sha256({"final": media_artifact.sha256})
    final = _binary("i5_final_render", "assembly", final_hash, "video/mp4", "ffmpeg_local", "i5-ffmpeg-renderer-v1", {"media_manifest_hash": media_artifact.sha256, "production_profile_hash": profile_artifact.sha256, "rights_basis": "assembled_canonical_i5_media", "disclosure_state": "not_required", "generation_input_hash": input_hash})
    segments = [{"command": {"scene": item["scene_position"]}, "narration_hash": item["narration"]["artifact_hash"], "observed_duration_seconds": item["narration"]["duration_seconds"], "scene_position": item["scene_position"], "segment_sha256": canonical_sha256({"segment": item["scene_position"]}), "visual_hash": item["visual"]["artifact_hash"], "visual_kind": "image"} for item in scene_media]
    total = sum(float(item["narration"]["duration_seconds"]) for item in scene_media)
    assembly = {"campaign_id": CAMPAIGN_ID, "command_spec": {"final_assembly": {"concat": True}, "scene_segments": [segment["command"] for segment in segments]}, "contract_version": "i5-assembly-manifest-v1", "expected_duration_seconds": total, "ffmpeg_version": profile["renderer"]["ffmpeg_version"], "ffprobe_version": profile["renderer"]["ffprobe_version"], "final_render_byte_size": final.byte_size, "final_render_object_uri": final.uri, "final_render_sha256": final.sha256, "gate": {"outcome": "PASS", "reasons": ["canonical_render_and_ffprobe_validation_passed"]}, "media_manifest_hash": media_artifact.sha256, "observed_duration_seconds": total, "ordered_scene_segments": segments, "production_profile_hash": profile_artifact.sha256, "script_hash": canonical_sha256(script), "storyboard_hash": storyboard_artifact.sha256, "stream_validation": {"duration_seconds": total}}
    assembly["stream_validation"] = _video_probe(total)
    final_provenance = json.loads(final.provenance_json)
    final_provenance["media_validation"] = assembly["stream_validation"]
    final = final.model_copy(update={"provenance_json": canonical_json(final_provenance)})
    assembly_artifact = _artifact("i5_assembly_manifest", "assembly", assembly)
    artifacts = {"topic": _artifact("i4_topic_packet", "topic", topic), "research": _artifact("i4_research_packet", "research", research), "script": _artifact("i4_script", "script", script), "storyboard": storyboard_artifact, "media": media_artifact, "assembly": assembly_artifact}
    gates = tuple(_gate(stage, artifacts[stage], artifacts[stage].payload or {}, media) for stage in ("topic", "research", "script", "storyboard", "media", "assembly"))
    lineage = CanonicalLineage(campaign_id=CAMPAIGN_ID, topic=artifacts["topic"], research=artifacts["research"], script=artifacts["script"], storyboard=storyboard_artifact, production_profile=profile_artifact, media=media_artifact, assembly=assembly_artifact, final_render=ArtifactSnapshot(campaign_id=CAMPAIGN_ID, kind="i5_final_render", source_stage="assembly", sha256=final.sha256), gates=gates, editorial_seed_payload=seed.artifact_payload(CAMPAIGN_ID), demand_snapshot_payload=demand.model_dump(mode="json"), scene_narration_artifacts=tuple(narrations), scene_visual_artifacts=tuple(visuals), voiceover_artifact=voiceover, final_render_artifact=final)
    claims = research["claims"]  # type: ignore[index]
    fact = next(claim for claim in claims if claim["claim_type"] == "fact")
    attributed = next(claim for claim in claims if claim["claim_type"] == "attributed_claim")
    estimate = next(claim for claim in claims if claim["claim_type"] == "estimate")
    opinion = next(claim for claim in claims if claim["claim_type"] == "opinion")
    implications = ((PackagingImplication(statement=fact["assertion_text"], claim_hashes=(fact["claim_hash"],)),), (PackagingImplication(statement=f"{attributed['attribution']} states: {attributed['assertion_text']}", claim_hashes=(attributed["claim_hash"],)),), (PackagingImplication(statement=f"Estimate: {estimate['assertion_text']} Assumptions: {'; '.join(estimate['assumptions'])}", claim_hashes=(estimate["claim_hash"],)), PackagingImplication(statement=f"Opinion: {opinion['assertion_text']}", claim_hashes=(opinion["claim_hash"],))))
    surfaces = (
        (topic["viewer_promise"]["core_question"], topic["viewer_promise"]["core_question"], "Can the workflow", topic["viewer_promise"]["core_question"], topic["viewer_promise"]["core_question"]),
        (fact["assertion_text"], fact["assertion_text"], "", fact["assertion_text"], fact["assertion_text"]),
        (f"Estimate: {estimate['assertion_text']} Assumptions: {'; '.join(estimate['assumptions'])}", f"Opinion: {opinion['assertion_text']}", "", f"Opinion: {opinion['assertion_text']}", f"Estimate: {estimate['assertion_text']} Assumptions: {'; '.join(estimate['assumptions'])}"),
    )
    concepts = tuple(PackagingConcept(concept_id=f"concept-{index}", title=surfaces[index][0], thumbnail_concept=surfaces[index][1], thumbnail_text=surfaces[index][2], viewer_trigger=surfaces[index][3], promise=surfaces[index][4], overclaim_risk=("low", "medium", "high")[index], target_audience=("Creator operators", "Technical operators", "Operations leads")[index], implications=implications[index]) for index in range(3))  # type: ignore[index]
    return MachineQAInput(lineage=lineage, packaging=PackagingQAInput(concepts=concepts, selected_concept_id="concept-0", thumbnails=tuple(_layout(concept.concept_id, concept.thumbnail_text, concept.thumbnail_concept) for concept in concepts)), commercial_score=100)


def _with(value: MachineQAInput, **updates: object) -> MachineQAInput:
    return value.model_copy(update={"lineage": value.lineage.model_copy(update=updates)})


def _with_observed_scene_durations(
    value: MachineQAInput, durations: dict[int, float]
) -> MachineQAInput:
    from app.qa.machine import _i5_input_hash

    media = dict(value.lineage.media.payload or {})
    scene_media = [dict(item) for item in media["scene_media"]]  # type: ignore[index]
    narrations = list(value.lineage.scene_narration_artifacts)
    for position, duration in durations.items():
        narration = dict(scene_media[position]["narration"])
        narration["duration_seconds"] = duration
        scene_media[position]["narration"] = narration
        provenance = json.loads(narrations[position].provenance_json)
        provenance["duration_seconds"] = duration
        narrations[position] = narrations[position].model_copy(
            update={"provenance_json": canonical_json(provenance)}
        )
    media["scene_media"] = scene_media
    total = sum(
        float(dict(item["narration"])["duration_seconds"]) for item in scene_media
    )
    voiceover = dict(media["voiceover"])
    voiceover["duration_seconds"] = total
    media["voiceover"] = voiceover
    altered_media = value.lineage.media.model_copy(
        update={"payload": media, "sha256": canonical_sha256(media)}
    )
    voice = value.lineage.voiceover_artifact
    assert voice is not None
    voice_provenance = json.loads(voice.provenance_json)
    voice_provenance["duration_seconds"] = total
    voice_provenance["media_validation"]["duration_seconds"] = total
    altered_voice = voice.model_copy(
        update={"provenance_json": canonical_json(voice_provenance)}
    )
    assembly = dict(value.lineage.assembly.payload or {})
    segments = [dict(item) for item in assembly["ordered_scene_segments"]]  # type: ignore[index]
    for position, duration in durations.items():
        segments[position]["observed_duration_seconds"] = duration
    assembly["ordered_scene_segments"] = segments
    assembly["expected_duration_seconds"] = total
    assembly["observed_duration_seconds"] = total
    assembly["media_manifest_hash"] = altered_media.sha256
    stream = dict(assembly["stream_validation"])
    stream["duration_seconds"] = total
    assembly["stream_validation"] = stream
    altered_assembly = value.lineage.assembly.model_copy(
        update={"payload": assembly, "sha256": canonical_sha256(assembly)}
    )
    final = value.lineage.final_render_artifact
    assert final is not None
    final_provenance = json.loads(final.provenance_json)
    final_provenance.update(
        {"media_manifest_hash": altered_media.sha256, "media_validation": stream}
    )
    final_provenance["generation_input_hash"] = _i5_input_hash(
        "assembly",
        assembly,
        media_payload={**media, "_artifact_sha256": altered_media.sha256},
    )
    altered_final = final.model_copy(
        update={"provenance_json": canonical_json(final_provenance)}
    )
    changed = _with(
        value,
        media=altered_media,
        assembly=altered_assembly,
        scene_narration_artifacts=tuple(narrations),
        voiceover_artifact=altered_voice,
        final_render_artifact=altered_final,
    )
    gates = tuple(
        _gate(
            stage,
            getattr(changed.lineage, stage),
            getattr(changed.lineage, stage).payload or {},
            media,
        )
        for stage in ("topic", "research", "script", "storyboard", "media", "assembly")
    )
    return changed.model_copy(
        update={"lineage": changed.lineage.model_copy(update={"gates": gates})}
    )


def test_clean_fully_bound_canonical_lineage_requires_thumbnail_semantic_review_and_is_repeatable() -> None:
    value = _input()
    result = evaluate_machine_qa(value)
    assert result.outcome == "NEEDS_HUMAN"
    assert result.hook.outcome == "NEEDS_HUMAN"
    assert result.packaging.outcome == "NEEDS_HUMAN"
    assert all(item.outcome == "NEEDS_HUMAN" for item in result.packaging.thumbnails)
    assert {item.check_id for item in result.hook.review_findings} == {
        "hook_unnecessary_introduction",
        "hook_information_density",
        "hook_continuation_reason",
        "hook_avoidable_length",
    }
    assert {item.check_id for item in result.packaging.review_findings} == {
        "thumbnail_dominant_idea", "thumbnail_hierarchy"
    }
    assert all(item.outcome == "PASS" for item in result.findings)
    assert result == evaluate_machine_qa(value)
    assert value.lineage.media.payload["scene_media"]  # type: ignore[index]
    assert value.lineage.media.payload.get("policy_version") is None  # type: ignore[union-attr]
    assert all(item.mime_type == "audio/wav" for item in value.lineage.scene_narration_artifacts)


def test_rehashed_noncanonical_topic_and_profile_mismatch_fail() -> None:
    value = _input()
    topic = dict(value.lineage.topic.payload or {})
    topic["commercial_score"] = 99
    altered_topic = value.lineage.topic.model_copy(update={"payload": topic, "sha256": canonical_sha256(topic)})
    profile = dict(value.lineage.production_profile.payload or {})
    profile["i4_script_sha256"] = "f" * 64
    altered_profile = value.lineage.production_profile.model_copy(update={"payload": profile, "sha256": canonical_sha256(profile)})
    for candidate, check in ((_with(value, topic=altered_topic), "lineage_topic_recompilation"), (_with(value, production_profile=altered_profile), "production_profile_payload")):
        result = evaluate_machine_qa(candidate)
        assert result.outcome == "FAIL"
        assert any(item.check_id == check for item in result.findings)


def test_media_binary_lineage_failures_are_hard() -> None:
    value = _input()
    bad_audio = value.lineage.scene_narration_artifacts[0].model_copy(update={"mime_type": "audio/mpeg"})
    bad_rights = value.lineage.scene_visual_artifacts[0].model_copy(update={"provenance_json": value.lineage.scene_visual_artifacts[0].provenance_json.replace("original_deterministic", "unknown_rights_basis")})
    bad_voice = value.lineage.voiceover_artifact.model_copy(update={"provenance_json": value.lineage.voiceover_artifact.provenance_json.replace(value.lineage.scene_narration_artifacts[0].sha256, "f" * 64)})  # type: ignore[union-attr]
    for candidate in (_with(value, scene_narration_artifacts=(bad_audio, *value.lineage.scene_narration_artifacts[1:])), _with(value, scene_visual_artifacts=(bad_rights, *value.lineage.scene_visual_artifacts[1:])), _with(value, voiceover_artifact=bad_voice)):
        assert evaluate_machine_qa(candidate).outcome == "FAIL"


def test_minimal_assembly_and_overlong_ordered_opening_cannot_pass() -> None:
    value = _input()
    assembly = {"campaign_id": CAMPAIGN_ID, "contract_version": "i5-assembly-manifest-v1"}
    minimal = value.lineage.assembly.model_copy(update={"payload": assembly, "sha256": canonical_sha256(assembly)})
    storyboard = dict(value.lineage.storyboard.payload or {})
    scenes = [dict(scene) for scene in storyboard["scenes"]]  # type: ignore[index]
    scenes[0]["estimated_duration_seconds"] = 30.01
    storyboard["scenes"] = scenes
    altered_storyboard = value.lineage.storyboard.model_copy(update={"payload": storyboard, "sha256": canonical_sha256(storyboard)})
    for candidate, check in ((_with(value, assembly=minimal), "assembly_persistence_envelope"), (_with(value, storyboard=altered_storyboard), "hook_first_30_seconds")):
        result = evaluate_machine_qa(candidate)
        assert result.outcome == "FAIL"
        assert any(item.check_id == check for item in (*result.findings, *result.hook.findings))


def test_hard_failure_dominates_commercial_score() -> None:
    value = _input()
    bad = value.lineage.research.model_copy(update={"campaign_id": 72})
    result = evaluate_machine_qa(_with(value, research=bad).model_copy(update={"commercial_score": 100}))
    assert result.outcome == "FAIL"
    assert any(item.hard_gate for item in result.findings)


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "reordered"))
def test_populated_media_manifest_rejects_missing_duplicate_and_reordered_entries(mutation: str) -> None:
    value = _input()
    media = dict(value.lineage.media.payload or {})
    scenes = [dict(item) for item in media["scene_media"]]  # type: ignore[index]
    if mutation == "missing":
        scenes.pop()
    elif mutation == "duplicate":
        scenes[-1] = scenes[0]
    else:
        scenes.reverse()
    media["scene_media"] = scenes
    altered = value.lineage.media.model_copy(update={"payload": media, "sha256": canonical_sha256(media)})
    result = evaluate_machine_qa(_with(value, media=altered))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "media_scene_cardinality_order" for item in result.findings)


@pytest.mark.parametrize("mutation", ("disclosure", "content_hash", "generated_evidence"))
def test_binary_provenance_mismatch_cannot_establish_media_integrity(mutation: str) -> None:
    value = _input()
    visual = value.lineage.scene_visual_artifacts[0]
    if mutation == "disclosure":
        altered = visual.model_copy(update={"provenance_json": visual.provenance_json.replace(str((value.lineage.storyboard.payload or {})["scenes"][0]["disclosure_state"]), "wrong_disclosure")})  # type: ignore[index]
    elif mutation == "content_hash":
        altered = visual.model_copy(update={"provenance_json": visual.provenance_json.replace(visual.sha256, "f" * 64)})
    else:
        altered = visual.model_copy(update={"provider_name": "openai_image", "provider_model": "gpt-image-2"})
    result = evaluate_machine_qa(_with(value, scene_visual_artifacts=(altered, *value.lineage.scene_visual_artifacts[1:])))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "media_scene_validation_lineage" for item in result.findings)


def test_false_media_integrity_and_wrong_manifest_policy_field_fail() -> None:
    value = _input()
    media = dict(value.lineage.media.payload or {})
    media["integrity"] = {"all_content_hashes_verified": False}
    altered = value.lineage.media.model_copy(update={"payload": media, "sha256": canonical_sha256(media)})
    result = evaluate_machine_qa(_with(value, media=altered))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "media_manifest_integrity_metadata" for item in result.findings)


def test_duplicate_sources_claims_and_forged_claim_evaluation_fail_closed() -> None:
    value = _input()
    research = dict(value.lineage.research.payload or {})
    sources = [dict(item) for item in research["sources"]]  # type: ignore[index]
    claims = [dict(item) for item in research["claims"]]  # type: ignore[index]
    claims[0]["assertion_text"] = "forged"
    research["sources"] = [*sources, dict(sources[0])]
    research["claims"] = [*claims, dict(claims[1])]
    altered = value.lineage.research.model_copy(update={"payload": research, "sha256": canonical_sha256(research)})
    result = evaluate_machine_qa(_with(value, research=altered))
    assert result.outcome == "FAIL"
    check_ids = {item.check_id for item in result.findings}
    assert {"research_duplicate_source_key", "research_duplicate_claim_identity", "research_claim_evaluation"} <= check_ids


def test_topic_demand_hash_and_assembly_final_render_binding_fail() -> None:
    value = _input()
    changed_demand = dict(value.lineage.demand_snapshot_payload)
    changed_demand["retrieved_at"] = "2026-08-12T12:00:00+00:00"
    bad_final = value.lineage.final_render_artifact.model_copy(update={"uri": "object://wrong/final"})  # type: ignore[union-attr]
    for candidate, expected in ((_with(value, demand_snapshot_payload=changed_demand), "lineage_topic_recompilation"), (_with(value, final_render_artifact=bad_final), "assembly_persistence_envelope")):
        result = evaluate_machine_qa(candidate)
        assert result.outcome == "FAIL"
        assert any(item.check_id == expected for item in result.findings)


def test_first_thirty_seconds_uses_complete_ordered_cold_open_scenes() -> None:
    value = _input()
    scenes = (value.lineage.storyboard.payload or {})["scenes"]  # type: ignore[index]
    assert len([scene for scene in scenes if scene["section_id"] == "cold_open"]) >= 1
    script_cold_open = next(
        section
        for section in value.lineage.script.payload["sections"]  # type: ignore[index,union-attr]
        if section["section_id"] == "cold_open"
    )
    assert value.lineage.topic.payload["viewer_promise"]["why_now"] != script_cold_open["continuation_reason"]  # type: ignore[index,union-attr]
    result = evaluate_machine_qa(value)
    finding = next(item for item in result.hook.findings if item.check_id == "hook_first_30_seconds")
    assert finding.outcome == "PASS"
    assert "Complete ordered canonical scenes" in finding.message
    assert {
        item.outcome
        for item in result.hook.findings
        if item.check_id in {"hook_core_question", "hook_stakes", "hook_why_now"}
    } == {"PASS"}


def test_storyboard_profile_hash_mismatch_fails() -> None:
    value = _input()
    storyboard = dict(value.lineage.storyboard.payload or {})
    storyboard["production_profile_hash"] = "f" * 64
    altered = value.lineage.storyboard.model_copy(update={"payload": storyboard, "sha256": canonical_sha256(storyboard)})
    result = evaluate_machine_qa(_with(value, storyboard=altered))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "production_profile_binding" for item in result.findings)


def test_media_payload_policy_version_is_not_canonical() -> None:
    value = _input()
    media = dict(value.lineage.media.payload or {})
    media["policy_version"] = "i5-production-v1"
    altered = value.lineage.media.model_copy(update={"payload": media, "sha256": canonical_sha256(media)})
    result = evaluate_machine_qa(_with(value, media=altered))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "media_persistence_envelope" for item in result.findings)


def test_final_render_generation_input_mismatch_fails() -> None:
    value = _input()
    final = value.lineage.final_render_artifact
    assert final is not None
    provenance = json.loads(final.provenance_json)
    provenance["generation_input_hash"] = "f" * 64
    altered = final.model_copy(update={"provenance_json": canonical_json(provenance)})
    result = evaluate_machine_qa(_with(value, final_render_artifact=altered))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "assembly_persistence_envelope" for item in result.findings)


def test_campaign_budget_total_includes_reservation_without_double_counting() -> None:
    value = _input()
    media = dict(value.lineage.media.payload or {})
    media.update({"authorized_cap_microusd": 100, "effective_metered_campaign_cost_microusd": 100, "reserved_in_flight_cost_microusd": 40, "soft_warning_active": False})
    altered = value.lineage.media.model_copy(update={"payload": media, "sha256": canonical_sha256(media)})
    result = evaluate_machine_qa(_with(value, media=altered))
    assert not any(item.check_id == "media_recomputed_summary" and item.outcome == "FAIL" for item in result.findings)
    assert media["budget_policy_version"] == "campaign-budget-v1"


@pytest.mark.parametrize("field", ("primary_model", "provider_catalog_version", "local_visual_renderer_version", "storage", "generated_media_bounds"))
def test_locked_profile_catalog_drift_fails(field: str) -> None:
    value = _input()
    profile = dict(value.lineage.production_profile.payload or {})
    if field == "primary_model":
        tts = dict(profile["tts"])
        tts[field] = "wrong"
        profile["tts"] = tts
    elif field in {"storage", "generated_media_bounds"}:
        nested = dict(profile[field])  # type: ignore[arg-type]
        nested["version" if field == "storage" else "max_video_scenes"] = "wrong" if field == "storage" else 99
        profile[field] = nested
    else:
        profile[field] = "wrong"
    altered = value.lineage.production_profile.model_copy(update={"payload": profile, "sha256": canonical_sha256(profile)})
    result = evaluate_machine_qa(_with(value, production_profile=altered))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "production_profile_payload" for item in result.findings)


def test_all_six_hook_dimension_findings_are_explicit() -> None:
    result = evaluate_machine_qa(_input()).hook
    findings = {item.check_id for item in result.findings}
    reviews = {item.check_id for item in result.review_findings}
    assert {"hook_promise_fulfillment", "hook_stakes"} <= findings
    assert {"hook_unnecessary_introduction", "hook_information_density", "hook_continuation_reason", "hook_avoidable_length"} <= reviews


def test_structurally_correct_hook_keeps_subjective_quality_in_human_review() -> None:
    result = evaluate_machine_qa(_input())
    structural = {
        item.check_id: item.outcome
        for item in result.hook.findings
        if item.check_id
        in {
            "hook_cold_open_contract",
            "hook_first_30_seconds",
            "hook_direct_start_proxy",
            "hook_observed_window_coverage",
        }
    }
    reviews = {item.check_id for item in result.hook.review_findings}

    assert structural == {
        "hook_cold_open_contract": "PASS",
        "hook_first_30_seconds": "PASS",
        "hook_direct_start_proxy": "PASS",
        "hook_observed_window_coverage": "PASS",
    }
    assert {
        "hook_unnecessary_introduction",
        "hook_information_density",
        "hook_continuation_reason",
        "hook_avoidable_length",
    } <= reviews
    assert result.hook.outcome == result.outcome == "NEEDS_HUMAN"


def test_reference_only_local_visual_is_canonical_but_generated_evidence_is_not() -> None:
    value = _input()
    visual = value.lineage.scene_visual_artifacts[0]
    reference_only = visual.model_copy(update={"provenance_json": visual.provenance_json.replace("original_deterministic", "reference_only")})
    reference_result = evaluate_machine_qa(_with(value, scene_visual_artifacts=(reference_only, *value.lineage.scene_visual_artifacts[1:])))
    assert not any(item.check_id == "media_scene_validation_lineage" for item in reference_result.findings)

    provenance = json.loads(visual.provenance_json)
    provenance.update({"provider_name": "openai_image", "provider_model": "gpt-image-2", "origin_class": "generated", "source_class": "metered_provider_output", "rights_basis": "generated_illustrative", "disclosure_state": "illustrative_generated_media", "generated_media_is_evidence": True, "claim_hashes": []})
    generated = visual.model_copy(update={"provider_name": "openai_image", "provider_model": "gpt-image-2", "provenance_json": canonical_json(provenance)})
    result = evaluate_machine_qa(_with(value, scene_visual_artifacts=(generated, *value.lineage.scene_visual_artifacts[1:])))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "media_scene_validation_lineage" and item.hard_gate for item in result.findings)


@pytest.mark.parametrize("model,quality", (("gpt-image-2-medium", "medium"), ("gpt-image-2-low", "low")))
def test_canonical_generated_image_models_are_accepted(model: str, quality: str) -> None:
    value = _input()
    visual = value.lineage.scene_visual_artifacts[0]
    provenance = json.loads(visual.provenance_json)
    provenance.update({"provider_name": "openai_image", "provider_model": model, "origin_class": "generated", "source_class": "metered_provider_output", "rights_basis": "generated_illustrative", "disclosure_state": "illustrative_generated_media", "generated_media_is_evidence": False, "claim_hashes": [], "source_keys": [], "provider_metadata": {"model": "gpt-image-2", "output_format": "png", "prompt": "canonical illustrative prompt", "quality": quality, "reservation_microunits": 100000 if quality == "medium" else 25000, "size": "1280x720"}})
    generated = visual.model_copy(update={"provider_name": "openai_image", "provider_model": model, "provenance_json": canonical_json(provenance)})
    media = dict(value.lineage.media.payload or {})
    entries = [dict(item) for item in media["scene_media"]]  # type: ignore[index]
    first_visual = dict(entries[0]["visual"])
    first_visual["provider"] = "openai_image"
    entries[0]["visual"] = first_visual
    media["scene_media"] = entries
    media["generated_media_summary"] = {"generated_image_scene_count": 1, "generated_video_scene_count": 0, "generated_video_total_seconds": 0.0, "local_visual_scene_count": len(entries) - 1}
    media["provider_fallback_summary"] = {"fallback_scene_count": 0, "provider_scene_counts": {"deterministic_local": len(entries) - 1, "openai_image": 1, "openai_video": 0}}
    result = evaluate_machine_qa(_with(value, media=value.lineage.media.model_copy(update={"payload": media, "sha256": canonical_sha256(media)}), scene_visual_artifacts=(generated, *value.lineage.scene_visual_artifacts[1:])))
    assert not any(item.check_id == "media_scene_validation_lineage" for item in result.findings)


def test_noncanonical_media_validation_and_selected_tts_fail() -> None:
    value = _input()
    narration = value.lineage.scene_narration_artifacts[0]
    bad_narration = narration.model_copy(update={"provenance_json": narration.provenance_json.replace('"audio_stream":true,"format":"wav","valid":true', '"valid":true')})
    media = dict(value.lineage.media.payload or {})
    media["selected_tts_model"] = "unbound-model"
    altered_media = value.lineage.media.model_copy(update={"payload": media, "sha256": canonical_sha256(media)})
    result = evaluate_machine_qa(_with(value, scene_narration_artifacts=(bad_narration, *value.lineage.scene_narration_artifacts[1:]), media=altered_media))
    assert result.outcome == "FAIL"
    assert any(item.check_id in {"media_scene_validation_lineage", "media_persistence_envelope"} for item in result.findings)


def test_hook_dimension_findings_do_not_contradict_failed_window_or_promise() -> None:
    value = _input()
    selected = value.packaging.concepts[0].model_copy(update={"promise": "custom semantic promise"})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (selected, *value.packaging.concepts[1:])})}))
    findings = {item.check_id: item.outcome for item in result.hook.findings}
    assert findings["hook_promise_fulfillment"] == "NEEDS_HUMAN"


@pytest.mark.parametrize("field", ("title", "thumbnail_concept", "thumbnail_text", "viewer_trigger", "promise"))
def test_free_form_package_surface_is_indeterminate_without_structured_lineage(field: str) -> None:
    value = _input()
    surface = "This cure works for everyone"
    first = value.packaging.concepts[0].model_copy(update={field: surface})
    packaging = value.packaging.model_copy(update={"concepts": (first, *value.packaging.concepts[1:])})
    if field == "thumbnail_text":
        packaging = packaging.model_copy(
            update={"thumbnails": (_layout(first.concept_id, surface), *value.packaging.thumbnails[1:])}
        )
    result = evaluate_machine_qa(value.model_copy(update={"packaging": packaging}))
    assert result.outcome == "NEEDS_HUMAN"
    finding = next(
        item
        for item in result.findings
        if item.check_id == "packaging_surface_truth_gate" and field in item.evidence
    )
    assert finding.outcome == "NEEDS_HUMAN" and not finding.hard_gate


def test_observed_assembly_timing_controls_first_thirty_seconds() -> None:
    value = _input()
    assembly = dict(value.lineage.assembly.payload or {})
    segments = [dict(item) for item in assembly["ordered_scene_segments"]]  # type: ignore[index]
    segments[0]["observed_duration_seconds"] = 35.0
    assembly["ordered_scene_segments"] = segments
    changed = value.lineage.assembly.model_copy(update={"payload": assembly, "sha256": canonical_sha256(assembly)})
    result = evaluate_machine_qa(_with(value, assembly=changed))
    assert result.outcome == "FAIL"
    assert next(item for item in result.hook.findings if item.check_id == "hook_first_30_seconds").outcome == "NEEDS_HUMAN"


def test_two_field_append_now_rewrite_is_rejected() -> None:
    value = _input()
    first = value.packaging.concepts[0]
    rewrite = value.packaging.concepts[1].model_copy(update={"title": f"{first.title} now", "thumbnail_concept": first.thumbnail_concept, "thumbnail_text": first.thumbnail_text, "viewer_trigger": f"{first.viewer_trigger} now", "promise": first.promise, "target_audience": first.target_audience, "overclaim_risk": first.overclaim_risk, "implications": first.implications})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (first, rewrite, value.packaging.concepts[2])})}))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "packaging_trivial_rewrite_duplicate" for item in result.findings)


@pytest.mark.parametrize("target, payload", (("storyboard", {"campaign_id": CAMPAIGN_ID, "scenes": []}), ("media", {"campaign_id": CAMPAIGN_ID, "scene_media": "bad"}), ("assembly", {"campaign_id": CAMPAIGN_ID, "ordered_scene_segments": "bad"})))
def test_malformed_snapshots_fail_closed_without_raising(target: str, payload: dict[str, object]) -> None:
    value = _input()
    artifact = getattr(value.lineage, target).model_copy(update={"payload": payload, "sha256": canonical_sha256(payload)})
    result = evaluate_machine_qa(_with(value, **{target: artifact}))
    assert result.outcome == "FAIL"


def test_advisory_outcome_semantics_are_explicit_not_check_id_based() -> None:
    from app.qa.machine import _outcome
    from app.qa.contracts import MachineQAFinding

    assert _outcome((MachineQAFinding(check_id="anything", outcome="NEEDS_HUMAN", message="review"),)) == "NEEDS_HUMAN"
    clean = evaluate_machine_qa(_input())
    aesthetic = [item for item in clean.findings if item.check_id in {"thumbnail_dominant_idea", "thumbnail_hierarchy"}]
    aesthetic = [item for item in clean.review_findings if item.check_id in {"thumbnail_dominant_idea", "thumbnail_hierarchy"}]
    assert clean.outcome == "NEEDS_HUMAN"
    assert {item.check_id for item in aesthetic} == {
        "thumbnail_dominant_idea", "thumbnail_hierarchy"
    }


def test_required_review_controls_result_outcome_composition() -> None:
    from app.qa.contracts import HumanReviewFinding, MachineQAFinding
    from app.qa.machine import _result_outcome

    passed = (MachineQAFinding(check_id="pass", outcome="PASS", message="pass"),)
    review = (HumanReviewFinding(check_id="review", message="required review"),)
    failed = (MachineQAFinding(check_id="fail", outcome="FAIL", message="fail"),)
    assert _result_outcome(passed, ()) == "PASS"
    assert _result_outcome(passed, review) == "NEEDS_HUMAN"
    assert _result_outcome(failed, review) == "FAIL"
    clean = evaluate_machine_qa(_input())
    assert not any(item.outcome in {"FAIL", "NEEDS_HUMAN"} for item in clean.findings)
    assert clean.review_findings and clean.outcome == "NEEDS_HUMAN"
    assert clean.hook.outcome == "NEEDS_HUMAN"
    assert clean.packaging.outcome == "NEEDS_HUMAN"
    assert all(result.outcome == "NEEDS_HUMAN" for result in clean.packaging.thumbnails)


def test_trivial_rewrite_detection_respects_token_boundaries() -> None:
    from app.qa.machine import _trivial_rewrite

    assert not _trivial_rewrite("cure", "secure")


def test_public_packaging_boundary_marks_unbound_free_form_surface_for_review() -> None:
    from app.qa import evaluate_packaging_qa

    value = _input()
    concept = value.packaging.concepts[0].model_copy(update={"title": "unsupported cure claim"})
    result = evaluate_packaging_qa(value.packaging.model_copy(update={"concepts": (concept, *value.packaging.concepts[1:])}), lineage=value.lineage)
    assert result.outcome == "NEEDS_HUMAN"
    assert any(
        item.check_id == "packaging_surface_truth_gate"
        and item.outcome == "NEEDS_HUMAN"
        for item in result.findings
    )


def test_public_packaging_boundary_rejects_rehashed_forged_topic_grounding() -> None:
    from app.qa import evaluate_packaging_qa

    value = _input()
    topic = dict(value.lineage.topic.payload or {})
    promise = dict(topic["viewer_promise"])  # type: ignore[index]
    promise["viewer_promise"] = "unsupported cure claim"
    topic["viewer_promise"] = promise
    forged_topic = value.lineage.topic.model_copy(
        update={"payload": topic, "sha256": canonical_sha256(topic)}
    )
    forged_lineage = value.lineage.model_copy(update={"topic": forged_topic})
    concept = value.packaging.concepts[0].model_copy(update={"title": "unsupported cure claim"})
    forged_packaging = value.packaging.model_copy(
        update={"concepts": (concept, *value.packaging.concepts[1:])}
    )
    result = evaluate_packaging_qa(forged_packaging, lineage=forged_lineage)

    assert result.outcome == "FAIL"
    check_ids = {item.check_id for item in result.findings}
    assert {"lineage_topic_recompilation", "lineage_gate_identity"} & check_ids
    assert not any(
        item.check_id == "packaging_surface_truth_gate" and item.outcome == "PASS"
        for item in result.findings
    )


@pytest.mark.parametrize("field", ("why_now", "stakes", "novelty", "broad_interest_bridge", "expected_takeaway"))
def test_editorial_premises_are_not_package_truth_authority(field: str) -> None:
    value = _input()
    premise = value.lineage.topic.payload["viewer_promise"][field]  # type: ignore[index]
    concept = value.packaging.concepts[0].model_copy(update={"title": premise})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (concept, *value.packaging.concepts[1:])})}))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "packaging_surface_truth_gate" and item.hard_gate for item in result.findings)


def test_unrelated_accepted_title_requires_hook_fulfillment_review() -> None:
    value = _input()
    claim = next(item for item in value.lineage.research.payload["claims"] if item["claim_type"] == "fact")  # type: ignore[index]
    concept = value.packaging.concepts[0].model_copy(update={"title": claim["assertion_text"]})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (concept, *value.packaging.concepts[1:])})}))
    fulfillment = [item for item in result.hook.findings if item.check_id == "hook_package_fulfillment" and item.evidence == ("title",)]
    assert fulfillment and fulfillment[0].outcome == "NEEDS_HUMAN"
    assert result.outcome != "PASS"


def test_unrelated_accepted_thumbnail_concept_requires_hook_review() -> None:
    value = _input()
    claim = next(item for item in value.lineage.research.payload["claims"] if item["claim_type"] == "fact")  # type: ignore[index]
    concept = value.packaging.concepts[0].model_copy(update={"thumbnail_concept": claim["assertion_text"]})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (concept, *value.packaging.concepts[1:])})}))
    assert not any(item.check_id == "packaging_surface_truth_gate" and item.outcome == "FAIL" and item.evidence[-1] == "thumbnail_concept" for item in result.findings)
    finding = next(item for item in result.hook.findings if item.check_id == "hook_package_fulfillment" and item.evidence == ("thumbnail_concept",))
    assert finding.outcome == "NEEDS_HUMAN"
    assert result.outcome != "PASS"
    clean = evaluate_machine_qa(value)
    assert any(item.check_id == "hook_package_fulfillment" and item.evidence == ("thumbnail_concept",) and item.outcome == "PASS" for item in clean.hook.findings)


def test_trigger_promise_role_swap_is_a_diversity_failure() -> None:
    value = _input()
    first = value.packaging.concepts[0]
    swapped = value.packaging.concepts[1].model_copy(update={"title": first.title, "thumbnail_concept": first.thumbnail_concept, "thumbnail_text": first.thumbnail_text, "viewer_trigger": first.promise, "promise": first.viewer_trigger, "target_audience": first.target_audience, "overclaim_risk": first.overclaim_risk, "implications": first.implications})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (first, swapped, value.packaging.concepts[2])})}))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "packaging_core_positioning_role_swap" for item in result.findings)


def test_genuinely_distinct_truth_authorized_concepts_are_accepted() -> None:
    result = evaluate_machine_qa(_input())
    assert result.packaging.outcome == "NEEDS_HUMAN"
    assert any(item.check_id == "packaging_diversity" and item.outcome == "PASS" for item in result.packaging.findings)
    assert not any(item.check_id in {"packaging_near_duplicate", "packaging_trivial_rewrite_duplicate", "packaging_core_positioning_duplicate", "packaging_core_positioning_role_swap"} and item.outcome == "FAIL" for item in result.packaging.findings)


@pytest.mark.parametrize("claim_type", ("estimate", "opinion", "attributed_claim"))
def test_package_surface_cannot_strip_accepted_claim_framing(claim_type: str) -> None:
    value = _input()
    claim = next(item for item in value.lineage.research.payload["claims"] if item["claim_type"] == claim_type)  # type: ignore[index]
    concept = value.packaging.concepts[0].model_copy(update={"title": claim["assertion_text"]})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (concept, *value.packaging.concepts[1:])})}))
    assert result.outcome == "FAIL"
    assert any(item.check_id == "packaging_surface_truth_gate" and item.hard_gate for item in result.findings)


def test_core_question_requires_full_surface_but_allows_leading_thumbnail_prefix() -> None:
    value = _input()
    core = value.lineage.topic.payload["viewer_promise"]["core_question"]  # type: ignore[index]
    interior = value.packaging.concepts[0].model_copy(update={"title": "workflow support its operational claims"})
    failed = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (interior, *value.packaging.concepts[1:])})}))
    assert failed.outcome == "NEEDS_HUMAN"
    assert any(
        item.check_id == "packaging_surface_truth_gate"
        and item.outcome == "NEEDS_HUMAN"
        and item.evidence[-1] == "title"
        for item in failed.findings
    )
    assert value.packaging.concepts[0].title == core
    assert value.packaging.concepts[0].thumbnail_text == "Can the workflow"


def test_empty_thumbnail_text_skips_hook_fulfillment_and_viewer_promise_is_reviewed() -> None:
    value = _input()
    empty = value.packaging.concepts[0].model_copy(update={"thumbnail_text": ""})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (empty, *value.packaging.concepts[1:]), "thumbnails": (_layout("concept-0", "", empty.thumbnail_concept), *value.packaging.thumbnails[1:])})}))
    assert result.outcome == "NEEDS_HUMAN"
    assert not any(item.check_id == "hook_package_fulfillment" and item.evidence == ("thumbnail_text",) for item in result.hook.findings)
    assert {
        "hook_unnecessary_introduction",
        "hook_information_density",
        "hook_continuation_reason",
        "hook_avoidable_length",
        "thumbnail_dominant_idea",
        "thumbnail_hierarchy",
    } <= {item.check_id for item in result.review_findings}


def test_punctuation_only_thumbnail_text_fails_closed() -> None:
    value = _input()
    punctuated = value.packaging.concepts[0].model_copy(update={"thumbnail_text": "!!!"})
    layout = _layout("concept-0", "!!!")
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (punctuated, *value.packaging.concepts[1:]), "thumbnails": (layout, *value.packaging.thumbnails[1:])})}))
    assert result.outcome == "NEEDS_HUMAN"
    assert any(item.check_id == "packaging_surface_truth_gate" and item.outcome == "NEEDS_HUMAN" and item.evidence[-1] == "thumbnail_text" for item in result.findings)
    assert not any(item.check_id == "hook_package_fulfillment" and item.outcome == "PASS" and item.evidence == ("thumbnail_text",) for item in result.hook.findings)


def test_coherently_rebound_observed_timing_controls_hook_without_other_lineage_failure() -> None:
    value = _input()
    old_duration = float(
        value.lineage.assembly.payload["ordered_scene_segments"][0][  # type: ignore[index,union-attr]
            "observed_duration_seconds"
        ]
    )
    changed = _with_observed_scene_durations(value, {0: 35.0})
    result = evaluate_machine_qa(changed)
    assert next(item for item in result.hook.findings if item.check_id == "hook_first_30_seconds").outcome == "NEEDS_HUMAN"
    assert {
        item.outcome
        for item in result.hook.findings
        if item.check_id in {"hook_core_question", "hook_stakes", "hook_why_now"}
    } == {"NEEDS_HUMAN"}
    assert result.hook.outcome == "NEEDS_HUMAN"
    forbidden = {"assembly_persistence_envelope", "media_scene_validation_lineage", "media_voiceover_lineage", "lineage_gate_identity"}
    assert not any(item.check_id in forbidden and item.outcome == "FAIL" for item in result.findings)
    assert result.outcome == "NEEDS_HUMAN" and old_duration < 30


def test_public_hook_timing_failure_dominates_boundary_uncertainty() -> None:
    candidate = _candidate().model_copy(
        update={
            "core_question": "Can " + "x " * 65 + "workflow hold?",
            "stakes": "Stakes " + "y " * 55 + "hold.",
            "why_now": "Why now phrase retained.",
        }
    )
    value = _input(candidate=candidate)
    viewer_promise = value.lineage.topic.payload["viewer_promise"]  # type: ignore[index,union-attr]
    scenes = value.lineage.storyboard.payload["scenes"]  # type: ignore[index,union-attr]
    assert viewer_promise["core_question"] in scenes[0]["narration"]
    assert viewer_promise["stakes"] in scenes[1]["narration"]
    assert viewer_promise["why_now"] in scenes[2]["narration"]
    changed = _with_observed_scene_durations(value, {0: 20.0, 1: 15.0, 2: 15.0})

    result = evaluate_machine_qa(changed)
    hook = {item.check_id: item for item in result.hook.findings}

    assert hook["hook_core_question"].outcome == "PASS"
    assert hook["hook_stakes"].outcome == "NEEDS_HUMAN"
    assert hook["hook_why_now"].outcome == "FAIL"
    assert hook["hook_required_components_proxy"].outcome == "FAIL"
    assert hook["hook_first_30_seconds"].outcome == "FAIL"
    assert result.outcome == "FAIL"
    assert not any(
        item.outcome == "FAIL" and not item.check_id.startswith("hook_")
        for item in result.findings
    )


def test_portable_wav_and_sora_probe_shapes_are_canonical() -> None:
    from app.qa.machine import _voiceover_probe_valid, _visual_provenance_valid

    assert _voiceover_probe_valid(_wav_probe(8.0))
    assert not _voiceover_probe_valid({"audio_stream": {"codec": "pcm_s16le"}})
    value = _input()
    scene = (value.lineage.storyboard.payload or {})["scenes"][0]  # type: ignore[index]
    artifact = value.lineage.scene_visual_artifacts[0].model_copy(update={"provider_name": "openai_video", "provider_model": "sora-2", "mime_type": "video/mp4"})
    provenance = json.loads(artifact.provenance_json)
    provenance.update({"provider_name": "openai_video", "provider_model": "sora-2", "origin_class": "generated", "source_class": "metered_provider_output", "rights_basis": "generated_illustrative", "disclosure_state": "illustrative_generated_media", "generated_media_is_evidence": False, "claim_hashes": [], "media_validation": _video_probe(8.0)})
    artifact = artifact.model_copy(update={"provenance_json": canonical_json(provenance)})
    assert _visual_provenance_valid(artifact, provenance, scene)
