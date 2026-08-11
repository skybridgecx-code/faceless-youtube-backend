from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import struct
from typing import Iterator
import wave

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import app.editorial.persistence as editorial_persistence
import app.production.media as media_module
import app.production.persistence as production_persistence
import app.production.providers as provider_module
from app.config import Settings
from app.db import Artifact, Campaign, GateDecision, GenerationJob, Scene
from app.editorial.claims import compile_research_packet
from app.editorial.contracts import (
    I4_POLICY_VERSION,
    canonical_json,
    canonical_sha256,
)
from app.editorial.script_compiler import compile_script_packet
from app.editorial.topic_intelligence import compile_topic_packet
from app.models import Channel
from app.production.budget import load_campaign_budget_policy
from app.production.local_visuals import (
    LocalVisualRequest,
    png_dimensions,
    render_local_visual,
)
from app.production.media import (
    CanonicalMediaStore,
    build_media_manifest,
    build_tts_plan_requests,
    create_full_voiceover,
    load_tts_job_input,
    persist_tts_bytes,
    produce_tts_job,
    produce_scene_visual,
    render_and_persist_local_visual,
    scene_contract,
)
from app.production.persistence import (
    I5_MEDIA_MANIFEST_KIND,
    I5_MEDIA_PLAN_KIND,
    claim_metered_dispatch,
    persist_media_manifest,
    reconcile_media_effect_set,
)
from app.production.profile import build_production_profile
from app.production.providers import ImageResult, ProviderError
from app.production.renderer import validate_wav
from app.production.storyboard import compile_storyboard
from app.workflows.persistence import WorkflowReplayConflict
from scripts.migrate_db import upgrade_database
from tests.i4_test_data import happy_demand, happy_seed


_AUDIO_DURATION_SECONDS = 0.05


@dataclass(frozen=True)
class _CampaignState:
    campaign_id: int
    generated_scene_ids: tuple[int, ...]
    profile_hash: str
    scene_ids: tuple[int, ...]
    storyboard_hash: str
    tts_job_ids: tuple[int, ...]


@dataclass(frozen=True)
class _Snapshot:
    database: Path
    output_dir: Path
    state: _CampaignState


@dataclass(frozen=True)
class _Snapshots:
    complete: _Snapshot
    planned: _Snapshot
    scene_media: _Snapshot


@dataclass(frozen=True)
class _MediaCase:
    engine: Engine
    sessions: sessionmaker[Session]
    settings: Settings
    state: _CampaignState


@pytest.fixture(autouse=True)
def refuse_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("I5 media tests attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _sessions(database: Path) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 10},
        future=True,
        poolclass=NullPool,
    )
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


def _wire_sessions(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
) -> None:
    monkeypatch.setattr(editorial_persistence, "SessionLocal", sessions)
    monkeypatch.setattr(production_persistence, "SessionLocal", sessions)
    monkeypatch.setattr(media_module, "SessionLocal", sessions)


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


def _seed_planned_campaign(
    sessions: sessionmaker[Session],
    settings: Settings,
) -> _CampaignState:
    with sessions() as db:
        channel = Channel(
            name="I5 media integration channel",
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
    editorial_persistence.persist_editorial_seed(campaign_id, seed)
    editorial_persistence.bind_i4_campaign_workflow(campaign_id)
    topic, research, script = _compile_i4_packets(campaign_id)
    editorial_persistence.persist_topic_stage(topic)
    editorial_persistence.persist_research_stage(research)
    editorial_persistence.persist_script_stage(script)

    script_hash = canonical_sha256(script)
    profile = build_production_profile(
        campaign_id=campaign_id,
        i4_script_sha256=script_hash,
        settings=settings,
        budget_policy=load_campaign_budget_policy(),
        ffmpeg_version="ffmpeg version test-only",
        ffprobe_version="ffprobe version test-only",
    )
    production_persistence.bind_i5_production_profile(campaign_id, profile)
    storyboard = compile_storyboard(
        campaign_id=campaign_id,
        script_packet=script,
        script_hash=script_hash,
        production_profile_hash=profile.sha256,
    )
    storyboard_result = production_persistence.persist_storyboard(storyboard)
    assert storyboard_result["outcome"] == "PASS"

    tts_plan = build_tts_plan_requests(campaign_id)
    assert tts_plan["blocked"] is False
    with sessions() as db:
        scenes = list(
            db.scalars(
                select(Scene)
                .where(Scene.campaign_id == campaign_id)
                .order_by(Scene.position)
            )
        )
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None and campaign.current_stage == "media"
    generated = tuple(
        scene.id for scene in scenes if scene.visual_mode == "GENERATED_CINEMATIC"
    )
    assert len(generated) >= 2
    return _CampaignState(
        campaign_id=campaign_id,
        generated_scene_ids=generated,
        profile_hash=profile.sha256,
        scene_ids=tuple(scene.id for scene in scenes),
        storyboard_hash=str(storyboard_result["output_hash"]),
        tts_job_ids=tuple(int(value) for value in tts_plan["job_ids"]),
    )


def _wav_bytes(position: int) -> bytes:
    sample_rate = 48_000
    frame_count = int(sample_rate * _AUDIO_DURATION_SECONDS)
    amplitude = 100 + (position % 100)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack("<h", amplitude) * frame_count)
    return buffer.getvalue()


