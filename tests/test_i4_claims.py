from __future__ import annotations

import json

import pytest

from app.editorial.claims import (
    build_research_sources,
    claim_hash,
    claim_structured_value_json,
    compile_research_packet,
    evaluate_claim,
    normalize_publisher,
)
from app.editorial.contracts import (
    ClaimSeed,
    EditorialSeed,
    EvidenceSourceInput,
    TopicCandidate,
)


RETRIEVED_AT = "2026-08-11T12:00:00+00:00"


def _source(
    source_key: str,
    source_class: str,
    publisher: str,
    *,
    evidence_snippet: str | None = None,
) -> EvidenceSourceInput:
    return EvidenceSourceInput(
        source_key=source_key,
        source_uri=f"https://evidence.example/{source_key}",
        publisher=publisher,
        source_class=source_class,
        evidence_snippet=evidence_snippet or f"Bounded excerpt for {source_key}.",
    )


def _sources() -> tuple[EvidenceSourceInput, ...]:
    return (
        _source("primary-record", "primary", "Primary Records Office"),
        _source("official-data", "official", "Public Data Office"),
        _source("secondary-one", "reputable_secondary", "Independent Journal"),
        _source("secondary-two", "reputable_secondary", "Evidence Review"),
        _source("company-statement", "company_claim", "Example Company"),
        _source("discovery-result", "discovery_only", "Discovery Channel"),
    )


def _sources_by_key(
    sources: tuple[EvidenceSourceInput, ...] | None = None,
) -> dict[str, EvidenceSourceInput]:
    selected = sources or _sources()
    return {source.source_key: source for source in selected}


def _claim(
    assertion_text: str = "The documented workflow completed the reported step.",
    *,
    claim_type: str = "fact",
    role: str = "evidence",
    source_keys: tuple[str, ...] = ("official-data",),
    attribution: str | None = None,
    assumptions: tuple[str, ...] = (),
    material: bool = True,
) -> ClaimSeed:
    return ClaimSeed(
        assertion_text=assertion_text,
        material=material,
        claim_type=claim_type,
        role=role,
        source_keys=source_keys,
        attribution=attribution,
        assumptions=assumptions,
    )


def _happy_claims() -> tuple[ClaimSeed, ...]:
    return (
        _claim(
            "The official dataset defines the observed sample.",
            role="context",
        ),
        _claim("The documented workflow completed the reported step."),
        _claim(
            "A failed handoff can consume operator time and reduce trust.",
            role="stakes",
        ),
        _claim(
            "The company describes the feature as an operational assistant.",
            claim_type="attributed_claim",
            role="counterpoint",
            source_keys=("company-statement",),
            attribution="Example Company",
        ),
        _claim(
            "The bounded sample could suggest an efficiency improvement.",
            claim_type="estimate",
            role="uncertainty",
            source_keys=("secondary-one",),
            assumptions=("The documented sample represents this workflow.",),
        ),
        _claim(
            "Editorial caution is more useful than treating a demo as proof.",
            claim_type="opinion",
            role="outlook",
            source_keys=(),
        ),
    )


def _candidate(
    *,
    claims: tuple[ClaimSeed, ...] | None = None,
    sources: tuple[EvidenceSourceInput, ...] | None = None,
) -> TopicCandidate:
    return TopicCandidate(
        candidate_key="evidence-workflow",
        topic="Evidence standards for creator automation workflows",
        angle="A skeptical investigation of documented capabilities and limits",
        target_viewer="Creator operators and small business teams",
        viewer_promise="A source-backed map of what the workflow can and cannot do.",
        core_question="Can the workflow support its operational claims without hype?",
        why_now="Current public documentation makes the operational tradeoffs timely.",
        stakes="Teams can waste time, trust, and scarce resources on unsupported systems.",
        novelty="The analysis separates verified evidence from estimates and opinion.",
        broad_interest_bridge="The same evidence discipline affects consequential software choices.",
        expected_takeaway="Viewers will leave with an evidence checklist and explicit limits.",
        visual_modes=("DOCUMENT", "DIAGRAM", "DATA_VISUALIZATION"),
        shelf_life="evergreen",
        sponsor_categories=("creator software",),
        sensitivity_tags=(),
        evidence_sources=sources or _sources(),
        claims=claims or _happy_claims(),
    )


