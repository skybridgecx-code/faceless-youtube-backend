from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Iterator
import wave

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
TEST_DATABASE = ROOT / "test_content_factory.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE}"

from dbos import DBOS  # noqa: E402

import app.production.providers as provider_module  # noqa: E402
import app.production.budget as budget_module  # noqa: E402
import app.production.media as media_module  # noqa: E402
import app.production.persistence as production_persistence  # noqa: E402
import app.workflows.i5_production_workflow as workflow_module  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import (  # noqa: E402
    Approval,
    Artifact,
    Campaign,
    CampaignBudgetOverride,
    GateDecision,
    GenerationJob,
    Scene,
    SessionLocal,
    engine,
)
from app.editorial.claims import compile_research_packet  # noqa: E402
from app.editorial.contracts import (  # noqa: E402
    I4_POLICY_VERSION,
    canonical_json,
    canonical_sha256,
)
from app.editorial.persistence import (  # noqa: E402
    bind_i4_campaign_workflow,
    persist_editorial_seed,
    persist_research_stage,
    persist_script_stage,
    persist_topic_stage,
)
from app.editorial.script_compiler import compile_script_packet  # noqa: E402
from app.editorial.topic_intelligence import compile_topic_packet  # noqa: E402
from app.models import Channel, PublishRecord  # noqa: E402
from app.main import app  # noqa: E402
from app.production.budget import load_campaign_budget_policy  # noqa: E402
from app.production.local_visuals import (  # noqa: E402
    LocalVisualRequest,
    render_local_visual,
)
from app.production.media import (  # noqa: E402
    build_tts_plan_requests,
    persist_tts_bytes,
)
from app.production.persistence import (  # noqa: E402
    I5_BUDGET_BLOCK_KIND,
    bind_i5_production_profile,
    claim_metered_dispatch,
    create_budget_override,
    persist_storyboard,
)
from app.production.profile import (  # noqa: E402
    build_production_profile,
    production_workflow_id,
)
from app.production.providers import (  # noqa: E402
    ImageResult,
    ProviderError,
    TTSResult,
)
from app.production.renderer import ToolVersions, capture_tool_versions  # noqa: E402
from app.production.storyboard import compile_storyboard  # noqa: E402
from app.workflows.dbos_runtime import (  # noqa: E402
    launch_dbos_runtime,
    shutdown_dbos_runtime,
)
from tests.db_helpers import reset_migrated_test_database  # noqa: E402
from tests.i4_test_data import happy_demand, happy_seed  # noqa: E402


_AUDIO_DURATION_SECONDS = 0.20
_TEST_API_KEY = "test-only-key"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
requires_media_tools = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="canonical I5 workflow requires local ffmpeg and ffprobe",
)


@dataclass(frozen=True)
class _RuntimeCase:
    settings: Settings
    versions: ToolVersions


@dataclass(frozen=True)
class _SeededCampaign:
    campaign_id: int
    i4_workflow_id: str
    script: dict[str, object]
    script_hash: str


@dataclass
class _ProviderCalls:
    tts_input_hashes: list[str]
    image_count: int = 0


@pytest.fixture
def i5_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_RuntimeCase]:
    shutdown_dbos_runtime()
    reset_migrated_test_database()
    output_dir = tmp_path / "output"
    settings = Settings(
        _env_file=None,
        database_url=str(engine.url),
        dbos_system_database_url=f"sqlite:///{tmp_path / 'test_dbos_i5.db'}",
        openai_api_key=_TEST_API_KEY,
        output_dir=str(output_dir),
    )
    versions = capture_tool_versions()
    monkeypatch.setattr(workflow_module, "get_settings", lambda: settings)
    monkeypatch.setattr(workflow_module, "capture_tool_versions", lambda: versions)

    def refuse_external_network(*_: object, **__: object) -> None:
        raise AssertionError("I5 workflow test attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", refuse_external_network)
    launch_dbos_runtime(settings)
    try:
        yield _RuntimeCase(settings=settings, versions=versions)
    finally:
        shutdown_dbos_runtime()


def _compile_i4_packets(
    campaign_id: int,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    seed = happy_seed()
    topic = compile_topic_packet(
        campaign_id=campaign_id,
        seed_hash=seed.sha256(campaign_id),
        seed=seed,
        demand=happy_demand(),
        channel_niche="AI infrastructure developer tools",
        channel_audience="technical AI operators and developer teams",
        novelty_history=(),
    )
    research = compile_research_packet(
        campaign_id=campaign_id,
        seed=seed,
        topic_packet=topic,
        topic_packet_hash=canonical_sha256(topic),
    )
    script = compile_script_packet(
        campaign_id=campaign_id,
        topic_packet=topic,
        topic_packet_hash=canonical_sha256(topic),
        research_packet=research,
        research_packet_hash=canonical_sha256(research),
    )
    return topic, research, script


def _seed_i4_campaign() -> _SeededCampaign:
    with SessionLocal() as db:
        channel = Channel(
            name="I5 workflow channel",
            niche="AI infrastructure developer tools",
            audience="technical AI operators and developer teams",
            brand_voice="careful technical analysis",
            visual_style="evidence-led diagrams",
        )
        db.add(channel)
        db.flush()
        campaign = Campaign(
            channel_id=channel.id,
            current_stage="topic",
            workflow_id=None,
            production_workflow_id=None,
            risk_tier="standard",
            policy_version=I4_POLICY_VERSION,
        )
        db.add(campaign)
        db.commit()
        campaign_id = campaign.id

    persist_editorial_seed(campaign_id, happy_seed())
    binding = bind_i4_campaign_workflow(campaign_id)
    topic, research, script = _compile_i4_packets(campaign_id)
    persist_topic_stage(topic)
    persist_research_stage(research)
    persist_script_stage(script)
    return _SeededCampaign(
        campaign_id=campaign_id,
        i4_workflow_id=str(binding["workflow_id"]),
        script=script,
        script_hash=canonical_sha256(script),
    )


def _prepare_i5_media(
    seeded: _SeededCampaign,
    runtime: _RuntimeCase,
) -> tuple[str, str]:
    profile = build_production_profile(
        campaign_id=seeded.campaign_id,
        i4_script_sha256=seeded.script_hash,
        settings=runtime.settings,
        budget_policy=load_campaign_budget_policy(),
        ffmpeg_version=runtime.versions.ffmpeg_version,
        ffprobe_version=runtime.versions.ffprobe_version,
    )
    binding = bind_i5_production_profile(seeded.campaign_id, profile)
    packet = compile_storyboard(
        campaign_id=seeded.campaign_id,
        script_packet=seeded.script,
        script_hash=seeded.script_hash,
        production_profile_hash=profile.sha256,
    )
    result = persist_storyboard(packet)
    assert result["outcome"] == "PASS"
    return str(binding["production_workflow_id"]), profile.sha256


def _wav_bytes() -> bytes:
    sample_rate = 48_000
    frame_count = round(sample_rate * _AUDIO_DURATION_SECONDS)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack("<h", 600) * frame_count)
    return buffer.getvalue()


