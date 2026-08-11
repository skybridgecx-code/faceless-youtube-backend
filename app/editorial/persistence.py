from __future__ import annotations

import json
import re
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Mapping, cast

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import (
    Artifact,
    Campaign,
    Claim,
    ClaimSource,
    GateDecision,
    GenerationJob,
    SessionLocal,
    Source,
)
from app.models import Channel, Video
from app.workflows.persistence import (
    CampaignNotFoundError,
    WorkflowReplayConflict,
    WorkflowTransitionError,
)

from .claims import (
    claim_hash as deterministic_claim_hash,
    claim_structured_value_json,
    evaluate_claim,
    normalize_whitespace,
    source_record_content_sha256,
)
from .contracts import (
    ClaimSeed,
    DemandSnapshot,
    EditorialSeed,
    EvidenceSourceInput,
    I4_POLICY_VERSION,
    canonical_json,
    canonical_sha256,
)


I4_SEED_KIND = "i4_editorial_seed"
I4_TOPIC_KIND = "i4_topic_packet"
I4_RESEARCH_KIND = "i4_research_packet"
I4_SCRIPT_KIND = "i4_script"
_WORKFLOW_ID_PATTERN = re.compile(r"^campaign:(?P<campaign_id>[1-9][0-9]*):i4:(?P<seed_hash>[0-9a-f]{64})$")


def i4_campaign_workflow_id(campaign_id: int, seed_hash: str) -> str:
    if campaign_id <= 0:
        raise ValueError("campaign_id must be positive")
    if re.fullmatch(r"[0-9a-f]{64}", seed_hash) is None:
        raise ValueError("seed_hash must be a full lowercase SHA-256 value")
    return f"campaign:{campaign_id}:i4:{seed_hash}"


