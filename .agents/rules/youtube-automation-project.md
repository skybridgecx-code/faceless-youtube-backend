---
trigger: always_on
---

YouTube Automation Project Rule

Always follow the project instructions in:

YOUTUBE_AUTOMATION_PROJECT_INSTRUCTIONS.md

Work only in this repo:

$HOME/Downloads/faceless_youtube_backend

Do not work in frontdesk-os, skybridgecx-agent-factory, commercepilot, or any other repo.

Core Rules
Complete only one phase at a time.
Do not build future phases early.
Do not add YouTube auto-upload yet.
Do not bypass or remove manual review.
Do not change YouTube privacy default to public.
Do not create fake income claims.
Do not create fake client results.
Do not claim real businesses use the system unless verified.
Do not store secrets in git.
Do not hardcode API keys.
Do not use Claude-style UI by default.
Use an original premium dark AI operations dashboard style.
Preserve existing backend behavior unless the phase explicitly says to change it.
Current Product

This is a faceless YouTube automation system for the channel:

Local AI Operator

Niche:

AI automation for local service businesses.

The current backend already supports:

GET /health
GET /channels
GET /videos
POST /videos/{video_id}/generate
GET /videos/{video_id}/assets
POST /videos/{video_id}/review
POST /videos/{video_id}/package
POST /publish/{video_id}/prepare-youtube-payload
Validation

After each phase, run these commands:

cd "$HOME/Downloads/faceless_youtube_backend"

source .venv/bin/activate

python -m pytest

python -m uvicorn app.main:app --reload --port 8000

Then open:

http://127.0.0.1:8000/app

After Each Phase

Summarize:

Files changed
What was added
Exact commands to run
Any known issues

Then remind the operator to commit.

Commit Rule

After successful validation, tell the operator to run these commands:

cd "$HOME/Downloads/faceless_youtube_backend"

source .venv/bin/activate

python -m pytest

git status -sb

git add .

git commit -m "phase X: short description"

END