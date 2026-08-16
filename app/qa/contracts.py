from __future__ import annotations

import json
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.editorial.contracts import canonical_json, canonical_sha256, normalize_key


I6_MACHINE_QA_POLICY_VERSION = "i6-machine-qa-v2"
I6_HOOK_MAX_SECONDS = 30.0
I6_MAX_THUMBNAIL_TEXT_WORDS = 6
I6_MAX_THUMBNAIL_TEXT_CHARACTERS = 42
I6_MIN_THUMBNAIL_TEXT_HEIGHT_PX = 24
I6_MIN_THUMBNAIL_TEXT_HEIGHT_RATIO = 0.04

GateOutcome: TypeAlias = Literal["PASS", "FAIL", "NEEDS_HUMAN"]
ArtifactKind: TypeAlias = Literal[
    "i4_topic_packet",
    "i4_research_packet",
    "i4_script",
    "i5_storyboard",
    "i5_production_profile",
    "i5_media_manifest",
    "i5_assembly_manifest",
    "i5_final_render",
]
StageName: TypeAlias = Literal[
    "topic", "research", "script", "storyboard", "media", "assembly"
]
OverclaimRisk: TypeAlias = Literal["low", "medium", "high"]


class FrozenQAModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class MachineQAFinding(FrozenQAModel):
    check_id: str = Field(min_length=1, max_length=160)
    outcome: GateOutcome
    message: str = Field(min_length=1, max_length=1_000)
    hard_gate: bool = False
    evidence: tuple[str, ...] = Field(default_factory=tuple, max_length=32)

    @model_validator(mode="after")
    def hard_gate_must_fail(self) -> MachineQAFinding:
        if self.hard_gate and self.outcome != "FAIL":
            raise ValueError("hard_gate findings must be FAIL outcomes")
        return self


class HumanReviewFinding(FrozenQAModel):
    check_id: str = Field(min_length=1, max_length=160)
    review_status: Literal["NEEDS_HUMAN"] = "NEEDS_HUMAN"
    message: str = Field(min_length=1, max_length=1_000)
    evidence: tuple[str, ...] = Field(default_factory=tuple, max_length=32)


class ArtifactSnapshot(FrozenQAModel):
    """Read-only artifact fields mirrored from the canonical Artifact row."""

    campaign_id: int = Field(ge=1)
    kind: ArtifactKind
    source_stage: StageName
    sha256: str = Field(min_length=1, max_length=64)
    payload: dict[str, object] | None = None


class GateDecisionSnapshot(FrozenQAModel):
    """Read-only GateDecision fields; reasons remain the canonical JSON column."""

    campaign_id: int = Field(ge=1)
    stage: StageName
    outcome: GateOutcome
    policy_version: str = Field(min_length=1, max_length=120)
    input_hash: str = Field(min_length=1, max_length=64)
    output_hash: str | None = Field(default=None, min_length=1, max_length=64)
    reasons_json: str = Field(min_length=2, max_length=8_000)

    @field_validator("reasons_json")
    @classmethod
    def require_canonical_reason_array(cls, value: str) -> str:
        try:
            reasons = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("reasons_json must be canonical JSON") from exc
        if not isinstance(reasons, list) or not all(
            isinstance(reason, str) and reason.strip() for reason in reasons
        ):
            raise ValueError("reasons_json must be a nonblank string array")
        if canonical_json(reasons) != value:
            raise ValueError("reasons_json must use canonical JSON encoding")
        return value


class BinaryArtifactSnapshot(FrozenQAModel):
    """Read-only canonical I5 binary Artifact fields and immutable provenance JSON."""

    campaign_id: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=120)
    source_stage: StageName
    uri: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(gt=0)
    mime_type: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    provider_model: str = Field(min_length=1)
    prompt_template_version: str = Field(min_length=1)
    provenance_json: str = Field(min_length=2, max_length=20_000)


class CanonicalLineage(FrozenQAModel):
    """The complete, in-memory I4/I5 chain required for pure I6 verification."""

    campaign_id: int = Field(ge=1)
    topic: ArtifactSnapshot
    research: ArtifactSnapshot
    script: ArtifactSnapshot
    storyboard: ArtifactSnapshot
    production_profile: ArtifactSnapshot
    media: ArtifactSnapshot
    assembly: ArtifactSnapshot
    final_render: ArtifactSnapshot
    gates: tuple[GateDecisionSnapshot, ...] = Field(min_length=6, max_length=6)
    editorial_seed_payload: dict[str, object]
    demand_snapshot_payload: dict[str, object]
    scene_narration_artifacts: tuple[BinaryArtifactSnapshot, ...] = ()
    scene_visual_artifacts: tuple[BinaryArtifactSnapshot, ...] = ()
    voiceover_artifact: BinaryArtifactSnapshot | None = None
    final_render_artifact: BinaryArtifactSnapshot | None = None

    @model_validator(mode="after")
    def require_one_gate_per_stage(self) -> CanonicalLineage:
        stages = [gate.stage for gate in self.gates]
        if set(stages) != {
            "topic",
            "research",
            "script",
            "storyboard",
            "media",
            "assembly",
        } or len(stages) != len(set(stages)):
            raise ValueError("lineage requires one gate for every canonical stage")
        return self


