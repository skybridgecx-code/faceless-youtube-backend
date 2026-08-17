from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.production.providers import (
    I6_SOFT_REVIEW_FALLBACK_PROFILE,
    I6_SOFT_REVIEW_CACHE_WRITE_INPUT_MULTIPLIER_DENOMINATOR,
    I6_SOFT_REVIEW_CACHE_WRITE_INPUT_MULTIPLIER_NUMERATOR,
    I6_SOFT_REVIEW_INPUT_TOKEN_MICROUSD_DENOMINATOR,
    I6_SOFT_REVIEW_INPUT_TOKEN_MICROUSD_NUMERATOR,
    I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_MULTIPLIER_DENOMINATOR,
    I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_MULTIPLIER_NUMERATOR,
    I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_TOKEN_THRESHOLD,
    I6_SOFT_REVIEW_LONG_CONTEXT_OUTPUT_MULTIPLIER_DENOMINATOR,
    I6_SOFT_REVIEW_LONG_CONTEXT_OUTPUT_MULTIPLIER_NUMERATOR,
    I6_SOFT_REVIEW_MODEL,
    I6_SOFT_REVIEW_OUTPUT_TOKEN_MICROUSD_DENOMINATOR,
    I6_SOFT_REVIEW_OUTPUT_TOKEN_MICROUSD_NUMERATOR,
    I6_SOFT_REVIEW_PRIMARY_PROFILE,
    I6_SOFT_REVIEW_PROMPT_TEMPLATE_VERSION,
    OpenAISoftReviewProvider,
    ProviderError,
    SoftReviewProviderResult,
    soft_review_conservative_cost_microusd,
    soft_review_reservation_microusd,
    validate_soft_review_provider_result,
)
from app.qa import build_soft_review_request, evaluate_machine_qa, resolve_soft_review
from tests.test_i6_machine_qa import _input


class _FakeClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.response


def _request_and_result():
    machine_input = _input()
    machine_result = evaluate_machine_qa(machine_input)
    return machine_input, machine_result, build_soft_review_request(machine_input, machine_result)


def _response_body(request, *, judgments: list[dict[str, object]] | None = None, **updates: object) -> dict[str, object]:
    judgments = judgments or [
        {
            "target_id": target.target_id,
            "outcome": "PASS",
            "rationale": "The supplied opening satisfies this bounded editorial criterion.",
            "observations": ["The conclusion follows only from the supplied canonical context."],
        }
        for target in request.targets
    ]
    body: dict[str, object] = {
        "id": "resp_i6_soft_review_001",
        "model": I6_SOFT_REVIEW_MODEL,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps({"judgments": judgments})}],
            }
        ],
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }
    body.update(updates)
    return body


def _provider(response: httpx.Response) -> tuple[OpenAISoftReviewProvider, _FakeClient]:
    client = _FakeClient(response)
    return OpenAISoftReviewProvider(api_key="secret-test-key", client=client), client


def _primary_reservation(request) -> int:
    return soft_review_reservation_microusd(request, I6_SOFT_REVIEW_PRIMARY_PROFILE)


@pytest.mark.parametrize(
    ("profile", "effort", "max_output_tokens"),
    (
        (I6_SOFT_REVIEW_PRIMARY_PROFILE, "medium", 4_096),
        (I6_SOFT_REVIEW_FALLBACK_PROFILE, "low", 2_048),
    ),
)
def test_responses_body_is_text_only_strict_and_profile_bounded(
    profile, effort: str, max_output_tokens: int
) -> None:
    _machine_input, _machine_result, request = _request_and_result()
    body = _response_body(request)
    provider, client = _provider(httpx.Response(200, json=body))
    reservation = soft_review_reservation_microusd(request, profile)

    provider.review(request, profile=profile, reserved_cost_microusd=reservation)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "https://api.openai.com/v1/responses"
    payload = call["json"]
    assert isinstance(payload, dict)
    assert payload["model"] == I6_SOFT_REVIEW_MODEL
    assert payload["store"] is False
    assert "tools" not in payload
    assert payload["reasoning"] == {"effort": effort}
    assert payload["max_output_tokens"] == max_output_tokens
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert len(payload["input"]) == 2
    assert all(part["content"][0]["type"] == "input_text" for part in payload["input"])
    assert json.loads(payload["input"][1]["content"][0]["text"]) == request.model_dump(mode="json")


