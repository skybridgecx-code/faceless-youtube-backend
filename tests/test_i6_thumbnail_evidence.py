from __future__ import annotations

import hashlib
import json
import struct
import zlib

import pytest

import app.qa.thumbnail_quality as thumbnail_quality
from app.editorial.contracts import canonical_json, canonical_sha256
from app.qa.contracts import (
    ThumbnailLayout,
    ThumbnailRenderSpec,
    VisualSourceBinding,
)
from app.qa.machine import evaluate_machine_qa
from app.qa.thumbnail_quality import (
    ThumbnailEvidenceError,
    build_rendered_thumbnail_evidence,
)
from tests.test_i6_machine_qa import _gate, _input


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(
    color: tuple[int, int, int], *, width: int = 1280, height: int = 720
) -> bytes:
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _layout_with_hash(layout: ThumbnailLayout, **updates: object) -> ThumbnailLayout:
    raw = layout.model_dump(mode="json")
    raw.update(updates)
    raw.pop("layout_spec_sha256")
    return ThumbnailLayout(**raw, layout_spec_sha256=canonical_sha256(raw))


def _bound_input(
    *, source_width: int = 1280, source_height: int = 720
):  # type: ignore[no-untyped-def]
    value = _input()
    sources = (
        _png((20, 80, 160), width=source_width, height=source_height),
        _png((160, 40, 20), width=source_width, height=source_height),
    )
    source_sha256s = tuple(
        hashlib.sha256(source).hexdigest() for source in sources
    )
    rebound_visuals = []
    for artifact, source, source_sha256 in zip(
        value.lineage.scene_visual_artifacts[:2],
        sources,
        source_sha256s,
        strict=True,
    ):
        provenance = json.loads(artifact.provenance_json)
        provenance["content_sha256"] = source_sha256
        rebound_visuals.append(
            artifact.model_copy(
                update={
                    "byte_size": len(source),
                    "provenance_json": canonical_json(provenance),
                    "sha256": source_sha256,
                    "uri": f"object://{artifact.kind}/{source_sha256}",
                }
            )
        )
    media = dict(value.lineage.media.payload or {})
    scene_media = [dict(item) for item in media["scene_media"]]
    for item, visual in zip(scene_media[:2], rebound_visuals, strict=True):
        manifest_visual = dict(item["visual"])
        manifest_visual.update({"artifact_hash": visual.sha256, "object_uri": visual.uri})
        item["visual"] = manifest_visual
    media["scene_media"] = scene_media
    media_artifact = value.lineage.media.model_copy(
        update={"payload": media, "sha256": canonical_sha256(media)}
    )
    final = value.lineage.final_render_artifact
    assert final is not None
    final_sha256 = canonical_sha256({"final": media_artifact.sha256})
    final_provenance = json.loads(final.provenance_json)
    final_provenance.update(
        {
            "content_sha256": final_sha256,
            "generation_input_hash": canonical_sha256(
                {
                    "contract_version": "i5-assembly-input-v1",
                    "media_manifest_hash": media_artifact.sha256,
                    "ordered_scene_media": scene_media,
                    "production_profile_hash": value.lineage.production_profile.sha256,
                    "voiceover_hash": value.lineage.voiceover_artifact.sha256,
                }
            ),
            "media_manifest_hash": media_artifact.sha256,
        }
    )
    final = final.model_copy(
        update={
            "provenance_json": canonical_json(final_provenance),
            "sha256": final_sha256,
            "uri": f"object://{final.kind}/{final_sha256}",
        }
    )
    assembly = dict(value.lineage.assembly.payload or {})
    segments = [dict(item) for item in assembly["ordered_scene_segments"]]
    for segment, visual in zip(segments[:2], rebound_visuals, strict=True):
        segment["visual_hash"] = visual.sha256
    assembly.update(
        {
            "final_render_object_uri": final.uri,
            "final_render_sha256": final.sha256,
            "media_manifest_hash": media_artifact.sha256,
            "ordered_scene_segments": segments,
        }
    )
    assembly_artifact = value.lineage.assembly.model_copy(
        update={"payload": assembly, "sha256": canonical_sha256(assembly)}
    )
    rebound = value.lineage.model_copy(
        update={
            "assembly": assembly_artifact,
            "final_render": value.lineage.final_render.model_copy(
                update={"sha256": final.sha256}
            ),
            "final_render_artifact": final,
            "media": media_artifact,
            "scene_visual_artifacts": (
                *rebound_visuals,
                *value.lineage.scene_visual_artifacts[2:],
            ),
        }
    )
    gates = tuple(
        _gate(
            stage,
            getattr(rebound, stage),
            getattr(rebound, stage).payload or {},
            media,
        )
        for stage in ("topic", "research", "script", "storyboard", "media", "assembly")
    )
    value = value.model_copy(
        update={"lineage": rebound.model_copy(update={"gates": gates})}
    )
    first_layout = value.packaging.thumbnails[0]
    text = first_layout.text_elements[0].model_copy(update={"font_height_px": 32})
    first_layout = _layout_with_hash(
        first_layout,
        text_elements=[text.model_dump(mode="json")],
    )
    packaging = value.packaging.model_copy(
        update={"thumbnails": (first_layout, *value.packaging.thumbnails[1:])}
    )
    value = value.model_copy(
        update={
            "packaging": packaging,
        }
    )
    spec = ThumbnailRenderSpec(
        concept_id=first_layout.concept_id,
        layout_spec_sha256=first_layout.layout_spec_sha256,
        visual_bindings=tuple(
            VisualSourceBinding(
                visual_index=index,
                source_artifact_sha256=source_sha256,
            )
            for index, source_sha256 in enumerate(source_sha256s)
        ),
    )
    return value, evaluate_machine_qa(value), spec, dict(
        zip(source_sha256s, sources, strict=True)
    )


