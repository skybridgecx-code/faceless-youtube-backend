from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping, Sequence, cast

from sqlalchemy import select

from app.config import Settings
from app.db import Artifact, GenerationJob, Scene, SessionLocal
from app.editorial.contracts import canonical_json, canonical_sha256
from app.workflows.persistence import WorkflowReplayConflict, WorkflowTransitionError

from .persistence import (
    I5_MEDIA_PLAN_KIND,
    I5_STORYBOARD_KIND,
    binary_artifact_values,
    claim_metered_dispatch,
    campaign_cost,
    complete_metered_job,
    fail_metered_job,
    latest_json_artifact,
    persist_zero_cost_binary,
    persist_submitted_provider_job,
    reconcile_completed_metered_job,
    reconcile_failed_metered_job,
    reconcile_storyboard_effect_set,
    reserve_tts_plan,
    reserve_visual_job,
    authorized_campaign_cap,
)
from .budget import load_campaign_budget_policy
from .providers import is_valid_sora_provider_job_id


I5_TTS_SAFE_CHARACTER_LIMIT = 3_800
I5_OBJECT_URI_PATTERN = re.compile(
    r"^i5-object://campaign/(?P<campaign_id>[1-9][0-9]*)/objects/"
    r"(?P<sha256>[0-9a-f]{64})\.(?P<extension>[a-z0-9]+)$"
)
_SAFE_EXTENSIONS = {"aac", "jpg", "mp4", "png", "wav"}
_VALIDATED_WAV_BY_SHA256: dict[str, object] = {}
_VALIDATED_PNG_BY_SHA256: dict[str, tuple[int, int]] = {}
_VALIDATED_VIDEO_BY_SHA256: dict[str, object] = {}
_MAX_VALIDATION_CACHE_ENTRIES = 4_096


def _bounded_validation_cache(cache: dict[str, object]) -> dict[str, object]:
    if len(cache) >= _MAX_VALIDATION_CACHE_ENTRIES:
        cache.clear()
    return cache


def canonical_i5_path(output_dir: Path, *components: str) -> Path:
    """Resolve an I5 runtime path while refusing symlink/path traversal escapes."""

    output_root = output_dir.resolve()
    for component in components:
        if (
            not component
            or component in {".", ".."}
            or Path(component).name != component
        ):
            raise WorkflowReplayConflict("Canonical I5 path component is invalid")
    candidate = output_root.joinpath("canonical_i5", *components).resolve()
    try:
        candidate.relative_to(output_root)
    except ValueError as exc:
        raise WorkflowReplayConflict(
            "Canonical I5 path escapes the configured output root"
        ) from exc
    return candidate


@dataclass(frozen=True)
class StoredObject:
    uri: str
    path: Path
    sha256: str
    byte_size: int
    extension: str


