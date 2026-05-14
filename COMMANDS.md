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

# Check final production readiness
curl http://127.0.0.1:8000/videos/1/final-production/status

# Create final export artifacts (runs when all gates pass)
curl -X POST http://127.0.0.1:8000/videos/1/final-production/export
```

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
