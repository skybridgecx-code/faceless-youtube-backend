# ADR-017: Eval-certified model, tool, and harness routing

## Context
Availability does not establish behavioral suitability; route changes alter agent outcomes.

## Decision
Require task-specific `RouteCertification`, record full routes, and treat model/tool/harness/policy/routing changes as behavioral releases with eval evidence.

## Alternatives
Global default model; silent fallback after quality/security/policy/audit failure.

## Rationale
Evaluation makes routing evidence-based and preserves hard security gates.

## Consequences
Only operational/certified fallback causes are permitted; corrections feed minimized sanitized regressions.

## Migration impact
No claim that a current provider route is already certified.

## Reversal trigger
A stronger demonstrated release mechanism with equivalent task-specific evidence.
