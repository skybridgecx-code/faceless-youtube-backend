# Faceless YouTube Backend

A local-first, review-gated backend for a faceless YouTube content factory focused on original, reviewable AI automation content.

It manages channel profiles, video ideas, scripts, Shorts, descriptions, thumbnail prompts, review gates, visual asset workflows, voiceover generation, exportable production packages, and publish-ready metadata.

It intentionally does **not** blindly upload unreviewed AI content. Every gate must be satisfied by a human operator before production artifacts are created. YouTube publishing is always manual.

See [docs/PRODUCTION_RUNBOOK.md](docs/PRODUCTION_RUNBOOK.md) for full setup, workflow, and safety details.

## Stack

- FastAPI
- SQLAlchemy + Alembic
- SQLite by default (Postgres-ready)
- Pydantic settings
- Pytest

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit as needed
alembic upgrade head
python -m app.seed            # optional
uvicorn app.main:app --reload
```

Dashboard: `http://127.0.0.1:8000/`
API docs: `http://127.0.0.1:8000/docs`

`ffmpeg` must be installed locally for draft preview rendering and final production export rendering.

## Run tests

```bash
python3 -m pytest -q
node --check app/static/app.js
```

## Local workflow smoke test

Run the full local review-gated workflow (with an isolated temp DB/output path) in one command:

```bash
python scripts/local_workflow_smoke.py
```

The smoke script intentionally expects final export to remain blocked until a real production voiceover exists.
It now includes a production voiceover readiness check that is local-only and does not call external APIs.
It also includes a production voiceover dry-run request preview that makes no external API call.

## Main workflow

1. Create or seed a channel.
2. Create video ideas.
3. Generate assets for each video (LLM or local templates).
4. Review and approve the assets.
5. Create a visual asset plan and queue generation jobs.
6. Run local generation (`/run-local`) and manually approve each visual asset.
7. Render and review a local draft preview.
8. Build a production package.
9. Check production voiceover readiness (`GET /videos/{id}/final-voiceover/readiness`) — local-only, no API calls.
10. Preview production voiceover request (`POST /videos/{id}/final-voiceover/dry-run`) — dry run only, no API calls.
11. Generate final production voiceover (cloud TTS required, done later when you are ready to spend API credits).
12. Run the final export — creates `final.mp4`, `final_export_manifest.json`, `render_plan.json`, and `render_command.json`.
13. Prepare YouTube metadata payload.
14. Upload manually via YouTube Studio.
15. Mark published.

## Review gates

No gate can be bypassed. Each must be satisfied by a human operator:

| Gate | Endpoint |
|---|---|
| Asset review | `POST /videos/{id}/review` |
| Preview review | `POST /videos/{id}/preview/review` |
| Visual asset approval | `POST /visual-generation/assets/{id}/approve` |
| Voiceover readiness | `GET /videos/{id}/final-voiceover/readiness` |
| Voiceover dry-run preview | `POST /videos/{id}/final-voiceover/dry-run` |
| Final voiceover (cloud TTS only) | `POST /videos/{id}/final-voiceover/generate` |
| Metadata cleanup | Edit video title/description |
| Final export | `POST /videos/{id}/final-production/export` |

## Key endpoints

```text
GET    /health
POST   /channels
GET    /videos
POST   /videos
POST   /videos/{id}/generate
POST   /videos/{id}/review
POST   /videos/{id}/preview/render-draft
POST   /videos/{id}/preview/review
POST   /videos/{id}/package
POST   /videos/{id}/final-voiceover/generate
GET    /videos/{id}/final-production/status
POST   /videos/{id}/final-production/export
GET    /videos/{id}/final-voiceover/readiness
POST   /videos/{id}/final-voiceover/dry-run
POST   /visual-assets/from-video/{id}
POST   /visual-generation/plans/{id}/queue
POST   /visual-generation/jobs/{id}/run-local
POST   /visual-generation/assets/{id}/approve
POST   /visual-generation/assets/{id}/reject
POST   /publish/{id}/prepare-youtube-payload
POST   /publish/{id}/mark-published
```

## Environment

```text
DATABASE_URL=sqlite:///./content_factory.db
OUTPUT_DIR=./out
APP_ENV=development
REQUIRE_HUMAN_REVIEW=true
ENABLE_YOUTUBE_UPLOADS=false
INTERNAL_API_KEY=                # required when APP_ENV=production
OPENAI_API_KEY=                  # for LLM and TTS
OPENAI_MODEL=gpt-4o-mini
OPENAI_TTS_MODEL=gpt-4o-mini-tts
OPENAI_TTS_VOICE=onyx
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
IMAGE_GENERATION_PROVIDER=placeholder
```

The system works without an OpenAI key using deterministic local templates and placeholder visuals.
Production final voiceover readiness can be checked without API calls via `GET /videos/{id}/final-voiceover/readiness`.
Production voiceover dry-run request preview is available at `POST /videos/{id}/final-voiceover/dry-run` and never calls external APIs.
Dry-run now includes a source-quality gate (`source_quality_ready`, `source_quality_blockers`, `source_quality_warnings`) so operators can clean text before spending credits.
Dry run output does not create voiceover files and does not satisfy the final voiceover gate.
Real generation also refuses before any provider API request when source-quality blockers are present.
Local/Mac/silent preview audio is only for draft preview and is never accepted for final export.

## Safety boundary

This system **never** automatically uploads to YouTube. Upload is always manual. No automated upload path exists. The `ENABLE_YOUTUBE_UPLOADS` flag is enforced as `false` in all production gates and requires deliberate code changes to alter.

## Operator dashboard workflow controls

The selected-video panel in the dashboard includes workflow controls for:

- Approve all pending visual assets
- Approve video
- Render draft preview
- Mark preview reviewed
- Build package
- Prepare YouTube payload
- Generate publishing payload
- Check final production
- Run final export

These controls call the same backend endpoints as the curl flow and do not bypass review or production gates. Final export still blocks until production voiceover is present.
