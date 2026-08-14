# Evidence, Telemetry, and Cost (P6)

Artifact bytes MUST be immutable and content-addressed. `ArtifactRef` describes bytes; `EvidenceBinding` explains their relevance. Consequential provenance must reconstruct request, runtime, project profile, architecture/policy snapshots, model route, tool catalog, environment, Git/base identity, changes, usage/cost, validation, audit, approval, effects, and outcome.

OpenTelemetry is the canonical telemetry vocabulary, not lifecycle authority. Metadata tracing is default-on; prompt, completion, and tool-payload capture is default-off. `UsageRecord` reports observed values only: missing usage/cost MUST NOT be fabricated, and M1 MUST NOT introduce a duplicate canonical `UsageRecord`.

Costs use integer micro-USD at call, step, task, run, project/time-window, and provider-quota scopes. Retention policies MUST separately govern evidence, raw content, telemetry, and credentials.

Budget enforcement is runtime-owned, pre-dispatch, and bounded: it evaluates declared limits, observed spend, reserved/estimated spend where available, provider quota, and retry budget. A missing provider usage record is an explicit unknown observation, never zero or fabricated cost. Cost pressure MAY trigger only a certified cost optimization route; it MUST NOT weaken policy, approval, evaluation, or evidence gates.
