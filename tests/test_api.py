import os
import shutil
import tempfile
import urllib.error
import json
from pathlib import Path
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///./test_content_factory.db"
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="yt_factory_test_")

from fastapi.testclient import TestClient  # noqa: E402

import app.routers.videos as videos_router  # noqa: E402
import app.routers.research as research_router  # noqa: E402
import app.services.content_engine as content_engine  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
import app.main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent, ContentAgent, ContentType, PublishRecord, Video, VideoOpportunity, VideoPerformanceMetric, VisualAssetPlan, VisualGeneratedAsset, VisualScene  # noqa: E402
from app.services.agents import seed_default_agents_if_empty  # noqa: E402
from app.services.research import SourceChannel, SourceVideo, build_research_patterns, build_research_strategy  # noqa: E402
from app.security import InMemoryRateLimiter  # noqa: E402
from tests.db_helpers import reset_migrated_test_database  # noqa: E402


def setup_function() -> None:
    reset_migrated_test_database()
    main_module.rate_limiter = InMemoryRateLimiter()
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def _write_real_preview_file(video_id: int) -> Path:
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    preview_path = output_dir / "previews" / str(video_id) / "draft.mp4"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    )
    return preview_path


def _write_final_voiceover_production(video_id: int) -> None:
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    voiceover_dir = output_dir / "final_voiceovers" / f"video_{video_id}"
    voiceover_dir.mkdir(parents=True, exist_ok=True)
    (voiceover_dir / "voiceover.mp3").write_bytes(b"\xff\xfb\x90\x00" * 16)
    meta = {"provider": "openai", "voice": "onyx", "model": "gpt-4o-mini-tts", "status": "complete"}
    (voiceover_dir / "voiceover_meta.json").write_text(json.dumps(meta))


def _write_final_export_file(video_id: int) -> Path:
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    final_path = output_dir / "final_exports" / f"video_{video_id}" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    return final_path


def _write_clean_description_asset(video_id: int) -> None:
    """Insert a clean description asset so the Phase 30 metadata gate passes."""
    from app.models import ContentAsset
    from app.models import AssetType as _AT
    from app.db import SessionLocal as _SL
    db = _SL()
    try:
        db.add(ContentAsset(
            video_id=video_id,
            asset_type=_AT.description,
            body="A production-ready educational video about AI automation. Subscribe for more.",
        ))
        db.commit()
    finally:
        db.close()


def _write_real_visual_asset_file(name: str = "visual_asset.png") -> Path:
    output_dir = Path(os.environ["OUTPUT_DIR"]).resolve()
    asset_path = output_dir / "generated" / name
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return asset_path


def _create_approved_video_for_preview(client: TestClient, channel_name: str, title: str) -> int:
    channel_id = client.post("/channels", json={"name": channel_name}).json()["id"]
    video_id = client.post("/videos", json={"channel_id": channel_id, "title": title}).json()["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    return video_id


def _install_fake_preview_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PREVIEW_TTS_PROVIDER", "auto")

    def fake_which(binary: str) -> str | None:
        if binary == "ffmpeg":
            return "/usr/bin/ffmpeg"
        if binary == "qlmanage":
            return "/usr/bin/qlmanage"
        if binary == "say":
            return None
        return shutil.which(binary)

    class FakeCompletedProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def fake_run(command: list[str], capture_output: bool, text: bool) -> FakeCompletedProcess:
        if command and command[0].endswith("qlmanage"):
            output_dir = Path(command[command.index("-o") + 1])
            html_path = Path(command[-1])
            png_path = output_dir / f"{html_path.name}.png"
            png_path.parent.mkdir(parents=True, exist_ok=True)
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        else:
            output_path = Path(command[-1])
            if output_path.suffix == ".mp4":
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(
                    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat"
                )
        return FakeCompletedProcess()

    monkeypatch.setattr(videos_router.shutil, "which", fake_which)
    monkeypatch.setattr(videos_router.subprocess, "run", fake_run)


@pytest.mark.usefixtures("monkeypatch")
def test_render_draft_preview_creates_non_empty_mp4_when_ffmpeg_available(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Renderer Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post("/videos", json={"channel_id": channel_id, "title": "Renderer Demo"})
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200
    patch_response = client.patch(
        f"/videos/{video_id}/assets/script",
        json={"body": "This is a safe draft script for local preview review only."},
    )
    assert patch_response.status_code == 200
    review_response = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"})
    assert review_response.status_code == 200
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PREVIEW_TTS_PROVIDER", "auto")

    def fake_which(binary: str) -> str | None:
        if binary == "ffmpeg":
            return "/usr/bin/ffmpeg"
        if binary == "qlmanage":
            return "/usr/bin/qlmanage"
        if binary == "say":
            return None
        return shutil.which(binary)

    class FakeCompletedProcess:
        def __init__(self, returncode: int = 0) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(command: list[str], capture_output: bool, text: bool) -> FakeCompletedProcess:
        if command and command[0].endswith("qlmanage"):
            output_dir = Path(command[command.index("-o") + 1])
            html_path = Path(command[-1])
            png_path = output_dir / f"{html_path.name}.png"
            png_path.parent.mkdir(parents=True, exist_ok=True)
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        else:
            output_path = Path(command[-1])
            if output_path.suffix == ".mp4":
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(
                    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat"
                )
        return FakeCompletedProcess(0)

    monkeypatch.setattr(videos_router.shutil, "which", fake_which)
    monkeypatch.setattr(videos_router.subprocess, "run", fake_run)

    render_response = client.post(f"/videos/{video_id}/preview/render-draft")
    assert render_response.status_code == 200
    render_payload = render_response.json()
    assert render_payload["preview_exists"] is True
    assert render_payload["audio_generated"] is False
    assert "tts_provider" in render_payload
    assert "tts_voice" in render_payload
    assert "tts_model" in render_payload

    preview_path = Path(render_payload["preview_path"])
    assert preview_path.exists()
    assert preview_path.stat().st_size > 0


@pytest.mark.usefixtures("monkeypatch")
def test_preview_status_does_not_expose_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "No Leak Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]
    video_response = client.post("/videos", json={"channel_id": channel_id, "title": "No Leak Demo"})
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "ok"})

    monkeypatch.setenv("PREVIEW_TTS_PROVIDER", "auto")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "LEAK_TEST_ELEVEN_KEY")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice_x")
    monkeypatch.setenv("OPENAI_API_KEY", "LEAK_TEST_OPENAI_KEY")
    monkeypatch.setenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    monkeypatch.setenv("OPENAI_TTS_VOICE", "marin")

    def fake_which(binary: str) -> str | None:
        if binary == "ffmpeg":
            return "/usr/bin/ffmpeg"
        if binary == "qlmanage":
            return "/usr/bin/qlmanage"
        if binary == "say":
            return None
        return shutil.which(binary)

    class FakeCompletedProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def fake_run(command: list[str], capture_output: bool, text: bool) -> FakeCompletedProcess:
        if command and command[0].endswith("qlmanage"):
            output_dir = Path(command[command.index("-o") + 1])
            html_path = Path(command[-1])
            (output_dir / f"{html_path.name}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        else:
            output_path = Path(command[-1])
            if output_path.suffix == ".mp4":
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        return FakeCompletedProcess()

    def fake_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        raise urllib.error.URLError("network disabled for test")

    monkeypatch.setattr(videos_router.shutil, "which", fake_which)
    monkeypatch.setattr(videos_router.subprocess, "run", fake_run)
    monkeypatch.setattr(videos_router.urllib.request, "urlopen", fake_urlopen)

    render_response = client.post(f"/videos/{video_id}/preview/render-draft")
    assert render_response.status_code == 200
    status_response = client.get(f"/videos/{video_id}/preview/status")
    assert status_response.status_code == 200
    body = status_response.text
    assert "LEAK_TEST_ELEVEN_KEY" not in body
    assert "LEAK_TEST_OPENAI_KEY" not in body


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_health_remains_public_with_internal_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_content_engine_without_openai_key_preserves_deterministic_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", None)
    content_engine.clear_asset_cache()

    def should_not_call(_prompt: str) -> str:  # noqa: ANN001
        raise AssertionError("LLM request should not run when OPENAI_API_KEY is missing.")

    monkeypatch.setattr(content_engine, "_llm_text_request", should_not_call)
    video = Video(
        id=11,
        channel_id=1,
        title="Deterministic Template Video",
        content_type=ContentType.long,
        pillar="AI call handling",
        target_viewer="Owner",
        pain_point="Missed calls",
        demo_idea="Workflow demo",
        thumbnail_text="DETERMINISTIC",
    )

    script = content_engine.build_script(video)
    brief = content_engine.build_brief(video)
    description = content_engine.build_description(video)
    assert "## 0:00 Hook" in script
    assert "# Creative Brief" in brief
    assert "This is an educational/demo video." in description


def test_content_engine_short_script_uses_shorts_structure_without_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", None)
    content_engine.clear_asset_cache()

    short_video = Video(
        id=12,
        channel_id=1,
        title="Short Script Demo",
        content_type=ContentType.short,
        pillar="AI call handling",
        target_viewer="Owner",
        pain_point="Missed calls",
        demo_idea="Workflow demo",
        thumbnail_text="SHORT DEMO",
    )
    long_video = Video(
        id=13,
        channel_id=1,
        title="Long Script Demo",
        content_type=ContentType.long,
        pillar="AI call handling",
        target_viewer="Owner",
        pain_point="Missed calls",
        demo_idea="Workflow demo",
        thumbnail_text="LONG DEMO",
    )

    short_script = content_engine.build_script(short_video)
    long_script = content_engine.build_script(long_video)
    assert "Short-Form Script (45-60s)" in short_script
    assert "0:00-0:03 Hook" in short_script
    assert "Vertical Demo Direction" in short_script
    assert "8:45 CTA" in long_script


def test_content_engine_llm_success_changes_core_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    content_engine.clear_asset_cache()

    def fake_llm(_prompt: str) -> str:  # noqa: ANN001
        return "Safe custom asset variant. Educational framing only."

    monkeypatch.setattr(content_engine, "_llm_text_request", fake_llm)
    video = Video(
        id=21,
        channel_id=1,
        title="LLM Success Video",
        content_type=ContentType.long,
        pillar="AI call handling",
        target_viewer="Owner",
        pain_point="Missed calls",
        demo_idea="Workflow demo",
        thumbnail_text="LLM SUCCESS",
    )

    script = content_engine.build_script(video)
    brief = content_engine.build_brief(video)
    description = content_engine.build_description(video)
    assert script == "Safe custom asset variant. Educational framing only."
    assert brief == "Safe custom asset variant. Educational framing only."
    assert description == "Safe custom asset variant. Educational framing only."


def test_content_engine_llm_failure_falls_back_to_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    content_engine.clear_asset_cache()

    def fail_llm(_prompt: str) -> str:  # noqa: ANN001
        raise RuntimeError("simulated llm failure")

    monkeypatch.setattr(content_engine, "_llm_text_request", fail_llm)
    video = Video(
        id=31,
        channel_id=1,
        title="LLM Failure Video",
        content_type=ContentType.long,
        pillar="AI call handling",
        target_viewer="Owner",
        pain_point="Missed calls",
        demo_idea="Workflow demo",
        thumbnail_text="LLM FAILURE",
    )

    brief = content_engine.build_brief(video)
    assert "# Creative Brief" in brief


def test_content_engine_unsafe_llm_output_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    content_engine.clear_asset_cache()

    def unsafe_llm(_prompt: str) -> str:  # noqa: ANN001
        return "Guaranteed results. You will make $10k fast."

    monkeypatch.setattr(content_engine, "_llm_text_request", unsafe_llm)
    video = Video(id=41, channel_id=1, title="LLM Unsafe Video")

    script = content_engine.build_script(video)
    assert "## 0:00 Hook" in script
    assert "Guaranteed results" not in script


def test_content_engine_short_script_unsafe_llm_output_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    content_engine.clear_asset_cache()

    def unsafe_llm(_prompt: str) -> str:  # noqa: ANN001
        return "Guaranteed results. You will make $10k fast."

    monkeypatch.setattr(content_engine, "_llm_text_request", unsafe_llm)
    video = Video(
        id=42,
        channel_id=1,
        title="Unsafe Short Script",
        content_type=ContentType.short,
        pillar="AI call handling",
        target_viewer="Owner",
        pain_point="Missed calls",
        demo_idea="Workflow demo",
        thumbnail_text="UNSAFE SHORT",
    )

    script = content_engine.build_script(video)
    assert "Short-Form Script (45-60s)" in script
    assert "Guaranteed results" not in script


def test_generate_video_ideas_parses_valid_json_from_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    content_engine.clear_asset_cache()

    payload = [
        {
            "title": "Idea One",
            "pillar": "AI call handling",
            "target_viewer": "Owner",
            "pain_point": "Missed calls",
            "demo_idea": "Lead routing demo",
            "thumbnail_text": "IDEA ONE",
        },
        {
            "title": "Idea Two",
            "pillar": "Business dashboard demos",
            "target_viewer": "Operator",
            "pain_point": "No follow-up",
            "demo_idea": "Dashboard walk-through",
            "thumbnail_text": "IDEA TWO",
        },
    ]

    monkeypatch.setattr(content_engine, "_llm_text_request", lambda _prompt: json.dumps(payload))
    ideas = content_engine.generate_video_ideas(2)
    assert ideas == payload


def test_generate_video_ideas_pads_invalid_items_with_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    content_engine.clear_asset_cache()

    payload = [
        {
            "title": "Valid Idea",
            "pillar": "Tool stack and tutorials",
            "target_viewer": "Local operator",
            "pain_point": "Messy workflow",
            "demo_idea": "Safe walkthrough",
            "thumbnail_text": "VALID IDEA",
        },
        {
            "title": "Missing Field Idea",
            "pillar": "AI call handling",
            "target_viewer": "Owner",
            "pain_point": "Missed calls",
            "thumbnail_text": "BROKEN",
        },
        "bad-row",
    ]

    monkeypatch.setattr(content_engine, "_llm_text_request", lambda _prompt: json.dumps(payload))
    ideas = content_engine.generate_video_ideas(4)
    assert len(ideas) == 4
    assert ideas[0]["title"] == "Valid Idea"
    assert ideas[1]["title"] == "This Is Why Contractors Miss So Many Leads"
    assert ideas[2]["title"] == "I Built a Dashboard That Tracks Every Missed Call"
    assert ideas[3]["title"] == "AI Phone Agent vs Human Receptionist"


def test_clear_asset_cache_clears_only_requested_video(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    content_engine.clear_asset_cache()

    calls: dict[str, int] = {"count": 0}

    def fake_llm(prompt: str) -> str:
        calls["count"] += 1
        if "Video A" in prompt:
            return f"Safe asset for Video A - call {calls['count']}"
        return f"Safe asset for Video B - call {calls['count']}"

    monkeypatch.setattr(content_engine, "_llm_text_request", fake_llm)

    video_a = Video(id=501, channel_id=1, title="Video A")
    video_b = Video(id=502, channel_id=1, title="Video B")

    first_a = content_engine.build_script(video_a)
    first_b = content_engine.build_script(video_b)
    assert calls["count"] == 2

    second_a = content_engine.build_script(video_a)
    second_b = content_engine.build_script(video_b)
    assert calls["count"] == 2
    assert second_a == first_a
    assert second_b == first_b

    content_engine.clear_asset_cache(video_id=501)
    third_a = content_engine.build_script(video_a)
    third_b = content_engine.build_script(video_b)
    assert calls["count"] == 3
    assert third_a != first_a
    assert third_b == first_b


def test_shorts_batch_creates_requested_count_capped_and_generates_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", None)
    content_engine.clear_asset_cache()

    def should_not_call(_prompt: str) -> str:  # noqa: ANN001
        raise AssertionError("LLM request should not run when OPENAI_API_KEY is missing.")

    monkeypatch.setattr(content_engine, "_llm_text_request", should_not_call)
    channel = client.post("/channels", json={"name": "Shorts Batch Channel"})
    assert channel.status_code == 200

    response = client.post(
        "/shorts/batch",
        json={
            "count": 12,
            "pillar": "Shorts QA",
            "topic_seed": "Missed call fix",
            "target_viewer": "Local owner",
            "auto_generate_assets": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["requested_count"] == 12
    assert payload["created_count"] == 10
    assert len(payload["videos"]) == 10
    assert any("Created 10 shorts candidates" in warning for warning in payload["warnings"])
    assert payload["next_required_action"]

    for row in payload["videos"]:
        assert row["content_type"] == "short"
        assert row["approved"] is False
        assert row["preview_reviewed"] is False
        assert row["generated_assets_count"] > 0
        assert row["next_required_action"]
        video = client.get(f"/videos/{row['id']}").json()
        assert video["approved"] is False
        assert video["preview_reviewed"] is False
        assets = client.get(f"/videos/{row['id']}/assets").json()
        assert len(assets) > 0


def test_shorts_batch_respects_auto_generate_assets_false() -> None:
    client = TestClient(app)
    channel = client.post("/channels", json={"name": "Shorts Batch No Auto Assets"})
    assert channel.status_code == 200

    response = client.post(
        "/shorts/batch",
        json={
            "count": 3,
            "pillar": "No Auto Assets",
            "auto_generate_assets": False,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["created_count"] == 3
    assert "Generate assets" in payload["next_required_action"]
    for row in payload["videos"]:
        assert row["generated_assets_count"] == 0
        assert row["workflow_status"] == "idea"
        assets = client.get(f"/videos/{row['id']}/assets").json()
        assert assets == []
        video = client.get(f"/videos/{row['id']}").json()
        assert video["approved"] is False
        assert video["preview_reviewed"] is False


def test_shorts_batch_queue_lists_shorts_only_and_filters() -> None:
    client = TestClient(app)
    channel = client.post("/channels", json={"name": "Shorts Queue Channel"})
    assert channel.status_code == 200
    channel_id = channel.json()["id"]

    first = client.post(
        "/shorts/batch",
        json={"count": 2, "pillar": "Queue A", "auto_generate_assets": True},
    )
    second = client.post(
        "/shorts/batch",
        json={"count": 2, "pillar": "Queue B", "auto_generate_assets": False},
    )
    assert first.status_code == 200
    assert second.status_code == 200

    long_video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Long Form Control", "content_type": "long"},
    )
    assert long_video.status_code == 200

    queue = client.get("/shorts/batch-queue?limit=50")
    assert queue.status_code == 200
    rows = queue.json()
    assert rows
    assert all(row["content_type"] == "short" for row in rows)
    assert all("next_required_action" in row and row["next_required_action"] for row in rows)

    pillar_filtered = client.get("/shorts/batch-queue?pillar=Queue A&limit=50")
    assert pillar_filtered.status_code == 200
    filtered_rows = pillar_filtered.json()
    assert filtered_rows
    assert all(row["pillar"] == "Queue A" for row in filtered_rows)

    status_filtered = client.get("/shorts/batch-queue?status=idea&limit=50")
    assert status_filtered.status_code == 200
    assert all(row["workflow_status"] == "idea" for row in status_filtered.json())


def test_shorts_batch_queue_limit_max_enforced() -> None:
    client = TestClient(app)
    response = client.get("/shorts/batch-queue?limit=201")
    assert response.status_code == 422


def test_protected_write_rejects_without_internal_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")
    response = client.post("/channels", json={"name": "Denied Channel"})
    assert response.status_code == 401
    assert "invalid" in response.json()["detail"].lower()


def test_protected_write_accepts_with_internal_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")
    response = client.post(
        "/channels",
        json={"name": "Allowed Channel"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Allowed Channel"


def test_cors_blocks_arbitrary_origin_when_production_origins_configured() -> None:
    settings = main_module.get_settings().__class__(
        app_env="production",
        allowed_origins="https://safe.example",
    )
    cors_app = FastAPI()
    cors_app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @cors_app.get("/health")
    def _health() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(cors_app)
    blocked = client.options(
        "/health",
        headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert blocked.headers.get("access-control-allow-origin") is None

    allowed = client.options(
        "/health",
        headers={
            "Origin": "https://safe.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.headers.get("access-control-allow-origin") == "https://safe.example"


def test_content_workflow() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Test Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post(
        "/videos",
        json={
            "channel_id": channel_id,
            "title": "I Built an AI Receptionist for a Roofing Company",
            "thumbnail_text": "AI ROOFING RECEPTIONIST",
        },
    )
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200
    assert len(generated_response.json()) >= 5

    bad_script_response = client.patch(
        f"/videos/{video_id}/assets/script",
        json={"body": "Here is an income guarantee that you will make 10k a month."},
    )
    assert bad_script_response.status_code == 200

    compliance_response = client.post(f"/videos/{video_id}/compliance/run")
    assert compliance_response.status_code == 200
    assert compliance_response.json()["overall_status"] == "blocked"

    blocked_package = client.post(f"/videos/{video_id}/package")
    assert blocked_package.status_code == 409

    review_response_fail = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "Looks safe"})
    assert review_response_fail.status_code == 400
    assert "compliance" in review_response_fail.json()["detail"].lower()

    client.patch(f"/videos/{video_id}/assets/script", json={"body": "A normal safe script about AI."})
    compliance_response = client.post(f"/videos/{video_id}/compliance/run")
    assert compliance_response.status_code == 200
    assert compliance_response.json()["overall_status"] != "blocked"

    review_response = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "Looks safe"})
    assert review_response.status_code == 200
    assert review_response.json()["passed"] is True

    readiness_before_preview = client.get(f"/videos/{video_id}/readiness")
    assert readiness_before_preview.status_code == 200
    assert readiness_before_preview.json()["preview_reviewed"] is False

    blocked_package_without_preview = client.post(f"/videos/{video_id}/package")
    assert blocked_package_without_preview.status_code == 409
    assert "preview" in blocked_package_without_preview.json()["detail"].lower()

    blocked_payload_without_preview_review = client.post(f"/publish/{video_id}/prepare-youtube-payload")
    assert blocked_payload_without_preview_review.status_code == 409
    assert "preview" in blocked_payload_without_preview_review.json()["detail"].lower()

    _write_real_preview_file(video_id)

    preview_status = client.get(f"/videos/{video_id}/preview/status")
    assert preview_status.status_code == 200
    assert preview_status.json()["preview_exists"] is True
    assert preview_status.json()["preview_reviewed"] is False

    mark_preview_reviewed = client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True})
    assert mark_preview_reviewed.status_code == 200
    assert mark_preview_reviewed.json()["preview_reviewed"] is True

    package_response = client.post(f"/videos/{video_id}/package")
    assert package_response.status_code == 200
    assert "package_dir" in package_response.json()

    payload_response = client.post(f"/publish/{video_id}/prepare-youtube-payload")
    assert payload_response.status_code == 200
    assert payload_response.json()["privacy_status"] == "private"


def test_long_title_validation_returns_422() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Length Guard Channel"}).json()["id"]
    title = "A" * 201
    response = client.post(
        "/videos",
        json={
            "channel_id": channel_id,
            "title": title,
        },
    )
    assert response.status_code == 422


def test_script_payload_in_title_returns_422() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Script Guard Channel"}).json()["id"]
    response = client.post(
        "/videos",
        json={
            "channel_id": channel_id,
            "title": "<script>alert('xss')</script>",
        },
    )
    assert response.status_code == 422


def test_ai_generation_failure_returns_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Generation Failure Channel"}).json()["id"]
    video_id = client.post("/videos", json={"channel_id": channel_id, "title": "Failing Generation"}).json()["id"]

    def fake_build_all_assets(_video):  # noqa: ANN001
        raise RuntimeError("simulated model failure")

    monkeypatch.setattr(videos_router, "build_all_assets", fake_build_all_assets)
    response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert response.status_code == 502
    assert "failed" in response.json()["detail"].lower()


def test_videos_list_respects_limit_and_offset() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Videos Pagination Channel"}).json()["id"]
    for idx in range(5):
        response = client.post("/videos", json={"channel_id": channel_id, "title": f"Video {idx}"})
        assert response.status_code == 200

    limited = client.get("/videos?limit=2")
    assert limited.status_code == 200
    assert len(limited.json()) == 2

    offset = client.get("/videos?limit=2&offset=2")
    assert offset.status_code == 200
    assert len(offset.json()) == 2


def test_videos_list_over_limit_rejected() -> None:
    client = TestClient(app)
    response = client.get("/videos?limit=101")
    assert response.status_code == 422


def test_ai_route_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "rate_limit_ai_per_minute", 2)
    monkeypatch.setattr(main_module, "rate_limiter", InMemoryRateLimiter())

    channel_id = client.post("/channels", json={"name": "Rate Limit Channel"}).json()["id"]
    assert channel_id > 0

    payload = {
        "niche_lane": "AI tool breakdowns",
        "query": "best ai tools",
        "max_results": 5,
    }
    first = client.post("/research/youtube/run", json=payload)
    second = client.post("/research/youtube/run", json=payload)
    third = client.post("/research/youtube/run", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429


def test_pipeline_and_batch() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Test Channel 2"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    batch_response = client.post(
        "/videos/batch",
        json={
            "channel_id": channel_id,
            "videos": [
                {"title": "Video 1", "thumbnail_text": "Thumb 1", "pillar": "pillar1", "target_view": "tv1"},
                {"title": "Video 2", "thumbnail_text": "Thumb 2", "pillar": "pillar2", "target_view": "tv2"},
            ],
        },
    )
    assert batch_response.status_code == 200
    videos = batch_response.json()
    assert len(videos) == 2

    invalid_batch = client.post(
        "/videos/batch",
        json={"channel_id": channel_id, "videos": [{"title": "Valid"}, {"title": "   "}]},
    )
    assert invalid_batch.status_code == 400

    summary_response = client.get("/pipeline/summary")
    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["status_counts"]["idea"] >= 2

    filter_response = client.get("/videos?search=Video 1")
    assert filter_response.status_code == 200
    filtered_videos = filter_response.json()
    assert len(filtered_videos) == 1
    assert filtered_videos[0]["title"] == "Video 1"

    filter_status_response = client.get("/videos?status=idea")
    assert filter_status_response.status_code == 200
    assert len(filter_status_response.json()) >= 2


def test_preview_route_and_review_state() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Preview Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post("/videos", json={"channel_id": channel_id, "title": "Preview Demo"})
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    missing_preview = client.get(f"/videos/{video_id}/preview")
    assert missing_preview.status_code == 404
    missing_preview_review = client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True})
    assert missing_preview_review.status_code == 404

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200
    review_response = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"})
    assert review_response.status_code == 200

    _write_real_preview_file(video_id)

    served_preview = client.get(f"/videos/{video_id}/preview")
    assert served_preview.status_code == 200
    assert served_preview.headers["content-type"].startswith("video/mp4")

    preview_review = client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True})
    assert preview_review.status_code == 200
    assert preview_review.json()["preview_reviewed"] is True

    video_after_review = client.get(f"/videos/{video_id}").json()
    assert video_after_review["preview_reviewed"] is True


