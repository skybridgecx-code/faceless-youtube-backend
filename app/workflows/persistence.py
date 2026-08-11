from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Artifact, Campaign, GateDecision, GenerationJob, SessionLocal
from app.db.models_content import RUNTIME_STAGES

from .contracts import (
    GateResult,
    PersistedStageResult,
    ProviderResult,
    StageName,
    StageRequest,
    canonical_json,
)


NEXT_STAGE: dict[StageName, StageName] = {
    "topic": "research",
    "research": "script",
    "script": "storyboard",
    "storyboard": "media",
    "media": "assembly",
    "assembly": "machine_qa",
    "machine_qa": "private_upload",
    "private_upload": "human_approval",
    "human_approval": "release",
}


class I3WorkflowError(RuntimeError):
    """Base class for deterministic I3 workflow failures."""


class CampaignNotFoundError(I3WorkflowError):
    pass


class WorkflowTransitionError(I3WorkflowError):
    pass


class WorkflowReplayConflict(I3WorkflowError):
    pass


def _stage_name(value: str) -> StageName:
    if value not in RUNTIME_STAGES:
        raise WorkflowTransitionError(f"Campaign contains unknown stage: {value}")
    return cast(StageName, value)


def build_stage_request(campaign_id: int, stage: StageName) -> StageRequest:
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
        return StageRequest.build(
            campaign_id=campaign.id,
            stage=stage,
            policy_version=campaign.policy_version,
        )


def _verify_artifact(
    artifact: Artifact,
    request: StageRequest,
    result: ProviderResult,
) -> None:
    expected = {
        "byte_size": result.byte_size,
        "campaign_id": request.campaign_id,
        "kind": result.artifact_kind,
        "mime_type": result.mime_type,
        "prompt_template_version": "i3-stage-request-v1",
        "provider_model": result.model,
        "provider_name": result.provider,
        "provenance_json": result.output_json,
        "sha256": result.output_hash,
        "source_stage": request.stage,
        "uri": result.artifact_uri,
    }
    actual = {key: getattr(artifact, key) for key in expected}
    if actual != expected:
        raise WorkflowReplayConflict("Existing artifact conflicts with deterministic replay content")


def _verify_job(
    job: GenerationJob,
    request: StageRequest,
    result: ProviderResult,
    artifact_id: int,
) -> None:
    expected = {
        "attempt": 1,
        "campaign_id": request.campaign_id,
        "cost_microunits": 0,
        "error_json": None,
        "input_hash": result.input_hash,
        "model": result.model,
        "output_artifact_id": artifact_id,
        "provider": result.provider,
        "provider_job_id": result.provider_job_id,
        "scene_id": None,
        "status": "completed",
        "usage_json": result.usage_json,
    }
    actual = {key: getattr(job, key) for key in expected}
    if actual != expected:
        raise WorkflowReplayConflict("Existing generation job conflicts with deterministic replay content")


def _reconcile_provider_effects(
    db: Session,
    request: StageRequest,
    result: ProviderResult,
    *,
    allow_create: bool,
) -> tuple[GenerationJob, Artifact]:
    if result.input_hash != request.input_hash:
        raise WorkflowReplayConflict("Provider input hash does not match the stage request")
    if result.cost_microunits != 0:
        raise WorkflowReplayConflict("I3 stub generation cost must be zero")

    artifact = db.scalar(
        select(Artifact).where(
            Artifact.campaign_id == request.campaign_id,
            Artifact.kind == result.artifact_kind,
            Artifact.sha256 == result.output_hash,
        )
    )
    artifact_is_new = artifact is None
    if artifact is None:
        if not allow_create:
            raise WorkflowReplayConflict(
                "Existing gate decision is missing its deterministic artifact"
            )
        artifact = Artifact(
            campaign_id=request.campaign_id,
            kind=result.artifact_kind,
            uri=result.artifact_uri,
            sha256=result.output_hash,
            byte_size=result.byte_size,
            mime_type=result.mime_type,
            source_stage=request.stage,
            provider_name=result.provider,
            provider_model=result.model,
            prompt_template_version="i3-stage-request-v1",
            provenance_json=result.output_json,
        )
        db.add(artifact)
        db.flush()
    else:
        _verify_artifact(artifact, request, result)

    job = db.scalar(
        select(GenerationJob).where(
            GenerationJob.campaign_id == request.campaign_id,
            GenerationJob.provider == result.provider,
            GenerationJob.model == result.model,
            GenerationJob.input_hash == result.input_hash,
            GenerationJob.attempt == 1,
        )
    )
    if job is None:
        if not allow_create:
            raise WorkflowReplayConflict(
                "Existing gate decision is missing its deterministic generation job"
            )
        job = GenerationJob(
            campaign_id=request.campaign_id,
            scene_id=None,
            provider=result.provider,
            model=result.model,
            attempt=1,
            status="completed",
            input_hash=result.input_hash,
            output_artifact_id=artifact.id,
            provider_job_id=result.provider_job_id,
            usage_json=result.usage_json,
            cost_microunits=0,
            error_json=None,
            completed_at=datetime.utcnow(),
        )
        db.add(job)
        db.flush()
    else:
        _verify_job(job, request, result, artifact.id)

    if not artifact_is_new:
        _verify_artifact(artifact, request, result)
    return job, artifact


