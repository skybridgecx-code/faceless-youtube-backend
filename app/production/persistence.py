from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import re
from typing import Iterator, Mapping, Sequence, cast

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import (
    Artifact,
    Campaign,
    CampaignBudgetOverride,
    GateDecision,
    GenerationJob,
    Scene,
    SessionLocal,
)
from app.editorial.contracts import canonical_json, canonical_sha256
from app.editorial.persistence import reconcile_accepted_i4_script_packet
from app.workflows.persistence import (
    CampaignNotFoundError,
    WorkflowReplayConflict,
    WorkflowTransitionError,
)

from .budget import (
    BudgetChoice,
    BudgetDecision,
    BudgetOption,
    EffectiveCampaignCost,
    budget_override_hash,
    budget_override_payload,
    choose_budget_option,
    effective_campaign_cost,
    load_campaign_budget_policy,
)
from .profile import (
    I5_BUDGET_GATE_POLICY_VERSION,
    I5_PRODUCTION_POLICY_VERSION,
    I5_PRODUCTION_PROFILE_KIND,
    ProductionProfile,
    production_workflow_id,
)
from .providers import is_valid_sora_provider_job_id


I5_STORYBOARD_KIND = "i5_storyboard"
I5_MEDIA_PLAN_KIND = "i5_media_plan"
I5_BUDGET_BLOCK_KIND = "i5_budget_block"
I5_MEDIA_MANIFEST_KIND = "i5_media_manifest"
I5_VOICEOVER_KIND = "i5_voiceover"
I5_FINAL_RENDER_KIND = "i5_final_render"
I5_ASSEMBLY_MANIFEST_KIND = "i5_assembly_manifest"

_I5_WORKFLOW_PATTERN = re.compile(
    r"^campaign:(?P<campaign_id>[1-9][0-9]*):i5:"
    r"(?P<script_hash>[0-9a-f]{64}):(?P<profile_hash>[0-9a-f]{64})$"
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@contextmanager
def serialized_transaction(db: Session) -> Iterator[None]:
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        db.begin()
    try:
        yield
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()


def campaign_for_update(db: Session, campaign_id: int) -> Campaign | None:
    return db.scalar(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    )


def _json_artifact_values(
    *,
    campaign_id: int,
    kind: str,
    source_stage: str,
    payload: Mapping[str, object],
    input_hash: str,
    provider_name: str,
    provider_model: str,
) -> dict[str, object]:
    payload_dict = dict(payload)
    payload_json = canonical_json(payload_dict)
    output_hash = canonical_sha256(payload_dict)
    return {
        "byte_size": len(payload_json.encode("utf-8")),
        "campaign_id": campaign_id,
        "kind": kind,
        "mime_type": "application/json",
        "payload_json": payload_json,
        "prompt_template_version": f"{provider_model}-contract",
        "provenance_json": canonical_json(
            {
                "contract_version": "i5-artifact-provenance-v1",
                "hash_scope": "exact_payload_json_utf8_bytes",
                "immutable": True,
                "input_hash": input_hash,
                "origin": "i5_durable_production_workflow",
            }
        ),
        "provider_model": provider_model,
        "provider_name": provider_name,
        "sha256": output_hash,
        "source_stage": source_stage,
        "uri": f"artifact://campaign/{campaign_id}/{kind}/{output_hash}",
    }


def binary_artifact_values(
    *,
    campaign_id: int,
    kind: str,
    source_stage: str,
    uri: str,
    sha256: str,
    byte_size: int,
    mime_type: str,
    provider_name: str,
    provider_model: str,
    input_hash: str,
    provenance: Mapping[str, object],
) -> dict[str, object]:
    if _SHA256_PATTERN.fullmatch(sha256) is None or byte_size <= 0:
        raise WorkflowReplayConflict("I5 binary artifact identity is invalid")
    custom_provenance = dict(provenance)
    reserved_provenance_fields = {
        "content_sha256",
        "contract_version",
        "generation_input_hash",
        "hash_scope",
        "immutable",
        "origin_class",
        "provider_model",
        "provider_name",
        "prompt_template_version",
        "source_class",
    }
    if reserved_provenance_fields.intersection(custom_provenance):
        raise WorkflowReplayConflict("I5 binary provenance overrides a reserved field")
    disclosure_state = str(
        custom_provenance.pop("disclosure_state", "not_required")
    )
    if not disclosure_state:
        raise WorkflowReplayConflict("I5 binary disclosure state is invalid")
    prompt_template_version = "i5-media-request-v1"
    provider_is_generated = provider_name.startswith("openai_")
    provenance_payload = {
        "content_sha256": sha256,
        "contract_version": "i5-binary-artifact-provenance-v1",
        "disclosure_state": disclosure_state,
        "generation_input_hash": input_hash,
        "hash_scope": "exact_binary_bytes",
        "immutable": True,
        "origin_class": "generated" if provider_is_generated else "original",
        "provider_model": provider_model,
        "provider_name": provider_name,
        "prompt_template_version": prompt_template_version,
        "source_class": (
            "metered_provider_output"
            if provider_is_generated
            else "deterministic_local_output"
        ),
        **custom_provenance,
    }
    return {
        "byte_size": byte_size,
        "campaign_id": campaign_id,
        "kind": kind,
        "mime_type": mime_type,
        "payload_json": None,
        "prompt_template_version": prompt_template_version,
        "provenance_json": canonical_json(provenance_payload),
        "provider_model": provider_model,
        "provider_name": provider_name,
        "sha256": sha256,
        "source_stage": source_stage,
        "uri": uri,
    }


def _verify_fields(row: object, expected: Mapping[str, object], *, label: str) -> None:
    actual = {key: getattr(row, key) for key in expected}
    if actual != dict(expected):
        raise WorkflowReplayConflict(f"Existing {label} conflicts with immutable content")


def _decode_json_artifact(artifact: Artifact) -> dict[str, object]:
    if artifact.payload_json is None:
        raise WorkflowReplayConflict(f"{artifact.kind} artifact has no canonical payload")
    try:
        payload = json.loads(artifact.payload_json)
    except json.JSONDecodeError as exc:
        raise WorkflowReplayConflict(f"{artifact.kind} payload is invalid") from exc
    if not isinstance(payload, dict):
        raise WorkflowReplayConflict(f"{artifact.kind} payload must be an object")
    if (
        canonical_json(payload) != artifact.payload_json
        or canonical_sha256(payload) != artifact.sha256
    ):
        raise WorkflowReplayConflict(f"{artifact.kind} payload hash is invalid")
    return payload


def _reconcile_artifact(
    db: Session,
    expected: Mapping[str, object],
    *,
    allow_create: bool,
) -> Artifact:
    artifact = db.scalar(
        select(Artifact).where(
            Artifact.campaign_id == expected["campaign_id"],
            Artifact.kind == expected["kind"],
            Artifact.sha256 == expected["sha256"],
        )
    )
    if artifact is None:
        if not allow_create:
            raise WorkflowReplayConflict("Committed I5 effect is missing its artifact")
        artifact = Artifact(**expected)
        db.add(artifact)
        db.flush()
    else:
        _verify_fields(artifact, expected, label=str(expected["kind"]))
    return artifact


def _profile_expected_values(
    campaign_id: int,
    profile: ProductionProfile,
) -> dict[str, object]:
    input_hash = canonical_sha256(
        {
            "contract_version": "i5-production-profile-input-v1",
            "i4_script_sha256": profile.payload["i4_script_sha256"],
            "production_profile_sha256": profile.sha256,
        }
    )
    expected = _json_artifact_values(
        campaign_id=campaign_id,
        kind=I5_PRODUCTION_PROFILE_KIND,
        source_stage="storyboard",
        payload=profile.payload,
        input_hash=input_hash,
        provider_name="operator_configuration+local_runtime",
        provider_model="i5-production-profile-v1",
    )
    if expected["sha256"] != profile.sha256:
        raise WorkflowReplayConflict("Production profile hash is invalid")
    return expected


def bind_i5_production_profile(
    campaign_id: int,
    profile: ProductionProfile,
) -> dict[str, object]:
    """Persist the immutable profile and bind the separate I5 identity atomically."""

    expected_artifact = _profile_expected_values(campaign_id, profile)
    expected_workflow_id = production_workflow_id(
        campaign_id,
        str(profile.payload["i4_script_sha256"]),
        profile.sha256,
    )
    with SessionLocal() as db:
        with serialized_transaction(db):
            campaign = campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            accepted_script = reconcile_accepted_i4_script_packet(db, campaign_id)
            if accepted_script["script_hash"] != profile.payload["i4_script_sha256"]:
                raise WorkflowReplayConflict(
                    "Production profile does not bind the accepted I4 script"
                )

            profile_artifacts = list(
                db.scalars(
                    select(Artifact).where(
                        Artifact.campaign_id == campaign_id,
                        Artifact.kind == I5_PRODUCTION_PROFILE_KIND,
                    )
                )
            )
            if any(row.sha256 != profile.sha256 for row in profile_artifacts):
                raise WorkflowReplayConflict(
                    "Campaign contains a conflicting immutable production profile"
                )

            if campaign.production_workflow_id is None:
                if campaign.current_stage != "storyboard":
                    raise WorkflowTransitionError(
                        "An unbound I5 campaign must begin at storyboard"
                    )
                artifact = _reconcile_artifact(
                    db,
                    expected_artifact,
                    allow_create=True,
                )
                bound = db.execute(
                    update(Campaign)
                    .where(
                        Campaign.id == campaign_id,
                        Campaign.production_workflow_id.is_(None),
                        Campaign.current_stage == "storyboard",
                    )
                    .values(
                        production_workflow_id=expected_workflow_id,
                        updated_at=datetime.utcnow(),
                    )
                )
                if bound.rowcount != 1:
                    raise WorkflowReplayConflict(
                        "Campaign production identity changed concurrently"
                    )
            else:
                if campaign.production_workflow_id != expected_workflow_id:
                    raise WorkflowReplayConflict(
                        "Campaign is already bound to a different production profile"
                    )
                artifact = _reconcile_artifact(
                    db,
                    expected_artifact,
                    allow_create=False,
                )
            return {
                "campaign_id": campaign_id,
                "created": len(profile_artifacts) == 0,
                "i4_script_payload": accepted_script["payload"],
                "i4_script_sha256": accepted_script["script_hash"],
                "production_profile_artifact_id": artifact.id,
                "production_profile_payload": profile.payload,
                "production_profile_sha256": profile.sha256,
                "production_workflow_id": expected_workflow_id,
            }


def _bound_context(db: Session, campaign_id: int) -> dict[str, object]:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
    match = _I5_WORKFLOW_PATTERN.fullmatch(campaign.production_workflow_id or "")
    if match is None or int(match.group("campaign_id")) != campaign_id:
        raise WorkflowReplayConflict("Campaign is not bound to a canonical I5 workflow")
    script = reconcile_accepted_i4_script_packet(db, campaign_id)
    if script["script_hash"] != match.group("script_hash"):
        raise WorkflowReplayConflict("Bound I5 script lineage is invalid")
    profile_hash = match.group("profile_hash")
    profile_artifacts = list(
        db.scalars(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_PRODUCTION_PROFILE_KIND,
            )
        )
    )
    if len(profile_artifacts) != 1 or profile_artifacts[0].sha256 != profile_hash:
        raise WorkflowReplayConflict("Bound I5 production profile is missing or ambiguous")
    profile_artifact = profile_artifacts[0]
    profile_payload = _decode_json_artifact(profile_artifact)
    expected_profile = ProductionProfile(payload=profile_payload, sha256=profile_hash)
    _verify_fields(
        profile_artifact,
        _profile_expected_values(campaign_id, expected_profile),
        label=I5_PRODUCTION_PROFILE_KIND,
    )
    if profile_payload.get("i4_script_sha256") != script["script_hash"]:
        raise WorkflowReplayConflict("Production profile script lineage is invalid")
    return {
        "campaign": campaign,
        "profile_artifact": profile_artifact,
        "profile_hash": profile_hash,
        "profile_payload": profile_payload,
        "script_hash": script["script_hash"],
        "script_payload": script["payload"],
        "workflow_id": campaign.production_workflow_id,
    }