def test_audit_trail_and_operator_export_are_safe() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Audit Test Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Audit Trail Demo"},
    )
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    audit_response = client.get("/audit")
    assert audit_response.status_code == 200
    assert any(event["event_type"] == "video_created" and event["video_id"] == video_id for event in audit_response.json())

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200

    video_audit_response = client.get(f"/videos/{video_id}/audit")
    assert video_audit_response.status_code == 200
    assert any(event["event_type"] == "assets_generated" for event in video_audit_response.json())

    compliance_response = client.post(f"/videos/{video_id}/compliance/run")
    assert compliance_response.status_code == 200

    video_audit_response = client.get(f"/videos/{video_id}/audit")
    event_types = [event["event_type"] for event in video_audit_response.json()]
    assert "compliance_run" in event_types

    missing_audit_response = client.get("/videos/999999/audit")
    assert missing_audit_response.status_code == 404

    long_body = "safe local operator script " * 60
    patch_response = client.patch(f"/videos/{video_id}/assets/script", json={"body": long_body})
    assert patch_response.status_code == 200

    export_response = client.get(f"/videos/{video_id}/operator-export")
    assert export_response.status_code == 200
    export_payload = export_response.json()
    assert export_payload["video"]["id"] == video_id
    assert "assets" in export_payload
    assert all("body" not in asset for asset in export_payload["assets"])
    assert any(asset["asset_type"] == "script" and asset["body_length"] == len(long_body) for asset in export_payload["assets"])
    assert long_body not in export_response.text
    assert "audit_events" in export_payload
    assert "youtube_payload_readiness" in export_payload


