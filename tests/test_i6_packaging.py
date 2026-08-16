from __future__ import annotations

import re

import pytest

from app.editorial.contracts import canonical_sha256
from tests.test_i6_machine_qa import _input
from app.qa import PackagingImplication, ThumbnailLayout, evaluate_machine_qa


def test_unrelated_free_form_implication_citing_verified_claim_requires_review() -> None:
    value = _input()
    first = value.packaging.concepts[0]
    unrelated = first.model_copy(
        update={
            "implications": (
                PackagingImplication(
                    statement="Take this medical treatment immediately.",
                    claim_hashes=(first.implications[0].claim_hashes[0],),
                ),
            )
        }
    )
    concepts = (unrelated, *value.packaging.concepts[1:])

    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(update={"concepts": concepts})
            }
        )
    )

    assert result.outcome == "NEEDS_HUMAN"
    assert any(
        item.check_id == "packaging_truth_gate"
        and item.outcome == "NEEDS_HUMAN"
        and not item.hard_gate
        for item in result.findings
    )


def test_zero_overlap_safe_nonextractive_implication_requires_human_review() -> None:
    value = _input()
    first = value.packaging.concepts[0]
    statement = "A bounded audit merits cautious interpretation."
    assert not (
        set(statement.casefold().split())
        & set(first.implications[0].statement.casefold().split())
    )
    paraphrase = first.model_copy(
        update={
            "implications": (
                PackagingImplication(
                    statement=statement,
                    claim_hashes=(first.implications[0].claim_hashes[0],),
                ),
            )
        }
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"concepts": (paraphrase, *value.packaging.concepts[1:])}
                )
            }
        )
    )

    finding = next(
        item
        for item in result.findings
        if item.check_id == "packaging_truth_gate"
        and item.evidence[:1] == (paraphrase.concept_id,)
    )
    assert finding.outcome == "NEEDS_HUMAN"
    assert not finding.hard_gate
    assert result.outcome == "NEEDS_HUMAN"


def test_shared_material_token_does_not_make_free_form_implication_pass() -> None:
    value = _input()
    first = value.packaging.concepts[0]
    overlap = first.model_copy(
        update={
            "implications": (
                PackagingImplication(
                    statement="The official record guarantees a miracle cure.",
                    claim_hashes=(first.implications[0].claim_hashes[0],),
                ),
            )
        }
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"concepts": (overlap, *value.packaging.concepts[1:])}
                )
            }
        )
    )

    finding = next(
        item
        for item in result.findings
        if item.check_id == "packaging_truth_gate"
        and item.evidence[:1] == (overlap.concept_id,)
    )
    assert finding.outcome == "NEEDS_HUMAN" and not finding.hard_gate


def test_exactly_one_shared_word_does_not_certify_free_form_truth() -> None:
    value = _input()
    first = value.packaging.concepts[0]
    accepted = next(
        claim
        for claim in value.lineage.research.payload["claims"]  # type: ignore[index,union-attr]
        if "workflow" in claim["assertion_text"].casefold()
    )
    statement = "Workflow nebula lattice drizzle."
    def normalized_words(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.casefold()))

    shared_words = normalized_words(accepted["assertion_text"]) & normalized_words(statement)
    assert shared_words == {"workflow"}
    free_form = first.model_copy(
        update={
            "implications": (
                PackagingImplication(
                    statement=statement,
                    claim_hashes=(accepted["claim_hash"],),
                ),
            )
        }
    )

    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"concepts": (free_form, *value.packaging.concepts[1:])}
                )
            }
        )
    )

    finding = next(
        item
        for item in result.findings
        if item.check_id == "packaging_truth_gate"
        and item.evidence[:1] == (free_form.concept_id,)
    )
    assert finding.outcome == "NEEDS_HUMAN"
    assert not finding.hard_gate


