from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.qa import (
    CriticArtifact,
    CriticJudgment,
    CriticResponse,
    HumanReviewFinding,
    build_soft_review_request,
    evaluate_machine_qa,
    extract_soft_review_targets,
    resolve_soft_review,
)
from tests.test_i6_machine_qa import _input


def _request_and_result():
    value = _input()
    result = evaluate_machine_qa(value)
    return value, result, build_soft_review_request(value, result)


def _artifact(request, *, judgments: tuple[CriticJudgment, ...] | None = None) -> CriticArtifact:
    judgments = judgments or tuple(
        CriticJudgment(
            target_id=target.target_id,
            outcome="PASS",
            rationale="Bounded editorial review.",
            observations=("canonical opening context reviewed",),
        )
        for target in request.targets
    )
    response = CriticResponse(judgments=judgments)
    return CriticArtifact(
        request_sha256=request.sha256(),
        machine_input_sha256=request.machine_input_sha256,
        machine_result_sha256=request.machine_result_sha256,
        provider="future_trusted_critic",
        model="future-model-v1",
        prompt_template_version="i6-soft-hook-v1",
        provider_response_sha256=response.sha256(),
        provider_response=response,
        judgments=judgments,
    )


def _tampered_context_request(request, **updates: object):
    return request.model_copy(update={"context": request.context.model_copy(update=updates)})


def test_extracts_exactly_four_authorized_structured_hook_reviews() -> None:
    value, result, _request = _request_and_result()
    targets = extract_soft_review_targets(value, result)
    assert {target.check_id for target in targets} == {
        "hook_unnecessary_introduction",
        "hook_information_density",
        "hook_continuation_reason",
        "hook_avoidable_length",
    }


def test_forged_machine_result_with_matching_input_hash_is_rejected_at_every_boundary() -> None:
    value, result, request = _request_and_result()
    forged = result.model_copy(update={"outcome": "PASS"})
    with pytest.raises(ValueError, match="canonical deterministic"):
        extract_soft_review_targets(value, forged)
    with pytest.raises(ValueError, match="canonical deterministic"):
        build_soft_review_request(value, forged)
    with pytest.raises(ValueError, match="canonical deterministic"):
        resolve_soft_review(value, forged, request, _artifact(request))


def test_removed_real_deterministic_fail_and_added_or_changed_human_reviews_are_rejected() -> None:
    failed_input = _input(include_rejected_claim=True)
    failed_result = evaluate_machine_qa(failed_input)
    assert any(item.outcome == "FAIL" for item in failed_result.findings)
    stripped = failed_result.model_copy(
        update={"findings": tuple(item for item in failed_result.findings if item.outcome != "FAIL")}
    )
    with pytest.raises(ValueError, match="canonical deterministic"):
        build_soft_review_request(failed_input, stripped)

    value, result, _request = _request_and_result()
    changed_review = result.hook.review_findings[0].model_copy(update={"message": "altered"})
    changed_hook = result.hook.model_copy(
        update={"review_findings": (changed_review, *result.hook.review_findings[1:])}
    )
    changed_result = result.model_copy(
        update={
            "hook": changed_hook,
            "review_findings": (
                *changed_hook.review_findings,
                *result.packaging.review_findings,
            ),
        }
    )
    with pytest.raises(ValueError, match="canonical deterministic"):
        extract_soft_review_targets(value, changed_result)

    added_review = HumanReviewFinding(
        check_id="packaging_truth_gate", message="caller-added review"
    )
    added_hook = result.hook.model_copy(
        update={"review_findings": (*result.hook.review_findings, added_review)}
    )
    added_result = result.model_copy(
        update={
            "hook": added_hook,
            "review_findings": (*added_hook.review_findings, *result.packaging.review_findings),
        }
    )
    with pytest.raises(ValueError, match="canonical deterministic"):
        extract_soft_review_targets(value, added_result)


@pytest.mark.parametrize(
    "request_mutation",
    (
        lambda request: _tampered_context_request(
            request, cold_open_narration="fabricated cold open"
        ),
        lambda request: _tampered_context_request(
            request, continuation_reason="fabricated continuation"
        ),
        lambda request: _tampered_context_request(
            request,
            opening_scenes=tuple(
                scene.model_copy(
                    update={
                        "observed_start_seconds": scene.observed_start_seconds + 0.5,
                        "observed_end_seconds": scene.observed_end_seconds + 0.5,
                    }
                )
                for scene in request.context.opening_scenes
            ),
        ),
        lambda request: _tampered_context_request(
            request,
            viewer_promise=request.context.viewer_promise.model_copy(
                update={"viewer_promise": "fabricated Viewer Promise"}
            ),
        ),
        lambda request: _tampered_context_request(
            request,
            selected_packaging_concept=request.context.selected_packaging_concept.model_copy(
                update={"title": "fabricated package surface"}
            ),
        ),
        lambda request: _tampered_context_request(request, review_boundary_seconds=29.5),
    ),
    ids=(
        "cold_open_narration",
        "continuation_reason",
        "scene_timing",
        "viewer_promise",
        "selected_packaging_context",
        "review_boundary_seconds",
    ),
)
def test_fresh_artifact_for_any_tampered_request_context_is_rejected(request_mutation) -> None:
    value, result, request = _request_and_result()
    tampered = request_mutation(request)
    with pytest.raises(ValueError, match="does not exactly match"):
        resolve_soft_review(value, result, tampered, _artifact(tampered))