def test_video_performance_write_requires_internal_api_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")

    channel_id = client.post(
        "/channels",
        json={"name": "Performance Key Channel"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()["id"]
    video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Performance Key Video"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()

    denied = client.post(f"/videos/{video['id']}/performance", json={"views": 10})
    assert denied.status_code == 401

    allowed = client.post(
        f"/videos/{video['id']}/performance",
        json={"impressions": 100, "views": 20, "clicks": 5},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert allowed.status_code == 200


def test_video_performance_save_and_get_computes_ctr_without_workflow_side_effects() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Performance Save Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Performance Save Video"}).json()
    before_video = client.get(f"/videos/{video['id']}").json()

    save = client.post(
        f"/videos/{video['id']}/performance",
        json={
            "platform": "youtube",
            "impressions": 1000,
            "views": 220,
            "clicks": 95,
            "average_view_duration_seconds": 78.2,
            "average_percentage_viewed": 49.0,
            "watch_time_minutes": 310.5,
            "likes": 31,
            "comments": 12,
            "subscribers_gained": 8,
            "published_url": "https://youtube.com/watch?v=localdemo",
            "notes": "manual entry",
        },
    )
    assert save.status_code == 200
    payload = save.json()
    assert payload["video_id"] == video["id"]
    assert payload["ctr"] == 9.5
    assert payload["has_data"] is True
    assert payload["performance_band"] in {"average", "strong"}
    assert payload["is_manual_local"] is True

    fetched = client.get(f"/videos/{video['id']}/performance")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["ctr"] == 9.5
    assert body["ctr_band"] == "strong"
    assert body["retention_band"] in {"average", "strong"}
    assert body["next_recommendation"]

    after_video = client.get(f"/videos/{video['id']}").json()
    assert after_video["approved"] == before_video["approved"]
    assert after_video["preview_reviewed"] == before_video["preview_reviewed"]


def test_video_performance_get_without_data_returns_needs_data() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Performance Empty Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Performance Empty Video"}).json()

    response = client.get(f"/videos/{video['id']}/performance")
    assert response.status_code == 200
    body = response.json()
    assert body["video_id"] == video["id"]
    assert body["has_data"] is False
    assert body["performance_band"] == "needs_data"
    assert body["manual_local_note"]


def test_performance_summary_returns_manual_local_rows_and_respects_limit() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Performance Summary Channel"}).json()["id"]

    for index in range(3):
        video = client.post("/videos", json={"channel_id": channel_id, "title": f"Performance Summary Video {index}"}).json()
        assert client.post(
            f"/videos/{video['id']}/performance",
            json={"impressions": 100 + index * 10, "views": 20 + index * 5, "clicks": 4 + index},
        ).status_code == 200

    response = client.get("/performance/summary?limit=2")
    assert response.status_code == 200
    payload = response.json()
    assert payload["manual_local_note"]
    assert payload["total_videos_with_manual_metrics"] >= 3
    assert len(payload["top_videos"]) <= 2
    assert len(payload["bottom_videos"]) <= 2


def test_operator_export_generates_local_file_with_blockers_and_visual_statuses() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Operator Export Local Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Operator Export Local Video"}).json()
    video_id = video["id"]

    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    plan = client.post(f"/visual-assets/from-video/{video_id}").json()
    queue = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"}).json()
    jobs = queue["jobs"][:3]
    path_pending = _write_real_visual_asset_file("operator_export_pending.png")
    path_approved = _write_real_visual_asset_file("operator_export_approved.png")
    path_rejected = _write_real_visual_asset_file("operator_export_rejected.png")

    pending_asset = client.post(
        f"/visual-generation/jobs/{jobs[0]['id']}/register-output",
        json={"output_path": str(path_pending)},
    ).json()
    approved_asset = client.post(
        f"/visual-generation/jobs/{jobs[1]['id']}/register-output",
        json={"output_path": str(path_approved)},
    ).json()
    rejected_asset = client.post(
        f"/visual-generation/jobs/{jobs[2]['id']}/register-output",
        json={"output_path": str(path_rejected)},
    ).json()
    assert client.post(f"/visual-generation/assets/{approved_asset['id']}/approve").status_code == 200
    assert client.post(
        f"/visual-generation/assets/{rejected_asset['id']}/reject",
        json={"review_notes": "Need different composition."},
    ).status_code == 200

    fake_asset_path = (Path(os.environ["OUTPUT_DIR"]).resolve() / "generated" / "operator_export_missing_fake.png").resolve()
    db = SessionLocal()
    try:
        first_scene = db.scalar(
            select(VisualScene)
            .where(VisualScene.plan_id == plan["id"])
            .order_by(VisualScene.scene_number.asc())
            .limit(1)
        )
        assert first_scene is not None
        db.add(
            VisualGeneratedAsset(
                visual_asset_plan_id=plan["id"],
                visual_scene_id=first_scene.id,
                generation_job_id=None,
                asset_type="image",
                file_path=str(fake_asset_path),
                file_exists=True,
            )
        )
        db.commit()
    finally:
        db.close()

    before_video = client.get(f"/videos/{video_id}").json()
    export_response = client.get(f"/videos/{video_id}/operator-export")
    assert export_response.status_code == 200
    payload = export_response.json()

    assert payload["ready_for_manual_upload"] is False
    assert payload["blockers"]
    assert payload["video"]["preview_reviewed"] is False
    assert payload["video"]["approved"] is False
    if payload["thumbnail_image_path"] is None:
        assert payload["thumbnail_review_status"] is None
        assert payload["thumbnail_warning"] == "Thumbnail image is not generated yet."
    else:
        assert payload["thumbnail_review_status"] in {"pending", "approved", "rejected"}
        if payload["thumbnail_review_status"] != "approved":
            assert "manual approval" in (payload["thumbnail_warning"] or "").lower()
    assert payload["export_path"]
    export_path = Path(payload["export_path"])
    assert export_path.exists()
    assert export_path.name == "operator_export.json"
    assert str(export_path).startswith(str(Path(os.environ["OUTPUT_DIR"]).resolve()))

    visual_rows = payload["visual_assets"]
    status_set = {row["review_status"] for row in visual_rows}
    assert "pending" in status_set
    assert "approved" in status_set
    assert "rejected" in status_set
    returned_paths = {row["file_path"] for row in visual_rows}
    assert str(path_pending) in returned_paths
    assert str(path_approved) in returned_paths
    assert str(path_rejected) in returned_paths
    assert str(fake_asset_path) not in returned_paths
    assert all(Path(path).exists() for path in returned_paths)

    written = json.loads(export_path.read_text(encoding="utf-8"))
    assert written["video"]["id"] == video_id
    assert written["ready_for_manual_upload"] is False
    assert written["export_path"] == str(export_path)

    after_video = client.get(f"/videos/{video_id}").json()
    assert after_video["preview_reviewed"] == before_video["preview_reviewed"]
    assert after_video["approved"] == before_video["approved"]
    assert pending_asset["id"] in {row["asset_id"] for row in visual_rows}


def test_operator_export_includes_manual_local_performance_when_present() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Export Performance Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Export Performance Video"}).json()
    video_id = video["id"]

    assert client.post(
        f"/videos/{video_id}/performance",
        json={"impressions": 500, "views": 120, "clicks": 21, "average_percentage_viewed": 33.0, "watch_time_minutes": 190.0},
    ).status_code == 200

    export_response = client.get(f"/videos/{video_id}/operator-export")
    assert export_response.status_code == 200
    payload = export_response.json()
    assert "performance" in payload
    assert payload["performance"]["video_id"] == video_id
    assert payload["performance"]["has_data"] is True
    assert payload["performance"]["ctr"] == 4.2
    assert payload["performance"]["performance_band"] in {"weak", "average", "strong"}
    assert "manual/local" in payload["performance"]["manual_local_note"].lower()


def test_operator_export_includes_generated_thumbnail_path_and_review_status() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Operator Export Thumbnail Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Operator Export Thumbnail Video"}).json()
    video_id = video["id"]

    thumbnail = client.post(f"/videos/{video_id}/thumbnail/generate")
    assert thumbnail.status_code == 200
    thumbnail_body = thumbnail.json()
    assert thumbnail_body["review_status"] == "pending"

    export_response = client.get(f"/videos/{video_id}/operator-export")
    assert export_response.status_code == 200
    payload = export_response.json()
    assert payload["thumbnail_image_path"] == thumbnail_body["thumbnail_path"]
    assert payload["thumbnail_review_status"] == "pending"
    assert "manual approval" in (payload["thumbnail_warning"] or "").lower()

    export_path = Path(payload["export_path"])
    written = json.loads(export_path.read_text(encoding="utf-8"))
    assert written["thumbnail_image_path"] == thumbnail_body["thumbnail_path"]
    assert written["thumbnail_review_status"] == "pending"


def test_publishing_payload_generate_requires_internal_api_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")

    channel_id = client.post(
        "/channels",
        json={"name": "Payload Key Channel"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()["id"]
    video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Payload Key Video"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()

    denied = client.post(f"/videos/{video['id']}/publishing-payload/generate")
    assert denied.status_code == 401

    allowed = client.post(
        f"/videos/{video['id']}/publishing-payload/generate",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert allowed.status_code == 200


def test_publishing_payload_generate_blocked_when_gates_incomplete_and_has_no_side_effects() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Payload Blocked Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Payload Blocked Video"}).json()
    before = client.get(f"/videos/{video['id']}").json()

    response = client.post(f"/videos/{video['id']}/publishing-payload/generate")
    assert response.status_code == 200
    body = response.json()
    assert body["payload_status"] == "blocked"
    assert body["ready_for_manual_upload"] is False
    assert body["blockers"]
    assert body["manual_upload_checklist"]
    payload_path = Path(body["payload_path"])
    assert payload_path.exists()
    assert payload_path.name == "publishing_payload.json"
    assert str(payload_path).startswith(str(Path(os.environ["OUTPUT_DIR"]).resolve()))

    after = client.get(f"/videos/{video['id']}").json()
    assert after["approved"] == before["approved"]
    assert after["preview_reviewed"] == before["preview_reviewed"]
    assert after["status"] == before["status"]
    assert client.post(f"/publish/{video['id']}/prepare-youtube-payload").status_code == 409


def test_publishing_payload_generate_ready_when_gates_satisfied_and_includes_paths() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Payload Ready Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Payload Ready Video"}).json()
    video_id = video["id"]

    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{video_id}").status_code == 200
    _write_real_preview_file(video_id)
    assert client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{video_id}/package").status_code == 200
    assert client.post(f"/publish/{video_id}/prepare-youtube-payload").status_code == 200
    thumbnail = client.post(f"/videos/{video_id}/thumbnail/generate")
    assert thumbnail.status_code == 200
    thumbnail_path = thumbnail.json()["thumbnail_path"]
    thumbnail_asset_id = thumbnail.json()["visual_asset_id"]
    assert client.post(f"/visual-generation/assets/{thumbnail_asset_id}/approve").status_code == 200

    # Phase 30: production gate requires final export + production voice + approved visuals + clean metadata
    _write_final_voiceover_production(video_id)
    _write_clean_description_asset(video_id)
    final_export_path = _write_final_export_file(video_id)

    export = client.get(f"/videos/{video_id}/operator-export")
    assert export.status_code == 200
    export_path = export.json()["export_path"]

    before = client.get(f"/videos/{video_id}").json()
    response = client.post(f"/videos/{video_id}/publishing-payload/generate")
    assert response.status_code == 200
    body = response.json()
    assert body["ready_for_manual_upload"] is True
    assert body["payload_status"] in {"ready", "regenerated"}
    assert body["video_file_path"] is not None
    assert "final_exports" in body["video_file_path"]
    assert body["thumbnail_image_path"] == thumbnail_path
    assert body["export_path"] == export_path
    assert body["manual_upload_checklist"]
    assert body["next_required_action"]
    written = json.loads(Path(body["payload_path"]).read_text(encoding="utf-8"))
    assert written["ready_for_manual_upload"] is True
    assert written["youtube_payload"]["title"] == before["title"][:100]

    after = client.get(f"/videos/{video_id}").json()
    assert after["approved"] == before["approved"]
    assert after["preview_reviewed"] == before["preview_reviewed"]
    assert after["status"] == before["status"]


def test_publishing_payload_read_and_list_filters() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Payload List Channel"}).json()["id"]

    blocked_video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Payload Blocked Queue Video", "content_type": "long"},
    ).json()
    ready_video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Payload Ready Queue Video", "content_type": "short"},
    ).json()

    blocked_generate = client.post(f"/videos/{blocked_video['id']}/publishing-payload/generate")
    assert blocked_generate.status_code == 200

    assert client.post(f"/videos/{ready_video['id']}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{ready_video['id']}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{ready_video['id']}").status_code == 200
    _write_real_preview_file(ready_video["id"])
    assert client.post(f"/videos/{ready_video['id']}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{ready_video['id']}/package").status_code == 200
    assert client.post(f"/publish/{ready_video['id']}/prepare-youtube-payload").status_code == 200
    # Phase 30: production gate requires thumbnail approved + production voice + clean metadata + final export
    thumb = client.post(f"/videos/{ready_video['id']}/thumbnail/generate")
    assert thumb.status_code == 200
    assert client.post(f"/visual-generation/assets/{thumb.json()['visual_asset_id']}/approve").status_code == 200
    _write_final_voiceover_production(ready_video["id"])
    _write_clean_description_asset(ready_video["id"])
    _write_final_export_file(ready_video["id"])
    ready_generate = client.post(f"/videos/{ready_video['id']}/publishing-payload/generate")
    assert ready_generate.status_code == 200

    read_response = client.get(f"/videos/{ready_video['id']}/publishing-payload")
    assert read_response.status_code == 200
    read_body = read_response.json()
    assert read_body["video_id"] == ready_video["id"]
    assert read_body["ready_for_manual_upload"] is True

    missing = client.get("/videos/999999/publishing-payload")
    assert missing.status_code == 404

    blocked_only = client.get("/publishing-payloads?status=blocked&limit=50")
    assert blocked_only.status_code == 200
    blocked_rows = blocked_only.json()
    assert blocked_rows
    assert all(row["payload_status"] == "blocked" for row in blocked_rows)

    ready_only = client.get("/publishing-payloads?ready_for_manual_upload=true&limit=50")
    assert ready_only.status_code == 200
    ready_rows = ready_only.json()
    assert ready_rows
    assert all(row["ready_for_manual_upload"] is True for row in ready_rows)

    short_only = client.get("/publishing-payloads?content_type=short&limit=50")
    assert short_only.status_code == 200
    short_rows = short_only.json()
    assert short_rows
    assert all(row["content_type"] == "short" for row in short_rows)

    limit_over = client.get("/publishing-payloads?limit=201")
    assert limit_over.status_code == 422


def test_operator_export_includes_latest_publishing_payload_metadata_when_present() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Export Payload Metadata Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Export Payload Metadata Video"}).json()
    video_id = video["id"]

    base_export = client.get(f"/videos/{video_id}/operator-export")
    assert base_export.status_code == 200
    base_body = base_export.json()
    assert base_body["publishing_payload_path"] is None
    assert base_body["publishing_payload_status"] is None
    assert base_body["publishing_payload_manual_upload_checklist"] == []

    assert client.post(f"/videos/{video_id}/publishing-payload/generate").status_code == 200
    with_payload = client.get(f"/videos/{video_id}/operator-export")
    assert with_payload.status_code == 200
    payload = with_payload.json()
    assert payload["publishing_payload_path"]
    assert payload["publishing_payload_status"] in {"blocked", "ready", "regenerated", "draft"}
    assert isinstance(payload["publishing_payload_ready_for_manual_upload"], bool)
    assert isinstance(payload["publishing_payload_blockers"], list)
    assert isinstance(payload["publishing_payload_warnings"], list)
    assert payload["publishing_payload_manual_upload_checklist"]

def test_opportunity_routes_and_promotion_workflow() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Opportunity Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    create_response = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Best AI tools for small business owners",
            "niche_lane": "Local business AI operations",
            "audience": "Small business owners",
            "monetization_path": "Affiliate + consulting audit",
            "notes": "Educational operator workflow only",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["topic"] == "Best AI tools for small business owners"
    assert created["score"]["total_score"] >= 8
    assert created["assigned_agent"]
    assert created["assigned_agent_id"] is not None

    list_response = client.get("/opportunities")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]

    top_response = client.get("/opportunities/top")
    assert top_response.status_code == 200
    top_items = top_response.json()
    assert len(top_items) == 1
    assert top_items[0]["id"] == created["id"]

    score_response = client.post(f"/opportunities/{created['id']}/score")
    assert score_response.status_code == 200
    rescored = score_response.json()
    assert rescored["score"]["total_score"] == created["score"]["total_score"]

    blocked_promote_response = client.post(f"/opportunities/{created['id']}/promote-to-video")
    assert blocked_promote_response.status_code == 400
    assert blocked_promote_response.json()["detail"] == "Opportunity must be approved for video before promotion."

    review_response = client.patch(
        f"/opportunities/{created['id']}/review",
        json={
            "review_status": "approved_for_video",
            "operator_notes": "Strong fit for local business audience.",
            "decision_summary": "Approve for production queue.",
        },
    )
    assert review_response.status_code == 200
    reviewed = review_response.json()
    assert reviewed["review_status"] == "approved_for_video"
    assert reviewed["reviewed_at"] is not None

    promote_response = client.post(f"/opportunities/{created['id']}/promote-to-video")
    assert promote_response.status_code == 200
    promoted_video = promote_response.json()
    assert promoted_video["title"] == created["recommended_title"]
    assert promoted_video["approved"] is False
    assert promoted_video["status"] == "idea"
    assert promoted_video["preview_reviewed"] is False
    assert promoted_video["assigned_agent_id"] == created["assigned_agent_id"]

    updated_opp = client.get("/opportunities").json()[0]
    assert updated_opp["promoted_video_id"] == promoted_video["id"]

    audit_response = client.get("/audit?limit=100")
    assert audit_response.status_code == 200
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "opportunity_created" in event_types
    assert "opportunity_scored" in event_types
    assert "opportunity_review_updated" in event_types
    assert "opportunity_promoted" in event_types


def test_opportunity_scoring_analytics_feedback_neutral_without_metrics() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Opportunity Neutral Analytics Channel"}).json()["id"]
    response = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI phone automation for local service follow-up",
            "niche_lane": "Local Automation Pillar",
            "audience": "service operators",
            "monetization_path": "consulting",
        },
    )
    assert response.status_code == 200
    score = response.json()["score"]
    assert score["analytics_signal"] == "neutral"
    assert score["analytics_confidence_adjustment"] == 0
    assert score["analytics_sample_size"] == 0
    assert score["analytics_adjusted_total_score"] == score["base_total_score"]


def test_opportunity_scoring_analytics_feedback_positive_for_strong_local_metrics() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Opportunity Positive Analytics Channel"}).json()["id"]
    pillar = "Local Automation Pillar"
    video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Strong Performance Video", "pillar": pillar},
    ).json()
    assert client.post(
        f"/videos/{video['id']}/performance",
        json={
            "impressions": 1200,
            "views": 420,
            "clicks": 96,
            "average_percentage_viewed": 51.0,
            "average_view_duration_seconds": 82.0,
            "watch_time_minutes": 420.0,
        },
    ).status_code == 200

    response = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Local automation scripts for dispatch operators",
            "niche_lane": pillar,
            "audience": "dispatch owners",
            "monetization_path": "service + audit",
        },
    )
    assert response.status_code == 200
    score = response.json()["score"]
    assert score["analytics_signal"] == "positive"
    assert 1 <= score["analytics_confidence_adjustment"] <= 3
    assert score["analytics_adjusted_total_score"] >= score["base_total_score"]
    assert score["analytics_sample_size"] >= 1
    assert "will perform" not in (score["analytics_reason"] or "").lower()


