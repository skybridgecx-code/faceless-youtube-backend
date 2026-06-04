from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from app.config import get_settings
from app.models import ContentType, Video
from app.services.compliance import build_review_checklist, scan_text
from app.services.monetization import apply_monetization, parse_affiliate_offers
from app.services.retention import retention_prompt_guidance


@dataclass(frozen=True)
class GeneratedAsset:
    asset_type: str
    body: str


DEFAULT_TAGS = [
    "AI automation",
    "local business",
    "AI receptionist",
    "contractor marketing",
    "small business automation",
    "CRM workflow",
]


FIRST_IDEAS = [
    (
        "I Built an AI Receptionist for a Roofing Company",
        "AI call handling",
        "Roofer or contractor",
        "Missed calls after hours and during job-site work",
        "AI receptionist and roofing lead dashboard walkthrough",
        "AI ROOFING RECEPTIONIST",
    ),
    (
        "This Is Why Contractors Miss So Many Leads",
        "Local business breakdown",
        "Home service business owner",
        "Leads are lost because calls, texts, and voicemails are scattered",
        "Lead leak map and missed-call workflow",
        "MISSED CALLS COST MONEY",
    ),
    (
        "I Built a Dashboard That Tracks Every Missed Call",
        "Business dashboard demos",
        "Contractor owner/operator",
        "Owners do not know which calls need action",
        "Missed-call dashboard with urgent lead tags",
        "NO MORE VOICEMAIL",
    ),
    (
        "AI Phone Agent vs Human Receptionist",
        "Tool stack and tutorials",
        "Small business owner comparing operations options",
        "Manual reception is expensive, but uncontrolled AI is risky",
        "Comparison board with guardrails and escalation rules",
        "AI VS HUMAN",
    ),
    (
        "The Local Business AI Stack I’d Use in 2026",
        "Tool stack and tutorials",
        "Local operator building systems",
        "Too many tools, no simple stack",
        "Practical stack map: phone, CRM, follow-up, dashboard",
        "THE AI STACK",
    ),
    (
        "How HVAC Companies Can Automate Follow-Up",
        "AI call handling",
        "HVAC owner or dispatcher",
        "Customers need reminders, estimates, and maintenance follow-up",
        "HVAC follow-up pipeline with call summaries and appointment reminders",
        "HVAC FOLLOW-UP AI",
    ),
    (
        "I Built an AI Proposal Generator for a Roofer",
        "Business dashboard demos",
        "Roofing sales owner",
        "Proposal creation is slow and inconsistent",
        "Roofing proposal generator from intake details",
        "AI PROPOSAL BUILDER",
    ),
    (
        "How Restaurants Can Use AI to Take Orders",
        "AI call handling",
        "Restaurant owner",
        "Phone orders interrupt staff and get missed during rush",
        "Phone order workflow with menu, modifiers, and pickup summary",
        "AI TOOK THE ORDER",
    ),
    (
        "The CRM Mistake Killing Local Businesses",
        "Local business breakdown",
        "Small business operator",
        "Leads are stored but not worked",
        "CRM board showing stale leads and required next actions",
        "CRM IS NOT ENOUGH",
    ),
    (
        "Can AI Actually Book Jobs for Contractors?",
        "Tool stack and tutorials",
        "Skeptical contractor",
        "Business owners hear AI hype but need practical limits",
        "Booking flow with approved windows and human escalation",
        "CAN AI BOOK JOBS?",
    ),
]

_ASSET_CACHE: dict[tuple[int, str], str] = {}
_REQUIRED_IDEA_KEYS = ("title", "pillar", "target_viewer", "pain_point", "demo_idea", "thumbnail_text")
_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_LLM_SYSTEM_PROMPT = (
    "You are writing educational local-business faceless YouTube content. "
    "Keep claims conservative, avoid guarantees, avoid fake results, and avoid hype."
)