def _generated_image_result(request: object) -> ImageResult:
    typed_request = request
    local = render_local_visual(
        LocalVisualRequest(
            mode="DETERMINISTIC_MOTION_GRAPHIC",
            title="Mocked generated plate",
            scene_position=0,
            visual_purpose="Provide a valid test-only illustrative plate",
        )
    )
    return ImageResult(
        image_bytes=local.png_bytes,
        model="gpt-image-2",
        quality=str(typed_request.quality),  # type: ignore[attr-defined]
        size="1280x720",
        output_format="png",
        prompt=str(typed_request.prompt),  # type: ignore[attr-defined]
        reservation_microunits=int(typed_request.reservation_microunits),  # type: ignore[attr-defined]
    )


def _complete_tts(state: _CampaignState, settings: Settings) -> None:
    for job_id in state.tts_job_ids:
        request = load_tts_job_input(job_id)
        assert request["voice"] == "onyx"
        dispatch = claim_metered_dispatch(job_id)
        assert dispatch["dispatch"] is True
        persist_tts_bytes(
            job_id=job_id,
            audio_bytes=_wav_bytes(int(request["scene_position"])),
            duration_seconds=_AUDIO_DURATION_SECONDS,
            settings=settings,
        )


def _complete_scene_media(
    state: _CampaignState,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _complete_tts(state, settings)

    generated_call_count = 0

    def generate(
        self: object,
        request: object,
        *,
        api_key: str,
    ) -> ImageResult:
        nonlocal generated_call_count
        assert api_key == "test-only-key"
        generated_call_count += 1
        if generated_call_count == 2:
            raise ProviderError("sanitized test-only provider failure")
        return _generated_image_result(request)

    monkeypatch.setattr(provider_module.GPTImageProvider, "generate", generate)
    for scene_id in state.scene_ids:
        produce_scene_visual(
            campaign_id=state.campaign_id,
            scene_id=scene_id,
            api_key="test-only-key",
            settings=settings,
        )
    assert generated_call_count == 2


def _copy_snapshot(source: _Snapshot, root: Path, label: str) -> _Snapshot:
    database = root / f"test_i5_media_{label}.db"
    output = root / f"{label}_output"
    shutil.copy2(source.database, database)
    shutil.copytree(source.output_dir, output)
    return _Snapshot(database=database, output_dir=output, state=source.state)


@pytest.fixture(scope="module")
def prepared_snapshots(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Snapshots]:
    root = tmp_path_factory.mktemp("i5_media_snapshots")
    planned_database = root / "planned.db"
    planned_output = root / "planned_output"
    planned_output.mkdir()
    upgrade_database(f"sqlite:///{planned_database}")

    patcher = pytest.MonkeyPatch()
    planned_engine, planned_sessions = _sessions(planned_database)
    _wire_sessions(patcher, planned_sessions)
    planned_settings = Settings(
        _env_file=None,
        openai_api_key="test-only-key",
        output_dir=str(planned_output),
    )
    state = _seed_planned_campaign(planned_sessions, planned_settings)
    planned_engine.dispose()
    planned = _Snapshot(planned_database, planned_output, state)

    scene_media = _copy_snapshot(planned, root, "scene_media_snapshot")
    scene_engine, scene_sessions = _sessions(scene_media.database)
    _wire_sessions(patcher, scene_sessions)
    scene_settings = Settings(
        _env_file=None,
        openai_api_key="test-only-key",
        output_dir=str(scene_media.output_dir),
    )
    _complete_scene_media(state, scene_settings, patcher)
    scene_engine.dispose()

    complete = _copy_snapshot(scene_media, root, "complete_snapshot")
    complete_engine, complete_sessions = _sessions(complete.database)
    _wire_sessions(patcher, complete_sessions)
    complete_settings = Settings(
        _env_file=None,
        openai_api_key="test-only-key",
        output_dir=str(complete.output_dir),
    )
    voiceover = create_full_voiceover(state.campaign_id, complete_settings)
    assert voiceover["replayed"] is False
    packet = build_media_manifest(state.campaign_id, complete_settings)
    assert packet["gate"] == {
        "outcome": "PASS",
        "reasons": ["all_scene_media_and_budget_integrity_checks_passed"],
    }
    complete_engine.dispose()

    try:
        yield _Snapshots(complete=complete, planned=planned, scene_media=scene_media)
    finally:
        patcher.undo()


def _case_from_snapshot(
    source: _Snapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> _MediaCase:
    snapshot = _copy_snapshot(source, tmp_path, label)
    engine, sessions = _sessions(snapshot.database)
    _wire_sessions(monkeypatch, sessions)
    settings = Settings(
        _env_file=None,
        openai_api_key="test-only-key",
        output_dir=str(snapshot.output_dir),
    )
    return _MediaCase(
        engine=engine,
        sessions=sessions,
        settings=settings,
        state=snapshot.state,
    )


@pytest.fixture
def planned_case(
    prepared_snapshots: _Snapshots,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_MediaCase]:
    case = _case_from_snapshot(
        prepared_snapshots.planned,
        tmp_path,
        monkeypatch,
        "planned_case",
    )
    try:
        yield case
    finally:
        case.engine.dispose()


@pytest.fixture
def scene_media_case(
    prepared_snapshots: _Snapshots,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_MediaCase]:
    case = _case_from_snapshot(
        prepared_snapshots.scene_media,
        tmp_path,
        monkeypatch,
        "scene_media_case",
    )
    try:
        yield case
    finally:
        case.engine.dispose()


@pytest.fixture
def completed_case(
    prepared_snapshots: _Snapshots,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_MediaCase]:
    case = _case_from_snapshot(
        prepared_snapshots.complete,
        tmp_path,
        monkeypatch,
        "completed_case",
    )
    try:
        yield case
    finally:
        case.engine.dispose()


def _artifact(case: _MediaCase, kind: str) -> Artifact:
    with case.sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == case.state.campaign_id,
                Artifact.kind == kind,
            )
        )
        assert artifact is not None
        db.expunge(artifact)
        return artifact