def load_bound_i5_context(campaign_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        context = _bound_context(db, campaign_id)
        campaign = cast(Campaign, context["campaign"])
        return {
            **{key: value for key, value in context.items() if key != "campaign"},
            "current_stage": campaign.current_stage,
        }


def _zero_cost_job_values(
    *,
    campaign_id: int,
    scene_id: int | None,
    provider: str,
    model: str,
    input_hash: str,
    output_artifact_id: int,
) -> dict[str, object]:
    return {
        "attempt": 1,
        "campaign_id": campaign_id,
        "cost_microunits": 0,
        "error_json": None,
        "input_hash": input_hash,
        "model": model,
        "output_artifact_id": output_artifact_id,
        "provider": provider,
        "provider_job_id": f"i5:local:{input_hash}",
        "reserved_cost_microunits": 0,
        "scene_id": scene_id,
        "status": "completed",
        "usage_json": canonical_json({"metered_cost_microunits": 0}),
    }


def _reconcile_job(
    db: Session,
    expected: Mapping[str, object],
    *,
    allow_create: bool,
) -> GenerationJob:
    job = db.scalar(
        select(GenerationJob).where(
            GenerationJob.campaign_id == expected["campaign_id"],
            GenerationJob.provider == expected["provider"],
            GenerationJob.model == expected["model"],
            GenerationJob.input_hash == expected["input_hash"],
            GenerationJob.attempt == expected["attempt"],
        )
    )
    if job is None:
        if not allow_create:
            raise WorkflowReplayConflict("Committed I5 effect is missing its generation job")
        job = GenerationJob(**expected, completed_at=datetime.utcnow())
        db.add(job)
        db.flush()
    else:
        _verify_fields(job, expected, label="I5 generation job")
    return job


def _gate_values(
    *,
    campaign_id: int,
    stage: str,
    policy_version: str,
    input_hash: str,
    outcome: str,
    output_hash: str | None,
    reasons: Sequence[str],
) -> dict[str, object]:
    return {
        "campaign_id": campaign_id,
        "input_hash": input_hash,
        "outcome": outcome,
        "output_hash": output_hash,
        "policy_version": policy_version,
        "reasons_json": canonical_json(list(reasons)),
        "stage": stage,
    }


def _reconcile_gate(
    db: Session,
    expected: Mapping[str, object],
    *,
    allow_create: bool,
) -> GateDecision:
    gate = db.scalar(
        select(GateDecision).where(
            GateDecision.campaign_id == expected["campaign_id"],
            GateDecision.stage == expected["stage"],
            GateDecision.policy_version == expected["policy_version"],
            GateDecision.input_hash == expected["input_hash"],
        )
    )
    if gate is None:
        if not allow_create:
            raise WorkflowReplayConflict("Committed I5 effect is missing its gate")
        gate = GateDecision(**expected)
        db.add(gate)
        db.flush()
    else:
        _verify_fields(gate, expected, label="I5 gate decision")
    return gate


def _scene_values(
    campaign_id: int,
    storyboard_artifact_id: int,
    scene: Mapping[str, object],
) -> dict[str, object]:
    narration_hash = str(scene.get("narration_sha256") or "")
    if _SHA256_PATTERN.fullmatch(narration_hash) is None:
        raise WorkflowReplayConflict("Storyboard scene narration hash is invalid")
    return {
        "campaign_id": campaign_id,
        "disclosure_state": scene["disclosure_state"],
        "narration_reference": narration_hash,
        "overlay_spec_json": canonical_json(dict(scene)),
        "position": int(scene["position"]),
        "storyboard_artifact_id": storyboard_artifact_id,
        "visual_mode": scene["visual_mode"],
    }


def _reconcile_scenes(
    db: Session,
    *,
    campaign_id: int,
    storyboard_artifact_id: int,
    scenes: Sequence[Mapping[str, object]],
    allow_create: bool,
) -> list[Scene]:
    positions = [int(scene["position"]) for scene in scenes]
    if positions != list(range(len(scenes))):
        raise WorkflowReplayConflict("Storyboard scene positions are not canonical")
    existing = list(
        db.scalars(
            select(Scene)
            .where(Scene.campaign_id == campaign_id)
            .order_by(Scene.position)
        )
    )
    if existing and len(existing) != len(scenes):
        raise WorkflowReplayConflict("Persisted storyboard scene count conflicts")
    by_position = {row.position: row for row in existing}
    rows: list[Scene] = []
    for scene_payload in scenes:
        expected = _scene_values(
            campaign_id,
            storyboard_artifact_id,
            scene_payload,
        )
        row = by_position.get(int(expected["position"]))
        if row is None:
            if not allow_create:
                raise WorkflowReplayConflict("Committed storyboard is missing a scene")
            row = Scene(**expected)
            db.add(row)
            db.flush()
        else:
            _verify_fields(row, expected, label="I5 storyboard scene")
        rows.append(row)
    return rows


def persist_storyboard(packet: dict[str, object]) -> dict[str, object]:
    campaign_id = int(packet.get("campaign_id") or 0)
    input_hash = canonical_sha256(
        {
            "contract_version": "i5-storyboard-stage-input-v1",
            "production_profile_hash": packet.get("production_profile_hash"),
            "script_hash": packet.get("script_hash"),
        }
    )
    expected_artifact = _json_artifact_values(
        campaign_id=campaign_id,
        kind=I5_STORYBOARD_KIND,
        source_stage="storyboard",
        payload=packet,
        input_hash=input_hash,
        provider_name="deterministic_editorial",
        provider_model="i5-storyboard-compiler-v1",
    )
    gate_payload = dict(packet.get("gate") or {})
    outcome = str(gate_payload.get("outcome") or "")
    reasons = tuple(str(value) for value in gate_payload.get("reasons", []))
    if outcome not in {"PASS", "FAIL"}:
        raise WorkflowTransitionError("I5 storyboard gate must be PASS or FAIL")

    from .storyboard import compile_storyboard

    with SessionLocal() as db:
        with serialized_transaction(db):
            campaign = campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            context = _bound_context(db, campaign_id)
            if packet.get("script_hash") != context["script_hash"]:
                raise WorkflowReplayConflict("Storyboard script lineage is invalid")
            if packet.get("production_profile_hash") != context["profile_hash"]:
                raise WorkflowReplayConflict("Storyboard profile lineage is invalid")
            video_profile = dict(
                cast(Mapping[str, object], context["profile_payload"])["video"]  # type: ignore[index]
            )
            expected_packet = compile_storyboard(
                campaign_id=campaign_id,
                script_packet=cast(dict[str, object], context["script_payload"]),
                script_hash=str(context["script_hash"]),
                production_profile_hash=str(context["profile_hash"]),
                generated_cinematic_enabled=True,
                generated_video_enabled=(
                    video_profile.get("provider") != "disabled"
                    and video_profile.get("allow_deprecated_sora") is True
                ),
            )
            if packet != expected_packet:
                raise WorkflowReplayConflict(
                    "Storyboard does not match deterministic compilation"
                )

            gate_expected = _gate_values(
                campaign_id=campaign_id,
                stage="storyboard",
                policy_version=I5_PRODUCTION_POLICY_VERSION,
                input_hash=input_hash,
                outcome=outcome,
                output_hash=str(expected_artifact["sha256"]),
                reasons=reasons,
            )
            existing_gate = db.scalar(
                select(GateDecision).where(
                    GateDecision.campaign_id == campaign_id,
                    GateDecision.stage == "storyboard",
                    GateDecision.policy_version == I5_PRODUCTION_POLICY_VERSION,
                    GateDecision.input_hash == input_hash,
                )
            )
            replayed = existing_gate is not None
            storyboard_artifacts = list(
                db.scalars(
                    select(Artifact).where(
                        Artifact.campaign_id == campaign_id,
                        Artifact.kind == I5_STORYBOARD_KIND,
                    )
                )
            )
            storyboard_jobs = list(
                db.scalars(
                    select(GenerationJob).where(
                        GenerationJob.campaign_id == campaign_id,
                        GenerationJob.provider == "deterministic_editorial",
                        GenerationJob.model == "i5-storyboard-compiler-v1",
                    )
                )
            )
            storyboard_gates = list(
                db.scalars(
                    select(GateDecision).where(
                        GateDecision.campaign_id == campaign_id,
                        GateDecision.stage == "storyboard",
                    )
                )
            )
            effect_counts = (
                len(storyboard_artifacts),
                len(storyboard_jobs),
                len(storyboard_gates),
            )
            if replayed:
                if effect_counts != (1, 1, 1):
                    raise WorkflowReplayConflict(
                        "Committed storyboard effect-set cardinality is invalid"
                    )
            elif effect_counts != (0, 0, 0):
                raise WorkflowReplayConflict(
                    "Partial storyboard effects exist without the canonical gate"
                )
            artifact = _reconcile_artifact(
                db,
                expected_artifact,
                allow_create=not replayed,
            )
            scenes = _reconcile_scenes(
                db,
                campaign_id=campaign_id,
                storyboard_artifact_id=artifact.id,
                scenes=cast(Sequence[Mapping[str, object]], packet.get("scenes", [])),
                allow_create=not replayed,
            )
            job = _reconcile_job(
                db,
                _zero_cost_job_values(
                    campaign_id=campaign_id,
                    scene_id=None,
                    provider="deterministic_editorial",
                    model="i5-storyboard-compiler-v1",
                    input_hash=input_hash,
                    output_artifact_id=artifact.id,
                ),
                allow_create=not replayed,
            )
            gate = _reconcile_gate(
                db,
                gate_expected,
                allow_create=not replayed,
            )

            expected_stage = "media" if outcome == "PASS" else "storyboard"
            if replayed:
                if campaign.current_stage != expected_stage:
                    raise WorkflowReplayConflict(
                        "Committed storyboard gate conflicts with campaign stage"
                    )
            else:
                if campaign.current_stage != "storyboard":
                    raise WorkflowTransitionError(
                        f"Campaign stage is {campaign.current_stage}; expected storyboard"
                    )
                if outcome == "PASS":
                    advanced = db.execute(
                        update(Campaign)
                        .where(
                            Campaign.id == campaign_id,
                            Campaign.current_stage == "storyboard",
                        )
                        .values(current_stage="media", updated_at=datetime.utcnow())
                    )
                    if advanced.rowcount != 1:
                        raise WorkflowReplayConflict(
                            "Storyboard compare-and-set advancement failed"
                        )
            return {
                "artifact_id": artifact.id,
                "campaign_id": campaign_id,
                "current_stage": expected_stage,
                "gate_decision_id": gate.id,
                "generation_job_id": job.id,
                "outcome": outcome,
                "output_hash": artifact.sha256,
                "replayed": replayed,
                "scene_count": len(scenes),
                "stage": "storyboard",
            }


def reconcile_storyboard_effect_set(
    db: Session,
    campaign_id: int,
) -> dict[str, object]:
    context = _bound_context(db, campaign_id)
    artifacts = list(
        db.scalars(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_STORYBOARD_KIND,
            )
        )
    )
    if len(artifacts) != 1:
        raise WorkflowReplayConflict(
            "Campaign must contain exactly one accepted I5 storyboard"
        )
    artifact = artifacts[0]
    packet = _decode_json_artifact(artifact)
    from .storyboard import compile_storyboard

    video_profile = dict(
        cast(Mapping[str, object], context["profile_payload"])["video"]  # type: ignore[index]
    )
    expected_packet = compile_storyboard(
        campaign_id=campaign_id,
        script_packet=cast(dict[str, object], context["script_payload"]),
        script_hash=str(context["script_hash"]),
        production_profile_hash=str(context["profile_hash"]),
        generated_cinematic_enabled=True,
        generated_video_enabled=(
            video_profile.get("provider") != "disabled"
            and video_profile.get("allow_deprecated_sora") is True
        ),
    )
    if packet != expected_packet:
        raise WorkflowReplayConflict(
            "Accepted storyboard no longer matches deterministic compilation"
        )
    input_hash = canonical_sha256(
        {
            "contract_version": "i5-storyboard-stage-input-v1",
            "production_profile_hash": context["profile_hash"],
            "script_hash": context["script_hash"],
        }
    )
    artifact_expected = _json_artifact_values(
        campaign_id=campaign_id,
        kind=I5_STORYBOARD_KIND,
        source_stage="storyboard",
        payload=packet,
        input_hash=input_hash,
        provider_name="deterministic_editorial",
        provider_model="i5-storyboard-compiler-v1",
    )
    _verify_fields(artifact, artifact_expected, label=I5_STORYBOARD_KIND)
    gate_payload = dict(packet.get("gate") or {})
    if gate_payload.get("outcome") != "PASS":
        raise WorkflowTransitionError("I5 storyboard gate is not PASS")
    gate = _reconcile_gate(
        db,
        _gate_values(
            campaign_id=campaign_id,
            stage="storyboard",
            policy_version=I5_PRODUCTION_POLICY_VERSION,
            input_hash=input_hash,
            outcome="PASS",
            output_hash=artifact.sha256,
            reasons=tuple(str(value) for value in gate_payload.get("reasons", [])),
        ),
        allow_create=False,
    )
    job = _reconcile_job(
        db,
        _zero_cost_job_values(
            campaign_id=campaign_id,
            scene_id=None,
            provider="deterministic_editorial",
            model="i5-storyboard-compiler-v1",
            input_hash=input_hash,
            output_artifact_id=artifact.id,
        ),
        allow_create=False,
    )
    storyboard_gates = list(
        db.scalars(
            select(GateDecision).where(
                GateDecision.campaign_id == campaign_id,
                GateDecision.stage == "storyboard",
            )
        )
    )
    storyboard_jobs = list(
        db.scalars(
            select(GenerationJob).where(
                GenerationJob.campaign_id == campaign_id,
                GenerationJob.provider == "deterministic_editorial",
                GenerationJob.model == "i5-storyboard-compiler-v1",
            )
        )
    )
    if (
        len(storyboard_gates) != 1
        or storyboard_gates[0].id != gate.id
        or len(storyboard_jobs) != 1
        or storyboard_jobs[0].id != job.id
    ):
        raise WorkflowReplayConflict("Accepted storyboard effect set is not exact")
    scenes = _reconcile_scenes(
        db,
        campaign_id=campaign_id,
        storyboard_artifact_id=artifact.id,
        scenes=cast(Sequence[Mapping[str, object]], packet.get("scenes", [])),
        allow_create=False,
    )
    return {
        **context,
        "storyboard_artifact": artifact,
        "storyboard_gate": gate,
        "storyboard_hash": artifact.sha256,
        "storyboard_job": job,
        "storyboard_packet": packet,
        "scenes": scenes,
    }


def _override_expected(row: CampaignBudgetOverride) -> dict[str, object]:
    payload = budget_override_payload(
        campaign_id=row.campaign_id,
        policy_version=row.policy_version,
        previous_authorized_cap_microunits=row.previous_authorized_cap_microunits,
        new_authorized_cap_microunits=row.new_authorized_cap_microunits,
        actor=row.actor,
        reason=row.reason,
        timestamp=row.created_at,
    )
    return {"payload": payload, "override_hash": budget_override_hash(payload)}