def clear_asset_cache(video_id: int | None = None) -> None:
    if video_id is None:
        _ASSET_CACHE.clear()
        return
    for cache_key in [key for key in _ASSET_CACHE if key[0] == int(video_id)]:
        _ASSET_CACHE.pop(cache_key, None)


def _fallback_idea(index: int) -> dict[str, str]:
    if index < len(FIRST_IDEAS):
        title, pillar, target_viewer, pain_point, demo_idea, thumbnail_text = FIRST_IDEAS[index]
        return {
            "title": title,
            "pillar": pillar,
            "target_viewer": target_viewer,
            "pain_point": pain_point,
            "demo_idea": demo_idea,
            "thumbnail_text": thumbnail_text,
        }
    n = index + 1
    return {
        "title": f"Local Business AI Workflow #{n}",
        "pillar": "Business dashboard demos",
        "target_viewer": "Local service business owner",
        "pain_point": "Missed follow-up and unclear lead status",
        "demo_idea": "AI workflow and dashboard walkthrough",
        "thumbnail_text": "AI WORKFLOW",
    }


def _fallback_ideas(count: int) -> list[dict[str, str]]:
    return [_fallback_idea(index) for index in range(max(0, count))]


def _llm_enabled() -> bool:
    return bool((get_settings().openai_api_key or "").strip())


def _cache_key(video: Video, asset_type: str) -> tuple[int, str] | None:
    if getattr(video, "id", None) is None:
        return None
    return (int(video.id), asset_type)


def _is_compliance_safe(text: str) -> bool:
    findings = scan_text(text)
    return not any(finding.severity == "high" for finding in findings)


