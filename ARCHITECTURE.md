# Autonomous YouTube Studio Architecture

Status: target architecture and migration authority
Architecture lock: `architecture.lock.json`
I0 reconciled baseline: `Aatifshow33/faceless-youtube-backend` / `youmo-clone-v2` / `3f270573d2786762123e9d11239f65a6c4d2061a`

## 1. Authority and scope

This repository is being evolved in place into **Autonomous YouTube Studio v1**, a single-channel, low-touch production system for original AI and future-technology documentary/explainer videos.

The architecture is governed in this order:

1. `architecture.lock.json` — machine-testable architecture contract.
2. `ARCHITECTURE.md` — human-readable architecture and migration rules.
3. Phase-specific implementation plan and acceptance criteria.
4. `README.md` — operator-facing current-state documentation.

If a lower-level document conflicts with the lock or this architecture document, stop and resolve the conflict before implementation.

## 2. Baseline reconciliation

The original architecture blueprint audited `skybridgecx-code/faceless-youtube-backend` at `20c30309d30a93bda9474c10c28f81cf237b733f`.

I0 established that the maintained implementation is instead:

- repository: `Aatifshow33/faceless-youtube-backend`
- branch: `youmo-clone-v2`
- baseline SHA: `3f270573d2786762123e9d11239f65a6c4d2061a`
- historical shared anchor: `5afebd93b98e6b65ccec41da036ad1ec972dc517`

The blueprint remains the architectural source, but repository-state claims from that audit are historical. Subsequent phases must use observed repository evidence from the reconciled baseline and must not reset the repository to the historical audit SHA.

## 3. Product boundary

v1 is a single-channel autonomous production studio.

Normal weekly operation should:

1. select one sourceable topic,
2. build a cited research and claims bundle,
3. write an original long-form script,
4. create a purpose-built storyboard,
5. generate or acquire bounded media assets,
6. synthesize narration and assemble the final render,
7. run machine QA,
8. upload the exact review render privately,
9. request one final owner approval bound to exact hashes,
10. release only the approved package and collect analytics.

Default long-form target: 8–12 minutes.
Derived Shorts may reuse the approved campaign evidence but may not introduce unsupported new factual claims.

Autopilot v1 excludes sensitive health, finance, elections/politics, war/conflict, and legal-advice topics.

## 4. Core architectural decisions

### 4.1 One campaign root

`Campaign` is the sole content root. Topic, research, claims, script, storyboard, media, QA, approval, publishing, and analytics attach to one campaign identity.

Legacy root-like entities remain only during controlled migration and must not be expanded as the target architecture.

### 4.2 Immutable artifacts

Accepted phase outputs are immutable artifacts. A later phase may create a replacement artifact, but it must not silently mutate an accepted upstream artifact in place.

Artifacts are content-addressed with SHA-256 and retain provenance.

### 4.3 Deterministic phase gates

Every runtime transition requires an immutable `GateDecision`.

Hard deterministic failures dominate soft model scores. No LLM score may override a failed claim, rights, media-integrity, disclosure, approval, or publication gate.

Gate outcomes are:

- `PASS`
- `FAIL`
- `NEEDS_HUMAN`

Only `PASS` advances the workflow.

### 4.4 Durable Python workflow

The target runtime is a single deployable Python modular monolith with DBOS providing durable workflow/step recovery.

`asyncio.TaskGroup` may be used only for bounded concurrency inside a durable step. Fire-and-forget tasks are not workflow state.

DBOS is a target dependency for the durable-workflow phase; it is not considered present until a later phase implements and validates it.

### 4.5 Provider isolation

LLM, video generation, TTS, YouTube, and other external systems sit behind provider adapters.

Providers:

- return typed results,
- do not write application state directly,
- do not advance workflow state,
- do not change prompts, policy, budgets, or release state.

### 4.6 Media boundary

FFmpeg is the deterministic assembly engine and ffprobe is the canonical media validator.

Generated video must not be trusted to render authoritative factual UI, numbers, charts, citations, names, logos, or code. Factual overlays are rendered deterministically from verified claims.

### 4.7 Human approval and publication

No public or scheduled release may occur without explicit human approval bound to:

