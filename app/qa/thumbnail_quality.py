"""Pure I6-B2B1 evidence construction for deterministic thumbnail pixels."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping

from pydantic import ValidationError

from app.editorial.contracts import canonical_sha256
from app.production.thumbnail_renderer import (
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    ThumbnailRenderError,
    decode_png,
    render_thumbnail,
)

from .contracts import (
    I6_THUMBNAIL_RENDERER_VERSION,
    I6_THUMBNAIL_CRITIC_CHECK_IDS,
    BinaryArtifactSnapshot,
    HumanReviewFinding,
    MachineQAFinding,
    MachineQAInput,
    MachineQAResult,
    RenderedThumbnailEvidence,
    ResolvedThumbnailCriticJudgment,
    ThumbnailCriticArtifact,
    ThumbnailCriticJudgment,
    ThumbnailCriticRequest,
    ThumbnailCriticResolution,
    ThumbnailCriticTarget,
    ThumbnailLayout,
    ThumbnailRenderSpec,
    revalidate_rendered_thumbnail_evidence,
    revalidate_thumbnail_critic_artifact,
    revalidate_thumbnail_critic_request,
    revalidate_thumbnail_render_spec,
)
from .machine import evaluate_machine_qa


_WORD_PATTERN = re.compile(r"\b[\w'-]+\b")


class ThumbnailEvidenceError(ValueError):
    """Canonical input, source binding, or rendered output verification failed."""


@dataclass(frozen=True, slots=True)
class RenderedThumbnail:
    evidence: RenderedThumbnailEvidence
    png_bytes: bytes


def build_rendered_thumbnail_evidence(
    machine_input: MachineQAInput,
    machine_result: MachineQAResult,
    render_spec: ThumbnailRenderSpec,
    source_png_bytes: Mapping[str, bytes],
) -> RenderedThumbnail:
    """Return immutable evidence and pixels only from exact canonical inputs."""

    machine_input = _revalidate_machine_input(machine_input)
    canonical_result = evaluate_machine_qa(machine_input)
    if machine_result != canonical_result:
        raise ThumbnailEvidenceError("caller-supplied MachineQAResult is stale")
    machine_input_sha256 = machine_input.input_sha256()
    if canonical_result.input_sha256 != machine_input_sha256:
        raise ThumbnailEvidenceError("canonical MachineQAResult input hash is invalid")
    if _machine_result_has_failure(canonical_result):
        raise ThumbnailEvidenceError("failed MachineQAResult cannot mint thumbnail evidence")
    try:
        render_spec = revalidate_thumbnail_render_spec(render_spec)
    except ValueError as exc:
        raise ThumbnailEvidenceError(str(exc)) from exc

    concept = next(
        (
            candidate
            for candidate in machine_input.packaging.concepts
            if candidate.concept_id == render_spec.concept_id
        ),
        None,
    )
    if concept is None:
        raise ThumbnailEvidenceError("render spec references an unknown concept")
    layout = _layout_for_concept(machine_input, render_spec.concept_id)
    layout_sha256 = canonical_sha256(layout.hash_payload())
    if layout.layout_spec_sha256 != layout_sha256:
        raise ThumbnailEvidenceError("layout hash is invalid")
    if render_spec.layout_spec_sha256 != layout_sha256:
        raise ThumbnailEvidenceError("render spec layout hash is stale")
    if _normalized_layout_text(layout) != _normalized_text(concept.thumbnail_text):
        raise ThumbnailEvidenceError("layout text does not bind packaging thumbnail text")

    _validate_visual_indexes(layout, render_spec)
    _validate_source_bindings(machine_input, render_spec, source_png_bytes)
    try:
        rendered = render_thumbnail(layout, render_spec, source_png_bytes)
        decoded = decode_png(rendered.png_bytes)
    except ThumbnailRenderError as exc:
        raise ThumbnailEvidenceError(str(exc)) from exc
    png_sha256 = hashlib.sha256(rendered.png_bytes).hexdigest()
    if (
        rendered.png_sha256 != png_sha256
        or rendered.width != THUMBNAIL_WIDTH
        or rendered.height != THUMBNAIL_HEIGHT
        or rendered.mime_type != "image/png"
        or rendered.renderer_version != I6_THUMBNAIL_RENDERER_VERSION
        or decoded.width != THUMBNAIL_WIDTH
        or decoded.height != THUMBNAIL_HEIGHT
    ):
        raise ThumbnailEvidenceError("deterministic renderer output metadata is invalid")

    source_hashes = tuple(
        sorted({binding.source_artifact_sha256 for binding in render_spec.visual_bindings})
    )
    evidence = RenderedThumbnailEvidence(
        machine_input_sha256=machine_input_sha256,
        machine_result_sha256=canonical_result.sha256(),
        campaign_id=machine_input.lineage.campaign_id,
        concept_id=concept.concept_id,
        layout_spec_sha256=layout_sha256,
        render_spec_sha256=render_spec.sha256(),
        source_artifact_sha256s=source_hashes,
        png_sha256=png_sha256,
        byte_size=len(rendered.png_bytes),
        renderer_version=rendered.renderer_version,
    )
    return RenderedThumbnail(evidence=evidence, png_bytes=rendered.png_bytes)


def build_rendered_thumbnail(
    machine_input: MachineQAInput,
    machine_result: MachineQAResult,
    render_spec: ThumbnailRenderSpec,
    source_png_bytes: Mapping[str, bytes],
) -> RenderedThumbnail:
    """Compatibility-friendly name for the sole pure B2B1 evidence builder."""

    return build_rendered_thumbnail_evidence(
        machine_input,
        machine_result,
        render_spec,
        source_png_bytes,
    )


def _layout_for_concept(
    machine_input: MachineQAInput, concept_id: str
) -> ThumbnailLayout:
    layouts = tuple(
        layout
        for layout in machine_input.packaging.thumbnails
        if layout.concept_id == concept_id
    )
    if len(layouts) != 1:
        raise ThumbnailEvidenceError("concept must have exactly one thumbnail layout")
    return layouts[0]


def _validate_source_bindings(
    machine_input: MachineQAInput,
    render_spec: ThumbnailRenderSpec,
    source_png_bytes: Mapping[str, bytes],
) -> None:
    expected_hashes = {
        binding.source_artifact_sha256 for binding in render_spec.visual_bindings
    }
    if set(source_png_bytes) != expected_hashes:
        raise ThumbnailEvidenceError("source byte mapping does not exactly match bindings")
    artifacts_by_sha: dict[str, tuple[BinaryArtifactSnapshot, ...]] = {}
    for artifact in machine_input.lineage.scene_visual_artifacts:
        artifacts_by_sha.setdefault(artifact.sha256, tuple())
        artifacts_by_sha[artifact.sha256] = (*artifacts_by_sha[artifact.sha256], artifact)
    for source_sha256 in expected_hashes:
        artifacts = artifacts_by_sha.get(source_sha256, ())
        if len(artifacts) != 1:
            raise ThumbnailEvidenceError("source SHA does not identify one canonical artifact")
        artifact = artifacts[0]
        if (
            artifact.campaign_id != machine_input.lineage.campaign_id
            or artifact.mime_type != "image/png"
        ):
            raise ThumbnailEvidenceError("source artifact is not a canonical campaign PNG")
        source_bytes = source_png_bytes[source_sha256]
        if not isinstance(source_bytes, bytes):
            raise ThumbnailEvidenceError("source PNG values must be immutable bytes")
        if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
            raise ThumbnailEvidenceError("source PNG bytes do not match canonical SHA")
        if artifact.byte_size != len(source_bytes):
            raise ThumbnailEvidenceError("source PNG byte size does not match canonical artifact")
        try:
            decoded = decode_png(source_bytes)
            provenance = json.loads(artifact.provenance_json)
        except (json.JSONDecodeError, ThumbnailRenderError) as exc:
            raise ThumbnailEvidenceError("source PNG provenance or bytes are invalid") from exc
        validation = provenance.get("media_validation") if isinstance(provenance, dict) else None
        if (
            not isinstance(validation, dict)
            or validation.get("valid") is not True
            or type(validation.get("width")) is not int  # noqa: E721
            or type(validation.get("height")) is not int  # noqa: E721
            or validation.get("width") != decoded.width
            or validation.get("height") != decoded.height
            or decoded.width != THUMBNAIL_WIDTH
            or decoded.height != THUMBNAIL_HEIGHT
        ):
            raise ThumbnailEvidenceError(
                "source PNG dimensions do not match immutable media validation"
            )


def _revalidate_machine_input(machine_input: MachineQAInput) -> MachineQAInput:
    """Reject unvalidated or normalizing model_copy-mutated QA inputs."""

    try:
        revalidated = MachineQAInput.model_validate(
            machine_input.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValidationError) as exc:
        raise ThumbnailEvidenceError(
            "machine input fails runtime validation"
        ) from exc
    if revalidated != machine_input:
        raise ThumbnailEvidenceError(
            "machine input changes during runtime validation"
        )
    return revalidated


def _validate_visual_indexes(
    layout: ThumbnailLayout, render_spec: ThumbnailRenderSpec
) -> None:
    indexes = tuple(binding.visual_index for binding in render_spec.visual_bindings)
    if indexes != tuple(range(len(layout.visual_elements))):
        raise ThumbnailEvidenceError("visual bindings do not exactly cover layout slots")


def _machine_result_has_failure(result: MachineQAResult) -> bool:
    findings = (
        *result.findings,
        *result.hook.findings,
        *result.packaging.findings,
        *(finding for thumbnail in result.packaging.thumbnails for finding in thumbnail.findings),
    )
    return result.outcome == "FAIL" or any(
        finding.outcome == "FAIL" for finding in findings
    )


def _normalized_layout_text(layout: ThumbnailLayout) -> str:
    return _normalized_text(" ".join(element.text for element in layout.text_elements))


def _normalized_text(value: str) -> str:
    return " ".join(_WORD_PATTERN.findall(value.casefold()))


class ThumbnailCriticError(ValueError):
    """B2B2 evidence, authority, or resolution validation failed before mutation."""


def _critic_revalidate_input(value: MachineQAInput) -> MachineQAInput:
    try:
        rebuilt = MachineQAInput.model_validate(value.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError) as exc:
        raise ThumbnailCriticError("machine input fails runtime validation") from exc
    if rebuilt != value:
        raise ThumbnailCriticError("machine input changes during runtime validation")
    return rebuilt


def _critic_revalidate_result(value: MachineQAResult) -> MachineQAResult:
    try:
        rebuilt = MachineQAResult.model_validate(value.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError) as exc:
        raise ThumbnailCriticError("machine result fails runtime validation") from exc
    if rebuilt != value:
        raise ThumbnailCriticError("machine result changes during runtime validation")
    return rebuilt


def _critic_findings(result: MachineQAResult) -> tuple[MachineQAFinding, ...]:
    """Return the canonical flattened machine findings without recomposition."""

    return result.findings


def _critic_target(
    *,
    check_id: str,
    finding: HumanReviewFinding,
    input_hash: str,
    result_hash: str,
    evidence: RenderedThumbnailEvidence,
    evidence_hash: str,
) -> ThumbnailCriticTarget:
    payload = {
        "check_id": check_id,
        "machine_input_sha256": input_hash,
        "machine_result_sha256": result_hash,
        "evidence_sha256": evidence_hash,
        "campaign_id": evidence.campaign_id,
        "concept_id": evidence.concept_id,
        "layout_spec_sha256": evidence.layout_spec_sha256,
        "render_spec_sha256": evidence.render_spec_sha256,
        "source_artifact_sha256s": evidence.source_artifact_sha256s,
        "png_sha256": evidence.png_sha256,
        "png_byte_size": evidence.byte_size,
        "mime_type": evidence.mime_type,
        "width": evidence.width,
        "height": evidence.height,
        "renderer_version": evidence.renderer_version,
        "original_human_finding": finding.model_dump(mode="json"),
    }
    return ThumbnailCriticTarget(target_id=canonical_sha256(payload), **payload)


def build_thumbnail_critic_request(
    machine_input: MachineQAInput,
    machine_result: MachineQAResult,
    evidence: RenderedThumbnailEvidence,
) -> ThumbnailCriticRequest:
    """Mint the sole two-target request from revalidated B2B1 evidence."""

    machine_input = _critic_revalidate_input(machine_input)
    machine_result = _critic_revalidate_result(machine_result)
    try:
        evidence = revalidate_rendered_thumbnail_evidence(evidence)
    except ValueError as exc:
        raise ThumbnailCriticError("rendered thumbnail evidence fails runtime validation") from exc
    canonical_result = evaluate_machine_qa(machine_input)
    if canonical_result != machine_result:
        raise ThumbnailCriticError("caller-supplied MachineQAResult is stale")
    if _machine_result_has_failure(machine_result):
        raise ThumbnailCriticError("deterministic Machine QA failure blocks thumbnail critic")

    input_hash, result_hash = machine_input.input_sha256(), machine_result.sha256()
    if (
        machine_result.input_sha256,
        evidence.machine_input_sha256,
        evidence.machine_result_sha256,
        evidence.campaign_id,
    ) != (input_hash, input_hash, result_hash, machine_input.lineage.campaign_id):
        raise ThumbnailCriticError("rendered evidence machine or campaign identity is invalid")
    selected = machine_input.packaging.selected_concept_id
    if evidence.concept_id != selected:
        raise ThumbnailCriticError("rendered evidence does not bind selected packaging concept")
    concepts = [item for item in machine_input.packaging.concepts if item.concept_id == selected]
    layouts = [item for item in machine_input.packaging.thumbnails if item.concept_id == selected]
    results = [item for item in machine_result.packaging.thumbnails if item.concept_id == selected]
    if len(concepts) != 1 or len(layouts) != 1 or len(results) != 1:
        raise ThumbnailCriticError("selected thumbnail authority must be unique")
    layout_hash = canonical_sha256(layouts[0].hash_payload())
    if layouts[0].layout_spec_sha256 != layout_hash or evidence.layout_spec_sha256 != layout_hash:
        raise ThumbnailCriticError("rendered evidence layout identity is invalid")
    if (
        evidence.mime_type,
        evidence.width,
        evidence.height,
        evidence.renderer_version,
    ) != ("image/png", THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT, I6_THUMBNAIL_RENDERER_VERSION):
        raise ThumbnailCriticError("rendered evidence has unsupported PNG metadata")
    reviews = results[0].review_findings
    if (
        tuple(finding.check_id for finding in reviews) != I6_THUMBNAIL_CRITIC_CHECK_IDS
        or any(finding.review_status != "NEEDS_HUMAN" for finding in reviews)
    ):
        raise ThumbnailCriticError("selected thumbnail has invalid human-review authority")
    evidence_hash = evidence.sha256()
    targets = tuple(
        _critic_target(
            check_id=check_id,
            finding=finding,
            input_hash=input_hash,
            result_hash=result_hash,
            evidence=evidence,
            evidence_hash=evidence_hash,
        )
        for check_id, finding in zip(I6_THUMBNAIL_CRITIC_CHECK_IDS, reviews, strict=True)
    )
    return ThumbnailCriticRequest(
        machine_input_sha256=input_hash,
        machine_result_sha256=result_hash,
        evidence=evidence,
        evidence_sha256=evidence_hash,
        targets=targets,  # type: ignore[arg-type]
    )


def _require_critic_coverage(
    request: ThumbnailCriticRequest,
    judgments: tuple[ThumbnailCriticJudgment, ThumbnailCriticJudgment],
) -> None:
    if tuple((item.target_id, item.check_id) for item in judgments) != tuple(
        (target.target_id, target.check_id) for target in request.targets
    ):
        raise ThumbnailCriticError("thumbnail critic judgments do not exactly cover canonical targets")


def resolve_thumbnail_critic(
    *,
    machine_input: MachineQAInput,
    machine_result: MachineQAResult,
    request: ThumbnailCriticRequest,
    artifact: ThumbnailCriticArtifact,
) -> ThumbnailCriticResolution:
    """Resolve only selected target findings; canonical Machine QA remains unchanged."""

    machine_input = _critic_revalidate_input(machine_input)
    machine_result = _critic_revalidate_result(machine_result)
    try:
        request = revalidate_thumbnail_critic_request(request)
        artifact = revalidate_thumbnail_critic_artifact(artifact)
    except ValueError as exc:
        raise ThumbnailCriticError("thumbnail critic request or artifact fails runtime validation") from exc
    if machine_result != evaluate_machine_qa(machine_input):
        raise ThumbnailCriticError("caller-supplied MachineQAResult is stale")
    if (
        request.machine_input_sha256,
        request.machine_result_sha256,
        artifact.request_sha256,
        artifact.machine_input_sha256,
        artifact.machine_result_sha256,
        artifact.evidence_sha256,
    ) != (
        machine_input.input_sha256(),
        machine_result.sha256(),
        request.sha256(),
        request.machine_input_sha256,
        request.machine_result_sha256,
        request.evidence_sha256,
    ):
        raise ThumbnailCriticError("thumbnail critic artifact is not bound to canonical request")
    _require_critic_coverage(request, artifact.judgments)
    selected = request.evidence.concept_id
    selected_results = [item for item in machine_result.packaging.thumbnails if item.concept_id == selected]
    if (
        len(selected_results) != 1
        or selected_results[0].review_findings
        != tuple(target.original_human_finding for target in request.targets)
    ):
        raise ThumbnailCriticError("thumbnail critic target authority no longer matches human review")

    judgments_by_target = {item.target_id: item for item in artifact.judgments}
    # MachineQAResult.review_findings is the canonical flattened order.  Remove
    # only the two selected-thumbnail positions; equality alone is insufficient
    # because other packaging concepts can legitimately carry the same finding.
    remaining_human = list(machine_result.review_findings)
    selected_index = next(
        index
        for index, thumbnail in enumerate(machine_result.packaging.thumbnails)
        if thumbnail.concept_id == selected
    )
    selected_reviews = machine_result.packaging.thumbnails[selected_index].review_findings
    review_offset = len(machine_result.hook.review_findings) + sum(
        len(thumbnail.review_findings)
        for thumbnail in machine_result.packaging.thumbnails[:selected_index]
    )
    if (
        selected_reviews != tuple(target.original_human_finding for target in request.targets)
        or tuple(remaining_human[review_offset : review_offset + len(selected_reviews)])
        != selected_reviews
    ):
        raise ThumbnailCriticError("thumbnail critic human-review order is invalid")
    remove_indexes = {
        review_offset + index
        for index, target in enumerate(request.targets)
        if judgments_by_target[target.target_id].outcome != "NEEDS_HUMAN"
    }
    remaining_human = [
        finding for index, finding in enumerate(remaining_human) if index not in remove_indexes
    ]

    remaining_machine = [finding for finding in _critic_findings(machine_result) if finding.outcome != "PASS"]
    resolved: list[ResolvedThumbnailCriticJudgment] = []
    for target, judgment in zip(request.targets, artifact.judgments, strict=True):
        resolved.append(
            ResolvedThumbnailCriticJudgment(
                target_id=target.target_id,
                check_id=target.check_id,
                outcome=judgment.outcome,
                rationale=judgment.rationale,
                visual_observations=judgment.visual_observations,
            )
        )
        if judgment.outcome == "FAIL":
            remaining_machine.append(
                MachineQAFinding(
                    check_id=target.check_id,
                    outcome="FAIL",
                    message=judgment.rationale,
                    hard_gate=False,
                    evidence=judgment.visual_observations,
                )
            )
    outcome: str = (
        "FAIL"
        if any(finding.outcome == "FAIL" for finding in remaining_machine)
        else "NEEDS_HUMAN"
        if remaining_human or remaining_machine
        else "PASS"
    )
    return ThumbnailCriticResolution(
        machine_result_sha256=machine_result.sha256(),
        request_sha256=request.sha256(),
        artifact_sha256=artifact.sha256(),
        resolved_judgments=tuple(resolved),  # type: ignore[arg-type]
        remaining_human_review_findings=tuple(remaining_human),
        remaining_machine_findings=tuple(remaining_machine),
        outcome=outcome,  # type: ignore[arg-type]
    )