def _install_successful_provider_mocks(
    monkeypatch: pytest.MonkeyPatch,
) -> _ProviderCalls:
    calls = _ProviderCalls(tts_input_hashes=[])
    wav_bytes = _wav_bytes()
    generated_plate = render_local_visual(
        LocalVisualRequest(
            mode="DETERMINISTIC_MOTION_GRAPHIC",
            title="Mocked generated plate",
            scene_position=0,
            visual_purpose="Provide a valid test-only illustrative plate",
        )
    ).png_bytes

    def generate_tts(
        self: object,
        request: object,
        *,
        api_key: str,
    ) -> TTSResult:
        del self
        assert api_key == _TEST_API_KEY
        text = str(request.text)  # type: ignore[attr-defined]
        calls.tts_input_hashes.append(hashlib.sha256(text.encode()).hexdigest())
        return TTSResult(
            audio_bytes=wav_bytes,
            model=str(request.model),  # type: ignore[attr-defined]
            voice=str(request.voice),  # type: ignore[attr-defined]
            response_format=str(request.response_format),  # type: ignore[attr-defined]
            character_count=int(request.character_count),  # type: ignore[attr-defined]
            cost_microunits=int(request.cost_microunits),  # type: ignore[attr-defined]
            reservation_microunits=int(request.reservation_microunits),  # type: ignore[attr-defined]
        )

    def generate_image(
        self: object,
        request: object,
        *,
        api_key: str,
    ) -> ImageResult:
        del self
        assert api_key == _TEST_API_KEY
        calls.image_count += 1
        return ImageResult(
            image_bytes=generated_plate,
            model="gpt-image-2",
            quality=str(request.quality),  # type: ignore[attr-defined]
            size="1280x720",
            output_format="png",
            prompt=str(request.prompt),  # type: ignore[attr-defined]
            reservation_microunits=int(request.reservation_microunits),  # type: ignore[attr-defined]
        )

    monkeypatch.setattr(provider_module.OpenAITTSProvider, "generate", generate_tts)
    monkeypatch.setattr(provider_module.GPTImageProvider, "generate", generate_image)
    return calls


def _forbidden_effect_counts(campaign_id: int) -> tuple[int, int]:
    with SessionLocal() as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(Approval)
                .where(Approval.campaign_id == campaign_id)
            )
            or 0,
            db.scalar(
                select(func.count())
                .select_from(PublishRecord)
                .where(PublishRecord.campaign_id == campaign_id)
            )
            or 0,
        )


