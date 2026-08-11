from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from functools import lru_cache
import json
from pathlib import Path
from typing import Iterable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.editorial.contracts import canonical_json, canonical_sha256


CAMPAIGN_BUDGET_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "policies" / "campaign_budget.v1.json"
)


class BudgetPolicyError(RuntimeError):
    """The locked campaign budget policy is absent or semantically invalid."""


class BudgetDecision(StrEnum):
    ALLOW_PRIMARY = "ALLOW_PRIMARY"
    USE_LOWER_COST_FALLBACK = "USE_LOWER_COST_FALLBACK"
    BLOCK_NEEDS_HUMAN = "BLOCK_NEEDS_HUMAN"


class _Scope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_type: str
    cost_basis: str
    metered_cost_categories: tuple[str, ...]
    excluded_local_costs: tuple[str, ...]
    local_cost_exclusion_condition: str


class _Limits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: int = Field(ge=0)
    soft_warning: int = Field(ge=0)
    default_hard_cap: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> _Limits:
        if not self.target < self.soft_warning < self.default_hard_cap:
            raise ValueError("campaign budget limits must be strictly ordered")
        return self


class _Requirements(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    require_preflight_estimate: bool
    fallback_after_soft_warning: bool
    block_request_above_hard_cap: bool
    owner_override_required: bool
    override_must_be_campaign_specific: bool
    override_must_be_audited: bool


class _SoftWarningBehavior(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_microusd: int = Field(ge=0)
    required_cost_reduction_actions: tuple[str, ...]
    editorial_quality_truth_rights_safety_and_approval_gates_remain_required: bool


class _HardCapBehavior(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pre_request_calculation: str
    compare_against: str
    on_predicted_breach: str
    allowed_responses: tuple[str, ...]


class _OverrideContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    automatic: bool
    campaign_specific: bool
    must_specify_new_authorized_cap: bool
    required_audit_fields: tuple[str, ...]
    auditable: bool
    analytics_may_mutate_global_policy: bool
    providers_may_mutate_global_policy: bool


class CampaignBudgetPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str
    currency: str
    scope: _Scope
    limits_microusd: _Limits
    requirements: _Requirements
    soft_warning_behavior: _SoftWarningBehavior
    hard_cap_behavior: _HardCapBehavior
    override_contract: _OverrideContract
    policy_sha256: str = Field(exclude=True)

    @model_validator(mode="after")
    def validate_locked_contract(self) -> CampaignBudgetPolicy:
        if self.policy_version != "campaign-budget-v1":
            raise ValueError("unexpected campaign budget policy version")
        if self.currency != "USD":
            raise ValueError("campaign budget currency must be USD")
        if self.limits_microusd.model_dump() != {
            "target": 20_000_000,
            "soft_warning": 25_000_000,
            "default_hard_cap": 35_000_000,
        }:
            raise ValueError("campaign budget limits conflict with the locked policy")
        requirements = self.requirements
        if not all(
            (
                requirements.require_preflight_estimate,
                requirements.fallback_after_soft_warning,
                requirements.block_request_above_hard_cap,
                requirements.owner_override_required,
                requirements.override_must_be_campaign_specific,
                requirements.override_must_be_audited,
            )
        ):
            raise ValueError("campaign budget requirements must remain enabled")
        if (
            self.soft_warning_behavior.trigger_microusd
            != self.limits_microusd.soft_warning
        ):
            raise ValueError("soft-warning trigger conflicts with the locked limit")
        override = self.override_contract
        if (
            override.automatic
            or not override.campaign_specific
            or not override.must_specify_new_authorized_cap
            or not override.auditable
            or override.analytics_may_mutate_global_policy
            or override.providers_may_mutate_global_policy
        ):
            raise ValueError("campaign budget override authority is invalid")
        required_fields = {
            "campaign_id",
            "new_authorized_cap_microusd",
            "actor",
            "reason",
            "timestamp",
        }
        if set(override.required_audit_fields) != required_fields:
            raise ValueError("campaign budget override audit fields are invalid")
        if self.hard_cap_behavior.on_predicted_breach != "do_not_send_provider_request":
            raise ValueError("hard-cap behavior must block the provider request")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"policy_sha256"})


@lru_cache(maxsize=1)
def load_campaign_budget_policy() -> CampaignBudgetPolicy:
    try:
        raw_text = CAMPAIGN_BUDGET_POLICY_PATH.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        if not isinstance(raw, dict):
            raise TypeError("policy root must be an object")
        policy_hash = canonical_sha256(raw)
        policy = CampaignBudgetPolicy.model_validate(
            {**raw, "policy_sha256": policy_hash}
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BudgetPolicyError("Campaign budget policy is invalid") from exc
    if canonical_json(policy.canonical_payload()) != canonical_json(raw):
        raise BudgetPolicyError("Campaign budget policy is not canonical after validation")
    return policy


class CostedJob(Protocol):
    status: str
    cost_microunits: int | None
    reserved_cost_microunits: int | None


@dataclass(frozen=True)
class EffectiveCampaignCost:
    committed_microusd: int
    reserved_microusd: int

    @property
    def total_microusd(self) -> int:
        return self.committed_microusd + self.reserved_microusd


def effective_campaign_cost(jobs: Iterable[CostedJob]) -> EffectiveCampaignCost:
    committed = 0
    reserved = 0
    for job in jobs:
        known = job.cost_microunits
        reservation = job.reserved_cost_microunits
        if known is not None:
            if known < 0:
                raise BudgetPolicyError("generation job cost cannot be negative")
            committed += known
            continue
        if job.status == "blocked_budget":
            continue
        if reservation is not None:
            if reservation < 0:
                raise BudgetPolicyError("generation reservation cannot be negative")
            reserved += reservation
    return EffectiveCampaignCost(
        committed_microusd=committed,
        reserved_microusd=reserved,
    )


@dataclass(frozen=True)
class BudgetOption:
    name: str
    reservation_microusd: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("budget option name must be nonblank")
        if self.reservation_microusd < 0:
            raise ValueError("budget reservation must be non-negative")


@dataclass(frozen=True)
class BudgetChoice:
    decision: BudgetDecision
    selected: BudgetOption | None
    projected_cost_microusd: int
    soft_warning_active: bool
    authorized_cap_microusd: int


def choose_budget_option(
    *,
    current_cost_microusd: int,
    authorized_cap_microusd: int,
    primary: BudgetOption,
    fallbacks: tuple[BudgetOption, ...] = (),
    policy: CampaignBudgetPolicy | None = None,
) -> BudgetChoice:
    budget_policy = policy or load_campaign_budget_policy()
    if current_cost_microusd < 0 or authorized_cap_microusd <= 0:
        raise ValueError("campaign cost and authorized cap must be valid")
    warning = budget_policy.limits_microusd.soft_warning
    primary_projected = current_cost_microusd + primary.reservation_microusd
    if primary_projected < warning and primary_projected <= authorized_cap_microusd:
        return BudgetChoice(
            decision=BudgetDecision.ALLOW_PRIMARY,
            selected=primary,
            projected_cost_microusd=primary_projected,
            soft_warning_active=False,
            authorized_cap_microusd=authorized_cap_microusd,
        )

    for fallback in fallbacks:
        if fallback.reservation_microusd >= primary.reservation_microusd:
            continue
        projected = current_cost_microusd + fallback.reservation_microusd
        if projected <= authorized_cap_microusd:
            return BudgetChoice(
                decision=BudgetDecision.USE_LOWER_COST_FALLBACK,
                selected=fallback,
                projected_cost_microusd=projected,
                soft_warning_active=projected >= warning or primary_projected >= warning,
                authorized_cap_microusd=authorized_cap_microusd,
            )

    return BudgetChoice(
        decision=BudgetDecision.BLOCK_NEEDS_HUMAN,
        selected=None,
        projected_cost_microusd=primary_projected,
        soft_warning_active=primary_projected >= warning,
        authorized_cap_microusd=authorized_cap_microusd,
    )


def budget_override_payload(
    *,
    campaign_id: int,
    policy_version: str,
    previous_authorized_cap_microunits: int,
    new_authorized_cap_microunits: int,
    actor: str,
    reason: str,
    timestamp: datetime,
) -> dict[str, object]:
    normalized_actor = actor.strip()
    normalized_reason = reason.strip()
    if campaign_id <= 0:
        raise ValueError("campaign_id must be positive")
    if not normalized_actor or not normalized_reason:
        raise ValueError("budget override actor and reason must be nonblank")
    if new_authorized_cap_microunits <= previous_authorized_cap_microunits:
        raise ValueError("new authorized cap must strictly increase")
    if timestamp.tzinfo is None:
        timestamp_utc = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp_utc = timestamp.astimezone(timezone.utc)
    timestamp_text = timestamp_utc.isoformat(
        timespec="microseconds",
    ).replace("+00:00", "Z")
    return {
        "actor": normalized_actor,
        "campaign_id": campaign_id,
        "contract_version": "i5-budget-override-v1",
        "new_authorized_cap_microunits": new_authorized_cap_microunits,
        "policy_version": policy_version,
        "previous_authorized_cap_microunits": previous_authorized_cap_microunits,
        "reason": normalized_reason,
        "timestamp": timestamp_text,
    }


def budget_override_hash(payload: Mapping[str, object]) -> str:
    return canonical_sha256(dict(payload))
