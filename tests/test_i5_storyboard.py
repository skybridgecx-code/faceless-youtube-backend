from __future__ import annotations

import hashlib
from collections import Counter
from copy import deepcopy

import pytest

from app.editorial.contracts import canonical_sha256
from app.production.contracts import (
    I5_GENERATED_CINEMATIC_MAX_SCENE_SECONDS,
    I5_MAX_GENERATED_CINEMATIC_SCENES,
    I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS,
    I5_MAX_GENERATED_VIDEO_SCENES,
    I5_MAX_GENERATED_VIDEO_SECONDS,
    I5_MAX_SCENES,
    I5_MIN_SCENES,
    I5_ORDINARY_SCENE_MAX_SECONDS,
    I5_ORDINARY_SCENE_MIN_SECONDS,
    I5_SCENE_DURATION_BOUNDARY_TOLERANCE_SECONDS,
    I5_TARGET_MAX_SCENES,
    I5_TARGET_MIN_SCENES,
)
from app.production.storyboard import (
    StoryboardInputError,
    compile_storyboard,
    evaluate_storyboard_gate,
    narration_sha256,
    normalize_narration,
    split_section_narration,
)
from tests.i5_test_data import (
    I5_TEST_CAMPAIGN_ID,
    I5_TEST_PRODUCTION_PROFILE_HASH,
    happy_i4_script_packet,
    normalized_i4_narration,
    numeric_i4_script_packet,
)


@pytest.fixture(scope="module")
def script_packet() -> dict[str, object]:
    return happy_i4_script_packet()


@pytest.fixture(scope="module")
def storyboard_packet(script_packet: dict[str, object]) -> dict[str, object]:
    return compile_storyboard(
        campaign_id=I5_TEST_CAMPAIGN_ID,
        script_packet=script_packet,
        script_hash=canonical_sha256(script_packet),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
    )


def _scenes(packet: dict[str, object]) -> list[dict[str, object]]:
    return [dict(scene) for scene in packet["scenes"]]  # type: ignore[index]


def _gate(
    packet: dict[str, object],
    script_packet: dict[str, object],
) -> dict[str, object]:
    return evaluate_storyboard_gate(
        packet,
        script_packet=script_packet,
        script_hash=canonical_sha256(script_packet),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
    )