def test_evidence_is_stable_and_preserves_thumbnail_human_reviews() -> None:
    value, result, spec, sources = _bound_input()

    assert result.outcome != "FAIL"
    findings = (
        *result.findings,
        *result.hook.findings,
        *result.packaging.findings,
        *(finding for thumbnail in result.packaging.thumbnails for finding in thumbnail.findings),
    )
    assert all(finding.outcome != "FAIL" for finding in findings)

    first = build_rendered_thumbnail_evidence(value, result, spec, sources)
    second = build_rendered_thumbnail_evidence(value, result, spec, sources)
    review_ids = {
        finding.check_id
        for thumbnail in result.packaging.thumbnails
        for finding in thumbnail.review_findings
    }

    assert first.png_bytes == second.png_bytes
    assert first.evidence.sha256() == second.evidence.sha256()
    assert first.evidence.png_sha256 == hashlib.sha256(first.png_bytes).hexdigest()
    assert review_ids >= {"thumbnail_dominant_idea", "thumbnail_hierarchy"}


def test_evidence_rejects_a_canonical_machine_failure() -> None:
    value, _, spec, sources = _bound_input()
    invalid_visual = value.lineage.scene_visual_artifacts[0].model_copy(
        update={"mime_type": "video/mp4"}
    )
    failed_value = value.model_copy(
        update={
            "lineage": value.lineage.model_copy(
                update={
                    "scene_visual_artifacts": (
                        invalid_visual,
                        *value.lineage.scene_visual_artifacts[1:],
                    )
                }
            )
        }
    )
    failed_result = evaluate_machine_qa(failed_value)

    assert failed_result.outcome == "FAIL"
    with pytest.raises(ThumbnailEvidenceError, match="cannot mint"):
        build_rendered_thumbnail_evidence(failed_value, failed_result, spec, sources)


def test_small_png_cannot_masquerade_as_1280x720_canonical_source() -> None:
    value, result, spec, sources = _bound_input(source_width=3, source_height=2)

    assert result.outcome != "FAIL"
    with pytest.raises(ThumbnailEvidenceError, match="dimensions"):
        build_rendered_thumbnail_evidence(value, result, spec, sources)


def test_evidence_rejects_model_copy_invalid_layout_before_machine_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, result, spec, sources = _bound_input()
    layout = value.packaging.thumbnails[0]
    invalid_bounds = layout.visual_elements[0].bounds.model_copy(update={"x": -1})
    invalid_layout = layout.model_copy(
        update={
            "visual_elements": (
                layout.visual_elements[0].model_copy(update={"bounds": invalid_bounds}),
                *layout.visual_elements[1:],
            )
        }
    )
    invalid_layout = invalid_layout.model_copy(
        update={"layout_spec_sha256": canonical_sha256(invalid_layout.hash_payload())}
    )
    invalid_value = value.model_copy(
        update={
            "packaging": value.packaging.model_copy(
                update={
                    "thumbnails": (
                        invalid_layout,
                        *value.packaging.thumbnails[1:],
                    )
                }
            )
        }
    )

    def fail_if_called(_: object) -> object:
        raise AssertionError("machine QA must not run for invalid machine input")

    monkeypatch.setattr(thumbnail_quality, "evaluate_machine_qa", fail_if_called)

    with pytest.raises(ThumbnailEvidenceError, match="machine input fails runtime validation"):
        build_rendered_thumbnail_evidence(invalid_value, result, spec, sources)