@requires_media_tools
def test_i5_happy_path_uses_separate_identity_and_duplicate_start_replays(
    i5_runtime: _RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_successful_provider_mocks(monkeypatch)
    seeded = _seed_i4_campaign()

    first = workflow_module.start_i5_production_workflow(seeded.campaign_id)
    second = workflow_module.start_i5_production_workflow(seeded.campaign_id)

    assert first.get_workflow_id() == second.get_workflow_id()
    result = first.get_result(polling_interval_sec=0.01)
    assert second.get_result(polling_interval_sec=0.01) == result
    assert result["outcome"] == "PASS"
    assert result["final_stage"] == "machine_qa"

    with SessionLocal() as db:
        campaign = db.get(Campaign, seeded.campaign_id)
        assert campaign is not None
        production_id = campaign.production_workflow_id
        assert campaign.current_stage == "machine_qa"
        assert campaign.workflow_id == seeded.i4_workflow_id
        assert production_id == result["production_workflow_id"]
        assert production_id != campaign.workflow_id
        assert production_id == production_workflow_id(
            seeded.campaign_id,
            seeded.script_hash,
            str(result["production_profile_hash"]),
        )
        scene_count = (
            db.scalar(
                select(func.count())
                .select_from(Scene)
                .where(Scene.campaign_id == seeded.campaign_id)
            )
            or 0
        )
        generated_count = (
            db.scalar(
                select(func.count())
                .select_from(Scene)
                .where(
                    Scene.campaign_id == seeded.campaign_id,
                    Scene.visual_mode == "GENERATED_CINEMATIC",
                )
            )
            or 0
        )

    assert len(calls.tts_input_hashes) == scene_count
    assert calls.image_count == generated_count
    assert _forbidden_effect_counts(seeded.campaign_id) == (0, 0)


def test_recovery_reuses_committed_tts_and_marks_in_flight_dispatch_ambiguous(
    i5_runtime: _RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_successful_provider_mocks(monkeypatch)
    seeded = _seed_i4_campaign()
    production_id, _ = _prepare_i5_media(seeded, i5_runtime)
    plan = build_tts_plan_requests(seeded.campaign_id)
    job_ids = [int(value) for value in plan["job_ids"]]
    assert len(job_ids) >= 2

    assert claim_metered_dispatch(job_ids[0])["dispatch"] is True
    committed = persist_tts_bytes(
        job_id=job_ids[0],
        audio_bytes=_wav_bytes(),
        duration_seconds=_AUDIO_DURATION_SECONDS,
        settings=i5_runtime.settings,
    )
    assert committed["status"] == "provider_complete"
    assert claim_metered_dispatch(job_ids[1])["dispatch"] is True

    first = workflow_module.start_i5_production_workflow(seeded.campaign_id)
    result = first.get_result(polling_interval_sec=0.01)
    replay = workflow_module.start_i5_production_workflow(seeded.campaign_id)

    assert first.get_workflow_id() == replay.get_workflow_id() == production_id
    assert replay.get_result(polling_interval_sec=0.01) == result
    assert result["outcome"] == "NEEDS_HUMAN"
    assert result["final_stage"] == "media"
    assert result["reason"] == "required_tts_ambiguous_dispatch"
    assert calls.tts_input_hashes == []
    assert calls.image_count == 0

    with SessionLocal() as db:
        committed_job = db.get(GenerationJob, job_ids[0])
        ambiguous_job = db.get(GenerationJob, job_ids[1])
        campaign = db.get(Campaign, seeded.campaign_id)
        assert committed_job is not None
        assert committed_job.status == "provider_complete"
        assert committed_job.output_artifact_id is not None
        assert committed_job.cost_microunits is not None
        assert ambiguous_job is not None
        assert ambiguous_job.status == "ambiguous_dispatch"
        assert ambiguous_job.output_artifact_id is None
        assert ambiguous_job.cost_microunits is None
        assert ambiguous_job.reserved_cost_microunits is not None
        assert campaign is not None and campaign.current_stage == "media"
    assert _forbidden_effect_counts(seeded.campaign_id) == (0, 0)


def test_budget_block_waits_for_committed_override_message_then_resumes_once(
    i5_runtime: _RuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_policy = load_campaign_budget_policy()
    low_limits = base_policy.limits_microusd.model_copy(
        update={"target": 100, "soft_warning": 200, "default_hard_cap": 300}
    )
    low_warning = base_policy.soft_warning_behavior.model_copy(
        update={"trigger_microusd": 200}
    )
    low_policy = base_policy.model_copy(
        update={
            "limits_microusd": low_limits,
            "soft_warning_behavior": low_warning,
        }
    )
    low_policy = low_policy.model_copy(
        update={"policy_sha256": canonical_sha256(low_policy.canonical_payload())}
    )
    for module in (
        budget_module,
        media_module,
        production_persistence,
        workflow_module,
    ):
        monkeypatch.setattr(module, "load_campaign_budget_policy", lambda: low_policy)

    calls = _install_successful_provider_mocks(monkeypatch)
    seeded = _seed_i4_campaign()
    monkeypatch.setattr(workflow_module, "I5_BUDGET_OVERRIDE_WAIT_SECONDS", 10)
    handle = workflow_module.start_i5_production_workflow(seeded.campaign_id)
    production_id = handle.get_workflow_id()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            blocked = db.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(
                    Artifact.campaign_id == seeded.campaign_id,
                    Artifact.kind == I5_BUDGET_BLOCK_KIND,
                )
            )
        if blocked:
            break
        time.sleep(0.01)
    assert blocked == 1, "workflow did not durably enter the budget wait"
    assert calls.tts_input_hashes == []
    assert calls.image_count == 0

    client = TestClient(app)
    request = {
        "actor": "workflow-test-owner",
        "new_authorized_cap_microusd": 2_000_000,
        "reason": "Explicit test authorization for the canonical production run",
    }
    override_response = client.post(
        f"/campaigns/{seeded.campaign_id}/production/budget/override",
        json=request,
    )
    assert override_response.status_code == 200, override_response.text
    override = override_response.json()
    assert override["created"] is True
    assert override["message_delivered"] is True
    assert override["production_workflow_id"] == production_id

    result = handle.get_result(polling_interval_sec=0.01)

    assert result["outcome"] == "PASS"
    assert result["final_stage"] == "machine_qa"
    assert calls.tts_input_hashes
    assert calls.image_count > 0

    exact_retry = client.post(
        f"/campaigns/{seeded.campaign_id}/production/budget/override",
        json=request,
    )
    assert exact_retry.status_code == 200, exact_retry.text
    assert exact_retry.json()["created"] is False
    assert exact_retry.json()["override_id"] == override["override_id"]
    assert exact_retry.json()["override_hash"] == override["override_hash"]
    assert exact_retry.json()["message_delivered"] is True
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(CampaignBudgetOverride)
                .where(CampaignBudgetOverride.campaign_id == seeded.campaign_id)
            )
            == 1
        )
        budget_gates = list(
            db.scalars(
                select(GateDecision).where(
                    GateDecision.campaign_id == seeded.campaign_id,
                    GateDecision.stage == "media",
                    GateDecision.outcome == "NEEDS_HUMAN",
                )
            )
        )
        assert len(budget_gates) == 1
        campaign = db.get(Campaign, seeded.campaign_id)
        assert campaign is not None and campaign.current_stage == "machine_qa"
    assert _forbidden_effect_counts(seeded.campaign_id) == (0, 0)


def _prepare_profile_hash(campaign_id: int) -> str:
    with SessionLocal() as db:
        profile = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == "i5_production_profile",
            )
        )
        assert profile is not None
        return profile.sha256


def test_i5_workflow_retries_waits_and_provider_dispatch_are_bounded() -> None:
    assert workflow_module.I5_WORKFLOW_MAX_RECOVERY_ATTEMPTS == 3
    assert workflow_module.I5_WORKFLOW_TIMEOUT_SECONDS == 691_200
    assert workflow_module.I5_BUDGET_OVERRIDE_WAIT_SECONDS == 604_800
    assert workflow_module.I5_MAX_BUDGET_OVERRIDE_MESSAGES == 3
    assert workflow_module.I5_SAFE_POST_RETRIES == 0
    assert workflow_module.I5_LOCAL_STEP_MAX_ATTEMPTS == 3
    assert workflow_module.I5_SORA_POLL_INTERVAL_SECONDS == 5
    assert workflow_module.I5_SORA_MAX_POLLS == 120


def _subprocess_environment(
    application_database: Path,
    system_database: Path,
    output_dir: Path,
    marker: Path,
    campaign_id_file: Path,
    provider_log: Path,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": f"sqlite:///{application_database}",
            "DBOS_SYSTEM_DATABASE_URL": f"sqlite:///{system_database}",
            "OUTPUT_DIR": str(output_dir),
            "OPENAI_API_KEY": _TEST_API_KEY,
            "IMAGE_GENERATION_API_KEY": "",
            "I5_RECOVERY_MARKER": str(marker),
            "I5_RECOVERY_CAMPAIGN_ID_FILE": str(campaign_id_file),
            "I5_RECOVERY_PROVIDER_LOG": str(provider_log),
            "YOUTUBE_DATA_API_KEY": "",
            "YOUTUBE_OAUTH_CLIENT_ID": "",
            "YOUTUBE_OAUTH_CLIENT_SECRET": "",
            "YOUTUBE_OAUTH_REFRESH_TOKEN": "",
        }
    )
    return environment


