from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

from app.models import ContentAgent
from app.services.agents import LANE_ALIAS_TO_AGENT_NAME


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


@dataclass(frozen=True)
class IntakeLaneTemplate:
    lane: str
    audiences: tuple[str, ...]
    monetization_paths: tuple[str, ...]
    topic_templates: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class IntakeBlueprint:
    topic: str
    niche_lane: str
    audience: str
    monetization_path: str
    notes: str
    assigned_agent_id: int | None
    assigned_agent: str


LANE_TEMPLATES: tuple[IntakeLaneTemplate, ...] = (
    IntakeLaneTemplate(
        lane="AI tool breakdowns",
        audiences=("small business owners", "operators evaluating AI stacks"),
        monetization_paths=("affiliate tools + templates", "affiliate software + prompt packs"),
        topic_templates=(
            "AI tool stack for local business intake workflows",
            "AI tool comparison for operator command centers",
        ),
        notes="Deterministic daily intake candidate. Educational workflow angle only.",
    ),
    IntakeLaneTemplate(
        lane="AI business automation",
        audiences=("small business operators", "service business teams"),
        monetization_paths=("consulting audits + lead qualification", "service audit + implementation consulting"),
        topic_templates=(
            "AI business automation SOP for missed-call follow-up",
            "Small business workflow automation with local AI operators",
        ),
        notes="Deterministic daily intake candidate. Keep claims operational and verifiable.",
    ),
    IntakeLaneTemplate(
        lane="AI side hustles / business model breakdowns",
        audiences=("aspiring operators", "solo founders exploring AI services"),
        monetization_paths=("templates + audit offers", "affiliate tools + practical playbooks"),
        topic_templates=(
            "AI side hustle model: service audit funnel breakdown",
            "Business model breakdown for AI automation micro-agencies",
        ),
        notes="Deterministic daily intake candidate. No income guarantees or fake outcome claims.",
    ),
    IntakeLaneTemplate(
        lane="faceless YouTube / creator automation",
        audiences=("faceless channel operators", "creator operations teams"),
        monetization_paths=("templates + affiliate software", "prompt packs + creator systems templates"),
        topic_templates=(
            "Faceless YouTube creator automation workflow map",
            "Creator operations checklist for local AI media workflows",
        ),
        notes="Deterministic daily intake candidate. Manual review and compliance gates required.",
    ),
    IntakeLaneTemplate(
        lane="ecommerce AI",
        audiences=("ecommerce founders", "store operators"),
        monetization_paths=("affiliate tools + ecommerce templates", "consulting audits + stack recommendations"),
        topic_templates=(
            "Ecommerce AI operations workflow for product research",
            "Store ops automation workflow using AI operators",
        ),
        notes="Deterministic daily intake candidate. Educational examples only unless verified.",
    ),
    IntakeLaneTemplate(
        lane="local business AI automation",
        audiences=("local service business owners", "front-desk operations teams"),
        monetization_paths=("SkybridgeCX leads + audits", "consulting audit + implementation"),
        topic_templates=(
            "Local business AI automation checklist for appointment handling",
            "Customer-call workflow automation for local operators",
        ),
        notes="Deterministic daily intake candidate. Keep operational claims realistic.",
    ),
    IntakeLaneTemplate(
        lane="career/productivity AI",
        audiences=("knowledge workers", "career-focused operators"),
        monetization_paths=("digital templates + affiliates", "productivity templates + prompt packs"),
        topic_templates=(
            "Career productivity AI workflow for weekly execution planning",
            "AI productivity system for operator task triage",
        ),
        notes="Deterministic daily intake candidate. Practical execution focus.",
    ),
)


def _find_agent_for_lane(agents: list[ContentAgent], lane: str) -> ContentAgent | None:
    normalized_lane = normalize_text(lane)
    for agent in agents:
        if normalize_text(agent.lane) == normalized_lane:
            return agent

    mapped_name = LANE_ALIAS_TO_AGENT_NAME.get(normalized_lane)
    if mapped_name:
        mapped_norm = normalize_text(mapped_name)
        for agent in agents:
            if normalize_text(agent.name) == mapped_norm:
                return agent
    return None


def build_daily_seed_blueprints(*, for_day: date, active_agents: list[ContentAgent], limit: int = 7) -> list[IntakeBlueprint]:
    if limit <= 0:
        return []

    offset = for_day.toordinal()
    blueprints: list[IntakeBlueprint] = []
    lane_templates = list(LANE_TEMPLATES)
    total_templates = min(limit, len(lane_templates))
    for lane_index in range(total_templates):
        template = lane_templates[lane_index]
        pick = (offset + lane_index) % len(template.topic_templates)
        topic = template.topic_templates[pick]
        audience = template.audiences[(offset + lane_index) % len(template.audiences)]
        monetization = template.monetization_paths[(offset + lane_index) % len(template.monetization_paths)]
        matched_agent = _find_agent_for_lane(active_agents, template.lane)
        blueprints.append(
            IntakeBlueprint(
                topic=topic,
                niche_lane=template.lane,
                audience=audience,
                monetization_path=monetization,
                notes=template.notes,
                assigned_agent_id=matched_agent.id if matched_agent else None,
                assigned_agent=matched_agent.name if matched_agent else "Opportunity Research Agent",
            )
        )
    return blueprints
