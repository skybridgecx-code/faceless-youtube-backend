"""Pure I6-B2B2 request, budget, provider, revalidation, and resolution flow."""

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
    I6_THUMBNAIL_CRITIC_FALLBACK_PROFILE,
    I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE,
    ThumbnailCriticExecutionProfile,
    ThumbnailCriticProviderResult,
    thumbnail_critic_reservation,
    validate_thumbnail_critic_provider_result,
)
from app.qa.contracts import (
    MachineQAInput,
    MachineQAResult,
    RenderedThumbnailEvidence,
    ThumbnailCriticRequest,
    ThumbnailCriticReservation,
    ThumbnailCriticResolution,
)
from app.qa.thumbnail_quality import build_thumbnail_critic_request, resolve_thumbnail_critic


class I6ThumbnailCriticProvider(Protocol):
    """State-free provider boundary with one preflighted immutable request."""

    def review(
        self,
        request: ThumbnailCriticRequest,
        *,
        png_bytes: bytes,
        profile: ThumbnailCriticExecutionProfile,
        reservation: ThumbnailCriticReservation,
    ) -> ThumbnailCriticProviderResult: ...


@dataclass(frozen=True, slots=True)
class ThumbnailCriticDispatchResult:
    status: Literal["EXECUTED", "BLOCK_NEEDS_HUMAN"]
    request: ThumbnailCriticRequest
    budget_choice: BudgetChoice
    reservation: ThumbnailCriticReservation | None
    execution: ThumbnailCriticProviderResult | None
    resolution: ThumbnailCriticResolution | None

    def __post_init__(self) -> None:
        executed = self.execution is not None and self.resolution is not None and self.reservation is not None
        if (self.status == "EXECUTED") != executed:
            raise ValueError("thumbnail critic dispatch fields conflict with status")


def dispatch_thumbnail_critic(
    *,
    machine_input: MachineQAInput,
    machine_result: MachineQAResult,
    evidence: RenderedThumbnailEvidence,
    png_bytes: bytes,
    current_effective_campaign_cost_microusd: int,
    authorized_campaign_cap_microusd: int,
    provider: I6ThumbnailCriticProvider,
    budget_policy: CampaignBudgetPolicy | None = None,
) -> ThumbnailCriticDispatchResult:
    """Perform pure B2B2 orchestration only; no persistence or stage advancement."""

    request = build_thumbnail_critic_request(machine_input, machine_result, evidence)
    primary = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    fallback = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_FALLBACK_PROFILE)
    choice = choose_budget_option(
        current_cost_microusd=current_effective_campaign_cost_microusd,
        authorized_cap_microusd=authorized_campaign_cap_microusd,
        primary=BudgetOption("primary", primary.reserved_cost_microusd),
        fallbacks=(BudgetOption("lower_cost_fallback", fallback.reserved_cost_microusd),),
        policy=budget_policy,
    )
    if choice.decision == BudgetDecision.BLOCK_NEEDS_HUMAN:
        return ThumbnailCriticDispatchResult("BLOCK_NEEDS_HUMAN", request, choice, None, None, None)
    if choice.selected is None:
        raise ValueError("executable thumbnail critic budget choice has no selected option")
    profiles = {
        "primary": (I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE, primary),
        "lower_cost_fallback": (I6_THUMBNAIL_CRITIC_FALLBACK_PROFILE, fallback),
    }
    selected = profiles.get(choice.selected.name)
    if selected is None or choice.selected.reservation_microusd != selected[1].reserved_cost_microusd:
        raise ValueError("thumbnail critic budget choice has unknown or stale profile")
    profile, reservation = selected
    # Reconstruct all external frozen B2B1 values a second time at dispatch.
    # A model_copy mutation after preflight must not reach the provider boundary.
    if build_thumbnail_critic_request(machine_input, machine_result, evidence) != request:
        raise ValueError("thumbnail critic request changed between preflight and dispatch")
    execution = provider.review(request, png_bytes=png_bytes, profile=profile, reservation=reservation)
    validate_thumbnail_critic_provider_result(
        request=request,
        result=execution,
        expected_profile=profile,
        expected_reservation=reservation,
    )
    resolution = resolve_thumbnail_critic(
        machine_input=machine_input,
        machine_result=machine_result,
        request=request,
        artifact=execution.artifact,
    )
    return ThumbnailCriticDispatchResult("EXECUTED", request, choice, reservation, execution, resolution)
