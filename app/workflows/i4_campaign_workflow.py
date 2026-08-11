from __future__ import annotations

from dbos import DBOS, SetWorkflowID, SetWorkflowTimeout, WorkflowHandle

from app.config import get_settings
from app.editorial.claims import compile_research_packet
from app.editorial.contracts import (
    DemandSnapshot,
    EditorialSeed,
    canonical_sha256,
)
from app.editorial.demand import DemandRequest, YouTubeDemandProvider
from app.editorial.persistence import (
    bind_i4_campaign_workflow,
    i4_campaign_workflow_id,
    load_active_editorial_seed,
    load_topic_compilation_context,
    persist_research_stage,
    persist_script_stage,
    persist_topic_stage,
)
from app.editorial.script_compiler import compile_script_packet
from app.editorial.topic_intelligence import compile_topic_packet

from .dbos_runtime import require_dbos_runtime


I4_WORKFLOW_MAX_RECOVERY_ATTEMPTS = 3
I4_PROVIDER_STEP_MAX_ATTEMPTS = 2
I4_PERSIST_STEP_MAX_ATTEMPTS = 3
I4_WORKFLOW_TIMEOUT_SECONDS = 120


class I4ConfigurationError(RuntimeError):
    pass


def _seed_from_payload(
    campaign_id: int,
    seed_hash: str,
    seed_payload: dict[str, object],
) -> EditorialSeed:
    seed = EditorialSeed.model_validate({"candidates": seed_payload["candidates"]})
    expected_payload = seed.artifact_payload(campaign_id)
    if seed_payload != expected_payload or seed.sha256(campaign_id) != seed_hash:
        raise ValueError("I4 seed payload does not match the bound seed hash")
    return seed