def test_missing_structured_claim_lineage_hard_fails() -> None:
    value = _input()
    first = value.packaging.concepts[0]
    missing = first.model_copy(
        update={
            "implications": (
                PackagingImplication(
                    statement=first.implications[0].statement,
                    claim_hashes=("f" * 64,),
                ),
            )
        }
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"concepts": (missing, *value.packaging.concepts[1:])}
                )
            }
        )
    )

    assert result.outcome == "FAIL"
    assert any(
        item.check_id == "packaging_truth_gate" and item.hard_gate
        for item in result.findings
    )


def _rejected_claim(value: object) -> dict[str, object]:
    payload = getattr(value, "lineage").research.payload
    assert payload is not None
    return next(dict(claim) for claim in payload["rejected_claims"])


@pytest.mark.parametrize(
    "field",
    ("title", "thumbnail_concept", "thumbnail_text", "viewer_trigger", "promise"),
)
def test_public_package_surface_exactly_matching_rejected_claim_hard_fails(
    field: str,
) -> None:
    value = _input(include_rejected_claim=True)
    rejected = _rejected_claim(value)
    first = value.packaging.concepts[0].model_copy(
        update={field: rejected["assertion_text"]}
    )
    packaging_updates: dict[str, object] = {
        "concepts": (first, *value.packaging.concepts[1:])
    }
    if field == "thumbnail_text":
        raw_layout = value.packaging.thumbnails[0].model_copy(
            update={
                "text_elements": (
                    value.packaging.thumbnails[0].text_elements[0].model_copy(
                        update={"text": rejected["assertion_text"]}
                    ),
                )
            }
        )
        packaging_updates["thumbnails"] = (
            raw_layout.model_copy(
                update={"layout_spec_sha256": canonical_sha256(raw_layout.hash_payload())}
            ),
            *value.packaging.thumbnails[1:],
        )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(update=packaging_updates)
            }
        )
    )

    assert result.outcome == "FAIL"
    assert any(
        item.check_id == "packaging_surface_truth_gate"
        and item.outcome == "FAIL"
        and item.hard_gate
        and item.evidence[:2] == (first.concept_id, field)
        for item in result.findings
    )


def test_public_structured_rejected_wording_cannot_be_laundered_by_accepted_hash() -> None:
    value = _input(include_rejected_claim=True)
    rejected = _rejected_claim(value)
    first = value.packaging.concepts[0]
    laundered = first.model_copy(
        update={
            "implications": (
                PackagingImplication(
                    statement=rejected["assertion_text"],
                    claim_hashes=(first.implications[0].claim_hashes[0],),
                ),
            )
        }
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"concepts": (laundered, *value.packaging.concepts[1:])}
                )
            }
        )
    )

    assert result.outcome == "FAIL"
    assert any(
        item.check_id == "packaging_truth_gate"
        and item.outcome == "FAIL"
        and item.hard_gate
        and rejected["claim_hash"] in item.evidence
        for item in result.findings
    )


def test_public_structured_rejected_claim_lineage_hard_fails() -> None:
    value = _input(include_rejected_claim=True)
    rejected = _rejected_claim(value)
    first = value.packaging.concepts[0].model_copy(
        update={
            "implications": (
                PackagingImplication(
                    statement=rejected["assertion_text"],
                    claim_hashes=(rejected["claim_hash"],),
                ),
            )
        }
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"concepts": (first, *value.packaging.concepts[1:])}
                )
            }
        )
    )

    assert result.outcome == "FAIL"
    assert any(
        item.check_id == "packaging_truth_gate" and item.hard_gate
        for item in result.findings
    )


def test_public_structured_exact_wording_with_different_claim_hash_hard_fails() -> None:
    value = _input()
    first, second = value.packaging.concepts[:2]
    mismatched = first.model_copy(
        update={
            "implications": (
                PackagingImplication(
                    statement=second.implications[0].statement,
                    claim_hashes=(first.implications[0].claim_hashes[0],),
                ),
            )
        }
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"concepts": (mismatched, second, value.packaging.concepts[2])}
                )
            }
        )
    )

    assert result.outcome == "FAIL"
    assert any(
        item.check_id == "packaging_truth_gate"
        and item.outcome == "FAIL"
        and item.hard_gate
        and item.evidence[:2]
        == (mismatched.concept_id, first.implications[0].claim_hashes[0])
        for item in result.findings
    )


