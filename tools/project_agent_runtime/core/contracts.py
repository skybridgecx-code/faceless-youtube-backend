"""Frozen, product-neutral platform contracts.

The M1 contracts intentionally describe only facts and replaceable boundaries.
They are not persistence implementations and do not make ``RunEvent`` active
for existing YouMo runs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ContractValidationError(ValueError):
    """Raised when a product-neutral contract cannot represent a safe fact."""


class RiskClass(str, Enum):
    R0_READ_ONLY = "R0_READ_ONLY"
    R1_LOCAL_REVERSIBLE = "R1_LOCAL_REVERSIBLE"
    R2_EXTERNAL_REVERSIBLE = "R2_EXTERNAL_REVERSIBLE"
    R3_CONSEQUENTIAL = "R3_CONSEQUENTIAL"
    R4_IRREVERSIBLE_HIGH_IMPACT = "R4_IRREVERSIBLE_HIGH_IMPACT"


class PolicyOutcome(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


class AttemptStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class FailureClass(str, Enum):
    MODEL_ERROR = "MODEL_ERROR"
    MODEL_REFUSAL = "MODEL_REFUSAL"
    TOOL_ERROR = "TOOL_ERROR"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    POLICY_DENIED = "POLICY_DENIED"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    ARCHITECTURE_DRIFT = "ARCHITECTURE_DRIFT"
    REPOSITORY_DRIFT = "REPOSITORY_DRIFT"
    WORKSPACE_DIRTY = "WORKSPACE_DIRTY"
    LEASE_CONFLICT = "LEASE_CONFLICT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    EVAL_REGRESSION = "EVAL_REGRESSION"
    EXTERNAL_STATE_CONFLICT = "EXTERNAL_STATE_CONFLICT"
    UNKNOWN = "UNKNOWN"


class _FrozenContract(BaseModel):
    """Common deterministic serialization and immutable-fact policy."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    def canonical_json(self) -> str:
        """Return a stable JSON representation suitable for content hashing."""

        return json.dumps(
            self.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def content_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _required_text(value: str) -> str:
    if not value.strip():
        raise ValueError("must be a non-empty string")
    return value


def _validate_sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


def _validate_git_sha(value: str) -> str:
    if not _GIT_SHA_RE.fullmatch(value):
        raise ValueError("must be a lowercase 40-character Git SHA")
    return value


def _validate_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must include a timezone")
    return value.astimezone(timezone.utc)


class RepositorySpec(_FrozenContract):
    schema_version: Literal[1] = 1
    repository: str
    canonical_branch: str
    allowed_branch_prefixes: tuple[str, ...]
    execution_branch_prefixes: tuple[str, ...]

    @field_validator("repository")
    @classmethod
    def _repository_is_owner_name(cls, value: str) -> str:
        normalized = value.removesuffix(".git")
        if not _REPOSITORY_RE.fullmatch(normalized):
            raise ValueError("must use owner/name form")
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ValueError("must not contain path traversal components")
        return normalized

    @field_validator("canonical_branch")
    @classmethod
    def _branch_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("allowed_branch_prefixes", "execution_branch_prefixes")
    @classmethod
    def _prefixes_are_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("must contain non-empty branch prefixes")
        return value


class ArchitectureSpec(_FrozenContract):
    schema_version: Literal[1] = 1
    lock_path: str
    source_paths: tuple[str, ...]

    @field_validator("lock_path")
    @classmethod
    def _lock_path_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("source_paths")
    @classmethod
    def _sources_are_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("must contain non-empty architecture source paths")
        return value


class ExecutionSpec(_FrozenContract):
    schema_version: Literal[1] = 1
    state_directory: str

    @field_validator("state_directory")
    @classmethod
    def _state_directory_is_present(cls, value: str) -> str:
        return _required_text(value)


class ValidationSpec(_FrozenContract):
    schema_version: Literal[1] = 1
    require_explicit_execute: bool


class HarnessSpec(_FrozenContract):
    schema_version: Literal[1] = 1
    adapter_id: str
    sdk_requirement: str
    implementation_model: str
    implementation_reasoning: str
    audit_model: str
    audit_reasoning: str

    @field_validator(
        "adapter_id",
        "sdk_requirement",
        "implementation_model",
        "implementation_reasoning",
        "audit_model",
        "audit_reasoning",
    )
    @classmethod
    def _harness_text_is_present(cls, value: str) -> str:
        return _required_text(value)


class ArchitectureSnapshotRef(_FrozenContract):
    schema_version: Literal[1] = 1
    snapshot_id: str
    sha256: str

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("sha256")
    @classmethod
    def _snapshot_hash_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)


class PolicyBundle(_FrozenContract):
    schema_version: Literal[1] = 1
    policy_id: str
    sha256: str

    @field_validator("policy_id")
    @classmethod
    def _policy_id_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("sha256")
    @classmethod
    def _policy_hash_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)