def _scene(case: _MediaCase, scene_id: int) -> Scene:
    with case.sessions() as db:
        scene = db.get(Scene, scene_id)
        assert scene is not None
        db.expunge(scene)
        return scene


def _effect_counts(case: _MediaCase) -> tuple[int, int, int]:
    with case.sessions() as db:
        campaign_id = case.state.campaign_id
        return (
            db.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(Artifact.campaign_id == campaign_id)
            )
            or 0,
            db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.campaign_id == campaign_id)
            )
            or 0,
            db.scalar(
                select(func.count())
                .select_from(GateDecision)
                .where(GateDecision.campaign_id == campaign_id)
            )
            or 0,
        )


def test_content_addressed_store_reuses_exact_bytes_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    store = CanonicalMediaStore(tmp_path, campaign_id=17)
    content = b"canonical-media-object"

    first = store.write(content, extension=".PNG")
    replay = store.write(content, extension="png")

    assert replay == first
    assert first.path.read_bytes() == content
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    assert list(store.object_root.iterdir()) == [first.path]
    assert store.resolve(first.uri, expected_sha256=first.sha256) == first.path

    first.path.write_bytes(b"tampered")
    with pytest.raises(WorkflowReplayConflict, match="content address"):
        store.write(content, extension="png")
    with pytest.raises(WorkflowReplayConflict, match="hash is invalid"):
        store.resolve(first.uri, expected_sha256=first.sha256)


def test_content_addressed_store_rejects_uri_and_symlink_path_traversal(
    tmp_path: Path,
) -> None:
    store = CanonicalMediaStore(tmp_path / "output", campaign_id=23)
    store.object_root.mkdir(parents=True)
    with pytest.raises(WorkflowReplayConflict, match="URI is invalid"):
        store.resolve(
            "i5-object://campaign/23/objects/../../outside.png",
        )
    with pytest.raises(WorkflowReplayConflict, match="URI is invalid"):
        store.resolve(
            f"i5-object://campaign/24/objects/{'0' * 64}.png",
        )

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    link = store.object_root / f"{digest}.png"
    link.symlink_to(outside)
    with pytest.raises(WorkflowReplayConflict, match="escapes"):
        store.resolve(
            f"i5-object://campaign/23/objects/{digest}.png",
            expected_sha256=digest,
        )


def test_scene_contract_rejects_column_json_and_narration_drift() -> None:
    script = _compile_i4_packets(31)[2]
    storyboard = compile_storyboard(
        campaign_id=31,
        script_packet=script,
        script_hash=canonical_sha256(script),
        production_profile_hash="f" * 64,
    )
    payload = dict(storyboard["scenes"][0])  # type: ignore[index]

    def row(value: dict[str, object]) -> Scene:
        return Scene(
            campaign_id=31,
            position=int(value["position"]),
            visual_mode=str(value["visual_mode"]),
            narration_reference=str(value["narration_sha256"]),
            overlay_spec_json=canonical_json(value),
            disclosure_state=str(value["disclosure_state"]),
        )

    baseline = row(payload)
    assert scene_contract(baseline) == payload

    for attribute, changed in (
        ("position", int(payload["position"]) + 1),
        ("visual_mode", "TIMELINE"),
        ("narration_reference", "0" * 64),
        ("disclosure_state", "tampered"),
    ):
        candidate = row(payload)
        setattr(candidate, attribute, changed)
        with pytest.raises(WorkflowReplayConflict, match="columns conflict"):
            scene_contract(candidate)

    changed_payload = {**payload, "narration": f"{payload['narration']} altered"}
    with pytest.raises(WorkflowReplayConflict, match="narration hash"):
        scene_contract(row(changed_payload))

    noncanonical = row(payload)
    noncanonical.overlay_spec_json = json.dumps(payload, indent=2)
    with pytest.raises(WorkflowReplayConflict, match="not canonical"):
        scene_contract(noncanonical)


