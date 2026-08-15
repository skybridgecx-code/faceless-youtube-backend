from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from tools.project_agent_runtime.compatibility import project_profile_from_legacy
from tools.project_agent_runtime.config import load_project_config
from tools.project_agent_runtime.core.contracts import (
    ActionRequest,
    ActionResult,
    ArchitectureSnapshotRef,
    ArtifactRef,
    Attempt,
    AttemptStatus,
    EvidenceBinding,
    EnvironmentHandle,
    EnvironmentSnapshot,
    EnvironmentSpec,
    ExecutionContext,
    ExecutionResult,
    FailureClass,
    PlanResult,
    PolicyBundle,
    PolicyDecision,
    PolicyOutcome,
    ProjectProfile,
    RiskClass,
    RunEvent,
    RunSnapshot,
    StepSpec,
    TaskSpec,
)
from tools.project_agent_runtime.core.protocols import (
    AgentExecutor,
    ArtifactStore,
    ExecutionEnvironment,
)


ROOT = Path(__file__).resolve().parents[1]
SHA256 = "a" * 64
GIT_SHA = "b" * 40
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _profile(*, display_name: str = "YouMo") -> ProjectProfile:
    config = load_project_config(ROOT, "tools/project_agent_runtime/projects/youmo.json")
    return project_profile_from_legacy(
        config.__class__(
            schema_version=config.schema_version,
            project_id=config.project_id,
            display_name=display_name,
            repository=config.repository,
            canonical_branch=config.canonical_branch,
            allowed_branch_prefixes=config.allowed_branch_prefixes,
            execution_branch_prefixes=config.execution_branch_prefixes,
            architecture_lock=config.architecture_lock,
            architecture_sources=config.architecture_sources,
            state_dir=config.state_dir,
            codex=config.codex,
        )
    )


def _snapshot() -> RunSnapshot:
    profile = _profile()
    return RunSnapshot(
        run_id="run-1",
        project_id=profile.project_id,
        created_at=NOW,
        profile_sha256=profile.profile_sha256,
        architecture=ArchitectureSnapshotRef(snapshot_id="architecture-v1", sha256=SHA256),
        policies=(PolicyBundle(policy_id="policy-v1", sha256=SHA256),),
        lifecycle_backend_id="manifest",
        lifecycle_schema_version=1,
        durability_backend_id="manifest",
        durability_schema_version=1,
        repository=profile.repository.repository,
        base_git_sha=GIT_SHA,
        environment_catalog_version="environment-v1",
        tool_catalog_version="tools-v1",
        model_route_version="route-v1",
        route_certification_version="certification-v1",
        allowed_capabilities=("repo.read",),
        budget_micro_usd=0,
        trace_id="trace-1",
    )


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        requested_outcome="validate contracts",
        input_artifacts=(),
        constraints=("read-only",),
        dependencies=(),
        acceptance_criteria=("tests pass",),
        time_limit_seconds=60,
        budget_micro_usd=0,
    )


def test_project_profile_v2_construction_and_json_schema() -> None:
    profile = _profile()

    assert profile.schema_version == 2
    assert profile.repository.repository == "skybridgecx-code/faceless-youtube-backend"
    assert profile.allowed_capabilities == ()
    assert profile.policy_bundles == ()
    assert profile.environment_classes == ()
    assert profile.retention_limit_days is None
    assert profile.budget_limit_micro_usd is None
    schema = ProjectProfile.model_json_schema()
    assert schema["properties"]["schema_version"]["const"] == 2
    assert {
        "repository",
        "architecture",
        "execution",
        "validation",
        "harness",
        "allowed_capabilities",
        "policy_bundles",
        "environment_classes",
        "retention_limit_days",
        "budget_limit_micro_usd",
    } <= set(schema["properties"])


