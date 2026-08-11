from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.project_agent_runtime.promotion as promotion_module
from tests.test_project_agent_publish import (
    BRANCH,
    _bare_sha,
    _checkpointed_run,
    _git,
    _make_sibling_commit,
)
from tools.project_agent_runtime.operation_lease import operation_lease
from tools.project_agent_runtime.promotion import (
    PromotionCheckError,
    check_promotion_readiness,
)
from tools.project_agent_runtime.publish import execute_publish
from tools.project_agent_runtime.run_manifest import run_directory


def _published(tmp_path: Path):
    control, executor, config, manifest_path, run, checkpoint, bare, base = _checkpointed_run(tmp_path)
    publish = execute_publish(control, config, run.run_id, _remote_target=str(bare))
    assert publish.remote_after == checkpoint.commit_sha
    assert _bare_sha(bare, BRANCH) == checkpoint.commit_sha
    assert _bare_sha(bare, config.canonical_branch) == base
    return control, executor, config, manifest_path, run, checkpoint, bare, base


def _descendant_commit(tmp_path: Path, source: Path, base: str, name: str) -> tuple[Path, str]:
    clone = tmp_path / name
    _git(tmp_path, "clone", "--no-hardlinks", str(source), str(clone))
    _git(clone, "config", "user.name", "Promotion Test")
    _git(clone, "config", "user.email", "promotion@example.invalid")
    _git(clone, "checkout", "-B", f"{name}-branch", base)
    (clone / f"{name}.txt").write_text(name + "\n", encoding="utf-8")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", name)
    return clone, _git(clone, "rev-parse", "HEAD")


def _worktree_fingerprint(root: Path) -> tuple[str, str, str]:
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "branch", "--show-current"),
        _git(root, "status", "--porcelain=v1", "-uall"),
    )


def test_promotion_ready_when_canonical_is_candidate_ancestor(tmp_path: Path) -> None:
    control, executor, config, _, run, checkpoint, bare, base = _published(tmp_path)
    before = _worktree_fingerprint(executor)
    canonical_before = _bare_sha(bare, config.canonical_branch)
    candidate_before = _bare_sha(bare, BRANCH)

    result = check_promotion_readiness(
        control, config, run.run_id, _remote_target=str(bare)
    )

    assert result.classification == "READY_FAST_FORWARD"
    assert result.fast_forward_possible
    assert result.promotion_needed
    assert not result.candidate_in_canonical
    assert result.canonical_sha == base
    assert result.candidate_sha == checkpoint.commit_sha
    assert result.remote_stable
    assert _worktree_fingerprint(executor) == before
    assert _bare_sha(bare, config.canonical_branch) == canonical_before
    assert _bare_sha(bare, BRANCH) == candidate_before


def test_promotion_reports_already_promoted_when_heads_match(tmp_path: Path) -> None:
    control, executor, config, _, run, checkpoint, bare, _ = _published(tmp_path)
    _git(
        executor,
        "push",
        str(bare),
        f"{checkpoint.commit_sha}:refs/heads/{config.canonical_branch}",
    )
    result = check_promotion_readiness(
        control, config, run.run_id, _remote_target=str(bare)
    )
    assert result.classification == "ALREADY_PROMOTED"
    assert not result.fast_forward_possible
    assert not result.promotion_needed
    assert result.candidate_in_canonical


def test_promotion_reports_already_included_when_canonical_is_descendant(tmp_path: Path) -> None:
    control, executor, config, _, run, checkpoint, bare, _ = _published(tmp_path)
    descendant, descendant_sha = _descendant_commit(
        tmp_path, executor, checkpoint.commit_sha, "canonical-descendant"
    )
    _git(
        descendant,
        "push",
        str(bare),
        f"{descendant_sha}:refs/heads/{config.canonical_branch}",
    )

    result = check_promotion_readiness(
        control, config, run.run_id, _remote_target=str(bare)
    )
    assert result.classification == "ALREADY_INCLUDED"
    assert result.canonical_sha == descendant_sha
    assert result.candidate_in_canonical
    assert not result.promotion_needed


