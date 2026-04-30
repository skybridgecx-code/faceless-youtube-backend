from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.models import ProductionBrief, Video


SCENE_BLUEPRINTS: list[tuple[str, str]] = [
    ("Hook Problem", "Introduce the operator pain clearly and fast."),
    ("Current Chaos", "Show the messy current workflow and missed handoffs."),
    ("System Overview", "Present the proposed workflow map with clear steps."),
    ("Step 1 Setup", "Walk through first implementation step with realistic tooling."),
    ("Step 2 Operations", "Show daily operations and operator checks."),
    ("Step 3 Quality Gate", "Highlight review and compliance checkpoints."),
    ("Demo Walkthrough", "Show dashboard/demo sequence with sample-safe data."),
    ("Before vs After", "Contrast old vs new workflow without fake promises."),
    ("Risks and Caveats", "State constraints, caveats, and safe claims framing."),
    ("CTA Wrap", "Close with a clear educational CTA and next step."),
]

FORBIDDEN_STYLE_NOTE = (
    "Use an original visual treatment. Do not imitate specific creators, franchises, "
    "or copyrighted thumbnail styles."
)

SAFETY_BASELINE = (
    "No fake proof, no fabricated performance screenshots, no fake testimonials, "
    "and no fake logos unless explicitly labeled as demo/fake assets."
)


@dataclass
class ScenePlan:
    scene_number: int
    scene_title: str
    narrative_beat: str
    on_screen_text: str
    image_prompt: str
    animation_prompt: str
    b_roll_prompt: str
    dashboard_demo_prompt: str
    safety_notes: str


@dataclass
class VisualPlanDraft:
    source_type: str
    title: str
    thumbnail_prompt: str
    thumbnail_text: str
    motion_style: str
    color_direction: str
    plan_notes: str
    safety_notes: str
    scenes: list[ScenePlan]


def normalize_text(value: str | None, fallback: str) -> str:
    text = re.sub(r"\s+", " ", (value or "")).strip()
    return text if text else fallback


def _scene_count(seed_text: str) -> int:
    digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    return 6 + (int(digest[:2], 16) % 5)


def _thumbnail_text(topic: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]+", "", topic.upper()).split()
    return " ".join(words[:6])[:80] or "AI WORKFLOW SYSTEM"


def _build_prompt_components(
    *,
    subject: str,
    environment: str,
    composition: str,
    lighting: str,
    camera_motion: str,
    mood: str,
    overlay_text: str,
) -> dict[str, str]:
    image_prompt = (
        f"Subject: {subject}. "
        f"Environment: {environment}. "
        f"Composition: {composition}. "
        f"Lighting: {lighting}. "
        f"Mood: {mood}. "
        f"Text overlay: {overlay_text or 'No overlay'}. "
        f"{FORBIDDEN_STYLE_NOTE} {SAFETY_BASELINE}"
    )
    animation_prompt = (
        f"Subject: {subject}. "
        f"Environment: {environment}. "
        f"Composition: {composition}. "
        f"Lighting: {lighting}. "
        f"Camera motion: {camera_motion}. "
        f"Mood: {mood}. "
        f"Text overlay: {overlay_text or 'No overlay'}. "
        f"{FORBIDDEN_STYLE_NOTE} {SAFETY_BASELINE}"
    )
    b_roll_prompt = (
        f"Subject: close-up details of {subject}. "
        f"Environment: {environment}. "
        f"Composition: practical inserts, hands-on actions, UI closeups, and transitions. "
        f"Lighting: {lighting}. Mood: {mood}. "
        f"Avoid fake KPI spikes and fabricated proof visuals."
    )
    dashboard_demo_prompt = (
        f"Subject: demo dashboard/workflow board for {subject}. "
        f"Environment: clean operator workstation UI scene. "
        f"Composition: clear cards, step tracker, and labeled panels using sample data only. "
        f"Lighting: {lighting}. Camera motion: {camera_motion}. Mood: {mood}. "
        f"Use placeholder/fake branding labels when needed; do not show real brand logos as proof."
    )
    return {
        "image_prompt": image_prompt,
        "animation_prompt": animation_prompt,
        "b_roll_prompt": b_roll_prompt,
        "dashboard_demo_prompt": dashboard_demo_prompt,
    }


