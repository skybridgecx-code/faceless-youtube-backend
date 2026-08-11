from __future__ import annotations

from typing import Mapping, cast

from dbos import DBOS, SetWorkflowID, SetWorkflowTimeout, WorkflowHandle

from app.config import Settings, get_settings
from app.editorial.persistence import load_accepted_i4_script_packet
from app.production.assembly import assemble_campaign
from app.production.budget import load_campaign_budget_policy
from app.production.media import (
    build_media_manifest,
    build_tts_plan_requests,
    create_sora_job,
    create_full_voiceover,
    download_and_persist_sora_job,
    load_tts_job_input,
    poll_sora_job,
    produce_scene_visual,
    produce_tts_job,
    render_and_persist_local_visual,
)
from app.production.persistence import (
    bind_i5_production_profile,
    fail_metered_job,
    load_bound_i5_context,
    persist_media_manifest,
    persist_storyboard,
    verify_budget_override_message,
)
from app.production.profile import (
    I5ConfigurationError,
    ProductionProfile,
    build_production_profile,
)
from app.production.renderer import RendererError, capture_tool_versions
from app.production.storyboard import compile_storyboard
from app.workflows.persistence import (
    CampaignNotFoundError,
    WorkflowReplayConflict,
    WorkflowTransitionError,
)

from .dbos_runtime import require_dbos_runtime


I5_WORKFLOW_MAX_RECOVERY_ATTEMPTS = 3
I5_WORKFLOW_TIMEOUT_SECONDS = 691_200
I5_BUDGET_OVERRIDE_WAIT_SECONDS = 604_800
I5_MAX_BUDGET_OVERRIDE_MESSAGES = 3
I5_SAFE_POST_RETRIES = 0
I5_LOCAL_STEP_MAX_ATTEMPTS = 3
I5_SORA_POLL_INTERVAL_SECONDS = 5
I5_SORA_MAX_POLLS = 120


def _build_current_profile(
    campaign_id: int,
    *,
    require_credentials: bool,
) -> tuple[Settings, dict[str, object], ProductionProfile]:
    settings = get_settings()
    try:
        settings.validate_i5_configuration(
            require_credentials=require_credentials,
        )
        script = load_accepted_i4_script_packet(campaign_id)
        versions = capture_tool_versions()
        profile = build_production_profile(
            campaign_id=campaign_id,
            i4_script_sha256=str(script["script_hash"]),
            settings=settings,
            budget_policy=load_campaign_budget_policy(),
            ffmpeg_version=versions.ffmpeg_version,
            ffprobe_version=versions.ffprobe_version,
        )
    except I5ConfigurationError:
        raise
    except (CampaignNotFoundError, WorkflowReplayConflict, WorkflowTransitionError):
        raise
    except RendererError as exc:
        raise I5ConfigurationError(str(exc)) from None
    except RuntimeError as exc:
        # Configuration messages name fields and requirements, never values.
        raise I5ConfigurationError(str(exc)) from None
    return settings, script, profile


def _bound_settings(campaign_id: int) -> tuple[Settings, dict[str, object]]:
    settings, script, profile = _build_current_profile(
        campaign_id,
        require_credentials=True,
    )
    context = load_bound_i5_context(campaign_id)
    if (
        context["script_hash"] != script["script_hash"]
        or context["profile_hash"] != profile.sha256
        or context["profile_payload"] != profile.payload
    ):
        raise WorkflowReplayConflict(
            "Current I5 runtime configuration conflicts with the bound production profile"
        )
    return settings, context


