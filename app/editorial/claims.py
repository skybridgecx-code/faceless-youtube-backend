from __future__ import annotations

import re
from collections import Counter
from typing import Mapping
from urllib.parse import quote

from .contracts import (
    ClaimEvaluation,
    ClaimSeed,
    EditorialSeed,
    EvidenceSourceInput,
    I4_POLICY_VERSION,
    SourceClass,
    TopicCandidate,
    canonical_json,
    canonical_sha256,
    normalize_key,
)
from .safety import compliance_failures, sensitivity_failures
from .topic_intelligence import topic_hard_failures


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def normalize_publisher(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def claim_identity_payload(claim: ClaimSeed) -> dict[str, object]:
    return {
        "assertion_text": normalize_whitespace(claim.assertion_text),
        "assumptions": [normalize_whitespace(value) for value in claim.assumptions],
        "attribution": (
            normalize_whitespace(claim.attribution)
            if claim.attribution is not None
            else None
        ),
        "claim_type": claim.claim_type,
        "material": claim.material,
        "role": claim.role,
        "source_keys": sorted(set(claim.source_keys)),
    }


def claim_hash(claim: ClaimSeed) -> str:
    return canonical_sha256(claim_identity_payload(claim))


def evaluate_claim(
    claim: ClaimSeed,
    sources_by_key: Mapping[str, EvidenceSourceInput],
) -> ClaimEvaluation:
    identity = claim_identity_payload(claim)
    supporting_sources = [
        sources_by_key[key]
        for key in identity["source_keys"]  # type: ignore[union-attr]
        if key in sources_by_key
    ]
    unknown_source_keys = tuple(
        sorted(set(identity["source_keys"]) - set(sources_by_key))  # type: ignore[arg-type]
    )
    reasons: list[str] = []
    state: str

    if unknown_source_keys:
        state = "REJECTED"
        reasons.append("unknown_source_keys:" + ",".join(unknown_source_keys))
    elif claim.claim_type == "fact":
        has_authoritative_source = any(
            source.source_class in {"primary", "official"}
            for source in supporting_sources
        )
        independent_secondary_publishers = {
            normalize_publisher(source.publisher)
            for source in supporting_sources
            if source.source_class == "reputable_secondary"
            and normalize_publisher(source.publisher)
        }
        if has_authoritative_source:
            state = "VERIFIED"
            reasons.append("primary_or_official_source_verifies_fact")
        elif len(independent_secondary_publishers) >= 2:
            state = "VERIFIED"
            reasons.append("two_independent_reputable_secondary_publishers_verify_fact")
        else:
            state = "REJECTED"
            reasons.append("fact_verification_rule_not_met")
    elif claim.claim_type == "attributed_claim":
        has_attribution = bool(identity["attribution"])
        has_attribution_source = any(
            source.source_class in {"primary", "official", "company_claim"}
            for source in supporting_sources
        )
        if has_attribution and has_attribution_source:
            state = "VERIFIED"
            reasons.append("attribution_and_authorized_source_present")
        else:
            state = "REJECTED"
            if not has_attribution:
                reasons.append("attribution_required")
            if not has_attribution_source:
                reasons.append("attribution_source_required")
    elif claim.claim_type == "estimate":
        has_assumptions = bool(identity["assumptions"])
        has_context_source = any(
            source.source_class != "discovery_only"
            for source in supporting_sources
        )
        if has_assumptions and (not claim.material or has_context_source):
            state = "ESTIMATE"
            reasons.append("estimate_assumptions_and_context_present")
        else:
            state = "REJECTED"
            if not has_assumptions:
                reasons.append("estimate_assumptions_required")
            if claim.material and not has_context_source:
                reasons.append("material_estimate_context_source_required")
    else:
        state = "OPINION"
        reasons.append("explicit_editorial_opinion")

    return ClaimEvaluation(
        claim_hash=canonical_sha256(identity),
        assertion_text=str(identity["assertion_text"]),
        material=claim.material,
        claim_type=claim.claim_type,
        role=claim.role,
        source_keys=tuple(identity["source_keys"]),  # type: ignore[arg-type]
        attribution=(
            str(identity["attribution"])
            if identity["attribution"] is not None
            else None
        ),
        assumptions=tuple(identity["assumptions"]),  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        reasons=tuple(reasons),
    )


def _source_hash_payload(
    *,
    source_uri: str,
    publisher: str,
    source_class: SourceClass,
    evidence_snippet: str,
    evidence_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "evidence_metadata": evidence_metadata or {},
        "evidence_snippet": normalize_whitespace(evidence_snippet),
        "publisher": normalize_whitespace(publisher),
        "rights_status": "reference_only",
        "source_class": source_class,
        "source_uri": source_uri,
    }


def _source_record(
    *,
    source_key: str,
    source_uri: str,
    publisher: str,
    source_class: SourceClass,
    evidence_snippet: str,
    retrieved_at: str,
    origin: str,
    evidence_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    hash_payload = _source_hash_payload(
        source_uri=source_uri,
        publisher=publisher,
        source_class=source_class,
        evidence_snippet=evidence_snippet,
        evidence_metadata=evidence_metadata,
    )
    return {
        "content_sha256": canonical_sha256(hash_payload),
        "evidence_snippet": hash_payload["evidence_snippet"],
        "provenance": {
            "hash_scope": sorted(hash_payload),
            "origin": origin,
            "source_key": source_key,
            "evidence_metadata": evidence_metadata or {},
        },
        "publisher": hash_payload["publisher"],
        "retrieved_at": retrieved_at,
        "rights_status": "reference_only",
        "source_class": source_class,
        "source_key": source_key,
        "source_uri": source_uri,
    }


def source_record_content_sha256(source: Mapping[str, object]) -> str:
    provenance = source.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("source provenance must be an object")
    evidence_metadata = provenance.get("evidence_metadata") or {}
    if not isinstance(evidence_metadata, dict):
        raise ValueError("source evidence metadata must be an object")
    hash_payload = _source_hash_payload(
        source_uri=str(source["source_uri"]),
        publisher=str(source["publisher"]),
        source_class=str(source["source_class"]),  # type: ignore[arg-type]
        evidence_snippet=str(source["evidence_snippet"]),
        evidence_metadata=evidence_metadata,
    )
    if provenance.get("hash_scope") != sorted(hash_payload):
        raise ValueError("source provenance hash scope is invalid")
    return canonical_sha256(hash_payload)


def _selected_candidate(seed: EditorialSeed, key: str) -> TopicCandidate:
    for candidate in seed.candidates:
        if candidate.candidate_key == key:
            return candidate
    raise ValueError("Selected topic candidate is missing from the immutable seed")


def _selected_ranked_candidate(
    topic_packet: dict[str, object],
    key: str,
) -> dict[str, object]:
    for raw in topic_packet.get("ranked_candidates", []):  # type: ignore[assignment]
        item = dict(raw)
        if item.get("candidate_key") == key:
            return item
    raise ValueError("Selected topic candidate is missing from the topic packet")


def build_research_sources(
    candidate: TopicCandidate,
    *,
    topic_packet: dict[str, object],
) -> list[dict[str, object]]:
    retrieved_at = str(topic_packet["retrieved_at"])
    records = [
        _source_record(
            source_key=source.source_key,
            source_uri=source.source_uri,
            publisher=source.publisher,
            source_class=source.source_class,
            evidence_snippet=source.evidence_snippet,
            retrieved_at=retrieved_at,
            origin="editorial_seed",
        )
        for source in candidate.evidence_sources
    ]
    used_keys = {str(record["source_key"]) for record in records}
    used_uris = {str(record["source_uri"]) for record in records}
    ranked = _selected_ranked_candidate(topic_packet, candidate.candidate_key)
    demand = dict(ranked.get("demand_evidence") or {})
    for raw_video in demand.get("videos", []):
        video = dict(raw_video)
        video_id = str(video.get("video_id") or "").strip()
        if not video_id:
            continue
        base_key = normalize_key(f"youtube-{video_id}")
        source_key = base_key
        if source_key in used_keys:
            source_key = f"{base_key}-{canonical_sha256(video)[:8]}"
        used_keys.add(source_key)
        source_uri = f"https://www.youtube.com/watch?v={quote(video_id, safe='-_')}"
        if source_uri in used_uris:
            continue
        used_uris.add(source_uri)
        publisher = str(video.get("channel_title") or video.get("channel_id") or "YouTube")
        title = normalize_whitespace(str(video.get("title") or "Public YouTube result"))
        evidence_metadata = {
            "channel_id": video.get("channel_id"),
            "channel_subscriber_count": video.get("channel_subscriber_count"),
            "comment_count": video.get("comment_count"),
            "like_count": video.get("like_count"),
            "published_at": video.get("published_at"),
            "view_count": video.get("view_count"),
            "video_id": video_id,
        }
        records.append(
            _source_record(
                source_key=source_key,
                source_uri=source_uri,
                publisher=publisher,
                source_class="discovery_only",
                evidence_snippet=f"Public YouTube demand result title: {title}",
                retrieved_at=retrieved_at,
                origin="youtube_demand_snapshot",
                evidence_metadata=evidence_metadata,
            )
        )
    return records


def compile_research_packet(
    *,
    campaign_id: int,
    seed: EditorialSeed,
    topic_packet: dict[str, object],
    topic_packet_hash: str,
) -> dict[str, object]:
    selected_key = str(topic_packet.get("selected_candidate_key") or "")
    if not selected_key:
        raise ValueError("Topic packet has no selected candidate")
    candidate = _selected_candidate(seed, selected_key)
    sources_by_key = {
        source.source_key: source for source in candidate.evidence_sources
    }
    evaluated_claims = [
        evaluate_claim(claim, sources_by_key)
        for claim in candidate.claims
    ]
    evaluations_by_hash = {
        evaluation.claim_hash: evaluation for evaluation in evaluated_claims
    }
    evaluations = [
        evaluations_by_hash[key] for key in sorted(evaluations_by_hash)
    ]
    sources = build_research_sources(candidate, topic_packet=topic_packet)

    usable_material = [
        claim for claim in evaluations if claim.material and claim.state != "REJECTED"
    ]
    verified_material_factual = [
        claim
        for claim in evaluations
        if claim.material
        and claim.claim_type in {"fact", "attributed_claim"}
        and claim.state == "VERIFIED"
    ]
    rejected_material = [
        claim for claim in evaluations if claim.material and claim.state == "REJECTED"
    ]
    story_supporting_source_keys = {
        source_key
        for claim in usable_material
        if claim.claim_type in {"fact", "attributed_claim", "estimate"}
        for source_key in claim.source_keys
    }
    independent_publishers = sorted(
        {
            normalize_publisher(source.publisher)
            for source in candidate.evidence_sources
            if source.source_key in story_supporting_source_keys
            and source.source_class != "discovery_only"
            and normalize_publisher(source.publisher)
        }
    )

    failures: set[str] = set()
    if len(evaluations) != len(evaluated_claims):
        failures.add("duplicate_semantic_claims_do_not_count_toward_sourceability")
    if rejected_material:
        failures.add("rejected_material_claim_present")
    if len(usable_material) < 6:
        failures.add("fewer_than_six_usable_material_claims")
    if len(verified_material_factual) < 4:
        failures.add("fewer_than_four_verified_material_factual_claims")
    if len(independent_publishers) < 2:
        failures.add("fewer_than_two_independent_non_discovery_publishers")
    failures.update(topic_hard_failures(candidate))
    failures.update(sensitivity_failures(candidate))
    failures.update(compliance_failures(candidate))

    counts = Counter(claim.state for claim in evaluations)
    usable_claims = [
        claim.model_dump(mode="json")
        for claim in evaluations
        if claim.state != "REJECTED"
    ]
    rejected_claims = [
        claim.model_dump(mode="json")
        for claim in evaluations
        if claim.state == "REJECTED"
    ]
    return {
        "campaign_id": campaign_id,
        "claim_summary": {
            "ESTIMATE": counts["ESTIMATE"],
            "OPINION": counts["OPINION"],
            "REJECTED": counts["REJECTED"],
            "VERIFIED": counts["VERIFIED"],
            "material_usable": len(usable_material),
            "material_verified_factual": len(verified_material_factual),
        },
        "claims": usable_claims,
        "contract_version": "i4-research-packet-v1",
        "gate": {
            "outcome": "FAIL" if failures else "PASS",
            "reasons": (
                sorted(failures)
                if failures
                else ["research_is_sourceable_and_claim_verified"]
            ),
        },
        "independent_publishers": independent_publishers,
        "policy_version": I4_POLICY_VERSION,
        "rejected_claims": rejected_claims,
        "selected_candidate_key": selected_key,
        "source_summary": {
            "discovery_only": sum(
                1 for source in sources if source["source_class"] == "discovery_only"
            ),
            "non_discovery": sum(
                1 for source in sources if source["source_class"] != "discovery_only"
            ),
            "total": len(sources),
        },
        "sourceability_metrics": {
            "independent_non_discovery_publishers": len(independent_publishers),
            "material_claims_usable": len(usable_material),
            "material_factual_claims_verified": len(verified_material_factual),
        },
        "sources": sources,
        "topic_packet_hash": topic_packet_hash,
    }


def claim_structured_value_json(claim: Mapping[str, object]) -> str:
    return canonical_json(
        {
            "assumptions": claim["assumptions"],
            "attribution": claim["attribution"],
            "claim_type": claim["claim_type"],
            "reasons": claim["reasons"],
            "role": claim["role"],
            "source_keys": claim["source_keys"],
        }
    )
