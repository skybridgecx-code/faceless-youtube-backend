from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from typing import cast

import pytest

from app.editorial.script_compiler import (
    I4_TARGET_MAX_RUNTIME_MINUTES,
    I4_TARGET_MAX_WORDS,
    I4_TARGET_MIN_RUNTIME_MINUTES,
    I4_TARGET_MIN_WORDS,
    NARRATION_WORDS_PER_MINUTE,
    NARRATOR_IDENTITY,
    REQUIRED_SECTION_IDS,
    compile_script_packet,
    evaluate_script_gate,
)


TOPIC_PACKET_HASH = "1" * 64
RESEARCH_PACKET_HASH = "2" * 64

FACT_CONTEXT_HASH = "a" * 64
FACT_EVIDENCE_HASH = "b" * 64
ATTRIBUTED_HASH = "c" * 64
ESTIMATE_HASH = "d" * 64
OPINION_HASH = "e" * 64
FACT_UNCERTAINTY_HASH = "f" * 64
REJECTED_HASH = "9" * 64


def _topic_packet() -> dict[str, object]:
    return {
        "campaign_id": 41,
        "contract_version": "i4-topic-packet-v1",
        "policy_version": "i4-editorial-v1",
        "ranked_candidates": [
            {
                "angle": "What durable checkpoints change after an interruption",
                "candidate_key": "durable-workflows",
                "topic": "Durable Workflow Recovery",
            }
        ],
        "selected_candidate_key": "durable-workflows",
        "viewer_promise": {
            "broad_interest_bridge": (
                "Reliable recovery affects every system entrusted with long-running work."
            ),
            "core_question": (
                "Can a workflow resume without repeating accepted application effects?"
            ),
            "expected_takeaway": (
                "Checkpointed inputs and replay-safe persistence solve different failure windows."
            ),
            "novelty": (
                "The episode separates orchestration checkpoints from database idempotency."
            ),
            "stakes": (
                "A replay mistake can duplicate durable records or silently change evidence."
            ),
            "target_viewer": "Engineers operating long-running editorial pipelines.",
            "viewer_promise": (
                "Understand how checkpointing and application reconciliation work together."
            ),
            "why_now": (
                "More production workflows now cross process and provider boundaries."
            ),
        },
    }


def _claim(
    *,
    claim_hash: str,
    assertion_text: str,
    claim_type: str,
    role: str,
    source_keys: list[str],
    state: str,
    attribution: str | None = None,
    assumptions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "assertion_text": assertion_text,
        "assumptions": assumptions or [],
        "attribution": attribution,
        "claim_hash": claim_hash,
        "claim_type": claim_type,
        "material": True,
        "reasons": ["fixture_claim_satisfies_its_deterministic_policy"],
        "role": role,
        "source_keys": source_keys,
        "state": state,
    }