def test_local_visual_persists_inside_store_and_replays_without_duplicates(
    planned_case: _MediaCase,
) -> None:
    with planned_case.sessions() as db:
        scene = db.scalar(
            select(Scene)
            .where(
                Scene.campaign_id == planned_case.state.campaign_id,
                Scene.visual_mode != "GENERATED_CINEMATIC",
            )
            .order_by(Scene.position)
        )
        assert scene is not None
        scene_id = scene.id
        position = scene.position

    first = render_and_persist_local_visual(
        campaign_id=planned_case.state.campaign_id,
        scene_id=scene_id,
        settings=planned_case.settings,
    )
    replay = render_and_persist_local_visual(
        campaign_id=planned_case.state.campaign_id,
        scene_id=scene_id,
        settings=planned_case.settings,
    )
    assert replay == first
    artifact = _artifact(planned_case, f"i5_scene_visual_{position:03d}")
    path = CanonicalMediaStore(
        planned_case.settings.output_path,
        planned_case.state.campaign_id,
    ).resolve(artifact.uri, expected_sha256=artifact.sha256)
    assert path.is_relative_to(planned_case.settings.output_path)
    assert png_dimensions(path.read_bytes()) == (1280, 720)
    provenance = json.loads(artifact.provenance_json)
    assert provenance["production_profile_hash"] == planned_case.state.profile_hash
    assert provenance["storyboard_hash"] == planned_case.state.storyboard_hash
    assert provenance["scene_position"] == position
    assert provenance["media_validation"] == {
        "height": 720,
        "valid": True,
        "width": 1280,
    }
    with planned_case.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(
                    Artifact.campaign_id == planned_case.state.campaign_id,
                    Artifact.kind == f"i5_scene_visual_{position:03d}",
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(
                    GenerationJob.campaign_id == planned_case.state.campaign_id,
                    GenerationJob.scene_id == scene_id,
                    GenerationJob.provider == "deterministic_local",
                )
            )
            == 1
        )


def test_generated_image_success_is_persisted_and_replay_does_not_redispatch(
    planned_case: _MediaCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    def generate(
        self: object,
        request: object,
        *,
        api_key: str,
    ) -> ImageResult:
        nonlocal provider_calls
        assert api_key == "test-only-key"
        provider_calls += 1
        return _generated_image_result(request)

    monkeypatch.setattr(provider_module.GPTImageProvider, "generate", generate)
    scene_id = planned_case.state.generated_scene_ids[0]
    scene = _scene(planned_case, scene_id)
    first = produce_scene_visual(
        campaign_id=planned_case.state.campaign_id,
        scene_id=scene_id,
        api_key="test-only-key",
        settings=planned_case.settings,
    )
    replay = produce_scene_visual(
        campaign_id=planned_case.state.campaign_id,
        scene_id=scene_id,
        api_key="test-only-key",
        settings=planned_case.settings,
    )

    assert first["status"] == "provider_complete"
    assert replay["dispatch"] is False
    assert replay["output_artifact_id"] == first["artifact_id"]
    assert provider_calls == 1
    artifact = _artifact(
        planned_case,
        f"i5_scene_visual_{scene.position:03d}",
    )
    provenance = json.loads(artifact.provenance_json)
    assert artifact.provider_name == "openai_image"
    assert provenance["generated_media_is_evidence"] is False
    assert provenance["disclosure_state"] == "illustrative_generated_media"
    assert provenance["rights_basis"] == "generated_illustrative"


def test_generated_image_failure_falls_back_locally_and_replays_without_redispatch(
    planned_case: _MediaCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    def fail(
        self: object,
        request: object,
        *,
        api_key: str,
    ) -> ImageResult:
        nonlocal provider_calls
        provider_calls += 1
        raise ProviderError("sanitized test-only failure")

    monkeypatch.setattr(provider_module.GPTImageProvider, "generate", fail)
    scene_id = planned_case.state.generated_scene_ids[1]
    scene = _scene(planned_case, scene_id)
    first = produce_scene_visual(
        campaign_id=planned_case.state.campaign_id,
        scene_id=scene_id,
        api_key="test-only-key",
        settings=planned_case.settings,
    )
    replay = produce_scene_visual(
        campaign_id=planned_case.state.campaign_id,
        scene_id=scene_id,
        api_key="test-only-key",
        settings=planned_case.settings,
    )

    assert replay["artifact_id"] == first["artifact_id"]
    assert replay["job_id"] == first["job_id"]
    assert first["fallback"] is replay["fallback"] is True
    assert provider_calls == 1
    artifact = _artifact(
        planned_case,
        f"i5_scene_visual_{scene.position:03d}",
    )
    provenance = json.loads(artifact.provenance_json)
    assert artifact.provider_name == "deterministic_local"
    assert provenance["fallback"] is True
    assert provenance["visual_mode"] == "DETERMINISTIC_MOTION_GRAPHIC"
    with planned_case.sessions() as db:
        jobs = list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.campaign_id == planned_case.state.campaign_id,
                    GenerationJob.scene_id == scene_id,
                    GenerationJob.provider.in_(
                        {"openai_image", "deterministic_local"}
                    ),
                )
            )
        )
    assert {(job.provider, job.status) for job in jobs} == {
        ("openai_image", "ambiguous_dispatch"),
        ("deterministic_local", "completed"),
    }