def test_legacy_youmo_v1_adapter_is_lossless_and_manifest_is_unchanged() -> None:
    manifest_path = ROOT / "tools/project_agent_runtime/projects/youmo.json"
    original = manifest_path.read_bytes()
    config = load_project_config(ROOT, manifest_path)
    profile = project_profile_from_legacy(config)

    assert config.schema_version == 1
    assert profile.project_id == config.project_id
    assert profile.display_name == config.display_name
    assert profile.repository.repository == config.repository
    assert profile.repository.canonical_branch == config.canonical_branch
    assert profile.repository.allowed_branch_prefixes == config.allowed_branch_prefixes
    assert profile.repository.execution_branch_prefixes == config.execution_branch_prefixes
    assert profile.architecture.lock_path == config.architecture_lock
    assert profile.architecture.source_paths == config.architecture_sources
    assert profile.execution.state_directory == config.state_dir
    assert profile.harness.adapter_id == "codex"
    assert profile.harness.sdk_requirement == config.codex.sdk_requirement
    assert profile.harness.implementation_model == config.codex.implementation_model
    assert profile.harness.implementation_reasoning == config.codex.implementation_reasoning
    assert profile.harness.audit_model == config.codex.audit_model
    assert profile.harness.audit_reasoning == config.codex.audit_reasoning
    assert profile.validation.require_explicit_execute == config.codex.require_explicit_execute
    assert manifest_path.read_bytes() == original


def test_profile_hash_and_serialization_are_deterministic() -> None:
    profile = _profile()
    equivalent = ProjectProfile.model_validate_json(profile.canonical_json())

    assert profile.canonical_json() == equivalent.canonical_json()
    assert profile.profile_sha256 == equivalent.profile_sha256
    assert profile.profile_sha256 == hashlib.sha256(
        profile.canonical_json().encode("utf-8")
    ).hexdigest()


def test_profile_hash_changes_when_governed_content_changes() -> None:
    assert _profile(display_name="YouMo").profile_sha256 != _profile(
        display_name="Other project"
    ).profile_sha256
    assert _profile().profile_sha256 != ProjectProfile.model_validate(
        _profile().model_dump(mode="python") | {"allowed_capabilities": ("repo.read",)}
    ).profile_sha256


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ("not-a-repository", "repository"),
        ("owner/too/many", "repository"),
        ("owner/", "repository"),
        ("../repo", "repository"),
        ("owner/..", "repository"),
    ],
)
def test_invalid_repository_input_fails(value: str, field: str) -> None:
    payload = _profile().model_dump(mode="python")
    payload["repository"][field] = value

    with pytest.raises(ValidationError):
        ProjectProfile.model_validate(payload)


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (ArtifactRef, "sha256", "A" * 64),
        (ArchitectureSnapshotRef, "sha256", "a" * 63),
        (RunSnapshot, "base_git_sha", "b" * 39),
    ],
)
def test_invalid_sha_and_git_sha_fail(factory: type[object], field: str, value: str) -> None:
    if factory is ArtifactRef:
        payload = {"sha256": SHA256, "size_bytes": 1, "media_type": "text/plain"}
    elif factory is ArchitectureSnapshotRef:
        payload = {"snapshot_id": "architecture-v1", "sha256": SHA256}
    else:
        payload = _snapshot().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        factory(**payload)  # type: ignore[operator]


def test_unsupported_schema_versions_and_secret_fields_fail_closed() -> None:
    payload = _profile().model_dump(mode="python")
    payload["schema_version"] = 1
    with pytest.raises(ValidationError):
        ProjectProfile.model_validate(payload)

    payload = _profile().model_dump(mode="python")
    payload["api_key"] = "not-permitted"
    with pytest.raises(ValidationError):
        ProjectProfile.model_validate(payload)

    action = {
        "action_id": "action-1",
        "action_digest": SHA256,
        "arguments_sha256": SHA256,
        "target": "repository",
        "risk_class": RiskClass.R0_READ_ONLY,
        "capabilities": ("repo.read",),
        "idempotency_key": "key-1",
        "secret": "not-permitted",
    }
    with pytest.raises(ValidationError):
        ActionRequest(**action)

    with pytest.raises(ValidationError):
        ArtifactRef(sha256=SHA256, size_bytes="1", media_type="text/plain")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        ProjectProfile.model_validate(
            _profile().model_dump(mode="python")
            | {"validation": {"schema_version": 1, "require_explicit_execute": "false"}}
        )

    config = load_project_config(ROOT, "tools/project_agent_runtime/projects/youmo.json")
    with pytest.raises(ValueError, match="unsupported legacy project config"):
        project_profile_from_legacy(replace(config, schema_version=999))