def test_opportunity_scoring_analytics_feedback_negative_for_weak_local_metrics_and_bounded() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Opportunity Negative Analytics Channel"}).json()["id"]
    pillar = "Weak Analytics Pillar"
    video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Weak Performance Video", "pillar": pillar},
    ).json()
    assert client.post(
        f"/videos/{video['id']}/performance",
        json={
            "impressions": 900,
            "views": 55,
            "clicks": 5,
            "average_percentage_viewed": 12.0,
            "average_view_duration_seconds": 15.0,
            "watch_time_minutes": 35.0,
        },
    ).status_code == 200

    response = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Weak pipeline angle to test",
            "niche_lane": pillar,
            "audience": "operators",
            "monetization_path": "affiliate",
        },
    )
    assert response.status_code == 200
    score = response.json()["score"]
    assert score["analytics_signal"] == "negative"
    assert -3 <= score["analytics_confidence_adjustment"] <= -1
    assert score["analytics_adjusted_total_score"] <= score["base_total_score"]
    assert "guarantee" not in (score["analytics_reason"] or "").lower()


def test_daily_seed_creates_opportunities_and_scores() -> None:
    client = TestClient(app)
    client.post("/channels", json={"name": "Daily Seed Channel"})

    response = client.post("/opportunities/intake/daily-seed", json={"limit": 7})
    assert response.status_code == 200
    payload = response.json()
    assert payload["created_count"] == 7
    assert payload["skipped_duplicates"] == 0
    assert len(payload["created_ids"]) == 7

    opportunities = client.get("/opportunities").json()
    assert len(opportunities) == 7
    for row in opportunities:
        assert row["recommended_title"]
        assert row["thumbnail_angle"]
        assert row["recommended_cta"]
        assert row["review_status"] == "unreviewed"
        assert row["promoted_video_id"] is None


def test_daily_seed_is_idempotent_same_day() -> None:
    client = TestClient(app)
    client.post("/channels", json={"name": "Daily Seed Idempotent Channel"})

    first = client.post("/opportunities/intake/daily-seed", json={"limit": 7})
    second = client.post("/opportunities/intake/daily-seed", json={"limit": 7})
    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["created_count"] == 7
    assert second_body["created_count"] == 0
    assert second_body["skipped_duplicates"] == 7
    assert len(client.get("/opportunities").json()) == 7


def test_daily_seed_assigns_active_agents_by_lane() -> None:
    client = TestClient(app)
    channel_response = client.post("/channels", json={"name": "Daily Seed Agent Match Channel"})
    channel_id = channel_response.json()["id"]
    assert channel_response.status_code == 200

    agents = client.get("/agents").json()
    business_agent = next(
        agent
        for agent in agents
        if agent["channel_id"] == channel_id and agent["name"] == "AI Business Automation Agent"
    )
    assert business_agent["is_active"] is True

    response = client.post("/opportunities/intake/daily-seed", json={"limit": 7, "channel_id": channel_id})
    assert response.status_code == 200
    opportunities = client.get("/opportunities").json()
    row = next(item for item in opportunities if item["niche_lane"] == "AI business automation")
    assert row["assigned_agent_id"] == business_agent["id"]
    assert row["assigned_agent"] == "AI Business Automation Agent"


def test_daily_seed_audit_events_only_for_inserted_opportunities() -> None:
    client = TestClient(app)
    client.post("/channels", json={"name": "Daily Seed Audit Channel"})

    first = client.post("/opportunities/intake/daily-seed", json={"limit": 7}).json()
    second = client.post("/opportunities/intake/daily-seed", json={"limit": 7}).json()
    assert first["created_count"] == 7
    assert second["created_count"] == 0

    audit = client.get("/audit?limit=200")
    assert audit.status_code == 200
    events = [event for event in audit.json() if event["event_type"] == "opportunity_daily_seed_created"]
    assert len(events) == 7


def test_daily_seed_does_not_auto_approve_or_promote() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Daily Seed No Auto Promote Channel"}).json()["id"]

    seed = client.post("/opportunities/intake/daily-seed", json={"limit": 7, "channel_id": channel_id})
    assert seed.status_code == 200
    rows = client.get("/opportunities").json()
    assert len(rows) == 7
    assert all(row["review_status"] == "unreviewed" for row in rows)
    assert all(row["promoted_video_id"] is None for row in rows)
    videos = client.get("/videos").json()
    assert len(videos) == 0


def test_command_center_includes_seeded_opportunities_candidates() -> None:
    client = TestClient(app)
    client.post("/channels", json={"name": "Daily Seed Command Center Channel"})
    seed = client.post("/opportunities/intake/daily-seed", json={"limit": 7})
    assert seed.status_code == 200

    command_center = client.get("/command-center/today")
    assert command_center.status_code == 200
    payload = command_center.json()
    assert payload["best_opportunity"] is not None
    assert payload["best_opportunity"]["id"] in seed.json()["created_ids"]


def test_create_production_brief_from_opportunity() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Brief Create Channel"}).json()["id"]
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Faceless YouTube automation workflow",
            "niche_lane": "faceless YouTube / creator automation",
            "audience": "creator operators",
            "monetization_path": "templates + affiliate software",
        },
    ).json()
    assert client.patch(f"/opportunities/{opp['id']}/review", json={"review_status": "shortlisted"}).status_code == 200

    create_response = client.post(f"/production-briefs/from-opportunity/{opp['id']}")
    assert create_response.status_code == 200
    payload = create_response.json()
    assert payload["brief"]["opportunity_id"] == opp["id"]
    assert payload["brief"]["status"] == "draft"
    assert payload["brief"]["title"]
    assert payload["brief"]["hook"]
    assert payload["brief"]["outline"]
    assert payload["brief"]["script_plan"]

    list_response = client.get("/production-briefs")
    assert list_response.status_code == 200
    assert any(item["id"] == payload["brief"]["id"] for item in list_response.json())

    read_response = client.get(f"/production-briefs/{payload['brief']['id']}")
    assert read_response.status_code == 200
    assert read_response.json()["id"] == payload["brief"]["id"]


def test_brief_uses_assigned_agent_profile_when_present() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Brief Agent Profile Channel"}).json()["id"]
    agents = client.get("/agents").json()
    target_agent = next(
        agent for agent in agents if agent["channel_id"] == channel_id and agent["name"] == "Local Business AI Agent"
    )
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "How to automate customer calls with AI",
            "niche_lane": "local business AI automation",
            "audience": "local operators",
            "monetization_path": "SkybridgeCX leads + audits",
        },
    ).json()
    assert client.patch(f"/opportunities/{opp['id']}/review", json={"review_status": "approved_for_video"}).status_code == 200
    brief_payload = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]
    assert brief_payload["assigned_agent_id"] == target_agent["id"]
    assert "Local Business AI Agent" in (brief_payload["operator_review_notes"] or "")


def test_brief_generation_does_not_auto_approve_or_promote() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Brief No Auto Channel"}).json()["id"]
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI business automation SOP for missed calls",
            "niche_lane": "AI business automation",
            "audience": "small business owners",
            "monetization_path": "consulting audits",
        },
    ).json()
    assert client.patch(f"/opportunities/{opp['id']}/review", json={"review_status": "shortlisted"}).status_code == 200
    brief = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]
    assert brief["status"] == "draft"
    assert brief["promoted_video_id"] is None
    assert len(client.get("/videos").json()) == 0


def test_brief_review_update_and_promote_rules() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Brief Review Promote Channel"}).json()["id"]
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI ecommerce product research workflow",
            "niche_lane": "ecommerce AI",
            "audience": "ecommerce founders",
            "monetization_path": "affiliate tools + templates",
        },
    ).json()
    assert client.patch(f"/opportunities/{opp['id']}/review", json={"review_status": "approved_for_video"}).status_code == 200
    brief = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]
    brief_id = brief["id"]

    blocked_promote = client.post(f"/production-briefs/{brief_id}/promote-to-video")
    assert blocked_promote.status_code == 400

    review_update = client.patch(
        f"/production-briefs/{brief_id}/review",
        json={"status": "needs_revision", "operator_review_notes": "Tighten hook and reduce assumptions."},
    )
    assert review_update.status_code == 200
    assert review_update.json()["status"] == "needs_revision"
    assert "Tighten hook" in (review_update.json()["operator_review_notes"] or "")

    assert client.patch(
        f"/production-briefs/{brief_id}/review",
        json={"status": "approved", "operator_review_notes": "Approved for promotion."},
    ).status_code == 200
    promoted_video = client.post(f"/production-briefs/{brief_id}/promote-to-video")
    assert promoted_video.status_code == 200
    video_body = promoted_video.json()
    assert video_body["status"] == "idea"
    assert video_body["approved"] is False
    assert video_body["preview_reviewed"] is False

    updated_brief = client.get(f"/production-briefs/{brief_id}").json()
    assert updated_brief["status"] == "promoted"
    assert updated_brief["promoted_video_id"] == video_body["id"]


def test_brief_audit_events_written_for_create_review_promote() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Brief Audit Channel"}).json()["id"]
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Career productivity AI workflow for weekly execution",
            "niche_lane": "career/productivity AI",
            "audience": "knowledge workers",
            "monetization_path": "templates + affiliates",
        },
    ).json()
    assert client.patch(f"/opportunities/{opp['id']}/review", json={"review_status": "approved_for_video"}).status_code == 200
    brief = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]
    brief_id = brief["id"]
    assert client.patch(f"/production-briefs/{brief_id}/review", json={"status": "approved"}).status_code == 200
    assert client.post(f"/production-briefs/{brief_id}/promote-to-video").status_code == 200

    events = client.get("/audit?limit=200").json()
    event_types = [item["event_type"] for item in events]
    assert "production_brief_created" in event_types
    assert "production_brief_review_updated" in event_types
    assert "production_brief_promoted_to_video" in event_types


