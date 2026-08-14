# ADR-019: Immutable evidence, OTel telemetry, and observed cost

## Context
Evidence, telemetry, and billing observations serve different authority roles.

## Decision
Content-address immutable artifacts; bind their meaning separately; use OTel as telemetry vocabulary only; record observed usage in integer micro-USD without fabrication.

## Alternatives
Mutable evidence; telemetry as lifecycle state; estimated/missing usage presented as observed.

## Rationale
Consequential decisions must be reconstructable without inventing facts or splitting authority.

## Consequences
Payload capture defaults off; retention separates raw content, evidence, telemetry, and credentials; M1 creates no duplicate canonical UsageRecord.

## Migration impact
This remains target contract work, with current usage behavior preserved.

## Reversal trigger
No reversal absent an immutable, privacy-preserving provenance alternative.
