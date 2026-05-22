from __future__ import annotations

import re
from dataclasses import dataclass

_BLOCKER_PHRASES = (
    "[insert link]",
    "how to i built",
    "draft preview",
)
_BLOCKER_HINT_TOKENS = ("todo", "tbd", "lorem ipsum")
_MIN_SOURCE_CHARS = 80
_ALLOWED_BRACKET_TAGS = {"[music]", "[pause]", "[beat]"}
_BRACKET_PATTERN = re.compile(r"\[[^\]]{1,80}\]")


@dataclass(frozen=True)
class FinalVoiceoverSourceQualityAssessment:
    ready: bool
    blockers: list[str]
    warnings: list[str]


def assess_final_voiceover_source_quality(source_text: str | None) -> FinalVoiceoverSourceQualityAssessment:
    text = (source_text or "").strip()
    lower = text.lower()
    blockers: list[str] = []
    warnings: list[str] = []

    if not text:
        blockers.append("No script text available for voiceover generation")
        return FinalVoiceoverSourceQualityAssessment(ready=False, blockers=blockers, warnings=warnings)

    if len(text) < _MIN_SOURCE_CHARS:
        blockers.append(f"Final voiceover source text is too short (minimum {_MIN_SOURCE_CHARS} characters recommended)")

    for phrase in _BLOCKER_PHRASES:
        if phrase in lower:
            blockers.append(f"Final voiceover source contains placeholder/editorial phrase: {phrase}")

    for token in _BLOCKER_HINT_TOKENS:
        if token in lower:
            blockers.append(f"Final voiceover source contains unresolved editorial token: {token}")

    for match in _BRACKET_PATTERN.findall(text):
        normalized = match.strip().lower()
        if normalized in _ALLOWED_BRACKET_TAGS:
            continue
        if "..." in normalized:
            blockers.append(f"Final voiceover source contains unresolved bracket placeholder: {match}")
            continue
        hint_keywords = ("insert", "link", "todo", "tbd", "placeholder", "draft")
        if any(keyword in normalized for keyword in hint_keywords):
            blockers.append(f"Final voiceover source contains unresolved bracket placeholder: {match}")
            continue
        warnings.append(f"Review bracketed narration token before generation: {match}")

    # De-dupe while preserving order
    seen_blockers: set[str] = set()
    deduped_blockers: list[str] = []
    for blocker in blockers:
        if blocker not in seen_blockers:
            seen_blockers.add(blocker)
            deduped_blockers.append(blocker)

    seen_warnings: set[str] = set()
    deduped_warnings: list[str] = []
    for warning in warnings:
        if warning not in seen_warnings:
            seen_warnings.add(warning)
            deduped_warnings.append(warning)

    return FinalVoiceoverSourceQualityAssessment(
        ready=len(deduped_blockers) == 0,
        blockers=deduped_blockers,
        warnings=deduped_warnings,
    )
