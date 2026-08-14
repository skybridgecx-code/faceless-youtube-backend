# Durability, Replay, and Recovery (P4)

Exactly one lifecycle authority exists per run. Current runs retain `RunManifest`/`ManifestBackend`. A future run created under `LifecycleEngine` plus transactional `RunEventStore` MAY use that authority; no in-flight run may be dual-authoritative. Freeze `lifecycle_backend_id`, `lifecycle_schema_version`, and `durability_backend_id` at creation. A durability backend MUST NOT independently decide platform lifecycle.

Attempts are immutable. Completed expensive or durable steps replay stored results rather than re-contact models/tools. Step class is `DETERMINISTIC`, `MODEL_IO`, `READ_ONLY_EXTERNAL`, `REVERSIBLE_EFFECT`, `CONSEQUENTIAL_EFFECT`, or `WAIT`. Checkpoints are replay/fork anchors, not lifecycle authority.

Cancellation progresses `REQUESTED -> PROPAGATING -> TERMINAL`. Recovery is `RETRY_SAFE`, `RECONCILE_THEN_RETRY`, `RESUME_FROM_CHECKPOINT`, `REQUIRES_APPROVAL`, `REQUIRES_OPERATOR`, or `TERMINAL`. Lease expiry never proves an external side effect did not occur.

## Reliability, backpressure, and failure taxonomy

Failure is evidence, not an automatic retry. The frozen classes are `MODEL_ERROR`, `MODEL_REFUSAL`, `TOOL_ERROR`, `TOOL_TIMEOUT`, `NETWORK_ERROR`, `RATE_LIMIT`, `VALIDATION_FAILURE`, `POLICY_DENIED`, `CAPABILITY_DENIED`, `APPROVAL_REQUIRED`, `ARCHITECTURE_DRIFT`, `REPOSITORY_DRIFT`, `WORKSPACE_DIRTY`, `LEASE_CONFLICT`, `BUDGET_EXCEEDED`, `EVAL_REGRESSION`, `EXTERNAL_STATE_CONFLICT`, and `UNKNOWN`. Each failure record MUST include retryability, effect certainty, recovery class, and whether a human is required.

The harness MUST apply bounded queues, concurrency limits, deadlines, retry budgets, and per-run/project/provider cost limits before dispatch. It MUST propagate backpressure rather than create unbounded work, swarms, or retries. Rate limits and unavailable providers may use certified fallback; a policy/security/quality/audit failure may not bypass its gate. Every consequential retry first verifies effect certainty and follows its recovery class.