_SUBPROCESS_I4_CAMPAIGN_SETUP = r'''
import os
from pathlib import Path

from app.config import Settings
from app.db import Campaign, SessionLocal
from app.editorial.claims import compile_research_packet
from app.editorial.contracts import I4_POLICY_VERSION, canonical_sha256
from app.editorial.persistence import (
    bind_i4_campaign_workflow,
    persist_editorial_seed,
    persist_research_stage,
    persist_script_stage,
    persist_topic_stage,
)
from app.editorial.script_compiler import compile_script_packet
from app.editorial.topic_intelligence import compile_topic_packet
from app.models import Channel
import app.workflows.i5_production_workflow as workflow
from tests.i4_test_data import happy_demand, happy_seed

campaign_id_file = Path(os.environ["I5_RECOVERY_CAMPAIGN_ID_FILE"])
settings = Settings(_env_file=None)
workflow.get_settings = lambda: settings

with SessionLocal() as db:
    channel = Channel(
        name="I5 subprocess recovery channel",
        niche="AI infrastructure developer tools",
        audience="technical AI operators and developer teams",
        brand_voice="careful technical analysis",
        visual_style="evidence-led diagrams",
    )
    db.add(channel)
    db.flush()
    campaign = Campaign(
        channel_id=channel.id,
        current_stage="topic",
        workflow_id=None,
        production_workflow_id=None,
        risk_tier="standard",
        policy_version=I4_POLICY_VERSION,
    )
    db.add(campaign)
    db.commit()
    campaign_id = campaign.id

seed = happy_seed()
persist_editorial_seed(campaign_id, seed)
bind_i4_campaign_workflow(campaign_id)
topic = compile_topic_packet(
    campaign_id=campaign_id,
    seed_hash=seed.sha256(campaign_id),
    seed=seed,
    demand=happy_demand(),
    channel_niche="AI infrastructure developer tools",
    channel_audience="technical AI operators and developer teams",
    novelty_history=(),
)
research = compile_research_packet(
    campaign_id=campaign_id,
    seed=seed,
    topic_packet=topic,
    topic_packet_hash=canonical_sha256(topic),
)
script = compile_script_packet(
    campaign_id=campaign_id,
    topic_packet=topic,
    topic_packet_hash=canonical_sha256(topic),
    research_packet=research,
    research_packet_hash=canonical_sha256(research),
)
persist_topic_stage(topic)
persist_research_stage(research)
persist_script_stage(script)
campaign_id_file.write_text(str(campaign_id), encoding="utf-8")
'''