class CanonicalMediaStore:
    def __init__(self, output_dir: Path, campaign_id: int) -> None:
        if campaign_id <= 0:
            raise ValueError("campaign_id must be positive")
        self.output_root = output_dir.resolve()
        self.campaign_id = campaign_id
        self.object_root = canonical_i5_path(
            self.output_root,
            f"campaign_{campaign_id}",
            "objects",
        )

    def write(self, data: bytes, *, extension: str) -> StoredObject:
        if not data:
            raise ValueError("canonical media object cannot be empty")
        normalized_extension = extension.strip().lower().lstrip(".")
        if normalized_extension not in _SAFE_EXTENSIONS:
            raise ValueError("canonical media extension is not allowed")
        digest = hashlib.sha256(data).hexdigest()
        self.object_root.mkdir(parents=True, exist_ok=True)
        destination = (self.object_root / f"{digest}.{normalized_extension}").resolve()
        self._require_within_root(destination)
        if destination.exists():
            if not destination.is_file() or _sha256_path(destination) != digest:
                raise WorkflowReplayConflict(
                    "Existing canonical media object conflicts with its content address"
                )
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=self.object_root,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                if _sha256_path(temporary) != digest:
                    raise WorkflowReplayConflict(
                        "Temporary canonical media object failed hash validation"
                    )
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return StoredObject(
            uri=(
                f"i5-object://campaign/{self.campaign_id}/objects/"
                f"{digest}.{normalized_extension}"
            ),
            path=destination,
            sha256=digest,
            byte_size=len(data),
            extension=normalized_extension,
        )

    def resolve(self, uri: str, *, expected_sha256: str | None = None) -> Path:
        match = I5_OBJECT_URI_PATTERN.fullmatch(uri.strip())
        if match is None or int(match.group("campaign_id")) != self.campaign_id:
            raise WorkflowReplayConflict("Canonical media URI is invalid")
        if match.group("extension") not in _SAFE_EXTENSIONS:
            raise WorkflowReplayConflict("Canonical media URI extension is invalid")
        digest = match.group("sha256")
        if expected_sha256 is not None and digest != expected_sha256:
            raise WorkflowReplayConflict("Canonical media URI hash conflicts")
        path = (self.object_root / f"{digest}.{match.group('extension')}").resolve()
        self._require_within_root(path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise WorkflowReplayConflict("Canonical media object is missing")
        if _sha256_path(path) != digest:
            raise WorkflowReplayConflict("Canonical media object hash is invalid")
        return path

    def _require_within_root(self, path: Path) -> None:
        try:
            path.relative_to(self.object_root)
            path.relative_to(self.output_root)
        except ValueError as exc:
            raise WorkflowReplayConflict(
                "Canonical media path escapes the campaign object root"
            ) from exc


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scene_contract(scene: Scene) -> dict[str, object]:
    try:
        payload = json.loads(scene.overlay_spec_json)
    except json.JSONDecodeError as exc:
        raise WorkflowReplayConflict("Persisted Scene contract is invalid JSON") from exc
    if not isinstance(payload, dict) or canonical_json(payload) != scene.overlay_spec_json:
        raise WorkflowReplayConflict("Persisted Scene contract is not canonical")
    expected = {
        "disclosure_state": payload.get("disclosure_state"),
        "narration_reference": payload.get("narration_sha256"),
        "position": payload.get("position"),
        "visual_mode": payload.get("visual_mode"),
    }
    actual = {
        "disclosure_state": scene.disclosure_state,
        "narration_reference": scene.narration_reference,
        "position": scene.position,
        "visual_mode": scene.visual_mode,
    }
    if expected != actual:
        raise WorkflowReplayConflict("Persisted Scene columns conflict with contract")
    narration = str(payload.get("narration") or "")
    if hashlib.sha256(narration.encode("utf-8")).hexdigest() != payload.get(
        "narration_sha256"
    ):
        raise WorkflowReplayConflict("Persisted Scene narration hash is invalid")
    return payload


def build_tts_plan_requests(campaign_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        context = reconcile_storyboard_effect_set(db, campaign_id)
        storyboard_hash = str(context["storyboard_hash"])
        profile_hash = str(context["profile_hash"])
        requests: list[dict[str, object]] = []
        for row in cast(Sequence[Scene], context["scenes"]):
            contract = scene_contract(row)
            narration = str(contract["narration"])
            if len(narration) > I5_TTS_SAFE_CHARACTER_LIMIT:
                raise WorkflowTransitionError(
                    "Accepted storyboard scene exceeds the safe TTS input limit"
                )
            input_hash = canonical_sha256(
                {
                    "campaign_id": campaign_id,
                    "contract_version": "i5-scene-tts-request-v1",
                    "narration": narration,
                    "narration_sha256": contract["narration_sha256"],
                    "production_profile_hash": profile_hash,
                    "scene_position": row.position,
                    "storyboard_hash": storyboard_hash,
                    "voice": "onyx",
                }
            )
            requests.append(
                {
                    "character_count": len(narration),
                    "input_hash": input_hash,
                    "narration_sha256": contract["narration_sha256"],
                    "scene_id": row.id,
                    "scene_position": row.position,
                }
            )
    reservation = reserve_tts_plan(
        campaign_id=campaign_id,
        storyboard_hash=storyboard_hash,
        requests=requests,
    )
    return {"requests": requests, "storyboard_hash": storyboard_hash, **reservation}


def load_tts_job_input(job_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        if job is None or job.provider != "openai_tts" or job.scene_id is None:
            raise WorkflowReplayConflict("TTS job identity is invalid")
        context = reconcile_storyboard_effect_set(db, job.campaign_id)
        scene = db.get(Scene, job.scene_id)
        if scene is None or scene.campaign_id != job.campaign_id:
            raise WorkflowReplayConflict("TTS job scene is missing")
        contract = scene_contract(scene)
        expected_input_hash = canonical_sha256(
            {
                "campaign_id": job.campaign_id,
                "contract_version": "i5-scene-tts-request-v1",
                "narration": contract["narration"],
                "narration_sha256": contract["narration_sha256"],
                "production_profile_hash": context["profile_hash"],
                "scene_position": scene.position,
                "storyboard_hash": context["storyboard_hash"],
                "voice": "onyx",
            }
        )
        if job.input_hash != expected_input_hash:
            raise WorkflowReplayConflict("TTS job input lineage is invalid")
        return {
            "campaign_id": job.campaign_id,
            "input_hash": job.input_hash,
            "model": job.model,
            "narration": contract["narration"],
            "narration_sha256": contract["narration_sha256"],
            "profile_hash": context["profile_hash"],
            "scene_id": scene.id,
            "scene_position": scene.position,
            "storyboard_hash": context["storyboard_hash"],
            "voice": "onyx",
        }


def persist_tts_bytes(
    *,
    job_id: int,
    audio_bytes: bytes,
    duration_seconds: float,
    settings: Settings,
) -> dict[str, object]:
    request = load_tts_job_input(job_id)
    if duration_seconds <= 0:
        raise WorkflowReplayConflict("TTS audio duration must be positive")
    store = CanonicalMediaStore(
        settings.output_path,
        int(request["campaign_id"]),
    )
    stored = store.write(audio_bytes, extension="wav")
    model = str(request["model"])
    price = 30 if model == "tts-1-hd" else 15
    character_count = len(str(request["narration"]))
    final_cost = character_count * price
    values = binary_artifact_values(
        campaign_id=int(request["campaign_id"]),
        kind=f"i5_scene_narration_{int(request['scene_position']):03d}",
        source_stage="media",
        uri=stored.uri,
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        mime_type="audio/wav",
        provider_name="openai_tts",
        provider_model=model,
        input_hash=str(request["input_hash"]),
        provenance={
            "campaign_id": request["campaign_id"],
            "duration_seconds": round(duration_seconds, 6),
            "media_validation": {
                "audio_stream": True,
                "format": "wav",
                "valid": True,
            },
            "narration_sha256": request["narration_sha256"],
            "production_profile_hash": request["profile_hash"],
            "rights_basis": "original_i4_narration",
            "scene_position": request["scene_position"],
            "storyboard_hash": request["storyboard_hash"],
        },
    )
    return complete_metered_job(
        job_id=job_id,
        artifact_values=values,
        final_cost_microunits=final_cost,
        usage={
            "character_count": character_count,
            "duration_seconds": round(duration_seconds, 6),
            "price_microusd_per_character": price,
        },
    )


def produce_tts_job(
    *,
    job_id: int,
    api_key: str,
    settings: Settings,
) -> dict[str, object]:
    """Execute one financially protected TTS POST exactly once.

    Provider imports stay local so the state-free provider module has no ORM
    dependency and DBOS recovery can reconcile a committed result first.
    """

    dispatch = claim_metered_dispatch(job_id)
    if not dispatch["dispatch"]:
        return dispatch
    request_data = load_tts_job_input(job_id)
    from .providers import OpenAITTSProvider, TTSRequest

    try:
        result = OpenAITTSProvider().generate(
            TTSRequest(
                model=str(request_data["model"]),
                voice="onyx",
                response_format="wav",
                text=str(request_data["narration"]),
            ),
            api_key=api_key,
        )
    except Exception:
        return fail_metered_job(
            job_id,
            code="tts_provider_outcome_unknown",
            ambiguous=True,
        )

    try:
        from .renderer import validate_wav

        temporary_root = canonical_i5_path(settings.output_path, ".validation")
        temporary_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="tts-",
            suffix=".wav",
            dir=temporary_root,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(result.audio_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            duration = validate_wav(temporary).duration_seconds
        finally:
            temporary.unlink(missing_ok=True)
        return persist_tts_bytes(
            job_id=job_id,
            audio_bytes=result.audio_bytes,
            duration_seconds=duration,
            settings=settings,
        )
    except Exception:
        # The provider response may already be billable.  A failure before the
        # result is durably linked is financially ambiguous and must never
        # permit an automatic second POST.
        return fail_metered_job(
            job_id,
            code="tts_result_not_durably_persisted",
            ambiguous=True,
        )


def visual_request_options(
    profile_payload: Mapping[str, object],
    *,
    allow_video: bool,
) -> tuple[dict[str, object], ...]:
    video = dict(profile_payload["video"])  # type: ignore[arg-type]
    options: list[dict[str, object]] = []
    if allow_video and video.get("provider") != "disabled":
        options.append(
            {
                "name": "sora-2",
                "provider": "openai_video",
                "reservation_microusd": 960_000,
            }
        )
    options.extend(
        (
            {
                "name": "gpt-image-2-medium",
                "provider": "openai_image",
                "reservation_microusd": 100_000,
            },
            {
                "name": "gpt-image-2-low",
                "provider": "openai_image",
                "reservation_microusd": 25_000,
            },
            {
                "name": "deterministic_local",
                "provider": "local",
                "reservation_microusd": 0,
            },
        )
    )
    return tuple(options)


def visual_input_hash(
    *,
    campaign_id: int,
    scene: Mapping[str, object],
    profile_hash: str,
    storyboard_hash: str,
) -> str:
    return canonical_sha256(
        {
            "campaign_id": campaign_id,
            "contract_version": "i5-scene-visual-request-v1",
            "factual_overlay": scene.get("factual_overlay"),
            "fallback_fulfillments": scene.get("fallback_fulfillments"),
            "primary_fulfillment": scene.get("primary_fulfillment"),
            "production_profile_hash": profile_hash,
            "scene_position": scene.get("position"),
            "storyboard_hash": storyboard_hash,
            "visual_mode": scene.get("visual_mode"),
            "visual_purpose": scene.get("visual_purpose"),
        }
    )


def reserve_generated_visual(campaign_id: int, scene_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        context = reconcile_storyboard_effect_set(db, campaign_id)
        scene_row = db.get(Scene, scene_id)
        if scene_row is None or scene_row.campaign_id != campaign_id:
            raise WorkflowReplayConflict("Visual scene identity is invalid")
        contract = scene_contract(scene_row)
        input_hash = visual_input_hash(
            campaign_id=campaign_id,
            scene=contract,
            profile_hash=str(context["profile_hash"]),
            storyboard_hash=str(context["storyboard_hash"]),
        )
        primary = dict(contract.get("primary_fulfillment") or {})
        options = visual_request_options(
            cast(Mapping[str, object], context["profile_payload"]),
            allow_video=primary.get("strategy") == "generated_video_primary",
        )
    return reserve_visual_job(
        campaign_id=campaign_id,
        scene_id=scene_id,
        input_hash=input_hash,
        options=options,
    )


def _verified_data_points(overlay_text: str):  # type: ignore[no-untyped-def]
    from .local_visuals import DataPoint

    match = re.search(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", overlay_text)
    if match is None:
        return ()
    display = match.group(0)
    try:
        value = float(display.replace(",", ""))
    except ValueError:
        return ()
    return (DataPoint(label="Verified value", value=value, display_value=display),)


def _canonical_local_visual_request(
    contract: Mapping[str, object],
    *,
    fallback: bool,
):  # type: ignore[no-untyped-def]
    from .local_visuals import LocalVisualRequest

    mode = str(contract["visual_mode"])
    if mode == "GENERATED_CINEMATIC" or fallback:
        mode = "DETERMINISTIC_MOTION_GRAPHIC"
    overlay = dict(cast(Mapping[str, object], contract.get("factual_overlay") or {}))
    overlay_text = str(overlay.get("text") or "")
    data_points = (
        _verified_data_points(overlay_text) if mode == "DATA_VISUALIZATION" else ()
    )
    rights = dict(cast(Mapping[str, object], contract.get("rights_basis") or {}))
    rights_basis = (
        "reference_only"
        if rights.get("kind") == "reference_only_evidence_card"
        else "original_deterministic"
    )
    disclosure_state = str(contract.get("disclosure_state") or "not_required")
    if fallback and disclosure_state == "illustrative_generated_media":
        disclosure_state = "not_required"
    return LocalVisualRequest(
        mode=mode,
        title=f"{contract['section_id']} · beat {int(contract['beat_index']) + 1}",
        scene_position=int(contract["position"]),
        visual_purpose=str(contract["visual_purpose"]),
        factual_overlay=overlay_text,
        claim_hashes=tuple(str(value) for value in contract.get("claim_hashes", [])),
        source_keys=tuple(str(value) for value in contract.get("source_keys", [])),
        detail_lines=(str(contract.get("payoff") or ""),),
        data_points=data_points,
        rights_basis=rights_basis,
        disclosure_state=disclosure_state,
    )


def render_and_persist_local_visual(
    *,
    campaign_id: int,
    scene_id: int,
    settings: Settings,
    fallback: bool = False,
) -> dict[str, object]:
    with SessionLocal() as db:
        context = reconcile_storyboard_effect_set(db, campaign_id)
        row = db.get(Scene, scene_id)
        if row is None or row.campaign_id != campaign_id:
            raise WorkflowReplayConflict("Local visual scene lineage is invalid")
        contract = scene_contract(row)
        storyboard_hash = str(context["storyboard_hash"])
        profile_hash = str(context["profile_hash"])

    from .local_visuals import render_local_visual

    request = _canonical_local_visual_request(contract, fallback=fallback)
    result = render_local_visual(request)
    store = CanonicalMediaStore(settings.output_path, campaign_id)
    stored = store.write(result.png_bytes, extension="png")
    input_hash = canonical_sha256(
        {
            "contract_version": "i5-local-visual-input-v1",
            "fallback": fallback,
            "local_request_hash": result.request_sha256,
            "production_profile_hash": profile_hash,
            "storyboard_hash": storyboard_hash,
        }
    )
    values = binary_artifact_values(
        campaign_id=campaign_id,
        kind=f"i5_scene_visual_{int(contract['position']):03d}",
        source_stage="media",
        uri=stored.uri,
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        mime_type="image/png",
        provider_name="deterministic_local",
        provider_model=result.renderer_version,
        input_hash=input_hash,
        provenance={
            "campaign_id": campaign_id,
            "claim_hashes": list(contract.get("claim_hashes", [])),
            "disclosure_state": request.disclosure_state,
            "fallback": fallback,
            "media_validation": {
                "height": result.height,
                "valid": True,
                "width": result.width,
            },
            "production_profile_hash": profile_hash,
            "rights_basis": request.rights_basis,
            "scene_position": contract["position"],
            "source_keys": list(contract.get("source_keys", [])),
            "storyboard_hash": storyboard_hash,
            "visual_mode": str(request.mode),
        },
    )
    persisted = persist_zero_cost_binary(
        campaign_id=campaign_id,
        scene_id=scene_id,
        artifact_values=values,
        provider="deterministic_local",
        model=result.renderer_version,
        input_hash=input_hash,
    )
    return {
        **persisted,
        "fallback": fallback,
        "provider": "deterministic_local",
        "visual_mode": str(request.mode),
    }


def produce_scene_visual(
    *,
    campaign_id: int,
    scene_id: int,
    api_key: str,
    settings: Settings,
) -> dict[str, object]:
    with SessionLocal() as db:
        context = reconcile_storyboard_effect_set(db, campaign_id)
        row = db.get(Scene, scene_id)
        if row is None or row.campaign_id != campaign_id:
            raise WorkflowReplayConflict("Visual scene lineage is invalid")
        contract = scene_contract(row)
        if contract["visual_mode"] != "GENERATED_CINEMATIC":
            return render_and_persist_local_visual(
                campaign_id=campaign_id,
                scene_id=scene_id,
                settings=settings,
            )
        profile_hash = str(context["profile_hash"])
        storyboard_hash = str(context["storyboard_hash"])
        primary = dict(contract.get("primary_fulfillment") or {})
        if primary.get("strategy") == "generated_video_primary":
            narration_artifact = _single_binary_artifact(
                db,
                campaign_id=campaign_id,
                kind=f"i5_scene_narration_{int(contract['position']):03d}",
            )
            narration_provenance = _canonical_provenance(narration_artifact)
            if float(narration_provenance.get("duration_seconds") or 0) > 8.0:
                return render_and_persist_local_visual(
                    campaign_id=campaign_id,
                    scene_id=scene_id,
                    settings=settings,
                    fallback=True,
                )

    reservation = reserve_generated_visual(campaign_id, scene_id)
    job_id = reservation.get("job_id")
    if job_id is None or reservation.get("provider") == "local":
        return render_and_persist_local_visual(
            campaign_id=campaign_id,
            scene_id=scene_id,
            settings=settings,
            fallback=True,
        )
    if reservation.get("provider") == "openai_video":
        return {
            **reservation,
            "requires_sora": True,
        }

    dispatch = claim_metered_dispatch(int(job_id))
    if not dispatch["dispatch"]:
        if dispatch["status"] in {"provider_complete", "completed"}:
            return dispatch
        return render_and_persist_local_visual(
            campaign_id=campaign_id,
            scene_id=scene_id,
            settings=settings,
            fallback=True,
        )

    selected = str(reservation["selected"])
    quality = "low" if selected.endswith("-low") else "medium"
    from .providers import GPTImageProvider, ImageRequest

    try:
        result = GPTImageProvider().generate(
            ImageRequest(
                concept=str(contract["visual_purpose"]),
                quality=quality,  # type: ignore[arg-type]
            ),
            api_key=api_key,
        )
    except Exception:
        fail_metered_job(
            int(job_id),
            code="image_provider_outcome_unknown",
            ambiguous=True,
        )
        return render_and_persist_local_visual(
            campaign_id=campaign_id,
            scene_id=scene_id,
            settings=settings,
            fallback=True,
        )

    try:
        store = CanonicalMediaStore(settings.output_path, campaign_id)
        stored = store.write(result.image_bytes, extension="png")
        input_hash = visual_input_hash(
            campaign_id=campaign_id,
            scene=contract,
            profile_hash=profile_hash,
            storyboard_hash=storyboard_hash,
        )
        values = binary_artifact_values(
            campaign_id=campaign_id,
            kind=f"i5_scene_visual_{int(contract['position']):03d}",
            source_stage="media",
            uri=stored.uri,
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            mime_type="image/png",
            provider_name="openai_image",
            provider_model=selected,
            input_hash=input_hash,
            provenance={
                "campaign_id": campaign_id,
                "claim_hashes": [],
                "disclosure_state": "illustrative_generated_media",
                "generated_media_is_evidence": False,
                "media_validation": {
                    "height": 720,
                    "valid": True,
                    "width": 1280,
                },
                "production_profile_hash": profile_hash,
                "provider_metadata": result.metadata(),
                "rights_basis": "generated_illustrative",
                "scene_position": contract["position"],
                "storyboard_hash": storyboard_hash,
            },
        )
        return complete_metered_job(
            job_id=int(job_id),
            artifact_values=values,
            final_cost_microunits=result.reservation_microunits,
            usage=result.metadata(),
        )
    except Exception:
        fail_metered_job(
            int(job_id),
            code="image_result_not_durably_persisted",
            ambiguous=True,
        )
        return render_and_persist_local_visual(
            campaign_id=campaign_id,
            scene_id=scene_id,
            settings=settings,
            fallback=True,
        )


def _load_sora_job_input(job_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        if (
            job is None
            or job.provider != "openai_video"
            or job.model != "sora-2"
            or job.scene_id is None
        ):
            raise WorkflowReplayConflict("Sora job identity is invalid")
        context = reconcile_storyboard_effect_set(db, job.campaign_id)
        scene = db.get(Scene, job.scene_id)
        if scene is None or scene.campaign_id != job.campaign_id:
            raise WorkflowReplayConflict("Sora job scene lineage is invalid")
        contract = scene_contract(scene)
        narration_artifact = _single_binary_artifact(
            db,
            campaign_id=job.campaign_id,
            kind=f"i5_scene_narration_{scene.position:03d}",
        )
        narration_provenance = _canonical_provenance(narration_artifact)
        narration_duration = float(
            narration_provenance.get("duration_seconds") or 0
        )
        if narration_duration <= 0:
            raise WorkflowReplayConflict("Sora narration duration is invalid")
        primary = dict(contract.get("primary_fulfillment") or {})
        expected_input_hash = visual_input_hash(
            campaign_id=job.campaign_id,
            scene=contract,
            profile_hash=str(context["profile_hash"]),
            storyboard_hash=str(context["storyboard_hash"]),
        )
        video_profile = dict(
            cast(Mapping[str, object], context["profile_payload"])["video"]  # type: ignore[index]
        )
        if (
            primary.get("strategy") != "generated_video_primary"
            or primary.get("generated_video_seconds") != 8
            or job.input_hash != expected_input_hash
            or video_profile.get("provider") == "disabled"
            or video_profile.get("model") != "sora-2"
            or video_profile.get("allow_deprecated_sora") is not True
        ):
            raise WorkflowReplayConflict("Sora job configuration lineage is invalid")
        return {
            "campaign_id": job.campaign_id,
            "concept": contract["visual_purpose"],
            "contract": contract,
            "input_hash": expected_input_hash,
            "narration_duration_seconds": narration_duration,
            "profile_hash": context["profile_hash"],
            "scene_id": scene.id,
            "scene_position": scene.position,
            "storyboard_hash": context["storyboard_hash"],
            "video_profile": video_profile,
        }


def _sora_provider(request: Mapping[str, object]):  # type: ignore[no-untyped-def]
    from .providers import SoraConfiguration, SoraProvider

    video = dict(request["video_profile"])  # type: ignore[arg-type]
    return SoraProvider(
        SoraConfiguration(
            provider=str(video["provider"]),  # type: ignore[arg-type]
            model=str(video["model"]),  # type: ignore[arg-type]
            allow_deprecated_sora=bool(video["allow_deprecated_sora"]),
        )
    )


def create_sora_job(*, job_id: int, api_key: str) -> dict[str, object]:
    """Send the metered Sora create POST once and durably bind its provider ID."""

    dispatch = claim_metered_dispatch(job_id)
    if not dispatch["dispatch"]:
        return dispatch
    request_data = _load_sora_job_input(job_id)
    from .providers import SoraCreateRequest

    try:
        provider_job = _sora_provider(request_data).create(
            SoraCreateRequest(concept=str(request_data["concept"])),
            api_key=api_key,
        )
    except Exception:
        # A transport failure can occur after the provider accepted the POST but
        # before it returned an identifier.  That state is financially ambiguous
        # and must never be eligible for an automatic create retry.
        return fail_metered_job(
            job_id,
            code="sora_create_outcome_unknown",
            ambiguous=True,
        )
    metadata = {
        "duration_seconds": provider_job.duration_seconds,
        "model": provider_job.model,
        "provider_status": provider_job.status,
        "reservation_microusd": provider_job.reservation_microunits,
        "size": provider_job.size,
    }
    try:
        return persist_submitted_provider_job(
            job_id=job_id,
            provider_job_id=provider_job.provider_job_id,
            metadata=metadata,
        )
    except Exception:
        return fail_metered_job(
            job_id,
            code="sora_submission_not_durably_persisted",
            ambiguous=True,
        )


def poll_sora_job(*, job_id: int, api_key: str) -> dict[str, object]:
    state = claim_metered_dispatch(job_id)
    if state.get("status") != "submitted":
        return state
    request_data = _load_sora_job_input(job_id)
    provider_job = _sora_provider(request_data).poll_once(
        str(state["provider_job_id"]),
        api_key=api_key,
    )
    return {
        "job_id": job_id,
        "provider_job_id": provider_job.provider_job_id,
        "status": provider_job.status,
        "terminal": provider_job.terminal,
    }


def download_and_persist_sora_job(
    *,
    job_id: int,
    api_key: str,
    settings: Settings,
) -> dict[str, object]:
    state = claim_metered_dispatch(job_id)
    if state.get("status") != "submitted":
        return state
    request_data = _load_sora_job_input(job_id)
    provider_job_id = str(state["provider_job_id"])
    try:
        downloaded = _sora_provider(request_data).download(
            provider_job_id,
            api_key=api_key,
        )
        temporary_root = canonical_i5_path(settings.output_path, ".validation")
        temporary_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="sora-",
            suffix=".mp4",
            dir=temporary_root,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(downloaded.video_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            from .renderer import probe_media

            probe = probe_media(temporary)
            if (
                not probe.has_video
                or (probe.width, probe.height) != (1280, 720)
                or not 7.5 <= probe.duration_seconds <= 8.5
                or probe.duration_seconds + 0.02
                < float(request_data["narration_duration_seconds"])
            ):
                raise WorkflowReplayConflict("Sora video validation failed")
        finally:
            temporary.unlink(missing_ok=True)
        store = CanonicalMediaStore(
            settings.output_path,
            int(request_data["campaign_id"]),
        )
        stored = store.write(downloaded.video_bytes, extension="mp4")
        portable_probe = {
            key: value for key, value in probe.metadata().items() if key != "path"
        }
        contract = cast(Mapping[str, object], request_data["contract"])
        values = binary_artifact_values(
            campaign_id=int(request_data["campaign_id"]),
            kind=f"i5_scene_visual_{int(request_data['scene_position']):03d}",
            source_stage="media",
            uri=stored.uri,
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            mime_type="video/mp4",
            provider_name="openai_video",
            provider_model="sora-2",
            input_hash=str(request_data["input_hash"]),
            provenance={
                "campaign_id": request_data["campaign_id"],
                "claim_hashes": [],
                "disclosure_state": "illustrative_generated_media",
                "generated_media_is_evidence": False,
                "media_validation": portable_probe,
                "production_profile_hash": request_data["profile_hash"],
                "provider_job_id": provider_job_id,
                "rights_basis": "generated_illustrative",
                "scene_position": request_data["scene_position"],
                "storyboard_hash": request_data["storyboard_hash"],
                "visual_purpose": contract["visual_purpose"],
            },
        )
        return complete_metered_job(
            job_id=job_id,
            artifact_values=values,
            final_cost_microunits=downloaded.cost_microunits,
            usage={
                "duration_seconds": 8,
                "provider_job_id": provider_job_id,
                "size": "1280x720",
            },
            provider_job_id=provider_job_id,
        )
    except Exception:
        return fail_metered_job(job_id, code="sora_download_or_validation_failed")


def media_plan(campaign_id: int) -> tuple[Artifact, dict[str, object]]:
    with SessionLocal() as db:
        artifact, payload = latest_json_artifact(db, campaign_id, I5_MEDIA_PLAN_KIND)
        if artifact is None or payload is None:
            raise WorkflowTransitionError("I5 media plan has not been reserved")
        return artifact, payload


def storyboard_artifact(campaign_id: int) -> tuple[Artifact, dict[str, object]]:
    with SessionLocal() as db:
        artifact, payload = latest_json_artifact(db, campaign_id, I5_STORYBOARD_KIND)
        if artifact is None or payload is None:
            raise WorkflowTransitionError("I5 storyboard has not been accepted")
        return artifact, payload


def _canonical_provenance(artifact: Artifact) -> dict[str, object]:
    try:
        payload = json.loads(artifact.provenance_json)
    except json.JSONDecodeError as exc:
        raise WorkflowReplayConflict("Binary artifact provenance is invalid") from exc
    if not isinstance(payload, dict) or canonical_json(payload) != artifact.provenance_json:
        raise WorkflowReplayConflict("Binary artifact provenance is not canonical")
    return payload


def _single_binary_artifact(
    db,  # type: ignore[no-untyped-def]
    *,
    campaign_id: int,
    kind: str,
) -> Artifact:
    rows = list(
        db.scalars(
            select(Artifact).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind == kind,
            )
        )
    )
    if len(rows) != 1:
        raise WorkflowReplayConflict(f"Expected exactly one {kind} artifact")
    artifact = rows[0]
    if artifact.payload_json is not None or artifact.byte_size <= 0:
        raise WorkflowReplayConflict(f"{kind} binary artifact fields are invalid")
    return artifact


def _verify_artifact_fields(
    artifact: Artifact,
    expected: Mapping[str, object],
    *,
    label: str,
) -> None:
    if {key: getattr(artifact, key) for key in expected} != dict(expected):
        raise WorkflowReplayConflict(f"{label} immutable metadata conflicts")


def _validate_zero_cost_media_job(
    job: GenerationJob,
    *,
    artifact: Artifact,
    campaign_id: int,
    scene_id: int | None,
) -> None:
    if (
        job.campaign_id != campaign_id
        or job.scene_id != scene_id
        or job.attempt != 1
        or job.provider != artifact.provider_name
        or job.model != artifact.provider_model
        or job.status != "completed"
        or job.input_hash
        != _canonical_provenance(artifact).get("generation_input_hash")
        or job.output_artifact_id != artifact.id
        or job.provider_job_id != f"i5:local:{job.input_hash}"
        or job.usage_json != canonical_json({"metered_cost_microunits": 0})
        or job.cost_microunits != 0
        or job.reserved_cost_microunits != 0
        or job.error_json is not None
        or job.completed_at is None
    ):
        raise WorkflowReplayConflict("Zero-cost media job lineage is invalid")


def _validate_png_path(path: Path) -> tuple[int, int]:
    from .providers import ProviderError, validate_png_bytes

    try:
        return validate_png_bytes(path.read_bytes())
    except (OSError, ProviderError):
        raise WorkflowReplayConflict("Canonical visual PNG is invalid") from None


def _validate_wav_path(path: Path):  # type: ignore[no-untyped-def]
    from .renderer import RendererError, validate_wav

    try:
        return validate_wav(path)
    except RendererError:
        raise WorkflowReplayConflict("Canonical narration WAV is invalid") from None


def _validate_video_path(path: Path):  # type: ignore[no-untyped-def]
    from .renderer import RendererError, probe_media

    try:
        probe = probe_media(path)
    except RendererError:
        raise WorkflowReplayConflict("Canonical generated video is invalid") from None
    if (
        not probe.has_video
        or (probe.width, probe.height) != (1280, 720)
        or not 7.5 <= probe.duration_seconds <= 8.5
    ):
        raise WorkflowReplayConflict("Canonical generated video violates I5 bounds")
    return probe


def _portable_probe(probe) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {key: value for key, value in probe.metadata().items() if key != "path"}


def create_full_voiceover(campaign_id: int, settings: Settings) -> dict[str, object]:
    from .renderer import concat_wav_files

    with SessionLocal() as db:
        context = reconcile_storyboard_effect_set(db, campaign_id)
        scene_rows = cast(Sequence[Scene], context["scenes"])
        store = CanonicalMediaStore(settings.output_path, campaign_id)
        audio_artifacts: list[Artifact] = []
        audio_paths: list[Path] = []
        durations: list[float] = []
        for scene in scene_rows:
            artifact = _single_binary_artifact(
                db,
                campaign_id=campaign_id,
                kind=f"i5_scene_narration_{scene.position:03d}",
            )
            provenance = _canonical_provenance(artifact)
            contract = scene_contract(scene)
            if (
                provenance.get("scene_position") != scene.position
                or provenance.get("narration_sha256")
                != contract["narration_sha256"]
                or provenance.get("production_profile_hash") != context["profile_hash"]
                or provenance.get("storyboard_hash") != context["storyboard_hash"]
            ):
                raise WorkflowReplayConflict("Scene narration artifact lineage is invalid")
            path = store.resolve(artifact.uri, expected_sha256=artifact.sha256)
            audio_artifacts.append(artifact)
            audio_paths.append(path)
            durations.append(float(provenance["duration_seconds"]))
        input_hash = canonical_sha256(
            {
                "contract_version": "i5-voiceover-input-v1",
                "production_profile_hash": context["profile_hash"],
                "scene_audio_hashes": [artifact.sha256 for artifact in audio_artifacts],
                "storyboard_hash": context["storyboard_hash"],
            }
        )
        existing = list(
            db.scalars(
                select(Artifact).where(
                    Artifact.campaign_id == campaign_id,
                    Artifact.kind == "i5_voiceover",
                )
            )
        )
        if len(existing) > 1:
            raise WorkflowReplayConflict("Full voiceover identity is ambiguous")
        if existing:
            artifact = existing[0]
            store.resolve(artifact.uri, expected_sha256=artifact.sha256)
            provenance = _canonical_provenance(artifact)
            if provenance.get("generation_input_hash") != input_hash:
                raise WorkflowReplayConflict("Full voiceover replay lineage conflicts")
            return {
                "artifact_id": artifact.id,
                "duration_seconds": provenance["duration_seconds"],
                "output_hash": artifact.sha256,
                "replayed": True,
            }

    work_root = canonical_i5_path(
        settings.output_path,
        f"campaign_{campaign_id}",
        "work",
        input_hash,
    )
    work_root.mkdir(parents=True, exist_ok=True)
    output_path = work_root / "voiceover.wav"
    rendered = concat_wav_files(audio_paths, output_path)
    content = output_path.read_bytes()
    stored = CanonicalMediaStore(settings.output_path, campaign_id).write(
        content,
        extension="wav",
    )
    values = binary_artifact_values(
        campaign_id=campaign_id,
        kind="i5_voiceover",
        source_stage="media",
        uri=stored.uri,
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        mime_type="audio/wav",
        provider_name="ffmpeg_local",
        provider_model="i5-wav-concat-v1",
        input_hash=input_hash,
        provenance={
            "campaign_id": campaign_id,
            "duration_seconds": round(rendered.probe.duration_seconds, 6),
            "media_validation": {
                key: value
                for key, value in rendered.probe.metadata().items()
                if key != "path"
            },
            "ordered_scene_audio_hashes": [artifact.sha256 for artifact in audio_artifacts],
            "production_profile_hash": context["profile_hash"],
            "rights_basis": "original_i4_narration",
            "storyboard_hash": context["storyboard_hash"],
        },
    )
    persisted = persist_zero_cost_binary(
        campaign_id=campaign_id,
        scene_id=None,
        artifact_values=values,
        provider="ffmpeg_local",
        model="i5-wav-concat-v1",
        input_hash=input_hash,
    )
    return {
        **persisted,
        "duration_seconds": round(rendered.probe.duration_seconds, 6),
        "replayed": False,
    }


def build_media_manifest_from_session(
    db,  # type: ignore[no-untyped-def]
    *,
    campaign_id: int,
    settings: Settings,
) -> dict[str, object]:
    context = reconcile_storyboard_effect_set(db, campaign_id)
    expected_i5_job_ids = {context["storyboard_job"].id}  # type: ignore[union-attr]
    media_plan_artifact, media_plan_payload = latest_json_artifact(
        db,
        campaign_id,
        I5_MEDIA_PLAN_KIND,
    )
    if media_plan_artifact is None or media_plan_payload is None:
        raise WorkflowTransitionError("I5 media plan is missing")
    profile_tts = dict(
        cast(Mapping[str, object], context["profile_payload"])["tts"]  # type: ignore[index]
    )
    policy = load_campaign_budget_policy()
    selected_tts_model = str(media_plan_payload.get("selected_tts_model") or "")
    expected_budget_decision = (
        "ALLOW_PRIMARY"
        if selected_tts_model == profile_tts.get("primary_model")
        else "USE_LOWER_COST_FALLBACK"
    )
    canonical_requests: list[dict[str, object]] = []
    for scene in cast(Sequence[Scene], context["scenes"]):
        contract = scene_contract(scene)
        narration = str(contract["narration"])
        canonical_requests.append(
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
    price = 30 if selected_tts_model == "tts-1-hd" else 15
    expected_reservations = [
        {
            **request,
            "reserved_cost_microusd": (
                int(request["character_count"]) * price * 120 + 99
            )
            // 100,
        }
        for request in canonical_requests
    ]
    if (
        media_plan_payload.get("campaign_id") != campaign_id
        or media_plan_payload.get("contract_version") != "i5-media-plan-v1"
        or media_plan_payload.get("storyboard_hash") != context["storyboard_hash"]
        or media_plan_payload.get("production_profile_hash") != context["profile_hash"]
        or media_plan_payload.get("budget_policy_sha256") != policy.policy_sha256
        or media_plan_payload.get("budget_decision") != expected_budget_decision
        or media_plan_payload.get("tts_voice") != profile_tts.get("voice")
        or selected_tts_model
        not in {
            profile_tts.get("primary_model"),
            profile_tts.get("fallback_model"),
        }
        or media_plan_payload.get("requests") != canonical_requests
        or media_plan_payload.get("reservations") != expected_reservations
        or set(media_plan_payload)
        != {
            "budget_decision",
            "budget_policy_sha256",
            "campaign_id",
            "contract_version",
            "production_profile_hash",
            "requests",
            "reservations",
            "selected_tts_model",
            "storyboard_hash",
            "tts_voice",
        }
    ):
        raise WorkflowReplayConflict("I5 media plan lineage is invalid")
    media_plan_input_hash = canonical_sha256(
        {
            "contract_version": "i5-media-plan-input-v1",
            "production_profile_hash": context["profile_hash"],
            "storyboard_hash": context["storyboard_hash"],
        }
    )
    _verify_artifact_fields(
        media_plan_artifact,
        {
            "byte_size": len(media_plan_artifact.payload_json.encode("utf-8")),  # type: ignore[union-attr]
            "campaign_id": campaign_id,
            "kind": I5_MEDIA_PLAN_KIND,
            "mime_type": "application/json",
            "payload_json": canonical_json(media_plan_payload),
            "prompt_template_version": "i5-media-planner-v1-contract",
            "provenance_json": canonical_json(
                {
                    "contract_version": "i5-artifact-provenance-v1",
                    "hash_scope": "exact_payload_json_utf8_bytes",
                    "immutable": True,
                    "input_hash": media_plan_input_hash,
                    "origin": "i5_durable_production_workflow",
                }
            ),
            "provider_model": "i5-media-planner-v1",
            "provider_name": "campaign_budget_guard",
            "sha256": canonical_sha256(media_plan_payload),
            "source_stage": "media",
            "uri": (
                f"artifact://campaign/{campaign_id}/{I5_MEDIA_PLAN_KIND}/"
                f"{canonical_sha256(media_plan_payload)}"
            ),
        },
        label="I5 media plan artifact",
    )

    store = CanonicalMediaStore(settings.output_path, campaign_id)
    scene_media: list[dict[str, object]] = []
    generated_image_count = 0
    generated_video_count = 0
    generated_video_seconds = 0.0
    generated_coverage_seconds = 0.0
    local_count = 0
    fallback_count = 0
    terminal_tts_job_ids: set[int] = set()
    wav_probes = _bounded_validation_cache(_VALIDATED_WAV_BY_SHA256)
    png_dimensions = _bounded_validation_cache(_VALIDATED_PNG_BY_SHA256)
    video_probes = _bounded_validation_cache(_VALIDATED_VIDEO_BY_SHA256)
    expected_kinds: set[str] = set()
    for scene in cast(Sequence[Scene], context["scenes"]):
        contract = scene_contract(scene)
        audio_kind = f"i5_scene_narration_{scene.position:03d}"
        visual_kind = f"i5_scene_visual_{scene.position:03d}"
        expected_kinds.update((audio_kind, visual_kind))
        audio = _single_binary_artifact(
            db,
            campaign_id=campaign_id,
            kind=audio_kind,
        )
        visual = _single_binary_artifact(
            db,
            campaign_id=campaign_id,
            kind=visual_kind,
        )
        audio_path = store.resolve(audio.uri, expected_sha256=audio.sha256)
        visual_path = store.resolve(visual.uri, expected_sha256=visual.sha256)
        if (
            audio.byte_size != audio_path.stat().st_size
            or visual.byte_size != visual_path.stat().st_size
        ):
            raise WorkflowReplayConflict("Scene media byte size is invalid")
        audio_provenance = _canonical_provenance(audio)
        visual_provenance = _canonical_provenance(visual)
        if (
            audio_provenance.get("narration_sha256")
            != contract["narration_sha256"]
            or audio_provenance.get("scene_position") != scene.position
            or visual_provenance.get("scene_position") != scene.position
            or audio_provenance.get("production_profile_hash")
            != context["profile_hash"]
            or visual_provenance.get("production_profile_hash")
            != context["profile_hash"]
            or audio_provenance.get("storyboard_hash") != context["storyboard_hash"]
            or visual_provenance.get("storyboard_hash") != context["storyboard_hash"]
        ):
            raise WorkflowReplayConflict("Scene media lineage is invalid")
        audio_jobs = list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.campaign_id == campaign_id,
                    GenerationJob.scene_id == scene.id,
                    GenerationJob.output_artifact_id == audio.id,
                )
            )
        )
        visual_jobs = list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.campaign_id == campaign_id,
                    GenerationJob.scene_id == scene.id,
                    GenerationJob.output_artifact_id == visual.id,
                )
            )
        )
        if len(audio_jobs) != 1 or audio_jobs[0].status not in {
            "provider_complete",
            "completed",
        }:
            raise WorkflowReplayConflict("Scene narration job is incomplete or ambiguous")
        if len(visual_jobs) != 1 or visual_jobs[0].status not in {
            "provider_complete",
            "completed",
        }:
            raise WorkflowReplayConflict("Scene visual job is incomplete or ambiguous")

        audio_job = audio_jobs[0]
        visual_job = visual_jobs[0]
        reconcile_completed_metered_job(db, audio_job)
        terminal_tts_job_ids.add(audio_job.id)
        expected_i5_job_ids.add(audio_job.id)
        if audio.sha256 not in wav_probes:
            wav_probes[audio.sha256] = _validate_wav_path(audio_path)
        audio_probe = wav_probes[audio.sha256]
        audio_duration = float(audio_provenance.get("duration_seconds") or 0)
        if (
            audio.mime_type != "audio/wav"
            or audio.provider_name != "openai_tts"
            or audio.provider_model != selected_tts_model
            or abs(audio_probe.duration_seconds - audio_duration) > 0.02
        ):
            raise WorkflowReplayConflict("Canonical scene narration validation conflicts")

        primary = dict(contract.get("primary_fulfillment") or {})
        if visual.provider_name == "openai_image":
            reconcile_completed_metered_job(db, visual_job)
            if visual.sha256 not in png_dimensions:
                png_dimensions[visual.sha256] = _validate_png_path(visual_path)
            if (
                contract.get("visual_mode") != "GENERATED_CINEMATIC"
                or visual.mime_type != "image/png"
                or png_dimensions[visual.sha256] != (1280, 720)
                or visual_provenance.get("generated_media_is_evidence") is not False
            ):
                raise WorkflowReplayConflict("Generated image media validation failed")
            generated_image_count += 1
            generated_coverage_seconds += float(
                contract.get("estimated_duration_seconds") or 0
            )
            if (
                primary.get("strategy") != "generated_image_primary"
                or visual.provider_model != "gpt-image-2-medium"
            ):
                fallback_count += 1
        elif visual.provider_name == "openai_video":
            reconcile_completed_metered_job(db, visual_job)
            if visual.sha256 not in video_probes:
                video_probes[visual.sha256] = _validate_video_path(visual_path)
            video_probe = video_probes[visual.sha256]
            if (
                contract.get("visual_mode") != "GENERATED_CINEMATIC"
                or primary.get("strategy") != "generated_video_primary"
                or visual.mime_type != "video/mp4"
                or visual_provenance.get("generated_media_is_evidence") is not False
                or video_probe.duration_seconds + 0.02 < audio_probe.duration_seconds
                or visual_provenance.get("media_validation")
                != _portable_probe(video_probe)
            ):
                raise WorkflowReplayConflict("Generated video media validation failed")
            generated_video_count += 1
            generated_video_seconds += video_probe.duration_seconds
            generated_coverage_seconds += video_probe.duration_seconds
        elif visual.provider_name == "deterministic_local":
            from .local_visuals import render_local_visual

            expected_fallback = contract.get("visual_mode") == "GENERATED_CINEMATIC"
            local_request = _canonical_local_visual_request(
                contract,
                fallback=expected_fallback,
            )
            local_result = render_local_visual(local_request)
            local_input_hash = canonical_sha256(
                {
                    "contract_version": "i5-local-visual-input-v1",
                    "fallback": expected_fallback,
                    "local_request_hash": local_result.request_sha256,
                    "production_profile_hash": context["profile_hash"],
                    "storyboard_hash": context["storyboard_hash"],
                }
            )
            expected_visual = binary_artifact_values(
                campaign_id=campaign_id,
                kind=f"i5_scene_visual_{scene.position:03d}",
                source_stage="media",
                uri=(
                    f"i5-object://campaign/{campaign_id}/objects/"
                    f"{local_result.sha256}.png"
                ),
                sha256=local_result.sha256,
                byte_size=len(local_result.png_bytes),
                mime_type="image/png",
                provider_name="deterministic_local",
                provider_model=local_result.renderer_version,
                input_hash=local_input_hash,
                provenance={
                    "campaign_id": campaign_id,
                    "claim_hashes": list(local_request.claim_hashes),
                    "disclosure_state": local_request.disclosure_state,
                    "fallback": expected_fallback,
                    "media_validation": {
                        "height": local_result.height,
                        "valid": True,
                        "width": local_result.width,
                    },
                    "production_profile_hash": context["profile_hash"],
                    "rights_basis": local_request.rights_basis,
                    "scene_position": scene.position,
                    "source_keys": list(local_request.source_keys),
                    "storyboard_hash": context["storyboard_hash"],
                    "visual_mode": str(local_request.mode),
                },
            )
            _verify_artifact_fields(
                visual,
                expected_visual,
                label="Deterministic scene visual artifact",
            )
            _validate_zero_cost_media_job(
                visual_job,
                artifact=visual,
                campaign_id=campaign_id,
                scene_id=scene.id,
            )
            if visual.sha256 not in png_dimensions:
                png_dimensions[visual.sha256] = _validate_png_path(visual_path)
            if (
                visual.mime_type != "image/png"
                or png_dimensions[visual.sha256] != (1280, 720)
                or visual_provenance.get("rights_basis")
                not in {"original_deterministic", "reference_only"}
            ):
                raise WorkflowReplayConflict("Deterministic visual validation failed")
            local_count += 1
            if visual_provenance.get("fallback") is True:
                fallback_count += 1
        else:
            raise WorkflowReplayConflict("Scene visual provider is not canonical")
        expected_i5_job_ids.add(visual_job.id)

        metered_visual_jobs = list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.campaign_id == campaign_id,
                    GenerationJob.scene_id == scene.id,
                    GenerationJob.provider.in_(("openai_image", "openai_video")),
                )
            )
        )
        if len(metered_visual_jobs) > 1:
            raise WorkflowReplayConflict("Scene has multiple metered visual attempts")
        if visual.provider_name in {"openai_image", "openai_video"}:
            if metered_visual_jobs != [visual_job]:
                raise WorkflowReplayConflict("Generated visual job identity is invalid")
        elif metered_visual_jobs:
            failed_job = metered_visual_jobs[0]
            expected_i5_job_ids.add(failed_job.id)
            reconcile_failed_metered_job(db, failed_job)
            expected_reservation = {
                ("openai_image", "gpt-image-2-medium"): 100_000,
                ("openai_image", "gpt-image-2-low"): 25_000,
                ("openai_video", "sora-2"): 960_000,
            }.get((failed_job.provider, failed_job.model))
            try:
                failed_usage = json.loads(failed_job.usage_json or "")
                failed_error = json.loads(failed_job.error_json or "")
            except json.JSONDecodeError:
                raise WorkflowReplayConflict(
                    "Failed visual job metadata is invalid"
                ) from None
            primary_strategy = str(primary.get("strategy") or "")
            expected_budget_decision = (
                "ALLOW_PRIMARY"
                if (
                    (primary_strategy == "generated_video_primary" and failed_job.model == "sora-2")
                    or (
                        primary_strategy == "generated_image_primary"
                        and failed_job.model == "gpt-image-2-medium"
                    )
                )
                else "USE_LOWER_COST_FALLBACK"
            )
            initial_usage = {
                "budget_decision": expected_budget_decision,
                "production_profile_hash": context["profile_hash"],
                "reservation_microusd": expected_reservation,
            }
            submitted_usage = (
                failed_job.provider == "openai_video"
                and failed_job.provider_job_id is not None
                and is_valid_sora_provider_job_id(failed_job.provider_job_id)
                and set(failed_usage)
                == {
                    "duration_seconds",
                    "model",
                    "provider_status",
                    "reservation_microusd",
                    "size",
                }
                and failed_usage.get("duration_seconds") == 8
                and failed_usage.get("model") == "sora-2"
                and failed_usage.get("provider_status")
                in {"queued", "in_progress", "processing", "completed"}
                and failed_usage.get("reservation_microusd") == 960_000
                and failed_usage.get("size") == "1280x720"
            )
            if (
                contract.get("visual_mode") != "GENERATED_CINEMATIC"
                or visual_provenance.get("fallback") is not True
                or expected_reservation is None
                or failed_job.attempt != 1
                or failed_job.input_hash
                != visual_input_hash(
                    campaign_id=campaign_id,
                    scene=contract,
                    profile_hash=str(context["profile_hash"]),
                    storyboard_hash=str(context["storyboard_hash"]),
                )
                or failed_job.status
                not in {"provider_failed", "ambiguous_dispatch"}
                or failed_job.output_artifact_id is not None
                or failed_job.cost_microunits is not None
                or failed_job.reserved_cost_microunits != expected_reservation
                or failed_job.completed_at is not None
                or not isinstance(failed_usage, dict)
                or canonical_json(failed_usage) != failed_job.usage_json
                or (
                    failed_usage != initial_usage
                    and not submitted_usage
                )
                or (
                    failed_usage == initial_usage
                    and failed_job.provider_job_id is not None
                )
                or not isinstance(failed_error, dict)
                or canonical_json(failed_error) != failed_job.error_json
                or failed_error.get("retryable_post") is not False
                or not str(failed_error.get("code") or "")
            ):
                raise WorkflowReplayConflict("Failed visual fallback lineage is invalid")
        scene_media.append(
            {
                "narration": {
                    "artifact_hash": audio.sha256,
                    "duration_seconds": round(audio_probe.duration_seconds, 6),
                    "mime_type": audio.mime_type,
                    "object_uri": audio.uri,
                },
                "scene_position": scene.position,
                "visual": {
                    "artifact_hash": visual.sha256,
                    "mime_type": visual.mime_type,
                    "object_uri": visual.uri,
                    "provider": visual.provider_name,
                    "visual_mode": contract["visual_mode"],
                },
            }
        )

    guard_jobs = list(
        db.scalars(
            select(GenerationJob).where(
                GenerationJob.campaign_id == campaign_id,
                GenerationJob.provider == "campaign_budget_guard",
            )
        )
    )
    guarded_scene_ids: set[int] = set()
    scenes_by_id = {
        scene.id: scene for scene in cast(Sequence[Scene], context["scenes"])
    }
    for guard_job in guard_jobs:
        expected_i5_job_ids.add(guard_job.id)
        scene = scenes_by_id.get(int(guard_job.scene_id or 0))
        artifact = (
            db.get(Artifact, guard_job.output_artifact_id)
            if guard_job.output_artifact_id is not None
            else None
        )
        if scene is None or artifact is None or scene.id in guarded_scene_ids:
            raise WorkflowReplayConflict("Visual budget-choice effect set is invalid")
        guarded_scene_ids.add(scene.id)
        contract = scene_contract(scene)
        expected_input_hash = visual_input_hash(
            campaign_id=campaign_id,
            scene=contract,
            profile_hash=str(context["profile_hash"]),
            storyboard_hash=str(context["storyboard_hash"]),
        )
        try:
            choice_payload = json.loads(artifact.payload_json or "")
        except json.JSONDecodeError:
            raise WorkflowReplayConflict("Visual budget-choice payload is invalid") from None
        expected_choice = {
            "campaign_id": campaign_id,
            "contract_version": "i5-local-visual-choice-v1",
            "decision": "USE_LOWER_COST_FALLBACK",
            "production_profile_hash": context["profile_hash"],
            "scene_position": scene.position,
            "selected": "deterministic_local",
            "visual_input_hash": expected_input_hash,
        }
        if (
            choice_payload != expected_choice
            or canonical_json(choice_payload) != artifact.payload_json
            or canonical_sha256(choice_payload) != artifact.sha256
            or artifact.kind != f"i5_visual_choice_{scene.position:03d}"
            or artifact.provider_name != "campaign_budget_guard"
            or artifact.provider_model != "i5-visual-choice-v1"
            or artifact.mime_type != "application/json"
            or guard_job.model != "deterministic_local"
            or guard_job.attempt != 1
            or guard_job.status != "completed"
            or guard_job.input_hash != expected_input_hash
            or guard_job.provider_job_id != f"i5:local:{expected_input_hash}"
            or guard_job.usage_json
            != canonical_json({"metered_cost_microunits": 0})
            or guard_job.cost_microunits != 0
            or guard_job.reserved_cost_microunits != 0
            or guard_job.error_json is not None
            or guard_job.completed_at is None
        ):
            raise WorkflowReplayConflict("Visual budget-choice lineage is invalid")

    actual_scene_kinds = set(
        db.scalars(
            select(Artifact.kind).where(
                Artifact.campaign_id == campaign_id,
                Artifact.kind.like("i5_scene_%"),
            )
        )
    )
    if actual_scene_kinds != expected_kinds:
        raise WorkflowReplayConflict("Campaign contains unexpected canonical scene media")

    voiceover = _single_binary_artifact(
        db,
        campaign_id=campaign_id,
        kind="i5_voiceover",
    )
    voiceover_path = store.resolve(voiceover.uri, expected_sha256=voiceover.sha256)
    if voiceover.byte_size != voiceover_path.stat().st_size:
        raise WorkflowReplayConflict("Full voiceover byte size is invalid")
    voiceover_provenance = _canonical_provenance(voiceover)
    if voiceover.sha256 not in wav_probes:
        wav_probes[voiceover.sha256] = _validate_wav_path(voiceover_path)
    voiceover_probe = wav_probes[voiceover.sha256]
    ordered_audio_hashes = [
        str(item["narration"]["artifact_hash"])  # type: ignore[index]
        for item in scene_media
    ]
    voiceover_duration = float(voiceover_provenance.get("duration_seconds") or 0)
    voiceover_input_hash = canonical_sha256(
        {
            "contract_version": "i5-voiceover-input-v1",
            "production_profile_hash": context["profile_hash"],
            "scene_audio_hashes": ordered_audio_hashes,
            "storyboard_hash": context["storyboard_hash"],
        }
    )
    if (
        voiceover_provenance.get("ordered_scene_audio_hashes") != ordered_audio_hashes
        or abs(voiceover_probe.duration_seconds - voiceover_duration) > 0.02
        or abs(
            voiceover_probe.duration_seconds
            - sum(
                float(item["narration"]["duration_seconds"])  # type: ignore[index]
                for item in scene_media
            )
        )
        > max(0.08, len(scene_media) * 0.01)
    ):
        raise WorkflowReplayConflict("Full voiceover scene order is invalid")
    expected_voiceover = binary_artifact_values(
        campaign_id=campaign_id,
        kind="i5_voiceover",
        source_stage="media",
        uri=voiceover.uri,
        sha256=voiceover.sha256,
        byte_size=voiceover.byte_size,
        mime_type="audio/wav",
        provider_name="ffmpeg_local",
        provider_model="i5-wav-concat-v1",
        input_hash=voiceover_input_hash,
        provenance={
            "campaign_id": campaign_id,
            "duration_seconds": round(voiceover_probe.duration_seconds, 6),
            "media_validation": _portable_probe(voiceover_probe),
            "ordered_scene_audio_hashes": ordered_audio_hashes,
            "production_profile_hash": context["profile_hash"],
            "rights_basis": "original_i4_narration",
            "storyboard_hash": context["storyboard_hash"],
        },
    )
    _verify_artifact_fields(
        voiceover,
        expected_voiceover,
        label="I5 full voiceover artifact",
    )
    voiceover_jobs = list(
        db.scalars(
            select(GenerationJob).where(
                GenerationJob.campaign_id == campaign_id,
                GenerationJob.output_artifact_id == voiceover.id,
            )
        )
    )
    if len(voiceover_jobs) != 1:
        raise WorkflowReplayConflict("Full voiceover job identity is ambiguous")
    _validate_zero_cost_media_job(
        voiceover_jobs[0],
        artifact=voiceover,
        campaign_id=campaign_id,
        scene_id=None,
    )
    expected_i5_job_ids.add(voiceover_jobs[0].id)

    cost = campaign_cost(db, campaign_id)
    cap = authorized_campaign_cap(db, campaign_id)
    if (
        cost.total_microusd > cap
        or cost.total_microusd < 0
        or cost.committed_microusd < 0
        or cost.reserved_microusd < 0
    ):
        raise WorkflowReplayConflict(
            "Campaign cost exceeds or violates the authorized budget cap"
        )
    all_tts_job_ids = set(
        db.scalars(
            select(GenerationJob.id).where(
                GenerationJob.campaign_id == campaign_id,
                GenerationJob.provider == "openai_tts",
            )
        )
    )
    if all_tts_job_ids != terminal_tts_job_ids:
        raise WorkflowReplayConflict("Campaign TTS effect set is not exact")
    known_historical_jobs = {
        ("stub", "i3-deterministic-v1"),
        ("youtube_public_data+deterministic", "i4-topic-intelligence-v1"),
        ("deterministic_editorial", "i4-claims-v1"),
        ("deterministic_editorial", "i4-script-compiler-v1"),
    }
    known_i5_jobs = {
        ("deterministic_editorial", "i5-storyboard-compiler-v1"),
        ("campaign_budget_guard", "deterministic_local"),
        ("deterministic_local", "i5-local-visuals-v1"),
        ("ffmpeg_local", "i5-wav-concat-v1"),
        ("ffmpeg_local", "i5-ffmpeg-renderer-v1"),
        ("i5_media_integrity", "i5-media-manifest-v1"),
        ("openai_tts", "tts-1-hd"),
        ("openai_tts", "tts-1"),
        ("openai_image", "gpt-image-2-medium"),
        ("openai_image", "gpt-image-2-low"),
        ("openai_video", "sora-2"),
    }
    campaign_jobs = list(
        db.scalars(
            select(GenerationJob).where(GenerationJob.campaign_id == campaign_id)
        )
    )
    for campaign_job in campaign_jobs:
        identity = (campaign_job.provider, campaign_job.model)
        if identity not in known_historical_jobs | known_i5_jobs:
            raise WorkflowReplayConflict(
                "Campaign contains an unrecognized generation job effect"
            )
    downstream_jobs = [
        job
        for job in campaign_jobs
        if (job.provider, job.model)
        in {
            ("i5_media_integrity", "i5-media-manifest-v1"),
            ("ffmpeg_local", "i5-ffmpeg-renderer-v1"),
        }
    ]
    campaign_stage = context["campaign"].current_stage  # type: ignore[union-attr]
    downstream_identities = [
        (job.provider, job.model) for job in downstream_jobs
    ]
    expected_downstream = {
        "media": [],
        "assembly": [("i5_media_integrity", "i5-media-manifest-v1")],
        "machine_qa": [
            ("i5_media_integrity", "i5-media-manifest-v1"),
            ("ffmpeg_local", "i5-ffmpeg-renderer-v1"),
        ],
    }.get(str(campaign_stage))
    if (
        expected_downstream is None
        or sorted(downstream_identities) != sorted(expected_downstream)
    ):
        raise WorkflowReplayConflict("I5 downstream job effect set is invalid")
    actual_i5_job_ids = {
        job.id
        for job in campaign_jobs
        if (job.provider, job.model) in known_i5_jobs
    }
    if actual_i5_job_ids != expected_i5_job_ids | {
        job.id for job in downstream_jobs
    }:
        raise WorkflowReplayConflict("I5 generation job effect set is not exact")
    if any(
        job.model != selected_tts_model
        for job in db.scalars(
            select(GenerationJob).where(
                GenerationJob.campaign_id == campaign_id,
                GenerationJob.provider == "openai_tts",
            )
        )
    ):
        raise WorkflowReplayConflict("Campaign TTS model changed between scenes")
    profile_bounds = dict(
        cast(Mapping[str, object], context["profile_payload"])[  # type: ignore[index]
            "generated_media_bounds"
        ]
    )
    storyboard_summary = dict(
        cast(Mapping[str, object], context["storyboard_packet"])[  # type: ignore[index]
            "generated_media_summary"
        ]
    )
    if (
        generated_image_count + generated_video_count + local_count
        != len(scene_media)
        or generated_image_count + generated_video_count
        > int(profile_bounds["max_cinematic_scenes"])
        or generated_image_count + generated_video_count
        > int(storyboard_summary["generated_cinematic_scene_count"])
        or generated_video_count > int(profile_bounds["max_video_scenes"])
        or generated_video_count
        > int(storyboard_summary["generated_video_scene_count"])
        or generated_video_seconds
        > float(profile_bounds["max_video_total_seconds"]) + 0.01
        or generated_coverage_seconds
        > float(profile_bounds["max_cinematic_coverage_seconds"]) + 0.01
    ):
        raise WorkflowReplayConflict("Generated media exceeds its canonical bounds")
    generated_summary = {
        "generated_image_scene_count": generated_image_count,
        "generated_video_scene_count": generated_video_count,
        "generated_video_total_seconds": round(generated_video_seconds, 6),
        "local_visual_scene_count": local_count,
    }
    provider_fallback_summary = {
        "fallback_scene_count": fallback_count,
        "provider_scene_counts": {
            "deterministic_local": local_count,
            "openai_image": generated_image_count,
            "openai_video": generated_video_count,
        },
    }
    packet: dict[str, object] = {
        "authorized_cap_microusd": cap,
        "budget_policy_sha256": policy.policy_sha256,
        "budget_policy_version": policy.policy_version,
        "campaign_id": campaign_id,
        "contract_version": "i5-media-manifest-v1",
        "effective_metered_campaign_cost_microusd": cost.total_microusd,
        "generated_media_summary": generated_summary,
        "integrity": {
            "all_content_hashes_verified": True,
            "all_objects_inside_canonical_store": True,
            "all_scene_media_present": True,
            "generated_media_is_illustrative_only": True,
            "rights_gate_passed": True,
        },
        "media_plan_hash": media_plan_artifact.sha256,
        "production_profile_hash": context["profile_hash"],
        "provider_fallback_summary": provider_fallback_summary,
        "reserved_in_flight_cost_microusd": cost.reserved_microusd,
        "scene_media": scene_media,
        "selected_tts_model": selected_tts_model,
        "soft_warning_active": cost.total_microusd
        >= policy.limits_microusd.soft_warning,
        "storyboard_hash": context["storyboard_hash"],
        "tts_voice": "onyx",
        "voiceover": {
            "artifact_hash": voiceover.sha256,
            "duration_seconds": voiceover_provenance["duration_seconds"],
            "object_uri": voiceover.uri,
        },
    }
    packet["gate"] = {
        "outcome": "PASS",
        "reasons": ["all_scene_media_and_budget_integrity_checks_passed"],
    }
    return packet


def build_media_manifest(campaign_id: int, settings: Settings) -> dict[str, object]:
    with SessionLocal() as db:
        return build_media_manifest_from_session(
            db,
            campaign_id=campaign_id,
            settings=settings,
        )
