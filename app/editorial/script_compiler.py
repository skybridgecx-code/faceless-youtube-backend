from __future__ import annotations

import re
from collections import defaultdict
from typing import Mapping, Sequence

from app.services.compliance import scan_text

from .contracts import I4_POLICY_VERSION
from .safety import contains_fabricated_identity


NARRATOR_IDENTITY = (
    "curious, technically literate, skeptical of hype, explicit about uncertainty, "
    "and interested in consequences"
)
NARRATION_WORDS_PER_MINUTE = 150
I4_TARGET_MIN_RUNTIME_MINUTES = 8
I4_TARGET_MAX_RUNTIME_MINUTES = 12
I4_TARGET_MIN_WORDS = NARRATION_WORDS_PER_MINUTE * I4_TARGET_MIN_RUNTIME_MINUTES
I4_TARGET_MAX_WORDS = NARRATION_WORDS_PER_MINUTE * I4_TARGET_MAX_RUNTIME_MINUTES
REQUIRED_SECTION_IDS: tuple[str, ...] = (
    "cold_open",
    "setup",
    "evidence_build",
    "tension",
    "synthesis",
    "consequences",
    "resolution",
    "what_next",
)

_SECTION_ROLES: dict[str, tuple[str, ...]] = {
    "cold_open": ("stakes",),
    "setup": ("context",),
    "evidence_build": ("evidence",),
    "tension": ("counterpoint", "uncertainty"),
    "synthesis": ("evidence", "outlook"),
    "consequences": ("stakes", "outlook"),
    "resolution": ("context",),
    "what_next": ("outlook", "uncertainty"),
}

_SECTION_CLAIM_LIMITS: dict[str, int] = {
    "cold_open": 1,
    "setup": 1,
    "evidence_build": 1,
    "tension": 2,
    "synthesis": 2,
    "consequences": 2,
    "resolution": 1,
    "what_next": 1,
}

_MAX_REFERENCES_PER_CLAIM = 2

_PURPOSES: dict[str, str] = {
    "cold_open": "Establish the core question, timely stakes, and continuation reason.",
    "setup": "Define the context and the evidence standard for the episode.",
    "evidence_build": "Build the strongest verified evidence in a deliberate sequence.",
    "tension": "Surface counterevidence, limits, and unresolved uncertainty.",
    "synthesis": "Separate supported conclusions from estimates and editorial analysis.",
    "consequences": "Explain the practical and broad-interest consequences.",
    "resolution": "Resolve the Viewer Promise without overstating the record.",
    "what_next": "Identify what evidence or developments would change the conclusion.",
}

_CONTINUATIONS: dict[str, str] = {
    "cold_open": "Continue to test whether the promise survives contact with the evidence.",
    "setup": "Continue to see which claims meet the verification standard.",
    "evidence_build": "Continue because the strongest evidence still has to face its counterpoint.",
    "tension": "Continue to learn which conclusion remains defensible under uncertainty.",
    "synthesis": "Continue to connect the supported conclusion to real consequences.",
    "consequences": "Continue to see what the evidence actually resolves for the viewer.",
    "resolution": "Continue for the concrete signals worth watching next.",
    "what_next": "The next payoff comes from comparing future evidence with this bounded conclusion.",
}

_PAYOFFS: dict[str, str] = {
    "cold_open": "The episode states the precise question it will answer.",
    "setup": "The audience receives a clear map of facts, claims, estimates, and analysis.",
    "evidence_build": "The strongest sourceable claims are made explicit.",
    "tension": "The evidence is tested against counterpoints and uncertainty.",
    "synthesis": "The script distinguishes the supported thesis from editorial interpretation.",
    "consequences": "The viewer sees why the evidence matters beyond the narrow topic.",
    "resolution": "The expected takeaway is resolved without inventing certainty.",
    "what_next": "The audience leaves with observable next signals rather than a prediction dressed as fact.",
}


