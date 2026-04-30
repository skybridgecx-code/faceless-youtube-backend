from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ContentAgent, VideoOpportunity
from app.services.audit import log_audit_event

DEFAULT_AGENT_DEFINITIONS: list[dict[str, str]] = [
    {
        "name": "AI Tools Agent",
        "lane": "AI tool breakdowns",
        "focus": "Tool reviews, comparisons, and practical tutorials.",
        "monetization_focus": "Affiliate links, templates, and prompt packs.",
        "compliance_notes": "Avoid fake performance claims. Frame tool examples as educational demonstrations.",
        "production_rules": "Use hands-on walkthrough structure with clear pros/cons and operator-safe notes.",
    },
    {
        "name": "AI Business Automation Agent",
        "lane": "AI business automation",
        "focus": "Workflow systems for small businesses.",
        "monetization_focus": "Consulting, audits, and SkybridgeCX leads.",
        "compliance_notes": "No unsupported client claims. Keep operational examples realistic and verifiable.",
        "production_rules": "Focus on repeatable SOP-style workflow steps and practical constraints.",
    },
    {
        "name": "AI Side Hustle Agent",
        "lane": "AI side hustles / business model breakdowns",
        "focus": "Realistic business model breakdowns with conservative assumptions.",
        "monetization_focus": "Templates, audits, and affiliate tools.",
        "compliance_notes": "No fake income claims. Avoid guaranteed outcomes and get-rich framing.",
        "production_rules": "Use risk-first format with cost/time assumptions and caveats.",
    },
    {
        "name": "Faceless Creator Agent",
        "lane": "faceless YouTube / creator automation",
        "focus": "Production workflows and creator operating systems.",
        "monetization_focus": "Templates, prompt packs, and affiliate software.",
        "compliance_notes": "Do not imply platform exploits. Keep methods policy-safe and manual-review gated.",
        "production_rules": "Prioritize reproducible production checklists over hype.",
    },
    {
        "name": "Ecommerce AI Agent",
        "lane": "ecommerce AI",
        "focus": "Product research, store operations, and automation systems.",
        "monetization_focus": "Templates, consulting, and affiliate tools.",
        "compliance_notes": "Avoid unsupported conversion/revenue claims. Label examples as educational unless verified.",
        "production_rules": "Anchor each video around one operator workflow with measurable checkpoints.",
    },
    {
        "name": "Local Business AI Agent",
        "lane": "local business AI automation",
        "focus": "Appointment handling, customer calls, and lead capture workflows.",
        "monetization_focus": "SkybridgeCX leads, audits, and consulting.",
        "compliance_notes": "No fake client outcomes. Keep integrations and call-flow claims realistic.",
        "production_rules": "Show before/after operational flow with explicit manual approval gates.",
    },
    {
        "name": "Career/Productivity AI Agent",
        "lane": "career/productivity AI",
        "focus": "Practical productivity and work improvement systems.",
        "monetization_focus": "Templates, digital products, and affiliates.",
        "compliance_notes": "Avoid absolute outcomes. Keep advice educational and context-aware.",
        "production_rules": "Use actionable step-by-step execution format with realistic time expectations.",
    },
]

LANE_ALIAS_TO_AGENT_NAME: dict[str, str] = {
    "ai ecommerce ops": "Ecommerce AI Agent",
    "ecommerce founders": "Ecommerce AI Agent",
    "ecommerce ai": "Ecommerce AI Agent",
    "local business ai operations": "Local Business AI Agent",
    "local business ai automation": "Local Business AI Agent",
    "ai tool breakdowns": "AI Tools Agent",
    "tool stack": "AI Tools Agent",
    "ai tools": "AI Tools Agent",
    "faceless youtube": "Faceless Creator Agent",
    "creator automation": "Faceless Creator Agent",
    "ai side hustles": "AI Side Hustle Agent",
    "business model breakdowns": "AI Side Hustle Agent",
    "ai business automation": "AI Business Automation Agent",
    "small business workflows": "AI Business Automation Agent",
    "career productivity ai": "Career/Productivity AI Agent",
    "productivity": "Career/Productivity AI Agent",
}