def test_tts_provider_exception_is_persisted_as_ambiguous_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_module,
        "claim_metered_dispatch",
        lambda job_id: {"dispatch": True, "job_id": job_id},
    )
    monkeypatch.setattr(
        media_module,
        "load_tts_job_input",
        lambda job_id: {
            "model": "tts-1-hd",
            "narration": f"bounded narration {job_id}",
        },
    )

    def unknown_outcome(self: object, request: object, *, api_key: str) -> None:
        del self, request, api_key
        raise ProviderError("sanitized transport failure")

    monkeypatch.setattr(
        provider_module.OpenAITTSProvider,
        "generate",
        unknown_outcome,
    )
    recorded: dict[str, object] = {}

    def record_failure(
        job_id: int,
        *,
        code: str,
        ambiguous: bool = False,
    ) -> dict[str, object]:
        recorded.update(job_id=job_id, code=code, ambiguous=ambiguous)
        return {"job_id": job_id, "status": "ambiguous_dispatch"}

    monkeypatch.setattr(media_module, "fail_metered_job", record_failure)

    result = produce_tts_job(
        job_id=11,
        api_key="test-only-key",
        settings=Settings(_env_file=None, output_dir=str(tmp_path)),
    )

    assert result == {"job_id": 11, "status": "ambiguous_dispatch"}
    assert recorded == {
        "ambiguous": True,
        "code": "tts_provider_outcome_unknown",
        "job_id": 11,
    }


def test_sora_create_transport_failure_is_persisted_as_ambiguous_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_module,
        "claim_metered_dispatch",
        lambda job_id: {"dispatch": True, "job_id": job_id},
    )
    monkeypatch.setattr(
        media_module,
        "_load_sora_job_input",
        lambda job_id: {
            "concept": f"bounded concept {job_id}",
            "video_profile": {
                "allow_deprecated_sora": True,
                "model": "sora-2",
                "provider": "openai",
            },
        },
    )

    class _UnknownOutcomeProvider:
        def create(self, request: object, *, api_key: str) -> None:
            del request, api_key
            raise ProviderError("sanitized transport failure")

    monkeypatch.setattr(
        media_module,
        "_sora_provider",
        lambda request: _UnknownOutcomeProvider(),
    )
    recorded: dict[str, object] = {}

    def record_failure(
        job_id: int,
        *,
        code: str,
        ambiguous: bool = False,
    ) -> dict[str, object]:
        recorded.update(job_id=job_id, code=code, ambiguous=ambiguous)
        return {"job_id": job_id, "status": "ambiguous_dispatch"}

    monkeypatch.setattr(media_module, "fail_metered_job", record_failure)

    result = media_module.create_sora_job(job_id=17, api_key="test-only-key")

    assert result == {"job_id": 17, "status": "ambiguous_dispatch"}
    assert recorded == {
        "ambiguous": True,
        "code": "sora_create_outcome_unknown",
        "job_id": 17,
    }


def test_whole_campaign_tts_plan_uses_one_model_voice_and_per_scene_media(
    planned_case: _MediaCase,
) -> None:
    replay = build_tts_plan_requests(planned_case.state.campaign_id)
    assert replay["replayed"] is True
    assert tuple(replay["job_ids"]) == planned_case.state.tts_job_ids
    assert replay["selected_model"] == "tts-1-hd"

    for job_id in planned_case.state.tts_job_ids:
        request = load_tts_job_input(job_id)
        assert request["model"] == "tts-1-hd"
        assert request["voice"] == "onyx"
        assert claim_metered_dispatch(job_id)["dispatch"] is True
        first = persist_tts_bytes(
            job_id=job_id,
            audio_bytes=_wav_bytes(int(request["scene_position"])),
            duration_seconds=_AUDIO_DURATION_SECONDS,
            settings=planned_case.settings,
        )
        second = persist_tts_bytes(
            job_id=job_id,
            audio_bytes=_wav_bytes(int(request["scene_position"])),
            duration_seconds=_AUDIO_DURATION_SECONDS,
            settings=planned_case.settings,
        )
        assert second["replayed"] is True
        assert second["artifact_id"] == first["artifact_id"]

    plan_artifact = _artifact(planned_case, I5_MEDIA_PLAN_KIND)
    plan = json.loads(str(plan_artifact.payload_json))
    assert plan["selected_tts_model"] == "tts-1-hd"
    assert plan["tts_voice"] == "onyx"
    assert len(plan["reservations"]) == len(planned_case.state.scene_ids)
    with planned_case.sessions() as db:
        jobs = list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.campaign_id == planned_case.state.campaign_id,
                    GenerationJob.provider == "openai_tts",
                )
            )
        )
        narration = list(
            db.scalars(
                select(Artifact).where(
                    Artifact.campaign_id == planned_case.state.campaign_id,
                    Artifact.kind.like("i5_scene_narration_%"),
                )
            )
        )
    assert len(jobs) == len(narration) == len(planned_case.state.scene_ids)
    assert {job.model for job in jobs} == {"tts-1-hd"}
    assert {job.status for job in jobs} == {"provider_complete"}
    assert all(artifact.provider_model == "tts-1-hd" for artifact in narration)


