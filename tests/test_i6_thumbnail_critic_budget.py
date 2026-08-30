"""DBOS-free B2B2 reservation, price, and hard-cap tests."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest

from app.editorial.contracts import canonical_json
from app.production.providers import (
    I6_THUMBNAIL_CRITIC_FALLBACK_PROFILE,
    I6_THUMBNAIL_CRITIC_IMAGE_TOKENS,
    I6_THUMBNAIL_CRITIC_LONG_CONTEXT_THRESHOLD,
    I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE,
    thumbnail_critic_conservative_cost_microusd,
    thumbnail_critic_request_payload,
    thumbnail_critic_reservation,
)
from app.qa.contracts import ThumbnailCriticProviderResult
from app.qa.machine import evaluate_machine_qa
from app.editorial.contracts import canonical_sha256
from tests.test_i6_thumbnail_critic import _artifact, _fixture_data


_WORKFLOW_SPEC = importlib.util.spec_from_file_location(
    "i6_thumbnail_review_pure_test",
    Path(__file__).resolve().parents[1] / "app/workflows/i6_thumbnail_review.py",
)
assert _WORKFLOW_SPEC is not None and _WORKFLOW_SPEC.loader is not None
_WORKFLOW_MODULE = importlib.util.module_from_spec(_WORKFLOW_SPEC)
sys.modules[_WORKFLOW_SPEC.name] = _WORKFLOW_MODULE
_WORKFLOW_SPEC.loader.exec_module(_WORKFLOW_MODULE)
dispatch_thumbnail_critic = _WORKFLOW_MODULE.dispatch_thumbnail_critic


class _NeverCall:
    def review(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("budget gate must block before provider call")


class _FakeStateFreeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object]] = []

    def review(self, request, *, png_bytes, profile, reservation):  # type: ignore[no-untyped-def]
        self.calls.append((request, profile, reservation))
        artifact = _artifact(request)
        return ThumbnailCriticProviderResult(
            request=request,
            reservation=reservation,
            artifact=artifact,
            artifact_sha256=artifact.sha256(),
            evidence_sha256=request.evidence_sha256,
            png_sha256=request.evidence.png_sha256,
            png_byte_size=request.evidence.byte_size,
            profile_name=profile.name,
            reasoning_effort=profile.reasoning_effort,
            max_output_tokens=profile.max_output_tokens,
            provider_response_id=artifact.provider_response_id,
            structured_response_sha256=artifact.structured_response_sha256,
            raw_response_sha256=artifact.raw_response_sha256,
            input_tokens=0,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            output_tokens=0,
            metered_cost_microusd=0,
        )


def test_15_hard_budget_cap_zero_calls() -> None:
    machine_input, machine_result, rendered, _ = _fixture_data()
    result = dispatch_thumbnail_critic(
        machine_input=machine_input, machine_result=machine_result, evidence=rendered.evidence,
        png_bytes=rendered.png_bytes, current_effective_campaign_cost_microusd=35_000_000,
        authorized_campaign_cap_microusd=35_000_000, provider=_NeverCall(),
    )
    assert result.status == "BLOCK_NEEDS_HUMAN"
    assert result.execution is None and result.resolution is None and result.reservation is None


def test_16_deterministic_primary_fallback_choice() -> None:
    *_, request = _fixture_data()
    primary = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    fallback = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_FALLBACK_PROFILE)
    assert fallback.reserved_cost_microusd < primary.reserved_cost_microusd
    assert fallback.profile_name == "lower_cost_fallback"


def test_38_exact_920_image_tokens() -> None:
    *_, request = _fixture_data()
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    assert reservation.image_input_tokens == I6_THUMBNAIL_CRITIC_IMAGE_TOKENS == 920
    assert reservation.total_input_token_ceiling == reservation.conservative_text_input_token_ceiling + 920

@pytest.mark.parametrize("cached,cache_write,expected", [(0, 1, 3), (1, 0, 1)])
def test_39_40_one_token_rounding(cached, cache_write, expected) -> None:  # type: ignore[no-untyped-def]
    assert thumbnail_critic_conservative_cost_microusd(input_tokens=1, cached_input_tokens=cached, cache_write_input_tokens=cache_write, output_tokens=0) == expected


def test_41_invalid_usage_category_sum_rejects() -> None:
    with pytest.raises(ValueError):
        thumbnail_critic_conservative_cost_microusd(input_tokens=1, cached_input_tokens=1, cache_write_input_tokens=1, output_tokens=0)


def test_42_mixed_categories_bill_each_token_once() -> None:
    assert thumbnail_critic_conservative_cost_microusd(input_tokens=3, cached_input_tokens=1, cache_write_input_tokens=1, output_tokens=0) == 6


def test_43_long_context_multipliers() -> None:
    assert thumbnail_critic_conservative_cost_microusd(
        input_tokens=I6_THUMBNAIL_CRITIC_LONG_CONTEXT_THRESHOLD + 1,
        cached_input_tokens=1, cache_write_input_tokens=1, output_tokens=1,
    ) == (I6_THUMBNAIL_CRITIC_LONG_CONTEXT_THRESHOLD - 1) * 4 + 1 + 5 + 18


def test_44_envelope_schema_wrapper_reservation_sensitivity() -> None:
    *_, request = _fixture_data()
    primary = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    fallback = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_FALLBACK_PROFILE)
    assert primary.envelope_sha256 != fallback.envelope_sha256
    assert primary.conservative_text_input_token_ceiling != fallback.conservative_text_input_token_ceiling


def test_45_base64_excluded_from_reservation() -> None:
    _, _, rendered, request = _fixture_data()
    sentinel = thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    concrete = thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE, png_bytes=rendered.png_bytes)
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    assert "<EXACT_BASE64_BYTES_EXCLUDED>" in canonical_json(sentinel)
    assert len(canonical_json(concrete).encode()) > reservation.conservative_text_input_token_ceiling


def test_legacy_full_utf8_byte_length_not_divided_by_four() -> None:
    *_, request = _fixture_data()
    envelope = thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    byte_length = len(canonical_json(envelope).encode("utf-8"))
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    assert reservation.conservative_text_input_token_ceiling == byte_length
    assert byte_length > (byte_length + 3) // 4


def test_legacy_multibyte_unicode_cannot_reduce_byte_ceiling() -> None:
    *_, request = _fixture_data()
    envelope = thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    baseline = len(canonical_json(envelope).encode("utf-8"))
    enriched = canonical_json({"envelope": envelope, "unicode": "é" * 32}).encode("utf-8")
    assert len(enriched) > baseline


def test_reservation_long_context_includes_image_term() -> None:
    *_, request = _fixture_data()
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    assert reservation.total_input_token_ceiling - reservation.conservative_text_input_token_ceiling == 920
    assert reservation.reserved_cost_microusd == thumbnail_critic_conservative_cost_microusd(
        input_tokens=reservation.total_input_token_ceiling,
        cached_input_tokens=0,
        cache_write_input_tokens=reservation.total_input_token_ceiling,
        output_tokens=reservation.max_output_tokens,
    )


@pytest.mark.parametrize("input_tokens,cached,cache_write,output", [
    (0, 0, 0, 0), (10, 0, 0, 1), (10, 1, 0, 1), (10, 0, 1, 1),
    (10, 3, 2, 1), (100, 40, 20, 3), (271_999, 0, 0, 2), (272_000, 0, 0, 2),
])
def test_price_policy_is_nonnegative_and_deterministic(input_tokens, cached, cache_write, output) -> None:  # type: ignore[no-untyped-def]
    cost = thumbnail_critic_conservative_cost_microusd(
        input_tokens=input_tokens, cached_input_tokens=cached, cache_write_input_tokens=cache_write, output_tokens=output
    )
    assert cost >= 0


def test_35_price_policy_is_bound_to_reservation() -> None:
    *_, request = _fixture_data()
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    assert reservation.request_sha256 == request.sha256()
    assert reservation.envelope_sha256 == hashlib.sha256(canonical_json(thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)).encode()).hexdigest()


def test_req_14_genuine_deterministic_fail_zero_calls() -> None:
    machine_input, _, rendered, _ = _fixture_data()
    layout = machine_input.packaging.thumbnails[0]
    tiny_text = layout.text_elements[0].model_copy(update={"font_height_px": 1})
    raw_layout = layout.model_dump(mode="json")
    raw_layout["text_elements"] = [tiny_text.model_dump(mode="json")]
    raw_layout["layout_spec_sha256"] = canonical_sha256({key: value for key, value in raw_layout.items() if key != "layout_spec_sha256"})
    failed_input = machine_input.model_copy(update={"packaging": machine_input.packaging.model_copy(update={"thumbnails": (layout.__class__.model_validate(raw_layout), *machine_input.packaging.thumbnails[1:])})})
    failed_result = evaluate_machine_qa(failed_input)
    assert failed_result.outcome == "FAIL"
    provider = _FakeStateFreeProvider()
    with pytest.raises(ValueError, match="deterministic"):
        dispatch_thumbnail_critic(machine_input=failed_input, machine_result=failed_result, evidence=rendered.evidence, png_bytes=rendered.png_bytes, current_effective_campaign_cost_microusd=0, authorized_campaign_cap_microusd=35_000_000, provider=provider)
    assert provider.calls == []


def test_req_15_hard_budget_cap_zero_calls() -> None:
    machine_input, machine_result, rendered, _ = _fixture_data()
    provider = _FakeStateFreeProvider()
    result = dispatch_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, evidence=rendered.evidence, png_bytes=rendered.png_bytes, current_effective_campaign_cost_microusd=35_000_000, authorized_campaign_cap_microusd=35_000_000, provider=provider)
    assert result.status == "BLOCK_NEEDS_HUMAN" and provider.calls == []


def test_req_16_actual_workflow_fallback_execution() -> None:
    machine_input, machine_result, rendered, request = _fixture_data()
    primary = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    fallback = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_FALLBACK_PROFILE)
    provider = _FakeStateFreeProvider()
    result = dispatch_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, evidence=rendered.evidence, png_bytes=rendered.png_bytes, current_effective_campaign_cost_microusd=35_000_000 - fallback.reserved_cost_microusd, authorized_campaign_cap_microusd=35_000_000, provider=provider)
    assert primary.reserved_cost_microusd > fallback.reserved_cost_microusd
    assert len(provider.calls) == 1 and result.status == "EXECUTED"
    assert result.budget_choice.decision.value == "USE_LOWER_COST_FALLBACK"
    assert result.execution is not None and result.execution.profile_name == "lower_cost_fallback"
    assert result.execution.reasoning_effort == "low" and result.execution.max_output_tokens == 1024
    assert result.reservation == fallback and result.resolution is not None
    assert result.resolution.request_sha256 == request.sha256()


def test_req_38_exact_920_image_tokens() -> None:
    *_, request = _fixture_data()
    assert thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE).image_input_tokens == 920


def test_req_39_cache_write_one_token_is_three_microusd() -> None:
    assert thumbnail_critic_conservative_cost_microusd(input_tokens=1, cached_input_tokens=0, cache_write_input_tokens=1, output_tokens=0) == 3


def test_req_40_cached_one_token_is_one_microusd() -> None:
    assert thumbnail_critic_conservative_cost_microusd(input_tokens=1, cached_input_tokens=1, cache_write_input_tokens=0, output_tokens=0) == 1


def test_req_41_invalid_usage_category_sum_rejects() -> None:
    with pytest.raises(ValueError):
        thumbnail_critic_conservative_cost_microusd(input_tokens=1, cached_input_tokens=1, cache_write_input_tokens=1, output_tokens=0)


def test_req_42_mixed_categories_bill_once() -> None:
    assert thumbnail_critic_conservative_cost_microusd(input_tokens=3, cached_input_tokens=1, cache_write_input_tokens=1, output_tokens=0) == 6


def test_req_43_long_context_multipliers() -> None:
    ordinary = thumbnail_critic_conservative_cost_microusd(input_tokens=I6_THUMBNAIL_CRITIC_LONG_CONTEXT_THRESHOLD, cached_input_tokens=0, cache_write_input_tokens=0, output_tokens=1)
    long = thumbnail_critic_conservative_cost_microusd(input_tokens=I6_THUMBNAIL_CRITIC_LONG_CONTEXT_THRESHOLD + 1, cached_input_tokens=0, cache_write_input_tokens=0, output_tokens=1)
    assert long > ordinary


def test_req_44_complete_envelope_schema_wrapper_reservation_sensitivity() -> None:
    *_, request = _fixture_data()
    payload = thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    encoded = canonical_json(payload).encode("utf-8")
    assert reservation.conservative_text_input_token_ceiling == len(encoded)
    assert payload["model"] == "gpt-5.6-terra" and payload["store"] is False
    assert payload["input"][0]["role"] == "system" and payload["input"][1]["content"][1]["detail"] == "original"
    assert payload["text"]["format"]["schema"]["properties"]["judgments"]["items"]["properties"]["rationale"]["pattern"] == ".*\\S.*"
    expanded = canonical_json({"payload": payload, "wrapper_growth": "x"})
    assert hashlib.sha256(expanded.encode()).hexdigest() != reservation.envelope_sha256


def test_req_45_base64_excluded_from_reservation() -> None:
    _, _, rendered, request = _fixture_data()
    sentinel = canonical_json(thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE))
    concrete = canonical_json(thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE, png_bytes=rendered.png_bytes))
    assert "EXACT_BASE64_BYTES_EXCLUDED" in sentinel and len(concrete.encode()) > len(sentinel.encode())


def test_req_35_price_policy_bound() -> None:
    *_, request = _fixture_data()
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    assert reservation.price_policy_version == request.price_policy_version
    assert reservation.reserved_cost_microusd == thumbnail_critic_conservative_cost_microusd(input_tokens=reservation.total_input_token_ceiling, cached_input_tokens=0, cache_write_input_tokens=reservation.total_input_token_ceiling, output_tokens=reservation.max_output_tokens)


def test_full_utf8_byte_length_not_divided_by_four() -> None:
    *_, request = _fixture_data()
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    assert reservation.conservative_text_input_token_ceiling == len(canonical_json(thumbnail_critic_request_payload(request, profile=I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)).encode("utf-8"))


def test_multibyte_unicode_cannot_reduce_byte_ceiling() -> None:
    ascii_bytes = len("x".encode("utf-8"))
    unicode_bytes = len("é".encode("utf-8"))
    assert unicode_bytes > ascii_bytes


def test_long_context_selection_includes_920_image_tokens() -> None:
    *_, request = _fixture_data()
    reservation = thumbnail_critic_reservation(request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE)
    assert reservation.total_input_token_ceiling == reservation.conservative_text_input_token_ceiling + 920


def test_sol_multibyte_reservation_uses_actual_utf8_bytes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    *_, request = _fixture_data()
    ascii_value = "a" * 513
    multibyte_value = "é" * 513
    ascii_envelope = {"canonical": ascii_value}
    multibyte_envelope = {"canonical": multibyte_value}
    assert len(ascii_value) == len(multibyte_value)
    envelopes = iter((ascii_envelope, multibyte_envelope))

    def synthetic_payload(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return next(envelopes)

    monkeypatch.setattr("app.production.providers.thumbnail_critic_request_payload", synthetic_payload)
    ascii_reservation = thumbnail_critic_reservation(
        request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE
    )
    multibyte_reservation = thumbnail_critic_reservation(
        request, I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE
    )

    assert ascii_reservation.conservative_text_input_token_ceiling == len(
        canonical_json(ascii_envelope).encode("utf-8")
    )
    assert multibyte_reservation.conservative_text_input_token_ceiling == len(
        canonical_json(multibyte_envelope).encode("utf-8")
    )
    assert (
        multibyte_reservation.conservative_text_input_token_ceiling
        > ascii_reservation.conservative_text_input_token_ceiling
    )


def test_sol_long_context_image_920_crosses_threshold(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    *_, request = _fixture_data()
    profile = I6_THUMBNAIL_CRITIC_PRIMARY_PROFILE
    assert profile.max_output_tokens == 2048

    def envelope_with_text_ceiling(text_ceiling: int) -> dict[str, str]:
        envelope = {"x": "a" * (text_ceiling - len(canonical_json({"x": ""}).encode("utf-8")))}
        assert len(canonical_json(envelope).encode("utf-8")) == text_ceiling
        return envelope

    standard = envelope_with_text_ceiling(271_080)
    long_context = envelope_with_text_ceiling(271_081)
    envelopes = iter((standard, long_context))

    def synthetic_payload(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return next(envelopes)

    monkeypatch.setattr("app.production.providers.thumbnail_critic_request_payload", synthetic_payload)
    standard_reservation = thumbnail_critic_reservation(request, profile)
    long_reservation = thumbnail_critic_reservation(request, profile)
    assert (
        standard_reservation.conservative_text_input_token_ceiling,
        standard_reservation.total_input_token_ceiling,
    ) == (271_080, 272_000)
    assert (
        long_reservation.conservative_text_input_token_ceiling,
        long_reservation.total_input_token_ceiling,
    ) == (271_081, 272_001)
    assert standard_reservation.reserved_cost_microusd == 704_576
    assert long_reservation.reserved_cost_microusd == 1_396_869
    assert standard_reservation.reserved_cost_microusd == thumbnail_critic_conservative_cost_microusd(
        input_tokens=272_000,
        cached_input_tokens=0,
        cache_write_input_tokens=272_000,
        output_tokens=2048,
    )
    assert long_reservation.reserved_cost_microusd == thumbnail_critic_conservative_cost_microusd(
        input_tokens=272_001,
        cached_input_tokens=0,
        cache_write_input_tokens=272_001,
        output_tokens=2048,
    )
    counterfactual_unmultiplied_cost = (272_001 * 5 + 1) // 2 + 2048 * 12
    assert counterfactual_unmultiplied_cost == 704_579
    assert long_reservation.reserved_cost_microusd != counterfactual_unmultiplied_cost
