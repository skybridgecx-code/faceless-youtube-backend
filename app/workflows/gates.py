from __future__ import annotations

from .contracts import (
    I3_PROVIDER_STAGES,
    GateResult,
    ProviderResult,
    StageRequest,
    canonical_json,
    sha256_text,
)


def evaluate_gate(
    request: StageRequest,
    provider_result: ProviderResult | None,
    *,
    hard_failures: tuple[str, ...] = (),
) -> GateResult:
    """Pure I3 gate evaluation. Hard failures always dominate provider validity."""

    if request.stage == "human_approval":
        return GateResult(
            outcome="NEEDS_HUMAN",
            reasons=("explicit_human_approval_required",),
            input_hash=request.input_hash,
            output_hash=None,
            policy_version=request.policy_version,
        )

    if request.stage == "release":
        return GateResult(
            outcome="FAIL",
            reasons=("release_not_implemented_in_i3",),
            input_hash=request.input_hash,
            output_hash=None,
            policy_version=request.policy_version,
        )

    failures = tuple(reason.strip() for reason in hard_failures if reason.strip())
    if failures:
        return GateResult(
            outcome="FAIL",
            reasons=failures,
            input_hash=request.input_hash,
            output_hash=provider_result.output_hash if provider_result else None,
            policy_version=request.policy_version,
        )

    structural_failures: list[str] = []
    if request.stage not in I3_PROVIDER_STAGES:
        structural_failures.append("stage_not_supported_by_i3_stub")
    if provider_result is None:
        structural_failures.append("provider_output_required")
    else:
        if provider_result.input_hash != request.input_hash:
            structural_failures.append("provider_input_hash_mismatch")
        if provider_result.provider != "stub":
            structural_failures.append("unexpected_provider")
        if provider_result.model != "i3-deterministic-v1":
            structural_failures.append("unexpected_provider_model")
        if provider_result.cost_microunits != 0:
            structural_failures.append("stub_cost_must_be_zero")
        if sha256_text(provider_result.output_json) != provider_result.output_hash:
            structural_failures.append("provider_output_hash_mismatch")
        try:
            expected_usage = canonical_json({"metered_cost_microunits": 0})
        except (TypeError, ValueError):  # pragma: no cover - constant serialization
            expected_usage = ""
        if provider_result.usage_json != expected_usage:
            structural_failures.append("stub_usage_must_record_zero_cost")

    if structural_failures:
        return GateResult(
            outcome="FAIL",
            reasons=tuple(structural_failures),
            input_hash=request.input_hash,
            output_hash=provider_result.output_hash if provider_result else None,
            policy_version=request.policy_version,
        )

    assert provider_result is not None
    return GateResult(
        outcome="PASS",
        reasons=("deterministic_stub_output_valid",),
        input_hash=request.input_hash,
        output_hash=provider_result.output_hash,
        policy_version=request.policy_version,
    )