def _render_claim(
    claim: Mapping[str, object],
    *,
    source_index: Mapping[str, Mapping[str, object]],
    include_evidence_detail: bool,
) -> str:
    assertion = str(claim["assertion_text"]).strip()
    claim_type = str(claim["claim_type"])
    if claim_type == "attributed_claim":
        rendered = f"According to {claim['attribution']}, {assertion}"
    elif claim_type == "estimate":
        assumptions = "; ".join(str(value) for value in claim.get("assumptions", []))
        rendered = (
            f"An estimate, based on {assumptions}, suggests {assertion}. "
            "It is approximate and could change if those assumptions change."
        )
    elif claim_type == "opinion":
        return f"Our read is: {assertion} This is editorial analysis, not a verified fact."
    else:
        rendered = assertion

    source_records = [
        source_index[str(source_key)]
        for source_key in claim.get("source_keys", [])
        if str(source_key) in source_index
    ]
    publishers = sorted({str(source["publisher"]) for source in source_records})
    if publishers:
        if include_evidence_detail:
            excerpts = [
                (
                    f"{source['publisher']} ({source['source_class']}) records this "
                    f"bounded excerpt: {source.get('evidence_snippet')}."
                )
                for source in source_records
                if str(source.get("evidence_snippet") or "").strip()
            ]
            if excerpts:
                rendered += " " + " ".join(excerpts)
            rendered += (
                " That evidence lineage defines the scope of the claim; it does not "
                "authorize a broader conclusion."
            )
        else:
            rendered += (
                " Revisited here, the claim remains bounded by the recorded lineage "
                f"from {', '.join(publishers)} rather than being upgraded by repetition."
            )
    return rendered


def _section_framing(
    section_id: str,
    *,
    viewer_promise: Mapping[str, object],
    topic: str,
    angle: str,
) -> str:
    if section_id == "cold_open":
        return (
            f"{viewer_promise['core_question']} "
            "The editorial brief frames the stakes as a premise to test: "
            f"{viewer_promise['stakes']} "
            "Its why-now premise, also subject to verification, is: "
            f"{viewer_promise['why_now']} "
            "This opening does not treat either premise as an established fact. It names "
            "the decision the episode must resolve, then asks a source-bound claim to "
            "show where the real consequence begins. The reason to continue is the gap "
            "between the urgency proposed by the brief and the narrower conclusion the "
            "record may actually support. That distinction matters because a practical "
            "decision can be urgent even while the opening explanation remains provisional."
        )
    if section_id == "setup":
        return (
            f"This episode examines {topic} through the angle: {angle}. "
            "The editorial promise under review is: "
            f"{viewer_promise['viewer_promise']} "
            "The intended audience named by the brief is: "
            f"{viewer_promise['target_viewer']} "
            "The brief proposes this novelty, which remains an editorial input rather "
            f"than an empirical conclusion: {viewer_promise['novelty']} "
            "Together, those inputs define a concrete route through the story: establish "
            "the operating context, test the strongest evidence, expose the counterpoint, "
            "and finish with a bounded answer. Verified facts, attributed positions, "
            "estimates, and editorial analysis remain separate so the viewer can see which "
            "part of the answer each one can legitimately carry. Each later section must "
            "answer one part of that route instead of restarting the argument or adding a "
            "disconnected fact."
        )
    if section_id == "evidence_build":
        return (
            "The evidence build now moves from the episode's setup to the proposition that "
            "does the most explanatory work. The point is not to stack citations for visual "
            "weight; it is to compare the assertion with the bounded excerpts actually "
            "recorded for it. An official or primary record speaks only within its stated "
            "scope, while independent reporting contributes a different vantage point. "
            "Reading the evidence this way creates a foundation that the counterpoint can "
            "challenge without quietly changing what any source said. The listener should "
            "leave this section knowing not just what the claim says, but why this specific "
            "record deserves weight."
        )
    if section_id == "tension":
        return (
            "The story changes direction here. A counterpoint is useful only when it puts "
            "pressure on the emerging answer, and an uncertainty matters only when its "
            "assumptions are visible. Attributed language therefore stays with its speaker, "
            "while an estimate stays conditional rather than becoming a forecast. The pair "
            "of claims in this section asks what the evidence leaves unresolved and whether "
            "the opening stakes were drawn too broadly. A useful conclusion should become "
            "narrower and clearer under that pressure, not merely more confident. If either "
            "claim changes the answer, the episode must show how; if not, it should explain "
            "why."
        )
    if section_id == "synthesis":
        return (
            "Synthesis brings the strongest evidence back into view beside an explicitly "
            "editorial reading. The factual layer supplies the floor of the argument; the "
            "analysis explains why that floor matters to the episode's question. Neither "
            "layer silently borrows certainty from the other. The task is to state the "
            "smallest conclusion that connects them, identify the part still unsettled by "
            "the tension section, and keep a visible path back to the material claim. That "
            "turns separate records into an intelligible answer without pretending the "
            "evidence resolved more than it did. This is where evidence becomes a decision "
            "framework rather than a list of statements that happen to share a topic."
        )
    if section_id == "consequences":
        return (
            "The proposed broad-interest bridge is: "
            f"{viewer_promise['broad_interest_bridge']} "
            "This is an editorial bridge to examine, not a substitute for evidence. For "
            f"{viewer_promise['target_viewer']}, the useful consequence is the choice or "
            "tradeoff made visible by the bounded record, not an invented outcome for a "
            "person or company. The claims here connect the opening stakes with the "
            "editorial outlook while preserving the difference between sourced support and "
            "interpretation. The payoff is relevance grounded in what the episode has "
            "actually established. It should leave the audience able to name the practical "
            "tradeoff without mistaking the editorial bridge for another verified claim."
        )
    if section_id == "resolution":
        return (
            "The requested takeaway, treated as a conclusion to earn rather than assume, is: "
            f"{viewer_promise['expected_takeaway']} "
            "Resolution compares that requested takeaway with the context established at "
            "the start, the strongest source-bound evidence, the counterpoint, and the "
            "remaining uncertainty. The Viewer Promise is resolved only to the extent those "
            "layers agree. Where the record stops, the conclusion stops. That still permits "
            "a useful answer, but it refuses a stronger claim simply because stronger "
            "wording would sound more satisfying. The payoff is a bounded thesis the viewer "
            "can trace back through the episode. A successful resolution also makes clear "
            "which part of the original promise remains conditional or unanswered."
        )
    return (
        "What happens next depends on new public evidence, not a confident prediction. The "
        "useful signals are changes that can be observed, attributed, and compared with the "
        "claim identities already recorded. A future benchmark, official update, or "
        "independent report could strengthen, narrow, or overturn part of the current "
        "synthesis. Until then, the remaining uncertainty stays explicit. The viewer leaves "
        "with a practical method: watch for evidence that changes an assumption, resolves "
        "the counterpoint, or adds genuinely independent support, then reevaluate the "
        "conclusion without rewriting what this episode established. That watchlist turns "
        "the ending into a usable next step instead of a prediction disguised as closure."
    )