def _research_packet() -> dict[str, object]:
    return {
        "campaign_id": 41,
        "claims": [
            _claim(
                claim_hash=FACT_CONTEXT_HASH,
                assertion_text="A completed durable step has a recorded replay result.",
                claim_type="fact",
                role="context",
                source_keys=["dbos-docs"],
                state="VERIFIED",
            ),
            _claim(
                claim_hash=FACT_EVIDENCE_HASH,
                assertion_text=(
                    "Application idempotency protects the commit-before-checkpoint window."
                ),
                claim_type="fact",
                role="evidence",
                source_keys=["engineering-paper"],
                state="VERIFIED",
            ),
            _claim(
                claim_hash=ATTRIBUTED_HASH,
                assertion_text="workflow execution can resume after process interruption.",
                claim_type="attributed_claim",
                role="stakes",
                source_keys=["dbos-docs"],
                state="VERIFIED",
                attribution="DBOS documentation",
            ),
            _claim(
                claim_hash=ESTIMATE_HASH,
                assertion_text="the narration runtime stays inside a bounded range",
                claim_type="estimate",
                role="outlook",
                source_keys=["narration-study"],
                state="ESTIMATE",
                assumptions=[
                    "a fixed narration speed",
                    "the compiled section word count",
                ],
            ),
            _claim(
                claim_hash=OPINION_HASH,
                assertion_text=(
                    "durability is most convincing when orchestration and storage are tested together."
                ),
                claim_type="opinion",
                role="counterpoint",
                source_keys=[],
                state="OPINION",
            ),
            _claim(
                claim_hash=FACT_UNCERTAINTY_HASH,
                assertion_text=(
                    "A provider response can change before its result is checkpointed."
                ),
                claim_type="fact",
                role="uncertainty",
                source_keys=["engineering-paper"],
                state="VERIFIED",
            ),
        ],
        "contract_version": "i4-research-packet-v1",
        "gate": {
            "outcome": "PASS",
            "reasons": ["research_is_sourceable_and_claim_verified"],
        },
        "policy_version": "i4-editorial-v1",
        "rejected_claims": [
            _claim(
                claim_hash=REJECTED_HASH,
                assertion_text="Every interrupted workflow always recovers without risk.",
                claim_type="fact",
                role="outlook",
                source_keys=["discovery-result"],
                state="REJECTED",
            )
        ],
        "selected_candidate_key": "durable-workflows",
        "sources": [
            {
                "publisher": "DBOS",
                "source_class": "official",
                "source_key": "dbos-docs",
                "source_uri": "https://docs.dbos.dev/workflows",
            },
            {
                "publisher": "Systems Research Journal",
                "source_class": "reputable_secondary",
                "source_key": "engineering-paper",
                "source_uri": "https://example.org/durable-execution",
            },
            {
                "publisher": "Narration Standards Lab",
                "source_class": "reputable_secondary",
                "source_key": "narration-study",
                "source_uri": "https://example.org/narration-rate",
            },
            {
                "publisher": "Discovery Channel",
                "source_class": "discovery_only",
                "source_key": "discovery-result",
                "source_uri": "https://www.youtube.com/watch?v=fixture",
            },
        ],
        "topic_packet_hash": TOPIC_PACKET_HASH,
    }


@pytest.fixture
def script_inputs() -> tuple[dict[str, object], dict[str, object]]:
    return _topic_packet(), _research_packet()


