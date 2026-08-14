# ADR-015: Capability and policy authority outside the model

## Context
Model-generated content and untrusted external content cannot safely confer execution rights.

## Decision
Models propose; runtime policy decides under `DENY > APPROVAL_REQUIRED > scoped ALLOW > default`; typed tools/environments execute.

## Alternatives
Prompt-enforced permissions; model-selected privileged tools; risk labels as grants.

## Rationale
Policy remains independently auditable and resistant to prompt/content injection.

## Consequences
Capabilities, trust provenance, and policy decisions become explicit records; risk class never grants authority.

## Migration impact
No change to current runtime authority is implied by this target contract.

## Reversal trigger
None without a stronger independently enforceable authorization boundary.
