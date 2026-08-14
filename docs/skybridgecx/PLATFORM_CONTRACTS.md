# Platform Contracts (P1)

This target contract defines product-neutral domain types. Provider schemas MUST remain behind adapters, and platform facts MUST NOT contain secret values.

## Domain surface and frozen snapshots

`ProjectProfile` defines project identity, allowed capabilities, project policy references, environment classes, and retention/budget limits. `ArchitectureSnapshot` and `PolicyBundle` are immutable, hash-addressed, versioned inputs. `RunSnapshot` MUST freeze project/run identity; creation time; architecture, policy, and profile hashes; lifecycle/durability backend IDs and schema versions; Git/base identity; environment/tool catalog versions; model-route/certification versions; capability boundary; budget limits; and trace identity. Each `Attempt` freezes its own `attempt_id` and ancestry. A snapshot change creates a successor fact, not an in-place edit.

`RunEvent` is an immutable target historical fact, not a current M1 lifecycle ledger. `TaskSpec` declares requested outcome, inputs, constraints, dependencies, acceptance criteria, and budget/time bounds. `Attempt` is immutable and links retry ancestry; `StepSpec` declares type, preconditions, expected outputs, retry/recovery class, and effect semantics. `ModelCall`, `ActionRequest`, `PolicyDecision`, `ApprovalRequest`, `ApprovalDecision`, and `ToolCall` capture a proposed/authorized execution trail. `ArtifactRef` identifies immutable bytes; `EvidenceBinding` says why those bytes matter. `GateDecision`, `Evaluation`, and `RunOutcome` capture decision and result.

The canonical hierarchy is `Project -> Run -> Task -> Step -> ModelCall/ToolCall/Observation/Artifact -> GateDecision -> Approval -> Evaluation -> RunOutcome`. All historical accepted facts MUST be immutable/versioned. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for identities and lifecycle boundary.

## Identity and attempts

`project_id`, `run_id`, `attempt_id`, `task_id`, `step_id`, `action_id`, `model_call_id`, `tool_call_id`, `artifact_id`, `evaluation_id`, `approval_id`, and `trace_id` are non-interchangeable. Retrying work creates a new `Attempt`, links to its predecessor, and preserves failed facts. An adapter MAY add provider IDs but MUST NOT replace platform identity.

## Brain, harness, memory, and coding-agent boundary

The **brain** is the bounded model-planning component: it consumes a deterministically selected context bundle and emits typed proposals, never authority. The **harness** is the runtime-owned orchestration boundary: it validates schemas, materializes a run snapshot, invokes policy, enforces budgets/time/concurrency, dispatches typed tools, persists evidence, and drives recovery. The harness MUST be replaceable independently of any provider/model adapter.

Memory is a versioned context service, not an implicit authority or a mutable prompt transcript. Memory items MUST carry source/provenance, trust class, scope, expiry/retention, and content/artifact hash where applicable. Retrieval returns bounded, attributed context selected by policy; retrieved content is untrusted data and cannot mutate policy, architecture, tools, or capabilities.

A coding-agent task MUST begin with a harness-owned preflight that records repository/workspace identity, clean/index state, allowed paths, protected-resource state, base hash, policy/architecture hashes, and lease availability. Its release/promotion gate MUST record validation, exact diff/artifacts, evaluation/approval where required, and serialized canonical promotion state. Failure to establish any required preflight fact is a terminal gate for that action, not an invitation for the model to guess.