def authorized_campaign_cap(db: Session, campaign_id: int) -> int:
    policy = load_campaign_budget_policy()
    current = policy.limits_microusd.default_hard_cap
    overrides = list(
        db.scalars(
            select(CampaignBudgetOverride)
            .where(CampaignBudgetOverride.campaign_id == campaign_id)
            .order_by(CampaignBudgetOverride.id)
        )
    )
    seen: set[str] = set()
    for row in overrides:
        expected = _override_expected(row)
        if (
            row.policy_version != policy.policy_version
            or row.previous_authorized_cap_microunits != current
            or row.new_authorized_cap_microunits <= current
            or row.override_hash != expected["override_hash"]
            or row.override_hash in seen
        ):
            raise WorkflowReplayConflict("Campaign budget override chain is invalid")
        seen.add(row.override_hash)
        current = row.new_authorized_cap_microunits
    return current


def create_budget_override(
    *,
    campaign_id: int,
    new_authorized_cap_microusd: int,
    actor: str,
    reason: str,
) -> dict[str, object]:
    policy = load_campaign_budget_policy()
    normalized_actor = actor.strip()
    normalized_reason = reason.strip()
    with SessionLocal() as db:
        with serialized_transaction(db):
            campaign = campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            _bound_context(db, campaign_id)
            semantic_matches = list(
                db.scalars(
                    select(CampaignBudgetOverride).where(
                        CampaignBudgetOverride.campaign_id == campaign_id,
                        CampaignBudgetOverride.new_authorized_cap_microunits
                        == new_authorized_cap_microusd,
                        CampaignBudgetOverride.actor == normalized_actor,
                        CampaignBudgetOverride.reason == normalized_reason,
                    )
                )
            )
            if len(semantic_matches) > 1:
                raise WorkflowReplayConflict("Budget override replay identity is ambiguous")
            current = authorized_campaign_cap(db, campaign_id)
            if semantic_matches:
                row = semantic_matches[0]
                expected = _override_expected(row)
                if row.override_hash != expected["override_hash"]:
                    raise WorkflowReplayConflict("Budget override replay content conflicts")
                return {
                    "created": False,
                    "new_authorized_cap_microusd": row.new_authorized_cap_microunits,
                    "override_hash": row.override_hash,
                    "override_id": row.id,
                    "previous_authorized_cap_microusd": row.previous_authorized_cap_microunits,
                    "production_workflow_id": campaign.production_workflow_id,
                }
            if campaign.current_stage != "media":
                raise WorkflowTransitionError(
                    "Campaign budget overrides are accepted only while I5 is at media"
                )
            payload = budget_override_payload(
                campaign_id=campaign_id,
                policy_version=policy.policy_version,
                previous_authorized_cap_microunits=current,
                new_authorized_cap_microunits=new_authorized_cap_microusd,
                actor=normalized_actor,
                reason=normalized_reason,
                timestamp=datetime.utcnow(),
            )
            created_at = datetime.fromisoformat(
                str(payload["timestamp"]).replace("Z", "+00:00")
            ).replace(tzinfo=None)
            row = CampaignBudgetOverride(
                campaign_id=campaign_id,
                policy_version=policy.policy_version,
                previous_authorized_cap_microunits=current,
                new_authorized_cap_microunits=new_authorized_cap_microusd,
                actor=normalized_actor,
                reason=normalized_reason,
                override_hash=budget_override_hash(payload),
                created_at=created_at,
            )
            db.add(row)
            db.flush()
            return {
                "created": True,
                "new_authorized_cap_microusd": row.new_authorized_cap_microunits,
                "override_hash": row.override_hash,
                "override_id": row.id,
                "previous_authorized_cap_microusd": row.previous_authorized_cap_microunits,
                "production_workflow_id": campaign.production_workflow_id,
            }


def verify_budget_override_message(
    *,
    campaign_id: int,
    production_workflow_id: str,
    message: Mapping[str, object],
) -> dict[str, object]:
    """Fail closed unless a workflow message names one committed override.

    DBOS notifications are only wake-up signals.  Authorization remains the
    immutable application-database row and its verified append-only cap chain.
    """

    expected_message = {
        "campaign_id": campaign_id,
        "override_hash": str(message.get("override_hash") or ""),
        "production_workflow_id": production_workflow_id,
    }
    if dict(message) != expected_message:
        raise WorkflowReplayConflict("Budget override message is noncanonical")
    override_hash = expected_message["override_hash"]
    if _SHA256_PATTERN.fullmatch(override_hash) is None:
        raise WorkflowReplayConflict("Budget override message hash is invalid")

    with SessionLocal() as db:
        context = _bound_context(db, campaign_id)
        campaign = cast(Campaign, context["campaign"])
        if campaign.production_workflow_id != production_workflow_id:
            raise WorkflowReplayConflict("Budget override workflow lineage is invalid")
        if campaign.current_stage != "media":
            raise WorkflowTransitionError(
                "Budget override messages are accepted only while I5 is at media"
            )
        rows = list(
            db.scalars(
                select(CampaignBudgetOverride).where(
                    CampaignBudgetOverride.campaign_id == campaign_id,
                    CampaignBudgetOverride.override_hash == override_hash,
                )
            )
        )
        if len(rows) != 1:
            raise WorkflowReplayConflict(
                "Budget override message does not identify one committed override"
            )
        row = rows[0]
        expected = _override_expected(row)
        if row.override_hash != expected["override_hash"]:
            raise WorkflowReplayConflict("Budget override row content conflicts")
        current_cap = authorized_campaign_cap(db, campaign_id)
        return {
            "authorized_cap_microusd": current_cap,
            "new_authorized_cap_microusd": row.new_authorized_cap_microunits,
            "override_hash": row.override_hash,
            "override_id": row.id,
        }


def campaign_cost(db: Session, campaign_id: int) -> EffectiveCampaignCost:
    jobs = list(
        db.scalars(
            select(GenerationJob).where(GenerationJob.campaign_id == campaign_id)
        )
    )
    return effective_campaign_cost(jobs)


def persist_budget_block(
    db: Session,
    *,
    campaign_id: int,
    request_identity: Mapping[str, object],
    current_cost: EffectiveCampaignCost,
    authorized_cap_microusd: int,
    projected_cost_microusd: int,
    profile_hash: str,
) -> dict[str, object]:
    policy = load_campaign_budget_policy()
    payload: dict[str, object] = {
        "authorized_cap_microusd": authorized_cap_microusd,
        "budget_policy_sha256": policy.policy_sha256,
        "budget_policy_version": policy.policy_version,
        "campaign_id": campaign_id,
        "committed_cost_microusd": current_cost.committed_microusd,
        "contract_version": "i5-budget-block-v1",
        "decision": BudgetDecision.BLOCK_NEEDS_HUMAN,
        "production_profile_hash": profile_hash,
        "projected_cost_microusd": projected_cost_microusd,
        "request_identity": dict(request_identity),
        "reserved_cost_microusd": current_cost.reserved_microusd,
    }
    input_hash = canonical_sha256(
        {
            "contract_version": "i5-budget-state-input-v1",
            **payload,
        }
    )
    artifact_expected = _json_artifact_values(
        campaign_id=campaign_id,
        kind=I5_BUDGET_BLOCK_KIND,
        source_stage="media",
        payload=payload,
        input_hash=input_hash,
        provider_name="campaign_budget_guard",
        provider_model=I5_BUDGET_GATE_POLICY_VERSION,
    )
    artifact = _reconcile_artifact(db, artifact_expected, allow_create=True)
    gate = _reconcile_gate(
        db,
        _gate_values(
            campaign_id=campaign_id,
            stage="media",
            policy_version=I5_BUDGET_GATE_POLICY_VERSION,
            input_hash=input_hash,
            outcome="NEEDS_HUMAN",
            output_hash=artifact.sha256,
            reasons=("required_metered_request_exceeds_authorized_campaign_cap",),
        ),
        allow_create=True,
    )
    return {
        "artifact_id": artifact.id,
        "gate_decision_id": gate.id,
        "input_hash": input_hash,
        "outcome": "NEEDS_HUMAN",
        "output_hash": artifact.sha256,
    }


def _reconcile_budget_block_gate_ids(
    db: Session,
    *,
    campaign_id: int,
    profile_hash: str,
) -> set[int]:
    policy = load_campaign_budget_policy()
    artifacts = list(
        db.scalars(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_BUDGET_BLOCK_KIND,
            )
        )
    )
    gates = list(
        db.scalars(
            select(GateDecision).where(
                GateDecision.campaign_id == campaign_id,
                GateDecision.stage == "media",
                GateDecision.policy_version == I5_BUDGET_GATE_POLICY_VERSION,
            )
        )
    )
    if len(artifacts) != len(gates):
        raise WorkflowReplayConflict("Budget-block artifact/gate cardinality is invalid")

    context = reconcile_storyboard_effect_set(db, campaign_id)
    requests = _canonical_tts_requests(campaign_id=campaign_id, context=context)
    primary_total = sum(
        (int(request["character_count"]) * 30 * 120 + 99) // 100
        for request in requests
    )
    fallback_total = sum(
        (int(request["character_count"]) * 15 * 120 + 99) // 100
        for request in requests
    )
    allowed_caps = {policy.limits_microusd.default_hard_cap}
    allowed_caps.update(
        db.scalars(
            select(CampaignBudgetOverride.new_authorized_cap_microunits).where(
                CampaignBudgetOverride.campaign_id == campaign_id
            )
        )
    )
    expected_request_identity = {
        "kind": "full_campaign_tts_plan",
        "storyboard_hash": context["storyboard_hash"],
        "tts_fallback_reservation_microusd": fallback_total,
        "tts_primary_reservation_microusd": primary_total,
    }
    gate_ids: set[int] = set()
    expected_keys = {
        "authorized_cap_microusd",
        "budget_policy_sha256",
        "budget_policy_version",
        "campaign_id",
        "committed_cost_microusd",
        "contract_version",
        "decision",
        "production_profile_hash",
        "projected_cost_microusd",
        "request_identity",
        "reserved_cost_microusd",
    }
    for artifact in artifacts:
        payload = _decode_json_artifact(artifact)
        committed = payload.get("committed_cost_microusd")
        reserved = payload.get("reserved_cost_microusd")
        if (
            set(payload) != expected_keys
            or payload.get("campaign_id") != campaign_id
            or payload.get("contract_version") != "i5-budget-block-v1"
            or payload.get("decision") != BudgetDecision.BLOCK_NEEDS_HUMAN.value
            or payload.get("budget_policy_sha256") != policy.policy_sha256
            or payload.get("budget_policy_version") != policy.policy_version
            or payload.get("production_profile_hash") != profile_hash
            or payload.get("request_identity") != expected_request_identity
            or payload.get("authorized_cap_microusd") not in allowed_caps
            or not isinstance(committed, int)
            or not isinstance(reserved, int)
            or committed < 0
            or reserved < 0
            or payload.get("projected_cost_microusd")
            != committed + reserved + primary_total
        ):
            raise WorkflowReplayConflict("Budget-block payload lineage is invalid")
        input_hash = canonical_sha256(
            {"contract_version": "i5-budget-state-input-v1", **payload}
        )
        expected_artifact = _json_artifact_values(
            campaign_id=campaign_id,
            kind=I5_BUDGET_BLOCK_KIND,
            source_stage="media",
            payload=payload,
            input_hash=input_hash,
            provider_name="campaign_budget_guard",
            provider_model=I5_BUDGET_GATE_POLICY_VERSION,
        )
        _verify_fields(
            artifact,
            expected_artifact,
            label="I5 budget-block artifact",
        )
        matching_gates = [gate for gate in gates if gate.input_hash == input_hash]
        if len(matching_gates) != 1:
            raise WorkflowReplayConflict("Budget-block gate identity is not exact")
        gate = matching_gates[0]
        _verify_fields(
            gate,
            _gate_values(
                campaign_id=campaign_id,
                stage="media",
                policy_version=I5_BUDGET_GATE_POLICY_VERSION,
                input_hash=input_hash,
                outcome="NEEDS_HUMAN",
                output_hash=artifact.sha256,
                reasons=(
                    "required_metered_request_exceeds_authorized_campaign_cap",
                ),
            ),
            label="I5 budget-block gate",
        )
        if gate.id in gate_ids:
            raise WorkflowReplayConflict("Budget-block gate is reused ambiguously")
        gate_ids.add(gate.id)
    if gate_ids != {gate.id for gate in gates}:
        raise WorkflowReplayConflict("Budget-block gate effect set is not exact")
    return gate_ids


def _canonical_tts_requests(
    *,
    campaign_id: int,
    context: Mapping[str, object],
) -> list[dict[str, object]]:
    from .media import scene_contract

    requests: list[dict[str, object]] = []
    for scene in cast(Sequence[Scene], context["scenes"]):
        contract = scene_contract(scene)
        narration = str(contract["narration"])
        if not narration or len(narration) > 3_800:
            raise WorkflowReplayConflict(
                "Accepted storyboard narration violates the TTS request bound"
            )
        requests.append(
            {
                "character_count": len(narration),
                "input_hash": canonical_sha256(
                    {
                        "campaign_id": campaign_id,
                        "contract_version": "i5-scene-tts-request-v1",
                        "narration": narration,
                        "narration_sha256": contract["narration_sha256"],
                        "production_profile_hash": context["profile_hash"],
                        "scene_position": scene.position,
                        "storyboard_hash": context["storyboard_hash"],
                        "voice": "onyx",
                    }
                ),
                "narration_sha256": contract["narration_sha256"],
                "scene_id": scene.id,
                "scene_position": scene.position,
            }
        )
    return requests