def test_promotion_blocks_diverged_canonical(tmp_path: Path) -> None:
    control, _, config, _, run, checkpoint, bare, base = _published(tmp_path)
    sibling, sibling_sha = _make_sibling_commit(
        tmp_path, control, base, "canonical-diverged"
    )
    _git(
        sibling,
        "push",
        str(bare),
        f"{sibling_sha}:refs/heads/{config.canonical_branch}",
    )

    result = check_promotion_readiness(
        control, config, run.run_id, _remote_target=str(bare)
    )
    assert result.classification == "BLOCKED_DIVERGED"
    assert result.blocked
    assert not result.fast_forward_possible
    assert not result.promotion_needed
    assert result.candidate_sha == checkpoint.commit_sha
    assert result.canonical_sha == sibling_sha


def test_promotion_rejects_candidate_remote_changed_after_publish(tmp_path: Path) -> None:
    control, executor, config, _, run, checkpoint, bare, _ = _published(tmp_path)
    descendant, descendant_sha = _descendant_commit(
        tmp_path, executor, checkpoint.commit_sha, "candidate-moved"
    )
    _git(descendant, "push", str(bare), f"{descendant_sha}:refs/heads/{BRANCH}")

    with pytest.raises(PromotionCheckError, match="published candidate is no longer valid"):
        check_promotion_readiness(control, config, run.run_id, _remote_target=str(bare))
    assert _bare_sha(bare, BRANCH) == descendant_sha


def test_promotion_requires_immutable_publish_evidence(tmp_path: Path) -> None:
    control, _, config, _, run, _, bare, _ = _published(tmp_path)
    evidence = run_directory(control, config, run.run_id) / "publish.json"
    evidence.unlink()

    with pytest.raises(PromotionCheckError, match="publish evidence is missing"):
        check_promotion_readiness(control, config, run.run_id, _remote_target=str(bare))


def test_promotion_rejects_tampered_publish_evidence(tmp_path: Path) -> None:
    control, _, config, _, run, _, bare, _ = _published(tmp_path)
    evidence = run_directory(control, config, run.run_id) / "publish.json"
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["remote_after"] = "0" * 40
    evidence.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(PromotionCheckError, match="publish evidence mismatch for remote_after"):
        check_promotion_readiness(control, config, run.run_id, _remote_target=str(bare))


def test_promotion_rejects_missing_canonical_remote(tmp_path: Path) -> None:
    control, _, config, _, run, _, bare, _ = _published(tmp_path)
    _git(
        control,
        "--git-dir",
        str(bare),
        "update-ref",
        "-d",
        f"refs/heads/{config.canonical_branch}",
    )
    with pytest.raises(PromotionCheckError, match="canonical remote branch is missing"):
        check_promotion_readiness(control, config, run.run_id, _remote_target=str(bare))


def test_promotion_detects_remote_race_during_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, _, config, _, run, _, bare, base = _published(tmp_path)
    racer, racer_sha = _make_sibling_commit(tmp_path, control, base, "canonical-race")
    original_is_ancestor = promotion_module._is_ancestor
    raced = False

    def racing_is_ancestor(root: Path, older: str, newer: str) -> bool:
        nonlocal raced
        if not raced:
            raced = True
            _git(
                racer,
                "push",
                str(bare),
                f"{racer_sha}:refs/heads/{config.canonical_branch}",
            )
        return original_is_ancestor(root, older, newer)

    monkeypatch.setattr(promotion_module, "_is_ancestor", racing_is_ancestor)
    with pytest.raises(PromotionCheckError, match="remote branch state changed"):
        check_promotion_readiness(control, config, run.run_id, _remote_target=str(bare))
    assert raced
    assert _bare_sha(bare, config.canonical_branch) == racer_sha


def test_promotion_respects_executor_operation_lease(tmp_path: Path) -> None:
    control, executor, config, _, run, _, bare, _ = _published(tmp_path)
    with operation_lease(control, config, executor, "external-test"):
        with pytest.raises(PromotionCheckError, match="lease"):
            check_promotion_readiness(
                control, config, run.run_id, _remote_target=str(bare)
            )
