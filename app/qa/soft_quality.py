"""Pure I6-B1 soft hook-review request construction and fail-closed resolution."""

from __future__ import annotations

from collections.abc import Mapping
import re

from app.editorial.contracts import canonical_sha256

from .contracts import (
    I6_HOOK_MAX_SECONDS,
    I6_SOFT_REVIEW_CHECK_IDS,
    I6_SOFT_REVIEW_POLICY_VERSION,
    CanonicalViewerPromiseContext,
    CriticArtifact,
    CriticJudgment,
    HumanReviewFinding,
    MachineQAInput,
    MachineQAResult,
    OpeningSceneContext,
    ResolvedSoftJudgment,
    SelectedPackagingContext,
    SoftReviewContext,
    SoftReviewRequest,
    SoftReviewResolution,
    SoftReviewTarget,
    UnresolvedReviewFinding,
)
from .machine import evaluate_machine_qa


def machine_result_sha256(result: MachineQAResult) -> str:
    """Return the canonical identity of the complete base machine-QA result."""

    return result.sha256()


def _require_matching_machine_pair(
    machine_input: MachineQAInput, machine_result: MachineQAResult
) -> tuple[MachineQAResult, str, str]:
    """Re-evaluate, rather than trusting caller-supplied result lineage."""

    input_sha256 = machine_input.input_sha256()
    canonical_result = evaluate_machine_qa(machine_input)
    if machine_result != canonical_result:
        raise ValueError(
            "supplied machine QA result does not match canonical deterministic evaluation"
        )
    if canonical_result.input_sha256 != input_sha256:
        raise ValueError("canonical machine QA result does not bind the supplied input")
    return canonical_result, input_sha256, machine_result_sha256(canonical_result)


def _is_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _target_payload(
    *,
    machine_input_sha256: str,
    machine_result_sha256: str,
    review: HumanReviewFinding,
) -> dict[str, object]:
    return {
        "check_id": review.check_id,
        "machine_input_sha256": machine_input_sha256,
        "machine_result_sha256": machine_result_sha256,
        "original_evidence": list(review.evidence),
        "original_message": review.message,
        "policy_version": I6_SOFT_REVIEW_POLICY_VERSION,
        "scope": "hook",
    }


def _base_review_findings(machine_result: MachineQAResult) -> tuple[HumanReviewFinding, ...]:
    structured = (*machine_result.hook.review_findings, *machine_result.packaging.review_findings)
    if machine_result.review_findings != structured:
        raise ValueError("machine result review findings do not match structured results")
    return structured


def _targets_from_canonical_result(
    machine_input_sha256: str, machine_result: MachineQAResult, machine_result_sha: str
) -> tuple[SoftReviewTarget, ...]:
    _base_review_findings(machine_result)
    reviews = tuple(
        review
        for review in machine_result.hook.review_findings
        if review.check_id in I6_SOFT_REVIEW_CHECK_IDS
    )
    review_ids = [review.check_id for review in reviews]
    if len(review_ids) != len(set(review_ids)):
        raise ValueError("machine hook result contains duplicate soft-review targets")
    return tuple(
        SoftReviewTarget(
            target_id=canonical_sha256(
                _target_payload(
                    machine_input_sha256=machine_input_sha256,
                    machine_result_sha256=machine_result_sha,
                    review=review,
                )
            ),
            machine_input_sha256=machine_input_sha256,
            machine_result_sha256=machine_result_sha,
            check_id=review.check_id,
            original_message=review.message,
            original_evidence=review.evidence,
        )
        for review in reviews
    )


def extract_soft_review_targets(
    machine_input: MachineQAInput, machine_result: MachineQAResult
) -> tuple[SoftReviewTarget, ...]:
    """Extract only policy-authorized targets from an independently re-proven result."""

    canonical_result, input_sha256, result_sha256 = _require_matching_machine_pair(
        machine_input, machine_result
    )
    return _targets_from_canonical_result(input_sha256, canonical_result, result_sha256)


def _canonical_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("canonical hook text must be a string")
    return value