def test_historical_contracts_are_frozen() -> None:
    artifact = ArtifactRef(sha256=SHA256, size_bytes=1, media_type="text/plain")

    with pytest.raises(ValidationError):
        artifact.size_bytes = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        {"sha256": "x" * 64, "size_bytes": 1, "media_type": "text/plain"},
        {"sha256": SHA256, "size_bytes": -1, "media_type": "text/plain"},
        {"sha256": SHA256, "size_bytes": 1, "media_type": ""},
    ],
)
def test_artifact_ref_rejects_invalid_sha_and_size(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(**payload)


def test_run_snapshot_requires_complete_provenance() -> None:
    payload = _snapshot().model_dump(mode="python")
    payload.pop("tool_catalog_version")
    with pytest.raises(ValidationError):
        RunSnapshot(**payload)

    payload = _snapshot().model_dump(mode="python")
    payload["created_at"] = datetime(2026, 8, 15, 12, 0)
    with pytest.raises(ValidationError):
        RunSnapshot(**payload)

    payload = _snapshot().model_dump(mode="python")
    payload["policies"] = ()
    with pytest.raises(ValidationError):
        RunSnapshot(**payload)


def test_run_event_enforces_schema_sequence_and_timezone() -> None:
    event = RunEvent(
        event_id="event-1",
        run_id="run-1",
        sequence=1,
        event_type="CREATED",
        occurred_at=NOW,
        payload_sha256=SHA256,
    )
    assert event.occurred_at.tzinfo == timezone.utc

    for field, value in (
        ("schema_version", 2),
        ("sequence", 0),
        ("occurred_at", datetime(2026, 8, 15, 12, 0)),
    ):
        payload = event.model_dump(mode="python")
        payload[field] = value
        with pytest.raises(ValidationError):
            RunEvent(**payload)


def test_risk_class_and_policy_decision_round_trip() -> None:
    decision = PolicyDecision(
        action_id="action-1",
        outcome=PolicyOutcome.APPROVAL_REQUIRED,
        policy_sha256=SHA256,
        decided_at=NOW,
    )

    assert RiskClass.R3_CONSEQUENTIAL.value == "R3_CONSEQUENTIAL"
    assert PolicyDecision.model_validate_json(decision.canonical_json()) == decision


def test_attempt_requires_failure_for_failed_status_and_is_immutable() -> None:
    payload = {
        "attempt_id": "attempt-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "attempt_number": 1,
        "status": AttemptStatus.FAILED,
        "started_at": NOW,
    }
    with pytest.raises(ValidationError):
        Attempt(**payload)

    attempt = Attempt(**(payload | {"failure_class": FailureClass.TOOL_ERROR}))
    with pytest.raises(ValidationError):
        attempt.status = AttemptStatus.SUCCEEDED  # type: ignore[misc]

    with pytest.raises(ValidationError):
        Attempt(
            **(payload | {"failure_class": FailureClass.TOOL_ERROR, "attempt_number": 2})
        )
    with pytest.raises(ValidationError):
        Attempt(
            **(
                payload
                | {
                    "failure_class": FailureClass.TOOL_ERROR,
                    "predecessor_attempt_id": "attempt-0",
                }
            )
        )
    with pytest.raises(ValidationError):
        Attempt(
            **(
                payload
                | {
                    "failure_class": FailureClass.TOOL_ERROR,
                    "attempt_number": 2,
                    "predecessor_attempt_id": "",
                }
            )
        )


def test_generic_core_imports_are_product_independent() -> None:
    core_root = ROOT / "tools/project_agent_runtime/core"
    forbidden = {"app", "youtube", "youtube_api", "youtube_client"}

    for path in core_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0
        )
        assert not imported & forbidden, f"product dependency in {path}: {imported & forbidden}"