def test_commercial_score_cannot_override_public_claim_lineage_failure() -> None:
    value = _input(include_rejected_claim=True)
    rejected = _rejected_claim(value)
    first = value.packaging.concepts[0].model_copy(update={"title": rejected["assertion_text"]})
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "commercial_score": 100,
                "packaging": value.packaging.model_copy(
                    update={"concepts": (first, *value.packaging.concepts[1:])}
                ),
            }
        )
    )

    assert result.outcome == "FAIL"
    assert any(
        item.check_id == "packaging_surface_truth_gate" and item.hard_gate
        for item in result.findings
    )


def test_canonical_extractive_implication_still_mechanically_passes() -> None:
    value = _input()
    result = evaluate_machine_qa(value)
    first = value.packaging.concepts[0]
    finding = next(
        item
        for item in result.findings
        if item.check_id == "packaging_truth_gate"
        and item.evidence == (first.concept_id, first.implications[0].claim_hashes[0])
    )
    assert finding.outcome == "PASS"


def test_rewritten_title_and_thumbnail_cannot_save_same_trigger_and_promise() -> None:
    value = _input()
    first = value.packaging.concepts[0]
    rewrite = value.packaging.concepts[1].model_copy(
        update={
            "title": "Completely new title",
            "thumbnail_concept": "Different image",
            "thumbnail_text": "NEW",
            "viewer_trigger": first.viewer_trigger,
            "promise": first.promise,
        }
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"concepts": (first, rewrite, value.packaging.concepts[2])}
                )
            }
        )
    )

    assert result.outcome == "FAIL"
    assert any(
        item.check_id == "packaging_core_positioning_duplicate"
        for item in result.findings
    )


def test_target_audience_risk_and_implication_identity_cannot_be_trivial_diversity_edits() -> (
    None
):
    value = _input()
    first = value.packaging.concepts[0]
    base = {
        "title": first.title,
        "thumbnail_concept": first.thumbnail_concept,
        "thumbnail_text": first.thumbnail_text,
        "viewer_trigger": first.viewer_trigger,
        "promise": first.promise,
        "overclaim_risk": first.overclaim_risk,
        "target_audience": first.target_audience,
        "implications": first.implications,
    }
    one_field_changes = (
        {"target_audience": "A different audience label"},
        {"overclaim_risk": "medium"},
        {"implications": value.packaging.concepts[1].implications},
    )
    for update in one_field_changes:
        near_duplicate = value.packaging.concepts[1].model_copy(
            update={**base, **update}
        )
        result = evaluate_machine_qa(
            value.model_copy(
                update={
                    "packaging": value.packaging.model_copy(
                        update={
                            "concepts": (
                                first,
                                near_duplicate,
                                value.packaging.concepts[2],
                            )
                        }
                    )
                }
            )
        )

        assert result.outcome == "FAIL"
        assert any(
            item.check_id == "packaging_near_duplicate" for item in result.findings
        )


def test_missing_or_self_attested_thumbnail_conclusions_cannot_make_pass() -> None:
    value = _input()
    raw_layout = value.packaging.thumbnails[0].model_copy(
        update={"visual_elements": ()}
    )
    layout = raw_layout.model_copy(
        update={"layout_spec_sha256": canonical_sha256(raw_layout.hash_payload())}
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"thumbnails": (layout, *value.packaging.thumbnails[1:])}
                )
            }
        )
    )

    assert result.outcome == "NEEDS_HUMAN"
    reviews = {item.check_id: item.review_status for item in result.packaging.thumbnails[0].review_findings}
    assert reviews["thumbnail_dominant_idea"] == "NEEDS_HUMAN"
    assert reviews["thumbnail_hierarchy"] == "NEEDS_HUMAN"


