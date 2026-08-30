"""DBOS-free semantic coverage for the pure I6-B2B2 critic contracts."""

from __future__ import annotations

import hashlib
from functools import lru_cache

import pytest

from app.qa.contracts import (
    MachineQAFinding,
    ResolvedThumbnailCriticJudgment,
    ThumbnailCriticArtifact,
    ThumbnailCriticJudgment,
    ThumbnailCriticResponse,
    revalidate_thumbnail_critic_request,
)
from app.qa.thumbnail_quality import (
    ThumbnailCriticError,
    build_rendered_thumbnail_evidence,
    build_thumbnail_critic_request,
    resolve_thumbnail_critic,
)
from tests.test_i6_thumbnail_evidence import _bound_input


@lru_cache(maxsize=1)
def _fixture_data():  # type: ignore[no-untyped-def]
    machine_input, machine_result, render_spec, sources = _bound_input()
    rendered = build_rendered_thumbnail_evidence(machine_input, machine_result, render_spec, sources)
    request = build_thumbnail_critic_request(machine_input, machine_result, rendered.evidence)
    return machine_input, machine_result, rendered, request


@pytest.fixture(scope="module")
def critic_fixture():  # type: ignore[no-untyped-def]
    return _fixture_data()


def _artifact(request, outcomes=("PASS", "PASS")):  # type: ignore[no-untyped-def]
    judgments = tuple(
        ThumbnailCriticJudgment(
            target_id=target.target_id,
            check_id=target.check_id,
            outcome=outcome,
            rationale="The visible focal relationship is adequately described.",
            visual_observations=("A single focal subject is visually prominent.",),
        )
        for target, outcome in zip(request.targets, outcomes, strict=True)
    )
    response = ThumbnailCriticResponse(judgments=judgments)  # type: ignore[arg-type]
    return ThumbnailCriticArtifact(
        request_sha256=request.sha256(),
        machine_input_sha256=request.machine_input_sha256,
        machine_result_sha256=request.machine_result_sha256,
        evidence_sha256=request.evidence_sha256,
        provider_response_id="resp_thumbnail",
        structured_response_sha256=response.sha256(),
        raw_response_sha256=hashlib.sha256(b"sanitized response").hexdigest(),
        response=response,
        judgments=judgments,  # type: ignore[arg-type]
    )


