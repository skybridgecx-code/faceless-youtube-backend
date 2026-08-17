from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest

from app.editorial.contracts import canonical_json
from app.production.budget import BudgetDecision, load_campaign_budget_policy
from app.production.providers import (
    I6_SOFT_REVIEW_FALLBACK_PROFILE,
    I6_SOFT_REVIEW_PRIMARY_PROFILE,
    I6_SOFT_REVIEW_PROMPT_TEMPLATE_VERSION,
    I6_SOFT_REVIEW_RESERVATION_MARGIN_PERCENT,
    SoftReviewProviderResult,
    ProviderError,
    build_openai_soft_review_payload,
    soft_review_conservative_cost_microusd,
    soft_review_reservation_microusd,
)
from app.qa import (
    CriticArtifact,
    CriticJudgment,
    CriticResponse,
    build_soft_review_request,
    evaluate_machine_qa,
)
from tests.test_i6_machine_qa import _input


_MODULE_SPEC = importlib.util.spec_from_file_location(
    "i6_soft_review_under_test", Path("app/workflows/i6_soft_review.py")
)
assert _MODULE_SPEC is not None and _MODULE_SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_MODULE_SPEC)
sys.modules[_MODULE_SPEC.name] = _MODULE
_MODULE_SPEC.loader.exec_module(_MODULE)
dispatch_soft_review = _MODULE.dispatch_soft_review


class _RecordingProvider:
    def __init__(self, *, excessive_usage: bool = False) -> None:
        self.calls: list[tuple[object, object, int]] = []
        self.excessive_usage = excessive_usage

    def review(self, request, *, profile, reserved_cost_microusd: int) -> SoftReviewProviderResult:
        self.calls.append((request, profile, reserved_cost_microusd))
        judgments = tuple(
            CriticJudgment(
                target_id=target.target_id,
                outcome="PASS",
                rationale="Bounded editorial judgment.",
                observations=("Only canonical supplied context was used.",),
            )
            for target in request.targets
        )
        response = CriticResponse(judgments=judgments)
        artifact = CriticArtifact(
            request_sha256=request.sha256(),
            machine_input_sha256=request.machine_input_sha256,
            machine_result_sha256=request.machine_result_sha256,
            provider="openai",
            model="gpt-5.6-terra",
            prompt_template_version=I6_SOFT_REVIEW_PROMPT_TEMPLATE_VERSION,
            provider_response_sha256=response.sha256(),
            provider_response=response,
            judgments=judgments,
        )
        input_tokens, output_tokens = (272_001, 0) if self.excessive_usage else (10, 20)
        return SoftReviewProviderResult(
            artifact=artifact,
            provider_response_id="resp_budget_test",
            actual_model="gpt-5.6-terra",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            metered_cost_microusd=soft_review_conservative_cost_microusd(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            reserved_cost_microusd=reserved_cost_microusd,
            selected_profile=profile,
            raw_response_sha256=hashlib.sha256(b"budget-test-response").hexdigest(),
        )


def _request():
    machine_input = _input()
    return build_soft_review_request(machine_input, evaluate_machine_qa(machine_input))


def test_primary_profile_is_selected_below_soft_warning() -> None:
    request = _request()
    provider = _RecordingProvider()
    policy = load_campaign_budget_policy()

    result = dispatch_soft_review(
        request=request,
        current_effective_campaign_cost_microusd=0,
        authorized_campaign_cap_microusd=policy.limits_microusd.default_hard_cap,
        provider=provider,
    )

    assert result.status == "EXECUTED"
    assert result.budget_choice.decision == BudgetDecision.ALLOW_PRIMARY
    assert result.execution is not None
    assert result.execution.selected_profile == I6_SOFT_REVIEW_PRIMARY_PROFILE
    assert len(provider.calls) == 1


def test_lower_cost_fallback_is_selected_at_soft_warning() -> None:
    request = _request()
    provider = _RecordingProvider()
    policy = load_campaign_budget_policy()
    fallback = soft_review_reservation_microusd(request, I6_SOFT_REVIEW_FALLBACK_PROFILE)

    result = dispatch_soft_review(
        request=request,
        current_effective_campaign_cost_microusd=policy.limits_microusd.soft_warning - fallback,
        authorized_campaign_cap_microusd=policy.limits_microusd.default_hard_cap,
        provider=provider,
    )

    assert result.status == "EXECUTED"
    assert result.budget_choice.decision == BudgetDecision.USE_LOWER_COST_FALLBACK
    assert result.execution is not None
    assert result.execution.selected_profile == I6_SOFT_REVIEW_FALLBACK_PROFILE
    assert len(provider.calls) == 1


def test_hard_cap_breach_blocks_provider_without_invocation() -> None:
    request = _request()
    provider = _RecordingProvider()
    policy = load_campaign_budget_policy()
    fallback = soft_review_reservation_microusd(request, I6_SOFT_REVIEW_FALLBACK_PROFILE)

    result = dispatch_soft_review(
        request=request,
        current_effective_campaign_cost_microusd=policy.limits_microusd.default_hard_cap - fallback + 1,
        authorized_campaign_cap_microusd=policy.limits_microusd.default_hard_cap,
        provider=provider,
    )

    assert result.status == "BLOCK_NEEDS_HUMAN"
    assert result.budget_choice.decision == BudgetDecision.BLOCK_NEEDS_HUMAN
    assert result.execution is None
    assert provider.calls == []


def test_fallback_over_authorized_cap_blocks_provider_without_invocation() -> None:
    request = _request()
    provider = _RecordingProvider()
    fallback = soft_review_reservation_microusd(request, I6_SOFT_REVIEW_FALLBACK_PROFILE)

    result = dispatch_soft_review(
        request=request,
        current_effective_campaign_cost_microusd=1_000_000,
        authorized_campaign_cap_microusd=1_000_000 + fallback - 1,
        provider=provider,
    )

    assert result.status == "BLOCK_NEEDS_HUMAN"
    assert result.execution is None
    assert provider.calls == []


def test_dispatch_fails_closed_when_provider_usage_exceeds_reservation() -> None:
    request = _request()
    provider = _RecordingProvider(excessive_usage=True)
    policy = load_campaign_budget_policy()

    with pytest.raises(ProviderError, match="exceeded its preflight reservation"):
        dispatch_soft_review(
            request=request,
            current_effective_campaign_cost_microusd=0,
            authorized_campaign_cap_microusd=policy.limits_microusd.default_hard_cap,
            provider=provider,
        )

    assert len(provider.calls) == 1


def test_reservation_covers_complete_payload_footprint_with_safety_margin() -> None:
    request = _request()
    primary = soft_review_reservation_microusd(request, I6_SOFT_REVIEW_PRIMARY_PROFILE)
    fallback = soft_review_reservation_microusd(request, I6_SOFT_REVIEW_FALLBACK_PROFILE)

    for profile, reservation in (
        (I6_SOFT_REVIEW_PRIMARY_PROFILE, primary),
        (I6_SOFT_REVIEW_FALLBACK_PROFILE, fallback),
    ):
        serialized_request_bytes = len(
            canonical_json(build_openai_soft_review_payload(request, profile)).encode("utf-8")
        )
        base_cost = soft_review_conservative_cost_microusd(
            input_tokens=serialized_request_bytes,
            output_tokens=profile.max_output_tokens,
        )
        assert reservation == (
            base_cost * I6_SOFT_REVIEW_RESERVATION_MARGIN_PERCENT + 99
        ) // 100
    assert fallback < primary