def test_raw_layout_hash_and_canonical_cold_open_cannot_be_replaced_by_caller_text() -> (
    None
):
    value = _input()
    altered_layout = value.packaging.thumbnails[0].model_copy(
        update={"layout_spec_sha256": "f" * 64}
    )
    script = dict(value.lineage.script.payload or {})
    sections = [dict(section) for section in script["sections"]]  # type: ignore[index]
    sections[0]["narration"] = "Caller-written hook."
    script["sections"] = sections
    altered_script = value.lineage.script.model_copy(
        update={
            "payload": script,
            "sha256": __import__(
                "app.editorial.contracts", fromlist=["canonical_sha256"]
            ).canonical_sha256(script),
        }
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "lineage": value.lineage.model_copy(update={"script": altered_script}),
                "packaging": value.packaging.model_copy(
                    update={
                        "thumbnails": (altered_layout, *value.packaging.thumbnails[1:])
                    }
                ),
            }
        )
    )

    assert result.outcome == "FAIL"
    assert any(item.check_id == "hook_script_gate" for item in result.findings)
    assert any(
        item.check_id == "thumbnail_layout_spec_hash" for item in result.findings
    )


def test_claim_type_framing_cannot_be_stripped_from_packaging_truth() -> None:
    value = _input()
    claims = value.lineage.research.payload["claims"]  # type: ignore[index]
    by_type = {claim["claim_type"]: claim for claim in claims}  # type: ignore[index]
    for claim in (
        by_type["attributed_claim"],
        by_type["estimate"],
        by_type["opinion"],
    ):
        concept = value.packaging.concepts[1].model_copy(
            update={
                "implications": (
                    PackagingImplication(
                        statement=claim["assertion_text"],
                        claim_hashes=(claim["claim_hash"],),
                    ),
                )
            }
        )
        result = evaluate_machine_qa(
            value.model_copy(
                update={
                    "packaging": value.packaging.model_copy(
                        update={
                            "concepts": (
                                value.packaging.concepts[0],
                                concept,
                                value.packaging.concepts[2],
                            )
                        }
                    )
                }
            )
        )

        assert result.outcome == "FAIL"
        assert any(
            item.check_id == "packaging_truth_gate" and item.hard_gate
            for item in result.findings
        )


def test_unequal_implication_counts_are_total_and_layout_hash_is_only_spec_integrity() -> (
    None
):
    value = _input()
    clean = evaluate_machine_qa(value)
    altered_layout = value.packaging.thumbnails[0].model_copy(
        update={"layout_spec_sha256": "f" * 64}
    )
    altered = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={
                        "thumbnails": (
                            altered_layout,
                            *value.packaging.thumbnails[1:],
                        )
                    }
                )
            }
        )
    )

    assert len(value.packaging.concepts[2].implications) == 2
    assert clean.outcome == "NEEDS_HUMAN"
    finding = next(
        item
        for item in altered.findings
        if item.check_id == "thumbnail_layout_spec_hash"
    )
    assert "not rendered-image proof" in finding.message


def test_fewer_than_three_concepts_and_measurable_layout_failures_are_rejected() -> None:
    value = _input()
    short = value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": value.packaging.concepts[:2], "selected_concept_id": value.packaging.concepts[0].concept_id, "thumbnails": value.packaging.thumbnails[:2]})})
    assert any(item.check_id == "packaging_minimum_distinct_concepts" for item in evaluate_machine_qa(short).findings)
    bounds = value.packaging.thumbnails[0].text_elements[0].bounds.model_copy(update={"x": 1200, "y": 700, "width": 200, "height": 80})
    excessive_text = "one two three four five six seven"
    layout = value.packaging.thumbnails[0].model_copy(update={"text_elements": (value.packaging.thumbnails[0].text_elements[0].model_copy(update={"text": excessive_text, "font_height_px": 1, "contrast_ratio": 1.0, "bounds": bounds}),)})
    layout = layout.model_copy(update={"layout_spec_sha256": canonical_sha256(layout.hash_payload())})
    altered_concept = value.packaging.concepts[0].model_copy(update={"thumbnail_text": excessive_text})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"concepts": (altered_concept, *value.packaging.concepts[1:]), "thumbnails": (layout, *value.packaging.thumbnails[1:])})}))
    altered_thumbnail = next(item for item in result.packaging.thumbnails if item.concept_id == "concept-0")
    checks = {item.check_id for item in altered_thumbnail.findings if item.outcome == "FAIL"}
    assert {"thumbnail_layout_bounds", "thumbnail_minimal_text", "thumbnail_text_readability"} <= checks