def _claims_by_role(
    claims: Sequence[Mapping[str, object]],
) -> dict[str, list[Mapping[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for claim in claims:
        grouped[str(claim["role"])].append(claim)
    for values in grouped.values():
        values.sort(key=lambda claim: str(claim["claim_hash"]))
    return grouped


def _section_claim_plan(
    all_claims: Sequence[Mapping[str, object]],
) -> dict[str, list[tuple[Mapping[str, object], int]]]:
    grouped = _claims_by_role(all_claims)
    ordered_claims = sorted(all_claims, key=lambda claim: str(claim["claim_hash"]))
    usage: dict[str, int] = defaultdict(int)
    plan: dict[str, list[tuple[Mapping[str, object], int]]] = {}

    for section_id in REQUIRED_SECTION_IDS:
        limit = _SECTION_CLAIM_LIMITS[section_id]
        selected: list[Mapping[str, object]] = []
        for role in _SECTION_ROLES[section_id]:
            candidates = sorted(
                grouped.get(role, ()),
                key=lambda claim: (
                    usage[str(claim["claim_hash"])],
                    str(claim["claim_hash"]),
                ),
            )
            candidate = next(
                (
                    claim
                    for claim in candidates
                    if claim not in selected
                    and usage[str(claim["claim_hash"])]
                    < _MAX_REFERENCES_PER_CLAIM
                ),
                None,
            )
            if candidate is not None:
                selected.append(candidate)
            if len(selected) == limit:
                break

        if len(selected) < limit:
            fallback = sorted(
                ordered_claims,
                key=lambda claim: (
                    usage[str(claim["claim_hash"])],
                    str(claim["claim_hash"]),
                ),
            )
            for candidate in fallback:
                claim_hash = str(candidate["claim_hash"])
                if (
                    candidate not in selected
                    and usage[claim_hash] < _MAX_REFERENCES_PER_CLAIM
                ):
                    selected.append(candidate)
                if len(selected) == limit:
                    break

        planned: list[tuple[Mapping[str, object], int]] = []
        for claim in selected:
            claim_hash = str(claim["claim_hash"])
            usage[claim_hash] += 1
            planned.append((claim, usage[claim_hash]))
        plan[section_id] = planned
    return plan


def _selected_topic(topic_packet: Mapping[str, object]) -> tuple[str, str]:
    selected_key = str(topic_packet["selected_candidate_key"])
    for raw in topic_packet.get("ranked_candidates", []):  # type: ignore[assignment]
        candidate = dict(raw)
        if candidate.get("candidate_key") == selected_key:
            return str(candidate.get("topic") or selected_key), str(
                candidate.get("angle") or ""
            )
    return selected_key, ""


def compile_script_packet(
    *,
    campaign_id: int,
    topic_packet: dict[str, object],
    topic_packet_hash: str,
    research_packet: dict[str, object],
    research_packet_hash: str,
) -> dict[str, object]:
    viewer_promise = dict(topic_packet["viewer_promise"])  # type: ignore[arg-type]
    usable_claims = [dict(claim) for claim in research_packet.get("claims", [])]  # type: ignore[assignment]
    usable_claims.sort(key=lambda claim: str(claim["claim_hash"]))
    topic, angle = _selected_topic(topic_packet)
    source_index = {
        str(source["source_key"]): {
            "evidence_snippet": source.get("evidence_snippet", ""),
            "publisher": source["publisher"],
            "source_class": source["source_class"],
            "source_uri": source["source_uri"],
        }
        for source in research_packet.get("sources", [])  # type: ignore[assignment]
    }

    sections: list[dict[str, object]] = []
    claim_plan = _section_claim_plan(usable_claims)
    for section_id in REQUIRED_SECTION_IDS:
        planned_claims = claim_plan[section_id]
        rendered_claims = " ".join(
            _render_claim(
                claim,
                source_index=source_index,
                include_evidence_detail=occurrence == 1,
            )
            for claim, occurrence in planned_claims
        )
        framing = _section_framing(
            section_id,
            viewer_promise=viewer_promise,
            topic=topic,
            angle=angle,
        )

        sections.append(
            {
                "claim_hashes": [
                    str(claim["claim_hash"]) for claim, _ in planned_claims
                ],
                "continuation_reason": _CONTINUATIONS[section_id],
                "narration": (framing + rendered_claims).strip(),
                "payoff": _PAYOFFS[section_id],
                "purpose": _PURPOSES[section_id],
                "section_id": section_id,
            }
        )

    claim_index = {
        str(claim["claim_hash"]): {
            "assertion_text": claim["assertion_text"],
            "attribution": claim.get("attribution"),
            "claim_type": claim["claim_type"],
            "material": claim["material"],
            "role": claim["role"],
            "source_keys": claim["source_keys"],
            "state": claim["state"],
        }
        for claim in usable_claims
    }
    script_text = " ".join(str(section["narration"]) for section in sections)
    word_count = len(re.findall(r"\b[\w'-]+\b", script_text))
    packet: dict[str, object] = {
        "angle": angle,
        "campaign_id": campaign_id,
        "claim_index": claim_index,
        "contract_version": "i4-script-v1",
        "estimated_runtime_minutes": round(
            word_count / NARRATION_WORDS_PER_MINUTE,
            2,
        ),
        "narration_rate_words_per_minute": NARRATION_WORDS_PER_MINUTE,
        "narrator_identity": NARRATOR_IDENTITY,
        "policy_version": I4_POLICY_VERSION,
        "research_packet_hash": research_packet_hash,
        "sections": sections,
        "selected_candidate_key": topic_packet["selected_candidate_key"],
        "source_index": source_index,
        "target_runtime_minutes": {
            "maximum": I4_TARGET_MAX_RUNTIME_MINUTES,
            "minimum": I4_TARGET_MIN_RUNTIME_MINUTES,
        },
        "thesis": (
            f"The episode tests whether this Viewer Promise is supported: "
            f"{viewer_promise['viewer_promise']}"
        ),
        "title": topic,
        "topic_packet_hash": topic_packet_hash,
        "viewer_promise": viewer_promise,
        "word_count": word_count,
    }
    packet["gate"] = evaluate_script_gate(packet, research_packet=research_packet)
    return packet


def evaluate_script_gate(
    script_packet: Mapping[str, object],
    *,
    research_packet: Mapping[str, object],
) -> dict[str, object]:
    failures: set[str] = set()
    sections = [dict(section) for section in script_packet.get("sections", [])]  # type: ignore[assignment]
    if tuple(str(section.get("section_id")) for section in sections) != REQUIRED_SECTION_IDS:
        failures.add("required_narrative_sections_missing_or_out_of_order")
    for section in sections:
        section_id = str(section.get("section_id") or "unknown")
        for field in ("purpose", "narration", "continuation_reason", "payoff"):
            if not str(section.get(field) or "").strip():
                failures.add(f"section_missing_{field}:{section_id}")

    claim_index = {
        str(key): dict(value)
        for key, value in dict(script_packet.get("claim_index") or {}).items()
    }
    research_claims = {
        str(claim["claim_hash"]): dict(claim)
        for claim in research_packet.get("claims", [])  # type: ignore[assignment]
    }
    rejected_claims = {
        str(claim["claim_hash"]): dict(claim)
        for claim in research_packet.get("rejected_claims", [])  # type: ignore[assignment]
    }
    source_index = dict(script_packet.get("source_index") or {})

    expected_claim_index = {
        claim_hash: {
            "assertion_text": claim["assertion_text"],
            "attribution": claim.get("attribution"),
            "claim_type": claim["claim_type"],
            "material": claim["material"],
            "role": claim["role"],
            "source_keys": claim["source_keys"],
            "state": claim["state"],
        }
        for claim_hash, claim in research_claims.items()
    }
    if claim_index != expected_claim_index:
        failures.add("claim_index_does_not_match_research")

    expected_source_index = {
        str(source["source_key"]): {
            "evidence_snippet": source.get("evidence_snippet", ""),
            "publisher": source["publisher"],
            "source_class": source["source_class"],
            "source_uri": source["source_uri"],
        }
        for source in research_packet.get("sources", [])  # type: ignore[assignment]
    }
    if source_index != expected_source_index:
        failures.add("source_index_does_not_match_research")

    ordered_research_claims = sorted(
        research_claims.values(),
        key=lambda claim: str(claim["claim_hash"]),
    )
    expected_plan = _section_claim_plan(ordered_research_claims)
    expected_sections: dict[str, dict[str, object]] = {}
    viewer_promise = dict(script_packet.get("viewer_promise") or {})
    topic = str(script_packet.get("title") or "")
    angle = str(script_packet.get("angle") or "")
    for section_id in REQUIRED_SECTION_IDS:
        planned_claims = expected_plan[section_id]
        rendered_claims = " ".join(
            _render_claim(
                claim,
                source_index=expected_source_index,
                include_evidence_detail=occurrence == 1,
            )
            for claim, occurrence in planned_claims
        )
        expected_sections[section_id] = {
            "claim_hashes": [
                str(claim["claim_hash"]) for claim, _ in planned_claims
            ],
            "continuation_reason": _CONTINUATIONS[section_id],
            "narration": (
                _section_framing(
                    section_id,
                    viewer_promise=viewer_promise,
                    topic=topic,
                    angle=angle,
                )
                + rendered_claims
            ).strip(),
            "payoff": _PAYOFFS[section_id],
            "purpose": _PURPOSES[section_id],
            "section_id": section_id,
        }

    for section in sections:
        section_id = str(section.get("section_id") or "unknown")
        expected_section = expected_sections.get(section_id)
        if expected_section is None or section != expected_section:
            failures.add(
                f"unsupported_or_noncanonical_narration:{section_id}"
            )

    for section in sections:
        narration = str(section.get("narration") or "")
        hashes = [str(value) for value in section.get("claim_hashes", [])]
        if not hashes:
            failures.add(f"section_without_claim_lineage:{section.get('section_id')}")
        for hash_value in hashes:
            if hash_value in rejected_claims:
                failures.add("rejected_claim_referenced")
                continue
            if hash_value not in research_claims or hash_value not in claim_index:
                failures.add("referenced_claim_hash_missing_from_research")
                continue
            claim = research_claims[hash_value]
            claim_type = str(claim["claim_type"])
            state = str(claim["state"])
            if claim_type in {"fact", "attributed_claim"} and state != "VERIFIED":
                failures.add("factual_claim_is_not_verified")
            if claim_type == "attributed_claim":
                attribution = str(claim.get("attribution") or "")
                if not attribution or attribution.casefold() not in narration.casefold():
                    failures.add("attributed_claim_lost_attribution")
            if claim_type == "estimate" and not any(
                marker in narration.casefold()
                for marker in ("estimate", "suggests", "approximately", "could")
            ):
                failures.add("estimate_lost_uncertainty_framing")
            if claim_type == "opinion" and not any(
                marker in narration.casefold()
                for marker in ("our read is", "editorial analysis", "editorial interpretation")
            ):
                failures.add("opinion_lost_editorial_framing")
            if claim_type != "opinion" and bool(claim.get("material")):
                source_keys = [str(key) for key in claim.get("source_keys", [])]
                if not source_keys or any(key not in source_index for key in source_keys):
                    failures.add("sourceability_lineage_broken")
        for research_hash, claim in research_claims.items():
            assertion = str(claim.get("assertion_text") or "")
            if (
                assertion
                and bool(claim.get("material"))
                and assertion.casefold() in narration.casefold()
                and research_hash not in hashes
            ):
                failures.add("factual_material_narration_lacks_claim_hash")

        if contains_fabricated_identity(narration):
            failures.add("fabricated_first_person_identity")
        if any(finding.severity == "high" for finding in scan_text(narration)):
            failures.add("blocked_compliance_language_in_script")

    all_narration = " ".join(str(section.get("narration") or "") for section in sections)
    for rejected in rejected_claims.values():
        assertion = str(rejected.get("assertion_text") or "")
        if assertion and assertion.casefold() in all_narration.casefold():
            failures.add("rejected_claim_appears_in_narration")

    if sections:
        cold_open = sections[0]
        cold_narration = str(cold_open.get("narration") or "")
        if str(viewer_promise.get("core_question") or "").casefold() not in cold_narration.casefold():
            failures.add("cold_open_missing_core_question")
        if str(viewer_promise.get("stakes") or "").casefold() not in cold_narration.casefold():
            failures.add("cold_open_missing_stakes")
        if str(viewer_promise.get("why_now") or "").casefold() not in cold_narration.casefold():
            failures.add("cold_open_missing_why_now")
        if not str(cold_open.get("continuation_reason") or "").strip():
            failures.add("cold_open_missing_continuation_reason")

        required_editorial_framing = {
            "viewer_promise_missing_editorial_framing": (
                "The editorial promise under review is: "
                f"{viewer_promise.get('viewer_promise') or ''}"
            ),
            "stakes_missing_editorial_premise_framing": (
                "The editorial brief frames the stakes as a premise to test: "
                f"{viewer_promise.get('stakes') or ''}"
            ),
            "why_now_missing_editorial_premise_framing": (
                "Its why-now premise, also subject to verification, is: "
                f"{viewer_promise.get('why_now') or ''}"
            ),
            "novelty_missing_editorial_premise_framing": (
                "The brief proposes this novelty, which remains an editorial input rather "
                "than an empirical conclusion: "
                f"{viewer_promise.get('novelty') or ''}"
            ),
            "broad_interest_missing_editorial_premise_framing": (
                "The proposed broad-interest bridge is: "
                f"{viewer_promise.get('broad_interest_bridge') or ''}"
            ),
            "takeaway_missing_editorial_premise_framing": (
                "The requested takeaway, treated as a conclusion to earn rather than assume, is: "
                f"{viewer_promise.get('expected_takeaway') or ''}"
            ),
        }
        for reason, required_text in required_editorial_framing.items():
            if required_text.casefold() not in all_narration.casefold():
                failures.add(reason)

    meaningful_payoffs = sum(
        1 for section in sections if str(section.get("payoff") or "").strip()
    )
    if meaningful_payoffs < 4:
        failures.add("too_few_meaningful_payoffs")
    if script_packet.get("narrator_identity") != NARRATOR_IDENTITY:
        failures.add("narrator_identity_mismatch")
    computed_word_count = len(re.findall(r"\b[\w'-]+\b", all_narration))
    if script_packet.get("word_count") != computed_word_count:
        failures.add("script_word_count_mismatch")
    if script_packet.get("estimated_runtime_minutes") != round(
        computed_word_count / NARRATION_WORDS_PER_MINUTE,
        2,
    ):
        failures.add("script_runtime_estimate_mismatch")
    if not I4_TARGET_MIN_WORDS <= computed_word_count <= I4_TARGET_MAX_WORDS:
        failures.add("script_outside_eight_to_twelve_minute_long_form_target")
    if script_packet.get("target_runtime_minutes") != {
        "maximum": I4_TARGET_MAX_RUNTIME_MINUTES,
        "minimum": I4_TARGET_MIN_RUNTIME_MINUTES,
    }:
        failures.add("script_runtime_target_mismatch")

    return {
        "outcome": "FAIL" if failures else "PASS",
        "reasons": (
            sorted(failures)
            if failures
            else ["script_is_structured_evidence_bound_and_editorially_safe"]
        ),
    }