def test_tts_completion_reconciles_dispatching_reservation_before_persisting(
    planned_case: _MediaCase,
) -> None:
    job_id = planned_case.state.tts_job_ids[0]
    request = load_tts_job_input(job_id)
    assert claim_metered_dispatch(job_id)["dispatch"] is True
    with planned_case.sessions() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        job.usage_json = "{}"
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="reservation accounting"):
        persist_tts_bytes(
            job_id=job_id,
            audio_bytes=_wav_bytes(int(request["scene_position"])),
            duration_seconds=_AUDIO_DURATION_SECONDS,
            settings=planned_case.settings,
        )

    with planned_case.sessions() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert job.status == "dispatching"
        assert job.output_artifact_id is None


def test_full_voiceover_concatenates_every_scene_in_order_and_replays(
    scene_media_case: _MediaCase,
) -> None:
    before = _effect_counts(scene_media_case)
    first = create_full_voiceover(
        scene_media_case.state.campaign_id,
        scene_media_case.settings,
    )
    after_first = _effect_counts(scene_media_case)
    replay = create_full_voiceover(
        scene_media_case.state.campaign_id,
        scene_media_case.settings,
    )
    after_replay = _effect_counts(scene_media_case)

    assert first["replayed"] is False
    assert replay == {
        "artifact_id": first["artifact_id"],
        "duration_seconds": first["duration_seconds"],
        "output_hash": first["output_hash"],
        "replayed": True,
    }
    assert after_first == (before[0] + 1, before[1] + 1, before[2])
    assert after_replay == after_first
    artifact = _artifact(scene_media_case, "i5_voiceover")
    store = CanonicalMediaStore(
        scene_media_case.settings.output_path,
        scene_media_case.state.campaign_id,
    )
    probe = validate_wav(store.resolve(artifact.uri, expected_sha256=artifact.sha256))
    expected_duration = len(scene_media_case.state.scene_ids) * _AUDIO_DURATION_SECONDS
    assert probe.duration_seconds == pytest.approx(expected_duration, abs=0.08)
    provenance = json.loads(artifact.provenance_json)
    with scene_media_case.sessions() as db:
        ordered_hashes = [
            db.scalar(
                select(Artifact.sha256).where(
                    Artifact.campaign_id == scene_media_case.state.campaign_id,
                    Artifact.kind == f"i5_scene_narration_{position:03d}",
                )
            )
            for position in range(len(scene_media_case.state.scene_ids))
        ]
    assert provenance["ordered_scene_audio_hashes"] == ordered_hashes


def test_media_manifest_is_canonical_complete_and_content_verified(
    completed_case: _MediaCase,
) -> None:
    first = build_media_manifest(
        completed_case.state.campaign_id,
        completed_case.settings,
    )
    second = build_media_manifest(
        completed_case.state.campaign_id,
        completed_case.settings,
    )

    assert first == second
    assert canonical_json(first) == canonical_json(second)
    assert first["contract_version"] == "i5-media-manifest-v1"
    assert first["storyboard_hash"] == completed_case.state.storyboard_hash
    assert first["production_profile_hash"] == completed_case.state.profile_hash
    assert first["selected_tts_model"] == "tts-1-hd"
    assert first["tts_voice"] == "onyx"
    assert first["gate"] == {
        "outcome": "PASS",
        "reasons": ["all_scene_media_and_budget_integrity_checks_passed"],
    }
    assert first["integrity"] == {
        "all_content_hashes_verified": True,
        "all_objects_inside_canonical_store": True,
        "all_scene_media_present": True,
        "generated_media_is_illustrative_only": True,
        "rights_gate_passed": True,
    }
    scene_media = first["scene_media"]
    assert [item["scene_position"] for item in scene_media] == list(  # type: ignore[index]
        range(len(completed_case.state.scene_ids))
    )
    assert len(scene_media) == len(completed_case.state.scene_ids)  # type: ignore[arg-type]
    summary = first["generated_media_summary"]
    assert summary["generated_image_scene_count"] == 1  # type: ignore[index]
    assert summary["generated_video_scene_count"] == 0  # type: ignore[index]
    assert summary["local_visual_scene_count"] == len(  # type: ignore[index]
        completed_case.state.scene_ids
    ) - 1


def test_media_pass_boundary_is_atomic_and_duplicate_safe(
    completed_case: _MediaCase,
) -> None:
    packet = build_media_manifest(
        completed_case.state.campaign_id,
        completed_case.settings,
    )
    before = _effect_counts(completed_case)
    first = persist_media_manifest(packet, settings=completed_case.settings)
    after_first = _effect_counts(completed_case)
    replay = persist_media_manifest(packet, settings=completed_case.settings)
    after_replay = _effect_counts(completed_case)

    assert first["outcome"] == "PASS"
    assert first["current_stage"] == "assembly"
    assert first["replayed"] is False
    assert replay == {**first, "replayed": True}
    assert after_first == (before[0] + 1, before[1] + 1, before[2] + 1)
    assert after_replay == after_first
    with completed_case.sessions() as db:
        campaign = db.get(Campaign, completed_case.state.campaign_id)
        assert campaign is not None and campaign.current_stage == "assembly"
        reconciled = reconcile_media_effect_set(
            db,
            completed_case.state.campaign_id,
            settings=completed_case.settings,
        )
    assert reconciled["media_hash"] == first["output_hash"]
    assert reconciled["media_packet"] == packet