@pytest.fixture
def script_packet(
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> dict[str, object]:
    topic_packet, research_packet = script_inputs
    return compile_script_packet(
        campaign_id=41,
        topic_packet=topic_packet,
        topic_packet_hash=TOPIC_PACKET_HASH,
        research_packet=research_packet,
        research_packet_hash=RESEARCH_PACKET_HASH,
    )


def _sections(packet: dict[str, object]) -> list[dict[str, object]]:
    return [dict(section) for section in cast(list[object], packet["sections"])]


def _section_with_claim(
    packet: dict[str, object],
    claim_hash: str,
) -> dict[str, object]:
    return next(
        section
        for section in _sections(packet)
        if claim_hash in cast(list[str], section["claim_hashes"])
    )


def _recheck(
    packet: dict[str, object],
    research_packet: dict[str, object],
) -> dict[str, object]:
    return evaluate_script_gate(packet, research_packet=research_packet)


def test_compile_script_has_required_eight_section_progression_and_passes_gate(
    script_packet: dict[str, object],
) -> None:
    sections = _sections(script_packet)

    assert tuple(section["section_id"] for section in sections) == REQUIRED_SECTION_IDS
    assert len(sections) == 8
    assert all(
        str(section[field]).strip()
        for section in sections
        for field in ("purpose", "narration", "continuation_reason", "payoff")
    )
    assert all(section["claim_hashes"] for section in sections)
    assert script_packet["gate"] == {
        "outcome": "PASS",
        "reasons": ["script_is_structured_evidence_bound_and_editorially_safe"],
    }


def test_compile_script_propagates_viewer_promise_and_exact_narrator_identity(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    topic_packet, _ = script_inputs
    viewer_promise = cast(dict[str, object], topic_packet["viewer_promise"])
    cold_open = _sections(script_packet)[0]

    assert script_packet["viewer_promise"] == viewer_promise
    assert script_packet["narrator_identity"] == NARRATOR_IDENTITY
    assert NARRATOR_IDENTITY == (
        "curious, technically literate, skeptical of hype, explicit about uncertainty, "
        "and interested in consequences"
    )
    assert viewer_promise["core_question"] in str(cold_open["narration"])
    assert viewer_promise["stakes"] in str(cold_open["narration"])
    assert viewer_promise["why_now"] in str(cold_open["narration"])


def test_viewer_promise_prose_remains_an_explicit_editorial_premise(
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    topic_packet, research_packet = deepcopy(script_inputs)
    viewer_promise = cast(dict[str, object], topic_packet["viewer_promise"])
    unsupported_why_now = (
        "Exactly 9,999 developer teams switched inference engines yesterday."
    )
    viewer_promise["why_now"] = unsupported_why_now
    packet = compile_script_packet(
        campaign_id=41,
        topic_packet=topic_packet,
        topic_packet_hash=TOPIC_PACKET_HASH,
        research_packet=research_packet,
        research_packet_hash=RESEARCH_PACKET_HASH,
    )
    cold_open = _sections(packet)[0]

    assert packet["gate"]["outcome"] == "PASS"  # type: ignore[index]
    assert (
        "Its why-now premise, also subject to verification, is: "
        f"{unsupported_why_now}"
    ) in str(cold_open["narration"])
    assert "does not treat either premise as an established fact" in str(
        cold_open["narration"]
    )

    mutated = deepcopy(packet)
    mutated_cold_open = cast(list[dict[str, object]], mutated["sections"])[0]
    mutated_cold_open["narration"] = str(mutated_cold_open["narration"]).replace(
        "Its why-now premise, also subject to verification, is: ",
        "Why now: ",
    )
    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert "why_now_missing_editorial_premise_framing" in gate["reasons"]


def test_compile_script_binds_material_facts_to_claim_hashes(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    factual_claims = [
        cast(dict[str, object], claim)
        for claim in cast(list[object], research_packet["claims"])
        if cast(dict[str, object], claim)["claim_type"] == "fact"
    ]
    sections = _sections(script_packet)

    for claim in factual_claims:
        containing_sections = [
            section
            for section in sections
            if str(claim["assertion_text"]) in str(section["narration"])
        ]
        assert containing_sections
        assert all(
            claim["claim_hash"] in cast(list[str], section["claim_hashes"])
            for section in containing_sections
        )


def test_compile_script_preserves_attribution_estimate_uncertainty_and_opinion_framing(
    script_packet: dict[str, object],
) -> None:
    attributed = _section_with_claim(script_packet, ATTRIBUTED_HASH)
    estimate = _section_with_claim(script_packet, ESTIMATE_HASH)
    opinion = _section_with_claim(script_packet, OPINION_HASH)

    assert "DBOS documentation" in str(attributed["narration"])
    assert any(
        marker in str(estimate["narration"]).casefold()
        for marker in ("estimate", "suggests", "approximately", "could")
    )
    assert "our read is" in str(opinion["narration"]).casefold()
    assert "editorial analysis" in str(opinion["narration"]).casefold()


def test_compile_script_excludes_rejected_claims(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    rejected = cast(dict[str, object], cast(list[object], research_packet["rejected_claims"])[0])
    all_narration = " ".join(str(section["narration"]) for section in _sections(script_packet))

    assert rejected["claim_hash"] not in cast(dict[str, object], script_packet["claim_index"])
    assert str(rejected["assertion_text"]) not in all_narration


def test_script_runtime_is_derived_from_documented_fixed_narration_rate(
    script_packet: dict[str, object],
) -> None:
    assert script_packet["narration_rate_words_per_minute"] == NARRATION_WORDS_PER_MINUTE
    assert script_packet["estimated_runtime_minutes"] == round(
        int(script_packet["word_count"]) / NARRATION_WORDS_PER_MINUTE,
        2,
    )
    assert I4_TARGET_MIN_WORDS <= int(script_packet["word_count"]) <= I4_TARGET_MAX_WORDS
    assert (
        I4_TARGET_MIN_RUNTIME_MINUTES
        <= float(script_packet["estimated_runtime_minutes"])
        <= I4_TARGET_MAX_RUNTIME_MINUTES
    )
    assert script_packet["target_runtime_minutes"] == {
        "maximum": I4_TARGET_MAX_RUNTIME_MINUTES,
        "minimum": I4_TARGET_MIN_RUNTIME_MINUTES,
    }
    reference_counts = Counter(
        claim_hash
        for section in _sections(script_packet)
        for claim_hash in cast(list[str], section["claim_hashes"])
    )
    assert set(reference_counts) == set(cast(dict[str, object], script_packet["claim_index"]))
    assert max(reference_counts.values()) <= 2


def test_gate_fails_when_claim_lineage_is_missing_or_factual_narration_loses_hash(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    broken_reference = deepcopy(script_packet)
    reference_section = _section_with_claim(broken_reference, FACT_CONTEXT_HASH)
    reference_section["claim_hashes"] = ["8" * 64]
    cast(list[dict[str, object]], broken_reference["sections"])[
        REQUIRED_SECTION_IDS.index(str(reference_section["section_id"]))
    ] = reference_section

    reference_gate = _recheck(broken_reference, research_packet)

    assert reference_gate["outcome"] == "FAIL"
    assert "referenced_claim_hash_missing_from_research" in reference_gate["reasons"]
    assert "factual_material_narration_lacks_claim_hash" in reference_gate["reasons"]


def test_gate_fails_when_attributed_claim_loses_attribution(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    section = _section_with_claim(mutated, ATTRIBUTED_HASH)
    section["narration"] = str(section["narration"]).replace(
        "According to DBOS documentation, ",
        "",
    )
    cast(list[dict[str, object]], mutated["sections"])[
        REQUIRED_SECTION_IDS.index(str(section["section_id"]))
    ] = section

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert "attributed_claim_lost_attribution" in gate["reasons"]


@pytest.mark.parametrize(
    ("claim_hash", "assertion", "expected_reason"),
    (
        (
            ESTIMATE_HASH,
            "The narration runtime stays inside a bounded range.",
            "estimate_lost_uncertainty_framing",
        ),
        (
            OPINION_HASH,
            "Durability is most convincing when both layers are tested together.",
            "opinion_lost_editorial_framing",
        ),
    ),
)
def test_gate_fails_when_estimate_or_opinion_loses_required_framing(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
    claim_hash: str,
    assertion: str,
    expected_reason: str,
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    section = _section_with_claim(mutated, claim_hash)
    section["claim_hashes"] = [claim_hash]
    section["narration"] = assertion
    cast(list[dict[str, object]], mutated["sections"])[
        REQUIRED_SECTION_IDS.index(str(section["section_id"]))
    ] = section

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert expected_reason in gate["reasons"]


def test_gate_fails_when_rejected_claim_is_referenced_or_rendered(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    section = _sections(mutated)[2]
    section["claim_hashes"] = [REJECTED_HASH]
    section["narration"] = "Every interrupted workflow always recovers without risk."
    cast(list[dict[str, object]], mutated["sections"])[2] = section

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert "rejected_claim_referenced" in gate["reasons"]
    assert "rejected_claim_appears_in_narration" in gate["reasons"]


@pytest.mark.parametrize(
    "fabricated_identity",
    (
        "I tested this system myself.",
        "I personally observed this behavior in production.",
        "We found the same outcome in our deployment.",
        "I measured this result during an investigation.",
    ),
)
def test_gate_fails_on_fabricated_first_person_identity(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
    fabricated_identity: str,
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    cold_open = _sections(mutated)[0]
    cold_open["narration"] = f"{cold_open['narration']} {fabricated_identity}"
    cast(list[dict[str, object]], mutated["sections"])[0] = cold_open

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert "fabricated_first_person_identity" in gate["reasons"]


def test_gate_fails_on_section_order_narrator_and_cold_open_mutations(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    sections = cast(list[dict[str, object]], mutated["sections"])
    sections[0], sections[1] = sections[1], sections[0]
    mutated["narrator_identity"] = "an invented first-person host"
    sections[0]["continuation_reason"] = ""

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert "required_narrative_sections_missing_or_out_of_order" in gate["reasons"]
    assert "narrator_identity_mismatch" in gate["reasons"]
    assert "cold_open_missing_core_question" in gate["reasons"]
    assert "cold_open_missing_stakes" in gate["reasons"]
    assert "cold_open_missing_continuation_reason" in gate["reasons"]


def test_gate_fails_when_sourceability_lineage_breaks_or_fact_is_not_verified(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    mutated_script = deepcopy(script_packet)
    cast(dict[str, object], mutated_script["source_index"]).pop("dbos-docs")
    mutated_research = deepcopy(research_packet)
    for claim in cast(list[dict[str, object]], mutated_research["claims"]):
        if claim["claim_hash"] == FACT_CONTEXT_HASH:
            claim["state"] = "REJECTED"

    gate = _recheck(mutated_script, mutated_research)

    assert gate["outcome"] == "FAIL"
    assert "sourceability_lineage_broken" in gate["reasons"]
    assert "factual_claim_is_not_verified" in gate["reasons"]


@pytest.mark.parametrize(
    ("section_id", "field"),
    (
        ("synthesis", "narration"),
        ("consequences", "purpose"),
        ("what_next", "continuation_reason"),
        ("setup", "payoff"),
    ),
)
def test_gate_fails_when_any_required_section_field_is_blank(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
    section_id: str,
    field: str,
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    section = cast(list[dict[str, object]], mutated["sections"])[
        REQUIRED_SECTION_IDS.index(section_id)
    ]
    section[field] = "   "

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert f"section_missing_{field}:{section_id}" in gate["reasons"]


def test_gate_fails_when_cold_open_loses_why_now(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    topic_packet, research_packet = script_inputs
    why_now = str(cast(dict[str, object], topic_packet["viewer_promise"])["why_now"])
    mutated = deepcopy(script_packet)
    cold_open = cast(list[dict[str, object]], mutated["sections"])[0]
    cold_open["narration"] = str(cold_open["narration"]).replace(
        f"Its why-now premise, also subject to verification, is: {why_now} ",
        "",
    )

    gate = _recheck(mutated, research_packet)

    assert why_now not in str(cold_open["narration"])
    assert gate["outcome"] == "FAIL"
    assert "cold_open_missing_why_now" in gate["reasons"]


def test_gate_fails_when_claim_index_semantics_change_under_the_same_hash(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    claim_index = cast(dict[str, dict[str, object]], mutated["claim_index"])
    claim_index[FACT_CONTEXT_HASH]["assertion_text"] = (
        "A different assertion hidden beneath the accepted claim hash."
    )

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert "claim_index_does_not_match_research" in gate["reasons"]


def test_gate_fails_when_source_index_semantics_change_under_the_same_key(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    source_index = cast(dict[str, dict[str, object]], mutated["source_index"])
    source_index["dbos-docs"]["publisher"] = "Unrelated Publisher"
    source_index["dbos-docs"]["source_uri"] = "https://example.org/unrelated"

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert "source_index_does_not_match_research" in gate["reasons"]


def test_gate_rejects_hidden_unsupported_empirical_narration(
    script_packet: dict[str, object],
    script_inputs: tuple[dict[str, object], dict[str, object]],
) -> None:
    _, research_packet = script_inputs
    mutated = deepcopy(script_packet)
    cold_open = cast(list[dict[str, object]], mutated["sections"])[0]
    cold_open["narration"] = (
        f"{cold_open['narration']} "
        "Exactly 9,999 developer teams switched inference engines yesterday."
    )
    all_narration = " ".join(
        str(section["narration"])
        for section in cast(list[dict[str, object]], mutated["sections"])
    )
    word_count = len(re.findall(r"\b[\w'-]+\b", all_narration))
    mutated["word_count"] = word_count
    mutated["estimated_runtime_minutes"] = round(
        word_count / NARRATION_WORDS_PER_MINUTE,
        2,
    )

    gate = _recheck(mutated, research_packet)

    assert gate["outcome"] == "FAIL"
    assert "unsupported_or_noncanonical_narration:cold_open" in gate["reasons"]
