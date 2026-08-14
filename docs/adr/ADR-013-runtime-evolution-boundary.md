# ADR-013: Runtime evolution boundary

## Context
The existing generic runtime is `tools/project_agent_runtime/`; YouTube workflow code is `app/workflows/`.

## Decision
Generalize the existing runtime, prove it with a second real project, and do not create a second runtime or extract a package yet.

## Alternatives
Build a parallel runtime; extract immediately; couple generic code to YouTube.

## Rationale
Two-project proof is needed to establish portability without destabilizing current compatibility.

## Consequences
Product-specific assumptions stay out of generic contracts and package extraction remains deferred.

## Migration impact
Current source locations remain authoritative for implementation.

## Reversal trigger
Explicit evidence that the existing boundary cannot support two project profiles, reviewed before any replacement proposal.