def _canonical_context(machine_input: MachineQAInput) -> SoftReviewContext:
    topic = machine_input.lineage.topic.payload
    script = machine_input.lineage.script.payload
    storyboard = machine_input.lineage.storyboard.payload
    assembly = machine_input.lineage.assembly.payload
    if not all(isinstance(value, Mapping) for value in (topic, script, storyboard, assembly)):
        raise ValueError("canonical I6 payloads required for soft review context")
    try:
        viewer_promise = CanonicalViewerPromiseContext.model_validate(topic["viewer_promise"])
        cold_open = next(
            dict(section)
            for section in script["sections"]
            if isinstance(section, Mapping) and section.get("section_id") == "cold_open"
        )
        selected = next(
            concept
            for concept in machine_input.packaging.concepts
            if concept.concept_id == machine_input.packaging.selected_concept_id
        )
        scenes = sorted(
            (dict(scene) for scene in storyboard["scenes"] if isinstance(scene, Mapping)),
            key=lambda scene: int(scene["position"]),
        )
        segments = sorted(
            (
                dict(segment)
                for segment in assembly["ordered_scene_segments"]
                if isinstance(segment, Mapping)
            ),
            key=lambda segment: int(segment["scene_position"]),
        )
        if len(scenes) != len(segments) or not scenes:
            raise ValueError("canonical opening scene timing is incomplete")
        elapsed = 0.0
        opening: list[OpeningSceneContext] = []
        for expected_position, (scene, segment) in enumerate(zip(scenes, segments)):
            if int(scene["position"]) != expected_position or int(segment["scene_position"]) != expected_position:
                raise ValueError("canonical opening scenes are not ordered")
            duration = float(segment["observed_duration_seconds"])
            narration = scene["narration"]
            if duration <= 0 or not isinstance(narration, str):
                raise ValueError("canonical observed scene duration must be positive")
            start, end = elapsed, elapsed + duration
            if start >= I6_HOOK_MAX_SECONDS:
                break
            opening.append(
                OpeningSceneContext(
                    position=expected_position,
                    narration=narration,
                    observed_start_seconds=start,
                    observed_end_seconds=end,
                )
            )
            elapsed = end
        return SoftReviewContext(
            campaign_id=machine_input.lineage.campaign_id,
            selected_packaging_concept=SelectedPackagingContext(
                concept_id=selected.concept_id,
                title=selected.title,
                thumbnail_concept=selected.thumbnail_concept,
                thumbnail_text=selected.thumbnail_text,
                viewer_trigger=selected.viewer_trigger,
                promise=selected.promise,
            ),
            viewer_promise=viewer_promise,
            cold_open_narration=_canonical_text(cold_open["narration"]),
            continuation_reason=_canonical_text(cold_open["continuation_reason"]),
            opening_scenes=tuple(opening),
            review_boundary_seconds=I6_HOOK_MAX_SECONDS,
        )
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise ValueError("canonical I6 hook-review context is unavailable") from exc


def _build_request_from_canonical_result(
    machine_input: MachineQAInput, machine_result: MachineQAResult, input_sha256: str, result_sha256: str
) -> SoftReviewRequest:
    targets = _targets_from_canonical_result(input_sha256, machine_result, result_sha256)
    if not targets:
        raise ValueError("machine result contains no I6-B1 authorized soft-review targets")
    return SoftReviewRequest(
        machine_input_sha256=input_sha256,
        machine_result_sha256=result_sha256,
        context=_canonical_context(machine_input),
        targets=targets,
    )


def build_soft_review_request(
    machine_input: MachineQAInput, machine_result: MachineQAResult
) -> SoftReviewRequest:
    """Build an immutable request from canonical I6 input and re-proven structured reviews."""

    canonical_result, input_sha256, result_sha256 = _require_matching_machine_pair(
        machine_input, machine_result
    )
    return _build_request_from_canonical_result(
        machine_input, canonical_result, input_sha256, result_sha256
    )


def _validate_request(
    machine_input: MachineQAInput,
    machine_result: MachineQAResult,
    request: SoftReviewRequest,
) -> tuple[MachineQAResult, SoftReviewRequest]:
    canonical_result, input_sha256, result_sha256 = _require_matching_machine_pair(
        machine_input, machine_result
    )
    expected = _build_request_from_canonical_result(
        machine_input, canonical_result, input_sha256, result_sha256
    )
    if request != expected:
        raise ValueError("soft review request does not exactly match canonical machine input and result")
    return canonical_result, expected


