# Copy-paste commands

## Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
python -m app.seed      # optional: seed first channel
uvicorn app.main:app --reload
```

## Validation

```bash
# Full test suite
python3 -m pytest -q

# JS syntax check
node --check app/static/app.js

# Git status
git status -sb
```

## One-command local workflow smoke test

```bash
python scripts/local_workflow_smoke.py
```

Notes:
- Uses isolated temporary SQLite DB + `OUTPUT_DIR` by default (no live uvicorn required).
- Verifies review gates end-to-end.
- Verifies production voiceover readiness endpoint without calling external APIs.
- Verifies production voiceover dry-run preview endpoint without calling external APIs.
- Expected final result is blocked final export when production voiceover is missing.

## Local renderer prerequisite

```bash
ffmpeg -version
```

## Database migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Check current state
alembic current

# Create a new migration after model changes
alembic revision --autogenerate -m "description of changes"

# Downgrade one step
alembic downgrade -1
```

## Health check

```bash
curl http://127.0.0.1:8000/health
```

## Create a channel

```bash
curl -X POST http://127.0.0.1:8000/channels \
  -H "Content-Type: application/json" \
  -d '{"name":"Local AI Operator"}'
```

## Create 10 starter video ideas

```bash
curl -X POST http://127.0.0.1:8000/videos/ideas/bulk \
  -H "Content-Type: application/json" \
  -d '{"channel_id":1,"count":10}'
```

## Generate all assets for video 1

```bash
curl -X POST http://127.0.0.1:8000/videos/1/generate \
  -H "Content-Type: application/json" \
  -d '{"stage":"all"}'
```

## Review and approve video 1

```bash
curl -X POST http://127.0.0.1:8000/videos/1/review \
  -H "Content-Type: application/json" \
  -d '{"passed":true,"reviewer":"operator","notes":"Approved after manual review."}'
```

## Package video 1

```bash
curl -X POST http://127.0.0.1:8000/videos/1/package
```

## Visual generation workflow

```bash
# Create a visual asset plan from the video
curl -X POST http://127.0.0.1:8000/visual-assets/from-video/1

# Queue local generation jobs (plan_id from above)
curl -X POST http://127.0.0.1:8000/visual-generation/plans/1/queue \
  -H "Content-Type: application/json" \
  -d '{"provider":"local"}'

# Run local generation for job 1
curl -X POST http://127.0.0.1:8000/visual-generation/jobs/1/run-local

# Check pending review queue
curl http://127.0.0.1:8000/visual-generation/assets/review-queue

# Manually approve an asset
curl -X POST http://127.0.0.1:8000/visual-generation/assets/1/approve

# Manually reject with notes
curl -X POST http://127.0.0.1:8000/visual-generation/assets/1/reject \
  -H "Content-Type: application/json" \
  -d '{"review_notes":"Composition off, regenerate"}'
```

## Final production workflow

```bash
# Render draft preview (for QA — not sufficient for production)
curl -X POST http://127.0.0.1:8000/videos/1/preview/render-draft

# Confirm preview reviewed
curl -X POST http://127.0.0.1:8000/videos/1/preview/review \
  -H "Content-Type: application/json" \
  -d '{"reviewed":true}'

# Generate production voiceover (requires OPENAI_API_KEY)
curl -X POST http://127.0.0.1:8000/videos/1/final-voiceover/generate \
  -H "Content-Type: application/json" \
  -d '{"provider":"openai"}'

# Check production voiceover readiness (local-only, no external API call)
curl http://127.0.0.1:8000/videos/1/final-voiceover/readiness

# Preview production voiceover request (dry run only, no external API call)
curl -X POST http://127.0.0.1:8000/videos/1/final-voiceover/dry-run \
  -H "Content-Type: application/json" \
  -d '{"provider":"openai","max_chars":5000}'

# Check final production readiness
curl http://127.0.0.1:8000/videos/1/final-production/status

# Create final export artifacts with ffmpeg (runs when all gates pass)
curl -X POST http://127.0.0.1:8000/videos/1/final-production/export
```

Note: production voiceover setup (OpenAI/ElevenLabs credentials and quota handling) may be deferred. In that case final export remains correctly blocked until `final_voice_ready=true`.
Local/Mac/silent preview audio is never treated as production final voiceover.
Dry-run preview does not create voiceover files and does not satisfy the production voiceover gate.

Required env vars for production voiceover generation:
- OpenAI: `OPENAI_API_KEY` (optional overrides: `OPENAI_TTS_MODEL`, `OPENAI_TTS_VOICE`)
- ElevenLabs: `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` (optional override: `ELEVENLABS_MODEL_ID`)

## Dashboard workflow controls (selected video)

Use the dashboard panel `Selected Video Workflow Controls` for these actions without curl:

- Refresh selected video status
- Approve all pending visual assets
- Approve video
- Render draft preview
- Mark preview reviewed
- Build package
- Prepare YouTube payload
- Generate publishing payload
- Check final production
- Run final export

Curl fallback commands remain the source of truth when scripting or troubleshooting.

## Prepare YouTube payload for video 1

```bash
curl -X POST http://127.0.0.1:8000/publish/1/prepare-youtube-payload
```

## Mark video as published (after manual upload)

```bash
curl -X POST http://127.0.0.1:8000/publish/1/mark-published \
  -H "Content-Type: application/json" \
  -d '{"youtube_video_id":"abc123xyz"}'
```

## Internal API key (production mode)

```bash
# Add X-Internal-API-Key header for write operations in production
curl -X POST http://127.0.0.1:8000/videos/1/package \
  -H "X-Internal-API-Key: your-key-here"
```