def _begin_serialized_write(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        db.begin()


def _campaign_for_update(db: Session, campaign_id: int) -> Campaign | None:
    return db.scalar(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .with_for_update()
    )


@contextmanager
def _serialized_transaction(db: Session) -> Iterator[None]:
    _begin_serialized_write(db)
    try:
        yield
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()


def _artifact_provenance(*, input_hash: str, origin: str) -> str:
    return canonical_json(
        {
            "contract_version": "i4-artifact-provenance-v1",
            "hash_scope": "exact_payload_json_utf8_bytes",
            "immutable": True,
            "input_hash": input_hash,
            "origin": origin,
        }
    )


def _seed_artifact_values(
    campaign_id: int,
    seed: EditorialSeed,
) -> dict[str, object]:
    payload_json = seed.canonical_json(campaign_id)
    seed_hash = canonical_sha256(json.loads(payload_json))
    return {
        "byte_size": len(payload_json.encode("utf-8")),
        "campaign_id": campaign_id,
        "kind": I4_SEED_KIND,
        "mime_type": "application/json",
        "payload_json": payload_json,
        "prompt_template_version": "i4-editorial-seed-v1",
        "provenance_json": _artifact_provenance(
            input_hash=seed_hash,
            origin="operator_editorial_seed",
        ),
        "provider_model": "i4-editorial-seed-v1",
        "provider_name": "operator_input",
        "sha256": seed_hash,
        "source_stage": "topic",
        "uri": f"artifact://campaign/{campaign_id}/{I4_SEED_KIND}/{seed_hash}",
    }


def _verify_artifact_fields(
    artifact: Artifact,
    expected: Mapping[str, object],
) -> None:
    actual = {key: getattr(artifact, key) for key in expected}
    if actual != dict(expected):
        raise WorkflowReplayConflict(
            f"Existing {expected['kind']} artifact conflicts with immutable content"
        )


def _decode_canonical_artifact_payload(artifact: Artifact) -> dict[str, object]:
    if artifact.payload_json is None:
        raise WorkflowReplayConflict(f"{artifact.kind} artifact has no canonical payload")
    try:
        payload = json.loads(artifact.payload_json)
    except json.JSONDecodeError as exc:
        raise WorkflowReplayConflict(f"{artifact.kind} artifact payload is invalid") from exc
    if not isinstance(payload, dict):
        raise WorkflowReplayConflict(f"{artifact.kind} artifact payload must be an object")
    if (
        canonical_json(payload) != artifact.payload_json
        or canonical_sha256(payload) != artifact.sha256
    ):
        raise WorkflowReplayConflict(f"{artifact.kind} artifact content hash is invalid")
    return payload


def _active_seed_artifact(db: Session, campaign_id: int) -> Artifact | None:
    return db.scalar(
        select(Artifact)
        .where(
            Artifact.campaign_id == campaign_id,
            Artifact.kind == I4_SEED_KIND,
        )
        .order_by(Artifact.id.desc())
        .limit(1)
    )


def _decode_seed_artifact(artifact: Artifact, campaign_id: int) -> EditorialSeed:
    if artifact.payload_json is None:
        raise WorkflowReplayConflict("Editorial seed artifact has no canonical payload")
    try:
        raw = json.loads(artifact.payload_json)
        seed = EditorialSeed.model_validate({"candidates": raw["candidates"]})
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkflowReplayConflict("Editorial seed artifact payload is invalid") from exc
    expected = _seed_artifact_values(campaign_id, seed)
    _verify_artifact_fields(artifact, expected)
    return seed


def _persist_editorial_seed_once(
    campaign_id: int,
    seed: EditorialSeed,
) -> dict[str, object]:
    expected = _seed_artifact_values(campaign_id, seed)
    with SessionLocal() as db:
        try:
            _begin_serialized_write(db)
            campaign = _campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            if campaign.policy_version != I4_POLICY_VERSION:
                raise WorkflowTransitionError(
                    "Editorial seeds are supported only for I4 campaigns"
                )

            active = _active_seed_artifact(db, campaign_id)
            exact = db.scalar(
                select(Artifact).where(
                    Artifact.campaign_id == campaign_id,
                    Artifact.kind == I4_SEED_KIND,
                    Artifact.sha256 == expected["sha256"],
                )
            )
            if campaign.workflow_id is not None:
                if active is None or active.sha256 != expected["sha256"]:
                    raise WorkflowTransitionError(
                        "Editorial seed is immutable after workflow binding"
                    )
                _verify_artifact_fields(active, expected)
                db.commit()
                return {
                    "artifact_id": active.id,
                    "created": False,
                    "seed_hash": active.sha256,
                }

            if exact is not None:
                _verify_artifact_fields(exact, expected)
                db.commit()
                return {
                    "artifact_id": exact.id,
                    "created": False,
                    "seed_hash": exact.sha256,
                }

            artifact = Artifact(**expected)
            db.add(artifact)
            db.flush()
            result = {
                "artifact_id": artifact.id,
                "created": True,
                "seed_hash": artifact.sha256,
            }
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def persist_editorial_seed(
    campaign_id: int,
    seed: EditorialSeed,
) -> dict[str, object]:
    try:
        return _persist_editorial_seed_once(campaign_id, seed)
    except IntegrityError:
        try:
            return _persist_editorial_seed_once(campaign_id, seed)
        except IntegrityError as exc:
            raise WorkflowReplayConflict(
                "Editorial seed identity violated a uniqueness constraint"
            ) from exc


def load_active_editorial_seed(
    campaign_id: int,
    *,
    expected_hash: str | None = None,
) -> tuple[Artifact, EditorialSeed]:
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
        artifact = _active_seed_artifact(db, campaign_id)
        if artifact is None:
            raise WorkflowTransitionError("I4 campaign requires an editorial seed")
        if expected_hash is not None and artifact.sha256 != expected_hash:
            raise WorkflowReplayConflict(
                "Active editorial seed changed before workflow binding"
            )
        seed = _decode_seed_artifact(artifact, campaign_id)
        return artifact, seed


def bind_i4_campaign_workflow(campaign_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        try:
            _begin_serialized_write(db)
            campaign = _campaign_for_update(db, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
            if campaign.policy_version != I4_POLICY_VERSION:
                raise WorkflowTransitionError("Campaign is not governed by the I4 policy")
            active = _active_seed_artifact(db, campaign_id)
            if active is None:
                raise WorkflowTransitionError("I4 campaign requires an editorial seed")
            seed = _decode_seed_artifact(active, campaign_id)
            expected_workflow_id = i4_campaign_workflow_id(campaign_id, active.sha256)

            if campaign.workflow_id is not None:
                if campaign.workflow_id != expected_workflow_id:
                    raise WorkflowReplayConflict(
                        "Campaign workflow identity conflicts with the active editorial seed"
                    )
            else:
                if campaign.current_stage != "topic":
                    raise WorkflowTransitionError(
                        "An unbound I4 campaign must begin at the topic stage"
                    )
                bound = db.execute(
                    update(Campaign)
                    .where(
                        Campaign.id == campaign_id,
                        Campaign.workflow_id.is_(None),
                        Campaign.current_stage == "topic",
                    )
                    .values(
                        workflow_id=expected_workflow_id,
                        updated_at=datetime.utcnow(),
                    )
                )
                if bound.rowcount != 1:
                    raise WorkflowReplayConflict(
                        "Campaign workflow identity changed concurrently"
                    )
            result = {
                "campaign_id": campaign_id,
                "seed_hash": active.sha256,
                "seed_payload": seed.artifact_payload(campaign_id),
                "workflow_id": expected_workflow_id,
            }
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def _canonical_topic_compilation_context(
    db: Session,
    campaign: Campaign,
) -> dict[str, object]:
    channel = db.get(Channel, campaign.channel_id)
    if channel is None:
        raise WorkflowReplayConflict("Campaign channel no longer exists")

    history = [
        str(title)
        for title in db.scalars(
            select(Video.title).where(Video.channel_id == campaign.channel_id)
        )
    ]
    prior_artifacts = db.scalars(
        select(Artifact)
        .join(Campaign, Artifact.campaign_id == Campaign.id)
        .where(
            Campaign.channel_id == campaign.channel_id,
            Artifact.campaign_id != campaign.id,
            Artifact.kind == I4_TOPIC_KIND,
            Artifact.payload_json.is_not(None),
        )
    )
    for artifact in prior_artifacts:
        packet = _verify_packet_artifact(
            db,
            artifact.campaign_id,
            kind=I4_TOPIC_KIND,
            expected_hash=artifact.sha256,
        )
        if dict(packet.get("gate") or {}).get("outcome") != "PASS":
            continue
        selected_key = packet.get("selected_candidate_key")
        ranked_candidates = packet.get("ranked_candidates", [])
        if not isinstance(ranked_candidates, list):
            raise WorkflowReplayConflict(
                "Historical I4 topic artifact contains invalid canonical content"
            )
        for ranked in ranked_candidates:
            if not isinstance(ranked, dict):
                raise WorkflowReplayConflict(
                    "Historical I4 topic artifact contains invalid canonical content"
                )
            if ranked.get("candidate_key") == selected_key:
                history.append(
                    f"{ranked.get('topic', '')} {ranked.get('angle', '')}".strip()
                )
                break
    return {
        "channel_audience": channel.audience,
        "channel_niche": channel.niche,
        "novelty_history": tuple(sorted(history)),
    }


def load_topic_compilation_context(
    campaign_id: int,
    seed_hash: str,
) -> dict[str, object]:
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
        expected_workflow_id = i4_campaign_workflow_id(campaign_id, seed_hash)
        if campaign.workflow_id != expected_workflow_id:
            raise WorkflowReplayConflict(
                "Campaign is not bound to the requested I4 editorial seed"
            )
        active = _active_seed_artifact(db, campaign_id)
        if active is None or active.sha256 != seed_hash:
            raise WorkflowReplayConflict("Bound editorial seed is no longer active")
        _decode_seed_artifact(active, campaign_id)
        return _canonical_topic_compilation_context(db, campaign)


def _stage_input_hash(stage: str, packet: Mapping[str, object]) -> str:
    if stage == "topic":
        identity = {
            "compilation_context": packet["compilation_context"],
            "contract_version": "i4-topic-stage-input-v1",
            "demand_snapshot_hash": packet["demand_snapshot_hash"],
            "policy_version": packet["policy_version"],
            "seed_hash": packet["seed_hash"],
            "stage": stage,
        }
    elif stage == "research":
        identity = {
            "contract_version": "i4-research-stage-input-v1",
            "policy_version": packet["policy_version"],
            "selected_candidate_key": packet["selected_candidate_key"],
            "stage": stage,
            "topic_packet_hash": packet["topic_packet_hash"],
        }
    elif stage == "script":
        identity = {
            "contract_version": "i4-script-stage-input-v1",
            "policy_version": packet["policy_version"],
            "research_packet_hash": packet["research_packet_hash"],
            "stage": stage,
            "topic_packet_hash": packet["topic_packet_hash"],
        }
    else:
        raise WorkflowTransitionError(f"Unsupported I4 persistence stage: {stage}")
    return canonical_sha256(identity)


def _stage_spec(stage: str, packet: Mapping[str, object]) -> dict[str, object]:
    kind_by_stage = {
        "topic": I4_TOPIC_KIND,
        "research": I4_RESEARCH_KIND,
        "script": I4_SCRIPT_KIND,
    }
    provider_by_stage = {
        "topic": ("youtube_public_data+deterministic", "i4-topic-intelligence-v1"),
        "research": ("deterministic_editorial", "i4-claims-v1"),
        "script": ("deterministic_editorial", "i4-script-compiler-v1"),
    }
    if stage not in kind_by_stage:
        raise WorkflowTransitionError(f"Unsupported I4 persistence stage: {stage}")
    expected_contract = {
        "topic": "i4-topic-packet-v1",
        "research": "i4-research-packet-v1",
        "script": "i4-script-v1",
    }[stage]
    if packet.get("contract_version") != expected_contract:
        raise WorkflowReplayConflict("I4 packet contract version is invalid")
    if packet.get("policy_version") != I4_POLICY_VERSION:
        raise WorkflowReplayConflict("I4 packet policy version is invalid")
    campaign_id = int(packet["campaign_id"])
    if campaign_id <= 0:
        raise WorkflowTransitionError("I4 packet campaign_id must be positive")
    payload_json = canonical_json(packet)
    output_hash = canonical_sha256(packet)
    input_hash = _stage_input_hash(stage, packet)
    provider, model = provider_by_stage[stage]
    kind = kind_by_stage[stage]
    gate = dict(packet["gate"])  # type: ignore[arg-type]
    outcome = str(gate.get("outcome"))
    if outcome not in {"PASS", "FAIL"}:
        raise WorkflowTransitionError("I4 stage gate must be PASS or FAIL")
    reasons = [str(reason) for reason in gate.get("reasons", [])]
    return {
        "artifact": {
            "byte_size": len(payload_json.encode("utf-8")),
            "campaign_id": campaign_id,
            "kind": kind,
            "mime_type": "application/json",
            "payload_json": payload_json,
            "prompt_template_version": f"{model}-contract",
            "provenance_json": _artifact_provenance(
                input_hash=input_hash,
                origin="i4_durable_editorial_workflow",
            ),
            "provider_model": model,
            "provider_name": provider,
            "sha256": output_hash,
            "source_stage": stage,
            "uri": f"artifact://campaign/{campaign_id}/{kind}/{output_hash}",
        },
        "campaign_id": campaign_id,
        "input_hash": input_hash,
        "model": model,
        "outcome": outcome,
        "output_hash": output_hash,
        "provider": provider,
        "reasons_json": canonical_json(reasons),
        "stage": stage,
    }


def _verify_packet_artifact(
    db: Session,
    campaign_id: int,
    *,
    kind: str,
    expected_hash: object,
) -> dict[str, object]:
    hash_value = str(expected_hash)
    if re.fullmatch(r"[0-9a-f]{64}", hash_value) is None:
        raise WorkflowReplayConflict("I4 packet lineage hash is invalid")
    artifact = db.scalar(
        select(Artifact).where(
            Artifact.campaign_id == campaign_id,
            Artifact.kind == kind,
            Artifact.sha256 == hash_value,
        )
    )
    if artifact is None or artifact.payload_json is None:
        raise WorkflowReplayConflict("I4 packet lineage artifact is missing")
    payload = _decode_canonical_artifact_payload(artifact)
    stage_by_kind = {
        I4_TOPIC_KIND: "topic",
        I4_RESEARCH_KIND: "research",
        I4_SCRIPT_KIND: "script",
    }
    stage = stage_by_kind.get(kind)
    if stage is None:
        raise WorkflowReplayConflict("I4 packet lineage artifact kind is invalid")
    spec = _stage_spec(stage, payload)
    _verify_artifact_fields(
        artifact,
        cast(Mapping[str, object], spec["artifact"]),
    )
    gate = db.scalar(
        select(GateDecision).where(
            GateDecision.campaign_id == campaign_id,
            GateDecision.stage == stage,
            GateDecision.policy_version == I4_POLICY_VERSION,
            GateDecision.input_hash == spec["input_hash"],
        )
    )
    if gate is None:
        raise WorkflowReplayConflict("I4 packet lineage gate is missing")
    _verify_gate(gate, spec)
    _reconcile_job(db, spec, artifact.id, allow_create=False)
    return payload


def _topic_demand_from_packet(
    seed: EditorialSeed,
    packet: Mapping[str, object],
) -> DemandSnapshot:
    try:
        ranked = [dict(item) for item in packet["ranked_candidates"]]  # type: ignore[index]
        ranked_by_key = {
            str(item["candidate_key"]): dict(item["demand_evidence"])  # type: ignore[arg-type]
            for item in ranked
        }
        if (
            len(ranked) != len(seed.candidates)
            or len(ranked_by_key) != len(seed.candidates)
            or set(ranked_by_key)
            != {candidate.candidate_key for candidate in seed.candidates}
        ):
            raise ValueError("ranked candidate identities do not match the seed")
        demand = DemandSnapshot.model_validate(
            {
                "candidates": [
                    ranked_by_key[candidate.candidate_key]
                    for candidate in seed.candidates
                ],
                "contract_version": "i4-youtube-demand-v1",
                "retrieved_at": packet["retrieved_at"],
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowReplayConflict(
            "Topic packet demand evidence is not canonical"
        ) from exc
    if canonical_sha256(demand.model_dump(mode="json")) != packet.get(
        "demand_snapshot_hash"
    ):
        raise WorkflowReplayConflict("Topic packet demand snapshot hash is invalid")
    return demand


def _verify_bound_stage_lineage(
    db: Session,
    campaign: Campaign,
    stage: str,
    packet: Mapping[str, object],
) -> None:
    match = _WORKFLOW_ID_PATTERN.fullmatch(campaign.workflow_id or "")
    if match is None or int(match.group("campaign_id")) != campaign.id:
        raise WorkflowReplayConflict("Campaign is not bound to a canonical I4 workflow")
    bound_seed_hash = match.group("seed_hash")
    active_seed = _active_seed_artifact(db, campaign.id)
    if active_seed is None or active_seed.sha256 != bound_seed_hash:
        raise WorkflowReplayConflict("Bound I4 editorial seed is no longer active")
    bound_seed = _decode_seed_artifact(active_seed, campaign.id)

    if stage == "topic":
        if packet.get("seed_hash") != bound_seed_hash:
            raise WorkflowReplayConflict("Topic packet does not match the bound I4 seed")
        demand = _topic_demand_from_packet(bound_seed, packet)
        try:
            packet_context = dict(packet["compilation_context"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowReplayConflict(
                "Topic packet compilation context is invalid"
            ) from exc
        current_context = _canonical_topic_compilation_context(db, campaign)
        expected_context = {
            "channel_audience": current_context["channel_audience"],
            "channel_niche": current_context["channel_niche"],
            "novelty_history": list(current_context["novelty_history"]),  # type: ignore[arg-type]
        }
        if packet_context != expected_context:
            raise WorkflowReplayConflict(
                "Topic packet compilation context conflicts with campaign lineage"
            )
        from .topic_intelligence import compile_topic_packet

        expected_topic = compile_topic_packet(
            campaign_id=campaign.id,
            seed_hash=bound_seed_hash,
            seed=bound_seed,
            demand=demand,
            channel_niche=str(current_context["channel_niche"]),
            channel_audience=str(current_context["channel_audience"]),
            novelty_history=tuple(current_context["novelty_history"]),  # type: ignore[arg-type]
        )
        if dict(packet) != expected_topic:
            raise WorkflowReplayConflict(
                "Topic packet does not match deterministic compilation"
            )
    elif stage == "research":
        topic_payload = _verify_packet_artifact(
            db,
            campaign.id,
            kind=I4_TOPIC_KIND,
            expected_hash=packet.get("topic_packet_hash"),
        )
        if (
            topic_payload.get("campaign_id") != campaign.id
            or topic_payload.get("policy_version") != I4_POLICY_VERSION
            or dict(topic_payload.get("gate") or {}).get("outcome") != "PASS"
            or packet.get("selected_candidate_key")
            != topic_payload.get("selected_candidate_key")
        ):
            raise WorkflowReplayConflict("Research packet lineage conflicts with topic")
        from .claims import compile_research_packet

        expected_research = compile_research_packet(
            campaign_id=campaign.id,
            seed=bound_seed,
            topic_packet=topic_payload,
            topic_packet_hash=canonical_sha256(topic_payload),
        )
        if dict(packet) != expected_research:
            raise WorkflowReplayConflict(
                "Research packet does not match deterministic compilation"
            )
    elif stage == "script":
        topic_payload = _verify_packet_artifact(
            db,
            campaign.id,
            kind=I4_TOPIC_KIND,
            expected_hash=packet.get("topic_packet_hash"),
        )
        research_payload = _verify_packet_artifact(
            db,
            campaign.id,
            kind=I4_RESEARCH_KIND,
            expected_hash=packet.get("research_packet_hash"),
        )
        selected_key = packet.get("selected_candidate_key")
        if (
            topic_payload.get("campaign_id") != campaign.id
            or research_payload.get("campaign_id") != campaign.id
            or topic_payload.get("policy_version") != I4_POLICY_VERSION
            or research_payload.get("policy_version") != I4_POLICY_VERSION
            or dict(topic_payload.get("gate") or {}).get("outcome") != "PASS"
            or dict(research_payload.get("gate") or {}).get("outcome") != "PASS"
            or selected_key != topic_payload.get("selected_candidate_key")
            or selected_key != research_payload.get("selected_candidate_key")
            or research_payload.get("topic_packet_hash")
            != packet.get("topic_packet_hash")
            or packet.get("viewer_promise") != topic_payload.get("viewer_promise")
        ):
            raise WorkflowReplayConflict("Script packet lineage conflicts with research")
        from .script_compiler import compile_script_packet

        expected_script = compile_script_packet(
            campaign_id=campaign.id,
            topic_packet=topic_payload,
            topic_packet_hash=canonical_sha256(topic_payload),
            research_packet=research_payload,
            research_packet_hash=canonical_sha256(research_payload),
        )
        if dict(packet) != expected_script:
            raise WorkflowReplayConflict(
                "Script packet does not match deterministic compilation"
            )
        _reconcile_research_entities(
            db,
            campaign.id,
            research_payload,
            allow_create=False,
        )


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
            raise WorkflowReplayConflict("Committed I4 gate is missing its artifact")
        artifact = Artifact(**expected)
        db.add(artifact)
        db.flush()
    else:
        _verify_artifact_fields(artifact, expected)
    return artifact


def _reconcile_job(
    db: Session,
    spec: Mapping[str, object],
    artifact_id: int,
    *,
    allow_create: bool,
) -> GenerationJob:
    job = db.scalar(
        select(GenerationJob).where(
            GenerationJob.campaign_id == spec["campaign_id"],
            GenerationJob.provider == spec["provider"],
            GenerationJob.model == spec["model"],
            GenerationJob.input_hash == spec["input_hash"],
            GenerationJob.attempt == 1,
        )
    )
    expected = {
        "attempt": 1,
        "campaign_id": spec["campaign_id"],
        "cost_microunits": 0,
        "error_json": None,
        "input_hash": spec["input_hash"],
        "model": spec["model"],
        "output_artifact_id": artifact_id,
        "provider": spec["provider"],
        "provider_job_id": f"i4:{spec['stage']}:{spec['input_hash']}",
        "scene_id": None,
        "status": "completed",
        "usage_json": canonical_json({"metered_cost_microunits": 0}),
    }
    if job is None:
        if not allow_create:
            raise WorkflowReplayConflict("Committed I4 gate is missing its generation job")
        job = GenerationJob(
            **expected,
            completed_at=datetime.utcnow(),
        )
        db.add(job)
        db.flush()
    else:
        actual = {key: getattr(job, key) for key in expected}
        if actual != expected:
            raise WorkflowReplayConflict(
                "Existing I4 generation job conflicts with immutable content"
            )
    return job


def _verify_gate(gate: GateDecision, spec: Mapping[str, object]) -> None:
    expected = {
        "campaign_id": spec["campaign_id"],
        "input_hash": spec["input_hash"],
        "outcome": spec["outcome"],
        "output_hash": spec["output_hash"],
        "policy_version": I4_POLICY_VERSION,
        "reasons_json": spec["reasons_json"],
        "stage": spec["stage"],
    }
    actual = {key: getattr(gate, key) for key in expected}
    if actual != expected:
        raise WorkflowReplayConflict(
            "Existing I4 gate decision conflicts with deterministic replay content"
        )


def _parse_retrieved_at(value: object) -> datetime:
    candidate = str(value).strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _source_expected_values(
    campaign_id: int,
    source: Mapping[str, object],
) -> dict[str, object]:
    try:
        computed_hash = source_record_content_sha256(source)
        provenance = dict(source["provenance"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowReplayConflict("I4 source identity is invalid") from exc
    if source.get("content_sha256") != computed_hash:
        raise WorkflowReplayConflict("I4 source content hash is invalid")
    if source.get("rights_status") != "reference_only":
        raise WorkflowReplayConflict("I4 source rights status is invalid")
    if provenance.get("source_key") != source.get("source_key"):
        raise WorkflowReplayConflict("I4 source provenance key is invalid")
    if provenance.get("origin") not in {
        "editorial_seed",
        "youtube_demand_snapshot",
    }:
        raise WorkflowReplayConflict("I4 source provenance origin is invalid")
    if source.get("evidence_snippet") != normalize_whitespace(
        str(source.get("evidence_snippet") or "")
    ):
        raise WorkflowReplayConflict("I4 source evidence snippet is not canonical")
    return {
        "campaign_id": campaign_id,
        "content_sha256": computed_hash,
        "evidence_snippet": source["evidence_snippet"],
        "provenance_json": canonical_json(source["provenance"]),
        "publisher": source["publisher"],
        "retrieved_at": _parse_retrieved_at(source["retrieved_at"]),
        "rights_status": source["rights_status"],
        "source_class": source["source_class"],
        "source_uri": source["source_uri"],
    }


def _claim_seed_from_record(claim: Mapping[str, object]) -> ClaimSeed:
    try:
        return ClaimSeed.model_validate(
            {
                "assertion_text": claim["assertion_text"],
                "assumptions": claim["assumptions"],
                "attribution": claim["attribution"],
                "claim_type": claim["claim_type"],
                "material": claim["material"],
                "role": claim["role"],
                "source_keys": claim["source_keys"],
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowReplayConflict("I4 claim semantic record is invalid") from exc


def _claim_expected_values(
    campaign_id: int,
    claim: Mapping[str, object],
) -> dict[str, object]:
    claim_hash = str(claim["claim_hash"])
    if re.fullmatch(r"[0-9a-f]{64}", claim_hash) is None:
        raise WorkflowReplayConflict("I4 claim hash is not a full SHA-256 value")
    if deterministic_claim_hash(_claim_seed_from_record(claim)) != claim_hash:
        raise WorkflowReplayConflict("I4 claim hash does not match semantic content")
    return {
        "assertion_text": claim["assertion_text"],
        "campaign_id": campaign_id,
        "claim_hash": claim_hash,
        "material": bool(claim["material"]),
        "state": claim["state"],
        "structured_value_json": claim_structured_value_json(claim),
        "unit": None,
    }


def _reconcile_research_entities(
    db: Session,
    campaign_id: int,
    packet: Mapping[str, object],
    *,
    allow_create: bool,
) -> tuple[dict[str, Source], dict[str, Claim]]:
    source_records = [dict(item) for item in packet.get("sources", [])]  # type: ignore[assignment]
    source_keys = [str(item.get("source_key") or "") for item in source_records]
    source_uris = [str(item.get("source_uri") or "") for item in source_records]
    if (
        not all(source_keys)
        or len(source_keys) != len(set(source_keys))
        or not all(source_uris)
        or len(source_uris) != len(set(source_uris))
    ):
        raise WorkflowReplayConflict("I4 research source identities are not unique")
    expected_source_uris = {str(item["source_uri"]) for item in source_records}
    existing_source_uris = set(
        db.scalars(select(Source.source_uri).where(Source.campaign_id == campaign_id))
    )
    if existing_source_uris - expected_source_uris:
        raise WorkflowReplayConflict("Campaign contains unexpected immutable research sources")

    sources_by_key: dict[str, Source] = {}
    source_inputs_by_key: dict[str, EvidenceSourceInput] = {}
    for record in source_records:
        expected = _source_expected_values(campaign_id, record)
        try:
            source_input = EvidenceSourceInput.model_validate(
                {
                    "evidence_snippet": record["evidence_snippet"],
                    "publisher": record["publisher"],
                    "source_class": record["source_class"],
                    "source_key": record["source_key"],
                    "source_uri": record["source_uri"],
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowReplayConflict("I4 research source record is invalid") from exc
        source = db.scalar(
            select(Source).where(
                Source.campaign_id == campaign_id,
                Source.source_uri == expected["source_uri"],
            )
        )
        if source is None:
            if not allow_create:
                raise WorkflowReplayConflict("Committed research gate is missing a source")
            source = Source(**expected)
            db.add(source)
            db.flush()
        else:
            actual = {key: getattr(source, key) for key in expected}
            if actual != expected:
                raise WorkflowReplayConflict(
                    "Existing research source conflicts with immutable evidence"
                )
        sources_by_key[str(record["source_key"])] = source
        source_inputs_by_key[source_input.source_key] = source_input

    claim_records = [
        dict(item)
        for item in (
            list(packet.get("claims", [])) + list(packet.get("rejected_claims", []))  # type: ignore[arg-type]
        )
    ]
    expected_claim_hashes = {str(item["claim_hash"]) for item in claim_records}
    existing_claim_hashes = set(
        db.scalars(
            select(Claim.claim_hash).where(
                Claim.campaign_id == campaign_id,
                Claim.claim_hash.is_not(None),
            )
        )
    )
    if existing_claim_hashes - expected_claim_hashes:
        raise WorkflowReplayConflict("Campaign contains unexpected immutable I4 claims")

    claims_by_hash: dict[str, Claim] = {}
    for record in claim_records:
        claim_seed = _claim_seed_from_record(record)
        expected_evaluation = evaluate_claim(
            claim_seed,
            source_inputs_by_key,
        ).model_dump(mode="json")
        actual_evaluation = {
            key: record.get(key)
            for key in (
                "assertion_text",
                "assumptions",
                "attribution",
                "claim_hash",
                "claim_type",
                "material",
                "reasons",
                "role",
                "source_keys",
                "state",
            )
        }
        if actual_evaluation != expected_evaluation:
            raise WorkflowReplayConflict(
                "I4 claim evaluation conflicts with source verification policy"
            )
        expected = _claim_expected_values(campaign_id, record)
        claim = db.scalar(
            select(Claim).where(
                Claim.campaign_id == campaign_id,
                Claim.claim_hash == expected["claim_hash"],
            )
        )
        if claim is None:
            if not allow_create:
                raise WorkflowReplayConflict("Committed research gate is missing a claim")
            claim = Claim(**expected)
            db.add(claim)
            db.flush()
        else:
            actual = {key: getattr(claim, key) for key in expected}
            if actual != expected:
                raise WorkflowReplayConflict(
                    "Existing claim conflicts with immutable semantic content"
                )
        claims_by_hash[str(record["claim_hash"])] = claim

        expected_source_ids = {
            sources_by_key[str(key)].id
            for key in record.get("source_keys", [])
            if str(key) in sources_by_key
        }
        if len(expected_source_ids) != len(set(record.get("source_keys", []))):
            raise WorkflowReplayConflict("Claim sourceability lineage is incomplete")
        actual_source_ids = set(
            db.scalars(
                select(ClaimSource.source_id).where(ClaimSource.claim_id == claim.id)
            )
        )
        if actual_source_ids - expected_source_ids:
            raise WorkflowReplayConflict("Claim contains conflicting source links")
        missing_source_ids = expected_source_ids - actual_source_ids
        if missing_source_ids and not allow_create:
            raise WorkflowReplayConflict("Committed research gate is missing claim-source links")
        for source_id in sorted(missing_source_ids):
            db.add(ClaimSource(claim_id=claim.id, source_id=source_id))
        if missing_source_ids:
            db.flush()
    return sources_by_key, claims_by_hash


def _persist_i4_stage_once(
    stage: str,
    packet: dict[str, object],
) -> dict[str, object]:
    spec = _stage_spec(stage, packet)
    with SessionLocal() as db:
        with _serialized_transaction(db):
            campaign = _campaign_for_update(db, int(spec["campaign_id"]))
            if campaign is None:
                raise CampaignNotFoundError(
                    f"Campaign {spec['campaign_id']} does not exist"
                )
            if campaign.policy_version != I4_POLICY_VERSION:
                raise WorkflowReplayConflict(
                    "Campaign policy changed during I4 stage persistence"
                )
            _verify_bound_stage_lineage(db, campaign, stage, packet)

            gate = db.scalar(
                select(GateDecision).where(
                    GateDecision.campaign_id == spec["campaign_id"],
                    GateDecision.stage == stage,
                    GateDecision.policy_version == I4_POLICY_VERSION,
                    GateDecision.input_hash == spec["input_hash"],
                )
            )
            replayed = gate is not None
            if gate is not None:
                _verify_gate(gate, spec)

            if stage == "research":
                _reconcile_research_entities(
                    db,
                    int(spec["campaign_id"]),
                    packet,
                    allow_create=not replayed,
                )

            artifact = _reconcile_artifact(
                db,
                cast(Mapping[str, object], spec["artifact"]),
                allow_create=not replayed,
            )
            job = _reconcile_job(
                db,
                spec,
                artifact.id,
                allow_create=not replayed,
            )

            next_stage = {"topic": "research", "research": "script", "script": "storyboard"}[stage]
            expected_current = next_stage if spec["outcome"] == "PASS" else stage
            if replayed:
                if campaign.current_stage != expected_current:
                    raise WorkflowReplayConflict(
                        "Committed I4 gate is inconsistent with campaign stage"
                    )
                assert gate is not None
                return {
                    "artifact_id": artifact.id,
                    "campaign_id": campaign.id,
                    "current_stage": campaign.current_stage,
                    "gate_decision_id": gate.id,
                    "generation_job_id": job.id,
                    "input_hash": spec["input_hash"],
                    "outcome": spec["outcome"],
                    "output_hash": spec["output_hash"],
                    "replayed": True,
                    "stage": stage,
                }

            if campaign.current_stage != stage:
                raise WorkflowTransitionError(
                    f"Campaign stage is {campaign.current_stage}; expected {stage}"
                )
            gate = GateDecision(
                campaign_id=campaign.id,
                stage=stage,
                outcome=spec["outcome"],
                policy_version=I4_POLICY_VERSION,
                input_hash=spec["input_hash"],
                output_hash=spec["output_hash"],
                reasons_json=spec["reasons_json"],
            )
            db.add(gate)
            db.flush()

            current_stage = stage
            if spec["outcome"] == "PASS":
                advanced = db.execute(
                    update(Campaign)
                    .where(
                        Campaign.id == campaign.id,
                        Campaign.current_stage == stage,
                    )
                    .values(current_stage=next_stage, updated_at=datetime.utcnow())
                )
                if advanced.rowcount != 1:
                    raise WorkflowTransitionError(
                        "I4 compare-and-set stage advancement failed"
                    )
                current_stage = next_stage
            return {
                "artifact_id": artifact.id,
                "campaign_id": campaign.id,
                "current_stage": current_stage,
                "gate_decision_id": gate.id,
                "generation_job_id": job.id,
                "input_hash": spec["input_hash"],
                "outcome": spec["outcome"],
                "output_hash": spec["output_hash"],
                "replayed": False,
                "stage": stage,
            }


def persist_i4_stage(stage: str, packet: dict[str, object]) -> dict[str, object]:
    try:
        return _persist_i4_stage_once(stage, packet)
    except IntegrityError:
        try:
            return _persist_i4_stage_once(stage, packet)
        except IntegrityError as exc:
            raise WorkflowReplayConflict(
                "I4 deterministic identity violated a uniqueness constraint"
            ) from exc


def persist_topic_stage(packet: dict[str, object]) -> dict[str, object]:
    return persist_i4_stage("topic", packet)


def persist_research_stage(packet: dict[str, object]) -> dict[str, object]:
    return persist_i4_stage("research", packet)


def persist_script_stage(packet: dict[str, object]) -> dict[str, object]:
    return persist_i4_stage("script", packet)


def _latest_artifact_payload(
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
    return artifact, _decode_canonical_artifact_payload(artifact)


def editorial_snapshot(campaign_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(f"Campaign {campaign_id} does not exist")
        seed, seed_payload = _latest_artifact_payload(db, campaign_id, I4_SEED_KIND)
        _, topic = _latest_artifact_payload(db, campaign_id, I4_TOPIC_KIND)
        _, research = _latest_artifact_payload(db, campaign_id, I4_RESEARCH_KIND)
        script, script_payload = _latest_artifact_payload(db, campaign_id, I4_SCRIPT_KIND)
        source_counts = Counter(
            db.scalars(
                select(Source.source_class).where(Source.campaign_id == campaign_id)
            )
        )
        claim_counts = Counter(
            db.scalars(select(Claim.state).where(Claim.campaign_id == campaign_id))
        )
        last_gate = db.scalar(
            select(GateDecision)
            .where(GateDecision.campaign_id == campaign_id)
            .order_by(GateDecision.id.desc())
            .limit(1)
        )
        selected_topic: str | None = None
        if topic is not None:
            selected_key = topic.get("selected_candidate_key")
            for ranked in topic.get("ranked_candidates", []):
                if ranked.get("candidate_key") == selected_key:
                    selected_topic = str(ranked.get("topic") or "") or None
                    break
        return {
            "active_seed_hash": seed.sha256 if seed is not None else None,
            "campaign_id": campaign.id,
            "claim_state_counts": dict(sorted(claim_counts.items())),
            "current_stage": campaign.current_stage,
            "last_gate_outcome": last_gate.outcome if last_gate is not None else None,
            "script_hash": script.sha256 if script is not None else None,
            "script_runtime_estimate_minutes": (
                script_payload.get("estimated_runtime_minutes")
                if script_payload is not None
                else None
            ),
            "selected_topic": selected_topic,
            "source_counts": dict(sorted(source_counts.items())),
            "topic_score": topic.get("commercial_score") if topic is not None else None,
            "topic_score_breakdown": (
                topic.get("commercial_score_breakdown") if topic is not None else None
            ),
            "viewer_promise": topic.get("viewer_promise") if topic is not None else None,
            "workflow_id": campaign.workflow_id,
            "seed_present": seed_payload is not None,
        }
