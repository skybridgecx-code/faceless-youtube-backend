# ADR-021: Two-project proof before extraction/package

## Context
One YouMo implementation cannot establish generic runtime portability.

## Decision
Forbid package extraction until at least two real project profiles prove the same generic runtime contracts.

## Alternatives
Publish/extract now; fork project-specific copies; declare proof from synthetic examples.

## Rationale
Two real projects expose genuine shared seams while avoiding speculative platform design.

## Consequences
Current generic source remains `tools/project_agent_runtime/`; YouTube remains `app/workflows/`; no package or canonical promotion follows this documentation change.

## Migration impact
Second-project work must preserve source separation and evidence before a later extraction decision.

## Reversal trigger
Explicit architecture review accepting a different empirical portability standard.