@pytest.mark.parametrize(
    ("dimension", "replacement"),
    (("width", 1280.0), ("height", 720.0)),
)
def test_evidence_rejects_non_integer_immutable_provenance_dimensions(
    dimension: str, replacement: float
) -> None:
    value, _, spec, sources = _bound_input()
    artifact = value.lineage.scene_visual_artifacts[0]
    provenance = json.loads(artifact.provenance_json)
    provenance["media_validation"][dimension] = replacement
    invalid_artifact = artifact.model_copy(
        update={"provenance_json": canonical_json(provenance)}
    )
    invalid_value = value.model_copy(
        update={
            "lineage": value.lineage.model_copy(
                update={
                    "scene_visual_artifacts": (
                        invalid_artifact,
                        *value.lineage.scene_visual_artifacts[1:],
                    )
                }
            )
        }
    )

    with pytest.raises(ThumbnailEvidenceError, match="dimensions"):
        build_rendered_thumbnail_evidence(
            invalid_value,
            evaluate_machine_qa(invalid_value),
            spec,
            sources,
        )


def test_evidence_rejects_source_mutation_unknown_sha_and_stale_result() -> None:
    value, result, spec, sources = _bound_input()
    source_sha256 = next(iter(sources))
    mutated = dict(sources)
    mutated[source_sha256] = sources[source_sha256] + b"changed"

    with pytest.raises(ThumbnailEvidenceError, match="do not match"):
        build_rendered_thumbnail_evidence(value, result, spec, mutated)
    unknown_source = _png((1, 2, 3))
    unknown_sha256 = hashlib.sha256(unknown_source).hexdigest()
    unknown_spec = spec.model_copy(
        update={
            "visual_bindings": (
                VisualSourceBinding(
                    visual_index=0,
                    source_artifact_sha256=unknown_sha256,
                ),
                spec.visual_bindings[1],
            )
        }
    )
    unknown_sources = {
        unknown_sha256: unknown_source,
        spec.visual_bindings[1].source_artifact_sha256: sources[
            spec.visual_bindings[1].source_artifact_sha256
        ],
    }
    with pytest.raises(ThumbnailEvidenceError, match="identify"):
        build_rendered_thumbnail_evidence(value, result, unknown_spec, unknown_sources)
    with pytest.raises(ThumbnailEvidenceError, match="stale"):
        build_rendered_thumbnail_evidence(
            value,
            result.model_copy(update={"outcome": "PASS"}),
            spec,
            sources,
        )


@pytest.mark.parametrize("field, replacement", (("campaign_id", 999), ("mime_type", "video/mp4")))
def test_evidence_rejects_wrong_campaign_or_non_png_source(
    field: str, replacement: object
) -> None:
    value, result, spec, sources = _bound_input()
    visual = value.lineage.scene_visual_artifacts[0].model_copy(
        update={field: replacement}
    )
    value = value.model_copy(
        update={
            "lineage": value.lineage.model_copy(
                update={
                    "scene_visual_artifacts": (
                        visual,
                        *value.lineage.scene_visual_artifacts[1:],
                    )
                }
            )
        }
    )

    with pytest.raises(ThumbnailEvidenceError, match="cannot mint"):
        build_rendered_thumbnail_evidence(value, evaluate_machine_qa(value), spec, sources)


