# ADR-020: Command control plane and persistence model

## Context
Clients and streams cannot be lifecycle authorities; persistence migration needs concurrency safety.

## Decision
Route commands through `RuntimeControlPort` with actor, expected version, idempotency, policy, and audit evidence. Persist stream cursors; use explicit schema migration and optimistic versions.

## Alternatives
Status patch endpoints; client-owned lifecycle; startup schema mutation; unversioned writes.

## Rationale
Command handling centralizes authorization, audit, recovery, and concurrency semantics.

## Consequences
Approval UX shows immutable exact action details; an event-backed backend atomically writes event/projection only for its own new runs.

## Migration impact
RunManifest runs remain unchanged and cannot be made dual-authoritative.

## Reversal trigger
Only after equivalent command and persistence guarantees are independently verified.
