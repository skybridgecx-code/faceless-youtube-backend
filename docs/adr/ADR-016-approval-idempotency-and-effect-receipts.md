# ADR-016: Approval, idempotency, and effect receipts

## Context
Authorization identity, execution identity, and duplicate-effect prevention differ; external effects can be uncertain.

## Decision
Use `action_id`, normalized `action_digest`, and `idempotency_key` separately. Bind approvals to exact state/hashes/expiry; default R3/R4 approvals to single use. Record effect receipts and reconcile uncertainty before retry.

## Alternatives
Approve an action label; retry on timeout; use one identifier for all roles.

## Rationale
Exact binding prevents stale/replayed approval and duplicate consequential effects.

## Consequences
Relevant mutation invalidates approval; `UNCERTAIN` is a reconciliation state, not a retry signal.

## Migration impact
Future actions can adopt the contract without changing current lifecycle authority.

## Reversal trigger
Only with a provably equivalent exact authorization and provider reconciliation protocol.