def _validate_artifact(request: SoftReviewRequest, artifact: CriticArtifact) -> dict[str, CriticJudgment]:
    if artifact.policy_version != I6_SOFT_REVIEW_POLICY_VERSION:
        raise ValueError("critic artifact policy mismatch")
    if any(
        not _is_sha256(value)
        for value in (
            artifact.request_sha256,
            artifact.machine_input_sha256,
            artifact.machine_result_sha256,
            artifact.provider_response_sha256,
        )
    ):
        raise ValueError("critic artifact contains an invalid hash")
    if artifact.request_sha256 != request.sha256():
        raise ValueError("critic artifact request hash is stale")
    if artifact.machine_input_sha256 != request.machine_input_sha256:
        raise ValueError("critic artifact machine input hash is stale")
    if artifact.machine_result_sha256 != request.machine_result_sha256:
        raise ValueError("critic artifact machine result hash is stale")
    if artifact.provider_response_sha256 != artifact.provider_response.sha256():
        raise ValueError("provider_response_sha256 does not bind the parsed typed critic response")
    if artifact.provider_response.judgments != artifact.judgments:
        raise ValueError("provider_response does not match critic artifact judgments")
    if not all(value.strip() for value in (artifact.provider, artifact.model, artifact.prompt_template_version)):
        raise ValueError("critic artifact provider metadata must be nonblank")
    target_ids = {target.target_id for target in request.targets}
    judgments_by_target: dict[str, CriticJudgment] = {}
    for judgment in artifact.judgments:
        if judgment.target_id not in target_ids:
            raise ValueError("critic artifact has an unknown target")
        if judgment.target_id in judgments_by_target:
            raise ValueError("critic artifact has duplicate target judgments")
        judgments_by_target[judgment.target_id] = judgment
    if set(judgments_by_target) != target_ids:
        raise ValueError("critic artifact is missing a requested target judgment")
    return judgments_by_target


def resolve_soft_review(
    machine_input: MachineQAInput,
    machine_result: MachineQAResult,
    request: SoftReviewRequest,
    artifact: CriticArtifact,
) -> SoftReviewResolution:
    """Resolve only exact B1 targets after re-proving input, result, and request identity."""

    canonical_result, canonical_request = _validate_request(
        machine_input, machine_result, request
    )
    judgments_by_target = _validate_artifact(canonical_request, artifact)
    target_by_check = {target.check_id: target for target in canonical_request.targets}
    resolved = tuple(
        ResolvedSoftJudgment(
            target_id=target.target_id,
            check_id=target.check_id,
            outcome=judgments_by_target[target.target_id].outcome,
            rationale=judgments_by_target[target.target_id].rationale,
            observations=judgments_by_target[target.target_id].observations,
        )
        for target in canonical_request.targets
    )
    unresolved: list[UnresolvedReviewFinding] = [
        UnresolvedReviewFinding(
            finding_kind="machine",
            check_id=finding.check_id,
            message=finding.message,
            evidence=finding.evidence,
        )
        for finding in canonical_result.findings
        if finding.outcome == "NEEDS_HUMAN"
    ]
    for review in canonical_result.hook.review_findings:
        target = target_by_check.get(review.check_id)
        judgment = judgments_by_target.get(target.target_id) if target else None
        if not target or judgment is None or judgment.outcome != "PASS":
            unresolved.append(
                UnresolvedReviewFinding(
                    finding_kind="human",
                    check_id=review.check_id,
                    message=review.message,
                    evidence=review.evidence,
                )
            )
    unresolved.extend(
        UnresolvedReviewFinding(
            finding_kind="human",
            check_id=review.check_id,
            message=review.message,
            evidence=review.evidence,
        )
        for review in canonical_result.packaging.review_findings
    )
    base_has_failure = canonical_result.outcome == "FAIL" or any(
        finding.outcome == "FAIL" for finding in canonical_result.findings
    )
    soft_has_failure = any(judgment.outcome == "FAIL" for judgment in resolved)
    outcome = "FAIL" if base_has_failure or soft_has_failure else "NEEDS_HUMAN" if unresolved else "PASS"
    return SoftReviewResolution(
        base_machine_result_sha256=machine_result_sha256(canonical_result),
        critic_artifact_sha256=artifact.sha256(),
        original_findings=canonical_result.findings,
        original_review_findings=canonical_result.review_findings,
        resolved_soft_judgments=resolved,
        remaining_unresolved_review_findings=tuple(unresolved),
        outcome=outcome,
    )