def test_create_visual_plan_from_brief() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Brief Channel"}).json()["id"]
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Faceless YouTube visual workflow",
            "niche_lane": "faceless YouTube / creator automation",
            "audience": "creator operators",
            "monetization_path": "templates + affiliates",
        },
    ).json()
    assert client.patch(f"/opportunities/{opp['id']}/review", json={"review_status": "shortlisted"}).status_code == 200
    brief = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]

    response = client.post(f"/visual-assets/from-brief/{brief['id']}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["brief_id"] == brief["id"]
    assert payload["thumbnail_prompt"]
    assert 6 <= len(payload["scenes"]) <= 10
    first_scene = payload["scenes"][0]
    assert first_scene["image_prompt"]
    assert first_scene["animation_prompt"]


def test_create_visual_plan_from_video_does_not_approve_or_generate_assets() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Video Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Plan Video"}).json()

    response = client.post(f"/visual-assets/from-video/{video['id']}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["video_id"] == video["id"]
    assert payload["thumbnail_prompt"]
    assert 6 <= len(payload["scenes"]) <= 10

    refreshed_video = client.get(f"/videos/{video['id']}").json()
    assert refreshed_video["approved"] is False
    assert refreshed_video["status"] == "idea"

    assets = client.get(f"/videos/{video['id']}/assets").json()
    assert assets == []
    readiness = client.get(f"/videos/{video['id']}/readiness").json()
    assert readiness["assets_generated"] is False


def test_visual_plan_mark_ready_changes_only_plan_status() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Ready Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Ready Video"}).json()
    plan = client.post(f"/visual-assets/from-video/{video['id']}").json()

    mark_ready = client.post(f"/visual-assets/plans/{plan['id']}/mark-ready")
    assert mark_ready.status_code == 200
    ready_plan = mark_ready.json()
    assert ready_plan["status"] == "ready_for_generation"
    assert ready_plan["ready_marked_at"] is not None

    refreshed_video = client.get(f"/videos/{video['id']}").json()
    assert refreshed_video["approved"] is False
    assert refreshed_video["status"] == "idea"
    assert client.get(f"/videos/{video['id']}/assets").json() == []


def test_visual_asset_write_routes_require_internal_api_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")

    channel_id = client.post(
        "/channels",
        json={"name": "Visual Key Channel"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()["id"]
    video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Visual Key Video"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()

    denied = client.post(f"/visual-assets/from-video/{video['id']}")
    assert denied.status_code == 401

    allowed = client.post(
        f"/visual-assets/from-video/{video['id']}",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert allowed.status_code == 200


def test_visual_generation_queue_from_plan_and_no_duplicate_active_jobs() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Queue Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Queue Video"}).json()
    plan = client.post(f"/visual-assets/from-video/{video['id']}").json()

    first = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["created_jobs"] > 0
    assert first_body["skipped_jobs"] == 0

    jobs = client.get(f"/visual-generation/jobs?plan_id={plan['id']}").json()
    assert len(jobs) == first_body["created_jobs"]
    assert any(job["job_type"] == "thumbnail" for job in jobs)
    assert all(job["status"] == "queued" for job in jobs)

    second = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"})
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["created_jobs"] == 0
    assert second_body["skipped_jobs"] >= first_body["created_jobs"]
    jobs_after = client.get(f"/visual-generation/jobs?plan_id={plan['id']}").json()
    assert len(jobs_after) == len(jobs)


def test_visual_generation_export_payload_marks_exported_without_external_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Export Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Export Video"}).json()
    plan = client.post(f"/visual-assets/from-video/{video['id']}").json()
    queue = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"}).json()
    job_id = queue["jobs"][0]["id"]

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("No external network call should be made when exporting payload")

    monkeypatch.setattr(videos_router.urllib.request, "urlopen", fail_if_called)

    exported = client.post(f"/visual-generation/jobs/{job_id}/export-payload")
    assert exported.status_code == 200
    body = exported.json()
    assert body["provider_payload"]["prompt"]
    assert body["provider_payload"]["safety_notes"]
    assert body["job"]["status"] == "exported"


def test_visual_generation_register_output_rejects_missing_file() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Missing File Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Missing File Video"}).json()
    plan = client.post(f"/visual-assets/from-video/{video['id']}").json()
    queue = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"}).json()
    job_id = queue["jobs"][0]["id"]

    response = client.post(
        f"/visual-generation/jobs/{job_id}/register-output",
        json={"output_path": "/tmp/does-not-exist.visual"},
    )
    assert response.status_code == 400
    assert "existing local file" in response.json()["detail"]


def test_visual_generation_register_output_creates_asset_and_marks_imported_without_approval_side_effects() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Register Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Register Video"}).json()
    plan = client.post(f"/visual-assets/from-video/{video['id']}").json()
    queue = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"}).json()
    job = queue["jobs"][0]
    file_path = _write_real_visual_asset_file("registered_visual.png")

    response = client.post(
        f"/visual-generation/jobs/{job['id']}/register-output",
        json={
            "output_path": str(file_path),
            "mime_type": "image/png",
            "width": 1920,
            "height": 1080,
            "notes": "local render output",
        },
    )
    assert response.status_code == 200
    asset = response.json()
    assert asset["file_exists"] is True
    assert asset["file_path"] == str(file_path)
    assert asset["asset_type"] == job["job_type"]

    refreshed_job = client.get(f"/visual-generation/jobs/{job['id']}").json()
    assert refreshed_job["status"] == "imported"
    assert refreshed_job["output_path"] == str(file_path)

    assets = client.get(f"/visual-generation/assets?plan_id={plan['id']}").json()
    assert any(row["id"] == asset["id"] for row in assets)

    refreshed_video = client.get(f"/videos/{video['id']}").json()
    assert refreshed_video["approved"] is False
    assert refreshed_video["preview_reviewed"] is False


def test_visual_generation_write_routes_require_internal_api_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")

    channel_id = client.post(
        "/channels",
        json={"name": "Visual Queue Key Channel"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()["id"]
    video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Visual Queue Key Video"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()
    plan = client.post(
        f"/visual-assets/from-video/{video['id']}",
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()

    denied = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"})
    assert denied.status_code == 401

    allowed = client.post(
        f"/visual-generation/plans/{plan['id']}/queue",
        json={"provider": "manual"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert allowed.status_code == 200


def test_thumbnail_generate_endpoint_requires_internal_api_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")

    channel_id = client.post(
        "/channels",
        json={"name": "Thumbnail Key Channel"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()["id"]
    video = client.post(
        "/videos",
        json={"channel_id": channel_id, "title": "Thumbnail Key Video"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    ).json()

    denied = client.post(f"/videos/{video['id']}/thumbnail/generate")
    assert denied.status_code == 401

    allowed = client.post(
        f"/videos/{video['id']}/thumbnail/generate",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert allowed.status_code == 200


def test_thumbnail_generate_endpoint_creates_asset_and_keeps_manual_gates() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Thumbnail Generate Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Thumbnail Generate Video"}).json()

    response = client.post(f"/videos/{video['id']}/thumbnail/generate")
    assert response.status_code == 200
    payload = response.json()
    assert payload["video_id"] == video["id"]
    assert payload["fallback_used"] is True
    assert payload["provider"] == "placeholder"
    assert payload["review_status"] == "pending"
    assert payload["visual_asset_id"] > 0
    assert payload["prompt_used"]
    thumbnail_path = Path(payload["thumbnail_path"])
    assert thumbnail_path.exists()
    assert thumbnail_path.is_file()

    visual_asset = client.get(f"/visual-generation/assets/{payload['visual_asset_id']}").json()
    assert visual_asset["asset_type"] == "thumbnail"
    assert visual_asset["file_exists"] is True
    assert visual_asset["file_path"] == str(thumbnail_path)
    queue_rows = client.get(
        f"/visual-generation/assets/review-queue?review_status=pending&video_id={video['id']}"
    ).json()
    assert any(row["id"] == payload["visual_asset_id"] and row["review_status"] == "pending" for row in queue_rows)

    refreshed_video = client.get(f"/videos/{video['id']}").json()
    assert refreshed_video["approved"] is False
    assert refreshed_video["preview_reviewed"] is False


def test_thumbnail_generate_endpoint_missing_video_returns_404() -> None:
    client = TestClient(app)
    response = client.post("/videos/999999/thumbnail/generate")
    assert response.status_code == 404


def test_visual_generation_asset_review_queue_defaults_to_pending_and_includes_context() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Queue Review Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Queue Review Video"}).json()
    plan = client.post(f"/visual-assets/from-video/{video['id']}").json()
    queue = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"}).json()
    scene_job = next((job for job in queue["jobs"] if job.get("visual_scene_id") is not None), queue["jobs"][0])
    job_id = scene_job["id"]

    file_path = _write_real_visual_asset_file("review_queue_pending.png")
    register = client.post(
        f"/visual-generation/jobs/{job_id}/register-output",
        json={"output_path": str(file_path)},
    )
    assert register.status_code == 200
    asset_id = register.json()["id"]

    response = client.get("/visual-generation/assets/review-queue")
    assert response.status_code == 200
    rows = response.json()
    assert rows
    statuses = {row["review_status"] for row in rows}
    assert statuses == {"pending"}
    row = next(item for item in rows if item["id"] == asset_id)
    assert row["video_id"] == video["id"]
    assert row["video_title"] == video["title"]
    assert row["plan_id"] == plan["id"]
    if scene_job.get("visual_scene_id") is not None:
        assert row["scene_number"] is not None


def test_visual_generation_asset_review_queue_filters_and_is_read_only() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Visual Queue Filter Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Queue Filter Video"}).json()
    plan = client.post(f"/visual-assets/from-video/{video['id']}").json()
    queue = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"}).json()
    jobs = queue["jobs"][:2]
    first_path = _write_real_visual_asset_file("review_queue_first.png")
    second_path = _write_real_visual_asset_file("review_queue_second.png")

    first = client.post(
        f"/visual-generation/jobs/{jobs[0]['id']}/register-output",
        json={"output_path": str(first_path)},
    ).json()
    second = client.post(
        f"/visual-generation/jobs/{jobs[1]['id']}/register-output",
        json={"output_path": str(second_path)},
    ).json()

    assert client.post(f"/visual-generation/assets/{first['id']}/approve").status_code == 200
    assert client.post(
        f"/visual-generation/assets/{second['id']}/reject",
        json={"review_notes": "Reject for framing."},
    ).status_code == 200

    approved = client.get("/visual-generation/assets/review-queue?review_status=approved")
    rejected = client.get("/visual-generation/assets/review-queue?review_status=rejected")
    assert approved.status_code == 200
    assert rejected.status_code == 200
    approved_rows = approved.json()
    rejected_rows = rejected.json()
    assert approved_rows and all(row["review_status"] == "approved" for row in approved_rows)
    assert rejected_rows and all(row["review_status"] == "rejected" for row in rejected_rows)
    assert any(row["id"] == first["id"] for row in approved_rows)
    assert any(row["id"] == second["id"] for row in rejected_rows)

    before_all = client.get("/visual-generation/assets/review-queue?review_status=all").json()
    after_all = client.get("/visual-generation/assets/review-queue?review_status=all").json()
    before_map = {item["id"]: item["review_status"] for item in before_all}
    after_map = {item["id"]: item["review_status"] for item in after_all}
    assert before_map == after_map


def test_preview_status_fallback_only_when_no_visual_plan_or_assets() -> None:
    client = TestClient(app)
    video_id = _create_approved_video_for_preview(
        client,
        channel_name="Preview Fallback Channel",
        title="Preview Fallback Video",
    )

    response = client.get(f"/videos/{video_id}/preview/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["preview_asset_mode"] == "fallback_only"
    assert payload["visual_assets_used_count"] == 0
    assert payload["visual_assets_missing_count"] == 0
    assert payload["included_asset_paths"] == []
    assert len(payload["visual_asset_warnings"]) >= 1


def test_preview_status_registered_assets_mode_with_real_local_files() -> None:
    client = TestClient(app)
    video_id = _create_approved_video_for_preview(
        client,
        channel_name="Preview Registered Channel",
        title="Preview Registered Video",
    )
    plan = client.post(f"/visual-assets/from-video/{video_id}").json()
    real_asset_path = _write_real_visual_asset_file("preview_mode_registered.png")

    db = SessionLocal()
    try:
        plan_row = db.scalar(select(VisualAssetPlan).where(VisualAssetPlan.id == plan["id"]).limit(1))
        assert plan_row is not None
        scenes = list(
            db.scalars(
                select(VisualScene)
                .where(VisualScene.plan_id == plan_row.id)
                .order_by(VisualScene.scene_number.asc())
            )
        )
        assert len(scenes) >= 1
        for scene in scenes:
            db.add(
                VisualGeneratedAsset(
                    visual_asset_plan_id=plan_row.id,
                    visual_scene_id=scene.id,
                    generation_job_id=None,
                    asset_type="image",
                    file_path=str(real_asset_path),
                    file_exists=True,
                )
            )
        db.commit()
    finally:
        db.close()

    response = client.get(f"/videos/{video_id}/preview/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["preview_asset_mode"] == "registered_assets"
    assert payload["visual_assets_missing_count"] == 0
    assert payload["visual_assets_used_count"] >= 1
    assert str(real_asset_path) in payload["included_asset_paths"]


def test_preview_status_mixed_mode_excludes_missing_fake_paths() -> None:
    client = TestClient(app)
    video_id = _create_approved_video_for_preview(
        client,
        channel_name="Preview Mixed Channel",
        title="Preview Mixed Video",
    )
    plan = client.post(f"/visual-assets/from-video/{video_id}").json()
    real_asset_path = _write_real_visual_asset_file("preview_mode_mixed_real.png")
    fake_asset_path = (Path(os.environ["OUTPUT_DIR"]).resolve() / "generated" / "preview_mode_missing_fake.png").resolve()

    db = SessionLocal()
    try:
        plan_row = db.scalar(select(VisualAssetPlan).where(VisualAssetPlan.id == plan["id"]).limit(1))
        assert plan_row is not None
        scenes = list(
            db.scalars(
                select(VisualScene)
                .where(VisualScene.plan_id == plan_row.id)
                .order_by(VisualScene.scene_number.asc())
            )
        )
        assert len(scenes) >= 2

        db.add(
            VisualGeneratedAsset(
                visual_asset_plan_id=plan_row.id,
                visual_scene_id=scenes[0].id,
                generation_job_id=None,
                asset_type="image",
                file_path=str(real_asset_path),
                file_exists=True,
            )
        )
        db.add(
            VisualGeneratedAsset(
                visual_asset_plan_id=plan_row.id,
                visual_scene_id=scenes[1].id,
                generation_job_id=None,
                asset_type="image",
                file_path=str(fake_asset_path),
                file_exists=True,
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get(f"/videos/{video_id}/preview/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["preview_asset_mode"] == "mixed"
    assert payload["visual_assets_missing_count"] >= 1
    assert str(real_asset_path) in payload["included_asset_paths"]
    assert str(fake_asset_path) not in payload["included_asset_paths"]
    assert any("Skipped asset" in warning for warning in payload["visual_asset_warnings"])


@pytest.mark.usefixtures("monkeypatch")
def test_preview_render_meta_includes_visual_asset_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    video_id = _create_approved_video_for_preview(
        client,
        channel_name="Preview Meta Visual Channel",
        title="Preview Meta Visual Video",
    )
    plan = client.post(f"/visual-assets/from-video/{video_id}").json()
    real_asset_path = _write_real_visual_asset_file("preview_meta_visual.png")

    db = SessionLocal()
    try:
        plan_row = db.scalar(select(VisualAssetPlan).where(VisualAssetPlan.id == plan["id"]).limit(1))
        assert plan_row is not None
        first_scene = db.scalar(
            select(VisualScene)
            .where(VisualScene.plan_id == plan_row.id)
            .order_by(VisualScene.scene_number.asc())
            .limit(1)
        )
        assert first_scene is not None
        db.add(
            VisualGeneratedAsset(
                visual_asset_plan_id=plan_row.id,
                visual_scene_id=first_scene.id,
                generation_job_id=None,
                asset_type="image",
                file_path=str(real_asset_path),
                file_exists=True,
            )
        )
        db.commit()
    finally:
        db.close()

    _install_fake_preview_renderer(monkeypatch)
    render = client.post(f"/videos/{video_id}/preview/render-draft")
    assert render.status_code == 200

    meta_path = Path(os.environ["OUTPUT_DIR"]).resolve() / "previews" / str(video_id) / "render_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["preview_asset_mode"] in {"fallback_only", "mixed", "registered_assets"}
    assert isinstance(meta["visual_assets_used_count"], int)
    assert isinstance(meta["visual_assets_missing_count"], int)
    assert isinstance(meta["included_asset_paths"], list)
    assert isinstance(meta["visual_asset_warnings"], list)
    assert str(real_asset_path) in meta["included_asset_paths"]


def test_preview_render_does_not_auto_approve_video() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Preview No Auto Approve Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Preview No Auto Approve Video"}).json()

    render = client.post(f"/videos/{video['id']}/preview/render-draft")
    assert render.status_code == 409
    refreshed = client.get(f"/videos/{video['id']}").json()
    assert refreshed["approved"] is False


@pytest.mark.usefixtures("monkeypatch")
def test_preview_render_does_not_mark_preview_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    video_id = _create_approved_video_for_preview(
        client,
        channel_name="Preview No Auto Review Channel",
        title="Preview No Auto Review Video",
    )
    _install_fake_preview_renderer(monkeypatch)

    render = client.post(f"/videos/{video_id}/preview/render-draft")
    assert render.status_code == 200
    body = render.json()
    assert body["preview_reviewed"] is False

    refreshed = client.get(f"/videos/{video_id}").json()
    assert refreshed["approved"] is True
    assert refreshed["preview_reviewed"] is False


def test_command_center_includes_briefs_queues() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Command Center Brief Queue Channel"}).json()["id"]
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI tool stack for local business intake workflows",
            "niche_lane": "AI tool breakdowns",
            "audience": "operators",
            "monetization_path": "affiliate tools",
        },
    ).json()
    assert client.patch(f"/opportunities/{opp['id']}/review", json={"review_status": "approved_for_video"}).status_code == 200
    draft_brief = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]
    approved_brief = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]
    assert client.patch(f"/production-briefs/{approved_brief['id']}/review", json={"status": "approved"}).status_code == 200

    summary = client.get("/command-center/today")
    assert summary.status_code == 200
    body = summary.json()
    draft_ids = {item["brief_id"] for item in body["briefs_needing_review"]}
    approved_ids = {item["brief_id"] for item in body["approved_briefs_ready_to_promote"]}
    assert draft_brief["id"] in draft_ids
    assert approved_brief["id"] in approved_ids


def test_opportunity_review_status_validation_and_rejection() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Review Validation Channel"}).json()["id"]
    opportunity_id = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Local business AI automation audit",
            "niche_lane": "Local AI consulting",
            "audience": "Service business owners",
            "monetization_path": "Audit service",
        },
    ).json()["id"]

    invalid = client.patch(
        f"/opportunities/{opportunity_id}/review",
        json={"review_status": "not_a_status"},
    )
    assert invalid.status_code == 422

    rejected = client.patch(
        f"/opportunities/{opportunity_id}/review",
        json={
            "review_status": "rejected",
            "operator_notes": "Too generic compared with current queue.",
            "rejection_reason": "Low differentiation for this week.",
            "decision_summary": "Revisit with sharper angle and examples.",
        },
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["review_status"] == "rejected"
    assert body["rejection_reason"] == "Low differentiation for this week."
    assert body["reviewed_at"] is not None


def test_opportunity_review_queue_ordering_and_filtering() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Queue Channel"}).json()["id"]

    created_ids: list[int] = []
    for topic in [
        "Best AI tools for small business owners",
        "How to automate customer calls with AI",
        "AI side hustles that are actually useful",
        "Faceless YouTube automation workflow",
    ]:
        response = client.post(
            "/opportunities",
            json={
                "channel_id": channel_id,
                "topic": topic,
                "niche_lane": "Operator lane",
                "audience": "Business operators",
                "monetization_path": "Service + affiliate",
            },
        )
        assert response.status_code == 200
        created_ids.append(response.json()["id"])

    assert len(created_ids) == 4
    first, second, third, fourth = created_ids

    assert client.patch(f"/opportunities/{first}/review", json={"review_status": "unreviewed"}).status_code == 200
    assert client.patch(f"/opportunities/{second}/review", json={"review_status": "shortlisted"}).status_code == 200
    assert client.patch(f"/opportunities/{third}/review", json={"review_status": "needs_more_research"}).status_code == 200
    assert client.patch(f"/opportunities/{fourth}/review", json={"review_status": "rejected"}).status_code == 200

    queue_response = client.get("/opportunities/review-queue")
    assert queue_response.status_code == 200
    queue = queue_response.json()
    queue_by_id = {item["id"]: idx for idx, item in enumerate(queue)}
    assert queue_by_id[second] < queue_by_id[third] < queue_by_id[first] < queue_by_id[fourth]

    filter_response = client.get("/opportunities/review-queue?review_status=shortlisted")
    assert filter_response.status_code == 200
    filtered = filter_response.json()
    assert len(filtered) == 1
    assert filtered[0]["id"] == second


def test_executive_producer_run_empty_state_without_reviewed_opportunities() -> None:
    client = TestClient(app)

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    data = run_response.json()
    assert data["selected_opportunity_id"] is None
    assert data["empty_state_message"] == "No reviewed opportunities are ready. Shortlist or approve an opportunity first."

    current_response = client.get("/executive-producer/recommendation")
    assert current_response.status_code == 200
    assert current_response.json()["empty_state_message"] == data["empty_state_message"]


def test_executive_producer_prefers_approved_for_video_over_shortlisted() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer Priority Channel"}).json()["id"]

    shortlist = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "How to automate customer calls with AI",
            "niche_lane": "Call operations",
            "audience": "Local service business owners",
            "monetization_path": "Service audit",
        },
    ).json()
    approved = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Faceless YouTube automation workflow",
            "niche_lane": "YouTube operations",
            "audience": "Faceless channel operators",
            "monetization_path": "Affiliate + template service",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{shortlist['id']}/review",
        json={"review_status": "shortlisted"},
    ).status_code == 200
    assert client.patch(
        f"/opportunities/{approved['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    result = run_response.json()
    assert result["selected_opportunity_id"] == approved["id"]
    assert result["selected_review_status"] == "approved_for_video"


def test_executive_producer_penalizes_compliance_and_production_difficulty() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer Penalty Channel"}).json()["id"]

    high_risk = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Best AI tools for small business owners",
            "niche_lane": "AI tooling",
            "audience": "Small business owners",
            "monetization_path": "Affiliate stack",
        },
    ).json()
    low_risk = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Local business AI automation audit",
            "niche_lane": "AI consulting",
            "audience": "Local operators",
            "monetization_path": "Service audit",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{high_risk['id']}/review",
        json={"review_status": "shortlisted"},
    ).status_code == 200
    assert client.patch(
        f"/opportunities/{low_risk['id']}/review",
        json={"review_status": "shortlisted"},
    ).status_code == 200

    db = SessionLocal()
    try:
        risky_row = db.get(VideoOpportunity, high_risk["id"])
        safe_row = db.get(VideoOpportunity, low_risk["id"])
        assert risky_row is not None
        assert safe_row is not None
        risky_row.total_score = 34
        risky_row.compliance_risk = 5
        risky_row.production_difficulty = 5
        risky_row.buyer_intent = 2
        risky_row.product_connection = 2

        safe_row.total_score = 30
        safe_row.compliance_risk = 1
        safe_row.production_difficulty = 1
        safe_row.buyer_intent = 5
        safe_row.product_connection = 5
        db.commit()
    finally:
        db.close()

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    result = run_response.json()
    assert result["selected_opportunity_id"] == low_risk["id"]


def test_executive_producer_history_and_audit_event_written() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer History Channel"}).json()["id"]

    opportunity = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI ecommerce product research workflow",
            "niche_lane": "Ecommerce ops",
            "audience": "Ecommerce founders",
            "monetization_path": "Service + affiliate",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{opportunity['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    first = client.post("/executive-producer/recommendation/run")
    second = client.post("/executive-producer/recommendation/run")
    assert first.status_code == 200
    assert second.status_code == 200

    history_response = client.get("/executive-producer/history")
    assert history_response.status_code == 200
    history = history_response.json()
    assert len(history) >= 2
    assert history[0]["id"] != history[1]["id"]

    audit_response = client.get("/audit?limit=100")
    assert audit_response.status_code == 200
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "executive_producer_recommendation_created" in event_types


def test_command_center_today_empty_state_when_no_data() -> None:
    client = TestClient(app)
    response = client.get("/command-center/today")
    assert response.status_code == 200
    body = response.json()
    assert body["summary_status"] == "empty"
    assert body["best_opportunity"] is None
    assert body["executive_recommendation"] is None
    assert body["next_best_action"]["target_page"] == "opportunities"
    assert len(body["operator_checklist"]) >= 3


def test_command_center_today_includes_best_opportunity() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "CC Best Opportunity Channel"}).json()["id"]
    client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Best AI tools for small business owners",
            "niche_lane": "AI tool breakdowns",
            "audience": "small business owners",
            "monetization_path": "affiliate tools + templates",
        },
    )
    response = client.get("/command-center/today")
    assert response.status_code == 200
    body = response.json()
    assert body["best_opportunity"] is not None
    assert body["best_opportunity"]["topic"] == "Best AI tools for small business owners"


def test_command_center_today_includes_latest_executive_recommendation() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "CC Recommendation Channel"}).json()["id"]
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Faceless YouTube automation workflow",
            "niche_lane": "faceless YouTube / creator automation",
            "audience": "creator operators",
            "monetization_path": "templates + affiliate software",
        },
    ).json()
    assert client.patch(
        f"/opportunities/{opp['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200
    assert client.post("/executive-producer/recommendation/run").status_code == 200

    response = client.get("/command-center/today")
    assert response.status_code == 200
    body = response.json()
    assert body["executive_recommendation"] is not None
    assert body["executive_recommendation"]["selected_opportunity_id"] == opp["id"]


def test_command_center_identifies_videos_needing_preview_review() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "CC Preview Review Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Preview Queue Demo"}).json()
    video_id = video["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    _write_real_preview_file(video_id)

    response = client.get("/command-center/today")
    assert response.status_code == 200
    body = response.json()
    preview_ids = {item["video_id"] for item in body["needs_preview_review"]}
    assert video_id in preview_ids


def test_command_center_identifies_videos_needing_compliance_review() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "CC Compliance Review Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Compliance Queue Demo"}).json()
    video_id = video["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200

    response = client.get("/command-center/today")
    assert response.status_code == 200
    body = response.json()
    compliance_ids = {item["video_id"] for item in body["needs_compliance_review"]}
    assert video_id in compliance_ids


def test_command_center_identifies_videos_ready_for_packaging() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "CC Packaging Ready Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Packaging Ready Demo"}).json()
    video_id = video["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    _write_real_preview_file(video_id)
    assert client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True}).status_code == 200

    response = client.get("/command-center/today")
    assert response.status_code == 200
    body = response.json()
    packaging_ids = {item["video_id"] for item in body["ready_for_packaging"]}
    assert video_id in packaging_ids