def test_protocols_are_generic_contract_ports() -> None:
    class StubExecutor:
        async def plan(self, task: TaskSpec, ctx: ExecutionContext) -> PlanResult:
            return PlanResult(plan_sha256=SHA256, steps=())

        async def execute(self, task: TaskSpec, ctx: ExecutionContext) -> ExecutionResult:
            attempt = Attempt(
                attempt_id=ctx.attempt_id,
                run_id=ctx.run_snapshot.run_id,
                task_id=task.task_id,
                attempt_number=1,
                status=AttemptStatus.SUCCEEDED,
                started_at=NOW,
                finished_at=NOW,
            )
            return ExecutionResult(attempt=attempt, artifacts=(), completed_at=NOW)

    class StubEnvironment:
        async def prepare(self, spec: EnvironmentSpec) -> EnvironmentHandle:
            return EnvironmentHandle(environment_id=spec.environment_id, handle_id="handle-1")

        async def execute(
            self, handle: EnvironmentHandle, action: ActionRequest
        ) -> ActionResult:
            return ActionResult(
                action_id=action.action_id,
                outcome="SUCCEEDED",
                observed_at=NOW,
                evidence=(),
            )

        async def snapshot(self, handle: EnvironmentHandle) -> EnvironmentSnapshot:
            return EnvironmentSnapshot(
                environment_id=handle.environment_id,
                captured_at=NOW,
                sha256=SHA256,
            )

        async def cleanup(self, handle: EnvironmentHandle) -> None:
            return None

    class StubArtifactStore:
        async def put(self, content: bytes, *, media_type: str) -> ArtifactRef:
            return ArtifactRef(sha256=SHA256, size_bytes=len(content), media_type=media_type)

        async def get(self, ref: ArtifactRef) -> bytes:
            return b""

        async def verify(self, ref: ArtifactRef) -> bool:
            return True

    assert isinstance(StubExecutor(), AgentExecutor)
    assert isinstance(StubEnvironment(), ExecutionEnvironment)
    assert isinstance(StubArtifactStore(), ArtifactStore)
    assert EnvironmentSpec(
        environment_id="local",
        workspace_root="/workspace",
        network_access=False,
    ).network_access is False


def test_current_runtime_has_no_event_store_dependency_or_usage_record_duplicate() -> None:
    runtime_root = ROOT / "tools/project_agent_runtime"
    current_modules = [
        path
        for path in runtime_root.glob("*.py")
        if path.name not in {"compatibility.py"}
    ]
    assert all("RunEventStore" not in path.read_text(encoding="utf-8") for path in current_modules)

    usage_record_definitions = [
        path
        for path in runtime_root.rglob("*.py")
        if "class UsageRecord" in path.read_text(encoding="utf-8")
    ]
    assert usage_record_definitions == [runtime_root / "usage.py"]


def test_execution_context_and_task_are_contract_only() -> None:
    snapshot = _snapshot()
    context = ExecutionContext(run_snapshot=snapshot, attempt_id="attempt-1", trace_id="trace-1")

    assert context.run_snapshot == snapshot
    assert _task().budget_micro_usd == 0
    with pytest.raises(ValidationError):
        ExecutionContext(run_snapshot=snapshot, attempt_id="attempt-1", trace_id="other")


def test_step_and_evidence_contracts_are_deterministic() -> None:
    step = StepSpec(
        step_id="step-1",
        step_kind="DETERMINISTIC",
        preconditions=("input present",),
        expected_outputs=("report",),
        recovery_class="RETRY_SAFE",
        risk_class=RiskClass.R0_READ_ONLY,
    )
    evidence = EvidenceBinding(
        artifact=ArtifactRef(sha256=SHA256, size_bytes=0, media_type="application/json"),
        purpose="validation evidence",
    )

    assert step.content_sha256() == StepSpec.model_validate_json(step.canonical_json()).content_sha256()
    assert evidence.artifact.size_bytes == 0
