# Copy-paste commands

## Install and run

```bash
cd faceless_youtube_backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.seed
uvicorn app.main:app --reload
```

## Database migrations

```bash
# Upgrade to latest migration
alembic upgrade head

# Generate new migration after model changes
alembic revision --autogenerate -m "description of changes"
```

## Check health

```bash
curl http://127.0.0.1:8000/health
```

## Create a channel

```bash
curl -X POST http://127.0.0.1:8000/channels -H "Content-Type: application/json" -d '{"name":"Local AI Operator"}'
```

## Create 10 starter video ideas

```bash
curl -X POST http://127.0.0.1:8000/videos/ideas/bulk -H "Content-Type: application/json" -d '{"channel_id":1,"count":10}'
```

## Generate all assets for video 1

```bash
curl -X POST http://127.0.0.1:8000/videos/1/generate -H "Content-Type: application/json" -d '{"stage":"all"}'
```

## Review and approve video 1

```bash
curl -X POST http://127.0.0.1:8000/videos/1/review -H "Content-Type: application/json" -d '{"passed":true,"reviewer":"operator","notes":"Approved after manual review."}'
```

## Package video 1

```bash
curl -X POST http://127.0.0.1:8000/videos/1/package
```

## Prepare YouTube payload for video 1

```bash
curl -X POST http://127.0.0.1:8000/publish/1/prepare-youtube-payload
```

## Run tests

```bash
pytest -q
```