def test_command_center_identifies_videos_ready_for_payload() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "CC Payload Ready Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Payload Ready Demo"}).json()
    video_id = video["id"]
    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    _write_real_preview_file(video_id)
    assert client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{video_id}/package").status_code == 200

    response = client.get("/command-center/today")
    assert response.status_code == 200
    body = response.json()
    payload_ids = {item["video_id"] for item in body["ready_for_payload"]}
    assert video_id in payload_ids


def test_command_center_get_route_does_not_create_audit_spam() -> None:
    client = TestClient(app)
    before = client.get("/audit?limit=200")
    assert before.status_code == 200
    before_count = len(before.json())

    first = client.get("/command-center/today")
    second = client.get("/command-center/today")
    assert first.status_code == 200
    assert second.status_code == 200

    after = client.get("/audit?limit=200")
    assert after.status_code == 200
    after_count = len(after.json())
    assert after_count == before_count


def test_default_agents_seeded_and_listed() -> None:
    client = TestClient(app)
    channel_response = client.post("/channels", json={"name": "Agent Seed Channel"})
    assert channel_response.status_code == 200

    agents_response = client.get("/agents")
    assert agents_response.status_code == 200
    agents = agents_response.json()
    assert len(agents) >= 7
    names = {agent["name"] for agent in agents}
    assert "AI Tools Agent" in names
    assert "Local Business AI Agent" in names


def test_default_agent_seeding_twice_does_not_create_duplicates() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Agent Idempotent Seed Channel"}).json()["id"]

    db = SessionLocal()
    try:
        seed_default_agents_if_empty(db, channel_id)
        seed_default_agents_if_empty(db, channel_id)

        channel_agents = list(
            db.scalars(
                select(ContentAgent).where(ContentAgent.channel_id == channel_id).order_by(ContentAgent.id.asc())
            )
        )
        assert len(channel_agents) == len(seed_default_agents_if_empty(db, channel_id))
        assert len(channel_agents) == 7

        normalized_pairs = {(
            " ".join(agent.name.lower().split()),
            " ".join(agent.lane.lower().split()),
        ) for agent in channel_agents}
        assert len(normalized_pairs) == len(channel_agents)

        seed_events = list(
            db.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == "agent_created_seed")
                .order_by(AuditEvent.id.asc())
            )
        )
        channel_seed_events = [
            event
            for event in seed_events
            if (json.loads(event.metadata_json) if event.metadata_json else {}).get("channel_id") == channel_id
        ]
        assert len(channel_seed_events) == 7
    finally:
        db.close()


def test_init_db_is_read_only_when_database_is_already_migrated() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Agent InitDb Idempotent Channel"}).json()["id"]

    db = SessionLocal()
    try:
        before = len(list(db.scalars(select(ContentAgent).where(ContentAgent.channel_id == channel_id))))
    finally:
        db.close()

    init_db()
    init_db()

    db = SessionLocal()
    try:
        channel_agents = list(
            db.scalars(
                select(ContentAgent).where(ContentAgent.channel_id == channel_id).order_by(ContentAgent.id.asc())
            )
        )
        assert len(channel_agents) == before == 7

        normalized_pairs = {(
            " ".join(agent.name.lower().split()),
            " ".join(agent.lane.lower().split()),
        ) for agent in channel_agents}
        assert len(normalized_pairs) == len(channel_agents)
    finally:
        db.close()


def test_patch_agent_updates_editable_fields() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Agent Update Channel"}).json()["id"]
    agents = client.get("/agents").json()
    target = next(agent for agent in agents if agent["channel_id"] == channel_id and agent["name"] == "AI Tools Agent")

    patch_response = client.patch(
        f"/agents/{target['id']}",
        json={
            "focus": "Tool comparisons with operator walkthroughs.",
            "monetization_focus": "Affiliate software and templates.",
            "compliance_notes": "No unsupported vendor claims.",
            "production_rules": "Use practical setup demos.",
            "is_active": False,
        },
    )
    assert patch_response.status_code == 200
    body = patch_response.json()
    assert body["focus"] == "Tool comparisons with operator walkthroughs."
    assert body["monetization_focus"] == "Affiliate software and templates."
    assert body["compliance_notes"] == "No unsupported vendor claims."
    assert body["production_rules"] == "Use practical setup demos."
    assert body["is_active"] is False


