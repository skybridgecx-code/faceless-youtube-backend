from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ComplianceFinding:
    severity: str
    message: str


BLOCKED_PATTERNS = [
    (r"\bguaranteed\b", "Avoid guarantee language."),
    (r"\bmake \$?\d+[kKmM]?\b", "Avoid unsupported income promises."),
    (r"\bwill make you rich\b", "Avoid get-rich claims."),
    (r"\bactual client results\b", "Do not claim real client results without proof."),
    (r"\bthis company uses\b", "Do not claim real company adoption unless verified."),
]


def scan_text(text: str) -> list[ComplianceFinding]:
    findings: list[ComplianceFinding] = []
    lower = text.lower()

    for pattern, message in BLOCKED_PATTERNS:
        if re.search(pattern, lower, flags=re.IGNORECASE):
            findings.append(ComplianceFinding(severity="high", message=message))

    if "ai voice" in lower or "synthetic" in lower:
        if "disclosure" not in lower and "ai-generated" not in lower:
            findings.append(
                ComplianceFinding(
                    severity="medium",
                    message="Mention when realistic AI voice or synthetic media is used.",
                )
            )

    return findings


def build_review_checklist(text: str) -> str:
    findings = scan_text(text)
    lines = [
        "# Review Checklist",
        "",
        "- [ ] No fake income claims",
        "- [ ] No fake client results",
        "- [ ] No unsupported claims about real companies",
        "- [ ] Visuals are original, licensed, or created by the operator",
        "- [ ] AI/synthetic content is disclosed when required",
        "- [ ] Video adds original explanation, demo, or commentary",
        "- [ ] Description says examples are educational/demo unless verified",
        "",
        "## Automated findings",
    ]
    if not findings:
        lines.append("No high-risk phrases detected by local scanner.")
    for finding in findings:
        lines.append(f"- {finding.severity.upper()}: {finding.message}")
    return "\n".join(lines)
