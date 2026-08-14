# Evaluation, Release, and Model Routing (P3)

The hierarchy is `EvalSuite -> EvalCase -> EvalTrial -> EvalGrade`. Suites are `REGRESSION`, `CAPABILITY`, `SECURITY`, `PERFORMANCE`, and `INTEGRATION` when appropriate. Prefer graders in order: deterministic, outcome/state, integration/provider, model, human. Security hard-gate failures tolerate zero unwaived failures.

Available is not certified. `ModelRequest` considers task class, risk, quality, context, capabilities, latency, cost, privacy, tool requirements, and availability. Task-specific `RouteCertification` is required. `ModelRoute` records provider, model, snapshot where available, reasoning, fallbacks, certification, and policy version. Allowed fallback causes are `PROVIDER_UNAVAILABLE`, `RATE_LIMIT`, `MODEL_UNAVAILABLE`, `CAPABILITY_UNSUPPORTED`, and `CERTIFIED_COST_OPTIMIZATION`; quality, policy, security, and audit failures MUST NOT silently fallback.

Model, tool, harness, policy, and routing changes that may alter outcomes are behavioral release changes requiring eval evidence. Production corrections SHOULD yield sanitized, minimized regression cases.