def test_channel_studio_seed_requires_internal_api_key_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")

    denied = client.post("/agents/seed-default-channel-studio")
    assert denied.status_code == 401

    first = client.post(
        "/agents/seed-default-channel-studio",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert first.status_code == 200
    first_rows = first.json()
    assert len(first_rows) == 7

    second = client.post(
        "/agents/seed-default-channel-studio",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert second.status_code == 200
    second_rows = second.json()
    assert len(second_rows) == 7
    assert sorted(row["name"] for row in first_rows) == sorted(row["name"] for row in second_rows)

    channels = client.get("/channels").json()
    assert channels == []
    assert all((row.get("channel_url") or "") == "" for row in second_rows)


def test_channel_studio_list_and_patch_update_fields() -> None:
    client = TestClient(app)
    assert client.post("/agents/seed-default-channel-studio").status_code == 200

    listing = client.get("/agents/channel-studio")
    assert listing.status_code == 200
    agents = listing.json()
    assert len(agents) == 7
    target = agents[0]

    patch = client.patch(
        f"/agents/channel-studio/{target['id']}",
        json={
            "name": "AI Tools / Automation Prime",
            "niche": "AI tools and automation workflows",
            "target_viewer": "Ops founders",
            "content_pillars": ["Tool comparisons", "Automation playbooks", "Prompt systems"],
            "title_style": "Outcome-first hooks",
            "thumbnail_style": "Bold contrast style",
            "script_style": "45-60 second one-point structure",
            "compliance_notes": "No guarantees",
            "launch_wave": 2,
            "launch_status": "ready_to_launch",
            "channel_url": None,
            "channel_handle": "@aitoolsprime",
            "notes": "Planning only",
        },
    )
    assert patch.status_code == 200
    body = patch.json()
    assert body["name"] == "AI Tools / Automation Prime"
    assert body["launch_status"] == "ready_to_launch"
    assert body["launch_wave"] == 2
    assert body["content_pillars"] == ["Tool comparisons", "Automation playbooks", "Prompt systems"]

    invalid = client.patch(
        f"/agents/channel-studio/{target['id']}",
        json={"launch_status": "invalid_status"},
    )
    assert invalid.status_code == 422


def test_channel_studio_agent_shorts_batch_requires_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    assert client.post("/agents/seed-default-channel-studio").status_code == 200
    agent_id = client.get("/agents/channel-studio").json()[0]["id"]

    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")
    denied = client.post(f"/agents/{agent_id}/shorts-batch", json={"count": 2})
    assert denied.status_code == 401

    allowed = client.post(
        f"/agents/{agent_id}/shorts-batch",
        json={"count": 2},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert allowed.status_code == 200


def test_channel_studio_agent_shorts_batch_caps_count_and_preserves_manual_gates() -> None:
    client = TestClient(app)
    assert client.post("/agents/seed-default-channel-studio").status_code == 200
    agent = client.get("/agents/channel-studio").json()[0]

    response = client.post(
        f"/agents/{agent['id']}/shorts-batch",
        json={"count": 12, "topic_seed": "Local operator workflow", "auto_generate_assets": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_id"] == agent["id"]
    assert payload["requested_count"] == 12
    assert payload["created_count"] == 10
    assert payload["videos"]
    assert any("max per batch" in warning.lower() for warning in payload["warnings"])

    first_video_id = payload["videos"][0]["id"]
    video = client.get(f"/videos/{first_video_id}").json()
    assert video["channel_studio_agent_id"] == agent["id"]
    assert video["content_type"] == "short"
    assert video["approved"] is False
    assert video["preview_reviewed"] is False
    assert video["niche"] == agent["niche"]
    assert video["target_viewer"] == agent["target_viewer"]
    assert video["notes"]
    assert "channel studio agent" in video["notes"].lower()
    assert any(video["pillar"] == pillar for pillar in agent["content_pillars"])


def test_channel_studio_scoreboard_has_waves_and_conservative_signals() -> None:
    client = TestClient(app)
    assert client.post("/agents/seed-default-channel-studio").status_code == 200
    agent = client.get("/agents/channel-studio").json()[0]

    empty_scoreboard = client.get("/agents/channel-studio/scoreboard")
    assert empty_scoreboard.status_code == 200
    empty_body = empty_scoreboard.json()
    row_before = next(item for item in empty_body["agents"] if item["agent_id"] == agent["id"])
    assert row_before["metrics_sample_size"] == 0
    assert row_before["readiness_score"] >= 0
    assert row_before["readiness_score"] <= 100
    assert "guarantee" not in row_before["recommended_action"].lower()
    assert empty_body["launch_waves"]

    batch = client.post(
        f"/agents/{agent['id']}/shorts-batch",
        json={"count": 1, "auto_generate_assets": True},
    )
    assert batch.status_code == 200
    video_id = batch.json()["videos"][0]["id"]
    thumb = client.post(f"/videos/{video_id}/thumbnail/generate")
    assert thumb.status_code == 200

    with_pending = client.get("/agents/channel-studio/scoreboard").json()
    row_pending = next(item for item in with_pending["agents"] if item["agent_id"] == agent["id"])
    assert row_pending["thumbnail_pending_count"] >= 1

    assert client.post(f"/visual-generation/assets/{thumb.json()['visual_asset_id']}/approve").status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    _write_real_preview_file(video_id)
    assert client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{video_id}/package").status_code == 200
    assert client.post(f"/publish/{video_id}/prepare-youtube-payload").status_code == 200
    # Phase 30: set up production-ready state before generating payload
    _write_final_voiceover_production(video_id)
    _write_clean_description_asset(video_id)
    _write_final_export_file(video_id)
    assert client.post(f"/videos/{video_id}/publishing-payload/generate").status_code == 200

    ready_board = client.get("/agents/channel-studio/scoreboard")
    assert ready_board.status_code == 200
    row_ready = next(item for item in ready_board.json()["agents"] if item["agent_id"] == agent["id"])
    assert row_ready["payload_ready_count"] >= 1
    assert row_ready["readiness_score"] >= row_pending["readiness_score"]
    assert row_ready["readiness_score"] <= 100


def test_agent_opportunities_and_videos_routes_and_promotion_carry_agent() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Agent Mapping Channel"}).json()["id"]

    agents = client.get("/agents").json()
    local_agent = next(agent for agent in agents if agent["channel_id"] == channel_id and agent["name"] == "Local Business AI Agent")

    opportunity = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "How to automate customer calls with AI",
            "niche_lane": "local business AI automation",
            "audience": "Service business owners",
            "monetization_path": "SkybridgeCX leads + audits",
        },
    ).json()
    assert opportunity["assigned_agent_id"] == local_agent["id"]

    opp_list_response = client.get(f"/agents/{local_agent['id']}/opportunities")
    assert opp_list_response.status_code == 200
    opp_rows = opp_list_response.json()
    assert any(item["id"] == opportunity["id"] for item in opp_rows)

    assert client.patch(
        f"/opportunities/{opportunity['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    promoted_video = client.post(f"/opportunities/{opportunity['id']}/promote-to-video").json()
    assert promoted_video["assigned_agent_id"] == local_agent["id"]
    assert promoted_video["approved"] is False
    assert promoted_video["preview_reviewed"] is False
    assert promoted_video["status"] == "idea"

    agent_videos_response = client.get(f"/agents/{local_agent['id']}/videos")
    assert agent_videos_response.status_code == 200
    video_rows = agent_videos_response.json()
    assert any(item["id"] == promoted_video["id"] for item in video_rows)


def test_lane_alias_matching_for_ecommerce_and_local_business_agents() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Lane Alias Match Channel"}).json()["id"]

    ecommerce = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI ecommerce product research workflow",
            "niche_lane": "AI ecommerce ops",
            "audience": "ecommerce founders",
            "monetization_path": "affiliate tools + templates",
        },
    ).json()
    local_business = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "How to automate customer calls with AI",
            "niche_lane": "local business AI operations",
            "audience": "local business owners",
            "monetization_path": "SkybridgeCX leads + audits",
        },
    ).json()

    assert ecommerce["assigned_agent"] == "Ecommerce AI Agent"
    assert ecommerce["assigned_agent_id"] is not None
    assert local_business["assigned_agent"] == "Local Business AI Agent"
    assert local_business["assigned_agent_id"] is not None


def test_executive_producer_includes_matched_agent_profile() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer Agent Profile Channel"}).json()["id"]

    opportunity = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Faceless YouTube automation workflow",
            "niche_lane": "faceless YouTube / creator automation",
            "audience": "Creator operators",
            "monetization_path": "Templates + affiliate software",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{opportunity['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["selected_opportunity_id"] == opportunity["id"]
    assert payload["matched_agent_id"] is not None
    assert payload["matched_agent_name"] == "Faceless Creator Agent"
    assert payload["matched_agent_lane"] == "faceless YouTube / creator automation"
    assert payload["matched_agent_focus"]
    assert payload["matched_agent_monetization_focus"]
    assert payload["matched_agent_compliance_notes"]
    assert payload["matched_agent_production_rules"]


def test_executive_producer_alias_lane_includes_matched_agent_profile_fields() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Producer Alias Agent Profile Channel"}).json()["id"]

    opportunity = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "AI ecommerce automation for store operations",
            "niche_lane": "ecommerce founders",
            "audience": "ecommerce operators",
            "monetization_path": "Affiliate + consulting audit",
        },
    ).json()

    assert client.patch(
        f"/opportunities/{opportunity['id']}/review",
        json={"review_status": "approved_for_video"},
    ).status_code == 200

    run_response = client.post("/executive-producer/recommendation/run")
    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["selected_opportunity_id"] == opportunity["id"]
    assert payload["matched_agent_name"] == "Ecommerce AI Agent"
    assert payload["matched_agent_lane"] == "ecommerce AI"
    assert payload["matched_agent_focus"]
    assert payload["matched_agent_monetization_focus"]
    assert payload["matched_agent_compliance_notes"]
    assert payload["matched_agent_production_rules"]


def test_pipeline_daily_empty_state() -> None:
    client = TestClient(app)
    response = client.get("/pipeline/daily")
    assert response.status_code == 200
    body = response.json()
    assert body["opportunities_to_review"] == []
    assert body["producer_recommendations"] == []
    assert body["briefs_to_review"] == []
    assert body["approved_briefs_ready_to_promote"] == []
    assert body["videos_needing_assets"] == []
    assert body["videos_missing_visual_plans"] == []
    assert body["videos_visual_jobs_pending"] == []
    assert body["videos_visual_assets_registered"] == []
    assert body["videos_needing_preview"] == []
    assert body["videos_needing_preview_review"] == []
    assert body["videos_needing_compliance"] == []
    assert body["videos_needing_manual_approval"] == []
    assert body["videos_ready_to_package"] == []
    assert body["videos_ready_for_payload"] == []
    assert body["completed_payloads"] == []
    assert body["next_step"]["key"] == "pipeline_clear"


def test_pipeline_daily_includes_opportunities_to_review() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Pipeline Opportunity Channel"}).json()["id"]
    created = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Best AI tools for small business owners",
            "niche_lane": "AI tool breakdowns",
            "audience": "small business owners",
            "monetization_path": "affiliate tools + templates",
        },
    )
    assert created.status_code == 200

    response = client.get("/pipeline/daily")
    assert response.status_code == 200
    body = response.json()
    topics = {item["topic"] for item in body["opportunities_to_review"]}
    assert "Best AI tools for small business owners" in topics


def test_pipeline_daily_surfaces_missing_visual_plan_before_preview() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Pipeline Visual Plan Gate Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Needs Visual Plan"}).json()
    video_id = video["id"]

    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200

    response = client.get("/pipeline/daily")
    assert response.status_code == 200
    body = response.json()
    missing_ids = {item["video_id"] for item in body["videos_missing_visual_plans"]}
    assert video_id in missing_ids
    assert video_id not in {item["video_id"] for item in body["videos_needing_preview"]}


def test_pipeline_daily_surfaces_visual_jobs_pending_and_registered_assets() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Pipeline Visual Queue Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Visual Queue Pipeline Video"}).json()
    video_id = video["id"]

    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    plan = client.post(f"/visual-assets/from-video/{video_id}").json()
    queue = client.post(f"/visual-generation/plans/{plan['id']}/queue", json={"provider": "manual"}).json()
    first_job_id = queue["jobs"][0]["id"]

    first = client.get("/pipeline/daily")
    assert first.status_code == 200
    first_body = first.json()
    assert video_id in {item["video_id"] for item in first_body["videos_visual_jobs_pending"]}

    file_path = _write_real_visual_asset_file("pipeline_registered_visual.png")
    register = client.post(
        f"/visual-generation/jobs/{first_job_id}/register-output",
        json={"output_path": str(file_path)},
    )
    assert register.status_code == 200

    second = client.get("/pipeline/daily")
    assert second.status_code == 200
    second_body = second.json()
    assert video_id in {item["video_id"] for item in second_body["videos_visual_assets_registered"]}


def test_pipeline_daily_includes_briefs_review_and_approved_buckets() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Pipeline Brief Buckets Channel"}).json()["id"]
    opp = client.post(
        "/opportunities",
        json={
            "channel_id": channel_id,
            "topic": "Faceless YouTube automation workflow",
            "niche_lane": "faceless YouTube / creator automation",
            "audience": "creator operators",
            "monetization_path": "templates + affiliate software",
        },
    ).json()
    assert client.patch(f"/opportunities/{opp['id']}/review", json={"review_status": "approved_for_video"}).status_code == 200

    draft_brief = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]
    approved_brief = client.post(f"/production-briefs/from-opportunity/{opp['id']}").json()["brief"]
    assert client.patch(f"/production-briefs/{approved_brief['id']}/review", json={"status": "approved"}).status_code == 200

    response = client.get("/pipeline/daily")
    assert response.status_code == 200
    body = response.json()
    draft_ids = {item["brief_id"] for item in body["briefs_to_review"]}
    approved_ids = {item["brief_id"] for item in body["approved_briefs_ready_to_promote"]}
    assert draft_brief["id"] in draft_ids
    assert approved_brief["id"] in approved_ids


def test_pipeline_daily_video_stage_buckets() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Pipeline Video Buckets Channel"}).json()["id"]

    needs_assets = client.post("/videos", json={"channel_id": channel_id, "title": "Needs Assets"}).json()

    needs_compliance = client.post("/videos", json={"channel_id": channel_id, "title": "Needs Compliance"}).json()
    assert client.post(f"/videos/{needs_compliance['id']}/generate", json={"stage": "all"}).status_code == 200

    needs_manual = client.post("/videos", json={"channel_id": channel_id, "title": "Needs Manual Approval"}).json()
    assert client.post(f"/videos/{needs_manual['id']}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{needs_manual['id']}/compliance/run").status_code == 200

    needs_preview = client.post("/videos", json={"channel_id": channel_id, "title": "Needs Preview"}).json()
    assert client.post(f"/videos/{needs_preview['id']}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{needs_preview['id']}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{needs_preview['id']}").status_code == 200

    needs_preview_review = client.post("/videos", json={"channel_id": channel_id, "title": "Needs Preview Review"}).json()
    assert client.post(f"/videos/{needs_preview_review['id']}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{needs_preview_review['id']}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{needs_preview_review['id']}").status_code == 200
    _write_real_preview_file(needs_preview_review["id"])

    ready_to_package = client.post("/videos", json={"channel_id": channel_id, "title": "Ready To Package"}).json()
    assert client.post(f"/videos/{ready_to_package['id']}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{ready_to_package['id']}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{ready_to_package['id']}").status_code == 200
    _write_real_preview_file(ready_to_package["id"])
    assert client.post(f"/videos/{ready_to_package['id']}/preview/review", json={"reviewed": True}).status_code == 200

    ready_for_payload = client.post("/videos", json={"channel_id": channel_id, "title": "Ready For Payload"}).json()
    assert client.post(f"/videos/{ready_for_payload['id']}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{ready_for_payload['id']}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{ready_for_payload['id']}").status_code == 200
    _write_real_preview_file(ready_for_payload["id"])
    assert client.post(f"/videos/{ready_for_payload['id']}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{ready_for_payload['id']}/package").status_code == 200

    completed_payload = client.post("/videos", json={"channel_id": channel_id, "title": "Completed Payload"}).json()
    assert client.post(f"/videos/{completed_payload['id']}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{completed_payload['id']}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{completed_payload['id']}").status_code == 200
    _write_real_preview_file(completed_payload["id"])
    assert client.post(f"/videos/{completed_payload['id']}/preview/review", json={"reviewed": True}).status_code == 200
    assert client.post(f"/videos/{completed_payload['id']}/package").status_code == 200
    assert client.post(f"/publish/{completed_payload['id']}/prepare-youtube-payload").status_code == 200

    response = client.get("/pipeline/daily")
    assert response.status_code == 200
    body = response.json()

    assert needs_assets["id"] in {item["video_id"] for item in body["videos_needing_assets"]}
    assert needs_compliance["id"] in {item["video_id"] for item in body["videos_needing_compliance"]}
    assert needs_manual["id"] in {item["video_id"] for item in body["videos_needing_manual_approval"]}
    assert needs_preview["id"] in {item["video_id"] for item in body["videos_needing_preview"]}
    assert needs_preview_review["id"] in {item["video_id"] for item in body["videos_needing_preview_review"]}
    assert ready_to_package["id"] in {item["video_id"] for item in body["videos_ready_to_package"]}
    assert ready_for_payload["id"] in {item["video_id"] for item in body["videos_ready_for_payload"]}
    assert completed_payload["id"] in {item["video_id"] for item in body["completed_payloads"]}


