from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from functools import lru_cache
from typing import Mapping, Sequence

from app.editorial.contracts import canonical_sha256
from app.editorial.script_compiler import REQUIRED_SECTION_IDS

from .contracts import (
    FactualOverlay,
    FulfillmentPlan,
    GeneratedMediaSummary,
    I5_GENERATED_CINEMATIC_MAX_SCENE_SECONDS,
    I5_MAX_GENERATED_CINEMATIC_SCENES,
    I5_MAX_FACTUAL_OVERLAY_CHARS,
    I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS,
    I5_MAX_GENERATED_VIDEO_SCENES,
    I5_MAX_GENERATED_VIDEO_SECONDS,
    I5_MAX_SCENES,
    I5_MIN_SCENES,
    I5_NARRATION_NORMALIZATION,
    I5_NARRATION_WORDS_PER_MINUTE,
    I5_ORDINARY_SCENE_MAX_SECONDS,
    I5_ORDINARY_SCENE_MIN_SECONDS,
    I5_PRODUCTION_POLICY_VERSION,
    I5_SCENE_DURATION_BOUNDARY_TOLERANCE_SECONDS,
    I5_STORYBOARD_CONTRACT_VERSION,
    RightsBasis,
    SourceRights,
    StoryboardCompilationOptions,
    StoryboardPacket,
    StoryboardScene,
)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WORD_PATTERN = re.compile(r"\b[\w'-]+\b")
_SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[.!?])\s+")
_NUMERIC_TOKEN_PATTERN = re.compile(
    r"(?<![\w])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:%|[kmbt])?(?![\w])",
    re.IGNORECASE,
)

_TARGET_SCENE_WORDS = 60
_MIN_SCENE_WORDS = round(
    I5_NARRATION_WORDS_PER_MINUTE * I5_ORDINARY_SCENE_MIN_SECONDS / 60
)
_MAX_SCENE_WORDS = round(
    I5_NARRATION_WORDS_PER_MINUTE * I5_ORDINARY_SCENE_MAX_SECONDS / 60
)

_LOCAL_MODE_CYCLE: tuple[str, ...] = (
    "KINETIC_TEXT",
    "DIAGRAM",
    "DOCUMENT",
    "DETERMINISTIC_MOTION_GRAPHIC",
    "CODE",
    "UI_RECONSTRUCTION",
    "TIMELINE",
)
_SOURCE_REUSE_MODES = frozenset(
    {"REAL_SOURCE_MEDIA", "PRODUCT_FOOTAGE", "SCREENSHOT"}
)
_REUSABLE_RIGHTS = frozenset({"owned", "licensed", "public_domain"})
_GENERATED_ELIGIBLE_SECTIONS = frozenset(
    {"cold_open", "setup", "consequences", "resolution", "what_next"}
)


class StoryboardInputError(ValueError):
    """The supplied I4 script is not an exact accepted canonical input."""


def normalize_narration(value: object) -> str:
    """Documented I5 reconstruction normalization: collapse all whitespace runs."""

    return " ".join(str(value).split())


def narration_sha256(narration: str) -> str:
    normalized = normalize_narration(narration)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _word_count(value: str) -> int:
    return len(_WORD_PATTERN.findall(value))


def _duration_seconds(value: str) -> float:
    words_per_second = I5_NARRATION_WORDS_PER_MINUTE / 60
    return round(_word_count(value) / words_per_second, 2)


def _sentences(narration: str) -> tuple[str, ...]:
    normalized = normalize_narration(narration)
    if not normalized:
        return ()
    return tuple(
        part
        for part in (
            normalize_narration(value)
            for value in _SENTENCE_BOUNDARY_PATTERN.split(normalized)
        )
        if part
    )


def _bounded_sentence_partition(sentences: Sequence[str]) -> tuple[str, ...] | None:
    counts = tuple(_word_count(sentence) for sentence in sentences)

    @lru_cache(maxsize=None)
    def solve(index: int) -> tuple[int, tuple[int, ...]] | None:
        if index == len(sentences):
            return 0, ()

        best: tuple[int, int, tuple[int, ...]] | None = None
        running_words = 0
        for end in range(index, len(sentences)):
            running_words += counts[end]
            if running_words > _MAX_SCENE_WORDS:
                break
            if running_words < _MIN_SCENE_WORDS:
                continue
            remainder = solve(end + 1)
            if remainder is None:
                continue
            tail_cost, tail_boundaries = remainder
            cost = (running_words - _TARGET_SCENE_WORDS) ** 2 + tail_cost
            boundaries = (end + 1, *tail_boundaries)
            candidate = (cost, len(boundaries), boundaries)
            if best is None or candidate < best:
                best = candidate

        if best is None:
            return None
        return best[0], best[2]

    solution = solve(0)
    if solution is None:
        return None
    _, boundaries = solution
    beats: list[str] = []
    start = 0
    for end in boundaries:
        beats.append(" ".join(sentences[start:end]))
        start = end
    return tuple(beats)