AGENT_KEYWORD_HINTS: dict[str, tuple[str, ...]] = {
    "Ecommerce AI Agent": ("ecommerce", "shopify", "store ops", "product research"),
    "Local Business AI Agent": ("local business", "appointment", "customer calls", "lead capture"),
    "AI Tools Agent": ("ai tools", "tool stack", "tool breakdown", "comparison", "review"),
    "Faceless Creator Agent": ("faceless youtube", "creator automation", "creator systems"),
    "AI Side Hustle Agent": ("side hustle", "business model"),
    "AI Business Automation Agent": ("business automation", "small business workflow", "operations workflow"),
    "Career/Productivity AI Agent": ("productivity", "career", "work improvement"),
}


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _find_agent_by_normalized_name(agents: list[ContentAgent], name: str | None) -> ContentAgent | None:
    target = _normalize(name)
    if not target:
        return None
    for agent in agents:
        if _normalize(agent.name) == target:
            return agent
    return None


def _find_agent_by_name_or_lane_match(agents: list[ContentAgent], *, name: str, lane: str) -> ContentAgent | None:
    target_name = _normalize(name)
    target_lane = _normalize(lane)
    for agent in agents:
        if _normalize(agent.name) == target_name:
            return agent
        if _normalize(agent.lane) == target_lane:
            return agent
    return None


def _has_global_unique_name_constraint(db: Session) -> bool:
    try:
        index_rows = db.execute(text("PRAGMA index_list(content_agents)")).fetchall()
    except Exception:  # noqa: BLE001
        return False

    for row in index_rows:
        index_name = row[1]
        is_unique = bool(row[2])
        if not is_unique:
            continue
        try:
            columns = db.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
        except Exception:  # noqa: BLE001
            continue
        if len(columns) == 1 and columns[0][2] == "name":
            return True
    return False


def _dedupe_agent_name(base_name: str, channel_id: int, existing_names: set[str]) -> str:
    candidate = base_name
    if candidate not in existing_names:
        return candidate
    candidate = f"{base_name} (Channel {channel_id})"
    counter = 2
    while candidate in existing_names:
        candidate = f"{base_name} (Channel {channel_id}-{counter})"
        counter += 1
    return candidate


def seed_default_agents_if_empty(db: Session, channel_id: int) -> list[ContentAgent]:
    def _current_channel_agents() -> list[ContentAgent]:
        return list(
            db.scalars(
                select(ContentAgent).where(ContentAgent.channel_id == channel_id).order_by(ContentAgent.id.asc())
            )
        )

    existing = _current_channel_agents()
    requires_unique_names = _has_global_unique_name_constraint(db)
    all_names = set(db.scalars(select(ContentAgent.name)))

    def _build_missing_agents(source_existing: list[ContentAgent]) -> tuple[list[ContentAgent], list[ContentAgent]]:
        created_agents: list[ContentAgent] = []
        observed = list(source_existing)
        for item in DEFAULT_AGENT_DEFINITIONS:
            if _find_agent_by_name_or_lane_match(observed, name=item["name"], lane=item["lane"]):
                continue
            agent_name = item["name"]
            if requires_unique_names and agent_name in all_names:
                agent_name = _dedupe_agent_name(item["name"], channel_id, all_names)
            all_names.add(agent_name)
            agent = ContentAgent(
                channel_id=channel_id,
                name=agent_name,
                lane=item["lane"],
                focus=item["focus"],
                monetization_focus=item["monetization_focus"],
                compliance_notes=item["compliance_notes"],
                production_rules=item["production_rules"],
                is_active=True,
            )
            db.add(agent)
            observed.append(agent)
            created_agents.append(agent)
        return created_agents, observed

    created, observed_agents = _build_missing_agents(existing)
    if not created:
        return observed_agents

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        all_names.clear()
        all_names.update(db.scalars(select(ContentAgent.name)))
        existing = _current_channel_agents()
        created, observed_agents = _build_missing_agents(existing)
        if created:
            db.commit()

    for agent in created:
        db.refresh(agent)
        log_audit_event(
            db,
            "agent_created_seed",
            f"Seeded content agent: {agent.name}",
            metadata={"agent_id": agent.id, "channel_id": agent.channel_id, "lane": agent.lane},
        )
    return _current_channel_agents()