class PackagingImplication(FrozenQAModel):
    """Only an extractive claim statement can be mechanically proven true."""

    statement: str = Field(min_length=1, max_length=4_000)
    claim_hashes: tuple[str, ...] = Field(min_length=1, max_length=1)


class PackagingConcept(FrozenQAModel):
    concept_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=180)
    thumbnail_concept: str = Field(min_length=1, max_length=1_000)
    thumbnail_text: str = Field(default="", max_length=160)
    viewer_trigger: str = Field(min_length=1, max_length=1_000)
    promise: str = Field(min_length=1, max_length=1_000)
    overclaim_risk: OverclaimRisk
    target_audience: str = Field(min_length=1, max_length=1_000)
    implications: tuple[PackagingImplication, ...] = Field(min_length=1, max_length=20)

    @field_validator("concept_id")
    @classmethod
    def normalize_concept_id(cls, value: str) -> str:
        return normalize_key(value)


class Rectangle(FrozenQAModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    def area(self) -> int:
        return self.width * self.height


class ThumbnailTextElement(FrozenQAModel):
    text: str = Field(min_length=1, max_length=160)
    bounds: Rectangle
    font_height_px: int = Field(ge=1)
    contrast_ratio: float = Field(ge=1, le=21)


class ThumbnailVisualElement(FrozenQAModel):
    bounds: Rectangle


class ThumbnailLayout(FrozenQAModel):
    """Raw geometry layout specification; it is not proof of rendered image pixels."""

    concept_id: str = Field(min_length=1, max_length=80)
    canvas_width_px: int = Field(ge=1, le=10_000)
    canvas_height_px: int = Field(ge=1, le=10_000)
    text_elements: tuple[ThumbnailTextElement, ...] = Field(default_factory=tuple)
    visual_elements: tuple[ThumbnailVisualElement, ...] = Field(default_factory=tuple)
    layout_spec_sha256: str = Field(min_length=1, max_length=64)

    @field_validator("concept_id")
    @classmethod
    def normalize_layout_concept_id(cls, value: str) -> str:
        return normalize_key(value)

    def hash_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        del payload["layout_spec_sha256"]
        return payload


class PackagingQAInput(FrozenQAModel):
    concepts: tuple[PackagingConcept, ...] = Field(min_length=1, max_length=12)
    selected_concept_id: str = Field(min_length=1, max_length=80)
    thumbnails: tuple[ThumbnailLayout, ...] = Field(
        default_factory=tuple, max_length=12
    )

    @field_validator("selected_concept_id")
    @classmethod
    def normalize_selected_concept_id(cls, value: str) -> str:
        return normalize_key(value)

    @model_validator(mode="after")
    def validate_ids(self) -> PackagingQAInput:
        concept_ids = [concept.concept_id for concept in self.concepts]
        if len(concept_ids) != len(set(concept_ids)):
            raise ValueError("packaging concept_id values must be unique")
        if self.selected_concept_id not in concept_ids:
            raise ValueError("selected_concept_id must reference a packaging concept")
        thumbnail_ids = [thumbnail.concept_id for thumbnail in self.thumbnails]
        if len(thumbnail_ids) != len(set(thumbnail_ids)):
            raise ValueError("thumbnail concept_id values must be unique")
        if set(thumbnail_ids) - set(concept_ids):
            raise ValueError("thumbnail layout references an unknown concept")
        return self


class HookQAResult(FrozenQAModel):
    outcome: GateOutcome
    findings: tuple[MachineQAFinding, ...]
    review_findings: tuple[HumanReviewFinding, ...] = ()


class ThumbnailQAResult(FrozenQAModel):
    concept_id: str
    outcome: GateOutcome
    findings: tuple[MachineQAFinding, ...]
    review_findings: tuple[HumanReviewFinding, ...] = ()


class PackagingQAResult(FrozenQAModel):
    outcome: GateOutcome
    findings: tuple[MachineQAFinding, ...]
    thumbnails: tuple[ThumbnailQAResult, ...]
    review_findings: tuple[HumanReviewFinding, ...] = ()


class MachineQAInput(FrozenQAModel):
    lineage: CanonicalLineage
    packaging: PackagingQAInput
    commercial_score: float | None = Field(default=None, ge=0, le=100)
    policy_version: Literal["i6-machine-qa-v2"] = I6_MACHINE_QA_POLICY_VERSION

    def input_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class MachineQAResult(FrozenQAModel):
    outcome: GateOutcome
    findings: tuple[MachineQAFinding, ...]
    hook: HookQAResult
    packaging: PackagingQAResult
    input_sha256: str = Field(min_length=64, max_length=64)
    review_findings: tuple[HumanReviewFinding, ...] = ()
    policy_version: Literal["i6-machine-qa-v2"] = I6_MACHINE_QA_POLICY_VERSION
