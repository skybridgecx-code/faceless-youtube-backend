from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import AssetType, Video, VideoStatus
from app.schemas import ComplianceCheck, ComplianceReport


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


RISKY_TERMS = [
    (r"\bguarante+d?\b", "Avoid guarantee language."),
    (r"\bwe made\b", "Avoid unsupported revenue claims."),
    (r"\bearned\b", "Avoid unsupported income claims."),
    (r"\$", "Review financial figures for accuracy."),
    (r"\brevenue\b", "Review revenue claims."),
    (r"\bprofit\b", "Review profit claims."),
    (r"\breal client\b", "Do not claim real client results without proof."),
    (r"\bcase study\b", "Ensure case studies are verified."),
    (r"\bresults\b", "Review claims about specific results."),
    (r"\b10x\b", "Avoid exaggerated multipliers like 10x."),
    (r"\bdouble your\b", "Avoid unrealistic multiplier claims."),
    (r"\btriple your\b", "Avoid unrealistic multiplier claims."),
]

def run_compliance_checks(video: Video) -> ComplianceReport:
    checks = []
    overall_status = "pass"
    
    # 1. Missing assets check
    if not video.assets:
        checks.append(ComplianceCheck(
            id="missing_assets",
            label="Assets Generated",
            status="blocked",
            detail="No assets have been generated for this video yet.",
            suggested_fix="Generate assets before running compliance checks."
        ))
        return ComplianceReport(
            video_id=video.id,
            title=video.title,
            overall_status="blocked",
            checks=checks
        )
    
    existing_types = {asset.asset_type for asset in video.assets}
    
    # 2. Check for required assets
    required_assets = [
        (AssetType.youtube_metadata, "YouTube Metadata"),
        (AssetType.description, "Description"),
        (AssetType.thumbnail_prompt, "Thumbnail Prompt")
    ]
    
    for asset_type, label in required_assets:
        if asset_type not in existing_types:
            checks.append(ComplianceCheck(
                id=f"missing_{asset_type.name}",
                label=f"Missing {label}",
                status="blocked",
                detail=f"The {label} asset is missing.",
                asset_type=asset_type.value,
                suggested_fix="Regenerate this specific asset or run full generation."
            ))
            overall_status = "blocked"
            
    # 3. Check for Package Manifest if status is packaged or ready
    if video.status in (VideoStatus.packaged, VideoStatus.publish_ready, VideoStatus.published) or video.publish_status in ("ready", "scheduled"):
        if AssetType.package_manifest not in existing_types:
            checks.append(ComplianceCheck(
                id="missing_manifest",
                label="Missing Package Manifest",
                status="blocked",
                detail="The video is marked as packaged/ready but has no manifest asset.",
                suggested_fix="Package the video again."
            ))
            overall_status = "blocked"

    # 4. Check for Risky Terms in text assets
    for asset in video.assets:
        text = asset.body.lower()
        if asset.asset_type in (AssetType.brief, AssetType.script, AssetType.shorts, AssetType.description, AssetType.youtube_metadata):
            # Also check for AI/synthetic disclosure
            if "ai voice" in text or "synthetic" in text:
                if "disclosure" not in text and "ai-generated" not in text:
                    checks.append(ComplianceCheck(
                        id=f"missing_disclosure_{asset.asset_type.name}",
                        label=f"Missing AI Disclosure in {asset.asset_type.name.capitalize()}",
                        status="warning",
                        detail="Found mention of AI/synthetic media but no clear disclosure.",
                        asset_type=asset.asset_type.value,
                        suggested_fix="Add an explicit disclosure about AI generation."
                    ))
                    if overall_status == "pass":
                        overall_status = "warning"

            for pattern, msg in RISKY_TERMS:
                if re.search(pattern, text, flags=re.IGNORECASE):
                    # Make ID safe
                    safe_pattern = re.sub(r'[^a-zA-Z0-9]', '', pattern)
                    checks.append(ComplianceCheck(
                        id=f"risky_terms_{asset.asset_type.name}_{safe_pattern}",
                        label=f"Risky Claim in {asset.asset_type.name.capitalize()}",
                        status="warning",
                        detail=msg,
                        asset_type=asset.asset_type.value,
                        suggested_fix="Review and soften language. Add educational framing or remove specific guarantees."
                    ))
                    if overall_status == "pass":
                        overall_status = "warning"

            for pattern, msg in BLOCKED_PATTERNS:
                if re.search(pattern, text, flags=re.IGNORECASE):
                    safe_pattern = re.sub(r'[^a-zA-Z0-9]', '', pattern)
                    checks.append(ComplianceCheck(
                        id=f"blocked_terms_{asset.asset_type.name}_{safe_pattern}",
                        label=f"Blocked Claim in {asset.asset_type.name.capitalize()}",
                        status="blocked",
                        detail=msg,
                        asset_type=asset.asset_type.value,
                        suggested_fix="Remove this claim entirely. It violates core safety policies."
                    ))
                    overall_status = "blocked"

    if not checks:
        checks.append(ComplianceCheck(
            id="all_clear",
            label="All Clear",
            status="pass",
            detail="No compliance issues found."
        ))

    return ComplianceReport(
        video_id=video.id,
        title=video.title,
        overall_status=overall_status,
        checks=checks
    )


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