def test_valid_structured_response_builds_canonical_artifact_and_round_trips_b1() -> None:
    machine_input, machine_result, request = _request_and_result()
    body = _response_body(request)
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    provider, _client = _provider(httpx.Response(200, content=raw))
    reservation = soft_review_reservation_microusd(request, I6_SOFT_REVIEW_PRIMARY_PROFILE)

    execution = provider.review(
        request,
        profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
        reserved_cost_microusd=reservation,
    )

    assert execution.artifact.request_sha256 == request.sha256()
    assert execution.artifact.machine_input_sha256 == request.machine_input_sha256
    assert execution.artifact.machine_result_sha256 == request.machine_result_sha256
    assert execution.artifact.provider == "openai"
    assert execution.artifact.model == I6_SOFT_REVIEW_MODEL
    assert execution.artifact.prompt_template_version == I6_SOFT_REVIEW_PROMPT_TEMPLATE_VERSION
    assert execution.provider_response_id == body["id"]
    assert execution.actual_model == I6_SOFT_REVIEW_MODEL
    assert execution.raw_response_sha256 == hashlib.sha256(raw).hexdigest()
    assert execution.input_tokens == 123
    assert execution.output_tokens == 45
    assert execution.metered_cost_microusd == soft_review_conservative_cost_microusd(
        input_tokens=123,
        output_tokens=45,
    )
    assert execution.metered_cost_microusd >= 0
    resolved = resolve_soft_review(machine_input, machine_result, request, execution.artifact)
    assert {item.target_id for item in resolved.resolved_soft_judgments} == {
        target.target_id for target in request.targets
    }
    assert len(resolved.resolved_soft_judgments) == 4


@pytest.mark.parametrize("case", ("missing", "duplicate", "unknown"))
def test_malformed_target_coverage_fails_closed(case: str) -> None:
    _machine_input, _machine_result, request = _request_and_result()
    body = _response_body(request)
    structured = json.loads(body["output"][0]["content"][0]["text"])
    judgments = structured["judgments"]
    if case == "missing":
        judgments = judgments[:-1]
        error = "did not cover every"
    elif case == "duplicate":
        judgments = [judgments[0], judgments[0], *judgments[2:]]
        error = "duplicate"
    else:
        judgments[0]["target_id"] = "f" * 64
        error = "unknown"
    body["output"][0]["content"][0]["text"] = json.dumps({"judgments": judgments})
    provider, _client = _provider(httpx.Response(200, json=body))

    with pytest.raises(ProviderError, match=error):
        provider.review(
            request,
            profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
            reserved_cost_microusd=_primary_reservation(request),
        )


@pytest.mark.parametrize(
    "body_update",
    (
        {"status": "incomplete"},
        {"model": "unexpected-model"},
        {"id": ""},
        {"usage": {"input_tokens": -1, "output_tokens": 0}},
        {"usage": {"input_tokens": True, "output_tokens": 0}},
        {"output": []},
    ),
)
def test_incomplete_identity_usage_and_missing_text_fail_closed(
    body_update: dict[str, object]
) -> None:
    _machine_input, _machine_result, request = _request_and_result()
    provider, _client = _provider(httpx.Response(200, json=_response_body(request, **body_update)))

    with pytest.raises(ProviderError):
        provider.review(
            request,
            profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
            reserved_cost_microusd=_primary_reservation(request),
        )


@pytest.mark.parametrize("output", ("not-json", json.dumps({"judgments": [{"target_id": "x"}]})))
def test_invalid_structured_output_fails_closed(output: str) -> None:
    _machine_input, _machine_result, request = _request_and_result()
    body = _response_body(request)
    body["output"][0]["content"][0]["text"] = output
    provider, _client = _provider(httpx.Response(200, json=body))

    with pytest.raises(ProviderError, match="invalid structured output"):
        provider.review(
            request,
            profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
            reserved_cost_microusd=_primary_reservation(request),
        )


def test_non_2xx_is_sanitized_and_does_not_leak_api_key() -> None:
    _machine_input, _machine_result, request = _request_and_result()
    provider, _client = _provider(httpx.Response(503, text="secret-test-key authorization"))

    with pytest.raises(ProviderError) as exc_info:
        provider.review(
            request,
            profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
            reserved_cost_microusd=_primary_reservation(request),
        )

    assert str(exc_info.value) == "OpenAI soft review failed with HTTP 503"
    assert "secret-test-key" not in str(exc_info.value)


def test_locked_base_prices_are_exact_rationals() -> None:
    assert I6_SOFT_REVIEW_INPUT_TOKEN_MICROUSD_NUMERATOR == 5
    assert I6_SOFT_REVIEW_INPUT_TOKEN_MICROUSD_DENOMINATOR == 2
    assert I6_SOFT_REVIEW_OUTPUT_TOKEN_MICROUSD_NUMERATOR == 15
    assert I6_SOFT_REVIEW_OUTPUT_TOKEN_MICROUSD_DENOMINATOR == 1


def test_conservative_cost_applies_cache_write_pricing_with_integer_ceiling() -> None:
    assert I6_SOFT_REVIEW_CACHE_WRITE_INPUT_MULTIPLIER_NUMERATOR == 5
    assert I6_SOFT_REVIEW_CACHE_WRITE_INPUT_MULTIPLIER_DENOMINATOR == 4
    assert (
        I6_SOFT_REVIEW_INPUT_TOKEN_MICROUSD_NUMERATOR
        * I6_SOFT_REVIEW_CACHE_WRITE_INPUT_MULTIPLIER_NUMERATOR
    ) == 25
    assert (
        I6_SOFT_REVIEW_INPUT_TOKEN_MICROUSD_DENOMINATOR
        * I6_SOFT_REVIEW_CACHE_WRITE_INPUT_MULTIPLIER_DENOMINATOR
    ) == 8
    assert soft_review_conservative_cost_microusd(input_tokens=1, output_tokens=0) == 4
    assert soft_review_conservative_cost_microusd(input_tokens=2, output_tokens=0) == 7


