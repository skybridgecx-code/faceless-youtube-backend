"""Bounded I6-B2A soft-critic dispatch with no persistence or stage advancement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.production.budget import (
    BudgetChoice,
    BudgetDecision,
    BudgetOption,
    CampaignBudgetPolicy,
    choose_budget_option,
)
from app.production.providers import (
    I6_SOFT_REVIEW_FALLBACK_PROFILE,
    I6_SOFT_REVIEW_PRIMARY_PROFILE,
    SoftReviewExecutionProfile,
    SoftReviewProviderResult,
    soft_review_reservation_microusd,
    validate_soft_review_provider_result,
)
from app.qa.contracts import SoftReviewRequest


class I6SoftReviewProvider(Protocol):
    """State-free provider boundary for one immutable, preflighted review request."""

    def review(
        self,
        request: SoftReviewRequest,
        *,
        profile: SoftReviewExecutionProfile,
        reserved_cost_microusd: int,
    ) -> SoftReviewProviderResult: ...


@dataclass(frozen=True, slots=True)
class SoftReviewDispatchResult:
    """Ephemeral B2A result; persistence and workflow advancement are deferred."""

    status: Literal["EXECUTED", "BLOCK_NEEDS_HUMAN"]
    budget_choice: BudgetChoice
    execution: SoftReviewProviderResult | None

    def __post_init__(self) -> None:
        if self.status == "EXECUTED" and self.execution is None:
            raise ValueError("executed soft review requires provider execution metadata")
        if self.status == "BLOCK_NEEDS_HUMAN" and self.execution is not None:
            raise ValueError("blocked soft review must not include provider execution metadata")


def dispatch_soft_review(
    *,
    request: SoftReviewRequest,
    current_effective_campaign_cost_microusd: int,
    authorized_campaign_cap_microusd: int,
    provider: I6SoftReviewProvider,
    budget_policy: CampaignBudgetPolicy | None = None,
) -> SoftReviewDispatchResult:
    """Select a permitted profile before exactly one provider invocation, or block."""

    request.require_bound_unique_targets()
    primary_reservation = soft_review_reservation_microusd(
        request, I6_SOFT_REVIEW_PRIMARY_PROFILE
    )
    fallback_reservation = soft_review_reservation_microusd(
        request, I6_SOFT_REVIEW_FALLBACK_PROFILE
    )
    choice = choose_budget_option(
        current_cost_microusd=current_effective_campaign_cost_microusd,
        authorized_cap_microusd=authorized_campaign_cap_microusd,
        primary=BudgetOption("primary", primary_reservation),
        fallbacks=(BudgetOption("lower_cost_fallback", fallback_reservation),),
        policy=budget_policy,
    )
    if choice.decision == BudgetDecision.BLOCK_NEEDS_HUMAN:
        return SoftReviewDispatchResult(
            status="BLOCK_NEEDS_HUMAN",
            budget_choice=choice,
            execution=None,
        )

    selected = choice.selected
    profile = _profile_for_choice(choice)
    if selected is None:
        raise ValueError("executable budget choice has no selected provider profile")
    execution = provider.review(
        request,
        profile=profile,
        reserved_cost_microusd=selected.reservation_microusd,
    )
    validate_soft_review_provider_result(
        request=request,
        result=execution,
        expected_profile=profile,
        expected_reservation_microusd=selected.reservation_microusd,
    )
    return SoftReviewDispatchResult(
        status="EXECUTED",
        budget_choice=choice,
        execution=execution,
    )


def _profile_for_choice(choice: BudgetChoice) -> SoftReviewExecutionProfile:
    if choice.selected is None:
        raise ValueError("blocked budget choice has no executable provider profile")
    profiles = {
        "primary": I6_SOFT_REVIEW_PRIMARY_PROFILE,
        "lower_cost_fallback": I6_SOFT_REVIEW_FALLBACK_PROFILE,
    }
    try:
        return profiles[choice.selected.name]
    except KeyError:
        raise ValueError("budget choice has an unknown soft-review profile") from None