def test_pipeline_daily_needing_preview_is_not_completed_payload() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Pipeline Preview Conflict Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Preview Conflict Video"}).json()
    video_id = video["id"]

    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{video_id}").status_code == 200

    db = SessionLocal()
    try:
        row = db.get(Video, video_id)
        assert row is not None
        row.publish_status = "ready"
        row.status = "publish_ready"
        db.commit()
    finally:
        db.close()

    response = client.get("/pipeline/daily")
    assert response.status_code == 200
    body = response.json()
    needing_preview_ids = {item["video_id"] for item in body["videos_needing_preview"]}
    completed_ids = {item["video_id"] for item in body["completed_payloads"]}
    assert video_id in needing_preview_ids
    assert video_id not in completed_ids


def test_pipeline_daily_completed_payload_only_after_prior_blockers_cleared() -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Pipeline Completed Gate Channel"}).json()["id"]
    video = client.post("/videos", json={"channel_id": channel_id, "title": "Completed Gate Video"}).json()
    video_id = video["id"]

    assert client.post(f"/videos/{video_id}/generate", json={"stage": "all"}).status_code == 200
    assert client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "approved"}).status_code == 200
    assert client.post(f"/visual-assets/from-video/{video_id}").status_code == 200

    first = client.get("/pipeline/daily")
    assert first.status_code == 200
    first_body = first.json()
    assert video_id in {item["video_id"] for item in first_body["videos_needing_preview"]}
    assert video_id not in {item["video_id"] for item in first_body["completed_payloads"]}

    _write_real_preview_file(video_id)
    assert client.post(f"/videos/{video_id}/preview/review", json={"reviewed": True}).status_code == 200

    second = client.get("/pipeline/daily")
    assert second.status_code == 200
    second_body = second.json()
    assert video_id in {item["video_id"] for item in second_body["videos_ready_to_package"]}
    assert video_id not in {item["video_id"] for item in second_body["completed_payloads"]}

    assert client.post(f"/videos/{video_id}/package").status_code == 200

    third = client.get("/pipeline/daily")
    assert third.status_code == 200
    third_body = third.json()
    assert video_id in {item["video_id"] for item in third_body["videos_ready_for_payload"]}
    assert video_id not in {item["video_id"] for item in third_body["completed_payloads"]}

    assert client.post(f"/publish/{video_id}/prepare-youtube-payload").status_code == 200

    fourth = client.get("/pipeline/daily")
    assert fourth.status_code == 200
    fourth_body = fourth.json()
    assert video_id in {item["video_id"] for item in fourth_body["completed_payloads"]}
    assert video_id not in {item["video_id"] for item in fourth_body["videos_needing_preview"]}


def test_pipeline_daily_get_does_not_create_audit_spam() -> None:
    client = TestClient(app)
    before = client.get("/audit?limit=200")
    assert before.status_code == 200
    before_count = len(before.json())

    first = client.get("/pipeline/daily")
    second = client.get("/pipeline/daily")
    assert first.status_code == 200
    assert second.status_code == 200

    after = client.get("/audit?limit=200")
    assert after.status_code == 200
    after_count = len(after.json())
    assert after_count == before_count


def _mock_research_sources() -> tuple[list[SourceVideo], list[SourceChannel]]:
    videos = [
        SourceVideo(
            youtube_video_id="vid_1",
            youtube_channel_id="chan_1",
            title="How to build an AI tool stack for local business workflows",
            channel_title="Ops Channel One",
            description="Tutorial with tool comparison and workflow checklist for operators.",
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            duration="PT11M5S",
            view_count=12000,
            like_count=640,
            comment_count=88,
            thumbnail_url="https://example.com/thumb1.jpg",
        ),
        SourceVideo(
            youtube_video_id="vid_2",
            youtube_channel_id="chan_2",
            title="Best AI tools vs old manual workflows (comparison)",
            channel_title="Ops Channel Two",
            description="Comparison format focused on tool stack upgrades and operator demos.",
            published_at=datetime(2026, 1, 11, tzinfo=timezone.utc),
            duration="PT9M40S",
            view_count=9800,
            like_count=521,
            comment_count=52,
            thumbnail_url="https://example.com/thumb2.jpg",
        ),
    ]
    channels = [
        SourceChannel(
            youtube_channel_id="chan_1",
            title="Ops Channel One",
            description="Operator workflow videos",
            subscriber_count=25000,
            video_count=180,
            view_count=1800000,
        ),
        SourceChannel(
            youtube_channel_id="chan_2",
            title="Ops Channel Two",
            description="AI tools and automation breakdowns",
            subscriber_count=17000,
            video_count=140,
            view_count=1200000,
        ),
    ]
    return videos, channels


def test_research_run_missing_api_key_returns_setup_required(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    monkeypatch.setattr(
        research_router,
        "get_settings",
        lambda: type("SettingsStub", (), {"youtube_data_api_key": None})(),
    )
    channel_id = client.post("/channels", json={"name": "Research Setup Channel"}).json()["id"]
    assert channel_id > 0

    response = client.post(
        "/research/youtube/run",
        json={
            "niche_lane": "AI tool breakdowns",
            "query": "best ai tools for operators",
            "max_results": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "setup_required"
    assert body["setup_required"] is True
    assert "YOUTUBE_DATA_API_KEY" in (body["setup_message"] or "")

    runs = client.get("/research/runs").json()
    assert any(item["id"] == body["id"] for item in runs)


def test_research_pattern_analysis_is_deterministic_from_mocked_sources() -> None:
    videos, channels = _mock_research_sources()
    first = build_research_patterns(
        niche_lane="AI tool breakdowns",
        query="best ai tools for operators",
        source_videos=videos,
        source_channels=channels,
    )
    second = build_research_patterns(
        niche_lane="AI tool breakdowns",
        query="best ai tools for operators",
        source_videos=videos,
        source_channels=channels,
    )
    first_payload = [(item.pattern_type, item.label, item.details, item.signal_strength) for item in first]
    second_payload = [(item.pattern_type, item.label, item.details, item.signal_strength) for item in second]
    assert first_payload == second_payload
    assert any(item[0] == "title_patterns" for item in first_payload)
    assert any(item[0] == "compliance_flags" for item in first_payload)


def test_research_strategy_includes_original_direction_and_what_not_to_copy() -> None:
    videos, channels = _mock_research_sources()
    patterns = build_research_patterns(
        niche_lane="AI tool breakdowns",
        query="best ai tools for operators",
        source_videos=videos,
        source_channels=channels,
    )
    strategy = build_research_strategy(
        niche_lane="AI tool breakdowns",
        query="best ai tools for operators",
        patterns=patterns,
    )
    assert strategy.original_video_angles
    assert strategy.recommended_topics
    assert "Do not copy exact titles" in strategy.what_not_to_copy
    assert "original" in strategy.recommended_next_action.lower()


def test_research_create_opportunities_workflow_and_duplicate_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    channel_id = client.post("/channels", json={"name": "Research Opportunity Channel"}).json()["id"]
    assert channel_id > 0
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "test_key")

    def fake_fetch_youtube_sources(*, api_key: str, query: str, max_results: int):  # noqa: ANN202
        assert api_key == "test_key"
        assert query == "best ai tools for operators"
        assert max_results == 10
        return _mock_research_sources()

    monkeypatch.setattr(research_router, "fetch_youtube_sources", fake_fetch_youtube_sources)

    run_response = client.post(
        "/research/youtube/run",
        json={
            "niche_lane": "AI tool breakdowns",
            "query": "best ai tools for operators",
            "max_results": 10,
        },
    )
    assert run_response.status_code == 200
    run_body = run_response.json()
    assert run_body["status"] == "completed"
    assert run_body["setup_required"] is False
    assert run_body["strategy_detail"] is not None
    run_id = run_body["id"]

    first_create = client.post(f"/research/runs/{run_id}/create-opportunities")
    assert first_create.status_code == 200
    first_body = first_create.json()
    assert first_body["created_count"] >= 1
    assert first_body["skipped_duplicates"] >= 0
    assert len(first_body["created_ids"]) == first_body["created_count"]

    opportunities = client.get("/opportunities").json()
    created_map = {item["id"]: item for item in opportunities if item["id"] in first_body["created_ids"]}
    assert len(created_map) == first_body["created_count"]
    for created in created_map.values():
        assert created["review_status"] == "unreviewed"
        assert created["promoted_video_id"] is None

    first_created_id = first_body["created_ids"][0]
    blocked_promote = client.post(f"/opportunities/{first_created_id}/promote-to-video")
    assert blocked_promote.status_code == 400
    assert "approved" in blocked_promote.json()["detail"].lower()

    second_create = client.post(f"/research/runs/{run_id}/create-opportunities")
    assert second_create.status_code == 200
    second_body = second_create.json()
    assert second_body["created_count"] == 0
    assert second_body["skipped_duplicates"] == first_body["requested_topics"]


def _run_review_prep_batch(client: TestClient, count: int = 1, **overrides) -> dict[str, object]:
    payload = {
        "count": count,
        "content_type": "short",
        "auto_generate_placeholders": True,
        "auto_render_preview": False,
        "auto_generate_payload": True,
    }
    payload.update(overrides)
    response = client.post("/review-prep/runs", json=payload)
    assert response.status_code == 200
    return response.json()


def test_review_prep_run_requires_internal_api_key_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(main_module.settings, "internal_api_key", "test-internal-key")

    denied = client.post("/review-prep/runs", json={"count": 1})
    assert denied.status_code == 401

    allowed = client.post(
        "/review-prep/runs",
        json={"count": 1, "auto_render_preview": False},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert allowed.status_code == 200


def test_review_prep_run_creates_workflow_artifacts_and_preserves_manual_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    settings = content_engine.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "image_generation_api_key", None)
    monkeypatch.setattr(settings, "image_generation_provider", "placeholder")
    content_engine.clear_asset_cache()

    payload = _run_review_prep_batch(client, count=1, auto_render_preview=False)
    assert payload["requested_count"] == 1
    assert payload["created_count"] == 1
    assert payload["ready_for_final_approval_count"] >= 0
    assert payload["run_status"] in {"completed", "blocked"}
    assert payload["videos"]

    row = payload["videos"][0]
    assert row["opportunity_id"] is not None
    assert row["brief_id"] is not None
    assert row["video_id"] > 0
    assert row["final_review_packet_path"]
    packet_path = Path(row["final_review_packet_path"])
    assert packet_path.exists()
    packet_payload = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet_payload["final_approval_required"] is True
    assert packet_payload["local_only"] is True
    assert isinstance(packet_payload.get("visual_assets"), list)
    assert any(str(asset.get("file_path", "")).endswith(".svg") for asset in packet_payload["visual_assets"])

    video = client.get(f"/videos/{row['video_id']}").json()
    assert video["approved"] is False
    assert video["preview_reviewed"] is False
    assert video["status"] != "published"

    assets = client.get(f"/videos/{row['video_id']}/assets").json()
    assert assets
    assert any(asset["asset_type"] == "script" for asset in assets)

    perf = client.get(f"/videos/{row['video_id']}/performance").json()
    assert perf["has_data"] is False
    assert perf["published_url"] is None

    db = SessionLocal()
    try:
        publish_records = list(db.scalars(select(PublishRecord).where(PublishRecord.video_id == row["video_id"])))
        assert publish_records == []
    finally:
        db.close()

    queue = client.get("/review-prep/final-review-queue?limit=100")
    assert queue.status_code == 200
    queue_rows = queue.json()
    matching = [item for item in queue_rows if item["video_id"] == row["video_id"]]
    assert matching
    assert matching[0]["final_review_packet_path"] == row["final_review_packet_path"]


def test_review_prep_final_review_decision_rules_and_side_effects() -> None:
    client = TestClient(app)

    missing_channel_id = client.post("/channels", json={"name": "Review Prep Missing Packet Channel"}).json()["id"]
    missing_video_id = client.post(
        "/videos",
        json={"channel_id": missing_channel_id, "title": "Missing Packet Video"},
    ).json()["id"]
    missing_packet = client.post(
        f"/review-prep/videos/{missing_video_id}/final-review-decision",
        json={"decision": "approve", "notes": "should fail"},
    )
    assert missing_packet.status_code == 400
    assert "packet" in missing_packet.json()["detail"].lower()

    run = _run_review_prep_batch(client, count=3, auto_render_preview=False)
    assert run["created_count"] == 3
    run_videos = [item for item in run["videos"] if item["video_id"] > 0]
    assert len(run_videos) == 3

    blocked_video_id = run_videos[0]["video_id"]
    assert client.patch(
        f"/videos/{blocked_video_id}/assets/script",
        json={"body": "Guaranteed results. You will make $10k fast."},
    ).status_code == 200
    blocked_approve = client.post(
        f"/review-prep/videos/{blocked_video_id}/final-review-decision",
        json={"decision": "approve", "notes": "try blocked"},
    )
    assert blocked_approve.status_code == 400
    assert "compliance" in blocked_approve.json()["detail"].lower()

    approve_video_id = run_videos[1]["video_id"]
    approve = client.post(
        f"/review-prep/videos/{approve_video_id}/final-review-decision",
        json={"decision": "approve", "notes": "ready for manual upload checklist"},
    )
    assert approve.status_code == 200
    approve_body = approve.json()
    assert approve_body["approval_status"] == "approved"
    approved_video = client.get(f"/videos/{approve_video_id}").json()
    assert approved_video["approved"] is True
    assert approved_video["status"] == "approved"

    reject_video_id = run_videos[2]["video_id"]
    packet_path = Path(run_videos[2]["final_review_packet_path"])
    assert packet_path.exists()
    reject = client.post(
        f"/review-prep/videos/{reject_video_id}/final-review-decision",
        json={"decision": "reject", "notes": "needs full rewrite"},
    )
    assert reject.status_code == 200
    reject_body = reject.json()
    assert reject_body["approval_status"] == "rejected"
    rejected_video = client.get(f"/videos/{reject_video_id}").json()
    assert rejected_video["approved"] is False
    assert rejected_video["status"] == "rejected"
    assert packet_path.exists()
    db = SessionLocal()
    try:
        rejected_row = db.get(Video, reject_video_id)
        assert rejected_row is not None
        assert rejected_row.final_approval_status == "rejected"
        assert rejected_row.final_approval_notes == "needs full rewrite"
    finally:
        db.close()

    needs_changes = client.post(
        f"/review-prep/videos/{reject_video_id}/final-review-decision",
        json={"decision": "needs_changes", "notes": "improve opening section"},
    )
    assert needs_changes.status_code == 200
    nc_body = needs_changes.json()
    assert nc_body["approval_status"] == "needs_changes"
    changed_video = client.get(f"/videos/{reject_video_id}").json()
    assert changed_video["approved"] is False
    assert changed_video["status"] != "approved"
    db = SessionLocal()
    try:
        changed_row = db.get(Video, reject_video_id)
        assert changed_row is not None
        assert changed_row.final_approval_status == "needs_changes"
        assert changed_row.final_approval_notes == "improve opening section"
    finally:
        db.close()
