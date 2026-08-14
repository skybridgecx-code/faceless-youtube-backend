import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/skybridgecx"
CONTRACT = json.loads((PACKAGE / "TARGET_CONTRACT.v1.json").read_text(encoding="utf-8"))

EXPECTED_NORMATIVE_DOCUMENTS = (
    "docs/skybridgecx/README.md",
    "docs/skybridgecx/ARCHITECTURE.md",
    "docs/skybridgecx/PLATFORM_CONTRACTS.md",
    "docs/skybridgecx/SECURITY_AND_CAPABILITIES.md",
    "docs/skybridgecx/EVALUATION_AND_MODEL_ROUTING.md",
    "docs/skybridgecx/DURABILITY_AND_RECOVERY.md",
    "docs/skybridgecx/EXECUTION_TOOLS_AND_MCP.md",
    "docs/skybridgecx/EVIDENCE_TELEMETRY_AND_COST.md",
    "docs/skybridgecx/DATA_AND_MIGRATION.md",
    "docs/skybridgecx/CONTROL_PLANE.md",
    "docs/skybridgecx/DEPLOYMENT_AND_PORTABILITY.md",
    "docs/skybridgecx/IMPLEMENTATION_ROADMAP.md",
)

EXPECTED_ADR_FILES = (
    "docs/adr/ADR-013-runtime-evolution-boundary.md",
    "docs/adr/ADR-014-lifecycle-and-durability-authority.md",
    "docs/adr/ADR-015-capability-policy-authority.md",
    "docs/adr/ADR-016-approval-idempotency-and-effect-receipts.md",
    "docs/adr/ADR-017-eval-certified-model-routing.md",
    "docs/adr/ADR-018-execution-environment-and-mcp-boundary.md",
    "docs/adr/ADR-019-evidence-otel-and-cost-provenance.md",
    "docs/adr/ADR-020-control-plane-command-and-persistence-model.md",
    "docs/adr/ADR-021-two-project-proof-before-extraction.md",
)

EXPECTED_ADRS = {
    "docs/adr/ADR-013-runtime-evolution-boundary.md": "# ADR-013: Runtime evolution boundary",
    "docs/adr/ADR-014-lifecycle-and-durability-authority.md": "# ADR-014: Lifecycle and durability authority",
    "docs/adr/ADR-015-capability-policy-authority.md": "# ADR-015: Capability and policy authority outside the model",
    "docs/adr/ADR-016-approval-idempotency-and-effect-receipts.md": "# ADR-016: Approval, idempotency, and effect receipts",
    "docs/adr/ADR-017-eval-certified-model-routing.md": "# ADR-017: Eval-certified model, tool, and harness routing",
    "docs/adr/ADR-018-execution-environment-and-mcp-boundary.md": "# ADR-018: Execution environment containment and MCP boundary",
    "docs/adr/ADR-019-evidence-otel-and-cost-provenance.md": "# ADR-019: Immutable evidence, OTel telemetry, and observed cost",
    "docs/adr/ADR-020-control-plane-command-and-persistence-model.md": "# ADR-020: Command control plane and persistence model",
    "docs/adr/ADR-021-two-project-proof-before-extraction.md": "# ADR-021: Two-project proof before extraction/package",
}

EXPECTED_ADR_DECISION_TERMS = {
    "docs/adr/ADR-013-runtime-evolution-boundary.md": (
        "Generalize the existing runtime", "do not create a second runtime", "extract a package yet",
    ),
    "docs/adr/ADR-014-lifecycle-and-durability-authority.md": (
        "exactly one lifecycle authority", "one durability backend per run", "only to newly created runs",
    ),
    "docs/adr/ADR-015-capability-policy-authority.md": (
        "Models propose; runtime policy decides", "DENY > APPROVAL_REQUIRED > scoped ALLOW > default",
    ),
    "docs/adr/ADR-016-approval-idempotency-and-effect-receipts.md": (
        "action_id", "action_digest", "idempotency_key", "reconcile uncertainty before retry",
    ),
    "docs/adr/ADR-017-eval-certified-model-routing.md": (
        "Require task-specific `RouteCertification`", "behavioral releases with eval evidence",
    ),
    "docs/adr/ADR-018-execution-environment-and-mcp-boundary.md": (
        "resolved-path policy", "deny-by-default networking", "MCP adapters outside MCP-independent domain types",
    ),
    "docs/adr/ADR-019-evidence-otel-and-cost-provenance.md": (
        "Content-address immutable artifacts", "OTel as telemetry vocabulary only", "without fabrication",
    ),
    "docs/adr/ADR-020-control-plane-command-and-persistence-model.md": (
        "RuntimeControlPort", "explicit schema migration", "optimistic versions",
    ),
    "docs/adr/ADR-021-two-project-proof-before-extraction.md": (
        "Forbid package extraction", "two real project profiles", "no package or canonical promotion",
    ),
}