def _canonical_media_plan_values(
    *,
    campaign_id: int,
    context: Mapping[str, object],
    selected_model: str,
) -> tuple[dict[str, object], dict[str, object]]:
    if selected_model not in {"tts-1-hd", "tts-1"}:
        raise WorkflowReplayConflict("Committed media plan TTS model is invalid")
    requests = _canonical_tts_requests(campaign_id=campaign_id, context=context)
    per_character = 30 if selected_model == "tts-1-hd" else 15
    reservations = [
        {
            **request,
            "reserved_cost_microusd": (
                int(request["character_count"]) * per_character * 120 + 99
            )
            // 100,
        }
        for request in requests
    ]
    policy = load_campaign_budget_policy()
    payload: dict[str, object] = {
        "budget_decision": (
            BudgetDecision.ALLOW_PRIMARY.value
            if selected_model == "tts-1-hd"
            else BudgetDecision.USE_LOWER_COST_FALLBACK.value
        ),
        "budget_policy_sha256": policy.policy_sha256,
        "campaign_id": campaign_id,
        "contract_version": "i5-media-plan-v1",
        "production_profile_hash": context["profile_hash"],
        "requests": requests,
        "reservations": reservations,
        "selected_tts_model": selected_model,
        "storyboard_hash": context["storyboard_hash"],
        "tts_voice": "onyx",
    }
    input_hash = canonical_sha256(
        {
            "contract_version": "i5-media-plan-input-v1",
            "production_profile_hash": context["profile_hash"],
            "storyboard_hash": context["storyboard_hash"],
        }
    )
    return payload, _json_artifact_values(
        campaign_id=campaign_id,
        kind=I5_MEDIA_PLAN_KIND,
        source_stage="media",
        payload=payload,
        input_hash=input_hash,
        provider_name="campaign_budget_guard",
        provider_model="i5-media-planner-v1",
    )


def _reconcile_committed_media_plan(
    artifact: Artifact,
    *,
    campaign_id: int,
    context: Mapping[str, object],
) -> dict[str, object]:
    actual = _decode_json_artifact(artifact)
    expected, expected_artifact = _canonical_media_plan_values(
        campaign_id=campaign_id,
        context=context,
        selected_model=str(actual.get("selected_tts_model") or ""),
    )
    if actual != expected:
        raise WorkflowReplayConflict("Committed media plan payload conflicts")
    _verify_fields(artifact, expected_artifact, label="committed media plan artifact")
    return expected