def test_evidence_rejects_layout_hash_unknown_concept_and_text_binding() -> None:
    value, result, spec, sources = _bound_input()
    bad_hash = spec.model_copy(update={"layout_spec_sha256": "0" * 64})
    unknown = spec.model_copy(update={"concept_id": "unknown"})
    wrong_text = _layout_with_hash(
        value.packaging.thumbnails[0],
        text_elements=[
            {
                "text": "Different",
                "bounds": {"x": 60, "y": 80, "width": 500, "height": 80},
                "font_height_px": 32,
                "contrast_ratio": 7.0,
            }
        ],
    )
    wrong_value = value.model_copy(
        update={
            "packaging": value.packaging.model_copy(
                update={
                    "thumbnails": (wrong_text, *value.packaging.thumbnails[1:])
                }
            )
        }
    )
    wrong_spec = spec.model_copy(
        update={"layout_spec_sha256": wrong_text.layout_spec_sha256}
    )

    with pytest.raises(ThumbnailEvidenceError, match="stale"):
        build_rendered_thumbnail_evidence(value, result, bad_hash, sources)
    with pytest.raises(ThumbnailEvidenceError, match="unknown concept"):
        build_rendered_thumbnail_evidence(value, result, unknown, sources)
    with pytest.raises(ThumbnailEvidenceError, match="cannot mint"):
        build_rendered_thumbnail_evidence(
            wrong_value,
            evaluate_machine_qa(wrong_value),
            wrong_spec,
            sources,
        )


def test_duplicate_and_extra_visual_bindings_fail_closed() -> None:
    value, result, spec, sources = _bound_input()
    duplicate = spec.model_copy(
        update={"visual_bindings": (spec.visual_bindings[0], spec.visual_bindings[0])}
    )
    extra = spec.model_copy(
        update={
            "visual_bindings": (
                *spec.visual_bindings,
                VisualSourceBinding(
                    visual_index=2,
                    source_artifact_sha256=spec.visual_bindings[0].source_artifact_sha256,
                ),
            )
        }
    )

    with pytest.raises(ThumbnailEvidenceError, match="runtime validation"):
        build_rendered_thumbnail_evidence(value, result, duplicate, sources)
    with pytest.raises(ThumbnailEvidenceError, match="exactly cover"):
        build_rendered_thumbnail_evidence(value, result, extra, sources)


def test_text_only_thumbnail_needs_no_source_binding() -> None:
    value, _, _, _ = _bound_input()
    concept = value.packaging.concepts[0].model_copy(update={"thumbnail_text": "TEXT"})
    raw = {
        "concept_id": concept.concept_id,
        "canvas_width_px": 1280,
        "canvas_height_px": 720,
        "text_elements": [
            {
                "text": "TEXT",
                "bounds": {"x": 40, "y": 40, "width": 500, "height": 100},
                "font_height_px": 48,
                "contrast_ratio": 7.0,
            }
        ],
        "visual_elements": [],
    }
    layout = ThumbnailLayout(**raw, layout_spec_sha256=canonical_sha256(raw))
    value = value.model_copy(
        update={
            "packaging": value.packaging.model_copy(
                update={
                    "concepts": (concept, *value.packaging.concepts[1:]),
                    "thumbnails": (layout, *value.packaging.thumbnails[1:]),
                }
            )
        }
    )
    spec = ThumbnailRenderSpec(
        concept_id=concept.concept_id,
        layout_spec_sha256=layout.layout_spec_sha256,
    )

    rendered = build_rendered_thumbnail_evidence(
        value,
        evaluate_machine_qa(value),
        spec,
        {},
    )

    assert rendered.evidence.source_artifact_sha256s == ()
    assert rendered.evidence.byte_size == len(rendered.png_bytes)


def test_evidence_rejects_whitespace_padded_literal_model_copies() -> None:
    value, result, spec, sources = _bound_input()
    invalid_policy = spec.model_copy(
        update={"policy_version": " i6-thumbnail-render-v1 "}
    )
    invalid_fit = spec.model_copy(
        update={
            "visual_bindings": (
                spec.visual_bindings[0].model_copy(update={"fit_mode": " cover "}),
                *spec.visual_bindings[1:],
            )
        }
    )

    with pytest.raises(ThumbnailEvidenceError, match="runtime validation"):
        build_rendered_thumbnail_evidence(value, result, invalid_policy, sources)
    with pytest.raises(ThumbnailEvidenceError, match="runtime validation"):
        build_rendered_thumbnail_evidence(value, result, invalid_fit, sources)


def test_evidence_rejects_model_copy_concept_id_canonicalization_drift() -> None:
    value, result, spec, sources = _bound_input()
    noncanonical = spec.model_copy(update={"concept_id": " Concept 0 "})

    with pytest.raises(
        ThumbnailEvidenceError,
        match="thumbnail render spec changes during runtime validation",
    ):
        build_rendered_thumbnail_evidence(value, result, noncanonical, sources)