def _replace_candidate(candidate: TopicCandidate, **updates: object) -> TopicCandidate:
    payload = candidate.model_dump(mode="python")
    payload.update(updates)
    return TopicCandidate.model_validate(payload)


def _topic_packet(
    candidate: TopicCandidate,
    *,
    demand_videos: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return {
        "selected_candidate_key": candidate.candidate_key,
        "retrieved_at": RETRIEVED_AT,
        "ranked_candidates": [
            {
                "candidate_key": candidate.candidate_key,
                "demand_evidence": {
                    "candidate_key": candidate.candidate_key,
                    "query": candidate.topic,
                    "videos": list(demand_videos),
                },
            }
        ],
    }


def _compile(candidate: TopicCandidate) -> dict[str, object]:
    return compile_research_packet(
        campaign_id=71,
        seed=EditorialSeed(candidates=(candidate,)),
        topic_packet=_topic_packet(candidate),
        topic_packet_hash="a" * 64,
    )


@pytest.mark.parametrize("source_key", ["primary-record", "official-data"])
def test_primary_or_official_source_verifies_a_fact(source_key: str) -> None:
    evaluation = evaluate_claim(
        _claim(source_keys=(source_key,)),
        _sources_by_key(),
    )

    assert evaluation.state == "VERIFIED"
    assert evaluation.reasons == ("primary_or_official_source_verifies_fact",)


def test_two_independent_reputable_secondary_publishers_verify_a_fact() -> None:
    evaluation = evaluate_claim(
        _claim(source_keys=("secondary-one", "secondary-two")),
        _sources_by_key(),
    )

    assert evaluation.state == "VERIFIED"
    assert evaluation.reasons == (
        "two_independent_reputable_secondary_publishers_verify_fact",
    )


def test_one_reputable_secondary_does_not_verify_a_fact() -> None:
    evaluation = evaluate_claim(
        _claim(source_keys=("secondary-one",)),
        _sources_by_key(),
    )

    assert evaluation.state == "REJECTED"
    assert evaluation.reasons == ("fact_verification_rule_not_met",)


def test_duplicate_normalized_secondary_publishers_are_not_independent() -> None:
    sources = (
        _source("secondary-a", "reputable_secondary", "The Daily-News"),
        _source("secondary-b", "reputable_secondary", "  the daily news  "),
    )
    evaluation = evaluate_claim(
        _claim(source_keys=("secondary-a", "secondary-b")),
        _sources_by_key(sources),
    )

    assert normalize_publisher(sources[0].publisher) == normalize_publisher(sources[1].publisher)
    assert evaluation.state == "REJECTED"


def test_discovery_only_never_verifies_a_material_fact() -> None:
    evaluation = evaluate_claim(
        _claim(source_keys=("discovery-result",)),
        _sources_by_key(),
    )

    assert evaluation.material is True
    assert evaluation.state == "REJECTED"


def test_company_claim_cannot_verify_an_unattributed_fact() -> None:
    evaluation = evaluate_claim(
        _claim(source_keys=("company-statement",)),
        _sources_by_key(),
    )

    assert evaluation.state == "REJECTED"
    assert evaluation.reasons == ("fact_verification_rule_not_met",)


@pytest.mark.parametrize("source_key", ["company-statement", "official-data"])
def test_company_or_official_source_verifies_a_properly_attributed_claim(
    source_key: str,
) -> None:
    evaluation = evaluate_claim(
        _claim(
            "The organization describes the feature as an assistant.",
            claim_type="attributed_claim",
            source_keys=(source_key,),
            attribution="The documented organization",
        ),
        _sources_by_key(),
    )

    assert evaluation.state == "VERIFIED"
    assert evaluation.attribution == "The documented organization"
    assert evaluation.reasons == ("attribution_and_authorized_source_present",)


def test_attributed_claim_requires_explicit_attribution() -> None:
    evaluation = evaluate_claim(
        _claim(
            claim_type="attributed_claim",
            source_keys=("company-statement",),
            attribution=None,
        ),
        _sources_by_key(),
    )

    assert evaluation.state == "REJECTED"
    assert "attribution_required" in evaluation.reasons


@pytest.mark.parametrize(
    ("source_keys", "assumptions", "expected_state", "expected_reason"),
    [
        (
            ("official-data",),
            ("The documented sample represents this workflow.",),
            "ESTIMATE",
            "estimate_assumptions_and_context_present",
        ),
        (
            ("official-data",),
            (),
            "REJECTED",
            "estimate_assumptions_required",
        ),
        (
            ("discovery-result",),
            ("The search result sample is representative.",),
            "REJECTED",
            "material_estimate_context_source_required",
        ),
    ],
)
def test_material_estimate_requires_assumptions_and_non_discovery_context(
    source_keys: tuple[str, ...],
    assumptions: tuple[str, ...],
    expected_state: str,
    expected_reason: str,
) -> None:
    evaluation = evaluate_claim(
        _claim(
            "The sample could suggest a bounded change.",
            claim_type="estimate",
            source_keys=source_keys,
            assumptions=assumptions,
        ),
        _sources_by_key(),
    )

    assert evaluation.state == expected_state
    assert expected_reason in evaluation.reasons


def test_opinion_remains_explicitly_opinion_without_sources() -> None:
    evaluation = evaluate_claim(
        _claim(
            "Editorial caution is the more useful interpretation.",
            claim_type="opinion",
            source_keys=(),
        ),
        _sources_by_key(),
    )

    assert evaluation.state == "OPINION"
    assert evaluation.reasons == ("explicit_editorial_opinion",)


def test_unknown_source_reference_rejects_claim_before_type_policy() -> None:
    evaluation = evaluate_claim(
        _claim(source_keys=("missing-source",)),
        _sources_by_key(),
    )

    assert evaluation.state == "REJECTED"
    assert evaluation.reasons == ("unknown_source_keys:missing-source",)


def test_claim_hash_reconciles_normalized_semantics_and_detects_changes() -> None:
    first = _claim(
        "The   documented workflow completed the step.",
        source_keys=("Official Data", "official-data"),
    )
    same = _claim(
        "The documented workflow completed the step.",
        source_keys=("official-data",),
    )
    changed_role = _claim(
        "The documented workflow completed the step.",
        role="stakes",
        source_keys=("official-data",),
    )

    assert first.source_keys == ("official-data",)
    assert claim_hash(first) == claim_hash(same)
    assert len(claim_hash(first)) == 64
    assert claim_hash(changed_role) != claim_hash(first)


def test_claim_structured_metadata_is_canonical_and_reconstructable() -> None:
    evaluation = evaluate_claim(
        _claim(source_keys=("official-data",)),
        _sources_by_key(),
    )

    serialized = claim_structured_value_json(evaluation.model_dump(mode="json"))
    parsed = json.loads(serialized)

    assert serialized == claim_structured_value_json(evaluation.model_dump(mode="json"))
    assert parsed == {
        "assumptions": [],
        "attribution": None,
        "claim_type": "fact",
        "reasons": ["primary_or_official_source_verifies_fact"],
        "role": "evidence",
        "source_keys": ["official-data"],
    }


def test_happy_research_packet_passes_with_six_usable_material_claims() -> None:
    packet = _compile(_candidate())

    assert packet["gate"] == {
        "outcome": "PASS",
        "reasons": ["research_is_sourceable_and_claim_verified"],
    }
    assert packet["claim_summary"] == {
        "ESTIMATE": 1,
        "OPINION": 1,
        "REJECTED": 0,
        "VERIFIED": 4,
        "material_usable": 6,
        "material_verified_factual": 4,
    }
    assert len(packet["claims"]) == 6
    assert packet["rejected_claims"] == []
    assert packet["sourceability_metrics"]["independent_non_discovery_publishers"] == 3  # type: ignore[index]


def test_unused_publishers_do_not_satisfy_research_independence_gate() -> None:
    claims = list(_happy_claims())
    claims[3] = _claim(
        "The official source describes the feature as an operational assistant.",
        claim_type="attributed_claim",
        role="counterpoint",
        source_keys=("official-data",),
        attribution="Public Data Office",
    )
    claims[4] = _claim(
        "The bounded sample could suggest an efficiency improvement.",
        claim_type="estimate",
        role="uncertainty",
        source_keys=("official-data",),
        assumptions=("The documented sample represents this workflow.",),
    )

    packet = _compile(_candidate(claims=tuple(claims)))

    assert packet["sourceability_metrics"]["independent_non_discovery_publishers"] == 1  # type: ignore[index]
    assert packet["gate"]["outcome"] == "FAIL"  # type: ignore[index]
    assert "fewer_than_two_independent_non_discovery_publishers" in packet["gate"]["reasons"]  # type: ignore[index]


def test_opinion_only_source_does_not_satisfy_research_independence_gate() -> None:
    claims = list(_happy_claims())
    claims[3] = _claim(
        "The official source describes the feature as an operational assistant.",
        claim_type="attributed_claim",
        role="counterpoint",
        source_keys=("official-data",),
        attribution="Public Data Office",
    )
    claims[4] = _claim(
        "The bounded sample could suggest an efficiency improvement.",
        claim_type="estimate",
        role="uncertainty",
        source_keys=("official-data",),
        assumptions=("The documented sample represents this workflow.",),
    )
    claims[5] = _claim(
        "Editorial caution is more useful than treating a demo as proof.",
        claim_type="opinion",
        role="outlook",
        source_keys=("secondary-one",),
    )

    packet = _compile(_candidate(claims=tuple(claims)))

    assert packet["sourceability_metrics"]["independent_non_discovery_publishers"] == 1  # type: ignore[index]
    assert packet["gate"]["outcome"] == "FAIL"  # type: ignore[index]
    assert "fewer_than_two_independent_non_discovery_publishers" in packet["gate"]["reasons"]  # type: ignore[index]


def test_rejected_material_claim_causes_research_gate_failure() -> None:
    claims = list(_happy_claims())
    claims[0] = _claim(
        "A single article proves the entire workflow outcome.",
        role="context",
        source_keys=("secondary-one",),
    )
    packet = _compile(_candidate(claims=tuple(claims)))

    assert packet["gate"]["outcome"] == "FAIL"  # type: ignore[index]
    assert "rejected_material_claim_present" in packet["gate"]["reasons"]  # type: ignore[index]
    assert packet["claim_summary"]["REJECTED"] == 1  # type: ignore[index]
    assert len(packet["rejected_claims"]) == 1


def test_duplicate_semantic_claims_do_not_inflate_research_sourceability() -> None:
    repeated = _claim("The official dataset defines the observed sample.", role="context")
    candidate = _candidate(claims=(repeated,) * 6)

    packet = _compile(candidate)

    assert packet["gate"]["outcome"] == "FAIL"  # type: ignore[index]
    reasons = packet["gate"]["reasons"]  # type: ignore[index]
    assert "duplicate_semantic_claims_do_not_count_toward_sourceability" in reasons
    assert "fewer_than_six_usable_material_claims" in reasons
    assert len(packet["claims"]) == 1


def test_source_records_reconcile_normalized_content_and_hash_semantic_conflicts() -> None:
    candidate = _candidate()
    first = build_research_sources(candidate, topic_packet=_topic_packet(candidate))
    second = build_research_sources(candidate, topic_packet=_topic_packet(candidate))

    whitespace_sources = list(candidate.evidence_sources)
    whitespace_sources[0] = EvidenceSourceInput.model_validate(
        {
            **whitespace_sources[0].model_dump(mode="python"),
            "evidence_snippet": "Bounded   excerpt\nfor primary-record.",
        }
    )
    whitespace_candidate = _replace_candidate(
        candidate,
        evidence_sources=tuple(whitespace_sources),
    )
    normalized = build_research_sources(
        whitespace_candidate,
        topic_packet=_topic_packet(whitespace_candidate),
    )

    changed_sources = list(candidate.evidence_sources)
    changed_sources[0] = EvidenceSourceInput.model_validate(
        {
            **changed_sources[0].model_dump(mode="python"),
            "evidence_snippet": "A materially different bounded excerpt.",
        }
    )
    changed_candidate = _replace_candidate(
        candidate,
        evidence_sources=tuple(changed_sources),
    )
    changed = build_research_sources(
        changed_candidate,
        topic_packet=_topic_packet(changed_candidate),
    )

    assert first == second
    assert first[0]["content_sha256"] == normalized[0]["content_sha256"]
    assert first[0]["content_sha256"] != changed[0]["content_sha256"]
    assert first[0]["rights_status"] == "reference_only"
    assert first[0]["provenance"]["hash_scope"] == [  # type: ignore[index]
        "evidence_metadata",
        "evidence_snippet",
        "publisher",
        "rights_status",
        "source_class",
        "source_uri",
    ]


def test_youtube_demand_sources_are_discovery_only_reference_records() -> None:
    candidate = _candidate()
    demand_video = {
        "video_id": "public_video-1",
        "title": "Public demand result",
        "channel_id": "channel-1",
        "channel_title": "Public Channel",
        "published_at": "2026-08-01T12:00:00+00:00",
        "view_count": 12_000,
        "like_count": 600,
        "comment_count": 120,
        "channel_subscriber_count": 80_000,
        "description": "This whole description must not be ingested.",
    }

    records = build_research_sources(
        candidate,
        topic_packet=_topic_packet(candidate, demand_videos=(demand_video,)),
    )
    youtube = records[-1]

    assert youtube["source_class"] == "discovery_only"
    assert youtube["rights_status"] == "reference_only"
    assert youtube["source_uri"] == "https://www.youtube.com/watch?v=public_video-1"
    assert youtube["evidence_snippet"] == "Public YouTube demand result title: Public demand result"
    assert "description" not in youtube["provenance"]["evidence_metadata"]  # type: ignore[operator]


def test_youtube_demand_uri_collision_preserves_stronger_seed_evidence() -> None:
    candidate = _candidate()
    evidence_sources = list(candidate.evidence_sources)
    evidence_sources[0] = evidence_sources[0].model_copy(
        update={"source_uri": "https://www.youtube.com/watch?v=shared-video"}
    )
    candidate = _replace_candidate(
        candidate,
        evidence_sources=tuple(evidence_sources),
    )
    demand_video = {
        "video_id": "shared-video",
        "title": "Demand result already supplied as evidence",
        "channel_id": "channel-1",
        "channel_title": "Public Channel",
        "published_at": RETRIEVED_AT,
        "view_count": 10_000,
        "like_count": 500,
        "comment_count": 100,
        "channel_subscriber_count": 50_000,
    }

    records = build_research_sources(
        candidate,
        topic_packet=_topic_packet(candidate, demand_videos=(demand_video,)),
    )

    matching = [
        record
        for record in records
        if record["source_uri"] == "https://www.youtube.com/watch?v=shared-video"
    ]
    assert len(matching) == 1
    assert matching[0]["source_class"] == evidence_sources[0].source_class
    assert matching[0]["provenance"]["origin"] == "editorial_seed"  # type: ignore[index]


def test_duplicate_claim_source_references_normalize_to_one_link_identity() -> None:
    claim = _claim(
        source_keys=("Official Data", "official-data", "OFFICIAL--DATA"),
    )
    evaluation = evaluate_claim(claim, _sources_by_key())

    assert claim.source_keys == ("official-data",)
    assert evaluation.source_keys == ("official-data",)
    assert evaluation.state == "VERIFIED"
