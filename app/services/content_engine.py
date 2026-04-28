from __future__ import annotations

from dataclasses import dataclass

from app.models import Video
from app.services.compliance import build_review_checklist


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


def generate_video_ideas(count: int) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for title, pillar, target_viewer, pain_point, demo_idea, thumbnail_text in FIRST_IDEAS[:count]:
        output.append(
            {
                "title": title,
                "pillar": pillar,
                "target_viewer": target_viewer,
                "pain_point": pain_point,
                "demo_idea": demo_idea,
                "thumbnail_text": thumbnail_text,
            }
        )
    while len(output) < count:
        n = len(output) + 1
        output.append(
            {
                "title": f"Local Business AI Workflow #{n}",
                "pillar": "Business dashboard demos",
                "target_viewer": "Local service business owner",
                "pain_point": "Missed follow-up and unclear lead status",
                "demo_idea": "AI workflow and dashboard walkthrough",
                "thumbnail_text": "AI WORKFLOW",
            }
        )
    return output


def build_brief(video: Video) -> str:
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


def build_script(video: Video) -> str:
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


def build_shorts(video: Video) -> str:
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


def build_description(video: Video) -> str:
    return f"""Local businesses lose money when calls are missed, leads are not followed up with, and customer details get scattered across voicemails, texts, and notebooks.

In this video, I break down: {video.title}

This is an educational/demo video. Any business examples shown are sample workflows unless clearly stated otherwise.

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


def build_thumbnail_prompt(video: Video) -> str:
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


def build_youtube_metadata(video: Video) -> str:
    tags = ", ".join(DEFAULT_TAGS)
    return f"""title: {video.title}
description: {build_description(video)}
tags: {tags}
category_id: 27
privacy_status: private
made_for_kids: false
review_required: true
""".strip()


def build_all_assets(video: Video) -> list[GeneratedAsset]:
    script = build_script(video)
    return [
        GeneratedAsset("brief", build_brief(video)),
        GeneratedAsset("script", script),
        GeneratedAsset("shorts", build_shorts(video)),
        GeneratedAsset("description", build_description(video)),
        GeneratedAsset("thumbnail_prompt", build_thumbnail_prompt(video)),
        GeneratedAsset("youtube_metadata", build_youtube_metadata(video)),
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