class ProjectProfile(_FrozenContract):
    """Schema-v2, product-neutral project identity and harness configuration."""

    schema_version: Literal[2] = 2
    project_id: str
    display_name: str
    repository: RepositorySpec
    architecture: ArchitectureSpec
    execution: ExecutionSpec
    validation: ValidationSpec
    harness: HarnessSpec
    allowed_capabilities: tuple[str, ...] = ()
    policy_bundles: tuple[PolicyBundle, ...] = ()
    environment_classes: tuple[str, ...] = ()
    retention_limit_days: int | None = Field(default=None, ge=1)
    budget_limit_micro_usd: int | None = Field(default=None, ge=0)

    @field_validator("project_id", "display_name")
    @classmethod
    def _profile_text_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("allowed_capabilities", "environment_classes")
    @classmethod
    def _profile_boundary_items_are_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("must contain only non-empty strings")
        return value

    @property
    def profile_sha256(self) -> str:
        return self.content_sha256()


class ArtifactRef(_FrozenContract):
    schema_version: Literal[1] = 1
    sha256: str
    size_bytes: int = Field(ge=0)
    media_type: str

    @field_validator("sha256")
    @classmethod
    def _artifact_hash_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("media_type")
    @classmethod
    def _media_type_is_present(cls, value: str) -> str:
        return _required_text(value)


class EvidenceBinding(_FrozenContract):
    schema_version: Literal[1] = 1
    artifact: ArtifactRef
    purpose: str

    @field_validator("purpose")
    @classmethod
    def _purpose_is_present(cls, value: str) -> str:
        return _required_text(value)


class RunSnapshot(_FrozenContract):
    """Creation/provenance vocabulary; not a live lifecycle record in M1."""

    schema_version: Literal[1] = 1
    run_id: str
    project_id: str
    created_at: datetime
    profile_sha256: str
    architecture: ArchitectureSnapshotRef
    policies: tuple[PolicyBundle, ...] = Field(min_length=1)
    lifecycle_backend_id: str
    lifecycle_schema_version: int = Field(ge=1)
    durability_backend_id: str
    durability_schema_version: int = Field(ge=1)
    repository: str
    base_git_sha: str
    environment_catalog_version: str
    tool_catalog_version: str
    model_route_version: str
    route_certification_version: str
    allowed_capabilities: tuple[str, ...]
    budget_micro_usd: int = Field(ge=0)
    trace_id: str

    @field_validator(
        "run_id",
        "project_id",
        "lifecycle_backend_id",
        "durability_backend_id",
        "environment_catalog_version",
        "tool_catalog_version",
        "model_route_version",
        "route_certification_version",
        "trace_id",
    )
    @classmethod
    def _snapshot_text_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("profile_sha256")
    @classmethod
    def _profile_hash_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("repository")
    @classmethod
    def _snapshot_repository_is_valid(cls, value: str) -> str:
        normalized = value.removesuffix(".git")
        if not _REPOSITORY_RE.fullmatch(normalized):
            raise ValueError("must use owner/name form")
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ValueError("must not contain path traversal components")
        return normalized

    @field_validator("base_git_sha")
    @classmethod
    def _base_git_sha_is_valid(cls, value: str) -> str:
        return _validate_git_sha(value)

    @field_validator("allowed_capabilities")
    @classmethod
    def _capabilities_are_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("must contain only non-empty capabilities")
        return value

    @field_validator("created_at")
    @classmethod
    def _created_at_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)


class ExecutionContext(_FrozenContract):
    schema_version: Literal[1] = 1
    run_snapshot: RunSnapshot
    attempt_id: str
    trace_id: str

    @field_validator("attempt_id", "trace_id")
    @classmethod
    def _context_text_is_present(cls, value: str) -> str:
        return _required_text(value)

    @model_validator(mode="after")
    def _context_trace_matches_snapshot(self) -> "ExecutionContext":
        if self.trace_id != self.run_snapshot.trace_id:
            raise ValueError("trace_id must match the run snapshot")
        return self


class TaskSpec(_FrozenContract):
    schema_version: Literal[1] = 1
    task_id: str
    requested_outcome: str
    input_artifacts: tuple[ArtifactRef, ...]
    constraints: tuple[str, ...]
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    time_limit_seconds: int = Field(gt=0)
    budget_micro_usd: int = Field(ge=0)

    @field_validator("task_id", "requested_outcome")
    @classmethod
    def _task_text_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("constraints", "dependencies", "acceptance_criteria")
    @classmethod
    def _task_text_items_are_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("must contain only non-empty strings")
        return value


class Attempt(_FrozenContract):
    schema_version: Literal[1] = 1
    attempt_id: str
    run_id: str
    task_id: str
    attempt_number: int = Field(ge=1)
    status: AttemptStatus
    started_at: datetime
    finished_at: datetime | None = None
    predecessor_attempt_id: str | None = None
    failure_class: FailureClass | None = None

    @field_validator("attempt_id", "run_id", "task_id")
    @classmethod
    def _attempt_text_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("predecessor_attempt_id")
    @classmethod
    def _predecessor_attempt_id_is_present(cls, value: str | None) -> str | None:
        return None if value is None else _required_text(value)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _attempt_times_are_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _validate_aware_datetime(value)

    @model_validator(mode="after")
    def _attempt_is_temporally_consistent(self) -> "Attempt":
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.status == AttemptStatus.FAILED and self.failure_class is None:
            raise ValueError("failed attempts require a failure_class")
        if self.attempt_number == 1 and self.predecessor_attempt_id is not None:
            raise ValueError("first attempts must not have a predecessor_attempt_id")
        if self.attempt_number > 1 and self.predecessor_attempt_id is None:
            raise ValueError("retry attempts require a predecessor_attempt_id")
        if self.predecessor_attempt_id == self.attempt_id:
            raise ValueError("predecessor_attempt_id cannot equal attempt_id")
        return self