def test_raw_geometry_never_claims_semantic_thumbnail_pass() -> None:
    value = _input()
    bounds = value.packaging.thumbnails[0].visual_elements[0].bounds.model_copy(update={"x": 80, "y": 300, "width": 200, "height": 200})
    layout = value.packaging.thumbnails[0].model_copy(update={"visual_elements": (*value.packaging.thumbnails[0].visual_elements, value.packaging.thumbnails[0].visual_elements[0].model_copy(update={"bounds": bounds}))})
    layout = layout.model_copy(update={"layout_spec_sha256": canonical_sha256(layout.hash_payload())})
    result = evaluate_machine_qa(value.model_copy(update={"packaging": value.packaging.model_copy(update={"thumbnails": (layout, *value.packaging.thumbnails[1:])})}))
    semantic = [item for item in result.review_findings if item.check_id in {"thumbnail_dominant_idea", "thumbnail_hierarchy"}]
    assert semantic and all(item.review_status == "NEEDS_HUMAN" for item in semantic)
    assert result.outcome == "NEEDS_HUMAN"


def test_tiny_single_visual_element_cannot_silently_pass() -> None:
    value = _input()
    tiny = value.packaging.thumbnails[0].model_copy(
        update={
            "visual_elements": (
                value.packaging.thumbnails[0].visual_elements[0].model_copy(
                    update={
                        "bounds": value.packaging.thumbnails[0].visual_elements[
                            0
                        ].bounds.model_copy(
                            update={"x": 10, "y": 10, "width": 8, "height": 8}
                        )
                    }
                ),
            ),
        }
    )
    tiny = tiny.model_copy(
        update={"layout_spec_sha256": canonical_sha256(tiny.hash_payload())}
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"thumbnails": (tiny, *value.packaging.thumbnails[1:])}
                )
            }
        )
    )

    thumbnail = result.packaging.thumbnails[0]
    assert thumbnail.outcome == "NEEDS_HUMAN"
    assert not any(
        item.check_id in {"thumbnail_dominant_idea", "thumbnail_hierarchy"}
        and item.outcome == "PASS"
        for item in thumbnail.findings
    )
    assert {item.check_id for item in thumbnail.review_findings} == {
        "thumbnail_dominant_idea",
        "thumbnail_hierarchy",
    }
    assert result.outcome == "NEEDS_HUMAN"


def test_large_single_visual_element_cannot_prove_semantic_hierarchy() -> None:
    value = _input()
    base = value.packaging.thumbnails[0]
    large = base.model_copy(
        update={
            "visual_elements": (
                base.visual_elements[0].model_copy(
                    update={
                        "bounds": base.visual_elements[0].bounds.model_copy(
                            update={"x": 0, "y": 0, "width": 1280, "height": 720}
                        )
                    }
                ),
            )
        }
    )
    large = large.model_copy(
        update={"layout_spec_sha256": canonical_sha256(large.hash_payload())}
    )
    result = evaluate_machine_qa(
        value.model_copy(
            update={
                "packaging": value.packaging.model_copy(
                    update={"thumbnails": (large, *value.packaging.thumbnails[1:])}
                )
            }
        )
    )

    thumbnail = result.packaging.thumbnails[0]
    assert thumbnail.outcome == "NEEDS_HUMAN"
    assert not any(
        item.check_id in {"thumbnail_dominant_idea", "thumbnail_hierarchy"}
        and item.outcome == "PASS"
        for item in thumbnail.findings
    )


def test_caller_semantic_proof_payload_is_rejected_and_cannot_bypass_review() -> None:
    value = _input()
    raw = value.packaging.thumbnails[0].model_dump(mode="json")
    raw["semantic_layout_proof"] = {
        "dominant_visual_index": 0,
        "hierarchy_visual_indexes": [0, 1],
    }

    with pytest.raises(ValueError, match="semantic_layout_proof"):
        ThumbnailLayout.model_validate(raw)