- campaign ID,
- final render SHA-256,
- metadata SHA-256,
- thumbnail manifest SHA-256,
- disclosure state,
- private YouTube video ID,
- intended release time,
- actor and timestamp.

Any approved package mutation invalidates approval.

The current legacy private-upload/manual-publication behavior is transitional and does not satisfy the target hash-bound release contract.

### 4.8 Analytics cannot self-modify policy

Analytics may produce recommendation artifacts and rank future topic candidates.

Analytics may not automatically mutate:

- architecture,
- prompts,
- policies,
- gate thresholds,
- source rules,
- safety rules,
- budget caps.

## 5. Target runtime stages

The locked runtime sequence is:

1. `topic`
2. `research`
3. `script`
4. `storyboard`
5. `media`
6. `assembly`
7. `machine_qa`
8. `private_upload`
9. `human_approval`
10. `release`

Each stage has a finite retry, time, and cost policy.

## 6. v1 infrastructure policy

The target is one deployable FastAPI application, one durable workflow runtime, and one database.

Do not introduce in v1 without an explicit architecture-lock version change and a documented trigger:

- microservices,
- Celery,
- Redis,
- Kafka,
- a Temporal cluster,
- dynamic agent swarms,
- self-modifying prompts,
- automatic public release,
- multi-channel tenancy.

SQLite remains valid for local development. Postgres is introduced only when hosted execution or concurrent durable workers require it.

## 7. Migration rules

The repository is migrated in place.

1. Preserve existing data and proven capabilities until replacement behavior is validated.
2. Do not delete legacy tables/routes/services merely because the target model exists.
3. Do not expand stale legacy domain concepts unless required for safe compatibility.
4. New target architecture is introduced alongside legacy behavior, then shadow-run.
5. Legacy removal occurs only after the shadow-production gate passes.
6. Every phase starts from a verified branch/SHA/clean-worktree precondition.
7. Every phase has explicit acceptance criteria and fail-closed stop conditions.
8. A green test suite alone does not authorize phase completion; semantic gate and diff audit are also required.
9. No phase may perform an unrelated architecture change.
10. No source edit is allowed when a phase explicitly requires evidence-only execution.

## 8. Current reconciled repository state

As of the I0 baseline:

- FastAPI, SQLAlchemy, Pydantic, SQLite, pytest, FFmpeg integration, visual workflows, production voiceover gates, final export logic, and a private YouTube upload path already exist.
- Alembic is already present with a legacy baseline migration.
- `app/db.py` still contains startup `Base.metadata.create_all()` plus additive SQLite migration helpers.
- The canonical `Campaign` / immutable `Artifact` / `GateDecision` target model is not yet implemented.
- DBOS durable workflow ownership is not yet implemented.
- Hash-bound final release approval is not yet implemented.
- Legacy domain duplication remains.

Therefore the old blueprint phase named “add Alembic” is superseded. The migration phase must reconcile existing Alembic state and remove startup schema mutation only after migration compatibility is proven.

## 9. Phase sequence

- **I0 — Baseline Freeze and Evidence Capture:** complete against reconciled baseline `3f270573...`.
- **I1 — Governance Lock:** architecture files, lock test, README direction, replacement agent rule. No runtime/schema behavior changes.
- **I2 — Migration Reconciliation and Canonical Core Schema:** reconcile existing Alembic, migrate fresh/current DB copies, introduce canonical tables alongside legacy state, then remove ad-hoc startup DDL only when proven safe.
- **I3 — Durable Workflow and Gate Engine on Stub Providers.**
- **I4 — Topic, Research, Claims, and Script.**
- **I5 — Storyboard, video-provider adapter, TTS, deterministic render.**
- **I6 — Machine QA and one-touch approval UI.**
- **I7 — Private upload, hash-bound release, analytics.**
- **I8 — Three real private shadow campaigns.**
- **I9 — Legacy removal.**
- **I10 — Scale only when triggered.**

## 10. Change control

Any change to runtime stages, hard invariants, forbidden v1 infrastructure, publication safety, provider boundaries, or migration-removal gates requires:

1. an explicit `schema_version` increment in `architecture.lock.json`,
2. corresponding updates to this document,
3. tests proving the new architecture contract,
4. an independently reviewed diff.

Normal implementation details that remain within the locked architecture do not require a lock-version increment.