def _relaxed_sentence_partition(sentences: Sequence[str]) -> tuple[str, ...]:
    counts = tuple(_word_count(sentence) for sentence in sentences)

    @lru_cache(maxsize=None)
    def solve(index: int) -> tuple[int, tuple[int, ...]]:
        if index == len(sentences):
            return 0, ()
        best: tuple[int, int, tuple[int, ...]] | None = None
        running_words = 0
        for end in range(index, len(sentences)):
            running_words += counts[end]
            below = max(0, _MIN_SCENE_WORDS - running_words)
            above = max(0, running_words - _MAX_SCENE_WORDS)
            boundary_penalty = (below + above) * 10_000
            tail_cost, tail_boundaries = solve(end + 1)
            cost = (
                boundary_penalty
                + ((running_words - _TARGET_SCENE_WORDS) ** 2)
                + tail_cost
            )
            boundaries = (end + 1, *tail_boundaries)
            candidate = (cost, len(boundaries), boundaries)
            if best is None or candidate < best:
                best = candidate
        assert best is not None
        return best[0], best[2]

    _, boundaries = solve(0)
    beats: list[str] = []
    start = 0
    for end in boundaries:
        beats.append(" ".join(sentences[start:end]))
        start = end
    return tuple(beats)


def split_section_narration(narration: str) -> tuple[str, ...]:
    """Split one I4 section only at deterministic sentence boundaries."""

    sentences = _sentences(narration)
    if not sentences:
        return ()
    return _bounded_sentence_partition(sentences) or _relaxed_sentence_partition(
        sentences
    )


def _validated_script_input(
    *,
    campaign_id: int,
    script_packet: Mapping[str, object],
    script_hash: str,
) -> tuple[
    list[dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
]:
    if campaign_id <= 0:
        raise StoryboardInputError("campaign_id must be positive")
    if _SHA256_PATTERN.fullmatch(script_hash) is None:
        raise StoryboardInputError("script_hash must be a full lowercase SHA-256")
    canonical_packet = dict(script_packet)
    if canonical_sha256(canonical_packet) != script_hash:
        raise StoryboardInputError("script_hash does not match the exact I4 script packet")
    if canonical_packet.get("contract_version") != "i4-script-v1":
        raise StoryboardInputError("I5 requires the canonical I4 script contract")
    if canonical_packet.get("campaign_id") != campaign_id:
        raise StoryboardInputError("I4 script campaign identity does not match")
    gate = canonical_packet.get("gate")
    if not isinstance(gate, Mapping) or gate.get("outcome") != "PASS":
        raise StoryboardInputError("I5 requires an accepted I4 script gate")

    raw_sections = canonical_packet.get("sections")
    if not isinstance(raw_sections, list):
        raise StoryboardInputError("I4 script sections must be a list")
    sections = [dict(section) for section in raw_sections]
    section_ids = tuple(str(section.get("section_id") or "") for section in sections)
    if section_ids != REQUIRED_SECTION_IDS:
        raise StoryboardInputError("I4 script sections are missing or out of order")

    raw_claim_index = canonical_packet.get("claim_index")
    raw_source_index = canonical_packet.get("source_index")
    if not isinstance(raw_claim_index, Mapping) or not isinstance(
        raw_source_index, Mapping
    ):
        raise StoryboardInputError("I4 script lineage indexes are invalid")
    claim_index = {
        str(key): dict(value)
        for key, value in raw_claim_index.items()
        if isinstance(value, Mapping)
    }
    source_index = {
        str(key): dict(value)
        for key, value in raw_source_index.items()
        if isinstance(value, Mapping)
    }
    if len(claim_index) != len(raw_claim_index) or len(source_index) != len(
        raw_source_index
    ):
        raise StoryboardInputError("I4 script lineage indexes contain invalid records")

    for section in sections:
        narration = normalize_narration(section.get("narration") or "")
        if not narration:
            raise StoryboardInputError("I4 script contains a blank section narration")
        raw_hashes = section.get("claim_hashes")
        if not isinstance(raw_hashes, list) or not raw_hashes:
            raise StoryboardInputError("Every I4 section must carry claim lineage")
        for raw_hash in raw_hashes:
            claim_hash = str(raw_hash)
            if claim_hash not in claim_index:
                raise StoryboardInputError("I4 section references an unknown claim hash")
            claim = claim_index[claim_hash]
            if claim.get("claim_type") in {"fact", "attributed_claim"} and claim.get(
                "state"
            ) != "VERIFIED":
                raise StoryboardInputError("I4 section contains an unverified factual claim")
            for source_key in claim.get("source_keys", []):
                if str(source_key) not in source_index:
                    raise StoryboardInputError("I4 claim source lineage is incomplete")

    reconstructed = " ".join(
        normalize_narration(section["narration"]) for section in sections
    )
    if canonical_packet.get("word_count") != _word_count(reconstructed):
        raise StoryboardInputError("I4 script word count does not match its narration")
    if canonical_packet.get("narration_rate_words_per_minute") != (
        I5_NARRATION_WORDS_PER_MINUTE
    ):
        raise StoryboardInputError("I4 narration planning rate is not canonical")
    return sections, claim_index, source_index


def _section_claim_hashes(section: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(value) for value in section.get("claim_hashes", []))


def _claim_source_keys(
    claim_hashes: Sequence[str],
    claim_index: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(source_key)
                for claim_hash in claim_hashes
                for source_key in claim_index[claim_hash].get("source_keys", [])
            }
        )
    )


