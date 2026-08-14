# ADR-018: Execution environment containment and MCP boundary

## Context
Tools need blast-radius containment; MCP is external interoperability, not the core domain.

## Decision
Use typed `ExecutionEnvironment` contracts, resolved-path policy, deny-by-default networking, JIT credentials, and MCP adapters outside MCP-independent domain types.

## Alternatives
Ambient shell/secrets; direct MCP domain coupling; shared coding checkout.

## Rationale
Containment, deterministic exposure, and provenance resist traversal, exfiltration, and metadata poisoning.

## Consequences
Writers require physical Git isolation; remote long work exposes durable task identity.

## Migration impact
Environment implementations are target adapters, not a current runtime replacement.

## Reversal trigger
Only when a different boundary demonstrably provides equivalent containment and auditability.