def _verify_gate(gate: GateDecision, decision: GateResult) -> None:
    expected_reasons = canonical_json(list(decision.reasons))
    if (
        gate.outcome != decision.outcome
        or gate.output_hash != decision.output_hash
        or gate.reasons_json != expected_reasons
    ):
        raise WorkflowReplayConflict("Existing gate decision conflicts with deterministic replay content")


def _validate_decision(
    request: StageRequest,
    provider_result: ProviderResult | None,
    decision: GateResult,
) -> None:
    if decision.input_hash != request.input_hash:
        raise WorkflowReplayConflict("Gate input hash does not match the stage request")
    if decision.policy_version != request.policy_version:
        raise WorkflowReplayConflict("Gate policy version does not match the stage request")
    if provider_result is None and decision.output_hash is not None:
        raise WorkflowReplayConflict("Gate output hash exists without a provider result")
    if provider_result is not None and decision.output_hash != provider_result.output_hash:
        raise WorkflowReplayConflict("Gate output hash does not match the provider result")
    if request.stage == "human_approval" and decision.outcome != "NEEDS_HUMAN":
        raise WorkflowTransitionError("Human approval must remain NEEDS_HUMAN during I3")
    if request.stage == "release":
        raise WorkflowTransitionError("I3 cannot persist a release-stage transition")


def _persist_once(
    request: StageRequest,
    provider_result: ProviderResult | None,
    decision: GateResult,
) -> PersistedStageResult:
    _validate_decision(request, provider_result, decision)

    with SessionLocal() as db:
        with db.begin():
            campaign = db.get(Campaign, request.campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {request.campaign_id} does not exist")
            if campaign.policy_version != request.policy_version:
                raise WorkflowReplayConflict(
                    "Campaign policy version changed during deterministic stage execution"
                )

            persisted_stage = _stage_name(campaign.current_stage)
            existing_gate = db.scalar(
                select(GateDecision).where(
                    GateDecision.campaign_id == request.campaign_id,
                    GateDecision.stage == request.stage,
                    GateDecision.policy_version == request.policy_version,
                    GateDecision.input_hash == request.input_hash,
                )
            )

            job: GenerationJob | None = None
            artifact: Artifact | None = None
            if existing_gate is not None:
                _verify_gate(existing_gate, decision)
                if provider_result is not None:
                    job, artifact = _reconcile_provider_effects(
                        db,
                        request,
                        provider_result,
                        allow_create=False,
                    )

                expected_stage = (
                    NEXT_STAGE[request.stage]
                    if decision.outcome == "PASS"
                    else request.stage
                )
                if persisted_stage != expected_stage:
                    raise WorkflowReplayConflict(
                        "Existing gate decision is inconsistent with campaign stage"
                    )
                return PersistedStageResult(
                    campaign_id=request.campaign_id,
                    stage=request.stage,
                    outcome=decision.outcome,
                    current_stage=persisted_stage,
                    generation_job_id=job.id if job else None,
                    artifact_id=artifact.id if artifact else None,
                    gate_decision_id=existing_gate.id,
                    input_hash=request.input_hash,
                    output_hash=decision.output_hash,
                    replayed=True,
                )

            if persisted_stage != request.stage:
                raise WorkflowTransitionError(
                    f"Campaign stage is {persisted_stage}; expected {request.stage}"
                )

            if provider_result is not None:
                job, artifact = _reconcile_provider_effects(
                    db,
                    request,
                    provider_result,
                    allow_create=True,
                )

            gate = GateDecision(
                campaign_id=request.campaign_id,
                stage=request.stage,
                outcome=decision.outcome,
                policy_version=request.policy_version,
                input_hash=request.input_hash,
                output_hash=decision.output_hash,
                reasons_json=canonical_json(list(decision.reasons)),
            )
            db.add(gate)
            db.flush()

            current_stage = request.stage
            if decision.outcome == "PASS":
                next_stage = NEXT_STAGE.get(request.stage)
                if next_stage is None or next_stage == "release":
                    raise WorkflowTransitionError("I3 cannot advance into release")
                advanced = db.execute(
                    update(Campaign)
                    .where(
                        Campaign.id == request.campaign_id,
                        Campaign.current_stage == request.stage,
                    )
                    .values(current_stage=next_stage, updated_at=datetime.utcnow())
                )
                if advanced.rowcount != 1:
                    raise WorkflowTransitionError("Campaign compare-and-set stage advance failed")
                current_stage = next_stage

            return PersistedStageResult(
                campaign_id=request.campaign_id,
                stage=request.stage,
                outcome=decision.outcome,
                current_stage=current_stage,
                generation_job_id=job.id if job else None,
                artifact_id=artifact.id if artifact else None,
                gate_decision_id=gate.id,
                input_hash=request.input_hash,
                output_hash=decision.output_hash,
                replayed=False,
            )


def persist_stage_result(
    request: StageRequest,
    provider_result: ProviderResult | None,
    decision: GateResult,
) -> PersistedStageResult:
    """Atomically persist/reconcile one deterministic application transition."""

    try:
        return _persist_once(request, provider_result, decision)
    except IntegrityError:
        try:
            return _persist_once(request, provider_result, decision)
        except IntegrityError as exc:
            raise WorkflowReplayConflict(
                "Deterministic replay identity violated a uniqueness constraint"
            ) from exc