def _matching_verified_claims(
    narration: str,
    claim_hashes: Sequence[str],
    claim_index: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    normalized = normalize_narration(narration).casefold()
    matching: list[tuple[str, Mapping[str, object]]] = []
    for claim_hash in claim_hashes:
        claim = claim_index[claim_hash]
        assertion = normalize_narration(claim.get("assertion_text") or "")
        if (
            claim.get("state") == "VERIFIED"
            and claim.get("claim_type") in {"fact", "attributed_claim"}
            and assertion
            and assertion.casefold() in normalized
        ):
            matching.append((claim_hash, claim))
    return tuple(matching)


def _factual_overlay(
    narration: str,
    claim_hashes: Sequence[str],
    claim_index: Mapping[str, Mapping[str, object]],
) -> dict[str, object] | None:
    matching = _matching_verified_claims(narration, claim_hashes, claim_index)
    if not matching:
        return None
    claim_hash, claim = matching[0]
    assertion = normalize_narration(claim["assertion_text"])
    claim_type = str(claim["claim_type"])
    if claim_type == "attributed_claim":
        attribution = normalize_narration(claim.get("attribution") or "")
        text = f"According to {attribution}: {assertion}"
        overlay_kind = "attributed_verified_claim"
    else:
        text = assertion
        overlay_kind = "verified_fact"
    if len(text) > I5_MAX_FACTUAL_OVERLAY_CHARS:
        return None
    source_keys = tuple(sorted(str(value) for value in claim.get("source_keys", [])))
    if not source_keys:
        return None
    return FactualOverlay(
        kind=overlay_kind,  # type: ignore[arg-type]
        text=text,
        claim_hashes=(claim_hash,),
        source_keys=source_keys,
    ).model_dump(mode="json")


def _numeric_tokens(value: object) -> frozenset[str]:
    return frozenset(
        token.casefold().replace(",", "")
        for token in _NUMERIC_TOKEN_PATTERN.findall(str(value))
    )


def _overlay_has_verified_numeric_data(
    overlay: Mapping[str, object] | None,
    claim_index: Mapping[str, Mapping[str, object]],
) -> bool:
    if overlay is None:
        return False
    for claim_hash in overlay.get("claim_hashes", []):
        claim = claim_index.get(str(claim_hash))
        if (
            claim is not None
            and claim.get("state") == "VERIFIED"
            and claim.get("claim_type") in {"fact", "attributed_claim"}
            and _numeric_tokens(claim.get("assertion_text"))
        ):
            return True
    return False


def _source_rights(
    source_keys: Sequence[str],
    source_index: Mapping[str, Mapping[str, object]],
) -> tuple[SourceRights, ...]:
    return tuple(
        SourceRights(
            source_key=source_key,
            rights_status=str(
                source_index.get(source_key, {}).get("rights_status")
                or "reference_only"
            ),
        )
        for source_key in source_keys
    )


def _visual_purpose(mode: str, section_id: str) -> str:
    purposes = {
        "DOCUMENT": (
            "Present a locally rendered evidence card with explicit source and claim "
            "lineage; do not reproduce source media."
        ),
        "DIAGRAM": (
            "Map the relationships in this reasoning beat without adding empirical facts."
        ),
        "DATA_VISUALIZATION": (
            "Visualize only the verified numeric value carried by the factual overlay."
        ),
        "TIMELINE": (
            "Sequence the argument's progression without inventing dates or events."
        ),
        "CODE": (
            "Use visibly illustrative pseudocode to clarify the mechanism, not sourced code."
        ),
        "UI_RECONSTRUCTION": (
            "Show a visibly labeled conceptual reconstruction, not a product screenshot."
        ),
        "KINETIC_TEXT": (
            "Emphasize the bounded takeaway using only narration-derived language."
        ),
        "DETERMINISTIC_MOTION_GRAPHIC": (
            "Animate an abstract relationship locally without implying new evidence."
        ),
        "GENERATED_CINEMATIC": (
            "Provide conceptual atmosphere only, with no text, logos, charts, interfaces, "
            "citations, numbers, or evidentiary implication."
        ),
    }
    return f"{section_id}: {purposes[mode]}"


def _disclosure_state(mode: str) -> str:
    if mode == "UI_RECONSTRUCTION":
        return "visible_reconstruction_label"
    if mode == "CODE":
        return "illustrative_pseudocode"
    if mode == "GENERATED_CINEMATIC":
        return "illustrative_generated_media"
    return "not_required"


def _fulfillments(
    mode: str,
    *,
    generated_video: bool = False,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if mode == "GENERATED_CINEMATIC":
        if generated_video:
            primary = FulfillmentPlan(
                strategy="generated_video_primary",
                visual_mode="GENERATED_CINEMATIC",
                generated_video_seconds=8,
            )
            fallbacks = (
                FulfillmentPlan(
                    strategy="generated_image_primary",
                    visual_mode="GENERATED_CINEMATIC",
                ),
                FulfillmentPlan(
                    strategy="generated_image_fallback",
                    visual_mode="GENERATED_CINEMATIC",
                ),
                FulfillmentPlan(
                    strategy="local_deterministic",
                    visual_mode="DETERMINISTIC_MOTION_GRAPHIC",
                ),
            )
        else:
            primary = FulfillmentPlan(
                strategy="generated_image_primary",
                visual_mode="GENERATED_CINEMATIC",
            )
            fallbacks = (
                FulfillmentPlan(
                    strategy="generated_image_fallback",
                    visual_mode="GENERATED_CINEMATIC",
                ),
                FulfillmentPlan(
                    strategy="local_deterministic",
                    visual_mode="DETERMINISTIC_MOTION_GRAPHIC",
                ),
            )
    else:
        fallback_mode = (
            "KINETIC_TEXT"
            if mode == "DETERMINISTIC_MOTION_GRAPHIC"
            else "DETERMINISTIC_MOTION_GRAPHIC"
        )
        primary = FulfillmentPlan(
            strategy="local_deterministic",
            visual_mode=mode,  # type: ignore[arg-type]
        )
        fallbacks = (
            FulfillmentPlan(
                strategy="local_deterministic",
                visual_mode=fallback_mode,  # type: ignore[arg-type]
            ),
        )
    return (
        primary.model_dump(mode="json"),
        [fallback.model_dump(mode="json") for fallback in fallbacks],
    )


def _rights_basis(
    *,
    mode: str,
    source_keys: Sequence[str],
    source_index: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    source_rights = _source_rights(source_keys, source_index)
    if mode == "GENERATED_CINEMATIC":
        kind = "generated_illustrative"
    elif mode == "DOCUMENT" and source_rights:
        kind = "reference_only_evidence_card"
    else:
        kind = "local_original"
    return RightsBasis(
        kind=kind,  # type: ignore[arg-type]
        reuses_source_media=False,
        source_rights=source_rights,
    ).model_dump(mode="json")


def _apply_mode(
    scene: dict[str, object],
    *,
    mode: str,
    source_index: Mapping[str, Mapping[str, object]],
    generated_video: bool = False,
) -> None:
    source_keys = [str(value) for value in scene["source_keys"]]  # type: ignore[union-attr]
    primary, fallbacks = _fulfillments(
        mode,
        generated_video=generated_video,
    )
    scene.update(
        {
            "disclosure_state": _disclosure_state(mode),
            "fallback_fulfillments": fallbacks,
            "primary_fulfillment": primary,
            "rights_basis": _rights_basis(
                mode=mode,
                source_keys=source_keys,
                source_index=source_index,
            ),
            "visual_mode": mode,
            "visual_purpose": _visual_purpose(mode, str(scene["section_id"])),
        }
    )


def _build_scenes(
    *,
    sections: Sequence[Mapping[str, object]],
    claim_index: Mapping[str, Mapping[str, object]],
    source_index: Mapping[str, Mapping[str, object]],
    generated_cinematic_enabled: bool,
    generated_video_enabled: bool,
) -> list[dict[str, object]]:
    scenes: list[dict[str, object]] = []
    for section in sections:
        section_id = str(section["section_id"])
        claim_hashes = _section_claim_hashes(section)
        source_keys = _claim_source_keys(claim_hashes, claim_index)
        beats = split_section_narration(str(section["narration"]))
        for beat_index, narration in enumerate(beats):
            overlay = _factual_overlay(narration, claim_hashes, claim_index)
            base_mode = _LOCAL_MODE_CYCLE[len(scenes) % len(_LOCAL_MODE_CYCLE)]
            if _overlay_has_verified_numeric_data(overlay, claim_index):
                base_mode = "DATA_VISUALIZATION"
            scene: dict[str, object] = {
                "beat_index": beat_index,
                "claim_hashes": list(claim_hashes),
                "continuation_reason": normalize_narration(
                    section["continuation_reason"]
                ),
                "estimated_duration_seconds": _duration_seconds(narration),
                "factual_overlay": overlay,
                "narration": narration,
                "narration_sha256": narration_sha256(narration),
                "payoff": normalize_narration(section["payoff"]),
                "position": len(scenes),
                "section_id": section_id,
                "source_keys": list(source_keys),
            }
            _apply_mode(scene, mode=base_mode, source_index=source_index)
            scenes.append(scene)

    if generated_cinematic_enabled:
        selected_positions: list[int] = []
        selected_sections: set[str] = set()
        coverage_seconds = 0.0
        for scene in scenes:
            duration = float(scene["estimated_duration_seconds"])
            position = int(scene["position"])
            section_id = str(scene["section_id"])
            if (
                scene["factual_overlay"] is not None
                or duration > I5_GENERATED_CINEMATIC_MAX_SCENE_SECONDS
                or section_id not in _GENERATED_ELIGIBLE_SECTIONS
                or section_id in selected_sections
                or (selected_positions and position - selected_positions[-1] <= 1)
                or coverage_seconds + duration
                > I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS
            ):
                continue
            _apply_mode(
                scene,
                mode="GENERATED_CINEMATIC",
                source_index=source_index,
                generated_video=(
                    generated_video_enabled
                    and len(selected_positions) < I5_MAX_GENERATED_VIDEO_SCENES
                    and (len(selected_positions) + 1) * 8
                    <= I5_MAX_GENERATED_VIDEO_SECONDS
                ),
            )
            selected_positions.append(position)
            selected_sections.add(section_id)
            coverage_seconds += duration
            if len(selected_positions) == min(2, I5_MAX_GENERATED_CINEMATIC_SCENES):
                break

    return [StoryboardScene.model_validate(scene).model_dump(mode="json") for scene in scenes]


def _maximum_mode_run(scenes: Sequence[Mapping[str, object]]) -> int:
    maximum = 0
    current = 0
    previous: str | None = None
    for scene in scenes:
        mode = str(scene.get("visual_mode") or "")
        current = current + 1 if mode == previous else 1
        previous = mode
        maximum = max(maximum, current)
    return maximum


def _generated_media_summary(
    scenes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    cinematic_scenes = [
        scene for scene in scenes if scene.get("visual_mode") == "GENERATED_CINEMATIC"
    ]
    video_seconds: list[float] = []
    for scene in scenes:
        primary = scene.get("primary_fulfillment")
        if isinstance(primary, Mapping) and primary.get("strategy") == (
            "generated_video_primary"
        ):
            video_seconds.append(float(primary.get("generated_video_seconds") or 0))
    return GeneratedMediaSummary(
        generated_cinematic_scene_count=len(cinematic_scenes),
        generated_cinematic_estimated_coverage_seconds=round(
            sum(float(scene.get("estimated_duration_seconds") or 0) for scene in cinematic_scenes),
            2,
        ),
        generated_video_scene_count=len(video_seconds),
        generated_video_estimated_duration_seconds=round(sum(video_seconds), 2),
    ).model_dump(mode="json")


def _narration_reconstructs(
    scenes: Sequence[Mapping[str, object]],
    sections: Sequence[Mapping[str, object]],
) -> bool:
    expected = " ".join(normalize_narration(section["narration"]) for section in sections)
    actual = " ".join(normalize_narration(scene.get("narration") or "") for scene in scenes)
    return actual == expected


def meaningful_visual_progression_score(
    scenes: Sequence[Mapping[str, object]],
    *,
    script_sections: Sequence[Mapping[str, object]],
) -> int:
    """Report visual progression; this score never overrides hard gate failures."""

    modes = {str(scene.get("visual_mode") or "") for scene in scenes}
    diversity = min(25, len(modes) * 5)
    maximum_run = _maximum_mode_run(scenes)
    repeat_control = 20 if maximum_run <= 2 else max(0, 20 - ((maximum_run - 2) * 10))

    positions_are_complete = [scene.get("position") for scene in scenes] == list(
        range(len(scenes))
    )
    coverage = (
        20
        if positions_are_complete and _narration_reconstructs(scenes, script_sections)
        else 0
    )

    factual_overlays = [
        scene for scene in scenes if isinstance(scene.get("factual_overlay"), Mapping)
    ]
    factual_lineage = 15
    if factual_overlays:
        factual_lineage = round(
            15
            * sum(
                bool(scene.get("claim_hashes")) and bool(scene.get("source_keys"))
                for scene in factual_overlays
            )
            / len(factual_overlays)
        )

    summary = _generated_media_summary(scenes)
    generated_bounded = (
        int(summary["generated_cinematic_scene_count"])
        <= I5_MAX_GENERATED_CINEMATIC_SCENES
        and float(summary["generated_cinematic_estimated_coverage_seconds"])
        <= I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS
        and int(summary["generated_video_scene_count"])
        <= I5_MAX_GENERATED_VIDEO_SCENES
        and float(summary["generated_video_estimated_duration_seconds"])
        <= I5_MAX_GENERATED_VIDEO_SECONDS
    )
    generated_score = 10 if generated_bounded else 0
    purpose_variation = min(10, len(modes) * 2)
    return min(
        100,
        diversity
        + repeat_control
        + coverage
        + factual_lineage
        + generated_score
        + purpose_variation,
    )


def _visual_mode_counts(
    scenes: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    return dict(
        sorted(
            Counter(str(scene.get("visual_mode") or "") for scene in scenes).items()
        )
    )


def _rights_status_by_key(scene: Mapping[str, object]) -> dict[str, str]:
    rights_basis = scene.get("rights_basis")
    if not isinstance(rights_basis, Mapping):
        return {}
    raw_rights = rights_basis.get("source_rights")
    if not isinstance(raw_rights, list):
        return {}
    return {
        str(item.get("source_key")): str(item.get("rights_status"))
        for item in raw_rights
        if isinstance(item, Mapping)
    }


def _overlay_numeric_values_are_claim_bound(
    overlay: Mapping[str, object],
    claim_index: Mapping[str, Mapping[str, object]],
) -> bool:
    overlay_numbers = _numeric_tokens(overlay.get("text"))
    supported_numbers = frozenset(
        number
        for raw_hash in overlay.get("claim_hashes", [])
        for field in ("assertion_text", "attribution")
        for number in _numeric_tokens(claim_index.get(str(raw_hash), {}).get(field))
    )
    return overlay_numbers <= supported_numbers


def evaluate_storyboard_gate(
    storyboard_packet: Mapping[str, object],
    *,
    script_packet: Mapping[str, object],
    script_hash: str | None = None,
    production_profile_hash: str | None = None,
) -> dict[str, object]:
    failures: set[str] = set()
    resolved_script_hash = script_hash or str(storyboard_packet.get("script_hash") or "")
    try:
        sections, claim_index, source_index = _validated_script_input(
            campaign_id=int(storyboard_packet.get("campaign_id") or 0),
            script_packet=script_packet,
            script_hash=resolved_script_hash,
        )
    except (TypeError, ValueError) as exc:
        return {
            "outcome": "FAIL",
            "reasons": [f"invalid_accepted_i4_script:{exc}"],
        }

    if storyboard_packet.get("contract_version") != I5_STORYBOARD_CONTRACT_VERSION:
        failures.add("storyboard_contract_version_mismatch")
    if storyboard_packet.get("policy_version") != I5_PRODUCTION_POLICY_VERSION:
        failures.add("storyboard_policy_version_mismatch")
    if storyboard_packet.get("script_hash") != resolved_script_hash:
        failures.add("storyboard_script_hash_mismatch")
    if storyboard_packet.get("narration_normalization") != I5_NARRATION_NORMALIZATION:
        failures.add("storyboard_narration_normalization_mismatch")
    packet_profile_hash = str(storyboard_packet.get("production_profile_hash") or "")
    if _SHA256_PATTERN.fullmatch(packet_profile_hash) is None:
        failures.add("production_profile_hash_invalid")
    if (
        production_profile_hash is not None
        and packet_profile_hash != production_profile_hash
    ):
        failures.add("production_profile_hash_mismatch")

    raw_scenes = storyboard_packet.get("scenes")
    scenes = (
        [dict(scene) for scene in raw_scenes if isinstance(scene, Mapping)]
        if isinstance(raw_scenes, list)
        else []
    )
    if not isinstance(raw_scenes, list) or len(scenes) != len(raw_scenes):
        failures.add("storyboard_scenes_invalid")
    if storyboard_packet.get("scene_count") != len(scenes):
        failures.add("storyboard_scene_count_mismatch")
    if not I5_MIN_SCENES <= len(scenes) <= I5_MAX_SCENES:
        failures.add("storyboard_scene_count_out_of_bounds")

    sections_by_id = {str(section["section_id"]): section for section in sections}
    expected_beat_index: dict[str, int] = defaultdict(int)
    previous_section_index = -1
    for expected_position, scene in enumerate(scenes):
        section_id = str(scene.get("section_id") or "")
        if scene.get("position") != expected_position:
            failures.add("scene_positions_are_not_contiguous")
        if section_id not in sections_by_id:
            failures.add("scene_section_missing_from_i4_script")
            continue
        section_index = REQUIRED_SECTION_IDS.index(section_id)
        if section_index < previous_section_index:
            failures.add("scene_sections_are_out_of_order")
        previous_section_index = section_index
        if scene.get("beat_index") != expected_beat_index[section_id]:
            failures.add("section_beat_indexes_are_not_contiguous")
        expected_beat_index[section_id] += 1

        narration = str(scene.get("narration") or "")
        normalized_narration = normalize_narration(narration)
        if not normalized_narration or narration != normalized_narration:
            failures.add("scene_narration_is_not_canonical")
        if scene.get("narration_sha256") != narration_sha256(narration):
            failures.add("scene_narration_hash_mismatch")
        expected_duration = _duration_seconds(narration)
        if scene.get("estimated_duration_seconds") != expected_duration:
            failures.add("scene_duration_estimate_mismatch")

        mode = str(scene.get("visual_mode") or "")
        if mode == "GENERATED_CINEMATIC":
            if expected_duration > I5_GENERATED_CINEMATIC_MAX_SCENE_SECONDS:
                failures.add("generated_cinematic_scene_is_not_short")
            if (
                scene.get("factual_overlay") is not None
                or section_id not in _GENERATED_ELIGIBLE_SECTIONS
            ):
                failures.add("generated_cinematic_used_as_evidence")
        elif not (
            I5_ORDINARY_SCENE_MIN_SECONDS
            - I5_SCENE_DURATION_BOUNDARY_TOLERANCE_SECONDS
            <= expected_duration
            <= I5_ORDINARY_SCENE_MAX_SECONDS
            + I5_SCENE_DURATION_BOUNDARY_TOLERANCE_SECONDS
        ):
            failures.add("ordinary_scene_duration_out_of_bounds")

        if not str(scene.get("visual_purpose") or "").strip():
            failures.add("scene_missing_visual_purpose")
        fallbacks = scene.get("fallback_fulfillments")
        if not isinstance(fallbacks, list) or not fallbacks:
            failures.add("scene_missing_fallback")
        primary = scene.get("primary_fulfillment")
        if not isinstance(primary, Mapping) or primary.get("visual_mode") != mode:
            failures.add("scene_primary_fulfillment_mismatch")

        expected_claim_hashes = _section_claim_hashes(sections_by_id[section_id])
        actual_claim_hashes = tuple(str(value) for value in scene.get("claim_hashes", []))
        if actual_claim_hashes != expected_claim_hashes:
            failures.add("scene_claim_lineage_mismatch")
        expected_source_keys = _claim_source_keys(expected_claim_hashes, claim_index)
        actual_source_keys = tuple(str(value) for value in scene.get("source_keys", []))
        if actual_source_keys != expected_source_keys:
            failures.add("scene_source_lineage_mismatch")

        rights_basis = scene.get("rights_basis")
        if not isinstance(rights_basis, Mapping):
            failures.add("scene_rights_basis_missing")
        else:
            rights_by_key = _rights_status_by_key(scene)
            expected_rights = {
                key: str(source_index[key].get("rights_status") or "reference_only")
                for key in expected_source_keys
            }
            if rights_by_key != expected_rights:
                failures.add("scene_source_rights_lineage_mismatch")
            if mode in _SOURCE_REUSE_MODES:
                reusable = any(
                    status in _REUSABLE_RIGHTS for status in rights_by_key.values()
                )
                if not reusable or not bool(rights_basis.get("reuses_source_media")):
                    failures.add("source_media_mode_lacks_reuse_rights")
            elif bool(rights_basis.get("reuses_source_media")):
                failures.add("non_source_mode_cannot_reuse_source_media")

        disclosure = scene.get("disclosure_state")
        if mode == "UI_RECONSTRUCTION" and disclosure != (
            "visible_reconstruction_label"
        ):
            failures.add("ui_reconstruction_missing_visible_disclosure")
        if mode == "CODE" and disclosure != "illustrative_pseudocode":
            failures.add("code_visual_missing_illustrative_disclosure")
        if mode == "GENERATED_CINEMATIC" and disclosure != (
            "illustrative_generated_media"
        ):
            failures.add("generated_media_missing_illustrative_disclosure")

        overlay = scene.get("factual_overlay")
        if overlay is not None:
            if not isinstance(overlay, Mapping):
                failures.add("factual_overlay_invalid")
            else:
                overlay_hashes = tuple(
                    str(value) for value in overlay.get("claim_hashes", [])
                )
                if not overlay_hashes or any(
                    claim_hash not in actual_claim_hashes
                    or claim_hash not in claim_index
                    or claim_index[claim_hash].get("state") != "VERIFIED"
                    or claim_index[claim_hash].get("claim_type")
                    not in {"fact", "attributed_claim"}
                    for claim_hash in overlay_hashes
                ):
                    failures.add("factual_overlay_lacks_verified_claim_lineage")
                overlay_source_keys = tuple(
                    str(value) for value in overlay.get("source_keys", [])
                )
                expected_overlay_sources = _claim_source_keys(
                    overlay_hashes,
                    claim_index,
                ) if all(value in claim_index for value in overlay_hashes) else ()
                if overlay_source_keys != expected_overlay_sources:
                    failures.add("factual_overlay_source_lineage_mismatch")
                if not _overlay_numeric_values_are_claim_bound(overlay, claim_index):
                    failures.add("factual_overlay_contains_unverified_numeric_value")

        overlay_mapping = overlay if isinstance(overlay, Mapping) else None
        if mode == "DATA_VISUALIZATION" and not _overlay_has_verified_numeric_data(
            overlay_mapping,
            claim_index,
        ):
            failures.add("data_visualization_lacks_verified_numeric_data")

    if set(expected_beat_index) != set(REQUIRED_SECTION_IDS):
        failures.add("not_every_i4_section_has_a_scene")
    if not _narration_reconstructs(scenes, sections):
        failures.add("scene_narration_does_not_reconstruct_i4_script")

    mode_counts = _visual_mode_counts(scenes)
    if storyboard_packet.get("visual_mode_counts") != mode_counts:
        failures.add("visual_mode_counts_mismatch")
    if len(mode_counts) < 4:
        failures.add("fewer_than_four_visual_modes")
    if _maximum_mode_run(scenes) > 2:
        failures.add("visual_mode_repeats_more_than_twice")

    generated_positions = [
        int(scene.get("position") or 0)
        for scene in scenes
        if scene.get("visual_mode") == "GENERATED_CINEMATIC"
    ]
    if any(
        right - left == 1
        for left, right in zip(generated_positions, generated_positions[1:])
    ):
        failures.add("generated_cinematic_scenes_are_adjacent")
    generated_summary = _generated_media_summary(scenes)
    if storyboard_packet.get("generated_media_summary") != generated_summary:
        failures.add("generated_media_summary_mismatch")
    if (
        int(generated_summary["generated_cinematic_scene_count"])
        > I5_MAX_GENERATED_CINEMATIC_SCENES
    ):
        failures.add("generated_cinematic_scene_limit_exceeded")
    if (
        float(generated_summary["generated_cinematic_estimated_coverage_seconds"])
        > I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS
    ):
        failures.add("generated_cinematic_coverage_limit_exceeded")
    if (
        int(generated_summary["generated_video_scene_count"])
        > I5_MAX_GENERATED_VIDEO_SCENES
    ):
        failures.add("generated_video_scene_limit_exceeded")
    if (
        float(generated_summary["generated_video_estimated_duration_seconds"])
        > I5_MAX_GENERATED_VIDEO_SECONDS
    ):
        failures.add("generated_video_duration_limit_exceeded")

    score = meaningful_visual_progression_score(scenes, script_sections=sections)
    if storyboard_packet.get("meaningful_visual_progression_score") != score:
        failures.add("meaningful_visual_progression_score_mismatch")

    raw_options = storyboard_packet.get("compilation_options")
    try:
        options = StoryboardCompilationOptions.model_validate(raw_options)
    except (TypeError, ValueError):
        failures.add("storyboard_compilation_options_invalid")
    else:
        expected_scenes = _build_scenes(
            sections=sections,
            claim_index=claim_index,
            source_index=source_index,
            generated_cinematic_enabled=options.generated_cinematic_enabled,
            generated_video_enabled=options.generated_video_enabled,
        )
        if scenes != expected_scenes:
            failures.add("storyboard_does_not_match_deterministic_compilation")

    return {
        "outcome": "FAIL" if failures else "PASS",
        "reasons": (
            sorted(failures)
            if failures
            else ["storyboard_is_complete_diverse_rights_safe_and_claim_bound"]
        ),
    }


def compile_storyboard(
    *,
    campaign_id: int,
    script_packet: Mapping[str, object],
    script_hash: str,
    production_profile_hash: str,
    generated_cinematic_enabled: bool = True,
    generated_video_enabled: bool = False,
) -> dict[str, object]:
    """Compile a canonical, provider-free I5 storyboard from one accepted I4 script."""

    if _SHA256_PATTERN.fullmatch(production_profile_hash) is None:
        raise StoryboardInputError(
            "production_profile_hash must be a full lowercase SHA-256"
        )
    sections, claim_index, source_index = _validated_script_input(
        campaign_id=campaign_id,
        script_packet=script_packet,
        script_hash=script_hash,
    )
    options = StoryboardCompilationOptions(
        generated_cinematic_enabled=generated_cinematic_enabled,
        generated_video_enabled=generated_video_enabled,
    )
    scenes = _build_scenes(
        sections=sections,
        claim_index=claim_index,
        source_index=source_index,
        generated_cinematic_enabled=generated_cinematic_enabled,
        generated_video_enabled=generated_video_enabled,
    )
    packet: dict[str, object] = {
        "campaign_id": campaign_id,
        "compilation_options": options.model_dump(mode="json"),
        "contract_version": I5_STORYBOARD_CONTRACT_VERSION,
        "generated_media_summary": _generated_media_summary(scenes),
        "meaningful_visual_progression_score": (
            meaningful_visual_progression_score(scenes, script_sections=sections)
        ),
        "narration_normalization": I5_NARRATION_NORMALIZATION,
        "policy_version": I5_PRODUCTION_POLICY_VERSION,
        "production_profile_hash": production_profile_hash,
        "scene_count": len(scenes),
        "scenes": scenes,
        "script_hash": script_hash,
        "visual_mode_counts": _visual_mode_counts(scenes),
    }
    packet["gate"] = evaluate_storyboard_gate(
        packet,
        script_packet=script_packet,
        script_hash=script_hash,
        production_profile_hash=production_profile_hash,
    )
    return StoryboardPacket.model_validate(packet).model_dump(mode="json")
