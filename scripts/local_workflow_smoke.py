#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def contains_text(values: list[str], needle: str) -> bool:
    needle_lower = needle.lower().strip()
    if not needle_lower:
        return False
    return any(needle_lower in str(value).lower() for value in values)


@dataclass
class SmokeContext:
    root_dir: Path
    db_path: Path
    output_dir: Path
    video_id: int | None = None
    preview_path: str | None = None
    package_dir: str | None = None
    youtube_payload_ready: bool = False


class SmokeFailure(RuntimeError):
    pass


class StepPrinter:
    def __init__(self) -> None:
        self._step = 0

    def pass_step(self, message: str) -> None:
        self._step += 1
        print(f"PASS {self._step:02d}: {message}")

    def fail_now(self, message: str) -> None:
        self._step += 1
        raise SmokeFailure(f"FAIL {self._step:02d}: {message}")


def _expect_status(response: Any, expected: int, failure_message: str) -> None:
    if response.status_code != expected:
        detail = response.text.strip()
        raise SmokeFailure(f"{failure_message} (status={response.status_code}, body={detail})")


def _json(response: Any, failure_message: str) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise SmokeFailure(f"{failure_message}: {exc}") from exc
    if not isinstance(data, dict):
        raise SmokeFailure(f"{failure_message}: expected object JSON payload")
    return data


def _prepare_isolated_environment() -> SmokeContext:
    root_dir = Path(tempfile.mkdtemp(prefix="faceless_workflow_smoke_")).resolve()
    db_path = root_dir / "smoke.db"
    output_dir = root_dir / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["OUTPUT_DIR"] = str(output_dir)
    os.environ.setdefault("APP_ENV", "development")
    os.environ["INTERNAL_API_KEY"] = ""
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["ELEVENLABS_API_KEY"] = ""
    os.environ["ELEVENLABS_VOICE_ID"] = ""
    return SmokeContext(root_dir=root_dir, db_path=db_path, output_dir=output_dir)


