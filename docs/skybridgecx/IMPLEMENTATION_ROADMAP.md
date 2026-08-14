# Implementation Roadmap and Boundaries

This package authorizes no runtime change. Implementation proceeds only through explicitly approved future slices: (1) retain and generalize existing runtime contracts without changing current YouMo compatibility; (2) prove the same contracts with a second real project profile; (3) evaluate portability and only then consider extraction; (4) introduce event-backed lifecycle only for newly created runs, with a separate migration decision.

Hard stops: a root architecture authority conflict; missing eval evidence for behavioral release changes; an attempt to dual-authorize an in-flight run; any proposal to fabricate usage; or a request to extract before two-project proof. DBOS/Temporal may be separately evaluated as durability adapters/scale paths, never assumed to be universal current authority.

All changes retain immutable evidence, explicit migration, bounded cost/retries, capability-first authorization, and isolated writer workspaces.