@requires_media_tools
def test_real_dbos_restart_does_not_repeat_a_committed_metered_post(
    tmp_path: Path,
) -> None:
    application_database = tmp_path / "test_i5_recovery_app.db"
    system_database = tmp_path / "test_dbos_i5_recovery.db"
    output_dir = tmp_path / "output"
    marker = tmp_path / "provider-result-committed"
    image_marker = tmp_path / "image-result-committed"
    campaign_id_file = tmp_path / "campaign-id"
    provider_log = tmp_path / "provider-posts.log"
    environment = _subprocess_environment(
        application_database,
        system_database,
        output_dir,
        marker,
        campaign_id_file,
        provider_log,
    )
    environment["I5_RECOVERY_IMAGE_MARKER"] = str(image_marker)
    subprocess.run(
        [str(PYTHON), "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    interrupted_code = r'''
import hashlib
import io
import os
from pathlib import Path
import struct
import time
import wave

from app.config import Settings
from app.db import Campaign, SessionLocal
from app.editorial.claims import compile_research_packet
from app.editorial.contracts import I4_POLICY_VERSION, canonical_sha256
from app.editorial.persistence import (
    bind_i4_campaign_workflow,
    persist_editorial_seed,
    persist_research_stage,
    persist_script_stage,
    persist_topic_stage,
)
from app.editorial.script_compiler import compile_script_packet
from app.editorial.topic_intelligence import compile_topic_packet
from app.models import Channel
from app.production.providers import OpenAITTSProvider, TTSResult
from app.workflows.dbos_runtime import launch_dbos_runtime
import app.workflows.i5_production_workflow as workflow
from tests.i4_test_data import happy_demand, happy_seed

marker = Path(os.environ["I5_RECOVERY_MARKER"])
campaign_id_file = Path(os.environ["I5_RECOVERY_CAMPAIGN_ID_FILE"])
provider_log = Path(os.environ["I5_RECOVERY_PROVIDER_LOG"])
settings = Settings(_env_file=None)
workflow.get_settings = lambda: settings

with SessionLocal() as db:
    channel = Channel(
        name="I5 restart workflow channel",
        niche="AI infrastructure developer tools",
        audience="technical AI operators and developer teams",
        brand_voice="careful technical analysis",
        visual_style="evidence-led diagrams",
    )
    db.add(channel)
    db.flush()
    campaign = Campaign(
        channel_id=channel.id,
        current_stage="topic",
        workflow_id=None,
        production_workflow_id=None,
        risk_tier="standard",
        policy_version=I4_POLICY_VERSION,
    )
    db.add(campaign)
    db.commit()
    campaign_id = campaign.id

seed = happy_seed()
persist_editorial_seed(campaign_id, seed)
bind_i4_campaign_workflow(campaign_id)
topic = compile_topic_packet(
    campaign_id=campaign_id,
    seed_hash=seed.sha256(campaign_id),
    seed=seed,
    demand=happy_demand(),
    channel_niche="AI infrastructure developer tools",
    channel_audience="technical AI operators and developer teams",
    novelty_history=(),
)
research = compile_research_packet(
    campaign_id=campaign_id,
    seed=seed,
    topic_packet=topic,
    topic_packet_hash=canonical_sha256(topic),
)
script = compile_script_packet(
    campaign_id=campaign_id,
    topic_packet=topic,
    topic_packet_hash=canonical_sha256(topic),
    research_packet=research,
    research_packet_hash=canonical_sha256(research),
)
persist_topic_stage(topic)
persist_research_stage(research)
persist_script_stage(script)
campaign_id_file.write_text(str(campaign_id), encoding="utf-8")

buffer = io.BytesIO()
with wave.open(buffer, "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(48000)
    output.writeframes(struct.pack("<h", 600) * 9600)
wav_bytes = buffer.getvalue()

def generate(self, request, *, api_key):
    del self
    assert api_key == "test-only-key"
    digest = hashlib.sha256(request.text.encode()).hexdigest()
    with provider_log.open("a", encoding="utf-8") as handle:
        handle.write("tts:" + digest + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return TTSResult(
        audio_bytes=wav_bytes,
        model=request.model,
        voice=request.voice,
        response_format=request.response_format,
        character_count=request.character_count,
        cost_microunits=request.cost_microunits,
        reservation_microunits=request.reservation_microunits,
    )

OpenAITTSProvider.generate = generate
original_produce_tts = workflow.produce_tts_job

def commit_then_block(*args, **kwargs):
    result = original_produce_tts(*args, **kwargs)
    marker.write_text("committed", encoding="utf-8")
    while True:
        time.sleep(0.05)

workflow.produce_tts_job = commit_then_block
launch_dbos_runtime(settings)
workflow.start_i5_production_workflow(campaign_id)
while True:
    time.sleep(1)
'''
    process = subprocess.Popen(
        [str(PYTHON), "-c", interrupted_code],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 25
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        assert marker.exists(), (
            "I5 workflow never reached the provider-commit-before-checkpoint barrier"
        )
        assert len(provider_log.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    campaign_id = int(campaign_id_file.read_text(encoding="utf-8"))
    connection = sqlite3.connect(application_database)
    try:
        first_job = connection.execute(
            "SELECT id, status, output_artifact_id FROM generation_jobs "
            "WHERE campaign_id = ? AND provider = 'openai_tts' ORDER BY id LIMIT 1",
            (campaign_id,),
        ).fetchone()
        assert first_job is not None
        assert first_job[1] == "provider_complete"
        assert first_job[2] is not None
        production_id = connection.execute(
            "SELECT production_workflow_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()[0]
    finally:
        connection.close()


    image_interrupted_code = r'''
import hashlib
import io
import os
from pathlib import Path
import struct
import time
import wave

from dbos import DBOS
from app.config import Settings
from app.db import Campaign, SessionLocal
from app.production.local_visuals import LocalVisualRequest, render_local_visual
from app.production.providers import (
    GPTImageProvider,
    ImageResult,
    OpenAITTSProvider,
    TTSResult,
)
from app.workflows.dbos_runtime import launch_dbos_runtime
import app.workflows.i5_production_workflow as workflow

provider_log = Path(os.environ["I5_RECOVERY_PROVIDER_LOG"])
image_marker = Path(os.environ["I5_RECOVERY_IMAGE_MARKER"])
campaign_id = int(Path(os.environ["I5_RECOVERY_CAMPAIGN_ID_FILE"]).read_text())
settings = Settings(_env_file=None)
workflow.get_settings = lambda: settings

buffer = io.BytesIO()
with wave.open(buffer, "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(48000)
    output.writeframes(struct.pack("<h", 600) * 9600)
wav_bytes = buffer.getvalue()
plate = render_local_visual(
    LocalVisualRequest(
        mode="DETERMINISTIC_MOTION_GRAPHIC",
        title="Restart-safe generated plate",
        scene_position=0,
        visual_purpose="Valid illustrative provider result for restart testing",
    )
).png_bytes

def write_log(kind, value):
    with provider_log.open("a", encoding="utf-8") as handle:
        handle.write(kind + ":" + hashlib.sha256(value.encode()).hexdigest() + "\n")
        handle.flush()
        os.fsync(handle.fileno())

def generate_tts(self, request, *, api_key):
    del self
    assert api_key == "test-only-key"
    write_log("tts", request.text)
    return TTSResult(
        audio_bytes=wav_bytes,
        model=request.model,
        voice=request.voice,
        response_format=request.response_format,
        character_count=request.character_count,
        cost_microunits=request.cost_microunits,
        reservation_microunits=request.reservation_microunits,
    )

def generate_image(self, request, *, api_key):
    del self
    assert api_key == "test-only-key"
    write_log("image", request.prompt)
    return ImageResult(
        image_bytes=plate,
        model="gpt-image-2",
        quality=request.quality,
        size="1280x720",
        output_format="png",
        prompt=request.prompt,
        reservation_microunits=request.reservation_microunits,
    )

OpenAITTSProvider.generate = generate_tts
GPTImageProvider.generate = generate_image
original_produce_visual = workflow.produce_scene_visual

def commit_image_then_block(*args, **kwargs):
    result = original_produce_visual(*args, **kwargs)
    if result.get("status") == "provider_complete":
        image_marker.write_text("committed", encoding="utf-8")
        while True:
            time.sleep(0.05)
    return result

workflow.produce_scene_visual = commit_image_then_block
with SessionLocal() as db:
    workflow_id = db.get(Campaign, campaign_id).production_workflow_id
launch_dbos_runtime(settings)
DBOS.retrieve_workflow(workflow_id).get_result(polling_interval_sec=0.01)
'''
    image_process = subprocess.Popen(
        [str(PYTHON), "-c", image_interrupted_code],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not image_marker.exists() and time.monotonic() < deadline:
            if image_process.poll() is not None:
                break
            time.sleep(0.01)
        assert image_marker.exists(), (
            "Recovered I5 workflow never reached the image-commit checkpoint barrier"
        )
    finally:
        if image_process.poll() is None:
            image_process.kill()
        image_process.wait(timeout=10)

    posts_after_image_commit = provider_log.read_text(encoding="utf-8").splitlines()
    first_tts_post = posts_after_image_commit[0]
    first_image_post = next(
        line for line in posts_after_image_commit if line.startswith("image:")
    )
    assert posts_after_image_commit.count(first_tts_post) == 1
    assert posts_after_image_commit.count(first_image_post) == 1

    connection = sqlite3.connect(application_database)
    try:
        first_image_job = connection.execute(
            "SELECT id, status, output_artifact_id FROM generation_jobs "
            "WHERE campaign_id = ? AND provider = 'openai_image' ORDER BY id LIMIT 1",
            (campaign_id,),
        ).fetchone()
        assert first_image_job is not None
        assert first_image_job[1] == "provider_complete"
        assert first_image_job[2] is not None
    finally:
        connection.close()

    final_recovery_code = r'''
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import wave

from dbos import DBOS
from app.config import Settings
from app.db import Campaign, SessionLocal
from app.production.local_visuals import LocalVisualRequest, render_local_visual
from app.production.providers import (
    GPTImageProvider,
    ImageResult,
    OpenAITTSProvider,
    TTSResult,
)
from app.workflows.dbos_runtime import launch_dbos_runtime, shutdown_dbos_runtime
import app.workflows.i5_production_workflow as workflow

provider_log = Path(os.environ["I5_RECOVERY_PROVIDER_LOG"])
campaign_id = int(Path(os.environ["I5_RECOVERY_CAMPAIGN_ID_FILE"]).read_text())
settings = Settings(_env_file=None)
workflow.get_settings = lambda: settings

buffer = io.BytesIO()
with wave.open(buffer, "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(48000)
    output.writeframes(struct.pack("<h", 600) * 9600)
wav_bytes = buffer.getvalue()
plate = render_local_visual(
    LocalVisualRequest(
        mode="DETERMINISTIC_MOTION_GRAPHIC",
        title="Restart-safe generated plate",
        scene_position=0,
        visual_purpose="Valid illustrative provider result for restart testing",
    )
).png_bytes

def write_log(kind, value):
    with provider_log.open("a", encoding="utf-8") as handle:
        handle.write(kind + ":" + hashlib.sha256(value.encode()).hexdigest() + "\n")
        handle.flush()
        os.fsync(handle.fileno())

def generate_tts(self, request, *, api_key):
    del self
    assert api_key == "test-only-key"
    write_log("tts", request.text)
    return TTSResult(
        audio_bytes=wav_bytes,
        model=request.model,
        voice=request.voice,
        response_format=request.response_format,
        character_count=request.character_count,
        cost_microunits=request.cost_microunits,
        reservation_microunits=request.reservation_microunits,
    )

def generate_image(self, request, *, api_key):
    del self
    assert api_key == "test-only-key"
    write_log("image", request.prompt)
    return ImageResult(
        image_bytes=plate,
        model="gpt-image-2",
        quality=request.quality,
        size="1280x720",
        output_format="png",
        prompt=request.prompt,
        reservation_microunits=request.reservation_microunits,
    )

OpenAITTSProvider.generate = generate_tts
GPTImageProvider.generate = generate_image
with SessionLocal() as db:
    workflow_id = db.get(Campaign, campaign_id).production_workflow_id
launch_dbos_runtime(settings)
try:
    result = DBOS.retrieve_workflow(workflow_id).get_result(polling_interval_sec=0.01)
    print("I5_RECOVERY_RESULT=" + json.dumps(result, sort_keys=True))
finally:
    shutdown_dbos_runtime()
'''
    recovered = subprocess.run(
        [str(PYTHON), "-c", final_recovery_code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    recovery_line = next(
        line
        for line in recovered.stdout.splitlines()
        if line.startswith("I5_RECOVERY_RESULT=")
    )
    result = json.loads(recovery_line.split("=", 1)[1])
    provider_posts = provider_log.read_text(encoding="utf-8").splitlines()

    assert result["outcome"] == "PASS"
    assert result["final_stage"] == "machine_qa"
    assert provider_posts.count(first_tts_post) == 1
    assert provider_posts.count(first_image_post) == 1

    connection = sqlite3.connect(application_database)
    try:
        scene_count = connection.execute(
            "SELECT COUNT(*) FROM scenes WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0]
        generated_count = connection.execute(
            "SELECT COUNT(*) FROM scenes "
            "WHERE campaign_id = ? AND visual_mode = 'GENERATED_CINEMATIC'",
            (campaign_id,),
        ).fetchone()[0]
        assert len([line for line in provider_posts if line.startswith("tts:")]) == scene_count
        assert len([line for line in provider_posts if line.startswith("image:")]) == generated_count
        assert connection.execute(
            "SELECT status, output_artifact_id FROM generation_jobs WHERE id = ?",
            (first_job[0],),
        ).fetchone() == ("provider_complete", first_job[2])
        assert connection.execute(
            "SELECT status, output_artifact_id FROM generation_jobs WHERE id = ?",
            (first_image_job[0],),
        ).fetchone() == ("provider_complete", first_image_job[2])
        assert connection.execute(
            "SELECT current_stage, production_workflow_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone() == ("machine_qa", production_id)
        assert connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM publish_records WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


@requires_media_tools
def test_real_dbos_restart_marks_pre_post_dispatch_ambiguous_without_provider_call(
    tmp_path: Path,
) -> None:
    application_database = tmp_path / "test_i5_ambiguous_app.db"
    system_database = tmp_path / "test_i5_ambiguous_dbos.db"
    output_dir = tmp_path / "output"
    marker = tmp_path / "dispatch-claimed"
    campaign_id_file = tmp_path / "campaign-id"
    provider_log = tmp_path / "provider-posts.log"
    environment = _subprocess_environment(
        application_database,
        system_database,
        output_dir,
        marker,
        campaign_id_file,
        provider_log,
    )
    subprocess.run(
        [str(PYTHON), "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    interrupted_code = _SUBPROCESS_I4_CAMPAIGN_SETUP + r'''
import time

from app.production.persistence import claim_metered_dispatch
from app.workflows.dbos_runtime import launch_dbos_runtime

marker = Path(os.environ["I5_RECOVERY_MARKER"])

def claim_then_block(*, job_id, api_key, settings):
    del api_key, settings
    claimed = claim_metered_dispatch(job_id)
    assert claimed["dispatch"] is True
    marker.write_text(str(job_id), encoding="utf-8")
    while True:
        time.sleep(0.05)

workflow.produce_tts_job = claim_then_block
launch_dbos_runtime(settings)
workflow.start_i5_production_workflow(campaign_id)
while True:
    time.sleep(1)
'''
    process = subprocess.Popen(
        [str(PYTHON), "-c", interrupted_code],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 40
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        assert marker.exists(), "I5 workflow never committed its dispatching claim"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    campaign_id = int(campaign_id_file.read_text(encoding="utf-8"))
    claimed_job_id = int(marker.read_text(encoding="utf-8"))
    connection = sqlite3.connect(application_database)
    try:
        claimed_before = connection.execute(
            "SELECT status, reserved_cost_microunits, output_artifact_id "
            "FROM generation_jobs WHERE id = ?",
            (claimed_job_id,),
        ).fetchone()
        assert claimed_before is not None
        assert claimed_before[0] == "dispatching"
        assert claimed_before[1] > 0
        assert claimed_before[2] is None
        production_id = connection.execute(
            "SELECT production_workflow_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()[0]
    finally:
        connection.close()

    recovery_code = r'''
import json
import os
from pathlib import Path

from dbos import DBOS
from app.config import Settings
from app.db import Campaign, SessionLocal
from app.production.providers import OpenAITTSProvider
from app.workflows.dbos_runtime import launch_dbos_runtime, shutdown_dbos_runtime
import app.workflows.i5_production_workflow as workflow

provider_log = Path(os.environ["I5_RECOVERY_PROVIDER_LOG"])
campaign_id = int(Path(os.environ["I5_RECOVERY_CAMPAIGN_ID_FILE"]).read_text())
settings = Settings(_env_file=None)
workflow.get_settings = lambda: settings

def forbidden_post(self, request, *, api_key):
    del self, request, api_key
    provider_log.write_text("unexpected-provider-post", encoding="utf-8")
    raise AssertionError("ambiguous dispatch must never POST again")

OpenAITTSProvider.generate = forbidden_post
with SessionLocal() as db:
    workflow_id = db.get(Campaign, campaign_id).production_workflow_id
launch_dbos_runtime(settings)
try:
    result = DBOS.retrieve_workflow(workflow_id).get_result(polling_interval_sec=0.01)
    print("I5_AMBIGUOUS_RESULT=" + json.dumps(result, sort_keys=True))
finally:
    shutdown_dbos_runtime()
'''
    recovered = subprocess.run(
        [str(PYTHON), "-c", recovery_code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result_line = next(
        line
        for line in recovered.stdout.splitlines()
        if line.startswith("I5_AMBIGUOUS_RESULT=")
    )
    result = json.loads(result_line.split("=", 1)[1])
    assert result["outcome"] == "NEEDS_HUMAN"
    assert result["final_stage"] == "media"
    assert result["reason"] == "required_tts_ambiguous_dispatch"
    assert not provider_log.exists()

    connection = sqlite3.connect(application_database)
    try:
        claimed_after = connection.execute(
            "SELECT status, reserved_cost_microunits, cost_microunits, "
            "output_artifact_id FROM generation_jobs WHERE id = ?",
            (claimed_job_id,),
        ).fetchone()
        assert claimed_after == (
            "ambiguous_dispatch",
            claimed_before[1],
            None,
            None,
        )
        assert connection.execute(
            "SELECT current_stage, production_workflow_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone() == ("media", production_id)
        assert connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM publish_records WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


@requires_media_tools
def test_real_dbos_restart_polls_persisted_sora_job_without_recreate(
    tmp_path: Path,
) -> None:
    application_database = tmp_path / "test_i5_sora_recovery_app.db"
    system_database = tmp_path / "test_i5_sora_recovery_dbos.db"
    output_dir = tmp_path / "output"
    marker = tmp_path / "sora-submitted"
    campaign_id_file = tmp_path / "campaign-id"
    provider_log = tmp_path / "sora-provider-calls.log"
    environment = _subprocess_environment(
        application_database,
        system_database,
        output_dir,
        marker,
        campaign_id_file,
        provider_log,
    )
    environment.update(
        {
            "I5_ALLOW_DEPRECATED_SORA": "true",
            "I5_VIDEO_PROVIDER": "openai",
        }
    )
    subprocess.run(
        [str(PYTHON), "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    interrupted_code = _SUBPROCESS_I4_CAMPAIGN_SETUP + r'''
import io
import struct
import time
import wave

from app.production.providers import (
    OpenAITTSProvider,
    SoraJob,
    SoraProvider,
    TTSResult,
)
from app.workflows.dbos_runtime import launch_dbos_runtime

marker = Path(os.environ["I5_RECOVERY_MARKER"])
provider_log = Path(os.environ["I5_RECOVERY_PROVIDER_LOG"])
provider_job_id = "video_i5_restart_1"

buffer = io.BytesIO()
with wave.open(buffer, "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(48000)
    output.writeframes(struct.pack("<h", 600) * 9600)
wav_bytes = buffer.getvalue()

def generate_tts(self, request, *, api_key):
    del self
    assert api_key == "test-only-key"
    return TTSResult(
        audio_bytes=wav_bytes,
        model=request.model,
        voice=request.voice,
        response_format=request.response_format,
        character_count=request.character_count,
        cost_microunits=request.cost_microunits,
        reservation_microunits=request.reservation_microunits,
    )

def create_sora(self, request, *, api_key):
    del self, request
    assert api_key == "test-only-key"
    with provider_log.open("a", encoding="utf-8") as handle:
        handle.write("create:" + provider_job_id + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return SoraJob(provider_job_id=provider_job_id, status="queued")

OpenAITTSProvider.generate = generate_tts
SoraProvider.create = create_sora
original_create_sora_job = workflow.create_sora_job

def persist_provider_id_then_block(*, job_id, api_key):
    result = original_create_sora_job(job_id=job_id, api_key=api_key)
    assert result["status"] == "submitted"
    assert result["provider_job_id"] == provider_job_id
    marker.write_text(provider_job_id, encoding="utf-8")
    while True:
        time.sleep(0.05)

workflow.create_sora_job = persist_provider_id_then_block
launch_dbos_runtime(settings)
workflow.start_i5_production_workflow(campaign_id)
while True:
    time.sleep(1)
'''
    process = subprocess.Popen(
        [str(PYTHON), "-c", interrupted_code],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        assert marker.exists(), (
            "I5 workflow never persisted the Sora provider ID before checkpoint"
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    provider_job_id = marker.read_text(encoding="utf-8")
    assert provider_log.read_text(encoding="utf-8").splitlines() == [
        f"create:{provider_job_id}"
    ]
    campaign_id = int(campaign_id_file.read_text(encoding="utf-8"))
    connection = sqlite3.connect(application_database)
    try:
        submitted_job = connection.execute(
            "SELECT id, scene_id, status, provider_job_id, "
            "reserved_cost_microunits, cost_microunits, output_artifact_id "
            "FROM generation_jobs WHERE campaign_id = ? "
            "AND provider = 'openai_video'",
            (campaign_id,),
        ).fetchone()
        assert submitted_job is not None
        assert submitted_job[2:] == (
            "submitted",
            provider_job_id,
            960_000,
            None,
            None,
        )
        production_id = connection.execute(
            "SELECT production_workflow_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()[0]
        assert connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM publish_records WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()

    recovery_code = r'''
import json
import os
from pathlib import Path

from dbos import DBOS
from sqlalchemy import select

from app.config import Settings
from app.db import Campaign, GenerationJob, Scene, SessionLocal
from app.production.media import scene_contract
from app.production.providers import (
    OpenAITTSProvider,
    ProviderError,
    SoraJob,
    SoraProvider,
)
from app.workflows.dbos_runtime import launch_dbos_runtime, shutdown_dbos_runtime
import app.workflows.i5_production_workflow as workflow

provider_log = Path(os.environ["I5_RECOVERY_PROVIDER_LOG"])
campaign_id = int(Path(os.environ["I5_RECOVERY_CAMPAIGN_ID_FILE"]).read_text())
settings = Settings(_env_file=None)
workflow.get_settings = lambda: settings

with SessionLocal() as db:
    submitted_job = db.scalar(
        select(GenerationJob).where(
            GenerationJob.campaign_id == campaign_id,
            GenerationJob.provider == "openai_video",
            GenerationJob.status == "submitted",
        )
    )
    assert submitted_job is not None
    submitted_scene_id = submitted_job.scene_id
    submitted_provider_job_id = submitted_job.provider_job_id
    workflow_id = db.get(Campaign, campaign_id).production_workflow_id

def record(kind, value):
    with provider_log.open("a", encoding="utf-8") as handle:
        handle.write(kind + ":" + value + "\n")
        handle.flush()
        os.fsync(handle.fileno())

def forbidden_tts(self, request, *, api_key):
    del self, request, api_key
    record("unexpected_tts", "called")
    raise AssertionError("committed TTS must not be dispatched during recovery")

def forbidden_create(self, request, *, api_key):
    del self, request, api_key
    record("recreate", "called")
    raise AssertionError("submitted Sora job must never be recreated")

def poll_once(self, provider_job_id, *, api_key):
    del self
    assert api_key == "test-only-key"
    assert provider_job_id == submitted_provider_job_id
    record("poll", provider_job_id)
    return SoraJob(provider_job_id=provider_job_id, status="completed")

def download(self, provider_job_id, *, api_key):
    del self
    assert api_key == "test-only-key"
    assert provider_job_id == submitted_provider_job_id
    record("download", provider_job_id)
    raise ProviderError("forced download failure exercises declared local fallback")

OpenAITTSProvider.generate = forbidden_tts
SoraProvider.create = forbidden_create
SoraProvider.poll_once = poll_once
SoraProvider.download = download
original_produce_scene_visual = workflow.produce_scene_visual

def isolate_submitted_sora_scene(*, campaign_id, scene_id, api_key, settings):
    if scene_id != submitted_scene_id:
        with SessionLocal() as db:
            scene = db.get(Scene, scene_id)
            assert scene is not None
            primary = dict(scene_contract(scene).get("primary_fulfillment") or {})
        if primary.get("strategy") == "generated_video_primary":
            return workflow.render_and_persist_local_visual(
                campaign_id=campaign_id,
                scene_id=scene_id,
                settings=settings,
                fallback=True,
            )
    return original_produce_scene_visual(
        campaign_id=campaign_id,
        scene_id=scene_id,
        api_key=api_key,
        settings=settings,
    )

workflow.produce_scene_visual = isolate_submitted_sora_scene
launch_dbos_runtime(settings)
try:
    result = DBOS.retrieve_workflow(workflow_id).get_result(polling_interval_sec=0.01)
    print("I5_SORA_RECOVERY_RESULT=" + json.dumps(result, sort_keys=True))
finally:
    shutdown_dbos_runtime()
'''
    recovered = subprocess.run(
        [str(PYTHON), "-c", recovery_code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    recovery_line = next(
        line
        for line in recovered.stdout.splitlines()
        if line.startswith("I5_SORA_RECOVERY_RESULT=")
    )
    result = json.loads(recovery_line.split("=", 1)[1])
    provider_calls = provider_log.read_text(encoding="utf-8").splitlines()

    assert result["outcome"] == "PASS"
    assert result["final_stage"] == "machine_qa"
    assert provider_calls == [
        f"create:{provider_job_id}",
        f"poll:{provider_job_id}",
        f"download:{provider_job_id}",
    ]

    connection = sqlite3.connect(application_database)
    try:
        failed_job = connection.execute(
            "SELECT status, provider_job_id, reserved_cost_microunits, "
            "cost_microunits, output_artifact_id, error_json FROM generation_jobs "
            "WHERE id = ?",
            (submitted_job[0],),
        ).fetchone()
        assert failed_job is not None
        assert failed_job[:5] == (
            "provider_failed",
            provider_job_id,
            960_000,
            None,
            None,
        )
        assert json.loads(failed_job[5]) == {
            "code": "sora_download_or_validation_failed",
            "retryable_post": False,
        }
        assert connection.execute(
            "SELECT jobs.status, artifacts.provider_name, artifacts.mime_type "
            "FROM generation_jobs AS jobs JOIN artifacts "
            "ON artifacts.id = jobs.output_artifact_id "
            "WHERE jobs.scene_id = ? AND jobs.provider = 'deterministic_local'",
            (submitted_job[1],),
        ).fetchone() == ("completed", "deterministic_local", "image/png")
        assert connection.execute(
            "SELECT current_stage, production_workflow_id FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone() == ("machine_qa", production_id)
        assert connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM publish_records WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()