def run_smoke() -> SmokeContext:
    ctx = _prepare_isolated_environment()
    steps = StepPrinter()

    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.db import SessionLocal
    from app.main import app
    from app.models import PublishRecord
    from app.security import InMemoryRateLimiter
    from sqlalchemy import select

    main_module.rate_limiter = InMemoryRateLimiter()

    with TestClient(app) as client:
        token = uuid.uuid4().hex[:8]
        channel_title = f"Smoke Channel {token}"
        video_title = f"Smoke Workflow Demo {token}"

        channel_resp = client.post("/channels", json={"name": channel_title})
        _expect_status(channel_resp, 200, "Unable to create smoke-test channel")
        channel = _json(channel_resp, "Channel response was not valid JSON")
        channel_id = int(channel["id"])

        video_resp = client.post(
            "/videos",
            json={"channel_id": channel_id, "title": video_title},
        )
        _expect_status(video_resp, 200, "Unable to create smoke-test video")
        video = _json(video_resp, "Video response was not valid JSON")
        video_id = int(video["id"])
        ctx.video_id = video_id
        steps.pass_step(f"Backend created demo video #{video_id}")

        generate_resp = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
        _expect_status(generate_resp, 200, "Asset generation failed")
        steps.pass_step("Core assets generated")

        plan_resp = client.post(f"/visual-assets/from-video/{video_id}")
        _expect_status(plan_resp, 200, "Visual asset plan creation failed")
        plan = _json(plan_resp, "Visual asset plan response was not valid JSON")
        plan_id = int(plan["id"])

        mark_ready_resp = client.post(f"/visual-assets/plans/{plan_id}/mark-ready")
        _expect_status(mark_ready_resp, 200, "Failed to mark visual plan ready")

        queue_resp = client.post(f"/visual-generation/plans/{plan_id}/queue", json={"provider": "local"})
        _expect_status(queue_resp, 200, "Failed to queue visual generation jobs")
        queue = _json(queue_resp, "Visual generation queue response was not valid JSON")
        jobs = list(queue.get("jobs") or [])
        if not jobs:
            raise SmokeFailure("FAIL 03: Visual generation queue returned no jobs")

        ran_jobs = 0
        for job in jobs[:2]:
            job_id = int(job["id"])
            run_resp = client.post(f"/visual-generation/jobs/{job_id}/run-local")
            _expect_status(run_resp, 200, f"run-local failed for job {job_id}")
            run_payload = _json(run_resp, "run-local response was not valid JSON")
            if str(run_payload.get("status")) != "imported":
                raise SmokeFailure(f"FAIL 03: run-local did not import output for job {job_id}")
            ran_jobs += 1
        if ran_jobs < 1:
            raise SmokeFailure("FAIL 03: No local visual placeholders were generated")
        steps.pass_step(f"Visual placeholders generated and registered ({ran_jobs} local job runs)")

        pending_resp = client.get("/visual-generation/assets/review-queue", params={"video_id": video_id, "review_status": "pending"})
        _expect_status(pending_resp, 200, "Failed to fetch pending visual review queue")
        pending_rows = pending_resp.json()
        if not isinstance(pending_rows, list) or not pending_rows:
            raise SmokeFailure("FAIL 04: Expected pending visual assets before manual approval")
        pending_ids = [int(row["id"]) for row in pending_rows if "id" in row]
        steps.pass_step(f"Visual assets remain pending until approved ({len(pending_ids)} pending)")

        blocked_preview_resp = client.post(f"/videos/{video_id}/preview/render-draft")
        if blocked_preview_resp.status_code != 409:
            raise SmokeFailure(
                "FAIL 05: Draft preview should refuse when video approval is missing "
                f"(status={blocked_preview_resp.status_code}, body={blocked_preview_resp.text.strip()})"
            )
        blocked_message = str(blocked_preview_resp.json().get("detail", ""))
        if "manually approved" not in blocked_message:
            raise SmokeFailure(f"FAIL 05: Unexpected preview refusal reason: {blocked_message}")
        steps.pass_step("Draft preview correctly refused before manual approvals")

        for asset_id in pending_ids:
            approve_resp = client.post(f"/visual-generation/assets/{asset_id}/approve")
            _expect_status(approve_resp, 200, f"Failed to approve visual asset {asset_id}")
        after_approve = client.get("/visual-generation/assets/review-queue", params={"video_id": video_id, "review_status": "pending"})
        _expect_status(after_approve, 200, "Failed to re-check visual review queue after approvals")
        if list(after_approve.json()):
            raise SmokeFailure("FAIL 06: Pending visual assets still exist after approval loop")
        steps.pass_step(f"Manual visual approval completed ({len(pending_ids)} approved)")

        review_resp = client.post(
            f"/videos/{video_id}/review",
            json={"passed": True, "reviewer": "smoke-test", "notes": "Workflow smoke approval"},
        )
        _expect_status(review_resp, 200, "Manual video approval failed")
        steps.pass_step("Manual video approval succeeded")

        preview_resp = client.post(f"/videos/{video_id}/preview/render-draft")
        _expect_status(
            preview_resp,
            200,
            "Draft preview render failed (requires local ffmpeg + macOS qlmanage)",
        )
        preview = _json(preview_resp, "Preview render response was not valid JSON")
        if not bool(preview.get("preview_exists")):
            raise SmokeFailure("FAIL 08: Preview render endpoint returned success but preview_exists=false")
        preview_path = str(preview.get("preview_path") or "")
        if not preview_path or not Path(preview_path).is_file():
            raise SmokeFailure(f"FAIL 08: Preview path missing on disk: {preview_path}")
        ctx.preview_path = preview_path
        steps.pass_step(f"Draft preview rendered at {preview_path}")

        preview_review_resp = client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True})
        _expect_status(preview_review_resp, 200, "Failed to mark preview reviewed")
        preview_review = _json(preview_review_resp, "Preview review response was not valid JSON")
        if not bool(preview_review.get("preview_reviewed")):
            raise SmokeFailure("FAIL 09: Preview review endpoint returned preview_reviewed=false")
        steps.pass_step("Preview manually reviewed")

        package_resp = client.post(f"/videos/{video_id}/package")
        _expect_status(package_resp, 200, "Package creation failed")
        package = _json(package_resp, "Package response was not valid JSON")
        package_dir = str(package.get("package_dir") or "")
        if not package_dir or not Path(package_dir).is_dir():
            raise SmokeFailure(f"FAIL 10: Package directory missing on disk: {package_dir}")
        ctx.package_dir = package_dir
        steps.pass_step(f"Package created at {package_dir}")

        payload_resp = client.post(f"/publish/{video_id}/prepare-youtube-payload")
        _expect_status(payload_resp, 200, "YouTube payload preparation failed")
        ctx.youtube_payload_ready = True
        steps.pass_step("YouTube payload prepared (manual upload path only)")

        voice_readiness_resp = client.get(f"/videos/{video_id}/final-voiceover/readiness")
        _expect_status(voice_readiness_resp, 200, "Failed to load final voiceover readiness")
        voice_readiness = _json(voice_readiness_resp, "Final voiceover readiness payload was not valid JSON")
        providers = list(voice_readiness.get("providers") or [])
        if len(providers) < 2:
            raise SmokeFailure("FAIL 12: Readiness payload did not include production providers")
        if not all("api_key_configured" in item for item in providers):
            raise SmokeFailure("FAIL 12: Readiness payload missing api_key_configured fields")
        available = [str(item) for item in voice_readiness.get("available_providers") or []]
        if available:
            raise SmokeFailure(f"FAIL 12: Isolated smoke env expected no configured production providers, got: {available}")
        if "current_final_voiceover_status" not in voice_readiness:
            raise SmokeFailure("FAIL 12: Readiness payload missing current_final_voiceover_status")
        steps.pass_step("Voiceover readiness endpoint reports production providers and blocked config state")

        dirty_source_resp = client.patch(
            f"/videos/{video_id}/assets/script",
            json={"body": "Draft Preview narrator notes: How to I Built this demo [INSERT LINK] [TBD]"},
        )
        _expect_status(dirty_source_resp, 200, "Failed to inject intentionally dirty source text")

        dry_run_dirty_resp = client.post(
            f"/videos/{video_id}/final-voiceover/dry-run",
            json={"provider": "openai", "max_chars": 5000},
        )
        _expect_status(dry_run_dirty_resp, 200, "Failed to load final voiceover dry run with dirty source")
        dry_run_dirty = _json(dry_run_dirty_resp, "Final voiceover dry run payload was not valid JSON")
        dirty_quality_blockers = [str(item) for item in dry_run_dirty.get("source_quality_blockers") or []]
        if bool(dry_run_dirty.get("dry_run")) is not True:
            raise SmokeFailure(f"FAIL 13: Expected dry_run=true, got: {dry_run_dirty.get('dry_run')}")
        if bool(dry_run_dirty.get("api_call_made")) is not False:
            raise SmokeFailure(f"FAIL 13: Expected api_call_made=false, got: {dry_run_dirty.get('api_call_made')}")
        if str(dry_run_dirty.get("provider")) != "openai":
            raise SmokeFailure(f"FAIL 13: Unexpected dry-run provider: {dry_run_dirty.get('provider')}")
        if bool(dry_run_dirty.get("source_quality_ready")) is not False:
            raise SmokeFailure("FAIL 13: Expected source_quality_ready=false for dirty source text")
        if not contains_text(dirty_quality_blockers, "placeholder") and not contains_text(dirty_quality_blockers, "insert link"):
            raise SmokeFailure(f"FAIL 13: Expected source quality blockers for dirty text, got: {dirty_quality_blockers}")
        steps.pass_step("Voiceover dry run detects dirty source-quality blockers without external API calls")

        clean_source_resp = client.patch(
            f"/videos/{video_id}/assets/script",
            json={
                "body": (
                    "This final narration explains practical local automation workflows with clear educational framing, "
                    "manual review boundaries, and a step-by-step operator walkthrough for deployment readiness."
                )
            },
        )
        _expect_status(clean_source_resp, 200, "Failed to clean source text for dry run quality check")
        dry_run_clean_resp = client.post(
            f"/videos/{video_id}/final-voiceover/dry-run",
            json={"provider": "openai", "max_chars": 5000},
        )
        _expect_status(dry_run_clean_resp, 200, "Failed to load final voiceover dry run with clean source")
        dry_run_clean = _json(dry_run_clean_resp, "Final voiceover dry run payload was not valid JSON")
        dry_run_clean_blockers = [str(item) for item in dry_run_clean.get("blockers") or []]
        if bool(dry_run_clean.get("source_quality_ready")) is not True:
            raise SmokeFailure(
                f"FAIL 14: Expected source_quality_ready=true for clean source text, got: {dry_run_clean.get('source_quality_ready')}"
            )
        if not contains_text(dry_run_clean_blockers, "OPENAI_API_KEY"):
            raise SmokeFailure(f"FAIL 14: Expected missing OPENAI_API_KEY blocker, got: {dry_run_clean_blockers}")
        steps.pass_step("Voiceover dry run source quality becomes ready after source cleanup")

        dirty_title = f"{video_title} [INSERT LINK]"
        dirty_resp = client.patch(f"/videos/{video_id}", json={"title": dirty_title})
        _expect_status(dirty_resp, 200, "Failed to inject metadata blocker into title")
        status_dirty_resp = client.get(f"/videos/{video_id}/final-production/status")
        _expect_status(status_dirty_resp, 200, "Failed to read final production status")
        status_dirty = _json(status_dirty_resp, "Final production status payload was not valid JSON")
        dirty_blockers = [str(item) for item in status_dirty.get("blockers") or []]
        if bool(status_dirty.get("final_metadata_ready")):
            raise SmokeFailure("FAIL 15: Metadata should be blocked after injecting placeholder text")
        if not contains_text(dirty_blockers, "metadata"):
            raise SmokeFailure(f"FAIL 15: Expected metadata blocker, got: {dirty_blockers}")
        steps.pass_step("Final metadata blockers detected")

        clean_resp = client.patch(f"/videos/{video_id}", json={"title": video_title})
        _expect_status(clean_resp, 200, "Failed to clean metadata title")
        clean_description_resp = client.patch(
            f"/videos/{video_id}/assets/description",
            json={"body": "Production-ready educational video metadata. Manual upload workflow only."},
        )
        _expect_status(clean_description_resp, 200, "Failed to clean description metadata asset")
        status_clean_resp = client.get(f"/videos/{video_id}/final-production/status")
        _expect_status(status_clean_resp, 200, "Failed to re-check final production status")
        status_clean = _json(status_clean_resp, "Final production status payload was not valid JSON")
        if not bool(status_clean.get("final_metadata_ready")):
            raise SmokeFailure(
                f"FAIL 16: Metadata did not become ready after cleanup. blockers={status_clean.get('blockers')}"
            )
        steps.pass_step("Metadata cleanup restored final metadata readiness")

        final_export_resp = client.post(f"/videos/{video_id}/final-production/export")
        _expect_status(final_export_resp, 200, "Final export endpoint request failed unexpectedly")
        final_export = _json(final_export_resp, "Final export response was not valid JSON")
        export_blockers = [str(item) for item in final_export.get("blockers") or []]
        if str(final_export.get("status")) != "blocked":
            raise SmokeFailure(f"FAIL 17: Final export should be blocked without production voiceover: {final_export}")
        if not contains_text(export_blockers, "voiceover"):
            raise SmokeFailure(f"FAIL 17: Expected voiceover blocker, got: {export_blockers}")
        steps.pass_step("Final export refused because production final voiceover is missing")

        db = SessionLocal()
        try:
            has_published_record = db.scalar(
                select(PublishRecord.id)
                .where(PublishRecord.video_id == video_id, PublishRecord.published.is_(True))
                .limit(1)
            )
        finally:
            db.close()
        if has_published_record is not None:
            raise SmokeFailure("FAIL 18: Smoke test unexpectedly marked video as published")
        steps.pass_step("No YouTube auto-upload occurred (manual publish record remains unset)")

    return ctx


def print_summary(ctx: SmokeContext) -> None:
    print("")
    print("Smoke test summary")
    print(f"- video_id: {ctx.video_id}")
    print(f"- isolated_root: {ctx.root_dir}")
    print(f"- isolated_db: {ctx.db_path}")
    print(f"- output_dir: {ctx.output_dir}")
    print(f"- preview_path: {ctx.preview_path or 'n/a'}")
    print(f"- package_dir: {ctx.package_dir or 'n/a'}")
    print(f"- youtube_payload_prepared: {ctx.youtube_payload_ready}")
    print("- final_export: blocked by missing production final voiceover (expected)")
    print("- note: Production voiceover requires OpenAI or ElevenLabs and is intentionally deferred.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a local, review-gated smoke test for the faceless-youtube-backend workflow "
            "using an isolated temporary database and output directory."
        )
    )
    _ = parser.parse_args()

    try:
        ctx = run_smoke()
        print_summary(ctx)
        return 0
    except SmokeFailure as exc:
        print(str(exc))
        print("Smoke test failed. Review the failing step above and resolve the gate before retrying.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: unexpected smoke test error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