def test_exact_build_artifact_resolve_path_is_accepted_and_repeatable() -> None:
    value, result, request = _request_and_result()
    artifact = _artifact(request)
    resolved = resolve_soft_review(value, result, request, artifact)
    assert resolved.outcome == "NEEDS_HUMAN"
    assert {item.check_id for item in resolved.resolved_soft_judgments} == {
        target.check_id for target in request.targets
    }
    assert {item.check_id for item in resolved.remaining_unresolved_review_findings} == {
        "thumbnail_dominant_idea",
        "thumbnail_hierarchy",
    }
    assert resolved.sha256() == resolve_soft_review(value, result, request, artifact).sha256()


def test_real_canonical_hard_failure_and_machine_needs_human_remain_unresolved() -> None:
    failed_input = _input(include_rejected_claim=True)
    failed_result = evaluate_machine_qa(failed_input)
    failed_request = build_soft_review_request(failed_input, failed_result)
    assert failed_result.outcome == "FAIL"
    assert resolve_soft_review(
        failed_input, failed_result, failed_request, _artifact(failed_request)
    ).outcome == "FAIL"

    value = _input()
    selected = value.packaging.concepts[0].model_copy(update={"promise": "custom semantic promise"})
    needs_input = value.model_copy(
        update={"packaging": value.packaging.model_copy(update={"concepts": (selected, *value.packaging.concepts[1:])})}
    )
    needs_result = evaluate_machine_qa(needs_input)
    assert any(item.outcome == "NEEDS_HUMAN" for item in needs_result.findings)
    needs_request = build_soft_review_request(needs_input, needs_result)
    needs = resolve_soft_review(needs_input, needs_result, needs_request, _artifact(needs_request))
    assert any(item.finding_kind == "machine" for item in needs.remaining_unresolved_review_findings)
    assert needs.outcome == "NEEDS_HUMAN"


def test_non_authorized_reviews_remain_unresolved_and_cannot_be_targets() -> None:
    value, result, request = _request_and_result()
    resolved = resolve_soft_review(value, result, request, _artifact(request))
    assert {item.check_id for item in resolved.remaining_unresolved_review_findings} == {
        "thumbnail_dominant_idea",
        "thumbnail_hierarchy",
    }
    changed_target = request.targets[0].model_copy(
        update={"check_id": "thumbnail_dominant_idea"}
    )
    forged = request.model_copy(update={"targets": (changed_target, *request.targets[1:])})
    with pytest.raises(ValueError, match="does not exactly match"):
        resolve_soft_review(value, result, forged, _artifact(request))


@pytest.mark.parametrize(
    "field,mutation_value",
    (
        ("target_id", "f" * 64),
        ("outcome", "FAIL"),
        ("rationale", "changed"),
        ("observations", ("changed observation",)),
    ),
)
def test_provider_response_hash_binds_every_typed_judgment_field(
    field: str, mutation_value: object
) -> None:
    machine_input, result, request = _request_and_result()
    artifact = _artifact(request)
    changed_judgment = artifact.judgments[0].model_copy(update={field: mutation_value})
    changed_judgments = (changed_judgment, *artifact.judgments[1:])
    assert CriticResponse(judgments=artifact.judgments).sha256() != CriticResponse(
        judgments=changed_judgments
    ).sha256()
    forged = artifact.model_copy(update={"judgments": changed_judgments})
    with pytest.raises(ValueError, match="provider_response"):
        resolve_soft_review(machine_input, result, request, forged)


def test_missing_duplicate_unknown_and_stale_artifacts_fail_closed() -> None:
    value, result, request = _request_and_result()
    artifact = _artifact(request)
    missing = _artifact(request, judgments=artifact.judgments[:-1])
    duplicate = _artifact(
        request,
        judgments=(artifact.judgments[0], artifact.judgments[0], *artifact.judgments[2:]),
    )
    unknown_judgment = artifact.judgments[0].model_copy(update={"target_id": "f" * 64})
    unknown = _artifact(request, judgments=(unknown_judgment, *artifact.judgments[1:]))
    for invalid in (missing, duplicate, unknown):
        with pytest.raises(ValueError):
            resolve_soft_review(value, result, request, invalid)
    for invalid in (
        artifact.model_copy(update={"request_sha256": "f" * 64}),
        artifact.model_copy(update={"machine_input_sha256": "f" * 64}),
        artifact.model_copy(update={"machine_result_sha256": "f" * 64}),
    ):
        with pytest.raises(ValueError):
            resolve_soft_review(value, result, request, invalid)
    stale_request = request.model_copy(update={"machine_input_sha256": "f" * 64})
    with pytest.raises(ValueError, match="does not exactly match"):
        resolve_soft_review(value, result, stale_request, _artifact(stale_request))


def test_qa_layer_has_no_provider_network_or_persistence_imports() -> None:
    forbidden_roots = {
        "asyncio", "dbos", "http", "httpx", "openai", "requests", "socket",
        "sqlalchemy", "subprocess", "urllib",
    }
    for path in Path("app/qa").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not imports & forbidden_roots, path
