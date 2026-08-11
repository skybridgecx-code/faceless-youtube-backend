from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from app.editorial.contracts import VisualMode


I5_PRODUCTION_POLICY_VERSION = "i5-production-v1"
I5_BUDGET_GATE_POLICY_VERSION = "i5-budget-v1"
I5_PROVIDER_CATALOG_VERSION = "i5-provider-catalog-2026-08-11-v1"
I5_STORYBOARD_CONTRACT_VERSION = "i5-storyboard-v1"
I5_NARRATION_NORMALIZATION = "collapse_unicode_whitespace_to_single_ascii_space"

I5_NARRATION_WORDS_PER_MINUTE = 150
I5_MIN_SCENES = 12
I5_MAX_SCENES = 40
I5_TARGET_MIN_SCENES = 18
I5_TARGET_MAX_SCENES = 32
I5_ORDINARY_SCENE_MIN_SECONDS = 12
I5_ORDINARY_SCENE_MAX_SECONDS = 30
# Exact sentence-boundary preservation may make the nearest legal grouping differ by
# one word (0.4 seconds at 150 WPM). This tolerance never permits mid-sentence edits.
I5_SCENE_DURATION_BOUNDARY_TOLERANCE_SECONDS = 0.5
I5_GENERATED_CINEMATIC_MAX_SCENE_SECONDS = 18
I5_MAX_GENERATED_CINEMATIC_SCENES = 3
I5_MAX_GENERATED_VIDEO_SCENES = 3
I5_MAX_GENERATED_VIDEO_SECONDS = 24
I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS = 45
I5_MAX_FACTUAL_OVERLAY_CHARS = 320

Sha256: TypeAlias = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DisclosureState: TypeAlias = Literal[
    "not_required",
    "visible_reconstruction_label",
    "illustrative_pseudocode",
    "illustrative_generated_media",
]
FulfillmentStrategy: TypeAlias = Literal[
    "local_deterministic",
    "generated_image_primary",
    "generated_image_fallback",
    "generated_video_primary",
]
RightsBasisKind: TypeAlias = Literal[
    "local_original",
    "reference_only_evidence_card",
    "generated_illustrative",
    "reusable_source_asset",
]


class FrozenProductionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class SourceRights(FrozenProductionModel):
    source_key: str = Field(min_length=1, max_length=120)
    rights_status: str = Field(min_length=1, max_length=80)


class RightsBasis(FrozenProductionModel):
    kind: RightsBasisKind
    reuses_source_media: bool
    source_rights: tuple[SourceRights, ...] = ()


class FulfillmentPlan(FrozenProductionModel):
    strategy: FulfillmentStrategy
    visual_mode: VisualMode
    generated_video_seconds: float = Field(default=0, ge=0)


class FactualOverlay(FrozenProductionModel):
    kind: Literal["verified_fact", "attributed_verified_claim"]
    text: str = Field(min_length=1, max_length=I5_MAX_FACTUAL_OVERLAY_CHARS)
    claim_hashes: tuple[Sha256, ...] = Field(min_length=1)
    source_keys: tuple[str, ...] = Field(min_length=1)


class StoryboardScene(FrozenProductionModel):
    position: int = Field(ge=0)
    section_id: str = Field(min_length=1, max_length=80)
    beat_index: int = Field(ge=0)
    narration: str = Field(min_length=1, max_length=20_000)
    narration_sha256: Sha256
    claim_hashes: tuple[Sha256, ...] = Field(min_length=1)
    estimated_duration_seconds: float = Field(gt=0)
    visual_mode: VisualMode
    visual_purpose: str = Field(min_length=1, max_length=2_000)
    factual_overlay: FactualOverlay | None
    source_keys: tuple[str, ...]
    rights_basis: RightsBasis
    disclosure_state: DisclosureState
    primary_fulfillment: FulfillmentPlan
    fallback_fulfillments: tuple[FulfillmentPlan, ...] = Field(min_length=1)
    continuation_reason: str = Field(min_length=1, max_length=2_000)
    payoff: str = Field(min_length=1, max_length=2_000)


class StoryboardCompilationOptions(FrozenProductionModel):
    generated_cinematic_enabled: bool = True
    generated_video_enabled: bool = False


class GeneratedMediaSummary(FrozenProductionModel):
    generated_cinematic_scene_count: int = Field(ge=0)
    generated_cinematic_estimated_coverage_seconds: float = Field(ge=0)
    generated_video_scene_count: int = Field(ge=0)
    generated_video_estimated_duration_seconds: float = Field(ge=0)


class StoryboardGate(FrozenProductionModel):
    outcome: Literal["PASS", "FAIL"]
    reasons: tuple[str, ...] = Field(min_length=1)


class StoryboardPacket(FrozenProductionModel):
    contract_version: Literal["i5-storyboard-v1"] = I5_STORYBOARD_CONTRACT_VERSION
    campaign_id: int = Field(ge=1)
    script_hash: Sha256
    production_profile_hash: Sha256
    narration_normalization: Literal[
        "collapse_unicode_whitespace_to_single_ascii_space"
    ] = I5_NARRATION_NORMALIZATION
    compilation_options: StoryboardCompilationOptions
    scene_count: int = Field(ge=0)
    visual_mode_counts: dict[str, int]
    generated_media_summary: GeneratedMediaSummary
    meaningful_visual_progression_score: int = Field(ge=0, le=100)
    scenes: tuple[StoryboardScene, ...]
    gate: StoryboardGate
    policy_version: Literal["i5-production-v1"] = I5_PRODUCTION_POLICY_VERSION
