# Data, Persistence, and Migration (P7)

Target logical entities are `ProjectProfile`, `ArchitectureSnapshot`, `PolicyBundle`, `Run`, `RunEvent`, `Task`, `Attempt`, `Step`, `ModelCall`, `Action`, `ToolCall`, `Observation`, `PolicyDecision`, `Approval`, `Artifact`, `EvidenceBinding`, `Checkpoint`, `EffectReceipt`, `UsageRecord`, `Lease`, `EvalSuite`, `EvalCase`, `EvalTrial`, `EvalGrade`, `ReleaseDecision`, and `SecretGrant`.

Postgres is recommended for run/lifecycle/task/approval/policy/usage/lease/eval metadata; a content-addressed object store for artifact/evidence bytes; Git for source history; provider plus `EffectReceipt` for external-effect truth/reconciliation; an OTel backend for telemetry only; a secret manager/keychain for credentials; and an optional vector/index store only for knowledge retrieval.

Domain contracts, persistence schemas, API schemas, and provider schemas MUST remain distinct. Schema migration is explicit; startup MUST NOT silently mutate production schemas. No in-flight dual lifecycle authority is allowed. Event-backed transition atomically persists `RunEvent` plus projection/version update and uses optimistic expected-version checks under concurrency.