def _extract_text_from_response(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload.get("output_text"):
        return str(payload["output_text"]).strip()

    chunks: list[str] = []
    for output_item in payload.get("output", []):
        if not isinstance(output_item, dict):
            continue
        for content_item in output_item.get("content", []):
            if not isinstance(content_item, dict):
                continue
            text_value = content_item.get("text")
            if isinstance(text_value, str) and text_value.strip():
                chunks.append(text_value.strip())
    return "\n".join(chunks).strip()


def _llm_text_request(user_prompt: str) -> str:
    settings = get_settings()
    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    payload = {
        "model": settings.openai_model,
        "input": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    req = urllib.request.Request(
        _OPENAI_RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310 - fixed API endpoint
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("LLM response was not valid JSON.") from exc
    text = _extract_text_from_response(parsed)
    if not text:
        raise RuntimeError("LLM response did not contain text output.")
    return text


def _extract_json_array_text(value: str) -> str:
    start = value.find("[")
    end = value.rfind("]")
    if start == -1 or end == -1 or end < start:
        return value
    return value[start:end + 1]


def _build_idea_prompt(count: int) -> str:
    return (
        "Return JSON only. Generate an array of idea objects for faceless local-business AI videos.\n"
        f"Create exactly {count} ideas.\n"
        "Each item must include string keys: title, pillar, target_viewer, pain_point, demo_idea, thumbnail_text.\n"
        "Keep claims educational and non-guaranteed."
    )


def _build_asset_prompt(video: Video, asset_type: str, fallback_text: str) -> str:
    content_type_value = getattr(getattr(video, "content_type", None), "value", "long")
    short_script_instruction = ""
    if asset_type == "script" and content_type_value == "short":
        short_script_instruction = (
            "\nThis is a SHORT-FORM script: 45-60 seconds, strong first 3-second hook, "
            "single clear point, vertical-video direction, and CTA or loop ending."
        )
    if asset_type == "script":
        short_script_instruction += "\n" + retention_prompt_guidance(content_type_value)
    return (
        f"Generate only the {asset_type} asset for this video.\n"
        f"Content type: {content_type_value}\n"
        f"Title: {video.title}\n"
        f"Pillar: {video.pillar}\n"
        f"Target viewer: {video.target_viewer}\n"
        f"Pain point: {video.pain_point}\n"
        f"Demo idea: {video.demo_idea}\n"
        f"Thumbnail text: {video.thumbnail_text}\n"
        "Keep output practical, educational, and safe.\n"
        "Do not include HTML.\n"
        f"{short_script_instruction}\n"
        "Use this deterministic template as structure/style reference, but produce a fresh variant:\n"
        f"{fallback_text}"
    )


def _llm_candidate_for_asset(video: Video, asset_type: str, fallback_text: str) -> str | None:
    if not _llm_enabled():
        return None
    cache_key = _cache_key(video, asset_type)
    if cache_key and cache_key in _ASSET_CACHE:
        return _ASSET_CACHE[cache_key]
    prompt = _build_asset_prompt(video, asset_type, fallback_text)
    candidate = _llm_text_request(prompt).strip()
    if not candidate:
        return None
    if not _is_compliance_safe(candidate):
        return None
    if cache_key:
        _ASSET_CACHE[cache_key] = candidate
    return candidate


def _render_asset_with_fallback(video: Video, asset_type: str, fallback_builder: Callable[[Video], str]) -> str:
    fallback_text = fallback_builder(video)
    if not _llm_enabled():
        return fallback_text
    try:
        llm_text = _llm_candidate_for_asset(video, asset_type, fallback_text)
    except Exception:  # noqa: BLE001
        return fallback_text
    if not llm_text:
        return fallback_text
    return llm_text


def generate_video_ideas(count: int) -> list[dict[str, str]]:
    fallback_ideas = _fallback_ideas(count)
    if count <= 0:
        return []
    if not _llm_enabled():
        return fallback_ideas

    try:
        raw = _llm_text_request(_build_idea_prompt(count))
        parsed = json.loads(_extract_json_array_text(raw))
    except Exception:  # noqa: BLE001
        return fallback_ideas

    if not isinstance(parsed, list):
        return fallback_ideas

    output: list[dict[str, str]] = []
    for index in range(count):
        fallback_item = fallback_ideas[index]
        item = parsed[index] if index < len(parsed) else None
        if not isinstance(item, dict):
            output.append(fallback_item)
            continue

        candidate: dict[str, str] = {}
        valid = True
        for key in _REQUIRED_IDEA_KEYS:
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                valid = False
                break
            candidate[key] = value.strip()
        if not valid:
            output.append(fallback_item)
            continue
        if not _is_compliance_safe(" ".join(candidate.values())):
            output.append(fallback_item)
            continue
        output.append(candidate)
    return output


def _template_build_brief(video: Video) -> str:
    return f"""# Creative Brief

## Title
{video.title}

## Content type
{video.content_type.value}

## Pillar
{video.pillar}

## Target viewer
{video.target_viewer}

## Pain point
{video.pain_point}

## Demo idea
{video.demo_idea}

## Thumbnail text
{video.thumbnail_text}

## Angle
Show the old messy workflow, then show the cleaner AI-assisted workflow. Keep the video practical and original. Do not claim verified client results unless the operator provides proof.
""".strip()


def build_brief(video: Video) -> str:
    return _render_asset_with_fallback(video, "brief", _template_build_brief)


def _template_build_script(video: Video) -> str:
    business = _infer_business(video.title)
    return f"""# Long-Form Script: {video.title}

## 0:00 Hook
A {business} does not only lose money when the work goes wrong. It loses money when a real customer reaches out and the business is too slow to respond.

## 0:15 Problem
The problem is not that local businesses are lazy. The problem is that the operation is messy. Calls come in while the owner is driving, on a job site, helping another customer, or already closed for the day.

When that happens, the customer does not wait. They call the next business.

## 1:00 Old workflow
Here is the old workflow:

1. Customer calls.
2. Nobody answers.
3. Customer leaves a voicemail or hangs up.
4. Someone checks the message later.
5. The callback happens too late.
6. The customer may already be gone.

The marketing worked. The phone rang. The business still lost the opportunity.

## 2:00 AI workflow
Here is the AI-assisted workflow:

1. Customer calls.
2. The AI receptionist answers immediately.
3. It asks what the customer needs.
4. It collects name, phone, address, urgency, and service type.
5. It summarizes the call.
6. It pushes the lead into a dashboard.
7. It alerts the owner or team.
8. If scheduling rules are connected, it can request an appointment window.

The goal is not to replace the business. The goal is to stop simple opportunities from slipping through the cracks.

## 3:30 Demo segment
In the demo dashboard, the operator can see:

- New calls
- Missed calls recovered
- Urgent leads
- Service requested
- Customer address
- AI summary
- Recommended next step
- Follow-up status

This turns a phone call into structured business data.

## 5:30 Why it matters
The real value is not the AI voice. The real value is speed, structure, and follow-up.

A local business owner should not have to dig through voicemail, texts, sticky notes, and memory to figure out what happened today.

They should open one screen and know exactly who needs attention.

## 6:45 Guardrails
This system should have guardrails.

It should not promise exact pricing unless pricing rules are approved.
It should not guarantee availability unless the schedule is connected.
It should not diagnose issues as fact.
It should not pretend to be a licensed expert.

A safe AI receptionist says things like:

“I can collect the details and send them to the team.”
“I can mark this as urgent.”
“I can request an appointment window.”

## 8:00 Takeaway
Most local businesses do not need complicated AI.

They need a system that answers, captures, summarizes, routes, and follows up.

That is the real opportunity.

## 8:45 CTA
This video is an educational/demo workflow. If you want to see more AI systems for local businesses, subscribe. If you want a demo built around your business, use the link in the description.
""".strip()


def _template_build_short_script(video: Video) -> str:
    business = _infer_business(video.title)
    return f"""# Short-Form Script (45-60s): {video.title}

## 0:00-0:03 Hook
If you run a {business}, one missed call can quietly become a lost job.

## 0:03-0:25 One Clear Point
Most losses are not from bad service. They happen when nobody answers, details are missed, and follow-up starts too late.

## 0:25-0:45 Vertical Demo Direction
Show a vertical split: missed call log on top, then a clean lead card with name, issue, urgency, and next step.

## 0:45-0:58 Practical Wrap
This is not magic. It is a safer workflow: answer fast, capture details, tag urgency, and follow up on time.

## 0:58-1:00 CTA / Loop
Want more local AI workflow breakdowns? Follow for the next short.
""".strip()


def build_script(video: Video) -> str:
    content_type = getattr(video, "content_type", ContentType.long)
    if content_type == ContentType.short:
        return _render_asset_with_fallback(video, "script", _template_build_short_script)
    return _render_asset_with_fallback(video, "script", _template_build_script)


def _template_build_shorts(video: Video) -> str:
    return f"""# Shorts Pack for {video.title}

## Short 1: Missed Call Problem
Voiceover:
A local business does not always lose the customer because of bad work. Sometimes it loses the customer because nobody answered the phone. The customer had intent, the phone rang, and the system failed. That is why missed-call recovery matters.

On-screen text:
MISSED CALLS = LOST JOBS

Visual direction:
Show ringing phone, voicemail screen, then dashboard card marked “urgent lead.”

## Short 2: Old Workflow vs AI Workflow
Voiceover:
Old workflow: customer calls, no answer, voicemail, late callback. AI workflow: customer calls, AI answers, details are captured, the lead is tagged, and the owner gets a summary. Same call, completely different outcome.

On-screen text:
OLD WAY VS AI WAY

Visual direction:
Split screen with messy voicemail on left and clean dashboard on right.

## Short 3: Not Magic, Just Process
Voiceover:
AI does not magically book jobs. It follows a process. Answer the call, ask the right questions, collect details, check urgency, summarize everything, and route the lead. The process is the product.

On-screen text:
THE PROCESS IS THE PRODUCT

Visual direction:
Step-by-step workflow animation.

## Short 4: Dashboard Value
Voiceover:
The AI voice is not the most important part. The dashboard is. The owner needs to know who called, what they needed, how urgent it is, and what happens next.

On-screen text:
ONE SCREEN. EVERY LEAD.

Visual direction:
Dark dashboard with lead cards and urgency badges.

## Short 5: Guardrails Matter
Voiceover:
A good AI receptionist should not make fake promises. It should not quote prices unless approved. It should not guarantee availability unless the schedule is connected. Useful AI needs guardrails.

On-screen text:
AI NEEDS GUARDRAILS

Visual direction:
Checklist overlay with approved/review/urgent states.
""".strip()


def build_shorts(video: Video) -> str:
    return _render_asset_with_fallback(video, "shorts", _template_build_shorts)


def _template_build_description(video: Video) -> str:
    return f"""Local businesses lose money when calls are missed, leads are not followed up with, and customer details get scattered across voicemails, texts, and notebooks.

In this video, I break down: {video.title}

This is an educational/demo video. Any business examples shown are sample workflows unless clearly stated otherwise.

AI disclosure: This video was produced with AI assistance (script, voiceover, and/or visuals).

Topics covered:
- AI receptionist workflow
- Missed call recovery
- Lead qualification
- Contractor automation
- Local business CRM
- Follow-up systems
- AI dashboard demos

Want a demo system for your business?
[INSERT LINK]

Subscribe for more local business AI systems and dashboard breakdowns.

#AIAutomation #LocalBusiness #AIReceptionist #SmallBusiness #ContractorMarketing
""".strip()


def build_description(video: Video) -> str:
    rendered = _render_asset_with_fallback(video, "description", _template_build_description)
    offers = parse_affiliate_offers(get_settings().affiliate_offers_json)
    return apply_monetization(rendered, offers)


def _template_build_thumbnail_prompt(video: Video) -> str:
    return f"""Create a premium YouTube thumbnail in a dark luxury dashboard style.

Text: {video.thumbnail_text}

Style:
- Charcoal black background
- Cream typography
- Restrained orange/gold accent glow
- One strong business object: phone, dashboard card, calendar, invoice, or truck
- High contrast
- No cartoon robot graphics
- No fake money
- No faces unless licensed
- Mobile-readable at small size
""".strip()


def build_thumbnail_prompt(video: Video) -> str:
    return _render_asset_with_fallback(video, "thumbnail_prompt", _template_build_thumbnail_prompt)


def _template_build_youtube_metadata(video: Video) -> str:
    tags = ", ".join(DEFAULT_TAGS)
    return f"""title: {video.title}
description: {_template_build_description(video)}
tags: {tags}
category_id: 27
privacy_status: private
made_for_kids: false
review_required: true
""".strip()


def build_youtube_metadata(video: Video) -> str:
    return _render_asset_with_fallback(video, "youtube_metadata", _template_build_youtube_metadata)


def build_all_assets(video: Video) -> list[GeneratedAsset]:
    brief = build_brief(video)
    script = build_script(video)
    shorts = build_shorts(video)
    description = build_description(video)
    thumbnail_prompt = build_thumbnail_prompt(video)
    youtube_metadata = build_youtube_metadata(video)
    return [
        GeneratedAsset("brief", brief),
        GeneratedAsset("script", script),
        GeneratedAsset("shorts", shorts),
        GeneratedAsset("description", description),
        GeneratedAsset("thumbnail_prompt", thumbnail_prompt),
        GeneratedAsset("youtube_metadata", youtube_metadata),
        GeneratedAsset("package_manifest", build_review_checklist(script)),
    ]


def _infer_business(title: str) -> str:
    lower = title.lower()
    if "roof" in lower:
        return "roofing company"
    if "hvac" in lower:
        return "HVAC company"
    if "restaurant" in lower or "pizza" in lower:
        return "restaurant"
    if "med spa" in lower:
        return "med spa"
    if "contractor" in lower:
        return "contractor"
    return "local business"
