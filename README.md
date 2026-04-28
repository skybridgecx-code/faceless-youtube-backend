# Faceless YouTube Backend

A runnable backend for a faceless YouTube content factory focused on original, reviewable local-business AI automation content.

It manages channel profiles, video ideas, scripts, Shorts, descriptions, thumbnail prompts, review gates, exportable production packages, and publish-ready metadata.

It intentionally does **not** blindly upload unreviewed AI content. YouTube publishing is kept behind an approval gate and a stub so you can connect OAuth safely later.

## Stack

- FastAPI
- SQLAlchemy
- SQLite by default
- Pydantic settings
- Pytest

## Quick start

```bash
cd faceless_youtube_backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000/docs
```

## Seed the first channel and ideas

```bash
python -m app.seed
```

## Run tests

```bash
pytest -q
```

## Main workflow

1. Create or seed a channel.
2. Create video ideas.
3. Generate assets for each video.
4. Review the assets.
5. Approve the video.
6. Build a production package.
7. Prepare YouTube metadata.
8. Upload manually or connect the YouTube adapter later.

## Important endpoints

```text
GET    /health
POST   /channels
GET    /channels
POST   /videos
GET    /videos
GET    /videos/{video_id}
POST   /videos/{video_id}/generate
POST   /videos/{video_id}/review
POST   /videos/{video_id}/package
GET    /videos/{video_id}/assets
POST   /publish/{video_id}/prepare-youtube-payload
POST   /publish/{video_id}/mark-published
```

## Environment

```text
DATABASE_URL=sqlite:///./content_factory.db
OUTPUT_DIR=./out
CHANNEL_DEFAULT_NAME=Local AI Operator
REQUIRE_HUMAN_REVIEW=true
ENABLE_YOUTUBE_UPLOADS=false
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.5
```

The backend works without an OpenAI key using deterministic local templates. Add an LLM provider later by replacing `app/services/content_engine.py` internals.

## Data model

- `Channel`: channel positioning and style guide
- `Video`: idea, content pillar, type, status, review state
- `ContentAsset`: generated script, shorts, description, thumbnail prompt, package manifest
- `Review`: approval/rejection notes
- `PublishRecord`: prepared/published metadata

## Review gate

A video cannot be packaged or marked publish-ready until it has a passing review. This prevents accidental upload of fake income claims, fake client results, unsupported claims, unreviewed synthetic media, or minimally transformed reused content.

## Next integrations to add

- Google OAuth + YouTube Data API upload
- ElevenLabs/PlayHT voiceover generation
- Canva/thumbnail generation
- CapCut/Descript export automation
- Queue worker with Redis/RQ or Celery
- Postgres for production
