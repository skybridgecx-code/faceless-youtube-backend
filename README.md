# Autonomous YouTube Studio — Migration Repository

This repository is the maintained implementation being evolved in place from the legacy faceless YouTube backend into **Autonomous YouTube Studio v1**: a single-channel, low-touch production system for original AI and future-technology documentary/explainer videos.

Architecture authority:

- `architecture.lock.json` — machine-testable architecture contract
- `ARCHITECTURE.md` — human-readable architecture and migration rules

## Repository identity and status

The current canonical repository is:

- repository: `skybridgecx-code/faceless-youtube-backend`
- branch: `youmo-clone-v2`

I0 historically occurred before the ownership transfer, under:

- repository: `Aatifshow33/faceless-youtube-backend`
- branch: `youmo-clone-v2`
- baseline SHA: `3f270573d2786762123e9d11239f65a6c4d2061a`

Ownership was transferred after I2 at `21f696798942a3fd23d65ca871b55085f4858f3d` with history preserved. The earlier architecture blueprint inspected a historical repository state at `skybridgecx-code/faceless-youtube-backend@20c30309...`; that historical repository is now archive-preserved as `skybridgecx-code/faceless-youtube-backend-historical`. Its audit remains architectural input, but its repository-state observations are not the implementation reset target.

The repository is in a **controlled migration**. Legacy routes, models, workflows, and data remain operational until replacement behavior passes the required phase gates and shadow-production cutover.

## Target product

The target weekly flow is:

1. Topic autopilot
2. Research and verified claims
3. Original 8–12 minute script
4. Storyboard
5. Media generation/acquisition
6. Narration and deterministic assembly
7. Machine QA
8. Private YouTube upload
9. One final hash-bound human approval
10. Release and analytics

Derived Shorts reuse the approved campaign evidence and may not introduce unsupported new factual claims.

Autopilot v1 excludes sensitive health, finance, elections/politics, war/conflict, and legal-advice topics.

## Current capabilities after I2

The application includes:

- FastAPI
- SQLAlchemy
- Alembic
- SQLite
- Pydantic settings
- pytest
- visual planning and local visual generation
- draft-preview rendering
- production voiceover readiness/dry-run/generation gates
- FFmpeg final-export logic
- publishing payload generation
- a gated **private** YouTube upload endpoint
- local deterministic workflow smoke testing

I2 completed migration reconciliation and introduced the canonical persistence core alongside the preserved legacy state:

- Alembic is canonical for schema evolution.
- Application startup validates schema state and no longer performs `Base.metadata.create_all()` or additive startup DDL.
- Canonical `Campaign`, `Source`, `Claim`, `Artifact`, `Scene`, `GenerationJob`, `GateDecision`, `Approval`, and `MetricSnapshot` persistence is present.
- Legacy compatibility remains until later migration and shadow-production gates pass.

Important current-state limitations:

- the legacy domain model still has multiple root-like entities and duplicated status concepts,
- DBOS durable workflow ownership is not yet implemented,
- the target workflow engine is not yet implemented,
- target hash-bound release approval is not yet implemented.

## Campaign budget policy

`policies/campaign_budget.v1.json` locks the future variable metered-cost envelope for a normal 8–12 minute long-form campaign:

- target: $20.00 / 20,000,000 micro-USD
- soft warning: $25.00 / 25,000,000 micro-USD
- default hard cap: $35.00 / 35,000,000 micro-USD

Future metered provider calls must pass a campaign budget preflight. The soft warning requires cost-reduction fallbacks, and a predicted hard-cap breach blocks the next request unless an explicit, audited, campaign-specific owner override authorizes a new cap. Budget controls never weaken truth, source, rights, safety, editorial-quality, or approval gates.

## Safety boundary

The current code contains a gated private-upload path:

`POST /publish/{id}/youtube/upload`

It does **not** constitute the target public-release workflow. At the I0 baseline, public release remains outside the autonomous target contract.

The target architecture requires:

- private upload first,
- explicit human approval bound to exact final render/metadata/thumbnail/disclosure hashes,
- approval invalidation after any bound-package mutation,
- no scheduled/public release without a currently valid approval envelope.

No phase may weaken that target safety boundary.

## Stack

Current baseline:

- FastAPI
- SQLAlchemy + Alembic
- SQLite by default
- Pydantic settings
- pytest
- FFmpeg / ffprobe

Target v1 adds DBOS during the durable-workflow phase. Do not add Redis/Celery, Kafka, a Temporal cluster, microservices, or dynamic agent swarms without an explicit architecture-lock change.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
python -m app.seed
uvicorn app.main:app --reload
```

Dashboard:

`http://127.0.0.1:8000/app`

API docs:

`http://127.0.0.1:8000/docs`

## Validation

Baseline test suite:

```bash
python -m pytest -q
node --check app/static/app.js
```

Local isolated workflow smoke test:

```bash
python scripts/local_workflow_smoke.py
```

The smoke workflow uses an isolated temporary database/output directory and is expected to block final production when required production voiceover is absent.

## Current legacy workflow

Until the new campaign workflow replaces it, the current application supports the legacy review-gated sequence:

1. Create or seed a channel.
2. Create/generate video content assets.
3. Create a visual asset plan.
4. Queue and run local visual generation.
5. Manually approve visual assets.
6. Manually approve the video.
7. Render and review the draft preview.
8. Build the package.
9. Check production voiceover readiness and dry run.
10. Generate production voiceover when configured.
11. Run final production export.
12. Prepare publishing metadata/payload.
13. Optionally use the currently gated private YouTube upload path.
14. Complete publication outside the target autonomous release flow.

Legacy behavior is compatibility state, not the target domain architecture.

## Current key endpoints

```text
GET    /health
GET    /app
POST   /channels
GET    /videos
POST   /videos
POST   /videos/{id}/generate
POST   /videos/{id}/review
POST   /videos/{id}/preview/render-draft
POST   /videos/{id}/preview/review
POST   /videos/{id}/package
GET    /videos/{id}/final-voiceover/readiness
POST   /videos/{id}/final-voiceover/dry-run
POST   /videos/{id}/final-voiceover/generate
GET    /videos/{id}/final-production/status
POST   /videos/{id}/final-production/export
POST   /visual-assets/from-video/{id}
POST   /visual-generation/plans/{id}/queue
POST   /visual-generation/jobs/{id}/run-local
POST   /visual-generation/assets/{id}/approve
POST   /visual-generation/assets/{id}/reject
POST   /publish/{id}/prepare-youtube-payload
POST   /publish/{id}/youtube/upload
POST   /publish/{id}/mark-published
```

## Environment

See `.env.example` for the current complete baseline configuration.

Never commit API keys, OAuth credentials, refresh tokens, or other secrets.

## Migration phases

- I0 — baseline freeze and evidence capture: complete
- I1 — governance lock
- I2 — existing Alembic reconciliation + canonical core schema: complete
- I2.1 — campaign budget + repository identity governance lock
- I3 — durable workflow and deterministic gate engine
- I4 — topic/research/claims/script
- I5 — storyboard/media/TTS/render
- I6 — machine QA + one-touch approval UI
- I7 — private upload + hash-bound release + analytics
- I8 — three real private shadow campaigns
- I9 — legacy removal
- I10 — scale only when operational triggers require it

Do not skip phase gates or delete legacy compatibility paths early.