def ensure_channel_agents(db: Session, channel_id: int) -> list[ContentAgent]:
    return seed_default_agents_if_empty(db, channel_id)


def resolve_agent_for_opportunity(db: Session, opportunity: VideoOpportunity) -> ContentAgent | None:
    if opportunity.assigned_agent_id:
        matched = db.get(ContentAgent, opportunity.assigned_agent_id)
        if matched:
            return matched

    agents = list(
        db.scalars(
            select(ContentAgent)
            .where(ContentAgent.channel_id == opportunity.channel_id, ContentAgent.is_active.is_(True))
            .order_by(ContentAgent.id.asc())
        )
    )
    if not agents:
        return None

    lane_value = _normalize(opportunity.niche_lane)
    for agent in agents:
        if _normalize(agent.lane) == lane_value:
            return agent

    alias_target_name: str | None = None
    if lane_value:
        alias_target_name = LANE_ALIAS_TO_AGENT_NAME.get(lane_value)
        if alias_target_name is None:
            for alias, canonical_name in LANE_ALIAS_TO_AGENT_NAME.items():
                if alias in lane_value:
                    alias_target_name = canonical_name
                    break
    if alias_target_name:
        matched = _find_agent_by_normalized_name(agents, alias_target_name)
        if matched:
            return matched

    assigned_text = _normalize(opportunity.assigned_agent)
    if assigned_text:
        for agent in agents:
            agent_name = _normalize(agent.name)
            if agent_name == assigned_text or agent_name in assigned_text or assigned_text in agent_name:
                return agent

    haystack = _normalize(
        " ".join(
            [
                opportunity.topic or "",
                opportunity.niche_lane or "",
                opportunity.audience or "",
                opportunity.assigned_agent or "",
            ]
        )
    )
    if haystack:
        for canonical_name, keywords in AGENT_KEYWORD_HINTS.items():
            if any(_normalize(keyword) in haystack for keyword in keywords):
                matched = _find_agent_by_normalized_name(agents, canonical_name)
                if matched:
                    return matched

    return None


def maybe_assign_agent_to_opportunity(db: Session, opportunity: VideoOpportunity, *, reason: str) -> ContentAgent | None:
    previous_agent_id = opportunity.assigned_agent_id
    matched = resolve_agent_for_opportunity(db, opportunity)
    if matched and matched.id != opportunity.assigned_agent_id:
        opportunity.assigned_agent_id = matched.id
        opportunity.assigned_agent = matched.name
        if opportunity.id is not None:
            log_audit_event(
                db,
                "opportunity_agent_assigned",
                f"Assigned opportunity '{opportunity.topic}' to agent: {matched.name}",
                metadata={
                    "opportunity_id": opportunity.id,
                    "previous_agent_id": previous_agent_id,
                    "assigned_agent_id": matched.id,
                    "reason": reason,
                },
            )
    return matched


def first_active_agent_name(agents: list[ContentAgent]) -> str:
    for agent in agents:
        if agent.is_active:
            return agent.name
    return agents[0].name if agents else "Opportunity Research Agent"


def agent_public_payload(agent: ContentAgent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "lane": agent.lane,
        "focus": agent.focus,
        "monetization_focus": agent.monetization_focus,
        "compliance_notes": agent.compliance_notes,
        "production_rules": agent.production_rules,
        "is_active": agent.is_active,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }
