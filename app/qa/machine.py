from __future__ import annotations

import json
import re
from itertools import combinations
from typing import Iterable, Mapping

from app.editorial.claims import (
    claim_hash,
    compile_research_packet,
    evaluate_claim,
    normalize_whitespace,
    source_record_content_sha256,
)
from app.editorial.contracts import (
    ClaimEvaluation,
    ClaimSeed,
    DemandSnapshot,
    EditorialSeed,
    EvidenceSourceInput,
    I4_POLICY_VERSION,
    canonical_json,
    canonical_sha256,
)
from app.editorial.topic_intelligence import compile_topic_packet
from app.editorial.script_compiler import evaluate_script_gate
from app.production.contracts import (
    I5_PRODUCTION_POLICY_VERSION,
    I5_STORYBOARD_CONTRACT_VERSION,
    StoryboardPacket,
)
from app.production.profile import (
    I5_LOCAL_VISUAL_RENDERER_VERSION,
    I5_MAX_GENERATED_CINEMATIC_SCENES,
    I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS,
    I5_MAX_GENERATED_VIDEO_SCENES,
    I5_MAX_GENERATED_VIDEO_SECONDS,
    I5_PROVIDER_CATALOG_VERSION,
    I5_PRODUCTION_PROFILE_KIND,
    I5_RENDERER_CONTRACT_VERSION,
    I5_RENDER_FRAME_RATE,
    I5_RENDER_HEIGHT,
    I5_RENDER_WIDTH,
)
from app.production.storyboard import evaluate_storyboard_gate, narration_sha256

from .contracts import (
    I6_HOOK_MAX_SECONDS,
    I6_MAX_THUMBNAIL_TEXT_CHARACTERS,
    I6_MAX_THUMBNAIL_TEXT_WORDS,
    I6_MIN_THUMBNAIL_TEXT_HEIGHT_PX,
    I6_MIN_THUMBNAIL_TEXT_HEIGHT_RATIO,
    ArtifactSnapshot,
    BinaryArtifactSnapshot,
    CanonicalLineage,
    HookQAResult,
    HumanReviewFinding,
    MachineQAFinding,
    MachineQAInput,
    MachineQAResult,
    PackagingConcept,
    PackagingQAInput,
    PackagingQAResult,
    ThumbnailLayout,
    ThumbnailQAResult,
)


_WORD_PATTERN = re.compile(r"\b[\w'-]+\b")
_I4_KINDS = {
    "topic": ("i4_topic_packet", "topic", "i4-topic-packet-v1"),
    "research": ("i4_research_packet", "research", "i4-research-packet-v1"),
    "script": ("i4_script", "script", "i4-script-v1"),
}
_I5_KINDS = {
    "storyboard": ("i5_storyboard", "storyboard", I5_STORYBOARD_CONTRACT_VERSION),
    "media": ("i5_media_manifest", "media", "i5-media-manifest-v1"),
    "assembly": ("i5_assembly_manifest", "assembly", "i5-assembly-manifest-v1"),
}
_PORTABLE_MEDIA_PROBE_FIELDS = frozenset({"audio_channels", "audio_codec", "audio_sample_rate", "duration_seconds", "format_name", "frame_rate", "has_audio", "has_video", "height", "pixel_format", "video_codec", "width"})


def _outcome(findings: Iterable[MachineQAFinding]) -> str:
    values = tuple(findings)
    if any(finding.outcome == "FAIL" for finding in values):
        return "FAIL"
    if any(finding.outcome == "NEEDS_HUMAN" for finding in values):
        return "NEEDS_HUMAN"
    return "PASS"


def _result_outcome(
    findings: Iterable[MachineQAFinding], reviews: Iterable[HumanReviewFinding]
) -> str:
    machine_outcome = _outcome(findings)
    if machine_outcome != "PASS":
        return machine_outcome
    return "NEEDS_HUMAN" if tuple(reviews) else "PASS"


def _finding(
    check_id: str,
    outcome: str,
    message: str,
    *evidence: str,
    hard_gate: bool = False,
) -> MachineQAFinding:
    return MachineQAFinding(
        check_id=check_id,
        outcome=outcome,  # type: ignore[arg-type]
        message=message,
        hard_gate=hard_gate,
        evidence=tuple(evidence),
    )


def _fail(
    check_id: str, message: str, *evidence: str, hard_gate: bool = False
) -> MachineQAFinding:
    return _finding(check_id, "FAIL", message, *evidence, hard_gate=hard_gate)


def _pass(check_id: str, message: str, *evidence: str) -> MachineQAFinding:
    return _finding(check_id, "PASS", message, *evidence)


def _needs_human(check_id: str, message: str, *evidence: str) -> MachineQAFinding:
    return _finding(check_id, "NEEDS_HUMAN", message, *evidence)


def _review(check_id: str, message: str, *evidence: str) -> HumanReviewFinding:
    return HumanReviewFinding(check_id=check_id, message=message, evidence=tuple(evidence))


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _payload(artifact: ArtifactSnapshot) -> dict[str, object] | None:
    if artifact.payload is None:
        return None
    return dict(artifact.payload)


def _artifact_findings(
    lineage: CanonicalLineage,
) -> tuple[MachineQAFinding, ...]:
    findings: list[MachineQAFinding] = []
    artifacts = {
        "topic": lineage.topic,
        "research": lineage.research,
        "script": lineage.script,
        "storyboard": lineage.storyboard,
        "media": lineage.media,
        "assembly": lineage.assembly,
    }
    for stage, artifact in artifacts.items():
        kind, source_stage, contract_version = (_I4_KINDS | _I5_KINDS)[stage]
        payload = _payload(artifact)
        if (
            artifact.campaign_id != lineage.campaign_id
            or artifact.kind != kind
            or artifact.source_stage != source_stage
            or payload is None
            or not _is_sha256(artifact.sha256)
        ):
            findings.append(
                _fail(
                    "lineage_artifact_envelope",
                    "Artifact identity is invalid.",
                    stage,
                    hard_gate=True,
                )
            )
            continue
        if canonical_sha256(payload) != artifact.sha256:
            findings.append(
                _fail(
                    "lineage_artifact_hash",
                    "JSON artifact hash does not match its canonical payload.",
                    stage,
                    hard_gate=True,
                )
            )
        if (
            payload.get("campaign_id") != lineage.campaign_id
            or payload.get("contract_version") != contract_version
        ):
            findings.append(
                _fail(
                    "lineage_payload_envelope",
                    "Artifact payload has an invalid campaign or contract version.",
                    stage,
                    hard_gate=True,
                )
            )
        required_policy = (
            I4_POLICY_VERSION if stage in _I4_KINDS else I5_PRODUCTION_POLICY_VERSION
        )
        if (stage in _I4_KINDS or stage == "storyboard") and payload.get(
            "policy_version"
        ) != required_policy:
            findings.append(
                _fail(
                    "lineage_policy_version",
                    "Artifact policy version is invalid.",
                    stage,
                    hard_gate=True,
                )
            )
    final = lineage.final_render
    if (
        final.campaign_id != lineage.campaign_id
        or final.kind != "i5_final_render"
        or final.source_stage != "assembly"
        or not _is_sha256(final.sha256)
        or final.payload is not None
    ):
        findings.append(
            _fail(
                "lineage_final_render_envelope",
                "Final-render artifact identity is invalid.",
                hard_gate=True,
            )
        )
    return tuple(findings)


