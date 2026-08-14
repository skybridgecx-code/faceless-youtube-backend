# SkyBridgeCX Runtime Target Architecture

This is an implementation-grade refinement of the parent blueprint, not an implementation claim. RFC-style **MUST**, **SHOULD**, and **MAY** express target requirements. Current migration facts are defined by [`README.md`](README.md).

## North Star and planes

The runtime generalizes the existing `tools/project_agent_runtime/`, proves the abstraction with a second real project, and only then may package/extract shared code. It MUST NOT create a second runtime. The generic runtime depends upward on no product workflow: `SkyBridgeCX Runtime <- YouMo, second project, future projects`; `app/workflows/` remains the YouTube product boundary.

The target has four planes: a **control plane** accepts authenticated commands; a **runtime plane** plans and executes Tasks/Steps; a **data/evidence plane** persists immutable facts and artifacts; and an **execution plane** contains tools, environments, and external effects. Models belong to the runtime plane but only propose; policy and typed executors authorize and perform effects.

## Canonical target hierarchy

`Project -> Run -> Task -> Step -> (ModelCall, ToolCall, Observation, Artifact) -> GateDecision -> Approval -> Evaluation -> RunOutcome`.

Identifiers are distinct: `project_id`, `run_id`, `attempt_id`, `task_id`, `step_id`, `action_id`, `model_call_id`, `tool_call_id`, `artifact_id`, `evaluation_id`, `approval_id`, and `trace_id`. A retry creates a new immutable `Attempt`; it MUST NOT overwrite its failed attempt.

## Global invariants

1. No second runtime; generic runtime and YouTube workflow remain separate.
2. Each run has exactly one lifecycle authority and one durability backend, frozen when the run is created.
3. Models propose; runtime policy authorizes; no model output directly reaches privileged execution.
4. Consequential effects require idempotency/reconciliation semantics; uncertain effects reconcile before retry.
5. Accepted historical evidence and artifact bytes are immutable and versioned.
6. Approvals bind exact action/state/artifact hashes; untrusted content is data, never authority.
7. Concurrent writers use physically separate workspaces; canonical promotion is serialized; later parallel work reconciles onto the newest canonical before promotion.
8. Model/tool/harness/routing changes require eval evidence. OTel is telemetry canon, never lifecycle authority.
9. MCP is an interoperability adapter, not the domain model. Missing usage/cost is never fabricated.
10. Current `RunManifest` authority is preserved until explicitly migrated; M1 has no active event-ledger migration or duplicate canonical `UsageRecord`; extraction waits for two real project profiles.

## Lifecycle and migration boundary

For existing/current runs, `RunManifest`/`ManifestBackend` remains authoritative. A future `LifecycleEngine` with transactional `RunEventStore` MAY be authoritative only for runs created under that backend. An in-flight run MUST NEVER be authoritative in both. A future event transition atomically writes a `RunEvent` and projection/version update. Durability adapters do not independently decide lifecycle state.

See the topic contracts and ADRs for detailed, non-duplicative requirements.
