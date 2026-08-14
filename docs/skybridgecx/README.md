# SkyBridgeCX Runtime Architecture Source Lock

**Status:** implementation-grade refinement of the parent blueprint; target architecture only.  It does not implement a runtime or promote a new authority.

## Authority and provenance

The parent target architecture is `Skybridge_Agent_Runtime_Competitive_Architecture_Blueprint_2026-08-12.pdf` (`e37b4a82b54e14a84ce0ac936395a370ebd327c52759653c2cde25ce05766fad`). This P1–P10 package refines that target into source-controlled contracts. Supporting external references are recorded in [`TARGET_CONTRACT.v1.json`](TARGET_CONTRACT.v1.json):

- `SkyBridgeCx_Runtime_Project_Instructions_Optimized(1).md` — `49d43f7c5f0fdb38c35c63bc7a5bd6fc32a21b25bb0b40a72a3a10e6b10ad434`
- `SkyBridgeCx_Runtime_Project_Specification(1).md` — `92037214ee366d3f5b79daea722a3e46fdd51e20e404d02bb43e964a357d3905`
- `SkyBridgeCx_M0_Baseline_Evidence_2026-08-12(1).md` — `25b1cbbae263c0e0f80b12488fa605448206a37e7ff2a768dfd16c9ac472beee`
- `SkyBridgeCx_M1_Generic_Contracts_Design_2026-08-12(1).md` — `ab8a0300a6239cd0b170d837784d95482363955b9db0e7d9a52be3c783071d09`
- `SkyBridgeCx_Runtime_Operational_State_Runtime_Remediation_2026-08-12(1).md` — `472bb48a374c0e4c554eb2d3c1879a7633856a16950515dfec9eaa7ff5d87e73`

The repository-root [`architecture.lock.json`](../../architecture.lock.json) and [`ARCHITECTURE.md`](../../ARCHITECTURE.md) remain the current YouMo **product** authority. This package neither replaces nor silently supersedes them. A conflict between this target package and a product authority MUST stop implementation pending explicit reconciliation.

Authority is interpreted in this order: the parent blueprint establishes the target direction; the five hashed supporting references provide project/M0/M1/operational context; this source package is the implementation-grade refinement; root YouMo authorities govern current product behavior. This package does not authorize an unrecorded deviation from the parent blueprint. When a target decision conflicts with a current product authority or the supporting M1 boundary, implementation MUST stop and record an explicit reconciliation before changing code. The external supporting files are provenance references only and are not copied or asserted present in this checkout.

## Current, target, and deferred

| Classification | Contract |
| --- | --- |
| **CURRENT** | Generic runtime source is `tools/project_agent_runtime/`; YouTube workflow source is `app/workflows/`; `RunManifest`/`ManifestBackend` is the sole lifecycle authority for current migration/M1 runs. |
| **TARGET** | Event-backed lifecycle, Postgres projections, typed execution environments, policy/approval contracts, and P1–P10 entities described here. |
| **DEFERRED** | Package extraction, a second-project completion claim, and DBOS/Temporal as universal lifecycle authority. DBOS/Temporal are optional durability adapters/scale paths only. |

No active `RunEvent` ledger migration exists. M1 introduces neither a competing canonical `UsageRecord` nor package extraction. Current YouMo compatibility behavior MUST be preserved.

## Navigation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — concise whole-system architecture and invariants.
- [`PLATFORM_CONTRACTS.md`](PLATFORM_CONTRACTS.md), [`SECURITY_AND_CAPABILITIES.md`](SECURITY_AND_CAPABILITIES.md), [`EVALUATION_AND_MODEL_ROUTING.md`](EVALUATION_AND_MODEL_ROUTING.md), [`DURABILITY_AND_RECOVERY.md`](DURABILITY_AND_RECOVERY.md), [`EXECUTION_TOOLS_AND_MCP.md`](EXECUTION_TOOLS_AND_MCP.md), [`EVIDENCE_TELEMETRY_AND_COST.md`](EVIDENCE_TELEMETRY_AND_COST.md), [`DATA_AND_MIGRATION.md`](DATA_AND_MIGRATION.md), [`CONTROL_PLANE.md`](CONTROL_PLANE.md), [`DEPLOYMENT_AND_PORTABILITY.md`](DEPLOYMENT_AND_PORTABILITY.md), and [`IMPLEMENTATION_ROADMAP.md`](IMPLEMENTATION_ROADMAP.md) — normative topic contracts.
- [`TARGET_CONTRACT.v1.json`](TARGET_CONTRACT.v1.json) — machine-readable source-consistency contract.
- [`../adr/`](../adr/) — consequential architecture decisions.