def test_17_exact_target_check_coverage(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    _, _, rendered, request = critic_fixture
    assert request.evidence_sha256 == rendered.evidence.sha256()
    assert tuple(target.check_id for target in request.targets) == (
        "thumbnail_dominant_idea", "thumbnail_hierarchy"
    )


@pytest.mark.parametrize("field,value", [
    ("concept_id", "forged-concept"),
    ("layout_spec_sha256", "0" * 64),
    ("mime_type", "image/jpeg"),
    ("width", 1279),
    ("height", 719),
])
def test_7_to_12_evidence_authority_mutations_block(critic_fixture, field, value) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, rendered, _ = critic_fixture
    with pytest.raises(ThumbnailCriticError):
        build_thumbnail_critic_request(machine_input, machine_result, rendered.evidence.model_copy(update={field: value}))


def test_13_model_copy_request_mutation_blocks(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    *_, request = critic_fixture
    with pytest.raises(ValueError):
        revalidate_thumbnail_critic_request(request.model_copy(update={"evidence_sha256": "0" * 64}))


def test_7_evidence_sha_mutation_blocks(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    *_, request = critic_fixture
    with pytest.raises(ValueError):
        revalidate_thumbnail_critic_request(request.model_copy(update={"evidence_sha256": "f" * 64}))


@pytest.mark.parametrize("field,value", [("png_sha256", "0" * 64), ("png_byte_size", 1)])
def test_10_11_target_png_identity_mutation_blocks(critic_fixture, field, value) -> None:  # type: ignore[no-untyped-def]
    *_, request = critic_fixture
    modified_target = request.targets[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        revalidate_thumbnail_critic_request(request.model_copy(update={"targets": (modified_target, request.targets[1])}))


@pytest.mark.parametrize("outcomes,expected", [
    (("PASS", "PASS"), "NEEDS_HUMAN"),
    (("FAIL", "PASS"), "FAIL"),
    (("NEEDS_HUMAN", "PASS"), "NEEDS_HUMAN"),
    (("PASS", "NEEDS_HUMAN"), "NEEDS_HUMAN"),
])
def test_23_to_29_resolution_semantics(critic_fixture, outcomes, expected) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    resolution = resolve_thumbnail_critic(
        machine_input=machine_input,
        machine_result=machine_result,
        request=request,
        artifact=_artifact(request, outcomes),
    )
    assert resolution.outcome == expected
    if "FAIL" in outcomes:
        assert any(item.check_id == "thumbnail_dominant_idea" and not item.hard_gate for item in resolution.remaining_machine_findings)
    if "NEEDS_HUMAN" in outcomes:
        assert any(item.check_id == "thumbnail_dominant_idea" for item in resolution.remaining_human_review_findings)


def test_27_unrelated_human_preservation(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    resolution = resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=_artifact(request))
    assert any(item.check_id not in {target.check_id for target in request.targets} for item in resolution.remaining_human_review_findings)


def test_28_unrelated_machine_needs_human_preservation(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    resolution = resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=_artifact(request))
    assert all(item.outcome != "PASS" for item in resolution.remaining_machine_findings)


def test_33_artifact_stable_sha(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    *_, request = critic_fixture
    assert _artifact(request).sha256() == _artifact(request).sha256()


@pytest.mark.parametrize("field,value", [
    ("request_sha256", "0" * 64),
    ("structured_response_sha256", "0" * 64),
    ("judgments", ()),
])
def test_36_to_37_artifact_mutations_block(critic_fixture, field, value) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    corrupted = _artifact(request).model_copy(update={field: value})
    with pytest.raises((ThumbnailCriticError, ValueError)):
        resolve_thumbnail_critic(
            machine_input=machine_input,
            machine_result=machine_result,
            request=request,
            artifact=corrupted,
        )


def test_54_contract_identity_changes_with_bound_content(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    *_, request = critic_fixture
    first, second = request.targets
    assert first.target_id != second.target_id
    assert request.sha256() == revalidate_thumbnail_critic_request(request).sha256()


def test_authority_rejects_extra_reordered_or_missing_human_reviews(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, rendered, _ = critic_fixture
    selected = machine_result.packaging.thumbnails[0]
    bad_reviews = (selected.review_findings[1], selected.review_findings[0])
    bad_thumbnail = selected.model_copy(update={"review_findings": bad_reviews})
    bad_packaging = machine_result.packaging.model_copy(update={"thumbnails": (bad_thumbnail, *machine_result.packaging.thumbnails[1:])})
    bad_result = machine_result.model_copy(update={"packaging": bad_packaging})
    with pytest.raises(ThumbnailCriticError):
        build_thumbnail_critic_request(machine_input, bad_result, rendered.evidence)


def test_resolution_rejects_forged_coverage(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    artifact = _artifact(request)
    first = artifact.judgments[0].model_copy(update={"check_id": "thumbnail_hierarchy"})
    response = ThumbnailCriticResponse(judgments=(first, artifact.judgments[1]))
    forged = artifact.model_copy(update={"response": response, "judgments": response.judgments, "structured_response_sha256": response.sha256()})
    with pytest.raises(ThumbnailCriticError):
        resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=forged)


def test_contract_rejects_whitespace_only_rationale(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    *_, request = critic_fixture
    with pytest.raises(ValueError, match="rationale"):
        ThumbnailCriticJudgment(
            target_id=request.targets[0].target_id,
            check_id=request.targets[0].check_id,
            outcome="PASS",
            rationale=" \t ",
            visual_observations=("visible subject",),
        )
    with pytest.raises(ValueError, match="rationale"):
        ResolvedThumbnailCriticJudgment(
            target_id=request.targets[0].target_id,
            check_id=request.targets[0].check_id,
            outcome="PASS",
            rationale="  ",
            visual_observations=("visible subject",),
        )


def test_contract_rejects_whitespace_only_visual_observation(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    *_, request = critic_fixture
    with pytest.raises(ValueError, match="visual_observations"):
        ThumbnailCriticJudgment(
            target_id=request.targets[0].target_id,
            check_id=request.targets[0].check_id,
            outcome="PASS",
            rationale="visible subject",
            visual_observations=("\n ",),
        )
    with pytest.raises(ValueError, match="visual_observations"):
        ResolvedThumbnailCriticJudgment(
            target_id=request.targets[0].target_id,
            check_id=request.targets[0].check_id,
            outcome="PASS",
            rationale="visible subject",
            visual_observations=("\n ",),
        )


def test_req_07_evidence_sha_mutation_blocks(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, rendered, _ = critic_fixture
    with pytest.raises(ThumbnailCriticError):
        build_thumbnail_critic_request(
            machine_input,
            machine_result,
            rendered.evidence.model_copy(update={"machine_result_sha256": "0" * 64}),
        )


def test_req_08_concept_mutation_blocks(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, rendered, _ = critic_fixture
    with pytest.raises(ThumbnailCriticError):
        build_thumbnail_critic_request(machine_input, machine_result, rendered.evidence.model_copy(update={"concept_id": "wrong-concept"}))


def test_req_09_layout_mutation_blocks(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, rendered, _ = critic_fixture
    with pytest.raises(ThumbnailCriticError):
        build_thumbnail_critic_request(machine_input, machine_result, rendered.evidence.model_copy(update={"layout_spec_sha256": "0" * 64}))


def test_req_17_exact_target_check_coverage(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    *_, request = critic_fixture
    assert tuple((target.target_id, target.check_id) for target in request.targets) == tuple(
        (target.target_id, check_id)
        for target, check_id in zip(request.targets, ("thumbnail_dominant_idea", "thumbnail_hierarchy"), strict=True)
    )


def test_req_23_pass_resolution(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    resolution = resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=_artifact(request))
    assert resolution.outcome == "NEEDS_HUMAN"
    for target in request.targets:
        assert sum(finding == target.original_human_finding for finding in resolution.remaining_human_review_findings) == 2


def test_req_24_fail_soft_resolution(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    resolution = resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=_artifact(request, ("FAIL", "PASS")))
    finding = next(item for item in resolution.remaining_machine_findings if item.check_id == "thumbnail_dominant_idea")
    assert resolution.outcome == "FAIL" and finding.hard_gate is False


def test_req_25_needs_human_exact_preservation(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    resolution = resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=_artifact(request, ("NEEDS_HUMAN", "PASS")))
    assert request.targets[0].original_human_finding in resolution.remaining_human_review_findings


def test_req_26_mixed_pass_needs_human(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    resolution = resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=_artifact(request, ("PASS", "NEEDS_HUMAN")))
    assert resolution.outcome == "NEEDS_HUMAN"
    assert request.targets[1].original_human_finding in resolution.remaining_human_review_findings


def test_req_27_unrelated_human_preservation(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    resolution = resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=_artifact(request))
    authorized = {target.original_human_finding for target in request.targets}
    assert any(finding not in authorized for finding in resolution.remaining_human_review_findings)


def test_req_28_exact_unrelated_machine_needs_human_preservation(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, _, render_spec, sources = _bound_input()
    concept = machine_input.packaging.concepts[0].model_copy(update={"thumbnail_concept": "semantic visual relationship"})
    revised_input = machine_input.model_copy(update={"packaging": machine_input.packaging.model_copy(update={"concepts": (concept, *machine_input.packaging.concepts[1:])})})
    revised_result = __import__("app.qa.machine", fromlist=["evaluate_machine_qa"]).evaluate_machine_qa(revised_input)
    assert revised_result.outcome == "NEEDS_HUMAN"
    rendered = build_rendered_thumbnail_evidence(revised_input, revised_result, render_spec, sources)
    request = build_thumbnail_critic_request(revised_input, revised_result, rendered.evidence)
    expected = next(
        finding
        for finding in revised_result.findings
        if finding.check_id == "hook_package_fulfillment"
        and finding.outcome == "NEEDS_HUMAN"
    )
    resolution = resolve_thumbnail_critic(machine_input=revised_input, machine_result=revised_result, request=request, artifact=_artifact(request))
    expected_prefix = tuple(
        finding for finding in revised_result.findings if finding.outcome != "PASS"
    )
    assert resolution.remaining_machine_findings == expected_prefix
    assert resolution.remaining_machine_findings.count(expected) == 1


def test_unrelated_machine_needs_human_preserved_exactly_once() -> None:
    machine_input, _, render_spec, sources = _bound_input()
    concept = machine_input.packaging.concepts[0].model_copy(
        update={"thumbnail_concept": "semantic visual relationship"}
    )
    revised_input = machine_input.model_copy(
        update={
            "packaging": machine_input.packaging.model_copy(
                update={"concepts": (concept, *machine_input.packaging.concepts[1:])}
            )
        }
    )
    revised_result = __import__(
        "app.qa.machine", fromlist=["evaluate_machine_qa"]
    ).evaluate_machine_qa(revised_input)
    rendered = build_rendered_thumbnail_evidence(
        revised_input, revised_result, render_spec, sources
    )
    request = build_thumbnail_critic_request(
        revised_input, revised_result, rendered.evidence
    )
    resolution = resolve_thumbnail_critic(
        machine_input=revised_input,
        machine_result=revised_result,
        request=request,
        artifact=_artifact(request),
    )
    canonical = tuple(
        finding for finding in revised_result.findings if finding.outcome != "PASS"
    )
    unrelated = next(
        finding
        for finding in canonical
        if finding.check_id == "hook_package_fulfillment"
        and finding.outcome == "NEEDS_HUMAN"
    )
    assert resolution.remaining_machine_findings == canonical
    assert resolution.remaining_machine_findings.count(unrelated) == 1


def test_req_29_deterministic_hard_failure_dominance_no_downgrade(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    hard = MachineQAFinding(check_id="unrelated_hard", outcome="FAIL", hard_gate=True, message="deterministic failure")
    failed = machine_result.model_copy(update={"findings": (*machine_result.findings, hard), "outcome": "FAIL"})
    with pytest.raises(ThumbnailCriticError, match="stale"):
        resolve_thumbnail_critic(machine_input=machine_input, machine_result=failed, request=request, artifact=_artifact(request))


def test_req_37_artifact_runtime_mutation_suite(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    artifact = _artifact(request)
    response = ThumbnailCriticResponse(judgments=artifact.judgments)
    for forged in (
        artifact.model_copy(update={"request_sha256": "0" * 64}),
        artifact.model_copy(update={"structured_response_sha256": "0" * 64}),
        artifact.model_copy(update={"response": response.model_copy(update={"judgments": tuple(reversed(response.judgments))})}),
        artifact.model_copy(update={"judgments": tuple(reversed(artifact.judgments))}),
    ):
        with pytest.raises((ThumbnailCriticError, ValueError)):
            resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=forged)
    with pytest.raises(ValueError, match="extra"):
        ThumbnailCriticArtifact.model_validate({**artifact.model_dump(mode="python"), "forged_extra": True})


def test_req_54_stable_mutation_sensitive_contract_identities(critic_fixture) -> None:  # type: ignore[no-untyped-def]
    machine_input, machine_result, _, request = critic_fixture
    artifact = _artifact(request)
    response = artifact.response
    resolution = resolve_thumbnail_critic(machine_input=machine_input, machine_result=machine_result, request=request, artifact=artifact)
    target = request.targets[0]
    assert target.sha256() == type(target).model_validate(target.model_dump(mode="python")).sha256()
    assert request.sha256() == revalidate_thumbnail_critic_request(request).sha256()
    assert response.sha256() == ThumbnailCriticResponse.model_validate(response.model_dump(mode="python")).sha256()
    assert artifact.sha256() == ThumbnailCriticArtifact.model_validate(artifact.model_dump(mode="python")).sha256()
    assert resolution.sha256() == type(resolution).model_validate(resolution.model_dump(mode="python")).sha256()
    changed = ThumbnailCriticJudgment(
        target_id=response.judgments[0].target_id,
        check_id=response.judgments[0].check_id,
        outcome="FAIL",
        rationale=response.judgments[0].rationale,
        visual_observations=response.judgments[0].visual_observations,
    )
    assert ThumbnailCriticResponse(judgments=(changed, response.judgments[1])).sha256() != response.sha256()
    assert target.model_copy(update={"target_id": "0" * 64}).sha256() != target.sha256()
    assert artifact.model_copy(update={"provider_response_id": "resp_changed"}).sha256() != artifact.sha256()
