# Phase 31 — Real Production Voice Provider

## Goal
Add a real final voiceover generation path separate from draft preview audio.

Final voice files must live at:

`out/final_voiceovers/video_{video_id}/voiceover.mp3`

Metadata should live at:

`out/final_voiceovers/video_{video_id}/voiceover_meta.json`

## Rules
- Do not weaken Phase 30.
- Do not let macOS/silent/fallback/local TTS pass production readiness.
- Do not fake API calls.
- Do not require real API keys in tests.
- Keep draft preview TTS separate from final production voice.
- Keep tests passing.
- Add tests.

## Routes to add
- `GET /videos/{video_id}/final-voiceover/status`
- `POST /videos/{video_id}/final-voiceover/generate`

## Service to add
`app/services/final_voiceover.py`

Helpers:
- `final_voiceover_dir(video_id)`
- `final_voiceover_path_for_video(video_id)`
- `final_voiceover_meta_path_for_video(video_id)`
- `assess_final_voiceover(video_id)`
- `generate_final_voiceover(video, db, provider, voice, force)`

## Readiness
Voice readiness passes only when:
- `voiceover.mp3` exists and is non-empty
- `voiceover_meta.json` exists
- provider is `openai` or `elevenlabs`
- status is complete/succeeded/ready
- path is under `final_voiceovers`, not previews

Blocked providers:
- `macos`
- `silent`
- `fallback`
- `local`
- `placeholder`
- `none`

## Required blockers
- `Final voiceover must use a production voice provider`
- `Final voiceover file must be generated before final export`

## Phase 30 update
Update `app/services/final_production.py` so final voice readiness comes from final voiceover status, not draft preview render metadata.

## Tests to add
`tests/test_final_voiceover.py`

Required coverage:
1. Missing final voiceover blocks.
2. Empty mp3 blocks.
3. Missing metadata blocks.
4. macOS/silent/fallback/local providers block.
5. OpenAI metadata + non-empty mp3 passes.
6. ElevenLabs metadata + non-empty mp3 passes.
7. POST generate with missing API key does not create fake file.
8. Unsupported provider returns blocker.
9. Final production status no longer treats preview render_meta as production voice.
10. Publishing payload remains blocked until final voiceover exists.

## Validation
Run:

`python -m pytest -q`
`node --check app/static/app.js`

Expected: all tests pass; test count increases beyond 142.
