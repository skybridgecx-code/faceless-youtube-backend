# ADR-014: Lifecycle and durability authority

## Context
M1 runs use `RunManifest`/`ManifestBackend`; an event store is target-only.

## Decision
Freeze exactly one lifecycle authority and one durability backend per run. Future event-backed authority applies only to newly created runs.

## Alternatives
Dual-write/dual-authorize in-flight runs; let a durability adapter decide lifecycle.

## Rationale
One authoritative state machine avoids split-brain recovery.

## Consequences
Event transitions atomically persist event plus projection/version; lease expiry is inconclusive for effects.

## Migration impact
RunManifest compatibility remains until an explicit backend-cutover decision.

## Reversal trigger
No reversal without a proven atomic migration and lifecycle reconciliation plan.
