# Control Plane (P8)

CLI/API/UI call `RuntimeControlPort`, then controller/policy/runtime adapters. Clients MUST NOT set lifecycle state directly (for example, no `PATCH {"status":"RUNNING"}`). Commands include `create`, `pause`, `resume`, `cancel`, `retry`, `approve`, `deny`, `fork`, `checkpoint`, `promote`, and `release`.

Every mutation carries `command_id`, actor, expected resource version, idempotency identity, requested effect, policy decision, and audit evidence. Operator identity is distinct from model/agent identity. Streams are projections only; after a dropped client, resumption uses a persisted cursor/sequence. Mature routine operation SHOULD NOT require shell access.

Approval UX MUST show exact action, target, effect, risk, diff/artifacts, policy, cost, and expiry and MUST NOT allow action mutation while approving.