def _binary_provenance(artifact: BinaryArtifactSnapshot) -> dict[str, object] | None:
    try:
        provenance = json.loads(artifact.provenance_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(provenance, dict) or canonical_json(provenance) != artifact.provenance_json:
        return None
    expected = {
        "content_sha256": artifact.sha256,
        "contract_version": "i5-binary-artifact-provenance-v1",
        "hash_scope": "exact_binary_bytes",
        "immutable": True,
        "provider_name": artifact.provider_name,
        "provider_model": artifact.provider_model,
        "prompt_template_version": artifact.prompt_template_version,
    }
    if any(provenance.get(key) != value for key, value in expected.items()):
        return None
    if (
        not _is_sha256(provenance.get("generation_input_hash"))
        or provenance.get("campaign_id") != artifact.campaign_id
        or provenance.get("origin_class")
        not in {"original", "generated"}
        or provenance.get("source_class")
        not in {"deterministic_local_output", "metered_provider_output"}
    ):
        return None
    generated = artifact.provider_name.startswith("openai_")
    if generated != (provenance.get("origin_class") == "generated"):
        return None
    if generated != (provenance.get("source_class") == "metered_provider_output"):
        return None
    return provenance


def _visual_provenance_valid(artifact: BinaryArtifactSnapshot, provenance: Mapping[str, object], scene: Mapping[str, object]) -> bool:
    validation = provenance.get("media_validation")
    if not isinstance(validation, Mapping):
        return False
    provider = artifact.provider_name
    if provider == "deterministic_local":
        return artifact.provider_model == I5_LOCAL_VISUAL_RENDERER_VERSION and artifact.mime_type == "image/png" and provenance.get("visual_mode") == scene.get("visual_mode") and provenance.get("disclosure_state") == scene.get("disclosure_state") and provenance.get("claim_hashes") == scene.get("claim_hashes") and provenance.get("source_keys") == scene.get("source_keys") and isinstance(provenance.get("fallback"), bool) and provenance.get("rights_basis") in {"original_deterministic", "reference_only"} and validation == {"height": 720, "valid": True, "width": 1280}
    if provider == "openai_image":
        metadata = provenance.get("provider_metadata")
        expected_quality = "medium" if artifact.provider_model == "gpt-image-2-medium" else "low"
        expected_reservation = 100000 if expected_quality == "medium" else 25000
        return artifact.provider_model in {"gpt-image-2-medium", "gpt-image-2-low"} and artifact.mime_type == "image/png" and provenance.get("rights_basis") == "generated_illustrative" and provenance.get("disclosure_state") == "illustrative_generated_media" and provenance.get("generated_media_is_evidence") is False and provenance.get("claim_hashes") == [] and isinstance(metadata, Mapping) and set(metadata) == {"model", "output_format", "prompt", "quality", "reservation_microunits", "size"} and metadata.get("model") == "gpt-image-2" and metadata.get("quality") == expected_quality and metadata.get("output_format") == "png" and metadata.get("size") == "1280x720" and metadata.get("reservation_microunits") == expected_reservation and bool(str(metadata.get("prompt") or "").strip()) and validation == {"height": 720, "valid": True, "width": 1280}
    if provider == "openai_video":
        return artifact.provider_model == "sora-2" and artifact.mime_type == "video/mp4" and provenance.get("rights_basis") == "generated_illustrative" and provenance.get("disclosure_state") == "illustrative_generated_media" and provenance.get("generated_media_is_evidence") is False and provenance.get("claim_hashes") == [] and set(validation) == _PORTABLE_MEDIA_PROBE_FIELDS and validation.get("has_video") is True and validation.get("width") == 1280 and validation.get("height") == 720 and 7.5 <= float(validation.get("duration_seconds") or 0) <= 8.5
    return False


def _voiceover_probe_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return set(value) == _PORTABLE_MEDIA_PROBE_FIELDS and value.get("has_audio") is True and value.get("has_video") is False and float(value.get("duration_seconds") or 0) > 0 and str(value.get("format_name") or "").startswith("wav") and value.get("audio_codec") in {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"} and isinstance(value.get("audio_sample_rate"), int) and value["audio_sample_rate"] > 0 and isinstance(value.get("audio_channels"), int) and value["audio_channels"] > 0 and value.get("width") is None and value.get("height") is None and value.get("video_codec") is None and value.get("pixel_format") is None and value.get("frame_rate") is None


def _final_probe_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return set(value) == _PORTABLE_MEDIA_PROBE_FIELDS and value.get("has_video") is True and value.get("has_audio") is True and value.get("width") == 1280 and value.get("height") == 720 and value.get("video_codec") == "h264" and value.get("pixel_format") == "yuv420p" and value.get("audio_codec") == "aac" and abs(float(value.get("frame_rate") or 0) - 30) <= 0.01 and float(value.get("duration_seconds") or 0) > 0 and bool(str(value.get("format_name") or "").strip()) and isinstance(value.get("audio_sample_rate"), int) and value["audio_sample_rate"] > 0 and isinstance(value.get("audio_channels"), int) and value["audio_channels"] > 0


def _profile_findings(lineage: CanonicalLineage, script_hash: str) -> tuple[MachineQAFinding, ...]:
    profile = lineage.production_profile
    payload = _payload(profile)
    if (
        profile.campaign_id != lineage.campaign_id
        or profile.kind != I5_PRODUCTION_PROFILE_KIND
        or profile.source_stage != "storyboard"
        or payload is None
        or canonical_sha256(payload) != profile.sha256
    ):
        return (_fail("production_profile_artifact", "Production-profile artifact identity is invalid.", hard_gate=True),)
    try:
        renderer = dict(payload["renderer"])
        tts = dict(payload["tts"])
        image = dict(payload["generated_image"])
        bounds = dict(payload["generated_media_bounds"])
        storage = dict(payload["storage"])
        video = dict(payload["video"])
        expected_fields = {
            "budget_policy_sha256", "budget_policy_version", "campaign_id",
            "contract_version", "generated_image", "generated_media_bounds",
            "i4_script_sha256", "local_visual_renderer_version",
            "production_policy_version", "provider_catalog_version", "renderer",
            "storage", "tts", "video",
        }
        valid = (
            set(payload) == expected_fields
            and payload.get("campaign_id") == lineage.campaign_id
            and payload.get("contract_version") == "i5-production-profile-v1"
            and payload.get("i4_script_sha256") == script_hash
            and payload.get("production_policy_version") == I5_PRODUCTION_POLICY_VERSION
            and payload.get("budget_policy_version") == "campaign-budget-v1"
            and _is_sha256(payload.get("budget_policy_sha256"))
            and payload.get("local_visual_renderer_version") == I5_LOCAL_VISUAL_RENDERER_VERSION
            and payload.get("provider_catalog_version") == I5_PROVIDER_CATALOG_VERSION
            and image == {"provider":"openai", "model":"gpt-image-2", "size":"1280x720", "primary_quality":"medium", "fallback_quality":"low"}
            and bounds == {"max_cinematic_coverage_seconds": I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS, "max_cinematic_scenes": I5_MAX_GENERATED_CINEMATIC_SCENES, "max_video_scenes": I5_MAX_GENERATED_VIDEO_SCENES, "max_video_total_seconds": I5_MAX_GENERATED_VIDEO_SECONDS}
            and storage == {"root_strategy":"output_dir/canonical_i5/campaign_<id>/objects", "version":"i5-content-addressed-local-v1"}
            and tts.get("response_format") == "wav"
            and tts.get("voice") == "onyx"
            and tts.get("provider") == "openai"
            and tts.get("primary_model") == "tts-1-hd"
            and tts.get("fallback_model") == "tts-1"
            and renderer.get("contract_version") == I5_RENDERER_CONTRACT_VERSION
            and renderer.get("width") == I5_RENDER_WIDTH
            and renderer.get("height") == I5_RENDER_HEIGHT
            and renderer.get("frame_rate") == I5_RENDER_FRAME_RATE
            and bool(str(renderer.get("ffmpeg_version") or "").strip())
            and bool(str(renderer.get("ffprobe_version") or "").strip())
            and video.get("model") == "sora-2" and video.get("size") == "1280x720"
            and video.get("clip_duration_seconds") == 8
            and video.get("provider") in {"disabled", "openai", "sora"}
            and (video.get("provider") == "disabled" or video.get("allow_deprecated_sora") is True)
        )
    except (KeyError, TypeError):
        valid = False
    return () if valid else (_fail("production_profile_payload", "Production-profile payload conflicts with canonical I5 configuration.", hard_gate=True),)


def _i4_input_hash(stage: str, payload: Mapping[str, object]) -> str:
    if stage == "topic":
        identity = {
            "compilation_context": payload["compilation_context"],
            "contract_version": "i4-topic-stage-input-v1",
            "demand_snapshot_hash": payload["demand_snapshot_hash"],
            "policy_version": payload["policy_version"],
            "seed_hash": payload["seed_hash"],
            "stage": stage,
        }
    elif stage == "research":
        identity = {
            "contract_version": "i4-research-stage-input-v1",
            "policy_version": payload["policy_version"],
            "selected_candidate_key": payload["selected_candidate_key"],
            "stage": stage,
            "topic_packet_hash": payload["topic_packet_hash"],
        }
    else:
        identity = {
            "contract_version": "i4-script-stage-input-v1",
            "policy_version": payload["policy_version"],
            "research_packet_hash": payload["research_packet_hash"],
            "stage": stage,
            "topic_packet_hash": payload["topic_packet_hash"],
        }
    return canonical_sha256(identity)


def _i5_input_hash(
    stage: str,
    payload: Mapping[str, object],
    *,
    media_payload: Mapping[str, object] | None = None,
) -> str:
    if stage == "storyboard":
        return canonical_sha256(
            {
                "contract_version": "i5-storyboard-stage-input-v1",
                "production_profile_hash": payload["production_profile_hash"],
                "script_hash": payload["script_hash"],
            }
        )
    if stage == "media":
        voiceover = dict(payload["voiceover"])  # type: ignore[arg-type]
        return canonical_sha256(
            {
                "contract_version": "i5-media-stage-input-v1",
                "media_plan_hash": payload["media_plan_hash"],
                "production_profile_hash": payload["production_profile_hash"],
                "storyboard_hash": payload["storyboard_hash"],
                "voiceover_hash": voiceover["artifact_hash"],
            }
        )
    if media_payload is None:
        raise ValueError("assembly input requires the canonical media manifest")
    voiceover = dict(media_payload["voiceover"])  # type: ignore[arg-type]
    return canonical_sha256(
        {
            "contract_version": "i5-assembly-input-v1",
            "media_manifest_hash": media_payload["_artifact_sha256"],
            "ordered_scene_media": media_payload["scene_media"],
            "production_profile_hash": payload["production_profile_hash"],
            "voiceover_hash": voiceover["artifact_hash"],
        }
    )


def _gate_findings(lineage: CanonicalLineage) -> tuple[MachineQAFinding, ...]:
    findings: list[MachineQAFinding] = []
    artifacts = {
        "topic": lineage.topic,
        "research": lineage.research,
        "script": lineage.script,
        "storyboard": lineage.storyboard,
        "media": lineage.media,
        "assembly": lineage.assembly,
    }
    by_stage = {gate.stage: gate for gate in lineage.gates}
    for stage, artifact in artifacts.items():
        gate = by_stage[stage]
        payload = _payload(artifact)
        try:
            if stage in _I4_KINDS:
                expected_input = _i4_input_hash(stage, payload)
            elif stage == "assembly":
                media_payload = _payload(lineage.media) or {}
                media_with_hash = {
                    **media_payload,
                    "_artifact_sha256": lineage.media.sha256,
                }
                expected_input = _i5_input_hash(
                    stage, payload, media_payload=media_with_hash
                )
            else:
                expected_input = _i5_input_hash(stage, payload)
            reasons = dict(payload["gate"])["reasons"]  # type: ignore[arg-type]
            expected_reasons = canonical_json(reasons)
            embedded_outcome = dict(payload["gate"])["outcome"]  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            findings.append(
                _fail(
                    "lineage_gate_identity",
                    "Gate input identity cannot be reconstructed.",
                    stage,
                    hard_gate=True,
                )
            )
            continue
        expected_policy = (
            I4_POLICY_VERSION if stage in _I4_KINDS else I5_PRODUCTION_POLICY_VERSION
        )
        if (
            gate.campaign_id != lineage.campaign_id
            or gate.outcome != "PASS"
            or gate.policy_version != expected_policy
            or gate.input_hash != expected_input
            or gate.output_hash != artifact.sha256
            or gate.reasons_json != expected_reasons
            or embedded_outcome != "PASS"
        ):
            findings.append(
                _fail(
                    "lineage_gate_identity",
                    "GateDecision does not bind the canonical stage input and output.",
                    stage,
                    hard_gate=True,
                )
            )
    return tuple(findings)


def _claim_seed(record: Mapping[str, object]) -> ClaimSeed:
    return ClaimSeed(
        assertion_text=str(record["assertion_text"]),
        material=bool(record["material"]),
        claim_type=str(record["claim_type"]),  # type: ignore[arg-type]
        role=str(record["role"]),  # type: ignore[arg-type]
        source_keys=tuple(str(value) for value in record["source_keys"]),
        attribution=(
            str(record["attribution"])
            if record.get("attribution") is not None
            else None
        ),
        assumptions=tuple(str(value) for value in record["assumptions"]),
    )


def _recomputed_claims(
    research: Mapping[str, object],
) -> tuple[dict[str, ClaimEvaluation], tuple[MachineQAFinding, ...]]:
    findings: list[MachineQAFinding] = []
    sources: dict[str, EvidenceSourceInput] = {}
    source_uris: set[str] = set()
    try:
        for raw in research["sources"]:  # type: ignore[index]
            source = dict(raw)
            source_key = str(source.get("source_key") or "")
            source_uri = str(source.get("source_uri") or "")
            duplicate = False
            if source_key in sources:
                findings.append(
                    _fail(
                        "research_duplicate_source_key",
                        "Research source_key values must be unique.",
                        source_key,
                        hard_gate=True,
                    )
                )
                duplicate = True
            if source_uri in source_uris:
                findings.append(
                    _fail(
                        "research_duplicate_source_uri",
                        "Research source_uri values must be unique.",
                        source_uri,
                        hard_gate=True,
                    )
                )
                duplicate = True
            if duplicate:
                continue
            if source_record_content_sha256(source) != source.get("content_sha256"):
                findings.append(
                    _fail(
                        "research_source_hash",
                        "Research source content hash is invalid.",
                        str(source.get("source_key")),
                        hard_gate=True,
                    )
                )
                continue
            sources[source_key] = EvidenceSourceInput(
                source_key=source_key,
                source_uri=source_uri,
                publisher=str(source["publisher"]),
                source_class=str(source["source_class"]),  # type: ignore[arg-type]
                evidence_snippet=str(source["evidence_snippet"]),
            )
            source_uris.add(source_uri)
    except (KeyError, TypeError, ValueError) as exc:
        return {}, (
            _fail(
                "research_sources_invalid",
                "Research sources cannot be reconstructed.",
                str(exc),
                hard_gate=True,
            ),
        )
    claims: dict[str, ClaimEvaluation] = {}
    try:
        accepted_records = [dict(raw) for raw in research["claims"]]  # type: ignore[index]
        rejected_records = [dict(raw) for raw in research["rejected_claims"]]  # type: ignore[index]
        seen_hashes: set[str] = set()
        seen_identities: set[str] = set()
        for record, expected_rejected in (
            *((record, False) for record in accepted_records),
            *((record, True) for record in rejected_records),
        ):
            seed = _claim_seed(record)
            recomputed = evaluate_claim(seed, sources)
            if (recomputed.state == "REJECTED") != expected_rejected:
                findings.append(
                    _fail(
                        "research_claim_partition",
                        "Research accepted/rejected claim partitions conflict with recomputed state.",
                        recomputed.claim_hash,
                        hard_gate=True,
                    )
                )
            if (
                recomputed.claim_hash in seen_hashes
                or recomputed.claim_hash in seen_identities
            ):
                findings.append(
                    _fail(
                        "research_duplicate_claim_identity",
                        "Research claim identities must be unique across accepted and rejected partitions.",
                        recomputed.claim_hash,
                        hard_gate=True,
                    )
                )
                continue
            seen_hashes.add(str(record.get("claim_hash") or ""))
            seen_identities.add(recomputed.claim_hash)
            if claim_hash(
                seed
            ) != recomputed.claim_hash or record != recomputed.model_dump(mode="json"):
                findings.append(
                    _fail(
                        "research_claim_evaluation",
                        "Canonical claim identity or evaluation does not recompute.",
                        str(record.get("claim_hash")),
                        hard_gate=True,
                    )
                )
                continue
            claims[recomputed.claim_hash] = recomputed
        counts = {state: 0 for state in ("VERIFIED", "ESTIMATE", "OPINION", "REJECTED")}
        for claim in claims.values():
            counts[claim.state] += 1
        usable_material = sum(
            1
            for claim in claims.values()
            if claim.material and claim.state != "REJECTED"
        )
        verified_material = sum(
            1
            for claim in claims.values()
            if claim.material
            and claim.claim_type in {"fact", "attributed_claim"}
            and claim.state == "VERIFIED"
        )
        expected_summary = {
            **counts,
            "material_usable": usable_material,
            "material_verified_factual": verified_material,
        }
        if research.get("claim_summary") != expected_summary:
            findings.append(
                _fail(
                    "research_claim_summary",
                    "Research claim_summary does not match the recomputed claim partition.",
                    hard_gate=True,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        findings.append(
            _fail(
                "research_claims_invalid",
                "Research claims cannot be reconstructed.",
                str(exc),
                hard_gate=True,
            )
        )
    return claims, tuple(findings)


def _lineage_cross_findings(lineage: CanonicalLineage) -> tuple[MachineQAFinding, ...]:
    findings: list[MachineQAFinding] = []
    topic, research, script = (
        _payload(lineage.topic),
        _payload(lineage.research),
        _payload(lineage.script),
    )
    storyboard, media, assembly = (
        _payload(lineage.storyboard),
        _payload(lineage.media),
        _payload(lineage.assembly),
    )
    if not all((topic, research, script, storyboard, media, assembly)):
        return (
            _fail(
                "lineage_payload_missing",
                "A required canonical payload is missing.",
                hard_gate=True,
            ),
        )
    assert (
        topic is not None
        and research is not None
        and script is not None
        and storyboard is not None
        and media is not None
        and assembly is not None
    )
    topic_hash, research_hash, script_hash = (
        lineage.topic.sha256,
        lineage.research.sha256,
        lineage.script.sha256,
    )
    try:
        seed_payload = lineage.editorial_seed_payload
        seed = EditorialSeed.model_validate({"candidates": seed_payload["candidates"]})
        demand = DemandSnapshot.model_validate(lineage.demand_snapshot_payload)
        context = dict(topic["compilation_context"])
        if (
            seed_payload.get("campaign_id") != lineage.campaign_id
            or seed_payload.get("contract_version") != "i4-editorial-seed-v1"
            or seed_payload.get("requires_youtube_demand") is not True
            or seed.sha256(lineage.campaign_id) != topic.get("seed_hash")
            or canonical_sha256(demand.model_dump(mode="json"))
            != topic.get("demand_snapshot_hash")
            or compile_topic_packet(
                campaign_id=lineage.campaign_id,
                seed_hash=seed.sha256(lineage.campaign_id),
                seed=seed,
                demand=demand,
                channel_niche=str(context["channel_niche"]),
                channel_audience=str(context["channel_audience"]),
                novelty_history=tuple(str(value) for value in context["novelty_history"]),
            )
            != topic
        ):
            findings.append(
                _fail(
                    "lineage_topic_recompilation",
                    "Topic packet does not exactly match deterministic compilation from immutable editorial seed and demand inputs.",
                    hard_gate=True,
                )
            )
    except (KeyError, TypeError, ValueError):
        findings.append(
            _fail(
                "lineage_topic_recompilation",
                "Immutable editorial-seed and demand inputs are insufficient to recompile the topic packet.",
                hard_gate=True,
            )
        )
    if research.get("topic_packet_hash") != topic_hash:
        findings.append(
            _fail(
                "lineage_research_topic",
                "Research does not bind the canonical topic packet.",
                hard_gate=True,
            )
        )
    try:
        seed_payload = lineage.editorial_seed_payload
        seed = EditorialSeed.model_validate({"candidates": seed_payload["candidates"]})
        if (
            seed_payload.get("campaign_id") != lineage.campaign_id
            or seed_payload.get("contract_version") != "i4-editorial-seed-v1"
            or seed_payload.get("requires_youtube_demand") is not True
            or seed.sha256(lineage.campaign_id) != topic.get("seed_hash")
            or compile_research_packet(
                campaign_id=lineage.campaign_id,
                seed=seed,
                topic_packet=topic,
                topic_packet_hash=topic_hash,
            )
            != research
        ):
            findings.append(
                _fail(
                    "lineage_research_recompilation",
                    "Research packet does not exactly match deterministic compilation from the immutable editorial seed and topic packet.",
                    hard_gate=True,
                )
            )
    except (KeyError, TypeError, ValueError):
        findings.append(
            _fail(
                "lineage_research_recompilation",
                "Immutable editorial-seed inputs are insufficient to recompile the research packet.",
                hard_gate=True,
            )
        )
    if (
        script.get("topic_packet_hash") != topic_hash
        or script.get("research_packet_hash") != research_hash
    ):
        findings.append(
            _fail(
                "lineage_script_inputs",
                "Script does not bind canonical topic and research packets.",
                hard_gate=True,
            )
        )
    if script.get("viewer_promise") != topic.get("viewer_promise"):
        findings.append(
            _fail(
                "lineage_viewer_promise",
                "Script Viewer Promise differs from the canonical topic packet.",
                hard_gate=True,
            )
        )
    if storyboard.get("script_hash") != script_hash:
        findings.append(
            _fail(
                "lineage_storyboard_script",
                "Storyboard does not bind the canonical script.",
                hard_gate=True,
            )
        )
    try:
        StoryboardPacket.model_validate(storyboard)
        storyboard_gate = evaluate_storyboard_gate(
            storyboard,
            script_packet=script,
            script_hash=script_hash,
            production_profile_hash=str(
                storyboard.get("production_profile_hash") or ""
            ),
        )
        if (
            storyboard.get("gate") != storyboard_gate
            or storyboard_gate.get("outcome") != "PASS"
        ):
            findings.append(
                _fail(
                    "lineage_storyboard_contract",
                    "Storyboard does not recompute to the canonical accepted gate.",
                    hard_gate=True,
                )
            )
    except (TypeError, ValueError):
        findings.append(
            _fail(
                "lineage_storyboard_contract",
                "Storyboard cannot be reconstructed as the canonical production contract.",
                hard_gate=True,
            )
        )
    profile_hash = storyboard.get("production_profile_hash")
    findings.extend(_profile_findings(lineage, script_hash))
    if profile_hash != lineage.production_profile.sha256:
        findings.append(_fail("production_profile_binding", "Storyboard does not bind the accepted production-profile artifact.", hard_gate=True))
    if media.get("campaign_id") != lineage.campaign_id:
        findings.append(
            _fail(
                "lineage_media_campaign",
                "Media manifest does not bind the canonical campaign.",
                hard_gate=True,
            )
        )
    if (
        media.get("storyboard_hash") != lineage.storyboard.sha256
        or media.get("production_profile_hash") != profile_hash
    ):
        findings.append(
            _fail(
                "lineage_media_storyboard",
                "Media manifest does not bind the canonical storyboard/profile.",
                hard_gate=True,
            )
        )
    storyboard_scenes: list[dict[str, object]] = []
    media_scenes: list[dict[str, object]] = []
    try:
        storyboard_scenes = [dict(scene) for scene in storyboard["scenes"]]  # type: ignore[index]
        media_scenes = [dict(scene) for scene in media["scene_media"]]  # type: ignore[index]
        narration_artifacts = {artifact.kind: artifact for artifact in lineage.scene_narration_artifacts}
        visual_artifacts = {artifact.kind: artifact for artifact in lineage.scene_visual_artifacts}
        expected_positions = list(range(len(storyboard_scenes)))
        actual_positions = [item.get("scene_position") for item in media_scenes]
        if (
            len(media_scenes) != len(storyboard_scenes)
            or actual_positions != expected_positions
            or len(narration_artifacts) != len(storyboard_scenes)
            or len(visual_artifacts) != len(storyboard_scenes)
        ):
            findings.append(
                _fail(
                    "media_scene_cardinality_order",
                    "Media manifest must contain exactly one ordered entry and binary artifact snapshot for every storyboard scene.",
                    hard_gate=True,
                )
            )
        required_manifest_fields = {
            "authorized_cap_microusd",
            "budget_policy_sha256",
            "budget_policy_version",
            "effective_metered_campaign_cost_microusd",
            "generated_media_summary",
            "provider_fallback_summary",
            "reserved_in_flight_cost_microusd",
            "selected_tts_model",
            "soft_warning_active",
            "tts_voice",
            "campaign_id", "contract_version", "integrity", "media_plan_hash", "production_profile_hash", "scene_media", "storyboard_hash", "voiceover", "gate",
        }
        profile_tts = dict((_payload(lineage.production_profile) or {})["tts"])
        if (
            set(media) != required_manifest_fields
            or not _is_sha256(media.get("budget_policy_sha256"))
            or not isinstance(media.get("authorized_cap_microusd"), int)
            or not isinstance(
                media.get("effective_metered_campaign_cost_microusd"), int
            )
            or not isinstance(media.get("reserved_in_flight_cost_microusd"), int)
            or not isinstance(media.get("generated_media_summary"), Mapping)
            or not isinstance(media.get("provider_fallback_summary"), Mapping)
            or not isinstance(media.get("soft_warning_active"), bool)
            or not str(media.get("budget_policy_version") or "").strip()
            or not str(media.get("selected_tts_model") or "").strip()
            or media.get("selected_tts_model") not in {profile_tts.get("primary_model"), profile_tts.get("fallback_model")}
            or not str(media.get("tts_voice") or "").strip()
        ):
            findings.append(
                _fail(
                    "media_persistence_envelope",
                    "Media manifest lacks the canonical I5 persisted budget, provider, and TTS envelope.",
                    hard_gate=True,
                )
            )
        narration_artifact_hashes: list[str] = []
        for position, (scene, item) in enumerate(zip(storyboard_scenes, media_scenes)):
            narration = dict(item["narration"])
            visual = dict(item["visual"])
            narration_hash = narration.get("artifact_hash")
            visual_hash = visual.get("artifact_hash")
            if (
                item.get("scene_position") != position
                or not _is_sha256(narration_hash)
                or not _is_sha256(visual_hash)
                or not isinstance(narration.get("duration_seconds"), (int, float))
                or float(narration["duration_seconds"]) <= 0
                or not str(narration.get("object_uri") or "").strip()
                or narration.get("mime_type") != "audio/wav"
                or not str(visual.get("object_uri") or "").strip()
                or not str(visual.get("mime_type") or "").startswith(
                    ("image/", "video/")
                )
                or visual.get("provider")
                not in {"deterministic_local", "openai_image", "openai_video"}
                or visual.get("visual_mode") != scene.get("visual_mode")
            ):
                findings.append(
                    _fail(
                        "media_scene_structure",
                        "Scene media does not satisfy canonical MIME, provider, visual-mode, or artifact identity constraints.",
                        str(position),
                        hard_gate=True,
                    )
                )
                continue
            narration_artifact_hashes.append(str(narration_hash))
            narration_artifact = narration_artifacts.get(f"i5_scene_narration_{position:03d}")
            visual_artifact = visual_artifacts.get(f"i5_scene_visual_{position:03d}")
            narration_provenance = _binary_provenance(narration_artifact) if narration_artifact else None
            visual_provenance = _binary_provenance(visual_artifact) if visual_artifact else None
            if (
                narration_artifact is None or visual_artifact is None
                or narration_artifact.sha256 != narration_hash or visual_artifact.sha256 != visual_hash
                or narration_artifact.campaign_id != lineage.campaign_id or visual_artifact.campaign_id != lineage.campaign_id
                or narration_artifact.source_stage != "media" or visual_artifact.source_stage != "media"
                or narration_artifact.uri != narration.get("object_uri") or visual_artifact.uri != visual.get("object_uri")
                or narration_artifact.prompt_template_version != "i5-media-request-v1" or visual_artifact.prompt_template_version != "i5-media-request-v1"
                or visual_artifact.provider_name != visual.get("provider")
                or narration_artifact.mime_type != "audio/wav" or narration_artifact.provider_name != "openai_tts"
                or narration_artifact.provider_model != media.get("selected_tts_model")
                or narration_provenance is None or visual_provenance is None
                or narration_provenance.get("narration_sha256") != scene.get("narration_sha256")
                or narration_provenance.get("scene_position") != position
                or narration_provenance.get("storyboard_hash") != lineage.storyboard.sha256
                or narration_provenance.get("production_profile_hash") != profile_hash
                or narration_provenance.get("rights_basis") != "original_i4_narration"
                or narration_provenance.get("disclosure_state") != "not_required"
                or narration_provenance.get("media_validation") != {"audio_stream": True, "format": "wav", "valid": True}
                or narration_provenance.get("duration_seconds") != narration.get("duration_seconds")
                or narration_provenance.get("generation_input_hash")
                != canonical_sha256(
                    {
                        "campaign_id": lineage.campaign_id,
                        "contract_version": "i5-scene-tts-request-v1",
                        "narration": scene.get("narration"),
                        "narration_sha256": scene.get("narration_sha256"),
                        "production_profile_hash": profile_hash,
                        "scene_position": position,
                        "storyboard_hash": lineage.storyboard.sha256,
                        "voice": "onyx",
                    }
                )
                or visual_provenance.get("scene_position") != position
                or visual_provenance.get("storyboard_hash") != lineage.storyboard.sha256
                or visual_provenance.get("production_profile_hash") != profile_hash
                or not _visual_provenance_valid(visual_artifact, visual_provenance, scene)
            ):
                findings.append(
                    _fail(
                        "media_scene_validation_lineage",
                    "Persistence-attested immutable binary artifact provenance does not bind this manifest entry to its storyboard narration and profile.",
                        str(position),
                        hard_gate=True,
                    )
                )
        voiceover = dict(media["voiceover"])
        voiceover_artifact = lineage.voiceover_artifact
        voiceover_provenance = _binary_provenance(voiceover_artifact) if voiceover_artifact else None
        if (
            not _is_sha256(voiceover.get("artifact_hash"))
            or not isinstance(voiceover.get("duration_seconds"), (int, float))
            or float(voiceover["duration_seconds"]) <= 0
            or not str(voiceover.get("object_uri") or "").strip()
            or voiceover_artifact is None or voiceover_artifact.kind != "i5_voiceover"
            or voiceover_artifact.campaign_id != lineage.campaign_id or voiceover_artifact.source_stage != "media"
            or voiceover_artifact.uri != voiceover.get("object_uri") or voiceover_artifact.prompt_template_version != "i5-media-request-v1"
            or voiceover_artifact.mime_type != "audio/wav"
            or voiceover_artifact.provider_name != "ffmpeg_local" or voiceover_artifact.provider_model != "i5-wav-concat-v1"
            or voiceover_artifact.sha256 != voiceover.get("artifact_hash")
            or voiceover_provenance is None
            or voiceover_provenance.get("ordered_scene_audio_hashes") != narration_artifact_hashes
            or voiceover_provenance.get("storyboard_hash") != lineage.storyboard.sha256
            or voiceover_provenance.get("production_profile_hash") != profile_hash
            or voiceover_provenance.get("rights_basis") != "original_i4_narration"
            or voiceover_provenance.get("disclosure_state") != "not_required"
            or not _voiceover_probe_valid(voiceover_provenance.get("media_validation"))
            or voiceover.get("duration_seconds") != voiceover_provenance.get("duration_seconds")
            or abs(float(voiceover_provenance.get("duration_seconds") or 0) - float(dict(voiceover_provenance.get("media_validation") or {}).get("duration_seconds") or 0)) > 0.02
            or abs(float(voiceover_provenance.get("duration_seconds") or 0) - sum(float(dict(item["narration"])["duration_seconds"]) for item in media_scenes)) > max(0.08, len(media_scenes) * 0.01)
            or voiceover_provenance.get("generation_input_hash")
            != canonical_sha256(
                {
                    "contract_version": "i5-voiceover-input-v1",
                    "production_profile_hash": profile_hash,
                    "scene_audio_hashes": narration_artifact_hashes,
                    "storyboard_hash": lineage.storyboard.sha256,
                }
            )
        ):
            findings.append(
                _fail(
                    "media_voiceover_lineage",
                    "Voiceover identity or immutable validation metadata does not bind the ordered scene narration artifacts.",
                    hard_gate=True,
                )
            )
        integrity = dict(media["integrity"])
        if integrity != {
            "all_content_hashes_verified": True,
            "all_objects_inside_canonical_store": True,
            "all_scene_media_present": True,
            "generated_media_is_illustrative_only": True,
            "rights_gate_passed": True,
        } or not _is_sha256(media.get("media_plan_hash")):
            findings.append(
                _fail(
                    "media_manifest_integrity_metadata",
                    "Canonical media manifest integrity metadata or media-plan identity is invalid.",
                    hard_gate=True,
                )
            )
        local_count = sum(
            1 for item in media_scenes if dict(item["visual"]).get("provider") == "deterministic_local"
        )
        image_count = sum(
            1 for item in media_scenes if dict(item["visual"]).get("provider") == "openai_image"
        )
        video_items = [item for item in media_scenes if dict(item["visual"]).get("provider") == "openai_video"]
        video_count = len(video_items)
        generated_seconds = round(sum(float(dict(_binary_provenance(visual_artifacts[f"i5_scene_visual_{int(item['scene_position']):03d}"]) or {}).get("media_validation", {}).get("duration_seconds") or 0) for item in video_items), 6)
        expected_summary = {
            "generated_image_scene_count": image_count,
            "generated_video_scene_count": video_count,
            "generated_video_total_seconds": generated_seconds,
            "local_visual_scene_count": local_count,
        }
        expected_provider_summary = {
            "fallback_scene_count": sum(bool((_binary_provenance(visual_artifacts[f"i5_scene_visual_{position:03d}"]) or {}).get("fallback")) for position in expected_positions),
            "provider_scene_counts": {
                "deterministic_local": local_count,
                "openai_image": image_count,
                "openai_video": video_count,
            },
        }
        if (
            media.get("generated_media_summary") != expected_summary
            or media.get("provider_fallback_summary") != expected_provider_summary
            or int(media["authorized_cap_microusd"]) <= 0
            or int(media["effective_metered_campaign_cost_microusd"]) < 0
            or int(media["reserved_in_flight_cost_microusd"]) < 0
            or int(media["reserved_in_flight_cost_microusd"]) > int(media["effective_metered_campaign_cost_microusd"])
            or int(media["effective_metered_campaign_cost_microusd"]) > int(media["authorized_cap_microusd"])
            or media.get("budget_policy_version") != "campaign-budget-v1"
            or media.get("budget_policy_sha256") != (_payload(lineage.production_profile) or {}).get("budget_policy_sha256")
            or media.get("tts_voice") != "onyx"
            or media.get("gate")
            != {
                "outcome": "PASS",
                "reasons": ["all_scene_media_and_budget_integrity_checks_passed"],
            }
        ):
            findings.append(
                _fail(
                    "media_recomputed_summary",
                    "Media manifest deterministic counts, budget invariants, TTS identity, or canonical gate payload are invalid.",
                    hard_gate=True,
                )
            )
    except (KeyError, TypeError, ValueError):
        findings.append(
            _fail(
                "media_manifest_structure",
                "Media manifest lacks canonical scene-media or immutable validation structure.",
                hard_gate=True,
            )
        )
    if (
        assembly.get("script_hash") != script_hash
        or assembly.get("storyboard_hash") != lineage.storyboard.sha256
        or assembly.get("media_manifest_hash") != lineage.media.sha256
        or assembly.get("production_profile_hash") != profile_hash
        or assembly.get("final_render_sha256") != lineage.final_render.sha256
    ):
        findings.append(
            _fail(
                "lineage_assembly_inputs",
                "Assembly does not bind the canonical script, storyboard, media, and render.",
                hard_gate=True,
            )
        )
    try:
        required_assembly_fields = {
            "campaign_id", "command_spec", "contract_version",
            "expected_duration_seconds", "ffmpeg_version", "ffprobe_version",
            "final_render_byte_size", "final_render_object_uri", "final_render_sha256",
            "gate", "media_manifest_hash", "observed_duration_seconds",
            "ordered_scene_segments", "production_profile_hash", "script_hash",
            "storyboard_hash", "stream_validation",
        }
        renderer = dict((_payload(lineage.production_profile) or {})["renderer"])
        segments = [dict(item) for item in assembly["ordered_scene_segments"]]  # type: ignore[index]
        command_spec = dict(assembly["command_spec"])
        final_artifact = lineage.final_render_artifact
        final_provenance = _binary_provenance(final_artifact) if final_artifact else None
        expected_duration = sum(float(dict(item["narration"])["duration_seconds"]) for item in media_scenes)
        valid_segments = (
            len(segments) == len(media_scenes)
            and [segment.get("scene_position") for segment in segments] == list(range(len(media_scenes)))
            and command_spec.get("scene_segments") == [segment.get("command") for segment in segments]
            and isinstance(command_spec.get("final_assembly"), Mapping)
            and all(
                segment.get("narration_hash") == dict(media_scenes[index]["narration"]).get("artifact_hash")
                and segment.get("visual_hash") == dict(media_scenes[index]["visual"]).get("artifact_hash")
                and _is_sha256(segment.get("segment_sha256"))
                and segment.get("visual_kind")
                == ("video" if str(dict(media_scenes[index]["visual"]).get("mime_type")).startswith("video/") else "image")
                and isinstance(segment.get("observed_duration_seconds"), (int, float))
                and float(segment["observed_duration_seconds"]) > 0
                and abs(float(segment["observed_duration_seconds"]) - float(dict(media_scenes[index]["narration"])["duration_seconds"])) <= 0.15
                for index, segment in enumerate(segments)
            )
        )
        valid = (
            set(assembly) == required_assembly_fields
            and assembly.get("campaign_id") == lineage.campaign_id
            and assembly.get("contract_version") == "i5-assembly-manifest-v1"
            and assembly.get("ffmpeg_version") == renderer.get("ffmpeg_version")
            and assembly.get("ffprobe_version") == renderer.get("ffprobe_version")
            and isinstance(assembly.get("expected_duration_seconds"), (int, float))
            and isinstance(assembly.get("observed_duration_seconds"), (int, float))
            and float(assembly["expected_duration_seconds"]) > 0
            and float(assembly["observed_duration_seconds"]) > 0
            and abs(float(assembly["expected_duration_seconds"]) - expected_duration) <= 0.000001
            and abs(float(assembly["observed_duration_seconds"]) - expected_duration) <= max(0.20, len(media_scenes) * 0.08)
            and isinstance(assembly.get("final_render_byte_size"), int)
            and int(assembly["final_render_byte_size"]) > 0
            and bool(str(assembly.get("final_render_object_uri") or "").strip())
            and _final_probe_valid(assembly.get("stream_validation"))
            and assembly.get("gate") == {"outcome": "PASS", "reasons": ["canonical_render_and_ffprobe_validation_passed"]}
            and valid_segments
            and final_artifact is not None
            and final_artifact.kind == "i5_final_render"
            and final_artifact.campaign_id == lineage.campaign_id
            and final_artifact.source_stage == "assembly"
            and final_artifact.prompt_template_version == "i5-media-request-v1"
            and final_artifact.mime_type == "video/mp4"
            and final_artifact.provider_name == "ffmpeg_local"
            and final_artifact.provider_model == "i5-ffmpeg-renderer-v1"
            and final_artifact.sha256 == assembly.get("final_render_sha256")
            and final_artifact.byte_size == assembly.get("final_render_byte_size")
            and final_artifact.uri == assembly.get("final_render_object_uri")
            and final_provenance is not None
            and final_provenance.get("media_manifest_hash") == lineage.media.sha256
            and final_provenance.get("production_profile_hash") == profile_hash
            and final_provenance.get("rights_basis") == "assembled_canonical_i5_media"
            and final_provenance.get("disclosure_state") == "not_required"
            and final_provenance.get("media_validation") == assembly.get("stream_validation")
            and final_provenance.get("generation_input_hash")
            == _i5_input_hash("assembly", assembly, media_payload={**media, "_artifact_sha256": lineage.media.sha256})
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        findings.append(
            _fail(
                "assembly_persistence_envelope",
                "Assembly manifest or final-render immutable provenance does not match the canonical I5 structural envelope.",
                hard_gate=True,
            )
        )
    return tuple(findings)


def _hook_qa(lineage: CanonicalLineage, selected: PackagingConcept) -> HookQAResult:
    findings: list[MachineQAFinding] = []
    reviews: list[HumanReviewFinding] = []
    covered: list[dict[str, object]] = []
    opening_text = ""
    required_text: list[str] = []
    continuation_metadata_bound = False
    component_states: dict[str, str] = {}
    timing_measurement_valid = False
    complete_window_seconds = 0.0
    topic, research, script = (
        _payload(lineage.topic),
        _payload(lineage.research),
        _payload(lineage.script),
    )
    if topic is None or research is None or script is None:
        finding = _fail(
            "hook_canonical_inputs_missing",
            "Canonical topic, research, and script payloads are required.",
            hard_gate=True,
        )
        return HookQAResult(outcome="FAIL", findings=(finding,), review_findings=())
    recomputed_gate = evaluate_script_gate(script, research_packet=research)
    if (
        script.get("gate") != recomputed_gate
        or recomputed_gate.get("outcome") != "PASS"
    ):
        findings.append(
            _fail(
                "hook_script_gate",
                "Script gate does not recompute to the canonical PASS result.",
                hard_gate=True,
            )
        )
        return HookQAResult(outcome="FAIL", findings=tuple(findings), review_findings=())
    sections = [
        dict(section)
        for section in script.get("sections", [])
        if isinstance(section, Mapping)
    ]
    cold_open = next(
        (section for section in sections if section.get("section_id") == "cold_open"),
        None,
    )
    viewer_promise = topic.get("viewer_promise")
    if not isinstance(cold_open, dict) or not isinstance(viewer_promise, Mapping):
        findings.append(
            _fail(
                "hook_cold_open_missing",
                "Canonical cold_open and Viewer Promise are required.",
                hard_gate=True,
            )
        )
        return HookQAResult(outcome="FAIL", findings=tuple(findings), review_findings=())
    narration = str(cold_open.get("narration") or "")
    required = ("core_question", "stakes", "why_now")
    if (
        any(str(viewer_promise.get(field) or "") not in narration for field in required)
        or not str(cold_open.get("continuation_reason") or "").strip()
    ):
        findings.append(
            _fail(
                "hook_cold_open_contract",
                "Canonical cold_open lacks required deterministic promise structure.",
                hard_gate=True,
            )
        )
    else:
        findings.append(
            _pass(
                "hook_cold_open_contract",
                "Canonical cold_open contains the compiler-required question, stakes, and why-now narration plus continuation metadata.",
            )
        )
    storyboard = _payload(lineage.storyboard)
    assembly = _payload(lineage.assembly)
    try:
        scenes = sorted(
            (dict(scene) for scene in storyboard["scenes"]),  # type: ignore[index]
            key=lambda scene: int(scene["position"]),
        )
        if not scenes or [int(scene["position"]) for scene in scenes] != list(range(len(scenes))):
            raise ValueError("storyboard scenes are not ordered")
        segments = [dict(segment) for segment in assembly["ordered_scene_segments"]]  # type: ignore[index]
        if [segment.get("scene_position") for segment in segments] != list(range(len(scenes))):
            raise ValueError("assembly segments are not ordered")
        elapsed = 0.0
        boundary_scenes: list[dict[str, object]] = []
        timed_scenes: list[tuple[dict[str, object], float, float]] = []
        for position, scene in enumerate(scenes):
            duration = float(segments[position]["observed_duration_seconds"])
            if duration <= 0 or scene.get("narration_sha256") != narration_sha256(str(scene["narration"])):
                raise ValueError("scene timing or narration identity is invalid")
            scene_start = elapsed
            scene_end = scene_start + duration
            timed_scenes.append((scene, scene_start, scene_end))
            if scene_end <= I6_HOOK_MAX_SECONDS:
                covered.append(scene)
                complete_window_seconds = scene_end
            elif scene_start < I6_HOOK_MAX_SECONDS < scene_end:
                boundary_scenes.append(scene)
            elapsed = scene_end
        if scenes[0].get("section_id") != "cold_open":
            raise ValueError("opening coverage does not begin with the cold open")
        opening_text = " ".join(str(scene["narration"]) for scene in covered)
        required_text = [str(viewer_promise[field]) for field in required]
        continuation_metadata_bound = bool(
            str(cold_open.get("continuation_reason") or "").strip()
        )
        timing_measurement_valid = True
    except (IndexError, KeyError, TypeError, ValueError):
        findings.append(
            _fail(
                "hook_duration_measurement",
                "Canonical storyboard opening timing is unavailable or unbound.",
                hard_gate=True,
            )
        )
    else:
        for field, text in zip(required, required_text):
            in_window = any(text in str(scene["narration"]) for scene in covered)
            boundary_uncertain = any(
                text in str(scene["narration"]) for scene in boundary_scenes
            )
            outside_window = any(
                text in str(scene["narration"])
                for scene, scene_start, _scene_end in timed_scenes
                if scene_start >= I6_HOOK_MAX_SECONDS
            )
            if in_window:
                state = "IN_WINDOW"
                finding = _pass(
                    f"hook_{field}",
                    "Required canonical component occurs in a scene wholly before the 30-second boundary.",
                    field,
                    state,
                )
            elif boundary_uncertain:
                state = "BOUNDARY_UNCERTAIN"
                finding = _needs_human(
                    f"hook_{field}",
                    "Required canonical component occurs only in a boundary-crossing scene; scene-level timing cannot resolve it.",
                    field,
                    state,
                )
            elif outside_window:
                state = "OUTSIDE_WINDOW"
                finding = _fail(
                    f"hook_{field}",
                    "Required canonical component occurs only after the 30-second boundary.",
                    field,
                    state,
                    hard_gate=True,
                )
            else:
                state = "ABSENT"
                finding = _fail(
                    f"hook_{field}",
                    "Required canonical component is absent from observed assembly narration.",
                    field,
                    state,
                    hard_gate=True,
                )
            component_states[field] = state
            findings.append(finding)
    findings.append(
        _pass(
            "hook_semantic_scope",
            "Pass derives from the accepted canonical cold-open contract, not WPM or free-form semantic inference.",
        )
    )
    package_surfaces = tuple((name, value) for name, value in (("title", selected.title), ("thumbnail_concept", selected.thumbnail_concept), ("thumbnail_text", selected.thumbnail_text), ("promise", selected.promise)) if value)
    for field_name, value in package_surfaces:
        tokens = tuple(_normalized(value).split())
        proven = bool(tokens) and _contains_token_slice(tuple(_normalized(opening_text).split()), tokens)
        if proven:
            findings.append(_pass("hook_package_fulfillment", "Selected package surface is literally present in observed opening material.", field_name))
        else:
            findings.append(_needs_human("hook_package_fulfillment", "Selected package surface is not literally present in observed opening material; semantic fulfillment requires review.", field_name))
    if not timing_measurement_valid or not component_states:
        component_outcome = "FAIL"
    elif any(
        state in {"OUTSIDE_WINDOW", "ABSENT"}
        for state in component_states.values()
    ):
        component_outcome = "FAIL"
    elif any(
        state == "BOUNDARY_UNCERTAIN" for state in component_states.values()
    ):
        component_outcome = "NEEDS_HUMAN"
    else:
        component_outcome = "PASS"
    component_evidence = tuple(
        f"{field}={component_states.get(field, 'UNMEASURED')}" for field in required
    )
    component_summary = (
        _pass(
            "hook_first_30_seconds",
            "Complete ordered canonical scenes within the measurable 30-second window prove the cold-open structure.",
            f"complete_opening_coverage_seconds={complete_window_seconds:.2f}",
            f"window_seconds={I6_HOOK_MAX_SECONDS:.2f}",
            *component_evidence,
        )
        if component_outcome == "PASS"
        else (
            _needs_human(
                "hook_first_30_seconds",
                "A required canonical component occurs in a boundary-crossing scene; scene-level timing cannot resolve it.",
                *component_evidence,
            )
            if component_outcome == "NEEDS_HUMAN"
            else _fail(
                "hook_first_30_seconds",
                "At least one required canonical component is absent or occurs only after the 30-second boundary.",
                *component_evidence,
                hard_gate=True,
            )
        )
    )
    required_components = (
        _pass(
            "hook_required_components_proxy",
            "Required canonical components are mechanically covered.",
            *component_evidence,
        )
        if component_outcome == "PASS"
        else (
            _needs_human(
                "hook_required_components_proxy",
                "A required component is boundary-uncertain and no required component is deterministically late or absent.",
                *component_evidence,
            )
            if component_outcome == "NEEDS_HUMAN"
            else _fail(
                "hook_required_components_proxy",
                "Required canonical components are not mechanically covered within the first 30 seconds.",
                *component_evidence,
                hard_gate=True,
            )
        )
    )
    observed_coverage = (
        _pass(
            "hook_observed_window_coverage",
            "Observed complete-scene timing fits the review window.",
            *component_evidence,
        )
        if component_outcome == "PASS"
        else (
            _needs_human(
                "hook_observed_window_coverage",
                "A required component is boundary-uncertain and no required component is deterministically late or absent.",
                *component_evidence,
            )
            if component_outcome == "NEEDS_HUMAN"
            else _fail(
                "hook_observed_window_coverage",
                "Observed opening timing does not cover every required canonical component.",
                *component_evidence,
                hard_gate=True,
            )
        )
    )
    findings.extend((
        component_summary,
        _pass("hook_promise_fulfillment", "Selected package promise is literally present in observed opening material.") if _contains_token_slice(tuple(_normalized(opening_text).split()), tuple(_normalized(selected.promise).split())) else _needs_human("hook_promise_fulfillment", "Selected promise needs semantic fulfillment review."),
        _pass("hook_direct_start_proxy", "Opening begins with canonical cold-open framing.") if opening_text.startswith(str(viewer_promise.get("core_question") or "")) else _needs_human("hook_direct_start_proxy", "Direct-start proxy cannot be mechanically established."),
        required_components,
        _pass("hook_continuation_metadata", "Continuation metadata is bound to the canonical cold-open section.") if continuation_metadata_bound else _fail("hook_continuation_metadata", "Canonical cold-open continuation metadata is missing."),
        observed_coverage,
    ))
    # The preceding checks establish only canonical presence, ordering, and
    # complete-scene timing. They cannot establish editorial quality.
    reviews.extend(
        (
            _review(
                "hook_unnecessary_introduction",
                "Structural opening evidence cannot determine whether an introduction is artistically unnecessary.",
            ),
            _review(
                "hook_information_density",
                "Canonical component coverage cannot determine information density or engagement.",
            ),
            _review(
                "hook_continuation_reason",
                "Continuation metadata is present, but continuation quality requires editorial review.",
            ),
            _review(
                "hook_avoidable_length",
                "A bounded timing window cannot determine whether the hook length is editorially optimal.",
            ),
        )
    )
    canonical_viewer_promise = str(viewer_promise.get("viewer_promise") or "")
    if _contains_token_slice(tuple(_normalized(opening_text).split()), tuple(_normalized(canonical_viewer_promise).split())):
        findings.append(_pass("hook_viewer_promise_fulfillment", "Canonical Viewer Promise is literally present in observed opening material."))
    else:
        reviews.append(_review("hook_viewer_promise_fulfillment", "Canonical Viewer Promise is not literally present in observed opening material; semantic fulfillment requires review."))
    frozen = tuple(findings)
    return HookQAResult(outcome=_result_outcome(frozen, reviews), findings=frozen, review_findings=tuple(reviews))  # type: ignore[arg-type]


def _normalized(value: str) -> str:
    return " ".join(_WORD_PATTERN.findall(value.casefold()))


def _contains_token_slice(source: tuple[str, ...], candidate: tuple[str, ...]) -> bool:
    return bool(candidate) and any(source[index:index + len(candidate)] == candidate for index in range(len(source) - len(candidate) + 1))


def _surface_truth_grounded(value: str, field_name: str, core_question: str) -> bool:
    normalized = _normalized(value)
    if not value.strip():
        return field_name == "thumbnail_text"
    if not normalized:
        return False
    if field_name == "thumbnail_text":
        candidate = tuple(normalized.split())
        question = tuple(_normalized(core_question).split())
        return question[:len(candidate)] == candidate
    return normalized == _normalized(core_question)


def _exact_claim_matches(
    value: str, claims: Mapping[str, ClaimEvaluation]
) -> tuple[ClaimEvaluation, ...]:
    """Return canonical claims whose full rendered or assertion text is exact."""
    normalized = normalize_whitespace(value)
    if not normalized:
        return ()
    return tuple(
        claim
        for claim in claims.values()
        if normalized
        in {
            normalize_whitespace(_expected_implication_statement(claim)),
            normalize_whitespace(claim.assertion_text),
        }
    )


def _surface_truth_finding(
    concept: PackagingConcept,
    field_name: str,
    value: str,
    *,
    claims: Mapping[str, ClaimEvaluation],
    canonical_sources: tuple[str, ...],
    core_question: str,
    editorial_premises: tuple[str, ...],
) -> MachineQAFinding | None:
    if not value.strip() and field_name == "thumbnail_text":
        return None
    exact_claims = _exact_claim_matches(value, claims)
    rejected = tuple(
        claim
        for claim in exact_claims
        if claim.state == "REJECTED" and claim.material
    )
    if rejected:
        return _fail(
            "packaging_surface_truth_gate",
            "Public package-facing text exactly matches a canonical rejected material claim.",
            concept.concept_id,
            field_name,
            *(claim.claim_hash for claim in rejected),
            hard_gate=True,
        )
    if _normalized(value) in {_normalized(premise) for premise in editorial_premises} or _mechanically_strips_claim_framing(value, canonical_sources):
        return _fail(
            "packaging_surface_truth_gate",
            "Public package-facing text mechanically strips required editorial or claim framing.",
            concept.concept_id,
            field_name,
            hard_gate=True,
        )
    accepted = tuple(claim for claim in exact_claims if claim.state != "REJECTED")
    if accepted or _surface_truth_grounded(value, field_name, core_question):
        return _pass(
            "packaging_surface_truth_gate",
            "Public package-facing text is an exact canonical surface.",
            concept.concept_id,
            field_name,
            *(claim.claim_hash for claim in accepted),
        )
    return _needs_human(
        "packaging_surface_truth_gate",
        "Non-extractive package-facing text is not mechanically entailed or disproven by canonical evidence.",
        concept.concept_id,
        field_name,
    )


def _mechanically_strips_claim_framing(
    value: str, canonical_sources: tuple[str, ...]
) -> bool:
    candidate = _normalized(value)
    for source in canonical_sources:
        normalized = _normalized(source)
        if normalized.startswith("estimate "):
            body, _, assumptions = normalized.partition(" assumptions ")
            if candidate == body.removeprefix("estimate ") or candidate == assumptions:
                return True
        elif normalized.startswith("opinion "):
            _, _, body = normalized.partition(" ")
            if candidate == body:
                return True
        if " states " in normalized:
            _, _, body = normalized.partition(" states ")
            if candidate == body:
                return True
    return False


def _layout_findings(
    layout: ThumbnailLayout,
    expected_text: str,
) -> ThumbnailQAResult:
    findings: list[MachineQAFinding] = []
    reviews: list[HumanReviewFinding] = []
    if canonical_sha256(layout.hash_payload()) != layout.layout_spec_sha256:
        findings.append(
            _fail(
                "thumbnail_layout_spec_hash",
                "Raw layout does not bind to its canonical layout-spec hash; this is not rendered-image proof.",
                layout.concept_id,
            )
        )
    rectangles = (
        *(element.bounds for element in layout.text_elements),
        *(element.bounds for element in layout.visual_elements),
    )
    if any(
        rectangle.x + rectangle.width > layout.canvas_width_px
        or rectangle.y + rectangle.height > layout.canvas_height_px
        for rectangle in rectangles
    ):
        findings.append(
            _fail(
                "thumbnail_layout_bounds",
                "Raw layout geometry exceeds the declared thumbnail canvas.",
                layout.concept_id,
            )
        )
    actual_text = " ".join(item.text for item in layout.text_elements)
    words = len(_WORD_PATTERN.findall(actual_text))
    if _normalized(actual_text) != _normalized(expected_text):
        findings.append(
            _fail(
                "thumbnail_text_binding",
                "Raw thumbnail text does not match its packaging concept.",
                layout.concept_id,
            )
        )
    elif (
        words > I6_MAX_THUMBNAIL_TEXT_WORDS
        or len(actual_text) > I6_MAX_THUMBNAIL_TEXT_CHARACTERS
    ):
        findings.append(
            _fail(
                "thumbnail_minimal_text",
                "Thumbnail text exceeds deterministic limits.",
                layout.concept_id,
            )
        )
    else:
        findings.append(
            _pass(
                "thumbnail_minimal_text",
                "Raw thumbnail text meets deterministic limits.",
                layout.concept_id,
            )
        )
    if actual_text:
        smallest = min(item.font_height_px for item in layout.text_elements)
        ratio = smallest / layout.canvas_height_px
        contrast = min(item.contrast_ratio for item in layout.text_elements)
        if (
            smallest < I6_MIN_THUMBNAIL_TEXT_HEIGHT_PX
            or ratio < I6_MIN_THUMBNAIL_TEXT_HEIGHT_RATIO
            or contrast < 4.5
        ):
            findings.append(
                _fail(
                    "thumbnail_text_readability",
                    "Raw text measurements fail the deterministic size or contrast floor.",
                    layout.concept_id,
                )
            )
        else:
            findings.append(
                _pass(
                    "thumbnail_text_readability",
                    "Raw text measurements meet deterministic size and contrast floors.",
                    layout.concept_id,
                )
            )
    if layout.visual_elements:
        areas = tuple(item.bounds.area() for item in layout.visual_elements)
        ordered_areas = sorted(areas, reverse=True)
        if len(ordered_areas) > 1 and ordered_areas[0] >= ordered_areas[1] * 2:
            findings.append(
                _pass(
                    "thumbnail_dominant_area_proxy",
                    "Raw layout geometry establishes only a dominant-area proxy.",
                )
            )

    reviews.extend(
        (
            _review(
                "thumbnail_dominant_idea",
                "Raw geometry cannot prove a semantic dominant idea or thumbnail communicative effectiveness.",
            ),
            _review(
                "thumbnail_hierarchy",
                "Raw geometry cannot prove meaningful visual hierarchy or artistic clarity.",
            ),
        )
    )
    frozen = tuple(findings)
    return ThumbnailQAResult(
        concept_id=layout.concept_id, outcome=_result_outcome(frozen, reviews), findings=frozen, review_findings=tuple(reviews)
    )  # type: ignore[arg-type]


def _concept_signature(concept: PackagingConcept) -> tuple[str, ...]:
    return (
        _normalized(concept.title),
        _normalized(concept.thumbnail_concept),
        _normalized(concept.thumbnail_text),
        _normalized(concept.viewer_trigger),
        _normalized(concept.promise),
        _normalized(concept.target_audience),
        concept.overclaim_risk,
        canonical_json(
            sorted(
                canonical_json(
                    {
                        "statement": _normalized(implication.statement),
                        "claim_hashes": sorted(implication.claim_hashes),
                    }
                )
                for implication in concept.implications
            )
        ),
    )


def _trivial_rewrite(left: str, right: str) -> bool:
    left_tokens, right_tokens = _normalized(left).split(), _normalized(right).split()
    if left_tokens == right_tokens:
        return True
    difference = abs(len(left_tokens) - len(right_tokens))
    if difference > 2:
        return False
    shorter, longer = (left_tokens, right_tokens) if len(left_tokens) <= len(right_tokens) else (right_tokens, left_tokens)
    return _contains_token_slice(tuple(longer), tuple(shorter))


def _expected_implication_statement(claim: ClaimEvaluation) -> str:
    if claim.claim_type == "fact":
        return claim.assertion_text
    if claim.claim_type == "attributed_claim":
        return f"{claim.attribution} states: {claim.assertion_text}"
    if claim.claim_type == "estimate":
        assumptions = "; ".join(claim.assumptions)
        return f"Estimate: {claim.assertion_text} Assumptions: {assumptions}"
    return f"Opinion: {claim.assertion_text}"


def _implication_truth_finding(
    concept: PackagingConcept,
    implication_statement: str,
    cited_claim_hash: str,
    claims: Mapping[str, ClaimEvaluation],
) -> MachineQAFinding:
    claim = claims.get(cited_claim_hash)
    if claim is None:
        return _fail(
            "packaging_truth_gate",
            "Implication cites a claim hash that is absent from the complete canonical claim set.",
            concept.concept_id,
            cited_claim_hash,
            hard_gate=True,
        )
    if claim.state == "REJECTED":
        return _fail(
            "packaging_truth_gate",
            "Implication cites a canonical rejected claim.",
            concept.concept_id,
            cited_claim_hash,
            hard_gate=True,
        )
    exact_claims = _exact_claim_matches(implication_statement, claims)
    rejected = tuple(item for item in exact_claims if item.state == "REJECTED")
    if rejected:
        return _fail(
            "packaging_truth_gate",
            "Implication exactly matches a canonical rejected claim regardless of its cited lineage.",
            concept.concept_id,
            cited_claim_hash,
            *(item.claim_hash for item in rejected),
            hard_gate=True,
        )
    if exact_claims and all(item.claim_hash != cited_claim_hash for item in exact_claims):
        return _fail(
            "packaging_truth_gate",
            "Implication exactly matches a different canonical claim than its cited lineage.",
            concept.concept_id,
            cited_claim_hash,
            *(item.claim_hash for item in exact_claims),
            hard_gate=True,
        )
    expected = _expected_implication_statement(claim)
    if normalize_whitespace(implication_statement) == normalize_whitespace(expected):
        return _pass(
            "packaging_truth_gate",
            "Implication is an exact normalized extraction of a recomputed accepted claim.",
            concept.concept_id,
            claim.claim_hash,
        )
    if (
        claim.claim_type != "fact"
        and normalize_whitespace(implication_statement)
        == normalize_whitespace(claim.assertion_text)
    ):
        return _fail(
            "packaging_truth_gate",
            "Implication mechanically strips required attribution, estimate, or opinion framing.",
            concept.concept_id,
            hard_gate=True,
        )
    return _needs_human(
        "packaging_truth_gate",
        "Non-extractive implication is not mechanically entailed or disproven by its cited canonical claim.",
        concept.concept_id,
        claim.claim_hash,
    )


def evaluate_thumbnail_qa(
    value: ThumbnailLayout, expected_text: str
) -> ThumbnailQAResult:
    return _layout_findings(value, expected_text)


def _evaluate_packaging_qa_bound(
    value: PackagingQAInput, *, claims: Mapping[str, ClaimEvaluation] | None = None,
    canonical_sources: tuple[str, ...] | None = None, core_question: str = "",
    editorial_premises: tuple[str, ...] = (),
) -> PackagingQAResult:
    findings: list[MachineQAFinding] = []
    claims = claims or {}
    if len(value.concepts) < 3:
        findings.append(
            _fail(
                "packaging_minimum_distinct_concepts",
                "At least three packaging concepts are required.",
            )
        )
    for left, right in combinations(value.concepts, 2):
        if (_normalized(left.viewer_trigger), _normalized(left.promise)) == (
            _normalized(right.viewer_trigger),
            _normalized(right.promise),
        ):
            findings.append(
                _fail(
                    "packaging_core_positioning_duplicate",
                    "Concepts share the same normalized viewer trigger and promise.",
                    left.concept_id,
                    right.concept_id,
                )
            )
        if sorted((_normalized(left.viewer_trigger), _normalized(left.promise))) == sorted((_normalized(right.viewer_trigger), _normalized(right.promise))):
            findings.append(_fail("packaging_core_positioning_role_swap", "Concepts reuse the same trigger/promise core positioning with roles swapped.", left.concept_id, right.concept_id))
        if _concept_signature(left) == _concept_signature(right):
            findings.append(
                _fail(
                    "packaging_full_positioning_duplicate",
                    "Concepts duplicate the complete deterministic positioning signature.",
                    left.concept_id,
                    right.concept_id,
                )
            )
        left_signature = _concept_signature(left)
        right_signature = _concept_signature(right)
        matching_fields = sum(
            left_field == right_field
            for left_field, right_field in zip(left_signature, right_signature)
        )
        if matching_fields >= len(left_signature) - 1:
            findings.append(
                _fail(
                    "packaging_near_duplicate",
                    "Changing only one full-positioning field cannot create a distinct concept.",
                    left.concept_id,
                    right.concept_id,
                )
            )
        textual_fields = (
            (left.title, right.title), (left.thumbnail_concept, right.thumbnail_concept),
            (left.thumbnail_text, right.thumbnail_text), (left.viewer_trigger, right.viewer_trigger),
            (left.promise, right.promise), (left.target_audience, right.target_audience),
        )
        substantive_same = sum(_trivial_rewrite(a, b) for a, b in textual_fields) + int(left.overclaim_risk == right.overclaim_risk) + int(left_signature[-1] == right_signature[-1])
        if substantive_same >= 7:
            findings.append(_fail("packaging_trivial_rewrite_duplicate", "Trivial phrase-level rewrites do not create distinct positioning.", left.concept_id, right.concept_id))
    if not any(
        finding.check_id.startswith("packaging_") and finding.outcome == "FAIL"
        for finding in findings
    ):
        findings.append(
            _pass(
                "packaging_diversity",
                "Concepts have distinct full positioning signatures.",
            )
        )
    for concept in value.concepts:
        if canonical_sources is not None:
            for field_name in ("title", "thumbnail_concept", "thumbnail_text", "viewer_trigger", "promise"):
                field_value = getattr(concept, field_name)
                finding = _surface_truth_finding(
                    concept,
                    field_name,
                    field_value,
                    claims=claims,
                    canonical_sources=canonical_sources,
                    core_question=core_question,
                    editorial_premises=editorial_premises,
                )
                if finding is not None:
                    findings.append(finding)
        for implication in concept.implications:
            findings.append(
                _implication_truth_finding(
                    concept,
                    implication.statement,
                    implication.claim_hashes[0],
                    claims,
                )
            )
    layouts = {layout.concept_id: layout for layout in value.thumbnails}
    results: list[ThumbnailQAResult] = []
    for concept in value.concepts:
        layout = layouts.get(concept.concept_id)
        if layout is None:
            findings.append(
                _needs_human(
                    "thumbnail_layout_missing",
                    "Raw thumbnail layout is unavailable.",
                    concept.concept_id,
                )
            )
        else:
            results.append(
                _layout_findings(
                    layout,
                    concept.thumbnail_text,
                )
            )
    for result in results:
        findings.extend(result.findings)
    reviews = tuple(review for result in results for review in result.review_findings)
    frozen = tuple(findings)
    return PackagingQAResult(
        outcome=_result_outcome(frozen, reviews), findings=frozen, thumbnails=tuple(results), review_findings=reviews
    )  # type: ignore[arg-type]


def evaluate_packaging_qa(value: PackagingQAInput, *, lineage: CanonicalLineage) -> PackagingQAResult:
    """Public package QA boundary; canonical lineage is revalidated in-memory."""
    try:
        lineage_findings = (
            *_artifact_findings(lineage),
            *_gate_findings(lineage),
            *_lineage_cross_findings(lineage),
        )
        if any(finding.outcome == "FAIL" for finding in lineage_findings):
            return PackagingQAResult(
                outcome="FAIL", findings=lineage_findings, thumbnails=()
            )
        research = _payload(lineage.research) or {}
        claims, claim_findings = _recomputed_claims(research)
        if any(finding.outcome == "FAIL" for finding in claim_findings):
            return PackagingQAResult(outcome="FAIL", findings=claim_findings, thumbnails=())
        topic = _payload(lineage.topic) or {}
        viewer_promise = topic.get("viewer_promise")
        if not isinstance(viewer_promise, Mapping):
            raise ValueError("canonical Viewer Promise is missing")
        sources = tuple(_expected_implication_statement(claim) for claim in claims.values() if claim.state != "REJECTED")
        return _evaluate_packaging_qa_bound(value, claims=claims, canonical_sources=sources, core_question=str(viewer_promise.get("core_question") or ""), editorial_premises=tuple(str(viewer_promise.get(field) or "") for field in ("why_now", "stakes", "novelty", "broad_interest_bridge", "expected_takeaway")))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        finding = _fail("packaging_canonical_evidence_invalid", "Canonical lineage cannot establish package truth.", hard_gate=True)
        return PackagingQAResult(outcome="FAIL", findings=(finding,), thumbnails=())


def evaluate_machine_qa(value: MachineQAInput) -> MachineQAResult:
    """Pure verification of supplied canonical payload snapshots; no persistence or I/O."""

    try:
        lineage_findings = (*_artifact_findings(value.lineage), *_gate_findings(value.lineage), *_lineage_cross_findings(value.lineage))
        research = _payload(value.lineage.research) or {}
        claims, claim_findings = _recomputed_claims(research)
        selected = next(concept for concept in value.packaging.concepts if concept.concept_id == value.packaging.selected_concept_id)
        topic = _payload(value.lineage.topic) or {}
        viewer_promise = topic.get("viewer_promise")
        sources = tuple(_expected_implication_statement(claim) for claim in claims.values() if claim.state != "REJECTED")
        hook = _hook_qa(value.lineage, selected)
        packaging = _evaluate_packaging_qa_bound(value.packaging, claims=claims, canonical_sources=sources, core_question=str((viewer_promise or {}).get("core_question") or ""), editorial_premises=tuple(str((viewer_promise or {}).get(field) or "") for field in ("why_now", "stakes", "novelty", "broad_interest_bridge", "expected_takeaway")))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, StopIteration) as exc:
        failure = _fail("machine_qa_malformed_snapshot", "Malformed canonical snapshot prevented deterministic reconstruction.", type(exc).__name__, hard_gate=True)
        hook = HookQAResult(outcome="FAIL", findings=(failure,))
        packaging = PackagingQAResult(outcome="FAIL", findings=(failure,), thumbnails=())
        lineage_findings, claim_findings = (failure,), ()
    findings = (*lineage_findings, *claim_findings, *hook.findings, *packaging.findings)
    reviews = (*hook.review_findings, *packaging.review_findings)
    return MachineQAResult(
        outcome=_result_outcome(findings, reviews),
        findings=findings,
        hook=hook,
        packaging=packaging,
        input_sha256=value.input_sha256(),
        review_findings=reviews,
    )  # type: ignore[arg-type]