def _scene_rows(topic: str, audience: str, angle: str, seed_text: str) -> list[ScenePlan]:
    count = _scene_count(seed_text)
    rows: list[ScenePlan] = []
    for idx in range(count):
        title, beat = SCENE_BLUEPRINTS[idx]
        subject = f"{topic} for {audience}"
        environment = "modern operations desk with neutral UI mockups and sample-safe metrics"
        composition = f"scene {idx + 1} storyboard frame, clear focal subject, depth layers, widescreen 16:9"
        lighting = "cinematic soft key light with practical monitor glow"
        camera_motion = "slow push-in with subtle lateral drift"
        mood = "credible, focused, and practical"
        on_screen_text = f"{title}: {angle}"[:170]
        prompt_parts = _build_prompt_components(
            subject=subject,
            environment=environment,
            composition=composition,
            lighting=lighting,
            camera_motion=camera_motion,
            mood=mood,
            overlay_text=on_screen_text,
        )
        rows.append(
            ScenePlan(
                scene_number=idx + 1,
                scene_title=title,
                narrative_beat=beat,
                on_screen_text=on_screen_text,
                image_prompt=prompt_parts["image_prompt"],
                animation_prompt=prompt_parts["animation_prompt"],
                b_roll_prompt=prompt_parts["b_roll_prompt"],
                dashboard_demo_prompt=prompt_parts["dashboard_demo_prompt"],
                safety_notes=f"{FORBIDDEN_STYLE_NOTE} {SAFETY_BASELINE}",
            )
        )
    return rows


def build_visual_plan_for_video(video: Video) -> VisualPlanDraft:
    topic = normalize_text(video.title, "Faceless workflow video")
    audience = normalize_text(video.target_audience or video.target_viewer, "operator audience")
    angle = normalize_text(video.angle or video.demo_idea, "show practical workflow")
    lane = normalize_text(video.niche, "general faceless automation")
    thumbnail_text = normalize_text(video.thumbnail_text, _thumbnail_text(topic))
    motion_style = "Kinetic editorial motion with controlled pans, zooms, and UI overlays"
    color_direction = "Charcoal background, cool blue highlights, restrained amber accent for key actions"
    thumbnail_prompt = (
        f"Subject: {topic}. Environment: dark editorial workspace with clean UI elements. "
        f"Composition: high-contrast central focal object with one supporting dashboard panel, 16:9 thumbnail crop. "
        f"Lighting: dramatic rim + monitor glow. Mood: urgent but credible. "
        f"Text overlay: {thumbnail_text}. {FORBIDDEN_STYLE_NOTE} {SAFETY_BASELINE}"
    )
    scenes = _scene_rows(topic, audience, angle, seed_text=f"video:{video.id}:{topic}:{lane}")
    return VisualPlanDraft(
        source_type="video",
        title=topic,
        thumbnail_prompt=thumbnail_prompt,
        thumbnail_text=thumbnail_text,
        motion_style=motion_style,
        color_direction=color_direction,
        plan_notes=(
            "Deterministic local-first visual planning draft. Prompts only. "
            "No image/video files are generated in this phase."
        ),
        safety_notes=f"{FORBIDDEN_STYLE_NOTE} {SAFETY_BASELINE}",
        scenes=scenes,
    )


def build_visual_plan_for_brief(brief: ProductionBrief) -> VisualPlanDraft:
    topic = normalize_text(brief.title or brief.topic, "Production brief video")
    audience = normalize_text(brief.target_audience, "operator audience")
    angle = normalize_text(brief.thumbnail_angle or brief.hook, "explain the workflow")
    lane = normalize_text(brief.niche_lane, "faceless automation")
    thumbnail_text = _thumbnail_text(topic)
    motion_style = "Structured storyboard motion with deliberate zooms and scene-to-scene continuity"
    color_direction = "Deep graphite UI scene, steel blue mid-tones, warm highlight for CTA moments"
    thumbnail_prompt = (
        f"Subject: {topic}. Environment: controlled studio-style workspace with process map visuals. "
        f"Composition: layered foreground subject + supporting checklist card, optimized for readability at small sizes. "
        f"Lighting: directional key with subdued background fill. Mood: tactical and trustworthy. "
        f"Text overlay: {thumbnail_text}. {FORBIDDEN_STYLE_NOTE} {SAFETY_BASELINE}"
    )
    scenes = _scene_rows(topic, audience, angle, seed_text=f"brief:{brief.id}:{topic}:{lane}")
    return VisualPlanDraft(
        source_type="brief",
        title=topic,
        thumbnail_prompt=thumbnail_prompt,
        thumbnail_text=thumbnail_text,
        motion_style=motion_style,
        color_direction=color_direction,
        plan_notes=(
            "Derived from approved/shortlisted production brief context. "
            "Prompts are editable and intended for later generation providers."
        ),
        safety_notes=f"{FORBIDDEN_STYLE_NOTE} {SAFETY_BASELINE}",
        scenes=scenes,
    )
