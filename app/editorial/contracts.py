from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


I4_POLICY_VERSION = "i4-editorial-v1"
I4_MAX_CANDIDATES = 3
I4_MAX_YOUTUBE_RESULTS_PER_CANDIDATE = 10

VisualMode: TypeAlias = Literal[
    "GENERATED_CINEMATIC",
    "REAL_SOURCE_MEDIA",
    "PRODUCT_FOOTAGE",
    "DOCUMENT",
    "SCREENSHOT",
    "DIAGRAM",
    "DATA_VISUALIZATION",
    "MAP",
    "TIMELINE",
    "CODE",
    "UI_RECONSTRUCTION",
    "KINETIC_TEXT",
    "DETERMINISTIC_MOTION_GRAPHIC",
]
ShelfLife: TypeAlias = Literal["breaking", "near_term", "mixed", "evergreen"]
SensitivityTag: TypeAlias = Literal[
    "health",
    "finance",
    "politics_elections",
    "war_conflict",
    "legal_advice",
]
SourceClass: TypeAlias = Literal[
    "primary",
    "official",
    "reputable_secondary",
    "company_claim",
    "discovery_only",
]
ClaimType: TypeAlias = Literal["fact", "attributed_claim", "estimate", "opinion"]
ClaimRole: TypeAlias = Literal[
    "context",
    "evidence",
    "stakes",
    "counterpoint",
    "uncertainty",
    "outlook",
]
ClaimState: TypeAlias = Literal["VERIFIED", "ESTIMATE", "OPINION", "REJECTED"]

NonBlank = Annotated[str, Field(min_length=1, max_length=4_000)]


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("key must contain at least one letter or number")
    if len(normalized) > 80:
        raise ValueError("normalized key must be at most 80 characters")
    return normalized


class FrozenEditorialModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class EvidenceSourceInput(FrozenEditorialModel):
    source_key: str = Field(min_length=1, max_length=80)
    source_uri: str = Field(min_length=1, max_length=2_000)
    publisher: str = Field(min_length=1, max_length=240)
    source_class: SourceClass
    evidence_snippet: str = Field(min_length=1, max_length=4_000)

    @field_validator("source_key")
    @classmethod
    def normalize_source_key(cls, value: str) -> str:
        return normalize_key(value)

    @field_validator("source_uri")
    @classmethod
    def require_https_source(cls, value: str) -> str:
        normalized = value.strip()
        try:
            parsed = urlsplit(normalized)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("evidence source_uri must be a valid HTTPS URL") from exc
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.netloc
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() for character in normalized)
        ):
            raise ValueError("evidence source_uri must be a valid HTTPS URL with a host")
        return normalized