def test_missing_binary_artifact_blocks_media_manifest(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == completed_case.state.campaign_id,
                Artifact.kind == "i5_scene_narration_000",
            )
        )
        assert artifact is not None
        artifact.kind = "tampered_missing_scene_narration"
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="Expected exactly one"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_nonbinary_artifact_shape_blocks_media_manifest(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == completed_case.state.campaign_id,
                Artifact.kind == "i5_scene_visual_000",
            )
        )
        assert artifact is not None
        artifact.payload_json = "{}"
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="binary artifact fields"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_missing_or_tampered_canonical_object_blocks_media_manifest(
    completed_case: _MediaCase,
    mutation: str,
) -> None:
    artifact = _artifact(completed_case, "i5_scene_narration_000")
    store = CanonicalMediaStore(
        completed_case.settings.output_path,
        completed_case.state.campaign_id,
    )
    path = store.resolve(artifact.uri, expected_sha256=artifact.sha256)
    if mutation == "missing":
        path.unlink()
    else:
        path.write_bytes(b"tampered canonical bytes")

    with pytest.raises(WorkflowReplayConflict, match="object is missing|hash is invalid"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_tampered_binary_provenance_lineage_blocks_media_manifest(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == completed_case.state.campaign_id,
                Artifact.kind == "i5_scene_visual_000",
            )
        )
        assert artifact is not None
        provenance = json.loads(artifact.provenance_json)
        provenance["storyboard_hash"] = "0" * 64
        artifact.provenance_json = canonical_json(provenance)
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="Scene media lineage"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_changed_media_plan_voice_lineage_blocks_manifest(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == completed_case.state.campaign_id,
                Artifact.kind == I5_MEDIA_PLAN_KIND,
            )
        )
        assert artifact is not None and artifact.payload_json is not None
        payload = json.loads(artifact.payload_json)
        payload["tts_voice"] = "alloy"
        artifact.payload_json = canonical_json(payload)
        artifact.sha256 = canonical_sha256(payload)
        artifact.byte_size = len(artifact.payload_json.encode("utf-8"))
        artifact.uri = (
            f"artifact://campaign/{completed_case.state.campaign_id}/"
            f"{I5_MEDIA_PLAN_KIND}/{artifact.sha256}"
        )
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="voice|media plan"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_tampered_manifest_packet_cannot_cross_media_pass_boundary(
    completed_case: _MediaCase,
) -> None:
    packet = build_media_manifest(
        completed_case.state.campaign_id,
        completed_case.settings,
    )
    tampered = deepcopy(packet)
    tampered["voiceover"]["artifact_hash"] = "0" * 64  # type: ignore[index]

    with pytest.raises(WorkflowReplayConflict, match="does not match canonical"):
        persist_media_manifest(tampered, settings=completed_case.settings)
    with completed_case.sessions() as db:
        campaign = db.get(Campaign, completed_case.state.campaign_id)
        assert campaign is not None and campaign.current_stage == "media"
        assert (
            db.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(
                    Artifact.campaign_id == completed_case.state.campaign_id,
                    Artifact.kind == I5_MEDIA_MANIFEST_KIND,
                )
            )
            == 0
        )


def test_committed_manifest_tamper_is_rejected_on_reconciliation(
    completed_case: _MediaCase,
) -> None:
    packet = build_media_manifest(
        completed_case.state.campaign_id,
        completed_case.settings,
    )
    persist_media_manifest(packet, settings=completed_case.settings)
    with completed_case.sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == completed_case.state.campaign_id,
                Artifact.kind == I5_MEDIA_MANIFEST_KIND,
            )
        )
        assert artifact is not None and artifact.payload_json is not None
        payload = json.loads(artifact.payload_json)
        payload["tts_voice"] = "tampered"
        artifact.payload_json = canonical_json(payload)
        db.commit()

    with completed_case.sessions() as db:
        with pytest.raises(WorkflowReplayConflict, match="payload hash"):
            reconcile_media_effect_set(
                db,
                completed_case.state.campaign_id,
                settings=completed_case.settings,
            )