@DBOS.step(
    name="i5_compile_storyboard",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def compile_storyboard_step(
    campaign_id: int,
    script_hash: str,
    production_profile_hash: str,
) -> dict[str, object]:
    context = load_bound_i5_context(campaign_id)
    if (
        context["script_hash"] != script_hash
        or context["profile_hash"] != production_profile_hash
    ):
        raise WorkflowReplayConflict("I5 storyboard workflow lineage is invalid")
    video_profile = dict(
        cast(Mapping[str, object], context["profile_payload"])["video"]  # type: ignore[index]
    )
    return compile_storyboard(
        campaign_id=campaign_id,
        script_packet=cast(dict[str, object], context["script_payload"]),
        script_hash=script_hash,
        production_profile_hash=production_profile_hash,
        generated_video_enabled=(
            video_profile.get("provider") != "disabled"
            and video_profile.get("allow_deprecated_sora") is True
        ),
    )


@DBOS.step(
    name="i5_persist_storyboard",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def persist_storyboard_step(packet: dict[str, object]) -> dict[str, object]:
    return persist_storyboard(packet)


@DBOS.step(
    name="i5_reserve_tts_plan",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def reserve_tts_plan_step(campaign_id: int) -> dict[str, object]:
    _bound_settings(campaign_id)
    return build_tts_plan_requests(campaign_id)


@DBOS.step(
    name="i5_verify_budget_override_message",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def verify_budget_override_message_step(
    campaign_id: int,
    production_workflow_id: str,
    message: object,
) -> dict[str, object]:
    if not isinstance(message, dict):
        return {"valid": False}
    try:
        verified = verify_budget_override_message(
            campaign_id=campaign_id,
            production_workflow_id=production_workflow_id,
            message=cast(Mapping[str, object], message),
        )
    except (ValueError, WorkflowReplayConflict, WorkflowTransitionError):
        return {"valid": False}
    return {"valid": True, **verified}


@DBOS.step(
    name="i5_produce_tts_job",
    retries_allowed=False,
    max_attempts=1,
)
def produce_tts_job_step(campaign_id: int, job_id: int) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    request = load_tts_job_input(job_id)
    if request["campaign_id"] != campaign_id:
        raise WorkflowReplayConflict("I5 TTS job belongs to another campaign")
    return produce_tts_job(
        job_id=job_id,
        api_key=(settings.openai_api_key or "").strip(),
        settings=settings,
    )


@DBOS.step(
    name="i5_produce_scene_visual",
    retries_allowed=False,
    max_attempts=1,
)
def produce_scene_visual_step(
    campaign_id: int,
    scene_id: int,
) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    return produce_scene_visual(
        campaign_id=campaign_id,
        scene_id=scene_id,
        api_key=(settings.i5_image_generation_api_key or "").strip(),
        settings=settings,
    )


@DBOS.step(
    name="i5_create_sora_job",
    retries_allowed=False,
    max_attempts=1,
)
def create_sora_job_step(campaign_id: int, job_id: int) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    return create_sora_job(
        job_id=job_id,
        api_key=(settings.openai_api_key or "").strip(),
    )


@DBOS.step(
    name="i5_poll_sora_job",
    retries_allowed=True,
    interval_seconds=0.05,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def poll_sora_job_step(campaign_id: int, job_id: int) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    return poll_sora_job(
        job_id=job_id,
        api_key=(settings.openai_api_key or "").strip(),
    )


@DBOS.step(
    name="i5_download_sora_job",
    retries_allowed=True,
    interval_seconds=0.05,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def download_sora_job_step(campaign_id: int, job_id: int) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    return download_and_persist_sora_job(
        job_id=job_id,
        api_key=(settings.openai_api_key or "").strip(),
        settings=settings,
    )


@DBOS.step(
    name="i5_fail_sora_job",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def fail_sora_job_step(job_id: int, code: str) -> dict[str, object]:
    return fail_metered_job(job_id, code=code)


@DBOS.step(
    name="i5_render_sora_fallback",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def render_sora_fallback_step(
    campaign_id: int,
    scene_id: int,
) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    return render_and_persist_local_visual(
        campaign_id=campaign_id,
        scene_id=scene_id,
        settings=settings,
        fallback=True,
    )


@DBOS.step(
    name="i5_create_full_voiceover",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def create_full_voiceover_step(campaign_id: int) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    return create_full_voiceover(campaign_id, settings)


@DBOS.step(
    name="i5_persist_media_manifest",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def persist_media_manifest_step(campaign_id: int) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    packet = build_media_manifest(campaign_id, settings)
    return persist_media_manifest(packet, settings=settings)


@DBOS.step(
    name="i5_assemble_campaign",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I5_LOCAL_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def assemble_campaign_step(campaign_id: int) -> dict[str, object]:
    settings, _ = _bound_settings(campaign_id)
    return assemble_campaign(campaign_id, settings)


def _human_required_result(
    *,
    campaign_id: int,
    production_workflow_id: str,
    production_profile_hash: str,
    reason: str,
    stage_results: list[dict[str, object]],
    final_stage: str = "media",
) -> dict[str, object]:
    return {
        "campaign_id": campaign_id,
        "final_stage": final_stage,
        "outcome": "NEEDS_HUMAN",
        "production_profile_hash": production_profile_hash,
        "production_workflow_id": production_workflow_id,
        "reason": reason,
        "stage_results": stage_results,
    }


@DBOS.workflow(
    name="i5_production_workflow",
    max_recovery_attempts=I5_WORKFLOW_MAX_RECOVERY_ATTEMPTS,
)
def run_i5_production_workflow(
    campaign_id: int,
    script_hash: str,
    production_profile_hash: str,
) -> dict[str, object]:
    context = load_bound_i5_context(campaign_id)
    workflow_id = str(context["workflow_id"])
    if (
        DBOS.workflow_id != workflow_id
        or context["script_hash"] != script_hash
        or context["profile_hash"] != production_profile_hash
    ):
        raise RuntimeError("DBOS workflow identity does not match bound I5 lineage")

    stage_results: list[dict[str, object]] = []
    storyboard_packet = compile_storyboard_step(
        campaign_id,
        script_hash,
        production_profile_hash,
    )
    storyboard_result = persist_storyboard_step(storyboard_packet)
    stage_results.append(storyboard_result)
    if storyboard_result.get("outcome") != "PASS":
        return _human_required_result(
            campaign_id=campaign_id,
            production_workflow_id=workflow_id,
            production_profile_hash=production_profile_hash,
            reason="storyboard_gate_did_not_pass",
            stage_results=stage_results,
            final_stage=str(storyboard_result.get("current_stage") or "storyboard"),
        )

    tts_plan = reserve_tts_plan_step(campaign_id)
    for _ in range(I5_MAX_BUDGET_OVERRIDE_MESSAGES):
        if not tts_plan.get("blocked"):
            break
        message = DBOS.recv(
            topic="budget_override",
            timeout_seconds=I5_BUDGET_OVERRIDE_WAIT_SECONDS,
        )
        if message is None:
            return _human_required_result(
                campaign_id=campaign_id,
                production_workflow_id=workflow_id,
                production_profile_hash=production_profile_hash,
                reason="budget_override_wait_expired",
                stage_results=[*stage_results, tts_plan],
            )
        verified = verify_budget_override_message_step(
            campaign_id,
            workflow_id,
            message,
        )
        if verified.get("valid"):
            tts_plan = reserve_tts_plan_step(campaign_id)
    if tts_plan.get("blocked"):
        return _human_required_result(
            campaign_id=campaign_id,
            production_workflow_id=workflow_id,
            production_profile_hash=production_profile_hash,
            reason="authorized_budget_remains_insufficient",
            stage_results=[*stage_results, tts_plan],
        )

    stage_results.append(tts_plan)
    for raw_job_id in cast(list[int], tts_plan["job_ids"]):
        result = produce_tts_job_step(campaign_id, int(raw_job_id))
        stage_results.append(result)
        if result.get("status") not in {"provider_complete", "completed"}:
            return _human_required_result(
                campaign_id=campaign_id,
                production_workflow_id=workflow_id,
                production_profile_hash=production_profile_hash,
                reason=f"required_tts_{result.get('status', 'failed')}",
                stage_results=stage_results,
            )

    requests = cast(list[Mapping[str, object]], tts_plan["requests"])
    for request in requests:
        result = produce_scene_visual_step(campaign_id, int(request["scene_id"]))
        stage_results.append(result)
        if not result.get("requires_sora"):
            continue
        job_id = int(result["job_id"])
        created = create_sora_job_step(campaign_id, job_id)
        stage_results.append(created)
        if created.get("status") != "submitted":
            stage_results.append(
                render_sora_fallback_step(campaign_id, int(request["scene_id"]))
            )
            continue

        sora_completed = False
        sora_failed = False
        for poll_index in range(I5_SORA_MAX_POLLS):
            try:
                polled = poll_sora_job_step(campaign_id, job_id)
            except Exception:
                stage_results.append(
                    fail_sora_job_step(job_id, "sora_poll_failed")
                )
                sora_failed = True
                break
            stage_results.append(polled)
            provider_status = str(polled.get("status") or "")
            if provider_status == "completed":
                downloaded = download_sora_job_step(campaign_id, job_id)
                stage_results.append(downloaded)
                sora_completed = downloaded.get("status") in {
                    "provider_complete",
                    "completed",
                }
                sora_failed = not sora_completed
                break
            if provider_status in {"failed", "cancelled"}:
                stage_results.append(
                    fail_sora_job_step(job_id, f"sora_{provider_status}")
                )
                sora_failed = True
                break
            if poll_index + 1 < I5_SORA_MAX_POLLS:
                DBOS.sleep(I5_SORA_POLL_INTERVAL_SECONDS)
        if not sora_completed:
            if not sora_failed:
                stage_results.append(
                    fail_sora_job_step(job_id, "sora_poll_timeout")
                )
            stage_results.append(
                render_sora_fallback_step(campaign_id, int(request["scene_id"]))
            )

    stage_results.append(create_full_voiceover_step(campaign_id))
    media_result = persist_media_manifest_step(campaign_id)
    stage_results.append(media_result)
    if media_result.get("outcome") != "PASS":
        return _human_required_result(
            campaign_id=campaign_id,
            production_workflow_id=workflow_id,
            production_profile_hash=production_profile_hash,
            reason="media_gate_did_not_pass",
            stage_results=stage_results,
        )

    assembly_result = assemble_campaign_step(campaign_id)
    stage_results.append(assembly_result)
    return {
        "campaign_id": campaign_id,
        "final_stage": assembly_result["current_stage"],
        "outcome": assembly_result["outcome"],
        "production_profile_hash": production_profile_hash,
        "production_workflow_id": workflow_id,
        "stage_results": stage_results,
    }


def start_i5_production_workflow(
    campaign_id: int,
) -> WorkflowHandle[dict[str, object]]:
    """Validate all start preconditions before binding the I5 identity."""

    require_dbos_runtime()
    _, script, profile = _build_current_profile(
        campaign_id,
        require_credentials=True,
    )
    binding = bind_i5_production_profile(campaign_id, profile)
    workflow_id = str(binding["production_workflow_id"])
    with SetWorkflowID(workflow_id), SetWorkflowTimeout(I5_WORKFLOW_TIMEOUT_SECONDS):
        return DBOS.start_workflow(
            run_i5_production_workflow,
            campaign_id,
            str(script["script_hash"]),
            profile.sha256,
        )


def send_i5_budget_override(
    *,
    workflow_id: str,
    message: Mapping[str, object],
    idempotency_key: str,
) -> None:
    require_dbos_runtime()
    DBOS.send(
        workflow_id,
        dict(message),
        topic="budget_override",
        idempotency_key=idempotency_key,
    )