def test_target_contract_identity_and_current_authority_boundary() -> None:
    assert CONTRACT["schema_version"] == 1
    assert "refinement" in CONTRACT["authority_role"]
    assert "not a replacement" in CONTRACT["authority_role"]
    assert "Conflict" in CONTRACT["contract_derivation_rule"]
    assert CONTRACT["parent_blueprint"]["sha256"] == (
        "e37b4a82b54e14a84ce0ac936395a370ebd327c52759653c2cde25ce05766fad"
    )
    assert CONTRACT["repository"] == {
        "branch_base": "youmo-clone-v2",
        "name": "skybridgecx-code/faceless-youtube-backend",
        "required_base_sha": "76c49a2092a140f1d63c4d17b68ab5b34b26eef2",
    }
    assert CONTRACT["generic_runtime_path"] == "tools/project_agent_runtime"
    assert CONTRACT["product_workflow_path"] == "app/workflows"
    assert "RunManifest" in CONTRACT["current_live_lifecycle_authority"]


def test_independent_document_and_adr_inventory_is_complete() -> None:
    assert CONTRACT["normative_documents"] == list(EXPECTED_NORMATIVE_DOCUMENTS)
    assert CONTRACT["adr_files"] == list(EXPECTED_ADR_FILES)
    for relative_path in (*EXPECTED_NORMATIVE_DOCUMENTS, *EXPECTED_ADR_FILES):
        assert (ROOT / relative_path).is_file(), relative_path


def test_global_invariants_and_m1_boundary() -> None:
    assert {
        "no_second_runtime",
        "exactly_one_lifecycle_authority_per_run",
        "one_durability_backend_per_run",
        "models_propose_runtime_policy_authorizes",
        "concurrent_writers_use_physically_separate_workspaces",
        "canonical_promotion_is_serialized",
        "consequential_effects_define_idempotency_and_reconciliation",
        "historical_accepted_evidence_is_immutable",
    } <= set(CONTRACT["global_invariants"])
    assert CONTRACT["minimum_project_profiles_before_package_extraction"] == 2
    assert set(CONTRACT["m1_compatibility_invariants"]) >= {
        "RunManifest compatibility lifecycle authority remains active",
        "no active RunEvent ledger migration",
        "no competing canonical UsageRecord",
        "package extraction is not authorized",
    }


def test_capability_policy_is_structured_and_exact() -> None:
    policy = CONTRACT["capability_policy"]
    assert policy["risk_classes"] == [
        "R0_READ_ONLY", "R1_LOCAL_REVERSIBLE", "R2_EXTERNAL_REVERSIBLE",
        "R3_CONSEQUENTIAL", "R4_IRREVERSIBLE_HIGH_IMPACT",
    ]
    assert CONTRACT["risk_classes"] == policy["risk_classes"]
    assert policy["precedence"] == [
        "DENY", "APPROVAL_REQUIRED", "SCOPED_ALLOW", "DEFAULT_POLICY",
    ]
    assert policy["risk_classification_grants_authority"] is False
    assert policy["representative_capabilities"] == [
        "repo.read", "repo.write", "shell.execute", "network.http", "secrets.read",
        "git.branch.create", "git.commit", "git.push", "github.pr.create",
        "database.read", "database.write", "email.send", "deploy.production",
        "architecture.modify", "policy.modify",
    ]


def test_action_authorization_is_structured_and_exact() -> None:
    authorization = CONTRACT["action_authorization"]
    assert authorization["action_id_role"] == "execution_identity"
    assert authorization["action_digest_role"] == "exact_authorization_approval_identity"
    assert authorization["idempotency_key_role"] == "duplicate_effect_prevention_identity"
    assert authorization["approval_bound_fields"] == [
        "project_id", "run_id", "action_digest", "arguments", "target", "risk_class",
        "capabilities", "artifact_hashes", "architecture_hash", "policy_hash",
        "protected_resource_state", "expiry",
    ]
    assert authorization["relevant_mutation_invalidates_approval"] is True
    assert authorization["single_use_by_default_risk_classes"] == [
        "R3_CONSEQUENTIAL", "R4_IRREVERSIBLE_HIGH_IMPACT",
    ]
    assert authorization["effect_states"] == [
        "CONFIRMED", "FAILED", "UNCERTAIN", "RECONCILED",
    ]
    assert authorization["uncertain_requires_reconciliation_before_retry"] is True


