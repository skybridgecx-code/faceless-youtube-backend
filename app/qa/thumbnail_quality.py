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
    BinaryArtifactSnapshot,
    MachineQAInput,
    MachineQAResult,
    RenderedThumbnailEvidence,
    ThumbnailLayout,
    ThumbnailRenderSpec,
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
