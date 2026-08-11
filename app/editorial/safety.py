from __future__ import annotations

import re

from app.services.compliance import scan_text

from .contracts import SensitivityTag, TopicCandidate


EXCLUDED_SENSITIVITY_TAGS: frozenset[SensitivityTag] = frozenset(
    {
        "health",
        "finance",
        "politics_elections",
        "war_conflict",
        "legal_advice",
    }
)

_SENSITIVE_TEXT_PATTERNS: tuple[tuple[SensitivityTag, re.Pattern[str]], ...] = (
    (
        "health",
        re.compile(
            r"\b(?:medical advice|diagnos(?:is|e)|treat(?:ment)?|prescription|cancer|disease|mental health)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "finance",
        re.compile(
            r"\b(?:financial advice|invest(?:ment|ing)?|stock market|crypto(?:currency)?|trading strategy|retirement fund)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "politics_elections",
        re.compile(
            r"\b(?:election|vot(?:e|ing)|political campaign|presidential candidate|ballot)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "war_conflict",
        re.compile(
            r"\b(?:warfare|armed conflict|military invasion|battlefield|airstrike|combat operation)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "legal_advice",
        re.compile(
            r"\b(?:legal advice|lawsuit strategy|sue your|attorney advice|criminal defense)\b",
            re.IGNORECASE,
        ),
    ),
)

_FABRICATED_IDENTITY_PATTERN = re.compile(
    r"\b(?:i|we)(?:\s+have|'ve)?(?:\s+personally)?\s+"
    r"(?:tested|built|interviewed|investigated|used|owned|witnessed|observed|found|"
    r"measured|discovered|confirmed|verified|recorded|experienced|saw|heard|visited|"
    r"deployed|implemented|created|conducted)\b",
    re.IGNORECASE,
)


def candidate_text(candidate: TopicCandidate) -> str:
    fields = (
        candidate.topic,
        candidate.angle,
        candidate.target_viewer,
        candidate.viewer_promise,
        candidate.core_question,
        candidate.why_now,
        candidate.stakes,
        candidate.novelty,
        candidate.broad_interest_bridge,
        candidate.expected_takeaway,
        *(claim.assertion_text for claim in candidate.claims),
    )
    return "\n".join(fields)


def sensitivity_failures(candidate: TopicCandidate) -> tuple[str, ...]:
    failures = {
        f"excluded_sensitivity_tag:{tag}"
        for tag in candidate.sensitivity_tags
        if tag in EXCLUDED_SENSITIVITY_TAGS
    }
    text = candidate_text(candidate)
    for tag, pattern in _SENSITIVE_TEXT_PATTERNS:
        if pattern.search(text):
            failures.add(f"excluded_sensitive_text:{tag}")
    return tuple(sorted(failures))


def compliance_failures(candidate: TopicCandidate) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                f"blocked_compliance_phrase:{finding.message}"
                for finding in scan_text(candidate_text(candidate))
                if finding.severity == "high"
            }
        )
    )


def contains_fabricated_identity(text: str) -> bool:
    return _FABRICATED_IDENTITY_PATTERN.search(text) is not None