class ClaimSeed(FrozenEditorialModel):
    assertion_text: str = Field(min_length=1, max_length=4_000)
    material: bool = True
    claim_type: ClaimType
    role: ClaimRole
    source_keys: tuple[str, ...] = Field(default_factory=tuple, max_length=12)
    attribution: str | None = Field(default=None, max_length=500)
    assumptions: tuple[str, ...] = Field(default_factory=tuple, max_length=12)

    @field_validator("source_keys")
    @classmethod
    def normalize_source_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({normalize_key(value) for value in values}))
        return normalized

    @field_validator("attribution")
    @classmethod
    def normalize_optional_attribution(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("assumptions")
    @classmethod
    def normalize_assumptions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


class ViewerPromise(FrozenEditorialModel):
    viewer_promise: NonBlank
    core_question: NonBlank
    why_now: NonBlank
    stakes: NonBlank
    novelty: NonBlank
    target_viewer: NonBlank
    broad_interest_bridge: NonBlank
    expected_takeaway: NonBlank


class TopicCandidate(FrozenEditorialModel):
    candidate_key: str = Field(min_length=1, max_length=80)
    topic: NonBlank
    angle: NonBlank
    target_viewer: NonBlank
    viewer_promise: NonBlank
    core_question: NonBlank
    why_now: NonBlank
    stakes: NonBlank
    novelty: NonBlank
    broad_interest_bridge: NonBlank
    expected_takeaway: NonBlank
    visual_modes: tuple[VisualMode, ...] = Field(min_length=1, max_length=13)
    shelf_life: ShelfLife
    sponsor_categories: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    sensitivity_tags: tuple[SensitivityTag, ...] = Field(default_factory=tuple, max_length=5)
    evidence_sources: tuple[EvidenceSourceInput, ...] = Field(min_length=2, max_length=12)
    claims: tuple[ClaimSeed, ...] = Field(min_length=1, max_length=60)

    @field_validator("candidate_key")
    @classmethod
    def normalize_candidate_key(cls, value: str) -> str:
        return normalize_key(value)

    @field_validator("visual_modes")
    @classmethod
    def unique_visual_modes(cls, values: tuple[VisualMode, ...]) -> tuple[VisualMode, ...]:
        return tuple(dict.fromkeys(values))

    @field_validator("sponsor_categories")
    @classmethod
    def normalize_sponsor_categories(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )

    @field_validator("sensitivity_tags")
    @classmethod
    def unique_sensitivity_tags(
        cls,
        values: tuple[SensitivityTag, ...],
    ) -> tuple[SensitivityTag, ...]:
        return tuple(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_source_identities(self) -> TopicCandidate:
        source_keys = [source.source_key for source in self.evidence_sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("source_key values must be unique within a candidate")
        source_uris = [source.source_uri for source in self.evidence_sources]
        if len(source_uris) != len(set(source_uris)):
            raise ValueError("source_uri values must be unique within a candidate")
        return self

    def viewer_promise_contract(self) -> ViewerPromise:
        return ViewerPromise(
            viewer_promise=self.viewer_promise,
            core_question=self.core_question,
            why_now=self.why_now,
            stakes=self.stakes,
            novelty=self.novelty,
            target_viewer=self.target_viewer,
            broad_interest_bridge=self.broad_interest_bridge,
            expected_takeaway=self.expected_takeaway,
        )


class EditorialSeed(FrozenEditorialModel):
    candidates: tuple[TopicCandidate, ...] = Field(
        min_length=1,
        max_length=I4_MAX_CANDIDATES,
    )

    @model_validator(mode="after")
    def validate_candidate_keys(self) -> EditorialSeed:
        keys = [candidate.candidate_key for candidate in self.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("candidate_key values must be unique within the seed")
        return self

    def artifact_payload(self, campaign_id: int) -> dict[str, object]:
        if campaign_id <= 0:
            raise ValueError("campaign_id must be positive")
        return {
            "campaign_id": campaign_id,
            "candidates": self.model_dump(mode="json")["candidates"],
            "contract_version": "i4-editorial-seed-v1",
            "requires_youtube_demand": True,
        }

    def canonical_json(self, campaign_id: int) -> str:
        return canonical_json(self.artifact_payload(campaign_id))

    def sha256(self, campaign_id: int) -> str:
        return canonical_sha256(self.artifact_payload(campaign_id))


class DemandVideo(FrozenEditorialModel):
    video_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    channel_id: str = Field(default="", max_length=120)
    channel_title: str = Field(default="", max_length=240)
    published_at: str | None = None
    view_count: int | None = Field(default=None, ge=0)
    like_count: int | None = Field(default=None, ge=0)
    comment_count: int | None = Field(default=None, ge=0)
    channel_subscriber_count: int | None = Field(default=None, ge=0)


class CandidateDemandSnapshot(FrozenEditorialModel):
    candidate_key: str
    query: str = Field(min_length=1, max_length=500)
    videos: tuple[DemandVideo, ...] = Field(max_length=I4_MAX_YOUTUBE_RESULTS_PER_CANDIDATE)

    @field_validator("candidate_key")
    @classmethod
    def normalize_candidate_key(cls, value: str) -> str:
        return normalize_key(value)


class DemandSnapshot(FrozenEditorialModel):
    contract_version: Literal["i4-youtube-demand-v1"] = "i4-youtube-demand-v1"
    retrieved_at: str = Field(min_length=1, max_length=80)
    candidates: tuple[CandidateDemandSnapshot, ...] = Field(
        min_length=1,
        max_length=I4_MAX_CANDIDATES,
    )


class ClaimEvaluation(FrozenEditorialModel):
    claim_hash: str = Field(min_length=64, max_length=64)
    assertion_text: str
    material: bool
    claim_type: ClaimType
    role: ClaimRole
    source_keys: tuple[str, ...]
    attribution: str | None
    assumptions: tuple[str, ...]
    state: ClaimState
    reasons: tuple[str, ...]

    def metadata_payload(self) -> dict[str, object]:
        return {
            "assumptions": list(self.assumptions),
            "attribution": self.attribution,
            "claim_type": self.claim_type,
            "role": self.role,
            "source_keys": list(self.source_keys),
        }
