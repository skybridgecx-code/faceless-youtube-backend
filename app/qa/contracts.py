from __future__ import annotations

import json
import re
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.editorial.contracts import canonical_json, canonical_sha256, normalize_key


I6_MACHINE_QA_POLICY_VERSION = "i6-machine-qa-v2"
I6_HOOK_MAX_SECONDS = 30.0
I6_MAX_THUMBNAIL_TEXT_WORDS = 6
I6_MAX_THUMBNAIL_TEXT_CHARACTERS = 42
I6_MIN_THUMBNAIL_TEXT_HEIGHT_PX = 24
I6_MIN_THUMBNAIL_TEXT_HEIGHT_RATIO = 0.04
I6_SOFT_REVIEW_POLICY_VERSION = "i6-soft-review-v1"
I6_SOFT_REVIEW_CHECK_IDS: tuple[str, ...] = (
    "hook_unnecessary_introduction",
    "hook_information_density",
    "hook_continuation_reason",
    "hook_avoidable_length",
)

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


class FrozenSoftReviewModel(BaseModel):
    """Frozen soft-review data that preserves canonical text exactly as supplied."""

    model_config = ConfigDict(frozen=True, extra="forbid")


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

    def sha256(self) -> str:
        """Canonical identity of the complete immutable machine-QA result."""

        return canonical_sha256(self.model_dump(mode="json"))


def _require_sha256(value: str, field_name: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 value")
    return value


class SoftReviewTarget(FrozenSoftReviewModel):
    """A content-addressed, policy-authorized editorial review target."""

    target_id: str = Field(min_length=64, max_length=64)
    machine_input_sha256: str = Field(min_length=64, max_length=64)
    machine_result_sha256: str = Field(min_length=64, max_length=64)
    scope: Literal["hook"] = "hook"
    check_id: str = Field(min_length=1, max_length=160)
    original_message: str = Field(min_length=1, max_length=1_000)
    original_evidence: tuple[str, ...] = Field(default_factory=tuple, max_length=32)

    @field_validator("target_id", "machine_input_sha256", "machine_result_sha256")
    @classmethod
    def require_hashes(cls, value: str, info: object) -> str:
        return _require_sha256(value, getattr(info, "field_name", "hash"))

    @field_validator("check_id")
    @classmethod
    def require_authorized_check(cls, value: str) -> str:
        if value not in I6_SOFT_REVIEW_CHECK_IDS:
            raise ValueError("check_id is not authorized for I6-B1 soft review")
        return value

    @field_validator("original_evidence")
    @classmethod
    def bound_nonblank_evidence(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 1_000 for value in values):
            raise ValueError("original_evidence must contain bounded nonblank strings")
        return values

    def hash_payload(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "machine_input_sha256": self.machine_input_sha256,
            "machine_result_sha256": self.machine_result_sha256,
            "original_evidence": list(self.original_evidence),
            "original_message": self.original_message,
            "policy_version": I6_SOFT_REVIEW_POLICY_VERSION,
            "scope": self.scope,
        }

    @model_validator(mode="after")
    def require_content_addressed_target_id(self) -> SoftReviewTarget:
        if self.target_id != canonical_sha256(self.hash_payload()):
            raise ValueError("target_id does not bind the canonical target payload")
        return self


class OpeningSceneContext(FrozenSoftReviewModel):
    position: int = Field(ge=0)
    narration: str = Field(min_length=1, max_length=8_000)
    observed_start_seconds: float = Field(ge=0, le=I6_HOOK_MAX_SECONDS)
    observed_end_seconds: float = Field(gt=0, le=86_400)

    @model_validator(mode="after")
    def require_ordered_duration(self) -> OpeningSceneContext:
        if self.observed_end_seconds <= self.observed_start_seconds:
            raise ValueError("opening scene end must be after its start")
        return self


class SelectedPackagingContext(FrozenSoftReviewModel):
    """The selected public packaging surfaces relevant to an opening review."""

    concept_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=180)
    thumbnail_concept: str = Field(min_length=1, max_length=1_000)
    thumbnail_text: str = Field(default="", max_length=160)
    viewer_trigger: str = Field(min_length=1, max_length=1_000)
    promise: str = Field(min_length=1, max_length=1_000)


class CanonicalViewerPromiseContext(FrozenSoftReviewModel):
    viewer_promise: str = Field(min_length=1, max_length=4_000)
    core_question: str = Field(min_length=1, max_length=4_000)
    why_now: str = Field(min_length=1, max_length=4_000)
    stakes: str = Field(min_length=1, max_length=4_000)
    novelty: str = Field(min_length=1, max_length=4_000)
    target_viewer: str = Field(min_length=1, max_length=4_000)
    broad_interest_bridge: str = Field(min_length=1, max_length=4_000)
    expected_takeaway: str = Field(min_length=1, max_length=4_000)


class SoftReviewContext(FrozenSoftReviewModel):
    campaign_id: int = Field(ge=1)
    selected_packaging_concept: SelectedPackagingContext
    viewer_promise: CanonicalViewerPromiseContext
    cold_open_narration: str = Field(min_length=1, max_length=20_000)
    continuation_reason: str = Field(min_length=1, max_length=4_000)
    opening_scenes: tuple[OpeningSceneContext, ...] = Field(min_length=1, max_length=64)
    review_boundary_seconds: float = Field(ge=0, le=I6_HOOK_MAX_SECONDS)

    @model_validator(mode="after")
    def require_ordered_opening_scenes(self) -> SoftReviewContext:
        if [scene.position for scene in self.opening_scenes] != list(range(len(self.opening_scenes))):
            raise ValueError("opening scenes must be canonically ordered from position zero")
        if any(later.observed_start_seconds < earlier.observed_end_seconds for earlier, later in zip(self.opening_scenes, self.opening_scenes[1:])):
            raise ValueError("opening scene timing must not overlap")
        return self