def test_conservative_cost_uses_normal_tier_at_long_context_threshold() -> None:
    assert soft_review_conservative_cost_microusd(
        input_tokens=I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_TOKEN_THRESHOLD,
        output_tokens=0,
    ) == 850_000


def test_conservative_cost_uses_long_context_tier_above_threshold() -> None:
    assert I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_MULTIPLIER_NUMERATOR == 2
    assert I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_MULTIPLIER_DENOMINATOR == 1
    assert (
        I6_SOFT_REVIEW_INPUT_TOKEN_MICROUSD_NUMERATOR
        * I6_SOFT_REVIEW_CACHE_WRITE_INPUT_MULTIPLIER_NUMERATOR
        * I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_MULTIPLIER_NUMERATOR
    ) == 50
    assert (
        I6_SOFT_REVIEW_INPUT_TOKEN_MICROUSD_DENOMINATOR
        * I6_SOFT_REVIEW_CACHE_WRITE_INPUT_MULTIPLIER_DENOMINATOR
        * I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_MULTIPLIER_DENOMINATOR
    ) == 8
    assert soft_review_conservative_cost_microusd(
        input_tokens=I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_TOKEN_THRESHOLD + 1,
        output_tokens=0,
    ) == 1_700_007


def test_long_context_output_cost_applies_the_locked_multiplier() -> None:
    assert I6_SOFT_REVIEW_LONG_CONTEXT_OUTPUT_MULTIPLIER_NUMERATOR == 3
    assert I6_SOFT_REVIEW_LONG_CONTEXT_OUTPUT_MULTIPLIER_DENOMINATOR == 2
    assert (
        I6_SOFT_REVIEW_OUTPUT_TOKEN_MICROUSD_NUMERATOR
        * I6_SOFT_REVIEW_LONG_CONTEXT_OUTPUT_MULTIPLIER_NUMERATOR
    ) == 45
    assert (
        I6_SOFT_REVIEW_OUTPUT_TOKEN_MICROUSD_DENOMINATOR
        * I6_SOFT_REVIEW_LONG_CONTEXT_OUTPUT_MULTIPLIER_DENOMINATOR
    ) == 2
    input_only_cost = soft_review_conservative_cost_microusd(
        input_tokens=I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_TOKEN_THRESHOLD + 1,
        output_tokens=0,
    )
    assert soft_review_conservative_cost_microusd(
        input_tokens=I6_SOFT_REVIEW_LONG_CONTEXT_INPUT_TOKEN_THRESHOLD + 1,
        output_tokens=1,
    ) == input_only_cost + 23


def test_incorrect_direct_provider_reservation_fails_before_post() -> None:
    _machine_input, _machine_result, request = _request_and_result()
    provider, client = _provider(httpx.Response(200, json=_response_body(request)))

    with pytest.raises(ProviderError, match="reservation conflicts with the request preflight"):
        provider.review(
            request,
            profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
            reserved_cost_microusd=_primary_reservation(request) - 1,
        )

    assert client.calls == []


def test_provider_result_at_reservation_is_allowed_and_one_microusd_over_fails() -> None:
    machine_input, machine_result, request = _request_and_result()
    body = _response_body(request)
    provider, _client = _provider(httpx.Response(200, json=body))
    reservation = soft_review_reservation_microusd(request, I6_SOFT_REVIEW_PRIMARY_PROFILE)
    execution = provider.review(
        request,
        profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
        reserved_cost_microusd=reservation,
    )
    expected_cost = soft_review_conservative_cost_microusd(input_tokens=1, output_tokens=0)
    at_reservation = SoftReviewProviderResult(
        artifact=execution.artifact,
        provider_response_id=execution.provider_response_id,
        actual_model=execution.actual_model,
        input_tokens=1,
        output_tokens=0,
        metered_cost_microusd=expected_cost,
        reserved_cost_microusd=expected_cost,
        selected_profile=execution.selected_profile,
        raw_response_sha256=execution.raw_response_sha256,
    )
    validate_soft_review_provider_result(
        request=request,
        result=at_reservation,
        expected_profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
        expected_reservation_microusd=expected_cost,
    )
    one_microusd_over = SoftReviewProviderResult(
        artifact=at_reservation.artifact,
        provider_response_id=at_reservation.provider_response_id,
        actual_model=at_reservation.actual_model,
        input_tokens=at_reservation.input_tokens,
        output_tokens=at_reservation.output_tokens,
        metered_cost_microusd=at_reservation.metered_cost_microusd,
        reserved_cost_microusd=expected_cost - 1,
        selected_profile=at_reservation.selected_profile,
        raw_response_sha256=at_reservation.raw_response_sha256,
    )
    with pytest.raises(ProviderError, match="exceeded its preflight reservation"):
        validate_soft_review_provider_result(
            request=request,
            result=one_microusd_over,
            expected_profile=I6_SOFT_REVIEW_PRIMARY_PROFILE,
            expected_reservation_microusd=expected_cost - 1,
        )