@DBOS.step(
    name="i4_acquire_demand_evidence",
    retries_allowed=True,
    interval_seconds=0.05,
    max_attempts=I4_PROVIDER_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def acquire_demand_evidence_step(
    campaign_id: int,
    seed_hash: str,
    seed_payload: dict[str, object],
) -> dict[str, object]:
    seed = _seed_from_payload(campaign_id, seed_hash, seed_payload)
    request = DemandRequest.from_seed(
        campaign_id=campaign_id,
        seed_hash=seed_hash,
        seed=seed,
    )
    snapshot = YouTubeDemandProvider().acquire(
        request,
        api_key=get_settings().youtube_data_api_key or "",
    )
    return snapshot.model_dump(mode="json")


@DBOS.step(
    name="i4_compile_topic_packet",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I4_PERSIST_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def compile_topic_packet_step(
    campaign_id: int,
    seed_hash: str,
    seed_payload: dict[str, object],
    demand_payload: dict[str, object],
) -> dict[str, object]:
    context = load_topic_compilation_context(campaign_id, seed_hash)
    return compile_topic_packet(
        campaign_id=campaign_id,
        seed_hash=seed_hash,
        seed=_seed_from_payload(campaign_id, seed_hash, seed_payload),
        demand=DemandSnapshot.model_validate(demand_payload),
        channel_niche=str(context["channel_niche"]),
        channel_audience=str(context["channel_audience"]),
        novelty_history=tuple(context["novelty_history"]),  # type: ignore[arg-type]
    )


@DBOS.step(
    name="i4_persist_topic_stage",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I4_PERSIST_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def persist_topic_stage_step(packet: dict[str, object]) -> dict[str, object]:
    return persist_topic_stage(packet)


@DBOS.step(
    name="i4_compile_research_packet",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I4_PERSIST_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def compile_research_packet_step(
    campaign_id: int,
    seed_payload: dict[str, object],
    topic_packet: dict[str, object],
) -> dict[str, object]:
    return compile_research_packet(
        campaign_id=campaign_id,
        seed=_seed_from_payload(
            campaign_id,
            str(topic_packet["seed_hash"]),
            seed_payload,
        ),
        topic_packet=topic_packet,
        topic_packet_hash=canonical_sha256(topic_packet),
    )


@DBOS.step(
    name="i4_persist_research_stage",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I4_PERSIST_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def persist_research_stage_step(packet: dict[str, object]) -> dict[str, object]:
    return persist_research_stage(packet)


@DBOS.step(
    name="i4_compile_script",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I4_PERSIST_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def compile_script_step(
    campaign_id: int,
    topic_packet: dict[str, object],
    research_packet: dict[str, object],
) -> dict[str, object]:
    return compile_script_packet(
        campaign_id=campaign_id,
        topic_packet=topic_packet,
        topic_packet_hash=canonical_sha256(topic_packet),
        research_packet=research_packet,
        research_packet_hash=canonical_sha256(research_packet),
    )


@DBOS.step(
    name="i4_persist_script_stage",
    retries_allowed=True,
    interval_seconds=0.01,
    max_attempts=I4_PERSIST_STEP_MAX_ATTEMPTS,
    backoff_rate=1.0,
)
def persist_script_stage_step(packet: dict[str, object]) -> dict[str, object]:
    return persist_script_stage(packet)


@DBOS.workflow(
    name="i4_campaign_workflow",
    max_recovery_attempts=I4_WORKFLOW_MAX_RECOVERY_ATTEMPTS,
)
def run_i4_campaign_workflow(
    campaign_id: int,
    seed_hash: str,
    seed_payload: dict[str, object],
) -> dict[str, object]:
    workflow_id = i4_campaign_workflow_id(campaign_id, seed_hash)
    if DBOS.workflow_id != workflow_id:
        raise RuntimeError("DBOS workflow identity does not match the bound I4 seed")

    demand = acquire_demand_evidence_step(campaign_id, seed_hash, seed_payload)
    topic_packet = compile_topic_packet_step(
        campaign_id,
        seed_hash,
        seed_payload,
        demand,
    )
    topic_result = persist_topic_stage_step(topic_packet)
    stage_results = [topic_result]
    if topic_result["outcome"] != "PASS":
        return {
            "campaign_id": campaign_id,
            "final_stage": topic_result["current_stage"],
            "seed_hash": seed_hash,
            "stage_results": stage_results,
            "workflow_id": workflow_id,
        }

    research_packet = compile_research_packet_step(
        campaign_id,
        seed_payload,
        topic_packet,
    )
    research_result = persist_research_stage_step(research_packet)
    stage_results.append(research_result)
    if research_result["outcome"] != "PASS":
        return {
            "campaign_id": campaign_id,
            "final_stage": research_result["current_stage"],
            "seed_hash": seed_hash,
            "stage_results": stage_results,
            "workflow_id": workflow_id,
        }

    script_packet = compile_script_step(
        campaign_id,
        topic_packet,
        research_packet,
    )
    script_result = persist_script_stage_step(script_packet)
    stage_results.append(script_result)
    return {
        "campaign_id": campaign_id,
        "final_stage": script_result["current_stage"],
        "seed_hash": seed_hash,
        "stage_results": stage_results,
        "workflow_id": workflow_id,
    }


def start_i4_campaign_workflow(
    campaign_id: int,
) -> WorkflowHandle[dict[str, object]]:
    require_dbos_runtime()
    load_active_editorial_seed(campaign_id)
    if not (get_settings().youtube_data_api_key or "").strip():
        raise I4ConfigurationError(
            "YOUTUBE_DATA_API_KEY is required before starting an I4 workflow"
        )
    binding = bind_i4_campaign_workflow(campaign_id)
    workflow_id = str(binding["workflow_id"])
    with SetWorkflowID(workflow_id), SetWorkflowTimeout(I4_WORKFLOW_TIMEOUT_SECONDS):
        return DBOS.start_workflow(
            run_i4_campaign_workflow,
            campaign_id,
            str(binding["seed_hash"]),
            dict(binding["seed_payload"]),  # type: ignore[arg-type]
        )