def reserve_tts_plan(
    *,
    campaign_id: int,
    storyboard_hash: str,
    requests: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Select one campaign TTS model and reserve every scene before the first POST."""

    if not requests:
        raise WorkflowTransitionError("TTS media plan requires at least one scene")
    for request in requests:
        if int(request["character_count"]) <= 0:
            raise WorkflowTransitionError("TTS request character count must be positive")

    with SessionLocal() as db:
        with serialized_transaction(db):
            campaign = campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            context = reconcile_storyboard_effect_set(db, campaign_id)
            if campaign.current_stage != "media":
                raise WorkflowTransitionError(
                    f"Campaign stage is {campaign.current_stage}; expected media"
                )
            storyboard = db.scalar(
                select(Artifact).where(
                    Artifact.campaign_id == campaign_id,
                    Artifact.kind == I5_STORYBOARD_KIND,
                    Artifact.sha256 == storyboard_hash,
                )
            )
            if storyboard is None:
                raise WorkflowReplayConflict("Accepted storyboard artifact is missing")
            _decode_json_artifact(storyboard)

            canonical_requests = _canonical_tts_requests(
                campaign_id=campaign_id,
                context=context,
            )
            if [dict(item) for item in requests] != canonical_requests:
                raise WorkflowReplayConflict(
                    "TTS reservation requests do not match canonical scene narration"
                )
            primary_total = sum(
                (int(request["character_count"]) * 30 * 120 + 99) // 100
                for request in canonical_requests
            )
            fallback_total = sum(
                (int(request["character_count"]) * 15 * 120 + 99) // 100
                for request in canonical_requests
            )

            existing_plans = list(
                db.scalars(
                    select(Artifact).where(
                    Artifact.campaign_id == campaign_id,
                    Artifact.kind == I5_MEDIA_PLAN_KIND,
                    )
                )
            )
            if len(existing_plans) > 1:
                raise WorkflowReplayConflict("Existing media plan identity is ambiguous")
            if existing_plans:
                existing_plan = existing_plans[0]
                plan = _reconcile_committed_media_plan(
                    existing_plan,
                    campaign_id=campaign_id,
                    context=context,
                )
                jobs = _media_plan_jobs(db, campaign_id, plan)
                return {
                    "blocked": False,
                    "job_ids": [job.id for job in jobs],
                    "media_plan_hash": existing_plan.sha256,
                    "replayed": True,
                    "selected_model": plan["selected_tts_model"],
                }

            cost = campaign_cost(db, campaign_id)
            cap = authorized_campaign_cap(db, campaign_id)
            choice = choose_budget_option(
                current_cost_microusd=cost.total_microusd,
                authorized_cap_microusd=cap,
                primary=BudgetOption("tts-1-hd", primary_total),
                fallbacks=(BudgetOption("tts-1", fallback_total),),
            )
            if choice.decision == BudgetDecision.BLOCK_NEEDS_HUMAN:
                block = persist_budget_block(
                    db,
                    campaign_id=campaign_id,
                    request_identity={
                        "kind": "full_campaign_tts_plan",
                        "storyboard_hash": storyboard_hash,
                        "tts_fallback_reservation_microusd": fallback_total,
                        "tts_primary_reservation_microusd": primary_total,
                    },
                    current_cost=cost,
                    authorized_cap_microusd=cap,
                    projected_cost_microusd=choice.projected_cost_microusd,
                    profile_hash=str(context["profile_hash"]),
                )
                return {"blocked": True, **block}

            assert choice.selected is not None
            selected_model = choice.selected.name
            per_character = 30 if selected_model == "tts-1-hd" else 15
            plan, plan_expected = _canonical_media_plan_values(
                campaign_id=campaign_id,
                context=context,
                selected_model=selected_model,
            )
            if plan["budget_decision"] != choice.decision.value:
                raise WorkflowReplayConflict("Media plan budget decision conflicts")
            normalized_requests = cast(
                list[dict[str, object]],
                plan["reservations"],
            )
            plan_artifact = _reconcile_artifact(db, plan_expected, allow_create=True)
            jobs: list[GenerationJob] = []
            for item in normalized_requests:
                expected_job = {
                    "attempt": 1,
                    "campaign_id": campaign_id,
                    "cost_microunits": None,
                    "error_json": None,
                    "input_hash": item["input_hash"],
                    "model": selected_model,
                    "output_artifact_id": None,
                    "provider": "openai_tts",
                    "provider_job_id": None,
                    "reserved_cost_microunits": item["reserved_cost_microusd"],
                    "scene_id": int(item["scene_id"]),
                    "status": "reserved",
                    "usage_json": canonical_json(
                        {
                            "character_count": int(item["character_count"]),
                            "price_microusd_per_character": per_character,
                        }
                    ),
                }
                job = GenerationJob(**expected_job)
                db.add(job)
                db.flush()
                jobs.append(job)
            return {
                "blocked": False,
                "job_ids": [job.id for job in jobs],
                "media_plan_hash": plan_artifact.sha256,
                "replayed": False,
                "selected_model": selected_model,
            }


def _media_plan_jobs(
    db: Session,
    campaign_id: int,
    plan: Mapping[str, object],
) -> list[GenerationJob]:
    jobs: list[GenerationJob] = []
    model = str(plan["selected_tts_model"])
    for reservation in cast(Sequence[Mapping[str, object]], plan["reservations"]):
        job = db.scalar(
            select(GenerationJob).where(
                GenerationJob.campaign_id == campaign_id,
                GenerationJob.provider == "openai_tts",
                GenerationJob.model == model,
                GenerationJob.input_hash == reservation["input_hash"],
                GenerationJob.attempt == 1,
            )
        )
        if job is None:
            raise WorkflowReplayConflict("Committed media plan is missing a TTS reservation")
        if (
            job.scene_id != int(reservation["scene_id"])
            or job.reserved_cost_microunits
            != int(reservation["reserved_cost_microusd"])
        ):
            raise WorkflowReplayConflict("TTS reservation conflicts with media plan")
        jobs.append(job)
    return jobs


def _reconcile_local_visual_choice(
    db: Session,
    *,
    campaign_id: int,
    scene: Scene,
    input_hash: str,
    selected: str,
    decision: str,
    profile_hash: str,
    allow_create: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "campaign_id": campaign_id,
        "contract_version": "i5-local-visual-choice-v1",
        "decision": decision,
        "production_profile_hash": profile_hash,
        "scene_position": scene.position,
        "selected": selected,
        "visual_input_hash": input_hash,
    }
    expected_artifact = _json_artifact_values(
        campaign_id=campaign_id,
        kind=f"i5_visual_choice_{scene.position:03d}",
        source_stage="media",
        payload=payload,
        input_hash=input_hash,
        provider_name="campaign_budget_guard",
        provider_model="i5-visual-choice-v1",
    )
    artifact = _reconcile_artifact(
        db,
        expected_artifact,
        allow_create=allow_create,
    )
    job = _reconcile_job(
        db,
        _zero_cost_job_values(
            campaign_id=campaign_id,
            scene_id=scene.id,
            provider="campaign_budget_guard",
            model=selected,
            input_hash=input_hash,
            output_artifact_id=artifact.id,
        ),
        allow_create=allow_create,
    )
    return {
        "decision": decision,
        "job_id": job.id,
        "provider": "local",
        "replayed": not allow_create,
        "selected": selected,
    }


def reserve_visual_job(
    *,
    campaign_id: int,
    scene_id: int,
    input_hash: str,
    options: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Atomically choose and reserve one metered visual request, or choose local."""

    if _SHA256_PATTERN.fullmatch(input_hash) is None:
        raise ValueError("visual generation input_hash is invalid")
    if not options:
        raise ValueError("visual generation requires deterministic options")
    primary_raw = dict(options[0])
    fallback_raw = [dict(option) for option in options[1:]]
    primary = BudgetOption(
        str(primary_raw["name"]),
        int(primary_raw["reservation_microusd"]),
    )
    fallbacks = tuple(
        BudgetOption(str(option["name"]), int(option["reservation_microusd"]))
        for option in fallback_raw
    )
    by_name = {str(option["name"]): option for option in [primary_raw, *fallback_raw]}
    if len(by_name) != len(options):
        raise ValueError("visual budget option names must be unique")

    with SessionLocal() as db:
        with serialized_transaction(db):
            campaign = campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            context = reconcile_storyboard_effect_set(db, campaign_id)
            if campaign.current_stage != "media":
                raise WorkflowTransitionError(
                    f"Campaign stage is {campaign.current_stage}; expected media"
                )
            scene = db.get(Scene, scene_id)
            if scene is None or scene.campaign_id != campaign_id:
                raise WorkflowReplayConflict("Visual generation scene lineage is invalid")

            from .media import scene_contract, visual_input_hash, visual_request_options

            contract = scene_contract(scene)
            expected_input_hash = visual_input_hash(
                campaign_id=campaign_id,
                scene=contract,
                profile_hash=str(context["profile_hash"]),
                storyboard_hash=str(context["storyboard_hash"]),
            )
            primary_fulfillment = dict(contract.get("primary_fulfillment") or {})
            expected_options = visual_request_options(
                cast(Mapping[str, object], context["profile_payload"]),
                allow_video=primary_fulfillment.get("strategy")
                == "generated_video_primary",
            )
            if input_hash != expected_input_hash or [dict(item) for item in options] != [
                dict(item) for item in expected_options
            ]:
                raise WorkflowReplayConflict(
                    "Visual reservation request does not match canonical scene lineage"
                )

            existing = list(
                db.scalars(
                    select(GenerationJob).where(
                        GenerationJob.campaign_id == campaign_id,
                        GenerationJob.scene_id == scene_id,
                        GenerationJob.input_hash == input_hash,
                    )
                )
            )
            if len(existing) > 1:
                raise WorkflowReplayConflict("Visual request replay identity is ambiguous")
            if existing:
                job = existing[0]
                option = by_name.get(job.model)
                if option is None:
                    raise WorkflowReplayConflict("Visual reservation model conflicts")
                if job.reserved_cost_microunits != int(
                    option["reservation_microusd"]
                ):
                    raise WorkflowReplayConflict("Visual reservation cost conflicts")
                expected_provider = str(option.get("provider"))
                if expected_provider == "local":
                    if job.provider != "campaign_budget_guard" or job.output_artifact_id is None:
                        raise WorkflowReplayConflict("Local visual choice job conflicts")
                    artifact = db.get(Artifact, job.output_artifact_id)
                    if artifact is None:
                        raise WorkflowReplayConflict("Local visual choice artifact is missing")
                    payload = _decode_json_artifact(artifact)
                    return _reconcile_local_visual_choice(
                        db,
                        campaign_id=campaign_id,
                        scene=scene,
                        input_hash=input_hash,
                        selected=job.model,
                        decision=str(payload.get("decision") or ""),
                        profile_hash=str(context["profile_hash"]),
                        allow_create=False,
                    )
                if job.provider != expected_provider:
                    raise WorkflowReplayConflict("Visual reservation provider conflicts")
                return {
                    "decision": str(option.get("decision") or "REPLAY"),
                    "job_id": job.id,
                    "provider": job.provider,
                    "replayed": True,
                    "selected": job.model,
                }

            cost = campaign_cost(db, campaign_id)
            cap = authorized_campaign_cap(db, campaign_id)
            choice = choose_budget_option(
                current_cost_microusd=cost.total_microusd,
                authorized_cap_microusd=cap,
                primary=primary,
                fallbacks=fallbacks,
            )
            if choice.decision == BudgetDecision.BLOCK_NEEDS_HUMAN:
                # Every canonical visual request has a zero-cost deterministic fallback.
                return _reconcile_local_visual_choice(
                    db,
                    campaign_id=campaign_id,
                    scene=scene,
                    input_hash=input_hash,
                    selected="deterministic_local",
                    decision=BudgetDecision.USE_LOWER_COST_FALLBACK,
                    profile_hash=str(context["profile_hash"]),
                    allow_create=True,
                )
            assert choice.selected is not None
            selected = by_name[choice.selected.name]
            if str(selected.get("provider")) == "local":
                return _reconcile_local_visual_choice(
                    db,
                    campaign_id=campaign_id,
                    scene=scene,
                    input_hash=input_hash,
                    selected=choice.selected.name,
                    decision=choice.decision,
                    profile_hash=str(context["profile_hash"]),
                    allow_create=True,
                )
            job = GenerationJob(
                campaign_id=campaign_id,
                scene_id=scene_id,
                provider=str(selected["provider"]),
                model=str(selected["name"]),
                attempt=1,
                status="reserved",
                input_hash=input_hash,
                output_artifact_id=None,
                provider_job_id=None,
                usage_json=canonical_json(
                    {
                        "budget_decision": choice.decision,
                        "production_profile_hash": context["profile_hash"],
                        "reservation_microusd": choice.selected.reservation_microusd,
                    }
                ),
                cost_microunits=None,
                reserved_cost_microunits=choice.selected.reservation_microusd,
                error_json=None,
            )
            db.add(job)
            db.flush()
            return {
                "decision": choice.decision,
                "job_id": job.id,
                "provider": job.provider,
                "replayed": False,
                "selected": job.model,
            }


def _validate_metered_reservation(
    db: Session,
    job: GenerationJob,
    *,
    expected_error_json: str | None = None,
) -> None:
    if (
        job.attempt != 1
        or job.scene_id is None
        or job.cost_microunits is not None
        or job.output_artifact_id is not None
        or job.provider_job_id is not None
        or job.error_json != expected_error_json
        or job.reserved_cost_microunits is None
        or job.reserved_cost_microunits <= 0
    ):
        raise WorkflowReplayConflict("Metered job reservation fields are invalid")

    context = reconcile_storyboard_effect_set(db, job.campaign_id)
    scene = db.get(Scene, job.scene_id)
    if scene is None or scene.campaign_id != job.campaign_id:
        raise WorkflowReplayConflict("Metered job scene lineage is invalid")

    if job.provider == "openai_tts":
        requests = _canonical_tts_requests(
            campaign_id=job.campaign_id,
            context=context,
        )
        matches = [item for item in requests if item["scene_id"] == scene.id]
        if len(matches) != 1 or matches[0]["input_hash"] != job.input_hash:
            raise WorkflowReplayConflict("TTS reservation input lineage is invalid")
        if job.model not in {"tts-1-hd", "tts-1"}:
            raise WorkflowReplayConflict("TTS reservation model is invalid")
        price = 30 if job.model == "tts-1-hd" else 15
        expected_reservation = (
            int(matches[0]["character_count"]) * price * 120 + 99
        ) // 100
        if (
            job.reserved_cost_microunits != expected_reservation
            or job.usage_json
            != canonical_json(
                {
                    "character_count": int(matches[0]["character_count"]),
                    "price_microusd_per_character": price,
                }
            )
        ):
            raise WorkflowReplayConflict("TTS reservation accounting is invalid")
        plan_rows = list(
            db.scalars(
                select(Artifact).where(
                    Artifact.campaign_id == job.campaign_id,
                    Artifact.kind == I5_MEDIA_PLAN_KIND,
                )
            )
        )
        if len(plan_rows) != 1:
            raise WorkflowReplayConflict("TTS reservation media plan is ambiguous")
        plan = _reconcile_committed_media_plan(
            plan_rows[0],
            campaign_id=job.campaign_id,
            context=context,
        )
        if (
            plan.get("campaign_id") != job.campaign_id
            or plan.get("contract_version") != "i5-media-plan-v1"
            or plan.get("production_profile_hash") != context["profile_hash"]
            or plan.get("storyboard_hash") != context["storyboard_hash"]
            or plan.get("selected_tts_model") != job.model
            or plan.get("tts_voice") != "onyx"
            or plan.get("requests") != requests
        ):
            raise WorkflowReplayConflict("TTS reservation media plan lineage is invalid")
        reservations = [
            dict(item)
            for item in cast(
                Sequence[Mapping[str, object]],
                plan.get("reservations", []),
            )
            if item.get("scene_id") == scene.id
        ]
        if len(reservations) != 1 or reservations[0] != {
            **matches[0],
            "reserved_cost_microusd": expected_reservation,
        }:
            raise WorkflowReplayConflict("TTS media-plan reservation is invalid")
        return

    from .media import scene_contract, visual_input_hash, visual_request_options

    contract = scene_contract(scene)
    expected_input_hash = visual_input_hash(
        campaign_id=job.campaign_id,
        scene=contract,
        profile_hash=str(context["profile_hash"]),
        storyboard_hash=str(context["storyboard_hash"]),
    )
    expected_visuals = {
        ("openai_image", "gpt-image-2-medium"): 100_000,
        ("openai_image", "gpt-image-2-low"): 25_000,
        ("openai_video", "sora-2"): 960_000,
    }
    expected_reservation = expected_visuals.get((job.provider, job.model))
    primary_fulfillment = dict(contract.get("primary_fulfillment") or {})
    expected_options = visual_request_options(
        cast(Mapping[str, object], context["profile_payload"]),
        allow_video=primary_fulfillment.get("strategy")
        == "generated_video_primary",
    )
    selected_indexes = [
        index
        for index, option in enumerate(expected_options)
        if option.get("provider") == job.provider and option.get("name") == job.model
    ]
    if (
        expected_reservation is None
        or contract.get("visual_mode") != "GENERATED_CINEMATIC"
        or job.input_hash != expected_input_hash
        or job.reserved_cost_microunits != expected_reservation
        or len(selected_indexes) != 1
    ):
        raise WorkflowReplayConflict("Visual reservation accounting is invalid")
    try:
        usage = json.loads(job.usage_json or "")
    except json.JSONDecodeError as exc:
        raise WorkflowReplayConflict("Visual reservation usage is invalid") from exc
    expected_usage = {
        "budget_decision": (
            BudgetDecision.ALLOW_PRIMARY
            if selected_indexes[0] == 0
            else BudgetDecision.USE_LOWER_COST_FALLBACK
        ),
        "production_profile_hash": context["profile_hash"],
        "reservation_microusd": expected_reservation,
    }
    if (
        not isinstance(usage, dict)
        or canonical_json(usage) != job.usage_json
        or usage != expected_usage
    ):
        raise WorkflowReplayConflict("Visual reservation usage conflicts")


def _terminal_metered_provenance(artifact: Artifact) -> dict[str, object]:
    try:
        provenance = json.loads(artifact.provenance_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkflowReplayConflict(
            "Completed metered artifact provenance is invalid"
        ) from exc
    if (
        not isinstance(provenance, dict)
        or canonical_json(provenance) != artifact.provenance_json
    ):
        raise WorkflowReplayConflict(
            "Completed metered artifact provenance is not canonical"
        )
    return provenance


def _validate_completed_metered_job(db: Session, job: GenerationJob) -> None:
    """Reconcile a committed provider result before DBOS may reuse it."""

    if (
        job.attempt != 1
        or job.scene_id is None
        or job.output_artifact_id is None
        or job.reserved_cost_microunits is None
        or job.reserved_cost_microunits <= 0
        or job.cost_microunits is None
        or job.cost_microunits < 0
        or job.cost_microunits > job.reserved_cost_microunits
        or job.error_json is not None
        or job.completed_at is None
    ):
        raise WorkflowReplayConflict("Completed metered job fields are invalid")

    context = reconcile_storyboard_effect_set(db, job.campaign_id)
    scene = db.get(Scene, job.scene_id)
    artifact = db.get(Artifact, job.output_artifact_id)
    if (
        scene is None
        or scene.campaign_id != job.campaign_id
        or artifact is None
        or artifact.campaign_id != job.campaign_id
        or artifact.payload_json is not None
        or artifact.byte_size <= 0
        or _SHA256_PATTERN.fullmatch(artifact.sha256) is None
    ):
        raise WorkflowReplayConflict("Completed metered result lineage is invalid")

    from .media import scene_contract, visual_input_hash

    contract = scene_contract(scene)
    provenance = _terminal_metered_provenance(artifact)
    expected_kind = (
        f"i5_scene_narration_{scene.position:03d}"
        if job.provider == "openai_tts"
        else f"i5_scene_visual_{scene.position:03d}"
    )
    expected_extension = "wav" if job.provider == "openai_tts" else (
        "mp4" if job.provider == "openai_video" else "png"
    )
    if (
        artifact.kind != expected_kind
        or artifact.source_stage != "media"
        or artifact.prompt_template_version != "i5-media-request-v1"
        or artifact.uri
        != (
            f"i5-object://campaign/{job.campaign_id}/objects/"
            f"{artifact.sha256}.{expected_extension}"
        )
    ):
        raise WorkflowReplayConflict("Completed metered artifact identity is invalid")

    if job.provider == "openai_tts":
        requests = _canonical_tts_requests(
            campaign_id=job.campaign_id,
            context=context,
        )
        matches = [item for item in requests if item["scene_id"] == scene.id]
        if len(matches) != 1 or matches[0]["input_hash"] != job.input_hash:
            raise WorkflowReplayConflict("Completed TTS input lineage is invalid")
        price = {"tts-1-hd": 30, "tts-1": 15}.get(job.model)
        if price is None:
            raise WorkflowReplayConflict("Completed TTS model is invalid")
        character_count = int(matches[0]["character_count"])
        reservation = (character_count * price * 120 + 99) // 100
        duration = provenance.get("duration_seconds")
        if not isinstance(duration, (int, float)) or float(duration) <= 0:
            raise WorkflowReplayConflict("Completed TTS duration is invalid")
        usage = {
            "character_count": character_count,
            "duration_seconds": round(float(duration), 6),
            "price_microusd_per_character": price,
        }
        expected_values = binary_artifact_values(
            campaign_id=job.campaign_id,
            kind=expected_kind,
            source_stage="media",
            uri=artifact.uri,
            sha256=artifact.sha256,
            byte_size=artifact.byte_size,
            mime_type="audio/wav",
            provider_name="openai_tts",
            provider_model=job.model,
            input_hash=job.input_hash,
            provenance={
                "campaign_id": job.campaign_id,
                "duration_seconds": round(float(duration), 6),
                "media_validation": {
                    "audio_stream": True,
                    "format": "wav",
                    "valid": True,
                },
                "narration_sha256": contract["narration_sha256"],
                "production_profile_hash": context["profile_hash"],
                "rights_basis": "original_i4_narration",
                "scene_position": scene.position,
                "storyboard_hash": context["storyboard_hash"],
            },
        )
        if (
            job.reserved_cost_microunits != reservation
            or job.cost_microunits != character_count * price
            or job.provider_job_id is not None
            or job.usage_json != canonical_json(usage)
        ):
            raise WorkflowReplayConflict("Completed TTS accounting is invalid")
        _verify_fields(artifact, expected_values, label="completed TTS artifact")
        return

    expected_input_hash = visual_input_hash(
        campaign_id=job.campaign_id,
        scene=contract,
        profile_hash=str(context["profile_hash"]),
        storyboard_hash=str(context["storyboard_hash"]),
    )
    if job.input_hash != expected_input_hash:
        raise WorkflowReplayConflict("Completed visual input lineage is invalid")

    if job.provider == "openai_image":
        quality = {
            "gpt-image-2-medium": "medium",
            "gpt-image-2-low": "low",
        }.get(job.model)
        reservation = {"medium": 100_000, "low": 25_000}.get(quality or "")
        if reservation is None:
            raise WorkflowReplayConflict("Completed image model is invalid")
        from .providers import safe_generated_media_prompt

        usage = {
            "model": "gpt-image-2",
            "output_format": "png",
            "prompt": safe_generated_media_prompt(str(contract["visual_purpose"])),
            "quality": quality,
            "reservation_microunits": reservation,
            "size": "1280x720",
        }
        expected_values = binary_artifact_values(
            campaign_id=job.campaign_id,
            kind=expected_kind,
            source_stage="media",
            uri=artifact.uri,
            sha256=artifact.sha256,
            byte_size=artifact.byte_size,
            mime_type="image/png",
            provider_name="openai_image",
            provider_model=job.model,
            input_hash=job.input_hash,
            provenance={
                "campaign_id": job.campaign_id,
                "claim_hashes": [],
                "disclosure_state": "illustrative_generated_media",
                "generated_media_is_evidence": False,
                "media_validation": {"height": 720, "valid": True, "width": 1280},
                "production_profile_hash": context["profile_hash"],
                "provider_metadata": usage,
                "rights_basis": "generated_illustrative",
                "scene_position": scene.position,
                "storyboard_hash": context["storyboard_hash"],
            },
        )
        if (
            job.reserved_cost_microunits != reservation
            or job.cost_microunits != reservation
            or job.provider_job_id is not None
            or job.usage_json != canonical_json(usage)
        ):
            raise WorkflowReplayConflict("Completed image accounting is invalid")
        _verify_fields(artifact, expected_values, label="completed image artifact")
        return

    if job.provider != "openai_video" or job.model != "sora-2":
        raise WorkflowReplayConflict("Completed metered provider is invalid")
    if not is_valid_sora_provider_job_id(job.provider_job_id):
        raise WorkflowReplayConflict("Completed Sora provider identity is invalid")
    usage = {
        "duration_seconds": 8,
        "provider_job_id": job.provider_job_id,
        "size": "1280x720",
    }
    media_validation = provenance.get("media_validation")
    if not isinstance(media_validation, dict):
        raise WorkflowReplayConflict("Completed Sora media validation is invalid")
    expected_values = binary_artifact_values(
        campaign_id=job.campaign_id,
        kind=expected_kind,
        source_stage="media",
        uri=artifact.uri,
        sha256=artifact.sha256,
        byte_size=artifact.byte_size,
        mime_type="video/mp4",
        provider_name="openai_video",
        provider_model="sora-2",
        input_hash=job.input_hash,
        provenance={
            "campaign_id": job.campaign_id,
            "claim_hashes": [],
            "disclosure_state": "illustrative_generated_media",
            "generated_media_is_evidence": False,
            "media_validation": media_validation,
            "production_profile_hash": context["profile_hash"],
            "provider_job_id": job.provider_job_id,
            "rights_basis": "generated_illustrative",
            "scene_position": scene.position,
            "storyboard_hash": context["storyboard_hash"],
            "visual_purpose": contract["visual_purpose"],
        },
    )
    if (
        job.reserved_cost_microunits != 960_000
        or job.cost_microunits != 800_000
        or job.usage_json != canonical_json(usage)
    ):
        raise WorkflowReplayConflict("Completed Sora accounting is invalid")
    _verify_fields(artifact, expected_values, label="completed Sora artifact")


def _validate_submitted_sora_job(
    db: Session,
    job: GenerationJob,
    *,
    expected_error_json: str | None = None,
) -> None:
    if (
        job.provider != "openai_video"
        or job.model != "sora-2"
        or job.attempt != 1
        or job.scene_id is None
        or job.output_artifact_id is not None
        or job.cost_microunits is not None
        or job.reserved_cost_microunits != 960_000
        or job.error_json != expected_error_json
        or job.completed_at is not None
        or not is_valid_sora_provider_job_id(job.provider_job_id)
    ):
        raise WorkflowReplayConflict("Submitted provider job fields are invalid")
    context = reconcile_storyboard_effect_set(db, job.campaign_id)
    scene = db.get(Scene, job.scene_id)
    if scene is None or scene.campaign_id != job.campaign_id:
        raise WorkflowReplayConflict("Submitted provider job scene is invalid")
    from .media import scene_contract, visual_input_hash

    contract = scene_contract(scene)
    video_profile = dict(
        cast(Mapping[str, object], context["profile_payload"])["video"]  # type: ignore[index]
    )
    expected_input_hash = visual_input_hash(
        campaign_id=job.campaign_id,
        scene=contract,
        profile_hash=str(context["profile_hash"]),
        storyboard_hash=str(context["storyboard_hash"]),
    )
    try:
        usage = json.loads(job.usage_json or "")
    except json.JSONDecodeError as exc:
        raise WorkflowReplayConflict("Submitted provider usage is invalid") from exc
    expected_statuses = {"queued", "in_progress", "processing", "completed"}
    if (
        dict(contract.get("primary_fulfillment") or {}).get("strategy")
        != "generated_video_primary"
        or video_profile.get("provider") == "disabled"
        or video_profile.get("model") != "sora-2"
        or video_profile.get("allow_deprecated_sora") is not True
        or job.input_hash != expected_input_hash
        or not isinstance(usage, dict)
        or canonical_json(usage) != job.usage_json
        or set(usage) != {
            "duration_seconds",
            "model",
            "provider_status",
            "reservation_microusd",
            "size",
        }
        or usage.get("duration_seconds") != 8
        or usage.get("model") != "sora-2"
        or usage.get("provider_status") not in expected_statuses
        or usage.get("reservation_microusd") != 960_000
        or usage.get("size") != "1280x720"
    ):
        raise WorkflowReplayConflict("Submitted provider job lineage is invalid")


def reconcile_completed_metered_job(db: Session, job: GenerationJob) -> None:
    """Public in-transaction terminal-result reconciliation for the media gate."""

    if job.status not in {"provider_complete", "completed"}:
        raise WorkflowReplayConflict("Metered generation job is not complete")
    _validate_completed_metered_job(db, job)


_AMBIGUOUS_DISPATCH_CODES = {
    "openai_tts": {
        "ambiguous_dispatch",
        "tts_provider_outcome_unknown",
        "tts_result_not_durably_persisted",
    },
    "openai_image": {
        "ambiguous_dispatch",
        "image_provider_outcome_unknown",
        "image_result_not_durably_persisted",
    },
    "openai_video": {
        "ambiguous_dispatch",
        "sora_create_outcome_unknown",
        "sora_submission_not_durably_persisted",
    },
}
_SORA_PROVIDER_FAILURE_CODES = {
    "sora_cancelled",
    "sora_download_or_validation_failed",
    "sora_failed",
    "sora_poll_failed",
    "sora_poll_timeout",
}


def _canonical_metered_failure_error(
    job: GenerationJob,
    *,
    allowed_codes: set[str],
    label: str,
) -> str:
    try:
        error = json.loads(job.error_json or "")
    except json.JSONDecodeError as exc:
        raise WorkflowReplayConflict(f"{label} error is invalid") from exc
    if (
        not isinstance(error, dict)
        or canonical_json(error) != job.error_json
        or set(error) != {"code", "retryable_post"}
        or error.get("code") not in allowed_codes
        or error.get("retryable_post") is not False
    ):
        raise WorkflowReplayConflict(f"{label} error is invalid")
    return job.error_json


def _ambiguous_dispatch_error(job: GenerationJob) -> str:
    return _canonical_metered_failure_error(
        job,
        allowed_codes=_AMBIGUOUS_DISPATCH_CODES.get(job.provider, set()),
        label="Ambiguous dispatch",
    )


def reconcile_failed_metered_job(db: Session, job: GenerationJob) -> None:
    """Reconcile immutable metered failure/ambiguity lineage before reuse."""

    if job.status == "ambiguous_dispatch":
        error_json = _ambiguous_dispatch_error(job)
        _validate_metered_reservation(
            db,
            job,
            expected_error_json=error_json,
        )
        return
    if job.status == "provider_failed":
        if job.provider != "openai_video" or job.provider_job_id is None:
            raise WorkflowReplayConflict("Provider failure lineage is invalid")
        error_json = _canonical_metered_failure_error(
            job,
            allowed_codes=_SORA_PROVIDER_FAILURE_CODES,
            label="Provider failure",
        )
        _validate_submitted_sora_job(
            db,
            job,
            expected_error_json=error_json,
        )
        return
    raise WorkflowReplayConflict("Metered generation job is not failed")


def claim_metered_dispatch(job_id: int) -> dict[str, object]:
    """Atomically claim a single no-retry metered POST or classify recovery ambiguity."""

    with SessionLocal() as db:
        with serialized_transaction(db):
            job = db.scalar(
                select(GenerationJob)
                .where(GenerationJob.id == job_id)
                .with_for_update()
            )
            if job is None:
                raise WorkflowReplayConflict("Metered generation job does not exist")
            _bound_context(db, job.campaign_id)
            if job.status in {"provider_complete", "completed"}:
                _validate_completed_metered_job(db, job)
                return {
                    "dispatch": False,
                    "job_id": job.id,
                    "output_artifact_id": job.output_artifact_id,
                    "status": job.status,
                }
            if job.status == "submitted":
                _validate_submitted_sora_job(db, job)
                return {
                    "dispatch": False,
                    "job_id": job.id,
                    "output_artifact_id": None,
                    "provider_job_id": job.provider_job_id,
                    "status": "submitted",
                }
            if job.status in {"dispatching", "ambiguous_dispatch"}:
                ambiguous_error = (
                    _ambiguous_dispatch_error(job)
                    if job.status == "ambiguous_dispatch"
                    else canonical_json(
                        {"code": "ambiguous_dispatch", "retryable_post": False}
                    )
                )
                _validate_metered_reservation(
                    db,
                    job,
                    expected_error_json=ambiguous_error
                    if job.status == "ambiguous_dispatch"
                    else None,
                )
                if job.status == "dispatching":
                    job.status = "ambiguous_dispatch"
                    job.error_json = ambiguous_error
                return {
                    "dispatch": False,
                    "job_id": job.id,
                    "output_artifact_id": None,
                    "status": "ambiguous_dispatch",
                }
            if job.status == "provider_failed":
                reconcile_failed_metered_job(db, job)
                return {
                    "dispatch": False,
                    "job_id": job.id,
                    "output_artifact_id": None,
                    "status": "provider_failed",
                }
            if job.status != "reserved":
                raise WorkflowTransitionError(
                    f"Metered job cannot dispatch from status {job.status}"
                )
            _validate_metered_reservation(db, job)
            cost = campaign_cost(db, job.campaign_id)
            if cost.total_microusd > authorized_campaign_cap(db, job.campaign_id):
                raise WorkflowTransitionError(
                    "Committed reservations exceed the authorized campaign cap"
                )
            job.status = "dispatching"
            return {
                "dispatch": True,
                "job_id": job.id,
                "model": job.model,
                "scene_id": job.scene_id,
                "status": "dispatching",
            }


def persist_submitted_provider_job(
    *,
    job_id: int,
    provider_job_id: str,
    metadata: Mapping[str, object],
) -> dict[str, object]:
    """Durably bind an asynchronous provider ID before polling can begin."""

    normalized_provider_job_id = provider_job_id.strip()
    if not is_valid_sora_provider_job_id(normalized_provider_job_id):
        raise ValueError("provider_job_id is invalid")
    usage_json = canonical_json(dict(metadata))
    with SessionLocal() as db:
        with serialized_transaction(db):
            job = db.scalar(
                select(GenerationJob)
                .where(GenerationJob.id == job_id)
                .with_for_update()
            )
            if job is None:
                raise WorkflowReplayConflict("Metered generation job does not exist")
            _bound_context(db, job.campaign_id)
            if job.provider != "openai_video" or job.model != "sora-2":
                raise WorkflowReplayConflict("Submitted job is not canonical Sora")
            if job.status == "submitted":
                if (
                    job.provider_job_id != normalized_provider_job_id
                    or job.usage_json != usage_json
                    or job.output_artifact_id is not None
                    or job.cost_microunits is not None
                ):
                    raise WorkflowReplayConflict(
                        "Submitted provider job replay conflicts"
                    )
                _validate_submitted_sora_job(db, job)
                return {
                    "job_id": job.id,
                    "provider_job_id": job.provider_job_id,
                    "replayed": True,
                    "status": "submitted",
                }
            if job.status != "dispatching" or job.provider_job_id is not None:
                raise WorkflowTransitionError(
                    f"Provider job cannot submit from status {job.status}"
                )
            _validate_metered_reservation(db, job)
            job.provider_job_id = normalized_provider_job_id
            job.usage_json = usage_json
            job.status = "submitted"
            _validate_submitted_sora_job(db, job)
            return {
                "job_id": job.id,
                "provider_job_id": job.provider_job_id,
                "replayed": False,
                "status": "submitted",
            }


def complete_metered_job(
    *,
    job_id: int,
    artifact_values: Mapping[str, object],
    final_cost_microunits: int,
    usage: Mapping[str, object],
    provider_job_id: str | None = None,
) -> dict[str, object]:
    if final_cost_microunits < 0:
        raise ValueError("final metered cost cannot be negative")
    with SessionLocal() as db:
        with serialized_transaction(db):
            job = db.scalar(
                select(GenerationJob)
                .where(GenerationJob.id == job_id)
                .with_for_update()
            )
            if job is None:
                raise WorkflowReplayConflict("Metered generation job does not exist")
            _bound_context(db, job.campaign_id)
            if job.status in {"provider_complete", "completed"}:
                _validate_completed_metered_job(db, job)
            elif job.status == "dispatching":
                _validate_metered_reservation(db, job)
            elif job.status == "submitted":
                _validate_submitted_sora_job(db, job)
            if (
                job.reserved_cost_microunits is None
                or job.reserved_cost_microunits <= 0
                or final_cost_microunits > job.reserved_cost_microunits
            ):
                raise WorkflowReplayConflict(
                    "Metered result exceeds or lacks its conservative reservation"
                )
            artifact = _reconcile_artifact(db, artifact_values, allow_create=True)
            if job.status in {"provider_complete", "completed"}:
                if (
                    job.output_artifact_id != artifact.id
                    or job.cost_microunits != final_cost_microunits
                    or job.usage_json != canonical_json(dict(usage))
                    or job.provider_job_id != provider_job_id
                ):
                    raise WorkflowReplayConflict(
                        "Completed metered job conflicts with provider replay"
                    )
                return {
                    "artifact_id": artifact.id,
                    "job_id": job.id,
                    "replayed": True,
                    "status": job.status,
                }
            if job.status not in {"dispatching", "submitted"}:
                raise WorkflowTransitionError(
                    f"Metered result cannot complete job from status {job.status}"
                )
            if job.status == "submitted" and job.provider_job_id != provider_job_id:
                raise WorkflowReplayConflict(
                    "Submitted provider job completion identity conflicts"
                )
            job.output_artifact_id = artifact.id
            job.provider_job_id = provider_job_id
            job.usage_json = canonical_json(dict(usage))
            job.cost_microunits = final_cost_microunits
            job.status = "provider_complete"
            job.completed_at = datetime.utcnow()
            return {
                "artifact_id": artifact.id,
                "job_id": job.id,
                "replayed": False,
                "status": job.status,
            }


def fail_metered_job(
    job_id: int,
    *,
    code: str,
    ambiguous: bool = False,
) -> dict[str, object]:
    with SessionLocal() as db:
        with serialized_transaction(db):
            job = db.scalar(
                select(GenerationJob)
                .where(GenerationJob.id == job_id)
                .with_for_update()
            )
            if job is None:
                raise WorkflowReplayConflict("Metered generation job does not exist")
            _bound_context(db, job.campaign_id)
            status = "ambiguous_dispatch" if ambiguous else "provider_failed"
            if job.status in {"provider_complete", "completed"}:
                raise WorkflowReplayConflict("Cannot fail a completed metered job")
            if job.status not in {"dispatching", "submitted", status}:
                raise WorkflowTransitionError(
                    f"Metered job cannot fail from status {job.status}"
                )
            expected_error = canonical_json(
                {"code": code, "retryable_post": False}
            )
            allowed_codes = (
                _AMBIGUOUS_DISPATCH_CODES.get(job.provider, set())
                if ambiguous
                else _SORA_PROVIDER_FAILURE_CODES
            )
            if code not in allowed_codes:
                raise WorkflowReplayConflict("Metered job failure code is invalid")
            if not ambiguous and (
                job.provider != "openai_video"
                or job.status not in {"submitted", "provider_failed"}
            ):
                raise WorkflowReplayConflict("Provider failure lineage is invalid")
            if job.status == "dispatching":
                _validate_metered_reservation(db, job)
            elif job.status == "submitted":
                _validate_submitted_sora_job(db, job)
            elif job.status == status:
                reconcile_failed_metered_job(db, job)
            if job.status == status and job.error_json != expected_error:
                raise WorkflowReplayConflict("Metered job failure replay conflicts")
            job.status = status
            job.error_json = expected_error
            return {"job_id": job.id, "status": status}


def persist_zero_cost_binary(
    *,
    campaign_id: int,
    scene_id: int | None,
    artifact_values: Mapping[str, object],
    provider: str,
    model: str,
    input_hash: str,
) -> dict[str, object]:
    with SessionLocal() as db:
        with serialized_transaction(db):
            campaign = campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            reconcile_storyboard_effect_set(db, campaign_id)
            if campaign.current_stage != "media":
                raise WorkflowTransitionError(
                    f"Campaign stage is {campaign.current_stage}; expected media"
                )
            if scene_id is not None:
                scene = db.get(Scene, scene_id)
                if scene is None or scene.campaign_id != campaign_id:
                    raise WorkflowReplayConflict("Local media scene lineage is invalid")
            artifact = _reconcile_artifact(db, artifact_values, allow_create=True)
            expected_job = _zero_cost_job_values(
                campaign_id=campaign_id,
                scene_id=scene_id,
                provider=provider,
                model=model,
                input_hash=input_hash,
                output_artifact_id=artifact.id,
            )
            job = _reconcile_job(db, expected_job, allow_create=True)
            return {
                "artifact_id": artifact.id,
                "job_id": job.id,
                "output_hash": artifact.sha256,
            }


def persist_media_manifest(
    packet: dict[str, object],
    *,
    settings,  # type: ignore[no-untyped-def]
) -> dict[str, object]:
    campaign_id = int(packet.get("campaign_id") or 0)
    with SessionLocal() as db:
        with serialized_transaction(db):
            campaign = campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            context = reconcile_storyboard_effect_set(db, campaign_id)
            from .media import build_media_manifest_from_session

            expected_packet = build_media_manifest_from_session(
                db,
                campaign_id=campaign_id,
                settings=settings,
            )
            if packet != expected_packet:
                raise WorkflowReplayConflict(
                    "Media manifest does not match canonical campaign media"
                )
            gate_payload = dict(packet.get("gate") or {})
            if gate_payload.get("outcome") != "PASS":
                raise WorkflowTransitionError("Canonical media manifest must PASS")
            input_hash = canonical_sha256(
                {
                    "contract_version": "i5-media-stage-input-v1",
                    "media_plan_hash": packet["media_plan_hash"],
                    "production_profile_hash": packet["production_profile_hash"],
                    "storyboard_hash": packet["storyboard_hash"],
                    "voiceover_hash": dict(packet["voiceover"])["artifact_hash"],  # type: ignore[arg-type]
                }
            )
            artifact_expected = _json_artifact_values(
                campaign_id=campaign_id,
                kind=I5_MEDIA_MANIFEST_KIND,
                source_stage="media",
                payload=packet,
                input_hash=input_hash,
                provider_name="i5_media_integrity",
                provider_model="i5-media-manifest-v1",
            )
            gate_expected = _gate_values(
                campaign_id=campaign_id,
                stage="media",
                policy_version=I5_PRODUCTION_POLICY_VERSION,
                input_hash=input_hash,
                outcome="PASS",
                output_hash=str(artifact_expected["sha256"]),
                reasons=tuple(str(value) for value in gate_payload["reasons"]),
            )
            existing_gate = db.scalar(
                select(GateDecision).where(
                    GateDecision.campaign_id == campaign_id,
                    GateDecision.stage == "media",
                    GateDecision.policy_version == I5_PRODUCTION_POLICY_VERSION,
                    GateDecision.input_hash == input_hash,
                )
            )
            replayed = existing_gate is not None
            budget_gate_ids = _reconcile_budget_block_gate_ids(
                db,
                campaign_id=campaign_id,
                profile_hash=str(context["profile_hash"]),
            )
            media_gates = list(
                db.scalars(
                    select(GateDecision).where(
                        GateDecision.campaign_id == campaign_id,
                        GateDecision.stage == "media",
                    )
                )
            )
            final_media_gates = [
                gate for gate in media_gates if gate.id not in budget_gate_ids
            ]
            manifest_rows = list(
                db.scalars(
                    select(Artifact).where(
                        Artifact.campaign_id == campaign_id,
                        Artifact.kind == I5_MEDIA_MANIFEST_KIND,
                    )
                )
            )
            integrity_jobs = list(
                db.scalars(
                    select(GenerationJob).where(
                        GenerationJob.campaign_id == campaign_id,
                        GenerationJob.provider == "i5_media_integrity",
                        GenerationJob.model == "i5-media-manifest-v1",
                    )
                )
            )
            if replayed:
                if (
                    len(final_media_gates) != 1
                    or final_media_gates[0].id != existing_gate.id
                    or len(manifest_rows) != 1
                    or len(integrity_jobs) != 1
                ):
                    raise WorkflowReplayConflict(
                        "Committed media effect-set cardinality is invalid"
                    )
            elif final_media_gates or manifest_rows or integrity_jobs:
                raise WorkflowReplayConflict(
                    "Partial media effects exist without the canonical PASS gate"
                )
            artifact = _reconcile_artifact(
                db,
                artifact_expected,
                allow_create=not replayed,
            )
            job = _reconcile_job(
                db,
                _zero_cost_job_values(
                    campaign_id=campaign_id,
                    scene_id=None,
                    provider="i5_media_integrity",
                    model="i5-media-manifest-v1",
                    input_hash=input_hash,
                    output_artifact_id=artifact.id,
                ),
                allow_create=not replayed,
            )
            gate = _reconcile_gate(
                db,
                gate_expected,
                allow_create=not replayed,
            )
            if replayed:
                if campaign.current_stage != "assembly":
                    raise WorkflowReplayConflict(
                        "Committed media gate conflicts with campaign stage"
                    )
            else:
                if campaign.current_stage != "media":
                    raise WorkflowTransitionError(
                        f"Campaign stage is {campaign.current_stage}; expected media"
                    )
                advanced = db.execute(
                    update(Campaign)
                    .where(
                        Campaign.id == campaign_id,
                        Campaign.current_stage == "media",
                    )
                    .values(current_stage="assembly", updated_at=datetime.utcnow())
                )
                if advanced.rowcount != 1:
                    raise WorkflowReplayConflict(
                        "Media compare-and-set advancement failed"
                    )
            return {
                "artifact_id": artifact.id,
                "campaign_id": campaign_id,
                "current_stage": "assembly",
                "gate_decision_id": gate.id,
                "generation_job_id": job.id,
                "outcome": "PASS",
                "output_hash": artifact.sha256,
                "replayed": replayed,
                "stage": "media",
            }


def reconcile_media_effect_set(
    db: Session,
    campaign_id: int,
    *,
    settings,  # type: ignore[no-untyped-def]
) -> dict[str, object]:
    context = reconcile_storyboard_effect_set(db, campaign_id)
    artifacts = list(
        db.scalars(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_MEDIA_MANIFEST_KIND,
            )
        )
    )
    if len(artifacts) != 1:
        raise WorkflowReplayConflict(
            "Campaign must contain exactly one accepted I5 media manifest"
        )
    artifact = artifacts[0]
    packet = _decode_json_artifact(artifact)
    from .media import build_media_manifest_from_session

    expected_packet = build_media_manifest_from_session(
        db,
        campaign_id=campaign_id,
        settings=settings,
    )
    if packet != expected_packet:
        raise WorkflowReplayConflict("Accepted media manifest no longer reconciles")
    input_hash = canonical_sha256(
        {
            "contract_version": "i5-media-stage-input-v1",
            "media_plan_hash": packet["media_plan_hash"],
            "production_profile_hash": packet["production_profile_hash"],
            "storyboard_hash": packet["storyboard_hash"],
            "voiceover_hash": dict(packet["voiceover"])["artifact_hash"],  # type: ignore[arg-type]
        }
    )
    artifact_expected = _json_artifact_values(
        campaign_id=campaign_id,
        kind=I5_MEDIA_MANIFEST_KIND,
        source_stage="media",
        payload=packet,
        input_hash=input_hash,
        provider_name="i5_media_integrity",
        provider_model="i5-media-manifest-v1",
    )
    _verify_fields(artifact, artifact_expected, label=I5_MEDIA_MANIFEST_KIND)
    gate_payload = dict(packet["gate"])  # type: ignore[arg-type]
    gate = _reconcile_gate(
        db,
        _gate_values(
            campaign_id=campaign_id,
            stage="media",
            policy_version=I5_PRODUCTION_POLICY_VERSION,
            input_hash=input_hash,
            outcome="PASS",
            output_hash=artifact.sha256,
            reasons=tuple(str(value) for value in gate_payload["reasons"]),
        ),
        allow_create=False,
    )
    job = _reconcile_job(
        db,
        _zero_cost_job_values(
            campaign_id=campaign_id,
            scene_id=None,
            provider="i5_media_integrity",
            model="i5-media-manifest-v1",
            input_hash=input_hash,
            output_artifact_id=artifact.id,
        ),
        allow_create=False,
    )
    budget_gate_ids = _reconcile_budget_block_gate_ids(
        db,
        campaign_id=campaign_id,
        profile_hash=str(context["profile_hash"]),
    )
    media_gates = list(
        db.scalars(
            select(GateDecision).where(
                GateDecision.campaign_id == campaign_id,
                GateDecision.stage == "media",
            )
        )
    )
    integrity_jobs = list(
        db.scalars(
            select(GenerationJob).where(
                GenerationJob.campaign_id == campaign_id,
                GenerationJob.provider == "i5_media_integrity",
                GenerationJob.model == "i5-media-manifest-v1",
            )
        )
    )
    if (
        {item.id for item in media_gates} != budget_gate_ids | {gate.id}
        or len(integrity_jobs) != 1
        or integrity_jobs[0].id != job.id
    ):
        raise WorkflowReplayConflict("Accepted media effect set is not exact")
    return {
        **context,
        "media_gate": gate,
        "media_hash": artifact.sha256,
        "media_job": job,
        "media_manifest_artifact": artifact,
        "media_packet": packet,
    }


def _validated_assembly_packet(
    db: Session,
    packet: Mapping[str, object],
    *,
    final_artifact_values: Mapping[str, object],
    settings,  # type: ignore[no-untyped-def]
) -> tuple[dict[str, object], dict[str, object], str]:
    campaign_id = int(packet.get("campaign_id") or 0)
    context = reconcile_media_effect_set(db, campaign_id, settings=settings)
    if set(packet) != {
        "campaign_id",
        "command_spec",
        "contract_version",
        "expected_duration_seconds",
        "ffmpeg_version",
        "ffprobe_version",
        "final_render_byte_size",
        "final_render_object_uri",
        "final_render_sha256",
        "gate",
        "media_manifest_hash",
        "observed_duration_seconds",
        "ordered_scene_segments",
        "production_profile_hash",
        "script_hash",
        "storyboard_hash",
        "stream_validation",
    }:
        raise WorkflowReplayConflict("Assembly manifest envelope is noncanonical")
    if packet.get("contract_version") != "i5-assembly-manifest-v1":
        raise WorkflowReplayConflict("Assembly manifest contract version is invalid")
    if (
        packet.get("script_hash") != context["script_hash"]
        or packet.get("production_profile_hash") != context["profile_hash"]
        or packet.get("storyboard_hash") != context["storyboard_hash"]
        or packet.get("media_manifest_hash") != context["media_hash"]
    ):
        raise WorkflowReplayConflict("Assembly manifest lineage is invalid")
    gate_payload = dict(packet.get("gate") or {})
    if gate_payload != {
        "outcome": "PASS",
        "reasons": ["canonical_render_and_ffprobe_validation_passed"],
    }:
        raise WorkflowReplayConflict("Assembly gate payload is invalid")

    from .media import CanonicalMediaStore, canonical_i5_path
    from .renderer import (
        build_final_assembly_plan,
        build_scene_segment_plan,
        capture_tool_versions,
        probe_media,
        sha256_file,
        validate_final_video,
    )

    profile_renderer = dict(
        cast(Mapping[str, object], context["profile_payload"])["renderer"]  # type: ignore[index]
    )
    versions = capture_tool_versions()
    if (
        packet.get("ffmpeg_version") != versions.ffmpeg_version
        or packet.get("ffprobe_version") != versions.ffprobe_version
        or profile_renderer.get("ffmpeg_version") != versions.ffmpeg_version
        or profile_renderer.get("ffprobe_version") != versions.ffprobe_version
    ):
        raise WorkflowReplayConflict("Assembly tool identity conflicts with profile")

    media_packet = cast(Mapping[str, object], context["media_packet"])
    scene_media = [
        dict(item)
        for item in cast(Sequence[Mapping[str, object]], media_packet["scene_media"])
    ]
    voiceover = dict(media_packet["voiceover"])  # type: ignore[arg-type]
    assembly_input_hash = canonical_sha256(
        {
            "contract_version": "i5-assembly-input-v1",
            "media_manifest_hash": context["media_hash"],
            "ordered_scene_media": scene_media,
            "production_profile_hash": context["profile_hash"],
            "voiceover_hash": voiceover["artifact_hash"],
        }
    )
    work_root = canonical_i5_path(
        settings.output_path,
        f"campaign_{campaign_id}",
        "work",
        assembly_input_hash,
    )
    store = CanonicalMediaStore(settings.output_path, campaign_id)
    records = [
        dict(item)
        for item in cast(
            Sequence[Mapping[str, object]],
            packet.get("ordered_scene_segments", []),
        )
    ]
    if len(records) != len(scene_media):
        raise WorkflowReplayConflict("Assembly segment count is invalid")
    expected_duration = 0.0
    segment_paths = []
    canonical_records: list[dict[str, object]] = []
    for expected_position, (media_item, record) in enumerate(
        zip(scene_media, records, strict=True)
    ):
        if int(media_item["scene_position"]) != expected_position:
            raise WorkflowReplayConflict("Media manifest scene order is invalid")
        narration = dict(media_item["narration"])  # type: ignore[arg-type]
        visual = dict(media_item["visual"])  # type: ignore[arg-type]
        duration = float(narration["duration_seconds"])
        expected_duration += duration
        audio_path = store.resolve(
            str(narration["object_uri"]),
            expected_sha256=str(narration["artifact_hash"]),
        )
        visual_path = store.resolve(
            str(visual["object_uri"]),
            expected_sha256=str(visual["artifact_hash"]),
        )
        visual_kind = (
            "video" if str(visual["mime_type"]).startswith("video/") else "image"
        )
        segment_path = work_root / f"segment_{expected_position:03d}.mp4"
        if not segment_path.is_file():
            raise WorkflowReplayConflict("Assembly segment is missing")
        plan = build_scene_segment_plan(
            visual_path,
            audio_path,
            segment_path,
            scene_position=expected_position,
            visual_kind=visual_kind,  # type: ignore[arg-type]
            narration_duration_seconds=duration,
        )
        probe = probe_media(segment_path)
        if abs(probe.duration_seconds - duration) > 0.15:
            raise WorkflowReplayConflict("Assembly segment duration is invalid")
        segment_hash = sha256_file(segment_path)
        canonical_record = {
            "command": plan.metadata(),
            "narration_hash": narration["artifact_hash"],
            "observed_duration_seconds": round(probe.duration_seconds, 6),
            "scene_position": expected_position,
            "segment_sha256": segment_hash,
            "visual_hash": visual["artifact_hash"],
            "visual_kind": visual_kind,
        }
        if record != canonical_record:
            raise WorkflowReplayConflict("Assembly segment manifest is noncanonical")
        canonical_records.append(canonical_record)
        segment_paths.append(segment_path)

    final_uri = str(packet.get("final_render_object_uri") or "")
    final_hash = str(packet.get("final_render_sha256") or "")
    final_path = store.resolve(final_uri, expected_sha256=final_hash)
    final_probe = validate_final_video(
        final_path,
        expected_duration_seconds=expected_duration,
        duration_tolerance_seconds=max(0.20, len(records) * 0.08),
    )
    final_size = final_path.stat().st_size
    if (
        packet.get("final_render_byte_size") != final_size
        or packet.get("expected_duration_seconds") != round(expected_duration, 6)
        or packet.get("observed_duration_seconds")
        != round(final_probe.duration_seconds, 6)
    ):
        raise WorkflowReplayConflict("Assembly final-render measurements are invalid")
    portable_probe = {
        key: value
        for key, value in final_probe.metadata().items()
        if key != "path"
    }
    if packet.get("stream_validation") != portable_probe:
        raise WorkflowReplayConflict("Assembly stream validation is invalid")
    final_plan = build_final_assembly_plan(
        segment_paths,
        work_root / "final.mp4",
    )
    expected_command_spec = {
        "final_assembly": final_plan.metadata(),
        "scene_segments": [record["command"] for record in canonical_records],
    }
    if packet.get("command_spec") != expected_command_spec:
        raise WorkflowReplayConflict("Assembly command specification is invalid")

    expected_final_values = binary_artifact_values(
        campaign_id=campaign_id,
        kind=I5_FINAL_RENDER_KIND,
        source_stage="assembly",
        uri=final_uri,
        sha256=final_hash,
        byte_size=final_size,
        mime_type="video/mp4",
        provider_name="ffmpeg_local",
        provider_model="i5-ffmpeg-renderer-v1",
        input_hash=assembly_input_hash,
        provenance={
            "campaign_id": campaign_id,
            "media_manifest_hash": context["media_hash"],
            "media_validation": portable_probe,
            "production_profile_hash": context["profile_hash"],
            "rights_basis": "assembled_canonical_i5_media",
        },
    )
    if dict(final_artifact_values) != expected_final_values:
        raise WorkflowReplayConflict("Final render artifact metadata is noncanonical")
    canonical_packet = dict(packet)
    return canonical_packet, expected_final_values, assembly_input_hash


def persist_assembly_outputs(
    packet: dict[str, object],
    *,
    final_artifact_values: Mapping[str, object],
    settings,  # type: ignore[no-untyped-def]
) -> dict[str, object]:
    campaign_id = int(packet.get("campaign_id") or 0)
    with SessionLocal() as db:
        with serialized_transaction(db):
            campaign = campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            canonical_packet, expected_final, input_hash = _validated_assembly_packet(
                db,
                packet,
                final_artifact_values=final_artifact_values,
                settings=settings,
            )
            manifest_expected = _json_artifact_values(
                campaign_id=campaign_id,
                kind=I5_ASSEMBLY_MANIFEST_KIND,
                source_stage="assembly",
                payload=canonical_packet,
                input_hash=input_hash,
                provider_name="ffmpeg_local",
                provider_model="i5-assembly-manifest-v1",
            )
            gate_expected = _gate_values(
                campaign_id=campaign_id,
                stage="assembly",
                policy_version=I5_PRODUCTION_POLICY_VERSION,
                input_hash=input_hash,
                outcome="PASS",
                output_hash=str(manifest_expected["sha256"]),
                reasons=("canonical_render_and_ffprobe_validation_passed",),
            )
            existing_gate = db.scalar(
                select(GateDecision).where(
                    GateDecision.campaign_id == campaign_id,
                    GateDecision.stage == "assembly",
                    GateDecision.policy_version == I5_PRODUCTION_POLICY_VERSION,
                    GateDecision.input_hash == input_hash,
                )
            )
            replayed = existing_gate is not None
            final_rows = list(
                db.scalars(
                    select(Artifact).where(
                        Artifact.campaign_id == campaign_id,
                        Artifact.kind == I5_FINAL_RENDER_KIND,
                    )
                )
            )
            manifest_rows = list(
                db.scalars(
                    select(Artifact).where(
                        Artifact.campaign_id == campaign_id,
                        Artifact.kind == I5_ASSEMBLY_MANIFEST_KIND,
                    )
                )
            )
            renderer_jobs = list(
                db.scalars(
                    select(GenerationJob).where(
                        GenerationJob.campaign_id == campaign_id,
                        GenerationJob.provider == "ffmpeg_local",
                        GenerationJob.model == "i5-ffmpeg-renderer-v1",
                    )
                )
            )
            assembly_gates = list(
                db.scalars(
                    select(GateDecision).where(
                        GateDecision.campaign_id == campaign_id,
                        GateDecision.stage == "assembly",
                    )
                )
            )
            machine_qa_gates = list(
                db.scalars(
                    select(GateDecision).where(
                        GateDecision.campaign_id == campaign_id,
                        GateDecision.stage == "machine_qa",
                    )
                )
            )
            if machine_qa_gates:
                raise WorkflowReplayConflict("I5 cannot persist machine-QA effects")
            effect_counts = (
                len(final_rows),
                len(manifest_rows),
                len(renderer_jobs),
                len(assembly_gates),
            )
            if replayed:
                if effect_counts != (1, 1, 1, 1):
                    raise WorkflowReplayConflict(
                        "Committed assembly effect-set cardinality is invalid"
                    )
            elif effect_counts != (0, 0, 0, 0):
                raise WorkflowReplayConflict(
                    "Partial assembly effects exist without the canonical gate"
                )
            final_artifact = _reconcile_artifact(
                db,
                expected_final,
                allow_create=not replayed,
            )
            manifest = _reconcile_artifact(
                db,
                manifest_expected,
                allow_create=not replayed,
            )
            job = _reconcile_job(
                db,
                _zero_cost_job_values(
                    campaign_id=campaign_id,
                    scene_id=None,
                    provider="ffmpeg_local",
                    model="i5-ffmpeg-renderer-v1",
                    input_hash=input_hash,
                    output_artifact_id=final_artifact.id,
                ),
                allow_create=not replayed,
            )
            gate = _reconcile_gate(
                db,
                gate_expected,
                allow_create=not replayed,
            )
            if replayed:
                if campaign.current_stage != "machine_qa":
                    raise WorkflowReplayConflict(
                        "Committed assembly gate conflicts with campaign stage"
                    )
            else:
                if campaign.current_stage != "assembly":
                    raise WorkflowTransitionError(
                        f"Campaign stage is {campaign.current_stage}; expected assembly"
                    )
                advanced = db.execute(
                    update(Campaign)
                    .where(
                        Campaign.id == campaign_id,
                        Campaign.current_stage == "assembly",
                    )
                    .values(current_stage="machine_qa", updated_at=datetime.utcnow())
                )
                if advanced.rowcount != 1:
                    raise WorkflowReplayConflict(
                        "Assembly compare-and-set advancement failed"
                    )
            return {
                "assembly_manifest_artifact_id": manifest.id,
                "campaign_id": campaign_id,
                "current_stage": "machine_qa",
                "final_render_artifact_id": final_artifact.id,
                "final_render_hash": final_artifact.sha256,
                "gate_decision_id": gate.id,
                "generation_job_id": job.id,
                "outcome": "PASS",
                "output_hash": manifest.sha256,
                "replayed": replayed,
                "stage": "assembly",
            }


def latest_json_artifact(
    db: Session,
    campaign_id: int,
    kind: str,
) -> tuple[Artifact | None, dict[str, object] | None]:
    artifact = db.scalar(
        select(Artifact)
        .where(Artifact.campaign_id == campaign_id, Artifact.kind == kind)
        .order_by(Artifact.id.desc())
        .limit(1)
    )
    if artifact is None:
        return None, None
    return artifact, _decode_json_artifact(artifact)


def production_snapshot(campaign_id: int) -> dict[str, object]:
    policy = load_campaign_budget_policy()
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
        script = reconcile_accepted_i4_script_packet(db, campaign_id)
        profile, _ = latest_json_artifact(db, campaign_id, I5_PRODUCTION_PROFILE_KIND)
        storyboard, storyboard_payload = latest_json_artifact(
            db, campaign_id, I5_STORYBOARD_KIND
        )
        media, media_payload = latest_json_artifact(db, campaign_id, I5_MEDIA_MANIFEST_KIND)
        voiceover = db.scalar(
            select(Artifact)
            .where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_VOICEOVER_KIND,
            )
            .order_by(Artifact.id.desc())
            .limit(1)
        )
        render = db.scalar(
            select(Artifact)
            .where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == I5_FINAL_RENDER_KIND,
            )
            .order_by(Artifact.id.desc())
            .limit(1)
        )
        assembly, _ = latest_json_artifact(db, campaign_id, I5_ASSEMBLY_MANIFEST_KIND)
        cost = campaign_cost(db, campaign_id)
        cap = authorized_campaign_cap(db, campaign_id)
        latest_block, block_payload = latest_json_artifact(
            db, campaign_id, I5_BUDGET_BLOCK_KIND
        )
        latest_gate = db.scalar(
            select(GateDecision)
            .where(
                GateDecision.campaign_id == campaign_id,
                GateDecision.policy_version.in_(
                    (I5_PRODUCTION_POLICY_VERSION, I5_BUDGET_GATE_POLICY_VERSION)
                ),
            )
            .order_by(GateDecision.id.desc())
            .limit(1)
        )
        scenes = list(
            db.scalars(
                select(Scene)
                .where(Scene.campaign_id == campaign_id)
                .order_by(Scene.position)
            )
        )
        visual_counts: dict[str, int] = {}
        for scene in scenes:
            visual_counts[scene.visual_mode] = visual_counts.get(scene.visual_mode, 0) + 1
        return {
            "assembly_manifest_hash": assembly.sha256 if assembly else None,
            "authorized_hard_cap_microusd": cap,
            "campaign_id": campaign_id,
            "current_effective_campaign_cost_microusd": cost.total_microusd,
            "current_stage": campaign.current_stage,
            "final_render_hash": render.sha256 if render else None,
            "generated_media_counts": (
                dict(media_payload.get("generated_media_summary") or {})
                if media_payload
                else {}
            ),
            "i4_script_hash": script["script_hash"],
            "latest_budget_block": block_payload if latest_block else None,
            "latest_i5_gate": (
                {
                    "outcome": latest_gate.outcome,
                    "policy_version": latest_gate.policy_version,
                    "stage": latest_gate.stage,
                }
                if latest_gate
                else None
            ),
            "media_manifest_hash": media.sha256 if media else None,
            "production_profile_hash": profile.sha256 if profile else None,
            "production_workflow_id": campaign.production_workflow_id,
            "reserved_cost_microusd": cost.reserved_microusd,
            "scene_count": len(scenes),
            "soft_warning_active": cost.total_microusd
            >= policy.limits_microusd.soft_warning,
            "storyboard_hash": storyboard.sha256 if storyboard else None,
            "visual_mode_counts": (
                dict(storyboard_payload.get("visual_mode_counts") or visual_counts)
                if storyboard_payload
                else visual_counts
            ),
            "voiceover_hash": voiceover.sha256 if voiceover else None,
        }


def retry_on_integrity_error(operation):  # type: ignore[no-untyped-def]
    try:
        return operation()
    except IntegrityError:
        try:
            return operation()
        except IntegrityError as exc:
            raise WorkflowReplayConflict(
                "I5 deterministic identity violated a uniqueness constraint"
            ) from exc