class RunEvent(_FrozenContract):
    """Target-only immutable event vocabulary; M1 does not write these events."""

    schema_version: Literal[1] = 1
    event_id: str
    run_id: str
    sequence: int = Field(ge=1)
    event_type: str
    occurred_at: datetime
    payload_sha256: str

    @field_validator("event_id", "run_id", "event_type")
    @classmethod
    def _event_text_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("payload_sha256")
    @classmethod
    def _event_payload_hash_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("occurred_at")
    @classmethod
    def _event_time_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)


class ActionRequest(_FrozenContract):
    schema_version: Literal[1] = 1
    action_id: str
    action_digest: str
    arguments_sha256: str
    target: str
    risk_class: RiskClass
    capabilities: tuple[str, ...]
    idempotency_key: str

    @field_validator("action_id", "target", "idempotency_key")
    @classmethod
    def _action_text_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("action_digest", "arguments_sha256")
    @classmethod
    def _action_hashes_are_valid(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("capabilities")
    @classmethod
    def _action_capabilities_are_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("must contain non-empty capabilities")
        return value


class PolicyDecision(_FrozenContract):
    schema_version: Literal[1] = 1
    action_id: str
    outcome: PolicyOutcome
    policy_sha256: str
    decided_at: datetime

    @field_validator("action_id")
    @classmethod
    def _decision_action_id_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("policy_sha256")
    @classmethod
    def _decision_policy_hash_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("decided_at")
    @classmethod
    def _decision_time_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)


class StepSpec(_FrozenContract):
    schema_version: Literal[1] = 1
    step_id: str
    step_kind: Literal[
        "DETERMINISTIC",
        "MODEL_IO",
        "READ_ONLY_EXTERNAL",
        "REVERSIBLE_EFFECT",
        "CONSEQUENTIAL_EFFECT",
        "WAIT",
    ]
    preconditions: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    recovery_class: Literal[
        "RETRY_SAFE",
        "RECONCILE_THEN_RETRY",
        "RESUME_FROM_CHECKPOINT",
        "REQUIRES_APPROVAL",
        "REQUIRES_OPERATOR",
        "TERMINAL",
    ]
    risk_class: RiskClass

    @field_validator("step_id")
    @classmethod
    def _step_id_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("preconditions", "expected_outputs")
    @classmethod
    def _step_items_are_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("must contain only non-empty strings")
        return value


class WaitCondition(_FrozenContract):
    schema_version: Literal[1] = 1
    condition_id: str
    description: str
    deadline: datetime

    @field_validator("condition_id", "description")
    @classmethod
    def _wait_text_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("deadline")
    @classmethod
    def _deadline_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)


class PlanResult(_FrozenContract):
    schema_version: Literal[1] = 1
    plan_sha256: str
    steps: tuple[StepSpec, ...]

    @field_validator("plan_sha256")
    @classmethod
    def _plan_hash_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)


class ExecutionResult(_FrozenContract):
    schema_version: Literal[1] = 1
    attempt: Attempt
    artifacts: tuple[ArtifactRef, ...]
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def _completed_at_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)


class ActionResult(_FrozenContract):
    schema_version: Literal[1] = 1
    action_id: str
    outcome: Literal["SUCCEEDED", "FAILED", "UNCERTAIN"]
    observed_at: datetime
    evidence: tuple[EvidenceBinding, ...]

    @field_validator("action_id")
    @classmethod
    def _result_action_id_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)


class EnvironmentSpec(_FrozenContract):
    schema_version: Literal[1] = 1
    environment_id: str
    workspace_root: str
    network_access: bool

    @field_validator("environment_id", "workspace_root")
    @classmethod
    def _environment_spec_text_is_present(cls, value: str) -> str:
        return _required_text(value)


class EnvironmentHandle(_FrozenContract):
    schema_version: Literal[1] = 1
    environment_id: str
    handle_id: str

    @field_validator("environment_id", "handle_id")
    @classmethod
    def _environment_handle_text_is_present(cls, value: str) -> str:
        return _required_text(value)


class EnvironmentSnapshot(_FrozenContract):
    schema_version: Literal[1] = 1
    environment_id: str
    captured_at: datetime
    sha256: str

    @field_validator("environment_id")
    @classmethod
    def _environment_snapshot_id_is_present(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("captured_at")
    @classmethod
    def _environment_snapshot_time_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)

    @field_validator("sha256")
    @classmethod
    def _environment_snapshot_hash_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)
