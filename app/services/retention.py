"""Deterministic retention analysis for video scripts.

Scores a script against 2026 YouTube retention best practices (strong early
hook, an early pattern interrupt, open loops, a loop/CTA ending, segment
cadence, concrete specificity). This is an *analyzer and adviser* — it never
rewrites or replaces the operator's script and bypasses no review gate. It also
exposes prompt guidance that can be fed to the LLM script generator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetentionCheck:
    id: str
    label: str
    passed: bool
    weight: int
    tip: str


@dataclass(frozen=True)
class RetentionReport:
    content_type: str
    score: int  # 0-100
    grade: str
    summary: str
    checks: list[RetentionCheck] = field(default_factory=list)


_HOOK_SIGNALS = ("?", "you", "your", "imagine", "stop", "most ", "nobody", "here's", "what if")
_INTERRUPT_SIGNALS = ("but ", "however", "actually", "the problem", "here's why", "the truth", "wait", "except")
_OPEN_LOOP_SIGNALS = (
    "by the end", "stick around", "stay until", "later in", "in a second", "in a moment",
    "wait until", "here's the part", "coming up", "i'll show you", "we'll get to",
)
_ENDING_SIGNALS = ("subscribe", "follow", "next video", "next short", "watch", "link in", "comment", "loop")
_SPECIFICITY_SIGNALS = ("dashboard", "workflow", "step", "system", "checklist", "%", "$")


def _first_chunk(text: str, chars: int) -> str:
    return text[:chars].lower()


def _last_chunk(text: str, chars: int) -> str:
    return text[-chars:].lower()


def _has_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n in haystack for n in needles)


def _grade(score: int) -> str:
    if score >= 85:
        return "strong"
    if score >= 70:
        return "solid"
    if score >= 50:
        return "needs work"
    return "weak"


def analyze_script(text: str, *, content_type: str = "long") -> RetentionReport:
    raw = (text or "").strip()
    lowered = raw.lower()
    is_short = content_type.strip().lower() == "short"
    head_window = 240 if not is_short else 160
    head = _first_chunk(raw, head_window)
    tail = _last_chunk(raw, 320)

    # Count timestamped segments like "## 0:00" / "0:03-0:25".
    segment_count = len(re.findall(r"\b\d{1,2}:\d{2}\b", raw))
    digit_specific = bool(re.search(r"\d", raw))

    checks: list[RetentionCheck] = []

    checks.append(
        RetentionCheck(
            id="hook_present",
            label="Hook in the first few seconds",
            passed=_has_any(head, _HOOK_SIGNALS),
            weight=25,
            tip="Open with a question or a direct 'you' statement in the first 7 seconds.",
        )
    )
    checks.append(
        RetentionCheck(
            id="early_pattern_interrupt",
            label="Early pattern interrupt / tension",
            passed=_has_any(_first_chunk(raw, head_window * 3), _INTERRUPT_SIGNALS),
            weight=20,
            tip="Add a contrast ('but', 'the problem', 'here's why') in the first ~5 seconds to interrupt the scroll.",
        )
    )
    checks.append(
        RetentionCheck(
            id="open_loop",
            label="Open loop to pull viewers forward",
            passed=_has_any(lowered, _OPEN_LOOP_SIGNALS) or "?" in head,
            weight=15,
            tip="Promise a payoff ('by the end…', 'stick around for…') so viewers keep watching.",
        )
    )
    checks.append(
        RetentionCheck(
            id="loop_or_cta_ending",
            label="Loop / CTA ending",
            passed=_has_any(tail, _ENDING_SIGNALS),
            weight=15,
            tip="Close with a CTA or a loop back to the opening to extend session watch time.",
        )
    )
    checks.append(
        RetentionCheck(
            id="segment_cadence",
            label="Clear segment cadence",
            passed=(segment_count >= 4) if not is_short else (segment_count >= 3),
            weight=15,
            tip="Break the script into timestamped beats to keep momentum and re-hook attention.",
        )
    )
    checks.append(
        RetentionCheck(
            id="concrete_specificity",
            label="Concrete specifics (numbers, named systems)",
            passed=digit_specific or _has_any(lowered, _SPECIFICITY_SIGNALS),
            weight=10,
            tip="Use specific numbers and named systems instead of vague claims to build trust and retention.",
        )
    )

    earned = sum(c.weight for c in checks if c.passed)
    total = sum(c.weight for c in checks) or 1
    score = round(100 * earned / total)
    failing = [c.label for c in checks if not c.passed]
    if failing:
        summary = f"{score}/100 ({_grade(score)}). Improve: " + "; ".join(failing) + "."
    else:
        summary = f"{score}/100 ({_grade(score)}). All retention checks passed."

    return RetentionReport(
        content_type="short" if is_short else "long",
        score=score,
        grade=_grade(score),
        summary=summary,
        checks=checks,
    )


def retention_prompt_guidance(content_type: str = "long") -> str:
    """Guidance appended to the LLM script prompt for 2026 retention structure."""
    is_short = content_type.strip().lower() == "short"
    if is_short:
        return (
            "Retention structure (short-form): hook in the first 2-3 seconds with a question or "
            "direct 'you' statement; one pattern interrupt immediately after; a single clear point; "
            "end with a loop back to the opening. Keep claims conservative and disclose AI assistance."
        )
    return (
        "Retention structure (long-form): open with a hook in the first 7 seconds (question or direct "
        "'you' statement); add a pattern interrupt / tension within the first 5 seconds; plant an open "
        "loop ('by the end…') early; keep clear timestamped segment cadence; use concrete numbers and "
        "named systems; close with a CTA or loop. Keep claims conservative and disclose AI assistance."
    )