def test_model_routing_is_certified_and_fallbacks_are_exact() -> None:
    routing = CONTRACT["model_routing"]
    assert routing["certification_required"] is True
    assert routing["allowed_fallback_causes"] == [
        "PROVIDER_UNAVAILABLE", "RATE_LIMIT", "MODEL_UNAVAILABLE",
        "CAPABILITY_UNSUPPORTED", "CERTIFIED_COST_OPTIMIZATION",
    ]
    assert routing["prohibited_silent_fallback_causes"] == [
        "QUALITY_FAILURE", "POLICY_DENIAL", "SECURITY_FAILURE", "AUDIT_FAILURE",
    ]
    assert routing["behavioral_release_subjects"] == [
        "model", "tool", "harness", "policy", "routing",
    ]


def test_execution_and_migration_policies_are_structured_and_exact() -> None:
    execution = CONTRACT["execution_policy"]
    assert execution["filesystem_paths"] == "CANONICAL_RESOLVED"
    assert execution["network_default"] == "DENY"
    assert execution["credential_policy"] == [
        "SCOPED", "JIT", "EXPIRING", "NO_AMBIENT_MODEL_READABLE_SECRET_VALUES",
    ]
    assert execution["tool_spec_required_fields"] == [
        "namespace", "name", "version", "input_schema", "output_schema", "risk",
        "capabilities", "idempotency", "timeout", "retry_class", "concurrency_limit",
        "result_size_limit", "provider",
    ]
    assert execution["mcp_role"] == "ADAPTER_INTEROPERABILITY_ONLY"
    assert execution["remote_mcp_metadata_trust"] == "UNTRUSTED"

    migration = CONTRACT["migration_policy"]
    assert migration["schema_domains"] == ["DOMAIN", "PERSISTENCE", "API", "PROVIDER"]
    assert migration["production_migrations"] == "EXPLICIT"
    assert migration["silent_startup_production_schema_mutation"] == "PROHIBITED"
    assert migration["in_flight_dual_lifecycle_authority"] == "PROHIBITED"
    assert migration["atomic_run_event_projection_version_transition_required"] is True
    assert migration["optimistic_concurrency"] == "EXPECTED_VERSION"


def test_control_plane_is_command_oriented_and_preserves_lifecycle_boundary() -> None:
    control_plane = CONTRACT["control_plane"]
    assert control_plane["port"] == "RuntimeControlPort"
    assert control_plane["mutation_model"] == "COMMAND_ORIENTED"
    assert control_plane["clients_can_directly_set_lifecycle_state"] is False
    assert control_plane["command_metadata_fields"] == [
        "command_id", "actor", "expected_resource_version", "idempotency_identity",
        "requested_effect", "policy_decision", "audit_evidence",
    ]
    assert control_plane["streams"] == "PROJECTIONS"
    assert control_plane["approval_can_mutate_action"] is False


def test_source_consistency_for_current_target_deferred_and_lock_boundaries() -> None:
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    architecture = (PACKAGE / "ARCHITECTURE.md").read_text(encoding="utf-8")
    roadmap = (PACKAGE / "IMPLEMENTATION_ROADMAP.md").read_text(encoding="utf-8")
    for marker in ("**CURRENT**", "**TARGET**", "**DEFERRED**"):
        assert marker in readme
    assert "does not implement a runtime" in readme
    assert "MUST NOT create a second runtime" in architecture
    assert "RunManifest" in architecture
    assert "two real project" in architecture
    assert "authorizes no runtime change" in roadmap
    assert "two-project proof" in roadmap


def test_all_adrs_have_exact_identity_and_decision_markers() -> None:
    for relative_path, title in EXPECTED_ADRS.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert text.splitlines()[0] == title
        for marker in ("## Context", "## Decision", "## Alternatives", "## Rationale"):
            assert marker in text, f"{relative_path} lacks {marker}"
        for term in EXPECTED_ADR_DECISION_TERMS[relative_path]:
            assert term in text, f"{relative_path} lacks decision term {term!r}"


def test_contract_lists_complete_frozen_failure_taxonomy() -> None:
    assert set(CONTRACT["failure_classes"]) == {
        "MODEL_ERROR", "MODEL_REFUSAL", "TOOL_ERROR", "TOOL_TIMEOUT", "NETWORK_ERROR",
        "RATE_LIMIT", "VALIDATION_FAILURE", "POLICY_DENIED", "CAPABILITY_DENIED",
        "APPROVAL_REQUIRED", "ARCHITECTURE_DRIFT", "REPOSITORY_DRIFT", "WORKSPACE_DIRTY",
        "LEASE_CONFLICT", "BUDGET_EXCEEDED", "EVAL_REGRESSION", "EXTERNAL_STATE_CONFLICT",
        "UNKNOWN",
    }
