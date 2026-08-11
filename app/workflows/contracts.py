from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, TypeAlias

from app.db.models_content import RUNTIME_STAGES


StageName: TypeAlias = Literal[
    "topic",
    "research",
    "script",
    "storyboard",
    "media",
    "assembly",
    "machine_qa",
    "private_upload",
    "human_approval",
    "release",
]
GateOutcome: TypeAlias = Literal["PASS", "FAIL", "NEEDS_HUMAN"]

I3_PROVIDER_STAGES: tuple[StageName, ...] = (
    "topic",
    "research",
    "script",
    "storyboard",
    "media",
    "assembly",
    "machine_qa",
    "private_upload",
)
I3_WORKFLOW_STAGES: tuple[StageName, ...] = (*I3_PROVIDER_STAGES, "human_approval")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StageRequest:
    campaign_id: int
    stage: StageName
    policy_version: str
    input_json: str
    input_hash: str

    @classmethod
    def build(
        cls,
        *,
        campaign_id: int,
        stage: StageName,
        policy_version: str,
    ) -> StageRequest:
        if campaign_id <= 0:
            raise ValueError("campaign_id must be positive")
        if stage not in RUNTIME_STAGES:
            raise ValueError(f"Unknown runtime stage: {stage}")
        normalized_policy = policy_version.strip()
        if not normalized_policy:
            raise ValueError("policy_version must not be empty")
        payload = {
            "campaign_id": campaign_id,
            "contract_version": "i3-stage-request-v1",
            "policy_version": normalized_policy,
            "stage": stage,
        }
        input_json = canonical_json(payload)
        return cls(
            campaign_id=campaign_id,
            stage=stage,
            policy_version=normalized_policy,
            input_json=input_json,
            input_hash=sha256_text(input_json),
        )


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    model: str
    provider_job_id: str
    input_hash: str
    output_json: str
    output_hash: str
    artifact_kind: str
    artifact_uri: str
    mime_type: str
    byte_size: int
    usage_json: str
    cost_microunits: int


@dataclass(frozen=True, slots=True)
class GateResult:
    outcome: GateOutcome
    reasons: tuple[str, ...]
    input_hash: str
    output_hash: str | None
    policy_version: str


@dataclass(frozen=True, slots=True)
class PersistedStageResult:
    campaign_id: int
    stage: StageName
    outcome: GateOutcome
    current_stage: StageName
    generation_job_id: int | None
    artifact_id: int | None
    gate_decision_id: int
    input_hash: str
    output_hash: str | None
    replayed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "campaign_id": self.campaign_id,
            "current_stage": self.current_stage,
            "gate_decision_id": self.gate_decision_id,
            "generation_job_id": self.generation_job_id,
            "input_hash": self.input_hash,
            "outcome": self.outcome,
            "output_hash": self.output_hash,
            "replayed": self.replayed,
            "stage": self.stage,
        }