def test_compile_storyboard_is_deterministic_bounded_and_passes(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    second = compile_storyboard(
        campaign_id=I5_TEST_CAMPAIGN_ID,
        script_packet=script_packet,
        script_hash=canonical_sha256(script_packet),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
    )

    assert storyboard_packet == second
    assert storyboard_packet["gate"] == {
        "outcome": "PASS",
        "reasons": ["storyboard_is_complete_diverse_rights_safe_and_claim_bound"],
    }
    assert I5_MIN_SCENES <= storyboard_packet["scene_count"] <= I5_MAX_SCENES  # type: ignore[operator]
    assert I5_TARGET_MIN_SCENES <= storyboard_packet["scene_count"] <= I5_TARGET_MAX_SCENES  # type: ignore[operator]


def test_scene_narration_reconstructs_the_exact_normalized_i4_script(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    reconstructed = " ".join(
        normalize_narration(scene["narration"])
        for scene in _scenes(storyboard_packet)
    )

    assert reconstructed == normalized_i4_narration(script_packet)


def test_each_section_is_split_only_at_deterministic_sentence_boundaries(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    scenes = _scenes(storyboard_packet)
    for section in script_packet["sections"]:  # type: ignore[index]
        section_id = section["section_id"]
        section_scenes = [scene for scene in scenes if scene["section_id"] == section_id]
        assert [scene["beat_index"] for scene in section_scenes] == list(
            range(len(section_scenes))
        )
        assert tuple(scene["narration"] for scene in section_scenes) == (
            split_section_narration(str(section["narration"]))
        )
        assert " ".join(str(scene["narration"]) for scene in section_scenes) == (
            normalize_narration(section["narration"])
        )


def test_scene_hashes_and_planning_durations_are_exact(
    storyboard_packet: dict[str, object],
) -> None:
    for scene in _scenes(storyboard_packet):
        narration = str(scene["narration"])
        expected_hash = hashlib.sha256(narration.encode("utf-8")).hexdigest()
        assert scene["narration_sha256"] == expected_hash == narration_sha256(narration)
        duration = float(scene["estimated_duration_seconds"])
        if scene["visual_mode"] == "GENERATED_CINEMATIC":
            assert duration <= I5_GENERATED_CINEMATIC_MAX_SCENE_SECONDS
        else:
            assert (
                I5_ORDINARY_SCENE_MIN_SECONDS
                - I5_SCENE_DURATION_BOUNDARY_TOLERANCE_SECONDS
                <= duration
                <= I5_ORDINARY_SCENE_MAX_SECONDS
                + I5_SCENE_DURATION_BOUNDARY_TOLERANCE_SECONDS
            )


def test_every_scene_has_the_full_contract_purpose_and_fallback(
    storyboard_packet: dict[str, object],
) -> None:
    required = {
        "position",
        "section_id",
        "beat_index",
        "narration",
        "narration_sha256",
        "claim_hashes",
        "estimated_duration_seconds",
        "visual_mode",
        "visual_purpose",
        "factual_overlay",
        "source_keys",
        "rights_basis",
        "disclosure_state",
        "primary_fulfillment",
        "fallback_fulfillments",
        "continuation_reason",
        "payoff",
    }
    for scene in _scenes(storyboard_packet):
        assert set(scene) == required
        assert str(scene["visual_purpose"]).strip()
        assert scene["fallback_fulfillments"]
        assert str(scene["continuation_reason"]).strip()
        assert str(scene["payoff"]).strip()


def test_scene_claim_and_source_lineage_matches_its_i4_section(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    sections = {
        section["section_id"]: section
        for section in script_packet["sections"]  # type: ignore[index]
    }
    claims = script_packet["claim_index"]  # type: ignore[assignment]
    for scene in _scenes(storyboard_packet):
        expected_hashes = sections[scene["section_id"]]["claim_hashes"]
        expected_sources = sorted(
            {
                source_key
                for claim_hash in expected_hashes
                for source_key in claims[claim_hash]["source_keys"]
            }
        )
        assert scene["claim_hashes"] == expected_hashes
        assert scene["source_keys"] == expected_sources
        overlay = scene["factual_overlay"]
        if overlay is not None:
            assert set(overlay["claim_hashes"]) <= set(scene["claim_hashes"])
            assert set(overlay["source_keys"]) <= set(scene["source_keys"])


def test_visual_modes_are_diverse_and_never_repeat_more_than_twice(
    storyboard_packet: dict[str, object],
) -> None:
    modes = [str(scene["visual_mode"]) for scene in _scenes(storyboard_packet)]
    assert len(set(modes)) >= 4
    assert storyboard_packet["visual_mode_counts"] == dict(sorted(Counter(modes).items()))
    assert all(
        not (modes[index] == modes[index + 1] == modes[index + 2])
        for index in range(len(modes) - 2)
    )


def test_generated_cinematic_plan_is_small_short_and_non_adjacent(
    storyboard_packet: dict[str, object],
) -> None:
    scenes = _scenes(storyboard_packet)
    generated = [
        scene for scene in scenes if scene["visual_mode"] == "GENERATED_CINEMATIC"
    ]
    summary = storyboard_packet["generated_media_summary"]

    assert len(generated) <= I5_MAX_GENERATED_CINEMATIC_SCENES
    assert all(
        float(scene["estimated_duration_seconds"])
        <= I5_GENERATED_CINEMATIC_MAX_SCENE_SECONDS
        for scene in generated
    )
    assert all(
        scene["factual_overlay"] is None
        and scene["section_id"]
        in {"cold_open", "setup", "consequences", "resolution", "what_next"}
        for scene in generated
    )
    assert all(
        right["position"] - left["position"] > 1
        for left, right in zip(generated, generated[1:])
    )
    assert summary["generated_cinematic_estimated_coverage_seconds"] <= I5_MAX_GENERATED_MEDIA_COVERAGE_SECONDS
    assert summary["generated_video_scene_count"] <= I5_MAX_GENERATED_VIDEO_SCENES
    assert summary["generated_video_estimated_duration_seconds"] <= I5_MAX_GENERATED_VIDEO_SECONDS
    assert summary["generated_video_scene_count"] == 0
    assert all(
        scene["primary_fulfillment"]["strategy"] == "generated_image_primary"
        and len(scene["fallback_fulfillments"]) == 2
        for scene in generated
    )


def test_generated_cinematic_can_be_disabled_without_weakening_the_gate(
    script_packet: dict[str, object],
) -> None:
    packet = compile_storyboard(
        campaign_id=I5_TEST_CAMPAIGN_ID,
        script_packet=script_packet,
        script_hash=canonical_sha256(script_packet),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
        generated_cinematic_enabled=False,
    )

    assert packet["gate"]["outcome"] == "PASS"  # type: ignore[index]
    assert packet["generated_media_summary"] == {
        "generated_cinematic_scene_count": 0,
        "generated_cinematic_estimated_coverage_seconds": 0.0,
        "generated_video_scene_count": 0,
        "generated_video_estimated_duration_seconds": 0.0,
    }


def test_generated_video_requires_explicit_opt_in_and_has_exact_fallback_chain(
    script_packet: dict[str, object],
) -> None:
    default_packet = compile_storyboard(
        campaign_id=I5_TEST_CAMPAIGN_ID,
        script_packet=script_packet,
        script_hash=canonical_sha256(script_packet),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
    )
    enabled_packet = compile_storyboard(
        campaign_id=I5_TEST_CAMPAIGN_ID,
        script_packet=script_packet,
        script_hash=canonical_sha256(script_packet),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
        generated_video_enabled=True,
    )

    assert default_packet["compilation_options"]["generated_video_enabled"] is False  # type: ignore[index]
    assert default_packet["generated_media_summary"]["generated_video_scene_count"] == 0  # type: ignore[index]
    enabled = [
        scene
        for scene in _scenes(enabled_packet)
        if scene["primary_fulfillment"]["strategy"] == "generated_video_primary"  # type: ignore[index]
    ]
    summary = enabled_packet["generated_media_summary"]
    assert enabled_packet["gate"]["outcome"] == "PASS"  # type: ignore[index]
    assert 1 <= len(enabled) <= I5_MAX_GENERATED_VIDEO_SCENES
    assert summary["generated_video_scene_count"] == len(enabled)  # type: ignore[index]
    assert summary["generated_video_estimated_duration_seconds"] == len(enabled) * 8  # type: ignore[index]
    assert summary["generated_video_estimated_duration_seconds"] <= I5_MAX_GENERATED_VIDEO_SECONDS  # type: ignore[index]
    for scene in enabled:
        assert scene["primary_fulfillment"] == {
            "generated_video_seconds": 8.0,
            "strategy": "generated_video_primary",
            "visual_mode": "GENERATED_CINEMATIC",
        }
        assert [
            item["strategy"] for item in scene["fallback_fulfillments"]  # type: ignore[index]
        ] == [
            "generated_image_primary",
            "generated_image_fallback",
            "local_deterministic",
        ]


def test_reference_only_sources_never_authorize_source_media_reuse(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    assert all(
        scene["visual_mode"]
        not in {"REAL_SOURCE_MEDIA", "PRODUCT_FOOTAGE", "SCREENSHOT"}
        for scene in _scenes(storyboard_packet)
    )
    assert all(
        not scene["rights_basis"]["reuses_source_media"]
        for scene in _scenes(storyboard_packet)
    )

    mutated = deepcopy(storyboard_packet)
    mutated["scenes"][0]["visual_mode"] = "SCREENSHOT"  # type: ignore[index]
    mutated["scenes"][0]["primary_fulfillment"]["visual_mode"] = "SCREENSHOT"  # type: ignore[index]
    gate = _gate(mutated, script_packet)

    assert gate["outcome"] == "FAIL"
    assert "source_media_mode_lacks_reuse_rights" in gate["reasons"]


def test_ui_reconstruction_always_has_visible_disclosure(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    ui_scenes = [
        scene
        for scene in _scenes(storyboard_packet)
        if scene["visual_mode"] == "UI_RECONSTRUCTION"
    ]
    assert ui_scenes
    assert all(
        scene["disclosure_state"] == "visible_reconstruction_label"
        and "visibly labeled" in str(scene["visual_purpose"])
        for scene in ui_scenes
    )

    mutated = deepcopy(storyboard_packet)
    target = next(
        scene
        for scene in mutated["scenes"]  # type: ignore[union-attr]
        if scene["visual_mode"] == "UI_RECONSTRUCTION"
    )
    target["disclosure_state"] = "not_required"
    gate = _gate(mutated, script_packet)
    assert gate["outcome"] == "FAIL"
    assert "ui_reconstruction_missing_visible_disclosure" in gate["reasons"]


def test_data_visualization_requires_actual_verified_numeric_claim_data() -> None:
    script = numeric_i4_script_packet()
    packet = compile_storyboard(
        campaign_id=I5_TEST_CAMPAIGN_ID,
        script_packet=script,
        script_hash=canonical_sha256(script),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
    )
    data_scenes = [
        scene
        for scene in _scenes(packet)
        if scene["visual_mode"] == "DATA_VISUALIZATION"
    ]

    assert packet["gate"]["outcome"] == "PASS"  # type: ignore[index]
    assert data_scenes
    assert all(
        scene["factual_overlay"] is not None
        and "42" in scene["factual_overlay"]["text"]
        and scene["factual_overlay"]["claim_hashes"]
        for scene in data_scenes
    )


def test_non_numeric_claim_cannot_be_presented_as_data_visualization(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    mutated = deepcopy(storyboard_packet)
    target = next(
        scene
        for scene in mutated["scenes"]  # type: ignore[union-attr]
        if scene["factual_overlay"] is None
    )
    target["visual_mode"] = "DATA_VISUALIZATION"
    target["primary_fulfillment"]["visual_mode"] = "DATA_VISUALIZATION"
    gate = _gate(mutated, script_packet)

    assert gate["outcome"] == "FAIL"
    assert "data_visualization_lacks_verified_numeric_data" in gate["reasons"]


def test_factual_overlay_cannot_invent_a_numeric_value() -> None:
    script = numeric_i4_script_packet()
    packet = compile_storyboard(
        campaign_id=I5_TEST_CAMPAIGN_ID,
        script_packet=script,
        script_hash=canonical_sha256(script),
        production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
    )
    mutated = deepcopy(packet)
    target = next(
        scene
        for scene in mutated["scenes"]  # type: ignore[union-attr]
        if scene["visual_mode"] == "DATA_VISUALIZATION"
    )
    target["factual_overlay"]["text"] += " Exactly 9,999 systems passed."
    gate = _gate(mutated, script)

    assert gate["outcome"] == "FAIL"
    assert "factual_overlay_contains_unverified_numeric_value" in gate["reasons"]


def test_narration_gap_fails_even_when_hash_and_duration_metadata_are_corrected(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    mutated = deepcopy(storyboard_packet)
    target = mutated["scenes"][0]  # type: ignore[index]
    target["narration"] = str(target["narration"]).replace("How ", "", 1)
    target["narration_sha256"] = narration_sha256(str(target["narration"]))
    words = len(str(target["narration"]).split())
    target["estimated_duration_seconds"] = round(words / 2.5, 2)
    gate = _gate(mutated, script_packet)

    assert gate["outcome"] == "FAIL"
    assert "scene_narration_does_not_reconstruct_i4_script" in gate["reasons"]


def test_three_consecutive_identical_modes_fail_the_hard_gate(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    mutated = deepcopy(storyboard_packet)
    for scene in mutated["scenes"][:3]:  # type: ignore[index]
        scene["visual_mode"] = "DIAGRAM"
        scene["primary_fulfillment"] = {
            "strategy": "local_deterministic",
            "visual_mode": "DIAGRAM",
            "generated_video_seconds": 0.0,
        }
        scene["disclosure_state"] = "not_required"
    gate = _gate(mutated, script_packet)

    assert gate["outcome"] == "FAIL"
    assert "visual_mode_repeats_more_than_twice" in gate["reasons"]


def test_high_progression_score_cannot_override_a_rights_failure(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    mutated = deepcopy(storyboard_packet)
    mutated["meaningful_visual_progression_score"] = 100
    target = mutated["scenes"][0]  # type: ignore[index]
    target["visual_mode"] = "REAL_SOURCE_MEDIA"
    target["primary_fulfillment"]["visual_mode"] = "REAL_SOURCE_MEDIA"
    gate = _gate(mutated, script_packet)

    assert gate["outcome"] == "FAIL"
    assert "source_media_mode_lacks_reuse_rights" in gate["reasons"]


def test_scene_count_hard_bound_cannot_be_rescored_away(
    script_packet: dict[str, object],
    storyboard_packet: dict[str, object],
) -> None:
    mutated = deepcopy(storyboard_packet)
    mutated["scenes"] = mutated["scenes"][: I5_MIN_SCENES - 1]  # type: ignore[index]
    mutated["scene_count"] = len(mutated["scenes"])
    mutated["meaningful_visual_progression_score"] = 100
    gate = _gate(mutated, script_packet)

    assert gate["outcome"] == "FAIL"
    assert "storyboard_scene_count_out_of_bounds" in gate["reasons"]


def test_script_and_profile_identity_fail_closed(
    script_packet: dict[str, object],
) -> None:
    with pytest.raises(StoryboardInputError, match="script_hash does not match"):
        compile_storyboard(
            campaign_id=I5_TEST_CAMPAIGN_ID,
            script_packet=script_packet,
            script_hash="0" * 64,
            production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
        )
    with pytest.raises(StoryboardInputError, match="production_profile_hash"):
        compile_storyboard(
            campaign_id=I5_TEST_CAMPAIGN_ID,
            script_packet=script_packet,
            script_hash=canonical_sha256(script_packet),
            production_profile_hash="not-a-hash",
        )


def test_unaccepted_i4_script_cannot_enter_storyboard_compilation(
    script_packet: dict[str, object],
) -> None:
    rejected = deepcopy(script_packet)
    rejected["gate"] = {"outcome": "FAIL", "reasons": ["test rejection"]}

    with pytest.raises(StoryboardInputError, match="accepted I4 script gate"):
        compile_storyboard(
            campaign_id=I5_TEST_CAMPAIGN_ID,
            script_packet=rejected,
            script_hash=canonical_sha256(rejected),
            production_profile_hash=I5_TEST_PRODUCTION_PROFILE_HASH,
        )