class SoftReviewRequest(FrozenSoftReviewModel):
    policy_version: Literal["i6-soft-review-v1"] = I6_SOFT_REVIEW_POLICY_VERSION
    machine_input_sha256: str = Field(min_length=64, max_length=64)
    machine_result_sha256: str = Field(min_length=64, max_length=64)
    context: SoftReviewContext
    targets: tuple[SoftReviewTarget, ...] = Field(min_length=1, max_length=4)

    @field_validator("machine_input_sha256", "machine_result_sha256")
    @classmethod
    def require_request_hashes(cls, value: str, info: object) -> str:
        return _require_sha256(value, getattr(info, "field_name", "hash"))

    @model_validator(mode="after")
    def require_bound_unique_targets(self) -> SoftReviewRequest:
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("soft review targets must be unique")
        if any(target.machine_input_sha256 != self.machine_input_sha256 or target.machine_result_sha256 != self.machine_result_sha256 for target in self.targets):
            raise ValueError("soft review targets must bind this machine input and result")
        return self

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class CriticJudgment(FrozenSoftReviewModel):
    target_id: str = Field(min_length=64, max_length=64)
    outcome: GateOutcome
    rationale: str = Field(min_length=1, max_length=2_000)
    observations: tuple[str, ...] = Field(default_factory=tuple, max_length=24)

    @field_validator("target_id")
    @classmethod
    def require_target_hash(cls, value: str) -> str:
        return _require_sha256(value, "target_id")

    @field_validator("observations")
    @classmethod
    def require_bounded_observations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 1_000 for value in values):
            raise ValueError("observations must contain bounded nonblank strings")
        return values


class CriticResponse(FrozenSoftReviewModel):
    """Parsed typed critic response; its hash is the B1 provider-response identity."""

    judgments: tuple[CriticJudgment, ...] = Field(min_length=1, max_length=4)

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class CriticArtifact(FrozenSoftReviewModel):
    """Immutable critic evidence; response hash binds parsed typed judgments, not transport bytes."""

    policy_version: Literal["i6-soft-review-v1"] = I6_SOFT_REVIEW_POLICY_VERSION
    request_sha256: str = Field(min_length=64, max_length=64)
    machine_input_sha256: str = Field(min_length=64, max_length=64)
    machine_result_sha256: str = Field(min_length=64, max_length=64)
    provider: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=240)
    prompt_template_version: str = Field(min_length=1, max_length=160)
    provider_response_sha256: str = Field(min_length=64, max_length=64)
    provider_response: CriticResponse
    judgments: tuple[CriticJudgment, ...] = Field(min_length=1, max_length=4)

    @field_validator("request_sha256", "machine_input_sha256", "machine_result_sha256", "provider_response_sha256")
    @classmethod
    def require_artifact_hashes(cls, value: str, info: object) -> str:
        return _require_sha256(value, getattr(info, "field_name", "hash"))

    @field_validator("provider", "model", "prompt_template_version")
    @classmethod
    def require_nonblank_metadata(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("critic provider metadata must be nonblank")
        return value

    @model_validator(mode="after")
    def require_response_bound_to_judgments(self) -> CriticArtifact:
        if self.provider_response.judgments != self.judgments:
            raise ValueError("provider_response judgments must equal critic artifact judgments")
        if self.provider_response_sha256 != self.provider_response.sha256():
            raise ValueError("provider_response_sha256 must bind the parsed typed critic response")
        return self

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ResolvedSoftJudgment(FrozenSoftReviewModel):
    target_id: str = Field(min_length=64, max_length=64)
    check_id: str = Field(min_length=1, max_length=160)
    outcome: GateOutcome
    rationale: str = Field(min_length=1, max_length=2_000)
    observations: tuple[str, ...] = Field(default_factory=tuple, max_length=24)


class UnresolvedReviewFinding(FrozenSoftReviewModel):
    finding_kind: Literal["machine", "human"]
    check_id: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1_000)
    evidence: tuple[str, ...] = Field(default_factory=tuple, max_length=32)


class SoftReviewResolution(FrozenSoftReviewModel):
    policy_version: Literal["i6-soft-review-v1"] = I6_SOFT_REVIEW_POLICY_VERSION
    base_machine_result_sha256: str = Field(min_length=64, max_length=64)
    critic_artifact_sha256: str = Field(min_length=64, max_length=64)
    original_findings: tuple[MachineQAFinding, ...]
    original_review_findings: tuple[HumanReviewFinding, ...]
    resolved_soft_judgments: tuple[ResolvedSoftJudgment, ...]
    remaining_unresolved_review_findings: tuple[UnresolvedReviewFinding, ...]
    outcome: GateOutcome

    @field_validator("base_machine_result_sha256", "critic_artifact_sha256")
    @classmethod
    def require_resolution_hashes(cls, value: str, info: object) -> str:
        return _require_sha256(value, getattr(info, "field_name", "hash"))

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))
