from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping


class UsageError(RuntimeError):
    """Raised when persisted YouMo usage telemetry cannot be interpreted safely."""


RATE_CARD_SOURCE = "OpenAI Codex rate card"
RATE_CARD_VERSION = "openai-codex-rate-card-2026-08-11"
RATE_CARD_SNAPSHOT_DATE = "2026-08-11"
RATE_CARD_UNIT = "credits_per_1m_tokens"
CACHE_RATIO_DEFINITION = (
    "cached_input_tokens / input_tokens; Codex SDK input_tokens includes cached input tokens"
)
WARN_TURN_CREDITS_ENV = "YOUMO_WARN_TURN_CREDITS"
WARN_RUN_CREDITS_ENV = "YOUMO_WARN_RUN_CREDITS"

_RATE_CARD: dict[str, tuple[float, float, float]] = {
    "gpt-5.6-terra": (62.50, 6.250, 375.0),
    "gpt-5.6-sol": (125.0, 12.50, 750.0),
}


@dataclass(frozen=True)
class CreditRateSnapshot:
    source: str
    version: str
    snapshot_date: str
    unit: str
    model: str
    input_credits_per_million: float | None
    cached_input_credits_per_million: float | None
    output_credits_per_million: float | None

    @property
    def priced(self) -> bool:
        return (
            self.input_credits_per_million is not None
            and self.cached_input_credits_per_million is not None
            and self.output_credits_per_million is not None
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CreditRateSnapshot":
        return cls(
            source=_required_string(value.get("source"), "credit rate source"),
            version=_required_string(value.get("version"), "credit rate version"),
            snapshot_date=_required_string(value.get("snapshot_date"), "credit rate snapshot_date"),
            unit=_required_string(value.get("unit"), "credit rate unit"),
            model=_required_string(value.get("model"), "credit rate model"),
            input_credits_per_million=_optional_number(
                value.get("input_credits_per_million"), "input credit rate"
            ),
            cached_input_credits_per_million=_optional_number(
                value.get("cached_input_credits_per_million"), "cached input credit rate"
            ),
            output_credits_per_million=_optional_number(
                value.get("output_credits_per_million"), "output credit rate"
            ),
        )


@dataclass(frozen=True)
class UsageRecord:
    run_id: str | None
    stage: str
    model: str
    reasoning_effort: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    estimated_credits: float | None
    credit_rate_snapshot: CreditRateSnapshot
    timestamp: str
    usage_available: bool
    sdk_total_tokens: int | None
    sdk_reasoning_output_tokens: int | None
    sdk_cache_write_input_tokens: int | None
    sdk_model_context_window: int | None

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "UsageRecord":
        stage = _required_string(value.get("stage"), "usage stage")
        if stage not in {"build", "audit"}:
            raise UsageError(f"unsupported usage stage: {stage!r}")
        run_id = value.get("run_id")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise UsageError("usage run_id must be null or a non-empty string")
        snapshot_raw = value.get("credit_rate_snapshot")
        if not isinstance(snapshot_raw, Mapping):
            raise UsageError("usage credit_rate_snapshot must be an object")
        available = value.get("usage_available")
        if not isinstance(available, bool):
            raise UsageError("usage_available must be boolean")
        return cls(
            run_id=run_id,
            stage=stage,
            model=_required_string(value.get("model"), "usage model"),
            reasoning_effort=_required_string(
                value.get("reasoning_effort"), "usage reasoning_effort"
            ),
            input_tokens=_optional_token(value.get("input_tokens"), "input_tokens"),
            cached_input_tokens=_optional_token(
                value.get("cached_input_tokens"), "cached_input_tokens"
            ),
            output_tokens=_optional_token(value.get("output_tokens"), "output_tokens"),
            estimated_credits=_optional_number(
                value.get("estimated_credits"), "estimated_credits"
            ),
            credit_rate_snapshot=CreditRateSnapshot.from_mapping(snapshot_raw),
            timestamp=_validated_timestamp(value.get("timestamp")),
            usage_available=available,
            sdk_total_tokens=_optional_token(value.get("sdk_total_tokens"), "sdk_total_tokens"),
            sdk_reasoning_output_tokens=_optional_token(
                value.get("sdk_reasoning_output_tokens"), "sdk_reasoning_output_tokens"
            ),
            sdk_cache_write_input_tokens=_optional_token(
                value.get("sdk_cache_write_input_tokens"), "sdk_cache_write_input_tokens"
            ),
            sdk_model_context_window=_optional_token(
                value.get("sdk_model_context_window"), "sdk_model_context_window"
            ),
        )


@dataclass(frozen=True)
class UsageTotals:
    turns: int
    available_turns: int
    unavailable_turns: int
    unpriced_turns: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    estimated_credits: float
    estimated_credits_complete: bool
    cache_ratio: float | None


@dataclass(frozen=True)
class UsageWarningThresholds:
    turn_credits: float | None
    run_credits: float | None



def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError(f"{name} must be a non-empty string")
    return value


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(f"{name} must be null or numeric")
    number = float(value)
    if number < 0:
        raise UsageError(f"{name} cannot be negative")
    return number


def _optional_token(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageError(f"{name} must be null or a non-negative integer")
    return value


def _safe_token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_timestamp(value: object) -> str:
    raw = _required_string(value, "usage timestamp")
    try:
        observed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise UsageError("usage timestamp must be ISO-8601") from exc
    if observed.tzinfo is None:
        raise UsageError("usage timestamp must include a timezone")
    return raw


def credit_rate_snapshot(model: str) -> CreditRateSnapshot:
    rates = _RATE_CARD.get(model.lower())
    return CreditRateSnapshot(
        source=RATE_CARD_SOURCE,
        version=RATE_CARD_VERSION,
        snapshot_date=RATE_CARD_SNAPSHOT_DATE,
        unit=RATE_CARD_UNIT,
        model=model,
        input_credits_per_million=None if rates is None else rates[0],
        cached_input_credits_per_million=None if rates is None else rates[1],
        output_credits_per_million=None if rates is None else rates[2],
    )


def estimate_credits(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    snapshot: CreditRateSnapshot,
) -> float | None:
    if not snapshot.priced:
        return None
    if cached_input_tokens > input_tokens:
        return None
    non_cached_input = input_tokens - cached_input_tokens
    total = (
        Decimal(non_cached_input) * Decimal(str(snapshot.input_credits_per_million))
        + Decimal(cached_input_tokens)
        * Decimal(str(snapshot.cached_input_credits_per_million))
        + Decimal(output_tokens) * Decimal(str(snapshot.output_credits_per_million))
    ) / Decimal(1_000_000)
    return float(total.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _jsonable_usage(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        payload = dump(mode="json")
        return payload if isinstance(payload, Mapping) else None
    return None


def capture_turn_usage(
    raw_usage: object,
    *,
    run_id: str | None,
    stage: str,
    model: str,
    reasoning_effort: str,
    timestamp: str | None = None,
) -> UsageRecord:
    if stage not in {"build", "audit"}:
        raise UsageError(f"unsupported usage stage: {stage!r}")
    snapshot = credit_rate_snapshot(model)
    usage = _jsonable_usage(raw_usage)
    last = usage.get("last") if usage is not None else None
    last_mapping = last if isinstance(last, Mapping) else None

    input_tokens = _safe_token(last_mapping.get("input_tokens")) if last_mapping else None
    cached_input_tokens = (
        _safe_token(last_mapping.get("cached_input_tokens")) if last_mapping else None
    )
    output_tokens = _safe_token(last_mapping.get("output_tokens")) if last_mapping else None
    total_tokens = _safe_token(last_mapping.get("total_tokens")) if last_mapping else None
    reasoning_tokens = (
        _safe_token(last_mapping.get("reasoning_output_tokens")) if last_mapping else None
    )
    cache_write_tokens = (
        _safe_token(last_mapping.get("cache_write_input_tokens")) if last_mapping else None
    )
    model_context_window = (
        _safe_token(usage.get("model_context_window")) if usage is not None else None
    )

    available = (
        input_tokens is not None
        and cached_input_tokens is not None
        and output_tokens is not None
        and cached_input_tokens <= input_tokens
    )
    credits = None
    if available:
        credits = estimate_credits(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            snapshot=snapshot,
        )

    return UsageRecord(
        run_id=run_id,
        stage=stage,
        model=model,
        reasoning_effort=reasoning_effort,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        estimated_credits=credits,
        credit_rate_snapshot=snapshot,
        timestamp=timestamp or _utc_now(),
        usage_available=available,
        sdk_total_tokens=total_tokens,
        sdk_reasoning_output_tokens=reasoning_tokens,
        sdk_cache_write_input_tokens=cache_write_tokens,
        sdk_model_context_window=model_context_window,
    )


def usage_record_from_evidence_payload(payload: Mapping[str, Any]) -> UsageRecord | None:
    raw = payload.get("usage")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise UsageError("evidence usage field must be an object or null")
    return UsageRecord.from_mapping(raw)


def load_usage_record(path: Path) -> UsageRecord | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"usage evidence is unreadable: {path}") from exc
    if not isinstance(payload, Mapping):
        raise UsageError(f"usage evidence root must be an object: {path}")
    return usage_record_from_evidence_payload(payload)


def aggregate_usage(records: Iterable[UsageRecord]) -> UsageTotals:
    values = tuple(records)
    available = tuple(record for record in values if record.usage_available)
    input_tokens = sum(record.input_tokens or 0 for record in available)
    cached_tokens = sum(record.cached_input_tokens or 0 for record in available)
    output_tokens = sum(record.output_tokens or 0 for record in available)
    priced = tuple(record for record in available if record.estimated_credits is not None)
    credits = round(sum(record.estimated_credits or 0.0 for record in priced), 6)
    unpriced = len(available) - len(priced)
    unavailable = len(values) - len(available)
    ratio = None if input_tokens == 0 else round(cached_tokens / input_tokens, 6)
    return UsageTotals(
        turns=len(values),
        available_turns=len(available),
        unavailable_turns=unavailable,
        unpriced_turns=unpriced,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        estimated_credits=credits,
        estimated_credits_complete=unavailable == 0 and unpriced == 0,
        cache_ratio=ratio,
    )


def usage_by_model(records: Iterable[UsageRecord]) -> dict[str, UsageTotals]:
    grouped: dict[str, list[UsageRecord]] = {}
    for record in records:
        grouped.setdefault(record.model, []).append(record)
    return {model: aggregate_usage(grouped[model]) for model in sorted(grouped)}


def usage_for_utc_date(records: Iterable[UsageRecord], target: date) -> tuple[UsageRecord, ...]:
    matched: list[UsageRecord] = []
    for record in records:
        try:
            observed = datetime.fromisoformat(record.timestamp)
        except ValueError:
            continue
        if observed.tzinfo is None:
            continue
        if observed.astimezone(timezone.utc).date() == target:
            matched.append(record)
    return tuple(matched)


def _threshold(name: str, value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise UsageError(f"{name} must be a non-negative number") from exc
    if parsed < 0:
        raise UsageError(f"{name} must be a non-negative number")
    return parsed


def warning_thresholds_from_env(
    environ: Mapping[str, str] | None = None,
) -> UsageWarningThresholds:
    source = os.environ if environ is None else environ
    return UsageWarningThresholds(
        turn_credits=_threshold(WARN_TURN_CREDITS_ENV, source.get(WARN_TURN_CREDITS_ENV)),
        run_credits=_threshold(WARN_RUN_CREDITS_ENV, source.get(WARN_RUN_CREDITS_ENV)),
    )


def usage_warnings(
    records: Iterable[UsageRecord],
    thresholds: UsageWarningThresholds,
) -> tuple[str, ...]:
    values = tuple(records)
    warnings: list[str] = []
    if thresholds.turn_credits is not None:
        for record in values:
            credits = record.estimated_credits
            if credits is not None and credits >= thresholds.turn_credits:
                warnings.append(
                    f"turn {record.stage} {record.model} estimated credits {credits:.6f} "
                    f">= warning threshold {thresholds.turn_credits:.6f}"
                )
    if thresholds.run_credits is not None:
        total = aggregate_usage(values)
        if total.estimated_credits >= thresholds.run_credits and values:
            qualifier = "" if total.estimated_credits_complete else "known "
            warnings.append(
                f"{qualifier}run estimated credits {total.estimated_credits:.6f} "
                f">= warning threshold {thresholds.run_credits:.6f}"
            )
    return tuple(warnings)