def test_storyboard_handoff_rejects_an_extra_wrong_policy_gate(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        canonical_gate = db.scalar(
            select(GateDecision).where(
                GateDecision.campaign_id == completed_case.state.campaign_id,
                GateDecision.stage == "storyboard",
            )
        )
        assert canonical_gate is not None
        db.add(
            GateDecision(
                campaign_id=completed_case.state.campaign_id,
                stage="storyboard",
                outcome="PASS",
                policy_version="wrong-storyboard-policy",
                input_hash="d" * 64,
                output_hash=canonical_gate.output_hash,
                reasons_json=canonical_gate.reasons_json,
            )
        )
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="storyboard effect set is not exact"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_media_handoff_rejects_an_extra_wrong_policy_gate(
    completed_case: _MediaCase,
) -> None:
    packet = build_media_manifest(
        completed_case.state.campaign_id,
        completed_case.settings,
    )
    persist_media_manifest(packet, settings=completed_case.settings)
    with completed_case.sessions() as db:
        canonical_gate = db.scalar(
            select(GateDecision).where(
                GateDecision.campaign_id == completed_case.state.campaign_id,
                GateDecision.stage == "media",
                GateDecision.outcome == "PASS",
            )
        )
        assert canonical_gate is not None
        db.add(
            GateDecision(
                campaign_id=completed_case.state.campaign_id,
                stage="media",
                outcome="PASS",
                policy_version="wrong-media-policy",
                input_hash="c" * 64,
                output_hash=canonical_gate.output_hash,
                reasons_json=canonical_gate.reasons_json,
            )
        )
        db.commit()

    with completed_case.sessions() as db:
        with pytest.raises(WorkflowReplayConflict, match="media effect set is not exact"):
            reconcile_media_effect_set(
                db,
                completed_case.state.campaign_id,
                settings=completed_case.settings,
            )


def test_media_gate_rejects_self_consistent_invalid_png_bytes(
    completed_case: _MediaCase,
) -> None:
    invalid = b"not a valid PNG image"
    store = CanonicalMediaStore(
        completed_case.settings.output_path,
        completed_case.state.campaign_id,
    )
    stored = store.write(invalid, extension="png")
    with completed_case.sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == completed_case.state.campaign_id,
                Artifact.kind == "i5_scene_visual_000",
            )
        )
        assert artifact is not None
        provenance = json.loads(artifact.provenance_json)
        provenance["content_sha256"] = stored.sha256
        artifact.uri = stored.uri
        artifact.sha256 = stored.sha256
        artifact.byte_size = stored.byte_size
        artifact.provenance_json = canonical_json(provenance)
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="immutable metadata conflicts|visual PNG is invalid"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_media_gate_rejects_artifact_byte_size_drift(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.campaign_id == completed_case.state.campaign_id,
                Artifact.kind == "i5_scene_narration_000",
            )
        )
        assert artifact is not None
        artifact.byte_size += 777
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="byte size"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_media_gate_reconciles_completed_metered_accounting(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        job = db.scalar(
            select(GenerationJob).where(
                GenerationJob.campaign_id == completed_case.state.campaign_id,
                GenerationJob.provider == "openai_tts",
            )
        )
        assert job is not None
        job.cost_microunits = 0
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="TTS accounting"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_media_gate_rejects_effective_cost_above_authorized_cap(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        db.add(
            GenerationJob(
                campaign_id=completed_case.state.campaign_id,
                scene_id=None,
                provider="openai_video",
                model="sora-2",
                attempt=1,
                status="ambiguous_dispatch",
                input_hash="f" * 64,
                output_artifact_id=None,
                provider_job_id=None,
                usage_json=canonical_json({"test_only": True}),
                cost_microunits=None,
                reserved_cost_microunits=35_000_000,
                error_json=canonical_json(
                    {"code": "ambiguous_dispatch", "retryable_post": False}
                ),
            )
        )
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="authorized budget cap"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_media_gate_rejects_unrecognized_generation_job_effect(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        db.add(
            GenerationJob(
                campaign_id=completed_case.state.campaign_id,
                scene_id=None,
                provider="unrecognized_provider",
                model="unrecognized_model",
                attempt=1,
                status="provider_failed",
                input_hash="e" * 64,
                output_artifact_id=None,
                provider_job_id=None,
                usage_json=canonical_json({"test_only": True}),
                cost_microunits=None,
                reserved_cost_microunits=1,
                error_json=canonical_json(
                    {"code": "test_only", "retryable_post": False}
                ),
            )
        )
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="unrecognized generation job"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_media_gate_rejects_failed_visual_usage_drift(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        job = db.scalar(
            select(GenerationJob).where(
                GenerationJob.campaign_id == completed_case.state.campaign_id,
                GenerationJob.provider == "openai_image",
                GenerationJob.status == "ambiguous_dispatch",
            )
        )
        assert job is not None
        job.usage_json = "{}"
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="Visual reservation usage conflicts|fallback lineage"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_media_gate_rejects_local_visual_claim_lineage_drift(
    completed_case: _MediaCase,
) -> None:
    with completed_case.sessions() as db:
        artifact = db.scalar(
            select(Artifact)
            .where(
                Artifact.campaign_id == completed_case.state.campaign_id,
                Artifact.provider_name == "deterministic_local",
                Artifact.kind.like("i5_scene_visual_%"),
            )
            .order_by(Artifact.kind)
        )
        assert artifact is not None
        provenance = json.loads(artifact.provenance_json)
        assert provenance["claim_hashes"]
        provenance["claim_hashes"] = ["0" * 64]
        artifact.provenance_json = canonical_json(provenance)
        db.commit()

    with pytest.raises(WorkflowReplayConflict, match="immutable metadata"):
        build_media_manifest(
            completed_case.state.campaign_id,
            completed_case.settings,
        )


def test_canonical_store_rejects_symlink_escape_from_output_root(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    (output / "canonical_i5").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkflowReplayConflict, match="configured output root"):
        CanonicalMediaStore(output, 7)
